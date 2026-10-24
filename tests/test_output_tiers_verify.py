"""Output tiering step 8: ``bth verify --tier outputs`` (spec 261023_output-tiering.md sections 4, 5.3, 7 and
9 row 8; review finding F-18, "vacuous checks").

Built on the step 6 fixtures: a real log-mode catalog and a fake ``myxcel cold`` runner over a temp directory.
The fake hashes the file it finds, so a flipped byte on the "remote" is a real mismatch and an unreachable host is
a real transport failure, not a canned answer. Each test names the broken implementation it guards against.
"""

# ruff: noqa: F811
from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from bathos.cli_cyclopts import app
from bathos.mcp import mcp_verify_tool, verify_tool
from bathos.output_tiers import myxcel as mx
from bathos.output_tiers import verify as verify_module
from bathos.output_tiers.verify import verify_outputs
from bathos.runlog.emit import emit_event
from bathos.runlog.mode import cutover_marker_path
from tests._cyclopts_runner import CyclopticRunner
from tests._fake_myxcel import FakeMyxcel, KillSwitch
from tests.test_output_tiers_adopt import RID
from tests.test_output_tiers_cold import (  # noqa: F401  (fixtures used by name)
    COLD,
    COLD_ROOT,
    MIRROR,
    RID2,
    ColdWorld,
    _fresh_probe_cache,
    cw,
)

VERIFIED = "output_copy.verified"


def _flip_first_byte(path: Path) -> bytes:
    """Damage a remote file in place and return its original bytes (so a test can repair it)."""
    original = path.read_bytes()
    path.write_bytes(bytes([original[0] ^ 0x01]) + original[1:])
    return original


def _archive(cw: ColdWorld, profile: str = COLD, rid: str = RID) -> Path:
    return cw.fake.remote(profile, f"{cw.dest(rid)}/{cw.final_names(rid)[1]}")


def _manifest(cw: ColdWorld, profile: str = COLD, rid: str = RID) -> Path:
    return cw.fake.remote(profile, f"{cw.dest(rid)}/{cw.final_names(rid)[0]}")


def _verify(cw: ColdWorld, run: str = "", **kw):
    kw.setdefault("runner", cw.fake)
    return verify_outputs(cw.w.catalog, run, **kw)


def _states(result: dict) -> dict[tuple[str, str], str]:
    return {(c["tier"], c["profile"]): c["state"] for r in result["runs"] for c in r["copies"]}


def _copied(cw: ColdWorld, *, mirror: bool = True, rid: str = RID) -> None:
    cw.standard_run(rid)
    cw.ingest()
    cw.cold(rid[:8], **({"mirror": MIRROR} if mirror else {}))
    mx.reset_capability_cache()  # the verify is a separate process: it probes myxcel itself


class Down:
    """A host that cannot be reached: every ``verify-file`` is a transport failure (the ssh call times out)."""

    def __init__(self, inner: FakeMyxcel) -> None:
        self.inner = inner
        self.verify_calls = 0

    def __call__(self, argv, timeout):
        if argv[:3] == ["myxcel", "cold", "verify-file"]:
            self.verify_calls += 1
            raise subprocess.TimeoutExpired(argv, timeout)
        return self.inner(argv, timeout)


# --------------------------------------------------------------------------------------------------
# the measured states


def test_verifying_intact_copies_reads_both_files_of_each_leg_and_records_verified_events(
    cw: ColdWorld,
):
    """Both legs re-read, archive AND manifest each, against the shas in the copy record; one event per copy with the
    method and a timestamp. A verify that reads only the archive, or records nothing, fails."""
    _copied(cw)
    cw.fake.calls.clear()

    result = _verify(cw)

    assert result["ok"] is True and "error" not in result
    assert _states(result) == {("cold", COLD): "verified", ("mirror", MIRROR): "verified"}
    assert (result["n_runs"], result["n_copies"], result["n_verified"]) == (1, 2, 2)
    assert result["runs"][0]["verdict"] == "verified" and result["runs"][0]["agree"] is True
    reads = sorted((p, a[0]) for s, (p, *a) in cw.fake.calls if s == "verify-file")
    m_name, a_name = cw.final_names()
    want = sorted(
        (prof, f"{cw.dest()}/{name}") for prof in (COLD, MIRROR) for name in (a_name, m_name)
    )
    assert reads == want
    events = cw.copy_events(VERIFIED)
    assert len(events) == 2
    for ev in events:
        assert ev["entity"][0] == RID
        d = ev["data"]
        assert d["state"] == "verified" and d["method"] == "login_ssh" and d["verified_at"]
        assert d["checked_at"] and d["archive"]["state"] == "verified"
        assert d["manifest"]["state"] == "verified"


def test_a_flipped_byte_on_the_remote_is_failed_recorded_and_exits_nonzero(cw: ColdWorld):
    """The archive's bytes were read and differ: ``failed`` (never ``unverifiable``), recorded, and ``where`` shows it
    after the ingest. The other leg is still measured. An implementation that trusts the recorded state, or maps a
    mismatch to unverifiable, fails here."""
    _copied(cw)
    _flip_first_byte(_archive(cw, COLD))
    runner = CyclopticRunner()

    result = runner.invoke(
        app,
        ["verify", "--tier", "outputs", "--run-id", RID, "--catalog-dir", str(cw.w.catalog)],
    )

    assert result.exit_code == 1, result.output
    report = json.loads(result.stdout[result.stdout.index("{") :])
    assert report["ok"] is False and report["error_code"] == "output_copies_failed"
    assert _states(report) == {("cold", COLD): "failed", ("mirror", MIRROR): "verified"}
    assert report["runs"][0]["verdict"] == "failed"
    assert "output_copies_failed" in result.stderr
    rows = {r["tier"]: r for r in cw.where()["copies"]}
    assert rows["cold"]["state"] == "failed" and rows["cold"]["verified_at"] is None
    assert rows["mirror"]["state"] == "verified" and rows["mirror"]["verified_at"]
    assert cw.where()["n_verified"] == 1


def test_a_flipped_byte_in_the_manifest_alone_fails_the_copy(cw: ColdWorld):
    """The manifest is verified too (spec 4: archive and manifest). A verify that checks only the archive passes it."""
    _copied(cw, mirror=False)
    _flip_first_byte(_manifest(cw))

    result = _verify(cw)

    (copy,) = result["runs"][0]["copies"]
    assert copy["state"] == "failed"
    assert copy["archive"]["state"] == "verified" and copy["manifest"]["state"] == "failed"
    assert "manifest" in copy["detail"] and result["ok"] is False


def test_the_latest_verification_wins_repairing_the_bytes_turns_failed_back_to_verified(
    cw: ColdWorld,
):
    """Fold picks the latest of {recorded, verified}. A fold that ignores ``output_copy.verified`` (or picks the
    oldest) leaves the first answer in ``where``."""
    _copied(cw, mirror=False)
    original = _flip_first_byte(_archive(cw))
    assert _verify(cw)["ok"] is False
    assert [r["state"] for r in cw.where()["copies"]] == ["failed"]

    _archive(cw).write_bytes(original)
    again = _verify(cw)

    assert again["ok"] is True
    (row,) = cw.where()["copies"]
    assert row["state"] == "verified" and row["verified_at"]
    assert len(cw.copy_events(VERIFIED)) == 2


def test_an_unreachable_host_is_unverifiable_never_failed_and_exit_zero_unless_strict(
    cw: ColdWorld,
):
    """Bytes that could not be read are not bytes that differ (three-state rule). Transport failure -> ``unverifiable``
    recorded and shown by ``where``; exit 0 by default, 1 under ``--strict``, and still never ``failed``."""
    _copied(cw, mirror=False)
    down = Down(cw.fake)

    result = _verify(cw, runner=down)

    assert down.verify_calls == 2  # both files tried
    assert result["ok"] is True and result["n_failed"] == 0 and result["n_unverifiable"] == 1
    (copy,) = result["runs"][0]["copies"]
    assert copy["state"] == "unverifiable" and copy["previous_state"] == "verified"
    assert copy["archive"]["state"] == "unverifiable" and "timed out" in copy["archive"]["message"]
    assert result["runs"][0]["verdict"] == "unverifiable"
    (ev,) = cw.copy_events(VERIFIED)
    assert ev["data"]["state"] == "unverifiable" and ev["data"]["verified_at"] is None
    (row,) = cw.where()["copies"]
    assert row["state"] == "unverifiable" and row["verified_at"] is None

    strict = _verify(cw, runner=Down(cw.fake), strict=True)
    assert strict["ok"] is False and strict["error_code"] == "output_copies_unverifiable"
    assert strict["n_failed"] == 0 and _states(strict) == {("cold", COLD): "unverifiable"}


def test_an_unverifiable_answer_from_myxcel_is_reported_distinctly_through_the_tool(cw: ColdWorld):
    """CLI/MCP shape: unverifiable is a warning (ok), strict makes it an error with its own code."""
    _copied(cw, mirror=False)
    cw.fake.fail("verify-file", "Duo timed out", status="unverifiable")

    lax = verify_tool(tier="outputs", catalog_dir=str(cw.w.catalog))
    strict = verify_tool(tier="outputs", catalog_dir=str(cw.w.catalog), strict=True)

    assert lax["ok"] is True and "error" not in lax
    (res,) = lax["results"]
    assert res["tier"] == "outputs" and res["errors"] == [] and "unverifiable" in res["warnings"][0]
    assert strict["ok"] is False and strict["error_code"] == "output_copies_unverifiable"
    assert (
        strict["resolution_hint"] and "failed" not in strict["outputs_report"]["runs"][0]["verdict"]
    )


def test_cold_and_mirror_recording_different_hashes_make_the_run_failed_even_if_each_verifies(
    cw: ColdWorld,
):
    """Spec 7 ("cold and mirror disagree -> run verdict failed"). Each copy matches its OWN record, so a verify that
    only compares each copy to its record reports success."""
    _copied(cw)
    rows = {r["tier"]: r for r in cw.where()["copies"]}
    mirror = rows["mirror"]
    new = b"a different archive"
    mpath = cw.fake.remote(MIRROR, f"{cw.dest()}/{cw.final_names()[1]}")
    mpath.write_bytes(new)
    import hashlib

    emit_event(
        kind="output_copy.recorded",
        entity=[RID, mirror["copy_id"]],
        data={
            "tier": "mirror",
            "profile": MIRROR,
            "host": mirror["host"],
            "path": mirror["path"],
            "config_layer": mirror["config_layer"],
            "archive_sha256": hashlib.sha256(new).hexdigest(),
            "archive_size_bytes": len(new),
            "manifest_sha256": mirror["manifest_sha256"],
            "state": "verified",
            "verified_at": "2099-01-01T00:00:00.000000Z",
            "recorded_at": "2099-01-01T00:00:00.000000Z",
        },
        cwd=cw.w.repo,
        hard_fail=True,
    )

    result = _verify(cw)

    assert _states(result) == {("cold", COLD): "verified", ("mirror", MIRROR): "verified"}
    (run,) = result["runs"]
    assert run["agree"] is False and "archive_sha256" in run["agree_detail"]
    assert run["verdict"] == "failed" and result["ok"] is False
    assert result["error_code"] == "output_copies_failed"


# --------------------------------------------------------------------------------------------------
# opt-in only: the default tier makes no network call


def test_default_verify_makes_no_myxcel_call_and_has_no_outputs_tier(
    cw: ColdWorld, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """F-18: ``--tier all`` must not reach the network. Every myxcel entry point is stubbed to record a call; with a
    recorded cold copy present, an implementation that folds the outputs tier into ``all`` (or probes myxcel) is
    caught. The stub is exercised first so a silent no-op stub cannot pass."""
    _copied(cw)
    calls: list[str] = []

    def stub_runner(argv, _timeout):
        calls.append("runner " + " ".join(argv[:4]))
        raise AssertionError("verify (default tier) must not run myxcel")

    for name in ("verify_file", "get", "put", "commit", "lock", "unlock", "probe_cold_capability"):

        def recorder(*_a, _n=name, **_k):
            calls.append(_n)
            raise AssertionError(f"verify (default tier) called mx.{_n}")

        monkeypatch.setattr(mx, name, recorder)
    monkeypatch.setattr(mx, "default_runner", stub_runner)
    n_events = len(cw.copy_events())

    from bathos.verify import verify_all

    direct = verify_all(cw.w.catalog, tmp_path / "archive")
    via_tool = verify_tool(catalog_dir=str(cw.w.catalog), archive_dir=str(tmp_path / "archive"))
    explicit = verify_tool(
        tier="all", catalog_dir=str(cw.w.catalog), archive_dir=str(tmp_path / "a")
    )
    cli = CyclopticRunner().invoke(app, ["verify", "--catalog-dir", str(cw.w.catalog)])
    mcp = asyncio.run(mcp_verify_tool(catalog_dir=str(cw.w.catalog)))

    assert calls == []
    assert [r.tier for r in direct] == ["cool", "warm", "archive", "runlog"]
    for payload in (via_tool, explicit, mcp):
        assert "outputs" not in [r["tier"] for r in payload["results"]]
        assert "outputs_report" not in payload
    assert "outputs" not in cli.output.replace('"outputs_report"', "")
    assert len(cw.copy_events()) == n_events  # no output_copy.verified written either

    # the stub is live: asking for the outputs tier does trip it
    with pytest.raises(AssertionError):
        verify_tool(tier="outputs", catalog_dir=str(cw.w.catalog))
    assert calls


# --------------------------------------------------------------------------------------------------
# which copies


def test_a_superseded_copy_is_skipped_and_only_the_live_pair_is_read(cw: ColdWorld):
    """A supersede renames the old pair aside (its row has ``superseded_at``). Verifying a renamed-aside pair would
    certify a file nobody uses; an implementation without the filter reads the ``.superseded-`` names."""
    cw.standard_run()
    cw.ingest()
    cw.cold()
    cw.cold(supersede=True, reason="redo")
    rows = cw.where()["copies"]
    assert sum(1 for r in rows if r["state"] == "superseded") == 1
    cw.fake.calls.clear()

    result = _verify(cw)

    assert result["n_copies"] == 1 and result["ok"] is True
    paths = [a[1] for s, a in cw.fake.calls if s == "verify-file"]
    assert len(paths) == 2 and not any("superseded" in p for p in paths)
    assert len(cw.copy_events(VERIFIED)) == 1
    aside = next(r for r in cw.where()["copies"] if r["state"] == "superseded")
    assert aside["state"] == "superseded"  # untouched by the verify


def test_a_run_prefix_limits_the_verify_to_that_run_and_no_prefix_covers_every_run(cw: ColdWorld):
    cw.standard_run(RID)
    cw.standard_run(RID2)
    cw.ingest()
    cw.cold(RID[:8])
    cw.cold(RID2[:8])
    cw.fake.calls.clear()

    one = _verify(cw, RID2[:8])
    only = {a[1].split("/")[2] for s, a in cw.fake.calls if s == "verify-file"}
    assert only == {RID2} and [r["run_id"] for r in one["runs"]] == [RID2]

    cw.fake.calls.clear()
    both = _verify(cw)
    seen = {a[1].split("/")[2] for s, a in cw.fake.calls if s == "verify-file"}
    assert seen == {RID, RID2} and both["n_runs"] == 2 and both["n_copies"] == 2


def test_profile_limits_the_copies_read_and_names_the_rest_as_skipped(cw: ColdWorld):
    _copied(cw)
    cw.fake.calls.clear()

    result = _verify(cw, profile=MIRROR)

    assert {p for _s, (p, *_a) in cw.fake.calls} == {MIRROR}
    assert _states(result) == {("mirror", MIRROR): "verified"}
    (skip,) = result["runs"][0]["skipped"]
    assert skip["reason"] == "other_profile" and skip["tier"] == "cold"


def test_a_run_with_no_copies_is_ok_and_never_touches_myxcel(cw: ColdWorld):
    cw.standard_run()
    cw.ingest()

    result = _verify(cw, RID[:8])

    assert result["ok"] is True and result["n_copies"] == 0
    assert result["runs"][0]["verdict"] == "no_copies" and cw.fake.calls == []
    assert _verify(cw)["n_runs"] == 0 and cw.fake.calls == []


# --------------------------------------------------------------------------------------------------
# refusals and degraded inputs


def test_an_old_myxcel_is_a_capability_gap_not_a_pile_of_unverifiable_copies(cw: ColdWorld):
    _copied(cw, mirror=False)

    class Old:
        def __call__(self, _argv, _timeout):
            return mx.RunResult(2, "", "Unused Tokens: ['cold']")

    with pytest.raises(Exception) as e:
        _verify(cw, runner=Old())
    assert e.value.error_code == "myxcel_capability_gap"  # type: ignore[attr-defined]
    assert cw.copy_events(VERIFIED) == []


def test_a_legacy_catalog_refuses_with_requires_log_mode(cw: ColdWorld):
    _copied(cw, mirror=False)
    cutover_marker_path(cw.w.catalog).unlink()
    out = verify_tool(tier="outputs", catalog_dir=str(cw.w.catalog))
    assert out["error_code"] == "requires_log_mode"


def test_a_profile_whose_cold_root_moved_is_unverifiable_not_a_guess(cw: ColdWorld):
    """The recorded absolute path must sit under the profile's current cold_root; otherwise bathos cannot name the
    file to myxcel and must not guess."""
    _copied(cw, mirror=False)
    (cw.cfg / f"{COLD}.toml").write_text('host = "coldhost"\ncold_root = "/elsewhere"\n')
    cw.fake.calls.clear()

    result = _verify(cw)

    (copy,) = result["runs"][0]["copies"]
    assert copy["state"] == "unverifiable" and "not under" in copy["detail"]
    assert cw.fake.calls_named("verify-file") == []


def test_a_missing_profile_file_makes_that_copy_unverifiable_and_the_others_proceed(cw: ColdWorld):
    _copied(cw)
    (cw.cfg / f"{MIRROR}.toml").unlink()

    result = _verify(cw)

    assert _states(result) == {("cold", COLD): "verified", ("mirror", MIRROR): "unverifiable"}
    mirror = next(c for c in result["runs"][0]["copies"] if c["tier"] == "mirror")
    assert str(cw.cfg / f"{MIRROR}.toml") in mirror["detail"]


def test_a_copy_recorded_without_a_manifest_size_has_the_manifest_re_read_with_get(
    cw: ColdWorld, monkeypatch: pytest.MonkeyPatch
):
    """``verify-file`` needs a size and the table has no manifest size; a record without ``manifest_size_bytes`` falls
    back to ``get`` + a local hash, so a flipped manifest byte is still ``failed``."""
    _copied(cw, mirror=False)
    monkeypatch.setattr(verify_module, "_recorded_manifest_size", lambda *_a, **_k: None)
    cw.fake.calls.clear()

    ok = _verify(cw)

    (copy,) = ok["runs"][0]["copies"]
    assert copy["state"] == "verified" and copy["manifest"]["method"] == "get"
    assert [a[1] for s, a in cw.fake.calls if s == "verify-file"] == [
        f"{cw.dest()}/{cw.final_names()[1]}"
    ]
    assert len(cw.fake.calls_named("get")) == 1

    _flip_first_byte(_manifest(cw))
    bad = _verify(cw)
    assert bad["runs"][0]["copies"][0]["manifest"]["state"] == "failed" and bad["ok"] is False


def test_bth_cold_records_the_manifest_size_that_verify_needs(cw: ColdWorld):
    _copied(cw, mirror=False)
    (ev,) = cw.copy_events("output_copy.recorded")
    assert ev["data"]["manifest_size_bytes"] == _manifest(cw).stat().st_size


def test_each_copy_is_recorded_as_soon_as_it_is_measured_a_kill_keeps_the_finished_ones(
    cw: ColdWorld,
):
    """Preemption safety: the cold leg's event exists before the mirror leg's reads finish. One event written at the
    end of the command would lose both."""
    _copied(cw)
    cw.fake.kill_after("verify-file", 3)  # the cold leg's two reads, then one mirror read

    with pytest.raises(KillSwitch):
        _verify(cw)

    events = cw.copy_events(VERIFIED)
    assert len(events) == 1 and events[0]["data"]["profile"] == COLD
    cw.fake.revive()


# --------------------------------------------------------------------------------------------------
# wiring


def test_mcp_wrapper_carries_the_report_the_code_and_the_hint(cw: ColdWorld):
    _copied(cw, mirror=False)
    _flip_first_byte(_archive(cw))

    result = asyncio.run(
        mcp_verify_tool(tier="outputs", run_id=RID[:8], catalog_dir=str(cw.w.catalog))
    )

    assert result["ok"] is False and result["error_code"] == "output_copies_failed"
    assert result["resolution_hint"]
    assert result["outputs_report"]["runs"][0]["copies"][0]["state"] == "failed"
    assert [r["tier"] for r in result["results"]] == ["outputs"]


def test_cli_success_prints_the_per_copy_json_and_exits_zero(cw: ColdWorld):
    _copied(cw)

    result = CyclopticRunner().invoke(
        app, ["verify", "--tier", "outputs", "--catalog-dir", str(cw.w.catalog), "--profile", COLD]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout[result.stdout.index("{") :])
    assert payload["results"][0]["tier"] == "outputs"
    assert payload["outputs_report"]["n_verified"] == 1
    assert COLD_ROOT in payload["outputs_report"]["runs"][0]["copies"][0]["path"]
