"""Blast-radius: sidecar/manifest/metadata matching, zero-match file-anchor warnings,
and tolerance of a write-locked warm catalog (debt #2683 and #2684).

Real throwaway git repo and real catalog, no mocks. The lock test holds a
read-write DuckDB connection to bathos.db from a SEPARATE process, which is the
only way DuckDB's file lock is actually exercised.
"""

from __future__ import annotations

import dataclasses
import gc
import json
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path

import duckdb
import pytest

from bathos.blast_radius import assess_blast_radius
from bathos.catalog import init_catalog, write_run
from bathos.cli_render import render_or_exit
from bathos.compact import compact
from bathos.schema import Run

_QUERY_SET = "data/queries_v2.csv"


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(["init"], r)
    _git(["config", "user.email", "test@example.com"], r)
    _git(["config", "user.name", "Test"], r)
    (r / "scripts").mkdir()
    (r / "scripts" / "score.py").write_text("x = 1\n")
    _git(["add", "."], r)
    _git(["commit", "-m", "init"], r)
    return r


@pytest.fixture
def catalog_dir(tmp_path):
    cat = tmp_path / "catalog"
    init_catalog(cat)
    return cat


def _write(catalog_dir, **overrides) -> Run:
    fields = dict(
        project_slug="proj",
        command="python scripts/score.py",
        argv=["python", "scripts/score.py"],
        git_hash="deadbeef",
        git_branch="main",
        git_dirty=False,
    )
    fields.update(overrides)
    run = Run(**fields)
    write_run(run, catalog_dir)
    return run


class TestSidecarAndManifestText:
    def test_file_named_only_inside_sidecar_text_is_matched(self, repo, catalog_dir, tmp_path):
        sidecar = tmp_path / "score.bth.toml"
        sidecar.write_text(f'[experiment]\nquery_set = "{_QUERY_SET}"\n')
        run = _write(catalog_dir, sidecar_path=str(sidecar))

        report = assess_blast_radius(catalog_dir, repo, files=[_QUERY_SET])

        hits = {m.run_id: m for m in report.affected + report.unverifiable}
        assert run.id in hits, "sidecar text reference must count as a dependence match"
        assert run.id not in report.unaffected_run_ids
        assert hits[run.id].matched_files == [_QUERY_SET]
        assert "sidecar text" in hits[run.id].reason

    def test_file_named_only_inside_manifest_text_is_matched(self, repo, catalog_dir, tmp_path):
        manifest = tmp_path / "manifest.json"
        manifest.write_text(f'{{"inputs": ["{_QUERY_SET}"]}}')
        run = _write(catalog_dir, manifest_path=str(manifest))

        report = assess_blast_radius(catalog_dir, repo, files=[_QUERY_SET])

        hits = {m.run_id: m for m in report.affected + report.unverifiable}
        assert run.id in hits
        assert "manifest text" in hits[run.id].reason

    def test_manifest_path_field_is_matched(self, repo, catalog_dir):
        run = _write(catalog_dir, manifest_path=f"/abs/{_QUERY_SET}")

        report = assess_blast_radius(catalog_dir, repo, files=[_QUERY_SET])

        hits = {m.run_id: m for m in report.affected + report.unverifiable}
        assert run.id in hits
        assert "manifest path" in hits[run.id].reason

    def test_unreadable_sidecar_is_skipped_silently(self, repo, catalog_dir, tmp_path):
        run = _write(catalog_dir, sidecar_path=str(tmp_path / "does_not_exist.bth.toml"))

        report = assess_blast_radius(catalog_dir, repo, files=[_QUERY_SET])

        assert run.id in report.unaffected_run_ids
        assert not any("does_not_exist" in w for w in report.warnings)

    def test_oversized_sidecar_is_capped_at_one_mib_and_says_so(self, repo, catalog_dir, tmp_path):
        sidecar = tmp_path / "big.bth.toml"
        sidecar.write_text("#" + "x" * (1 << 20) + f"\nquery_set = {_QUERY_SET}\n")
        run = _write(catalog_dir, sidecar_path=str(sidecar))

        report = assess_blast_radius(catalog_dir, repo, files=[_QUERY_SET])

        assert run.id in report.unaffected_run_ids
        assert any("1 MiB" in w and "big.bth.toml" in w for w in report.warnings)


class TestZeroMatchFileAnchorWarning:
    def test_unmatched_file_anchor_warns_that_zero_is_unverified(self, repo, catalog_dir):
        run = _write(catalog_dir)  # command mentions only scripts/score.py

        report = assess_blast_radius(
            catalog_dir, repo, files=[_QUERY_SET, "src/scorer_lib.py"]
        )

        assert report.affected == [] and report.unverifiable == []
        assert run.id in report.unaffected_run_ids, "unaffected_run_ids meaning is unchanged"
        assert len(report.warnings) == 2
        for path in (_QUERY_SET, "src/scorer_lib.py"):
            matching = [w for w in report.warnings if path in w]
            assert len(matching) == 1
            assert "does NOT mean" in matching[0]
            assert "import closure" in matching[0]
            assert "unverified" in matching[0]

    def test_matched_file_anchor_emits_no_zero_match_warning(self, repo, catalog_dir):
        _write(catalog_dir)

        report = assess_blast_radius(catalog_dir, repo, files=["scripts/score.py"])

        assert len(report.affected) == 1
        assert report.warnings == []


class TestRenderedWarnings:
    def test_mcp_result_carries_warnings_key(self, repo, catalog_dir):
        from bathos.mcp import blast_radius_assess_tool

        _write(catalog_dir)
        result = blast_radius_assess_tool(
            catalog_dir=str(catalog_dir), project_root=str(repo), files=_QUERY_SET, flag=False
        )

        assert "error" not in result
        assert result["unaffected_run_ids"] and len(result["warnings"]) == 1
        assert _QUERY_SET in result["warnings"][0]

    def test_cli_warning_block_goes_to_stderr_and_json_stays_on_stdout(
        self, repo, catalog_dir, capsys
    ):
        _write(catalog_dir)
        report = assess_blast_radius(catalog_dir, repo, files=[_QUERY_SET])

        render_or_exit(dataclasses.asdict(report))

        captured = capsys.readouterr()
        payload = json.loads(captured.out)  # stdout is still pure JSON
        assert payload["warnings"] == report.warnings
        assert "unverified coverage" in captured.err
        assert f"WARNING: file anchor '{_QUERY_SET}'" in captured.err


@contextmanager
def _holding_write_lock(db_path: Path, tmp_path: Path):
    """Hold a DuckDB file open read-WRITE in a subprocess (exclusive file lock)."""
    gc.collect()  # drop any cached in-process DuckDB instance for this path
    ready = tmp_path / "ready"
    holder = (
        "import sys, duckdb\n"
        "con = duckdb.connect(sys.argv[1])\n"
        "con.execute('SELECT 1').fetchall()\n"
        "open(sys.argv[2], 'w').write('held')\n"
        "sys.stdin.read()\n"
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", holder, str(db_path), str(ready)],
        stdin=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 30
        while not ready.exists():
            if proc.poll() is not None:
                raise AssertionError(f"lock holder died: {proc.stderr.read()}")
            if time.monotonic() > deadline:
                raise AssertionError("lock holder never took the lock")
            time.sleep(0.02)
        yield
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


class TestLockedWarmCatalog:
    def test_assess_completes_while_warm_db_is_write_locked(self, repo, catalog_dir, tmp_path):
        run = _write(catalog_dir)
        compact(catalog_dir)  # creates bathos.db, so the warm tier is the one that gets locked

        with _holding_write_lock(catalog_dir / "bathos.db", tmp_path):
            # Precondition: the lock really does shut out a read-only opener.
            with pytest.raises(duckdb.Error, match="Could not set lock"):
                duckdb.connect(str(catalog_dir / "bathos.db"), read_only=True)

            report = assess_blast_radius(catalog_dir, repo, files=["scripts/score.py"])

        assert [m.run_id for m in report.affected] == [run.id]
        # The run list came from the cool tier and the warning names it.
        assert any("cool tier" in w for w in report.warnings)
        # Campaign membership could not be read and the warning says so.
        assert any("campaign" in w for w in report.warnings)
        for w in report.warnings:
            assert "PID" not in w
            assert "kill" not in w.lower()

    def test_assess_completes_while_index_db_is_write_locked_in_log_mode(
        self, repo, catalog_dir, tmp_path, monkeypatch
    ):
        """Log-mode catalogs read through index.db (connect_read ATTACHes it), so the
        same lock can surface there. Forced via the test-only BTH_LOG_MODE gate."""
        from bathos.index import index_db_path, init_index_schema

        monkeypatch.setenv("BTH_LOG_MODE", "1")
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
        run = _write(catalog_dir)
        idx = index_db_path(catalog_dir)
        con = duckdb.connect(str(idx))
        init_index_schema(con)
        con.close()

        with _holding_write_lock(idx, tmp_path):
            report = assess_blast_radius(catalog_dir, repo, files=["scripts/score.py"])

        assert [m.run_id for m in report.affected] == [run.id]
        assert any("cool tier" in w for w in report.warnings)
        assert any("campaign" in w for w in report.warnings)
        for w in report.warnings:
            assert "PID" not in w
            assert "kill" not in w.lower()
