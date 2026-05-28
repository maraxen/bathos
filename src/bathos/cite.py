"""Citation formatting for bathos runs.

Provides structured citation output linking runs to their hypotheses and manifests.
"""

from __future__ import annotations

import json

from bathos.schema import Run


def format_citation(run: Run, fmt: str = "markdown") -> str:
    """Format a structured citation for a run.

    Args:
        run: The Run object to cite.
        fmt: Output format. Options: "markdown" (default), "json".

    Returns:
        Formatted citation string.
    """
    # Handle pre-v0.6 runs with missing manifest fields
    manifest_hash = (
        run.manifest_sha256
        if run.manifest_sha256
        else "not recorded — pre-v0.6"
    )
    manifest_path_display = (
        run.manifest_path if run.manifest_path else "not recorded — pre-v0.6"
    )
    sidecar_hash = (
        run.sidecar_sha256
        if run.sidecar_sha256
        else "not recorded — pre-v0.6"
    )

    if fmt == "json":
        return json.dumps(
            {
                "run_id": run.id,
                "project_slug": run.project_slug,
                "command": run.command,
                "sidecar_sha256": sidecar_hash,
                "manifest_sha256": run.manifest_sha256,
                "manifest_path": run.manifest_path,
                "git_sha": run.git_hash,
                "outcome": run.outcome or "unknown",
                "timestamp": str(run.timestamp),
            },
            indent=2,
        )

    # markdown (default)
    lines = [
        f"Run {run.id[:8]}",
        f"  Project:          {run.project_slug}",
        f"  Hypothesis hash:  {sidecar_hash[:16] + '...' if sidecar_hash != 'not recorded — pre-v0.6' else sidecar_hash}",
        f"  Manifest hash:    {manifest_hash[:16] + '...' if manifest_hash != 'not recorded — pre-v0.6' else manifest_hash}",
        f"  Manifest path:    {manifest_path_display}",
        f"  Git SHA:          {run.git_hash[:8] if run.git_hash else 'unknown'}",
        f"  Outcome:          {run.outcome or 'unknown'}",
        f"  Timestamp:        {run.timestamp}",
    ]
    return "\n".join(lines)
