"""Parity-store location resolver and plain content-addressed store (WP01).

Spec 261123 §3.1 and §12.2 WP01:
- Resolution order:
  1. explicit `--parity-store` argument
  2. `BTH_PARITY_STORE` (`""` or `"none"` disables)
  3. `[tool.bathos.parity] store` in nearest `pyproject.toml`
  4. `${XDG_CONFIG_HOME:-~/.config}/bathos/config.toml` `[parity] store`
  5. off (no default path in library code)
- Plain content-addressed layout: ``<store>/<sha[:2]>/<sha>``
- Atomic per-writer writes via temp file + ``os.replace``.
- Pre-write file-count and byte-quota check emitting a warning when exceeded.
"""

from __future__ import annotations

import ast
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import tomllib
import uuid
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")
_HEX_PREFIX_RE = re.compile(r"^[0-9a-f]{4,64}$")

LAYER_CLI = "cli_arg"
LAYER_ENV = "env:BTH_PARITY_STORE"
LAYER_PYPROJECT = "pyproject:[tool.bathos.parity]"
LAYER_XDG = "xdg:config.toml"
LAYER_OFF = "off"


class ParityStoreConfigError(ValueError):
    """Raised when a configuration file for the parity store is malformed."""


class ParityStoreUnavailableError(RuntimeError):
    """Raised when a step requires a parity store and resolution ended in off/disabled."""

    def __init__(self, resolution: ParityStoreResolution) -> None:
        self.resolution = resolution
        super().__init__(
            f"Parity store is not configured ({resolution.source}). Resolution trace:\n"
            + resolution.format_trace()
        )


@dataclass(frozen=True)
class ResolutionLayer:
    """One step inspected during parity-store resolution."""

    name: str
    status: str  # "selected" | "disabled" | "absent" | "unset" | "empty" | "not_configured"
    detail: str


@dataclass(frozen=True)
class ParityStoreResolution:
    """Result of resolving the parity-store location across the five precedence layers."""

    path: Path | None
    source: str
    trace: tuple[ResolutionLayer, ...]
    max_files: int | None = None
    max_bytes: int | None = None

    def format_trace(self) -> str:
        """Render a human-readable trace naming every layer."""
        lines = [f"- {layer.name}: {layer.status} ({layer.detail})" for layer in self.trace]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """Serialize resolution outcome to a JSON-compatible dictionary."""
        return {
            "path": str(self.path) if self.path is not None else None,
            "source": self.source,
            "max_files": self.max_files,
            "max_bytes": self.max_bytes,
            "trace": [
                {"name": layer.name, "status": layer.status, "detail": layer.detail}
                for layer in self.trace
            ],
        }


def _resolve_store_path(raw: str, *, base_dir: Path) -> Path:
    expanded = Path(raw).expanduser()
    if not expanded.is_absolute():
        expanded = base_dir / expanded
    return expanded.resolve()


def _find_nearest_pyproject(start_dir: Path) -> Path | None:
    cur = start_dir.resolve()
    for parent in (cur, *cur.parents):
        candidate = parent / "pyproject.toml"
        if candidate.is_file():
            return candidate
    return None


def _parse_int_quota(val: Any, *, field_name: str, config_path: Path) -> int | None:
    if val is None:
        return None
    if isinstance(val, bool) or not isinstance(val, int) or val < 0:
        raise ParityStoreConfigError(
            f"Malformed parity store config in {config_path}: {field_name!r} must be a non-negative integer, got {val!r}"
        )
    return val


def _read_pyproject_parity(pyproject_path: Path) -> tuple[Path | None, int | None, int | None, str]:
    try:
        raw_bytes = pyproject_path.read_bytes()
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ParityStoreConfigError(
            f"Malformed pyproject.toml at {pyproject_path}: {exc}"
        ) from exc

    tool = data.get("tool")
    if tool is None:
        return None, None, None, f"{pyproject_path}: no [tool] table"
    if not isinstance(tool, dict):
        raise ParityStoreConfigError(
            f"Malformed pyproject.toml at {pyproject_path}: [tool] must be a table"
        )
    bathos_tbl = tool.get("bathos")
    if bathos_tbl is None:
        return None, None, None, f"{pyproject_path}: no [tool.bathos] table"
    if not isinstance(bathos_tbl, dict):
        raise ParityStoreConfigError(
            f"Malformed pyproject.toml at {pyproject_path}: [tool.bathos] must be a table"
        )
    parity_tbl = bathos_tbl.get("parity")
    if parity_tbl is None:
        return None, None, None, f"{pyproject_path}: no [tool.bathos.parity] table"
    if not isinstance(parity_tbl, dict):
        raise ParityStoreConfigError(
            f"Malformed pyproject.toml at {pyproject_path}: [tool.bathos.parity] must be a table"
        )

    max_files = _parse_int_quota(
        parity_tbl.get("max_files"), field_name="max_files", config_path=pyproject_path
    )
    max_bytes = _parse_int_quota(
        parity_tbl.get("max_bytes"), field_name="max_bytes", config_path=pyproject_path
    )
    store_val = parity_tbl.get("store")
    if store_val is None:
        return None, max_files, max_bytes, f"{pyproject_path}: [tool.bathos.parity].store not set"
    if not isinstance(store_val, str):
        raise ParityStoreConfigError(
            f"Malformed pyproject.toml at {pyproject_path}: [tool.bathos.parity].store must be a string, got {type(store_val).__name__}"
        )
    stripped = store_val.strip()
    if not stripped or stripped.lower() == "none":
        return None, max_files, max_bytes, f"{pyproject_path}: [tool.bathos.parity].store disabled ({stripped!r})"
    resolved = _resolve_store_path(stripped, base_dir=pyproject_path.parent)
    return resolved, max_files, max_bytes, f"{pyproject_path}: {resolved}"


def _read_xdg_parity(config_path: Path) -> tuple[Path | None, int | None, int | None, str]:
    try:
        raw_bytes = config_path.read_bytes()
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ParityStoreConfigError(
            f"Malformed XDG bathos config at {config_path}: {exc}"
        ) from exc

    parity_tbl = data.get("parity")
    if parity_tbl is None:
        return None, None, None, f"{config_path}: no [parity] table"
    if not isinstance(parity_tbl, dict):
        raise ParityStoreConfigError(
            f"Malformed XDG bathos config at {config_path}: [parity] must be a table"
        )
    max_files = _parse_int_quota(
        parity_tbl.get("max_files"), field_name="max_files", config_path=config_path
    )
    max_bytes = _parse_int_quota(
        parity_tbl.get("max_bytes"), field_name="max_bytes", config_path=config_path
    )
    store_val = parity_tbl.get("store")
    if store_val is None:
        return None, max_files, max_bytes, f"{config_path}: [parity].store not set"
    if not isinstance(store_val, str):
        raise ParityStoreConfigError(
            f"Malformed XDG bathos config at {config_path}: [parity].store must be a string, got {type(store_val).__name__}"
        )
    stripped = store_val.strip()
    if not stripped or stripped.lower() == "none":
        return None, max_files, max_bytes, f"{config_path}: [parity].store disabled ({stripped!r})"
    resolved = _resolve_store_path(stripped, base_dir=config_path.parent)
    return resolved, max_files, max_bytes, f"{config_path}: {resolved}"


def resolve_parity_store(
    *,
    explicit: str | Path | None = None,
    start_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> ParityStoreResolution:
    """Resolve the parity-store location across the 5 documented layers.

    Precedence:
    1. explicit ``--parity-store`` argument
    2. ``BTH_PARITY_STORE`` environment variable (``""`` or ``"none"`` disables)
    3. ``[tool.bathos.parity] store`` in nearest ``pyproject.toml``
    4. ``${XDG_CONFIG_HOME:-~/.config}/bathos/config.toml`` ``[parity] store``
    5. off (no default path in library code)

    Malformed config files always raise :class:`ParityStoreConfigError`.
    """
    environ = os.environ if env is None else env
    cwd = (start_dir or Path.cwd()).resolve()
    trace: list[ResolutionLayer] = []

    # Read optional quota/config layers even if overridden? Wait: read config layers as
    # we walk, or stop at first match while recording remaining layers if needed.
    # Always inspect lower config files ONLY if reached, OR record every layer cleanly in trace.
    # For trace completeness when off, all 5 layers are inspected and named in trace.

    # Layer 1: Explicit CLI / function argument
    if explicit is not None:
        raw_explicit = str(explicit).strip()
        if not raw_explicit or raw_explicit.lower() == "none":
            trace.append(ResolutionLayer(LAYER_CLI, "disabled", f"explicit={raw_explicit!r}"))
            return ParityStoreResolution(path=None, source=f"{LAYER_CLI}:disabled", trace=tuple(trace))
        resolved_cli = _resolve_store_path(raw_explicit, base_dir=cwd)
        trace.append(ResolutionLayer(LAYER_CLI, "selected", str(resolved_cli)))
        return ParityStoreResolution(path=resolved_cli, source=LAYER_CLI, trace=tuple(trace))
    trace.append(ResolutionLayer(LAYER_CLI, "unset", "not passed"))

    # Layer 2: BTH_PARITY_STORE environment variable
    if "BTH_PARITY_STORE" in environ:
        raw_env = environ["BTH_PARITY_STORE"].strip()
        if not raw_env or raw_env.lower() == "none":
            trace.append(ResolutionLayer(LAYER_ENV, "disabled", f"value={raw_env!r}"))
            return ParityStoreResolution(path=None, source=f"{LAYER_ENV}:disabled", trace=tuple(trace))
        resolved_env = _resolve_store_path(raw_env, base_dir=cwd)
        trace.append(ResolutionLayer(LAYER_ENV, "selected", str(resolved_env)))
        return ParityStoreResolution(path=resolved_env, source=LAYER_ENV, trace=tuple(trace))
    trace.append(ResolutionLayer(LAYER_ENV, "unset", "BTH_PARITY_STORE not set"))

    # Layer 3: Nearest pyproject.toml [tool.bathos.parity]
    pyproject_path = _find_nearest_pyproject(cwd)
    if pyproject_path is not None:
        p_path, p_files, p_bytes, p_detail = _read_pyproject_parity(pyproject_path)
        if p_path is not None:
            trace.append(ResolutionLayer(LAYER_PYPROJECT, "selected", p_detail))
            return ParityStoreResolution(
                path=p_path,
                source=LAYER_PYPROJECT,
                trace=tuple(trace),
                max_files=p_files,
                max_bytes=p_bytes,
            )
        if "disabled" in p_detail:
            trace.append(ResolutionLayer(LAYER_PYPROJECT, "disabled", p_detail))
            return ParityStoreResolution(
                path=None,
                source=f"{LAYER_PYPROJECT}:disabled",
                trace=tuple(trace),
                max_files=p_files,
                max_bytes=p_bytes,
            )
        trace.append(ResolutionLayer(LAYER_PYPROJECT, "absent", p_detail))
    else:
        trace.append(ResolutionLayer(LAYER_PYPROJECT, "absent", f"no pyproject.toml found from {cwd}"))

    # Layer 4: ${XDG_CONFIG_HOME:-~/.config}/bathos/config.toml [parity]
    xdg_raw = environ.get("XDG_CONFIG_HOME")
    if xdg_raw is None:
        xdg_base = Path.home() / ".config"
        xdg_note = f"default ~/.config ({xdg_base})"
    elif not xdg_raw.strip():
        xdg_base = Path.home() / ".config"
        xdg_note = f"XDG_CONFIG_HOME empty -> {xdg_base}"
    else:
        xdg_base = Path(xdg_raw).expanduser()
        xdg_note = f"XDG_CONFIG_HOME={xdg_base}"

    xdg_config = xdg_base / "bathos" / "config.toml"
    if xdg_config.is_file():
        x_path, x_files, x_bytes, x_detail = _read_xdg_parity(xdg_config)
        if x_path is not None:
            trace.append(ResolutionLayer(LAYER_XDG, "selected", x_detail))
            return ParityStoreResolution(
                path=x_path,
                source=LAYER_XDG,
                trace=tuple(trace),
                max_files=x_files,
                max_bytes=x_bytes,
            )
        if "disabled" in x_detail:
            trace.append(ResolutionLayer(LAYER_XDG, "disabled", x_detail))
            return ParityStoreResolution(
                path=None,
                source=f"{LAYER_XDG}:disabled",
                trace=tuple(trace),
                max_files=x_files,
                max_bytes=x_bytes,
            )
        trace.append(ResolutionLayer(LAYER_XDG, "absent", x_detail))
    else:
        trace.append(ResolutionLayer(LAYER_XDG, "absent", f"{xdg_config} does not exist ({xdg_note})"))

    # Layer 5: off
    trace.append(ResolutionLayer(LAYER_OFF, "selected", "no store configured"))
    return ParityStoreResolution(path=None, source=LAYER_OFF, trace=tuple(trace))


def require_parity_store(
    *,
    explicit: str | Path | None = None,
    start_dir: Path | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[ParityStore, ParityStoreResolution]:
    """Resolve and open a :class:`ParityStore`, raising with full trace if off."""
    res = resolve_parity_store(explicit=explicit, start_dir=start_dir, env=env)
    if res.path is None:
        raise ParityStoreUnavailableError(res)
    return ParityStore(res.path, max_files=res.max_files, max_bytes=res.max_bytes), res


def atomic_write_bytes(target: Path, data: bytes) -> None:
    """Atomically write bytes to *target* via temp file + ``os.replace``."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
    try:
        with tmp.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


def atomic_write_json(target: Path, payload: Any) -> None:
    """Deterministically serialize *payload* as JSON and write atomically."""
    text = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
    atomic_write_bytes(target, text.encode("utf-8"))


class ParityStore:
    """Content-addressed parity store on a plain filesystem layout."""

    def __init__(
        self,
        root: Path | str,
        *,
        max_files: int | None = None,
        max_bytes: int | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.max_files = max_files
        self.max_bytes = max_bytes

    def blob_path(self, sha256: str) -> Path:
        """Return ``<root>/<sha[:2]>/<sha>`` after validating 64-hex digest format."""
        digest = sha256.lower()
        if not _HEX64_RE.match(digest):
            raise ValueError(f"Invalid sha256 digest: {sha256!r}")
        return self.root / digest[:2] / digest

    def has(self, sha256: str) -> bool:
        """Return True iff the content-addressed blob exists."""
        return self.blob_path(sha256).is_file()

    def get_bytes(self, sha256: str) -> bytes:
        """Read bytes for *sha256* and verify the content digest matches."""
        path = self.blob_path(sha256)
        if not path.is_file():
            raise FileNotFoundError(f"Parity store blob missing: {sha256} ({path})")
        data = path.read_bytes()
        actual = hashlib.sha256(data).hexdigest()
        if actual != sha256.lower():
            raise ValueError(
                f"Parity store blob corruption for {sha256}: expected {sha256.lower()}, got {actual}"
            )
        return data

    def put_bytes(self, data: bytes) -> str:
        """Store *data* content-addressed by sha256 with atomic write and quota check."""
        digest = hashlib.sha256(data).hexdigest()
        target = self.blob_path(digest)
        if target.is_file():
            return digest
        self.check_quota(additional_files=1, additional_bytes=len(data))
        atomic_write_bytes(target, data)
        return digest

    def resolve_prefix(self, prefix: str) -> str:
        """Resolve a sha256 hex prefix (>=4 chars) to its unique 64-hex digest."""
        norm = prefix.lower()
        if not _HEX_PREFIX_RE.match(norm):
            raise ValueError(f"Invalid sha256 prefix: {prefix!r}")
        if len(norm) == 64:
            if self.has(norm):
                return norm
            raise FileNotFoundError(f"No blob matching digest {norm}")
        shard = self.root / norm[:2]
        if not shard.is_dir():
            raise FileNotFoundError(f"No blob matching prefix {prefix!r}")
        matches = sorted(
            p.name
            for p in shard.iterdir()
            if p.is_file() and _HEX64_RE.match(p.name) and p.name.startswith(norm)
        )
        if not matches:
            raise FileNotFoundError(f"No blob matching prefix {prefix!r}")
        if len(matches) > 1:
            raise ValueError(
                f"Ambiguous sha256 prefix {prefix!r}: matches {len(matches)} blobs ({matches[:3]})"
            )
        return matches[0]

    def write_unit_dir(self, rel_target: Path | str, populate_fn: Callable[[Path], None]) -> Path:
        """Populate a temporary directory and atomically rename it to ``root / rel_target``."""
        target = (self.root / rel_target).resolve()
        if self.root not in target.parents and target != self.root:
            raise ValueError(f"Unit directory escapes store root: {rel_target!r}")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp_dir = target.parent / f".{target.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        try:
            populate_fn(tmp_dir)
            if target.exists():
                shutil.rmtree(tmp_dir)
                return target
            os.replace(tmp_dir, target)
            return target
        except BaseException:
            with contextlib.suppress(OSError):
                shutil.rmtree(tmp_dir)
            raise

    def usage_stats(self) -> tuple[int, int]:
        """Return ``(file_count, total_bytes)`` currently stored under ``self.root``."""
        if not self.root.exists():
            return 0, 0
        count = 0
        total = 0
        for dirpath, _dirnames, filenames in self.root.walk():
            for name in filenames:
                if name.startswith(".") and name.endswith(".tmp"):
                    continue
                count += 1
                with contextlib.suppress(OSError):
                    total += (dirpath / name).stat().st_size
        return count, total

    def check_quota(self, *, additional_files: int = 1, additional_bytes: int = 0) -> list[str]:
        """Warn before a write would exceed configured ``max_files`` or ``max_bytes``."""
        if self.max_files is None and self.max_bytes is None:
            return []
        cur_files, cur_bytes = self.usage_stats()
        warnings_emitted: list[str] = []
        if self.max_files is not None and (cur_files + additional_files) > self.max_files:
            msg = (
                f"Parity store {self.root} file count would exceed max_files cap "
                f"({cur_files} + {additional_files} > {self.max_files})"
            )
            warnings_emitted.append(msg)
            logger.warning(msg)
            warnings.warn(msg, ResourceWarning, stacklevel=2)
        if self.max_bytes is not None and (cur_bytes + additional_bytes) > self.max_bytes:
            msg = (
                f"Parity store {self.root} size would exceed max_bytes cap "
                f"({cur_bytes} + {additional_bytes} > {self.max_bytes})"
            )
            warnings_emitted.append(msg)
            logger.warning(msg)
            warnings.warn(msg, ResourceWarning, stacklevel=2)
        return warnings_emitted


_FORBIDDEN_DEFAULT_PATH_PATTERNS = (
    re.compile(r"\.bth[/\\]parity\b"),
    re.compile(r"\.cache[/\\]bathos[/\\]parity\b"),
    re.compile(r"default_parity_store", re.IGNORECASE),
    re.compile(r"DEFAULT_PARITY_STORE"),
)


def lint_no_default_store_path(source_root: Path) -> list[str]:
    """Verify library code holds no hard-coded default parity-store path.

    Scans Python modules under *source_root* and returns human-readable violations.
    """
    violations: list[str] = []
    if not source_root.exists():
        return violations
    for py_file in sorted(source_root.rglob("*.py")):
        try:
            text = py_file.read_text(encoding="utf-8")
            tree = ast.parse(text, filename=str(py_file))
        except (OSError, SyntaxError) as exc:
            violations.append(f"{py_file}: failed to inspect ({exc})")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and any(
                pat.search(node.name) for pat in _FORBIDDEN_DEFAULT_PATH_PATTERNS
            ):
                violations.append(
                    f"{py_file}:{node.lineno}: forbidden default parity store function {node.name!r}"
                )
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                for pat in _FORBIDDEN_DEFAULT_PATH_PATTERNS[:2]:
                    if pat.search(node.value):
                        violations.append(
                            f"{py_file}:{node.lineno}: hard-coded parity store path string {node.value!r}"
                        )
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and any(
                        pat.search(target.id) for pat in _FORBIDDEN_DEFAULT_PATH_PATTERNS
                    ):
                        violations.append(
                            f"{py_file}:{node.lineno}: forbidden default parity store constant {target.id!r}"
                        )
    return violations
