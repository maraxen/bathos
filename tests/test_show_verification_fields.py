"""bth show must expose the fields the 'verify by record' rule depends on.

Backlog #5553: `outcome`, `sidecar_mode`, `sidecar_path` and `sidecar_sha256`
were stored on the Run but absent from the `show` projection (JSON / MCP
`get_run`) and the Rich detail view.
"""

import io
import json
from datetime import UTC, datetime
from pathlib import Path

from rich.console import Console

from bathos.catalog import init_catalog, write_run
from bathos.cli_cyclopts import app
from bathos.mcp import get_run_tool
from bathos.rich_fmt import render_run_detail
from bathos.schema import Run
from tests._cyclopts_runner import CyclopticRunner

runner = CyclopticRunner()

VERIFY_KEYS = ("outcome", "sidecar_mode", "sidecar_path", "sidecar_sha256")


def _make_run(run_id: str, **overrides) -> Run:
    fields = dict(
        id=run_id,
        project_slug="verifyproj",
        command="python scripts/experiments/x.py",
        argv=["python", "scripts/experiments/x.py"],
        git_hash="abc123def456",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC),
        duration_s=3.5,
        status="completed",
        exit_code=0,
        outcome="pass",
        sidecar_mode="enforced",
        sidecar_path="scripts/experiments/x.bth.toml",
        sidecar_sha256="9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    )
    fields.update(overrides)
    return Run(**fields)


def _catalog_with(tmp_path: Path, run: Run) -> Path:
    catalog = tmp_path / ".bth" / "catalog"
    init_catalog(catalog)
    write_run(run, catalog)
    return catalog


def test_show_json_includes_verification_fields(tmp_path: Path):
    run = _make_run("verify-run-0001")
    catalog = _catalog_with(tmp_path, run)

    result = runner.invoke(
        app, ["show", "--catalog-dir", str(catalog), "--run-id", run.id]
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["outcome"] == "pass"
    assert payload["sidecar_mode"] == "enforced"
    assert payload["sidecar_path"] == "scripts/experiments/x.bth.toml"
    assert payload["sidecar_sha256"] == run.sidecar_sha256


def test_show_json_keeps_existing_fields(tmp_path: Path):
    """Adding the verification fields must not drop the existing projection."""
    run = _make_run("verify-run-0002")
    catalog = _catalog_with(tmp_path, run)

    result = runner.invoke(
        app, ["show", "--catalog-dir", str(catalog), "--run-id", run.id]
    )

    payload = json.loads(result.output)
    for key in (
        "id",
        "project_slug",
        "command",
        "argv",
        "git_hash",
        "git_branch",
        "git_dirty",
        "status",
        "exit_code",
        "duration_s",
        "timestamp",
        "output_paths",
        "tags",
        "hostname",
    ):
        assert key in payload, key


def test_show_json_empty_verification_fields_are_explicit(tmp_path: Path):
    """An unset field is emitted as an empty value, never omitted."""
    run = _make_run(
        "verify-run-0003",
        outcome="",
        sidecar_mode="",
        sidecar_path="",
        sidecar_sha256="",
    )
    catalog = _catalog_with(tmp_path, run)

    result = runner.invoke(
        app, ["show", "--catalog-dir", str(catalog), "--run-id", run.id]
    )

    payload = json.loads(result.output)
    for key in VERIFY_KEYS:
        assert key in payload, key
        assert payload[key] == ""


def test_mcp_get_run_tool_includes_verification_fields(tmp_path: Path):
    run = _make_run("verify-run-0004")
    catalog = _catalog_with(tmp_path, run)

    payload = get_run_tool(run_id=run.id, catalog_dir=str(catalog))

    assert "error" not in payload
    for key in VERIFY_KEYS:
        assert key in payload, key
    assert payload["outcome"] == "pass"
    assert payload["sidecar_sha256"] == run.sidecar_sha256


def test_render_run_detail_shows_sidecar_sha256_and_outcome():
    run = _make_run("verify-run-0005")
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, width=200)

    render_run_detail(run, console=console)

    text = output.getvalue()
    assert "Sidecar SHA256:" in text
    assert run.sidecar_sha256 in text
    assert "Outcome:" in text
    assert "pass" in text


def test_render_run_detail_empty_outcome_shows_marker():
    """An empty outcome renders an explicit marker, not a missing line."""
    run = _make_run("verify-run-0006", outcome="", sidecar_sha256="")
    output = io.StringIO()
    console = Console(file=output, force_terminal=False, width=200)

    render_run_detail(run, console=console)

    text = output.getvalue()
    assert "Outcome:" in text
    assert "Sidecar SHA256:" in text
