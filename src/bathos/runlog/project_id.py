"""Project id handling (D7) and main-root registration (Discovery).

The `project_id` is a UUID stored in the project's tracked `.bth.toml`
(`[project] id`), minted only by `bth init` (a fresh project) or
`bth init --assign-id` (an existing one). A run never writes `.bth.toml`; it
only reads the id off the resolved main root.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import tomlkit

from .mode import RunLogError


def projects_registry() -> Path:
    """`~/.bth/projects.toml`, resolved per call so a redirected HOME is honoured (AC-11)."""
    return Path.home() / ".bth" / "projects.toml"


class ProjectIdMissingError(RunLogError):
    """A local `bth run` (or other local writer) found no `[project] id` in
    the resolved main root's `.bth.toml` (D7).

    Naming the failing command per D7: a run never writes `.bth.toml` itself,
    so the fix is always `bth init --assign-id` run once, by hand, and
    committed.
    """


def mint_project_id() -> str:
    return str(uuid.uuid4())


def read_project_id(bth_toml_path: Path) -> str | None:
    """Read `[project] id` from a `.bth.toml` file, or None if absent/missing."""
    if not bth_toml_path.exists():
        return None
    try:
        doc = tomlkit.parse(bth_toml_path.read_text())
    except Exception:
        return None
    project = doc.get("project")
    if not isinstance(project, dict):
        return None
    value = project.get("id")
    return str(value) if value else None


def require_project_id(main_root: Path) -> str:
    """Read the project id off `main_root/.bth.toml`, raising if absent.

    Used by local writers (2b) that must fail rather than silently write
    `project_id: null` for a project that simply forgot to assign one --
    unlike a SLURM job, which has its own `BTH_PROJECT_ID` fallback path.
    """
    pid = read_project_id(main_root / ".bth.toml")
    if pid is None:
        raise ProjectIdMissingError(
            f"{main_root}/.bth.toml has no [project] id. Run "
            f"`bth init --assign-id` in {main_root}, commit the result, and retry."
        )
    return pid


@dataclass(frozen=True)
class AssignResult:
    project_id: str
    minted: bool  # True if a new id was written; False if one already existed


def assign_project_id(project_root: Path, *, force: bool = False) -> AssignResult:
    """Ensure `project_root/.bth.toml` carries a `[project] id`.

    Idempotent: if an id is already present and `force` is False, it is
    returned unchanged (`minted=False`). Preserves the rest of the file
    (comments, key order, other sections) via tomlkit rather than a
    parse-and-regenerate round trip through `tomllib`/`toml`.

    Raises `FileNotFoundError` if `project_root/.bth.toml` does not exist --
    this function retrofits an EXISTING project; a brand-new one should go
    through `bathos.init.init_project`, which mints an id unconditionally.
    """
    toml_path = project_root / ".bth.toml"
    if not toml_path.exists():
        raise FileNotFoundError(
            f"{toml_path} does not exist; run `bth init` to create a new project "
            "(which mints an id automatically), or check project_root."
        )
    doc = tomlkit.parse(toml_path.read_text())
    project = doc.get("project")
    if project is None:
        project = tomlkit.table()
        doc["project"] = project
    existing = project.get("id")
    if existing and not force:
        return AssignResult(project_id=str(existing), minted=False)
    new_id = mint_project_id()
    project["id"] = new_id
    toml_path.write_text(tomlkit.dumps(doc))
    return AssignResult(project_id=new_id, minted=True)


# --- Discovery: root registration in ~/.bth/projects.toml -------------------


def _load_registry() -> tomlkit.TOMLDocument:
    if projects_registry().exists():
        try:
            return tomlkit.parse(projects_registry().read_text())
        except Exception:
            return tomlkit.document()
    return tomlkit.document()


def _write_registry(doc: tomlkit.TOMLDocument) -> None:
    projects_registry().parent.mkdir(parents=True, exist_ok=True)
    projects_registry().write_text(tomlkit.dumps(doc))


def register_main_root(main_root: Path) -> bool:
    """Idempotently register `main_root` as a known project root.

    "the first event written to a project log registers it (idempotent)" --
    called by the writer (writer.py) after a successful append to a project
    log (never for the fallback or mirror). Returns True if a new entry was
    added, False if `main_root` was already registered.
    """
    main_root = main_root.resolve()
    doc = _load_registry()
    roots_table = doc.get("roots")
    if roots_table is None:
        roots_table = tomlkit.aot()
        doc["roots"] = roots_table
    for entry in roots_table:
        if entry.get("root") == str(main_root):
            return False
    entry = tomlkit.table()
    entry["root"] = str(main_root)
    pid = read_project_id(main_root / ".bth.toml")
    if pid:
        entry["project_id"] = pid
    roots_table.append(entry)
    _write_registry(doc)
    return True


def list_registered_roots() -> list[Path]:
    doc = _load_registry()
    roots_table = doc.get("roots")
    if roots_table is None:
        return []
    return [Path(entry["root"]) for entry in roots_table if entry.get("root")]


def prune_vanished_roots() -> list[Path]:
    """Remove registered roots that no longer exist on disk. Returns the
    pruned paths (`bth projects prune`, CLI wiring left to a later step)."""
    doc = _load_registry()
    roots_table = doc.get("roots")
    if roots_table is None:
        return []
    kept = tomlkit.aot()
    pruned: list[Path] = []
    for entry in roots_table:
        root = Path(entry["root"]) if entry.get("root") else None
        if root is not None and root.exists():
            kept.append(entry)
        elif root is not None:
            pruned.append(root)
    doc["roots"] = kept
    if pruned:
        _write_registry(doc)
    return pruned


def live_roots_with_id(project_id: str, *, exclude: Path | None = None) -> set[Path]:
    """Registered roots that exist on disk, carry `project_id`, and are not
    `exclude` -- "live" per D7's `bth log restore` rule."""
    exclude_resolved = exclude.resolve() if exclude is not None else None
    live: set[Path] = set()
    for root in list_registered_roots():
        if not root.exists():
            continue
        if exclude_resolved is not None and root.resolve() == exclude_resolved:
            continue
        if read_project_id(root / ".bth.toml") == project_id:
            live.add(root.resolve())
    return live
