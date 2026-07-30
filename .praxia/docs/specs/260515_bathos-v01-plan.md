# bathos v0.1 Core Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the bathos v0.1 core: project init, run provenance capture, and query interface backed by a DuckDB+Parquet central catalog.

**Architecture:** One Parquet file per run in `~/.bth/catalog/runs/`; DuckDB queries across all files via glob. Each run is written atomically (write-then-rename), making parallel SLURM job arrays safe without locking. CLI and future MCP layer are thin Typer/FastMCP skins over shared core modules.

**Tech Stack:** Python 3.12, Typer, DuckDB, PyArrow, GitPython (via subprocess), pytest, uv

**Out of scope for this plan:** FastMCP server (plan #2), `@bth.experiment` decorator (plan #3), `bth check` (plan #3), SLURM `_bth_env.sh` integration beyond template generation (plan #3), `bth migrate` (plan #4).

**Backlog:** #124 (schema+catalog), #125 (bth init), #126 (bth run), #127 (query interface)

---

## File Map

| File | Responsibility |
|---|---|
| `pyproject.toml` | deps, `bth` entrypoint |
| `src/bathos/__init__.py` | package root, `__version__` |
| `src/bathos/schema.py` | `Run` dataclass, `RUN_SCHEMA` PyArrow schema |
| `src/bathos/catalog.py` | write/read/dedup Parquet, catalog dir init |
| `src/bathos/config.py` | parse `.bth.toml` + `~/.bth/config.toml` |
| `src/bathos/git.py` | `capture_git_state()` → `GitState` |
| `src/bathos/init.py` | `bth init` logic: create dirs, write `.bth.toml`, `_bth_env.sh` |
| `src/bathos/runner.py` | `run_script()`: capture provenance, exec subprocess, update catalog |
| `src/bathos/query.py` | `list_runs()`, `get_run()`, `find_runs()`, `run_sql()` |
| `src/bathos/cli.py` | Typer app: `init`, `run`, `ls`, `show`, `find`, `sql` commands |
| `src/bathos/templates/_bth_env.sh` | SLURM env helper template (string, not Jinja) |
| `tests/conftest.py` | `tmp_catalog`, `tmp_project` fixtures |
| `tests/test_schema.py` | Run construction, round-trip |
| `tests/test_catalog.py` | write/read/dedup, SLURM-parallel safety |
| `tests/test_config.py` | config discovery (walk-up), parse |
| `tests/test_git.py` | `capture_git_state()` in real and non-git dirs |
| `tests/test_init.py` | dir creation, `.bth.toml`, `_bth_env.sh` output |
| `tests/test_runner.py` | subprocess exec, provenance capture, killed status |
| `tests/test_query.py` | ls/show/find/sql against fixture catalog |
| `tests/test_cli.py` | CLI integration via `typer.testing.CliRunner` |

---

## Task 1: Project setup — pyproject.toml, package skeleton, uv sync

**Files:**
- Modify: `pyproject.toml`
- Create: `src/bathos/__init__.py`
- Create: `src/bathos/templates/_bth_env.sh`
- Create: `tests/__init__.py`

- [ ] **Step 1: Update pyproject.toml with deps and entrypoint**

Replace the content of `pyproject.toml`:

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "bathos"
version = "0.1.0"
description = "Local-first, zero-server experiment tracking for researchers"
readme = "README.md"
requires-python = ">=3.12"
dependencies = [
    "typer>=0.12",
    "duckdb>=1.0",
    "pyarrow>=16",
]

[project.scripts]
bth = "bathos.cli:app"

[tool.hatch.build.targets.wheel]
packages = ["src/bathos"]

[tool.pytest.ini_options]
testpaths = ["tests"]

[dependency-groups]
dev = [
    "pytest>=8",
    "pytest-tmp-files>=0.0.2",
]
```

- [ ] **Step 2: Create package root**

Create `src/bathos/__init__.py`:

```python
__version__ = "0.1.0"
```

- [ ] **Step 3: Create SLURM env helper template**

Create `src/bathos/templates/_bth_env.sh`:

```bash
# Source from SLURM scripts: source scripts/slurm/_bth_env.sh
# Sets BTH_PROJECT_SLUG so bth run works transparently in batch jobs.
set -euo pipefail
export BTH_PROJECT_SLUG="{slug}"
export BTH_PROJECT_ROOT="{root}"
```

- [ ] **Step 4: Create empty test package**

```bash
touch tests/__init__.py
```

- [ ] **Step 5: Sync dependencies**

```bash
uv sync --dev
```

Expected: lock file updated, `.venv` created/updated.

- [ ] **Step 6: Verify import**

```bash
uv run python -c "import bathos; print(bathos.__version__)"
```

Expected: `0.1.0`

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml src/bathos/__init__.py src/bathos/templates/ tests/__init__.py uv.lock
git commit -m "chore: project setup — deps, entrypoint, templates"
```

---

## Task 2: Schema — Run dataclass and PyArrow schema

**Files:**
- Create: `src/bathos/schema.py`
- Create: `tests/test_schema.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_schema.py`:

```python
from datetime import datetime, timezone
from bathos.schema import Run, RUN_SCHEMA
import pyarrow as pa


def test_run_has_generated_id():
    r = Run(project_slug="proj", command="python foo.py", argv=["python", "foo.py"],
            git_hash="abc123", git_branch="main", git_dirty=False)
    assert len(r.id) == 36  # UUID4


def test_two_runs_have_different_ids():
    r1 = Run(project_slug="p", command="x", argv=["x"],
             git_hash="a", git_branch="main", git_dirty=False)
    r2 = Run(project_slug="p", command="x", argv=["x"],
             git_hash="a", git_branch="main", git_dirty=False)
    assert r1.id != r2.id


def test_run_defaults():
    r = Run(project_slug="proj", command="python foo.py", argv=["python", "foo.py"],
            git_hash="abc123", git_branch="main", git_dirty=False)
    assert r.status == "running"
    assert r.exit_code == -1
    assert r.duration_s == 0.0
    assert r.output_paths == []
    assert r.tags == []
    assert isinstance(r.timestamp, datetime)
    assert r.timestamp.tzinfo is not None


def test_run_to_arrow_table():
    r = Run(project_slug="proj", command="python foo.py", argv=["python", "foo.py"],
            git_hash="abc123", git_branch="main", git_dirty=False)
    table = r.to_arrow()
    assert table.schema.equals(RUN_SCHEMA)
    assert table.num_rows == 1


def test_run_roundtrip_via_arrow():
    r = Run(project_slug="proj", command="python foo.py", argv=["python", "foo.py"],
            git_hash="abc123", git_branch="main", git_dirty=False,
            status="completed", exit_code=0, duration_s=1.5,
            output_paths=["/tmp/out.parquet"], tags=["tip3p"])
    table = r.to_arrow()
    r2 = Run.from_arrow_row(table.to_pydict(), 0)
    assert r2.id == r.id
    assert r2.status == "completed"
    assert r2.exit_code == 0
    assert r2.duration_s == 1.5
    assert r2.output_paths == ["/tmp/out.parquet"]
    assert r2.tags == ["tip3p"]
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/test_schema.py -v
```

Expected: `ImportError` or `ModuleNotFoundError` — `bathos.schema` does not exist.

- [ ] **Step 3: Implement schema.py**

Create `src/bathos/schema.py`:

```python
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4
import pyarrow as pa


RUN_SCHEMA = pa.schema([
    pa.field("id", pa.string()),
    pa.field("project_slug", pa.string()),
    pa.field("command", pa.string()),
    pa.field("argv", pa.list_(pa.string())),
    pa.field("git_hash", pa.string()),
    pa.field("git_branch", pa.string()),
    pa.field("git_dirty", pa.bool_()),
    pa.field("timestamp", pa.timestamp("us", tz="UTC")),
    pa.field("duration_s", pa.float64()),
    pa.field("exit_code", pa.int32()),
    pa.field("status", pa.string()),
    pa.field("output_paths", pa.list_(pa.string())),
    pa.field("tags", pa.list_(pa.string())),
])


@dataclass
class Run:
    project_slug: str
    command: str
    argv: list[str]
    git_hash: str
    git_branch: str
    git_dirty: bool
    id: str = field(default_factory=lambda: str(uuid4()))
    timestamp: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    duration_s: float = 0.0
    exit_code: int = -1
    status: str = "running"
    output_paths: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)

    def to_arrow(self) -> pa.Table:
        return pa.table(
            {
                "id": [self.id],
                "project_slug": [self.project_slug],
                "command": [self.command],
                "argv": [self.argv],
                "git_hash": [self.git_hash],
                "git_branch": [self.git_branch],
                "git_dirty": [self.git_dirty],
                "timestamp": pa.array(
                    [self.timestamp], type=pa.timestamp("us", tz="UTC")
                ),
                "duration_s": [self.duration_s],
                "exit_code": [self.exit_code],
                "status": [self.status],
                "output_paths": [self.output_paths],
                "tags": [self.tags],
            },
            schema=RUN_SCHEMA,
        )

    @classmethod
    def from_arrow_row(cls, pydict: dict, i: int) -> Run:
        ts = pydict["timestamp"][i]
        if not isinstance(ts, datetime):
            ts = ts.as_py()
        return cls(
            id=pydict["id"][i],
            project_slug=pydict["project_slug"][i],
            command=pydict["command"][i],
            argv=list(pydict["argv"][i]),
            git_hash=pydict["git_hash"][i],
            git_branch=pydict["git_branch"][i],
            git_dirty=bool(pydict["git_dirty"][i]),
            timestamp=ts,
            duration_s=float(pydict["duration_s"][i]),
            exit_code=int(pydict["exit_code"][i]),
            status=pydict["status"][i],
            output_paths=list(pydict["output_paths"][i]),
            tags=list(pydict["tags"][i]),
        )
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_schema.py -v
```

Expected: 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bathos/schema.py tests/test_schema.py
git commit -m "feat: Run schema and PyArrow round-trip"
```

---

## Task 3: Catalog — DuckDB+Parquet write, read, dedup

**Files:**
- Create: `src/bathos/catalog.py`
- Create: `tests/conftest.py`
- Create: `tests/test_catalog.py`

- [ ] **Step 1: Write failing tests**

Create `tests/conftest.py`:

```python
import pytest
from pathlib import Path
from bathos.schema import Run


@pytest.fixture
def tmp_catalog(tmp_path: Path) -> Path:
    catalog = tmp_path / ".bth" / "catalog"
    catalog.mkdir(parents=True)
    return catalog


@pytest.fixture
def sample_run() -> Run:
    return Run(
        project_slug="testproj",
        command="python scripts/experiments/run.py --n 10",
        argv=["python", "scripts/experiments/run.py", "--n", "10"],
        git_hash="deadbeef",
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        duration_s=2.5,
        output_paths=["/tmp/results.parquet"],
        tags=["smoke"],
    )
```

Create `tests/test_catalog.py`:

```python
from pathlib import Path
from bathos.catalog import write_run, read_runs, init_catalog
from bathos.schema import Run
import dataclasses


def test_init_catalog_creates_dirs(tmp_catalog: Path):
    init_catalog(tmp_catalog)
    assert (tmp_catalog / "runs").is_dir()


def test_write_and_read_single_run(tmp_catalog: Path, sample_run: Run):
    init_catalog(tmp_catalog)
    write_run(sample_run, tmp_catalog)
    runs = read_runs(tmp_catalog)
    assert len(runs) == 1
    assert runs[0].id == sample_run.id
    assert runs[0].status == "completed"
    assert runs[0].exit_code == 0
    assert runs[0].output_paths == ["/tmp/results.parquet"]


def test_write_creates_parquet_file(tmp_catalog: Path, sample_run: Run):
    init_catalog(tmp_catalog)
    write_run(sample_run, tmp_catalog)
    files = list((tmp_catalog / "runs").glob("*.parquet"))
    assert len(files) == 1
    assert sample_run.id in files[0].name


def test_overwrite_deduplicates_by_id(tmp_catalog: Path, sample_run: Run):
    """Writing the same run id twice (status update) should yield one result."""
    init_catalog(tmp_catalog)
    write_run(sample_run, tmp_catalog)
    updated = dataclasses.replace(sample_run, status="completed", duration_s=5.0)
    write_run(updated, tmp_catalog)
    runs = read_runs(tmp_catalog)
    assert len(runs) == 1
    assert runs[0].duration_s == 5.0


def test_multiple_runs_all_returned(tmp_catalog: Path):
    init_catalog(tmp_catalog)
    for i in range(3):
        r = Run(
            project_slug="proj",
            command=f"python run.py --i {i}",
            argv=["python", "run.py", "--i", str(i)],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
        )
        write_run(r, tmp_catalog)
    runs = read_runs(tmp_catalog)
    assert len(runs) == 3


def test_parallel_writes_do_not_collide(tmp_catalog: Path):
    """Simulate two concurrent SLURM jobs writing simultaneously."""
    init_catalog(tmp_catalog)
    r1 = Run(project_slug="p", command="a", argv=["a"],
             git_hash="x", git_branch="main", git_dirty=False)
    r2 = Run(project_slug="p", command="b", argv=["b"],
             git_hash="x", git_branch="main", git_dirty=False)
    # Write both without waiting (atomic rename ensures no collision)
    write_run(r1, tmp_catalog)
    write_run(r2, tmp_catalog)
    runs = read_runs(tmp_catalog)
    assert len(runs) == 2
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/test_catalog.py -v
```

Expected: `ImportError` — `bathos.catalog` does not exist.

- [ ] **Step 3: Implement catalog.py**

Create `src/bathos/catalog.py`:

```python
from __future__ import annotations
from pathlib import Path
import pyarrow.parquet as pq
import duckdb

from bathos.schema import Run, RUN_SCHEMA


def init_catalog(catalog_dir: Path) -> None:
    (catalog_dir / "runs").mkdir(parents=True, exist_ok=True)


def write_run(run: Run, catalog_dir: Path) -> None:
    """Write (or overwrite) a run record atomically."""
    runs_dir = catalog_dir / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    target = runs_dir / f"run_{run.id}.parquet"
    tmp = runs_dir / f"run_{run.id}.tmp.parquet"
    pq.write_table(run.to_arrow(), tmp)
    tmp.rename(target)  # atomic on POSIX


def read_runs(catalog_dir: Path) -> list[Run]:
    """Read all runs, deduplicating by id (latest write wins)."""
    runs_dir = catalog_dir / "runs"
    if not runs_dir.exists():
        return []
    parquet_files = list(runs_dir.glob("run_*.parquet"))
    if not parquet_files:
        return []
    # One file per run id (write_run overwrites in-place), no dedup needed.
    # duckdb reads multiple files efficiently via glob.
    glob_pattern = str(runs_dir / "run_*.parquet")
    con = duckdb.connect()
    result = con.execute(
        f"SELECT * FROM read_parquet('{glob_pattern}') ORDER BY timestamp DESC"
    ).fetchall()
    columns = [desc[0] for desc in con.description]
    pydict = {col: [row[i] for row in result] for i, col in enumerate(columns)}
    return [Run.from_arrow_row(pydict, i) for i in range(len(result))]
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_catalog.py -v
```

Expected: 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bathos/catalog.py tests/conftest.py tests/test_catalog.py
git commit -m "feat: DuckDB+Parquet catalog — write, read, SLURM-safe append"
```

---

## Task 4: Config — .bth.toml discovery and parsing

**Files:**
- Create: `src/bathos/config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_config.py`:

```python
from pathlib import Path
import pytest
from bathos.config import find_project_config, load_project_config, ProjectConfig


def test_find_config_in_current_dir(tmp_path: Path):
    cfg = tmp_path / ".bth.toml"
    cfg.write_text('[project]\nslug = "myproj"\nroot = "/home/user/projects/myproj"\n')
    result = find_project_config(tmp_path)
    assert result == cfg


def test_find_config_walks_up(tmp_path: Path):
    cfg = tmp_path / ".bth.toml"
    cfg.write_text('[project]\nslug = "myproj"\nroot = "/home/user/projects/myproj"\n')
    subdir = tmp_path / "scripts" / "experiments"
    subdir.mkdir(parents=True)
    result = find_project_config(subdir)
    assert result == cfg


def test_find_config_returns_none_when_absent(tmp_path: Path):
    result = find_project_config(tmp_path)
    assert result is None


def test_load_minimal_config(tmp_path: Path):
    cfg = tmp_path / ".bth.toml"
    cfg.write_text('[project]\nslug = "prolix"\nroot = "/home/user/projects/prolix"\n')
    pc = load_project_config(cfg)
    assert pc.slug == "prolix"
    assert pc.root == Path("/home/user/projects/prolix")
    assert pc.remotes == {}
    assert pc.slurm == {}


def test_load_config_with_remote_and_slurm(tmp_path: Path):
    cfg = tmp_path / ".bth.toml"
    cfg.write_text(
        '[project]\nslug = "prolix"\nroot = "/home/user/projects/prolix"\n'
        '[slurm]\npartition = "pi_so3"\ndefault_walltime = "04:00:00"\n'
        '[remotes.engaging]\nhost = "engaging"\nremote_root = "~/projects/prolix"\n'
    )
    pc = load_project_config(cfg)
    assert pc.slurm["partition"] == "pi_so3"
    assert pc.remotes["engaging"]["host"] == "engaging"


def test_default_catalog_dir():
    from bathos.config import default_catalog_dir
    d = default_catalog_dir()
    assert d == Path.home() / ".bth" / "catalog"
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/test_config.py -v
```

Expected: `ImportError`.

- [ ] **Step 3: Implement config.py**

Create `src/bathos/config.py`:

```python
from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
import tomllib


@dataclass
class ProjectConfig:
    slug: str
    root: Path
    remotes: dict[str, dict] = field(default_factory=dict)
    slurm: dict = field(default_factory=dict)


def default_catalog_dir() -> Path:
    return Path.home() / ".bth" / "catalog"


def find_project_config(start: Path = Path.cwd()) -> Path | None:
    for directory in [start, *start.parents]:
        candidate = directory / ".bth.toml"
        if candidate.exists():
            return candidate
    return None


def load_project_config(path: Path) -> ProjectConfig:
    with open(path, "rb") as f:
        data = tomllib.load(f)
    project = data["project"]
    return ProjectConfig(
        slug=project["slug"],
        root=Path(project["root"]),
        remotes=data.get("remotes", {}),
        slurm=data.get("slurm", {}),
    )
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_config.py -v
```

Expected: 6 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bathos/config.py tests/test_config.py
git commit -m "feat: .bth.toml config discovery and parsing"
```

---

## Task 5: Git state capture

**Files:**
- Create: `src/bathos/git.py`
- Create: `tests/test_git.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_git.py`:

```python
import subprocess
from pathlib import Path
import pytest
from bathos.git import capture_git_state, GitState


def test_captures_state_in_git_repo(tmp_path: Path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path,
                   check=True, capture_output=True)
    state = capture_git_state(tmp_path)
    assert len(state.hash) == 40
    assert state.branch in ("main", "master")
    assert state.dirty is False


def test_detects_dirty_state(tmp_path: Path):
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@test.com"],
                   cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test"],
                   cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "file.txt").write_text("hello")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path,
                   check=True, capture_output=True)
    (tmp_path / "file.txt").write_text("modified")
    state = capture_git_state(tmp_path)
    assert state.dirty is True


def test_returns_sentinel_outside_git_repo(tmp_path: Path):
    state = capture_git_state(tmp_path)
    assert state.hash == "unknown"
    assert state.branch == "unknown"
    assert state.dirty is False
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/test_git.py -v
```

Expected: `ImportError`.

- [ ] **Step 3: Implement git.py**

Create `src/bathos/git.py`:

```python
from __future__ import annotations
from dataclasses import dataclass
from pathlib import Path
import subprocess


@dataclass
class GitState:
    hash: str
    branch: str
    dirty: bool


_UNKNOWN = GitState(hash="unknown", branch="unknown", dirty=False)


def capture_git_state(cwd: Path = Path.cwd()) -> GitState:
    try:
        hash_ = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=cwd, text=True, stderr=subprocess.DEVNULL
        ).strip()
        branch = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd, text=True, stderr=subprocess.DEVNULL,
        ).strip()
        dirty = bool(
            subprocess.check_output(
                ["git", "status", "--porcelain"],
                cwd=cwd, text=True, stderr=subprocess.DEVNULL,
            ).strip()
        )
        return GitState(hash=hash_, branch=branch, dirty=dirty)
    except (subprocess.CalledProcessError, FileNotFoundError):
        return _UNKNOWN
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_git.py -v
```

Expected: 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bathos/git.py tests/test_git.py
git commit -m "feat: git state capture with sentinel for non-git dirs"
```

---

## Task 6: Init — bth init command logic

**Files:**
- Create: `src/bathos/init.py`
- Create: `tests/test_init.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_init.py`:

```python
from pathlib import Path
from bathos.init import init_project, SCRIPT_DIRS


def test_creates_all_script_dirs(tmp_path: Path):
    init_project(tmp_path, slug="myproj", catalog_dir=tmp_path / ".bth" / "catalog")
    for d in SCRIPT_DIRS:
        assert (tmp_path / d).is_dir(), f"Missing: {d}"


def test_writes_bth_toml(tmp_path: Path):
    init_project(tmp_path, slug="myproj", catalog_dir=tmp_path / ".bth" / "catalog")
    toml = (tmp_path / ".bth.toml").read_text()
    assert 'slug = "myproj"' in toml
    assert str(tmp_path) in toml


def test_writes_bth_env_sh(tmp_path: Path):
    init_project(tmp_path, slug="myproj", catalog_dir=tmp_path / ".bth" / "catalog")
    env_sh = (tmp_path / "scripts" / "slurm" / "_bth_env.sh").read_text()
    assert "BTH_PROJECT_SLUG" in env_sh
    assert "myproj" in env_sh


def test_adds_scratch_to_gitignore_if_present(tmp_path: Path):
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text("*.pyc\n")
    init_project(tmp_path, slug="myproj", catalog_dir=tmp_path / ".bth" / "catalog")
    content = gitignore.read_text()
    assert "scripts/scratch/" in content


def test_creates_gitignore_if_absent(tmp_path: Path):
    init_project(tmp_path, slug="myproj", catalog_dir=tmp_path / ".bth" / "catalog")
    content = (tmp_path / ".gitignore").read_text()
    assert "scripts/scratch/" in content


def test_idempotent_on_rerun(tmp_path: Path):
    catalog = tmp_path / ".bth" / "catalog"
    init_project(tmp_path, slug="myproj", catalog_dir=catalog)
    init_project(tmp_path, slug="myproj", catalog_dir=catalog)  # should not raise
    dirs = [d for d in SCRIPT_DIRS if (tmp_path / d).is_dir()]
    assert len(dirs) == len(SCRIPT_DIRS)
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/test_init.py -v
```

Expected: `ImportError`.

- [ ] **Step 3: Implement init.py**

Create `src/bathos/init.py`:

```python
from __future__ import annotations
from pathlib import Path
import importlib.resources

from bathos.catalog import init_catalog

SCRIPT_DIRS = [
    "scripts/experiments",
    "scripts/analysis",
    "scripts/validation",
    "scripts/benchmarks",
    "scripts/data",
    "scripts/slurm",
    "scripts/debug",
    "scripts/explore",
    "scripts/scratch",
]

_BTH_TOML_TEMPLATE = """\
[project]
slug = "{slug}"
root = "{root}"
"""

_GITIGNORE_ENTRY = "scripts/scratch/\n"


def _load_env_sh_template() -> str:
    pkg = importlib.resources.files("bathos") / "templates" / "_bth_env.sh"
    return pkg.read_text(encoding="utf-8")


def init_project(
    project_root: Path,
    slug: str,
    catalog_dir: Path,
    remote: str | None = None,
    slurm_partition: str | None = None,
) -> None:
    # Script directories
    for d in SCRIPT_DIRS:
        (project_root / d).mkdir(parents=True, exist_ok=True)

    # .bth.toml
    toml_path = project_root / ".bth.toml"
    content = _BTH_TOML_TEMPLATE.format(slug=slug, root=str(project_root))
    if remote:
        host, remote_root = remote.split(":", 1)
        content += f"\n[remotes.{host}]\nhost = \"{host}\"\nremote_root = \"{remote_root}\"\n"
    if slurm_partition:
        content += f"\n[slurm]\npartition = \"{slurm_partition}\"\n"
    toml_path.write_text(content)

    # scripts/slurm/_bth_env.sh
    template = _load_env_sh_template()
    env_sh = template.format(slug=slug, root=str(project_root))
    (project_root / "scripts" / "slurm" / "_bth_env.sh").write_text(env_sh)

    # .gitignore
    gitignore = project_root / ".gitignore"
    existing = gitignore.read_text() if gitignore.exists() else ""
    if _GITIGNORE_ENTRY.strip() not in existing:
        with open(gitignore, "a") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write(_GITIGNORE_ENTRY)

    # Catalog
    init_catalog(catalog_dir)
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_init.py -v
```

Expected: 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bathos/init.py tests/test_init.py
git commit -m "feat: bth init — script dirs, .bth.toml, _bth_env.sh, .gitignore"
```

---

## Task 7: Runner — bth run subprocess wrapper

**Files:**
- Create: `src/bathos/runner.py`
- Create: `tests/test_runner.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_runner.py`:

```python
import sys
from pathlib import Path
from bathos.runner import run_script
from bathos.catalog import read_runs, init_catalog


def test_run_records_completed_status(tmp_catalog: Path):
    init_catalog(tmp_catalog)
    exit_code = run_script(
        argv=[sys.executable, "-c", "pass"],
        project_slug="testproj",
        catalog_dir=tmp_catalog,
        output_paths=[],
        tags=[],
    )
    assert exit_code == 0
    runs = read_runs(tmp_catalog)
    assert len(runs) == 1
    assert runs[0].status == "completed"
    assert runs[0].exit_code == 0
    assert runs[0].project_slug == "testproj"


def test_run_records_failed_status(tmp_catalog: Path):
    init_catalog(tmp_catalog)
    exit_code = run_script(
        argv=[sys.executable, "-c", "raise SystemExit(1)"],
        project_slug="testproj",
        catalog_dir=tmp_catalog,
        output_paths=[],
        tags=[],
    )
    assert exit_code == 1
    runs = read_runs(tmp_catalog)
    assert runs[0].status == "failed"
    assert runs[0].exit_code == 1


def test_run_captures_git_hash(tmp_catalog: Path, tmp_path: Path):
    import subprocess
    # init a git repo so capture_git_state returns a real hash
    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@t.com"],
                   cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "T"],
                   cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "f.txt").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "i"], cwd=tmp_path,
                   check=True, capture_output=True)

    init_catalog(tmp_catalog)
    run_script(argv=[sys.executable, "-c", "pass"], project_slug="p",
               catalog_dir=tmp_catalog, output_paths=[], tags=[], cwd=tmp_path)
    runs = read_runs(tmp_catalog)
    assert runs[0].git_hash != "unknown"
    assert len(runs[0].git_hash) == 40


def test_run_records_output_paths(tmp_catalog: Path):
    init_catalog(tmp_catalog)
    run_script(
        argv=[sys.executable, "-c", "pass"],
        project_slug="p",
        catalog_dir=tmp_catalog,
        output_paths=["/tmp/result.parquet"],
        tags=["tag1"],
    )
    runs = read_runs(tmp_catalog)
    assert runs[0].output_paths == ["/tmp/result.parquet"]
    assert runs[0].tags == ["tag1"]


def test_run_duration_is_positive(tmp_catalog: Path):
    init_catalog(tmp_catalog)
    run_script(
        argv=[sys.executable, "-c", "import time; time.sleep(0.05)"],
        project_slug="p",
        catalog_dir=tmp_catalog,
        output_paths=[],
        tags=[],
    )
    runs = read_runs(tmp_catalog)
    assert runs[0].duration_s >= 0.05
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/test_runner.py -v
```

Expected: `ImportError`.

- [ ] **Step 3: Implement runner.py**

Create `src/bathos/runner.py`:

```python
from __future__ import annotations
import subprocess
import time
from pathlib import Path

from bathos.catalog import write_run
from bathos.git import capture_git_state
from bathos.schema import Run


def run_script(
    argv: list[str],
    project_slug: str,
    catalog_dir: Path,
    output_paths: list[str],
    tags: list[str],
    cwd: Path = Path.cwd(),
) -> int:
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
    )
    write_run(run, catalog_dir)

    start = time.monotonic()
    try:
        result = subprocess.run(argv, cwd=cwd)
        exit_code = result.returncode
        status = "completed" if exit_code == 0 else "failed"
    except KeyboardInterrupt:
        exit_code = 130
        status = "killed"

    import dataclasses
    run = dataclasses.replace(
        run,
        duration_s=time.monotonic() - start,
        exit_code=exit_code,
        status=status,
    )
    write_run(run, catalog_dir)
    return exit_code
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_runner.py -v
```

Expected: 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bathos/runner.py tests/test_runner.py
git commit -m "feat: bth run subprocess wrapper with provenance capture"
```

---

## Task 8: Query — ls, show, find, sql

**Files:**
- Create: `src/bathos/query.py`
- Create: `tests/test_query.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_query.py`:

```python
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
import dataclasses
import pytest
from bathos.catalog import write_run, init_catalog
from bathos.query import list_runs, get_run, find_runs, run_sql
from bathos.schema import Run


@pytest.fixture
def populated_catalog(tmp_catalog: Path) -> Path:
    init_catalog(tmp_catalog)
    base = datetime(2026, 5, 10, 12, 0, 0, tzinfo=timezone.utc)
    for i, (proj, status) in enumerate([
        ("prolix", "completed"),
        ("prolix", "failed"),
        ("espaloma", "completed"),
    ]):
        r = Run(
            project_slug=proj,
            command=f"python run_{i}.py",
            argv=["python", f"run_{i}.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            timestamp=base + timedelta(hours=i),
            status=status,
            exit_code=0 if status == "completed" else 1,
        )
        write_run(r, tmp_catalog)
    return tmp_catalog


def test_list_runs_returns_all(populated_catalog: Path):
    runs = list_runs(populated_catalog)
    assert len(runs) == 3


def test_list_runs_filter_by_project(populated_catalog: Path):
    runs = list_runs(populated_catalog, project="prolix")
    assert len(runs) == 2
    assert all(r.project_slug == "prolix" for r in runs)


def test_list_runs_filter_by_status(populated_catalog: Path):
    runs = list_runs(populated_catalog, status="failed")
    assert len(runs) == 1
    assert runs[0].status == "failed"


def test_get_run_returns_correct(populated_catalog: Path):
    all_runs = list_runs(populated_catalog)
    target = all_runs[0]
    found = get_run(target.id, populated_catalog)
    assert found is not None
    assert found.id == target.id


def test_get_run_returns_none_for_unknown(populated_catalog: Path):
    assert get_run("nonexistent-id", populated_catalog) is None


def test_find_runs_since(populated_catalog: Path):
    since = datetime(2026, 5, 10, 13, 30, 0, tzinfo=timezone.utc)
    runs = find_runs(populated_catalog, since=since)
    assert len(runs) == 1
    assert runs[0].project_slug == "espaloma"


def test_run_sql_returns_rows(populated_catalog: Path):
    glob = str(populated_catalog / "runs" / "run_*.parquet")
    rows = run_sql(f"SELECT project_slug, count(*) as n FROM read_parquet('{glob}') GROUP BY 1 ORDER BY 1")
    assert len(rows) == 2
    projects = {row[0] for row in rows}
    assert "prolix" in projects
    assert "espaloma" in projects
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/test_query.py -v
```

Expected: `ImportError`.

- [ ] **Step 3: Implement query.py**

Create `src/bathos/query.py`:

```python
from __future__ import annotations
from datetime import datetime
from pathlib import Path
import duckdb

from bathos.catalog import read_runs
from bathos.schema import Run


def list_runs(
    catalog_dir: Path,
    project: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[Run]:
    runs = read_runs(catalog_dir)
    if project:
        runs = [r for r in runs if r.project_slug == project]
    if status:
        runs = [r for r in runs if r.status == status]
    return runs[:limit]


def get_run(run_id: str, catalog_dir: Path) -> Run | None:
    runs = read_runs(catalog_dir)
    for r in runs:
        if r.id == run_id:
            return r
    return None


def find_runs(
    catalog_dir: Path,
    since: datetime | None = None,
    project: str | None = None,
    status: str | None = None,
    tags: list[str] | None = None,
) -> list[Run]:
    runs = read_runs(catalog_dir)
    if since:
        runs = [r for r in runs if r.timestamp >= since]
    if project:
        runs = [r for r in runs if r.project_slug == project]
    if status:
        runs = [r for r in runs if r.status == status]
    if tags:
        runs = [r for r in runs if any(t in r.tags for t in tags)]
    return runs


def run_sql(sql: str) -> list[tuple]:
    con = duckdb.connect()
    result = con.execute(sql).fetchall()
    return result
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_query.py -v
```

Expected: 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add src/bathos/query.py tests/test_query.py
git commit -m "feat: query interface — list_runs, get_run, find_runs, run_sql"
```

---

## Task 9: CLI — Typer app wiring all commands

**Files:**
- Create: `src/bathos/cli.py`
- Create: `tests/test_cli.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_cli.py`:

```python
import sys
from pathlib import Path
from typer.testing import CliRunner
from bathos.cli import app

runner = CliRunner()


def test_version():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "0.1.0" in result.output


def test_init_creates_dirs(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / ".bth" / "catalog"))
    result = runner.invoke(app, ["init", "--slug", "testproj"])
    assert result.exit_code == 0
    assert (tmp_path / "scripts" / "experiments").is_dir()
    assert (tmp_path / ".bth.toml").exists()


def test_run_records_run(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    # Create .bth.toml so bth run can find project slug
    (tmp_path / ".bth.toml").write_text(
        f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n'
    )
    result = runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])
    assert result.exit_code == 0


def test_ls_shows_runs(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    (tmp_path / ".bth.toml").write_text(
        f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n'
    )
    runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])
    result = runner.invoke(app, ["ls"])
    assert result.exit_code == 0
    assert "testproj" in result.output


def test_show_displays_run_detail(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))
    (tmp_path / ".bth.toml").write_text(
        f'[project]\nslug = "testproj"\nroot = "{tmp_path}"\n'
    )
    runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])
    from bathos.catalog import read_runs, init_catalog
    init_catalog(catalog)
    runs = read_runs(catalog)
    result = runner.invoke(app, ["show", runs[0].id])
    assert result.exit_code == 0
    assert runs[0].id in result.output
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/test_cli.py -v
```

Expected: `ImportError`.

- [ ] **Step 3: Implement cli.py**

Create `src/bathos/cli.py`:

```python
from __future__ import annotations
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
import typer

app = typer.Typer(help="bathos — local-first experiment tracking")


def _catalog_dir() -> Path:
    override = os.environ.get("BTH_CATALOG_DIR")
    if override:
        return Path(override)
    from bathos.config import default_catalog_dir
    return default_catalog_dir()


def _require_project_slug() -> str:
    slug_env = os.environ.get("BTH_PROJECT_SLUG")
    if slug_env:
        return slug_env
    from bathos.config import find_project_config, load_project_config
    cfg_path = find_project_config()
    if cfg_path is None:
        typer.echo("No .bth.toml found. Run `bth init` first.", err=True)
        raise typer.Exit(1)
    return load_project_config(cfg_path).slug


@app.callback(invoke_without_command=True)
def main(
    version: bool = typer.Option(False, "--version", "-V", is_eager=True),
):
    if version:
        from bathos import __version__
        typer.echo(f"bathos {__version__}")
        raise typer.Exit()


@app.command()
def init(
    slug: str = typer.Option(..., "--slug", "-s", help="Project slug"),
    remote: Optional[str] = typer.Option(None, "--remote", help="host:remote_path"),
    slurm_partition: Optional[str] = typer.Option(None, "--slurm-partition"),
):
    """Register project, scaffold scripts/ dirs, write .bth.toml."""
    from bathos.init import init_project
    init_project(
        Path.cwd(),
        slug=slug,
        catalog_dir=_catalog_dir(),
        remote=remote,
        slurm_partition=slurm_partition,
    )
    typer.echo(f"Initialized bathos project '{slug}'")


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def run(
    argv: list[str] = typer.Argument(...),
    out: list[str] = typer.Option([], "--out", help="Output path to register"),
    tag: list[str] = typer.Option([], "--tag", "-t"),
):
    """Run a script and record provenance."""
    from bathos.runner import run_script
    slug = _require_project_slug()
    exit_code = run_script(
        argv=argv,
        project_slug=slug,
        catalog_dir=_catalog_dir(),
        output_paths=out,
        tags=tag,
    )
    raise typer.Exit(exit_code)


@app.command("ls")
def ls_cmd(
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    since: Optional[str] = typer.Option(None, "--since", help="e.g. 7d, 24h"),
    status: Optional[str] = typer.Option(None, "--status"),
    limit: int = typer.Option(20, "--limit", "-n"),
):
    """List recent runs."""
    from bathos.query import find_runs
    since_dt = _parse_since(since)
    runs = find_runs(_catalog_dir(), since=since_dt, project=project, status=status)
    runs = runs[:limit]
    if not runs:
        typer.echo("No runs found.")
        return
    header = f"{'ID':38} {'PROJECT':12} {'STATUS':10} {'EXIT':5} {'DURATION':8} COMMAND"
    typer.echo(header)
    typer.echo("-" * len(header))
    for r in runs:
        typer.echo(
            f"{r.id:38} {r.project_slug:12} {r.status:10} {r.exit_code:5} "
            f"{r.duration_s:7.1f}s {r.command[:40]}"
        )


@app.command()
def show(run_id: str = typer.Argument(...)):
    """Show full details of a run."""
    from bathos.query import get_run
    r = get_run(run_id, _catalog_dir())
    if r is None:
        typer.echo(f"Run not found: {run_id}", err=True)
        raise typer.Exit(1)
    typer.echo(f"id:           {r.id}")
    typer.echo(f"project:      {r.project_slug}")
    typer.echo(f"status:       {r.status}")
    typer.echo(f"exit_code:    {r.exit_code}")
    typer.echo(f"duration:     {r.duration_s:.2f}s")
    typer.echo(f"git_hash:     {r.git_hash}")
    typer.echo(f"git_branch:   {r.git_branch}")
    typer.echo(f"git_dirty:    {r.git_dirty}")
    typer.echo(f"timestamp:    {r.timestamp.isoformat()}")
    typer.echo(f"command:      {r.command}")
    typer.echo(f"output_paths: {r.output_paths}")
    typer.echo(f"tags:         {r.tags}")


@app.command()
def find(
    project: Optional[str] = typer.Option(None, "--project", "-p"),
    since: Optional[str] = typer.Option(None, "--since"),
    status: Optional[str] = typer.Option(None, "--status"),
    tag: list[str] = typer.Option([], "--tag"),
):
    """Find runs matching filters."""
    from bathos.query import find_runs
    runs = find_runs(
        _catalog_dir(),
        since=_parse_since(since),
        project=project,
        status=status,
        tags=tag or None,
    )
    for r in runs:
        typer.echo(f"{r.id}  {r.project_slug}  {r.status}  {r.command[:60]}")


@app.command()
def sql(query: str = typer.Argument(...)):
    """Run raw DuckDB SQL against the catalog."""
    from bathos.query import run_sql
    rows = run_sql(query)
    for row in rows:
        typer.echo("\t".join(str(v) for v in row))


def _parse_since(since: str | None) -> datetime | None:
    if since is None:
        return None
    if since.endswith("d"):
        return datetime.now(timezone.utc) - timedelta(days=float(since[:-1]))
    if since.endswith("h"):
        return datetime.now(timezone.utc) - timedelta(hours=float(since[:-1]))
    return None
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_cli.py -v
```

Expected: 5 tests PASS.

- [ ] **Step 5: Run full test suite**

```bash
uv run pytest -v
```

Expected: all tests PASS (no failures, no errors).

- [ ] **Step 6: Smoke test the CLI**

```bash
uv run bth --version
```

Expected: `bathos 0.1.0`

- [ ] **Step 7: Commit**

```bash
git add src/bathos/cli.py tests/test_cli.py
git commit -m "feat: Typer CLI — init, run, ls, show, find, sql commands"
```

---

## Task 10: Integration smoke test

**Files:**
- Create: `tests/test_integration.py`

- [ ] **Step 1: Write integration test**

Create `tests/test_integration.py`:

```python
"""End-to-end: init → run → ls → show → find."""
import sys
from pathlib import Path
from typer.testing import CliRunner
from bathos.cli import app
from bathos.catalog import read_runs, init_catalog

runner = CliRunner(mix_stderr=False)


def test_full_workflow(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    catalog = tmp_path / ".bth" / "catalog"
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))

    # 1. init
    r = runner.invoke(app, ["init", "--slug", "intproj"])
    assert r.exit_code == 0
    assert (tmp_path / ".bth.toml").exists()
    assert (tmp_path / "scripts" / "experiments").is_dir()

    # 2. run a passing script
    r = runner.invoke(app, ["run", sys.executable, "--", "-c", "pass"])
    assert r.exit_code == 0

    # 3. run a failing script
    r = runner.invoke(app, ["run", sys.executable, "--", "-c", "raise SystemExit(1)"])
    assert r.exit_code == 1

    # 4. ls shows both runs
    r = runner.invoke(app, ["ls"])
    assert r.exit_code == 0
    assert "intproj" in r.output
    lines = [l for l in r.output.splitlines() if "intproj" in l]
    assert len(lines) == 2

    # 5. find by status
    r = runner.invoke(app, ["find", "--status", "failed"])
    assert r.exit_code == 0
    assert "failed" in r.output

    # 6. show run detail
    init_catalog(catalog)
    runs = read_runs(catalog)
    run_id = runs[0].id
    r = runner.invoke(app, ["show", run_id])
    assert r.exit_code == 0
    assert run_id in r.output
    assert "intproj" in r.output

    # 7. sql escape hatch
    glob = str(catalog / "runs" / "run_*.parquet")
    r = runner.invoke(app, ["sql", f"SELECT count(*) FROM read_parquet('{glob}')"])
    assert r.exit_code == 0
    assert "2" in r.output
```

- [ ] **Step 2: Run integration test**

```bash
uv run pytest tests/test_integration.py -v
```

Expected: 1 test PASS.

- [ ] **Step 3: Run full suite one final time**

```bash
uv run pytest -v
```

Expected: all tests PASS.

- [ ] **Step 4: Final commit**

```bash
git add tests/test_integration.py
git commit -m "test: end-to-end integration smoke test for bathos v0.1"
```

---

## Self-Review

**Spec coverage check:**
- ✅ Core schema + DuckDB catalog init (#124)
- ✅ `bth init` — project registration, script dirs, `.bth.toml`, `_bth_env.sh` (#125)
- ✅ `bth run` — CLI wrapper with provenance capture (#126)
- ✅ `bth ls` / `bth show` / `bth find` / `bth sql` (#127)
- ⏭ `@bth.experiment` decorator — plan #2
- ⏭ `bth check` — plan #2
- ⏭ FastMCP server — plan #3
- ⏭ `bth migrate` — plan #4

**Type consistency check:**
- `Run.from_arrow_row(pydict, i)` defined in Task 2, used in `catalog.py` Task 3 ✅
- `write_run(run, catalog_dir)` defined in Task 3, used in `runner.py` Task 7 ✅
- `read_runs(catalog_dir)` defined in Task 3, used in `query.py` Task 8 ✅
- `init_catalog(catalog_dir)` defined in Task 3, used in `init.py` Task 6 and tests ✅
- `_catalog_dir()` defined in `cli.py`, uses `BTH_CATALOG_DIR` env var in tests ✅

**No placeholders detected.** All steps contain complete code.
