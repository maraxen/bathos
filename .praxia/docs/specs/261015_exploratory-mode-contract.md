---
title: 'Exploratory-mode contract: pre-register the question, not a verdict'
description: Operationalize exploration vs calibration vs validation in bathos so exploratory runs stop inventing pass/marginal/fail thresholds, with a gated promotion path to confirmatory
status: draft
task_id: 261015_exploratory-mode-contract
date: '261015'
backlog_ids: ''
adversarial_review: ''
---
# Exploratory-mode contract: pre-register the question, not a verdict

**Status:** draft, not yet adversarially reviewed · **task_id:** 261015_exploratory-mode-contract

## 1. Problem

bathos only offers two ways to run analytical work, and both are wrong for exploration:

| Option | What happens | Why it fails exploration |
|---|---|---|
| `scripts/explore/` or `--no-sidecar` | untracked | The global rule ("any run that will produce a finding gets a sidecar") forbids citing anything from it. The first interesting observation then can't be used. |
| `scripts/experiments/` + sidecar | `[outcomes]` with `condition` + `decision` + `reasoning` + an `is_residual` branch is **mandatory** | You must declare pass/marginal/fail cutoffs before you know what you're looking for. The cutoffs are arbitrary, and they then pose as findings (`outcome='pass'` in the catalog). |

Nothing in between says: *"I am looking at X. I will produce plots/tables Y. I am not testing anything yet."*

The arbitrary thresholds are not a minor annoyance. They actively corrupt the record:
1. A made-up `pass` sits in the catalog alongside real pre-registered verdicts. Readers, and later agents, can't tell them apart.
2. Because the label is enforced, agents spend effort tuning cutoffs instead of looking at the data. This is the pattern in memory `feedback_threshold_cutoffs` (it has already reshaped project direction once).
3. When the visualization then shows something unexpected, there is no first-class path from "noticed" to "test it". So the follow-up either reuses the same data (circular) or is never written down.

## 2. What exists today (grounded)

Each claim below was read at file:line on main @ 2161670a. None of these are spikes.

| Fact | Where | Tag |
|---|---|---|
| `[experiment].stage_name` ∈ {exploration, calibration, validation, ablation, production}, default `exploration` | `sidecar.py:215,273` | verified |
| `[outcomes]` is required for every experiment sidecar regardless of stage | `validate.py:578-588` | verified |
| Each branch must have condition, decision and reasoning, plus one `is_residual` branch, and some outcome must reference a `result_schema` field | `validate.py:591-711` | verified |
| `bth new-experiment` scaffolds `stage_name="exploration"` **together with** pass/marginal/fail at `metric < 5.0 / 10.0` | `new_experiment.py:51-79` | verified (this template is the friction, verbatim) |
| `stage_name` changes behaviour in only two places: the `requires_pass_stem` gate (`cli_cyclopts.py:752-763`) and the parity gate (`parity.py:249-252`) | grep | verified |
| Campaign `mode` ∈ {exploration, confirmation, sequential}. Exploration only softens gates at **conclude** (warning, no downgrade) | `campaigns.py:29,1090,1171,1214,1322` | verified |
| MCP `campaign_create` accepts only exploration/confirmation | `mcp.py:1990` (= debt #2080) | verified |
| `OutcomeSpec.source` and Tier-2 lint `check_threshold_basis` shipped (item #760); the lint is warning-only | `sidecar.py:37`, `linter.py:870` | verified |
| Runs carry `derived_from` (sweep parent), but nothing models "this hypothesis came from that observation" | `mcp.py:1782` | verified |
| Runs record no input-data hashes, so "confirm on data you didn't explore" can't be checked mechanically yet | debt #2683, #2686 | unverified for confirm-path use. Treat it as **attested, not enforced** until #2686 lands |

So there are three vocabularies: run `stage_name`, campaign `mode`, and the directory taxonomy. They overlap, none of them changes what a run must pre-register, and two of them disagree with each other (#2195, #2080).

### Related open debt (this spec subsumes or touches these; no existing item covers the core issue)
- **#2195**: `stage` vs `stage_name`; "confirmation is a campaign mode not a run stage". This spec resolves it by mapping stage → epistemic class (§3.1).
- **#2080**: MCP rejects `mode='sequential'`. Fold this in as a trivial fix.
- **#2224**: outcome-partition gaps turn a gate into a human override. Exploratory runs stop having partitions at all, which removes most of the surface.
- **#2343 / #2241 / #2243**: `outcome='unknown'` is ambiguous. This spec adds a reserved `observed` label so "intentionally not evaluated" can't be confused with "evaluation broke".
- **#2677**: estimand discipline. This is the confirmatory-side complement. Out of scope here, but the promotion scaffold (§3.4) is where an estimand would be declared.

## 3. Design

### 3.1 One axis: epistemic class, derived from `stage_name`

No new field. `stage_name` already exists and defaults to the right value. Give it a contract:

| stage_name | class | Pre-register | `[outcomes]` | Citable as |
|---|---|---|---|---|
| `exploration` | **exploratory** | `question` + `[observe]` (what will be produced) | optional; if present, every numeric cutoff needs `source` | an *observation* ("we saw…"), never evidence for a claim clause |
| `calibration` | **calibrating** | what quantity is being calibrated (noise floor, MDE, null distribution) | optional; records the *value*, no verdict needed | a threshold *source* for later runs (`source = "run:<id>:<field>"`) |
| `validation` / `ablation` / `production` | **confirmatory** | hypothesis + full `[outcomes]` as today | required (today's rules, unchanged) | evidence |

Campaign `mode` is derived: a `confirmation` campaign may hold only confirmatory runs (plus calibration runs it names as threshold sources). An `exploration` campaign may hold anything. This answers #2195: confirmation is a property of the campaign, and the run's class is what the campaign checks against.

### 3.2 The exploratory sidecar

```toml
[experiment]
stage_name = "exploration"
question = "How does per-residue MI distribute across the PDZ β2–β3 loop in apo vs holo?"

[observe]                       # what you commit to producing and looking at; NOT a test
outputs = ["mi_heatmap.png", "per_residue_mi.csv"]
looking_for = "any structure: hubs, apo/holo asymmetry, seed-dependent artefacts"   # free text, optional
sanity = ["n_frames_pooled > 0", "mi_diag_is_entropy"]    # instrument checks, not cutoffs

[result_schema]                 # still recommended; enables later queries
n_frames_pooled = "int"
top_hub = "str"
```

Validator changes (`validate.py`), when the class is exploratory:
- `[outcomes]` absent → **OK**. `question` is required in place of `hypothesis`; `hypothesis` stays accepted as an alias.
- `[observe].outputs` required and non-empty. This keeps the pre-registration meaningful: you said what you'd look at *before* looking. It is also what lets #2343's missing-`--output-paths` warning fire correctly.
- `sanity` entries are DuckDB booleans over `result_schema` with no numeric literal other than 0/1. They guard the instrument (cf. BATHOS.md positive/negative controls), not the hypothesis. A failure records `invalid_measurement`, reusing the existing reserved label (`sidecar.py:285`).
- `[outcomes]` present → each numeric literal in a condition needs `source` (literature, `run:<calibration_id>:<field>`, or `provisional:<why>`). This promotes `check_threshold_basis` from a Tier-2 warning to a validation **error** for exploratory runs only. Confirmatory runs keep the warning, and can be ratcheted separately.

Runner change: an exploratory run with no `[outcomes]` records `outcome = 'observed'`, a new reserved label. It never records `unknown`, so it is distinguishable from the broken-evaluation bugs.

### 3.3 Noticing: an observation is a first-class record

The step where examining the data "leads to follow-up" gets its own verb:

```
bth observe <run_id> "MI hub at H71 in holo only; absent in 2 of 3 seeds"  [--figure mi_heatmap.png] [--tags hub]
```

- This appends to a ledger (`~/.bth/catalog/observations/<project>/obs_<id>.parquet`, written atomically like the cool tier). It is linked to the run and the output sha.
- It is citable as *"observed in run X"*. That is what the global rule needs in order to stop forbidding exploratory findings: an observation *is* tracked, and it is labelled as not-a-test.
- It exists as an MCP tool too (`observe`). Agents doing EDA call it instead of writing a verdict.
- Seed disagreement is recorded as a *description*, never as pass/fail. This is consistent with `md-replicas-are-samples.md`.

### 3.4 Promotion: observation → confirmatory test

```
bth new-experiment confirm_h71_hub --from-observation <obs_id>
```

This scaffolds a **validation-stage** sidecar pre-filled with:
- `hypothesis` drafted from the observation text, for the user to edit.
- `[provenance] derived_from_observation = "<obs_id>"`, which records that the hypothesis was data-generated.
- **`[holdout]` (required when `derived_from_observation` is set):** what makes this test independent of the data that suggested it. Valid kinds: `new_seeds`, `new_builds`, `held_out_split`, `new_system`, `literature_prediction`. Recorded as an attestation now. Mechanically checked against input hashes once #2686 adds input-artifact anchors. Until then, lint warns when the confirmatory command's argv matches the source run's argv.
- **Thresholds are not scaffolded as numbers.** Each `[outcomes]` condition is emitted as `metric <op> ${THRESHOLD}` with a `source` that must be filled. The scaffold suggests, in order: (a) a calibration run (noise floor / spike-in MDE, see §3.5), (b) literature, (c) `provisional:` with a reason. A sidecar with an unfilled `${…}` fails validation. That is the point: the number has to be chosen consciously, and its basis is recorded.
- The effect size seen in the exploratory run may be shown as context, with a warning. It is the optimistic, selection-biased estimate, so it can't serve as a `source`. Lint rejects `source = "run:<the exploratory run>"`.

### 3.5 Where thresholds legitimately come from: calibration runs

A calibration run measures the quantity a threshold should be made of: the noise floor, the null distribution, the spike-in minimum detectable effect, or seed-to-seed spread at the pooled level. It records the value, not a verdict. A later confirmatory sidecar cites it as `source = "run:<id>:<field>"`. Lint checks that the run exists, is `stage_name="calibration"`, and has the field. This turns "arbitrary threshold" into "threshold = 2× the measured noise floor of run abc", which is a choice that is defensible, reviewable and re-derivable. The multiplier itself still needs its own basis, or a `provisional:` reason.

### 3.6 Gates that read the class

- **Union Gate / claim register:** reject exploratory runs as evidence for a clause. Message: "run X is exploratory (observed); promote via `bth new-experiment --from-observation`".
- **Confirmation campaign add:** refuse exploratory runs, and allow calibration runs.
- **sprint-audit, new signal:** the rate of exploratory sidecars that still carry verdict-labelled `[outcomes]` without `source`, i.e. residual cutoff-invention. This is the metric for whether the friction went down.

## 4. Non-changes (explicit)
- Confirmatory pre-registration rules are untouched. This spec loosens nothing that a citable verdict depends on.
- `scripts/explore/` stays unenforced (true scratch). The new contract is for exploration you intend to keep and cite.
- No new run-schema column for class: it is derived from `stage_name`, which is already in cool and warm. `observations` is a new table, not a column.

## 5. Rule and skill text changes (proposed; the user's files, not edited by this spec)

Global `~/.claude/CLAUDE.md`, "STAGE EVERY EXPERIMENT WITH BATHOS". Replace "stating the hypothesis and the `[outcomes]` pass/fail criteria" with:

> stating either (a) **exploratory**: the question and what will be produced (`[observe]`), with no verdict thresholds (cite results as observations, `bth observe`); or (b) **confirmatory**: the hypothesis and `[outcomes]` criteria, each numeric cutoff carrying a `source` (literature, a calibration run, or `provisional:` with reason). A confirmatory test of something first *seen* in exploration must use data the exploration did not (`[holdout]`).

`using-bathos` skill: add a "Which mode am I in?" decision table (§3.1) at the top. Update `bth new-experiment`'s default template to the exploratory shape (§3.2): the current template *teaches* the arbitrary-threshold pattern.

## 6. Implementation slices (each independently shippable)

| # | Slice | Files | Size |
|---|---|---|---|
| S1 | Stage → class mapping; exploratory validator branch (`question`, `[observe]`, outcomes optional, `source` required if present); reserved `observed` label in runner | `sidecar.py`, `validate.py`, `runner.py`, tests | standard |
| S2 | Exploratory scaffold as `new-experiment` default; `--mode confirm` keeps today's template with `${THRESHOLD}` placeholders | `new_experiment.py`, `authoring/` | quick |
| S3 | `bth observe` + observations ledger + MCP tool | new `observations.py`, CLI, `mcp.py` | standard |
| S4 | `--from-observation` promotion + `[holdout]` block + lint against self-sourcing | `new_experiment.py`, `sidecar.py`, `linter.py` | standard |
| S5 | `source = "run:<id>:<field>"` resolution for calibration runs | `linter.py`, `validate.py` | quick |
| S6 | Gate wiring (Union Gate, confirmation-campaign add) + sprint-audit signal; fix #2080 in passing | `claim.py`, `campaigns.py`, `sprint_audit.py`, `mcp.py` | standard |
| S7 | Skill + rule card (`DSGN-*`: "exploration records observations, not verdicts") | `agent_assets/` | quick |

Recommended order: S1 + S2 removes the friction on day one. S3 + S4 add the follow-up path. S5 and S6 harden it.

## 7. Open questions (for the user)
1. Should exploratory `[outcomes]` be *forbidden* outright, or allowed only with `source`? This draft allows them with `source`. The stricter version is simpler to explain.
2. Should `bth observe` also accept observations not tied to a run (e.g. from a notebook or a paper figure), or must there always be a `run_id`?
3. Is `[holdout]` as an attestation (until #2686) acceptable, or should promotion block until input hashes exist?

## 8. Verification plan
- Unit tests: an exploratory sidecar without `[outcomes]` validates; with a bare-number outcome it fails; with `source` it passes. The whole existing validate suite must stay green.
- **Negative control:** a confirmatory sidecar that drops `[outcomes]` must still fail. This proves the exploratory branch didn't loosen the confirmatory path.
- Dogfood: re-author one existing exploratory sidecar from a sibling project (e.g. a demistify EDA) in the new shape, and confirm the catalog records `observed`, not a verdict.

