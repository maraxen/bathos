"""The CLI and MCP authoring surfaces must not drift apart.

Parity is guaranteed by a shared core (``bathos.authoring.write.author_claim``). As of
the final cutover (backlog #4702), both surfaces are cyclopts commands registered via
``cisternal.wire()`` -- `claim_author` (MCP) and `claim_author_cli_tool` (CLI, reading
``--from-json``/stdin) both delegate to the same `claim_author_tool`, which is the one
function that actually calls `author_claim`. Before the cutover, bathos's CLI was Typer
and the two surfaces were written and pinned separately here (wiring an async tool onto
Typer fails silently -- the command exits 0 having awaited nothing -- which is worse
than not wiring it at all); the structural checks below still verify the same
invariant, just against the now-unified surface.

Following the precedent already documented at ``bathos/postmortem.py``: "Shared by the
CLI and the MCP tool so the two surfaces cannot diverge (regression: debt #479)."
"""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "bathos"

VALID_CLAIM = {
    "headline": "Sparse attention matches dense below 4k context",
    "kill_condition": "Sparse trails dense by more than 2 points at any context length",
    "kill_condition_satisfiable_by_null": False,
    "hypotheses": [
        {"id": "H_sparse_parity", "label": "Sparse reaches parity"},
        {"id": "H_null_misspec", "label": "Parity is a measurement artefact"},
    ],
    "assumptions": [{"id": "A_tok", "label": "Both arms share a tokenizer"}],
    "confounds": [{"id": "C_len", "label": "Sequence length distribution differs"}],
    "discriminability": [
        {
            "hypothesis_a": "H_sparse_parity",
            "hypothesis_b": "H_null_misspec",
            "planned_run_label": "ctx_2k",
            "predicted_outcome": "pass",
        },
        {
            "hypothesis_a": "H_sparse_parity",
            "hypothesis_b": "H_null_misspec",
            "planned_run_label": "ctx_8k",
            "predicted_outcome": "fail",
        },
    ],
    "union_gate_clauses": [
        {
            "id": "C_main",
            "description": "Does sparse reach parity below 4k?",
            "hypothesis_ids": ["H_sparse_parity", "H_null_misspec"],
        }
    ],
}


def _mcp_token() -> str:
    return (Path.home() / ".bth" / "mcp_token").read_text().strip()


def _author_via_cli(target: Path, payload: dict, tmp_path: Path):
    """Invoke `bth claim author` via the cyclopts CLI.

    claim_author_tool (mcp.py) treats `path` as relative to the workspace
    root and rejects a target that resolves outside it (a design decision
    from the extraction-heavy batch, not present in the retired Typer
    command) -- so `target` must be inside `tmp_path`, passed relative, with
    `tmp_path` itself pinned as `--workspace-root`. Mirrors `_author_via_mcp`
    below, which already does the equivalent (`path=str(target.relative_to(
    workspace))`, `workspace_root=str(workspace)`).
    """
    from tests._cyclopts_runner import CyclopticRunner

    payload_file = tmp_path / "payload.json"
    payload_file.write_text(json.dumps(payload))
    return CyclopticRunner().invoke(
        __import__("bathos.cli_cyclopts", fromlist=["app"]).app,
        [
            "claim",
            "author",
            str(target.relative_to(tmp_path)),
            "--from-json",
            str(payload_file),
            "--workspace-root",
            str(tmp_path),
        ],
    )


def _author_via_mcp(target: Path, payload: dict, workspace: Path):
    from bathos.mcp import claim_author

    return asyncio.run(
        claim_author(
            claim=payload,
            path=str(target.relative_to(workspace)),
            workspace_root=str(workspace),
            token=_mcp_token(),
        )
    )


# ---------------------------------------------------------------------------
# Behavioural parity
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (Path.home() / ".bth" / "mcp_token").exists(),
    reason="MCP write token not present on this machine",
)
def test_both_surfaces_produce_identical_bytes(tmp_path):
    """The same payload authored either way yields the same document."""
    cli_target = tmp_path / "cli.claim.toml"
    mcp_target = tmp_path / "mcp.claim.toml"

    assert _author_via_cli(cli_target, VALID_CLAIM, tmp_path).exit_code == 0
    assert _author_via_mcp(mcp_target, VALID_CLAIM, tmp_path)["ok"] is True

    assert cli_target.read_bytes() == mcp_target.read_bytes()


@pytest.mark.skipif(
    not (Path.home() / ".bth" / "mcp_token").exists(),
    reason="MCP write token not present on this machine",
)
def test_both_surfaces_refuse_the_same_payload(tmp_path):
    """A typo is refused on both surfaces, and neither writes anything."""
    bad = dict(VALID_CLAIM)
    bad["headlien"] = "typo"

    cli_target = tmp_path / "cli.claim.toml"
    mcp_target = tmp_path / "mcp.claim.toml"

    cli_result = _author_via_cli(cli_target, bad, tmp_path)
    mcp_result = _author_via_mcp(mcp_target, bad, tmp_path)

    assert cli_result.exit_code == 1
    assert mcp_result["ok"] is False
    assert mcp_result["error_code"] == "document_invalid"
    assert "headlien" in cli_result.output
    assert mcp_result["unknown_keys"] == ["headlien"]
    assert not cli_target.exists()
    assert not mcp_target.exists()


# ---------------------------------------------------------------------------
# Structural parity: both surfaces must route through the shared core
# ---------------------------------------------------------------------------


def _calls_author_claim(source: str, func_name: str) -> bool:
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == func_name:
            return any(
                isinstance(call.func, ast.Name) and call.func.id == "author_claim"
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
            )
    raise AssertionError(f"{func_name} not found -- rename it here too")


def test_cli_command_routes_through_the_shared_core():
    """bth claim author (claim_author_cli_tool, mcp.py) must delegate to
    claim_author_tool -- the shared core test_mcp_tool_routes_through_the_shared_core
    (below) already confirms calls author_claim. Regression note (final cutover,
    backlog #4702): bathos.cli (Typer) was deleted; claim_author_cmd no longer
    exists -- this test used to check that function directly, back when the CLI
    and MCP surfaces were separate frameworks that couldn't share one function."""
    mcp_source = (SRC / "mcp.py").read_text()
    tree = ast.parse(mcp_source)
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "claim_author_cli_tool":
            calls_claim_author_tool = any(
                isinstance(call.func, ast.Name) and call.func.id == "claim_author_tool"
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
            )
            break
    else:
        raise AssertionError("claim_author_cli_tool not found -- rename it here too")
    assert calls_claim_author_tool, (
        "bth claim author (claim_author_cli_tool) must delegate to claim_author_tool, "
        "not reimplement the render/validate/write pipeline"
    )


def test_mcp_tool_routes_through_the_shared_core():
    # backlog #4702 Milestone 2: claim_author's own body no longer calls
    # author_claim directly -- it delegates to claim_author_tool, the new
    # plain sync function shared with the cyclopts CLI's `claim author`
    # command (src/bathos/cli_cyclopts.py). The architectural invariant this
    # test guards -- both surfaces route through ONE shared core, so they
    # cannot silently drift apart -- still holds; the call chain just has one
    # more hop now (claim_author -> claim_author_tool -> author_claim), so
    # both links are checked instead of one direct call.
    mcp_source = (SRC / "mcp.py").read_text()
    assert _calls_author_claim(mcp_source, "claim_author_tool"), (
        "claim_author_tool (the shared core both claim_author and "
        "claim_author_cli_tool delegate to) must call authoring.write.author_claim"
    )

    tree = ast.parse(mcp_source)
    claim_author_calls_claim_author_tool = False
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "claim_author":
            claim_author_calls_claim_author_tool = any(
                isinstance(call.func, ast.Name) and call.func.id == "claim_author_tool"
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
            )
            break
    else:
        raise AssertionError("claim_author not found -- rename it here too")
    assert claim_author_calls_claim_author_tool, (
        "the claim_author MCP tool must delegate to claim_author_tool, the shared core"
    )
