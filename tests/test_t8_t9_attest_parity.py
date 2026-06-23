"""Integration tests for T8 (F4 CLI + MCP attest-parity) and T9 (Signal 13)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from unittest.mock import patch

import duckdb
import pytest
from typer.testing import CliRunner

from bathos.catalog import init_catalog
from bathos.cli import app
from bathos.mcp import claim_attest_parity


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


class TestT8CLIAttestParity:
    """AC-11/12/13 at CLI integration level."""

    def test_cli_attest_parity_binds_run(
        self, tmp_path, catalog_with_tables, claim_file, parity_run_id, monkeypatch
    ):
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

        monkeypatch.chdir(tmp_path)

        with patch("bathos.cli._catalog_dir", return_value=catalog_dir):
            runner = CliRunner()
            result = runner.invoke(
                app,
                ["campaign", "attest-parity", campaign_id, parity_run_id],
            )

        assert result.exit_code == 0, result.output
        assert "Attested parity run" in result.output

        updated = claim_path.read_text()
        assert f'parity_run_id = "{parity_run_id}"' in updated

        db = duckdb.connect(str(catalog_dir / "bathos.db"))
        new_sha = db.execute(
            "SELECT claim_sha256 FROM campaigns WHERE id = ?", [campaign_id]
        ).fetchone()[0]
        db.close()
        assert new_sha == hashlib.sha256(updated.encode()).hexdigest()

    def test_cli_attest_parity_rejects_bad_run(
        self, tmp_path, catalog_with_tables, claim_file, monkeypatch
    ):
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

        monkeypatch.chdir(tmp_path)

        with patch("bathos.cli._catalog_dir", return_value=catalog_dir):
            runner = CliRunner()
            result = runner.invoke(
                app,
                ["campaign", "attest-parity", campaign_id, bad_run_id],
            )

        assert result.exit_code == 1
        assert "parity_run_type" in result.output.lower() or "missing" in result.output.lower()


class TestT8MCPAttestParity:
    """MCP claim_attest_parity wraps attest_parity."""

    def test_mcp_attest_parity_ok(
        self, tmp_path, catalog_with_tables, claim_file, parity_run_id
    ):
        catalog_dir = catalog_with_tables
        db = duckdb.connect(str(catalog_dir / "bathos.db"))
        claim_rel, claim_sha, claim_path = claim_file
        campaign_id = "camp-mcp-1111-2222-3333-444455556666"

        db.execute(
            """INSERT INTO campaigns
               (id, project_slug, name, mode, status, started_at, claim_path, claim_sha256)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                campaign_id,
                "testproj",
                "MCP campaign",
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

        result = asyncio.run(
            claim_attest_parity(
                campaign_id=campaign_id,
                parity_run_id=parity_run_id,
                catalog_dir=str(catalog_dir),
                workspace_root=str(tmp_path),
            )
        )

        assert result["ok"] is True
        assert parity_run_id in result["message"]
        assert f'parity_run_id = "{parity_run_id}"' in claim_path.read_text()

    def test_mcp_attest_parity_rejects_missing_run(
        self, tmp_path, catalog_with_tables, claim_file
    ):
        catalog_dir = catalog_with_tables
        db = duckdb.connect(str(catalog_dir / "bathos.db"))
        claim_rel, claim_sha, _claim_path = claim_file
        campaign_id = "camp-mcp-aaaa-bbbb-cccc-dddddddddddd"

        db.execute(
            """INSERT INTO campaigns
               (id, project_slug, name, mode, status, started_at, claim_path, claim_sha256)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                campaign_id,
                "testproj",
                "MCP campaign",
                "confirmation",
                "open",
                datetime.now(UTC).isoformat(),
                claim_rel,
                claim_sha,
            ],
        )
        db.commit()
        db.close()

        result = asyncio.run(
            claim_attest_parity(
                campaign_id=campaign_id,
                parity_run_id="run_does_not_exist",
                catalog_dir=str(catalog_dir),
                workspace_root=str(tmp_path),
            )
        )

        assert result["ok"] is False
        assert "not found" in (result.get("error") or "").lower()


class TestT9Signal13:
    """AC-14: Signal 13 flags uncontrolled reference_parity."""

    def test_signal_13_source_present(self):
        from bathos.sprint_audit import sprint_audit as _sprint_audit
        import inspect

        source = inspect.getsource(_sprint_audit)
        assert "Signal 13" in source
        assert "uncontrolled reference_parity" in source
        assert "claim file missing at" in source or "missing at" in source

    def test_signal_13_empty_parity_run_id_is_uncontrolled(self, claim_file):
        from bathos.claim import parity_confound_check

        _claim_rel, _claim_sha, claim_path = claim_file
        result = parity_confound_check(claim_path, db=None)
        statuses = [c["status"] for c in result["confounds"]]
        assert "uncontrolled" in statuses
