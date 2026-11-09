"""Validity Engine: Read-time validity resolution, cache management, and distance facts.

Spec 261106 §2.1, §2.2, §3.4, §3.5.
Invariant I1 (Non-Obliteration): base run records in cool Parquet fragments,
.bth/log/*.jsonl, and runs tables are never mutated or deleted.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from bathos.config import default_catalog_dir
from bathos.runlog.mode import is_log_mode
from bathos.runlog.project_id import list_registered_roots
from bathos.version_resolver import (
    DefectEntry,
    commit_in_range,
    evaluate_predicate,
    load_instrument_registries,
    resolve_toolchain_version,
    version_in_range,
)

logger = logging.getLogger(__name__)

NEUTRAL_OUTCOME_LABELS = frozenset(
    {"observed", "collected", "recorded", "reaped", "unassigned", ""}
)

VALIDITY_CACHE_META_DDL = """
CREATE TABLE IF NOT EXISTS validity_cache_meta (
    singleton INTEGER PRIMARY KEY DEFAULT 1,
    inputs_hash TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""

VALIDITY_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS validity_cache (
    run_id TEXT PRIMARY KEY,
    validity_state TEXT NOT NULL,
    reason TEXT,
    replacement_id TEXT,
    version_source TEXT NOT NULL,
    evidence_tier TEXT NOT NULL,
    defect_id TEXT,
    defect_revision INTEGER,
    estimand_marker TEXT,
    inputs_hash TEXT NOT NULL,
    cached_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_validity_state ON validity_cache(validity_state);
"""

VERSION_CACHE_DDL = """
CREATE TABLE IF NOT EXISTS version_cache (
    main_root TEXT NOT NULL,
    git_hash TEXT NOT NULL,
    lock_sha256 TEXT NOT NULL,
    package_name TEXT NOT NULL,
    resolved_version TEXT,
    resolved_commit TEXT,
    version_source TEXT NOT NULL,
    PRIMARY KEY (main_root, git_hash, lock_sha256, package_name)
);
"""


@dataclass(frozen=True)
class ValidityResult:
    run_id: str
    state: str  # "current" | "suspect" | "unverifiable" | "void" | "affected" | "superseded" | "archived"
    reason: str | None
    replacement_id: str | None
    version_source: str  # "recorded" | "verified-from-lock" | "inferred-from-commit" | "unknown"
    evidence_tier: str  # "confirmatory" | "calibrating" | "exploratory" | "spike" | "undeclared"
    is_hidden: bool  # True iff state in ("void", "affected", "superseded", "archived")
    defect_id: str | None = None
    defect_revision: int | None = None
    estimand_marker: str | None = None  # e.g. "aminx:pre-AMINX-2026-002" | "aminx:unknown"


def validity_cache_db_path(catalog_dir: Path | str | None = None) -> Path:
    return Path(catalog_dir or default_catalog_dir()) / "validity_cache.db"


def validity_cache_lock_path(catalog_dir: Path | str | None = None) -> Path:
    return Path(catalog_dir or default_catalog_dir()) / "validity_cache.lock"


def resolve_evidence_tier(stage_name: str | None, epistemic_class: str | None) -> str:
    """Resolve evidence tier string according to Spec §4.2."""
    raw = (stage_name or epistemic_class or "").lower().strip()
    if not raw:
        return "undeclared"
    if raw in ("confirmatory", "validation", "power"):
        return "confirmatory"
    if raw in ("calibrating", "benchmark"):
        return "calibrating"
    if raw in ("exploratory", "explore"):
        return "exploratory"
    if raw == "spike":
        return "spike"
    return "undeclared"


def compute_validity_inputs_hash(
    catalog_dir: Path | str,
    con: duckdb.DuckDBPyConnection | None = None,
    workspace_root: Path | None = None,
) -> str:
    """Compute 64-char hex SHA256 over:
    (1) sorted instrument registries (canonical_path, sha256) + use_user_registry
    (2) catalog state: log mode (events rowid/count, watermarks, runs count) or legacy warm mode
    (3) cool-tier run fragments (count, total_size, max_mtime)
    (4) ledger states: blast_radius, archived_items, supersession_edges.
    """
    cd = Path(catalog_dir)
    hasher = hashlib.sha256()

    # 1. Collect candidate project roots
    candidate_roots: list[Path] = []

    # 1a. Explicit workspace_root
    if workspace_root is not None and Path(workspace_root).is_dir():
        candidate_roots.append(Path(workspace_root).resolve())

    # 1b. Environment override
    env_root = os.environ.get("BTH_WORKSPACE_ROOT")
    if env_root and Path(env_root).is_dir():
        candidate_roots.append(Path(env_root).resolve())

    # 1c. Upward traversal from cwd
    cwd = Path.cwd()
    for d in [cwd, *cwd.parents]:
        if (d / ".bth").is_dir() or (d / ".bth.toml").is_file():
            candidate_roots.append(d.resolve())
            break

    # 1d. Upward traversal from catalog_dir
    cd_res = cd.resolve()
    for d in [cd_res, *cd_res.parents]:
        if d.name == ".bth":
            cand = d.parent
            if (cand / ".bth" / "instruments").is_dir():
                candidate_roots.append(cand.resolve())
        elif (d / ".bth" / "instruments").is_dir():
            candidate_roots.append(d.resolve())

    # 1e. Workspace resolution via bathos.workspace
    try:
        from bathos.workspace import resolve_workspace

        ws_ctx = resolve_workspace(cwd)
        if ws_ctx.fs_root.is_dir():
            candidate_roots.append(ws_ctx.fs_root.resolve())
    except Exception:
        pass

    # 1f. Registered project roots
    try:
        for r in list_registered_roots():
            if r.is_dir():
                candidate_roots.append(r.resolve())
    except Exception:
        pass

    # 1g. Filter out ~/.bth (OD-4: Never use ~/.bth/instruments/)
    bth_home = (Path.home() / ".bth").resolve()
    user_home = Path.home().resolve()
    valid_roots: list[Path] = []
    seen_roots: set[Path] = set()
    for r in candidate_roots:
        if r in seen_roots:
            continue
        if r == bth_home or (r / ".bth").resolve() == bth_home or r == user_home:
            continue
        seen_roots.add(r)
        valid_roots.append(r)

    # 1h. Collect registry files
    reg_files: list[tuple[str, str]] = []
    # User global (${XDG_CONFIG_HOME:-~/.config}/bathos/instruments/*.toml)
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    user_inst = Path(xdg_config or (Path.home() / ".config")) / "bathos" / "instruments"
    if user_inst.is_dir():
        for p in sorted(user_inst.glob("*.toml")):
            try:
                reg_files.append((str(p.resolve()), hashlib.sha256(p.read_bytes()).hexdigest()))
            except Exception:
                pass

    # Project-local registries (<root>/.bth/instruments/*.toml)
    for root in valid_roots:
        p_inst = root / ".bth" / "instruments"
        if p_inst.is_dir():
            for p in sorted(p_inst.glob("*.toml")):
                try:
                    reg_files.append((str(p.resolve()), hashlib.sha256(p.read_bytes()).hexdigest()))
                except Exception:
                    pass

    reg_files = sorted(set(reg_files))
    hasher.update(json.dumps(reg_files).encode("utf-8"))

    # 1i. Spec §2.2.B.1: Hash use_user_registry booleans per project and env
    from bathos.version_resolver import _should_use_user_registry

    user_reg_flags: list[tuple[str, bool]] = [
        ("env", _should_use_user_registry(None)),
    ]
    for root in valid_roots:
        user_reg_flags.append((str(root), _should_use_user_registry(root)))
    user_reg_flags = sorted(set(user_reg_flags))
    hasher.update(json.dumps(user_reg_flags).encode("utf-8"))

    # 2. Database state
    should_close_con = False
    if con is None:
        from bathos.index import connect_read

        try:
            con = connect_read(cd, missing="empty")
            should_close_con = True
        except Exception:
            con = None

    if con is not None:
        try:
            # Check log mode vs warm mode
            tables = {
                r[0]
                for r in con.execute(
                    "SELECT table_name FROM duckdb_tables()"
                ).fetchall()
            }

            if "events" in tables:
                ev_info = con.execute("SELECT count(*), max(rowid) FROM events").fetchone()
                hasher.update(str(ev_info).encode("utf-8"))
            if "ingest_watermarks" in tables:
                wm_info = con.execute(
                    "SELECT root_kind, root_id, path, byte_offset FROM ingest_watermarks ORDER BY 1,2,3"
                ).fetchall()
                hasher.update(str(wm_info).encode("utf-8"))
            if "runs" in tables:
                r_info = con.execute("SELECT count(*) FROM runs").fetchone()
                hasher.update(str(r_info).encode("utf-8"))
            if "blast_radius_ledger" in tables:
                br_info = con.execute("SELECT count(*), max(amended_at) FROM blast_radius_ledger").fetchone()
                hasher.update(str(br_info).encode("utf-8"))
            if "archived_items" in tables:
                ai_info = con.execute("SELECT count(*), max(recorded_at) FROM archived_items").fetchone()
                hasher.update(str(ai_info).encode("utf-8"))
            if "supersession_edges" in tables:
                se_info = con.execute("SELECT count(*), max(declared_at) FROM supersession_edges").fetchone()
                hasher.update(str(se_info).encode("utf-8"))
        except Exception as exc:
            logger.debug("Inputs hash DB query failed: %s", exc)
        finally:
            if should_close_con:
                con.close()

    # 2b. Cool-tier run fragments (Spec §2.2.B.3: count, total size, max mtime)
    runs_dir = cd / "runs"
    if runs_dir.is_dir():
        from bathos.catalog import iter_run_fragments

        try:
            run_frags = list(iter_run_fragments(runs_dir))
        except Exception:
            run_frags = sorted(
                f for f in runs_dir.rglob("*.parquet") if not f.name.endswith(".tmp.parquet")
            )
        frag_count = len(run_frags)
        frag_total_size = sum(f.stat().st_size for f in run_frags)
        frag_max_mtime = max((f.stat().st_mtime_ns for f in run_frags), default=0)
        hasher.update(json.dumps([frag_count, frag_total_size, frag_max_mtime]).encode("utf-8"))

    # Legacy cool fragments mtimes
    for dirname in ("blast_radius", "archived_items"):
        d = cd / dirname
        if d.is_dir():
            frag_meta = [
                (f.name, f.stat().st_mtime_ns, f.stat().st_size)
                for f in sorted(d.glob("*.parquet"))
            ]
            hasher.update(json.dumps(frag_meta).encode("utf-8"))

    return hasher.hexdigest()


def ensure_validity_cache(
    catalog_dir: Path | str,
    con: duckdb.DuckDBPyConnection | None = None,
    workspace_root: Path | None = None,
) -> None:
    """Ensure <catalog_dir>/validity_cache.db exists, matches inputs_hash, and covers all runs."""
    cd = Path(catalog_dir)
    cache_db = validity_cache_db_path(cd)
    lock_file = validity_cache_lock_path(cd)
    lock_file.parent.mkdir(parents=True, exist_ok=True)

    inputs_hash = compute_validity_inputs_hash(cd, con, workspace_root=workspace_root)

    # Fast read check without exclusive lock
    if cache_db.is_file():
        try:
            read_con = duckdb.connect(str(cache_db), read_only=True)
            meta = read_con.execute(
                "SELECT inputs_hash FROM validity_cache_meta WHERE singleton = 1"
            ).fetchone()
            read_con.close()
            if meta and meta[0] == inputs_hash:
                return  # Cache is up to date
        except Exception:
            pass

    # Cache is missing or stale: acquire lock and populate
    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        # Double check after taking lock
        if cache_db.is_file():
            try:
                read_con = duckdb.connect(str(cache_db), read_only=True)
                meta = read_con.execute(
                    "SELECT inputs_hash FROM validity_cache_meta WHERE singleton = 1"
                ).fetchone()
                read_con.close()
                if meta and meta[0] == inputs_hash:
                    return
            except Exception:
                pass

        # Re-resolve and populate validity cache
        # 1. Fetch runs
        should_close_con = False
        if con is None:
            from bathos.index import connect_read

            con = connect_read(cd, missing="empty")
            should_close_con = True

        try:
            # Check if runs table exists
            tables = {
                r[0]
                for r in con.execute(
                    "SELECT table_name FROM duckdb_tables()"
                ).fetchall()
            }
            if "runs" not in tables:
                from bathos.catalog import read_runs

                try:
                    run_rows = [(r.id,) for r in read_runs(cd)]
                except Exception:
                    run_rows = []
            else:
                run_rows = con.execute("SELECT id FROM runs").fetchall()
        finally:
            if should_close_con:
                con.close()
                con = None

        run_ids = [r[0] for r in run_rows]
        validity_map = resolve_validity(run_ids, cd, con=con, workspace_root=workspace_root)

        # Write to validity_cache.db
        w_con = duckdb.connect(str(cache_db))
        try:
            w_con.execute(VALIDITY_CACHE_META_DDL)
            w_con.execute(VALIDITY_CACHE_DDL)
            w_con.execute(VERSION_CACHE_DDL)

            now_iso = datetime.now(UTC).isoformat()
            w_con.execute("BEGIN")
            w_con.execute("DELETE FROM validity_cache")
            if validity_map:
                insert_data = [
                    (
                        v.run_id,
                        v.state,
                        v.reason,
                        v.replacement_id,
                        v.version_source,
                        v.evidence_tier,
                        v.defect_id,
                        v.defect_revision,
                        v.estimand_marker,
                        inputs_hash,
                        now_iso,
                    )
                    for v in validity_map.values()
                ]
                w_con.executemany(
                    "INSERT INTO validity_cache VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    insert_data,
                )
            w_con.execute(
                "INSERT OR REPLACE INTO validity_cache_meta (singleton, inputs_hash, updated_at) "
                "VALUES (1, ?, ?)",
                [inputs_hash, now_iso],
            )
            w_con.execute("COMMIT")
            w_con.execute("CHECKPOINT")
        finally:
            w_con.close()
    finally:
        with contextlib.suppress(Exception):
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _find_run_main_root(
    run_id: str,
    catalog_dir: Path | str,
    con: duckdb.DuckDBPyConnection | None = None,
    workspace_root: Path | None = None,
) -> Path | None:
    """Find main_root Path for a run via events table or registered roots."""
    if workspace_root is not None and workspace_root.is_dir():
        return workspace_root

    env_root = os.environ.get("BTH_WORKSPACE_ROOT")
    if env_root and Path(env_root).is_dir():
        return Path(env_root)

    cd = Path(catalog_dir)
    should_close = False
    if con is None:
        from bathos.index import connect_read

        try:
            con = connect_read(cd, missing="empty")
            should_close = True
        except Exception:
            con = None

    proj_slug = None
    if con is not None:
        try:
            tables = {
                r[0]
                for r in con.execute(
                    "SELECT table_name FROM duckdb_tables()"
                ).fetchall()
            }
            if "events" in tables:
                row = con.execute(
                    "SELECT main_root FROM events WHERE entity_key = ? AND main_root IS NOT NULL LIMIT 1",
                    [run_id],
                ).fetchone()
                if row and row[0]:
                    p = Path(row[0])
                    if p.is_dir():
                        return p
            if "runs" in tables:
                slug_row = con.execute(
                    "SELECT project_slug FROM runs WHERE id = ? LIMIT 1",
                    [run_id],
                ).fetchone()
                if slug_row and slug_row[0]:
                    proj_slug = slug_row[0]
        except Exception:
            pass
        finally:
            if should_close:
                con.close()

    # Try list_registered_roots
    reg_roots = list_registered_roots()
    if proj_slug:
        import tomllib
        for root in reg_roots:
            if root.is_dir() and (root / ".bth.toml").is_file():
                try:
                    doc = tomllib.loads((root / ".bth.toml").read_text())
                    if doc.get("project", {}).get("slug") == proj_slug:
                        return root
                except Exception:
                    pass

    # Check catalog_dir hierarchy for project root with instruments (excluding ~/.bth per OD-4)
    bth_home = (Path.home() / ".bth").resolve()
    user_home = Path.home().resolve()
    for d in [cd.resolve(), *cd.resolve().parents]:
        if d == bth_home or (d / ".bth").resolve() == bth_home or d == user_home:
            continue
        if d.name == ".bth":
            cand = d.parent
            if (cand / ".bth" / "instruments").is_dir():
                return cand
        elif (d / ".bth" / "instruments").is_dir():
            return d

    for root in reg_roots:
        if root.is_dir():
            return root

    cwd = Path.cwd()
    for d in [cwd, *cwd.parents]:
        if (d / ".bth").is_dir() or (d / ".bth.toml").is_file() or (d / "pyproject.toml").is_file():
            return d

    return None


def resolve_validity(
    run_ids: list[str],
    catalog_dir: Path | str,
    con: duckdb.DuckDBPyConnection | None = None,
    workspace_root: Path | None = None,
    db: duckdb.DuckDBPyConnection | None = None,
) -> dict[str, ValidityResult]:
    """Batched resolver returning dict[str, ValidityResult].

    Precedence order:
    1. archived (archived_items)
    2. superseded (supersession_edges)
    3. blast radius (blast_radius_ledger)
    4. instrument defect & estimand registries
    5. current
    """
    if not run_ids:
        return {}

    if con is None and db is not None:
        con = db

    cd = Path(catalog_dir)
    should_close = False
    if con is None:
        from bathos.index import connect_read

        con = connect_read(cd, missing="empty")
        should_close = True

    try:
        tables = {
            r[0]
            for r in con.execute(
                "SELECT table_name FROM duckdb_tables()"
            ).fetchall()
        }

        # 1. Fetch runs
        runs_by_id: dict[str, dict[str, Any]] = {}
        if "runs" in tables:
            # Query runs
            placeholders = ", ".join(["?"] * len(run_ids))
            cur = con.execute(f"SELECT * FROM runs WHERE id IN ({placeholders})", run_ids)
            col_names = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                d = dict(zip(col_names, row))
                runs_by_id[d["id"]] = d
        else:
            # Cool-tier fallback when runs table is absent
            from bathos.catalog import read_runs

            try:
                target_ids = set(run_ids)
                for r in read_runs(cd):
                    if r.id in target_ids:
                        runs_by_id[r.id] = {
                            "id": r.id,
                            "project_slug": r.project_slug,
                            "command": r.command,
                            "argv": r.argv,
                            "git_hash": r.git_hash,
                            "git_branch": r.git_branch,
                            "git_dirty": r.git_dirty,
                            "timestamp": r.timestamp,
                            "duration_s": r.duration_s,
                            "exit_code": r.exit_code,
                            "status": r.status,
                            "outcome": r.outcome,
                            "metadata": r.metadata,
                            "output_paths": r.output_paths,
                            "sidecar_path": r.sidecar_path,
                            "sidecar_sha256": r.sidecar_sha256,
                            "script_sha256": r.script_sha256,
                            "parent_run_id": r.parent_run_id,
                            "agent_mode": r.agent_mode,
                            "sidecar_mode": r.sidecar_mode,
                            "campaign_id": r.campaign_id,
                            "stage_name": r.stage_name,
                            "epistemic_class": r.epistemic_class,
                            "tags": r.tags,
                            "toolchain_version_source": getattr(r, "toolchain_version_source", None),
                        }
            except Exception:
                pass

        # 2. Fetch archived items
        archived_items: list[dict[str, Any]] = []
        if "archived_items" in tables:
            cur = con.execute("SELECT * FROM archived_items ORDER BY recorded_at ASC")
            ai_cols = [desc[0] for desc in cur.description]
            for row in cur.fetchall():
                archived_items.append(dict(zip(ai_cols, row)))

        # Collapse latest per item id
        archived_by_item_id: dict[str, dict[str, Any]] = {}
        for ai in archived_items:
            archived_by_item_id[ai["id"]] = ai

        # 3. Fetch supersession edges
        superseded_map: dict[str, str] = {}
        if "supersession_edges" in tables:
            try:
                rows = con.execute(
                    "SELECT from_id, to_id FROM supersession_edges WHERE from_type = 'run' AND state = 'active'"
                ).fetchall()
                for f_id, t_id in rows:
                    superseded_map[f_id] = t_id
            except Exception:
                pass

        # 4. Fetch blast radius records for these runs
        blast_records: dict[str, list[dict[str, Any]]] = {}
        if "blast_radius_ledger" in tables:
            try:
                placeholders = ", ".join(["?"] * len(run_ids))
                cur = con.execute(
                    f"SELECT * FROM blast_radius_ledger WHERE entity_type = 'run' AND entity_id IN ({placeholders}) "
                    "ORDER BY amended_at ASC",
                    run_ids,
                )
                br_cols = [desc[0] for desc in cur.description]
                for row in cur.fetchall():
                    d = dict(zip(br_cols, row))
                    blast_records.setdefault(d["entity_id"], []).append(d)
            except Exception:
                pass

        # 5. Load instrument registries
        # Map main_roots
        main_root_cache: dict[str, Path | None] = {}
        for r_id in run_ids:
            main_root_cache[r_id] = _find_run_main_root(r_id, cd, con=con, workspace_root=workspace_root)

        registries_by_root: dict[Path | None, dict[str, Any]] = {}
        for r_id, m_root in main_root_cache.items():
            if m_root not in registries_by_root:
                registries_by_root[m_root] = load_instrument_registries(m_root)

    finally:
        if should_close:
            con.close()

    results: dict[str, ValidityResult] = {}
    for r_id in run_ids:
        run = runs_by_id.get(r_id, {"id": r_id})
        evidence_tier = resolve_evidence_tier(
            run.get("stage_name"),
            run.get("epistemic_class"),
        )
        main_root = main_root_cache.get(r_id)
        registries = registries_by_root.get(main_root, {})

        # Step 1: Check archived items
        archived_match = None
        for item in archived_by_item_id.values():
            if item.get("event") != "archived":
                continue
            # Check run_ids
            raw_rids = item.get("run_ids")
            if raw_rids:
                try:
                    rids = json.loads(raw_rids) if isinstance(raw_rids, str) else raw_rids
                    if r_id in rids:
                        archived_match = item
                        break
                except Exception:
                    pass
            # Check script_sha256
            s_sha = run.get("script_sha256")
            if s_sha and s_sha in (item.get("script_sha256"), item.get("pre_archive_sha")):
                archived_match = item
                break
            # Check paths
            raw_paths = item.get("paths")
            if raw_paths:
                try:
                    paths = json.loads(raw_paths) if isinstance(raw_paths, str) else raw_paths
                    s_path = run.get("sidecar_path")
                    if s_path and s_path in paths:
                        archived_match = item
                        break
                except Exception:
                    pass

        if archived_match is not None:
            results[r_id] = ValidityResult(
                run_id=r_id,
                state="archived",
                reason=archived_match.get("reason") or f"archived under bundle {archived_match.get('bundle_sha256')}",
                replacement_id=None,
                version_source=run.get("toolchain_version_source") or "unknown",
                evidence_tier=evidence_tier,
                is_hidden=True,
            )
            continue

        # Step 2: Check supersession
        if r_id in superseded_map:
            succ_id = superseded_map[r_id]
            results[r_id] = ValidityResult(
                run_id=r_id,
                state="superseded",
                reason=f"superseded by {succ_id}",
                replacement_id=succ_id,
                version_source=run.get("toolchain_version_source") or "unknown",
                evidence_tier=evidence_tier,
                is_hidden=True,
            )
            continue

        # Step 3: Check blast radius ledger & exemptions
        exemptions: set[str] = set()
        br_state = None
        br_reason = None
        r_blast_records = blast_records.get(r_id, [])
        for rec in r_blast_records:
            if rec.get("to_state") == "cleared":
                a_kind = rec.get("anchor_kind")
                a_val = rec.get("anchor_value")
                if a_kind == "dependency_range" and a_val:
                    exemptions.add(a_val)
                else:
                    br_state = "cleared"
                    br_reason = rec.get("reason")
            elif rec.get("to_state") in ("affected", "unverifiable"):
                a_kind = rec.get("anchor_kind")
                if a_kind != "dependency_range":
                    br_state = rec.get("to_state")
                    br_reason = rec.get("reason") or rec.get("match_reason")

        if br_state in ("affected", "unverifiable"):
            results[r_id] = ValidityResult(
                run_id=r_id,
                state=br_state,
                reason=br_reason or "flagged in blast radius ledger",
                replacement_id=None,
                version_source=run.get("toolchain_version_source") or "unknown",
                evidence_tier=evidence_tier,
                is_hidden=(br_state == "affected"),
            )
            continue

        # Step 4: Check instrument defect & estimand registries
        matched_defect: tuple[DefectEntry, str, bool, str | None] | None = None
        estimand_marker: str | None = None
        primary_version_source: str = run.get("toolchain_version_source") or "unknown"

        for pkg, registry in registries.items():
            version, commit, v_source = resolve_toolchain_version(run, main_root, pkg)
            if v_source != "unknown":
                primary_version_source = v_source

            for entry in registry.entries:
                # Per-revision exemption check
                exempt_key = f"{entry.id}@r{entry.revision}"
                if exempt_key in exemptions:
                    continue

                # Estimand change check (OD-6)
                if entry.kind == "estimand_change":
                    if v_source == "unknown":
                        estimand_marker = f"{pkg}:unknown"
                    else:
                        for r in entry.ranges:
                            if r.type == "ECOSYSTEM":
                                if version is not None and r.introduced:
                                    from packaging.version import Version
                                    try:
                                        v_clean = Version(Version(version).public)
                                        intro_clean = Version(Version(r.introduced).public)
                                        if v_clean >= intro_clean:
                                            estimand_marker = f"{pkg}:post-{entry.id}"
                                        else:
                                            estimand_marker = f"{pkg}:pre-{entry.id}"
                                        break
                                    except Exception:
                                        pass
                            elif r.type == "GIT":
                                target_commit = commit or run.get("git_hash")
                                repo_root = main_root or Path.cwd()
                                if target_commit and r.introduced:
                                    if target_commit == r.introduced or commit_in_range(target_commit, r.introduced, None, repo_root):
                                        estimand_marker = f"{pkg}:post-{entry.id}"
                                    else:
                                        estimand_marker = f"{pkg}:pre-{entry.id}"
                                    break
                    continue

                # Range check for defects
                matches_range = False
                for r in entry.ranges:
                    if r.type == "ECOSYSTEM":
                        if version is not None and version_in_range(version, r.introduced, r.fixed):
                            matches_range = True
                            break
                    elif r.type == "GIT":
                        target_commit = commit or run.get("git_hash")
                        repo_root = main_root or Path.cwd()
                        if target_commit and commit_in_range(target_commit, r.introduced, r.fixed, repo_root):
                            matches_range = True
                            break

                if matches_range:

                    # Defect predicate check (OD-2)
                    if entry.predicate:
                        pred_match, pred_err = evaluate_predicate(entry.predicate.condition, run)
                        if not pred_match:
                            if pred_err and "missing predicate field" in pred_err:
                                # OD-2: Resolve to suspect due to missing field
                                if entry.hide_inferred and v_source == "inferred-from-commit":
                                    d_state = "void"
                                    d_hidden = True
                                else:
                                    d_state = "suspect"
                                    d_hidden = False
                                matched_defect = (entry, d_state, d_hidden, pred_err)
                                break
                            else:
                                # Condition was False
                                continue

                    # Defect matched
                    if v_source == "inferred-from-commit":
                        if entry.hide_inferred:  # OD-1
                            d_state = entry.severity
                            d_hidden = True
                        else:
                            d_state = "suspect"
                            d_hidden = False
                    else:
                        d_state = entry.severity
                        d_hidden = (entry.severity == "void")

                    matched_defect = (
                        entry,
                        d_state,
                        d_hidden,
                        f"matches defect {entry.id}: {entry.description}" if entry.description else f"matches defect {entry.id}",
                    )
                    break

            if matched_defect:
                break

            # Unknown version checks for active defect / estimand
            if v_source == "unknown":
                active_defects = [e for e in registry.entries if e.kind == "defect"]
                if active_defects and not matched_defect:
                    first_def = active_defects[0]
                    matched_defect = (
                        first_def,
                        "unverifiable",
                        False,
                        f"toolchain version unresolvable for defect {first_def.id}",
                    )
                active_estimands = [e for e in registry.entries if e.kind == "estimand_change"]
                if active_estimands and not estimand_marker:
                    estimand_marker = f"{pkg}:unknown"

        if matched_defect is not None:
            entry, d_state, d_hidden, d_reason = matched_defect
            results[r_id] = ValidityResult(
                run_id=r_id,
                state=d_state,
                reason=d_reason,
                replacement_id=None,
                version_source=primary_version_source,
                evidence_tier=evidence_tier,
                is_hidden=d_hidden,
                defect_id=entry.id,
                defect_revision=entry.revision,
                estimand_marker=estimand_marker,
            )
            continue

        # Step 5: Default current
        results[r_id] = ValidityResult(
            run_id=r_id,
            state="current",
            reason=None,
            replacement_id=None,
            version_source=primary_version_source,
            evidence_tier=evidence_tier,
            is_hidden=False,
            estimand_marker=estimand_marker,
        )

    return results


def compute_distance_from_present(
    run: Any,
    validity: ValidityResult,
    main_root: Path | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
) -> dict[str, Any]:
    """Compute distance-from-present facts (§3.5).

    Returns:
    - age_days: float
    - versions_behind: int | None
    - script_commits_since: int | None
    - reproduction_count: int (excluding neutral labels)
    - n_units: int | None
    - version_source: str
    """
    ts = None
    if isinstance(run, dict):
        ts = run.get("timestamp")
    elif hasattr(run, "timestamp"):
        ts = getattr(run, "timestamp")

    age_days = 0.0
    if ts:
        if isinstance(ts, str):
            try:
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                age_days = round((datetime.now(UTC) - dt).total_seconds() / 86400.0, 1)
            except Exception:
                pass
        elif isinstance(ts, datetime):
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            age_days = round((datetime.now(UTC) - ts).total_seconds() / 86400.0, 1)

    git_hash = run.get("git_hash") if isinstance(run, dict) else getattr(run, "git_hash", None)
    script_path = run.get("sidecar_path") if isinstance(run, dict) else getattr(run, "sidecar_path", None)

    script_commits_since: int | None = None
    versions_behind: int | None = None
    if main_root is not None and main_root.is_dir() and git_hash and git_hash != "unknown":
        try:
            # Script commits since
            rel_script = None
            if script_path:
                try:
                    rel_script = Path(script_path).relative_to(main_root)
                except ValueError:
                    rel_script = Path(script_path)
            cmd = ["git", "-C", str(main_root), "rev-list", "--count", f"{git_hash}..HEAD"]
            if rel_script:
                cmd.extend(["--", str(rel_script)])
            res = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if res.returncode == 0 and res.stdout.strip().isdigit():
                script_commits_since = int(res.stdout.strip())

            # Versions behind (overall commits behind HEAD)
            res_all = subprocess.run(
                ["git", "-C", str(main_root), "rev-list", "--count", f"{git_hash}..HEAD"],
                capture_output=True,
                text=True,
                check=False,
            )
            if res_all.returncode == 0 and res_all.stdout.strip().isdigit():
                versions_behind = int(res_all.stdout.strip())
        except Exception:
            pass

    # Reproduction count: count of runs with same sidecar_sha256 whose outcome not in NEUTRAL_OUTCOME_LABELS
    sidecar_sha = run.get("sidecar_sha256") if isinstance(run, dict) else getattr(run, "sidecar_sha256", None)
    run_id = run.get("id") if isinstance(run, dict) else getattr(run, "id", None)
    reproduction_count = 0

    if sidecar_sha and con is not None:
        try:
            tables = {
                r[0]
                for r in con.execute(
                    "SELECT table_name FROM duckdb_tables()"
                ).fetchall()
            }
            if "runs" in tables:
                neutral_placeholders = ", ".join(["?"] * len(NEUTRAL_OUTCOME_LABELS))
                query = (
                    "SELECT count(*) FROM runs WHERE sidecar_sha256 = ? AND id != ? "
                    f"AND COALESCE(outcome, '') NOT IN ({neutral_placeholders})"
                )
                params = [sidecar_sha, run_id, *list(NEUTRAL_OUTCOME_LABELS)]
                row = con.execute(query, params).fetchone()
                if row:
                    reproduction_count = int(row[0])
        except Exception:
            pass

    return {
        "age_days": age_days,
        "versions_behind": versions_behind,
        "script_commits_since": script_commits_since,
        "reproduction_count": reproduction_count,
        "n_units": None,
        "version_source": validity.version_source,
    }
