"""Tests for bathos.gate — BP-2 synthetic-recovery gate ledger and staleness state machine."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bathos.claim import parse_claim
from bathos.gate import (
    gate_state,
    load_ledger,
    stamp_gate,
    synthetic_recovery_confound_check,
)


def _git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def git_repo(tmp_path):
    """A real git repo with one tracked guarded file, committed."""
    _git(["init"], tmp_path)
    _git(["config", "user.email", "test@example.com"], tmp_path)
    _git(["config", "user.name", "Test"], tmp_path)
    guarded = tmp_path / "src" / "component.py"
    guarded.parent.mkdir(parents=True)
    guarded.write_text("def f(): return 1\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "init"], tmp_path)
    sha = _git(["rev-parse", "HEAD"], tmp_path)
    return tmp_path, guarded, sha


class TestLedgerRoundTrip:
    def test_stamp_then_load(self, tmp_path):
        entry = stamp_gate(tmp_path, "my_gate", "pass", "abc123")
        assert entry.gate_name == "my_gate"
        assert entry.result == "pass"
        assert entry.sha == "abc123"

        ledger = load_ledger(tmp_path)
        assert ledger["gates"]["my_gate"]["result"] == "pass"
        assert ledger["gates"]["my_gate"]["sha"] == "abc123"

    def test_stamp_overwrites_prior_entry(self, tmp_path):
        stamp_gate(tmp_path, "my_gate", "fail", "sha1")
        stamp_gate(tmp_path, "my_gate", "pass", "sha2")
        ledger = load_ledger(tmp_path)
        assert ledger["gates"]["my_gate"]["result"] == "pass"
        assert ledger["gates"]["my_gate"]["sha"] == "sha2"

    def test_stamp_rejects_invalid_result(self, tmp_path):
        with pytest.raises(ValueError):
            stamp_gate(tmp_path, "my_gate", "maybe", "sha1")

    def test_load_ledger_missing_file_returns_empty(self, tmp_path):
        ledger = load_ledger(tmp_path)
        assert ledger["gates"] == {}


class TestGateStateMachine:
    def test_unknown_when_never_stamped(self, tmp_path):
        assert gate_state(tmp_path, "never_stamped", ["src/x.py"]) == "UNKNOWN"

    def test_red_when_last_result_fail(self, git_repo):
        tmp_path, guarded, sha = git_repo
        stamp_gate(tmp_path, "g", "fail", sha)
        assert gate_state(tmp_path, "g", ["src/component.py"]) == "RED"

    def test_green_when_pass_and_guards_unchanged(self, git_repo):
        tmp_path, guarded, sha = git_repo
        stamp_gate(tmp_path, "g", "pass", sha)
        assert gate_state(tmp_path, "g", ["src/component.py"]) == "GREEN"

    def test_stale_when_guard_edited_uncommitted_since_pass(self, git_repo):
        tmp_path, guarded, sha = git_repo
        stamp_gate(tmp_path, "g", "pass", sha)
        # Uncommitted edit to the guarded path
        guarded.write_text("def f(): return 2\n")
        assert gate_state(tmp_path, "g", ["src/component.py"]) == "STALE"

    def test_stale_when_guard_committed_since_pass(self, git_repo):
        tmp_path, guarded, sha = git_repo
        stamp_gate(tmp_path, "g", "pass", sha)
        guarded.write_text("def f(): return 3\n")
        _git(["add", "-A"], tmp_path)
        _git(["commit", "-m", "change guard"], tmp_path)
        assert gate_state(tmp_path, "g", ["src/component.py"]) == "STALE"

    def test_green_unaffected_by_unrelated_file_change(self, git_repo):
        tmp_path, guarded, sha = git_repo
        stamp_gate(tmp_path, "g", "pass", sha)
        (tmp_path / "unrelated.py").write_text("x = 1\n")
        assert gate_state(tmp_path, "g", ["src/component.py"]) == "GREEN"

    def test_fail_safe_on_bad_sha(self, git_repo):
        tmp_path, guarded, sha = git_repo
        stamp_gate(tmp_path, "g", "pass", "not-a-real-sha")
        assert gate_state(tmp_path, "g", ["src/component.py"]) == "STALE"


def _write_claim_with_synth(tmp_path, gate_name="g", guards=None):
    guards = guards if guards is not None else ["src/component.py"]
    guards_toml = ", ".join(f'"{g}"' for g in guards)
    claim_path = tmp_path / "claim.bth.toml"
    claim_path.write_text(f"""[claim]
headline = "Test"
kill_condition = "test"

[[hypotheses]]
id = "H_primary"
label = "Primary"

[[hypotheses]]
id = "H_null"
label = "Null"

[[confounds]]
id = "C_1"
label = "Pipeline soundness"
[confounds.synthetic_recovery]
gate_name = "{gate_name}"
guards = [{guards_toml}]
""")
    return claim_path


class TestSyntheticRecoveryConfoundCheck:
    def test_uncontrolled_when_never_stamped(self, tmp_path):
        claim_path = _write_claim_with_synth(tmp_path)
        claim = parse_claim(claim_path)
        result = synthetic_recovery_confound_check(claim, tmp_path)
        assert result["confounds"][0]["status"] == "uncontrolled"
        assert result["confounds"][0]["gate_state"] == "UNKNOWN"

    def test_controlled_when_green(self, git_repo):
        tmp_path, guarded, sha = git_repo
        claim_path = _write_claim_with_synth(tmp_path)
        claim = parse_claim(claim_path)
        stamp_gate(tmp_path, "g", "pass", sha)
        result = synthetic_recovery_confound_check(claim, tmp_path)
        assert result["confounds"][0]["status"] == "controlled"
        assert result["confounds"][0]["gate_state"] == "GREEN"

    def test_uncontrolled_when_stale(self, git_repo):
        tmp_path, guarded, sha = git_repo
        claim_path = _write_claim_with_synth(tmp_path)
        claim = parse_claim(claim_path)
        stamp_gate(tmp_path, "g", "pass", sha)
        guarded.write_text("def f(): return 99\n")
        result = synthetic_recovery_confound_check(claim, tmp_path)
        assert result["confounds"][0]["status"] == "uncontrolled"
        assert result["confounds"][0]["gate_state"] == "STALE"

    def test_no_synthetic_recovery_block_skipped(self, tmp_path):
        claim_path = tmp_path / "claim.bth.toml"
        claim_path.write_text("""[claim]
headline = "Test"
kill_condition = "test"

[[hypotheses]]
id = "H_primary"
label = "Primary"

[[hypotheses]]
id = "H_null"
label = "Null"

[[confounds]]
id = "C_1"
label = "No synthetic_recovery here"
""")
        claim = parse_claim(claim_path)
        result = synthetic_recovery_confound_check(claim, tmp_path)
        assert result["confounds"] == []
