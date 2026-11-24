"""Tests for #2789 non-JAX profiling & vectorized-vs-loop skill guidance and bundle sync."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
USING_CANONICAL = REPO_ROOT / "agent_assets" / "skills" / "using-bathos" / "SKILL.md"
USING_MIRROR = REPO_ROOT / "agent_assets" / "using_bathos" / "SKILL.md"
USING_PLUGIN = REPO_ROOT / "plugin" / "skills" / "using-bathos" / "SKILL.md"
CLUSTER_CANONICAL = REPO_ROOT / "agent_assets" / "skills" / "bathos-cluster" / "SKILL.md"
CLUSTER_PLUGIN = REPO_ROOT / "plugin" / "skills" / "bathos-cluster" / "SKILL.md"


def _split_frontmatter(text: str) -> tuple[dict, str]:
    assert text.startswith("---\n")
    parts = text.split("---", 2)
    assert len(parts) == 3, "SKILL.md frontmatter must be closed with '---'"
    fm = yaml.safe_load(parts[1])
    assert isinstance(fm, dict)
    return fm, parts[2]


def _validate_using_bathos_2789(text: str) -> None:
    assert "#### Non-JAX workloads: production-shape profiling & vectorized-vs-loop check (`#2789`)" in text
    for term in (
        "per_unit_s",
        "est_wall_s",
        "setup/compile/device/host-Python",
        "recompile or graph-break count",
        "batched/vectorized form",
        "np.allclose",
        "torch.allclose",
    ):
        assert term in text, f"Missing #2789 term in using-bathos/SKILL.md: {term!r}"


def _validate_cluster_2789(text: str) -> None:
    assert "### Pre-Submission Profiling, Budget & Vectorization Gate (`#2789`)" in text
    for term in (
        "bth preflight",
        "per_unit_s",
        "probe_record",
        "compile/device/host/Python",
        "recompile/graph-break count",
        "batched/vectorized form",
    ):
        assert term in text, f"Missing #2789 term in bathos-cluster/SKILL.md: {term!r}"


def test_skills_include_2789_guidance_and_mirrors_match() -> None:
    using_text = USING_CANONICAL.read_text(encoding="utf-8")
    _validate_using_bathos_2789(using_text)
    assert USING_MIRROR.read_text(encoding="utf-8") == using_text
    using_fm, using_body = _split_frontmatter(using_text)
    plugin_using_fm, plugin_using_body = _split_frontmatter(USING_PLUGIN.read_text(encoding="utf-8"))
    assert plugin_using_fm == using_fm
    assert plugin_using_body == using_body

    cluster_text = CLUSTER_CANONICAL.read_text(encoding="utf-8")
    _validate_cluster_2789(cluster_text)
    cluster_fm, cluster_body = _split_frontmatter(cluster_text)
    plugin_cluster_fm, plugin_cluster_body = _split_frontmatter(
        CLUSTER_PLUGIN.read_text(encoding="utf-8")
    )
    assert plugin_cluster_fm == cluster_fm
    assert plugin_cluster_body == cluster_body


def test_negative_missing_2789_section_fails() -> None:
    bad_using = USING_CANONICAL.read_text(encoding="utf-8").replace(
        "#### Non-JAX workloads: production-shape profiling & vectorized-vs-loop check (`#2789`)",
        "#### Removed",
    )
    with pytest.raises(AssertionError):
        _validate_using_bathos_2789(bad_using)

    bad_cluster = CLUSTER_CANONICAL.read_text(encoding="utf-8").replace(
        "### Pre-Submission Profiling, Budget & Vectorization Gate (`#2789`)",
        "### Removed",
    )
    with pytest.raises(AssertionError):
        _validate_cluster_2789(bad_cluster)
