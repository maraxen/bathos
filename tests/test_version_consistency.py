"""The version is written down twice; this keeps the two copies equal.

`pyproject.toml`'s `[project] version` is what gets built and published.
`bathos.__version__` is what the running package reports, and what
`tests/test_plugin_export.py` asserts the exported plugin manifest carries.

Nothing connected them. A release that bumped one and forgot the other would ship a
wheel whose metadata disagreed with the version it reports at runtime, and every test
would still pass — `test_plugin_export` compares the manifest against `__version__`,
so it stays self-consistently wrong. Caught while cutting 0.13.0a2, where the
`pyproject.toml` bump landed first and `__init__.py` was still on 0.13.0a1.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import bathos

_PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def test_pyproject_version_matches_package_dunder_version() -> None:
    assert _PYPROJECT.is_file(), f"expected pyproject.toml at {_PYPROJECT}"
    declared = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))["project"]["version"]

    assert declared == bathos.__version__, (
        f"pyproject.toml declares {declared!r} but bathos.__version__ is "
        f"{bathos.__version__!r}. Both must be bumped together — the first is what is "
        f"built and published, the second is what the package reports at runtime and "
        f"what the exported plugin manifest inherits."
    )
