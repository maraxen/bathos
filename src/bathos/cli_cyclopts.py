"""Preview cyclopts CLI, driven by cisternal's `wire()` — backlog #4702 Milestone 1.

This is a standalone, additional console-script entry point (`bth-campaign-preview`
in `[project.scripts]`) proving the registry-driven CLI pattern end to end for
the `campaign` command group. It is NOT the shipped `bth` binary: `bathos.cli`'s
hand-written Typer `campaign_app` subgroup is untouched and remains what the
real `bth campaign ...` command runs. Typer and cyclopts are not interoperable
frameworks, and mounting a cyclopts sub-app inside `bth`'s existing Typer app
(or vice versa) is not a supported path — see the Milestone 1 plan's "safety
decision" in `.praxia/docs/plans/` for the reasoning.

Cutting the real `bth` entry point over to a cyclopts-generated `campaign`
group (and deleting the old Typer subgroup) is explicit follow-on scope,
bundled with migrating the other 11 command groups.
"""

from __future__ import annotations

import cyclopts
from cisternal import wire

from bathos.cli_render import cyclopts_result_action

app = cyclopts.App(name="bth-campaign-preview", result_action=cyclopts_result_action)

# Importing bathos.mcp executes its module-level @cisternal.tool decorations,
# populating the "bathos-cli" registry partition this wire() call snapshots.
import bathos.mcp as _bathos_mcp  # noqa: E402 — must follow app construction, F401 — decoration side effect

wire(
    None,
    app,
    registry="bathos-cli",
    expected=[
        "campaign_create",
        "campaign_add",
        "campaign_conclude",
        "claim_attest_parity",
        "campaign_list",
        "campaign_show",
        "campaign_review",
    ],
)

del _bathos_mcp


if __name__ == "__main__":
    app()
