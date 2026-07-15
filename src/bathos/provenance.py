"""W3C PROV-JSON format for bathos lineage.

Converts bathos Run lineage chains to W3C PROV-JSON 1.0 format,
using the bth: namespace for bathos-specific extensions.

Multi-parent wasDerivedFrom (B2-03, #2181): `run_parent_edges` is an optional lookup a caller
builds from `bathos.campaign_edges.get_run_parents` (one call per run, assembled into a dict) --
this module stays a pure formatter with no direct DuckDB dependency, so the DB query lives in
`campaign_edges`, not here. Every existing caller that omits `run_parent_edges` gets output
byte-identical to before this parameter existed (falls back to the single `Run.parent_run_id`
field), which is the "single -> multi-parent is natural schema evolution, not a breaking change"
story B2-03's own gate text describes.
"""

from __future__ import annotations

from collections.abc import Mapping

from bathos.schema import Run


def format_prov_json(
    runs: list[Run],
    *,
    run_parent_edges: Mapping[str, list[str]] | None = None,
) -> dict:
    """Format a lineage chain as W3C PROV-JSON 1.0.

    Uses bth: namespace for bathos-specific fields.
    Entities represent runs, activities represent executions,
    agents represent who triggered the run, and wasDerivedFrom
    links parent-child relationships.

    Args:
        runs: List of Run objects in chronological order (oldest first).
        run_parent_edges: optional; `run.id -> [parent_run_id, ...]` (multi-parent, B2-03).
            A run present in this mapping emits one `wasDerivedFrom` link per listed parent,
            instead of just its single `parent_run_id` field. A run ABSENT from this mapping
            (or when the whole parameter is omitted) falls back to the existing single-parent
            `run.parent_run_id` behavior.

    Returns:
        Dictionary conforming to W3C PROV-JSON 1.0 schema with bth: namespace.
    """
    entities = {}
    activities = {}
    was_derived_from = {}
    agents = {}

    # Build entities (one per run), activities (one per execution),
    # and agents (one per unique agent mode)
    for run in runs:
        run_key = f"bth:run_{run.id[:8]}"

        # Entity: the run outcome
        entities[run_key] = {
            "prov:type": "bth:Run",
            "bth:run_id": run.id,
            "bth:outcome": run.outcome or "",
            "bth:manifest_sha256": run.manifest_sha256 or "not_recorded",
            "bth:git_sha": run.git_hash or "",
            "bth:timestamp": str(run.timestamp),
        }

        # Activity: the subprocess execution
        activity_key = f"bth:exec_{run.id[:8]}"
        activities[activity_key] = {
            "prov:type": "bth:Execution",
            "bth:run_id": run.id,
            "bth:command": run.command,
        }

        # Agent: who triggered the run (human, orchestrator, fixer, etc.)
        agent_id = run.agent_mode if run.agent_mode else "human"
        agent_key = f"bth:agent_{agent_id}"
        if agent_key not in agents:  # Only add once per unique agent
            agents[agent_key] = {
                "prov:type": "bth:Agent",
                "bth:id": agent_id,
            }

    # Build wasDerivedFrom links for parent-child relationships. A run present in
    # run_parent_edges emits one link per listed parent (multi-parent, B2-03); a run absent
    # from it falls back to its single parent_run_id field (pre-B2-03 behavior, unchanged).
    derived_idx = 0
    for run in runs:
        if run_parent_edges is not None and run.id in run_parent_edges:
            parent_ids = run_parent_edges[run.id]
        elif run.parent_run_id:
            parent_ids = [run.parent_run_id]
        else:
            parent_ids = []

        child_key = f"bth:run_{run.id[:8]}"
        for parent_id in parent_ids:
            parent_key = f"bth:run_{parent_id[:8]}"
            # Only add if parent exists in our entities
            if parent_key in entities:
                derived_key = f"_:derived_{derived_idx}"
                was_derived_from[derived_key] = {
                    "prov:generatedEntity": child_key,
                    "prov:usedEntity": parent_key,
                }
                derived_idx += 1

    return {
        "entity": entities,
        "activity": activities,
        "agent": agents,
        "wasDerivedFrom": was_derived_from,
    }
