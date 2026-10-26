"""Distributional breakdown, T4 (spec 261026 D3): the claim-bound breakdown gate.

A run cited by a claim-bound campaign must have declared `[breakdown]` (or a run-time waiver) for
every worst-case aggregate its verdict reads, and a clean non-residual verdict must have emitted
the summary columns. Warn everywhere else; refuse only where the run is cited: `bth run
--campaign`, campaign add, claim register and (claim-bound) conclude. The compaction link path is
never gated, so register and conclude are the backstops.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest

from bathos.breakdown_gate import (
    Unresolvable,
    breakdown_gate,
    resolve_run_sidecar,
)
from bathos.campaigns import (
    BreakdownRefused,
    CampaignError,
    add_run_to_campaign,
    conclude_campaign,
    create_campaign,
)
from bathos.catalog import init_catalog, read_runs, write_run
from bathos.claim import register_claim
from bathos.compact import compact
from bathos.errors import EXCEPTION_TO_CODE, RESOLUTION_HINTS, BathosErrorCode
from bathos.schema import Run
from bathos.sidecar import Sidecar, parse_sidecar

_OFFENDING = textwrap.dedent("""
    [experiment]
    hypothesis = "h"
    [popper]
    null_pass_rate = 0.1
    alt_pass_rate = 0.9
    stopping_threshold = 20.0
    [outcomes.pass]
    condition = "e_max_placement <= 0.5"
    decision = "go"
    reasoning = "r"
    [outcomes.fail]
    condition = "e_max_placement > 0.5"
    decision = "stop"
    reasoning = "r"
    is_residual = true
    [result_schema]
    e_max_placement = "float"
""")

_BREAKDOWN = textwrap.dedent("""
    [breakdown]
    unit = "binder"
    [breakdown.aggregates.e_max_placement]
    direction = "max"
""")
_SUMMARY_SCHEMA = textwrap.dedent("""
    e_max_placement__worst_unit = "str"
    e_max_placement__second = "float"
    e_max_placement__median = "float"
    e_max_placement__n_units = "int"
""")
_SUMMARY = {
    "e_max_placement__worst_unit": "b17",
    "e_max_placement__second": 0.1,
    "e_max_placement__median": 0.004,
    "e_max_placement__n_units": 24,
}
# `[breakdown]` must precede every sub-table, so the covered form is built, not appended.
_COVERED = _OFFENDING.replace("[experiment]", _BREAKDOWN + "[experiment]", 1).replace(
    '[result_schema]\ne_max_placement = "float"\n',
    '[result_schema]\ne_max_placement = "float"\n' + _SUMMARY_SCHEMA,
)
# a waiver written before the run, in the run-time sidecar
_WAIVED = _OFFENDING.replace(
    "[experiment]",
    '[breakdown]\nwaive = "calibration; nothing is built on the maximum"\n[experiment]',
    1,
)

_CLAIM = """[claim]
headline = "Test claim"
kill_condition = "Outcome != expected"

[[hypotheses]]
id = "H_primary"
label = "Primary hypothesis"

[claim.union_gate]
[[claim.union_gate.clauses]]
id = "C_main"
description = "Main clause"
hypothesis_ids = ["H_primary"]
"""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _write_sidecar(tmp_path: Path, text: str, name: str = "probe.bth.toml") -> Path:
    p = tmp_path / "scripts" / "experiments" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return p


@pytest.fixture
def cat(tmp_catalog: Path) -> Path:
    init_catalog(tmp_catalog)
    seed = Run(
        project_slug="p",
        command="seed",
        argv=["seed"],
        git_hash="a",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime(2026, 5, 1, tzinfo=UTC),
        status="completed",
        exit_code=0,
    )
    write_run(seed, tmp_catalog)
    compact(tmp_catalog)
    return tmp_catalog


def _db(cat: Path):
    return duckdb.connect(str(cat / "bathos.db"))


def _bound_campaign(cat: Path, tmp_path: Path, mode: str = "confirmation"):
    db = _db(cat)
    campaign = create_campaign(db, name="gate", project_slug="p", mode=mode)
    claim_path = tmp_path / "test.claim.toml"
    claim_path.write_text(_CLAIM)
    register_claim(claim_path, campaign.id, db, tmp_path, catalog_dir=cat)
    db.close()  # recording runs compacts, which needs the catalog free; callers reopen with _db
    return campaign


def _record_run(
    cat: Path,
    campaign,
    sidecar_path: Path | None,
    *,
    sidecar_text: str | None = None,
    outcome: str = "pass",
    metadata: dict | None = None,
    stamp_campaign: bool = False,
    tentative: str | None = None,
) -> Run:
    run = Run(
        project_slug="p",
        command="python probe.py",
        argv=["python", "probe.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime.fromisoformat(campaign.started_at) + timedelta(minutes=1),
        status="completed",
        exit_code=0,
        outcome=outcome,
        tentative_outcome=tentative,
        metadata=json.dumps(metadata if metadata is not None else {"e_max_placement": 0.2}),
        sidecar_path=str(sidecar_path) if sidecar_path else "",
        sidecar_sha256=_sha(sidecar_text) if sidecar_text is not None else "",
        campaign_id=campaign.id if stamp_campaign else None,
    )
    write_run(run, cat)
    compact(cat)
    return run


# ── the error type ───────────────────────────────────────────────────────────────────


def test_breakdown_refused_is_registered_with_codes_and_hints():
    err = BreakdownRefused(BathosErrorCode.BREAKDOWN_MISSING, "x")
    assert isinstance(err, CampaignError) and isinstance(err, RuntimeError)
    assert err.code is BathosErrorCode.BREAKDOWN_MISSING
    assert EXCEPTION_TO_CODE["BreakdownRefused"] is BathosErrorCode.CAMPAIGN_ERROR
    for code in ("breakdown_missing", "sidecar_unverifiable", "fork_record_missing"):
        assert RESOLUTION_HINTS[BathosErrorCode(code)]


# ── the gate itself ──────────────────────────────────────────────────────────────────


def _sc(tmp_path, text) -> Sidecar:
    return parse_sidecar(_write_sidecar(tmp_path, text))


def _run_ns(outcome="pass", metadata=None, tentative=None):
    return SimpleNamespace(
        id="r1",
        outcome=outcome,
        tentative_outcome=tentative,
        metadata=json.dumps(metadata if metadata is not None else {}),
    )


def test_gate_flags_an_uncovered_aggregate(tmp_path):
    reasons = breakdown_gate(_run_ns(), _sc(tmp_path, _OFFENDING))
    assert reasons and "e_max_placement" in reasons[0]


def test_gate_accepts_a_waiver_and_a_covered_run_with_columns(tmp_path):
    assert breakdown_gate(_run_ns(), _sc(tmp_path, _WAIVED)) == []
    meta = {"e_max_placement": 0.2, **_SUMMARY}
    assert breakdown_gate(_run_ns(metadata=meta), _sc(tmp_path, _COVERED)) == []


def test_gate_columns_half_and_residual_exemption(tmp_path):
    sc = _sc(tmp_path, _COVERED)
    missing = breakdown_gate(_run_ns(metadata={"e_max_placement": 0.2}), sc)
    assert missing and "e_max_placement__worst_unit" in missing[0]
    # the aggregate-only half (used before a run has results) ignores the columns
    assert breakdown_gate(_run_ns(), sc, check_columns=False) == []
    # residual, error and invalid_measurement labels owe nothing
    for label in ("fail", "error", "invalid_measurement"):
        assert breakdown_gate(_run_ns(outcome=label), sc) == []


def test_gate_exploratory_run_reads_tentative_outcome(tmp_path):
    sc = _sc(tmp_path, _COVERED)
    assert breakdown_gate(_run_ns(outcome="observed", tentative="pass"), sc)
    assert breakdown_gate(_run_ns(outcome="observed", tentative="fail"), sc) == []


# ── the resolver ─────────────────────────────────────────────────────────────────────


def test_resolver_live_file_with_matching_sha(tmp_path):
    p = _write_sidecar(tmp_path, _OFFENDING)
    run = SimpleNamespace(id="r1", sidecar_path=str(p), sidecar_sha256=_sha(_OFFENDING))
    assert isinstance(resolve_run_sidecar(run, tmp_path), Sidecar)


def test_resolver_no_sidecar_is_outside_the_gate(tmp_path):
    run = SimpleNamespace(id="r1", sidecar_path="", sidecar_sha256="")
    assert resolve_run_sidecar(run, tmp_path) is None


def test_resolver_edited_sidecar_is_unresolvable_without_a_pin(tmp_path):
    p = _write_sidecar(tmp_path, _WAIVED)  # edited after the run recorded _OFFENDING's sha
    run = SimpleNamespace(id="r1", sidecar_path=str(p), sidecar_sha256=_sha(_OFFENDING))
    assert isinstance(resolve_run_sidecar(run, tmp_path), Unresolvable)


def test_resolver_empty_sha_fails_closed(tmp_path):
    p = _write_sidecar(tmp_path, _OFFENDING)
    run = SimpleNamespace(id="r1", sidecar_path=str(p), sidecar_sha256="")
    assert isinstance(resolve_run_sidecar(run, tmp_path), Unresolvable)


def test_resolver_recovers_the_run_time_sidecar_from_the_pinned_ref(tmp_path):
    from bathos.git_pin import pin_run

    def git(*args):
        return subprocess.run(["git", "-C", str(tmp_path), *args], capture_output=True, text=True)

    git("init", "-q")
    git("config", "user.email", "a@b")
    git("config", "user.name", "t")
    (tmp_path / "README").write_text("x")
    git("add", "README")
    git("commit", "-qm", "i")
    head = git("rev-parse", "HEAD").stdout.strip()
    p = _write_sidecar(tmp_path, _OFFENDING)  # untracked, as in a dirty worktree (spike T0)
    assert pin_run("rid1", head, "main", True, tmp_path).run_ref_ok
    p.write_text(_WAIVED)  # post-hoc waiver on the live file
    run = SimpleNamespace(id="rid1", sidecar_path=str(p), sidecar_sha256=_sha(_OFFENDING))
    resolved = resolve_run_sidecar(run, tmp_path)
    assert isinstance(resolved, Sidecar)
    assert resolved.breakdown is None  # the run-time sidecar, not the waived live file


def test_resolver_log_mode_uses_the_frozen_declaration(tmp_path, monkeypatch):
    from bathos import breakdown_gate as gate_module
    from bathos.runlog.emit import sidecar_declaration_for_event

    sc = _sc(tmp_path, _WAIVED)
    decl = json.loads(json.dumps(sidecar_declaration_for_event(sc)))
    monkeypatch.setattr(gate_module, "_run_started_declaration", lambda *_args: decl)
    run = SimpleNamespace(id="r1", sidecar_path="/gone.bth.toml", sidecar_sha256="deadbeef")
    resolved = resolve_run_sidecar(run, tmp_path, db=object(), log_mode=True)
    assert isinstance(resolved, Sidecar) and resolved.breakdown.waive


# ── gate point: campaign add ─────────────────────────────────────────────────────────


def test_add_refuses_an_uncovered_aggregate_and_leaves_no_threshold(cat, tmp_path):
    sidecar = _write_sidecar(tmp_path, _OFFENDING)
    campaign = _bound_campaign(cat, tmp_path, mode="sequential")
    run = _record_run(cat, campaign, sidecar, sidecar_text=_OFFENDING)
    db = _db(cat)
    with pytest.raises(BreakdownRefused) as exc:
        add_run_to_campaign(db, campaign.id, run.id, cat)
    assert exc.value.code is BathosErrorCode.BREAKDOWN_MISSING
    assert db.execute("SELECT COUNT(*) FROM campaign_runs").fetchone()[0] == 0
    # refused before `threshold_set`: the sidecar's popper threshold was not locked in
    assert db.execute(
        "SELECT stopping_threshold FROM campaigns WHERE id = ?", [campaign.id]
    ).fetchone() == (None,)


def test_add_to_an_unbound_campaign_accepts(cat, tmp_path):
    sidecar = _write_sidecar(tmp_path, _OFFENDING)
    db = _db(cat)
    campaign = create_campaign(db, name="u", project_slug="p", mode="confirmation")
    db.close()
    run = _record_run(cat, campaign, sidecar, sidecar_text=_OFFENDING)
    db = _db(cat)
    add_run_to_campaign(db, campaign.id, run.id, cat)
    assert db.execute("SELECT COUNT(*) FROM campaign_runs").fetchone()[0] == 1


def test_add_accepts_a_run_time_waiver_and_a_covered_run(cat, tmp_path):
    campaign = _bound_campaign(cat, tmp_path)
    waived = _write_sidecar(tmp_path, _WAIVED, "waived.bth.toml")
    r1 = _record_run(cat, campaign, waived, sidecar_text=_WAIVED)
    covered = _write_sidecar(tmp_path, _COVERED, "covered.bth.toml")
    r2 = _record_run(
        cat, campaign, covered, sidecar_text=_COVERED, metadata={"e_max_placement": 0.2, **_SUMMARY}
    )
    db = _db(cat)
    add_run_to_campaign(db, campaign.id, r1.id, cat)
    add_run_to_campaign(db, campaign.id, r2.id, cat)
    assert db.execute("SELECT COUNT(*) FROM campaign_runs").fetchone()[0] == 2


def test_add_refuses_a_post_hoc_waiver(cat, tmp_path):
    sidecar = _write_sidecar(tmp_path, _OFFENDING)
    campaign = _bound_campaign(cat, tmp_path)
    run = _record_run(cat, campaign, sidecar, sidecar_text=_OFFENDING)
    sidecar.write_text(_WAIVED)  # the waiver arrives after the run
    db = _db(cat)
    with pytest.raises(BreakdownRefused) as exc:
        add_run_to_campaign(db, campaign.id, run.id, cat)
    assert exc.value.code is BathosErrorCode.SIDECAR_UNVERIFIABLE


def test_add_refuses_missing_summary_columns_but_accepts_a_residual_run(cat, tmp_path):
    covered = _write_sidecar(tmp_path, _COVERED)
    campaign = _bound_campaign(cat, tmp_path)
    bad = _record_run(cat, campaign, covered, sidecar_text=_COVERED)  # outcome pass, no columns
    residual = _record_run(cat, campaign, covered, sidecar_text=_COVERED, outcome="fail")
    db = _db(cat)
    with pytest.raises(BreakdownRefused) as exc:
        add_run_to_campaign(db, campaign.id, bad.id, cat)
    assert exc.value.code is BathosErrorCode.BREAKDOWN_MISSING
    add_run_to_campaign(db, campaign.id, residual.id, cat)
    assert db.execute("SELECT COUNT(*) FROM campaign_runs").fetchone()[0] == 1


def test_add_accepts_a_run_with_no_sidecar(cat, tmp_path):
    campaign = _bound_campaign(cat, tmp_path)
    run = _record_run(cat, campaign, None)
    db = _db(cat)
    add_run_to_campaign(db, campaign.id, run.id, cat)
    assert db.execute("SELECT COUNT(*) FROM campaign_runs").fetchone()[0] == 1


# ── gate points: link path, register, conclude ───────────────────────────────────────


def test_link_never_raises_and_register_catches_the_member(cat, tmp_path):
    sidecar = _write_sidecar(tmp_path, _OFFENDING)
    db = _db(cat)
    campaign = create_campaign(db, name="late", project_slug="p", mode="confirmation")
    db.close()
    # recording compacts: that is the link path, which stamps membership and must not raise
    _record_run(cat, campaign, sidecar, sidecar_text=_OFFENDING, stamp_campaign=True)
    db = _db(cat)
    assert db.execute("SELECT COUNT(*) FROM campaign_runs").fetchone()[0] == 1
    claim_path = tmp_path / "test.claim.toml"
    claim_path.write_text(_CLAIM)
    with pytest.raises(BreakdownRefused) as exc:
        register_claim(claim_path, campaign.id, db, tmp_path, catalog_dir=cat)
    assert exc.value.code is BathosErrorCode.BREAKDOWN_MISSING
    # --force does not bypass
    with pytest.raises(BreakdownRefused):
        register_claim(claim_path, campaign.id, db, tmp_path, force=True, catalog_dir=cat)
    assert db.execute(
        "SELECT claim_path FROM campaigns WHERE id = ?", [campaign.id]
    ).fetchone() == (None,)


def _late_member_campaign(cat: Path, tmp_path: Path, mode: str):
    """A claim-bound campaign whose offending member arrived through the link path."""
    sidecar = _write_sidecar(tmp_path, _OFFENDING)
    campaign = _bound_campaign(cat, tmp_path, mode=mode)
    _record_run(cat, campaign, sidecar, sidecar_text=_OFFENDING, stamp_campaign=True)
    return _db(cat), campaign


def test_conclude_refuses_a_claim_bound_confirmation_campaign(cat, tmp_path):
    db, campaign = _late_member_campaign(cat, tmp_path, "confirmation")
    with pytest.raises(BreakdownRefused) as exc:
        conclude_campaign(db, campaign.id, "pass", "c", workspace_root=tmp_path, catalog_dir=cat)
    assert exc.value.code is BathosErrorCode.BREAKDOWN_MISSING
    assert db.execute("SELECT status FROM campaigns WHERE id = ?", [campaign.id]).fetchone() == (
        "open",
    )


def test_conclude_force_verdict_does_not_bypass(cat, tmp_path):
    db, campaign = _late_member_campaign(cat, tmp_path, "confirmation")
    with pytest.raises(BreakdownRefused):
        conclude_campaign(
            db,
            campaign.id,
            "pass",
            "c",
            workspace_root=tmp_path,
            force_verdict=True,
            catalog_dir=cat,
        )


def test_conclude_sequential_campaign_refuses_too(cat, tmp_path):
    db, campaign = _late_member_campaign(cat, tmp_path, "sequential")
    with pytest.raises(BreakdownRefused):
        conclude_campaign(db, campaign.id, "pass", "c", workspace_root=tmp_path, catalog_dir=cat)


def test_conclude_exploration_campaign_warns_and_proceeds(cat, tmp_path, capsys):
    db, campaign = _late_member_campaign(cat, tmp_path, "exploration")
    conclude_campaign(db, campaign.id, "pass", "c", workspace_root=tmp_path, catalog_dir=cat)
    assert "e_max_placement" in capsys.readouterr().out
    assert db.execute("SELECT status FROM campaigns WHERE id = ?", [campaign.id]).fetchone() == (
        "concluded",
    )


def test_conclude_accepts_a_clean_member_and_an_unresolvable_one_fails_closed(cat, tmp_path):
    sidecar = _write_sidecar(tmp_path, _WAIVED)
    campaign = _bound_campaign(cat, tmp_path)
    _record_run(cat, campaign, sidecar, sidecar_text=_WAIVED, stamp_campaign=True)
    # a second campaign whose member's sidecar was edited after the run: cannot be verified
    sidecar2 = _write_sidecar(tmp_path, _OFFENDING, "second.bth.toml")
    campaign2 = _bound_campaign(cat, tmp_path)
    _record_run(cat, campaign2, sidecar2, sidecar_text=_OFFENDING, stamp_campaign=True)
    sidecar2.write_text(_WAIVED)
    db = _db(cat)
    conclude_campaign(db, campaign.id, "pass", "c", workspace_root=tmp_path, catalog_dir=cat)
    db.close()
    db2 = _db(cat)
    with pytest.raises(BreakdownRefused) as exc:
        conclude_campaign(db2, campaign2.id, "pass", "c", workspace_root=tmp_path, catalog_dir=cat)
    assert exc.value.code is BathosErrorCode.SIDECAR_UNVERIFIABLE


# ── MCP and traced_tool ──────────────────────────────────────────────────────────────


def test_campaign_add_tool_returns_the_error_code(cat, tmp_path, monkeypatch):
    from bathos.mcp import campaign_add_tool

    sidecar = _write_sidecar(tmp_path, _OFFENDING)
    campaign = _bound_campaign(cat, tmp_path)
    run = _record_run(cat, campaign, sidecar, sidecar_text=_OFFENDING)
    monkeypatch.chdir(tmp_path)
    result = campaign_add_tool(run_id=run.id, campaign_id=campaign.id, catalog_dir=str(cat))
    assert result["ok"] is False
    assert result["error_code"] == "breakdown_missing"
    assert result["resolution_hint"]


def test_claim_register_returns_the_error_code(cat, tmp_path):
    from bathos.mcp import _claim_register_sync

    sidecar = _write_sidecar(tmp_path, _OFFENDING)
    db = _db(cat)
    campaign = create_campaign(db, name="reg", project_slug="p", mode="confirmation")
    db.close()
    _record_run(cat, campaign, sidecar, sidecar_text=_OFFENDING, stamp_campaign=True)
    claim_path = tmp_path / "test.claim.toml"
    claim_path.write_text(_CLAIM)
    result = _claim_register_sync(str(claim_path), campaign.id, cat, tmp_path, force=True)
    assert result["ok"] is False
    assert result["error_code"] == "breakdown_missing"


def test_conclude_tool_returns_the_error_code(cat, tmp_path):
    from bathos.mcp import campaign_conclude_tool

    db, campaign = _late_member_campaign(cat, tmp_path, "confirmation")
    db.close()
    result = campaign_conclude_tool(
        campaign_id=campaign.id,
        outcome_label="pass",
        catalog_dir=str(cat),
        workspace_root=str(tmp_path),
    )
    assert result["error_code"] == "breakdown_missing"


def test_traced_tool_shapes_the_specific_code():
    from bathos.mcp import traced_tool

    @traced_tool
    async def refuses():
        raise BreakdownRefused(BathosErrorCode.SIDECAR_UNVERIFIABLE, "cannot verify")

    result = asyncio.run(refuses())
    assert result["ok"] is False
    assert result["error_code"] == "sidecar_unverifiable"
    assert result["resolution_hint"] == RESOLUTION_HINTS[BathosErrorCode.SIDECAR_UNVERIFIABLE]


# ── gate point: bth run --campaign ───────────────────────────────────────────────────


_SCRIPT = textwrap.dedent("""
    import json, os
    p = os.environ.get("BTH_RESULTS_PATH")
    if p:
        json.dump({"e_max_placement": 0.2}, open(p, "w"))
""")


def _run_campaign(cat, tmp_path, sidecar_text, campaign_id):
    from bathos.runner import run_script

    d = tmp_path / "scripts" / "explore"
    d.mkdir(parents=True, exist_ok=True)
    (d / "probe.py").write_text(_SCRIPT)
    (d / "probe.bth.toml").write_text(sidecar_text)
    return run_script(
        argv=[sys.executable, str(d / "probe.py")],
        project_slug="p",
        catalog_dir=cat,
        output_paths=[],
        tags=[],
        cwd=tmp_path,
        campaign_id=campaign_id,
    )


def test_run_campaign_refuses_before_compute_when_claim_bound(cat, tmp_path, capsys):
    campaign = _bound_campaign(cat, tmp_path)
    before = len(read_runs(cat))
    assert _run_campaign(cat, tmp_path, _OFFENDING, campaign.id) == 1
    err = capsys.readouterr().err
    assert "e_max_placement" in err and "breakdown_missing" in err
    assert len(read_runs(cat)) == before  # refused before any run was recorded


def test_run_campaign_proceeds_for_covered_waived_and_unbound(cat, tmp_path):
    campaign = _bound_campaign(cat, tmp_path)
    assert _run_campaign(cat, tmp_path, _WAIVED, campaign.id) == 0
    covered_without_results = _COVERED  # the aggregate half only: results do not exist yet
    assert _run_campaign(cat, tmp_path, covered_without_results, campaign.id) == 0
    db = _db(cat)
    unbound = create_campaign(db, name="free", project_slug="p", mode="exploration")
    db.close()
    assert _run_campaign(cat, tmp_path, _OFFENDING, unbound.id) == 0
