"""Blast-radius shadow-trigger tests (SAC-4 through SAC-8, backlog #4555)."""

from __future__ import annotations

import subprocess

import pytest

from bathos.blast_radius import (
    fold_blast_radius_state,
    matches_fix_like_keywords,
    record_shadow_trigger,
)
from bathos.catalog import init_catalog, write_run
from bathos.schema import Run


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _commit_file(repo, relpath, content, message):
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    _git(["add", relpath], repo)
    _git(["commit", "-m", message], repo)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(["init"], r)
    _git(["config", "user.email", "test@example.com"], r)
    _git(["config", "user.name", "Test"], r)
    return r


@pytest.fixture
def catalog_dir(tmp_path):
    cat = tmp_path / "catalog"
    init_catalog(cat)
    return cat


class TestKeywordFilter:
    @pytest.mark.parametrize("msg", ["fix bug", "Fixes #123", "hotfix: patch", "BUG: crash"])
    def test_matches_fix_like_messages(self, msg):
        assert matches_fix_like_keywords(msg)

    @pytest.mark.parametrize("msg", ["add feature", "refactor module", "update docs"])
    def test_does_not_match_unrelated_messages(self, msg):
        assert not matches_fix_like_keywords(msg)


class TestRecordShadowTrigger:
    def test_records_shadow_only_state(self, repo, catalog_dir):
        pre_fix_sha = _commit_file(repo, "scripts/experiments/foo.py", "a = 1\n", "initial")
        fix_sha = _commit_file(repo, "scripts/experiments/foo.py", "a = 2\n", "fix bug")

        run = Run(
            project_slug="proj", command="scripts/experiments/foo.py",
            argv=["scripts/experiments/foo.py"], git_hash=pre_fix_sha,
            git_branch="main", git_dirty=False,
        )
        write_run(run, catalog_dir)

        record = record_shadow_trigger(catalog_dir, repo, fix_sha)

        assert record is not None
        assert record.entity_type == "shadow_trigger"
        assert record.entity_id == fix_sha
        assert record.to_state == "shadow_only"
        assert run.id in record.match_reason

    def test_never_pollutes_real_run_state(self, repo, catalog_dir):
        pre_fix_sha = _commit_file(repo, "scripts/experiments/foo.py", "a = 1\n", "initial")
        fix_sha = _commit_file(repo, "scripts/experiments/foo.py", "a = 2\n", "fix bug")

        run = Run(
            project_slug="proj", command="scripts/experiments/foo.py",
            argv=["scripts/experiments/foo.py"], git_hash=pre_fix_sha,
            git_branch="main", git_dirty=False,
        )
        write_run(run, catalog_dir)

        record_shadow_trigger(catalog_dir, repo, fix_sha)

        # SAC-7: the shadow trigger must never be visible via the real-entity reads.
        assert fold_blast_radius_state(catalog_dir, "run", run.id) == "clean"

    def test_first_commit_with_no_parent_fails_quietly(self, repo, catalog_dir):
        first_sha = _commit_file(repo, "foo.py", "a = 1\n", "initial")
        record = record_shadow_trigger(catalog_dir, repo, first_sha)
        assert record is None
