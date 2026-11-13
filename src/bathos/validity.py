"""Validity Engine: Read-time validity resolution, cache management, and distance facts.

Spec 261106 §2.1, §2.2, §3.4, §3.5.
Invariant I1 (Non-Obliteration): base run records in cool Parquet fragments,
.bth/log/*.jsonl, and runs tables are never mutated or deleted.
"""

from __future__ import annotations

import ast
import contextlib
import fcntl
import functools
import hashlib
import json
import logging
import os
import re
import subprocess
import time
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from bathos.config import default_catalog_dir
from bathos.runlog.project_id import list_registered_roots
from bathos.version_resolver import (
    DefectEntry,
    _get_uv_lock_packages,
    commit_in_range,
    evaluate_predicate,
    is_self_package,
    is_valid_git_commit,
    key_prefix,
    load_instrument_registries,
    resolve_toolchain_version,
    version_in_range,
)

logger = logging.getLogger(__name__)

NEUTRAL_OUTCOME_LABELS = frozenset(
    {"", "unknown", "observed", "invalid_measurement", "error", "collected", "recorded", "reaped", "unassigned"}
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
    if catalog_dir is None:
        env_cat = os.environ.get("BTH_CATALOG_DIR")
        if env_cat:
            catalog_dir = Path(env_cat)
    return Path(catalog_dir or default_catalog_dir()) / "validity_cache.db"


def validity_cache_lock_path(catalog_dir: Path | str | None = None) -> Path:
    if catalog_dir is None:
        env_cat = os.environ.get("BTH_CATALOG_DIR")
        if env_cat:
            catalog_dir = Path(env_cat)
    return Path(catalog_dir or default_catalog_dir()) / "validity_cache.lock"


def resolve_evidence_tier(stage_name: str | None, epistemic_class: str | None) -> str:
    """Resolve evidence tier string according to Spec §4.2."""
    from bathos.sidecar import resolve_epistemic_tier

    return resolve_epistemic_tier(
        stage_name_declared=bool(stage_name),
        stage_name_raw=stage_name,
        epistemic_class=epistemic_class,
    )


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
        if r in (bth_home, user_home) or (r / ".bth").resolve() == bth_home:
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
            with contextlib.suppress(Exception):
                reg_files.append((str(p.resolve()), hashlib.sha256(p.read_bytes()).hexdigest()))

    # Project-local registries (<root>/.bth/instruments/*.toml)
    for root in valid_roots:
        p_inst = root / ".bth" / "instruments"
        if p_inst.is_dir():
            for p in sorted(p_inst.glob("*.toml")):
                with contextlib.suppress(Exception):
                    reg_files.append((str(p.resolve()), hashlib.sha256(p.read_bytes()).hexdigest()))

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
            con = connect_read(cd, missing="empty", wire_validity=False)
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

            con = connect_read(cd, missing="empty", wire_validity=False)
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
            run_ids = [r[0] for r in run_rows]
            validity_map = resolve_validity(run_ids, cd, con=con, workspace_root=workspace_root)
        finally:
            if should_close_con:
                con.close()
                con = None

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
                import pyarrow as pa

                vals = list(validity_map.values())
                n_vals = len(vals)
                tbl = pa.Table.from_pydict(
                    {
                        "run_id": [v.run_id for v in vals],
                        "validity_state": [v.state for v in vals],
                        "reason": [v.reason for v in vals],
                        "replacement_id": [v.replacement_id for v in vals],
                        "version_source": [v.version_source for v in vals],
                        "evidence_tier": [v.evidence_tier for v in vals],
                        "defect_id": [v.defect_id for v in vals],
                        "defect_revision": [v.defect_revision for v in vals],
                        "estimand_marker": [v.estimand_marker for v in vals],
                        "inputs_hash": [inputs_hash] * n_vals,
                        "cached_at": [now_iso] * n_vals,
                    }
                )
                w_con.register("_vc_insert", tbl)
                w_con.execute("INSERT INTO validity_cache SELECT * FROM _vc_insert")
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
            con = connect_read(cd, missing="empty", wire_validity=False)
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
        if d in (bth_home, user_home) or (d / ".bth").resolve() == bth_home:
            continue
        if d.name == ".bth":
            cand = d.parent
            if (cand / ".bth" / "instruments").is_dir():
                return cand
        elif (d / ".bth" / "instruments").is_dir():
            return d

    if not proj_slug:
        for root in reg_roots:
            if root.is_dir():
                return root

        cwd = Path.cwd()
        for d in [cwd, *cwd.parents]:
            if (d / ".bth").is_dir() or (d / ".bth.toml").is_file() or (d / "pyproject.toml").is_file():
                return d

    return None


_DEFECT_STATE_PRIORITY: dict[str, int] = {
    "void": 4,
    "affected": 3,
    "suspect": 2,
    "unverifiable": 1,
}


def _prefer_defect(
    curr: tuple[DefectEntry, str, bool, str | None, str] | None,
    cand: tuple[DefectEntry, str, bool, str | None, str],
) -> tuple[DefectEntry, str, bool, str | None, str]:
    """Select the higher-severity defect match (void > affected > suspect > unverifiable)."""
    if curr is None:
        return cand
    if _DEFECT_STATE_PRIORITY.get(cand[1], 0) > _DEFECT_STATE_PRIORITY.get(curr[1], 0):
        return cand
    return curr


def _run_explicitly_records_package(run: dict[str, Any], pkg: str) -> bool:
    """Return True if run's dependency_provenance or metadata explicitly mentions pkg."""
    pkg_key = key_prefix(pkg)
    dep_prov = run.get("dependency_provenance")
    if dep_prov:
        if isinstance(dep_prov, str):
            with contextlib.suppress(Exception):
                dep_prov = json.loads(dep_prov)
        if isinstance(dep_prov, dict):
            for k in (
                f"{pkg_key}_version",
                f"{pkg}_version",
                f"{pkg_key}_source_commit",
                f"{pkg}_source_commit",
            ):
                val = dep_prov.get(k)
                if val and str(val) != "unknown":
                    return True

    meta = run.get("metadata")
    if meta:
        if isinstance(meta, str):
            with contextlib.suppress(Exception):
                meta = json.loads(meta)
        if isinstance(meta, dict):
            for k in (
                f"{pkg}_version",
                f"{pkg_key}_version",
                f"{pkg}_installed_dist_version",
                f"{pkg_key}_installed_dist_version",
                f"scores_{pkg}_version",
                f"scores_{pkg_key}_version",
                f"tev_pinned_{pkg}_wheel",
                f"tev_pinned_{pkg_key}_wheel",
            ):
                val = meta.get(k)
                if val and str(val) != "unknown":
                    return True
    return False


def _resolve_run_script_file(run: dict[str, Any], main_root: Path | None) -> Path | None:
    """Resolve the run's .py script file on disk under main_root if it exists."""
    if main_root is None or not main_root.is_dir():
        return None
    cand_strs: list[str] = []
    cmd = run.get("command")
    if isinstance(cmd, str) and cmd:
        parts = cmd.split()
        for idx, p in enumerate(parts):
            if p.endswith(".py"):
                cand_strs.append(p)
            elif p == "-m" and idx + 1 < len(parts):
                cand_strs.append(parts[idx + 1].replace(".", "/") + ".py")
    raw_argv = run.get("argv")
    if raw_argv:
        argvs = raw_argv
        if isinstance(raw_argv, str):
            with contextlib.suppress(Exception):
                argvs = json.loads(raw_argv)
        if isinstance(argvs, (list, tuple)):
            for idx, arg in enumerate(argvs):
                if isinstance(arg, str):
                    if arg.endswith(".py"):
                        cand_strs.append(arg)
                    elif arg == "-m" and idx + 1 < len(argvs) and isinstance(argvs[idx + 1], str):
                        cand_strs.append(argvs[idx + 1].replace(".", "/") + ".py")
    s_path = run.get("sidecar_path")
    if isinstance(s_path, str) and s_path.endswith(".bth.toml"):
        cand_strs.append(s_path[:-9] + ".py")

    for c_str in cand_strs:
        p = Path(c_str)
        if not p.is_absolute():
            p = main_root / p
        with contextlib.suppress(Exception):
            if p.is_file():
                return p.resolve()
    return None


def _resolve_local_import_files(
    cur_file: Path,
    main_root: Path,
    text: str,
) -> list[Path]:
    """Resolve local .py files imported by cur_file within main_root."""
    cand_rel_modules: list[tuple[Path, str]] = []

    try:
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name:
                        cand_rel_modules.append((main_root, alias.name))
                        cand_rel_modules.append((main_root / "src", alias.name))
                        cand_rel_modules.append((cur_file.parent, alias.name))
            elif isinstance(node, ast.ImportFrom):
                base_dirs: list[Path] = []
                if node.level and node.level > 0:
                    rel_base = cur_file.parent
                    for _ in range(node.level - 1):
                        rel_base = rel_base.parent
                    base_dirs.append(rel_base)
                else:
                    base_dirs.extend([main_root, main_root / "src", cur_file.parent])

                mod_prefix = node.module or ""
                for bdir in base_dirs:
                    if mod_prefix:
                        cand_rel_modules.append((bdir, mod_prefix))
                    for alias in node.names:
                        if alias.name and alias.name != "*":
                            sub_mod = f"{mod_prefix}.{alias.name}" if mod_prefix else alias.name
                            cand_rel_modules.append((bdir, sub_mod))
    except SyntaxError:
        for m in re.finditer(r"^\s*(?:from|import)\s+([a-zA-Z0-9_.]+)", text, re.MULTILINE):
            mod_name = m.group(1).lstrip(".")
            if mod_name:
                cand_rel_modules.append((main_root, mod_name))
                cand_rel_modules.append((main_root / "src", mod_name))
                cand_rel_modules.append((cur_file.parent, mod_name))

    resolved: list[Path] = []
    seen: set[Path] = set()
    for base_dir, dotted in cand_rel_modules:
        rel_slash = dotted.replace(".", "/")
        for cand in (base_dir / f"{rel_slash}.py", base_dir / rel_slash / "__init__.py"):
            with contextlib.suppress(OSError):
                if cand.is_file():
                    rp = cand.resolve()
                    if rp not in seen:
                        seen.add(rp)
                        resolved.append(rp)
    return resolved


@functools.lru_cache(maxsize=512)
def _script_file_mentions_package(script_path_str: str, main_root_str: str, pkg_key: str) -> bool:
    """Return True if script_path (or its local imports in main_root up to depth 4) mentions pkg_key."""
    script_path = Path(script_path_str)
    main_root = Path(main_root_str)
    pkg_alt = pkg_key.replace("_", "-")
    pat = re.compile(rf"\b(?:{re.escape(pkg_key)}|{re.escape(pkg_alt)})\b", re.IGNORECASE)

    queue: list[tuple[Path, int]] = [(script_path, 0)]
    visited: set[Path] = set()

    while queue:
        cur_path, depth = queue.pop(0)
        with contextlib.suppress(OSError):
            cur_resolved = cur_path.resolve()
            if cur_resolved in visited:
                continue
            visited.add(cur_resolved)

        try:
            text = cur_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            if depth == 0:
                return True
            continue

        if pat.search(text):
            return True

        if depth < 4:
            for child_path in _resolve_local_import_files(cur_path, main_root, text):
                if child_path not in visited:
                    queue.append((child_path, depth + 1))

    return False


def _resolve_pkg_git_repo(
    pkg: str,
    self_pkg: bool,
    target_commit: str | None,
    introduced_commit: str | None,
    main_root: Path | None,
) -> Path:
    """Resolve the git repository root for evaluating a GIT defect/estimand range."""
    base_root = main_root or Path.cwd()
    if self_pkg or main_root is None:
        return base_root

    # If the commit exists in main_root (e.g. synthetic test cohorts), use main_root
    check_sha = target_commit or introduced_commit
    if check_sha and is_valid_git_commit(check_sha, base_root):
        return base_root

    # Otherwise check sibling repository (<parent>/<pkg> or <parent>/<pkg_key>)
    pkg_key = key_prefix(pkg)
    for cand_name in (pkg, pkg_key, pkg.replace("_", "-")):
        sibling = main_root.parent / cand_name
        if (
            sibling.is_dir()
            and (sibling / ".git").exists()
            and (not check_sha or is_valid_git_commit(check_sha, sibling))
        ):
            return sibling
    return base_root


@functools.lru_cache(maxsize=256)
def _read_uv_lock_pkg_keys(lock_file_str: str, mtime_ns: int) -> frozenset[str] | None:  # noqa: ARG001 -- mtime_ns is the lru_cache invalidation key
    with contextlib.suppress(Exception):
        lock_doc = tomllib.loads(Path(lock_file_str).read_text(encoding="utf-8"))
        names: set[str] = set()
        for p in lock_doc.get("package", []):
            n = p.get("name", "")
            if n:
                names.add(n)
                names.add(key_prefix(n))
        return frozenset(names)
    return None


def _is_external_pkg_absent(
    run: dict[str, Any],
    main_root: Path | None,
    pkg: str,
    catalog_dir: Path | str | None = None,
) -> bool:
    """Return True if external pkg is absent from the project's lockfile and not declared project-locally."""
    if main_root is None or not main_root.is_dir():
        return False
    pkg_key = key_prefix(pkg)
    git_hash = run.get("git_hash")
    if git_hash and git_hash != "unknown":
        cat_str = str(catalog_dir) if catalog_dir is not None else None
        calc_sha, pkgs = _get_uv_lock_packages(str(main_root), git_hash, catalog_dir=cat_str)
        if calc_sha is not None:
            return not any(name == pkg or key_prefix(name) == pkg_key for name in pkgs)
    proj_inst = main_root / ".bth" / "instruments" / f"{pkg}.toml"
    if not proj_inst.is_file():
        lock_file = main_root / "uv.lock"
        if lock_file.is_file():
            with contextlib.suppress(OSError):
                keys = _read_uv_lock_pkg_keys(str(lock_file), lock_file.stat().st_mtime_ns)
                if keys is not None:
                    return pkg not in keys and pkg_key not in keys
        return True
    return False


def resolve_validity(
    run_ids: list[str],
    catalog_dir: Path | str | None = None,
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

    start_time = time.perf_counter()

    if con is None and db is not None:
        con = db

    if catalog_dir is None:
        catalog_dir = os.environ.get("BTH_CATALOG_DIR") or default_catalog_dir()
    cd = Path(catalog_dir)
    should_close = False
    if con is None:
        from bathos.index import connect_read

        con = connect_read(cd, missing="empty", wire_validity=False)
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
            # Also resolve any 8-char prefix run_ids passed by findings/verdicts
            for r_id in run_ids:
                if r_id not in runs_by_id and 8 <= len(r_id) < 36:
                    with contextlib.suppress(Exception):
                        p_cur = con.execute("SELECT * FROM runs WHERE id LIKE ? LIMIT 1", [f"{r_id}%"])
                        p_row = p_cur.fetchone()
                        if p_row is not None:
                            p_cols = [desc[0] for desc in p_cur.description]
                            runs_by_id[r_id] = dict(zip(p_cols, p_row))
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
                            "dependency_provenance": r.dependency_provenance,
                            "dependency_lock_sha256": r.dependency_lock_sha256,
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
                cols = [c[1] for c in con.execute("PRAGMA table_info('supersession_edges')").fetchall()]
                if "entity_type" in cols:
                    rows = con.execute(
                        "SELECT entity_id, superseded_by_id FROM supersession_edges WHERE entity_type = 'run'"
                    ).fetchall()
                else:
                    rows = con.execute(
                        "SELECT from_id, to_id FROM supersession_edges WHERE from_type = 'run'"
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
        # Map main_roots (batched)
        main_root_cache: dict[str, Path | None] = {}
        missing_root_ids: list[str] = []

        for r_id in run_ids:
            if workspace_root and workspace_root.is_dir():
                main_root_cache[r_id] = workspace_root
            else:
                missing_root_ids.append(r_id)

        if missing_root_ids:
            slug_to_root: dict[str, Path | None] = {}
            # 1. Query distinct project -> main_root from events table once
            if con is not None and "events" in tables:
                with contextlib.suppress(Exception):
                    proj_rows = con.execute(
                        "SELECT DISTINCT project, main_root FROM events "
                        "WHERE project IS NOT NULL AND main_root IS NOT NULL"
                    ).fetchall()
                    for proj_s, mr in proj_rows:
                        if proj_s and mr and proj_s not in slug_to_root:
                            p = Path(mr)
                            if p.is_dir():
                                slug_to_root[str(proj_s)] = p

                    if len(missing_root_ids) <= 256:
                        placeholders = ", ".join(["?"] * len(missing_root_ids))
                        event_rows = con.execute(
                            f"SELECT entity_key, main_root FROM events WHERE entity_key IN ({placeholders}) AND main_root IS NOT NULL",
                            missing_root_ids,
                        ).fetchall()
                        for ek, mr in event_rows:
                            if mr:
                                p = Path(mr)
                                if p.is_dir():
                                    main_root_cache[ek] = p

            # 2. Populate slug_to_root from registered roots once
            reg_roots = list_registered_roots()
            for root in reg_roots:
                if root.is_dir() and (root / ".bth.toml").is_file():
                    with contextlib.suppress(Exception):
                        doc = tomllib.loads((root / ".bth.toml").read_text())
                        s_val = doc.get("project", {}).get("slug")
                        if s_val and str(s_val) not in slug_to_root:
                            slug_to_root[str(s_val)] = root

            fallback_no_slug_root: Path | None = None
            computed_fallback = False

            for r_id in missing_root_ids:
                if r_id in main_root_cache:
                    continue
                p_slug = runs_by_id.get(r_id, {}).get("project_slug")
                if p_slug:
                    main_root_cache[r_id] = slug_to_root.get(str(p_slug))
                else:
                    if not computed_fallback:
                        fallback_no_slug_root = _find_run_main_root(
                            r_id, cd, con=con, workspace_root=workspace_root
                        )
                        computed_fallback = True
                    main_root_cache[r_id] = fallback_no_slug_root

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
        run_proj = run.get("project_slug")
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
            # Check paths with project_slug guard
            if run_proj and item.get("project_slug") == run_proj:
                raw_paths = item.get("paths")
                if raw_paths:
                    try:
                        paths = set(json.loads(raw_paths) if isinstance(raw_paths, str) else raw_paths)
                        cand_paths: set[str] = set()
                        s_path = run.get("sidecar_path")
                        if s_path:
                            cand_paths.add(s_path)
                        raw_outputs = run.get("output_paths")
                        if raw_outputs:
                            outs = json.loads(raw_outputs) if isinstance(raw_outputs, str) else raw_outputs
                            if isinstance(outs, (list, tuple, set)):
                                cand_paths.update(str(p) for p in outs)
                            elif isinstance(outs, str):
                                cand_paths.add(outs)
                        # Extract script path from command
                        cmd = run.get("command")
                        if cmd:
                            cmd_parts = cmd.split()
                            for part in cmd_parts:
                                if part.endswith((".py", ".sh", ".bash", ".slurm", ".rs", ".jl", ".r")):
                                    cand_paths.add(part)
                                    break
                            else:
                                if cmd_parts:
                                    cand_paths.add(cmd_parts[0])
                        # Extract script path from argv
                        raw_argv = run.get("argv")
                        if raw_argv:
                            argvs = json.loads(raw_argv) if isinstance(raw_argv, str) else raw_argv
                            if isinstance(argvs, (list, tuple)):
                                for arg in argvs:
                                    if isinstance(arg, str) and arg.endswith((".py", ".sh", ".bash", ".slurm", ".rs", ".jl", ".r")):
                                        cand_paths.add(arg)
                                        break
                                else:
                                    if argvs and isinstance(argvs[0], str):
                                        cand_paths.add(argvs[0])
                        if cand_paths & paths:
                            archived_match = item
                            break
                    except Exception:
                        pass

        if archived_match is not None:
            results[r_id] = ValidityResult(
                run_id=r_id,
                state="archived",
                reason=archived_match.get("reason") or f"archived under bundle {archived_match.get('bundle_sha256')}",
                replacement_id=archived_match.get("superseded_by"),
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
        matched_defect: tuple[DefectEntry, str, bool, str | None, str] | None = None
        estimand_marker: str | None = None
        primary_version_source: str = run.get("toolchain_version_source") or "unknown"

        for pkg, registry in registries.items():
            self_pkg = is_self_package(pkg, run, main_root)
            explicit_pkg = _run_explicitly_records_package(run, pkg)

            if not self_pkg and not explicit_pkg:
                script_file = _resolve_run_script_file(run, main_root)
                if script_file is not None and not _script_file_mentions_package(
                    str(script_file), str(main_root), key_prefix(pkg)
                ):
                    continue

            version, commit, v_source = resolve_toolchain_version(run, main_root, pkg, catalog_dir=cd)

            if not self_pkg and not explicit_pkg and v_source == "unknown" and _is_external_pkg_absent(
                run, main_root, pkg, catalog_dir=cd
            ):
                continue

            if v_source != "unknown" and (primary_version_source == "unknown" or self_pkg or explicit_pkg):
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
                                target_commit = commit or (run.get("git_hash") if self_pkg else None)
                                repo_root = _resolve_pkg_git_repo(
                                    pkg, self_pkg, target_commit, r.introduced, main_root
                                )
                                if target_commit and r.introduced:
                                    if target_commit == r.introduced or commit_in_range(
                                        target_commit, r.introduced, None, repo_root
                                    ):
                                        estimand_marker = f"{pkg}:post-{entry.id}"
                                    else:
                                        estimand_marker = f"{pkg}:pre-{entry.id}"
                                    break
                    continue

                # Range check for defects
                matches_range = False
                effective_v_source = v_source
                for r in entry.ranges:
                    if r.type == "ECOSYSTEM":
                        if version is not None and version_in_range(version, r.introduced, r.fixed):
                            matches_range = True
                            break
                    elif r.type == "GIT":
                        target_commit = commit or (run.get("git_hash") if self_pkg else None)
                        repo_root = _resolve_pkg_git_repo(
                            pkg, self_pkg, target_commit, r.introduced, main_root
                        )
                        fixed_targets = r.fixed_all if r.fixed_all else (r.fixed,)
                        if target_commit and commit_in_range(target_commit, r.introduced, fixed_targets, repo_root):
                            matches_range = True
                            if commit is None and v_source != "recorded":
                                effective_v_source = "inferred-from-commit"
                            break

                if matches_range:
                    # Defect predicate check (OD-2)
                    if entry.predicate:
                        pred_match, pred_err = evaluate_predicate(entry.predicate.condition, run)
                        if not pred_match:
                            if pred_err and "missing predicate field" in pred_err:
                                # OD-2: Resolve to suspect due to missing field
                                if entry.hide_inferred and effective_v_source == "inferred-from-commit":
                                    d_state = "void"
                                    d_hidden = True
                                else:
                                    d_state = "suspect"
                                    d_hidden = False
                                matched_defect = _prefer_defect(
                                    matched_defect,
                                    (entry, d_state, d_hidden, pred_err, effective_v_source),
                                )
                                continue
                            else:
                                # Condition was False
                                continue

                    # Defect matched
                    if effective_v_source == "inferred-from-commit":
                        if entry.hide_inferred:  # OD-1
                            d_state = entry.severity
                            d_hidden = True
                        else:
                            d_state = "suspect"
                            d_hidden = False
                    else:
                        d_state = entry.severity
                        d_hidden = (entry.severity == "void")

                    matched_defect = _prefer_defect(
                        matched_defect,
                        (
                            entry,
                            d_state,
                            d_hidden,
                            f"matches defect {entry.id}: {entry.description}"
                            if entry.description
                            else f"matches defect {entry.id}",
                            effective_v_source,
                        ),
                    )

            # Unknown version checks for active defect / estimand
            if v_source == "unknown":
                active_defects = [e for e in registry.entries if e.kind == "defect"]
                if active_defects:
                    first_def = active_defects[0]
                    matched_defect = _prefer_defect(
                        matched_defect,
                        (
                            first_def,
                            "unverifiable",
                            False,
                            f"toolchain version unresolvable for defect {first_def.id}",
                            v_source,
                        ),
                    )
                active_estimands = [e for e in registry.entries if e.kind == "estimand_change"]
                if active_estimands and not estimand_marker:
                    estimand_marker = f"{pkg}:unknown"

        if matched_defect is not None:
            entry, d_state, d_hidden, d_reason, matched_v_src = matched_defect
            results[r_id] = ValidityResult(
                run_id=r_id,
                state=d_state,
                reason=d_reason,
                replacement_id=None,
                version_source=matched_v_src if matched_v_src != "unknown" else primary_version_source,
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

    try:
        from bathos.telemetry import event

        dur_ms = max(0, int((time.perf_counter() - start_time) * 1000))
        hidden_cnt = sum(1 for v in results.values() if v.is_hidden)
        void_cnt = sum(1 for v in results.values() if v.state == "void")
        affected_cnt = sum(1 for v in results.values() if v.state == "affected")
        superseded_cnt = sum(1 for v in results.values() if v.state == "superseded")
        archived_cnt = sum(1 for v in results.values() if v.state == "archived")
        suspect_cnt = sum(1 for v in results.values() if v.state == "suspect")
        unverifiable_cnt = sum(1 for v in results.values() if v.state == "unverifiable")
        event(
            "validity.resolved",
            run_ids_count=len(run_ids),
            hidden_count=hidden_cnt,
            void_count=void_cnt,
            affected_count=affected_cnt,
            superseded_count=superseded_cnt,
            archived_count=archived_cnt,
            suspect_count=suspect_cnt,
            unverifiable_count=unverifiable_cnt,
            cache_hit=False,
            duration_ms=dur_ms,
        )
        if hidden_cnt > 0:
            event(
                "validity.filter_applied",
                filter_name="resolve_validity",
                hidden_count=hidden_cnt,
                total_count=len(run_ids),
            )
    except Exception:
        pass

    return results


def emit_validity_filter_applied(
    filter_name: str = "runs_current",
    hidden_count: int = 0,
    total_count: int = 0,
    **extra: Any,
) -> None:
    """Emit diagnostic validity.filter_applied telemetry."""
    try:
        from bathos.telemetry import event

        event(
            "validity.filter_applied",
            filter_name=filter_name,
            hidden_count=hidden_count,
            total_count=total_count,
            **extra,
        )
    except Exception:
        pass


def emit_validity_override_used(
    override_kind: str,
    entity_id: str = "",
    reason: str = "",
    **extra: Any,
) -> None:
    """Emit diagnostic validity.override_used telemetry."""
    try:
        from bathos.telemetry import event

        event(
            "validity.override_used",
            override_kind=override_kind,
            entity_id=entity_id,
            reason=reason,
            **extra,
        )
    except Exception:
        pass


def compute_distance_from_present(
    run: Any,
    validity: ValidityResult,
    main_root: Path | None = None,
    con: duckdb.DuckDBPyConnection | None = None,
    catalog_dir: Path | str | None = None,
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
    run_ts = run.get("timestamp") if isinstance(run, dict) else getattr(run, "timestamp", None)
    run_outcome = run.get("outcome") if isinstance(run, dict) else getattr(run, "outcome", None)
    run_slug = run.get("project_slug") if isinstance(run, dict) else getattr(run, "project_slug", None)
    reproduction_count = 0

    # Spec §1.4.C.3: Exploratory/neutral runs never have confirmatory reproductions
    if (
        sidecar_sha
        and run_id
        and run_ts
        and run_outcome
        and run_slug
        and run_outcome not in NEUTRAL_OUTCOME_LABELS
    ):
        ts_val = run_ts
        if isinstance(ts_val, datetime):
            if ts_val.tzinfo is None:
                ts_val = ts_val.replace(tzinfo=UTC)
            ts_str = ts_val.isoformat()
        else:
            ts_str = str(ts_val)

        close_con = False
        db_con = con
        if db_con is None:
            try:
                from bathos.index import connect_read

                db_con = connect_read(read_only=True)
                close_con = True
            except Exception:
                db_con = None

        if db_con is not None:
            try:
                tables = {
                    r[0]
                    for r in db_con.execute(
                        "SELECT table_name FROM duckdb_tables()"
                    ).fetchall()
                }
                has_runs_current = False
                try:
                    db_con.execute("SELECT 1 FROM runs_current LIMIT 0")
                    has_runs_current = True
                except Exception:
                    has_runs_current = False

                tbl_target = "runs_current" if has_runs_current else "runs"
                if tbl_target in tables or has_runs_current:
                    query = f"""
                        SELECT id FROM {tbl_target}
                        WHERE sidecar_sha256 = ?
                          AND id != ?
                          AND project_slug = ?
                          AND timestamp > ?
                          AND status = 'completed'
                          AND outcome = ?
                    """
                    try:
                        rows = db_con.execute(
                            query,
                            [sidecar_sha, run_id, run_slug, ts_val, run_outcome],
                        ).fetchall()
                    except Exception:
                        rows = db_con.execute(
                            query,
                            [sidecar_sha, run_id, run_slug, ts_str, run_outcome],
                        ).fetchall()

                    cand_ids = [r[0] for r in rows if r[0]]
                    if cand_ids:
                        try:
                            cd = Path(catalog_dir or os.environ.get("BTH_CATALOG_DIR") or default_catalog_dir())
                            v_map = resolve_validity(
                                cand_ids,
                                catalog_dir=cd,
                                con=db_con,
                                workspace_root=main_root,
                            )
                            reproduction_count = sum(
                                1 for cid in cand_ids if v_map.get(cid) and v_map[cid].state == "current"
                            )
                        except Exception:
                            try:
                                q_vc = f"SELECT run_id FROM validity_cache WHERE run_id IN ({','.join(['?']*len(cand_ids))}) AND validity_state != 'current'"
                                bad_ids = {r[0] for r in db_con.execute(q_vc, cand_ids).fetchall()}
                                reproduction_count = len([cid for cid in cand_ids if cid not in bad_ids])
                            except Exception:
                                reproduction_count = len(cand_ids)
            except Exception:
                pass
            finally:
                if close_con:
                    with contextlib.suppress(Exception):
                        db_con.close()

    return {
        "age_days": age_days,
        "versions_behind": versions_behind,
        "script_commits_since": script_commits_since,
        "reproduction_count": reproduction_count,
        "n_units": None,
        "version_source": validity.version_source,
    }
