"""CLI tests for bth claim subcommands."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest
from typer.testing import CliRunner

from bathos.cli import app

runner = CliRunner()


@pytest.fixture
def claim_cli_env(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(
        f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n'
    )
    return catalog


def test_claim_help_lists_subcommands():
    result = runner.invoke(app, ["claim", "--help"])
    assert result.exit_code == 0
    assert "scaffold" in result.output
    assert "register" in result.output
    assert "validate" in result.output


def test_claim_scaffold_creates_file(claim_cli_env, tmp_path):
    catalog = claim_cli_env
    db_path = catalog / "bathos.db"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE campaigns (id VARCHAR, project_slug VARCHAR, name VARCHAR, "
        "mode VARCHAR, status VARCHAR, started_at VARCHAR, hypothesis VARCHAR)"
    )
    con.execute(
        "INSERT INTO campaigns VALUES (?, ?, ?, ?, ?, ?, ?)",
        ["camp-1", "testproj", "parity_test", "confirmation", "open",
         datetime.now(UTC).isoformat(), "test hypothesis"],
    )
    con.close()

    result = runner.invoke(app, ["claim", "scaffold", "camp-1"])
    assert result.exit_code == 0, result.output
    claim_path = tmp_path / ".bth" / "claims" / "parity_test.claim.toml"
    assert claim_path.exists()
    assert "parity_run_id" in claim_path.read_text()


def test_claim_validate_ok_on_minimal_claim(tmp_path):
    claim_path = tmp_path / "minimal.claim.toml"
    claim_path.write_text("""[claim]
headline = "Test headline"
kill_condition = "Fails if wrong"

[[hypotheses]]
id = "H_main_effect"
label = "Main"

[[hypotheses]]
id = "H_null_misspec"
label = "Null"
""")
    result = runner.invoke(app, ["claim", "validate", str(claim_path)])
    assert result.exit_code == 0, result.output
    assert "valid" in result.output.lower()


def test_claim_register_binds_campaign(claim_cli_env, tmp_path):
    catalog = claim_cli_env
    claim_path = tmp_path / "bind.claim.toml"
    claim_path.write_text("""[claim]
headline = "Bind test"
kill_condition = "test"

[[hypotheses]]
id = "H_main"
label = "Main"

[[hypotheses]]
id = "H_null"
label = "Null"
""")
    db_path = catalog / "bathos.db"
    con = duckdb.connect(str(db_path))
    con.execute(
        "CREATE TABLE campaigns (id VARCHAR, project_slug VARCHAR, name VARCHAR, "
        "mode VARCHAR, status VARCHAR, started_at VARCHAR, claim_path VARCHAR, claim_sha256 VARCHAR)"
    )
    con.execute(
        "INSERT INTO campaigns VALUES (?, ?, ?, ?, ?, ?, NULL, NULL)",
        ["camp-1", "testproj", "bind_test", "confirmation", "open",
         datetime.now(UTC).isoformat()],
    )
    con.close()

    result = runner.invoke(
        app,
        ["claim", "register", str(claim_path), "--campaign", "camp-1"],
    )
    assert result.exit_code == 0, result.output

    con = duckdb.connect(str(db_path))
    row = con.execute(
        "SELECT claim_path, claim_sha256 FROM campaigns WHERE id = 'camp-1'"
    ).fetchone()
    con.close()
    assert row[0] is not None
    assert row[1] is not None
