"""Session-start current beliefs cache generator.

Spec: 261106 §1.2 Invariant I4, §1.4.C.6, §4.5, AC-P2-07, AC-P2-08, AC-P2-09.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bathos.version_resolver import load_instrument_registries
from bathos.workspace import resolve_workspace

logger = logging.getLogger(__name__)


def _relative_time_str(iso_timestamp: str | None) -> str:
    """Format an ISO timestamp into a relative time string, e.g. '14d ago'."""
    if not iso_timestamp:
        return "recently"
    try:
        dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
        now = datetime.now(UTC)
        diff = now - dt
        if diff.days > 0:
            return f"{diff.days}d ago"
        hours = diff.seconds // 3600
        if hours > 0:
            return f"{hours}h ago"
        minutes = diff.seconds // 60
        return f"{max(1, minutes)}m ago"
    except Exception:
        return "recently"


def _format_defect_alerts(registries: dict[str, Any]) -> list[str]:
    """Generate Active Defect Alert lines (! ALERT: ...)."""
    lines: list[str] = []
    for pkg, reg in sorted(registries.items()):
        for entry in getattr(reg, "entries", []):
            if getattr(entry, "kind", "defect") != "defect":
                continue
            severity = getattr(entry, "severity", "void")
            range_parts = []
            for r in getattr(entry, "ranges", []):
                if getattr(r, "fixed", None):
                    range_parts.append(f"<= {r.fixed}")
                elif getattr(r, "introduced", None):
                    range_parts.append(f">= {r.introduced}")
            range_str = " ".join(range_parts) if range_parts else "range"
            pred_str = ""
            predicate = getattr(entry, "predicate", None)
            if predicate and getattr(predicate, "condition", None):
                pred_str = f" ({predicate.condition})"
            lines.append(f"! ALERT: {pkg} {range_str} {severity} range active{pred_str}")
    return lines


def _format_findings(
    catalog_dir: Path | str | None, main_root: Path | None
) -> tuple[list[str], list[str], list[str]]:
    """Format findings into (needs_recheck, settled, open) line buckets."""
    needs_recheck: list[str] = []
    settled: list[str] = []
    open_items: list[str] = []

    try:
        from bathos.findings import list_findings

        findings = list_findings(
            catalog_dir=catalog_dir, workspace_root=main_root, include_superseded=False
        )
    except (ImportError, Exception) as exc:
        logger.debug("Findings list unavailable: %s", exc)
        return needs_recheck, settled, open_items

    for f in findings:
        status = getattr(f, "active_status", "") or getattr(f, "status", "")
        tier = getattr(f, "evidence_tier", "")
        # Exclude void, superseded, and spike items (AC-P2-09)
        if status in ("void", "superseded") or tier == "spike":
            continue

        slug = getattr(f, "question_slug", "")
        answer = getattr(f, "answer", "")
        ts = getattr(f, "declared_at", None) or getattr(f, "last_reviewed", None)
        age_str = _relative_time_str(ts)

        if status == "needs-recheck":
            reason = getattr(f, "recheck_reason", None) or "all supporting runs hidden"
            needs_recheck.append(f"? NEEDS RECHECK: '{slug}' ({reason})")
        elif status == "settled":
            settled.append(f"* SETTLED: '{slug}' -> {answer} [{tier}, {age_str}]")
        elif status in ("open", "tentative"):
            open_items.append(f"o OPEN: '{slug}' [{status}, {age_str}]")

    return needs_recheck, settled, open_items


def _pack_within_budget(
    alerts: list[str],
    needs_recheck: list[str],
    settled: list[str],
    open_items: list[str],
    byte_cap: int = 2048,
) -> str:
    """Pack items into lines preserving priority order within byte_cap budget."""
    chosen_lines: list[str] = []
    current_bytes = 0

    for group in (alerts, needs_recheck, settled, open_items):
        for line in group:
            encoded_len = len(line.encode("utf-8"))
            extra = 1 if chosen_lines else 0
            if current_bytes + extra + encoded_len > byte_cap:
                return "\n".join(chosen_lines)
            chosen_lines.append(line)
            current_bytes += extra + encoded_len

    return "\n".join(chosen_lines)


def generate_session_start_cache(
    catalog_dir: Path | str | None = None,
    workspace_root: Path | None = None,
    byte_cap: int = 2048,
) -> dict[str, Any]:
    """Generate and write <main_root>/.bth/cache/session_start.json within byte_cap budget."""
    if workspace_root is None:
        ws = resolve_workspace()
        workspace_root = ws.fs_root

    main_root = workspace_root.resolve()
    registries = load_instrument_registries(main_root)
    alerts = _format_defect_alerts(registries)
    rechecks, settled, open_items = _format_findings(catalog_dir, main_root)

    summary = _pack_within_budget(alerts, rechecks, settled, open_items, byte_cap=byte_cap)

    cache_dir = main_root / ".bth" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / "session_start.json"

    payload = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": summary,
        }
    }

    tmp_file = cache_dir / f"session_start.json.tmp.{os.getpid()}"
    tmp_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp_file.replace(cache_file)

    return {
        "message": summary,
        "hookSpecificOutput": payload["hookSpecificOutput"],
        "path": str(cache_file),
        "byte_count": len(summary.encode("utf-8")),
    }
