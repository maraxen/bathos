"""Reaper for orphaned bathos runs: mark abandoned without deleting."""

import json
import os
import shlex
import socket
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb

from bathos.catalog import read_runs, write_run
from bathos.index import connect_read
from bathos.runlog.emit import current_mode, emit_event, unit_of_work
from bathos.schema import Run
from bathos.telemetry import event

SACCT_NO_RECORD_REAP_H = 336  # 14 days


def _write_reap_ledger_entry(
    catalog_dir: Path, run_id: str, project_slug: str, record: dict
) -> None:
    """Write a reap ledger entry atomically.

    Args:
        catalog_dir: Catalog directory path
        run_id: Run ID (e.g., "run_test", already includes "run_" prefix)
        project_slug: Project slug
        record: Reap record dict with keys: reaped_at, reason, window_h, prior_status
    """
    ledger_dir = catalog_dir / "reaped" / project_slug
    ledger_dir.mkdir(parents=True, exist_ok=True)

    ledger_path = ledger_dir / f"{run_id}.json"
    tmp_path = ledger_path.with_suffix(".json.tmp")

    # Write to temp file first
    with open(tmp_path, "w") as f:
        json.dump(record, f)

    # Atomic rename
    os.replace(tmp_path, ledger_path)


def read_reap_ledger(catalog_dir: Path) -> dict[str, dict]:
    """Read all reap ledger entries.

    Args:
        catalog_dir: Catalog directory path

    Returns:
        Dictionary mapping run_id to reap record
    """
    ledger = {}
    reaped_dir = catalog_dir / "reaped"

    if not reaped_dir.exists():
        return ledger

    for project_dir in reaped_dir.iterdir():
        if not project_dir.is_dir():
            continue
        # Skip the reverted subdirectory
        if project_dir.name == "reverted":
            continue
        for ledger_file in project_dir.glob("*.json"):
            # Skip reverted files
            if ledger_file.parent.name == "reverted":
                continue
            try:
                with open(ledger_file) as f:
                    record = json.load(f)
                run_id = ledger_file.stem  # e.g., "run_test"
                ledger[run_id] = record
            except (OSError, json.JSONDecodeError):
                pass

    return ledger


class ReapError(Exception):
    """Error during reap operation."""
    pass


@dataclass
class ReapStats:
    """Statistics from a reap operation."""
    total_candidates: int
    reaped_count: int
    skipped_count: int


_SLURM_NONTERMINAL_STATES = frozenset(
    {
        "RUNNING",
        "PENDING",
        "CONFIGURING",
        "RESIZING",
        "COMPLETING",
        "SUSPENDED",
        "REQUEUED",
        "STAGE_OUT",
    }
)
_GENERIC_LAUNCHERS = frozenset(
    {
        "python",
        "python3",
        "uv",
        "run",
        "bash",
        "sh",
        "zsh",
        "env",
        "timeout",
        "nice",
        "nohup",
        "setsid",
    }
)


def _is_slurm_nonterminal(state: str | None) -> bool:
    if not state:
        return False
    base = state.strip().split()[0].rstrip("+").upper()
    return base in _SLURM_NONTERMINAL_STATES


def _normalize_utc(ts: object) -> datetime | None:
    if isinstance(ts, str):
        cleaned = ts.strip()
        if not cleaned:
            return None
        try:
            ts = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(ts, datetime):
        return None
    if ts.tzinfo is None:
        return ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC)


def _is_generic_launcher(exe: str) -> bool:
    base = Path(exe.replace("\\", "/")).name.lower()
    return base in _GENERIC_LAUNCHERS or base.startswith(("python3.", "python2."))


def _query_slurm_job_details(job_id: str) -> tuple[str | None, str | None, str | None]:
    """Query SLURM job state and exit code using sacct -X.

    Args:
        job_id: SLURM job ID

    Returns:
        Tuple of (state, exit_code, error_or_none) where:
        - state is the job state (e.g., "RUNNING", "COMPLETED") or None if not found
        - exit_code is the sacct ExitCode string (e.g., "0:15") if available, else None
        - error_or_none is None if successful, or an error type if the query failed
    """
    try:
        result = subprocess.run(
            [
                "sacct",
                "-X",
                "-j",
                job_id,
                "--format=State,ExitCode",
                "--parsable2",
                "--noheader",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode != 0:
            # Non-zero exit = error
            return None, None, "sacct_error"
        lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        if not lines:
            # Empty output = no record found
            return None, None, "sacct_no_record"
        parsed: list[tuple[str, str | None]] = []
        for row_line in lines:
            if "|" in row_line:
                parts = [p.strip() for p in row_line.split("|")]
                st = parts[0]
                ec = parts[1] if len(parts) > 1 and parts[1] else None
            else:
                st = row_line
                ec = None
            if st:
                parsed.append((st, ec))
        if not parsed:
            return None, None, "sacct_no_record"
        # Array jobs stay active if ANY task row is in an active/non-terminal state.
        for st, ec in parsed:
            if _is_slurm_nonterminal(st):
                return st, ec, None
        # All rows terminal: prefer any non-COMPLETED terminal row so failures aren't masked.
        for st, ec in parsed:
            if st.strip().split()[0].rstrip("+").upper() != "COMPLETED":
                return st, ec, None
        return parsed[0][0], parsed[0][1], None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None, None, "sacct_error"


def _query_slurm_job(job_id: str) -> tuple[str | None, str | None]:
    """Query SLURM job state using sacct -X.

    Args:
        job_id: SLURM job ID

    Returns:
        Tuple of (state, error_or_none) where:
        - state is the job state (e.g., "RUNNING", "COMPLETED") or None if not found
        - error_or_none is None if successful, or an error type if the query failed
    """
    state, _exit_code, error = _query_slurm_job_details(job_id)
    return state, error


_ORIG_QUERY_SLURM_JOB = _query_slurm_job


def _format_sacct_reason(state: str, exit_code: str | None) -> str:
    """Format a sacct terminal state (and optional ExitCode) as a reap reason."""
    if exit_code:
        return f"sacct:{state} (exit={exit_code})"
    return f"sacct:{state}"


def _parse_ps_line(line: str) -> tuple[int | None, list[str]]:
    """Parse `(pid, command_tokens)` from one `ps aux` line."""
    parts = line.split(None, 10)
    if len(parts) >= 11 and parts[1].isdigit():
        pid = int(parts[1])
        cmd_str = parts[10]
    elif len(parts) >= 2 and parts[0].isdigit():
        pid = int(parts[0])
        cmd_str = line.split(None, 1)[1]
    else:
        return None, []
    try:
        tokens = shlex.split(cmd_str)
    except ValueError:
        tokens = cmd_str.split()
    return pid, tokens


def _token_matches_cmd(tok: str, cmd_tokens: list[str], cmd_bases: list[str]) -> bool:
    if tok in cmd_tokens:
        return True
    norm = tok.replace("\\", "/")
    if "/" in norm:
        return any(ct.replace("\\", "/").endswith("/" + norm) for ct in cmd_tokens)
    return Path(norm).name in cmd_bases


def _same_host(a: str | None, b: str | None) -> bool:
    """Compare hostnames by lowercased shortname (e.g. 'titanix' == 'titanix.mit.edu')."""
    if not a or not b:
        return False
    na = a.strip().split(".")[0].lower()
    nb = b.strip().split(".")[0].lower()
    return bool(na) and na == nb


def _is_live_process_on_host(hostname: str, argv: list[str]) -> bool:
    """Check if a live process matching argv exists on this host.

    Args:
        hostname: Recorded hostname from run
        argv: Recorded argv

    Returns:
        True if a matching live process exists
    """
    this_host = socket.gethostname()
    if not _same_host(hostname, this_host):
        return False

    if not argv:
        return False

    # Require at least one non-generic identifying token (script path, specific binary,
    # or target argument) so bare launcher substrings like "python"/"uv" never match.
    identifying = [
        tok for tok in argv if tok and not tok.startswith("-") and not _is_generic_launcher(tok)
    ]
    if not identifying:
        return False

    my_pid = os.getpid()
    try:
        result = subprocess.run(
            ["ps", "aux"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        for line in result.stdout.splitlines():
            if not line.strip() or "ps aux" in line:
                continue
            pid, cmd_tokens = _parse_ps_line(line)
            if not cmd_tokens or pid == my_pid:
                continue
            cmd_bases = [Path(ct.replace("\\", "/")).name for ct in cmd_tokens]
            if all(_token_matches_cmd(tok, cmd_tokens, cmd_bases) for tok in identifying):
                return True
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    return False


def reap_runs(
    catalog_dir: Path,
    older_than_h: float = 24,
    dry_run: bool = True,  # noqa: ARG001 - writes are gated by apply; dry_run is kept for CLI/MCP signature
    apply: bool = False,
    revert: bool = False,
    revert_ids: list[str] | None = None,
    *,
    cwd: Path | None = None,
    reconcile_warm: bool = True,
    slurm_only: bool = False,
    include_remote: bool = False,
) -> tuple[list[Run], list[tuple[Run, str]]]:
    """Reap orphaned runs by marking them abandoned.

    Args:
        catalog_dir: Catalog directory path
        older_than_h: Minimum age in hours to reap (default 24)
        dry_run: Writes are gated by apply; dry_run is kept for CLI/MCP signature compatibility
        apply: If True, actually write reaped runs
        revert: If True, restore prior_status instead of marking abandoned
        revert_ids: List of run IDs to revert
        cwd: Working directory used to resolve the runlog project/root for
            run.reaped / run.reap_reverted when the flag is on. Defaults to
            `Path.cwd()` (via `resolve_log_root`) -- same default every other
            local command uses.
        reconcile_warm: Legacy-only (flag off): whether to force-rebuild the
            warm tier and merge ledger entries after writing reaped/reverted
            runs (spec Migration step 1: "with its warm-tier reconciliation
            disabled ... so the reap writes only its cool fragment rewrite
            and ledger JSON"). Default True (today's behaviour, unchanged for
            every existing caller); `bth migrate --to-log` step 1 is the only
            caller passing `False`. Has no effect when the flag is on: the
            flag-on path never touches the warm tier at all.
        slurm_only: If True, only treat SLURM runs whose `sacct` query confirms
            a terminal state as reap candidates (safe mode for auto-reaping).

    Returns:
        (candidates_list, skipped_list) where skipped_list has (run, skip_reason) tuples

    One call is one unit of work (Mode section): the flag is fixed for the
    whole reap pass, so every run.reaped / run.reap_reverted event emitted
    below (there may be many, one per candidate) shares a single resolution.

    Candidate discovery (spec delivery step 4, "the reaper on folded
    status"): with the flag ON, candidates (and, for `revert`, each run's
    prior reap-ledger record) are read from the folded `runs` table via
    `bathos.index.connect_read` -- never from cool fragments, which a flag-on
    `bth run` never writes. With the flag OFF, this is unchanged: cool
    fragments via `bathos.catalog.read_runs` and the JSON ledger via
    `read_reap_ledger`.
    """
    # Enforce floor (before the writers lock -- a floor violation is not a write).
    if apply and older_than_h < 24:
        raise ReapError(f"Floor: --older-than-h must be >= 24 (given {older_than_h})")

    with unit_of_work(catalog_dir):
        return _reap_runs_impl(
            catalog_dir,
            older_than_h=older_than_h,
            apply=apply,
            revert=revert,
            revert_ids=revert_ids,
            cwd=cwd,
            reconcile_warm=reconcile_warm,
            slurm_only=slurm_only,
            include_remote=include_remote,
        )


def _read_all_runs_from_index(catalog_dir: Path) -> list[Run]:
    """Every run, as folded by the index (spec "the reaper on folded
    status") -- the flag-on equivalent of `bathos.catalog.read_runs`, which
    reads cool fragments a flag-on `bth run` never writes.

    Column-name-keyed via `bathos.query._run_from_index_row` (the same
    helper `query.py`'s own index-backed reads use), not the legacy warm
    tier's positional unpacking -- `bathos.index.RUNS_DDL`'s column order is
    not the legacy warm table's.
    """
    from bathos.query import _run_from_index_row

    con = connect_read(catalog_dir)
    try:
        cur = con.execute("SELECT * FROM runs")
        col_names = [d[0] for d in cur.description]
        rows = cur.fetchall()
    finally:
        con.close()
    return [_run_from_index_row(dict(zip(col_names, r, strict=True))) for r in rows]


def _ledger_from_folded_runs(all_runs: list[Run]) -> dict[str, dict]:
    """The flag-on equivalent of `read_reap_ledger`: a flag-on reap/revert
    never writes `catalog/reaped/<slug>/<run_id>.json` at all (spec: the
    `run.reaped` event "replaces ... the ledger-JSON write"), so the ledger
    record for a revert must come from the run's own folded
    `metadata.reaped` (`fold_run`'s "metadata.reaped: the latest not-
    cancelled ledger-shaped abandoned claim") instead of a file on disk.
    """
    ledger: dict[str, dict] = {}
    for run in all_runs:
        try:
            metadata = json.loads(run.metadata or "{}")
        except (json.JSONDecodeError, TypeError, ValueError):
            metadata = {}
        reaped = metadata.get("reaped") if isinstance(metadata, dict) else None
        if reaped:
            ledger[run.id] = reaped
    return ledger


def _classify_reap_candidates(
    all_runs: list[Run],
    *,
    now: datetime,
    older_than_h: float,
    slurm_only: bool = False,
    include_remote: bool = False,
) -> tuple[list[Run], list[tuple[Run, str]], dict[str, str], dict[str, tuple[str, str | None]]]:
    """Classify running runs into reap candidates vs skipped, capturing sacct exit reasons."""
    now_utc = _normalize_utc(now) or datetime.now(UTC)
    cutoff_time = now_utc - timedelta(hours=older_than_h)
    local_host = socket.gethostname()
    candidates: list[Run] = []
    skipped: list[tuple[Run, str]] = []
    candidate_reasons: dict[str, str] = {}
    candidate_sacct: dict[str, tuple[str, str | None]] = {}

    for run in all_runs:
        if run.status != "running":
            skipped.append((run, "not_running"))
            continue

        run_ts = _normalize_utc(run.timestamp)
        if run_ts is None:
            skipped.append((run, "missing_timestamp"))
            continue

        if run_ts >= cutoff_time:
            skipped.append((run, "too_recent"))
            continue

        if run.slurm_job_id:
            if _query_slurm_job is _ORIG_QUERY_SLURM_JOB:
                state, slurm_exit_code, error = _query_slurm_job_details(run.slurm_job_id)
            else:
                state, error = _query_slurm_job(run.slurm_job_id)
                slurm_exit_code = None

            if error:
                if error == "sacct_error":
                    skipped.append((run, "sacct_error"))
                elif error == "sacct_no_record":
                    if slurm_only:
                        skipped.append((run, "auto_reap_requires_sacct_terminal"))
                    else:
                        age_hours = (now_utc - run_ts).total_seconds() / 3600
                        if age_hours >= SACCT_NO_RECORD_REAP_H:
                            candidates.append(run)
                            candidate_reasons[run.id] = "sacct_no_record"
                        else:
                            skipped.append((run, "sacct_no_record"))
            elif _is_slurm_nonterminal(state):
                skipped.append((run, "slurm_nonterminal"))
            else:
                assert state is not None
                candidates.append(run)
                candidate_reasons[run.id] = _format_sacct_reason(state, slurm_exit_code)
                candidate_sacct[run.id] = (state, slurm_exit_code)
        else:
            is_imported = bool(getattr(run, "imported_from_host", None))
            host_norm = (run.hostname or "").strip().lower()
            # Treat explicit loopback placeholders (used by legacy test fixtures) as
            # local unless the run carries an import receipt (`imported_from_host`).
            is_remote_host = is_imported or (
                bool(host_norm)
                and host_norm not in ("localhost", "localhost.localdomain")
                and not _same_host(run.hostname, local_host)
            )
            if slurm_only:
                skipped.append((run, "auto_reap_requires_sacct_terminal"))
            elif is_remote_host and not include_remote:
                skipped.append((run, "remote_host_skipped"))
            elif not is_remote_host and _is_live_process_on_host(run.hostname, run.argv):
                skipped.append((run, "same_host_live_process"))
            else:
                candidates.append(run)
                candidate_reasons[run.id] = "orphan_window_exceeded"

    return candidates, skipped, candidate_reasons, candidate_sacct


def _reap_runs_impl(
    catalog_dir: Path,
    *,
    older_than_h: float,
    apply: bool,
    revert: bool,
    revert_ids: list[str] | None,
    cwd: Path | None,
    reconcile_warm: bool = True,
    slurm_only: bool = False,
    include_remote: bool = False,
) -> tuple[list[Run], list[tuple[Run, str]]]:
    flag_on = current_mode()

    if flag_on:
        all_runs = _read_all_runs_from_index(catalog_dir)
        ledger = _ledger_from_folded_runs(all_runs) if revert else {}
    else:
        all_runs = read_runs(catalog_dir)
        ledger = read_reap_ledger(catalog_dir) if revert else {}

    now = datetime.now(UTC)
    candidates: list[Run] = []
    skipped: list[tuple[Run, str]] = []
    candidate_reasons: dict[str, str] = {}
    candidate_sacct: dict[str, tuple[str, str | None]] = {}

    if revert:
        for run in all_runs:
            if revert_ids and run.id in revert_ids:
                if run.id not in ledger:
                    skipped.append((run, "no_ledger_entry"))
                    continue

                ledger_record = ledger[run.id]
                prior_status = ledger_record.get("prior_status")

                if prior_status and run.status == "abandoned":
                    run.status = prior_status

                    if apply:
                        if flag_on:
                            # run.reap_reverted (AC-25): the event replaces BOTH the
                            # write_run status rewrite and the ledger-JSON move.
                            emit_event(
                                kind="run.reap_reverted",
                                entity=[run.id],
                                data=ledger_record,
                                cwd=cwd,
                            )
                        else:
                            write_run(run, catalog_dir)
                            ledger_dir = catalog_dir / "reaped" / run.project_slug
                            ledger_path = ledger_dir / f"{run.id}.json"
                            reverted_dir = ledger_dir / "reverted"
                            reverted_dir.mkdir(parents=True, exist_ok=True)
                            ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
                            reverted_path = reverted_dir / f"{run.id}.{ts}.json"
                            os.replace(ledger_path, reverted_path)

                    candidates.append(run)
    else:
        candidates, skipped, candidate_reasons, candidate_sacct = _classify_reap_candidates(
            all_runs,
            now=now,
            older_than_h=older_than_h,
            slurm_only=slurm_only,
            include_remote=include_remote,
        )

    # Apply reaping if requested
    if apply:
        if revert:
            # Revert path: reconcile warm tier after rewrites (legacy only --
            # the flag-on path above already emitted events, no warm rebuild).
            if not flag_on and reconcile_warm:
                reconcile_warm_tier(catalog_dir)
        else:
            # Write reaped runs and ledger entries
            for run in candidates:
                reason = candidate_reasons.get(run.id, "orphan_window_exceeded")

                # Mark as abandoned
                run.status = "abandoned"

                reaped_at = datetime.now(UTC).isoformat()

                ledger_record: dict = {
                    "run_id": run.id,
                    "project_slug": run.project_slug,
                    "reaped_at": reaped_at,
                    "reason": reason,
                    "window_h": older_than_h,
                    "prior_status": "running",
                }
                if run.id in candidate_sacct:
                    sacct_state, sacct_exit_code = candidate_sacct[run.id]
                    ledger_record["sacct_state"] = sacct_state
                    if sacct_exit_code is not None:
                        ledger_record["sacct_exit_code"] = sacct_exit_code

                if flag_on:
                    # run.reaped (AC-25): replaces the write_run status rewrite,
                    # the ledger-JSON write, AND the warm-tier reconcile below.
                    emit_event(kind="run.reaped", entity=[run.id], data=ledger_record, cwd=cwd)
                else:
                    # Write back (atomic) — BEFORE ledger entry
                    write_run(run, catalog_dir)
                    # Write ledger entry after successful write
                    _write_reap_ledger_entry(catalog_dir, run.id, run.project_slug, ledger_record)

                event("catalog.reap_run", run_id=run.id, reason=reason)

            # Reconcile warm tier after writing all reaped runs (legacy only).
            if not flag_on and reconcile_warm:
                reconcile_warm_tier(catalog_dir)

    return candidates, skipped


def find_stale_running_runs(
    catalog_dir: Path,
    *,
    older_than_h: float = 24,
    slurm_only: bool = False,
    include_remote: bool = False,
) -> list[dict]:
    """Read-only helper returning stale `status='running'` runs eligible for reaping.

    Includes SLURM `sacct` state/exit-code details when `slurm_job_id` is present
    (#2238). Never mutates fragments, ledger files, or event logs.
    """
    from bathos.runlog.mode import is_log_mode

    all_runs = (
        _read_all_runs_from_index(catalog_dir)
        if is_log_mode(catalog_dir)
        else read_runs(catalog_dir)
    )
    now = datetime.now(UTC)
    candidates, _skipped, candidate_reasons, candidate_sacct = _classify_reap_candidates(
        all_runs,
        now=now,
        older_than_h=older_than_h,
        slurm_only=slurm_only,
        include_remote=include_remote,
    )
    results: list[dict] = []
    for run in candidates:
        sacct_state, sacct_exit_code = candidate_sacct.get(run.id, (None, None))
        results.append(
            {
                "run_id": run.id,
                "project_slug": run.project_slug,
                "slurm_job_id": run.slurm_job_id,
                "age_hours": (now - run.timestamp).total_seconds() / 3600,
                "reason": candidate_reasons.get(run.id, "orphan_window_exceeded"),
                "sacct_state": sacct_state,
                "sacct_exit_code": sacct_exit_code,
            }
        )
    return results


def auto_reap_stale_runs(
    catalog_dir: Path,
    *,
    older_than_h: float = 24,
    apply: bool = False,
    slurm_only: bool = True,
    include_remote: bool = False,
    cwd: Path | None = None,
    reconcile_warm: bool = True,
) -> tuple[list[Run], list[tuple[Run, str]]]:
    """Safe opt-in auto-reap entry point (#2238).

    Defaults to `apply=False` (dry-run) and `slurm_only=True` so automated
    callers only reap runs whose SLURM job is confirmed terminal by `sacct`
    (never `sacct_error`, `sacct_no_record`, or unverified remote-host runs).
    """
    return reap_runs(
        catalog_dir,
        older_than_h=older_than_h,
        dry_run=not apply,
        apply=apply,
        cwd=cwd,
        reconcile_warm=reconcile_warm,
        slurm_only=slurm_only,
        include_remote=include_remote,
    )


def reconcile_warm_tier(catalog_dir: Path) -> None:
    """Reconcile warm tier with cool tier after reaping.

    Backs up and force-rebuilds the warm tier from cool fragments,
    then merges reaped ledger entries into metadata.

    Args:
        catalog_dir: Catalog directory path
    """
    from bathos.compact import _backup_warm_db, compact
    from bathos.runlog.mode import is_log_mode

    # Log mode: `reap_runs` already emitted run.reaped / run.reap_reverted, which
    # `bth compact` folds into index.db. `bathos.db` is the frozen pre-cutover
    # copy (it still exists, so the guard below does not fire) and compact
    # rejects force_rebuild there -- after the events were written.
    if is_log_mode(catalog_dir):
        return

    db_path = catalog_dir / "bathos.db"
    if not db_path.exists():
        return

    # Backup before rebuild
    _backup_warm_db(db_path)

    # Force rebuild (will remove old DB and rebuild from scratch)
    compact(catalog_dir, force_rebuild=True)

    # After rebuild, merge reaped ledger entries into warm metadata
    ledger = read_reap_ledger(catalog_dir)

    if not ledger:
        return

    # Update warm tier metadata with reaped info
    conn = duckdb.connect(str(db_path))
    try:
        for run_id, ledger_record in ledger.items():
            # Get current metadata or empty object
            result = conn.execute(
                "SELECT metadata FROM runs WHERE id = ?", [run_id]
            ).fetchone()
            if result:
                current_metadata = result[0]
                try:
                    metadata = json.loads(current_metadata or "{}")
                except (json.JSONDecodeError, ValueError):
                    metadata = {}

                # Merge reaped info
                metadata["reaped"] = ledger_record

                # Update the record
                conn.execute(
                    "UPDATE runs SET metadata = ? WHERE id = ?",
                    [json.dumps(metadata), run_id],
                )
        conn.commit()
    finally:
        conn.close()
