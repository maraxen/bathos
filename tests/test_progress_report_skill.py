"""Smoke tests for the bathos-progress-report skill and plugin bundle parity."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSET_SKILL = ROOT / "agent_assets" / "skills" / "bathos-progress-report" / "SKILL.md"
PLUGIN_SKILL = ROOT / "plugin" / "skills" / "bathos-progress-report" / "SKILL.md"
MANIFEST = ROOT / ".praxia" / "manifest.toml"
SNAPSHOT = ROOT / "src" / "bathos" / "agent_plugin.json"


def _split_frontmatter(text: str) -> tuple[dict[str, str], str]:
    assert text.startswith("---\n"), "SKILL.md must start with YAML frontmatter"
    _, rest = text.split("---\n", 1)
    fm_block, body = rest.split("\n---\n", 1)
    parsed: dict[str, str] = {}
    for line in fm_block.splitlines():
        if ":" in line:
            key, val = line.split(":", 1)
            val = val.strip()
            if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
                val = val[1:-1]
            parsed[key.strip()] = val
    return parsed, body


def test_progress_report_skill_frontmatter_and_sections() -> None:
    text = ASSET_SKILL.read_text(encoding="utf-8")
    fm, _ = _split_frontmatter(text)
    assert fm["name"] == "bathos-progress-report"
    assert fm["description"]
    assert fm["description"] != ">"
    assert "description: >" not in text and 'description: ">"' not in text
    assert "when_to_use" in fm and "progress report" in fm["when_to_use"].lower()

    for heading in (
        "## Inputs to settle first",
        "## Sources: gather per project, evidence for every line",
        "## Selection: work it out with the researcher",
        "## Report structure",
        "## After the meeting",
        "## Optional integrations",
    ):
        assert heading in text, f"Missing required section: {heading}"

    # Advisor pronouns should remain gender-neutral per 5bec39fe.
    for gendered in (" his ", " him ", " he "):
        assert gendered not in text.lower(), f"Found gendered pronoun {gendered!r} in SKILL.md"

    # Referenced CLI subcommands must exist.
    assert "bth stepback status --campaign-id" in text
    assert "bth stepback brief" in text


def test_progress_report_skill_registered_and_synced_in_bundle() -> None:
    import tomllib

    manifest_data = tomllib.loads(MANIFEST.read_text(encoding="utf-8"))
    skill_entries = manifest_data["plugin"]["skills"]
    assert any(
        entry["name"] == "bathos-progress-report"
        and entry["path"] == "agent_assets/skills/bathos-progress-report/SKILL.md"
        for entry in skill_entries
    )

    assert PLUGIN_SKILL.is_file(), "plugin/skills/bathos-progress-report/SKILL.md missing"
    asset_fm, asset_body = _split_frontmatter(ASSET_SKILL.read_text(encoding="utf-8"))
    plugin_fm, plugin_body = _split_frontmatter(PLUGIN_SKILL.read_text(encoding="utf-8"))
    assert plugin_fm == asset_fm
    assert plugin_body == asset_body

    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    snapshot_skills = {s["name"]: s for s in snapshot["bundle"]["skills"]}
    assert "bathos-progress-report" in snapshot_skills
