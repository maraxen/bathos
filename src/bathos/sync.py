from __future__ import annotations

import contextlib
import json
import logging
import re
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bathos.catalog import iter_run_fragments
from bathos.cluster_catalog import ensure_remote_catalog_dir, remote_catalog_path
from bathos.config import ProjectConfig
from bathos.telemetry import event

logger = logging.getLogger(__name__)

_SYNC_TIMEOUT_S = 120  # rsync kill switch: fail if no completion within 2 minutes
_RSYNC_STALL_SECONDS = 30  # Stall detection: no progress for N seconds


def _popen_rsync_watchdog(cmd: list[str]) -> tuple[subprocess.Popen, threading.Event]:
    proc = subprocess.Popen(
        cmd,
        stderr=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    fired = threading.Event()

    def watchdog():
        try:
            proc.wait(timeout=_SYNC_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            fired.set()
            proc.kill()
        except Exception:
            pass

    threading.Thread(target=watchdog, daemon=True).start()
    return proc, fired


@dataclass
class SyncResult:
    """Result of a sync operation."""

    transferred: int
    duration_s: float
    remote: str
    filtered: int = 0
    truncated_candidates: list[str] = field(default_factory=list)


def _sync_shadow_dir(
    host: str,
    remote_cat: str,
    catalog_dir: Path,
    pull: bool,
    ssh_opts: str,
    remote_name: str,
    direction: str,
) -> None:
    """Carry the Bearings shadow log at its default ``<catalog_dir>/shadow`` (spec 261104 T7): at-work labels written
    by cluster runs are captured once, so they must come back. Files are per host and pid, so a merge never collides;
    ``--update`` re-copies a log that grew since the last sync (``--ignore-existing`` would freeze a partial copy).
    Best effort: a failure is said on stderr and never fails the sync. A shadow log configured elsewhere is not
    carried."""
    local = catalog_dir / "shadow"
    if not pull and not local.is_dir():
        return
    local.mkdir(parents=True, exist_ok=True)
    remote = f"{host}:{remote_cat}/shadow/"
    src, dst = (remote, str(local) + "/") if pull else (str(local) + "/", remote)
    cmd = [
        "rsync",
        "-az",
        f"-e{ssh_opts}",
        "--update",
        "--ignore-missing-args",
        "--info=progress2",
        src,
        dst,
    ]
    event(
        "sync.rsync_start",
        direction=direction,
        remote=remote_name,
        src=src,
        dst=dst,
        filters="shadow",
    )
    try:
        proc, fired = _popen_rsync_watchdog(cmd)
        _out, err = proc.communicate()
        code = proc.returncode
    except Exception as e:  # noqa: BLE001 - best effort, reported below
        code, err, fired = -1, str(e), None
    event("sync.rsync_end", exit_code=code, duration_ms=0, bytes_transferred=0, files_transferred=0)
    if code != 0 or (fired is not None and fired.is_set()):
        print(
            f"bathos: Bearings shadow log not synced ({direction} {remote}): rsync exit {code} {str(err)[:200]}",
            file=sys.stderr,
        )


def sync_catalog(
    remote_name: str, config: ProjectConfig, catalog_dir: Path, pull: bool = False
) -> SyncResult:
    """
    Sync cool-tier catalog to/from remote using rsync.

    Args:
        remote_name: Key in config.remotes (e.g., 'engaging')
        config: ProjectConfig with remotes dict
        catalog_dir: Local catalog directory (e.g., ~/.bth/catalog)
        pull: If False (default), push to remote; if True, pull from remote

    Returns:
        SyncResult with transferred file count, duration, and remote name

    Raises:
        ValueError: if remote_name not in config.remotes
        RuntimeError: if rsync fails
    """
    if remote_name not in config.remotes:
        raise ValueError(f"Remote '{remote_name}' not in config")

    remote_config = config.remotes[remote_name]
    host = remote_config["host"]
    remote_root = remote_config["remote_root"]
    ensure_remote_catalog_dir(host, remote_root)
    remote_cat = remote_catalog_path(remote_root)

    project_slug = config.slug
    sync_filter = getattr(config, "sync_filter", "project_slug")

    if sync_filter == "project_slug":
        local_runs = catalog_dir / "runs" / project_slug
        remote_runs = f"{host}:{remote_cat}/runs/{project_slug}/"
        # Count filtered runs (total in catalog minus this project's runs)
        runs_root = catalog_dir / "runs"
        if not pull and runs_root.exists():
            total = len(iter_run_fragments(runs_root))
            project_count = (
                len(iter_run_fragments(local_runs, recursive=False)) if local_runs.exists() else 0
            )
            filtered = total - project_count
        else:
            filtered = 0
    else:
        local_runs = catalog_dir / "runs"
        remote_runs = f"{host}:{remote_cat}/runs/"
        filtered = 0

    if not pull:
        local_runs.mkdir(parents=True, exist_ok=True)

    if pull:
        src = remote_runs
        dst = str(local_runs) + "/"
    else:
        src = str(local_runs) + "/"
        dst = remote_runs

    # ConnectTimeout=10: fail in 10s if host is unreachable rather than hanging
    # BatchMode=yes: never prompt for a password — fail immediately instead
    ssh_opts = "ssh -o ConnectTimeout=10 -o BatchMode=yes"

    # Build rsync command with --info=progress2 for streaming progress
    cmd = [
        "rsync",
        "-az",
        "--partial",
        f"-e{ssh_opts}",
        "--ignore-existing",
        "--info=progress2",
        src,
        dst,
    ]

    # Determine direction (push vs pull)
    direction = "pull" if pull else "push"

    # Emit telemetry: sync.rsync_start
    event(
        "sync.rsync_start",
        direction=direction,
        remote=remote_name,
        src=src,
        dst=dst,
        filters="project_slug" if sync_filter == "project_slug" else "none",
    )

    start_time = time.time()
    last_progress_ts = time.monotonic()
    last_progress_ts_lock = threading.Lock()
    last_progress_ts_ref = [last_progress_ts]  # mutable container for thread sharing
    total_bytes = 0
    total_files = 0
    progress2_seen = False
    proc = None
    stdout_output = ""
    _watchdog_fired = threading.Event()

    try:
        proc, _watchdog_fired = _popen_rsync_watchdog(cmd)

        # Stall monitor thread: detect silent hangs where no output is produced
        def stall_monitor():
            while not _watchdog_fired.is_set() and proc.poll() is None:
                time.sleep(5)
                with last_progress_ts_lock:
                    elapsed_since = (time.monotonic() - last_progress_ts_ref[0]) * 1000
                if elapsed_since > _RSYNC_STALL_SECONDS * 1000 and proc.poll() is None:
                    event("sync.rsync_stall", elapsed_since_progress_ms=int(elapsed_since))

        stall_thread = threading.Thread(target=stall_monitor, daemon=True)
        stall_thread.start()

        # Stream-read stderr for progress2 format and stdout for completion info
        if proc.stderr is not None:
            for line in iter(proc.stderr.readline, ""):
                if not line:
                    break
                line = line.strip()

                # Parse progress2 format: "   1,234 100%    1.23MB/s    0:00:01 (xfr#1, to-chk=0/1)"
                m = re.match(
                    r"\s*([\d,]+)\s+(\d+)%\s+([\d.]+\S*)\s+\S+(?:\s+\(xfr#(\d+),.*\))?",
                    line,
                )
                if m:
                    bytes_str = m.group(1).replace(",", "")
                    pct_str = m.group(2)
                    rate_str = m.group(3)
                    xfr_str = m.group(4)

                    try:
                        bytes_xfr = int(bytes_str)
                        pct = int(pct_str)
                        total_bytes = bytes_xfr
                        progress2_seen = True
                        # "xfr#N" is rsync's own running count of files transferred
                        # so far in this session -- the true file count. Bytes are
                        # per-file progress, not a transfer total, and must not be
                        # used as a stand-in for "how many files transferred"
                        # (#1945: that conflation is why `transferred` under-/
                        # over-reported, including showing 0 after a real transfer).
                        if xfr_str is not None:
                            total_files = int(xfr_str)
                        with last_progress_ts_lock:
                            last_progress_ts_ref[0] = time.monotonic()

                        event(
                            "sync.rsync_progress",
                            bytes_transferred=bytes_xfr,
                            pct=pct,
                            xfer_rate=rate_str,
                        )
                    except (ValueError, IndexError):
                        pass

        # Read any remaining stdout (e.g., final stats)
        remaining_stdout = proc.stdout.read() if proc.stdout is not None else ""
        if remaining_stdout:
            stdout_output = remaining_stdout

        # Wait for process to complete
        exit_code = proc.wait()

        # Check if watchdog killed the process
        if _watchdog_fired.is_set():
            duration_s = time.time() - start_time
            event(
                "sync.rsync_end",
                exit_code=-1,
                duration_ms=duration_s * 1000,
                bytes_transferred=total_bytes,
                files_transferred=total_files,
            )
            raise RuntimeError(f"rsync timed out after {_SYNC_TIMEOUT_S}s and was killed")
    except subprocess.TimeoutExpired:
        # Main thread's wait() timed out
        duration_s = time.time() - start_time
        event(
            "sync.rsync_end",
            exit_code=-1,
            duration_ms=duration_s * 1000,
            bytes_transferred=total_bytes,
            files_transferred=total_files,
        )
        raise RuntimeError(f"rsync timed out after {_SYNC_TIMEOUT_S}s and was killed")
    except Exception:
        duration_s = time.time() - start_time
        event(
            "sync.rsync_end",
            exit_code=-1,
            duration_ms=duration_s * 1000,
            bytes_transferred=total_bytes,
            files_transferred=total_files,
        )
        raise

    duration_s = time.time() - start_time
    duration_ms = duration_s * 1000

    # File count from rsync's own "xfr#N" progress2 counter if we saw any
    # progress2 output; otherwise fall back to parsing `--stats`-style totals
    # from stdout (e.g. when nothing hit stderr, as some mocked/older rsync
    # builds do). Bytes are never used as a substitute file count (#1945).
    transferred = total_files if progress2_seen else _parse_transferred_count(stdout_output)

    # Emit telemetry: sync.rsync_end
    event(
        "sync.rsync_end",
        exit_code=exit_code,
        duration_ms=duration_ms,
        bytes_transferred=total_bytes,
        files_transferred=total_files,
    )

    if exit_code != 0:
        raise RuntimeError(f"rsync failed with exit code {exit_code}")

    local_campaigns = catalog_dir / "campaigns"
    local_campaigns.mkdir(parents=True, exist_ok=True)
    remote_campaigns = f"{host}:{remote_cat}/campaigns/"
    if pull:
        c_src, c_dst = remote_campaigns, str(local_campaigns) + "/"
    else:
        c_src, c_dst = str(local_campaigns) + "/", remote_campaigns
    camp_cmd = [
        "rsync",
        "-az",
        f"-e{ssh_opts}",
        "--update",
        "--exclude",
        "*.tmp",
        "--info=progress2",
        c_src,
        c_dst,
    ]
    event(
        "sync.rsync_start",
        direction=direction,
        remote=remote_name,
        src=c_src,
        dst=c_dst,
        filters="campaigns",
    )
    camp_proc, camp_watchdog_fired = _popen_rsync_watchdog(camp_cmd)

    def _drain(pipe):
        if pipe is not None:
            pipe.read()

    drain_err = threading.Thread(target=_drain, args=(camp_proc.stderr,), daemon=True)
    drain_out = threading.Thread(target=_drain, args=(camp_proc.stdout,), daemon=True)
    drain_err.start()
    drain_out.start()
    drain_err.join()
    drain_out.join()
    camp_exit = camp_proc.wait()
    if camp_watchdog_fired.is_set() or camp_exit == -9:
        raise RuntimeError(f"rsync timed out after {_SYNC_TIMEOUT_S}s and was killed")
    event(
        "sync.rsync_end",
        exit_code=camp_exit,
        duration_ms=0,
        bytes_transferred=0,
        files_transferred=0,
    )
    if camp_exit != 0:
        raise RuntimeError(f"rsync failed with exit code {camp_exit}")

    _sync_shadow_dir(host, remote_cat, catalog_dir, pull, ssh_opts, remote_name, direction)

    # Post-pull truncation scan (Fix 6)
    truncated_candidates = []
    if pull:
        import pyarrow.parquet as pq

        scan_window_start = start_time - 5.0  # 5s buffer for clock skew
        for parquet_file in iter_run_fragments(Path(dst.rstrip("/ "))):
            if parquet_file.stat().st_mtime < scan_window_start:
                continue  # skip files that predate this pull
            try:
                pq.read_metadata(str(parquet_file))
            except Exception:
                truncated_candidates.append(str(parquet_file))
                logger.warning("Truncated or corrupt Parquet after sync: %s", parquet_file)
                event("sync.truncated_fragment", path=str(parquet_file))

    return SyncResult(
        transferred=transferred,
        duration_s=duration_s,
        remote=remote_name,
        filtered=filtered,
        truncated_candidates=truncated_candidates,
    )


def cluster_log_remote_dirs(project_root: Path, remote_name: str) -> tuple[Path, Path, Path]:
    """Local destination directories for the cluster log pull (spec
    "Cluster"): `<project_root>/.bth/log/remote/<remote_name>/{log,fallback,mirror}/`,
    the same subtree `bathos.runlog.ingest.discover_roots` enumerates as the
    `remote-log`/`remote-fallback`/`remote-mirror` root kinds."""
    base = project_root / ".bth" / "log" / "remote" / remote_name
    return base / "log", base / "fallback", base / "mirror"


@dataclass
class ClusterLogPullResult:
    """Structured, per-subpath outcome of `pull_cluster_log`.

    Each of the three sub-pulls (`log`/`fallback`/`mirror`) is caught
    individually and best-effort (see `pull_cluster_log`'s docstring), so a
    call that returns normally can still have pulled only SOME of the three:
    each sub-pull is best-effort, and a failing one is recorded here rather
    than raised (e.g. a fallback directory never written on the remote, or
    an installed myxcel that predates `pull-path`, which raises
    `MyxcelCapabilityGapError`; see `bathos.cluster.pull_path`). A caller must
    inspect this result -- `bool(pulled)`/`any_pulled` and `failures` -- rather
    than treat a normal return as full success.

    - `pulled`: labels (`"log"`/`"fallback"`/`"mirror"`) that succeeded.
    - `failures`: one `{"label", "src", "remote", "error"}` dict per
      sub-path that raised, in attempt order.
    """

    pulled: list[str] = field(default_factory=list)
    failures: list[dict] = field(default_factory=list)

    @property
    def any_pulled(self) -> bool:
        return bool(self.pulled)


def pull_cluster_log(
    remote_name: str,
    config: ProjectConfig,
    project_root: Path,
    project_id: str | None,
) -> ClusterLogPullResult:
    """Cluster log pull (spec "Cluster", delivery step 4 wave d): pull the
    remote checkout's `.bth/log/`, the remote `~/.bth/log/fallback/<slug>/`,
    and the remote `~/.bth/log-mirror/<project_id>/` (or, with no project id,
    `~/.bth/log-mirror/_null/<slug>/` -- `bth submit` exports `BTH_PROJECT_ID`,
    and the writer also falls back to the resolved `.bth.toml` id when that
    env var is absent, per D7/`resolve_write_project_id`)
    into the local main checkout's
    `.bth/log/remote/<remote_name>/{log,fallback,mirror}/`.

    Each destination is its own root kind for ingest (`remote-log`,
    `remote-fallback`, `remote-mirror`, `bathos.runlog.ingest.discover_roots`);
    `eid` dedup at ingest makes the overlapping copies -- and a repeated pull
    of the same segments -- safe, so this function is a plain, idempotent,
    best-effort refresh of those three directories.

    Goes through `bathos.cluster.pull_path` (a myxcel wrapper), never rsync
    directly (CLAUDE.md "Bathos Sync Delegates to Myxcel"). Each of the three
    sub-pulls is independent and best-effort: a fallback or mirror directory
    that has never been written on the remote (the common case -- most runs
    never fail over to the fallback, or lose an active segment) must not
    abort the other two.

    Returns a `ClusterLogPullResult` recording which sub-paths actually
    pulled and which failed (with reasons). This function never raises for a
    per-subpath failure (see above); it only raises for a bad `remote_name`.
    Each sub-pull runs `bathos.cluster.pull_path` (myxcel `pull-path`, debt
    #1993), so a sub-path can fail for a missing myxcel subcommand, a
    transfer error, or a timeout. Callers must report the per-subpath result
    honestly rather than reading "no exception" as "pulled".
    """
    if remote_name not in config.remotes:
        raise ValueError(f"Remote '{remote_name}' not in config")
    remote_config = config.remotes[remote_name]
    host = remote_config["host"]
    remote_root = remote_config["remote_root"].rstrip("/")
    slug = config.slug

    log_dest, fallback_dest, mirror_dest = cluster_log_remote_dirs(project_root, remote_name)
    for d in (log_dest, fallback_dest, mirror_dest):
        d.mkdir(parents=True, exist_ok=True)

    from bathos.cluster import pull_path

    mirror_component = project_id if project_id else f"_null/{slug}"

    result = ClusterLogPullResult()

    for src, dst, label in (
        (f"{remote_root}/.bth/log/", log_dest, "log"),
        (f"~/.bth/log/fallback/{slug}/", fallback_dest, "fallback"),
        (f"~/.bth/log-mirror/{mirror_component}/", mirror_dest, "mirror"),
    ):
        try:
            # `host` is passed as the myxcel PROFILE name. This couples to
            # `remote_config["host"]` being the profile name, which holds for
            # every current .bth.toml ("engaging", "titanix"). If a remote's
            # host ever diverges from its myxcel profile name, map it here.
            pull_path(host, src, str(dst) + "/")
        except Exception as exc:  # noqa: BLE001 -- best-effort, see docstring
            logger.warning(
                "bth sync --pull: cluster log %s pull from %s:%s failed (remote=%s): %s",
                label,
                host,
                src,
                remote_name,
                exc,
            )
            result.failures.append(
                {"label": label, "src": src, "remote": remote_name, "error": str(exc)}
            )
        else:
            result.pulled.append(label)

    return result


def _parse_transferred_count(rsync_output: str) -> int:
    """
    Parse number of transferred files from rsync output.

    Returns 0 if count cannot be determined.
    """
    for line in rsync_output.split("\n"):
        if "files transferred" in line:
            # Extract number from lines like "Number of regular files transferred: 42"
            parts = line.split(":")
            if len(parts) > 1:
                try:
                    return int(parts[-1].strip().split()[0])
                except (ValueError, IndexError):
                    pass
    return 0


# ---------------------------------------------------------------------------
# Option A: Selective pull via remote `bth bundle export` + `pull_path`,
# with mirror fallback (`sync_pull_mirror_unverified`). Never imports myxcel
# and never streams binary archive bytes over SSH stdout (NC-10).
# ---------------------------------------------------------------------------

_SSH_BASE = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]

_REMOTE_SELECTIVE_EXPORT_PY = r"""
import json, os, shutil, subprocess, sys, uuid
from datetime import datetime, timezone
from pathlib import Path

cfg = json.loads(sys.argv[1])
bth_bin = shutil.which("bth")
if not bth_bin:
    cand = Path.home() / ".local" / "bin" / "bth"
    if cand.is_file():
        bth_bin = str(cand)
if not bth_bin:
    print(json.dumps({"mode": "no_bundle_cmd", "reason": "bth not found"}))
    sys.exit(0)

probe = subprocess.run([bth_bin, "bundle", "export", "--help"], capture_output=True, text=True)
probe_txt = (probe.stdout + "\n" + probe.stderr).lower()
if probe.returncode != 0 or any(m in probe_txt for m in ("no such command", "unknown", "unused tokens")):
    print(json.dumps({"mode": "no_bundle_cmd", "reason": "bth bundle export unavailable"}))
    sys.exit(0)

run_ids = cfg.get("run_ids") or []
project = cfg.get("project") or ""
since_iso = cfg.get("since_iso") or ""
include_outputs = bool(cfg.get("include_outputs"))
with_campaign = bool(cfg.get("with_campaign"))
allow_missing_sidecar = bool(cfg.get("allow_missing_sidecar"))
dry_run = bool(cfg.get("dry_run"))

def _parse_ts(raw):
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None

since_dt = _parse_ts(since_iso)

matched = []
if since_dt is not None or not run_ids or dry_run:
    find_cmd = [bth_bin, "find", "--include-superseded"]
    if project:
        find_cmd.extend(["--project", project])
    res = subprocess.run(find_cmd, capture_output=True, text=True)
    if res.returncode != 0:
        sys.stderr.write(res.stderr or res.stdout)
        sys.exit(res.returncode or 1)
    try:
        payload = json.loads(res.stdout)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"Invalid JSON from remote bth find: {exc}\n")
        sys.exit(1)
    runs = payload.get("runs") or []
    if since_dt is not None:
        filtered = []
        for r in runs:
            rt = _parse_ts(r.get("timestamp"))
            if rt is not None and rt >= since_dt:
                filtered.append(r)
        runs = filtered
    if run_ids:
        selected = []
        seen = set()
        for prefix in run_ids:
            hits = [r for r in runs if str(r.get("id", "")).startswith(prefix)]
            if not hits:
                sys.stderr.write(f"run_not_found: {prefix}\n")
                sys.exit(2)
            if len(hits) > 1:
                sys.stderr.write(f"run_prefix_ambiguous: {prefix} matches {len(hits)} runs\n")
                sys.exit(2)
            rid = hits[0]["id"]
            if rid not in seen:
                seen.add(rid)
                selected.append(rid)
        matched = selected
    else:
        matched = [str(r["id"]) for r in runs if r.get("id")]
else:
    matched = list(dict.fromkeys(run_ids))

if dry_run:
    print(json.dumps({"mode": "bundle", "dry_run": True, "matched_runs": matched, "staged_dir": None, "staged_files": [], "exports": []}))
    sys.exit(0)

if not matched:
    print(json.dumps({"mode": "bundle", "dry_run": False, "matched_runs": [], "staged_dir": None, "staged_files": [], "exports": []}))
    sys.exit(0)

batch_name = "sync-" + uuid.uuid4().hex[:12]
staged_abs = Path.home() / ".bth" / "tmp" / batch_name
staged_abs.mkdir(parents=True, exist_ok=True)
staged_rel = f"~/.bth/tmp/{batch_name}"

exports = []
staged_files = []
resolved_ids = []
try:
    for idx, rid in enumerate(matched):
        out_name = f"{idx:04d}.bthbundle"
        out_abs = staged_abs / out_name
        cmd = [bth_bin, "bundle", "export", rid, "-o", str(out_abs)]
        if project:
            cmd.extend(["--project", project])
        if include_outputs:
            cmd.append("--include-outputs")
        if with_campaign:
            cmd.append("--with-campaign")
        if allow_missing_sidecar:
            cmd.append("--allow-missing-sidecar")
        exp = subprocess.run(cmd, capture_output=True, text=True)
        if exp.returncode != 0:
            raise RuntimeError((exp.stderr or exp.stdout or f"bundle export failed for {rid}").strip())
        exp_json = json.loads(exp.stdout)
        if exp_json.get("error"):
            raise RuntimeError(str(exp_json["error"]))
        exports.append(exp_json)
        staged_files.append(f"{staged_rel}/{out_name}")
        resolved_ids.append(str(exp_json.get("run_id") or rid))
except Exception as exc:
    shutil.rmtree(staged_abs, ignore_errors=True)
    sys.stderr.write(f"{exc}\n")
    sys.exit(1)

print(json.dumps({
    "mode": "bundle",
    "dry_run": False,
    "batch_name": batch_name,
    "matched_runs": resolved_ids,
    "staged_dir": staged_rel,
    "staged_files": staged_files,
    "exports": exports,
}))
""".strip()


def _parse_since_timestamp(since: str) -> datetime | None:
    if not since or not since.strip():
        return None
    from bathos.cli_common import parse_since

    dt = parse_since(since)
    if dt is None:
        raise ValueError(
            f"Invalid --since value {since!r}: expected duration (e.g. '24h', '7d') or ISO-8601 timestamp"
        )
    return dt


def _parse_event_ts(raw: object) -> datetime | None:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)
    except ValueError:
        return None


def _run_id_in_run_ids(raw_rids: Any, run_id: str) -> bool:
    if isinstance(raw_rids, str):
        try:
            raw_rids = json.loads(raw_rids)
        except json.JSONDecodeError:
            return False
    return isinstance(raw_rids, list) and run_id in raw_rids


def _collect_staged_causal_closure(
    all_events: list[dict[str, Any]],
    run_id: str,
    run_row: dict[str, Any],
    *,
    with_campaign: bool,
) -> list[dict[str, Any]]:
    """Select the causal closure for ``run_id`` from an in-memory pool of staged mirror/log events."""
    from bathos.run_bundle import _CAMPAIGN_EVENT_KINDS  # noqa: PLC2701

    sc_path = run_row.get("sidecar_path") or ""
    sc_sha = run_row.get("sidecar_sha256") or ""
    slurm_job_id = run_row.get("slurm_job_id") or ""
    slug = run_row.get("project_slug") or ""
    cmd = run_row.get("command") or ""
    campaign_id = run_row.get("campaign_id") or ""
    derived_obs_id: str | None = None
    raw_meta = run_row.get("metadata")
    if isinstance(raw_meta, str) and raw_meta.strip():
        with contextlib.suppress(json.JSONDecodeError):
            parsed = json.loads(raw_meta)
            if isinstance(parsed, dict) and parsed.get("derived_from_observation"):
                derived_obs_id = str(parsed["derived_from_observation"])
    elif isinstance(raw_meta, dict) and raw_meta.get("derived_from_observation"):
        derived_obs_id = str(raw_meta["derived_from_observation"])

    selected: dict[str, dict[str, Any]] = {}

    def _keep(ev: dict[str, Any]) -> None:
        eid = str(ev.get("eid") or "")
        if eid and eid not in selected:
            selected[eid] = ev

    for ev in all_events:
        kind = str(ev.get("kind") or "")
        entity = ev.get("entity") or []
        data = ev.get("data") or {}

        if entity == [run_id]:
            _keep(ev)
        elif kind.startswith("anchor.") and sc_path and sc_sha:
            if data.get("path") == sc_path and data.get("sha256") == sc_sha:
                _keep(ev)
        elif kind.startswith("output_copy.") and len(entity) == 2 and entity[0] == run_id:
            _keep(ev)
        elif kind.startswith("submit."):
            if (slurm_job_id and data.get("slurm_job_id") == slurm_job_id) or (
                not slurm_job_id
                and slug
                and sc_sha
                and cmd
                and data.get("project_slug") == slug
                and data.get("sidecar_sha256") == sc_sha
                and data.get("command") == cmd
            ):
                _keep(ev)
        elif kind.startswith("edge.") and len(entity) == 3 and entity[2] == "run":
            if run_id in (entity[0], entity[1]):
                _keep(ev)
        elif kind.startswith("supersession."):
            if (data.get("entity_type") == "run" and data.get("entity_id") == run_id) or (
                data.get("superseded_by_type") == "run" and data.get("superseded_by_id") == run_id
            ):
                _keep(ev)
        elif kind.startswith("blast_radius."):
            if (data.get("entity_type") == "run" and data.get("entity_id") == run_id) or (
                sc_sha and data.get("entity_type") == "sidecar" and data.get("anchor_value") == sc_sha
            ):
                _keep(ev)
        elif kind.startswith("trust_ledger.") and data.get("run_id") == run_id:
            _keep(ev)
        elif kind.startswith("archived_item."):
            if _run_id_in_run_ids(data.get("run_ids"), run_id):
                _keep(ev)
        elif kind.startswith("observation."):
            if data.get("run_id") == run_id or (derived_obs_id and entity == [derived_obs_id]):
                _keep(ev)
        elif kind in ("validity.acknowledged", "validity.estimand_override_recorded"):
            if _run_id_in_run_ids(data.get("run_ids"), run_id):
                _keep(ev)
        elif with_campaign and campaign_id and (
            entity in ([campaign_id], [campaign_id, run_id])
            or (
                kind.startswith("campaign_edge.")
                and len(entity) == 3
                and entity[2] == "campaign"
                and campaign_id in (entity[0], entity[1])
            )
        ):
            _keep(ev)

    if not with_campaign:
        filtered = {
            eid: ev
            for eid, ev in selected.items()
            if ev.get("kind")
            not in (_CAMPAIGN_EVENT_KINDS | {"campaign.run_added", "campaign_run.imported"})
        }
        selected = filtered

    return sorted(selected.values(), key=lambda e: (e.get("ts", ""), e.get("eid", "")))


def _mirror_fallback_selective_pull(
    *,
    remote_name: str,
    host: str,
    remote_root: str,
    config: ProjectConfig,
    catalog_dir: Path,
    run_ids: list[str],
    project: str,
    since_dt: datetime | None,
    with_campaign: bool,
    allow_missing_sidecar: bool,
    dry_run: bool,
    project_root: Path | None,
    worktree_root: str | Path | None,
) -> dict[str, Any]:
    """Fallback when remote lacks `bth bundle export`: pull mirror/log into temp staging,
    filter causal closure, enforce guards, write `sync_pull_mirror_unverified`, and ingest."""
    from bathos.cluster import pull_path
    from bathos.errors import RunBundleInvalidError, RunImportConflictError
    from bathos.output_tiers.reference import RUN_NOT_FOUND, OutputTierError
    from bathos.query import AmbiguousRunId
    from bathos.run_bundle import (
        _atomic_write_bytes,
        _canonical_bytes_strict,
        _check_events_equivocation,
        _check_git_commit_local,
        _compute_digest_strict,
        _loads_strict,
        _now_utc_iso,
        _probe_sidecar_bytes,
        _resolve_local_project_roots,
        _sanitize_path_segment,
        _sha256_bytes,
        _validate_safe_run_id,
        _validate_safe_sha256,
    )
    from bathos.runlog.fold_runs import fold_run
    from bathos.runlog.ingest import _RUN_KINDS, _validate_envelope, run_ingest
    from bathos.runlog.project_id import read_project_id, register_main_root
    from bathos.runlog.resolve import resolve_log_root

    slug = project or config.slug
    project_id: str | None = None
    search_root = project_root or Path.cwd()
    with contextlib.suppress(Exception):
        lr = resolve_log_root(search_root)
        if not lr.unaffiliated:
            project_id = read_project_id(lr.main_root / ".bth.toml")
    if project_id is None and config.root is not None:
        with contextlib.suppress(Exception):
            project_id = read_project_id(Path(config.root) / ".bth.toml")

    with tempfile.TemporaryDirectory(prefix="bth-sync-mirror-") as tmp_str:
        tmp_dir = Path(tmp_str)
        stage_log = tmp_dir / "log"
        stage_fallback = tmp_dir / "fallback"
        stage_mirror = tmp_dir / "mirror"
        for d in (stage_log, stage_fallback, stage_mirror):
            d.mkdir(parents=True, exist_ok=True)

        sources: list[tuple[str, Path]] = [
            (f"{remote_root.rstrip('/')}/.bth/log/", stage_log),
            (f"~/.bth/log/fallback/{slug}/", stage_fallback),
        ]
        if project_id and (not project or project == config.slug):
            sources.append((f"~/.bth/log-mirror/{project_id}/", stage_mirror))
        else:
            sources.append((f"~/.bth/log-mirror/_null/{slug}/", stage_mirror))
            sources.append(("~/.bth/log-mirror/", stage_mirror))

        pulled_any = False
        pull_errors: list[str] = []
        for src, dst in sources:
            try:
                pull_path(host, src, str(dst) + "/")
                pulled_any = True
            except Exception as exc:  # noqa: BLE001 - best-effort per subpath
                pull_errors.append(f"{src}: {exc}")

        if not pulled_any and not any(tmp_dir.rglob("*.jsonl")):
            raise RuntimeError(
                "Mirror fallback could not pull any remote logs: " + "; ".join(pull_errors)
            )

        events_by_eid: dict[str, dict[str, Any]] = {}
        digests_by_eid: dict[str, str] = {}
        for jsonl_file in sorted(tmp_dir.rglob("*.jsonl")):
            for raw_line in jsonl_file.read_text(encoding="utf-8").splitlines():
                if not raw_line.strip():
                    continue
                ev = _loads_strict(raw_line)
                err = _validate_envelope(ev)
                if err:
                    raise RunBundleInvalidError(f"Invalid event in staged remote mirror: {err}")
                eid = str(ev["eid"])
                digest = _compute_digest_strict(ev)
                if eid in events_by_eid:
                    if digests_by_eid[eid] != digest:
                        raise RunImportConflictError(
                            f"Conflicting envelope for eid={eid} across staged remote logs"
                        )
                    continue
                events_by_eid[eid] = ev
                digests_by_eid[eid] = digest

        all_events = sorted(events_by_eid.values(), key=lambda e: (e.get("ts", ""), e.get("eid", "")))
        run_events_map: dict[str, list[dict[str, Any]]] = {}
        for ev in all_events:
            if ev.get("kind") in _RUN_KINDS:
                ent = ev.get("entity")
                if isinstance(ent, list) and len(ent) == 1 and isinstance(ent[0], str):
                    run_events_map.setdefault(ent[0], []).append(ev)

        folded_by_id: dict[str, dict[str, Any]] = {}
        for rid, reves in run_events_map.items():
            folded = fold_run(reves)
            if project and folded.get("project_slug") != project:
                continue
            if since_dt is not None:
                rt = _parse_event_ts(folded.get("timestamp"))
                if rt is None or rt < since_dt:
                    continue
            folded_by_id[rid] = folded

        if run_ids:
            matched_ids: list[str] = []
            for prefix in run_ids:
                hits = sorted(rid for rid in folded_by_id if rid.startswith(prefix))
                if not hits:
                    raise OutputTierError(RUN_NOT_FOUND, prefix)
                if len(hits) > 1:
                    raise AmbiguousRunId(prefix, hits)
                if hits[0] not in matched_ids:
                    matched_ids.append(hits[0])
        else:
            matched_ids = sorted(folded_by_id)

        warnings: list[str] = []
        planned: list[dict[str, Any]] = []
        for rid in matched_ids:
            _validate_safe_run_id(rid)
            reves = run_events_map[rid]
            if not any(ev.get("kind") in ("run.started", "run.imported") for ev in reves):
                raise RunBundleInvalidError(
                    f"Remote run {rid} in mirror lacks genesis event (run.started / run.imported)"
                )
            folded = folded_by_id[rid]
            if folded.get("status") == "running":
                warnings.append(f"Run {rid} is non-terminal (status='running') on remote mirror")

            closure = _collect_staged_causal_closure(
                all_events, rid, folded, with_campaign=with_campaign
            )
            ev_digests = {str(ev["eid"]): digests_by_eid[str(ev["eid"])] for ev in closure}
            new_events, skipped_eids = _check_events_equivocation(
                catalog_dir,
                run_id=rid,
                events=closure,
                event_digests=ev_digests,
            )

            started_evs = [e for e in reves if e.get("kind") == "run.started"] or reves
            ev_pid = next((e.get("project_id") for e in started_evs if e.get("project_id")), None)
            local_main_root, local_worktree_root = _resolve_local_project_roots(
                worktree_root=worktree_root,
                bundle_project_id=ev_pid,
                bundle_project_slug=folded.get("project_slug"),
            )
            sc_sha = str(folded.get("sidecar_sha256") or "")
            sc_path = str(folded.get("sidecar_path") or "")
            local_sc_path: Path | None = None
            if sc_sha:
                _validate_safe_sha256(sc_sha, label="sidecar_sha256")
                found_bytes, local_sc_path = _probe_sidecar_bytes(
                    sidecar_path=sc_path,
                    sidecar_sha256=sc_sha,
                    run_id=rid,
                    catalog_dir=catalog_dir,
                    worktree_root=str(local_worktree_root) if local_worktree_root else None,
                    main_root=str(local_main_root) if local_main_root else None,
                    allow_raw_sidecar_path=False,
                )
                if found_bytes is None and not allow_missing_sidecar:
                    raise RunBundleInvalidError(
                        f"Mirror fallback run {rid} requires sidecar sha256={sc_sha}, which is missing locally; "
                        "pass --allow-missing-sidecar to proceed"
                    )

            events_bytes = b"".join(_canonical_bytes_strict(ev) + b"\n" for ev in closure)
            synth_sha = _sha256_bytes(events_bytes)
            planned.append(
                {
                    "run_id": rid,
                    "folded": folded,
                    "closure": closure,
                    "events_bytes": events_bytes,
                    "bundle_sha256": synth_sha,
                    "new_events": new_events,
                    "skipped_eids": skipped_eids,
                    "started": started_evs[0],
                    "local_main_root": local_main_root,
                    "local_worktree_root": local_worktree_root,
                    "local_sc_path": local_sc_path,
                    "sc_sha": sc_sha,
                }
            )

        if dry_run:
            return {
                "ok": True,
                "mode": "sync_pull_mirror_unverified",
                "remote": remote_name,
                "dry_run": True,
                "matched_runs": matched_ids,
                "imported": [
                    {
                        "run_id": p["run_id"],
                        "status": "already_present" if not p["new_events"] else "would_import",
                        "new_events": len(p["new_events"]),
                        "skipped_existing": len(p["skipped_eids"]),
                        "import_method": "sync_pull_mirror_unverified",
                    }
                    for p in planned
                ],
                "warnings": warnings,
            }

        imported_results: list[dict[str, Any]] = []
        for p in planned:
            rid = p["run_id"]
            folded = p["folded"]
            origin_host = str(folded.get("hostname") or remote_name or "remote")
            safe_host = _sanitize_path_segment(origin_host, "remote")
            safe_slug = _sanitize_path_segment(folded.get("project_slug"), "_unaffiliated")
            local_main_root = p["local_main_root"]
            local_worktree_root = p["local_worktree_root"]
            if local_main_root is not None:
                target_dir = local_main_root / ".bth" / "log" / "imported" / safe_host
                with contextlib.suppress(Exception):
                    register_main_root(local_main_root)
            else:
                target_dir = Path.home() / ".bth" / "log" / "imported" / safe_slug / safe_host

            _validate_safe_run_id(rid)
            stem = f"{rid}.{p['bundle_sha256'][:12]}"
            jsonl_path = target_dir / f"{stem}.jsonl"
            receipt_path = target_dir / f"{stem}.receipt.json"

            if not p["new_events"] and receipt_path.is_file():
                imported_results.append(
                    {
                        "ok": True,
                        "status": "already_present",
                        "run_id": rid,
                        "origin_host": origin_host,
                        "bundle_sha256": p["bundle_sha256"],
                        "new_events": 0,
                        "skipped_existing": len(p["skipped_eids"]),
                        "import_method": "sync_pull_mirror_unverified",
                    }
                )
                continue

            cid = folded.get("campaign_id") or None
            ext_camp_ref = None if with_campaign else cid
            git_commit_local = _check_git_commit_local(
                str(folded.get("git_hash") or ""),
                local_worktree_root or local_main_root,
            )
            receipt = {
                "v": 1,
                "run_id": rid,
                "bundle_sha256": p["bundle_sha256"],
                "origin_host": origin_host,
                "source_main_root": str(p["started"].get("main_root") or ""),
                "source_worktree_root": str(p["started"].get("worktree_root") or ""),
                "imported_at": _now_utc_iso(),
                "import_method": "sync_pull_mirror_unverified",
                "remote_name": remote_name,
                "local_worktree_root": str(local_worktree_root) if local_worktree_root else None,
                "local_sidecar_path": str(p["local_sc_path"]) if p["local_sc_path"] else None,
                "external_campaign_ref": ext_camp_ref,
                "git_commit_local": git_commit_local,
                "sidecar_missing_allowed": bool(
                    allow_missing_sidecar and p["local_sc_path"] is None and p["sc_sha"]
                ),
            }
            _atomic_write_bytes(jsonl_path, p["events_bytes"])
            _atomic_write_bytes(
                receipt_path,
                (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
            imported_results.append(
                {
                    "ok": True,
                    "status": "imported",
                    "run_id": rid,
                    "origin_host": origin_host,
                    "bundle_sha256": p["bundle_sha256"],
                    "new_events": len(p["new_events"]),
                    "skipped_existing": len(p["skipped_eids"]),
                    "import_method": "sync_pull_mirror_unverified",
                }
            )

        ingest_report = run_ingest(catalog_dir)
        return {
            "ok": True,
            "mode": "sync_pull_mirror_unverified",
            "remote": remote_name,
            "dry_run": False,
            "matched_runs": matched_ids,
            "imported": imported_results,
            "imported_count": sum(1 for r in imported_results if r["status"] == "imported"),
            "already_present_count": sum(
                1 for r in imported_results if r["status"] == "already_present"
            ),
            "warnings": warnings,
            "ingest_report": {
                "new_events": ingest_report.new_events,
                "skipped_existing": ingest_report.skipped_existing,
                "quarantined": ingest_report.quarantined,
                "entities_refolded": ingest_report.entities_refolded,
            },
        }


def selective_pull_runs(
    remote_name: str,
    config: ProjectConfig,
    catalog_dir: Path,
    *,
    run_ids: list[str] | None = None,
    project: str = "",
    since: str = "",
    include_outputs: bool = False,
    with_campaign: bool = False,
    allow_missing_sidecar: bool = False,
    dry_run: bool = False,
    project_root: Path | None = None,
    worktree_root: str | Path | None = None,
) -> dict[str, Any]:
    """Selectively pull matching runs from ``remote_name`` via remote ``bth bundle export`` + ``pull_path``,
    falling back to ``sync_pull_mirror_unverified`` when the remote lacks ``bth bundle``."""
    if remote_name not in config.remotes:
        raise ValueError(f"Remote '{remote_name}' not in config")

    remote_config = config.remotes[remote_name]
    host = remote_config["host"]
    remote_root = remote_config["remote_root"]
    since_dt = _parse_since_timestamp(since)
    req_runs = [r.strip() for r in (run_ids or []) if r and r.strip()]

    payload_cfg = {
        "run_ids": req_runs,
        "project": project or "",
        "since_iso": since_dt.isoformat() if since_dt is not None else "",
        "include_outputs": include_outputs,
        "with_campaign": with_campaign,
        "allow_missing_sidecar": allow_missing_sidecar,
        "dry_run": dry_run,
    }
    remote_cmd = (
        f"python3 -c {shlex.quote(_REMOTE_SELECTIVE_EXPORT_PY)} "
        f"{shlex.quote(json.dumps(payload_cfg))}"
    )
    proc = subprocess.run(
        [*_SSH_BASE, host, remote_cmd],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if proc.returncode != 0:
        err_msg = (proc.stderr or proc.stdout or f"ssh exit {proc.returncode}").strip()
        # Check if remote lacked bth/python or bundle command
        low = err_msg.lower()
        if any(m in low for m in ("no such command", "unknown command", "unused tokens")):
            meta = {"mode": "no_bundle_cmd"}
        else:
            raise RuntimeError(f"Selective pull SSH failed on {host}: {err_msg}")
    else:
        try:
            meta = json.loads(proc.stdout.strip() or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"Invalid JSON from selective pull SSH on {host}: {proc.stdout[:200]}"
            ) from exc

    if meta.get("mode") == "no_bundle_cmd":
        return _mirror_fallback_selective_pull(
            remote_name=remote_name,
            host=host,
            remote_root=remote_root,
            config=config,
            catalog_dir=catalog_dir,
            run_ids=req_runs,
            project=project,
            since_dt=since_dt,
            with_campaign=with_campaign,
            allow_missing_sidecar=allow_missing_sidecar,
            dry_run=dry_run,
            project_root=project_root,
            worktree_root=worktree_root,
        )

    matched_runs = list(meta.get("matched_runs") or [])
    if dry_run or not matched_runs:
        return {
            "ok": True,
            "mode": "selective_bundle",
            "remote": remote_name,
            "dry_run": dry_run,
            "matched_runs": matched_runs,
            "imported": [],
            "imported_count": 0,
            "already_present_count": 0,
        }

    from bathos.cluster import pull_path
    from bathos.run_bundle import import_run_bundle

    staged_dir = str(meta.get("staged_dir") or "")
    staged_files = list(meta.get("staged_files") or [])
    batch_name = str(meta.get("batch_name") or (Path(staged_dir).name if staged_dir else ""))

    imported: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="bth-sync-pull-") as local_tmp_str:
        local_tmp = Path(local_tmp_str)
        try:
            if staged_dir:
                pull_path(host, staged_dir.rstrip("/") + "/", str(local_tmp) + "/")
            bundle_files = sorted(local_tmp.glob("*.bthbundle"))
            if not bundle_files and staged_files:
                for sf in staged_files:
                    pull_path(host, sf, str(local_tmp) + "/")
                bundle_files = sorted(local_tmp.glob("*.bthbundle"))
            if not bundle_files:
                raise RuntimeError(
                    f"No bundle files arrived after pull_path from {host}:{staged_dir or staged_files}"
                )
            for bf in bundle_files:
                res = import_run_bundle(
                    bf,
                    allow_missing_sidecar=allow_missing_sidecar,
                    catalog_dir=catalog_dir,
                    worktree_root=worktree_root,
                    import_method="sync_pull",
                    remote_name=remote_name,
                )
                imported.append(res)
        finally:
            if batch_name and re.fullmatch(r"[A-Za-z0-9._-]+", batch_name):
                with contextlib.suppress(Exception):
                    subprocess.run(
                        [*_SSH_BASE, host, f'rm -rf "$HOME/.bth/tmp/{batch_name}"'],
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )

    return {
        "ok": True,
        "mode": "selective_bundle",
        "remote": remote_name,
        "dry_run": False,
        "matched_runs": matched_runs,
        "imported": imported,
        "imported_count": sum(1 for r in imported if r.get("status") == "imported"),
        "already_present_count": sum(1 for r in imported if r.get("status") == "already_present"),
    }
