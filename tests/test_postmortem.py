import json
import textwrap
from pathlib import Path
import pytest
import duckdb
from typer.testing import CliRunner

from bathos.catalog import init_catalog, write_run
from bathos.cli import app
from bathos.schema import Run
from bathos.compact import compact

# We import the postmortem functions which we expect to be implemented in bathos.postmortem.
# Since we are in the RED phase, this import or the tests will fail, which is correct.
try:
    from bathos.postmortem import parse_postmortem, validate_postmortem, Postmortem
except ImportError:
    # We define dummy place-holders if we want pytest to parse the file without immediate ImportError,
    # or we can let it raise ImportError immediately. Raising ImportError immediately is a perfectly valid
    # way to fail, but to ensure pytest runs and discovers other potential failures or shows exactly
    # what fails, let's check: does the prompt say "verify that running pytest tests/test_postmortem.py fails"?
    # Yes. Let's not catch ImportError, so it fails cleanly with ImportError, OR we can catch it and raise
    # NotImplementedError inside the tests/functions to make it more elegant.
    # Wait, in standard python testing, if a module is missing, importing it at the top level makes the entire
    # test suite/file fail to load. Usually, it's better to import them inside the test functions, or let them fail.
    # Let's import them inside the test functions or let the ImportError happen at top level.
    # Actually, importing at the top level is fine because it fails the run immediately, but importing inside
    # the test functions allows pytest to collect the tests and show multiple failures if they are mocked or if
    # the test fails on import. Let's import inside the test functions or have a helper.
    # Let's do top level imports to be straightforward, since the instruction says "verify that running pytest
    # tests/test_postmortem.py fails (RED status)". Let's import inside the test functions to make the tests
    # discoverable, which provides a cleaner pytest output (e.g., "ImportError: cannot import name ...").
    pass

runner = CliRunner()


def test_parse_postmortem_valid(tmp_path: Path):
    """Test parsing a valid postmortem TOML file."""
    from bathos.postmortem import parse_postmortem, Postmortem

    postmortem_content = """
    run_id = "test-run-123"

    [postmortem]
    hypothesis_status = "held"
    summary = "The NVT simulation successfully maintained the target temperature."
    unexpected_observations = "Slight pressure spike at step 5000, but resolved."
    root_cause = "Thermostat damping parameter was too low initially."
    verdict_override = "pass"
    next_steps = "Proceed to NPT validation."

    [asset_links]
    checkpoint = "outputs/checkpoint.pt"
    log_file = { path = "outputs/run.log", sha256 = "123456abcdef123456abcdef123456abcdef123456abcdef123456abcdef1234" }
    """
    toml_path = tmp_path / "run.py.test-run-123.bth.postmortem.toml"
    toml_path.write_text(textwrap.dedent(postmortem_content))

    postmortem = parse_postmortem(toml_path)
    
    assert isinstance(postmortem, Postmortem)
    assert postmortem.run_id == "test-run-123"
    assert postmortem.hypothesis_status == "held"
    assert postmortem.summary == "The NVT simulation successfully maintained the target temperature."
    assert postmortem.unexpected_observations == "Slight pressure spike at step 5000, but resolved."
    assert postmortem.root_cause == "Thermostat damping parameter was too low initially."
    assert postmortem.verdict_override == "pass"
    assert postmortem.next_steps == "Proceed to NPT validation."
    
    assert isinstance(postmortem.asset_links, dict)
    assert postmortem.asset_links["checkpoint"] == "outputs/checkpoint.pt"
    assert isinstance(postmortem.asset_links["log_file"], dict)
    assert postmortem.asset_links["log_file"]["path"] == "outputs/run.log"
    assert postmortem.asset_links["log_file"]["sha256"] == "123456abcdef123456abcdef123456abcdef123456abcdef123456abcdef1234"


def test_parse_postmortem_invalid_toml(tmp_path: Path):
    """Test that parsing invalid TOML syntax raises a ValueError or PostmortemParseError."""
    from bathos.postmortem import parse_postmortem

    toml_path = tmp_path / "invalid.toml"
    toml_path.write_text("invalid = { TOML syntax error")

    with pytest.raises(Exception):
        parse_postmortem(toml_path)


def test_parse_postmortem_missing_fields(tmp_path: Path):
    """Test that parsing a TOML missing required fields fails validation during parsing."""
    from bathos.postmortem import parse_postmortem

    # Missing run_id and postmortem section
    toml_content = """
    [postmortem]
    summary = "No run_id"
    """
    toml_path = tmp_path / "missing.toml"
    toml_path.write_text(textwrap.dedent(toml_content))

    with pytest.raises(Exception):
        parse_postmortem(toml_path)


def test_validation_relative_paths(tmp_path: Path):
    """Test asset links validation: paths must be relative and stay within workspace root."""
    from bathos.postmortem import Postmortem, validate_postmortem

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    # Create dummy run to pass to validate_postmortem
    run = Run(
        project_slug="testproj",
        command="python run.py",
        argv=["python", "run.py"],
        git_hash="abcd123",
        git_branch="main",
        git_dirty=False,
    )

    # 1. Valid relative path
    pm_valid = Postmortem(
        run_id=run.id,
        hypothesis_status="held",
        summary="Summary",
        unexpected_observations="",
        root_cause="",
        verdict_override="pass",
        next_steps="",
        asset_links={"checkpoint": "outputs/chk.pt"},
    )
    result = validate_postmortem(pm_valid, workspace_root=workspace_root, run=run)
    assert result.ok is True
    assert len(result.errors) == 0

    # 2. Absolute path is invalid
    pm_abs = Postmortem(
        run_id=run.id,
        hypothesis_status="held",
        summary="Summary",
        unexpected_observations="",
        root_cause="",
        verdict_override="pass",
        next_steps="",
        asset_links={"checkpoint": "/absolute/path/chk.pt"},
    )
    result = validate_postmortem(pm_abs, workspace_root=workspace_root, run=run)
    assert result.ok is False
    assert any("absolute path" in err.message.lower() for err in result.errors)

    # 3. Path escaping workspace root using '..' is invalid
    pm_escape = Postmortem(
        run_id=run.id,
        hypothesis_status="held",
        summary="Summary",
        unexpected_observations="",
        root_cause="",
        verdict_override="pass",
        next_steps="",
        asset_links={"checkpoint": "../../../etc/passwd"},
    )
    result = validate_postmortem(pm_escape, workspace_root=workspace_root, run=run)
    assert result.ok is False
    assert any("escape the workspace" in err.message.lower() or "outside" in err.message.lower() for err in result.errors)


def test_validation_cryptographic_checksums(tmp_path: Path):
    """Test asset links validation: sha256 checksums must match the file content."""
    from bathos.postmortem import Postmortem, validate_postmortem

    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    
    # Create a dummy asset file
    asset_rel_path = Path("outputs/checkpoint.pt")
    asset_abs_path = workspace_root / asset_rel_path
    asset_abs_path.parent.mkdir(parents=True, exist_ok=True)
    asset_abs_path.write_text("dummy-checkpoint-data")
    
    # Calculate sha256 of "dummy-checkpoint-data"
    import hashlib
    correct_sha = hashlib.sha256(b"dummy-checkpoint-data").hexdigest()
    wrong_sha = "a" * 64

    run = Run(
        project_slug="testproj",
        command="python run.py",
        argv=["python", "run.py"],
        git_hash="abcd123",
        git_branch="main",
        git_dirty=False,
    )

    # 1. Matching sha256 should pass
    pm_correct = Postmortem(
        run_id=run.id,
        hypothesis_status="held",
        summary="Summary",
        unexpected_observations="",
        root_cause="",
        verdict_override="pass",
        next_steps="",
        asset_links={"checkpoint": {"path": str(asset_rel_path), "sha256": correct_sha}},
    )
    result = validate_postmortem(pm_correct, workspace_root=workspace_root, run=run)
    assert result.ok is True
    assert len(result.errors) == 0

    # 2. Mismatched sha256 should fail
    pm_wrong = Postmortem(
        run_id=run.id,
        hypothesis_status="held",
        summary="Summary",
        unexpected_observations="",
        root_cause="",
        verdict_override="pass",
        next_steps="",
        asset_links={"checkpoint": {"path": str(asset_rel_path), "sha256": wrong_sha}},
    )
    result = validate_postmortem(pm_wrong, workspace_root=workspace_root, run=run)
    assert result.ok is False
    assert any("checksum" in err.message.lower() or "sha256" in err.message.lower() for err in result.errors)

    # 3. Non-existent file should fail validation
    pm_missing_file = Postmortem(
        run_id=run.id,
        hypothesis_status="held",
        summary="Summary",
        unexpected_observations="",
        root_cause="",
        verdict_override="pass",
        next_steps="",
        asset_links={"checkpoint": {"path": "outputs/nonexistent.pt", "sha256": correct_sha}},
    )
    result = validate_postmortem(pm_missing_file, workspace_root=workspace_root, run=run)
    assert result.ok is False
    assert any("not exist" in err.message.lower() or "missing" in err.message.lower() for err in result.errors)


def test_validation_code_drift(tmp_path: Path):
    """Test validation detects code-drift and dirty git states between workspace and run."""
    from bathos.postmortem import Postmortem, validate_postmortem
    
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()

    # Initialize a mock git repo in workspace
    import subprocess
    subprocess.run(["git", "init"], cwd=workspace_root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=workspace_root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=workspace_root, check=True, capture_output=True)
    (workspace_root / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=workspace_root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=workspace_root, check=True, capture_output=True)

    # Get HEAD hash of the workspace
    current_head = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace_root, text=True).strip()

    # 1. Run with matching git state and clean repo -> passes
    run_clean = Run(
        project_slug="testproj",
        command="python run.py",
        argv=["python", "run.py"],
        git_hash=current_head,
        git_branch="master",
        git_dirty=False,
    )
    pm = Postmortem(
        run_id=run_clean.id,
        hypothesis_status="held",
        summary="Summary",
        unexpected_observations="",
        root_cause="",
        verdict_override="pass",
        next_steps="",
        asset_links={},
    )
    result = validate_postmortem(pm, workspace_root=workspace_root, run=run_clean)
    assert result.ok is True

    # 2. Run was recorded with git_dirty = True -> warning or error in validation
    run_dirty = Run(
        project_slug="testproj",
        command="python run.py",
        argv=["python", "run.py"],
        git_hash=current_head,
        git_branch="master",
        git_dirty=True,
    )
    result = validate_postmortem(pm, workspace_root=workspace_root, run=run_dirty)
    assert result.ok is False
    assert any("dirty" in err.message.lower() for err in result.errors)

    # 3. Run git_hash differs from workspace HEAD (drift) -> warning or error in validation
    run_drift = Run(
        project_slug="testproj",
        command="python run.py",
        argv=["python", "run.py"],
        git_hash="differenthash123456",
        git_branch="master",
        git_dirty=False,
    )
    result = validate_postmortem(pm, workspace_root=workspace_root, run=run_drift)
    assert result.ok is False
    assert any("drift" in err.message.lower() or "hash" in err.message.lower() for err in result.errors)


def test_validation_refutation_mapping():
    """Test validation of consistency between hypothesis_status and verdict_override."""
    from bathos.postmortem import Postmortem, validate_postmortem

    # 1. Invalid: hypothesis is refuted but verdict is pass
    pm_refuted_pass = Postmortem(
        run_id="run-1",
        hypothesis_status="refuted",
        summary="Summary",
        unexpected_observations="",
        root_cause="",
        verdict_override="pass",
        next_steps="",
        asset_links={},
    )
    result = validate_postmortem(pm_refuted_pass, workspace_root=Path("/dummy"), run=None)
    assert result.ok is False
    assert any("refuted" in err.message.lower() and "pass" in err.message.lower() for err in result.errors)

    # 2. Invalid: hypothesis is held but verdict is fail
    pm_held_fail = Postmortem(
        run_id="run-2",
        hypothesis_status="held",
        summary="Summary",
        unexpected_observations="",
        root_cause="",
        verdict_override="fail",
        next_steps="",
        asset_links={},
    )
    result = validate_postmortem(pm_held_fail, workspace_root=Path("/dummy"), run=None)
    assert result.ok is False
    assert any("held" in err.message.lower() and "fail" in err.message.lower() for err in result.errors)

    # 3. Valid: refuted and fail
    pm_refuted_fail = Postmortem(
        run_id="run-3",
        hypothesis_status="refuted",
        summary="Summary",
        unexpected_observations="",
        root_cause="",
        verdict_override="fail",
        next_steps="",
        asset_links={},
    )
    result = validate_postmortem(pm_refuted_fail, workspace_root=Path("/dummy"), run=None)
    assert result.ok is True

    # 4. Valid: held and pass
    pm_held_pass = Postmortem(
        run_id="run-4",
        hypothesis_status="held",
        summary="Summary",
        unexpected_observations="",
        root_cause="",
        verdict_override="pass",
        next_steps="",
        asset_links={},
    )
    result = validate_postmortem(pm_held_pass, workspace_root=Path("/dummy"), run=None)
    assert result.ok is True

    # 5. Valid: inconclusive and marginal
    pm_inconconclusive_marginal = Postmortem(
        run_id="run-5",
        hypothesis_status="inconclusive",
        summary="Summary",
        unexpected_observations="",
        root_cause="",
        verdict_override="marginal",
        next_steps="",
        asset_links={},
    )
    result = validate_postmortem(pm_inconconclusive_marginal, workspace_root=Path("/dummy"), run=None)
    assert result.ok is True


def test_compact_updates_postmortem(tmp_path: Path, monkeypatch):
    """Test that compact() ingests postmortem files, populates database columns, and overrides outcome."""
    # Set up mock workspace and catalog directories
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    # Configure environment
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    monkeypatch.chdir(workspace_root)

    # Write project configuration file
    (workspace_root / ".bth.toml").write_text(textwrap.dedent(f"""
        [project]
        slug = "testproj"
        root = "{workspace_root}"
        catalog_dir = "{catalog_dir}"
    """))

    init_catalog(catalog_dir)

    # Create and write a cool run fragment.
    # The run's outcome will initially be 'fail'. We will override it to 'pass' in postmortem.
    run = Run(
        project_slug="testproj",
        command="python scripts/run.py --seed 42",
        argv=["python", "scripts/run.py", "--seed", "42"],
        git_hash="hash123",
        git_branch="main",
        git_dirty=False,
        outcome="fail",
    )
    write_run(run, catalog_dir)

    # Create the postmortem adjacent to the script in workspace root
    script_dir = workspace_root / "scripts"
    script_dir.mkdir()
    script_file = script_dir / "run.py"
    script_file.write_text("pass")

    postmortem_path = script_dir / f"run.py.{run.id}.bth.postmortem.toml"
    postmortem_content = f"""
    run_id = "{run.id}"

    [postmortem]
    hypothesis_status = "held"
    summary = "Overridden to pass due to updated threshold."
    unexpected_observations = "None"
    root_cause = "N/A"
    verdict_override = "pass"
    next_steps = "Complete"

    [asset_links]
    log = "outputs/run.log"
    """
    postmortem_path.write_text(textwrap.dedent(postmortem_content))

    # Run compact
    res = compact(catalog_dir)
    assert res.ingested == 1

    # Verify DuckDB has updated columns & overridden outcome
    db_path = catalog_dir / "bathos.db"
    assert db_path.exists()
    con = duckdb.connect(str(db_path))
    
    # Query the runs table
    row = con.execute("SELECT outcome, postmortem_hypothesis_status, postmortem_summary, postmortem_verdict_override, postmortem_asset_links FROM runs WHERE id = ?", [run.id]).fetchone()
    assert row is not None
    
    # outcome must be overridden to 'pass'
    assert row[0] == "pass"
    # Postmortem columns should be populated
    assert row[1] == "held"
    assert row[2] == "Overridden to pass due to updated threshold."
    assert row[3] == "pass"
    # Asset links should be populated in DB as JSON
    asset_links_db = json.loads(row[4])
    assert asset_links_db["log"] == "outputs/run.log"


def test_cli_postmortem_scaffold(tmp_path: Path, monkeypatch):
    """Test Typer command 'bth postmortem scaffold <run_id>' creates the template file."""
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    monkeypatch.chdir(workspace_root)

    (workspace_root / ".bth.toml").write_text(textwrap.dedent(f"""
        [project]
        slug = "testproj"
        root = "{workspace_root}"
        catalog_dir = "{catalog_dir}"
    """))

    init_catalog(catalog_dir)

    # 1. Test scaffold with non-existent run_id fails
    result = runner.invoke(app, ["postmortem", "scaffold", "nonexistent"])
    assert result.exit_code != 0
    assert "Run not found" in result.output

    # 2. Test scaffold with valid run_id succeeds and creates TOML file
    run = Run(
        project_slug="testproj",
        command="python scripts/experiments/run_nvt.py --n 10",
        argv=["python", "scripts/experiments/run_nvt.py", "--n", "10"],
        git_hash="hash123",
        git_branch="main",
        git_dirty=False,
    )
    write_run(run, catalog_dir)

    # Create the script file so we can write adjacent to it
    script_dir = workspace_root / "scripts" / "experiments"
    script_dir.mkdir(parents=True)
    (script_dir / "run_nvt.py").write_text("pass")

    result = runner.invoke(app, ["postmortem", "scaffold", run.id])
    assert result.exit_code == 0

    expected_toml_path = script_dir / f"run_nvt.py.{run.id}.bth.postmortem.toml"
    assert expected_toml_path.exists()

    # Verify template content
    toml_content = expected_toml_path.read_text()
    assert f'run_id = "{run.id}"' in toml_content
    assert "[postmortem]" in toml_content
    assert "hypothesis_status =" in toml_content
    assert "[asset_links]" in toml_content


def test_cli_postmortem_show(tmp_path: Path, monkeypatch):
    """Test Typer command 'bth postmortem show <run_id>' displays info or validation errors."""
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()

    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    monkeypatch.chdir(workspace_root)

    (workspace_root / ".bth.toml").write_text(textwrap.dedent(f"""
        [project]
        slug = "testproj"
        root = "{workspace_root}"
        catalog_dir = "{catalog_dir}"
    """))

    init_catalog(catalog_dir)

    # Create run
    run = Run(
        project_slug="testproj",
        command="python scripts/run.py",
        argv=["python", "scripts/run.py"],
        git_hash="hash123",
        git_branch="main",
        git_dirty=False,
    )
    write_run(run, catalog_dir)

    script_file = workspace_root / "scripts" / "run.py"
    script_file.parent.mkdir(parents=True)
    script_file.write_text("pass")

    postmortem_path = workspace_root / "scripts" / f"run.py.{run.id}.bth.postmortem.toml"

    # 1. Test show when postmortem file does not exist
    result = runner.invoke(app, ["postmortem", "show", run.id])
    assert result.exit_code != 0
    assert "Postmortem not found" in result.output

    # 2. Test show with valid postmortem TOML
    postmortem_content = f"""
    run_id = "{run.id}"
    [postmortem]
    hypothesis_status = "held"
    summary = "Validation works!"
    unexpected_observations = "none"
    root_cause = "none"
    verdict_override = "pass"
    next_steps = "none"
    [asset_links]
    """
    postmortem_path.write_text(textwrap.dedent(postmortem_content))

    result = runner.invoke(app, ["postmortem", "show", run.id])
    assert result.exit_code == 0
    assert "Validation works!" in result.output
    assert "held" in result.output
    assert "pass" in result.output

    # 3. Test show with invalid postmortem TOML (refutation mapping violation)
    postmortem_invalid_content = f"""
    run_id = "{run.id}"
    [postmortem]
    hypothesis_status = "refuted"
    summary = "Refuted but passing (invalid)"
    unexpected_observations = "none"
    root_cause = "none"
    verdict_override = "pass"
    next_steps = "none"
    [asset_links]
    """
    postmortem_path.write_text(textwrap.dedent(postmortem_invalid_content))

    result = runner.invoke(app, ["postmortem", "show", run.id])
    assert result.exit_code != 0
    assert "Validation failed" in result.output
    assert "refuted" in result.output
    assert "pass" in result.output


# --- Worktree-aware workspace resolution (spec 260611) ---

def _init_repo_pm(path):
    import subprocess
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=path, check=True, capture_output=True)
    (path / "seed.txt").write_text("seed")
    subprocess.run(["git", "add", "."], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)


def test_postmortem_validates_worktree_asset(tmp_path: Path, monkeypatch):  # AC-8
    """Asset present only in the worktree validates against the LIVE worktree root,
    not the recorded main-checkout root (the bug this spec fixes)."""
    import hashlib
    import subprocess
    from bathos.postmortem import parse_postmortem, validate_postmortem
    from bathos.workspace import resolve_workspace

    monkeypatch.delenv("BTH_WORKSPACE_ROOT", raising=False)
    repo = tmp_path / "repo"
    _init_repo_pm(repo)
    # recorded [project] root points at the MAIN checkout
    (repo / ".bth.toml").write_text(f'[project]\nslug = "proj"\nroot = "{repo}"\n')
    wt = tmp_path / "wt1"
    subprocess.run(["git", "worktree", "add", str(wt)], cwd=repo, check=True, capture_output=True)

    # asset exists ONLY in the worktree
    (wt / "assets").mkdir()
    asset_bytes = b"figure-bytes"
    (wt / "assets" / "fig.png").write_bytes(asset_bytes)
    sha = hashlib.sha256(asset_bytes).hexdigest()

    pm_file = wt / "run.py.r1.bth.postmortem.toml"
    pm_file.write_text(
        'run_id = "r1"\n\n[postmortem]\nhypothesis_status = "unassigned"\n'
        'summary = "s"\nverdict_override = "none"\nstatus = "final"\n\n'
        f'[asset_links]\nfig = {{ path = "assets/fig.png", sha256 = "{sha}" }}\n'
    )
    pm = parse_postmortem(pm_file)

    # resolve_workspace from the worktree yields the live worktree root
    fs_root = resolve_workspace(wt).fs_root
    assert fs_root.resolve() == wt.resolve()
    # validates against the worktree (asset present, checksum matches)
    assert validate_postmortem(pm, workspace_root=fs_root).ok is True
    # against the recorded MAIN root the asset is absent -> fails (proves fix is load-bearing)
    assert validate_postmortem(pm, workspace_root=repo).ok is False


def test_mcp_postmortem_validate_explicit_param_wins(tmp_path: Path, monkeypatch):  # AC-11
    """An explicit workspace_root passed to the MCP mirror takes precedence over a
    discoverable .bth.toml recorded root (the precedence was inverted in code)."""
    import asyncio
    import hashlib
    from bathos import mcp

    monkeypatch.delenv("BTH_WORKSPACE_ROOT", raising=False)
    explicit_ws = tmp_path / "explicit"
    (explicit_ws / "assets").mkdir(parents=True)
    asset_bytes = b"abc"
    (explicit_ws / "assets" / "a.png").write_bytes(asset_bytes)
    sha = hashlib.sha256(asset_bytes).hexdigest()
    recorded = tmp_path / "recorded"
    recorded.mkdir()
    # a .bth.toml in explicit_ws whose recorded root is the (asset-less) `recorded` dir;
    # old behavior would override ws with this recorded root and FAIL to find the asset.
    (explicit_ws / ".bth.toml").write_text(f'[project]\nslug = "proj"\nroot = "{recorded}"\n')

    pm_file = explicit_ws / "p.bth.postmortem.toml"
    pm_file.write_text(
        'run_id = "r1"\n\n[postmortem]\nhypothesis_status = "unassigned"\n'
        'summary = "s"\nverdict_override = "none"\nstatus = "final"\n\n'
        f'[asset_links]\na = {{ path = "assets/a.png", sha256 = "{sha}" }}\n'
    )
    res = asyncio.run(
        mcp.postmortem_validate(path=str(pm_file), workspace_root=str(explicit_ws))
    )
    assert res["validation_ok"] is True  # explicit param won; asset resolved under explicit_ws
