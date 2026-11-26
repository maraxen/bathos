"""Canonical verifiable run bundles (`bathos.bundle.v1`) for cross-host sync & offline import.

Implements spec 261125 (§3, §4, §6):
- `export_run_bundle`: resolves run prefix (optionally scoped by project), collects full causal
  closure across all 11 event families (plus optional `--with-campaign`), synthesizes events on
  legacy pre-cutover catalogs, verifies raw sidecar bytes against `sidecar_sha256`, optionally
  embeds verified output blobs (512 MiB default cap) and `git.bundle`, computes RFC 8785-style
  canonical JSON digests, and writes `.tar.zst` (`.bthbundle`) atomically.
- `verify_run_bundle`: performs full in-memory container security, bomb-cap, canonical digest,
  sidecar, output manifest, and blob verification before touching disk.
- `import_run_bundle`: runs `verify_run_bundle`, enforces strict equivocation checks against the
  local catalog, resolves local checkout/sidecar paths by SHA-256, optionally extracts outputs
  safely (symlink-free parents + no-clobber default), writes immutable `.jsonl` + `.receipt.json`
  + cached sidecar atomically, and triggers `run_ingest()`.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.metadata
import io
import json
import os
import re
import stat
import subprocess
import tarfile
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

import duckdb
import zstandard

from bathos.config import default_catalog_dir, find_project_config, load_project_config
from bathos.errors import (
    RunBundleInvalidError,
    RunBundleTamperedError,
    RunBundleUnsafePathError,
    RunImportConflictError,
)
from bathos.index import connect_legacy, connect_read, index_db_path
from bathos.output_tiers.reference import (
    PHASE_RUN_END,
    RUN_NOT_FOUND,
    OutputTierError,
    reference_hashes,
)
from bathos.query import AmbiguousRunId, _escape_like, get_run, run_source
from bathos.runlog.emit import run_event_data
from bathos.runlog.envelope import (
    NAMESPACE_BATHOS,
    build_envelope,
    canonical_json_bytes,
    compute_envelope_sha256,
)
from bathos.runlog.fold_runs import fold_run
from bathos.runlog.ingest import (
    _EVENT_SELECT,
    _RUN_KINDS,
    _row_to_event,
    _validate_envelope,
    ingest_after_write,
    run_ingest,
)
from bathos.runlog.mode import is_log_mode
from bathos.runlog.project_id import (
    list_registered_entries,
    read_project_id,
    register_main_root,
)
from bathos.runlog.resolve import resolve_log_root

BUNDLE_FORMAT = "bathos.bundle.v1"
BUNDLE_VERSION = 1
DEFAULT_MAX_OUTPUT_BYTES = 512 * 1024 * 1024  # 512 MiB; 0 = unlimited
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_EVENTS_BYTES = 64 * 1024 * 1024
MAX_SIDECAR_BYTES = 4 * 1024 * 1024
MAX_GIT_BUNDLE_BYTES = 256 * 1024 * 1024
MAX_MEMBER_COUNT = 50_000
READ_CHUNK_BYTES = 1 << 20

ALLOWED_TOP_LEVEL_FILES = frozenset(
    {"manifest.json", "events.jsonl", "sidecar.bth.toml", "git.bundle"}
)
OUTPUT_BLOB_PREFIX = "blobs/outputs/"

_CAMPAIGN_EVENT_KINDS = frozenset(
    {
        "campaign.created",
        "campaign.claim_bound",
        "campaign.threshold_set",
        "campaign.claim_bypassed",
        "campaign.concluded",
        "campaign.imported",
    }
)
_SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _bth_version() -> str:
    try:
        return importlib.metadata.version("bathos")
    except Exception:
        return "0.18.0"


def _now_utc_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(READ_CHUNK_BYTES):
            h.update(chunk)
    return h.hexdigest()


def _reject_json_constant(value: str) -> Any:
    raise RunBundleInvalidError(f"Non-standard JSON constant {value!r} is disallowed")


def _loads_strict(raw: bytes | str) -> Any:
    try:
        text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
    except UnicodeDecodeError as exc:
        raise RunBundleInvalidError(f"Invalid UTF-8 in bundle JSON: {exc}") from exc
    try:
        return json.loads(text, parse_constant=_reject_json_constant)
    except json.JSONDecodeError as exc:
        raise RunBundleInvalidError(f"Malformed JSON in bundle: {exc}") from exc


def _canonical_bytes_strict(obj: Any) -> bytes:
    try:
        return canonical_json_bytes(obj)
    except (TypeError, ValueError) as exc:
        raise RunBundleInvalidError(f"Non-canonical or non-finite JSON value: {exc}") from exc


def _compute_digest_strict(ev: dict[str, Any]) -> str:
    try:
        return compute_envelope_sha256(ev)
    except (TypeError, ValueError) as exc:
        raise RunBundleInvalidError(f"Event envelope cannot be canonicalized: {exc}") from exc


def _normalize_hostname(host: str | None) -> str:
    if not host:
        return ""
    return host.strip().split(".")[0].lower()


def _sanitize_path_segment(value: str | None, fallback: str) -> str:
    raw = (value or "").strip()
    if not raw or raw in (".", "..") or "/" in raw or "\\" in raw or "\x00" in raw:
        return fallback
    if _SAFE_SEGMENT_RE.fullmatch(raw):
        return raw
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", raw).strip(".")
    return cleaned or fallback


def _validate_safe_run_id(run_id: str) -> str:
    if (
        not isinstance(run_id, str)
        or not run_id
        or ".." in run_id
        or "/" in run_id
        or "\\" in run_id
        or "\x00" in run_id
        or not _SAFE_RUN_ID_RE.fullmatch(run_id)
    ):
        raise RunBundleUnsafePathError(f"Unsafe run_id: {run_id!r}")
    return run_id


def _validate_safe_sha256(sha: str, *, label: str = "sidecar_sha256") -> str:
    if not isinstance(sha, str) or not _SAFE_SHA256_RE.fullmatch(sha):
        raise RunBundleUnsafePathError(f"Unsafe {label}: {sha!r}")
    return sha


def _probe_sidecar_bytes(
    *,
    sidecar_path: str,
    sidecar_sha256: str,
    run_id: str,
    catalog_dir: Path,
    worktree_root: str | None,
    main_root: str | None,
    manifest_path: str = "",
    allow_raw_sidecar_path: bool = True,
) -> tuple[bytes | None, Path | None]:
    """Locate raw sidecar TOML bytes whose SHA-256 matches ``sidecar_sha256``."""
    if not sidecar_sha256:
        return None, None

    candidates: list[Path] = []
    if sidecar_path:
        sp = Path(sidecar_path)
        for root_str in (worktree_root, main_root):
            if not root_str:
                continue
            root = Path(root_str)
            if not sp.is_absolute():
                candidates.append(root / sp)
            else:
                for idx in range(1, len(sp.parts)):
                    candidates.append(root.joinpath(*sp.parts[idx:]))
        if allow_raw_sidecar_path and sp.is_absolute():
            candidates.append(sp)

    if _SAFE_SHA256_RE.fullmatch(sidecar_sha256):
        candidates.extend(
            [
                Path.home() / ".bth" / "sidecar-cache" / sidecar_sha256[:2] / f"{sidecar_sha256}.bth.toml",
                Path.home() / ".bth" / "sidecar-cache" / f"{sidecar_sha256}.bth.toml",
                catalog_dir / "sidecars" / "snapshots" / f"{sidecar_sha256}.bth.toml",
            ]
        )
        if _SAFE_RUN_ID_RE.fullmatch(run_id) and ".." not in run_id:
            candidates.extend(
                [
                    catalog_dir / "imported_sidecars" / run_id / f"{sidecar_sha256}.bth.toml",
                    catalog_dir / "sidecars" / "snapshots" / f"{run_id}.bth.toml",
                ]
            )

    seen: set[str] = set()
    for cand in candidates:
        key = str(cand)
        if key in seen:
            continue
        seen.add(key)
        try:
            if cand.is_file():
                data = cand.read_bytes()
                if _sha256_bytes(data) == sidecar_sha256:
                    return data, cand.resolve()
        except OSError:
            continue

    if manifest_path:
        try:
            from bathos.lock_ref import locate_lock_bytes

            lookup = locate_lock_bytes(manifest_path, run_id=run_id, sidecar_path=sidecar_path)
            if lookup.data is not None and _sha256_bytes(lookup.data) == sidecar_sha256:
                return lookup.data, None
        except Exception:
            pass

    return None, None


def _ensure_symlink_free_path(root: Path, relpath: str) -> Path:
    """Verify ``root / relpath`` sits plainly inside ``root`` with no symlinks or special files."""
    if not root.is_dir():
        raise RunBundleInvalidError(f"Output root directory does not exist: {root}")
    cur = root
    for part in PurePosixPath(relpath).parts:
        cur = cur / part
        try:
            st = os.lstat(cur)
        except FileNotFoundError as exc:
            raise RunBundleInvalidError(f"Expected output file missing: {cur}") from exc
        if stat.S_ISLNK(st.st_mode):
            raise RunBundleUnsafePathError(f"Refusing symlink in output path: {cur}")
    st_final = os.lstat(cur)
    if not stat.S_ISREG(st_final.st_mode):
        raise RunBundleUnsafePathError(f"Output path is not a regular file: {cur}")
    return cur


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _fetch_events_by_key(
    con: Any, key: list[str], *, has_extra_keys: bool = True
) -> list[dict[str, Any]]:
    select_sql = (
        _EVENT_SELECT
        if has_extra_keys
        else (
            "SELECT eid, v, kind, entity, ts, project, project_id, writer, seq, "
            "main_root, worktree_root, origin, data, NULL::VARCHAR AS extra_keys FROM events"
        )
    )
    rows = con.execute(
        f"{select_sql} WHERE entity_key = ?",  # noqa: S608 -- internal constant
        [json.dumps(key)],
    ).fetchall()
    return [_row_to_event(r) for r in rows]


def _resolve_log_run_row(con: Any, run_id: str, project: str | None) -> dict[str, Any]:
    if not run_id:
        raise OutputTierError(RUN_NOT_FOUND, "empty run id")
    params: list[Any] = [run_id]
    proj_sql = ""
    if project:
        proj_sql = " AND project_slug = ?"
        params.append(project)
    cur = con.execute(f"SELECT * FROM runs WHERE id = ?{proj_sql}", params)  # noqa: S608
    cols = [d[0] for d in cur.description]
    rows = cur.fetchall()
    if rows:
        return dict(zip(cols, rows[0], strict=True))

    prefix_params: list[Any] = [_escape_like(run_id)]
    if project:
        prefix_params.append(project)
    id_rows = con.execute(
        f"SELECT DISTINCT id FROM runs WHERE id LIKE ? || '%' ESCAPE '\\'{proj_sql}",  # noqa: S608
        prefix_params,
    ).fetchall()
    ids = sorted({r[0] for r in id_rows})
    if not ids:
        raise OutputTierError(RUN_NOT_FOUND, run_id)
    if len(ids) > 1:
        raise AmbiguousRunId(run_id, ids)
    cur = con.execute("SELECT * FROM runs WHERE id = ?", [ids[0]])
    cols = [d[0] for d in cur.description]
    row = cur.fetchone()
    if row is None:
        raise OutputTierError(RUN_NOT_FOUND, run_id)
    return dict(zip(cols, row, strict=True))


def _collect_log_causal_closure(
    con: Any, run_row: dict[str, Any], *, with_campaign: bool
) -> list[dict[str, Any]]:
    run_id = str(run_row["id"])
    collected: list[dict[str, Any]] = []

    # 1. runs (full lifecycle for [run_id])
    collected.extend(_fetch_events_by_key(con, [run_id]))

    # 2. sidecar_anchors
    sc_path = run_row.get("sidecar_path") or ""
    sc_sha = run_row.get("sidecar_sha256") or ""
    if sc_path and sc_sha:
        anchor_rows = con.execute(
            "SELECT id FROM sidecar_anchors WHERE path = ? AND sha256 = ?",
            [sc_path, sc_sha],
        ).fetchall()
        for (aid,) in anchor_rows:
            if aid:
                collected.extend(_fetch_events_by_key(con, [aid]))

    # 3. output_copies
    try:
        copy_rows = con.execute(
            "SELECT DISTINCT copy_id FROM output_copies WHERE run_id = ?", [run_id]
        ).fetchall()
        for (cid,) in copy_rows:
            if cid:
                collected.extend(_fetch_events_by_key(con, [run_id, cid]))
    except duckdb.Error:
        pass

    # 4. submits (per SUBMITS_DDL: slurm_job_id or (project_slug, sidecar_sha256, command))
    slurm_job_id = run_row.get("slurm_job_id") or ""
    if slurm_job_id:
        submit_rows = con.execute(
            "SELECT id FROM submits WHERE slurm_job_id = ?", [slurm_job_id]
        ).fetchall()
    elif run_row.get("project_slug") and sc_sha and run_row.get("command"):
        submit_rows = con.execute(
            "SELECT id FROM submits WHERE project_slug = ? AND sidecar_sha256 = ? AND command = ?",
            [run_row["project_slug"], sc_sha, run_row["command"]],
        ).fetchall()
    else:
        submit_rows = []
    for (sid,) in submit_rows:
        if sid:
            collected.extend(_fetch_events_by_key(con, [sid]))

    # 5. run_edges
    edge_rows = con.execute(
        "SELECT child_run_id, parent_run_id FROM run_edges "
        "WHERE child_run_id = ? OR parent_run_id = ?",
        [run_id, run_id],
    ).fetchall()
    for child_id, parent_id in edge_rows:
        if child_id and parent_id:
            collected.extend(_fetch_events_by_key(con, [child_id, parent_id, "run"]))

    # 6. supersession_edges
    try:
        sup_rows = con.execute(
            "SELECT id FROM supersession_edges "
            "WHERE (entity_type = 'run' AND entity_id = ?) "
            "OR (superseded_by_type = 'run' AND superseded_by_id = ?)",
            [run_id, run_id],
        ).fetchall()
        for (sup_id,) in sup_rows:
            if sup_id:
                collected.extend(_fetch_events_by_key(con, [sup_id]))
    except duckdb.Error:
        pass

    # 7. blast_radius_ledger
    if sc_sha:
        br_rows = con.execute(
            "SELECT id FROM blast_radius_ledger "
            "WHERE (entity_type = 'run' AND entity_id = ?) "
            "OR (entity_type = 'sidecar' AND anchor_value = ?)",
            [run_id, sc_sha],
        ).fetchall()
    else:
        br_rows = con.execute(
            "SELECT id FROM blast_radius_ledger WHERE entity_type = 'run' AND entity_id = ?",
            [run_id],
        ).fetchall()
    for (br_id,) in br_rows:
        if br_id:
            collected.extend(_fetch_events_by_key(con, [br_id]))

    # 8. trust_ledger
    tl_rows = con.execute("SELECT id FROM trust_ledger WHERE run_id = ?", [run_id]).fetchall()
    for (tl_id,) in tl_rows:
        if tl_id:
            collected.extend(_fetch_events_by_key(con, [tl_id]))

    # 9. archived_items
    arch_rows = con.execute("SELECT record_id, run_ids FROM archived_items").fetchall()
    for rec_id, rids_json in arch_rows:
        if not rec_id or not rids_json:
            continue
        try:
            rids = json.loads(rids_json) if isinstance(rids_json, str) else rids_json
        except json.JSONDecodeError:
            continue
        if isinstance(rids, list) and run_id in rids:
            collected.extend(_fetch_events_by_key(con, [rec_id]))

    # 10. observations
    derived_obs_id: str | None = None
    raw_meta = run_row.get("metadata")
    if isinstance(raw_meta, str) and raw_meta.strip():
        with contextlib.suppress(json.JSONDecodeError):
            parsed_meta = json.loads(raw_meta)
            if isinstance(parsed_meta, dict) and parsed_meta.get("derived_from_observation"):
                derived_obs_id = str(parsed_meta["derived_from_observation"])
    elif isinstance(raw_meta, dict) and raw_meta.get("derived_from_observation"):
        derived_obs_id = str(raw_meta["derived_from_observation"])

    try:
        if derived_obs_id:
            obs_rows = con.execute(
                "SELECT id FROM observations WHERE run_id = ? OR id = ?",
                [run_id, derived_obs_id],
            ).fetchall()
        else:
            obs_rows = con.execute(
                "SELECT id FROM observations WHERE run_id = ?", [run_id]
            ).fetchall()
        for (obs_id,) in obs_rows:
            if obs_id:
                collected.extend(_fetch_events_by_key(con, [obs_id]))
    except duckdb.Error:
        pass

    # 11. acknowledgment_ledger
    try:
        ack_rows = con.execute("SELECT id, run_ids FROM acknowledgment_ledger").fetchall()
        for ack_id, rids_json in ack_rows:
            if not ack_id or not rids_json:
                continue
            try:
                rids = json.loads(rids_json) if isinstance(rids_json, str) else rids_json
            except json.JSONDecodeError:
                continue
            if isinstance(rids, list) and run_id in rids:
                collected.extend(_fetch_events_by_key(con, [ack_id]))
    except duckdb.Error:
        pass

    # Optional campaign closure
    campaign_id = run_row.get("campaign_id") or ""
    if with_campaign and campaign_id:
        collected.extend(_fetch_events_by_key(con, [campaign_id]))
        collected.extend(_fetch_events_by_key(con, [campaign_id, run_id]))
        camp_edges = con.execute(
            "SELECT child_campaign_id, parent_campaign_id FROM campaign_edges "
            "WHERE child_campaign_id = ? OR parent_campaign_id = ?",
            [campaign_id, campaign_id],
        ).fetchall()
        for child_cid, parent_cid in camp_edges:
            if child_cid and parent_cid:
                collected.extend(_fetch_events_by_key(con, [child_cid, parent_cid, "campaign"]))

    # Deduplicate by eid, filter campaign events if with_campaign=False, sort deterministically
    by_eid: dict[str, dict[str, Any]] = {}
    for ev in collected:
        if not with_campaign and ev.get("kind") in (
            _CAMPAIGN_EVENT_KINDS | {"campaign.run_added", "campaign_run.imported"}
        ):
            continue
        eid = ev.get("eid")
        if eid and eid not in by_eid:
            by_eid[eid] = ev
    return sorted(by_eid.values(), key=lambda e: (e.get("ts", ""), e.get("eid", "")))


def _resolve_legacy_run(catalog_dir: Path, run_id: str, project: str | None) -> Any:
    if not project:
        return get_run(run_id, catalog_dir, allow_cool_fallback=True, include_superseded=True)

    from bathos.catalog import read_runs
    from bathos.query import _SOURCE_KEY, COOL_UNINDEXED_SOURCE, find_runs

    pool: dict[str, Any] = {}
    for r in find_runs(catalog_dir, project=project, include_superseded=True):
        pool[r.id] = r
    if (catalog_dir / "bathos.db").is_file():
        for r in read_runs(catalog_dir):
            if r.project_slug == project and r.id not in pool:
                try:
                    meta = json.loads(r.metadata or "{}")
                except (json.JSONDecodeError, TypeError):
                    meta = {}
                if not isinstance(meta, dict):
                    meta = {}
                meta[_SOURCE_KEY] = COOL_UNINDEXED_SOURCE
                r.metadata = json.dumps(meta)
                pool[r.id] = r

    if run_id in pool:
        return pool[run_id]
    matches = sorted(rid for rid in pool if rid.startswith(run_id))
    if not matches:
        return None
    if len(matches) > 1:
        raise AmbiguousRunId(run_id, matches)
    return pool[matches[0]]


def _synthesize_legacy_closure(
    catalog_dir: Path, run_id: str, project: str | None
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any]]:
    """Synthesize canonical events for a pre-cutover cool/warm catalog."""
    run = _resolve_legacy_run(catalog_dir, run_id, project)
    if run is None:
        detail = f"{run_id} (project={project})" if project else run_id
        raise OutputTierError(RUN_NOT_FOUND, detail)

    ws = resolve_workspace_for_run(run, catalog_dir)
    main_root = ws["main_root"]
    worktree_root = ws["worktree_root"]
    project_id = read_project_id(main_root / ".bth.toml") if main_root else None

    src_class = "fragment" if run_source(run) == "cool_unindexed" else "warm"
    data = run_event_data(run)
    data["source_class"] = src_class
    ts_str = run.timestamp.isoformat()

    events: list[dict[str, Any]] = []
    imp_eid = str(uuid.uuid5(NAMESPACE_BATHOS, f"bundle-legacy:run.imported:{run.id}"))
    events.append(
        build_envelope(
            kind="run.imported",
            entity=[run.id],
            data=data,
            main_root=main_root,
            worktree_root=worktree_root,
            project=run.project_slug,
            project_id=project_id,
            writer="bundle-legacy",
            seq=1,
            origin="migration",
            ts=ts_str,
            eid=imp_eid,
        )
    )

    # Synthesize run.outputs_hashed when output_metadata is present
    parsed_om: list[dict[str, Any]] = []
    if run.output_metadata and run.output_metadata != "[]":
        with contextlib.suppress(json.JSONDecodeError):
            loaded = json.loads(run.output_metadata)
            if isinstance(loaded, list):
                parsed_om = [x for x in loaded if isinstance(x, dict)]
    if parsed_om:
        oh_eid = str(uuid.uuid5(NAMESPACE_BATHOS, f"bundle-legacy:run.outputs_hashed:{run.id}"))
        events.append(
            build_envelope(
                kind="run.outputs_hashed",
                entity=[run.id],
                data={"phase": PHASE_RUN_END, "output_metadata": parsed_om},
                main_root=main_root,
                worktree_root=worktree_root,
                project=run.project_slug,
                project_id=project_id,
                writer="bundle-legacy",
                seq=2,
                origin="migration",
                ts=ts_str,
                eid=oh_eid,
            )
        )

    # Pull any legacy warm-table closure rows if bathos.db exists
    campaign_ctx: dict[str, Any] = {
        "campaign_id": run.campaign_id or None,
        "external_campaign_ref": run.campaign_id or None,
        "with_campaign": False,
    }
    db_path = catalog_dir / "bathos.db"
    if db_path.is_file():
        opened = connect_legacy(db_path)
        if not isinstance(opened, dict):
            con = opened
            try:
                if run.campaign_id:
                    with contextlib.suppress(Exception):
                        crow = con.execute(
                            "SELECT name, mode, claim_path, claim_sha256 FROM campaigns WHERE id = ?",
                            [run.campaign_id],
                        ).fetchone()
                        if crow:
                            campaign_ctx.update(
                                {
                                    "campaign_name": crow[0],
                                    "campaign_mode": crow[1],
                                    "claim_path": crow[2],
                                    "claim_sha256": crow[3],
                                }
                            )
                        cr_row = con.execute(
                            "SELECT seq_position, evalue FROM campaign_runs "
                            "WHERE campaign_id = ? AND run_id = ?",
                            [run.campaign_id, run.id],
                        ).fetchone()
                        if cr_row:
                            campaign_ctx["seq_position"] = cr_row[0]
                            campaign_ctx["evalue"] = cr_row[1]
            finally:
                con.close()

    run_dict = run_event_data(run)
    run_dict["_resolved_main_root"] = str(main_root)
    run_dict["_resolved_worktree_root"] = str(worktree_root)
    run_dict["_project_id"] = project_id
    return run_dict, events, campaign_ctx


def resolve_workspace_for_run(run: Any, catalog_dir: Path) -> dict[str, Path]:
    from bathos.workspace import resolve_workspace

    cand = Path(run.sidecar_path) if getattr(run, "sidecar_path", "") else Path.cwd()
    if cand.is_absolute() and cand.exists():
        ws = resolve_workspace(cand.parent)
        return {"main_root": ws.fs_root, "worktree_root": ws.fs_root}
    with contextlib.suppress(Exception):
        ws = resolve_workspace(Path.cwd())
        return {"main_root": ws.fs_root, "worktree_root": ws.fs_root}
    return {"main_root": catalog_dir, "worktree_root": catalog_dir}


def _build_campaign_context_log(
    con: Any, run_row: dict[str, Any], *, with_campaign: bool
) -> dict[str, Any]:
    cid = run_row.get("campaign_id") or ""
    if not cid:
        return {
            "campaign_id": None,
            "external_campaign_ref": None,
            "with_campaign": bool(with_campaign),
        }
    ctx: dict[str, Any] = {
        "campaign_id": cid,
        "external_campaign_ref": None if with_campaign else cid,
        "with_campaign": bool(with_campaign),
        "seq_position": run_row.get("seq_position"),
        "evalue": run_row.get("evalue"),
    }
    with contextlib.suppress(Exception):
        crow = con.execute(
            "SELECT name, mode, claim_path, claim_sha256 FROM campaigns WHERE id = ?",
            [cid],
        ).fetchone()
        if crow:
            ctx.update(
                {
                    "campaign_name": crow[0],
                    "campaign_mode": crow[1],
                    "claim_path": crow[2],
                    "claim_sha256": crow[3],
                }
            )
    return ctx


def _create_git_bundle_bytes(git_hash: str, root: Path) -> bytes:
    if not git_hash or git_hash == "unknown":
        raise RunBundleInvalidError("Cannot include git bundle: run has no valid git_hash")
    if not root.is_dir():
        raise RunBundleInvalidError(f"Cannot include git bundle: root does not exist ({root})")
    with tempfile.TemporaryDirectory(prefix="bth-git-bundle-") as tmpdir:
        bundle_file = Path(tmpdir) / "git.bundle"
        proc = subprocess.run(
            ["git", "bundle", "create", str(bundle_file), git_hash],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if proc.returncode != 0 or not bundle_file.is_file():
            raise RunBundleInvalidError(
                f"git bundle create failed for {git_hash}: {proc.stderr.strip()}"
            )
        return bundle_file.read_bytes()


def _write_archive_atomic(out_path: Path, members: list[tuple[str, bytes]]) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_name(f".{out_path.name}.{uuid.uuid4().hex[:10]}.tmp")
    name_lower = out_path.name.lower()

    def _add_members(tf: tarfile.TarFile) -> None:
        for arcname, payload in members:
            info = tarfile.TarInfo(name=arcname)
            info.size = len(payload)
            info.mode = 0o644
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            info.type = tarfile.REGTYPE
            tf.addfile(info, io.BytesIO(payload))

    try:
        if name_lower.endswith((".tar.gz", ".tgz")):
            with tarfile.open(tmp_path, mode="w:gz", format=tarfile.PAX_FORMAT) as tf:
                _add_members(tf)
        elif name_lower.endswith(".tar"):
            with tarfile.open(tmp_path, mode="w", format=tarfile.PAX_FORMAT) as tf:
                _add_members(tf)
        else:
            cctx = zstandard.ZstdCompressor(level=19)
            with (
                open(tmp_path, "wb") as raw_fh,
                cctx.stream_writer(raw_fh, closefd=False) as zw,
                tarfile.open(fileobj=zw, mode="w|", format=tarfile.PAX_FORMAT) as tf,
            ):
                _add_members(tf)
            with open(tmp_path, "rb") as sync_fh:
                os.fsync(sync_fh.fileno())
        os.replace(tmp_path, out_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def export_run_bundle(
    run_id: str,
    output_path: str | Path,
    *,
    project: str | None = None,
    include_outputs: bool = False,
    include_git: bool = False,
    with_campaign: bool = False,
    allow_missing_sidecar: bool = False,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    catalog_dir: Path | None = None,
) -> dict[str, Any]:
    """Export a single run and its full causal closure into a verifiable `bathos.bundle.v1` archive."""
    cd = Path(catalog_dir) if catalog_dir is not None else default_catalog_dir()
    out_path = Path(output_path)

    if is_log_mode(cd):
        ingest_after_write(cd)
        con = connect_read(cd, wire_validity=False)
        try:
            run_row = _resolve_log_run_row(con, run_id, project)
            events = _collect_log_causal_closure(con, run_row, with_campaign=with_campaign)
            campaign_ctx = _build_campaign_context_log(
                con, run_row, with_campaign=with_campaign
            )
        finally:
            con.close()
        source_mode = "log"
        full_run_id = str(run_row["id"])
        run_events = [e for e in events if e.get("entity") == [full_run_id]]
        # Prefer started/live event roots over synthetic
        started = [e for e in run_events if e.get("kind") == "run.started"] or run_events
        source_main_root = str((started[0].get("main_root") if started else None) or "")
        source_worktree_root = str((started[0].get("worktree_root") if started else None) or "")
        project_id = next((e.get("project_id") for e in started if e.get("project_id")), None)
    else:
        run_row, events, campaign_ctx = _synthesize_legacy_closure(cd, run_id, project)
        source_mode = "legacy"
        full_run_id = str(run_row["id"])
        source_main_root = str(run_row.get("_resolved_main_root") or "")
        source_worktree_root = str(run_row.get("_resolved_worktree_root") or "")
        project_id = run_row.get("_project_id")

    if not events:
        raise RunBundleInvalidError(f"No events found for run {full_run_id}")

    origin_host = (run_row.get("hostname") or "").strip() or os.uname().nodename

    # Verify and gather sidecar bytes
    sc_sha = (run_row.get("sidecar_sha256") or "").strip()
    sc_path = (run_row.get("sidecar_path") or "").strip()
    manifest_path = (run_row.get("manifest_path") or "").strip()
    sidecar_bytes: bytes | None = None
    if sc_sha:
        sidecar_bytes, _ = _probe_sidecar_bytes(
            sidecar_path=sc_path,
            sidecar_sha256=sc_sha,
            run_id=full_run_id,
            catalog_dir=cd,
            worktree_root=source_worktree_root or None,
            main_root=source_main_root or None,
            manifest_path=manifest_path,
        )
        if sidecar_bytes is None and not allow_missing_sidecar:
            raise RunBundleInvalidError(
                f"Sidecar bytes matching sha256 {sc_sha} not found for run {full_run_id}; "
                "pass --allow-missing-sidecar to export without sidecar"
            )
    sidecar_meta = {
        "included": sidecar_bytes is not None,
        "recorded_path": sc_path,
        "sha256": sc_sha,
        "missing_reason": (
            None if (sidecar_bytes is not None or not sc_sha) else "unavailable"
        ),
    }

    # Build events.jsonl + canonical event digests
    events_lines = [_canonical_bytes_strict(ev) + b"\n" for ev in events]
    events_jsonl_bytes = b"".join(events_lines)
    events_sha256 = _sha256_bytes(events_jsonl_bytes)
    event_digests = {str(ev["eid"]): _compute_digest_strict(ev) for ev in events}

    # Outputs manifest + optional blobs
    refs = reference_hashes(events)
    outputs_manifest: list[dict[str, Any]] = []
    blob_members: list[tuple[str, bytes]] = []
    total_output_bytes = 0
    for relpath in sorted(refs):
        entry = refs[relpath]
        blob_included = False
        if include_outputs:
            root_candidates = [
                Path(r)
                for r in (
                    entry.worktree_root,
                    entry.external_root,
                    source_worktree_root,
                    source_main_root,
                )
                if r
            ]
            blob_file: Path | None = None
            for root in root_candidates:
                if (root / relpath).exists() or (root / relpath).is_symlink():
                    blob_file = _ensure_symlink_free_path(root, relpath)
                    break
            if blob_file is None and entry.path:
                ep = Path(entry.path)
                if ep.is_symlink():
                    raise RunBundleUnsafePathError(f"Refusing symlink output: {ep}")
                if ep.is_file():
                    blob_file = ep
            if blob_file is None:
                raise RunBundleInvalidError(
                    f"Output blob missing for run {full_run_id}: {relpath}"
                )
            payload = blob_file.read_bytes()
            actual_sha = _sha256_bytes(payload)
            if actual_sha != entry.sha256:
                raise RunBundleTamperedError(
                    f"Output blob SHA-256 mismatch for {relpath}: "
                    f"expected {entry.sha256}, got {actual_sha}"
                )
            total_output_bytes += len(payload)
            if max_output_bytes > 0 and total_output_bytes > max_output_bytes:
                raise RunBundleInvalidError(
                    f"Total output blob size ({total_output_bytes} bytes) exceeds "
                    f"max_output_bytes ({max_output_bytes})"
                )
            blob_members.append((f"{OUTPUT_BLOB_PREFIX}{relpath}", payload))
            blob_included = True
        outputs_manifest.append(
            {
                "relpath": entry.relpath,
                "sha256": entry.sha256,
                "size_bytes": entry.size_bytes,
                "phase": entry.phase,
                "blob_included": blob_included,
            }
        )

    # Optional git.bundle
    git_bundle_bytes: bytes | None = None
    git_hash = str(run_row.get("git_hash") or "")
    if include_git:
        git_root = Path(source_worktree_root or source_main_root or Path.cwd())
        git_bundle_bytes = _create_git_bundle_bytes(git_hash, git_root)

    git_meta = {
        "git_hash": git_hash,
        "git_branch": str(run_row.get("git_branch") or ""),
        "git_dirty": bool(run_row.get("git_dirty")),
        "git_dirty_content_id": run_row.get("git_dirty_content_id"),
        "git_provenance_source": run_row.get("git_provenance_source"),
        "dependency_lock_sha256": run_row.get("dependency_lock_sha256"),
        "git_bundle_included": git_bundle_bytes is not None,
    }

    manifest_body: dict[str, Any] = {
        "format": BUNDLE_FORMAT,
        "bundle_version": BUNDLE_VERSION,
        "source_mode": source_mode,
        "run_id": full_run_id,
        "project_slug": run_row.get("project_slug"),
        "project_id": project_id,
        "origin_host": origin_host,
        "source_main_root": source_main_root,
        "source_worktree_root": source_worktree_root,
        "exported_at": _now_utc_iso(),
        "exported_by_bth_version": _bth_version(),
        "include_outputs": include_outputs,
        "include_git": include_git,
        "events_count": len(events),
        "events_sha256": events_sha256,
        "event_digests": event_digests,
        "sidecar": sidecar_meta,
        "outputs": outputs_manifest,
        "campaign_context": campaign_ctx,
        "git": git_meta,
    }
    bundle_sha256 = _sha256_bytes(_canonical_bytes_strict(manifest_body))
    manifest = {**manifest_body, "bundle_sha256": bundle_sha256}
    manifest_bytes = json.dumps(
        manifest, indent=2, sort_keys=True, ensure_ascii=False, allow_nan=False
    ).encode("utf-8")

    members: list[tuple[str, bytes]] = [
        ("manifest.json", manifest_bytes),
        ("events.jsonl", events_jsonl_bytes),
    ]
    if sidecar_bytes is not None:
        members.append(("sidecar.bth.toml", sidecar_bytes))
    if git_bundle_bytes is not None:
        members.append(("git.bundle", git_bundle_bytes))
    members.extend(blob_members)

    _write_archive_atomic(out_path, members)

    return {
        "ok": True,
        "run_id": full_run_id,
        "bundle_path": str(out_path),
        "bundle_sha256": bundle_sha256,
        "events_count": len(events),
        "events_sha256": events_sha256,
        "sidecar_included": sidecar_bytes is not None,
        "outputs_count": len(outputs_manifest),
        "include_outputs": include_outputs,
        "include_git": include_git,
        "source_mode": source_mode,
    }


# ---------------------------------------------------------------------------
# Container security & verification
# ---------------------------------------------------------------------------


def _validate_archive_member_name(name: str) -> str:
    if not isinstance(name, str) or not name:
        raise RunBundleUnsafePathError("Archive contains empty member name")
    if "\x00" in name or "\\" in name or "//" in name:
        raise RunBundleUnsafePathError(f"Unsafe characters in archive member name: {name!r}")
    if name.startswith("/") or name.endswith("/"):
        raise RunBundleUnsafePathError(f"Leading or trailing slash in archive member: {name!r}")
    parts = name.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise RunBundleUnsafePathError(f"Unsafe path segment in archive member: {name!r}")
    if ":" in parts[0]:
        raise RunBundleUnsafePathError(f"Drive-like prefix in archive member: {name!r}")
    if name in ALLOWED_TOP_LEVEL_FILES:
        return name
    if name.startswith(OUTPUT_BLOB_PREFIX) and len(name) > len(OUTPUT_BLOB_PREFIX):
        return name
    raise RunBundleUnsafePathError(f"Disallowed archive member: {name!r}")


def _read_bounded_member_bytes(
    tf: tarfile.TarFile, member: tarfile.TarInfo, *, limit: int | None
) -> bytes:
    if member.size < 0:
        raise RunBundleUnsafePathError(f"Negative member size for {member.name!r}")
    if limit is not None and member.size > limit:
        raise RunBundleUnsafePathError(
            f"Member {member.name!r} size ({member.size}) exceeds limit ({limit})"
        )
    fobj = tf.extractfile(member)
    if fobj is None:
        raise RunBundleUnsafePathError(f"Cannot read archive member {member.name!r}")
    buf = bytearray()
    with fobj:
        while chunk := fobj.read(READ_CHUNK_BYTES):
            buf.extend(chunk)
            if limit is not None and len(buf) > limit:
                raise RunBundleUnsafePathError(
                    f"Member {member.name!r} actual bytes exceed limit ({limit})"
                )
    return bytes(buf)


def _open_tar_stream(raw_fh: Any):
    head = raw_fh.read(4)
    raw_fh.seek(0)
    if head == b"\x28\xb5\x2f\xfd":
        dctx = zstandard.ZstdDecompressor()
        zr = dctx.stream_reader(raw_fh)
        return zr, tarfile.open(fileobj=zr, mode="r|")
    if head[:2] == b"\x1f\x8b":
        return None, tarfile.open(fileobj=raw_fh, mode="r:gz")
    return None, tarfile.open(fileobj=raw_fh, mode="r:")


def _read_and_validate_archive(
    bundle_path: Path, *, max_output_bytes: int
) -> dict[str, bytes]:
    if not bundle_path.is_file():
        raise RunBundleInvalidError(f"Bundle file does not exist: {bundle_path}")

    members: dict[str, bytes] = {}
    member_count = 0
    total_output_bytes = 0

    try:
        with open(bundle_path, "rb") as raw_fh:
            zr, tf = _open_tar_stream(raw_fh)
            try:
                with tf:
                    for m in tf:
                        member_count += 1
                        if member_count > MAX_MEMBER_COUNT:
                            raise RunBundleUnsafePathError(
                                f"Archive exceeds max member count ({MAX_MEMBER_COUNT})"
                            )
                        if m.issym() or m.islnk() or not m.isreg():
                            raise RunBundleUnsafePathError(
                                f"Disallowed non-regular archive member {m.name!r} (type={m.type!r})"
                            )
                        clean_name = _validate_archive_member_name(m.name)
                        if clean_name in members:
                            raise RunBundleUnsafePathError(
                                f"Duplicate archive member: {clean_name!r}"
                            )

                        limit: int | None
                        if clean_name == "manifest.json":
                            limit = MAX_MANIFEST_BYTES
                        elif clean_name == "events.jsonl":
                            limit = MAX_EVENTS_BYTES
                        elif clean_name == "sidecar.bth.toml":
                            limit = MAX_SIDECAR_BYTES
                        elif clean_name == "git.bundle":
                            limit = MAX_GIT_BUNDLE_BYTES
                        else:
                            if max_output_bytes > 0:
                                remaining = max(0, max_output_bytes - total_output_bytes)
                                if remaining == 0 and m.size > 0:
                                    raise RunBundleUnsafePathError(
                                        f"Output blobs exceed max_output_bytes ({max_output_bytes})"
                                    )
                                limit = remaining
                            else:
                                limit = None

                        payload = _read_bounded_member_bytes(tf, m, limit=limit)
                        if clean_name.startswith(OUTPUT_BLOB_PREFIX):
                            total_output_bytes += len(payload)
                            if max_output_bytes > 0 and total_output_bytes > max_output_bytes:
                                raise RunBundleUnsafePathError(
                                    f"Output blobs ({total_output_bytes}) exceed "
                                    f"max_output_bytes ({max_output_bytes})"
                                )
                        members[clean_name] = payload
            finally:
                if zr is not None:
                    zr.close()
    except (RunBundleUnsafePathError, RunBundleInvalidError, RunBundleTamperedError):
        raise
    except (tarfile.TarError, zstandard.ZstdError, EOFError, OSError) as exc:
        raise RunBundleInvalidError(f"Failed to read bundle archive {bundle_path}: {exc}") from exc

    if "manifest.json" not in members or "events.jsonl" not in members:
        raise RunBundleInvalidError("Bundle missing required manifest.json or events.jsonl")
    return members


@dataclass
class _VerifiedBundle:
    manifest: dict[str, Any]
    bundle_sha256: str
    run_id: str
    origin_host: str
    events_bytes: bytes
    events: list[dict[str, Any]]
    event_digests: dict[str, str]
    sidecar_bytes: bytes | None
    local_sidecar_path: Path | None
    local_worktree_root: Path | None
    local_main_root: Path | None
    output_blobs: dict[str, bytes]
    expected_refs: dict[str, Any]
    folded_run: dict[str, Any]


def _resolve_local_project_roots(
    *,
    worktree_root: str | Path | None,
    bundle_project_id: str | None,
    bundle_project_slug: str | None,
) -> tuple[Path | None, Path | None]:
    """Return ``(local_main_root, local_worktree_root)`` if a matching local checkout exists."""
    if worktree_root is not None:
        wr = Path(worktree_root).expanduser().resolve()
        if not wr.is_dir():
            raise RunBundleInvalidError(f"--worktree-root directory does not exist: {wr}")
        lr = resolve_log_root(wr)
        if not lr.unaffiliated:
            return lr.main_root.resolve(), wr
        return None, wr

    # Try current workspace if affiliated and matching project identity
    with contextlib.suppress(Exception):
        lr = resolve_log_root(Path.cwd())
        if not lr.unaffiliated:
            local_pid = read_project_id(lr.main_root / ".bth.toml")
            local_slug = None
            cfg = find_project_config(lr.main_root)
            if cfg is not None:
                with contextlib.suppress(Exception):
                    local_slug = load_project_config(cfg).slug
            if (bundle_project_id and local_pid == bundle_project_id) or (
                not bundle_project_id and bundle_project_slug and local_slug == bundle_project_slug
            ):
                return lr.main_root.resolve(), lr.worktree_root.resolve()

    # Fall back to registered roots in ~/.bth/projects.toml
    for reg_root, reg_pid in list_registered_entries():
        if not reg_root.is_dir():
            continue
        if bundle_project_id and reg_pid == bundle_project_id:
            return reg_root.resolve(), reg_root.resolve()
        if not bundle_project_id and bundle_project_slug:
            cfg = find_project_config(reg_root)
            if cfg is not None:
                with contextlib.suppress(Exception):
                    if load_project_config(cfg).slug == bundle_project_slug:
                        return reg_root.resolve(), reg_root.resolve()

    return None, None


def _verify_bundle_in_memory(
    bundle_path: str | Path,
    *,
    allow_missing_sidecar: bool = False,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    worktree_root: str | Path | None = None,
    catalog_dir: Path | None = None,
) -> _VerifiedBundle:
    cd = Path(catalog_dir) if catalog_dir is not None else default_catalog_dir()
    bp = Path(bundle_path)
    members = _read_and_validate_archive(bp, max_output_bytes=max_output_bytes)

    # 1. Parse and verify manifest.json + bundle_sha256
    manifest = _loads_strict(members["manifest.json"])
    if not isinstance(manifest, dict):
        raise RunBundleInvalidError("manifest.json must be a JSON object")
    if manifest.get("format") != BUNDLE_FORMAT or manifest.get("bundle_version") != BUNDLE_VERSION:
        raise RunBundleInvalidError(
            f"Unsupported bundle format/version: {manifest.get('format')!r} v{manifest.get('bundle_version')!r}"
        )
    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise RunBundleInvalidError("manifest.json missing non-empty run_id")
    _validate_safe_run_id(run_id)
    declared_bundle_sha = manifest.get("bundle_sha256")
    if not isinstance(declared_bundle_sha, str) or not declared_bundle_sha:
        raise RunBundleTamperedError("manifest.json missing bundle_sha256")

    manifest_without_sha = {k: v for k, v in manifest.items() if k != "bundle_sha256"}
    computed_bundle_sha = _sha256_bytes(_canonical_bytes_strict(manifest_without_sha))
    if computed_bundle_sha != declared_bundle_sha:
        raise RunBundleTamperedError(
            f"bundle_sha256 mismatch: expected {declared_bundle_sha}, got {computed_bundle_sha}"
        )

    # 2. Verify events.jsonl bytes, envelope schema, event count, and per-event canonical digests
    events_bytes = members["events.jsonl"]
    if _sha256_bytes(events_bytes) != manifest.get("events_sha256"):
        raise RunBundleTamperedError("events.jsonl SHA-256 mismatch against manifest.events_sha256")

    try:
        events_text = events_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RunBundleInvalidError(f"events.jsonl is not valid UTF-8: {exc}") from exc

    events: list[dict[str, Any]] = []
    seen_eids: set[str] = set()
    event_digests: dict[str, str] = {}
    manifest_digests = manifest.get("event_digests")
    if not isinstance(manifest_digests, dict):
        raise RunBundleTamperedError("manifest.event_digests must be an object")

    for raw_line in events_text.splitlines():
        if not raw_line.strip():
            continue
        ev = _loads_strict(raw_line)
        err = _validate_envelope(ev)
        if err:
            raise RunBundleInvalidError(f"Invalid event envelope in events.jsonl: {err}")
        eid = ev["eid"]
        if eid in seen_eids:
            raise RunBundleInvalidError(f"Duplicate eid {eid!r} in events.jsonl")
        seen_eids.add(eid)
        digest = _compute_digest_strict(ev)
        if manifest_digests.get(eid) != digest:
            raise RunBundleTamperedError(f"Canonical digest mismatch for event eid={eid}")
        event_digests[eid] = digest
        events.append(ev)

    if len(events) != manifest.get("events_count"):
        raise RunBundleTamperedError(
            f"events_count mismatch: manifest={manifest.get('events_count')}, actual={len(events)}"
        )
    if set(manifest_digests) != seen_eids:
        raise RunBundleTamperedError("manifest.event_digests keys do not match events.jsonl eids")

    campaign_ctx_raw = manifest.get("campaign_context")
    if campaign_ctx_raw is not None and not isinstance(campaign_ctx_raw, dict):
        raise RunBundleInvalidError("manifest.campaign_context must be an object")
    campaign_ctx = campaign_ctx_raw if isinstance(campaign_ctx_raw, dict) else {}
    campaign_bundled = bool(campaign_ctx.get("with_campaign")) or (
        bool(campaign_ctx.get("campaign_id")) and campaign_ctx.get("external_campaign_ref") is None
    )

    if not campaign_bundled:
        for ev in events:
            ev_kind = str(ev.get("kind") or "")
            ev_entity = ev.get("entity")
            if ev_kind in _RUN_KINDS or ev_kind == "run.remote_dispatched":
                if ev_entity != [run_id]:
                    raise RunBundleInvalidError(
                        f"Single-run bundle for {run_id!r} cannot contain foreign run event "
                        f"{ev_kind!r} for entity={ev_entity!r}"
                    )
            elif ev_kind.startswith("output_copy."):
                if not (isinstance(ev_entity, list) and len(ev_entity) >= 1 and ev_entity[0] == run_id):
                    raise RunBundleInvalidError(
                        f"Single-run bundle for {run_id!r} cannot contain foreign output_copy event "
                        f"{ev_kind!r} for entity={ev_entity!r}"
                    )
            elif (
                ev_kind in (_CAMPAIGN_EVENT_KINDS | {"campaign.run_added", "campaign_run.imported"})
                or ev_kind.startswith("campaign_edge.")
            ):
                raise RunBundleInvalidError(
                    f"Single-run bundle without campaign context cannot contain campaign event {ev_kind!r}"
                )

    run_events = [e for e in events if e.get("kind") in _RUN_KINDS and e.get("entity") == [run_id]]
    if not run_events:
        raise RunBundleInvalidError(f"Bundle contains no run events for run_id={run_id!r}")

    folded_run = fold_run(run_events)
    local_main_root, local_worktree_root = _resolve_local_project_roots(
        worktree_root=worktree_root,
        bundle_project_id=manifest.get("project_id"),
        bundle_project_slug=manifest.get("project_slug"),
    )

    # 3. Verify sidecar
    sidecar_meta = manifest.get("sidecar")
    if not isinstance(sidecar_meta, dict):
        raise RunBundleInvalidError("manifest.sidecar must be an object")
    run_sidecar_sha = str(folded_run.get("sidecar_sha256") or "")
    if run_sidecar_sha:
        _validate_safe_sha256(run_sidecar_sha, label="sidecar_sha256")
    run_sidecar_path = str(folded_run.get("sidecar_path") or "")
    sidecar_included = bool(sidecar_meta.get("included"))
    sidecar_bytes = members.get("sidecar.bth.toml")

    if sidecar_included:
        if sidecar_bytes is None:
            raise RunBundleTamperedError("manifest.sidecar.included is true but sidecar.bth.toml missing")
        actual_sc_sha = _sha256_bytes(sidecar_bytes)
        if actual_sc_sha != sidecar_meta.get("sha256"):
            raise RunBundleTamperedError("sidecar.bth.toml SHA-256 mismatch against manifest.sidecar.sha256")
        if run_sidecar_sha and actual_sc_sha != run_sidecar_sha:
            raise RunBundleTamperedError("sidecar.bth.toml SHA-256 mismatch against run.sidecar_sha256")
    elif sidecar_bytes is not None:
        raise RunBundleTamperedError("sidecar.bth.toml present in archive but manifest.sidecar.included is false")

    # Resolve local checkout sidecar first by SHA-256
    local_sidecar_path: Path | None = None
    if run_sidecar_sha:
        if sidecar_meta.get("sha256") and sidecar_meta.get("sha256") != run_sidecar_sha:
            raise RunBundleTamperedError("manifest.sidecar.sha256 disagrees with run.sidecar_sha256")
        _found_bytes, local_sidecar_path = _probe_sidecar_bytes(
            sidecar_path=run_sidecar_path,
            sidecar_sha256=run_sidecar_sha,
            run_id=run_id,
            catalog_dir=cd,
            worktree_root=str(local_worktree_root) if local_worktree_root else None,
            main_root=str(local_main_root) if local_main_root else None,
            allow_raw_sidecar_path=False,
        )
        if not sidecar_included and _found_bytes is None and not allow_missing_sidecar:
            raise RunBundleInvalidError(
                f"Bundle omits sidecar.bth.toml for run {run_id} (expected sha256 {run_sidecar_sha}) "
                "and no matching local sidecar was found; pass --allow-missing-sidecar to proceed"
            )

    # 4. Verify outputs manifest against reference_hashes(events) and blobs if included
    expected_refs = reference_hashes(events)
    manifest_outputs = manifest.get("outputs")
    if not isinstance(manifest_outputs, list):
        raise RunBundleTamperedError("manifest.outputs must be a list")

    expected_out_map = {
        rel: (entry.sha256, entry.size_bytes, entry.phase) for rel, entry in expected_refs.items()
    }
    manifest_out_map: dict[str, tuple[Any, Any, Any]] = {}
    for item in manifest_outputs:
        if not isinstance(item, dict) or not isinstance(item.get("relpath"), str):
            raise RunBundleTamperedError("Invalid entry in manifest.outputs")
        rel = item["relpath"]
        if rel in manifest_out_map:
            raise RunBundleTamperedError(f"Duplicate relpath in manifest.outputs: {rel!r}")
        manifest_out_map[rel] = (item.get("sha256"), item.get("size_bytes"), item.get("phase"))

    if manifest_out_map != expected_out_map:
        raise RunBundleTamperedError(
            "manifest.outputs does not match reference_hashes(events)"
        )

    include_outputs = bool(manifest.get("include_outputs"))
    blob_members = {
        k[len(OUTPUT_BLOB_PREFIX) :]: v
        for k, v in members.items()
        if k.startswith(OUTPUT_BLOB_PREFIX)
    }
    if include_outputs:
        if set(blob_members) != set(expected_refs):
            missing = sorted(set(expected_refs) - set(blob_members))
            extra = sorted(set(blob_members) - set(expected_refs))
            raise RunBundleTamperedError(
                f"Output blobs mismatch against reference_hashes: missing={missing}, extra={extra}"
            )
        for rel, entry in expected_refs.items():
            blob = blob_members[rel]
            if _sha256_bytes(blob) != entry.sha256:
                raise RunBundleTamperedError(f"Output blob SHA-256 mismatch for {rel!r}")
            if entry.size_bytes is not None and len(blob) != entry.size_bytes:
                raise RunBundleTamperedError(f"Output blob size mismatch for {rel!r}")
    elif blob_members:
        raise RunBundleTamperedError(
            "Archive contains output blobs when manifest.include_outputs is false"
        )

    origin_host = str(manifest.get("origin_host") or folded_run.get("hostname") or "remote")
    return _VerifiedBundle(
        manifest=manifest,
        bundle_sha256=declared_bundle_sha,
        run_id=run_id,
        origin_host=origin_host,
        events_bytes=events_bytes,
        events=events,
        event_digests=event_digests,
        sidecar_bytes=sidecar_bytes,
        local_sidecar_path=local_sidecar_path,
        local_worktree_root=local_worktree_root,
        local_main_root=local_main_root,
        output_blobs=blob_members,
        expected_refs=expected_refs,
        folded_run=folded_run,
    )


def verify_run_bundle(
    bundle_path: str | Path,
    *,
    allow_missing_sidecar: bool = False,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    worktree_root: str | Path | None = None,
    catalog_dir: Path | None = None,
) -> dict[str, Any]:
    """Verify a `bathos.bundle.v1` archive completely in memory without writing to disk."""
    vb = _verify_bundle_in_memory(
        bundle_path,
        allow_missing_sidecar=allow_missing_sidecar,
        max_output_bytes=max_output_bytes,
        worktree_root=worktree_root,
        catalog_dir=catalog_dir,
    )
    return {
        "ok": True,
        "run_id": vb.run_id,
        "origin_host": vb.origin_host,
        "bundle_sha256": vb.bundle_sha256,
        "events_count": len(vb.events),
        "events_sha256": vb.manifest.get("events_sha256"),
        "sidecar_included": vb.sidecar_bytes is not None,
        "outputs_count": len(vb.expected_refs),
        "include_outputs": bool(vb.manifest.get("include_outputs")),
        "include_git": bool(vb.manifest.get("include_git")),
        "source_mode": vb.manifest.get("source_mode"),
        "manifest": vb.manifest,
    }


# ---------------------------------------------------------------------------
# Equivocation check & import
# ---------------------------------------------------------------------------


def _load_catalog_events_for_equivocation(
    cd: Path,
    eids: list[str],
    run_id: str,
    *,
    entity_keys: list[list[str]] | None = None,
) -> tuple[dict[str, str], list[dict[str, Any]]]:
    if not is_log_mode(cd) or not index_db_path(cd).exists():
        return {}, []
    con = connect_read(cd, wire_validity=False)
    try:
        event_cols = {
            str(r[1])
            for r in con.execute("PRAGMA table_info('events')").fetchall()
        }
        has_env_sha = "envelope_sha256" in event_cols
        has_extra = "extra_keys" in event_cols
        env_expr = "envelope_sha256" if has_env_sha else "NULL::VARCHAR AS envelope_sha256"
        extra_expr = "extra_keys" if has_extra else "NULL::VARCHAR AS extra_keys"

        existing_digests: dict[str, str] = {}
        if eids:
            rows = con.execute(
                f"SELECT eid, v, kind, entity, ts, project, project_id, writer, seq, "  # noqa: S608
                f"main_root, worktree_root, origin, data, {env_expr}, {extra_expr} "
                "FROM events WHERE eid IN (SELECT unnest(?::VARCHAR[]))",
                [eids],
            ).fetchall()
            for (
                eid,
                v,
                kind,
                entity,
                ts,
                project,
                project_id,
                writer,
                seq,
                main_root,
                worktree_root,
                origin,
                data,
                env_sha,
                extra_keys,
            ) in rows:
                if env_sha:
                    existing_digests[eid] = env_sha
                else:
                    ev: dict[str, Any] = {
                        "v": v,
                        "eid": eid,
                        "kind": kind,
                        "entity": json.loads(entity),
                        "ts": ts,
                        "project": project,
                        "project_id": project_id,
                        "writer": writer,
                        "seq": seq,
                        "main_root": main_root,
                        "worktree_root": worktree_root,
                        "origin": origin if origin is not None else "live",
                        "data": json.loads(data),
                    }
                    if extra_keys:
                        parsed_extra = json.loads(extra_keys)
                        if isinstance(parsed_extra, dict):
                            ev.update(parsed_extra)
                    existing_digests[eid] = _compute_digest_strict(ev)

        keys_to_load: list[list[str]] = [[run_id]]
        seen_keys: set[tuple[str, ...]] = {(run_id,)}
        for key in entity_keys or ():
            kt = tuple(str(x) for x in key)
            if kt and kt not in seen_keys:
                seen_keys.add(kt)
                keys_to_load.append([str(x) for x in key])

        existing_entity_events: list[dict[str, Any]] = []
        for key in keys_to_load:
            existing_entity_events.extend(
                _fetch_events_by_key(con, key, has_extra_keys=has_extra)
            )
        return existing_digests, existing_entity_events
    finally:
        con.close()


def _genesis_signature(ev: dict[str, Any]) -> tuple[Any, ...]:
    d = ev.get("data") or {}
    return (
        d.get("sidecar_sha256") or "",
        d.get("command") or "",
        tuple(d.get("argv") or ()),
        d.get("git_hash") or "",
        d.get("timestamp") or "",
        _normalize_hostname(d.get("hostname")),
    )


def _terminal_signature(ev: dict[str, Any]) -> tuple[Any, ...]:
    d = ev.get("data") or {}
    return (
        d.get("status"),
        d.get("exit_code"),
        d.get("outcome"),
        tuple(d.get("output_paths") or ()),
        d.get("duration_s"),
    )


def _run_end_outputs_signature(ev: dict[str, Any]) -> bytes:
    d = ev.get("data") or {}
    return _canonical_bytes_strict(
        {
            "output_metadata": d.get("output_metadata"),
            "refused": d.get("refused"),
        }
    )


def _campaign_genesis_signature(ev: dict[str, Any]) -> tuple[Any, ...]:
    d = ev.get("data") or {}
    return (
        d.get("name") or "",
        d.get("mode") or "",
        d.get("claim_sha256") or "",
        d.get("started_at") or "",
    )


def _output_copy_signature(ev: dict[str, Any]) -> tuple[Any, ...]:
    d = ev.get("data") or {}
    return (
        d.get("tier") or "",
        d.get("profile") or "",
        d.get("path") or "",
        d.get("archive_sha256") or "",
        d.get("manifest_sha256") or "",
    )


def _check_events_equivocation(
    cd: Path,
    *,
    run_id: str,
    events: list[dict[str, Any]],
    event_digests: dict[str, str],
) -> tuple[list[dict[str, Any]], list[str]]:
    eids = [str(ev["eid"]) for ev in events]
    entity_keys: list[list[str]] = []
    for ev in events:
        ent = ev.get("entity")
        kind = str(ev.get("kind") or "")
        if not isinstance(ent, list):
            continue
        if (
            (kind in _RUN_KINDS or kind == "run.remote_dispatched" or kind in ("campaign.created", "campaign.imported"))
            and len(ent) == 1
        ) or (kind == "output_copy.recorded" and len(ent) == 2):
            entity_keys.append([str(x) for x in ent])

    existing_digests, existing_entity_events = _load_catalog_events_for_equivocation(
        cd, eids, run_id, entity_keys=entity_keys
    )

    new_events: list[dict[str, Any]] = []
    skipped_eids: list[str] = []
    for ev in events:
        eid = str(ev["eid"])
        digest = event_digests[eid]
        prior = existing_digests.get(eid)
        if prior is not None:
            if prior != digest:
                raise RunImportConflictError(
                    f"Same-eid equivocation detected for eid={eid} (existing digest {prior} != {digest})"
                )
            skipped_eids.append(eid)
        else:
            new_events.append(ev)

    if not new_events or not existing_entity_events:
        return new_events, skipped_eids

    existing_by_ent_kind: dict[tuple[tuple[str, ...], str], list[dict[str, Any]]] = {}
    for ev in existing_entity_events:
        ent = ev.get("entity")
        if isinstance(ent, list):
            ent_key = tuple(str(x) for x in ent)
            existing_by_ent_kind.setdefault((ent_key, str(ev.get("kind"))), []).append(ev)

    for ev in new_events:
        ent = ev.get("entity")
        if not isinstance(ent, list):
            continue
        ent_key = tuple(str(x) for x in ent)
        kind = str(ev.get("kind") or "")

        if len(ent_key) == 1 and (kind in _RUN_KINDS or kind == "run.remote_dispatched"):
            rid = ent_key[0]
            # Option D stub -> remote run.started transition
            if kind == "run.started" and (ent_key, "run.remote_dispatched") in existing_by_ent_kind:
                inc_d = ev.get("data") or {}
                for stub in existing_by_ent_kind[(ent_key, "run.remote_dispatched")]:
                    st_d = stub.get("data") or {}
                    if st_d.get("sidecar_sha256") and st_d.get("sidecar_sha256") != inc_d.get(
                        "sidecar_sha256"
                    ):
                        raise RunImportConflictError(
                            f"Incoming run.started sidecar_sha256 conflicts with remote_dispatched stub for {rid}"
                        )
                    if st_d.get("git_hash") and st_d.get("git_hash") != inc_d.get("git_hash"):
                        raise RunImportConflictError(
                            f"Incoming run.started git_hash conflicts with remote_dispatched stub for {rid}"
                        )

            # Distinct-eid genesis equivocation
            if kind == "run.started" and (ent_key, "run.started") in existing_by_ent_kind:
                inc_sig = _genesis_signature(ev)
                for ex in existing_by_ent_kind[(ent_key, "run.started")]:
                    if _genesis_signature(ex) != inc_sig:
                        raise RunImportConflictError(
                            f"Conflicting run.started genesis event for run {rid}"
                        )

            # Distinct-eid terminal equivocation
            if kind == "run.finished" and (ent_key, "run.finished") in existing_by_ent_kind:
                inc_term = _terminal_signature(ev)
                for ex in existing_by_ent_kind[(ent_key, "run.finished")]:
                    if _terminal_signature(ex) != inc_term:
                        raise RunImportConflictError(
                            f"Conflicting run.finished terminal event for run {rid}"
                        )

            # Distinct-eid run_end outputs equivocation
            if (
                kind == "run.outputs_hashed"
                and (ev.get("data") or {}).get("phase") == PHASE_RUN_END
            ):
                existing_run_end = [
                    ex
                    for ex in existing_by_ent_kind.get((ent_key, "run.outputs_hashed"), [])
                    if (ex.get("data") or {}).get("phase") == PHASE_RUN_END
                ]
                if existing_run_end:
                    inc_out = _run_end_outputs_signature(ev)
                    for ex in existing_run_end:
                        if _run_end_outputs_signature(ex) != inc_out:
                            raise RunImportConflictError(
                                f"Conflicting run.outputs_hashed (phase='run_end') event for run {rid}"
                            )

        if len(ent_key) == 1 and kind in ("campaign.created", "campaign.imported"):
            cid = ent_key[0]
            prior_camp = existing_by_ent_kind.get(
                (ent_key, "campaign.created"), []
            ) + existing_by_ent_kind.get((ent_key, "campaign.imported"), [])
            if prior_camp:
                inc_camp = _campaign_genesis_signature(ev)
                for ex in prior_camp:
                    if _campaign_genesis_signature(ex) != inc_camp:
                        raise RunImportConflictError(
                            f"Conflicting {kind} event for campaign {cid}"
                        )

        if len(ent_key) == 2 and kind == "output_copy.recorded":
            prior_copies = existing_by_ent_kind.get((ent_key, "output_copy.recorded"), [])
            if prior_copies:
                inc_copy = _output_copy_signature(ev)
                for ex in prior_copies:
                    if _output_copy_signature(ex) != inc_copy:
                        raise RunImportConflictError(
                            f"Conflicting output_copy.recorded event for {ent_key!r}"
                        )

    return new_events, skipped_eids


def _check_equivocation(
    cd: Path, vb: _VerifiedBundle
) -> tuple[list[dict[str, Any]], list[str]]:
    return _check_events_equivocation(
        cd,
        run_id=vb.run_id,
        events=vb.events,
        event_digests=vb.event_digests,
    )


def _check_parent_dirs_no_symlink(dest_dir: Path, relpath: str) -> None:
    if dest_dir.exists() or dest_dir.is_symlink():
        st = os.lstat(dest_dir)
        if stat.S_ISLNK(st.st_mode):
            raise RunBundleUnsafePathError(f"Destination directory is a symlink: {dest_dir}")
        if not stat.S_ISDIR(st.st_mode):
            raise RunBundleUnsafePathError(f"Destination is not a directory: {dest_dir}")
    cur = dest_dir
    for part in PurePosixPath(relpath).parts[:-1]:
        cur = cur / part
        if cur.exists() or cur.is_symlink():
            st = os.lstat(cur)
            if stat.S_ISLNK(st.st_mode):
                raise RunBundleUnsafePathError(
                    f"Pre-existing symlink in extraction parent path: {cur}"
                )
            if not stat.S_ISDIR(st.st_mode):
                raise RunBundleUnsafePathError(
                    f"Non-directory component in extraction parent path: {cur}"
                )


def _extract_outputs_safely(
    dest_dir: Path,
    vb: _VerifiedBundle,
    *,
    overwrite_outputs: bool,
    dry_run: bool,
) -> list[str]:
    if not vb.expected_refs:
        return []
    if not vb.manifest.get("include_outputs"):
        raise RunBundleInvalidError(
            "Bundle does not contain output blobs (exported without --include-outputs)"
        )

    actions: list[tuple[Path, bytes]] = []
    for relpath, entry in sorted(vb.expected_refs.items()):
        _check_parent_dirs_no_symlink(dest_dir, relpath)
        target = dest_dir / relpath
        blob = vb.output_blobs[relpath]
        if target.exists() or target.is_symlink():
            st = os.lstat(target)
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode):
                raise RunBundleUnsafePathError(
                    f"Refusing to extract over symlink/non-regular file: {target}"
                )
            existing_sha = _sha256_file(target)
            if existing_sha == entry.sha256:
                continue
            if not overwrite_outputs:
                raise RunBundleTamperedError(
                    f"Existing file differs from bundle output {relpath!r} at {target}; "
                    "pass --overwrite-outputs to replace"
                )
        actions.append((target, blob))

    if dry_run:
        return [str(t) for t, _ in actions]

    written: list[str] = []
    for target, blob in actions:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:10]}.tmp")
        try:
            with open(tmp, "wb") as fh:
                fh.write(blob)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, target)
            written.append(str(target))
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    return written


def _atomic_write_bytes(path: Path, data: bytes, *, readonly: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:10]}.tmp")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if readonly:
            os.chmod(tmp, 0o444)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _check_git_commit_local(git_hash: str, root: Path | None) -> bool:
    if not git_hash or git_hash == "unknown" or root is None or not root.is_dir():
        return False
    try:
        proc = subprocess.run(
            ["git", "cat-file", "-e", f"{git_hash}^{{commit}}"],
            cwd=root,
            capture_output=True,
            timeout=10,
        )
        return proc.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def import_run_bundle(
    bundle_path: str | Path,
    *,
    worktree_root: str | Path | None = None,
    extract_outputs: str | Path | None = None,
    overwrite_outputs: bool = False,
    allow_missing_sidecar: bool = False,
    max_output_bytes: int = DEFAULT_MAX_OUTPUT_BYTES,
    dry_run: bool = False,
    catalog_dir: Path | None = None,
    import_method: str = "bundle_import",
    remote_name: str | None = None,
) -> dict[str, Any]:
    """Verify and import a `bathos.bundle.v1` archive into the local catalog."""
    cd = Path(catalog_dir) if catalog_dir is not None else default_catalog_dir()
    vb = _verify_bundle_in_memory(
        bundle_path,
        allow_missing_sidecar=allow_missing_sidecar,
        max_output_bytes=max_output_bytes,
        worktree_root=worktree_root,
        catalog_dir=cd,
    )
    new_events, skipped_eids = _check_equivocation(cd, vb)

    # Pre-validate output extraction before writing any log/receipt/sidecar
    extract_dest: Path | None = None
    if extract_outputs is not None and str(extract_outputs).strip():
        extract_dest = Path(extract_outputs).expanduser().resolve()

    _validate_safe_run_id(vb.run_id)
    run_sidecar_sha = str(vb.folded_run.get("sidecar_sha256") or "")
    if run_sidecar_sha:
        _validate_safe_sha256(run_sidecar_sha, label="sidecar_sha256")
    resolved_sidecar_path = vb.local_sidecar_path
    cached_sidecar_path: Path | None = None
    if resolved_sidecar_path is None and vb.sidecar_bytes is not None and run_sidecar_sha:
        cached_sidecar_path = cd / "imported_sidecars" / vb.run_id / f"{run_sidecar_sha}.bth.toml"
        resolved_sidecar_path = cached_sidecar_path

    if dry_run:
        if extract_dest is not None:
            _extract_outputs_safely(
                extract_dest, vb, overwrite_outputs=overwrite_outputs, dry_run=True
            )
        status = "already_present" if not new_events else "would_import"
        return {
            "ok": True,
            "status": status,
            "run_id": vb.run_id,
            "origin_host": vb.origin_host,
            "bundle_sha256": vb.bundle_sha256,
            "new_events": len(new_events),
            "skipped_existing": len(skipped_eids),
            "local_sidecar_path": str(resolved_sidecar_path) if resolved_sidecar_path else None,
            "local_worktree_root": (
                str(vb.local_worktree_root) if vb.local_worktree_root else None
            ),
            "extracted_outputs": [],
        }

    extracted: list[str] = []
    if extract_dest is not None:
        extracted = _extract_outputs_safely(
            extract_dest, vb, overwrite_outputs=overwrite_outputs, dry_run=False
        )

    # Idempotent replay when all events already exist and receipt is on disk
    safe_host = _sanitize_path_segment(vb.origin_host, "remote")
    safe_slug = _sanitize_path_segment(vb.manifest.get("project_slug"), "_unaffiliated")
    if vb.local_main_root is not None:
        target_dir = vb.local_main_root / ".bth" / "log" / "imported" / safe_host
    else:
        target_dir = Path.home() / ".bth" / "log" / "imported" / safe_slug / safe_host

    stem = f"{vb.run_id}.{vb.bundle_sha256[:12]}"
    jsonl_path = target_dir / f"{stem}.jsonl"
    receipt_path = target_dir / f"{stem}.receipt.json"

    if not new_events and receipt_path.is_file():
        return {
            "ok": True,
            "status": "already_present",
            "run_id": vb.run_id,
            "origin_host": vb.origin_host,
            "bundle_sha256": vb.bundle_sha256,
            "new_events": 0,
            "skipped_existing": len(skipped_eids),
            "local_sidecar_path": str(resolved_sidecar_path) if resolved_sidecar_path else None,
            "local_worktree_root": (
                str(vb.local_worktree_root) if vb.local_worktree_root else None
            ),
            "extracted_outputs": extracted,
        }

    # Cache verified sidecar bytes if not already matched in local checkout
    if cached_sidecar_path is not None and vb.sidecar_bytes is not None and not cached_sidecar_path.is_file():
        _atomic_write_bytes(cached_sidecar_path, vb.sidecar_bytes, readonly=True)

    git_hash = str((vb.manifest.get("git") or {}).get("git_hash") or "")
    git_commit_local = _check_git_commit_local(
        git_hash, vb.local_worktree_root or vb.local_main_root
    )
    campaign_ctx = vb.manifest.get("campaign_context") or {}
    external_campaign_ref = campaign_ctx.get("external_campaign_ref")

    receipt = {
        "v": 1,
        "run_id": vb.run_id,
        "bundle_sha256": vb.bundle_sha256,
        "origin_host": vb.origin_host,
        "source_main_root": vb.manifest.get("source_main_root") or "",
        "source_worktree_root": vb.manifest.get("source_worktree_root") or "",
        "imported_at": _now_utc_iso(),
        "import_method": import_method,
        "remote_name": remote_name or vb.origin_host,
        "local_worktree_root": str(vb.local_worktree_root) if vb.local_worktree_root else None,
        "local_sidecar_path": str(resolved_sidecar_path) if resolved_sidecar_path else None,
        "external_campaign_ref": external_campaign_ref,
        "git_commit_local": git_commit_local,
        "sidecar_missing_allowed": bool(
            allow_missing_sidecar and vb.sidecar_bytes is None and run_sidecar_sha
        ),
    }

    if vb.local_main_root is not None:
        with contextlib.suppress(Exception):
            register_main_root(vb.local_main_root)

    _atomic_write_bytes(jsonl_path, vb.events_bytes)
    _atomic_write_bytes(
        receipt_path,
        (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8"),
    )

    report = run_ingest(cd)
    status = "already_present" if not new_events and report.new_events == 0 else "imported"
    return {
        "ok": True,
        "status": status,
        "run_id": vb.run_id,
        "origin_host": vb.origin_host,
        "bundle_sha256": vb.bundle_sha256,
        "new_events": len(new_events),
        "skipped_existing": len(skipped_eids),
        "local_sidecar_path": str(resolved_sidecar_path) if resolved_sidecar_path else None,
        "local_worktree_root": (
            str(vb.local_worktree_root) if vb.local_worktree_root else None
        ),
        "extracted_outputs": extracted,
    }


__all__ = [
    "BUNDLE_FORMAT",
    "BUNDLE_VERSION",
    "DEFAULT_MAX_OUTPUT_BYTES",
    "export_run_bundle",
    "import_run_bundle",
    "verify_run_bundle",
]
