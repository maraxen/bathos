"""Claim-bound breakdown gate (spec 261026 D3).

A run cited by a claim-bound campaign must have declared `[breakdown]` (or a waiver) for every
worst-case aggregate its verdict reads, in the sidecar that was in force when the run happened,
and a clean non-residual verdict must have emitted the summary columns. Everywhere else the same
findings are advisories (`bathos.sidecar.uncovered_aggregates`, the runner's `_advise`); this
module is the refusal side.

Only the run-time sidecar counts, so a waiver or `[breakdown]` added afterwards never passes:
`resolve_run_sidecar` recovers the sidecar the run recorded (the frozen log-mode declaration, a
live file whose sha256 still matches, or the pinned git ref) and answers `Unresolvable` otherwise.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import duckdb

from bathos.errors import BathosErrorCode
from bathos.sidecar import (
    Sidecar,
    SidecarError,
    effective_label,
    label_names_nonresidual_outcome,
    missing_breakdown_columns,
    parse_sidecar,
    uncovered_aggregate_message,
    uncovered_aggregates,
)


@dataclass(frozen=True)
class Unresolvable:
    """The run-time sidecar cannot be recovered, so the run cannot be verified."""

    reason: str


@dataclass(frozen=True)
class Finding:
    """One run the gate would refuse: its id, the error code, and a human reason."""

    run_id: str
    code: BathosErrorCode
    message: str


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _run_started_declaration(db, run_id: str) -> dict | None:
    """The sidecar declaration frozen in the run's `run.started` event (log mode), or None."""
    from bathos.runlog.fold_campaigns import member_sidecar_declaration
    from bathos.runlog.ingest import _fetch_events_by_entity_key

    try:
        events = _fetch_events_by_entity_key(db, [run_id])
    except Exception:  # noqa: BLE001 -- no events table / unreadable: not log-mode data
        return None
    return member_sidecar_declaration(events)


def _git_show_sidecar(run_id: str, sidecar_path: Path, workspace_root: Path) -> bytes | None:
    """`git show refs/bathos/runs/<run_id>:<path relative to the repo root>`, or None."""
    try:
        top = subprocess.run(
            ["git", "-C", str(workspace_root), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if top.returncode != 0:
            return None
        rel = sidecar_path.resolve().relative_to(Path(top.stdout.strip()).resolve())
        shown = subprocess.run(
            [
                "git",
                "-C",
                top.stdout.strip(),
                "show",
                f"refs/bathos/runs/{run_id}:{rel.as_posix()}",
            ],
            capture_output=True,
            timeout=30,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return shown.stdout if shown.returncode == 0 else None


def resolve_run_sidecar(
    run, workspace_root: Path, *, db=None, log_mode: bool = False
) -> Sidecar | Unresolvable | None:
    """The sidecar in force when `run` happened: a `Sidecar`, `Unresolvable`, or None when the
    run had no sidecar (outside this gate).

    Order: (1) log mode: the frozen `run.started` declaration (append-only, so no sha recheck);
    (2) the live file at `run.sidecar_path` whose sha256 equals `run.sidecar_sha256`; (3) the
    pinned git ref `refs/bathos/runs/<id>` with a matching sha; (4) `Unresolvable`.
    """
    if log_mode and db is not None:
        decl = _run_started_declaration(db, run.id)
        if decl:
            try:
                return Sidecar.from_declaration(decl)
            except SidecarError as e:
                return Unresolvable(f"run.started declaration is unreadable: {e}")
    sidecar_path = getattr(run, "sidecar_path", "") or ""
    if not sidecar_path:
        return None
    recorded_sha = getattr(run, "sidecar_sha256", "") or ""
    if not recorded_sha:
        return Unresolvable("the run recorded no sidecar sha256 to verify against")
    path = Path(sidecar_path)
    if not path.is_absolute():
        path = Path(workspace_root) / path
    try:
        if path.is_file() and _sha256(path.read_bytes()) == recorded_sha:
            return parse_sidecar(path)
    except (OSError, SidecarError):
        pass
    pinned = _git_show_sidecar(run.id, path, Path(workspace_root))
    if pinned is not None and _sha256(pinned) == recorded_sha:
        with tempfile.TemporaryDirectory() as tmp:
            recovered = Path(tmp) / path.name
            recovered.write_bytes(pinned)
            try:
                return parse_sidecar(recovered)
            except SidecarError as e:
                return Unresolvable(f"pinned sidecar does not parse: {e}")
    return Unresolvable(
        f"{sidecar_path} changed since the run (sha256 mismatch) and no pinned copy matches"
    )


def _result_dict(metadata) -> dict:
    try:
        parsed = json.loads(metadata) if metadata else {}
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def breakdown_gate(run, sidecar: Sidecar, *, check_columns: bool = True) -> list[str]:
    """Reasons `run` fails the breakdown gate under its run-time `sidecar` (empty = passes).

    Fails when an aggregate its verdict reads is neither covered by `[breakdown]` nor waived, or
    (with `check_columns`) when the effective label names a non-residual outcome, `[breakdown]`
    is declared, and its summary columns are missing from the run's results. `check_columns` is
    False before a run exists (the `bth run --campaign` gate has no results yet).
    """
    reasons = [uncovered_aggregate_message(u) for u in uncovered_aggregates(sidecar)]
    if check_columns and sidecar.breakdown is not None and sidecar.breakdown.aggregates:
        label = effective_label(run.outcome, getattr(run, "tentative_outcome", None))
        if label_names_nonresidual_outcome(sidecar, label):
            missing = missing_breakdown_columns(sidecar, _result_dict(run.metadata))
            if missing:
                reasons.append(
                    f"outcome {label!r} was reached without the [breakdown] summary columns: "
                    + ", ".join(missing)
                )
    return reasons


def check_runs(
    runs: dict[str, object | None],
    workspace_root: Path,
    *,
    db=None,
    log_mode: bool = False,
    check_columns: bool = True,
) -> list[Finding]:
    """Run the gate over `runs` (id -> row, None for an id that did not resolve). Fails closed:
    an unresolvable member is a `SIDECAR_UNVERIFIABLE` finding, never a pass."""
    findings: list[Finding] = []
    for run_id, run in runs.items():
        if run is None:
            findings.append(
                Finding(
                    run_id,
                    BathosErrorCode.SIDECAR_UNVERIFIABLE,
                    "the run is not in the catalog, so its sidecar cannot be verified",
                )
            )
            continue
        sidecar = resolve_run_sidecar(run, workspace_root, db=db, log_mode=log_mode)
        if sidecar is None:
            continue  # no sidecar: outside this gate
        if isinstance(sidecar, Unresolvable):
            findings.append(Finding(run_id, BathosErrorCode.SIDECAR_UNVERIFIABLE, sidecar.reason))
            continue
        for reason in breakdown_gate(run, sidecar, check_columns=check_columns):
            findings.append(Finding(run_id, BathosErrorCode.BREAKDOWN_MISSING, reason))
    return findings


def format_findings(findings: list[Finding], where: str) -> str:
    lines = [f"{where}: {len(findings)} breakdown finding(s) on claim-bound runs"]
    lines += [f"  {f.run_id[:8]}: {f.message}" for f in findings[:10]]
    if len(findings) > 10:
        lines.append(f"  ... and {len(findings) - 10} more")
    return "\n".join(lines)


def refuse_on_findings(findings: list[Finding], where: str) -> None:
    """Raise `BreakdownRefused` for `findings`; `.code` is BREAKDOWN_MISSING when any run
    failed the check itself, else SIDECAR_UNVERIFIABLE."""
    if not findings:
        return
    from bathos.campaigns import BreakdownRefused

    code = (
        BathosErrorCode.BREAKDOWN_MISSING
        if any(f.code is BathosErrorCode.BREAKDOWN_MISSING for f in findings)
        else BathosErrorCode.SIDECAR_UNVERIFIABLE
    )
    raise BreakdownRefused(code, format_findings(findings, where))


def _run_finished_metadata(db, run_id: str) -> str | None:
    """The result payload (`Run.metadata` JSON) the run recorded in its latest `run.finished`
    event, or None. Log mode only: the folded index keeps `runs.metadata` for reap bookkeeping
    alone, so the script's own results live only in the event."""
    from bathos.runlog.ingest import _fetch_events_by_entity_key

    try:
        events = _fetch_events_by_entity_key(db, [run_id])
    except Exception:  # noqa: BLE001 -- no events table: not log-mode data
        return None
    finished = sorted(
        (e for e in events if e.get("kind") == "run.finished"),
        key=lambda e: (e.get("ts", ""), e.get("eid", "")),
    )
    if not finished:
        return None
    metadata = (finished[-1].get("data") or {}).get("metadata")
    return metadata if isinstance(metadata, str) else None


def fetch_member_runs(
    db, run_ids: list[str], catalog_dir: Path | None, *, cool_runs=None, log_mode: bool = False
) -> dict[str, object | None]:
    """Rows for `run_ids` from the active backend, falling back to cool fragments (members that
    exist only in the cool tier are gated too). An id found nowhere maps to None.

    `cool_runs` lets a caller that already read the cool tier pass it in (one scan per conclude).
    In `log_mode` a row's `metadata` is the result payload from its `run.finished` event, because
    the index's `runs.metadata` does not carry results.
    """
    wanted = list(dict.fromkeys(run_ids))
    found: dict[str, object | None] = {}
    if wanted and db is not None:
        marks = ", ".join("?" for _ in wanted)
        for cols in (
            "id, outcome, tentative_outcome, sidecar_path, sidecar_sha256, metadata",
            "id, outcome, NULL AS tentative_outcome, sidecar_path, sidecar_sha256, metadata",
        ):
            try:
                rows = db.execute(
                    f"SELECT {cols} FROM runs WHERE id IN ({marks})",  # noqa: S608
                    wanted,
                ).fetchall()
            except duckdb.Error:
                continue
            for rid, outcome, tentative, sidecar_path, sha, metadata in rows:
                if log_mode:
                    metadata = _run_finished_metadata(db, rid) or metadata
                found[rid] = SimpleNamespace(
                    id=rid,
                    outcome=outcome,
                    tentative_outcome=tentative,
                    sidecar_path=sidecar_path or "",
                    sidecar_sha256=sha or "",
                    metadata=metadata,
                )
            break
    missing = [rid for rid in wanted if rid not in found]
    if missing and catalog_dir is not None:
        if cool_runs is None:
            from bathos.catalog import read_runs

            cool_runs = read_runs(catalog_dir)
        by_id = {r.id: r for r in cool_runs}
        for rid in missing:
            if rid in by_id:
                found[rid] = by_id[rid]
    return {rid: found.get(rid) for rid in wanted}
