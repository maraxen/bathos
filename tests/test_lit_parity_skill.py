"""Smoke and contract tests for the bathos-literature-parity skill (#4379, AC-16/17).

Verifies:
1. Artifact presence in both ``agent_assets/skills/bathos-literature-parity/``
   and ``plugin/skills/bathos-literature-parity/`` as well as ``.praxia/manifest.toml``.
2. YAML frontmatter and required Markdown structure across ``SKILL.md`` and the
   bundled ``assets/`` phase templates, config template, and example runner.
3. Byte-level parity (body + assets) and semantic frontmatter parity between
   ``agent_assets/skills/`` and ``plugin/skills/``.
4. Live code alignment for CLI commands (``bth campaign attest-parity``,
   ``bth claim scaffold``, ``bth check``, ``bth sprint-audit``), MCP tool
   (``claim_attest_parity``), ``scripts/validation/parity_validate.py``, and
   ``bathos.parity`` APIs.
5. Negative/mutation tests proving broken or missing skill invariants fail.
"""

from __future__ import annotations

import ast
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
AGENT_SKILL_DIR = REPO_ROOT / "agent_assets" / "skills" / "bathos-literature-parity"
PLUGIN_SKILL_DIR = REPO_ROOT / "plugin" / "skills" / "bathos-literature-parity"
MANIFEST_PATH = REPO_ROOT / ".praxia" / "manifest.toml"

EXPECTED_PHASE_TEMPLATES = (
    "01_reconstruct.md",
    "02_reconcile.md",
    "03_refute.md",
    "04_adjudicate.md",
    "05_verdict.md",
)
EXPECTED_ASSETS = (
    *EXPECTED_PHASE_TEMPLATES,
    "example_parity_validate.py",
    "parity.bth.toml.template",
)
REQUIRED_HEADINGS = (
    "## When to use literature-parity validation",
    "## The 5-phase protocol",
    "## Configuration: `parity.bth.toml`",
    "## Orchestrator-owned re-derivation lock (Constraint 1)",
    "## Integration with claim-tier gates",
    "## Grading: the cap-lattice ceiling table",
)
REQUIRED_PARITY_TOML_FIELDS = (
    "paper_pdf",
    "impl_paths",
    "reference_code",
    "citation_note",
    "recon_lenses",
    "attack_lenses",
    "hypotheses",
    "equivalence_bound",
    "N",
    "M",
)


def _split_frontmatter(text: str) -> tuple[dict, str]:
    """Parse leading YAML frontmatter and return ``(metadata, body)``."""
    assert text.startswith("---\n") or text.startswith("---\r\n"), (
        "SKILL.md must start with YAML frontmatter delimiter '---'"
    )
    assert "description: >" not in text and 'description: ">"' not in text, (
        "SKILL.md frontmatter must not use folded YAML 'description: >'"
    )
    parts = text.split("---", 2)
    assert len(parts) == 3, "SKILL.md frontmatter must be closed with '---'"
    raw_fm, body = parts[1], parts[2]
    loaded = yaml.safe_load(raw_fm)
    assert isinstance(loaded, dict), "SKILL.md frontmatter must parse to a mapping"
    return loaded, body


def _validate_skill_tree(skill_dir: Path) -> tuple[dict, str]:
    """Validate structure, frontmatter, headings, and bundled assets for a skill dir."""
    skill_md = skill_dir / "SKILL.md"
    assert skill_md.is_file(), f"Missing SKILL.md at {skill_md}"
    raw = skill_md.read_text(encoding="utf-8")
    assert raw.strip(), f"SKILL.md is empty at {skill_md}"

    frontmatter, body = _split_frontmatter(raw)
    assert frontmatter.get("name") == "bathos-literature-parity", (
        f"Unexpected skill name: {frontmatter.get('name')!r}"
    )
    for key in ("description", "when_to_use"):
        val = frontmatter.get(key)
        assert isinstance(val, str) and val.strip(), (
            f"SKILL.md frontmatter missing non-empty {key!r}"
        )

    for heading in REQUIRED_HEADINGS:
        assert heading in body, f"SKILL.md missing required section: {heading}"

    for phase_num in range(1, 6):
        assert f"Phase {phase_num}:" in body, f"SKILL.md missing Phase {phase_num} protocol step"

    for verdict in ("PARITY", "PARTIAL", "FAIL"):
        assert verdict in body, f"SKILL.md missing graded verdict {verdict!r}"

    for required_field in ("paper_pdf", "impl_paths"):
        assert required_field in body, f"SKILL.md missing required field {required_field!r}"

    assets_dir = skill_dir / "assets"
    assert assets_dir.is_dir(), f"Missing assets/ directory at {assets_dir}"

    actual_assets = tuple(sorted(p.name for p in assets_dir.iterdir() if p.is_file()))
    expected_sorted = tuple(sorted(EXPECTED_ASSETS))
    missing = set(expected_sorted) - set(actual_assets)
    extra = set(actual_assets) - set(expected_sorted)
    assert not missing, f"Missing bundled asset(s) in {assets_dir}: {sorted(missing)}"
    assert not extra, f"Unexpected asset(s) in {assets_dir}: {sorted(extra)}"

    for idx, template_name in enumerate(EXPECTED_PHASE_TEMPLATES, start=1):
        content = (assets_dir / template_name).read_text(encoding="utf-8")
        assert content.strip(), f"Phase template {template_name} is empty"
        assert content.lstrip().startswith(f"# Phase {idx}:"), (
            f"{template_name} must start with '# Phase {idx}:'"
        )

    reconcile_text = (assets_dir / "02_reconcile.md").read_text(encoding="utf-8")
    for clause_verdict in ("MATCH", "DEVIATION", "MISSING", "AMBIGUOUS"):
        assert clause_verdict in reconcile_text, (
            f"02_reconcile.md missing clause verdict {clause_verdict!r}"
        )

    verdict_text = (assets_dir / "05_verdict.md").read_text(encoding="utf-8")
    for grade in ("PARITY", "PARTIAL", "FAIL"):
        assert grade in verdict_text, f"05_verdict.md missing grade {grade!r}"

    return frontmatter, body


def _validate_skill_parity(agent_dir: Path, plugin_dir: Path) -> None:
    """Verify agent_assets/skills/ and plugin/skills/ copies remain in sync."""
    agent_fm, agent_body = _split_frontmatter((agent_dir / "SKILL.md").read_text(encoding="utf-8"))
    plugin_fm, plugin_body = _split_frontmatter(
        (plugin_dir / "SKILL.md").read_text(encoding="utf-8")
    )
    assert agent_fm == plugin_fm, (
        f"Frontmatter diverged between agent_assets and plugin: {agent_fm!r} != {plugin_fm!r}"
    )
    assert agent_body == plugin_body, (
        "SKILL.md body diverged between agent_assets/skills/ and plugin/skills/"
    )

    agent_assets = {p.name: p for p in (agent_dir / "assets").iterdir() if p.is_file()}
    plugin_assets = {p.name: p for p in (plugin_dir / "assets").iterdir() if p.is_file()}
    assert set(agent_assets) == set(plugin_assets), (
        f"Asset file sets diverged: {sorted(agent_assets)} vs {sorted(plugin_assets)}"
    )
    for name in sorted(agent_assets):
        assert agent_assets[name].read_bytes() == plugin_assets[name].read_bytes(), (
            f"Bundled asset {name!r} diverged between agent_assets/skills/ and plugin/skills/"
        )


def _validate_code_references(skill_text: str, repo_root: Path) -> None:
    """Verify CLI, MCP, and validation script references in SKILL.md exist in code."""
    from bathos import mcp
    from bathos.cli_cyclopts import app

    assert "bth claim attest-parity" not in skill_text, (
        "SKILL.md references non-existent 'bth claim attest-parity'; "
        "CLI command is 'bth campaign attest-parity'"
    )

    expected_cli_paths: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("bth campaign attest-parity", ("campaign", "attest-parity")),
        ("bth claim scaffold", ("claim", "scaffold")),
        ("bth check", ("check",)),
        ("bth sprint-audit", ("sprint-audit",)),
    )
    for phrase, command_path in expected_cli_paths:
        assert phrase in skill_text, f"SKILL.md missing CLI reference {phrase!r}"
        node = app
        for part in command_path:
            assert part in node, f"CLI command {phrase!r} missing segment {part!r} in app"
            node = node[part]

    # Verify every single-line `bth <subcmd>` invocation mentioned in SKILL.md resolves.
    for match in re.finditer(r"`bth ([a-z][a-z0-9-]*(?:\s+[a-z][a-z0-9-]*)?)", skill_text):
        tokens = match.group(1).split()
        assert tokens[0] in app, f"SKILL.md references unknown CLI command 'bth {tokens[0]}'"
        if len(tokens) == 2 and not tokens[1].startswith("-"):
            sub_app = app[tokens[0]]
            # Only sub-apps have nested command names; top-level commands take positional args.
            if hasattr(sub_app, "_commands") and sub_app._commands:
                assert tokens[1] in sub_app, (
                    f"SKILL.md references unknown CLI subcommand 'bth {' '.join(tokens)}'"
                )

    assert "claim_attest_parity" in skill_text, (
        "SKILL.md missing MCP tool reference 'claim_attest_parity'"
    )
    assert hasattr(mcp, "claim_attest_parity"), (
        "MCP tool 'claim_attest_parity' missing from bathos.mcp"
    )

    assert "parity_validate" in skill_text, (
        "SKILL.md missing reference to 'parity_validate' validation script"
    )
    parity_script = repo_root / "scripts" / "validation" / "parity_validate.py"
    parity_sidecar = repo_root / "scripts" / "validation" / "parity_validate.bth.toml"
    assert parity_script.is_file(), f"Referenced validation script missing: {parity_script}"
    assert parity_sidecar.is_file(), f"Companion validation sidecar missing: {parity_sidecar}"


class TestLiteratureParitySkillArtifacts:
    """AC-16 & AC-17: Presence, structure, and agent_assets/plugin parity."""

    @pytest.mark.parametrize("skill_dir", [AGENT_SKILL_DIR, PLUGIN_SKILL_DIR])
    def test_skill_directory_and_assets_valid(self, skill_dir: Path) -> None:
        _validate_skill_tree(skill_dir)

    def test_agent_assets_and_plugin_skill_in_sync(self) -> None:
        _validate_skill_parity(AGENT_SKILL_DIR, PLUGIN_SKILL_DIR)

    def test_manifest_declares_literature_parity_skill(self) -> None:
        manifest = tomllib.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        skills = manifest.get("plugin", {}).get("skills", [])
        matches = [s for s in skills if s.get("name") == "bathos-literature-parity"]
        assert len(matches) == 1, f"Expected 1 manifest entry, got {matches!r}"
        declared_path = REPO_ROOT / matches[0]["path"]
        assert declared_path == AGENT_SKILL_DIR / "SKILL.md"
        assert declared_path.is_file()


class TestLiteratureParitySkillCodeAlignment:
    """Verify documented CLI/MCP/config/script surfaces match current code."""

    def test_skill_cli_mcp_and_script_references_exist(self) -> None:
        skill_text = (AGENT_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        _validate_code_references(skill_text, REPO_ROOT)

    def test_parity_toml_template_covers_schema_fields(self) -> None:
        template_path = AGENT_SKILL_DIR / "assets" / "parity.bth.toml.template"
        template_text = template_path.read_text(encoding="utf-8")
        assert "[parity]" in template_text
        for field_name in REQUIRED_PARITY_TOML_FIELDS:
            assert re.search(rf"^\s*{re.escape(field_name)}\s*=", template_text, re.MULTILINE), (
                f"parity.bth.toml.template missing schema field {field_name!r}"
            )

    def test_example_parity_validate_parses_and_runs(self, tmp_path: Path) -> None:
        example_path = AGENT_SKILL_DIR / "assets" / "example_parity_validate.py"
        source = example_path.read_text(encoding="utf-8")
        ast.parse(source, filename=str(example_path))
        assert "from bathos.parity import ParityEvidence, compute_grade" in source

        env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
        proc = subprocess.run(
            [sys.executable, str(example_path)],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        # The example intentionally simulates a FAIL verdict (exit code 1) with JSON on stdout.
        assert proc.returncode == 1, f"Unexpected exit {proc.returncode}: {proc.stderr}"
        payload = json.loads(proc.stdout)
        assert payload["parity_grade"] in {"PARITY", "PARTIAL", "FAIL"}
        assert isinstance(payload["clause_parity_pct"], float)
        assert payload["success"] is False


class TestLiteratureParitySkillNegativeInvariants:
    """Negative/mutation tests ensuring broken or missing invariants actually fail."""

    def test_missing_skill_md_fails(self, tmp_path: Path) -> None:
        copy = tmp_path / "skill"
        shutil.copytree(AGENT_SKILL_DIR, copy)
        (copy / "SKILL.md").unlink()
        with pytest.raises(AssertionError, match="Missing SKILL.md"):
            _validate_skill_tree(copy)

    def test_missing_bundled_asset_fails(self, tmp_path: Path) -> None:
        copy = tmp_path / "skill"
        shutil.copytree(AGENT_SKILL_DIR, copy)
        (copy / "assets" / "03_refute.md").unlink()
        with pytest.raises(AssertionError, match="03_refute.md"):
            _validate_skill_tree(copy)

    def test_unexpected_extra_asset_fails(self, tmp_path: Path) -> None:
        copy = tmp_path / "skill"
        shutil.copytree(AGENT_SKILL_DIR, copy)
        (copy / "assets" / "stray.md").write_text("stray", encoding="utf-8")
        with pytest.raises(AssertionError, match="Unexpected asset"):
            _validate_skill_tree(copy)

    def test_folded_yaml_description_fails(self, tmp_path: Path) -> None:
        copy = tmp_path / "skill"
        shutil.copytree(AGENT_SKILL_DIR, copy)
        skill_md = copy / "SKILL.md"
        text = skill_md.read_text(encoding="utf-8").replace(
            "description: Validating a reimplemented baseline",
            "description: >\n  Validating a reimplemented baseline",
        )
        skill_md.write_text(text, encoding="utf-8")
        with pytest.raises(AssertionError, match="folded YAML"):
            _validate_skill_tree(copy)

    def test_missing_required_section_fails(self, tmp_path: Path) -> None:
        copy = tmp_path / "skill"
        shutil.copytree(AGENT_SKILL_DIR, copy)
        skill_md = copy / "SKILL.md"
        text = skill_md.read_text(encoding="utf-8").replace(
            "## The 5-phase protocol", "## Protocol"
        )
        skill_md.write_text(text, encoding="utf-8")
        with pytest.raises(AssertionError, match="The 5-phase protocol"):
            _validate_skill_tree(copy)

    def test_diverged_plugin_body_fails_parity(self, tmp_path: Path) -> None:
        agent_copy = tmp_path / "agent"
        plugin_copy = tmp_path / "plugin"
        shutil.copytree(AGENT_SKILL_DIR, agent_copy)
        shutil.copytree(PLUGIN_SKILL_DIR, plugin_copy)
        skill_md = plugin_copy / "SKILL.md"
        skill_md.write_text(skill_md.read_text(encoding="utf-8") + "\ndrift\n", encoding="utf-8")
        with pytest.raises(AssertionError, match="SKILL.md body diverged"):
            _validate_skill_parity(agent_copy, plugin_copy)

    def test_diverged_asset_fails_parity(self, tmp_path: Path) -> None:
        agent_copy = tmp_path / "agent"
        plugin_copy = tmp_path / "plugin"
        shutil.copytree(AGENT_SKILL_DIR, agent_copy)
        shutil.copytree(PLUGIN_SKILL_DIR, plugin_copy)
        (plugin_copy / "assets" / "05_verdict.md").write_text("stale", encoding="utf-8")
        with pytest.raises(AssertionError, match="05_verdict.md.*diverged"):
            _validate_skill_parity(agent_copy, plugin_copy)

    def test_stale_cli_reference_fails_code_alignment(self) -> None:
        skill_text = (AGENT_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8").replace(
            "bth campaign attest-parity", "bth claim attest-parity"
        )
        with pytest.raises(AssertionError, match="bth claim attest-parity"):
            _validate_code_references(skill_text, REPO_ROOT)

    def test_unknown_cli_command_fails_code_alignment(self) -> None:
        skill_text = (
            (AGENT_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
            + "\nSee `bth campaign bogus-subcmd`.\n"
        )
        with pytest.raises(AssertionError, match="bogus-subcmd"):
            _validate_code_references(skill_text, REPO_ROOT)

    def test_missing_validation_script_fails_code_alignment(self, tmp_path: Path) -> None:
        skill_text = (AGENT_SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
        with pytest.raises(AssertionError, match="parity_validate.py"):
            _validate_code_references(skill_text, tmp_path)
