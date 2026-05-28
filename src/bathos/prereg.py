from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Literal

import duckdb

from bathos.sidecar import Sidecar, find_sidecar, parse_sidecar
from bathos.validate import ValidationResult, validate_sidecar
from bathos.telemetry import event

AgentMode = Literal["collaborative", "autonomous"]

logger = logging.getLogger(__name__)


class GateErrorCode(str, Enum):
    """Enumeration of structured error codes for pre-registration gate failures."""
    SIDECAR_MISSING = "sidecar_missing"
    SIDECAR_INVALID = "sidecar_invalid"
    SIDECAR_HASH_MISMATCH = "sidecar_hash_mismatch"
    NOT_FIRST_OF_KIND = "not_first_of_kind"
    MANIFEST_WRITE_FAILED = "manifest_write_failed"
    ADVERSARIAL_CHECK_MISSING = "adversarial_check_missing"
    HYPOTHESIS_LOCK_MISSING = "hypothesis_lock_missing"
    OUTCOME_EVALUATION_ERROR = "outcome_evaluation_error"
    RESULT_SCHEMA_MISMATCH = "result_schema_mismatch"
    OUTCOME_AMBIGUOUS = "outcome_ambiguous"
    INTERNAL = "internal"


@dataclass
class GateErrorPayload:
    """Structured payload for gate check failures, ready for MCP serialization."""
    error_code: GateErrorCode
    phase: Literal["pre_execution", "post_execution"]
    taxonomy_label: str
    errors: list[str]
    agent_mode: str
    resolution_hint: str
    gate_schema_version: int = 2


class GateError(Exception):
    """Exception raised when a pre-registration gate check fails."""
    def __init__(self, message: str, payload: GateErrorPayload | None = None):
        super().__init__(message)
        self.payload = payload


@dataclass
class SidecarBundle:
    path: Path | None
    sha256: str  # hex digest, "" if no sidecar
    found: bool
    generated: bool = False  # True if agent-generated in autonomous mode


@dataclass
class GateResult:
    ok: bool
    mode: AgentMode
    bundle: SidecarBundle
    validation: ValidationResult | None = None
    error_payload: GateErrorPayload | None = None  # structured MCP-ready error


_RESOLUTION_HINTS: dict[GateErrorCode, str] = {
    GateErrorCode.SIDECAR_MISSING: "Create a .bth.toml sidecar adjacent to the script",
    GateErrorCode.SIDECAR_INVALID: "Fix the sidecar TOML syntax or missing required sections",
    GateErrorCode.SIDECAR_HASH_MISMATCH: "Re-run 'bth hypothesis lock' to regenerate the manifest",
    GateErrorCode.NOT_FIRST_OF_KIND: "Use --derived-from to link to the parent run",
    GateErrorCode.MANIFEST_WRITE_FAILED: "Check write permissions in the script directory",
    GateErrorCode.ADVERSARIAL_CHECK_MISSING: "Add adversarial_check to all outcomes.pass blocks in the sidecar",
    GateErrorCode.HYPOTHESIS_LOCK_MISSING: "Run 'bth hypothesis lock <script>' before executing",
    GateErrorCode.OUTCOME_EVALUATION_ERROR: "Fix the DuckDB SQL condition in the sidecar outcomes block",
    GateErrorCode.RESULT_SCHEMA_MISMATCH: "Ensure script output JSON matches the result_schema in the sidecar",
    GateErrorCode.OUTCOME_AMBIGUOUS: "Ensure exactly one outcome condition evaluates to true",
    GateErrorCode.INTERNAL: "File a bug report with the full error message",
}


def _gate_failure_payload(
    error_code: GateErrorCode,
    phase: Literal["pre_execution", "post_execution"],
    errors: list[str],
    agent_mode: str,
    taxonomy_label: str = "",
) -> GateErrorPayload:
    """Generate structured error payload for gate check failures."""
    return GateErrorPayload(
        error_code=error_code,
        phase=phase,
        taxonomy_label=taxonomy_label or error_code.value,
        errors=errors,
        agent_mode=agent_mode,
        resolution_hint=_RESOLUTION_HINTS.get(error_code, ""),
        gate_schema_version=2,
    )


def resolve_sidecar(script_path: Path) -> SidecarBundle:
    """Find and hash the sidecar adjacent to script_path."""
    p = find_sidecar(script_path)
    if p is None:
        return SidecarBundle(path=None, sha256="", found=False)
    sha = hashlib.sha256(p.read_bytes()).hexdigest()
    return SidecarBundle(path=p.resolve(), sha256=sha, found=True)


def resolve_agent_mode(
    cli_flag: str | None,
    sidecar: Sidecar | None,
    project_config=None,  # ProjectConfig | None
    global_config: dict | None = None,
) -> AgentMode:
    """Priority: CLI flag → sidecar [experiment] agent_mode → project [defaults] → global → 'collaborative'."""
    if cli_flag in ("collaborative", "autonomous"):
        return cli_flag  # type: ignore
    if sidecar and sidecar.agent_mode in ("collaborative", "autonomous"):
        return sidecar.agent_mode  # type: ignore
    if project_config is not None:
        # ProjectConfig may have a .defaults dict from [defaults] section
        defaults = getattr(project_config, "defaults", {}) or {}
        if defaults.get("agent_mode") in ("collaborative", "autonomous"):
            return defaults["agent_mode"]  # type: ignore
    if global_config:
        val = global_config.get("defaults", {}).get("agent_mode")
        if val in ("collaborative", "autonomous"):
            return val  # type: ignore
    return "collaborative"


def check_first_of_kind(script_path: Path, catalog_dir: Path, git_hash: str) -> bool:
    """Return True if no prior run in the catalog shares (script_path, git_hash).

    Uses HEAD hash (same value runner.py writes as run.git_hash) so the check
    is semantically consistent with what the catalog contains.
    Q5 resolution: first-of-kind = no prior run with same (command LIKE script_path%, git_hash).

    Raises GateError if warm DB check fails (not merely absent).
    """
    from bathos.query import _resolve_backend

    # Normalize script path to a comparable string
    script_str = str(script_path.resolve())
    try:
        if _resolve_backend(catalog_dir) == "warm":
            db_path = catalog_dir / "bathos.db"
            if db_path.exists():
                con = duckdb.connect(str(db_path), read_only=True)
                try:
                    rows = con.execute(
                        "SELECT COUNT(*) FROM runs WHERE command LIKE ? AND git_hash = ?",
                        [f"%{script_str}%", git_hash]
                    ).fetchall()
                    con.close()
                    return rows[0][0] == 0
                finally:
                    if not con.closed:
                        con.close()
    except Exception as e:
        logger.warning(f"Warm tier gate check failed: {e}")
    # If warm tier unavailable, scan cool tier
    from bathos.query import list_runs

    runs = list_runs(catalog_dir)
    return not any(
        script_str in (r.command or "") and r.git_hash == git_hash
        for r in runs
    )


def gate_check(
    script_path: Path,
    bundle: SidecarBundle,
    mode: AgentMode,
    catalog_dir: Path | None = None,
    git_hash: str = "",
) -> GateResult:
    """Run the pre-registration gate. Returns GateResult with ok=True or structured error."""
    from bathos.sidecar import is_in_enforced_dir

    if not is_in_enforced_dir(script_path):
        # Ungated directory — always pass
        return GateResult(ok=True, mode=mode, bundle=bundle)

    if not bundle.found:
        payload = _gate_failure_payload(
            error_code=GateErrorCode.SIDECAR_MISSING,
            phase="pre_execution",
            errors=[f"No sidecar found: {script_path.stem}.bth.toml"],
            agent_mode=mode,
        )
        event("prereg.gate_deny", script_path=str(script_path), reason="sidecar_missing", agent_mode=mode)
        return GateResult(ok=False, mode=mode, bundle=bundle, error_payload=payload)

    sidecar = parse_sidecar(bundle.path)
    validation = validate_sidecar(sidecar, sidecar_path=bundle.path)

    if not validation.ok:
        payload = _gate_failure_payload(
            error_code=GateErrorCode.SIDECAR_INVALID,
            phase="pre_execution",
            errors=[f"[{e.field}] {e.message}" for e in validation.errors],
            agent_mode=mode,
        )
        event("prereg.gate_deny", script_path=str(script_path), reason="sidecar_invalid", agent_mode=mode)
        return GateResult(ok=False, mode=mode, bundle=bundle, validation=validation, error_payload=payload)

    # Autonomous mode: enforce first-of-kind
    if mode == "autonomous" and catalog_dir and git_hash:
        if not check_first_of_kind(script_path, catalog_dir, git_hash):
            payload = _gate_failure_payload(
                error_code=GateErrorCode.NOT_FIRST_OF_KIND,
                phase="pre_execution",
                errors=["Script has prior runs — autonomous sidecar generation disallowed for iterated scripts"],
                agent_mode=mode,
            )
            event("prereg.gate_deny", script_path=str(script_path), reason="not_first_of_kind", agent_mode=mode)
            return GateResult(ok=False, mode=mode, bundle=bundle, validation=validation, error_payload=payload)

    # Adversarial check enforcement (required for agent-mode)
    if mode == "autonomous":
        missing_adversarial = [
            label for label, outcome in sidecar.outcomes.items()
            if label == "pass" and outcome.adversarial_check is None
        ]
        if missing_adversarial:
            payload = _gate_failure_payload(
                error_code=GateErrorCode.ADVERSARIAL_CHECK_MISSING,
                phase="pre_execution",
                errors=[f"outcomes.{label}.adversarial_check is missing" for label in missing_adversarial],
                agent_mode=mode,
            )
            event("prereg.gate_deny", script_path=str(script_path), reason="adversarial_check_missing", agent_mode=mode)
            raise GateError("adversarial_check required in agent mode", payload=payload)

    event("prereg.gate_pass", script_path=str(script_path), sidecar_sha256=bundle.sha256, agent_mode=mode)
    return GateResult(ok=True, mode=mode, bundle=bundle, validation=validation)


