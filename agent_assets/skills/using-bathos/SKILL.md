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

## Claim-Tier Pre-Registration (confirmatory campaigns)

The sidecar pre-registers a single *run*. A **claim** pre-registers the *campaign* — the headline a set of
runs is meant to establish, what would falsify it, and which runs discriminate which hypotheses. It exists to
prevent the failure where several narrow gates each pass but their union does not establish the headline.
Use it for `confirmation` / `sequential` campaigns; `exploration` campaigns are exempt.

**The discipline is: author and *register* the claim before the confirmatory runs.** The Union Gate at
`bth campaign conclude` only fires when a claim is registered — an unregistered claim provides no enforcement
at all (`bth sprint-audit` Signal 12 flags exactly this).

### Workflow

```bash
# 1. Scaffold a claim template (pulls hypotheses / outcome labels from the catalog)
bth claim scaffold <campaign-id>
#    -> writes .bth/claims/<campaign-name>.claim.toml + per-run sidecar snippets

# 2. Author it (headline, kill_condition, hypotheses, confounds, discriminability, clauses), then validate
bth claim validate .bth/claims/<campaign-name>.claim.toml

# 3. Register BEFORE any confirmatory run — records claim_path + claim_sha256 (the tamper anchor)
bth claim register .bth/claims/<campaign-name>.claim.toml --campaign <campaign-id>
#    amending a registered claim and re-registering requires --force (writes an audit event)

# 4. Run the campaign; each confirmatory sidecar declares which hypotheses it discriminates / isolates
#    (see "Signal discrimination and probe design" below for how to design those runs)

# 5. Conclude — the Union Gate checks clause coverage
bth campaign conclude <campaign-id> --outcome pass
```

`bth claim scaffold` and `bth claim validate` are also exposed as MCP tools (`claim_scaffold`, `claim_validate`).

### `claim.bth.toml` anatomy

```toml
[claim]
headline       = "<falsifiable proposition the campaign must establish>"
kill_condition = "<result that would falsify it>"        # mandatory; no bypass
regime         = "<parameter range the claim ranges over; runs must cover it>"

# >= 2 hypotheses, each with a descriptive id + label; ONE must be a null / misspecified alternative
[[hypotheses]]
id    = "H_main_effect"
label = "the proposed mechanism drives the effect"
predicted_signature = "monotone improvement with signal"
[[hypotheses]]
id    = "H_null_misspec"
label = "both wrong / measurement misspecified"
predicted_signature = "flat or non-monotone response"

# load-bearing assumptions; the campaign halts if one is falsified
[[assumptions]]
id      = "A_info_symmetry"
label   = "method and baseline have symmetric access to the signal"
halt_if = "one method uses information the other lacks"
status  = "untested"

# one row per confound; status must reach "controlled" for a pass verdict
[[confounds]]
id            = "C_baseline"
label         = "baseline is the published method, not a weak reimplementation"
control       = "reference-parity gate"
isolating_run = ""
status        = "uncontrolled"
# optional sub-block when the baseline is a reimplementation of a published method:
[confounds.reference_parity]
reference_paper   = "Author YEAR"
reference_metric  = "recovery_hamming"
reference_value   = 0.0
equivalence_bound = 0.0
parity_run_id     = ""                                    # the run that establishes parity

# which planned run separates which hypothesis pair (every row needs a predicted_outcome)
[[claim.discriminability]]
hypothesis_a      = "H_main_effect"
hypothesis_b      = "H_null_misspec"
planned_run_label = "sweet_spot"
predicted_outcome = "advantage_ci_lower_gt_0"

[union_gate]
[[union_gate.clauses]]
id             = "C_main_effect"
description    = "primary hypothesis distinguishable from the null on the target metric"
hypothesis_ids = ["H_main_effect", "H_null_misspec"]      # cross-ref to [[hypotheses]] ids
```

Confirmatory **sidecars** cross-reference the claim by short id:

```toml
[experiment]
claim_discriminates = ["H_main_effect", "H_null_misspec"]  # hypotheses this run separates
claim_isolates      = ["C_baseline"]                        # confound / variable this run isolates
```

### The Union Gate at `conclude`

- A clause is **covered** when some run has all of its `hypothesis_ids` in its `claim_discriminates`.
- **confirmation / sequential** campaign: an uncovered clause downgrades the verdict to `confounded`
  (not `pass`). `bth campaign conclude --force-verdict` bypasses, recording `claim_mode='bypassed'`.
- **exploration** campaign: the checks still run but are warn-only — no downgrade.
- Modifying the claim file after registration → `conclude` errors on the SHA mismatch; re-register with `--force`.
- **Signal 12** (`bth sprint-audit`) flags a confirmation campaign with no registered claim — the one case
  where the gate silently does nothing.

## Signal discrimination and probe design

Before submitting a confirmatory campaign, design runs that actively discriminate between competing
hypotheses. Each probe type targets a different failure mode.

### Probe types

**Scaled-divergence probe**
Purpose: Confirm the effect scales with the signal — rules out ceiling/floor effects masking the null.
Design: Run the same experiment at 3+ signal levels (e.g., K=2, K=4, K=8 for an information-content claim).
Expected signature: monotonic improvement tracking the signal; flat response falsifies the claim.
Discriminates: Genuine causal effect vs. threshold artifact or capacity bottleneck.
Sidecar field: `claim_discriminates = ["H_main_effect", "H_scaling"]`

**Planted-mode probe**
Purpose: Verify the model actually uses the planted information — rules out spurious correlation.
Design: Run with the planted signal deliberately corrupted or ablated; model must fail.
Expected signature: performance degrades to chance on the ablated version.
Discriminates: Information-use vs. pattern matching on surface cues unrelated to the planted signal.
Sidecar field: `claim_discriminates = ["H_information_use", "H_null"]`

**Null-injection probe**
Purpose: Confirm the null hypothesis is actually falsifiable by the eval.
Design: Submit a known-bad model or a random-output baseline through the full eval pipeline.
Expected signature: null model scores at chance; if it scores above chance, the eval is miscalibrated.
Discriminates: Eval sensitivity vs. leakage from training data or shared artifacts.
Sidecar field: `claim_discriminates = ["H_null", "H_eval_validity"]`

**Information-ablation probe**
Purpose: Isolate which specific information channel drives the result.
Design: Ablate one information source at a time (sequence identity, structural context, coevolution signal).
Expected signature: performance drops precisely when the claimed channel is removed; other ablations leave performance intact.
Discriminates: Channel-specific contribution vs. redundancy or compensation across channels.
Sidecar field: `claim_isolates = ["V_sequence_identity"]`

### Connecting probes to the Union Gate

Each probe maps to one or more `[[union_gate.clauses]]` in `claim.bth.toml`.
A clause is covered when at least one run has all of its `hypothesis_ids` in `claim_discriminates`.

Typical pattern: one scaled-divergence probe covers the main-effect clause; one null-injection probe covers the eval-validity clause; one information-ablation probe covers each isolation clause.

Signal 12 (`bth sprint-audit`): fires when a confirmation campaign has no `claim_path` registered — the Union Gate will not run at conclude, and the probe design above will have no enforcement.

## Validating a reimplemented baseline (literature-parity)

When you reimplement a method from a published paper — especially one that publishes no reference code — the reimplementation can silently diverge from the described method, confounding any downstream comparison (`[confounds.reference_parity]` in claim-tier language). A unit test cannot catch this: the reimplemented method runs, passes internal checks, and produces plausible numbers. This section documents bathos's structured validation protocol.

### When to use literature-parity validation

**Use this workflow when:**
- Your project reimplements a method from a peer-reviewed publication
- The original paper publishes no reference code (or the code diverges significantly from the paper text)
- The reimplementation will be compared against the published results or other baselines
- You need to flag the `[confounds.reference_parity]` confound as *controlled* for downstream claim-tier gates

**Outcome:** A graded parity run with verdict PARITY (faithfully reimplemented), PARTIAL (controlled deviations documented), or FAIL (significant discrepancies). The verdict controls whether the F2 conclude-gate and F3 submit-gate allow downstream campaigns to proceed.

### The 5-phase protocol

The protocol is **operator-driven** (you orchestrate the agents) and **blind-first** (reconstructors see only the paper text, not your code or prior summaries). The steps are:

**Phase 1: Blind reconstruction (N independent agents, default N=3)**
- Each agent independently reconstructs the method from the paper **only**
- Reconstruction follows diverse lenses: mathematical formulation, algorithmic detail, experimental protocol
- Agents record ambiguities they encounter rather than guessing
- No cross-talk; agents do not see each other's work or your code

**Phase 2: Reconcile**
- Compare the N reconstructions; flag disagreements (likely indicating paper ambiguity or misreads)
- Map each reconstructed clause onto your actual code with a verdict (MATCH / DEVIATION / MISSING / AMBIGUOUS)
- Produce a checklist of code-to-paper correspondences

**Phase 3: Adversarial refutation (M independent attacks, default M=3)**
- Each attacker assumes a defect and tries to prove it using different evidence channels
- Channels: statistical correctness, hyperparameter fidelity, algorithmic structure
- Each attacker must state its assumption upfront (honesty-tax); default to "deviation" if evidence is inconclusive
- Goal: find or rule out mechanism-nullifying defects that unit tests missed

**Phase 4: Adjudicate**
- Confirm findings by ≥2-vote or hard evidence (runnable invariant tests you write)
- Rank severity: does this deviation affect the method's core behavior?
- Recommend fixes (code changes to restore parity, or documented deviations)

**Phase 5: Graded verdict**
- Compute grade from evidence: PARITY (all clear), PARTIAL (controlled deviations), FAIL (significant discrepancies)
- Produce an executable **invariant-test spec** — synthetic ground-truth tests that lock in the verdict
  (see AC-15 / AC-20: the tests are registered in the run's `output_paths` and checksummed via SHA; drift is detectable via `bth check`)
- Write a reproduce-the-protocol plan (how to restore your implementation to parity if needed)
- Populate `[confounds.reference_parity]` block for the next campaign

### Configuration: `parity.bth.toml`

Create a `parity.bth.toml` sidecar alongside the relevant script. Example structure:

```toml
[parity]
paper_pdf              = "path/to/paper.pdf"          # Source of truth (required)
impl_paths             = [
  "src/myproject/method.py",
  "src/myproject/baseline.py"
]                                                      # Your implementation files (required)
reference_code         = null                          # Optional: if the paper published code, path to it
citation_note          = "arXiv:1234.5678 describes the method in §3.2–3.4"
recon_lenses           = [
  "math",
  "algo",
  "protocol"
]                                                      # Default if omitted; customize for your paper
attack_lenses          = [
  "stats",
  "hyper",
  "struct"
]                                                      # Default if omitted
hypotheses             = [
  "core mechanism (coevolution reshuffle) is implemented faithfully",
  "metric readout captures the intended signal"
]                                                      # Your upfront hypotheses about what could go wrong
equivalence_bound      = 0.05                          # Tolerance for numeric equivalence (if applicable)
N                      = 3                             # Number of reconstructors (default 3)
M                      = 3                             # Number of refutation attackers (default 3)
```

**Required fields:** `paper_pdf`, `impl_paths`  
**Optional fields (with sensible defaults):** `recon_lenses`, `attack_lenses`, `equivalence_bound`, `N`, `M`, `hypotheses`, `citation_note`, `reference_code`

### Orchestrator-owned re-derivation lock (Constraint 1)

**This is critical and skill-enforced only in v1** (no code-enforced gate):

> After the agents' phases complete, **you (the orchestrator) must independently re-derive the decisive findings using runnable tests.** Never trust an agent's assertion "parity is established"; run your own synthetic-ground-truth invariant tests to confirm.

In practice: if Phase 3 or 4 identifies a potential defect (e.g., "the method is invariant to coevolution signal"), write a `tests/test_<method>_invariants.py` that explicitly tests that claim and run it to failure and success.

**Why this matters:** The Zeinaty 2026 case in `asr` caught a mechanism-nullifying bug via this discipline: the paper's metric readout was mathematically invariant to the core mechanism (it contributed exactly zero to the reported result). Three sprints of unit tests missed this; an invariant-test specification locked in by orchestrator re-derivation found it immediately.

### Integration with claim-tier gates

Once a parity run completes successfully:

1. **Record the run ID** — `bth show <run-id>` to get its UUID
2. **Populate `[confounds.reference_parity]` in your campaign's `claim.bth.toml`**:
   ```toml
   [[confounds]]
   id = "C_baseline"
   label = "baseline is the published method, not a weak reimplementation"
   [confounds.reference_parity]
   parity_run_id = "run_12abc345..."  # from the parity run
   ```
3. **At campaign conclude** (F2 gate) — the Union Gate reads the parity run's verdict:
   - PARITY or PARTIAL → confound marked controlled; campaign proceeds
   - FAIL → confound uncontrolled; confirmation/sequential campaign downgrades to `confounded`
4. **At campaign submit** (F3 gate) — a reproduction sidecar can declare a prerequisite parity run:
   ```toml
   [reproduction]
   requires_parity = "run_12abc345..."  # hard-blocks validation/production if uncontrolled
   ```

### Evidence channels and evidence severity

Literature-parity relies on multiple evidence channels working together:

- **C1 (reconstruction parity):** N independent agents converge on the same interpretation of the paper (agreement = higher confidence)
- **C4 (adversarial severity):** M attackers try diverse refutation strategies; if all fail or find only minor deviations, confidence increases
- **D2 (evidence channels):** reconstruction via math, algorithm, protocol; refutation via stats, hyperparameter, structure
- **E1 (reproduction rung):** R0 = text parity only; R1 = numeric equivalence; R2–R4 = partial/full reproducibility with published code
- **D3 (manifest-declared mode):** your `parity.bth.toml` declares whether Mode A (code-published) or Mode B (text-only) applies

### Grading: the cap-lattice ceiling table

The final verdict (PARITY / PARTIAL / FAIL) is **computed automatically** from evidence using a cap-lattice (no human adjudication):

- **Invariant-test failure** → FAIL (no override)
- **Clause-parity % below threshold** → caps to PARTIAL
- **Adversarial survival** — all refutations failed or found only minor issues → boosts toward PARITY
- **Ambiguity load (load-bearing)** — unresolved paper ambiguities in core mechanism → caps to PARTIAL
- **Reproduction rung R2 or worse** (partial reproducibility, missing systems) → caps to PARTIAL

The compute-grade function returns the minimum across all applicable ceilings.

### Related

- **`parity.bth.toml` fields and validation**: see `parity.bth.toml.template` in `agent_assets/skills/using-bathos/literature-parity/`
- **Phase templates** (orchestrator-facing agent prompts): in `agent_assets/skills/using-bathos/literature-parity/` (01_reconstruct.md through 05_verdict.md)
- **Signal 13** (`bth sprint-audit`): flags a confirmation campaign citing a published-method baseline with uncontrolled `reference_parity`
- **AC-16–AC-22** (epic-level acceptance criteria): all parity-related gates and integration points

## Related

- **CLAUDE.md**: Bathos architecture, schema versions, backlog
- **Global rules**: `~/.claude/rules/BATHOS.md` — `uv run python` discipline, sidecar validation, DuckDB conditions
- **Cluster rules**: `~/.claude/rules/CLUSTER.md` — SLURM partition limits, job submission, local validation gates
