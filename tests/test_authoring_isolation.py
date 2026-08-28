"""The generic half of the authoring layer must not depend on bathos.

``models`` and ``render`` implement a mechanism -- typed payload in, canonical TOML out
-- that is not specific to bathos's document schemas. Keeping them free of ``bathos.*``
imports means promoting them into cisternal later is a move plus a re-export shim,
exactly as ``bathos.git`` / ``bathos.git_pin`` now shim over ``cisternal.provenance``.

Without this test the coupling would creep back in silently, and the promotion would
quietly become a rewrite. ``scaffolds`` is deliberately exempt: it encodes bathos policy
(which fields a new claim prompts for, and how the prompts are worded), so it may import
``models`` but stays on the bathos side of the line.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

AUTHORING = Path(__file__).resolve().parents[1] / "src" / "bathos" / "authoring"

# Modules that must stay liftable. `scaffolds` and `__init__` are bathos-side.
GENERIC_MODULES = ["models.py", "render.py"]

# The one bathos import a generic module may make: its own sibling.
ALLOWED_PREFIXES = ("bathos.authoring.",)


def _bathos_imports(source: str) -> list[str]:
    """Every ``bathos.*`` module name imported by *source*, at any nesting depth.

    ``ast.walk`` covers imports inside function bodies too, so a deferred import cannot
    dodge this check by hiding in a call site.
    """
    found: list[str] = []

    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found.extend(a.name for a in node.names if a.name.startswith("bathos"))
        # level > 0 is a relative import, which within this package is a sibling.
        elif (
            isinstance(node, ast.ImportFrom)
            and node.level == 0
            and node.module
            and node.module.startswith("bathos")
        ):
            found.append(node.module)

    return found


@pytest.mark.parametrize("module", GENERIC_MODULES)
def test_generic_modules_import_nothing_from_bathos_outside_authoring(module):
    path = AUTHORING / module
    assert path.exists(), f"{module} is missing -- update GENERIC_MODULES if it was renamed"

    offending = [
        name for name in _bathos_imports(path.read_text()) if not name.startswith(ALLOWED_PREFIXES)
    ]
    assert not offending, (
        f"{module} imports from bathos outside the authoring package: {offending}. "
        "The generic render/model layer must stay liftable into cisternal -- move the "
        "bathos-specific part into scaffolds.py or the caller instead."
    )


def test_the_import_walk_actually_detects_a_violation():
    """Guard the guard: a check that cannot fail protects nothing."""
    assert _bathos_imports("from bathos.claim import parse_claim") == ["bathos.claim"]
    assert _bathos_imports("import bathos.catalog") == ["bathos.catalog"]
    assert _bathos_imports("def f():\n    from bathos.query import find_runs\n") == ["bathos.query"]
    assert _bathos_imports("from bathos.authoring.models import ClaimPayload") == [
        "bathos.authoring.models"
    ]


def test_generic_modules_import_cleanly():
    """Smoke check that the layer imports without dragging in the rest of bathos."""
    import importlib

    for module in ("bathos.authoring.models", "bathos.authoring.render"):
        importlib.import_module(module)
