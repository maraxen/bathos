# Blast-Radius Assessment (Phase 2a) Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Extend `bathos.blast_radius` (Phase 1, backlog #4551, merged in PR #54) with
campaign/claim-level propagation (AC-7, AC-8), the dependency-version anchor (AC-3), the
auto-clear shadow heuristic (AC-10), and a scoped slice of AC-12 (informational, non-gating
blast-radius status in `campaign show` / `claim validate` output). Backlog #4552 (narrowed
260826 — the git-hook shadow trigger split out to #4555 since it needs its own OS-integration
design pass).

**Architecture:** Additive on the SAME `blast_radius_ledger` table from Phase 1 (two new
nullable columns: `matched_clauses`, `shadow_verdict`) rather than a new table — Phase 1's
composite `(entity_type, entity_id)` key already supports `entity_type="campaign"` and
`"claim"` (proven by Phase 1's own `test_composite_key_does_not_cross_entity_types`). Campaign
propagation groups Phase-1 run-level matches by `run.campaign_id`; claim propagation reuses
`bathos.claim.load_registered_claim` + the exact covering-run matching logic
`bathos.claim.run_union_gate` already uses (`claim_discriminates` JSON array vs.
`hypothesis_ids`), scoped to the affected run-id set instead of "any campaign member." The
dependency anchor is a 4th, non-file-based anchor type: instead of `_run_touches_files`, it
calls `bathos.checker.check_dependency_lock_drift` per run, with runs missing a recorded
`dependency_lock_sha256` routed to "unverifiable" (NOT silently "not drifted" — deliberately
NOT reusing `check_dependency_lock_drift`'s own fail-open default for that case, since silent
false-negatives are exactly the failure class Phase 1's review already caught and fixed once).
The auto-clear shadow heuristic re-runs a flagged run's producing script is NOT attempted
(there is no generic "re-run" capability in bathos) — instead it computes a cheap, real proxy
signal (does the flagged run's output content_hash still match what's recorded — i.e. AC-20
SHA-drift is clean) and logs that as `shadow_verdict`, never applying it.

**Tech Stack:** Same as Phase 1 — Python 3.13, DuckDB, PyArrow, Typer CLI, `cisternal.tool` MCP.

**Spec:** `.praxia/docs/specs/260826_blast-radius-assessment-skill.md`. Prior work:
`src/bathos/blast_radius.py` (Phase 1), `src/bathos/checker.py` (`check_dependency_lock_drift`,
`check_output_sha_drift`), `src/bathos/claim.py` (`load_registered_claim`, `run_union_gate`),
`src/bathos/campaigns.py` (`connect_catalog_db`, `review_campaign`, `campaign_runs` table).

---

### Task 1: Ledger schema additions (matched_clauses, shadow_verdict)

**Files:**
- Modify: `src/bathos/blast_radius.py` (`_LEDGER_TABLE_SCHEMA`, `_LEDGER_FRAGMENT_SCHEMA`,
  `BlastRadiusRecord`, `write_ledger_fragment`, `read_ledger_fragments`,
  `_insert_warm_row`, `latest_ledger_record` — every place that lists all columns)
- Modify: `src/bathos/compact.py` (`_ingest_blast_radius_fragments`'s INSERT column list;
  add an `ALTER TABLE blast_radius_ledger ADD COLUMN IF NOT EXISTS ...` pair to the existing
  ALTER-TABLE block, since PR #54 may already be merged with the old 2-column-short schema
  by the time this runs — additive migration, not a fresh CREATE)
- Test: extend `tests/test_blast_radius_ledger.py`

**Step 1: Write the failing test**

```python
class TestNewPhase2Columns:
    def test_matched_clauses_and_shadow_verdict_round_trip(self, catalog_dir):
        record = BlastRadiusRecord(
            entity_type="claim",
            entity_id="camp-001",
            to_state="affected",
            matched_clauses='["clause-a", "clause-b"]',
            shadow_verdict='{"kind": "output_sha_still_matches", "verdict": "clean"}',
        )
        append_ledger_record(record, catalog_dir)

        latest = latest_ledger_record(catalog_dir, "claim", "camp-001")
        assert latest is not None
        assert latest.matched_clauses == '["clause-a", "clause-b"]'
        assert latest.shadow_verdict == '{"kind": "output_sha_still_matches", "verdict": "clean"}'

    def test_existing_warm_table_migrates_via_compact(self, catalog_dir):
        """A blast_radius_ledger table created by Phase-1-era code (no
        matched_clauses/shadow_verdict columns) must gain them via compact(),
        the same ALTER TABLE ADD COLUMN IF NOT EXISTS pattern used elsewhere in
        compact.py for campaigns/etc."""
        import duckdb

        (catalog_dir / "bathos.db").parent.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(catalog_dir / "bathos.db"))
        con.execute("""
            CREATE TABLE blast_radius_ledger (
                id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
                from_state TEXT, to_state TEXT NOT NULL, anchor_kind TEXT, anchor_value TEXT,
                matched_files TEXT, match_reason TEXT, reason TEXT, amended_at TEXT NOT NULL
            )
        """)
        con.close()

        from bathos.compact import compact as compact_catalog

        compact_catalog(catalog_dir)  # must not raise on the pre-existing short-column table

        append_ledger_record(
            BlastRadiusRecord(
                entity_type="claim", entity_id="camp-002", to_state="affected",
                matched_clauses='["x"]',
            ),
            catalog_dir,
        )
        assert fold_blast_radius_state(catalog_dir, "claim", "camp-002") == "affected"
```

**Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_blast_radius_ledger.py::TestNewPhase2Columns -v`
Expected: FAIL — `TypeError: BlastRadiusRecord.__init__() got an unexpected keyword argument
'matched_clauses'`

**Step 3: Add the two columns everywhere in blast_radius.py**

Add `matched_clauses: str | None = None` and `shadow_verdict: str | None = None` fields to
`BlastRadiusRecord` (after `matched_files`, before `reason`). Add matching columns to
`_LEDGER_TABLE_SCHEMA` (`matched_clauses TEXT, shadow_verdict TEXT,` after `matched_files
TEXT,`) and `_LEDGER_FRAGMENT_SCHEMA` (`pa.field("matched_clauses", pa.string()),
pa.field("shadow_verdict", pa.string()),` in the same position). Update the INSERT column
list + values tuple in `_insert_warm_row`, the SELECT column list + positional unpacking in
`latest_ledger_record`, the dict literal in `write_ledger_fragment`'s `pa.table(...)` call,
and the `BlastRadiusRecord(...)` reconstruction in `read_ledger_fragments`. Keep column order
consistent across all five places (id, entity_type, entity_id, from_state, to_state,
anchor_kind, anchor_value, matched_files, matched_clauses, shadow_verdict, match_reason,
reason, amended_at).

**Step 4: Add the ALTER TABLE migration in compact.py**

In `_ingest_blast_radius_fragments` (compact.py), before the INSERT loop, add:

```python
    con.execute(_BLAST_RADIUS_TABLE_SCHEMA)
    for _alter_sql in [
        "ALTER TABLE blast_radius_ledger ADD COLUMN IF NOT EXISTS matched_clauses TEXT",
        "ALTER TABLE blast_radius_ledger ADD COLUMN IF NOT EXISTS shadow_verdict TEXT",
    ]:
        with contextlib.suppress(Exception):
            con.execute(_alter_sql)
```

(mirrors the exact `with contextlib.suppress(Exception): con.execute(_alter_sql)` pattern
already used for the `campaigns`/`campaign_runs` ALTER TABLE block in compact.py — `import
contextlib` is already present at the top of compact.py). Update the INSERT statement's
column list and values tuple to include the two new columns.

**Step 5: Run to verify it passes**

Run: `uv run pytest tests/test_blast_radius_ledger.py -v`
Expected: PASS (all tests, old and new)

**Step 6: Commit**

```bash
git add src/bathos/blast_radius.py src/bathos/compact.py tests/test_blast_radius_ledger.py
git commit -m "feat(blast-radius): add matched_clauses/shadow_verdict columns (Phase 2a, #4552)"
```

---

### Task 2: Dependency-version anchor (AC-3)

**Files:**
- Modify: `src/bathos/blast_radius.py` (`assess_blast_radius` signature + dispatch,
  `BlastRadiusMatch` — add `campaign_id` field here too, needed by Task 3)
- Test: extend `tests/test_blast_radius_assess.py`

**Step 1: Write the failing tests**

```python
class TestDependencyAnchor:
    def test_run_with_drifted_lock_is_affected(self, repo, catalog_dir):
        (repo / "uv.lock").write_text("old-lock-content\n")
        old_hash = hash_dependency_lock(repo)
        run = _run(
            catalog_dir, command="scripts/experiments/foo.py",
            argv=["scripts/experiments/foo.py"], git_hash="abc123",
        )
        # Simulate: this run recorded the OLD lock hash, but the write_run helper
        # doesn't set dependency_lock_sha256 -- patch it directly via the Run object.
        from bathos.schema import Run
        from bathos.catalog import write_run
        run2 = Run(
            project_slug="proj", command="scripts/experiments/foo.py",
            argv=["scripts/experiments/foo.py"], git_hash="def456", git_branch="main",
            git_dirty=False, dependency_lock_sha256=old_hash,
        )
        write_run(run2, catalog_dir)

        (repo / "uv.lock").write_text("new-lock-content\n")  # lock changes

        report = assess_blast_radius(catalog_dir, repo, dependency=True)

        assert report.anchor_kind == "dependency"
        affected_ids = [m.run_id for m in report.affected]
        assert run2.id in affected_ids

    def test_run_with_no_recorded_lock_hash_is_unverifiable(self, repo, catalog_dir):
        (repo / "uv.lock").write_text("content\n")
        run = _run(
            catalog_dir, command="scripts/experiments/foo.py",
            argv=["scripts/experiments/foo.py"], git_hash="abc123",
        )  # dependency_lock_sha256 defaults to None/unset

        report = assess_blast_radius(catalog_dir, repo, dependency=True)

        assert run.id not in [m.run_id for m in report.affected]
        assert run.id in [m.run_id for m in report.unverifiable]

    def test_run_with_matching_lock_hash_is_unaffected(self, repo, catalog_dir):
        (repo / "uv.lock").write_text("stable-content\n")
        current_hash = hash_dependency_lock(repo)
        from bathos.schema import Run
        from bathos.catalog import write_run
        run = Run(
            project_slug="proj", command="scripts/experiments/foo.py",
            argv=["scripts/experiments/foo.py"], git_hash="abc123", git_branch="main",
            git_dirty=False, dependency_lock_sha256=current_hash,
        )
        write_run(run, catalog_dir)

        report = assess_blast_radius(catalog_dir, repo, dependency=True)

        assert run.id in report.unaffected_run_ids
```

Add `from bathos.checker import hash_dependency_lock` to the test file's imports.

**Step 2: Run to verify failure**

Run: `uv run pytest tests/test_blast_radius_assess.py::TestDependencyAnchor -v`
Expected: FAIL — `TypeError: assess_blast_radius() got an unexpected keyword argument
'dependency'`

**Step 3: Implement**

In `assess_blast_radius`'s signature, add `dependency: bool = False`. Update the anchor-count
validation to `sum(x is not None for x in (commit, commit_range, files)) + int(dependency)`.
Add a 4th branch:

```python
    elif dependency:
        anchor_kind = "dependency"
        current_lock_hash = hash_dependency_lock(project_root)
        anchor_value = current_lock_hash or "no-uv.lock-present"
        changed_files = []  # not file-based; the per-run loop below special-cases this anchor
```

(`hash_dependency_lock` needs importing from `bathos.checker` at the top of blast_radius.py,
alongside the existing `check_runs` import.)

In the per-run loop, branch on `anchor_kind == "dependency"` BEFORE the
`_run_touches_files`/`matched_files` logic (dependency anchor doesn't use file matching at
all):

```python
    for run in all_runs:
        if anchor_kind == "dependency":
            if not run.dependency_lock_sha256:
                unverifiable.append(BlastRadiusMatch(
                    run_id=run.id, git_hash=run.git_hash, command=run.command,
                    campaign_id=run.campaign_id, matched_files=[],
                    reason="no recorded dependency_lock_sha256 -- predates that field or was never captured; cannot verify",
                ))
                continue
            # Deliberately NOT reusing check_dependency_lock_drift's fail-open-on-missing-hash
            # default for the missing-hash case above -- that default is right for checker.py's
            # own purpose (a freshness scan skipping what it can't check) but wrong here (a
            # missing hash silently read as "not affected" is the exact false-negative failure
            # class this module's Phase-1 review already caught and fixed once).
            if check_dependency_lock_drift(run.dependency_lock_sha256, project_root):
                affected.append(BlastRadiusMatch(
                    run_id=run.id, git_hash=run.git_hash, command=run.command,
                    campaign_id=run.campaign_id, matched_files=[],
                    reason=f"dependency_lock_sha256 {run.dependency_lock_sha256[:9]} drifted from current uv.lock",
                ))
            else:
                unaffected_run_ids.append(run.id)
            continue

        matched_files = _run_touches_files(run, changed_files)
        # ... existing file/commit logic unchanged below ...
```

Also add `campaign_id: str = ""` to `BlastRadiusMatch` (needed by Task 3), and populate it
from `run.campaign_id` at every `BlastRadiusMatch(...)` construction site in the function
(the 4 existing ones from Phase 1, plus the 2 new dependency-anchor ones above).

Import `check_dependency_lock_drift` and `hash_dependency_lock` from `bathos.checker` at the
top of blast_radius.py.

**Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_blast_radius_assess.py -v`
Expected: PASS (all tests, old and new)

**Step 5: Commit**

```bash
git add src/bathos/blast_radius.py tests/test_blast_radius_assess.py
git commit -m "feat(blast-radius): add dependency-version anchor (AC-3, Phase 2a, #4552)"
```

---

### Task 3: Campaign-level propagation (AC-7)

**Files:**
- Modify: `src/bathos/blast_radius.py` (new function `propagate_to_campaigns`)
- Test: new `tests/test_blast_radius_propagation.py`

**Step 1: Write the failing test**

```python
"""Blast-radius campaign/claim propagation tests (AC-7, AC-8, Phase 2a, #4552)."""

from __future__ import annotations

import pytest

from bathos.blast_radius import (
    BlastRadiusMatch,
    BlastRadiusReport,
    fold_blast_radius_state,
    propagate_to_campaigns,
)
from bathos.campaigns import create_campaign, add_run_to_campaign, connect_catalog_db
from bathos.catalog import init_catalog, write_run
from bathos.schema import Run


@pytest.fixture
def catalog_dir(tmp_path):
    cat = tmp_path / "catalog"
    init_catalog(cat)
    return cat


def _report(affected=(), unverifiable=()):
    return BlastRadiusReport(
        anchor_kind="commit", anchor_value="deadbeef", changed_files=["src/foo.py"],
        affected=list(affected), unverifiable=list(unverifiable), unaffected_run_ids=[],
    )


class TestCampaignPropagation:
    def test_campaign_with_affected_member_gets_flagged(self, catalog_dir):
        run = Run(
            project_slug="p", command="foo.py", argv=["foo.py"], git_hash="abc",
            git_branch="main", git_dirty=False,
        )
        write_run(run, catalog_dir)
        from bathos.compact import compact as compact_catalog
        compact_catalog(catalog_dir)
        db = connect_catalog_db(catalog_dir, read_only=False)
        campaign = create_campaign(db, "p", "camp-1", "exploration", catalog_dir=catalog_dir)
        add_run_to_campaign(db, campaign.id, run.id, catalog_dir=catalog_dir)
        db.close()

        match = BlastRadiusMatch(
            run_id=run.id, git_hash="abc", command="foo.py", campaign_id=campaign.id,
            matched_files=["src/foo.py"], reason="r",
        )
        records = propagate_to_campaigns(_report(affected=[match]), catalog_dir)

        assert len(records) == 1
        assert records[0].entity_type == "campaign"
        assert records[0].entity_id == campaign.id
        assert records[0].to_state == "affected"
        assert fold_blast_radius_state(catalog_dir, "campaign", campaign.id) == "affected"

    def test_campaign_with_only_unverifiable_members_gets_unverifiable_state(self, catalog_dir):
        match = BlastRadiusMatch(
            run_id="run-x", git_hash="abc", command="foo.py", campaign_id="camp-2",
            matched_files=["x"], reason="r",
        )
        records = propagate_to_campaigns(_report(unverifiable=[match]), catalog_dir)
        assert records[0].to_state == "unverifiable"

    def test_mixed_affected_and_unverifiable_takes_affected(self, catalog_dir):
        a = BlastRadiusMatch(
            run_id="run-a", git_hash="abc", command="a", campaign_id="camp-3",
            matched_files=["a"], reason="r",
        )
        u = BlastRadiusMatch(
            run_id="run-u", git_hash="abc", command="u", campaign_id="camp-3",
            matched_files=["u"], reason="r",
        )
        records = propagate_to_campaigns(_report(affected=[a], unverifiable=[u]), catalog_dir)
        assert len(records) == 1  # one campaign, one record
        assert records[0].to_state == "affected"  # more severe wins

    def test_matches_with_no_campaign_id_are_ignored(self, catalog_dir):
        match = BlastRadiusMatch(
            run_id="run-y", git_hash="abc", command="y", campaign_id="",
            matched_files=["y"], reason="r",
        )
        records = propagate_to_campaigns(_report(affected=[match]), catalog_dir)
        assert records == []
```

**Step 2: Run to verify failure**

Run: `uv run pytest tests/test_blast_radius_propagation.py -v`
Expected: FAIL — `ImportError: cannot import name 'propagate_to_campaigns'`

**Step 3: Implement**

```python
def propagate_to_campaigns(
    report: BlastRadiusReport, catalog_dir: Path | str
) -> list[BlastRadiusRecord]:
    """AC-7: derive and record campaign-level flags from a run-level report.

    Groups affected/unverifiable matches by `match.campaign_id` (populated by
    assess_blast_radius from each run's own campaign_id field -- reads existing
    linkage, no new tracking). A campaign gets "affected" if ANY member match is
    affected, else "unverifiable" if any member match is unverifiable (more
    severe state wins). Matches with no campaign_id (campaign_id == "") are
    skipped -- nothing to propagate to.
    """
    by_campaign: dict[str, list[tuple[BlastRadiusMatch, str]]] = {}
    for m in report.affected:
        if m.campaign_id:
            by_campaign.setdefault(m.campaign_id, []).append((m, "affected"))
    for m in report.unverifiable:
        if m.campaign_id:
            by_campaign.setdefault(m.campaign_id, []).append((m, "unverifiable"))

    records: list[BlastRadiusRecord] = []
    for campaign_id, entries in by_campaign.items():
        to_state = "affected" if any(s == "affected" for _, s in entries) else "unverifiable"
        matched_files = sorted({f for m, _ in entries for f in m.matched_files})
        record = BlastRadiusRecord(
            entity_type="campaign",
            entity_id=campaign_id,
            to_state=to_state,
            from_state=fold_blast_radius_state(catalog_dir, "campaign", campaign_id),
            anchor_kind=report.anchor_kind,
            anchor_value=report.anchor_value,
            matched_files=json.dumps(matched_files),
            match_reason=f"{len(entries)} member run(s) implicated",
        )
        records.append(append_ledger_record(record, catalog_dir))
    return records
```

**Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_blast_radius_propagation.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/bathos/blast_radius.py tests/test_blast_radius_propagation.py
git commit -m "feat(blast-radius): add propagate_to_campaigns (AC-7, Phase 2a, #4552)"
```

---

### Task 4: Claim-level propagation (AC-8)

**Files:**
- Modify: `src/bathos/blast_radius.py` (new function `propagate_to_claims`)
- Test: extend `tests/test_blast_radius_propagation.py`

**Step 1: Write the failing tests**

Set up a campaign with a registered claim (mirror `tests/test_claim.py`'s or
`tests/test_t6_parity_conclude_gate.py`'s fixture pattern for scaffolding+registering a
minimal claim.bth.toml — read one of those files first to get the exact
`scaffold_claim`/`register_claim` call shape and required TOML fields, e.g.
`kill_condition_satisfiable_by_null`, before writing this test, since claim.bth.toml's
required-field set has grown across schema versions and a hand-written inline TOML risks
being wrong).

```python
class TestClaimPropagation:
    def test_claim_gets_flagged_with_implicated_clause(self, catalog_dir, tmp_path):
        # ... scaffold campaign + claim with one union_gate clause hypothesis_ids=["h1"] ...
        # ... write a run with claim_discriminates=["h1"], add to campaign ...
        match = BlastRadiusMatch(
            run_id=run.id, git_hash="abc", command="x", campaign_id=campaign.id,
            matched_files=["x"], reason="r",
        )
        records = propagate_to_claims(_report(affected=[match]), catalog_dir, workspace_root=tmp_path)

        assert len(records) == 1
        assert records[0].entity_type == "claim"
        assert records[0].entity_id == campaign.id
        assert json.loads(records[0].matched_clauses) == ["clause-1"]

    def test_campaign_with_no_registered_claim_produces_no_claim_record(self, catalog_dir):
        # campaign exists, claim_path is NULL -- propagate_to_claims returns []
        ...

    def test_clause_not_backed_by_any_affected_run_is_not_implicated(self, catalog_dir, tmp_path):
        # two clauses, only one backed by an affected run -- only that one appears
        ...
```

**Step 2: Run to verify failure**

Run: `uv run pytest tests/test_blast_radius_propagation.py::TestClaimPropagation -v`
Expected: FAIL — `ImportError: cannot import name 'propagate_to_claims'`

**Step 3: Implement**

```python
def _clauses_backed_by_runs(db, claim, run_ids: set[str]) -> list[str]:
    """Union-gate clause IDs from `claim` backed by >=1 run in `run_ids`. Same
    covering-run matching bathos.claim.run_union_gate uses (claim_discriminates
    JSON array must contain ALL of the clause's hypothesis_ids), scoped to a
    specific run-id set instead of "any campaign member". positive_control
    clauses are skipped -- they use a differential/dependency-lock check, not
    discriminates matching, out of scope here.
    """
    implicated = []
    for clause in claim.union_gate_clauses:
        if clause.get("positive_control") is True:
            continue
        hypothesis_ids = clause.get("hypothesis_ids", [])
        clause_id = clause.get("id", "?")
        for run_id in run_ids:
            row = db.execute(
                "SELECT claim_discriminates FROM runs WHERE id = ?", [run_id]
            ).fetchone()
            if not row or not row[0]:
                continue
            try:
                disc_list = json.loads(row[0])
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(disc_list, list) and all(h in disc_list for h in hypothesis_ids):
                implicated.append(clause_id)
                break
    return implicated


def propagate_to_claims(
    report: BlastRadiusReport,
    catalog_dir: Path | str,
    *,
    workspace_root: Path | str | None = None,
) -> list[BlastRadiusRecord]:
    """AC-8: derive and record claim-level flags naming implicated union-gate
    clauses, for any campaign with a registered claim and >=1 affected run
    backing one of its clauses.

    Claim-level records key on campaign_id (entity_type="claim") -- claims are
    always accessed via their owning campaign_id in this codebase (there is no
    separate claim_id), so this is consistent with load_registered_claim's own
    resolution path. Silently skips (no record, no error) a campaign with no
    registered claim, an unreadable/moved claim file, or a SHA mismatch -- this
    is read-only propagation, not a validation gate.
    """
    from bathos.campaigns import CampaignError, connect_catalog_db
    from bathos.claim import load_registered_claim

    by_campaign: dict[str, set[str]] = {}
    for m in report.affected:
        if m.campaign_id:
            by_campaign.setdefault(m.campaign_id, set()).add(m.run_id)
    for m in report.unverifiable:
        if m.campaign_id:
            by_campaign.setdefault(m.campaign_id, set()).add(m.run_id)

    if not by_campaign:
        return []

    db = connect_catalog_db(Path(catalog_dir), read_only=True)
    if db is None:
        return []  # no warm tier at all -- claims only resolve from the warm `campaigns` table

    records: list[BlastRadiusRecord] = []
    try:
        for campaign_id, run_ids in by_campaign.items():
            try:
                claim = load_registered_claim(
                    db, campaign_id,
                    workspace_root=Path(workspace_root) if workspace_root else None,
                )
            except (CampaignError, FileNotFoundError, ValueError):
                continue
            if claim is None:
                continue
            implicated = _clauses_backed_by_runs(db, claim, run_ids)
            if not implicated:
                continue
            record = BlastRadiusRecord(
                entity_type="claim",
                entity_id=campaign_id,
                to_state="affected",
                from_state=fold_blast_radius_state(catalog_dir, "claim", campaign_id),
                anchor_kind=report.anchor_kind,
                anchor_value=report.anchor_value,
                matched_clauses=json.dumps(sorted(implicated)),
                match_reason=f"union-gate clause(s) {sorted(implicated)} backed by an affected run",
            )
            records.append(append_ledger_record(record, catalog_dir))
    finally:
        db.close()
    return records
```

**Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_blast_radius_propagation.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/bathos/blast_radius.py tests/test_blast_radius_propagation.py
git commit -m "feat(blast-radius): add propagate_to_claims (AC-8, Phase 2a, #4552)"
```

---

### Task 5: Auto-clear shadow heuristic (AC-10)

**Files:**
- Modify: `src/bathos/blast_radius.py` (new function `compute_shadow_auto_clear_verdict`)
- Test: extend `tests/test_blast_radius_flag_clear.py`

**Step 1: Write the failing test**

```python
class TestShadowAutoClear:
    def test_shadow_verdict_clean_when_output_sha_matches(self, catalog_dir):
        run = Run(
            project_slug="p", command="x", argv=["x"], git_hash="abc", git_branch="main",
            git_dirty=False, output_metadata=json.dumps([{"path": "out.json", "sha256": "a" * 64}]),
        )
        write_run(run, catalog_dir)
        # ... write an actual file at out.json with sha256 "a"*64 (or monkeypatch
        # _collect_output_metadata) ...
        verdict = compute_shadow_auto_clear_verdict(run, catalog_dir)
        assert verdict["kind"] == "output_sha_still_matches"
        assert verdict["verdict"] == "clean"

    def test_shadow_verdict_dirty_when_output_sha_drifted(self, catalog_dir, tmp_path):
        # ... output file's on-disk content no longer matches recorded sha256 ...
        verdict = compute_shadow_auto_clear_verdict(run, catalog_dir)
        assert verdict["verdict"] == "drifted"

    def test_flag_blast_radius_records_shadow_verdict_without_applying_it(self, catalog_dir):
        """The shadow verdict is computed and stored, but to_state stays
        whatever assess_blast_radius decided (affected/unverifiable) -- it is
        NEVER auto-applied as a clear (spec Decision Log #5)."""
        match = BlastRadiusMatch(
            run_id="run-z", git_hash="abc", command="x", campaign_id="",
            matched_files=["x"], reason="r",
        )
        records = flag_blast_radius(_report(affected=[match]), catalog_dir)
        assert records[0].to_state == "affected"  # NOT auto-cleared
        assert records[0].shadow_verdict is not None
```

**Step 2: Run to verify failure**

Run: `uv run pytest tests/test_blast_radius_flag_clear.py::TestShadowAutoClear -v`
Expected: FAIL — `ImportError: cannot import name 'compute_shadow_auto_clear_verdict'`

**Step 3: Implement**

```python
def compute_shadow_auto_clear_verdict(run: Run, catalog_dir: Path | str) -> dict:
    """AC-10: compute (never apply) an auto-clear signal for a flagged run.

    There is no generic "re-run this script" capability in bathos, so the
    shadow heuristic uses the cheapest REAL proxy already available: does every
    catalogued output file's on-disk content still match its recorded sha256
    (bathos.checker.check_output_sha_drift, the same AC-20 check `bth check
    --check-outputs` uses)? "clean" is weak evidence the run's product hasn't
    silently changed since flagging -- NOT evidence the flagged code issue
    doesn't apply, which is why this is logged, never applied (spec Decision
    Log #5: "best of both worlds... live test any auto-clear feature before it
    actually would be used in production").

    Returns a JSON-serializable dict: {"kind": "output_sha_still_matches",
    "verdict": "clean" | "drifted" | "no_outputs_recorded", "checked_at": iso8601}.
    """
    from bathos.checker import check_output_sha_drift

    results = check_output_sha_drift(run)
    if not results:
        verdict = "no_outputs_recorded"
    elif any(r.status in ("DRIFT", "MISSING", "UNREADABLE") for r in results):
        verdict = "drifted"
    else:
        verdict = "clean"
    return {
        "kind": "output_sha_still_matches",
        "verdict": verdict,
        "checked_at": _now_iso(),
    }
```

Wire it into `flag_blast_radius`: for each `BlastRadiusRecord` built for an "affected" match
(not "unverifiable" -- a run bathos can't even verify the git state of has no meaningful
output-drift signal either), fetch the `Run` via `bathos.query.get_run(match.run_id,
catalog_dir)` and set `shadow_verdict=json.dumps(compute_shadow_auto_clear_verdict(run,
catalog_dir))` if the run is found, else `None`. `to_state` is computed exactly as before
(Phase 1 logic unchanged) — the shadow verdict is purely an additional field on the SAME
record, never a second write path and never influencing `to_state`.

**Step 4: Run to verify it passes**

Run: `uv run pytest tests/test_blast_radius_flag_clear.py -v`
Expected: PASS

**Step 5: Commit**

```bash
git add src/bathos/blast_radius.py tests/test_blast_radius_flag_clear.py
git commit -m "feat(blast-radius): add shadow auto-clear verdict, never auto-applied (AC-10, #4552)"
```

---

### Task 6: CLI/MCP wiring

**Files:**
- Modify: `src/bathos/cli.py` (`blast_radius_assess_cmd`: add `--dependency` flag, call
  `propagate_to_campaigns`/`propagate_to_claims` after `flag_blast_radius`)
- Modify: `src/bathos/mcp.py` (`blast_radius_assess_tool`: add `dependency: bool = False`
  param, same propagation call)
- Test: extend `tests/test_blast_radius_cli.py` and `tests/test_blast_radius_mcp.py`

**Step 1: Write failing tests** (CLI: `--dependency` flag produces `anchor_kind: "dependency"`
in output and doesn't require `--commit`/`--commit-range`/`--file`; a campaign/claim record
appears in the ledger after assessing a run that belongs to a campaign with a registered
claim. MCP: same, via the plain `blast_radius_assess_tool` function.)

**Step 2: Run to verify failure.**

**Step 3: Implement.** In `blast_radius_assess_cmd`, add:
```python
    dependency: bool = typer.Option(
        False, "--dependency", help="Dependency-lock-drift anchor (no commit/file needed)"
    ),
```
pass `dependency=dependency` to `assess_blast_radius(...)`. After the existing
`flag_blast_radius(report, _catalog_dir())` call (guarded by `if no_flag: return` as today),
add:
```python
    campaign_records = propagate_to_campaigns(report, _catalog_dir())
    claim_records = propagate_to_claims(report, _catalog_dir(), workspace_root=ws_root)
    if campaign_records:
        typer.echo(f"Flagged {len(campaign_records)} campaign(s).")
    if claim_records:
        typer.echo(f"Flagged {len(claim_records)} claim(s), clauses: "
                    f"{[json.loads(r.matched_clauses) for r in claim_records]}")
```
Mirror the same in `mcp.py`'s `blast_radius_assess_tool` (add `dependency: bool = False` and
`project` already present; call both propagate functions when `flag=True`, add
`campaign_flagged_count`/`claim_flagged_count` to the returned dict) and its
`mcp_blast_radius_assess_tool` wrapper (already `@require_write_token`-gated from Phase 1 —
no new gating needed, propagation is part of the same durable-write action).

**Step 4: Run to verify it passes.**

**Step 5: Commit**

```bash
git add src/bathos/cli.py src/bathos/mcp.py tests/test_blast_radius_cli.py tests/test_blast_radius_mcp.py
git commit -m "feat(blast-radius): wire dependency anchor + campaign/claim propagation into CLI/MCP (#4552)"
```

---

### Task 7: AC-12 slice — informational status in campaign show / claim validate

**Files:**
- Modify: `src/bathos/campaigns.py` (`review_campaign`: add `blast_radius_status` key)
- Modify: `src/bathos/claim.py` (`validate_claim`: add an INFO-level line, never an error/
  warning that would fail the gate, when `entity_type="claim"` status is not "clean"/"cleared")
- Test: extend `tests/test_campaigns.py` / `tests/test_claim.py` (find the exact files first)

**Step 1: Write failing tests** asserting the new field/line appears and that it is
purely informational (existing PASS/FAIL semantics of `validate_claim` are unchanged for a
flagged claim — this must NOT become a new gate).

**Step 2: Run to verify failure.**

**Step 3: Implement.** In `review_campaign`, after resolving `campaign_id`, add:
```python
    from bathos.blast_radius import fold_blast_radius_state
    blast_radius_status = fold_blast_radius_state(
        catalog_dir or Path.cwd(), "campaign", campaign_id
    ) if catalog_dir else "clean"
```
and include `"blast_radius_status": blast_radius_status` in the returned dict. In
`validate_claim`, near wherever campaign context is already available, add an `infos.append(...)`
line (matching the existing `ValidationResult.infos` convention) when the claim's own
blast-radius status (`fold_blast_radius_state(catalog_dir, "claim", campaign_id)`) is not
"clean" — read Phase 1's spec Decision Log #3 again before writing this: **no gating**, so this
must be an `infos` entry, never appended to `errors` or `warnings` in a way that could flip
`ok=False`.

**Step 4: Run to verify it passes.**

**Step 5: Commit**

```bash
git add src/bathos/campaigns.py src/bathos/claim.py tests/...
git commit -m "feat(blast-radius): surface status in campaign show / claim validate, non-gating (AC-12 slice, #4552)"
```

---

### Task 8: CHANGELOG + full regression pass + push

**Step 1:** Add a `### Added` bullet under `## [Unreleased]` in `CHANGELOG.md` summarizing
Phase 2a (campaign/claim propagation, dependency anchor, shadow auto-clear, informational
AC-12 slice), referencing backlog #4552 and noting #4555 (git-hook trigger) as the remaining
split-out piece.

**Step 2:** Run the full blast-radius suite plus a scoped regression pass on every touched
module (`campaigns.py`, `claim.py`, `checker.py`, `compact.py`, `cli.py`, `mcp.py` and their
existing test files — same modules Phase 1's Task 7 regression-checked, plus `test_claim*.py`
and `test_campaigns*.py` since this phase touches those two modules for the first time).

**Step 3:** `uv run ruff check` on every modified file.

**Step 4:** Commit the CHANGELOG entry, then `git push` (same branch, same PR #54 — Phase 1
and Phase 2a are being kept on one branch/PR rather than a stacked PR, since #54 hasn't
merged yet and a stacked-PR workflow isn't established precedent in this repo; note this
choice in the push confirmation to the user).
