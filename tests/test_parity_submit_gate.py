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
    """Write a passing parity run record to the cool tier (Parquet).

    Uses parity_run_type as a first-class column (v9 schema), NOT the metadata JSON blob.
    """
    import pyarrow.parquet as pq
    import pyarrow as pa
    from bathos.catalog import init_catalog, write_run
    from bathos.compact import compact
    from bathos.schema import Run
    from datetime import UTC, datetime

    # Use the real Run→write_run path so the cool Parquet has the correct v9 schema
    init_catalog(tmp_path)
    run = Run(
        project_slug="test_proj",
        command=f"scripts/validation/parity_validate.py {stem}",
        argv=["scripts/validation/parity_validate.py", stem],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
        status="completed",
        exit_code=0,
        outcome="pass",
        parity_run_type="literature_parity",  # real column, not metadata JSON
    )
    write_run(run, tmp_path)


def _write_passing_parity_run_to_warm(
    catalog_dir: Path,
    stem: str = "baseline_parity",
) -> None:
    """Write a passing parity run into the warm DuckDB (for tests that need warm-path check)."""
    from bathos.catalog import init_catalog, write_run
    from bathos.compact import compact
    from bathos.schema import Run
    from datetime import UTC, datetime

    init_catalog(catalog_dir)
    run = Run(
        project_slug="test_proj",
        command=f"scripts/validation/parity_validate.py {stem}",
        argv=["scripts/validation/parity_validate.py", stem],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
        status="completed",
        exit_code=0,
        outcome="pass",
        parity_run_type="literature_parity",
    )
    write_run(run, catalog_dir)
    compact(catalog_dir)


class TestParitySubmitGate:
    """F3 submit-gate tests."""

    def _write_non_parity_cool_fragment(self, catalog_dir):
        """Write a regular (non-parity) cool-tier fragment so the gate can scan a readable file.

        This ensures fragments_read_ok >= 1 (determinable: no match), which is required
        for AC-09/AC-10 to produce satisfied=False rather than the AC-22 fail-open (satisfied=None).
        """
        from bathos.catalog import init_catalog, write_run
        from bathos.schema import Run
        from datetime import UTC, datetime

        init_catalog(catalog_dir)
        run = Run(
            project_slug="test_proj",
            command="scripts/experiments/regular_experiment.py",
            argv=["scripts/experiments/regular_experiment.py"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
            timestamp=datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC),
            status="completed",
            exit_code=0,
            outcome="pass",
            parity_run_type=None,  # not a parity run
        )
        write_run(run, catalog_dir)

    def test_ac09_validation_tier_unmet_parity_prereq_hard_blocks(
        self, tmp_path, monkeypatch
    ):
        """AC-09: validation tier with unmet parity prereq -> hard block (non-zero exit).

        Scenario: warm DB absent, cool tier has readable fragments but no parity run.
        fragments_read_ok >= 1 → determinable (satisfied=False) → tier_enforced=True for validation.
        """
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

        # Write a non-parity cool fragment so the gate can scan (fragments_read_ok >= 1)
        # Without this, the gate returns satisfied=None (AC-22 fail-open), not satisfied=False.
        self._write_non_parity_cool_fragment(catalog_dir)

        parsed_sidecar = parse_sidecar(sidecar_path)

        # Call the gate
        result = check_parity_confounds_for_submit(parsed_sidecar, catalog_dir)

        # Should be NOT satisfied (hard block condition)
        assert result["satisfied"] is False
        assert result["tier_enforced"] is True  # validation tier = enforced

    def test_ac10_exploration_tier_unmet_parity_prereq_advisory(
        self, tmp_path, monkeypatch
    ):
        """AC-10: exploration tier with unmet parity prereq -> advisory (no hard block).

        Scenario: warm DB absent, cool tier has readable fragments but no parity run.
        fragments_read_ok >= 1 → determinable (satisfied=False) → tier_enforced=False for exploration.
        """
        from bathos.parity import check_parity_confounds_for_submit
        from bathos.sidecar import parse_sidecar

        monkeypatch.chdir(tmp_path)
        catalog_dir = tmp_path / ".bth" / "catalog"
        catalog_dir.mkdir(parents=True)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

        # Write exploration-tier sidecar with parity prereq
        sidecar_path = _write_sidecar_with_parity_prereq(
            tmp_path,
            stage_name="exploration",
            requires_parity_stem="baseline_parity",
        )

        # Write a non-parity cool fragment so the gate can scan (fragments_read_ok >= 1)
        self._write_non_parity_cool_fragment(catalog_dir)

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


class TestF3CoolToWarmRoundTrip:
    """F3 gate: parity_run_type column survives cool→warm and is used by the warm-path query.

    These tests verify that the F3 gate reads the parity_run_type COLUMN (not metadata JSON)
    in both the warm and cool fallback paths.
    """

    def test_f3_satisfied_via_warm_column_path(self, tmp_path, monkeypatch):
        """F3 gate satisfied when warm DB has a run with parity_run_type column set."""
        from bathos.parity import check_parity_confounds_for_submit
        from bathos.sidecar import parse_sidecar

        monkeypatch.chdir(tmp_path)
        catalog_dir = tmp_path / ".bth" / "catalog"
        catalog_dir.mkdir(parents=True)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

        # Write parity run through the real cool→warm path
        _write_passing_parity_run_to_warm(catalog_dir, stem="baseline_parity")

        sidecar_path = _write_sidecar_with_parity_prereq(
            tmp_path, stage_name="validation", requires_parity_stem="baseline_parity"
        )
        parsed_sidecar = parse_sidecar(sidecar_path)

        result = check_parity_confounds_for_submit(parsed_sidecar, catalog_dir)

        assert result["satisfied"] is True, (
            "F3 warm-path should find the parity run via the parity_run_type column, "
            f"but got satisfied={result['satisfied']!r}"
        )
        assert result["tier_enforced"] is False

    def test_f3_satisfied_via_cool_column_path(self, tmp_path, monkeypatch):
        """F3 gate satisfied when cool-tier has a run with parity_run_type column set (warm absent)."""
        from bathos.parity import check_parity_confounds_for_submit
        from bathos.sidecar import parse_sidecar

        monkeypatch.chdir(tmp_path)
        catalog_dir = tmp_path / ".bth" / "catalog"
        catalog_dir.mkdir(parents=True)
        # Do NOT create bathos.db — force cool-tier fallback
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

        # Write cool-tier fragment with real parity_run_type column
        _write_passing_parity_run(catalog_dir, stem="baseline_parity")

        sidecar_path = _write_sidecar_with_parity_prereq(
            tmp_path, stage_name="validation", requires_parity_stem="baseline_parity"
        )
        parsed_sidecar = parse_sidecar(sidecar_path)

        result = check_parity_confounds_for_submit(parsed_sidecar, catalog_dir)

        assert result["satisfied"] is True, (
            "F3 cool-path should find the parity run via the parity_run_type column, "
            f"but got satisfied={result['satisfied']!r}"
        )
        assert result["tier_enforced"] is False


class TestAC22Variants:
    """AC-22 restructured behavior: fragments_read_ok tracking.

    (a) Post-v9 cool fragment present, column readable, no parity match, validation tier
        → satisfied=False, tier_enforced=True (determinable hard block)
    (b) Warm absent AND no readable cool fragments → satisfied=None, tier_enforced=False (fail open)
    """

    def test_ac22a_readable_fragment_no_match_validation_is_hard_block(
        self, tmp_path, monkeypatch
    ):
        """AC-22(a): cool fragment readable with parity_run_type column, no match → determinable, validation = hard block."""
        from bathos.parity import check_parity_confounds_for_submit
        from bathos.sidecar import parse_sidecar
        import pyarrow as pa
        import pyarrow.parquet as pq
        from bathos.schema import COOL_SCHEMA

        monkeypatch.chdir(tmp_path)
        catalog_dir = tmp_path / ".bth" / "catalog"
        catalog_dir.mkdir(parents=True)
        # No warm DB — force cool-tier fallback
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

        # Write a cool fragment with parity_run_type column but NO matching parity run
        # (parity_run_type=None → not a parity run)
        from bathos.catalog import init_catalog, write_run
        from bathos.schema import Run
        from datetime import UTC, datetime

        init_catalog(catalog_dir)
        run = Run(
            project_slug="test_proj",
            command="scripts/experiments/run_something.py",
            argv=["scripts/experiments/run_something.py"],
            git_hash="abc123",
            git_branch="main",
            git_dirty=False,
            timestamp=datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC),
            status="completed",
            exit_code=0,
            outcome="pass",
            parity_run_type=None,  # not a parity run
        )
        write_run(run, catalog_dir)

        sidecar_path = _write_sidecar_with_parity_prereq(
            tmp_path, stage_name="validation", requires_parity_stem="baseline_parity"
        )
        parsed_sidecar = parse_sidecar(sidecar_path)

        result = check_parity_confounds_for_submit(parsed_sidecar, catalog_dir)

        # Fragment was readable (fragments_read_ok >= 1), no parity match
        # → satisfied=False, tier_enforced=True for validation (determinable hard block)
        assert result["satisfied"] is False, (
            f"Expected satisfied=False (no parity match found in readable fragment), got {result['satisfied']!r}"
        )
        assert result["tier_enforced"] is True, (
            f"Expected tier_enforced=True for validation tier with determinable no-match, got {result['tier_enforced']!r}"
        )

    def test_ac22b_no_readable_fragments_fails_open(self, tmp_path, monkeypatch):
        """AC-22(b): warm absent AND no cool fragments → satisfied=None, tier_enforced=False."""
        from bathos.parity import check_parity_confounds_for_submit
        from bathos.sidecar import parse_sidecar

        monkeypatch.chdir(tmp_path)
        catalog_dir = tmp_path / ".bth" / "catalog"
        catalog_dir.mkdir(parents=True)
        # No warm DB, no cool-tier fragments
        monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

        sidecar_path = _write_sidecar_with_parity_prereq(
            tmp_path, stage_name="validation", requires_parity_stem="baseline_parity"
        )
        parsed_sidecar = parse_sidecar(sidecar_path)

        result = check_parity_confounds_for_submit(parsed_sidecar, catalog_dir)

        # No readable fragments → unsearchable → fail open
        assert result["satisfied"] is None, (
            f"Expected satisfied=None (no readable fragments, unsearchable), got {result['satisfied']!r}"
        )
        assert result["tier_enforced"] is False, (
            f"Expected tier_enforced=False (fail open per AC-22), got {result['tier_enforced']!r}"
        )
