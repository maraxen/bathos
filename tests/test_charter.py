"""Acceptance tests for Project Charter, Confirmation, and Purpose Ledger (spec 261104 slice 1).

Covers:
- AC-1: Human-only hash-bound confirmation, agent refusal, mandate immutability, MCP refusal.
- AC-2: Pivot live-purpose completeness, state transition to retired, cheapest_next_test validation.
- AC-3 (ext half): Preservation of arbitrary ext keys across parse, fold, and draft.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bathos.charter import (
    Charter,
    Purpose,
    charter_sha256,
    confirm,
    draft,
    dump_charter,
    parse_charter,
    pivot,
    status,
)
from bathos.cli_cyclopts import app
from bathos.errors import (
    CharterInvalidError,
    CharterMandateChangedError,
    CharterPivotIncompleteError,
    CharterUnauthorizedError,
)
from bathos.index import connect_read
from bathos.mcp import charter_confirm
from bathos.stepback.escalation import AGENT_ENV_VARS
from tests._cyclopts_runner import CyclopticRunner

runner = CyclopticRunner()


@pytest.fixture
def charter_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Set up isolated test catalog and project environment."""
    home_dir = tmp_path / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    (home_dir / ".bth").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home_dir))

    from bathos.runlog.writer import reset_writers_for_test
    reset_writers_for_test()

    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(cat_dir))
    monkeypatch.setenv("BTH_LOG_MODE", "1")

    proj_dir = tmp_path / "project"
    bth_dir = proj_dir / ".bth"
    bth_dir.mkdir(parents=True, exist_ok=True)

    # .bth.toml
    (proj_dir / ".bth.toml").write_text(
        f'[project]\nslug = "charter_test"\nroot = "{proj_dir}"\nid = "12345678-1234-5678-1234-567812345678"\n'
    )
    monkeypatch.setenv("BTH_PROJECT_SLUG", "charter_test")
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(proj_dir))

    # Chair token path in tmp_path with 0600 permissions
    token_file = home_dir / ".bth" / "chair_token"
    token_file.write_text("secret_chair_token_xyz\n")
    token_file.chmod(0o600)
    monkeypatch.setenv("BTH_CHAIR_TOKEN_PATH", str(token_file))

    # Helper to create initial charter
    def write_charter(mandate: str = "Explore multiscale protein design", purposes: dict | None = None) -> Path:
        if purposes is None:
            purposes = {
                "P1": Purpose(id="P1", text="Validate binding affinity", state="live"),
                "P2": Purpose(id="P2", text="Verify thermostability", state="live"),
            }
        ch = Charter(mandate=mandate, purposes=purposes)
        charter_file = bth_dir / "charter.toml"
        charter_file.write_text(dump_charter(ch), encoding="utf-8")
        return charter_file

    yield {
        "catalog_dir": cat_dir,
        "project_dir": proj_dir,
        "token_file": token_file,
        "write_charter": write_charter,
    }
    reset_writers_for_test()


def _clear_agent_vars(monkeypatch: pytest.MonkeyPatch):
    for var in AGENT_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _count_confirmed_events(cat_dir: Path) -> int:
    con = connect_read(cat_dir)
    try:
        row = con.execute("SELECT count(*) FROM charter_confirmations").fetchone()
        return row[0] if row else 0
    finally:
        con.close()


def _count_pivot_events(cat_dir: Path) -> int:
    con = connect_read(cat_dir)
    try:
        row = con.execute("SELECT count(*) FROM charter_pivots").fetchone()
        return row[0] if row else 0
    finally:
        con.close()


# ── AC-1 Tests ───────────────────────────────────────────────────────────────


def test_ac1_confirm_refused_under_agent_without_token(charter_env, monkeypatch: pytest.MonkeyPatch):
    """bth charter confirm with CLAUDE_AGENT set and no chair token exits non-zero and appends no event."""
    charter_env["write_charter"]()
    proj = charter_env["project_dir"]
    cat = charter_env["catalog_dir"]

    # Autonomous agent environment without token
    monkeypatch.setenv("CLAUDE_AGENT", "1")
    monkeypatch.setenv("BTH_CHAIR_TOKEN_PATH", str(charter_env["project_dir"] / "nonexistent_token"))

    # Direct API raises CharterUnauthorizedError
    with pytest.raises(CharterUnauthorizedError):
        confirm(proj, chair_token=None, interactive_tty=False, catalog_dir=cat)

    # CLI exits non-zero
    res = runner.invoke(app, ["charter", "confirm", "--project-root", str(proj), "--catalog-dir", str(cat)])
    assert res.exit_code != 0
    assert _count_confirmed_events(cat) == 0


def test_ac1_confirm_success_with_chair_token(charter_env, monkeypatch: pytest.MonkeyPatch):
    """With agent vars cleared and valid 0600 chair token, appends one event that folds into charter_confirmations."""
    charter_file = charter_env["write_charter"]()
    proj = charter_env["project_dir"]
    cat = charter_env["catalog_dir"]

    _clear_agent_vars(monkeypatch)

    res = confirm(proj, chair_token="secret_chair_token_xyz", interactive_tty=False, catalog_dir=cat)
    assert res["ok"] is True
    assert res["auth_mode"] == "token"
    assert _count_confirmed_events(cat) == 1

    # Status reports confirmed
    st = status(proj, catalog_dir=cat)
    assert st["state"] == "confirmed"
    assert st["current_sha256"] == charter_sha256(charter_file)
    assert st["last_confirmed_sha256"] == st["current_sha256"]
    assert st["confirmed_at"] is not None


def test_ac1_confirm_success_with_tty(charter_env, monkeypatch: pytest.MonkeyPatch):
    """With agent vars cleared and interactive_tty=True, confirmation succeeds with auth_mode='tty'."""
    charter_env["write_charter"]()
    proj = charter_env["project_dir"]
    cat = charter_env["catalog_dir"]

    _clear_agent_vars(monkeypatch)
    # Point away from token file to ensure tty was used
    monkeypatch.setenv("BTH_CHAIR_TOKEN_PATH", str(proj / "no_token"))

    res = confirm(proj, interactive_tty=True, catalog_dir=cat)
    assert res["ok"] is True
    assert res["auth_mode"] == "tty"
    assert _count_confirmed_events(cat) == 1


def test_ac1_status_unconfirmed_after_file_edit(charter_env, monkeypatch: pytest.MonkeyPatch):
    """After editing .bth/charter.toml, bth charter status reports unconfirmed and names last confirmed sha256."""
    charter_file = charter_env["write_charter"]()
    proj = charter_env["project_dir"]
    cat = charter_env["catalog_dir"]

    _clear_agent_vars(monkeypatch)
    confirm(proj, chair_token="secret_chair_token_xyz", interactive_tty=False, catalog_dir=cat)
    pre_edit_sha = charter_sha256(charter_file)

    # Edit file
    charter_file.write_text(charter_file.read_text(encoding="utf-8") + "\n# comment\n", encoding="utf-8")
    post_edit_sha = charter_sha256(charter_file)
    assert pre_edit_sha != post_edit_sha

    st = status(proj, catalog_dir=cat)
    assert st["state"] == "unconfirmed"
    assert st["current_sha256"] == post_edit_sha
    assert st["last_confirmed_sha256"] == pre_edit_sha

    # Test via CLI status command
    cli_res = runner.invoke(app, ["charter", "status", "--project-root", str(proj), "--catalog-dir", str(cat)])
    assert cli_res.exit_code == 0
    assert "State:                 unconfirmed" in cli_res.output
    assert pre_edit_sha in cli_res.output


def test_ac1_mandate_changed_refused_without_amend_flag(charter_env, monkeypatch: pytest.MonkeyPatch):
    """A changed mandate is refused without --amend-mandate; with the flag it records amended_mandate_from."""
    charter_env["write_charter"](mandate="Initial Mandate Text")
    proj = charter_env["project_dir"]
    cat = charter_env["catalog_dir"]

    _clear_agent_vars(monkeypatch)
    confirm(proj, chair_token="secret_chair_token_xyz", interactive_tty=False, catalog_dir=cat)
    orig_mandate_sha = hashlib_sha("Initial Mandate Text")

    # Change mandate text
    charter_env["write_charter"](mandate="Changed Mandate Text")

    # Refused without --amend-mandate
    with pytest.raises(CharterMandateChangedError):
        confirm(proj, amend_mandate=False, chair_token="secret_chair_token_xyz", interactive_tty=False, catalog_dir=cat)

    cli_res = runner.invoke(app, ["charter", "confirm", "--project-root", str(proj), "--chair-token", "secret_chair_token_xyz", "--catalog-dir", str(cat)])
    assert cli_res.exit_code != 0

    # Allowed with amend_mandate=True
    res = confirm(proj, amend_mandate=True, chair_token="secret_chair_token_xyz", interactive_tty=False, catalog_dir=cat)
    assert res["ok"] is True
    assert res["amended_mandate_from"] == orig_mandate_sha
    assert _count_confirmed_events(cat) == 2


@pytest.mark.anyio
async def test_ac1_mcp_confirm_always_refuses():
    """MCP charter_confirm always unconditionally refuses."""
    res = await charter_confirm(project_root="/tmp/any")
    assert res["ok"] is False
    assert res["error_code"] == "charter_agent_confirm_refused"
    assert "strictly human-only" in res["error"]


def hashlib_sha(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── AC-2 Tests ───────────────────────────────────────────────────────────────


def test_ac2_pivot_incomplete_refused(charter_env):
    """With live purposes P1 and P2, pivot naming only P1 is refused with CHARTER_PIVOT_INCOMPLETE listing P2."""
    charter_file = charter_env["write_charter"]()
    content_before = charter_file.read_text(encoding="utf-8")
    proj = charter_env["project_dir"]
    cat = charter_env["catalog_dir"]

    # Pivot naming only P1
    with pytest.raises(CharterPivotIncompleteError) as exc_info:
        pivot(
            proj,
            reason="Pivoting direction",
            dispositions={"P1": "reaffirm"},
            cheapest_next_test="none_pending",
            catalog_dir=cat,
        )
    assert "P2" in str(exc_info.value)

    # CLI refusal
    res = runner.invoke(
        app,
        [
            "charter",
            "pivot",
            "--project-root",
            str(proj),
            "--reason",
            "Pivoting direction",
            "--reaffirm",
            "P1",
            "--cheapest-next-test",
            "none_pending",
            "--catalog-dir",
            str(cat),
        ],
    )
    assert res.exit_code != 0
    assert "P2" in res.output

    # File must be untouched and no event appended
    assert charter_file.read_text(encoding="utf-8") == content_before
    assert _count_pivot_events(cat) == 0


def test_ac2_pivot_success_reaffirm_and_retire(charter_env, monkeypatch: pytest.MonkeyPatch):
    """Naming both live purposes with valid cheapest_next_test appends 1 event, retires purpose, leaves charter unconfirmed."""
    charter_file = charter_env["write_charter"]()
    proj = charter_env["project_dir"]
    cat = charter_env["catalog_dir"]

    # Confirm initially
    _clear_agent_vars(monkeypatch)
    confirm(proj, chair_token="secret_chair_token_xyz", interactive_tty=False, catalog_dir=cat)
    assert status(proj, catalog_dir=cat)["state"] == "confirmed"

    # Pivot reaffirming P1 and retiring P2
    res = pivot(
        proj,
        reason="P2 hypothesis disproved by run_001",
        dispositions={"P1": "reaffirm", "P2": "retire"},
        retired_reasons={"P2": "Binding failed in run_001"},
        cheapest_next_test="executed:run_001",
        catalog_dir=cat,
    )
    assert res["ok"] is True
    assert _count_pivot_events(cat) == 1

    # Check updated file
    ch = parse_charter(charter_file)
    assert ch.purposes["P1"].state == "live"
    assert ch.purposes["P2"].state == "retired"
    assert ch.purposes["P2"].retired_reason == "Binding failed in run_001"

    # Status is now unconfirmed
    st = status(proj, catalog_dir=cat)
    assert st["state"] == "unconfirmed"


def test_ac2_pivot_cheapest_next_test_validation(charter_env):
    """cheapest_next_test must match executed:<run>, cancelled:<reason>, or none_pending."""
    charter_env["write_charter"]()
    proj = charter_env["project_dir"]
    cat = charter_env["catalog_dir"]

    for bad_cnt in ("executed:", "cancelled:", "invalid_format", "", "executed"):
        with pytest.raises(CharterInvalidError):
            pivot(
                proj,
                reason="Pivot test",
                dispositions={"P1": "reaffirm", "P2": "reaffirm"},
                cheapest_next_test=bad_cnt,
                catalog_dir=cat,
            )

    for good_cnt in ("executed:run_42", "cancelled:budget_cut", "none_pending"):
        res = pivot(
            proj,
            reason="Pivot test",
            dispositions={"P1": "reaffirm", "P2": "reaffirm"},
            cheapest_next_test=good_cnt,
            catalog_dir=cat,
        )
        assert res["ok"] is True


# ── AC-3 (ext half) Tests ───────────────────────────────────────────────────


def test_ac3_ext_canonical_json_preserved(charter_env, monkeypatch: pytest.MonkeyPatch):
    """Arbitrary [purpose.p1.ext] keys give equal canonical JSON across parse, fold, and draft."""
    proj = charter_env["project_dir"]
    cat = charter_env["catalog_dir"]

    ext_arbitrary = {
        "bool_val": True,
        "float_val": 3.14159,
        "int_val": 42,
        "list_val": [1, "two", 3.0],
        "nested_table": {
            "inner_int": 99,
            "inner_list": [True, False],
            "sub_table": {"alpha": "val"},
        },
        "unicode_str": "Test with \u00e9, \u03c0, and \u2603",
    }

    purposes = {
        "P1": Purpose(id="P1", text="Purpose with complex ext", state="live", ext=ext_arbitrary),
    }

    charter_file = charter_env["write_charter"](mandate="Ext preservation mandate", purposes=purposes)

    # 1. Canonical JSON of parsed charter
    parsed = parse_charter(charter_file)
    p1_ext = parsed.purposes["P1"].ext
    canonical_from_parse = json.dumps(p1_ext, sort_keys=True, separators=(",", ":"))

    # 2. Confirm and check canonical JSON from folded charter_confirmations row
    _clear_agent_vars(monkeypatch)
    confirm(proj, chair_token="secret_chair_token_xyz", interactive_tty=False, catalog_dir=cat)

    con = connect_read(cat)
    try:
        row = con.execute(
            "SELECT purposes_json FROM charter_confirmations WHERE project = 'charter_test' ORDER BY ts DESC, eid DESC"
        ).fetchone()
        assert row is not None
        folded_purposes = json.loads(row[0])
        folded_p1_ext = folded_purposes["P1"]["ext"]
        canonical_from_fold = json.dumps(folded_p1_ext, sort_keys=True, separators=(",", ":"))
    finally:
        con.close()

    # 3. Draft output re-parsed
    draft_file = draft(proj)
    drafted = parse_charter(draft_file)
    draft_p1_ext = drafted.purposes["P1"].ext
    canonical_from_draft = json.dumps(draft_p1_ext, sort_keys=True, separators=(",", ":"))

    # Assert equality of all three canonical JSON representations
    assert canonical_from_parse == canonical_from_fold
    assert canonical_from_fold == canonical_from_draft


def test_draft_scaffold_when_no_charter_exists(tmp_path: Path):
    """bth charter draft without an existing charter creates a scaffold at .bth/charter.draft.toml."""
    empty_proj = tmp_path / "empty_project"
    empty_proj.mkdir()
    d_path = draft(empty_proj)
    assert d_path.is_file()
    assert d_path.name == "charter.draft.toml"
    content = d_path.read_text(encoding="utf-8")
    assert 'mandate = ""' in content
    assert "# [purpose.example]" in content
