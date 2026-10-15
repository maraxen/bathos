---
title: 'Exploratory-mode contract: pre-register the question, not a verdict'
description: Operationalize exploration vs calibration vs validation in bathos so exploratory runs stop inventing pass/marginal/fail thresholds, with a gated promotion path to confirmatory
status: draft
task_id: 261015_exploratory-mode-contract
date: '261015'
backlog_ids: ''
adversarial_review: audits/261015_exploratory-mode-contract-adversarial-review.md
---
# Exploratory-mode contract: pre-register the question, not a verdict

**Status:** revised after a sequential adversarial review (challenger, then defender; dispositions in §10) · **task_id:** 261015_exploratory-mode-contract

## 1. Problem

bathos only offers two ways to run analytical work, and both are wrong for exploration:

| Option | What happens | Why it fails exploration |
|---|---|---|
| `scripts/explore/` or `--no-sidecar` | untracked | The global rule ("any run that will produce a finding gets a sidecar") forbids citing anything from it. The first interesting observation then can't be used. |
| `scripts/experiments/` + sidecar | `[outcomes]` with `condition` + `decision` + `reasoning` + an `is_residual` branch is **mandatory** | You must declare pass/marginal/fail cutoffs before you know what you're looking for. The cutoffs are arbitrary, and they then pose as findings (`outcome='pass'` in the catalog). |

Nothing in between says: *"I am looking at X. I will produce plots/tables Y. I am not testing anything yet."*

The arbitrary thresholds are not a minor annoyance. They actively corrupt the record:
1. A made-up `pass` sits in the catalog alongside real pre-registered verdicts. Readers, and later agents, can't tell them apart. Neither can machine gates: `requires_pass_stem` counts any `outcome='pass'` (`prereg.py:318`).
2. Because the label is enforced, agents spend effort tuning cutoffs instead of looking at the data. This is the pattern in memory `feedback_threshold_cutoffs` (it has already reshaped project direction once).
3. When the visualization then shows something unexpected, there is no first-class path from "noticed" to "test it". So the follow-up either reuses the same data (circular) or is never written down.

## 2. What exists today (grounded), and the assumption ledger

Facts were read at file:line on main @ 2161670a. The challenger and defender re-read them independently during review; the orchestrator re-checked the blocker ones.

| Fact / assumption | Where | Tag |
|---|---|---|
| `[experiment].stage_name` ∈ {exploration, calibration, validation, ablation, production} | `sidecar.py:215,273` | verified |
| **Absent `stage_name` silently defaults to `exploration`, and so does the catalog row** | `sidecar.py:311`, `catalog.py:176` | verified. It falsified the draft's "stage_name already defaults to the right value" (C1) |
| **A non-canonical `stage_name` is coerced to `exploration`, with only a log warning** | `sidecar.py:312-317` (the authoring write path already hard-errors: `authoring/models.py:36-41`) | verified. It falsified the draft's fail-safe assumption (C2) |
| Sibling projects hold confirmatory sidecars with no `stage_name` (`ksweep-bench/denxity`: 18–19 `[experiment]` sidecars, mostly `*_confirmatory`, zero `source` lines) | grep | verified. A migration is required (C1) |
| `[outcomes]` is required for every experiment sidecar regardless of stage | `validate.py:578-588` | verified |
| Each branch must have condition, decision and reasoning, plus one `is_residual` branch, and some outcome must reference a `result_schema` field. `hypothesis` content itself is not validated | `validate.py:485-731` | verified |
| `bth new-experiment` scaffolds `stage_name="exploration"` **together with** pass/marginal/fail at `metric < 5.0 / 10.0` | `new_experiment.py:51-79` | verified (this template is the friction, verbatim) |
| `stage_name` changes behaviour in the `requires_pass_stem` gate (`cli_cyclopts.py:752-763`), the parity gate (`parity.py:249-252`), a Tier-1 lint (`linter.py:233`) and sprint-audit (`sprint_audit.py:166,262`). `ablation` falls through both gates today | grep | verified (draft said "only two places": wrong, C22) |
| **`requires_pass_stem` matches `outcome = 'pass'` with no stage filter**, and is a hard gate for validation | `prereg.py:318`, `cli_cyclopts.py:754` | verified. Render-only "tentative" would leak (C4) |
| **An outcome-less sidecar has an empty pass set, so any label counts as failure.** That opens an `outcome_failed` obligation and scores as fail evidence in e-values | `sidecar.py:584-602,618,636`, `runner.py:1128`, `campaigns.py:421` | verified (C5) |
| Autonomous mode already denies runs on sidecar drift; drift is a whole-file sha keyed by script path | `prereg.py:181-233,480-496` | verified (C6) |
| Union Gate coverage reads `claim_discriminates` and ignores class and outcome | `claim.py:908-931` | verified (C14) |
| Every run records `script_sha256` | `schema.py:55` | verified (used for the laundering tripwire, C3) |
| `get_run(..., allow_cool_fallback=True)` exists, so a run is visible before `bth compact` | `query.py:345` (PR #92) | verified (rebuts C17's first half) |
| All write sites branch through `emit_or_legacy` | `runlog/emit.py:172` | verified |
| Campaign `mode` ∈ {exploration, confirmation, sequential}. Exploration only softens gates at **conclude** (warning, no downgrade) | `campaigns.py:29,1090,1171,1214,1322` | verified |
| MCP `campaign_create` accepts only exploration/confirmation | `mcp.py:1990` (= debt #2080) | verified |
| `OutcomeSpec.source` (one per branch) and Tier-2 lint `check_threshold_basis` shipped (item #760); the lint is warning-only. `source` is free text today | `sidecar.py:37`, `linter.py:870,907` | verified |
| Reserved `invalid_measurement` label; derived review tier | `sidecar.py:268`; `review_tier` at `sidecar.py:189` | verified (draft cited :285 / :129, which was wrong) |
| `postmortem` already has an `unexpected_observations` field | `postmortem.py:35` | verified. Related, not merged (§3.3) |
| Runs carry `derived_from` (sweep parent), but nothing models "this hypothesis came from that observation" | `mcp.py:1782` | verified |
| Runs record no input-data hashes, so "confirm on data you didn't explore" can't be checked mechanically yet | debt #2683, #2686 | unverified for confirm-path use. Treat it as **attested, not enforced** until #2686 lands |
| **Subprocesses see `CLAUDE_CODE_SESSION_ID`; a subagent inherits its parent's id** | spike 261015: the top-level Bash and a Haiku subagent both printed `fcb7b7e4-…` | verified. The id separates top-level sessions, **not** subagents within one (§3.3.2) |
| NotebookLM-relayed research claims | `research/261015_*` | unverified. Present in NotebookLM's answers only; no primary source read |

So there are three vocabularies: run `stage_name`, campaign `mode`, and the directory taxonomy. They overlap, none of them changes what a run must pre-register, and two of them disagree with each other (#2195, #2080).

### Related open debt (this spec subsumes or touches these; no existing item covers the core issue)
- **#2195**: `stage` vs `stage_name`; "confirmation is a campaign mode not a run stage". This spec resolves it by mapping stage → epistemic class (§3.1).
- **#2080**: MCP rejects `mode='sequential'`. Fold this in as a trivial fix.
- **#2224**: outcome-partition gaps turn a gate into a human override. Exploratory runs stop having binding partitions, which removes most of the surface.
- **#2343 / #2241 / #2243**: `outcome='unknown'` is ambiguous. This spec adds a reserved `observed` label so "intentionally not a verdict" can't be confused with "evaluation broke".
- **#2345**: quote/value provenance checking (§3.3.1).
- **#2677**: estimand discipline. This is the confirmatory-side complement. Out of scope here, but the promotion scaffold (§3.4) is where an estimand, the contrast unit and `n_units` would be declared.
- **#2743**: per-domain plugin interface for appraisal categories (deferred, §7.7).

## 3. Design

### 3.1 One axis: epistemic class, resolved from an explicit `stage_name` and stored on the run

The class is a **run-time column**, `epistemic_class`, written once when the run starts and never recomputed from the sidecar afterwards. This replaces the draft's "derived on read, no new column": that design could not tell legacy rows from real exploration (`catalog.py:176`), and it let an edited `stage_name` retroactively reclassify past runs (C1, C4, C6, C15).

Resolution at run start, in order:

| sidecar | `epistemic_class` | rules applied |
|---|---|---|
| `stage_name` key **absent** | `legacy` | today's rules and today's rendering, unchanged. A Tier-2 lint suggests restaging: "declare stage_name explicitly". Rows written before S1 (column NULL) read as `legacy` |
| `stage_name` **not canonical** (typo) | `confirmatory` | fail closed, plus a lint **error** naming the canonical set. A typo can never loosen the rules |
| `exploration` **with an `[observe]` block** | `exploratory` | §3.2 |
| `calibration` **with a `[calibrate]` block** (`quantity = "…"`) | `calibrating` | §3.5 |
| `exploration` / `calibration` **without** its block, on a stem that already has runs | `legacy` | today's rules; Tier-2 lint "restage: add `[observe]`/`[calibrate]` or choose a confirmatory stage" |
| the same, on a **new** stem | — | validation **error** with the same message |
| `validation` / `ablation` / `production` | `confirmatory` | today's rules, unchanged. The class does not alter which stages the existing `requires_pass_stem`/parity gates name. Ablation stays outside them as today |
| a confirmatory stem whose canonical block drifted (§3.2.1) | `exploratory` | the declared `stage_name` is left as written; only the class is downgraded |

**Shape gating (decision, user 261015).** 44 sidecars across 11 sibling projects declare `stage_name = "exploration"` explicitly, because the old scaffold wrote it, and they carry real pass/fail `[outcomes]`; 22 more declare `calibration`. Honouring the declared stage would demote all of them at once. So the new class takes effect only when the sidecar has the new contract's shape. New work can't reproduce the old pattern, because a new stem without the block fails validation.

| class | Pre-register | `[outcomes]` | Stored `outcome` | Citable as |
|---|---|---|---|---|
| **exploratory** | `question` + `[observe]` | optional, **held lightly** (§3.2.1): every non-residual branch needs `source` | always `observed`. The evaluated label goes to `tentative_outcome` | an *observation* ("we saw…"), never evidence for a claim clause |
| **calibrating** | the quantity being calibrated (noise floor, MDE, null distribution) | optional, no verdict needed | `observed` (label, if any, in `tentative_outcome`) | a threshold *source* for later runs (`run:<id>:<field>`) |
| **confirmatory** / **legacy** | hypothesis + full `[outcomes]` as today | required (today's rules) | the evaluated label, as today | evidence |

Campaign `mode` reads the class:
- `confirmation` holds only confirmatory/legacy runs, plus the calibration runs it names as threshold sources.
- `sequential` holds only confirmatory/legacy runs. Exploratory and calibrating runs are refused, so they can never feed an e-value.
- `exploration` holds anything.

This answers #2195: confirmation is a property of the campaign, and the run's stored class is what the campaign checks.

### 3.2 The exploratory sidecar

```toml
[experiment]
stage_name = "exploration"
question = "How does per-residue MI distribute across the PDZ β2–β3 loop in apo vs holo?"

[observe]                       # what you commit to producing and looking at; NOT a test
outputs = ["mi_heatmap.png", "per_residue_mi.csv"]
looking_for = "any structure: hubs, apo/holo asymmetry, seed-dependent artefacts"   # free text, optional
sanity = ["n_frames_pooled > 0", "mi_diag_is_entropy"]    # instrument checks, not cutoffs
planned_tests = 2               # optional, see §3.2.1

[result_schema]                 # still recommended; enables later queries
n_frames_pooled = "int"
mi_diag_is_entropy = "bool"
top_hub = "str"
```

Validator rules (`validate.py`), when the class is exploratory:
- `[outcomes]` absent → **OK**. `question` is required; `hypothesis` is accepted as an alias. This is a new exploratory-only rule: confirmatory `hypothesis` content stays unvalidated, as today.
- `[observe].outputs` is required and non-empty. This keeps the pre-registration meaningful: you said what you'd look at *before* looking. It is also what lets #2343's missing-`--output-paths` warning fire correctly.
- `sanity` entries are DuckDB booleans over `result_schema` fields. Literals are limited to 0/1, `NULL` and booleans, and **no arithmetic on literals** is allowed. This is checked on DuckDB's parsed expression, not by regex, so `x > 1+1+1` is rejected. They guard the instrument (cf. BATHOS.md positive/negative controls), not the hypothesis.
- `[outcomes]` present → **every non-residual branch needs a `source`, whether or not its condition has a numeric literal.** This closes the dodge of moving the cutoff into the script as a boolean result field (`condition = "is_hub"`). It promotes `check_threshold_basis` (`linter.py:907`, per-branch) from a Tier-2 warning to a validation **error** for the exploratory class only. Confirmatory runs keep the warning.
- **`source` grammar (closed, for every class):** `lit:<ref>` | `run:<id>:<field>` | `sesoi:<value>:<why>` | `provisional:<why>` | `card:<id>`. A malformed value is an error in the exploratory class and a warning in the others.
- **Migration posture (C1):** for an exploratory stem that already has runs in the catalog, the source rule is a **warning** in the first release. It is an error for new stems from day one.

**Runtime precedence** (it slots into the existing order at `runner.py:855-1037`):

| step | condition | stored result |
|---|---|---|
| 1 | differential short-circuit (existing) | unchanged |
| 2 | non-zero exit (existing exit-code guard) | unchanged (`error`) |
| 3 | any `sanity` entry is false, NULL, or references a missing field | `outcome='invalid_measurement'`, reason `sanity:<expr>` in metadata. `differential_status` is left untouched |
| 4 | a declared `[observe].outputs` file is missing (matched by basename under `BTH_OUTPUT_DIR`) | **warning** only, recorded in metadata |
| 5 | exploratory/calibrating class | `outcome='observed'`. If `[outcomes]` exist, evaluate them and store the label in `tentative_outcome` |
| 6 | confirmatory/legacy class | evaluate as today |

`invalid_measurement` keeps its existing meaning ("the instrument didn't measure"). A failed sanity check is exactly that case, so the reuse is consistent with `sidecar.py:264-268`.

**`observed` is neutral everywhere.** It joins `RESERVED_OUTCOME_LABELS` and is treated like `''`/`unknown` in `is_failure_outcome`, in `compute_evalue` (`sidecar.py:618`), in the sequential neutral list (`campaigns.py:421`) and at `runner.py:1049`. No `outcome_failed` obligation is ever opened for an exploratory or calibrating run, whatever its tentative label (C5).

#### 3.2.1 Exploratory outcomes are allowed but held lightly (decision, user 261015)

Banning `[outcomes]` would throw away useful triage ("flag runs where the instrument looks broken", "sort these 40 builds into roughly three bins to decide which to look at"). The problem is not having labels. The problem is labels that *read as verdicts*. So:

- **Stored as tentative, not merely rendered (C4).** The `outcome` column holds `observed`, and the evaluated label lives in `tentative_outcome`. Every existing `outcome='pass'` consumer (`requires_pass_stem`, parity, e-values, ad-hoc `bth sql`) is therefore safe by construction, with no consumer list to keep in sync. Readers that render outcomes (`bth show/ls`, `cite_run`, viz, campaign report) show `observed (tentative: pass)`. `cite_run` on an exploratory run never says "passed".
- **The scaffold and skill say so in words.** The exploratory template's outcome comment reads: *"Exploration runs under heavy epistemic and operational uncertainty: you don't yet know what the right quantity, scale or cutoff is, and the pipeline itself may still be wrong. Treat these labels as sorting hints for what to look at next, not results. Expect to change them, and change them freely; a changed exploratory outcome is not a deviation."* This matches the registered-reports position that exploratory results "should be considered tentative and in need of verification via further confirmatory research" (research doc Q1/Q6, NotebookLM-reported, primary not read).
- **Editing freely is the point.** Sidecar drift on an exploratory sidecar is advisory only, not a gate.
- **Asymmetric reclassification (C6).** The converse is enforced: a confirmatory stem is downgraded when its **canonical block** drifts after its first run. The canonical block is the canonicalized `stage_name` + `[outcomes]` + `[result_schema]`, so comments, `[review]` edits and appraisal ids don't count. Later runs under the drifted block get `epistemic_class='exploratory'` and store `observed`; the declared `stage_name` is not rewritten. Details:
  - **The strictest class ever declared on a stem governs.** Changing `validation` to `exploration` after a run does not soften that stem's drift check.
  - **The autonomous-mode drift deny (`prereg.py:480-496`) stays as is.** Reclassification is the collaborative-mode behaviour; autonomous mode still refuses.
  - **Renaming the stem is the legitimate escape**, and it is caught by the laundering tripwire (§3.4) when nothing else changed.
- **Optional `planned_tests = <int>`** in `[observe]`: the "planned exploratory analyses" middle ground (research Q1). The counting unit is the number of **distinct non-residual branch conditions declared across all revisions of the stem's sidecar** (C20). Exceeding it is flagged in sprint-audit as unplanned multiplicity. If absent, nothing is flagged.
- **Not a loophole.** The COS rule "at least one confirmatory test per preregistration" applies to *confirmatory* registrations. An exploratory sidecar makes no confirmatory claim, so it is consistent with that rule, and it can't be cited as one (§3.6).

### 3.3 Noticing: an observation is a first-class record

The step where examining the data "leads to follow-up" gets its own verb:

```
bth observe <run_id> "MI hub at H71 in holo only; absent in 2 of 3 seeds"  [--figure mi_heatmap.png] [--tags hub]
```

- **Storage:** an `observation.created` event written through `emit_or_legacy` (`runlog/emit.py:172`), like every other write site. The legacy write is an atomic Parquet fragment under `~/.bth/catalog/observations/<project>/obs_<id>.parquet`, compacted into a warm `observations` table. Each record links to the run and the output sha.
- **Run lookup** uses `get_run(..., allow_cool_fallback=True)` (`query.py:345`), so a run can be observed before `bth compact` runs.
- **Citation:** an observation is citable as *"observed in run X"*. That is what the global rule needs in order to stop forbidding exploratory findings: an observation *is* tracked, and it is labelled as not-a-test.
- **MCP:** it exists as an MCP tool too (`observe`). Agents doing EDA call it instead of writing a verdict.
- **Seeds:** seed disagreement is recorded as a *description*, never as pass/fail. This is consistent with `md-replicas-are-samples.md`.
- **Append-only.** No update or delete; a correction is a new row with `supersedes = <obs_id>` (research Q5).
- **`status` ∈ {open, refuted, survived-seed-check, promoted}** is a separate field carried on a status row (`observation.status` event, `of = <obs_id>`). A correction (`supersedes`) and a status change are different event kinds, so "this text was wrong" is never conflated with "this was tested" (C11).
- **`origin` ∈ {human, agent}** and `agent_session` are recorded, so sprint-audit can measure the share of agent-originated observations (research Q4).
- **Relation to `postmortem.unexpected_observations`** (`postmortem.py:35`): a post-mortem may reference obs ids. The two are not merged in this spec.

#### 3.3.1 Run-less observations: allowed, through the existing citation surface, skeptical by default (decision, user 261015)

An observation can come from a paper figure, a notebook, a talk or a colleague, with no bathos run. It **reuses the shape bathos already has for citing prior results**, `[[review.literature]]` (`sidecar.py:106-115`: `ref`, `claim`, `bears_on`, `disposition`, `checked`). It does not invent a parallel citation format:

```
bth observe --ref doi:10.1101/2024.01.02.123456 --locator "Fig 3B" \
  --claim "H71 shows holo-only coupling" --value-provenance figure-read \
  --appraisal appr_7f3c          # required: see below
```

The failure mode to design against is an agent treating every paper as true, never noticing an inflated claim, and copying an eyeballed number into a threshold. Hence:

- **Skepticism fields, added to both run-less observations and `LiteratureReview`:**
  - `locator`: page, figure or line. Required for run-less observations.
  - `quote`: optional verbatim text.
  - `value_provenance ∈ {published, figure-read, derived, agent-summary}`. This is debt #2345's field. `agent-summary` covers NotebookLM/LLM-relayed content, which is known to splice quotes (research caveats; #2345).
  - `quote_checked = true` is set only by #2345's checker, when the quote matches the extracted text at its locator.
- **A run-less observation requires an appraisal (C25).** `bth observe --ref` without `--appraisal <id>` records the observation with `status = open` and `unappraised = true`. Such an observation cannot be promoted (§3.4) and cannot be a `source`. There is no path where a citation is simply accepted.
- **A paper is one draw, too.** A published effect size has the same winner's-curse inflation as an exploratory one (research Q3). So a literature observation is a *hypothesis source* by default. `source = "lit:<ref>"` on a confirmatory cutoff requires derived `use_tier = threshold-source` (§3.3.2), which `figure-read` and `agent-summary` values can never reach.
- **Contradiction is first-class.** `disposition = contradicts` is encouraged. Concerns are not a free-text field with a "none found" escape. Every cited claim carries structured appraisals with arguments *for* and *against* trusting it (§3.3.2).

#### 3.3.2 The appraisal payload (decision, user 261015: never "no concerns"; split into arguments for and against)

Grounding: `research/261015_claim-appraisal-nlm-research.md` (notebook `1d786e78`). It supplies four design rules, each NotebookLM-reported and not yet read in primary sources:
- (R1) **Keep dimensions separate, never one score.** "Never emit a monolithic 'this looks right' or 'LGTM' judgment"; "aggregate scores alone cannot characterize the reliability of a comparison".
- (R2) **Non-compensatory.** "when any of these elementary tests fails, the comparison is fragile": one decisive objection is not outweighed by many positives.
- (R3) **Disclosed ≠ checked.** "Artifact evaluation checks whether code runs, not whether it produced the reported numbers"; a checklist "verifies intention, not execution".
- (R4) **No self-grading**, and record the theory-load of interpretations.

Not in that corpus: GRADE domains, Cochrane RoB, Toulmin, and adversarial or red-team appraisal as a role. The categories below are therefore our synthesis of the ML-rigor taxonomies (Pineau, Lipton & Steinhardt, Vaccaro, Oh, Bouthillier) plus MD-specific items. They are not a published instrument.

**Where it lives (C7):** appraisals are **ledger records, written only through `bth appraise`** (CLI and MCP; `appraisal.created` events through `emit_or_legacy`). The draft's sidecar sub-table could not be stamped with a session, could not hold a second appraisal, and its edits drifted the sidecar. `bth appraise` stamps:
- `appraisal_id`;
- `appraiser_session` (= `CLAUDE_CODE_SESSION_ID`, the MCP client id, or `human:<handle>`);
- the timestamp;
- the sha of the payload.

Sidecars and observations reference appraisals by id. One schema lives in `sidecar.py`, so the same object serves the TOML input file, the JSON ledger row and the MCP payload:

```toml
# sidecar
[[review.literature]]
ref = "doi:10.1101/2024.01.02.123456"
locator = "Fig 3B; Results ¶4"
claim = "H71–E40 MI is elevated only in the holo state"          # what the SOURCE asserts, in its words
quote = "…coupling between H71 and E40 emerges exclusively upon ligand binding…"   # optional
value_provenance = "figure-read"          # published | figure-read | derived | agent-summary
disposition = "supports"                  # supports | contradicts | scope-differs   (existing)
bears_on = "H1"                           # existing
uses_as = "hypothesis-source"             # contrast-context | hypothesis-source | threshold-source
appraisal_ids = ["appr_7f3c"]             # ledger records; ≥ 1 required
```

```toml
# bth appraise --ref doi:10.1101/2024.01.02.123456 --locator "Fig 3B; Results ¶4" --file appr.toml
appraiser = "agent:claude-opus-5-5"       # agent:<model> | human:<handle>; appraiser_session is stamped, not written
read_depth = "methods"                    # abstract < full-text < methods < supplement < data-or-code
blind_to = []                             # ids of earlier appraisals NOT in this appraiser's context (attested)
scope_claimed = "PDZ3 allostery generally"
scope_supported = "PDZ3 (1BE9), one ligand, 3×100 ns, one force field"
interpretation = "Read 'exclusively' off a heatmap with no colour-bar values; apo cell is pale, not zero."

[[for]]
id = "f1"
category = "multi-seed-uncertainty"
statement = "Three independent replicas per state, with bootstrap CIs shown in Fig S4."
basis = "inspected"                       # asserted < inspected < executed < reproduced < independent
evidence = "SI p.6, Fig S4"

[[against]]
id = "a1"
category = "unconverged-sampling"
statement = "100 ns per replica; the paper's own block analysis (Fig S5) has not plateaued."
basis = "inspected"
severity = "major"                        # minor < major < decisive
evidence = "SI Fig S5"
addressed_by = []                          # ids of `for` entries that rebut this

[[against]]
id = "a2"
category = "single-source"
statement = "No independent replication found; the only other report is from the same group."
basis = "inspected"
severity = "minor"
evidence = "search: 'PDZ3 H71 holo coupling', 2026-10-15, 0 independent hits"
```

**Ordinals (all listed orders are total, ascending):**
- `read_depth`: abstract < full-text < methods < supplement < data-or-code
- `basis`: asserted < inspected < executed < reproduced < independent
- `severity`: minor < major < decisive
- `use_tier`: refuted-for-use < contested < hypothesis-source < threshold-source
- `uses_as` → minimum tier it needs: `contrast-context` → contested; `hypothesis-source` → hypothesis-source; `threshold-source` → threshold-source

**Field rules (validator, enforced by `bth appraise`):**
1. `for` and `against` must **both be non-empty**. There is no sentinel: a statement matching `none|n/?a|no (concerns|issues)|nothing` (case-insensitive) or shorter than 30 characters is rejected. If the appraiser can't find a specific objection, they must still name the strongest *generic* one from the closed list (e.g. `single-source`, `not-reproduced`) with `basis = "asserted"`. Lack of scrutiny then shows up as weak basis instead of reading as clean. Severity floors per category were considered and rejected as scope creep: the high-stakes path (`threshold-source`) is gated by independence instead.
2. **Basis ladder (R3).** `asserted` means the source or the appraiser says so and nothing was checked. `inspected` means the appraiser read the relevant methods, data or code. `executed` means they ran it. `reproduced` means they obtained the reported number. `independent` means a third-party reproduction, which is itself a citation that needs its own appraisal. A `for` entry of category `artifact-available` with basis `asserted` is legal but contributes nothing to the use tier.
3. **No combined score field exists (R1).** There is deliberately no `confidence` and no `trust = 0.7`. The only aggregate is the derived `use_tier` below, and it is rule-based and non-compensatory.
4. `severity` is required on `against`, and absent on `for`, since strength there is carried by `basis`. An `against` entry counts as addressed only if every id in `addressed_by` points to a `for` entry whose basis is ≥ `inspected`. **Self-rebuttal limit (C12):** a `decisive` or `major` entry is addressed only by a `for` entry from a *different* appraisal record with a different `appraiser_session`, or by a `for` entry with basis ≥ `executed`. Cross-record `addressed_by` uses `<appraisal_id>:<for_id>`.
5. `scope_supported` must differ from `scope_claimed` unless `read_depth ≥ methods`. The comparison is done after normalizing case, whitespace and punctuation, so a trailing period doesn't count as different (C12). Otherwise "it says what it says" wins by default.
6. Closed category lists; `other` is allowed with a mandatory `other_label`. Recurring `other_label`s are evidence for extending the list, the same posture as HYG-002 for stage names (debt #2743).

**Category lists (v1):**
- `against`: `small-n`, `sampling-variance` (seeds/replicas), `unconverged-sampling`, `no-uncertainty-reported`, `weak-or-untuned-baseline`, `missing-control`, `missing-ablation`, `leakage-or-circularity`, `researcher-degrees-of-freedom` (selective reporting, post-hoc analysis choices, HARKing), `measurement-validity` (metric doesn't measure the claim), `outlier-dominance` (a few cases carry the result), `scope-overreach` (the claim-evidence gap), `not-reproduced`, `artifact-unavailable`, `single-source`, `conflict-of-interest`, `other`.
- `for`: `preregistered`, `independent-replication`, `multi-seed-uncertainty`, `effect-size-uncertainty`, `controls-present` (positive/negative/placebo), `external-validation` (new system, data or conditions), `artifact-available`, `convergent-methods` (different method, same answer), `mechanistic-consistency`, `other`.
- What was checked lives in `basis`, not in the category. So there is no separate "code-ran" category: it is `artifact-available` + `basis = "executed"`.

**Derived `use_tier`** for a citation (computed over *all* its appraisals, never declared; same posture as `review_tier`, `sidecar.py:189`):

| tier | requires |
|---|---|
| `refuted-for-use` | any appraisal has an unaddressed `against` with `severity = decisive` |
| `contested` | any appraisal has an unaddressed `against` with `severity = major` |
| `hypothesis-source` | otherwise. This is the default ceiling. |
| `threshold-source` | `hypothesis-source`, plus `read_depth ≥ methods`, plus at least one `for` with basis ≥ `inspected`, plus `value_provenance ∈ {published, derived}`, plus **appraiser independence** (below) |

Non-compensatory across appraisals as well: a later favourable appraisal does not erase an earlier decisive objection. Only an `addressed_by` that meets rule 4 does.

**Appraiser independence (decision, user 261015: a different agent session counts).** At least one qualifying appraisal must have an `appraiser_session` different from the run's `agent_session`. The run's `agent_session` is a new run column, stamped from the same env var on the run that first executes the citing confirmatory sidecar. `blind_to` is attested, not enforced. Two consequences come from the 261015 spike (§2):
- **A fresh subagent counts; a fork does not (§7.8).** A subagent inherits its parent's `CLAUDE_CODE_SESSION_ID`, so the session id alone cannot tell a fresh subagent, a fork and the parent apart. The appraisal therefore also records `appraiser_agent_id` and `appraiser_kind ∈ {session, subagent, fork}`, from a mechanism to be spiked in S3.
- **Fallback.** Where no session id is available (non-Claude MCP client, plain shell), `appraiser_session` must be passed explicitly and is recorded as attested (`session_attested = true`). Attested sessions are labelled as such in `cite_run`.

`uses_as` states what the author *wants*. `use_tier` states what the appraisals *license*. Validation fails when `uses_as` needs a higher tier than `use_tier` (ordinals above), e.g. `uses_as = "threshold-source"` with tier `contested`.

**Skill guidance (not enforceable, stated anyway):** write the `against` entries *before* the `for` entries. This is the blind-then-reveal idea from R4's corpus, applied against sycophantic, confirmation-seeking appraisal, the failure mode the user named.

### 3.4 Promotion: observation → confirmatory test

```
bth new-experiment confirm_h71_hub --from-observation <obs_id>
```

This scaffolds a **validation-stage** sidecar (except for rank 0, see below) pre-filled with:
- `hypothesis` drafted from the observation text, for the user to edit.
- `[provenance] derived_from_observation = "<obs_id>"`, which records that the hypothesis was data-generated. The observation's status becomes `promoted` once the sidecar's first run is recorded.
- **`[holdout]` (required when `derived_from_observation` is set):** what makes the **test data** independent of the data that suggested the hypothesis. Kinds are **ranked**, and the rank bounds what the promoted claim may assert:

  | rank | kind | independent of | bounds the claim to |
  |---|---|---|---|
  | 0 | `new_seeds` (same build) | nothing above the frame/seed level | **kill test only** (§7.6). It scaffolds an *exploratory* sidecar, not a validation one. It may set the observation's status to `refuted` or `survived-seed-check`, but it never promotes. In MD, seeds are draws from one distribution (`md-replicas-are-samples.md`); in ML, the corpus reports new seeds as "merely a check on repeatability" (research Q2) |
  | 1 | `new_builds` | initial condition / placement | this system under this protocol |
  | min(2, unit level) | `held_out_split` with `unit ∈ {seed, build, system}` | the observed subset | the population the split was drawn from. A `unit = "seed"` split ranks 0 and `unit = "build"` ranks 1; only a split by system or a larger unit reaches 2. Frame or time splits of the same trajectories are not a valid kind (C9). **Must be declared before the observation's source run(s)** (see timing, below) |
  | 3 | `new_system` | the system itself | the class of systems sampled |
  | 3 | `literature_prediction` | our data entirely | as stated in the prediction. **Valid only when `derived_from_observation` points at a run-less observation** with an appraisal (§3.3.1); a run-backed observation needs a data holdout of rank ≥ 1 (C8) |

  The ranking adapts NotebookLM's ML-literature synthesis (seeds < IID samples < new systems) to the nested MD structure (frames ⊂ seeds ⊂ build ⊂ condition). The order is our judgement, not a sourced result. `n_units` and the contrast unit (the build-level test the MD rule requires) belong to estimand discipline (#2677) and are not specified here.
- **Split timing (C10).** The check compares against the timestamps of the observation's **source run(s)**, not against `obs.created_at`, which the observer controls. The split manifest's own time must come from its first git commit or from its being a recorded output of an earlier `bth run`. If neither is available, the split ranks 0.
- **Laundering tripwire (C3).** A lint **warning** fires when a confirmatory sidecar has no `[holdout]` and either its `script_sha256` or its canonicalized `[outcomes]` matches a prior exploratory run in the project. This is the rename-and-retune path. It is a warning, the same attestation posture as the argv check below, because no tool can stop a deliberate undeclared copy; the warning makes it visible to review and to sprint-audit. Requiring a `[holdout]` on *every* confirmatory sidecar was rejected because it would change confirmatory rules (§4).
- **Attested vs. checked (decision, user 261015: attestation acceptable).** Until #2686 adds input-artifact anchors, `[holdout]` is an attestation. Lint warns when the confirmatory command's argv matches the source run's argv. Attested holdouts are recorded as `holdout_verified = false`, and a claim resting only on attested holdouts is labelled so in `cite_run` and the campaign report; the claim tier is not capped (§7.3). When hash checking ships, it upgrades existing records retroactively where input hashes allow.
- **Thresholds are not scaffolded as numbers.** Each `[outcomes]` condition is emitted as `metric <op> ${THRESHOLD}` with a `source` that must be filled. The scaffold suggests, in order:
  - (a) `sesoi:<value>:<why>`, the smallest effect size of interest, i.e. the effect that would matter for the decision, stated before the test (research Q3: SESOI/MMES; equivalence-test bounds come from it).
  - (b) A calibration run (noise floor / spike-in MDE, see §3.5). Because the SESOI carries a number, the comparison is deterministic: a calibration MDE larger than the SESOI value means the design is underpowered by construction, and lint says so (C21).
  - (c) A literature value whose `use_tier` is `threshold-source` (§3.3.2).
  - (d) `provisional:` with a reason.

  A sidecar with an unfilled `${…}` fails validation. That is the point: the number has to be chosen consciously, and its basis is recorded.
- The effect size seen in the exploratory run may be shown as context, with a warning. It is the optimistic, selection-biased estimate, so it can't serve as a `source`. Lint rejects `source = "run:<the exploratory run>"`, and more generally any `run:` source whose class is not `calibrating`.

### 3.5 Where thresholds legitimately come from: calibration runs

A calibration run measures the quantity a threshold should be made of: the noise floor, the null distribution, the spike-in minimum detectable effect, or seed-to-seed spread at the pooled level. It records the value, not a verdict (`outcome='observed'`, §3.1). A later confirmatory sidecar cites it as `source = "run:<id>:<field>"`. Lint checks that the run:
- exists;
- has `epistemic_class = 'calibrating'`;
- has the field;
- **exited 0, with an outcome that is not `error` or `invalid_measurement`** (C16).

This turns "arbitrary threshold" into "threshold = 2× the measured noise floor of run abc", which is a choice that is defensible, reviewable and re-derivable. The multiplier itself still needs its own basis, or a `provisional:` reason. Calibration sidecars validate under their own branch: `[outcomes]` optional, and `[result_schema]` required, since it must contain the calibrated field. Calibration drift follows the confirmatory default (not advisory), because a calibration value is about to be used as a source. Guidance (not enforced): cite a calibration *stem* with the spread across its runs, rather than the single most convenient run.

### 3.6 Gates that read the class

- **Union Gate / claim register:** coverage at `claim.py:908` counts only runs with `epistemic_class ∈ {confirmatory, legacy}`. Exploratory and calibrating runs never cover a clause, in any campaign mode. Message: "run X is exploratory (observed); promote via `bth new-experiment --from-observation`" (C14).
- **Campaign add:** a confirmation campaign refuses exploratory runs and allows named calibration runs. A sequential campaign refuses both exploratory and calibrating runs (§3.1).
- **sprint-audit, new signals:**
  - the rate of exploratory sidecars whose `[outcomes]` branches lack `source` during the warning window, i.e. residual cutoff-invention. This is the metric for whether the friction went down;
  - the laundering-tripwire hit rate (§3.4);
  - the share of appraisals whose `against` entries are all `basis = asserted`.

## 4. Non-changes (explicit)
- Confirmatory pre-registration rules are untouched. This spec loosens nothing that a citable verdict depends on. Sidecars with no `stage_name` (`legacy`) behave exactly as today, and a typo fails closed (§3.1).
- `scripts/explore/` stays unenforced (true scratch). The new contract is for exploration you intend to keep and cite.
- **Schema additions (revised, C1/C4/C6/C7/C15):** three run columns, `epistemic_class`, `tentative_outcome` and `agent_session`, all written at run start or end and immutable afterwards (a schema-version bump in the cool and warm tiers). Two new ledgers: `observations` and `appraisals`. The draft's "no new column" was a design preference, not a user decision, and review showed it could not be made safe.
- The existing `requires_pass_stem` and parity gates keep their stage lists; they become safe because exploratory rows no longer store `pass`.

## 5. Rule and skill text changes (proposed; the user's files, not edited by this spec)

Global `~/.claude/CLAUDE.md`, "STAGE EVERY EXPERIMENT WITH BATHOS". Replace "stating the hypothesis and the `[outcomes]` pass/fail criteria" with:

> stating either (a) **exploratory**: the question and what will be produced (`[observe]`), with no verdict thresholds (cite results as observations, `bth observe`); or (b) **confirmatory**: the hypothesis and `[outcomes]` criteria, each branch carrying a `source` (literature with an appraisal, a calibration run, a SESOI, or `provisional:` with reason). A confirmatory test of something first *seen* in exploration must use data the exploration did not (`[holdout]`, rank ≥ 1). Always set `stage_name` explicitly.

`using-bathos` skill: add a "Which mode am I in?" decision table (§3.1) at the top. Update `bth new-experiment`'s default template to the exploratory shape (§3.2): the current template *teaches* the arbitrary-threshold pattern.

## 6. Implementation slices

| # | Slice | Files | Size | Depends on |
|---|---|---|---|---|
| S1 | Run columns (`epistemic_class`, `tentative_outcome`, `agent_session`) + schema bump; class resolution incl. `legacy` and fail-closed typos; exploratory **and calibration** validator branches; `source` grammar; sanity checks + runtime precedence; `observed` stored and neutral at all five sites; no obligations for exploratory/calibrating runs | `schema.py`, `catalog.py`, `compact.py`, `sidecar.py`, `validate.py`, `runner.py`, `campaigns.py` (neutral list), `linter.py`, `migrate.py`, tests | standard+ | — |
| S2 | Exploratory scaffold as `new-experiment` default; `--mode confirm` keeps today's template with `${THRESHOLD}` placeholders | `new_experiment.py`, `authoring/`, `mcp.py` (`new_experiment_tool`) | quick | **ships together with S1** |
| S3 | `bth observe` (run-backed + run-less) + observation status; `bth appraise` + appraisal ledger + `use_tier`; skepticism fields on `LiteratureReview`; MCP tools | new `observations.py`, new `appraisal.py`, `runlog/` event kinds, `sidecar.py`, CLI, `mcp.py` | large | S1 |
| S3b | Rendering of `observed (tentative: …)` in every outcome reader; canonical-block drift reclassification | `query.py`, `rich_fmt.py`, `cite.py`, `viz/`, `prereg.py` | standard | S1 (storage already safe without it) |
| S5 | `run:<id>:<field>` resolution for calibration runs; SESOI-vs-MDE lint | `linter.py`, `validate.py` | quick | S1 |
| S4 | `--from-observation` promotion + `[holdout]` (units, timing, rank) + laundering tripwire + self-sourcing lint | `new_experiment.py`, `sidecar.py`, `linter.py`, `mcp.py` | standard | S3, S5 |
| S6 | Gate wiring (Union Gate class filter, campaign add for confirmation + sequential) + sprint-audit signals; fix #2080 in passing | `claim.py`, `campaigns.py`, `sprint_audit.py`, `mcp.py` | standard | S1 (signals also need S3/S4) |
| S7 | Skill + rule card (`DSGN-*`: "exploration records observations, not verdicts"); agent plugin manifest | `agent_assets/`, `agent_plugin.json` | quick | S2 |

Recommended order: S1 + S2 together remove the friction on day one, and S1 alone already makes storage safe (C4/C5). Then S3, S5, S4 for the follow-up path, and S3b/S6 to harden it.

## 7. Decisions (user, 261015)

1. **Exploratory `[outcomes]`: allowed, not banned**, and taken lightly, because exploration runs under heavy epistemic and operational uncertainty. See §3.2.1.
2. **Run-less observations: allowed**, through the existing citation surface and skeptical by default. Accepting claims blindly is a known agent failure mode. See §3.3.1.
3. **`[holdout]` as attestation: acceptable** (no strong preference). Attested holdouts are labelled `holdout_verified = false` and upgraded when #2686 lands. See §3.4.
4. **No "no concerns", ever.** Appraisals carry structured for/against arguments with a basis ladder and a derived, non-compensatory use tier (§3.3.2).
5. **Done:** notebook `1d786e78` was cross-checked, targeted at the appraisal payload. See `research/261015_claim-appraisal-nlm-research.md`.
6. **Rank-0 (`new_seeds`) holdouts: kill test only.** A seed-only follow-up may set the observation's status to `refuted` or `survived-seed-check` (§3.3). It cannot promote the observation to a confirmatory claim; promotion requires holdout rank ≥ 1. `--from-observation` with `kind = "new_seeds"` scaffolds an exploratory-class kill test. Rationale: new seeds can falsify cheaply but cannot independently confirm, because they share the build and the initial-condition confound.
7. **Category lists: ship v1 closed, grow via `other` + `other_label`.** A per-domain plugin interface is out of scope, filed as low-priority debt #2743, to be revisited once `other` is used stably and consistently.
8. **Appraiser independence: a different agent session counts, and so does a fresh subagent, but a fork does not (user, 261015, amended).** A fresh subagent sees only its dispatch prompt; a fork inherits the citing session's full context, so it is not independent. Plus attested `blind_to` in every case. Same model is fine. **Mechanism is open (S3 prerequisite spike):** subagents share their parent's `CLAUDE_CODE_SESSION_ID` and no agent-id env var exists (§2), so `appraiser_session` alone cannot distinguish them. The candidate is hook input (`agent_id` / `agent_type`), injected as an identity that `bth appraise` stamps. Until that is verified, a subagent appraisal is accepted only with an attested `appraiser_kind = "subagent"`, labelled as attested. S1 records only `agent_session` on runs.

## 8. Verification plan
- **Unit tests:** an exploratory sidecar without `[outcomes]` validates. With a branch lacking `source` it fails, for new stems. With a well-formed `source` it passes, and a malformed `source` fails. The whole existing validate suite must stay green.
- **Negative controls** (each must fail, or must keep today's behaviour):
  - a confirmatory sidecar that drops `[outcomes]` still fails. This proves the exploratory branch didn't loosen the confirmatory path;
  - a sidecar with no `stage_name` and bare cutoffs validates and evaluates exactly as today (`legacy`) (C1);
  - `stage_name = "validaton"` is confirmatory and raises a lint error (C2);
  - an exploratory run whose tentative label is `pass` does **not** satisfy `requires_pass_stem` (C4);
  - an exploratory run opens no obligation, and is neutral in `compute_evalue` and in the sequential lock (C5);
  - a sequential campaign refuses an exploratory run (C5);
  - editing `stage_name` after a run does not change that run's stored class (C15);
  - copying an exploratory script to a new stem with retuned cutoffs fires the laundering warning (C3);
  - a self-authored `for` entry does not address a `major` or `decisive` `against` (C12);
  - a `figure-read` value never reaches `threshold-source` (§3.3.2);
  - a fork appraisal does not satisfy independence for its parent session, while a fresh-subagent appraisal does (§7.8);
  - explicit `exploration` without `[observe]` is `legacy` on a stem with runs and a validation error on a new stem;
  - exploratory and calibrating runs never cover a claim clause (C14);
  - a `held_out_split` with `unit = "seed"` ranks 0 and cannot promote (C9).
- **Positive control for sanity:** a run whose sanity entry is deliberately false records `invalid_measurement` with reason `sanity:<expr>`.
- **Dogfood:** re-author one existing exploratory sidecar from a sibling project (e.g. a demistify EDA) in the new shape, then verify **by record**, after `bth compact`: `bth sql "SELECT id, outcome, tentative_outcome, epistemic_class FROM runs WHERE id LIKE '<prefix>%'"` shows `observed` / the tentative label / `exploratory`. Run one denxity `*_confirmatory` sidecar unchanged and confirm it shows `legacy` with today's outcome.

## 9. Research grounding

[`research/261015_exploratory-mode-nlm-research.md`](../research/261015_exploratory-mode-nlm-research.md) and [`research/261015_claim-appraisal-nlm-research.md`](../research/261015_claim-appraisal-nlm-research.md). Questions went to NotebookLM notebooks `3f0490aa` and `1d786e78`. Quotes are verified only as *present in NotebookLM's answers*, and no primary source has been read, so nothing there is cited as a finding. Numbers flagged in those docs' caveats (e.g. "22–29% seed false-positive rate") must not be copied into this spec or any sidecar.

## 10. Adversarial review dispositions

Full reports: [`audits/261015_exploratory-mode-contract-adversarial-review.md`](../audits/261015_exploratory-mode-contract-adversarial-review.md). The challenger raised 25 findings (7 blocker, 12 major, 6 minor). The defender conceded 10, partly conceded 14 and rebutted 1. Every concession is applied above, using the defender's minimal fix unless noted.

| id | verdict | applied in |
|---|---|---|
| C1 legacy sidecars reclassified | concede | §3.1 `legacy` class, §3.2 migration warning, §8 |
| C2 typo coerces to exploration | concede | §3.1 fail closed + lint error |
| C3 rename-and-retune laundering | partial | §3.4 tripwire (warning); `data_unseen` on all confirmatory and 2-sig-fig matching rejected |
| C4 tentative is render-only | concede | §3.1/§3.2.1 `outcome='observed'` + `tentative_outcome` |
| C5 `observed` counts as failure | concede | §3.2 neutral at five sites, no obligations; §3.1 sequential refuses |
| C6 reclassification needs a column | concede | §3.2.1 canonical-block drift, strictest class, autonomous deny kept; §4 |
| C7 independence unverifiable | concede | §3.3.2 `bth appraise` ledger, `agent_session`; spike done (§2) |
| C8 literature_prediction conflation | partial | §3.4 run-less observations only |
| C9 split without unit | partial | §3.4 `unit`, rank = min(2, unit level); `n_units` → #2677 |
| C10 obs timestamp gameable | partial | §3.4 timing against source runs + git/bth time |
| C11 rank-0 carve-out contradicts §7.6 | concede | §3.4 carve-out deleted; §3.3 status events |
| C12 appraisal gaming | partial | §3.3.2 self-rebuttal limit, normalized compare, ordinals; severity floors rejected |
| C13 cutoff hiding | partial | §3.2 source on every non-residual branch, AST sanity check, closed grammar |
| C14 Union Gate ignores class | concede (partial per defender) | §3.6 |
| C15 retroactive reclassification | rebut (moot) | §3.1 immutable column makes it structurally impossible |
| C16 calibration underspecified | partial | §3.5, §6 S1 |
| C17 ledger bypasses event store | partial | §3.3 `emit_or_legacy`; cool-fallback half was wrong (`query.py:345`) |
| C18 runtime precedence | partial | §3.2 precedence table |
| C19 verification plan thin | partial | §8 |
| C20 `planned_tests` unit | concede | §3.2.1 |
| C21 SESOI not numeric | concede | §3.4 `sesoi:<value>:<why>` |
| C22 §2 errors | concede | §2 rows and citations |
| C23 slice dependencies | partial | §6 |
| C24 no assumption ledger | partial | §2 tags |
| C25 run-less appraisal optional | partial | §3.3.1 required; claim-tier cap rejected (§7.3 is labelling) |

**Next:** ready for implementation planning, starting with S1 + S2.
