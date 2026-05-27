"""
Tests for bathos.telemetry — structured JSONL event substrate.

All tests are RED: they fail with ImportError until src/bathos/telemetry.py is written.

Design contract (from task spec):
  - init_telemetry(level=None, log_dir=None) — idempotent, configures QueueHandler pipeline
  - get_logger(name) -> logging.Logger named "bathos.<name>", propagate=False
  - event(name, **fields) — emit a structured event; lazy-inits if not yet initialized
  - span(name, **fields) — context manager: emits name.start + name.end with span_id + duration_ms
  - run_uuid_var — contextvars.ContextVar for correlation
  - _INITIALIZED — module-level flag (reset to trigger re-init in tests)
  - Envelope fields on every record: ts, level, pid, tid, host, surface, event, msg
  - JSONL file naming: events.<hostname>.<pid>.jsonl
"""

from __future__ import annotations

import json
import logging
import multiprocessing
import os
import socket
import time
import uuid
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# NOTE: All imports from bathos.telemetry are inside test bodies so that
# collection-time ImportError messages are clear and don't cascade into
# confusing AttributeErrors.  Each test imports only what it needs.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _drain_and_read_jsonl(log_dir: Path, sleep_s: float = 0.2) -> list[dict]:
    """Wait briefly for the async listener, then read all JSONL records."""
    time.sleep(sleep_s)
    records = []
    for f in log_dir.glob("events.*.jsonl"):
        for line in f.read_text().splitlines():
            line = line.strip()
            if line:
                records.append(json.loads(line))
    return records


def _reset_telemetry_module():
    """Force telemetry module to re-initialize on next call.

    Pokes _INITIALIZED=False so tests that need a fresh pipeline can call
    init_telemetry() without side-effects from a prior test's init.
    """
    import importlib
    import sys

    if "bathos.telemetry" in sys.modules:
        mod = sys.modules["bathos.telemetry"]
        mod._INITIALIZED = False
        # Also shut down the existing listener so we don't leak threads
        if hasattr(mod, "_shutdown"):
            mod._shutdown()


# ---------------------------------------------------------------------------
# § 1  Module API surface
# ---------------------------------------------------------------------------


class TestModuleAPISurface:
    """Verify the public symbols exist and behave correctly."""

    def test_init_telemetry_is_idempotent(self, tmp_path):
        """Calling init_telemetry twice must not raise and must not duplicate handlers."""
        from bathos.telemetry import init_telemetry

        _reset_telemetry_module()
        init_telemetry(level=logging.DEBUG, log_dir=tmp_path)
        # Second call — must not raise, must not add duplicate handlers
        init_telemetry(level=logging.DEBUG, log_dir=tmp_path)

    def test_get_logger_returns_logging_logger(self, tmp_path):
        """get_logger('foo') returns logging.Logger named 'bathos.foo'."""
        from bathos.telemetry import get_logger, init_telemetry

        _reset_telemetry_module()
        init_telemetry(log_dir=tmp_path)
        logger = get_logger("foo")
        assert isinstance(logger, logging.Logger)
        assert logger.name == "bathos.foo"

    def test_get_logger_propagate_false(self, tmp_path):
        """get_logger must set propagate=False to avoid duplicate root-logger output."""
        from bathos.telemetry import get_logger, init_telemetry

        _reset_telemetry_module()
        init_telemetry(log_dir=tmp_path)
        logger = get_logger("bar")
        assert logger.propagate is False

    def test_event_before_init_lazy_inits(self, tmp_path, monkeypatch):
        """event() before init_telemetry() must lazy-init and never silently drop."""
        monkeypatch.chdir(tmp_path)
        import bathos.telemetry as tel

        _reset_telemetry_module()
        # Redirect default log_dir to tmp_path so we can find the file
        monkeypatch.setattr(tel, "_DEFAULT_LOG_DIR", tmp_path, raising=False)

        from bathos.telemetry import event

        event("test.lazy_init", payload="hello")

        # Drain and verify file was created
        time.sleep(0.3)
        files = list(tmp_path.glob("events.*.jsonl"))
        assert len(files) >= 1, "No JSONL file created after lazy-init event()"

        records = []
        for f in files:
            for line in f.read_text().splitlines():
                line = line.strip()
                if line:
                    records.append(json.loads(line))
        assert any(r.get("event") == "test.lazy_init" for r in records)

    def test_span_emits_start_and_end(self, tmp_path):
        """span() context manager emits <name>.start and <name>.end."""
        from bathos.telemetry import init_telemetry, span

        _reset_telemetry_module()
        init_telemetry(log_dir=tmp_path)

        with span("db.query", table="runs"):
            pass

        records = _drain_and_read_jsonl(tmp_path)
        event_names = [r["event"] for r in records]
        assert "db.query.start" in event_names
        assert "db.query.end" in event_names

    def test_span_end_has_span_id_and_duration(self, tmp_path):
        """span() end record must carry span_id and duration_ms fields."""
        from bathos.telemetry import init_telemetry, span

        _reset_telemetry_module()
        init_telemetry(log_dir=tmp_path)

        with span("compact.run"):
            time.sleep(0.01)

        records = _drain_and_read_jsonl(tmp_path)
        end_records = [r for r in records if r.get("event") == "compact.run.end"]
        assert len(end_records) == 1
        end = end_records[0]
        assert "span_id" in end
        assert "duration_ms" in end
        assert end["duration_ms"] >= 0

    def test_span_start_and_end_share_span_id(self, tmp_path):
        """start and end records for the same span must have the same span_id."""
        from bathos.telemetry import init_telemetry, span

        _reset_telemetry_module()
        init_telemetry(log_dir=tmp_path)

        with span("archive.write"):
            pass

        records = _drain_and_read_jsonl(tmp_path)
        start_records = [r for r in records if r.get("event") == "archive.write.start"]
        end_records = [r for r in records if r.get("event") == "archive.write.end"]
        assert start_records and end_records
        assert start_records[0]["span_id"] == end_records[0]["span_id"]

    def test_span_on_exception_emits_end_with_ok_false(self, tmp_path):
        """span() on exception emits end with ok=false, exc_type, exc_msg, and re-raises."""
        from bathos.telemetry import init_telemetry, span

        _reset_telemetry_module()
        init_telemetry(log_dir=tmp_path)

        with pytest.raises(ValueError, match="boom"):
            with span("runner.exec"):
                raise ValueError("boom")

        records = _drain_and_read_jsonl(tmp_path)
        end_records = [r for r in records if r.get("event") == "runner.exec.end"]
        assert len(end_records) == 1
        end = end_records[0]
        assert end.get("ok") is False
        assert end.get("exc_type") == "ValueError"
        assert "boom" in end.get("exc_msg", "")


# ---------------------------------------------------------------------------
# § 2  JSONL format — envelope fields
# ---------------------------------------------------------------------------


class TestJSONLFormat:
    """Every emitted record must carry the required envelope fields."""

    def test_envelope_fields_present(self, tmp_path):
        """Each JSONL record must have: ts, level, pid, tid, host, surface, event, msg."""
        from bathos.telemetry import event, init_telemetry

        _reset_telemetry_module()
        init_telemetry(log_dir=tmp_path)
        event("run.start", run_id="abc123")

        records = _drain_and_read_jsonl(tmp_path)
        assert records, "No records written"
        rec = next(r for r in records if r.get("event") == "run.start")
        for field in ("ts", "level", "pid", "tid", "host", "surface", "event", "msg"):
            assert field in rec, f"Missing envelope field: {field!r}"

    def test_ts_is_iso8601(self, tmp_path):
        """The ts field must be parseable as ISO 8601."""
        from datetime import datetime

        from bathos.telemetry import event, init_telemetry

        _reset_telemetry_module()
        init_telemetry(log_dir=tmp_path)
        event("catalog.write")

        records = _drain_and_read_jsonl(tmp_path)
        rec = next(r for r in records if r.get("event") == "catalog.write")
        ts = rec["ts"]
        # fromisoformat handles +00:00 and Z suffixes in Python 3.11+
        # For 3.10 compat, strip trailing Z and replace with +00:00
        ts_normalized = ts.replace("Z", "+00:00")
        # Should not raise
        parsed = datetime.fromisoformat(ts_normalized)
        assert parsed.year >= 2020

    def test_surface_derived_from_event_prefix(self, tmp_path):
        """surface field is the first component of the event name (before the dot)."""
        from bathos.telemetry import event, init_telemetry

        _reset_telemetry_module()
        init_telemetry(log_dir=tmp_path)
        event("run.start")
        event("catalog.write")
        event("compact.done")

        records = _drain_and_read_jsonl(tmp_path)
        by_event = {r["event"]: r for r in records}
        assert by_event["run.start"]["surface"] == "run"
        assert by_event["catalog.write"]["surface"] == "catalog"
        assert by_event["compact.done"]["surface"] == "compact"

    def test_null_contextvar_fields_omitted(self, tmp_path):
        """When run_uuid_var / mcp_request_id_var / task_id_var are not set,
        the resulting record must NOT contain those keys (not even as null).
        """
        from bathos.telemetry import event, init_telemetry, run_uuid_var

        _reset_telemetry_module()
        init_telemetry(log_dir=tmp_path)

        # Make sure run_uuid_var is unset (default sentinel)
        token = run_uuid_var.set(None)
        try:
            event("sidecar.hash")
        finally:
            run_uuid_var.reset(token)

        records = _drain_and_read_jsonl(tmp_path)
        rec = next(r for r in records if r.get("event") == "sidecar.hash")
        assert "run_uuid" not in rec, "run_uuid must be omitted when not set, got null noise"
        assert "mcp_request_id" not in rec
        assert "task_id" not in rec


# ---------------------------------------------------------------------------
# § 3  Contextvar correlation — CRITICAL regression test for F2
# ---------------------------------------------------------------------------


class TestContextvarCorrelation:
    """
    The common bug: reading var.get() in the LISTENER thread returns empty
    because the listener has its own context.

    The fix: prepare() snapshots contextvar values onto the LogRecord __dict__
    BEFORE enqueuing, on the PRODUCER thread.  The listener then reads from
    __dict__, not from the var.

    This test proves that fix is in place.
    """

    def test_run_uuid_propagates_from_producer_to_jsonl(self, tmp_path):
        """run_uuid set on main thread must appear in the JSONL record written by listener."""
        from bathos.telemetry import event, init_telemetry, run_uuid_var

        _reset_telemetry_module()
        init_telemetry(log_dir=tmp_path)

        known_uuid = str(uuid.uuid4())
        token = run_uuid_var.set(known_uuid)
        try:
            event("run.progress", step=42)
        finally:
            run_uuid_var.reset(token)

        # Drain the queue — listener writes asynchronously
        time.sleep(0.3)

        records = _drain_and_read_jsonl(tmp_path)
        matching = [r for r in records if r.get("event") == "run.progress"]
        assert matching, "No 'run.progress' record found in JSONL"
        rec = matching[0]

        assert "run_uuid" in rec, (
            "run_uuid missing from record. "
            "Common bug: listener reads var.get() on its own thread/context. "
            "Fix: snapshot var values into record.__dict__ before enqueue (producer side)."
        )
        assert rec["run_uuid"] == known_uuid, (
            f"Expected run_uuid={known_uuid!r}, got {rec.get('run_uuid')!r}. "
            "The contextvar was not snapshotted before enqueue."
        )


# ---------------------------------------------------------------------------
# § 4  Per-process file naming
# ---------------------------------------------------------------------------


class TestPerProcessFileNaming:
    """Each process must write to its own events.<hostname>.<pid>.jsonl file."""

    def test_filename_contains_hostname_and_pid(self, tmp_path, monkeypatch):
        """File name pattern: events.<hostname>.<pid>.jsonl."""
        from bathos.telemetry import event, init_telemetry

        _reset_telemetry_module()
        init_telemetry(log_dir=tmp_path)
        event("test.file_naming")
        time.sleep(0.2)

        hostname = socket.gethostname()
        pid = os.getpid()
        expected_name = f"events.{hostname}.{pid}.jsonl"
        files = list(tmp_path.glob("events.*.jsonl"))
        assert any(f.name == expected_name for f in files), (
            f"Expected file {expected_name!r}, got: {[f.name for f in files]}"
        )

    def test_different_pids_create_separate_files(self, tmp_path, monkeypatch):
        """Two different fake PIDs must produce two separate files."""
        import bathos.telemetry as tel

        _reset_telemetry_module()

        fake_pid_1 = 99991
        fake_pid_2 = 99992
        hostname = socket.gethostname()

        # First init with fake PID 1
        monkeypatch.setattr(os, "getpid", lambda: fake_pid_1)
        tel._INITIALIZED = False
        if hasattr(tel, "_shutdown"):
            tel._shutdown()
        tel.init_telemetry(log_dir=tmp_path)
        tel.event("proc.one")
        time.sleep(0.2)
        # Shutdown so we can re-init cleanly
        if hasattr(tel, "_shutdown"):
            tel._shutdown()

        # Second init with fake PID 2
        monkeypatch.setattr(os, "getpid", lambda: fake_pid_2)
        tel._INITIALIZED = False
        tel.init_telemetry(log_dir=tmp_path)
        tel.event("proc.two")
        time.sleep(0.2)

        file1 = tmp_path / f"events.{hostname}.{fake_pid_1}.jsonl"
        file2 = tmp_path / f"events.{hostname}.{fake_pid_2}.jsonl"
        assert file1.exists(), f"File for PID {fake_pid_1} not created"
        assert file2.exists(), f"File for PID {fake_pid_2} not created"


# ---------------------------------------------------------------------------
# § 5  SLURM-array concurrent write safety
# ---------------------------------------------------------------------------


def _worker_emit_events(log_dir: str, n_events: int) -> None:
    """Subprocess target: init telemetry and emit n_events."""
    import sys

    sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
    from bathos.telemetry import _INITIALIZED  # noqa: F401 — check importable
    import bathos.telemetry as tel

    tel._INITIALIZED = False
    tel.init_telemetry(log_dir=Path(log_dir))
    for i in range(n_events):
        tel.event("slurm.step", step=i)
    time.sleep(0.3)  # give listener time to flush
    if hasattr(tel, "_shutdown"):
        tel._shutdown()


class TestSLURMConcurrentWrites:
    """Four worker processes each writing 50 events → 4 files, 200 total events."""

    def test_concurrent_multiprocess_writes(self, tmp_path):
        n_workers = 4
        events_per_worker = 50

        procs = []
        ctx = multiprocessing.get_context("spawn")
        for _ in range(n_workers):
            p = ctx.Process(
                target=_worker_emit_events,
                args=(str(tmp_path), events_per_worker),
            )
            procs.append(p)
            p.start()

        for p in procs:
            p.join(timeout=30)
            assert p.exitcode == 0, f"Worker exited with code {p.exitcode}"

        files = list(tmp_path.glob("events.*.jsonl"))
        assert len(files) == n_workers, (
            f"Expected {n_workers} files, got {len(files)}: {[f.name for f in files]}"
        )

        all_records = []
        for f in files:
            for line in f.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)  # raises if invalid JSON
                all_records.append(rec)

        slurm_events = [r for r in all_records if r.get("event") == "slurm.step"]
        assert len(slurm_events) == n_workers * events_per_worker, (
            f"Expected {n_workers * events_per_worker} slurm.step events, "
            f"got {len(slurm_events)}"
        )


# ---------------------------------------------------------------------------
# § 6  Queue listener shutdown / atexit drain
# ---------------------------------------------------------------------------


class TestAtexitDrain:
    """After shutdown/drain, queue must be empty and all events in JSONL."""

    def test_atexit_drain_flushes_queue(self, tmp_path):
        """Calling the drain function directly must flush all pending events."""
        from bathos.telemetry import event, init_telemetry

        _reset_telemetry_module()
        init_telemetry(log_dir=tmp_path)

        for i in range(10):
            event("drain.test", index=i)

        import bathos.telemetry as tel

        # Call the registered drain/shutdown directly (simulates atexit)
        assert hasattr(tel, "_shutdown"), (
            "telemetry module must expose a _shutdown() callable for atexit/drain"
        )
        tel._shutdown()

        # After explicit shutdown, all events must be in JSONL
        records = []
        for f in tmp_path.glob("events.*.jsonl"):
            for line in f.read_text().splitlines():
                line = line.strip()
                if line:
                    records.append(json.loads(line))

        drain_events = [r for r in records if r.get("event") == "drain.test"]
        assert len(drain_events) == 10, (
            f"Expected 10 drain.test events after shutdown, got {len(drain_events)}"
        )

    def test_queue_empty_after_shutdown(self, tmp_path):
        """After _shutdown(), the internal queue must report empty (qsize==0)."""
        from bathos.telemetry import event, init_telemetry

        _reset_telemetry_module()
        init_telemetry(log_dir=tmp_path)
        event("queue.check", x=1)

        import bathos.telemetry as tel

        tel._shutdown()

        assert hasattr(tel, "_queue"), "telemetry module must expose _queue"
        assert tel._queue.empty(), "Queue must be empty after _shutdown()"


# ---------------------------------------------------------------------------
# § 7  Rotation configuration
# ---------------------------------------------------------------------------


class TestRotationConfiguration:
    """JSONL file rotates when max_bytes exceeded."""

    def test_rotation_creates_backup_file(self, tmp_path):
        """After exceeding max_bytes=1024, at least one .jsonl.1 backup must exist."""
        from bathos.telemetry import event, init_telemetry

        _reset_telemetry_module()
        init_telemetry(log_dir=tmp_path, max_bytes=1024, backup_count=2)

        # Emit enough large events to exceed 1024 bytes
        large_payload = "x" * 200
        for i in range(20):
            event("rotation.test", index=i, data=large_payload)

        time.sleep(0.4)

        backup_files = list(tmp_path.glob("events.*.jsonl.1"))
        assert len(backup_files) >= 1, (
            f"Expected at least one .jsonl.1 backup file after exceeding max_bytes=1024, "
            f"got: {list(tmp_path.iterdir())}"
        )
