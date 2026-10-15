"""Guard: a project config's recorded ``root`` is identity, not a filesystem root.

``bathos.workspace`` is the only module allowed to read ``<project_config>.root``
(it records the root as identity and then resolves the live filesystem root
itself, via the env -> git toplevel -> recorded root -> cwd ladder). Any other
module treating ``project_config.root`` as a directory to read or write bypasses
that ladder and silently points at a stale checkout (e.g. a deleted worktree).

This test scans ``src/bathos`` and fails on new occurrences outside
``workspace.py``. The matcher is deliberately narrow: it only matches attribute
access on names that conventionally hold a parsed project config, so unrelated
``.root`` attributes (dataclass fields, ``self.root``, dict keys) do not trip it.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC_ROOT = Path(__file__).resolve().parents[1] / "src" / "bathos"
ALLOWED_FILE = SRC_ROOT / "workspace.py"

# Names that conventionally hold a parsed project config (ProjectConfig instances).
PROJECT_CONFIG_ROOT = re.compile(
    r"\b(?:project_config|project_cfg|project_conf|pc|pcfg|cfg|project)\.root\b"
)


def _offending_lines(paths: list[Path]) -> list[str]:
    hits: list[str] = []
    for path in paths:
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if PROJECT_CONFIG_ROOT.search(line):
                hits.append(f"{path.relative_to(SRC_ROOT.parents[1])}:{lineno}: {line.strip()}")
    return hits


def _source_files_outside_workspace() -> list[Path]:
    return sorted(p for p in SRC_ROOT.rglob("*.py") if p != ALLOWED_FILE)


def test_no_project_config_root_used_as_fs_root_outside_workspace() -> None:
    hits = _offending_lines(_source_files_outside_workspace())
    assert not hits, (
        "project config `.root` read outside bathos/workspace.py. It is identity, not a "
        "filesystem root: resolve the live root via bathos.workspace.resolve_workspace() "
        "instead.\n" + "\n".join(hits)
    )


def test_matcher_positive_control_fires() -> None:
    """A matcher that cannot fire proves nothing: the pattern must catch a real usage."""
    assert PROJECT_CONFIG_ROOT.search("base = project_config.root / 'outputs'")
    assert PROJECT_CONFIG_ROOT.search("p = pc.root")


@pytest.mark.parametrize(
    "line",
    [
        "self.root = root",  # dataclass / instance field, not a project config
        "root = Path(project['root'])",  # dict key
        "fs_root = resolve(cwd)",
        "ws.root",  # unrelated object
    ],
)
def test_matcher_negative_control_does_not_fire(line: str) -> None:
    assert not PROJECT_CONFIG_ROOT.search(line)


def test_allowed_usage_still_exists_in_workspace() -> None:
    """If workspace.py stops reading pc.root, the exemption is stale and should go."""
    text = ALLOWED_FILE.read_text(encoding="utf-8")
    assert re.search(r"\bpc\.root\b", text), "workspace.py no longer reads pc.root; drop the exemption"
