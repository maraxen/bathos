from __future__ import annotations

import dataclasses
import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from contextlib import suppress
from pathlib import Path

from bathos.catalog import write_run
from bathos.checker import hash_dependency_lock
from bathos.git import capture_git_state
from bathos.git_pin import pin_result_as_dict, pin_run
from bathos.prereg import (
    GateErrorCode,
    _gate_failure_payload,
    gate_check,
    resolve_agent_mode,
    resolve_sidecar,
)
from bathos.schema import Run
from bathos.sidecar import (
    DifferentialBlock,
    SidecarError,
    evaluate_outcome,
    is_in_enforced_dir,
    parse_sidecar,
)
from bathos.telemetry import event, init_telemetry, run_uuid_var

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


def _read_result_emission(
    env_var_path: Path,
    script_path: Path | None,
    output_paths: list[str] | None = None,
) -> str:
    """
    Read result emission from, in order:
    1. env_var_path (set by BTH_RESULTS_PATH env var)
    2. <script_stem>.bth-results.json adjacent to script (fallback)
    3. a single registered --out JSON path (fallback for scripts that only write
       their result to --out and never to $BTH_RESULTS_PATH — this was the root
       cause of outcome staying 'unknown' for otherwise-passing runs; debt #485/#487/#369)

    Returns JSON string, or "{}" if none exist or none parse as valid JSON.
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

    # Try a single registered --out JSON path. Only applied when exactly one
    # candidate exists — with zero or multiple, guessing which one is "the"
    # result file would be more likely to mislead than help.
    if output_paths:
        json_outs = [p for p in output_paths if p.endswith(".json")]
        if len(json_outs) == 1:
            out_path = Path(json_outs[0])
            if out_path.exists():
                try:
                    content = out_path.read_text()
                    json.loads(content)
                    return content
                except (json.JSONDecodeError, OSError):
                    return "{}"

    return "{}"


def _write_manifest(
    run: Run,
    sidecar_path: Path | None,
    sidecar_sha256: str,
    catalog_dir: Path,  # noqa: ARG001 - kept for manifest helper interface
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
        f"agent_id = null\n"
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


@dataclasses.dataclass
class DifferentialResult:
    """Outcome of the [differential] instrument-sensitivity pre-flight (debt #1071)."""

    ok: bool
    off_metadata: str
    on_metadata: str
    effect: float | None
    reason: str


def _run_differential_preflight(
    differential: DifferentialBlock,
    argv: list[str],
    cwd: Path,
    base_env: dict,
    results_temp_dir: Path,
    run_id: str,
) -> DifferentialResult:
    """Run `argv` once with the [differential] knob set to `off`, once to `on`, and assert
    the results `expect` (differ / are identical).

    Neither sub-execution becomes a catalogued Run row -- they are plumbing to prove the
    measurement can detect a real effect, not scientific results in their own right, and
    would otherwise pollute `bth ls`/first-of-kind/residual-rate logic that assumes every
    catalog Run is a real experiment attempt.
    """

    def _run_phase(value: str, phase: str, results_path: Path, output_dir: Path) -> dict:
        env = base_env.copy()
        env["BTH_RESULTS_PATH"] = str(results_path)
        output_dir.mkdir(parents=True, exist_ok=True)
        env["BTH_OUTPUT_DIR"] = str(output_dir)
        env["BTH_DIFFERENTIAL_KNOB"] = differential.knob
        env["BTH_DIFFERENTIAL_VALUE"] = value
        env["BTH_DIFFERENTIAL_PHASE"] = phase
        proc = subprocess.Popen(argv, cwd=cwd, env=env)
        exit_code = proc.wait()
        raw = _read_result_emission(results_path, None, None)
        try:
            meta = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            meta = {}
        results_path.unlink(missing_ok=True)
        return {"exit_code": exit_code, "metadata": meta, "raw": raw}

    off_path = results_temp_dir / f"{run_id}.differential-off.bth-results.json"
    on_path = results_temp_dir / f"{run_id}.differential-on.bth-results.json"
    off_output_dir = results_temp_dir / f"{run_id}.differential-off-outputs"
    on_output_dir = results_temp_dir / f"{run_id}.differential-on-outputs"

    off_result = _run_phase(differential.off, "off", off_path, off_output_dir)
    on_result = _run_phase(differential.on, "on", on_path, on_output_dir)

    effect: float | None = None
    if differential.metric:
        off_val = off_result["metadata"].get(differential.metric)
        on_val = on_result["metadata"].get(differential.metric)
        is_numeric = (
            isinstance(off_val, (int, float))
            and not isinstance(off_val, bool)
            and isinstance(on_val, (int, float))
            and not isinstance(on_val, bool)
        )
        if is_numeric:
            effect = abs(float(on_val) - float(off_val))
            differs = effect >= (differential.min_effect or 0.0)
        else:
            differs = off_val != on_val
    else:
        differs = json.dumps(off_result["metadata"], sort_keys=True) != json.dumps(
            on_result["metadata"], sort_keys=True
        )

    ok = differs if differential.expect == "differs" else not differs

    reason = ""
    if not ok:
        reason = (
            f"[differential] knob={differential.knob!r} expect={differential.expect!r} "
            f"off={differential.off!r} on={differential.on!r}"
        )
        if differential.metric:
            reason += (
                f" metric={differential.metric!r} effect={effect!r} "
                f"min_effect={differential.min_effect!r}"
            )
        reason += f" — invariant did not fire (differs={differs})"

    return DifferentialResult(
        ok=ok,
        off_metadata=off_result["raw"],
        on_metadata=on_result["raw"],
        effect=effect,
        reason=reason,
    )


def _is_ephemeral_path(path: str) -> bool:
    """Return True if path resolves under a system temp directory."""
    p = Path(path)
    temp_root = Path(tempfile.gettempdir()).resolve()
    try:
        p.resolve().relative_to(temp_root)
        return True
    except ValueError:
        pass
    for root in (Path("/tmp"), Path("/var/tmp")):
        if root.exists():
            try:
                p.resolve().relative_to(root.resolve())
                return True
            except ValueError:
                pass
    return False


def run_script(
    argv: list[str],
    project_slug: str,
    catalog_dir: Path,
    output_paths: list[str],
    tags: list[str],
    cwd: Path = Path.cwd(),
    agent_mode: str | None = None,
    no_sidecar: bool = False,
    allow_stale: bool = False,
    derived_from: str | None = None,
    campaign_id: str | None = None,
    component_id: str | None = None,
    component_sidecar_sha256: str | None = None,
) -> int:
    init_telemetry()

    # Resolve --campaign up front (fail fast, before running the subprocess) and
    # store the full UUID — never the raw prefix — on the run record.
    resolved_campaign_id: str | None = None
    if campaign_id:
        import duckdb

        from bathos.campaigns import CampaignError, _resolve_campaign_id

        db_path = catalog_dir / "bathos.db"
        campaign_db = None
        try:
            if db_path.exists():
                # read_only: SLURM array tasks otherwise take exclusive locks on the
                # same bathos.db and fail with "Conflicting lock is held".
                campaign_db = duckdb.connect(str(db_path), read_only=True)
            resolved_campaign_id = _resolve_campaign_id(
                campaign_db, campaign_id, catalog_dir=catalog_dir
            )
        except CampaignError as e:
            print(f"Error: {e}", file=sys.stderr)
            cool_dir = catalog_dir / "campaigns"
            has_cool = cool_dir.exists() and any(cool_dir.glob("*.json"))
            if not db_path.exists() and not has_cool:
                print("(run `bth campaign create` first)", file=sys.stderr)
            return 1
        finally:
            if campaign_db is not None:
                campaign_db.close()

    # Warn if any registered output path is ephemeral
    ephemeral_outs = [p for p in output_paths if _is_ephemeral_path(p)]
    if ephemeral_outs:
        for ep in ephemeral_outs:
            print(
                f"Warning: --out {ep!r} is in a temp directory and will be lost on reboot. "
                "Use a persistent project path (e.g. outputs/) instead.",
                file=sys.stderr,
            )

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
                print(f"Error: invalid sidecar — {e}", file=sys.stderr)
                return 1

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
        project_config={"defaults": {"agent_mode": project_config_mode}}
        if project_config_mode
        else None,
        global_config={"defaults": {"agent_mode": global_config_mode}}
        if global_config_mode
        else None,
    )

    git = capture_git_state(cwd)

    # Dependency-lock provenance (debt #1071): captured unconditionally on every run, not
    # just differential ones -- the incident this guards against (a package re-pin silently
    # invalidating every prior differential/SC result) is a general provenance gap.
    dependency_lock_sha256 = hash_dependency_lock(cwd)

    # Run gate check for enforced dirs
    if script_path is not None and is_in_enforced_dir(script_path) and not no_sidecar:
        gate_result = gate_check(
            script_path=script_path,
            bundle=bundle,
            mode=resolved_mode,
            catalog_dir=catalog_dir,
            git_hash=git.hash,
            allow_stale=allow_stale,
        )
        if not gate_result.ok:
            # Serialize error payload to dict for JSON output
            payload_dict = (
                dataclasses.asdict(gate_result.error_payload) if gate_result.error_payload else {}
            )
            print(json.dumps(payload_dict), file=sys.stderr)
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
        campaign_id=resolved_campaign_id or "",
        script_sha256=script_sha256_val,
        stage_name=sidecar.stage_name if sidecar else None,
        # (#3717) Propagate claim-tier discriminability from the sidecar. The warm columns and
        # the cool->warm compact path already existed (#2276), but nothing ever populated them
        # AT THE ORIGIN, so runs.claim_discriminates was NULL for all 1418 runs in the catalog
        # and the Union Gate -- which maps a run onto a claim clause via this field -- found
        # every clause unmapped and downgraded every confirmation campaign to `confounded`
        # regardless of design. Stored as a JSON array string to match the warm schema.
        claim_discriminates=(
            json.dumps(sidecar.claim_discriminates)
            if sidecar and sidecar.claim_discriminates
            else None
        ),
        claim_isolates=(
            json.dumps(sidecar.claim_isolates) if sidecar and sidecar.claim_isolates else None
        ),
        component_id=component_id,
        component_sidecar_sha256=component_sidecar_sha256,
        dependency_lock_sha256=dependency_lock_sha256,
        git_dirty_content_id=git.dirty_content_id,
        git_provenance_source=git.provenance_source,
    )
    run_uuid_var.set(run.id)
    event(
        "run.start",
        run_uuid=run.id,
        script_path=str(script_path) if script_path else "",
        script_sha256=script_sha256_val,
        argv=argv,
        cwd=str(cwd),
        campaign_id=resolved_campaign_id or "",
        agent_mode=resolved_mode,
    )

    # Lineage: resolve derived_from to parent run_uuid if provided
    if derived_from:
        try:
            from bathos.query import get_run

            parent_run = get_run(catalog_dir, derived_from)
            if parent_run:
                event("lineage.resolved", child_run_uuid=run.id, parent_run_uuid=parent_run.id)
            else:
                event(
                    "lineage.resolve_error",
                    child_run_uuid=run.id,
                    derived_from=derived_from,
                    reason="parent run not found",
                )
        except Exception as e:
            event(
                "lineage.resolve_error",
                child_run_uuid=run.id,
                derived_from=derived_from,
                reason=str(e),
            )

    catalog_dir.mkdir(parents=True, exist_ok=True)
    try:
        write_run(run, catalog_dir)
    except Exception as e:
        event("run.error", phase="persist", exc_type=type(e).__name__, exc_msg=str(e))
        raise

    # Make the git provenance DURABLE, not merely recorded. Capturing `git_hash` is already
    # reliable; keeping it resolvable is not. Measured on one project's catalog (2026-08-18):
    # 345/345 runs had a hash, only 40.6% still resolved, and 92.2% ran on a dirty tree -- so the
    # median run recorded a clean-looking hash for a tree that never existed. A per-run ref makes
    # the cited commit un-collectable and survives deletion of the branch it was made on, and on a
    # dirty tree the ref points at a snapshot of what actually ran.
    #
    # Best-effort by construction: provenance capture must never be able to fail a run.
    try:
        # Declared, load-bearing paths. bathos cannot discover UNdeclared inputs, but it can refuse
        # to let a declared one be silently omitted from the snapshot because the repo ignores it.
        declared = [str(p) for p in output_paths]
        if script_path is not None:
            declared.append(str(script_path))
        if bundle and bundle.path:
            declared.append(str(bundle.path))

        pin = pin_run(
            run_id=run.id,
            git_hash=git.hash,
            git_branch=git.branch,
            dirty=git.dirty,
            cwd=cwd,
            declared_paths=declared,
        )
        event("run.pinned", run_uuid=run.id, **pin_result_as_dict(pin))

        if pin.ignored_provenance_paths:
            # Loud, because the failure is silent otherwise: a claim written to an ignored path is
            # never committed, and a claim's sha256 is the tamper anchor its campaign's Union Gate
            # evaluates against. Observed in the wild leaving 3 of 4 claims untracked.
            print(
                "warning: these provenance paths are gitignored, so anything written there will "
                f"never be committed: {', '.join(pin.ignored_provenance_paths)}. "
                "Narrow the ignore rule (e.g. `.bth/*` plus `!.bth/claims/` and `!.bth/refs/`).",
                file=sys.stderr,
            )
        if pin.ignored_declared_paths:
            print(
                "warning: these declared paths are gitignored, so they were NOT captured in this "
                f"run's provenance snapshot: {', '.join(pin.ignored_declared_paths)}",
                file=sys.stderr,
            )
        if pin.snapshot_mode == "metadata_only":
            print(
                f"warning: working tree is {pin.skipped_bytes:,} bytes of uncommitted content -- "
                "too large to snapshot, so its CONTENTS were not captured. Largest contributors: "
                f"{', '.join(pin.skipped_paths[:5])}. Consider gitignoring these.",
                file=sys.stderr,
            )
        if pin.unpinned_reason and pin.snapshot_mode != "metadata_only":
            # A ref that failed to be written leaves the object collectable. Saying nothing here is
            # what turns an incomplete record into a false attestation.
            print(f"warning: run provenance is not durable: {pin.unpinned_reason}", file=sys.stderr)
    except Exception as e:  # pragma: no cover - defensive; pinning must not break a run
        event("run.pin_error", run_uuid=run.id, exc_type=type(e).__name__, exc_msg=str(e))

    results_temp_dir = Path(tempfile.gettempdir())

    # Instrument-sensitivity pre-flight (debt #1071): before the main run, prove the
    # measurement can actually detect a real effect by running the script once with the
    # [differential] knob set to "off" and once to "on". If the declared invariant doesn't
    # fire, the main subprocess never executes -- the run is recorded with
    # outcome="invalid_measurement" instead, so a broken measurement can never masquerade
    # as a legitimate pass/fail result.
    if sidecar is not None and sidecar.differential is not None:
        preflight = _run_differential_preflight(
            differential=sidecar.differential,
            argv=argv,
            cwd=cwd,
            base_env=os.environ.copy(),
            results_temp_dir=results_temp_dir,
            run_id=run.id,
        )
        if not preflight.ok:
            event("run.differential_preflight_failed", run_uuid=run.id, reason=preflight.reason)
            payload = _gate_failure_payload(
                error_code=GateErrorCode.DIFFERENTIAL_INVARIANT_VIOLATED,
                phase="pre_execution",
                errors=[preflight.reason],
                agent_mode=resolved_mode,
            )
            run = dataclasses.replace(
                run,
                status="completed",
                exit_code=0,
                outcome="invalid_measurement",
                outcome_error_reason=json.dumps(dataclasses.asdict(payload)),
                differential_status="invalid_measurement",
                differential_off_value=sidecar.differential.off,
                differential_on_value=sidecar.differential.on,
                differential_effect=preflight.effect,
            )
            try:
                write_run(run, catalog_dir)
            except Exception as e:
                event("run.error", phase="persist", exc_type=type(e).__name__, exc_msg=str(e))
                raise
            print(json.dumps(dataclasses.asdict(payload)), file=sys.stderr)
            return 1
        event("run.differential_preflight_passed", run_uuid=run.id)

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
    results_temp_path = results_temp_dir / f"{run.id}.bth-results.json"

    # Set up environment with results path and per-run output directory
    env = os.environ.copy()
    env["BTH_RESULTS_PATH"] = str(results_temp_path)
    bth_output_dir = cwd / "outputs" / run.id[:8]
    bth_output_dir.mkdir(parents=True, exist_ok=True)
    env["BTH_OUTPUT_DIR"] = str(bth_output_dir)

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

    # Read result emission from BTH_RESULTS_PATH, script-adjacent fallback, or --out
    metadata = _read_result_emission(results_temp_path, script_path, output_paths)

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

    # Auto-register any files written to BTH_OUTPUT_DIR that weren't in --out
    registered_paths = set(output_paths)
    discovered = sorted(str(p) for p in bth_output_dir.rglob("*") if p.is_file())
    new_paths = [p for p in discovered if p not in registered_paths]
    if new_paths:
        output_paths = list(output_paths) + new_paths
        event("run.output_dir_discovered", count=len(new_paths), bth_output_dir=str(bth_output_dir))

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

    # Evaluate the selected branch's adversarial_check (v14). Distinct from the status field
    # above: that records whether a check was DECLARED, this records what happened when it ran.
    adversarial_check_result = None
    if sidecar is not None and outcome:
        try:
            from bathos.sidecar import evaluate_adversarial_check

            adversarial_check_result = evaluate_adversarial_check(
                sidecar, outcome, json.loads(metadata) if metadata else {}
            )
        except (json.JSONDecodeError, TypeError):
            adversarial_check_result = None

    # Step 4: Extract parity_run_type from doubly-nested metadata (AC-19)
    # parity_validate.py emits result["metadata"]["parity_run_type"] = "literature_parity"
    # We extract it to the Run column for gates (F2, F3) to query
    parity_run_type = None
    try:
        meta = json.loads(metadata) if metadata else {}
        parity_run_type = (meta or {}).get("metadata", {}).get("parity_run_type")
    except (json.JSONDecodeError, TypeError, AttributeError):
        # If metadata is invalid/empty JSON, parity_run_type stays None
        pass

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
        adversarial_check_result=adversarial_check_result,
        output_paths=output_paths,
        parity_run_type=parity_run_type,
        # (debt #1071) The differential pre-flight already ran and passed by this point --
        # its short-circuit (outcome="invalid_measurement") returns before this code is ever
        # reached. Recording "passed" (not just leaving it None) is what lets a later Union
        # Gate/lint distinguish "instrument sensitivity was verified for this run" from
        # "no [differential] block was declared at all".
        differential_status=("passed" if sidecar and sidecar.differential else None),
        differential_off_value=(
            sidecar.differential.off if sidecar and sidecar.differential else None
        ),
        differential_on_value=(
            sidecar.differential.on if sidecar and sidecar.differential else None
        ),
    )

    # Obligation trigger 1 (§5.1): a computed outcome outside the pass direction opens an
    # obligation to explain it. Opt-in via BTH_OBLIGATION_OUTCOME_FAILED; a no-op otherwise,
    # which is the default. Opening is deliberately never fatal — a ledger write failure must
    # not lose a run that already completed and is about to be persisted.
    if sidecar is not None:
        try:
            from bathos.obligations import maybe_open
            from bathos.sidecar import is_failure_outcome
            from bathos.workspace import resolve_workspace

            ws_root = resolve_workspace(cwd).fs_root
            if is_failure_outcome(sidecar, outcome):
                maybe_open(
                    ws_root,
                    "run",
                    run.id,
                    "outcome_failed",
                    detail=f"outcome={outcome!r}",
                )
            # Trigger 3 (§5.3): the stricter bar failed on a run that otherwise landed on
            # this branch — the outcome is not to be believed at face value.
            if adversarial_check_result == "fired":
                spec = sidecar.outcomes.get(outcome)
                maybe_open(
                    ws_root,
                    "run",
                    run.id,
                    "adversarial_check_fired",
                    detail=(
                        f"outcome={outcome!r} but adversarial_check failed: "
                        f"{getattr(spec, 'adversarial_check', '') or ''}"
                    ),
                )
        except Exception as e:
            event("run.error", phase="obligation", exc_type=type(e).__name__, exc_msg=str(e))

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
    event(
        "run.parquet_written",
        path=str(parquet_path),
        bytes=parquet_bytes,
        duration_ms=parquet_duration_ms,
    )

    # Clean up temp results file if it exists
    if results_temp_path.exists():
        with suppress(OSError):
            results_temp_path.unlink()

    # Link this run into campaign_runs (the table campaign review/conclude actually
    # read from). The run only exists in the cool tier until compacted, so compact
    # first — add_run_to_campaign looks the run up in the warm `runs` table.
    # Membership is the cool parquet campaign_id column. Compact (or review) ingests
    # campaign_runs later — do not open bathos.db writable from array tasks.
    return exit_code
