---
name: bathos-campaigns
description: Campaigns, figure manifests, lineage/citation, postmortems, and claim-tier pre-registration for confirmatory bathos campaigns
when_to_use: 'Triggers: bth campaign, claim, union gate, figure manifest, postmortem, bth lineage, bth cite, maraxiom, confirmatory campaign.'
---
# bathos-campaigns

Grouping runs into campaigns, registering falsifiable claims for confirmatory campaigns, and the lineage/citation/postmortem tooling that closes a campaign out. Assumes the core run-tracking and sidecar workflow from **using-bathos**.

## Campaigns

### Create Campaign

```bash
bth campaign create "baseline sweep" --mode confirmation --question "..." --hypothesis "..."
```

Returns campaign ID.

**Note:** `name` is a positional argument; real options are `--mode`, `--sequential`, `--question`, `--hypothesis`, `--parent`. There is no `--description` flag — no such field exists in the Campaign schema.

### List Campaigns

```bash
bth campaign ls
```

Shows campaign name, mode, status, question/hypothesis, run count.

### Add Runs to Campaign

```bash
bth campaign add <run-id> --campaign-id <campaign-id>
```

Links runs to campaign.

**Note:** One run per invocation (repeat the command per run) — flag is `--campaign-id`, no `-c` shorthand; `--runs` (plural) does not exist.

### Review Campaign Results

```bash
bth campaign review <campaign-id>
```

Reports residual rate, bypass rate, outcome distribution, anomalies, POPPER sequential-test summary (for sequential campaigns), and `blast_radius_status`/`claim_blast_radius_status` (see **bathos-blast-radius**).

### Conclude Campaign

```bash
bth campaign conclude <campaign-id>
```

Marks campaign closed; queries still work but status is `concluded`. If a claim is registered (see Claim-Tier below), this is also where the Union Gate evaluates.

**Flags:**
- `--outcome` — required; specifies the campaign outcome (e.g., `pass`, `fail`, `inconclusive`)
- `--negative-check TEXT` — required when `--outcome` is negative-sounding and a claim is registered (see **bathos-rigor-gates** for the detection vocabulary)
- `--abort-if-below-threshold` — halts the conclude operation if metric thresholds are not met

**Obligations:** The post-mortem Obligations system (`[obligations]` in `.bth.toml`) is documented in **bathos-rigor-gates**.

## Figure Manifest (Campaign → Maraxiom)

When a campaign concludes, bathos emits `figure_manifest.json` at
`~/.bth/catalog/sidecars/<campaign_id>/figure_manifest.json`. Maraxiom reads this
during `mrx check` freshness sweeps (F7/F8 signals) to confirm figure pins are current.

### Register figure outputs during a run

Pass figure file paths alongside the result JSON using repeated `--output-paths` flags (any file type is valid; repeat the flag for each path):

```bash
bth run \
  --output-paths outputs/results/my_run.json \
  --output-paths outputs/figures/scatter.svg \
  --output-paths outputs/figures/barplot.png \
  --campaign-id <campaign-id> \
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
- `output_path` (str) — Path to the data file within the bathos catalog (typically registered via `bth run --output-paths`).
- `sha256` (str) — SHA256 hash of the data product (immutability guarantee). This is the hash of the **DATA file** (e.g., JSON result), not the rendered figure.

### Consuming the Manifest

**Quick read (CLI):** For a quick read without writing Python, use:

```bash
bth report show-manifest <campaign-id>
```

**Programmatic read:** Import and read the manifest programmatically:

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

**Note:** `--format dot` is listed in `--help` but is **not actually implemented** — it errors with 'dot format not yet implemented' (exit 1). Only `text` and `prov` currently work.

**Multi-parent DAG:** `bathos.provenance.format_prov_json` supports multi-parent DAG lineage via a `run_parent_edges` parameter, but neither `bth lineage --format prov` nor the `lineage_prov` MCP tool currently passes it in — today's PROV output is always single-parent. See **bathos-rigor-gates** for the library-only multi-parent API (`bathos.campaign_edges`).

### Citation String

```bash
bth cite <run-id>
```

Emits a structured citation (markdown or `--format json`) linking the run to its hypothesis-sidecar hash, manifest hash, git SHA, and outcome — not a bibliographic (BibTeX/APA) format.

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

### Show Postmortem

```bash
bth postmortem show <run-id>
```

Retrieves and displays postmortem metadata.

**Note:** The CLI verb is `show` — the MCP tool is confusingly named `postmortem_get` (a genuine CLI/MCP naming mismatch). Both `scaffold` and `show` now accept `--campaign-id` to scaffold/show a campaign-scoped postmortem instead of a run-scoped one.

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
bth claim register .bth/claims/<campaign-name>.claim.toml --campaign-id <campaign-id>
#    amending a registered claim and re-registering requires --force (writes an audit event)

# 4. Run the campaign; each confirmatory sidecar declares which hypotheses it discriminates / isolates
#    (see "Signal discrimination and probe design" below for how to design those runs)

# 5. Conclude — the Union Gate checks clause coverage
bth campaign conclude <campaign-id> --outcome pass
```

`bth claim scaffold` and `bth claim validate` are also exposed as MCP tools (`claim_scaffold`, `claim_validate`) — see **bathos-mcp**.

### Authoring via typed payload

**Alternative to hand-editing:** `bth claim author --from-json <payload.json> <path>` (or MCP `claim_author`) is an alternative to hand-editing the claim TOML — same document, written through a typed `ClaimPayload` model that's rendered, re-parsed, and validated before anything reaches disk. See **bathos-mcp** for the calling contract (error envelope, write-token requirement).

### Descriptive labels (opaque IDs)

Claim entities use short **ids** for machine cross-referencing (`H_information_symmetry`, `C_topology_coupling`) and a required **`label`** field for human-readable output. If an id matches the opaque pattern `/^[A-Z][0-9]+$/` (e.g. `H1`, `C2`), `bth claim validate` **errors** when `label` is blank. `bth lint` scans `.bth/claims/*.toml` and emits **warnings** for missing or placeholder labels (`REQUIRED: …`) before you register. Conclude-gate messages, parity confound checks, and `claim_coverage_*.json` (`clause_labels`) prefer labels over raw ids where available.

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
parity_run_id     = ""                                    # the run that establishes parity — see bathos-literature-parity

# which planned run separates which hypothesis pair (every row needs a predicted_outcome)
[[claim.discriminability]]
hypothesis_a      = "H_main_effect"
hypothesis_b      = "H_null_misspec"
planned_run_label = "sweet_spot"
predicted_outcome = "advantage_ci_lower_gt_0"

[claim.union_gate]
[[claim.union_gate.clauses]]
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

**Confirmatory-rigor extensions:** Three related fields — `kill_condition_satisfiable_by_null` (bool, on `[claim]`, AC-23), the `positive_control = true` tag on a `[[claim.union_gate.clauses]]` entry, and the `[confounds.synthetic_recovery]` sub-block — are documented in full in **bathos-rigor-gates**, which covers the confirmatory-rigor layer built on top of this claim schema.

**Review Coverage Gate:** A `[review]` sidecar block (separate from the claim file itself) is a sibling gate to the Union Gate below, firing at the same `bth campaign conclude` moment — see **bathos-rigor-gates**.

### The Union Gate at `conclude`

- A clause is **covered** when some run has all of its `hypothesis_ids` in its `claim_discriminates`.
- **confirmation / sequential** campaign: an uncovered clause downgrades the verdict to `confounded`
  (not `pass`). `bth campaign conclude --force` bypasses, recording `claim_mode='bypassed'`.
- **exploration** campaign: the checks still run but are warn-only — no downgrade.
- Modifying the claim file after registration → `conclude` errors on the SHA mismatch; re-register with `--force`.
- **Signal 12** (`bth sprint-audit`) flags a confirmation campaign with no registered claim — the one case
  where the gate silently does nothing.
- `bth campaign review`'s `blast_radius_status`/`claim_blast_radius_status` fields (informational, non-gating) report whether any covering run has been flagged by a blast-radius assessment — see **bathos-blast-radius**.

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

Each probe maps to one or more `[[claim.union_gate.clauses]]` in `claim.bth.toml`.
A clause is covered when at least one run has all of its `hypothesis_ids` in `claim_discriminates`.

Typical pattern: one scaled-divergence probe covers the main-effect clause; one null-injection probe covers the eval-validity clause; one information-ablation probe covers each isolation clause.

Signal 12 (`bth sprint-audit`): fires when a confirmation campaign has no `claim_path` registered — the Union Gate will not run at conclude, and the probe design above will have no enforcement.

## Step-Back Reviews & Trajectory Escalation

While the Union Gate and claim tier look **inward and downward** (*is this run valid, is this claim supported?*), the Step-Back Review subsystem looks **outward and upward** (*is this line of work still worth doing, and does it still matter?*).

Step-back reviews protect campaigns from behavioral lock-in, endless parameter-tweaking loops, creeping thresholds with diminishing returns, and proxy optimization (Goodharting). They are evaluated automatically on a system cadence and event triggers, or can be invoked on demand.

### Checking Step-Back Status

Check whether a campaign has active triggers, pending reviews, or open escalation holds:

```bash
bth stepback status <campaign-id>
bth stepback status <campaign-id> --verbose
```

FastMCP equivalent: `stepback_status(campaign_id="<campaign-id>", verbose=True)`.

Returns:
- `current_level`: `0` (Nominal), `1` (Advisory Warning), `2` (Step-Back Hold), or `3` (Human Chair Escalation)
- `active_triggers`: list of firing trigger names and metrics
- `review_count`: total reviews recorded
- `last_review`: summary of most recent review (recommendation, score, reviewer)
- `snoozed_triggers`: list of active snooze windows and expiration conditions

### Evaluating System Triggers

Evaluate whether campaign trajectory triggers are currently firing:

```bash
bth stepback trigger <campaign-id>
bth stepback trigger <campaign-id> --window 5 --dry-run
```

FastMCP equivalent: `stepback_trigger(campaign_id="<campaign-id>", window=5, dry_run=False)`.

Triggers evaluate mathematical properties over the member run sequence:
1. `repeated_marginal`: 3 consecutive marginal runs ($s \ge 3$) or $\ge 60\%$ marginal in last $k$ runs.
2. `near_bound`: 2+ runs skating within 10% of decision boundary ($d_i \le 1.0$) or 1 run within 2.5% ($d_N \le 0.25$).
3. `entropy_collapse`: Shannon outcome entropy collapsing ($H_e < 0.35$ nats over $k \ge 5$ runs).
4. `metric_plateau`: OLS relative moving slope $< 1\%$/run ($\bar{\beta}_{\text{rel}} < 0.01$) with $CV < 0.02$.
5. `stale`: 14 days idle, or 15 runs without resolving a single hypothesis or Union Gate clause.

### Snoozing and Acknowledging Triggers

When an intentional parameter sweep or exploratory phase fires a non-L3 trigger, acknowledge and snooze it to prevent review fatigue while maintaining safety:

```bash
bth stepback snooze <campaign-id> --trigger repeated_marginal --runs 5 --rationale "Exploring solvent density bounds"
```

FastMCP equivalent: `stepback_snooze(campaign_id="<campaign-id>", trigger_name="repeated_marginal", runs_window=5, rationale="...")`.

Constraints:
- Bounded: max 10 runs (`--runs 10`) or 72 hours (`--hours 72`).
- Rationale is mandatory and non-empty.
- **Invariance**: Level 3 Human Chair escalations can **never** be snoozed.

### Running a Step-Back Review Workflow

Execute the full step-back review orchestration workflow:

```bash
bth stepback run <campaign-id>
```

FastMCP equivalent: `stepback_run(campaign_id="<campaign-id>")`.

Orchestration pipeline:
1. Evaluates all 5 system triggers over member runs.
2. If triggers fire and campaign is at Level 0, escalates to Level 1 (Advisory Warning).
3. Dispatches an independent reviewer (see **bathos-rigor-gates** and **bathos-step-back** for Blind Reviewer Protocol).
4. Emits advisor brief and logs telemetry.

### Recording a Review

Record an independent evaluation using either Path A (Scored Rubric) or Path B (Qualitative Open Analysis):

```bash
# Path A: Scored, versioned 5-dimension rubric (1-5 scale)
bth stepback record <campaign-id> \
  --path rubric \
  --scores '{"question_validity": 4, "empirical_progress": 3, "theoretical_grounding": 4, "resource_efficiency": 4, "risk_confound_surface": 3}' \
  --evidence '["run:run_1234abcd", "sidecar:scripts/experiments/opt.bth.toml#outcomes.pass"]' \
  --reviewer-id "critic_agent_1"

# Path B: Qualitative open analysis (Advisor, Pre-Mortem, So-What, Outsider, Steelman-The-Stop)
bth stepback record <campaign-id> \
  --path open_analysis \
  --mode pre_mortem \
  --recommendation PIVOT \
  --text "Assuming this campaign fails at 6 months, the root cause will be unmodelled solvent polarization." \
  --evidence '["run:run_1234abcd", "run:run_5678ef01"]' \
  --reviewer-id "critic_agent_1"
```

FastMCP equivalents: `stepback_record(...)` and `stepback_review(...)`.

### Rendering the Advisor Brief & Project Status Matrix

Generate an advisor-ready briefing document (Markdown) suitable for a real thesis committee or advisor one-on-one, and export the status string for the `"End / reconsider"` column of `PROJECT_STATUS.advisor.md`:

```bash
bth stepback brief <campaign-id>
bth stepback show <campaign-id> --format brief
```

FastMCP equivalent: `stepback_brief(campaign_id="<campaign-id>")` or `stepback_show(campaign_id="<campaign-id>", format="brief")`.

The brief summarizes:
1. Stakes & Scientific Question
2. Trajectory & Outcomes
3. Threshold Basis & Drift
4. Alternative Hypotheses & Dissent
5. Exit Criteria & Sunk Cost
6. Escalation Status & Chair Disposition
7. Project Status Matrix Entry (e.g. `[HOLD L2: repeated marginals (3/5 runs); review rev_1234 recommending PIVOT]`)

### Escalation Ladder and Resolution

Transition a campaign along the 4-tier escalation ladder:
- **Level 0 (Nominal)**: Normal progression.
- **Level 1 (Advisory Warning)**: Soft warning banner on run launches and CLI queries.
- **Level 2 (Step-Back Hold)**: Active hold. In confirmation/sequential mode, conclusion verdict downgrades to `"confounded"` unless resolved or forced.
- **Level 3 (Human Chair Escalation)**: Highest escalation. Reached via catastrophic findings or committee referral.

```bash
# Escalate campaign
bth stepback escalate <campaign-id> --target-level 2 --reason "3 consecutive marginal runs skate decision bound"

# Resolve / De-escalate campaign (Levels 0, 1, 2)
bth stepback resolve <campaign-id> --deescalate-to 0 --note "Pivot plan accepted; new hypothesis registered"
```

FastMCP equivalents: `stepback_escalate(...)` and `stepback_resolve(...)`.

**Human Chair Invariance (Iron Law)**:
FastMCP `stepback_resolve` strictly and unconditionally **refuses** to resolve Level 3 escalations (`STEPBACK_AGENT_OVERRIDE_REFUSED`). Level 3 resolution requires an interactive human session via TTY (`sys.stdin.isatty()`) or a secure chair token at `~/.bth/chair_token`. Autonomous agents can **never** close, de-escalate, or override a Level 3 escalation.

For exhaustive methodology, blind reviewer isolation rules, and rubric dimension definitions, consult **bathos-step-back** and **bathos-rigor-gates**.

## Related

- **using-bathos** — run tracking, sidecar pre-registration, controls discipline, catalog queries
- **bathos-step-back** — dedicated skill for step-back review orchestration, rubric dimensions, cognitive modes, and blind reviewer protocol
- **bathos-rigor-gates** — post-Union-Gate confirmatory rigor, Decision D1 non-blocking posture, and blind reviewer isolation
- **bathos-cluster** — SLURM submission and sync for the runs grouped here
- **bathos-literature-parity** — validating a reimplemented baseline, feeds `[confounds.reference_parity]` above
- **bathos-mcp** — `claim_scaffold`/`claim_validate` and `stepback_*` MCP tool contracts
- **CLAUDE.md**: Bathos architecture, schema versions, backlog

