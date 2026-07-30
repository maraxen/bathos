"""Tests for the post-mortem obligation ledger (build-order step 4)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from bathos.obligations import (
    Obligation,
    discharge,
    discharge_from_postmortem,
    ledger_dir,
    list_obligations,
    open_obligation,
    signal_open_obligation_age,
)


def test_open_creates_a_ledger_file(tmp_path):
    ob = open_obligation(tmp_path, "run", "r1", "outcome_failed", detail="temp_std 12")
    p = ledger_dir(tmp_path) / f"{ob.obligation_id}.json"
    assert p.exists()
    assert json.loads(p.read_text())["trigger"] == "outcome_failed"
    assert ob.is_open


def test_open_is_idempotent_and_does_not_reset_age(tmp_path):
    """Re-running a failing experiment must not duplicate, nor reset an old obligation's age."""
    first = open_obligation(tmp_path, "run", "r1", "outcome_failed")
    again = open_obligation(tmp_path, "run", "r1", "outcome_failed")
    assert again.opened_at == first.opened_at
    assert len(list(ledger_dir(tmp_path).glob("*.json"))) == 1


def test_run_and_campaign_ids_do_not_collide(tmp_path):
    """§10 open item 3: a flat <id>.json conflated two ID namespaces."""
    open_obligation(tmp_path, "run", "same", "outcome_failed")
    open_obligation(tmp_path, "campaign", "same", "campaign_confounded")
    assert len(list_obligations(tmp_path)) == 2


def test_distinct_triggers_are_distinct_obligations(tmp_path):
    open_obligation(tmp_path, "run", "r1", "outcome_failed")
    open_obligation(tmp_path, "run", "r1", "citation_contradicted")
    assert len(list_obligations(tmp_path, entity_id="r1")) == 2


@pytest.mark.parametrize("bad", ["nonsense", "", "OUTCOME_FAILED"])
def test_unknown_trigger_is_rejected(tmp_path, bad):
    with pytest.raises(ValueError, match="trigger must be one of"):
        open_obligation(tmp_path, "run", "r1", bad)


def test_unknown_entity_kind_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="entity_kind must be one of"):
        open_obligation(tmp_path, "sprint", "s1", "outcome_failed")


def test_discharge_marks_closed_and_records_who(tmp_path):
    ob = open_obligation(tmp_path, "run", "r1", "outcome_failed")
    out = discharge(tmp_path, ob.obligation_id, "notes/pm.toml")
    assert not out.is_open
    assert out.discharged_by == "notes/pm.toml"
    assert list_obligations(tmp_path, open_only=True) == []
    assert len(list_obligations(tmp_path, open_only=False)) == 1


def test_discharging_an_unknown_obligation_returns_none(tmp_path):
    assert discharge(tmp_path, "run_nope_outcome_failed", "x") is None


def test_corrupt_ledger_file_is_skipped_not_fatal(tmp_path):
    open_obligation(tmp_path, "run", "good", "outcome_failed")
    (ledger_dir(tmp_path) / "corrupt.json").write_text("{not json", encoding="utf-8")
    obs = list_obligations(tmp_path)
    assert [o.entity_id for o in obs] == ["good"]


def test_no_ledger_dir_lists_empty(tmp_path):
    assert list_obligations(tmp_path) == []


# ── discharge via postmortem ────────────────────────────────────────────────


def test_valid_postmortem_discharges_named_obligations(tmp_path):
    ob = open_obligation(tmp_path, "campaign", "c1", "campaign_confounded")
    pm = tmp_path / "c1.bth.postmortem.toml"
    pm.write_text(
        "[postmortem]\n"
        'campaign_id = "c1"\n'
        'hypothesis_status = "refuted"\n'
        f'discharges = ["{ob.obligation_id}"]\n',
        encoding="utf-8",
    )
    assert discharge_from_postmortem(tmp_path, pm) == [ob.obligation_id]
    assert list_obligations(tmp_path, open_only=True) == []


def test_an_invalid_postmortem_discharges_nothing(tmp_path):
    """Validity is the existing consistency rule — refuted + a 'pass' override is inconsistent."""
    ob = open_obligation(tmp_path, "run", "r1", "outcome_failed")
    pm = tmp_path / "r1.bth.postmortem.toml"
    pm.write_text(
        "[postmortem]\n"
        'run_id = "r1"\n'
        'hypothesis_status = "refuted"\n'
        'verdict_override = "pass"\n'
        f'discharges = ["{ob.obligation_id}"]\n',
        encoding="utf-8",
    )
    assert discharge_from_postmortem(tmp_path, pm) == []
    assert len(list_obligations(tmp_path, open_only=True)) == 1


# ── Signal 11 ───────────────────────────────────────────────────────────────


def test_signal_reports_and_does_not_threshold(tmp_path):
    open_obligation(tmp_path, "run", "r1", "outcome_failed")
    open_obligation(tmp_path, "campaign", "c1", "campaign_confounded")
    sig = signal_open_obligation_age(tmp_path)
    assert sig["signal"] == "open_obligation_age"
    assert sig["open_count"] == 2
    assert sig["by_trigger"] == {"outcome_failed": 1, "campaign_confounded": 1}
    # Reporting only: no pass/fail, no anomaly flag, no cutoff.
    assert "anomaly" not in sig and "threshold" not in sig


def test_signal_on_an_empty_ledger(tmp_path):
    sig = signal_open_obligation_age(tmp_path)
    assert sig["open_count"] == 0 and sig["max_age_days"] == 0.0


def test_age_days_uses_the_opened_timestamp():
    old = (datetime.now(UTC) - timedelta(days=10)).isoformat()
    ob = Obligation("x", "run", "r", "outcome_failed", opened_at=old)
    assert 9.5 < ob.age_days() < 10.5


def test_age_days_survives_a_malformed_timestamp():
    ob = Obligation("x", "run", "r", "outcome_failed", opened_at="not-a-date")
    assert ob.age_days() == 0.0
