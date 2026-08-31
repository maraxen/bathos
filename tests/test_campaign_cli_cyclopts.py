"""CLI-level tests for the registry-driven `campaign` command group
(backlog #4702 Milestone 1 pilot), exercised through the preview cyclopts
app (`bathos.cli_cyclopts.app`) via the `CyclopticRunner` shim.

Two of these (attest-parity success/failure) are ports of the equivalent
`typer.testing.CliRunner`-based tests in `test_t8_t9_attest_parity.py` —
that original file is left completely untouched, so it continues to prove
the shipped `bth` CLI still works unchanged. The other six commands
(`create`, `add`, `conclude`, `ls`, `show`, `review`) have NO CLI-level
tests at all prior to this file (confirmed via grep) — only Python-API-layer
tests (`test_campaigns.py`) and MCP-tool-layer tests. One happy-path +
(where a natural error case exists) one error-path smoke test per command
is the goal here, not exhaustive edge-case coverage — that already lives at
the Python-API layer and is untouched by this migration.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from bathos.campaigns import add_run_to_campaign, create_campaign
from bathos.catalog import init_catalog, write_run
from bathos.cli_cyclopts import app
from bathos.compact import compact
from bathos.schema import Run
from tests._cyclopts_runner import CyclopticRunner

runner = CyclopticRunner()


# ---------------------------------------------------------------------------
# Fixtures -- duplicated (not imported) from test_t8_t9_attest_parity.py and
# test_campaigns.py. Those files stay untouched (proving the shipped `bth`
# CLI and the Python-API layer keep working unchanged); there's no existing
# cross-test-file fixture-import precedent in this suite, and pytest fixture
# functions used as test parameters trip ruff's F811 when imported by name,
# so a small local copy is the more idiomatic fit here.
# ---------------------------------------------------------------------------


@pytest.fixture
def catalog_with_tables(tmp_catalog):
    """Warm catalog directory with campaigns + runs tables."""
    init_catalog(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    db.execute("""
        CREATE TABLE IF NOT EXISTS campaigns (
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
            claim_mode TEXT
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id TEXT PRIMARY KEY,
            campaign_id TEXT,
            outcome TEXT,
            metadata TEXT,
            parity_run_type TEXT
        )
    """)
    db.commit()
    db.close()
    return tmp_catalog


@pytest.fixture
def claim_file(tmp_path):
    """Claim with reference_parity confound and empty parity_run_id."""
    claim_rel = ".bth/claims/test.claim.toml"
    claim_dir = tmp_path / ".bth" / "claims"
    claim_dir.mkdir(parents=True)
    claim_path = claim_dir / "test.claim.toml"
    content = """[claim]
headline = "Test claim"
kill_condition = "fail"

[[hypotheses]]
id = "H_primary"
label = "Primary"

[[hypotheses]]
id = "H_null"
label = "Null"

[[assumptions]]
id = "A1"
label = "Assumption"

[[confounds]]
id = "C_parity"
label = "Literature parity"
[confounds.reference_parity]
reference_paper = "Example 2026"
parity_run_id = ""

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "main"
predicted_outcome = "discriminates"

[claim.union_gate]
"""
    claim_path.write_text(content)
    claim_sha = hashlib.sha256(claim_path.read_bytes()).hexdigest()
    return claim_rel, claim_sha, claim_path


@pytest.fixture
def parity_run_id():
    return "run_parity_abc123"


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


# ---------------------------------------------------------------------------
# attest-parity: ported from test_t8_t9_attest_parity.py's TestT8CLIAttestParity
# ---------------------------------------------------------------------------


class TestCampaignAttestParityCyclopts:
    def test_binds_run(self, tmp_path, catalog_with_tables, claim_file, parity_run_id):
        catalog_dir = catalog_with_tables
        db = duckdb.connect(str(catalog_dir / "bathos.db"))
        claim_rel, claim_sha, claim_path = claim_file
        campaign_id = "camp-1111-2222-3333-444455556666"

        db.execute(
            """INSERT INTO campaigns
               (id, project_slug, name, mode, status, started_at, claim_path, claim_sha256)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                campaign_id,
                "testproj",
                "Confirm baseline",
                "confirmation",
                "open",
                datetime.now(UTC).isoformat(),
                claim_rel,
                claim_sha,
            ],
        )
        db.execute(
            """INSERT INTO runs (id, campaign_id, outcome, metadata, parity_run_type)
               VALUES (?, ?, ?, ?, ?)""",
            [
                parity_run_id,
                campaign_id,
                "pass",
                json.dumps({"parity_run_type": "literature_parity"}),
                "literature_parity",
            ],
        )
        db.commit()
        db.close()

        result = runner.invoke(
            app,
            [
                "campaign",
                "attest-parity",
                campaign_id,
                parity_run_id,
                "--catalog-dir",
                str(catalog_dir),
                "--workspace-root",
                str(tmp_path),
            ],
        )

        assert result.exit_code == 0, result.output
        assert "Attested parity run" in result.output

        updated = claim_path.read_text()
        assert f'parity_run_id = "{parity_run_id}"' in updated

    def test_rejects_bad_run(self, tmp_path, catalog_with_tables, claim_file):
        catalog_dir = catalog_with_tables
        db = duckdb.connect(str(catalog_dir / "bathos.db"))
        claim_rel, claim_sha, _claim_path = claim_file
        campaign_id = "camp-aaaa-bbbb-cccc-dddddddddddd"
        bad_run_id = "run_not_parity"

        db.execute(
            """INSERT INTO campaigns
               (id, project_slug, name, mode, status, started_at, claim_path, claim_sha256)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                campaign_id,
                "testproj",
                "Confirm baseline",
                "confirmation",
                "open",
                datetime.now(UTC).isoformat(),
                claim_rel,
                claim_sha,
            ],
        )
        db.execute(
            """INSERT INTO runs (id, campaign_id, outcome, metadata, parity_run_type)
               VALUES (?, ?, ?, ?, ?)""",
            [bad_run_id, campaign_id, "pass", "{}", None],
        )
        db.commit()
        db.close()

        result = runner.invoke(
            app,
            [
                "campaign",
                "attest-parity",
                campaign_id,
                bad_run_id,
                "--catalog-dir",
                str(catalog_dir),
                "--workspace-root",
                str(tmp_path),
            ],
        )

        assert result.exit_code == 1
        assert "parity_run_type" in result.output.lower() or "missing" in result.output.lower()


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


class TestCampaignCreateCyclopts:
    def test_happy_path(self, tmp_catalog):
        result = runner.invoke(
            app,
            [
                "campaign",
                "create",
                "smoke-test",
                "--mode",
                "exploration",
                "--catalog-dir",
                str(tmp_catalog),
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["name"] == "smoke-test"
        assert payload["mode"] == "exploration"
        assert payload["status"] == "open"

    def test_missing_name_is_clean_error(self, tmp_catalog):
        result = runner.invoke(app, ["campaign", "create", "--catalog-dir", str(tmp_catalog)])
        assert result.exit_code == 1
        assert "name parameter is required" in result.output


# ---------------------------------------------------------------------------
# ls
# ---------------------------------------------------------------------------


class TestCampaignLsCyclopts:
    def test_happy_path_lists_created_campaign(self, populated_warm_catalog):
        db = duckdb.connect(str(populated_warm_catalog / "bathos.db"))
        try:
            create_campaign(db, name="Listable", project_slug="prolix", mode="exploration")
        finally:
            db.close()

        result = runner.invoke(
            app, ["campaign", "ls", "--catalog-dir", str(populated_warm_catalog)]
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["count"] >= 1
        assert any(c["name"] == "Listable" for c in payload["campaigns"])


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


class TestCampaignShowCyclopts:
    def test_happy_path(self, populated_warm_catalog):
        db = duckdb.connect(str(populated_warm_catalog / "bathos.db"))
        try:
            campaign = create_campaign(
                db, name="Showable", project_slug="prolix", mode="exploration"
            )
        finally:
            db.close()

        result = runner.invoke(
            app,
            ["campaign", "show", campaign.id, "--catalog-dir", str(populated_warm_catalog)],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["campaign_id"] == campaign.id
        assert payload["name"] == "Showable"

    def test_not_found_is_clean_error(self, populated_warm_catalog):
        result = runner.invoke(
            app,
            [
                "campaign",
                "show",
                "does-not-exist",
                "--catalog-dir",
                str(populated_warm_catalog),
            ],
        )
        assert result.exit_code == 1
        assert "not found" in result.output.lower()


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


class TestCampaignAddCyclopts:
    def test_happy_path(self, populated_warm_catalog):
        db = duckdb.connect(str(populated_warm_catalog / "bathos.db"))
        try:
            campaign = create_campaign(
                db, name="Addable", project_slug="prolix", mode="exploration"
            )
            run_id = db.execute(
                "SELECT id FROM runs WHERE project_slug = 'prolix' LIMIT 1"
            ).fetchone()[0]
        finally:
            db.close()

        result = runner.invoke(
            app,
            [
                "campaign",
                "add",
                run_id,
                "--campaign-id",
                campaign.id,
                "--catalog-dir",
                str(populated_warm_catalog),
            ],
        )
        assert result.exit_code == 0, result.output
        assert run_id in result.output
        assert campaign.id in result.output

    def test_missing_campaign_id_is_clean_error(self, populated_warm_catalog):
        result = runner.invoke(
            app,
            [
                "campaign",
                "add",
                "some-run-id",
                "--catalog-dir",
                str(populated_warm_catalog),
            ],
        )
        assert result.exit_code == 1
        assert "campaign_id parameter is required" in result.output


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


class TestCampaignReviewCyclopts:
    def test_happy_path(self, populated_warm_catalog):
        db = duckdb.connect(str(populated_warm_catalog / "bathos.db"))
        try:
            campaign = create_campaign(
                db, name="Reviewable", project_slug="prolix", mode="exploration"
            )
            runs = db.execute("SELECT id FROM runs WHERE project_slug = 'prolix'").fetchall()
            for (run_id,) in runs:
                add_run_to_campaign(db, campaign.id, run_id)
        finally:
            db.close()

        result = runner.invoke(
            app,
            ["campaign", "review", campaign.id, "--catalog-dir", str(populated_warm_catalog)],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["total_runs"] >= 2
        assert "residual_rate" in payload

    def test_no_runs_is_clean_error(self, populated_warm_catalog):
        db = duckdb.connect(str(populated_warm_catalog / "bathos.db"))
        try:
            campaign = create_campaign(
                db, name="Empty", project_slug="prolix", mode="exploration"
            )
        finally:
            db.close()

        result = runner.invoke(
            app,
            ["campaign", "review", campaign.id, "--catalog-dir", str(populated_warm_catalog)],
        )
        assert result.exit_code == 1
        assert "not found" in result.output.lower() or "no runs" in result.output.lower()


# ---------------------------------------------------------------------------
# conclude
# ---------------------------------------------------------------------------


class TestCampaignConcludeCyclopts:
    def test_happy_path(self, populated_warm_catalog):
        db = duckdb.connect(str(populated_warm_catalog / "bathos.db"))
        try:
            campaign = create_campaign(
                db, name="Concludable", project_slug="prolix", mode="exploration"
            )
        finally:
            db.close()

        result = runner.invoke(
            app,
            [
                "campaign",
                "conclude",
                campaign.id,
                "--outcome-label",
                "success",
                "--catalog-dir",
                str(populated_warm_catalog),
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "concluded"
        assert payload["outcome_label"] == "success"

    def test_missing_outcome_label_is_clean_error(self, populated_warm_catalog):
        db = duckdb.connect(str(populated_warm_catalog / "bathos.db"))
        try:
            campaign = create_campaign(
                db, name="NoOutcome", project_slug="prolix", mode="exploration"
            )
        finally:
            db.close()

        result = runner.invoke(
            app,
            ["campaign", "conclude", campaign.id, "--catalog-dir", str(populated_warm_catalog)],
        )
        assert result.exit_code == 1
        assert "outcome_label parameter is required" in result.output
