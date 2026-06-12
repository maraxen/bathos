"""Tests for sprint_audit signals extension (Phase 4a, item 5)."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bathos.catalog import init_catalog, write_run
from bathos.compact import compact
from bathos.schema import Run


@pytest.fixture
def monkeypatch_registry(monkeypatch, tmp_path) -> None:
    """Monkey-patch the registry to a temporary location."""
    registry_path = tmp_path / "projects.toml"
    monkeypatch.setattr(
        "bathos.config.PROJECTS_REGISTRY",
        registry_path,
    )


class TestBypassSignals:
    """Test error_rate, bypass_explicit, and bypass_in_agent_mode signals."""

    def test_error_rate_zero(self, monkeypatch_registry, tmp_path):
        """Test error_rate is 0 when no error outcomes."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        catalog_dir = tmp_path / "test_catalog"
        catalog_dir.mkdir()
        init_catalog(catalog_dir)

        base_time = datetime.now(UTC)
        runs = [
            Run(
                project_slug="test_project",
                command="python test.py",
                argv=["python", "test.py"],
                git_hash="abc123",
                git_branch="main",
                git_dirty=False,
                timestamp=base_time,
                status="completed",
                exit_code=0,
                outcome="pass",
                sidecar_mode="normal",
                outcome_is_residual=False,
            ),
            Run(
                project_slug="test_project",
                command="python test.py",
                argv=["python", "test.py"],
                git_hash="abc123",
                git_branch="main",
                git_dirty=False,
                timestamp=base_time + timedelta(seconds=1),
                status="completed",
                exit_code=1,
                outcome="fail",
                sidecar_mode="normal",
                outcome_is_residual=False,
            ),
        ]

        for r in runs:
            write_run(r, catalog_dir)
        compact(catalog_dir)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        assert "test_project" in result["audit_results"]
        signals = result["audit_results"]["test_project"]["signals"]
        assert signals["error_rate"] == 0.0

    def test_error_rate_nonzero(self, monkeypatch_registry, tmp_path):
        """Test error_rate > 0 when error outcomes present."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        catalog_dir = tmp_path / "test_catalog"
        catalog_dir.mkdir()
        init_catalog(catalog_dir)

        base_time = datetime.now(UTC)
        runs = [
            Run(
                project_slug="test_project",
                command="python test.py",
                argv=["python", "test.py"],
                git_hash="abc123",
                git_branch="main",
                git_dirty=False,
                timestamp=base_time,
                status="completed",
                exit_code=0,
                outcome="pass",
                sidecar_mode="normal",
                outcome_is_residual=False,
            ),
            Run(
                project_slug="test_project",
                command="python test.py",
                argv=["python", "test.py"],
                git_hash="abc123",
                git_branch="main",
                git_dirty=False,
                timestamp=base_time + timedelta(seconds=1),
                status="completed",
                exit_code=0,
                outcome="error",
                sidecar_mode="normal",
                outcome_is_residual=False,
            ),
            Run(
                project_slug="test_project",
                command="python test.py",
                argv=["python", "test.py"],
                git_hash="abc123",
                git_branch="main",
                git_dirty=False,
                timestamp=base_time + timedelta(seconds=2),
                status="completed",
                exit_code=0,
                outcome="error",
                sidecar_mode="normal",
                outcome_is_residual=False,
            ),
        ]

        for r in runs:
            write_run(r, catalog_dir)
        compact(catalog_dir)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        assert signals["error_rate"] == pytest.approx(2 / 3)

    def test_bypass_explicit_and_agent_mode_are_separate(self, monkeypatch_registry, tmp_path):
        """Test that bypass_explicit and bypass_in_agent_mode are distinct signals."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        catalog_dir = tmp_path / "test_catalog"
        catalog_dir.mkdir()
        init_catalog(catalog_dir)

        base_time = datetime.now(UTC)
        runs = [
            # Normal run
            Run(
                project_slug="test_project",
                command="python test.py",
                argv=["python", "test.py"],
                git_hash="abc123",
                git_branch="main",
                git_dirty=False,
                timestamp=base_time,
                status="completed",
                exit_code=0,
                outcome="pass",
                sidecar_mode="normal",
                agent_mode="",
                outcome_is_residual=False,
            ),
            # Bypassed (no sidecar), not in agent mode
            Run(
                project_slug="test_project",
                command="python test.py",
                argv=["python", "test.py"],
                git_hash="abc123",
                git_branch="main",
                git_dirty=False,
                timestamp=base_time + timedelta(seconds=1),
                status="completed",
                exit_code=0,
                outcome="unknown",
                sidecar_mode="bypassed",
                agent_mode="",
                outcome_is_residual=False,
            ),
            # Bypassed in agent mode
            Run(
                project_slug="test_project",
                command="python test.py",
                argv=["python", "test.py"],
                git_hash="abc123",
                git_branch="main",
                git_dirty=False,
                timestamp=base_time + timedelta(seconds=2),
                status="completed",
                exit_code=0,
                outcome="unknown",
                sidecar_mode="bypassed",
                agent_mode="audit",
                outcome_is_residual=False,
            ),
            # Agent mode but not bypassed
            Run(
                project_slug="test_project",
                command="python test.py",
                argv=["python", "test.py"],
                git_hash="abc123",
                git_branch="main",
                git_dirty=False,
                timestamp=base_time + timedelta(seconds=3),
                status="completed",
                exit_code=0,
                outcome="pass",
                sidecar_mode="normal",
                agent_mode="audit",
                outcome_is_residual=False,
            ),
        ]

        for r in runs:
            write_run(r, catalog_dir)
        compact(catalog_dir)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]

        # bypass_explicit: sidecar_mode="bypassed" and agent_mode=""
        # Expected: 1 / 4 = 0.25
        assert "bypass_explicit" in signals
        assert signals["bypass_explicit"] == pytest.approx(0.25)

        # bypass_in_agent_mode: sidecar_mode="bypassed" and agent_mode non-empty
        # Expected: 1 / 1 = 1.0 (only 1 agent-mode run was bypassed, of 2 total agent-mode runs)
        assert "bypass_in_agent_mode" in signals
        # There are 2 agent_mode runs total (index 2 and 3)
        # Of those, 1 was bypassed (index 2)
        assert signals["bypass_in_agent_mode"] == pytest.approx(0.5)


class TestOutcomeEntropy:
    """Test outcome_entropy signal."""

    def test_outcome_entropy_zero_for_single_label(self, monkeypatch_registry, tmp_path):
        """Test outcome_entropy is 0 when all runs have same outcome."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        catalog_dir = tmp_path / "test_catalog"
        catalog_dir.mkdir()
        init_catalog(catalog_dir)

        base_time = datetime.now(UTC)
        runs = [
            Run(
                project_slug="test_project",
                command="python test.py",
                argv=["python", "test.py"],
                git_hash="abc123",
                git_branch="main",
                git_dirty=False,
                timestamp=base_time + timedelta(seconds=i),
                status="completed",
                exit_code=0,
                outcome="pass",
                sidecar_mode="normal",
                outcome_is_residual=False,
            )
            for i in range(5)
        ]

        for r in runs:
            write_run(r, catalog_dir)
        compact(catalog_dir)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        assert signals["outcome_entropy"] == pytest.approx(0.0)

    def test_outcome_entropy_positive_for_mixed(self, monkeypatch_registry, tmp_path):
        """Test outcome_entropy > 0 for mixed outcomes."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        catalog_dir = tmp_path / "test_catalog"
        catalog_dir.mkdir()
        init_catalog(catalog_dir)

        base_time = datetime.now(UTC)
        # 2 pass, 2 fail
        outcomes = ["pass", "pass", "fail", "fail"]
        runs = [
            Run(
                project_slug="test_project",
                command="python test.py",
                argv=["python", "test.py"],
                git_hash="abc123",
                git_branch="main",
                git_dirty=False,
                timestamp=base_time + timedelta(seconds=i),
                status="completed",
                exit_code=0,
                outcome=outcomes[i],
                sidecar_mode="normal",
                outcome_is_residual=False,
            )
            for i in range(4)
        ]

        for r in runs:
            write_run(r, catalog_dir)
        compact(catalog_dir)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        # H = -0.5*ln(0.5) - 0.5*ln(0.5) = ln(2) ≈ 0.693
        assert signals["outcome_entropy"] > 0.0
        assert signals["outcome_entropy"] == pytest.approx(math.log(2))

    def test_warn_when_entropy_below_threshold(self, monkeypatch_registry, tmp_path):
        """Test [WARN] annotation when outcome_entropy < 0.5 nats."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        catalog_dir = tmp_path / "test_catalog"
        catalog_dir.mkdir()
        init_catalog(catalog_dir)

        base_time = datetime.now(UTC)
        # All same outcome → entropy = 0 < 0.5
        runs = [
            Run(
                project_slug="test_project",
                command="python test.py",
                argv=["python", "test.py"],
                git_hash="abc123",
                git_branch="main",
                git_dirty=False,
                timestamp=base_time + timedelta(seconds=i),
                status="completed",
                exit_code=0,
                outcome="pass",
                sidecar_mode="normal",
                outcome_is_residual=False,
            )
            for i in range(5)
        ]

        for r in runs:
            write_run(r, catalog_dir)
        compact(catalog_dir)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        anomalies = result["audit_results"]["test_project"]["anomalies"]

        # Entropy should be 0 (below threshold)
        assert signals["outcome_entropy"] < 0.5
        # Should have a warning
        assert any("outcome_entropy" in str(a) for a in anomalies)


class TestUnfiredBranches:
    """Test unfired_branches signal."""

    def test_unfired_branches_zero_when_all_outcomes_used(self, monkeypatch_registry, tmp_path):
        """Test unfired_branches is 0 when all sidecar outcomes are used."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        catalog_dir = tmp_path / "test_catalog"
        catalog_dir.mkdir()
        init_catalog(catalog_dir)

        # Create a sidecar with 3 outcomes
        sidecar_content = """
[experiment]
hypothesis = "Test hypothesis"

[outcomes.pass]
condition = "result == 'pass'"
decision = "Continue"

[outcomes.marginal]
condition = "result == 'marginal'"
decision = "Retry"

[outcomes.fail]
condition = "result == 'fail'"
decision = "Stop"

[result_schema]
result = "str"
"""
        sidecar_path = catalog_dir / "test_script.bth.toml"
        sidecar_path.write_text(sidecar_content)

        base_time = datetime.now(UTC)
        # Use all three outcomes
        outcomes = ["pass", "marginal", "fail"]
        runs = [
            Run(
                project_slug="test_project",
                command="python test_script.py",
                argv=["python", "test_script.py"],
                git_hash="abc123",
                git_branch="main",
                git_dirty=False,
                timestamp=base_time + timedelta(seconds=i),
                status="completed",
                exit_code=0,
                outcome=outcomes[i],
                sidecar_mode="normal",
                sidecar_path=str(sidecar_path),
                sidecar_sha256="abc123def456",
                outcome_is_residual=False,
            )
            for i in range(3)
        ]

        for r in runs:
            write_run(r, catalog_dir)
        compact(catalog_dir)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        # All declared outcomes are used → unfired_branches = 0
        assert signals["unfired_branches"] == pytest.approx(0.0)

    def test_unfired_branches_positive_when_outcomes_unused(self, monkeypatch_registry, tmp_path):
        """Test unfired_branches > 0 when some sidecar outcomes are never used."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        catalog_dir = tmp_path / "test_catalog"
        catalog_dir.mkdir()
        init_catalog(catalog_dir)

        # Create a sidecar with 3 outcomes
        sidecar_content = """
[experiment]
hypothesis = "Test hypothesis"

[outcomes.pass]
condition = "result == 'pass'"
decision = "Continue"

[outcomes.marginal]
condition = "result == 'marginal'"
decision = "Retry"

[outcomes.fail]
condition = "result == 'fail'"
decision = "Stop"

[result_schema]
result = "str"
"""
        sidecar_path = catalog_dir / "test_script.bth.toml"
        sidecar_path.write_text(sidecar_content)

        base_time = datetime.now(UTC)
        # Only use pass and marginal, skip fail
        outcomes = ["pass", "marginal", "pass"]
        runs = [
            Run(
                project_slug="test_project",
                command="python test_script.py",
                argv=["python", "test_script.py"],
                git_hash="abc123",
                git_branch="main",
                git_dirty=False,
                timestamp=base_time + timedelta(seconds=i),
                status="completed",
                exit_code=0,
                outcome=outcomes[i],
                sidecar_mode="normal",
                sidecar_path=str(sidecar_path),
                sidecar_sha256="abc123def456",
                outcome_is_residual=False,
            )
            for i in range(3)
        ]

        for r in runs:
            write_run(r, catalog_dir)
        compact(catalog_dir)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        # 1 of 3 outcomes is never used → unfired_branches = 1/3 ≈ 0.333
        assert signals["unfired_branches"] == pytest.approx(1 / 3)


class TestSchemaOverflowRate:
    """Test schema_overflow_rate signal."""

    def test_schema_overflow_rate_zero_when_no_overflow(self, monkeypatch_registry, tmp_path):
        """Test schema_overflow_rate is 0 when metadata is empty."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        catalog_dir = tmp_path / "test_catalog"
        catalog_dir.mkdir()
        init_catalog(catalog_dir)

        base_time = datetime.now(UTC)
        runs = [
            Run(
                project_slug="test_project",
                command="python test.py",
                argv=["python", "test.py"],
                git_hash="abc123",
                git_branch="main",
                git_dirty=False,
                timestamp=base_time + timedelta(seconds=i),
                status="completed",
                exit_code=0,
                outcome="pass",
                sidecar_mode="normal",
                metadata="{}",
                outcome_is_residual=False,
            )
            for i in range(3)
        ]

        for r in runs:
            write_run(r, catalog_dir)
        compact(catalog_dir)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        assert signals["schema_overflow_rate"] == pytest.approx(0.0)

    def test_schema_overflow_rate_always_zero_for_standard_runs(self, monkeypatch_registry, tmp_path):
        """Test schema_overflow_rate is 0 for standard runs (metadata computed during compact)."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        catalog_dir = tmp_path / "test_catalog"
        catalog_dir.mkdir()
        init_catalog(catalog_dir)

        base_time = datetime.now(UTC)
        # Create runs without special metadata
        runs = [
            Run(
                project_slug="test_project",
                command="python test.py",
                argv=["python", "test.py"],
                git_hash="abc123",
                git_branch="main",
                git_dirty=False,
                timestamp=base_time + timedelta(seconds=i),
                status="completed",
                exit_code=0,
                outcome="pass",
                sidecar_mode="normal",
                outcome_is_residual=False,
            )
            for i in range(3)
        ]

        for r in runs:
            write_run(r, catalog_dir)
        compact(catalog_dir)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        # All runs have empty metadata → 0.0
        assert signals["schema_overflow_rate"] == pytest.approx(0.0)


class TestPostHocBiasFlagAndIntegration:
    """Test post_hoc_bias_flag and overall signal integration."""

    def test_post_hoc_bias_flag_false_when_no_bias(self, monkeypatch_registry, tmp_path):
        """Test post_hoc_bias_flag is False when worst outcome is not concentrated early."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        catalog_dir = tmp_path / "test_catalog"
        catalog_dir.mkdir()
        init_catalog(catalog_dir)

        base_time = datetime.now(UTC)
        # Create chronological sequence: no concentration of "fail" at start
        outcomes = ["pass", "pass", "pass", "fail", "fail"]
        runs = [
            Run(
                project_slug="test_project",
                command="python test.py",
                argv=["python", "test.py"],
                git_hash="abc123",
                git_branch="main",
                git_dirty=False,
                timestamp=base_time + timedelta(seconds=i),
                status="completed",
                exit_code=0,
                outcome=outcomes[i],
                sidecar_mode="normal",
                outcome_is_residual=False,
            )
            for i in range(5)
        ]

        for r in runs:
            write_run(r, catalog_dir)
        compact(catalog_dir)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        assert signals["post_hoc_bias_flag"] is False

    def test_signals_dict_contains_all_seven(self, monkeypatch_registry, tmp_path):
        """Test that all 7 signals are present in output."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        catalog_dir = tmp_path / "test_catalog"
        catalog_dir.mkdir()
        init_catalog(catalog_dir)

        base_time = datetime.now(UTC)
        runs = [
            Run(
                project_slug="test_project",
                command="python test.py",
                argv=["python", "test.py"],
                git_hash="abc123",
                git_branch="main",
                git_dirty=False,
                timestamp=base_time + timedelta(seconds=i),
                status="completed",
                exit_code=0,
                outcome="pass",
                sidecar_mode="normal",
                outcome_is_residual=False,
            )
            for i in range(3)
        ]

        for r in runs:
            write_run(r, catalog_dir)
        compact(catalog_dir)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        assert "test_project" in result["audit_results"]
        audit_data = result["audit_results"]["test_project"]

        # All 7 signals must be present
        expected_signals = {
            "error_rate",
            "bypass_explicit",
            "bypass_in_agent_mode",
            "outcome_entropy",
            "unfired_branches",
            "schema_overflow_rate",
            "post_hoc_bias_flag",
        }
        signals = audit_data["signals"]
        for signal_name in expected_signals:
            assert signal_name in signals, f"Missing signal: {signal_name}"


# ---------------------------------------------------------------------------
# Boundary tests — Part A: 2 per signal (just below and just above threshold)
# ---------------------------------------------------------------------------


def _make_run(base_time, i, **kwargs):
    """Helper to construct a Run with minimal boilerplate."""
    defaults = dict(
        project_slug="test_project",
        command="python test.py",
        argv=["python", "test.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        timestamp=base_time + timedelta(seconds=i),
        status="completed",
        exit_code=0,
        outcome="pass",
        sidecar_mode="normal",
        agent_mode="",
        outcome_is_residual=False,
    )
    defaults.update(kwargs)
    return Run(**defaults)


def _build_catalog(tmp_path, runs):
    """Write runs to a fresh catalog, compact, and return catalog_dir."""
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    init_catalog(catalog_dir)
    for r in runs:
        write_run(r, catalog_dir)
    compact(catalog_dir)
    return catalog_dir


class TestErrorRateBoundary:
    """Boundary tests for error_rate signal (threshold 0.10)."""

    def test_error_rate_below_threshold(self, monkeypatch_registry, tmp_path):
        """9 runs, 0 errors -> error_rate = 0.0, no anomaly fired."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        base_time = datetime.now(UTC)
        runs = [_make_run(base_time, i, outcome="pass") for i in range(9)]
        catalog_dir = _build_catalog(tmp_path, runs)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        anomalies = result["audit_results"]["test_project"]["anomalies"]

        assert signals["error_rate"] == pytest.approx(0.0)
        assert not any("error_rate" in a for a in anomalies)

    def test_error_rate_above_threshold(self, monkeypatch_registry, tmp_path):
        """10 runs, 2 errors (20%) -> anomaly fires."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        base_time = datetime.now(UTC)
        outcomes = ["error", "error"] + ["pass"] * 8
        runs = [_make_run(base_time, i, outcome=outcomes[i]) for i in range(10)]
        catalog_dir = _build_catalog(tmp_path, runs)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        anomalies = result["audit_results"]["test_project"]["anomalies"]

        assert signals["error_rate"] == pytest.approx(0.2)
        assert any("error_rate" in a for a in anomalies)


class TestBypassExplicitBoundary:
    """Boundary tests for bypass_explicit signal (threshold 0.30)."""

    def test_bypass_explicit_below_threshold(self, monkeypatch_registry, tmp_path):
        """10 runs, 2 bypassed non-agent (20%) -> no anomaly."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        base_time = datetime.now(UTC)
        runs = (
            [_make_run(base_time, i, sidecar_mode="bypassed", agent_mode="") for i in range(2)]
            + [_make_run(base_time, i + 2, sidecar_mode="normal", agent_mode="") for i in range(8)]
        )
        catalog_dir = _build_catalog(tmp_path, runs)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        anomalies = result["audit_results"]["test_project"]["anomalies"]

        assert signals["bypass_explicit"] == pytest.approx(0.2)
        assert not any("bypass_explicit" in a for a in anomalies)

    def test_bypass_explicit_above_threshold(self, monkeypatch_registry, tmp_path):
        """10 runs, 4 bypassed non-agent (40%) -> anomaly fires."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        base_time = datetime.now(UTC)
        runs = (
            [_make_run(base_time, i, sidecar_mode="bypassed", agent_mode="") for i in range(4)]
            + [_make_run(base_time, i + 4, sidecar_mode="normal", agent_mode="") for i in range(6)]
        )
        catalog_dir = _build_catalog(tmp_path, runs)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        anomalies = result["audit_results"]["test_project"]["anomalies"]

        assert signals["bypass_explicit"] == pytest.approx(0.4)
        assert any("bypass_explicit" in a for a in anomalies)


class TestBypassAgentModeBoundary:
    """Boundary tests for bypass_in_agent_mode signal (threshold 0.05)."""

    def test_bypass_agent_mode_below_threshold(self, monkeypatch_registry, tmp_path):
        """20 agent-mode runs, 0 bypassed -> no anomaly."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        base_time = datetime.now(UTC)
        runs = [
            _make_run(base_time, i, sidecar_mode="normal", agent_mode="claude")
            for i in range(20)
        ]
        catalog_dir = _build_catalog(tmp_path, runs)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        anomalies = result["audit_results"]["test_project"]["anomalies"]

        assert signals["bypass_in_agent_mode"] == pytest.approx(0.0)
        assert not any("bypass_in_agent_mode" in a for a in anomalies)

    def test_bypass_agent_mode_above_threshold(self, monkeypatch_registry, tmp_path):
        """20 agent-mode runs, 2 bypassed (10%) -> anomaly fires."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        base_time = datetime.now(UTC)
        runs = (
            [
                _make_run(base_time, i, sidecar_mode="bypassed", agent_mode="claude")
                for i in range(2)
            ]
            + [
                _make_run(base_time, i + 2, sidecar_mode="normal", agent_mode="claude")
                for i in range(18)
            ]
        )
        catalog_dir = _build_catalog(tmp_path, runs)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        anomalies = result["audit_results"]["test_project"]["anomalies"]

        assert signals["bypass_in_agent_mode"] == pytest.approx(0.1)
        assert any("bypass_in_agent_mode" in a for a in anomalies)


class TestOutcomeEntropyBoundary:
    """Boundary tests for outcome_entropy (flag when < 0.5 nats)."""

    def test_outcome_entropy_above_threshold(self, monkeypatch_registry, tmp_path):
        """Balanced 4-label distribution -> entropy ~ ln(4) ~ 1.386 > 0.5, no anomaly."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        base_time = datetime.now(UTC)
        # 4 labels, 3 runs each -> entropy = ln(4) ~ 1.386
        labels = ["pass", "fail", "marginal", "error"] * 3
        runs = [_make_run(base_time, i, outcome=labels[i]) for i in range(12)]
        catalog_dir = _build_catalog(tmp_path, runs)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        anomalies = result["audit_results"]["test_project"]["anomalies"]

        assert signals["outcome_entropy"] > 0.5
        assert not any("outcome_entropy" in a for a in anomalies)

    def test_outcome_entropy_below_threshold(self, monkeypatch_registry, tmp_path):
        """Highly skewed: 9 pass, 1 fail -> H ~ 0.325 < 0.5, anomaly fires."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        base_time = datetime.now(UTC)
        # H = -(0.9*ln(0.9) + 0.1*ln(0.1)) ~ 0.325
        outcomes = ["pass"] * 9 + ["fail"]
        runs = [_make_run(base_time, i, outcome=outcomes[i]) for i in range(10)]
        catalog_dir = _build_catalog(tmp_path, runs)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        anomalies = result["audit_results"]["test_project"]["anomalies"]

        assert signals["outcome_entropy"] < 0.5
        assert any("outcome_entropy" in a for a in anomalies)


class TestUnfiredBranchesBoundary:
    """Boundary tests for unfired_branches signal (threshold 0.40)."""

    def _make_sidecar(self, tmp_path, n_outcomes):
        """Write a sidecar declaring n_outcomes outcome labels."""
        labels = [f"outcome_{i}" for i in range(n_outcomes)]
        sections = "\n".join(
            f'[outcomes.{label}]\ncondition = "x > {i}"\ndecision = "d"\n'
            for i, label in enumerate(labels)
        )
        content = f'[experiment]\nhypothesis = "h"\n\n{sections}\n[result_schema]\nx = "float"\n'
        path = tmp_path / "exp.bth.toml"
        path.write_text(content)
        return path, labels

    def test_unfired_branches_below_threshold(self, monkeypatch_registry, tmp_path):
        """10 declared outcomes, 3 never fired (30%) -> no anomaly."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        sidecar_path, labels = self._make_sidecar(tmp_path, 10)
        fired_labels = labels[:7]  # fire 7 of 10 -> 3 unfired = 30%

        base_time = datetime.now(UTC)
        runs = [
            _make_run(
                base_time,
                i,
                outcome=fired_labels[i % len(fired_labels)],
                sidecar_mode="normal",
                sidecar_path=str(sidecar_path),
                sidecar_sha256="abc123",
            )
            for i in range(14)
        ]
        catalog_dir = _build_catalog(tmp_path, runs)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        anomalies = result["audit_results"]["test_project"]["anomalies"]

        assert signals["unfired_branches"] == pytest.approx(3 / 10)
        assert not any("unfired_branches" in a for a in anomalies)

    def test_unfired_branches_above_threshold(self, monkeypatch_registry, tmp_path):
        """10 declared outcomes, 5 never fired (50%) -> anomaly fires."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        sidecar_path, labels = self._make_sidecar(tmp_path, 10)
        fired_labels = labels[:5]  # fire only 5 of 10 -> 5 unfired = 50%

        base_time = datetime.now(UTC)
        runs = [
            _make_run(
                base_time,
                i,
                outcome=fired_labels[i % len(fired_labels)],
                sidecar_mode="normal",
                sidecar_path=str(sidecar_path),
                sidecar_sha256="abc123",
            )
            for i in range(10)
        ]
        catalog_dir = _build_catalog(tmp_path, runs)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        anomalies = result["audit_results"]["test_project"]["anomalies"]

        assert signals["unfired_branches"] == pytest.approx(5 / 10)
        assert any("unfired_branches" in a for a in anomalies)


def _patch_warm_metadata(catalog_dir, run_ids, metadata_json):
    """Update the metadata column in the warm DuckDB for the given run IDs.

    The cool-tier Parquet schema does not carry a ``metadata`` column, so
    ``compact()`` always leaves ``metadata = '{}'`` in the warm DB.  Tests
    that exercise schema-overflow logic must call this helper after compaction
    to inject the desired metadata values.

    Args:
        catalog_dir: Path to the catalog directory (contains bathos.db).
        run_ids: Iterable of run IDs to patch.
        metadata_json: JSON string to write into the ``metadata`` column.
    """
    import duckdb as _duckdb

    db_path = catalog_dir / "bathos.db"
    con = _duckdb.connect(str(db_path))
    for rid in run_ids:
        con.execute("UPDATE runs SET metadata = ? WHERE id = ?", [metadata_json, rid])
    con.close()


class TestSchemaOverflowBoundary:
    """Boundary tests for schema_overflow_rate signal (threshold 0.20)."""

    def _make_sidecar_with_keys(self, path, keys):
        """Write a sidecar declaring given result_schema keys."""
        schema_lines = "\n".join(f'{k} = "float"' for k in keys)
        content = (
            '[experiment]\nhypothesis = "h"\n\n'
            '[outcomes.pass]\ncondition = "x > 0"\ndecision = "d"\n\n'
            f"[result_schema]\n{schema_lines}\n"
        )
        path.write_text(content)

    def test_schema_overflow_below_threshold(self, monkeypatch_registry, tmp_path):
        """10 sidecar runs, all metadata keys declared -> rate = 0.0, no anomaly."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        sidecar_path = tmp_path / "exp.bth.toml"
        self._make_sidecar_with_keys(sidecar_path, ["temp_std", "temp_mean"])

        base_time = datetime.now(UTC)
        runs = [
            _make_run(
                base_time,
                i,
                sidecar_mode="normal",
                sidecar_path=str(sidecar_path),
                sidecar_sha256="abc123",
            )
            for i in range(10)
        ]
        catalog_dir = _build_catalog(tmp_path, runs)
        # All metadata keys are declared in sidecar; inject after compaction
        _patch_warm_metadata(catalog_dir, [r.id for r in runs], '{"temp_std": 1.0, "temp_mean": 300.0}')
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        anomalies = result["audit_results"]["test_project"]["anomalies"]

        assert signals["schema_overflow_rate"] == pytest.approx(0.0)
        assert not any("schema_overflow_rate" in a for a in anomalies)

    def test_schema_overflow_above_threshold(self, monkeypatch_registry, tmp_path):
        """10 sidecar runs, 3 have undeclared keys (30%) -> anomaly fires."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        sidecar_path = tmp_path / "exp.bth.toml"
        self._make_sidecar_with_keys(sidecar_path, ["temp_std"])

        base_time = datetime.now(UTC)
        overflow_runs = [
            _make_run(
                base_time,
                i,
                sidecar_mode="normal",
                sidecar_path=str(sidecar_path),
                sidecar_sha256="abc123",
            )
            for i in range(3)
        ]
        clean_runs = [
            _make_run(
                base_time,
                i + 3,
                sidecar_mode="normal",
                sidecar_path=str(sidecar_path),
                sidecar_sha256="abc123",
            )
            for i in range(7)
        ]
        catalog_dir = _build_catalog(tmp_path, overflow_runs + clean_runs)
        # Inject overflow metadata for 3 runs (undeclared key), clean for the rest
        _patch_warm_metadata(catalog_dir, [r.id for r in overflow_runs], '{"temp_std": 1.0, "debug_flag": 42}')
        _patch_warm_metadata(catalog_dir, [r.id for r in clean_runs], '{"temp_std": 1.0}')
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        anomalies = result["audit_results"]["test_project"]["anomalies"]

        assert signals["schema_overflow_rate"] == pytest.approx(3 / 10)
        assert any("schema_overflow_rate" in a for a in anomalies)


class TestPostHocBiasFlagBoundary:
    """Boundary tests for post_hoc_bias_flag (worst in first third > 10% total)."""

    def test_post_hoc_bias_not_flagged(self, monkeypatch_registry, tmp_path):
        """12 runs, 'fail' only in last third -> flag = False."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        base_time = datetime.now(UTC)
        # first third = indices 0-3 (4 runs), all "pass"
        # "fail" at indices 10,11 only -> early_worst_count = 0
        # 0 is not > 0.1 * 12 = 1.2
        outcomes = ["pass"] * 10 + ["fail"] * 2
        runs = [_make_run(base_time, i, outcome=outcomes[i]) for i in range(12)]
        catalog_dir = _build_catalog(tmp_path, runs)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]

        assert signals["post_hoc_bias_flag"] is False

    def test_post_hoc_bias_flagged(self, monkeypatch_registry, tmp_path):
        """12 runs, 2 'fail' in first 4 (first third) -> 2/12 = 16.7% > 10%, flag = True."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        base_time = datetime.now(UTC)
        # first third = indices 0-3 (4 runs): 2 "fail" there
        # 2 > 0.1 * 12 = 1.2 -> flag = True
        outcomes = ["fail", "fail", "pass", "pass"] + ["pass"] * 8
        runs = [_make_run(base_time, i, outcome=outcomes[i]) for i in range(12)]
        catalog_dir = _build_catalog(tmp_path, runs)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        anomalies = result["audit_results"]["test_project"]["anomalies"]

        assert signals["post_hoc_bias_flag"] is True
        assert any("post_hoc_bias_flag" in a for a in anomalies)


# ---------------------------------------------------------------------------
# Part B: schema_overflow_rate semantic tests (4 tests)
# ---------------------------------------------------------------------------


class TestSchemaOverflowSemantics:
    """Semantic tests for _load_sidecar_schema_keys and undeclared-key detection."""

    def _write_sidecar(self, path, schema_keys):
        """Write a minimal sidecar declaring the given result_schema keys."""
        schema_lines = "\n".join(f'{k} = "float"' for k in schema_keys)
        content = (
            '[experiment]\nhypothesis = "h"\n\n'
            '[outcomes.pass]\ncondition = "x > 0"\ndecision = "d"\n\n'
            f"[result_schema]\n{schema_lines}\n"
        )
        path.write_text(content)

    def test_schema_overflow_declared_keys_only(self, monkeypatch_registry, tmp_path):
        """metadata = {temp_std: 1.0} with sidecar declaring temp_std -> overflow_rate == 0.0."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        sidecar_path = tmp_path / "exp.bth.toml"
        self._write_sidecar(sidecar_path, ["temp_std"])

        base_time = datetime.now(UTC)
        runs = [
            _make_run(
                base_time,
                i,
                sidecar_mode="normal",
                sidecar_path=str(sidecar_path),
                sidecar_sha256="abc",
            )
            for i in range(5)
        ]
        catalog_dir = _build_catalog(tmp_path, runs)
        # Inject declared-only metadata after compaction (metadata not in cool tier)
        _patch_warm_metadata(catalog_dir, [r.id for r in runs], '{"temp_std": 1.0}')
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        assert signals["schema_overflow_rate"] == pytest.approx(0.0)

    def test_schema_overflow_undeclared_key(self, monkeypatch_registry, tmp_path):
        """metadata has undeclared_debug not in sidecar -> overflow_rate > 0."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        sidecar_path = tmp_path / "exp.bth.toml"
        self._write_sidecar(sidecar_path, ["temp_std"])

        base_time = datetime.now(UTC)
        runs = [
            _make_run(
                base_time,
                i,
                sidecar_mode="normal",
                sidecar_path=str(sidecar_path),
                sidecar_sha256="abc",
            )
            for i in range(3)
        ]
        catalog_dir = _build_catalog(tmp_path, runs)
        # Inject metadata with undeclared key after compaction (metadata not in cool tier)
        _patch_warm_metadata(catalog_dir, [r.id for r in runs], '{"temp_std": 1.0, "undeclared_debug": 42}')
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        assert signals["schema_overflow_rate"] > 0.0

    def test_schema_overflow_no_sidecar_skipped(self, monkeypatch_registry, tmp_path):
        """All runs have no sidecar_path -> denominator = 0, overflow_rate == 0.0."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        base_time = datetime.now(UTC)
        runs = [
            _make_run(
                base_time,
                i,
                metadata='{"temp_std": 1.0, "undeclared": 99}',
                sidecar_mode="normal",
                sidecar_path="",
            )
            for i in range(5)
        ]
        catalog_dir = _build_catalog(tmp_path, runs)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        # No runs have sidecar_path -> runs_with_sidecar = 0 -> rate = 0.0
        assert signals["schema_overflow_rate"] == pytest.approx(0.0)

    def test_schema_overflow_denominator_is_sidecar_runs(self, monkeypatch_registry, tmp_path):
        """5 sidecar runs (all clean) + 5 no-sidecar runs -> denominator = 5, not 10."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        sidecar_path = tmp_path / "exp.bth.toml"
        self._write_sidecar(sidecar_path, ["temp_std"])

        base_time = datetime.now(UTC)
        runs_with = [
            _make_run(
                base_time,
                i,
                metadata='{"temp_std": 1.0}',
                sidecar_mode="normal",
                sidecar_path=str(sidecar_path),
                sidecar_sha256="abc",
            )
            for i in range(5)
        ]
        # no-sidecar runs have undeclared keys but should be ignored in denominator
        runs_without = [
            _make_run(
                base_time,
                i + 5,
                metadata='{"undeclared_key": 999}',
                sidecar_mode="normal",
                sidecar_path="",
            )
            for i in range(5)
        ]
        catalog_dir = _build_catalog(tmp_path, runs_with + runs_without)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        # denominator = 5 (sidecar runs), overflow_count = 0 -> rate = 0.0
        assert signals["schema_overflow_rate"] == pytest.approx(0.0)


class TestControlArmRate:
    """Test control_arm_rate signal (AC-5)."""

    def test_control_arm_rate_zero_no_ctrl_runs(self, monkeypatch_registry, tmp_path):
        """Test control_arm_rate is 0 when no runs have ctrl_* outcomes."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        catalog_dir = tmp_path / "test_catalog"
        catalog_dir.mkdir()
        init_catalog(catalog_dir)

        base_time = datetime.now(UTC)
        runs = [
            Run(
                project_slug="test_project",
                command="python test.py",
                argv=["python", "test.py"],
                git_hash="abc123",
                git_branch="main",
                git_dirty=False,
                timestamp=base_time + timedelta(seconds=i),
                status="completed",
                exit_code=0,
                outcome="pass",
                sidecar_mode="normal",
                outcome_is_residual=False,
            )
            for i in range(5)
        ]

        for r in runs:
            write_run(r, catalog_dir)
        compact(catalog_dir)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        assert signals["control_arm_rate"] == pytest.approx(0.0)

    def test_control_arm_rate_nonzero_with_ctrl_passes(self, monkeypatch_registry, tmp_path):
        """Test control_arm_rate > 0 when runs have ctrl_pass outcomes."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        catalog_dir = tmp_path / "test_catalog"
        catalog_dir.mkdir()
        init_catalog(catalog_dir)

        base_time = datetime.now(UTC)
        runs = [
            Run(
                project_slug="test_project",
                command="python test.py",
                argv=["python", "test.py"],
                git_hash="abc123",
                git_branch="main",
                git_dirty=False,
                timestamp=base_time + timedelta(seconds=i),
                status="completed",
                exit_code=0,
                outcome="ctrl_pass" if i < 2 else "pass",
                sidecar_mode="normal",
                outcome_is_residual=False,
            )
            for i in range(5)
        ]

        for r in runs:
            write_run(r, catalog_dir)
        compact(catalog_dir)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        # 2 out of 5 have ctrl_* outcomes
        assert signals["control_arm_rate"] == pytest.approx(0.4)

    def test_control_arm_rate_mixed_ctrl_outcomes(self, monkeypatch_registry, tmp_path):
        """Test control_arm_rate counts both ctrl_pass and ctrl_fail."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        catalog_dir = tmp_path / "test_catalog"
        catalog_dir.mkdir()
        init_catalog(catalog_dir)

        base_time = datetime.now(UTC)
        outcomes = [
            "ctrl_pass", "ctrl_pass", "ctrl_fail", "pass", "pass"
        ]
        runs = [
            Run(
                project_slug="test_project",
                command="python test.py",
                argv=["python", "test.py"],
                git_hash="abc123",
                git_branch="main",
                git_dirty=False,
                timestamp=base_time + timedelta(seconds=i),
                status="completed",
                exit_code=0,
                outcome=outcomes[i],
                sidecar_mode="normal",
                outcome_is_residual=False,
            )
            for i in range(5)
        ]

        for r in runs:
            write_run(r, catalog_dir)
        compact(catalog_dir)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        # 3 out of 5 have ctrl_* outcomes
        assert signals["control_arm_rate"] == pytest.approx(0.6)

    def test_control_arm_rate_warning_zero_with_validation_stage(self, monkeypatch_registry, tmp_path):
        """Test WARNING when control_arm_rate==0.0 and validation/production runs exist."""
        from bathos.config import register_project
        from bathos.sprint_audit import sprint_audit

        catalog_dir = tmp_path / "test_catalog"
        catalog_dir.mkdir()
        init_catalog(catalog_dir)

        base_time = datetime.now(UTC)
        runs = [
            Run(
                project_slug="test_project",
                command="python test.py",
                argv=["python", "test.py"],
                git_hash="abc123",
                git_branch="main",
                git_dirty=False,
                timestamp=base_time + timedelta(seconds=i),
                status="completed",
                exit_code=0,
                outcome="pass",
                sidecar_mode="normal",
                outcome_is_residual=False,
                stage_name="validation",  # AC-0: stage_name populated
            )
            for i in range(3)
        ]

        for r in runs:
            write_run(r, catalog_dir)
        compact(catalog_dir)
        register_project(slug="test_project", catalog_dir=catalog_dir)

        result = sprint_audit(hours=24)
        signals = result["audit_results"]["test_project"]["signals"]
        anomalies = result["audit_results"]["test_project"]["anomalies"]

        # Rate should be 0.0 (no ctrl_* runs)
        assert signals["control_arm_rate"] == pytest.approx(0.0)
        # Should have a warning about control arm rate
        assert any("control_arm_rate" in str(a) for a in anomalies)
