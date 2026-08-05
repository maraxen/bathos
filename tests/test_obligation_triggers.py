"""Wiring of the §5 obligation triggers, each behind its own opt-in flag.

The load-bearing property is that **every flag defaults off**: with none set, no call site
writes to the ledger and no verdict changes. Failed attempt b1 established why — a gate that
fires on every pre-existing entity is a retroactive verdict change, not a gate.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from bathos.catalog import init_catalog, write_run
from bathos.compact import compact
from bathos.obligations import TRIGGERS, WIRED_TRIGGERS, list_obligations, trigger_enabled
from bathos.schema import Run
from bathos.sidecar import (
    OutcomeSpec,
    Sidecar,
    SidecarKind,
    derive_pass_labels,
    is_failure_outcome,
)


def _sidecar(**outcomes: OutcomeSpec) -> Sidecar:
    return Sidecar(
        kind=SidecarKind.EXPERIMENT, result_schema={"x": "float"}, outcomes=dict(outcomes)
    )


PASS = OutcomeSpec(condition="x < 5", decision="go")
FAIL = OutcomeSpec(condition="x >= 5", decision="stop", is_residual=True)


# ── the failure predicate (trigger 1's definition) ──────────────────────────


def test_pass_labels_exclude_residual_and_bookkeeping_labels():
    sc = _sidecar(
        pass_=PASS,
        marginal=OutcomeSpec(condition="c", decision="d"),
        catchall=FAIL,
    )
    assert derive_pass_labels(sc) == {"pass_"}


def test_a_passing_outcome_is_not_a_failure():
    """Polarity guard. An inverted predicate would open an obligation on every healthy run —
    the single most damaging way this could be wrong, and silent if untested."""
    sc = _sidecar(**{"pass": PASS, "fail": FAIL})
    assert is_failure_outcome(sc, "pass") is False


@pytest.mark.parametrize("label", ["fail", "marginal", "error"])
def test_non_pass_labels_are_failures(label):
    sc = _sidecar(**{"pass": PASS, "fail": FAIL})
    assert is_failure_outcome(sc, label) is True


def test_a_non_pass_label_is_caught_without_the_word_fail():
    """Outcome labels have no canonical set, so a literal "fail" match would miss these."""
    sc = _sidecar(
        **{
            "converged": PASS,
            "unstable": OutcomeSpec(condition="c", decision="d", is_residual=True),
        }
    )
    assert is_failure_outcome(sc, "unstable") is True
    assert is_failure_outcome(sc, "converged") is False


@pytest.mark.parametrize("label", ["", "unknown"])
def test_an_unevaluated_outcome_is_not_a_failure(label):
    """No outcome computed is a different thing from an evaluated non-pass."""
    sc = _sidecar(**{"pass": PASS, "fail": FAIL})
    assert is_failure_outcome(sc, label) is False


def test_no_sidecar_is_not_a_failure():
    assert is_failure_outcome(None, "fail") is False


# ── flag plumbing ───────────────────────────────────────────────────────────


def test_every_trigger_defaults_off(monkeypatch):
    for trig in TRIGGERS:
        monkeypatch.delenv(f"BTH_OBLIGATION_{trig.upper()}", raising=False)
        assert trigger_enabled(trig) is False


@pytest.mark.parametrize("trig", sorted(TRIGGERS))
def test_each_flag_enables_only_its_own_trigger(monkeypatch, trig):
    """Independently toggleable: the four differ sharply in blast radius, so enabling the
    safest must not enable the widest."""
    for other in TRIGGERS:
        monkeypatch.delenv(f"BTH_OBLIGATION_{other.upper()}", raising=False)
    monkeypatch.setenv(f"BTH_OBLIGATION_{trig.upper()}", "1")

    assert trigger_enabled(trig) is True
    assert [t for t in TRIGGERS if trigger_enabled(t)] == [trig]


def test_unknown_trigger_is_never_enabled(monkeypatch):
    monkeypatch.setenv("BTH_OBLIGATION_NONSENSE", "1")
    assert trigger_enabled("nonsense") is False


def test_maybe_open_is_a_noop_while_disabled(monkeypatch, tmp_path):
    from bathos.obligations import maybe_open

    monkeypatch.delenv("BTH_OBLIGATION_OUTCOME_FAILED", raising=False)
    assert maybe_open(tmp_path, "run", "r1", "outcome_failed") is None
    assert list_obligations(tmp_path) == []

    monkeypatch.setenv("BTH_OBLIGATION_OUTCOME_FAILED", "1")
    assert maybe_open(tmp_path, "run", "r1", "outcome_failed") is not None
    assert len(list_obligations(tmp_path)) == 1


def test_all_four_triggers_are_wired():
    """Trigger 3's polarity is settled (stricter conjunct → fires when FALSE), so the
    unwired-by-design carve-out is gone."""
    assert WIRED_TRIGGERS == TRIGGERS

    import bathos

    src = Path(bathos.__file__).parent
    call_sites = {
        p.name
        for p in src.rglob("*.py")
        if "adversarial_check_fired" in p.read_text() and p.name != "obligations.py"
    }
    assert "runner.py" in call_sites


# ── conclude-time gate (triggers 2 and 4, and the D1 downgrade) ─────────────


def _catalog_with_campaign(tmp_path: Path, mode: str = "confirmation"):
    from bathos.campaigns import create_campaign

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    init_catalog(catalog_dir)
    write_run(
        Run(
            project_slug="p",
            command="python s.py",
            argv=["python", "s.py"],
            git_hash="a",
            git_branch="main",
            git_dirty=False,
        ),
        catalog_dir,
    )
    compact(catalog_dir)
    db = duckdb.connect(str(catalog_dir / "bathos.db"))
    campaign = create_campaign(db, "c", "p", mode)
    db.commit()
    return catalog_dir, db, campaign


def test_conclude_opens_no_obligation_while_flags_are_off(tmp_path, monkeypatch):
    from bathos.campaigns import conclude_campaign

    for trig in TRIGGERS:
        monkeypatch.delenv(f"BTH_OBLIGATION_{trig.upper()}", raising=False)
    monkeypatch.delenv("BTH_OBLIGATION_ENFORCE", raising=False)

    _cat, db, campaign = _catalog_with_campaign(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    conclude_campaign(db, campaign.id, "confounded", "note", workspace_root=ws)

    assert list_obligations(ws) == []
    row = db.execute("SELECT outcome_label FROM campaigns WHERE id=?", [campaign.id]).fetchone()
    assert row[0] == "confounded"  # the researcher's own label, untouched


def test_trigger_2_opens_when_a_campaign_concludes_confounded(tmp_path, monkeypatch):
    from bathos.campaigns import conclude_campaign

    monkeypatch.setenv("BTH_OBLIGATION_CAMPAIGN_CONFOUNDED", "1")
    _cat, db, campaign = _catalog_with_campaign(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()

    conclude_campaign(db, campaign.id, "confounded", "the arms were not comparable", ws)

    obs = list_obligations(ws)
    assert [(o.entity_kind, o.trigger) for o in obs] == [("campaign", "campaign_confounded")]
    assert obs[0].entity_id == campaign.id
    assert "not comparable" in obs[0].detail


def test_trigger_2_does_not_open_on_a_clean_conclude(tmp_path, monkeypatch):
    from bathos.campaigns import conclude_campaign

    monkeypatch.setenv("BTH_OBLIGATION_CAMPAIGN_CONFOUNDED", "1")
    _cat, db, campaign = _catalog_with_campaign(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()

    conclude_campaign(db, campaign.id, "pass", "clean", ws)
    assert list_obligations(ws) == []


def test_conclude_gate_is_advisory_until_the_enforce_flag_is_set(tmp_path, monkeypatch, capsys):
    """Mirrors BTH_REVIEW_COVERAGE_ENFORCE: the check always reports, only the verdict change
    is opt-in. Enforcing by default would let a newly-enabled trigger retroactively downgrade
    an unrelated campaign."""
    from bathos.campaigns import conclude_campaign
    from bathos.obligations import open_obligation

    monkeypatch.delenv("BTH_OBLIGATION_ENFORCE", raising=False)
    _cat, db, campaign = _catalog_with_campaign(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    open_obligation(ws, "campaign", campaign.id, "outcome_failed", detail="temp_std 12")

    conclude_campaign(db, campaign.id, "pass", "note", ws)

    out = capsys.readouterr().out
    assert "Open obligation:" in out
    assert "advisory until BTH_OBLIGATION_ENFORCE=1" in out
    row = db.execute("SELECT outcome_label FROM campaigns WHERE id=?", [campaign.id]).fetchone()
    assert row[0] == "pass", "advisory mode must not change the verdict"


def test_conclude_gate_downgrades_under_the_enforce_flag(tmp_path, monkeypatch):
    from bathos.campaigns import conclude_campaign
    from bathos.obligations import open_obligation

    monkeypatch.setenv("BTH_OBLIGATION_ENFORCE", "1")
    _cat, db, campaign = _catalog_with_campaign(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()
    open_obligation(ws, "campaign", campaign.id, "outcome_failed")

    conclude_campaign(db, campaign.id, "pass", "note", ws)

    row = db.execute("SELECT outcome_label FROM campaigns WHERE id=?", [campaign.id]).fetchone()
    assert row[0] == "confounded"


def test_the_gate_binds_on_member_run_obligations_too(tmp_path, monkeypatch):
    """§5: 'open obligations on member runs downgrade the campaign verdict'."""
    from bathos.campaigns import add_run_to_campaign, conclude_campaign
    from bathos.obligations import open_obligation

    monkeypatch.setenv("BTH_OBLIGATION_ENFORCE", "1")
    catalog_dir, db, campaign = _catalog_with_campaign(tmp_path, mode="exploration")
    run_id = db.execute("SELECT id FROM runs LIMIT 1").fetchone()[0]
    add_run_to_campaign(db, campaign.id, run_id)
    db.commit()

    ws = tmp_path / "ws"
    ws.mkdir()
    open_obligation(ws, "run", run_id, "outcome_failed")
    open_obligation(ws, "run", "some-unrelated-run", "outcome_failed")

    conclude_campaign(db, campaign.id, "pass", "note", ws)

    # exploration mode warns rather than downgrading, but the member-run obligation must be
    # the one found -- the unrelated run's must never enter the campaign's scope.
    from bathos.obligations import list_obligations_for_scope

    scoped = list_obligations_for_scope(ws, {campaign.id, run_id})
    assert [o.entity_id for o in scoped] == [run_id]


# ── trigger 4, end to end through conclude ─────────────────────────────────

_CLAIM = """
[claim]
headline = "h"
kill_condition = "k"

[[hypotheses]]
id = "H1"
label = "primary"

[[hypotheses]]
id = "H2"
label = "rival"

[[claim.discriminability]]
hypothesis_a = "H1"
hypothesis_b = "H2"
planned_run_label = "fail"
predicted_outcome = "H2"

[claim.union_gate]
"""

_REVIEW_SIDECAR = """
[experiment]
hypothesis = "h"

[[review.literature]]
ref = "10.1/prior"
claim = "prior work says H1 holds"
bears_on = "H1"
disposition = "supports"

[outcomes.pass]
condition = "x < 5"
decision = "go"
reasoning = "r"

[outcomes.fail]
condition = "x >= 5"
decision = "stop"
reasoning = "r"
is_residual = true

[result_schema]
x = "float"
"""


def _campaign_with_contradicted_citation(tmp_path: Path):
    """A member run whose observed 'fail' outcome disfavours the very hypothesis its
    [review] entry cited prior work in support of."""
    from bathos.campaigns import add_run_to_campaign, create_campaign
    from bathos.claim import register_claim

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    init_catalog(catalog_dir)

    sidecar = tmp_path / "run_x.bth.toml"
    sidecar.write_text(_REVIEW_SIDECAR)
    (tmp_path / "claim.bth.toml").write_text(_CLAIM)

    run = Run(
        project_slug="p",
        command="python s.py",
        argv=["python", "s.py"],
        git_hash="a",
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        sidecar_path=str(sidecar),
        outcome="fail",
    )
    write_run(run, catalog_dir)
    compact(catalog_dir)

    db = duckdb.connect(str(catalog_dir / "bathos.db"))
    campaign = create_campaign(db, "c", "p", "exploration")
    register_claim(tmp_path / "claim.bth.toml", campaign.id, db, tmp_path)
    add_run_to_campaign(db, campaign.id, run.id)
    db.commit()
    return db, campaign, run


def test_trigger_4_opens_on_a_contradicted_supports_citation(tmp_path, monkeypatch):
    from bathos.campaigns import conclude_campaign

    monkeypatch.setenv("BTH_OBLIGATION_CITATION_CONTRADICTED", "1")
    db, campaign, run = _campaign_with_contradicted_citation(tmp_path)

    conclude_campaign(db, campaign.id, "pass", "note", workspace_root=tmp_path)

    obs = [o for o in list_obligations(tmp_path) if o.trigger == "citation_contradicted"]
    assert [o.entity_id for o in obs] == [run.id]
    assert "10.1/prior" in obs[0].detail and "H1" in obs[0].detail


def test_trigger_4_stays_shut_while_disabled(tmp_path, monkeypatch):
    from bathos.campaigns import conclude_campaign

    monkeypatch.delenv("BTH_OBLIGATION_CITATION_CONTRADICTED", raising=False)
    db, campaign, _run = _campaign_with_contradicted_citation(tmp_path)

    conclude_campaign(db, campaign.id, "pass", "note", workspace_root=tmp_path)
    assert list_obligations(tmp_path) == []


# ── trigger 1, end to end through `bth run` ────────────────────────────────

_SCRIPT = """
import os, json
p = os.environ.get("BTH_RESULTS_PATH")
if p:
    json.dump({{"temp_std": {value}}}, open(p, "w"))
"""

_SIDECAR = """
[experiment]
hypothesis = "h"
[outcomes.pass]
condition = "temp_std < 5"
decision = "good"
reasoning = "stable"
[outcomes.fail]
condition = "temp_std >= 5"
decision = "stop"
reasoning = "unstable"
is_residual = true
[result_schema]
temp_std = "float"
"""


def _run(tmp_path: Path, temp_std: float) -> str:
    import sys

    from bathos.catalog import read_runs
    from bathos.runner import run_script

    catalog = tmp_path / "catalog"
    catalog.mkdir(exist_ok=True)
    enforced = tmp_path / "scripts" / "experiments"
    enforced.mkdir(parents=True, exist_ok=True)
    (enforced / "run_x.py").write_text(_SCRIPT.format(value=temp_std))
    (enforced / "run_x.bth.toml").write_text(_SIDECAR)

    rc = run_script(
        argv=[sys.executable, str(enforced / "run_x.py")],
        project_slug="proj",
        catalog_dir=catalog,
        output_paths=[],
        tags=[],
        cwd=tmp_path,
    )
    assert rc == 0
    runs = read_runs(catalog)
    return runs[0].outcome


def test_a_failing_run_opens_an_obligation_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("BTH_OBLIGATION_OUTCOME_FAILED", "1")

    assert _run(tmp_path, 12.0) == "fail"

    obs = list_obligations(tmp_path)
    assert [(o.entity_kind, o.trigger) for o in obs] == [("run", "outcome_failed")]
    assert "fail" in obs[0].detail


def test_a_failing_run_opens_nothing_while_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.delenv("BTH_OBLIGATION_OUTCOME_FAILED", raising=False)

    assert _run(tmp_path, 12.0) == "fail"
    assert list_obligations(tmp_path) == []


def test_a_passing_run_opens_nothing_even_when_enabled(tmp_path, monkeypatch):
    """The polarity guard, end to end: enabling the trigger must not burden healthy runs."""
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("BTH_OBLIGATION_OUTCOME_FAILED", "1")

    assert _run(tmp_path, 2.0) == "pass"
    assert list_obligations(tmp_path) == []


# ── bth submit warns, never blocks (D1) ────────────────────────────────────


def test_submit_warns_about_open_obligations_without_blocking(tmp_path, monkeypatch):
    """The warning is unflagged and unconditional, but inert by default: with no trigger
    enabled the ledger is empty, so it prints nothing."""
    from typer.testing import CliRunner

    from bathos.cli import app
    from bathos.obligations import open_obligation

    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "p"\nroot = "{tmp_path}"\n')

    runner = CliRunner()
    quiet = runner.invoke(app, ["submit", "--no-push-first", "--", "echo", "hi"])
    assert "open obligation" not in quiet.output.lower()

    ob = open_obligation(tmp_path, "run", "r1", "outcome_failed")
    noisy = runner.invoke(app, ["submit", "--no-push-first", "--", "echo", "hi"])
    assert "open obligation(s) awaiting a post-mortem" in noisy.output
    assert ob.obligation_id in noisy.output
    # It warns and moves on: the run still reaches cluster resolution rather than exiting here.
    assert "Submitting anyway" in noisy.output


def test_an_unreadable_ledger_cannot_break_conclude(tmp_path, monkeypatch, capsys):
    """A campaign whose verdict is already decided must not be lost to a ledger read."""
    from bathos.campaigns import conclude_campaign

    _cat, db, campaign = _catalog_with_campaign(tmp_path)
    ws = tmp_path / "ws"
    ws.mkdir()

    def _explode(*_args, **_kwargs):
        raise OSError("ledger on fire")

    monkeypatch.setattr("bathos.obligations.list_obligations_for_scope", _explode)
    conclude_campaign(db, campaign.id, "pass", "note", ws)

    assert "could not read the obligation ledger" in capsys.readouterr().out
    row = db.execute("SELECT outcome_label FROM campaigns WHERE id=?", [campaign.id]).fetchone()
    assert row[0] == "pass"
