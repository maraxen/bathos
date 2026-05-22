Let me create the plan document now:

I'm using the **writing-plans** skill to create the implementation plan.

# bathos Visualization Suite — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Ship the three-surface visualization suite (Rich CLI, `bth view` FastAPI server, `bth export --html` static file) with full test coverage and packaging integration.

**Architecture:** Modular separation — `rich_fmt.py` (base dep, CLI enhancements), `viz/data.py` (projection layer), `viz/server.py` (FastAPI), `viz/html.py` (static renderer), shared Jinja2 templates and vendored JS/CSS.

**Tech Stack:** Python 3.12, Rich 13+, FastAPI 0.110+, Jinja2 3.1+, Alpine.js 3.x (vendored), Pico CSS (vendored)

---

## Phase 1: Dependencies, Module Structure, and Packaging (P0 — Blocker)

**Dependency:** None  
**Blocks:** All downstream visualization work  
**Parallelism:** Sequential  
**Risk:** Critical — missing dependencies or broken packaging cascade into all later phases

### Task 1.1: Update `pyproject.toml` with Rich base dependency and `viz` optional extra

**Files:**
- Modify: `/home/marielle/projects/bathos/pyproject.toml:25-42`

**Steps:**

**Step 1: Add Rich to base dependencies**

Open `pyproject.toml` and update the `dependencies` list to include `"rich>=13"`:

```toml
dependencies = [
    "typer>=0.12",
    "duckdb>=1.0",
    "pyarrow>=16",
    "pytz>=2026.2",
    "tomlkit>=0.12",
    "toml>=0.10",
    "rich>=13",
]
```

**Step 2: Add `viz` optional-dependencies group**

After the `mcp` group (line 35), add:

```toml
[project.optional-dependencies]
mcp = ["fastmcp>=2.0,<3.0"]
viz = [
    "fastapi>=0.110",
    "uvicorn[standard]>=0.27",
    "jinja2>=3.1",
]
```

**Step 3: Update wheel packaging to include viz assets**

Find `[tool.hatch.build.targets.wheel]` section. Update or add the `include` list:

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/bathos"]

[[tool.hatch.build.targets.wheel.force-include]]
"src/bathos/viz/templates" = "bathos/viz/templates"
"src/bathos/viz/static" = "bathos/viz/static"
```

Alternatively, if hatch supports `include` (preferred):

```toml
[tool.hatch.build.targets.wheel]
packages = ["src/bathos"]
include = [
  "src/bathos/viz/templates/index.html",
  "src/bathos/viz/templates/_runs.html",
  "src/bathos/viz/templates/_run_detail.html",
  "src/bathos/viz/templates/_campaign.html",
  "src/bathos/viz/static/alpine.min.js",
  "src/bathos/viz/static/pico.min.css",
  "src/bathos/viz/static/VERSIONS.md",
]
```

**Step 4: Run syntax check**

```bash
python -m tomllib < pyproject.toml > /dev/null && echo "PASS: pyproject.toml syntax valid"
```

Expected: PASS message, exit code 0.

**Step 5: Commit**

```bash
cd /home/marielle/projects/bathos
git add pyproject.toml
git commit -m "feat(viz): add rich to base deps, add viz optional extra with fastapi, uvicorn, jinja2"
```

---

### Task 1.2: Create `viz/` module structure with `__init__.py`

**Files:**
- Create: `/home/marielle/projects/bathos/src/bathos/viz/__init__.py`
- Create: `/home/marielle/projects/bathos/src/bathos/viz/templates/`
- Create: `/home/marielle/projects/bathos/src/bathos/viz/static/`

**Steps:**

**Step 1: Create viz package**

```bash
mkdir -p /home/marielle/projects/bathos/src/bathos/viz/templates
mkdir -p /home/marielle/projects/bathos/src/bathos/viz/static
touch /home/marielle/projects/bathos/src/bathos/viz/__init__.py
```

**Step 2: Write `__init__.py`**

Create `/home/marielle/projects/bathos/src/bathos/viz/__init__.py`:

```python
"""bathos visualization suite — Rich CLI, FastAPI server, static HTML export."""

__all__ = ["data", "server", "html"]
```

**Step 3: Create stub files**

Create empty stubs to be filled later:
```bash
touch /home/marielle/projects/bathos/src/bathos/viz/data.py
touch /home/marielle/projects/bathos/src/bathos/viz/server.py
touch /home/marielle/projects/bathos/src/bathos/viz/html.py
```

**Step 4: Verify structure**

```bash
find /home/marielle/projects/bathos/src/bathos/viz -type f | sort
```

Expected output:
```
/home/marielle/projects/bathos/src/bathos/viz/__init__.py
/home/marielle/projects/bathos/src/bathos/viz/data.py
/home/marielle/projects/bathos/src/bathos/viz/html.py
/home/marielle/projects/bathos/src/bathos/viz/server.py
/home/marielle/projects/bathos/src/bathos/viz/templates/
/home/marielle/projects/bathos/src/bathos/viz/static/
```

**Step 5: Commit**

```bash
cd /home/marielle/projects/bathos
git add src/bathos/viz/
git commit -m "chore(viz): scaffold module structure"
```

---

### Task 1.3: Download and vendor Alpine.js and Pico CSS

**Files:**
- Create: `/home/marielle/projects/bathos/src/bathos/viz/static/alpine.min.js`
- Create: `/home/marielle/projects/bathos/src/bathos/viz/static/pico.min.css`
- Create: `/home/marielle/projects/bathos/src/bathos/viz/static/VERSIONS.md`

**Steps:**

**Step 1: Download Alpine.js 3.x**

```bash
cd /home/marielle/projects/bathos/src/bathos/viz/static
curl -s https://cdn.jsdelivr.net/npm/alpinejs@3.x/dist/cdn.min.js > alpine.min.js
```

Expected: alpine.min.js ~50 KB, contains `Alpine` global variable.

**Step 2: Verify Alpine.js download**

```bash
wc -c /home/marielle/projects/bathos/src/bathos/viz/static/alpine.min.js
grep -q "Alpine" /home/marielle/projects/bathos/src/bathos/viz/static/alpine.min.js && echo "PASS: Alpine.js contains 'Alpine' reference"
```

Expected: file size > 40000 bytes, PASS message.

**Step 3: Download Pico CSS**

```bash
cd /home/marielle/projects/bathos/src/bathos/viz/static
curl -s https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css > pico.min.css
```

Expected: pico.min.css ~20 KB, contains CSS rules.

**Step 4: Verify Pico CSS**

```bash
wc -c /home/marielle/projects/bathos/src/bathos/viz/static/pico.min.css
grep -q "body" /home/marielle/projects/bathos/src/bathos/viz/static/pico.min.css && echo "PASS: Pico CSS contains 'body' rule"
```

Expected: file size > 10000 bytes, PASS message.

**Step 5: Create VERSIONS.md with license attribution**

Create `/home/marielle/projects/bathos/src/bathos/viz/static/VERSIONS.md`:

```markdown
# Vendored Assets

## Alpine.js

- **Version:** 3.x (latest)
- **Source:** https://alpinejs.dev/
- **License:** MIT
- **URL:** https://cdn.jsdelivr.net/npm/alpinejs@3.x/dist/cdn.min.js
- **SHA256:** [computed below]

## Pico CSS

- **Version:** 2.x (latest)
- **Source:** https://picocss.com/
- **License:** MIT
- **URL:** https://cdn.jsdelivr.net/npm/@picocss/pico@2/css/pico.min.css
- **SHA256:** [computed below]

---

Both libraries are MIT-licensed. No modifications have been made.

To regenerate checksums:
```bash
sha256sum alpine.min.js pico.min.css
```
```

**Step 6: Compute and record SHA256 checksums**

```bash
cd /home/marielle/projects/bathos/src/bathos/viz/static
sha256sum alpine.min.js pico.min.css
```

Automate the VERSIONS.md update with a one-liner:

```bash
cd /home/marielle/projects/bathos/src/bathos/viz/static
python3 -c "
import hashlib, pathlib
for f in ['alpine.min.js', 'pico.min.css']:
    h = hashlib.sha256(pathlib.Path(f).read_bytes()).hexdigest()
    print(f'{h}  {f}')
" | tee /tmp/checksums.txt

# Embed checksums into VERSIONS.md
python3 - <<'EOF'
import pathlib, re
checksums = {}
for line in pathlib.Path('/tmp/checksums.txt').read_text().splitlines():
    sha, name = line.split('  ')
    checksums[name] = sha
md = pathlib.Path('VERSIONS.md').read_text()
for name, sha in checksums.items():
    md = re.sub(rf'(- \*\*SHA256:\*\* )(\[computed below\]|[a-f0-9]{{64}})',
                rf'\g<1>{sha}', md, count=1)
pathlib.Path('VERSIONS.md').write_text(md)
print("VERSIONS.md updated with checksums.")
EOF
```

**Step 7: Verify files are readable and non-empty**

```bash
test -s /home/marielle/projects/bathos/src/bathos/viz/static/alpine.min.js && test -s /home/marielle/projects/bathos/src/bathos/viz/static/pico.min.css && echo "PASS: Both static files present and non-empty"
```

Expected: PASS message.

**Step 8: Commit**

```bash
cd /home/marielle/projects/bathos
git add src/bathos/viz/static/
git commit -m "chore(viz): vendor alpine.js and pico.css with license attribution"
```

---

## Phase 2: Rich CLI Formatter (`rich_fmt.py`) — Base Feature

**Dependency:** Phase 1 complete  
**Blocks:** `cli.py` integration (Phase 4)  
**Parallelism:** Sequential  
**Risk:** Medium — formatting logic is critical for all CLI surfaces, needs careful test coverage

### Task 2.1: Implement `rich_fmt.py` with `render_runs_table()`

**Files:**
- Create: `/home/marielle/projects/bathos/src/bathos/rich_fmt.py`
- Create: `/home/marielle/projects/bathos/tests/test_rich_fmt.py`

**Steps:**

**Step 1: Write failing test for `render_runs_table()`**

Create `/home/marielle/projects/bathos/tests/test_rich_fmt.py`:

```python
import io
from datetime import UTC, datetime
from rich.console import Console

from bathos.rich_fmt import render_runs_table
from bathos.schema import Run


def test_render_runs_table_basic():
    """Test that render_runs_table produces a table with correct headers."""
    runs = [
        Run(
            id="abc123def456",
            project_slug="test-proj",
            command="python test.py",
            argv=["python", "test.py"],
            git_hash="a1b2c3d4",
            git_branch="main",
            git_dirty=False,
            status="completed",
            exit_code=0,
            duration_s=12.5,
            timestamp=datetime(2026, 5, 21, 10, 30, 0, tzinfo=UTC),
            outcome="pass",
            outcome_is_residual=False,
            tags=["v1"],
            output_paths=["/tmp/result.json"],
            campaign_id="",
        )
    ]

    console = Console(file=io.StringIO(), force_terminal=True, width=200)
    render_runs_table(runs, console=console)
    output = console.file.getvalue()

    # Check for table headers
    assert "ID" in output or "id" in output.lower()
    assert "Project" in output or "project" in output.lower()
    assert "Status" in output or "status" in output.lower()
    assert "Outcome" in output or "outcome" in output.lower()
    assert "Duration" in output or "duration" in output.lower()
    
    # Check for run data
    assert "abc123de" in output or "test-proj" in output


def test_render_runs_table_empty():
    """Test that render_runs_table handles empty list gracefully."""
    console = Console(file=io.StringIO(), force_terminal=True)
    render_runs_table([], console=console)
    output = console.file.getvalue()
    
    # Should produce some output (at least a message)
    assert len(output) > 0
```

**Step 2: Run test to verify it fails**

```bash
cd /home/marielle/projects/bathos
pytest tests/test_rich_fmt.py::test_render_runs_table_basic -xvs
```

Expected: `ModuleNotFoundError: No module named 'bathos.rich_fmt'`

**Step 3: Implement `rich_fmt.py`**

Create `/home/marielle/projects/bathos/src/bathos/rich_fmt.py`:

```python
"""Rich CLI formatters for bathos."""

from __future__ import annotations

import io
from datetime import datetime

from rich.console import Console
from rich.table import Table
from rich.style import Style
from rich.panel import Panel
from rich.text import Text

from bathos.schema import Run, CURRENT_SCHEMA_VERSION
from bathos.campaigns import Campaign


def _format_duration(duration_s: float) -> str:
    """Format duration in seconds as human-readable string."""
    if duration_s < 60:
        return f"{duration_s:.1f}s"
    else:
        minutes = int(duration_s // 60)
        seconds = int(duration_s % 60)
        return f"{minutes}m {seconds}s"


def _status_style(status: str) -> str:
    """Return Rich style for run status."""
    if status == "completed":
        return "green"
    elif status == "failed":
        return "red"
    elif status == "running":
        return "yellow"
    else:
        return "dim"


def _outcome_style(outcome: str) -> str:
    """Return Rich style for outcome."""
    if outcome == "pass":
        return "green"
    elif outcome == "fail":
        return "red"
    elif outcome == "marginal":
        return "yellow"
    else:
        return "dim"


def render_runs_table(runs: list[Run], console: Console | None = None) -> None:
    """
    Render a Rich Table of runs.
    
    Columns: ID (short), Project, Status, Outcome, Duration, Campaign, Branch, Timestamp
    
    Args:
        runs: List of Run objects
        console: Rich Console instance; defaults to Console() if None
    """
    if console is None:
        console = Console()
    
    if not runs:
        console.print("[dim]No runs to display.[/dim]")
        return
    
    table = Table(title="Runs", show_lines=False)
    table.add_column("ID", style="cyan", width=8)
    table.add_column("Project", style="magenta", width=15)
    table.add_column("Status", width=10)
    table.add_column("Outcome", width=12)
    table.add_column("Duration", width=10)
    table.add_column("Campaign", style="dim", width=12)
    table.add_column("Branch", style="blue", width=12)
    table.add_column("Timestamp", style="dim", width=19)
    
    for run in runs:
        id_short = run.id[:8]
        campaign_display = run.campaign_id[:8] if run.campaign_id else ""
        timestamp_str = run.timestamp.strftime("%Y-%m-%d %H:%M:%S")
        duration_str = _format_duration(run.duration_s)
        
        status_text = Text(run.status, style=_status_style(run.status))
        outcome_text = Text(run.outcome or "unknown", style=_outcome_style(run.outcome))
        
        table.add_row(
            id_short,
            run.project_slug,
            status_text,
            outcome_text,
            duration_str,
            campaign_display,
            run.git_branch,
            timestamp_str,
        )
    
    console.print(table)


def render_run_detail(run: Run, console: Console | None = None) -> None:
    """
    Render detailed view of a single run with four panels.
    
    Panels: Execution, Provenance, Outcome, Postmortem (if assigned)
    
    Args:
        run: Run object
        console: Rich Console instance; defaults to Console() if None
    """
    if console is None:
        console = Console()
    
    # Execution panel
    exec_lines = [
        f"Command:     {run.command}",
        f"Hostname:    {run.hostname}",
        f"SLURM Job:   {run.slurm_job_id or '(none)'}",
        f"Duration:    {_format_duration(run.duration_s)}",
        f"Exit Code:   {run.exit_code}",
    ]
    if run.argv:
        exec_lines.append(f"Args:        {' '.join(run.argv)}")
    
    console.print(Panel("\n".join(exec_lines), title="Execution", expand=False))
    
    # Provenance panel
    prov_lines = [
        f"Git Hash:    {run.git_hash[:8]} ({run.git_branch})",
        f"Git Dirty:   {run.git_dirty}",
        f"Script SHA:  {run.script_sha256[:16]}..." if run.script_sha256 else "Script SHA:  (none)",
        f"Sidecar:     {run.sidecar_mode or 'none'} @ {run.sidecar_path or '(none)'}",
        f"Agent Mode:  {run.agent_mode or 'none'}",
        f"Parent Run:  {run.parent_run_id[:8] if run.parent_run_id else '(none)'}",
    ]
    
    console.print(Panel("\n".join(prov_lines), title="Provenance", expand=False))
    
    # Outcome panel
    outcome_lines = [
        f"Status:      {run.status}",
        f"Outcome:     {run.outcome or 'unknown'}",
        f"Residual:    {run.outcome_is_residual}",
    ]
    if run.tags:
        outcome_lines.append(f"Tags:        {', '.join(run.tags)}")
    if run.output_paths:
        outcome_lines.append(f"Outputs:     {len(run.output_paths)} file(s)")
        for path in run.output_paths[:3]:
            outcome_lines.append(f"             {path}")
        if len(run.output_paths) > 3:
            outcome_lines.append(f"             ... and {len(run.output_paths) - 3} more")
    
    console.print(Panel("\n".join(outcome_lines), title="Outcome", expand=False))
    
    # Postmortem panel (if assigned)
    if run.postmortem_status != "unassigned":
        pm_lines = [
            f"Status:      {run.postmortem_status}",
            f"Hypothesis:  {run.postmortem_hypothesis_status}",
            f"Author:      {run.postmortem_author or '(unassigned)'}",
            f"Summary:     {run.postmortem_summary or '(none)'}",
            f"Path:        {run.postmortem_path or '(none)'}",
        ]
        console.print(Panel("\n".join(pm_lines), title="Postmortem", expand=False))


def render_campaign_table(campaigns: list[Campaign], console: Console | None = None) -> None:
    """
    Render a Rich Table of campaigns.
    
    Columns: ID (short), Name, Mode, Status, Started, Run Count
    
    Args:
        campaigns: List of Campaign objects
        console: Rich Console instance; defaults to Console() if None
    """
    if console is None:
        console = Console()
    
    if not campaigns:
        console.print("[dim]No campaigns to display.[/dim]")
        return
    
    table = Table(title="Campaigns", show_lines=False)
    table.add_column("ID", style="cyan", width=8)
    table.add_column("Name", style="magenta", width=20)
    table.add_column("Mode", width=12)
    table.add_column("Status", width=10)
    table.add_column("Started", style="dim", width=19)
    
    for campaign in campaigns:
        id_short = campaign.id[:8]
        started = campaign.started_at[:19] if campaign.started_at else "(unknown)"
        
        table.add_row(
            id_short,
            campaign.name,
            campaign.mode,
            campaign.status,
            started,
        )
    
    console.print(table)


def render_campaign_review(
    campaign: Campaign,
    review: dict,
    console: Console | None = None,
) -> None:
    """
    Render campaign review with outcome distribution and anomalies.
    
    Args:
        campaign: Campaign object
        review: Dict returned by campaigns.review_campaign()
        console: Rich Console instance; defaults to Console() if None
    """
    if console is None:
        console = Console()
    
    # Campaign header
    header = f"[bold]{campaign.name}[/bold] ({campaign.mode})"
    if campaign.question:
        header += f"\n[dim]{campaign.question}[/dim]"
    
    console.print(Panel(header, title="Campaign", expand=False))
    
    # Outcome distribution table
    if "outcome_distribution" in review:
        dist_table = Table(title="Outcome Distribution", show_header=True)
        dist_table.add_column("Outcome", style="cyan")
        dist_table.add_column("Count", style="magenta")
        
        for outcome, count in sorted(review["outcome_distribution"].items()):
            dist_table.add_row(outcome, str(count))
        
        console.print(dist_table)
    
    # Anomalies
    if review.get("anomalies"):
        anomalies_text = "\n".join([f"[yellow]⚠ {a}[/yellow]" for a in review["anomalies"]])
        console.print(Panel(anomalies_text, title="Anomalies", expand=False))
```

**Step 4: Run test to verify it passes**

```bash
cd /home/marielle/projects/bathos
pytest tests/test_rich_fmt.py::test_render_runs_table_basic -xvs
```

Expected: PASS

**Step 5: Run all rich_fmt tests**

```bash
cd /home/marielle/projects/bathos
pytest tests/test_rich_fmt.py -v
```

Expected: Both tests pass.

**Step 6: Commit**

```bash
cd /home/marielle/projects/bathos
git add src/bathos/rich_fmt.py tests/test_rich_fmt.py
git commit -m "feat(rich_fmt): implement run table, detail, campaign table, and campaign review renderers"
```

---

### Task 2.2: Implement remaining Rich CLI formatters (task split for clarity)

**Files:**
- Modify: `/home/marielle/projects/bathos/tests/test_rich_fmt.py` — add test cases for each function
- Verify: `/home/marielle/projects/bathos/src/bathos/rich_fmt.py` — already complete from Task 2.1

**Steps:**

**Step 1: Write tests for `render_run_detail()`**

Add to `/home/marielle/projects/bathos/tests/test_rich_fmt.py`:

```python
def test_render_run_detail_with_postmortem():
    """Test that render_run_detail includes postmortem panel when status != 'unassigned'."""
    run = Run(
        id="abc123def456",
        project_slug="test-proj",
        command="python test.py",
        argv=["python", "test.py"],
        git_hash="a1b2c3d4",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime(2026, 5, 21, 10, 30, 0, tzinfo=UTC),
        postmortem_status="assigned",
        postmortem_author="alice",
        postmortem_summary="Issue found in step 42",
    )

    console = Console(file=io.StringIO(), force_terminal=True)
    render_run_detail(run, console=console)
    output = console.file.getvalue()

    assert "Postmortem" in output
    assert "alice" in output


def test_render_run_detail_without_postmortem():
    """Test that render_run_detail omits postmortem panel when status == 'unassigned'."""
    run = Run(
        id="abc123def456",
        project_slug="test-proj",
        command="python test.py",
        argv=["python", "test.py"],
        git_hash="a1b2c3d4",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime(2026, 5, 21, 10, 30, 0, tzinfo=UTC),
        postmortem_status="unassigned",
    )

    console = Console(file=io.StringIO(), force_terminal=True)
    render_run_detail(run, console=console)
    output = console.file.getvalue()

    # Postmortem section should not appear if unassigned
    assert output.count("Postmortem") == 0
```

**Step 2: Write tests for `render_campaign_table()`**

Add to `/home/marielle/projects/bathos/tests/test_rich_fmt.py`:

```python
def test_render_campaign_table():
    """Test that render_campaign_table produces a table with campaigns."""
    campaigns = [
        Campaign(
            id="camp1234567890",
            project_slug="test-proj",
            name="Phase 1: Baseline",
            mode="exploration",
            question="What is the baseline?",
            status="open",
            started_at="2026-05-21T10:00:00",
        )
    ]

    console = Console(file=io.StringIO(), force_terminal=True)
    render_campaign_table(campaigns, console=console)
    output = console.file.getvalue()

    assert "Phase 1: Baseline" in output
    assert "exploration" in output
```

**Step 3: Write tests for `render_campaign_review()`**

Add to `/home/marielle/projects/bathos/tests/test_rich_fmt.py`:

```python
def test_render_campaign_review_with_anomalies():
    """Test that render_campaign_review displays anomalies in yellow."""
    campaign = Campaign(
        id="camp1234567890",
        project_slug="test-proj",
        name="Phase 1",
        mode="exploration",
        status="open",
        started_at="2026-05-21T10:00:00",
    )
    review = {
        "outcome_distribution": {"pass": 8, "fail": 2},
        "anomalies": ["High failure rate: 20% (2/10 runs)"],
    }

    console = Console(file=io.StringIO(), force_terminal=True)
    render_campaign_review(campaign, review, console=console)
    output = console.file.getvalue()

    assert "Anomalies" in output
    assert "High failure rate" in output
```

**Step 4: Run all tests**

```bash
cd /home/marielle/projects/bathos
pytest tests/test_rich_fmt.py -v
```

Expected: All 6 tests pass.

**Step 5: Commit**

```bash
cd /home/marielle/projects/bathos
git add tests/test_rich_fmt.py
git commit -m "test(rich_fmt): add comprehensive test coverage for all formatters"
```

---

## Phase 3: Visualization Data Layer (`viz/data.py`)

**Dependency:** Phase 1 complete  
**Blocks:** HTML renderer and FastAPI server (Phase 4–5)  
**Parallelism:** Can start after Phase 1; independent of Rich formatters  
**Risk:** Medium — data projection logic must exactly match design contracts for TypedDicts

### Task 3.1: Implement `viz/data.py` with TypedDict schemas and projection functions

**Files:**
- Create: `/home/marielle/projects/bathos/src/bathos/viz/data.py`
- Create: `/home/marielle/projects/bathos/tests/test_viz_data.py`

**Steps:**

**Step 1: Write failing tests for TypedDict schemas**

Create `/home/marielle/projects/bathos/tests/test_viz_data.py`:

```python
"""Unit tests for viz/data.py projection layer."""

import json
from datetime import UTC, datetime
from typing import Any, TypedDict

import pytest

from bathos.schema import Run
from bathos.campaigns import Campaign
from bathos.viz.data import project_run, project_campaign, aggregate_campaign_runs


def test_project_run_basic():
    """Test that project_run produces a RunDisplay with all required fields."""
    run = Run(
        id="abc123def456",
        project_slug="test-proj",
        command="python test.py",
        argv=["python", "test.py"],
        git_hash="a1b2c3d4",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime(2026, 5, 21, 13, 47, 23, tzinfo=UTC),
        status="completed",
        exit_code=0,
        duration_s=7.3,
        outcome="pass",
        outcome_is_residual=False,
        tags=["v1", "baseline"],
        output_paths=["/tmp/result.json"],
        hostname="compute1.local",
        slurm_job_id="12345",
        script_sha256="abc123",
        sidecar_path="test.bth.toml",
        sidecar_mode="enforced",
        agent_mode="collaborative",
        parent_run_id="",
        campaign_id="",
        postmortem_status="unassigned",
    )

    display = project_run(run, campaign_name="")

    # Check required fields
    assert display["id"] == "abc123def456"
    assert display["id_short"] == "abc123de"
    assert display["project_slug"] == "test-proj"
    assert display["status"] == "completed"
    assert display["exit_code"] == 0
    assert display["duration_s"] == 7.3
    assert display["duration_display"] == "7.3s"
    assert display["timestamp"] == "2026-05-21 13:47:23 UTC"
    assert display["command"] == "python test.py"
    assert display["argv"] == ["python", "test.py"]
    assert display["hostname"] == "compute1.local"
    assert display["slurm_job_id"] == "12345"
    assert display["git_hash"] == "a1b2c3d4"
    assert display["git_hash_short"] == "a1b2c3"
    assert display["git_branch"] == "main"
    assert display["git_dirty"] is False
    assert display["script_sha256"] == "abc123"
    assert display["sidecar_path"] == "test.bth.toml"
    assert display["sidecar_mode"] == "enforced"
    assert display["agent_mode"] == "collaborative"
    assert display["parent_run_id"] == ""
    assert display["tags"] == ["v1", "baseline"]
    assert display["output_paths"] == ["/tmp/result.json"]
    assert display["outcome"] == "pass"
    assert display["outcome_is_residual"] is False
    assert display["campaign_id"] == ""
    assert display["campaign_name"] == ""
    assert display["postmortem_status"] == "unassigned"
    assert display["postmortem_author"] == ""


def test_project_run_with_postmortem():
    """Test that postmortem fields are projected correctly."""
    run = Run(
        id="run1",
        project_slug="proj",
        command="cmd",
        argv=["cmd"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime(2026, 5, 21, 0, 0, 0, tzinfo=UTC),
        postmortem_status="assigned",
        postmortem_author="alice",
        postmortem_summary="Issue in step 42",
        postmortem_path="/path/to/postmortem.toml",
        postmortem_hypothesis_status="refuted",
        postmortem_verdict_override="confirmed",
        postmortem_has_anomalies=True,
        postmortem_asset_links='{"image": "/path/to/image.png"}',
    )

    display = project_run(run, campaign_name="")

    assert display["postmortem_status"] == "assigned"
    assert display["postmortem_author"] == "alice"
    assert display["postmortem_summary"] == "Issue in step 42"
    assert display["postmortem_path"] == "/path/to/postmortem.toml"
    assert display["postmortem_hypothesis_status"] == "refuted"
    assert display["postmortem_verdict_override"] == "confirmed"
    assert display["postmortem_has_anomalies"] is True
    assert display["postmortem_asset_links"] == {"image": "/path/to/image.png"}


def test_project_run_duration_display():
    """Test duration_display formatting."""
    run_short = Run(
        id="r1", project_slug="p", command="c", argv=["c"],
        git_hash="g", git_branch="m", git_dirty=False,
        timestamp=datetime(2026, 5, 21, 0, 0, 0, tzinfo=UTC),
        duration_s=45.5,
    )
    display = project_run(run_short, campaign_name="")
    assert display["duration_display"] == "45.5s"

    run_long = Run(
        id="r2", project_slug="p", command="c", argv=["c"],
        git_hash="g", git_branch="m", git_dirty=False,
        timestamp=datetime(2026, 5, 21, 0, 0, 0, tzinfo=UTC),
        duration_s=134.0,
    )
    display = project_run(run_long, campaign_name="")
    assert display["duration_display"] == "2m 14s"


def test_project_campaign():
    """Test that project_campaign produces a CampaignDisplay."""
    campaign = Campaign(
        id="camp1234567890",
        project_slug="proj",
        name="Phase 1",
        mode="exploration",
        question="What works?",
        hypothesis="X is better than Y",
        status="open",
        started_at="2026-05-21T10:00:00",
    )
    
    aggregates = {
        "run_count": 10,
        "outcome_distribution": {"pass": 7, "fail": 3},
        "residual_rate": 0.1,
        "bypass_rate": 0.2,
        "unknown_rate": 0.0,
        "anomalies": ["High bypass rate"],
    }

    display = project_campaign(campaign, aggregates)

    assert display["id"] == "camp1234567890"
    assert display["id_short"] == "camp1234"
    assert display["name"] == "Phase 1"
    assert display["mode"] == "exploration"
    assert display["question"] == "What works?"
    assert display["hypothesis"] == "X is better than Y"
    assert display["status"] == "open"
    assert display["run_count"] == 10
    assert display["outcome_distribution"] == {"pass": 7, "fail": 3}
    assert display["residual_rate"] == 0.1
    assert display["bypass_rate"] == 0.2
    assert display["unknown_rate"] == 0.0
    assert display["anomalies"] == ["High bypass rate"]
```

**Step 2: Run tests to verify they fail**

```bash
cd /home/marielle/projects/bathos
pytest tests/test_viz_data.py::test_project_run_basic -xvs
```

Expected: `ModuleNotFoundError: No module named 'bathos.viz.data'`

**Step 3: Implement `viz/data.py`**

Create `/home/marielle/projects/bathos/src/bathos/viz/data.py`:

```python
"""Visualization data layer — projection from Run/Campaign to display TypedDicts."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, TypedDict

from bathos.schema import Run
from bathos.campaigns import Campaign


class RunDisplay(TypedDict):
    """Display representation of a Run for web/HTML rendering."""
    
    id: str
    id_short: str
    project_slug: str
    status: str
    exit_code: int
    duration_s: float
    duration_display: str
    timestamp: str
    command: str
    argv: list[str]
    hostname: str
    slurm_job_id: str
    git_hash: str
    git_hash_short: str
    git_branch: str
    git_dirty: bool
    script_sha256: str
    sidecar_path: str
    sidecar_mode: str
    agent_mode: str
    parent_run_id: str
    tags: list[str]
    output_paths: list[str]
    outcome: str
    outcome_is_residual: bool
    campaign_id: str
    campaign_name: str
    postmortem_status: str
    postmortem_hypothesis_status: str
    postmortem_verdict_override: str
    postmortem_author: str
    postmortem_summary: str
    postmortem_path: str
    postmortem_has_anomalies: bool
    postmortem_asset_links: dict[str, Any]


class CampaignDisplay(TypedDict):
    """Display representation of a Campaign for web/HTML rendering."""
    
    id: str
    id_short: str
    name: str
    mode: str
    question: str
    hypothesis: str
    status: str
    started_at: str
    concluded_at: str
    conclusion: str
    outcome_label: str
    parent_campaign_id: str
    run_count: int
    outcome_distribution: dict[str, int]
    residual_rate: float
    bypass_rate: float
    unknown_rate: float
    anomalies: list[str]


def _format_duration(duration_s: float) -> str:
    """Format duration in seconds as human-readable string."""
    if duration_s < 60:
        return f"{duration_s:.1f}s"
    else:
        minutes = int(duration_s // 60)
        seconds = int(duration_s % 60)
        return f"{minutes}m {seconds}s"


def project_run(run: Run, campaign_name: str = "") -> RunDisplay:
    """
    Project a Run object to a RunDisplay TypedDict for rendering.
    
    Args:
        run: Run object from schema
        campaign_name: Pre-resolved campaign name; empty string if not in a campaign
    
    Returns:
        RunDisplay TypedDict with all required fields populated
    """
    # Parse postmortem asset links
    postmortem_asset_links: dict[str, Any] = {}
    if run.postmortem_asset_links and run.postmortem_asset_links != "{}":
        try:
            postmortem_asset_links = json.loads(run.postmortem_asset_links)
        except (json.JSONDecodeError, TypeError):
            postmortem_asset_links = {}
    
    # Format timestamp
    timestamp_str = run.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")
    
    # Return TypedDict
    return RunDisplay(
        id=run.id,
        id_short=run.id[:8],
        project_slug=run.project_slug,
        status=run.status,
        exit_code=run.exit_code,
        duration_s=run.duration_s,
        duration_display=_format_duration(run.duration_s),
        timestamp=timestamp_str,
        command=run.command,
        argv=run.argv,
        hostname=run.hostname or "",
        slurm_job_id=run.slurm_job_id or "",
        git_hash=run.git_hash,
        git_hash_short=run.git_hash[:8],
        git_branch=run.git_branch,
        git_dirty=run.git_dirty,
        script_sha256=run.script_sha256 or "",
        sidecar_path=run.sidecar_path or "",
        sidecar_mode=run.sidecar_mode or "",
        agent_mode=run.agent_mode or "",
        parent_run_id=run.parent_run_id or "",
        tags=run.tags,
        output_paths=run.output_paths,
        outcome=run.outcome or "",
        outcome_is_residual=run.outcome_is_residual,
        campaign_id=run.campaign_id or "",
        campaign_name=campaign_name,
        postmortem_status=run.postmortem_status,
        postmortem_hypothesis_status=run.postmortem_hypothesis_status or "",
        postmortem_verdict_override=run.postmortem_verdict_override or "",
        postmortem_author=run.postmortem_author or "",
        postmortem_summary=run.postmortem_summary or "",
        postmortem_path=run.postmortem_path or "",
        postmortem_has_anomalies=run.postmortem_has_anomalies,
        postmortem_asset_links=postmortem_asset_links,
    )


def project_campaign(
    campaign: Campaign,
    aggregates: dict,
) -> CampaignDisplay:
    """
    Project a Campaign object to a CampaignDisplay TypedDict.
    
    Args:
        campaign: Campaign object
        aggregates: Dict with keys: run_count, outcome_distribution, residual_rate,
                    bypass_rate, unknown_rate, anomalies
    
    Returns:
        CampaignDisplay TypedDict
    """
    return CampaignDisplay(
        id=campaign.id,
        id_short=campaign.id[:8],
        name=campaign.name,
        mode=campaign.mode,
        question=campaign.question or "",
        hypothesis=campaign.hypothesis or "",
        status=campaign.status,
        started_at=campaign.started_at or "",
        concluded_at=campaign.concluded_at or "",
        conclusion=campaign.conclusion or "",
        outcome_label=campaign.outcome_label or "",
        parent_campaign_id=campaign.parent_campaign_id or "",
        run_count=aggregates.get("run_count", 0),
        outcome_distribution=aggregates.get("outcome_distribution", {}),
        residual_rate=aggregates.get("residual_rate", 0.0),
        bypass_rate=aggregates.get("bypass_rate", 0.0),
        unknown_rate=aggregates.get("unknown_rate", 0.0),
        anomalies=aggregates.get("anomalies", []),
    )
```

**Step 4: Run tests to verify they pass**

```bash
cd /home/marielle/projects/bathos
pytest tests/test_viz_data.py -v
```

Expected: All tests pass.

**Step 5: Commit**

```bash
cd /home/marielle/projects/bathos
git add src/bathos/viz/data.py tests/test_viz_data.py
git commit -m "feat(viz): implement data projection layer with RunDisplay and CampaignDisplay"
```

---

## Phase 4: Jinja2 Templates

**Dependency:** Phase 1 complete, Phase 3 recommended (to understand data schema)  
**Blocks:** HTML renderer and FastAPI server  
**Parallelism:** Can start independently  
**Risk:** Medium — template syntax requires careful testing in context; out of scope for unit tests

### Task 4.1: Implement Jinja2 templates (`index.html`, includes)

**Files:**
- Create: `/home/marielle/projects/bathos/src/bathos/viz/templates/index.html`
- Create: `/home/marielle/projects/bathos/src/bathos/viz/templates/_runs.html`
- Create: `/home/marielle/projects/bathos/src/bathos/viz/templates/_run_detail.html`
- Create: `/home/marielle/projects/bathos/src/bathos/viz/templates/_campaign.html`

**Steps:**

**Step 1: Create `index.html` — SPA shell**

Create `/home/marielle/projects/bathos/src/bathos/viz/templates/index.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>bathos — Experiment Tracking</title>
    <style>
        {{ pico_css | safe }}
    </style>
</head>
<body x-data="app()" x-init="init()">
    <nav>
        <ul>
            <li><strong>bathos</strong></li>
        </ul>
        <ul>
            <li><a href="#" @click.prevent="currentView = 'runs'" :class="currentView === 'runs' ? 'active' : ''">Runs</a></li>
            <li><a href="#" @click.prevent="currentView = 'campaigns'" :class="currentView === 'campaigns' ? 'active' : ''">Campaigns</a></li>
        </ul>
    </nav>

    {% if catalog_state == 'cool' %}
    <article style="background-color: #fff3cd; border: 1px solid #ffc107; padding: 1rem; margin-bottom: 1rem;">
        <p><strong>Note:</strong> Viewing cool-tier data — some fields may be absent. Run <code>bth compact</code> for the full catalog.</p>
    </article>
    {% endif %}

    {% if not runs %}
    <article>
        <h2>No Runs Found</h2>
        <p>Your catalog is empty. Start by running:</p>
        <pre><code>bth init --slug my-project
bth run python my_script.py</code></pre>
    </article>
    {% endif %}

    <div x-show="currentView === 'runs'" style="display: block;">
        {% include '_runs.html' %}
    </div>

    <div x-show="currentView === 'campaigns'" style="display: none;">
        {% include '_campaign.html' %}
    </div>

    <script>
        {{ alpine_js | safe }}
    </script>

    <script>
        function app() {
            return {
                currentView: 'runs',
                data: window.__BATHOS_DATA__ || { runs: [], campaigns: [], catalog_state: 'warm' },
                
                init() {
                    // Initialize view
                },
                
                expandRow(runId) {
                    const row = document.querySelector(`[data-run-id="${runId}"]`);
                    if (row) {
                        row.classList.toggle('expanded');
                    }
                }
            };
        }
    </script>
</body>
</html>
```

**Step 2: Create `_runs.html` — run list section**

Create `/home/marielle/projects/bathos/src/bathos/viz/templates/_runs.html`:

```html
<article>
    <h2>Runs</h2>

    {% if total_run_count > 1000 %}
    <p style="background-color: #e7f3ff; padding: 0.5rem; border-left: 3px solid #2196F3;">
        <strong>Info:</strong> Showing 1000 of {{ total_run_count }} runs. Use filters to narrow the view, or run <code>bth view --project &lt;slug&gt;</code> to scope to one project.
    </p>
    {% endif %}

    <table>
        <thead>
            <tr>
                <th>ID</th>
                <th>Project</th>
                <th>Status</th>
                <th>Outcome</th>
                <th>Duration</th>
                <th>Campaign</th>
                <th>Branch</th>
                <th>Timestamp</th>
            </tr>
        </thead>
        <tbody>
            {% for run in runs %}
            <tr data-run-id="{{ run.id }}" style="cursor: pointer;" @click="expandRow('{{ run.id }}')">
                <td><code>{{ run.id_short }}</code></td>
                <td>{{ run.project_slug }}</td>
                <td>
                    <span style="background-color: {% if run.status == 'completed' %}#28a745{% elif run.status == 'failed' %}#dc3545{% elif run.status == 'running' %}#ffc107{% else %}#6c757d{% endif %}; color: white; padding: 0.25rem 0.5rem; border-radius: 3px;">
                        {{ run.status }}
                    </span>
                </td>
                <td>
                    <span style="background-color: {% if run.outcome == 'pass' %}#28a745{% elif run.outcome == 'fail' %}#dc3545{% elif run.outcome == 'marginal' %}#ffc107{% else %}#6c757d{% endif %}; color: white; padding: 0.25rem 0.5rem; border-radius: 3px;">
                        {{ run.outcome or 'unknown' }}
                    </span>
                </td>
                <td>{{ run.duration_display }}</td>
                <td>{{ run.campaign_name or '-' }}</td>
                <td><code>{{ run.git_branch }}</code></td>
                <td>{{ run.timestamp }}</td>
            </tr>
            <tr data-run-detail="{{ run.id }}" style="display: none;">
                <td colspan="8">
                    {% include '_run_detail.html' %}
                </td>
            </tr>
            {% endfor %}
        </tbody>
    </table>
</article>
```

**Step 3: Create `_run_detail.html` — run detail panel**

Create `/home/marielle/projects/bathos/src/bathos/viz/templates/_run_detail.html`:

```html
<div style="padding: 1rem; background-color: #f9f9f9; border: 1px solid #e0e0e0; border-radius: 4px;">
    <h4>Run Detail: {{ run.id_short }}</h4>

    <h5>Execution</h5>
    <dl>
        <dt>Command:</dt>
        <dd><code>{{ run.command }}</code></dd>
        <dt>Hostname:</dt>
        <dd>{{ run.hostname }}</dd>
        <dt>SLURM Job:</dt>
        <dd>{{ run.slurm_job_id or '(none)' }}</dd>
        <dt>Duration:</dt>
        <dd>{{ run.duration_display }}</dd>
        <dt>Exit Code:</dt>
        <dd>{{ run.exit_code }}</dd>
    </dl>

    <h5>Provenance</h5>
    <dl>
        <dt>Git Hash:</dt>
        <dd><code>{{ run.git_hash_short }}</code> ({{ run.git_branch }}) {% if run.git_dirty %}[dirty]{% endif %}</dd>
        <dt>Script SHA:</dt>
        <dd><code>{{ run.script_sha256[:16] }}...</code></dd>
        <dt>Sidecar:</dt>
        <dd>{{ run.sidecar_mode or 'none' }} @ {{ run.sidecar_path or '(none)' }}</dd>
        <dt>Agent Mode:</dt>
        <dd>{{ run.agent_mode or 'none' }}</dd>
        <dt>Parent Run:</dt>
        <dd>{{ run.parent_run_id[:8] if run.parent_run_id else '(none)' }}</dd>
    </dl>

    <h5>Outcome</h5>
    <dl>
        <dt>Status:</dt>
        <dd>{{ run.status }}</dd>
        <dt>Outcome:</dt>
        <dd>{{ run.outcome or 'unknown' }} {% if run.outcome_is_residual %}[residual]{% endif %}</dd>
        {% if run.tags %}
        <dt>Tags:</dt>
        <dd>{{ run.tags | join(', ') }}</dd>
        {% endif %}
        {% if run.output_paths %}
        <dt>Output Paths:</dt>
        <dd>
            <ul>
                {% for path in run.output_paths %}
                <li><code>{{ path }}</code></li>
                {% endfor %}
            </ul>
        </dd>
        {% endif %}
    </dl>

    {% if run.postmortem_status != 'unassigned' %}
    <h5>Postmortem</h5>
    <dl>
        <dt>Status:</dt>
        <dd>{{ run.postmortem_status }}</dd>
        <dt>Hypothesis Status:</dt>
        <dd>{{ run.postmortem_hypothesis_status }}</dd>
        <dt>Author:</dt>
        <dd>{{ run.postmortem_author or '(unassigned)' }}</dd>
        <dt>Summary:</dt>
        <dd>{{ run.postmortem_summary or '(none)' }}</dd>
        <dt>Path:</dt>
        <dd><code>{{ run.postmortem_path }}</code></dd>
        {% if run.postmortem_asset_links %}
        <dt>Assets:</dt>
        <dd>
            <ul>
                {% for key, value in run.postmortem_asset_links.items() %}
                <li><code>{{ key }}</code>: {{ value }}</li>
                {% endfor %}
            </ul>
        </dd>
        {% endif %}
    </dl>
    {% endif %}
</div>
```

**Step 4: Create `_campaign.html` — campaign section**

Create `/home/marielle/projects/bathos/src/bathos/viz/templates/_campaign.html`:

```html
<article>
    <h2>Campaigns</h2>

    {% if campaigns %}
    <table>
        <thead>
            <tr>
                <th>ID</th>
                <th>Name</th>
                <th>Mode</th>
                <th>Status</th>
                <th>Started</th>
                <th>Runs</th>
            </tr>
        </thead>
        <tbody>
            {% for campaign in campaigns %}
            <tr>
                <td><code>{{ campaign.id_short }}</code></td>
                <td><strong>{{ campaign.name }}</strong></td>
                <td>{{ campaign.mode }}</td>
                <td>{{ campaign.status }}</td>
                <td>{{ campaign.started_at[:19] }}</td>
                <td>{{ campaign.run_count }}</td>
            </tr>
            <tr>
                <td colspan="6">
                    {% if campaign.question %}
                    <p><strong>Question:</strong> {{ campaign.question }}</p>
                    {% endif %}
                    {% if campaign.hypothesis %}
                    <p><strong>Hypothesis:</strong> {{ campaign.hypothesis }}</p>
                    {% endif %}

                    <h5>Outcome Distribution</h5>
                    <table>
                        <thead>
                            <tr>
                                <th>Outcome</th>
                                <th>Count</th>
                            </tr>
                        </thead>
                        <tbody>
                            {% for outcome, count in campaign.outcome_distribution.items() %}
                            <tr>
                                <td>{{ outcome }}</td>
                                <td>{{ count }}</td>
                            </tr>
                            {% endfor %}
                        </tbody>
                    </table>

                    {% if campaign.anomalies %}
                    <h5 style="color: #ff9800;">⚠ Anomalies</h5>
                    <ul style="background-color: #fff3cd; padding: 1rem; border-left: 3px solid #ff9800;">
                        {% for anomaly in campaign.anomalies %}
                        <li>{{ anomaly }}</li>
                        {% endfor %}
                    </ul>
                    {% endif %}
                </td>
            </tr>
            {% endfor %}
        </tbody>
    </table>
    {% else %}
    <p>No campaigns yet.</p>
    {% endif %}
</article>
```

**Step 5: Verify templates are valid Jinja2**

Create a quick validation script `/tmp/test_jinja.py`:

```python
from jinja2 import Environment, PackageLoader

try:
    env = Environment(loader=PackageLoader("bathos.viz", "templates"))
    tmpl = env.get_template("index.html")
    print("PASS: Jinja2 templates are syntactically valid")
except Exception as e:
    print(f"FAIL: {e}")
    exit(1)
```

Run:
```bash
cd /home/marielle/projects/bathos
python /tmp/test_jinja.py
```

Expected: PASS message.

**Step 6: Commit**

```bash
cd /home/marielle/projects/bathos
git add src/bathos/viz/templates/
git commit -m "feat(viz): add jinja2 templates (index, runs list, run detail, campaigns)"
```

---

## Phase 5: HTML Renderer (`viz/html.py`) and Static Export

**Dependency:** Phase 1, 3, 4 complete  
**Blocks:** `bth export --html` CLI command  
**Parallelism:** Can start after Phase 4  
**Risk:** Medium — file size estimation and Jinja2 error handling are critical

### Task 5.1: Implement `viz/html.py` with static HTML rendering

**Files:**
- Create: `/home/marielle/projects/bathos/src/bathos/viz/html.py`
- Create: `/home/marielle/projects/bathos/tests/test_viz_html.py`

**Steps:**

**Step 1: Write failing test for HTML rendering**

Create `/home/marielle/projects/bathos/tests/test_viz_html.py`:

```python
"""Integration tests for viz/html.py static HTML rendering."""

import json
from datetime import UTC, datetime

import pytest

from bathos.schema import Run
from bathos.viz.html import render_html_report


def test_render_html_report_basic():
    """Test that render_html_report produces valid HTML with data embedded."""
    runs = [
        Run(
            id="abc123def456",
            project_slug="test-proj",
            command="python test.py",
            argv=["python", "test.py"],
            git_hash="a1b2c3d4",
            git_branch="main",
            git_dirty=False,
            timestamp=datetime(2026, 5, 21, 13, 47, 23, tzinfo=UTC),
            status="completed",
            exit_code=0,
            duration_s=7.3,
            outcome="pass",
            campaign_id="",
        )
    ]

    html = render_html_report(runs, campaigns=[])

    # Check for HTML structure
    assert "<!DOCTYPE html>" in html
    assert "</html>" in html
    assert "<title>bathos</title>" in html or "bathos" in html

    # Check for data blob
    assert "window.__BATHOS_DATA__" in html
    assert json.dumps("abc123def456") in html or "abc123de" in html

    # Check for Alpine.js embedded
    assert "Alpine" in html

    # Check for Pico CSS embedded
    assert "body" in html or "css" in html.lower()


def test_render_html_report_empty():
    """Test that render_html_report handles empty run list gracefully."""
    html = render_html_report([], campaigns=[])

    assert "<!DOCTYPE html>" in html
    assert "No Runs Found" in html or "no runs" in html.lower()
```

**Step 2: Run test to verify it fails**

```bash
cd /home/marielle/projects/bathos
pytest tests/test_viz_html.py::test_render_html_report_basic -xvs
```

Expected: `ModuleNotFoundError` or import error.

**Step 3: Implement `viz/html.py`**

Create `/home/marielle/projects/bathos/src/bathos/viz/html.py`:

```python
"""Static HTML export for bathos catalog."""

from __future__ import annotations

import importlib.resources
import json
import sys
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, PackageLoader, TemplateError

from bathos.schema import Run
from bathos.campaigns import Campaign
from bathos.viz.data import project_run, project_campaign, RunDisplay, CampaignDisplay


def _load_static_asset(filename: str) -> str:
    """Load a static asset (JS, CSS) via importlib.resources."""
    try:
        ref = importlib.resources.files("bathos.viz").joinpath("static", filename)
        return ref.read_text(encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"Failed to load static asset {filename}: {e}")


def _project_campaigns(
    campaigns: list[Campaign],
    catalog_dir: Path | None = None,
) -> list[CampaignDisplay]:
    """Stub — replaced in Task 5.2 with real aggregation logic."""
    return []


def render_html_report(
    runs: list[Run],
    campaigns: list[Campaign] | None = None,
    catalog_state: str = "warm",
    catalog_dir: Path | None = None,
    total_run_count: int | None = None,
) -> str:
    """
    Render a complete HTML report with embedded data, Alpine.js, and Pico CSS.
    
    Args:
        runs: List of Run objects to display
        campaigns: List of Campaign objects; defaults to []
        catalog_state: 'warm' or 'cool' — determines banner messaging
        catalog_dir: Path to catalog dir for campaign aggregation; None skips aggregation
        total_run_count: Total runs in catalog (may exceed len(runs) if capped); defaults to len(runs)
    
    Returns:
        Complete HTML as a string
    
    Raises:
        TemplateError: If Jinja2 template rendering fails
        RuntimeError: If static assets cannot be loaded
    """
    if campaigns is None:
        campaigns = []
    
    # Load static assets
    try:
        alpine_js = _load_static_asset("alpine.min.js")
        pico_css = _load_static_asset("pico.min.css")
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise
    
    # Project runs and campaigns to display TypedDicts
    run_displays: list[RunDisplay] = [
        project_run(run, campaign_name="")
        for run in runs
    ]
    
    # Campaign aggregation — see Task 5.2 which completes this step
    campaign_displays: list[CampaignDisplay] = _project_campaigns(campaigns, catalog_dir)
    
    # Construct data blob
    data_blob = {
        "runs": run_displays,
        "campaigns": campaign_displays,
        "catalog_state": catalog_state,
    }
    
    # Render template
    try:
        env = Environment(loader=PackageLoader("bathos.viz", "templates"))
        template = env.get_template("index.html")
        
        html = template.render(
            alpine_js=alpine_js,
            pico_css=pico_css,
            catalog_state=catalog_state,
            runs=run_displays,
            campaigns=campaign_displays,
            total_run_count=total_run_count if total_run_count is not None else len(run_displays),
        )
    except TemplateError as e:
        print(f"Template error: {e}", file=sys.stderr)
        raise
    
    # Inject data blob as JavaScript
    data_injection = f"\n    <script>\n        window.__BATHOS_DATA__ = {json.dumps(data_blob)};\n    </script>\n"
    html = html.replace("</head>", data_injection + "</head>", 1)
    
    return html


def estimate_html_size(html: str) -> float:
    """Estimate HTML size in MB."""
    return len(html) / (1024 * 1024)


def export_html(
    runs: list[Run],
    campaigns: list[Campaign] | None = None,
    output_path: str | None = None,
    catalog_dir: Path | None = None,
) -> tuple[str, bool]:
    """
    Render and export HTML report to file.
    
    Args:
        runs: List of Run objects
        campaigns: List of Campaign objects
        output_path: Path to write HTML; defaults to "report.html"
        catalog_dir: Path to catalog dir for campaign aggregation
    
    Returns:
        Tuple of (output_path, size_warning_issued)
    """
    if output_path is None:
        output_path = "report.html"
    
    html = render_html_report(runs, campaigns=campaigns, catalog_dir=catalog_dir)
    size_mb = estimate_html_size(html)
    
    # Warn if > 5 MB
    size_warning = False
    if size_mb > 5.0:
        print(
            f"Warning: Report is {size_mb:.1f} MB. "
            f"Consider adding --project or --campaign to reduce size.",
            file=sys.stderr,
        )
        size_warning = True
    
    # Write file
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    
    return output_path, size_warning
```

**Step 4: Run tests**

```bash
cd /home/marielle/projects/bathos
pytest tests/test_viz_html.py -v
```

Expected: Both tests pass.

**Step 5: Commit**

```bash
cd /home/marielle/projects/bathos
git add src/bathos/viz/html.py tests/test_viz_html.py
git commit -m "feat(viz): implement static HTML renderer with embedded JS/CSS"
```

---

### Task 5.2: Implement campaign aggregation in `viz/html.py`

**Files:**
- Modify: `/home/marielle/projects/bathos/src/bathos/viz/html.py`
- Modify: `/home/marielle/projects/bathos/tests/test_viz_html.py`

**Dependency:** Task 5.1 complete, Phase 3 complete (`viz/data.py` has `project_campaign`)

**Steps:**

**Step 1: Write failing test for campaign projection**

Add to `tests/test_viz_html.py`:

```python
def test_render_html_report_with_campaign():
    """Test that campaign data appears in rendered HTML."""
    from bathos.campaigns import Campaign
    campaign = Campaign(
        id="camp-001",
        project_slug="test-proj",
        name="Baseline sweep",
        mode="exploration",
        question="Does NVT hold at 300K?",
        hypothesis="Yes",
        status="open",
        started_at="2026-05-21T10:00:00Z",
    )
    html = render_html_report([], campaigns=[campaign])
    assert "Baseline sweep" in html
```

**Step 2: Implement `_project_campaigns()` helper in `viz/html.py`**

Add this function to `viz/html.py` (above `render_html_report`):

```python
def _project_campaigns(
    campaigns: list[Campaign],
    catalog_dir: Path | None = None,
) -> list[CampaignDisplay]:
    """Project Campaign objects to CampaignDisplay TypedDicts with aggregated run stats."""
    displays: list[CampaignDisplay] = []
    for campaign in campaigns:
        if catalog_dir is not None:
            try:
                import duckdb
                agg_sql = """
                    SELECT outcome, COUNT(*) AS n,
                      COUNT(*) FILTER (WHERE outcome_is_residual) AS n_residual,
                      COUNT(*) FILTER (WHERE sidecar_mode = 'bypassed') AS n_bypassed,
                      COUNT(*) FILTER (WHERE outcome IN ('unknown', '')) AS n_unknown
                    FROM runs WHERE campaign_id = ? GROUP BY outcome
                """
                # Open a read-only connection directly — run_sql() does not accept params
                conn = duckdb.connect(str(catalog_dir / "bathos.db"), read_only=True)
                rows = conn.execute(agg_sql, [campaign.id]).fetchall()
                col_names = [d[0] for d in conn.description]
                row_dicts = [dict(zip(col_names, row)) for row in rows]
                conn.close()
                run_count = sum(r["n"] for r in row_dicts)
                outcome_distribution = {r["outcome"]: r["n"] for r in row_dicts}
                n_residual = sum(r["n_residual"] for r in row_dicts)
                n_bypassed = sum(r["n_bypassed"] for r in row_dicts)
                n_unknown = sum(r["n_unknown"] for r in row_dicts)
                residual_rate = n_residual / run_count if run_count else 0.0
                bypass_rate = n_bypassed / run_count if run_count else 0.0
                unknown_rate = n_unknown / run_count if run_count else 0.0
                # review_campaign(db, campaign_id) — out of scope for v1 anomaly detection
            except Exception:
                run_count = 0
                outcome_distribution = {}
                residual_rate = bypass_rate = unknown_rate = 0.0
        else:
            run_count = 0
            outcome_distribution = {}
            residual_rate = bypass_rate = unknown_rate = 0.0

        displays.append(project_campaign(
            campaign,
            {
                "run_count": run_count,
                "outcome_distribution": outcome_distribution,
                "residual_rate": residual_rate,
                "bypass_rate": bypass_rate,
                "unknown_rate": unknown_rate,
                "anomalies": [],
            },
        ))
    return displays
```

**Step 3: Verify `render_html_report` signature already includes `catalog_dir` and `total_run_count`**

The signature was established in Task 5.1 as:
```python
def render_html_report(
    runs: list[Run],
    campaigns: list[Campaign] | None = None,
    catalog_state: str = "warm",
    catalog_dir: Path | None = None,
    total_run_count: int | None = None,
) -> str:
```

Confirm the body calls `_project_campaigns(campaigns, catalog_dir)` — this is already wired in Task 5.1. No signature change needed here.

**Step 4: Run tests**

```bash
cd /home/marielle/projects/bathos
pytest tests/test_viz_html.py -xvs
```

Expected: all 3 tests pass, including the new campaign test.

**Step 5: Commit**

```bash
cd /home/marielle/projects/bathos
git add src/bathos/viz/html.py tests/test_viz_html.py
git commit -m "feat(viz): implement campaign aggregation in html renderer"
```

---

## Phase 6: FastAPI Server (`viz/server.py`) and CLI Integration

**Dependency:** Phase 1–5 complete  
**Blocks:** None (completes feature)  
**Parallelism:** None  
**Risk:** High — port binding and uvicorn error handling are critical

### Task 6.1: Implement `viz/server.py` FastAPI application

**Files:**
- Create: `/home/marielle/projects/bathos/src/bathos/viz/server.py`
- Create: `/home/marielle/projects/bathos/tests/test_viz_server.py`

**Steps:**

**Step 1: Write failing test for FastAPI server**

Create `/home/marielle/projects/bathos/tests/test_viz_server.py`:

```python
"""Tests for viz/server.py FastAPI application."""

import sys
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from bathos.schema import Run
from bathos.viz.server import create_app


def test_fastapi_app_get_root():
    """Test that GET / returns 200 with run data in response."""
    runs = [
        Run(
            id="abc123def456",
            project_slug="test-proj",
            command="python test.py",
            argv=["python", "test.py"],
            git_hash="a1b2c3d4",
            git_branch="main",
            git_dirty=False,
            timestamp=datetime(2026, 5, 21, 13, 47, 23, tzinfo=UTC),
        )
    ]
    
    app = create_app(runs=runs, campaigns=[])
    client = TestClient(app)
    
    response = client.get("/")
    
    assert response.status_code == 200
    assert "abc123de" in response.text or "test-proj" in response.text


def test_fastapi_app_empty():
    """Test that app handles empty catalog gracefully."""
    app = create_app(runs=[], campaigns=[])
    client = TestClient(app)
    
    response = client.get("/")
    
    assert response.status_code == 200
    assert "No Runs Found" in response.text or "catalog is empty" in response.text.lower()
```

**Step 2: Run test to verify it fails**

```bash
cd /home/marielle/projects/bathos
pytest tests/test_viz_server.py::test_fastapi_app_get_root -xvs
```

Expected: Import error or `ModuleNotFoundError`.

**Step 3: Implement `viz/server.py`**

Create `/home/marielle/projects/bathos/src/bathos/viz/server.py`:

```python
"""FastAPI server for local visualization dashboard."""

from __future__ import annotations

import webbrowser
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
import importlib.resources
from pathlib import Path

from bathos.schema import Run
from bathos.campaigns import Campaign
from bathos.viz.html import render_html_report


def create_app(
    runs: list[Run],
    campaigns: list[Campaign] | None = None,
    total_run_count: int | None = None,
) -> FastAPI:
    """
    Create a FastAPI application for visualization.
    
    Args:
        runs: List of Run objects to display (may be capped at 1000)
        campaigns: List of Campaign objects
        total_run_count: Actual catalog size; used to trigger the sticky banner
    
    Returns:
        Configured FastAPI app
    """
    if campaigns is None:
        campaigns = []
    
    app = FastAPI(title="bathos dashboard")
    
    # Render HTML once at startup; per-request rendering is out of scope for v1
    html_content = render_html_report(
        runs,
        campaigns=campaigns,
        total_run_count=total_run_count,
    )
    
    @app.get("/", response_class=str)
    def root():
        """Serve the main HTML page."""
        from fastapi.responses import HTMLResponse
        return HTMLResponse(content=html_content)
    
    # Serve static files if available
    try:
        static_dir = importlib.resources.files("bathos.viz").joinpath("static")
        if static_dir.is_dir():
            app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")
    except Exception:
        pass  # Static files optional in dev
    
    return app


def run_server(
    runs: list[Run],
    campaigns: list[Campaign] | None = None,
    total_run_count: int | None = None,
    host: str = "127.0.0.1",
    port: int = 8080,
    open_browser: bool = True,
) -> None:
    """
    Run the visualization server and optionally open a browser tab.
    
    Args:
        runs: List of Run objects (may be capped at 1000)
        campaigns: List of Campaign objects
        total_run_count: Actual catalog size for sticky banner
        host: Host to bind to
        port: Port to bind to
        open_browser: Whether to open browser automatically
    
    Raises:
        OSError: If port is already in use
    """
    import uvicorn
    
    app = create_app(runs, campaigns=campaigns, total_run_count=total_run_count)
    
    # Try to open browser
    if open_browser:
        import threading
        def open_browser_thread():
            import time
            time.sleep(1)  # Give server time to start
            webbrowser.open(f"http://{host}:{port}")
        
        thread = threading.Thread(target=open_browser_thread, daemon=True)
        thread.start()
    
    # Run server
    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    except OSError as e:
        if "Address already in use" in str(e) or port in str(e):
            raise OSError(
                f"Port {port} is already in use. Try: bth view --port {port + 1}"
            ) from e
        raise
```

**Step 4: Run tests**

```bash
cd /home/marielle/projects/bathos
pytest tests/test_viz_server.py -v
```

Expected: Both tests pass.

**Step 5: Commit**

```bash
cd /home/marielle/projects/bathos
git add src/bathos/viz/server.py tests/test_viz_server.py
git commit -m "feat(viz): implement FastAPI server with HTML rendering"
```

---

### Task 6.2: Integrate `bth view` and `bth export --html` CLI commands

**Files:**
- Modify: `/home/marielle/projects/bathos/src/bathos/cli.py`

**Steps:**

**Step 1: Add `bth view` command**

In `/home/marielle/projects/bathos/src/bathos/cli.py`, after the `@app.command()` decorators, add:

```python
@app.command()
def view(
    port: int = typer.Option(8080, "--port", "-p", help="Port to bind to"),
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Host to bind to"),
    no_open: bool = typer.Option(False, "--no-open", help="Do not open browser automatically"),
    project: str | None = typer.Option(None, "--project", help="Scope to single project"),
):
    """Launch a local FastAPI dashboard to visualize runs and campaigns."""
    try:
        from bathos.viz.server import run_server
    except ImportError:
        typer.echo(
            "Error: bathos[viz] is not installed.\n"
            "Install with: uv tool install 'bathos[viz]'",
            err=True,
        )
        raise typer.Exit(1)
    
    from bathos.query import list_runs
    
    catalog = _catalog_dir()
    # Query 1001 so we can detect truncation without an extra COUNT query
    runs = list_runs(catalog, project=project, limit=1001)
    total_run_count = len(runs)
    runs = runs[:1000]  # cap at 1000 for client-side rendering
    
    if not runs:
        typer.echo("No runs found in catalog.", err=True)
        raise typer.Exit(1)
    
    try:
        run_server(runs, total_run_count=total_run_count, host=host, port=port, open_browser=not no_open)
    except OSError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)
```

**Step 2: Add `--html` flag to the existing `export_cmd`**

The existing `export_cmd` (registered as `bth export`) handles MCP/skill registration. We add an `--html` flag that, when set, routes to the HTML renderer instead. This preserves the spec surface `bth export --html` without naming conflict or a second command.

Find `def export_cmd(` in `/home/marielle/projects/bathos/src/bathos/cli.py` and update it:

```python
@app.command("export")
def export_cmd(
    tool: str = typer.Option("claude", "--tool", "-t", help="Target tool: claude or gemini"),
    level: str = typer.Option("user", "--level", "-l", help="Install level: user, workspace, or system"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print what would happen without writing"),
    html: bool = typer.Option(False, "--html", help="Export catalog as a self-contained HTML report"),
    out: str = typer.Option("report.html", "--out", "-o", help="Output file for --html export"),
    project: str | None = typer.Option(None, "--project", help="Filter by project (--html only)"),
    campaign: str | None = typer.Option(None, "--campaign", help="Filter by campaign (--html only)"),
):
    """Export the using-bathos skill and register MCP server, or export catalog as HTML."""
    if html:
        try:
            from bathos.viz.html import export_html as do_export
        except ImportError:
            typer.echo(
                "Error: bathos[viz] is not installed.\n"
                "Install with: uv tool install 'bathos[viz]'",
                err=True,
            )
            raise typer.Exit(1)

        from bathos.query import list_runs

        catalog = _catalog_dir()
        runs = list_runs(catalog, project=project)
        if campaign:
            runs = [r for r in runs if r.campaign_id == campaign]

        if not runs:
            typer.echo(f"No matching runs. Writing empty report to {out}.", err=True)

        path, size_warned = do_export(runs, output_path=out, catalog_dir=catalog)
        typer.echo(f"Exported to {path}")
        if size_warned:
            typer.echo("(Use --project or --campaign to reduce file size)", err=True)
        return

    # Original MCP/skill export path (unchanged)
    from bathos.export import ExportError, export_skill, register_mcp, resolve_target

    try:
        target = resolve_target(tool=tool, level=level)
    except ExportError as e:
        typer.echo(f"Error: {e}", err=True)
        raise typer.Exit(1)

    result = export_skill(target=target, dry_run=dry_run)
    mcp_target = register_mcp(tool=tool, level=level, dry_run=dry_run)

    if dry_run:
        typer.echo(f"Dry run — would write skill to:  {result.target}")
        typer.echo(f"Dry run — would register MCP at: {mcp_target}")
    else:
        typer.echo(f"Exported skill to:    {result.target}")
        typer.echo(f"Registered MCP at:   {mcp_target}")
```

**Verification:** After adding the flag, confirm that the existing MCP/skill export path still works by checking that `bth export --tool claude` continues to call `export_skill`. The `html=False` default preserves all existing behaviour.

**Step 3: Verify CLI still works**

```bash
cd /home/marielle/projects/bathos
python -m bathos.cli --version
```

Expected: Version output.

**Step 4: Run existing CLI tests to ensure no regressions**

```bash
cd /home/marielle/projects/bathos
pytest tests/test_cli.py -v -k "not integration"
```

Expected: All tests pass.

**Step 5: Commit**

```bash
cd /home/marielle/projects/bathos
git add src/bathos/cli.py
git commit -m "feat(cli): add 'bth view' and 'bth export --html' commands with viz dependencies"
```

---

### Task 6.3: Integrate Rich CLI formatters into existing commands

**Files:**
- Modify: `/home/marielle/projects/bathos/src/bathos/cli.py`

**Steps:**

**Step 1: Update `bth ls` body to use Rich formatter — preserve all existing options**

Do NOT replace `ls_cmd`. The existing signature has `--project`, `--since`, `--status`, `--limit` and uses `find_runs()`. Preserve all of these and the compaction banner. Replace only the manual `header`/`typer.echo` row loop with a `render_runs_table()` call.

Before making any changes, run the existing tests as a baseline:

```bash
cd /home/marielle/projects/bathos
pytest tests/ -k "ls" -v --tb=short 2>&1 | tee /tmp/ls_baseline.txt
```

Then update `ls_cmd` body in `src/bathos/cli.py` from this:

```python
    if not runs:
        typer.echo("No runs found.")
        return
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

To this (import `render_runs_table` inside the function body, below existing imports):

```python
    from bathos.rich_fmt import render_runs_table

    if not runs:
        typer.echo("No runs found.")
        return
    render_runs_table(runs)
```

The `--since`, `--status`, `--limit`, `find_runs()` call, and compaction banner remain untouched. Only the output loop is replaced.

After the change, verify no regressions:

```bash
cd /home/marielle/projects/bathos
pytest tests/ -k "ls" -v --tb=short
```

Expected: same tests pass as baseline.

**Step 2: Update `bth show` to use Rich formatter**

Find the `show` command and update it:

```python
@app.command()
def show(run_id: str = typer.Argument(..., help="Run ID")):
    """Show detailed run information."""
    from bathos.query import get_run
    from bathos.rich_fmt import render_run_detail
    
    catalog = _catalog_dir()
    run = get_run(run_id, catalog)
    
    if run is None:
        typer.echo(f"Run {run_id} not found.", err=True)
        raise typer.Exit(1)
    
    render_run_detail(run)
```

**Step 3: Update `bth campaign ls` to use Rich formatter**

In the campaign_app subcommand group, update the list command:

```python
@campaign_app.command(name="ls")
def campaign_ls(
    project: str | None = typer.Option(None, "--project", help="Filter by project"),
    status: str | None = typer.Option(None, "--status", help="Filter by status (open/concluded)"),
):
    """List campaigns."""
    from bathos.campaigns import list_campaigns
    from bathos.rich_fmt import render_campaign_table
    
    catalog = _catalog_dir()
    db = duckdb.connect(str(catalog / "bathos.db"))
    
    campaigns = list_campaigns(db, project_slug=project, status=status)
    render_campaign_table(campaigns)
```

**Step 4: Update `bth campaign review` to use Rich formatter**

```python
@campaign_app.command(name="review")
def campaign_review(campaign_id: str = typer.Argument(..., help="Campaign ID")):
    """Review campaign outcomes and anomalies."""
    from bathos.campaigns import get_campaign, review_campaign
    from bathos.rich_fmt import render_campaign_review
    
    catalog = _catalog_dir()
    db = duckdb.connect(str(catalog / "bathos.db"))
    
    campaign = get_campaign(db, campaign_id)
    if campaign is None:
        typer.echo(f"Campaign {campaign_id} not found.", err=True)
        raise typer.Exit(1)
    
    review = review_campaign(db, campaign_id)
    render_campaign_review(campaign, review)
```

**Step 5: Verify CLI still works**

```bash
cd /home/marielle/projects/bathos
python -m bathos.cli --help | head -20
```

Expected: Help output with new commands visible.

**Step 6: Commit**

```bash
cd /home/marielle/projects/bathos
git add src/bathos/cli.py
git commit -m "feat(cli): integrate rich formatters into ls, show, campaign ls, campaign review"
```

---

## Phase 7: Testing and Packaging (P0 — Verification Gate)

**Dependency:** All Phase 1–6 complete  
**Blocks:** Release  
**Parallelism:** Sequential  
**Risk:** Critical — packaging must work in CI and on clean install

### Task 7.1: Write comprehensive test suite for viz module

**Files:**
- Create/Modify: `/home/marielle/projects/bathos/tests/test_viz_packaging.py`

**Steps:**

**Step 1: Create packaging verification test**

Create `/home/marielle/projects/bathos/tests/test_viz_packaging.py`:

```python
"""Tests for viz module packaging and asset availability."""

import importlib.resources
import pytest


def test_viz_templates_exist():
    """Verify all template files are packaged."""
    template_files = [
        "index.html",
        "_runs.html",
        "_run_detail.html",
        "_campaign.html",
    ]
    
    templates = importlib.resources.files("bathos.viz").joinpath("templates")
    for filename in template_files:
        assert (templates / filename).is_file(), f"Template {filename} not found"


def test_viz_static_assets_exist():
    """Verify static assets (JS, CSS) are packaged."""
    static_files = ["alpine.min.js", "pico.min.css"]
    
    static = importlib.resources.files("bathos.viz").joinpath("static")
    for filename in static_files:
        asset = static / filename
        assert asset.is_file(), f"Static asset {filename} not found"
        # Verify file is non-empty
        content = asset.read_text(encoding="utf-8")
        assert len(content) > 0, f"Static asset {filename} is empty"


def test_viz_versions_md_exists():
    """Verify VERSIONS.md with license attribution exists."""
    static = importlib.resources.files("bathos.viz").joinpath("static")
    versions = static / "VERSIONS.md"
    assert versions.is_file(), "VERSIONS.md not found"
    
    content = versions.read_text(encoding="utf-8")
    assert "MIT" in content, "VERSIONS.md missing MIT license reference"
    assert "Alpine" in content, "VERSIONS.md missing Alpine attribution"
    assert "Pico" in content, "VERSIONS.md missing Pico attribution"


def test_viz_data_imports():
    """Verify viz.data module exports required TypedDicts."""
    from bathos.viz.data import RunDisplay, CampaignDisplay, project_run, project_campaign
    
    assert RunDisplay is not None
    assert CampaignDisplay is not None
    assert callable(project_run)
    assert callable(project_campaign)


def test_viz_html_imports():
    """Verify viz.html module is importable and functional."""
    from bathos.viz.html import render_html_report, export_html
    
    assert callable(render_html_report)
    assert callable(export_html)


def test_viz_server_imports():
    """Verify viz.server module is importable."""
    from bathos.viz.server import create_app, run_server
    
    assert callable(create_app)
    assert callable(run_server)
```

**Step 2: Run all viz tests**

```bash
cd /home/marielle/projects/bathos
pytest tests/test_viz_*.py -v
```

Expected: All tests pass (may require `uv run pytest` if dependencies not installed).

**Step 3: Commit**

```bash
cd /home/marielle/projects/bathos
git add tests/test_viz_packaging.py
git commit -m "test(viz): add comprehensive packaging and import verification tests"
```

---

### Task 7.2: Run full test suite and verify no regressions

**Steps:**

**Step 1: Install dev dependencies**

```bash
cd /home/marielle/projects/bathos
uv sync --group dev
```

Expected: All dependencies installed.

**Step 2: Run full test suite**

```bash
cd /home/marielle/projects/bathos
uv run pytest tests/ -v --tb=short 2>&1 | tee /tmp/test-results.txt
```

Expected: 348+ tests passing, 0 failures.

**Step 3: Check for regressions**

```bash
grep -E "^(PASSED|FAILED)" /tmp/test-results.txt | wc -l
grep "FAILED" /tmp/test-results.txt || echo "No failures detected"
```

Expected: All tests passing.

**Step 4: Commit**

```bash
cd /home/marielle/projects/bathos
git status
# (Ensure all changes are staged and committed)
```

---

### Task 7.3: Build and test installation with `uv tool`

**Steps:**

**Step 1: Build wheel**

```bash
cd /home/marielle/projects/bathos
python -m pip install build
python -m build --wheel
```

Expected: `dist/bathos-*.whl` created.

**Step 2: Test base installation (without viz extra)**

```bash
cd /tmp
python -m venv test_bathos
source test_bathos/bin/activate
pip install /home/marielle/projects/bathos/dist/bathos-*.whl
bth --version
```

Expected: Version output, `bth` command works.

**Step 3: Test viz extra installation**

```bash
source /tmp/test_bathos/bin/activate
pip install /home/marielle/projects/bathos/dist/bathos-*.whl[viz]
python -c "from bathos.viz.server import create_app; print('PASS: viz extra installed')"
```

Expected: PASS message, imports work.

**Step 4: Clean up**

```bash
rm -rf /tmp/test_bathos
```

---

### Task 7.4: Update CI workflow to run packaging test against installed wheel

**Files:**
- Modify: `/home/marielle/projects/bathos/.github/workflows/ci.yml` (or whichever CI file exists)

**Steps:**

**Step 1: Locate the CI configuration**

```bash
find /home/marielle/projects/bathos/.github -name "*.yml" | head -5
```

**Step 2: Add a `test-packaging` job**

Add a job that builds the wheel and runs `test_viz_packaging.py` against the installed package, not the source tree:

```yaml
test-packaging:
  runs-on: ubuntu-latest
  needs: [test]  # run after unit tests pass
  steps:
    - uses: actions/checkout@v4
    - uses: astral-sh/setup-uv@v3
    - name: Build wheel
      run: uv build --wheel
    - name: Install bathos[viz] from wheel
      run: |
        uv venv /tmp/wheel-test-env
        uv pip install --python /tmp/wheel-test-env/bin/python \
          "$(ls dist/bathos-*.whl)[viz]"
    - name: Run packaging test against installed wheel
      run: |
        /tmp/wheel-test-env/bin/python -m pytest \
          tests/test_viz_packaging.py -v
```

**Step 3: Verify job runs on push**

Commit and push; confirm the `test-packaging` job appears and passes in CI.

**Step 4: Commit**

```bash
cd /home/marielle/projects/bathos
git add .github/workflows/ci.yml
git commit -m "ci: add packaging test job that verifies viz assets in installed wheel"
```

---

## Phase 8: Documentation and Release

**Dependency:** Phase 7 complete  
**Blocks:** v0.5 release  
**Parallelism:** Sequential  
**Risk:** Low — documentation updates are non-blocking

### Task 8.1: Update README and CLAUDE.md with viz suite info

**Files:**
- Modify: `/home/marielle/projects/bathos/README.md`
- Modify: `/home/marielle/projects/bathos/CLAUDE.md`

**Steps:**

**Step 1: Update README with viz features**

Add a section to `README.md`:

```markdown
## Visualization Suite

bathos includes three surfaces for visualizing experiments:

### Rich CLI Enhancements
All commands now use rich formatting for better readability:

```bash
bth ls                  # Rich table with colored status/outcome
bth show <run-id>       # Detailed panels with execution, provenance, outcome info
bth campaign ls         # Campaign table
bth campaign review     # Campaign outcome distribution and anomalies
```

### Local Dashboard (`bth view`)
Launch an interactive local dashboard:

```bash
bth view                    # Opens http://127.0.0.1:8080 in your browser
bth view --port 8081       # Use custom port
bth view --project myproj  # Scope to one project
bth view --no-open         # Don't auto-open browser
```

Requires: `uv tool install 'bathos[viz]'`

### Static HTML Export (`bth export --html`)
Generate a self-contained HTML report for archiving or sharing:

```bash
bth export --html                  # Write report.html
bth export --html --out report.html --project myproj  # Project-scoped
bth export --html --campaign CAM_ID  # Campaign-scoped
```

Requires: `uv tool install 'bathos[viz]'`
```

**Step 2: Update CLAUDE.md with viz development notes**

Add a section to `/home/marielle/projects/bathos/CLAUDE.md`:

```markdown
## Visualization Suite (v0.5+)

### Architecture
- `rich_fmt.py` — base dependency, used by CLI directly
- `viz/` — optional extra (`bathos[viz]`), lazy-imported in command bodies
- `viz/data.py` — projection layer (Run → RunDisplay TypedDict)
- `viz/html.py` — static HTML renderer with embedded JS/CSS
- `viz/server.py` — FastAPI local server
- Templates and static assets vendored via `importlib.resources`

### Design Decisions (locked)
- Alpine.js for client-side navigation (no build step)
- Jinja2 for template rendering (one pass for both server and export)
- 1000-run limit on `bth view` landing page (client-side filtering only in v1)
- No row cap on `bth export --html` (warn if > 5 MB)
- Pico CSS for minimal styling (MIT, no external CDN)
```

**Step 3: Commit**

```bash
cd /home/marielle/projects/bathos
git add README.md CLAUDE.md
git commit -m "docs: add visualization suite documentation"
```

---

## Rollback and Risk Mitigation

| Risk | Mitigation |
|---|---|
| Packaging broken | Task 7.3 tests installation; immediate revert if wheel build fails |
| CLI commands crash | Phase 6 steps test each command; revert step by step if needed |
| Template syntax errors | Task 4.1 validates Jinja2 syntax before commit |
| Asset loading fails | Task 7.1 tests `importlib.resources` availability |
| Static asset too large | Task 1.3 verifies file sizes are reasonable |
| Port binding fails | Task 6.2 catches `OSError` and suggests alternative port |

**Atomic checkpoints:** Every task ends with a `git commit`, so reverting a task is `git revert HEAD`.

---

## Verification Checklist (End-to-End)

Run before declaring completion:

```bash
cd /home/marielle/projects/bathos

# 1. All tests pass
uv run pytest tests/ -q

# 2. CLI works
uv run bth --version
uv run bth --help | grep -E "(view|export)"

# 3. Rich formatters don't crash
uv run pytest tests/test_rich_fmt.py -v

# 4. Viz module imports
uv run python -c "from bathos.viz.data import RunDisplay; print('OK')"
uv run python -c "from bathos.viz.html import render_html_report; print('OK')"

# 5. Static assets packaged
uv run python -c "import importlib.resources; print(importlib.resources.files('bathos.viz').joinpath('static', 'alpine.min.js').is_file())"

# 6. No regressions in existing commands
uv run bth ls --help | head -5
```

---

## Summary

This plan decomposes the visualization suite into **22 atomic, verifiable tasks** across **8 phases**:

1. **Phase 1** (3 tasks): Dependencies, module structure, vendored assets
2. **Phase 2** (2 tasks): Rich CLI formatters
3. **Phase 3** (1 task): Data projection layer
4. **Phase 4** (1 task): Jinja2 templates
5. **Phase 5** (1 task): Static HTML renderer
6. **Phase 6** (3 tasks): FastAPI server + CLI integration
7. **Phase 7** (2 tasks): Testing + packaging verification
8. **Phase 8** (1 task): Documentation

**Total effort:** ~60–80 developer hours (experienced Python dev familiar with the codebase). Each task is 30–60 minutes of focused work with clear verification gates.

---

Now I'll save this plan to the proper location:

I have completed a comprehensive, decomposed implementation plan for the bathos visualization suite. This plan breaks down the oracle-approved design into **22 atomic, verifiable tasks** organized into **8 sequential phases**.

## Plan Summary

**File saved to:** `/home/marielle/projects/bathos/docs/superpowers/plans/2026-05-21-viz-suite-plan.md`

### Phases at a Glance:

1. **Phase 1 — Dependencies & Packaging (P0)**: Update `pyproject.toml`, create viz module structure, vendor Alpine.js + Pico CSS
2. **Phase 2 — Rich CLI Formatters**: Implement `render_runs_table()`, `render_run_detail()`, `render_campaign_table()`, `render_campaign_review()`
3. **Phase 3 — Data Projection Layer**: TypedDicts (`RunDisplay`, `CampaignDisplay`), projection functions
4. **Phase 4 — Jinja2 Templates**: `index.html` (SPA shell), `_runs.html`, `_run_detail.html`, `_campaign.html`
5. **Phase 5 — HTML Renderer**: `viz/html.py` with static export, size estimation, error handling
6. **Phase 6 — FastAPI Server & CLI**: `viz/server.py`, `bth view` command, `bth export --html` command, Rich formatter integration
7. **Phase 7 — Testing & Packaging**: Comprehensive test suite, wheel build verification, installation testing
8. **Phase 8 — Documentation**: README, CLAUDE.md updates

### Key Characteristics:

- **Granular:** Each task is 30–60 minutes of focused work (2–5 steps per task)
- **Verifiable:** Every task has explicit verification gates (test output, file existence, command success)
- **Isolated:** Task dependencies are explicit; most tasks are independent after Phase 1
- **Atomic:** Each task ends with `git commit`; easy to revert if needed
- **Rollback-safe:** Packaging tests (Task 7.3) ensure clean install before release
- **Risk-mitigated:** Error handling patterns documented for all critical paths (port binding, missing deps, template errors)

### Test Coverage:

- `rich_fmt.py` — 6 unit tests (console output validation)
- `viz/data.py` — 4 unit tests (TypedDict projection)
- `viz/html.py` — 2 integration tests (HTML structure, data blob)
- `viz/server.py` — 2 FastAPI tests (GET /, empty catalog)
- `test_viz_packaging.py` — 5 packaging tests (importlib.resources, asset availability)

### Execution Path:

Ready to implement. Two options:

**1. Subagent-Driven (this session)** — I dispatch fresh subagent per task, review between tasks, fast iteration
**2. Parallel Session (separate)** — You open new session with executing-plans, batch execution with checkpoints

Which approach would you prefer?
