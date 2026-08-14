---
name: using-bathos
description: Experiment tracking with bathos — run tracking, sidecar pre-registration, controls discipline, catalog queries
triggers: [bathos, bth, experiment, run, sidecar, catalog, init, lint, controls, stage_name]
---

# using-bathos

bathos (`bth`) is a standalone experiment tracking CLI for researchers running 10+ projects across local and SLURM cluster environments. It tracks script runs, pre-registers hypotheses via sidecars, syncs results to/from clusters, and provides rich query and reporting interfaces.

This skill covers the daily-driver workflow: installation, run tracking, sidecar pre-registration, controls discipline, and catalog queries. For cluster submission and sync, campaigns and claim-tier rigor, literature-parity validation, or MCP tool integration, see the sibling skills listed under **Related** below.

## Core Concepts

**Run** — A single script execution tracked in the catalog. Fields: `id`, `project_slug`, `command`, `argv`, `git_hash`, `git_branch`, `timestamp`, `duration_s`, `exit_code`, `status`, `output_paths`, `tags`, `outcome`, `sidecar_sha256`, `campaign_id`, `slurm_job_id`, `slurm_array_task_id`, postmortem metadata.

**Sidecar** — A `.bth.toml` file alongside a script that pre-registers hypothesis, expected outcome conditions (DuckDB SQL), and result schema. Enforced by default at `bth run` time (use `--no-sidecar` to bypass, logs `BYPASSED`).

**Outcome** — Evaluated at run-end by matching result JSON against DuckDB SQL conditions in the sidecar. Values: `pass`, `marginal`, `fail`, `error`. One outcome must be marked `is_residual = true`.

**Campaign** — A named group of related runs. Accessible via `bth campaign` subcommands; queries via `campaign_id` field. See **bathos-campaigns** for campaign, claim-tier, postmortem, and lineage workflows.

**Catalog** — Tiered Parquet + DuckDB store at `~/.bth/catalog/` (or `.bth.toml` `[project].catalog_dir`). Cool tier (per-run fragments) → compacted to warm tier (DuckDB database) → optionally archived to cold tier (partitioned Parquet).

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

**How outcome evaluation actually finds your result JSON** — `bth run` reads it from, in order:
1. `$BTH_RESULTS_PATH` — an env var `bth run` sets for the subprocess. **This is the
   reliable mechanism; write your result dict here.**
2. `<script_stem>.bth-results.json` adjacent to the script (fallback).
3. A single registered `--out` JSON path (fallback) — only used when *exactly one*
   `--out` path ends in `.json`; with zero or multiple candidates this is skipped
   rather than guessing, and outcome stays `unknown`.

Writing only to `--out` and never to `$BTH_RESULTS_PATH` is a common mistake that
silently leaves `outcome='unknown'` for every run, even when the run completes
successfully and the sidecar conditions would otherwise evaluate to `pass`. Prefer
writing to both:

```python
import json
import os

result = {"temp_mean": 300.5, "temp_std": 2.3, "n_steps": 1000}

with open(args.out, "w") as f:
    json.dump(result, f)

results_path = os.environ.get("BTH_RESULTS_PATH")
if results_path:
    with open(results_path, "w") as f:
        json.dump(result, f)
```

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

## Controls Discipline (v0.11.0)

### Stage Classification

Every experiment sidecar can declare a `stage_name` field (default: `"exploration"`) to classify the maturity and intent of the run. Canonical values are advisory — non-canonical values are logged as a warning and coerced to `"exploration"` at parse time.

**Canonical stages:**
- `exploration` — hypothesis generation, parameter sensitivity, proof-of-concept (default)
- `calibration` — tuning hyperparameters before validation; outcome refinement
- `validation` — testing hypothesis with controlled parameters; reproducibility required
- `ablation` — isolating contributions of components
- `production` — final tested run ready for publication

```toml
[experiment]
hypothesis = "..."
stage_name = "validation"  # optional, defaults to "exploration"
```

Non-canonical values (e.g., `"pilot"`, `"final"`) trigger a warning and are coerced to `"exploration"`:
```
WARNING: Invalid stage_name 'pilot' in scripts/experiments/train.bth.toml; must be one of {'exploration', 'calibration', 'validation', 'ablation', 'production'}. Coercing to 'exploration'.
```

### Novel Flag

Mark a run as novel (a new claim, not reproduction of prior work) with the `novel` flag:

```toml
[experiment]
hypothesis = "..."
novel = true
```

Setting `novel = true` satisfies Tier-1 lint requirements for validation/production experiments (see Lint Checks below).

### Reproduction Block

Declare reproduction metadata (optional `[reproduction]` block) to link your experiment to prior work or control runs:

```toml
[reproduction]
reproduces_paper = "doi:10.1234/example"    # DOI or citation string (or "")
reproduces_run = "run_abc123"               # Bathos run UUID (or "")
tolerance_pct = 5.0                         # Allowed deviation in outcome metrics (optional)
requires_pass_stem = "baseline_train"       # Script stem that must pass first (optional)
```

**Fields:**
- `reproduces_paper` — DOI or full citation if this experiment reproduces published work. Leave empty if not reproducing a paper.
- `reproduces_run` — Bathos run UUID if this experiment reproduces a prior bathos run. Leave empty if not reproducing a bathos run.
- `tolerance_pct` — Allowed percentage deviation in outcome metrics when comparing to the reproduced reference. Optional; omit if not doing quantitative reproduction.
- `requires_pass_stem` — Script stem (e.g., `"baseline_train"`) that must have at least one passing run before this script can be submitted. Enforced at `bth submit` time. Optional; omit if no prerequisite.

### Controls Block

Declare which outcome labels count as "control arm success" and "control arm failure" (optional `[controls]` block):

```toml
[controls]
positive_outcome = ["pass"]
negative_outcome = ["fail", "marginal"]
```

**Fields:**
- `positive_outcome` — List of outcome labels that indicate the control arm succeeded (e.g., `["pass"]`).
- `negative_outcome` — List of outcome labels that indicate the control arm failed (e.g., `["fail", "marginal"]`).

The control block is declarative; it feeds Sprint Audit Signal 9 (see below).

### bth submit Gate

Two separate gate mechanisms protect experimental discipline (see **bathos-cluster** for `bth submit` itself):

**1. Run-time gate** (`gate_check()` in `bth run`) — happens automatically at run start, validates sidecar presence, hash, and first-of-kind properties. Does NOT touch `stage_name`, `novel`, or `[reproduction]` fields.

**2. Submit-time gate** (at `bth submit`) — keyed ONLY on `[reproduction].requires_pass_stem`. Operates in two modes:

**Hard gate for validation/production:**
- If `requires_pass_stem` is set AND `stage_name` is in `("validation", "production")`, **exit with error** if no passing run of that script stem exists.
- Error code: `REPRODUCTION_PREREQUISITE_UNMET`

```bash
$ bth submit --preset gpu -- bth run uv run python scripts/validate.py
REPRODUCTION_PREREQUISITE_UNMET: no passing run of 'baseline_train' found
# Exit 1 — submission blocked
```

**Advisory warning for exploration/calibration:**
- If `requires_pass_stem` is set AND `stage_name` is in `("exploration", "calibration")`, **warn but continue**.

```bash
$ bth submit --preset gpu -- bth run uv run python scripts/calibrate.py
WARNING: no passing run of 'baseline_train' found (advisory for calibration stage)
# Exit 0 — submission proceeds
```

**Silent skip:**
- If `[reproduction]` block is absent or `requires_pass_stem` is empty, gate is skipped silently.

### Lint Checks

#### Tier-1 (Error)

**`check_novel_or_reproduces_declared`** — Enforces reproducibility documentation for validation/production runs.

Triggered by: `bth lint` (Tier-1 block)

**Rule:** Any experiment with `stage_name` in `{"validation", "production"}` must have either:
- `[reproduction]` block with non-empty `reproduces_paper` or `reproduces_run`, OR
- `novel = true`

**Example violations:**
```toml
[experiment]
hypothesis = "Testing X"
stage_name = "validation"
# ✗ FAIL: No [reproduction] block, no novel=true
```

**Fixes:**
```toml
# Option 1: Declare reproduction
[experiment]
hypothesis = "Testing X"
stage_name = "validation"

[reproduction]
reproduces_paper = "doi:10.1234/example"

# Option 2: Declare as novel
[experiment]
hypothesis = "Testing X"
stage_name = "validation"
novel = true
```

#### Tier-2 (Warning)

**`check_bypass_trend`** — Flags increasing use of `--no-sidecar` (tracked as `sidecar_mode='bypassed'`).

Triggered by: `bth lint` (Tier-2 advisory)

Warns if latest week's bypass rate is higher than prior week's.

**`check_canonical_stage_names`** — Flags non-canonical `stage_name` values already in the warm catalog.

Triggered by: `bth lint` (Tier-2 advisory)

Example:
```
WARNING: Non-canonical stage_name 'pilot' in warm catalog (1 run). Consider renaming to one of: exploration, calibration, validation, ablation, production
```

### Sprint Audit Signals

Run sprint audit to check project health across controls:

```bash
bth sprint-audit
```

#### Signal 9: control_arm_rate

**Definition:** Fraction of all runs in the project with outcome labels matching the pattern `ctrl_%` (e.g., `ctrl_pass`, `ctrl_fail`).

**Status values:**
- **OK** if `control_arm_rate > 0.0` (at least some control runs exist)
- **WARNING** if `control_arm_rate == 0.0` AND validation/production runs exist (no control runs despite having main runs)
- **INFO** if no runs exist or catalog unavailable

**Usage:** Not based on `[controls]` sidecar block presence; it scans actual outcome values across the catalog.

```bash
$ bth sprint-audit
Signal 9 (control_arm_rate): control_arm_rate=12.5% (5/40 runs with ctrl_* outcome)
Status: OK
```

#### Signal 10: submit_bypass_rate

**Definition:** Fraction of validation/production cluster runs (those with `slurm_job_id` populated) that **lack** a matching submit-provenance record.

Provenance stored at: `~/.bth/catalog/submits/<project_slug>/**/*.parquet`

**Status values:**
- **OK** if `submit_bypass_rate <= 5%` (0.05)
- **WARNING** if `submit_bypass_rate > 5%` (more than 1 in 20 V/P jobs lack provenance)
- **INFO** if no validation/production cluster runs exist

**Usage:** Detects when jobs are submitted outside `bth submit` workflow (e.g., raw `sbatch`), bypassing provenance tracking.

```bash
$ bth sprint-audit
Signal 10 (submit_bypass_rate): submit_bypass_rate=3.0% (1/34 validation/production cluster runs without provenance)
Status: OK
```

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
- **Postmortem colocated with script** — `<script>.bth.postmortem.toml` alongside `<script>.py` (see **bathos-campaigns**)

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

# 4. Run locally or submit to cluster (see bathos-cluster for bth submit)
bth run -- uv run python scripts/experiments/baseline_training.py --out outputs/run.json

# 5. Query results
bth find --filter "outcome='pass' AND project_slug='myproject'"

# 6. Export report
bth export --html --out ~/reports/latest.html
```

## Related

- **bathos-cluster** — SLURM submission (`bth submit`), catalog sync (`bth sync`), remote profiles
- **bathos-campaigns** — campaigns, figure manifests, lineage/citation, postmortems, claim-tier pre-registration, signal discrimination and probe design
- **bathos-literature-parity** — validating a reimplemented baseline against a published method
- **bathos-mcp** — MCP tool error envelope and integration contract
- **CLAUDE.md**: Bathos architecture, schema versions, backlog
- **Global rules**: `~/.claude/rules/BATHOS.md` — `uv run python` discipline, sidecar validation, DuckDB conditions
