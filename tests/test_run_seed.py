"""`bth run --seed N` records Run.seed and hands the same value to the script (debt #2077).

Before this, Run.seed existed in the schema and the warm table but nothing ever wrote it, so
every seed-floor gate was unreachable. bathos does not seed anything itself: the recorded value
is only a claim about the run if the script can read it, so the runner also exports it as
$BTH_SEED. A `--seed` placed AFTER the script path belongs to the script.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bathos.catalog import read_runs
from bathos.cli_cyclopts import app
from bathos.runner import run_script
from tests._cyclopts_runner import CyclopticRunner

runner = CyclopticRunner()


def _probe(tmp_path: Path) -> tuple[Path, Path]:
    """A script that records the BTH_SEED it was given (or null) and its own argv."""
    record = tmp_path / "seen.json"
    script = tmp_path / "probe.py"
    script.write_text(
        "import json, os, sys\n"
        f"json.dump({{'env': os.environ.get('BTH_SEED'), 'argv': sys.argv[1:]}}, "
        f"open(r'{record}', 'w'))\n"
    )
    return script, record


def _run(tmp_path: Path, script: Path, **kwargs) -> int:
    return run_script(
        [__import__("sys").executable, str(script)],
        "testproj",
        tmp_path / "catalog",
        [],
        [],
        cwd=tmp_path,
        no_sidecar=True,
        **kwargs,
    )


def test_seed_is_recorded_and_exported_to_the_script(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BTH_SEED", raising=False)
    script, record = _probe(tmp_path)

    assert _run(tmp_path, script, seed=1234) == 0

    (run,) = read_runs(tmp_path / "catalog")
    assert run.seed == 1234
    assert json.loads(record.read_text())["env"] == "1234"


def test_no_seed_means_none_recorded_and_nothing_injected(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("BTH_SEED", raising=False)
    script, record = _probe(tmp_path)

    assert _run(tmp_path, script) == 0

    (run,) = read_runs(tmp_path / "catalog")
    assert run.seed is None
    assert json.loads(record.read_text())["env"] is None


def test_zero_and_negative_seeds_are_recorded_not_dropped(tmp_path: Path, monkeypatch):
    """0 is falsy; a truthiness check would silently record no seed for the most common one."""
    monkeypatch.delenv("BTH_SEED", raising=False)
    script, record = _probe(tmp_path)

    assert _run(tmp_path, script, seed=0) == 0
    assert json.loads(record.read_text())["env"] == "0"
    assert [r.seed for r in read_runs(tmp_path / "catalog")] == [0]


def test_mcp_run_tool_takes_seed_and_keeps_script_args_separate(tmp_path: Path, monkeypatch):
    """The MCP `run` tool passes script args as a separate list, so `seed` cannot collide."""
    from bathos.mcp import run_tool

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
    monkeypatch.delenv("BTH_SEED", raising=False)
    (tmp_path / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n')
    script, record = _probe(tmp_path)

    result = run_tool(script_path=str(script), args=["--seed", "9"], no_sidecar=True, seed=5)

    assert result["exit_code"] == 0, result
    seen = json.loads(record.read_text())
    assert seen == {"env": "5", "argv": ["--seed", "9"]}
    (run,) = read_runs(tmp_path / "catalog")
    assert run.seed == 5


class TestRunCli:
    @pytest.fixture(autouse=True)
    def _env(self, tmp_path: Path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / ".bth" / "catalog"))
        monkeypatch.setenv("BTH_PROJECT_SLUG", "testproj")
        monkeypatch.delenv("BTH_SEED", raising=False)
        (tmp_path / ".bth.toml").write_text(
            f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n'
        )

    def test_bth_seed_flag_is_recorded_and_exported(self, tmp_path: Path):
        script, record = _probe(tmp_path)

        result = runner.invoke(app, ["run", "--bth-seed", "42", str(script), "--no-sidecar"])

        assert result.exit_code == 0, result.output
        (run,) = read_runs(tmp_path / ".bth" / "catalog")
        assert run.seed == 42
        assert json.loads(record.read_text())["env"] == "42"

    def test_scripts_own_seed_flag_is_never_stolen(self, tmp_path: Path):
        """The reason the CLI flag is `--bth-seed`: cyclopts consumes a known option anywhere on
        the line, so a plain `--seed` flag swallowed `bth run train.py --seed 9`, leaving
        Run.seed=9 recorded while the script ran with its default seed."""
        script, record = _probe(tmp_path)

        result = runner.invoke(app, ["run", str(script), "--seed", "9"])

        assert result.exit_code == 0, result.output
        seen = json.loads(record.read_text())
        assert seen["argv"] == ["--seed", "9"]
        assert seen["env"] is None
        (run,) = read_runs(tmp_path / ".bth" / "catalog")
        assert run.seed is None
