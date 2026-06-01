from __future__ import annotations

import dataclasses
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import tomllib
from pathlib import Path

import typer

from bathos.catalog import write_run
from bathos.git import capture_git_state
from bathos.schema import Run
from bathos.sidecar import find_sidecar, is_in_enforced_dir, parse_sidecar, evaluate_outcome, SidecarError
from bathos.prereg import resolve_sidecar, resolve_agent_mode, gate_check, GateErrorCode, _gate_failure_payload
from bathos.telemetry import init_telemetry, event, run_uuid_var

logger = logging.getLogger(__name__)


def _find_script_path(argv: list[str], cwd: Path) -> Path | None:
    """Extract script path from argv. Returns None if not a file-based script."""
    if not argv:
        return None

    # If first arg is not python/python3, assume it's the script
    first = argv[0].lower()
    if not any(first.endswith(exe) for exe in ("python", "python3", "uv")):
        candidate = cwd / argv[0] if not Path(argv[0]).is_absolute() else Path(argv[0])
        if candidate.exists() and candidate.suffix == ".py":
            return candidate.resolve()
        return None

    # First arg is python/uv; look for script file in subsequent args
    # Handle: python script.py, python -c "...", python -m module, etc.
    # Also handle: uv run python script.py (skip 'run' and 'python' tokens)
    _UV_PASSTHROUGH = {"run", "python", "python3"}
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg in _UV_PASSTHROUGH:
            i += 1
            continue
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


def _write_manifest(
    run: Run,
    sidecar_path: Path | None,
    sidecar_sha256: str,
    catalog_dir: Path,
) -> tuple[str, str]:
    """Write pre-execution manifest file and return (manifest_sha256, manifest_path).

    Manifest is written adjacent to the sidecar (same directory).
    Format: <script_stem>.<run_id>.bth.lock.toml

    Returns:
        Tuple of (manifest_sha256 hex string, manifest_path absolute string)

    Raises:
        RuntimeError if write fails in --agent-mode; logs warning otherwise.
    """
    import hashlib
    from datetime import UTC, datetime

    if sidecar_path is None:
        return "", ""

    manifest_filename = f"{sidecar_path.stem}.{run.id}.bth.lock.toml"
    manifest_path = sidecar_path.parent / manifest_filename

    manifest_content = (
        f"# {sidecar_path.stem}.{run.id}.bth.lock.toml — written at run time, never modified\n"
        f"[manifest]\n"
        f'written_at = "{datetime.now(UTC).isoformat()}"\n'
        f'sidecar_sha256 = "{sidecar_sha256}"\n'
        f'sidecar_path = "{str(sidecar_path.resolve())}"\n'
        f'git_sha = "{run.git_hash}"\n'
        f'script_sha256 = "{run.script_sha256}"\n'
        f'run_id = "{run.id}"\n'
        f'agent_id = null\n'
    )

    try:
        manifest_path.write_text(manifest_content)
        manifest_sha = hashlib.sha256(manifest_content.encode()).hexdigest()
        return manifest_sha, str(manifest_path.resolve())
    except Exception as e:
        if run.agent_mode == "autonomous":
            raise RuntimeError(f"Failed to write manifest: {e}") from e
        logger.warning(f"Failed to write manifest {manifest_path}: {e}")
        return "", ""


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
    init_telemetry()
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
        except Exception as e:
            logger.warning(f"Failed to compute script SHA-256 for {script_path}: {e}")

    # Sidecar resolution
    bundle = None
    sidecar = None
    if script_path is not None:
        bundle = resolve_sidecar(script_path)
        if bundle.found:
            try:
                sidecar = parse_sidecar(bundle.path)
            except SidecarError as e:
                event("run.error", phase="validate", exc_type=type(e).__name__, exc_msg=str(e))
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
            # Serialize error payload to dict for JSON output
            payload_dict = dataclasses.asdict(gate_result.error_payload) if gate_result.error_payload else {}
            typer.echo(json.dumps(payload_dict), err=True)
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
        slurm_array_task_id=os.environ.get("SLURM_ARRAY_TASK_ID", ""),
        sidecar_sha256=bundle.sha256 if bundle and bundle.found else "",
        sidecar_path=str(bundle.path) if bundle and bundle.path else "",
        parent_run_id=derived_from or "",
        agent_mode=resolved_mode,
        sidecar_mode=sidecar_mode_str,
        campaign_id=campaign_id or "",
        script_sha256=script_sha256_val,
    )
    run_uuid_var.set(run.id)
    event("run.start", run_uuid=run.id, script_path=str(script_path) if script_path else "", script_sha256=script_sha256_val, argv=argv, cwd=str(cwd), campaign_id=campaign_id or "", agent_mode=resolved_mode)

    # Lineage: resolve derived_from to parent run_uuid if provided
    if derived_from:
        try:
            from bathos.query import get_run
            parent_run = get_run(catalog_dir, derived_from)
            if parent_run:
                event("lineage.resolved", child_run_uuid=run.id, parent_run_uuid=parent_run.id)
            else:
                event("lineage.resolve_error", child_run_uuid=run.id, derived_from=derived_from, reason="parent run not found")
        except Exception as e:
            event("lineage.resolve_error", child_run_uuid=run.id, derived_from=derived_from, reason=str(e))

    catalog_dir.mkdir(parents=True, exist_ok=True)
    try:
        write_run(run, catalog_dir)
    except Exception as e:
        event("run.error", phase="persist", exc_type=type(e).__name__, exc_msg=str(e))
        raise

    # Write pre-execution manifest (before subprocess)
    if bundle and bundle.found:
        try:
            manifest_sha256, manifest_path = _write_manifest(
                run, bundle.path, bundle.sha256, catalog_dir
            )
            # Update run object with manifest info
            run.manifest_sha256 = manifest_sha256
            run.manifest_path = manifest_path
        except RuntimeError as e:
            # In autonomous mode, manifest write failure is fatal
            event("run.error", phase="manifest", exc_type=type(e).__name__, exc_msg=str(e))
            raise

    # Create temporary results file path for subprocess to write to
    results_temp_dir = Path(tempfile.gettempdir())
    results_temp_path = results_temp_dir / f"{run.id}.bth-results.json"

    # Set up environment with results path
    env = os.environ.copy()
    env["BTH_RESULTS_PATH"] = str(results_temp_path)

    start = time.monotonic()
    exit_code = 1
    status = "failed"
    heartbeat_stop = None
    try:
        proc = subprocess.Popen(argv, cwd=cwd, env=env)
        event("run.subprocess_spawn", pid=proc.pid, cmd=argv)

        # Heartbeat thread: emit every 60s after initial 60s wall-clock
        heartbeat_stop = threading.Event()
        def emit_heartbeat():
            wall_start = time.time()
            while not heartbeat_stop.is_set():
                elapsed_wall = time.time() - wall_start
                if elapsed_wall > 60:
                    elapsed_ms = int((time.monotonic() - start) * 1000)
                    event("run.heartbeat", pid=proc.pid, elapsed_ms=elapsed_ms)
                heartbeat_stop.wait(60)
        heartbeat_thread = threading.Thread(target=emit_heartbeat, daemon=True)
        heartbeat_thread.start()

        exit_code = proc.wait()
        status = "completed" if exit_code == 0 else "failed"
    except KeyboardInterrupt:
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (AttributeError, subprocess.TimeoutExpired):
            pass
        exit_code = 130
        status = "killed"
    except Exception as e:
        event("run.error", phase="spawn", exc_type=type(e).__name__, exc_msg=str(e))
        return 1
    finally:
        if heartbeat_stop:
            heartbeat_stop.set()

    duration_ms = int((time.monotonic() - start) * 1000)
    event("run.subprocess_exit", exit_code=exit_code, duration_ms=duration_ms)

    # Read result emission from BTH_RESULTS_PATH or fallback path
    metadata = _read_result_emission(results_temp_path, script_path)

    outcome = ""
    outcome_error_reason = ""

    # Exit code guard: if exit_code != 0, outcome is "error"
    if exit_code != 0:
        outcome = "error"
        outcome_error_reason = f"exit_code={exit_code}"
    elif sidecar is not None:
        # Outcome evaluation: read result_schema fields from metadata
        try:
            meta = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            meta = {}
        try:
            outcome = evaluate_outcome(sidecar, meta)
        except SidecarError as e:
            outcome = "error"
            payload = _gate_failure_payload(
                error_code=GateErrorCode.OUTCOME_EVALUATION_ERROR,
                phase="post_execution",
                errors=[str(e)],
                agent_mode=resolved_mode,
            )
            outcome_error_reason = json.dumps(dataclasses.asdict(payload))
            event("run.error", phase="evaluate", exc_type=type(e).__name__, exc_msg=str(e))
        except Exception as e:
            event("run.error", phase="evaluate", exc_type=type(e).__name__, exc_msg=str(e))
            raise

    # Populate outcome_is_residual flag
    outcome_is_residual = False
    if sidecar and outcome and outcome not in ("unknown", "", "error"):
        spec = sidecar.outcomes.get(outcome)
        if spec:
            outcome_is_residual = getattr(spec, "is_residual", False)

    # Populate adversarial_check_status
    adversarial_check_status = ""
    if sidecar is None:
        adversarial_check_status = "n/a"
    elif any(
        getattr(outcome_spec, "adversarial_check", None) is not None
        for outcome_spec in sidecar.outcomes.values()
    ):
        adversarial_check_status = "present"
    else:
        adversarial_check_status = "missing"

    run = dataclasses.replace(
        run,
        duration_s=time.monotonic() - start,
        exit_code=exit_code,
        status=status,
        metadata=metadata,
        outcome=outcome,
        outcome_error_reason=outcome_error_reason,
        outcome_is_residual=outcome_is_residual,
        adversarial_check_status=adversarial_check_status,
    )

    # Record parquet write with telemetry
    parquet_start = time.monotonic()
    try:
        write_run(run, catalog_dir)
    except Exception as e:
        event("run.error", phase="persist", exc_type=type(e).__name__, exc_msg=str(e))
        raise
    parquet_duration_ms = int((time.monotonic() - parquet_start) * 1000)
    parquet_path = catalog_dir / "runs" / run.project_slug / f"run_{run.id}.parquet"
    parquet_bytes = parquet_path.stat().st_size if parquet_path.exists() else 0
    event("run.parquet_written", path=str(parquet_path), bytes=parquet_bytes, duration_ms=parquet_duration_ms)

    # Clean up temp results file if it exists
    if results_temp_path.exists():
        try:
            results_temp_path.unlink()
        except OSError:
            pass  # Silent fail on cleanup

    return exit_code
