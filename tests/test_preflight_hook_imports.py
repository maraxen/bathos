"""The hook module must import only the standard library (it runs before tool calls)."""

from __future__ import annotations

import subprocess
import sys

FORBIDDEN = (
    "bathos.cli_cyclopts",
    "cyclopts",
    "duckdb",
    "pyarrow",
    "cisternal",
    "pydantic",
    "rich",
)


def test_importing_preflight_hook_loads_no_third_party_module():
    code = (
        "import sys\n"
        "import bathos.hooks.preflight_hook\n"
        f"bad = [m for m in {FORBIDDEN!r} if m in sys.modules]\n"
        "print('LOADED:' + ','.join(bad))\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "LOADED:", proc.stdout
