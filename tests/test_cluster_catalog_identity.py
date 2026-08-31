"""Cluster jobs and bth sync must share {remote_root}/.bth/catalog."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bathos.cluster_catalog import (
    CatalogIdentityError,
    check_env_catalog_matches_remote,
    cluster_catalog_export,
    ensure_remote_catalog_dir,
    remote_catalog_path,
    write_bth_env_sh,
)
from bathos.init import init_project
from bathos.remote import add_remote


def test_remote_catalog_path_appends_bth_catalog():
    assert remote_catalog_path("~/projects/prolix") == "~/projects/prolix/.bth/catalog"


def test_cluster_catalog_export_uses_home_for_tilde():
    export = cluster_catalog_export("~/projects/prolix")
    assert export == "${HOME}/projects/prolix/.bth/catalog"
    assert "~/.bth/catalog" not in export
    assert "/.bth/catalog" in export


def test_cluster_catalog_export_fallback_is_project_root_not_home():
    export = cluster_catalog_export(None)
    assert export == "${BTH_PROJECT_ROOT}/.bth/catalog"


def test_init_with_remote_exports_remote_catalog_not_local_home(tmp_path: Path):
    local_home_catalog = Path.home() / ".bth" / "catalog"
    init_project(
        tmp_path,
        slug="prolix",
        catalog_dir=local_home_catalog,
        remote="engaging:~/projects/prolix",
    )
    env_sh = (tmp_path / "scripts" / "slurm" / "_bth_env.sh").read_text()
    assert 'export BTH_CATALOG_DIR="${HOME}/projects/prolix/.bth/catalog"' in env_sh
    assert 'export BTH_PROJECT_ROOT="${HOME}/projects/prolix"' in env_sh
    assert str(local_home_catalog) not in env_sh
    assert '"${HOME}/.bth/catalog"' not in env_sh


def test_init_without_remote_falls_back_to_project_root_catalog(tmp_path: Path):
    init_project(tmp_path, slug="prolix", catalog_dir=Path.home() / ".bth" / "catalog")
    env_sh = (tmp_path / "scripts" / "slurm" / "_bth_env.sh").read_text()
    assert 'export BTH_CATALOG_DIR="${BTH_PROJECT_ROOT}/.bth/catalog"' in env_sh


def test_add_remote_rewrites_env_helper_catalog_dir(tmp_path: Path):
    init_project(tmp_path, slug="prolix", catalog_dir=tmp_path / "local-catalog")
    add_remote(tmp_path / ".bth.toml", "engaging", "engaging", "~/projects/prolix")
    env_sh = (tmp_path / "scripts" / "slurm" / "_bth_env.sh").read_text()
    assert 'export BTH_CATALOG_DIR="${HOME}/projects/prolix/.bth/catalog"' in env_sh


def test_preflight_rejects_home_catalog_when_remote_is_project_tree(tmp_path: Path):
    env_path = tmp_path / "scripts" / "slurm" / "_bth_env.sh"
    env_path.parent.mkdir(parents=True)
    env_path.write_text(
        'export BTH_PROJECT_ROOT="/home/x/projects/prolix"\n'
        'export BTH_CATALOG_DIR="${HOME}/.bth/catalog"\n'
    )
    with pytest.raises(CatalogIdentityError, match="BTH_CATALOG_DIR"):
        check_env_catalog_matches_remote(tmp_path, "~/projects/prolix")


def test_preflight_accepts_matching_remote_catalog(tmp_path: Path):
    write_bth_env_sh(tmp_path, slug="prolix", remote_root="~/projects/prolix")
    check_env_catalog_matches_remote(tmp_path, "~/projects/prolix")


def test_ensure_remote_catalog_dir_sshes_mkdir(monkeypatch):
    mock_run = MagicMock(return_value=MagicMock(returncode=0, stderr=""))
    monkeypatch.setattr("bathos.cluster_catalog.subprocess.run", mock_run)
    ensure_remote_catalog_dir("engaging", "~/projects/prolix")
    argv = mock_run.call_args[0][0]
    assert argv[:6] == [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "engaging",
    ]
    assert "--" in argv
    remote_cmd = argv[-1]
    assert "mkdir -p --" in remote_cmd
    assert "${HOME}" in remote_cmd or "$HOME" in remote_cmd
    assert "'~/" not in remote_cmd
    assert ".bth/catalog/campaigns" in remote_cmd


def test_submit_refuses_mismatched_env_catalog(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
    (tmp_path / ".bth.toml").write_text(
        "[project]\n"
        'slug = "myproject"\n'
        f'root = "{tmp_path}"\n'
        "\n"
        "[slurm]\n"
        'remote = "engaging"\n'
        'preset = "gpu"\n'
        "\n"
        "[remotes.engaging]\n"
        'host = "engaging"\n'
        'remote_root = "~/projects/myproject"\n'
    )
    env_path = tmp_path / "scripts" / "slurm" / "_bth_env.sh"
    env_path.parent.mkdir(parents=True)
    env_path.write_text('export BTH_CATALOG_DIR="${HOME}/.bth/catalog"\n')

    from bathos.cli_cyclopts import app
    from tests._cyclopts_runner import CyclopticRunner

    result = CyclopticRunner().invoke(
        app, ["submit", "--no-wait", "uv", "run", "python", "train.py"]
    )
    assert result.exit_code != 0
    assert "BTH_CATALOG_DIR" in result.output
