"""Output tiering step 6: ``bth cold``, the ``output_copies`` index table and ``bth outputs where``
(spec 261023_output-tiering.md sections 4, 5.3, 6.1, 6.4, 7 and 9 row 6).

Built on a real log-mode catalog (events are run-log segments, folded by ``run_ingest``) and a fake ``myxcel cold``
runner that simulates a remote filesystem in a temp directory (``tests/_fake_myxcel.py``): no real myxcel or ssh,
and nothing here deletes a final remote file. Crash windows are real: the fake "kills the process" at a chosen
step. Each test names the broken implementation it guards against.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import shutil
import threading
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from bathos.cli_cyclopts import app
from bathos.index import connect_read, index_db_path
from bathos.mcp import cold_outputs_tool, mcp_cold_outputs_tool, outputs_where_tool
from bathos.output_tiers import cold as cold_module
from bathos.output_tiers import myxcel as mx
from bathos.output_tiers.cold import _get_bytes, cold_outputs, where_outputs
from bathos.output_tiers.config import OutputTiersConfigError
from bathos.output_tiers.reference import OutputTierError
from bathos.runlog.envelope import build_envelope
from bathos.runlog.mode import cutover_marker_path
from tests._cyclopts_runner import CyclopticRunner
from tests._fake_myxcel import FakeMyxcel, KillSwitch
from tests.test_output_tiers_adopt import RID, T1, T2, World

RID2 = "22222222-bbbb-4bbb-8bbb-222222222222"
RID3 = "33333333-cccc-4ccc-8ccc-333333333333"
COLD = "coldprof"
MIRROR = "mirrorprof"
COLD_ROOT = "/cold"


# --------------------------------------------------------------------------------------------------
# fixtures


class ColdWorld:
    def __init__(self, w: World, fake: FakeMyxcel, cfg: Path) -> None:
        self.w = w
        self.fake = fake
        self.cfg = cfg

    def standard_run(self, rid: str = RID, files: dict[str, bytes] | None = None) -> dict[str, str]:
        """A finished standard run (declared outputs, run-end hashes). Call ``ingest`` once afterwards."""
        files = files or {
            f"out/{rid[:4]}/a.json": b"alpha-" + rid[:4].encode(),
            f"out/{rid[:4]}/cells/c1.npy": b"cell-1-" + rid[:4].encode(),
        }
        shas = {rel: self.w.write(rel, data) for rel, data in files.items()}
        self.w.started(rid, declared=True)
        self.w.run_end([self.w.entry(rel, phase="run_end") for rel in files], run_id=rid)
        self.w.finished(rid)
        return shas

    def ingest(self) -> None:
        self.w.ingest()

    def cold(self, prefix: str = RID[:8], **kw):
        kw.setdefault("runner", self.fake)
        return cold_outputs(self.w.catalog, prefix, **kw)

    def where(self, rid: str = RID) -> dict:
        return outputs_where_tool(run_id=rid, catalog_dir=str(self.w.catalog))

    def dest(self, rid: str = RID, slug: str = "repo") -> str:
        return f"bathos-cold/{slug}/{rid}"

    def final_names(self, rid: str = RID) -> tuple[str, str]:
        return f"run-{rid}.manifest.json", f"run-{rid}.tar.zst"

    def copy_events(self, kind: str | None = None) -> list[dict]:
        con = connect_read(self.w.catalog)
        try:
            rows = con.execute(
                "SELECT eid, kind, entity, ts, data FROM events WHERE kind LIKE 'output_copy.%' ORDER BY ts, eid"
            ).fetchall()
        finally:
            con.close()
        out = [
            {"eid": e, "kind": k, "entity": json.loads(en), "ts": ts, "data": json.loads(d)}
            for e, k, en, ts, d in rows
        ]
        return [e for e in out if kind is None or e["kind"] == kind]


@pytest.fixture(autouse=True)
def _fresh_probe_cache():
    mx.reset_capability_cache()
    yield
    mx.reset_capability_cache()


@pytest.fixture
def cw(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ColdWorld:
    w = World(tmp_path)
    monkeypatch.chdir(w.elsewhere)
    for var in ("SLURM_JOB_ID", "BTH_NO_INGEST", "BTH_COLD_PROFILE", "BTH_MIRROR_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    monkeypatch.setenv("BTH_COLD_PROFILE", COLD)
    cfg = tmp_path / "myxcel-config"
    cfg.mkdir()
    for name, host in ((COLD, "coldhost"), (MIRROR, "mirrorhost")):
        (cfg / f"{name}.toml").write_text(f'host = "{host}"\ncold_root = "{COLD_ROOT}"\n')
    monkeypatch.setattr("bathos.output_tiers.config.myxcel_config_dir", lambda: cfg)
    fake = FakeMyxcel(tmp_path / "remote")
    monkeypatch.setattr(mx, "default_runner", fake)  # the CLI/MCP paths take no runner argument
    return ColdWorld(w, fake, cfg)


def _code(excinfo) -> str:
    return excinfo.value.error_code


# --------------------------------------------------------------------------------------------------
# the write, end to end


def test_cold_writes_the_pair_verifies_it_and_records_one_verified_copy(cw: ColdWorld):
    """The pair lands under the run's destination; the recorded row carries the profile's host, the layer that
    chose it and the manifest's hashes. A write that skips the post-commit verify, or records nothing, fails."""
    shas = cw.standard_run()
    cw.ingest()

    result = cw.cold()

    assert result["ok"] is True
    (leg,) = result["legs"]
    assert (leg["tier"], leg["state"], leg["uploaded"], leg["resumed"]) == (
        "cold",
        "verified",
        True,
        False,
    )
    m_name, a_name = cw.final_names()
    finals = cw.fake.finals(COLD)
    assert set(finals) == {f"{cw.dest()}/{m_name}", f"{cw.dest()}/{a_name}"}
    manifest = json.loads(cw.fake.remote(COLD, f"{cw.dest()}/{m_name}").read_text())
    assert {f["relpath"]: f["sha256"] for f in manifest["files"]} == shas
    assert manifest["archive"]["sha256"] == finals[f"{cw.dest()}/{a_name}"]
    assert not cw.fake.lock_exists(COLD, cw.dest())  # released
    assert not [
        k for k in cw.fake.snapshot(COLD) if ".partial-" in k
    ]  # commit linked the partials away

    where = cw.where()
    assert where["n_copies"] == 1 and where["n_verified"] == 1
    (row,) = where["copies"]
    assert row["tier"] == "cold" and row["profile"] == COLD and row["host"] == "coldhost"
    assert row["state"] == "verified" and row["superseded_at"] is None
    assert row["config_layer"] == "$BTH_COLD_PROFILE"
    assert row["archive_sha256"] == manifest["archive"]["sha256"]
    assert row["path"] == f"{COLD_ROOT}/{cw.dest()}/{a_name}"


def test_cold_writes_cold_then_mirror_each_with_its_own_layer(cw: ColdWorld):
    cw.standard_run()
    cw.ingest()

    result = cw.cold(mirror=MIRROR)

    assert [(x["tier"], x["state"]) for x in result["legs"]] == [
        ("cold", "verified"),
        ("mirror", "verified"),
    ]
    assert cw.fake.finals(COLD) and cw.fake.finals(MIRROR)
    rows = {r["tier"]: r for r in cw.where()["copies"]}
    assert rows["mirror"]["host"] == "mirrorhost"
    assert rows["mirror"]["config_layer"] == "explicit --mirror"
    assert rows["cold"]["config_layer"] == "$BTH_COLD_PROFILE"


def test_a_failing_mirror_keeps_the_verified_cold_copy_and_says_so(cw: ColdWorld):
    cw.standard_run()
    cw.ingest()
    cw.fake.hold_lock(MIRROR, cw.dest())  # someone else is writing the mirror

    with pytest.raises(OutputTierError) as e:
        cw.cold(mirror=MIRROR)

    assert _code(e) == "lock_held" and "cold leg is already verified" in e.value.detail
    assert [r["tier"] for r in cw.where()["copies"]] == ["cold"]


# --------------------------------------------------------------------------------------------------
# refusals before anything touches the remote


def test_no_cold_profile_resolves_refuses_and_names_the_layers_checked(
    cw: ColdWorld, monkeypatch: pytest.MonkeyPatch
):
    cw.standard_run()
    cw.ingest()
    monkeypatch.delenv("BTH_COLD_PROFILE")

    with pytest.raises(OutputTierError) as e:
        cw.cold()

    assert _code(e) == "no_cold_root"
    for layer in ("--profile", "$BTH_COLD_PROFILE", "[tool.bathos.outputs]", "config.toml"):
        assert layer in e.value.detail
    assert cw.fake.calls == []


def test_a_profile_without_cold_root_refuses_no_cold_root(cw: ColdWorld):
    cw.standard_run()
    cw.ingest()
    (cw.cfg / "bare.toml").write_text('host = "h"\n')

    with pytest.raises(Exception) as e:
        cw.cold(profile="bare")

    assert e.value.error_code == "no_cold_root"  # type: ignore[attr-defined]
    assert cw.fake.calls == []


def test_a_drifted_live_file_is_outputs_drifted_and_nothing_reaches_the_remote(cw: ColdWorld):
    """The drift is found by the local build, before any lock or put: a broken implementation that locks or
    uploads first leaves calls (or a lock) behind."""
    cw.standard_run()
    cw.ingest()
    cw.w.write(f"out/{RID[:4]}/a.json", b"tampered")

    with pytest.raises(OutputTierError) as e:
        cw.cold()

    assert _code(e) == "outputs_drifted" and f"out/{RID[:4]}/a.json" in e.value.detail
    assert cw.fake.mutating_calls() == []
    assert cw.fake.snapshot(COLD) == {}
    assert cw.copy_events() == []


def test_a_missing_live_file_is_outputs_drifted(cw: ColdWorld):
    cw.standard_run()
    cw.ingest()
    (cw.w.root / f"out/{RID[:4]}/cells/c1.npy").unlink()

    with pytest.raises(OutputTierError) as e:
        cw.cold()

    assert _code(e) == "outputs_drifted"
    assert cw.fake.mutating_calls() == []


def test_a_run_with_no_reference_set_refuses(cw: ColdWorld):
    cw.w.started(RID)
    cw.w.finished(RID)
    cw.ingest()
    with pytest.raises(OutputTierError) as e:
        cw.cold()
    assert _code(e) == "no_reference_hashes"


def test_a_running_run_refuses_run_not_terminal(cw: ColdWorld):
    cw.w.write(f"out/{RID[:4]}/a.json", b"alpha")
    cw.w.started(RID, declared=True)
    cw.w.run_end([cw.w.entry(f"out/{RID[:4]}/a.json", phase="run_end")])
    cw.ingest()
    with pytest.raises(OutputTierError) as e:
        cw.cold()
    assert _code(e) == "run_not_terminal"


def test_a_legacy_catalog_refuses_with_requires_log_mode(cw: ColdWorld):
    cw.standard_run()
    cw.ingest()
    cutover_marker_path(cw.w.catalog).unlink()
    with pytest.raises(OutputTierError) as e:
        cw.cold()
    assert _code(e) == "requires_log_mode"
    assert cw.fake.calls == []


def test_an_imported_run_without_worktree_root_is_worktree_unknown_and_nothing_reaches_the_remote(
    cw: ColdWorld,
):
    cw.w.write("out/a.json", b"alpha")
    cw.w.imported([cw.w.entry("out/a.json")])
    cw.ingest()

    with pytest.raises(OutputTierError) as e:
        cw.cold()
    assert _code(e) == "worktree_unknown"
    assert cw.fake.calls == []

    ok = cw.cold(worktree_root=str(cw.w.repo))  # with the root it works, and the event carries it
    assert ok["legs"][0]["state"] == "verified"
    (rec,) = cw.copy_events("output_copy.recorded")
    assert rec["data"]["state"] == "verified"


def test_supersede_needs_a_reason_and_one_target_is_required(cw: ColdWorld):
    cw.standard_run()
    cw.ingest()
    with pytest.raises(OutputTierError) as e:
        cw.cold(supersede=True)
    assert _code(e) == "invalid_param"
    with pytest.raises(OutputTierError):
        cold_outputs(cw.w.catalog, "", runner=cw.fake)
    with pytest.raises(OutputTierError):
        cold_outputs(cw.w.catalog, RID[:8], all_uncopied=True, runner=cw.fake)


def test_an_old_myxcel_is_a_capability_gap_before_any_build_or_lock(cw: ColdWorld):
    cw.standard_run()
    cw.ingest()

    class Old:
        calls: list = []

        def __call__(self, argv, _timeout):
            self.calls.append(argv)
            return mx.RunResult(2, "", "Unused Tokens: ['cold']")

    old = Old()
    with pytest.raises(Exception) as e:
        cw.cold(runner=old)
    assert e.value.error_code == "myxcel_capability_gap"  # type: ignore[attr-defined]
    assert old.calls == [["myxcel", "cold", "put", "--help"]]


# --------------------------------------------------------------------------------------------------
# dry run


def test_dry_run_writes_no_event_and_runs_no_put_commit_or_lock(cw: ColdWorld):
    cw.standard_run()
    cw.ingest()
    before = cw.w.log_bytes()

    result = cw.cold(dry_run=True, mirror=MIRROR)

    assert result["dry_run"] is True and result["n_files"] == 2
    assert result["total_bytes"] == sum(
        len(d) for d in (b"alpha-" + RID[:4].encode(), b"cell-1-" + RID[:4].encode())
    )
    assert sorted(result["files"]) == [f"out/{RID[:4]}/a.json", f"out/{RID[:4]}/cells/c1.npy"]
    assert [d["leg"] for d in result["destinations"]] == ["cold", "mirror"]
    assert result["destinations"][0]["path"] == f"{COLD_ROOT}/{cw.dest()}"
    assert cw.w.log_bytes() == before
    assert cw.fake.calls == []  # not even the probe


def test_dry_run_still_refuses_a_drifted_file(cw: ColdWorld):
    cw.standard_run()
    cw.ingest()
    cw.w.write(f"out/{RID[:4]}/a.json", b"tampered")
    with pytest.raises(OutputTierError) as e:
        cw.cold(dry_run=True)
    assert _code(e) == "outputs_drifted"


# --------------------------------------------------------------------------------------------------
# never overwritten


def test_a_final_with_different_content_is_archive_conflict_and_is_never_overwritten(cw: ColdWorld):
    cw.standard_run()
    cw.ingest()
    m_name, a_name = cw.final_names()
    for name, content in ((m_name, b'{"not": "ours"}'), (a_name, b"other bytes")):
        p = cw.fake.remote(COLD, f"{cw.dest()}/{name}")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)
    before = cw.fake.finals(COLD)

    with pytest.raises(OutputTierError) as e:
        cw.cold()

    assert _code(e) == "archive_conflict"
    assert cw.fake.finals(COLD) == before
    assert not cw.fake.lock_exists(COLD, cw.dest())  # every exit releases
    assert cw.copy_events() == []
    assert cw.fake.calls_named("put") == [] and cw.fake.calls_named("commit") == []


def test_a_matching_manifest_whose_archive_differs_is_archive_conflict(cw: ColdWorld):
    cw.standard_run()
    cw.ingest()
    cw.cold()
    m_name, a_name = cw.final_names()
    arch = cw.fake.remote(COLD, f"{cw.dest()}/{a_name}")
    arch.write_bytes(arch.read_bytes()[:-1] + b"\x00")
    before = cw.fake.finals(COLD)

    with pytest.raises(OutputTierError) as e:
        cw.cold()

    assert _code(e) == "archive_conflict" and cw.fake.finals(COLD) == before


def test_a_second_run_of_the_same_run_records_without_uploading(cw: ColdWorld):
    """Resume check: both finals present and consistent with the locally built reference set => record only."""
    cw.standard_run()
    cw.ingest()
    cw.cold()
    n_put = len(cw.fake.calls_named("put"))

    again = cw.cold()

    assert again["legs"][0]["resumed"] is True and again["legs"][0]["uploaded"] is False
    assert len(cw.fake.calls_named("put")) == n_put
    assert len(cw.fake.calls_named("commit")) == 2  # only the first run's pair
    assert len(cw.copy_events("output_copy.recorded")) == 2
    rows = cw.where()["copies"]
    assert len(rows) == 1 and rows[0]["state"] == "verified"  # same copy_id: one row


def test_a_partial_that_does_not_verify_is_never_committed(cw: ColdWorld):
    """Step 5: verify the partials BEFORE committing. An implementation that commits first leaves a corrupt final."""
    cw.standard_run()
    cw.ingest()

    def corrupt_archive_after_put(args, base):
        target = base / args[1]
        if ".tar.zst.partial-" in args[1]:
            target.write_bytes(target.read_bytes()[:-1] + b"\x00")

    cw.fake.after("put", corrupt_archive_after_put)

    with pytest.raises(OutputTierError) as e:
        cw.cold()

    assert _code(e) == "copy_verify_failed" and ".partial-" in e.value.detail
    assert (
        cw.fake.finals(COLD)
        == {k: v for k, v in cw.fake.snapshot(COLD).items() if ".partial-" not in k}
        == {}
    )
    assert cw.fake.calls_named("commit") == []
    assert not cw.fake.lock_exists(COLD, cw.dest())
    assert cw.copy_events() == []


def test_a_failed_upload_is_myxcel_error_releases_the_lock_and_commits_nothing(cw: ColdWorld):
    cw.standard_run()
    cw.ingest()
    cw.fake.fail("put", "quota exceeded")

    with pytest.raises(OutputTierError) as e:
        cw.cold()

    assert _code(e) == "myxcel_error" and "quota exceeded" in e.value.detail
    assert not cw.fake.lock_exists(COLD, cw.dest()) and cw.fake.finals(COLD) == {}
    assert cw.fake.calls_named("commit") == [] and cw.copy_events() == []


def test_an_unreachable_host_while_verifying_the_partials_is_copy_unverifiable_never_a_commit(
    cw: ColdWorld,
):
    """unverifiable never turns into verified: nothing is committed on a read that could not happen."""
    cw.standard_run()
    cw.ingest()
    cw.fake.fail("verify-file", "ssh: connection refused", status="unverifiable")

    with pytest.raises(OutputTierError) as e:
        cw.cold()

    assert _code(e) == "copy_unverifiable"
    assert cw.fake.calls_named("commit") == [] and cw.fake.finals(COLD) == {}
    assert not cw.fake.lock_exists(COLD, cw.dest())


def test_a_final_that_fails_its_post_commit_verify_is_recorded_as_failed_and_exits_with_an_error(
    cw: ColdWorld,
):
    """The pair IS committed, so it is recorded with what was measured (never as verified)."""
    cw.standard_run()
    cw.ingest()
    seen_commits = []

    def after_commit(args, base):
        seen_commits.append(args[1])
        if len(seen_commits) == 2:  # the archive just reached its final name: damage it
            p = base / args[1]
            p.write_bytes(p.read_bytes()[:-1] + b"\x00")  # damage in place; nothing is deleted

    cw.fake.after("commit", after_commit)

    with pytest.raises(OutputTierError) as e:
        cw.cold()

    assert _code(e) == "copy_verify_failed"
    (row,) = cw.where()["copies"]
    assert row["state"] == "failed" and row["verified_at"] is None
    assert cw.where()["n_verified"] == 0


# --------------------------------------------------------------------------------------------------
# crash windows


def test_kill_after_the_manifest_commit_plain_rerun_is_lock_held_break_lock_rerun_is_half_committed(
    cw: ColdWorld,
):
    cw.standard_run()
    cw.ingest()
    m_name, a_name = cw.final_names()
    cw.fake.kill_after("commit", 1)

    with pytest.raises(KillSwitch):
        cw.cold()

    # Manifest first, archive last: only the manifest final exists, the dead writer's lock is still held.
    assert set(cw.fake.finals(COLD)) == {f"{cw.dest()}/{m_name}"}
    assert cw.fake.lock_exists(COLD, cw.dest())
    cw.fake.revive()

    with pytest.raises(OutputTierError) as plain:
        cw.cold()
    assert (
        _code(plain) == "lock_held" and "fake-host" in plain.value.detail
    )  # names the dead holder

    with pytest.raises(OutputTierError) as broken:
        cw.cold(break_lock=True)
    assert _code(broken) == "archive_conflict" and "half_committed" in broken.value.detail
    assert not cw.fake.lock_exists(COLD, cw.dest())  # every exit path releases
    assert set(cw.fake.finals(COLD)) == {
        f"{cw.dest()}/{m_name}"
    }  # nothing overwritten, nothing added
    assert cw.copy_events() == []


def test_kill_after_the_archive_commit_before_the_event_break_lock_rerun_records_without_uploading(
    cw: ColdWorld,
):
    cw.standard_run()
    cw.ingest()
    cw.fake.kill_after("commit", 2)

    with pytest.raises(KillSwitch):
        cw.cold()

    assert len(cw.fake.finals(COLD)) == 2 and cw.fake.lock_exists(COLD, cw.dest())
    assert cw.copy_events() == []  # the event was never written
    cw.fake.revive()
    puts, commits = len(cw.fake.calls_named("put")), len(cw.fake.calls_named("commit"))
    finals_before = cw.fake.finals(COLD)

    result = cw.cold(break_lock=True)

    (leg,) = result["legs"]
    assert (leg["uploaded"], leg["resumed"], leg["state"]) == (False, True, "verified")
    assert len(cw.fake.calls_named("put")) == puts and len(cw.fake.calls_named("commit")) == commits
    assert cw.fake.finals(COLD) == finals_before
    (rec,) = cw.copy_events("output_copy.recorded")
    assert rec["data"]["broken_lock"] == {
        "holder": cw.fake.holder
    }  # the dead holder is on the record
    assert cw.where()["n_verified"] == 1


def test_a_stale_lock_is_lock_held_and_break_lock_recovers_and_records_the_holder(cw: ColdWorld):
    cw.standard_run()
    cw.ingest()
    stale = {
        "host": "gone-host",
        "pid": 1,
        "attempt": "00000000",
        "started": "2025-01-01T00:00:00Z",
    }
    cw.fake.hold_lock(COLD, cw.dest(), stale)

    with pytest.raises(OutputTierError) as e:
        cw.cold()
    assert (
        _code(e) == "lock_held" and "gone-host" in e.value.detail and "2025-01-01" in e.value.detail
    )
    assert (
        cw.fake.mutating_calls().count("put") == 0
    )  # nothing was uploaded under someone else's lock

    result = cw.cold(break_lock=True)

    assert result["legs"][0]["state"] == "verified"
    assert result["legs"][0]["broken_lock"] == stale
    (rec,) = cw.copy_events("output_copy.recorded")
    assert rec["data"]["broken_lock"] == {"holder": stale}
    assert not cw.fake.lock_exists(COLD, cw.dest())


def test_fake_unlock_of_an_absent_lock_is_an_error_like_real_myxcel(cw: ColdWorld):
    """Real `myxcel cold unlock` exits 2 'lock does not exist' (status error) when there is no `.bth-lock`."""
    res = mx.unlock(COLD, "bathos-cold/repo/nothing-here", runner=cw.fake)
    assert res.status == "error" and "does not exist" in res.message
    assert mx.lock(COLD, "bathos-cold/repo/nothing-here", runner=cw.fake).status == "acquired"
    assert mx.unlock(COLD, "bathos-cold/repo/nothing-here", runner=cw.fake).status == "released"
    assert mx.unlock(COLD, "bathos-cold/repo/nothing-here", runner=cw.fake).status == "error"


def test_break_lock_when_the_holder_releases_first_tolerates_the_unlock_error_and_records_no_break(
    cw: ColdWorld,
):
    """`lock` says held, the holder releases, our `unlock` then errors 'lock does not exist': nothing is left to
    break, and the relock decides. It acquires, so the run proceeds and no broken holder is recorded."""
    cw.standard_run()
    cw.ingest()
    cw.fake.hold_lock(COLD, cw.dest())

    def holder_releases(args: list[str], base: Path) -> None:
        shutil.rmtree(base / args[0] / ".bth-lock", ignore_errors=True)

    cw.fake.before("unlock", holder_releases)

    result = cw.cold(break_lock=True)

    assert result["legs"][0]["state"] == "verified"
    assert result["legs"][0]["broken_lock"] is None
    (rec,) = cw.copy_events("output_copy.recorded")
    assert rec["data"]["broken_lock"] is None
    assert not cw.fake.lock_exists(COLD, cw.dest())


def test_break_lock_whose_unlock_fails_for_another_reason_is_lock_held_with_the_message(
    cw: ColdWorld,
):
    """An unlock `error` that left the lock in place is not 'nothing to break': the relock answers held, and the
    run refuses (lock_held) naming the unlock error. Nothing is uploaded."""
    cw.standard_run()
    cw.ingest()
    cw.fake.hold_lock(COLD, cw.dest())
    cw.fake.fail("unlock", "rmdir: permission denied")

    with pytest.raises(OutputTierError) as e:
        cw.cold(break_lock=True)

    assert _code(e) == "lock_held" and "permission denied" in e.value.detail
    assert cw.fake.mutating_calls().count("put") == 0
    assert cw.fake.lock_exists(COLD, cw.dest())  # the planted lock was not touched


def test_break_lock_on_a_directory_with_no_lock_just_locks(cw: ColdWorld):
    """No lock to break: `lock` acquires at once, `unlock` is not called to break anything (only the normal
    release at the end), and no break is recorded."""
    cw.standard_run()
    cw.ingest()

    result = cw.cold(break_lock=True)

    assert result["legs"][0]["state"] == "verified" and result["legs"][0]["broken_lock"] is None
    assert len(cw.fake.calls_named("lock")) == 1
    assert len(cw.fake.calls_named("unlock")) == 1  # the release, after the writes
    assert not cw.fake.lock_exists(COLD, cw.dest())


def test_a_failed_normal_release_after_we_held_the_lock_is_still_a_reported_warning(
    cw: ColdWorld,
):
    """Once we hold the lock, an unlock `error` (the fake now answers it for a missing lock too) stays a real
    problem and is surfaced, not swallowed."""
    cw.standard_run()
    cw.ingest()
    cw.fake.fail("unlock", "lock does not exist")

    result = cw.cold()

    (warning,) = result["legs"][0]["warnings"]
    assert "was not released" in warning and "lock does not exist" in warning


def test_fake_get_refuses_a_missing_local_dir_like_real_myxcel(cw: ColdWorld, tmp_path: Path):
    cw.fake.remote(COLD, "d").mkdir(parents=True)
    cw.fake.remote(COLD, "d/f.txt").write_text("hello")
    missing = tmp_path / "no-such-dir"

    res = mx.get(COLD, "d/f.txt", str(missing), runner=cw.fake)

    assert res.status == "error" and "local_dir does not exist" in res.message
    assert not missing.exists()  # the fake no longer creates it


def test_bathos_creates_the_scratch_directory_before_get(cw: ColdWorld, tmp_path: Path):
    """`_get_bytes` mkdirs its scratch target (real `get` will not), so a fetch through it works against the
    strict fake."""
    cw.fake.remote(COLD, "d").mkdir(parents=True)
    cw.fake.remote(COLD, "d/f.txt").write_text("hello")
    leg = SimpleNamespace(profile=SimpleNamespace(name=COLD))
    scratch = tmp_path / "scratch-not-yet-created"

    got = _get_bytes(leg, "d/f.txt", scratch, cw.fake, [0])  # type: ignore[arg-type]

    assert got == b"hello"
    assert (scratch / "get-1").is_dir()


@pytest.mark.parametrize(
    "bad_slug", ["my project", "-leading-dash", "naïve", "tab\there", "dollar$sign"]
)
def test_a_bad_project_slug_is_refused_before_any_myxcel_call(
    cw: ColdWorld, monkeypatch: pytest.MonkeyPatch, bad_slug: str
):
    """Real myxcel accepts only `[A-Za-z0-9._@+=,%-]` segments, none starting with '-'. bathos mirrors that, so a
    bad slug refuses locally (invalid_param) and the fake sees zero calls."""
    cw.standard_run()
    cw.ingest()
    real_open_run = cold_module.open_run
    monkeypatch.setattr(
        cold_module,
        "open_run",
        lambda *a, **k: dataclasses.replace(real_open_run(*a, **k), project_slug=bad_slug),
    )

    with pytest.raises(OutputTiersConfigError) as e:
        cw.cold()

    assert e.value.error_code == "invalid_param"
    assert cw.fake.calls == []


def test_a_normal_slug_with_the_allowed_punctuation_passes(
    cw: ColdWorld, monkeypatch: pytest.MonkeyPatch
):
    cw.standard_run()
    cw.ingest()
    slug = "my-proj_1.2+x@y=z,w%"
    real_open_run = cold_module.open_run
    monkeypatch.setattr(
        cold_module,
        "open_run",
        lambda *a, **k: dataclasses.replace(real_open_run(*a, **k), project_slug=slug),
    )

    result = cw.cold()

    assert result["legs"][0]["state"] == "verified"
    assert cw.fake.lock_exists(COLD, cw.dest(slug=slug)) is False
    assert cw.fake.calls_named("lock")[0][1] == cw.dest(slug=slug)  # [profile, dir]


def test_two_concurrent_writers_one_gets_lock_held_and_the_other_finishes(cw: ColdWorld):
    """Writer A is parked inside its first upload (lock held); B must refuse. Without the lock B would race A."""
    cw.standard_run()
    cw.ingest()
    parked, release = threading.Event(), threading.Event()
    first = threading.Event()

    def park(_args, _base):
        if not first.is_set():
            first.set()
            parked.set()
            assert release.wait(30)

    cw.fake.before("put", park)
    out: dict = {}

    def writer_a():
        try:
            out["a"] = cw.cold()
        except BaseException as e:  # noqa: BLE001 - surfaced to the main thread below
            out["a_exc"] = e

    t = threading.Thread(target=writer_a)
    t.start()
    assert parked.wait(30)
    try:
        with pytest.raises(OutputTierError) as e:
            cw.cold()
        assert _code(e) == "lock_held"
    finally:
        release.set()
        t.join(60)

    assert "a_exc" not in out, out.get("a_exc")
    assert out["a"]["legs"][0]["state"] == "verified"
    assert len(cw.fake.calls_named("commit")) == 2  # only A committed
    assert not cw.fake.lock_exists(COLD, cw.dest())


# --------------------------------------------------------------------------------------------------
# supersede


def _half_committed(cw: ColdWorld) -> None:
    cw.fake.kill_after("commit", 1)
    with pytest.raises(KillSwitch):
        cw.cold()
    cw.fake.revive()
    with pytest.raises(OutputTierError) as e:
        cw.cold(break_lock=True)
    assert "half_committed" in e.value.detail


def test_half_committed_then_supersede_ends_with_a_fresh_verified_copy_and_the_old_pair_findable(
    cw: ColdWorld,
):
    cw.standard_run()
    cw.ingest()
    m_name, a_name = cw.final_names()
    _half_committed(cw)
    old_manifest = cw.fake.remote(COLD, f"{cw.dest()}/{m_name}").read_bytes()

    result = cw.cold(supersede=True, reason="half-committed pair from a killed writer")

    (leg,) = result["legs"]
    assert leg["state"] == "verified" and leg["uploaded"] is True
    assert leg["superseded"]["renamed_to"] is None  # the archive never existed
    aside = leg["superseded"]["renamed_manifest_to"]
    assert ".superseded-" in aside
    # The old manifest was renamed, never deleted: its bytes are still there under the aside name.
    assert cw.fake.remote(COLD, aside.removeprefix(COLD_ROOT + "/")).read_bytes() == old_manifest
    assert {f"{cw.dest()}/{m_name}", f"{cw.dest()}/{a_name}"} <= set(cw.fake.finals(COLD))

    rows = cw.where()["copies"]
    live = [r for r in rows if r["renamed_from"] is None]
    old = [r for r in rows if r["renamed_from"] is not None]
    assert len(live) == 1 and live[0]["state"] == "verified" and live[0]["superseded_at"] is None
    assert len(old) == 1 and old[0]["state"] == "superseded" and old[0]["path"] == aside
    assert old[0]["renamed_from"] == live[0]["copy_id"] and old[0]["superseded_at"] is not None
    assert cw.where()["n_verified"] == 1  # the old pair is excluded from "verified copy" lookups


def test_superseding_a_complete_pair_renames_both_aside_and_the_rewrite_is_not_hidden(
    cw: ColdWorld,
):
    """The rewrite has the same copy_id as the superseded copy; the fold must clear superseded_at (B1)."""
    cw.standard_run()
    cw.ingest()
    m_name, a_name = cw.final_names()
    cw.cold()
    first = cw.fake.finals(COLD)

    result = cw.cold(supersede=True, reason="redo")

    sup = result["legs"][0]["superseded"]
    assert sup["renamed_to"] and sup["renamed_manifest_to"]
    after = cw.fake.finals(COLD)
    # four files: the old pair (aside) and the new pair; nothing from the first write is gone
    assert len(after) == 4
    assert set(first.values()) <= set(after.values())
    rows = cw.where()["copies"]
    live = [r for r in rows if r["renamed_from"] is None]
    assert len(live) == 1 and live[0]["superseded_at"] is None and live[0]["state"] == "verified"
    assert sum(1 for r in rows if r["state"] == "superseded") == 1
    assert cw.where()["n_verified"] == 1


def test_supersede_while_another_writer_holds_the_lock_is_lock_held_and_renames_nothing(
    cw: ColdWorld,
):
    cw.standard_run()
    cw.ingest()
    cw.cold()
    before = cw.fake.finals(COLD)
    cw.fake.hold_lock(COLD, cw.dest())
    commits = len(cw.fake.calls_named("commit"))

    with pytest.raises(OutputTierError) as e:
        cw.cold(supersede=True, reason="mine")

    assert _code(e) == "lock_held"
    assert cw.fake.finals(COLD) == before
    assert len(cw.fake.calls_named("commit")) == commits
    assert cw.copy_events("output_copy.superseded") == []


def test_supersede_with_nothing_to_rename_just_writes(cw: ColdWorld):
    cw.standard_run()
    cw.ingest()
    result = cw.cold(supersede=True, reason="nothing there")
    assert result["legs"][0]["superseded"] is None and result["legs"][0]["state"] == "verified"
    assert cw.copy_events("output_copy.superseded") == []


# --------------------------------------------------------------------------------------------------
# --all-uncopied


def _three_runs(cw: ColdWorld) -> None:
    for rid in (RID, RID2, RID3):
        cw.standard_run(rid)
    cw.ingest()


def test_all_uncopied_continues_past_a_failing_run_and_exits_1(
    cw: ColdWorld, monkeypatch: pytest.MonkeyPatch
):
    _three_runs(cw)
    cw.w.write(f"out/{RID2[:4]}/a.json", b"tampered")  # the middle run drifted

    result = cold_outputs(cw.w.catalog, "", all_uncopied=True, runner=cw.fake, project_slug="repo")

    assert result["ok"] is False and result["error_code"] == "cold_runs_failed"
    assert result["n_runs"] == 3 and result["n_failed"] == 1
    by_run = {r["run_id"]: r for r in result["results"]}
    assert by_run[RID2]["error_code"] == "outputs_drifted"
    assert by_run[RID]["ok"] and by_run[RID3]["ok"]  # the run AFTER the failure was still copied
    assert {r["run_id"] for r in cw.where(RID)["copies"]} == {RID}
    assert cw.where(RID3)["n_verified"] == 1 and cw.where(RID2)["n_copies"] == 0

    # ... and through the CLI the same result is exit 1 with the code in the message.
    monkeypatch.chdir(cw.w.repo)  # the current project comes from the cwd's .bth.toml
    res = CyclopticRunner().invoke(
        app, ["cold", "--all-uncopied", "--catalog-dir", str(cw.w.catalog)]
    )
    assert res.exit_code == 1
    assert "cold_runs_failed" in res.stderr and RID2[:8] in res.stderr


def test_all_uncopied_skips_runs_that_already_have_a_verified_copy(cw: ColdWorld):
    _three_runs(cw)
    cw.cold(RID[:8])
    puts = len(cw.fake.calls_named("put"))

    result = cold_outputs(cw.w.catalog, "", all_uncopied=True, runner=cw.fake, project_slug="repo")

    assert result["ok"] is True and result["n_runs"] == 2
    assert {r["run_id"] for r in result["results"]} == {RID2, RID3}
    assert len(cw.fake.calls_named("put")) == puts + 4  # two runs, manifest + archive each


def test_all_uncopied_covers_only_the_current_project(cw: ColdWorld):
    _three_runs(cw)
    result = cold_outputs(cw.w.catalog, "", all_uncopied=True, runner=cw.fake, project_slug="other")
    assert result["n_runs"] == 0 and cw.fake.calls_named("put") == []


def test_under_slurm_a_second_pass_uploads_nothing_and_every_run_prints_one_json_line(
    cw: ColdWorld, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    """SLURM nodes do not refresh the index, so pass 2 re-selects the same runs: the remote state (resume check)
    must turn them into record-only passes. An implementation that trusts the stale index re-uploads."""
    _three_runs(cw)
    monkeypatch.setenv("SLURM_JOB_ID", "12345")

    def run_pass() -> list[dict]:
        capsys.readouterr()
        result = cold_outputs(
            cw.w.catalog, "", all_uncopied=True, runner=cw.fake, project_slug="repo"
        )
        assert result["ok"] is True, result
        return [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.strip()]

    first = run_pass()
    puts, commits = len(cw.fake.calls_named("put")), len(cw.fake.calls_named("commit"))
    second = run_pass()

    for lines in (first, second):
        assert sorted(x["run_id"] for x in lines) == sorted([RID, RID2, RID3])  # one line per run
        assert all(set(x) == {"run_id", "leg", "state", "error_code"} for x in lines)
        assert all(
            x["leg"] == "cold" and x["state"] == "verified" and x["error_code"] is None
            for x in lines
        )
    assert len(cw.fake.calls_named("put")) == puts and len(cw.fake.calls_named("commit")) == commits
    # SLURM never refreshed the index: the progress lines and the event log are the only record.
    assert cw.where(RID)["n_copies"] == 0
    assert len(cw.copy_events()) == 0  # not ingested yet
    monkeypatch.delenv("SLURM_JOB_ID")
    from bathos.runlog.ingest import run_ingest

    run_ingest(cw.w.catalog)
    assert cw.where(RID)["n_verified"] == 1  # catches up at the next off-cluster ingest


def test_a_failure_under_slurm_prints_one_failed_line_for_that_run(
    cw: ColdWorld, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    _three_runs(cw)
    cw.w.write(f"out/{RID2[:4]}/a.json", b"tampered")
    monkeypatch.setenv("SLURM_JOB_ID", "1")
    capsys.readouterr()

    cold_outputs(cw.w.catalog, "", all_uncopied=True, runner=cw.fake, project_slug="repo")

    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.strip()]
    bad = [x for x in lines if x["run_id"] == RID2]
    assert (
        len(bad) == 1 and bad[0]["state"] == "failed" and bad[0]["error_code"] == "outputs_drifted"
    )
    assert len(lines) == 3


# --------------------------------------------------------------------------------------------------
# the index: an old index.db, and where


def test_an_index_built_before_the_table_still_reads_and_the_table_fills_on_the_next_ingest(
    cw: ColdWorld,
):
    """`connect_read` attaches read-only: a view over a missing table would raise CatalogException for every
    reader. Drop the table to look like an index.db from before this step."""
    cw.standard_run()
    cw.ingest()
    con = duckdb.connect(str(index_db_path(cw.w.catalog)))
    con.execute("DROP TABLE output_copies")
    con.close()

    rc = connect_read(cw.w.catalog)
    try:
        assert rc.execute("SELECT count(*) FROM output_copies").fetchone() == (0,)
        assert rc.execute("SELECT count(*) FROM runs").fetchone() == (1,)
    finally:
        rc.close()
    assert cw.where()["copies"] == [] and cw.where()["n_copies"] == 0
    assert "error" not in cw.where()
    assert cw.w.hash()["all_match"] is True  # another reader is unaffected

    cw.cold()  # writes events; the unit's ingest creates the table and folds them

    assert cw.where()["n_verified"] == 1
    con = duckdb.connect(str(index_db_path(cw.w.catalog)), read_only=True)
    try:
        assert con.execute("SELECT count(*) FROM output_copies").fetchone() == (1,)
    finally:
        con.close()


def test_where_resolves_a_prefix_and_refuses_an_unknown_run(cw: ColdWorld):
    cw.standard_run()
    cw.ingest()
    cw.cold()
    assert cw.where(RID[:8])["run_id"] == RID
    assert cw.where("ffffffff")["error_code"] == "run_not_found"
    assert where_outputs(cw.w.catalog, RID[:6])["n_copies"] == 1


def test_the_folded_event_dispatch_is_wired_into_ingest(cw: ColdWorld):
    """Hand-written output_copy events must reach the table through `fold_roots_into`; an unwired kind is
    stored in `events` and never folded (the watermark moves past it)."""
    cw.standard_run()
    cid = "abcd1234abcd1234"
    for i, (kind, data, ts) in enumerate(
        [
            (
                "output_copy.recorded",
                {
                    "tier": "cold",
                    "profile": COLD,
                    "host": "h",
                    "path": "/p/a.tar.zst",
                    "config_layer": "L",
                    "state": "unverifiable",
                    "verified_at": None,
                    "recorded_at": T1,
                },
                T1,
            ),
            ("output_copy.verified", {"state": "verified", "verified_at": T2}, T2),
        ]
    ):
        env = build_envelope(
            kind=kind,
            entity=[RID, cid],
            data=data,
            main_root=cw.w.repo,
            worktree_root=cw.w.root,
            project="repo",
            project_id=cw.w.pid,
            writer="w1",
            seq=100 + i,
            ts=ts,
            eid=f"e-copy-{i}",
        )
        cw.w._lines.append(json.dumps(env, sort_keys=True) + "\n")
    cw.ingest()

    (row,) = cw.where()["copies"]
    assert row["copy_id"] == cid and row["state"] == "verified" and row["verified_at"] == T2
    assert row["host"] == "h" and row["config_layer"] == "L"


# --------------------------------------------------------------------------------------------------
# verify_runlog fold parity


def test_verify_runlog_reports_an_event_that_never_folded_and_is_clean_otherwise(cw: ColdWorld):
    from bathos.verify import verify_runlog

    cw.standard_run()
    cw.ingest()
    cw.cold()
    clean = verify_runlog(cw.w.catalog)
    assert not [f for f in clean.stats["findings"] if f["type"] == "output_copies_fold_mismatch"]

    con = duckdb.connect(str(index_db_path(cw.w.catalog)))
    con.execute(
        "DELETE FROM output_copies"
    )  # the signature of an unwired kind: events stored, rows missing
    con.close()
    bad = verify_runlog(cw.w.catalog)
    found = [f for f in bad.stats["findings"] if f["type"] == "output_copies_fold_mismatch"]
    assert (
        len(found) == 1 and found[0]["detail"] == "missing_from_index" and found[0]["run_id"] == RID
    )
    assert not bad.ok


# --------------------------------------------------------------------------------------------------
# wiring: CLI and MCP


def test_cli_cold_dry_run_then_real_then_where(cw: ColdWorld):
    cw.standard_run()
    cw.ingest()
    runner = CyclopticRunner()
    base = ["cold", "--run-id", RID, "--catalog-dir", str(cw.w.catalog)]

    dry = runner.invoke(app, [*base, "--dry-run"])
    assert dry.exit_code == 0, dry.output
    assert json.loads(dry.stdout[dry.stdout.index("{") :])["n_files"] == 2
    assert cw.fake.calls == []

    real = runner.invoke(app, base)
    assert real.exit_code == 0, real.output
    assert json.loads(real.stdout[real.stdout.index("{") :])["legs"][0]["state"] == "verified"

    where = runner.invoke(
        app, ["outputs", "where", "--run-id", RID, "--catalog-dir", str(cw.w.catalog)]
    )
    assert where.exit_code == 0, where.output
    payload = json.loads(where.stdout[where.stdout.index("{") :])
    assert payload["copies"][0]["config_layer"] == "$BTH_COLD_PROFILE"

    cw.w.write(f"out/{RID[:4]}/a.json", b"tampered")
    bad = runner.invoke(app, [*base, "--supersede", "--reason", "x"])
    assert bad.exit_code == 1 and "outputs_drifted" in bad.stderr


def test_mcp_wrapper_returns_the_error_envelope_with_code_and_hint(cw: ColdWorld, monkeypatch):
    cw.standard_run()
    cw.ingest()
    cw.fake.hold_lock(COLD, cw.dest())

    result = asyncio.run(mcp_cold_outputs_tool(run_id=RID, catalog_dir=str(cw.w.catalog)))

    assert result["error_code"] == "lock_held" and result["resolution_hint"]
    assert result["error"].startswith("lock_held:")

    monkeypatch.delenv("BTH_COLD_PROFILE")
    nocold = cold_outputs_tool(run_id=RID, catalog_dir=str(cw.w.catalog))
    assert nocold["error_code"] == "no_cold_root" and "$BTH_COLD_PROFILE" in nocold["error"]


# --------------------------------------------------------------------------------------------------
# step 6b: partials (myxcel cold ls / rm-partial)


def _corrupt_archive_partial(cw: ColdWorld) -> None:
    def hook(args, base):
        if ".tar.zst.partial-" in args[1]:
            target = base / args[1]
            target.write_bytes(target.read_bytes()[:-1] + b"\x00")

    cw.fake.after("put", hook)


def _token(rel: str) -> str:
    return rel.rsplit(".partial-", 1)[1]


def test_a_verify_failure_removes_exactly_this_attempts_partials_and_touches_nothing_else(
    cw: ColdWorld,
):
    """Step 5. Broken: leave the partials (Step 6 behaviour), or remove by pattern and take the neighbours."""
    cw.standard_run()
    cw.ingest()
    d = cw.dest()
    other_attempt = f"{d}/run-{RID}.tar.zst.partial-aaaaaaaa"
    sibling_final = f"{d}/run-{RID}.superseded-20260101T000000Z.tar.zst"
    cw.fake.plant(COLD, other_attempt)
    cw.fake.plant(COLD, sibling_final, b"an older, superseded pair")
    planted = cw.fake.snapshot(COLD)
    _corrupt_archive_partial(cw)

    with pytest.raises(OutputTierError) as e:
        cw.cold()

    assert _code(e) == "copy_verify_failed"
    assert "removed this attempt's partials" in e.value.detail
    assert (
        cw.fake.snapshot(COLD) == planted
    )  # this attempt's two partials are gone, the two planted stay
    removed = [rel for rel, _ in cw.fake.rm_log]
    assert len(removed) == 2
    assert {_token(r) for r in removed} != {"aaaaaaaa"} and len({_token(r) for r in removed}) == 1
    assert {r.rsplit(".partial-", 1)[0] for r in removed} == {
        f"{d}/run-{RID}.manifest.json",
        f"{d}/run-{RID}.tar.zst",
    }
    assert all(held for _, held in cw.fake.rm_log), "every removal happens while the lock is held"
    assert not cw.fake.lock_exists(COLD, d)  # and the lock is released afterwards
    assert cw.copy_events() == []


def test_a_failed_commit_removes_this_attempts_partials_while_still_locked(cw: ColdWorld):
    cw.standard_run()
    cw.ingest()
    cw.fake.fail("commit", "disk full")

    with pytest.raises(OutputTierError) as e:
        cw.cold()

    assert _code(e) == "myxcel_error" and "disk full" in e.value.detail
    assert "removed this attempt's partials" in e.value.detail
    assert cw.fake.snapshot(COLD) == {}
    assert len(cw.fake.rm_log) == 2 and all(held for _, held in cw.fake.rm_log)
    assert not cw.fake.lock_exists(COLD, cw.dest())


def test_a_failed_archive_commit_removes_the_archive_partial_and_never_the_committed_manifest(
    cw: ColdWorld,
):
    """The manifest final was committed (so its partial is already linked away): cleanup reports it absent and
    the final stays (half_committed is fixed by --supersede, never by deleting)."""
    cw.standard_run()
    cw.ingest()
    m_name, a_name = cw.final_names()
    d = cw.dest()

    def archive_final_appears_first(args, base):
        if args[1].endswith(".tar.zst"):
            (base / args[1]).write_bytes(b"someone else's archive")

    cw.fake.before("commit", archive_final_appears_first)

    with pytest.raises(OutputTierError) as e:
        cw.cold()

    assert _code(e) == "archive_conflict" and "half_committed" in e.value.detail
    assert "already absent" in e.value.detail  # the manifest partial was committed away
    remote = cw.fake.snapshot(COLD)
    assert set(remote) == {f"{d}/{m_name}", f"{d}/{a_name}"}  # no partials, both finals kept
    assert remote[f"{d}/{a_name}"] != "" and (
        cw.fake.remote(COLD, f"{d}/{a_name}")
    ).read_bytes() == (b"someone else's archive")


def test_resume_reports_another_attempts_partial_by_name_and_does_not_remove_it(cw: ColdWorld):
    """Step 4. Broken: no listing, or a listing that deletes."""
    cw.standard_run()
    cw.ingest()
    d = cw.dest()
    leftover = f"run-{RID}.tar.zst.partial-aaaaaaaa"
    cw.fake.plant(COLD, f"{d}/{leftover}")

    first = cw.cold()
    again = cw.cold()  # the resume path: record only

    for res in (first, again):
        parts = res["legs"][0]["partials"]
        assert parts["listed"] is True and parts["other_attempts"] == [leftover]
        assert parts["removed"] == [] and parts["failed"] == []
    assert again["legs"][0]["resumed"] is True
    assert cw.fake.rm_log == [] and cw.fake.calls_named("rm-partial") == []
    assert f"{d}/{leftover}" in cw.fake.snapshot(COLD)


def test_a_clean_directory_lists_no_partials_and_a_missing_directory_is_not_an_error(
    cw: ColdWorld,
):
    cw.standard_run()
    cw.ingest()
    res = cw.cold()
    assert res["legs"][0]["partials"]["listed"] is True
    assert res["legs"][0]["partials"]["other_attempts"] == []
    assert res["legs"][0]["warnings"] == []
    (ls_args,) = cw.fake.calls_named("ls")
    assert ls_args == [COLD, cw.dest()]  # the run's own directory, and only that


def test_clean_partials_removes_stale_partials_in_the_runs_own_directory_only(cw: ColdWorld):
    """Broken: remove a final or the lock, reach into another run's directory, or remove a near-miss name."""
    cw.standard_run()
    cw.ingest()
    d = cw.dest()
    stale = [
        f"{d}/run-{RID}.manifest.json.partial-bbbbbbbb",
        f"{d}/run-{RID}.tar.zst.partial-aaaaaaaa",
    ]
    keep = [
        f"{d}/notes.txt",
        f"{d}/x.partial-0123abc",  # 7 hex: not a partial
        f"{d}/x.partial-0123ABCD",  # upper case: not a partial
        f"{d}/run-{RID}.superseded-20260101T000000Z.tar.zst",
        f"bathos-cold/repo/{RID2}/run-{RID2}.tar.zst.partial-cccccccc",  # another run's directory
        f"bathos-cold/other/{RID}/run-{RID}.tar.zst.partial-dddddddd",  # another project
    ]
    for rel in (*stale, *keep):
        cw.fake.plant(COLD, rel)

    res = cw.cold(clean_partials=True)

    leg = res["legs"][0]
    assert leg["state"] == "verified" and leg["uploaded"] is True
    assert leg["partials"]["removed"] == sorted(Path(r).name for r in stale)
    assert leg["partials"]["failed"] == [] and leg["warnings"] == []
    remote = cw.fake.snapshot(COLD)
    assert not any(r in remote for r in stale)
    assert all(r in remote for r in keep)
    m_name, a_name = cw.final_names()
    assert f"{d}/{m_name}" in remote and f"{d}/{a_name}" in remote
    assert sorted(rel for rel, _ in cw.fake.rm_log) == sorted(
        stale
    )  # nothing else was ever asked for
    assert all(held for _, held in cw.fake.rm_log), "removals happen only while the lock is held"
    assert all(not Path(rel).name.startswith(".bth-lock") for rel, _ in cw.fake.rm_log)
    assert not cw.fake.lock_exists(COLD, d)
    # the lock was taken before the first removal and released after the last
    subs = [s for s, _ in cw.fake.calls]
    assert subs.index("lock") < subs.index("rm-partial")
    assert max(i for i, s in enumerate(subs) if s == "rm-partial") < len(subs) - 1 - subs[
        ::-1
    ].index("unlock")


def test_clean_partials_with_the_lock_held_by_someone_else_removes_nothing(cw: ColdWorld):
    cw.standard_run()
    cw.ingest()
    d = cw.dest()
    stale = f"{d}/run-{RID}.tar.zst.partial-aaaaaaaa"
    cw.fake.plant(COLD, stale)
    cw.fake.hold_lock(COLD, d)

    with pytest.raises(OutputTierError) as e:
        cw.cold(clean_partials=True)

    assert _code(e) == "lock_held"
    assert cw.fake.rm_log == [] and stale in cw.fake.snapshot(COLD)


def test_a_failed_removal_under_clean_partials_is_a_warning_not_a_failed_copy(cw: ColdWorld):
    cw.standard_run()
    cw.ingest()
    d = cw.dest()
    stale = f"{d}/run-{RID}.tar.zst.partial-aaaaaaaa"
    cw.fake.plant(COLD, stale)
    cw.fake.fail("rm-partial", "permission denied")

    res = cw.cold(clean_partials=True)

    leg = res["legs"][0]
    assert leg["state"] == "verified"
    assert leg["partials"]["removed"] == []
    assert [f["name"] for f in leg["partials"]["failed"]] == [Path(stale).name]
    assert any("permission denied" in w for w in leg["warnings"])
    assert stale in cw.fake.snapshot(COLD)
    assert cw.where()["n_verified"] == 1


def test_a_failed_ls_is_reported_and_does_not_fail_an_otherwise_good_copy(cw: ColdWorld):
    cw.standard_run()
    cw.ingest()
    cw.fake.fail("ls", "ssh: connection reset")

    res = cw.cold()

    leg = res["legs"][0]
    assert leg["state"] == "verified"
    assert (
        leg["partials"]["listed"] is False and "connection reset" in leg["partials"]["list_error"]
    )
    assert any("connection reset" in w for w in leg["warnings"])


def test_an_old_myxcel_without_ls_and_rm_partial_behaves_as_step_6(cw: ColdWorld):
    """No listing, no removal, partials left and named, and --clean-partials refuses myxcel_capability_gap."""
    cw.fake.without_ls_rm_partial()
    cw.standard_run()
    cw.ingest()
    d = cw.dest()
    other = f"{d}/run-{RID}.tar.zst.partial-aaaaaaaa"
    cw.fake.plant(COLD, other)

    # --clean-partials refuses before anything touches the remote
    with pytest.raises(OutputTierError) as e:
        cw.cold(clean_partials=True)
    assert _code(e) == "myxcel_capability_gap" and "6.2" in e.value.detail
    assert "ls, rm-partial" in e.value.detail
    assert cw.fake.calls == [] and other in cw.fake.snapshot(COLD)

    # a verify failure leaves this attempt's partials in place and names them
    before = set(cw.fake.snapshot(COLD))
    _corrupt_archive_partial(cw)
    with pytest.raises(OutputTierError) as e:
        cw.cold()
    assert _code(e) == "copy_verify_failed" and "left in place" in e.value.detail
    leftovers = set(cw.fake.snapshot(COLD)) - before
    assert len(leftovers) == 2 and all(".partial-" in k for k in leftovers)
    assert all(Path(k).name in e.value.detail for k in leftovers)

    # a good copy lists nothing
    cw.fake.after("put", lambda _args, _base: None)
    ok = cw.cold()
    assert ok["legs"][0]["partials"]["listed"] is False
    assert ok["legs"][0]["partials"]["other_attempts"] == []
    assert cw.fake.calls_named("ls") == [] and cw.fake.calls_named("rm-partial") == []
    assert cw.fake.unsupported_calls == [], (
        "bathos never invoked a primitive the probe said was missing"
    )


def test_a_myxcel_with_ls_but_no_rm_partial_lists_but_cannot_clean(cw: ColdWorld):
    cw.fake.has_rm_partial = False
    cw.standard_run()
    cw.ingest()
    d = cw.dest()
    leftover = f"run-{RID}.tar.zst.partial-aaaaaaaa"
    cw.fake.plant(COLD, f"{d}/{leftover}")

    with pytest.raises(OutputTierError) as e:
        cw.cold(clean_partials=True)
    assert _code(e) == "myxcel_capability_gap" and "rm-partial" in e.value.detail
    assert "lacks: rm-partial" in e.value.detail

    res = cw.cold()  # no cleaning requested: the listing still works
    assert res["legs"][0]["partials"]["other_attempts"] == [leftover]
    assert cw.fake.unsupported_calls == []
