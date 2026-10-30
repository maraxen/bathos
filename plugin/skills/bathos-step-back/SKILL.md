---
name: bathos-step-back
description: System-evaluated step-back reviews, 5-dimension rubric (Path A), 5 cognitive modes (Path B), blind reviewer protocol, and advisor committee escalation ladder for bathos campaigns
when_to_use: "Triggers: stepback, step-back review, bth stepback, advisor brief, committee escalation, campaign hold, rubric evaluation, pre-mortem, outsider read, steelman-the-stop, blind reviewer, repeated_marginal, near_bound, entropy_collapse, metric_plateau, campaign staleness, decision D1."
---
# bathos-step-back

Systematized advisor / committee review and trajectory escalation for bathos campaigns. Assumes familiarity with campaign organization in **bathos-campaigns** and rigor gates in **bathos-rigor-gates**.

## Core Philosophy: Outward & Upward

`bathos` provides extensive tools that look **inward and downward**:
- Pre-registration and sidecars (`*.bth.toml`)
- DuckDB SQL outcome conditions
- The claim-tier Union Gate (`claim.bth.toml`)
- Confounds, differential pre-flights, and post-mortems

All of these verify whether *this specific run is valid* and whether *this specific claim is supported*. However, during the grind of an exploratory or confirmatory campaign, an autonomous agent (or human researcher) can execute sprint after sprint of green, pre-registered, perfectly valid runs while:
1. Chasing a question that has ceased to matter, or that no real decision depends on.
2. Creeping toward an arbitrary numerical threshold with diminishing returns (sunk cost fallacy).
3. Quietly reframing the registered question or optimizing a proxy metric (Goodhart's Law).
4. Ignoring a cheaper, more discriminating experiment or published literature that already answers it.
5. Spending GPU/CPU budget that another project in the portfolio needs more urgently.

**Step-Back Reviews** provide the **outward and upward** check: periodic, partly structured, partly conversational checkpoints conducted by independent reviewers who did not perform the work, asking:
- *"What is the central question, and what gap does it fill?"*
- *"What would change your plan?"*
- *"What does each alternative hypothesis predict?"*
- *"Is this line of work still worth pursuing?"*

---

## Architecture & Lifecycle

```
Campaign Run Stream
      │
      ▼
System Event Triggers (triggers.py)
[repeated_marginal | near_bound | entropy_collapse | metric_plateau | stale]
      │
      ├──────────────────────┬──────────────────────┐
      ▼                      ▼                      ▼
Level 0: Clean         Level 1: Advisory      Level 2: Step-Back Hold (min score=1)
                             │                      │
                             ▼                      ▼
                  Independent Blind Reviewer Dispatched
                  ┌─────────────────────────────────┐
                  │ Path A: 5-Dimension Scored      │
                  │         Rubric (Hard Floor Veto)│
                  │ Path B: Qualitative Mode        │
                  │         (Advisor / Pre-Mortem)  │
                  └─────────────────────────────────┘
                                    │
                                    ▼
                         Escalation State Machine
                    ┌───────────────┴───────────────┐
                    ▼                               ▼
            Level 2 Hold Resolved          Level 3 Human Chair
            (Pivot Anchor Set)             (Iron Law: No Agent Override)
```

1. **Detection**: Pure mathematical triggers evaluate recent run trajectories with rigorous guard clauses against sparse data ($N < 3$) and post-pivot deadlocks.
2. **Review**: Dual paths produce durable, provenanced JSON records in cool storage (`<catalog_dir>/stepback/reviews/`) and DuckDB relational tables (`stepback_reviews` in schema v19).
3. **Escalation**: A 4-level state machine governs campaign status, terminating at an inviolable Human Chair gate.
4. **Action Posture (Decision D1)**: Non-blocking execution warnings by default; conclusion verdict downgraded to `"confounded"` for confirmation/sequential campaigns; opt-in execution blocking via `[stepback] enforce = true`.
5. **Surveillance**: Automated meta-signals (Signals 15–18 in `sprint-audit`) monitor review velocity, evidence density, and unvarying verdict streaks.

---

## 1. System Triggers

Step-back triggers run on system event hooks (after runs complete in `runner.py`, during `bth run` / `bth submit`, or during `bth campaign review`). They evaluate trajectory properties without relying on human or agent memory.

### Universal Preconditions & Guards
- **Minimum Run Count Guard**: If campaign run count $N < 3$ ($N < 5$ for entropy and plateau), triggers return `{"fired": False, "reason": "insufficient_runs"}` immediately.
- **Post-Pivot Epoch Guard**: When a campaign resolves a hold with an approved `PIVOT`, subsequent triggers filter member runs to $r_i > r_{\text{pivot}}$ with a $k$-run grace period, preventing historical marginal runs from deadlocking the new trajectory.

### The Five Triggers

| Trigger Name | Mathematical Condition | Default Parameters | Failure Mode Detected |
|---|---|---|---|
| `repeated_marginal` | Streak $s \ge s_{\text{thresh}} \lor (N \ge k \land f \ge \theta)$ | $s = 3$, $k = 5$, $\theta = 0.60$ | Hyperparameter-tweaking loops; stuck in suboptimal basins. |
| `near_bound` | Hits $N_{\text{nb}} \ge m \lor d_N \le 0.25$ | $m = 2$, $k = 5$, margin $\epsilon = 0.10 \times \|B\|$ | Fragile results skating right at pass/fail decision boundaries. |
| `entropy_collapse` | Shannon entropy $H_e < \tau_{\text{entropy}}$ (nats) | $\tau = 0.35$ nats over $k \ge 5$ runs | Rut exploration; 90%+ concentration in a single outcome label. |
| `metric_plateau` | OLS relative slope $\bar{\beta}_{\text{rel}} < \delta \land CV < \delta_{CV}$ | $n = 6$, $\delta = 0.01$ (1%/run), $\delta_{CV} = 0.02$ | Diminishing returns; flat progress with dead variance. |
| `stale` | Idle time $> T_{\text{days}} \lor$ wheel-spinning $> N_{\text{stall}}$ | $T = 14$ days, $N = 15$ runs with $\Delta \text{clauses} = 0$ | Abandoned campaigns or active compute burn with zero hypothesis progress. |

### Trigger CLI & MCP
```bash
# Evaluate triggers manually
bth stepback trigger <campaign-id>
bth stepback trigger <campaign-id> --window 5 --dry-run
```
MCP: `stepback_trigger(campaign_id="<campaign-id>", window=5, dry_run=False)`.

### Snooze & Acknowledge Semantics
When an intentional sweep triggers a non-L3 warning, acknowledge and snooze it:
```bash
bth stepback snooze <campaign-id> --trigger repeated_marginal --runs 5 --rationale "Sweeping Langevin gamma grid"
```
MCP: `stepback_snooze(campaign_id="<campaign-id>", trigger_name="repeated_marginal", runs_window=5, rationale="...")`.
- Max run window: 10 runs (`--runs 10`). Max duration: 72 hours (`--hours 72`).
- Rationale is mandatory.
- **Level 3 Escalations can NEVER be snoozed.**

---

## 2. Review Path A: Scored, Versioned Rubric

Path A provides a longitudinally comparable evaluation scored across 5 weighted dimensions on an integer 1–5 scale. The specification is defined in `agent_assets/rubrics/stepback_v1.toml` and content-addressed by SHA-256.

### The Five Dimensions

1. **Question Validity ($w_1 = 0.20$)**:
   - 1: Hypothesis is unfalsifiable, already conclusively answered in literature, or no decision depends on it.
   - 3: Valid exploratory question, but downstream actionability is ambiguous.
   - 5: Crisp falsifiable hypothesis with high discriminability; binary branch point in project plan.
2. **Empirical Progress ($w_2 = 0.25$)**:
   - 1: Deadlock; repeated marginals with no parameter-space movement; zero hypotheses resolved in $>10$ runs.
   - 3: Moderate progress; incremental signal amidst high variance.
   - 5: Clear trajectory; hypothesis confirmed or rejected with robust statistical separation.
3. **Theoretical Grounding ($w_3 = 0.20$)**:
   - 1: Empirical curve-fitting disconnected from mechanism; contradicts core physical/domain constraints.
   - 3: Plausible mechanism, but key assumptions remain unverified.
   - 5: Direct mechanistic model; predictions derived from first principles or established theory.
4. **Resource Efficiency ($w_4 = 0.15$)**:
   - 1: Disproportionate token/GPU burn; expensive assays used where simple analytical checks suffice.
   - 3: Acceptable efficiency; minor redundant sampling.
   - 5: Minimal decisive experiments; aggressive compute pruning and early exit.
5. **Risk & Confound Surface ($w_5 = 0.20$)**:
   - 1: Fatal unmitigated confound; data leakage; tautological controls; replicate pooling conflated with independent samples.
   - 3: Known confounds partially controlled; sensitivity analysis incomplete.
   - 5: Comprehensive controls; positive/negative controls validated; no hidden degrees of freedom.

### The Hard Floor Veto Invariant
Standard weighted averaging allows compensatory masking: a project with fatal data leakage ($s_5 = 1$) could score $s = [5, 5, 5, 5, 1] \implies S = 4.2$ (`CONTINUE`).

**Veto Rule**: If **ANY** dimension receives a score of $s_i = 1$:
- The recommendation is **strictly capped at `HOLD`** (or lower).
- The campaign is **forced to Escalation Level $\ge 2$**.
- Compensatory masking is mathematically impossible.

### Hierarchical Gap-Free Recommendation Logic

```python
if any(s == 1 for s in scores.values()):
    recommendation = "HOLD"  # Hard floor veto (Escalation Level >= 2)
elif any(s == 2 for s in scores.values()):
    if composite >= 3.4:
        recommendation = "PIVOT"
    elif composite >= 2.6:
        recommendation = "HOLD"
    elif composite >= 2.0:
        recommendation = "ESCALATE"
    else:
        recommendation = "CONCLUDE_ABANDON"
else:  # All dimensions s >= 3
    if composite >= 4.2:
        recommendation = "CONTINUE"
    elif composite >= 3.4:
        recommendation = "PIVOT"
    elif composite >= 2.6:
        recommendation = "HOLD"
    elif composite >= 2.0:
        recommendation = "ESCALATE"
    else:
        recommendation = "CONCLUDE_ABANDON"
```

### Recording Path A Reviews
```bash
bth stepback record <campaign-id> \
  --path rubric \
  --scores '{"question_validity": 4, "empirical_progress": 2, "theoretical_grounding": 4, "resource_efficiency": 3, "risk_confound_surface": 3}' \
  --evidence '["run:run_1234abcd", "sidecar:scripts/experiments/opt.bth.toml#outcomes.pass"]' \
  --reviewer-id "critic_agent_1"
```

---

## 3. Review Path B: Structured Open Analysis

Path B captures unstructured, qualitative insights that scalar rubrics cannot anticipate, organized into 5 cognitive inquiry modes.

### The Five Cognitive Modes

1. **`advisor` (Advisor Conversation)**:
   - Socratic interrogation: *"What is the central question, and what gap does it fill?"*, *"What alternative hypotheses could explain this?"*, *"What would change your plan?"*.
2. **`pre_mortem` (Prospective Hindsight Pre-Mortem)**:
   - Assume it is 6 months in the future and this campaign failed catastrophically. Write the post-mortem narrative. What were the early warning signs visible in the data today? (Klein 2007).
3. **`so_what` (The So-What Test)**:
   - Assume every experiment in this campaign passes cleanly. What scientific or practical decision actually changes? If the answer is "none" or "we run another sweep", the campaign lacks impact.
4. **`outsider` (Outsider Read)**:
   - Jargon-free reduction to elementary scientific principles. Strips away model acronyms and parameter names. Would a competent scientist outside this subfield find this argument compelling?
5. **`steelman_stop` (Steelman-The-Stop)**:
   - Act as an aggressive prosecutor making the strongest possible evidence-backed argument for terminating this campaign immediately.

### Mandatory Primary Evidence Citation
Path B reviews **cannot** consist of unsupported prose or summary recitation. Every assertion must cite verifiable primary entities in the catalog:
- `run:<uuid>` (must be a valid member run of the campaign)
- `sidecar:<path>#<clause>` (must belong to a script executed by the campaign)
- `clause:<path>#<id>` (must belong to the registered claim file)
- `postmortem:<id>` or `git:<sha>`

**Verification Gate**: At least 2 verified primary citations ($N_{\text{valid}} \ge 2$) are required. Unresolvable citations cause review submission to fail with `STEPBACK_INSUFFICIENT_EVIDENCE`.

### Recording Path B Reviews
```bash
bth stepback record <campaign-id> \
  --path open_analysis \
  --mode pre_mortem \
  --recommendation PIVOT \
  --text "If this campaign fails, it will be due to treating simulation replicates as independent experiments. In run_1234, between-replica spread was used as uncertainty, inflating confidence by 5.7x." \
  --evidence '["run:run_1234abcd", "sidecar:scripts/experiments/md.bth.toml#outcomes.marginal"]' \
  --reviewer-id "critic_agent_1"
```

---

## 4. The Blind Reviewer Protocol

A primary finding in research grounding (§5, §8) is that **mechanizing a lesson moves the failure mode**. Agents game visible gates by inventing arbitrary passes or writing checks that are their own negation. When an agent reviews its own work, confirmation bias and proposer advocacy dominate (Praxia lesson #246).

### 1. Attribution Barrier & Independence Check
- Reviewer identity (`reviewer_id`) is verified against the campaign author set:
  $$\mathcal{A}_{\text{forbidden}} = \{\text{creator}, \text{run authors}, \text{git committers during campaign window}\}$$
- If $R_{\text{id}} \in \mathcal{A}_{\text{forbidden}}$, the review is marked `is_independent = false`, emits `REVIEWER_CONFLICT_OF_INTEREST`, and **cannot** satisfy escalation exit conditions or discharge holds.

### 2. Blind Evaluation (Primary Records Only)
- The blind reviewer **does not receive** the doing agent's summary report, chat logs, or narrative rationalizations.
- The reviewer receives only the raw, primary records:
  - Member run parameters, outputs, and timestamps.
  - Sidecar TOML definitions and evaluated SQL conditions.
  - Claim files and literature citations.
  - Output artifacts and figures.

### 3. Protein Science Domain Diagnostic Inquiries
Reviewers are prompted to test for known recurring domain errors (§9, §10):
- **Simulation Replicate Pooling vs Convergence**:
  - Replicates are partial samples of a single thermodynamic ensemble. They must be **pooled/concatenated**, not treated as independent experiments that must each converge.
  - The reviewer explicitly asks: *"What is the unit of independence here, and what does pooling assume?"*
- **Checks That Cannot Fail or Discriminate**:
  - Census fields fixed to 0; controls that pass unconditionally; metrics sharing formulas with the training objective (trusted-broken-pipeline).
- **Variance Inflation & Mismatched Noise Floors**:
  - Between-build vs within-build variance; paired effects tested against unpaired floors.
- **Over-Trusting Tool Scores**:
  - Structure predictor scores (AUROC ~0.5 on designs); fold confidence tracking alignment composition rather than structure quality.
- **Silently Inert Gates or Knobs**:
  - Null results accepted without positive controls demonstrating the knob actually moves the output.

### 4. Preservation of Minority Dissent
- Review records embed an immutable `dissents` array capturing minority reviews.
- If ANY panelist recommends `HOLD`, `ESCALATE`, or `CONCLUDE_ABANDON`, an automatic Level 2 Advisory Hold is placed on the campaign. Dissent can never be outvoted.

---

## 5. Escalation Ladder & Human Chair Invariance

The escalation state machine manages campaign governance across four levels:

| Level | Name | Trigger / Condition | Consequence | Resolution Authority |
|---|---|---|---|---|
| **0** | Clean (Nominal) | Default starting state. | Normal execution. | Automatic |
| **1** | Advisory Warning | 1 trigger active; or advisory review. | Warning banner on run/query. | Independent Reviewer / Agent |
| **2** | Step-Back Hold | Hard floor veto ($s_i = 1$); multiple triggers; marginal streak $s \ge 3$; minority dissent; Path B `HOLD`/`PIVOT`. | Warning banner during run; **conclude verdict downgraded to "confounded"**. | Independent Reviewer / Panel |
| **3** | Human Chair Escalation | Catastrophic flaw; data corruption; panel referral; ignored Level 2 hold $> 7$ days. | Mandatory hold; conclude blocked/confounded. | **Human Chair ONLY** |

### Human Chair Invariance (Iron Law)
An autonomous agent can **NEVER** close, resolve, or de-escalate a Level 3 escalation.
- FastMCP tool `stepback_resolve` **unconditionally refuses** Level 3 resolutions (`STEPBACK_AGENT_OVERRIDE_REFUSED`).
- CLI `bth stepback resolve` requires an interactive TTY session (`sys.stdin.isatty()`) or a secure human chair token (`~/.bth/chair_token`, 0600 permissions).
- Any environment containing agent session markers (`BTH_AGENT_MODE=1`, `CLAUDE_AGENT`, etc.) is rejected.

### Decision D1 Posture
Step-back reviews recommend rather than act:
- **`bth run` / `bth submit`**: Emits a prominent warning banner to stderr. Never blocks unless opted in via `[stepback] enforce = true` or `BTH_STEP_BACK_ENFORCE=1`.
- **`bth campaign conclude`**: For confirmation and sequential campaigns with an active Level 2 or 3 hold, the conclusion outcome is automatically downgraded to `"confounded"` unless `--force-verdict` / `--force` is supplied.

---

## 6. Advisor Brief & Project Status Matrix

Step-back records synthesize into an **Advisor-Ready Brief** (Markdown) for real advisor one-on-ones, thesis committee meetings, or lab presentations:

```bash
bth stepback brief <campaign-id>
bth stepback show <campaign-id> --format brief
```
MCP: `stepback_brief(campaign_id="<campaign-id>")`.

### The 8 Brief Sections
1. `# Advisor Brief: <campaign_name> (<campaign_id>)`
2. `## 1. Stakes & Scientific Question` (hypothesis, gap, system)
3. `## 2. Trajectory & Outcomes` (run count, outcome distribution, trajectory plot)
4. `## 3. Threshold Basis & Drift` (sidecar threshold vs actual, drift checks)
5. `## 4. Alternative Hypotheses & Dissent` (rival models, recorded `DissentRecord`s)
6. `## 5. Exit Criteria & Sunk Cost` ("What would change your plan?", spend to date)
7. `## 6. Escalation Status & Chair Disposition` (Level 0–3, chair notes)
8. `## 7. Project Status Matrix Entry ("End / reconsider")`

### "End / reconsider" Column Format
The brief exports a concise status snippet feeding `PROJECT_STATUS.advisor.md`:
- Level 0: `[NOMINAL: N runs, pass rate X.XX]`
- Level 1: `[ADVISORY L1: <trigger_name> active; review recommended]`
- Level 2: `[HOLD L2: <trigger_reason>; review <review_id> recommends <rec>]`
- Level 3: `[ESCALATE L3: Human Chair disposition required; <reason>]`

---

## 7. Anti-Rubber-Stamp Meta-Signals

To prevent the step-back review mechanism itself from becoming a rubber-stamp ritual, `bth sprint-audit` tracks four surveillance meta-signals:

- **Signal 15 (`stepback_unvarying_verdicts`)**: Detects reviewers exhibiting zero score variance ($\sigma_S^2 < 0.005$) or 100% identical recommendations over $N \ge 4$ reviews.
- **Signal 16 (`stepback_low_evidence_rate`)**: Flags when $> 15\%$ of reviews cite fewer than 2 valid catalog records or suffer from post-review citation rot.
- **Signal 17 (`stepback_ignored_escalations`)**: Identifies active Level 2/3 holds that remain unresolved after $> 7$ days or where $\ge 3$ subsequent runs were launched without acknowledgment.
- **Signal 18 (`stepback_turnaround_anomalies`)**: Flags automated agent reviews completed in $< 2.0$ seconds or with 0 database reads, verified via `dispatched_at` telemetry timestamps.

---

## 8. CLI & FastMCP Tool Reference

### CLI Commands (`bth stepback <subcommand>`)
- `bth stepback status <campaign-id> [--verbose]` — View level, active triggers, reviews, snooze state.
- `bth stepback trigger <campaign-id> [--window 5] [--dry-run]` — Evaluate 5 trajectory triggers.
- `bth stepback run <campaign-id>` — Orchestrate trigger check, escalation, and reviewer dispatch.
- `bth stepback record <campaign-id> --path <rubric|open_analysis> ...` — Record an evaluation.
- `bth stepback snooze <campaign-id> --trigger <name> --runs <n> --rationale <str>` — Snooze non-L3 trigger.
- `bth stepback brief <campaign-id>` — Render advisor-ready Markdown brief.
- `bth stepback show <campaign-id> [--format json|brief]` — Inspect reviews and escalation history.
- `bth stepback escalate <campaign-id> --target-level <1|2|3> --reason <str>` — Escalate level.
- `bth stepback resolve <campaign-id> [--deescalate-to 0] --note <str>` — Resolve escalation (L0-L2).

### FastMCP Tools
- `stepback_status(campaign_id: str, verbose: bool = False) -> dict`
- `stepback_trigger(campaign_id: str, window: int = 5, dry_run: bool = False) -> dict`
- `stepback_run(campaign_id: str, window: int = 5, dry_run: bool = False) -> dict`
- `stepback_record(...) -> dict`
- `stepback_review(...) -> dict`
- `stepback_snooze(campaign_id: str, trigger_name: str, runs_window: int = 5, epoch_hours: int = 24, rationale: str = "") -> dict`
- `stepback_brief(campaign_id: str) -> dict`
- `stepback_show(campaign_id: str, format: str = "json") -> dict`
- `stepback_escalate(campaign_id: str, target_level: int, reason: str) -> dict`
- `stepback_resolve(campaign_id: str, deescalate_to: int = 0, note: str = "") -> dict` *(Refuses L3)*

### Error Codes (`BathosErrorCode`)
- `STEPBACK_NOT_FOUND`: Review or escalation record does not exist.
- `STEPBACK_TRIGGER_FAILED`: Trigger computation encountered numerical or data error.
- `STEPBACK_RUBRIC_INVALID`: Rubric dimension score out of range (1–5) or unanchored.
- `STEPBACK_INSUFFICIENT_EVIDENCE`: Review cites fewer than 2 valid primary catalog records.
- `REVIEWER_CONFLICT_OF_INTEREST`: Reviewer is campaign author or run operator.
- `STEPBACK_AGENT_OVERRIDE_REFUSED`: Autonomous agent attempted to resolve Level 3 escalation.
- `STEPBACK_SNOOZE_INVALID`: Attempted to snooze Level 3 or exceeded maximum snooze bounds.

---

## Related Skills

- **bathos-campaigns** — Campaign lifecycle, runs, claims, and Union Gate.
- **bathos-rigor-gates** — Decision D1 non-blocking posture, obligation ledger, and blind reviewer isolation.
- **using-bathos** — Run tracking, pre-registration sidecars, outcome evaluation.

## 9. Locked User Governance Policies (Task `261027_step-back-reviews`)

1. **Tiered Reviewer Dispatch Policy**:
   - **Level 1 (Advisory Warning)**: Dispatch a **single blind independent reviewer** (`outsider` or `pre_mortem` + `stepback_v1` rubric).
   - **Level 2 (Step-Back Hold) or on `HOLD` / Minority Dissent**: Escalate to a **3-persona committee** (Advisor, Methodologist, Outside Skeptic).
2. **Per-Project & Mode Snooze Caps**:
   - Exploratory campaigns default to max `10` runs ($\le 72$ hours).
   - Confirmatory and sequential campaigns default to max `3` runs (`confirmatory_max_snooze_runs = 3`).
   - `.bth.toml` `[stepback]` may configure `max_snooze_runs`, `confirmatory_max_snooze_runs`, and `max_snooze_hours`.
3. **Portfolio Status Matrix Filtering (`PROJECT_STATUS.advisor.md`)**:
   - Populate the `"End / reconsider"` column only for campaigns at **Level $\ge 1$** (`format_project_status_entry(..., include_nominal=False)` / `stepback_brief()["advisor_status_entry"]`), keeping Level 0 nominal campaigns blank/concise in the advisor table.
