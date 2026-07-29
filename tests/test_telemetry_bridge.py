"""Tests for CISTERNAL_TELEMETRY=bathos cutover bridge (M6)."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from bathos.telemetry_bridge import (
    cisternal_cutover_enabled,
    emit_via_cisternal,
    init_server_telemetry,
)


@pytest.fixture(autouse=True)
def _reset_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset bathos + cisternal telemetry state between tests."""
    import sys

    import bathos.telemetry as tel

    tel._INITIALIZED = False
    tel._listener = None
    tel._queue = None
    tel._handlers.clear()
    tel._lazy_init_warning_shown = False
    tel._DEFAULT_LOG_DIR = None

    monkeypatch.delenv("CISTERNAL_TELEMETRY", raising=False)

    try:
        import cisternal
        from cisternal.telemetry.pipeline import shutdown_pipeline

        shutdown_pipeline()
    except ImportError:
        pass

    yield

    try:
        from cisternal.telemetry.pipeline import shutdown_pipeline

        shutdown_pipeline()
    except ImportError:
        pass

    if "bathos.telemetry" in sys.modules:
        mod = sys.modules["bathos.telemetry"]
        mod._INITIALIZED = False
        mod._listener = None
        mod._queue = None


def test_cisternal_cutover_disabled_by_default() -> None:
    assert cisternal_cutover_enabled() is False


@pytest.mark.parametrize("value", ["bathos", "all", "1", "true", "yes"])
def test_cisternal_cutover_enabled_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("CISTERNAL_TELEMETRY", value)
    assert cisternal_cutover_enabled() is True


def test_legacy_event_writes_jsonl(tmp_path: Path) -> None:
    from bathos.telemetry import event, init_telemetry

    init_telemetry(log_dir=tmp_path)
    event("run.start", run_uuid="abc")
    time.sleep(0.05)

    files = list(tmp_path.glob("events.*.jsonl"))
    assert len(files) >= 1


def test_cisternal_event_when_flag_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pytest.importorskip("cisternal")
    monkeypatch.setenv("CISTERNAL_TELEMETRY", "bathos")

    init_server_telemetry(log_dir=tmp_path)
    assert emit_via_cisternal("mcp.call_start", tool="demo_tool", request_id="r1")

    import cisternal

    pipeline = cisternal.get_pipeline()
    assert pipeline is not None
    assert pipeline.events_emitted >= 1

    files = list(tmp_path.glob("events.*.jsonl"))
    assert len(files) >= 1
