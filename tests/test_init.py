from pathlib import Path

from bathos.git_pin import MANIFEST_GITIGNORE_LINES
from bathos.init import SCRIPT_DIRS, InitReport, init_project


def test_creates_all_script_dirs(tmp_path: Path):
    init_project(tmp_path, slug="myproj", catalog_dir=tmp_path / ".bth" / "catalog")
    for d in SCRIPT_DIRS:
        assert (tmp_path / d).is_dir(), f"Missing: {d}"


def test_writes_bth_toml(tmp_path: Path):
    init_project(tmp_path, slug="myproj", catalog_dir=tmp_path / ".bth" / "catalog")
    toml = (tmp_path / ".bth.toml").read_text()
    assert 'slug = "myproj"' in toml
    assert str(tmp_path) in toml


def test_writes_bth_env_sh(tmp_path: Path):
    init_project(tmp_path, slug="myproj", catalog_dir=tmp_path / ".bth" / "catalog")
    env_sh = (tmp_path / "scripts" / "slurm" / "_bth_env.sh").read_text()
    assert "BTH_PROJECT_SLUG" in env_sh
    assert "myproj" in env_sh


def test_adds_scratch_to_gitignore_if_present(tmp_path: Path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("*.pyc\n")
    init_project(tmp_path, slug="myproj", catalog_dir=tmp_path / ".bth" / "catalog")
    content = gitignore.read_text()
    assert "scripts/scratch/" in content


def test_creates_gitignore_if_absent(tmp_path: Path):
    init_project(tmp_path, slug="myproj", catalog_dir=tmp_path / ".bth" / "catalog")
    content = (tmp_path / ".gitignore").read_text()
    assert "scripts/scratch/" in content


def test_idempotent_on_rerun(tmp_path: Path):
    catalog = tmp_path / ".bth" / "catalog"
    init_project(tmp_path, slug="myproj", catalog_dir=catalog)
    init_project(tmp_path, slug="myproj", catalog_dir=catalog)  # should not raise
    dirs = [d for d in SCRIPT_DIRS if (tmp_path / d).is_dir()]
    assert len(dirs) == len(SCRIPT_DIRS)


def test_gitignore_includes_manifest_lines(tmp_path: Path):
    """Debt #1943: fresh `bth init` gitignores the run manifest so a run in a clean tree
    never gets dirtied purely by its own provenance bookkeeping."""
    init_project(tmp_path, slug="myproj", catalog_dir=tmp_path / ".bth" / "catalog")
    content = (tmp_path / ".gitignore").read_text()
    for line in MANIFEST_GITIGNORE_LINES:
        assert line in content


def test_fresh_init_reports_created(tmp_path: Path):
    report = init_project(tmp_path, slug="myproj", catalog_dir=tmp_path / ".bth" / "catalog")
    assert isinstance(report, InitReport)
    assert report.created is True
    assert "[project]" in report.added


# ---------------------------------------------------------------------------
# Debt #1952: re-`init` on an already-initialized project must never destroy
# existing config -- only ever fill in what's missing.
# ---------------------------------------------------------------------------


def test_reinit_preserves_existing_remote_and_slurm_config(tmp_path: Path):
    catalog = tmp_path / ".bth" / "catalog"
    init_project(tmp_path, slug="myproj", catalog_dir=catalog)

    # Simulate a hand-edited / previously-configured .bth.toml with cluster config that
    # a plain re-`init` must not lose.
    toml_path = tmp_path / ".bth.toml"
    original = toml_path.read_text()
    original += (
        '\n[remotes.engaging]\n'
        'host = "engaging"\n'
        'remote_root = "/orcd/data/example/myproj"\n'
        '\n[slurm]\n'
        'partition = "pi_so3"\n'
        'preset = "gpu-a100"\n'
    )
    toml_path.write_text(original)

    report = init_project(
        tmp_path,
        slug="myproj",
        catalog_dir=catalog,
        remote="engaging:/some/other/path",
        slurm_partition="mit_preemptable",
    )

    after = toml_path.read_text()
    assert after == original, "re-init must not touch a single existing byte"
    assert 'remote_root = "/orcd/data/example/myproj"' in after
    assert 'partition = "pi_so3"' in after
    assert 'preset = "gpu-a100"' in after

    assert report.created is False
    assert "[remotes.engaging]" in report.preserved
    assert "[slurm].partition" in report.preserved
    assert any("engaging:/some/other/path" in s for s in report.skipped_requests)
    assert any("mit_preemptable" in s for s in report.skipped_requests)


def test_reinit_adds_a_new_remote_without_disturbing_an_existing_one(tmp_path: Path):
    catalog = tmp_path / ".bth" / "catalog"
    init_project(tmp_path, slug="myproj", catalog_dir=catalog, remote="engaging:/data/myproj")

    toml_path = tmp_path / ".bth.toml"
    before = toml_path.read_text()
    assert '[remotes.engaging]' in before

    report = init_project(
        tmp_path, slug="myproj", catalog_dir=catalog, remote="titanix:/data/myproj2"
    )

    after = toml_path.read_text()
    assert '[remotes.engaging]' in after
    assert 'remote_root = "/data/myproj"' in after  # original untouched
    assert '[remotes.titanix]' in after
    assert 'remote_root = "/data/myproj2"' in after  # new one appended

    # This call only requested `titanix`, so only titanix gets reported -- engaging
    # wasn't touched or even inspected, its survival is just a byte-for-byte fact above.
    assert "[remotes.titanix]" in report.added


def test_reinit_with_no_new_args_touches_nothing(tmp_path: Path):
    catalog = tmp_path / ".bth" / "catalog"
    init_project(tmp_path, slug="myproj", catalog_dir=catalog)
    toml_path = tmp_path / ".bth.toml"
    before = toml_path.read_text()

    report = init_project(tmp_path, slug="myproj", catalog_dir=catalog)

    assert toml_path.read_text() == before
    assert report.created is False
    assert report.added == []


def test_reinit_on_unparseable_bth_toml_does_not_raise_or_overwrite(tmp_path: Path):
    catalog = tmp_path / ".bth" / "catalog"
    (tmp_path / ".bth.toml").write_text("this is not [ valid toml")

    report = init_project(tmp_path, slug="myproj", catalog_dir=catalog)

    assert (tmp_path / ".bth.toml").read_text() == "this is not [ valid toml"
    assert report.created is False
