"""Synthetic-recovery gate ledger (BP-2): staleness-aware attestation for pipeline-soundness confounds.

Ported from asr's C1 pre-run synthetic-invariant gate (`.praxia/c1_invariant_gates.toml` +
`scripts/gates/c1_invariant_gate.py`), generalized into a project-agnostic bathos primitive.

bathos does not execute tests itself (it is stack-agnostic across Python/Rust/JAX projects).
This module owns only two things: recording a self-attested pass/fail stamp for a named gate
(`stamp_gate`), and judging whether that stamp is still trustworthy (`gate_state`) by checking
whether any of its declared guarded paths have changed since the stamp's recorded commit. Proving
the test currently passes is the caller's job (a project's own CI/test runner calls `bth gate
stamp` after running its own invariant test) — this is an accepted trust boundary, not a solved
one; see `.praxia/docs/decisions/260721_bp2-bp3-claim-tier-gate-ports.md`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from bathos.git import paths_changed_since
from bathos.telemetry import event

LEDGER_RELATIVE_PATH = Path(".bth") / "synthetic_recovery_ledger.json"


@dataclass
class GateEntry:
    """A single recorded gate stamp."""

    gate_name: str
    result: str  # "pass" | "fail"
    sha: str
    ts: str


def _ledger_path(workspace_root: Path) -> Path:
    return workspace_root / LEDGER_RELATIVE_PATH


def load_ledger(workspace_root: Path) -> dict:
    """Load the synthetic-recovery ledger, or an empty structure if absent/corrupt."""
    path = _ledger_path(workspace_root)
    if not path.exists():
        return {"schema_version": 1, "gates": {}}
    try:
        with path.open() as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {"schema_version": 1, "gates": {}}
    data.setdefault("gates", {})
    return data


def save_ledger(workspace_root: Path, ledger: dict) -> None:
    path = _ledger_path(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w") as fh:
        json.dump(ledger, fh, indent=2, sort_keys=True)
        fh.write("\n")


def stamp_gate(workspace_root: Path, gate_name: str, result: str, sha: str) -> GateEntry:
    """Record a self-attested pass/fail for `gate_name` at the given git sha.

    Args:
        workspace_root: Project workspace root (ledger lives at <root>/.bth/synthetic_recovery_ledger.json)
        gate_name: Name of the gate being stamped (matches a claim's [confounds.synthetic_recovery].gate_name)
        result: "pass" or "fail"
        sha: git HEAD sha at stamp time

    Raises:
        ValueError: If result is not "pass" or "fail"
    """
    if result not in ("pass", "fail"):
        raise ValueError(f"result must be 'pass' or 'fail', got {result!r}")

    ledger = load_ledger(workspace_root)
    ts = datetime.now(UTC).isoformat()
    ledger["gates"][gate_name] = {"result": result, "sha": sha, "ts": ts}
    save_ledger(workspace_root, ledger)

    event("gate.stamp", gate_name=gate_name, result=result, sha=sha)

    return GateEntry(gate_name=gate_name, result=result, sha=sha, ts=ts)


def gate_state(workspace_root: Path, gate_name: str, guards: list[str]) -> str:
    """Compute the current state of a named gate: GREEN, STALE, RED, or UNKNOWN.

    - UNKNOWN: no ledger entry for gate_name (never stamped).
    - RED: last recorded result was "fail".
    - STALE: last result was "pass", but a guarded path has changed since the recorded sha.
    - GREEN: last result was "pass" and no guarded path has changed since.

    Fail-closed by construction: only GREEN is ever treated as sound elsewhere in the claim tier.
    """
    ledger = load_ledger(workspace_root)
    entry = ledger.get("gates", {}).get(gate_name)

    if entry is None:
        return "UNKNOWN"
    if entry.get("result") != "pass":
        return "RED"
    if paths_changed_since(entry.get("sha", ""), guards, cwd=workspace_root):
        return "STALE"
    return "GREEN"


def synthetic_recovery_confound_check(claim, workspace_root: Path) -> dict:
    """Check confounds with [confounds.synthetic_recovery] blocks and infer their status.

    Mirrors `bathos.claim.parity_confound_check`'s shape: for each confound carrying a
    synthetic_recovery block, computes 'controlled' (gate GREEN) or 'uncontrolled' (gate
    STALE/RED/UNKNOWN, or block malformed) — fail-closed default uncontrolled.

    Args:
        claim: Parsed ClaimFile (bathos.claim.ClaimFile)
        workspace_root: Project workspace root

    Returns:
        Dict with 'confounds' key: list of {id, label, gate_name, status, gate_state}
    """
    from bathos.claim import display_label

    result_confounds = []

    for confound in claim.confounds:
        synth = confound.get("synthetic_recovery", {})
        if not synth:
            continue

        confound_info = {
            "id": confound.get("id", "unknown"),
            "label": display_label(confound),
            "gate_name": synth.get("gate_name", ""),
            "status": "uncontrolled",
            "gate_state": "UNKNOWN",
        }

        gate_name = synth.get("gate_name", "")
        guards = synth.get("guards", [])

        if not gate_name:
            result_confounds.append(confound_info)
            continue

        state = gate_state(workspace_root, gate_name, guards)
        confound_info["gate_state"] = state
        confound_info["status"] = "controlled" if state == "GREEN" else "uncontrolled"

        result_confounds.append(confound_info)

    return {"confounds": result_confounds}
