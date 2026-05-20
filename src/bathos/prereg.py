from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from bathos.sidecar import Sidecar, find_sidecar, parse_sidecar
from bathos.validate import ValidationResult, validate_sidecar

AgentMode = Literal["collaborative", "autonomous"]


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
    error_payload: dict | None = None  # structured MCP-ready error


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
    """
    from bathos.query import _resolve_backend, run_sql

    # Normalize script path to a comparable string
    script_str = str(script_path.resolve())
    try:
        if _resolve_backend(catalog_dir) == "warm":
            rows = run_sql(
                f"SELECT COUNT(*) FROM runs WHERE command LIKE '%{script_str}%' AND git_hash = '{git_hash}'",
                catalog_dir,
            )
            return rows[0][0] == 0
    except Exception:
        pass
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
            gate="sidecar_missing",
            errors=[f"No sidecar found: {script_path.stem}.bth.toml"],
            mode=mode,
            script_path=script_path,
        )
        return GateResult(ok=False, mode=mode, bundle=bundle, error_payload=payload)

    sidecar = parse_sidecar(bundle.path)
    validation = validate_sidecar(sidecar)

    if not validation.ok:
        payload = _gate_failure_payload(
            gate="sidecar_invalid",
            errors=[f"[{e.field}] {e.message}" for e in validation.errors],
            mode=mode,
            script_path=script_path,
        )
        return GateResult(ok=False, mode=mode, bundle=bundle, validation=validation, error_payload=payload)

    # Autonomous mode: enforce first-of-kind
    if mode == "autonomous" and catalog_dir and git_hash:
        if not check_first_of_kind(script_path, catalog_dir, git_hash):
            payload = _gate_failure_payload(
                gate="not_first_of_kind",
                errors=["Script has prior runs — autonomous sidecar generation disallowed for iterated scripts"],
                mode=mode,
                script_path=script_path,
            )
            return GateResult(ok=False, mode=mode, bundle=bundle, validation=validation, error_payload=payload)

    return GateResult(ok=True, mode=mode, bundle=bundle, validation=validation)


def _gate_failure_payload(gate: str, errors: list[str], mode: str, script_path: Path) -> dict:
    return {
        "status": "gate_failure",
        "gate": gate,
        "errors": errors,
        "agent_mode": mode,
        "remediation": f"Create {script_path.stem}.bth.toml with [experiment], [outcomes.*], [result_schema] sections. Each outcome needs condition (DuckDB SQL), decision, and reasoning. One outcome must have is_residual=true.",
        "gate_schema_version": 1,
    }
