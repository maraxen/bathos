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
