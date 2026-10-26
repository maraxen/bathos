"""Distributional breakdown, T7a (spec 261026 D5/D6): discharge wiring, `query.lookup_run`, the
`near_bound` entity check and re-discharge, and the claim-bound conclude fork-record check.

`bth postmortem validate` (the MCP tool underneath) is what discharges obligations, so the
end-to-end tests drive the real tools against a real warm catalog and a real ledger."""

from __future__ import annotations

import hashlib
import json
import sys
import textwrap
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from bathos.campaigns import (
    BreakdownRefused,
    conclude_campaign,
    create_campaign,
)
from bathos.catalog import init_catalog, write_run
from bathos.claim import register_claim
from bathos.compact import compact
from bathos.errors import BathosErrorCode
from bathos.mcp import postmortem_validate_tool
from bathos.obligations import list_obligations, open_obligation
from bathos.query import AmbiguousRunId, lookup_run
from bathos.schema import Run

# ── fixtures ─────────────────────────────────────────────────────────────────────────

_SIDECAR = textwrap.dedent("""
    [breakdown]
    unit = "binder"
    [breakdown.aggregates.e_max_placement]
    direction = "max"
    bound = 0.5
    [experiment]
    hypothesis = "h"
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
    e_max_placement__worst_unit = "str"
    e_max_placement__second = "float"
    e_max_placement__median = "float"
    e_max_placement__n_units = "int"
    e_max_placement__n_over = "int"
""")
_NEAR = {  # 78db9e04: one of 24 over the bound
    "e_max_placement": 0.48,
    "e_max_placement__worst_unit": "b17",
    "e_max_placement__second": 0.1,
    "e_max_placement__median": 0.004,
    "e_max_placement__n_units": 24,
    "e_max_placement__n_over": 1,
}
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


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("BTH_OBLIGATION_NEAR_BOUND", raising=False)
    monkeypatch.delenv("BTH_NO_INGEST", raising=False)


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


def _campaign(cat: Path, tmp_path: Path, mode: str = "confirmation", bound: bool = True):
    db = _db(cat)
    campaign = create_campaign(db, name="c", project_slug="p", mode=mode)
    if bound:
        claim = tmp_path / "test.claim.toml"
        claim.write_text(_CLAIM)
        register_claim(claim, campaign.id, db, tmp_path, catalog_dir=cat)
    db.close()
    return campaign


def _run(
    cat: Path,
    tmp_path: Path,
    campaign,
    *,
    results: dict | None = None,
    run_id: str | None = None,
    stamp: bool = True,
    compacted: bool = True,
    name: str = "probe.bth.toml",
) -> Run:
    sidecar = tmp_path / "scripts" / "experiments" / name
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(_SIDECAR)
    kwargs = {"id": run_id} if run_id else {}
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
        outcome="pass",
        metadata=json.dumps(results if results is not None else _NEAR),
        sidecar_path=str(sidecar),
        sidecar_sha256=_sha(_SIDECAR),
        campaign_id=campaign.id if stamp else None,
        **kwargs,
    )
    write_run(run, cat)
    if compacted:
        compact(cat)
    return run


def _conclude(cat: Path, tmp_path: Path, campaign, **kw):
    db = _db(cat)
    try:
        conclude_campaign(
            db,
            campaign.id,
            "pass",
            "c",
            workspace_root=tmp_path,
            catalog_dir=cat,
            **kw,
        )
    finally:
        db.close()


_PM = textwrap.dedent("""
    run_id = "{run_id}"
    format = {fmt}
    [postmortem]
    hypothesis_status = "inconclusive"
    summary = "s"
    author = "me"
    status = "submitted"
    discharges = {discharges}
    {extra}
""")


def _fork(choice="stop", reason="internal validation only", cited="false") -> str:
    return f'[fork]\nchoice = "{choice}"\nreason = "{reason}"\ncited = {cited}\n'


def _write_pm(
    tmp_path: Path,
    run_id: str,
    discharges: list[str],
    fork: str = "",
    fmt: int = 2,
    extra: str = "",
    name: str = "pm.bth.postmortem.toml",
) -> Path:
    path = tmp_path / name
    path.write_text(
        _PM.format(run_id=run_id, fmt=fmt, discharges=json.dumps(discharges), extra=fork + extra)
    )
    return path


def _validate(tmp_path: Path, cat: Path, path: Path) -> dict:
    return postmortem_validate_tool(
        path=str(path), workspace_root=str(tmp_path), catalog_dir=str(cat)
    )


def _oid(run: Run) -> str:
    return f"run_{run.id}_near_bound"


# ── query.lookup_run ─────────────────────────────────────────────────────────────────


def test_lookup_resolves_an_eight_char_prefix(cat, tmp_path):
    run = _run(cat, tmp_path, _campaign(cat, tmp_path, bound=False), run_id="a1b2c3d4-0001")
    found = lookup_run("a1b2c3d4", cat)
    assert found is not None and found.id == run.id


def test_lookup_errors_on_a_genuinely_ambiguous_prefix(cat, tmp_path):
    campaign = _campaign(cat, tmp_path, bound=False)
    _run(cat, tmp_path, campaign, run_id="a1b2c3d4-0001")
    _run(cat, tmp_path, campaign, run_id="a1b2c3d4-0002", name="other.bth.toml")
    with pytest.raises(AmbiguousRunId) as exc:
        lookup_run("a1b2c3d4", cat)
    assert exc.value.candidates == ["a1b2c3d4-0001", "a1b2c3d4-0002"]
    assert lookup_run("a1b2c3d4-0002", cat).id == "a1b2c3d4-0002"


def test_lookup_does_not_call_a_compacted_run_ambiguous(cat, tmp_path):
    """A compacted run is in the warm table AND its kept cool fragment: one run, not two."""
    run = _run(cat, tmp_path, _campaign(cat, tmp_path, bound=False), run_id="c0ffee00-0001")
    assert any(r.id == run.id for r in __import__("bathos.catalog").catalog.read_runs(cat))
    assert lookup_run("c0ffee00", cat).id == run.id


def test_warm_mode_resolves_a_cool_only_run_through_the_cool_object(cat, tmp_path):
    campaign = _campaign(cat, tmp_path, bound=False)
    run = _run(cat, tmp_path, campaign, run_id="feedbeef-0001", compacted=False)
    found = lookup_run("feedbeef", cat)
    assert found is not None and found.id == run.id
    assert json.loads(found.metadata)["_source"] == "cool_unindexed"


def test_prefix_wildcards_match_literally_and_empty_prefix_is_none(cat, tmp_path):
    _run(cat, tmp_path, _campaign(cat, tmp_path, bound=False), run_id="abc_def-0001")
    assert lookup_run("abc_", cat) is not None
    assert lookup_run("abc%", cat) is None
    assert lookup_run("a_c", cat) is None  # `_` is not a wildcard
    assert lookup_run("\\", cat) is None
    assert lookup_run("", cat) is None


def test_ambiguous_run_id_is_registered():
    from bathos.errors import EXCEPTION_TO_CODE

    assert EXCEPTION_TO_CODE["AmbiguousRunId"] is BathosErrorCode.RUN_PREFIX_AMBIGUOUS


# ── discharge through `bth postmortem validate` ──────────────────────────────────────


def test_validate_discharges_a_near_bound_obligation(cat, tmp_path):
    run = _run(cat, tmp_path, _campaign(cat, tmp_path))
    open_obligation(tmp_path, "run", run.id, "near_bound")
    result = _validate(tmp_path, cat, _write_pm(tmp_path, run.id[:8], [_oid(run)], _fork()))
    assert result["validation_ok"] is True
    assert result["discharged"] == [_oid(run)]
    (ob,) = list_obligations(tmp_path, open_only=False)
    assert not ob.is_open
    assert ob.discharged_by == "pm.bth.postmortem.toml"  # workspace-relative, POSIX
    assert ob.superseded_by_paths == []


def test_a_postmortem_for_run_x_cannot_discharge_run_y(cat, tmp_path):
    campaign = _campaign(cat, tmp_path)
    x = _run(cat, tmp_path, campaign)
    y = _run(cat, tmp_path, campaign, name="y.bth.toml")
    open_obligation(tmp_path, "run", y.id, "near_bound")
    result = _validate(tmp_path, cat, _write_pm(tmp_path, x.id, [_oid(y)], _fork()))
    assert result["validation_ok"] is True
    assert result["discharged"] == []
    assert _oid(y) in result["not_discharged"]
    assert list_obligations(tmp_path)[0].is_open


def test_a_campaign_scoped_postmortem_discharges_a_member_runs_obligation(cat, tmp_path):
    campaign = _campaign(cat, tmp_path)
    run = _run(cat, tmp_path, campaign)
    open_obligation(tmp_path, "run", run.id, "near_bound")
    path = tmp_path / "campaign.bth.postmortem.toml"
    path.write_text(
        f'campaign_id = "{campaign.id}"\nformat = 2\n[postmortem]\n'
        f'hypothesis_status = "inconclusive"\ndischarges = ["{_oid(run)}"]\n' + _fork()
    )
    result = _validate(tmp_path, cat, path)
    assert result["discharged"] == [_oid(run)]


def test_legacy_triggers_still_discharge_with_a_relative_path(cat, tmp_path):
    open_obligation(tmp_path, "run", "r-legacy", "outcome_failed")
    path = _write_pm(tmp_path, "r-legacy", ["run_r-legacy_outcome_failed"], fmt=1)
    result = _validate(tmp_path, cat, path)
    assert result["discharged"] == ["run_r-legacy_outcome_failed"]
    assert list_obligations(tmp_path, open_only=False)[0].discharged_by == path.name


def test_a_postmortem_outside_the_workspace_is_stored_absolute(cat, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    open_obligation(ws, "run", "r-legacy", "outcome_failed")
    path = _write_pm(tmp_path, "r-legacy", ["run_r-legacy_outcome_failed"], fmt=1)
    postmortem_validate_tool(path=str(path), workspace_root=str(ws), catalog_dir=str(cat))
    assert list_obligations(ws, open_only=False)[0].discharged_by == str(path)


def test_d5_errors_fire_through_the_tool_as_errors(cat, tmp_path):
    """With run_lookup wired, an unresolvable analysis_run_id is an error, not a warning."""
    run = _run(cat, tmp_path, _campaign(cat, tmp_path))
    amendment = (
        '[[amendments]]\nanalysis_run_id = "deadbeef"\nkind = "reanalysis"\n'
        'effect = "verdict_unchanged"\n'
    )
    path = _write_pm(tmp_path, run.id, [], extra=amendment)
    result = _validate(tmp_path, cat, path)
    assert result["validation_ok"] is False
    assert any("deadbeef" in e and "bth compact" in e for e in result["errors"])
    assert not any("no catalog" in w for w in result["warnings"])


def test_a_resolving_amendment_validates_and_get_wires_the_lookup_too(cat, tmp_path):
    campaign = _campaign(cat, tmp_path)
    run = _run(cat, tmp_path, campaign)
    other = _run(cat, tmp_path, campaign, name="analysis.bth.toml")
    amendment = (
        f'[[amendments]]\nanalysis_run_id = "{other.id[:8]}"\nkind = "reanalysis"\n'
        'effect = "verdict_unchanged"\n'
    )
    path = _write_pm(tmp_path, run.id, [], extra=amendment)
    assert _validate(tmp_path, cat, path)["validation_ok"] is True


# ── conclude: the claim-bound fork-record check ──────────────────────────────────────


def test_end_to_end_open_near_bound_blocks_conclude_until_a_fork_is_validated(cat, tmp_path):
    campaign = _campaign(cat, tmp_path)
    run = _run(cat, tmp_path, campaign)
    open_obligation(tmp_path, "run", run.id, "near_bound")
    with pytest.raises(BreakdownRefused) as exc:
        _conclude(cat, tmp_path, campaign)
    assert exc.value.code is BathosErrorCode.FORK_RECORD_MISSING
    assert "bth postmortem validate" in str(exc.value)

    cited = _fork(choice="replicate", reason="cited; replicating", cited="true")
    path = _write_pm(tmp_path, run.id, [_oid(run)], cited)
    assert _validate(tmp_path, cat, path)["discharged"] == [_oid(run)]
    _conclude(cat, tmp_path, campaign)  # proceeds
    db = _db(cat)
    try:
        assert db.execute(
            "SELECT status FROM campaigns WHERE id = ?", [campaign.id]
        ).fetchone() == ("concluded",)
    finally:
        db.close()


def test_conclude_recomputes_and_opens_when_the_run_time_open_never_happened(cat, tmp_path):
    """Opt-out (`near_bound = false`), a swallowed failure or a pre-feature run: the ledger
    has no entry, yet conclude raises, opening it now."""
    (tmp_path / ".bth.toml").write_text(
        f'[project]\nslug = "p"\nroot = "{tmp_path}"\n[obligations]\nnear_bound = false\n'
    )
    campaign = _campaign(cat, tmp_path)
    run = _run(cat, tmp_path, campaign)
    assert list_obligations(tmp_path) == []
    with pytest.raises(BreakdownRefused) as exc:
        _conclude(cat, tmp_path, campaign)
    assert exc.value.code is BathosErrorCode.FORK_RECORD_MISSING
    assert [o.obligation_id for o in list_obligations(tmp_path)] == [_oid(run)]


def test_force_verdict_does_not_bypass_the_fork_check(cat, tmp_path):
    campaign = _campaign(cat, tmp_path)
    _run(cat, tmp_path, campaign)
    with pytest.raises(BreakdownRefused):
        _conclude(cat, tmp_path, campaign, force_verdict=True)


def test_a_run_not_near_the_bound_concludes(cat, tmp_path):
    campaign = _campaign(cat, tmp_path)
    far = {**_NEAR, "e_max_placement": 0.05, "e_max_placement__n_over": 0}
    _run(cat, tmp_path, campaign, results=far)
    _conclude(cat, tmp_path, campaign)


def test_a_moved_or_deleted_discharging_postmortem_raises(cat, tmp_path):
    campaign = _campaign(cat, tmp_path)
    run = _run(cat, tmp_path, campaign)
    open_obligation(tmp_path, "run", run.id, "near_bound")
    path = _write_pm(tmp_path, run.id, [_oid(run)], _fork())
    _validate(tmp_path, cat, path)
    path.unlink()
    with pytest.raises(BreakdownRefused) as exc:
        _conclude(cat, tmp_path, campaign)
    assert exc.value.code is BathosErrorCode.FORK_RECORD_MISSING
    assert "pm.bth.postmortem.toml" in str(exc.value)


def test_a_file_edited_after_discharge_is_caught_at_conclude(cat, tmp_path):
    campaign = _campaign(cat, tmp_path)
    run = _run(cat, tmp_path, campaign)
    open_obligation(tmp_path, "run", run.id, "near_bound")
    cited = _fork(choice="replicate", reason="internal validation only", cited="true")
    path = _write_pm(tmp_path, run.id, [_oid(run)], cited)
    _validate(tmp_path, cat, path)
    path.write_text(path.read_text().replace('reason = "internal validation only"', 'reason = ""'))
    with pytest.raises(BreakdownRefused):
        _conclude(cat, tmp_path, campaign)


def test_cited_false_raises_then_a_cited_true_postmortem_re_points_the_discharge(cat, tmp_path):
    campaign = _campaign(cat, tmp_path)
    run = _run(cat, tmp_path, campaign)
    open_obligation(tmp_path, "run", run.id, "near_bound")
    internal = _write_pm(
        tmp_path, run.id, [_oid(run)], _fork(), name="internal.bth.postmortem.toml"
    )
    assert _validate(tmp_path, cat, internal)["discharged"] == [_oid(run)]
    with pytest.raises(BreakdownRefused) as exc:
        _conclude(cat, tmp_path, campaign)
    assert exc.value.code is BathosErrorCode.FORK_RECORD_MISSING
    assert "internal.bth.postmortem.toml" in str(exc.value)

    cited = _write_pm(
        tmp_path,
        run.id,
        [_oid(run)],
        _fork(choice="replicate", reason="cited; replicating", cited="true"),
        name="cited.bth.postmortem.toml",
    )
    assert _validate(tmp_path, cat, cited)["discharged"] == [_oid(run)]
    (ob,) = list_obligations(tmp_path, open_only=False)
    assert ob.discharged_by == "cited.bth.postmortem.toml"
    assert ob.superseded_by_paths == ["internal.bth.postmortem.toml"]
    _conclude(cat, tmp_path, campaign)  # proceeds


def test_a_cool_only_member_is_checked(cat, tmp_path):
    campaign = _campaign(cat, tmp_path)
    run = _run(cat, tmp_path, campaign, compacted=False)
    open_obligation(tmp_path, "run", run.id, "near_bound")
    db = _db(cat)
    try:
        # membership exists only as a stamp on the cool fragment
        assert db.execute("SELECT COUNT(*) FROM runs WHERE id = ?", [run.id]).fetchone() == (0,)
    finally:
        db.close()
    with pytest.raises(BreakdownRefused) as exc:
        _conclude(cat, tmp_path, campaign)
    assert exc.value.code is BathosErrorCode.FORK_RECORD_MISSING


def test_exploration_mode_conclude_warns_and_proceeds(cat, tmp_path, capsys):
    campaign = _campaign(cat, tmp_path, mode="exploration")
    _run(cat, tmp_path, campaign)
    _conclude(cat, tmp_path, campaign)
    assert "near_bound" in capsys.readouterr().out


def test_conclude_reads_the_cool_tier_once_for_the_gate(cat, tmp_path, monkeypatch):
    """Count `read_runs` calls: from the membership union to the end of the gate (fork check
    included) the cool tier is scanned zero further times, and one list serves both users."""
    import bathos.breakdown_gate as gate
    import bathos.campaigns as campaigns_module
    import bathos.catalog as catalog_module

    campaign = _campaign(cat, tmp_path)
    far = {**_NEAR, "e_max_placement": 0.05, "e_max_placement__n_over": 0}
    _run(cat, tmp_path, campaign, results=far)
    scans = {"n": 0}
    real_read = catalog_module.read_runs

    def counting_read(*args, **kwargs):
        scans["n"] += 1
        return real_read(*args, **kwargs)

    seen: dict = {"union": [], "fetch": [], "at_union": None, "at_gate_end": None}
    real_union = campaigns_module.union_campaign_member_ids
    real_fetch = gate.fetch_member_runs
    real_near = gate.check_near_bound

    def spy_union(db, cid, catalog_dir, *, cool_runs=None):
        seen["union"].append(cool_runs)
        if cool_runs is not None:
            seen["at_union"] = scans["n"]
        return real_union(db, cid, catalog_dir, cool_runs=cool_runs)

    def spy_fetch(db, ids, catalog_dir, **kw):
        seen["fetch"].append(kw.get("cool_runs"))
        return real_fetch(db, ids, catalog_dir, **kw)

    def spy_near(*args, **kwargs):
        try:
            return real_near(*args, **kwargs)
        finally:
            seen["at_gate_end"] = scans["n"]

    monkeypatch.setattr(catalog_module, "read_runs", counting_read)
    monkeypatch.setattr(campaigns_module, "union_campaign_member_ids", spy_union)
    monkeypatch.setattr(gate, "fetch_member_runs", spy_fetch)
    monkeypatch.setattr(gate, "check_near_bound", spy_near)
    _conclude(cat, tmp_path, campaign)
    gate_union = [c for c in seen["union"] if c is not None]
    assert len(gate_union) == 1 and len(seen["fetch"]) == 1
    assert seen["fetch"][0] is gate_union[0]  # one list, passed to both
    assert seen["at_union"] is not None and seen["at_gate_end"] is not None
    assert seen["at_gate_end"] == seen["at_union"]  # no scan between the union and the gate end
    assert seen["at_union"] >= 1  # and the one scan really happened through read_runs


# ── scaffold resolves a prefix to the full id ────────────────────────────────────────


def test_postmortem_scaffold_resolves_the_full_id_for_the_checklist(cat, tmp_path, monkeypatch):
    from bathos.mcp import postmortem_scaffold_tool

    monkeypatch.setenv("BTH_CATALOG_DIR", str(cat))
    run = _run(cat, tmp_path, _campaign(cat, tmp_path))
    open_obligation(tmp_path, "run", run.id, "near_bound")
    result = postmortem_scaffold_tool(run_id=run.id[:8], workspace_root=str(tmp_path))
    assert "path" in result, result
    path = Path(result["path"])
    text = path.read_text()
    assert f'run_id = "{run.id}"' in text
    assert f"#   {_oid(run)}" in text
    assert "discharges = []" in text


# ── log mode: lookup after a one-shot ingest ─────────────────────────────────────────


def test_log_mode_lookup_folds_once_and_no_ingest_names_compact(tmp_path, monkeypatch):
    from bathos.runlog.mode import cutover_marker_path
    from bathos.runlog.project_id import assign_project_id, register_main_root
    from bathos.runlog.writer import reset_writers_for_test

    # build the project by hand (the fixture lives in a runlog-scoped conftest)
    from tests.runlog.conftest import make_git_repo, write_bth_toml
    from tests.runlog.test_breakdown_gate_log_mode import _COVERED, _SUMMARY

    ws = make_git_repo(tmp_path / "ws")
    write_bth_toml(ws, slug="proj")
    assign_project_id(ws)
    register_main_root(ws)
    (ws / ".gitignore").write_text("/.bth/log/\n")
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    cutover_marker_path(catalog).write_text(
        json.dumps({"at": "2020-01-01T00:00:00Z", "bathos": "t", "attempt": "t", "segments": []})
    )
    monkeypatch.chdir(ws)
    monkeypatch.delenv("BTH_PROJECT_ID", raising=False)
    try:
        monkeypatch.setenv("BTH_NO_INGEST", "1")  # the run's events are written but not folded
        run_id = _real_run_no_ingest(ws, catalog, _COVERED, {"e_max_placement": 0.2, **_SUMMARY})
        assert lookup_run(run_id[:8], catalog) is None  # no-op ingest: stays unresolved

        # validate names the remedy
        amendment = (
            f'[[amendments]]\nanalysis_run_id = "{run_id[:8]}"\nkind = "reanalysis"\n'
            'effect = "verdict_unchanged"\n'
        )
        path = _write_pm(tmp_path, "other-run", [], extra=amendment)
        result = postmortem_validate_tool(
            path=str(path), workspace_root=str(ws), catalog_dir=str(catalog)
        )
        assert result["validation_ok"] is False
        assert any("bth compact" in e for e in result["errors"])

        monkeypatch.delenv("BTH_NO_INGEST")  # now a miss folds once and resolves
        state: dict = {}
        found = lookup_run(run_id[:8], catalog, ingest_state=state)
        assert found is not None and found.id == run_id
        assert state == {"done": True}
    finally:
        reset_writers_for_test()


def _real_run_no_ingest(ws: Path, catalog: Path, sidecar: str, result: dict) -> str:
    """A real `bth run` whose run.started/run.finished are written but not folded."""
    from bathos.catalog import read_runs
    from bathos.runner import run_script

    scripts = ws / "scripts" / "explore"
    scripts.mkdir(parents=True, exist_ok=True)
    (scripts / "probe.py").write_text(
        "import json, os, sys\np = os.environ.get('BTH_RESULTS_PATH')\n"
        "p and json.dump(json.loads(sys.argv[1]), open(p, 'w'))\n"
    )
    (scripts / "probe.bth.toml").write_text(sidecar)
    rc = run_script(
        argv=[sys.executable, str(scripts / "probe.py"), json.dumps(result)],
        project_slug="proj",
        catalog_dir=catalog,
        output_paths=[],
        tags=[],
        cwd=ws,
    )
    assert rc == 0
    # the id is in the events only (log mode writes no fragment)
    from bathos.runlog.writer import reset_writers_for_test  # noqa: F401

    assert read_runs(catalog) == []
    segs = sorted((ws / ".bth" / "log").rglob("*.jsonl"))
    for seg in segs:
        for line in seg.read_text().splitlines():
            event = json.loads(line)
            if event.get("kind") == "run.started":
                return event["entity"][0]
    raise AssertionError("no run.started event written")
