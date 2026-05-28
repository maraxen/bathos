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
