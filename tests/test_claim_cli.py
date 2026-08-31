"""CLI tests for bth claim subcommands.

Note (final cutover, backlog #4702): the shipped Typer `claim register`/
`validate-sidecar` commands exposed `--campaign`/`-c`; the registry-driven
cyclopts commands derive their flag name from the shared tool functions'
`campaign_id` parameter, so the flag is `--campaign-id` (no `-c` alias). An
`Annotated[..., cyclopts.Parameter(name=[...])]` alias was tried and reverted:
cisternal's wire() copies the wrapped function's `__annotations__` dict onto a
new closure defined inside `cisternal.registration.wired`, and `from __future__
import annotations` means those annotations are strings resolved via
`get_type_hints()` against the WRAPPER's `__globals__` (cisternal's own module),
not bathos.mcp's -- so `Annotated`/`cyclopts` are undefined there. Accepted as a
CLI-flag-name difference, same category as `find --tag` -> `--tags`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import duckdb
import pytest

from bathos.cli_cyclopts import app
from bathos.compact import _CAMPAIGNS_TABLE_SCHEMA
from tests._cyclopts_runner import CyclopticRunner

# Canonical warm-tier campaigns DDL. The CLI's claim flow upserts cool-tier
# campaign JSON (ingest_cool_campaigns) whose INSERT references the FULL
# modern column list; hand-rolled stale schemas here caused Binder exceptions.
# Always build the table from this DDL and use named columns in inserts so
# future schema additions cannot silently break these tests again.

runner = CyclopticRunner()


@pytest.fixture
def claim_cli_env(tmp_path, monkeypatch):
    catalog = tmp_path / "catalog"
    catalog.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')
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
    con.execute(_CAMPAIGNS_TABLE_SCHEMA)
    con.execute(
        "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            "camp-1",
            "testproj",
            "parity_test",
            "confirmation",
            "open",
            datetime.now(UTC).isoformat(),
        ],
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
kill_condition_satisfiable_by_null = false

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
    con.execute(_CAMPAIGNS_TABLE_SCHEMA)
    con.execute(
        "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ["camp-1", "testproj", "bind_test", "confirmation", "open", datetime.now(UTC).isoformat()],
    )
    con.close()

    result = runner.invoke(
        app,
        ["claim", "register", str(claim_path), "--campaign-id", "camp-1"],
    )
    assert result.exit_code == 0, result.output

    con = duckdb.connect(str(db_path))
    row = con.execute(
        "SELECT claim_path, claim_sha256 FROM campaigns WHERE id = 'camp-1'"
    ).fetchone()
    con.close()
    assert row[0] is not None
    assert row[1] is not None


def _register_test_claim(tmp_path, catalog, campaign_id="camp-1"):
    """Write a minimal 2-hypothesis claim + register it against a manually-seeded campaign."""
    import textwrap

    claim_path = tmp_path / "test.claim.toml"
    claim_path.write_text(
        textwrap.dedent("""
        [claim]
        headline = "Test claim"
        kill_condition = "Outcome != expected"

        [[hypotheses]]
        id = "H_primary"
        label = "Primary"

        [[hypotheses]]
        id = "H_null"
        label = "Null"
    """)
    )
    db_path = catalog / "bathos.db"
    con = duckdb.connect(str(db_path))
    con.execute(_CAMPAIGNS_TABLE_SCHEMA)
    con.execute(
        "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            campaign_id,
            "testproj",
            "sidecar_gate_test",
            "confirmation",
            "open",
            datetime.now(UTC).isoformat(),
        ],
    )
    con.close()
    result = runner.invoke(app, ["claim", "register", str(claim_path), "--campaign-id", campaign_id])
    assert result.exit_code == 0, result.output
    return claim_path


def test_validate_sidecar_campaign_flag_catches_wrong_hypothesis_id(claim_cli_env, tmp_path):
    """#3719: bth validate-sidecar --campaign catches the #3717 authoring mistake --
    claim_discriminates set to outcome labels instead of registered hypothesis ids."""
    import textwrap

    catalog = claim_cli_env
    _register_test_claim(tmp_path, catalog)

    sidecar_path = tmp_path / "run_x.bth.toml"
    sidecar_path.write_text(
        textwrap.dedent("""
        [experiment]
        hypothesis = "test hypothesis"
        claim_discriminates = ["beyond_nj_regime_found", "caps_at_nj_no_beyond_nj_regime"]
        [outcomes.pass]
        condition = "x == 1"
        decision = "good"
        reasoning = "expected behavior"
        [outcomes.fallback]
        condition = "1==1"
        decision = "other"
        reasoning = "catch-all"
        is_residual = true
        [result_schema]
        x = "float"
    """)
    )

    result = runner.invoke(app, ["validate-sidecar", str(sidecar_path), "--campaign-id", "camp-1"])
    assert result.exit_code == 1
    assert "beyond_nj_regime_found" in result.output
    assert "claim_discriminates" in result.output
    assert "H_primary" in result.output and "H_null" in result.output


def test_validate_sidecar_campaign_flag_passes_with_correct_ids(claim_cli_env, tmp_path):
    import textwrap

    catalog = claim_cli_env
    _register_test_claim(tmp_path, catalog)

    sidecar_path = tmp_path / "run_x.bth.toml"
    sidecar_path.write_text(
        textwrap.dedent("""
        [experiment]
        hypothesis = "test hypothesis"
        claim_discriminates = ["H_primary", "H_null"]
        [outcomes.pass]
        condition = "x == 1"
        decision = "good"
        reasoning = "expected behavior"
        [outcomes.fallback]
        condition = "1==1"
        decision = "other"
        reasoning = "catch-all"
        is_residual = true
        [result_schema]
        x = "float"
    """)
    )

    result = runner.invoke(app, ["validate-sidecar", str(sidecar_path), "--campaign-id", "camp-1"])
    assert result.exit_code == 0, result.output


def test_validate_sidecar_without_campaign_flag_unaffected(
    claim_cli_env,  # noqa: ARG001 - pytest fixture
    tmp_path,
):
    """No --campaign given: validates exactly as before #3719, no regression."""
    import textwrap

    sidecar_path = tmp_path / "run_x.bth.toml"
    sidecar_path.write_text(
        textwrap.dedent("""
        [experiment]
        hypothesis = "test hypothesis"
        claim_discriminates = ["anything at all, no claim to check against"]
        [outcomes.pass]
        condition = "x == 1"
        decision = "good"
        reasoning = "expected behavior"
        [outcomes.fallback]
        condition = "1==1"
        decision = "other"
        reasoning = "catch-all"
        is_residual = true
        [result_schema]
        x = "float"
    """)
    )

    result = runner.invoke(app, ["validate-sidecar", str(sidecar_path)])
    assert result.exit_code == 0, result.output
