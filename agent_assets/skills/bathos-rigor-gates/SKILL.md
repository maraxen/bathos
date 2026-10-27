---
name: bathos-rigor-gates
description: Post-Union-Gate confirmatory rigor — review coverage, post-mortem obligations, instrument-sensitivity positive controls, synthetic-recovery gates, negative-outcome hedging, the rule-card corpus, step-back review holds (Decision D1), blind reviewer protocols, and multi-parent lineage/stats tooling for bathos campaigns
when_to_use: 'Triggers: review_tier, obligations, positive_control, differential, synthetic_recovery, bth gate, negative_check, bth ref, campaign_edges, stats_gates, capability_probe, review coverage, obligation ledger, rule card, stepback, step-back review, blind reviewer, escalation hold, decision D1.'
---
# bathos-rigor-gates

A second layer of confirmatory-campaign machinery that sits **on top of** the Union Gate,
claim files, and confounds documented in **bathos-campaigns** — read that skill first if you
haven't registered a claim before. Everything here either (a) fires automatically at `bth run`
or `bth campaign conclude` once opted in, (b) is a modifier on a `[[claim.union_gate.clauses]]` entry
bathos-campaigns already documents, or (c) is a standalone query surface (`bth ref`, `bth gate`)
with no campaign dependency at all.

**Ships silently.** All of it is opt-in, off by default, and none of it appears in
bathos-campaigns. A confirmation campaign with no `[obligations]`/`[claim]` flags set in
`.bth.toml` behaves exactly as bathos-campaigns describes.

## Conclude-time gate ordering

`bth campaign conclude` runs these checks in this order, when a claim is registered
(confirmation/sequential mode only unless noted):

0. **Negative-check hedging** (BP-3) — before anything else runs, if `--outcome` is
   negative-sounding and a claim is registered, `--negative-check` is required or the command
   refuses outright (see Negative-Outcome Hedging below)
1. Parity confound check (bathos-literature-parity)
2. **Review Coverage Gate** — every hypothesis/confound needs a `[review]` entry
3. **Synthetic-Recovery confound check** (BP-2) — any `[confounds.synthetic_recovery]` block must
   be `GREEN` or the confound is `"uncontrolled"` (see Synthetic-Recovery Gate below) — this runs
   **before** Union Gate, so a campaign can already be downgraded here without Union Gate ever
   evaluating clause coverage
4. **Union Gate** (bathos-campaigns) — clause coverage, with `positive_control` clauses checked
   against the differential pre-flight instead of plain `claim_discriminates` coverage
5. **Obligation trigger 4** (`citation_contradicted`) — a `supports` review entry the outcome disfavors
6. Claim-coverage JSON sidecar emitted
7. **Obligation Gate** — any open obligation on the campaign or its member runs
7.5. **Step-Back Review & Escalation Gate (Decision D1)** — if an active Level 2 Hold or Level 3
   Human Chair Escalation exists: for confirmation/sequential campaigns, outcome is downgraded to
   `"confounded"` unless `--force-verdict` / `--force` is supplied (which records a `bypassed`
   state); for exploration campaigns, prints a warning banner with no downgrade (see below)
8. **Obligation trigger 2** (`campaign_confounded`) — opened *after* step 7 and 7.5, so a downgrade can't
   trigger an obligation that then downgrades itself

Each of 2, 3, 5, and 7 is independently opt-in and prints a `WARNING:` with no verdict change when
its flag is unset — the same "observe, then enforce" posture bathos-campaigns uses for the Union
Gate's own `--force`.

*(Adversarial review 260828 caught this table originally omitting steps 0 and 3 entirely —
verified against `campaigns.py:760-1184`'s actual `conclude_campaign` execution order before
finalizing; if further work touches `conclude_campaign`, re-verify this ordering rather than
trusting this table indefinitely.)*

## Review Coverage Gate

`[review]` is a sidecar block — declared per-run, not part of the claim file itself:

```toml
[[review.literature]]
ref        = "10.1234/example.2024"     # DOI, arXiv id, or a bth run UUID
claim      = "prior work reports X scales monotonically with signal"
bears_on   = "H_main_effect"            # hypothesis/confound id from the claim file
disposition = "supports"                # supports | contradicts | scope-differs
checked    = "2026-08-20"

[[review.implementation]]
source            = "https://github.com/org/repo/blob/abc123/model.py"
commit            = "abc123"
what_was_checked  = "loss function matches published formula, no undocumented regularizer"
bears_on          = "C_baseline"
disposition       = "matches"           # matches | diverges | not-applicable
```

Tier is **derived, never declared**:
- `""` (none) — no `[review]` block, or an empty one
- `C0` — a citation with no substantive check (missing required fields)
- `C1` — a real entry: literature needs `ref` + `bears_on` + `disposition`; implementation needs
  `source` + `commit` + `what_was_checked`
- `C2` (parity) — not derivable from a sidecar at all; earned only by the full
  literature-parity audit (**bathos-literature-parity**)

**Coverage** is stricter than tier: an entry only counts if it is C1 **and** names the
hypothesis/confound it bears on. A `bears_on`-only entry is C0, not coverage.

At `bth campaign conclude`, for confirmation/sequential campaigns only: every hypothesis and
confound id in the registered claim must be the `covering_id` of at least one `[review]` entry
across the campaign's member-run sidecars. An **empty claim** reports `empty_slate`, never
`covered`.

```toml
# .bth.toml
[claim]
review_coverage_enforce = true    # or BTH_REVIEW_COVERAGE_ENFORCE=1
```

Off (default): warns, verdict unchanged. On: forces `outcome_label` to `confounded`.

## Post-mortem Obligations

Five triggers, four opt-in and `near_bound` default ON, each writing one JSON file to
`.bth/obligations/<kind>_<entity_id>_<trigger>.json`:

| Trigger | Fires when | Flag |
|---|---|---|
| `outcome_failed` | a run's computed outcome lands outside the pass direction | `BTH_OBLIGATION_OUTCOME_FAILED` |
| `adversarial_check_fired` | the selected branch's `adversarial_check` evaluates FALSE | `BTH_OBLIGATION_ADVERSARIAL_CHECK_FIRED` |
| `campaign_confounded` | a campaign concluded `confounded` | `BTH_OBLIGATION_CAMPAIGN_CONFOUNDED` |
| `citation_contradicted` | a `[[review.literature]]` entry with `disposition = "supports"` cites a hypothesis the observed outcome disfavors | `BTH_OBLIGATION_CITATION_CONTRADICTED` |
| `near_bound` | a clean run's `[breakdown]` aggregate sits at its `bound` (one of >=2 units over, or within `margin`); **default ON**, set `=0` / `near_bound = false` to opt out; discharged by a format-2 postmortem `[fork]`, see **using-bathos** (Distributional Breakdown) | `BTH_OBLIGATION_NEAR_BOUND` |

Resolution order: env var → `.bth.toml [obligations] <trigger>` → off (on for `near_bound`). Opening is idempotent per
`(entity_kind, entity_id, trigger)`. `outcome_failed`/`adversarial_check_fired` open at `bth run`
end; `campaign_confounded`/`citation_contradicted` open at `bth campaign conclude`.

This project's own `.bth.toml` runs with three of the four enabled and enforcement on. `enforce`
gates the downgrade separately from opening: unset → warn only; `enforce = true` → any open
obligation scoped to the campaign or its member runs downgrades a confirmation/sequential verdict
to `confounded`. `bth run` always warns on open obligations, any mode, any flag state — it never
blocks.

**Discharging an obligation:**
```bash
bth postmortem scaffold --campaign-id <campaign-id>   # lists this scope's open obligation ids
# ...author the postmortem, adding the listed ids to its `discharges` array...
bth postmortem validate <path>                        # only a VALID postmortem discharges anything
```
There is no `bth obligations list` command and no dedicated MCP tool — inspect
`.bth/obligations/*.json` directly, via `bth postmortem scaffold`, or via
`bathos.obligations.list_obligations`/`list_obligations_for_scope` from Python.

## Instrument-Sensitivity Pre-Flight (`[differential]`) and `positive_control`

```toml
[differential]
knob   = "SIGNAL_STRENGTH"     # env var or CLI arg toggled between the two probe runs
off    = "0.0"
on     = "1.0"
expect = "differs"             # "differs" or "identical"
metric = "recovery_hamming"    # optional: compare a result_schema key numerically
min_effect = 0.05              # required when metric is set
```

`bth run` executes this **before** the main script: once at `off`, once at `on`. If the declared
invariant doesn't fire, the main subprocess never runs — the run is recorded with
`outcome = "invalid_measurement"` instead. A passing pre-flight sets `differential_status =
"passed"`.

`positive_control` is a modifier on a `[[claim.union_gate.clauses]]` entry bathos-campaigns already documents:

```toml
[[claim.union_gate.clauses]]
id             = "C_instrument_sensitivity"
description    = "the pipeline can detect a known-real effect"
hypothesis_ids = []                 # ignored for a positive_control clause
positive_control = true
```

For a `positive_control` clause, the Union Gate does not check `hypothesis_ids` coverage — it
checks whether some covering run has BOTH `differential_status == "passed"` AND a
`dependency_lock_sha256` still matching the current `uv.lock` (a re-pin since the pre-flight
invalidates the proof). This is unconditional, built into `run_union_gate` — no separate opt-in flag.

Required when the claim declares a genuinely falsifiable-by-null kill condition:

```toml
[claim]
kill_condition_satisfiable_by_null = true   # AC-23
```

If `true`, `bth claim validate` errors unless at least one clause carries `positive_control = true`.

`bth sprint-audit` reports uncontrolled `positive_control` clauses as `differential_staleness_count`
— a leading indicator, doesn't itself gate anything.

## Synthetic-Recovery Gate (`bth gate`)

```bash
bth gate stamp my_invariant_test --result pass     # after YOUR test passes, at current git HEAD
bth gate status my_invariant_test --guard src/model/loss.py --guard src/model/train.py
```

`status` reports `UNKNOWN` (never stamped) / `RED` (last result `fail`) / `STALE` (last result
`pass` but a guarded path changed since) / `GREEN` (pass, nothing changed) — exits non-zero for
anything but GREEN. Ledger: `.bth/synthetic_recovery_ledger.json`. Self-attested by design — the
trust model is "you ran your own test."

```toml
[[confounds]]
id    = "C_pipeline_soundness"
label = "the analysis pipeline recovers a known-planted signal"
[confounds.synthetic_recovery]
gate_name = "my_invariant_test"
guards    = ["src/model/loss.py", "src/model/train.py"]
```

Controlled at conclude iff `gate_state == "GREEN"` — anything else fails closed to
`"uncontrolled"`.

## Negative-Outcome Hedging (`--negative-check`)

When a claim is registered and `--outcome` matches the negative-outcome vocabulary
(`fail(ed)?|falsified|void|no-?go|not-a-fair-test|dead-?end|reversed|null|neutral|marginal`,
overridable via `.bth.toml [claim] negative_outcome_pattern`), `bth campaign conclude` requires:

```bash
bth campaign conclude <campaign-id> --outcome no-go \
  --negative-check "ruled out via 3 independent seeds at 2x the planned effect size; not a power failure"
```

Fires *before* the Review Coverage Gate, Union Gate, or Obligation Gate. Opt-in on claim
registration — a campaign with no claim never hits this regardless of outcome label.

## Rule-Card Corpus (`bth ref`)

20 shipped rule cards (`agent_assets/corpus/<domain>/<ID>_<slug>.md`), independently citable by id:

```bash
bth ref list                          # every card, one line each, severity + title
bth ref show DSGN-001                 # full card
bth ref search "positive control"     # substring match
bth ref applicable <script> [--show-context]
```

`applicable` evaluates every card's `applies_when` (DuckDB scalar SQL) against a context row
built from the script's sidecar. A malformed card is skipped and reported, never fatal.
`source_check` on a card is documentation, not enforcement — a card wired into `bth lint` will
independently surface there too; `bth ref applicable` itself never blocks anything.

## Multi-Parent Campaign/Run DAG (library-only — no CLI/MCP surface yet)

```python
from bathos.campaign_edges import add_run_edge, get_run_parents, CycleRejectedError

add_run_edge(db, child_run_id="run_b", parent_run_id="run_a")
get_run_parents(db, "run_b")   # -> ["run_a"]
```

Additive alongside the existing single-parent `parent_campaign_id`/`parent_run_id` columns.
Cycles (including self-loops) rejected via `CycleRejectedError`.

**Verified gap — not wired to anything user-facing today.** Neither `bth lineage --format prov`
nor the `lineage_prov` MCP tool passes `run_parent_edges` into `format_prov_json` — today's
output is always single-parent regardless of what's recorded. `bth lineage --format dot` is
listed in `--help` but **not implemented** (`exit 1`, "dot format not yet implemented"). Reading
multi-parent edges requires calling the Python functions directly.

## Statistical Battery (`bathos[stats]`, library-only) and `capability_probe`

```python
from bathos.stats_gates import run_stats_battery

verdict = run_stats_battery(
    candidate_values=[0.82, 0.79, 0.85, 0.81],
    baseline_values=[0.71, 0.70, 0.73, 0.69],
    higher_is_better=True,
)
# verdict.verdict: "pass" | "confounded" | "underpowered"
```

Requires `uv tool install 'bathos[stats]'` (scipy); without it, degrades to
`verdict="underpowered"` rather than raising.

**Verified gap — deliberately not wired into `bth campaign conclude`.** `Run.metadata` (which a
per-run-metric integration would need) is never actually populated in bathos's live write path —
this is a manual toolkit, not an automatic campaign gate.

`capability_probe` (MCP tool only) checks whether a bathos instance can support either of the
above (`seed_live`, `stats_battery_live` + unavailable reason).

## Decision D1 Posture & Step-Back Review Gate

In accordance with bathos Decision **D1** (`src/bathos/obligations.py`: obligations and rigor gates never block execution by default; they warn or downgrade, with blocking strictly opt-in), the Step-Back Review subsystem enforces:

1. **Non-Blocking Execution (Default)**:
   - When a campaign is placed under Level 1 Advisory Warning, Level 2 Step-Back Hold, or Level 3 Human Chair Escalation, running member experiments (`bth run`, `bth submit`) or adding runs (`bth campaign add`) is **never blocked**.
   - Instead, the system prints a prominent warning banner to stderr:
     ```
     WARNING: Active step-back Step-Back Hold (Level 2) on campaign 6f792f0d! Reason: repeated_marginal
     ```
   - This posture prevents deadlocks during exploratory parameter sweeps or intentional stress tests.

2. **Opt-in Execution Enforcement**:
   - For high-stakes or budget-capped campaigns, strict blocking can be enabled in `.bth.toml`:
     ```toml
     [stepback]
     enforce = true  # or BTH_STEP_BACK_ENFORCE=1
     ```
   - When `enforce = true`, `bth run` and `bth submit` refuse execution if an active Level 2 or Level 3 hold is open, returning exit code 1.

3. **Conclusion Verdict Downgrade**:
   - At `bth campaign conclude`:
     - **Confirmation & Sequential Campaigns**: If an unaddressed Level 2 Hold or Level 3 Escalation is active, the campaign's conclusion outcome is automatically downgraded to `"confounded"`, regardless of the user-supplied `--outcome`.
     - **Bypass Mechanism**: Passing `--force-verdict` (or `--force`) overrides the downgrade and allows conclusion under the specified outcome, while logging `stepback_status = "bypassed"` in the catalog record.
     - **Exploration Campaigns**: Never downgraded; emits an advisory warning banner.

## The Blind Reviewer Protocol

A core lesson from bathos and praxia history is that **"an agent's report is a claim, not evidence"** (praxia lessons #46, #51, #134, #310, #435, #471, #477, #521) and that **"a proposer should not grade their own work"** (praxia #246, where an independent refuter found six load-bearing errors, all in the author's favor). Furthermore, mechanizing a lesson often moves the failure mode: agents game visible gates by inventing arbitrary passes or negating check conditions (e.g. self-negating adversarial checks in `bth new-experiment`, `CHANGELOG.md` lines 740–748).

The **Blind Reviewer Protocol** eliminates these failure modes through rigorous isolation:

### 1. Operationalizing Reviewer Independence
- **Attribution Barrier**: The reviewer's authenticated identity (`reviewer_id`) must not match any entity in the campaign author set $\mathcal{A}_{\text{forbidden}}$:
  - The campaign creator.
  - Any agent session ID that authored or executed member runs in the campaign.
  - The author of any git commits modifying experiment scripts during the campaign window.
- If $R_{\text{id}} \in \mathcal{A}_{\text{forbidden}}$, the review is marked `is_independent = false`, emits `REVIEWER_CONFLICT_OF_INTEREST`, and **cannot** discharge holds or de-escalate the campaign.

### 2. Primary Record Ingestion (Blindness to Advocacy)
- The blind reviewer **does not read** the doing agent's summary narrative, chat justification, or advocacy.
- Instead, the reviewer evaluates **primary catalog records directly**:
  - Raw member runs (`run_*.parquet` and warm `runs` table in `bathos.db`).
  - Pre-registered sidecars (`*.bth.toml`) and outcome SQL fragments.
  - Registered claim definitions, clauses, and literature reviews.
  - Output artifacts and metric distributions.
- **Evidence Citation Requirement**: Every assertion in an open analysis review (Path B) must cite verifiable primary entities:
  - `run:<uuid>` (must be a valid member run of the campaign)
  - `sidecar:<path>#<clause>` (must match an executed script)
  - `clause:<path>#<id>` (must match the registered claim)
  - At least 2 valid primary citations ($N_{\text{valid}} \ge 2$) are required, or the review is rejected (`STEPBACK_INSUFFICIENT_EVIDENCE`).

### 3. Protein Science Domain Failure Checks
Reviewers are prompted with targeted domain diagnostic inquiries that written rules alone fail to prevent (research grounding §9 & §10):
- **Replicate Pooling vs Convergence (#1 Domain Failure)**:
  - In computational biophysics and molecular dynamics, simulation replicates are samples of a single thermodynamic ensemble that are intentionally pooled/concatenated—**not** independent experimental units that must each converge.
  - The reviewer explicitly asks: *"What is the unit of independence here, and what does pooling assume?"*
  - Flags any campaign that treats replicate spread as experimental uncertainty or gates on per-replica convergence.
- **Checks That Cannot Fail or Discriminate**:
  - Flags census fields that are structurally 0, tautological controls, primary metrics sharing formulas with optimization objectives (trusted-broken-pipeline), and richer models winning on shuffled data.
- **Variance Inflation & Noise Floors**:
  - Distinguishes between-build vs within-build variance (e.g. 5.7x inflation in sweetprots) and verifies paired effects are not gated against irrelevant unpaired floors.
- **Silently Inert Gates or Knobs**:
  - Flags campaigns where a null result is accepted without a positive control proving the knob/instrument actually moves the output.

### 4. Preservation of Minority Dissent
- When review panels evaluate a campaign, consensus aggregation must never erase minority critiques.
- Review records embed an immutable `dissents` array capturing the dissenting reviewer's ID, recommendation, rationale, and cited counter-evidence.
- **Dissent Escalation Trigger**: If any panel reviewer recommends `HOLD`, `ESCALATE`, or `CONCLUDE_ABANDON`, the campaign is placed under an automatic Level 2 Advisory Hold, ensuring that minority warnings cannot be outvoted or buried.

## What could not be fully verified (flag before treating as settled)

- `signal_open_obligation_age`'s own docstring claims "Signal 11," but `bathos.sprint_audit`'s real
  Signal 11 is `differential_staleness_count` — no call site wires the obligation-age function
  into `bth sprint-audit`'s actual output. Treat it as a standalone Python function you call
  directly, not something the CLI currently reports. **This looks like a genuine source bug
  (dead wiring / numbering collision), not just a doc gap — worth a real backlog item, separate
  from this skill-writing pass.**
- `[differential]`'s two-invocation subprocess pre-flight was read from source, not exercised
  end-to-end live.
- AC-23's exact validation error text was read from source, not triggered live.
- `capability_probe`'s exact MCP return shape was read from source, not called live.
- (Pre-existing bug in a *different* skill, flagged here since found during this research, not
  fixed here): `bathos-campaigns` documents `bth campaign conclude --force-verdict`, but the real
  flag is `--force` — see that skill's own edit plan, Finding 6b.

## Related

- **using-bathos** — sidecar basics (`[experiment]`, `[controls]`, `stage_name`).
- **bathos-campaigns** — campaigns, claim-tier pre-registration, Union Gate basics, hypotheses/
  confounds/discriminability, probe design. Read first.
- **bathos-step-back** — dedicated skill for step-back review orchestration, rubric dimensions, cognitive modes, and blind reviewer protocol.
- **bathos-literature-parity** — the C2/`parity` review tier and `[confounds.reference_parity]`.
- **bathos-mcp** — error envelope shape for `capability_probe` and other MCP tools named above.

