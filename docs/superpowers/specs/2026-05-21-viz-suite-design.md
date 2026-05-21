# bathos Visualization Suite — Design Spec

**Date:** 2026-05-21
**Status:** Approved
**Stack:** All-Python (Rich + Jinja2 + Alpine.js + FastAPI)

---

## Overview

Add a visualization suite to bathos covering three surfaces:

1. **Rich CLI** — enhanced terminal output for existing commands (`bth ls`, `bth show`, `bth find`, `bth campaign ls`, `bth campaign review`)
2. **`bth view`** — local FastAPI server that opens a browser tab with a live read-only dashboard
3. **`bth export --html`** — generates a single self-contained HTML file for archiving or sharing

All three surfaces share one query layer and one set of Jinja2 templates. No bun, no Node, no build step. Stays entirely within the Python/uv toolchain.

---

## Architecture

### Module layout

```
src/bathos/
  rich_fmt.py           # NEW — top-level, all Rich CLI formatters
  viz/
    __init__.py
    data.py             # query.py → RunDisplay/CampaignDisplay TypedDicts
    server.py           # FastAPI local server for `bth view`
    html.py             # static HTML renderer for `bth export --html`
    templates/
      base.html         # shared layout; embeds __BATHOS_DATA__ JSON blob
      runs.html         # run list view
      run_detail.html   # single-run drill-down
      campaign.html     # campaign detail + run list
    static/
      alpine.min.js     # vendored Alpine.js (offline-safe)
      pico.min.css      # vendored Pico CSS
      VERSIONS.md       # pinned versions + SHA256s for auditability
```

### Key boundaries

- `rich_fmt.py` is a **base dependency** — used by `cli.py` directly; does not import from `viz/`
- `viz/` is an **optional extra** (`bathos[viz]`) — lazy-imported inside command bodies; never imported at module top-level in `cli.py`
- `viz/data.py` consumes **only `query.py`** — never touches `catalog.py` or `compact.py` directly
- Existing `src/bathos/export.py` (MCP/skill registration) is **unchanged** — new HTML export lives in `viz/html.py` to avoid naming collision

### New CLI commands

```
bth view [--port 8080] [--host 127.0.0.1] [--no-open]
bth export --html [--out report.html] [--project <slug>] [--campaign <id>]
```

### Dependencies

```toml
# base (add rich here)
dependencies = [
  ...,
  "rich>=13",
]

# new optional extra
[project.optional-dependencies]
viz = [
  "fastapi>=0.110",
  "uvicorn[standard]>=0.27",
  "jinja2>=3.1",
]
```

Install: `uv tool install 'bathos[viz]'`

---

## Data Flow

```
DuckDB (warm tier)
  ↓ query.py  (list_runs, get_run, find_runs)
  → Run dataclass objects
  ↓ viz/data.py  (projection layer)
  → RunDisplay / CampaignDisplay TypedDicts
  → JSON-serialized → window.__BATHOS_DATA__
  ↓ Jinja2 template (base.html)
  → Alpine.js reads blob, drives filtering / sorting / display

bth view:          FastAPI serves template + /static/*
bth export --html: html.py renders template with embed_mode=True
                   → alpine.min.js + pico.min.css inlined via importlib.resources
                   → single .html file written to disk
```

`viz/data.py` does two things only: call `query.py` functions, then project `Run` objects to display-ready TypedDicts (short ID, formatted duration, serializable asset links, human-readable timestamps). No business logic.

Static assets are accessed at runtime via `importlib.resources.files("bathos.viz")`. Jinja2 uses `PackageLoader("bathos.viz", "templates")`.

---

## Views

### Run list (landing / primary)

A sortable, filterable table. Columns: short ID (8 chars), project, status (colored badge), outcome (colored badge), duration, campaign name, git branch, timestamp.

Alpine.js handles all interactivity client-side (no server round-trips):
- Filter inputs: project dropdown, status/outcome checkboxes, free-text search over command + tags
- Column sort (click header)
- Row click → inline expansion or navigation to run detail

### Run detail

Four grouped panels:

| Panel | Fields |
|---|---|
| Execution | command, argv, hostname, SLURM job ID, duration, exit code |
| Provenance | git hash, branch, dirty flag, script SHA256, sidecar path/mode |
| Outcome | status, outcome label, residual flag, agent mode |
| Postmortem | hypothesis status, verdict override, summary, root cause, next steps, asset links, author, git hash at postmortem time |

If `postmortem_status == "unassigned"`, the Postmortem panel is omitted. If the run belongs to a campaign, a badge links to the campaign view.

### Campaign view

Accessible via campaign badge on run detail or a top-level "Campaigns" tab. Shows:
- Campaign metadata: mode, question, hypothesis, status, conclusion, outcome label
- Outcome distribution: count table (pass / fail / marginal / unknown)
- Anomaly warnings (from `campaign_review()`)
- Filtered run list for that campaign (same component as the main run list)

### Rich CLI enhancements

| Command | Enhancement |
|---|---|
| `bth ls` | Rich Table with colored Status and Outcome columns |
| `bth show <id>` | Rich panels grouped as run detail above |
| `bth find` | Rich Table (same as `bth ls`) |
| `bth campaign ls` | Rich Table |
| `bth campaign review` | Rich panel with outcome count table; anomaly warnings in yellow |

All Rich formatting lives in `bathos/rich_fmt.py`. `cli.py` imports from it directly.

---

## Static Export

`bth export --html` always produces a **single file**. To manage payload size, apply filters at export time:

```
bth export --html --project prolix --out prolix-report.html
bth export --html --campaign <id> --out campaign-42.html
bth export --html  # all runs — warn if >1000 runs, suggest adding --project or --campaign
```

This avoids the complexity of a directory-format export while keeping file sizes practical for a single researcher's dataset.

---

## Error Handling

| Scenario | Behavior |
|---|---|
| `bth view` without `bathos[viz]` installed | `typer.Exit` with message: `Install with: uv tool install 'bathos[viz]'` — no traceback |
| `bth view` when catalog doesn't exist | Empty-state page with setup instructions; no crash |
| `bth export --html` with no matching runs | Valid HTML with empty-state message; exit 0 |
| Template rendering error | CLI error with Jinja2 file/line context |

---

## Testing

| Surface | Approach |
|---|---|
| `rich_fmt.py` | Unit tests: `Console(file=io.StringIO())`, assert table column presence and row counts against fixture `Run` lists. No snapshot tests. |
| `viz/data.py` | Pure unit tests: fixture `Run` objects in, assert `RunDisplay` field values out. No DuckDB. |
| `viz/html.py` | Integration test: render template with 3 fixture runs, assert JSON blob present and Alpine.js inlined in output. |
| `viz/server.py` | FastAPI `TestClient`: assert `/` returns 200 and contains run IDs from fixture data. |

No browser automation tests at v1 (read-only, Alpine.js logic verifiable via data blob structure).

---

## Packaging

Add to `pyproject.toml`:

```toml
[tool.hatch.build.targets.wheel]
include = [
  "src/bathos/**/*.html",
  "src/bathos/**/*.js",
  "src/bathos/**/*.css",
  "src/bathos/**/*.md",
]
```

Alpine.js and Pico CSS are vendored into `src/bathos/viz/static/`. Pinned versions and SHA256s documented in `viz/static/VERSIONS.md`.

---

## Out of Scope (v1)

- Writes / editing from the browser (postmortem creation, tag edits)
- Multi-file / directory HTML export
- Browser automation tests
- Chart components (sparklines, time-series plots) — Alpine.js + count tables are sufficient for v1
- Authentication on `bth view` (local-only, single researcher)
