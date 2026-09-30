"""The pre-execution lock manifest must be valid TOML (bathos debt 2318).

`_write_manifest` used to emit `agent_id = null`. TOML has no null, so every lock manifest
bathos ever wrote was unparseable by a real TOML reader (tomllib raises on the last line).
Nothing inside bathos parses the file, so the defect was silent; downstream verifiers
(protamer's run-record checker) had to special-case the one bare line to read it at all.
"""

from __future__ import annotations

import dataclasses
import hashlib
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest

from bathos.runner import _write_manifest, run_script
from bathos.schema import Run


def _sidecar(tmp_path: Path) -> Path:
    path = tmp_path / "exp.bth.toml"
    path.write_text("[experiment]\nhypothesis = 'h'\n")
    return path


def test_negative_control_toml_rejects_null():
    """The parser must actually reject the old line, or the tests below prove nothing."""
    with pytest.raises(tomllib.TOMLDecodeError):
        tomllib.loads('[manifest]\nrun_id = "x"\nagent_id = null\n')


def test_manifest_is_valid_toml_and_carries_identity(tmp_path: Path, sample_run: Run):
    run = dataclasses.replace(sample_run, id="11111111-2222-3333-4444-555555555555")
    sidecar = _sidecar(tmp_path)

    sha, path = _write_manifest(run, sidecar, "a" * 64, tmp_path)

    assert path == str((tmp_path / f"exp.bth.{run.id}.bth.lock.toml").resolve())
    raw = Path(path).read_bytes()
    parsed = tomllib.loads(raw.decode())  # raised TOMLDecodeError before the fix
    manifest = parsed["manifest"]
    assert manifest["run_id"] == run.id
    assert manifest["sidecar_sha256"] == "a" * 64
    assert manifest["git_sha"] == run.git_hash
    assert sha == hashlib.sha256(raw).hexdigest()


def test_manifest_does_not_invent_an_agent_id(tmp_path: Path, sample_run: Run):
    """Absent, not null and not an empty string: a reader must not mistake it for an agent."""
    _, path = _write_manifest(sample_run, _sidecar(tmp_path), "b" * 64, tmp_path)
    assert "agent_id" not in tomllib.loads(Path(path).read_text())["manifest"]


def test_run_script_writes_a_parseable_lock_manifest(tmp_path: Path):
    """End to end through the runner, in an enforced dir, rather than the helper alone."""
    (tmp_path / "catalog").mkdir()
    enforced = tmp_path / "scripts" / "experiments"
    enforced.mkdir(parents=True)
    script = enforced / "run_x.py"
    script.write_text("print('hi')")
    (enforced / "run_x.bth.toml").write_text(
        textwrap.dedent("""
            [experiment]
            hypothesis = "h"
            [outcomes.pass]
            condition = "x == 1"
            decision = "good"
            reasoning = "expected"
            [outcomes.fallback]
            condition = "1==1"
            decision = "other"
            reasoning = "catch-all"
            is_residual = true
            [result_schema]
            x = "float"
        """)
    )

    rc = run_script(
        argv=[sys.executable, str(script)],
        project_slug="proj",
        catalog_dir=tmp_path / "catalog",
        output_paths=[],
        tags=[],
        cwd=tmp_path,
    )

    assert rc == 0
    locks = sorted(enforced.glob("*.bth.lock.toml"))
    assert len(locks) == 1, locks
    parsed = tomllib.loads(locks[0].read_text())
    assert parsed["manifest"]["run_id"] in locks[0].name
