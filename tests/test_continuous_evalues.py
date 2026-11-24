"""Tests for continuous metric POPPER e-values (deferred:continuous-evalues)."""

from __future__ import annotations

import json
import math
import random
import textwrap
import uuid
from pathlib import Path

import duckdb
import pytest

from bathos.campaigns import (
    _campaign_threshold_met,
    add_run_to_campaign,
    create_campaign,
    link_cool_runs_to_campaigns,
    review_campaign,
)
from bathos.catalog import init_catalog, write_run
from bathos.compact import compact
from bathos.schema import Run
from bathos.sidecar import (
    ContinuousPopperSpec,
    OutcomeSpec,
    Sidecar,
    SidecarError,
    SidecarKind,
    compute_continuous_evalue,
    compute_evalue,
    parse_sidecar,
)
from bathos.validate import validate_popper_block, validate_sidecar


def _write_toml(tmp_path: Path, content: str, name: str = "cont.bth.toml") -> Path:
    p = tmp_path / name
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


_CONT_SIDECAR = """
[experiment]
hypothesis = "Mean score improves from 0.5 to 0.7"

[outcomes.pass]
condition = "score >= 0.6"
decision = "ship"
reasoning = "above midpoint"

[outcomes.fail]
condition = "score < 0.6"
decision = "iterate"
reasoning = "below midpoint"
is_residual = true

[result_schema]
score = "float"

[popper]
stopping_threshold = 20.0

[popper.continuous]
metric = "score"
null_mean = 0.5
alt_mean = 0.7
std = 0.1
method = "gaussian"
"""


def test_parse_and_roundtrip_continuous_popper(tmp_path: Path):
    """[popper.continuous] parses cleanly without discrete pass rates and round-trips."""
    p = _write_toml(tmp_path, _CONT_SIDECAR)
    sc = parse_sidecar(p)
    assert sc.popper_null_pass_rate is None
    assert sc.popper_stopping_threshold == 20.0
    assert sc.popper_continuous == ContinuousPopperSpec(
        metric="score",
        null_mean=0.5,
        alt_mean=0.7,
        std=0.1,
        null_std=None,
        alt_std=None,
        method="gaussian",
        unknown_keys=[],
    )
    res = validate_sidecar(sc, sidecar_path=p)
    assert res.ok is True, [e.message for e in res.errors]

    restored = Sidecar.from_declaration(json.loads(json.dumps(sc.to_declaration())))
    assert restored == sc


def test_parse_continuous_non_numeric_raises_sidecar_error(tmp_path: Path):
    p = _write_toml(
        tmp_path,
        _CONT_SIDECAR.replace("std = 0.1", "std = true"),
    )
    with pytest.raises(SidecarError, match="\\[popper\\.continuous\\]\\.std"):
        parse_sidecar(p)


def test_compute_continuous_evalue_gaussian_and_subgaussian():
    spec_g = ContinuousPopperSpec(
        metric="score", null_mean=0.5, alt_mean=0.7, std=0.1, method="gaussian"
    )
    # At midpoint 0.6, LR = 1.0
    assert compute_continuous_evalue(spec_g, 0.6) == pytest.approx(1.0)
    # At alt_mean 0.7, exponent = 0.2 * 0.1 / 0.01 = 2.0
    assert compute_continuous_evalue(spec_g, 0.7) == pytest.approx(math.exp(2.0))
    # At null_mean 0.5, exponent = 0.2 * (-0.1) / 0.01 = -2.0
    assert compute_continuous_evalue(spec_g, 0.5) == pytest.approx(math.exp(-2.0))

    # Subgaussian matches exponential MGF form
    spec_sg = ContinuousPopperSpec(
        metric="score", null_mean=0.5, alt_mean=0.7, std=0.1, method="subgaussian"
    )
    assert compute_continuous_evalue(spec_sg, 0.7) == pytest.approx(math.exp(2.0))

    # Heteroscedastic Gaussian likelihood ratio (null_std != alt_std)
    spec_het = ContinuousPopperSpec(
        metric="score", null_mean=0.0, alt_mean=1.0, null_std=1.0, alt_std=2.0, method="gaussian"
    )
    expected_at_1 = (1.0 / 2.0) * math.exp((1.0**2) / 2.0 - 0.0)
    assert compute_continuous_evalue(spec_het, 1.0) == pytest.approx(expected_at_1)


def test_compute_continuous_evalue_null_expectation_is_one():
    """Monte Carlo verification that E_{H0}[E(X)] ~= 1.0 for Gaussian LR."""
    rng = random.Random(42)
    spec = ContinuousPopperSpec(
        metric="x", null_mean=0.0, alt_mean=0.5, null_std=1.0, alt_std=1.2, method="gaussian"
    )
    samples = [compute_continuous_evalue(spec, rng.gauss(0.0, 1.0)) for _ in range(20000)]
    mean_e = sum(samples) / len(samples)
    assert 0.95 <= mean_e <= 1.05


def test_compute_continuous_evalue_clamps_extremes_and_rejects_bad_inputs():
    spec = ContinuousPopperSpec(metric="x", null_mean=0.0, alt_mean=1.0, std=0.1)
    huge = compute_continuous_evalue(spec, 1e6)
    tiny = compute_continuous_evalue(spec, -1e6)
    assert math.isfinite(huge) and huge > 0
    assert math.isfinite(tiny) and tiny > 0

    for bad in (float("nan"), float("inf"), True, "1.0"):
        with pytest.raises(ValueError):
            compute_continuous_evalue(spec, bad)  # type: ignore[arg-type]


def test_compute_evalue_continuous_and_backward_compat():
    sc = Sidecar(
        kind=SidecarKind.EXPERIMENT,
        result_schema={"score": "float"},
        outcomes={
            "pass": OutcomeSpec(condition="score >= 0.6", decision="ship", reasoning="ok"),
            "fail": OutcomeSpec(
                condition="score < 0.6", decision="stop", reasoning="bad", is_residual=True
            ),
        },
        hypothesis="h",
        popper_null_pass_rate=0.25,
        popper_alt_pass_rate=0.75,
        popper_stopping_threshold=20.0,
        popper_continuous=ContinuousPopperSpec(
            metric="score", null_mean=0.5, alt_mean=0.7, std=0.1, method="gaussian"
        ),
    )
    # When result_values supplies the metric, continuous e-value is used
    assert compute_evalue(sc, "pass", result_values={"score": 0.7}) == pytest.approx(math.exp(2.0))
    # Neutral outcomes always return 1.0 even with result_values
    for neutral in ("error", "unknown", "observed", "invalid_measurement", "invariant_fail"):
        assert compute_evalue(sc, neutral, result_values={"score": 0.7}) == 1.0
    # Without result_values, discrete fallback is preserved 100%
    assert compute_evalue(sc, "pass") == pytest.approx(3.0)
    assert compute_evalue(sc, "fail") == pytest.approx(0.25 / 0.75)


@pytest.mark.parametrize(
    ("patch", "expected_field"),
    [
        (('metric = "score"', 'metric = "missing_col"'), "popper.continuous.metric"),
        (('score = "float"', 'score = "int"'), "popper.continuous.metric"),
        (('method = "gaussian"', 'method = "cauchy"'), "popper.continuous.method"),
        (("std = 0.1", "std = -0.5"), "popper.continuous.std"),
        (("alt_mean = 0.7", "alt_mean = 0.5"), "popper.continuous"),
        (("stopping_threshold = 20.0", "stopping_threshold = 0.5"), "popper.stopping_threshold"),
        (
            ('method = "gaussian"', 'method = "subgaussian"\nnull_std = 0.1\nalt_std = 0.2'),
            "popper.continuous.alt_std",
        ),
    ],
)
def test_validate_popper_continuous_rejects_invalid_specs(
    tmp_path: Path, patch: tuple[str, str], expected_field: str
):
    old, new = patch
    p = _write_toml(tmp_path, _CONT_SIDECAR.replace(old, new))
    errors = validate_popper_block(parse_sidecar(p), p)
    assert any(expected_field in e.field for e in errors), [(e.field, e.message) for e in errors]


def test_campaigns_sequential_continuous_evalues_warm_and_compact(tmp_path: Path, tmp_catalog: Path):
    """add_run_to_campaign and link_cool_runs_to_campaigns compute continuous e-values from metadata."""
    sidecar_path = _write_toml(tmp_path, _CONT_SIDECAR)
    init_catalog(tmp_catalog)
    r_init = Run(
        project_slug="test",
        command="python x.py",
        argv=["python", "x.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
    )
    write_run(r_init, tmp_catalog)
    compact(tmp_catalog)

    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        campaign = create_campaign(db, name="Continuous Seq", project_slug="test", mode="sequential")
        # Two passing runs with score=0.7 -> each e = exp(2) ~= 7.389 -> E_2 = exp(4) ~= 54.6 >= 20
        for _ in range(2):
            rid = str(uuid.uuid4())
            db.execute(
                """
                INSERT INTO runs (id, project_slug, command, argv, git_hash, git_branch, git_dirty,
                                  timestamp, duration_s, exit_code, status, output_paths, tags,
                                  schema_version, outcome, sidecar_path, script_sha256, metadata)
                VALUES (?, 'test', 'python x.py', ['python', 'x.py'], 'abc', 'main', false,
                        current_timestamp, 1.0, 0, 'completed', [], [], '20', 'pass', ?, 'sha1', ?)
                """,
                [rid, str(sidecar_path), json.dumps({"score": 0.7})],
            )
            add_run_to_campaign(db, campaign.id, rid)

        # Error run remains neutral (1.0) even with score=0.7
        err_rid = str(uuid.uuid4())
        db.execute(
            """
            INSERT INTO runs (id, project_slug, command, argv, git_hash, git_branch, git_dirty,
                              timestamp, duration_s, exit_code, status, output_paths, tags,
                              schema_version, outcome, sidecar_path, script_sha256, metadata)
            VALUES (?, 'test', 'python x.py', ['python', 'x.py'], 'abc', 'main', false,
                    current_timestamp, 1.0, 1, 'failed', [], [], '20', 'error', ?, 'sha1', ?)
            """,
            [err_rid, str(sidecar_path), json.dumps({"score": 0.7})],
        )
        add_run_to_campaign(db, campaign.id, err_rid)

        rows = db.execute(
            "SELECT evalue FROM campaign_runs WHERE campaign_id = ? ORDER BY seq_position",
            [campaign.id],
        ).fetchall()
        assert rows[0][0] == pytest.approx(math.exp(2.0), rel=1e-6)
        assert rows[1][0] == pytest.approx(math.exp(2.0), rel=1e-6)
        assert rows[2][0] == pytest.approx(1.0)
        assert _campaign_threshold_met(db, campaign.id, 20.0) is True

        review = review_campaign(db, campaign.id)
        assert review["popper"]["scripts"][0]["evalue_product"] == pytest.approx(
            math.exp(4.0), rel=1e-5
        )

        # Cool-run linking path also passes metadata into continuous e-value computation
        c2 = create_campaign(db, name="Cool Link Seq", project_slug="test", mode="sequential")
    finally:
        db.close()

    cool_run = Run(
        project_slug="test",
        command="python x.py",
        argv=["python", "x.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        outcome="pass",
        sidecar_path=str(sidecar_path),
        campaign_id=c2.id,
        metadata=json.dumps({"score": 0.7}),
    )
    write_run(cool_run, tmp_catalog)
    compact(tmp_catalog)

    db2 = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        link_cool_runs_to_campaigns(db2, [cool_run], catalog_dir=tmp_catalog, campaign_id=c2.id)
        linked = db2.execute(
            "SELECT evalue FROM campaign_runs WHERE campaign_id = ? AND run_id = ?",
            [c2.id, cool_run.id],
        ).fetchone()
        assert linked is not None
        assert linked[0] == pytest.approx(math.exp(2.0), rel=1e-6)
    finally:
        db2.close()
