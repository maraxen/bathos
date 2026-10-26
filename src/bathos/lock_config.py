"""Where a run's sidecar lock goes: repo selection (P0) and configuration (P1).

Spec ``261026_provenance-out-of-working-tree``. Two independent pieces, both pure of any lock writing:

* :func:`select_lock_repo` picks the git repo the lock commit-ref belongs in. It is the SIDECAR's checkout, never
  ``cwd`` (the lock lives beside the sidecar, and a run may be launched from a different repo than its script), and
  it refuses the ref path whenever the recorded git hash is not a real commit id or no repo can be found.
* :func:`resolve_lock_settings` resolves ``lock_location`` / ``lock_dir`` / ``ignore_outputs``. First match wins,
  per key:

  1. an explicit argument;
  2. ``$BTH_LOCK_LOCATION`` / ``$BTH_LOCK_DIR`` / ``$BTH_IGNORE_OUTPUTS``;
  3. ``[provenance]`` in ``<cwd>/.bth.toml`` (the file the runner reads, ``runner.py:866``; read with raw tomllib
     because ``load_project_config`` demands ``[project]``). SLURM jobs read the same file and ``_bth_env.sh``
     exports none of the env vars, so the cluster relies on this layer;
  4. ``[tool.bathos.provenance]`` in the nearest ``pyproject.toml`` at or above ``cwd``;
  5. ``[provenance]`` in ``~/.bth/config.toml`` (bathos's existing user config; a documented deviation from the
     global XDG rule, to avoid a second user-config file);
  6. the defaults ``"git"``, ``<catalog_dir>/locks``, ``False``.

  ``resolve_flag`` swallows a malformed config to False (B10), which is wrong for a setting that decides where
  provenance lands, so nothing here is swallowed: a consulted file that cannot be parsed, an unknown key, a wrong
  type or an invalid value raises :class:`LockConfigError`. ``lock_dir`` of ``none`` or empty disables the
  fallback directory at any layer, and that decision is final. Every resolution reports the layer that decided each
  key, for the ``run.lock_written`` event.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from cisternal.provenance.capture import clean_git_env

from bathos.config import _FALSE, _TRUE

LockLocation = Literal["git", "tree"]

LOCK_LOCATIONS = ("git", "tree")
ENV_LOCK_LOCATION = "BTH_LOCK_LOCATION"
ENV_LOCK_DIR = "BTH_LOCK_DIR"
ENV_IGNORE_OUTPUTS = "BTH_IGNORE_OUTPUTS"

_KEYS = ("lock_location", "lock_dir", "ignore_outputs")
_ENV = {
    "lock_location": ENV_LOCK_LOCATION,
    "lock_dir": ENV_LOCK_DIR,
    "ignore_outputs": ENV_IGNORE_OUTPUTS,
}
_DISABLED = frozenset({"", "none"})
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_GIT_TIMEOUT = 10.0


class LockConfigError(ValueError):
    """Lock configuration is unreadable or invalid. Raised, never swallowed."""


@dataclass(frozen=True)
class LockSettings:
    """Resolved lock settings and, per key, the layer that decided it.

    ``lock_dir`` is ``None`` when the fallback directory is disabled (``none`` / empty at any layer): a ref failure
    is then a warning, or fatal under ``--agent-mode autonomous``.
    """

    lock_location: LockLocation
    lock_dir: Path | None
    ignore_outputs: bool
    layers: dict[str, str] = field(default_factory=dict)


def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise LockConfigError(f"cannot read {path}: {e}") from e


def _nearest_pyproject(start: Path) -> Path | None:
    start = start.resolve()
    for directory in [start, *start.parents]:
        candidate = directory / "pyproject.toml"
        if candidate.is_file():
            return candidate
    return None


def _provenance_table(table: object, where: str) -> dict:
    if not isinstance(table, dict):
        raise LockConfigError(f"{where} must be a table")
    unknown = sorted(set(table) - set(_KEYS))
    if unknown:
        raise LockConfigError(f"{where} has unknown key(s) {unknown}; supported: {list(_KEYS)}")
    return table


def _file_layers(cwd: Path) -> list[tuple[str, Path, Callable[[], dict | None]]]:
    """The three file layers, highest first, each a (label, base dir, lazy table loader).

    Lazy so a lower file that no higher layer made necessary is never opened. A file that IS opened and cannot be
    parsed raises even when it holds no provenance table: a broken config is a broken config.
    """
    layers: list[tuple[str, Path, Callable[[], dict | None]]] = []

    bth_toml = cwd / ".bth.toml"

    def load_bth_toml() -> dict | None:
        if not bth_toml.is_file():
            return None
        data = _read_toml(bth_toml)
        if "provenance" not in data:
            return None
        return _provenance_table(data["provenance"], f"{bth_toml} [provenance]")

    layers.append((f"project config {bth_toml} [provenance]", cwd, load_bth_toml))

    pyproject = _nearest_pyproject(cwd)

    def load_pyproject() -> dict | None:
        if pyproject is None:
            return None
        tool = _read_toml(pyproject).get("tool", {})
        bathos = tool.get("bathos", {}) if isinstance(tool, dict) else {}
        if not isinstance(bathos, dict) or "provenance" not in bathos:
            return None
        return _provenance_table(bathos["provenance"], f"{pyproject} [tool.bathos.provenance]")

    layers.append(
        (
            f"pyproject {pyproject} [tool.bathos.provenance]",
            pyproject.parent if pyproject is not None else cwd,
            load_pyproject,
        )
    )

    user = Path.home() / ".bth" / "config.toml"

    def load_user() -> dict | None:
        if not user.is_file():
            return None
        data = _read_toml(user)
        if "provenance" not in data:
            return None
        return _provenance_table(data["provenance"], f"{user} [provenance]")

    layers.append((f"user config {user} [provenance]", user.parent, load_user))
    return layers


def _check_location(value: object, source: str) -> LockLocation:
    if not isinstance(value, str) or value.strip().lower() not in LOCK_LOCATIONS:
        raise LockConfigError(
            f"{source}: lock_location must be one of {list(LOCK_LOCATIONS)}, got {value!r}"
        )
    return value.strip().lower()  # type: ignore[return-value]


def _check_flag(value: object, source: str, from_env: bool) -> bool:
    if from_env:
        raw = str(value).strip().lower()
        if raw in _TRUE:
            return True
        if raw in _FALSE:
            return False
        raise LockConfigError(
            f"{source}: ignore_outputs must be one of {list(_TRUE + _FALSE)}, got {value!r}"
        )
    if not isinstance(value, bool):
        raise LockConfigError(f"{source}: ignore_outputs must be a boolean, got {value!r}")
    return value


def _check_dir(value: object, source: str, base: Path) -> Path | None:
    if not isinstance(value, str):
        raise LockConfigError(f"{source}: lock_dir must be a string, got {value!r}")
    text = value.strip()
    if text.lower() in _DISABLED:
        return None
    path = Path(text).expanduser()
    return path if path.is_absolute() else base / path


def _convert(key: str, value: object, source: str, base: Path, from_env: bool = False):
    if key == "lock_location":
        return _check_location(value, source)
    if key == "ignore_outputs":
        return _check_flag(value, source, from_env)
    return _check_dir(value, source, base)


def resolve_lock_settings(
    *,
    lock_location: str | None = None,
    lock_dir: str | Path | None = None,
    ignore_outputs: bool | None = None,
    cwd: Path | str | None = None,
    catalog_dir: Path | str,
) -> LockSettings:
    """Resolve the three lock settings and the layer that decided each. See the module docstring for the order.

    ``catalog_dir`` is only used for the ``<catalog_dir>/locks`` default. An argument of ``None`` means "not given";
    pass ``lock_dir="none"`` to disable the fallback directory explicitly.
    """
    base_cwd = Path(cwd) if cwd is not None else Path.cwd()
    explicit = {
        "lock_location": lock_location,
        "lock_dir": lock_dir,
        "ignore_outputs": ignore_outputs,
    }
    file_layers = _file_layers(base_cwd)
    tables: dict[int, dict | None] = {}

    values: dict[str, object] = {}
    layers: dict[str, str] = {}
    for key in _KEYS:
        arg = explicit[key]
        if arg is not None:
            if key == "ignore_outputs":
                values[key] = _check_flag(arg, f"argument {key}", from_env=False)
            else:
                values[key] = _convert(key, str(arg), f"argument {key}", base_cwd)
            layers[key] = "argument" + (
                " (disabled)" if key == "lock_dir" and values[key] is None else ""
            )
            continue
        env = _ENV[key]
        if env in os.environ:
            values[key] = _convert(key, os.environ[env], f"${env}", base_cwd, from_env=True)
            layers[key] = f"${env}" + (
                " (disabled)" if key == "lock_dir" and values[key] is None else ""
            )
            continue
        for index, (label, base, load) in enumerate(file_layers):
            if index not in tables:
                tables[index] = load()
            table = tables[index]
            if table is not None and key in table:
                values[key] = _convert(key, table[key], f"{label} {key}", base)
                layers[key] = label + (
                    " (disabled)" if key == "lock_dir" and values[key] is None else ""
                )
                break
        else:
            layers[key] = "default"
            values[key] = {
                "lock_location": "git",
                "lock_dir": Path(catalog_dir) / "locks",
                "ignore_outputs": False,
            }[key]

    return LockSettings(
        lock_location=values["lock_location"],  # type: ignore[arg-type]
        lock_dir=values["lock_dir"],  # type: ignore[arg-type]
        ignore_outputs=values["ignore_outputs"],  # type: ignore[arg-type]
        layers=layers,
    )


# -- repo selection (P0) -------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class LockRepoChoice:
    """The repo a lock ref belongs in, or why the ref path is unavailable.

    ``root`` is ``None`` exactly when ``ref_error`` is set (``no_repo``, ``sentinel_hash`` or
    ``sidecar_outside_repo``); the lock then goes to the fallback directory.
    """

    root: Path | None
    sidecar_dir_rel: str = ""
    ref_error: str | None = None

    def lock_relpath(self, sidecar_stem: str, run_id: str) -> str:
        """The lock's path relative to the repo root: the same name tree mode uses, so G19 finds it."""
        name = f"{sidecar_stem}.{run_id}.bth.lock.toml"
        return f"{self.sidecar_dir_rel}/{name}" if self.sidecar_dir_rel else name


def _show_toplevel(directory: Path) -> Path | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            env=clean_git_env(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    top = result.stdout.strip()
    if result.returncode != 0 or not top:
        return None
    return Path(top)


def select_lock_repo(
    sidecar_path: Path, git_hash: str, *, root: Path | None = None
) -> LockRepoChoice:
    """Pick the repo for a run's lock ref from the SIDECAR's directory (never cwd).

    The ref path needs all of: ``git_hash`` is a 40-hex commit id (excludes empty, ``unknown`` incl. a broken
    gitlink, and ``nogit``), a root probe from the sidecar directory succeeds (a myxcel-env hash can be real on a
    host with no ``.git``), and the sidecar directory lies inside that root. A caller that already knows the root
    passes ``root`` to skip the probe; the hash gate and the inside-root check still apply.
    """
    if not _FULL_SHA.fullmatch(git_hash or ""):
        return LockRepoChoice(None, ref_error="sentinel_hash")
    sidecar_dir = sidecar_path.parent
    top = root if root is not None else _show_toplevel(sidecar_dir)
    if top is None:
        return LockRepoChoice(None, ref_error="no_repo")
    root = top.resolve()
    try:
        rel = sidecar_dir.resolve().relative_to(root)
    except ValueError:
        return LockRepoChoice(None, ref_error="sidecar_outside_repo")
    return LockRepoChoice(root, sidecar_dir_rel=rel.as_posix() if rel.parts else "")
