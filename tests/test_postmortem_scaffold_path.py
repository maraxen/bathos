"""Where `scaffold_postmortem_template` writes a run postmortem (debt #3046).

The scaffold used to take the first command token that existed as a file. For a run
recorded as `/abs/.venv/bin/python3 scripts/x.py` that token is the interpreter, so the
postmortem went into `.venv/bin/` -- a full-quota group directory on Engaging, where the
write failed with EDQUOT (lathex bth 78db9e04). Even when such a write succeeds the file
is invisible: `.venv` is in `walk.PRUNE_DIRS`, so `find_postmortem` and compact's scan
never descend into it.
"""

from __future__ import annotations

from pathlib import Path

from bathos.postmortem import find_postmortem, scaffold_postmortem_template

RUN_ID = "run_abc123"


def _touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    return path


def test_absolute_venv_interpreter_is_not_the_script(tmp_path):
    interpreter = _touch(tmp_path / ".venv" / "bin" / "python3")
    script = _touch(tmp_path / "scripts" / "experiments" / "x.py")

    out = scaffold_postmortem_template(f"{interpreter} scripts/experiments/x.py", RUN_ID, tmp_path)

    assert out == script.parent / f"x.py.{RUN_ID}.bth.postmortem.toml"
    assert not list((tmp_path / ".venv").rglob("*.bth.postmortem.toml"))
    assert find_postmortem(tmp_path, run_id=RUN_ID) is not None


def test_interpreter_outside_workspace_is_ignored(tmp_path):
    outside = _touch(tmp_path / "elsewhere" / "bin" / "python3")
    workspace = tmp_path / "ws"
    script = _touch(workspace / "scripts" / "x.py")

    out = scaffold_postmortem_template(f"{outside} scripts/x.py", RUN_ID, workspace)

    assert out == script.parent / f"x.py.{RUN_ID}.bth.postmortem.toml"
    assert not list(outside.parent.glob("*.bth.postmortem.toml"))


def test_absolute_script_path_inside_workspace(tmp_path):
    interpreter = _touch(tmp_path / ".venv" / "bin" / "python3")
    script = _touch(tmp_path / "scripts" / "x.py")

    out = scaffold_postmortem_template(f"{interpreter} {script}", RUN_ID, tmp_path)

    assert out == script.parent / f"x.py.{RUN_ID}.bth.postmortem.toml"


def test_non_python_script_inside_workspace_still_used(tmp_path):
    script = _touch(tmp_path / "scripts" / "slurm" / "go.sh")

    out = scaffold_postmortem_template("bash scripts/slurm/go.sh", RUN_ID, tmp_path)

    assert out == script.parent / f"go.sh.{RUN_ID}.bth.postmortem.toml"


def test_no_script_token_falls_back_to_bth_postmortems(tmp_path):
    tool = _touch(tmp_path / ".venv" / "bin" / "mytool")

    out = scaffold_postmortem_template(f"{tool} --flag 3", RUN_ID, tmp_path)

    assert out == tmp_path / ".bth" / "postmortems" / f"run_{RUN_ID}.bth.postmortem.toml"
    assert find_postmortem(tmp_path, run_id=RUN_ID) is not None


def test_script_symlinked_from_outside_stays_beside_the_link(tmp_path):
    real = _touch(tmp_path / "shared" / "x.py")
    workspace = tmp_path / "ws"
    link = workspace / "scripts" / "x.py"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)

    out = scaffold_postmortem_template("python scripts/x.py", RUN_ID, workspace)

    assert out == link.parent / f"x.py.{RUN_ID}.bth.postmortem.toml"
    assert find_postmortem(workspace, run_id=RUN_ID) is not None


def test_venv_interpreter_symlinked_to_system_python_is_still_excluded(tmp_path):
    system_python = _touch(tmp_path / "usr" / "bin" / "python3.13")
    workspace = tmp_path / "ws"
    interpreter = workspace / ".venv" / "bin" / "python3"
    interpreter.parent.mkdir(parents=True)
    interpreter.symlink_to(system_python)
    script = _touch(workspace / "scripts" / "x.py")

    out = scaffold_postmortem_template(f"{interpreter} scripts/x.py", RUN_ID, workspace)

    assert out == script.parent / f"x.py.{RUN_ID}.bth.postmortem.toml"


def test_config_flag_py_value_is_skipped_in_favor_of_script(tmp_path):
    """Debt #3061: `--config cfg.py run.py` must resolve `run.py`, not `cfg.py`."""
    _touch(tmp_path / "cfg.py")
    script = _touch(tmp_path / "run.py")

    out = scaffold_postmortem_template("python --config cfg.py run.py", RUN_ID, tmp_path)

    assert out == script.parent / f"run.py.{RUN_ID}.bth.postmortem.toml"


def test_equals_flag_and_multiple_value_flags_skipped(tmp_path):
    _touch(tmp_path / "cfg.py")
    _touch(tmp_path / "side.py")
    script = _touch(tmp_path / "scripts" / "experiments" / "train.py")

    cmd = (
        "uv run --offline --with helper.py --config=cfg.py --sidecar side.py "
        "scripts/experiments/train.py --out out.py"
    )
    out = scaffold_postmortem_template(cmd, RUN_ID, tmp_path)

    assert out == script.parent / f"train.py.{RUN_ID}.bth.postmortem.toml"


def test_short_bool_flags_do_not_swallow_script(tmp_path):
    script = _touch(tmp_path / "scripts" / "x.py")

    out = scaffold_postmortem_template("python -u scripts/x.py", RUN_ID, tmp_path)
    assert out == script.parent / f"x.py.{RUN_ID}.bth.postmortem.toml"

    sh = _touch(tmp_path / "scripts" / "slurm" / "go.sh")
    out_sh = scaffold_postmortem_template("bash -eu scripts/slurm/go.sh --config cfg.py", RUN_ID, tmp_path)
    assert out_sh == sh.parent / f"go.sh.{RUN_ID}.bth.postmortem.toml"


def test_unknown_flag_prioritizes_unflagged_script_without_breaking_unknown_bool(tmp_path):
    _touch(tmp_path / "cfg.py")
    script = _touch(tmp_path / "run.py")

    # Unflagged `run.py` beats `cfg.py` following unknown `--custom-cfg`
    out = scaffold_postmortem_template("python --custom-cfg cfg.py run.py", RUN_ID, tmp_path)
    assert out == script.parent / f"run.py.{RUN_ID}.bth.postmortem.toml"

    # Unknown boolean flag before the only script still resolves `run.py`
    out_bool = scaffold_postmortem_template("python --custom-bool run.py", RUN_ID, tmp_path)
    assert out_bool == script.parent / f"run.py.{RUN_ID}.bth.postmortem.toml"


def test_value_only_py_flag_without_script_falls_back_to_bth_postmortems(tmp_path):
    _touch(tmp_path / "cfg.py")
    tool = _touch(tmp_path / ".venv" / "bin" / "mytool")

    out = scaffold_postmortem_template(f"{tool} --config cfg.py", RUN_ID, tmp_path)

    assert out == tmp_path / ".bth" / "postmortems" / f"run_{RUN_ID}.bth.postmortem.toml"


def test_python_module_and_end_of_options_marker(tmp_path):
    _touch(tmp_path / "cfg.py")
    _touch(tmp_path / "mod.py")
    out_mod = scaffold_postmortem_template("python -m mod.py --config cfg.py", RUN_ID, tmp_path)
    assert out_mod == tmp_path / ".bth" / "postmortems" / f"run_{RUN_ID}.bth.postmortem.toml"

    script = _touch(tmp_path / "scripts" / "run.py")
    out_sep = scaffold_postmortem_template("python -- scripts/run.py", RUN_ID, tmp_path)
    assert out_sep == script.parent / f"run.py.{RUN_ID}.bth.postmortem.toml"
