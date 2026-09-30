"""Project-configurable sidecar enforcement directories (`.bth.toml [enforcement] dirs`).

Written BEFORE the implementation. The defect (protamer debt #1960, bathos #2210): hard sidecar enforcement is gated on the
script PATH -- ``ENFORCED_DIRS = {experiments, benchmarks, validation}`` -- so a project that keeps its tracked experiment
scripts in ``scripts/method/`` or ``scripts/release/`` is never gated: a missing, invalid or hash-drifted sidecar cannot stop a
run, and it records ``sidecar_mode=""`` with exit 0. The fix is ADDITIVE configuration: the built-in set is unchanged, and a
project may add directories in ``.bth.toml``::

    [enforcement]
    dirs = ["scripts/method", "release"]

An entry without ``/`` is a path COMPONENT name matched anywhere in the script's path (the existing semantics). An entry with
``/`` is a PROJECT-ROOT-RELATIVE prefix, so ``scripts/method`` does not match ``scripts/methodology`` or ``vendor/scripts/method``.
A typo must never silently disable enforcement, so an invalid ``[enforcement]`` block raises instead of being ignored.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from bathos.config import EnforcementConfigError, parse_enforcement
from bathos.errors import EXCEPTION_TO_CODE, BathosErrorCode
from bathos.sidecar import ENFORCED_DIRS, is_in_enforced_dir


def project(tmp_path: Path, enforcement_toml: str | None) -> Path:
    """A minimal project root with an optional ``[enforcement]`` block; returns the root."""
    body = '[project]\nslug = "proj"\nroot = "."\n'
    if enforcement_toml is not None:
        body += "\n" + textwrap.dedent(enforcement_toml)
    (tmp_path / ".bth.toml").write_text(body)
    return tmp_path


def script_at(root: Path, *parts: str) -> Path:
    path = root.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("print('hi')")
    return path


# ---------------------------------------------------------------- the built-in behaviour is unchanged


def test_builtin_set_is_unchanged():
    assert set(ENFORCED_DIRS) == {"experiments", "benchmarks", "validation"}


def test_without_any_config_only_the_builtin_dirs_are_enforced(tmp_path):
    root = project(tmp_path, None)
    assert is_in_enforced_dir(script_at(root, "scripts", "experiments", "a.py")) is True
    assert is_in_enforced_dir(script_at(root, "scripts", "method", "a.py")) is False
    assert is_in_enforced_dir(script_at(root, "scripts", "release", "a.py")) is False


def test_no_config_file_at_all_is_defaults_and_not_an_error(tmp_path):
    assert is_in_enforced_dir(script_at(tmp_path, "scripts", "method", "a.py")) is False
    assert is_in_enforced_dir(script_at(tmp_path, "benchmarks", "a.py")) is True


def test_config_without_an_enforcement_block_is_defaults(tmp_path):
    root = project(tmp_path, None)
    assert is_in_enforced_dir(script_at(root, "scripts", "method", "a.py")) is False


# ---------------------------------------------------------------- configured names and prefixes


def test_a_configured_component_name_is_enforced_anywhere_in_the_path(tmp_path):
    root = project(tmp_path, '[enforcement]\ndirs = ["method"]\n')
    assert is_in_enforced_dir(script_at(root, "scripts", "method", "a.py")) is True
    assert is_in_enforced_dir(script_at(root, "method", "a.py")) is True
    assert is_in_enforced_dir(script_at(root, "scripts", "other", "a.py")) is False


def test_a_configured_prefix_is_relative_to_the_project_root_and_exact(tmp_path):
    root = project(tmp_path, '[enforcement]\ndirs = ["scripts/method"]\n')
    assert is_in_enforced_dir(script_at(root, "scripts", "method", "a.py")) is True
    assert is_in_enforced_dir(script_at(root, "scripts", "method", "sub", "deep", "a.py")) is True
    # the negative controls that make the prefix semantics testable: near-misses must NOT match
    assert is_in_enforced_dir(script_at(root, "scripts", "methodology", "a.py")) is False
    assert is_in_enforced_dir(script_at(root, "scripts", "method_extra", "a.py")) is False
    assert is_in_enforced_dir(script_at(root, "vendor", "scripts", "method", "a.py")) is False
    assert is_in_enforced_dir(script_at(root, "scripts", "a.py")) is False


def test_a_prefix_does_not_match_a_file_named_like_the_directory(tmp_path):
    root = project(tmp_path, '[enforcement]\ndirs = ["scripts/method"]\n')
    assert (
        is_in_enforced_dir(script_at(root, "scripts", "method")) is False
    )  # the prefix itself, not inside it


def test_a_file_named_like_a_configured_name_is_not_a_directory_match(tmp_path):
    root = project(tmp_path, '[enforcement]\ndirs = ["method"]\n')
    assert (
        is_in_enforced_dir(script_at(root, "scripts", "method")) is False
    )  # a FILE called 'method'
    assert (
        is_in_enforced_dir(script_at(root, "tools", "method", "x")) is True
    )  # a directory called 'method'


def test_a_parent_of_the_project_root_named_like_a_configured_name_does_not_count(tmp_path):
    outer = tmp_path / "method" / "checkout"  # the project itself lives under a dir called 'method'
    outer.mkdir(parents=True)
    root = project(outer, '[enforcement]\ndirs = ["method"]\n')
    assert is_in_enforced_dir(script_at(root, "scripts", "other", "a.py")) is False


def test_configured_dirs_are_additive_and_the_builtins_still_apply(tmp_path):
    root = project(tmp_path, '[enforcement]\ndirs = ["scripts/release"]\n')
    assert is_in_enforced_dir(script_at(root, "scripts", "release", "a.py")) is True
    assert is_in_enforced_dir(script_at(root, "experiments", "a.py")) is True
    assert is_in_enforced_dir(script_at(root, "validation", "a.py")) is True


def test_several_entries_mix_names_and_prefixes(tmp_path):
    root = project(
        tmp_path, '[enforcement]\ndirs = ["scripts/method", "release", "scripts/eval"]\n'
    )
    for parts in (
        ("scripts", "method", "a.py"),
        ("x", "release", "a.py"),
        ("scripts", "eval", "a.py"),
    ):
        assert is_in_enforced_dir(script_at(root, *parts)) is True
    assert is_in_enforced_dir(script_at(root, "scripts", "data", "a.py")) is False


def test_the_config_is_found_by_walking_up_from_a_deeply_nested_script(tmp_path):
    root = project(tmp_path, '[enforcement]\ndirs = ["scripts/method"]\n')
    assert is_in_enforced_dir(script_at(root, "scripts", "method", "a", "b", "c", "d.py")) is True


def test_an_empty_dirs_list_adds_nothing(tmp_path):
    root = project(tmp_path, "[enforcement]\ndirs = []\n")
    assert is_in_enforced_dir(script_at(root, "scripts", "method", "a.py")) is False


# ---------------------------------------------------------------- a typo must never silently disable enforcement


@pytest.mark.parametrize(
    "section",
    [
        {"dirs": "scripts/method"},  # a bare string, not a list (would iterate characters)
        {
            "dirs": "method"
        },  # ...and this one would iterate into valid single-letter NAMES and be silently accepted
        {"dirs": [1, 2]},
        {"dirs": [""]},
        {"dirs": ["  "]},
        {"dirs": ["/abs/path"]},
        {"dirs": ["../escape"]},
        {"dirs": ["scripts/../x"]},
        {"dirs": ["back\\slash"]},
        {"dirs": ["scripts//method"]},
        {"dirs": ["."]},
        {"dirs": ["scripts/method/"]},  # trailing slash is ambiguous: reject rather than guess
        {"dir": ["scripts/method"]},  # misspelt key
        {"dirs": ["a"], "extra": 1},
        "not a table",
    ],
)
def test_invalid_enforcement_blocks_are_rejected_not_ignored(section):
    with pytest.raises(EnforcementConfigError):
        parse_enforcement(section)


def test_valid_blocks_parse_to_names_and_prefixes():
    names, prefixes = parse_enforcement({"dirs": ["method", "scripts/release", "a/b/c"]})
    assert names == frozenset({"method"})
    assert prefixes == (("scripts", "release"), ("a", "b", "c"))
    assert parse_enforcement(None) == (frozenset(), ())
    assert parse_enforcement({}) == (frozenset(), ())


def test_an_invalid_block_in_a_real_config_raises_for_a_script_outside_the_builtin_dirs(tmp_path):
    root = project(tmp_path, '[enforcement]\ndirs = "scripts/method"\n')
    with pytest.raises(EnforcementConfigError):
        is_in_enforced_dir(script_at(root, "scripts", "method", "a.py"))


def test_a_builtin_dir_short_circuits_before_the_config_is_read(tmp_path):
    root = project(tmp_path, '[enforcement]\ndirs = "broken"\n')
    assert (
        is_in_enforced_dir(script_at(root, "experiments", "a.py")) is True
    )  # already enforced; config irrelevant


def test_unparseable_toml_raises_rather_than_silently_dropping_enforcement(tmp_path):
    (tmp_path / ".bth.toml").write_text("[project\nslug = ")
    with pytest.raises(EnforcementConfigError):
        is_in_enforced_dir(script_at(tmp_path, "scripts", "method", "a.py"))


def test_the_exception_is_registered_in_the_error_code_registry():
    assert EXCEPTION_TO_CODE["EnforcementConfigError"] == BathosErrorCode.INVALID_PARAM


# ---------------------------------------------------------------- the gate and the runner actually use it


def test_gate_check_blocks_a_configured_dir_script_with_no_sidecar(tmp_path):
    from bathos.prereg import GateErrorCode, SidecarBundle, gate_check

    root = project(tmp_path, '[enforcement]\ndirs = ["scripts/method"]\n')
    script = script_at(root, "scripts", "method", "a.py")
    res = gate_check(script, SidecarBundle(path=None, sha256="", found=False), "collaborative")
    assert res.ok is False
    assert (
        res.error_payload is not None
        and res.error_payload.error_code == GateErrorCode.SIDECAR_MISSING
    )


def test_gate_check_fails_closed_on_an_invalid_enforcement_block(tmp_path):
    from bathos.prereg import GateErrorCode, SidecarBundle, gate_check

    root = project(tmp_path, '[enforcement]\ndirs = "scripts/method"\n')
    script = script_at(root, "scripts", "data", "a.py")
    res = gate_check(script, SidecarBundle(path=None, sha256="", found=False), "collaborative")
    assert res.ok is False and res.error_payload is not None
    assert res.error_payload.error_code == GateErrorCode.INTERNAL
    assert res.error_payload.taxonomy_label == "enforcement_config_invalid"
    assert (
        "[enforcement]" in res.error_payload.resolution_hint
    )  # not the generic 'file a bug report'


def test_gate_check_still_passes_an_unconfigured_dir_with_no_sidecar(tmp_path):
    from bathos.prereg import SidecarBundle, gate_check

    root = project(tmp_path, '[enforcement]\ndirs = ["scripts/method"]\n')
    script = script_at(root, "scripts", "data", "a.py")
    bundle = SidecarBundle(path=None, sha256="", found=False)
    assert gate_check(script, bundle, "collaborative").ok is True


def _runner_project(tmp_path: Path, dirs: str | None):
    root = project(tmp_path, None if dirs is None else f"[enforcement]\ndirs = {dirs}\n")
    (root / "catalog").mkdir()
    return root


def _marker_script(root: Path, *parts: str) -> tuple[Path, Path]:
    marker = root / f"ran_{'_'.join(parts[:-1])}.txt"
    script = root.joinpath(*parts)
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n")
    return script, marker


def _run(root: Path, script: Path) -> int:
    from bathos.runner import run_script

    return run_script(
        argv=[sys.executable, str(script)],
        project_slug="proj",
        catalog_dir=root / "catalog",
        output_paths=[],
        tags=[],
        cwd=root,
    )


def test_runner_refuses_and_does_not_execute_a_configured_dir_script_with_no_sidecar(tmp_path):
    root = _runner_project(tmp_path, '["scripts/method"]')
    script, marker = _marker_script(root, "scripts", "method", "a.py")
    assert _run(root, script) == 1
    assert not marker.exists()  # the script was never launched


def test_positive_control_the_same_script_in_an_unconfigured_dir_runs(tmp_path):
    root = _runner_project(tmp_path, '["scripts/method"]')
    script, marker = _marker_script(root, "scripts", "data", "a.py")
    assert _run(root, script) == 0
    assert marker.exists()


def test_positive_control_without_the_config_the_same_method_script_ran_before_the_fix(tmp_path):
    root = _runner_project(tmp_path, None)
    script, marker = _marker_script(root, "scripts", "method", "a.py")
    assert _run(root, script) == 0  # the pre-fix fail-open behaviour, now opt-out by default only
    assert marker.exists()


def test_runner_reports_an_invalid_enforcement_block_and_does_not_execute(tmp_path, capsys):
    root = _runner_project(tmp_path, '"scripts/method"')
    script, marker = _marker_script(root, "scripts", "method", "a.py")
    assert _run(root, script) == 1
    assert not marker.exists()
    err = capsys.readouterr().err
    assert "enforcement" in err.lower() and "invalid_param" in err


def test_explicit_no_sidecar_bypass_still_runs_a_configured_dir_script_and_records_it(tmp_path):
    from bathos.catalog import read_runs
    from bathos.runner import run_script

    root = _runner_project(tmp_path, '["scripts/method"]')
    script, marker = _marker_script(root, "scripts", "method", "a.py")
    code = run_script(
        argv=[sys.executable, str(script)],
        project_slug="proj",
        catalog_dir=root / "catalog",
        output_paths=[],
        tags=[],
        cwd=root,
        no_sidecar=True,
    )
    assert code == 0 and marker.exists()
    assert (
        read_runs(root / "catalog")[0].sidecar_mode == "bypassed"
    )  # the bypass is still visible in the record


def test_explicit_no_sidecar_bypass_is_not_blocked_by_a_broken_enforcement_block(tmp_path):
    from bathos.runner import run_script

    root = _runner_project(tmp_path, '"scripts/method"')
    script, marker = _marker_script(root, "scripts", "method", "a.py")
    code = run_script(
        argv=[sys.executable, str(script)],
        project_slug="proj",
        catalog_dir=root / "catalog",
        output_paths=[],
        tags=[],
        cwd=root,
        no_sidecar=True,
    )
    assert code == 0 and marker.exists()


def test_runner_lets_a_configured_dir_script_with_a_valid_sidecar_through(tmp_path):
    root = _runner_project(tmp_path, '["scripts/method"]')
    script, marker = _marker_script(root, "scripts", "method", "a.py")
    (script.parent / "a.bth.toml").write_text(
        textwrap.dedent("""
        [experiment]
        hypothesis = "test hypothesis"
        [outcomes.pass]
        condition = "x == 1"
        decision = "proceed"
        reasoning = "expected behavior"
        [outcomes.fallback]
        condition = "1==1"
        decision = "other"
        reasoning = "catch-all"
        is_residual = true
        [result_schema]
        x = "float"
        """)
    )
    assert _run(root, script) == 0
    assert marker.exists()
