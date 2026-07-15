import textwrap
from pathlib import Path

import pytest


def _write_toml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "run_test.bth.toml"
    p.write_text(textwrap.dedent(content))
    return p


def _write_valid_sidecar(tmp_path: Path, script_stem: str = "run_test") -> Path:
    """Write a valid experiment sidecar for testing."""
    p = tmp_path / f"{script_stem}.bth.toml"
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
    return p


def test_resolve_sidecar_found(tmp_path):
    from bathos.prereg import resolve_sidecar

    script = tmp_path / "run_nvt.py"
    script.touch()
    sidecar = _write_valid_sidecar(tmp_path, "run_nvt")

    bundle = resolve_sidecar(script)
    assert bundle.found is True
    assert bundle.path == sidecar.resolve()
    assert len(bundle.sha256) == 64
    assert bundle.generated is False


def test_resolve_sidecar_not_found(tmp_path):
    from bathos.prereg import resolve_sidecar

    script = tmp_path / "run_nvt.py"
    script.touch()

    bundle = resolve_sidecar(script)
    assert bundle.found is False
    assert bundle.path is None
    assert bundle.sha256 == ""
    assert bundle.generated is False


def test_resolve_agent_mode_cli_precedence():
    from bathos.prereg import resolve_agent_mode

    mode = resolve_agent_mode(
        cli_flag="autonomous",
        sidecar=None,
        project_config=None,
        global_config=None,
    )
    assert mode == "autonomous"


def test_resolve_agent_mode_sidecar_precedence(tmp_path):
    from bathos.sidecar import parse_sidecar
    from bathos.prereg import resolve_agent_mode

    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "h"
        agent_mode = "autonomous"
        [outcomes.pass]
        condition = "value > 0"
        decision = "proceed"
        reasoning = "Good"
        [outcomes.fallback]
        condition = "TRUE"
        decision = "review"
        reasoning = "Fallback"
        is_residual = true
        [result_schema]
        value = "float"
    """)
    sidecar = parse_sidecar(path)

    mode = resolve_agent_mode(
        cli_flag=None,
        sidecar=sidecar,
        project_config=None,
        global_config=None,
    )
    assert mode == "autonomous"


def test_resolve_agent_mode_default_collaborative():
    from bathos.prereg import resolve_agent_mode

    mode = resolve_agent_mode(
        cli_flag=None,
        sidecar=None,
        project_config=None,
        global_config=None,
    )
    assert mode == "collaborative"


def test_gate_check_ungated_dir_passes(tmp_path):
    from bathos.prereg import gate_check, resolve_sidecar

    script = tmp_path / "scripts" / "scratch" / "explore_data.py"
    script.parent.mkdir(parents=True)
    script.touch()

    bundle = resolve_sidecar(script)
    result = gate_check(script, bundle, "collaborative")

    assert result.ok is True
    assert result.error_payload is None


def test_gate_check_missing_sidecar_fails(tmp_path):
    from bathos.prereg import gate_check, resolve_sidecar, GateErrorCode

    script = tmp_path / "scripts" / "experiments" / "run_nvt.py"
    script.parent.mkdir(parents=True)
    script.touch()

    bundle = resolve_sidecar(script)
    result = gate_check(script, bundle, "collaborative")

    assert result.ok is False
    assert result.error_payload is not None
    assert result.error_payload.error_code == GateErrorCode.SIDECAR_MISSING
    assert "No sidecar found" in result.error_payload.errors[0]


def test_gate_check_valid_sidecar_passes(tmp_path):
    from bathos.prereg import gate_check, resolve_sidecar

    script = tmp_path / "scripts" / "experiments" / "run_nvt.py"
    script.parent.mkdir(parents=True)
    script.touch()

    _write_valid_sidecar(script.parent, "run_nvt")

    bundle = resolve_sidecar(script)
    result = gate_check(script, bundle, "collaborative")

    assert result.ok is True
    assert result.validation is not None
    assert result.validation.ok is True
    assert result.error_payload is None


def test_gate_check_invalid_sidecar_fails(tmp_path):
    from bathos.prereg import gate_check, resolve_sidecar, GateErrorCode

    script = tmp_path / "scripts" / "experiments" / "run_nvt.py"
    script.parent.mkdir(parents=True)
    script.touch()

    p = script.parent / "run_nvt.bth.toml"
    p.write_text(textwrap.dedent("""
        [experiment]
        hypothesis = "h"
        [outcomes.pass]
        condition = "value > 0"
        decision = "proceed"
        [result_schema]
        value = "float"
    """))

    bundle = resolve_sidecar(script)
    result = gate_check(script, bundle, "collaborative")

    assert result.ok is False
    assert result.error_payload is not None
    assert result.error_payload.error_code == GateErrorCode.SIDECAR_INVALID
    assert any("reasoning" in e.lower() for e in result.error_payload.errors)


def test_check_first_of_kind_no_prior_runs(tmp_path):
    from bathos.prereg import check_first_of_kind

    script = tmp_path / "scripts" / "experiments" / "run_nvt.py"
    script.parent.mkdir(parents=True)
    script.touch()

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    result = check_first_of_kind(script, catalog_dir, "abc123")
    assert result is True


def test_check_first_of_kind_prior_run_same_hash(tmp_path):
    from bathos.schema import Run
    from bathos.catalog import write_run
    from bathos.prereg import check_first_of_kind

    script = tmp_path / "scripts" / "experiments" / "run_nvt.py"
    script.parent.mkdir(parents=True)
    script.touch()

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    run = Run(
        project_slug="test",
        command=f"python {script}",
        argv=["python", str(script)],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
    )
    write_run(run, catalog_dir)

    result = check_first_of_kind(script, catalog_dir, "abc123")
    assert result is False


def test_check_first_of_kind_prior_run_different_hash(tmp_path):
    from bathos.schema import Run
    from bathos.catalog import write_run
    from bathos.prereg import check_first_of_kind

    script = tmp_path / "scripts" / "experiments" / "run_nvt.py"
    script.parent.mkdir(parents=True)
    script.touch()

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    run = Run(
        project_slug="test",
        command=f"python {script}",
        argv=["python", str(script)],
        git_hash="old_hash_xyz",
        git_branch="main",
        git_dirty=False,
    )
    write_run(run, catalog_dir)

    result = check_first_of_kind(script, catalog_dir, "new_hash_abc123")
    assert result is True


def test_check_sidecar_drift_no_prior_runs(tmp_path):
    from bathos.prereg import check_sidecar_drift

    script = tmp_path / "scripts" / "experiments" / "run_nvt.py"
    script.parent.mkdir(parents=True)
    script.touch()

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    result = check_sidecar_drift(script, catalog_dir, "current_sha_abc123")
    assert result is False


def test_check_sidecar_drift_matches_first_run(tmp_path):
    from bathos.schema import Run
    from bathos.catalog import write_run
    from bathos.prereg import check_sidecar_drift

    script = tmp_path / "scripts" / "experiments" / "run_nvt.py"
    script.parent.mkdir(parents=True)
    script.touch()

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    run = Run(
        project_slug="test",
        command=f"python {script}",
        argv=["python", str(script)],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        sidecar_sha256="stable_sha_abc",
    )
    write_run(run, catalog_dir)

    result = check_sidecar_drift(script, catalog_dir, "stable_sha_abc")
    assert result is False


def test_check_sidecar_drift_diverges_from_first_run(tmp_path):
    from bathos.schema import Run
    from bathos.catalog import write_run
    from bathos.prereg import check_sidecar_drift

    script = tmp_path / "scripts" / "experiments" / "run_nvt.py"
    script.parent.mkdir(parents=True)
    script.touch()

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    run = Run(
        project_slug="test",
        command=f"python {script}",
        argv=["python", str(script)],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        sidecar_sha256="original_sha_abc",
    )
    write_run(run, catalog_dir)

    result = check_sidecar_drift(script, catalog_dir, "edited_sha_xyz")
    assert result is True


def test_check_sidecar_drift_ignores_runs_without_sidecar_hash(tmp_path):
    """A prior run recorded before B2-04 (no sidecar_sha256 yet) is not a valid baseline."""
    from bathos.schema import Run
    from bathos.catalog import write_run
    from bathos.prereg import check_sidecar_drift

    script = tmp_path / "scripts" / "experiments" / "run_nvt.py"
    script.parent.mkdir(parents=True)
    script.touch()

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    run = Run(
        project_slug="test",
        command=f"python {script}",
        argv=["python", str(script)],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        sidecar_sha256="",
    )
    write_run(run, catalog_dir)

    result = check_sidecar_drift(script, catalog_dir, "current_sha_abc123")
    assert result is False


def test_check_sidecar_drift_empty_current_hash_never_drifts(tmp_path):
    from bathos.schema import Run
    from bathos.catalog import write_run
    from bathos.prereg import check_sidecar_drift

    script = tmp_path / "scripts" / "experiments" / "run_nvt.py"
    script.parent.mkdir(parents=True)
    script.touch()

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    run = Run(
        project_slug="test",
        command=f"python {script}",
        argv=["python", str(script)],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        sidecar_sha256="original_sha_abc",
    )
    write_run(run, catalog_dir)

    result = check_sidecar_drift(script, catalog_dir, "")
    assert result is False


def test_gate_check_sidecar_drift_denies_autonomous(tmp_path):
    from bathos.schema import Run
    from bathos.catalog import write_run
    from bathos.prereg import gate_check, resolve_sidecar, GateErrorCode

    script = tmp_path / "scripts" / "experiments" / "run_nvt.py"
    script.parent.mkdir(parents=True)
    script.touch()
    _write_valid_sidecar(script.parent, "run_nvt")

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    bundle = resolve_sidecar(script)

    run = Run(
        project_slug="test",
        command=f"python {script}",
        argv=["python", str(script)],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        sidecar_sha256="a_prior_first_run_sha_that_differs",
    )
    write_run(run, catalog_dir)

    result = gate_check(script, bundle, "autonomous", catalog_dir=catalog_dir, git_hash="new_hash")

    assert result.ok is False
    assert result.error_payload is not None
    assert result.error_payload.error_code == GateErrorCode.SIDECAR_HASH_MISMATCH
    assert "first-run manifest" in result.error_payload.errors[0]


def test_gate_check_sidecar_drift_warns_but_passes_collaborative(tmp_path, caplog):
    from bathos.schema import Run
    from bathos.catalog import write_run
    from bathos.prereg import gate_check, resolve_sidecar

    script = tmp_path / "scripts" / "experiments" / "run_nvt.py"
    script.parent.mkdir(parents=True)
    script.touch()
    _write_valid_sidecar(script.parent, "run_nvt")

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    bundle = resolve_sidecar(script)

    run = Run(
        project_slug="test",
        command=f"python {script}",
        argv=["python", str(script)],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        sidecar_sha256="a_prior_first_run_sha_that_differs",
    )
    write_run(run, catalog_dir)

    with caplog.at_level("WARNING"):
        result = gate_check(script, bundle, "collaborative", catalog_dir=catalog_dir)

    assert result.ok is True
    assert result.error_payload is None
    assert any("drift" in message.lower() for message in caplog.messages)


def test_gate_check_sidecar_drift_matching_hash_passes_silently(tmp_path):
    from bathos.schema import Run
    from bathos.catalog import write_run
    from bathos.prereg import gate_check, resolve_sidecar

    script = tmp_path / "scripts" / "experiments" / "run_nvt.py"
    script.parent.mkdir(parents=True)
    script.touch()
    _write_valid_sidecar(script.parent, "run_nvt")

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    bundle = resolve_sidecar(script)

    run = Run(
        project_slug="test",
        command=f"python {script}",
        argv=["python", str(script)],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        sidecar_sha256=bundle.sha256,
    )
    write_run(run, catalog_dir)

    result = gate_check(script, bundle, "collaborative", catalog_dir=catalog_dir)

    assert result.ok is True
    assert result.error_payload is None


def test_check_reproduction_prerequisite_cool_tier_found(tmp_path):
    """Test finding a passing run in cool-tier Parquet (no warm DB)."""
    from bathos.prereg import check_reproduction_prerequisite
    from bathos.schema import Run
    from bathos.catalog import write_run

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    # Write a passing run with a specific stem in the command
    run = Run(
        project_slug="test",
        command="python scripts/experiments/alanine_dipeptide.py",
        argv=["python", "scripts/experiments/alanine_dipeptide.py"],
        outcome="pass",
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
    )
    write_run(run, catalog_dir)

    # Check for that stem
    found = check_reproduction_prerequisite("alanine_dipeptide", catalog_dir)
    assert found is True


def test_check_reproduction_prerequisite_cool_tier_not_found(tmp_path):
    """Test not finding a passing run in cool-tier Parquet."""
    from bathos.prereg import check_reproduction_prerequisite
    from bathos.schema import Run
    from bathos.catalog import write_run

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    # Write a failing run with a different stem
    run = Run(
        project_slug="test",
        command="python scripts/experiments/other_script.py",
        argv=["python", "scripts/experiments/other_script.py"],
        outcome="fail",
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
    )
    write_run(run, catalog_dir)

    # Check for a stem that doesn't exist
    found = check_reproduction_prerequisite("alanine_dipeptide", catalog_dir)
    assert found is False


def test_check_reproduction_prerequisite_cool_tier_failing_run_not_matched(tmp_path):
    """Test that a run with matching stem but failing outcome is not matched."""
    from bathos.prereg import check_reproduction_prerequisite
    from bathos.schema import Run
    from bathos.catalog import write_run

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    # Write a failing run with the target stem
    run = Run(
        project_slug="test",
        command="python scripts/experiments/alanine_dipeptide.py",
        argv=["python", "scripts/experiments/alanine_dipeptide.py"],
        outcome="fail",
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
    )
    write_run(run, catalog_dir)

    # Check for that stem (should not find, since outcome is fail)
    found = check_reproduction_prerequisite("alanine_dipeptide", catalog_dir)
    assert found is False


def test_check_reproduction_prerequisite_empty_catalog(tmp_path):
    """Test that an empty catalog returns False."""
    from bathos.prereg import check_reproduction_prerequisite

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    found = check_reproduction_prerequisite("some_stem", catalog_dir)
    assert found is False


def test_check_reproduction_prerequisite_no_catalog_dir(tmp_path):
    """Test that a non-existent catalog directory returns False."""
    from bathos.prereg import check_reproduction_prerequisite

    catalog_dir = tmp_path / "nonexistent_catalog"

    found = check_reproduction_prerequisite("some_stem", catalog_dir)
    assert found is False


def test_verify_run_manifest_matching_hash(tmp_path):
    """A manifest file whose current hash matches the recorded hash verifies True."""
    import hashlib

    from bathos.prereg import verify_run_manifest
    from bathos.schema import Run

    manifest_path = tmp_path / "run_test.abc123.bth.lock.toml"
    manifest_path.write_text('sidecar_sha256 = "abc"\n')
    actual_sha256 = hashlib.sha256(manifest_path.read_bytes()).hexdigest()

    run = Run(
        project_slug="p",
        command="c",
        argv=["c"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        manifest_path=str(manifest_path),
        manifest_sha256=actual_sha256,
    )
    assert verify_run_manifest(run) is True


def test_verify_run_manifest_tampered_file(tmp_path):
    """A manifest file edited after the recorded hash was taken verifies False."""
    from bathos.prereg import verify_run_manifest
    from bathos.schema import Run

    manifest_path = tmp_path / "run_test.abc123.bth.lock.toml"
    manifest_path.write_text('sidecar_sha256 = "abc"\n')

    run = Run(
        project_slug="p",
        command="c",
        argv=["c"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        manifest_path=str(manifest_path),
        manifest_sha256="not_the_real_hash",
    )
    assert verify_run_manifest(run) is False


def test_verify_run_manifest_missing_file(tmp_path):
    """A recorded manifest_path that no longer exists on disk verifies False."""
    from bathos.prereg import verify_run_manifest
    from bathos.schema import Run

    run = Run(
        project_slug="p",
        command="c",
        argv=["c"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        manifest_path=str(tmp_path / "does_not_exist.bth.lock.toml"),
        manifest_sha256="some_hash",
    )
    assert verify_run_manifest(run) is False


def test_verify_run_manifest_no_manifest_recorded():
    """A run with no manifest recorded at all (manifest_sha256/path empty) verifies False."""
    from bathos.prereg import verify_run_manifest
    from bathos.schema import Run

    run = Run(
        project_slug="p",
        command="c",
        argv=["c"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
    )
    assert run.manifest_sha256 == ""
    assert run.manifest_path == ""
    assert verify_run_manifest(run) is False


def test_check_component_sidecar_drift_no_prior_runs(tmp_path):
    from bathos.prereg import check_component_sidecar_drift

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    result = check_component_sidecar_drift(
        "stage_bundle.preprocess", catalog_dir, "current_sha_abc123"
    )
    assert result is False


def test_check_component_sidecar_drift_matches_first_run(tmp_path):
    from bathos.catalog import write_run
    from bathos.prereg import check_component_sidecar_drift
    from bathos.schema import Run

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    run = Run(
        project_slug="test",
        command="python run.py",
        argv=["python", "run.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        component_id="stage_bundle.preprocess",
        component_sidecar_sha256="stable_sha_abc",
    )
    write_run(run, catalog_dir)

    result = check_component_sidecar_drift(
        "stage_bundle.preprocess", catalog_dir, "stable_sha_abc"
    )
    assert result is False


def test_check_component_sidecar_drift_diverges_from_first_run(tmp_path):
    from bathos.catalog import write_run
    from bathos.prereg import check_component_sidecar_drift
    from bathos.schema import Run

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    run = Run(
        project_slug="test",
        command="python run.py",
        argv=["python", "run.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        component_id="stage_bundle.preprocess",
        component_sidecar_sha256="original_sha_abc",
    )
    write_run(run, catalog_dir)

    result = check_component_sidecar_drift(
        "stage_bundle.preprocess", catalog_dir, "edited_sha_xyz"
    )
    assert result is True


def test_check_component_sidecar_drift_ignores_other_components(tmp_path):
    """A prior run recorded under a DIFFERENT component_id is not a valid baseline."""
    from bathos.catalog import write_run
    from bathos.prereg import check_component_sidecar_drift
    from bathos.schema import Run

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    run = Run(
        project_slug="test",
        command="python run.py",
        argv=["python", "run.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        component_id="stage_bundle.other_stage",
        component_sidecar_sha256="original_sha_abc",
    )
    write_run(run, catalog_dir)

    result = check_component_sidecar_drift(
        "stage_bundle.preprocess", catalog_dir, "current_sha_abc123"
    )
    assert result is False


def test_check_component_sidecar_drift_empty_current_hash_never_drifts(tmp_path):
    from bathos.catalog import write_run
    from bathos.prereg import check_component_sidecar_drift
    from bathos.schema import Run

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    run = Run(
        project_slug="test",
        command="python run.py",
        argv=["python", "run.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        component_id="stage_bundle.preprocess",
        component_sidecar_sha256="original_sha_abc",
    )
    write_run(run, catalog_dir)

    result = check_component_sidecar_drift("stage_bundle.preprocess", catalog_dir, "")
    assert result is False


def test_check_component_sidecar_drift_empty_component_id_never_drifts(tmp_path):
    from bathos.prereg import check_component_sidecar_drift

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    result = check_component_sidecar_drift("", catalog_dir, "current_sha_abc123")
    assert result is False
