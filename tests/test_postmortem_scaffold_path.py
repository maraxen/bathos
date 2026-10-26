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
