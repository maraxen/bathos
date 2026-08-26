"""Blast-radius shadow-trigger tests (SAC-4 through SAC-8, backlog #4555)."""

from __future__ import annotations

import subprocess

import pytest

from bathos.blast_radius import (
    fold_blast_radius_state,
    identify_fix_like_keyword,
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
        # Message must itself be keyword-matching -- otherwise record_shadow_trigger
        # returns None from the keyword gate (SAC-5) rather than exercising the
        # no-parent ValueError path this test targets.
        first_sha = _commit_file(repo, "foo.py", "a = 1\n", "fix bug")
        record = record_shadow_trigger(catalog_dir, repo, first_sha)
        assert record is None

    def test_non_matching_commit_never_calls_assess_blast_radius(
        self, repo, catalog_dir, monkeypatch
    ):
        """SAC-5: for a non-matching commit, no assessment is attempted at all.

        Regression for the confirmed dead-code divergence (PR #54 second jury
        round): record_shadow_trigger previously never checked the keyword
        itself at all -- the only gate was the hook's own shell `case`
        statement, which used a different (unanchored substring) pattern than
        matches_fix_like_keywords/identify_fix_like_keyword. This asserts the
        Python-side function is now itself the authoritative gate."""
        import bathos.blast_radius as blast_radius_mod

        _commit_file(repo, "foo.py", "a = 1\n", "initial")
        unrelated_sha = _commit_file(repo, "foo.py", "a = 2\n", "add a feature")

        called = []

        def _spy(*args, **kwargs):
            called.append((args, kwargs))
            raise AssertionError("assess_blast_radius must not be called")

        monkeypatch.setattr(blast_radius_mod, "assess_blast_radius", _spy)

        record = record_shadow_trigger(catalog_dir, repo, unrelated_sha)
        assert record is None
        assert not called, "assess_blast_radius must never run for a non-matching commit"

    def test_matched_keyword_is_captured_in_the_record(self, repo, catalog_dir):
        """SAC-8: the review surface must be able to show which keyword matched."""
        pre_sha = _commit_file(repo, "scripts/experiments/foo.py", "a = 1\n", "initial")
        fix_sha = _commit_file(
            repo, "scripts/experiments/foo.py", "a = 2\n", "Fixes the regression"
        )
        run = Run(
            project_slug="proj", command="scripts/experiments/foo.py",
            argv=["scripts/experiments/foo.py"], git_hash=pre_sha,
            git_branch="main", git_dirty=False,
        )
        write_run(run, catalog_dir)

        record = record_shadow_trigger(catalog_dir, repo, fix_sha)

        assert record is not None
        # "Fixes the regression" matches "fixes" first (leftmost in the message).
        assert "keyword=fixes" in record.match_reason


class TestIdentifyFixLikeKeyword:
    @pytest.mark.parametrize(
        "msg,expected",
        [
            ("fix bug", "fix"),
            ("Fixes #123", "fixes"),
            ("BUG: crash", "bug"),
            ("add feature", None),
            ("prefix cleanup", None),  # word-boundary: "prefix" must not match "fix"
        ],
    )
    def test_identifies_expected_keyword(self, msg, expected):
        assert identify_fix_like_keyword(msg) == expected
