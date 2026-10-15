"""Epistemic class resolution, `observed` neutrality, source grammar, sanity expressions.

Spec: .praxia/docs/specs/261015_exploratory-mode-contract.md §3.1, §3.2, §3.5.
"""

import textwrap
from pathlib import Path

import pytest


def _sidecar(tmp_path: Path, body: str):
    from bathos.sidecar import parse_sidecar

    p = tmp_path / "run_test.bth.toml"
    p.write_text(textwrap.dedent(body))
    return parse_sidecar(p)


OBSERVE = """
[observe]
outputs = ["out.csv"]
"""

CALIBRATE = """
[calibrate]
quantity = "noise floor"
"""

OUTCOMES = """
[outcomes.pass]
condition = "x > 0"
decision = "proceed"
"""


# --- class resolution (spec §3.1 table) --------------------------------------------------------


@pytest.mark.parametrize("prior", [True, False])
def test_stage_absent_is_legacy(tmp_path, prior):
    from bathos.sidecar import resolve_epistemic_class

    s = _sidecar(tmp_path, "[experiment]\nhypothesis = 'h'\n" + OUTCOMES)
    assert resolve_epistemic_class(s, stem_has_prior_runs=prior) == "legacy"


def test_non_canonical_stage_is_confirmatory_even_with_observe(tmp_path):
    from bathos.sidecar import resolve_epistemic_class

    # "validaton" is a typo: it fails closed to confirmatory, and cannot pick up the
    # exploratory class from the [observe] block that sits beside it.
    s = _sidecar(tmp_path, '[experiment]\nstage_name = "validaton"\n' + OBSERVE)
    assert resolve_epistemic_class(s, stem_has_prior_runs=False) == "confirmatory"
    assert resolve_epistemic_class(s, stem_has_prior_runs=True) == "confirmatory"


def test_exploration_with_observe_is_exploratory(tmp_path):
    from bathos.sidecar import resolve_epistemic_class

    s = _sidecar(tmp_path, '[experiment]\nstage_name = "exploration"\n' + OBSERVE)
    assert resolve_epistemic_class(s, stem_has_prior_runs=False) == "exploratory"
    assert resolve_epistemic_class(s, stem_has_prior_runs=True) == "exploratory"


def test_calibration_with_calibrate_is_calibrating(tmp_path):
    from bathos.sidecar import resolve_epistemic_class

    s = _sidecar(tmp_path, '[experiment]\nstage_name = "calibration"\n' + CALIBRATE)
    assert resolve_epistemic_class(s, stem_has_prior_runs=False) == "calibrating"


@pytest.mark.parametrize("stage", ["validation", "ablation", "production"])
def test_confirmatory_stages(tmp_path, stage):
    from bathos.sidecar import resolve_epistemic_class

    s = _sidecar(tmp_path, f'[experiment]\nstage_name = "{stage}"\n' + OUTCOMES)
    assert resolve_epistemic_class(s, stem_has_prior_runs=False) == "confirmatory"
    assert resolve_epistemic_class(s, stem_has_prior_runs=True) == "confirmatory"


def test_exploration_without_observe_on_existing_stem_is_legacy(tmp_path):
    from bathos.sidecar import resolve_epistemic_class

    # The 44 sibling sidecars: explicit stage_name = "exploration" with real [outcomes] and no
    # new shape. Shape gating keeps them on today's rules.
    s = _sidecar(tmp_path, '[experiment]\nstage_name = "exploration"\n' + OUTCOMES)
    assert resolve_epistemic_class(s, stem_has_prior_runs=True) == "legacy"


def test_exploration_without_observe_on_new_stem_is_invalid(tmp_path):
    from bathos.sidecar import resolve_epistemic_class

    s = _sidecar(tmp_path, '[experiment]\nstage_name = "exploration"\n' + OUTCOMES)
    assert resolve_epistemic_class(s, stem_has_prior_runs=False) is None


def test_calibration_without_calibrate_on_existing_stem_is_legacy(tmp_path):
    from bathos.sidecar import resolve_epistemic_class

    s = _sidecar(tmp_path, '[experiment]\nstage_name = "calibration"\n' + OUTCOMES)
    assert resolve_epistemic_class(s, stem_has_prior_runs=True) == "legacy"


def test_calibration_without_calibrate_on_new_stem_is_invalid(tmp_path):
    from bathos.sidecar import resolve_epistemic_class

    s = _sidecar(tmp_path, '[experiment]\nstage_name = "calibration"\n')
    assert resolve_epistemic_class(s, stem_has_prior_runs=False) is None


def test_non_experiment_sidecar_is_legacy(tmp_path):
    from bathos.sidecar import resolve_epistemic_class

    s = _sidecar(
        tmp_path,
        """
        [benchmark]
        baseline_ref = "run_abc"
        metric = "ns_per_day"
        [result_schema]
        ns_per_day = "float"
        """,
    )
    assert resolve_epistemic_class(s, stem_has_prior_runs=False) == "legacy"


def test_every_resolved_class_is_in_the_vocabulary():
    from bathos.sidecar import EPISTEMIC_CLASSES

    assert set(EPISTEMIC_CLASSES) == {"exploratory", "calibrating", "confirmatory", "legacy"}


# --- `observed` is neutral (spec §3.2, C5) ----------------------------------------------------


def _popper_sidecar():
    from bathos.sidecar import OutcomeSpec, Sidecar, SidecarKind

    return Sidecar(
        kind=SidecarKind.EXPERIMENT,
        result_schema={},
        outcomes={"pass": OutcomeSpec(condition="x > 0", decision="proceed")},
        popper_null_pass_rate=0.1,
        popper_alt_pass_rate=0.8,
    )


def test_observed_is_not_a_failure():
    from bathos.sidecar import is_failure_outcome

    s = _popper_sidecar()
    assert is_failure_outcome(s, "observed") is False
    assert is_failure_outcome(s, "") is False
    assert is_failure_outcome(s, "unknown") is False
    # A real non-pass is still a failure, so the neutrality is not a blanket "no failures".
    assert is_failure_outcome(s, "fail") is True
    assert is_failure_outcome(s, "pass") is False


def test_observed_has_neutral_evalue():
    from bathos.sidecar import compute_evalue

    s = _popper_sidecar()
    assert compute_evalue(s, "observed") == 1.0
    # Explicit weights cannot make an observation non-neutral either.
    s.popper_weights = {"observed": 5.0}
    assert compute_evalue(s, "observed") == 1.0
    # Sanity: the same sidecar still produces a non-neutral e-value for a real pass.
    assert compute_evalue(s, "pass") == pytest.approx(8.0)


def test_observed_is_excluded_from_pass_labels():
    from bathos.sidecar import RESERVED_OUTCOME_LABELS, derive_pass_labels

    assert "observed" in RESERVED_OUTCOME_LABELS
    s = _popper_sidecar()
    s.outcomes["observed"] = s.outcomes["pass"]
    assert derive_pass_labels(s) == {"pass"}


# --- source grammar (spec §3.2) ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind", "attrs"),
    [
        ("lit:doi:10.1101/2024.01.02.123456", "lit", {"ref": "doi:10.1101/2024.01.02.123456"}),
        ("run:abc123:mi_noise", "run", {"ref": "abc123", "field": "mi_noise"}),
        ("sesoi:0.5:smallest effect we care about", "sesoi", {"value": 0.5}),
        ("provisional:no basis yet", "provisional", {"why": "no basis yet"}),
        ("card:C1", "card", {"ref": "C1"}),
    ],
)
def test_parse_source_accepts_each_kind(text, kind, attrs):
    from bathos.sidecar import parse_source

    ref = parse_source(text)
    assert ref is not None
    assert ref.kind == kind
    for k, v in attrs.items():
        assert getattr(ref, k) == v


def test_parse_source_sesoi_keeps_colons_in_why():
    from bathos.sidecar import parse_source

    ref = parse_source("sesoi:0.5:why: colon kept")
    assert ref.value == 0.5
    assert ref.why == "why: colon kept"


@pytest.mark.parametrize(
    "text",
    [
        "sesoi:abc:x",  # non-numeric value
        "sesoi:nan:x",  # non-finite
        "sesoi:1.0:",  # empty why
        "run:abc",  # missing field
        "run::field",  # empty id
        "run:abc:",  # empty field
        "lit:",  # empty ref
        "card:",  # empty id
        "provisional:",  # empty why
        "${THRESHOLD}",  # unexpanded template placeholder
        "lit:${X}",  # placeholder anywhere is malformed
        "free text",  # no kind
        "",  # empty
        "foo:bar",  # unknown kind
    ],
)
def test_parse_source_rejects_malformed(text):
    from bathos.sidecar import parse_source

    assert parse_source(text) is None


# --- sanity expressions (spec §3.2 validator rules) -------------------------------------------

SCHEMA = {"n_frames_pooled", "is_ok", "a", "b", "c", "x"}


@pytest.mark.parametrize(
    "expr",
    [
        "n_frames_pooled > 0",
        "is_ok",
        "a > b AND c = 1",
        "NOT is_ok",
        "x IS NULL",
        "x IS NOT NULL",
        "x > 1",
        "x = TRUE",
        "x = 0 OR a = NULL",
    ],
)
def test_check_sanity_expr_accepts(expr):
    from bathos.sidecar import check_sanity_expr

    assert check_sanity_expr(expr, SCHEMA) == []


@pytest.mark.parametrize(
    "expr",
    [
        "x > 1+1+1",  # literal inside arithmetic: the dodge this check exists to stop
        "x > 5",  # literal other than 0/1/NULL/boolean
        "x > 0.5",  # fractional literal
        "unknown_field > 0",  # field not in result_schema
        "x > )",  # syntax error
        "x) UNION SELECT (1",  # injection attempt: not a single expression
        "x) FROM t WHERE (1",  # injection attempt: extra clauses
        "t.x > 0",  # qualified reference
        "x = 'abc'",  # string literal
    ],
)
def test_check_sanity_expr_rejects(expr):
    from bathos.sidecar import check_sanity_expr

    assert check_sanity_expr(expr, SCHEMA) != []


def test_check_sanity_expr_unknown_field_message_names_field():
    from bathos.sidecar import check_sanity_expr

    errs = check_sanity_expr("y > 0", {"x"})
    assert len(errs) == 1
    assert "'y'" in errs[0]


def test_check_sanity_expr_syntax_error_is_reported():
    from bathos.sidecar import check_sanity_expr

    errs = check_sanity_expr("x > )", SCHEMA)
    assert errs and "parse" in errs[0]


# --- sanity allowlist, hidden thresholds (spec 261015 §3.2, fix B) -----------------------------

SCHEMA_XN = {"x", "n"}


@pytest.mark.parametrize(
    "expr",
    [
        "count(*) FILTER (WHERE x > 5) >= 1",  # threshold smuggled under FUNCTION `filter`
        "sum(x) FILTER (WHERE x > 5) > 0",  # same, but the call does reference a column
        "CASE WHEN x > 5 THEN true ELSE false END",  # CASE branches
        "x BETWEEN 0 AND 100",  # BETWEEN is a two-sided literal range
        "x IN (3, 4)",  # IN list of literals
        "CAST(x AS INT) > 1",  # general cast
        "x > -5",  # negative literal
        "x > 1e9",  # non-0/1 numeric literal
        "x > 'a'",  # string literal
        "x > (SELECT 5)",  # subquery
        "x > 1+1+1",  # literal inside arithmetic
    ],
)
def test_sanity_hidden_threshold_is_rejected(expr):
    from bathos.sidecar import check_sanity_expr

    errs = check_sanity_expr(expr, SCHEMA_XN)
    assert errs, f"expected {expr!r} to be rejected"


@pytest.mark.parametrize(
    "expr",
    [
        "x > 0",
        "n >= 1",
        "x IS NOT NULL",
        "NOT (x < 0) AND n > 0",
        "x = x",
        # Functions over result fields with no literal beneath them are instrument checks.
        "isfinite(x)",
        "abs(x) <= abs(n)",
        "x - n < 0",
    ],
)
def test_sanity_allowlist_accepts_plain_checks(expr):
    from bathos.sidecar import check_sanity_expr

    assert check_sanity_expr(expr, SCHEMA_XN) == [], f"expected {expr!r} to pass"


# --- gate_stage: a typo fails closed for the enforcement gates (spec 261015 §3.1, fix A) ------


def test_gate_stage_typo_fails_closed_to_validation():
    from bathos.sidecar import Sidecar, SidecarKind

    s = Sidecar(
        kind=SidecarKind.EXPERIMENT,
        result_schema={},
        stage_name="exploration",  # coerced value, as parse_sidecar stores it
        stage_name_declared=True,
        stage_name_raw="validaton",
    )
    assert s.gate_stage == "validation"
    # The stored coerced value is untouched; only the gate reads gate_stage.
    assert s.stage_name == "exploration"


@pytest.mark.parametrize(
    "stage", ["exploration", "calibration", "validation", "ablation", "production"]
)
def test_gate_stage_canonical_stage_is_unchanged(tmp_path, stage):
    s = _sidecar(tmp_path, f'[experiment]\nstage_name = "{stage}"\nhypothesis = "H"\n')
    assert s.gate_stage == stage


def test_gate_stage_absent_stage_name_is_exploration(tmp_path):
    s = _sidecar(tmp_path, '[experiment]\nhypothesis = "H"\n')
    assert s.stage_name_declared is False
    assert s.gate_stage == "exploration"


# --- invalid_measurement is neutral, exactly like observed (fix D) ----------------------------


def test_invalid_measurement_is_not_a_failure():
    from bathos.sidecar import is_failure_outcome

    s = _popper_sidecar()
    assert is_failure_outcome(s, "invalid_measurement") is False
    # Same as observed, and a real non-pass still counts.
    assert is_failure_outcome(s, "observed") is False
    assert is_failure_outcome(s, "fail") is True


def test_invalid_measurement_has_neutral_evalue():
    from bathos.sidecar import compute_evalue

    s = _popper_sidecar()
    assert compute_evalue(s, "invalid_measurement") == 1.0
    # Explicit weights cannot make an instrument failure non-neutral either.
    s.popper_weights = {"invalid_measurement": 5.0}
    assert compute_evalue(s, "invalid_measurement") == 1.0
    assert compute_evalue(s, "pass") == pytest.approx(8.0)


@pytest.mark.parametrize(
    "expr",
    [
        "abs(x) > abs(1)",  # even an allowed literal is refused under a function
        "count(*) >= 1",  # function reads no result field
        "isfinite(CAST(x AS DOUBLE))",  # cast beneath a function
    ],
)
def test_sanity_function_rules(expr):
    from bathos.sidecar import check_sanity_expr

    assert check_sanity_expr(expr, SCHEMA_XN) != [], f"expected {expr!r} to be rejected"
