# bathos Visualization Suite — Design Spec

**Date:** 2026-05-21
**Status:** Under Review
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
      index.html        # SPA shell — Alpine.js manages navigation between views
      _runs.html        # Jinja2 include: run list section
      _run_detail.html  # Jinja2 include: run detail panel
      _campaign.html    # Jinja2 include: campaign section
    static/
      alpine.min.js     # vendored Alpine.js (offline-safe)
      pico.min.css      # vendored Pico CSS
      VERSIONS.md       # pinned versions, SHA256s, MIT license notices
```

**SPA structure:** `bth export --html` and `bth view` both render a single `index.html` via Jinja2. Alpine.js manages navigation state (which view is visible) entirely client-side. The four template files map to Jinja2 `{% include %}` blocks inside `index.html`, not separately-rendered pages. This makes the single-file export natural — there is exactly one template render pass.

### Key boundaries

- `rich_fmt.py` is a **base dependency** — used by `cli.py` directly; does not import from `viz/`
- `viz/` is an **optional extra** (`bathos[viz]`) — lazy-imported inside command bodies; never imported at module top-level in `cli.py`
- `viz/data.py` consumes **only `query.py`** — never touches `catalog.py` or `compact.py` directly
- Existing `src/bathos/export.py` (MCP/skill registration) is **unchanged** — new HTML export lives in `viz/html.py` to avoid naming collision
- `bth view` never triggers compaction — read-only, always

### New CLI commands

```
bth view [--port 8080] [--host 127.0.0.1] [--no-open]
bth export --html [--out report.html] [--project <slug>] [--campaign <id>]
```

`--project` and `--campaign` in `bth export --html` are AND-combined if both are provided.

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
DuckDB (warm tier) or cool-tier Parquet
  ↓ query.py  (list_runs, get_run, find_runs, run_sql)
  → Run dataclass objects
  ↓ viz/data.py  (projection layer)
  → RunDisplay / CampaignDisplay TypedDicts
  → JSON-serialized → window.__BATHOS_DATA__ = { runs: [...], campaigns: [...] }
  ↓ Jinja2 renders index.html (single pass)
  → Alpine.js reads blob, drives navigation / filtering / sorting / display

bth view:          FastAPI serves rendered index.html + /static/* assets
bth export --html: html.py renders with embed_mode=True
                   → alpine.min.js + pico.min.css read via importlib.resources, inlined
                   → single .html file written to disk
```

Static assets accessed via `importlib.resources.files("bathos.viz")`. Jinja2 uses `PackageLoader("bathos.viz", "templates")`.

---

## Data Schemas

### `RunDisplay` TypedDict

`viz/data.py` projects `Run` objects to this shape. `metadata` and `output_metadata` are **excluded** (unbounded TEXT fields not suitable for client-side embedding). `sidecar_sha256` and `schema_version` are **excluded** (internal provenance, not display-relevant).

```python
class RunDisplay(TypedDict):
    # Identity
    id: str                         # full UUID
    id_short: str                   # first 8 chars
    project_slug: str

    # Execution
    status: str
    exit_code: int
    duration_s: float
    duration_display: str           # formatted: "7.3s" or "2m 14s"
    timestamp: str                  # ISO8601 UTC formatted as "2026-05-21 13:47:23 UTC"
    command: str
    argv: list[str]
    hostname: str
    slurm_job_id: str

    # Provenance
    git_hash: str
    git_hash_short: str             # first 8 chars
    git_branch: str
    git_dirty: bool
    script_sha256: str
    sidecar_path: str
    sidecar_mode: str
    agent_mode: str
    parent_run_id: str

    # Outputs
    tags: list[str]
    output_paths: list[str]

    # Outcome
    outcome: str
    outcome_is_residual: bool

    # Campaign
    campaign_id: str                # empty string if none
    campaign_name: str              # resolved by viz/data.py; empty string if none

    # Postmortem (all empty string / False if postmortem_status == "unassigned")
    postmortem_status: str
    postmortem_hypothesis_status: str
    postmortem_verdict_override: str
    postmortem_author: str
    postmortem_summary: str
    postmortem_path: str
    postmortem_has_anomalies: bool
    postmortem_asset_links: dict[str, Any]  # parsed from JSON TEXT; empty dict if absent
```

**Serialization rules:**
- `timestamp` → `run.timestamp.strftime("%Y-%m-%d %H:%M:%S UTC")` (always UTC, already tz-aware in `Run`)
- `postmortem_asset_links` → `json.loads(run.postmortem_asset_links)` if non-empty string, else `{}`
- `argv`, `tags`, `output_paths` → already `list[str]` on `Run`; pass through directly
- All other string fields → pass through; `None` → `""`
- `duration_display`: `f"{v:.1f}s"` if `< 60`, else `f"{int(v//60)}m {int(v%60)}s"`

### `CampaignDisplay` TypedDict

```python
class CampaignDisplay(TypedDict):
    id: str
    id_short: str                   # first 8 chars
    name: str
    mode: str                       # "exploration" | "confirmation"
    question: str
    hypothesis: str
    status: str                     # "open" | "concluded"
    started_at: str                 # ISO8601 string, pass through from Campaign
    concluded_at: str               # empty string if None
    conclusion: str
    outcome_label: str
    parent_campaign_id: str         # empty string if None

    # Aggregated from runs (computed in viz/data.py via run_sql)
    run_count: int
    outcome_distribution: dict[str, int]   # {"pass": 3, "fail": 1, "unknown": 2, ...}
    residual_rate: float
    bypass_rate: float
    unknown_rate: float
    anomalies: list[str]            # warning strings from campaign_review()
```

**Campaign aggregation:** `viz/data.py` computes `outcome_distribution`, `run_count`, `residual_rate`, `bypass_rate`, and `unknown_rate` by calling `query.run_sql()` with a parameterized aggregation:

```sql
SELECT
    outcome,
    COUNT(*) AS n,
    COUNT(*) FILTER (WHERE outcome_is_residual) AS n_residual,
    COUNT(*) FILTER (WHERE sidecar_mode = 'bypassed') AS n_bypassed,
    COUNT(*) FILTER (WHERE outcome IN ('unknown', '')) AS n_unknown
FROM runs
WHERE campaign_id = ?
GROUP BY outcome
```

`anomalies` are computed by `campaigns.review_campaign()` (Python, already exists) — `viz/data.py` calls it after fetching the run list for the campaign. This is the one exception to the "only `query.py`" rule: `campaigns.review_campaign()` is a pure function that takes a list of runs and returns a dict; it has no I/O and is appropriate to call from the data layer.

---

## Dataset Size Strategy

### `bth view` (live server)

Landing page loads at most **1000 most-recent runs** (ORDER BY timestamp DESC LIMIT 1000). If the catalog has more runs, a sticky banner appears:

> "Showing 1000 of {total} runs. Use filters to narrow the view, or run `bth view --project <slug>` to scope to one project."

Filters in the UI (project, status, outcome, free-text) operate on this 1000-run window client-side. This is a deliberate tradeoff: v1 is client-side-only, no AJAX re-queries.

### `bth export --html` (static file)

Loads all matching runs (no cap). Before writing the file, estimates payload size from the JSON blob:

- If rendered HTML > **5 MB**: print a warning to stderr — "Report is {N} MB. Consider adding `--project` or `--campaign` to reduce size." — then write anyway.
- If no runs match filters: write valid HTML with empty-state message; exit 0.

---

## Cool-Tier Fallback

If the warm catalog (`bathos.db`) does not exist (no `bth compact` run yet), `query.py` falls back to the cool-tier Parquet files. `viz/data.py` uses only the public `query.py` API, so this is transparent. However:

- Cool-tier Run objects are missing `metadata` and `output_metadata` (already excluded from `RunDisplay`) — no impact.
- Both `bth view` and `bth export --html` show an informational banner: "Viewing cool-tier data — some fields may be absent. Run `bth compact` for the full catalog."
- `bth view` detects this by catching the specific error from `query.py` and setting a `catalog_state: "cool"` flag in `__BATHOS_DATA__`.

---

## Views

### Run list (landing / primary)

Sortable, filterable table. Columns: short ID (8 chars), project, status (colored badge), outcome (colored badge), duration, campaign name, git branch, timestamp.

Alpine.js handles all interactivity:
- Filter inputs: project dropdown, status/outcome checkboxes, free-text search over `command` + `tags`
- Column sort (click header, toggle asc/desc)
- Row click → inline expansion to run detail panel (same page, no navigation)

**Known gap (accepted for v1):** Alpine.js filtering and sorting logic is not directly unit-tested. It is verified indirectly by asserting the correct data shape in `__BATHOS_DATA__`. Browser automation tests are out of scope.

### Run detail

Inline expansion from run list row. Four grouped panels:

| Panel | Fields |
|---|---|
| Execution | command, argv, hostname, slurm_job_id, duration_display, exit_code |
| Provenance | git_hash_short, git_branch, git_dirty, script_sha256, sidecar_path, sidecar_mode, agent_mode, parent_run_id |
| Outcome | status, outcome, outcome_is_residual, output_paths, tags |
| Postmortem | postmortem_status, postmortem_hypothesis_status, postmortem_verdict_override, postmortem_summary, postmortem_author, postmortem_path, postmortem_has_anomalies, postmortem_asset_links (rendered as a file-path list) |

If `postmortem_status == "unassigned"`, the Postmortem panel is omitted. Campaign badge links to the campaign section.

### Campaign view

Top-level "Campaigns" tab plus accessible via campaign badge on run detail. Shows:
- Campaign metadata: mode, question, hypothesis, status, started_at, concluded_at, conclusion, outcome_label
- Outcome distribution count table (pass / fail / marginal / unknown + any other labels)
- Anomaly warnings (in yellow/warning color)
- Filtered run list for that campaign (same run list component, pre-filtered by campaign_id)

### Rich CLI enhancements

All formatting lives in `bathos/rich_fmt.py`. `cli.py` imports directly.

| Command | Enhancement |
|---|---|
| `bth ls` / `bth find` | Rich Table with colored Status and Outcome columns |
| `bth show <id>` | Rich panels grouped as run detail above |
| `bth campaign ls` | Rich Table |
| `bth campaign review` | Rich panel with outcome count table; anomaly warnings in yellow |

---

## `rich_fmt.py` Public API

Functions take an optional `console: Console | None = None` (defaults to `Console()` if None) for testability. They print to the console directly — callers do not need to handle the return value.

```python
def render_runs_table(runs: list[Run], console: Console | None = None) -> None
    # Rich Table: id_short, project_slug, status (colored), outcome (colored),
    # duration_display, campaign_id (short), git_branch, timestamp

def render_run_detail(run: Run, console: Console | None = None) -> None
    # Four Rich Panels: Execution, Provenance, Outcome, Postmortem
    # Postmortem panel omitted if postmortem_status == "unassigned"

def render_campaign_table(campaigns: list[Campaign], console: Console | None = None) -> None
    # Rich Table: id_short, name, mode, status, started_at, run_count

def render_campaign_review(
    campaign: Campaign,
    review: dict,         # return value of campaigns.review_campaign()
    console: Console | None = None,
) -> None
    # Rich Panel: outcome distribution count table + anomaly warnings in yellow
```

---

## Error Handling

| Scenario | Behavior |
|---|---|
| `bth view` without `bathos[viz]` installed | `typer.Exit` with message: `Install with: uv tool install 'bathos[viz]'` — no traceback |
| `bth view` — port already in use | Catch `OSError` from uvicorn startup; print: `Port {port} is already in use. Try: bth view --port 8081` — no traceback |
| `bth view` — warm catalog absent (cool-only) | Serve page with informational banner; `catalog_state: "cool"` in data blob |
| `bth view` — catalog entirely absent | Empty-state page with setup instructions (`bth init`, `bth run`) |
| `bth export --html` — no matching runs | Valid HTML with empty-state message; exit 0 |
| `bth export --html` — rendered HTML > 5 MB | Warning to stderr with size + filter suggestion; write file anyway; exit 0 |
| Template rendering error | CLI error with Jinja2 file/line context |

### DuckDB connection lifecycle (`bth view`)

Open a new **read-only** DuckDB connection per request. No connection pooling — single-user local server, request concurrency is negligible. Read-only mode (`read_only=True`) prevents any accidental mutation from the view layer.

---

## Testing

| Surface | Approach |
|---|---|
| `rich_fmt.py` | Unit tests: `Console(file=io.StringIO(), force_terminal=True)`, assert table column headers and row counts against fixture `Run` lists |
| `viz/data.py` | Pure unit tests: fixture `Run` and `Campaign` objects in, assert `RunDisplay` / `CampaignDisplay` field values and types out. No DuckDB. |
| `viz/html.py` | Integration test: render with 3 fixture `RunDisplay` dicts, assert `__BATHOS_DATA__` JSON blob present, Alpine.js inlined, and Pico CSS inlined |
| `viz/server.py` | FastAPI `TestClient`: assert GET `/` returns 200 and response body contains run IDs from fixture data |
| **Packaging** | `tests/test_viz_packaging.py`: assert `importlib.resources.files("bathos.viz") / "static" / "alpine.min.js"` is a file, same for `pico.min.css` and `templates/index.html`. This test must pass against the installed package, not just the source tree — run in CI after `uv tool install` step. |

**Known gap (accepted):** Alpine.js client-side sorting/filtering is not directly tested. The data blob structure is verified; browser behavior is not.

---

## Packaging

```toml
[tool.hatch.build.targets.wheel]
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

Explicit file list (not a glob) to prevent accidental inclusion of unrelated `.html`/`.md` files.

`VERSIONS.md` documents pinned versions, SHA256 checksums, and MIT license attribution for Alpine.js and Pico CSS (both MIT, attribution required).

---

## Out of Scope (v1)

- Writes / editing from the browser (postmortem creation, tag edits)
- Multi-file / directory HTML export
- Browser automation tests
- Chart components (sparklines, time-series plots) — count tables are sufficient for v1
- Authentication on `bth view` (local-only, 127.0.0.1 default, single researcher)
- Server-side pagination or AJAX re-query in `bth view`
- Auto-compaction triggered by `bth view`
