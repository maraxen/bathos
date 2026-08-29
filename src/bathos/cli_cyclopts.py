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

import sys
from pathlib import Path
from typing import Annotated

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
        # anchor group (Milestone 2)
        "anchor_insert",
        "anchor_get",
        "anchor_find",
        "figure_entry_register",
        # attestation group (Milestone 2)
        "attestation_scaffold",
        "attestation_validate",
        "attestation_register",
        # blast-radius group, partial (Milestone 2 -- assess/clear only; the 3
        # hook-management commands have no MCP equivalent, stay CLI-only)
        "blast_radius_assess",
        "blast_radius_clear",
        # outputs group (Milestone 2)
        "list_outputs",
        "outputs_summary",
        # query group (Milestone 2)
        "resolve_pin",
        "get_trust_state",
        "query_attestation",
        "figure_lookup",
        "list_candidates",
        "get_blast_radius_status",
        # claim group (Milestone 2 -- extraction-heavy batch; claim_register
        # corrected from the codegen audit's "direct" classification, see
        # claim_register_tool's docstring)
        "claim_scaffold",
        "claim_validate",
        "claim_register",
        "claim_author",
        # gate group (Milestone 2)
        "gate_stamp",
        "gate_status",
        # postmortem group (Milestone 2)
        "postmortem_scaffold",
        "postmortem_validate",
        "postmortem_get",
        # ref group (Milestone 2)
        "reference_list",
        "reference_get",
        "reference_search",
        "reference_applicable",
    ],
)

del _bathos_mcp

# ---------------------------------------------------------------------------
# CLI-only batch (backlog #4702 Milestone 2, sequencing step 5): 22 commands
# with no MCP-tool equivalent, so no cisternal registry involvement -- these
# are hand-written cyclopts commands, ported directly from the equivalent
# bathos.cli Typer command bodies (typer.echo -> print, typer.Exit -> raise
# SystemExit; see the Milestone 2 close-out plan for the full substitution
# table). `cyclopts_result_action` (this app's `result_action`) only acts on
# `dict` returns, so it no-ops for all of these -- they print directly and
# signal failure via SystemExit, same as they always have.
# ---------------------------------------------------------------------------

remote_app = cyclopts.App(name="remote", help="Manage remote hosts for sync.")
app.command(remote_app)


@remote_app.command(name="add")
def remote_add_cmd(name: str, url: str) -> None:
    """Add a remote host for sync.

    Parameters
    ----------
    name: Remote name (e.g. 'engaging').
    url: host:path (e.g. 'engaging:~/projects/myproject').
    """
    from bathos.config import find_project_config
    from bathos.remote import add_remote

    cfg_path = find_project_config()
    if cfg_path is None:
        print("No .bth.toml found. Run 'bth init' first.")
        raise SystemExit(1)

    if ":" not in url:
        print(f"Invalid URL '{url}': expected 'host:path' format")
        raise SystemExit(1)

    host, path = url.split(":", 1)

    try:
        add_remote(cfg_path, name, host, path)
        print(f"Remote '{name}' added ({host}:{path})")
    except ValueError as e:
        print(str(e))
        raise SystemExit(1) from None


@remote_app.command(name="list")
def remote_list_cmd() -> None:
    """List configured remotes."""
    from bathos.config import find_project_config, load_project_config
    from bathos.remote import list_remotes

    cfg_path = find_project_config()
    if cfg_path is None:
        print("No .bth.toml found. Run 'bth init' first.")
        raise SystemExit(1)

    config = load_project_config(cfg_path)
    remotes = list_remotes(config)

    if not remotes:
        print("No remotes configured. Use 'bth remote add' to add one.")
        return

    name_width = max(len("NAME"), max((len(r[0]) for r in remotes), default=0), 10)
    host_path_width = max(
        len("HOST:PATH"), max((len(f"{r[1]}:{r[2]}") for r in remotes), default=0), 9
    )

    print(f"{'NAME':<{name_width}}  {'HOST:PATH':<{host_path_width}}")
    print("-" * name_width + "  " + "-" * host_path_width)

    for name, host, remote_root in remotes:
        host_path = f"{host}:{remote_root}"
        print(f"{name:<{name_width}}  {host_path:<{host_path_width}}")


@remote_app.command(name="remove")
def remote_remove_cmd(name: str) -> None:
    """Remove a configured remote.

    Parameters
    ----------
    name: Remote name to remove.
    """
    from bathos.config import find_project_config
    from bathos.remote import remove_remote

    cfg_path = find_project_config()
    if cfg_path is None:
        print("No .bth.toml found. Run 'bth init' first.")
        raise SystemExit(1)

    try:
        remove_remote(cfg_path, name)
        print(f"Remote '{name}' removed.")
    except ValueError as e:
        print(str(e))
        raise SystemExit(1) from None


@remote_app.command(name="test")
def remote_test_cmd(name: str) -> None:
    """Test SSH connectivity to a remote.

    Parameters
    ----------
    name: Remote name to test.
    """
    from bathos.config import find_project_config, load_project_config
    from bathos.remote import test_remote

    cfg_path = find_project_config()
    if cfg_path is None:
        print("No .bth.toml found. Run 'bth init' first.")
        raise SystemExit(1)

    config = load_project_config(cfg_path)

    try:
        result = test_remote(config, name)
    except ValueError as e:
        print(str(e))
        raise SystemExit(1) from None

    if result.success:
        print(f"{name}: ok ({result.latency_ms:.0f}ms)")
    else:
        print(f"{name}: unreachable — {result.error}")
        raise SystemExit(1)


report_app = cyclopts.App(
    name="report", help="Generate and emit campaign reports and manifests"
)
app.command(report_app)


@report_app.command(name="emit")
def report_emit_cmd(campaign_id: str) -> None:
    """Generate and emit campaign report and figure manifest sidecars.

    Creates both campaign_report.json and figure_manifest.json at
    <catalog>/sidecars/<campaign_id>/ for a concluded campaign.

    Parameters
    ----------
    campaign_id: Campaign ID.
    """
    from bathos.campaigns import (
        CampaignError,
        connect_catalog_db,
        emit_campaign_report,
        emit_figure_manifest,
        get_campaign,
        prepare_catalog_for_conclude,
    )
    from bathos.cli_common import catalog_dir

    cat_dir = catalog_dir()
    prepare_catalog_for_conclude(cat_dir)
    db = connect_catalog_db(cat_dir, read_only=False)
    try:
        if db is None:
            print("Catalog database not found; run `bth compact` first", file=sys.stderr)
            raise SystemExit(1)
        try:
            campaign = get_campaign(db, campaign_id, catalog_dir=cat_dir)
        except CampaignError as e:
            print(f"Error: {e}", file=sys.stderr)
            raise SystemExit(1) from None
        if campaign is None:
            print(f"Campaign not found: {campaign_id}", file=sys.stderr)
            raise SystemExit(1)
        if campaign.status != "concluded":
            print(
                f"Campaign {campaign_id[:8]} is not concluded (status: {campaign.status})",
                file=sys.stderr,
            )
            raise SystemExit(1)

        manifest_ref = f"sidecars/{campaign_id}/figure_manifest.json"
        emit_figure_manifest(db, str(cat_dir), campaign_id)
        emit_campaign_report(db, str(cat_dir), campaign_id, figure_manifest_ref=manifest_ref)

        report_path = cat_dir / "sidecars" / campaign_id / "campaign_report.json"
        manifest_path = cat_dir / "sidecars" / campaign_id / "figure_manifest.json"
        print(f"✓ Emitted campaign report and manifest for {campaign_id[:8]}")
        print(f"  Report:   {report_path}")
        print(f"  Manifest: {manifest_path}")
    except CampaignError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1) from None
    finally:
        if db is not None:
            db.close()


@report_app.command(name="show")
def report_show_cmd(campaign_id: str) -> None:
    """Read the campaign_report.json sidecar for a campaign (S1 read-back API; real).

    Thin CLI wrapper over bathos.readback.read_campaign_report. Emit the sidecar first
    with `bth report emit <campaign_id>`.

    Parameters
    ----------
    campaign_id: Campaign ID (full, exact match).
    """
    import json as json_mod

    from bathos.cli_common import catalog_dir
    from bathos.readback import read_campaign_report

    try:
        report = read_campaign_report(catalog_dir(), campaign_id)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1) from None
    print(json_mod.dumps(report.model_dump(), indent=2))


@report_app.command(name="show-manifest")
def report_show_manifest_cmd(campaign_id: str) -> None:
    """Read the figure_manifest.json sidecar for a campaign (S1 read-back API; real).

    Thin CLI wrapper over bathos.readback.read_figure_manifest. Emit the sidecar first
    with `bth report emit <campaign_id>`.

    Parameters
    ----------
    campaign_id: Campaign ID (full, exact match).
    """
    import json as json_mod

    from bathos.cli_common import catalog_dir
    from bathos.readback import read_figure_manifest

    try:
        manifest = read_figure_manifest(catalog_dir(), campaign_id)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1) from None
    print(json_mod.dumps(manifest.model_dump(), indent=2))


provenance_app = cyclopts.App(
    name="provenance",
    help="Inspect a run's durable git provenance (pinned commit, dirty-tree snapshot, diff)",
)
app.command(provenance_app)


@provenance_app.command(name="show")
def provenance_show_cmd(run_id: str) -> None:
    """Show what was durably pinned for a run: HEAD, the pinned commit, and whether it was dirty.

    Parameters
    ----------
    run_id: Run id.
    """
    from pathlib import Path

    from bathos.git_pin import (
        RUN_REF_PREFIX,
        SNAPSHOT_METADATA_ONLY,
        WIP_REF_PREFIX,
        manifest_entry,
        ref_resolves,
    )

    cwd = Path.cwd()
    entry = manifest_entry(run_id, cwd)
    run_ref = f"{RUN_REF_PREFIX}/{run_id}"
    wip_ref = f"{WIP_REF_PREFIX}/{run_id}"
    run_ref_live = ref_resolves(run_ref, cwd)
    wip_ref_live = ref_resolves(wip_ref, cwd)

    if entry is None and not run_ref_live:
        print(
            f"no provenance record for run {run_id}. Either it predates ref pinning, or it ran "
            "in a different repository (cluster runs pin in the clone they executed on).",
            file=sys.stderr,
        )
        raise SystemExit(1)

    if entry is None:
        print(f"run          {run_id}")
        print(f"run ref      {run_ref}  (resolves)")
        print(
            "\nThe ref is present but no manifest entry was found in this checkout. The run was "
            "most likely pinned in another worktree of this repository."
        )
        return

    print(f"run          {run_id}")
    print(f"branch       {entry.get('branch', '')}")
    print(f"head         {entry.get('head_sha', '')}")
    print(f"pinned       {entry.get('pinned_sha', '')}")
    print(f"dirty        {entry.get('dirty')}")
    print(f"complete     {entry.get('complete')}")
    print(f"recorded_at  {entry.get('recorded_at', '')}")
    print(f"run ref      {run_ref}  ({'resolves' if run_ref_live else 'MISSING'})")
    if entry.get("wip_commit"):
        print(f"wip ref      {wip_ref}  ({'resolves' if wip_ref_live else 'MISSING'})")

    if entry.get("unpinned_reason"):
        print(f"\nPROVENANCE INCOMPLETE: {entry['unpinned_reason']}", file=sys.stderr)
    if entry.get("snapshot_mode") == SNAPSHOT_METADATA_ONLY:
        skipped = entry.get("skipped_paths") or []
        print(
            f"\nThe working tree was too large to snapshot ({entry.get('skipped_bytes', 0):,} "
            "bytes), so its CONTENTS were not captured. Largest contributors:",
            file=sys.stderr,
        )
        for path in skipped[:10]:
            print(f"  {path}", file=sys.stderr)
        print("Consider gitignoring these, then re-running.", file=sys.stderr)
    if entry.get("ignored_declared_paths"):
        print(
            "\nThese declared paths are gitignored and were NOT captured in the snapshot: "
            f"{', '.join(entry['ignored_declared_paths'])}",
            file=sys.stderr,
        )
    if entry.get("wip_commit") and not entry.get("unpinned_reason"):
        print("")
        print(
            "This run executed on a DIRTY tree, so `pinned` is a snapshot of what actually ran, "
            "not `head`. See `bth provenance diff` for the uncommitted delta."
        )


@provenance_app.command(name="diff")
def provenance_diff_cmd(run_id: str, name_only: bool = False) -> None:
    """Show the uncommitted changes that were live when a run executed.

    Answers "what was actually different when this ran?" for the 92% of runs that execute on a
    dirty tree, where the recorded commit alone does not identify the code.

    Parameters
    ----------
    run_id: Run id.
    name_only: List changed paths only.
    """
    from pathlib import Path

    from bathos.git_pin import uncommitted_diff_for_run

    diff = uncommitted_diff_for_run(run_id, Path.cwd(), name_only=name_only)
    if diff is None:
        print(
            f"cannot reconstruct the working state for run {run_id}. It predates ref pinning, ran "
            "in another repository, or its snapshot object is gone.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    if diff == "":
        print(f"run {run_id} executed on a clean tree; the recorded commit is exact.")
        return
    print(diff, end="")


@provenance_app.command(name="import")
def provenance_import_cmd(
    directory: Annotated[
        str, cyclopts.Parameter(name=["--dir"], help="Directory of .bundle files")
    ] = "",
) -> None:
    """Import provenance bundles produced by runs on another machine.

    Cluster runs pin into the cluster's object store, which is not the one results are read from,
    and compute hosts usually have no network path to the shared remote. So a dirty remote run
    exports its snapshot as a bundle under the results directory, which travels back with the
    results; this imports it.
    """
    from pathlib import Path

    from bathos.git_pin import EXPORT_DIRNAME, import_bundles

    cwd = Path.cwd()
    target = Path(directory) if directory else None
    report = import_bundles(cwd, import_dir=target)

    for run_id in report.imported:
        print(f"imported  {run_id}")
    for run_id in report.already_present:
        print(f"present   {run_id}")
    for run_id, reason in report.unusable:
        print(f"UNUSABLE  {run_id}: {reason}", file=sys.stderr)

    if not (report.imported or report.already_present or report.unusable):
        where = target or (cwd / EXPORT_DIRNAME)
        print(f"no provenance bundles found in {where}")
        return

    print(
        f"\n{len(report.imported)} imported, {len(report.already_present)} already present, "
        f"{len(report.unusable)} unusable"
    )
    if report.unusable:
        raise SystemExit(1)


def _build_shadow_hook_script() -> str:
    """Generate the installed post-commit hook's script content.

    The `case` branch below is a CHEAP, coarse pre-filter only, generated from
    the exact same `SHADOW_KEYWORDS` tuple that
    `bathos.blast_radius.identify_fix_like_keyword` uses (confirmed, PR #54
    second jury round). Its only job is avoiding a `bth` process spawn for
    commits obviously irrelevant; `record_shadow_trigger` independently
    re-derives the commit message and makes the real, authoritative keyword
    decision (SAC-5: no assessment ever runs for a genuinely non-matching
    commit, regardless of what this coarser filter let through) and captures
    which keyword matched (SAC-8).
    """
    from bathos.blast_radius import SHADOW_KEYWORDS

    case_pattern = "|".join(f"*{kw}*" for kw in SHADOW_KEYWORDS)
    return f"""#!/bin/sh
# Installed by bathos (backlog #4555) -- do not edit directly, re-run
# `bth blast-radius install-hook` to regenerate.
sha="$(git rev-parse HEAD)"
msg_lower="$(git log -1 --pretty=%B HEAD | tr '[:upper:]' '[:lower:]')"
case "$msg_lower" in
    {case_pattern})
        setsid nohup bth blast-radius shadow-check "$sha" >/dev/null 2>&1 &
        ;;
esac
"""


@app["blast-radius"].command(name="install-hook")
def blast_radius_install_hook_cmd() -> None:
    """Install the post-commit shadow-trigger hook (SAC-1/2, backlog #4555).

    Preserves any pre-existing hooks (chains to post-commit if one existed,
    symlinks every other hook name through unchanged) -- see
    bathos.git_hooks.install_managed_hooks.
    """
    from bathos.git_hooks import install_managed_hooks
    from bathos.workspace import resolve_workspace

    # Built here, not at module scope -- see _build_shadow_hook_script's
    # original placement note in bathos.cli: avoids the duckdb/pyarrow import
    # cost on every `bth` invocation, not just this command.
    shadow_hook_script = _build_shadow_hook_script()

    ws_root = resolve_workspace().fs_root
    managed = ws_root / ".bth" / "hooks"
    install_managed_hooks(ws_root, managed, {"post-commit": shadow_hook_script})
    print(f"Installed shadow-trigger hook at {managed}")


@app["blast-radius"].command(name="uninstall-hook")
def blast_radius_uninstall_hook_cmd() -> None:
    """Uninstall the shadow-trigger hook, restoring the prior core.hooksPath (SAC-3)."""
    from bathos.git_hooks import uninstall_managed_hooks
    from bathos.workspace import resolve_workspace

    ws_root = resolve_workspace().fs_root
    managed = ws_root / ".bth" / "hooks"
    try:
        uninstall_managed_hooks(ws_root, managed)
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1) from None
    print("Uninstalled shadow-trigger hook")


@app["blast-radius"].command(name="shadow-check")
def blast_radius_shadow_check_cmd(commit: str) -> None:
    """Run a shadow-only assessment for one commit (SAC-4/5/6/7, backlog #4555).

    Called by the installed post-commit hook (detached, in the background);
    safe to invoke directly for testing/debugging. Never durably affects a
    real run/campaign/claim's state.

    Parameters
    ----------
    commit: Commit SHA to shadow-assess.
    """
    from bathos.blast_radius import record_shadow_trigger
    from bathos.cli_common import catalog_dir
    from bathos.workspace import resolve_workspace

    ws_root = resolve_workspace().fs_root
    record = record_shadow_trigger(catalog_dir(), ws_root, commit)
    if record is None:
        print(f"No shadow trigger recorded for {commit} (e.g. no parent commit)")
        return
    print(f"Shadow-recorded {commit}: {record.match_reason}")


@app["query"].command(name="shadow-log")
def query_shadow_log_cmd(limit: int = 20) -> None:
    """List recent shadow-trigger firings for calibration review (SAC-8, backlog #4555).

    Parameters
    ----------
    limit: Max records to show.
    """
    import duckdb

    from bathos.cli_common import catalog_dir

    cat_dir = catalog_dir()
    db_path = cat_dir / "bathos.db"
    if not db_path.exists():
        return
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        rows = con.execute(
            "SELECT entity_id, match_reason, amended_at FROM blast_radius_ledger "
            "WHERE entity_type = 'shadow_trigger' ORDER BY amended_at DESC LIMIT ?",
            [limit],
        ).fetchall()
    finally:
        con.close()
    for commit, reason, amended_at in rows:
        print(f"{amended_at}  {commit[:9]}  {reason}")


@app.command
def submit(
    command: list[str],
    preset: str | None = None,
    remote: str | None = None,
    array: str = "",
    dependency: str = "",
    name: str = "",
    sbatch_arg: list[str] | None = None,
    sidecar: str | None = None,
    push_first: bool = True,
    wait: bool = False,
    then_pull: bool = False,
    then_sync: bool = False,
) -> None:
    """Submit a command to the cluster using a configured preset.

    Parameters
    ----------
    command: Command to submit (after --).
    preset: Override preset.
    remote: Override remote.
    array: SLURM array spec e.g. 0-9%4.
    dependency: SLURM dependency e.g. afterok:12345.
    name: Job name (default: first token of command).
    sbatch_arg: Passthrough raw sbatch arg; repeatable.
    sidecar: Explicit path to experiment sidecar (.bth.toml).
    push_first: Push project before submitting.
    wait: Block until job reaches terminal state.
    then_pull: Pull results after job completes (implies --wait).
    then_sync: Run bth sync after pull (implies --then-pull --wait).
    """
    import tomllib
    from pathlib import Path

    from bathos.cli_common import catalog_dir
    from bathos.cluster import (
        job_wait,
        pull_project,
        push_project,
        resolve_cluster_config,
        submit_job,
    )
    from bathos.config import find_project_config, load_project_config
    from bathos.sync import sync_catalog

    sbatch_arg = sbatch_arg or []

    # 1. Validate flag implications
    if then_sync:
        then_pull = True
    if then_pull:
        wait = True

    # 2. Load project config
    cfg_path = find_project_config()
    if cfg_path is None:
        print("No .bth.toml found. Run `bth init` first.", file=sys.stderr)
        raise SystemExit(1)
    config = load_project_config(cfg_path)

    # 3. Load sidecar [cluster] override (optional)
    sidecar_data = None
    sidecar_path = None
    if sidecar:
        try:
            sidecar_path = Path(sidecar)
            with open(sidecar, "rb") as f:
                sidecar_data = tomllib.load(f)
        except (FileNotFoundError, OSError) as e:
            print(f"Failed to parse sidecar: {e}", file=sys.stderr)
            raise SystemExit(1) from None
    else:
        for cmd_token in command:
            if cmd_token.endswith(".py"):
                candidate_py = Path(cmd_token)
                if candidate_py.exists():
                    candidate_sidecar = candidate_py.with_suffix(".bth.toml")
                    if candidate_sidecar.exists():
                        try:
                            sidecar_path = candidate_sidecar
                            with open(candidate_sidecar, "rb") as f:
                                sidecar_data = tomllib.load(f)
                        except (FileNotFoundError, OSError):
                            pass
                break

    # 3a. Check reproduction prerequisite gate (before cluster submission)
    parsed_sidecar = None
    if sidecar_data:
        from bathos.prereg import check_reproduction_prerequisite
        from bathos.sidecar import parse_sidecar

        try:
            parsed_sidecar = parse_sidecar(sidecar_path) if sidecar_path else None

            if parsed_sidecar and parsed_sidecar.reproduction:
                requires_pass_stem = parsed_sidecar.reproduction.requires_pass_stem
                stage_name = parsed_sidecar.stage_name or "exploration"

                if requires_pass_stem and stage_name in ("validation", "production"):
                    found = check_reproduction_prerequisite(requires_pass_stem, catalog_dir())
                    if not found:
                        print(
                            f"REPRODUCTION_PREREQUISITE_UNMET: no passing run of "
                            f"'{requires_pass_stem}' found",
                            file=sys.stderr,
                        )
                        raise SystemExit(1)
                elif requires_pass_stem and stage_name in ("exploration", "calibration"):
                    found = check_reproduction_prerequisite(requires_pass_stem, catalog_dir())
                    if not found:
                        print(
                            f"WARNING: no passing run of '{requires_pass_stem}' found "
                            f"(advisory for {stage_name} stage)",
                            file=sys.stderr,
                        )
        except SystemExit:
            raise
        except Exception as e:
            print(f"Warning: reproduction prerequisite check failed: {e}", file=sys.stderr)

    # 3b. Check parity confound gate (F3 submit-gate, mirrors reproduction gate)
    if parsed_sidecar:
        from bathos.parity import check_parity_confounds_for_submit

        try:
            result = check_parity_confounds_for_submit(parsed_sidecar, catalog_dir())
            stage_name = parsed_sidecar.stage_name or "exploration"

            if result["satisfied"] is False and result["tier_enforced"]:
                print(
                    f"PARITY_PREREQUISITE_UNMET: no passing parity run for "
                    f"'{parsed_sidecar.reproduction.requires_parity_stem}' found",
                    file=sys.stderr,
                )
                raise SystemExit(1)
            elif result["satisfied"] is False and not result["tier_enforced"]:
                print(
                    f"WARNING: no passing parity run for "
                    f"'{parsed_sidecar.reproduction.requires_parity_stem}' found "
                    f"(advisory for {stage_name} stage)",
                    file=sys.stderr,
                )
            elif result["satisfied"] is None:
                print(
                    f"WARNING: could not determine parity prerequisite status for "
                    f"'{parsed_sidecar.reproduction.requires_parity_stem}' (warm DB absent); "
                    "proceeding with caution",
                    file=sys.stderr,
                )
        except SystemExit:
            raise
        except Exception as e:
            print(f"Warning: parity prerequisite check failed: {e}", file=sys.stderr)

    # 3c. Open-obligation warning (D1: warns only, never blocks, at EVERY stage).
    try:
        from bathos.obligations import list_obligations
        from bathos.workspace import resolve_workspace

        open_obs = list_obligations(resolve_workspace().fs_root, open_only=True)
        if open_obs:
            print(
                f"WARNING: {len(open_obs)} open obligation(s) awaiting a post-mortem "
                f"(oldest {max(o.age_days() for o in open_obs):.1f}d). Submitting anyway.",
                file=sys.stderr,
            )
            for ob in open_obs[-3:]:
                print(f"  - {ob.obligation_id} ({ob.trigger})", file=sys.stderr)
    except Exception as e:
        print(f"Warning: obligation check failed: {e}", file=sys.stderr)

    # 4. Resolve cluster config
    try:
        cluster = resolve_cluster_config(
            config,
            sidecar_data=sidecar_data,
            cli_remote=remote,
            cli_preset=preset,
        )
    except ValueError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1) from None

    remote_cfg = config.remotes.get(cluster.remote)
    if remote_cfg and remote_cfg.get("remote_root"):
        from bathos.cluster_catalog import (
            CatalogIdentityError,
            check_env_catalog_matches_remote,
            ensure_remote_catalog_dir,
        )

        try:
            check_env_catalog_matches_remote(cfg_path.parent, remote_cfg["remote_root"])
            ensure_remote_catalog_dir(
                remote_cfg.get("host", cluster.remote), remote_cfg["remote_root"]
            )
        except CatalogIdentityError as e:
            print(str(e), file=sys.stderr)
            raise SystemExit(1) from None
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            raise SystemExit(1) from None

    # 5. Derive job_name
    job_name = name or (command[0].split("/")[-1] if command else "bth-submit")

    # 6. Push if requested
    if push_first:
        try:
            push_project(cluster.remote, cluster.project)
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            raise SystemExit(1) from None

    # 7. Submit
    cmd_str = " ".join(command)
    try:
        result = submit_job(
            cluster.remote,
            cluster.project,
            cluster.preset,
            cmd_str,
            job_name=job_name,
            array=array,
            dependency=dependency,
            sbatch_args=sbatch_arg or None,
        )
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1) from None

    slurm_job_id = result["slurm_job_id"]
    print(f"Submitted {slurm_job_id} on {cluster.remote} using preset {cluster.preset}")

    # 7a. Write submit-provenance record (AC-9 Part 1)
    try:
        import hashlib

        from bathos.catalog import write_submit_provenance

        sidecar_sha256 = ""
        stage_name = "exploration"

        if sidecar_path and sidecar_path.exists():
            sha256_hash = hashlib.sha256()
            with open(sidecar_path, "rb") as f:
                sha256_hash.update(f.read())
            sidecar_sha256 = sha256_hash.hexdigest()

        if sidecar_data:
            experiment_section = sidecar_data.get("experiment", {})
            if isinstance(experiment_section, dict):
                stage_name = experiment_section.get("stage_name", "exploration")

        write_submit_provenance(
            project_slug=cluster.project,
            command=cmd_str,
            sidecar_sha256=sidecar_sha256,
            myxcel_job_id=slurm_job_id,
            stage_name=stage_name,
            catalog_dir=catalog_dir(),
        )
    except Exception as e:
        print(f"Warning: submit-provenance write failed: {e}", file=sys.stderr)

    # 8. Exit if not waiting
    if not wait:
        raise SystemExit(0)

    # 9. Wait for completion
    try:
        wait_result_dict = job_wait(cluster.remote, slurm_job_id)
    except RuntimeError as e:
        print(str(e), file=sys.stderr)
        raise SystemExit(1) from None

    # 10. Pull if requested
    if then_pull:
        try:
            pull_project(cluster.remote, cluster.project)
        except RuntimeError as e:
            print(str(e), file=sys.stderr)
            raise SystemExit(1) from None

    # 11. Sync if requested
    if then_sync:
        try:
            sync_catalog(cluster.remote, config, catalog_dir(), pull=True)
        except (ValueError, RuntimeError) as e:
            print(str(e), file=sys.stderr)
            raise SystemExit(1) from None

    # 12. Handle exit codes
    wait_result = wait_result_dict.get("wait_result", "")
    failure_class = wait_result_dict.get("failure_class", "")

    if wait_result == "timeout":
        print(
            f"Job {slurm_job_id} still running on {cluster.remote}. "
            f"Re-run with --wait --no-push-first to resume polling, or cancel with: "
            f"myxcel cancel-job --remote {cluster.remote} {slurm_job_id}",
            file=sys.stderr,
        )
        raise SystemExit(2)

    if failure_class and failure_class != "SUCCESS":
        raise SystemExit(1)

    raise SystemExit(0)


@app.command
def migrate(dry_run: bool = False, classify: bool = False, project: str | None = None) -> None:
    """Migrate cool-tier Parquet fragments to current schema, optionally classifying scripts.

    Parameters
    ----------
    dry_run: Show what would be migrated without writing.
    classify: Classify flat scripts into subdirs (Phase 2).
    project: Scope migration to a single project slug's runs/<project>/ fragments
        (default: all projects in the catalog).
    """
    from bathos.cli_common import catalog_dir

    if classify:
        from bathos.classifier import apply_classify_plan, build_move_plan, classify_flat_scripts

        project_root = Path.cwd()
        if not (project_root / "scripts").exists():
            print(f"Error: no scripts/ directory found at {project_root}", file=sys.stderr)
            raise SystemExit(1)

        results = classify_flat_scripts(project_root)
        if results:
            plan = build_move_plan(project_root, results)
            try:
                apply_classify_plan(plan, scaffold_sidecars=True)
                print(f"Classified and moved {len(plan.actions)} script(s).")
            except RuntimeError as e:
                print(f"Error: {e}", file=sys.stderr)
                raise SystemExit(1) from None
        else:
            print("No flat scripts found to classify.")
        return

    from bathos.migrate import migrate_catalog

    result = migrate_catalog(catalog_dir(), dry_run=dry_run, project=project)
    scope = f"project {project!r}" if project else "ALL projects"
    print(f"Scanned {result.scanned} fragments ({scope}).")
    print(f"  {result.already_current} already at current schema")
    if result.migrated:
        action = "Would migrate" if dry_run else "Migrated"
        print(f"  {action} {result.migrated} fragment(s).")
    else:
        print("  Nothing to migrate.")
    if result.corrupt:
        print(f"  WARNING: skipped {len(result.corrupt)} corrupt/unreadable fragment(s):", file=sys.stderr)
        for p in result.corrupt:
            print(f"    {p}", file=sys.stderr)
        print("  Run 'bth repair --tier cool' to quarantine them.", file=sys.stderr)


@app.command(name="migrate-to-project-subdirs")
def migrate_to_subdirs_cmd(dry_run: bool = False) -> None:
    """Move flat cool-tier run parquets into per-project subdirectories.

    Reads each run's project_slug and moves it to runs/<slug>/run_<uuid>.parquet.
    Run this on both local and remote before using per-project sync filtering.

    Parameters
    ----------
    dry_run: Show what would be moved without writing.
    """
    from bathos.cli_common import catalog_dir
    from bathos.migrate import migrate_to_project_subdirs

    result = migrate_to_project_subdirs(catalog_dir(), dry_run=dry_run)
    action = "Would move" if dry_run else "Moved"
    print(f"{action} {result.moved} run(s) into per-project subdirectories.")
    if result.skipped:
        print(f"  {result.skipped} already in place (skipped).")
    if result.by_slug:
        for slug, count in sorted(result.by_slug.items()):
            print(f"  {slug}: {count}")


@app.command
def classify(
    min_confidence: str = "low",
    no_scaffold: bool = False,
    apply: bool = False,
    project: str = "",
    json_output: bool = False,
) -> None:
    """Classify flat scripts into the correct scripts/ subdirectory.

    Scans scripts/ root for .py files not already in a subdirectory,
    infers the correct target directory, and prints a git mv plan.
    Apply the plan with --apply.

    Parameters
    ----------
    min_confidence: Only include classifications at or above this level (high|medium|low).
    no_scaffold: Do not scaffold sidecar stubs when applying.
    apply: Execute git mv commands and write sidecars.
    project: Project root (defaults to cwd).
    json_output: Output as JSON (machine-readable).
    """
    import json

    from rich import print as rprint
    from rich.table import Table

    from bathos.classifier import (
        ClassificationConfidence,
        apply_classify_plan,
        build_move_plan,
        classify_flat_scripts,
    )

    if min_confidence.lower() not in ("high", "medium", "low"):
        print(
            f"Error: min-confidence must be high, medium, or low (got {min_confidence!r})",
            file=sys.stderr,
        )
        raise SystemExit(1)

    project_root = (Path(project) if project else Path.cwd()).resolve()
    if not (project_root / "scripts").exists():
        print(f"Error: no scripts/ directory found at {project_root}", file=sys.stderr)
        raise SystemExit(1)

    print(f"Scanning {project_root / 'scripts'} for unclassified files...")

    results = classify_flat_scripts(project_root)

    if not results:
        print("No flat scripts found.")
        raise SystemExit(0)

    plan = build_move_plan(project_root, results)

    min_conf_enum = ClassificationConfidence(min_confidence.lower())
    confidence_order = [
        ClassificationConfidence.HIGH,
        ClassificationConfidence.MEDIUM,
        ClassificationConfidence.LOW,
    ]
    min_conf_idx = confidence_order.index(min_conf_enum)

    filtered_actions = [
        a
        for a in plan.actions
        if confidence_order.index(a.classification.confidence) <= min_conf_idx
    ]

    if not filtered_actions:
        print(f"No classifications found at or above {min_confidence} confidence.")
        raise SystemExit(0)

    if json_output:
        output = {
            "project_root": str(project_root),
            "total_files": len(results),
            "high_confidence": plan.high_confidence,
            "medium_confidence": plan.medium_confidence,
            "low_confidence": plan.low_confidence,
            "conflicts": plan.conflicts,
            "sidecars_to_scaffold": plan.sidecars_to_scaffold,
            "actions": [
                {
                    "source": str(a.source),
                    "destination": str(a.destination),
                    "confidence": a.classification.confidence.value,
                    "rationale": a.classification.rationale,
                    "rename_required": a.classification.rename_required,
                    "suggested_stem": a.classification.suggested_stem,
                    "sidecar_required": a.classification.sidecar_required,
                    "conflict": a.conflict,
                }
                for a in filtered_actions
            ],
        }
        print(json.dumps(output, indent=2))
        raise SystemExit(0)

    table = Table(title="Script Classification Plan")
    table.add_column("Source", style="cyan")
    table.add_column("Target", style="green")
    table.add_column("Confidence", style="yellow")
    table.add_column("Rename", style="magenta")
    table.add_column("Sidecar", style="blue")

    for action in filtered_actions:
        rename_str = "yes" if action.classification.rename_required else "no"
        sidecar_str = "scaffold" if action.classification.sidecar_required else "no"
        table.add_row(
            str(action.source),
            f"scripts/{action.classification.target_dir}/",
            action.classification.confidence.value,
            rename_str,
            sidecar_str,
        )

    rprint(table)
    print()

    summary_parts = [
        f"{len(filtered_actions)} script(s)",
        f"{plan.high_confidence} HIGH",
        f"{plan.medium_confidence} MEDIUM",
        f"{plan.low_confidence} LOW",
    ]
    if plan.conflicts:
        summary_parts.append(f"{plan.conflicts} conflict(s)")
    if plan.sidecars_to_scaffold:
        summary_parts.append(f"{plan.sidecars_to_scaffold} sidecar(s) to scaffold")

    print(" | ".join(summary_parts))

    if apply:
        if plan.conflicts:
            print(
                f"Error: {plan.conflicts} conflict(s) detected. Resolve them manually before "
                "retrying.",
                file=sys.stderr,
            )
            raise SystemExit(1)

        scaffold = not no_scaffold
        try:
            apply_classify_plan(plan, scaffold_sidecars=scaffold)
            print(f"Applied: moved {len(filtered_actions)} script(s).")
            if scaffold:
                print(f"Scaffolded: {plan.sidecars_to_scaffold} sidecar(s).")
        except RuntimeError as e:
            print(f"Error: {e}", file=sys.stderr)
            raise SystemExit(1) from None
    else:
        print("Run with --apply to execute.")


@app.command(name="sprint-audit")
def sprint_audit_cmd(hours: int = 24) -> None:
    """Audit recent runs and campaigns across all registered projects.

    Parameters
    ----------
    hours: Lookback window in hours.
    """
    from bathos.sprint_audit import sprint_audit

    result = sprint_audit(hours)
    if result["warnings"]:
        print("Warnings:")
        for w in result["warnings"]:
            print(f"  WARNING: {w}")
        print()
    if not result["audit_results"]:
        print("No projects found. Run 'bth init' in each project first.")
        return
    for slug, data in result["audit_results"].items():
        print(f"{slug}: {data['runs']} runs, {data['campaigns']} campaigns")
        for anomaly in data["anomalies"]:
            print(f"  WARNING: {anomaly}")


@app.command(name="export")
def export_cmd(
    tool: str = "claude",
    level: str = "user",
    dry_run: bool = False,
    html: bool = False,
    out: str = "report.html",
    project: str | None = None,
    campaign: str | None = None,
    surface: str | None = None,
    plugin_out: str = ".claude-plugin-dist",
) -> None:
    """Export the using-bathos skill and register MCP server, or export catalog as HTML.

    Parameters
    ----------
    tool: Target tool: claude or gemini.
    level: Install level: user, workspace, or system.
    dry_run: Print what would happen without writing.
    html: Export catalog as a self-contained HTML report.
    out: Output file for --html export.
    project: Filter by project (--html only).
    campaign: Filter by campaign (--html only).
    surface: Plugin surface: claude, cursor, copilot, or antigravity.
    plugin_out: Output directory for the plugin bundle (--surface only).
    """
    from bathos.cli_common import catalog_dir

    if surface:
        from bathos.plugin_export import PluginExportError, export_plugin_bundle

        resolved_surface = "claude" if surface == "claude_code" else surface

        try:
            result = export_plugin_bundle(
                surface=resolved_surface, out=Path(plugin_out), dry_run=dry_run
            )
        except PluginExportError as e:
            print(f"Error: {e}", file=sys.stderr)
            raise SystemExit(1) from None

        if dry_run:
            print(result.stdout, end="")
        else:
            print(f"Exported {result.surface} plugin bundle to {result.out}")
        raise SystemExit(0)

    if html:
        try:
            from bathos.viz.html import export_html as do_export
        except ImportError:
            print(
                "Error: bathos[viz] is not installed.\nInstall with: uv tool install 'bathos[viz]'",
                file=sys.stderr,
            )
            raise SystemExit(1)

        from bathos.query import list_runs

        catalog = catalog_dir()
        runs = list_runs(catalog, project=project)
        if campaign:
            runs = [r for r in runs if r.campaign_id == campaign]

        if not runs:
            print(f"No matching runs. Writing empty report to {out}.", file=sys.stderr)

        path, size_warned = do_export(runs, output_path=out, catalog_dir=catalog)
        print(f"Exported to {path}")
        if size_warned:
            print("(Use --project or --campaign to reduce file size)", file=sys.stderr)
        return

    from bathos.export import ExportError, export_skill, register_mcp, resolve_target

    try:
        target = resolve_target(tool=tool, level=level)
    except ExportError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1) from None

    result = export_skill(target=target, dry_run=dry_run)
    mcp_target = register_mcp(tool=tool, level=level, dry_run=dry_run)

    if dry_run:
        print(f"Dry run — would write skill to:  {result.target}")
        print(f"Dry run — would register MCP at: {mcp_target}")
    else:
        print(f"Exported skill to:    {result.target}")
        print(f"Registered MCP at:   {mcp_target}")


@app.command
def view(
    port: int = 8080,
    host: str = "127.0.0.1",
    no_open: bool = False,
    project: str | None = None,
) -> None:
    """Launch a local FastAPI dashboard to visualize runs and campaigns.

    Parameters
    ----------
    port: Port to bind to.
    host: Host to bind to.
    no_open: Do not open browser automatically.
    project: Scope to single project.
    """
    try:
        from bathos.viz.server import run_server
    except ImportError:
        print(
            "Error: bathos[viz] is not installed.\nInstall with: uv tool install 'bathos[viz]'",
            file=sys.stderr,
        )
        raise SystemExit(1)

    from bathos.cli_common import catalog_dir
    from bathos.query import list_runs

    catalog = catalog_dir()
    runs = list_runs(catalog, project=project, limit=1001)
    total_run_count = len(runs)
    runs = runs[:1000]

    try:
        run_server(
            runs, total_run_count=total_run_count, host=host, port=port, open_browser=not no_open
        )
    except OSError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1) from None


@app.command(name="catalog-version")
def catalog_version_cmd() -> None:
    """Show schema version status of the catalog."""
    from bathos.cli_common import catalog_dir as _catalog_dir_fn
    from bathos.migrate import migrate_catalog
    from bathos.schema import CURRENT_SCHEMA_VERSION

    cat_dir = _catalog_dir_fn()
    print(f"Current schema version: {CURRENT_SCHEMA_VERSION}")

    result = migrate_catalog(cat_dir, dry_run=True)
    print(f"Cool-tier fragments: {result.scanned} scanned, {result.migrated} need migration.")


if __name__ == "__main__":
    app()
