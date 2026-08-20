"""Tests for Signal 14 (unarchived_stale_scripts_count), sprint_audit.py."""

from pathlib import Path


def test_signal_ok_when_no_stale_scripts(tmp_path):
    from bathos.sprint_audit import signal_unarchived_stale_scripts

    scripts_dir = tmp_path / "scripts" / "experiments"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "run_thing.py").write_text("print('hello')\n")
    (scripts_dir / "run_thing.bth.toml").write_text("[experiment]\nhypothesis = 'h'\n")

    result = signal_unarchived_stale_scripts(tmp_path, tmp_path / "catalog")
    assert result.value == 0.0
    assert result.level == "OK"


def test_signal_warning_when_stale_script_unarchived(tmp_path):
    from bathos.sprint_audit import signal_unarchived_stale_scripts

    scripts_dir = tmp_path / "scripts" / "experiments"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "run_thing.py").write_text("print('hello')\n")
    (scripts_dir / "run_thing.bth.toml").write_text(
        "[experiment]\nhypothesis = 'h'\n[status]\nstale = true\nstale_reason = 'bad'\n"
    )

    result = signal_unarchived_stale_scripts(tmp_path, tmp_path / "catalog")
    assert result.value == 1.0
    assert result.level == "WARNING"
    assert "unarchived_stale_scripts_count=1" in result.message


def test_signal_wired_into_sprint_audit_signals_dict(tmp_path, monkeypatch):
    """unarchived_stale_scripts_count appears in sprint_audit()'s per-project signals dict
    without crashing a project that has never archived anything."""
    from bathos.compact import compact
    from bathos.sprint_audit import sprint_audit

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    compact(catalog_dir)  # creates an empty warm DB with the expected schema

    # sprint_audit() does `from bathos.config import list_registered_projects` as a lazy
    # import inside the function body, so the patch target is bathos.config's own name,
    # not a (nonexistent) module-level reference on bathos.sprint_audit.
    monkeypatch.setattr(
        "bathos.config.list_registered_projects",
        lambda: [{"slug": "testproj", "catalog_dir": str(catalog_dir), "root": str(tmp_path)}],
    )

    result = sprint_audit(hours=24)
    assert "testproj" in result["audit_results"]
    assert "unarchived_stale_scripts_count" in result["audit_results"]["testproj"]["signals"]
