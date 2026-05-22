# bathos

[![PyPI](https://img.shields.io/pypi/v/bathos.svg)](https://pypi.org/project/bathos/)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache 2.0](https://img.shields.io/badge/license-Apache%202.0-blue.svg)](https://github.com/mariellerossi/bathos/blob/main/LICENSE)
[![Documentation](https://img.shields.io/readthedocs/bathos.svg)](https://bathos.readthedocs.io)

> **Public alpha — experimental software.** bathos is under active development and should be considered a work in progress. APIs, catalog schema, and CLI commands may change between releases without a deprecation period. It is used in production by the author, but expect rough edges and missing features. Feedback and bug reports welcome.

Local-first, zero-server experiment tracking for researchers working across multiple projects and SLURM clusters.

Never lose track of what ran, what it produced, or whether results are still valid.

## Install

```bash
uv tool install bathos           # base install (includes Rich CLI)
uv tool install 'bathos[viz]'    # adds bth view + bth export --html
```

## Quick start

```bash
# Register this project (creates .bth.toml + scripts/ skeleton)
bth init

# Run and track an experiment
bth run scripts/experiments/benchmark_tip3p.py -- --n-steps 1000 --out outputs/run1.parquet

# Query recent runs
bth ls --since 7d
bth show <run-id>

# Check catalog runs for git-drift freshness
bth check [--status <status>] [--check-outputs]

# Escape hatch: raw DuckDB SQL against the catalog
bth sql "SELECT project_slug, count(*) FROM runs GROUP BY 1"
```

### `bth run` flags

**v0.3+ flags:**
- `--agent-mode collaborative|autonomous` — declare whether this run is agent-driven
- `--derived-from <run-id>` — record parent run for lineage tracking
- `--campaign <campaign-id>` — associate this run with a campaign
- `--no-sidecar` — bypass sidecar enforcement (use for ad-hoc runs)

## CLI Reference

**Core commands:**
- `bth init` — initialize bathos in a project
- `bth run` — execute and track an experiment
- `bth ls` — list recent runs
- `bth show <run-id>` — display run details
- `bth find` — search runs by criteria
- `bth sql` — raw DuckDB query against catalog
- `bth check` — check catalog runs for git-drift freshness
- `bth lineage <run-id>` — show ancestor chain following parent_run_id links
- `bth sprint-audit [--hours N]` — audit recent runs across all registered projects
- `bth lint [--project-root PATH]` — check scripts/ for naming conventions and missing sidecars
- `bth new-experiment <name>` — scaffold a new experiment script and sidecar
- `bth migrate` — upgrade cool-tier Parquet fragments to current schema version
- `bth migrate-to-project-subdirs [--dry-run]` — move flat catalog runs into `runs/<slug>/` subdirs (v0.4+)
- `bth export` — export the using-bathos skill and register MCP server
- `bth export --html [--out report.html] [--project slug] [--campaign id]` — self-contained HTML report (requires `bathos[viz]`)
- `bth view [--port 8080] [--project slug] [--no-open]` — local FastAPI dashboard (requires `bathos[viz]`)

**`bth sync`** — Sync cool-tier catalog to/from cluster (v0.4+: per-project filtered)
- `bth sync [<remote>]` — push only this project's runs to the remote (filtered by `project_slug`)
- `bth sync [<remote>] --pull` — pull only this project's runs from the remote
- Output: `Pushed 47 runs (filtered 275 from other projects) to 'engaging' in 1.2s`
- Config: `sync_filter = "none"` in `.bth.toml` disables filtering (pushes all projects)

**`bth remote`** — Manage sync remotes
- `bth remote add <name> <url>` — add an SSH remote for catalog sync
- `bth remote list` — list configured remotes
- `bth remote remove <name>` — remove a remote
- `bth remote test <name>` — test SSH connectivity to a remote

**`bth postmortem`** — Retrospective tracking for completed experiments (v0.4.1+)
- `bth postmortem validate <file>` — validate a `*.bth.postmortem.toml` file (refutation consistency, asset checksums, git drift)

**`bth campaign`** — Manage experiment campaigns (v0.3+)
- `bth campaign create <id> --hypothesis <text>` — create a new campaign
- `bth campaign add <run-id> --campaign <id>` — associate a run with a campaign
- `bth campaign ls` — list campaigns
- `bth campaign show <id>` — show campaign details and runs
- `bth campaign review <id>` — statistical summary and anomaly detection
- `bth campaign conclude <id> --outcome <label>` — close campaign with outcome

## Script conventions

`bth init` scaffolds the following structure in your project:

| Directory | Purpose | Naming | Tracked |
|---|---|---|---|
| `scripts/experiments/` | Typer experiment runners | `verb_noun.py` | Yes |
| `scripts/analysis/` | Post-hoc analysis and plots | `verb_noun.py` | Optional |
| `scripts/validation/` | Correctness checks | `verb_noun.py` | Optional |
| `scripts/benchmarks/` | Performance benchmarks | `verb_noun.py` | Yes |
| `scripts/data/` | Data pipeline / conversion | `verb_noun.py` | No |
| `scripts/slurm/` | SLURM job files + env helpers | `verb_noun.slurm` | Via wrapper |
| `scripts/debug/` | Debug specific issues | `YYMMDD_desc.py` | No |
| `scripts/explore/` | Open-ended investigation | `YYMMDD_desc.py` | No |
| `scripts/scratch/` | Catchall / ephemeral | `YYMMDD_desc.py` | No (gitignored) |

## Python decorator

For Typer-based scripts that want direct integration:

```python
import bth

@bth.experiment(name="benchmark_tip3p", tags=["tip3p", "nvt"])
def main(n_steps: int, out: Path):
    ...
```

## SLURM

`bth init` generates a `scripts/slurm/_bth_env.sh` helper. Source it in job scripts to get automatic provenance capture for batch runs.

## Catalog

All runs land in `~/.bth/catalog/` as DuckDB + Parquet. Query with `bth ls`, `bth find`, or raw `bth sql`.

## Documentation

Full documentation is available at [https://bathos.readthedocs.io](https://bathos.readthedocs.io).

- [Installation guide](https://bathos.readthedocs.io/en/stable/install/)
- [Design and architecture](https://bathos.readthedocs.io/en/stable/design/)
- [API reference](https://bathos.readthedocs.io/en/stable/api/)
- [SLURM integration](https://bathos.readthedocs.io/en/stable/slurm/)
