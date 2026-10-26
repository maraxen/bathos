"""Distributional breakdown, T3 (spec 261026 D3): `Sidecar.from_declaration` rebuilds the whole
parsed sidecar from the `run.started` declaration, replacing the lossy 7-field rebuild."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

from bathos.runlog.emit import sidecar_declaration_for_event
from bathos.sidecar import OutcomeSpec, Sidecar, SidecarKind, compute_evalue, parse_sidecar

_RICH = textwrap.dedent("""
    [experiment]
    hypothesis = "h"
    stage_name = "exploration"
    novel = true
    claim_discriminates = ["H1"]
    claim_isolates = ["C1"]
    [observe]
    looking_for = "worst case"
    sanity = ["e_max_placement < 9"]
    outputs = ["per_unit.parquet"]
    planned_tests = 2
    [calibrate]
    quantity = "noise floor"
    [popper]
    null_pass_rate = 0.1
    alt_pass_rate = 0.9
    stopping_threshold = 20.0
    [popper.weights]
    pass = 1.0
    [reproduction]
    reproduces_paper = "doi:10.1/x"
    tolerance_pct = 5.0
    [controls]
    positive_outcome = ["pass"]
    [differential]
    knob = "SIGNAL"
    off = "0"
    on = "1"
    [requires]
    workload = "jax"
    artifact = "out/*.json"
    json_key = "meta.sha"
    must_equal = "HEAD"
    [budget]
    est_wall_s = 120.0
    n_units = 24
    [status]
    stale = false
    [[review.literature]]
    ref = "10.1/x"
    claim = "c"
    bears_on = "H1"
    disposition = "supports"
    [[review.implementation]]
    source = "repo/file.py"
    commit = "abc"
    what_was_checked = "loss"
    [outputs]
    roots = ["outputs/a"]
    [outcomes.pass]
    condition = "e_max_placement <= 0.5"
    decision = "go"
    reasoning = "r"
    adversarial_check = "e_max_placement__n_over = 0"
    source = "lit"
    [outcomes.fail]
    condition = "e_max_placement > 0.5"
    decision = "stop"
    reasoning = "r"
    is_residual = true
    [breakdown]
    unit = "binder"
    artifact = "per_unit.parquet"
    [breakdown.aggregates.e_max_placement]
    direction = "max"
    bound = 0.5
    margin = 0.05
    [result_schema]
    e_max_placement = "float"
    e_max_placement__worst_unit = "str"
    e_max_placement__second = "float"
    e_max_placement__median = "float"
    e_max_placement__n_units = "int"
    e_max_placement__n_over = "int"
    [result_schema_meta.e_max_placement]
    aggregate = true
""")


def _wire(decl: dict) -> dict:
    """What a reader of the append-only log sees: the declaration after a JSON round trip."""
    return json.loads(json.dumps(decl))


def test_full_round_trip_equals_the_parsed_sidecar(tmp_path: Path):
    path = tmp_path / "s.bth.toml"
    path.write_text(_RICH)
    original = parse_sidecar(path)
    restored = Sidecar.from_declaration(_wire(sidecar_declaration_for_event(original)))
    assert restored == original
    # the fields the old rebuild dropped, spot-checked by name
    assert restored.breakdown is not None
    assert restored.breakdown.aggregates["e_max_placement"].bound == 0.5
    assert restored.result_schema_meta == {"e_max_placement": {"aggregate": True}}
    assert restored.observe is not None and restored.observe.sanity == ["e_max_placement < 9"]
    assert restored.kind is SidecarKind.EXPERIMENT
    assert restored.requires is not None and restored.requires.must_equal == "HEAD"


def test_pre_v3_snapshot_without_breakdown_fields_loads(tmp_path: Path):
    """A `run.started` written before `[breakdown]` existed has no `breakdown` or
    `result_schema_meta` key (and, older still, none of the later blocks)."""
    path = tmp_path / "s.bth.toml"
    path.write_text(_RICH)
    decl = _wire(sidecar_declaration_for_event(parse_sidecar(path)))
    for key in ("breakdown", "result_schema_meta"):
        del decl[key]
    restored = Sidecar.from_declaration(decl)
    assert restored.breakdown is None
    assert restored.result_schema_meta == {}
    assert restored.outcomes["pass"].adversarial_check == "e_max_placement__n_over = 0"


def test_literal_old_seven_field_snapshot_still_drives_evalue():
    snapshot = {
        "kind": "experiment",
        "result_schema": {"x": "float"},
        "outcomes": {
            "pass": {"condition": "x > 0", "decision": "go", "reasoning": "r"},
            "fail": {
                "condition": "x <= 0",
                "decision": "stop",
                "reasoning": "r",
                "is_residual": True,
                "adversarial_check": None,
                "source": "",
                "multiple_comparisons_correction": "",
            },
        },
        "popper_null_pass_rate": 0.1,
        "popper_alt_pass_rate": 0.9,
        "popper_stopping_threshold": 20.0,
        "popper_weights": {},
    }
    sc = Sidecar.from_declaration(snapshot)
    assert isinstance(sc.outcomes["pass"], OutcomeSpec)
    assert sc.outcomes["fail"].is_residual is True
    assert compute_evalue(sc, "pass") == 9.0


def test_sparse_outcome_entries_read_missing_strings_as_empty():
    """The old rebuild defaulted absent strings to "" and a null entry to an empty table; real
    declarations (and the runlog fixtures) rely on both."""
    sc = Sidecar.from_declaration(
        {"result_schema": {}, "outcomes": {"a": {"condition": "x > 0"}, "b": None}}
    )
    assert sc.outcomes["a"] == OutcomeSpec(condition="x > 0", decision="")
    assert sc.outcomes["b"] == OutcomeSpec(condition="", decision="")


def test_unknown_keys_are_ignored_and_kind_defaults_to_experiment():
    sc = Sidecar.from_declaration({"result_schema": {}, "added_by_a_future_version": 1})
    assert sc.kind is SidecarKind.EXPERIMENT
    assert sc.breakdown is None
