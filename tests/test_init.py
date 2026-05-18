from pathlib import Path

from bathos.init import SCRIPT_DIRS, init_project


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
