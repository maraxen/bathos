from __future__ import annotations

import logging
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECTS_REGISTRY = Path.home() / ".bth" / "projects.toml"


@dataclass
class ProjectConfig:
    slug: str
    root: Path
    catalog_dir: Path = field(default_factory=lambda: Path.home() / ".bth" / "catalog")
    remotes: dict[str, dict] = field(default_factory=dict)
    slurm: dict = field(default_factory=dict)
    sync_filter: str = "project_slug"
    claim: dict = field(default_factory=dict)
    #: [obligations] — per-trigger opt-in for the §5 post-mortem obligation triggers,
    #: plus `enforce`. Lives in .bth.toml so the setting is versioned and reaches SLURM
    #: jobs, which read the same file; a shell-only env var would be honoured locally and
    #: silently skipped on the cluster. Env vars still override (see obligations.py).
    obligations: dict = field(default_factory=dict)


def default_catalog_dir() -> Path:
    return Path.home() / ".bth" / "catalog"


_TRUE = ("1", "true", "yes")
_FALSE = ("0", "false", "no")


def env_override(name: str) -> bool | None:
    """Tri-state read of a boolean env flag: True / False / None (unset or unrecognised).

    A set-but-false value must be able to turn a config-enabled flag OFF, so this cannot
    collapse to a plain truthiness test — otherwise `BTH_X=0` would silently mean "fall
    through to config", i.e. still enabled, which is the opposite of what an override is for.
    An unrecognised value returns None rather than False so garbage is not read as a
    deliberate "off" that beats a considered config setting.
    """
    raw = os.environ.get(name)
    if raw is None:
        return None
    val = raw.strip().lower()
    if val in _TRUE:
        return True
    if val in _FALSE:
        return False
    return None


def resolve_flag(
    env_name: str,
    section: str,
    key: str,
    workspace_root: Path | str | None = None,
) -> bool:
    """Resolve a boolean gate: env var (both directions) → `.bth.toml [section] key` → False.

    Single implementation so the obligation triggers and the review-coverage gate cannot
    drift apart on the subtle half (the tri-state env read above).

    The config file is the durable home for these settings: a SLURM job reads the same
    `.bth.toml`, whereas a shell-only export is honoured locally and silently skipped on the
    cluster. A malformed or missing config resolves to False — the safe direction for a gate
    that changes verdicts or writes ledger entries — rather than raising mid-run.
    """
    override = env_override(env_name)
    if override is not None:
        return override
    try:
        cfg_path = find_project_config(Path(workspace_root) if workspace_root else None)
        if cfg_path is None:
            return False
        return bool(getattr(load_project_config(cfg_path), section, {}).get(key, False))
    except Exception:
        return False


def find_project_config(start: Path | None = None) -> Path | None:
    if start is None:
        start = Path.cwd()
    for directory in [start, *start.parents]:
        candidate = directory / ".bth.toml"
        if candidate.exists():
            return candidate
    return None


class EnforcementConfigError(ValueError):
    """``.bth.toml [enforcement]`` is unreadable or invalid.

    Raised and never swallowed: a typo in a setting that ADDS enforcement must not silently leave the gate off
    (the failure class behind protamer debt #1960 / bathos #2210 -- the gate is fail-open for any directory not named).
    """


_ENFORCEMENT_KEYS = frozenset({"dirs"})


def parse_enforcement(
    section: object,
) -> tuple[frozenset[str], tuple[tuple[str, ...], ...]]:
    """Validate an ``[enforcement]`` table -> (component names, project-root-relative prefixes).

    ``dirs`` entries are additive to ``bathos.sidecar.ENFORCED_DIRS``. An entry without ``/`` is a path COMPONENT name
    (matched anywhere below the project root); an entry with ``/`` is a project-root-relative PREFIX, so ``scripts/method``
    does not match ``scripts/methodology`` or ``vendor/scripts/method``. Ambiguous or escaping entries (empty, absolute,
    ``.``/``..``, backslash, doubled or trailing ``/``) are rejected rather than guessed at.
    """
    if section is None:
        return frozenset(), ()
    if not isinstance(section, dict):
        raise EnforcementConfigError("[enforcement] must be a table")
    unknown = sorted(set(section) - _ENFORCEMENT_KEYS)
    if unknown:
        raise EnforcementConfigError(
            f"[enforcement] has unknown key(s) {unknown}; the only supported key is 'dirs'"
        )
    dirs = section.get("dirs", [])
    if not isinstance(dirs, list):
        raise EnforcementConfigError("[enforcement] dirs must be a list of strings")
    names: set[str] = set()
    prefixes: list[tuple[str, ...]] = []
    for entry in dirs:
        if not isinstance(entry, str) or not entry or entry != entry.strip():
            raise EnforcementConfigError(
                f"[enforcement] dirs entry {entry!r} must be a non-empty string without surrounding whitespace"
            )
        if "\\" in entry or entry.startswith("/") or entry.endswith("/"):
            raise EnforcementConfigError(
                f"[enforcement] dirs entry {entry!r} must be a relative posix path without a leading or trailing '/' or any backslash"
            )
        parts = tuple(entry.split("/"))
        if any(part in ("", ".", "..") for part in parts):
            raise EnforcementConfigError(
                f"[enforcement] dirs entry {entry!r} must not contain empty, '.' or '..' components"
            )
        if len(parts) == 1:
            names.add(entry)
        elif parts not in prefixes:
            prefixes.append(parts)
    return frozenset(names), tuple(prefixes)


def load_enforcement(
    script_path: Path,
) -> tuple[frozenset[str], tuple[tuple[str, ...], ...], Path | None]:
    """The project's configured extra enforcement dirs for ``script_path`` -> (names, prefixes, project root).

    The project is found by walking up from the script to the NEAREST ``.bth.toml`` (the same rule as everywhere else in bathos: a nested
    ``.bth.toml`` without ``[enforcement]`` is its own project and hides an outer one's dirs). No config file, or no ``[enforcement]``
    table, is the built-in behaviour (empty result). An invalid ``[enforcement]`` table raises :class:`EnforcementConfigError`. A config
    that cannot be PARSED raises only if the file mentions ``enforcement`` (it evidently intended to opt in); otherwise it is logged and
    treated as the built-in behaviour, because bathos has always tolerated a broken project config and a project that never asked for
    this must not start having every run refused. An unreadable file is logged and treated the same way.
    """
    cfg_path = find_project_config(Path(os.path.abspath(script_path)).parent)
    if cfg_path is None:
        return frozenset(), (), None
    try:
        raw = cfg_path.read_bytes()
    except OSError as e:
        # Unreadable, so we cannot tell whether it opted in. Bathos has always tolerated an unreadable project config; keep that.
        logger.warning(
            "cannot read %s, so any [enforcement] setting in it is NOT applied: %s", cfg_path, e
        )
        return frozenset(), (), None
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        # case-insensitive: a miscased header must not read as "never opted in"
        if b"enforcement" in raw.lower():
            # The file evidently intended to opt in, so a parse error must not silently leave the gate off.
            raise EnforcementConfigError(f"cannot parse {cfg_path}: {e}") from e
        # Never mentioned enforcement: a broken config that bathos already tolerated must not start refusing every run.
        logger.warning("cannot parse %s (%s); no [enforcement] setting is applied", cfg_path, e)
        return frozenset(), (), None
    try:
        names, prefixes = parse_enforcement(data.get("enforcement"))
    except EnforcementConfigError as e:
        raise EnforcementConfigError(f"{cfg_path}: {e}") from e
    return names, prefixes, cfg_path.parent


def load_project_config(path: Path) -> ProjectConfig:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    project = data["project"]
    return ProjectConfig(
        slug=project["slug"],
        root=Path(project["root"]).expanduser(),
        catalog_dir=Path(project["catalog_dir"]).expanduser()
        if "catalog_dir" in project
        else default_catalog_dir(),
        remotes=data.get("remotes", {}),
        slurm=data.get("slurm", {}),
        sync_filter=project.get("sync_filter", "project_slug"),
        claim=data.get("claim", {}),
        obligations=data.get("obligations", {}),
    )


def register_project(slug: str, catalog_dir: Path) -> None:
    """Register project in global registry at ~/.bth/projects.toml."""
    try:
        import toml  # type: ignore

        registry: dict = {}
        if PROJECTS_REGISTRY.exists():
            registry = tomllib.loads(PROJECTS_REGISTRY.read_text())
        projects = registry.setdefault("projects", [])
        # Avoid duplicates
        existing_slugs = [p.get("slug") for p in projects]
        if slug not in existing_slugs:
            projects.append({"slug": slug, "catalog_dir": str(catalog_dir)})
        PROJECTS_REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        PROJECTS_REGISTRY.write_text(toml.dumps(registry))
    except Exception as e:
        logger.warning(
            f"Failed to register project {slug} in global registry: {e}"
        )  # Registry is best-effort; never block init


def list_registered_projects() -> list[dict]:
    """List all registered projects from global registry."""
    if not PROJECTS_REGISTRY.exists():
        return []
    try:
        return tomllib.loads(PROJECTS_REGISTRY.read_text()).get("projects", [])
    except Exception as e:
        logger.warning(f"Failed to read projects registry {PROJECTS_REGISTRY}: {e}")
        return []
