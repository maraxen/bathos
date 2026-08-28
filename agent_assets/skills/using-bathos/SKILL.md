---
name: using-bathos
description: Experiment tracking with bathos — run tracking, sidecar pre-registration, controls discipline, catalog queries
triggers: [bathos, bth, experiment, run, sidecar, catalog, init, lint, controls, stage_name]
---

# using-bathos

bathos (`bth`) is a standalone experiment tracking CLI for researchers running 10+ projects across local and SLURM cluster environments. It tracks script runs, pre-registers hypotheses via sidecars, syncs results to/from clusters, and provides rich query and reporting interfaces.

This skill covers the daily-driver workflow: installation, run tracking, sidecar pre-registration, controls discipline, and catalog queries. For cluster submission and sync, campaigns and claim-tier rigor, literature-parity validation, or MCP tool integration, see the sibling skills listed under **Related** below.

## Core Concepts

**Run** — A single script execution tracked in the catalog. Fields: `id`, `project_slug`, `command`, `argv`, `git_hash`, `git_branch`, `timestamp`, `duration_s`, `exit_code`, `status`, `output_paths`, `tags`, `outcome`, `sidecar_sha256`, `campaign_id`, `slurm_job_id`, `slurm_array_task_id`, postmortem metadata. Dirty runs (with uncommitted changes) get a real snapshot commit via a throwaway index rather than a hash of a possibly-nonexistent tree; remote/cluster dirty runs bundle provenance home via `outputs/provenance/`.

**Sidecar** — A `.bth.toml` file alongside a script that pre-registers hypothesis, expected outcome conditions (DuckDB SQL), and result schema. Enforced by default at `bth run` time (use `--no-sidecar` to bypass, logs `BYPASSED`).

**Outcome** — Evaluated at run-end by matching result JSON against DuckDB SQL conditions in the sidecar. Values: `pass`, `marginal`, `fail`, `error`. One outcome must be marked `is_residual = true`.

**Campaign** — A named group of related runs. Accessible via `bth campaign` subcommands; queries via `campaign_id` field. See **bathos-campaigns** for campaign, claim-tier, postmortem, and lineage workflows.

**Catalog** — Tiered Parquet + DuckDB store at `~/.bth/catalog/` (or `.bth.toml` `[project].catalog_dir`). Cool tier (per-run fragments) → compacted to warm tier (DuckDB database) → optionally archived to cold tier (partitioned Parquet).

**@bth.experiment** — An alternative Python-embedding entry point to provenance capture. Decorate a function with `@bth.experiment` (`bathos.decorators`) to get the same provenance capture as `bth run`, without a CLI invocation. Silently no-ops with a warning if `BTH_PROJECT_SLUG` is unset.

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
- `--tag TAG`, `-t` — Add tag (can repeat)
- `--campaign ID` — Associate with campaign
- `--agent-mode collaborative|autonomous` — Mark collaborative (human-in-loop) or autonomous runs
- `--derived-from RUN_ID` — Link lineage to parent run
- `--no-sidecar` — Bypass sidecar enforcement (logs `BYPASSED`)
- `--allow-stale` — Run anyway despite the sidecar's `[status] stale = true` flag; without it, `bth run` refuses to execute a script marked stale
- `--component-id ID` / `--component-sidecar-sha256 SHA` — Bind this run to an xtrax composition-node/StageBundle slot (cross-tool bridge; niche)

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
bth new-experiment my_experiment
bth new-experiment my_experiment --force  # overwrite existing files
```

Creates `scripts/experiments/my_experiment.py` and `scripts/experiments/my_experiment.bth.toml` skeleton. `name` is a positional argument — `bth new-experiment --name my_experiment` fails with "no such option". (MCP mirror: `new_experiment` tool.)

### Validate Sidecar

```bash
bth validate-sidecar scripts/experiments/train.bth.toml
bth validate-sidecar scripts/experiments/train.bth.toml --campaign my-campaign-id  # also cross-checks claim_discriminates/claim_isolates against the campaign's registered claim
```

Checks TOML syntax, schema completeness, DuckDB condition validity, and residual outcome presence. Prints `field: message` for each error and exits 1 if invalid; prints `✓ <path> is valid` on success.

Note: `bth check` (see **Query and Inspect** below) is a *different* command — it checks already-recorded runs for git-drift against current HEAD, it does not validate sidecars.

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

This section covers Signals 9 and 10 (controls-discipline-relevant); the audit computes 14 signals total — Signals 1-8 and 11/13/14 cover error/bypass rates, outcome entropy, claim registration, and stale-script backlog, and aren't documented in this skill (Signal 12 is covered in **bathos-campaigns**).

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

### Check Git-Drift Validity

```bash
bth check
bth check --status STALE
bth check --check-outputs
```

Compares each recorded run's git hash against current HEAD, reporting `OK` / `STALE` / `DIRTY_RUN` / `UNKNOWN_CODE`. `--check-outputs` additionally verifies registered output files still exist and re-checks their SHA against the recorded hash. Exits 1 if any run is `STALE` or has drift. Not a sidecar validator — see `bth validate-sidecar` above.

### Find Runs by Condition

```bash
bth find --project myproject --status completed
bth find --tag experiment:baseline --tag date:2026-06-01
bth find --slurm-job 12345678
bth find --output-file "outputs/run_*.json"
```

**Options:** `--project/-p`, `--since` (e.g. `7d`, `24h`), `--status`, `--tag` (repeatable), `--slurm-job`, `--output-file` (glob against registered output paths). There is **no `--filter` and no `--limit`** — these are fixed equality/glob filters and every match is printed. For an arbitrary DuckDB `WHERE` clause, use `bth sql` instead:

```bash
bth sql "SELECT * FROM runs WHERE outcome='pass' AND project_slug='myproject'"
```

### Run Arbitrary SQL

```bash
bth sql "SELECT outcome, COUNT(*) FROM runs WHERE project_slug='myproject' GROUP BY outcome"
```

Query catalog directly. Useful for analytics and audits.

## Catalog Management

### Compact Cool Tier → Warm Tier

```bash
bth compact
bth compact --strict          # exit non-zero if any cool fragment is corrupt (CI/automation)
bth compact --force-rebuild   # rebuild bathos.db from cool fragments if the warm DB is corrupt
```

Merges per-run Parquet fragments into DuckDB database. Automatic (banner shown) on `bth ls` if fragmentation is high. A corrupt/unreadable fragment is skipped and reported by default rather than aborting compaction catalog-wide (`bth repair --tier cool` quarantines it); `--strict` restores the old hard-fail behavior.

### Archive Old Runs

```bash
bth archive --project myproject --dry-run   # preview
bth archive --project myproject --archive-dir ~/backups/archive
```

Exports warm-tier runs to cold-tier partitioned Parquet (by project/year/month, default root `~/.bth/archive`) plus a manifest. There is **no `--before` age filter and no `--out` file target** — `archive` exports every run for `--project` (default: all projects); `--dry-run` shows counts without writing. Requires a warm catalog (`bth compact` first).

**Not the same as** the stale-script archive gate (`bth archive-artifact`/`bth restore`) — see "Stale-Script Archive Gate" below.

### Stale-Script Archive Gate

Distinct from `bth archive` above (catalog cold-tier export). A sidecar's `[status] stale = true`
blocks `bth run` unless `--allow-stale` is passed (see Run Tracking Options). `bth archive-artifact`
moves the stale script's tracked bytes into a git-notes-backed ledger for exact later recovery via
`bth restore`.

### Migrate Schema

```bash
bth migrate                      # scans and migrates ALL projects' cool fragments
bth migrate --project myproject  # scope to runs/myproject/ only
bth migrate --dry-run            # preview without writing
```

Upgrades cool-tier Parquet fragments to the current schema. Run after updating bathos. Note: `bth compact` does **not** require this to run first — it already tolerates old-schema fragments on its own.

### Catalog Maintenance & Repair

```bash
bth verify --tier cool        # read-only diagnostic: cool | warm | archive | all
bth repair --tier cool --dry-run
bth repair --tier cool --apply
```

`bth verify` diagnoses catalog integrity issues (sentinel files, corrupt fragments, warm/cool
mismatches) without changing anything. `bth repair` fixes what `verify` finds: sentinel cleanup,
corrupt-fragment quarantine, and (with `--acknowledge-warm-loss`) rebuilding a corrupted warm DB
from cool fragments (`--from-warm` for the reverse direction). MCP mirrors: `repair_scan`, `repair`.

### Project Subdirectories (v0.4+)

```bash
bth migrate-to-project-subdirs
```

Reorganizes flat catalog to per-project subdirectories (`runs/<slug>/run_<uuid>.parquet`). Enables per-project filtering in `bth sync`.

## Visualization (v0.5+)

### Local Dashboard

```bash
bth view
bth view --port 9000 --project myproject --no-open
```

Opens FastAPI dashboard (default: `http://localhost:8080`; `--port`/`--host` override, `--no-open` skips auto-launching a browser, `--project` scopes to one project) with interactive run browser, campaign summaries, outcome histograms.

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
- **Verify sidecar before submission** — run `bth validate-sidecar <path>` to validate outcome conditions; `bth check` checks *recorded runs* against current git HEAD, it does not validate sidecars
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
bth new-experiment baseline_training
# Edit scripts/experiments/baseline_training.py and .bth.toml

# 3. Validate locally
bth validate-sidecar scripts/experiments/baseline_training.bth.toml
uv run python scripts/experiments/baseline_training.py --smoke --out /tmp/test.json  # NOT via bth run — smoke outputs are ephemeral, not tracked

# 4. Run locally or submit to cluster (see bathos-cluster for bth submit)
bth run -- uv run python scripts/experiments/baseline_training.py --out outputs/run.json

# 5. Query results
bth sql "SELECT * FROM runs WHERE outcome='pass' AND project_slug='myproject'"

# 6. Export report
bth export --html --out ~/reports/latest.html
```

## Related

- **bathos-cluster** — SLURM submission (`bth submit`), catalog sync (`bth sync`), remote profiles
- **bathos-campaigns** — campaigns, figure manifests, lineage/citation, postmortems, claim-tier pre-registration, signal discrimination and probe design
- **bathos-literature-parity** — validating a reimplemented baseline against a published method
- **bathos-blast-radius** — trace which past runs a bug fix/commit implicates
- **bathos-mcp** — MCP tool error envelope and integration contract
- **CLAUDE.md**: Bathos architecture, schema versions, backlog
- **Global rules**: `~/.claude/rules/BATHOS.md` — `uv run python` discipline, sidecar validation, DuckDB conditions
