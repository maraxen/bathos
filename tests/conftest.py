import os
from pathlib import Path

import pytest

from bathos.schema import Run

# Guard repair module import for test collection — repair.py is optional at collection time
pytest.importorskip("bathos.repair")


@pytest.fixture
def tmp_catalog(tmp_path: Path) -> Path:
    catalog = tmp_path / ".bth" / "catalog"
    catalog.mkdir(parents=True)
    return catalog


@pytest.fixture
def sample_run() -> Run:
    return Run(
        project_slug="testproj",
        command="python scripts/experiments/run.py --n 10",
        argv=["python", "scripts/experiments/run.py", "--n", "10"],
        git_hash="deadbeef",
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        duration_s=2.5,
        output_paths=["/tmp/results.parquet"],
        tags=["smoke"],
        hostname="test-host",
    )


@pytest.fixture(autouse=True)
def clear_myxcel_env(monkeypatch):
    """Autouse fixture to clear all MYXCEL_* env vars for each test.

    This prevents ambient MYXCEL_* variables from a myxcel-submitted job
    from affecting test behavior. Tests that need specific MYXCEL_* values
    can set them explicitly via monkeypatch after this fixture clears them.
    """
    myxcel_vars = [k for k in os.environ if k.startswith("MYXCEL_")]
    for var in myxcel_vars:
        monkeypatch.delenv(var, raising=False)
