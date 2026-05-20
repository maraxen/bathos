from datetime import datetime

import pyarrow as pa

from bathos.schema import COOL_SCHEMA, WARM_SCHEMA, Run


def test_run_has_generated_id():
    r = Run(
        project_slug="proj",
        command="python foo.py",
        argv=["python", "foo.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
    )
    assert len(r.id) == 36  # UUID4


def test_two_runs_have_different_ids():
    r1 = Run(
        project_slug="p", command="x", argv=["x"], git_hash="a", git_branch="main", git_dirty=False
    )
    r2 = Run(
        project_slug="p", command="x", argv=["x"], git_hash="a", git_branch="main", git_dirty=False
    )
    assert r1.id != r2.id


def test_run_defaults():
    r = Run(
        project_slug="proj",
        command="python foo.py",
        argv=["python", "foo.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
    )
    assert r.status == "running"
    assert r.exit_code == -1
    assert r.duration_s == 0.0
    assert r.output_paths == []
    assert r.tags == []
    assert isinstance(r.timestamp, datetime)
    assert r.timestamp.tzinfo is not None
    assert r.schema_version == "3"
    assert r.slurm_job_id == ""
    assert r.metadata == "{}"


def test_run_to_arrow_table():
    r = Run(
        project_slug="proj",
        command="python foo.py",
        argv=["python", "foo.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
    )
    table = r.to_arrow()
    assert table.schema.equals(COOL_SCHEMA)
    assert table.num_rows == 1


def test_run_roundtrip_via_arrow():
    r = Run(
        project_slug="proj",
        command="python foo.py",
        argv=["python", "foo.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        duration_s=1.5,
        output_paths=["/tmp/out.parquet"],
        tags=["tip3p"],
    )
    table = r.to_arrow()
    r2 = Run.from_arrow_row(table.to_pydict(), 0)
    assert r2.id == r.id
    assert r2.status == "completed"
    assert r2.exit_code == 0
    assert r2.duration_s == 1.5
    assert r2.output_paths == ["/tmp/out.parquet"]
    assert r2.tags == ["tip3p"]
    assert r2.schema_version == "3"
    assert r2.slurm_job_id == ""


def test_schema_version_in_cool_parquet():
    """Verify schema_version field is written to cool Parquet."""
    r = Run(
        project_slug="proj",
        command="python foo.py",
        argv=["python", "foo.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
    )
    table = r.to_arrow()
    assert "schema_version" in table.column_names
    assert table.column("schema_version")[0].as_py() == "3"


def test_slurm_job_id_captured_from_env():
    """Verify slurm_job_id can be set and round-trips through Parquet."""
    r = Run(
        project_slug="proj",
        command="python foo.py",
        argv=["python", "foo.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        slurm_job_id="12345678",
    )
    table = r.to_arrow()
    assert "slurm_job_id" in table.column_names
    assert table.column("slurm_job_id")[0].as_py() == "12345678"

    # Round-trip
    r2 = Run.from_arrow_row(table.to_pydict(), 0)
    assert r2.slurm_job_id == "12345678"


def test_metadata_not_in_cool_parquet():
    """Verify metadata field is NOT written to cool Parquet."""
    r = Run(
        project_slug="proj",
        command="python foo.py",
        argv=["python", "foo.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        metadata='{"key": "value"}',
    )
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
    r = Run(
        project_slug="proj",
        command="python foo.py",
        argv=["python", "foo.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        metadata='{"hypothesis": "test", "outcome": "pass"}',
    )
    assert r.metadata == '{"hypothesis": "test", "outcome": "pass"}'
    table = r.to_arrow()
    assert "metadata" not in table.column_names


def test_cool_schema_has_hostname_field():
    """Verify COOL_SCHEMA contains hostname field."""
    assert "hostname" in COOL_SCHEMA.names
    hostname_field = next(f for f in COOL_SCHEMA if f.name == "hostname")
    assert hostname_field.type == pa.string()


def test_warm_schema_has_hostname_field():
    """Verify WARM_SCHEMA contains hostname field."""
    assert "hostname" in WARM_SCHEMA.names
    hostname_field = next(f for f in WARM_SCHEMA if f.name == "hostname")
    assert hostname_field.type == pa.string()


def test_run_hostname_default_empty_string():
    """Verify Run hostname defaults to empty string."""
    r = Run(
        project_slug="proj",
        command="python foo.py",
        argv=["python", "foo.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
    )
    assert r.hostname == ""


def test_hostname_roundtrip_via_arrow():
    """Verify hostname is serialized to Parquet and round-trips correctly."""
    r = Run(
        project_slug="proj",
        command="python foo.py",
        argv=["python", "foo.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        hostname="compute-node-42",
    )
    table = r.to_arrow()
    assert "hostname" in table.column_names
    assert table.column("hostname")[0].as_py() == "compute-node-42"

    # Round-trip
    r2 = Run.from_arrow_row(table.to_pydict(), 0)
    assert r2.hostname == "compute-node-42"


def test_schema_version_defaults_to_3():
    """Verify new runs default to schema_version='3'."""
    r = Run(
        project_slug="test",
        command="python foo.py",
        argv=["python", "foo.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
    )
    assert r.schema_version == "3"


def test_sample_run_fixture_has_hostname(sample_run):
    """Verify sample_run fixture includes hostname field."""
    assert hasattr(sample_run, "hostname")
    assert sample_run.hostname == "test-host"


def test_run_has_outcome_field():
    r = Run(project_slug="p", command="c", argv=["c"], git_hash="abc",
            git_branch="main", git_dirty=False)
    assert r.outcome == ""


def test_run_outcome_can_be_set():
    r = Run(project_slug="p", command="c", argv=["c"], git_hash="abc",
            git_branch="main", git_dirty=False, outcome="pass")
    assert r.outcome == "pass"


def test_run_to_arrow_includes_outcome():
    r = Run(project_slug="p", command="c", argv=["c"], git_hash="abc",
            git_branch="main", git_dirty=False, outcome="pass")
    tbl = r.to_arrow()
    assert "outcome" in tbl.schema.names
    assert tbl.column("outcome")[0].as_py() == "pass"


def test_run_v3_fields_have_defaults():
    """Verify all 8 v3 fields have correct defaults when not specified."""
    r = Run(
        project_slug="p",
        command="c",
        argv=["c"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
    )
    assert r.sidecar_sha256 == ""
    assert r.sidecar_path == ""
    assert r.parent_run_id == ""
    assert r.agent_mode == ""
    assert r.sidecar_mode == ""
    assert r.outcome_is_residual is False
    assert r.skill_sha256 == ""
    assert r.campaign_id == ""


def test_run_v3_fields_round_trip_arrow():
    """Verify all 8 v3 fields round-trip through Arrow serialization."""
    r = Run(
        project_slug="p",
        command="c",
        argv=["c"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        sidecar_sha256="sha256_test_value",
        sidecar_path="/path/to/sidecar.toml",
        parent_run_id="parent_uuid_123",
        agent_mode="auto",
        sidecar_mode="declared",
        outcome_is_residual=True,
        skill_sha256="skill_sha_789",
        campaign_id="campaign_xyz",
    )
    table = r.to_arrow()
    r2 = Run.from_arrow_row(table.to_pydict(), 0)

    assert r2.sidecar_sha256 == "sha256_test_value"
    assert r2.sidecar_path == "/path/to/sidecar.toml"
    assert r2.parent_run_id == "parent_uuid_123"
    assert r2.agent_mode == "auto"
    assert r2.sidecar_mode == "declared"
    assert r2.outcome_is_residual is True
    assert r2.skill_sha256 == "skill_sha_789"
    assert r2.campaign_id == "campaign_xyz"


def test_cool_schema_has_v3_fields():
    """Verify COOL_SCHEMA contains all 8 v3 fields."""
    v3_fields = [
        "sidecar_sha256",
        "sidecar_path",
        "parent_run_id",
        "agent_mode",
        "sidecar_mode",
        "outcome_is_residual",
        "skill_sha256",
        "campaign_id",
    ]
    for field_name in v3_fields:
        assert field_name in COOL_SCHEMA.names


def test_warm_schema_has_v3_fields():
    """Verify WARM_SCHEMA contains all 8 v3 fields."""
    v3_fields = [
        "sidecar_sha256",
        "sidecar_path",
        "parent_run_id",
        "agent_mode",
        "sidecar_mode",
        "outcome_is_residual",
        "skill_sha256",
        "campaign_id",
    ]
    for field_name in v3_fields:
        assert field_name in WARM_SCHEMA.names
