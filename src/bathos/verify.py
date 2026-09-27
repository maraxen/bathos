from __future__ import annotations

import contextlib
import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import pyarrow.parquet as pq

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

    # Check each fragment (exclude .tmp.parquet files which are sentinel markers)
    fragments = [f for f in runs_dir.rglob("run_*.parquet") if not f.name.endswith(".tmp.parquet")]
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
            row_count = con.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
            stats["runs_count"] = row_count
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
    cool_fragments = list(runs_dir.rglob("run_*.parquet")) if runs_dir.exists() else []

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

    root_dirs = {(kind, root_id): log_dir for kind, root_id, log_dir in discover_roots()}

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


def _campaign_findings(catalog_dir: Path) -> list[dict]:
    """AC-24: "threshold mismatch" (BC-12) and "`evalue_changed_after_
    conclusion`" (spec "Fold rules": a member whose folded end time is later
    than its campaign's conclusion time). Recomputes each campaign's fold
    fresh via the same `_compute_campaign_fold` ingest itself uses -- the
    persisted `campaigns`/`campaign_runs` tables alone cannot distinguish "a
    real mismatch" from "no sequential members yet" (both leave
    `stopping_threshold` NULL), so this reads the one source of truth
    (`fold_campaign`'s own `_threshold_mismatch` flag) rather than
    re-deriving the condition a second, possibly-diverging way."""
    from bathos.index import connect_read
    from bathos.runlog.fold_runs import fold_run
    from bathos.runlog.ingest import _compute_campaign_fold, _fetch_events_by_entity_key

    con = connect_read(catalog_dir)
    try:
        campaign_ids = [r[0] for r in con.execute("SELECT id FROM campaigns").fetchall()]
    finally:
        con.close()

    findings: list[dict] = []
    for campaign_id in campaign_ids:
        con = connect_read(catalog_dir)
        try:
            row, campaign_runs_rows = _compute_campaign_fold(con, campaign_id)

            if row.get("_threshold_mismatch"):
                findings.append({"type": "threshold_mismatch", "campaign_id": campaign_id})

            concluded_at = row.get("concluded_at")
            concluded_dt = _parse_iso_ts(concluded_at) if row.get("status") == "concluded" else None
            if concluded_dt is not None:
                for member in campaign_runs_rows:
                    run_id = member["run_id"]
                    member_events = _fetch_events_by_entity_key(con, [run_id])
                    if not member_events:
                        continue
                    end_ts = _folded_run_end_ts(fold_run(member_events))
                    if end_ts is not None and end_ts > concluded_dt:
                        findings.append(
                            {
                                "type": "evalue_changed_after_conclusion",
                                "campaign_id": campaign_id,
                                "run_id": run_id,
                            }
                        )
        finally:
            con.close()
    return findings


def _known_import_chains(catalog_dir: Path) -> dict[tuple[str, ...], tuple[int, str]]:
    """The already-ingested equivalent of `importer._existing_chains`
    (which reads staging JSONL files pre-cutover): `(kind, *entity,
    source_class, source_locator) -> (max ordinal, source_sha256)`, read
    from `idx.events` instead, so a post-cutover legacy write can be told
    apart from one the importer already knows about (spec "Legacy writes
    after cut-over" / AC-23 / Migration step 5)."""
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
    return _existing_chains(events)


def _legacy_findings(catalog_dir: Path) -> list[dict]:
    """AC-24: "a legacy write after cut-over", "a `corrupt_legacy_source`",
    and "a `legacy_db_locked`" -- reuses the importer's own candidate scan
    (`_all_candidates`, `connect_legacy`) rather than re-implementing legacy-
    source discovery a second time (spec: "the same test the importer uses,
    so verify and importer never disagree"). Only meaningful post-cutover
    (spec Migration step 5: "an older bathos ... writing after cut-over");
    before cut-over, an unimported legacy source is simply not yet migrated,
    not an anomaly.
    """
    from bathos.runlog.importer import ImportReport, _all_candidates, _source_sha256

    report = ImportReport()
    candidates = _all_candidates(catalog_dir, report)

    findings: list[dict] = []
    for path in report.locked:
        findings.append({"type": "legacy_db_locked", "path": path})
    for locator in report.unreadable:
        findings.append({"type": "corrupt_legacy_source", "source_locator": locator})

    known_chains = _known_import_chains(catalog_dir)
    for cand in candidates:
        if cand.kind == "legacy_source.unreadable":
            continue  # already reported above via report.unreadable
        sha = _source_sha256(cand.fields)
        chain_key = (cand.kind, *cand.entity, cand.source_class, cand.source_locator)
        known = known_chains.get(chain_key)
        if known is None or known[1] != sha:
            findings.append(
                {
                    "type": "legacy_write_after_cutover",
                    "kind": cand.kind,
                    "entity": cand.entity,
                    "source_locator": cand.source_locator,
                    "writing_host": cand.fields.get("hostname") or None,
                }
            )
    return findings


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

    if is_log_mode(catalog_dir):
        findings.extend(_quarantine_findings(catalog_dir))
        findings.extend(_watermark_findings(catalog_dir))
        findings.extend(_campaign_findings(catalog_dir))
        findings.extend(_legacy_findings(catalog_dir))

    return VerifyResult(
        tier="runlog",
        ok=len(findings) == 0,
        errors=[_format_finding(f) for f in findings],
        warnings=[],
        stats={"findings": findings, "finding_count": len(findings)},
    )


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
