from __future__ import annotations

import dataclasses
import json
import os
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path

import typer

from bathos.catalog import write_run
from bathos.git import capture_git_state
from bathos.schema import Run
from bathos.sidecar import find_sidecar, is_in_enforced_dir, parse_sidecar, evaluate_outcome, SidecarError
from bathos.prereg import resolve_sidecar, resolve_agent_mode, gate_check


def _find_script_path(argv: list[str], cwd: Path) -> Path | None:
    """Extract script path from argv. Returns None if not a file-based script."""
    if not argv:
        return None

    # If first arg is not python/python3, assume it's the script
    first = argv[0].lower()
    if not any(first.endswith(exe) for exe in ("python", "python3")):
        candidate = cwd / argv[0] if not Path(argv[0]).is_absolute() else Path(argv[0])
        if candidate.exists() and candidate.suffix == ".py":
            return candidate.resolve()
        return None

    # First arg is python; look for script file in subsequent args
    # Handle: python script.py, python -c "...", python -m module, etc.
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in ("-c", "-m", "-W"):
            # These take an argument but don't point to a file
            i += 2
        elif arg.startswith("-"):
            # Other flags
            i += 1
        else:
            # First non-flag arg after python is the script
            candidate = cwd / arg if not Path(arg).is_absolute() else Path(arg)
            if candidate.exists() and candidate.suffix == ".py":
                return candidate.resolve()
            return None
        i += 1
    return None


def _read_result_emission(env_var_path: Path, script_path: Path | None) -> str:
    """
    Read result emission from either:
    1. env_var_path (set by BTH_RESULTS_PATH env var)
    2. <script_stem>.bth-results.json adjacent to script (fallback)

    Returns JSON string, or "{}" if neither exists or if JSON is invalid.
    """
    # Try env var path first
    if env_var_path.exists():
        try:
            content = env_var_path.read_text()
            # Validate it's valid JSON
            json.loads(content)
            return content
        except (json.JSONDecodeError, OSError):
            return "{}"

    # Try fallback path adjacent to script
    if script_path is not None:
        fallback_path = script_path.parent / f"{script_path.stem}.bth-results.json"
        if fallback_path.exists():
            try:
                content = fallback_path.read_text()
                # Validate it's valid JSON
                json.loads(content)
                return content
            except (json.JSONDecodeError, OSError):
                return "{}"

    return "{}"


def run_script(
    argv: list[str],
    project_slug: str,
    catalog_dir: Path,
    output_paths: list[str],
    tags: list[str],
    cwd: Path = Path.cwd(),
    agent_mode: str | None = None,
    no_sidecar: bool = False,
    derived_from: str | None = None,
    campaign_id: str | None = None,
) -> int:
    script_path = _find_script_path(argv, cwd)

    # Calculate script SHA-256 at runtime
    script_sha256_val = ""
    if script_path is not None and script_path.exists():
        try:
            import hashlib
            h = hashlib.sha256()
            with open(script_path, "rb") as f:
                while chunk := f.read(8192):
                    h.update(chunk)
            script_sha256_val = h.hexdigest()
        except Exception:
            pass

    # Sidecar resolution
    bundle = None
    sidecar = None
    if script_path is not None:
        bundle = resolve_sidecar(script_path)
        if bundle.found:
            try:
                sidecar = parse_sidecar(bundle.path)
            except SidecarError as e:
                typer.echo(f"Error: invalid sidecar — {e}", err=True)
                return 1

    # Resolve agent mode
    sidecar_agent_mode = sidecar.agent_mode if sidecar else ""

    # Read project config for agent_mode default
    project_config_mode = ""
    try:
        bth_config_path = cwd / ".bth.toml"
        if bth_config_path.exists():
            project_config = tomllib.loads(bth_config_path.read_text()).get("defaults", {})
            project_config_mode = project_config.get("agent_mode", "")
    except Exception:
        pass

    global_config_mode = ""
    try:
        global_config_path = Path.home() / ".bth" / "config.toml"
        if global_config_path.exists():
            global_config = tomllib.loads(global_config_path.read_text()).get("defaults", {})
            global_config_mode = global_config.get("agent_mode", "")
    except Exception:
        pass

    resolved_mode = resolve_agent_mode(
        cli_flag=agent_mode,
        sidecar=sidecar,
        project_config={"defaults": {"agent_mode": project_config_mode}} if project_config_mode else None,
        global_config={"defaults": {"agent_mode": global_config_mode}} if global_config_mode else None,
    )

    git = capture_git_state(cwd)

    # Run gate check for enforced dirs
    if script_path is not None and is_in_enforced_dir(script_path) and not no_sidecar:
        gate_result = gate_check(
            script_path=script_path,
            bundle=bundle,
            mode=resolved_mode,
            catalog_dir=catalog_dir,
            git_hash=git.hash,
        )
        if not gate_result.ok:
            typer.echo(json.dumps(gate_result.error_payload), err=True)
            return 1

    # Determine sidecar_mode string
    if no_sidecar:
        sidecar_mode_str = "bypassed"
    elif bundle and bundle.found:
        sidecar_mode_str = "declared"
    else:
        sidecar_mode_str = ""
    run = Run(
        project_slug=project_slug,
        command=" ".join(argv),
        argv=argv,
        git_hash=git.hash,
        git_branch=git.branch,
        git_dirty=git.dirty,
        output_paths=output_paths,
        tags=tags,
        status="running",
        slurm_job_id=os.environ.get("SLURM_JOB_ID", ""),
        sidecar_sha256=bundle.sha256 if bundle and bundle.found else "",
        sidecar_path=str(bundle.path) if bundle and bundle.path else "",
        parent_run_id=derived_from or "",
        agent_mode=resolved_mode,
        sidecar_mode=sidecar_mode_str,
        campaign_id=campaign_id or "",
        script_sha256=script_sha256_val,
    )
    catalog_dir.mkdir(parents=True, exist_ok=True)
    write_run(run, catalog_dir)

    # Create temporary results file path for subprocess to write to
    results_temp_dir = Path(tempfile.gettempdir())
    results_temp_path = results_temp_dir / f"{run.id}.bth-results.json"

    # Set up environment with results path
    env = os.environ.copy()
    env["BTH_RESULTS_PATH"] = str(results_temp_path)

    start = time.monotonic()
    try:
        result = subprocess.run(argv, cwd=cwd, env=env)
        exit_code = result.returncode
        status = "completed" if exit_code == 0 else "failed"
    except KeyboardInterrupt:
        exit_code = 130
        status = "killed"

    # Read result emission from BTH_RESULTS_PATH or fallback path
    metadata = _read_result_emission(results_temp_path, script_path)

    outcome = ""
    if sidecar is not None:
        # Outcome evaluation: read result_schema fields from metadata
        try:
            meta = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            meta = {}
        outcome = evaluate_outcome(sidecar, meta)

    # Populate outcome_is_residual flag
    outcome_is_residual = False
    if sidecar and outcome and outcome not in ("unknown", ""):
        spec = sidecar.outcomes.get(outcome)
        if spec:
            outcome_is_residual = getattr(spec, "is_residual", False)

    run = dataclasses.replace(
        run,
        duration_s=time.monotonic() - start,
        exit_code=exit_code,
        status=status,
        metadata=metadata,
        outcome=outcome,
        outcome_is_residual=outcome_is_residual,
    )
    write_run(run, catalog_dir)

    # Clean up temp results file if it exists
    if results_temp_path.exists():
        try:
            results_temp_path.unlink()
        except OSError:
            pass  # Silent fail on cleanup

    return exit_code
