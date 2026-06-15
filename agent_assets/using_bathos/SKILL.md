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

Two separate gate mechanisms protect experimental discipline:

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

### Figure Manifest Schema

The manifest is a structured JSON sidecar stored at `<catalog>/sidecars/<campaign_id>/figure_manifest.json`.

**Root schema:**
```json
{
  "manifest_version": "1.0",
  "campaign_id": "<campaign-id>",
  "figures": [...]
}
```

**Fields:**
- `manifest_version` (str) — Schema version (e.g., `"1.0"`). Used for backward-compatibility.
- `campaign_id` (str) — Campaign ID this manifest belongs to. Must match the sidecar directory name.
- `figures` (list[FigureEntry]) — List of figures. Empty list `[]` is valid (no figures to render).

**FigureEntry schema:**
```json
{
  "figure_id": "scatter_cross_model_r",
  "intent": "Cross-model energy correlation — mismatch-ceiling verification",
  "input_pins": [...],
  "render_state": "ready",
  "figure_kind": "analysis_chart"
}
```

**Fields:**
- `figure_id` (str) — Unique figure identifier (slug format, e.g., `"scatter_cross_model_r"`).
- `intent` (str) — Human-readable intent describing what the figure is meant to show. Example: `"main result"`, `"supplementary ablation"`, `"owner-side comparison"`.
- `input_pins` (list[InputPin]) — Data sources this figure derives from (see InputPin schema below). Typically one pin for analysis figures; may be multiple for comparisons.
- `render_state` (str) — One of `"ready"` or `"deferred"`.
  - `"ready"` — Figure is fully rendered and available.
  - `"deferred"` — Figure intent is pinned but rendering is blocked (e.g., needs owner-only data or styling).
- `figure_kind` (str | None) — Optional figure kind (freeform vocabulary). Examples: `"analysis_chart"`, `"structural"`. `null`/absent indicates unclassified or legacy figure.

**InputPin schema:**
```json
{
  "run_id": "run_abc123",
  "output_path": "outputs/results/my_run.json",
  "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
}
```

**Fields:**
- `run_id` (str) — Bathos run ID that produced the data product.
- `output_path` (str) — Path to the data file within the bathos catalog (typically registered via `bth run --out`).
- `sha256` (str) — SHA256 hash of the data product (immutability guarantee). This is the hash of the **DATA file** (e.g., JSON result), not the rendered figure.

### Consuming the Manifest

Import and read the manifest programmatically:

```python
from bathos.figure_manifest import FigureManifest

manifest = FigureManifest.read_manifest(
    Path("~/.bth/catalog/sidecars/camp_abc123/figure_manifest.json")
)

for fig in manifest.figures:
    print(f"{fig.figure_id}: {fig.intent} ({fig.render_state})")
    for pin in fig.input_pins:
        print(f"  run {pin.run_id} -> {pin.output_path}")
        # Verify immutability via sha256
        assert pin.sha256 == compute_sha256(pin.output_path)
```

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

## MCP Error Envelope

All 22 bathos MCP tools return a typed envelope with consistent structure. Understanding the envelope shape and error codes is essential for robust integrations.

### Envelope Shape

Every successful or failed MCP call returns a dictionary with these four keys (always present; KeyError is impossible):

```json
{
  "ok": true,
  "error_code": null,
  "error": null,
  "resolution_hint": null,
  "data_field_1": "...",
  "data_field_2": "..."
}
```

**Fields:**
- `ok` (bool) — `true` if call succeeded; `false` if error
- `error_code` (str | null) — `null` on success; one of 16 BathosErrorCode values on error
- `error` (str | null) — `null` on success; human-readable error message on error
- `resolution_hint` (str | null) — `null` on success; actionable fix suggestion on error
- Additional fields vary by tool (on success only)

### BathosErrorCode Values (16 total)

**Gate-derived codes (11, aliased from GateErrorCode):**
- `sidecar_missing` — Sidecar `.bth.toml` not found
- `sidecar_invalid` — TOML syntax error or missing required sections
- `sidecar_hash_mismatch` — Sidecar content changed; hash mismatch detected
- `not_first_of_kind` — Run of this script already exists; use `--derived-from`
- `manifest_write_failed` — Failed to write `.bth.postmortem.toml` manifest
- `adversarial_check_missing` — Missing `adversarial_check` in `outcomes.pass` blocks
- `hypothesis_lock_missing` — Hypothesis lock file not found
- `outcome_evaluation_error` — DuckDB SQL condition parsing or evaluation failed
- `result_schema_mismatch` — Result JSON doesn't match declared schema
- `outcome_ambiguous` — Multiple outcome conditions matched (exactly one expected)
- `internal` — Unexpected internal error

**Domain-specific codes (5):**
- `catalog_error` — Database or Parquet I/O failure
- `campaign_error` — Campaign query or update failure
- `sidecar_error` — Sidecar parsing or content validation error
- `export_error` — Export (HTML, archive) generation failure
- `invalid_param` — Invalid parameter or argument

### Caller Pattern (Standard Case)

For most tools, check `ok` and extract data:

```python
result = await session.call_tool("bathos", "list_runs", {"project_slug": "myproject"})
if not result["ok"]:
    raise RuntimeError(
        f"[{result['error_code']}] {result['error']}\n"
        f"Hint: {result['resolution_hint']}"
    )

# On success, access tool-specific data
runs = result["runs"]
for run in runs:
    print(f"{run['id']}: {run['outcome']}")
```

### Special Case: validation_ok (postmortem_validate, validate_sidecar)

Two tools use a different validation result field:

```python
result = await session.call_tool("bathos", "validate_sidecar", {"script_path": "scripts/experiments/train.bth.toml"})

# Transport always succeeds (ok=True)
assert result["ok"] is True

# Validation result is in validation_ok (NOT ok)
if not result["validation_ok"]:
    for error_msg in result.get("errors", []):
        print(f"Validation error: {error_msg}")
else:
    print("Sidecar is valid")
```

**Envelope shape for these tools (success):**
```json
{
  "ok": true,
  "error_code": null,
  "error": null,
  "resolution_hint": null,
  "validation_ok": true,
  "path": "..."
}
```

**Envelope shape for these tools (failure):**
```json
{
  "ok": true,
  "error_code": null,
  "error": null,
  "resolution_hint": null,
  "validation_ok": false,
  "errors": ["field1: error message", "field2: error message"]
}
```

**Why two validation fields?**
- `ok` indicates transport success (the MCP call itself worked)
- `validation_ok` indicates semantic success (the sidecar/postmortem is structurally valid)
- `errors` is absent on success; present as a list of human-readable validation issues on failure

If a validation fails for reasons outside the tool (missing file, permission denied), both `ok` and `validation_ok` are `false`, and the error message is in `error`, not `errors`.

## Related

- **CLAUDE.md**: Bathos architecture, schema versions, backlog
- **Global rules**: `~/.claude/rules/BATHOS.md` — `uv run python` discipline, sidecar validation, DuckDB conditions
- **Cluster rules**: `~/.claude/rules/CLUSTER.md` — SLURM partition limits, job submission, local validation gates
