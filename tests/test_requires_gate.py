"""Enforcement of [requires]/[budget] (spec 261016 B2): check_requires and gate_check.

Each behaviour is its own test. Git-backed tests use a real throwaway repo so HEAD resolves.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import date
from pathlib import Path

import pytest

from bathos import requires_gate
from bathos.config import EnforcementConfigError, load_requires_enforce_after
from bathos.prereg import GateErrorCode, gate_check, resolve_sidecar
from bathos.requires_gate import check_requires
from bathos.sidecar import parse_sidecar

TODAY = date(2026, 10, 17)
PAST_CUTOFF = "2026-10-01"
FUTURE_CUTOFF = "2026-11-01"

BASE_OUTCOMES = """
[outcomes.pass]
condition = "value > 0"
decision = "proceed"
reasoning = "r"
source = "provisional:fixture"

[outcomes.fallback]
condition = "TRUE"
decision = "review"
reasoning = "r"
is_residual = true
source = "provisional:fixture"

[result_schema]
value = "float"
"""


def _git_repo(root: Path) -> str:
    """A git repo with one commit at ``root``; returns the HEAD sha."""
    root.mkdir(parents=True, exist_ok=True)
    git = ["git", "-C", str(root)]
    subprocess.run([*git, "init", "-q"], check=True, capture_output=True)
    (root / "README").write_text("x\n")
    subprocess.run([*git, "add", "README"], check=True, capture_output=True)
    subprocess.run(
        [*git, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", "init"],
        check=True,
        capture_output=True,
    )
    out = subprocess.run([*git, "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _gate_config(root: Path, cutoff_toml: str | None) -> None:
    """Write the project .bth.toml; ``cutoff_toml`` is the raw value of requires_enforce_after, or None."""
    body = '[project]\nslug = "proj"\nroot = "."\n'
    if cutoff_toml is not None:
        body += f"\n[gate]\nrequires_enforce_after = {cutoff_toml}\n"
    (root / ".bth.toml").write_text(body)


def _sidecar(root: Path, stage: str = "validation", extra: str = "") -> Path:
    """An experiment sidecar at scripts/experiments/run_nvt.bth.toml under ``root``."""
    d = root / "scripts" / "experiments"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "run_nvt.bth.toml"
    p.write_text(
        f'[experiment]\nhypothesis = "h"\nstage_name = "{stage}"\n' + extra + BASE_OUTCOMES
    )
    return p


def _check(sidecar_path: Path, *, script_path: Path | None = None, today: date = TODAY):
    sidecar = parse_sidecar(sidecar_path)
    return check_requires(
        sidecar, sidecar_path, script_path=script_path, project_root=None, today=today
    )


@pytest.fixture
def events(monkeypatch):
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(requires_gate, "event", lambda name, **fields: seen.append((name, fields)))
    return seen


# ── positive control and sha matching ────────────────────────────────────────


def test_positive_control_passes_at_validation(tmp_path):
    head = _git_repo(tmp_path)
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    (tmp_path / "scripts/experiments").mkdir(parents=True)
    (tmp_path / "scripts/experiments/out.json").write_text(json.dumps({"git_sha": head}))
    sc = _sidecar(
        tmp_path,
        extra='[requires]\nartifact = "out.json"\njson_key = "git_sha"\nmust_equal = "HEAD"\n',
    )
    outcome = _check(sc)
    assert outcome.status == "pass", outcome
    assert outcome.error_code is None
    assert outcome.errors == []


def test_short_sha_prefix_passes(tmp_path):
    head = _git_repo(tmp_path)
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    (tmp_path / "scripts/experiments").mkdir(parents=True)
    (tmp_path / "scripts/experiments/out.json").write_text(json.dumps({"git_sha": head[:8]}))
    sc = _sidecar(
        tmp_path,
        extra='[requires]\nartifact = "out.json"\njson_key = "git_sha"\nmust_equal = "HEAD"\n',
    )
    assert _check(sc).status == "pass"


def test_non_hex_mismatch_blocks(tmp_path):
    _git_repo(tmp_path)
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    (tmp_path / "scripts/experiments").mkdir(parents=True)
    (tmp_path / "scripts/experiments/out.json").write_text(json.dumps({"git_sha": "not-a-sha"}))
    sc = _sidecar(
        tmp_path,
        extra='[requires]\nartifact = "out.json"\njson_key = "git_sha"\nmust_equal = "HEAD"\n',
    )
    outcome = _check(sc)
    assert outcome.status == "block"
    assert outcome.error_code == GateErrorCode.REQUIRES_UNMET
    assert any("expected HEAD" in e for e in outcome.errors)


# ── reject_suffixes and severity ─────────────────────────────────────────────


def _dirty_setup(tmp_path: Path, stage: str, cutoff_toml: str | None):
    head = _git_repo(tmp_path)
    _gate_config(tmp_path, cutoff_toml)
    (tmp_path / "scripts/experiments").mkdir(parents=True)
    (tmp_path / "scripts/experiments/out.json").write_text(json.dumps({"git_sha": head + "-dirty"}))
    return _sidecar(
        tmp_path,
        stage=stage,
        extra='[requires]\nartifact = "out.json"\njson_key = "git_sha"\nmust_equal = "HEAD"\n',
    )


def test_dirty_suffix_blocks_at_validation_when_enforced(tmp_path):
    sc = _dirty_setup(tmp_path, "validation", f'"{PAST_CUTOFF}"')
    outcome = _check(sc)
    assert outcome.status == "block"
    assert outcome.error_code == GateErrorCode.REQUIRES_UNMET
    assert any("ends with -dirty" in e for e in outcome.errors)


def test_dirty_suffix_warns_at_exploration_even_when_enforced(tmp_path):
    sc = _dirty_setup(tmp_path, "exploration", f'"{PAST_CUTOFF}"')
    outcome = _check(sc)
    assert outcome.status == "warn"
    assert outcome.error_code == GateErrorCode.REQUIRES_UNMET


def test_dirty_suffix_warns_at_validation_when_cutoff_unset(tmp_path):
    sc = _dirty_setup(tmp_path, "validation", None)
    outcome = _check(sc)
    assert outcome.status == "warn"


def test_dirty_suffix_warns_at_validation_before_cutoff(tmp_path):
    sc = _dirty_setup(tmp_path, "validation", f'"{FUTURE_CUTOFF}"')
    outcome = _check(sc)
    assert outcome.status == "warn"


def test_cutoff_as_toml_date_is_accepted(tmp_path):
    sc = _dirty_setup(tmp_path, "validation", "2026-10-01")
    assert _check(sc).status == "block"


# ── missing / unreadable artifacts ───────────────────────────────────────────


def test_missing_artifact_glob_blocks(tmp_path):
    _git_repo(tmp_path)
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    sc = _sidecar(tmp_path, extra='[requires]\nartifact = "nothing_*.json"\n')
    outcome = _check(sc)
    assert outcome.status == "block"
    assert outcome.error_code == GateErrorCode.REQUIRES_UNMET
    assert "no file matches artifact glob nothing_*.json" in outcome.errors[0]


def test_bad_json_blocks(tmp_path):
    _git_repo(tmp_path)
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    (tmp_path / "scripts/experiments").mkdir(parents=True)
    (tmp_path / "scripts/experiments/out.json").write_text("{not json")
    sc = _sidecar(tmp_path, extra='[requires]\nartifact = "out.json"\njson_key = "git_sha"\n')
    outcome = _check(sc)
    assert outcome.status == "block"
    assert "cannot read out.json as JSON" in outcome.errors[0]


def test_missing_json_key_blocks(tmp_path):
    _git_repo(tmp_path)
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    (tmp_path / "scripts/experiments").mkdir(parents=True)
    (tmp_path / "scripts/experiments/out.json").write_text(json.dumps({"other": "x"}))
    sc = _sidecar(tmp_path, extra='[requires]\nartifact = "out.json"\njson_key = "git_sha"\n')
    outcome = _check(sc)
    assert outcome.status == "block"
    assert "has no key git_sha" in outcome.errors[0]


def test_requires_without_artifact_blocks(tmp_path):
    _git_repo(tmp_path)
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    sc = _sidecar(tmp_path, extra='[requires]\nworkload = "jax"\n')
    outcome = _check(sc)
    assert outcome.status == "block"
    assert outcome.errors == ["[requires] declares no artifact"]


def test_multiple_matches_uses_newest_and_names_it_on_failure(tmp_path):
    _git_repo(tmp_path)
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    d = tmp_path / "scripts/experiments"
    d.mkdir(parents=True)
    old = d / "out_a.json"
    new = d / "out_b.json"
    old.write_text(json.dumps({"git_sha": "x" * 3}))
    new.write_text(json.dumps({"git_sha": "y" * 3}))
    os.utime(old, (1_000_000_000, 1_000_000_000))
    os.utime(new, (2_000_000_000, 2_000_000_000))
    sc = _sidecar(
        tmp_path,
        extra='[requires]\nartifact = "out_*.json"\njson_key = "git_sha"\nmust_equal = "HEAD"\n',
    )
    outcome = _check(sc)
    assert outcome.status == "block"
    assert any("used the newest, out_b.json" in e for e in outcome.errors)


# ── waiver ───────────────────────────────────────────────────────────────────


def test_waiver_passes_and_emits_event(tmp_path, events):
    _git_repo(tmp_path)
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    sc = _sidecar(
        tmp_path,
        extra='[requires]\nartifact = "missing.json"\n\n[requires.waiver]\nreason = "pilot run"\n',
    )
    outcome = _check(sc)
    assert outcome.status == "pass"
    assert outcome.waiver_reason == "pilot run"
    assert ("prereg.requires_waived", {"sidecar": str(sc), "reason": "pilot run"}) in events


# ── [budget] ─────────────────────────────────────────────────────────────────


def _budget_sidecar(tmp_path: Path, per_unit_s: float, measured: float) -> Path:
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    (tmp_path / "scripts/experiments").mkdir(parents=True, exist_ok=True)
    (tmp_path / "scripts/experiments/probe.json").write_text(
        json.dumps({"timing": {"per_unit_s": measured}})
    )
    return _sidecar(
        tmp_path,
        extra=(
            f'[budget]\nper_unit_s = {per_unit_s}\nprobe_record = "probe.json"\n'
            'record_per_unit_key = "timing.per_unit_s"\ntolerance = 0.5\n'
        ),
    )


def test_budget_underestimate_blocks_with_budget_code(tmp_path):
    sc = _budget_sidecar(tmp_path, per_unit_s=1.0, measured=10.0)
    outcome = _check(sc)
    assert outcome.status == "block"
    assert outcome.error_code == GateErrorCode.BUDGET_UNMET
    assert "underestimates the measured 10.0" in outcome.errors[0]


def test_budget_at_floor_passes(tmp_path):
    # floor = 10.0 * (1 - 0.5) = 5.0; equal to the floor is not an underestimate
    assert _check(_budget_sidecar(tmp_path, per_unit_s=5.0, measured=10.0)).status == "pass"


def test_budget_above_floor_passes(tmp_path):
    assert _check(_budget_sidecar(tmp_path, per_unit_s=6.0, measured=10.0)).status == "pass"


def test_budget_missing_probe_record_blocks(tmp_path):
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    sc = _sidecar(tmp_path, extra='[budget]\nper_unit_s = 1.0\nprobe_record = "absent.json"\n')
    outcome = _check(sc)
    assert outcome.status == "block"
    assert outcome.error_code == GateErrorCode.BUDGET_UNMET
    assert "absent.json not found" in outcome.errors[0]


def test_requires_and_budget_both_failing_report_requires_code(tmp_path):
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    sc = _sidecar(
        tmp_path,
        extra='[requires]\nartifact = "missing.json"\n\n[budget]\nper_unit_s = 1.0\n'
        'probe_record = "absent.json"\n',
    )
    outcome = _check(sc)
    assert outcome.error_code == GateErrorCode.REQUIRES_UNMET
    assert len(outcome.errors) == 2


# ── internal errors fail closed ──────────────────────────────────────────────


def test_internal_error_is_unmet_not_pass(tmp_path, monkeypatch):
    head = _git_repo(tmp_path)
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    (tmp_path / "scripts/experiments").mkdir(parents=True)
    (tmp_path / "scripts/experiments/out.json").write_text(json.dumps({"git_sha": head}))
    sc = _sidecar(
        tmp_path,
        extra='[requires]\nartifact = "out.json"\njson_key = "git_sha"\nmust_equal = "HEAD"\n',
    )

    def boom(*_a, **_k):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(requires_gate.json, "loads", boom)
    outcome = _check(sc)
    assert outcome.status == "block"
    assert outcome.error_code == GateErrorCode.REQUIRES_UNMET
    assert outcome.errors == ["internal error in requires gate: RuntimeError: simulated failure"]


def test_internal_error_in_budget_fails_closed(tmp_path, monkeypatch):
    sc = _budget_sidecar(tmp_path, per_unit_s=6.0, measured=10.0)

    def boom(*_a, **_k):
        raise RuntimeError("simulated budget failure")

    monkeypatch.setattr(requires_gate, "_budget_errors", boom)
    outcome = _check(sc)
    assert outcome.status != "pass"
    assert outcome.error_code == GateErrorCode.REQUIRES_UNMET
    assert "internal error in requires gate: RuntimeError" in outcome.errors[0]


# ── [gate] config ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("bad", ['"next week"', "20261101", '"2026-13-40"', "2026-10-01T12:00:00"])
def test_malformed_cutoff_raises_config_error(tmp_path, bad):
    _gate_config(tmp_path, bad)
    with pytest.raises(EnforcementConfigError, match="requires_enforce_after"):
        load_requires_enforce_after(tmp_path)


def test_unset_cutoff_reads_as_none(tmp_path):
    _gate_config(tmp_path, None)
    assert load_requires_enforce_after(tmp_path) is None


def test_unknown_gate_key_raises(tmp_path):
    (tmp_path / ".bth.toml").write_text('[gate]\nrequires_enforce_yesterday = "2026-10-01"\n')
    with pytest.raises(EnforcementConfigError, match="unknown key"):
        load_requires_enforce_after(tmp_path)


def test_malformed_cutoff_blocks_in_gate_check(tmp_path):
    head = _git_repo(tmp_path)
    _gate_config(tmp_path, '"next week"')
    (tmp_path / "scripts/experiments").mkdir(parents=True)
    (tmp_path / "scripts/experiments/out.json").write_text(json.dumps({"git_sha": head}))
    _sidecar(
        tmp_path,
        extra='[requires]\nartifact = "out.json"\njson_key = "git_sha"\nmust_equal = "HEAD"\n',
    )
    script = tmp_path / "scripts/experiments/run_nvt.py"
    script.write_text("print(1)\n")
    result = gate_check(script, resolve_sidecar(script), "collaborative")
    assert result.ok is False
    assert result.error_payload is not None
    assert result.error_payload.taxonomy_label == "gate_config_invalid"


# ── JAX heuristic ────────────────────────────────────────────────────────────


def test_jax_script_without_requires_warns(tmp_path):
    _git_repo(tmp_path)
    _gate_config(tmp_path, None)
    sc = _sidecar(tmp_path)
    script = tmp_path / "scripts/experiments/run_nvt.py"
    script.write_text("import jax\nprint(1)\n")
    outcome = _check(sc, script_path=script)
    assert outcome.status == "warn"
    assert outcome.error_code is None
    assert "JAX workload with no [requires] table" in outcome.errors[0]


def test_from_jax_import_also_warns(tmp_path):
    _git_repo(tmp_path)
    _gate_config(tmp_path, None)
    sc = _sidecar(tmp_path)
    script = tmp_path / "scripts/experiments/run_nvt.py"
    script.write_text("x = 1\nfrom jax import numpy as jnp\n")
    assert _check(sc, script_path=script).status == "warn"


def test_non_jax_script_without_requires_passes(tmp_path):
    _git_repo(tmp_path)
    _gate_config(tmp_path, None)
    sc = _sidecar(tmp_path)
    script = tmp_path / "scripts/experiments/run_nvt.py"
    script.write_text("import numpy\nprint(1)\n")
    assert _check(sc, script_path=script).status == "pass"


# ── gate_check integration ───────────────────────────────────────────────────


def test_gate_check_blocks_unmet_requires_with_sidecar_hint(tmp_path):
    _git_repo(tmp_path)
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    _sidecar(
        tmp_path,
        extra=(
            '[requires]\nartifact = "missing.json"\n'
            'hint = "Run scripts/experiments/make_probe.py first"\n'
        ),
    )
    script = tmp_path / "scripts/experiments/run_nvt.py"
    script.write_text("print(1)\n")
    result = gate_check(script, resolve_sidecar(script), "collaborative")
    assert result.ok is False
    assert result.error_payload is not None
    assert result.error_payload.error_code == GateErrorCode.REQUIRES_UNMET
    assert result.error_payload.taxonomy_label == "requires_unmet"
    assert result.error_payload.resolution_hint == "Run scripts/experiments/make_probe.py first"


def test_gate_check_blocks_in_autonomous_mode_too(tmp_path):
    _git_repo(tmp_path)
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    _sidecar(tmp_path, extra='[requires]\nartifact = "missing.json"\n')
    script = tmp_path / "scripts/experiments/run_nvt.py"
    script.write_text("print(1)\n")
    result = gate_check(script, resolve_sidecar(script), "autonomous")
    assert result.ok is False
    assert result.error_payload.error_code == GateErrorCode.REQUIRES_UNMET


def test_gate_check_warns_and_passes_at_exploration(tmp_path):
    _git_repo(tmp_path)
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    _sidecar(
        tmp_path,
        stage="exploration",
        extra='[observe]\noutputs = ["x.csv"]\n\n[requires]\nartifact = "missing.json"\n',
    )
    script = tmp_path / "scripts/experiments/run_nvt.py"
    script.write_text("print(1)\n")
    result = gate_check(script, resolve_sidecar(script), "collaborative")
    assert result.ok is True
    assert result.error_payload is None


def test_gate_check_carries_waiver_reason(tmp_path):
    _git_repo(tmp_path)
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    _sidecar(
        tmp_path,
        extra='[requires]\nartifact = "missing.json"\n\n[requires.waiver]\nreason = "pilot"\n',
    )
    script = tmp_path / "scripts/experiments/run_nvt.py"
    script.write_text("print(1)\n")
    result = gate_check(script, resolve_sidecar(script), "collaborative")
    assert result.ok is True
    assert result.waiver_reason == "pilot"


def test_cluster_only_sidecar_is_not_parsed_as_experiment(tmp_path):
    # A [cluster]-only sidecar (used for submit config) declares no [requires]/[budget] and must not
    # be rejected by the experiment parser; it reads as ungated.
    from bathos.requires_gate import preflight

    _gate_config(tmp_path, None)
    sidecar = tmp_path / "scripts" / "experiments" / "run_nvt.bth.toml"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text('[cluster]\npreset = "gpu"\n')
    script = sidecar.with_suffix(".py")
    script.write_text("print(1)\n")
    assert preflight(script, sidecar).status == "pass"


# ── review fixes (spec 261016 B2 review): fail-open paths ───────────────────


def test_ambient_git_dir_does_not_redirect_head(tmp_path, monkeypatch):
    """An ambient GIT_DIR must not make HEAD resolve to another repository (finding 3)."""
    other = tmp_path / "other"
    other_head = _git_repo(other)
    proj = tmp_path / "proj"
    _git_repo(proj)
    subprocess.run(
        [
            "git",
            "-C",
            str(proj),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "second",
        ],
        check=True,
        capture_output=True,
    )
    proj_head = subprocess.run(
        ["git", "-C", str(proj), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    assert other_head != proj_head
    _gate_config(proj, f'"{PAST_CUTOFF}"')
    (proj / "scripts/experiments").mkdir(parents=True)
    (proj / "scripts/experiments/out.json").write_text(json.dumps({"git_sha": other_head}))
    sc = _sidecar(
        proj,
        extra='[requires]\nartifact = "out.json"\njson_key = "git_sha"\nmust_equal = "HEAD"\n',
    )
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    outcome = _check(sc)
    assert outcome.status == "block", outcome
    assert any(f"expected HEAD {proj_head}" in e for e in outcome.errors)


@pytest.mark.parametrize("measured", [0.0, -2.0])
def test_budget_non_positive_probe_value_is_unmet(tmp_path, measured):
    """A measured per-unit time <= 0 is not a valid probe and must not pass (finding 4)."""
    sc = _budget_sidecar(tmp_path, per_unit_s=1.0, measured=measured)
    outcome = _check(sc)
    assert outcome.status == "block"
    assert outcome.error_code == GateErrorCode.BUDGET_UNMET
    assert (
        f"probe record value at timing.per_unit_s is {measured}, not a positive finite number"
        in outcome.errors[0]
    )


def test_json_key_non_string_value_is_unmet(tmp_path):
    """A numeric git_sha is not a commit hash; the value must be a string (finding 5)."""
    _git_repo(tmp_path)
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    (tmp_path / "scripts/experiments").mkdir(parents=True)
    (tmp_path / "scripts/experiments/out.json").write_text(json.dumps({"git_sha": 1234567}))
    sc = _sidecar(
        tmp_path,
        extra='[requires]\nartifact = "out.json"\njson_key = "git_sha"\nmust_equal = "HEAD"\n',
    )
    outcome = _check(sc)
    assert outcome.status == "block"
    assert "git_sha is int, expected a string" in outcome.errors[0]


def test_json_value_whitespace_is_stripped_before_matching(tmp_path):
    head = _git_repo(tmp_path)
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    (tmp_path / "scripts/experiments").mkdir(parents=True)
    (tmp_path / "scripts/experiments/out.json").write_text(json.dumps({"git_sha": f" {head}\n"}))
    sc = _sidecar(
        tmp_path,
        extra='[requires]\nartifact = "out.json"\njson_key = "git_sha"\nmust_equal = "HEAD"\n',
    )
    assert _check(sc).status == "pass"


def test_reject_suffix_match_is_case_insensitive(tmp_path):
    _git_repo(tmp_path)
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    (tmp_path / "scripts/experiments").mkdir(parents=True)
    (tmp_path / "scripts/experiments/out.json").write_text(json.dumps({"git_sha": "abc1234-DIRTY"}))
    sc = _sidecar(tmp_path, extra='[requires]\nartifact = "out.json"\njson_key = "git_sha"\n')
    outcome = _check(sc)
    assert outcome.status == "block"
    assert "ends with -dirty" in outcome.errors[0]


def test_literal_must_equal_is_exact_not_hex_prefix(tmp_path):
    """Hex-prefix matching is for HEAD only; a literal 7-char must_equal does not prefix-match (finding 6)."""
    _git_repo(tmp_path)
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    (tmp_path / "scripts/experiments").mkdir(parents=True)
    (tmp_path / "scripts/experiments/out.json").write_text(json.dumps({"tag": "abcdef1234"}))
    sc = _sidecar(
        tmp_path,
        extra='[requires]\nartifact = "out.json"\njson_key = "tag"\nmust_equal = "abcdef1"\n',
    )
    outcome = _check(sc)
    assert outcome.status == "block"
    assert "tag = abcdef1234, expected abcdef1" in outcome.errors[0]


def test_budget_only_failure_gets_budget_hint_not_requires_hint(tmp_path):
    """A budget-only failure must not point at [requires] or a waiver (finding 7)."""
    sc = _budget_sidecar(tmp_path, per_unit_s=1.0, measured=10.0)
    outcome = _check(sc)
    assert outcome.status == "block"
    assert "Re-measure with the probe and update [budget] per_unit_s / est_wall_s" in outcome.hint
    assert "[requires]" not in outcome.hint
    assert "waiver" not in outcome.hint


def test_both_failures_include_both_hints(tmp_path):
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    sc = _sidecar(
        tmp_path,
        extra='[requires]\nartifact = "missing.json"\n\n[budget]\nper_unit_s = 1.0\n'
        'probe_record = "absent.json"\n',
    )
    outcome = _check(sc)
    assert "Produce the artifact named in [requires]" in outcome.hint
    assert "Re-measure with the probe" in outcome.hint


def test_budget_resolution_hint_does_not_suggest_waiver():
    """The waiver does not cover [budget]; the catalogued BUDGET_UNMET hint must not offer one (finding 7)."""
    from bathos.prereg import _RESOLUTION_HINTS

    hint = _RESOLUTION_HINTS[GateErrorCode.BUDGET_UNMET]
    assert "waiver" not in hint
    assert "[budget]" in hint


def test_artifact_symlink_escaping_sidecar_dir_is_unmet(tmp_path):
    head = _git_repo(tmp_path)
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"git_sha": head}))
    d = tmp_path / "scripts/experiments"
    d.mkdir(parents=True)
    (d / "out.json").symlink_to(outside)
    sc = _sidecar(
        tmp_path,
        extra='[requires]\nartifact = "out.json"\njson_key = "git_sha"\nmust_equal = "HEAD"\n',
    )
    outcome = _check(sc)
    assert outcome.status == "block"
    assert any("artifact resolves outside the sidecar directory" in e for e in outcome.errors)


def test_probe_record_symlink_escaping_sidecar_dir_is_unmet(tmp_path):
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    outside = tmp_path / "outside_probe.json"
    outside.write_text(json.dumps({"timing": {"per_unit_s": 1.0}}))  # would pass the budget
    d = tmp_path / "scripts/experiments"
    d.mkdir(parents=True)
    (d / "probe.json").symlink_to(outside)
    sc = _sidecar(
        tmp_path,
        extra=(
            '[budget]\nper_unit_s = 10.0\nprobe_record = "probe.json"\n'
            'record_per_unit_key = "timing.per_unit_s"\n'
        ),
    )
    outcome = _check(sc)
    assert outcome.status == "block"
    assert any("resolves outside the sidecar directory" in e for e in outcome.errors)


def test_nested_bth_toml_without_gate_uses_ancestor_cutoff(tmp_path, caplog):
    """A nearer .bth.toml with no [gate] must not silently turn enforcement off (finding 10)."""
    import logging

    _git_repo(tmp_path)
    _gate_config(tmp_path, f'"{PAST_CUTOFF}"')
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / ".bth.toml").write_text('[project]\nslug = "nested"\nroot = "."\n')
    sc = _sidecar(sub, extra='[requires]\nartifact = "missing.json"\n')
    with caplog.at_level(logging.WARNING, logger="bathos.config"):
        outcome = _check(sc)
    assert outcome.status == "block", outcome
    assert any(".bth.toml" in r.getMessage() and "sub" in r.getMessage() for r in caplog.records)
