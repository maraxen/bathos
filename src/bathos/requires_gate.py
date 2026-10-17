"""Enforcement of the optional ``[requires]`` and ``[budget]`` sidecar tables (spec 261016 B2).

``check_requires`` is the single decision point. ``gate_check`` (``bathos.prereg``) and the
``bth submit`` / ``bth preflight`` commands all call it, so a verdict cannot differ by entry point.

Outcomes:
  * ``pass``  -- nothing to enforce, every requirement met, or a ``[requires.waiver]`` is present.
  * ``warn``  -- a requirement is unmet but not enforced yet (exploration/calibration stage, or the
    ``[gate] requires_enforce_after`` cutoff is unset or in the future), or the JAX heuristic fired.
  * ``block`` -- a requirement is unmet at a validation, ablation or production stage once enforcement is
    active. A sidecar with no stage resolves to exploration and only warns.

Every unexpected failure inside the checks is reported as unmet, never as a pass (fail closed).
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from bathos.prereg import GateErrorCode
from bathos.telemetry import event

if TYPE_CHECKING:
    from bathos.sidecar import BudgetBlock, RequiresBlock, Sidecar

logger = logging.getLogger(__name__)

_ENFORCED_STAGES = ("validation", "ablation", "production")
_JAX_IMPORT = re.compile(r"^\s*(import jax|from jax\b)", re.MULTILINE)
_HEX = re.compile(r"[0-9a-fA-F]{7,}")
_GENERIC_HINT = 'Produce the artifact named in [requires] at the current commit, or add [requires.waiver] reason = "..."'
_BUDGET_HINT = "Re-measure with the probe and update [budget] per_unit_s / est_wall_s to at least the measured value"
_JAX_HINT = (
    "Profile one unit with `xtrax profile` and cite it, or add a [requires] table to the sidecar"
)
_GIT_TIMEOUT_S = 10


@dataclass(frozen=True)
class RequiresOutcome:
    status: Literal["pass", "warn", "block"]
    error_code: GateErrorCode | None  # set when status != "pass" due to an unmet requirement
    errors: list[str]
    hint: str
    waiver_reason: str | None


def _pass(waiver_reason: str | None = None) -> RequiresOutcome:
    return RequiresOutcome("pass", None, [], "", waiver_reason)


def _jax_heuristic(script_path: Path | None) -> RequiresOutcome:
    """A JAX script with no [requires]/[budget] table only ever warns."""
    if script_path is None:
        return _pass()
    try:
        source = script_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _pass()
    if _JAX_IMPORT.search(source):
        return RequiresOutcome(
            "warn",
            None,
            [
                "JAX workload with no [requires] table; profile one unit with `xtrax profile` and cite it"
            ],
            _JAX_HINT,
            None,
        )
    return _pass()


def _walk_key(data: object, dotted: str) -> tuple[bool, object]:
    """Follow a dotted key path through nested dicts. Returns (found, value)."""
    current = data
    for segment in dotted.split("."):
        if not isinstance(current, dict) or segment not in current:
            return False, None
        current = current[segment]
    return True, current


def _inside(path: Path, root: Path) -> bool:
    """True iff ``path`` (symlinks followed) lies within ``root`` (symlinks followed)."""
    return path.resolve().is_relative_to(root.resolve())


def _hashes_match(value: str, expected: str, *, allow_prefix: bool) -> bool:
    """Equal strings; with ``allow_prefix`` (HEAD only), two hex strings (>= 7 chars) where the longer
    starts with the shorter. A literal ``must_equal`` never prefix-matches."""
    if value == expected:
        return True
    if allow_prefix and _HEX.fullmatch(value) and _HEX.fullmatch(expected):
        short, long_ = sorted((value.lower(), expected.lower()), key=len)
        return long_.startswith(short)
    return False


def _resolve_head(sidecar_dir: Path) -> str:
    """The current commit of the repository holding the sidecar. Raises on any failure."""
    # Drop every GIT_* redirect (GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE, GIT_COMMON_DIR, ...): an
    # ambient one would make `-C dir` resolve HEAD in another repository and pass a stale sha.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    proc = subprocess.run(
        ["git", "-C", str(sidecar_dir), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_S,
        check=False,
        env=env,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"git exited {proc.returncode}")
    sha = proc.stdout.strip()
    if not sha:
        raise RuntimeError("git printed no commit")
    return sha


def _requires_errors(requires: RequiresBlock, sidecar_path: Path) -> list[str]:
    """Unmet-requirement messages for a [requires] table. Empty means met."""
    if requires.artifact is None:
        return ["[requires] declares no artifact"]
    glob = requires.artifact
    matches = [p for p in sidecar_path.parent.glob(glob) if p.is_file()]
    if not matches:
        return [f"no file matches artifact glob {glob}"]
    chosen = max(matches, key=lambda p: p.stat().st_mtime)
    if not _inside(chosen, sidecar_path.parent):
        return [
            f"artifact resolves outside the sidecar directory: {chosen.name} -> {chosen.resolve()}"
        ]
    notes: list[str] = []
    if len(matches) > 1:
        notes.append(f"{len(matches)} files match {glob}; used the newest, {chosen.name}")

    if requires.json_key is None:
        return []  # artifact existence is the whole requirement

    try:
        data = json.loads(chosen.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        return [*notes, f"cannot read {chosen.name} as JSON: {e}"]
    found, raw_value = _walk_key(data, requires.json_key)
    if not found:
        return [*notes, f"{chosen.name} has no key {requires.json_key}"]
    if not isinstance(raw_value, str):
        return [*notes, f"{requires.json_key} is {type(raw_value).__name__}, expected a string"]
    value = raw_value.strip()

    errors: list[str] = []
    for suffix in requires.reject_suffixes:
        if value.casefold().endswith(suffix.casefold()):
            errors.append(f"{requires.json_key} = {value} ends with {suffix}")
            break

    if requires.must_equal is not None:
        token = requires.must_equal.strip()
        if token == "HEAD":
            try:
                expected = _resolve_head(sidecar_path.parent)
            except Exception as e:  # noqa: BLE001 - any failure to resolve HEAD is unmet
                return [*notes, *errors, f"cannot resolve HEAD: {e}"]
            if not _hashes_match(value, expected, allow_prefix=True):
                errors.append(f"{requires.json_key} = {value}, expected HEAD {expected}")
        elif not _hashes_match(value, token, allow_prefix=False):
            errors.append(f"{requires.json_key} = {value}, expected {token}")

    if errors:
        errors = [*notes, *errors]
    return errors


def _budget_errors(budget: BudgetBlock, sidecar_path: Path) -> list[str]:
    """Unmet-budget messages for a [budget] table. Empty means met."""
    if budget.probe_record is None:
        return []
    probe = sidecar_path.parent / budget.probe_record
    if probe.exists() and not _inside(probe, sidecar_path.parent):
        return [f"probe_record {budget.probe_record} resolves outside the sidecar directory"]
    try:
        record = json.loads(probe.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"probe_record {budget.probe_record} not found"]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as e:
        return [f"cannot read probe_record {budget.probe_record} as JSON: {e}"]

    if budget.record_per_unit_key is None or budget.per_unit_s is None:
        return []
    found, raw_value = _walk_key(record, budget.record_per_unit_key)
    if not found:
        return [f"probe_record {budget.probe_record} has no key {budget.record_per_unit_key}"]
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        return [
            f"probe_record {budget.probe_record} key {budget.record_per_unit_key} is not a number: "
            f"{raw_value!r}"
        ]
    measured = float(raw_value)
    if not math.isfinite(measured) or measured <= 0:
        return [
            f"probe record value at {budget.record_per_unit_key} is {raw_value}, "
            "not a positive finite number"
        ]
    floor = measured * (1 - budget.tolerance)
    if budget.per_unit_s < floor:
        return [
            f"budget per_unit_s {budget.per_unit_s} underestimates the measured {measured} "
            f"by more than tolerance {budget.tolerance}"
        ]
    return []


def _enforcement_active(sidecar_path: Path, today: date) -> bool:
    """True iff [gate] requires_enforce_after is set and today >= it. Raises on a malformed value."""
    from bathos.config import load_requires_enforce_after

    cutoff = load_requires_enforce_after(sidecar_path.parent)
    return cutoff is not None and today >= cutoff


def check_requires(
    sidecar: Sidecar,
    sidecar_path: Path,
    *,
    script_path: Path | None,
    project_root: Path | None,
    today: date | None = None,
) -> RequiresOutcome:
    """Decide the [requires]/[budget] verdict for one sidecar. See the module docstring.

    ``project_root`` is accepted for the gate's call shape; the ``.bth.toml`` that carries
    ``[gate] requires_enforce_after`` is found by walking up from the sidecar's directory, which is
    the same rule every other project setting uses.

    Raises :class:`bathos.config.EnforcementConfigError` when a sidecar has a [requires]/[budget] table
    and the ``[gate]`` cutoff is malformed (the caller maps that to a block).
    """
    del project_root  # documented above; the config is located from the sidecar's directory
    requires = sidecar.requires
    budget = sidecar.budget
    if requires is None and budget is None:
        return _jax_heuristic(script_path)

    # Validated eagerly: a malformed [gate] cutoff fails closed even when every requirement is met.
    enforce = _enforcement_active(sidecar_path, today or date.today())

    unmet: list[str] = []
    codes: list[GateErrorCode] = []
    hints: list[str] = []
    waiver_reason: str | None = None

    if requires is not None:
        if requires.waiver is not None:
            waiver_reason = requires.waiver.reason
            event("prereg.requires_waived", sidecar=str(sidecar_path), reason=waiver_reason)
        else:
            try:
                errors = _requires_errors(requires, sidecar_path)
            except Exception as e:  # noqa: BLE001 - fail closed on anything unexpected
                errors = [f"internal error in requires gate: {type(e).__name__}: {e}"]
            if errors:
                unmet.extend(errors)
                codes.append(GateErrorCode.REQUIRES_UNMET)
                hints.append((requires.hint if requires.hint else None) or _GENERIC_HINT)

    if budget is not None:
        try:
            errors = _budget_errors(budget, sidecar_path)
        except Exception as e:  # noqa: BLE001 - fail closed on anything unexpected
            unmet.append(f"internal error in requires gate: {type(e).__name__}: {e}")
            codes.append(GateErrorCode.REQUIRES_UNMET)
            hints.append(_BUDGET_HINT)
        else:
            if errors:
                unmet.extend(errors)
                codes.append(GateErrorCode.BUDGET_UNMET)
                hints.append(_BUDGET_HINT)

    if not unmet:
        return _pass(waiver_reason)

    error_code = (
        GateErrorCode.REQUIRES_UNMET
        if GateErrorCode.REQUIRES_UNMET in codes
        else GateErrorCode.BUDGET_UNMET
    )
    stage = sidecar.gate_stage
    status: Literal["warn", "block"] = (
        "block" if (stage in _ENFORCED_STAGES and enforce) else "warn"
    )
    return RequiresOutcome(status, error_code, unmet, " ; ".join(hints), waiver_reason)


def discover_sidecar(script_path: Path) -> Path | None:
    """The sidecar adjacent to a script (``<stem>.bth.toml``), or None."""
    candidate = script_path.parent / f"{script_path.stem}.bth.toml"
    return candidate if candidate.exists() else None


def preflight(
    script_path: Path | None,
    sidecar_path: Path | None,
    *,
    today: date | None = None,
) -> RequiresOutcome:
    """Parse a sidecar (when there is one) and run :func:`check_requires`.

    With no sidecar, or one that declares neither table, only the JAX heuristic applies. A sidecar
    that declares a table and fails to parse raises ``SidecarError``; callers treat that as a block.
    """
    import tomllib

    from bathos.sidecar import parse_sidecar

    if script_path is not None:
        from bathos.sidecar import is_in_enforced_dir

        # Same rule as gate_check: a script outside an enforced directory is not gated (pass).
        if not is_in_enforced_dir(script_path):
            return _pass()
    if sidecar_path is None:
        return _jax_heuristic(script_path)
    # A sidecar that declares neither table (e.g. a [cluster]-only sidecar used for submit config)
    # has nothing to enforce, so it is not parsed as an experiment sidecar here.
    raw = tomllib.loads(sidecar_path.read_text(encoding="utf-8"))
    if "requires" not in raw and "budget" not in raw:
        return _jax_heuristic(script_path)
    sidecar = parse_sidecar(sidecar_path)
    return check_requires(
        sidecar, sidecar_path, script_path=script_path, project_root=None, today=today
    )
