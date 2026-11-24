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

import contextlib
import hashlib
import json
import posixpath
import re
import subprocess
import tomllib
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa

from bathos.catalog import read_runs
from bathos.checker import check_dependency_lock_drift, check_runs, hash_dependency_lock
from bathos.index import _LOCK_ERROR_MARKER as _DUCKDB_LOCK_MARKER
from bathos.index import catalog_readable, connect_read
from bathos.ledger import AppendOnlyLedger, now_iso
from bathos.lock_ref import read_lock_bytes
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
    return now_iso()


@dataclass(frozen=True)
class BlastRadiusRecord:
    """One append-only ledger entry: a `from_state -> to_state` transition for an
    entity identified by `(entity_type, entity_id)`."""

    entity_type: str  # "run" | "campaign" | "claim" | "shadow_trigger"
    entity_id: str
    to_state: str  # "affected" | "unverifiable" | "cleared" | "shadow_only"
    from_state: str | None = None
    # "commit" | "commit_range" | "file" | "dependency" | "sidecar" | "input_artifact" | "instrument"
    anchor_kind: str | None = None
    anchor_value: str | None = None
    matched_files: str | None = None  # JSON-encoded list[str]
    matched_clauses: str | None = None  # JSON-encoded list[str], claim entity_type only (AC-8)
    shadow_verdict: str | None = None  # JSON-encoded dict, never applied (AC-10)
    match_reason: str | None = None  # AC-13: why this entity matched
    reason: str | None = None  # free-form clearing justification (AC-9)
    amended_at: str = field(default_factory=_now_iso)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))


_LEDGER_RECORD_COLUMNS = (
    "id",
    "entity_type",
    "entity_id",
    "from_state",
    "to_state",
    "anchor_kind",
    "anchor_value",
    "matched_files",
    "matched_clauses",
    "shadow_verdict",
    "match_reason",
    "reason",
    "amended_at",
)
# Phase-1-era blast_radius_ledger tables predate matched_clauses/shadow_verdict
# (added Phase 2a, AC-8/AC-10). _connect() self-heals this via `ALTER TABLE ADD
# COLUMN IF NOT EXISTS` on every read-write connection -- but a caller-supplied
# READ-ONLY connection (see *_using_conn below) can never run that ALTER, so
# selecting the full column list against an un-migrated table raises
# duckdb.BinderException. Falling back to this legacy column set (rather than
# treating the BinderException as "no record" -> "clean") is load-bearing.
_LEDGER_RECORD_COLUMNS_LEGACY = tuple(
    c for c in _LEDGER_RECORD_COLUMNS if c not in ("matched_clauses", "shadow_verdict")
)


def _connect(catalog_dir: Path | str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(Path(catalog_dir) / "bathos.db"))
    con.execute(_LEDGER_TABLE_SCHEMA)
    # Phase 2a (#4552): CREATE TABLE IF NOT EXISTS above is a no-op against a table a
    # Phase-1-era catalog already created without matched_clauses/shadow_verdict.
    for _alter_sql in (
        "ALTER TABLE blast_radius_ledger ADD COLUMN IF NOT EXISTS matched_clauses TEXT",
        "ALTER TABLE blast_radius_ledger ADD COLUMN IF NOT EXISTS shadow_verdict TEXT",
    ):
        with contextlib.suppress(Exception):
            con.execute(_alter_sql)
    return con


def _row_to_record(columns: tuple[str, ...], row: tuple) -> BlastRadiusRecord:
    return _record_from_values(dict(zip(columns, row)))


def _record_from_values(values: Mapping[str, Any]) -> BlastRadiusRecord:
    return BlastRadiusRecord(
        id=values["id"],
        entity_type=values["entity_type"],
        entity_id=values["entity_id"],
        from_state=values["from_state"],
        to_state=values["to_state"],
        anchor_kind=values["anchor_kind"],
        anchor_value=values["anchor_value"],
        matched_files=values["matched_files"],
        matched_clauses=values.get("matched_clauses"),
        shadow_verdict=values.get("shadow_verdict"),
        match_reason=values["match_reason"],
        reason=values["reason"],
        amended_at=values["amended_at"],
    )


_LEDGER: AppendOnlyLedger[BlastRadiusRecord] = AppendOnlyLedger(
    table_name="blast_radius_ledger",
    fragments_dirname=_LEDGER_FRAGMENTS_DIRNAME,
    fragment_prefix="blast_radius",
    pk_col="id",
    order_col="amended_at",
    columns=_LEDGER_RECORD_COLUMNS,
    legacy_columns=_LEDGER_RECORD_COLUMNS_LEGACY,
    fragment_schema=_LEDGER_FRAGMENT_SCHEMA,
    runlog_kind="blast_radius.recorded",
    telemetry_prefix="blast_radius",
    connect_fn=_connect,
    record_from_values=_record_from_values,
    append_event_fields=lambda r: {
        "entity_type": r.entity_type,
        "entity_id": r.entity_id,
        "from_state": r.from_state,
        "to_state": r.to_state,
    },
)


def _ledger_fragments_dir(catalog_dir: Path | str) -> Path:
    return _LEDGER.fragments_dir(catalog_dir)


def write_ledger_fragment(record: BlastRadiusRecord, catalog_dir: Path | str) -> None:
    """Write an immutable cool-tier Parquet fragment for one ledger record."""
    _LEDGER.write_fragment(record, catalog_dir)


def read_ledger_fragments(catalog_dir: Path | str) -> list[BlastRadiusRecord]:
    """Read every cool-tier ledger fragment, unfolded (full history)."""
    return _LEDGER.read_fragments(catalog_dir)


def _insert_warm_row_using_conn(con: duckdb.DuckDBPyConnection, record: BlastRadiusRecord) -> None:
    """Like `_insert_warm_row`, but against an already-open connection."""
    _LEDGER.insert_warm_row_using_conn(con, record)


def _insert_warm_row(record: BlastRadiusRecord, catalog_dir: Path | str) -> None:
    _LEDGER.insert_warm_row(record, catalog_dir)


def append_ledger_record(
    record: BlastRadiusRecord,
    catalog_dir: Path | str,
    *,
    con: duckdb.DuckDBPyConnection | None = None,
    cwd: Path | None = None,
) -> BlastRadiusRecord:
    """Durably append one ledger record: cool-tier fragment + warm-tier row."""
    return _LEDGER.append_record(record, catalog_dir, con=con, cwd=cwd)


def _fetch_latest_record(con, entity_type: str, entity_id: str) -> BlastRadiusRecord | None:
    return _LEDGER.fetch_latest_record(
        con, [("entity_type", entity_type), ("entity_id", entity_id)]
    )


def _is_missing_ledger_table(exc: BaseException) -> bool:
    """True only when DuckDB reports that `blast_radius_ledger` does not exist."""
    return _LEDGER.is_missing_table_error(exc)


def _latest_record_from_fragments(
    catalog_dir: Path | str, entity_type: str, entity_id: str
) -> BlastRadiusRecord | None:
    """Latest-by-`amended_at` ledger record from cool-tier Parquet fragments."""
    return _LEDGER.latest_record_from_fragments(
        catalog_dir, [("entity_type", entity_type), ("entity_id", entity_id)]
    )


def latest_ledger_record(
    catalog_dir: Path | str, entity_type: str, entity_id: str
) -> BlastRadiusRecord | None:
    """Return the latest-by-`amended_at` ledger record for `(entity_type, entity_id)`,
    or `None` if no record exists."""
    return _LEDGER.latest_record_log_aware(
        catalog_dir, [("entity_type", entity_type), ("entity_id", entity_id)]
    )


def latest_ledger_record_using_conn(
    con, entity_type: str, entity_id: str
) -> BlastRadiusRecord | None:
    """Like `latest_ledger_record`, but queries an EXISTING connection instead of
    opening a new one."""
    return _LEDGER.latest_record_using_conn(
        con, [("entity_type", entity_type), ("entity_id", entity_id)]
    )


def fold_blast_radius_state(catalog_dir: Path | str, entity_type: str, entity_id: str) -> str:
    """Fold the ledger for `(entity_type, entity_id)`, latest-wins by `amended_at`.

    Returns `"clean"` if no ledger record exists at all -- an entity is implicitly
    clean until something flags it (unlike bathos.trust_ledger.fold_trust_state,
    which returns `None` for "never graduated"; blast-radius has no evidence-gated
    ratchet, so a plain default string is the right shape for callers).
    """
    latest = latest_ledger_record(catalog_dir, entity_type, entity_id)
    if latest is None:
        return "clean"
    return latest.to_state or "unverifiable"


def fold_blast_radius_state_using_conn(con, entity_type: str, entity_id: str) -> str:
    """Like `fold_blast_radius_state`, but via `latest_ledger_record_using_conn` --
    see that function's docstring for why this exists."""
    latest = latest_ledger_record_using_conn(con, entity_type, entity_id)
    if latest is None:
        return "clean"
    return latest.to_state or "unverifiable"


def _fold_state_using_conn(con: duckdb.DuckDBPyConnection, entity_type: str, entity_id: str) -> str:
    """Like `fold_blast_radius_state`, but against a connection this module itself
    already opened (via `_connect`) rather than a caller-supplied one.

    Unlike `fold_blast_radius_state_using_conn` (built for a possibly read-only,
    externally-owned connection that may predate this module's own migration
    ALTERs -- see its docstring), this does not swallow a non-BinderException
    `duckdb.Error`: `con` here was just opened by `_connect()`, which always runs
    the CREATE TABLE/ALTER migration, so any other error is a real anomaly and
    should surface exactly as `fold_blast_radius_state` itself would (used by
    `flag_blast_radius` to reuse one connection across a batch -- debt #1475).
    """
    latest = _fetch_latest_record(con, entity_type, entity_id)
    if latest is None:
        return "clean"
    return latest.to_state or "unverifiable"


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

    anchor_kind: str  # "commit" | "commit_range" | "file" | "dependency" | "sidecar" | "input_artifact" | "instrument"
    anchor_value: str
    changed_files: list[str]
    affected: list[BlastRadiusMatch]
    unverifiable: list[BlastRadiusMatch]
    unaffected_run_ids: list[str]
    #: Coverage caveats for this assessment, shown to the user alongside the buckets.
    #: For file anchors, a file that matched no run is reported here as "unverified":
    #: zero matches does not mean no run depends on the file (bathos records no import
    #: closure or runtime reads). Also covers catalog reads that fell back or were
    #: skipped because the catalog was locked, and sidecar/manifest texts that were
    #: only partly searched. Does not change what `unaffected_run_ids` means.
    warnings: list[str] = field(default_factory=list)


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


def _is_ancestor(candidate_sha: str, boundary_sha: str, project_root: Path) -> bool | None:
    """True iff candidate_sha is boundary_sha or an ancestor of it.

    Fails closed (False) on "not an ancestor" -- git's own documented rc=1 for
    `merge-base --is-ancestor` -- and also on an unresolvable/flag-like sha,
    which can never legitimately be an ancestor (security-audit finding). A
    flag-like sha fails closed here rather than raising -- this is a read-path
    classification helper, not a user-facing error site.

    Debt #1473: rc=1 ("not an ancestor") must not be conflated with a genuine
    git failure (any other nonzero rc: unresolvable revision, corrupt repo,
    git missing from PATH, etc). Those are surfaced via this module's own
    `event()` telemetry convention -- same rationale as read_ledger_fragments'
    corrupt-fragment handling: don't let one bad git_hash crash the whole
    assess_blast_radius scan over potentially thousands of runs, but never
    swallow the anomaly silently either. Returns None (unknown) rather than
    raising, since assess_blast_radius has no per-run try/except around this
    call; the caller treats unknown as affected, because a blast-radius check
    that cannot rule a run out must not silently clear it.
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
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    event(
        "blast_radius.ancestor_check_error",
        candidate_sha=candidate_sha,
        boundary_sha=boundary_sha,
        returncode=result.returncode,
        stderr=result.stderr.decode("utf-8", errors="replace").strip(),
    )
    return None


#: Cap on how much of a run's sidecar or manifest is read and searched. A file over
#: the cap is searched only up to it, and that is reported as a warning: a reference
#: past the cap would be missed, which is the false-negative direction.
_TEXT_SOURCE_CAP_BYTES = 1 << 20


class _MatchContext:
    """Per-assess-call state for file matching.

    `text_of` reads each sidecar/manifest at most once per call (keyed by the raw
    path recorded on the run), so thousands of runs sharing one sidecar cost one
    read. Unreadable paths resolve to None and are skipped silently. `contains`
    memoizes the substring test for the same reason.
    """

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self._texts: dict[str, tuple[str, str] | None] = {}
        self._contains: dict[tuple[str, str], bool] = {}
        #: resolved paths whose text was longer than the cap (first-seen order)
        self.truncated: list[str] = []

    def text_of(
        self, raw: str | None, run_id: str | None = None, sidecar_path: str = ""
    ) -> str | None:
        """Text of the file `raw` names. For a `git:` lock URI, `run_id` and `sidecar_path` let the reader fall
        through to the repo near the sidecar when the URI's own common dir is absent here (spec 261026 P4)."""
        if not raw:
            return None
        if raw not in self._texts:
            self._texts[raw] = self._load(raw, run_id, sidecar_path)
        loaded = self._texts[raw]
        return loaded[1] if loaded is not None else None

    def _load(
        self, raw: str, run_id: str | None = None, sidecar_path: str = ""
    ) -> tuple[str, str] | None:
        if raw.startswith("git:"):
            # A lock stored as a commit ref (spec 261026): same text, read through the one resolver.
            try:
                data = read_lock_bytes(raw, run_id, sidecar_path)
            except (OSError, ValueError):
                return None
            if len(data) > _TEXT_SOURCE_CAP_BYTES:
                data = data[:_TEXT_SOURCE_CAP_BYTES]
                if raw not in self.truncated:
                    self.truncated.append(raw)
            return raw, data.decode("utf-8", errors="replace")
        p = Path(raw)
        candidates = [p] if p.is_absolute() else [self.project_root / p, p]
        for cand in candidates:
            try:
                with cand.open("rb") as fh:
                    data = fh.read(_TEXT_SOURCE_CAP_BYTES + 1)
            except OSError:
                continue
            key = str(cand)
            if len(data) > _TEXT_SOURCE_CAP_BYTES:
                data = data[:_TEXT_SOURCE_CAP_BYTES]
                if key not in self.truncated:
                    self.truncated.append(key)
            return key, data.decode("utf-8", errors="replace")
        return None

    def contains(self, memo_id: str | None, text: str, needle: str) -> bool:
        if memo_id is None:
            return needle in text
        ck = (memo_id, needle)
        if ck not in self._contains:
            self._contains[ck] = needle in text
        return self._contains[ck]


def _run_match_sources(
    run: Run, changed_files: list[str], ctx: _MatchContext | None = None
) -> dict[str, list[str]]:
    """Map each changed file that matches `run` to the source(s) it matched on.

    Sources, in order (the label is what match_reason reports):
      - "command": run.command and each run.argv entry. Two-way substring, the
        original v1 heuristic (spec Decision Log #2): a changed path matches if it
        is a substring of the haystack or the haystack is a substring of it.
        Deliberately coarse; the accepted mitigation is match_reason auditability.
      - "sidecar path" / "manifest path": run.sidecar_path / run.manifest_path,
        same two-way test.
      - "metadata": run.metadata JSON text. One-way: the changed path appears in it.
      - "sidecar text" / "manifest text": the text of those files, when readable.
        One-way, for the same reason: a two-way test against a whole file body would
        let any short body match every path.

    Only bathos's recorded fields and the text of the run's own sidecar/manifest are
    consulted. bathos records no import closure or runtime reads, so a module or
    input that the run loads at runtime, and that no recorded field names, cannot
    match here. See the warnings on BlastRadiusReport.
    """
    if ctx is None:
        ctx = _MatchContext(Path.cwd())

    two_way: list[tuple[str, str]] = [("command", run.command or "")]
    two_way += [("command", arg or "") for arg in (run.argv or [])]
    two_way += [
        ("sidecar path", run.sidecar_path or ""),
        ("manifest path", run.manifest_path or ""),
    ]

    one_way: list[tuple[str, str, str | None]] = [("metadata", run.metadata or "", None)]
    for label, raw in (("sidecar text", run.sidecar_path), ("manifest text", run.manifest_path)):
        text = ctx.text_of(raw, run.id, run.sidecar_path or "")
        if text is not None:
            one_way.append((label, text, raw))

    sources: dict[str, list[str]] = {}
    for changed in changed_files:
        changed_norm = changed.replace("\\", "/")
        if not changed_norm:
            continue
        labels: list[str] = []
        for label, hay in two_way:
            hay_norm = hay.replace("\\", "/")
            if not hay_norm:
                continue
            # Just the two `in` checks: `s.endswith(t)` implies `t in s`, so an
            # endswith clause here could never independently flip the result --
            # dropped per code-review finding (verified: exhaustive brute-force
            # check found no case where the two endswith clauses mattered).
            if (changed_norm in hay_norm or hay_norm in changed_norm) and label not in labels:
                labels.append(label)
        for label, text, memo_id in one_way:
            if label not in labels and ctx.contains(memo_id, text, changed_norm):
                labels.append(label)
        if labels:
            sources[changed] = labels
    return sources


def _run_touches_files(
    run: Run, changed_files: list[str], ctx: _MatchContext | None = None
) -> list[str]:
    """Changed files that match `run`, in `changed_files` order. See _run_match_sources."""
    return list(_run_match_sources(run, changed_files, ctx))


def _via(sources: dict[str, list[str]], matched_files: list[str]) -> str:
    """Human label for which source(s) produced these matches, for match_reason."""
    labels: list[str] = []
    for f in matched_files:
        for label in sources.get(f, []):
            if label not in labels:
                labels.append(label)
    return ", ".join(labels)


def _cool_tier_runs(catalog_dir: Path, project: str | None) -> list[Run]:
    """Run list from the cool tier (Parquet fragments). Fallback for when the warm
    DuckDB catalog cannot be opened because another process holds its lock."""
    runs = read_runs(catalog_dir)
    if project:
        runs = [r for r in runs if r.project_slug == project]
    return runs


def _campaign_read_warning(exc: duckdb.Error) -> str:
    """User-facing text for a failed campaign-membership read. Deliberately does not
    echo the DuckDB message: the lock text names a PID and the message is not a
    user-facing instruction."""
    if _DUCKDB_LOCK_MARKER in str(exc):
        cause = "the catalog database is locked by another process"
    else:
        cause = f"the catalog read failed ({type(exc).__name__})"
    return (
        f"campaign membership was not read because {cause}; only each run's own "
        "campaign_id was used, so campaign-level and claim-level propagation may be "
        "incomplete. Unverified, not clean."
    )


#: list_runs()/check_runs() require an int limit (no "unbounded" sentinel) and build
#: their SQL with no ORDER BY, so the default limit=50 silently drops an ARBITRARY
#: subset of runs on any catalog above 50 rows -- not "the 50 most recent". That is
#: exactly the multi-project, many-run scale bathos is designed for (CLAUDE.md:
#: "a single researcher across 10+ projects"), and for a tool whose entire purpose is
#: "did I miss a run this bug affects", silent truncation is a false-negative machine,
#: worse than the heuristic-noise false-positive risk the spec's pre-mortem already
#: accepted. Pass this instead of the 50-row default. Found in PR #54 review.
_UNBOUNDED_SCAN_LIMIT = 1_000_000


_HEX_DIGEST_RE = re.compile(r"^(?:sha256:)?[0-9a-fA-F]{7,64}$", re.IGNORECASE)


def _path_boundary_match(a: str, b: str) -> bool:
    """True when *a* and *b* match at a full path-component boundary."""
    a_norm = posixpath.normpath(a.replace("\\", "/"))
    b_norm = posixpath.normpath(b.replace("\\", "/"))
    if not a_norm or a_norm == "." or not b_norm or b_norm == ".":
        return False
    if a_norm == b_norm:
        return True
    if not b_norm.startswith("/") and a_norm.endswith("/" + b_norm):
        return True
    return not a_norm.startswith("/") and b_norm.endswith("/" + a_norm)


def _toml_has_clause(doc: Mapping[str, Any], clause: str) -> bool:
    """True when dotted *clause* (or bare key) exists as a structural TOML key/table."""
    parts = [p for p in clause.split(".") if p]
    if not parts:
        return False

    def _walk_dotted(node: Mapping[str, Any], rem: list[str]) -> bool:
        if not rem:
            return True
        full_rem = ".".join(rem)
        if full_rem in node:
            return True
        head = rem[0]
        if head in node:
            if len(rem) == 1:
                return True
            child = node[head]
            if isinstance(child, Mapping):
                return _walk_dotted(child, rem[1:])
        return False

    if _walk_dotted(doc, parts):
        return True
    if len(parts) == 1:
        key = parts[0]
        stack: list[Mapping[str, Any]] = [doc]
        while stack:
            cur = stack.pop()
            if key in cur:
                return True
            for v in cur.values():
                if isinstance(v, Mapping):
                    stack.append(v)
    return False


def _sidecar_clause_matches(text: str, clause: str) -> bool:
    """True when *clause* (e.g. 'outcomes.pass', 'outcomes.pass.condition', or key) exists in TOML *text*."""
    doc = tomllib.loads(text)
    return _toml_has_clause(doc, clause)


def _read_live_text_verified(
    raw: str | None,
    expected_sha256: str,
    project_root: Path,
    ctx: _MatchContext,
    run_id: str | None,
) -> str | None:
    """Read live sidecar text only if *raw*'s bytes match *expected_sha256*."""
    if not raw or not expected_sha256:
        return None
    expected_lower = expected_sha256.lower()
    if raw.startswith("git:"):
        try:
            data = read_lock_bytes(raw, run_id, raw)
        except (OSError, ValueError):
            return None
        if hashlib.sha256(data).hexdigest().lower() != expected_lower:
            return None
        if len(data) > _TEXT_SOURCE_CAP_BYTES and raw not in ctx.truncated:
            ctx.truncated.append(raw)
        return data[:_TEXT_SOURCE_CAP_BYTES].decode("utf-8", errors="replace")

    p = Path(raw)
    candidates = [p] if p.is_absolute() else [project_root / p, p]
    for cand in candidates:
        try:
            data = cand.read_bytes()
        except OSError:
            continue
        if hashlib.sha256(data).hexdigest().lower() != expected_lower:
            return None
        if len(data) > _TEXT_SOURCE_CAP_BYTES:
            key = str(cand)
            if key not in ctx.truncated:
                ctx.truncated.append(key)
        return data[:_TEXT_SOURCE_CAP_BYTES].decode("utf-8", errors="replace")
    return None


def _resolve_sidecar_text(
    run: Run, base: str, is_sha: bool, ctx: _MatchContext, catalog_dir: Path
) -> str | None:
    """Resolve frozen sidecar snapshot first, else live file only when its SHA256 matches."""
    if not run.sidecar_sha256:
        return None
    with contextlib.suppress(Exception):
        from bathos.sidecar import sidecar_snapshot_path

        snap = sidecar_snapshot_path(catalog_dir, run.sidecar_sha256)
        if snap.is_file():
            raw = snap.read_text(encoding="utf-8")
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                payload = None
            if isinstance(payload, dict):
                raw_text = payload.get("raw_text") or payload.get("content")
                if isinstance(raw_text, str):
                    return raw_text
            return raw

    text = _read_live_text_verified(
        run.sidecar_path, run.sidecar_sha256, ctx.project_root, ctx, run.id
    )
    if text is not None:
        return text
    if not is_sha and not run.sidecar_path:
        return _read_live_text_verified(base, run.sidecar_sha256, ctx.project_root, ctx, run.id)
    return None


def _assess_sidecar_run(
    run: Run,
    *,
    base: str,
    clause: str,
    is_sha: bool,
    disk_sha: str | None,
    ctx: _MatchContext,
    catalog_dir: Path,
    campaign_id: str,
) -> tuple[str, BlastRadiusMatch | None]:
    """Classify *run* against a sidecar anchor ('affected', 'unverifiable', or 'unaffected')."""
    base_norm = base.replace("\\", "/")
    matched_via: list[str] = []
    matched_files: list[str] = []

    if is_sha:
        needle = base.lower().removeprefix("sha256:")
        for label, val in (
            ("sidecar_sha256", (run.sidecar_sha256 or "").lower()),
            ("component_sidecar_sha256", (run.component_sidecar_sha256 or "").lower()),
        ):
            if val and (val == needle or val.startswith(needle)):
                matched_via.append(label)
        if matched_via and run.sidecar_path:
            matched_files.append(run.sidecar_path)
    else:
        sp = (run.sidecar_path or "").replace("\\", "/")
        if sp and _path_boundary_match(sp, base_norm):
            matched_via.append("sidecar path")
            matched_files.append(base)
        if disk_sha and run.sidecar_sha256 and run.sidecar_sha256.lower() == disk_sha.lower():
            matched_via.append("sidecar_sha256")
            if base not in matched_files:
                matched_files.append(base)

    if not matched_via:
        return "unaffected", None

    via = ", ".join(matched_via)
    if not clause:
        return "affected", BlastRadiusMatch(
            run_id=run.id,
            git_hash=run.git_hash,
            command=run.command,
            campaign_id=campaign_id,
            matched_files=matched_files,
            reason=f"matches sidecar {base} via {via}",
        )

    text = _resolve_sidecar_text(run, base, is_sha, ctx, catalog_dir)
    if text is None:
        return "unverifiable", BlastRadiusMatch(
            run_id=run.id,
            git_hash=run.git_hash,
            command=run.command,
            campaign_id=campaign_id,
            matched_files=matched_files,
            reason=f"matches sidecar {base} via {via}, but sidecar text is unreadable or modified to verify clause {clause!r}",
        )
    try:
        matched_clause = _sidecar_clause_matches(text, clause)
    except tomllib.TOMLDecodeError:
        return "unverifiable", BlastRadiusMatch(
            run_id=run.id,
            git_hash=run.git_hash,
            command=run.command,
            campaign_id=campaign_id,
            matched_files=matched_files,
            reason=f"matches sidecar {base} via {via}, but sidecar text is unreadable/invalid TOML to verify clause {clause!r}",
        )
    if matched_clause:
        return "affected", BlastRadiusMatch(
            run_id=run.id,
            git_hash=run.git_hash,
            command=run.command,
            campaign_id=campaign_id,
            matched_files=matched_files,
            reason=f"matches sidecar {base} via {via} and contains clause {clause!r}",
        )
    return "unaffected", None


def _one_way_path_sources(
    run: Run, art_path: str, ctx: _MatchContext
) -> dict[str, list[str]]:
    """One-way path-boundary-aware search for *art_path* across run fields and sidecar/manifest texts."""
    art_norm = posixpath.normpath(art_path.replace("\\", "/"))
    if not art_norm or art_norm == ".":
        return {}
    pat = re.compile(rf"(?:^|[/\s'\"=]){re.escape(art_norm)}(?:$|[/\s'\",;\]\}}])")

    labels: list[str] = []
    hay_sources: list[tuple[str, str]] = [
        ("command", (run.command or "").replace("\\", "/")),
        *[("command", (arg or "").replace("\\", "/")) for arg in (run.argv or [])],
        ("sidecar path", (run.sidecar_path or "").replace("\\", "/")),
        ("manifest path", (run.manifest_path or "").replace("\\", "/")),
        ("metadata", (run.metadata or "").replace("\\", "/")),
        ("output_metadata", (run.output_metadata or "").replace("\\", "/")),
    ]
    for label, raw in (("sidecar text", run.sidecar_path), ("manifest text", run.manifest_path)):
        text = ctx.text_of(raw, run.id, run.sidecar_path or "")
        if text is not None:
            hay_sources.append((label, text.replace("\\", "/")))

    for label, hay in hay_sources:
        if not hay or label in labels:
            continue
        if pat.search(hay):
            labels.append(label)
    return {art_path: labels} if labels else {}


def _one_way_digest_sources(
    run: Run, art_digest: str, ctx: _MatchContext
) -> dict[str, list[str]]:
    """One-way hex-boundary search for *art_digest* across run fields and sidecar/manifest texts."""
    hex_part = art_digest.lower().removeprefix("sha256:").strip()
    if len(hex_part) < 4:
        return {}
    pat = re.compile(rf"(?<![0-9a-f]){re.escape(hex_part)}")

    labels: list[str] = []
    hay_sources: list[tuple[str, str]] = [
        ("command", run.command or ""),
        *[("command", arg or "") for arg in (run.argv or [])],
        ("metadata", run.metadata or ""),
        ("output_metadata", run.output_metadata or ""),
    ]
    for label, raw in (("sidecar text", run.sidecar_path), ("manifest text", run.manifest_path)):
        text = ctx.text_of(raw, run.id, run.sidecar_path or "")
        if text is not None:
            hay_sources.append((label, text))

    for label, hay in hay_sources:
        if not hay or label in labels:
            continue
        if pat.search(hay.lower()):
            labels.append(label)
    return {art_digest: labels} if labels else {}


def _assess_input_artifact_run(
    run: Run,
    *,
    art_path: str,
    art_digest: str | None,
    ctx: _MatchContext,
    campaign_id: str,
) -> tuple[str, BlastRadiusMatch | None]:
    """Classify *run* against an input-artifact anchor (path, digest, or path@digest)."""
    path_sources = _one_way_path_sources(run, art_path, ctx) if art_path else {}
    digest_sources = _one_way_digest_sources(run, art_digest, ctx) if art_digest else {}

    if art_path and art_digest:
        if path_sources and digest_sources:
            matched_d = list(digest_sources)
            return "affected", BlastRadiusMatch(
                run_id=run.id,
                git_hash=run.git_hash,
                command=run.command,
                campaign_id=campaign_id,
                matched_files=[art_path],
                reason=(
                    f"matches input artifact '{art_path}' ({_via(path_sources, [art_path])}) "
                    f"and digest {art_digest} ({_via(digest_sources, matched_d)})"
                ),
            )
        if path_sources:
            return "unverifiable", BlastRadiusMatch(
                run_id=run.id,
                git_hash=run.git_hash,
                command=run.command,
                campaign_id=campaign_id,
                matched_files=[art_path],
                reason=(
                    f"touches input artifact path '{art_path}' via {_via(path_sources, [art_path])}, "
                    f"but digest {art_digest} was not recorded on the run"
                ),
            )
        return "unaffected", None

    if art_digest:
        if digest_sources:
            matched_d = list(digest_sources)
            return "affected", BlastRadiusMatch(
                run_id=run.id,
                git_hash=run.git_hash,
                command=run.command,
                campaign_id=campaign_id,
                matched_files=matched_d,
                reason=f"matches input artifact digest {art_digest} via {_via(digest_sources, matched_d)}",
            )
        return "unaffected", None

    if not path_sources:
        return "unaffected", None
    return "affected", BlastRadiusMatch(
        run_id=run.id,
        git_hash=run.git_hash,
        command=run.command,
        campaign_id=campaign_id,
        matched_files=[art_path],
        reason=f"references input artifact '{art_path}' via {_via(path_sources, [art_path])}",
    )


def _resolve_instrument_spec(
    instrument: str, project_root: Path
) -> tuple[str, str | None, Any | None]:
    """Parse *instrument* into ``(package, target_version, defect_entry)``."""
    from bathos.version_resolver import load_instrument_registries

    val = instrument.strip()
    registries = load_instrument_registries(project_root)

    if ":" in val and "://" not in val:
        pkg, _, defect_id = val.partition(":")
        pkg, defect_id = pkg.strip(), defect_id.strip()
        if not pkg or not defect_id:
            raise ValueError(f"malformed instrument anchor {instrument!r}")
        reg = registries.get(pkg)
        if reg is None:
            raise ValueError(f"instrument registry for package {pkg!r} not found")
        entry = next((e for e in reg.entries if e.id == defect_id), None)
        if entry is None:
            raise ValueError(f"instrument defect {defect_id!r} not found in {pkg!r} registry")
        return pkg, None, entry

    if "==" in val:
        pkg, _, ver = val.partition("==")
        if not pkg.strip() or not ver.strip():
            raise ValueError(f"malformed instrument anchor {instrument!r}")
        return pkg.strip(), ver.strip(), None

    if "@" in val:
        pkg, _, ver = val.partition("@")
        if not pkg.strip() or not ver.strip():
            raise ValueError(f"malformed instrument anchor {instrument!r}")
        return pkg.strip(), ver.strip(), None

    for pkg, reg in registries.items():
        for entry in reg.entries:
            if entry.id == val:
                return pkg, None, entry

    return val, None, None


def _resolve_inst_git_repo(
    pkg: str,
    self_pkg: bool,
    target_commit: str | None,
    introduced_commit: str | None,
    project_root: Path | None,
) -> Path:
    from bathos.version_resolver import is_valid_git_commit, key_prefix

    base_root = project_root or Path.cwd()
    if self_pkg or project_root is None:
        return base_root
    check_sha = target_commit or introduced_commit
    if check_sha and is_valid_git_commit(check_sha, base_root):
        return base_root
    pkg_key = key_prefix(pkg)
    for cand_name in (pkg, pkg_key, pkg.replace("_", "-")):
        sibling = project_root.parent / cand_name
        if (
            sibling.is_dir()
            and (sibling / ".git").exists()
            and (not check_sha or is_valid_git_commit(check_sha, sibling))
        ):
            return sibling
    return base_root


def _assess_instrument_run(
    run: Run,
    *,
    pkg: str,
    target_version: str | None,
    defect_entry: Any | None,
    campaign_id: str,
    ver_key: str,
    project_root: Path | None = None,
    catalog_dir: Path | None = None,
) -> tuple[str, BlastRadiusMatch | None, bool]:
    """Classify *run* against an instrument anchor.

    Returns ``(bucket, match_or_none, recorded_pkg_flag)``.
    """
    from packaging.version import InvalidVersion, Version

    from bathos.version_resolver import (
        commit_in_range,
        evaluate_predicate,
        is_self_package,
        is_valid_git_commit,
        resolve_toolchain_version,
        version_in_range,
    )

    if run.dependency_provenance:
        try:
            prov = json.loads(run.dependency_provenance)
        except (json.JSONDecodeError, TypeError):
            return (
                "unverifiable",
                BlastRadiusMatch(
                    run_id=run.id,
                    git_hash=run.git_hash,
                    command=run.command,
                    campaign_id=campaign_id,
                    matched_files=[],
                    reason="malformed dependency_provenance JSON -- cannot verify instrument version",
                ),
                False,
            )
        if not isinstance(prov, dict):
            return (
                "unverifiable",
                BlastRadiusMatch(
                    run_id=run.id,
                    git_hash=run.git_hash,
                    command=run.command,
                    campaign_id=campaign_id,
                    matched_files=[],
                    reason="non-dict dependency_provenance -- cannot verify instrument version",
                ),
                False,
            )
        for cand_key in (ver_key, f"{pkg}_version"):
            if cand_key in prov:
                raw_val = prov.get(cand_key)
                sval = str(raw_val).strip() if raw_val is not None else ""
                if not sval or sval.lower() == "unknown":
                    commit_key = cand_key.removesuffix("_version") + "_source_commit"
                    cval = str(prov.get(commit_key) or "").strip()
                    if not cval or cval.lower() == "unknown":
                        return (
                            "unverifiable",
                            BlastRadiusMatch(
                                run_id=run.id,
                                git_hash=run.git_hash,
                                command=run.command,
                                campaign_id=campaign_id,
                                matched_files=[],
                                reason=f"recorded {cand_key} is {sval or 'empty'!r} -- cannot verify instrument version",
                            ),
                            True,
                        )

    ver, commit, v_source = resolve_toolchain_version(
        run, project_root, pkg, catalog_dir=catalog_dir
    )
    if v_source == "unknown" or (not ver and not commit):
        reason = (
            "no recorded dependency_provenance -- cannot verify instrument version"
            if not run.dependency_provenance
            else f"dependency_provenance lacks {pkg} and version could not be resolved -- cannot verify instrument version"
        )
        return (
            "unverifiable",
            BlastRadiusMatch(
                run_id=run.id,
                git_hash=run.git_hash,
                command=run.command,
                campaign_id=campaign_id,
                matched_files=[],
                reason=reason,
            ),
            False,
        )

    recorded_pkg = True
    ver_label = ver or commit or "unknown"

    if defect_entry is not None:
        if not defect_entry.ranges:
            matches_range = True
            unverifiable_reason: str | None = None
        else:
            matches_range = False
            evaluated_any = False
            unverifiable_reason = None
            for r in defect_entry.ranges:
                if r.type == "ECOSYSTEM":
                    if ver:
                        try:
                            Version(ver)
                        except InvalidVersion:
                            unverifiable_reason = (
                                unverifiable_reason
                                or f"unparseable {pkg} version {ver!r} -- cannot verify defect {defect_entry.id}"
                            )
                            continue
                        evaluated_any = True
                        last_aff = getattr(r, "last_affected", None)
                        fixed_targets = (
                            r.fixed_all
                            if getattr(r, "fixed_all", None) and r.fixed is None
                            else (r.fixed,)
                        )
                        if any(
                            version_in_range(ver, r.introduced, f, last_affected=last_aff)
                            for f in fixed_targets
                        ):
                            matches_range = True
                            break
                    else:
                        unverifiable_reason = (
                            unverifiable_reason
                            or f"missing {pkg} version for ECOSYSTEM range -- cannot verify defect {defect_entry.id}"
                        )
                elif r.type == "GIT":
                    self_pkg = is_self_package(pkg, run, project_root)
                    target_commit = commit or (run.git_hash if self_pkg else None)
                    repo_root = _resolve_inst_git_repo(
                        pkg, self_pkg, target_commit, r.introduced, project_root
                    )
                    fixed_targets = r.fixed_all if getattr(r, "fixed_all", None) else r.fixed
                    if target_commit and is_valid_git_commit(target_commit, repo_root):
                        evaluated_any = True
                        if commit_in_range(target_commit, r.introduced, fixed_targets, repo_root):
                            matches_range = True
                            break
                    else:
                        unverifiable_reason = (
                            unverifiable_reason
                            or f"unresolvable {pkg} commit for GIT range -- cannot verify defect {defect_entry.id}"
                        )

            if not matches_range:
                if not evaluated_any or unverifiable_reason:
                    return (
                        "unverifiable",
                        BlastRadiusMatch(
                            run_id=run.id,
                            git_hash=run.git_hash,
                            command=run.command,
                            campaign_id=campaign_id,
                            matched_files=[],
                            reason=unverifiable_reason
                            or f"cannot evaluate defect {defect_entry.id} range for {pkg}",
                        ),
                        recorded_pkg,
                    )
                return "unaffected", None, recorded_pkg

        if getattr(defect_entry, "predicate", None) is not None:
            pred_match, pred_err = evaluate_predicate(defect_entry.predicate.condition, run)
            if not pred_match:
                if pred_err:
                    return (
                        "unverifiable",
                        BlastRadiusMatch(
                            run_id=run.id,
                            git_hash=run.git_hash,
                            command=run.command,
                            campaign_id=campaign_id,
                            matched_files=[],
                            reason=(
                                f"instrument {pkg}=={ver_label} matched defect {defect_entry.id} "
                                f"range, but predicate unverified ({pred_err})"
                            ),
                        ),
                        recorded_pkg,
                    )
                return "unaffected", None, recorded_pkg

        return (
            "affected",
            BlastRadiusMatch(
                run_id=run.id,
                git_hash=run.git_hash,
                command=run.command,
                campaign_id=campaign_id,
                matched_files=[],
                reason=f"instrument {pkg}=={ver_label} matches defect {defect_entry.id}",
            ),
            recorded_pkg,
        )

    if target_version is not None:
        if ver:
            try:
                parsed_v = Version(ver)
            except InvalidVersion:
                if ver == target_version or (
                    commit and (commit == target_version or commit.startswith(target_version))
                ):
                    return (
                        "affected",
                        BlastRadiusMatch(
                            run_id=run.id,
                            git_hash=run.git_hash,
                            command=run.command,
                            campaign_id=campaign_id,
                            matched_files=[],
                            reason=f"instrument {pkg}=={ver} matches defective version {target_version}",
                        ),
                        recorded_pkg,
                    )
                return (
                    "unverifiable",
                    BlastRadiusMatch(
                        run_id=run.id,
                        git_hash=run.git_hash,
                        command=run.command,
                        campaign_id=campaign_id,
                        matched_files=[],
                        reason=f"unparseable {pkg} version {ver!r} -- cannot compare to {target_version}",
                    ),
                    recorded_pkg,
                )
            try:
                parsed_target = Version(target_version)
                is_match = parsed_v == parsed_target
            except InvalidVersion:
                is_match = ver == target_version
            if is_match:
                return (
                    "affected",
                    BlastRadiusMatch(
                        run_id=run.id,
                        git_hash=run.git_hash,
                        command=run.command,
                        campaign_id=campaign_id,
                        matched_files=[],
                        reason=f"instrument {pkg}=={ver} matches defective version {target_version}",
                    ),
                    recorded_pkg,
                )
            return "unaffected", None, recorded_pkg
        if commit and (commit == target_version or commit.startswith(target_version)):
            return (
                "affected",
                BlastRadiusMatch(
                    run_id=run.id,
                    git_hash=run.git_hash,
                    command=run.command,
                    campaign_id=campaign_id,
                    matched_files=[],
                    reason=f"instrument {pkg}@{commit} matches defective target {target_version}",
                ),
                recorded_pkg,
            )
        return (
            "unverifiable",
            BlastRadiusMatch(
                run_id=run.id,
                git_hash=run.git_hash,
                command=run.command,
                campaign_id=campaign_id,
                matched_files=[],
                reason=f"instrument {pkg} version missing -- cannot compare to {target_version}",
            ),
            recorded_pkg,
        )

    return (
        "affected",
        BlastRadiusMatch(
            run_id=run.id,
            git_hash=run.git_hash,
            command=run.command,
            campaign_id=campaign_id,
            matched_files=[],
            reason=f"instrument {pkg}=={ver_label} recorded in dependency_provenance",
        ),
        recorded_pkg,
    )


def assess_blast_radius(
    catalog_dir: Path | str,
    project_root: Path | str,
    *,
    commit: str | None = None,
    commit_range: str | None = None,
    files: list[str] | None = None,
    dependency: bool = False,
    sidecar: str | None = None,
    input_artifact: str | None = None,
    instrument: str | None = None,
    project: str | None = None,
) -> BlastRadiusReport:
    """Assess which catalogued runs a bug/fix implicates (AC-1..AC-5, #2686).

    Exactly one of `commit`, `commit_range`, `files`, `dependency=True`,
    `sidecar`, `input_artifact`, or `instrument` must be given. This function
    is a pure read: it does not write to the ledger. Callers pass the returned
    report to flag_blast_radius() to durably record it (AC-11: report-then-flag
    ordering).

    Args:
        catalog_dir: Path to the bathos catalog root.
        project_root: Path to the git repository to diff/query ancestry against.
        commit: A single fix commit SHA.
        commit_range: A `"<base>..<tip>"` range.
        files: Explicit file/symbol path(s) (AC-2, no commit ancestry check).
        dependency: If True (AC-3), assess for dependency-lock drift.
        sidecar: Non-code defect anchor (#2686): sidecar path, `<path>#<clause>`,
            or sidecar sha256 (prefix or full) implicating runs referencing that
            sidecar/clause.
        input_artifact: Non-code defect anchor (#2686): input artifact path,
            digest, or `<path>@<digest>` implicating runs referencing that input.
        instrument: Non-code defect anchor (#2686): instrument package (`"aminx"`),
            pinned version (`"aminx@0.12.1"` or `"aminx==0.12.1"`), or defect ID
            (`"AMINX-2026-001"` / `"aminx:AMINX-2026-001"`).
        project: Optional project_slug filter.

    Returns:
        A BlastRadiusReport.
    """
    n_anchors = sum(
        x is not None
        for x in (commit, commit_range, files, sidecar, input_artifact, instrument)
    ) + int(dependency)
    if n_anchors != 1:
        raise ValueError(
            "assess_blast_radius requires exactly one of commit, commit_range, files, "
            f"dependency=True, sidecar, input_artifact, or instrument (got {n_anchors})"
        )

    project_root = Path(project_root)
    boundary: str | None = None
    changed_files: list[str] = []

    sidecar_base = ""
    sidecar_clause = ""
    sidecar_is_sha = False
    sidecar_disk_sha: str | None = None

    art_path = ""
    art_digest: str | None = None

    inst_pkg = ""
    inst_ver: str | None = None
    inst_defect: Any | None = None
    inst_ver_key = ""

    if dependency:
        anchor_kind = "dependency"
        current_lock_hash = hash_dependency_lock(project_root)
        anchor_value = current_lock_hash or "no-uv.lock-present"
    elif commit is not None:
        anchor_kind, anchor_value = "commit", commit
        changed_files = _git_diff_name_only(f"{commit}^", commit, project_root)
        boundary = f"{commit}^"
    elif commit_range is not None:
        if ".." not in commit_range:
            raise ValueError(f"commit_range must contain '..', got {commit_range!r}")
        if "..." in commit_range:
            raise ValueError(
                f"commit_range must use two-dot '<base>..<tip>' syntax, not "
                f"three-dot (symmetric-difference) syntax: got {commit_range!r}"
            )
        anchor_kind, anchor_value = "commit_range", commit_range
        changed_files = _git_diff_name_only_range(commit_range, project_root)
        boundary = commit_range.split("..")[0]
    elif files is not None:
        anchor_kind, anchor_value = "file", ",".join(files)
        changed_files = list(files)
    elif sidecar is not None:
        anchor_value = sidecar.strip()
        if not anchor_value:
            raise ValueError("sidecar anchor must be non-empty")
        anchor_kind = "sidecar"
        raw_base, has_clause, raw_clause = anchor_value.partition("#")
        sidecar_base = raw_base.strip()
        sidecar_clause = raw_clause.strip()
        if not sidecar_base:
            raise ValueError("sidecar anchor must name a sidecar path or sha256")
        if has_clause and not sidecar_clause:
            raise ValueError("sidecar anchor '#' clause must be non-empty")
        sidecar_is_sha = bool(_HEX_DIGEST_RE.fullmatch(sidecar_base))
        if not sidecar_is_sha:
            changed_files = [sidecar_base]
            cand = Path(sidecar_base) if Path(sidecar_base).is_absolute() else project_root / sidecar_base
            if cand.is_file():
                with contextlib.suppress(OSError):
                    sidecar_disk_sha = hashlib.sha256(cand.read_bytes()).hexdigest()
    elif input_artifact is not None:
        anchor_value = input_artifact.strip()
        if not anchor_value:
            raise ValueError("input_artifact anchor must be non-empty")
        anchor_kind = "input_artifact"
        if "@" in anchor_value:
            raw_p, _, raw_d = anchor_value.partition("@")
            art_path = raw_p.strip()
            art_digest = raw_d.strip()
            if not art_path or not art_digest:
                raise ValueError(f"malformed input_artifact anchor {input_artifact!r}")
        elif anchor_value.lower().startswith("sha256:"):
            hex_part = anchor_value[7:]
            if not re.fullmatch(r"[0-9a-fA-F]{7,64}", hex_part):
                raise ValueError(f"malformed input_artifact digest {input_artifact!r}")
            art_digest = anchor_value
        elif re.fullmatch(r"[0-9a-fA-F]{64}", anchor_value):
            art_digest = anchor_value
        else:
            art_path = anchor_value
        if art_path:
            changed_files = [art_path]
    else:
        assert instrument is not None
        anchor_value = instrument.strip()
        if not anchor_value:
            raise ValueError("instrument anchor must be non-empty")
        anchor_kind = "instrument"
        from bathos.version_resolver import key_prefix

        inst_pkg, inst_ver, inst_defect = _resolve_instrument_spec(anchor_value, project_root)
        inst_ver_key = f"{key_prefix(inst_pkg)}_version"

    warnings: list[str] = []

    try:
        all_runs = list_runs(Path(catalog_dir), project=project, limit=_UNBOUNDED_SCAN_LIMIT)
    except duckdb.Error as exc:
        if _DUCKDB_LOCK_MARKER not in str(exc):
            raise
        all_runs = _cool_tier_runs(Path(catalog_dir), project)
        warnings.append(
            "run list was read from the cool tier (Parquet fragments), not the warm "
            "DuckDB catalog, because the catalog database is locked by another process. "
            "The cool tier can differ from the warm tier until the next compact."
        )
    check_results = (
        {
            r.run_id: r
            for r in check_runs(
                Path(catalog_dir),
                project_root,
                project=project,
                limit=_UNBOUNDED_SCAN_LIMIT,
                runs=all_runs,
            )
        }
        if anchor_kind in ("commit", "commit_range", "file")
        else {}
    )

    campaign_by_run: dict[str, str] = {}
    if catalog_readable(Path(catalog_dir)):
        try:
            con = connect_read(Path(catalog_dir), read_only=True)
            try:
                rows = con.execute("SELECT run_id, campaign_id FROM campaign_runs").fetchall()
            finally:
                con.close()
            campaign_by_run = dict(rows)
        except duckdb.Error as exc:
            warnings.append(_campaign_read_warning(exc))

    ctx = _MatchContext(project_root)
    matched_anywhere: set[str] = set()
    affected: list[BlastRadiusMatch] = []
    unverifiable: list[BlastRadiusMatch] = []
    unaffected_run_ids: list[str] = []
    instrument_pkg_recorded_count = 0

    for run in all_runs:
        camp_id = run.campaign_id or campaign_by_run.get(run.id, "")
        if anchor_kind == "dependency":
            if not run.dependency_lock_sha256:
                unverifiable.append(
                    BlastRadiusMatch(
                        run_id=run.id,
                        git_hash=run.git_hash,
                        command=run.command,
                        campaign_id=camp_id,
                        matched_files=[],
                        reason=(
                            "no recorded dependency_lock_sha256 -- predates that "
                            "field or was never captured; cannot verify"
                        ),
                    )
                )
                continue
            if check_dependency_lock_drift(
                run.dependency_lock_sha256, project_root, current_sha256=current_lock_hash
            ):
                affected.append(
                    BlastRadiusMatch(
                        run_id=run.id,
                        git_hash=run.git_hash,
                        command=run.command,
                        campaign_id=camp_id,
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

        if anchor_kind == "sidecar":
            bucket, match = _assess_sidecar_run(
                run,
                base=sidecar_base,
                clause=sidecar_clause,
                is_sha=sidecar_is_sha,
                disk_sha=sidecar_disk_sha,
                ctx=ctx,
                catalog_dir=Path(catalog_dir),
                campaign_id=camp_id,
            )
            if bucket == "affected" and match is not None:
                affected.append(match)
                matched_anywhere.update(match.matched_files)
            elif bucket == "unverifiable" and match is not None:
                unverifiable.append(match)
                matched_anywhere.update(match.matched_files)
            else:
                unaffected_run_ids.append(run.id)
            continue

        if anchor_kind == "input_artifact":
            bucket, match = _assess_input_artifact_run(
                run,
                art_path=art_path,
                art_digest=art_digest,
                ctx=ctx,
                campaign_id=camp_id,
            )
            if bucket == "affected" and match is not None:
                affected.append(match)
                matched_anywhere.update(match.matched_files)
            elif bucket == "unverifiable" and match is not None:
                unverifiable.append(match)
                matched_anywhere.update(match.matched_files)
            else:
                unaffected_run_ids.append(run.id)
            continue

        if anchor_kind == "instrument":
            bucket, match, recorded_pkg = _assess_instrument_run(
                run,
                pkg=inst_pkg,
                target_version=inst_ver,
                defect_entry=inst_defect,
                campaign_id=camp_id,
                ver_key=inst_ver_key,
                project_root=project_root,
                catalog_dir=Path(catalog_dir),
            )
            if recorded_pkg:
                instrument_pkg_recorded_count += 1
            if bucket == "affected" and match is not None:
                affected.append(match)
            elif bucket == "unverifiable" and match is not None:
                unverifiable.append(match)
            else:
                unaffected_run_ids.append(run.id)
            continue

        sources = _run_match_sources(run, changed_files, ctx)
        matched_files = list(sources)
        if not matched_files:
            unaffected_run_ids.append(run.id)
            continue
        matched_anywhere.update(matched_files)
        touch = f"touches {matched_files} via {_via(sources, matched_files)}"

        status = check_results[run.id].status if run.id in check_results else "OK"

        if status in ("DIRTY_RUN", "UNKNOWN_CODE"):
            unverifiable.append(
                BlastRadiusMatch(
                    run_id=run.id,
                    git_hash=run.git_hash,
                    command=run.command,
                    campaign_id=camp_id,
                    matched_files=matched_files,
                    reason=(
                        f"{touch} but git status is {status} -- "
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
                    campaign_id=camp_id,
                    matched_files=matched_files,
                    reason=f"{touch} (file anchor -- no commit ancestry check applied)",
                )
            )
            continue

        ancestry = _is_ancestor(run.git_hash, boundary, project_root)
        if ancestry is None:
            affected.append(
                BlastRadiusMatch(
                    run_id=run.id,
                    git_hash=run.git_hash,
                    command=run.command,
                    campaign_id=camp_id,
                    matched_files=matched_files,
                    reason=(
                        f"{touch}; ancestry of git_hash "
                        f"{run.git_hash[:9]} vs fix boundary {boundary} could not be "
                        "determined (git error) -- flagged conservatively"
                    ),
                )
            )
        elif ancestry:
            affected.append(
                BlastRadiusMatch(
                    run_id=run.id,
                    git_hash=run.git_hash,
                    command=run.command,
                    campaign_id=camp_id,
                    matched_files=matched_files,
                    reason=(
                        f"{touch}; git_hash {run.git_hash[:9]} "
                        f"predates fix boundary {boundary}"
                    ),
                )
            )
        else:
            unaffected_run_ids.append(run.id)

    if anchor_kind == "file":
        for path in dict.fromkeys(changed_files):
            if path not in matched_anywhere:
                warnings.append(
                    f"file anchor '{path}' matched no run's command/argv/sidecar/manifest "
                    "(or metadata). This does NOT mean no run depends on it: bathos "
                    "records no import closure or runtime reads, so an imported module "
                    "or a runtime-read input is invisible here. Zero matches is "
                    "unverified, not clean."
                )
    elif anchor_kind == "input_artifact" and art_path and not matched_anywhere and not affected and not unverifiable:
        warnings.append(
            f"input_artifact anchor '{anchor_value}' matched no run's command/argv/sidecar/"
            "manifest/metadata. bathos records no runtime file reads unless declared or "
            "logged, so zero matches is unverified, not clean."
        )
    elif anchor_kind == "sidecar" and not sidecar_is_sha and not affected and not unverifiable:
        warnings.append(
            f"sidecar anchor '{anchor_value}' matched no run's sidecar_path or sidecar_sha256. "
            "Zero matches is unverified if runs predated sidecar recording."
        )
    elif anchor_kind == "instrument" and all_runs and instrument_pkg_recorded_count == 0 and not affected:
        warnings.append(
            f"no run recorded dependency_provenance for '{inst_pkg}' ({inst_ver_key}); "
            f"if '{inst_pkg}' was not listed in [provenance] track_packages, zero matches "
            "is unverified, not clean."
        )

    for key in ctx.truncated:
        warnings.append(
            f"only the first 1 MiB of '{key}' was searched for file references; a "
            "reference past that point would be missed, so zero matches from it are "
            "unverified, not clean."
        )

    return BlastRadiusReport(
        anchor_kind=anchor_kind,
        anchor_value=anchor_value,
        changed_files=changed_files,
        affected=affected,
        unverifiable=unverifiable,
        unaffected_run_ids=unaffected_run_ids,
        warnings=warnings,
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
    """One unit of work for the whole batch: the mode is read once, so a cut-over
    can never split one assess into some legacy rows and some events (spec "Mode")."""
    from bathos.runlog.emit import unit_of_work

    with unit_of_work(Path(catalog_dir)):
        return _flag_blast_radius_impl(report, catalog_dir)


def _flag_blast_radius_impl(
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

    Debt #1475: opens at most ONE warm-tier DuckDB connection for this whole
    batch (used for both the from_state fold and the ledger insert of every
    record), instead of a fresh connection per record. Opened lazily on first
    use rather than up front, so the point at which bathos.db springs into
    existence (a `_connect()` side effect -- see the get_run() comment below)
    is unchanged from before this connection was shared.
    """
    from bathos.runlog.emit import current_mode, in_unit_of_work
    from bathos.runlog.mode import is_log_mode

    log_mode = current_mode() if in_unit_of_work() else is_log_mode(Path(catalog_dir))
    records: list[BlastRadiusRecord] = []
    batch_run_states: dict[str, str] = {}
    con: duckdb.DuckDBPyConnection | None = None
    try:
        for match, to_state in [
            *((m, "affected") for m in report.affected),
            *((m, "unverifiable") for m in report.unverifiable),
        ]:
            shadow_verdict_json = None
            if to_state == "affected":
                try:
                    run = get_run(match.run_id, Path(catalog_dir))
                except duckdb.Error:
                    run = None
                if run is not None:
                    shadow_verdict_json = json.dumps(compute_shadow_auto_clear_verdict(run))

            if log_mode:
                from_state = batch_run_states.get(match.run_id)
                if from_state is None:
                    from_state = fold_blast_radius_state(catalog_dir, "run", match.run_id)
                batch_run_states[match.run_id] = to_state
            else:
                if con is None:
                    con = _connect(catalog_dir)
                from_state = _fold_state_using_conn(con, "run", match.run_id)

            record = BlastRadiusRecord(
                entity_type="run",
                entity_id=match.run_id,
                to_state=to_state,
                from_state=from_state,
                anchor_kind=report.anchor_kind,
                anchor_value=report.anchor_value,
                matched_files=json.dumps(match.matched_files),
                shadow_verdict=shadow_verdict_json,
                match_reason=match.reason,
            )
            records.append(append_ledger_record(record, catalog_dir, con=con))
    finally:
        if con is not None:
            con.close()
    return records


def clear_blast_radius_flag(
    catalog_dir: Path | str,
    entity_type: str,
    entity_id: str,
    *,
    reason: str,
    anchor_kind: str | None = None,
    anchor_value: str | None = None,
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
        anchor_kind=anchor_kind,
        anchor_value=anchor_value,
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

    Debt #1488: fetches every run's claim_discriminates in ONE batched query
    (`WHERE id IN (...)`), not one query per (clause, run_id) pair -- the old
    per-pair query loop issued up to `len(clauses) * len(run_ids)` round trips
    for the same handful of rows.
    """
    if not run_ids:
        return []

    placeholders = ", ".join("?" for _ in run_ids)
    rows = db.execute(
        f"SELECT id, claim_discriminates FROM runs WHERE id IN ({placeholders})",
        list(run_ids),
    ).fetchall()

    disc_lists_by_run: dict[str, list] = {}
    for run_id, disc_json in rows:
        if not disc_json:
            continue
        try:
            disc_list = json.loads(disc_json)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(disc_list, list):
            disc_lists_by_run[run_id] = disc_list

    implicated: list[str] = []
    for clause in claim.union_gate_clauses:
        if clause.get("positive_control") is True:
            continue
        hypothesis_ids = clause.get("hypothesis_ids", [])
        clause_id = clause.get("id", "?")
        for disc_list in disc_lists_by_run.values():
            if all(h in disc_list for h in hypothesis_ids):
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


# Single canonical keyword list (spec Decision Log #2, backlog #4555). The installed
# post-commit hook's shell `case` pre-filter (cli.py's _SHADOW_HOOK_SCRIPT) is generated
# FROM this same tuple rather than hardcoding its own copy -- a prior version duplicated
# the list by hand in a shell glob, which silently drifted from this pattern (confirmed,
# PR #54 second jury round: the shell glob did unanchored substring matching -- e.g.
# "prefix"/"debug" -- while this word-boundary regex did not, and the shell filter alone
# decided whether to run at all, making this function dead code in production).
SHADOW_KEYWORDS = ("fix", "fixes", "fixed", "bug", "bugfix", "hotfix", "regression", "patch")

_FIX_LIKE_KEYWORD_PATTERN = re.compile(
    r"\b(" + "|".join(SHADOW_KEYWORDS) + r")\b", re.IGNORECASE
)


def identify_fix_like_keyword(commit_message: str) -> str | None:
    """Return the first canonical fix-like keyword found in `commit_message`
    (lowercased), or None if none match. The authoritative keyword decision --
    `record_shadow_trigger` uses this (not the shell's own coarser pre-filter)
    to decide whether to run at all (SAC-5) and to capture which keyword
    matched for display (SAC-8)."""
    match = _FIX_LIKE_KEYWORD_PATTERN.search(commit_message)
    return match.group(1).lower() if match else None


def matches_fix_like_keywords(commit_message: str) -> bool:
    """Shadow-trigger keyword filter (spec Decision Log #2, backlog #4555).

    Hardcoded pattern, not configurable via .bth.toml yet -- the user's own
    call: prove the trigger before building configurability.
    """
    return identify_fix_like_keyword(commit_message) is not None


def _commit_message(commit: str, project_root: Path | str) -> str:
    _reject_flag_like(commit)
    result = subprocess.run(
        ["git", "log", "-1", "--pretty=%B", commit],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError(f"git log -1 --pretty=%B {commit} failed: {result.stderr.strip()}")
    return result.stdout


def record_shadow_trigger(
    catalog_dir: Path | str, project_root: Path | str, commit: str
) -> BlastRadiusRecord | None:
    """SAC-5/SAC-6/SAC-7/SAC-8: run a shadow-only assessment for `commit` and log a
    single entity_type="shadow_trigger" record -- but only if `commit`'s own message
    matches the canonical keyword pattern (SAC-5: no assessment attempted at all,
    not even in the background, for a non-matching commit). The installed hook's
    shell pre-filter is a coarse, cheap gate to avoid spawning this command's
    process at all for most commits; this function makes the real, authoritative
    keyword decision and captures which keyword matched (SAC-8) regardless of what
    the shell filter decided.

    NEVER calls flag_blast_radius or either propagate_to_* function -- this
    can never durably affect a real run/campaign/claim's state (spec Decision
    Log #7). `to_state="shadow_only"` is a 4th state value distinct from
    affected/unverifiable/cleared, and `entity_id` is the commit sha rather
    than a run/campaign id, so this is a fully separate namespace within the
    same blast_radius_ledger table (proven collision-free by Phase 1's own
    test_composite_key_does_not_cross_entity_types).

    Returns the appended record, or None if the commit message doesn't match
    the keyword pattern, or if assess_blast_radius raised ValueError (e.g.
    `commit` has no parent -- the very first commit in a repo) -- a shadow
    trigger failing quietly is acceptable (spec pre-mortem: "a detached
    background process's own errors are invisible to the user at commit time
    by design").
    """
    try:
        message = _commit_message(commit, project_root)
    except ValueError:
        return None
    keyword = identify_fix_like_keyword(message)
    if keyword is None:
        return None

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
            f"keyword={keyword}; {len(report.affected)} affected, "
            f"{len(report.unverifiable)} unverifiable run(s) would have been flagged: "
            f"{[m.run_id for m in all_matches]}"
        ),
    )
    return append_ledger_record(record, catalog_dir)
