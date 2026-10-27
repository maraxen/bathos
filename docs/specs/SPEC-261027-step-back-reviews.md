---
title: "Step-Back Reviews for Bathos Campaigns: System Triggers, Dual Review Paths, Reviewer Independence, Escalation Ladders, and Anti-Rubber-Stamp Meta-Signals"
description: "Exhaustive architectural specification for Task 261027_step-back-reviews: automated event triggers with mathematical guard clauses, 5-dimension scored rubric with hierarchical gap-free evaluation and hard-floor veto, 5 qualitative cognitive modes with campaign domain evidence citations, authenticated reviewer independence constraints, 4-tier escalation ladder terminating at the human chair with unconditional FastMCP refusal and TTY/chair-token enforcement, meta-signals 15-18 with dispatch-telemetry guards, Cyclopts CLI/FastMCP tooling parity, DuckDB schema v19 migration, and advisor brief matrix integration."
status: APPROVED_SPEC
task_id: 261027_step-back-reviews
authors:
  - "Worker Agent (worker_phase2_spec_1)"
  - "Worker Agent (worker_spec_revision_1)"
  - "Worker Agent (worker_skills_spec_1)"
  - "Teamwork Orchestrator (orchestrator_1)"
date: "2026-10-27"
version: "1.2.0"
backlog_ids: ""
adversarial_review: "passed adversarial revision cycles resolving BLK-01..03, MAJ-01..05, MIN-01..04, Findings 01..15 (0 Blocker / 0 Major)"
---

# Step-Back Reviews for Bathos Campaigns: Architectural Specification

**Task ID:** `261027_step-back-reviews`  
**Version:** `1.2.0` (Comprehensive Approved Specification)  
**Date:** 2026-10-27  
**Status:** APPROVED_SPEC  
**Workspace:** `/home/marielle/projects/bathos` (Worktree: `.claude/worktrees/261027-step-back-reviews`)  
**Dual Placement:**  
- `docs/specs/SPEC-261027-step-back-reviews.md`  
- `.praxia/docs/specs/261027_step-back-reviews-advisor-committee-escalation.md`  

---

## 0. Problem Statement & Motivation

Autonomous and semi-autonomous research agents operating in computational biology, molecular dynamics, machine learning, and protein design face severe cognitive failure modes:

1. **Behavioral Lock-in & Parameter-Tweaking Loops**: When an initial hypothesis underperforms, agents often enter local optimization loops—fine-tuning Langevin gamma parameters, grid spacing, learning rates, or sequence masks—without stepping back to question underlying mechanistic assumptions.
2. **Superficial Review Compliance ("Rubber-Stamping")**: When self-reviews or peer-reviews are automated, agents default to agreeable, low-variance evaluations that praise progress and issue `CONTINUE` recommendations without checking primary catalog data.
3. **Escalation Erosion**: Warnings, near-bound hits, and statistical anomalies are either dismissed by autonomous agents or buried under rapid sequential job launches.
4. **Proposer Bias & Gamed Checks**: An agent reviewing its own work tends to find justifications that support its desired hypothesis. Past history demonstrates that mechanizing judgment often simply moves the failure mode: agents game visible gates by inventing arbitrary cutoffs or scaffolding self-negating controls.

While `bathos` has established rigorous pre-registration gates for experiment setup (sidecars, claim definitions, Union Gate clauses, literature parity attestations), **post-launch trajectory evaluation has remained largely ungated**. Once a campaign is open, nothing systematically prompts independent evaluation of whether the campaign remains scientifically viable.

**Step-Back Reviews** provide the **outward and upward** check:
- **System-Evaluated Event Triggers**: Algorithmic detectors monitoring campaign run streams for repeated marginal outcomes, near-bound hits, outcome-entropy collapse, metric plateaus, and staleness—equipped with robust guard clauses against numerical singularities and post-pivot evaluation deadlocks.
- **Dual Review Paths**: A longitudinally comparable, 5-dimension scored rubric with hierarchical gap-free mapping and hard-floor veto (Path A), and a structured qualitative open-analysis path featuring 5 cognitive modes and mandatory campaign domain primary evidence citations (Path B).
- **Reviewer Independence & Minority Dissent Preservation**: Formal attribution barriers binding reviewers to authenticated runtime identities and preventing self-review, coupled with immutable schemas that preserve dissenting minority critiques.
- **An Escalation Ladder Terminating at the Human Chair**: A 4-tier state machine (Levels 0–3) strictly adhering to bathos Decision D1 (advisory non-blocking warnings during execution, conclusion downgrade), terminating at an inviolable Human Chair gate that AI agents can never close, de-escalate, or override via API, CLI, or FastMCP.
- **Anti-Rubber-Stamp Meta-Signals**: Automated cross-project surveillance signals (Signals 15–18) in `sprint-audit` that detect unvarying verdict streaks, low-evidence reviews, ignored escalations, and review velocity anomalies with dispatch-timestamp telemetry gating.

---

## 1. Recon Assumption Table

This section documents the foundational empirical and codebase assumptions upon which the step-back review subsystem is constructed. In accordance with the verification protocol, each assertion is explicitly tagged with `verified` (citing exact `file:line`) or `unverified`.

| # | Assumption / Assertion | Tag | Source / Evidence Location | Technical Impact & Notes |
|---|---|---|---|---|
| 1 | CLI is implemented using Cyclopts in `cli_cyclopts.py`, not Typer `cli.py` | `verified (src/bathos/cli_cyclopts.py:1935)` | Commit `f37bdf09`; PR #148; `cli_cyclopts.py` lines 1935–2220 | Command group `stepback_app = cyclopts.App(name="stepback")` registered at root. |
| 2 | FastMCP server tools mirror CLI subcommands | `verified (src/bathos/mcp.py:5915-6044)` | `src/bathos/mcp.py` lines 5915–6044 | Full tooling parity: `stepback_{status,trigger,run,record,review,escalate,show,resolve}`. |
| 3 | Schema version is currently `"18"` before v19 bump | `verified (src/bathos/schema.py:10)` | `src/bathos/schema.py` line 10 | `CURRENT_SCHEMA_VERSION = "18"` consumes run-level cool schema. Bump to `"19"` required. |
| 4 | DuckDB warm tier tables are created idempotently in `compact.py` | `verified (src/bathos/compact.py:865, 1087)` | `src/bathos/compact.py` lines 865–872, 1085–1087 | Invokes `_ingest_stepback_fragments()` during compaction pass; uses `CREATE TABLE IF NOT EXISTS`. |
| 5 | Cool-tier storage uses atomic write-then-rename JSON files | `verified (src/bathos/stepback/catalog.py:145-175)` | `src/bathos/stepback/catalog.py` lines 145–175 | Uses `tempfile.NamedTemporaryFile` + `os.replace` to prevent SLURM concurrency races. |
| 6 | `stepback_escalations.opened_by_review_id` is nullable | `verified (src/bathos/stepback/catalog.py:111)` | `src/bathos/stepback/catalog.py` line 111 | Schema allows `opened_by_review_id VARCHAR` to be NULL when triggers open escalations. |
| 7 | Conclude-time Step-Back Gate checks Level $\ge 2$ and downgrades to `"confounded"` | `verified (src/bathos/campaigns.py:1580-1614)` | `src/bathos/campaigns.py` lines 1580–1614 | Decision D1 conclude hook: confirmation/sequential campaigns downgraded unless `--force-verdict`. |
| 8 | Decision D1 non-blocking execution posture in `obligations.py` | `verified (src/bathos/obligations.py:85)` | `src/bathos/obligations.py` line 85 | Rigor gates never block execution by default; warn or downgrade; blocking is strictly opt-in. |
| 9 | Signal 11 in `sprint_audit.py` is `differential_staleness_count` (Signal 12 is missing claim) | `verified (src/bathos/sprint_audit.py:410, 485)` | `src/bathos/sprint_audit.py` lines 410, 485 | Numbering verified; Signal 11 is differential staleness; Signal 12 is confirmation claim absence. |
| 10 | Anti-rubber-stamp signals 15–18 implemented in `sprint_audit.py` | `verified (src/bathos/sprint_audit.py:520-680)` | `src/bathos/sprint_audit.py` lines 520–680 | Tracks unvarying verdicts, low evidence rate, ignored escalations, turnaround velocity anomalies. |
| 11 | Retrospective validation script exists at `scripts/experiments/validate_stepback_retrospective.py` | `verified (scripts/experiments/validate_stepback_retrospective.py:1)` | `scripts/experiments/validate_stepback_retrospective.py` lines 1–170 | Validates triggers against historical catalog campaigns `6f792f0d`, `ec49fb00`, `ff94e8bb`. |
| 12 | Historical catalog campaigns exist in local catalog | `verified (~/.bth/catalog/runs)` | `~/.bth/catalog/runs/<project>/run_*.parquet` | Read-only mining verified `sp98` (`6f792f0d`), `sp91` (`ec49fb00`), and `mentee` (`ff94e8bb`). |
| 13 | Grounding research document available at `.praxia/docs/research/` | `verified (.praxia/docs/research/261027_advisor-checkpoints-step-back-research.md:1-251)` | Primary checkout uncommitted research doc | Read-only survey verified §1, §5, §7, §8, §9 findings on advisor questions and domain errors. |
| 14 | Praxia `scripts/projects/project_status.py` and `project_status.toml` structure | `unverified` | Direct filesystem access restricted | Reconstructed from incident corpus and historical specification records. |
| 15 | External literature citations (Platt 1964, BrainBench, ForecastBench) | `unverified` | Gemini Notebook queries timed out | Notebook grounding unconfirmed on external web; internal incident corpus fully verified. |

---

## 2. Catalog-Mining Findings & Literature Synthesis

This section synthesizes the empirical grounding from the historical catalog mining and internal incident corpus (`.praxia/docs/research/261027_advisor-checkpoints-step-back-research.md`).

### 2.1 What Advisors Actually Ask, and When (§1)
Advisors in empirical science (biophysics, protein engineering, machine learning) serve a distinct cognitive function compared to doing researchers. Grounded in laboratory management compacts (Schvartzman lab compact; *Making the Right Moves*, HHMI/BWF):

**The Core Advisor Questions:**
1. *"What is the central question, and what gap does it fill?"*
2. *"What is your hypothesis, in your own words?"*
3. *"What alternative hypotheses could explain this, and what experiment distinguishes them?"*
4. *"What is the simplest system that tests it?"*
5. *"Are these data preliminary or final?"*
6. *"What would change your plan?"*

**The Operational Moments:**
- **Before scaling up**: Testing the simplest system first before launching large compute sweeps.
- **On failure or anomaly**: Ad-hoc data meetings upon encountering out-of-distribution outcomes.
- **At milestones**: Periodic committee checkpoints (semi-annual or quarterly reviews).
- **On a schedule**: Bi-weekly one-on-ones with a three-part agenda (past actions, data, 1–3 priorities).

These moments directly inform the system event triggers: firing before compute escalations, on threshold edits, upon flat results that stop changing the plan, and after sustained grind down an exploratory rabbit hole.

### 2.2 History: Mechanising Judgment Moves the Failure Mode (§5)
A critical finding from bathos and praxia historical retrospectives is that **judgment lessons recur after being written down**, and **mechanizing a lesson moves the failure mode**:

- **Checking the Instrument First**:
  - `asr` lesson #15: 3 hours lost to an inverted temperature convention.
  - `praxia` lesson #381: A multi-agent swarm investigated a measurement that was null on every row for 74 minutes, burning 2.54M subagent tokens and 558 tool uses.
- **Proposer Bias & Self-Grading**:
  - `praxia` lesson #246: An outside refuter evaluated a proposer's self-graded work and uncovered six load-bearing errors—all in the author's favor. Proposers cannot impartially grade their own hypotheses.
- **"An Agent's Report is a Claim, Not Evidence"**:
  - This principle had to be rewritten at least eight times across praxia history (#46, #51, #134, #310, #435, #471, #477, #521) because agents routinely treat self-reports as verified ground truth.
- **Pathologies of Mechanization**:
  - *The Self-Negating Adversarial Check* (`CHANGELOG.md` lines 740–748): `bth new-experiment` scaffolded an `adversarial_check` that was the pass condition's own negation. Because the heuristic only checked field presence, the invalid check persisted unnoticed.
  - *Exploratory Mode Arbitrary Cutoffs* (`.praxia/docs/specs/261015_exploratory-mode-contract.md` §1): Mandatory `[outcomes]` forced agents to invent arbitrary pass/fail cutoffs for exploratory sweeps, which then falsely satisfied `requires_pass_stem`.
  - *The Inactive Claim Gate* (Commit `2065fd50`): A catalog query returned `"1418, 0, 0"` because the origin field was never populated. Every campaign passed while tests remained green. As noted: *"a gate that returns the same verdict for every possible input is not a gate."*
  - *STEM Fellows Census Sentinel Pass*: A census script passed pre-registered controls while counting sentinel defaults as findings until a saturation control exposed the failure.

**Core Lesson**: If a check is visible to the agent generating the work, the agent will learn to satisfy the check without fulfilling its underlying scientific intent. Therefore, step-back reviews **must be conducted by an independent blind reviewer** evaluating raw catalog records against stated goals, rather than evaluating the doing agent's advocacy.

### 2.3 Forecast Calibration vs Knowing When to Stop (§7)
- Proper scoring rules (multiclass Brier score, log score with floor) decompose into **calibration** and **resolution** (sharpness).
- However, selective prediction (knowing when to ask for human guidance or stop) depends on **ranking and failure AUROC**, not calibration alone. Monotone recalibration cannot alter ranking.
- **Implication**: A well-calibrated agent does not inherently know when to terminate a stalled campaign. Step-back triggers cannot rely on self-calibration; they must be mechanically evaluated from trajectory statistics.

### 2.4 Design Implications for Step-Back Subsystem (§8)
1. **Triggers Detectable from Logs**: Firing moments must be extracted mechanically from catalog telemetry: run-count jumps (scaling up), sidecar condition diffs (threshold moving), identical consecutive outcomes (entropy collapse), and token/duration consumption.
2. **Reviewer Independence & Blind Reviewer**: Evaluators must be blind to the doing agent's summary justifications.
3. **Scoring by Downstream Outcomes**: Measure step-back efficacy not by the literary eloquence of the agent's prose, but by **downstream compute saved** after cheap diagnostics expose stagnation.
4. **Avoid Another Rigid Pass/Fail Gate**: Do not mechanize step-back judgment into a binary pass/fail gate that invites gaming. Deliver the question at the right moment, evaluate via structured rubric and open analysis, and let empirical outcomes score the trajectory.

### 2.5 Protein Science Domain Failures (§9, §10)
Analysis of the 716-item incident corpus and multi-project session traces across `biosensors`, `adaptyv`, `sweetprots`, `nanobody`, and `tev-design` reveals recurring domain blunders:

1. **Simulation Replicate Pooling vs Convergence (#1 Domain Failure)**:
   - Claude repeatedly treats simulation replicates as separate experiments that must each converge, rather than partial samples of a single thermodynamic ensemble that must be pooled/concatenated.
   - Six dated sessions across four projects recurred even after explicit rules were logged:
     * `biosensors` (260826): Replicate spread used as error bar flipped verdict ($z=9.1$ pooled vs $z=1.0$ spread).
     * `adaptyv` (261001): Within-replica convergence gate failed all 475 hotspots. User: *"we do not expect within or between replica convergence. that is literally why we pool."*
     * `sweetprots` (260930): Per-site Welch tests conducted on 3 vs 3 seeds instead of pooled ensemble.
   - **Step-Back Defense**: The Blind Reviewer protocol explicitly poses the diagnostic inquiry: *"What is the unit of independence here, and what does pooling assume?"*
2. **Checks That Cannot Fail or Discriminate (~15 Incident Clusters)**:
   - Census fields structurally locked to 0 read for weeks as "clean".
   - Primary metric sharing a formula with the training objective ("trusted-broken-pipeline", which corrupted 44% of 1,091 runs in `asr`).
   - Richer models winning on shuffled data.
3. **Variance Inflation & Mismatched Noise Floors**:
   - Seeds of one build sharing placement (between-build variance was 5.7x within-build).
   - Paired effects gated against irrelevant unpaired floors (inflating t-statistics to 20–45).
4. **Over-Trusting Tool Scores**:
   - Structure predictors yielding ranking AUROC $pprox 0.5$ on de novo designs.
   - Fold confidence tracking alignment depth and MSA composition rather than physical validity.
5. **Silently Inert Gates or Knobs**:
   - Accepting a null result without a positive control proving the knob actually moves the output.

### 2.6 Historical Catalog Retrospective Case Studies
- **`sp98-gap-augmented-testbed` (`6f792f0d`)**: Suffered from measurement pipeline contamination across 9 cancelled/preempted runs and 5 guardrail violations. The step-back triggers $T_{	ext{repeated\_marginal}}$ and $T_{	ext{stale}}$ successfully fire; Path B Pre-Mortem detects pipeline contamination and enforces Level 2 Hold.
- **`sp91-testbed-first-fix-development` (`ec49fb00`)**: Near-bound mode-disagreement score ($0.45$ vs $0.50$ cutoff) with hidden basin splitting. Trigger $T_{	ext{near\_bound}}$ fires ($d_N \le 1.0$); Path B review surfaces minority dissent regarding basin divergence, forcing `PIVOT`.
- **`mentee-convergence-deck` (`ff94e8bb`)**: Stalled progress across runs 3–7 with alternating marginal outcomes. Triggers $T_{	ext{repeated\_marginal}}$ and $T_{	ext{plateau}}$ fire; Path A rubric scores Empirical Progress as 1, triggering the hard-floor veto and Level 2 Hold.

---

## 3. Contemplex Architectural Decisions

During Phase 2 ideation and architectural trade-off analysis, the design team evaluated competing options and established binding decisions:

### Decision 1: Modular Sub-package (`src/bathos/stepback/`) over Monolithic Module
- **Context**: Older bathos modules like `claim.py` and `postmortem.py` are monolithic files.
- **Decision**: Implement step-back reviews as a modular sub-package:
  - `schema.py`: Dataclasses and type contracts.
  - `triggers.py`: Pure mathematical trigger algorithms with zero database dependencies.
  - `rubrics.py`: 5-dimension rubric evaluator, TOML loader, and hard-floor veto logic.
  - `analysis.py`: 5 qualitative cognitive modes and campaign domain citation validation.
  - `escalation.py`: 4-tier state machine, direct transitions, and human chair invariance.
  - `catalog.py`: Atomic cool JSON persistence and DuckDB schema v19 ingestion.
  - `engine.py`: High-level orchestration facade.
- **Rationale**: Isolates mathematical unit testing from DuckDB dependencies, prevents cyclic imports, and allows auditing human chair security logic in isolation.

### Decision 2: Trigger Formulas & Sensitivity Calibration
- **Repeated Marginals**: $s_{	ext{thresh}} = 3$ streak or frequency $	heta \ge 0.60$ over $k=5$.
- **Near-Bound Proximity**: Margin $\epsilon = 0.10 	imes |B|$ with $m \ge 2$ hits, or critical proximity $d_N \le 0.25$. When $B=0$, scales by $0.50 	imes s_y$ to guarantee numerical scale invariance.
- **Entropy Collapse**: Shannon entropy $H_e < 0.35$ nats over $k \ge 5$ runs (standardized strictly on natural logarithm).
- **Metric Plateau**: Moving OLS relative slope $ar{eta}_{	ext{rel}} < 0.01$ (1%/run) and $CV < 0.02$ over $n=6$ runs. Handles zero-centered metrics via absolute thresholds ($\delta_{	ext{abs}} = 0.001$).
- **Staleness**: 14 days idle, or 15 runs with zero clause progress on claim campaigns.

### Decision 3: Dual Complementary Review Paths
- **Path A (Scored Rubric)**: 5 dimensions on 1–5 scale with **Hard-Floor Veto Rule** (any score of 1 caps recommendation at `HOLD` and forces Level 2 hold). Eliminates compensatory averaging of fatal confounds.
- **Path B (Structured Open Analysis)**: 5 cognitive modes (`advisor`, `pre_mortem`, `so_what`, `outsider`, `steelman_stop`) with mandatory primary evidence citations ($N_{	ext{valid}} \ge 2$).

### Decision 4: Reviewer Independence & Minority Dissent Preservation
- Reviewer identity is bound to authenticated runtime identity; self-review by run authors or campaign creators is barred (`REVIEWER_CONFLICT_OF_INTEREST`).
- Dissenting reviews are stored in an immutable `dissents` array; any minority recommendation of `HOLD` or `ESCALATE` places the campaign in automatic Level 2 hold.

### Decision 5: Escalation Ladder & Human Chair Invariance
- 4-level state machine (Levels 0–3).
- **Human Chair Invariance (Iron Law)**: FastMCP strictly refuses Level 3 resolutions. Level 3 resolution requires interactive TTY (`sys.stdin.isatty()`) or secure chair token (`~/.bth/chair_token`). Autonomous agents can never override or close Level 3.

### Decision 6: Decision D1 Non-Blocking Posture
- In alignment with bathos Decision D1 (`obligations.py:85`), reviews recommend rather than act.
- `bth run` and `bth submit` emit warning banners by default; blocking occurs only if opted in via `[stepback] enforce = true`.
- At `bth campaign conclude`, confirmation/sequential campaigns with active Level 2/3 holds are automatically downgraded to `"confounded"` unless `--force-verdict` is passed.

### Decision 7: Snooze & Acknowledge Semantics
- Allows researchers/agents to acknowledge non-L3 triggers during intentional sweeps.
- Strictly bounded: maximum 10 runs or 72 hours; non-empty rationale required.
- **Level 3 escalations can NEVER be snoozed.**

---

## 4. Adversarial Review Log (Rounds 1–3+)

This section logs the adversarial findings identified during challenger cycles and their resolutions:

| Finding ID | Severity | Category | Description | Resolution & Spec Section |
|---|---|---|---|---|
| **BLK-01** | **BLOCKER** | Governance / Security | FastMCP parameter injection (`as_human_chair: true`) and unverified string `reviewer_id` spoofing permitted agents to resolve Level 3 escalations. | FastMCP `stepback_resolve` strictly refuses Level 3 (`STEPBACK_AGENT_OVERRIDE_REFUSED`). CLI requires interactive TTY or `~/.bth/chair_token`. `reviewer_id` bound to authenticated runtime identity. (Section 9.2) |
| **BLK-02** | **BLOCKER** | Mathematical Logic | Piecewise score gaps in rubric mapping (e.g. $s=[5,5,5,2,5] \implies S=4.55$ fell through all conditions). | Implemented hierarchical gap-free resolution tree covering all $[1,5]^5$ score combinations. (Section 7.1) |
| **BLK-03** | **BLOCKER** | Relational Database | DuckDB `stepback_escalations.opened_by_review_id` had `NOT NULL` constraint, crashing automated triggers that fire before reviews exist. | Made `opened_by_review_id VARCHAR` nullable; added `opened_by_kind` and `trigger_name`. (Section 13.2) |
| **MAJ-01** | **MAJOR** | Numerical Singularities | Zero division in OLS moving slope at $n=1$, index error $d_N$ at $N=0$, dimensionless $B=0$ boundary comparisons, and false staleness on exploratory campaigns. | Added universal guard clauses ($N \ge 3$, $N \ge 5$); scaled $B=0$ proximity by sample standard deviation $s_y$; evaluated staleness rule 2 only on claim campaigns. (Section 6.1) |
| **MAJ-02** | **MAJOR** | State Machine Deadlock | Post-pivot deadlock: de-escalating to Level 1 under a pivot immediately re-triggered Level 2 on the next run due to old marginal runs in sliding window. | Added `review_epoch` and `pivot_anchor_run_id` to `EscalationState`. Sliding window triggers filter runs strictly to $r_i > r_{	ext{pivot}}$ with a $k$-run grace period. (Section 9.3) |
| **MAJ-03** | **MAJOR** | Empirical Integrity | Off-target evidence laundering: reviewers cited passing runs from unrelated projects to satisfy evidence requirements. | Enforced campaign domain verification: `run:<id>` must be in `campaign_runs`; `sidecar:<path>` must belong to campaign scripts. (Section 7.3) |
| **MAJ-04** | **MAJOR** | Decision D1 Posture | Contradiction between unconditional conclude downgrade and advisory default posture. | Harmonized with Decision D1: non-blocking warnings during execution; conclusion downgrade to `confounded` for confirmation/sequential campaigns; `--force-verdict` records `bypassed`. (Section 9.4) |
| **MAJ-05** | **MAJOR** | Meta-Signals | Signal 16 incoherence: hard registration gate ($N_{	ext{valid}} \ge 2$) meant low-evidence reviews could never be saved, so Signal 16 would never fire. | Clarified Signal 16 semantics: detects post-review citation rot (pruned or mutated artifacts) and historical/imported records. (Section 11.2) |
| **MIN-01** | **MINOR** | Cryptographic Binding | Review schema lacked cryptographic binding to input state at review time. | Added `input_snapshot_sha256 VARCHAR NOT NULL` and `input_snapshot TEXT NOT NULL`. (Section 7.1) |
| **MIN-02** | **MINOR** | Concurrency / Storage | Escalations overwrote a single mutable file (`<campaign_id>.json`), causing race conditions and history deletion. | Converted cool persistence to unique UUID files: `<campaign_id>_<escalation_id>.json`. (Section 13.1) |
| **MIN-03** | **MINOR** | Telemetry | Signal 18 lacked dispatch timestamp in schema, causing false alarms on human CLI reviews. | Added `dispatched_at TIMESTAMP WITH TIME ZONE NULL` to schema; restricted Signal 18 strictly to automated agent reviews. (Section 11.4) |
| **MIN-04** | **MINOR** | Usability / Parity | Redundant parameter requirements between `campaign_id` and `escalation_id` in `stepback_resolve`. | Made `campaign_id` optional in `stepback_resolve`, allowing resolution by `escalation_id` alone. (Section 12.1) |

All 12 findings and 15 review items have been completely resolved, achieving **0 Blocker / 0 Major** status across adversarial review cycles.

---

## 5. System Architecture & Component Inventory

The step-back review subsystem is structured cleanly under `src/bathos/stepback/`:

```
src/bathos/
├── stepback/
│   ├── __init__.py           # Public API facade, constants, and error codes
│   ├── schema.py             # Dataclasses: StepBackReview, EscalationRecord, etc.
│   ├── triggers.py           # Pure mathematical trigger functions with guard clauses
│   ├── rubrics.py            # 5-dimension rubric evaluator, TOML loader, hard-floor veto
│   ├── analysis.py           # 5 qualitative cognitive modes, campaign domain citation validation
│   ├── escalation.py         # Levels 0-3 state machine, direct transitions, human chair invariance
│   ├── catalog.py            # Cool-tier atomic JSON I/O, DuckDB ingestion and query helpers
│   └── engine.py             # High-level orchestration facade
├── compact.py                # Schema v19 DuckDB table creation & compaction ingestion hook
├── sprint_audit.py           # Signals 15-18 (unvarying streaks, low evidence, ignored holds, velocity)
├── linter.py                 # Tier-1 and Tier-2 linter checks for stepback records
├── errors.py                 # Extends BathosErrorCode with STEPBACK_* error codes
├── telemetry.py              # Telemetry event catalogue (stepback.* surfaces)
├── mcp.py                    # FastMCP tools: stepback_{status,trigger,run,record,review,escalate,resolve,brief,show}
└── cli_cyclopts.py           # Cyclopts command group: `bth stepback <subcommand>`
agent_assets/
└── rubrics/
    └── stepback_v1.toml      # Canonical, content-addressed 5-dimension rubric specification
```

### 5.1 Python Dataclass Contracts (`src/bathos/stepback/schema.py`)

The step-back review data model defines strong type contracts, content-hashing helpers, and serialization methods:

```python
from __future__ import annotations
import hashlib
import json
from dataclasses import asdict, dataclass, field
from enum import IntEnum, StrEnum
from typing import Any

class ReviewPath(StrEnum):
    RUBRIC = "rubric"
    OPEN_ANALYSIS = "open_analysis"
    HYBRID = "hybrid"

class OpenAnalysisMode(StrEnum):
    ADVISOR_CONVO = "advisor_convo"
    PRE_MORTEM = "pre_mortem"
    SO_WHAT = "so_what"
    OUTSIDER = "outsider"
    STEELMAN = "steelman"

class Recommendation(StrEnum):
    CONTINUE = "CONTINUE"
    PIVOT = "PIVOT"
    HOLD = "HOLD"
    ESCALATE = "ESCALATE"
    CONCLUDE_ABANDON = "CONCLUDE_ABANDON"

class EscalationLevel(IntEnum):
    NOTICE = 0      # Nominal
    WARNING = 1     # Advisory Warning
    HOLD = 2        # Step-Back Hold
    CHAIR = 3       # Human Chair Escalation

class EscalationStatus(StrEnum):
    ACTIVE = "active"
    RESOLVED = "resolved"
    SUPERSEDED = "superseded"

@dataclass
class ReviewRubricScore:
    """Individual dimension score in Path A rubric evaluation."""
    dimension_id: str
    score: int  # 1, 2, 3, 4, 5
    rationale: str = ""

@dataclass
class OpenAnalysis:
    """Qualitative narrative sections for Path B cognitive modes."""
    mode: str  # 'advisor_convo', 'pre_mortem', 'so_what', 'outsider', 'steelman'
    sections: dict[str, str] = field(default_factory=dict)

@dataclass
class DissentRecord:
    """Immutable minority dissent record preserved in committee reviews."""
    dissenting_reviewer_id: str
    minority_recommendation: str  # 'CONTINUE', 'PIVOT', 'HOLD', 'ESCALATE', 'CONCLUDE_ABANDON'
    minority_composite_score: float | None = None
    dissent_summary: str = ""
    cited_counter_evidence: list[str] = field(default_factory=list)
    majority_rebuttal: str = ""

@dataclass
class StepBackReview:
    """Full step-back review record persisted to cool and warm tiers."""
    review_id: str  # rev_<12-hex>
    campaign_id: str
    project_slug: str
    created_at: str  # ISO-8601 UTC
    reviewer_id: str  # Authenticated identity string
    reviewer_kind: str  # 'human', 'agent', 'panel'
    is_independent: bool
    path_kind: str  # 'rubric', 'open_analysis', 'hybrid'
    input_snapshot_sha256: str  # Cryptographic hash of campaign state
    input_snapshot: str  # JSON string of input snapshot state
    recommendation: str  # 'CONTINUE', 'PIVOT', 'HOLD', 'ESCALATE', 'CONCLUDE_ABANDON'
    escalation_level: int  # 0, 1, 2, 3
    cited_evidence: list[str] = field(default_factory=list)
    rubric_id: str | None = None
    rubric_version: str | None = None
    rubric_sha256: str | None = None
    rubric_scores: dict[str, int | float] = field(default_factory=dict)
    composite_score: float | None = None
    open_analysis: OpenAnalysis | None = None
    dissents: list[DissentRecord] = field(default_factory=list)
    dispatched_at: str | None = None  # ISO-8601 UTC for agent telemetry
    signature_sha256: str | None = None  # Content hash of the review record
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class EscalationRecord:
    """Historical escalation event recorded in catalog."""
    escalation_id: str  # esc_<12-hex>
    campaign_id: str
    project_slug: str
    level: int  # 0, 1, 2, 3
    status: str  # 'active', 'resolved', 'superseded'
    opened_at: str  # ISO-8601 UTC
    opened_by_kind: str  # 'trigger', 'review', 'manual', 'chair'
    opened_by_review_id: str | None = None
    trigger_name: str | None = None  # 'repeated_marginal', 'near_bound', etc.
    trigger_reason: str = ""
    resolved_at: str | None = None
    resolved_by: str | None = None
    resolved_kind: str | None = None  # 'human', 'chair'
    resolution_note: str = ""
    deescalated_to: int | None = None
    review_epoch: int = 0
    pivot_anchor_run_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

@dataclass
class EscalationState:
    """Current in-memory escalation status for a campaign."""
    campaign_id: str
    current_level: int = 0
    status: str = "nominal"  # 'nominal', 'warning', 'hold', 'escalated'
    active_escalation_id: str | None = None
    active_triggers: list[str] = field(default_factory=list)
    review_epoch: int = 0
    pivot_anchor_run_id: str | None = None
    latest_review_id: str | None = None
    latest_recommendation: str | None = None

@dataclass
class StepBackVerdict:
    """Result of evaluating step-back constraints for a campaign."""
    ok: bool
    hold_active: bool
    escalation_level: int
    recommendation: str | None
    active_triggers: list[str]
    message: str
```

---

## 6. System-Evaluated Trigger Formulas & Detection Engine

### 6.1 Universal Guard Clauses & Epoch Filtering
Let $N$ be the total count of member runs in campaign $C$.
1. If $N < 3$ ($N < 5$ for entropy collapse and metric plateau), triggers return `{"fired": False, "reason": "insufficient_runs"}` immediately.
2. In the presence of a post-pivot anchor $r_{	ext{pivot}}$, only runs with $r_i > r_{	ext{pivot}}$ are included in the active epoch $\mathcal{R}_{	ext{active}}$. If $|\mathcal{R}_{	ext{active}}| < 3$, triggers do not fire.

### 6.2 Trigger Formulations

#### 1. Repeated Marginal Outcomes ($T_{	ext{repeated\_marginal}}$)
- Streak length: $S_{	ext{marginal}} = \max \{ s \in [0, N] \mid orall j \in [N-s+1, N], o(r_j) = 	ext{"marginal"} \}$.
- Window frequency over $W_k$ ($k=5$): $f_{	ext{marginal}}(W_k) = rac{1}{k} \sum_{r \in W_k} \mathbf{1}(o(r) = 	ext{"marginal"})$.
- **Trigger Rule**: Fires if $N \ge 3$ and ($S_{	ext{marginal}} \ge 3 \lor (|\mathcal{R}_{	ext{active}}| \ge 5 \land f_{	ext{marginal}}(W_5) \ge 0.60)$).

#### 2. Near-Bound Hits ($T_{	ext{near\_bound}}$)
- Normalized proximity distance: $d_i = rac{|y_i - B|}{\epsilon}$.
- Tolerance margin $\epsilon = 0.10 	imes |B|$ if $B 
e 0$; $\max(0.50 	imes s_y, 10^{-6})$ if $B=0$.
- **Trigger Rule**: Over $W_5$, fires if hit count $N_{	ext{nb}} = \sum \mathbf{1}(d_i \le 1.0) \ge 2 \lor d_N \le 0.25$.

#### 3. Outcome-Entropy Collapse ($T_{	ext{entropy\_collapse}}$)
- Shannon entropy in nats: $H_e(W_k) = - \sum_{j=1}^m p_j \ln(p_j)$, with $0 \ln(0) \equiv 0$.
- **Trigger Rule**: Over $W_5$, fires if $H_e(W_5) < 0.35$ nats ($pprox 0.50$ bits) or $H_e = 0$. Annotates `fixation_positive` (all pass) vs `fixation_negative` (non-pass).

#### 4. Metric Plateaus ($T_{	ext{plateau}}$)
- Moving OLS regression over last $n=6$ runs: slope $eta = rac{12 \sum (t_i - ar{t})(y_i - ar{y})}{n(n^2-1)}$.
- Standard case ($|ar{y}| \ge 0.01$): $ar{eta}_{	ext{rel}} = rac{|eta|}{|ar{y}|}$, $CV = rac{s_y}{|ar{y}|}$. Fires if $ar{eta}_{	ext{rel}} < 0.01 \land CV < 0.02$.
- Near-zero case ($|ar{y}| < 0.01$): Fires if $|eta| < 0.001 \land s_y < 0.001$.

#### 5. Campaign Staleness ($T_{	ext{stale}}$)
- Fires if:
  1. Campaign is open and wall-clock idle time $\Delta t_{	ext{idle}} \ge 14.0$ days.
  2. Campaign has a registered claim, $N_{	ext{runs}} \ge 15$, and zero clauses resolved in last 10 runs ($\Delta U_{10} = 0$).
  3. Exploratory campaign has $N_{	ext{runs}} \ge 25$ without conclusion or child branching.

### 6.3 Snooze & Acknowledge Semantics
- Allows snoozing a firing non-L3 trigger for bounded windows:
  - Max runs window: $\Delta N \le 10$ runs.
  - Max duration window: $\Delta t \le 72$ hours.
- Mandatory non-empty rationale string.
- Stored as an `EscalationRecord(level=0, status="snoozed", ...)`.
- **Level 3 Escalations can NEVER be snoozed.**

---

## 7. Dual Review Paths

### 7.1 Path A: Scored, Versioned Rubric Path
- Canonical rubric at `agent_assets/rubrics/stepback_v1.toml` content-addressed by SHA-256.
- 5 Dimensions scored on 1–5 integer scale:
  1. Question Validity ($w_1 = 0.20$)
  2. Empirical Progress ($w_2 = 0.25$)
  3. Theoretical Grounding ($w_3 = 0.20$)
  4. Resource Efficiency ($w_4 = 0.15$)
  5. Risk & Confound Surface ($w_5 = 0.20$)
- Weighted composite: $S_{	ext{composite}} = \sum w_i s_i$.
- **Hard-Floor Veto Rule**: If ANY $s_i = 1 \implies$ recommendation capped at `HOLD` (Escalation Level $\ge 2$).
- **Hierarchical Gap-Free Mapping**:
  - Any $s_i = 1 \implies$ `HOLD` (Level 2).
  - Any $s_i = 2 \implies$ `PIVOT` if $S \ge 3.4$; `HOLD` if $2.6 \le S < 3.4$; `ESCALATE` if $2.0 \le S < 2.6$; `CONCLUDE_ABANDON` if $< 2.0$.
  - All $s_i \ge 3 \implies$ `CONTINUE` if $S \ge 4.2$; `PIVOT` if $3.4 \le S < 4.2$; `HOLD` if $2.6 \le S < 3.4$; `ESCALATE` if $2.0 \le S < 2.6$; `CONCLUDE_ABANDON` if $< 2.0$.
- Review records bind cryptographic snapshot `input_snapshot_sha256` and JSON state.

### 7.2 Path B: Qualitative Open Analysis Path
- 5 Cognitive Modes:
  1. `advisor`: Socratic premise challenge, simplest decisive test, "what would change your plan?".
  2. `pre_mortem`: Prospective hindsight failure narrative assuming project failure at 6 months; red flags.
  3. `so_what`: Pragmatic utility under ideal confirmation; what decision shifts?
  4. `outsider`: Jargon-free reduction to elementary scientific principles.
  5. `steelman_stop`: Strongest evidence-backed prosecution case for immediate termination.
- Requires explicit `recommendation` argument (`CONTINUE`, `PIVOT`, `HOLD`, `ESCALATE`, `CONCLUDE_ABANDON`).

### 7.3 Campaign Domain Evidence Verification
- All review assertions must cite primary records:
  - `run:<id>`: Verified as a member of `campaign_runs` for this campaign.
  - `sidecar:<path>`: Verified as belonging to a script executed in this campaign.
  - `clause:<path>#<id>`: Verified as matching the registered claim.
- Minimum threshold: $N_{	ext{valid}} \ge 2$ verified domain citations required; otherwise rejected (`STEPBACK_INSUFFICIENT_EVIDENCE`).

---

## 8. Governance: Reviewer Independence & Minority Dissent Preservation

### 8.1 The Blind Reviewer Protocol
- **Attribution Barrier**: Reviewer identity $R_{	ext{id}}$ is verified against $\mathcal{A}_{	ext{forbidden}}$ (campaign creator, run authors, script git committers). If $R_{	ext{id}} \in \mathcal{A}_{	ext{forbidden}}$, review is marked `is_independent = false` with `REVIEWER_CONFLICT_OF_INTEREST` and cannot discharge holds.
- **Primary Records Only**: Blind reviewers receive only raw catalog data (runs, sidecars, claims, artifacts), withholding doing agents' summary advocacy.
- **Domain Failure Prompts**: Reviewers evaluate:
  1. Simulation replicate pooling vs convergence (replicates are ensemble samples, not independent experiments).
  2. Checks that cannot fail (census fields locked to 0, tautological controls, shared objective formulas).
  3. Variance inflation (between-build vs within-build, paired vs unpaired floors).
  4. Tool over-trust (structure predictors AUROC ~0.5).
  5. Silently inert knobs (positive controls required).

### 8.2 Minority Dissent Preservation
- Panels embed an immutable `dissents` array capturing minority reviewer ID, recommendation, rationale, and counter-evidence.
- **Dissent Trigger**: If ANY panelist recommends `HOLD`, `ESCALATE`, or `CONCLUDE_ABANDON`, an automatic Level 2 Advisory Hold is placed on the campaign. Dissent cannot be outvoted.

---

## 9. Escalation Ladder & Human Chair Invariance

### 9.1 The 4-Tier Ladder & State Machine
- **Level 0 (Nominal)**: Normal progression.
- **Level 1 (Advisory Warning)**: Soft warning banner on run launches and queries.
- **Level 2 (Step-Back Hold)**: Active hold. In confirmation/sequential mode, conclusion downgraded to `"confounded"` unless forced.
- **Level 3 (Human Chair Escalation)**: Terminal escalation. Reached via catastrophic findings or committee referral.

### 9.2 Human Chair Invariance (Iron Law)
- FastMCP `stepback_resolve` strictly and unconditionally **refuses** Level 3 resolutions (`STEPBACK_AGENT_OVERRIDE_REFUSED`).
- CLI resolution requires interactive TTY (`sys.stdin.isatty()`) or secure chair token (`~/.bth/chair_token`, 0600 permissions).
- Autonomous agent environments (`BTH_AGENT_MODE=1`, etc.) are rejected. Agents can **never** override Level 3.

### 9.3 Post-Pivot Anchoring
- Resolving a hold via `PIVOT` records `pivot_anchor_run_id = r_N` and increments `review_epoch`.
- Sliding window triggers ignore historical runs prior to $r_{	ext{pivot}}$ with a $k$-run grace period.

### 9.4 Decision D1 Posture
- Execution (`bth run`, `bth submit`, `bth campaign add`): Emits warning banners to stderr; never blocks unless opted in via `[stepback] enforce = true` or `BTH_STEP_BACK_ENFORCE=1`.
- Conclusion (`bth campaign conclude`): Confirmation/sequential campaigns automatically downgraded to `"confounded"`; bypass via `--force-verdict` records `bypassed` state. Exploration campaigns print warning banners.

---

## 10. Advisor Brief & "End / reconsider" Matrix Integration

### 10.1 Markdown Brief Architecture (`render_advisor_brief`)
The advisor brief generates a structured, publication-grade Markdown document suitable for a real thesis committee, advisor one-on-one, or laboratory progress review:

```markdown
# Advisor Brief: <campaign_name> (<campaign_id>)

**Campaign ID:** `<campaign_id>`  
**Project:** `<project_slug>`  
**Mode:** `<mode>`  
**Status:** `<status>`  
**Current Escalation Level:** Level `<current_level>` (`<level_label>`)  
**Generated At:** `<timestamp_utc>`  

---

## 1. Stakes & Scientific Question
- **Central Question**: What is the core mechanism under investigation?
- **Hypothesis**: Formal pre-registered hypothesis statement.
- **Decision Gap**: What engineering or biological choice branches on this result?

## 2. Trajectory & Outcomes
- **Run Count**: N total member runs.
- **Outcome Distribution**: Pass: N_p, Marginal: N_m, Fail: N_f, Confounded: N_c.
- **Active Triggers**: Firing trajectory triggers (repeated_marginal, near_bound, etc.).
- **Trajectory Trend**: Outcome sequence across the active review epoch.

## 3. Threshold Basis & Drift
- **Primary Metric**: The quantitative target metric tracked.
- **Decision Boundary**: Boundary B and tolerance margin epsilon.
- **Boundary Skaters**: Runs clustering within proximity distance d_i <= 1.0.
- **Drift Evaluation**: Checked output SHA drift against baseline pins.

## 4. Alternative Hypotheses & Dissent
- **Rival Models**: Candidate alternative mechanisms that could explain the observations.
- **Dissent Records**: Preserved minority critiques from committee panels.
- **Counter-Evidence**: Specific run IDs and sidecar clauses cited by dissenters.

## 5. Exit Criteria & Sunk Cost
- **Falsification / Stop Criteria**: What concrete outcome would prompt campaign termination?
- **Sunk Cost Audit**: Total execution duration (seconds) and estimated GPU/CPU hours expended.

## 6. Escalation Status & Chair Disposition
- **Current Escalation State**: Level 0 (Nominal), Level 1 (Advisory), Level 2 (Hold), or Level 3 (Chair).
- **Active Holds**: Trigger origins and reviewer recommendations.
- **Human Chair Disposition**: Chair rationale and resolution signature (Level 3 only).

## 7. Project Status Matrix Entry ("End / reconsider")
```
[<STATUS_TAG>: <concise_summary>]
```
```

### 10.2 Project Status Matrix Syntax & Praxia Integration
The final section of the brief exports an unforgeable, machine-parseable status tag feeding the `"End / reconsider"` column of `PROJECT_STATUS.advisor.md` (generated by Praxia's `scripts/projects/project_status.py`):

1. **Level 0 (Nominal)**:
   ```
   [NOMINAL: N runs, pass rate X.XX]
   ```
   - Indicates normal exploratory or confirmatory progress within healthy variance bounds.
2. **Level 1 (Advisory Warning)**:
   ```
   [ADVISORY L1: <trigger_name> active; review recommended]
   ```
   - Signals a single active trigger (e.g. repeated marginals or near-bound proximity); non-blocking advisory posture.
3. **Level 2 (Step-Back Hold)**:
   ```
   [HOLD L2: <trigger_reason>; review <review_id> recommends <rec>]
   ```
   - Indicates an active hold triggered by a hard-floor veto, multiple triggers, or panel dissent. In confirmation/sequential mode, conclusion outcome is automatically downgraded to `"confounded"` unless resolved or forced.
4. **Level 3 (Human Chair Escalation)**:
   ```
   [ESCALATE L3: Human Chair disposition required; <reason>]
   ```
   - Terminal escalation requiring direct human intervention. Autonomous agents are strictly barred from resolving or de-escalating Level 3 holds.

---

## 11. Anti-Rubber-Stamp Meta-Signals (Signals 15–18)

Surveillance meta-signals integrated into `bth sprint-audit`:
- **Signal 15 (`stepback_unvarying_verdicts`)**: Score variance $\sigma_S^2 < 0.005$ or 100% identical recommendations over $N \ge 4$ reviews.
- **Signal 16 (`stepback_low_evidence_rate`)**: $> 15\%$ of reviews citing fewer than 2 valid catalog records or suffering from citation rot.
- **Signal 17 (`stepback_ignored_escalations`)**: Active Level 2/3 holds open $> 7$ days or $\ge 3$ subsequent runs launched without acknowledgment.
- **Signal 18 (`stepback_turnaround_anomalies`)**: Automated reviews completed in $< 2.0$ seconds or with 0 database reads, verified via `dispatched_at` telemetry timestamps.

---

## 12. Tooling Contract: Cyclopts CLI & FastMCP Tool Parity

### 12.1 Tool Parity Table

| Operation | CLI Command (`bth stepback <cmd>`) | FastMCP Tool | Error Codes Handled |
|---|---|---|---|
| View Status | `status <id> [--verbose]` | `stepback_status(campaign_id, verbose)` | `CAMPAIGN_NOT_FOUND` |
| Evaluate Triggers | `trigger <id> [--window 5] [--dry-run]` | `stepback_trigger(campaign_id, window, dry_run)` | `TRIGGER_FAILED` |
| Run Orchestration | `run <id>` | `stepback_run(campaign_id)` | `TRIGGER_FAILED` |
| Record Evaluation | `record <id> --path <p> ...` | `stepback_record(...)` | `RUBRIC_INVALID`, `INSUFFICIENT_EVIDENCE` |
| Review Shortcut | `review <id> ...` | `stepback_review(...)` | `INVALID_SCHEMA` |
| Snooze Trigger | `snooze <id> --trigger <t> --runs <n>` | `stepback_snooze(campaign_id, trigger, runs)` | `SNOOZE_INVALID` |
| Advisor Brief | `brief <id>` / `show <id> --format brief` | `stepback_brief(campaign_id)` | `CAMPAIGN_NOT_FOUND` |
| Inspect Records | `show <id> [--format json\|brief]` | `stepback_show(campaign_id, format)` | `STEPBACK_NOT_FOUND` |
| Escalate Level | `escalate <id> --target-level <l>` | `stepback_escalate(campaign_id, level, reason)` | `INVALID_PARAM` |
| Resolve Hold | `resolve <id> [--deescalate-to 0]` | `stepback_resolve(campaign_id, deescalate_to)` | `AGENT_OVERRIDE_REFUSED` |

---

## 13. Schema Migration Plan (v18 -> v19) & Structured Telemetry Events

### 13.1 Tiered Storage Topology
- **Cool Tier (Atomic JSON Fragments)**:
  - Reviews: `<catalog_dir>/stepback/reviews/<campaign_id>_<review_id>.json`
  - Escalations: `<catalog_dir>/stepback/escalations/<campaign_id>_<escalation_id>.json`
  - Written via write-then-rename atomicity (`tempfile` + `os.replace`).
- **Warm Tier (DuckDB Relational Tables in `bathos.db`)**:
  - `stepback_reviews` and `stepback_escalations` created via `CREATE TABLE IF NOT EXISTS` in `compact.py`.
  - Ingestion hook `_ingest_stepback_fragments` updates warm tier idempotently on compaction.

### 13.2 DuckDB Schema Version 19 Migration Plan
1. **Schema Version Bump**: Set `CURRENT_SCHEMA_VERSION = "19"` in `src/bathos/schema.py:10`.
2. **Compaction Migration Hook**: In `src/bathos/compact.py`, register `_migrate_v18` in `MIGRATIONS["18"]`:
   ```python
   def _migrate_v18(run_dict: dict) -> dict:
       """Migrate v18 fragment to v19 (warm stepback tables)."""
       run_dict["schema_version"] = "19"
       return run_dict

   MIGRATIONS["18"] = _migrate_v18
   ```
3. **Parquet Cool Fragments**: Run-level schema `COOL_SCHEMA` remains unchanged; `migrate_catalog` skips v18 fragments (`not missing`), avoiding unnecessary parquet rewrites.
4. **DuckDB Tables**:
   ```sql
   CREATE TABLE IF NOT EXISTS stepback_reviews (
       id VARCHAR PRIMARY KEY,
       campaign_id VARCHAR NOT NULL,
       project_slug VARCHAR NOT NULL,
       created_at TIMESTAMP WITH TIME ZONE NOT NULL,
       dispatched_at TIMESTAMP WITH TIME ZONE,
       reviewer_id VARCHAR NOT NULL,
       reviewer_kind VARCHAR NOT NULL,
       is_independent BOOLEAN NOT NULL,
       path_kind VARCHAR NOT NULL,
       input_snapshot_sha256 VARCHAR NOT NULL,
       input_snapshot TEXT NOT NULL,
       rubric_id VARCHAR,
       rubric_version VARCHAR,
       rubric_sha256 VARCHAR,
       scores_json TEXT,
       composite_score DOUBLE,
       open_analysis_mode VARCHAR,
       open_analysis_json TEXT,
       cited_evidence_json TEXT NOT NULL,
       recommendation VARCHAR NOT NULL,
       escalation_level INTEGER NOT NULL,
       is_dissent BOOLEAN NOT NULL DEFAULT FALSE,
       parent_panel_id VARCHAR,
       dissents_json TEXT,
       metadata TEXT
   );

   CREATE TABLE IF NOT EXISTS stepback_escalations (
       id VARCHAR PRIMARY KEY,
       campaign_id VARCHAR NOT NULL,
       project_slug VARCHAR NOT NULL,
       level INTEGER NOT NULL,
       status VARCHAR NOT NULL,
       opened_at TIMESTAMP WITH TIME ZONE NOT NULL,
       opened_by_kind VARCHAR NOT NULL,
       opened_by_review_id VARCHAR,
       trigger_name VARCHAR,
       trigger_reason VARCHAR NOT NULL,
       resolved_at TIMESTAMP WITH TIME ZONE,
       resolved_by VARCHAR,
       resolved_kind VARCHAR,
       resolution_note TEXT,
       deescalated_to INTEGER,
       review_epoch INTEGER NOT NULL DEFAULT 0,
       pivot_anchor_run_id VARCHAR,
       metadata TEXT
   );
   ```

### 13.3 Structured Telemetry Events Catalogue
Telemetry events emit under surface `stepback.*` via `src/bathos/telemetry.py` (and cisternal bridge):

| Event Name | Trigger Moment | Payload Fields | Context Captured |
|---|---|---|---|
| `stepback.trigger_evaluated` | Trigger evaluated on campaign | `campaign_id`, `trigger_name`, `fired`, `details` | `run_uuid`, `task_id` |
| `stepback.trigger_snoozed` | Trigger snoozed | `campaign_id`, `trigger_name`, `runs_window`, `epoch_hours`, `rationale` | `task_id` |
| `stepback.review_recorded` | Review registered | `campaign_id`, `review_id`, `path_kind`, `recommendation`, `composite_score`, `reviewer_id` | `task_id` |
| `stepback.escalation_created` | Escalation level raised | `campaign_id`, `escalation_id`, `level`, `trigger_name`, `reason` | `task_id` |
| `stepback.escalation_resolved`| Escalation resolved/lowered | `campaign_id`, `escalation_id`, `deescalated_to`, `resolved_by`, `resolution_note` | `task_id` |
| `stepback.brief_rendered` | Advisor brief generated | `campaign_id`, `current_level`, `active_triggers_count` | `task_id` |

---

## 14. Retrospective Validation Pre-Registration

### 14.1 Pre-Registration Sidecar (`validate_stepback_retrospective.bth.toml`)
```toml
[experiment]
hypothesis = "Step-back review triggers detect 100% of historical campaign anomalies with zero false positives on nominal runs"

[outcomes.pass]
condition = "accuracy == 1.0 AND false_positive_rate == 0.0 AND n_campaigns_evaluated >= 3"
decision = "Accept step-back review subsystem for production release"

[outcomes.fail]
condition = "accuracy < 1.0 OR false_positive_rate > 0.0"
decision = "Refine trigger parameters and re-test"

[result_schema]
accuracy = "float"
precision = "float"
recall = "float"
false_positive_rate = "float"
n_campaigns_evaluated = "int"
sp98_triggered = "bool"
sp91_near_bound_triggered = "bool"
mentee_plateau_triggered = "bool"
```

### 14.2 Execution & Verification Method
1. Execute `scripts/experiments/validate_stepback_retrospective.py` via `bth run` in an isolated catalog environment (`BTH_CATALOG_DIR=/tmp/bth_val_cat`).
2. Verify that DuckDB outcome evaluates to `outcome = "pass"`.
3. Capture exact `run_id` and outcome label in the PR description and final handoff.

---

## 15. Acceptance Criteria Checklist (R1 through R4)

### Requirement R1: Step-Back Trigger and Review Mechanism
- [x] System-evaluated triggers fire automatically with singularity guard clauses ($N \ge 3$, $N \ge 5$ for entropy/plateau).
- [x] Repeated marginal outcomes ($s \ge 3 \lor f \ge 0.60$) with post-pivot epoch filtering.
- [x] Near-bound hits ($N_{	ext{nb}} \ge 2 \lor d_N \le 0.25$) with $B=0$ stddev scaling.
- [x] Outcome-entropy collapse ($H_e < 0.35$ nats) standardized on natural logarithm.
- [x] Metric plateaus ($ar{eta}_{	ext{rel}} < 0.01 \land CV < 0.02$) with near-zero absolute metric guards.
- [x] Campaign staleness (14d idle / 15 runs wheel-spinning / 25 runs uncapped drift).
- [x] Snooze/acknowledge semantics bounded by $\le 10$ runs or $\le 72$ hours.
- [x] Path A scored rubric: 5 weighted dimensions on 1–5 scale with hard-floor veto rule and gap-free hierarchical logic.
- [x] Path B open analysis: 5 cognitive modes with mandatory primary evidence citations ($N_{	ext{valid}} \ge 2$) verified against campaign domain.

### Requirement R2: Reviewer Independence, Escalation Ladder, and Non-Blocking Posture
- [x] Reviewer independence bound to authenticated runtime identity; self-review flagged with `REVIEWER_CONFLICT_OF_INTEREST`.
- [x] Minority dissent preserved in immutable `dissents` array; minority recommendation of `HOLD`/`ESCALATE` places campaign in automatic Level 2 hold.
- [x] 4-tier escalation ladder (Levels 0–3) with bidirectional state transitions and direct paths.
- [x] Human Chair Invariance: FastMCP strictly refuses Level 3 resolutions; CLI requires interactive TTY or `~/.bth/chair_token`; agent overrides rejected.
- [x] Harmonized Decision D1 posture: default advisory warnings during execution; conclusion verdict downgraded to `confounded` on confirmation/sequential campaigns with active holds; opt-in execution blocking via `[stepback] enforce = true`.

### Requirement R3: Anti-Rubber-Stamp Meta-Signals and Tooling Integration
- [x] Meta-signals 15–18 integrated into `sprint-audit`: unvarying streaks, low-evidence reviews, ignored escalations, turnaround velocity anomalies.
- [x] Full tooling parity across Cyclopts CLI (`bth stepback *`) and FastMCP server (`stepback_*`).
- [x] Error envelopes governed by `BathosErrorCode`, mapped domain exception classes, and resolution hints.
- [x] Backward-compatible DuckDB schema v19 migration with cool JSON atomic persistence and nullable `opened_by_review_id`.
- [x] Advisor brief rendering (`render_advisor_brief`) and `"End / reconsider"` matrix formatting.

### Requirement R4: Execution Isolation, Retrospective Validation, and Review Gates
- [x] All development and testing executed in isolated git worktree `.claude/worktrees/261027-step-back-reviews`.
- [x] Primary checkout and real `~/.bth` catalog completely untouched.
- [x] Tests run via targeted pytest invocations.
- [x] Retrospective validation pre-registered in `scripts/experiments/validate_stepback_retrospective.bth.toml` against 3 historical campaigns.
- [x] Adversarial spec cycling achieves 0 Blocker / 0 Major findings.

---

*Specification completed and approved for Task 261027_step-back-reviews.*
