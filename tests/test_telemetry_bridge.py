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


# --- level is forwarded to cisternal (cisternal >= 0.1.1a11) ------------------
#
# On the legacy path `level` is a stdlib severity filter (init_telemetry calls
# root_logger.setLevel). cisternal 0.1.1a11 added init(level=...), so the
# cutover path forwards the same value -- explicit argument, else BTH_LOG_LEVEL
# -- instead of dropping it (cisternal debt #2272).


def test_cisternal_forwards_explicit_level(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("cisternal")
    import logging
    import warnings

    import cisternal

    from bathos.telemetry_bridge import init_via_cisternal

    monkeypatch.setenv("CISTERNAL_TELEMETRY", "bathos")
    monkeypatch.delenv("BTH_LOG_LEVEL", raising=False)
    monkeypatch.delenv("CISTERNAL_LOG_LEVEL", raising=False)

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        assert init_via_cisternal(level="WARNING", log_dir=tmp_path) is True
    assert cisternal.get_pipeline().min_level == logging.WARNING


def test_cisternal_forwards_bth_log_level(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("cisternal")
    import logging

    import cisternal

    from bathos.telemetry_bridge import init_via_cisternal

    monkeypatch.setenv("CISTERNAL_TELEMETRY", "bathos")
    monkeypatch.setenv("BTH_LOG_LEVEL", "ERROR")
    monkeypatch.delenv("CISTERNAL_LOG_LEVEL", raising=False)

    assert init_via_cisternal(log_dir=tmp_path) is True
    assert cisternal.get_pipeline().min_level == logging.ERROR


def test_cisternal_explicit_level_beats_bth_log_level(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("cisternal")
    import logging

    import cisternal

    from bathos.telemetry_bridge import init_via_cisternal

    monkeypatch.setenv("CISTERNAL_TELEMETRY", "bathos")
    monkeypatch.setenv("BTH_LOG_LEVEL", "ERROR")
    monkeypatch.delenv("CISTERNAL_LOG_LEVEL", raising=False)

    assert init_via_cisternal(level="DEBUG", log_dir=tmp_path) is True
    assert cisternal.get_pipeline().min_level == logging.DEBUG


def test_cisternal_no_level_means_no_filtering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No level asked for: cisternal's default (no filtering) is kept, silently."""
    pytest.importorskip("cisternal")
    import warnings

    import cisternal

    from bathos.telemetry_bridge import init_via_cisternal

    monkeypatch.setenv("CISTERNAL_TELEMETRY", "bathos")
    monkeypatch.delenv("BTH_LOG_LEVEL", raising=False)
    monkeypatch.delenv("CISTERNAL_LOG_LEVEL", raising=False)

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        assert init_via_cisternal(log_dir=tmp_path) is True
    assert cisternal.get_pipeline().min_level is None


def test_no_warning_when_cutover_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Off by default: the legacy path honours level, so there is nothing to warn about."""
    import warnings

    from bathos.telemetry_bridge import init_via_cisternal

    monkeypatch.delenv("CISTERNAL_TELEMETRY", raising=False)
    monkeypatch.setenv("BTH_LOG_LEVEL", "DEBUG")

    with warnings.catch_warnings():
        warnings.simplefilter("error", RuntimeWarning)
        assert init_via_cisternal(level="DEBUG", log_dir=tmp_path) is False
