from datetime import datetime, timezone
from bathos.schema import Run, COOL_SCHEMA, WARM_SCHEMA
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
    assert r.schema_version == "1"
    assert r.slurm_job_id == ""
    assert r.metadata == "{}"


def test_run_to_arrow_table():
    r = Run(project_slug="proj", command="python foo.py", argv=["python", "foo.py"],
            git_hash="abc123", git_branch="main", git_dirty=False)
    table = r.to_arrow()
    assert table.schema.equals(COOL_SCHEMA)
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
    assert r2.schema_version == "1"
    assert r2.slurm_job_id == ""


def test_schema_version_in_cool_parquet():
    """Verify schema_version field is written to cool Parquet."""
    r = Run(project_slug="proj", command="python foo.py", argv=["python", "foo.py"],
            git_hash="abc123", git_branch="main", git_dirty=False)
    table = r.to_arrow()
    assert "schema_version" in table.column_names
    assert table.column("schema_version")[0].as_py() == "1"


def test_slurm_job_id_captured_from_env(monkeypatch):
    """Verify slurm_job_id can be set and round-trips through Parquet."""
    r = Run(project_slug="proj", command="python foo.py", argv=["python", "foo.py"],
            git_hash="abc123", git_branch="main", git_dirty=False,
            slurm_job_id="12345678")
    table = r.to_arrow()
    assert "slurm_job_id" in table.column_names
    assert table.column("slurm_job_id")[0].as_py() == "12345678"

    # Round-trip
    r2 = Run.from_arrow_row(table.to_pydict(), 0)
    assert r2.slurm_job_id == "12345678"


def test_metadata_not_in_cool_parquet():
    """Verify metadata field is NOT written to cool Parquet."""
    r = Run(project_slug="proj", command="python foo.py", argv=["python", "foo.py"],
            git_hash="abc123", git_branch="main", git_dirty=False,
            metadata='{"key": "value"}')
    table = r.to_arrow()
    assert "metadata" not in table.column_names


def test_warm_schema_has_metadata_column():
    """Verify WARM_SCHEMA includes metadata column."""
    assert "metadata" in WARM_SCHEMA.names
    # Find metadata field and check it's string type
    metadata_field = next(f for f in WARM_SCHEMA if f.name == "metadata")
    assert metadata_field.type == pa.string()


def test_warm_schema_has_outcome_column():
    """Verify WARM_SCHEMA includes outcome column."""
    assert "outcome" in WARM_SCHEMA.names
    # Find outcome field and check it's string type
    outcome_field = next(f for f in WARM_SCHEMA if f.name == "outcome")
    assert outcome_field.type == pa.string()


def test_cool_schema_has_new_fields():
    """Verify COOL_SCHEMA includes schema_version and slurm_job_id."""
    assert "schema_version" in COOL_SCHEMA.names
    assert "slurm_job_id" in COOL_SCHEMA.names


def test_run_with_custom_metadata():
    """Verify metadata is stored in Run but not serialized to cool Parquet."""
    r = Run(project_slug="proj", command="python foo.py", argv=["python", "foo.py"],
            git_hash="abc123", git_branch="main", git_dirty=False,
            metadata='{"hypothesis": "test", "outcome": "pass"}')
    assert r.metadata == '{"hypothesis": "test", "outcome": "pass"}'
    table = r.to_arrow()
    assert "metadata" not in table.column_names
