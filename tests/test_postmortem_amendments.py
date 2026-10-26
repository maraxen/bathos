"""Distributional breakdown, T6 (spec 261026 D5): postmortem `[[amendments]]` (format 2).

Errors versus warnings, the effective label (an amendment never alters `runs.outcome`; only the
existing `verdict_override` does), and rendering (`postmortem show` / `postmortem_get` and the
campaign review show amendments regardless of drift, with a banner).
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import duckdb
from rich.console import Console

from bathos.postmortem import (
    AMENDMENTS_BANNER,
    parse_postmortem,
    removed_amendments,
    validate_postmortem,
)

_FULL = {
    "797b791e": "797b791e-aaaa-bbbb-cccc-000000000001",
    "abcdef12": "abcdef12-0000-0000-0000-1",
}


def _lookup(run_id: str):
    for short, full in _FULL.items():
        if full.startswith(run_id):
            return SimpleNamespace(id=full)
    return None


_HEADER = 'run_id = "abcdef12"\nformat = 2\n'
_PM = """
[postmortem]
hypothesis_status = "inconclusive"
verdict_override = "{override}"
summary = "s"
author = "me"
status = "submitted"
"""


def _amendment(**over) -> str:
    fields = {
        "analysis_run_id": '"797b791e"',
        "kind": '"distributional_outlier"',
        "outlier_units": '["binder_17"]',
        "effect": '"verdict_contextualized"',
        "note": '"1 of 24 binders over; median binder max 0.004 A"',
    }
    fields.update({k: v for k, v in over.items()})
    body = "\n".join(f"{k} = {v}" for k, v in fields.items() if v is not None)
    return f"\n[[amendments]]\n{body}\n"


def _pm(tmp_path: Path, amendments: str, *, header: str = _HEADER, override: str = "none"):
    path = tmp_path / "pm.bth.postmortem.toml"
    path.write_text(header + _PM.format(override=override) + amendments)
    return path, parse_postmortem(path)


def _validate(tmp_path, amendments, *, lookup=_lookup, **kw):
    _, pm = _pm(tmp_path, amendments, **kw)
    return validate_postmortem(pm, workspace_root=tmp_path, run_lookup=lookup)


def _messages(result) -> list[str]:
    return [e.message for e in result.errors]


# ── errors ───────────────────────────────────────────────────────────────────────────


def test_a_valid_amendment_passes_cleanly(tmp_path):
    result = _validate(tmp_path, _amendment())
    assert result.ok, _messages(result)
    assert result.warnings == []


def test_unresolvable_analysis_run_is_an_error_naming_the_compact_remedy(tmp_path):
    result = _validate(tmp_path, _amendment(analysis_run_id='"deadbeef"'))
    assert not result.ok
    (message,) = _messages(result)
    assert "deadbeef" in message and "bth compact" in message


def test_analysis_run_equal_to_the_amended_run_is_an_error(tmp_path):
    same = _validate(tmp_path, _amendment(analysis_run_id='"abcdef12"'))
    assert any("equal" in m for m in _messages(same))
    # a prefix and the full id are the same run once resolved
    resolved = _validate(
        tmp_path, _amendment(analysis_run_id='"abcdef12-0000"', amends_run_id='"abcdef12"')
    )
    assert any("equal" in m for m in _messages(resolved))


def test_outlier_kind_needs_outlier_units(tmp_path):
    result = _validate(tmp_path, _amendment(outlier_units="[]"))
    assert any("outlier_units" in m for m in _messages(result))
    ok = _validate(tmp_path, _amendment(kind='"reanalysis"', outlier_units="[]"))
    assert ok.ok, _messages(ok)


def test_verdict_changed_requires_a_verdict_override(tmp_path):
    amendment = _amendment(effect='"verdict_changed"')
    refused = _validate(tmp_path, amendment, override="none")
    assert any("verdict_override" in m for m in _messages(refused))
    allowed = _validate(tmp_path, amendment, override="fail")
    assert allowed.ok, _messages(allowed)


def test_campaign_scoped_postmortem_requires_amends_run_id(tmp_path):
    header = 'campaign_id = "camp-1"\nformat = 2\n'
    missing = _validate(tmp_path, _amendment(), header=header)
    assert any("amends_run_id" in m for m in _messages(missing))
    named = _validate(tmp_path, _amendment(amends_run_id='"abcdef12"'), header=header)
    assert named.ok, _messages(named)


def test_vocabulary_and_required_ids_are_checked(tmp_path):
    result = _validate(
        tmp_path, _amendment(kind='"vibes"', effect='"vanished"', analysis_run_id=None)
    )
    messages = " | ".join(_messages(result))
    assert "kind" in messages and "effect" in messages and "analysis_run_id" in messages


def test_a_lookup_that_raises_is_reported_as_an_error_naming_the_cause(tmp_path):
    def ambiguous(_run_id):
        raise LookupError("797b matches 797b791e-aaaa and 797b0000-bbbb")

    result = _validate(tmp_path, _amendment(), lookup=ambiguous)
    assert any("797b0000-bbbb" in m for m in _messages(result))


# ── warnings ─────────────────────────────────────────────────────────────────────────


def test_without_a_catalog_run_checks_become_warnings_not_errors(tmp_path):
    result = _validate(tmp_path, _amendment(analysis_run_id='"deadbeef"'), lookup=None)
    assert result.ok
    assert any("skipped" in w and "no catalog" in w for w in result.warnings)


def test_run_independent_errors_still_fire_without_a_catalog(tmp_path):
    result = _validate(tmp_path, _amendment(outlier_units="[]"), lookup=None)
    assert not result.ok


def test_removed_amendments_are_reported_against_the_previous_record(tmp_path):
    previous = [
        {"analysis_run_id": "797b791e", "kind": "distributional_outlier"},
        {"analysis_run_id": "11112222", "kind": "reanalysis"},
    ]
    _, pm = _pm(tmp_path, _amendment())
    assert removed_amendments(previous, pm) == ["11112222 (reanalysis)"]
    assert removed_amendments([], pm) == []


# ── the effective label: amendments never alter runs.outcome ─────────────────────────


def _compact_outcome(tmp_catalog: Path, tmp_path: Path, monkeypatch, pm_text: str) -> str:
    from bathos.catalog import init_catalog, write_run
    from bathos.compact import compact
    from bathos.schema import Run

    ws = tmp_path / "ws"
    exp = ws / "scripts" / "experiments"
    exp.mkdir(parents=True)
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(ws))
    init_catalog(tmp_catalog)
    run = Run(
        project_slug="p",
        command="python scripts/experiments/probe.py",
        argv=["python", "scripts/experiments/probe.py"],
        git_hash="deadbeef",
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        outcome="pass",
    )
    write_run(run, tmp_catalog)
    (exp / f"probe.py.{run.id}.bth.postmortem.toml").write_text(pm_text.format(run_id=run.id))
    compact(tmp_catalog)
    con = duckdb.connect(str(tmp_catalog / "bathos.db"), read_only=True)
    try:
        return con.execute("SELECT outcome FROM runs WHERE id = ?", [run.id]).fetchone()[0]
    finally:
        con.close()


def test_an_amendment_does_not_change_the_recorded_outcome(tmp_catalog, tmp_path, monkeypatch):
    text = 'run_id = "{run_id}"\nformat = 2\n' + _PM.format(override="none") + _amendment()
    assert _compact_outcome(tmp_catalog, tmp_path, monkeypatch, text) == "pass"


def test_only_the_existing_override_changes_the_outcome(tmp_catalog, tmp_path, monkeypatch):
    text = (
        'run_id = "{run_id}"\nformat = 2\n'
        + _PM.format(override="fail")
        + _amendment(effect='"verdict_changed"')
    )
    assert _compact_outcome(tmp_catalog, tmp_path, monkeypatch, text) == "fail"


# ── rendering ────────────────────────────────────────────────────────────────────────


def _workspace_with_postmortem(tmp_path: Path, amendments: str, override="none") -> Path:
    ws = tmp_path / "ws"
    ws.mkdir(exist_ok=True)
    (ws / "probe.py.abcdef12.bth.postmortem.toml").write_text(
        _HEADER + _PM.format(override=override) + amendments
    )
    return ws


def test_postmortem_get_renders_amendments_with_a_banner(tmp_path, tmp_catalog, monkeypatch):
    from bathos.mcp import postmortem_get_tool

    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_catalog))
    ws = _workspace_with_postmortem(tmp_path, _amendment())
    result = postmortem_get_tool(run_id="abcdef12", workspace_root=str(ws))
    assert result["format"] == 2
    (amendment,) = result["amendments"]
    assert amendment["analysis_run_id"] == "797b791e"
    assert amendment["outlier_units"] == ["binder_17"]
    assert amendment["effect"] == "verdict_contextualized"
    assert result["amendments_banner"] == AMENDMENTS_BANNER


def test_amendments_show_even_when_validation_fails_on_drift(tmp_path, tmp_catalog, monkeypatch):
    """A failed validation (here refuted + pass; drift fails the same way) still renders them."""
    from bathos.mcp import postmortem_get_tool

    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_catalog))
    ws = _workspace_with_postmortem(tmp_path, _amendment(), override="pass")
    text = (ws / "probe.py.abcdef12.bth.postmortem.toml").read_text()
    (ws / "probe.py.abcdef12.bth.postmortem.toml").write_text(
        text.replace("inconclusive", "refuted")
    )
    result = postmortem_get_tool(run_id="abcdef12", workspace_root=str(ws))
    assert "Validation failed" in result["error"]
    assert result["amendments"][0]["analysis_run_id"] == "797b791e"
    assert result["amendments_banner"] == AMENDMENTS_BANNER


def test_postmortem_get_without_amendments_adds_no_banner(tmp_path, tmp_catalog, monkeypatch):
    from bathos.mcp import postmortem_get_tool

    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_catalog))
    ws = _workspace_with_postmortem(tmp_path, "")
    result = postmortem_get_tool(run_id="abcdef12", workspace_root=str(ws))
    assert result["amendments"] == []
    assert result["amendments_banner"] is None


def test_postmortem_validate_tool_returns_warnings(tmp_path):
    from bathos.mcp import postmortem_validate_tool

    path, _ = _pm(tmp_path, _amendment(), header='run_id = "abcdef12"\n')  # format 1
    result = postmortem_validate_tool(path=str(path), workspace_root=str(tmp_path))
    assert result["validation_ok"] is True
    assert any("not enforced" in w for w in result["warnings"])


def test_campaign_review_lists_amendments_and_the_renderer_shows_them(tmp_catalog, tmp_path):
    from bathos.campaigns import create_campaign, review_campaign
    from bathos.catalog import init_catalog, write_run
    from bathos.compact import compact
    from bathos.rich_fmt import render_campaign_review
    from bathos.schema import Run

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
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    campaign = create_campaign(db, name="rev", project_slug="p", mode="exploration")
    db.close()
    run = Run(
        project_slug="p",
        command="probe",
        argv=["probe"],
        git_hash="a",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime.fromisoformat(campaign.started_at) + timedelta(minutes=1),
        status="completed",
        exit_code=0,
        outcome="pass",
        campaign_id=campaign.id,
    )
    write_run(run, tmp_catalog)
    compact(tmp_catalog)
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / f"probe.py.{run.id}.bth.postmortem.toml").write_text(
        f'run_id = "{run.id}"\nformat = 2\n' + _PM.format(override="none") + _amendment()
    )
    db = duckdb.connect(str(tmp_catalog / "bathos.db"), read_only=True)
    try:
        review = review_campaign(db, campaign.id, tmp_catalog, workspace_root=ws)
        campaign_row = SimpleNamespace(name="rev", mode="exploration")
    finally:
        db.close()
    (entry,) = review["amendments"]
    assert entry["run_id"] == run.id and entry["analysis_run_id"] == "797b791e"
    out = io.StringIO()
    render_campaign_review(campaign_row, review, console=Console(file=out, width=120))
    assert "Amendments" in out.getvalue() and "797b791e" in out.getvalue()
    # no workspace: the key is simply empty
    db = duckdb.connect(str(tmp_catalog / "bathos.db"), read_only=True)
    try:
        assert review_campaign(db, campaign.id, tmp_catalog)["amendments"] == []
    finally:
        db.close()


# ── log mode: amendment_removed ──────────────────────────────────────────────────────


def test_log_mode_validate_warns_when_a_recorded_amendment_disappears(tmp_path, monkeypatch):
    import json

    from bathos.mcp import postmortem_validate_tool
    from bathos.runlog.mode import cutover_marker_path
    from bathos.runlog.project_id import assign_project_id, register_main_root
    from bathos.runlog.writer import reset_writers_for_test
    from tests.runlog.conftest import make_git_repo, write_bth_toml

    ws = make_git_repo(tmp_path / "ws")
    write_bth_toml(ws, slug="proj")
    assign_project_id(ws)
    register_main_root(ws)
    (ws / ".gitignore").write_text("/.bth/log/\n")
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    cutover_marker_path(catalog_dir).write_text(
        json.dumps({"at": "2020-01-01T00:00:00Z", "bathos": "t", "attempt": "t", "segments": []})
    )
    monkeypatch.chdir(ws)
    monkeypatch.delenv("BTH_PROJECT_ID", raising=False)
    # the amendment's analysis run is not in this empty catalog; resolving it is T7a's concern
    monkeypatch.setattr(
        "bathos.mcp._postmortem_run_lookup", lambda _cat: lambda rid: SimpleNamespace(id=rid)
    )
    try:
        path = ws / "pm.bth.postmortem.toml"
        path.write_text(_HEADER + _PM.format(override="none") + _amendment())
        first = postmortem_validate_tool(
            path=str(path), workspace_root=str(ws), catalog_dir=str(catalog_dir)
        )
        assert first["validation_ok"] is True
        assert not any("amendment_removed" in w for w in first["warnings"])

        path.write_text(_HEADER + _PM.format(override="none"))  # the amendment is dropped
        second = postmortem_validate_tool(
            path=str(path), workspace_root=str(ws), catalog_dir=str(catalog_dir)
        )
        assert second["validation_ok"] is True
        assert any("amendment_removed" in w and "797b791e" in w for w in second["warnings"])
    finally:
        reset_writers_for_test()


def test_amendment_removed_is_not_reported_outside_log_mode(tmp_path, tmp_catalog):
    from bathos.mcp import postmortem_validate_tool

    path, _ = _pm(tmp_path, _amendment())
    postmortem_validate_tool(
        path=str(path), workspace_root=str(tmp_path), catalog_dir=str(tmp_catalog)
    )
    path.write_text(_HEADER + _PM.format(override="none"))
    result = postmortem_validate_tool(
        path=str(path), workspace_root=str(tmp_path), catalog_dir=str(tmp_catalog)
    )
    assert not any("amendment_removed" in w for w in result["warnings"])
