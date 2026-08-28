# Blast-Radius Assessment (Phase 1) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Implement `bathos.blast_radius` — a new append-only ledger + assessment function that
answers "a bug was found/fixed in this commit/file — which past runs did it implicate?" —
scoped to Phase 1 (backlog #4551): run-level flagging only, commit/commit-range/file anchors,
manual invocation, no gating, no shadow-mode subsystems.

**Architecture:** Mirror `bathos.trust_ledger`'s proven dual-write shape (cool-tier Parquet
fragment per append + warm DuckDB `blast_radius_ledger` table, re-ingested at every
`compact()`, fold-latest-wins by `amended_at`) but composite-key on `(entity_type, entity_id)`
instead of `content_hash`, so the schema is forward-compatible with Phase 2's campaign/claim
records without a future migration. The assessment itself is a pure read-path: `git diff
--name-only` for the changed-file set, `git merge-base --is-ancestor` for pre/post-fix
ancestry, reusing `bathos.checker.check_runs()`'s DIRTY_RUN/UNKNOWN_CODE classification for the
"unverifiable" bucket (AC-5) rather than re-deriving it.

**Tech Stack:** Python 3.13, DuckDB, PyArrow, Typer CLI, `cisternal.tool` MCP registration
(same stack as `bathos.trust_ledger`/`bathos.readback`).

**Spec:** `.praxia/docs/specs/260826_blast-radius-assessment-skill.md` — ACs referenced below
(AC-1, AC-2, AC-4, AC-5, AC-6, AC-9, AC-11, AC-13) are Phase 1's slice of that spec.

**Deliberate scope note (read before Task 3):** the spec's Decision Log #5 says clearing
"mirrors the trust-ledger's PASS-before-promote gate." Phase 1 implements the *manual* half of
that literally (AC-9 requires a re-attestation action, not automatic clearing) but does **not**
wire it to a new attestation kind — `clear_blast_radius_flag` requires only a non-empty `reason`
string, not a validated PASS attestation. Building an attestation-gated version is a natural
Phase-2-or-later follow-up, not attempted here (YAGNI — nothing in backlog #4551's scope
requires it).

---

### Task 1: Core ledger primitives

**Files:**
- Create: `src/bathos/blast_radius.py`
- Test: `tests/test_blast_radius_ledger.py`

**Step 1: Write the failing tests**

```python
"""Blast-radius ledger tests (Phase 1, backlog #4551, spec 260826).

Mirrors tests/test_trust_ledger.py's TestAppendAndFoldLatestWins shape, adapted for
the (entity_type, entity_id) composite key instead of content_hash.
"""

from __future__ import annotations

import pytest

from bathos.blast_radius import (
    BlastRadiusRecord,
    append_ledger_record,
    fold_blast_radius_state,
    latest_ledger_record,
    read_ledger_fragments,
)


@pytest.fixture
def catalog_dir(tmp_path):
    cat = tmp_path / "catalog"
    cat.mkdir(parents=True)
    return cat


class TestAppendAndFoldLatestWins:
    def test_fold_returns_clean_when_never_flagged(self, catalog_dir):
        assert fold_blast_radius_state(catalog_dir, "run", "run-nonexistent") == "clean"

    def test_single_append_is_visible_via_fold(self, catalog_dir):
        record = BlastRadiusRecord(
            entity_type="run",
            entity_id="run-001",
            from_state="clean",
            to_state="affected",
            match_reason="touches src/bathos/checker.py",
        )
        append_ledger_record(record, catalog_dir)

        assert fold_blast_radius_state(catalog_dir, "run", "run-001") == "affected"

    def test_supersede_resolves_to_latest_record(self, catalog_dir):
        first = BlastRadiusRecord(
            entity_type="run",
            entity_id="run-002",
            from_state="clean",
            to_state="affected",
            amended_at="2026-08-26T00:00:00+00:00",
        )
        second = BlastRadiusRecord(
            entity_type="run",
            entity_id="run-002",
            from_state="affected",
            to_state="cleared",
            reason="re-ran post-fix, identical output",
            amended_at="2026-08-26T01:00:00+00:00",
        )
        append_ledger_record(first, catalog_dir)
        append_ledger_record(second, catalog_dir)

        assert fold_blast_radius_state(catalog_dir, "run", "run-002") == "cleared"
        latest = latest_ledger_record(catalog_dir, "run", "run-002")
        assert latest is not None
        assert latest.reason == "re-ran post-fix, identical output"

    def test_composite_key_does_not_cross_entity_types(self, catalog_dir):
        """Same entity_id, different entity_type, must not collide (forward-compat
        with Phase 2's campaign/claim records sharing id-space with run ids)."""
        append_ledger_record(
            BlastRadiusRecord(entity_type="run", entity_id="shared-id", to_state="affected"),
            catalog_dir,
        )
        assert fold_blast_radius_state(catalog_dir, "campaign", "shared-id") == "clean"
        assert fold_blast_radius_state(catalog_dir, "run", "shared-id") == "affected"

    def test_ledger_is_append_only_both_records_present(self, catalog_dir):
        append_ledger_record(
            BlastRadiusRecord(
                entity_type="run", entity_id="run-003", to_state="affected",
                amended_at="2026-08-26T00:00:00+00:00",
            ),
            catalog_dir,
        )
        append_ledger_record(
            BlastRadiusRecord(
                entity_type="run", entity_id="run-003", to_state="cleared",
                amended_at="2026-08-26T01:00:00+00:00",
            ),
            catalog_dir,
        )

        all_records = read_ledger_fragments(catalog_dir)
        matching = [r for r in all_records if r.entity_id == "run-003"]
        assert len(matching) == 2, "append-only: both records must survive"


class TestSurvivesCompactForceRebuild:
    def test_survives_force_rebuild(self, catalog_dir):
        from bathos.compact import compact as compact_catalog

        append_ledger_record(
            BlastRadiusRecord(entity_type="run", entity_id="run-004", to_state="affected"),
            catalog_dir,
        )
        assert fold_blast_radius_state(catalog_dir, "run", "run-004") == "affected"

        result = compact_catalog(catalog_dir, force_rebuild=True)
        assert result is not None

        assert fold_blast_radius_state(catalog_dir, "run", "run-004") == "affected"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_blast_radius_ledger.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'bathos.blast_radius'`

**Step 3: Write `src/bathos/blast_radius.py` (ledger primitives section)**

```python
"""Blast-radius ledger (Phase 1, backlog #4551, spec .praxia/docs/specs/260826_blast-radius-assessment-skill.md).

Answers "a bug was found/fixed in commit/file X -- which past runs does it implicate?" --
the inverse of bathos's existing per-run drift checks (bathos.checker: git-drift,
output-SHA-drift, dependency-lock-drift), none of which answer this question.

Durability: built on the exact same substrate as bathos.trust_ledger (cool-tier immutable
Parquet fragment per append, re-ingested into a warm DuckDB table on every
bathos.compact.compact() call). The one structural difference: this ledger is
composite-keyed on (entity_type, entity_id) instead of content_hash, so Phase 2
(campaign/claim-level flagging, backlog #4552) needs no schema migration -- it is
additive on the SAME table.

States (spec Decision Log #5, #7): a fresh entity with no ledger record is implicitly
"clean". assess_blast_radius() (see below) writes "affected" or "unverifiable" records;
clear_blast_radius_flag() writes "cleared" records. There is no automatic clearing in
Phase 1 -- see this module's own docstring note on clear_blast_radius_flag for the
deliberate scope cut vs. trust_ledger's PASS-attestation-gated ratchet.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from bathos.telemetry import event

_LEDGER_TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS blast_radius_ledger (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    from_state TEXT,
    to_state TEXT NOT NULL,
    anchor_kind TEXT,
    anchor_value TEXT,
    matched_files TEXT,
    match_reason TEXT,
    reason TEXT,
    amended_at TEXT NOT NULL
)
"""

_LEDGER_FRAGMENTS_DIRNAME = "blast_radius"

_LEDGER_FRAGMENT_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string()),
        pa.field("entity_type", pa.string()),
        pa.field("entity_id", pa.string()),
        pa.field("from_state", pa.string()),
        pa.field("to_state", pa.string()),
        pa.field("anchor_kind", pa.string()),
        pa.field("anchor_value", pa.string()),
        pa.field("matched_files", pa.string()),
        pa.field("match_reason", pa.string()),
        pa.field("reason", pa.string()),
        pa.field("amended_at", pa.string()),
    ]
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class BlastRadiusRecord:
    """One append-only ledger entry: a `from_state -> to_state` transition for an
    entity identified by `(entity_type, entity_id)`."""

    entity_type: str  # "run" | "campaign" | "claim"
    entity_id: str
    to_state: str  # "affected" | "unverifiable" | "cleared"
    from_state: str | None = None
    anchor_kind: str | None = None  # "commit" | "commit_range" | "file"
    anchor_value: str | None = None
    matched_files: str | None = None  # JSON-encoded list[str]
    match_reason: str | None = None  # AC-13: why this entity matched
    reason: str | None = None  # free-form clearing justification (AC-9)
    amended_at: str = field(default_factory=_now_iso)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


def _ledger_fragments_dir(catalog_dir: Path | str) -> Path:
    return Path(catalog_dir) / _LEDGER_FRAGMENTS_DIRNAME


def write_ledger_fragment(record: BlastRadiusRecord, catalog_dir: Path | str) -> None:
    """Write an immutable cool-tier Parquet fragment for one ledger record.

    Mirrors `bathos.trust_ledger.write_ledger_fragment` exactly: one fragment per
    record, atomic tmp-write + POSIX rename, never superseded on read-back.
    """
    frag_dir = _ledger_fragments_dir(catalog_dir)
    frag_dir.mkdir(parents=True, exist_ok=True)
    target = frag_dir / f"blast_radius_{record.id}.parquet"
    tmp = frag_dir / f"blast_radius_{record.id}.tmp.parquet"

    t_start = time.monotonic()
    table = pa.table(
        {
            "id": [record.id],
            "entity_type": [record.entity_type],
            "entity_id": [record.entity_id],
            "from_state": [record.from_state],
            "to_state": [record.to_state],
            "anchor_kind": [record.anchor_kind],
            "anchor_value": [record.anchor_value],
            "matched_files": [record.matched_files],
            "match_reason": [record.match_reason],
            "reason": [record.reason],
            "amended_at": [record.amended_at],
        },
        schema=_LEDGER_FRAGMENT_SCHEMA,
    )
    pq.write_table(table, tmp)
    tmp.rename(target)  # atomic on POSIX
    duration_ms = (time.monotonic() - t_start) * 1000

    event(
        "blast_radius.write_fragment", path=str(target), rows=1, duration_ms=int(duration_ms)
    )


def read_ledger_fragments(catalog_dir: Path | str) -> list[BlastRadiusRecord]:
    """Read every cool-tier ledger fragment, unfolded (full history)."""
    frag_dir = _ledger_fragments_dir(catalog_dir)
    if not frag_dir.exists():
        return []
    parquet_files = list(frag_dir.glob("blast_radius_*.parquet"))
    if not parquet_files:
        return []

    tables = [pq.read_table(f) for f in parquet_files]
    combined = pa.concat_tables(tables, promote_options="permissive")
    pydict = combined.to_pydict()

    records = []
    for i in range(combined.num_rows):
        records.append(
            BlastRadiusRecord(
                id=pydict["id"][i],
                entity_type=pydict["entity_type"][i],
                entity_id=pydict["entity_id"][i],
                from_state=pydict["from_state"][i],
                to_state=pydict["to_state"][i],
                anchor_kind=pydict["anchor_kind"][i],
                anchor_value=pydict["anchor_value"][i],
                matched_files=pydict["matched_files"][i],
                match_reason=pydict["match_reason"][i],
                reason=pydict["reason"][i],
                amended_at=pydict["amended_at"][i],
            )
        )
    return records


def _connect(catalog_dir: Path | str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(Path(catalog_dir) / "bathos.db"))
    con.execute(_LEDGER_TABLE_SCHEMA)
    return con


def _insert_warm_row(record: BlastRadiusRecord, catalog_dir: Path | str) -> None:
    con = _connect(catalog_dir)
    try:
        existing = con.execute(
            "SELECT id FROM blast_radius_ledger WHERE id = ?", [record.id]
        ).fetchone()
        if existing:
            return  # append-only: never update an existing ledger row
        con.execute(
            "INSERT INTO blast_radius_ledger "
            "(id, entity_type, entity_id, from_state, to_state, anchor_kind, anchor_value, "
            "matched_files, match_reason, reason, amended_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                record.id,
                record.entity_type,
                record.entity_id,
                record.from_state,
                record.to_state,
                record.anchor_kind,
                record.anchor_value,
                record.matched_files,
                record.match_reason,
                record.reason,
                record.amended_at,
            ],
        )
    finally:
        con.close()


def append_ledger_record(
    record: BlastRadiusRecord, catalog_dir: Path | str
) -> BlastRadiusRecord:
    """Durably append one ledger record: cool-tier fragment + warm-tier row."""
    write_ledger_fragment(record, catalog_dir)
    _insert_warm_row(record, catalog_dir)
    event(
        "blast_radius.append",
        entity_type=record.entity_type,
        entity_id=record.entity_id,
        from_state=record.from_state,
        to_state=record.to_state,
    )
    return record


def latest_ledger_record(
    catalog_dir: Path | str, entity_type: str, entity_id: str
) -> BlastRadiusRecord | None:
    """Return the latest-by-`amended_at` ledger record for `(entity_type, entity_id)`,
    or `None` if no record exists."""
    con = _connect(catalog_dir)
    try:
        row = con.execute(
            "SELECT id, entity_type, entity_id, from_state, to_state, anchor_kind, "
            "anchor_value, matched_files, match_reason, reason, amended_at "
            "FROM blast_radius_ledger WHERE entity_type = ? AND entity_id = ? "
            "ORDER BY amended_at DESC LIMIT 1",
            [entity_type, entity_id],
        ).fetchone()
        if row is None:
            return None
        return BlastRadiusRecord(
            id=row[0],
            entity_type=row[1],
            entity_id=row[2],
            from_state=row[3],
            to_state=row[4],
            anchor_kind=row[5],
            anchor_value=row[6],
            matched_files=row[7],
            match_reason=row[8],
            reason=row[9],
            amended_at=row[10],
        )
    finally:
        con.close()


def fold_blast_radius_state(catalog_dir: Path | str, entity_type: str, entity_id: str) -> str:
    """Fold the ledger for `(entity_type, entity_id)`, latest-wins by `amended_at`.

    Returns `"clean"` if no ledger record exists at all -- an entity is implicitly
    clean until something flags it (unlike bathos.trust_ledger.fold_trust_state,
    which returns `None` for "never graduated"; blast-radius has no evidence-gated
    ratchet, so a plain default string is the right shape for callers).
    """
    latest = latest_ledger_record(catalog_dir, entity_type, entity_id)
    return latest.to_state if latest is not None else "clean"
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_blast_radius_ledger.py -v`
Expected: PASS (5 tests). `TestSurvivesCompactForceRebuild` will still fail at this step
because `compact()` doesn't yet call an ingest function for this ledger -- that's Task 4.
Confirm the failure is specifically about the ledger not surviving rebuild (i.e. everything
else passes), not an unrelated error.

**Step 5: Commit**

```bash
git add src/bathos/blast_radius.py tests/test_blast_radius_ledger.py
git commit -m "feat(blast-radius): add ledger primitives (Phase 1, #4551)"
```

---

### Task 2: Assessment logic (anchors, ancestry, file-touch heuristic)

**Files:**
- Modify: `src/bathos/blast_radius.py` (append assessment section)
- Test: `tests/test_blast_radius_assess.py`

**Step 1: Write the failing tests**

```python
"""Blast-radius assessment tests (Phase 1, backlog #4551).

Uses a real throwaway git repo (tmp_path) so git diff/merge-base behavior is exercised
for real, not mocked -- these are exactly the primitives the heuristic-noise pre-mortem
(spec) said to make auditable, so the tests must prove the match_reason/matched_files
fields are actually populated, not just that a bucket assignment happened.
"""

from __future__ import annotations

import subprocess

import pytest

from bathos.blast_radius import assess_blast_radius
from bathos.catalog import init_catalog, write_run
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


def _run(catalog_dir, *, command, argv, git_hash, git_dirty=False):
    r = Run(
        project_slug="proj",
        command=command,
        argv=argv,
        git_hash=git_hash,
        git_branch="main",
        git_dirty=git_dirty,
    )
    write_run(r, catalog_dir)
    return r


class TestCommitAnchor:
    def test_run_predating_fix_is_affected(self, repo, catalog_dir):
        pre_fix_sha = _commit_file(repo, "scripts/experiments/foo.py", "buggy = True\n", "initial")
        fix_sha = _commit_file(repo, "scripts/experiments/foo.py", "buggy = False\n", "fix bug")

        _run(
            catalog_dir,
            command="scripts/experiments/foo.py",
            argv=["scripts/experiments/foo.py"],
            git_hash=pre_fix_sha,
        )

        report = assess_blast_radius(catalog_dir, repo, commit=fix_sha)

        assert report.changed_files == ["scripts/experiments/foo.py"]
        assert len(report.affected) == 1
        assert report.affected[0].matched_files == ["scripts/experiments/foo.py"]
        assert "foo.py" in report.affected[0].reason

    def test_run_at_fix_commit_itself_is_not_affected(self, repo, catalog_dir):
        _commit_file(repo, "scripts/experiments/foo.py", "buggy = True\n", "initial")
        fix_sha = _commit_file(repo, "scripts/experiments/foo.py", "buggy = False\n", "fix bug")

        _run(
            catalog_dir,
            command="scripts/experiments/foo.py",
            argv=["scripts/experiments/foo.py"],
            git_hash=fix_sha,
        )

        report = assess_blast_radius(catalog_dir, repo, commit=fix_sha)

        assert report.affected == []
        assert "run-" not in "".join(report.unaffected_run_ids)  # sanity: list is populated
        assert len(report.unaffected_run_ids) == 1

    def test_run_touching_unrelated_file_is_unaffected(self, repo, catalog_dir):
        _commit_file(repo, "scripts/experiments/foo.py", "a = 1\n", "initial")
        fix_sha = _commit_file(repo, "scripts/experiments/foo.py", "a = 2\n", "fix bug")

        _run(
            catalog_dir,
            command="scripts/experiments/bar.py",
            argv=["scripts/experiments/bar.py"],
            git_hash=fix_sha + "^",  # doesn't matter, file doesn't match
        )
        # replace with a real predating sha for realism
        pre_sha = subprocess.run(
            ["git", "rev-parse", f"{fix_sha}^"], cwd=repo, capture_output=True, text=True
        ).stdout.strip()
        run2 = _run(
            catalog_dir,
            command="scripts/experiments/bar.py",
            argv=["scripts/experiments/bar.py"],
            git_hash=pre_sha,
        )

        report = assess_blast_radius(catalog_dir, repo, commit=fix_sha)

        affected_ids = [m.run_id for m in report.affected]
        assert run2.id not in affected_ids

    def test_dirty_run_touching_changed_file_is_unverifiable(self, repo, catalog_dir):
        pre_fix_sha = _commit_file(repo, "scripts/experiments/foo.py", "a = 1\n", "initial")
        fix_sha = _commit_file(repo, "scripts/experiments/foo.py", "a = 2\n", "fix bug")

        _run(
            catalog_dir,
            command="scripts/experiments/foo.py",
            argv=["scripts/experiments/foo.py"],
            git_hash=pre_fix_sha,
            git_dirty=True,
        )

        report = assess_blast_radius(catalog_dir, repo, commit=fix_sha)

        assert report.affected == []
        assert len(report.unverifiable) == 1
        assert "DIRTY_RUN" in report.unverifiable[0].reason


class TestFileAnchor:
    def test_file_anchor_matches_without_ancestry_check(self, repo, catalog_dir):
        _commit_file(repo, "scripts/experiments/foo.py", "a = 1\n", "initial")

        run = _run(
            catalog_dir,
            command="scripts/experiments/foo.py",
            argv=["scripts/experiments/foo.py"],
            git_hash="deadbeef",  # not even a real sha -- no ancestry check for file anchor
        )

        report = assess_blast_radius(
            catalog_dir, repo, files=["scripts/experiments/foo.py"]
        )

        assert report.anchor_kind == "file"
        assert len(report.affected) == 1
        assert report.affected[0].run_id == run.id


class TestInputValidation:
    def test_requires_exactly_one_anchor(self, repo, catalog_dir):
        with pytest.raises(ValueError):
            assess_blast_radius(catalog_dir, repo)
        with pytest.raises(ValueError):
            assess_blast_radius(
                catalog_dir, repo, commit="abc", files=["x.py"]
            )
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_blast_radius_assess.py -v`
Expected: FAIL with `ImportError: cannot import name 'assess_blast_radius'`

**Step 3: Append assessment logic to `src/bathos/blast_radius.py`**

```python
# --- append below the ledger primitives section written in Task 1 ---

import subprocess  # add to top-of-file imports alongside the existing ones

from bathos.checker import check_runs
from bathos.query import list_runs
from bathos.schema import Run


@dataclass(frozen=True)
class BlastRadiusMatch:
    """One run's match against a blast-radius anchor."""

    run_id: str
    git_hash: str
    command: str
    matched_files: list[str]
    reason: str


@dataclass(frozen=True)
class BlastRadiusReport:
    """Result of one assess_blast_radius() call. Not itself persisted -- callers
    pass this to flag_blast_radius() to durably record it (AC-11: report-then-flag,
    this dataclass IS the "report" shown to the user before that write happens)."""

    anchor_kind: str  # "commit" | "commit_range" | "file"
    anchor_value: str
    changed_files: list[str]
    affected: list[BlastRadiusMatch]
    unverifiable: list[BlastRadiusMatch]
    unaffected_run_ids: list[str]


def _git_diff_name_only(a: str, b: str, project_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", a, b],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(
            f"git diff --name-only {a} {b} failed: {result.stderr.strip()}"
        )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _git_diff_name_only_range(commit_range: str, project_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", commit_range],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(
            f"git diff --name-only {commit_range} failed: {result.stderr.strip()}"
        )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _is_ancestor(candidate_sha: str, boundary_sha: str, project_root: Path) -> bool:
    """True iff candidate_sha is boundary_sha or an ancestor of it.

    Fails closed (False) on any git error -- an unresolvable sha (e.g. run.git_hash
    is a value git can't look up) must never be treated as "predates the fix".
    """
    if not candidate_sha or candidate_sha in ("unknown", "nogit"):
        return False
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", candidate_sha, boundary_sha],
        cwd=project_root,
        capture_output=True,
    )
    return result.returncode == 0


def _run_touches_files(run: Run, changed_files: list[str]) -> list[str]:
    """v1 file-path heuristic (spec Decision Log #2): a changed file "matches" a run
    if it appears as a substring of the run's command or any argv entry, in either
    direction. Deliberately coarse -- see spec Pre-mortem Record: the accepted v1
    mitigation is AC-13's match_reason auditability, not precision here.
    """
    haystacks = [run.command or "", *(run.argv or [])]
    matched: list[str] = []
    for changed in changed_files:
        changed_norm = changed.replace("\\", "/")
        for hay in haystacks:
            hay_norm = (hay or "").replace("\\", "/")
            if not hay_norm:
                continue
            if (
                changed_norm in hay_norm
                or hay_norm in changed_norm
                or hay_norm.endswith(changed_norm)
                or changed_norm.endswith(hay_norm)
            ):
                matched.append(changed)
                break
    return matched


def assess_blast_radius(
    catalog_dir: Path | str,
    project_root: Path | str,
    *,
    commit: str | None = None,
    commit_range: str | None = None,
    files: list[str] | None = None,
) -> BlastRadiusReport:
    """Assess which catalogued runs a bug/fix implicates (AC-1, AC-2, AC-4, AC-5).

    Exactly one of `commit`, `commit_range`, or `files` must be given -- these are
    the three anchor types (spec Decision Log #1). This function is a pure read: it
    does not write to the ledger. Callers pass the returned report to
    flag_blast_radius() to durably record it (AC-11: report-then-flag ordering).

    Args:
        catalog_dir: Path to the bathos catalog root.
        project_root: Path to the git repository to diff/query ancestry against.
        commit: A single fix commit SHA. Changed-file set = that commit's diff
            against its own parent. Ancestry boundary = the commit's parent (a run
            at or before the parent predates the fix -> "affected" if it touches a
            changed file; a run AT the fix commit itself already has the fix).
        commit_range: A `"<base>..<tip>"` range. Changed-file set = the whole
            range's diff. Ancestry boundary = `<base>` (a run at or before base
            predates the fix).
        files: Explicit file/symbol path(s) (AC-2). No ancestry check is applied --
            there is no commit boundary to compare against, so any run touching
            these files is "affected" regardless of when it ran.

    Returns:
        A BlastRadiusReport. Does not raise for "no runs matched" (empty buckets is
        a valid, non-error result) -- only raises ValueError for a malformed anchor
        argument combination.
    """
    n_anchors = sum(x is not None for x in (commit, commit_range, files))
    if n_anchors != 1:
        raise ValueError(
            "assess_blast_radius requires exactly one of commit, commit_range, or files "
            f"(got {n_anchors})"
        )

    project_root = Path(project_root)
    use_ancestry = True
    boundary: str | None = None

    if commit is not None:
        anchor_kind, anchor_value = "commit", commit
        changed_files = _git_diff_name_only(f"{commit}^", commit, project_root)
        boundary = f"{commit}^"
    elif commit_range is not None:
        if ".." not in commit_range:
            raise ValueError(f"commit_range must contain '..', got {commit_range!r}")
        anchor_kind, anchor_value = "commit_range", commit_range
        changed_files = _git_diff_name_only_range(commit_range, project_root)
        boundary = commit_range.split("..")[0]
    else:
        anchor_kind, anchor_value = "file", ",".join(files)
        changed_files = list(files)
        use_ancestry = False

    check_results = {r.run_id: r for r in check_runs(Path(catalog_dir), project_root)}
    all_runs = list_runs(Path(catalog_dir))

    affected: list[BlastRadiusMatch] = []
    unverifiable: list[BlastRadiusMatch] = []
    unaffected_run_ids: list[str] = []

    for run in all_runs:
        matched_files = _run_touches_files(run, changed_files)
        if not matched_files:
            unaffected_run_ids.append(run.id)
            continue

        status = check_results[run.id].status if run.id in check_results else "OK"

        if status in ("DIRTY_RUN", "UNKNOWN_CODE"):
            unverifiable.append(
                BlastRadiusMatch(
                    run_id=run.id,
                    git_hash=run.git_hash,
                    command=run.command,
                    matched_files=matched_files,
                    reason=(
                        f"touches {matched_files} but git status is {status} -- "
                        "cannot verify which code actually ran"
                    ),
                )
            )
            continue

        if not use_ancestry:
            affected.append(
                BlastRadiusMatch(
                    run_id=run.id,
                    git_hash=run.git_hash,
                    command=run.command,
                    matched_files=matched_files,
                    reason=(
                        f"touches {matched_files} (file anchor -- no commit "
                        "ancestry check applied)"
                    ),
                )
            )
            continue

        if _is_ancestor(run.git_hash, boundary, project_root):
            affected.append(
                BlastRadiusMatch(
                    run_id=run.id,
                    git_hash=run.git_hash,
                    command=run.command,
                    matched_files=matched_files,
                    reason=(
                        f"touches {matched_files}; git_hash {run.git_hash[:9]} "
                        f"predates fix boundary {boundary}"
                    ),
                )
            )
        else:
            unaffected_run_ids.append(run.id)

    return BlastRadiusReport(
        anchor_kind=anchor_kind,
        anchor_value=anchor_value,
        changed_files=changed_files,
        affected=affected,
        unverifiable=unverifiable,
        unaffected_run_ids=unaffected_run_ids,
    )
```

Note: `Run`/`list_runs`/`check_runs` imports go at the top of the file with the other
imports, not inline — written inline above only to show what's new vs. Task 1.

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_blast_radius_assess.py -v`
Expected: PASS (6 tests)

**Step 5: Commit**

```bash
git add src/bathos/blast_radius.py tests/test_blast_radius_assess.py
git commit -m "feat(blast-radius): add assess_blast_radius (AC-1/2/4/5, #4551)"
```

---

### Task 3: Flagging + manual clearing

**Files:**
- Modify: `src/bathos/blast_radius.py` (append flag/clear section)
- Test: `tests/test_blast_radius_flag_clear.py`

**Step 1: Write the failing tests**

```python
"""Blast-radius flag/clear tests (AC-6, AC-9, backlog #4551)."""

from __future__ import annotations

import json

import pytest

from bathos.blast_radius import (
    BlastRadiusMatch,
    BlastRadiusReport,
    clear_blast_radius_flag,
    flag_blast_radius,
    fold_blast_radius_state,
)


@pytest.fixture
def catalog_dir(tmp_path):
    cat = tmp_path / "catalog"
    cat.mkdir(parents=True)
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


class TestFlagBlastRadius:
    def test_flags_affected_runs(self, catalog_dir):
        match = BlastRadiusMatch(
            run_id="run-a", git_hash="abc123", command="src/foo.py",
            matched_files=["src/foo.py"], reason="touches src/foo.py",
        )
        records = flag_blast_radius(_report(affected=[match]), catalog_dir)

        assert len(records) == 1
        assert records[0].entity_type == "run"
        assert records[0].entity_id == "run-a"
        assert records[0].to_state == "affected"
        assert json.loads(records[0].matched_files) == ["src/foo.py"]
        assert fold_blast_radius_state(catalog_dir, "run", "run-a") == "affected"

    def test_flags_unverifiable_runs_distinctly(self, catalog_dir):
        match = BlastRadiusMatch(
            run_id="run-b", git_hash="abc123", command="src/foo.py",
            matched_files=["src/foo.py"], reason="DIRTY_RUN, cannot verify",
        )
        records = flag_blast_radius(_report(unverifiable=[match]), catalog_dir)

        assert records[0].to_state == "unverifiable"
        assert fold_blast_radius_state(catalog_dir, "run", "run-b") == "unverifiable"

    def test_records_from_state_from_prior_flag(self, catalog_dir):
        match = BlastRadiusMatch(
            run_id="run-c", git_hash="abc", command="c", matched_files=["c"], reason="r",
        )
        flag_blast_radius(_report(unverifiable=[match]), catalog_dir)
        records = flag_blast_radius(_report(affected=[match]), catalog_dir)

        assert records[0].from_state == "unverifiable"
        assert records[0].to_state == "affected"


class TestClearBlastRadiusFlag:
    def test_clear_requires_nonempty_reason(self, catalog_dir):
        with pytest.raises(ValueError):
            clear_blast_radius_flag(catalog_dir, "run", "run-d", reason="")
        with pytest.raises(ValueError):
            clear_blast_radius_flag(catalog_dir, "run", "run-d", reason="   ")

    def test_clear_transitions_to_cleared(self, catalog_dir):
        match = BlastRadiusMatch(
            run_id="run-e", git_hash="abc", command="c", matched_files=["c"], reason="r",
        )
        flag_blast_radius(_report(affected=[match]), catalog_dir)

        record = clear_blast_radius_flag(
            catalog_dir, "run", "run-e", reason="re-ran post-fix, identical output"
        )

        assert record.to_state == "cleared"
        assert record.from_state == "affected"
        assert fold_blast_radius_state(catalog_dir, "run", "run-e") == "cleared"

    def test_clear_on_never_flagged_entity_still_works(self, catalog_dir):
        """Clearing something that was never flagged is a legal no-op-ish action --
        from_state is 'clean', to_state becomes 'cleared'. Not an error: the caller
        may be pre-emptively documenting that they checked."""
        record = clear_blast_radius_flag(
            catalog_dir, "run", "run-f", reason="manually verified, was never affected"
        )
        assert record.from_state == "clean"
        assert record.to_state == "cleared"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_blast_radius_flag_clear.py -v`
Expected: FAIL with `ImportError: cannot import name 'flag_blast_radius'`

**Step 3: Append flag/clear logic to `src/bathos/blast_radius.py`**

```python
import json  # add to top-of-file imports


def flag_blast_radius(
    report: BlastRadiusReport, catalog_dir: Path | str
) -> list[BlastRadiusRecord]:
    """Durably record every affected/unverifiable match in `report` (AC-6).

    Pure write step -- callers MUST render/print `report` to the user BEFORE
    calling this (AC-11: report-then-flag ordering). This function does no
    printing itself; see `bth blast-radius assess`'s CLI command for the ordering.
    """
    records: list[BlastRadiusRecord] = []
    for match, to_state in [
        *((m, "affected") for m in report.affected),
        *((m, "unverifiable") for m in report.unverifiable),
    ]:
        record = BlastRadiusRecord(
            entity_type="run",
            entity_id=match.run_id,
            to_state=to_state,
            from_state=fold_blast_radius_state(catalog_dir, "run", match.run_id),
            anchor_kind=report.anchor_kind,
            anchor_value=report.anchor_value,
            matched_files=json.dumps(match.matched_files),
            match_reason=match.reason,
        )
        records.append(append_ledger_record(record, catalog_dir))
    return records


def clear_blast_radius_flag(
    catalog_dir: Path | str, entity_type: str, entity_id: str, *, reason: str
) -> BlastRadiusRecord:
    """Manually clear a blast-radius flag (AC-9).

    Deliberate Phase-1 scope cut (see module docstring / plan doc "scope note"):
    this requires only a non-empty `reason` string, NOT a validated PASS
    attestation like bathos.trust_ledger.graduate_product's ratchet. "Manual
    re-attestation" here means a human explicitly invoked this action and wrote
    down why -- it does not verify the claim.
    """
    if not reason or not reason.strip():
        raise ValueError(
            "clear_blast_radius_flag requires a non-empty reason (manual "
            "re-attestation, AC-9) -- clearing without a recorded justification "
            "defeats the audit trail this ledger exists to provide"
        )
    current = fold_blast_radius_state(catalog_dir, entity_type, entity_id)
    record = BlastRadiusRecord(
        entity_type=entity_type,
        entity_id=entity_id,
        to_state="cleared",
        from_state=current,
        reason=reason,
    )
    return append_ledger_record(record, catalog_dir)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_blast_radius_flag_clear.py -v`
Expected: PASS (6 tests)

**Step 5: Commit**

```bash
git add src/bathos/blast_radius.py tests/test_blast_radius_flag_clear.py
git commit -m "feat(blast-radius): add flag_blast_radius + clear_blast_radius_flag (AC-6/9, #4551)"
```

---

### Task 4: compact.py wiring (durability across force_rebuild)

**Files:**
- Modify: `src/bathos/compact.py:533-580` (add `_ingest_blast_radius_fragments`, mirroring
  the existing `_ingest_ledger_fragments` immediately above it)
- Modify: `src/bathos/compact.py:855-863` (add the call site, mirroring the existing
  `_ingest_ledger_fragments(con, catalog_dir)` call)

**Step 1: Run the Task-1 durability test to confirm it currently fails**

Run: `uv run pytest tests/test_blast_radius_ledger.py::TestSurvivesCompactForceRebuild -v`
Expected: FAIL — `fold_blast_radius_state` returns `"clean"` after `force_rebuild=True`
because nothing re-ingests the cool-tier fragments into the fresh `bathos.db`.

**Step 2: Add `_ingest_blast_radius_fragments`**

Insert immediately after `_ingest_ledger_fragments` (ends at compact.py:579, before
`_ingest_archived_item_fragments` at line 582):

```python
def _ingest_blast_radius_fragments(con: duckdb.DuckDBPyConnection, catalog_dir: Path) -> int:
    """Re-derive the warm `blast_radius_ledger` table from cool-tier fragments.

    Backlog #4551. Mirrors :func:`_ingest_ledger_fragments` exactly, adapted for
    the blast-radius ledger's composite (entity_type, entity_id) key instead of
    content_hash -- same append-only, skip-if-present-by-own-id semantics.

    No-op if `<catalog_dir>/blast_radius/` does not exist. Returns the number of
    fragment records ingested (post skip-if-present).
    """
    from bathos.blast_radius import _LEDGER_TABLE_SCHEMA as _BLAST_RADIUS_TABLE_SCHEMA
    from bathos.blast_radius import read_ledger_fragments as read_blast_radius_fragments

    records = read_blast_radius_fragments(catalog_dir)
    if not records:
        return 0

    con.execute(_BLAST_RADIUS_TABLE_SCHEMA)
    ingested = 0
    for record in records:
        existing = con.execute(
            "SELECT id FROM blast_radius_ledger WHERE id = ?", [record.id]
        ).fetchone()
        if existing:
            continue
        con.execute(
            "INSERT INTO blast_radius_ledger "
            "(id, entity_type, entity_id, from_state, to_state, anchor_kind, anchor_value, "
            "matched_files, match_reason, reason, amended_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                record.id,
                record.entity_type,
                record.entity_id,
                record.from_state,
                record.to_state,
                record.anchor_kind,
                record.anchor_value,
                record.matched_files,
                record.match_reason,
                record.reason,
                record.amended_at,
            ],
        )
        ingested += 1
    return ingested
```

**Step 3: Add the call site**

Change compact.py:855-858 from:

```python
        # S3 (backlog #3491): re-derive trust_ledger from cool-tier ledger fragments,
        # same pattern as anchors above. Unconditional, cheap no-op if no ledger/
        # fragments exist, fires on force_rebuild too.
        _ingest_ledger_fragments(con, catalog_dir)
```

to:

```python
        # S3 (backlog #3491): re-derive trust_ledger from cool-tier ledger fragments,
        # same pattern as anchors above. Unconditional, cheap no-op if no ledger/
        # fragments exist, fires on force_rebuild too.
        _ingest_ledger_fragments(con, catalog_dir)

        # Backlog #4551: re-derive blast_radius_ledger from cool-tier fragments,
        # same pattern as trust_ledger above. Unconditional, cheap no-op if no
        # blast_radius/ fragments exist, fires on force_rebuild too.
        _ingest_blast_radius_fragments(con, catalog_dir)
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_blast_radius_ledger.py -v`
Expected: PASS (all tests including `TestSurvivesCompactForceRebuild`)

Run the full existing compact suite to check for regressions:
`uv run pytest tests/test_compact.py -v`
Expected: PASS, no regressions.

**Step 5: Commit**

```bash
git add src/bathos/compact.py
git commit -m "feat(blast-radius): wire ledger into compact() for durability (#4551)"
```

---

### Task 5: CLI surface

**Files:**
- Modify: `src/bathos/cli.py` (add `blast_app` Typer group + 2 commands; add 1 command to
  existing `query_app`)
- Test: `tests/test_blast_radius_cli.py`

**Step 1: Write the failing tests**

```python
"""Blast-radius CLI tests (backlog #4551). Uses typer.testing.CliRunner."""

from __future__ import annotations

import json
import subprocess

import pytest
from typer.testing import CliRunner

from bathos.cli import app
from bathos.catalog import init_catalog, write_run
from bathos.schema import Run

runner = CliRunner()


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(["init"], r)
    _git(["config", "user.email", "test@example.com"], r)
    _git(["config", "user.name", "Test"], r)
    (r / "foo.py").write_text("a = 1\n")
    _git(["add", "foo.py"], r)
    _git(["commit", "-m", "initial"], r)
    return r


@pytest.fixture
def catalog_dir(tmp_path):
    cat = tmp_path / "catalog"
    init_catalog(cat)
    return cat


def _fix_commit(repo):
    (repo / "foo.py").write_text("a = 2\n")
    _git(["add", "foo.py"], repo)
    _git(["commit", "-m", "fix bug"], repo)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


class TestBlastRadiusAssessCmd:
    def test_assess_by_commit_flags_matching_run(self, repo, catalog_dir, monkeypatch):
        pre_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        run = Run(
            project_slug="p", command="foo.py", argv=["foo.py"],
            git_hash=pre_sha, git_branch="main", git_dirty=False,
        )
        write_run(run, catalog_dir)
        fix_sha = _fix_commit(repo)

        monkeypatch.chdir(repo)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
        result = runner.invoke(app, ["blast-radius", "assess", "--commit", fix_sha])

        assert result.exit_code == 0, result.output
        assert "foo.py" in result.output
        assert "Flagged 1 run" in result.output

        status = runner.invoke(app, ["query", "blast-status", "run", run.id])
        assert status.output.strip() == "affected"

    def test_assess_requires_an_anchor(self, repo, catalog_dir, monkeypatch):
        monkeypatch.chdir(repo)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
        result = runner.invoke(app, ["blast-radius", "assess"])
        assert result.exit_code != 0


class TestBlastRadiusClearCmd:
    def test_clear_requires_reason(self, catalog_dir, monkeypatch):
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
        result = runner.invoke(app, ["blast-radius", "clear", "run", "run-x"])
        assert result.exit_code != 0

    def test_clear_writes_record(self, catalog_dir, monkeypatch):
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
        result = runner.invoke(
            app, ["blast-radius", "clear", "run", "run-x", "--reason", "verified fine"]
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["to_state"] == "cleared"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_blast_radius_cli.py -v`
Expected: FAIL — `No such command 'blast-radius'`

> Note: check first whether `_catalog_dir()` in cli.py already honors a `BTH_CATALOG_DIR`
> env var (grep `src/bathos/cli.py` and `src/bathos/config.py` for `BTH_CATALOG_DIR`). If it
> does not, use whatever mechanism the existing CLI tests in `tests/test_cli_*.py` use to
> point `_catalog_dir()` at a tmp_path (e.g. `.bth.toml` in a chdir'd tmp project, or a
> monkeypatched `_catalog_dir` — check `tests/test_attestation_cli.py` or similar for the
> established pattern before inventing a new one) and adjust this test's fixtures to match.
> This is the one place in the plan where the exact mechanism depends on a convention this
> plan's author did not re-verify against cli.py's test suite — confirm before writing Step 3.

**Step 3: Add CLI commands**

Add near the other `_app = typer.Typer(...)` declarations (around cli.py:59, after
`provenance_app`):

```python
blast_app = typer.Typer(
    help="Blast-radius assessment: which runs does a bug/fix implicate? (backlog #4551)"
)
app.add_typer(blast_app, name="blast-radius")
```

Add near the other `@attestation_app.command(...)` definitions (e.g. after line 1197):

```python
@blast_app.command("assess")
def blast_radius_assess_cmd(
    commit: str | None = typer.Option(None, "--commit", help="Single fix commit SHA"),
    commit_range: str | None = typer.Option(
        None, "--commit-range", help="Commit range, e.g. abc123..def456"
    ),
    files: list[str] | None = typer.Option(
        None, "--file", help="File/symbol path anchor (repeatable, no ancestry check)"
    ),
    no_flag: bool = typer.Option(
        False, "--no-flag", help="Print the report only; do not write ledger records"
    ),
):
    """Assess which runs a bug/fix implicates, then flag them (AC-1/2/4/5/6/11)."""
    import dataclasses
    import json as json_mod

    from bathos.blast_radius import assess_blast_radius, flag_blast_radius
    from bathos.workspace import resolve_workspace

    ws_root = resolve_workspace().fs_root
    try:
        report = assess_blast_radius(
            _catalog_dir(),
            ws_root,
            commit=commit,
            commit_range=commit_range,
            files=files or None,
        )
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    typer.echo(json_mod.dumps(dataclasses.asdict(report), indent=2))

    if no_flag:
        return
    records = flag_blast_radius(report, _catalog_dir())
    typer.echo(f"\nFlagged {len(records)} run(s) in blast_radius_ledger.")


@blast_app.command("clear")
def blast_radius_clear_cmd(
    entity_type: str = typer.Argument(..., help="'run', 'campaign', or 'claim'"),
    entity_id: str = typer.Argument(..., help="Entity ID to clear"),
    reason: str = typer.Option(
        ..., "--reason", help="Required justification (manual re-attestation, AC-9)"
    ),
):
    """Manually clear a blast-radius flag. Does not verify the reason -- records it
    for audit (see clear_blast_radius_flag's docstring for the deliberate scope cut
    vs. an attestation-gated ratchet)."""
    import dataclasses
    import json as json_mod

    from bathos.blast_radius import clear_blast_radius_flag

    try:
        record = clear_blast_radius_flag(_catalog_dir(), entity_type, entity_id, reason=reason)
    except ValueError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
    typer.echo(json_mod.dumps(dataclasses.asdict(record), indent=2))
```

Add near the other `@query_app.command(...)` definitions (e.g. after `query_candidates`,
line 1476):

```python
@query_app.command("blast-status")
def query_blast_status(
    entity_type: str = typer.Argument(..., help="'run', 'campaign', or 'claim'"),
    entity_id: str = typer.Argument(..., help="Entity ID to look up"),
):
    """Look up blast-radius status for an entity: clean/affected/unverifiable/cleared."""
    from bathos.blast_radius import fold_blast_radius_state

    typer.echo(fold_blast_radius_state(_catalog_dir(), entity_type, entity_id))
```

**Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_blast_radius_cli.py -v`
Expected: PASS (4 tests)

**Step 5: Commit**

```bash
git add src/bathos/cli.py tests/test_blast_radius_cli.py
git commit -m "feat(blast-radius): add bth blast-radius assess/clear + query blast-status CLI (#4551)"
```

---

### Task 6: MCP tool surface

**Files:**
- Modify: `src/bathos/mcp.py` (add 3 plain tool functions near the other readback-adapter
  functions around line 380-480; add 3 `@cisternal.tool` wrappers near the other
  `mcp_*_tool` wrappers around line 1620-1856)
- Test: `tests/test_blast_radius_mcp.py`

**Step 1: Write the failing tests**

```python
"""Blast-radius MCP tool tests (backlog #4551). Calls the plain (pre-decorator)
functions directly -- same convention as tests/test_trust_ledger_mcp.py, which
tests graduate_product_tool()/resolve_pin_tool() etc. directly rather than going
through the async @cisternal.tool wrapper (see that file for the established
pattern; confirm it before writing this file, since the exact import path for
`_get_catalog_dir` and any auth/token helpers needs to match)."""

from __future__ import annotations

import subprocess

import pytest

from bathos.catalog import init_catalog, write_run
from bathos.schema import Run


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(["init"], r)
    _git(["config", "user.email", "test@example.com"], r)
    _git(["config", "user.name", "Test"], r)
    (r / "foo.py").write_text("a = 1\n")
    _git(["add", "foo.py"], r)
    _git(["commit", "-m", "initial"], r)
    return r


@pytest.fixture
def catalog_dir(tmp_path):
    cat = tmp_path / "catalog"
    init_catalog(cat)
    return cat


def test_assess_tool_flags_matching_run(repo, catalog_dir):
    from bathos.mcp import blast_radius_assess_tool

    pre_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    run = Run(
        project_slug="p", command="foo.py", argv=["foo.py"],
        git_hash=pre_sha, git_branch="main", git_dirty=False,
    )
    write_run(run, catalog_dir)

    (repo / "foo.py").write_text("a = 2\n")
    _git(["add", "foo.py"], repo)
    _git(["commit", "-m", "fix"], repo)
    fix_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    result = blast_radius_assess_tool(
        catalog_dir=str(catalog_dir), project_root=str(repo), commit=fix_sha
    )

    assert "error" not in result
    assert result["flagged_count"] == 1
    assert len(result["affected"]) == 1


def test_assess_tool_requires_exactly_one_anchor(catalog_dir, repo):
    from bathos.mcp import blast_radius_assess_tool

    result = blast_radius_assess_tool(catalog_dir=str(catalog_dir), project_root=str(repo))
    assert "error" in result


def test_clear_tool_requires_reason(catalog_dir):
    from bathos.mcp import blast_radius_clear_tool

    result = blast_radius_clear_tool(
        catalog_dir=str(catalog_dir), entity_type="run", entity_id="run-x", reason=""
    )
    assert "error" in result


def test_status_tool_returns_clean_by_default(catalog_dir):
    from bathos.mcp import get_blast_radius_status_tool

    result = get_blast_radius_status_tool(
        catalog_dir=str(catalog_dir), entity_type="run", entity_id="never-flagged"
    )
    assert result["status"] == "clean"
```

**Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_blast_radius_mcp.py -v`
Expected: FAIL — `ImportError: cannot import name 'blast_radius_assess_tool'`

**Step 3: Add plain tool functions**

Add near the other read-back tool functions (after `list_candidates_tool`, before the
"anchor" section — check exact line by re-grepping `def list_candidates_tool` since Task 5
may have shifted line numbers):

```python
def blast_radius_assess_tool(
    catalog_dir: str = "",
    project_root: str = "",
    commit: str = "",
    commit_range: str = "",
    files: str = "",
    flag: bool = True,
) -> dict:
    """Assess which runs a bug/fix implicates (backlog #4551; real).

    Args:
        catalog_dir: Catalog directory (empty = use default).
        project_root: Git repo to diff/query ancestry against (empty = cwd).
        commit: Single fix commit SHA anchor.
        commit_range: "<base>..<tip>" range anchor.
        files: Comma-separated file/symbol path anchor (no ancestry check).
        flag: If True (default), also durably record affected/unverifiable runs
            in the blast_radius_ledger (AC-6). Exactly one of commit/commit_range/
            files must be non-empty.

    Returns:
        Dict with anchor_kind/anchor_value/changed_files/affected/unverifiable/
        unaffected_run_ids (report), plus flagged_count if flag=True. Or
        {"error": ...} if the anchor arguments are malformed.
    """
    import dataclasses

    from bathos.blast_radius import assess_blast_radius, flag_blast_radius

    n = sum(bool(x) for x in (commit, commit_range, files))
    if n != 1:
        return {"error": "exactly one of commit, commit_range, or files is required"}

    cat_dir = _get_catalog_dir(catalog_dir or None)
    proj_root = Path(project_root) if project_root else Path.cwd()
    file_list = [f.strip() for f in files.split(",") if f.strip()] or None

    try:
        report = assess_blast_radius(
            cat_dir,
            proj_root,
            commit=commit or None,
            commit_range=commit_range or None,
            files=file_list,
        )
    except ValueError as e:
        return {"error": str(e)}

    result = dataclasses.asdict(report)
    if flag:
        records = flag_blast_radius(report, cat_dir)
        result["flagged_count"] = len(records)
    return result


def blast_radius_clear_tool(
    catalog_dir: str = "",
    entity_type: str = "",
    entity_id: str = "",
    reason: str = "",
) -> dict:
    """Manually clear a blast-radius flag (AC-9; real).

    Requires token= matching the local ~/.bth/mcp_token (debt #619) at the MCP
    wrapper layer — see mcp_blast_radius_clear_tool.

    Args:
        catalog_dir: Catalog directory (empty = use default).
        entity_type: 'run', 'campaign', or 'claim'.
        entity_id: Entity ID to clear.
        reason: Required non-empty justification.

    Returns:
        Dict with the appended ledger record, or {"error": ...} if reason is empty.
    """
    import dataclasses

    from bathos.blast_radius import clear_blast_radius_flag

    if not entity_type or not entity_id:
        return {"error": "entity_type and entity_id are required"}
    cat_dir = _get_catalog_dir(catalog_dir or None)
    try:
        record = clear_blast_radius_flag(cat_dir, entity_type, entity_id, reason=reason)
    except ValueError as e:
        return {"error": str(e)}
    return dataclasses.asdict(record)


def get_blast_radius_status_tool(
    catalog_dir: str = "",
    entity_type: str = "",
    entity_id: str = "",
) -> dict:
    """Look up blast-radius status for an entity (real).

    Args:
        catalog_dir: Catalog directory (empty = use default).
        entity_type: 'run', 'campaign', or 'claim'.
        entity_id: Entity ID to look up.

    Returns:
        Dict with entity_type, entity_id, and status
        (clean/affected/unverifiable/cleared).
    """
    from bathos.blast_radius import fold_blast_radius_state

    if not entity_type or not entity_id:
        return {"error": "entity_type and entity_id are required"}
    cat_dir = _get_catalog_dir(catalog_dir or None)
    return {
        "entity_type": entity_type,
        "entity_id": entity_id,
        "status": fold_blast_radius_state(cat_dir, entity_type, entity_id),
    }
```

**Step 4: Add `@cisternal.tool` wrappers**

Add near the other wrappers (after `mcp_graduate_product_tool`, before `mcp_compact` —
re-grep `@cisternal.tool(registry="bathos", name="graduate_product")` for the exact
insertion point, since Task 5 may have shifted line numbers):

```python
@cisternal.tool(registry="bathos", name="blast_radius_assess")
@traced_tool
async def mcp_blast_radius_assess_tool(
    catalog_dir: str = "",
    project_root: str = "",
    commit: str = "",
    commit_range: str = "",
    files: str = "",
    flag: bool = True,
) -> dict:
    """Assess which runs a bug/fix implicates, optionally flagging them (backlog #4551)."""
    return blast_radius_assess_tool(
        catalog_dir=catalog_dir,
        project_root=project_root,
        commit=commit,
        commit_range=commit_range,
        files=files,
        flag=flag,
    )


@cisternal.tool(registry="bathos", name="blast_radius_clear")
@traced_tool
@require_write_token
async def mcp_blast_radius_clear_tool(
    catalog_dir: str = "",
    entity_type: str = "",
    entity_id: str = "",
    reason: str = "",
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token, not the tool body
) -> dict:
    """Manually clear a blast-radius flag (backlog #4551, AC-9).

    Requires token= matching the local ~/.bth/mcp_token (debt #619)."""
    return blast_radius_clear_tool(
        catalog_dir=catalog_dir, entity_type=entity_type, entity_id=entity_id, reason=reason
    )


@cisternal.tool(registry="bathos", name="get_blast_radius_status")
@traced_tool
async def mcp_get_blast_radius_status_tool(
    catalog_dir: str = "",
    entity_type: str = "",
    entity_id: str = "",
) -> dict:
    """Look up blast-radius status for an entity (backlog #4551)."""
    return get_blast_radius_status_tool(
        catalog_dir=catalog_dir, entity_type=entity_type, entity_id=entity_id
    )
```

**Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_blast_radius_mcp.py -v`
Expected: PASS (4 tests)

**Step 6: Commit**

```bash
git add src/bathos/mcp.py tests/test_blast_radius_mcp.py
git commit -m "feat(blast-radius): add MCP tool surface (assess/clear/status, #4551)"
```

---

### Task 7: CHANGELOG + full regression pass

**Files:**
- Modify: `CHANGELOG.md` (add to `## [Unreleased]` → `### Added`)

**Step 1: Add CHANGELOG entry**

Insert as a new bullet under `## [Unreleased]` → `### Added` (top of file):

```markdown
- **Blast-radius assessment (Phase 1, backlog #4551).** `bth blast-radius assess
  --commit <sha>|--commit-range <a..b>|--file <path>` answers "a bug was found/fixed here —
  which past runs does it implicate?": a new `blast_radius_ledger` (mirrors
  `trust_ledger`'s durable dual-write shape, composite-keyed on `(entity_type, entity_id)`
  for Phase 2 forward-compat) records "affected"/"unverifiable" runs via a v1 file-path
  heuristic + git ancestry check, reusing `check_runs()`'s DIRTY_RUN/UNKNOWN_CODE
  classification for the unverifiable bucket. `bth blast-radius clear` manually clears a
  flag (requires a reason, not attestation-gated in Phase 1). `bth query blast-status`
  and matching MCP tools (`blast_radius_assess`, `blast_radius_clear`,
  `get_blast_radius_status`) round out the surface. Campaign/claim-level propagation,
  the dependency-version anchor, and both shadow-mode subsystems are Phase 2
  (backlog #4552). Spec: `.praxia/docs/specs/260826_blast-radius-assessment-skill.md`.
```

**Step 2: Run the full test suite**

Run: `uv run pytest tests/ -x -q -k "blast_radius"`
Expected: PASS, all ~19 new tests.

Then run a broader regression check scoped to the modules this plan touched (do NOT run
the whole suite per the local-compute-limits rule — narrow to affected modules):

Run: `uv run pytest tests/test_compact.py tests/test_cli_*.py -q` (adjust glob to match
whatever the actual CLI test file naming convention turns out to be, discovered in Task 5)

Expected: PASS, no regressions from the compact.py/cli.py/mcp.py edits.

**Step 3: Commit**

```bash
git add CHANGELOG.md
git commit -m "docs: add blast-radius Phase 1 CHANGELOG entry (#4551)"
```

**Step 4: Push**

```bash
git push
```

(This branch — `wt-20260826-185859` — is already tracked on `origin` from the earlier
spec-only commit; this push adds the implementation on top. Open/update the PR once pushed.)
