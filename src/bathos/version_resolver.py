"""4-tier toolchain version resolution, instrument defect registry parsing, and predicate checking.

Spec 261106 §1.3.C, §2.1, §3.2, §3.3.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import logging
import os
import re
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
from packaging.version import Version

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DefectPredicate:
    condition: str  # DuckDB SQL condition, e.g. "sample_s > 1"


@dataclass(frozen=True)
class DefectRange:
    type: str  # "ECOSYSTEM" | "GIT"
    introduced: str | None = None
    fixed: str | None = None


@dataclass(frozen=True)
class DefectEntry:
    id: str  # e.g. "AMINX-2026-001"
    revision: int = 1
    kind: str = "defect"  # "defect" | "estimand_change"
    severity: str = "void"  # "void" | "suspect"
    description: str = ""
    hide_inferred: bool = False  # OD-1: if true, inferred match resolves to void instead of suspect
    ranges: list[DefectRange] = field(default_factory=list)
    predicate: DefectPredicate | None = None


@dataclass(frozen=True)
class InstrumentRegistry:
    package: str
    ecosystem: str = "PyPI"
    entries: list[DefectEntry] = field(default_factory=list)


def key_prefix(dist_name: str) -> str:
    """PEP 503-normalised name with separators as underscore."""
    return re.sub(r"[-_.]+", "_", dist_name).lower()


def parse_instrument_registry(path: Path) -> InstrumentRegistry:
    """Parse a TOML defect registry file according to OSV-inspired schema v1.0.0."""
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    pkg = data.get("package") or path.stem
    eco = data.get("ecosystem", "PyPI")
    entries: list[DefectEntry] = []
    for raw in data.get("entries", []):
        ranges: list[DefectRange] = []
        for r in raw.get("ranges", []):
            events = r.get("events", [])
            intro = None
            fixed = None
            for ev in events:
                if "introduced" in ev:
                    intro = str(ev["introduced"])
                if "fixed" in ev:
                    fixed = str(ev["fixed"])
            ranges.append(
                DefectRange(
                    type=r.get("type", "ECOSYSTEM"),
                    introduced=intro,
                    fixed=fixed,
                )
            )
        pred: DefectPredicate | None = None
        if "predicate" in raw and isinstance(raw["predicate"], dict):
            cond = raw["predicate"].get("condition")
            if cond:
                pred = DefectPredicate(condition=cond)
        entries.append(
            DefectEntry(
                id=raw["id"],
                revision=int(raw.get("revision", 1)),
                kind=raw.get("kind", "defect"),
                severity=raw.get("severity", "void"),
                description=raw.get("description", ""),
                hide_inferred=bool(raw.get("hide_inferred", False)),
                ranges=ranges,
                predicate=pred,
            )
        )
    return InstrumentRegistry(package=pkg, ecosystem=eco, entries=entries)


def _should_use_user_registry(main_root: Path | None = None) -> bool:
    env_val = os.environ.get("BTH_USE_USER_REGISTRY", "").strip().lower()
    if env_val in ("1", "true", "yes"):
        return True
    if main_root is not None:
        bth_toml = main_root / ".bth.toml"
        if bth_toml.is_file():
            try:
                data = tomllib.loads(bth_toml.read_text(encoding="utf-8"))
                if data.get("instruments", {}).get("use_user_registry") is True:
                    return True
            except Exception:
                pass
        pyproj = main_root / "pyproject.toml"
        if pyproj.is_file():
            try:
                data = tomllib.loads(pyproj.read_text(encoding="utf-8"))
                if (
                    data.get("tool", {})
                    .get("bathos", {})
                    .get("instruments", {})
                    .get("use_user_registry")
                    is True
                ):
                    return True
            except Exception:
                pass
    return False


def load_instrument_registries(
    main_root: Path | None = None,
    *,
    use_user_registry: bool | None = None,
) -> dict[str, InstrumentRegistry]:
    """Load instrument registries.

    Order of precedence:
    - User-global: ${XDG_CONFIG_HOME:-~/.config}/bathos/instruments/*.toml (if use_user_registry)
    - Project-local: <main_root>/.bth/instruments/*.toml
    Project-local entries override user-global entries with identical id.
    Never uses ~/.bth/instruments/.
    """
    if use_user_registry is None:
        use_user_registry = _should_use_user_registry(main_root)

    registries_by_pkg: dict[str, list[DefectEntry]] = {}
    ecosystems: dict[str, str] = {}

    # 1. User-global registry (opted-in)
    if use_user_registry:
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        if xdg_config:
            user_inst_dir = Path(xdg_config) / "bathos" / "instruments"
        else:
            user_inst_dir = Path.home() / ".config" / "bathos" / "instruments"
        if user_inst_dir.is_dir():
            for p in sorted(user_inst_dir.glob("*.toml")):
                try:
                    reg = parse_instrument_registry(p)
                    ecosystems[reg.package] = reg.ecosystem
                    for e in reg.entries:
                        registries_by_pkg.setdefault(reg.package, []).append(e)
                except Exception as exc:
                    logger.warning("Failed to parse user registry %s: %s", p, exc)

    # 2. Project-local registry (overlay, overrides by entry id)
    if main_root is not None:
        proj_inst_dir = main_root / ".bth" / "instruments"
        if proj_inst_dir.is_dir():
            for p in sorted(proj_inst_dir.glob("*.toml")):
                try:
                    reg = parse_instrument_registry(p)
                    ecosystems[reg.package] = reg.ecosystem
                    existing = registries_by_pkg.setdefault(reg.package, [])
                    for e in reg.entries:
                        existing = [x for x in existing if x.id != e.id]
                        existing.append(e)
                    registries_by_pkg[reg.package] = existing
                except Exception as exc:
                    logger.warning("Failed to parse project registry %s: %s", p, exc)

    result: dict[str, InstrumentRegistry] = {}
    for pkg, entries in registries_by_pkg.items():
        result[pkg] = InstrumentRegistry(
            package=pkg,
            ecosystem=ecosystems.get(pkg, "PyPI"),
            entries=entries,
        )
    return result


def load_instrument_registry_packages(start: Path) -> list[str]:
    """Find package names declared in any instrument registry accessible from start."""
    pkgs: set[str] = set()

    # Search upwards for .bth/instruments
    curr = start.resolve()
    for directory in [curr, *curr.parents]:
        inst_dir = directory / ".bth" / "instruments"
        if inst_dir.is_dir():
            for p in inst_dir.glob("*.toml"):
                try:
                    reg = parse_instrument_registry(p)
                    pkgs.add(reg.package)
                except Exception:
                    pkgs.add(p.stem)
            break

    # Also check user registry if enabled
    if _should_use_user_registry(start):
        xdg_config = os.environ.get("XDG_CONFIG_HOME")
        if xdg_config:
            user_inst_dir = Path(xdg_config) / "bathos" / "instruments"
        else:
            user_inst_dir = Path.home() / ".config" / "bathos" / "instruments"
        if user_inst_dir.is_dir():
            for p in user_inst_dir.glob("*.toml"):
                try:
                    reg = parse_instrument_registry(p)
                    pkgs.add(reg.package)
                except Exception:
                    pkgs.add(p.stem)

    return sorted(pkgs)


def version_in_range(
    v_str: str,
    introduced: str | None = None,
    fixed: str | None = None,
    last_affected: str | None = None,
) -> bool:
    """Evaluate whether version v_str is within [introduced, fixed) or [introduced, last_affected].

    PEP 440 local versions (+armask) are stripped to public version (removing local tag)
    unless the boundary explicitly specifies '+'.
    """
    try:
        v = Version(v_str)
    except Exception:
        return False

    if introduced:
        try:
            if "+" in introduced:
                intro_v = Version(introduced)
                intro_target = v
            else:
                intro_v = Version(introduced)
                intro_target = Version(v.public)
            if intro_target < intro_v:
                return False
        except Exception:
            return False

    if fixed:
        try:
            if "+" in fixed:
                fixed_v = Version(fixed)
                fixed_target = v
            else:
                fixed_v = Version(fixed)
                fixed_target = Version(v.public)
            if fixed_target >= fixed_v:
                return False
        except Exception:
            return False

    if last_affected:
        try:
            if "+" in last_affected:
                la_v = Version(last_affected)
                la_target = v
            else:
                la_v = Version(last_affected)
                la_target = Version(v.public)
            if la_target > la_v:
                return False
        except Exception:
            return False

    return True


@functools.lru_cache(maxsize=2048)
def is_git_ancestor(ancestor: str, descendant: str, repo_root: Path) -> bool:
    """True iff ancestor is an ancestor of descendant in repo_root."""
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_root), "merge-base", "--is-ancestor", ancestor, descendant],
            capture_output=True,
            check=False,
        )
        return res.returncode == 0
    except Exception:
        return False


def commit_in_range(
    commit_sha: str,
    introduced: str | None,
    fixed: str | None,
    repo_root: Path,
) -> bool:
    """Evaluate commit ancestry: commit matches iff introduced is ancestor and fixed is NOT ancestor."""
    if not commit_sha:
        return False
    if introduced and not is_git_ancestor(introduced, commit_sha, repo_root):
        return False
    return not (fixed and is_git_ancestor(fixed, commit_sha, repo_root))


@functools.lru_cache(maxsize=128)
def extract_predicate_columns(condition: str) -> list[str]:
    """Extract referenced column names from DuckDB SQL expression AST."""
    if not condition or not condition.strip():
        return []
    try:
        row = (
            duckdb.connect()
            .execute("SELECT json_serialize_sql(?)", [f"SELECT ({condition})"])
            .fetchone()
        )
    except Exception:
        return []
    if not row or not row[0]:
        return []

    parsed = json.loads(row[0])
    cols: list[str] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("class") == "COLUMN_REF":
                names = node.get("column_names") or []
                cols.extend(names)
            for val in node.values():
                _walk(val)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(parsed)
    return cols


def _get_run_field(run: Any, field_name: str) -> Any:
    # 1. Top-level
    if isinstance(run, dict):
        if field_name in run and run[field_name] is not None:
            return run[field_name]
    elif hasattr(run, field_name):
        val = getattr(run, field_name)
        if val is not None:
            return val

    # 2. metadata
    meta = None
    if isinstance(run, dict):
        meta = run.get("metadata")
    elif hasattr(run, "metadata"):
        meta = getattr(run, "metadata")
    if meta:
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = None
        if isinstance(meta, dict) and field_name in meta and meta[field_name] is not None:
            return meta[field_name]

    # 3. argv
    argv = None
    if isinstance(run, dict):
        argv = run.get("argv")
    elif hasattr(run, "argv"):
        argv = getattr(run, "argv")
    if argv:
        if isinstance(argv, str):
            try:
                argv = json.loads(argv)
            except Exception:
                try:
                    import shlex

                    argv = shlex.split(argv)
                except Exception:
                    argv = None
        if isinstance(argv, (list, tuple)):
            # Check --field_name or --field-name
            candidates = [
                f"--{field_name}",
                f"--{field_name.replace('_', '-')}",
            ]
            for i, arg in enumerate(argv):
                for cand in candidates:
                    val_str = None
                    if arg == cand and i + 1 < len(argv):
                        val_str = argv[i + 1]
                    elif arg.startswith(f"{cand}="):
                        val_str = arg.split("=", 1)[1]

                    if val_str is not None:
                        # Strip surrounding quotes if present (e.g. --sample_s="4")
                        if (
                            isinstance(val_str, str)
                            and len(val_str) >= 2
                            and val_str[0] in ('"', "'")
                            and val_str[-1] == val_str[0]
                        ):
                            val_str = val_str[1:-1]
                        try:
                            return int(val_str)
                        except (ValueError, TypeError):
                            try:
                                return float(val_str)
                            except (ValueError, TypeError):
                                return val_str

    return None


def evaluate_predicate(condition: str, run: Any) -> tuple[bool, str | None]:
    """Evaluate predicate condition against run.

    OD-2: First check presence of referenced columns across run top-level, metadata, and argv.
    If any field is missing, return (False, f"missing predicate field: {field}").
    If all present, evaluate SQL condition in DuckDB.
    """
    cols = extract_predicate_columns(condition)
    values: dict[str, Any] = {}
    for c in cols:
        val = _get_run_field(run, c)
        if val is None:
            return False, f"missing predicate field: {c}"
        values[c] = val

    # Evaluate SQL condition using a temporary PyArrow table in DuckDB
    try:
        tbl = pa.Table.from_pydict({k: [v] for k, v in values.items()})
        con = duckdb.connect()
        con.register("_run_row", tbl)
        res = con.execute(f"SELECT ({condition}) FROM _run_row").fetchone()
        if res and res[0] is not None:
            return bool(res[0]), None
        return False, None
    except Exception as exc:
        return False, f"predicate evaluation error: {exc}"


def _read_version_cache(
    main_root_str: str, git_hash: str
) -> tuple[str | None, dict[str, tuple[str, str | None]]] | None:
    # 1. Check <main_root>/.bth/cache/version_cache.json
    try:
        json_path = Path(main_root_str) / ".bth" / "cache" / "version_cache.json"
        if json_path.is_file():
            doc = json.loads(json_path.read_text(encoding="utf-8"))
            entry = doc.get(git_hash)
            if entry and isinstance(entry, dict):
                calc_sha = entry.get("lock_sha256")
                pkgs_raw = entry.get("packages", {})
                pkgs = {
                    k: (v[0], v[1])
                    for k, v in pkgs_raw.items()
                    if isinstance(v, (list, tuple)) and len(v) >= 2
                }
                if calc_sha is not None:
                    return calc_sha, pkgs
    except Exception:
        pass

    # 2. Check validity_cache.db:version_cache
    try:
        from bathos.config import default_catalog_dir
        from bathos.validity import ensure_validity_cache, validity_cache_db_path

        cat_dir = os.environ.get("BTH_CATALOG_DIR") or default_catalog_dir()
        db_path = validity_cache_db_path(cat_dir)
        if db_path.is_file():
            with duckdb.connect(str(db_path), read_only=True) as con:
                tables = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
            if "version_cache" not in tables and Path(cat_dir).is_dir():
                with contextlib.suppress(Exception):
                    ensure_validity_cache(cat_dir)
                    with duckdb.connect(str(db_path), read_only=True) as con:
                        tables = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
            if "version_cache" in tables:
                with duckdb.connect(str(db_path), read_only=True) as con:
                    rows = con.execute(
                        "SELECT lock_sha256, package_name, resolved_version, resolved_commit "
                        "FROM version_cache WHERE main_root = ? AND git_hash = ?",
                        [main_root_str, git_hash],
                    ).fetchall()
                    if rows:
                        calc_sha = rows[0][0]
                        pkgs = {r[1]: (r[2], r[3]) for r in rows}
                        return calc_sha, pkgs
    except Exception:
        pass

    return None


def _write_version_cache(
    main_root_str: str,
    git_hash: str,
    calc_sha: str,
    pkgs: dict[str, tuple[str, str | None]],
) -> None:
    # 1. Update <main_root>/.bth/cache/version_cache.json
    try:
        cache_dir = Path(main_root_str) / ".bth" / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        json_path = cache_dir / "version_cache.json"
        doc = {}
        if json_path.is_file():
            with contextlib.suppress(Exception):
                doc = json.loads(json_path.read_text(encoding="utf-8"))
        doc[git_hash] = {
            "lock_sha256": calc_sha,
            "packages": {pkg: [ver, comm] for pkg, (ver, comm) in pkgs.items()},
        }
        tmp_path = json_path.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        tmp_path.replace(json_path)
    except Exception:
        pass

    # 2. Update validity_cache.db:version_cache
    try:
        from bathos.config import default_catalog_dir
        from bathos.validity import VERSION_CACHE_DDL, ensure_validity_cache, validity_cache_db_path

        cat_dir = os.environ.get("BTH_CATALOG_DIR") or default_catalog_dir()
        db_path = validity_cache_db_path(cat_dir)
        if "BTH_CATALOG_DIR" in os.environ or db_path.parent.is_dir():
            db_path.parent.mkdir(parents=True, exist_ok=True)
            if not db_path.is_file():
                with contextlib.suppress(Exception):
                    ensure_validity_cache(cat_dir)
            with duckdb.connect(str(db_path)) as con:
                con.execute(VERSION_CACHE_DDL)
                rows = [
                    (main_root_str, git_hash, calc_sha, pkg_name, ver, comm, "lockfile")
                    for pkg_name, (ver, comm) in pkgs.items()
                ]
                if rows:
                    con.executemany(
                        "INSERT OR REPLACE INTO version_cache VALUES (?, ?, ?, ?, ?, ?, ?)",
                        rows,
                    )
    except Exception:
        pass


@functools.lru_cache(maxsize=1024)
def _get_uv_lock_packages(
    main_root_str: str, git_hash: str
) -> tuple[str | None, dict[str, tuple[str, str | None]]]:
    """Return (calc_sha, dict_of_pkg_to_tuple_of_version_and_commit)."""
    # 1. Check persistent caches (JSON & DuckDB)
    cached = _read_version_cache(main_root_str, git_hash)
    if cached is not None:
        return cached

    # 2. Fallback to git show <git_hash>:uv.lock
    try:
        res = subprocess.run(
            ["git", "-C", main_root_str, "show", f"{git_hash}:uv.lock"],
            capture_output=True,
            check=False,
        )
        if res.returncode == 0 and res.stdout:
            lock_bytes = res.stdout
            calc_sha = hashlib.sha256(lock_bytes).hexdigest()
            lock_doc = tomllib.loads(lock_bytes.decode("utf-8"))
            pkgs: dict[str, tuple[str, str | None]] = {}
            for p in lock_doc.get("package", []):
                name = p.get("name")
                if name:
                    v = p.get("version")
                    src = p.get("source", {})
                    c = src.get("commit") or src.get("git") if isinstance(src, dict) else None
                    pkgs[name] = (v, c)
            # 3. Populate persistent caches
            _write_version_cache(main_root_str, git_hash, calc_sha, pkgs)
            return calc_sha, pkgs
    except Exception:
        pass
    return None, {}


def resolve_toolchain_version(
    run: Any,
    main_root: Path | None,
    package: str,
) -> tuple[str | None, str | None, str]:
    """Resolve (version, commit, version_source) across the 4-tier hierarchy.

    Tiers:
    1. "recorded": dependency_provenance or metadata verified by lock SHA.
    2. "verified-from-lock": git show <run.git_hash>:uv.lock SHA matches dependency_lock_sha256.
    3. "inferred-from-commit": runtime metadata (metadata.<pkg>_version, wheel name, etc.)
       or uv.lock without lock SHA match.
    4. "unknown": toolchain unresolvable.
    """
    pkg_key = key_prefix(package)

    # 1. Tier 1: Check dependency_provenance
    dep_prov = None
    if isinstance(run, dict):
        dep_prov = run.get("dependency_provenance")
    elif hasattr(run, "dependency_provenance"):
        dep_prov = getattr(run, "dependency_provenance")

    if dep_prov:
        if isinstance(dep_prov, str):
            try:
                dep_prov = json.loads(dep_prov)
            except Exception:
                dep_prov = None
        if isinstance(dep_prov, dict):
            v = dep_prov.get(f"{pkg_key}_version") or dep_prov.get(f"{package}_version")
            c = dep_prov.get(f"{pkg_key}_source_commit") or dep_prov.get(f"{package}_source_commit")
            if (v and v != "unknown") or (c and c != "unknown"):
                return str(v) if v else None, str(c) if c else None, "recorded"

    # Extract metadata version if present
    meta = None
    if isinstance(run, dict):
        meta = run.get("metadata")
    elif hasattr(run, "metadata"):
        meta = getattr(run, "metadata")
    if meta and isinstance(meta, str):
        try:
            meta = json.loads(meta)
        except Exception:
            meta = None

    meta_version: str | None = None
    if isinstance(meta, dict):
        # Look for direct keys
        raw_v = (
            meta.get(f"{package}_version")
            or meta.get(f"{pkg_key}_version")
            or meta.get(f"{package}_installed_dist_version")
            or meta.get(f"{pkg_key}_installed_dist_version")
            or meta.get(f"scores_{package}_version")
            or meta.get(f"scores_{pkg_key}_version")
        )
        if raw_v and str(raw_v) != "unknown":
            meta_version = str(raw_v)
        else:
            # Check wheel name, e.g. tev_pinned_<pkg>_wheel
            wheel_name = (
                meta.get(f"tev_pinned_{package}_wheel")
                or meta.get(f"tev_pinned_{pkg_key}_wheel")
                or meta.get("wheel")
            )
            if wheel_name and isinstance(wheel_name, str):
                m = re.search(r"[-_]([0-9]+(?:\.[0-9]+)*(?:[a-zA-Z0-9_+.-]*))[-_]", wheel_name)
                if m:
                    meta_version = m.group(1)

    # 2. Check uv.lock via git show
    git_hash = None
    if isinstance(run, dict):
        git_hash = run.get("git_hash")
    elif hasattr(run, "git_hash"):
        git_hash = getattr(run, "git_hash")

    dep_lock_sha = None
    if isinstance(run, dict):
        dep_lock_sha = run.get("dependency_lock_sha256")
    elif hasattr(run, "dependency_lock_sha256"):
        dep_lock_sha = getattr(run, "dependency_lock_sha256")

    lock_version: str | None = None
    lock_commit: str | None = None
    lock_verified = False

    if main_root is not None and main_root.is_dir() and git_hash and git_hash != "unknown":
        calc_sha, pkgs = _get_uv_lock_packages(str(main_root), git_hash)
        if calc_sha is not None:
            for name, (v, c) in pkgs.items():
                if name == package or key_prefix(name) == pkg_key:
                    lock_version = v
                    lock_commit = c
                    break
            if dep_lock_sha and dep_lock_sha == calc_sha:
                lock_verified = True

    # Resolve Tiers 1-4:
    if lock_verified and lock_version:
        if meta_version and meta_version != lock_version:
            # Metadata version discrepancy (e.g. ad-hoc wheel installed) -> downgrade to Tier 3
            return meta_version, lock_commit, "inferred-from-commit"
        return lock_version, lock_commit, "verified-from-lock"

    if meta_version:
        # Tier 3 (inferred from metadata)
        return meta_version, lock_commit, "inferred-from-commit"

    if lock_version:
        # Tier 3 (uv.lock found at commit, but lock SHA unverified/missing)
        return lock_version, lock_commit, "inferred-from-commit"

    if (
        git_hash
        and git_hash != "unknown"
        and main_root is not None
        and (main_root.name == package or (isinstance(run, dict) and run.get("project_slug") == package))
    ):
        return None, git_hash, "inferred-from-commit"

    # Tier 4: Unknown
    return None, None, "unknown"
