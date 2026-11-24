"""Tests for optional `[hypothesis]` block parsing and validation (#3098)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from bathos.sidecar import HypothesisSpec, SidecarError, SidecarKind, parse_sidecar
from bathos.validate import validate_hypothesis_block, validate_sidecar


def _write_toml(tmp_path: Path, content: str, name: str = "exp.bth.toml") -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


_BASE_EXPERIMENT = """
[experiment]
hypothesis = "Active learning cuts sample complexity by 2x vs random sampling"

[outcomes.pass]
condition = "ratio <= 0.5"
decision = "adopt active learning"
reasoning = "2x reduction achieved"

[outcomes.fail]
condition = "ratio > 0.5"
decision = "keep random baseline"
reasoning = "insufficient gain"
is_residual = true

[result_schema]
ratio = "float"
"""


def test_sidecar_without_hypothesis_block_stays_valid(tmp_path: Path):
    """[hypothesis] is opt-in; existing sidecars without it stay valid."""
    p = _write_toml(tmp_path, _BASE_EXPERIMENT)
    sc = parse_sidecar(p)
    assert sc.hypothesis_spec is None
    res = validate_sidecar(sc, sidecar_path=p)
    assert res.ok is True
    assert res.errors == []


def test_parse_and_validate_valid_hypothesis_block(tmp_path: Path):
    """Valid [hypothesis] block populates HypothesisSpec and passes validation."""
    p = _write_toml(
        tmp_path,
        _BASE_EXPERIMENT
        + """
[hypothesis]
rivals = ["Variance reduction alone explains the gain", "Hyperparameter tuning asymmetry"]
discriminating_pilot = "Run 20-seed fixed-budget pilot comparing random vs uncertainty sampling"
which_source_may_lose = "random_sampling_baseline"
pitfalls = ["Cold-start batch correlation", "Outlier sensitivity in uncertainty score"]
remediations = ["Stratify initial seed batch", "Clip top 1% uncertainty scores"]
""",
    )
    sc = parse_sidecar(p)
    assert isinstance(sc.hypothesis_spec, HypothesisSpec)
    assert sc.hypothesis_spec.rivals == [
        "Variance reduction alone explains the gain",
        "Hyperparameter tuning asymmetry",
    ]
    assert sc.hypothesis_spec.discriminating_pilot.startswith("Run 20-seed")
    assert sc.hypothesis_spec.which_source_may_lose == "random_sampling_baseline"
    assert len(sc.hypothesis_spec.pitfalls) == 2
    assert len(sc.hypothesis_spec.remediations) == 2

    res = validate_sidecar(sc, sidecar_path=p)
    assert res.ok is True
    assert res.errors == []
    assert res.warnings == []

    # Declaration round-trip
    restored = sc.from_declaration(json.loads(json.dumps(sc.to_declaration())))
    assert restored == sc


def test_hypothesis_source_may_lose_alias_and_structured_pitfalls(tmp_path: Path):
    """Supports `source_may_lose` alias and `[[hypothesis.pitfalls]]` table entries."""
    p = _write_toml(
        tmp_path,
        _BASE_EXPERIMENT
        + """
[hypothesis]
rivals = ["Null drift"]
discriminating_pilot = "Paired 10-run split test"
source_may_lose = "prior_model"

[[hypothesis.pitfalls]]
pitfall = "Seed leakage across splits"
remediation = "Enforce disjoint build manifest"
""",
    )
    sc = parse_sidecar(p)
    assert sc.hypothesis_spec is not None
    assert sc.hypothesis_spec.which_source_may_lose == "prior_model"
    assert sc.hypothesis_spec.pitfalls == ["Seed leakage across splits"]
    assert sc.hypothesis_spec.remediations == ["Enforce disjoint build manifest"]
    res = validate_sidecar(sc, sidecar_path=p)
    assert res.ok is True


def test_hypothesis_non_table_raises_sidecar_error(tmp_path: Path):
    """Non-table `[hypothesis]` fails at parse time with SidecarError."""
    p = _write_toml(
        tmp_path,
        'hypothesis = "not a table"\n' + _BASE_EXPERIMENT,
    )
    with pytest.raises(SidecarError, match="\\[hypothesis\\] must be a table"):
        parse_sidecar(p)


def test_hypothesis_rejected_on_benchmark_sidecar(tmp_path: Path):
    """[hypothesis] block is rejected on non-experiment sidecars."""
    p = _write_toml(
        tmp_path,
        """
[benchmark]
baseline_ref = "run_123"
metric = "ns_per_day"
regression_threshold = 0.05

[result_schema]
ns_per_day = "float"

[hypothesis]
rivals = ["Hardware throttling"]
discriminating_pilot = "Pin clock speed"
which_source_may_lose = "baseline"
pitfalls = ["Thermal drift"]
remediations = ["Warmup 60s"]
""",
    )
    sc = parse_sidecar(p)
    assert sc.kind == SidecarKind.BENCHMARK
    errors = validate_hypothesis_block(sc, sidecar_path=p)
    assert any(e.field == "hypothesis" for e in errors)


@pytest.mark.parametrize(
    ("block", "expected_field"),
    [
        (
            """
            [hypothesis]
            rivals = []
            discriminating_pilot = "pilot"
            which_source_may_lose = "src"
            pitfalls = ["p1"]
            remediations = ["r1"]
            """,
            "hypothesis.rivals",
        ),
        (
            """
            [hypothesis]
            rivals = "not-a-list"
            discriminating_pilot = "pilot"
            which_source_may_lose = "src"
            pitfalls = ["p1"]
            remediations = ["r1"]
            """,
            "hypothesis.rivals",
        ),
        (
            """
            [hypothesis]
            rivals = ["r1", "  "]
            discriminating_pilot = "pilot"
            which_source_may_lose = "src"
            pitfalls = ["p1"]
            remediations = ["r1"]
            """,
            "hypothesis.rivals",
        ),
        (
            """
            [hypothesis]
            rivals = ["r1"]
            discriminating_pilot = ""
            which_source_may_lose = "src"
            pitfalls = ["p1"]
            remediations = ["r1"]
            """,
            "hypothesis.discriminating_pilot",
        ),
        (
            """
            [hypothesis]
            rivals = ["r1"]
            discriminating_pilot = "pilot"
            which_source_may_lose = ""
            pitfalls = ["p1"]
            remediations = ["r1"]
            """,
            "hypothesis.which_source_may_lose",
        ),
        (
            """
            [hypothesis]
            rivals = ["r1"]
            discriminating_pilot = "pilot"
            which_source_may_lose = "src"
            pitfalls = []
            remediations = ["r1"]
            """,
            "hypothesis.pitfalls",
        ),
        (
            """
            [hypothesis]
            rivals = ["r1"]
            discriminating_pilot = "pilot"
            which_source_may_lose = "src"
            pitfalls = ["p1"]
            remediations = 123
            """,
            "hypothesis.remediations",
        ),
        (
            """
            [hypothesis]
            rivals = ["r1"]
            discriminating_pilot = "pilot"
            which_source_may_lose = "src"
            pitfalls = ["p1"]
            remediations = ["r1"]
            extra_unknown = "oops"
            """,
            "hypothesis.extra_unknown",
        ),
        (
            """
            [hypothesis]
            rivals = ["Active learning cuts sample complexity by 2x vs random sampling"]
            discriminating_pilot = "pilot"
            which_source_may_lose = "src"
            pitfalls = ["p1"]
            remediations = ["r1"]
            """,
            "hypothesis.rivals",
        ),
        (
            """
            [hypothesis]
            rivals = ["Rival A", "rival a"]
            discriminating_pilot = "pilot"
            which_source_may_lose = "src"
            pitfalls = ["p1"]
            remediations = ["r1"]
            """,
            "hypothesis.rivals",
        ),
        (
            """
            [hypothesis]
            rivals = ["Rival A"]
            discriminating_pilot = "${PILOT}"
            which_source_may_lose = "src"
            pitfalls = ["p1"]
            remediations = ["r1"]
            """,
            "hypothesis.discriminating_pilot",
        ),
    ],
)
def test_hypothesis_rejects_empty_or_malformed_fields(tmp_path: Path, block: str, expected_field: str):
    """Empty, wrong-type, duplicate, placeholder, or unknown fields inside [hypothesis] fail."""
    p = _write_toml(tmp_path, _BASE_EXPERIMENT + "\n" + textwrap.dedent(block))
    sc = parse_sidecar(p)
    res = validate_sidecar(sc, sidecar_path=p)
    assert res.ok is False
    assert any(e.field == expected_field for e in res.errors), [
        (e.field, e.message) for e in res.errors
    ]


def test_hypothesis_fewer_remediations_than_pitfalls_warns(tmp_path: Path):
    """Fewer remediations than pitfalls surfaces a non-blocking warning."""
    p = _write_toml(
        tmp_path,
        _BASE_EXPERIMENT
        + """
[hypothesis]
rivals = ["Rival A"]
discriminating_pilot = "Pilot X"
which_source_may_lose = "Source Y"
pitfalls = ["Pitfall 1", "Pitfall 2"]
remediations = ["Remediation 1"]
""",
    )
    sc = parse_sidecar(p)
    res = validate_sidecar(sc, sidecar_path=p)
    assert res.ok is True
    assert any(w.startswith("hypothesis.remediations:") for w in res.warnings)


def test_hypothesis_validation_emits_telemetry_with_path(tmp_path: Path, monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr("bathos.validate.event", lambda name, **kw: captured.append((name, kw)))

    p = _write_toml(
        tmp_path,
        _BASE_EXPERIMENT
        + """
[hypothesis]
rivals = []
discriminating_pilot = "pilot"
which_source_may_lose = "src"
pitfalls = ["p1"]
remediations = ["r1"]
""",
    )
    errors = validate_hypothesis_block(parse_sidecar(p), p)
    assert errors
    assert captured
    assert all(name == "sidecar.validate_error" for name, _ in captured)
    assert any(fields["field"] == "hypothesis.rivals" for _, fields in captured)


@pytest.mark.parametrize(
    ("block", "expected_field"),
    [
        (
            """
            [hypothesis]
            rivals = ["TODO: add rival"]
            discriminating_pilot = "pilot"
            which_source_may_lose = "src"
            pitfalls = ["p1"]
            remediations = ["r1"]
            """,
            "hypothesis.rivals",
        ),
        (
            """
            [hypothesis]
            rivals = ["Rival A"]
            discriminating_pilot = "TBD"
            which_source_may_lose = "src"
            pitfalls = ["p1"]
            remediations = ["r1"]
            """,
            "hypothesis.discriminating_pilot",
        ),
        (
            """
            [hypothesis]
            rivals = ["Rival A"]
            discriminating_pilot = "pilot"
            which_source_may_lose = "N/A"
            pitfalls = ["p1"]
            remediations = ["r1"]
            """,
            "hypothesis.which_source_may_lose",
        ),
        (
            """
            [hypothesis]
            rivals = ["Rival A"]
            discriminating_pilot = "pilot"
            which_source_may_lose = "src"
            pitfalls = ["FIXME: check drift"]
            remediations = ["r1"]
            """,
            "hypothesis.pitfalls",
        ),
        (
            """
            [hypothesis]
            rivals = ["Rival A"]
            discriminating_pilot = "pilot"
            which_source_may_lose = "src"
            pitfalls = ["p1"]
            remediations = ["<fill remediation>"]
            """,
            "hypothesis.remediations",
        ),
        (
            """
            [hypothesis]
            rivals = ["Rival A.", "rival a!"]
            discriminating_pilot = "pilot"
            which_source_may_lose = "src"
            pitfalls = ["p1"]
            remediations = ["r1"]
            """,
            "hypothesis.rivals",
        ),
        (
            """
            [hypothesis]
            rivals = ["Rival-A", "Rival A"]
            discriminating_pilot = "pilot"
            which_source_may_lose = "src"
            pitfalls = ["p1"]
            remediations = ["r1"]
            """,
            "hypothesis.rivals",
        ),
        (
            """
            [hypothesis]
            rivals = ["Active learning cuts sample complexity by 2x vs random sampling."]
            discriminating_pilot = "pilot"
            which_source_may_lose = "src"
            pitfalls = ["p1"]
            remediations = ["r1"]
            """,
            "hypothesis.rivals",
        ),
    ],
)
def test_hypothesis_rejects_placeholders_and_punct_duplicates(
    tmp_path: Path, block: str, expected_field: str
):
    p = _write_toml(tmp_path, _BASE_EXPERIMENT + "\n" + textwrap.dedent(block))
    res = validate_sidecar(parse_sidecar(p), sidecar_path=p)
    assert res.ok is False
    assert any(e.field == expected_field for e in res.errors), [
        (e.field, e.message) for e in res.errors
    ]


def test_hypothesis_rejects_placeholder_in_experiment_hypothesis_and_allows_inequalities(tmp_path: Path):
    p_bad = _write_toml(
        tmp_path,
        _BASE_EXPERIMENT.replace(
            "Active learning cuts sample complexity by 2x vs random sampling",
            "${HYPOTHESIS}",
        )
        + """
[hypothesis]
rivals = ["Rival A"]
discriminating_pilot = "pilot"
which_source_may_lose = "src"
pitfalls = ["p1"]
remediations = ["r1"]
""",
    )
    res_bad = validate_sidecar(parse_sidecar(p_bad), sidecar_path=p_bad)
    assert res_bad.ok is False
    assert any(e.field == "hypothesis" for e in res_bad.errors)

    p_ok = _write_toml(
        tmp_path,
        _BASE_EXPERIMENT
        + """
[hypothesis]
rivals = ["Baseline reaches loss < 0.5 and accuracy > 0.9 without active sampling"]
discriminating_pilot = "Check when loss < 0.5 and acc > 0.9 across 10 seeds"
which_source_may_lose = "random_baseline"
pitfalls = ["Variance when n < 20 and p > 0.05"]
remediations = ["Increase n >= 50"]
""",
        name="ok_ineq.bth.toml",
    )
    res_ok = validate_sidecar(parse_sidecar(p_ok), sidecar_path=p_ok)
    assert res_ok.ok is True, [e.message for e in res_ok.errors]
