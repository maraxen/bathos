"""myxcel ``cold`` capability probe and typed client (spec 261023_output-tiering sections 6.1-6.2, step 5b).

No real myxcel or ssh is ever run: every call goes through a fake runner that records its argv.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from bathos.cluster import MyxcelCapabilityGapError
from bathos.output_tiers import myxcel as mx
from bathos.output_tiers.config import OutputTiersConfigError

SHA = "ab" * 32
HELP_OK = "Usage: myxcel cold put PROFILE LOCAL REMOTE [--json]\n  --json  machine output\n"


class FakeRunner:
    """Records argv/timeout; replies from a queue (a RunResult, or an exception to raise)."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls: list[tuple[list[str], float]] = []

    def __call__(self, argv, timeout):
        self.calls.append((list(argv), timeout))
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, BaseException):
            raise reply
        return reply


def ok(payload, code=0):
    return mx.RunResult(code, json.dumps(payload), "")


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    mx.reset_capability_cache()
    yield
    mx.reset_capability_cache()


# ------------------------------------------------------------------------------------------- probe


def test_probe_passes_on_new_myxcel_and_uses_the_help_form():
    r = FakeRunner(mx.RunResult(0, HELP_OK, ""))
    mx.probe_cold_capability(runner=r)
    assert r.calls[0][0] == ["myxcel", "cold", "put", "--help"]


def test_old_myxcel_nonzero_exit_is_a_capability_gap():
    # cyclopts-era myxcel that predates `cold`: unused tokens, exit 2.
    r = FakeRunner(mx.RunResult(2, "", "Unused Tokens: ['cold', 'put']"))
    with pytest.raises(MyxcelCapabilityGapError) as e:
        mx.probe_cold_capability(runner=r)
    assert e.value.error_code == "myxcel_capability_gap"


def test_old_myxcel_that_exits_zero_without_json_is_a_capability_gap():
    r = FakeRunner(mx.RunResult(0, "Usage: myxcel [OPTIONS] COMMAND\n", ""))
    with pytest.raises(MyxcelCapabilityGapError) as e:
        mx.probe_cold_capability(runner=r)
    assert "--json" in str(e.value)


def test_missing_binary_is_a_capability_gap():
    r = FakeRunner(FileNotFoundError(2, "No such file or directory: 'myxcel'"))
    with pytest.raises(MyxcelCapabilityGapError) as e:
        mx.probe_cold_capability(runner=r)
    assert e.value.error_code == "myxcel_capability_gap"
    assert "not installed" in str(e.value)


def test_probe_timeout_is_a_capability_gap():
    r = FakeRunner(subprocess.TimeoutExpired(["myxcel"], 30))
    with pytest.raises(MyxcelCapabilityGapError):
        mx.probe_cold_capability(runner=r)


def test_probe_success_is_cached_failure_is_not():
    bad = FakeRunner(mx.RunResult(2, "", "nope"))
    with pytest.raises(MyxcelCapabilityGapError):
        mx.probe_cold_capability(runner=bad)
    with pytest.raises(MyxcelCapabilityGapError):  # a failure is retried, not remembered
        mx.probe_cold_capability(runner=bad)
    assert len(bad.calls) == 2

    good = FakeRunner(mx.RunResult(0, HELP_OK, ""))
    mx.probe_cold_capability(runner=good)
    mx.probe_cold_capability(runner=good)
    assert len(good.calls) == 1  # second call served from the cache
    mx.reset_capability_cache()
    mx.probe_cold_capability(runner=good)
    assert len(good.calls) == 2


def test_capability_gap_error_is_the_cluster_one():
    assert mx.MyxcelCapabilityGapError is MyxcelCapabilityGapError
    assert issubclass(MyxcelCapabilityGapError, RuntimeError)


# ------------------------------------------------------------------------------------------- argv


def test_put_argv_and_ok_mapping():
    r = FakeRunner(
        ok(
            {
                "status": "ok",
                "remote_path": "bathos-cold/p/r/a.partial-1",
                "bytes": 7,
                "message": "m",
            }
        )
    )
    res = mx.put("so3", "/tmp/a.tar.zst", "bathos-cold/p/r/a.partial-1", runner=r, timeout=11)
    assert r.calls == [
        (
            [
                "myxcel",
                "cold",
                "put",
                "so3",
                "/tmp/a.tar.zst",
                "bathos-cold/p/r/a.partial-1",
                "--json",
            ],
            11,
        )
    ]
    assert res == mx.PutResult("ok", "bathos-cold/p/r/a.partial-1", 7, "m")


def test_put_error_status_is_error():
    r = FakeRunner(ok({"status": "error", "message": "disk full"}, code=1))
    res = mx.put("so3", "/tmp/a", "x/a", runner=r)
    assert res.status == "error" and res.message == "disk full"


def test_commit_argv_and_ok():
    r = FakeRunner(ok({"status": "ok"}))
    res = mx.commit("so3", "d/run.tar.zst.partial-1", "d/run.tar.zst", runner=r)
    assert r.calls[0][0] == [
        "myxcel",
        "cold",
        "commit",
        "so3",
        "d/run.tar.zst.partial-1",
        "d/run.tar.zst",
        "--json",
    ]
    assert res.status == "ok"


def test_commit_exists_surfaces_as_exists_never_ok_even_on_exit_zero():
    for code in (0, 1):
        res = mx.commit("so3", "d/p", "d/f", runner=FakeRunner(ok({"status": "exists"}, code=code)))
        assert res.status == "exists"
        assert res.status != "ok"


def test_verify_file_argv():
    r = FakeRunner(
        ok(
            {
                "status": "verified",
                "sha256_observed": SHA,
                "size_observed": 10,
                "method": "login_ssh",
            }
        )
    )
    mx.verify_file("titanix", "d/m.json", SHA, 10, runner=r)
    assert r.calls[0][0] == [
        "myxcel",
        "cold",
        "verify-file",
        "titanix",
        "d/m.json",
        SHA,
        "10",
        "--json",
    ]


def test_get_argv_and_mapping():
    r = FakeRunner(ok({"status": "ok", "local_path": "/tmp/w/m.json", "bytes": 5}))
    res = mx.get("titanix", "d/m.json", "/tmp/w", runner=r)
    assert r.calls[0][0] == ["myxcel", "cold", "get", "titanix", "d/m.json", "/tmp/w", "--json"]
    assert res == mx.GetResult("ok", "/tmp/w/m.json", 5, "")


def test_lock_and_unlock_argv():
    r = FakeRunner(ok({"status": "acquired", "holder": None}))
    assert mx.lock("so3", "bathos-cold/p/r", runner=r).status == "acquired"
    assert r.calls[0][0] == ["myxcel", "cold", "lock", "so3", "bathos-cold/p/r", "--json"]
    r2 = FakeRunner(ok({"status": "released"}))
    assert mx.unlock("so3", "bathos-cold/p/r", runner=r2).status == "released"
    assert r2.calls[0][0] == ["myxcel", "cold", "unlock", "so3", "bathos-cold/p/r", "--json"]


def test_default_runner_uses_list_argv_without_shell(monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen.update(argv=argv, **kw)
        return subprocess.CompletedProcess(argv, 0, '{"status":"ok"}', "")

    monkeypatch.setattr(mx.subprocess, "run", fake_run)
    assert mx.commit("so3", "a/b", "a/c").status == "ok"
    assert isinstance(seen["argv"], list)
    assert not seen.get("shell")
    assert seen["timeout"] == mx.COMMIT_TIMEOUT


# ------------------------------------------------------------------------------------------- local refusals

BAD_PATHS = [
    "/abs/path",
    "",
    "   ",
    "a/../b",
    "..",
    "a/..",
    "-rf",
    "a\nb",
    "a\rb",
    "~/x",
    "~",
    "a//b",
    "a/./b",
    ".",
    "a b/c",
    "bathos-cold/my project/r",
    "bathos-cold/-x/r",
    "a/-b",
    "bathos-cold/naïve/r",
    "a;b",
    "a$b",
    "a`b`",
]


@pytest.mark.parametrize("bad", BAD_PATHS)
def test_bad_remote_paths_are_refused_without_running(bad):
    r = FakeRunner(ok({"status": "ok"}))
    calls = [
        lambda: mx.put("so3", "/tmp/a", bad, runner=r),
        lambda: mx.commit("so3", bad, "d/f", runner=r),
        lambda: mx.commit("so3", "d/p", bad, runner=r),
        lambda: mx.verify_file("so3", bad, SHA, 1, runner=r),
        lambda: mx.get("so3", bad, "/tmp/w", runner=r),
        lambda: mx.lock("so3", bad, runner=r),
        lambda: mx.unlock("so3", bad, runner=r),
    ]
    for call in calls:
        with pytest.raises(OutputTiersConfigError) as e:
            call()
        assert e.value.error_code == "invalid_param"
    assert r.calls == []


def test_bad_profile_sha_and_size_are_refused_without_running():
    r = FakeRunner(ok({"status": "ok"}))
    with pytest.raises(OutputTiersConfigError):
        mx.lock("--evil", "d", runner=r)
    with pytest.raises(OutputTiersConfigError):
        mx.verify_file("so3", "d/f", "notasha", 1, runner=r)
    with pytest.raises(OutputTiersConfigError):
        mx.verify_file("so3", "d/f", SHA, -1, runner=r)
    assert r.calls == []


def test_the_allowed_charset_passes_and_a_trailing_slash_is_for_directories_only():
    r = FakeRunner(ok({"status": "acquired"}))
    assert mx.check_remote_path("bathos-cold/my-proj_1.2+x@y=z,w%/run-1") == (
        "bathos-cold/my-proj_1.2+x@y=z,w%/run-1"
    )
    assert mx.lock("so3", "bathos-cold/p/r/", runner=r).status == "acquired"  # as in myxcel
    for call in (
        lambda: mx.check_remote_path("bathos-cold/p/r/"),
        lambda: mx.put("so3", "/tmp/a", "d/", runner=r),
        lambda: mx.get("so3", "d/", "/tmp/w", runner=r),
        lambda: mx.lock("so3", "/", runner=r),
    ):
        with pytest.raises(OutputTiersConfigError):
            call()
    assert len(r.calls) == 1


def test_dotdot_inside_a_name_is_not_a_traversal():
    r = FakeRunner(ok({"status": "acquired"}))
    assert mx.lock("so3", "bathos-cold/p/run..1", runner=r).status == "acquired"


# ------------------------------------------------------------------------------------------- verify-file states


def test_verify_garbage_stdout_is_unverifiable():
    res = mx.verify_file(
        "so3", "d/f", SHA, 1, runner=FakeRunner(mx.RunResult(0, "Traceback ...", "boom"))
    )
    assert res.status == "unverifiable" and res.message


def test_verify_nonzero_exit_without_json_is_unverifiable():
    res = mx.verify_file(
        "so3", "d/f", SHA, 1, runner=FakeRunner(mx.RunResult(255, "", "ssh: no route"))
    )
    assert res.status == "unverifiable"


def test_verify_timeout_is_unverifiable():
    res = mx.verify_file(
        "so3", "d/f", SHA, 1, runner=FakeRunner(subprocess.TimeoutExpired(["myxcel"], 5))
    )
    assert res.status == "unverifiable" and "timed out" in res.message


def test_verify_missing_binary_is_unverifiable():
    res = mx.verify_file("so3", "d/f", SHA, 1, runner=FakeRunner(FileNotFoundError("myxcel")))
    assert res.status == "unverifiable"


def test_verify_failed_is_failed_with_observed_values_and_exit_code_irrelevant():
    payload = {
        "status": "failed",
        "sha256_observed": "cd" * 32,
        "size_observed": 9,
        "method": "slurm_job",
        "message": "x",
    }
    for code in (0, 1):
        res = mx.verify_file("so3", "d/f", SHA, 10, runner=FakeRunner(ok(payload, code=code)))
        assert res == mx.VerifyResult("failed", "cd" * 32, 9, "slurm_job", "x")


def test_verify_unknown_status_is_unverifiable():
    res = mx.verify_file("so3", "d/f", SHA, 1, runner=FakeRunner(ok({"status": "maybe"})))
    assert res.status == "unverifiable"
    res = mx.verify_file("so3", "d/f", SHA, 1, runner=FakeRunner(ok({"status": True})))
    assert res.status == "unverifiable"


def test_verify_verified_requires_matching_observed_values_and_exit_zero():
    good = {
        "status": "verified",
        "sha256_observed": SHA,
        "size_observed": 10,
        "method": "login_ssh",
    }
    res = mx.verify_file("so3", "d/f", SHA, 10, runner=FakeRunner(ok(good)))
    assert res.status == "verified" and res.method == "login_ssh"
    # verified, but for different bytes / size / nothing observed / non-zero exit: never verified
    for patch in (
        {"sha256_observed": "cd" * 32},
        {"size_observed": 11},
        {"sha256_observed": None},
        {"size_observed": None},
    ):
        res = mx.verify_file("so3", "d/f", SHA, 10, runner=FakeRunner(ok({**good, **patch})))
        assert res.status == "unverifiable", patch
    assert (
        mx.verify_file("so3", "d/f", SHA, 10, runner=FakeRunner(ok(good, code=1))).status
        == "unverifiable"
    )


def test_verify_unverifiable_passes_through_and_is_never_failed():
    res = mx.verify_file(
        "so3",
        "d/f",
        SHA,
        1,
        runner=FakeRunner(ok({"status": "unverifiable", "message": "host down"}, 1)),
    )
    assert res.status == "unverifiable" and res.message == "host down"


# ------------------------------------------------------------------------------------------- never success


def test_non_json_timeout_unknown_status_are_typed_errors_for_every_other_command():
    cases = {
        "put": lambda r: mx.put("so3", "/tmp/a", "d/a", runner=r),
        "commit": lambda r: mx.commit("so3", "d/p", "d/f", runner=r),
        "get": lambda r: mx.get("so3", "d/f", "/tmp/w", runner=r),
        "lock": lambda r: mx.lock("so3", "d", runner=r),
        "unlock": lambda r: mx.unlock("so3", "d", runner=r),
    }
    bad_replies = [
        mx.RunResult(0, "not json", ""),
        mx.RunResult(1, "", "ssh failed"),
        mx.RunResult(0, "[1, 2]", ""),
        subprocess.TimeoutExpired(["myxcel"], 1),
        FileNotFoundError("myxcel"),
        ok({"status": "weird"}),
        ok({}),
    ]
    for name, call in cases.items():
        for reply in bad_replies:
            res = call(FakeRunner(reply))
            assert res.status == "error", (name, reply)
            assert res.message, (name, reply)


def test_success_status_on_nonzero_exit_is_not_success():
    assert (
        mx.put("so3", "/tmp/a", "d/a", runner=FakeRunner(ok({"status": "ok"}, 1))).status == "error"
    )
    assert (
        mx.commit("so3", "d/p", "d/f", runner=FakeRunner(ok({"status": "ok"}, 1))).status == "error"
    )
    assert (
        mx.get(
            "so3", "d/f", "/w", runner=FakeRunner(ok({"status": "ok", "local_path": "/w/f"}, 1))
        ).status
        == "error"
    )
    assert mx.lock("so3", "d", runner=FakeRunner(ok({"status": "acquired"}, 1))).status == "error"


def test_get_ok_without_local_path_is_error():
    assert mx.get("so3", "d/f", "/w", runner=FakeRunner(ok({"status": "ok"}))).status == "error"


# ------------------------------------------------------------------------------------------- lock


def test_lock_held_returns_the_holder():
    holder = {"host": "box", "pid": 42, "attempt": "ab12cd34", "started": "2026-10-23T10:00:00Z"}
    res = mx.lock(
        "so3",
        "bathos-cold/p/r",
        runner=FakeRunner(ok({"status": "held", "holder": holder}, code=1)),
    )
    assert res.status == "held"
    assert res.holder == holder


def test_unlock_held_returns_holder_and_acquired_is_not_accepted():
    res = mx.unlock("so3", "d", runner=FakeRunner(ok({"status": "held", "holder": {"pid": 1}})))
    assert res.status == "held" and res.holder == {"pid": 1}
    assert mx.unlock("so3", "d", runner=FakeRunner(ok({"status": "acquired"}))).status == "error"


def test_probe_nonzero_exit_is_a_gap_even_if_the_output_mentions_json():
    r = FakeRunner(mx.RunResult(2, "Usage: myxcel [--json] COMMAND\n", "Unknown command 'cold'"))
    with pytest.raises(MyxcelCapabilityGapError) as e:
        mx.probe_cold_capability(runner=r)
    assert "exited 2" in str(e.value)
