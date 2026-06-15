---
name: using-bathos
description: Experiment tracking with bathos — run tracking, sidecar pre-registration, cluster submission, catalog queries
triggers: [bathos, bth, experiment, run, sidecar, cluster, submit, slurm, catalog, campaign]
---

# using-bathos

bathos (`bth`) is a standalone experiment tracking CLI for researchers running 10+ projects across local and SLURM cluster environments. It tracks script runs, pre-registers hypotheses via sidecars, syncs results to/from clusters, and provides rich query and reporting interfaces.

## Core Concepts

**Run** — A single script execution tracked in the catalog. Fields: `id`, `project_slug`, `command`, `argv`, `git_hash`, `git_branch`, `timestamp`, `duration_s`, `exit_code`, `status`, `output_paths`, `tags`, `outcome`, `sidecar_sha256`, `campaign_id`, `slurm_job_id`, `slurm_array_task_id`, postmortem metadata.

**Sidecar** — A `.bth.toml` file alongside a script that pre-registers hypothesis, expected outcome conditions (DuckDB SQL), and result schema. Enforced by default at `bth run` time (use `--no-sidecar` to bypass, logs `BYPASSED`).

**Outcome** — Evaluated at run-end by matching result JSON against DuckDB SQL conditions in the sidecar. Values: `pass`, `marginal`, `fail`, `error`. One outcome must be marked `is_residual = true`.

**Campaign** — A named group of related runs. Accessible via `bth campaign` subcommands; queries via `campaign_id` field.

**Catalog** — Tiered Parquet + DuckDB store at `~/.bth/catalog/` (or `.bth.toml` `[project].catalog_dir`). Cool tier (per-run fragments) → compacted to warm tier (DuckDB database) → optionally archived to cold tier (partitioned Parquet).

**Sync** — Push/pull catalog between local and cluster remote via myxcel. Uses `bth sync [remote] [--pull]` or cluster submission flags `--push-first`, `--then-pull`, `--then-sync`.

## Installation

```bash
uv tool install bathos
bth --version
```

## Project Initialization

```bash
bth init --slug myproject --slurm-partition mit_normal
```

Creates `.bth.toml` and initializes catalog. Defines project slug (required for all other commands), cluster remote, and SLURM defaults.

## Run Tracking

### Basic Tracking

```bash
bth run -- uv run python scripts/experiments/train_model.py --epochs 10 --out outputs/result.json
```

Runs script, captures git state, and records run in catalog with auto-generated UUID.

### With Metadata

```bash
bth run \
  --out outputs/result.json \
  --tag experiment:baseline \
  --tag date:2026-06-01 \
  --campaign my-campaign-id \
  -- uv run python scripts/train.py
```

**Options:**
- `--out PATH` — Register output file path (can repeat)
- `--tag TAG` — Add tag (can repeat)
- `--campaign ID` — Associate with campaign
- `--agent-mode collaborative|autonomous` — Mark collaborative (human-in-loop) or autonomous runs
- `--derived-from RUN_ID` — Link lineage to parent run
- `--no-sidecar` — Bypass sidecar enforcement (logs `BYPASSED`)

Exit code is script's exit code.

## Output Path Convention

Output JSON files registered with `bth run --out` must **never** be in ephemeral directories (`/tmp`, `/var/tmp`, or `$TMPDIR`). Bathos catalogs these paths as durable references; a temp-dir path will be lost on reboot or system cleanup, making the catalog entry unreproducible.

Non-JSON files (PNG, SVG, PDF figures) are equally valid `--out` targets; bathos stores them in `output_paths` as opaque file references alongside result JSON. Repeat the flag for each path.

```bash
# ✓ Correct — persistent project-relative path
bth run --out outputs/run_abc.json -- uv run python scripts/experiments/train.py

# ✗ Wrong — /tmp is ephemeral; catalog entry becomes invalid after reboot
bth run --out /tmp/result.json -- uv run python scripts/experiments/train.py
```

Smoke-test validation runs (pre-flight checks before a real run) should be executed **directly**, not via `bth run`, so they are not tracked:

```bash
# ✓ Correct — smoke test run directly, not cataloged
uv run python scripts/experiments/train.py --smoke --out /tmp/test.json

# Then the real tracked run uses a persistent path
bth run --out outputs/run_abc.json -- uv run python scripts/experiments/train.py
```

## Sidecar Pre-Registration

Every script in `scripts/experiments/` and `scripts/benchmarks/` should have a sidecar `.bth.toml` declaring hypothesis and expected outcomes.

### Experiment Sidecar Format

```toml
[experiment]
hypothesis = "NVT with dt=0.5fs maintains ±5K temperature stability over 50ps"

[outcomes.pass]
condition = "temp_std < 5"
is_residual = false

[outcomes.marginal]
condition = "temp_std >= 5 AND temp_std < 10"
is_residual = false

[outcomes.fail]
condition = "temp_std >= 10"
is_residual = true

[result_schema]
temp_mean = "float"
temp_std = "float"
n_steps = "int"
```

**Key Rules:**
- Outcome `condition` fields are DuckDB SQL fragments evaluated on result JSON
- Exactly one outcome must have `is_residual = true`
- `result_schema` declares all columns referenced by outcome conditions
- No Python-style chained comparisons: use `AND` instead of `0.4 <= x < 0.7`
- Script outputs JSON to path registered with `bth run --out`

### Benchmark Sidecar Format

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

### Scaffold a Template

```bash
bth new-experiment --name my_experiment
```

Creates `scripts/experiments/my_experiment.py` and `scripts/experiments/my_experiment.bth.toml` skeleton.

### Validate Sidecar

```bash
bth check --path scripts/experiments/train.bth.toml
```

Checks TOML syntax, schema completeness, DuckDB condition validity, and residual outcome presence.

## Query and Inspect

### List Recent Runs

```bash
bth ls --limit 20 --since 7d
bth ls --status completed --project myproject
```

Shows table of recent runs (project, command, outcome, duration, timestamp).

### Show Run Details

```bash
bth show abc123-uuid
```

Full run metadata, git state, outcome, sidecar hash, postmortem status.

### Find Runs by Condition

```bash
bth find --filter "outcome='pass' AND project_slug='myproject'"
bth find --filter "campaign_id='my-campaign' AND slurm_job_id IS NOT NULL"
bth find --limit 100
```

DuckDB WHERE clause over catalog. Returns table of matching runs.

### Run Arbitrary SQL

```bash
bth sql "SELECT outcome, COUNT(*) FROM runs WHERE project_slug='myproject' GROUP BY outcome"
```

Query catalog directly. Useful for analytics and audits.

## Catalog Management

### Compact Cool Tier → Warm Tier

```bash
bth compact
```

Merges per-run Parquet fragments into DuckDB database. Automatic on `bth ls` if fragmentation is high.

### Archive Old Runs

```bash
bth archive --before 90d --out ~/backups/archive.tar.zst
```

Exports cold-tier Parquet (partitioned by project/year/month) and compresses. Reduces catalog size.

### Migrate Schema

```bash
bth migrate
```

Upgrades catalog schema to current version. Run after updating bathos.

### Project Subdirectories (v0.4+)

```bash
bth migrate-to-project-subdirs
```

Reorganizes flat catalog to per-project subdirectories (`runs/<slug>/run_<uuid>.parquet`). Enables per-project filtering in `bth sync`.

## Cluster Submission (Phase 2)

### Submit Job to SLURM

```bash
bth submit \
  --preset gpu-h200 \
  --array 0-19%4 \
  --then-sync \
  -- bth run uv run python scripts/train.py --epochs 100
```

Submits to SLURM via myxcel, waits for completion, syncs results back. Records `slurm_job_id` and `slurm_array_task_id` in run record.

**Options:**
- `--preset NAME` — SLURM preset (gpu, gpu-h200, cpu, quicktest, etc.)
- `--remote NAME` — Override myxcel remote (default: from `.bth.toml`)
- `--array SPEC` — SLURM array spec (e.g., `0-9%4`)
- `--dependency SPEC` — SLURM dependency (e.g., `afterok:12345`)
- `--name NAME` — Job name
- `--push-first / --no-push-first` — Push project before submit (default: push)
- `--wait / --no-wait` — Block until completion (default: no-wait)
- `--then-pull` — Pull results after completion (implies `--wait`)
- `--then-sync` — Run `bth sync` after pull (implies `--then-pull --wait`)

Exit codes: 0 = success or no-wait, 1 = job failure, 2 = timeout.

### Override Preset in Sidecar

```toml
[cluster]
preset = "gpu-h200"
remote = "engaging"
project = "myproject"
```

Sidecar `[cluster]` section overrides `.bth.toml` defaults. CLI flags override sidecar.

## Sync Catalog

### Push to Remote

```bash
bth sync engaging
```

Uses myxcel to rsync local catalog to cluster remote. Only syncs current project (v0.4+).

### Pull from Remote

```bash
bth sync engaging --pull
```

Fetches latest catalog fragments from cluster. Merges with local catalog.

### Full Workflow

```bash
bth sync engaging --pull  # Get latest cluster results
bth find --filter "slurm_job_id = '12345'"  # Query locally
```

## Campaigns

### Create Campaign

```bash
bth campaign create --name "baseline sweep" --description "Hyperparameter space exploration"
```

Returns campaign ID.

### List Campaigns

```bash
bth campaign ls
```

Shows campaign names, descriptions, associated run counts.

### Add Runs to Campaign

```bash
bth campaign add --id <campaign-id> --runs <run-id-1> <run-id-2>
```

Links runs to campaign.

### Review Campaign Results

```bash
bth campaign review <campaign-id>
```

Summary table: outcome counts, average duration, tags, sample runs.

### Conclude Campaign

```bash
bth campaign conclude <campaign-id>
```

Marks campaign closed; queries still work but status is `concluded`.

## Figure Manifest (Campaign → Maraxiom)

When a campaign concludes, bathos emits `figure_manifest.json` at
`~/.bth/catalog/sidecars/<campaign_id>/figure_manifest.json`. Maraxiom reads this
during `mrx check` freshness sweeps (F7/F8 signals) to confirm figure pins are current.

### Register figure outputs during a run

Pass figure file paths alongside the result JSON using repeated `--out` flags (any file type is valid; repeat the flag for each path):

```bash
bth run \
  --out outputs/results/my_run.json \
  --out outputs/figures/scatter.svg \
  --out outputs/figures/barplot.png \
  --campaign <campaign-id> \
  -- uv run python scripts/experiments/my_experiment.py
```

To make figure paths queryable from outcome conditions or postmortems, declare them in
`[result_schema]` and write them to the result JSON alongside scalar metrics:

```toml
[result_schema]
my_metric            = "float"
figure_path_scatter  = "str"   # e.g., "outputs/figures/scatter.svg"
figure_path_barplot  = "str"
```

### Populate the figure manifest after runs finish

`bth campaign conclude` emits an empty manifest by default. Populate it programmatically:

```python
from bathos.figure_manifest import FigureManifest, FigureEntry, InputPin

manifest = FigureManifest(
    campaign_id="<campaign-id>",
    figures=[
        FigureEntry(
            figure_id="scatter_cross_model_r",
            intent="Cross-model energy correlation — mismatch-ceiling verification",
            figure_kind="analysis_chart",   # optional
            render_state="ready",           # "ready" | "deferred"
            input_pins=[InputPin(
                run_id="<bathos-run-id>",
                output_path="outputs/results/my_run.json",
                sha256="<sha256-of-data-file>",  # hash of the DATA file, not the figure
            )],
        ),
    ],
)
manifest.write_manifest()
```

`render_state` values:
- `"ready"` — figure is rendered; maraxiom can reference its asset path
- `"deferred"` — figure intent is registered but rendering is pending (use for stubs before figures are generated)

### Key rule

Populate `figure_manifest.json` before presenting. `mrx check` reads it during freshness sweeps (F7/F8 signals) to confirm figure pins are current. `mrx context` ingests run records from the bathos catalog independently — the manifest does not gate `mrx context`.

## Lineage and Citation

### Lineage Graph

```bash
bth lineage <run-id>
bth lineage <run-id> --format prov
```

Shows parent-child run relationships. `--format prov` outputs W3C PROV-JSON.

### Citation String

```bash
bth cite <run-id>
```

BibTeX/APA-style citation for reproducibility documentation.

## Postmortems

### Scaffold Postmortem

```bash
bth postmortem scaffold <run-id>
```

Creates `<script>.bth.postmortem.toml` template for run review.

### Validate Postmortem

```bash
bth postmortem validate <path>
```

Checks TOML syntax, required fields, git drift detection, and asset integrity.

### Get Postmortem

```bash
bth postmortem get <run-id>
```

Retrieves and displays postmortem metadata.

## Visualization (v0.5+)

### Local Dashboard

```bash
bth view
```

Opens FastAPI dashboard (default: `http://localhost:8000`) with interactive run browser, campaign summaries, outcome histograms.

### Static HTML Export

```bash
bth export --html --out ~/reports/report.html
```

Generates self-contained HTML report of all runs. Warns if > 5 MB.

## Remote Profiles

### Add Remote

```bash
bth remote add engaging --host engaging.csail.mit.edu --path ~/projects/myproject
```

Registers cluster host for sync and submission.

### List Remotes

```bash
bth remote ls
```

Shows configured remotes.

### Test Connectivity

```bash
bth remote test engaging
```

Verifies SSH access to remote.

## Linting and Validation

### Lint Catalog and Scripts

```bash
bth lint
```

Tier-1 checks: missing sidecars, schema violations, outcome condition validity.
Tier-2 checks: adversarial conditions (always true/false), unbound columns, drift detection.

### Linting in Agent Mode

Scripts run with `--agent-mode` enforce stricter validation and flag adversarial conditions.

## Project Configuration (`.bth.toml`)

```toml
[project]
slug = "myproject"
root = "."
catalog_dir = "~/.bth/catalog"

[slurm]
remote = "engaging"
preset = "gpu"
project = "myproject"

[remotes.engaging]
host = "engaging"
path = "~/projects/myproject"
```

## Key Rules

- **Always use `uv run python`** in sbatch scripts, never bare `python` (cluster nodes have no global python)
- **Verify sidecar before submission** — run `bth check` to validate outcome conditions (DuckDB SQL must parse)
- **Result schema must include all outcome columns** — if `condition = "metric >= 0.9"`, declare `metric = "float"` in `[result_schema]`
- **Exactly one residual outcome** — one outcome must have `is_residual = true` for gate evaluation
- **No `--no-sidecar` in production** — bypassing logs `BYPASSED` and breaks pre-registration discipline
- **Test measurement pipeline on synthetic data** — verify metrics work before trusting research conclusions
- **Postmortem colocated with script** — `<script>.bth.postmortem.toml` alongside `<script>.py`

## Typical Workflow

```bash
# 1. Initialize project
bth init --slug myproject --slurm-partition mit_normal

# 2. Create experiment
bth new-experiment --name baseline_training
# Edit scripts/experiments/baseline_training.py and .bth.toml

# 3. Validate locally
bth check --path scripts/experiments/baseline_training.bth.toml
uv run python scripts/experiments/baseline_training.py --smoke --out /tmp/test.json  # NOT via bth run — smoke outputs are ephemeral, not tracked

# 4. Run locally or submit to cluster
bth run -- uv run python scripts/experiments/baseline_training.py --out outputs/run.json
# OR
bth submit --preset gpu --then-sync -- bth run uv run python scripts/experiments/baseline_training.py

# 5. Query results
bth find --filter "outcome='pass' AND project_slug='myproject'"
bth campaign review <campaign-id>

# 6. Review postmortem
bth postmortem scaffold <run-id>
bth postmortem validate scripts/experiments/baseline_training.bth.postmortem.toml

# 7. Export report
bth export --html --out ~/reports/latest.html

# 8. Populate figure manifest (if campaign produced figures for maraxiom)
# from bathos.figure_manifest import FigureManifest, FigureEntry, InputPin
# manifest = FigureManifest(campaign_id="...", figures=[FigureEntry(...)])
# manifest.write_manifest()
# Then mrx check reads it during freshness sweeps; mrx context ingests run records independently
```

## Related

- **CLAUDE.md**: Bathos architecture, schema versions, backlog
- **Global rules**: `~/.claude/rules/BATHOS.md` — `uv run python` discipline, sidecar validation, DuckDB conditions
- **Cluster rules**: `~/.claude/rules/CLUSTER.md` — SLURM partition limits, job submission, local validation gates
