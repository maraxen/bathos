"""Tests for POPPER e-value sequential campaign primitives (#792)."""
import textwrap
import uuid
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from bathos.catalog import init_catalog, write_run
from bathos.campaigns import (
    Campaign,
    CampaignError,
    _campaign_threshold_met,
    add_run_to_campaign,
    conclude_campaign,
    create_campaign,
    review_campaign,
)
from bathos.compact import compact
from bathos.schema import Run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_popper_sidecar(tmp_path: Path, null: float, alt: float, threshold: float, extra: str = "") -> Path:
    """Write a minimal experiment sidecar with a [popper] block to disk."""
    content = textwrap.dedent(f"""
        [experiment]
        hypothesis = "test hypothesis"
        [outcomes.pass]
        condition = "x > 0"
        decision = "proceed"
        reasoning = "good"
        is_residual = false
        [outcomes.fail]
        condition = "x <= 0"
        decision = "debug"
        reasoning = "bad"
        is_residual = true
        [result_schema]
        x = "float"
        [popper]
        null_pass_rate = {null}
        alt_pass_rate = {alt}
        stopping_threshold = {threshold}
        {extra}
    """)
    p = tmp_path / f"sidecar_{uuid.uuid4().hex[:8]}.bth.toml"
    p.write_text(content)
    return p


def _insert_run(db, run_id: str, outcome: str, sidecar_path: str | None = None):
    """Insert a run row directly into the warm DB, bypassing cool-tier."""
    db.execute(
        """
        INSERT INTO runs (id, project_slug, command, argv, git_hash, git_branch, git_dirty,
                          timestamp, duration_s, exit_code, status, output_paths, tags,
                          schema_version, outcome, sidecar_path, script_sha256)
        VALUES (?, 'test', 'python x.py', ['python', 'x.py'], 'abc', 'main', false,
                current_timestamp, 1.0, 0, 'completed', [], [], '6', ?, ?, '')
        """,
        [run_id, outcome, sidecar_path or ""],
    )


@pytest.fixture
def warm_db(tmp_path, tmp_catalog):
    """Return an open DuckDB connection to an initialized warm catalog."""
    init_catalog(tmp_catalog)
    r = Run(
        project_slug="test",
        command="python x.py",
        argv=["python", "x.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
    )
    write_run(r, tmp_catalog)
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    yield db
    db.close()


# ---------------------------------------------------------------------------
# Campaign creation
# ---------------------------------------------------------------------------


def test_create_sequential_campaign(warm_db):
    """create_campaign with mode='sequential' succeeds and stores mode correctly."""
    campaign = create_campaign(warm_db, name="Seq Test", project_slug="test", mode="sequential")
    assert campaign.mode == "sequential"

    rows = warm_db.execute(
        "SELECT mode FROM campaigns WHERE id = ?", [campaign.id]
    ).fetchall()
    assert rows[0][0] == "sequential"


def test_create_campaign_invalid_mode(warm_db):
    """create_campaign with an unrecognised mode raises CampaignError."""
    with pytest.raises(CampaignError):
        create_campaign(warm_db, name="Bad", project_slug="test", mode="nonsense")


# ---------------------------------------------------------------------------
# E-value computation via add_run_to_campaign
# ---------------------------------------------------------------------------


def test_add_run_sequential_computes_evalue(tmp_path, warm_db):
    """Sequential campaign computes e-value 2.5 for a 'pass' run (null=0.3, alt=0.75)."""
    sidecar_path = _write_popper_sidecar(tmp_path, null=0.30, alt=0.75, threshold=20.0)
    campaign = create_campaign(warm_db, name="Evalue Test", project_slug="test", mode="sequential")

    run_id = str(uuid.uuid4())
    _insert_run(warm_db, run_id, "pass", str(sidecar_path))

    add_run_to_campaign(warm_db, campaign.id, run_id)

    row = warm_db.execute(
        "SELECT evalue, seq_position FROM campaign_runs WHERE campaign_id = ? AND run_id = ?",
        [campaign.id, run_id],
    ).fetchone()
    assert row is not None
    evalue, seq_position = row
    assert abs(evalue - 2.5) < 1e-6
    assert seq_position == 1


def test_add_run_non_sequential_evalue_null(warm_db):
    """Non-sequential campaign stores NULL for evalue and seq_position."""
    campaign = create_campaign(warm_db, name="Exploration", project_slug="test", mode="exploration")

    run_id = str(uuid.uuid4())
    _insert_run(warm_db, run_id, "pass")

    add_run_to_campaign(warm_db, campaign.id, run_id)

    row = warm_db.execute(
        "SELECT evalue, seq_position FROM campaign_runs WHERE campaign_id = ? AND run_id = ?",
        [campaign.id, run_id],
    ).fetchone()
    assert row is not None
    evalue, seq_position = row
    assert evalue is None
    assert seq_position is None


# ---------------------------------------------------------------------------
# seq_position monotonically increments
# ---------------------------------------------------------------------------


def test_seq_position_increments(tmp_path, warm_db):
    """Three sequential runs get seq_positions 1, 2, 3 in insertion order."""
    sidecar_path = _write_popper_sidecar(tmp_path, null=0.30, alt=0.75, threshold=20.0)
    campaign = create_campaign(warm_db, name="Pos Test", project_slug="test", mode="sequential")

    run_ids = [str(uuid.uuid4()) for _ in range(3)]
    for run_id in run_ids:
        _insert_run(warm_db, run_id, "pass", str(sidecar_path))
        add_run_to_campaign(warm_db, campaign.id, run_id)

    rows = warm_db.execute(
        "SELECT seq_position FROM campaign_runs WHERE campaign_id = ? ORDER BY seq_position",
        [campaign.id],
    ).fetchall()
    positions = [r[0] for r in rows]
    assert positions == [1, 2, 3]


# ---------------------------------------------------------------------------
# Threshold lock
# ---------------------------------------------------------------------------


def test_threshold_lock_fires(tmp_path, warm_db):
    """Second run with different stopping_threshold raises CampaignError (threshold locked)."""
    sidecar_a = _write_popper_sidecar(tmp_path, null=0.30, alt=0.75, threshold=20.0)
    sidecar_b = _write_popper_sidecar(tmp_path, null=0.30, alt=0.75, threshold=50.0)

    campaign = create_campaign(warm_db, name="Lock Test", project_slug="test", mode="sequential")

    run_a = str(uuid.uuid4())
    _insert_run(warm_db, run_a, "pass", str(sidecar_a))
    add_run_to_campaign(warm_db, campaign.id, run_a)

    # Threshold is now locked at 20.0; adding a run with threshold=50.0 must fail
    run_b = str(uuid.uuid4())
    _insert_run(warm_db, run_b, "pass", str(sidecar_b))
    with pytest.raises(CampaignError):
        add_run_to_campaign(warm_db, campaign.id, run_b)


def test_error_runs_dont_lock_threshold(tmp_path, warm_db):
    """Error-outcome runs do not lock the stopping_threshold."""
    sidecar_error = _write_popper_sidecar(tmp_path, null=0.30, alt=0.75, threshold=20.0)
    sidecar_pass = _write_popper_sidecar(tmp_path, null=0.30, alt=0.75, threshold=30.0)

    campaign = create_campaign(warm_db, name="Error Lock", project_slug="test", mode="sequential")

    # Add an error-outcome run — should NOT lock threshold
    run_e = str(uuid.uuid4())
    _insert_run(warm_db, run_e, "error", str(sidecar_error))
    add_run_to_campaign(warm_db, campaign.id, run_e)

    threshold_after_error = warm_db.execute(
        "SELECT stopping_threshold FROM campaigns WHERE id = ?", [campaign.id]
    ).fetchone()[0]
    assert threshold_after_error is None  # Still unlocked

    # Now add a non-error run with a different threshold — should lock at 30.0
    run_p = str(uuid.uuid4())
    _insert_run(warm_db, run_p, "pass", str(sidecar_pass))
    add_run_to_campaign(warm_db, campaign.id, run_p)

    threshold_after_pass = warm_db.execute(
        "SELECT stopping_threshold FROM campaigns WHERE id = ?", [campaign.id]
    ).fetchone()[0]
    assert threshold_after_pass == 30.0


# ---------------------------------------------------------------------------
# _campaign_threshold_met
# ---------------------------------------------------------------------------


def test_campaign_threshold_met_true(tmp_path, warm_db):
    """After 4 pass runs (e-value 2.5 each), E_n = 2.5^4 = 39.0625 >= 20.0 → True."""
    sidecar_path = _write_popper_sidecar(tmp_path, null=0.30, alt=0.75, threshold=20.0)
    campaign = create_campaign(warm_db, name="Met True", project_slug="test", mode="sequential")

    for _ in range(4):
        run_id = str(uuid.uuid4())
        _insert_run(warm_db, run_id, "pass", str(sidecar_path))
        add_run_to_campaign(warm_db, campaign.id, run_id)

    assert _campaign_threshold_met(warm_db, campaign.id, 20.0) is True


def test_campaign_threshold_not_met(tmp_path, warm_db):
    """After 2 pass runs (e-value 2.5 each), E_n = 2.5^2 = 6.25 < 20.0 → False."""
    sidecar_path = _write_popper_sidecar(tmp_path, null=0.30, alt=0.75, threshold=20.0)
    campaign = create_campaign(warm_db, name="Not Met", project_slug="test", mode="sequential")

    for _ in range(2):
        run_id = str(uuid.uuid4())
        _insert_run(warm_db, run_id, "pass", str(sidecar_path))
        add_run_to_campaign(warm_db, campaign.id, run_id)

    assert _campaign_threshold_met(warm_db, campaign.id, 20.0) is False


# ---------------------------------------------------------------------------
# Marginal e-value
# ---------------------------------------------------------------------------


def test_marginal_evalue_is_1(tmp_path, warm_db):
    """'marginal' outcome stores e-value == 1.0."""
    # Sidecar with a declared marginal outcome
    content = textwrap.dedent("""
        [experiment]
        hypothesis = "test"
        [outcomes.pass]
        condition = "x > 1"
        decision = "ok"
        reasoning = "good"
        is_residual = false
        [outcomes.marginal]
        condition = "x == 1"
        decision = "review"
        reasoning = "borderline"
        is_residual = false
        [outcomes.fail]
        condition = "x < 1"
        decision = "debug"
        reasoning = "bad"
        is_residual = true
        [result_schema]
        x = "float"
        [popper]
        null_pass_rate = 0.30
        alt_pass_rate = 0.75
        stopping_threshold = 20.0
    """)
    sidecar_path = tmp_path / "marginal.bth.toml"
    sidecar_path.write_text(content)

    campaign = create_campaign(warm_db, name="Marginal", project_slug="test", mode="sequential")
    run_id = str(uuid.uuid4())
    _insert_run(warm_db, run_id, "marginal", str(sidecar_path))
    add_run_to_campaign(warm_db, campaign.id, run_id)

    row = warm_db.execute(
        "SELECT evalue FROM campaign_runs WHERE campaign_id = ? AND run_id = ?",
        [campaign.id, run_id],
    ).fetchone()
    assert row is not None
    assert abs(row[0] - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# review_campaign — POPPER section
# ---------------------------------------------------------------------------


def test_evalue_product_calculation(tmp_path, warm_db):
    """3 pass runs with null=0.4, alt=0.8: E_n product = 2.0^3 = 8.0."""
    sidecar_path = _write_popper_sidecar(tmp_path, null=0.4, alt=0.8, threshold=20.0)
    campaign = create_campaign(warm_db, name="Product", project_slug="test", mode="sequential")

    for _ in range(3):
        run_id = str(uuid.uuid4())
        _insert_run(warm_db, run_id, "pass", str(sidecar_path))
        add_run_to_campaign(warm_db, campaign.id, run_id)

    review = review_campaign(warm_db, campaign.id)
    assert review.get("popper") is not None
    scripts = review["popper"]["scripts"]
    assert len(scripts) >= 1
    assert abs(scripts[0]["evalue_product"] - 8.0) < 1e-6


def test_review_campaign_popper_key_present(tmp_path, warm_db):
    """review_campaign on a sequential campaign returns a 'popper' dict with expected keys."""
    sidecar_path = _write_popper_sidecar(tmp_path, null=0.30, alt=0.75, threshold=20.0)
    campaign = create_campaign(warm_db, name="Review Popper", project_slug="test", mode="sequential")

    run_id = str(uuid.uuid4())
    _insert_run(warm_db, run_id, "pass", str(sidecar_path))
    add_run_to_campaign(warm_db, campaign.id, run_id)

    review = review_campaign(warm_db, campaign.id)
    assert "popper" in review
    popper = review["popper"]
    assert popper is not None
    for key in ("mode", "stopping_threshold", "threshold_met", "scripts"):
        assert key in popper, f"Missing key: {key}"


def test_review_campaign_popper_none_for_exploration(warm_db):
    """review_campaign on an exploration campaign returns popper=None."""
    campaign = create_campaign(warm_db, name="Explore", project_slug="test", mode="exploration")

    run_id = str(uuid.uuid4())
    _insert_run(warm_db, run_id, "pass")
    add_run_to_campaign(warm_db, campaign.id, run_id)

    review = review_campaign(warm_db, campaign.id)
    assert review.get("popper") is None


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


def test_backward_compat_existing_campaign_row(warm_db):
    """Existing campaigns rows have stopping_threshold = NULL (no error querying it)."""
    # Create a campaign via the normal path (stopping_threshold defaults to NULL)
    campaign = create_campaign(warm_db, name="Compat", project_slug="test", mode="exploration")

    row = warm_db.execute(
        "SELECT stopping_threshold FROM campaigns WHERE id = ?", [campaign.id]
    ).fetchone()
    assert row is not None
    assert row[0] is None
