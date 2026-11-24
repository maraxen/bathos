"""Tests for campaign formulation (#3315) and claim formulation validation hooks."""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from bathos.campaign_formulation import (
    DecisionBranch,
    MatrixRow,
    ReuseEntry,
    parse_formulation,
    require_valid_formulation,
    validate_formulation,
)
from bathos.campaigns import CampaignError
from bathos.claim import (
    parse_claim,
    register_claim,
    validate_claim,
    validate_claim_formulation,
)


def _valid_formulation_dict() -> dict:
    return {
        "matrix": [
            {
                "hypothesis": "H_primary",
                "arm": "treatment_lr_1e4",
                "readout": "val_loss",
                "confound_control": "matched_seed_baseline",
            },
            {
                "hypothesis": "H_null",
                "arm": "sham_shuffled_labels",
                "readout": "val_loss",
                "confound_control": "matched_seed_baseline",
            },
        ],
        "decision_tree": {
            "max_fallbacks": 2,
            "max_depth": 3,
            "branches": [
                {
                    "branch_id": "b1",
                    "condition": "val_loss < 0.25",
                    "action": "promote_to_confirmatory",
                    "fallback": "halve learning rate",
                    "fallback_to": "b2",
                },
                {
                    "branch_id": "b2",
                    "condition": "val_loss >= 0.25",
                    "action": "rerun_with_halved_lr",
                    "fallback": "halt_and_report_null",
                    "terminal": True,
                },
            ],
        },
        "reuse_table": [
            {
                "already_done": "baseline_run_001 (seed=42)",
                "used_by": "treatment_lr_1e4 control comparison",
            }
        ],
    }


def test_validate_formulation_happy_path() -> None:
    res = validate_formulation(
        _valid_formulation_dict(),
        known_hypotheses=["H_primary", "H_null"],
    )
    assert res.ok is True
    assert res.errors == ()
    assert res.formulation is not None
    assert len(res.formulation.matrix) == 2
    assert len(res.formulation.decision_tree) == 2
    assert len(res.formulation.reuse_table) == 1


def test_validate_formulation_requires_all_three_artifacts() -> None:
    res = validate_formulation({})
    assert res.ok is False
    joined = " | ".join(res.errors)
    assert "matrix" in joined
    assert "decision_tree" in joined
    assert "reuse_table" in joined


def test_validate_formulation_rejects_incomplete_matrix_row_and_unknown_hypothesis() -> None:
    payload = _valid_formulation_dict()
    payload["matrix"] = [
        {
            "hypothesis": "H_undeclared",
            "arm": "arm_a",
            "readout": "",
            "confound_control": "",
        }
    ]
    res = validate_formulation(payload, known_hypotheses=["H_primary", "H_null"])
    assert res.ok is False
    joined = " | ".join(res.errors)
    assert "readout" in joined
    assert "confound_control" in joined
    assert "H_undeclared" in joined


def test_validate_formulation_rejects_unbounded_or_cyclic_decision_tree() -> None:
    # Unbounded fallback (no max_fallbacks / max_depth)
    unbounded = _valid_formulation_dict()
    unbounded["decision_tree"] = [
        {
            "branch_id": "b1",
            "condition": "metric < 1.0",
            "action": "retry",
            "fallback": "fallback_action",
        }
    ]
    res_unbounded = validate_formulation(unbounded)
    assert res_unbounded.ok is False
    assert any("explicit finite bound" in err for err in res_unbounded.errors)

    # Cyclic fallback_to
    cyclic = _valid_formulation_dict()
    cyclic["decision_tree"] = {
        "max_fallbacks": 3,
        "branches": [
            {
                "branch_id": "b1",
                "condition": "cond1",
                "action": "act1",
                "fallback": "go b2",
                "fallback_to": "b2",
            },
            {
                "branch_id": "b2",
                "condition": "cond2",
                "action": "act2",
                "fallback": "go b1",
                "fallback_to": "b1",
            },
        ],
    }
    res_cyclic = validate_formulation(cyclic)
    assert res_cyclic.ok is False
    assert any("cycle" in err for err in res_cyclic.errors)

    # Exceeds max_fallbacks bound
    exceeds = _valid_formulation_dict()
    exceeds["decision_tree"] = {
        "max_fallbacks": 1,
        "branches": [
            {
                "branch_id": "b1",
                "condition": "c1",
                "action": "a1",
                "fallback": "to b2",
                "fallback_to": "b2",
            },
            {
                "branch_id": "b2",
                "condition": "c2",
                "action": "a2",
                "fallback": "to b3",
                "fallback_to": "b3",
            },
            {
                "branch_id": "b3",
                "condition": "c3",
                "action": "a3",
                "fallback": "stop",
                "terminal": True,
            },
        ],
    }
    res_exceeds = validate_formulation(exceeds)
    assert res_exceeds.ok is False
    assert any("exceeding max_fallbacks=1" in err for err in res_exceeds.errors)


def test_validate_formulation_rejects_empty_reuse_fields() -> None:
    payload = _valid_formulation_dict()
    payload["reuse_table"] = [{"already_done": "", "used_by": ""}]
    with pytest.raises(CampaignError, match="already_done"):
        require_valid_formulation(payload)


def test_parse_formulation_typed_objects_roundtrip() -> None:
    parsed = parse_formulation(_valid_formulation_dict())
    assert isinstance(parsed.matrix[0], MatrixRow)
    assert isinstance(parsed.decision_tree[0], DecisionBranch)
    assert isinstance(parsed.reuse_table[0], ReuseEntry)
    assert require_valid_formulation(parsed) == parsed


def _setup_claim_db(tmp_path: Path) -> duckdb.DuckDBPyConnection:
    db = duckdb.connect(str(tmp_path / "claim_form.db"))
    db.execute(
        """
        CREATE TABLE campaigns (
            id TEXT PRIMARY KEY,
            project_slug TEXT NOT NULL,
            name TEXT NOT NULL,
            mode TEXT NOT NULL,
            question TEXT,
            hypothesis TEXT,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            concluded_at TEXT,
            conclusion TEXT,
            outcome_label TEXT,
            parent_campaign_id TEXT,
            stopping_threshold REAL,
            claim_path TEXT,
            claim_sha256 TEXT,
            claim_mode TEXT,
            negative_check TEXT
        )
        """
    )
    db.execute(
        """
        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            campaign_id TEXT,
            claim_discriminates TEXT,
            outcome TEXT,
            parity_run_type TEXT,
            metadata TEXT,
            differential_status TEXT,
            dependency_lock_sha256 TEXT
        )
        """
    )
    db.execute(
        """
        CREATE TABLE campaign_runs (
            campaign_id TEXT NOT NULL,
            run_id TEXT NOT NULL,
            evalue REAL,
            seq_position INTEGER,
            PRIMARY KEY (campaign_id, run_id)
        )
        """
    )
    db.execute(
        """
        INSERT INTO campaigns (id, project_slug, name, mode, status, started_at)
        VALUES ('camp-form-001', 'proj', 'form_test', 'confirmation', 'open', '2026-11-24T00:00:00Z')
        """
    )
    db.commit()
    return db


def test_claim_parse_validate_and_register_with_formulation_hook(tmp_path: Path) -> None:
    claim_with_form = tmp_path / "with_form.claim.toml"
    claim_with_form.write_text(
        """[claim]
headline = "Formulated test claim"
kill_condition = "No difference between arms"
kill_condition_satisfiable_by_null = false

[[hypotheses]]
id = "H_primary"
label = "Primary effect"

[[hypotheses]]
id = "H_null"
label = "Null effect"

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "main"
predicted_outcome = "pass"

[claim.union_gate]
[[claim.union_gate.clauses]]
id = "C_main"
description = "Main discriminator"
hypothesis_ids = ["H_primary", "H_null"]

[formulation]
max_fallbacks = 2

[[formulation.matrix]]
hypothesis = "H_primary"
arm = "treatment"
readout = "accuracy"
confound_control = "seed_matched_control"

[[formulation.decision_tree]]
branch_id = "root"
condition = "accuracy >= 0.9"
action = "accept"
fallback = "stop_and_escalate"

[[formulation.reuse_table]]
already_done = "baseline_run_42"
used_by = "treatment control arm"
""",
        encoding="utf-8",
    )
    parsed = parse_claim(claim_with_form)
    assert parsed.formulation
    val_res = validate_claim(parsed)
    assert val_res.ok is True, val_res.errors

    # Claim missing formulation passes opt-in by default, fails when require_formulation=True
    claim_no_form = tmp_path / "no_form.claim.toml"
    claim_no_form.write_text(
        """[claim]
headline = "Unformulated claim"
kill_condition = "Null holds"
kill_condition_satisfiable_by_null = false

[[hypotheses]]
id = "H_primary"
label = "Primary"

[[hypotheses]]
id = "H_null"
label = "Null"
""",
        encoding="utf-8",
    )
    parsed_no_form = parse_claim(claim_no_form)
    assert validate_claim_formulation(parsed_no_form).ok is True
    assert validate_claim_formulation(parsed_no_form, require=True).ok is False

    db = _setup_claim_db(tmp_path)
    try:
        with pytest.raises(CampaignError, match="formulation"):
            register_claim(
                claim_no_form,
                "camp-form-001",
                db,
                tmp_path,
                require_formulation=True,
            )

        # Registering claim that embeds valid [formulation] succeeds cleanly
        register_claim(
            claim_with_form,
            "camp-form-001",
            db,
            tmp_path,
            require_formulation=True,
        )
        row = db.execute(
            "SELECT claim_path FROM campaigns WHERE id = 'camp-form-001'"
        ).fetchone()
        assert row == ("with_form.claim.toml",)
    finally:
        db.close()
