# v0.2 Feature Wiring Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire the `outcome` field through the Run dataclass and `bth ls` display, then implement pre-registration sidecar enforcement in `bth run`.

**Architecture:** Two independent changes. (1) `outcome` already exists in WARM_SCHEMA and is unpacked in `_row_to_run()` but dropped — add the field to `Run` and pass it through. (2) A new `sidecar.py` module parses `.bth.toml` sidecars; `runner.py` calls it pre-run to block enforced directories and post-run to evaluate outcome labels.

**Tech Stack:** Python 3.12, Typer, DuckDB, PyArrow, tomllib (stdlib), uv

---

## File Map

| Action | Path |
|--------|------|
| Modify | `src/bathos/schema.py` |
| Modify | `src/bathos/query.py` |
| Modify | `src/bathos/cli.py` |
| Create | `src/bathos/sidecar.py` |
| Modify | `src/bathos/runner.py` |
| Create | `tests/test_sidecar.py` |
| Modify | `tests/test_runner.py` |

---

## Task 1: Add `outcome` field to `Run` dataclass

**Files:**
- Modify: `src/bathos/schema.py`

- [ ] **Step 1.1: Write the failing tests**

```python
# tests/test_schema.py  (add to existing file)
def test_run_has_outcome_field():
    from bathos.schema import Run
    r = Run(project_slug="p", command="c", argv=["c"], git_hash="abc",
            git_branch="main", git_dirty=False)
    assert r.outcome == ""

def test_run_outcome_can_be_set():
    from bathos.schema import Run
    r = Run(project_slug="p", command="c", argv=["c"], git_hash="abc",
            git_branch="main", git_dirty=False, outcome="pass")
    assert r.outcome == "pass"

def test_run_to_arrow_includes_outcome():
    from bathos.schema import Run
    r = Run(project_slug="p", command="c", argv=["c"], git_hash="abc",
            git_branch="main", git_dirty=False, outcome="pass")
    tbl = r.to_arrow()
    assert "outcome" in tbl.schema.names
    assert tbl.column("outcome")[0].as_py() == "pass"
```

- [ ] **Step 1.2: Run test to verify it fails**

```bash
cd /home/marielle/projects/bathos
uv run pytest tests/test_schema.py::test_run_has_outcome_field -v
```

Expected: `FAILED` — `TypeError: Run.__init__() got an unexpected keyword argument 'outcome'`

- [ ] **Step 1.3: Add `outcome` to `COOL_SCHEMA`, `Run` dataclass, and `to_arrow()`**

In `src/bathos/schema.py`:

**1. Add `outcome` to `COOL_SCHEMA`** (after `hostname` field, around line 25):
```python
        pa.field("hostname", pa.string()),
        pa.field("outcome", pa.string()),
    ]
)
```

**2. Add `outcome` field to the `Run` dataclass** (after `metadata`, around line 74):
```python
    metadata: str = "{}"
    outcome: str = ""
```

**3. Update `Run.to_arrow()`** to include `outcome` in the table dict and pass against `COOL_SCHEMA`. Add after `"hostname": [self.hostname],`:
```python
                "hostname": [self.hostname],
                "outcome": [self.outcome],
```

- [ ] **Step 1.4: Run tests to verify they pass**

```bash
uv run pytest tests/test_schema.py::test_run_has_outcome_field tests/test_schema.py::test_run_outcome_can_be_set -v
```

Expected: `2 passed`

- [ ] **Step 1.5: Run full suite to catch regressions**

```bash
uv run pytest -x -q
```

Expected: all pass

- [ ] **Step 1.6: Commit**

```bash
git add src/bathos/schema.py tests/test_schema.py
git commit -m "feat(schema): add outcome field to Run dataclass"
```

---

## Task 2: Wire `outcome` through `_row_to_run` and `ls_cmd`

**Files:**
- Modify: `src/bathos/query.py`
- Modify: `src/bathos/cli.py`

- [ ] **Step 2.1: Write failing test for `_row_to_run` outcome**

```python
# tests/test_query.py  (add to existing file)
def test_list_runs_includes_outcome(tmp_path):
    """Runs compacted into warm DuckDB expose outcome field via list_runs."""
    import duckdb
    from bathos.compact import compact
    from bathos.query import list_runs
    from bathos.schema import Run
    from bathos.catalog import write_run

    r = Run(project_slug="proj", command="echo hi", argv=["echo", "hi"],
            git_hash="abc", git_branch="main", git_dirty=False,
            status="completed", exit_code=0)
    write_run(r, tmp_path)
    compact(tmp_path)

    # Manually set outcome in DuckDB
    db_path = tmp_path / "bathos.db"
    con = duckdb.connect(str(db_path))
    con.execute(f"UPDATE runs SET outcome = 'pass' WHERE id = '{r.id}'")
    con.close()

    runs = list_runs(tmp_path)
    assert len(runs) == 1
    assert runs[0].outcome == "pass"
```

- [ ] **Step 2.2: Run test to verify it fails**

```bash
uv run pytest tests/test_query.py::test_list_runs_includes_outcome -v
```

Expected: `FAILED` — `AssertionError: assert '' == 'pass'`

- [ ] **Step 2.3: Wire outcome in `_row_to_run`**

In `src/bathos/query.py`, find the `Run(...)` call inside `_row_to_run` and add `outcome`:

```python
        return Run(
            id=id_,
            project_slug=project_slug,
            command=command,
            argv=argv if argv else [],
            git_hash=git_hash,
            git_branch=git_branch,
            git_dirty=git_dirty,
            timestamp=timestamp,
            duration_s=duration_s,
            exit_code=exit_code,
            status=status,
            output_paths=output_paths if output_paths else [],
            tags=tags if tags else [],
            schema_version=schema_version,
            slurm_job_id=slurm_job_id if slurm_job_id else "",
            hostname=hostname if hostname else "",
            metadata=metadata if metadata else "{}",
            outcome=outcome if outcome else "",
        )
```

- [ ] **Step 2.4: Write failing test for `ls` outcome column**

```python
# tests/test_cli.py  (add to existing file)
def test_ls_shows_outcome_column(runner, tmp_path, monkeypatch):
    """ls output includes OUTCOME column header."""
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path))
    monkeypatch.setenv("BTH_PROJECT_SLUG", "proj")
    from bathos.schema import Run
    from bathos.catalog import write_run
    from bathos.compact import compact
    import duckdb

    r = Run(project_slug="proj", command="echo hi", argv=["echo", "hi"],
            git_hash="abc", git_branch="main", git_dirty=False,
            status="completed", exit_code=0)
    write_run(r, tmp_path)
    compact(tmp_path)

    con = duckdb.connect(str(tmp_path / "bathos.db"))
    con.execute(f"UPDATE runs SET outcome = 'pass' WHERE id = '{r.id}'")
    con.close()

    from bathos.cli import app
    result = runner.invoke(app, ["ls"])
    assert "OUTCOME" in result.output
    assert "pass" in result.output
```

- [ ] **Step 2.5: Run test to verify it fails**

```bash
uv run pytest tests/test_cli.py::test_ls_shows_outcome_column -v
```

Expected: `FAILED` — `AssertionError: assert 'OUTCOME' in ...`

- [ ] **Step 2.6: Add OUTCOME column to `ls_cmd` in `cli.py`**

Replace the header and row format in `ls_cmd`:

```python
    header = f"{'ID':38} {'PROJECT':12} {'STATUS':10} {'EXIT':5} {'OUTCOME':10} {'DURATION':8} COMMAND"
    typer.echo(header)
    typer.echo("-" * len(header))
    for r in runs:
        outcome_str = r.outcome if r.outcome else "-"
        typer.echo(
            f"{r.id:38} {r.project_slug:12} {r.status:10} {r.exit_code:5} "
            f"{outcome_str:10} {r.duration_s:7.1f}s {r.command[:40]}"
        )
```

- [ ] **Step 2.7: Run tests to verify they pass**

```bash
uv run pytest tests/test_query.py::test_list_runs_includes_outcome tests/test_cli.py::test_ls_shows_outcome_column -v
```

Expected: `2 passed`

- [ ] **Step 2.8: Run full suite**

```bash
uv run pytest -x -q
```

Expected: all pass

- [ ] **Step 2.9: Commit**

```bash
git add src/bathos/query.py src/bathos/cli.py tests/test_query.py tests/test_cli.py
git commit -m "feat(ls): wire outcome column through query layer and display in bth ls"
```

---

## Task 3: Create `sidecar.py` — parse and validate `.bth.toml`

**Files:**
- Create: `src/bathos/sidecar.py`
- Create: `tests/test_sidecar.py`

- [ ] **Step 3.1: Write failing tests for sidecar parsing**

Create `tests/test_sidecar.py`:

```python
import textwrap
from pathlib import Path
import pytest


def _write_toml(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "run_test.bth.toml"
    p.write_text(textwrap.dedent(content))
    return p


def test_parse_experiment_sidecar(tmp_path):
    from bathos.sidecar import parse_sidecar, SidecarKind
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "NVT maintains ±5K over 50ps"
        [outcomes.pass]
        condition = "temp_std < 5"
        decision = "proceed"
        [outcomes.fail]
        condition = "temp_std >= 5"
        decision = "debug"
        [result_schema]
        temp_std = "float"
    """)
    s = parse_sidecar(path)
    assert s.kind == SidecarKind.EXPERIMENT
    assert s.hypothesis == "NVT maintains ±5K over 50ps"
    assert "pass" in s.outcomes
    assert s.outcomes["pass"].condition == "temp_std < 5"
    assert s.result_schema == {"temp_std": "float"}


def test_parse_benchmark_sidecar(tmp_path):
    from bathos.sidecar import parse_sidecar, SidecarKind
    path = _write_toml(tmp_path, """
        [benchmark]
        baseline_ref = "run_abc123"
        metric = "ns_per_day"
        regression_threshold = 0.05
        target = "> 50 ns/day"
        [result_schema]
        ns_per_day = "float"
    """)
    s = parse_sidecar(path)
    assert s.kind == SidecarKind.BENCHMARK
    assert s.baseline_ref == "run_abc123"
    assert s.regression_threshold == 0.05


def test_parse_sidecar_invalid_toml(tmp_path):
    from bathos.sidecar import SidecarError
    path = tmp_path / "run_test.bth.toml"
    path.write_text("not valid toml ][[[")
    with pytest.raises(SidecarError, match="Failed to parse"):
        from bathos.sidecar import parse_sidecar
        parse_sidecar(path)


def test_find_sidecar_found(tmp_path):
    from bathos.sidecar import find_sidecar
    script = tmp_path / "run_nvt.py"
    script.touch()
    sidecar = tmp_path / "run_nvt.bth.toml"
    sidecar.write_text("[experiment]\nhypothesis='h'\n[result_schema]\n")
    assert find_sidecar(script) == sidecar


def test_find_sidecar_missing(tmp_path):
    from bathos.sidecar import find_sidecar
    script = tmp_path / "run_nvt.py"
    script.touch()
    assert find_sidecar(script) is None


def test_is_in_enforced_dir_true(tmp_path):
    from bathos.sidecar import is_in_enforced_dir
    script = tmp_path / "scripts" / "experiments" / "run_nvt.py"
    script.parent.mkdir(parents=True)
    script.touch()
    assert is_in_enforced_dir(script) is True


def test_is_in_enforced_dir_false(tmp_path):
    from bathos.sidecar import is_in_enforced_dir
    script = tmp_path / "scripts" / "scratch" / "explore_data.py"
    script.parent.mkdir(parents=True)
    script.touch()
    assert is_in_enforced_dir(script) is False


def test_evaluate_outcome_pass(tmp_path):
    from bathos.sidecar import parse_sidecar, evaluate_outcome
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "h"
        [outcomes.pass]
        condition = "temp_std < 5"
        decision = "proceed"
        [outcomes.fail]
        condition = "temp_std >= 5"
        decision = "debug"
        [result_schema]
        temp_std = "float"
    """)
    s = parse_sidecar(path)
    label = evaluate_outcome(s, {"temp_std": 2.1})
    assert label == "pass"


def test_evaluate_outcome_no_match(tmp_path):
    from bathos.sidecar import parse_sidecar, evaluate_outcome
    path = _write_toml(tmp_path, """
        [experiment]
        hypothesis = "h"
        [outcomes.pass]
        condition = "temp_std < 5"
        decision = "proceed"
        [result_schema]
        temp_std = "float"
    """)
    s = parse_sidecar(path)
    # value that matches no condition (shouldn't happen in well-formed sidecars, but be safe)
    label = evaluate_outcome(s, {})
    assert label == "unknown"


def test_evaluate_outcome_bool_result(tmp_path):
    from bathos.sidecar import parse_sidecar, evaluate_outcome
    path = _write_toml(tmp_path, """
        [debug]
        symptom = "NaN forces"
        suspected_cause = "PME grid"
        verification = "compare box sizes"
        [outcomes.reproduced]
        condition = "reproduced = TRUE"
        decision = "confirmed bug"
        [outcomes.not_reproduced]
        condition = "reproduced = FALSE"
        decision = "environment issue"
        [verdict_schema]
        reproduced = "bool"
    """)
    s = parse_sidecar(path)
    label = evaluate_outcome(s, {"reproduced": True})
    assert label == "reproduced"
```

- [ ] **Step 3.2: Run tests to verify they fail**

```bash
uv run pytest tests/test_sidecar.py -v
```

Expected: `ERROR` — `ModuleNotFoundError: No module named 'bathos.sidecar'`

- [ ] **Step 3.3: Create `src/bathos/sidecar.py`**

```python
from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import duckdb


class SidecarError(Exception):
    pass


class SidecarKind(str, Enum):
    EXPERIMENT = "experiment"
    BENCHMARK = "benchmark"
    VALIDATION = "validation"
    DEBUG = "debug"


@dataclass
class OutcomeSpec:
    condition: str
    decision: str


@dataclass
class Sidecar:
    kind: SidecarKind
    result_schema: dict[str, str]
    outcomes: dict[str, OutcomeSpec] = field(default_factory=dict)
    # experiment fields
    hypothesis: str = ""
    # benchmark fields
    baseline_ref: str = ""
    metric: str = ""
    regression_threshold: float = 0.0
    target: str = ""
    # validation fields
    property: str = ""
    reference: str = ""
    tolerance: str = ""
    # debug fields
    symptom: str = ""
    suspected_cause: str = ""
    verification: str = ""


ENFORCED_DIRS = {"experiments", "benchmarks", "validation"}


def parse_sidecar(path: Path) -> Sidecar:
    try:
        data = tomllib.loads(path.read_text())
    except Exception as e:
        raise SidecarError(f"Failed to parse {path}: {e}") from e

    if "experiment" in data:
        kind = SidecarKind.EXPERIMENT
        section = data["experiment"]
        outcomes = _parse_outcomes(data)
        return Sidecar(
            kind=kind,
            hypothesis=section.get("hypothesis", ""),
            outcomes=outcomes,
            result_schema=data.get("result_schema", {}),
        )
    elif "benchmark" in data:
        kind = SidecarKind.BENCHMARK
        section = data["benchmark"]
        return Sidecar(
            kind=kind,
            baseline_ref=section.get("baseline_ref", ""),
            metric=section.get("metric", ""),
            regression_threshold=section.get("regression_threshold", 0.0),
            target=section.get("target", ""),
            result_schema=data.get("result_schema", {}),
        )
    elif "validation" in data:
        kind = SidecarKind.VALIDATION
        section = data["validation"]
        outcomes = _parse_outcomes(data)
        return Sidecar(
            kind=kind,
            property=section.get("property", ""),
            reference=section.get("reference", ""),
            tolerance=section.get("tolerance", ""),
            outcomes=outcomes,
            result_schema=data.get("result_schema", {}),
        )
    elif "debug" in data:
        kind = SidecarKind.DEBUG
        section = data["debug"]
        return Sidecar(
            kind=kind,
            symptom=section.get("symptom", ""),
            suspected_cause=section.get("suspected_cause", ""),
            verification=section.get("verification", ""),
            result_schema=data.get("result_schema", {}),
        )
    else:
        raise SidecarError(
            f"{path}: must have one of [experiment], [benchmark], [validation], [debug] sections"
        )


def _parse_outcomes(data: dict) -> dict[str, OutcomeSpec]:
    outcomes_data = data.get("outcomes", {})
    return {
        label: OutcomeSpec(
            condition=spec.get("condition", ""),
            decision=spec.get("decision", ""),
        )
        for label, spec in outcomes_data.items()
    }


def find_sidecar(script_path: Path) -> Path | None:
    """Return the .bth.toml adjacent to script_path, or None if absent."""
    candidate = script_path.parent / f"{script_path.stem}.bth.toml"
    return candidate if candidate.exists() else None


def is_in_enforced_dir(script_path: Path) -> bool:
    """Return True if script is inside a directory name in ENFORCED_DIRS."""
    return any(part in ENFORCED_DIRS for part in script_path.parts)


def evaluate_outcome(sidecar: Sidecar, result: dict) -> str:
    """Evaluate DuckDB SQL fragments against result dict; return matching label or 'unknown'."""
    if not sidecar.outcomes or not result:
        return "unknown"

    def _sql_literal(v: object, k: str) -> str:
        if isinstance(v, bool):
            return f"{'TRUE' if v else 'FALSE'} AS {k}"
        if isinstance(v, float):
            return f"{v!r}::DOUBLE AS {k}"
        return f"{v!r} AS {k}"

    cols = ", ".join(_sql_literal(v, k) for k, v in result.items())
    for label, spec in sidecar.outcomes.items():
        try:
            rows = duckdb.execute(f"SELECT ({spec.condition}) FROM (SELECT {cols})").fetchall()
            if rows and rows[0][0]:
                return label
        except Exception:
            continue
    return "unknown"
```

- [ ] **Step 3.4: Run tests to verify they pass**

```bash
uv run pytest tests/test_sidecar.py -v
```

Expected: `9 passed`

- [ ] **Step 3.5: Run full suite**

```bash
uv run pytest -x -q
```

Expected: all pass

- [ ] **Step 3.6: Commit**

```bash
git add src/bathos/sidecar.py tests/test_sidecar.py
git commit -m "feat(sidecar): add sidecar parser, enforced-dir check, and outcome evaluator"
```

---

## Task 4: Pre-registration enforcement in `runner.py`

**Files:**
- Modify: `src/bathos/runner.py`
- Modify: `tests/test_runner.py`

- [ ] **Step 4.1: Write failing tests**

Add to `tests/test_runner.py`:

```python
def test_run_blocks_enforced_dir_without_sidecar(tmp_path, monkeypatch):
    """bth run must raise SystemExit(1) if script in enforced dir has no sidecar."""
    import subprocess, sys
    from bathos.runner import run_script

    enforced = tmp_path / "scripts" / "experiments"
    enforced.mkdir(parents=True)
    script = enforced / "run_nvt.py"
    script.write_text("print('hi')")
    # No sidecar present

    result = run_script(
        argv=[sys.executable, str(script)],
        project_slug="proj",
        catalog_dir=tmp_path / "catalog",
        output_paths=[],
        tags=[],
        cwd=tmp_path,
    )
    assert result == 1  # blocked


def test_run_allows_enforced_dir_with_sidecar(tmp_path):
    """bth run proceeds if script in enforced dir has a valid sidecar."""
    import sys, textwrap
    from bathos.runner import run_script

    (tmp_path / "catalog").mkdir()
    enforced = tmp_path / "scripts" / "experiments"
    enforced.mkdir(parents=True)
    script = enforced / "run_nvt.py"
    script.write_text("print('hi')")
    sidecar = enforced / "run_nvt.bth.toml"
    sidecar.write_text(textwrap.dedent("""
        [experiment]
        hypothesis = "test"
        [result_schema]
        x = "float"
    """))

    result = run_script(
        argv=[sys.executable, str(script)],
        project_slug="proj",
        catalog_dir=tmp_path / "catalog",
        output_paths=[],
        tags=[],
        cwd=tmp_path,
    )
    assert result == 0


def test_run_skips_enforcement_for_scratch(tmp_path):
    """bth run does not enforce sidecars for scripts/scratch/."""
    import sys
    from bathos.runner import run_script

    (tmp_path / "catalog").mkdir()
    scratch = tmp_path / "scripts" / "scratch"
    scratch.mkdir(parents=True)
    script = scratch / "explore.py"
    script.write_text("print('hi')")
    # No sidecar — should still run

    result = run_script(
        argv=[sys.executable, str(script)],
        project_slug="proj",
        catalog_dir=tmp_path / "catalog",
        output_paths=[],
        tags=[],
        cwd=tmp_path,
    )
    assert result == 0
```

- [ ] **Step 4.2: Run tests to verify they fail**

```bash
uv run pytest tests/test_runner.py::test_run_blocks_enforced_dir_without_sidecar -v
```

Expected: `FAILED` — test expects `result == 1` but runner doesn't check sidecars yet

- [ ] **Step 4.3a: Establish baseline before replacing runner.py**

```bash
uv run pytest tests/test_runner.py -v --tb=short 2>&1 | tee /tmp/runner_baseline.txt
grep -E "PASSED|FAILED|ERROR" /tmp/runner_baseline.txt
```

Record the number of passing tests. The replacement must not reduce this count.

- [ ] **Step 4.3b: Read current `src/bathos/runner.py` and confirm `run_script` signature**

```bash
grep -n "^def run_script" /home/marielle/projects/bathos/src/bathos/runner.py
```

Expected: matches `def run_script(argv, project_slug, catalog_dir, output_paths, tags, cwd)`. If the signature differs, update the replacement below to match.

- [ ] **Step 4.3: Update `runner.py` to enforce sidecars**

Replace `src/bathos/runner.py` content:

```python
from __future__ import annotations

import dataclasses
import os
import subprocess
import time
from pathlib import Path

import typer

from bathos.catalog import write_run
from bathos.git import capture_git_state
from bathos.schema import Run
from bathos.sidecar import find_sidecar, is_in_enforced_dir, parse_sidecar, evaluate_outcome, SidecarError


def run_script(
    argv: list[str],
    project_slug: str,
    catalog_dir: Path,
    output_paths: list[str],
    tags: list[str],
    cwd: Path = Path.cwd(),
) -> int:
    script_path = Path(argv[0]).resolve()

    # Pre-registration enforcement
    sidecar_path = find_sidecar(script_path)
    sidecar = None
    if is_in_enforced_dir(script_path):
        if sidecar_path is None:
            typer.echo(
                f"Error: {script_path.name} is in an enforced directory "
                f"({script_path.parent.name}/) but has no sidecar.\n"
                f"Create {script_path.stem}.bth.toml next to the script before running.",
                err=True,
            )
            return 1
        try:
            sidecar = parse_sidecar(sidecar_path)
        except SidecarError as e:
            typer.echo(f"Error: invalid sidecar — {e}", err=True)
            return 1

    git = capture_git_state(cwd)
    run = Run(
        project_slug=project_slug,
        command=" ".join(argv),
        argv=argv,
        git_hash=git.hash,
        git_branch=git.branch,
        git_dirty=git.dirty,
        output_paths=output_paths,
        tags=tags,
        status="running",
        slurm_job_id=os.environ.get("SLURM_JOB_ID", ""),
    )
    catalog_dir.mkdir(parents=True, exist_ok=True)
    write_run(run, catalog_dir)

    start = time.monotonic()
    try:
        result = subprocess.run(argv, cwd=cwd)
        exit_code = result.returncode
        status = "completed" if exit_code == 0 else "failed"
    except KeyboardInterrupt:
        exit_code = 130
        status = "killed"

    outcome = ""
    if sidecar is not None:
        # Outcome evaluation: read result_schema fields from metadata
        import json
        try:
            meta = json.loads(run.metadata)
        except (json.JSONDecodeError, TypeError):
            meta = {}
        outcome = evaluate_outcome(sidecar, meta)

    run = dataclasses.replace(
        run,
        duration_s=time.monotonic() - start,
        exit_code=exit_code,
        status=status,
        outcome=outcome,
    )
    write_run(run, catalog_dir)
    return exit_code
```

- [ ] **Step 4.4: Run tests to verify they pass**

```bash
uv run pytest tests/test_runner.py -v
```

Expected: all pass

- [ ] **Step 4.5: Run full suite**

```bash
uv run pytest -x -q
```

Expected: all pass

- [ ] **Step 4.6: Commit**

```bash
git add src/bathos/runner.py tests/test_runner.py
git commit -m "feat(runner): enforce pre-registration sidecars for experiments/benchmarks/validation"
```

---

## Final Verification

- [ ] **Run full test suite and confirm count**

```bash
uv run pytest -v --tb=short 2>&1 | tail -5
```

Expected: `N passed` where N ≥ 59

- [ ] **Smoke test CLI**

```bash
cd /tmp && mkdir bth_smoke && cd bth_smoke
BTH_PROJECT_SLUG=smoke BTH_CATALOG_DIR=/tmp/bth_smoke_cat uv run --project /home/marielle/projects/bathos bth ls
```

Expected: `No runs found.` (clean catalog) with OUTCOME column in header if runs existed
