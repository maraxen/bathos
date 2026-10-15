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
| `exploration` | **exploratory** | `question` + `[observe]` (what will be produced) | allowed, **held lightly** (§3.2.1): every numeric cutoff needs `source` (`provisional:` is fine), and the label is recorded as tentative | an *observation* ("we saw…"), never evidence for a claim clause |
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

#### 3.2.1 Exploratory outcomes are allowed but held lightly (decision, user 261015)

Banning `[outcomes]` would throw away useful triage ("flag runs where the instrument looks broken", "sort these 40 builds into roughly three bins to decide which to look at"). The problem is not having labels. The problem is labels that *read as verdicts*. So:

- **Recorded as tentative, not verdicts.** An exploratory run's evaluated label is stored with its class (`stage_name='exploration'` is already on the row). Every reader that renders an outcome (`bth show/ls`, `cite_run`, viz, campaign report) displays it as `tentative:<label>`. `cite_run` on an exploratory run emits "observed (tentative label `pass`)", never "passed".
- **The scaffold and skill say so in words.** The exploratory template's outcome comment reads: *"Exploration runs under heavy epistemic and operational uncertainty: you don't yet know what the right quantity, scale or cutoff is, and the pipeline itself may still be wrong. Treat these labels as sorting hints for what to look at next, not results. Expect to change them, and change them freely; a changed exploratory outcome is not a deviation."* This matches the registered-reports position that exploratory results "should be considered tentative and in need of verification via further confirmatory research" (research doc Q1/Q6, NotebookLM-reported, primary not read).
- **Editing freely is the point.** Sidecar drift on an exploratory sidecar is advisory only, not a gate.
- **Asymmetric reclassification.** The converse is enforced: if a *confirmatory* sidecar's outcomes change after its first run, later runs under the drifted hash are recorded as exploratory unless the stem is renamed. This is the "a changed confirmatory plan becomes exploratory" rule (research Q1), reusing `check_sidecar_drift` (`prereg.py:181`).
- **Optional `planned_tests = <int>`** in `[observe]`: the "planned exploratory analyses" middle ground (research Q1). If declared, more evaluated labels than this are flagged in sprint-audit as unplanned multiplicity. If absent, nothing is flagged.
- **Not a loophole.** The COS rule "at least one confirmatory test per preregistration" applies to *confirmatory* registrations. An exploratory sidecar makes no confirmatory claim, so it is consistent with that rule, and it can't be cited as one (§3.6).

### 3.3 Noticing: an observation is a first-class record

The step where examining the data "leads to follow-up" gets its own verb:

```
bth observe <run_id> "MI hub at H71 in holo only; absent in 2 of 3 seeds"  [--figure mi_heatmap.png] [--tags hub]
```

- This appends to a ledger (`~/.bth/catalog/observations/<project>/obs_<id>.parquet`, written atomically like the cool tier). It is linked to the run and the output sha.
- It is citable as *"observed in run X"*. That is what the global rule needs in order to stop forbidding exploratory findings: an observation *is* tracked, and it is labelled as not-a-test.
- It exists as an MCP tool too (`observe`). Agents doing EDA call it instead of writing a verdict.
- Seed disagreement is recorded as a *description*, never as pass/fail. This is consistent with `md-replicas-are-samples.md`.
- **Append-only.** No update or delete; a correction is a new row with `supersedes = <obs_id>` (research Q5).
- `origin ∈ {human, agent}` is recorded, so sprint-audit can measure the share of agent-originated observations (research Q4).

#### 3.3.1 Run-less observations: allowed, through the existing citation surface, skeptical by default (decision, user 261015)

An observation can come from a paper figure, a notebook, a talk or a colleague, with no bathos run. It **reuses the shape bathos already has for citing prior results**, `[[review.literature]]` (`sidecar.py:106-115`: `ref`, `claim`, `bears_on`, `disposition`, `checked`). It does not invent a parallel citation format:

```
bth observe --ref doi:10.1101/2024.01.02.123456 --locator "Fig 3B" \
  --claim "H71 shows holo-only coupling" --value-provenance figure-read
```

The failure mode to design against is an agent treating every paper as true, never noticing an inflated claim, and copying an eyeballed number into a threshold. Hence:

- **Skepticism fields, added to both run-less observations and `LiteratureReview`:**
  - `locator`: page, figure or line. Required for run-less observations.
  - `quote`: optional verbatim text.
  - `value_provenance ∈ {published, figure-read, derived, agent-summary}`. This is debt #2345's field. `agent-summary` covers NotebookLM/LLM-relayed content, which is known to splice quotes (research caveats; #2345).
  - an `appraisal` (§3.3.2) with `read_depth`, arguments for/against on a basis ladder, and claimed-vs-supported scope. This replaces the earlier single `verification` field. Separately, `quote_checked = true` is set only by #2345's checker, when the quote matches the extracted text at its locator.
- **A paper is one draw, too.** A published effect size has the same winner's-curse inflation as an exploratory one (research Q3). So a literature observation is a *hypothesis source* by default. `source = "lit:<ref>"` on a confirmatory cutoff requires derived `use_tier = threshold-source` (§3.3.2), which `figure-read` and `agent-summary` values can never reach.
- **Contradiction is first-class.** `disposition = contradicts` is encouraged. Concerns are not a free-text field with a "none found" escape. Every cited claim carries a structured **appraisal** with arguments *for* and *against* trusting it (§3.3.2).
#### 3.3.2 The appraisal payload (decision, user 261015: never "no concerns"; split into arguments for and against)

Grounding: `research/261015_claim-appraisal-nlm-research.md` (notebook `1d786e78`). It supplies four design rules, each NotebookLM-reported and not yet read in primary sources:
- (R1) **Keep dimensions separate, never one score.** "Never emit a monolithic 'this looks right' or 'LGTM' judgment"; "aggregate scores alone cannot characterize the reliability of a comparison".
- (R2) **Non-compensatory.** "when any of these elementary tests fails, the comparison is fragile": one decisive objection is not outweighed by many positives.
- (R3) **Disclosed ≠ checked.** "Artifact evaluation checks whether code runs, not whether it produced the reported numbers"; a checklist "verifies intention, not execution".
- (R4) **No self-grading**, and record the theory-load of interpretations.

Not in that corpus: GRADE domains, Cochrane RoB, Toulmin, and adversarial or red-team appraisal as a role. The categories below are therefore our synthesis of the ML-rigor taxonomies (Pineau, Lipton & Steinhardt, Vaccaro, Oh, Bouthillier) plus MD-specific items. They are not a published instrument.

**Where it lives:** one schema in `sidecar.py`, shared by `[[review.literature]]` entries (an `appraisal` sub-table) and by run-less observations in the ledger (§3.3.1). TOML in sidecars; the same object as JSON in the ledger and the MCP `observe` payload.

```toml
[[review.literature]]
ref = "doi:10.1101/2024.01.02.123456"
locator = "Fig 3B; Results ¶4"
claim = "H71–E40 MI is elevated only in the holo state"          # what the SOURCE asserts, in its words
quote = "…coupling between H71 and E40 emerges exclusively upon ligand binding…"   # optional
value_provenance = "figure-read"          # published | figure-read | derived | agent-summary
disposition = "supports"                  # supports | contradicts | scope-differs   (existing)
bears_on = "H1"                           # existing

[review.literature.appraisal]
appraiser = "agent:claude-opus-5-5"       # agent:<model> | human:<handle>
appraised = "2026-10-15"
read_depth = "methods"                    # abstract | full-text | methods | supplement | data-or-code
uses_as = "hypothesis-source"             # hypothesis-source | threshold-source | contrast-context
# R6/P6: what the source CLAIMS vs what its evidence SUPPORTS. Required, and never left equal by default.
scope_claimed = "PDZ3 allostery generally"
scope_supported = "PDZ3 (1BE9), one ligand, 3×100 ns, one force field"
# R4: where the appraiser had to interpret (figure reading, ambiguous methods). Required if value_provenance = figure-read.
interpretation = "Read 'exclusively' off a heatmap with no colour-bar values; apo cell is pale, not zero."

[[review.literature.appraisal.for]]
id = "f1"
category = "multi-seed-uncertainty"
statement = "Three independent replicas per state, with bootstrap CIs shown in Fig S4."
basis = "inspected"                       # asserted | inspected | executed | reproduced | independent
evidence = "SI p.6, Fig S4"

[[review.literature.appraisal.against]]
id = "a1"
category = "unconverged-sampling"
statement = "100 ns per replica; the paper's own block analysis (Fig S5) has not plateaued."
basis = "inspected"
severity = "major"                        # decisive | major | minor
evidence = "SI Fig S5"

[[review.literature.appraisal.against]]
id = "a2"
category = "single-source"
statement = "No independent replication found; the only other report is from the same group."
basis = "inspected"
severity = "minor"
evidence = "search: 'PDZ3 H71 holo coupling', 2026-10-15, 0 independent hits"
addressed_by = []                          # ids of `for` entries that rebut this, each with its own basis
```

**Field rules (validator, enforced):**
1. `for` and `against` must **both be non-empty**. There is no sentinel: a statement matching `none|n/?a|no (concerns|issues)|nothing` (case-insensitive) or shorter than 30 characters is rejected. If the appraiser can't find a specific objection, they must still name the strongest *generic* one from the closed list (e.g. `single-source`, `not-reproduced`) with `basis = "asserted"`. Lack of scrutiny then shows up as weak basis instead of reading as clean.
2. **Basis ladder (R3).** `asserted` means the source or the appraiser says so and nothing was checked. `inspected` means the appraiser read the relevant methods, data or code. `executed` means they ran it. `reproduced` means they obtained the reported number. `independent` means a third-party reproduction, which is itself a citation that needs its own appraisal. A `for` entry of category `artifact-available` with basis `asserted` is legal but contributes nothing to the use tier.
3. **No combined score field exists (R1).** There is deliberately no `confidence` and no `trust = 0.7`. The only aggregate is the derived `use_tier` below, and it is rule-based and non-compensatory.
4. `severity` is required on `against`, and absent on `for`, since strength there is carried by `basis`. An `against` entry counts as addressed only if every id in `addressed_by` points to a `for` entry whose basis is ≥ `inspected`.
5. `scope_supported` must not be byte-equal to `scope_claimed` unless `read_depth ≥ methods`. Otherwise "it says what it says" wins by default.
6. Closed category lists; `other` is allowed with a mandatory `other_label`. Recurring `other_label`s are evidence for extending the list, the same posture as HYG-002 for stage names.

**Category lists (v1):**
- `against`: `small-n`, `sampling-variance` (seeds/replicas), `unconverged-sampling`, `no-uncertainty-reported`, `weak-or-untuned-baseline`, `missing-control`, `missing-ablation`, `leakage-or-circularity`, `researcher-degrees-of-freedom` (selective reporting, post-hoc analysis choices, HARKing), `measurement-validity` (metric doesn't measure the claim), `outlier-dominance` (a few cases carry the result), `scope-overreach` (the claim-evidence gap), `not-reproduced`, `artifact-unavailable`, `single-source`, `conflict-of-interest`, `other`.
- `for`: `preregistered`, `independent-replication`, `multi-seed-uncertainty`, `effect-size-uncertainty`, `controls-present` (positive/negative/placebo), `external-validation` (new system, data or conditions), `artifact-available`, `convergent-methods` (different method, same answer), `mechanistic-consistency`, `other`.
- What was checked lives in `basis`, not in the category. So there is no separate "code-ran" category: it is `artifact-available` + `basis = "executed"`.

**Derived `use_tier`** (computed, never declared, same posture as the review tier in `sidecar.py:129`):

| tier | requires |
|---|---|
| `refuted-for-use` | any unaddressed `against` with `severity = decisive` |
| `contested` | any unaddressed `against` with `severity = major` |
| `hypothesis-source` | otherwise. This is the default ceiling for any appraisal. |
| `threshold-source` | `hypothesis-source`, plus `read_depth ≥ methods`, plus at least one `for` with basis ≥ `inspected`, plus `value_provenance ∈ {published, derived}`, plus an **appraiser independence** condition (R4): the appraiser is not the same agent session that is citing the value as a threshold source, *or* a second appraisal by a different appraiser exists with use tier ≥ `hypothesis-source` |

`uses_as` states what the author *wants*. `use_tier` states what the appraisal *licenses*. Validation fails when `uses_as` exceeds `use_tier`, e.g. `uses_as = "threshold-source"` with tier `contested`.

**Skill guidance (not enforceable, stated anyway):** write the `against` entries *before* the `for` entries. This is the blind-then-reveal idea from R4's corpus, applied against sycophantic, confirmation-seeking appraisal, the failure mode the user named.

- **Promotion from a run-less observation** (§3.4) works the same way. The holdout is trivially "not the paper's data", but the scaffold additionally asks whether the bathos test measures *the same quantity* the paper did. This hooks into estimand discipline (#2677).

### 3.4 Promotion: observation → confirmatory test

```
bth new-experiment confirm_h71_hub --from-observation <obs_id>
```

This scaffolds a **validation-stage** sidecar pre-filled with:
- `hypothesis` drafted from the observation text, for the user to edit.
- `[provenance] derived_from_observation = "<obs_id>"`, which records that the hypothesis was data-generated.
- **`[holdout]` (required when `derived_from_observation` is set):** what makes this test independent of the data that suggested it. Kinds are **ranked**, and the rank bounds what the promoted claim may assert:

  | rank | kind | independent of | bounds the claim to |
  |---|---|---|---|
  | 0 | `new_seeds` (same build) | nothing above the frame/seed level | sampling repeatability of *this build*. **Not valid** for a promotion unless the claim is about seed stability. In MD, seeds are draws from one distribution (`md-replicas-are-samples.md`); in ML, the corpus reports new seeds as "merely a check on repeatability" (research Q2) |
  | 1 | `new_builds` | initial condition / placement | this system under this protocol |
  | 2 | `held_out_split` | the observed subset | the population the split was drawn from. **Must be declared before the observation**: the split manifest's sha is recorded with an earlier timestamp than `obs.created_at`, otherwise it is rank 0 (research Q2: discovery→holdout leak) |
  | 3 | `new_system` | the system itself | the class of systems sampled |
  | 3 | `literature_prediction` | our data entirely | as stated in the prediction, subject to §3.3.1 verification |

  The ranking adapts NotebookLM's ML-literature synthesis (seeds < IID samples < new systems) to the nested MD structure (frames ⊂ seeds ⊂ build ⊂ condition). The order is our judgement, not a sourced result.
- **Attested vs. checked (decision, user 261015: attestation acceptable).** Until #2686 adds input-artifact anchors, `[holdout]` is an attestation. Lint warns when the confirmatory command's argv matches the source run's argv. Attested holdouts are recorded as `holdout_verified = false`, and a claim resting only on attested holdouts is labelled so in `cite_run` and the campaign report. When hash checking ships, it upgrades existing records retroactively where input hashes allow.
- **Thresholds are not scaffolded as numbers.** Each `[outcomes]` condition is emitted as `metric <op> ${THRESHOLD}` with a `source` that must be filled. The scaffold suggests, in order: (a) `sesoi:<why>`, the smallest effect size of interest, i.e. the effect that would matter for the decision, stated before the test (research Q3: SESOI/MMES; equivalence-test bounds come from it). (b) A calibration run (noise floor / spike-in MDE, see §3.5); a calibration MDE larger than the SESOI means the design is underpowered by construction and lint says so. (c) Verified literature (§3.3.1). (d) `provisional:` with a reason. A sidecar with an unfilled `${…}` fails validation. That is the point: the number has to be chosen consciously, and its basis is recorded.
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
| S3 | `bth observe` (run-backed + run-less) + append-only observations ledger + MCP tool; skepticism fields on `LiteratureReview` | new `observations.py`, `sidecar.py`, CLI, `mcp.py` | standard |
| S3b | `tentative:` rendering in every outcome reader; asymmetric confirmatory→exploratory reclassification on drift | `query.py`, `rich_fmt.py`, `cite.py`, `viz/`, `prereg.py` | standard |
| S4 | `--from-observation` promotion + `[holdout]` block + lint against self-sourcing | `new_experiment.py`, `sidecar.py`, `linter.py` | standard |
| S5 | `source = "run:<id>:<field>"` resolution for calibration runs | `linter.py`, `validate.py` | quick |
| S6 | Gate wiring (Union Gate, confirmation-campaign add) + sprint-audit signal; fix #2080 in passing | `claim.py`, `campaigns.py`, `sprint_audit.py`, `mcp.py` | standard |
| S7 | Skill + rule card (`DSGN-*`: "exploration records observations, not verdicts") | `agent_assets/` | quick |

Recommended order: S1 + S2 removes the friction on day one. S3 + S4 add the follow-up path. S5 and S6 harden it.

## 7. Decisions (user, 261015) and remaining questions

Resolved:
1. **Exploratory `[outcomes]`: allowed, not banned**, and taken lightly, because exploration runs under heavy epistemic and operational uncertainty. See §3.2.1.
2. **Run-less observations: allowed**, through the existing citation surface and skeptical by default. Accepting claims blindly is a known agent failure mode. See §3.3.1.
3. **`[holdout]` as attestation: acceptable** (no strong preference). Attested holdouts are labelled `holdout_verified = false` and upgraded when #2686 lands. See §3.4.

4. **Resolved (user, 261015):** no "no concerns", ever. Appraisals carry structured for/against arguments with a basis ladder and a derived, non-compensatory use tier (§3.3.2).
5. **Done:** notebook `1d786e78` was cross-checked, targeted at the appraisal payload. See `research/261015_claim-appraisal-nlm-research.md`.

Still open, for adversarial review:
6. **Rank-0 (`new_seeds`) holdouts.** Recommendation (pending user): allow them only as a **kill test**. A seed-only follow-up may record `refuted` or `survived-seed-check` on the *observation*, but it cannot promote the observation to a confirmatory claim; promotion requires rank ≥ 1. Rationale: new seeds can falsify cheaply but cannot independently confirm (same build, same initial-condition confound).
7. Is the v1 category list right for your domains (MD, protein design, ML ports)? It is our synthesis, not a published instrument.
8. Does the appraiser-independence condition for `threshold-source` cost too much when you work alone? A same-session agent appraisal can never self-license; a second appraisal by a different agent or a human is required.

## 9. Research grounding

[`research/261015_exploratory-mode-nlm-research.md`](../research/261015_exploratory-mode-nlm-research.md). All seven questions went to NotebookLM notebook `3f0490aa`. Quotes are verified only as *present in NotebookLM's answers*, and no primary source has been read, so nothing there is cited as a finding. Numbers flagged in that doc's caveats (e.g. "22–29% seed false-positive rate") must not be copied into this spec or any sidecar.

## 8. Verification plan
- Unit tests: an exploratory sidecar without `[outcomes]` validates; with a bare-number outcome it fails; with `source` it passes. The whole existing validate suite must stay green.
- **Negative control:** a confirmatory sidecar that drops `[outcomes]` must still fail. This proves the exploratory branch didn't loosen the confirmatory path.
- Dogfood: re-author one existing exploratory sidecar from a sibling project (e.g. a demistify EDA) in the new shape, and confirm the catalog records `observed`, not a verdict.

