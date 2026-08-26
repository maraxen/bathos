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
import re
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from bathos.checker import check_dependency_lock_drift, check_runs, hash_dependency_lock
from bathos.query import get_run, list_runs
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
    matched_clauses TEXT,
    shadow_verdict TEXT,
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
        pa.field("matched_clauses", pa.string()),
        pa.field("shadow_verdict", pa.string()),
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
    matched_clauses: str | None = None  # JSON-encoded list[str], claim entity_type only (AC-8)
    shadow_verdict: str | None = None  # JSON-encoded dict, never applied (AC-10)
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
            "matched_clauses": [record.matched_clauses],
            "shadow_verdict": [record.shadow_verdict],
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
    """Read every cool-tier ledger fragment, unfolded (full history).

    Two robustness guards (code-review finding, PR #54): the glob excludes
    `*.tmp.parquet` (write_ledger_fragment's own tmp-write-then-rename target),
    which would otherwise match `blast_radius_*.parquet` and be read as a
    fragment if a write was interrupted between the tmp-write and the atomic
    rename. And a fragment that still fails to parse (e.g. corruption from a
    non-atomic-rename edge case, or disk-level corruption) is skipped with a
    telemetry event rather than raising -- one bad fragment must not crash
    bathos.compact.compact() for the entire catalog (every project's data),
    which is what an uncaught pq.read_table() exception here would do, since
    this function is called from compact()'s unconditional ingest step.
    """
    frag_dir = _ledger_fragments_dir(catalog_dir)
    if not frag_dir.exists():
        return []
    parquet_files = [
        f for f in frag_dir.glob("blast_radius_*.parquet") if not f.name.endswith(".tmp.parquet")
    ]
    if not parquet_files:
        return []

    tables = []
    for f in parquet_files:
        try:
            tables.append(pq.read_table(f))
        except Exception as exc:  # noqa: BLE001 — deliberately broad, see docstring
            event("blast_radius.corrupt_fragment", path=str(f), error=str(exc))
    if not tables:
        return []
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
                matched_clauses=pydict["matched_clauses"][i],
                shadow_verdict=pydict["shadow_verdict"][i],
                match_reason=pydict["match_reason"][i],
                reason=pydict["reason"][i],
                amended_at=pydict["amended_at"][i],
            )
        )
    return records


def _connect(catalog_dir: Path | str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(Path(catalog_dir) / "bathos.db"))
    con.execute(_LEDGER_TABLE_SCHEMA)
    # Phase 2a (#4552): CREATE TABLE IF NOT EXISTS above is a no-op against a table a
    # Phase-1-era catalog already created without matched_clauses/shadow_verdict -- this
    # ALTER must run on every direct connection (not just compact.py's ingest path, which
    # only fires when there are cool-tier fragments to re-derive: an old table with zero
    # fragments yet would otherwise never get migrated before the first Phase-2a INSERT).
    import contextlib

    for _alter_sql in (
        "ALTER TABLE blast_radius_ledger ADD COLUMN IF NOT EXISTS matched_clauses TEXT",
        "ALTER TABLE blast_radius_ledger ADD COLUMN IF NOT EXISTS shadow_verdict TEXT",
    ):
        with contextlib.suppress(Exception):
            con.execute(_alter_sql)
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
            "matched_files, matched_clauses, shadow_verdict, match_reason, reason, amended_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                record.id,
                record.entity_type,
                record.entity_id,
                record.from_state,
                record.to_state,
                record.anchor_kind,
                record.anchor_value,
                record.matched_files,
                record.matched_clauses,
                record.shadow_verdict,
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
            "anchor_value, matched_files, matched_clauses, shadow_verdict, match_reason, "
            "reason, amended_at "
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
            matched_clauses=row[8],
            shadow_verdict=row[9],
            match_reason=row[10],
            reason=row[11],
            amended_at=row[12],
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
    campaign_id: str = ""  # "" = run has no campaign; needed for AC-7/AC-8 propagation


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


def _reject_flag_like(value: str) -> None:
    """Refuse a git revision argument that looks like a flag (starts with '-').

    Argv-list subprocess calls (no shell=True, used throughout this module) are
    already immune to shell injection, but git still parses a leading '-' as a
    flag WITHIN a single argv token -- e.g. the single token "--output=/tmp/x" is
    parsed as the --output flag regardless of surrounding context. A caller-
    supplied commit/commit_range value that starts with '-' could smuggle an
    arbitrary git flag (e.g. --output=<path>, which WRITES the diff to an
    attacker-chosen path) instead of being treated as a revision. Security-audit
    finding, PR #54.
    """
    if value.startswith("-"):
        raise ValueError(
            f"refusing to pass {value!r} to git: it starts with '-', which git "
            "parses as a flag rather than a revision (flag-injection guard)"
        )


def _git_diff_name_only(a: str, b: str, project_root: Path) -> list[str]:
    _reject_flag_like(a)
    _reject_flag_like(b)
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
    _reject_flag_like(commit_range)
    base, _, tip = commit_range.partition("..")
    _reject_flag_like(base)
    _reject_flag_like(tip.lstrip("."))  # tolerate a three-dot A...B range
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
    is a value git can't look up) must never be treated as "predates the fix". A
    flag-like sha (starts with '-') also fails closed here rather than raising --
    this is a read-path classification helper, not a user-facing error site, and a
    flag-like value can never legitimately be an ancestor (security-audit finding).
    """
    if not candidate_sha or candidate_sha in ("unknown", "nogit"):
        return False
    if candidate_sha.startswith("-") or boundary_sha.startswith("-"):
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
            # Just the two `in` checks: `s.endswith(t)` implies `t in s`, so an
            # endswith clause here could never independently flip the result --
            # dropped per code-review finding (verified: exhaustive brute-force
            # check found no case where the two endswith clauses mattered).
            if changed_norm in hay_norm or hay_norm in changed_norm:
                matched.append(changed)
                break
    return matched


#: list_runs()/check_runs() require an int limit (no "unbounded" sentinel) and build
#: their SQL with no ORDER BY, so the default limit=50 silently drops an ARBITRARY
#: subset of runs on any catalog above 50 rows -- not "the 50 most recent". That is
#: exactly the multi-project, many-run scale bathos is designed for (CLAUDE.md:
#: "a single researcher across 10+ projects"), and for a tool whose entire purpose is
#: "did I miss a run this bug affects", silent truncation is a false-negative machine,
#: worse than the heuristic-noise false-positive risk the spec's pre-mortem already
#: accepted. Pass this instead of the 50-row default. Found in PR #54 review.
_UNBOUNDED_SCAN_LIMIT = 1_000_000


def assess_blast_radius(
    catalog_dir: Path | str,
    project_root: Path | str,
    *,
    commit: str | None = None,
    commit_range: str | None = None,
    files: list[str] | None = None,
    dependency: bool = False,
    project: str | None = None,
) -> BlastRadiusReport:
    """Assess which catalogued runs a bug/fix implicates (AC-1, AC-2, AC-3, AC-4, AC-5).

    Exactly one of `commit`, `commit_range`, `files`, or `dependency=True` must be
    given -- these are the four anchor types (spec Decision Log #1). This function
    is a pure read: it does not write to the ledger. Callers pass the returned
    report to flag_blast_radius() to durably record it (AC-11: report-then-flag
    ordering).

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
        dependency: If True (AC-3), assess for dependency-lock drift instead of a
            file/commit anchor. Not file-based -- reuses
            bathos.checker.check_dependency_lock_drift/hash_dependency_lock as-is
            (backlog #4552 constraint: whole-lockfile hash comparison only, no
            per-package diffing). A run with no recorded dependency_lock_sha256
            (predates that field) goes to "unverifiable", NOT "unaffected" --
            deliberately not reusing check_dependency_lock_drift's own
            fail-open-on-missing-hash default, which is right for its own
            freshness-scan purpose but would be a silent false negative here.
        project: Optional project_slug filter. bathos's catalog is shared across
            multiple projects (a run in project B whose command happens to
            substring-match a changed filename in project A would otherwise be
            pulled into the match set); pass this to scope the scan to one
            project. Default None scans the whole catalog, matching the tool's
            other defaults -- cross-project matches remain possible but are of a
            piece with the already-accepted heuristic-noise tradeoff (auditable
            via AC-13's match_reason, not silently wrong the way truncation was).

    Returns:
        A BlastRadiusReport. Does not raise for "no runs matched" (empty buckets is
        a valid, non-error result) -- only raises ValueError for a malformed anchor
        argument combination.
    """
    n_anchors = sum(x is not None for x in (commit, commit_range, files)) + int(dependency)
    if n_anchors != 1:
        raise ValueError(
            "assess_blast_radius requires exactly one of commit, commit_range, files, "
            f"or dependency=True (got {n_anchors})"
        )

    project_root = Path(project_root)
    #: None only for the file and dependency anchors (no commit boundary to check
    #: ancestry against); set for both commit and commit_range. This one variable is
    #: the sole ancestry switch -- a separate `use_ancestry` bool used to shadow it,
    #: redundant since it was always exactly `boundary is not None` (code-review
    #: finding, PR #54).
    boundary: str | None = None

    if dependency:
        anchor_kind = "dependency"
        current_lock_hash = hash_dependency_lock(project_root)
        anchor_value = current_lock_hash or "no-uv.lock-present"
        changed_files: list[str] = []  # not file-based; per-run loop special-cases this anchor
    elif commit is not None:
        anchor_kind, anchor_value = "commit", commit
        changed_files = _git_diff_name_only(f"{commit}^", commit, project_root)
        boundary = f"{commit}^"
    elif commit_range is not None:
        if ".." not in commit_range:
            raise ValueError(f"commit_range must contain '..', got {commit_range!r}")
        if "..." in commit_range:
            # git's three-dot "A...B" is symmetric-difference and does NOT guarantee
            # A is an ancestor of B -- but `commit_range.split("..")[0]` below treats
            # the base as exactly that ancestry boundary. Silently accepting a
            # three-dot range would compute changed_files correctly (git handles it
            # natively) while quietly using a boundary the range doesn't actually
            # promise, producing a wrong affected/unaffected split with no warning
            # (code-review finding, PR #54). This function only ever documented
            # two-dot "<base>..<tip>" syntax; reject the ambiguous case explicitly.
            raise ValueError(
                f"commit_range must use two-dot '<base>..<tip>' syntax, not "
                f"three-dot (symmetric-difference) syntax: got {commit_range!r}"
            )
        anchor_kind, anchor_value = "commit_range", commit_range
        changed_files = _git_diff_name_only_range(commit_range, project_root)
        boundary = commit_range.split("..")[0]
    else:
        assert files is not None  # guaranteed by the n_anchors == 1 check above
        anchor_kind, anchor_value = "file", ",".join(files)
        changed_files = list(files)

    check_results = {
        r.run_id: r
        for r in check_runs(
            Path(catalog_dir), project_root, project=project, limit=_UNBOUNDED_SCAN_LIMIT
        )
    }
    all_runs = list_runs(Path(catalog_dir), project=project, limit=_UNBOUNDED_SCAN_LIMIT)

    # Campaign membership has two independent, NOT-mutually-synced sources: Run.campaign_id
    # (set at write time by some flows, and what readback.list_candidates reads) and the
    # campaign_runs junction table (populated by bathos.campaigns.add_run_to_campaign,
    # which does NOT also update the run's own campaign_id field -- confirmed live: a run
    # added via add_run_to_campaign alone has campaign_id == ""). AC-7/AC-8 propagation
    # needs the real membership, so both are checked, run.campaign_id preferred when set.
    # Built once here, not per-run, mirroring check_results above.
    campaign_by_run: dict[str, str] = {}
    db_path = Path(catalog_dir) / "bathos.db"
    if db_path.exists():
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            rows = con.execute("SELECT run_id, campaign_id FROM campaign_runs").fetchall()
            campaign_by_run = dict(rows)
        except duckdb.Error:
            pass
        finally:
            con.close()

    affected: list[BlastRadiusMatch] = []
    unverifiable: list[BlastRadiusMatch] = []
    unaffected_run_ids: list[str] = []

    for run in all_runs:
        if anchor_kind == "dependency":
            if not run.dependency_lock_sha256:
                unverifiable.append(
                    BlastRadiusMatch(
                        run_id=run.id,
                        git_hash=run.git_hash,
                        command=run.command,
                        campaign_id=run.campaign_id or campaign_by_run.get(run.id, ""),
                        matched_files=[],
                        reason=(
                            "no recorded dependency_lock_sha256 -- predates that "
                            "field or was never captured; cannot verify"
                        ),
                    )
                )
                continue
            if check_dependency_lock_drift(run.dependency_lock_sha256, project_root):
                affected.append(
                    BlastRadiusMatch(
                        run_id=run.id,
                        git_hash=run.git_hash,
                        command=run.command,
                        campaign_id=run.campaign_id or campaign_by_run.get(run.id, ""),
                        matched_files=[],
                        reason=(
                            f"dependency_lock_sha256 {run.dependency_lock_sha256[:9]} "
                            "drifted from current uv.lock"
                        ),
                    )
                )
            else:
                unaffected_run_ids.append(run.id)
            continue

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
                    campaign_id=run.campaign_id or campaign_by_run.get(run.id, ""),
                    matched_files=matched_files,
                    reason=(
                        f"touches {matched_files} but git status is {status} -- "
                        "cannot verify which code actually ran"
                    ),
                )
            )
            continue

        if boundary is None:
            affected.append(
                BlastRadiusMatch(
                    run_id=run.id,
                    git_hash=run.git_hash,
                    command=run.command,
                    campaign_id=run.campaign_id or campaign_by_run.get(run.id, ""),
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
                    campaign_id=run.campaign_id or campaign_by_run.get(run.id, ""),
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


def compute_shadow_auto_clear_verdict(run: Run) -> dict:
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


def flag_blast_radius(
    report: BlastRadiusReport, catalog_dir: Path | str
) -> list[BlastRadiusRecord]:
    """Durably record every affected/unverifiable match in `report` (AC-6).

    Pure write step -- callers MUST render/print `report` to the user BEFORE
    calling this (AC-11: report-then-flag ordering). This function does no
    printing itself; see `bth blast-radius assess`'s CLI command for the ordering.

    For "affected" matches only (AC-10), also computes a shadow auto-clear
    verdict (compute_shadow_auto_clear_verdict) and stores it on the SAME
    record's shadow_verdict field -- purely observational, never applied:
    `to_state` is exactly what the caller decided (affected/unverifiable),
    regardless of what the shadow verdict says. "unverifiable" matches get no
    shadow verdict -- a run bathos can't even trust the git state of has no
    more meaningful an output-drift signal either.
    """
    records: list[BlastRadiusRecord] = []
    for match, to_state in [
        *((m, "affected") for m in report.affected),
        *((m, "unverifiable") for m in report.unverifiable),
    ]:
        shadow_verdict_json = None
        if to_state == "affected":
            # Best-effort: the shadow verdict is purely observational (AC-10) and must
            # never break the actual flagging write below. get_run() can raise here in
            # a real, pre-existing cross-module scenario shared with trust_ledger.py:
            # writing ANY ledger record creates bathos.db as a side effect of
            # duckdb.connect() (see this module's own _connect()), and if that happens
            # before bathos.compact.compact() has ever populated a `runs` table, a
            # later get_run() sees bathos.db exists (query.py's _resolve_backend picks
            # "warm") but finds no `runs` table there -- CatalogException, not a
            # missing-run None. Caught here (backlog debt item filed for the deeper
            # _resolve_backend assumption, not fixed in this module).
            try:
                run = get_run(match.run_id, Path(catalog_dir))
            except duckdb.Error:
                run = None
            if run is not None:
                shadow_verdict_json = json.dumps(compute_shadow_auto_clear_verdict(run))

        record = BlastRadiusRecord(
            entity_type="run",
            entity_id=match.run_id,
            to_state=to_state,
            from_state=fold_blast_radius_state(catalog_dir, "run", match.run_id),
            anchor_kind=report.anchor_kind,
            anchor_value=report.anchor_value,
            matched_files=json.dumps(match.matched_files),
            shadow_verdict=shadow_verdict_json,
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


def _clauses_backed_by_runs(db: duckdb.DuckDBPyConnection, claim, run_ids: set[str]) -> list[str]:
    """Union-gate clause IDs from `claim` backed by >=1 run in `run_ids`.

    Same covering-run matching bathos.claim.run_union_gate uses (a run's
    claim_discriminates JSON array must contain ALL of a clause's
    hypothesis_ids), scoped to a specific run-id set instead of "any campaign
    member". positive_control clauses are skipped -- they use a differential/
    dependency-lock check (bathos.claim.differential_confound_check), not
    discriminates matching, out of scope here.
    """
    implicated: list[str] = []
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
    clauses, for any campaign with a registered claim and >=1 affected/
    unverifiable run backing one of its clauses.

    Claim-level records key on campaign_id (entity_type="claim") -- claims are
    always accessed via their owning campaign_id in this codebase (there is no
    separate claim_id), consistent with bathos.claim.load_registered_claim's
    own resolution path. Silently skips (no record, no error) a campaign with
    no registered claim, an unreadable/moved claim file, or a SHA mismatch --
    this is read-only propagation, not a validation gate (spec Decision Log
    #3: no gating in Phase 1/2a).
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

    # Read phase (campaigns db held open) and write phase (blast_radius_ledger db,
    # via fold_blast_radius_state/append_ledger_record) are kept strictly separate:
    # DuckDB refuses a second connection to the SAME bathos.db file with a different
    # read_only configuration while the first is still open, and fold_blast_radius_state
    # opens its own read-write connection internally -- calling it while `db` (read-only)
    # is still open raised "Connection Error: Can't open a connection to same database
    # file with a different configuration than existing connections" (caught by this
    # module's own tests).
    to_write: list[tuple[str, list[str]]] = []
    try:
        for campaign_id, run_ids in by_campaign.items():
            try:
                claim = load_registered_claim(
                    db,
                    campaign_id,
                    workspace_root=Path(workspace_root) if workspace_root else None,
                )
            except (CampaignError, FileNotFoundError, ValueError):
                continue
            if claim is None:
                continue
            implicated = _clauses_backed_by_runs(db, claim, run_ids)
            if not implicated:
                continue
            to_write.append((campaign_id, implicated))
    finally:
        db.close()

    records: list[BlastRadiusRecord] = []
    for campaign_id, implicated in to_write:
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
    return records


_FIX_LIKE_KEYWORD_PATTERN = re.compile(
    r"\b(fix|fixes|fixed|bug|bugfix|hotfix|regression|patch)\b", re.IGNORECASE
)


def matches_fix_like_keywords(commit_message: str) -> bool:
    """Shadow-trigger keyword filter (spec Decision Log #2, backlog #4555).

    Hardcoded pattern, not configurable via .bth.toml yet -- the user's own
    call: prove the trigger before building configurability.
    """
    return bool(_FIX_LIKE_KEYWORD_PATTERN.search(commit_message))


def record_shadow_trigger(
    catalog_dir: Path | str, project_root: Path | str, commit: str
) -> BlastRadiusRecord | None:
    """SAC-6/SAC-7: run a shadow-only assessment for `commit` and log a single
    entity_type="shadow_trigger" record.

    NEVER calls flag_blast_radius or either propagate_to_* function -- this
    can never durably affect a real run/campaign/claim's state (spec Decision
    Log #7). `to_state="shadow_only"` is a 4th state value distinct from
    affected/unverifiable/cleared, and `entity_id` is the commit sha rather
    than a run/campaign id, so this is a fully separate namespace within the
    same blast_radius_ledger table (proven collision-free by Phase 1's own
    test_composite_key_does_not_cross_entity_types).

    Returns the appended record, or None if assess_blast_radius raised
    ValueError (e.g. `commit` has no parent -- the very first commit in a
    repo) -- a shadow trigger failing quietly is acceptable (spec pre-mortem:
    "a detached background process's own errors are invisible to the user at
    commit time by design").
    """
    try:
        report = assess_blast_radius(catalog_dir, project_root, commit=commit)
    except ValueError:
        return None

    all_matches = list(report.affected) + list(report.unverifiable)
    record = BlastRadiusRecord(
        entity_type="shadow_trigger",
        entity_id=commit,
        to_state="shadow_only",
        anchor_kind=report.anchor_kind,
        anchor_value=report.anchor_value,
        matched_files=json.dumps(sorted({f for m in all_matches for f in m.matched_files})),
        match_reason=(
            f"{len(report.affected)} affected, {len(report.unverifiable)} unverifiable "
            f"run(s) would have been flagged: {[m.run_id for m in all_matches]}"
        ),
    )
    return append_ledger_record(record, catalog_dir)
