"""Regression tests for backlog #5608: claim scaffold -> validate -> register.

Covers three claims about the claim round trip:
  (a) a fresh scaffold parses as TOML and reaches validation (no parse error)
  (b) a fresh scaffold's only validation error is the unfilled parity baseline, and
      that error tells the author what to fill in
  (c) a declared catalog_dir that does not exist is named as such (no silent
      fallback to the global catalog)

A nested `[project.catalog] catalog_dir` (denxity, molts) is deliberately NOT handled here:
the loader ignores it and, after the log-mode cutover (debt #1998), per-project catalog dirs
are obsolete, so registering into the global catalog is the correct behaviour. Whether to
migrate or delete those keys is the owner's call.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

CAMPAIGN_ID = "camp_test01"

_CAMPAIGNS_DDL = """
    CREATE TABLE campaigns (
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
"""


def _seed_campaign(db: duckdb.DuckDBPyConnection) -> None:
    db.execute(_CAMPAIGNS_DDL)
    db.execute(
        "INSERT INTO campaigns (id, project_slug, name, mode, hypothesis, status, started_at) "
        "VALUES (?, 'proj', 'testcamp', 'confirmation', 'H: x beats y', 'open', "
        "'2026-08-28T00:00:00Z')",
        [CAMPAIGN_ID],
    )


def _write_bth_toml(proj: Path, body: str) -> None:
    proj.mkdir(parents=True, exist_ok=True)
    (proj / ".bth.toml").write_text(f'[project]\nslug = "proj"\nroot = "{proj}"\n{body}')


# --- (a) and (b): fresh scaffold ------------------------------------------------------


@pytest.fixture
def scaffolded_claim(tmp_path):
    from bathos.claim import parse_claim, scaffold_claim

    db = duckdb.connect(":memory:")
    _seed_campaign(db)
    path = scaffold_claim(CAMPAIGN_ID, db, tmp_path)
    db.close()
    return parse_claim(path)


def test_fresh_scaffold_parses_and_reaches_validation(scaffolded_claim):
    """(a) The scaffold is parseable; validation reports on content, not on syntax."""
    from bathos.claim import validate_claim

    result = validate_claim(scaffolded_claim)
    assert all("parse" not in e.message.lower() for e in result.errors)


def test_fresh_scaffold_fails_only_on_unfilled_parity_baseline(scaffolded_claim):
    """(b) The fresh scaffold's sole error is the parity baseline it created."""
    from bathos.claim import validate_claim

    result = validate_claim(scaffolded_claim)
    messages = [e.message for e in result.errors]
    assert len(messages) == 1, messages
    assert messages[0].startswith("baseline admissibility not established")


def test_fresh_scaffold_parity_error_names_the_remedy(scaffolded_claim):
    """(b) The error must say what to fill in, not only that admissibility failed."""
    from bathos.claim import validate_claim

    message = validate_claim(scaffolded_claim).errors[0].message
    assert "parity_run_id" in message, message
    assert "reference_parity" in message, message


# --- (c): declared catalog_dir resolution in claim_register ---------------------------


def test_register_names_missing_declared_catalog_dir(tmp_path, monkeypatch):
    """(c) A declared-but-absent catalog_dir is reported as the .bth.toml declaration."""
    from bathos.mcp import claim_register_tool

    proj = tmp_path / "proj"
    _write_bth_toml(proj, 'catalog_dir = ".bth/catalog"\n')
    claim = proj / "c.claim.toml"
    claim.write_text("[claim]\n")
    monkeypatch.delenv("BTH_CATALOG_DIR", raising=False)
    monkeypatch.chdir(proj)

    result = claim_register_tool(str(claim), CAMPAIGN_ID, workspace_root=str(proj))

    assert result["ok"] is False
    error = result["error"]
    assert ".bth.toml" in error, error
    assert "does not exist" in error, error
    assert "no fallback" in error, error

