"""Preview cyclopts CLI, driven by cisternal's `wire()` — backlog #4702.

This is a standalone, additional console-script entry point (`bth-preview` in
`[project.scripts]`, renamed from `bth-campaign-preview` once Milestone 2
extended it past the campaign group) proving the registry-driven CLI pattern
end to end for each migrated command group/singleton. It is NOT the shipped
`bth` binary: `bathos.cli`'s hand-written Typer commands are untouched and
remain what the real `bth ...` commands run. Typer and cyclopts are not
interoperable frameworks, and mounting a cyclopts sub-app inside `bth`'s
existing Typer app (or vice versa) is not a supported path — see the
Milestone 1 plan's "safety decision" in `.praxia/docs/plans/` for the
reasoning.

Cutting the real `bth` entry point over to this cyclopts-generated surface
(and deleting the old Typer commands) is explicit follow-on scope, once every
command in `scripts/analysis/cli_migration_map.toml` is migrated — see the
Milestone 2 scope doc in `.praxia/docs/plans/`.

`expected=` below must list every `registry="bathos-cli"` tool name currently
registered in `bathos.mcp` — cisternal's `wire()` raises `CisternalWireError`
at import time if any listed name is missing, so a forgotten name here is
caught immediately rather than silently absent from the preview app.
"""

from __future__ import annotations

import cyclopts
from cisternal import wire

from bathos.cli_render import cyclopts_result_action

app = cyclopts.App(name="bth-preview", result_action=cyclopts_result_action)

# Importing bathos.mcp executes its module-level @cisternal.tool decorations,
# populating the "bathos-cli" registry partition this wire() call snapshots.
import bathos.mcp as _bathos_mcp  # noqa: E402 — must follow app construction, F401 — decoration side effect

wire(
    None,
    app,
    registry="bathos-cli",
    expected=[
        # campaign group (Milestone 1 pilot)
        "campaign_create",
        "campaign_add",
        "campaign_conclude",
        "claim_attest_parity",
        "campaign_list",
        "campaign_show",
        "campaign_review",
        # top-level singletons, direct decoration (Milestone 2)
        "list_runs",
        "find_runs",
        "get_run",
        "run_sql",
        "compact",
        "archive",
        "archive_artifact",
        "restore",
        "check",
        "sync",
        "init",
        "run",
        "verify",
        "lint",
        # top-level singletons, extracted (Milestone 2)
        "cite_run",
        "lineage_prov",
        "repair",
        "new_experiment",
        "validate_sidecar",
    ],
)

del _bathos_mcp


if __name__ == "__main__":
    app()
