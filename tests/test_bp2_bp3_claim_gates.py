"""Integration tests for BP-2 (synthetic_recovery confound at conclude) and BP-3 (negative_check),
mirroring the existing F2 reference_parity conclude-gate tests in test_t6_parity_conclude_gate.py.
"""

from __future__ import annotations

import datetime as dt
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from bathos.campaigns import (
    CampaignError,
    add_run_to_campaign,
    conclude_campaign,
    create_campaign,
    get_campaign,
)
from bathos.catalog import init_catalog, write_run
from bathos.claim import register_claim
from bathos.compact import compact
from bathos.gate import stamp_gate
from bathos.schema import Run


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


@pytest.fixture
def git_workspace(tmp_path):
    """Init a real git repo at tmp_path with a committed guarded file; returns (root, sha)."""
    _git(["init"], tmp_path)
    _git(["config", "user.email", "t@example.com"], tmp_path)
    _git(["config", "user.name", "T"], tmp_path)
    guarded = tmp_path / "src" / "component.py"
    guarded.parent.mkdir(parents=True)
    guarded.write_text("x = 1\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-m", "init"], tmp_path)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True, check=True
    ).stdout.strip()
    return tmp_path, sha


@pytest.fixture
def tmp_catalog(tmp_path):
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    init_catalog(catalog_dir)
    return catalog_dir


@pytest.fixture
def clean_db(tmp_catalog):
    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    yield db
    db.close()


def _claim_with_synth(root, gate_name="g", guards=("src/component.py",)):
    guards_toml = ", ".join(f'"{g}"' for g in guards)
    claim_path = root / "claim.bth.toml"
    claim_path.write_text(f"""[claim]
headline = "Test claim"
kill_condition = "Outcome != expected"

[[hypotheses]]
id = "H_primary"
label = "Primary"

[[hypotheses]]
id = "H_null"
label = "Null"

[[confounds]]
id = "C_pipeline"
label = "Pipeline soundness"
[confounds.synthetic_recovery]
gate_name = "{gate_name}"
guards = [{guards_toml}]

[claim.union_gate]
""")
    return claim_path


def _add_completed_run(tmp_catalog, campaign, db):
    campaign_time = datetime.fromisoformat(campaign.started_at)
    run_time = campaign_time.replace(microsecond=0) + dt.timedelta(minutes=1)
    run = Run(
        project_slug="test_proj",
        command="python test.py",
        argv=["python", "test.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        timestamp=run_time,
        status="completed",
        exit_code=0,
    )
    write_run(run, tmp_catalog)
    db.close()
    compact(tmp_catalog)
    db2 = duckdb.connect(str(tmp_catalog / "bathos.db"))
    add_run_to_campaign(db2, campaign.id, run.id)
    return db2


class TestBP2SyntheticRecoveryConclude:
    def test_confirmation_downgrades_when_gate_never_stamped(
        self, tmp_catalog, git_workspace, clean_db
    ):
        root, _sha = git_workspace
        db = clean_db
        campaign = create_campaign(db, name="Confirm", project_slug="test_proj", mode="confirmation")
        claim_path = _claim_with_synth(root)
        register_claim(claim_path, campaign.id, db, root)

        db = _add_completed_run(tmp_catalog, campaign, db)
        conclude_campaign(db, campaign.id, "pass", "conclusion", workspace_root=root)

        rows = db.execute("SELECT outcome_label FROM campaigns WHERE id=?", [campaign.id]).fetchall()
        assert rows[0][0] == "confounded"

    def test_confirmation_no_downgrade_when_gate_green(self, tmp_catalog, git_workspace, clean_db):
        root, sha = git_workspace
        db = clean_db
        campaign = create_campaign(db, name="Confirm", project_slug="test_proj", mode="confirmation")
        claim_path = _claim_with_synth(root)
        register_claim(claim_path, campaign.id, db, root)
        stamp_gate(root, "g", "pass", sha)

        db = _add_completed_run(tmp_catalog, campaign, db)
        conclude_campaign(db, campaign.id, "pass", "conclusion", workspace_root=root)

        rows = db.execute("SELECT outcome_label FROM campaigns WHERE id=?", [campaign.id]).fetchall()
        assert rows[0][0] == "pass"

    def test_exploration_warns_no_downgrade(self, tmp_catalog, git_workspace, clean_db, capsys):
        root, _sha = git_workspace
        db = clean_db
        campaign = create_campaign(db, name="Explore", project_slug="test_proj", mode="exploration")
        claim_path = _claim_with_synth(root)
        register_claim(claim_path, campaign.id, db, root)

        db = _add_completed_run(tmp_catalog, campaign, db)
        conclude_campaign(db, campaign.id, "pass", "conclusion", workspace_root=root)

        rows = db.execute("SELECT outcome_label FROM campaigns WHERE id=?", [campaign.id]).fetchall()
        assert rows[0][0] == "pass"
        captured = capsys.readouterr()
        assert "uncontrolled" in captured.out.lower()

    def test_stale_gate_downgrades_confirmation(self, tmp_catalog, git_workspace, clean_db):
        root, sha = git_workspace
        db = clean_db
        campaign = create_campaign(db, name="Confirm", project_slug="test_proj", mode="confirmation")
        claim_path = _claim_with_synth(root)
        register_claim(claim_path, campaign.id, db, root)
        stamp_gate(root, "g", "pass", sha)
        # Edit the guarded path after stamping -> STALE
        (root / "src" / "component.py").write_text("x = 2\n")

        db = _add_completed_run(tmp_catalog, campaign, db)
        conclude_campaign(db, campaign.id, "pass", "conclusion", workspace_root=root)

        rows = db.execute("SELECT outcome_label FROM campaigns WHERE id=?", [campaign.id]).fetchall()
        assert rows[0][0] == "confounded"


class TestBP3NegativeCheck:
    def _claim_no_confounds(self, root):
        claim_path = root / "claim.bth.toml"
        claim_path.write_text("""[claim]
headline = "Test claim"
kill_condition = "Outcome != expected"

[[hypotheses]]
id = "H_primary"
label = "Primary"

[[hypotheses]]
id = "H_null"
label = "Null"

[claim.union_gate]
""")
        return claim_path

    def test_negative_outcome_without_claim_registered_is_unaffected(
        self, tmp_catalog, git_workspace, clean_db
    ):
        """No claim registered -> BP-3 opt-in skip, negative outcome needs no --negative-check."""
        root, _sha = git_workspace
        db = clean_db
        campaign = create_campaign(db, name="NoClaim", project_slug="test_proj", mode="exploration")

        # Should NOT raise even though outcome is negative and negative_check is blank
        conclude_campaign(db, campaign.id, "failed", "no claim attached", workspace_root=root)

        rows = db.execute("SELECT outcome_label FROM campaigns WHERE id=?", [campaign.id]).fetchall()
        assert rows[0][0] == "failed"

    def test_negative_outcome_with_claim_and_blank_check_raises(
        self, tmp_catalog, git_workspace, clean_db
    ):
        root, _sha = git_workspace
        db = clean_db
        campaign = create_campaign(db, name="Confirm", project_slug="test_proj", mode="confirmation")
        claim_path = self._claim_no_confounds(root)
        register_claim(claim_path, campaign.id, db, root)

        with pytest.raises(CampaignError, match="negative claim"):
            conclude_campaign(db, campaign.id, "failed", "dead end", workspace_root=root)

    def test_negative_outcome_with_claim_and_backing_succeeds(
        self, tmp_catalog, git_workspace, clean_db
    ):
        root, _sha = git_workspace
        db = clean_db
        campaign = create_campaign(db, name="Confirm", project_slug="test_proj", mode="confirmation")
        claim_path = self._claim_no_confounds(root)
        register_claim(claim_path, campaign.id, db, root)

        conclude_campaign(
            db,
            campaign.id,
            "failed",
            "dead end",
            workspace_root=root,
            negative_check="bootstrap CI excludes zero; see run abc123",
        )

        campaign_row = get_campaign(db, campaign.id)
        assert campaign_row.outcome_label == "failed"
        assert campaign_row.negative_check == "bootstrap CI excludes zero; see run abc123"

    def test_positive_outcome_with_claim_needs_no_negative_check(
        self, tmp_catalog, git_workspace, clean_db
    ):
        root, _sha = git_workspace
        db = clean_db
        campaign = create_campaign(db, name="Confirm", project_slug="test_proj", mode="confirmation")
        claim_path = self._claim_no_confounds(root)
        register_claim(claim_path, campaign.id, db, root)

        # "pass" does not match the negative vocabulary -> no error even with blank negative_check
        conclude_campaign(db, campaign.id, "pass", "worked", workspace_root=root)

        rows = db.execute("SELECT outcome_label FROM campaigns WHERE id=?", [campaign.id]).fetchall()
        assert rows[0][0] == "pass"

    def test_custom_negative_outcome_pattern_override(self, tmp_catalog, git_workspace, clean_db):
        import re

        root, _sha = git_workspace
        db = clean_db
        campaign = create_campaign(db, name="Confirm", project_slug="test_proj", mode="confirmation")
        claim_path = self._claim_no_confounds(root)
        register_claim(claim_path, campaign.id, db, root)

        custom_pattern = re.compile(r"\b(inconclusive)\b", re.IGNORECASE)

        # "failed" is not in the custom vocabulary -> passes without negative_check
        conclude_campaign(
            db, campaign.id, "failed", "ok", workspace_root=root,
            negative_outcome_pattern=custom_pattern,
        )
        rows = db.execute("SELECT outcome_label FROM campaigns WHERE id=?", [campaign.id]).fetchall()
        assert rows[0][0] == "failed"
