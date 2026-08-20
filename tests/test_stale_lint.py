"""Tests for the Tier-2 stale-without-reason lint check (check_stale_scripts_without_reason).

A [status] stale=true flag with no stale_reason is as unjustified as a numeric threshold
with no source (see test_threshold_lint.py) -- it also degrades the pre-registration gate's
deny message (prereg.gate_check) into something un-actionable for whoever hits it.
"""

from pathlib import Path


def _make_sidecar_toml(path: Path, content: str) -> Path:
    path.write_text(content)
    return path


def test_check_stale_without_reason_warns(tmp_path):
    from bathos.linter import IssueSeverity, check_stale_scripts_without_reason

    toml_content = """
[experiment]
hypothesis = "NVT temperature stability"

[outcomes.pass]
condition = "temp_std < 5.0"
decision = "proceed"
reasoning = "good enough"
is_residual = false

[result_schema]
temp_std = "float"

[status]
stale = true
"""
    _make_sidecar_toml(tmp_path / "run_nvt.bth.toml", toml_content)
    issues = check_stale_scripts_without_reason(tmp_path)
    stale_issues = [i for i in issues if i.issue == "stale_without_reason"]
    assert len(stale_issues) == 1
    assert stale_issues[0].severity == IssueSeverity.WARNING


def test_check_stale_with_reason_no_warning(tmp_path):
    from bathos.linter import check_stale_scripts_without_reason

    toml_content = """
[experiment]
hypothesis = "NVT temperature stability"

[outcomes.pass]
condition = "temp_std < 5.0"
decision = "proceed"
reasoning = "good enough"
is_residual = false

[result_schema]
temp_std = "float"

[status]
stale = true
stale_reason = "known-wrong default input"
"""
    _make_sidecar_toml(tmp_path / "run_nvt.bth.toml", toml_content)
    issues = check_stale_scripts_without_reason(tmp_path)
    assert [i for i in issues if i.issue == "stale_without_reason"] == []


def test_check_stale_not_flagged_absent(tmp_path):
    from bathos.linter import check_stale_scripts_without_reason

    toml_content = """
[experiment]
hypothesis = "NVT temperature stability"

[outcomes.pass]
condition = "temp_std < 5.0"
decision = "proceed"
reasoning = "good enough"
is_residual = false

[result_schema]
temp_std = "float"
"""
    _make_sidecar_toml(tmp_path / "run_nvt.bth.toml", toml_content)
    issues = check_stale_scripts_without_reason(tmp_path)
    assert [i for i in issues if i.issue == "stale_without_reason"] == []


def test_check_stale_false_not_flagged(tmp_path):
    from bathos.linter import check_stale_scripts_without_reason

    toml_content = """
[experiment]
hypothesis = "NVT temperature stability"

[outcomes.pass]
condition = "temp_std < 5.0"
decision = "proceed"
reasoning = "good enough"
is_residual = false

[result_schema]
temp_std = "float"

[status]
stale = false
"""
    _make_sidecar_toml(tmp_path / "run_nvt.bth.toml", toml_content)
    issues = check_stale_scripts_without_reason(tmp_path)
    assert [i for i in issues if i.issue == "stale_without_reason"] == []
