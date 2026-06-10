from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bathos.catalog import init_catalog, write_run
from bathos.campaigns import (
    CampaignError,
    add_run_to_campaign,
    conclude_campaign,
    create_campaign,
    get_campaign,
    list_campaigns,
    review_campaign,
)
from bathos.compact import compact
from bathos.schema import Run
import duckdb


@pytest.fixture
def populated_warm_catalog(tmp_catalog: Path) -> Path:
    """Create a catalog with runs and campaign tables."""
    init_catalog(tmp_catalog)
    base = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    for i, (proj, status) in enumerate(
        [
            ("prolix", "completed"),
            ("prolix", "failed"),
            ("espaloma", "completed"),
        ]
    ):
        r = Run(
            project_slug=proj,
            command=f"python run_{i}.py",
            argv=["python", f"run_{i}.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            timestamp=base + timedelta(hours=i),
            status=status,
            exit_code=0 if status == "completed" else 1,
        )
        write_run(r, tmp_catalog)
    compact(tmp_catalog)
    return tmp_catalog


def test_create_campaign_stores_to_db(populated_warm_catalog: Path):
    """Test that create_campaign stores campaign to DB."""
    db = duckdb.connect(str(populated_warm_catalog / "bathos.db"))
    try:
        campaign = create_campaign(db, name="Test Campaign", project_slug="prolix", mode="exploration")
        assert campaign.name == "Test Campaign"
        assert campaign.mode == "exploration"
        assert campaign.status == "open"

        # Verify stored in DB
        rows = db.execute("SELECT id, name, mode, status FROM campaigns WHERE id = ?", [campaign.id]).fetchall()
        assert len(rows) == 1
        assert rows[0][1] == "Test Campaign"
        assert rows[0][2] == "exploration"
        assert rows[0][3] == "open"
    finally:
        db.close()


def test_add_run_to_campaign_idempotent(populated_warm_catalog: Path):
    """Test that adding same run twice to campaign is idempotent."""
    db = duckdb.connect(str(populated_warm_catalog / "bathos.db"))
    try:
        campaign = create_campaign(db, name="Test", project_slug="prolix", mode="exploration")

        # Get a run
        runs = db.execute("SELECT id FROM runs WHERE project_slug = 'prolix' LIMIT 1").fetchall()
        run_id = runs[0][0]

        # Add same run twice
        add_run_to_campaign(db, campaign.id, run_id)
        add_run_to_campaign(db, campaign.id, run_id)

        # Verify only one row in campaign_runs
        rows = db.execute("SELECT COUNT(*) FROM campaign_runs WHERE campaign_id = ? AND run_id = ?", [campaign.id, run_id]).fetchall()
        assert rows[0][0] == 1
    finally:
        db.close()


def test_conclude_campaign_updates_status(populated_warm_catalog: Path):
    """Test that conclude_campaign updates status and outcome."""
    db = duckdb.connect(str(populated_warm_catalog / "bathos.db"))
    try:
        campaign = create_campaign(db, name="Test", project_slug="prolix", mode="exploration")
        conclude_campaign(db, campaign.id, "pass", "All tests passed")

        # Verify updated in DB
        rows = db.execute("SELECT status, outcome_label, conclusion FROM campaigns WHERE id = ?", [campaign.id]).fetchall()
        assert rows[0][0] == "concluded"
        assert rows[0][1] == "pass"
        assert rows[0][2] == "All tests passed"

        # Verify Campaign dataclass attribute round-trips correctly
        campaign_reloaded = get_campaign(db, campaign.id)
        assert campaign_reloaded is not None
        assert campaign_reloaded.conclusion == "All tests passed"
    finally:
        db.close()


def test_confirmation_campaign_rejects_prior_run(populated_warm_catalog: Path):
    """Test that confirmation campaign rejects runs with timestamp < campaign start."""
    db = duckdb.connect(str(populated_warm_catalog / "bathos.db"))
    try:
        # Create campaign at T1
        campaign = create_campaign(db, name="Confirmation Test", project_slug="prolix", mode="confirmation")
        campaign_start = campaign.started_at

        # Insert a run with prior timestamp directly into DB
        old_timestamp = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC).isoformat()
        prior_run = Run(
            id="prior_run_id",
            project_slug="prolix",
            command="python run_prior.py",
            argv=["python", "run_prior.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            timestamp=datetime.fromisoformat(old_timestamp.replace('Z', '+00:00')),
            status="completed",
            exit_code=0,
        )
        db.execute("""
            INSERT INTO runs (id, project_slug, command, argv, git_hash, git_branch, git_dirty, timestamp, status, exit_code, duration_s)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [prior_run.id, prior_run.project_slug, prior_run.command, prior_run.argv, prior_run.git_hash,
              prior_run.git_branch, prior_run.git_dirty, old_timestamp, prior_run.status, prior_run.exit_code, 0.0])

        # Try to add prior run to confirmation campaign — should fail
        with pytest.raises(CampaignError):
            add_run_to_campaign(db, campaign.id, "prior_run_id")
    finally:
        db.close()


def test_exploration_campaign_allows_prior_run(populated_warm_catalog: Path):
    """Test that exploration campaign allows any timestamp."""
    db = duckdb.connect(str(populated_warm_catalog / "bathos.db"))
    try:
        # Create exploration campaign
        campaign = create_campaign(db, name="Exploration Test", project_slug="prolix", mode="exploration")

        # Insert a run with prior timestamp
        old_timestamp = datetime(2026, 5, 9, 12, 0, 0, tzinfo=UTC).isoformat()
        db.execute("""
            INSERT INTO runs (id, project_slug, command, argv, git_hash, git_branch, git_dirty, timestamp, status, exit_code, duration_s)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, ["prior_run_id", "prolix", "python run.py", ["python", "run.py"], "abc",
              "main", False, old_timestamp, "completed", 0, 0.0])

        # Should succeed without error
        add_run_to_campaign(db, campaign.id, "prior_run_id")

        # Verify added
        rows = db.execute("SELECT COUNT(*) FROM campaign_runs WHERE campaign_id = ? AND run_id = ?", [campaign.id, "prior_run_id"]).fetchall()
        assert rows[0][0] == 1
    finally:
        db.close()


def test_list_campaigns_with_status_filter(populated_warm_catalog: Path):
    """Test list_campaigns filters by status."""
    db = duckdb.connect(str(populated_warm_catalog / "bathos.db"))
    try:
        # Create open campaign
        open_campaign = create_campaign(db, name="Open", project_slug="prolix", mode="exploration")

        # Create and conclude another
        concluded_campaign = create_campaign(db, name="Concluded", project_slug="prolix", mode="exploration")
        conclude_campaign(db, concluded_campaign.id, "pass", "Done")

        # List all
        all_campaigns = list_campaigns(db)
        assert len(all_campaigns) >= 2

        # List only open
        open_only = list_campaigns(db, status="open")
        assert all(c.status == "open" for c in open_only)
        assert any(c.id == open_campaign.id for c in open_only)

        # List only concluded
        concluded_only = list_campaigns(db, status="concluded")
        assert all(c.status == "concluded" for c in concluded_only)
    finally:
        db.close()


def test_review_campaign_computes_rates(populated_warm_catalog: Path):
    """Test review_campaign computes residual/bypass/unknown rates."""
    db = duckdb.connect(str(populated_warm_catalog / "bathos.db"))
    try:
        campaign = create_campaign(db, name="Review Test", project_slug="prolix", mode="exploration")

        # Get runs and add to campaign
        runs = db.execute("SELECT id FROM runs WHERE project_slug = 'prolix'").fetchall()
        for (run_id,) in runs:
            add_run_to_campaign(db, campaign.id, run_id)

        # Review
        review = review_campaign(db, campaign.id)
        assert "error" not in review
        assert review["total_runs"] >= 2
        assert "residual_rate" in review
        assert "bypass_rate" in review
        assert "unknown_rate" in review
        assert "outcome_distribution" in review
    finally:
        db.close()


def test_compact_populates_campaign_runs(tmp_catalog: Path):
    """Test that compact() populates campaign_runs from runs with campaign_id."""
    init_catalog(tmp_catalog)

    # Create runs with campaign_id
    base = datetime(2026, 5, 10, 12, 0, 0, tzinfo=UTC)
    campaign_id = "test_campaign_123"
    for i in range(2):
        r = Run(
            project_slug="testproj",
            command=f"python run_{i}.py",
            argv=["python", f"run_{i}.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            timestamp=base + timedelta(hours=i),
            status="completed",
            exit_code=0,
            campaign_id=campaign_id,
        )
        write_run(r, tmp_catalog)

    # Compact
    result = compact(tmp_catalog)
    assert result.ingested >= 2

    # Verify campaign_runs populated
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        rows = db.execute("SELECT COUNT(*) FROM campaign_runs WHERE campaign_id = ?", [campaign_id]).fetchall()
        assert rows[0][0] == 2
    finally:
        db.close()


def test_get_campaign_returns_none_for_unknown(populated_warm_catalog: Path):
    """Test get_campaign returns None for non-existent campaign."""
    db = duckdb.connect(str(populated_warm_catalog / "bathos.db"), read_only=True)
    try:
        campaign = get_campaign(db, "nonexistent_id")
        assert campaign is None
    finally:
        db.close()


def test_create_campaign_with_parent(populated_warm_catalog: Path):
    """Test create_campaign with parent_campaign_id."""
    db = duckdb.connect(str(populated_warm_catalog / "bathos.db"))
    try:
        parent = create_campaign(db, name="Parent", project_slug="prolix", mode="exploration")
        child = create_campaign(db, name="Child", project_slug="prolix", mode="exploration", parent_campaign_id=parent.id)

        assert child.parent_campaign_id == parent.id

        # Verify stored
        rows = db.execute("SELECT parent_campaign_id FROM campaigns WHERE id = ?", [child.id]).fetchall()
        assert rows[0][0] == parent.id
    finally:
        db.close()


def test_conclude_campaign_accepts_short_id(populated_warm_catalog: Path):
    """conclude_campaign should accept 8-char short IDs as displayed by bth campaign ls."""
    db = duckdb.connect(str(populated_warm_catalog / "bathos.db"))
    try:
        campaign = create_campaign(db, name="Short ID Test", project_slug="prolix", mode="exploration")
        short_id = campaign.id[:8]
        conclude_campaign(db, short_id, "pass", "concluded via short ID")

        rows = db.execute("SELECT status, outcome_label FROM campaigns WHERE id = ?", [campaign.id]).fetchall()
        assert rows[0][0] == "concluded"
        assert rows[0][1] == "pass"
    finally:
        db.close()


def test_get_campaign_accepts_short_id(populated_warm_catalog: Path):
    """get_campaign should resolve 8-char short IDs."""
    db = duckdb.connect(str(populated_warm_catalog / "bathos.db"))
    try:
        campaign = create_campaign(db, name="Short ID Fetch", project_slug="prolix", mode="exploration")
        short_id = campaign.id[:8]
        fetched = get_campaign(db, short_id)
        assert fetched is not None
        assert fetched.id == campaign.id
    finally:
        db.close()


def test_conclude_campaign_raises_on_ambiguous_prefix(populated_warm_catalog: Path):
    """conclude_campaign should raise CampaignError if a prefix matches multiple campaigns."""
    import uuid
    db = duckdb.connect(str(populated_warm_catalog / "bathos.db"))
    try:
        # Force two campaigns with the same 4-char prefix by setting IDs directly
        shared_prefix = "aaaa"
        id1 = shared_prefix + str(uuid.uuid4())[4:]
        id2 = shared_prefix + str(uuid.uuid4())[4:]
        for cid in (id1, id2):
            db.execute(
                "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at) VALUES (?, 'prolix', 'Ambig', 'exploration', 'open', ?)",
                [cid, datetime.now(UTC).isoformat()],
            )
        with pytest.raises(CampaignError, match="Ambiguous"):
            conclude_campaign(db, shared_prefix, "pass", "")
    finally:
        db.close()
