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
    assert r.schema_version == "12"
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
    assert r2.schema_version == "12"
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
    assert table.column("schema_version")[0].as_py() == "12"


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


def test_schema_version_defaults_to_7():
    """Verify new runs default to schema_version='7'."""
    r = Run(
        project_slug="test",
        command="python foo.py",
        argv=["python", "foo.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
    )
    assert r.schema_version == "12"


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


def test_schema_v5_fields_exist():
    """Verify schema v5 fields exist: manifest_sha256, manifest_path, outcome_error_reason, adversarial_check_status."""
    from bathos.schema import CURRENT_SCHEMA_VERSION, Run

    # Current version should be "7" (v5 fields still present)
    assert CURRENT_SCHEMA_VERSION == "12"

    # Run should have all 4 new fields
    r = Run(
        project_slug="p",
        command="c",
        argv=["c"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
    )
    assert hasattr(r, "manifest_sha256")
    assert hasattr(r, "manifest_path")
    assert hasattr(r, "outcome_error_reason")
    assert hasattr(r, "adversarial_check_status")

    # Check defaults
    assert r.manifest_sha256 == ""
    assert r.manifest_path == ""
    assert r.outcome_error_reason == ""
    assert r.adversarial_check_status == ""


def test_schema_v5_fields_in_cool_schema():
    """Verify COOL_SCHEMA includes all 4 v5 fields."""
    v5_fields = [
        "manifest_sha256",
        "manifest_path",
        "outcome_error_reason",
        "adversarial_check_status",
    ]
    for field_name in v5_fields:
        assert field_name in COOL_SCHEMA.names, f"Missing {field_name} in COOL_SCHEMA"


def test_schema_v5_fields_in_warm_schema():
    """Verify WARM_SCHEMA includes all 4 v5 fields."""
    v5_fields = [
        "manifest_sha256",
        "manifest_path",
        "outcome_error_reason",
        "adversarial_check_status",
    ]
    for field_name in v5_fields:
        assert field_name in WARM_SCHEMA.names, f"Missing {field_name} in WARM_SCHEMA"


def test_v5_fields_round_trip_arrow():
    """Verify v5 fields round-trip through Arrow serialization."""
    r = Run(
        project_slug="p",
        command="c",
        argv=["c"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        manifest_sha256="abc123def456",
        manifest_path="/path/to/manifest.toml",
        outcome_error_reason="exit_code=1",
        adversarial_check_status="present",
    )
    table = r.to_arrow()
    r2 = Run.from_arrow_row(table.to_pydict(), 0)

    assert r2.manifest_sha256 == "abc123def456"
    assert r2.manifest_path == "/path/to/manifest.toml"
    assert r2.outcome_error_reason == "exit_code=1"
    assert r2.adversarial_check_status == "present"


def test_schema_version_is_7():
    """Verify CURRENT_SCHEMA_VERSION is now '11'."""
    from bathos.schema import CURRENT_SCHEMA_VERSION

    assert CURRENT_SCHEMA_VERSION == "12"


def test_run_stage_name_default_none():
    """Verify Run stage_name defaults to None."""
    r = Run(
        project_slug="p",
        command="c",
        argv=["c"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
    )
    assert r.stage_name is None


def test_run_stage_name_optional():
    """Verify stage_name can be set and defaults to None."""
    r = Run(
        project_slug="p",
        command="c",
        argv=["c"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        stage_name="calibration",
    )
    assert r.stage_name == "calibration"


def test_stage_name_in_cool_schema():
    """Verify stage_name is in COOL_SCHEMA."""
    assert "stage_name" in COOL_SCHEMA.names
    stage_name_field = next(f for f in COOL_SCHEMA if f.name == "stage_name")
    assert stage_name_field.type == pa.string()


def test_stage_name_in_warm_schema():
    """Verify stage_name is in WARM_SCHEMA."""
    assert "stage_name" in WARM_SCHEMA.names
    stage_name_field = next(f for f in WARM_SCHEMA if f.name == "stage_name")
    assert stage_name_field.type == pa.string()


def test_stage_name_round_trip_arrow():
    """Verify stage_name round-trips through Arrow serialization."""
    r = Run(
        project_slug="p",
        command="c",
        argv=["c"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        stage_name="exploration",
    )
    table = r.to_arrow()
    assert "stage_name" in table.schema.names
    assert table.column("stage_name")[0].as_py() == "exploration"

    r2 = Run.from_arrow_row(table.to_pydict(), 0)
    assert r2.stage_name == "exploration"


def test_stage_name_none_round_trip_arrow():
    """Verify stage_name=None round-trips through Arrow serialization."""
    r = Run(
        project_slug="p",
        command="c",
        argv=["c"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        stage_name=None,
    )
    table = r.to_arrow()
    assert "stage_name" in table.schema.names
    assert table.column("stage_name")[0].as_py() is None

    r2 = Run.from_arrow_row(table.to_pydict(), 0)
    assert r2.stage_name is None


def test_stage_name_regex_valid_kebab():
    """Verify stage_name regex accepts valid kebab-case names."""
    from bathos.schema import STAGE_NAME_REGEX

    assert STAGE_NAME_REGEX.match("exploration") is not None
    assert STAGE_NAME_REGEX.match("final-validation") is not None
    assert STAGE_NAME_REGEX.match("phase1-calibration") is not None
    assert STAGE_NAME_REGEX.match("abc") is not None


def test_stage_name_regex_rejects_invalid():
    """Verify stage_name regex rejects invalid names."""
    from bathos.schema import STAGE_NAME_REGEX

    assert STAGE_NAME_REGEX.match("") is None  # empty
    assert STAGE_NAME_REGEX.match("1exploration") is None  # leading digit
    assert STAGE_NAME_REGEX.match("Exploration") is None  # uppercase
    assert STAGE_NAME_REGEX.match("final-") is None  # trailing dash
    assert STAGE_NAME_REGEX.match("final--validation") is None  # consecutive dashes
    assert STAGE_NAME_REGEX.match("final validation") is None  # space


def test_stage_name_validator_valid():
    """Verify stage_name validator accepts valid names."""
    from bathos.schema import _validate_stage_name

    assert _validate_stage_name("exploration") is True
    assert _validate_stage_name("final-validation") is True
    assert _validate_stage_name("phase1-calibration") is True
    assert _validate_stage_name("abc") is True  # exactly 3 chars
    assert _validate_stage_name(None) is True  # None is valid


def test_stage_name_validator_rejects_invalid():
    """Verify stage_name validator rejects invalid names."""
    from bathos.schema import _validate_stage_name

    assert _validate_stage_name("") is False  # empty
    assert _validate_stage_name("ab") is False  # too short (2 chars)
    assert _validate_stage_name("1exploration") is False  # leading digit
    assert _validate_stage_name("Exploration") is False  # uppercase
    assert _validate_stage_name("final-") is False  # trailing dash
    assert _validate_stage_name("final--validation") is False  # consecutive dashes
    assert _validate_stage_name("final validation") is False  # space
    assert _validate_stage_name("a" * 41) is False  # too long (41 chars)


def test_stage_name_validator_allows_40_chars():
    """Verify stage_name validator allows exactly 40 characters."""
    from bathos.schema import _validate_stage_name

    name_40 = "a" * 40  # exactly 40 lowercase letters
    assert _validate_stage_name(name_40) is True


def test_run_seed_fields_default_none():
    """Verify B2-02 fields (seed, baseline_hpo_trials, baseline_hpo_compute_budget) default to None."""
    r = Run(
        project_slug="p",
        command="c",
        argv=["c"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
    )
    assert r.seed is None
    assert r.baseline_hpo_trials is None
    assert r.baseline_hpo_compute_budget is None


def test_seed_fields_in_cool_schema():
    """Verify B2-02 fields are in COOL_SCHEMA as nullable numeric types."""
    seed_field = next(f for f in COOL_SCHEMA if f.name == "seed")
    trials_field = next(f for f in COOL_SCHEMA if f.name == "baseline_hpo_trials")
    compute_field = next(f for f in COOL_SCHEMA if f.name == "baseline_hpo_compute_budget")
    assert seed_field.type == pa.int64()
    assert seed_field.nullable
    assert trials_field.type == pa.int64()
    assert compute_field.type == pa.float64()


def test_seed_fields_in_warm_schema():
    """Verify B2-02 fields are in WARM_SCHEMA as nullable numeric types."""
    for field_name in ("seed", "baseline_hpo_trials", "baseline_hpo_compute_budget"):
        assert field_name in WARM_SCHEMA.names, f"Missing {field_name} in WARM_SCHEMA"


def test_seed_fields_round_trip_arrow():
    """Verify seed/baseline_hpo_trials/baseline_hpo_compute_budget round-trip through Arrow."""
    r = Run(
        project_slug="p",
        command="c",
        argv=["c"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        seed=42,
        baseline_hpo_trials=50,
        baseline_hpo_compute_budget=3600.5,
    )
    table = r.to_arrow()
    assert table.column("seed")[0].as_py() == 42
    assert table.column("baseline_hpo_trials")[0].as_py() == 50
    assert table.column("baseline_hpo_compute_budget")[0].as_py() == 3600.5

    r2 = Run.from_arrow_row(table.to_pydict(), 0)
    assert r2.seed == 42
    assert r2.baseline_hpo_trials == 50
    assert r2.baseline_hpo_compute_budget == 3600.5


def test_seed_fields_none_round_trip_arrow():
    """Verify seed=None (not seed=0) round-trips through Arrow serialization."""
    r = Run(
        project_slug="p",
        command="c",
        argv=["c"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
    )
    table = r.to_arrow()
    assert table.column("seed")[0].as_py() is None
    assert table.column("baseline_hpo_trials")[0].as_py() is None
    assert table.column("baseline_hpo_compute_budget")[0].as_py() is None

    r2 = Run.from_arrow_row(table.to_pydict(), 0)
    assert r2.seed is None
    assert r2.baseline_hpo_trials is None
    assert r2.baseline_hpo_compute_budget is None


def test_run_stdout_sha256_defaults_none():
    """Verify B2-07's stdout_sha256 field defaults to None."""
    r = Run(
        project_slug="p",
        command="c",
        argv=["c"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
    )
    assert r.stdout_sha256 is None


def test_stdout_sha256_in_cool_and_warm_schema():
    """Verify stdout_sha256 is in both COOL_SCHEMA and WARM_SCHEMA as a nullable string."""
    for schema in (COOL_SCHEMA, WARM_SCHEMA):
        assert "stdout_sha256" in schema.names
        field_obj = next(f for f in schema if f.name == "stdout_sha256")
        assert field_obj.type == pa.string()
        assert field_obj.nullable


def test_stdout_sha256_round_trip_arrow():
    """Verify stdout_sha256 round-trips through Arrow serialization."""
    r = Run(
        project_slug="p",
        command="c",
        argv=["c"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        stdout_sha256="deadbeef" * 8,
    )
    table = r.to_arrow()
    assert table.column("stdout_sha256")[0].as_py() == "deadbeef" * 8

    r2 = Run.from_arrow_row(table.to_pydict(), 0)
    assert r2.stdout_sha256 == "deadbeef" * 8


def test_stdout_sha256_none_round_trip_arrow():
    """Verify stdout_sha256=None (not empty string) round-trips through Arrow serialization."""
    r = Run(
        project_slug="p",
        command="c",
        argv=["c"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
    )
    table = r.to_arrow()
    assert table.column("stdout_sha256")[0].as_py() is None

    r2 = Run.from_arrow_row(table.to_pydict(), 0)
    assert r2.stdout_sha256 is None


def test_run_component_fields_default_none():
    """Verify B2-08's component_id/component_sidecar_sha256 fields default to None."""
    r = Run(
        project_slug="p",
        command="c",
        argv=["c"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
    )
    assert r.component_id is None
    assert r.component_sidecar_sha256 is None


def test_component_fields_in_cool_and_warm_schema():
    """Verify component_id/component_sidecar_sha256 are in both COOL_SCHEMA and WARM_SCHEMA."""
    for schema in (COOL_SCHEMA, WARM_SCHEMA):
        for field_name in ("component_id", "component_sidecar_sha256"):
            assert field_name in schema.names
            field_obj = next(f for f in schema if f.name == field_name)
            assert field_obj.type == pa.string()
            assert field_obj.nullable


def test_component_fields_round_trip_arrow():
    """Verify component_id/component_sidecar_sha256 round-trip through Arrow serialization."""
    r = Run(
        project_slug="p",
        command="c",
        argv=["c"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        component_id="stage_bundle.preprocess",
        component_sidecar_sha256="cafebabe" * 8,
    )
    table = r.to_arrow()
    assert table.column("component_id")[0].as_py() == "stage_bundle.preprocess"
    assert table.column("component_sidecar_sha256")[0].as_py() == "cafebabe" * 8

    r2 = Run.from_arrow_row(table.to_pydict(), 0)
    assert r2.component_id == "stage_bundle.preprocess"
    assert r2.component_sidecar_sha256 == "cafebabe" * 8


def test_component_fields_none_round_trip_arrow():
    """Verify component fields default to None (not empty string) through Arrow serialization."""
    r = Run(
        project_slug="p",
        command="c",
        argv=["c"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
    )
    table = r.to_arrow()
    assert table.column("component_id")[0].as_py() is None
    assert table.column("component_sidecar_sha256")[0].as_py() is None

    r2 = Run.from_arrow_row(table.to_pydict(), 0)
    assert r2.component_id is None
    assert r2.component_sidecar_sha256 is None
