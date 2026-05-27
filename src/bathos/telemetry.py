"""
Bathos telemetry substrate — structured JSONL event logging.

Module provides a zero-dependency structured logging system for audit trails
across all bathos surfaces (runner, mcp, sync, catalog, sidecar, prereg, etc.).

**Fork safety note:** This module forbids multiprocessing with the `fork` start
method. Use `forkserver` or `spawn` only.

Key design decisions:
- D1: stdlib logging + QueueHandler → QueueListener → RotatingFileHandler (zero-dep)
- D4: Contextvars snapshotted in producer thread via QueueHandler.prepare() (F2 fix)
- D6: Per-process file naming (events.<hostname>.<pid>.jsonl) for SLURM-array safety
- D7: JSONL format for grep/jq/duckdb friendliness

Public API:
  init_telemetry(level=None, log_dir=None) -> None
  get_logger(name: str) -> logging.Logger
  event(name: str, **fields) -> None
  span(name: str, **fields) -> ContextManager

Contextvars (set by callers):
  run_uuid_var — set by runner.py at bth run start
  mcp_request_id_var — set by mcp.py per tool invocation
  task_id_var — read from $BTH_TASK_ID at init_telemetry()
"""

from __future__ import annotations

import atexit
import contextlib
import contextvars
import json
import logging
import os
import queue
import socket
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path
from typing import Any

# ─────────────────────────────────────────────────────────────────────────────
# Contextvars — set by callers (runner.py, mcp.py, cli.py)
# ─────────────────────────────────────────────────────────────────────────────

run_uuid_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "bathos.run_uuid", default=None
)
mcp_request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "bathos.mcp_request_id", default=None
)
task_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "bathos.task_id", default=None
)

# ─────────────────────────────────────────────────────────────────────────────
# Module state
# ─────────────────────────────────────────────────────────────────────────────

_INITIALIZED = False
_queue: queue.Queue | None = None  # unbounded; created at init_telemetry
_listener: QueueListener | None = None
_handlers: dict[str, logging.Handler] = {}  # track handlers to avoid duplication
_lazy_init_warning_shown = False

# Default log directory — can be overridden
_DEFAULT_LOG_DIR: Path | None = None


# ─────────────────────────────────────────────────────────────────────────────
# JsonFormatter — serialize LogRecord to JSON
# ─────────────────────────────────────────────────────────────────────────────


class JsonFormatter(logging.Formatter):
    """Formats LogRecord as JSON line.

    Reads envelope fields + captured contextvars + per-event fields from record.__dict__.
    Omits null contextvar values (no 'null' noise).
    """

    def format(self, record: logging.LogRecord) -> str:
        """Convert LogRecord to JSON line."""
        # Envelope: required on every record
        envelope = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(
                timespec="microseconds"
            ),
            "level": record.levelname.lower(),
            "pid": record.process,
            "tid": record.thread,
            "host": socket.gethostname(),
            "surface": _extract_surface(record.name),
            "event": record.name,
            "msg": record.getMessage(),
        }

        # Contextvar fields (from prepare() snapshots) — omit if None
        for var_name in ("run_uuid", "mcp_request_id", "task_id"):
            val = getattr(record, var_name, None)
            if val is not None:
                envelope[var_name] = val

        # Per-event fields (extra kwargs passed to event/span)
        for key in record.__dict__:
            if key not in (
                "name",
                "msg",
                "args",
                "created",
                "filename",
                "funcName",
                "levelname",
                "levelno",
                "lineno",
                "module",
                "msecs",
                "message",
                "pathname",
                "process",
                "processName",
                "relativeCreated",
                "thread",
                "threadName",
                "exc_info",
                "exc_text",
                "stack_info",
                "getMessage",
                "run_uuid",
                "mcp_request_id",
                "task_id",
            ):
                envelope[key] = getattr(record, key)

        # Serialize with fallback for non-serializable values
        try:
            return json.dumps(envelope, default=str)
        except (TypeError, ValueError):
            # If str() fallback fails, emit a warning and retry
            try:
                result = json.dumps(envelope, default=repr)
                # Emit warning about serialization fallback to stderr (not via event() to avoid recursion)
                print(f"telemetry: serialise_error for field in event {getattr(record, 'event', '?')}", file=sys.stderr)
                return result
            except Exception as e:
                # Last resort: encode as string representation
                return json.dumps(
                    {**envelope, "error": f"JSON encode failed: {e}"}, default=str
                )


def _extract_surface(logger_name: str) -> str:
    """Extract surface prefix (part before first dot in event name)."""
    # For log records, the logger name is the event name
    if "." in logger_name:
        return logger_name.split(".", 1)[0]
    return "unknown"


# ─────────────────────────────────────────────────────────────────────────────
# Custom QueueHandler with contextvar snapshot
# ─────────────────────────────────────────────────────────────────────────────


class ContextVarCaptureQueueHandler(QueueHandler):
    """QueueHandler that snapshots contextvars before enqueuing.

    This is the F2 fix: capture contextvar values on the PRODUCER thread
    before the record is enqueued, then read from record.__dict__ on the
    LISTENER thread (which has a different context).
    """

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        """Snapshot contextvars onto record before enqueuing."""
        record = super().prepare(record)
        # Capture contextvar values on producer thread
        record.run_uuid = run_uuid_var.get(None)
        record.mcp_request_id = mcp_request_id_var.get(None)
        record.task_id = task_id_var.get(None)
        return record


# ─────────────────────────────────────────────────────────────────────────────
# Public API
# ─────────────────────────────────────────────────────────────────────────────


def init_telemetry(
    level: str | int | None = None, log_dir: str | Path | None = None, max_bytes: int = 10485760, backup_count: int = 5
) -> None:
    """Initialize telemetry pipeline (idempotent).

    Args:
        level: logging level (e.g., 'DEBUG', 'INFO', logging.DEBUG). Defaults to INFO.
        log_dir: directory for JSONL files. Defaults to ~/.bth/catalog/logs/ (or $BTH_LOG_DIR).
        max_bytes: max bytes per JSONL file before rotation (default 10 MB).
        backup_count: number of backup files to keep (default 5).
    """
    global _INITIALIZED, _listener, _queue, _handlers, _lazy_init_warning_shown

    if _INITIALIZED:
        return

    # Initialize queue and handler tracking.
    _queue = queue.Queue(-1)  # unbounded
    _handlers.clear()

    # Resolve log_dir
    if log_dir is None:
        log_dir = _get_default_log_dir()
    log_dir = Path(log_dir)

    # Ensure log_dir exists
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        # Fall back to temp dir if not writable
        import tempfile

        log_dir = Path(tempfile.gettempdir()) / "bathos_logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        # Warn once to stderr (not stdout — FastMCP uses stdout)
        if not _lazy_init_warning_shown:
            print(
                f"WARNING: BTH_LOG_DIR not writable, using {log_dir}",
                file=sys.stderr,
            )
            _lazy_init_warning_shown = True

    # Resolve level
    if level is None:
        level_str = os.environ.get("BTH_LOG_LEVEL", "INFO").upper()
        level = getattr(logging, level_str, logging.INFO)
    elif isinstance(level, str):
        level = getattr(logging, level.upper(), logging.INFO)

    # Create QueueHandler + QueueListener
    queue_handler = ContextVarCaptureQueueHandler(_queue)

    # File handler targeting events.<hostname>.<pid>.jsonl
    hostname = socket.gethostname()
    pid = os.getpid()
    log_file = log_dir / f"events.{hostname}.{pid}.jsonl"

    file_handler = RotatingFileHandler(
        log_file, maxBytes=max_bytes, backupCount=backup_count
    )
    file_handler.setFormatter(JsonFormatter())

    # Listener thread (daemon)
    listener = QueueListener(_queue, file_handler, respect_handler_level=False)
    listener.start()

    # Register cleanup
    atexit.register(_shutdown)

    # Store for later shutdown
    _listener = listener
    _handlers["queue"] = queue_handler
    _handlers["file"] = file_handler

    # Attach queue handler to root logger to catch all logs
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.addHandler(queue_handler)

    # Initialize task_id_var from environment
    task_id = os.environ.get("BTH_TASK_ID")
    if task_id:
        task_id_var.set(task_id)

    _INITIALIZED = True


def get_logger(name: str) -> logging.Logger:
    """Return a logger named 'bathos.<name>' with propagate=False.

    Args:
        name: e.g., 'runner', 'mcp', 'sync'.

    Returns:
        logging.Logger configured to use the telemetry pipeline.
    """
    logger = logging.getLogger(f"bathos.{name}")
    logger.propagate = False
    # Set level to match root logger
    logger.setLevel(logging.DEBUG)

    # If telemetry is initialized, ensure the QueueHandler is on this logger
    if _INITIALIZED and _queue is not None:
        # Check if it already has a QueueHandler
        has_queue_handler = any(isinstance(h, ContextVarCaptureQueueHandler) for h in logger.handlers)
        if not has_queue_handler:
            queue_handler = ContextVarCaptureQueueHandler(_queue)
            logger.addHandler(queue_handler)

    return logger


def _wait_for_queue_drain(timeout_s: float = 2.0) -> None:
    """Wait for all queued events to be processed (helper for testing)."""
    if _queue is None or not _INITIALIZED:
        return

    start = time.monotonic()
    while not _queue.empty():
        if time.monotonic() - start > timeout_s:
            break
        time.sleep(0.01)


def event(name: str, **fields) -> None:
    """Emit a structured event with optional fields.

    If telemetry not yet initialized, lazy-inits with defaults and warns once to stderr.

    Args:
        name: event name (e.g., 'run.start', 'catalog.write'). Becomes the LogRecord name.
        **fields: arbitrary key-value pairs to include in the JSONL record.
    """
    global _lazy_init_warning_shown

    if not _INITIALIZED:
        # Lazy init with defaults
        if not _lazy_init_warning_shown:
            print(
                "WARNING: event() called before init_telemetry(); lazy-initializing with defaults",
                file=sys.stderr,
            )
            _lazy_init_warning_shown = True
        init_telemetry()

    # Use the name directly as the logger name (event name)
    # Logger will propagate to root which has the QueueHandler
    logger = logging.getLogger(name)
    # Log with extra fields (empty message)
    logger.info("", extra=fields)


@contextlib.contextmanager
def span(name: str, **fields):
    """Context manager for structured timing spans.

    On enter: emits '<name>.start' with span_id and given fields.
    On normal exit: emits '<name>.end' with span_id, duration_ms, ok=true.
    On exception: emits '<name>.end' with ok=false, exc_type, exc_msg, traceback, then re-raises.

    Args:
        name: span name (e.g., 'run.execute', 'catalog.compact').
        **fields: arbitrary context fields to include in start/end records.

    Yields:
        None.

    Raises:
        Re-raises any exception caught during the span.
    """
    span_id = uuid.uuid4().hex
    t0 = time.monotonic_ns()

    event(f"{name}.start", span_id=span_id, **fields)

    try:
        yield
        duration_ms = (time.monotonic_ns() - t0) / 1e6
        event(f"{name}.end", span_id=span_id, duration_ms=duration_ms, ok=True)
    except Exception as exc:
        duration_ms = (time.monotonic_ns() - t0) / 1e6
        event(
            f"{name}.end",
            span_id=span_id,
            duration_ms=duration_ms,
            ok=False,
            exc_type=type(exc).__name__,
            exc_msg=str(exc),
            traceback=traceback.format_exc()[:8192],
        )
        raise


# ─────────────────────────────────────────────────────────────────────────────
# Shutdown / drain
# ─────────────────────────────────────────────────────────────────────────────


def _shutdown() -> None:
    """Drain queue with timeout and stop listener cleanly.

    Called automatically by atexit. Safe to call multiple times.
    """
    global _INITIALIZED, _listener

    if _listener is None:
        return

    try:
        # Wait for queue to drain before stopping listener
        _wait_for_queue_drain(2.0)
        # Stop the listener (this will process remaining items in queue and stop)
        _listener.stop()
    except Exception as e:
        # If listener died, write emergency record to stderr
        print(f"ERROR: telemetry listener shutdown failed: {e}", file=sys.stderr)
    finally:
        _INITIALIZED = False
        _listener = None


def _get_default_log_dir() -> Path:
    """Resolve default log directory.

    Order: _DEFAULT_LOG_DIR (for tests) → BTH_LOG_DIR env var → ~/.bth/catalog/logs/ (default).
    """
    # Check module variable (for test monkeypatch)
    if _DEFAULT_LOG_DIR is not None:
        return _DEFAULT_LOG_DIR

    if env_dir := os.environ.get("BTH_LOG_DIR"):
        return Path(env_dir)

    # Default to ~/.bth/catalog/logs/
    return Path.home() / ".bth" / "catalog" / "logs"
