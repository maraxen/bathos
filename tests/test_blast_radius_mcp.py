"""Blast-radius MCP tool tests (backlog #4551).

Calls the plain (pre-decorator) tool functions directly -- same convention as
tests/test_trust_ledger_mcp.py.
"""

from __future__ import annotations

import subprocess

import pytest

from bathos.catalog import init_catalog, write_run
from bathos.schema import Run


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(["init"], r)
    _git(["config", "user.email", "test@example.com"], r)
    _git(["config", "user.name", "Test"], r)
    (r / "foo.py").write_text("a = 1\n")
    _git(["add", "foo.py"], r)
    _git(["commit", "-m", "initial"], r)
    return r


@pytest.fixture
def catalog_dir(tmp_path):
    cat = tmp_path / "catalog"
    init_catalog(cat)
    return cat


def test_assess_tool_flags_matching_run(repo, catalog_dir):
    from bathos.mcp import blast_radius_assess_tool

    pre_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    run = Run(
        project_slug="p", command="foo.py", argv=["foo.py"],
        git_hash=pre_sha, git_branch="main", git_dirty=False,
    )
    write_run(run, catalog_dir)

    (repo / "foo.py").write_text("a = 2\n")
    _git(["add", "foo.py"], repo)
    _git(["commit", "-m", "fix"], repo)
    fix_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    result = blast_radius_assess_tool(
        catalog_dir=str(catalog_dir), project_root=str(repo), commit=fix_sha
    )

    assert "error" not in result
    assert result["flagged_count"] == 1
    assert len(result["affected"]) == 1


def test_assess_tool_requires_exactly_one_anchor(catalog_dir, repo):
    from bathos.mcp import blast_radius_assess_tool

    result = blast_radius_assess_tool(catalog_dir=str(catalog_dir), project_root=str(repo))
    assert "error" in result


def test_clear_tool_requires_reason(catalog_dir):
    from bathos.mcp import blast_radius_clear_tool

    result = blast_radius_clear_tool(
        catalog_dir=str(catalog_dir), entity_type="run", entity_id="run-x", reason=""
    )
    assert "error" in result


def test_status_tool_returns_clean_by_default(catalog_dir):
    from bathos.mcp import get_blast_radius_status_tool

    result = get_blast_radius_status_tool(
        catalog_dir=str(catalog_dir), entity_type="run", entity_id="never-flagged"
    )
    assert result["status"] == "clean"


def test_assess_tool_dependency_anchor_and_campaign_propagation(repo, catalog_dir):
    from bathos.campaigns import add_run_to_campaign, connect_catalog_db, create_campaign
    from bathos.checker import hash_dependency_lock
    from bathos.compact import compact as compact_catalog
    from bathos.mcp import blast_radius_assess_tool

    (repo / "uv.lock").write_text("old\n")
    old_hash = hash_dependency_lock(repo)
    run = Run(
        project_slug="p", command="foo.py", argv=["foo.py"], git_hash="abc",
        git_branch="main", git_dirty=False, dependency_lock_sha256=old_hash,
    )
    write_run(run, catalog_dir)
    compact_catalog(catalog_dir)
    db = connect_catalog_db(catalog_dir, read_only=False)
    campaign = create_campaign(db, "camp-mcp", "p", "exploration", catalog_dir=catalog_dir)
    add_run_to_campaign(db, campaign.id, run.id, catalog_dir=catalog_dir)
    db.close()

    (repo / "uv.lock").write_text("new\n")

    result = blast_radius_assess_tool(
        catalog_dir=str(catalog_dir), project_root=str(repo), dependency=True
    )

    assert "error" not in result
    assert result["anchor_kind"] == "dependency"
    assert result["flagged_count"] == 1
    assert result["campaign_flagged_count"] == 1
