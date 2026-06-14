"""Tests for bathos error codes and resolution hints."""

import ast
from pathlib import Path
from bathos.errors import BathosErrorCode, EXCEPTION_TO_CODE, RESOLUTION_HINTS

BUILTIN_EXCEPTIONS = {
    "ValueError", "RuntimeError", "KeyError", "NotImplementedError", "TypeError",
    "AttributeError", "IndexError", "SystemExit", "Exception", "BaseException",
    "StopIteration", "GeneratorExit", "FileExistsError", "FileNotFoundError", "OSError",
}
EXCLUDED_FILES = {"cli.py"}

def _collect_raised_exception_classes(src_root: Path) -> set[str]:
    raised = set()
    for py_file in src_root.rglob("*.py"):
        if py_file.name in EXCLUDED_FILES:
            continue
        tree = ast.parse(py_file.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise) and node.exc is not None:
                exc = node.exc
                if isinstance(exc, ast.Call) and isinstance(exc.func, ast.Name):
                    raised.add(exc.func.id)
                elif isinstance(exc, ast.Name):
                    raised.add(exc.id)
    return raised

def test_every_domain_exception_has_registered_code():
    src_root = Path(__file__).parent.parent / "src" / "bathos"
    raised = _collect_raised_exception_classes(src_root)
    domain_exceptions = raised - BUILTIN_EXCEPTIONS
    unregistered = domain_exceptions - set(EXCEPTION_TO_CODE)
    assert not unregistered, (
        f"Domain exceptions raised in src/bathos/ with no EXCEPTION_TO_CODE entry: {unregistered}. "
        "Add them to EXCEPTION_TO_CODE in src/bathos/errors.py."
    )


def test_resolution_hints_complete():
    """Verify that every BathosErrorCode has a non-empty RESOLUTION_HINTS entry."""
    missing = set(BathosErrorCode) - set(RESOLUTION_HINTS)
    assert not missing, f"Missing RESOLUTION_HINTS entries for: {missing}"


def test_resolution_hints_nonempty():
    """Verify that every RESOLUTION_HINTS entry is a non-empty string."""
    for code, hint in RESOLUTION_HINTS.items():
        assert hint and hint.strip(), f"Empty resolution hint for {code}"
