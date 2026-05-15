from datetime import datetime, timezone
from bathos.schema import Run, RUN_SCHEMA
import pyarrow as pa


def test_run_has_generated_id():
    r = Run(project_slug="proj", command="python foo.py", argv=["python", "foo.py"],
            git_hash="abc123", git_branch="main", git_dirty=False)
    assert len(r.id) == 36  # UUID4


def test_two_runs_have_different_ids():
    r1 = Run(project_slug="p", command="x", argv=["x"],
             git_hash="a", git_branch="main", git_dirty=False)
    r2 = Run(project_slug="p", command="x", argv=["x"],
             git_hash="a", git_branch="main", git_dirty=False)
    assert r1.id != r2.id


def test_run_defaults():
    r = Run(project_slug="proj", command="python foo.py", argv=["python", "foo.py"],
            git_hash="abc123", git_branch="main", git_dirty=False)
    assert r.status == "running"
    assert r.exit_code == -1
    assert r.duration_s == 0.0
    assert r.output_paths == []
    assert r.tags == []
    assert isinstance(r.timestamp, datetime)
    assert r.timestamp.tzinfo is not None


def test_run_to_arrow_table():
    r = Run(project_slug="proj", command="python foo.py", argv=["python", "foo.py"],
            git_hash="abc123", git_branch="main", git_dirty=False)
    table = r.to_arrow()
    assert table.schema.equals(RUN_SCHEMA)
    assert table.num_rows == 1


def test_run_roundtrip_via_arrow():
    r = Run(project_slug="proj", command="python foo.py", argv=["python", "foo.py"],
            git_hash="abc123", git_branch="main", git_dirty=False,
            status="completed", exit_code=0, duration_s=1.5,
            output_paths=["/tmp/out.parquet"], tags=["tip3p"])
    table = r.to_arrow()
    r2 = Run.from_arrow_row(table.to_pydict(), 0)
    assert r2.id == r.id
    assert r2.status == "completed"
    assert r2.exit_code == 0
    assert r2.duration_s == 1.5
    assert r2.output_paths == ["/tmp/out.parquet"]
    assert r2.tags == ["tip3p"]
