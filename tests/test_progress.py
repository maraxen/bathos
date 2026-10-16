"""Advisory per-run progress file, `$BTH_PROGRESS_PATH` (issue #105).

A progress file is optional, never a result, and any malformed state reads as "unknown" (None).
"""

from __future__ import annotations

import json
import sys
import tempfile
import textwrap
from dataclasses import replace
from pathlib import Path

import pytest

from bathos.catalog import init_catalog, read_runs, write_run
from bathos.mcp import get_run_tool
from bathos.progress import progress_path_for, read_progress
from bathos.runner import run_script
from bathos.schema import Run

# ---------------------------------------------------------------- read_progress


def _write(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


def test_valid_file_is_read(tmp_path: Path):
    p = _write(
        tmp_path / "p.bth-progress.json",
        json.dumps(
            {"done": 3, "total": 10, "unit": "frames", "updated_at": "2026-10-16T12:00:00+00:00"}
        ),
    )
    assert read_progress(p) == {
        "done": 3,
        "total": 10,
        "unit": "frames",
        "updated_at": "2026-10-16T12:00:00+00:00",
    }


def test_missing_file_is_none(tmp_path: Path):
    assert read_progress(tmp_path / "absent.bth-progress.json") is None


@pytest.mark.parametrize(
    "text",
    [
        "{not json",  # malformed
        '{"done": 3, "total": 1',  # truncated / partial write
        "",  # empty file
        "[1, 2, 3]",  # JSON list, not an object
        '"just a string"',
        '{"done": "3", "total": 10}',  # done as string
        '{"total": 10}',  # done missing
        '{"done": true}',  # bool is not a count
        '{"done": 3.5}',  # float is not an int
    ],
)
def test_unusable_content_is_none(tmp_path: Path, text: str):
    p = _write(tmp_path / "p.bth-progress.json", text)
    assert read_progress(p) is None


def test_non_utf8_bytes_are_none(tmp_path: Path):
    p = tmp_path / "p.bth-progress.json"
    p.write_bytes(b"\xff\xfe\x00garbage")
    assert read_progress(p) is None


def test_directory_in_place_of_file_is_none(tmp_path: Path):
    d = tmp_path / "x.bth-progress.json"
    d.mkdir()
    assert read_progress(d) is None


def test_extra_keys_ignored_and_bad_optionals_coerced(tmp_path: Path):
    p = _write(
        tmp_path / "p.bth-progress.json",
        json.dumps({"done": 1, "total": "ten", "unit": 7, "updated_at": None, "extra": [1]}),
    )
    assert read_progress(p) == {"done": 1, "total": None, "unit": None, "updated_at": None}


def test_negative_control_garbage_changes_nothing_else(tmp_path: Path):
    """Garbage in the progress file must not raise or affect any other state."""
    p = _write(tmp_path / "p.bth-progress.json", "\x00\x01 not progress at all }{")
    assert read_progress(p) is None
    # A sibling file (stand-in for the results temp file) is untouched by reading progress.
    sibling = _write(tmp_path / "r.bth-results.json", '{"x": 1}')
    read_progress(p)
    assert json.loads(sibling.read_text()) == {"x": 1}


# ---------------------------------------------------------------- progress_path_for


def test_progress_path_is_per_run_and_suffixed(tmp_path: Path):
    a = progress_path_for("run_a", tmp_path)
    b = progress_path_for("run_b", tmp_path)
    assert a != b
    assert a.name == "run_a.bth-progress.json"
    assert a.parent == tmp_path
    assert a.name.endswith(".bth-progress.json")


def test_progress_path_defaults_to_system_temp():
    p = progress_path_for("run_z")
    assert p.parent == Path(tempfile.gettempdir())
    assert p.name == "run_z.bth-progress.json"


# ---------------------------------------------------------------- real bth run

# Child script: argv[1] is where it records the env var it saw; argv[2] is the mode.
# mode "write" overwrites BTH_PROGRESS_PATH atomically; mode "garbage" writes a broken file;
# mode "none" never touches it.
_PROGRESS_SCRIPT = textwrap.dedent("""
    import json, os, sys
    seen_out, mode = sys.argv[1], sys.argv[2]
    p = os.environ.get("BTH_PROGRESS_PATH", "")
    with open(seen_out, "w") as f:
        f.write(p)
    if mode == "write" and p:
        tmp = p + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"done": 4, "total": 8, "unit": "steps"}, f)
        os.replace(tmp, p)
    elif mode == "garbage" and p:
        with open(p, "w") as f:
            f.write("{partial")
""")


def _run_progress_script(tmp_path: Path, tmp_catalog: Path, mode: str) -> tuple[int, str, Path]:
    init_catalog(tmp_catalog)
    scripts = tmp_path / "scripts" / "explore"
    scripts.mkdir(parents=True, exist_ok=True)
    script = scripts / "progress_probe.py"
    script.write_text(_PROGRESS_SCRIPT)
    seen = tmp_path / "seen_env.txt"
    rc = run_script(
        argv=[sys.executable, str(script), str(seen), mode],
        project_slug="testproj",
        catalog_dir=tmp_catalog,
        output_paths=[],
        tags=[],
        cwd=tmp_path,
    )
    return rc, seen.read_text(), seen


@pytest.mark.parametrize("mode", ["write", "garbage"])
def test_run_exports_env_var_and_removes_progress_file(tmp_path, tmp_catalog, mode):
    rc, seen_path_str, _ = _run_progress_script(tmp_path, tmp_catalog, mode)
    assert rc == 0
    assert seen_path_str, "BTH_PROGRESS_PATH was not exported to the child"
    progress_file = Path(seen_path_str)
    assert progress_file.name.endswith(".bth-progress.json")
    # The child wrote the file during the run; the runner must have removed it at run end.
    assert not progress_file.exists()
    runs = read_runs(tmp_catalog)
    assert len(runs) == 1
    assert runs[0].status == "completed"


def test_script_that_never_writes_progress_runs_fine_and_leaves_nothing(tmp_path, tmp_catalog):
    rc, seen_path_str, _ = _run_progress_script(tmp_path, tmp_catalog, "none")
    assert rc == 0
    assert seen_path_str
    assert not Path(seen_path_str).exists()
    assert read_runs(tmp_catalog)[0].status == "completed"


# ---------------------------------------------------------------- get_run progress key


@pytest.fixture
def temp_dir_at_tmp(tmp_path, monkeypatch) -> Path:
    """Point tempfile.gettempdir() (used by the default progress path) at tmp_path."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return tmp_path


def _seed(tmp_catalog: Path, sample_run: Run, run_id: str, status: str) -> None:
    init_catalog(tmp_catalog)
    write_run(replace(sample_run, id=run_id, status=status), tmp_catalog)


@pytest.mark.usefixtures("temp_dir_at_tmp")
def test_get_run_includes_progress_when_running(tmp_catalog, sample_run):
    _seed(tmp_catalog, sample_run, "runR", "running")
    progress_path_for("runR").write_text(json.dumps({"done": 2, "total": 5, "unit": "frames"}))
    out = get_run_tool(catalog_dir=str(tmp_catalog), run_id="runR")
    assert out["status"] == "running"
    assert out["progress"] == {"done": 2, "total": 5, "unit": "frames", "updated_at": None}


@pytest.mark.usefixtures("temp_dir_at_tmp")
def test_get_run_running_without_progress_file_has_no_key(tmp_catalog, sample_run):
    _seed(tmp_catalog, sample_run, "runR", "running")
    out = get_run_tool(catalog_dir=str(tmp_catalog), run_id="runR")
    assert out["status"] == "running"
    assert "progress" not in out


@pytest.mark.usefixtures("temp_dir_at_tmp")
def test_get_run_running_with_malformed_progress_has_no_key(tmp_catalog, sample_run):
    _seed(tmp_catalog, sample_run, "runR", "running")
    progress_path_for("runR").write_text("{partial")
    out = get_run_tool(catalog_dir=str(tmp_catalog), run_id="runR")
    assert "progress" not in out


@pytest.mark.usefixtures("temp_dir_at_tmp")
def test_get_run_non_running_ignores_stale_progress_file(tmp_catalog, sample_run):
    _seed(tmp_catalog, sample_run, "runC", "completed")
    baseline = get_run_tool(catalog_dir=str(tmp_catalog), run_id="runC")
    progress_path_for("runC").write_text(json.dumps({"done": 9, "total": 9}))
    out = get_run_tool(catalog_dir=str(tmp_catalog), run_id="runC")
    assert "progress" not in out
    assert out == baseline
