"""Smoke tests for `using-bathos` skill updates, mirror parity, and `get_run` #3333 behavior."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from bathos.catalog import init_catalog, write_run
from bathos.mcp import get_run_tool
from bathos.query import AmbiguousRunId, CatalogError, get_run
from bathos.schema import Run

REPO_ROOT = Path(__file__).resolve().parent.parent
CANONICAL_SKILL = REPO_ROOT / "agent_assets" / "skills" / "using-bathos" / "SKILL.md"
MIRROR_SKILL = REPO_ROOT / "agent_assets" / "using_bathos" / "SKILL.md"
PLUGIN_SKILL = REPO_ROOT / "plugin" / "skills" / "using-bathos" / "SKILL.md"
AGENT_PLUGIN_JSON = REPO_ROOT / "src" / "bathos" / "agent_plugin.json"

REQUIRED_SECTIONS = (
    "### Entrypoint must be a `.py` file path — never `x.py::fn` (`#3333`)",
    "## Literature-Review Cross-Checks, `claims JSONL` & Coverage Probes (`#2346`)",
    "## Code & Port Parity Verification (`#2323`)",
    "## PyTorch / ML Device-Placement Sanity Checks (`#2177`)",
)

REQUIRED_TERMS = (
    # #3333
    "x.py::fn",
    ".suffix",
    "unambiguous UUID prefix",
    # #3327
    "[result_schema.metrics]",
    "metrics.loss",
    "IS NULL",
    "= null",
    "--result",
    # #2346
    "claims.jsonl",
    "[[review.literature]]",
    "capability_probe",
    "bth claim validate",
    # #2323
    "comparison_kind",
    "oracle_match",
    "$BTH_SEED",
    "[differential]",
    # #2177
    "map_location",
    "torch.load",
    "torch.cuda.is_available()",
)


def _split_frontmatter(text: str) -> tuple[dict, str]:
    assert text.startswith("---\n")
    parts = text.split("---", 2)
    assert len(parts) == 3, "SKILL.md frontmatter must be closed with '---'"
    fm = yaml.safe_load(parts[1])
    assert isinstance(fm, dict)
    return fm, parts[2]


def _validate_skill_text(text: str) -> None:
    fm, body = _split_frontmatter(text)
    assert fm.get("name") == "using-bathos"
    assert isinstance(fm.get("description"), str) and fm["description"].strip()
    assert "description: >" not in text and 'description: ">"' not in text
    for heading in REQUIRED_SECTIONS:
        assert heading in body, f"Missing required section: {heading}"
    for term in REQUIRED_TERMS:
        assert term in body, f"Missing required term: {term!r}"


def test_using_bathos_skill_has_all_required_sections_and_terms() -> None:
    text = CANONICAL_SKILL.read_text(encoding="utf-8")
    _validate_skill_text(text)


def test_using_bathos_skill_mirrors_and_snapshot_in_sync() -> None:
    canonical_raw = CANONICAL_SKILL.read_text(encoding="utf-8")
    canonical_fm, canonical_body = _split_frontmatter(canonical_raw)

    assert MIRROR_SKILL.is_file(), "agent_assets/using_bathos/SKILL.md missing"
    assert MIRROR_SKILL.read_text(encoding="utf-8") == canonical_raw

    assert PLUGIN_SKILL.is_file(), "plugin/skills/using-bathos/SKILL.md missing"
    plugin_fm, plugin_body = _split_frontmatter(PLUGIN_SKILL.read_text(encoding="utf-8"))
    assert plugin_fm == canonical_fm
    assert plugin_body == canonical_body

    snapshot = json.loads(AGENT_PLUGIN_JSON.read_text(encoding="utf-8"))
    skills = {s["name"]: s for s in snapshot["bundle"]["skills"]}
    assert "using-bathos" in skills
    assert skills["using-bathos"]["body"] == canonical_body.lstrip("\n")


def test_using_bathos_negative_missing_section_fails() -> None:
    bad = CANONICAL_SKILL.read_text(encoding="utf-8").replace(
        "## PyTorch / ML Device-Placement Sanity Checks (`#2177`)",
        "## Removed Section",
    )
    with pytest.raises(AssertionError, match="PyTorch / ML Device-Placement"):
        _validate_skill_text(bad)


def test_get_run_smoke_prefix_ambiguity_and_not_found(tmp_path: Path) -> None:
    cat = tmp_path / "catalog"
    init_catalog(cat)

    def _run(rid: str) -> Run:
        return Run(
            id=rid,
            project_slug="smoke",
            command="python train.py",
            argv=["python", "train.py"],
            git_hash="deadbeef",
            git_branch="main",
            git_dirty=False,
            timestamp=datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC),
            status="completed",
            exit_code=0,
        )

    write_run(_run("1111aaaa-0001"), cat)
    write_run(_run("1111bbbb-0002"), cat)
    write_run(_run("2222cccc-0003"), cat)

    # Unambiguous prefix resolves cleanly via get_run and get_run_tool
    found = get_run("2222cc", cat)
    assert found is not None and found.id == "2222cccc-0003"
    tool_ok = get_run_tool(run_id="2222cc", catalog_dir=str(cat))
    assert tool_ok["id"] == "2222cccc-0003"

    # Ambiguous prefix raises AmbiguousRunId
    with pytest.raises(AmbiguousRunId) as exc_info:
        get_run("1111", cat)
    assert exc_info.value.candidates == ["1111aaaa-0001", "1111bbbb-0002"]

    # Not-found behavior
    assert get_run("9999", cat) is None
    with pytest.raises(CatalogError, match="Run not found"):
        get_run("9999", cat, raise_if_not_found=True)
    tool_missing = get_run_tool(run_id="9999", catalog_dir=str(cat))
    assert "not found" in tool_missing["error"].lower()
