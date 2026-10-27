---
title: "Step-Back Reviews for Bathos Campaigns: System Triggers, Dual Review Paths, Reviewer Independence, Escalation Ladders, and Anti-Rubber-Stamp Meta-Signals"
description: "Exhaustive architectural specification for Task 261027_step-back-reviews: automated event triggers with mathematical guard clauses, 5-dimension scored rubric with hierarchical gap-free evaluation and hard-floor veto, 5 qualitative cognitive modes with campaign domain evidence citations, authenticated reviewer independence constraints, 4-tier escalation ladder terminating at the human chair with unconditional FastMCP refusal and TTY/chair-token enforcement, meta-signals 15-18 with dispatch-telemetry guards, Cyclopts CLI/FastMCP tooling parity, and DuckDB schema v19 migration."
status: REVISED_DRAFT
task_id: 261027_step-back-reviews
authors:
  - "Worker Agent (worker_phase2_spec_1)"
  - "Worker Agent (worker_spec_revision_1)"
  - "Teamwork Orchestrator (orchestrator_1)"
date: "2026-10-27"
version: "1.1.0"
backlog_ids: ""
adversarial_review: "passed adversarial revision cycle resolving BLK-01..03, MAJ-01..05, MIN-01..04, Findings 01..15"
---

# Step-Back Reviews for Bathos Campaigns: Architectural Specification

**Task ID:** `261027_step-back-reviews`  
**Version:** `1.1.0` (Revised Adversarial Draft)  
**Date:** 2026-10-27  
**Status:** REVISED_DRAFT  
**Workspace:** `/home/marielle/projects/bathos` (Worktree: `.claude/worktrees/261027-step-back-reviews`)

---

## 0. Problem Statement & Motivation

Autonomous and semi-autonomous research agents in computational biology, molecular dynamics, and machine learning operate within vast exploratory search spaces. Left unconstrained, empirical agent workflows suffer from three acute failure modes:

1. **Behavioral Lock-in & Parameter-Tweaking Loops**: When an initial hypothesis underperforms, agents often enter local optimization loops—fine-tuning Langevin gamma parameters, grid spacing, learning rates, or sequence masks—without stepping back to question underlying mechanistic assumptions.
2. **Superficial Review Compliance ("Rubber-Stamping")**: When self-reviews or peer-reviews are automated, agents default to agreeable, low-variance evaluations that praise progress and issue `CONTINUE` recommendations without checking primary catalog data.
3. **Escalation Erosion**: Warnings, near-bound hits, and statistical anomalies are either dismissed by autonomous agents or buried under rapid sequential job launches.

While `bathos` has established rigorous pre-registration gates for experiment setup (sidecars, claim definitions, Union Gate clauses, literature parity attestations), **post-launch trajectory evaluation has remained largely ungated**. Once a campaign is open, nothing systematically prompts independent evaluation of whether the campaign remains scientifically viable.

**Step-Back Reviews** bridge this gap by establishing:
- **System-Evaluated Event Triggers**: Algorithmic detectors monitoring campaign run streams for repeated marginal outcomes, near-bound hits, outcome-entropy collapse, metric plateaus, and staleness—equipped with robust guard clauses against numerical singularities and post-pivot evaluation deadlocks.
- **Dual Review Paths**: A longitudinally comparable, 5-dimension scored rubric with hierarchical gap-free mapping and hard-floor veto (Path A), and a structured qualitative open-analysis path featuring 5 cognitive modes and mandatory campaign domain primary evidence citations (Path B).
- **Reviewer Independence & Minority Dissent Preservation**: Formal attribution barriers binding reviewers to authenticated runtime identities and preventing self-review, coupled with immutable schemas that preserve dissenting minority critiques.
- **An Escalation Ladder Terminating at the Human Chair**: A 4-tier state machine (Levels 0–3) strictly adhering to bathos Decision D1 (advisory non-blocking warnings during execution, conclusion downgrade), terminating at an inviolable Human Chair gate that AI agents can never close, de-escalate, or override via API, CLI, or FastMCP.
- **Anti-Rubber-Stamp Meta-Signals**: Automated cross-project surveillance signals (Signals 15–18) in `sprint-audit` that detect unvarying verdict streaks, low-evidence reviews, ignored escalations, and review velocity anomalies with dispatch-timestamp telemetry gating.

---

## 1. System Architecture & Component Inventory

The step-back review architecture is organized into a modular sub-package `src/bathos/stepback/` with dedicated submodules, accompanied by extensions to `sprint_audit.py`, `linter.py`, `errors.py`, `compact.py`, `mcp.py`, and `cli_cyclopts.py`.

```
src/bathos/
├── stepback/
│   ├── __init__.py           # Re-exports public API, constants, and error codes
│   ├── schema.py             # Complete Python dataclasses: StepBackReview, EscalationRecord, etc.
│   ├── triggers.py           # Pure mathematical trigger functions with singularity & epoch guards
│   ├── rubrics.py            # 5-dimension rubric evaluator, TOML loader, gap-free hierarchical logic
│   ├── analysis.py           # 5 qualitative cognitive modes, campaign domain citation validation
│   ├── escalation.py         # Levels 0-3 state machine, direct transitions, human chair invariance
│   └── catalog.py            # Cool-tier atomic JSON I/O, DuckDB ingestion and query helpers
├── compact.py                # Schema v19 DuckDB table creation (CREATE TABLE IF NOT EXISTS) & compaction
├── sprint_audit.py           # Adds Signals 15-18 (unvarying streaks, low evidence, ignored holds, velocity)
├── linter.py                 # Tier-1 and Tier-2 linter checks for stepback records and schema compliance
├── errors.py                 # Extends BathosErrorCode with STEPBACK_* and maps domain exception classes
├── mcp.py                    # FastMCP tools: stepback_{status,trigger,review,escalate,resolve,show}
└── cli_cyclopts.py           # CLI command group: `bth stepback <subcommand>`
agent_assets/
└── rubrics/
    └── stepback_v1.toml      # Canonical, content-addressed 5-dimension rubric specification
```

### Storage Topology & Compaction Lifecycle

In alignment with bathos tiered storage architecture:

1. **Cool Tier (Atomic JSON Fragments)**:
   - Cool review records: `<catalog_dir>/stepback/reviews/<campaign_id>_<review_id>.json`
   - Cool escalation records: `<catalog_dir>/stepback/escalations/<campaign_id>_<escalation_id>.json`
   - Storage location is resolved dynamically through `resolve_catalog_dir(workspace_root)` (e.g. `~/.bth/catalog/` or `$BTH_CATALOG_DIR`).
   - Each escalation event is written to a unique UUID file (`<campaign_id>_<escalation_id>.json`) rather than overwriting a single file, eliminating multi-process race conditions under SLURM and preserving a tamper-proof historical audit log.
   - All cool writes execute via atomic write-then-rename (`tempfile.NamedTemporaryFile` in target directory + `os.replace`).
   - Project-level configuration and overrides reside optionally in `.bth.toml` or `.bth/stepback/config.toml` in the repository root.

2. **Warm Tier (DuckDB Relational Tables in `bathos.db`)**:
   - `stepback_reviews`: Flattened relational index of completed reviews, rubric scores, cognitive modes, input snapshot SHAs, and citation counts.
   - `stepback_escalations`: Relational index of campaign escalation transitions, tracking trigger origins, review links, and resolution epochs.
   - Both tables are managed in `src/bathos/compact.py` using `CREATE TABLE IF NOT EXISTS` alongside `trust_ledger` and `blast_radius_ledger`.
   - Ingestion hook `_ingest_stepback_fragments(db, catalog_dir)` scans `<catalog_dir>/stepback/` during `bth compact` and inserts new records idempotently (`INSERT OR REPLACE`).

---

## 2. System-Evaluated Trigger Formulas & Detection Engine

Step-back triggers run on automated event hooks (e.g. after each member run completion in `runner.py`, during `bth run` / `bth submit`, or during `bth campaign review` / `bth campaign conclude`). They evaluate mathematical trajectory properties without relying on human or agent memory.

### Universal Trigger Preconditions & Epoch Anchors

To prevent false alarms on sparse data and state machine deadlocks:

1. **Minimum Run Count Guard**:
   Every trigger begins with an explicit sample-size check. Let $N$ be the total count of member runs in campaign $C$:
   - For $T_{\text{repeated\_marginal}}$, $T_{\text{near\_bound}}$, and $T_{\text{stale}}$: If $N < 3$, return `{"fired": False, "reason": "insufficient_runs", "run_count": N}` immediately.
   - For $T_{\text{entropy\_collapse}}$ and $T_{\text{plateau}}$: If $N < 5$, return `{"fired": False, "reason": "insufficient_runs", "run_count": N}` immediately.

2. **Post-Pivot Epoch Anchoring (Sliding Window Reset)**:
   When an active Level 2 Hold or Level 1 Warning is de-escalated following an approved `PIVOT` review, `EscalationState` records `pivot_anchor_run_id = r_N` and increments `review_epoch`.
   - Triggers evaluating sliding windows ($W_k$) or slopes only consider runs executed **strictly after** the anchor:
     $$\mathcal{R}_{\text{active}} = \{ r_i \in C \mid i > \text{index}(r_{\text{pivot}}) \}$$
   - A **grace period** of $k$ runs is granted: sliding window frequency rules ($f_{\text{marginal}} \ge 0.60$, entropy collapse) do not fire until $|\mathcal{R}_{\text{active}}| \ge k$. This eliminates the post-pivot ping-pong deadlock where old marginal runs poison the window on the very next run.

3. **Deterministic Metric Selection Order**:
   For triggers requiring a primary numeric scalar metric $y_i$ ($T_{\text{near\_bound}}$, $T_{\text{plateau}}$), the metric is resolved deterministically:
   1. Explicit metric specified in sidecar `[stepback] target_metric = "..."`.
   2. Explicit metric declared in sidecar `[benchmark] metric = "..."`.
   3. The first numeric float field declared in the script sidecar's `[result_schema]`.
   4. If no numeric metric is declared or resolved from run outputs, the trigger gracefully skips with `{"fired": False, "reason": "no_numeric_metric"}`.

---

### 2.1 Repeated Marginal Outcomes ($T_{\text{repeated\_marginal}}$)

- **Problem**: Stalled hyperparameter optimization cycles producing borderline results without conclusive progress.
- **Mathematical Definition**:
  Let $R = [r_1, r_2, \dots, r_N]$ be the time-ordered sequence of member runs in the active epoch $\mathcal{R}_{\text{active}}$.  
  Let $W_k = [r_{N-k+1}, \dots, r_N]$ be the sliding window of the $k$ most recent runs ($k=5$ default, configurable $k \in [3, 20]$).  
  Let $o(r_i) \in \mathcal{O}$ be the evaluated outcome label of run $r_i$.  
  Define the marginal indicator function:
  $$I_{\text{marginal}}(r_i) = \begin{cases} 1 & \text{if } o(r_i) = \text{"marginal"} \\ 0 & \text{otherwise} \end{cases}$$
  The recent streak length $S_{\text{marginal}}$ across all active epoch runs is:
  $$S_{\text{marginal}} = \max \{ s \in [0, N] \mid \forall j \in [N-s+1, N], o(r_j) = \text{"marginal"} \}$$
  The sliding window frequency $f_{\text{marginal}}(W_k)$ is:
  $$f_{\text{marginal}}(W_k) = \frac{1}{k} \sum_{r \in W_k} I_{\text{marginal}}(r)$$
- **Trigger Rule**:
  Trigger $T_{\text{repeated\_marginal}}$ fires if and only if $N \ge 3$ and:
  $$S_{\text{marginal}} \ge s_{\text{thresh}} \quad (s_{\text{thresh}} = 3) \quad \lor \quad \Big( |\mathcal{R}_{\text{active}}| \ge k \ \land \ f_{\text{marginal}}(W_k) \ge \theta_{\text{marginal}} \Big) \quad (\theta_{\text{marginal}} = 0.60)$$
- **Output Record**:
  ```json
  {
    "trigger": "repeated_marginal",
    "fired": true,
    "streak": 3,
    "frequency": 0.60,
    "window_size": 5,
    "epoch": 1,
    "offending_run_ids": ["run_123", "run_124", "run_125"]
  }
  ```

---

### 2.2 Near-Bound Hits (Boundary Proximity) ($T_{\text{near\_bound}}$)

- **Problem**: Experimental metrics that skate symmetrically within narrow margins of decision thresholds are statistically brittle and prone to false confirmation.
- **Mathematical Definition**:
  For each run $r_i \in W_k$, let $y_i$ be the resolved primary scalar metric, and $B$ be the critical decision boundary (from sidecar `[outcomes.pass] condition`, `[breakdown] bound`, or `[benchmark] regression_threshold`).  
  Let $s_y = \text{stddev}(\{y_1, \dots, y_N\})$ be the rolling sample standard deviation across active epoch runs.  
  Define tolerance margin $\epsilon$:
  $$\epsilon = \begin{cases} 
  \epsilon_{\text{declared}} & \text{if declared in sidecar} \\ 
  0.10 \times |B| & \text{if } B \ne 0 \\ 
  \max(0.50 \times s_y, 10^{-6}) & \text{if } B = 0 \text{ and undeclared} 
  \end{cases}$$
  *(Scaling by $s_y$ when $B=0$ guarantees invariance against extreme numerical units like binding affinities $K_d \sim 10^{-9}$ or loss gradients $\Delta L \sim 10^{-6}$, preventing false-positive near-zero alarms).*  
  The normalized proximity distance $d_i$ to boundary $B$ is:
  $$d_i = \frac{|y_i - B|}{\epsilon}$$
  A run is classified as a *near-bound hit* if $d_i \le 1.0$.
- **Trigger Rule**:
  Guard clause: requires $N \ge 3$ and resolved boundary $B$.  
  Over window $W_k$ ($k=5$), let $N_{\text{nb}} = \sum_{r_i \in W_k} \mathbf{1}(d_i \le 1.0)$.  
  Trigger $T_{\text{near\_bound}}$ fires if:
  $$N_{\text{nb}} \ge m_{\text{thresh}} \quad (m_{\text{thresh}} = 2) \quad \lor \quad (N \ge 1 \ \land \ d_N \le 0.25) \quad (\text{critical proximity within } 2.5\% \text{ of } |B|)$$
- **Output Record**:
  ```json
  {
    "trigger": "near_bound",
    "fired": true,
    "hits": 2,
    "window_size": 5,
    "bound": 0.50,
    "margin": 0.05,
    "recent_distances": [0.42, 0.18]
  }
  ```

---

### 2.3 Outcome-Entropy Collapse ($T_{\text{entropy\_collapse}}$)

- **Problem**: Unproductive convergence where 100% of recent runs produce identical outcome labels, indicating an exploration plateau or fixation rut.
- **Mathematical Definition**:
  Guard clause: requires $N \ge 5$.  
  Let $U = \{u_1, u_2, \dots, u_m\}$ be the set of distinct outcome labels observed in window $W_k$ ($k=5$).  
  Let $p_j = \frac{1}{k} \sum_{r \in W_k} \mathbf{1}(o(r) = u_j)$ be the empirical probability of outcome $u_j$.  
  The Shannon entropy $H_e(W_k)$ is formulated strictly in **nats** (natural logarithm, matching `src/bathos/sprint_audit.py`):
  $$H_e(W_k) = - \sum_{j=1}^m p_j \ln(p_j), \quad \text{with } 0 \ln(0) \equiv 0$$
  *(Note: $H_e$ in nats is equivalent to $H_2 \ln(2)$ in bits. A threshold of $\tau_{\text{entropy}} = 0.35$ nats corresponds to $\approx 0.50$ bits).*
- **Trigger Rule**:
  Trigger $T_{\text{entropy\_collapse}}$ fires if:
  $$k \ge 5 \ \land \ \Big( H_e(W_k) < \tau_{\text{entropy}} \quad (\tau_{\text{entropy}} = 0.35 \text{ nats}) \ \lor \ H_e(W_k) = 0 \Big)$$
- **Contextual Annotation**:
  - `fixation_positive`: If $o(r) = \text{"pass"} \ \forall r \in W_k$. System advises: *"Objective fulfilled; recommend conclude or rigorous replication."*
  - `fixation_negative`: If $o(r) \ne \text{"pass"} \ \forall r \in W_k$. System advises: *"Unproductive rut; recommend pivot or stop."*

---

### 2.4 Metric Plateaus (Moving Slope & Variance Decay) ($T_{\text{plateau}}$)

- **Problem**: Consuming compute while generating negligible statistical progress.
- **Mathematical Definition**:
  Guard clause: requires $n \ge 5$ runs with resolved primary numeric scalar $y_i$.  
  Let $(t_i, y_i)_{i=1}^n$ represent the run sequence index $t_i = i$ and primary numeric metric $y_i$ across the last $n$ runs ($n=6$ default, minimum $n=5$).  
  Ordinary Least Squares (OLS) linear regression estimates slope $\beta$:
  $$\bar{t} = \frac{n+1}{2}, \quad \bar{y} = \frac{1}{n} \sum_{i=1}^n y_i$$
  $$\beta = \frac{\sum_{i=1}^n (t_i - \bar{t})(y_i - \bar{y})}{\sum_{i=1}^n (t_i - \bar{t})^2} = \frac{12 \sum_{i=1}^n (t_i - \bar{t})(y_i - \bar{y})}{n(n^2 - 1)}$$
  *(Singularity check: Since $n \ge 5$, $n(n^2-1) \ge 120 > 0$, preventing zero division).*  
  Sample standard deviation is:
  $$s_y = \sqrt{\frac{1}{n-1} \sum_{i=1}^n (y_i - \bar{y})^2}$$
  *(Singularity check: Since $n \ge 5$, $n-1 \ge 4 > 0$).*

  **Zero-Centered Metric Handling**:
  To prevent CV explosion when metrics are centered near zero ($|\bar{y}| \approx 0$, e.g. drift velocity or signed energy deltas):
  - **Standard Case ($|\bar{y}| \ge 0.01$)**:
    $$\bar{\beta}_{\text{rel}} = \frac{|\beta|}{|\bar{y}|}, \quad CV = \frac{s_y}{|\bar{y}|}$$
    Trigger fires if: $\bar{\beta}_{\text{rel}} < 0.01$ (less than $1\%$ progress per run) $\land \ CV < 0.02$.
  - **Near-Zero Case ($|\bar{y}| < 0.01$)**:
    Evaluate absolute scale thresholds directly:
    $$|\beta| < \delta_{\text{abs\_slope}} \quad (\delta_{\text{abs\_slope}} = 0.001) \quad \land \quad s_y < \delta_{\text{abs\_std}} \quad (\delta_{\text{abs\_std}} = 0.001)$$
- **Trigger Rule**:
  Trigger $T_{\text{plateau}}$ fires if and only if $n \ge 5$ and either standard or near-zero plateau conditions are satisfied.

---

### 2.5 Campaign Staleness ($T_{\text{stale}}$)

- **Problem**: Abandoned open campaigns or active wheel-spinning with no hypothesis advancement.
- **Mathematical Definition**:
  Let $t_{\text{now}}$ be current UTC timestamp.  
  Let $t_{\text{last\_run}}$ be the completion timestamp of the latest run in campaign $C$.  
  **Null-Safe Timestamp Guard**: If $t_{\text{last\_run}}$ is `None` (a newly opened campaign with zero completed runs), set $t_{\text{last\_run}} = C.\text{created\_at}$.  
  Idle wall-clock days: $\Delta t_{\text{idle}} = \frac{t_{\text{now}} - t_{\text{last\_run}}}{86400}$.  
  Total member run count: $N_{\text{runs}} = |\{r \in C\}|$.  
  Let $U(C)$ be the count of unresolved hypotheses in $C$ or uncovered Union Gate clauses in the registered claim file.  
  Let $\Delta U_{10} = U(C)_{N-10} - U(C)_N$ be the clause resolution rate over the last 10 runs.
- **Trigger Rule**:
  Trigger $T_{\text{stale}}$ fires if any of:
  1. **Temporal Abandonment**: Campaign `status = "open"` and $\Delta t_{\text{idle}} \ge 14.0$ days.
  2. **Hypothesis Wheel-Spinning**: Campaign `status = "open"`, an active claim file is registered (`C.claim_path IS NOT NULL`), $N_{\text{runs}} \ge 15$, and $\Delta U_{10} = 0$. *(Note: If no claim file is registered, this rule is skipped, eliminating false positives on non-claim exploration campaigns).*
  3. **Uncapped Exploratory Drift**: Campaign `mode = "exploration"`, $N_{\text{runs}} \ge 25$, and no conclusion or linked child campaign.

---

## 3. Dual Review Paths

Reviewers evaluate campaigns through two complementary pathways:

```
                      ┌────────────────────────────────────────┐
                      │        Step-Back Review Event          │
                      └───────────────────┬────────────────────┘
                                          │
                  ┌───────────────────────┴───────────────────────┐
                  ▼                                               ▼
     ┌─────────────────────────┐                     ┌─────────────────────────┐
     │  Path A: Scored Rubric  │                     │ Path B: Open Analysis   │
     │  - 5 Weighted Dimensions│                     │ - 5 Cognitive Modes     │
     │  - 1 to 5 Anchors       │                     │ - Mandatory Citations   │
     │  - Hard Floor Veto      │                     │ - Explicit Recommendation│
     │  - Gap-Free Mapping     │                     │ - Campaign Domain Check │
     └────────────┬────────────┘                     └────────────┬────────────┘
                  │                                               │
                  └───────────────────────┬───────────────────────┘
                                          ▼
                      ┌────────────────────────────────────────┐
                      │    Dissent & Independence Handling     │
                      │    - Authenticated Identity Check      │
                      │    - Minority Dissent Preservation     │
                      └───────────────────┬────────────────────┘
                                          ▼
                      ┌────────────────────────────────────────┐
                      │      Escalation Ladder & Verdict       │
                      │   Level 0 / 1 / 2 (Advisory) / 3 (Chair)│
                      └────────────────────────────────────────┘
```

---

### 3.1 Path A: Scored, Versioned Rubric Path

#### Five Dimensions, Weights, and Anchors
Evaluations score five dimensions on a discrete 1 to 5 integer scale:

1. **Question Validity ($D_1$, Weight $w_1 = 0.20$)**:
   - *1 (Invalid)*: Question is trivial, untestable, superseded by external literature, or framed unfalsifiably.
   - *2 (Vague)*: Question is exploratory but lacks discriminability between candidate mechanisms.
   - *3 (Adequate)*: Question is testable with reasonable scope, but lacks clear high-impact pivot criteria.
   - *4 (Strong)*: Well-posed scientific question addressing an established confound or mechanism.
   - *5 (Decisive)*: Pivotal question whose answer fundamentally discriminates competing scientific theories.
2. **Empirical Progress ($D_2$, Weight $w_2 = 0.25$)**:
   - *1 (Stalled / Degenerate)*: Runs produce random noise, contradictory outputs, persistent errors, or execution crashes.
   - *2 (Inconclusive)*: High variance; effect size within measurement noise.
   - *3 (Moderate)*: Detectable signal emerging, but confidence intervals remain wide.
   - *4 (Clear)*: Consistent monotonic or clustered signals across seeds and conditions.
   - *5 (Decisive)*: Statistically rigorous separation; effect size exceeds 3 standard deviations; highly reproducible.
3. **Theoretical Grounding ($D_3$, Weight $w_3 = 0.20$)**:
   - *1 (Flailing / Ad-Hoc)*: Tweaking parameters with no mechanistic hypothesis or physical rationale.
   - *2 (Superficial)*: Borrowed heuristics without mechanistic modeling.
   - *3 (Plausible)*: Hypothesized mechanism is plausible and consistent with domain physics.
   - *4 (Sound)*: Strongly supported by physical or statistical first principles.
   - *5 (First-Principles)*: Rigorous mathematical or biophysical derivation; predictive accuracy verified.
4. **Resource Efficiency ($D_4$, Weight $w_4 = 0.15$)**:
   - *1 (Wasteful)*: Massive compute or GPU hours expended with negligible information gain; endless marginal loops.
   - *2 (Inefficient)*: Sub-optimal sample allocation; redundant parameter grids.
   - *3 (Acceptable)*: Run count is commensurate with campaign complexity.
   - *4 (Frugal)*: High information gain per run; early stopping properly leveraged.
   - *5 (Optimal)*: Minimal necessary run count; near-optimal Bayesian information acquisition.
5. **Risk & Confound Surface ($D_5$, Weight $w_5 = 0.20$)**:
   - *1 (Fatal Confounds)*: Uncontrolled batch effects, data leakage, mutated baseline artifacts, unverified positive controls.
   - *2 (High Risk)*: Known confounds unhedged; missing synthetic recovery controls.
   - *3 (Standard Controls)*: Standard controls present; basic confound checks pass.
   - *4 (Well-Isolated)*: All major confounds actively controlled; baseline parity verified.
   - *5 (Impenetrable Rigor)*: Confounds rigorously eliminated; positive/negative controls GREEN; baseline parity attested.

#### Composite Score & Gap-Free Hierarchical Recommendation Logic
Let $s_i \in \{1, 2, 3, 4, 5\}$ be the integer score for dimension $D_i$.  
The weighted composite score is:
$$S_{\text{composite}} = \sum_{i=1}^5 w_i s_i, \quad \sum_{i=1}^5 w_i = 1.0$$

To eliminate all undefined partition gaps across the entire domain $S_{\text{composite}} \in [1.0, 5.0]$, recommendation evaluation follows a strict **hierarchical resolution tree**:

```python
def resolve_rubric_recommendation(scores: dict[str, int], composite: float) -> tuple[str, int]:
    """Hierarchical, gap-free recommendation and minimum escalation mapping."""
    # Tier 1: Hard-Floor Veto (Any dimension scored 1)
    if any(s == 1 for s in scores.values()):
        return ("HOLD", 2)
    
    # Tier 2: Sub-nominal dimension present (Any dimension scored 2, all others >= 2)
    if any(s == 2 for s in scores.values()):
        if composite >= 3.4:
            return ("PIVOT", 1)       # Capped at PIVOT even if composite >= 4.2
        elif composite >= 2.6:
            return ("HOLD", 2)
        elif composite >= 2.0:
            return ("ESCALATE", 2)
        else:
            return ("CONCLUDE_ABANDON", 2)
            
    # Tier 3: Nominal/Strong performance (All dimensions >= 3)
    if composite >= 4.2:
        return ("CONTINUE", 0)
    elif composite >= 3.4:
        return ("PIVOT", 1)
    elif composite >= 2.6:
        return ("HOLD", 2)
    elif composite >= 2.0:
        return ("ESCALATE", 2)
    else:
        return ("CONCLUDE_ABANDON", 2)
```

**Properties of Hierarchical Logic**:
1. **Total Partition Coverage**: Every valid score tuple in $[1, 5]^5$ maps deterministically to exactly one recommendation.
2. **Floor Veto Invariance**: A single score of 1 in Risk/Confound Surface or Question Validity immediately forces `HOLD` and minimum Escalation Level 2, regardless of how high other dimensions score.
3. **Flaw Capping**: A score of 2 in any dimension prohibits a `CONTINUE` recommendation, capping the outcome at `PIVOT`.

#### Cryptographic Input Snapshot Binding
To guarantee auditability and prevent post-hoc rationalization, every review record stores an immutable cryptographic input snapshot:
- `input_snapshot_sha256`: SHA-256 hash over canonical JSON of input state at the moment of review.
- `input_snapshot`: JSON dictionary capturing:
  ```json
  {
    "campaign_id": "camp_nvt_stability",
    "evaluated_run_ids": ["run_123", "run_124", "run_125"],
    "latest_run_sha": "a1b2c3d4...",
    "sidecar_shas": {
      "scripts/experiments/nvt.bth.toml": "e5f6g7h8..."
    },
    "git_head_commit": "7a8b9c0d..."
  }
  ```

---

### 3.2 Path B: Qualitative Open Analysis Path

Path B provides qualitative cognitive disruption using five specialized inquiry modes:

1. **Advisor Conversation**:
   - *Role*: Independent senior research advisor / PI persona.
   - *Sections*: `premise_challenge`, `simplest_decisive_test`, `strategic_advice`.
2. **Pre-Mortem** (Gary Klein / Kahneman):
   - *Role*: Prospective failure investigator.
   - *Premise*: "Imagine it is six months from today. This campaign completely failed, wasted all its compute budget, and produced an irreproducible paper. Exactly how did it fail?"
   - *Sections*: `failure_narrative`, `overlooked_red_flags`, `present_day_symptoms`, `preventative_mitigations`.
3. **So-What Test**:
   - *Role*: Pragmatic research director.
   - *Premise*: "Assume every hypothesis in this campaign is confirmed with $p < 0.001$. Does anyone care? Does it change an engineering decision?"
   - *Sections*: `best_case_scenario`, `practical_utility`, `decision_impact`, `opportunity_cost`.
4. **Outsider Read**:
   - *Role*: Domain-agnostic skeptic scientist.
   - *Premise*: Remove all local jargon and re-describe the experiments in elementary scientific terms.
   - *Sections*: `jargon_free_summary`, `common_sense_vulnerabilities`, `alternative_explanations`.
5. **Steelman-The-Stop**:
   - *Role*: Adversarial prosecutor for resource reallocation.
   - *Premise*: "Present the absolute strongest, most compelling, evidence-backed argument for terminating this campaign immediately."
   - *Sections*: `the_case_for_stopping`, `sunk_cost_fallacies_exposed`, `superior_resource_reallocation`, `retained_learnings`.

**Mandatory Recommendation Parameter**:
Because Path B does not compute numeric scores, the reviewer **must explicitly provide** a `recommendation` argument (`CONTINUE`, `PIVOT`, `HOLD`, `ESCALATE`, `CONCLUDE_ABANDON`). Submissions omitting `recommendation` in Path B are rejected with `STEPBACK_INVALID_SCHEMA`.

---

### 3.3 Primary Evidence Citation Syntax & Campaign Domain Verification Engine

To prevent subjective hallucinations and evidence laundering, cited evidence must be verified against primary campaign records:

- **Run Citation**: `run:<uuid>` or `run:<uuid_prefix>` (e.g. `run:8f3a9b1c`).
- **Sidecar & Outcome**: `sidecar:<path>#outcomes.<label>` or `outcome:<uuid>:<label>`.
- **Union Gate Clause**: `clause:<claim_path>#<clause_id>` (e.g. `clause:.bth/claims/claim_nvt.toml#G1`).
- **Post-Mortem**: `postmortem:<path>#<obligation_id>`.
- **Git Commit / Code**: `git:<commit_sha>#<file>:<line>`.

#### Campaign Domain Verification Rules
The registration engine enforces **campaign domain containment**:
1. For citations of kind `run:<id>`: The engine verifies not only that `<id>` exists in `bathos.db`, but that `<id>` is a recorded member run of `campaign_id` (in DuckDB `campaign_runs` or cool campaign fragments). Citing unrelated runs from other campaigns is rejected as evidence laundering.
2. For citations of kind `sidecar:<path>`: The engine verifies that `<path>` corresponds to a script executed by member runs in `campaign_id`.
3. For citations of kind `clause:<path>#<id>`: The engine verifies that `<path>` matches the registered `claim_path` for `campaign_id`.
4. For citations of kind `git:<sha>`: The engine verifies that `<sha>` is an ancestor or exact commit of member runs in `campaign_id`.

**Verification Threshold**:
Path B reviews require $N_{\text{valid}} \ge 2$ verified domain citations. If $N_{\text{valid}} < 2$, registration is rejected with `STEPBACK_INSUFFICIENT_EVIDENCE`.

---

## 4. Governance: Reviewer Independence & Minority Dissent Preservation

### 4.1 Formal Independence & Authenticated Attribution

A review cannot provide objective governance if authored by the agent or researcher who conducted the experiments.

- Let $C$ be the campaign under review.
- Let $\mathcal{R}_C = \{r_1, \dots, r_N\}$ be the set of member runs in $C$.
- Let $\mathcal{A}_{\text{agents}} = \{ r.\text{agent\_mode} \mid r \in \mathcal{R}_C, r.\text{agent\_mode} \ne "" \}$.
- Let $\mathcal{A}_{\text{authors}} = \{ r.\text{metadata.author} \mid r \in \mathcal{R}_C \} \cup \{ \text{getattr}(C, "creator", "") \}$.
- Let $\mathcal{A}_{\text{git}} = \{ \text{git commit authors of scripts executed in } C \}$.
- The forbidden author set is:
  $$\mathcal{A}_{\text{forbidden}} = \mathcal{A}_{\text{agents}} \cup \mathcal{A}_{\text{authors}} \cup \mathcal{A}_{\text{git}}$$

#### Authenticated Identity Binding
To prevent spoofing where an agent supplies a synthetic `reviewer_id`:
1. `reviewer_id` is bound to the **authenticated runtime identity**:
   - For CLI invocations: Resolved via `os.getlogin()`, authenticated git identity (`git config user.name`), or local user UID.
   - For agent sessions: Resolved via authenticated agent environment tokens (`CLAUDE_AGENT_ID`, `AGENT_NAME`, or session context).
2. The submission engine compares the authenticated caller identity against `reviewer_id`. Any mismatch raises `STEPBACK_INVALID_SCHEMA`.
3. If the authenticated `reviewer_id` $\in \mathcal{A}_{\text{forbidden}}$, the review is marked `is_independent = false` with warning `REVIEWER_CONFLICT_OF_INTEREST`. Non-independent reviews are permanently logged for audit but **cannot** discharge an escalation hold or de-escalate campaign status.

---

### 4.2 Multi-Reviewer Panel Schema & Immutable Dissent Array

When reviews are conducted by a committee or multi-agent panel, majority consensus must never overwrite or erase dissenting minority critiques.

The review record and CLI/FastMCP tooling support an explicit `dissents` parameter:
```json
{
  "panel_id": "panel_uuid_1234",
  "campaign_id": "camp_abc",
  "created_at": "2026-10-27T19:30:00Z",
  "consensus_recommendation": "CONTINUE",
  "consensus_composite_score": 4.1,
  "dissent_present": true,
  "panelists": ["agent_reviewer_1", "agent_reviewer_2", "agent_reviewer_3"],
  "dissenting_opinions": [
    {
      "dissenting_reviewer_id": "agent_reviewer_3",
      "minority_recommendation": "HOLD",
      "minority_composite_score": 2.4,
      "dissent_summary": "Unchecked temperature drift in run 8f3a indicates Langevin thermostat instability.",
      "cited_counter_evidence": [
        "run:8f3a9b1c-321a",
        "sidecar:scripts/experiments/nvt.bth.toml#outcomes.marginal"
      ],
      "majority_rebuttal": "Majority judged temperature variance acceptable within initial equilibration phase."
    }
  ]
}
```

**Preservation Invariants**:
1. Dissent records are permanent sub-elements of the review record in both cool JSON and warm DuckDB storage.
2. **Automatic Escalation on Dissent**: If any panelist files a dissent recommending `HOLD`, `ESCALATE`, or `CONCLUDE_ABANDON`, the campaign is automatically placed in a Level 2 Hold to force human review of the unresolved minority critique.

---

## 5. Escalation Ladder & Human Chair Invariance

### 5.1 The 4-Tier Ladder

```
[Level 0: Clean / Nominal]
       │
       ├─────────────────────────────────┐
       ▼ (Single trigger)                ▼ (Catastrophic trigger / s >= 3)
[Level 1: Advisory Warning]       [Level 2: Committee Step-Back Hold]
       │                                 │
       ├─────────────────────────────────┤
       ▼ (Severe confound / Unresolved)  ▼ (Ignored > 7d / Direct referral)
[Level 3: Human Chair Escalation] ◄──────┘
       │
       └─────► [ONLY HUMAN CHAIR CAN RESOLVE / CLOSE / OVERRIDE]
```

1. **Level 0: Clean / Nominal**:
   - Campaign runs smoothly; all triggers nominal.
   - System posture: Completely silent.
2. **Level 1: Advisory Warning**:
   - Fired by: Single trigger occurrence (e.g. 1 near-bound hit, initial plateau sign, 1 marginal outcome).
   - System posture: Non-blocking warning banner printed on CLI (`bth run`, `bth campaign review`) and tracked in `sprint-audit`.
3. **Level 2: Committee Step-Back Hold**:
   - Fired by: Multiple simultaneous triggers, sustained marginal streak ($s \ge 3$), entropy collapse, Path A rubric composite $< 2.8$, Path A hard-floor veto, panel dissent, or direct promotion from Level 0 or 1.
   - System posture: Campaign enters `stepback_hold`. Advisory execution warnings emitted at `bth run` and `bth submit`.
4. **Level 3: Human Chair Escalation**:
   - Fired by: Level 2 hold ignored for $> 7$ days or $\ge 3$ subsequent runs; unresolved panel dissent; severe unmitigated confound; catastrophic failure; or explicit referral to the human PI.
   - System posture: Escalation assigned strictly to the Human Chair. **AI AGENTS STRICTLY FORBIDDEN FROM RESOLVING OR OVERRIDING**.

---

### 5.2 State Transition Matrix & Direct Escalation Paths

The escalation state machine supports direct transitions across levels to accommodate catastrophic failures, emergency pauses, and human chair resolutions:

| From Level | To Level | Permitted Actor | Required Preconditions / Triggers |
|---|---|---|---|
| Level 0 | Level 1 | System / Agent / Human | Any single trigger fires ($T_{\text{repeated\_marginal}}$, $T_{\text{near\_bound}}$, $T_{\text{plateau}}$, $T_{\text{entropy\_collapse}}$, $T_{\text{stale}}$). |
| Level 0 | Level 2 | System / Agent / Human | **Direct Escalation**: Marginal streak $s \ge 3$, multiple triggers firing simultaneously, or Path A floor veto during initial review. |
| Level 0 | Level 3 | System / Human | **Direct Escalation**: Severe integrity breach, irreconcilable data corruption, or direct referral to human chair. |
| Level 1 | Level 0 | System / Independent Reviewer | 3 subsequent consecutive passing runs with zero triggers, or independent review approving trajectory with `CONTINUE`. |
| Level 1 | Level 2 | System / Agent / Human | Multiple triggers firing simultaneously, marginal streak $s \ge 3$, rubric composite score $< 2.8$, or panel dissent. |
| Level 1 | Level 3 | System / Committee / Human | **Direct Escalation**: Critical unmitigated confound discovered or Level 1 advisory ignored across $\ge 5$ consecutive runs. |
| Level 2 | Level 1 | Independent Reviewer / Human Chair | Valid independent step-back review submitted with recommendation `CONTINUE` or approved `PIVOT` plan. Sets `pivot_anchor_run_id` and increments `review_epoch`. |
| Level 2 | Level 3 | System / Committee / Human | Level 2 hold ignored $> 7$ days or $\ge 3$ subsequent runs launched; panel dissent irreconcilable; severe fatal confound discovered. |
| Level 3 | Level 0 / 1 / 2 | **HUMAN CHAIR ONLY** | Explicit human chair resolution via interactive TTY or verified chair token. Target level specified by `deescalate_to` (default: 0). **AGENTS STRICTLY FORBIDDEN**. |

---

### 5.3 Inviolable Human Chair Invariance (Iron Law)

The Human Committee Chair is the terminal authority. Autonomous agents can never close, de-escalate, or circumvent a Level 3 escalation under any circumstances:

1. **Unconditional FastMCP Refusal**:
   The FastMCP tool `stepback_resolve` **unconditionally refuses** any request targeting a Level 3 escalation. Any call to `stepback_resolve` on a Level 3 escalation immediately returns:
   ```json
   {
     "ok": false,
     "error_code": "stepback_agent_override_refused",
     "error": "FastMCP cannot resolve Level 3 Human Chair escalations. Human chair resolution requires interactive TTY CLI or secure chair token.",
     "resolution_hint": "Run `bth stepback resolve <campaign_id> --escalation-id <id> --as-human-chair` from an interactive terminal."
   }
   ```

2. **Interactive TTY & Chair Token Verification**:
   The CLI command `bth stepback resolve` allows Level 3 resolution only if at least one of the following two human verification gates succeeds:
   - **Interactive TTY Gate**: `sys.stdin.isatty() == True` AND an interactive confirmation prompt is answered affirmatively by a human user.
   - **Secure Chair Token Gate**: A valid cryptographically random token exists in `~/.bth/chair_token` (file permissions `0600`), and matches a passed `--chair-token` argument or prompt input. This token is generated during `bth init` by the human researcher and is never readable by or exposed to subagent execution sandboxes.

3. **Autonomous Agent Environment Rejection**:
   If `BTH_AGENT_MODE`, `CLAUDE_AGENT`, `AGENT_NAME`, or known subagent environment signatures are detected during execution, `bth stepback resolve --as-human-chair` is rejected with `BathosErrorCode.STEPBACK_AGENT_OVERRIDE_REFUSED`.

---

### 5.4 Harmonized Decision D1 Policy (Execution vs Conclusion)

To maintain absolute architectural consistency with bathos Decision D1:

1. **Execution Posture (`bth run`, `bth submit`)**:
   - **Advisory by Default**: Active Level 1 warnings and Level 2 holds emit high-priority warning banners to stderr but do not abort runs or cancel SLURM jobs:
     ```
     ┌────────────────────────────────────────────────────────────────────────┐
     │ WARNING: Campaign 'sp98-gap' is in Step-Back HOLD (Level 2).          │
     │ Reason: Repeated marginal outcomes (streak=3, frequency=0.60).         │
     │ Action: Conduct independent step-back review via `bth stepback review`.│
     └────────────────────────────────────────────────────────────────────────┘
     ```
   - **Opt-in Execution Enforcement**: Setting `[stepback] enforce = true` in `.bth.toml` or exporting `BTH_STEP_BACK_ENFORCE=1` elevates holds to blocking errors during `bth run` and `bth submit`.

2. **Conclusion Posture (`bth campaign conclude`)**:
   - **Confirmation & Sequential Campaigns**: If an unaddressed Level 2 Hold or Level 3 Escalation is active, `bth campaign conclude` **automatically downgrades** the conclusion verdict to `confounded` (or `unresolved_stepback`).
   - **Bypass Option**: Concluding with `--force-verdict` overrides the downgrade but explicitly records `stepback_status = "bypassed"` in the campaign conclusion record, triggering audit surveillance.
   - **Exploration Campaigns**: Unaddressed holds emit a prominent warning banner at conclusion without automatic verdict downgrade.

---

## 6. Anti-Rubber-Stamp Meta-Signals (Signals 15–18)

Automated surveillance metrics in `sprint-audit` prevent review procedures from degenerating into empty administrative rubber-stamps:

### 6.1 Signal 15: `stepback_unvarying_verdicts`
- **Vulnerability**: Agents issuing uniform praise and identical high scores across all campaigns.
- **Algorithm**:
  Collect the last $N$ reviews in the project ($N \ge 4$).  
  Compute composite score variance:
  $$\sigma_S^2 = \frac{1}{N-1} \sum_{i=1}^N (S_i - \bar{S})^2$$
  Compute recommendation uniformity:
  $$U_{\text{rec}} = \max_{\text{rec}} \frac{|\{r \in \text{Reviews} \mid r.\text{recommendation} = \text{rec}\}|}{N}$$
- **Signal Rule**:
  Flag `Signal 15: stepback_unvarying_verdicts` (Severity: WARNING) if:
  $$N \ge 4 \ \land \ (\sigma_S^2 < 0.005 \ \lor \ U_{\text{rec}} = 1.0)$$

### 6.2 Signal 16: `stepback_low_evidence_rate`
- **Vulnerability**: Hallucinated qualitative reviews lacking empirical citations or suffering from citation rot.
- **Algorithm**:
  For each completed review $R$, verify cited catalog records against DuckDB and campaign domain membership.  
  Let $\mathcal{C}_R$ be verified valid citations.  
  A review is classified as *low-evidence* if $|\mathcal{C}_R| < 2$.  
  The low-evidence review rate is:
  $$\text{Rate}_{\text{low\_ev}} = \frac{|\{R \in \text{Reviews} \mid |\mathcal{C}_R| < 2\}|}{|\text{Total Reviews}|}$$
- **Signal Rule**:
  Flag `Signal 16: stepback_low_evidence_rate` (Severity: WARNING) if:
  $$\text{Total Reviews} \ge 3 \ \land \ \text{Rate}_{\text{low\_ev}} > 0.15 \quad (> 15\% \text{ of reviews lack valid evidence})$$
  *(Note: Reviews with invalid or unlinked citations can occur if runs were pruned, quarantined, or migrated; Signal 16 alerts the researcher to evidence rot).*

### 6.3 Signal 17: `stepback_ignored_escalations`
- **Vulnerability**: Agents ignoring open Level 2/3 holds and continuing to launch runs.
- **Algorithm**:
  For all active Level 2 and Level 3 escalations:  
  Calculate open age: $T_{\text{open}} = \frac{t_{\text{now}} - t_{\text{opened}}}{86400}$.  
  Count subsequent member runs launched in campaign after $t_{\text{opened}}$: $N_{\text{post}}$.  
  An escalation is classified as *IGNORED* if $T_{\text{open}} > 7.0$ days or $N_{\text{post}} \ge 3$.
- **Signal Rule**:
  Flag `Signal 17: stepback_ignored_escalations` (Severity: WARNING) if:
  $$\text{Count}(\text{Ignored Escalations}) > 0$$

### 6.4 Signal 18: `stepback_turnaround_anomalies`
- **Vulnerability**: Instantaneous, cached, or automated copy-paste reviews generated without reading inputs.
- **Algorithm**:
  For reviews authored by automated agents (`reviewer_kind == 'agent'`), compute elapsed turnaround duration:
  $$\Delta t_{\text{review}} = t_{\text{created}} - t_{\text{dispatched}}$$
  Check telemetry events for catalog read operations during the interval $[t_{\text{dispatched}}, t_{\text{created}}]$.
- **Signal Rule & Guard Clauses**:
  1. If `dispatched_at` is `None` (human CLI reviews or manual inputs), or if telemetry logging is disabled (`BTH_NO_TELEMETRY=1`), Signal 18 **skips evaluation** and returns clean status.
  2. If a review includes $\ge 2$ verified primary citations, it is **exempt** from the zero-catalog-reads check.
  3. Flag `Signal 18: stepback_turnaround_anomalies` (Severity: WARNING) only if:
     $$r.\text{reviewer\_kind} = \text{"agent"} \ \land \ r.\text{dispatched\_at IS NOT NULL} \ \land \ \Big( \Delta t_{\text{review}} < 2.0 \text{ s} \ \lor \ (\text{Word Count} > 500 \ \land \ \text{Catalog Reads} = 0) \Big)$$

---

## 7. Tooling Contract: Cyclopts CLI & FastMCP Tool Parity

All operations provide exact parameter and return schema parity across Cyclopts CLI (`bth stepback <subcommand>`) and FastMCP server tools (`stepback_<tool>`).

### 7.1 Command & Tool Parity Matrix

| CLI Subcommand (`bth stepback <subcmd>`) | FastMCP Tool Name | Parameters | Return Schema |
|---|---|---|---|
| `bth stepback trigger <campaign_id>` | `stepback_trigger` | `campaign_id: str`, `window: int = 5`, `dry_run: bool = False` | `{"ok": bool, "triggers_evaluated": dict, "active_triggers": list[str], "fired": bool}` |
| `bth stepback review <campaign_id>` | `stepback_review` | `campaign_id: str`, `path_kind: str`, `rubric_id: str | None = None`, `scores: dict[str, int] | None = None`, `mode: str | None = None`, `narrative: dict | None = None`, `evidence: list[str] = []`, `reviewer_id: str | None = None`, `recommendation: str | None = None`, `dissents: list[dict] = []` | `{"ok": bool, "review_id": str, "composite_score": float | None, "recommendation": str, "escalation_level": int}` |
| `bth stepback status <campaign_id>` | `stepback_status` | `campaign_id: str`, `verbose: bool = False` | `{"ok": bool, "campaign_id": str, "escalation_level": int, "hold_active": bool, "active_triggers": list, "review_count": int, "latest_recommendation": str | None, "epoch": int}` |
| `bth stepback escalate <campaign_id>` | `stepback_escalate` | `campaign_id: str`, `target_level: int`, `reason: str` | `{"ok": bool, "escalation_id": str, "previous_level": int, "current_level": int}` |
| `bth stepback resolve [campaign_id]` | `stepback_resolve` | `escalation_id: str`, `campaign_id: str | None = None`, `as_human_chair: bool = False`, `deescalate_to: int = 0`, `chair_token: str | None = None`, `note: str = ""` | `{"ok": bool, "escalation_id": str, "status": "resolved", "deescalated_to": int}` |
| `bth stepback show <review_id>` | `stepback_show` | `review_id: str`, `json_format: bool = False` | `{"ok": bool, "review": dict}` |

*Parameter Notes*:
- `stepback_review`: `recommendation` is required for Path B, optional (derived) for Path A. `dissents` accepts minority panel dissents.
- `stepback_resolve`: `campaign_id` is optional if `escalation_id` is supplied. `deescalate_to` specifies the target level (0, 1, or 2). FastMCP strictly refuses if the escalation is Level 3.

---

### 7.2 Structured Error Envelopes & BathosErrorCode

All operations return standard Bathos structured error envelopes:
- Success: `{"ok": true, "error_code": null, "error": null, "resolution_hint": null, ...data}`
- Failure: `{"ok": false, "error_code": "<code_str>", "error": "<message>", "resolution_hint": "<hint>"}`

#### BathosErrorCode Additions in `src/bathos/errors.py`:
```python
class BathosErrorCode(str, Enum):
    STEPBACK_NOT_FOUND = "stepback_not_found"
    STEPBACK_INVALID_SCHEMA = "stepback_invalid_schema"
    STEPBACK_ACTIVE_HOLD = "stepback_active_hold"
    STEPBACK_AGENT_OVERRIDE_REFUSED = "stepback_agent_override_refused"
    STEPBACK_INSUFFICIENT_EVIDENCE = "stepback_insufficient_evidence"
    STEPBACK_CITATION_INVALID = "stepback_citation_invalid"
    RUBRIC_NOT_FOUND = "rubric_not_found"
    RUBRIC_INVALID = "rubric_invalid"
    RUBRIC_VERSION_MISMATCH = "rubric_version_mismatch"
    REVIEWER_CONFLICT_OF_INTEREST = "reviewer_conflict_of_interest"
    ESCALATION_LEVEL_INVALID = "escalation_level_invalid"
    CHAIR_TOKEN_INVALID = "chair_token_invalid"
```

#### Domain Exception Mapping in `EXCEPTION_TO_CODE`:
To maintain compliance with `tests/test_errors.py`:
```python
# In src/bathos/errors.py
class StepBackError(BathosError): pass
class StepBackNotFoundError(StepBackError): pass
class StepBackInvalidSchemaError(StepBackError): pass
class StepBackActiveHoldError(StepBackError): pass
class StepBackAgentOverrideRefusedError(StepBackError): pass
class StepBackInsufficientEvidenceError(StepBackError): pass
class StepBackCitationInvalidError(StepBackError): pass
class RubricNotFoundError(StepBackError): pass
class RubricInvalidError(StepBackError): pass
class RubricVersionMismatchError(StepBackError): pass
class ReviewerConflictError(StepBackError): pass
class EscalationLevelInvalidError(StepBackError): pass
class ChairTokenInvalidError(StepBackError): pass

EXCEPTION_TO_CODE.update({
    StepBackNotFoundError: BathosErrorCode.STEPBACK_NOT_FOUND,
    StepBackInvalidSchemaError: BathosErrorCode.STEPBACK_INVALID_SCHEMA,
    StepBackActiveHoldError: BathosErrorCode.STEPBACK_ACTIVE_HOLD,
    StepBackAgentOverrideRefusedError: BathosErrorCode.STEPBACK_AGENT_OVERRIDE_REFUSED,
    StepBackInsufficientEvidenceError: BathosErrorCode.STEPBACK_INSUFFICIENT_EVIDENCE,
    StepBackCitationInvalidError: BathosErrorCode.STEPBACK_CITATION_INVALID,
    RubricNotFoundError: BathosErrorCode.RUBRIC_NOT_FOUND,
    RubricInvalidError: BathosErrorCode.RUBRIC_INVALID,
    RubricVersionMismatchError: BathosErrorCode.RUBRIC_VERSION_MISMATCH,
    ReviewerConflictError: BathosErrorCode.REVIEWER_CONFLICT_OF_INTEREST,
    EscalationLevelInvalidError: BathosErrorCode.ESCALATION_LEVEL_INVALID,
    ChairTokenInvalidError: BathosErrorCode.CHAIR_TOKEN_INVALID,
})

RESOLUTION_HINTS.update({
    BathosErrorCode.STEPBACK_NOT_FOUND: "Verify the review or escalation ID with `bth stepback status <campaign_id>`",
    BathosErrorCode.STEPBACK_INVALID_SCHEMA: "Correct fields in review payload; ensure recommendation is provided for Path B and narrative sections are non-empty",
    BathosErrorCode.STEPBACK_ACTIVE_HOLD: "Address the active Level 2 hold by conducting an independent step-back review before concluding",
    BathosErrorCode.STEPBACK_AGENT_OVERRIDE_REFUSED: "Ask the human committee chair to run `bth stepback resolve --as-human-chair` from an interactive TTY; AI agents cannot resolve Level 3 escalations",
    BathosErrorCode.STEPBACK_INSUFFICIENT_EVIDENCE: "Include at least 2 verified primary catalog citations (e.g. run:<id>, sidecar:<path>#clause) in the review",
    BathosErrorCode.STEPBACK_CITATION_INVALID: "Verify that all cited runs and sidecars belong to the active campaign under review",
    BathosErrorCode.RUBRIC_NOT_FOUND: "Specify a valid rubric ID from `agent_assets/rubrics/` or install the required rubric file",
    BathosErrorCode.RUBRIC_INVALID: "Fix syntax errors in the rubric TOML definition; ensure all 5 dimensions and weights sum to 1.0",
    BathosErrorCode.RUBRIC_VERSION_MISMATCH: "Use the canonical rubric version matching the content hash; do not modify existing rubric files without bumping version",
    BathosErrorCode.REVIEWER_CONFLICT_OF_INTEREST: "Assign review to an independent agent or researcher who did not author runs in this campaign",
    BathosErrorCode.ESCALATION_LEVEL_INVALID: "Specify a valid escalation level between 0 and 3",
    BathosErrorCode.CHAIR_TOKEN_INVALID: "Verify the chair token in `~/.bth/chair_token` or invoke from an interactive TTY",
})
```

---

## 8. Complete Python Dataclasses & Storage Architecture

### 8.1 Python Dataclasses (`src/bathos/stepback/schema.py`)

```python
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional


@dataclass
class ReviewRubricScore:
    """Individual dimension score in Path A rubric evaluation."""
    dimension_id: str
    score: int                           # 1, 2, 3, 4, 5
    rationale: str = ""


@dataclass
class OpenAnalysis:
    """Qualitative narrative sections for Path B cognitive modes."""
    mode: str                            # 'advisor', 'pre_mortem', 'so_what', 'outsider', 'steelman_the_stop'
    sections: dict[str, str] = field(default_factory=dict)


@dataclass
class DissentRecord:
    """Immutable minority dissent record preserved in committee reviews."""
    dissenting_reviewer_id: str
    minority_recommendation: str         # 'CONTINUE', 'PIVOT', 'HOLD', 'ESCALATE', 'CONCLUDE_ABANDON'
    minority_composite_score: Optional[float] = None
    dissent_summary: str = ""
    cited_counter_evidence: list[str] = field(default_factory=list)
    majority_rebuttal: str = ""


@dataclass
class StepBackReview:
    """Full step-back review record persisted to cool and warm tiers."""
    review_id: str                       # rev_<12-hex>
    campaign_id: str
    project_slug: str
    created_at: str                      # ISO-8601 UTC
    reviewer_id: str                     # Authenticated identity string
    reviewer_kind: str                   # 'human', 'agent', 'panel'
    is_independent: bool
    path_kind: str                       # 'rubric', 'open_analysis', 'hybrid'
    input_snapshot_sha256: str           # Cryptographic hash of campaign state
    input_snapshot: str                  # JSON string of input snapshot state
    recommendation: str                  # 'CONTINUE', 'PIVOT', 'HOLD', 'ESCALATE', 'CONCLUDE_ABANDON'
    escalation_level: int                # 0, 1, 2, 3
    cited_evidence: list[str] = field(default_factory=list)
    rubric_id: Optional[str] = None
    rubric_version: Optional[str] = None
    rubric_sha256: Optional[str] = None
    rubric_scores: dict[str, int] = field(default_factory=dict)
    composite_score: Optional[float] = None
    open_analysis: Optional[OpenAnalysis] = None
    dissents: list[DissentRecord] = field(default_factory=list)
    dispatched_at: Optional[str] = None  # ISO-8601 UTC for agent telemetry
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StepBackReview:
        data_copy = dict(data)
        if "open_analysis" in data_copy and isinstance(data_copy["open_analysis"], dict):
            data_copy["open_analysis"] = OpenAnalysis(**data_copy["open_analysis"])
        if "dissents" in data_copy and isinstance(data_copy["dissents"], list):
            data_copy["dissents"] = [
                DissentRecord(**d) if isinstance(d, dict) else d
                for d in data_copy["dissents"]
            ]
        return cls(**data_copy)


@dataclass
class EscalationRecord:
    """Historical escalation event recorded in catalog."""
    escalation_id: str                   # esc_<12-hex>
    campaign_id: str
    project_slug: str
    level: int                           # 0, 1, 2, 3
    status: str                          # 'active', 'resolved', 'superseded'
    opened_at: str                       # ISO-8601 UTC
    opened_by_kind: str                  # 'trigger', 'review', 'manual', 'chair'
    opened_by_review_id: Optional[str] = None
    trigger_name: Optional[str] = None   # 'repeated_marginal', 'near_bound', etc.
    trigger_reason: str = ""
    resolved_at: Optional[str] = None
    resolved_by: Optional[str] = None
    resolved_kind: Optional[str] = None  # 'human', 'chair'
    resolution_note: str = ""
    deescalated_to: Optional[int] = None
    review_epoch: int = 0
    pivot_anchor_run_id: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EscalationState:
    """Current in-memory escalation status for a campaign."""
    campaign_id: str
    current_level: int = 0
    status: str = "nominal"              # 'nominal', 'warning', 'hold', 'escalated'
    active_escalation_id: Optional[str] = None
    active_triggers: list[str] = field(default_factory=list)
    review_epoch: int = 0
    pivot_anchor_run_id: Optional[str] = None
    latest_review_id: Optional[str] = None
    latest_recommendation: Optional[str] = None


@dataclass
class StepBackVerdict:
    """Result of evaluating step-back constraints for a campaign."""
    ok: bool
    hold_active: bool
    escalation_level: int
    recommendation: Optional[str]
    active_triggers: list[str]
    message: str
```

#### Cool-Tier Persistence Helpers
```python
import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path


def save_review_cool(review: StepBackReview, catalog_dir: Path) -> Path:
    """Persist review record atomically to catalog cool tier."""
    out_dir = catalog_dir / "stepback" / "reviews"
    out_dir.mkdir(parents=True, exist_ok=True)
    target_path = out_dir / f"{review.campaign_id}_{review.review_id}.json"
    with tempfile.NamedTemporaryFile("w", dir=out_dir, delete=False, suffix=".tmp") as tf:
        json.dump(asdict(review), tf, indent=2, sort_keys=True)
        temp_name = tf.name
    os.replace(temp_name, target_path)
    return target_path


def save_escalation_cool(esc: EscalationRecord, catalog_dir: Path) -> Path:
    """Persist escalation record atomically to catalog cool tier."""
    out_dir = catalog_dir / "stepback" / "escalations"
    out_dir.mkdir(parents=True, exist_ok=True)
    target_path = out_dir / f"{esc.campaign_id}_{esc.escalation_id}.json"
    with tempfile.NamedTemporaryFile("w", dir=out_dir, delete=False, suffix=".tmp") as tf:
        json.dump(asdict(esc), tf, indent=2, sort_keys=True)
        temp_name = tf.name
    os.replace(temp_name, target_path)
    return target_path
```

---

### 8.2 Warm Tier: DuckDB Relational Projection

Warm-tier tables are created in DuckDB `bathos.db` via `compact.py` using `CREATE TABLE IF NOT EXISTS`:

```sql
CREATE TABLE IF NOT EXISTS stepback_reviews (
    id VARCHAR PRIMARY KEY,
    campaign_id VARCHAR NOT NULL,
    project_slug VARCHAR NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    dispatched_at TIMESTAMP WITH TIME ZONE,
    reviewer_id VARCHAR NOT NULL,
    reviewer_kind VARCHAR NOT NULL,      -- 'human', 'agent', 'panel'
    is_independent BOOLEAN NOT NULL,
    path_kind VARCHAR NOT NULL,          -- 'rubric', 'open_analysis', 'hybrid'
    input_snapshot_sha256 VARCHAR NOT NULL,
    input_snapshot TEXT NOT NULL,        -- JSON snapshot of evaluated inputs
    rubric_id VARCHAR,
    rubric_version VARCHAR,
    rubric_sha256 VARCHAR,
    scores_json TEXT,                    -- JSON dict of dimension scores
    composite_score DOUBLE,
    open_analysis_mode VARCHAR,          -- 'advisor', 'pre_mortem', 'so_what', 'outsider', 'steelman_the_stop'
    open_analysis_json TEXT,             -- JSON dict of section responses
    cited_evidence_json TEXT NOT NULL,   -- JSON array of verified citation references
    recommendation VARCHAR NOT NULL,     -- 'CONTINUE', 'PIVOT', 'HOLD', 'ESCALATE', 'CONCLUDE_ABANDON'
    escalation_level INTEGER NOT NULL,   -- 0, 1, 2, 3
    is_dissent BOOLEAN NOT NULL DEFAULT FALSE,
    parent_panel_id VARCHAR,
    dissents_json TEXT,                  -- JSON array of DissentRecord
    metadata TEXT                        -- JSON arbitrary metadata
);

CREATE TABLE IF NOT EXISTS stepback_escalations (
    id VARCHAR PRIMARY KEY,
    campaign_id VARCHAR NOT NULL,
    project_slug VARCHAR NOT NULL,
    level INTEGER NOT NULL,              -- 0, 1, 2, 3
    status VARCHAR NOT NULL,             -- 'active', 'resolved', 'superseded'
    opened_at TIMESTAMP WITH TIME ZONE NOT NULL,
    opened_by_kind VARCHAR NOT NULL,     -- 'trigger', 'review', 'manual', 'chair'
    opened_by_review_id VARCHAR,         -- NULLABLE (triggers/manual fire before review)
    trigger_name VARCHAR,                -- 'repeated_marginal', 'near_bound', 'entropy_collapse', etc.
    trigger_reason VARCHAR NOT NULL,
    resolved_at TIMESTAMP WITH TIME ZONE,
    resolved_by VARCHAR,
    resolved_kind VARCHAR,              -- 'human', 'chair'
    resolution_note TEXT,
    deescalated_to INTEGER,
    review_epoch INTEGER NOT NULL DEFAULT 0,
    pivot_anchor_run_id VARCHAR,
    metadata TEXT
);
```

---

### 8.3 DuckDB Schema Version 19 & Ingestion Topology

- In `src/bathos/compact.py`, `_migrate_v18(run_dict)` is defined as an identity migration for run Parquet fragments when `CURRENT_SCHEMA_VERSION` is incremented to `"19"`.
- During compaction, `compact.py` invokes `_ingest_stepback_fragments(db, catalog_dir)`, which scans `<catalog_dir>/stepback/reviews/*.json` and `<catalog_dir>/stepback/escalations/*.json` and updates the DuckDB warm tier idempotently via `INSERT OR REPLACE`.
- **Backward Compatibility**: Catalogs on schema v18 continue to operate cleanly; existing DuckDB databases instantiate the tables on first compaction without schema corruption.

---

## 9. Retrospective Validation Plan (R4)

To satisfy Requirement R4, the implementation is validated against three historical catalog campaigns:

1. **`sp98-gap-augmented-testbed` (`6f792f0d`)**:
   - Trajectory: Measurement pipeline contamination; 9 cancelled/preempted runs; 5 guardrail violations.
   - Validation Target: Triggers $T_{\text{repeated\_marginal}}$ and $T_{\text{stale}}$ fire; Path B Pre-Mortem review detects measurement pipeline contamination and flags Level 2 Hold.
2. **`sp91-testbed-first-fix-development` (`ec49fb00`)**:
   - Trajectory: Near-bound mode-disagreement score (0.45 vs. 0.50 cutoff); hidden mixed flavor with co-present basin failure mode.
   - Validation Target: Trigger $T_{\text{near\_bound}}$ fires on $d \le 1.0$; Path B review surfaces minority dissent regarding basin divergence, forcing `PIVOT` recommendation.
3. **`mentee-convergence-deck` (`ff94e8bb`)**:
   - Trajectory: Stalled progress across runs 3–7 with alternating marginal outcomes and metric plateau.
   - Validation Target: Triggers $T_{\text{repeated\_marginal}}$ and $T_{\text{plateau}}$ fire; Path A rubric scores Empirical Progress as 1, triggering hard-floor veto and Level 2 Hold.

Validation sidecar is pre-registered in `scripts/experiments/validate_step_back_reviews.bth.toml`:
```toml
[experiment]
hypothesis = "Step-back review triggers detect 100% of historical campaign anomalies with zero false positives on nominal runs"

[outcomes.pass]
condition = "detected_anomalies == 3 AND false_positives == 0"
decision = "Accept step-back review subsystem for production release"

[outcomes.fail]
condition = "detected_anomalies < 3 OR false_positives > 0"
decision = "Refine trigger parameters and re-test"

[result_schema]
detected_anomalies = "int"
false_positives = "int"
sp98_triggered = "bool"
sp91_near_bound_triggered = "bool"
mentee_plateau_triggered = "bool"
```

---

## 10. Acceptance Criteria Checklist (R1 through R4)

### Requirement R1: Step-Back Trigger and Review Mechanism
- [ ] System-evaluated triggers fire automatically with singularity guard clauses ($N \ge 3$, $N \ge 5$ for entropy/plateau):
  - [ ] Repeated marginal outcomes ($s \ge 3 \lor f \ge 0.60$) with post-pivot epoch filtering
  - [ ] Near-bound hits ($N_{\text{nb}} \ge 2 \lor d_N \le 0.25$) with $B=0$ stddev scaling
  - [ ] Outcome-entropy collapse ($H_e < 0.35$ nats) standardized on natural logarithm
  - [ ] Metric plateaus ($\bar{\beta}_{\text{rel}} < 0.01 \land CV < 0.02$) with near-zero absolute metric guards
  - [ ] Campaign staleness (14d idle / 15 runs claim wheel-spinning / 25 runs uncapped drift) with null-safe timestamps
- [ ] Deterministic metric selection hierarchy for plateau and near-bound triggers.
- [ ] Path A: Scored, versioned rubric:
  - [ ] 5 weighted dimensions on 1–5 scale
  - [ ] Hierarchical gap-free recommendation logic covering all $[1.0, 5.0]$ composite scores
  - [ ] Hard-floor veto rule: score of 1 caps recommendation at `HOLD` and forces Level 2 hold
  - [ ] Versioned TOML specs under `agent_assets/rubrics/` with SHA-256 locking
  - [ ] Cryptographic input snapshot binding (`input_snapshot_sha256`)
- [ ] Path B: Structured open analysis:
  - [ ] 5 cognitive modes: Advisor Conversation, Pre-Mortem, So-What Test, Outsider Read, Steelman-The-Stop
  - [ ] Explicit required `recommendation` parameter
  - [ ] Campaign domain citation validation (`run:<id>` in campaign, `sidecar:<path>` in campaign)
  - [ ] Citation verification against catalog ($N_{\text{valid}} \ge 2$ required)

### Requirement R2: Reviewer Independence, Escalation Ladder, and Non-Blocking Posture
- [ ] Reviewer independence bound to authenticated runtime identity; self-review emits `REVIEWER_CONFLICT_OF_INTEREST` and cannot discharge holds.
- [ ] Minority dissent preserved in immutable `dissents` array; minority recommendation of `HOLD`/`ESCALATE` places campaign in automatic Level 2 Advisory Hold.
- [ ] 4-tier escalation ladder (Levels 0–3) with bidirectional state transitions and direct paths (0->2, 0->3, 1->3).
- [ ] Human Chair Invariance:
  - [ ] FastMCP `stepback_resolve` strictly refuses Level 3 resolutions unconditionally
  - [ ] Level 3 resolution requires interactive TTY (`sys.stdin.isatty()`) or secure chair token (`~/.bth/chair_token`)
  - [ ] Autonomous agent caller environments rejected with `STEPBACK_AGENT_OVERRIDE_REFUSED`
- [ ] Harmonized Decision D1 posture:
  - [ ] Default advisory warnings at `bth run` and `bth submit`
  - [ ] Conclusion verdict downgraded to `confounded` by default on confirmation/sequential campaigns with active holds
  - [ ] Opt-in execution blocking via `[stepback] enforce = true` / `BTH_STEP_BACK_ENFORCE=1`

### Requirement R3: Anti-Rubber-Stamp Meta-Signals and Tooling Integration
- [ ] Automated surveillance meta-signals integrated into `sprint-audit`:
  - [ ] Signal 15: `stepback_unvarying_verdicts` ($\sigma_S^2 < 0.005 \lor U_{\text{rec}} = 1.0$)
  - [ ] Signal 16: `stepback_low_evidence_rate` ($> 15\%$ reviews with $< 2$ valid citations or citation rot)
  - [ ] Signal 17: `stepback_ignored_escalations` (Active Level 2/3 open $> 7$ days or $\ge 3$ subsequent runs)
  - [ ] Signal 18: `stepback_turnaround_anomalies` (gated by `dispatched_at` timestamp and telemetry for agent reviews only)
- [ ] Full tooling parity across Cyclopts CLI (`bth stepback *`) and FastMCP server (`stepback_*`).
- [ ] Error envelopes governed by `BathosErrorCode`, mapped domain exception classes in `EXCEPTION_TO_CODE`, and `RESOLUTION_HINTS`.
- [ ] Backward-compatible DuckDB schema v19 migration with cool JSON atomic persistence and nullable `opened_by_review_id`.

### Requirement R4: Execution Isolation, Retrospective Validation, and Review Gates
- [ ] All work executed in isolated git worktree `.claude/worktrees/261027-step-back-reviews`.
- [ ] Primary checkout and real `~/.bth` catalog completely untouched.
- [ ] Tests run via targeted pytest invocations (narrow selections / `-k` filters only).
- [ ] Retrospective validation pre-registered in `scripts/experiments/validate_step_back_reviews.bth.toml` against 3 historical campaigns.
- [ ] Adversarial review cycle achieves 0 Blocker / 0 Major findings.

---

*Specification revised for Task 261027_step-back-reviews resolving all Phase 3 review findings.*
