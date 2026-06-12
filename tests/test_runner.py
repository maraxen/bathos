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
        hypothesis = "test hypothesis"
        [outcomes.pass]
        condition = "x == 1"
        decision = "good"
        reasoning = "expected behavior"
        [outcomes.fallback]
        condition = "1==1"
        decision = "other"
        reasoning = "catch-all"
        is_residual = true
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
        reasoning = "stable temperature"
        [outcomes.marginal]
        condition = "temp_std >= 5 AND temp_std < 10"
        decision = "borderline"
        reasoning = "marginal stability"
        [outcomes.fail]
        condition = "temp_std >= 10"
        decision = "unstable"
        reasoning = "poor stability"
        is_residual = true
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
        reasoning = "expected value"
        [outcomes.fallback]
        condition = "1==1"
        decision = "other"
        reasoning = "catch-all"
        is_residual = true
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


def test_gate_fires_in_enforced_dir_missing_sidecar(tmp_path):
    """Gate layer blocks enforced dir script without sidecar via gate_check()."""
    import sys
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
    assert result == 1  # blocked by gate


def test_gate_passes_with_valid_sidecar(tmp_path):
    """Gate layer passes with valid sidecar in enforced dir."""
    import sys
    import textwrap
    from bathos.runner import run_script

    (tmp_path / "catalog").mkdir()
    enforced = tmp_path / "scripts" / "experiments"
    enforced.mkdir(parents=True)
    script = enforced / "run_nvt.py"
    script.write_text("print('hi')")
    sidecar = enforced / "run_nvt.bth.toml"
    sidecar.write_text(textwrap.dedent("""
        [experiment]
        hypothesis = "test hypothesis"
        [outcomes.pass]
        condition = "x == 1"
        decision = "proceed"
        reasoning = "expected behavior"
        [outcomes.fallback]
        condition = "1==1"
        decision = "other"
        reasoning = "catch-all"
        is_residual = true
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
    assert result == 0  # gate passes


def test_no_sidecar_flag_bypasses_gate(tmp_path):
    """no_sidecar=True bypasses gate even in enforced dir (sidecar_mode='bypassed')."""
    import sys
    from bathos.catalog import read_runs
    from bathos.runner import run_script

    (tmp_path / "catalog").mkdir()
    enforced = tmp_path / "scripts" / "experiments"
    enforced.mkdir(parents=True)
    script = enforced / "run_nvt.py"
    script.write_text("print('hi')")
    # No sidecar present, but bypassed via flag

    result = run_script(
        argv=[sys.executable, str(script)],
        project_slug="proj",
        catalog_dir=tmp_path / "catalog",
        output_paths=[],
        tags=[],
        cwd=tmp_path,
        no_sidecar=True,
    )
    assert result == 0  # bypassed
    runs = read_runs(tmp_path / "catalog")
    assert len(runs) == 1
    assert runs[0].sidecar_mode == "bypassed"


def test_outcome_is_residual_populated(tmp_catalog: Path, tmp_path: Path):
    """outcome_is_residual is set to True when outcome spec has is_residual=True."""
    import json
    import textwrap
    from bathos.runner import run_script

    # Create enforced directory with script and sidecar where fallback outcome has is_residual=True
    enforced = tmp_path / "scripts" / "experiments"
    enforced.mkdir(parents=True)
    script = enforced / "run_test.py"
    script.write_text(textwrap.dedent("""
        import os
        import json

        # This outcome doesn't match the pass condition
        results_path = os.environ.get("BTH_RESULTS_PATH")
        if results_path:
            with open(results_path, "w") as f:
                json.dump({"x": 10}, f)
    """))

    sidecar = enforced / "run_test.bth.toml"
    sidecar.write_text(textwrap.dedent("""
        [experiment]
        hypothesis = "test hypothesis"
        [outcomes.pass]
        condition = "x == 5"
        decision = "good"
        reasoning = "expected value"
        [outcomes.fallback]
        condition = "1==1"
        decision = "fallback"
        reasoning = "catch-all"
        is_residual = true
        [result_schema]
        x = "int"
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
    # outcome should be "fallback" because x==10, not x==5
    assert runs[0].outcome == "fallback"
    # outcome_is_residual should be True because fallback has is_residual=true
    assert runs[0].outcome_is_residual == True


def test_outcome_error_on_nonzero_exit(tmp_catalog: Path, tmp_path: Path):
    """When exit_code != 0, outcome should be set to 'error'."""
    import textwrap

    init_catalog(tmp_catalog)
    # Create a script that fails with exit code 1
    exit_code = run_script(
        argv=[sys.executable, "-c", "raise SystemExit(1)"],
        project_slug="testproj",
        catalog_dir=tmp_catalog,
        output_paths=[],
        tags=[],
        cwd=tmp_path,
    )
    assert exit_code == 1
    runs = read_runs(tmp_catalog)
    assert len(runs) == 1
    assert runs[0].outcome == "error"
    assert runs[0].outcome_error_reason == "exit_code=1"


def test_evaluate_outcome_not_called_on_error(tmp_catalog: Path, tmp_path: Path):
    """When exit_code != 0, evaluate_outcome is not called even with a sidecar."""
    import textwrap

    enforced = tmp_path / "scripts" / "experiments"
    enforced.mkdir(parents=True)
    script = enforced / "run_test.py"
    script.write_text(textwrap.dedent("""
        raise SystemExit(1)
    """))

    sidecar = enforced / "run_test.bth.toml"
    sidecar.write_text(textwrap.dedent("""
        [experiment]
        hypothesis = "test hypothesis"
        [outcomes.pass]
        condition = "temp_std < 5"
        decision = "good"
        reasoning = "stable temperature"
        is_residual = true
        [result_schema]
        temp_std = "float"
    """))

    init_catalog(tmp_catalog)
    exit_code = run_script(
        argv=[sys.executable, str(script)],
        project_slug="testproj",
        catalog_dir=tmp_catalog,
        output_paths=[],
        tags=[],
        cwd=tmp_path,
    )
    assert exit_code == 1
    runs = read_runs(tmp_catalog)
    assert len(runs) == 1
    # outcome should be "error", not evaluated against sidecar outcomes
    assert runs[0].outcome == "error"
    assert runs[0].outcome_error_reason == "exit_code=1"


def test_manifest_sha256_populated(tmp_catalog: Path, tmp_path: Path):
    """After a normal run, run.manifest_sha256 and run.manifest_path are populated."""
    import textwrap

    enforced = tmp_path / "scripts" / "experiments"
    enforced.mkdir(parents=True)
    script = enforced / "run_test.py"
    script.write_text("pass")

    sidecar = enforced / "run_test.bth.toml"
    sidecar.write_text(textwrap.dedent("""
        [experiment]
        hypothesis = "test"
        [outcomes.pass]
        condition = "x == 1"
        decision = "good"
        reasoning = "x is 1"
        is_residual = true
        [result_schema]
        x = "int"
    """))

    init_catalog(tmp_catalog)
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
    # After normal run, manifest_sha256 should be non-empty
    assert runs[0].manifest_sha256 != ""
    assert runs[0].manifest_path != ""


def test_bth_output_dir_injected(tmp_catalog: Path, tmp_path: Path):
    """BTH_OUTPUT_DIR env var is set and points to a writable per-run dir."""
    import sys, os
    from bathos.catalog import init_catalog, read_runs
    from bathos.runner import run_script

    init_catalog(tmp_catalog)
    script = tmp_path / "check_env.py"
    script.write_text(
        "import os, json, pathlib\n"
        "d = os.environ['BTH_OUTPUT_DIR']\n"
        "assert pathlib.Path(d).is_dir(), f'BTH_OUTPUT_DIR not a dir: {d!r}'\n"
        "print(json.dumps({'out_dir': d}))\n"
    )
    exit_code = run_script(
        argv=[sys.executable, str(script)],
        project_slug="p",
        catalog_dir=tmp_catalog,
        output_paths=[],
        tags=[],
        cwd=tmp_path,
    )
    assert exit_code == 0
    runs = read_runs(tmp_catalog)
    assert len(runs) == 1
    assert runs[0].id[:8] in runs[0].output_paths[0] if runs[0].output_paths else True


def test_bth_output_dir_files_auto_registered(tmp_catalog: Path, tmp_path: Path):
    """Files written to BTH_OUTPUT_DIR are auto-registered in output_paths."""
    import sys
    from bathos.catalog import init_catalog, read_runs
    from bathos.runner import run_script

    init_catalog(tmp_catalog)
    script = tmp_path / "write_output.py"
    script.write_text(
        "import os, pathlib, json\n"
        "out_dir = pathlib.Path(os.environ['BTH_OUTPUT_DIR'])\n"
        "(out_dir / 'result.json').write_text(json.dumps({'x': 42}))\n"
        "(out_dir / 'model.pt').write_bytes(b'fake')\n"
        "print(json.dumps({}))\n"
    )
    exit_code = run_script(
        argv=[sys.executable, str(script)],
        project_slug="p",
        catalog_dir=tmp_catalog,
        output_paths=[],
        tags=[],
        cwd=tmp_path,
    )
    assert exit_code == 0
    runs = read_runs(tmp_catalog)
    assert len(runs) == 1
    registered = runs[0].output_paths
    assert len(registered) == 2
    names = {p.split("/")[-1] for p in registered}
    assert names == {"result.json", "model.pt"}


def test_explicit_out_and_output_dir_merged(tmp_catalog: Path, tmp_path: Path):
    """Explicit --out paths and BTH_OUTPUT_DIR files are merged, no duplicates."""
    import sys
    from bathos.catalog import init_catalog, read_runs
    from bathos.runner import run_script

    explicit_file = tmp_path / "explicit.json"
    explicit_file.write_text("{}")

    init_catalog(tmp_catalog)
    script = tmp_path / "write_both.py"
    script.write_text(
        "import os, pathlib, json\n"
        "out_dir = pathlib.Path(os.environ['BTH_OUTPUT_DIR'])\n"
        "(out_dir / 'auto.txt').write_text('auto')\n"
        "print(json.dumps({}))\n"
    )
    exit_code = run_script(
        argv=[sys.executable, str(script)],
        project_slug="p",
        catalog_dir=tmp_catalog,
        output_paths=[str(explicit_file)],
        tags=[],
        cwd=tmp_path,
    )
    assert exit_code == 0
    runs = read_runs(tmp_catalog)
    registered = runs[0].output_paths
    names = {p.split("/")[-1] for p in registered}
    assert "explicit.json" in names
    assert "auto.txt" in names
    assert len(registered) == len(set(registered))  # no duplicates
