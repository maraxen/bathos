"""Tests for Tier-2 threshold epistemic hygiene lint check (#760).

Tests validate that check_threshold_basis warns when numeric thresholds lack
documented justification via source (outcomes) or regression_threshold_basis (benchmarks).
"""

import pytest
from pathlib import Path


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _make_sidecar_toml(path: Path, content: str) -> Path:
    path.write_text(content)
    return path


# ---------------------------------------------------------------------------
# TC-1: Bare numeric literal in condition, no source → WARNING
# ---------------------------------------------------------------------------

def test_check_threshold_basis_bare_numeric_warns(tmp_path):
    from bathos.linter import check_threshold_basis, IssueSeverity

    toml_content = """
[experiment]
hypothesis = "NVT temperature stability"

[outcomes.pass]
condition = "temp_std < 5.0"
decision = "proceed"
reasoning = "good enough"
is_residual = false

[outcomes.fail]
condition = "temp_std >= 5.0"
decision = "debug"
reasoning = "too noisy"
is_residual = true

[result_schema]
temp_std = "float"
"""
    _make_sidecar_toml(tmp_path / "run_nvt.bth.toml", toml_content)
    issues = check_threshold_basis(tmp_path)
    threshold_issues = [i for i in issues if i.issue == "unjustified_threshold"]
    assert len(threshold_issues) >= 1
    assert all(i.severity == IssueSeverity.WARNING for i in threshold_issues)


# ---------------------------------------------------------------------------
# TC-2: Numeric literal in condition WITH source → no warning
# ---------------------------------------------------------------------------

def test_check_threshold_basis_source_suppresses_warning(tmp_path):
    from bathos.linter import check_threshold_basis

    toml_content = """
[experiment]
hypothesis = "NVT temperature stability"

[outcomes.pass]
condition = "temp_std < 5.0"
decision = "proceed"
reasoning = "good enough"
source = "NVT standard: ±5K from Frenkel & Smit, §4.2"
is_residual = false

[outcomes.fail]
condition = "temp_std >= 5.0"
decision = "debug"
reasoning = "too noisy"
source = "complement of pass condition"
is_residual = true

[result_schema]
temp_std = "float"
"""
    _make_sidecar_toml(tmp_path / "run_nvt.bth.toml", toml_content)
    issues = check_threshold_basis(tmp_path)
    threshold_issues = [i for i in issues if i.issue == "unjustified_threshold"]
    assert threshold_issues == []


# ---------------------------------------------------------------------------
# TC-2b: 1=1 residual pattern fires; must suppress with source or use "true"
# ---------------------------------------------------------------------------

def test_check_threshold_basis_one_equals_one_residual_warns(tmp_path):
    """Test accept-by-design: condition = '1=1' is a catch-all residual pattern.
    The regex intentionally matches the '1' in '1=1'. Users must either use
    condition = 'true' (scaffold default) or add source = 'trivial residual'."""
    from bathos.linter import check_threshold_basis, IssueSeverity

    toml_content = """
[experiment]
hypothesis = "test 1=1 pattern"

[outcomes.pass]
condition = "success = true"
decision = "proceed"
reasoning = "succeeded"
is_residual = false

[outcomes.fail]
condition = "1=1"
decision = "fallback"
reasoning = "catch-all"
is_residual = true

[result_schema]
success = "bool"
"""
    _make_sidecar_toml(tmp_path / "run_catchall.bth.toml", toml_content)
    issues = check_threshold_basis(tmp_path)
    threshold_issues = [i for i in issues if i.issue == "unjustified_threshold"]
    # Should warn because '1' matches numeric literal regex
    assert len(threshold_issues) >= 1
    assert all(i.severity == IssueSeverity.WARNING for i in threshold_issues)


def test_check_threshold_basis_one_equals_one_with_source_suppressed(tmp_path):
    """Users can suppress '1=1' warning by adding source field."""
    from bathos.linter import check_threshold_basis

    toml_content = """
[experiment]
hypothesis = "test 1=1 pattern with source"

[outcomes.pass]
condition = "success = true"
decision = "proceed"
reasoning = "succeeded"
is_residual = false

[outcomes.fail]
condition = "1=1"
decision = "fallback"
reasoning = "catch-all"
source = "trivial residual"
is_residual = true

[result_schema]
success = "bool"
"""
    _make_sidecar_toml(tmp_path / "run_catchall.bth.toml", toml_content)
    issues = check_threshold_basis(tmp_path)
    threshold_issues = [i for i in issues if i.issue == "unjustified_threshold"]
    assert threshold_issues == []


# ---------------------------------------------------------------------------
# TC-3: numeric in condition fires regardless of adversarial_check field
# ---------------------------------------------------------------------------

def test_numeric_in_condition_warns_regardless_of_adversarial_check_field(tmp_path):
    """Test that the condition field is scanned for numeric literals even when
    adversarial_check is present. The numeric literal in condition (0.90) should
    trigger a warning because there is no source field."""
    from bathos.linter import check_threshold_basis, IssueSeverity

    toml_content = """
[experiment]
hypothesis = "accuracy above baseline"

[outcomes.pass]
condition = "accuracy > 0.90"
decision = "deploy"
reasoning = "good"
adversarial_check = "accuracy < 0.50"
is_residual = false

[outcomes.fail]
condition = "accuracy <= 0.90"
decision = "retrain"
reasoning = "too low"
is_residual = true

[result_schema]
accuracy = "float"
"""
    _make_sidecar_toml(tmp_path / "run_clf.bth.toml", toml_content)
    issues = check_threshold_basis(tmp_path)
    threshold_issues = [i for i in issues if i.issue == "unjustified_threshold"]
    # The pass outcome condition contains 0.90, which is numeric and has no source
    assert len(threshold_issues) >= 1
    assert all(i.severity == IssueSeverity.WARNING for i in threshold_issues)


# ---------------------------------------------------------------------------
# TC-4a: Non-numeric condition (boolean) → no warning
# ---------------------------------------------------------------------------

def test_check_threshold_basis_boolean_condition_no_warning(tmp_path):
    from bathos.linter import check_threshold_basis

    toml_content = """
[experiment]
hypothesis = "reproduction check"

[outcomes.pass]
condition = "reproduced = TRUE"
decision = "proceed"
reasoning = "boolean check"
is_residual = false

[outcomes.fail]
condition = "reproduced = FALSE"
decision = "debug"
reasoning = "did not reproduce"
is_residual = true

[result_schema]
reproduced = "bool"
"""
    _make_sidecar_toml(tmp_path / "run_repro.bth.toml", toml_content)
    issues = check_threshold_basis(tmp_path)
    threshold_issues = [i for i in issues if i.issue == "unjustified_threshold"]
    assert threshold_issues == []


# ---------------------------------------------------------------------------
# TC-4b: Non-numeric condition (NULL check) → no warning
# ---------------------------------------------------------------------------

def test_check_threshold_basis_null_check_no_warning(tmp_path):
    from bathos.linter import check_threshold_basis

    toml_content = """
[experiment]
hypothesis = "output file produced"

[outcomes.pass]
condition = "output_path IS NOT NULL"
decision = "proceed"
reasoning = "file was written"
is_residual = false

[outcomes.fail]
condition = "output_path IS NULL"
decision = "debug"
reasoning = "file missing"
is_residual = true

[result_schema]
output_path = "str"
"""
    _make_sidecar_toml(tmp_path / "run_file.bth.toml", toml_content)
    issues = check_threshold_basis(tmp_path)
    threshold_issues = [i for i in issues if i.issue == "unjustified_threshold"]
    assert threshold_issues == []


# ---------------------------------------------------------------------------
# TC-5: Benchmark regression_threshold without basis → WARNING
# ---------------------------------------------------------------------------

def test_check_threshold_basis_benchmark_threshold_warns(tmp_path):
    from bathos.linter import check_threshold_basis, IssueSeverity

    toml_content = """
[benchmark]
baseline_ref = "run_abc123"
metric = "ns_per_day"
regression_threshold = 0.05
target = "> 50 ns/day"

[result_schema]
ns_per_day = "float"
"""
    _make_sidecar_toml(tmp_path / "bench_perf.bth.toml", toml_content)
    issues = check_threshold_basis(tmp_path)
    threshold_issues = [i for i in issues if i.issue == "unjustified_threshold"]
    assert len(threshold_issues) == 1
    assert threshold_issues[0].severity == IssueSeverity.WARNING
    assert "regression_threshold" in threshold_issues[0].detail


# ---------------------------------------------------------------------------
# TC-6: Benchmark regression_threshold WITH basis → no warning
# ---------------------------------------------------------------------------

def test_check_threshold_basis_benchmark_with_basis_suppresses_warning(tmp_path):
    from bathos.linter import check_threshold_basis

    toml_content = """
[benchmark]
baseline_ref = "run_abc123"
metric = "ns_per_day"
regression_threshold = 0.05
regression_threshold_basis = "5% is standard GROMACS regression gate (internal policy)"
target = "> 50 ns/day"

[result_schema]
ns_per_day = "float"
"""
    _make_sidecar_toml(tmp_path / "bench_perf.bth.toml", toml_content)
    issues = check_threshold_basis(tmp_path)
    threshold_issues = [i for i in issues if i.issue == "unjustified_threshold"]
    assert threshold_issues == []


# ---------------------------------------------------------------------------
# TC-7: Benchmark regression_threshold = 0.0 (default/unset) → no warning
# ---------------------------------------------------------------------------

def test_check_threshold_basis_benchmark_zero_threshold_no_warning(tmp_path):
    """regression_threshold = 0.0 is the dataclass default and means 'not set'.
    It must not trigger a warning even without regression_threshold_basis."""
    from bathos.linter import check_threshold_basis

    toml_content = """
[benchmark]
baseline_ref = "run_abc123"
metric = "ns_per_day"
regression_threshold = 0.0
target = "> 50 ns/day"

[result_schema]
ns_per_day = "float"
"""
    _make_sidecar_toml(tmp_path / "bench_perf.bth.toml", toml_content)
    issues = check_threshold_basis(tmp_path)
    threshold_issues = [i for i in issues if i.issue == "unjustified_threshold"]
    assert threshold_issues == []


# ---------------------------------------------------------------------------
# TC-8: Unparseable TOML → silent skip, no crash
# ---------------------------------------------------------------------------

def test_check_threshold_basis_invalid_toml_skips(tmp_path):
    from bathos.linter import check_threshold_basis

    bad = tmp_path / "broken.bth.toml"
    bad.write_text("[experiment\nhypothesis = broken toml")
    # Must not raise; broken file must be silently skipped
    issues = check_threshold_basis(tmp_path)
    assert all("broken" not in str(i.path) for i in issues)


# ---------------------------------------------------------------------------
# TC-9: Multiple sidecars, mixed — only bare-numeric ones produce warnings
# ---------------------------------------------------------------------------

def test_check_threshold_basis_multiple_sidecars_mixed(tmp_path):
    from bathos.linter import check_threshold_basis

    good_toml = """
[experiment]
hypothesis = "good"
[outcomes.pass]
condition = "temp_std < 5.0"
decision = "proceed"
reasoning = "fine"
source = "domain knowledge"
is_residual = false
[outcomes.fail]
condition = "temp_std >= 5.0"
decision = "fix"
reasoning = "noisy"
source = "complement"
is_residual = true
[result_schema]
temp_std = "float"
"""
    bad_toml = """
[experiment]
hypothesis = "bad"
[outcomes.pass]
condition = "accuracy > 0.95"
decision = "deploy"
reasoning = "good enough"
is_residual = false
[outcomes.fail]
condition = "accuracy <= 0.95"
decision = "retrain"
reasoning = "too low"
is_residual = true
[result_schema]
accuracy = "float"
"""
    _make_sidecar_toml(tmp_path / "run_good.bth.toml", good_toml)
    _make_sidecar_toml(tmp_path / "run_bad.bth.toml", bad_toml)

    issues = check_threshold_basis(tmp_path)
    threshold_issues = [i for i in issues if i.issue == "unjustified_threshold"]

    assert len(threshold_issues) >= 1
    paths_warned = {str(i.path) for i in threshold_issues}
    assert any("run_bad" in p for p in paths_warned)
    assert not any("run_good" in p for p in paths_warned)


# ---------------------------------------------------------------------------
# TC-10: Integer literal fires (count > 0 intentional per spec)
# ---------------------------------------------------------------------------

def test_check_threshold_basis_integer_literal_fires(tmp_path):
    """Conservative regex intentionally fires on small integers like 'count > 0'.
    The researcher must add source = 'zero is the natural lower bound' to suppress."""
    from bathos.linter import check_threshold_basis, IssueSeverity

    toml_content = """
[experiment]
hypothesis = "at least one result produced"

[outcomes.pass]
condition = "count > 0"
decision = "proceed"
reasoning = "something ran"
is_residual = false

[outcomes.fail]
condition = "count = 0"
decision = "debug"
reasoning = "nothing ran"
is_residual = true

[result_schema]
count = "int"
"""
    _make_sidecar_toml(tmp_path / "run_count.bth.toml", toml_content)
    issues = check_threshold_basis(tmp_path)
    threshold_issues = [i for i in issues if i.issue == "unjustified_threshold"]
    assert len(threshold_issues) >= 1
    assert all(i.severity == IssueSeverity.WARNING for i in threshold_issues)


# ---------------------------------------------------------------------------
# TC-11: Integer with source suppresses integer literal warning
# ---------------------------------------------------------------------------

def test_check_threshold_basis_integer_with_source_suppressed(tmp_path):
    """Researcher explicitly justifies count > 0 via source field."""
    from bathos.linter import check_threshold_basis

    toml_content = """
[experiment]
hypothesis = "at least one result produced"

[outcomes.pass]
condition = "count > 0"
decision = "proceed"
reasoning = "something ran"
source = "zero is the natural lower bound — no output means nothing ran"
is_residual = false

[outcomes.fail]
condition = "count = 0"
decision = "debug"
reasoning = "nothing ran"
source = "complement of pass"
is_residual = true

[result_schema]
count = "int"
"""
    _make_sidecar_toml(tmp_path / "run_count.bth.toml", toml_content)
    issues = check_threshold_basis(tmp_path)
    threshold_issues = [i for i in issues if i.issue == "unjustified_threshold"]
    assert threshold_issues == []


# ---------------------------------------------------------------------------
# TC-12: Scientific notation literal fires
# ---------------------------------------------------------------------------

def test_check_threshold_basis_scientific_notation_fires(tmp_path):
    """Scientific notation (e.g. 1e-3) must be caught by the regex."""
    from bathos.linter import check_threshold_basis, IssueSeverity

    toml_content = """
[experiment]
hypothesis = "small error"

[outcomes.pass]
condition = "err < 1e-3"
decision = "proceed"
reasoning = "tiny error"
is_residual = false

[outcomes.fail]
condition = "err >= 1e-3"
decision = "debug"
reasoning = "too big"
is_residual = true

[result_schema]
err = "float"
"""
    _make_sidecar_toml(tmp_path / "run_err.bth.toml", toml_content)
    issues = check_threshold_basis(tmp_path)
    threshold_issues = [i for i in issues if i.issue == "unjustified_threshold"]
    assert len(threshold_issues) >= 1
    assert all(i.severity == IssueSeverity.WARNING for i in threshold_issues)


# ---------------------------------------------------------------------------
# TC-13: Existing Tier-2 check_adversarial_checks still passes (regression guard)
# ---------------------------------------------------------------------------

def test_existing_tier2_check_adversarial_checks_still_passes(tmp_path):
    """Ensure check_adversarial_checks continues to work after the new check is wired in.
    A sidecar with outcomes.pass missing adversarial_check must still produce
    a missing_adversarial_check WARNING — unaffected by the threshold check."""
    from bathos.linter import check_adversarial_checks, IssueSeverity

    toml_content = """
[experiment]
hypothesis = "adversarial coverage"

[outcomes.pass]
condition = "accuracy > 0.9"
decision = "proceed"
reasoning = "good"
is_residual = false

[outcomes.fail]
condition = "accuracy <= 0.9"
decision = "debug"
reasoning = "bad"
is_residual = true

[result_schema]
accuracy = "float"
"""
    _make_sidecar_toml(tmp_path / "run_adv.bth.toml", toml_content)
    issues = check_adversarial_checks(tmp_path)
    adv_issues = [i for i in issues if i.issue == "missing_adversarial_check"]
    assert len(adv_issues) == 1
    assert adv_issues[0].severity == IssueSeverity.WARNING


# ---------------------------------------------------------------------------
# TC-14: CLI bth lint surfaces unjustified_threshold warnings
# ---------------------------------------------------------------------------

def test_cli_lint_threshold_warning_appears(tmp_path, monkeypatch):
    from bathos.cli import app
    from typer.testing import CliRunner

    monkeypatch.chdir(tmp_path)
    catalog_dir = tmp_path / ".bth" / "catalog"
    catalog_dir.mkdir(parents=True)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

    toml_content = """
[experiment]
hypothesis = "test threshold lint integration"
[outcomes.pass]
condition = "err < 0.01"
decision = "proceed"
reasoning = "good"
is_residual = false
[outcomes.fail]
condition = "err >= 0.01"
decision = "fix"
reasoning = "bad"
is_residual = true
[result_schema]
err = "float"
"""
    (tmp_path / "run_test.bth.toml").write_text(toml_content)

    cli_runner = CliRunner()
    result = cli_runner.invoke(app, ["lint", "--project-root", str(tmp_path)])
    assert "unjustified_threshold" in result.output
    assert "warning" in result.output.lower()
