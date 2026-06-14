"""Tests for bathos error codes and resolution hints."""

from bathos.errors import BathosErrorCode, RESOLUTION_HINTS


def test_resolution_hints_complete():
    """Verify that every BathosErrorCode has a non-empty RESOLUTION_HINTS entry."""
    missing = set(BathosErrorCode) - set(RESOLUTION_HINTS)
    assert not missing, f"Missing RESOLUTION_HINTS entries for: {missing}"


def test_resolution_hints_nonempty():
    """Verify that every RESOLUTION_HINTS entry is a non-empty string."""
    for code, hint in RESOLUTION_HINTS.items():
        assert hint and hint.strip(), f"Empty resolution hint for {code}"
