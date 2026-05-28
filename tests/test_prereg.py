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
