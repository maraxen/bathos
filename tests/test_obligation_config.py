"""`[obligations]` in .bth.toml, and how it resolves against the env vars.

The config file is the durable home for these flags: a SLURM job reads the same `.bth.toml`,
whereas a shell-only export is honoured locally and silently skipped on the cluster — which
would produce a ledger where identical work does or does not open obligations depending on
where it ran. The env var remains the per-invocation override.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bathos.obligations import TRIGGERS, enforcement_enabled, list_obligations, trigger_enabled


@pytest.fixture(autouse=True)
def _no_env(monkeypatch):
    """Every test starts from a clean environment so config is what's under test."""
    for trig in TRIGGERS:
        monkeypatch.delenv(f"BTH_OBLIGATION_{trig.upper()}", raising=False)
    monkeypatch.delenv("BTH_OBLIGATION_ENFORCE", raising=False)


def _project(tmp_path: Path, body: str) -> Path:
    (tmp_path / ".bth.toml").write_text(
        f'[project]\nslug = "p"\nroot = "{tmp_path}"\n\n{body}', encoding="utf-8"
    )
    return tmp_path


def test_config_enables_a_trigger(tmp_path):
    root = _project(tmp_path, "[obligations]\ncitation_contradicted = true\n")
    assert trigger_enabled("citation_contradicted", root) is True


def test_config_only_enables_what_it_names(tmp_path):
    root = _project(tmp_path, "[obligations]\ncitation_contradicted = true\n")
    assert trigger_enabled("outcome_failed", root) is False
    assert trigger_enabled("adversarial_check_fired", root) is False


def test_explicit_false_in_config_stays_off(tmp_path):
    root = _project(tmp_path, "[obligations]\noutcome_failed = false\n")
    assert trigger_enabled("outcome_failed", root) is False


def test_no_obligations_section_is_all_off(tmp_path):
    root = _project(tmp_path, "")
    assert [t for t in TRIGGERS if trigger_enabled(t, root)] == []


def test_no_project_config_at_all_is_all_off(tmp_path):
    assert [t for t in TRIGGERS if trigger_enabled(t, tmp_path)] == []


def test_enforce_reads_from_config(tmp_path):
    assert enforcement_enabled(_project(tmp_path, "[obligations]\nenforce = true\n")) is True


def test_enforce_defaults_off(tmp_path):
    assert enforcement_enabled(_project(tmp_path, "[obligations]\n")) is False


def test_a_subdirectory_inherits_the_projects_config(tmp_path):
    """find_project_config walks parents, so a script run from scripts/experiments/ resolves
    the same flags as one run from the project root."""
    root = _project(tmp_path, "[obligations]\ncitation_contradicted = true\n")
    nested = root / "scripts" / "experiments"
    nested.mkdir(parents=True)
    assert trigger_enabled("citation_contradicted", nested) is True


# ── env override, both directions ──────────────────────────────────────────


def test_env_can_enable_what_config_omits(tmp_path, monkeypatch):
    root = _project(tmp_path, "[obligations]\n")
    monkeypatch.setenv("BTH_OBLIGATION_OUTCOME_FAILED", "1")
    assert trigger_enabled("outcome_failed", root) is True


def test_env_can_disable_what_config_enables(tmp_path, monkeypatch):
    """The load-bearing half. If a set-but-false env var fell through to config, `=0` would
    silently still be enabled — the opposite of what an override is for."""
    root = _project(tmp_path, "[obligations]\ncitation_contradicted = true\n")
    monkeypatch.setenv("BTH_OBLIGATION_CITATION_CONTRADICTED", "0")
    assert trigger_enabled("citation_contradicted", root) is False


@pytest.mark.parametrize("val", ["0", "false", "no", "FALSE"])
def test_falsey_env_values_disable(tmp_path, monkeypatch, val):
    root = _project(tmp_path, "[obligations]\ncitation_contradicted = true\n")
    monkeypatch.setenv("BTH_OBLIGATION_CITATION_CONTRADICTED", val)
    assert trigger_enabled("citation_contradicted", root) is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "TRUE"])
def test_truthy_env_values_enable(tmp_path, monkeypatch, val):
    root = _project(tmp_path, "[obligations]\n")
    monkeypatch.setenv("BTH_OBLIGATION_CITATION_CONTRADICTED", val)
    assert trigger_enabled("citation_contradicted", root) is True


def test_an_unrecognised_env_value_falls_through_to_config(tmp_path, monkeypatch):
    """Garbage must not be read as an intentional 'off' that overrides a deliberate config."""
    root = _project(tmp_path, "[obligations]\ncitation_contradicted = true\n")
    monkeypatch.setenv("BTH_OBLIGATION_CITATION_CONTRADICTED", "maybe")
    assert trigger_enabled("citation_contradicted", root) is True


def test_enforce_env_overrides_config(tmp_path, monkeypatch):
    root = _project(tmp_path, "[obligations]\nenforce = true\n")
    monkeypatch.setenv("BTH_OBLIGATION_ENFORCE", "0")
    assert enforcement_enabled(root) is False


# ── robustness ─────────────────────────────────────────────────────────────


def test_a_malformed_bth_toml_does_not_crash(tmp_path):
    """An unreadable config means 'not enabled' — the safe direction for a flag that writes
    ledger entries — rather than an exception in the middle of a completed run."""
    (tmp_path / ".bth.toml").write_text("[project\nslug = broken", encoding="utf-8")
    assert trigger_enabled("citation_contradicted", tmp_path) is False
    assert enforcement_enabled(tmp_path) is False


def test_unknown_trigger_is_never_enabled_by_config(tmp_path):
    root = _project(tmp_path, "[obligations]\nnonsense = true\n")
    assert trigger_enabled("nonsense", root) is False


def test_maybe_open_honours_config(tmp_path):
    from bathos.obligations import maybe_open

    root = _project(tmp_path, "[obligations]\ncitation_contradicted = true\n")
    assert maybe_open(root, "run", "r1", "outcome_failed") is None
    assert maybe_open(root, "run", "r1", "citation_contradicted") is not None
    assert [o.trigger for o in list_obligations(root)] == ["citation_contradicted"]


# ── this repo's own committed configuration ────────────────────────────────


def test_this_projects_bth_toml_enables_exactly_the_two_inert_triggers():
    """Pins the deliberate choice: the two triggers that cannot fire against pre-existing
    work are armed; the wider ones and `enforce` wait for observed data."""
    import tomllib

    import bathos

    repo_root = Path(bathos.__file__).resolve().parents[2]
    cfg = tomllib.loads((repo_root / ".bth.toml").read_text())
    obligations = cfg.get("obligations", {})

    assert obligations.get("citation_contradicted") is True
    assert obligations.get("adversarial_check_fired") is True
    assert obligations.get("outcome_failed") is False
    assert obligations.get("campaign_confounded") is False
    assert obligations.get("enforce") is False
