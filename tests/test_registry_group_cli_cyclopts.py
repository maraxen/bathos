"""CLI-level smoke tests for the registry-driven grouped-command batch
(backlog #4702 Milestone 2: anchor / attestation / blast-radius (partial) /
outputs / query), exercised through the preview cyclopts app
(`bathos.cli_cyclopts.app`, `bth-preview`) via the `CyclopticRunner` shim.

Mirrors `tests/test_top_level_cli_cyclopts.py`'s depth: one happy-path +
(where a natural error case exists) one error-path per command. Exhaustive
edge-case coverage already lives at the Python-API layer (`test_anchor.py`,
`test_attestation.py`, `test_blast_radius_*.py`, `test_outputs.py`,
`test_readback*.py`) and the existing typer-CLI layer (`test_anchor_cli.py`,
`test_attestation_cli.py`, `test_blast_radius_cli.py`), both untouched by
this migration.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bathos.catalog import init_catalog, write_run
from bathos.cli_cyclopts import app
from bathos.schema import Run
from tests._cyclopts_runner import CyclopticRunner

runner = CyclopticRunner()


def _json(output: str) -> dict:
    """Parse the JSON payload out of `output`, tolerating a leading warning
    line (e.g. cisternal's lazy-telemetry-init notice on a cold first call)
    mixed into the same captured stdout+stderr stream."""
    return json.loads(output[output.index("{") :])

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


def _fix_commit(repo: Path) -> str:
    (repo / "foo.py").write_text("a = 2\n")
    _git(["add", "foo.py"], repo)
    _git(["commit", "-m", "fix bug"], repo)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


class TestAnchorCyclopts:
    def test_insert_then_get_round_trips(self, tmp_catalog):
        result = runner.invoke(
            app,
            [
                "anchor",
                "insert",
                "--path",
                "fig.png",
                "--sha256",
                "a" * 64,
                "--kind",
                "figure",
                "--content-hash",
                "b" * 64,
                "--catalog-dir",
                str(tmp_catalog),
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["anchor"]["path"] == "fig.png"
        assert payload["anchor"]["content_hash"] == "b" * 64

        get_result = runner.invoke(
            app,
            [
                "anchor",
                "get",
                "--path",
                "fig.png",
                "--sha256",
                "a" * 64,
                "--catalog-dir",
                str(tmp_catalog),
            ],
        )
        assert get_result.exit_code == 0, get_result.output
        fetched = json.loads(get_result.output)
        assert fetched["anchor"]["kind"] == "figure"

    def test_find_with_no_matches_returns_empty(self, tmp_catalog):
        result = runner.invoke(
            app, ["anchor", "find", "--kind", "figure", "--catalog-dir", str(tmp_catalog)]
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["count"] == 0

    def test_insert_missing_path_is_clean_error(self, tmp_catalog):
        result = runner.invoke(
            app,
            [
                "anchor",
                "insert",
                "--sha256",
                "a" * 64,
                "--kind",
                "figure",
                "--catalog-dir",
                str(tmp_catalog),
            ],
        )
        assert result.exit_code == 1

    def test_figure_register_happy_path(self, tmp_catalog):
        result = runner.invoke(
            app,
            [
                "anchor",
                "figure-register",
                "--asset-sha256",
                "a" * 64,
                "--sidecar-ref",
                "fig.figure.toml",
                "--figure-kind",
                "chord_diagram",
                "--catalog-dir",
                str(tmp_catalog),
            ],
        )
        assert result.exit_code == 0, result.output
        payload = _json(result.output)
        assert payload["figure_entry"]["figure_kind"] == "chord_diagram"


class TestAttestationCyclopts:
    def test_scaffold_creates_template(self, tmp_path):
        result = runner.invoke(
            app,
            [
                "attestation",
                "scaffold",
                "--kind",
                "oracle_match",
                "--workspace-root",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["ok"] is True

    def test_scaffold_rejects_bad_kind(self, tmp_path):
        result = runner.invoke(
            app,
            ["attestation", "scaffold", "--kind", "bogus", "--workspace-root", str(tmp_path)],
        )
        assert result.exit_code == 1

    def test_register_then_query_via_query_attestation(self, tmp_catalog, tmp_path):
        content_hash = "a" * 64
        src = tmp_path / "attest.toml"
        src.write_text(ORACLE_MATCH_TOML.format(content_hash=content_hash, oracle_sha="b" * 64))

        result = runner.invoke(
            app,
            ["attestation", "register", "--path", str(src), "--catalog-dir", str(tmp_catalog)],
        )
        assert result.exit_code == 0, result.output

        query_result = runner.invoke(
            app,
            [
                "query",
                "attestation",
                "--content-hash",
                content_hash,
                "--catalog-dir",
                str(tmp_catalog),
            ],
        )
        assert query_result.exit_code == 0, query_result.output
        payload = json.loads(query_result.output)
        assert payload["attestation"]["verdict"] == "PASS"

    def test_validate_valid_file(self, tmp_path):
        content_hash = "c" * 64
        src = tmp_path / "attest.toml"
        src.write_text(ORACLE_MATCH_TOML.format(content_hash=content_hash, oracle_sha="d" * 64))
        result = runner.invoke(app, ["attestation", "validate", "--path", str(src)])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["ok"] is True

    def test_validate_invalid_file_reports_ok_false(self, tmp_path):
        # attestation_validate_tool signals a failed validation via
        # {"ok": false, "errors": [...]} -- plural "errors", not the singular
        # "error" key render_or_exit checks for -- so this renders as a
        # normal (exit 0) JSON payload with ok=False, not a CLI error exit.
        # An existing, unrelated quirk this migration preserves rather than
        # silently changing (same shape as validate_sidecar's, documented in
        # the Milestone 2 top-level batch).
        src = tmp_path / "bad.toml"
        src.write_text(
            '[attestation]\nkind = "oracle_match"\nverdict = "PASS"\n'
            '[attestation.attested]\nrun_id = "r1"\noutput_path = "o"\ncontent_hash = "x"\n'
        )
        result = runner.invoke(app, ["attestation", "validate", "--path", str(src)])
        assert result.exit_code == 0, result.output
        payload = _json(result.output)
        assert payload["ok"] is False


class TestBlastRadiusCyclopts:
    def test_assess_by_commit_flags_matching_run(self, repo, tmp_catalog):
        init_catalog(tmp_catalog)
        pre_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
        ).stdout.strip()
        run = Run(
            project_slug="p",
            command="foo.py",
            argv=["foo.py"],
            git_hash=pre_sha,
            git_branch="main",
            git_dirty=False,
        )
        write_run(run, tmp_catalog)
        fix_sha = _fix_commit(repo)

        result = runner.invoke(
            app,
            [
                "blast-radius",
                "assess",
                "--commit",
                fix_sha,
                "--project-root",
                str(repo),
                "--catalog-dir",
                str(tmp_catalog),
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["flagged_count"] == 1

        status = runner.invoke(
            app,
            [
                "query",
                "blast-status",
                "--entity-type",
                "run",
                "--entity-id",
                run.id,
                "--catalog-dir",
                str(tmp_catalog),
            ],
        )
        assert status.exit_code == 0, status.output
        status_payload = json.loads(status.output)
        assert status_payload["status"] == "affected"

    def test_assess_requires_an_anchor(self, repo, tmp_catalog):
        init_catalog(tmp_catalog)
        result = runner.invoke(
            app,
            [
                "blast-radius",
                "assess",
                "--project-root",
                str(repo),
                "--catalog-dir",
                str(tmp_catalog),
            ],
        )
        assert result.exit_code == 1

    def test_clear_requires_reason(self, tmp_catalog):
        init_catalog(tmp_catalog)
        result = runner.invoke(
            app,
            [
                "blast-radius",
                "clear",
                "--entity-type",
                "run",
                "--entity-id",
                "run-x",
                "--catalog-dir",
                str(tmp_catalog),
            ],
        )
        assert result.exit_code == 1

    def test_clear_writes_record(self, tmp_catalog):
        init_catalog(tmp_catalog)
        result = runner.invoke(
            app,
            [
                "blast-radius",
                "clear",
                "--entity-type",
                "run",
                "--entity-id",
                "run-x",
                "--reason",
                "verified fine",
                "--catalog-dir",
                str(tmp_catalog),
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["to_state"] == "cleared"


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
    import duckdb

    from bathos.compact import compact

    compact(catalog_dir)
    db = duckdb.connect(str(catalog_dir / "bathos.db"))
    try:
        return db.execute("SELECT id FROM runs LIMIT 1").fetchone()[0]
    finally:
        db.close()


class TestOutputsCyclopts:
    def test_list_happy_path(self, populated_catalog):
        run_id = _first_run_id(populated_catalog)
        result = runner.invoke(
            app,
            ["outputs", "list", "--run-id", run_id, "--catalog-dir", str(populated_catalog)],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["run_id"] == run_id

    def test_list_missing_run_is_clean_error(self, populated_catalog):
        result = runner.invoke(
            app,
            [
                "outputs",
                "list",
                "--run-id",
                "does-not-exist",
                "--catalog-dir",
                str(populated_catalog),
            ],
        )
        assert result.exit_code == 1

    def test_summary_happy_path(self, populated_catalog):
        result = runner.invoke(
            app, ["outputs", "summary", "--catalog-dir", str(populated_catalog)]
        )
        assert result.exit_code == 0, result.output


class TestQueryCyclopts:
    def test_resolve_pin_unknown_returns_error(self, tmp_catalog):
        init_catalog(tmp_catalog)
        result = runner.invoke(
            app,
            [
                "query",
                "resolve-pin",
                "--run-id",
                "does-not-exist",
                "--output-path",
                "out.txt",
                "--catalog-dir",
                str(tmp_catalog),
            ],
        )
        assert result.exit_code == 1

    def test_trust_state_unknown_hash(self, tmp_catalog):
        init_catalog(tmp_catalog)
        result = runner.invoke(
            app,
            [
                "query",
                "trust-state",
                "--content-hash",
                "a" * 64,
                "--catalog-dir",
                str(tmp_catalog),
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["trust_state"] == "unknown"

    def test_figures_empty(self, tmp_catalog):
        init_catalog(tmp_catalog)
        result = runner.invoke(app, ["query", "figures", "--catalog-dir", str(tmp_catalog)])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["count"] == 0

    def test_candidates_missing_campaign_id_is_clean_error(self, tmp_catalog):
        init_catalog(tmp_catalog)
        result = runner.invoke(app, ["query", "candidates", "--catalog-dir", str(tmp_catalog)])
        assert result.exit_code == 1

    def test_blast_status_defaults_clean(self, tmp_catalog):
        init_catalog(tmp_catalog)
        result = runner.invoke(
            app,
            [
                "query",
                "blast-status",
                "--entity-type",
                "run",
                "--entity-id",
                "run-x",
                "--catalog-dir",
                str(tmp_catalog),
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "clean"
