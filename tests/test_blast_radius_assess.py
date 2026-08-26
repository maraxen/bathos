"""Blast-radius assessment tests (Phase 1, backlog #4551).

Uses a real throwaway git repo (tmp_path) so git diff/merge-base behavior is exercised
for real, not mocked -- these are exactly the primitives the heuristic-noise pre-mortem
(spec) said to make auditable, so the tests must prove the match_reason/matched_files
fields are actually populated, not just that a bucket assignment happened.
"""

from __future__ import annotations

import subprocess

import pytest

from bathos.blast_radius import assess_blast_radius
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


def _run(catalog_dir, *, command, argv, git_hash, git_dirty=False):
    r = Run(
        project_slug="proj",
        command=command,
        argv=argv,
        git_hash=git_hash,
        git_branch="main",
        git_dirty=git_dirty,
    )
    write_run(r, catalog_dir)
    return r


class TestCommitAnchor:
    def test_run_predating_fix_is_affected(self, repo, catalog_dir):
        pre_fix_sha = _commit_file(repo, "scripts/experiments/foo.py", "buggy = True\n", "initial")
        fix_sha = _commit_file(repo, "scripts/experiments/foo.py", "buggy = False\n", "fix bug")

        _run(
            catalog_dir,
            command="scripts/experiments/foo.py",
            argv=["scripts/experiments/foo.py"],
            git_hash=pre_fix_sha,
        )

        report = assess_blast_radius(catalog_dir, repo, commit=fix_sha)

        assert report.changed_files == ["scripts/experiments/foo.py"]
        assert len(report.affected) == 1
        assert report.affected[0].matched_files == ["scripts/experiments/foo.py"]
        assert "foo.py" in report.affected[0].reason

    def test_run_at_fix_commit_itself_is_not_affected(self, repo, catalog_dir):
        _commit_file(repo, "scripts/experiments/foo.py", "buggy = True\n", "initial")
        fix_sha = _commit_file(repo, "scripts/experiments/foo.py", "buggy = False\n", "fix bug")

        _run(
            catalog_dir,
            command="scripts/experiments/foo.py",
            argv=["scripts/experiments/foo.py"],
            git_hash=fix_sha,
        )

        report = assess_blast_radius(catalog_dir, repo, commit=fix_sha)

        assert report.affected == []
        assert len(report.unaffected_run_ids) == 1

    def test_run_touching_unrelated_file_is_unaffected(self, repo, catalog_dir):
        _commit_file(repo, "scripts/experiments/foo.py", "a = 1\n", "initial")
        fix_sha = _commit_file(repo, "scripts/experiments/foo.py", "a = 2\n", "fix bug")

        pre_sha = subprocess.run(
            ["git", "rev-parse", f"{fix_sha}^"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        run2 = _run(
            catalog_dir,
            command="scripts/experiments/bar.py",
            argv=["scripts/experiments/bar.py"],
            git_hash=pre_sha,
        )

        report = assess_blast_radius(catalog_dir, repo, commit=fix_sha)

        affected_ids = [m.run_id for m in report.affected]
        assert run2.id not in affected_ids
        assert run2.id in report.unaffected_run_ids

    def test_dirty_run_touching_changed_file_is_unverifiable(self, repo, catalog_dir):
        pre_fix_sha = _commit_file(repo, "scripts/experiments/foo.py", "a = 1\n", "initial")
        fix_sha = _commit_file(repo, "scripts/experiments/foo.py", "a = 2\n", "fix bug")

        _run(
            catalog_dir,
            command="scripts/experiments/foo.py",
            argv=["scripts/experiments/foo.py"],
            git_hash=pre_fix_sha,
            git_dirty=True,
        )

        report = assess_blast_radius(catalog_dir, repo, commit=fix_sha)

        assert report.affected == []
        assert len(report.unverifiable) == 1
        assert "DIRTY_RUN" in report.unverifiable[0].reason


class TestCommitRangeAnchor:
    def test_range_boundary_is_range_start(self, repo, catalog_dir):
        base_sha = _commit_file(repo, "scripts/experiments/foo.py", "a = 1\n", "base")
        _commit_file(repo, "scripts/experiments/foo.py", "a = 2\n", "mid")
        tip_sha = _commit_file(repo, "scripts/experiments/foo.py", "a = 3\n", "tip")

        run_at_base = _run(
            catalog_dir,
            command="scripts/experiments/foo.py",
            argv=["scripts/experiments/foo.py"],
            git_hash=base_sha,
        )

        report = assess_blast_radius(
            catalog_dir, repo, commit_range=f"{base_sha}..{tip_sha}"
        )

        assert report.anchor_kind == "commit_range"
        affected_ids = [m.run_id for m in report.affected]
        assert run_at_base.id in affected_ids

    def test_range_requires_double_dot(self, repo, catalog_dir):
        with pytest.raises(ValueError):
            assess_blast_radius(catalog_dir, repo, commit_range="abc123")


class TestFileAnchor:
    def test_file_anchor_matches_without_ancestry_check(self, repo, catalog_dir):
        _commit_file(repo, "scripts/experiments/foo.py", "a = 1\n", "initial")

        run = _run(
            catalog_dir,
            command="scripts/experiments/foo.py",
            argv=["scripts/experiments/foo.py"],
            git_hash="deadbeef",  # not even a real sha -- no ancestry check for file anchor
        )

        report = assess_blast_radius(catalog_dir, repo, files=["scripts/experiments/foo.py"])

        assert report.anchor_kind == "file"
        assert len(report.affected) == 1
        assert report.affected[0].run_id == run.id
        assert "no commit ancestry check" in report.affected[0].reason


class TestInputValidation:
    def test_requires_exactly_one_anchor(self, repo, catalog_dir):
        with pytest.raises(ValueError):
            assess_blast_radius(catalog_dir, repo)
        with pytest.raises(ValueError):
            assess_blast_radius(catalog_dir, repo, commit="abc", files=["x.py"])
