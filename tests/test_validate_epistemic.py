"""Epistemic-class validator branches (spec 261015_exploratory-mode-contract §3.1, §3.2, §3.5)."""

import textwrap
from pathlib import Path


def _sidecar(tmp_path: Path, content: str, name: str = "run_test.bth.toml") -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(content))
    return p


def _validate(tmp_path, content, *, prior=False, name="run_test.bth.toml"):
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    path = _sidecar(tmp_path, content, name=name)
    return validate_sidecar(parse_sidecar(path), sidecar_path=path, stem_has_prior_runs=prior)


EXPLORATORY_NO_OUTCOMES = """
[experiment]
stage_name = "exploration"
question = "How does MI distribute across the loop?"

[observe]
outputs = ["mi_heatmap.png"]
sanity = ["n_frames > 0"]

[result_schema]
n_frames = "int"
"""

EXPLORATORY_WITH_OUTCOME = """
[experiment]
stage_name = "exploration"
question = "Is there a hub?"

[observe]
outputs = ["hubs.csv"]

[outcomes.hub]
condition = "top_hub_score > 0.5"
decision = "look at hub residues"
reasoning = "triage"
source = "{source}"

[outcomes.rest]
condition = "1 = 1"
decision = "nothing"
reasoning = "residual"
is_residual = true

[result_schema]
top_hub_score = "float"
"""

CALIBRATION_NO_OUTCOMES = """
[experiment]
stage_name = "calibration"

[calibrate]
quantity = "noise floor of pooled MI"

[result_schema]
noise_floor = "float"
"""


def test_exploratory_without_outcomes_validates(tmp_path):
    result = _validate(tmp_path, EXPLORATORY_NO_OUTCOMES)
    assert result.ok is True, [e.message for e in result.errors]


def test_exploratory_missing_observe_outputs_fails(tmp_path):
    content = EXPLORATORY_NO_OUTCOMES.replace('outputs = ["mi_heatmap.png"]', "outputs = []")
    result = _validate(tmp_path, content)
    assert result.ok is False
    assert any(e.field == "observe.outputs" for e in result.errors)


def test_exploratory_outcome_without_source_is_error_on_new_stem(tmp_path):
    content = EXPLORATORY_WITH_OUTCOME.replace('source = "{source}"', "")
    result = _validate(tmp_path, content, prior=False)
    assert result.ok is False
    assert any(e.field == "outcomes.hub.source" for e in result.errors)


def test_exploratory_outcome_without_source_is_warning_with_prior_runs(tmp_path):
    content = EXPLORATORY_WITH_OUTCOME.replace('source = "{source}"', "")
    result = _validate(tmp_path, content, prior=True)
    assert result.ok is True
    assert any(w.field == "outcomes.hub.source" for w in result.warnings)


def test_exploratory_well_formed_source_passes(tmp_path):
    content = EXPLORATORY_WITH_OUTCOME.replace("{source}", "sesoi:0.5:half of max hub score")
    result = _validate(tmp_path, content)
    assert result.ok is True, [e.message for e in result.errors]
    assert result.warnings == []


def test_exploratory_malformed_source_fails(tmp_path):
    content = EXPLORATORY_WITH_OUTCOME.replace("{source}", "sesoi:notanumber:why")
    result = _validate(tmp_path, content)
    assert result.ok is False
    assert any(e.field == "outcomes.hub.source" for e in result.errors)


def test_unfilled_template_placeholder_source_fails(tmp_path):
    content = EXPLORATORY_WITH_OUTCOME.replace("{source}", "run:${THRESHOLD}:mi")
    result = _validate(tmp_path, content)
    assert result.ok is False
    assert any("unfilled template placeholder" in e.message for e in result.errors)


def test_placeholder_source_fails_for_confirmatory_too(tmp_path):
    content = """
    [experiment]
    stage_name = "validation"
    hypothesis = "H"
    [outcomes.pass]
    condition = "x > 1"
    decision = "go"
    reasoning = "r"
    source = "lit:${REF}"
    [outcomes.fail]
    condition = "x <= 1"
    decision = "stop"
    reasoning = "r"
    is_residual = true
    [result_schema]
    x = "float"
    """
    result = _validate(tmp_path, content)
    assert result.ok is False
    assert any("unfilled template placeholder" in e.message for e in result.errors)


def test_sanity_arithmetic_literal_fails(tmp_path):
    content = EXPLORATORY_NO_OUTCOMES.replace('sanity = ["n_frames > 0"]', 'sanity = ["n_frames > 1+1+1"]')
    result = _validate(tmp_path, content)
    assert result.ok is False
    assert any(e.field == "observe.sanity" for e in result.errors)


def test_calibration_without_block_fails_on_new_stem(tmp_path):
    content = """
    [experiment]
    stage_name = "calibration"
    [outcomes.pass]
    condition = "noise_floor < 1.0"
    decision = "use it"
    reasoning = "r"
    source = "provisional:first pass"
    [outcomes.fail]
    condition = "noise_floor >= 1.0"
    decision = "recheck"
    reasoning = "r"
    is_residual = true
    [result_schema]
    noise_floor = "float"
    """
    result = _validate(tmp_path, content, prior=False)
    assert result.ok is False
    assert any(e.field == "stage_name" for e in result.errors)


def test_calibration_without_block_validates_as_legacy_with_prior_runs(tmp_path):
    content = """
    [experiment]
    stage_name = "calibration"
    [outcomes.pass]
    condition = "noise_floor < 1.0"
    decision = "use it"
    reasoning = "r"
    [outcomes.fail]
    condition = "noise_floor >= 1.0"
    decision = "recheck"
    reasoning = "r"
    is_residual = true
    [result_schema]
    noise_floor = "float"
    """
    result = _validate(tmp_path, content, prior=True)
    assert result.ok is True, [e.message for e in result.errors]


def test_calibration_without_block_and_without_outcomes_fails_with_prior_runs(tmp_path):
    # Legacy rules apply: outcomes are still required.
    content = """
    [experiment]
    stage_name = "calibration"
    [result_schema]
    noise_floor = "float"
    """
    result = _validate(tmp_path, content, prior=True)
    assert result.ok is False
    assert any(e.field == "outcomes" for e in result.errors)


def test_calibrating_with_block_and_no_outcomes_validates(tmp_path):
    result = _validate(tmp_path, CALIBRATION_NO_OUTCOMES)
    assert result.ok is True, [e.message for e in result.errors]


def test_calibrating_requires_result_schema(tmp_path):
    content = CALIBRATION_NO_OUTCOMES.replace('[result_schema]\nnoise_floor = "float"', "")
    result = _validate(tmp_path, content)
    assert result.ok is False
    assert any(e.field == "result_schema" for e in result.errors)


def test_negative_control_confirmatory_without_outcomes_still_fails(tmp_path):
    content = """
    [experiment]
    stage_name = "validation"
    hypothesis = "NVT holds within 5K"
    [result_schema]
    temp_std = "float"
    """
    result = _validate(tmp_path, content)
    assert result.ok is False
    assert any(e.field == "outcomes" for e in result.errors)


def test_no_stage_name_bare_cutoffs_validates_as_today(tmp_path):
    content = """
    [experiment]
    hypothesis = "NVT holds within 5K"
    [outcomes.pass]
    condition = "temp_std < 5"
    decision = "proceed"
    reasoning = "r"
    [outcomes.fail]
    condition = "temp_std >= 5"
    decision = "debug"
    reasoning = "r"
    is_residual = true
    [result_schema]
    temp_std = "float"
    """
    result = _validate(tmp_path, content)
    assert result.ok is True, [e.message for e in result.errors]
    assert result.warnings == []


def test_typo_stage_name_fails_closed_as_confirmatory(tmp_path):
    content = """
    [experiment]
    stage_name = "validaton"
    hypothesis = "H"
    [result_schema]
    x = "float"
    """
    result = _validate(tmp_path, content)
    assert result.ok is False
    assert any(e.field == "outcomes" for e in result.errors)


def test_explicit_exploration_without_observe_with_prior_runs_is_legacy(tmp_path):
    content = """
    [experiment]
    stage_name = "exploration"
    hypothesis = "H"
    [outcomes.pass]
    condition = "x > 1"
    decision = "go"
    reasoning = "r"
    [outcomes.fail]
    condition = "x <= 1"
    decision = "stop"
    reasoning = "r"
    is_residual = true
    [result_schema]
    x = "float"
    """
    result = _validate(tmp_path, content, prior=True)
    assert result.ok is True, [e.message for e in result.errors]


def test_gate_check_surfaces_warnings_without_denying(tmp_path, monkeypatch):
    from bathos import prereg
    from bathos.prereg import SidecarBundle, gate_check

    script = tmp_path / "scripts" / "experiments" / "explore_mi.py"
    script.parent.mkdir(parents=True)
    script.touch()
    sidecar_path = tmp_path / "scripts" / "experiments" / "explore_mi.bth.toml"
    sidecar_path.write_text(
        textwrap.dedent(EXPLORATORY_WITH_OUTCOME.replace("{source}", "").replace('source = ""', ""))
    )

    monkeypatch.setattr(prereg, "_stem_has_prior_runs", lambda *_a, **_k: True)
    bundle = SidecarBundle(path=sidecar_path, sha256="abc", found=True)
    result = gate_check(script, bundle, "collaborative")

    assert result.ok is True
    assert result.error_payload is None
    assert result.validation is not None
    assert any(w.field == "outcomes.hub.source" for w in result.validation.warnings)


def test_gate_check_denies_same_sidecar_on_new_stem(tmp_path, monkeypatch):
    from bathos import prereg
    from bathos.prereg import SidecarBundle, gate_check

    script = tmp_path / "scripts" / "experiments" / "explore_mi.py"
    script.parent.mkdir(parents=True)
    script.touch()
    sidecar_path = tmp_path / "scripts" / "experiments" / "explore_mi.bth.toml"
    sidecar_path.write_text(
        textwrap.dedent(EXPLORATORY_WITH_OUTCOME.replace("{source}", "").replace('source = ""', ""))
    )

    monkeypatch.setattr(prereg, "_stem_has_prior_runs", lambda *_a, **_k: False)
    bundle = SidecarBundle(path=sidecar_path, sha256="abc", found=True)
    result = gate_check(script, bundle, "collaborative")

    assert result.ok is False


def _lint_one(tmp_path, name, content):
    from bathos.linter import lint_project

    d = tmp_path / "scripts" / "experiments"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.bth.toml").write_text(textwrap.dedent(content))
    return lint_project(tmp_path)


def test_lint_non_canonical_stage_name_is_error(tmp_path):
    from bathos.linter import IssueSeverity

    issues = _lint_one(
        tmp_path,
        "run_typo",
        """
        [experiment]
        stage_name = "validaton"
        hypothesis = "H"
        [outcomes.pass]
        condition = "x > 1"
        decision = "go"
        reasoning = "r"
        [outcomes.fail]
        condition = "x <= 1"
        decision = "stop"
        reasoning = "r"
        is_residual = true
        [result_schema]
        x = "float"
        """,
    )
    hits = [i for i in issues if i.issue == "non_canonical_stage_name"]
    assert hits and hits[0].severity == IssueSeverity.ERROR
    assert "validaton" in hits[0].detail


def test_lint_explicit_exploration_without_observe_warns_restage(tmp_path):
    from bathos.linter import IssueSeverity

    issues = _lint_one(
        tmp_path,
        "run_restage",
        """
        [experiment]
        stage_name = "exploration"
        hypothesis = "H"
        [outcomes.pass]
        condition = "x > 1"
        decision = "go"
        reasoning = "r"
        [outcomes.fail]
        condition = "x <= 1"
        decision = "stop"
        reasoning = "r"
        is_residual = true
        [result_schema]
        x = "float"
        """,
    )
    hits = [i for i in issues if i.issue == "restage"]
    assert hits and hits[0].severity == IssueSeverity.WARNING


def test_lint_malformed_source_on_confirmatory_warns(tmp_path):
    from bathos.linter import IssueSeverity

    issues = _lint_one(
        tmp_path,
        "run_badsrc",
        """
        [experiment]
        stage_name = "validation"
        hypothesis = "H"
        [outcomes.pass]
        condition = "x > 1"
        decision = "go"
        reasoning = "r"
        source = "bogus"
        [outcomes.fail]
        condition = "x <= 1"
        decision = "stop"
        reasoning = "r"
        is_residual = true
        [result_schema]
        x = "float"
        """,
    )
    hits = [i for i in issues if i.issue == "malformed_source"]
    assert hits and hits[0].severity == IssueSeverity.WARNING
