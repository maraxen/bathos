"""Retention and citation-time cold-archive guard for gitignored outputs (`#3495`).

Decision-relevant outputs often live under gitignored paths (e.g. ``outputs/``).
Without a reference hash pin (``[outputs] roots`` or ``bth outputs adopt``) and
a verified cold/mirror archive (``bth cold``), those outputs are vulnerable to
local cleanup or worktree loss with no recovery path.

This module provides:
- :func:`assess_paths_retention`: pure gitignore + pin/archive check over paths.
- :func:`check_citation_retention`: catalog-backed check for a run at citation time.
- :func:`guard_citation_retention`: warning or strict refusal guard.
"""

from __future__ import annotations

import json
import logging
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bathos.output_tiers.reference import (
    OutputTierError,
    _entries_of,
    legacy_relpath,
    normalize_relpath,
    reference_hashes,
)
from bathos.output_tiers.run_context import _resolve_root, open_run
from bathos.telemetry import event

logger = logging.getLogger(__name__)

UNPINNED_GITIGNORED = "gitignored_no_reference_pin"
UNARCHIVED_GITIGNORED = "gitignored_no_cold_archive"
COLD_COPY_UNVERIFIED = "cold_copy_unverified"
UNARCHIVED_GITIGNORED_OUTPUTS = "unarchived_gitignored_outputs"

_DECISION_STAGES = frozenset({"validation", "production", "confirmatory", "ablation"})


@dataclass(frozen=True)
class RetentionReport:
    """Result of checking a run's outputs for gitignored retention risk."""

    ok: bool
    run_id: str
    decision_relevant: bool
    decision_reasons: tuple[str, ...]
    gitignored_paths: tuple[str, ...]
    unpinned_paths: tuple[str, ...]
    unarchived_paths: tuple[str, ...]
    has_verified_cold_copy: bool
    has_unverified_cold_copy: bool
    flag_codes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-friendly dictionary."""
        return {
            "ok": self.ok,
            "run_id": self.run_id,
            "decision_relevant": self.decision_relevant,
            "decision_reasons": list(self.decision_reasons),
            "gitignored_paths": list(self.gitignored_paths),
            "unpinned_paths": list(self.unpinned_paths),
            "unarchived_paths": list(self.unarchived_paths),
            "has_verified_cold_copy": self.has_verified_cold_copy,
            "has_unverified_cold_copy": self.has_unverified_cold_copy,
            "flag_codes": list(self.flag_codes),
            "warnings": list(self.warnings),
        }


def _gitignored_paths(paths: Iterable[str], root: Path) -> set[str]:
    """Return the subset of *paths* (relative POSIX under *root*) ignored by git."""
    candidates = sorted({p for p in paths if p})
    if not candidates or not root.is_dir():
        return set()
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "--stdin", "--no-index", "-z"],
            input="\0".join(candidates) + "\0",
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return set()
    # git check-ignore exits 0 when >=1 path is ignored, 1 when none are ignored,
    # and >1 (e.g. 128) when not a git repository or fatal error.
    if proc.returncode not in (0, 1):
        return set()
    return {p for p in proc.stdout.split("\0") if p}


def assess_paths_retention(
    output_paths: Iterable[str],
    reference_paths: Iterable[str],
    *,
    worktree_root: Path,
    has_verified_cold_copy: bool,
    has_unverified_cold_copy: bool = False,
    run_id: str = "",
    decision_relevant: bool = True,
    decision_reasons: tuple[str, ...] = (),
) -> RetentionReport:
    """Assess retention risk for *output_paths* against *reference_paths* and cold archive state."""
    norm_outputs: list[str] = []
    seen_out: set[str] = set()
    for raw in output_paths:
        rel = normalize_relpath(raw, worktree_root)
        key = rel if rel is not None else raw
        if key and key not in seen_out:
            seen_out.add(key)
            norm_outputs.append(key)

    norm_refs: set[str] = set()
    for raw in reference_paths:
        rel = normalize_relpath(raw, worktree_root)
        key = rel if rel is not None else raw
        if key:
            norm_refs.add(key)

    all_candidates = sorted(set(norm_outputs) | norm_refs)
    ignored = _gitignored_paths(all_candidates, worktree_root)
    gitignored_list = tuple(p for p in all_candidates if p in ignored)
    unpinned_list = tuple(p for p in gitignored_list if p not in norm_refs)
    unarchived_list = () if has_verified_cold_copy else gitignored_list

    if not decision_relevant or not gitignored_list:
        return RetentionReport(
            ok=True,
            run_id=run_id,
            decision_relevant=decision_relevant,
            decision_reasons=decision_reasons,
            gitignored_paths=gitignored_list,
            unpinned_paths=unpinned_list,
            unarchived_paths=unarchived_list,
            has_verified_cold_copy=has_verified_cold_copy,
            has_unverified_cold_copy=has_unverified_cold_copy,
            flag_codes=(),
            warnings=(),
        )

    flags: list[str] = []
    warnings: list[str] = []
    run_label = f"run {run_id[:8]}" if run_id else "run"

    if unpinned_list:
        flags.append(UNPINNED_GITIGNORED)
        shown = ", ".join(unpinned_list[:5]) + (
            f" (+{len(unpinned_list) - 5} more)" if len(unpinned_list) > 5 else ""
        )
        warnings.append(
            f"retention warning: decision-relevant {run_label} has gitignored output(s) "
            f"without a reference hash pin ({shown}); declare [outputs] roots or run "
            "`bth outputs adopt` before cold archiving."
        )

    if not has_verified_cold_copy:
        flags.append(UNARCHIVED_GITIGNORED)
        if has_unverified_cold_copy:
            flags.append(COLD_COPY_UNVERIFIED)
            warnings.append(
                f"retention warning: decision-relevant {run_label} has gitignored output(s) "
                f"({len(gitignored_list)} path(s)) whose cold/mirror copy is not verified; "
                "run `bth verify --tier outputs` or `bth cold --supersede`."
            )
        else:
            shown = ", ".join(gitignored_list[:5]) + (
                f" (+{len(gitignored_list) - 5} more)" if len(gitignored_list) > 5 else ""
            )
            warnings.append(
                f"retention warning: decision-relevant {run_label} cites gitignored output(s) "
                f"({shown}) with no verified cold archive copy; run `bth cold --run-id "
                f"{run_id or '<run_id>'}` to preserve them."
            )

    return RetentionReport(
        ok=not flags,
        run_id=run_id,
        decision_relevant=decision_relevant,
        decision_reasons=decision_reasons,
        gitignored_paths=gitignored_list,
        unpinned_paths=unpinned_list,
        unarchived_paths=unarchived_list,
        has_verified_cold_copy=has_verified_cold_copy,
        has_unverified_cold_copy=has_unverified_cold_copy,
        flag_codes=tuple(flags),
        warnings=tuple(warnings),
    )


def _infer_decision_relevance(
    con: Any, run_id: str, events: list[dict[str, Any]]
) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    row = con.execute(
        "SELECT stage_name, epistemic_class, outcome, campaign_id FROM runs WHERE id = ?",
        [run_id],
    ).fetchone()
    if row:
        stage_name, epistemic_class, outcome, campaign_id = row
        if stage_name in _DECISION_STAGES:
            reasons.append(f"stage:{stage_name}")
        if epistemic_class == "confirmatory":
            reasons.append("epistemic_class:confirmatory")
        if outcome == "pass":
            reasons.append("outcome:pass")
        if campaign_id:
            reasons.append(f"campaign:{campaign_id}")
    try:
        camp_rows = con.execute(
            "SELECT campaign_id FROM campaign_runs WHERE run_id = ?", [run_id]
        ).fetchall()
        for (cid,) in camp_rows:
            tag = f"campaign:{cid}"
            if cid and tag not in reasons:
                reasons.append(tag)
    except Exception:
        pass
    for ev in events:
        data = ev.get("data") or {}
        if ev.get("kind") == "run.started":
            st = data.get("stage_name")
            if st in _DECISION_STAGES and f"stage:{st}" not in reasons:
                reasons.append(f"stage:{st}")
            ec = data.get("epistemic_class")
            if ec == "confirmatory" and "epistemic_class:confirmatory" not in reasons:
                reasons.append("epistemic_class:confirmatory")
        elif ev.get("kind") == "run.finished":
            oc = data.get("outcome")
            if oc == "pass" and "outcome:pass" not in reasons:
                reasons.append("outcome:pass")
    return bool(reasons), tuple(reasons)


def _collect_run_output_paths(
    con: Any,
    run_id: str,
    events: list[dict[str, Any]],
    worktree_root: Path,
    imported_root: str | None,
    extra_output_paths: Iterable[str] | None,
) -> list[str]:
    paths: list[str] = []
    row = con.execute("SELECT output_paths FROM runs WHERE id = ?", [run_id]).fetchone()
    if row and row[0]:
        raw = row[0]
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except json.JSONDecodeError:
                raw = []
        if isinstance(raw, list):
            paths.extend(str(p) for p in raw if p)
    for ev in events:
        data = ev.get("data") or {}
        if ev.get("kind") == "run.started":
            for p in data.get("output_paths") or []:
                if p:
                    paths.append(str(p))
        root_str = (imported_root if imported_root and ev.get("kind") == "run.imported" else None) or (
            ev.get("worktree_root") or str(worktree_root)
        )
        for entry in _entries_of(data.get("output_metadata")):
            rel = entry.get("relpath") or legacy_relpath(str(entry.get("path", "")), root_str)
            if rel:
                paths.append(str(rel))
    if extra_output_paths is not None:
        paths.extend(str(p) for p in extra_output_paths if p)
    return paths


def _cold_copy_flags(con: Any, run_id: str) -> tuple[bool, bool]:
    """Return ``(has_verified_cold_copy, has_unverified_cold_copy)`` from ``output_copies``."""
    try:
        rows = con.execute(
            "SELECT state FROM output_copies WHERE run_id = ? "
            "AND superseded_at IS NULL AND tier IN ('cold', 'mirror')",
            [run_id],
        ).fetchall()
    except Exception:
        return False, False
    states = [r[0] for r in rows]
    has_verified = any(s == "verified" for s in states)
    has_unverified = (not has_verified) and bool(states)
    return has_verified, has_unverified


def check_citation_retention(
    catalog_dir: Path,
    run_prefix: str,
    *,
    worktree_root: str | None = None,
    output_paths: Iterable[str] | None = None,
    decision_relevant: bool | None = None,
    emit_telemetry: bool = True,
) -> RetentionReport:
    """Check whether a run's gitignored decision-relevant outputs lack a reference or cold pin."""
    from bathos.index import connect_read

    ctx = open_run(catalog_dir, run_prefix, need_root=False)
    try:
        resolved_root, _src, imported_root = _resolve_root(ctx.events, worktree_root)
    except OutputTierError:
        resolved_root = Path(worktree_root) if worktree_root else Path.cwd()
        imported_root = worktree_root

    ref = reference_hashes(ctx.events, imported_root=imported_root)
    con = connect_read(catalog_dir)
    try:
        inferred_relevant, reasons = _infer_decision_relevance(con, ctx.run_id, ctx.events)
        all_paths = _collect_run_output_paths(
            con, ctx.run_id, ctx.events, resolved_root, imported_root, output_paths
        )
        has_verified, has_unverified = _cold_copy_flags(con, ctx.run_id)
    finally:
        con.close()

    if decision_relevant is True:
        effective_relevant = True
        effective_reasons = ("explicit_citation", *reasons)
    elif decision_relevant is False:
        effective_relevant = False
        effective_reasons = reasons
    else:
        effective_relevant = inferred_relevant
        effective_reasons = reasons

    report = assess_paths_retention(
        all_paths,
        ref.keys(),
        worktree_root=resolved_root,
        has_verified_cold_copy=has_verified,
        has_unverified_cold_copy=has_unverified,
        run_id=ctx.run_id,
        decision_relevant=effective_relevant,
        decision_reasons=effective_reasons,
    )
    if emit_telemetry and not report.ok:
        event(
            "output_tiers.retention_warning",
            run_id=ctx.run_id,
            flag_codes=list(report.flag_codes),
            n_gitignored=len(report.gitignored_paths),
            n_unpinned=len(report.unpinned_paths),
        )
    return report


def guard_citation_retention(
    catalog_dir: Path,
    run_prefix: str,
    *,
    worktree_root: str | None = None,
    output_paths: Iterable[str] | None = None,
    decision_relevant: bool | None = True,
    strict: bool = False,
) -> RetentionReport:
    """Citation-time cold-archive guard: warn (or refuse if ``strict=True``) on unarchived gitignored outputs."""
    report = check_citation_retention(
        catalog_dir,
        run_prefix,
        worktree_root=worktree_root,
        output_paths=output_paths,
        decision_relevant=decision_relevant,
    )
    if not report.ok:
        for w in report.warnings:
            logger.warning("%s", w)
        if strict:
            raise OutputTierError(UNARCHIVED_GITIGNORED_OUTPUTS, "; ".join(report.warnings))
    return report
