"""Which myxcel profile holds the cold copy and which holds the mirror, and where on that host it goes.

Resolution applies to profile NAMES, never paths (spec section 5.1). First match wins:

1. an explicit argument (``--profile`` / ``--mirror``);
2. ``$BTH_COLD_PROFILE`` / ``$BTH_MIRROR_PROFILE`` if set at all;
3. ``[tool.bathos.outputs]`` ``cold_profile`` / ``mirror_profile`` in the nearest ``pyproject.toml`` at or above
   ``start`` (default cwd);
4. ``[outputs]`` with the same keys in ``${XDG_CONFIG_HOME:-~/.config}/bathos/config.toml`` (D9;
   ``~/.bth/config.toml`` is not read for this);
5. off.

At every layer ``none`` or an empty string disables the leg, and that decision is final (a lower layer is not
consulted). A file that is consulted and cannot be parsed, or holds an unknown key or a non-string value, raises
:class:`OutputTiersConfigError` naming it; nothing is swallowed. Every resolution reports the layer that decided, so
a command can show it and a copy row can record it.

A profile name resolves to ``~/.config/myxcel/<name>.toml`` (the directory myxcel itself reads), of which only the
top-level ``host`` and ``cold_root`` keys are part of the contract.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Leg = Literal["cold", "mirror"]

ENV_VARS: dict[Leg, str] = {"cold": "BTH_COLD_PROFILE", "mirror": "BTH_MIRROR_PROFILE"}
CONFIG_KEYS: dict[Leg, str] = {"cold": "cold_profile", "mirror": "mirror_profile"}
_KNOWN_KEYS = frozenset(CONFIG_KEYS.values())
_DISABLED = frozenset({"", "none"})
# A profile name becomes a file name under the myxcel config dir, so it may not carry a path.
_PROFILE_NAME = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_.-]*")


class OutputTiersConfigError(ValueError):
    """Output-tier configuration is unreadable or invalid. Raised, never swallowed.

    ``error_code`` is the JSON error code a command reports: ``invalid_param`` for a bad setting,
    ``no_cold_root`` for a profile that names no usable ``cold_root``.
    """

    def __init__(self, message: str, error_code: str = "invalid_param") -> None:
        super().__init__(message)
        self.error_code = error_code


@dataclass(frozen=True)
class ProfileChoice:
    """The profile chosen for one leg (``None`` = leg off) and the layer that decided it."""

    leg: Leg
    name: str | None
    layer: str


@dataclass(frozen=True)
class ColdProfile:
    """The two contract keys of a myxcel profile, plus the file they came from."""

    name: str
    host: str
    cold_root: str
    source: Path

    def destination(self, project_slug: str, run_id: str) -> str:
        """``<cold_root>/bathos-cold/<project_slug>/<run_id>``, a POSIX path on ``host``."""
        for label, part in (("project_slug", project_slug), ("run_id", run_id)):
            if not part or "/" in part or part in {".", ".."}:
                raise OutputTiersConfigError(f"{label} {part!r} cannot be used as a path component")
        return f"{self.cold_root.rstrip('/')}/bathos-cold/{project_slug}/{run_id}"


def user_config_path() -> Path:
    """``${XDG_CONFIG_HOME:-~/.config}/bathos/config.toml``, resolved at call time."""
    base = os.environ.get("XDG_CONFIG_HOME") or "~/.config"
    return Path(base).expanduser() / "bathos" / "config.toml"


def myxcel_config_dir() -> Path:
    """``~/.config/myxcel``, the directory ``myxcel.config.load_profile`` reads (it does not consult XDG)."""
    return Path("~/.config/myxcel").expanduser()


def _read_toml(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise OutputTiersConfigError(f"cannot read {path}: {e}") from e


def _nearest_pyproject(start: Path) -> Path | None:
    start = start.resolve()
    for directory in [start, *start.parents]:
        candidate = directory / "pyproject.toml"
        if candidate.is_file():
            return candidate
    return None


def _outputs_table(table: object, where: str) -> dict:
    if not isinstance(table, dict):
        raise OutputTiersConfigError(f"{where} must be a table")
    unknown = sorted(set(table) - _KNOWN_KEYS)
    if unknown:
        raise OutputTiersConfigError(
            f"{where} has unknown key(s) {unknown}; supported: {sorted(_KNOWN_KEYS)}"
        )
    return table


def _choice(leg: Leg, value: object, layer: str) -> ProfileChoice:
    if not isinstance(value, str):
        raise OutputTiersConfigError(f"{layer}: {CONFIG_KEYS[leg]} must be a string, got {value!r}")
    name = value.strip()
    if name.lower() in _DISABLED:
        return ProfileChoice(leg, None, f"{layer} (disabled)")
    if not _PROFILE_NAME.fullmatch(name):
        raise OutputTiersConfigError(
            f"{layer}: profile name {name!r} must match {_PROFILE_NAME.pattern}"
        )
    return ProfileChoice(leg, name, layer)


def resolve_profile(
    leg: Leg, explicit: str | None = None, start: Path | str | None = None
) -> ProfileChoice:
    """The profile for ``leg`` and the layer that chose it. See the module docstring for the order."""
    flag = "--profile" if leg == "cold" else "--mirror"
    if explicit is not None:
        return _choice(leg, explicit, f"explicit {flag}")
    env = ENV_VARS[leg]
    if env in os.environ:
        return _choice(leg, os.environ[env], f"${env}")
    key = CONFIG_KEYS[leg]
    checked = [f"{flag}", f"${env}"]
    pyproject = _nearest_pyproject(Path(start) if start is not None else Path.cwd())
    if pyproject is not None:
        tool = _read_toml(pyproject).get("tool", {})
        bathos = tool.get("bathos", {}) if isinstance(tool, dict) else {}
        if isinstance(bathos, dict) and "outputs" in bathos:
            table = _outputs_table(bathos["outputs"], f"{pyproject} [tool.bathos.outputs]")
            if key in table:
                return _choice(leg, table[key], f"project config {pyproject}")
        checked.append(f"{pyproject} [tool.bathos.outputs]")
    else:
        checked.append("pyproject.toml [tool.bathos.outputs] (none found)")
    user = user_config_path()
    if user.is_file():
        data = _read_toml(user)
        if "outputs" in data:
            table = _outputs_table(data["outputs"], f"{user} [outputs]")
            if key in table:
                return _choice(leg, table[key], f"user config {user}")
    checked.append(f"{user} [outputs]")
    return ProfileChoice(leg, None, f"off (no layer set {key}; checked {', '.join(checked)})")


def load_profile(name: str, config_dir: Path | None = None) -> ColdProfile:
    """Read ``host`` and ``cold_root`` from ``<config_dir>/<name>.toml`` (default :func:`myxcel_config_dir`).

    ``cold_root`` must be an absolute path on the remote host: ``~`` would expand to the LOCAL home.
    """
    if not _PROFILE_NAME.fullmatch(name):
        raise OutputTiersConfigError(f"profile name {name!r} must match {_PROFILE_NAME.pattern}")
    path = (config_dir if config_dir is not None else myxcel_config_dir()) / f"{name}.toml"
    if not path.is_file():
        raise OutputTiersConfigError(f"myxcel profile {name!r} not found at {path}")
    data = _read_toml(path)
    host = data.get("host")
    if not isinstance(host, str) or not host.strip():
        raise OutputTiersConfigError(f"{path}: 'host' must be a non-empty string, got {host!r}")
    cold_root = data.get("cold_root")
    if cold_root is None:
        raise OutputTiersConfigError(
            f"{path} has no 'cold_root'; add one to tier outputs to {name!r}", "no_cold_root"
        )
    if not isinstance(cold_root, str) or not cold_root.startswith("/"):
        raise OutputTiersConfigError(
            f"{path}: 'cold_root' must be an absolute path on {host!r}, got {cold_root!r}",
            "no_cold_root",
        )
    return ColdProfile(name=name, host=host.strip(), cold_root=cold_root, source=path)
