"""Git-hook wrapping tests (backlog #4555). Blast-radius-agnostic -- these
test the generic install/uninstall/chain mechanics only."""

from __future__ import annotations

import subprocess

import pytest

from bathos.git_hooks import install_managed_hooks, uninstall_managed_hooks


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(["init"], r)
    _git(["config", "user.email", "test@example.com"], r)
    _git(["config", "user.name", "Test"], r)
    return r


def _get_hooks_path(repo):
    result = subprocess.run(
        ["git", "config", "--get", "core.hooksPath"], cwd=repo,
        capture_output=True, text=True,
    )
    return result.stdout.strip() if result.returncode == 0 else None


class TestInstallFreshRepo:
    def test_sets_core_hooks_path(self, repo, tmp_path):
        managed = tmp_path / "managed"
        install_managed_hooks(repo, managed, {"post-commit": "#!/bin/sh\necho hi\n"})
        assert _get_hooks_path(repo) == str(managed)

    def test_installed_hook_is_executable_and_runs(self, repo, tmp_path):
        managed = tmp_path / "managed"
        marker = tmp_path / "marker.txt"
        install_managed_hooks(
            repo, managed, {"post-commit": f"#!/bin/sh\ntouch {marker}\n"}
        )
        (repo / "f.txt").write_text("x")
        _git(["add", "f.txt"], repo)
        _git(["commit", "-m", "test"], repo)
        assert marker.exists()


class TestPreservesExistingDefaultHooks:
    def test_preexisting_post_commit_is_chained(self, repo, tmp_path):
        default_hooks = repo / ".git" / "hooks"
        original_marker = tmp_path / "original_ran.txt"
        original = default_hooks / "post-commit"
        original.write_text(f"#!/bin/sh\ntouch {original_marker}\n")
        original.chmod(0o755)

        managed = tmp_path / "managed"
        bathos_marker = tmp_path / "bathos_ran.txt"
        install_managed_hooks(
            repo, managed, {"post-commit": f"#!/bin/sh\ntouch {bathos_marker}\n"}
        )
        (repo / "f.txt").write_text("x")
        _git(["add", "f.txt"], repo)
        _git(["commit", "-m", "test"], repo)

        assert original_marker.exists(), "pre-existing post-commit must still fire"
        assert bathos_marker.exists(), "bathos's own logic must also fire"

    def test_other_preexisting_hooks_are_untouched(self, repo, tmp_path):
        default_hooks = repo / ".git" / "hooks"
        pre_commit_marker = tmp_path / "pre_commit_ran.txt"
        pre_commit = default_hooks / "pre-commit"
        pre_commit.write_text(f"#!/bin/sh\ntouch {pre_commit_marker}\n")
        pre_commit.chmod(0o755)

        managed = tmp_path / "managed"
        install_managed_hooks(repo, managed, {"post-commit": "#!/bin/sh\nexit 0\n"})
        (repo / "f.txt").write_text("x")
        _git(["add", "f.txt"], repo)
        _git(["commit", "-m", "test"], repo)

        assert pre_commit_marker.exists(), "unrelated pre-existing hook must still fire"


class TestPreservesExistingCoreHooksPath:
    def test_existing_custom_hooks_path_is_chained(self, repo, tmp_path):
        custom_hooks = tmp_path / "custom_hooks"
        custom_hooks.mkdir()
        original_marker = tmp_path / "custom_ran.txt"
        original = custom_hooks / "post-commit"
        original.write_text(f"#!/bin/sh\ntouch {original_marker}\n")
        original.chmod(0o755)
        _git(["config", "core.hooksPath", str(custom_hooks)], repo)

        managed = tmp_path / "managed"
        bathos_marker = tmp_path / "bathos_ran.txt"
        install_managed_hooks(
            repo, managed, {"post-commit": f"#!/bin/sh\ntouch {bathos_marker}\n"}
        )
        (repo / "f.txt").write_text("x")
        _git(["add", "f.txt"], repo)
        _git(["commit", "-m", "test"], repo)

        assert original_marker.exists()
        assert bathos_marker.exists()
        assert _get_hooks_path(repo) == str(managed)


class TestUninstall:
    def test_restores_unset_hooks_path(self, repo, tmp_path):
        managed = tmp_path / "managed"
        install_managed_hooks(repo, managed, {"post-commit": "#!/bin/sh\nexit 0\n"})
        uninstall_managed_hooks(repo, managed)
        assert _get_hooks_path(repo) is None
        assert not managed.exists()

    def test_restores_previous_custom_hooks_path(self, repo, tmp_path):
        custom_hooks = tmp_path / "custom_hooks"
        custom_hooks.mkdir()
        _git(["config", "core.hooksPath", str(custom_hooks)], repo)

        managed = tmp_path / "managed"
        install_managed_hooks(repo, managed, {"post-commit": "#!/bin/sh\nexit 0\n"})
        uninstall_managed_hooks(repo, managed)

        assert _get_hooks_path(repo) == str(custom_hooks)

    def test_uninstall_without_install_raises(self, repo, tmp_path):
        with pytest.raises(FileNotFoundError):
            uninstall_managed_hooks(repo, tmp_path / "never_installed")


class TestIdempotentInstall:
    def test_installing_twice_does_not_double_chain(self, repo, tmp_path):
        managed = tmp_path / "managed"
        marker = tmp_path / "marker.txt"
        script = f"#!/bin/sh\necho x >> {marker}\n"
        install_managed_hooks(repo, managed, {"post-commit": script})
        install_managed_hooks(repo, managed, {"post-commit": script})

        (repo / "f.txt").write_text("x")
        _git(["add", "f.txt"], repo)
        _git(["commit", "-m", "test"], repo)

        # Exactly one line -- not two (which would mean the second install
        # chained to the first install's already-bathos-owned wrapper).
        assert marker.read_text().count("x") == 1
