# bathos

Local-first, zero-server experiment tracking for researchers working across multiple projects and SLURM clusters.

Never lose track of what ran, what it produced, or whether results are still valid.

## Install

```bash
uv tool install bathos
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

# Check if a result is still valid (compares recorded git hash to HEAD)
bth check outputs/run1.parquet

# Escape hatch: raw DuckDB SQL against the catalog
bth sql "SELECT project_slug, count(*) FROM runs GROUP BY 1"
```

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
