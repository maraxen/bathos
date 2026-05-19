# `@bth.experiment` Decorator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `@bth.experiment` decorator that wraps any callable, captures provenance (git state, timing, exit code), and writes a `Run` record to the catalog automatically — without requiring `bth run` as the outer command.

**Architecture:** New `src/bathos/decorators.py` module. Decorator reads `BTH_PROJECT_SLUG` and `BTH_CATALOG_DIR` from env (same as CLI). Captures `sys.argv`, `capture_git_state()`, timing, and exit code. Writes Run via `write_run()`. Export via `src/bathos/__init__.py`. Pre-registration enforcement is **not** applied (decorator is provenance-only per design).

**Tech Stack:** Python 3.12, dataclasses, functools, sys, time, pathlib

---

## File Map

| Action | Path |
|--------|------|
| Create | `src/bathos/decorators.py` |
| Modify | `src/bathos/__init__.py` |
| Create | `tests/test_decorators.py` |

---

## Task 1: Create `decorators.py`

**Files:**
- Create: `src/bathos/decorators.py`
- Create: `tests/test_decorators.py`

- [ ] **Step 1.1: Write failing tests**

Create `tests/test_decorators.py`:

```python
import os
import sys
from pathlib import Path
from typer.testing import CliRunner

runner = CliRunner()


def test_decorator_records_run(tmp_path, monkeypatch):
    """@bth.experiment writes a Run to the catalog."""
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "test_proj")
    monkeypatch.chdir(tmp_path)

    from bathos.decorators import experiment
    from bathos.query import list_runs

    @experiment
    def my_fn():
        return 42

    result = my_fn()
    assert result == 42

    runs = list_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0].project_slug == "test_proj"
    assert runs[0].status == "completed"
    assert runs[0].exit_code == 0


def test_decorator_records_failure(tmp_path, monkeypatch):
    """@bth.experiment records failed runs on exception."""
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "test_proj")

    from bathos.decorators import experiment
    from bathos.query import list_runs

    @experiment
    def bad_fn():
        raise ValueError("boom")

    try:
        bad_fn()
    except ValueError:
        pass

    runs = list_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0].status == "failed"
    assert runs[0].exit_code == 1


def test_decorator_captures_function_name(tmp_path, monkeypatch):
    """@bth.experiment uses function name as command."""
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "proj")

    from bathos.decorators import experiment
    from bathos.query import list_runs

    @experiment
    def run_nvt_stability():
        pass

    run_nvt_stability()

    runs = list_runs(tmp_path)
    assert "run_nvt_stability" in runs[0].command


def test_decorator_preserves_function_name():
    """@bth.experiment preserves __name__ and __doc__."""
    from bathos.decorators import experiment

    @experiment
    def my_fn():
        """My docstring."""
        pass

    assert my_fn.__name__ == "my_fn"
    assert my_fn.__doc__ == "My docstring."


def test_decorator_no_project_slug_skips_recording(tmp_path, monkeypatch):
    """@bth.experiment skips recording (warns) if BTH_PROJECT_SLUG not set."""
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path))
    monkeypatch.delenv("BTH_PROJECT_SLUG", raising=False)

    from bathos.decorators import experiment
    from bathos.query import list_runs

    @experiment
    def my_fn():
        return 99

    result = my_fn()
    assert result == 99
    assert list_runs(tmp_path) == []


def test_bth_experiment_importable():
    """import bathos; bathos.experiment is the decorator."""
    import bathos
    assert hasattr(bathos, "experiment")
    assert callable(bathos.experiment)
```

- [ ] **Step 1.2: Run tests to confirm failure**

```bash
cd /home/marielle/projects/bathos
uv run pytest tests/test_decorators.py -v
```

Expected: `ERROR` — `ModuleNotFoundError: No module named 'bathos.decorators'`

- [ ] **Step 1.3: Create `src/bathos/decorators.py`**

```python
from __future__ import annotations

import dataclasses
import functools
import os
import sys
import time
import warnings
from pathlib import Path

from bathos.catalog import write_run
from bathos.config import default_catalog_dir
from bathos.git import capture_git_state
from bathos.schema import Run


def experiment(func):
    """Decorator: capture provenance for a function and write a Run to the catalog.

    Reads BTH_PROJECT_SLUG and BTH_CATALOG_DIR from env. If BTH_PROJECT_SLUG
    is not set, skips recording and runs the function unmodified.
    """
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        project_slug = os.environ.get("BTH_PROJECT_SLUG", "").strip()
        if not project_slug:
            warnings.warn(
                f"@bth.experiment: BTH_PROJECT_SLUG not set — provenance not recorded for {func.__name__}",
                stacklevel=2,
            )
            return func(*args, **kwargs)

        catalog_dir_env = os.environ.get("BTH_CATALOG_DIR")
        catalog_dir = Path(catalog_dir_env) if catalog_dir_env else default_catalog_dir()

        cwd = Path.cwd()
        git = capture_git_state(cwd)
        command = f"{func.__module__}.{func.__name__}"
        argv = [func.__name__] + sys.argv[1:]

        run = Run(
            project_slug=project_slug,
            command=command,
            argv=argv,
            git_hash=git.hash,
            git_branch=git.branch,
            git_dirty=git.dirty,
            status="running",
        )
        catalog_dir.mkdir(parents=True, exist_ok=True)
        write_run(run, catalog_dir)

        start = time.monotonic()
        exit_code = 0
        status = "completed"
        try:
            result = func(*args, **kwargs)
        except BaseException as exc:
            exit_code = 1
            status = "failed"
            raise
        finally:
            run = dataclasses.replace(
                run,
                duration_s=time.monotonic() - start,
                exit_code=exit_code,
                status=status,
            )
            write_run(run, catalog_dir)

        return result

    return wrapper
```

- [ ] **Step 1.4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_decorators.py -v
```

Expected: `6 passed`

- [ ] **Step 1.5: Run full suite**

```bash
uv run pytest -q
```

Expected: all pass

- [ ] **Step 1.6: Commit**

```bash
git add src/bathos/decorators.py tests/test_decorators.py
git commit -m "feat(decorators): add @bth.experiment provenance decorator"
```

---

## Task 2: Export via `__init__.py`

**Files:**
- Modify: `src/bathos/__init__.py`

- [ ] **Step 2.1: Read current `__init__.py`**

```bash
cat /home/marielle/projects/bathos/src/bathos/__init__.py
```

- [ ] **Step 2.2: Add export**

The file currently contains only `__version__ = "0.1.0"`. Update to:

```python
__version__ = "0.1.0"

from bathos.decorators import experiment

__all__ = ["experiment"]
```

- [ ] **Step 2.3: Run the import test**

```bash
uv run pytest tests/test_decorators.py::test_bth_experiment_importable -v
```

Expected: `1 passed`

- [ ] **Step 2.4: Run full suite**

```bash
uv run pytest -q
```

Expected: all pass

- [ ] **Step 2.5: Commit**

```bash
git add src/bathos/__init__.py
git commit -m "feat(init): export @bth.experiment decorator from top-level package"
```

---

## Final Verification

- [ ] **Smoke test in a tmp script**

```bash
cd /tmp
BTH_PROJECT_SLUG=smoke BTH_CATALOG_DIR=/tmp/smoke_cat python3 - <<'EOF'
import sys
sys.path.insert(0, "/home/marielle/projects/bathos/src")
import bathos

@bathos.experiment
def run_smoke():
    print("smoke test")
    return 0

run_smoke()

from bathos.query import list_runs
from pathlib import Path
runs = list_runs(Path("/tmp/smoke_cat"))
print(f"Recorded {len(runs)} run(s): {runs[0].command if runs else 'none'}")
EOF
```

Expected: `Recorded 1 run(s): <module>.run_smoke`
