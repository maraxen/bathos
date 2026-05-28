"""W3C PROV-JSON format for bathos lineage.

Converts bathos Run lineage chains to W3C PROV-JSON 1.0 format,
using the bth: namespace for bathos-specific extensions.
"""

from __future__ import annotations

from bathos.schema import Run


def format_prov_json(runs: list[Run]) -> dict:
    """Format a lineage chain as W3C PROV-JSON 1.0.

    Uses bth: namespace for bathos-specific fields.
    Entities represent runs, activities represent executions,
    agents represent who triggered the run, and wasDerivedFrom
    links parent-child relationships.

    Args:
        runs: List of Run objects in chronological order (oldest first).

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

    # Build wasDerivedFrom links for parent-child relationships
    for i, run in enumerate(runs):
        if run.parent_run_id:
            # Find parent run key (parent_run_id is full UUID, shorten to 8 chars)
            parent_key = f"bth:run_{run.parent_run_id[:8]}"
            child_key = f"bth:run_{run.id[:8]}"

            # Only add if parent exists in our entities
            if parent_key in entities:
                derived_key = f"_:derived_{i}"
                was_derived_from[derived_key] = {
                    "prov:generatedEntity": child_key,
                    "prov:usedEntity": parent_key,
                }

    return {
        "entity": entities,
        "activity": activities,
        "agent": agents,
        "wasDerivedFrom": was_derived_from,
    }
