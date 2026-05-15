# bathos CLAUDE.md

CLI tool: `bth` | Install: `uv tool install bathos` | Stack: Python 3.12, Typer, DuckDB, PyArrow

## What This Is

bathos is a standalone experiment tracking CLI for a single researcher across 10+ projects and a SLURM cluster. Central goal: you never lose track of what ran, what it produced, or whether results are still valid.

**Core insight:** experiments already sit in a directory taxonomy (`scripts/experiments/`, `scripts/benchmarks/`, etc.) — bathos uses that structure to enforce per-directory schemas and pre-registration discipline at the tool level.

---

## Current Status (as of 2026-05-15)

**v0.1: complete and merged to main.** 44 tests passing.

- Full design spec: `.praxia/specs/bathos-design.md`
- v0.1 implementation plan: `.praxia/specs/bathos-v01-plan.md`
- Backlog: items #124–139 in praxia DB (see below); #124–127 done

**Next sprint candidates (P2):** #128 (FastMCP), #130 (bth check) + #128 unblocked once 130 done, #138 (bth sync), #139 (bth compact / warm tier), #131 (SLURM integration), #132 (bth new-experiment).

---

## Architecture

### Tiered Storage

```
SLURM job / bth run
      │
      ▼  atomic write-then-rename
 cool/   ~/.bth/catalog/runs/run_<uuid>.parquet   ← minimal schema, SLURM-safe
      │
 bth compact  (lazy on query or explicit)
      │
      ▼
 warm/   ~/.bth/catalog/bathos.db                 ← DuckDB, full schema + metadata JSON
      │
 bth archive  (explicit)
      │
      ▼
 cold/   ~/.bth/catalog/archive/project=X/year=Y/month=M/runs.parquet
```

- **Hot:** in-memory `Run` object during execution
- **Cool:** per-run Parquet fragments, atomic write target, SLURM parallel-safe
- **Warm:** DuckDB database, primary interactive query target (`bth ls`, `bth find`)
- **Cold:** partitioned archive Parquet, historical bulk queries, sync-able to cluster remote

Cool schema is intentionally minimal (13 provenance fields). Warm adds `metadata TEXT` (JSON) and `outcome TEXT` columns.

### Pre-Registration Schemas (per script directory)

Every script in `scripts/experiments/` and `scripts/benchmarks/` must have a companion sidecar **`<script-stem>.bth.toml`** declaring its hypothesis, expected outcomes, and result schema before `bth run` will execute it.

**Sidecar format (experiments):**
```toml
[experiment]
hypothesis = "NVT with dt=0.5fs maintains ±5K temperature stability over 50ps"

[outcomes.pass]
condition = "temp_std < 5"        # DuckDB SQL fragment
decision = "proceed to NPT validation"

[outcomes.marginal]
condition = "temp_std >= 5 AND temp_std < 10"
decision = "tune Langevin gamma, re-run"

[outcomes.fail]
condition = "temp_std >= 10"
decision = "debug thermostat, open issue"

[result_schema]
temp_mean = "float"
temp_std = "float"
n_steps = "int"
dt_fs = "float"
```

**Sidecar format (benchmarks):**
```toml
[benchmark]
baseline_ref = "run_abc123"
metric = "ns_per_day"
regression_threshold = 0.05
target = "> 50 ns/day on pi_so3"

[result_schema]
ns_per_day = "float"
system = "str"
n_atoms = "int"
```

**Sidecar format (debug):**
```toml
[debug]
symptom = "NaN forces after step 847 on 1uao"
suspected_cause = "PME grid aliasing with box < 2*cutoff"
verification = "reproduce with box=4nm, compare box=6nm"

[verdict_schema]
reproduced = "bool"
root_cause = "str"
fix = "str"
```

### Key Design Decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Pre-registration mechanism | **Sidecar TOML** (`<stem>.bth.toml`) | Language-agnostic; works for shell/Rust/Python; readable by CLI without import |
| `@bth.experiment` scope | **Provenance only** | Keeps decorator and sidecar as separate concerns with no sync problem |
| Outcome condition evaluation | **DuckDB SQL fragments**, stored label at run-end | Reuses existing stack; no custom DSL; no `eval()`; label queryable as `WHERE outcome='pass'` |
| Declaration indexing | **Content-addressed** at `bth run` time | Rename-safe; catalog records never depend on file paths |
| Sidecar lookup | `<script-stem>.bth.toml` adjacent to script | Consistent, predictable; resolve real path under symlinks |
| v0.2 option | **Frontmatter** (`# ---bth---` YAML block in-file) | Cleaner long-term (one file, language-agnostic via comment); ship sidecar first |
| Catalog storage | **DuckDB + Parquet** (not pyiceberg) | pyiceberg is for distributed data lakes; DuckDB+Parquet is simpler, no overhead |
| FastMCP | Mirror CLI tool-for-tool | `cli.py` and `mcp.py` both thin layers over same core modules |

### Source Layout (planned)

```
src/bathos/
  __init__.py
  cli.py          # Typer app (thin — calls core)
  mcp.py          # FastMCP server (thin — mirrors cli.py)
  schema.py       # Run dataclass, RUN_SCHEMA PyArrow schema
  catalog.py      # DuckDB+Parquet write/read (cool tier)
  compact.py      # cool → warm compaction
  config.py       # .bth.toml + ~/.bth/config.toml parsing
  git.py          # git state capture
  init.py         # bth init logic
  runner.py       # bth run subprocess wrapper
  query.py        # list_runs, get_run, find_runs, run_sql
  checker.py      # bth check — git-drift validity (v0.2)
  sidecar.py      # .bth.toml sidecar parse, content-hash, outcome eval (v0.2)
  templates/
    _bth_env.sh   # SLURM env helper template
    experiment.py # bth new-experiment script skeleton
```

---

## Backlog (praxia DB)

| ID | Title | Priority | Depends on |
|---|---|---|---|
| 124 | Core schema + DuckDB catalog init | P1 | — |
| 125 | `bth init` — dirs, .bth.toml, catalog bootstrap, SLURM config | P1 | 124 |
| 126 | `bth run` — CLI wrapper, provenance capture | P1 | 124 |
| 127 | `bth ls` / `show` / `find` / `sql` — query interface | P1 | 126 |
| 128 | FastMCP server — mirrors CLI tool-for-tool | P1 | 126, 127, 130 |
| 129 | `@bth.experiment` — provenance decorator for Typer scripts | P2 | 124 |
| 130 | `bth check` — freshness/validity vs git HEAD | P2 | 126 |
| 131 | SLURM `_bth_env.sh` integration | P2 | 125, 126 |
| 132 | `bth new-experiment` — Typer + sidecar scaffold | P2 | 125 |
| 133 | `uv tool` packaging + release | P3 | 126–130 |
| 134 | Script convention linter | P3 | — |
| 135 | `bth migrate` — Phase 1 mechanical (existing projects) | P2 | 125 |
| 136 | `bth-migrate` praxia workflow — agentic classification + git mv plan | P2 | 135 |
| 137 | Global instruction portability (separate design session needed) | P2 | — |
| 138 | `bth sync` — rsync cool-tier catalog to/from cluster remote | P2 | 125, v0.1 done |
| 139 | `bth compact` + warm-tier DuckDB (`bth sql` catalog queries) | P2 | v0.1 done |
| 140 | Schema versioning + extended provenance (hostname, slurm_job_id, metadata JSON, migrations) | P2 | 139 |
| 141 | `bth archive` — warm→cold partitioned Parquet export | P3 | 139, 140 |

**Not yet backlogged:**
- Sidecar pre-registration enforcement in `bth run` (add as P2, depends on 126)
- Outcome evaluation + `runs.outcome` column in warm DuckDB (add as P2, depends on 139)

---

## Script Directory Convention

| Directory | Schema enforced | Naming | Tracked |
|---|---|---|---|
| `scripts/experiments/` | hypothesis + outcomes + result_schema | `verb_noun.py` | Yes |
| `scripts/benchmarks/` | baseline_ref + metric + regression_threshold | `verb_noun.py` | Yes |
| `scripts/validation/` | property + reference + tolerance | `verb_noun.py` | Optional |
| `scripts/analysis/` | none | `verb_noun.py` | Optional |
| `scripts/data/` | none | `verb_noun.py` | No |
| `scripts/slurm/` | none | `verb_noun.slurm` | Via wrapper |
| `scripts/debug/` | symptom + suspected_cause + verification | `YYMMDD_desc.py` | No |
| `scripts/explore/` | none | `YYMMDD_desc.py` | No |
| `scripts/scratch/` | none | `YYMMDD_desc.py` | No (gitignored) |

---

## Implementation Notes

- Use `uv run pytest` for all test runs
- `BTH_CATALOG_DIR` env var overrides default `~/.bth/catalog/` (used in tests)
- `BTH_PROJECT_SLUG` env var overrides `.bth.toml` lookup (used in SLURM jobs)
- Outcome conditions are DuckDB SQL; validate they parse at `bth run` start before job runs
- Content-hash sidecars at run time: store parsed declaration in run record or `declarations` table keyed by SHA256
- `bth run` on a script in `scripts/experiments/` without a sidecar: warn but don't block in v0.1; enforce in v0.2
