"""Regression tests for the blast-radius perf/correctness debt cleanups
(praxia debt #1488, #1487, #1486, #1475, #1473). Each test pins the exact same
result the pre-fix code produced (behavior-preserving refactors), plus one
call-count assertion proving the specific N+1/double-scan/per-record-connection
pattern is actually gone -- and, for #1473, an error-path test proving a real
git failure is no longer conflated with "not an ancestor".
"""

from __future__ import annotations

import json
import subprocess

import duckdb
import pytest

import bathos.blast_radius as blast_radius
import bathos.checker as checker
from bathos.blast_radius import (
    BlastRadiusMatch,
    BlastRadiusReport,
    _clauses_backed_by_runs,
    _is_ancestor,
    assess_blast_radius,
    flag_blast_radius,
)
from bathos.catalog import init_catalog, write_run
from bathos.checker import hash_dependency_lock
from bathos.schema import Run


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _commit_file(repo, relpath, content, message):
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _git(["add", relpath], repo)
    _git(["commit", "-m", message], repo)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(["init"], r)
    _git(["config", "user.email", "test@example.com"], r)
    _git(["config", "user.name", "Test"], r)
    return r


@pytest.fixture
def catalog_dir(tmp_path):
    cat = tmp_path / "catalog"
    init_catalog(cat)
    return cat


def _report(affected=(), unverifiable=()):
    return BlastRadiusReport(
        anchor_kind="commit",
        anchor_value="deadbeef",
        changed_files=["src/foo.py"],
        affected=list(affected),
        unverifiable=list(unverifiable),
        unaffected_run_ids=[],
    )


class _FakeClaim:
    """Minimal stand-in for bathos.claim.ClaimFile -- _clauses_backed_by_runs only
    ever reads `.union_gate_clauses` (list[dict]), so a full ClaimFile isn't needed."""

    def __init__(self, clauses):
        self.union_gate_clauses = clauses


class TestClausesBackedByRunsBatching:
    """Debt #1488: _clauses_backed_by_runs used to issue one DuckDB query per
    (clause, run_id) pair. Pin identical results AND prove exactly one query
    is now issued regardless of clause/run count."""

    def _make_db(self):
        db = duckdb.connect(":memory:")
        db.execute("CREATE TABLE runs (id VARCHAR, claim_discriminates VARCHAR)")
        db.execute(
            "INSERT INTO runs VALUES (?, ?), (?, ?), (?, ?)",
            [
                "run-1", json.dumps(["H_primary"]),
                "run-2", json.dumps(["H_null"]),
                "run-3", None,
            ],
        )
        return db

    def test_identical_results_on_fixture(self):
        db = self._make_db()
        claim = _FakeClaim(
            [
                {"id": "clause-1", "hypothesis_ids": ["H_primary"]},
                {"id": "clause-2", "hypothesis_ids": ["H_null"]},
                {"id": "clause-3", "hypothesis_ids": ["H_missing"]},
                {
                    "id": "clause-pc",
                    "hypothesis_ids": ["H_primary"],
                    "positive_control": True,
                },
            ]
        )

        implicated = _clauses_backed_by_runs(db, claim, {"run-1", "run-2", "run-3"})

        # clause-1 backed by run-1, clause-2 backed by run-2, clause-3 has no
        # covering run, clause-pc is skipped (positive_control, out of scope here).
        assert sorted(implicated) == ["clause-1", "clause-2"]

    def test_no_matching_run_ids_yields_no_clauses(self):
        db = self._make_db()
        claim = _FakeClaim([{"id": "clause-1", "hypothesis_ids": ["H_primary"]}])
        assert _clauses_backed_by_runs(db, claim, set()) == []

    def test_batches_into_one_query_regardless_of_clause_and_run_count(self):
        db = self._make_db()
        claim = _FakeClaim(
            [
                {"id": "clause-1", "hypothesis_ids": ["H_primary"]},
                {"id": "clause-2", "hypothesis_ids": ["H_null"]},
                {"id": "clause-3", "hypothesis_ids": ["H_missing"]},
            ]
        )

        class _CountingConn:
            def __init__(self, real):
                self._real = real
                self.execute_calls = 0

            def execute(self, *args, **kwargs):
                self.execute_calls += 1
                return self._real.execute(*args, **kwargs)

        counting = _CountingConn(db)
        implicated = _clauses_backed_by_runs(counting, claim, {"run-1", "run-2", "run-3"})

        assert sorted(implicated) == ["clause-1", "clause-2"]
        # Old N+1 code issued up to len(clauses) * len(run_ids) = 3*3 = 9 queries
        # for this fixture. Batched version issues exactly one, regardless of
        # how many clauses or run_ids are involved.
        assert counting.execute_calls == 1, (
            f"expected exactly one batched query, got {counting.execute_calls} -- "
            "debt #1488 regression (N+1 per clause/run_id pair)"
        )


class TestDependencyLockHashedOnce:
    """Debt #1487: check_dependency_lock_drift used to re-hash uv.lock once per
    run inside assess_blast_radius's dependency-anchor loop. Pin identical
    results AND prove the lockfile is hashed exactly once per assess() call."""

    def test_identical_results_on_fixture(self, repo, catalog_dir):
        (repo / "uv.lock").write_text("old-lock-content\n")
        old_hash = hash_dependency_lock(repo)

        drifted = Run(
            project_slug="proj", command="a.py", argv=["a.py"], git_hash="a1",
            git_branch="main", git_dirty=False, dependency_lock_sha256=old_hash,
        )
        write_run(drifted, catalog_dir)
        stable = Run(
            project_slug="proj", command="b.py", argv=["b.py"], git_hash="a2",
            git_branch="main", git_dirty=False,
        )  # no recorded dependency_lock_sha256 -> unverifiable
        write_run(stable, catalog_dir)

        (repo / "uv.lock").write_text("new-lock-content\n")  # lock changes

        report = assess_blast_radius(catalog_dir, repo, dependency=True)

        assert [m.run_id for m in report.affected] == [drifted.id]
        assert [m.run_id for m in report.unverifiable] == [stable.id]

    def test_hash_dependency_lock_called_exactly_once_for_many_runs(
        self, repo, catalog_dir, monkeypatch
    ):
        (repo / "uv.lock").write_text("stable-content\n")
        current_hash = hash_dependency_lock(repo)

        # 5 runs all carrying a recorded dependency_lock_sha256 -- the old code
        # called check_dependency_lock_drift() (which re-hashed uv.lock every
        # time) once per such run, on top of the one hash for anchor_value.
        for i in range(5):
            write_run(
                Run(
                    project_slug="proj", command=f"r{i}.py", argv=[f"r{i}.py"],
                    git_hash=f"h{i}", git_branch="main", git_dirty=False,
                    dependency_lock_sha256=current_hash,
                ),
                catalog_dir,
            )

        real_hash = hash_dependency_lock
        calls = []

        def counting_hash(workspace_root):
            calls.append(workspace_root)
            return real_hash(workspace_root)

        # Both module-level bindings must be patched: `from bathos.checker import
        # hash_dependency_lock` in blast_radius.py copies the name at import
        # time, so patching one module's namespace doesn't affect the other.
        monkeypatch.setattr(blast_radius, "hash_dependency_lock", counting_hash)
        monkeypatch.setattr(checker, "hash_dependency_lock", counting_hash)

        report = assess_blast_radius(catalog_dir, repo, dependency=True)

        assert len(report.unaffected_run_ids) == 5  # lock unchanged -> none affected
        assert len(calls) == 1, (
            f"expected hash_dependency_lock() called exactly once, got {len(calls)} -- "
            "debt #1487 regression (re-hash per run)"
        )


class TestAssessBlastRadiusSingleScan:
    """Debt #1486: assess_blast_radius used to scan the catalog twice
    (check_runs()'s own internal list_runs() call, plus its own separate
    list_runs() call). Pin identical results AND prove list_runs() is now
    called exactly once per assess() call."""

    def test_identical_results_on_fixture(self, repo, catalog_dir):
        pre_fix_sha = _commit_file(repo, "scripts/experiments/foo.py", "a = 1\n", "initial")
        fix_sha = _commit_file(repo, "scripts/experiments/foo.py", "a = 2\n", "fix bug")

        write_run(
            Run(
                project_slug="proj", command="scripts/experiments/foo.py",
                argv=["scripts/experiments/foo.py"], git_hash=pre_fix_sha,
                git_branch="main", git_dirty=False,
            ),
            catalog_dir,
        )

        report = assess_blast_radius(catalog_dir, repo, commit=fix_sha)

        assert len(report.affected) == 1

    def test_list_runs_called_exactly_once(self, repo, catalog_dir, monkeypatch):
        from bathos import query

        pre_fix_sha = _commit_file(repo, "scripts/experiments/foo.py", "a = 1\n", "initial")
        fix_sha = _commit_file(repo, "scripts/experiments/foo.py", "a = 2\n", "fix bug")

        for _ in range(3):
            write_run(
                Run(
                    project_slug="proj", command="scripts/experiments/foo.py",
                    argv=["scripts/experiments/foo.py"], git_hash=pre_fix_sha,
                    git_branch="main", git_dirty=False,
                ),
                catalog_dir,
            )

        real_list_runs = query.list_runs
        calls = []

        def counting_list_runs(*args, **kwargs):
            calls.append((args, kwargs))
            return real_list_runs(*args, **kwargs)

        # Same cross-module binding note as above: blast_radius.py and
        # checker.py each imported their own `list_runs` name from
        # bathos.query at import time.
        monkeypatch.setattr(blast_radius, "list_runs", counting_list_runs)
        monkeypatch.setattr(checker, "list_runs", counting_list_runs)

        report = assess_blast_radius(catalog_dir, repo, commit=fix_sha)

        assert len(report.affected) == 3
        assert len(calls) == 1, (
            f"expected list_runs() called exactly once, got {len(calls)} -- "
            "debt #1486 regression (check_runs()+list_runs() double-scan)"
        )


class TestFlagBlastRadiusConnectionReuse:
    """Debt #1475: flag_blast_radius used to open a fresh warm-tier DuckDB
    connection per record (once via fold_blast_radius_state, once via
    append_ledger_record's _insert_warm_row). Pin identical results AND
    prove exactly one connection is opened for an entire batch."""

    def test_identical_results_on_fixture(self, tmp_path):
        catalog_dir = tmp_path / "catalog"
        catalog_dir.mkdir(parents=True)

        matches = [
            BlastRadiusMatch(
                run_id=f"run-{i}", git_hash="abc", command="c",
                matched_files=["c"], reason="r",
            )
            for i in range(3)
        ]
        records = flag_blast_radius(_report(unverifiable=matches), catalog_dir)

        assert len(records) == 3
        assert {r.entity_id for r in records} == {"run-0", "run-1", "run-2"}
        assert all(r.to_state == "unverifiable" for r in records)
        assert all(r.from_state == "clean" for r in records)

    def test_connect_called_exactly_once_for_a_batch(self, tmp_path, monkeypatch):
        catalog_dir = tmp_path / "catalog"
        catalog_dir.mkdir(parents=True)

        real_connect = blast_radius._connect
        connect_calls = []

        def counting_connect(catalog_dir_arg):
            connect_calls.append(catalog_dir_arg)
            return real_connect(catalog_dir_arg)

        monkeypatch.setattr(blast_radius, "_connect", counting_connect)

        # unverifiable-only matches skip the (separate, out-of-scope) get_run/
        # shadow-verdict path entirely, isolating this measurement to exactly
        # the ledger-table connection this debt item targets.
        matches = [
            BlastRadiusMatch(
                run_id=f"run-{i}", git_hash="abc", command="c",
                matched_files=["c"], reason="r",
            )
            for i in range(5)
        ]
        records = flag_blast_radius(_report(unverifiable=matches), catalog_dir)

        assert len(records) == 5
        assert len(connect_calls) == 1, (
            f"expected exactly one _connect() call reused across the batch, got "
            f"{len(connect_calls)} -- debt #1475 regression (fresh connection per record)"
        )

    def test_no_connection_opened_for_an_empty_report(self, tmp_path, monkeypatch):
        catalog_dir = tmp_path / "catalog"
        catalog_dir.mkdir(parents=True)

        real_connect = blast_radius._connect
        connect_calls = []
        monkeypatch.setattr(
            blast_radius,
            "_connect",
            lambda c: connect_calls.append(c) or real_connect(c),
        )

        records = flag_blast_radius(_report(), catalog_dir)

        assert records == []
        assert connect_calls == []


class TestIsAncestorErrorSurfacing:
    """Debt #1473: _is_ancestor conflated git's own "not an ancestor" answer
    (rc=1) with a genuine git failure (any other nonzero rc). The former is a
    legitimate, silent False; the latter must now be surfaced via event()."""

    def test_rc_1_is_not_an_ancestor_and_is_not_logged(self, repo, monkeypatch):
        base_sha = _commit_file(repo, "f.txt", "a\n", "base")
        tip_sha = _commit_file(repo, "f.txt", "b\n", "tip")
        # tip is NOT an ancestor of base (base is an ancestor of tip, not vice
        # versa) -- legitimate rc=1, no error.
        events = []
        monkeypatch.setattr(blast_radius, "event", lambda name, **kw: events.append((name, kw)))

        assert _is_ancestor(tip_sha, base_sha, repo) is False
        assert events == []

    def test_rc_0_is_an_ancestor(self, repo, monkeypatch):
        base_sha = _commit_file(repo, "f.txt", "a\n", "base")
        tip_sha = _commit_file(repo, "f.txt", "b\n", "tip")
        events = []
        monkeypatch.setattr(blast_radius, "event", lambda name, **kw: events.append((name, kw)))

        assert _is_ancestor(base_sha, tip_sha, repo) is True
        assert events == []

    def test_genuine_git_error_is_surfaced_not_silently_false(self, repo, monkeypatch):
        _commit_file(repo, "f.txt", "a\n", "initial")
        events = []
        monkeypatch.setattr(blast_radius, "event", lambda name, **kw: events.append((name, kw)))

        unresolvable_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
        result = _is_ancestor(unresolvable_sha, "HEAD", repo)

        assert result is None  # unknown: caller flags the run as affected
        assert len(events) == 1, (
            "expected the genuine git error (unresolvable sha) to be surfaced via "
            "event(), not silently swallowed as an ordinary rc=1 'not ancestor' -- "
            "debt #1473 regression"
        )
        name, fields = events[0]
        assert name == "blast_radius.ancestor_check_error"
        assert fields["candidate_sha"] == unresolvable_sha
        assert fields["boundary_sha"] == "HEAD"
        assert fields["returncode"] not in (0, 1)
        assert fields["returncode"] == 128
