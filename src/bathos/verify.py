from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

import pyarrow.parquet as pq

from bathos.catalog import iter_run_fragments

if TYPE_CHECKING:
    from bathos.runlog.importer import ImportCandidate

logger = logging.getLogger(__name__)


@dataclass
class VerifyResult:
    """Result of a verify operation for one or more tiers."""

    tier: str  # "cool" | "warm" | "archive" | "all"
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def verify_cool(catalog_dir: Path) -> VerifyResult:
    """Verify cool-tier Parquet fragments.

    Checks:
    - Each run_*.parquet is readable by pyarrow
    - No .bak files are present (signals interrupted migration)
    - No .tmp files are present (signals interrupted write)
    - Row count in each fragment is >= 1
    """
    runs_dir = catalog_dir / "runs"
    errors = []
    warnings = []
    stats = {"fragments_checked": 0, "fragments_readable": 0}

    if not runs_dir.exists():
        return VerifyResult(tier="cool", ok=True, errors=[], warnings=[], stats=stats)

    # Check for .bak and .tmp files (signals of interrupted operations)
    bak_files = list(runs_dir.rglob("*.bak"))
    tmp_files = list(runs_dir.rglob("*.tmp.parquet"))

    for bak_file in bak_files:
        errors.append(f"Interrupted migration: backup file exists at {bak_file}")
        logger.error(f"Interrupted migration: {bak_file}")

    for tmp_file in tmp_files:
        errors.append(f"Interrupted write: temporary file exists at {tmp_file}")
        logger.error(f"Interrupted write: {tmp_file}")

    # Check each fragment (iter_run_fragments excludes .tmp.parquet sentinel markers)
    fragments = iter_run_fragments(runs_dir)
    for frag in fragments:
        stats["fragments_checked"] += 1
        try:
            tbl = pq.read_table(str(frag))
            if len(tbl) > 0:
                stats["fragments_readable"] += 1
            else:
                errors.append(f"Empty fragment: {frag}")
                logger.error(f"Empty fragment: {frag}")
        except Exception as e:
            errors.append(f"Unreadable fragment {frag}: {e}")
            logger.error(f"Unreadable fragment {frag}: {e}")

    return VerifyResult(
        tier="cool",
        ok=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        stats=stats,
    )


def verify_warm(catalog_dir: Path) -> VerifyResult:
    """Verify warm-tier DuckDB database.

    Checks:
    - bathos.db exists
    - duckdb.connect() succeeds without IOException (header check)
    - _schema_meta table is accessible (structural check)
    - If cool-tier fragments exist AND runs table is empty:
        warn "Cool fragments exist but runs table is empty — run bth compact"
    - If no cool-tier fragments exist AND runs table is empty:
        do NOT warn (empty runs table is normal for a new installation)
    """
    from bathos.compact import CorruptDatabaseError, _open_db

    db_path = catalog_dir / "bathos.db"
    errors = []
    warnings = []
    stats = {"db_exists": db_path.exists()}

    if not db_path.exists():
        errors.append("bathos.db not found — run 'bth compact' first")
        logger.error("bathos.db not found")
        return VerifyResult(
            tier="warm",
            ok=False,
            errors=errors,
            warnings=warnings,
            stats=stats,
        )

    # Try to open database (checks header and _schema_meta)
    try:
        con = _open_db(db_path)
        stats["db_valid"] = True
        try:
            row_count_row = con.execute("SELECT COUNT(*) FROM runs").fetchone()
            row_count = int(row_count_row[0]) if row_count_row else 0
            stats["runs_count"] = row_count
            tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
            if "campaign_runs" in tables and "campaigns" in tables:
                orphan_row = con.execute(
                    "SELECT COUNT(*) FROM campaign_runs cr "
                    "LEFT JOIN campaigns c ON cr.campaign_id = c.id "
                    "WHERE c.id IS NULL"
                ).fetchone()
                orphan_count = int(orphan_row[0]) if orphan_row else 0
                stats["orphaned_campaign_runs"] = int(orphan_count)
                if orphan_count:
                    errors.append(
                        f"Orphaned campaign_runs rows in bathos.db: {orphan_count} row(s) "
                        "reference missing campaigns"
                    )
        except Exception as e:
            errors.append(f"Could not query runs table: {e}")
            logger.error(f"Could not query runs table: {e}")
        finally:
            con.close()
    except CorruptDatabaseError as e:
        errors.append(f"Database integrity check failed: {e}")
        logger.error(f"Database integrity check failed: {e}")
        return VerifyResult(
            tier="warm",
            ok=False,
            errors=errors,
            warnings=warnings,
            stats=stats,
        )
    except Exception as e:
        errors.append(f"Could not open database: {e}")
        logger.error(f"Could not open database: {e}")
        return VerifyResult(
            tier="warm",
            ok=False,
            errors=errors,
            warnings=warnings,
            stats=stats,
        )

    # Check if cool fragments exist but runs table is empty
    runs_dir = catalog_dir / "runs"
    cool_fragments = iter_run_fragments(runs_dir) if runs_dir.exists() else []

    if cool_fragments and stats.get("runs_count", 0) == 0:
        warnings.append("Cool fragments exist but runs table is empty — run bth compact")
        logger.warning("Cool fragments exist but runs table is empty")

    return VerifyResult(
        tier="warm",
        ok=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        stats=stats,
    )


def verify_archive(archive_root: Path) -> VerifyResult:
    """Verify cold-tier archive Parquet files against manifest.json checksums.

    Checks:
    - manifest.json exists and is valid JSON
    - manifest has schema_version >= "2" (contains sha256 checksums)
    - For each manifest entry: Parquet file exists, SHA256 matches, row count matches
    - Warns (does not error) on manifests with schema_version < "2"
    """
    manifest_path = archive_root / "manifest.json"
    errors = []
    warnings = []
    stats = {"manifest_exists": manifest_path.exists()}

    if not manifest_path.exists():
        return VerifyResult(
            tier="archive",
            ok=True,
            errors=[],
            warnings=["No archive found"],
            stats=stats,
        )

    try:
        with open(manifest_path) as f:
            manifest = json.load(f)
    except Exception as e:
        errors.append(f"Could not read manifest.json: {e}")
        logger.error(f"Could not read manifest.json: {e}")
        return VerifyResult(
            tier="archive",
            ok=False,
            errors=errors,
            warnings=warnings,
            stats=stats,
        )

    schema_version = manifest.get("schema_version", "1")
    stats["manifest_schema_version"] = schema_version

    # Check schema version
    if schema_version < "2":
        warnings.append(
            f"Manifest schema version {schema_version} does not include checksums — "
            f"run 'bth archive' to upgrade"
        )
        logger.warning(f"Manifest schema version {schema_version} is old")

    # If no entries or old schema, just warn/return
    entries = manifest.get("entries", [])
    if not entries:
        return VerifyResult(
            tier="archive",
            ok=True,
            errors=[],
            warnings=warnings,
            stats=stats,
        )

    # Check each entry
    stats["entries_checked"] = len(entries)
    stats["entries_valid"] = 0

    for entry in entries:
        partition = entry.get("partition")
        expected_rows = entry.get("rows")
        expected_sha256 = entry.get("sha256", "")

        if not partition:
            continue

        # Reconstruct path
        parquet_path = archive_root / partition / "runs.parquet"

        if not parquet_path.exists():
            errors.append(f"Missing archived Parquet: {partition}/runs.parquet")
            logger.error(f"Missing archived Parquet: {partition}/runs.parquet")
            continue

        try:
            tbl = pq.read_table(str(parquet_path))
            actual_rows = len(tbl)

            if actual_rows != expected_rows:
                errors.append(
                    f"Row count mismatch in {partition}: expected {expected_rows}, got {actual_rows}"
                )
                logger.error(
                    f"Row count mismatch in {partition}: expected {expected_rows}, got {actual_rows}"
                )
                continue

            # If schema_version >= 2, check SHA256
            if schema_version >= "2" and expected_sha256:
                actual_sha256 = _sha256_file(parquet_path)
                if actual_sha256 != expected_sha256:
                    errors.append(
                        f"SHA256 mismatch in {partition}: "
                        f"expected {expected_sha256}, got {actual_sha256}"
                    )
                    logger.error(
                        f"SHA256 mismatch in {partition}: "
                        f"expected {expected_sha256}, got {actual_sha256}"
                    )
                    continue

            stats["entries_valid"] += 1
        except Exception as e:
            errors.append(f"Could not verify {partition}: {e}")
            logger.error(f"Could not verify {partition}: {e}")

    return VerifyResult(
        tier="archive",
        ok=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        stats=stats,
    )


def _sha256_file(path: Path) -> str:
    """Compute SHA256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _format_finding(finding: dict) -> str:
    """One structured finding -> one human-readable line, for the `errors`
    list every other tier here already uses (spec: "extend that, do not
    build a parallel one")."""
    detail = ", ".join(f"{k}={v!r}" for k, v in finding.items() if k != "type")
    return f"{finding['type']}: {detail}" if detail else str(finding["type"])


def _parse_iso_ts(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _folded_run_end_ts(folded_run: dict) -> datetime | None:
    """`timestamp + duration_s` (spec: "'end time' everywhere in this spec
    means `timestamp + duration_s`, there being no end-time column")."""
    ts = _parse_iso_ts(folded_run.get("timestamp"))
    if ts is None:
        return None
    try:
        duration = float(folded_run.get("duration_s") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    return ts + timedelta(seconds=duration)


def _duplicate_project_id_findings() -> list[dict]:
    """AC-24: "two roots sharing a `project_id`" (D7)."""
    from bathos.runlog.project_id import list_registered_roots, read_project_id

    by_id: dict[str, list[str]] = {}
    for root in list_registered_roots():
        if not root.exists():
            continue
        pid = read_project_id(root / ".bth.toml")
        if pid:
            by_id.setdefault(pid, []).append(str(root))
    return [
        {"type": "duplicate_project_id", "project_id": pid, "roots": sorted(roots)}
        for pid, roots in by_id.items()
        if len(roots) > 1
    ]


def _stale_project_id_findings() -> list[dict]:
    """#3508: detect stale `project_id` mismatches between `~/.bth/projects.toml`
    and `<root>/.bth.toml`."""
    from bathos.runlog.project_id import list_registered_entries, read_project_id

    findings: list[dict] = []
    for root, registry_pid in list_registered_entries():
        if not root.exists():
            continue
        toml_pid = read_project_id(root / ".bth.toml")
        if registry_pid != toml_pid:
            findings.append(
                {
                    "type": "stale_project_id",
                    "root": str(root),
                    "registry_project_id": registry_pid,
                    "toml_project_id": toml_pid,
                }
            )
    return findings


def _quarantine_findings(catalog_dir: Path) -> list[dict]:
    """AC-24: "a quarantined line" and "an `eid_conflict`" -- every row of
    the folded index's `quarantine` table, split by `reason` (spec Line
    envelope: an `eid_conflict` is one specific quarantine reason among
    others, e.g. `invalid_json`/a schema violation -- see `ingest.py`)."""
    from bathos.index import connect_read

    con = connect_read(catalog_dir)
    try:
        rows = con.execute(
            "SELECT file, byte_offset, reason, raw_line, detected_at FROM quarantine"
        ).fetchall()
    finally:
        con.close()

    findings = []
    for file, byte_offset, reason, raw_line, detected_at in rows:
        eid = None
        if raw_line:
            with contextlib.suppress(Exception):
                eid = json.loads(raw_line).get("eid")
        finding_type = "eid_conflict" if reason == "eid_conflict" else "quarantined_line"
        findings.append(
            {
                "type": finding_type,
                "file": file,
                "byte_offset": byte_offset,
                "reason": reason,
                "eid": eid,
                "detected_at": detected_at,
            }
        )
    return findings


def _mirror_dir_for_main_root(main_root: Path) -> Path:
    """The mirror directory D7 pairs with `main_root`'s project log (spec
    "Segments and writers": the same segment filename in both copies)."""
    from bathos.runlog.project_id import read_project_id
    from bathos.runlog.writer import mirror_dir_for

    pid = read_project_id(main_root / ".bth.toml")
    slug = None
    if pid is None:
        cfg_path = main_root / ".bth.toml"
        if cfg_path.is_file():
            with contextlib.suppress(Exception):
                from bathos.config import load_project_config

                slug = load_project_config(cfg_path).slug
    return mirror_dir_for(pid, slug)


def _watermark_findings(catalog_dir: Path) -> list[dict]:
    """AC-24: "a log file shrunk below or vanished past its watermark", and
    (spec D7 / AC-14) "a line present in neither the project log nor the
    mirror" -- the stronger case where a `project` root's shrunk/vanished
    file's own paired mirror copy (same segment filename) is ALSO short of
    the same watermark, so `bth log restore` could not recover it either.
    """
    from bathos.index import connect_read
    from bathos.runlog.ingest import discover_roots

    con = connect_read(catalog_dir)
    try:
        rows = con.execute(
            "SELECT root_kind, root_id, path, size, byte_offset FROM ingest_watermarks"
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return []

    root_dirs = {
        (kind, root_id): log_dir for kind, root_id, log_dir in discover_roots(catalog_dir)
    }

    findings: list[dict] = []
    for root_kind, root_id, path, _size, byte_offset in rows:
        base = root_dirs.get((root_kind, root_id))
        file_path = (base / path) if base is not None else None
        actual_size = file_path.stat().st_size if (file_path and file_path.exists()) else 0
        if actual_size >= byte_offset:
            continue
        findings.append(
            {
                "type": "log_watermark_shrunk",
                "root_kind": root_kind,
                "root_id": root_id,
                "path": path,
                "watermark_offset": byte_offset,
                "actual_size": actual_size,
            }
        )
        if root_kind != "project":
            continue
        mirror_dir = _mirror_dir_for_main_root(Path(root_id))
        mirror_path = mirror_dir / path
        mirror_size = mirror_path.stat().st_size if mirror_path.exists() else 0
        if mirror_size < byte_offset:
            findings.append(
                {
                    "type": "line_missing_from_both_copies",
                    "root_id": root_id,
                    "path": path,
                    "watermark_offset": byte_offset,
                }
            )
    return findings


_SEGMENT_RE = re.compile(r"^(?P<host>[^.]+)\.(?P<pid>\d+)\.(?P<start_ns>\d+)(?:\.slurm-.*)?\.jsonl$")

_UNTERMINATED_TAIL_STALE_DAYS = 7


def _parse_segment_name(name: str) -> tuple[str, int, int] | None:
    """`<host>.<pid>.<start_ns>[.slurm-...].jsonl` -> `(host, pid, start_ns)`,
    or None for a name that does not match the segment naming convention
    (spec "Segments and writers")."""
    m = _SEGMENT_RE.match(name)
    if not m:
        return None
    return m.group("host"), int(m.group("pid")), int(m.group("start_ns"))


def _unterminated_tail_findings(catalog_dir: Path) -> list[dict]:
    """AC-7 (verify half, delivery step 4): "An unterminated tail is never
    quarantined by ingest, and `bth verify` reports it once the writer has a
    later segment[,] or the file is unchanged for 7 days" (spec "Line
    envelope"). Ingest's own watermark stops at the last `\\n`
    (`ingest.py:_read_new_lines`), so a segment whose actual size exceeds
    its recorded watermark has trailing bytes past the last complete line --
    an unterminated tail, by construction, at every moment between two
    completed lines. It is reported as torn only once one of the two
    trigger conditions holds, distinguishing "still being written" from
    "abandoned mid-line": a later segment from the SAME writer (same host
    and pid, a higher `start_ns`) proves that writer rotated away and will
    never complete this line, and a 7-day-stale mtime is the same signal
    when no later segment exists to prove it directly.
    """
    from bathos.index import connect_read
    from bathos.runlog.ingest import discover_roots

    con = connect_read(catalog_dir)
    try:
        rows = con.execute(
            "SELECT root_kind, root_id, path, size, byte_offset FROM ingest_watermarks"
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return []

    root_dirs = {
        (kind, root_id): log_dir for kind, root_id, log_dir in discover_roots(catalog_dir)
    }
    now = datetime.now(UTC)
    siblings_by_dir: dict[Path, list[tuple[str, tuple[str, int, int] | None]]] = {}

    findings: list[dict] = []
    for root_kind, root_id, path, _size, byte_offset in rows:
        base = root_dirs.get((root_kind, root_id))
        file_path = (base / path) if base is not None else None
        if file_path is None or not file_path.exists():
            continue
        actual_size = file_path.stat().st_size
        if actual_size <= byte_offset:
            continue  # nothing past the last completed line

        later_segment = False
        parsed = _parse_segment_name(path)
        if parsed is not None and base is not None:
            host, pid, start_ns = parsed
            if base not in siblings_by_dir:
                siblings_by_dir[base] = [
                    (sib.name, _parse_segment_name(sib.name))
                    for sib in base.glob("*.jsonl")
                ]
            for sib_name, sib_parsed in siblings_by_dir[base]:
                if sib_name == path:
                    continue
                if (
                    sib_parsed
                    and sib_parsed[0] == host
                    and sib_parsed[1] == pid
                    and sib_parsed[2] > start_ns
                ):
                    later_segment = True
                    break

        mtime = datetime.fromtimestamp(file_path.stat().st_mtime, tz=UTC)
        stale = (now - mtime) >= timedelta(days=_UNTERMINATED_TAIL_STALE_DAYS)
        if not (later_segment or stale):
            continue

        findings.append(
            {
                "type": "unterminated_tail",
                "root_kind": root_kind,
                "root_id": root_id,
                "path": path,
                "watermark_offset": byte_offset,
                "actual_size": actual_size,
            }
        )
    return findings


def _prefetch_lookup_entity_keys(con, lookup, keys: list[list[str]]) -> None:
    """Batch-populate `lookup._keys` in ONE query over `events` (works on
    `connect_read`'s `events` view, which does not expose `rowid`)."""
    from bathos.runlog.ingest import _row_to_event

    wanted: dict[str, None] = {}
    for key in keys:
        k = json.dumps(key)
        if k not in lookup._keys:
            wanted[k] = None
    if not wanted:
        return
    wanted_list = list(wanted)
    rows = con.execute(
        "SELECT entity_key, eid, kind, entity, ts, project, project_id, writer, seq, "
        "main_root, worktree_root, origin, data FROM events "
        "WHERE entity_key IN (SELECT unnest(?::VARCHAR[]))",
        [wanted_list],
    ).fetchall()
    for k in wanted_list:
        lookup._keys[k] = []
    for r in rows:
        lookup._keys[r[0]].append(_row_to_event(r[1:]))


def _campaign_findings(catalog_dir: Path) -> list[dict]:
    """AC-24: "threshold mismatch" (BC-12) and "`evalue_changed_after_
    conclusion`" (spec "Fold rules": a member whose folded end time is later
    than its campaign's conclusion time). Recomputes each campaign's fold
    fresh via the same `_compute_campaign_fold` ingest itself uses -- the
    persisted `campaigns`/`campaign_runs` tables alone cannot distinguish "a
    real mismatch" from "no sequential members yet" (both leave
    `stopping_threshold` NULL), so this reads the one source of truth
    (`fold_campaign`'s own `_threshold_mismatch` flag) rather than
    re-deriving the condition a second, possibly-diverging way.

    Uses a single `connect_read` connection and a shared `_FoldLookup` with
    one batched entity-key prefetch so checking N campaigns is O(1)
    connections and avoids N+1 `_fetch_events_by_entity_key` queries.
    """
    from bathos.index import connect_read
    from bathos.runlog.fold_runs import fold_run
    from bathos.runlog.ingest import _compute_campaign_fold, _fold_lookup_scope, _FoldLookup

    con = connect_read(catalog_dir)
    try:
        campaign_ids = [r[0] for r in con.execute("SELECT id FROM campaigns").fetchall()]
        if not campaign_ids:
            return []

        lookup = _FoldLookup(con)
        keys_to_prefetch: list[list[str]] = [[cid] for cid in campaign_ids]
        seen_runs: set[str] = set()
        for cid in campaign_ids:
            for rid in lookup.campaign_member_run_ids(cid):
                if rid not in seen_runs:
                    seen_runs.add(rid)
                    keys_to_prefetch.append([rid])
        _prefetch_lookup_entity_keys(con, lookup, keys_to_prefetch)

        findings: list[dict] = []
        end_ts_cache: dict[str, datetime | None] = {}
        with _fold_lookup_scope(lookup):
            for campaign_id in campaign_ids:
                row, campaign_runs_rows = _compute_campaign_fold(con, campaign_id)

                if row.get("_threshold_mismatch"):
                    findings.append({"type": "threshold_mismatch", "campaign_id": campaign_id})

                concluded_at = row.get("concluded_at")
                concluded_dt = (
                    _parse_iso_ts(concluded_at) if row.get("status") == "concluded" else None
                )
                if concluded_dt is not None:
                    for member in campaign_runs_rows:
                        run_id = member["run_id"]
                        if run_id not in end_ts_cache:
                            member_events = lookup.events_for_entity_key([run_id])
                            end_ts_cache[run_id] = (
                                _folded_run_end_ts(fold_run(member_events))
                                if member_events
                                else None
                            )
                        end_ts = end_ts_cache[run_id]
                        if end_ts is not None and end_ts > concluded_dt:
                            findings.append(
                                {
                                    "type": "evalue_changed_after_conclusion",
                                    "campaign_id": campaign_id,
                                    "run_id": run_id,
                                }
                            )
        return findings
    finally:
        con.close()


def _orphaned_campaign_run_findings(catalog_dir: Path) -> list[dict]:
    """Detect orphaned and prefix-mismatched `campaign_runs` / phantom `campaigns` rows in `index.db`."""
    from bathos.index import connect_read, index_db_path

    if not index_db_path(catalog_dir).exists():
        return []

    con = connect_read(catalog_dir)
    try:
        valid_cids = [
            r[0]
            for r in con.execute(
                "SELECT id FROM campaigns WHERE mode IS NOT NULL"
            ).fetchall()
            if r[0]
        ]
        orphans = con.execute(
            "SELECT cr.campaign_id, cr.run_id FROM campaign_runs cr "
            "LEFT JOIN campaigns c ON cr.campaign_id = c.id "
            "WHERE c.id IS NULL OR c.mode IS NULL "
            "ORDER BY cr.campaign_id, cr.run_id"
        ).fetchall()
        phantom_cids = [
            r[0]
            for r in con.execute(
                "SELECT id FROM campaigns WHERE mode IS NULL ORDER BY id"
            ).fetchall()
            if r[0]
        ]
    finally:
        con.close()

    findings: list[dict] = []
    seen_cids: set[str] = set()
    for bad_cid, run_id in orphans:
        if bad_cid:
            seen_cids.add(bad_cid)
        matches = (
            [c for c in valid_cids if c.startswith(bad_cid)]
            if bad_cid and len(bad_cid) >= 4
            else []
        )
        if len(matches) == 1:
            findings.append(
                {
                    "type": "campaign_run_prefix_mismatch",
                    "campaign_id": bad_cid,
                    "resolved_campaign_id": matches[0],
                    "run_id": run_id,
                }
            )
        else:
            findings.append(
                {
                    "type": "orphaned_campaign_run",
                    "campaign_id": bad_cid,
                    "run_id": run_id,
                }
            )

    for phantom_cid in phantom_cids:
        if phantom_cid not in seen_cids:
            findings.append(
                {
                    "type": "orphaned_campaign_run",
                    "campaign_id": phantom_cid,
                    "run_id": None,
                }
            )
    return findings


def _output_copy_findings(catalog_dir: Path) -> list[dict]:
    """Fold parity for ``output_copies`` (output tiering spec 261023, section 5.3, "Index wiring"): refold every
    ``output_copy.*`` event in memory with the same fold ingest uses and compare with the table. A row missing
    from the index is the signature of an event kind that ingest stored but never folded (it advances the
    watermark past kinds it does not dispatch); an orphan row has no event behind it; a differing row means the
    fold and the table drifted apart."""
    from bathos.index import OUTPUT_COPIES_COLUMNS, connect_read
    from bathos.runlog.fold_output_copies import OUTPUT_COPY_KINDS, fold_output_copy
    from bathos.runlog.ingest import _fetch_events_by_kinds

    con = connect_read(catalog_dir)
    try:
        events = _fetch_events_by_kinds(con, OUTPUT_COPY_KINDS)
        cols = ", ".join(OUTPUT_COPIES_COLUMNS)
        actual = {
            r[0]: dict(zip(OUTPUT_COPIES_COLUMNS, r, strict=True))
            for r in con.execute(f"SELECT {cols} FROM output_copies").fetchall()  # noqa: S608
        }
    finally:
        con.close()

    by_entity: dict[tuple[str, ...], list[dict]] = {}
    for ev in events:
        by_entity.setdefault(tuple(ev.get("entity") or []), []).append(ev)
    expected: dict[str, dict] = {}
    for evs in by_entity.values():
        for row in fold_output_copy(evs):
            expected[row["copy_id"]] = row

    findings: list[dict] = []
    for copy_id in sorted(set(expected) | set(actual)):
        want, got = expected.get(copy_id), actual.get(copy_id)
        if got is None:
            detail = "missing_from_index"
        elif want is None:
            detail = "orphan_row"
        elif any(want.get(c) != got.get(c) for c in OUTPUT_COPIES_COLUMNS):
            detail = "differs_from_fold"
        else:
            continue
        run_id = (want or got or {}).get("run_id")
        findings.append(
            {
                "type": "output_copies_fold_mismatch",
                "run_id": run_id,
                "copy_id": copy_id,
                "detail": detail,
            }
        )
    return findings


_LEGACY_CACHE_FILENAME = ".verify_legacy_cache.json"
_LEGACY_CACHE_VERSION = 1
_MISMATCH_FETCH_BATCH_SIZE = 200


def _query_import_chains(con) -> dict[tuple[str, ...], tuple[int, str]]:
    """Query `(kind, *entity, source_class, source_locator) -> (max ordinal, source_sha256)`
    without transferring or deserializing full `data` JSON payloads."""
    rows = con.execute(
        "SELECT kind, entity, "
        "json_extract_string(data, '$.source_class'), "
        "json_extract_string(data, '$.source_locator'), "
        "COALESCE(TRY_CAST(json_extract(data, '$.snapshot') AS BIGINT), 0), "
        "COALESCE(json_extract_string(data, '$.source_sha256'), '') "
        "FROM events "
        "WHERE origin = 'migration' "
        "AND (kind LIKE '%.imported' OR kind = 'legacy_source.unreadable')"
    ).fetchall()
    chains: dict[tuple[str, ...], tuple[int, str]] = {}
    for kind, entity, source_class, source_locator, ordinal, sha in rows:
        ent = json.loads(entity) if isinstance(entity, str) else (entity or [])
        key = (kind or "", *ent, source_class or "", source_locator or "")
        current = chains.get(key)
        if current is None or ordinal > current[0]:
            chains[key] = (ordinal, sha or "")
    return chains


def _known_import_chains(catalog_dir: Path) -> dict[tuple[str, ...], tuple[int, str]]:
    """The already-ingested equivalent of `importer._existing_chains`
    (which reads staging JSONL files pre-cutover): `(kind, *entity,
    source_class, source_locator) -> (max ordinal, source_sha256)`, read
    from `idx.events` instead, so a post-cutover legacy write can be told
    apart from one the importer already knows about (spec "Legacy writes
    after cut-over" / AC-23 / Migration step 5)."""
    from bathos.index import connect_read

    con = connect_read(catalog_dir)
    try:
        return _query_import_chains(con)
    finally:
        con.close()


# Importer bookkeeping keys on a `*.imported` event's `data` (importer.py),
# stripped to recover the source's own fields.
_IMPORT_BOOKKEEPING = ("source_class", "source_locator", "canon", "snapshot", "source_sha256")


def _known_import_chains_and_fields(
    catalog_dir: Path,
) -> tuple[dict[tuple[str, ...], tuple[int, str]], dict[tuple[str, ...], dict]]:
    """`_known_import_chains`, plus each chain's latest-snapshot source fields
    (bookkeeping stripped), for telling a schema-only rewrite from a data change."""
    from bathos.index import connect_read
    from bathos.runlog.importer import _existing_chains

    con = connect_read(catalog_dir)
    try:
        rows = con.execute(
            "SELECT kind, entity, origin, data FROM events "
            "WHERE kind LIKE '%.imported' OR kind = 'legacy_source.unreadable'"
        ).fetchall()
    finally:
        con.close()
    events = [
        {
            "kind": kind,
            "entity": json.loads(entity) if isinstance(entity, str) else entity,
            "origin": origin,
            "data": json.loads(data) if isinstance(data, str) else data,
        }
        for kind, entity, origin, data in rows
    ]
    chains = _existing_chains(events)
    fields: dict[tuple[str, ...], dict] = {}
    for ev in events:
        data = ev["data"] or {}
        key = (
            ev["kind"],
            *(ev["entity"] or []),
            data.get("source_class", ""),
            data.get("source_locator", ""),
        )
        known = chains.get(key)
        if known is not None and (data.get("snapshot") or 0) == known[0]:
            fields[key] = {k: v for k, v in data.items() if k not in _IMPORT_BOOKKEEPING}
    return chains, fields


def _fetch_latest_import_fields(
    con,
    wanted_ordinals: dict[tuple[str, ...], int],
    entity_keys: list[list[str]],
) -> dict[tuple[str, ...], dict]:
    """Fetch bookkeeping-stripped `data` ONLY for the specific mismatched `entity_keys`."""
    if not wanted_ordinals or not entity_keys:
        return {}
    unique_keys = list({json.dumps(k): None for k in entity_keys}.keys())
    fields: dict[tuple[str, ...], dict] = {}
    for start in range(0, len(unique_keys), _MISMATCH_FETCH_BATCH_SIZE):
        batch = unique_keys[start : start + _MISMATCH_FETCH_BATCH_SIZE]
        rows = con.execute(
            "SELECT kind, entity, data FROM events "
            "WHERE origin = 'migration' "
            "AND entity_key IN (SELECT unnest(?::VARCHAR[])) "
            "AND (kind LIKE '%.imported' OR kind = 'legacy_source.unreadable')",
            [batch],
        ).fetchall()
        for kind, entity, raw_data in rows:
            ent = json.loads(entity) if isinstance(entity, str) else (entity or [])
            data = (json.loads(raw_data) if isinstance(raw_data, str) else raw_data) or {}
            key = (
                kind,
                *ent,
                data.get("source_class", ""),
                data.get("source_locator", ""),
            )
            want_ord = wanted_ordinals.get(key)
            if want_ord is not None and (data.get("snapshot") or 0) == want_ord:
                fields[key] = {k: v for k, v in data.items() if k not in _IMPORT_BOOKKEEPING}
    return fields


def _is_schema_only_rewrite(prior: dict, current: dict) -> bool:
    """True when `current` is `prior` re-written by a fragment schema upgrade
    (`bathos.migrate.migrate_catalog`): every prior field unchanged except
    `schema_version`, and every new column holding exactly the default the
    upgrade inserts. Compared in the importer's canonical JSON form, which is
    the form `prior` was stored in."""
    from bathos.migrate import _default_array
    from bathos.runlog.importer import _canon_bytes
    from bathos.schema import COOL_SCHEMA

    cur = json.loads(_canon_bytes(current))
    if set(prior) - set(cur):
        return False
    for k, v in prior.items():
        if k != "schema_version" and cur.get(k) != v:
            return False
    schema = {f.name: f.type for f in COOL_SCHEMA}
    for k in set(cur) - set(prior):
        if k not in schema:
            return False
        default = _default_array(schema[k], 1, k).to_pylist()[0]
        if cur[k] != json.loads(_canon_bytes({"v": default}))["v"]:
            return False
    return True


def _stat_sig(path: Path) -> list[int]:
    st = path.stat()
    return [st.st_size, st.st_mtime_ns, st.st_ctime_ns, st.st_ino]


def _load_legacy_verify_cache(catalog_dir: Path) -> dict:
    cache_path = catalog_dir / _LEGACY_CACHE_FILENAME
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
        if (
            isinstance(data, dict)
            and data.get("version") == _LEGACY_CACHE_VERSION
            and isinstance(data.get("warm"), dict)
            and isinstance(data.get("files"), dict)
        ):
            return data
    except Exception:
        pass
    return {"version": _LEGACY_CACHE_VERSION, "warm": {}, "files": {}}


def _save_legacy_verify_cache(catalog_dir: Path, new_cache: dict, old_cache: dict) -> None:
    if not catalog_dir.is_dir() or new_cache == old_cache:
        return
    cache_path = catalog_dir / _LEGACY_CACHE_FILENAME
    tmp_path = catalog_dir / f"{_LEGACY_CACHE_FILENAME}.tmp"
    with contextlib.suppress(OSError):
        tmp_path.write_text(json.dumps(new_cache, separators=(",", ":")), encoding="utf-8")
        tmp_path.replace(cache_path)


def _iter_legacy_file_readers(catalog_dir: Path):
    """Yield `(path, locator, read_fn)` across all legacy fragment/JSON directories
    using the exact discovery rules and candidate semantics of `bathos.runlog.importer`."""
    import uuid

    from bathos.runlog.envelope import NAMESPACE_BATHOS
    from bathos.runlog.importer import (
        ImportCandidate,
        _iter_parquet_rows,
        _parse_reverted_filename_ts,
        _run_ts,
    )

    def _parquet_reader(
        kind: str,
        source_class: str,
        id_field: str | None = None,
        ts_field: str | None = None,
        *,
        is_run: bool = False,
        is_submit: bool = False,
    ):
        def _read(path: Path, locator: str) -> list[ImportCandidate] | None:
            rows = _iter_parquet_rows(path)
            if rows is None:
                return None
            out: list[ImportCandidate] = []
            for row in rows:
                if is_submit:
                    submitted_at = row.get("submitted_at") or ""
                    submit_id = str(uuid.uuid5(NAMESPACE_BATHOS, f"submit:{locator}"))
                    out.append(
                        ImportCandidate(
                            "submit.imported",
                            [submit_id],
                            row,
                            submitted_at,
                            "submit_parquet",
                            locator,
                        )
                    )
                elif is_run:
                    run_id = row.get("id")
                    if run_id:
                        out.append(
                            ImportCandidate(
                                kind, [run_id], row, _run_ts(row), source_class, locator
                            )
                        )
                else:
                    assert id_field is not None
                    assert ts_field is not None
                    record_id = row.get(id_field)
                    if record_id:
                        out.append(
                            ImportCandidate(
                                kind,
                                [record_id],
                                row,
                                row.get(ts_field) or "",
                                source_class,
                                locator,
                            )
                        )
            return out

        return _read

    def _read_reaped_json(path: Path, locator: str) -> list[ImportCandidate] | None:
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(record, dict):
            return None
        run_id = record.get("run_id") or path.stem
        fields = dict(record)
        fields["status"] = "abandoned"
        ts = str(record.get("reaped_at") or "")
        return [ImportCandidate("run.imported", [run_id], fields, ts, "ledger_json", locator)]

    def _read_reverted_json(path: Path, locator: str) -> list[ImportCandidate] | None:
        try:
            record = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(record, dict):
            return None
        run_id = record.get("run_id") or ""
        reaped_at = str(record.get("reaped_at") or "")
        filename_ts = _parse_reverted_filename_ts(path.stem)
        reverted_at = max(reaped_at, filename_ts) if filename_ts else reaped_at
        fields = dict(record)
        fields["reverted_at"] = reverted_at
        return [
            ImportCandidate(
                "run_reap.imported", [run_id], fields, reaped_at, "ledger_reverted", locator
            )
        ]

    def _read_campaign_json(path: Path, locator: str) -> list[ImportCandidate] | None:
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or "id" not in payload:
            return []
        campaign_id = payload["id"]
        ts = payload.get("concluded_at") or payload.get("started_at") or ""
        return [
            ImportCandidate("campaign.imported", [campaign_id], payload, ts, "campaign_json", locator)
        ]

    runs_dir = catalog_dir / "runs"
    if runs_dir.is_dir():
        reader = _parquet_reader("run.imported", "fragment", is_run=True)
        for f in sorted(runs_dir.rglob("run_*.parquet")):
            if not f.name.endswith(".tmp.parquet"):
                yield f, str(f.relative_to(catalog_dir)), reader

    remote_dir = catalog_dir / "remote-runs"
    if remote_dir.is_dir():
        reader = _parquet_reader("run.imported", "fragment_remote", is_run=True)
        for f in sorted(remote_dir.rglob("run_*.parquet")):
            if not f.name.endswith(".tmp.parquet"):
                yield f, str(f.relative_to(catalog_dir)), reader

    reaped_dir = catalog_dir / "reaped"
    if reaped_dir.is_dir():
        for project_dir in sorted(reaped_dir.iterdir()):
            if not project_dir.is_dir() or project_dir.name == "reverted":
                continue
            for f in sorted(project_dir.glob("*.json")):
                if f.parent.name != "reverted":
                    yield f, str(f.relative_to(catalog_dir)), _read_reaped_json
            reverted_dir = project_dir / "reverted"
            if reverted_dir.is_dir():
                for f in sorted(reverted_dir.glob("*.json")):
                    yield f, str(f.relative_to(catalog_dir)), _read_reverted_json

    submits_dir = catalog_dir / "submits"
    if submits_dir.is_dir():
        reader = _parquet_reader("submit.imported", "submit_parquet", is_submit=True)
        for f in sorted(submits_dir.rglob("*_submit.parquet")):
            if not f.name.endswith(".tmp"):
                yield f, str(f.relative_to(catalog_dir)), reader

    campaigns_dir = catalog_dir / "campaigns"
    if campaigns_dir.is_dir():
        for f in sorted(campaigns_dir.glob("*.json")):
            yield f, str(f.relative_to(catalog_dir)), _read_campaign_json

    for dirname, glob, kind, id_field, ts_field in (
        ("ledger", "ledger_*.parquet", "trust_ledger.imported", "id", "amended_at"),
        ("archived_items", "archived_*.parquet", "archived_item.imported", "record_id", "recorded_at"),
        ("blast_radius", "blast_radius_*.parquet", "blast_radius.imported", "id", "amended_at"),
    ):
        frag_dir = catalog_dir / dirname
        if frag_dir.is_dir():
            reader = _parquet_reader(kind, "fragment", id_field=id_field, ts_field=ts_field)
            for f in sorted(frag_dir.glob(glob)):
                if not f.name.endswith(".tmp.parquet"):
                    yield f, str(f.relative_to(catalog_dir)), reader


def _legacy_findings(catalog_dir: Path) -> list[dict]:
    """AC-24: "a legacy write after cut-over", "a `corrupt_legacy_source`",
    and "a `legacy_db_locked`" -- reuses the importer's own candidate builders
    and `connect_legacy` rather than re-implementing legacy-source semantics a
    second time (spec: "the same test the importer uses, so verify and
    importer never disagree"). Only meaningful post-cutover (spec Migration
    step 5: "an older bathos ... writing after cut-over"); before cut-over, an
    unimported legacy source is simply not yet migrated, not an anomaly.

    Streams per-table / per-file, extracts only chain metadata from `events`
    (fetching full `data` only for mismatched chains), and caches verified-clean
    stat signatures in `.verify_legacy_cache.json` to avoid rescanning frozen
    legacy sources on subsequent runs.
    """
    from bathos.index import connect_legacy, connect_read
    from bathos.runlog.importer import (
        _read_table_rows,
        _source_sha256,
        _warm_anchor_candidates,
        _warm_archived_item_candidates,
        _warm_blast_radius_candidates,
        _warm_campaign_candidates,
        _warm_campaign_run_candidates,
        _warm_edge_candidates,
        _warm_run_candidates,
        _warm_sources,
        _warm_trust_ledger_candidates,
    )

    con_idx = connect_read(catalog_dir)
    try:
        known_chains = _query_import_chains(con_idx)

        findings: list[dict] = []
        mismatched_batch: list[tuple[tuple[str, ...], ImportCandidate, int]] = []

        def _flush_mismatched() -> None:
            if not mismatched_batch:
                return
            wanted = {ck: ord_ for ck, _cand, ord_ in mismatched_batch}
            entity_keys = [cand.entity for _ck, cand, _ord in mismatched_batch]
            fields_by_key = _fetch_latest_import_fields(con_idx, wanted, entity_keys)
            for chain_key, cand, _ord in mismatched_batch:
                prior = fields_by_key.get(chain_key)
                if prior is not None and _is_schema_only_rewrite(prior, cand.fields):
                    findings.append(
                        {
                            "type": "legacy_schema_upgrade_after_cutover",
                            "kind": cand.kind,
                            "entity": cand.entity,
                            "source_locator": cand.source_locator,
                        }
                    )
                else:
                    findings.append(
                        {
                            "type": "legacy_write_after_cutover",
                            "kind": cand.kind,
                            "entity": cand.entity,
                            "source_locator": cand.source_locator,
                            "writing_host": cand.fields.get("hostname") or None,
                        }
                    )
            mismatched_batch.clear()

        def _check_candidate(cand) -> tuple[bool, str]:
            sha = _source_sha256(cand.fields)
            chain_key = (cand.kind, *cand.entity, cand.source_class, cand.source_locator)
            known = known_chains.get(chain_key)
            if known is not None and known[1] == sha:
                return True, sha
            if known is not None and known[1] != sha:
                mismatched_batch.append((chain_key, cand, known[0]))
                if len(mismatched_batch) >= _MISMATCH_FETCH_BATCH_SIZE:
                    _flush_mismatched()
                return False, sha
            findings.append(
                {
                    "type": "legacy_write_after_cutover",
                    "kind": cand.kind,
                    "entity": cand.entity,
                    "source_locator": cand.source_locator,
                    "writing_host": cand.fields.get("hostname") or None,
                }
            )
            return False, sha

        old_cache = _load_legacy_verify_cache(catalog_dir)
        new_cache: dict = {"version": _LEGACY_CACHE_VERSION, "warm": {}, "files": {}}

        warm_builders = (
            ("runs", _warm_run_candidates),
            ("campaigns", _warm_campaign_candidates),
            ("campaign_runs", _warm_campaign_run_candidates),
            (
                "campaign_edges",
                lambda rows, sc, lp: _warm_edge_candidates(
                    rows, sc, lp, table="campaign_edges", etype="campaign"
                ),
            ),
            (
                "run_edges",
                lambda rows, sc, lp: _warm_edge_candidates(
                    rows, sc, lp, table="run_edges", etype="run"
                ),
            ),
            ("sidecar_anchors", _warm_anchor_candidates),
            ("blast_radius_ledger", _warm_blast_radius_candidates),
            ("trust_ledger", _warm_trust_ledger_candidates),
            ("archived_items", _warm_archived_item_candidates),
        )

        for path, source_class, locator_prefix in _warm_sources(catalog_dir):
            opened = connect_legacy(path)
            if isinstance(opened, dict):
                status = opened.get("status")
                if status == "legacy_db_locked":
                    findings.append({"type": "legacy_db_locked", "path": str(path)})
                elif status == "corrupt_legacy_source":
                    findings.append(
                        {"type": "corrupt_legacy_source", "source_locator": locator_prefix}
                    )
                continue

            con_legacy = opened
            try:
                stat_sig = _stat_sig(path)
                cached_warm = old_cache["warm"].get(locator_prefix)
                if (
                    isinstance(cached_warm, dict)
                    and cached_warm.get("stat") == stat_sig
                    and isinstance(cached_warm.get("chains"), list)
                    and all(
                        (known := known_chains.get((k, *ent, source_class, loc))) is not None
                        and known[1] == sha
                        for k, ent, loc, sha in cached_warm["chains"]
                    )
                ):
                    new_cache["warm"][locator_prefix] = cached_warm
                    continue

                warm_chains: list[list] = []
                all_clean = True
                for table, builder in warm_builders:
                    for cand in builder(
                        _read_table_rows(con_legacy, table), source_class, locator_prefix
                    ):
                        clean, sha = _check_candidate(cand)
                        if clean:
                            warm_chains.append([cand.kind, cand.entity, cand.source_locator, sha])
                        else:
                            all_clean = False
                if all_clean:
                    new_cache["warm"][locator_prefix] = {"stat": stat_sig, "chains": warm_chains}
            finally:
                con_legacy.close()

        for path, locator, reader in _iter_legacy_file_readers(catalog_dir):
            try:
                stat_sig = _stat_sig(path)
            except OSError:
                continue

            cached_file = old_cache["files"].get(locator)
            if (
                isinstance(cached_file, dict)
                and cached_file.get("stat") == stat_sig
                and isinstance(cached_file.get("chains"), list)
                and all(
                    (known := known_chains.get((k, *ent, sc, locator))) is not None
                    and known[1] == sha
                    for k, ent, sc, sha in cached_file["chains"]
                )
            ):
                new_cache["files"][locator] = cached_file
                continue

            cands = reader(path, locator)
            if cands is None:
                findings.append({"type": "corrupt_legacy_source", "source_locator": locator})
                continue

            file_chains: list[list] = []
            all_clean = True
            for cand in cands:
                clean, sha = _check_candidate(cand)
                if clean:
                    file_chains.append([cand.kind, cand.entity, cand.source_class, sha])
                else:
                    all_clean = False
            if all_clean:
                new_cache["files"][locator] = {"stat": stat_sig, "chains": file_chains}

        _flush_mismatched()
        _save_legacy_verify_cache(catalog_dir, new_cache, old_cache)
        return findings
    finally:
        con_idx.close()


def verify_runlog(catalog_dir: Path) -> VerifyResult:
    """`bth verify`'s project-local-run-log checks (spec delivery step 4,
    "`bth verify` checks"; AC-7's verify half, AC-24, and the reap/revert
    semantics of AC-26 via `bathos.reap`).

    Extends the existing per-tier `VerifyResult` shape rather than building
    a parallel report format: `errors` gets one human-readable line per
    finding (same convention as `verify_cool`/`verify_warm`/`verify_archive`
    above), and `stats["findings"]` additionally carries the same findings
    as structured dicts (`{"type": "...", ...}`) for a caller that wants to
    branch on `type` rather than parse a message string.

    `duplicate_project_id` runs unconditionally (a registry-level check, not
    an index one); every other check depends on the folded index /
    `events`/`quarantine` tables, which do not exist in any meaningful sense
    before cut-over (`connect_read`'s flag-off branch is a plain pass-
    through to `bathos.db`, per spec "Reads": "G3 holds only from cut-over")
    -- so this returns just the registry check, clean otherwise, before
    cut-over rather than reading `bathos.db` a second time here (that is
    `verify_warm`'s job).
    """
    from bathos.runlog.mode import is_log_mode

    findings: list[dict] = []
    findings.extend(_duplicate_project_id_findings())
    findings.extend(_stale_project_id_findings())

    if is_log_mode(catalog_dir):
        findings.extend(_quarantine_findings(catalog_dir))
        findings.extend(_watermark_findings(catalog_dir))
        findings.extend(_unterminated_tail_findings(catalog_dir))
        findings.extend(_campaign_findings(catalog_dir))
        findings.extend(_orphaned_campaign_run_findings(catalog_dir))
        findings.extend(_output_copy_findings(catalog_dir))
        findings.extend(_legacy_findings(catalog_dir))

    advisory = [f for f in findings if f["type"] in _ADVISORY_FINDINGS]
    blocking = [f for f in findings if f["type"] not in _ADVISORY_FINDINGS]
    warnings: list[str] = []
    if advisory:
        # One line, not thousands: a single schema pass rewrites every fragment.
        warnings.append(
            f"legacy_schema_upgrade_after_cutover: {len(advisory)} frozen cool fragment(s) were "
            "rewritten by a fragment schema upgrade after cut-over; no folded value changed "
            "(details in stats['findings'])"
        )
    return VerifyResult(
        tier="runlog",
        ok=len(blocking) == 0,
        errors=[_format_finding(f) for f in blocking],
        warnings=warnings,
        stats={"findings": findings, "finding_count": len(findings)},
    )


# Finding types that are reported but do not fail the runlog tier.
_ADVISORY_FINDINGS = frozenset({"legacy_schema_upgrade_after_cutover"})


def verify_all(catalog_dir: Path, archive_root: Path | None = None) -> list[VerifyResult]:
    """Run verify_cool, verify_warm, verify_archive, and verify_runlog; return all results."""
    if archive_root is None:
        archive_root = Path.home() / ".bth" / "archive"

    return [
        verify_cool(catalog_dir),
        verify_warm(catalog_dir),
        verify_archive(archive_root),
        verify_runlog(catalog_dir),
    ]
