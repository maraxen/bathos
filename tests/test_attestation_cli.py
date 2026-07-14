"""CLI tests for `bth attestation` subcommands (S4 attestation sidecar, item 3492).

Mirrors tests/test_anchor_cli.py / tests/test_claim_cli.py conventions.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from bathos.cli import app

runner = CliRunner()

ORACLE_MATCH_TOML = """
[attestation]
kind = "oracle_match"
verdict = "PASS"
attested = {{ run_id = "run-001", output_path = "out/result.zarr", content_hash = "{content_hash}" }}
oracle_sha256 = "{oracle_sha}"
harness_run_ref = "run-harness-001"
max_discrepancy = 0.001
tolerance_policy = "abs<=1e-3"
created_by = "test-suite"
created_at = "2026-07-14T00:00:00Z"
"""


@pytest.fixture
def attestation_cli_env(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    return catalog


def test_attestation_help_lists_subcommands():
    result = runner.invoke(app, ["attestation", "--help"])
    assert result.exit_code == 0
    assert "scaffold" in result.output
    assert "register" in result.output
    assert "validate" in result.output


def test_attestation_scaffold_creates_template(attestation_cli_env):
    _ = attestation_cli_env  # fixture sets BTH_CATALOG_DIR via monkeypatch as a side effect
    result = runner.invoke(app, ["attestation", "scaffold", "oracle_match"])
    assert result.exit_code == 0, result.output
    assert "Created:" in result.output


def test_attestation_scaffold_rejects_bad_kind(attestation_cli_env):
    _ = attestation_cli_env
    result = runner.invoke(app, ["attestation", "scaffold", "bogus"])
    assert result.exit_code != 0


def test_attestation_register_then_query(attestation_cli_env, tmp_path):
    _ = attestation_cli_env
    content_hash = "a" * 64
    src = tmp_path / "attest.toml"
    src.write_text(ORACLE_MATCH_TOML.format(content_hash=content_hash, oracle_sha="b" * 64))

    result = runner.invoke(app, ["attestation", "register", str(src)])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["kind"] == "oracle_match"
    assert payload["content_hash"] == content_hash
    assert payload["label"] == "PASS"

    query_result = runner.invoke(app, ["query", "attestation", content_hash])
    assert query_result.exit_code == 0, query_result.output
    attestation = json.loads(query_result.output)
    assert attestation["verdict"] == "PASS"
    assert attestation["kind"] == "oracle_match"


def test_attestation_register_missing_file_errors(attestation_cli_env, tmp_path):
    _ = attestation_cli_env
    result = runner.invoke(app, ["attestation", "register", str(tmp_path / "nope.toml")])
    assert result.exit_code != 0


def test_attestation_validate_valid_file(tmp_path):
    content_hash = "c" * 64
    src = tmp_path / "attest.toml"
    src.write_text(ORACLE_MATCH_TOML.format(content_hash=content_hash, oracle_sha="d" * 64))

    result = runner.invoke(app, ["attestation", "validate", str(src)])
    assert result.exit_code == 0, result.output
    assert "valid" in result.output


def test_attestation_validate_invalid_file_errors(tmp_path):
    src = tmp_path / "bad.toml"
    src.write_text(
        """
[attestation]
kind = "oracle_match"
verdict = "PASS"
[attestation.attested]
run_id = "r1"
output_path = "o"
content_hash = "x"
"""
    )

    result = runner.invoke(app, ["attestation", "validate", str(src)])
    assert result.exit_code != 0


def test_query_attestation_returns_null_when_unregistered(attestation_cli_env):
    _ = attestation_cli_env
    result = runner.invoke(app, ["query", "attestation", "f" * 64])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "null"
