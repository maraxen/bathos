from pathlib import Path

from bathos.config import find_project_config, load_project_config


def test_find_config_in_current_dir(tmp_path: Path):
    cfg = tmp_path / ".bth.toml"
    cfg.write_text('[project]\nslug = "myproj"\nroot = "/home/user/projects/myproj"\n')
    result = find_project_config(tmp_path)
    assert result == cfg


def test_find_config_walks_up(tmp_path: Path):
    cfg = tmp_path / ".bth.toml"
    cfg.write_text('[project]\nslug = "myproj"\nroot = "/home/user/projects/myproj"\n')
    subdir = tmp_path / "scripts" / "experiments"
    subdir.mkdir(parents=True)
    result = find_project_config(subdir)
    assert result == cfg


def test_find_config_returns_none_when_absent(tmp_path: Path):
    result = find_project_config(tmp_path)
    assert result is None


def test_load_minimal_config(tmp_path: Path):
    cfg = tmp_path / ".bth.toml"
    cfg.write_text('[project]\nslug = "prolix"\nroot = "/home/user/projects/prolix"\n')
    pc = load_project_config(cfg)
    assert pc.slug == "prolix"
    assert pc.root == Path("/home/user/projects/prolix")
    assert pc.remotes == {}
    assert pc.slurm == {}


def test_load_config_with_remote_and_slurm(tmp_path: Path):
    cfg = tmp_path / ".bth.toml"
    cfg.write_text(
        '[project]\nslug = "prolix"\nroot = "/home/user/projects/prolix"\n'
        '[slurm]\npartition = "pi_so3"\ndefault_walltime = "04:00:00"\n'
        '[remotes.engaging]\nhost = "engaging"\nremote_root = "~/projects/prolix"\n'
    )
    pc = load_project_config(cfg)
    assert pc.slurm["partition"] == "pi_so3"
    assert pc.remotes["engaging"]["host"] == "engaging"


def test_default_catalog_dir():
    from bathos.config import default_catalog_dir

    d = default_catalog_dir()
    assert d == Path.home() / ".bth" / "catalog"


def test_load_config_with_custom_catalog_dir(tmp_path: Path):
    custom_catalog = tmp_path / "my_custom_catalog"
    cfg = tmp_path / ".bth.toml"
    cfg.write_text(
        f'[project]\nslug = "prolix"\nroot = "{tmp_path}"\ncatalog_dir = "{custom_catalog}"\n'
    )
    pc = load_project_config(cfg)
    assert pc.catalog_dir == custom_catalog
