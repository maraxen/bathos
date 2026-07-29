"""Telemetry cutover bridge — legacy bathos JSONL vs cisternal pipeline (M6)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

_ENABLED_VALUES = frozenset({"1", "true", "yes", "all"})


def cisternal_cutover_enabled() -> bool:
    """True when ``CISTERNAL_TELEMETRY`` enables bathos cutover."""
    raw = os.environ.get("CISTERNAL_TELEMETRY", "").strip().lower()
    if not raw:
        return False
    if raw in _ENABLED_VALUES:
        return True
    return raw == "bathos"


def init_server_telemetry(
    level: str | int | None = None,
    log_dir: str | Path | None = None,
    max_bytes: int = 10_485_760,
    backup_count: int = 5,
) -> None:
    """Initialize telemetry for MCP/CLI startup (delegates to bathos.telemetry)."""
    from bathos.telemetry import init_telemetry

    init_telemetry(
        level=level,
        log_dir=log_dir,
        max_bytes=max_bytes,
        backup_count=backup_count,
    )


def init_via_cisternal(
    level: str | int | None = None,
    log_dir: str | Path | None = None,
    max_bytes: int = 10_485_760,
    backup_count: int = 5,
) -> bool:
    """Initialize cisternal pipeline when cutover flag is set."""
    if not cisternal_cutover_enabled():
        return False

    import cisternal
    from bathos.telemetry import _get_default_log_dir, task_id_var

    resolved = Path(log_dir) if log_dir is not None else _get_default_log_dir()
    cisternal.init(
        log_dir=resolved,
        max_bytes=max_bytes,
        backup_count=backup_count,
        heartbeat_interval=30.0,
    )

    import bathos.telemetry as tel

    tel._INITIALIZED = True

    task_id = os.environ.get("BTH_TASK_ID")
    if task_id:
        from cisternal.telemetry.context import task_id_var as cisternal_task_id_var

        task_id_var.set(task_id)
        cisternal_task_id_var.set(task_id)

    return True


def _sync_context_to_cisternal() -> None:
    """Copy bathos contextvars into cisternal before emit (best-effort)."""
    from bathos.telemetry import mcp_request_id_var, run_uuid_var, task_id_var
    from cisternal.telemetry.context import (
        mcp_request_id_var as c_mcp_request_id_var,
        run_uuid_var as c_run_uuid_var,
        task_id_var as c_task_id_var,
    )

    for src, dst in (
        (run_uuid_var, c_run_uuid_var),
        (mcp_request_id_var, c_mcp_request_id_var),
        (task_id_var, c_task_id_var),
    ):
        value = src.get()
        if value is not None:
            dst.set(value)


def emit_via_cisternal(event_name: str, **fields: Any) -> bool:
    """Emit through cisternal when cutover flag is set."""
    if not cisternal_cutover_enabled():
        return False

    import cisternal

    if cisternal.get_pipeline() is None:
        init_via_cisternal()

    _sync_context_to_cisternal()
    cisternal.emit_event(event_name, **fields)
    return True


def span_via_cisternal(
    name: str, **fields: Any
) -> AbstractContextManager[None] | None:
    """Return cisternal span context manager when cutover flag is set."""
    if not cisternal_cutover_enabled():
        return None

    import cisternal

    if cisternal.get_pipeline() is None:
        init_via_cisternal()

    return cisternal.span(name, **fields)
