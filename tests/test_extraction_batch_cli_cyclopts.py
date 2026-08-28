"""CLI-level smoke tests for the extraction-heavy registry-driven batch
(backlog #4702 Milestone 2: claim / gate / postmortem / ref), exercised
through the preview cyclopts app (`bathos.cli_cyclopts.app`, `bth-preview`)
via the `CyclopticRunner` shim.

Unlike the earlier batches, every command here needed a brand-new plain
sync function extracted from inline async MCP logic (claim_register is
included despite the codegen audit's "direct" classification -- see
claim_register_tool's docstring in src/bathos/mcp.py for why). Mirrors
tests/test_registry_group_cli_cyclopts.py's depth: one happy-path +
(where a natural error case exists) one error-path per command.
Exhaustive edge-case coverage lives at the Python-API layer
(test_claim.py, test_gate.py, test_postmortem.py, test_corpus.py) and the
existing typer-CLI layer (test_claim_cli.py), both untouched.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from bathos.catalog import init_catalog, write_run
from bathos.cli_cyclopts import app
from bathos.compact import _CAMPAIGNS_TABLE_SCHEMA
from bathos.schema import Run
from tests._cyclopts_runner import CyclopticRunner

runner = CyclopticRunner()


def _json(output: str) -> dict:
    """Tolerant JSON parse -- see test_registry_group_cli_cyclopts.py's helper
    of the same name/purpose (a cold first call can prepend a lazy-telemetry
    warning line to the captured stdout+stderr stream)."""
    return json.loads(output[output.index("{") :])


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    _git(["init"], r)
    _git(["config", "user.email", "test@example.com"], r)
    _git(["config", "user.name", "Test"], r)
    (r / "foo.py").write_text("a = 1\n")
    _git(["add", "foo.py"], r)
    _git(["commit", "-m", "initial"], r)
    return r


def _seed_campaign(catalog: Path, campaign_id: str = "camp-1", name: str = "camp") -> None:
    db_path = catalog / "bathos.db"
    con = duckdb.connect(str(db_path))
    con.execute(_CAMPAIGNS_TABLE_SCHEMA)
    con.execute(
        "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [campaign_id, "testproj", name, "confirmation", "open", datetime.now(UTC).isoformat()],
    )
    con.close()


MINIMAL_CLAIM_TOML = textwrap.dedent("""
[claim]
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


class TestClaimCyclopts:
    def test_scaffold_creates_file(self, tmp_catalog, tmp_path):
        init_catalog(tmp_catalog)
        _seed_campaign(tmp_catalog, "camp-1", "parity_test")
        result = runner.invoke(
            app,
            [
                "claim",
                "scaffold",
                "--campaign-id",
                "camp-1",
                "--catalog-dir",
                str(tmp_catalog),
                "--workspace-root",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        # claim_scaffold_tool's success dict carries a "message" field, which
        # render_or_exit prints verbatim instead of the full JSON envelope.
        assert "Claim template created at" in result.output
        claim_path = tmp_path / ".bth" / "claims" / "parity_test.claim.toml"
        assert claim_path.exists()

    def test_scaffold_missing_campaign_is_clean_error(self, tmp_catalog, tmp_path):
        init_catalog(tmp_catalog)
        result = runner.invoke(
            app,
            [
                "claim",
                "scaffold",
                "--campaign-id",
                "does-not-exist",
                "--catalog-dir",
                str(tmp_catalog),
                "--workspace-root",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 1

    def test_validate_ok_on_minimal_claim(self, tmp_path):
        claim_path = tmp_path / "minimal.claim.toml"
        claim_path.write_text(MINIMAL_CLAIM_TOML)
        result = runner.invoke(app, ["claim", "validate", "--path", str(claim_path)])
        assert result.exit_code == 0, result.output
        payload = _json(result.output)
        assert payload["ok"] is True

    def test_validate_missing_file_is_clean_error(self, tmp_path):
        result = runner.invoke(
            app, ["claim", "validate", "--path", str(tmp_path / "nope.claim.toml")]
        )
        assert result.exit_code == 1

    def test_register_binds_campaign(self, tmp_catalog, tmp_path):
        init_catalog(tmp_catalog)
        _seed_campaign(tmp_catalog, "camp-1", "bind_test")
        claim_path = tmp_path / "bind.claim.toml"
        claim_path.write_text(MINIMAL_CLAIM_TOML)

        result = runner.invoke(
            app,
            [
                "claim",
                "register",
                "--path",
                str(claim_path),
                "--campaign-id",
                "camp-1",
                "--catalog-dir",
                str(tmp_catalog),
                "--workspace-root",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        # claim_register_tool's success dict also carries a "message" field.
        assert "Registered claim for campaign camp-1" in result.output

        db_path = tmp_catalog / "bathos.db"
        con = duckdb.connect(str(db_path))
        row = con.execute(
            "SELECT claim_path, claim_sha256 FROM campaigns WHERE id = 'camp-1'"
        ).fetchone()
        con.close()
        assert row[0] is not None
        assert row[1] is not None

    def test_register_missing_catalog_is_clean_error(self, tmp_catalog, tmp_path):
        claim_path = tmp_path / "x.claim.toml"
        claim_path.write_text(MINIMAL_CLAIM_TOML)
        result = runner.invoke(
            app,
            [
                "claim",
                "register",
                "--path",
                str(claim_path),
                "--campaign-id",
                "camp-1",
                "--catalog-dir",
                str(tmp_catalog),
                "--workspace-root",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 1

    def test_author_writes_claim_from_json_file(self, tmp_path):
        payload_path = tmp_path / "payload.json"
        payload_path.write_text(
            json.dumps(
                {
                    "headline": "Authored headline",
                    "kill_condition": "Fails if wrong",
                    "kill_condition_satisfiable_by_null": False,
                    "hypotheses": [
                        {"id": "H_main", "label": "Main"},
                        {"id": "H_null", "label": "Null"},
                    ],
                }
            )
        )
        target = tmp_path / "authored.claim.toml"
        result = runner.invoke(
            app,
            [
                "claim",
                "author",
                "--path",
                str(target),
                "--from-json",
                str(payload_path),
                "--workspace-root",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        assert target.exists()

    def test_author_requires_from_json(self, tmp_path):
        result = runner.invoke(
            app,
            [
                "claim",
                "author",
                "--path",
                str(tmp_path / "x.claim.toml"),
                "--workspace-root",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 1

    def test_author_rejects_invalid_json(self, tmp_path):
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        result = runner.invoke(
            app,
            [
                "claim",
                "author",
                "--path",
                str(tmp_path / "x.claim.toml"),
                "--from-json",
                str(bad),
                "--workspace-root",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 1


class TestGateCyclopts:
    def test_stamp_then_status_green(self, repo):
        result = runner.invoke(
            app,
            [
                "gate",
                "stamp",
                "--gate-name",
                "my_gate",
                "--result",
                "pass",
                "--workspace-root",
                str(repo),
            ],
        )
        assert result.exit_code == 0, result.output
        payload = _json(result.output)
        assert payload["ok"] is True
        assert payload["result"] == "pass"

        status = runner.invoke(
            app,
            [
                "gate",
                "status",
                "--gate-name",
                "my_gate",
                "--guards",
                "foo.py",
                "--workspace-root",
                str(repo),
            ],
        )
        assert status.exit_code == 0, status.output
        status_payload = _json(status.output)
        assert status_payload["state"] == "GREEN"

    def test_stamp_rejects_bad_result(self, repo):
        result = runner.invoke(
            app,
            [
                "gate",
                "stamp",
                "--gate-name",
                "my_gate",
                "--result",
                "bogus",
                "--workspace-root",
                str(repo),
            ],
        )
        # gate_stamp_tool signals this via a singular "error" key, which
        # render_or_exit does treat as a CLI failure (unlike attestation_
        # validate_tool's plural "errors" quirk elsewhere in this migration).
        assert result.exit_code == 1

    def test_status_unknown_when_never_stamped(self, repo):
        result = runner.invoke(
            app,
            [
                "gate",
                "status",
                "--gate-name",
                "never_stamped",
                "--guards",
                "foo.py",
                "--workspace-root",
                str(repo),
            ],
        )
        assert result.exit_code == 0, result.output
        payload = _json(result.output)
        assert payload["state"] == "UNKNOWN"


@pytest.fixture
def populated_catalog(tmp_catalog: Path) -> Path:
    init_catalog(tmp_catalog)
    r = Run(
        project_slug="prolix",
        command="python run_0.py",
        argv=["python", "run_0.py"],
        git_hash="abc123",
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
    )
    write_run(r, tmp_catalog)
    return tmp_catalog


def _first_run_id(catalog_dir: Path) -> str:
    from bathos.compact import compact

    compact(catalog_dir)
    db = duckdb.connect(str(catalog_dir / "bathos.db"))
    try:
        return db.execute("SELECT id FROM runs LIMIT 1").fetchone()[0]
    finally:
        db.close()


class TestPostmortemCyclopts:
    def test_scaffold_for_run_id(self, populated_catalog, tmp_path):
        run_id = _first_run_id(populated_catalog)
        result = runner.invoke(
            app,
            [
                "postmortem",
                "scaffold",
                "--run-id",
                run_id,
                "--catalog-dir",
                str(populated_catalog),
                "--workspace-root",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        payload = _json(result.output)
        assert payload["run_id"] == run_id
        assert Path(payload["path"]).exists()

    def test_scaffold_requires_exactly_one_id(self, populated_catalog, tmp_path):
        result = runner.invoke(
            app,
            [
                "postmortem",
                "scaffold",
                "--catalog-dir",
                str(populated_catalog),
                "--workspace-root",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 1

    def test_show_not_found_is_clean_error(self, tmp_path):
        result = runner.invoke(
            app,
            [
                "postmortem",
                "show",
                "--run-id",
                "does-not-exist",
                "--workspace-root",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 1

    def test_validate_missing_file_is_clean_error(self, tmp_path):
        result = runner.invoke(
            app,
            ["postmortem", "validate", "--path", str(tmp_path / "nope.postmortem.toml")],
        )
        assert result.exit_code == 0, result.output
        payload = _json(result.output)
        assert payload["validation_ok"] is False


class TestRefCyclopts:
    def test_list_includes_known_card(self):
        result = runner.invoke(app, ["ref", "list"])
        assert result.exit_code == 0, result.output
        payload = _json(result.output)
        assert any(c["id"] == "STAT-001" for c in payload["cards"])

    def test_show_known_card(self):
        result = runner.invoke(app, ["ref", "show", "STAT-001"])
        assert result.exit_code == 0, result.output
        payload = _json(result.output)
        assert payload["card"]["id"] == "STAT-001"

    def test_show_unknown_card_is_clean_error(self):
        result = runner.invoke(app, ["ref", "show", "NOPE-999"])
        assert result.exit_code == 1

    def test_search_finds_known_card(self):
        result = runner.invoke(app, ["ref", "search", "STAT-001"])
        assert result.exit_code == 0, result.output
        payload = _json(result.output)
        assert payload["count"] >= 1

    def test_applicable_returns_fired_and_unevaluable(self, tmp_path):
        result = runner.invoke(
            app, ["ref", "applicable", "--script", str(tmp_path / "no_such_script.py")]
        )
        assert result.exit_code == 0, result.output
        payload = _json(result.output)
        assert "fired" in payload
        assert "unevaluable" in payload
