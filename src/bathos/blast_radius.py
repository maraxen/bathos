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
"clean". assess_blast_radius() writes "affected" or "unverifiable" records;
clear_blast_radius_flag() writes "cleared" records. There is no automatic clearing in
Phase 1 -- see clear_blast_radius_flag's own docstring for the deliberate scope cut
vs. trust_ledger's PASS-attestation-gated ratchet.
"""

from __future__ import annotations

import json
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from bathos.checker import check_runs
from bathos.query import list_runs
from bathos.schema import Run
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
        raise ValueError(f"git diff --name-only {a} {b} failed: {result.stderr.strip()}")
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _git_diff_name_only_range(commit_range: str, project_root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--name-only", commit_range],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(f"git diff --name-only {commit_range} failed: {result.stderr.strip()}")
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
        assert files is not None  # guaranteed by the n_anchors == 1 check above
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

        assert boundary is not None  # set whenever use_ancestry is True
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

    Deliberate Phase-1 scope cut: this requires only a non-empty `reason` string,
    NOT a validated PASS attestation like bathos.trust_ledger.graduate_product's
    ratchet. "Manual re-attestation" here means a human explicitly invoked this
    action and wrote down why -- it does not verify the claim. Wiring this to a new
    attestation kind is a natural follow-up, not attempted here (YAGNI -- nothing
    in backlog #4551's scope requires it).
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
