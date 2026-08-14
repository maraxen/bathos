---
name: bathos-campaigns
description: Campaigns, figure manifests, lineage/citation, postmortems, and claim-tier pre-registration for confirmatory bathos campaigns
triggers: [bth campaign, claim, union gate, figure manifest, postmortem, bth lineage, bth cite, maraxiom, confirmatory campaign]
---

# bathos-campaigns

Grouping runs into campaigns, registering falsifiable claims for confirmatory campaigns, and the lineage/citation/postmortem tooling that closes a campaign out. Assumes the core run-tracking and sidecar workflow from **using-bathos**.

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

Marks campaign closed; queries still work but status is `concluded`. If a claim is registered (see Claim-Tier below), this is also where the Union Gate evaluates.

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

`bth claim scaffold` and `bth claim validate` are also exposed as MCP tools (`claim_scaffold`, `claim_validate`) — see **bathos-mcp**.

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

## Related

- **using-bathos** — run tracking, sidecar pre-registration, controls discipline, catalog queries
- **bathos-cluster** — SLURM submission and sync for the runs grouped here
- **bathos-literature-parity** — validating a reimplemented baseline, feeds `[confounds.reference_parity]` above
- **bathos-mcp** — `claim_scaffold`/`claim_validate` MCP tool contracts
- **CLAUDE.md**: Bathos architecture, schema versions, backlog
