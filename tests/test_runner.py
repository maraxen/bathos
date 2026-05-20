import sys
from pathlib import Path

from bathos.catalog import init_catalog, read_runs
from bathos.runner import run_script


def test_run_records_completed_status(tmp_catalog: Path):
    init_catalog(tmp_catalog)
    exit_code = run_script(
        argv=[sys.executable, "-c", "pass"],
        project_slug="testproj",
        catalog_dir=tmp_catalog,
        output_paths=[],
        tags=[],
    )
    assert exit_code == 0
    runs = read_runs(tmp_catalog)
    assert len(runs) == 1
    assert runs[0].status == "completed"
    assert runs[0].exit_code == 0
    assert runs[0].project_slug == "testproj"


def test_run_records_failed_status(tmp_catalog: Path):
    init_catalog(tmp_catalog)
    exit_code = run_script(
        argv=[sys.executable, "-c", "raise SystemExit(1)"],
        project_slug="testproj",
        catalog_dir=tmp_catalog,
        output_paths=[],
        tags=[],
    )
    assert exit_code == 1
    runs = read_runs(tmp_catalog)
    assert runs[0].status == "failed"
    assert runs[0].exit_code == 1


def test_run_captures_git_hash(tmp_catalog: Path, tmp_path: Path):
    import subprocess

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True
    )
    (tmp_path / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "i"], cwd=tmp_path, check=True, capture_output=True)

    init_catalog(tmp_catalog)
    run_script(
        argv=[sys.executable, "-c", "pass"],
        project_slug="p",
        catalog_dir=tmp_catalog,
        output_paths=[],
        tags=[],
        cwd=tmp_path,
    )
    runs = read_runs(tmp_catalog)
    assert runs[0].git_hash != "unknown"
    assert len(runs[0].git_hash) == 40


def test_run_records_output_paths(tmp_catalog: Path):
    init_catalog(tmp_catalog)
    run_script(
        argv=[sys.executable, "-c", "pass"],
        project_slug="p",
        catalog_dir=tmp_catalog,
        output_paths=["/tmp/result.parquet"],
        tags=["tag1"],
    )
    runs = read_runs(tmp_catalog)
    assert runs[0].output_paths == ["/tmp/result.parquet"]
    assert runs[0].tags == ["tag1"]


def test_run_duration_is_positive(tmp_catalog: Path):
    init_catalog(tmp_catalog)
    run_script(
        argv=[sys.executable, "-c", "import time; time.sleep(0.05)"],
        project_slug="p",
        catalog_dir=tmp_catalog,
        output_paths=[],
        tags=[],
    )
    runs = read_runs(tmp_catalog)
    assert runs[0].duration_s >= 0.05


def test_run_blocks_enforced_dir_without_sidecar(tmp_path):
    """bth run must raise SystemExit(1) if script in enforced dir has no sidecar."""
    import subprocess, sys
    from bathos.runner import run_script

    (tmp_path / "catalog").mkdir()
    enforced = tmp_path / "scripts" / "experiments"
    enforced.mkdir(parents=True)
    script = enforced / "run_nvt.py"
    script.write_text("print('hi')")
    # No sidecar present

    result = run_script(
        argv=[sys.executable, str(script)],
        project_slug="proj",
        catalog_dir=tmp_path / "catalog",
        output_paths=[],
        tags=[],
        cwd=tmp_path,
    )
    assert result == 1  # blocked


def test_run_allows_enforced_dir_with_sidecar(tmp_path):
    """bth run proceeds if script in enforced dir has a valid sidecar."""
    import sys, textwrap
    from bathos.runner import run_script

    (tmp_path / "catalog").mkdir()
    enforced = tmp_path / "scripts" / "experiments"
    enforced.mkdir(parents=True)
    script = enforced / "run_nvt.py"
    script.write_text("print('hi')")
    sidecar = enforced / "run_nvt.bth.toml"
    sidecar.write_text(textwrap.dedent("""
        [experiment]
        hypothesis = "test"
        [result_schema]
        x = "float"
    """))

    result = run_script(
        argv=[sys.executable, str(script)],
        project_slug="proj",
        catalog_dir=tmp_path / "catalog",
        output_paths=[],
        tags=[],
        cwd=tmp_path,
    )
    assert result == 0


def test_run_skips_enforcement_for_scratch(tmp_path):
    """bth run does not enforce sidecars for scripts/scratch/."""
    import sys
    from bathos.runner import run_script

    (tmp_path / "catalog").mkdir()
    scratch = tmp_path / "scripts" / "scratch"
    scratch.mkdir(parents=True)
    script = scratch / "explore.py"
    script.write_text("print('hi')")
    # No sidecar — should still run

    result = run_script(
        argv=[sys.executable, str(script)],
        project_slug="proj",
        catalog_dir=tmp_path / "catalog",
        output_paths=[],
        tags=[],
        cwd=tmp_path,
    )
    assert result == 0


def test_result_emission_via_env_var(tmp_catalog: Path, tmp_path: Path):
    """Sets BTH_RESULTS_PATH, script writes JSON, outcome is evaluated."""
    import json
    import textwrap

    # Create enforced directory with script and sidecar
    enforced = tmp_path / "scripts" / "experiments"
    enforced.mkdir(parents=True)
    script = enforced / "run_test.py"
    script.write_text(textwrap.dedent("""
        import os
        import json

        results_path = os.environ.get("BTH_RESULTS_PATH")
        if results_path:
            with open(results_path, "w") as f:
                json.dump({"temp_mean": 300.5, "temp_std": 2.3, "n_steps": 1000}, f)
    """))

    sidecar = enforced / "run_test.bth.toml"
    sidecar.write_text(textwrap.dedent("""
        [experiment]
        hypothesis = "test hypothesis"
        [outcomes.pass]
        condition = "temp_std < 5"
        decision = "good"
        [outcomes.marginal]
        condition = "temp_std >= 5 AND temp_std < 10"
        decision = "borderline"
        [result_schema]
        temp_mean = "float"
        temp_std = "float"
        n_steps = "int"
    """))

    exit_code = run_script(
        argv=[sys.executable, str(script)],
        project_slug="testproj",
        catalog_dir=tmp_catalog,
        output_paths=[],
        tags=[],
        cwd=tmp_path,
    )
    assert exit_code == 0
    runs = read_runs(tmp_catalog)
    assert len(runs) == 1
    # Outcome should be evaluated based on metadata (temp_std < 5 = "pass")
    assert runs[0].outcome == "pass"


def test_result_emission_fallback_path(tmp_catalog: Path, tmp_path: Path):
    """No env var, fallback .bth-results.json adjacent to script is used."""
    import json
    import textwrap

    # Create enforced directory with script and sidecar
    enforced = tmp_path / "scripts" / "experiments"
    enforced.mkdir(parents=True)
    script = enforced / "run_test.py"
    # Script must write to absolute path for fallback to work
    script.write_text(textwrap.dedent("""
        import json
        import os

        # Write results to fallback path (script_stem.bth-results.json) adjacent to script
        script_dir = os.path.dirname(os.path.abspath(__file__))
        fallback_path = os.path.join(script_dir, "run_test.bth-results.json")
        with open(fallback_path, "w") as f:
            json.dump({"metric_a": 42, "metric_b": 3.14}, f)
    """))

    sidecar = enforced / "run_test.bth.toml"
    sidecar.write_text(textwrap.dedent("""
        [experiment]
        hypothesis = "test hypothesis"
        [outcomes.pass]
        condition = "metric_a == 42"
        decision = "correct"
        [result_schema]
        metric_a = "int"
        metric_b = "float"
    """))

    exit_code = run_script(
        argv=[sys.executable, str(script)],
        project_slug="testproj",
        catalog_dir=tmp_catalog,
        output_paths=[],
        tags=[],
        cwd=tmp_path,
    )
    assert exit_code == 0
    runs = read_runs(tmp_catalog)
    assert len(runs) == 1
    # Outcome should be evaluated based on metadata from fallback file
    assert runs[0].outcome == "pass"


def test_result_emission_missing_file(tmp_catalog: Path):
    """No env var, no fallback file, metadata stays '{}', outcome stays ''."""
    init_catalog(tmp_catalog)
    exit_code = run_script(
        argv=[sys.executable, "-c", "pass"],
        project_slug="testproj",
        catalog_dir=tmp_catalog,
        output_paths=[],
        tags=[],
    )
    assert exit_code == 0
    runs = read_runs(tmp_catalog)
    assert len(runs) == 1
    # No sidecar, no metadata file, outcome should be empty
    assert runs[0].outcome == ""


def test_result_emission_invalid_json(tmp_catalog: Path, tmp_path: Path):
    """File exists but invalid JSON, metadata stays '{}', outcome stays ''."""
    import textwrap

    init_catalog(tmp_catalog)
    # Create script that writes invalid JSON
    script = tmp_path / "bad_json.py"
    script.write_text(textwrap.dedent("""
        import os

        results_path = os.environ.get("BTH_RESULTS_PATH")
        if results_path:
            with open(results_path, "w") as f:
                f.write("{invalid json content")
    """))

    exit_code = run_script(
        argv=[sys.executable, str(script)],
        project_slug="testproj",
        catalog_dir=tmp_catalog,
        output_paths=[],
        tags=[],
        cwd=tmp_path,
    )
    assert exit_code == 0
    runs = read_runs(tmp_catalog)
    assert len(runs) == 1
    # Invalid JSON should result in empty outcome
    assert runs[0].outcome == ""
