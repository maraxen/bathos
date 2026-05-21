---
name: using-bathos
description: "Use when working in any bathos-tracked research project — experiment execution, run tracking, SLURM job submission, querying results, syncing to cluster, or managing remotes."
---

# SKILL: Using bathos for Experiment Tracking

## Overview

**bathos** is a local-first, zero-server experiment tracking CLI for researchers working across multiple projects and SLURM clusters.

**Core promise:** Never lose track of what ran, where results live, or whether they're still valid.

**Scope:** Single researcher, 10+ projects, SLURM cluster integration.

**Status:** ✅ v0.4.0 shipped — 333 tests passing. Full CLI + MCP server available, including campaigns, agentic integrity gates, and per-project sync filtering.

---

## When to Use This Skill

- **Executing/planning experiments** in research projects with custom scripts
- **Tracking provenance** (git state, command, timing, outputs, SLURM job ID)
- **SLURM job submission** and atomic recording
- **Analyzing results** across multiple runs (filtering, aggregation)
- **Verifying validity** of past runs (`bth check` — git drift detection)
- **Preparing results** for publication or sharing (`bth archive` — cold-tier export)
- **Multi-project analysis** via DuckDB queries

---

## Command Reference

| Command | Arguments | Status | Notes |
|---------|-----------|--------|-------|
| bth init | --slug, --remote, --slurm-partition | ✅ | Project initialization |
| bth run | <script> [-- args], --tag, --out, --campaign, --agent-mode, --derived-from, --no-sidecar | ✅ | Execute + provenance capture; v0.3: `--agent-mode` (collaborative/autonomous), `--derived-from` (lineage), `--campaign` (campaign association), `--no-sidecar` (bypass enforcement) |
| bth ls | --since, --status, --limit, --project | ✅ | List recent runs + OUTCOME column |
| bth show | <run-id> | ✅ | Full run details + git state |
| bth find | --project, --since, --status, --tag, --output-file | ✅ | Flexible filtered query |
| bth sql | "<query>" | ✅ | DuckDB escape hatch (cool/warm) |
| bth compact | (no args) | ✅ | cool→warm consolidation |
| bth check | --status | ✅ | Git drift detection |
| bth archive | --project, --archive-dir, --dry-run | ✅ | Cold-tier partitioned export |
| bth sync | --remote, --pull | ✅ | Cluster rsync |
| bth migrate | --dry-run | ✅ | Upgrade cool-tier fragments to current schema |
| bth campaign create | --name, --mode, --question, --hypothesis | ✅ | Create exploration/confirmation campaign |
| bth campaign list | --status | ✅ | List open/concluded campaigns |
| bth campaign review | <id> | ✅ | Outcome distribution + anomaly flags |
| bth campaign conclude | <id>, --outcome, --note | ✅ | Mark campaign concluded |
| bth lint | --project, --since | ✅ | Residual/bypass/drift rate analysis |
| bth new-experiment | NAME, --force | ✅ | Scaffold script + sidecar |
| bth export | --tool, --level, --dry-run | ✅ | Install skill + MCP server |

---

# SECTION 1: Core Concepts

## Tiered Storage Model

bathos operates on a **four-tier storage system** optimized for SLURM safety and query performance:

- **Hot (memory):** In-process `Run` object during script execution. Lost on exit.
- **Cool (~/.bth/catalog/runs/):** Per-run Parquet fragments, atomic write-then-rename. **SLURM-safe for parallel job arrays.** Survives process termination. Query-slow, but storage-cheap.
- **Warm (~/.bth/catalog/bathos.db):** DuckDB database created by `bth compact` when cool fragments accumulate (>50 files). Primary interactive query target. Fast queries; human-friendly schema.
- **Cold (optional, ~/.bth/catalog/archive/):** Partitioned Parquet (project/year/month) for long-term storage and sharing. v0.2 feature.

**Design rationale:** Cool tier's atomic write-then-rename lets 100 parallel SLURM jobs safely append without locking. Warm tier consolidates on-demand via `bth compact`, avoiding constant I/O. Cold tier enables time-bucketed archival and publication.

## Run Record Structure

Every run is stored as a Parquet row with these **core provenance fields** (13 total):

```
id (UUID)           | Unique identifier, content-addressed
project_slug        | From .bth.toml
command             | Full command line (e.g., "python measure_nvt.py")
argv                | Parsed argument list
git_hash            | SHA1 at run time
git_branch          | Active branch (e.g., "main", "feature-x")
git_dirty           | Boolean (true if uncommitted changes)
timestamp           | ISO 8601, UTC
duration_s          | Wall-clock seconds
exit_code           | Process exit code (0=success)
status              | pending | running | completed | failed
output_paths        | JSON array of registered output files
tags                | Comma-sep labels (e.g., "nvt,tip3p,equilibration")
slurm_job_id        | If present in environment
```

**v0.2 additions:** `hostname`, `outcome` (pass/fail/marginal), `schema_version`, `metadata` (JSON).

## Pre-Registration (Sidecars) — Core Discipline

**Every script in tracked directories (`scripts/experiments/`, `scripts/benchmarks/`, `scripts/validation/`) must have a companion `.bth.toml` sidecar** declaring hypothesis, expected outcomes, and result schema.

- **v0.1:** Warns if sidecar missing; does not block execution.
- **v0.2:** Enforces; blocks execution without valid sidecar.

**Start writing sidecars now.** They are content-addressed by SHA256; renaming scripts is safe.

### Experiment Schema

File: `scripts/experiments/<stem>.bth.toml`

```toml
[experiment]
hypothesis = "Clear, falsifiable statement describing system behavior under conditions"

[outcomes.pass]
condition = "temp_std < 5"              # DuckDB SQL fragment
decision = "Proceed to NPT equilibration"

[outcomes.marginal]
condition = "temp_std >= 5 AND temp_std < 10"
decision = "Tune Langevin gamma; re-run with different seed"

[outcomes.fail]
condition = "temp_std >= 10"
decision = "Investigate thermostat coupling; check PME settings"

[result_schema]
temp_mean = "float"
temp_std = "float"
n_steps = "int"
dt_fs = "float"
ensemble = "str"
```

**Key points:**
- `condition` is evaluated by DuckDB at run-end; no custom DSL.
- Multiple outcome branches allow nuanced decisions (not just pass/fail).
- `result_schema` documents what the script writes.

### Benchmark Schema

File: `scripts/benchmarks/<stem>.bth.toml`

```toml
[benchmark]
baseline_ref = "run_<uuid_of_reference>"
metric = "ns_per_day"
regression_threshold = 0.05             # ±5%
target = "Qualitative goal or reference (e.g., >50 ns/day on pi_so3 GPU)"

[result_schema]
ns_per_day = "float"
system = "str"
atom_count = "int"
ensemble = "str"
dt_fs = "float"
hardware_tag = "str"
```

**Rationale:** Benchmarks compare against a baseline (UUID-based, rename-safe). Regression threshold sets tolerance. Multiple metrics trackable via `result_schema`.

### Debug Schema

File: `scripts/debug/<stem>.bth.toml`

```toml
[debug]
symptom = "Concrete symptom (e.g., 'NaN forces after step 847', 'NPT diverges to 10^115 K')"
suspected_cause = "Initial hypothesis (e.g., 'PME grid aliasing when box < 2*cutoff')"
verification = "Concrete reproduction steps (e.g., 'reproduce with box=4nm, compare box=6nm')"

[verdict_schema]
reproduced = "bool"
root_cause = "str"
fix = "str"
```

**Use case:** Isolate bug symptoms, trace causes, document fixes. Not for publication; for debugging workflows.

## Directory Convention

| Directory | Schema | Sidecar | Tracked | Naming |
|-----------|--------|---------|---------|--------|
| `scripts/experiments/` | experiment | required | Yes | `verb_noun.py` |
| `scripts/benchmarks/` | benchmark | required | Yes | `verb_noun.py` |
| `scripts/validation/` | property+ref+tolerance | optional | Optional | `verb_noun.py` |
| `scripts/analysis/` | none | none | Optional | `verb_noun.py` |
| `scripts/data/` | none | none | No | `verb_noun.py` |
| `scripts/slurm/` | none | none | Via wrapper | `verb_noun.slurm` |
| `scripts/debug/` | debug | optional | No | `YYMMDD_desc.py` |
| `scripts/explore/` | none | none | No | `YYMMDD_desc.py` |
| `scripts/scratch/` | none | none | No (gitignored) | `YYMMDD_desc.py` |

---

## Agentic Integrity — Three-Tier Validation & Governance

bathos enforces a three-tier validation discipline for autonomous and collaborative agent workflows. The tiers work together to prevent drift and unsafe execution.

### Tier 1: Machine-Enforced Invariants (Gate Layer)

**Pre-execution validation — blocks unsafe runs.** Evaluated at `bth run` time before subprocess starts.

- **Sidecar presence:** Every script in `scripts/experiments/` and `scripts/benchmarks/` MUST have a `<stem>.bth.toml` file (fail fast).
- **Sidecar validity:** TOML must parse; all sections required: `[experiment]`/`[benchmark]`, `[outcomes.*]`, `[result_schema]`.
- **Outcome structure:** Each outcome block MUST include:
  - `condition`: DuckDB SQL fragment (validated at gate time, not runtime)
  - `decision`: What to do if this outcome fires
  - `reasoning`: Why this threshold/condition makes sense (cite mechanistic expectations or references, never bare narrative)
  - Exactly one outcome MUST have `is_residual = true` (fallback branch)
- **Agent mode constraints:** Autonomous mode blocks execution on iterative scripts (scripts with prior runs in catalog) unless `--no-sidecar` is explicitly passed.
- **Gate errors:** When validation fails, `bth run` returns exit code 1 and writes structured JSON to stderr with `gate_schema_version=1`, `errors[]`, and `remediation` field. MCP layer surfaces this as a structured tool result (not an exception).

**Agent response to gate failure:** Read `errors[]` and `remediation` fields. Fix sidecar (most common: missing `reasoning` field or invalid SQL condition). Never use `--no-sidecar` to bypass; reserve that flag for exploratory runs in `scripts/explore/`.

### Tier 2: Warm-Catalog Analytics (Lint Layer)

**Post-run monitoring — flags suspicious patterns.** Evaluated by `bth lint` on accumulated runs.

- **Residual rate:** Warn if >10% of runs in a campaign map to the residual outcome (suggests outcomes are too strict or sidecar reasoning is misaligned with empirical results).
- **Bypass rate:** Warn if >10% of runs used `--no-sidecar` (suggests scripts are exploratory and should not be in experiments/ directory).
- **Outcome drift:** Warn if all runs in a campaign map to a single outcome branch (suggests other branches are unreachable or sidecar was over-fitted).
- **Unfired branches:** Warn if an outcome `condition` has never evaluated to true (dead code in sidecar logic).

**Agent response to lint warnings:** Use `bth campaign review <id>` to inspect outcome distribution and anomalies. Adjust `condition` thresholds or `reasoning` fields if empirical results diverge from predictions. If exploratory, move script to `scripts/explore/` and re-run without sidecar.

### Tier 3: Agentic Principles (Workflow Guidance)

**Human+agent decision-making — prose discipline for collaborative workflows.**

- **Hypothesis articulation (pre-run):** State what measurement you expect and why, BEFORE executing. Do not post-hoc rationalize results. Use sidecar `[experiment]` section; cite literature or prior work.
- **Outcome reasoning fields:** Must cite specific thresholds, mechanistic expectations, or reference data — not vague narratives. Example (good): "temp_std < 5K per Berendsen et al. validation on TIP3P in 4nm box at 300K." Example (bad): "stable temperature because it looks right."
- **Autonomous vs. collaborative modes:**
  - **Autonomous:** Only for well-understood experiment types with >5 prior successful runs and consistent outcome patterns. Gate enforces first-of-kind check.
  - **Collaborative:** Default for novel territory. Requires human review before `bth run` proceeds; agent cites sidecar reasoning in dispatch context.
- **Campaign discipline:** Use `exploration` campaigns for discovery (outcome distribution is uncertain). Switch to `confirmation` campaigns when ready to validate a specific hypothesis (tight outcome thresholds, clear success criteria). `bth campaign review` provides anomaly feedback.
- **Unknown outcomes:** If a run's outcome is 'unknown' after completion, check that the script wrote results via `$BTH_RESULTS_PATH` env var and that result schema in sidecar matches script output.
- **When gate fails:** Never retry with `--no-sidecar`. Fix the sidecar: add missing `reasoning`, fix SQL syntax in `condition`, or split overly-strict outcome into marginal + fail branches.

---

## Campaign Workflow (Two-Mode Design)

bathos campaigns enable organized, accountable parametric exploration and hypothesis testing.

### Mode: Exploration

For discovery phase — outcome distribution is uncertain, multiple branches expected to fire.

```bash
bth campaign create "nvt-thermostat-search" --mode exploration \
  --question "Which Langevin coupling strength gives best stability?" \
  --project myproject

bth run python scripts/experiments/nvt_stability.py --campaign <id> \
  -- --gamma 0.1
bth run python scripts/experiments/nvt_stability.py --campaign <id> \
  -- --gamma 0.5
bth run python scripts/experiments/nvt_stability.py --campaign <id> \
  -- --gamma 1.0

bth campaign review <id>
# Output: outcome distribution, residual rate, anomalies
# Interpret results → decide next phase (confirmation or pivot)
```

### Mode: Confirmation

For validation phase — hypothesis is specific, outcomes are pre-registered, success criteria are tight.

```bash
bth campaign create "nvt-validation" --mode confirmation \
  --hypothesis "Langevin gamma=0.5 maintains ±5K stability for 100ps NVT" \
  --project myproject

bth run python scripts/experiments/nvt_stability_100ps.py --campaign <id> \
  -- --gamma 0.5 --seed 1
bth run python scripts/experiments/nvt_stability_100ps.py --campaign <id> \
  -- --gamma 0.5 --seed 2
bth run python scripts/experiments/nvt_stability_100ps.py --campaign <id> \
  -- --gamma 0.5 --seed 3

# All runs should fire the same outcome (pass/marginal/fail)
bth campaign review <id>

# Conclude with outcome label and summary
bth campaign conclude <id> \
  --outcome pass \
  --note "All 3 seeds showed temp_std < 3K; proceeds to NPT phase"
```

### CLI Usage Sequence

```bash
# 1. Create campaign (exploration or confirmation mode)
CAMPAIGN=$(bth campaign create "my-sweep" --mode exploration | jq -r .campaign_id)

# 2. Run experiments, associating each with campaign
bth run python scripts/experiments/script.py --campaign $CAMPAIGN -- --param value

# 3. Monitor accumulated runs
bth campaign review $CAMPAIGN

# 4. Analyze outcome distribution, refine sidecar if needed
bth campaign list --status open

# 5. Conclude when done
bth campaign conclude $CAMPAIGN --outcome success --note "Hypothesis validated"

# 6. Query concluded campaigns
bth campaign list --status concluded
```

---

# SECTION 2: Getting Started

## Installation

### v0.1 (Current)

Install from source:

```bash
cd /home/marielle/projects/bathos
uv pip install -e .
# or
uv tool install --from . bathos
```

### PyPI / uv tool

```bash
uv tool install bathos
```

## Environment Variables

bathos respects these environment variables to control behavior:

| Variable | Default | Usage | Context |
|----------|---------|-------|---------|
| `BTH_CATALOG_DIR` | `~/.bth/catalog/` | Override catalog location | Testing; multi-catalog setups |
| `BTH_PROJECT_SLUG` | From `.bth.toml` | Override project name | SLURM jobs; multi-project repos |
| `SLURM_JOB_ID` | (not set) | SLURM job identifier | Auto-captured by `bth run` |

**Example (SLURM batch script):**

```bash
#!/bin/bash
source scripts/slurm/_bth_env.sh       # Sets BTH_PROJECT_SLUG, BTH_CATALOG_DIR
echo "Running in project: $BTH_PROJECT_SLUG"
uv run bth run python scripts/experiments/measure_nvt.py -- --n-steps 1000
# Automatically captures $SLURM_JOB_ID in run record
```

## Project Setup (First Time)

```bash
cd /path/to/your/research/project
bth init --slug myproject [--remote engaging:~/projects/myproject] [--slurm-partition pi_so3]
```

**Creates:**
- `.bth.toml` — project metadata
- `scripts/` — 9 subdirectories with README
- `.bth/catalog/` — local run storage
- `.gitignore` entry for `.bth/`

## Basic Workflow

1. **Write a script** in `scripts/experiments/measure_nvt_stability.py`
2. **Create sidecar** `scripts/experiments/measure_nvt_stability.bth.toml` with `[experiment]` section
3. **Run with bathos:**
   ```bash
   bth run python scripts/experiments/measure_nvt_stability.py -- --n-steps 1000 --dt 0.5 --out results.json
   ```
4. **List recent runs:**
   ```bash
   bth ls [--since 7d]
   ```
5. **Query results:**
   ```bash
   bth find --status completed --tag tip3p
   ```
6. **Maintain warm tier** (after 50+ runs):
   ```bash
   bth compact
   ```

---

# SECTION 3: CLI Commands Reference

## Available Now (v0.1) ✅

### Setup

**`bth init --slug <name> [--remote host:path] [--slurm-partition P]`**
- Register a project, scaffold directories, initialize catalog
- Creates `.bth.toml` with metadata
- Creates 9 script directories (experiments, benchmarks, validation, analysis, data, slurm, debug, explore, scratch)
- One project per `.bth.toml` per repository

### Execution

**`bth run <script> [-- <args>]`**
- Execute any script; capture provenance atomically to cool tier
- `--out <path>`: register output file(s) for filtering (e.g., `--out results.json`)
- `--tag <label>`: add search tags (repeatable; e.g., `--tag tip3p --tag nvt`)
- Captures `SLURM_JOB_ID` if set in environment
- **Atomicity:** write-then-rename Parquet ensures parallel SLURM safety

Example:
```bash
bth run python scripts/experiments/measure_nvt.py --out nvt_out.json -- --n-steps 1000 --dt 0.5
```

### Query Commands (Hot)

**`bth ls [--since <time>] [--status S] [--limit N]`**
- List recent runs (default 20)
- `--since 7d | 24h | 30m` for relative time filtering
- `--status pending | running | completed | failed`
- Shows: ID, project, status, exit_code, duration_s, command

**`bth show <run-id>`**
- Full details: timestamps, git state (hash, branch, dirty), command, argv, output paths, tags, SLURM job ID

**`bth find --project P --since TIME --status S --tag TAG --output-file GLOB`**
- Flexible filtered query over cool or warm tier
- `--project myproject`: filter by project slug
- `--status pending|running|completed|failed`
- `--tag equilibration`: filter by tag (single)
- `--output-file *.json`: filter by registered output file glob
- All filters AND together

**`bth sql "<query>"`**
- Arbitrary DuckDB query (escape hatch)
- **Cool tier** (before compact): `SELECT * FROM read_parquet('~/.bth/catalog/runs/run_*.parquet')`
- **Warm tier** (after compact): `SELECT * FROM runs` (auto-populated by `bth compact`)

### Maintenance

**`bth compact`**
- Consolidate cool Parquet fragments (>50 files) into warm DuckDB (`bathos.db`)
- Idempotent; safe to run repeatedly
- Dramatically speeds up `ls`, `find`, `sql` on large catalogs
- No data loss; cool fragments remain (optional v0.2 cleanup)

---

---

# SECTION 4: Common Agent Tasks

### Task 1: Seed a New Experiment

Steps:

1. **Verify project initialized:**
   ```bash
   ls .bth.toml || bth init --slug myproject
   ```
2. **Create script** in `scripts/experiments/equilibrate_nvt_water.py` (naming: `verb_noun.py`)
3. **Create sidecar** `scripts/experiments/equilibrate_nvt_water.bth.toml` with `[experiment]` section, outcomes, and result schema
4. **Record dry-run:**
   ```bash
   bth run python scripts/experiments/equilibrate_nvt_water.py --dry-run
   ```
5. **Run full experiment:**
   ```bash
   bth run python scripts/experiments/equilibrate_nvt_water.py --out nvt_results.json -- --n-steps 1000 --dt 0.5
   ```
6. **Verify completion:**
   ```bash
   bth ls --since 5m | grep completed
   ```

### Task 2: Analyze All Runs for a Project

Steps:

1. **List recent runs:**
   ```bash
   bth ls --project myproject --since 7d
   ```
2. **If >50 fragments, compact:**
   ```bash
   bth compact
   ```
3. **Query warm tier:**
   ```bash
   bth sql "SELECT id, exit_code, duration_s FROM runs WHERE project_slug='myproject' AND status='completed' ORDER BY timestamp DESC LIMIT 20"
   ```
4. **Filter by tags:**
   ```bash
   bth find --project myproject --tag equilibration --status completed
   ```
5. **Inspect run:**
   ```bash
   bth show <run-id>
   ```

### Task 3: Check Run Validity After Code Changes (v0.2 Preview)

Steps:

1. **Make code changes, commit:**
   ```bash
   git add -A && git commit -m "Fix temperature control coupling"
   ```
2. **Verify stale runs:**
   ```bash
   bth check
   # Compares recorded git_hash against current HEAD
   ```
3. **Find stale runs:**
   ```bash
   bth find --status stale
   ```
4. **Decision:** Re-run or note limitations in analysis

### Task 4: Submit Experiment to SLURM Cluster

**Prerequisites:**
- Project initialized: `bth init --slug myproject --remote engaging:~/projects/myproject`
- Script validated locally with L1/L2/L3 gates (see CLUSTER.md §7)
- Output directory created: `mkdir -p outputs/logs/slurm`

**Local Validation Gates (CLUSTER.md §7, mandatory before any submission):**

| Gate | Check | Command |
|------|-------|---------|
| L1 dry-run | Paths, imports, no remote fetches at runtime | `uv run python scripts/benchmarks/measure_tip3p.py --dry-run` |
| L2 smoke test | End-to-end CPU run < 60s | `uv run python scripts/benchmarks/measure_tip3p.py --smoke` |
| L3 cluster smoke test | Single reduced-budget task on cluster | `sbatch --array=0-0 --time=0:10:00 scripts/slurm/bench.slurm` |

**Concrete sbatch Template** (`scripts/slurm/bench_tip3p.slurm`):

```bash
#!/bin/bash
#SBATCH --job-name=bench_tip3p
#SBATCH --partition=pi_so3
#SBATCH --time=23:00:00                    # 10% under 24h max (CLUSTER.md §1)
#SBATCH --output=outputs/logs/slurm/%j.out
#SBATCH --error=outputs/logs/slurm/%j.err

# Load bathos environment (v0.2 feature)
source scripts/slurm/_bth_env.sh
# Exports: BTH_PROJECT_SLUG, BTH_CATALOG_DIR, SLURM_JOB_ID

# Verify setup
echo "Project: $BTH_PROJECT_SLUG, Catalog: $BTH_CATALOG_DIR, Job: $SLURM_JOB_ID"

# Execute with bathos (captures SLURM_JOB_ID automatically)
uv run bth run python scripts/benchmarks/measure_tip3p_ns_per_day.py \
  --out bench_results.json \
  --tag cluster \
  -- \
  --n-steps 1000 \
  --dt 0.5
```

**Execution Flow:**

```bash
# Step 1: Local gates (required)
uv run python scripts/benchmarks/measure_tip3p_ns_per_day.py --dry-run      # L1
uv run python scripts/benchmarks/measure_tip3p_ns_per_day.py --smoke        # L2

# Step 2: Cluster smoke test (single task)
sbatch --array=0-0 --time=0:10:00 scripts/slurm/bench_tip3p.slurm
# Wait for completion; check output/logs/slurm/<jobid>.out

# Step 3: Scale to full array (after smoke test passes)
sbatch --array=0-9%5 scripts/slurm/bench_tip3p.slurm
# %5 = 5 concurrent tasks (CLUSTER.md §2: keep %M ≤ 10-16)

# Step 4: Monitor runs
squeue -u marielle                                  # Watch job status
ssh engaging "tail -f ~/projects/myproject/outputs/logs/slurm/<jobid>.out"  # Live tail

# Step 5: Retrieve results (back on laptop)
bth sync --remote engaging                         # pull cool fragments
# OR manually:
rsync -azP --dry-run engaging:~/.bth/catalog/runs/ ~/.bth/catalog/runs/
rsync -azP engaging:~/.bth/catalog/runs/ ~/.bth/catalog/runs/

# Step 6: Analyze
bth ls --since 1h                                  # See cluster runs
bth compact                                        # Consolidate if >50
bth find --tag cluster --status completed         # Filter cluster runs
```

**SLURM Integration Notes:**

- `_bth_env.sh` is v0.2; in v0.1, set `BTH_PROJECT_SLUG` manually in script
- `SLURM_JOB_ID` is auto-set by sbatch; `bth run` captures it → queryable later
- **CLUSTER.md reference:** Consult §1 (partition walltime limits), §2 (job submission, array sizing), §3 (rsync safety), §4 (queue monitoring), §6 (environment setup, no `module load`)
- Omit `module load` directives; use `uv` for Python
- Test script locally first with all three gates before scaling

### Task 5: Multi-Run Analysis Across Projects

Steps:

1. **Initialize bathos in each project:**
   ```bash
   cd /path/to/proj_A && bth init --slug proj_A
   cd /path/to/proj_B && bth init --slug proj_B
   ```
2. **Compact each project:**
   ```bash
   # In proj_A: bth compact
   # In proj_B: bth compact
   ```
3. **Query across projects:**
   ```bash
   # Cool tier (before compact):
   bth sql "SELECT project_slug, COUNT(*), AVG(duration_s) FROM read_parquet('~/.bth/catalog/runs/run_*.parquet') GROUP BY project_slug"
   
   # Warm tier (after compact):
   bth sql "SELECT project_slug, COUNT(*), AVG(duration_s) FROM runs GROUP BY project_slug"
   ```
4. **Compare outcomes manually:**
   ```bash
   bth find --project proj_A --status completed | wc -l
   bth find --project proj_B --status completed | wc -l
   ```

---

# SECTION 5: Integration with Agent Workflows

### Dispatch Context for Orchestrators

When dispatching a sub-agent for experiment execution, include:

```
RESEARCH_CONTEXT:
  Project root: /home/marielle/projects/myproject
  bathos initialized: verify with `ls .bth.toml`
  Script directory: scripts/experiments/ (or benchmarks/, debug/)
  Script naming: verb_noun.py
  Sidecar required: scripts/experiments/<stem>.bth.toml with [experiment] section
  
BATHOS COMMANDS:
  bth run <script> -- <args> [--tag X] [--out PATH]
  bth ls [--since 7d]
  bth find --status failed --tag <label>
  bth show <run-id>
  bth sql "<query>"
```

### Verification Gates Before Task Completion

**Before claiming "experiment submitted":**
```bash
bth ls --since 5m
# Must show 1 row with status='running' or 'completed'
```

**Before claiming "results analyzed":**
```bash
count=$(bth sql "SELECT COUNT(*) FROM runs WHERE project_slug='X' AND status='completed'")
# Must match expected count (e.g., 10 for 10-parameter sweep)
```

### Distinguishing bathos Runs from OODA Logging

**bathos:** Tracks individual **script executions** (provenance: git state, command, exit code, output paths, duration). Results live in `~/.bth/catalog/runs/` (cool tier, per-run Parquet) and `bathos.db` (warm tier, consolidated DuckDB).

**OODA logging** (`.praxia/*.jsonl`): Tracks **agent decision phases** (recon, plan, audit, research). Records agent reasoning and phase transitions.

**Complementary, not redundant:** An agent dispatched to run an experiment will create BOTH a bathos run record AND OODA phase records. Query bathos for research results; query OODA for agent reasoning.

### Query Patterns

**Find failed experiments:**
```bash
bth find --status failed --tag myexp
bth show <run-id>  # Inspect exit_code and stderr
```

**Aggregate performance across runs:**
```bash
bth compact
bth sql "SELECT tag, AVG(duration_s), MIN(duration_s), MAX(duration_s) FROM runs WHERE status='completed' GROUP BY tag"
```

**Track output files:**
```bash
bth find --output-file "*.json" --status completed
```

---

# SECTION 6: Architecture Decision Locks

These decisions constrain implementation and enable agent safety:

| Decision | Choice | Agent Implication |
|----------|--------|-------------------|
| Pre-registration | **Sidecar TOML** (not decorators) | Agents can validate sidecars without importing Python |
| Outcome eval | **DuckDB SQL fragments** (not custom DSL) | Agents write outcome logic in plain SQL |
| Atomicity | **write-then-rename Parquet** (not locking) | Agents can dispatch 100+ parallel SLURM jobs safely |
| Storage | **Cool + Warm tiers** (not single tier) | Agents query warm DB fast; jobs write cool fragments atomically |
| Addressing | **Content-addressed runs** (not path-based) | Runs don't break if scripts are moved/renamed |
| Query | **DuckDB + SQL** (not custom filters) | Agents reuse SQL knowledge; no new DSL |

**Implications for agents:**
- Always use `bth run` (not manual Parquet writes)
- Always use `bth find` / `bth sql` (not raw file access)
- After SLURM jobs: verify with `bth ls` (not polling logs)
- Before dispatching analyzer: ensure runs have status='completed'
- For cluster workflows: use `bth sync` or manual rsync before analysis

---

# SECTION 7: Example Scenarios

### Scenario A: Run Single Experiment (v0.1)

```bash
bth run python scripts/experiments/test_nvt.py -- --n-steps 1000
# Expected: Exit 0, status='completed'
bth ls | grep completed
```

### Scenario B: Submit 10 Parameter Sweeps to SLURM (v0.1+v0.2)

```bash
for param in $(seq 0.1 0.1 1.0); do
  sbatch --job-name=sweep_$param scripts/slurm/sweep.slurm $param
done

# Later:
bth ls --since 1h | wc -l
# Should show ~10 runs
```

### Scenario C: Check Result Validity (v0.2 Preview)

```bash
# Make code change, commit
git commit -am "Fix temperature thermostat"

# Check stale runs
bth check
bth find --status stale
# Decision: which runs to repeat?
```

### Scenario D: Analyze Outcomes (v0.2 Preview)

```bash
bth compact
bth sql "SELECT outcome, COUNT(*) as count FROM runs WHERE outcome IS NOT NULL GROUP BY outcome"
# Shows: pass:45, marginal:8, fail:2
```

### Scenario E: Export for Publication (v0.2 Preview)

```bash
bth archive --project X --year 2026 --month 05
ls ~/.bth/catalog/archive/project=X/year=2026/month=05/
# Partitioned, shareable Parquet ready for zenodo/figshare
```

### Scenario F: Compare Two Projects (v0.1)

```bash
bth find --project proj_A --status completed | wc -l
# → 87 runs

bth find --project proj_B --status completed | wc -l
# → 62 runs

# Aggregate:
bth sql "SELECT project_slug, COUNT(*) FROM read_parquet('~/.bth/catalog/runs/run_*.parquet') GROUP BY project_slug"
```

### Scenario G: Cluster Sync Workflow (v0.2 Preview)

```bash
# On cluster, in SLURM job:
source scripts/slurm/_bth_env.sh
bth run python scripts/benchmarks/measure_nvt.py -- --n-steps 1000

# At home, after job completes:
bth sync --remote engaging --pull
# OR manually: rsync -azP engaging:~/.bth/catalog/runs/ ~/.bth/catalog/runs/

bth ls --since 1h
# See runs recorded on cluster
```

### Scenario H: Rerun Failed Experiment (v0.1)

```bash
bth find --status failed --tag variant_A
# → shows failed run ID

bth show <run-id>
# Inspect exit_code, command, git state

# Make code changes, commit
git commit -am "Fix NaN handling in force calculation"

# Rerun with same arguments
bth run python scripts/experiments/measure_x.py --out results.json -- <same args>
# New run created; old one remains for comparison
```

---

# SECTION 8: FAQ & Common Pitfalls

### Q: I ran a script but bathos didn't record it. Why?

**A:**
- Verify `.bth.toml` exists: `ls .bth.toml`
- Verify env var in SLURM jobs: `echo $BTH_PROJECT_SLUG`
- Use `bth run <script>`, not `python <script>` directly
- Check: `bth ls | head` should show the run
- Check `.bth/catalog/runs/` for Parquet fragments

### Q: My cool tier has 500 fragments. Performance issue?

**A:** Not broken, but queries are slow. Run `bth compact` to consolidate into warm-tier DuckDB. Then `bth ls`, `bth find`, `bth sql` are fast again.

### Q: Can I run experiments on two different machines?

**A:** Yes, if sharing catalog via `bth sync` or NFS. Cool tier is SLURM-safe (atomic) but not network-FS-safe (multiple writers can corrupt). Warm tier is single-writer (DuckDB).

### Q: How do I delete a run?

**A:** Currently impossible (intentional: audit trail). Filter it out in queries or mark invalid in metadata (v0.2 feature). Runs are immutable records.

### Q: Can I use bathos for hyperparameter tuning?

**A:** No. Use Optuna, Ray Tune, or similar. bathos is provenance tracking, not optimization. It records hyperparameter sweeps you run; it doesn't search the space.

### Q: My script has no output files. Can I still track it?

**A:** Yes. Just `bth run script.py` and query provenance (command, exit_code, duration, git state). Output file registration (`--out`) is optional.

### Q: My SQL query broke after I upgraded bathos. Why?

**A:** Schema evolution is handled automatically. `bth migrate` upgrades cool-tier Parquet fragments to the current schema. `bth compact` applies migration logic during warm-tier ingestion. `bth show <run-id>` includes `schema_version` so you can verify what version a run was recorded with.

### Q: Can I use bathos with notebooks (Jupyter)?

**A:** Not yet. Scripts only. Notebooks will be addressed in v0.3 (future roadmap).

### Q: How do I query across multiple projects with different .bth.toml files?

**A:** Each project has its own `.bth.toml` and catalog. Use `bth sql` with manual path globs (cool tier) or union queries if all projects compact to the same warm DB (future: `bth sync` handles this). v0.2 multi-catalog feature will simplify this.

### Common Pitfalls

- **Forgetting `--out`**: Output files aren't tracked. Queries with `--output-file GLOB` won't find them. Register all results.
- **SLURM jobs without `_bth_env.sh`**: `SLURM_JOB_ID` isn't captured. Can't link runs to job logs later.
- **Missing sidecars**: v0.1 warns; v0.2 enforces. Start writing them now to avoid surprises.
- **Running `bth compact` in parallel**: DuckDB is single-writer. Serialize `bth compact` calls.
- **Assuming cool fragments auto-cleanup**: They don't. Older versions remain until `bth archive`.

---

# SECTION 9: MCP Tool Reference (v0.2 — Fully Shipped)

**Status:** FastMCP server ships with bathos. Start with `bth-mcp` (stdio transport). Register automatically via `bth export --tool claude --level user` (writes skill + wires MCP server). If MCP tools unavailable in your environment, use CLI commands instead. Semantics are identical.

### All Shipped Tools ✅

| MCP Tool | Arguments | Return | CLI Equivalent |
|----------|-----------|--------|----------------|
| `list_runs` | `catalog_dir, limit, since, status` | `{runs: [], count: int}` | `bth ls` |
| `find_runs` | `catalog_dir, project, since, status, tag, output_file` | `{runs: [], count: int}` | `bth find` |
| `get_run` | `run_id, catalog_dir` | `{run: {id, command, git_hash, outcome, ...}}` | `bth show` |
| `run_sql` | `query, catalog_dir` | `{rows: []}` | `bth sql` |
| `init` | `project_root, slug, remote, slurm_partition` | `{success: bool, msg: str}` | `bth init` |
| `run` | `script_path, args, project_slug, catalog_dir, output_paths, tags, agent_mode, campaign_id, no_sidecar` | `{script_path, exit_code, success}` or gate error | `bth run` |
| `compact` | `catalog_dir` | `{ingested: int, skipped: int}` | `bth compact` |
| `archive` | `project, archive_dir, dry_run, catalog_dir` | `{runs: int, partitions: int}` | `bth archive` |
| `check` | `catalog_dir, project_root, status_filter` | `{results: [], total: int}` | `bth check` |
| `sync` | `remote, pull, catalog_dir` | `{transferred: int, duration_s: float}` | `bth sync` |
| `campaign_create` | `name, mode, project_slug, catalog_dir, question, hypothesis` | `{campaign_id, name, mode, status, started_at}` | `bth campaign create` |
| `campaign_list` | `catalog_dir, project_slug, status` | `{campaigns: [], count: int}` | `bth campaign list` |
| `campaign_review` | `campaign_id, catalog_dir` | `{total_runs, residual_rate, outcome_distribution, anomalies}` | `bth campaign review` |
| `campaign_conclude` | `campaign_id, outcome_label, conclusion, catalog_dir` | `{status: 'concluded', campaign_id, outcome_label}` | `bth campaign conclude` |

### Registration

```bash
# Claude Code — user level (all projects)
bth export --tool claude --level user

# Claude Code — workspace level (this project only)
bth export --tool claude --level workspace

# Gemini CLI — user level
bth export --tool gemini --level user
```

Both skill and MCP server are registered in one command. The MCP entry:
- **Claude Code:** written to `~/.claude.json` (user) or `.mcp.json` in CWD (workspace)
- **Gemini CLI:** merged into `~/.gemini/settings.json` (user) or `.gemini/settings.json` (workspace)

---

# SECTION 10: When to Dispatch vs. Manual Execution

### Dispatch a Sub-Agent When

- Task requires **custom Python/Rust script** (write experiment code, data processing)
- Task involves **file I/O and argument parsing** (use `bth run`, not manual scripting)
- Task requires **git workflow awareness** (commit before/after runs, branch tracking)
- Task needs **SLURM job submission** (create + submit .slurm script, monitor)
- Task involves **sidecar creation** (write TOML schemas)

### Execute bathos CLI Directly When

- Querying existing runs (`bth ls`, `bth find`)
- Inspecting a single run (`bth show`)
- Compacting or archiving (housekeeping, v0.2)
- Checking git validity (`bth check`) — v0.2
- Ad-hoc SQL queries (`bth sql`)

### Decision Flow

```
Request: "Run measure_stability.py on 5 parameters"
  → Multiple parameters + loop
  → Needs git awareness (commit parameter files)
  → Dispatch sub-agent (write script, loop, bth run, commit)

Request: "Show me all failed runs from today"
  → Pure query, no side effects
  → Execute directly: bth find --since 24h --status failed

Request: "Create benchmark sidecar for my script"
  → TOML creation (text; no execution)
  → Can execute directly or dispatch depending on scope
```

---

# APPENDIX A: Future Scenarios (v0.2 Preview)



### Scenario I: Pre-Registration Validation

```bash
# Agent creates script
cat > scripts/experiments/validate_nvt.py << 'EOF'
#!/usr/bin/env python
# ... experiment code ...
print('{"temp_mean": 300.5, "temp_std": 2.3, "n_steps": 1000}')
EOF

# Agent creates sidecar
cat > scripts/experiments/validate_nvt.bth.toml << 'EOF'
[experiment]
hypothesis = "NVT with Langevin coupling maintains ±5K stability"

[outcomes.pass]
condition = "temp_std < 5"
decision = "Proceed to NPT"

[outcomes.marginal]
condition = "temp_std >= 5 AND temp_std < 10"
decision = "Tune gamma, re-run"

[outcomes.fail]
condition = "temp_std >= 10"
decision = "Debug thermostat"

[result_schema]
temp_mean = "float"
temp_std = "float"
n_steps = "int"
EOF

# CLI validates sidecar before execution
bth run python scripts/experiments/validate_nvt.py -- --n-steps 1000
# v0.2 output captures outcome = 'pass' or 'marginal' or 'fail' automatically
```

### Scenario J: Metadata Enrichment & Analysis

```bash
# After warm compaction, outcome evaluated and stored
bth compact

bth sql "SELECT id, outcome, metadata FROM runs WHERE outcome IS NOT NULL LIMIT 5"
# metadata is JSON string; parse for custom fields
# e.g., {"temperature": 300, "box_size": 4.0, "algorithm": "PME"}

# Agent processes outcomes
bth sql "
  SELECT outcome, COUNT(*) as count, AVG(duration_s) as avg_time
  FROM runs
  WHERE project_slug = 'myproject'
    AND status = 'completed'
  GROUP BY outcome
"
# Result: pass:123, marginal:8, fail:2
```

### Scenario G: Cluster Sync Workflow (v0.2 Preview, Extended)

```bash
# On cluster, in SLURM job:
source scripts/slurm/_bth_env.sh
uv run bth run python scripts/benchmarks/measure_tip3p.py \
  --out bench.json \
  --tag cluster \
  -- \
  --n-steps 1000

# After job completes, cool-tier Parquet written to:
# ~/.bth/catalog/runs/run_<uuid>.parquet (on cluster)

# At home, sync catalog:
bth sync --remote engaging --pull
# Downloads ~/.bth/catalog/runs/run_*.parquet from cluster

# Query synced runs:
bth ls --since 1h | grep cluster
bth find --tag cluster --status completed

# Compact if needed:
bth compact

# Analyze:
bth sql "
  SELECT slurm_job_id, duration_s, exit_code
  FROM runs
  WHERE tag LIKE '%cluster%'
    AND status = 'completed'
"
```

---

## Summary for Agents

**bathos is:**
- A **provenance tracker** for experiments across projects
- **SLURM-safe** via atomic write-then-rename storage
- **Query-first** with DuckDB backend
- **Low-ceremony** (no server, no auth, local-first)
- **Extensible** via sidecars (TOML pre-registration)

**Use it to:**
1. Execute scripts with provenance: `bth run`
2. Query results: `bth ls`, `bth find`, `bth sql`
3. Maintain catalogs: `bth compact`, `bth check`, `bth sync`
4. Track outputs and validity across projects

**Integration points:**
- Dispatch agents to write/test scripts; verify with `bth ls`
- Dispatch agents to analyze runs; use `bth find` / `bth sql`
- Verify SLURM submissions with `bth ls --since 1h`
- Track sidecar creation in pre-registration workflows
