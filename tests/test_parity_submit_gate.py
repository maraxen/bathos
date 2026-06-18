"""Tests for F3 submit-gate: parity confound checking.

AC-09: validation/production tier with unmet parity prereq -> hard block
AC-10: exploration/calibration tier with unmet parity prereq -> advisory only
AC-22: warm DB absent (simulate) -> fail open (advisory)
Plus: satisfied case (passing parity run exists) -> gate passes for all tiers
"""
import json
import textwrap
from pathlib import Path

import pytest


def _write_sidecar_with_parity_prereq(
    tmp_path: Path,
    script_stem: str = "run_test",
    stage_name: str = "exploration",
    requires_parity_stem: str = "baseline_parity",
) -> Path:
    """Write an experiment sidecar with parity prerequisite."""
    p = tmp_path / f"{script_stem}.bth.toml"
    p.write_text(textwrap.dedent(f"""
        [experiment]
        hypothesis = "Test hypothesis"
        stage_name = "{stage_name}"
        [reproduction]
        requires_parity_stem = "{requires_parity_stem}"
        [outcomes.pass]
        condition = "value > 0"
        decision = "proceed"
        reasoning = "Good value"
        [outcomes.fallback]
        condition = "TRUE"
        decision = "review"
        reasoning = "Catch-all outcome"
        is_residual = true
        [result_schema]
        value = "float"
    """))
    return p


def _write_passing_parity_run(
    tmp_path: Path,
    stem: str = "baseline_parity",
) -> None:
    """Write a passing parity run record to the cool tier (Parquet)."""
    import pyarrow.parquet as pq
    import pyarrow as pa

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)

    # Create a Parquet file with a passing parity run
    table = pa.table({
        "id": ["run_001"],
        "command": [f"scripts/validation/parity_validate.py {stem}"],
        "outcome": ["pass"],
        "metadata": [json.dumps({"parity_run_type": "literature_parity"})],
    })
    pq.write_table(table, str(runs_dir / "parity_run_001.parquet"))


class TestParitySubmitGate:
    """F3 submit-gate tests."""

    def test_ac09_validation_tier_unmet_parity_prereq_hard_blocks(
        self, tmp_path, monkeypatch
    ):
        """AC-09: validation tier with unmet parity prereq -> hard block (non-zero exit)."""
        from bathos.parity import check_parity_confounds_for_submit
        from bathos.sidecar import parse_sidecar

        monkeypatch.chdir(tmp_path)
        catalog_dir = tmp_path / ".bth" / "catalog"
        catalog_dir.mkdir(parents=True)
        # Create runs_dir so cool-tier search is possible (but empty)
        (catalog_dir / "runs").mkdir(parents=True)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

        # Write validation-tier sidecar with parity prereq
        sidecar_path = _write_sidecar_with_parity_prereq(
            tmp_path,
            stage_name="validation",
            requires_parity_stem="baseline_parity",
        )

        # Do NOT write a passing parity run
        parsed_sidecar = parse_sidecar(sidecar_path)

        # Call the gate
        result = check_parity_confounds_for_submit(parsed_sidecar, catalog_dir)

        # Should be NOT satisfied (hard block condition)
        assert result["satisfied"] is False
        assert result["tier_enforced"] is True  # validation tier = enforced

    def test_ac10_exploration_tier_unmet_parity_prereq_advisory(
        self, tmp_path, monkeypatch
    ):
        """AC-10: exploration tier with unmet parity prereq -> advisory (no hard block)."""
        from bathos.parity import check_parity_confounds_for_submit
        from bathos.sidecar import parse_sidecar

        monkeypatch.chdir(tmp_path)
        catalog_dir = tmp_path / ".bth" / "catalog"
        catalog_dir.mkdir(parents=True)
        # Create runs_dir so cool-tier search is possible (but empty)
        (catalog_dir / "runs").mkdir(parents=True)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

        # Write exploration-tier sidecar with parity prereq
        sidecar_path = _write_sidecar_with_parity_prereq(
            tmp_path,
            stage_name="exploration",
            requires_parity_stem="baseline_parity",
        )

        # Do NOT write a passing parity run
        parsed_sidecar = parse_sidecar(sidecar_path)

        # Call the gate
        result = check_parity_confounds_for_submit(parsed_sidecar, catalog_dir)

        # Should be NOT satisfied BUT not tier-enforced (advisory only)
        assert result["satisfied"] is False
        assert result["tier_enforced"] is False  # exploration tier = advisory only

    def test_ac22_warm_db_absent_fails_open_advisory(
        self, tmp_path, monkeypatch
    ):
        """AC-22: warm DB absent -> gate fails OPEN (advisory), submit not hard-blocked."""
        from bathos.parity import check_parity_confounds_for_submit
        from bathos.sidecar import parse_sidecar

        monkeypatch.chdir(tmp_path)
        catalog_dir = tmp_path / ".bth" / "catalog"
        catalog_dir.mkdir(parents=True)
        # Do NOT create bathos.db (simulate absent warm tier)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

        sidecar_path = _write_sidecar_with_parity_prereq(
            tmp_path,
            stage_name="validation",
            requires_parity_stem="baseline_parity",
        )

        # Do NOT write a cool-tier run either
        parsed_sidecar = parse_sidecar(sidecar_path)

        # Call the gate
        result = check_parity_confounds_for_submit(parsed_sidecar, catalog_dir)

        # Should be INDETERMINATE (warm absent, cool absent) and NOT tier-enforced
        # (AC-22: fail open = advisory, never hard-block)
        assert result["satisfied"] in (None, False)  # indeterminate or None
        assert result["tier_enforced"] is False  # AC-22: always advisory when indeterminate

    def test_satisfied_case_passing_parity_run_exists(
        self, tmp_path, monkeypatch
    ):
        """Satisfied case: passing parity run exists -> gate passes for all tiers."""
        from bathos.parity import check_parity_confounds_for_submit
        from bathos.sidecar import parse_sidecar

        monkeypatch.chdir(tmp_path)
        catalog_dir = tmp_path / ".bth" / "catalog"
        catalog_dir.mkdir(parents=True)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

        # Write validation-tier sidecar with parity prereq
        sidecar_path = _write_sidecar_with_parity_prereq(
            tmp_path,
            stage_name="validation",
            requires_parity_stem="baseline_parity",
        )

        # Write a passing parity run to cool tier
        _write_passing_parity_run(catalog_dir, stem="baseline_parity")

        parsed_sidecar = parse_sidecar(sidecar_path)

        # Call the gate
        result = check_parity_confounds_for_submit(parsed_sidecar, catalog_dir)

        # Should be satisfied
        assert result["satisfied"] is True
        assert result["tier_enforced"] is False  # satisfied, so enforcement not needed

    def test_no_parity_prereq_declared_passes(
        self, tmp_path, monkeypatch
    ):
        """No parity prereq declared -> gate passes (no check needed)."""
        from bathos.parity import check_parity_confounds_for_submit
        from bathos.sidecar import parse_sidecar

        monkeypatch.chdir(tmp_path)
        catalog_dir = tmp_path / ".bth" / "catalog"
        catalog_dir.mkdir(parents=True)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

        # Write sidecar WITHOUT parity prereq
        p = tmp_path / "run_test.bth.toml"
        p.write_text(textwrap.dedent("""
            [experiment]
            hypothesis = "Test hypothesis"
            [outcomes.pass]
            condition = "value > 0"
            decision = "proceed"
            reasoning = "Good value"
            [outcomes.fallback]
            condition = "TRUE"
            decision = "review"
            reasoning = "Catch-all outcome"
            is_residual = true
            [result_schema]
            value = "float"
        """))

        parsed_sidecar = parse_sidecar(p)

        # Call the gate
        result = check_parity_confounds_for_submit(parsed_sidecar, catalog_dir)

        # Should be satisfied (no check needed)
        assert result["satisfied"] is True
