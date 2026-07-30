# bathos long-horizon rigor — claim-level pre-registration, variable isolation, and the union gate

**Date:** 2026-06-16
**task_id:** 260616_sp67-verdict-design
**Provenance:** Seeded into bathos `.praxia/docs/research/` on 2026-06-16 from the asr worked example (`asr:.praxia/docs/reference/260616_bathos-long-horizon-rigor.md`). **This copy is the input for a bathos research → brainstorm → spec (adversarial review) → coherence pass to produce a backlog DAG for the systematization.** All `decisions/`, `research/`, `specs/`-relative cross-refs below point to the *asr* repo (the motivating worked example), not bathos-internal docs.
**Status:** PROPOSAL for a `/using-bathos` methodology layer **+ enforcement/tooling design**. Drafted in asr because asr produced the canonical failure that motivates it. §3–§6 = the rigor artifacts/rubrics (→ skill section); **§8–§9 = the tooling-and-enforcement design and a ready-to-log bathos backlog chain** (added 2026-06-16 per PI). MVP = update the skill (L0) + the two catch-the-failure lints (L2); heavier tooling is a staged bathos effort. **§10 scopes adjacent rigor concerns to systematize** — baseline/literature-reimplementation fidelity (the "is our reshuffle really Zeinaty's?" problem) and signal-discrimination via structured/scaled perturbations & probes. Port target: `projects/bathos/agent_assets/skills/using-bathos/SKILL.md`.
**Grounding:** NLM `3f0490aa` (agentic-science bathos), `1dc79999`, `ebf033db`, `9e443060`; skill audit `~/.claude/skills/using-bathos/SKILL.md`; worked example `decisions/260616_sp67-claim-and-controls-design.md`.

---

## 1. The gap, stated precisely

bathos enforces rigor at exactly one level: **the run.** A sidecar pre-registers a hypothesis, `[outcomes.{pass,marginal,fail}]` DuckDB conditions, and a result schema, enforced at `bth run`. The **Campaign** is only a *container* (`create/add/review/conclude`, name + description + a `--question` string + a figure manifest). **There is no pre-registration, no hypothesis decomposition, no variable-isolation discipline, and no "did the union actually establish the headline" gate at the campaign/epic level.**

That missing tier has a named failure mode — **Objective Drift** (NLM `3f0490aa`): a program is decomposed into narrow sub-tests, each sub-test optimizes an easier local metric and passes, and the union is *claimed* to establish the headline though it does not.

**Worked example (asr EPIC-6).** Five sprints (SP-6.1..6.5) each closed `pass` on a single favourable cell (K=4, μ=10). The disconfirming regime (μ=5) was *pre-registered out* of the boundary map. The first time anyone asked "does the union establish the headline?" — the N=30 confirmatory run — the answer was **falsified**. Every per-run sidecar was honest; the *claim* was never the unit of rigor. See `decisions/260616_sp67-claim-and-controls-design.md §1`.

**Thesis: the unit of pre-registration must rise from the run to the claim.**

## 2. Two-tier pre-registration

Adopt **sequential / programmatic pre-registration** (a decision-tree, not a single frozen doc, because early results legitimately reroute later branches — NLM `3f0490aa`):

- **Claim tier (campaign/epic):** a `claim` pre-registration — *what would make the headline true, and what would kill it.* Lives once per campaign; updated only by explicit amendment (logged).
- **Run tier (sidecar):** unchanged in spirit, **plus** each run declares *which claim-tier hypothesis it discriminates* and *which one variable it isolates.* A run that can't name either is exploratory, not confirmatory.

## 3. The four claim-tier artifacts

### 3.1 Claim Ledger (the hypothesis slate)
- **Headline claim**, stated as a falsifiable proposition with its **kill condition** (the result that would falsify it) written *before* any run.
- **Hypothesis slate**: ≥2 competing hypotheses **plus an explicit "third alternative" slot** = "both wrong / the model (or the measurement) is misspecified." (NLM `3f0490aa`: hypothesis hygiene.)
- **Assumption Ledger**: the load-bearing assumptions the whole campaign rests on (e.g. "the generator is a faithful proxy", "the optimizer converges", "the two methods have symmetric information"). **If one is later falsified, the campaign halts** — it does not silently proceed. (asr: "MTT and reshuffle have symmetric information" was an *unstated* assumption and was false — `λ_potts=0` vs DCA-aware acceptance.)

### 3.2 Discriminability Map (the hypothesis × outcome matrix)
A table: rows = hypotheses in the slate, columns = the planned runs, cells = the *predicted* outcome under each hypothesis. **If two hypotheses predict the same outcome for a run, that run has zero discriminative power — flag it and redesign.** (NLM `3f0490aa`: discriminative-power check.) This is what forces "test the claim across the parameter space," not at one cell: a single favourable cell usually fails to discriminate the headline from "advantage exists only in this corner."

### 3.3 Confound / Variable-Isolation Register
One row per known confound: *name → how it is controlled → which run isolates it → status.* A claim cannot be marked established while any load-bearing confound is `uncontrolled`. The discipline: **one experiment moves one variable.** (asr had six interacting confounds — information symmetry, topology, model class, optimizer, baseline strength, divergence regime — none registered, several pulling opposite ways.) Include **outcome-neutral / potency controls** ("chastity vs impotence"): a positive control proving the apparatus works, so a null is a real null and not a broken assay or a bug (the SP-0.6 temperature-inversion bug is the archetype).

### 3.4 Union Gate (enforced at `bth campaign conclude`)
Before a campaign may conclude with a positive verdict, it must pass a **backward trace**: every clause of the headline claim must point to a specific run whose `[outcomes]` actually tested *that clause*, in the regime the claim asserts. A claim clause with no such run is an **"unverifiable inference chain"** and blocks the positive conclusion (NLM `3f0490aa`: semantic provenance + credit-ladder). Corollary — the **credit ladder**: a *mechanistic/causal* headline cannot be assembled from a union of *narrow observational passes*; it needs an intervention/ablation run. (asr: five "advantage-exists" cells never amounted to "the mechanism holds.")

## 4. Two rubrics (the checklists that gate plan → run → claim)

### PLAN-gate (before the first confirmatory run)
1. Is the **headline claim** written as a falsifiable proposition with an explicit **kill condition**?
2. Does the **hypothesis slate** have ≥2 competitors **and** a third-alternative ("misspecified") slot?
3. Is every **load-bearing assumption** in the Assumption Ledger, each with a halt-trigger?
4. Does the **Discriminability Map** show every confirmatory run discriminates ≥2 hypotheses? (Flag zero-power runs.)
5. Is every known **confound** registered with an isolating run? Is there a **potency/positive control**?
6. Does the plan **stratify the full parameter space** the claim ranges over (no claim read off one cell)? Is the **disconfirming regime included**, not excluded?
7. **Stage 0 first:** are the measurement-pipeline invariants (does the metric return the known answer on planted ground truth?) scheduled *before* any method comparison?

### CONCLUDE-gate (before a positive verdict)
1. **Union trace:** does every clause of the headline map to a run that tested it *in the asserted regime*? (else block)
2. Did any **assumption** in the ledger get falsified mid-campaign? (if yes → halt/re-scope, not conclude)
3. Are all **load-bearing confounds** `controlled`? (else the result is `confounded`, not `pass`)
4. For a **mechanistic** claim: is there an **ablation/intervention** run, not just observational passes?
5. Were verdicts read from **raw result data**, not from an agent's summary of the data? (defeats the "eureka instinct.")
6. Does the claim hold in the **sweet spot**, or only at a boundary/saturation regime? (label saturation-only wins as such.)

## 5. Fail-fast staging (the campaign shape)

Replace "build everything, then confirm" with a **tree search that prunes early** (NLM `3f0490aa`):
- **Stage 0 — measurement-pipeline gates.** Synthetic-invariant tests: planted ground truth → metric returns the known answer (error≈0, ECE≈0, brute-force-correct). **Any failure stops the campaign.** Cheapest, most decisive, and the step skipped most often.
- **Stage 1 — one decisive cell in the sweet spot, all load-bearing confounds controlled.** If the headline can't survive here, **kill it now.**
- **Stage 2 — full stratified sweep**, N adequate, disconfirming regime included.
- **Stage 3 — ablations** for causal necessity.

## 6. Kill discipline
Severity-rank anomalies. When a load-bearing assumption fails a sanity/scale check, **justify a Kill** for that hypothesis rather than tweaking a broken experiment (avoid the sunk-cost loop — cf. the BATHOS rule "if you've falsified 3+ hypotheses in a row and each is numerically suspicious, stop and verify the measurement"). A falsification that survives controls is a *result*, not a failure — bank it (e.g. a boundary/null map).

## 7. Concrete schema (optional CLI/format extension)

A campaign-level pre-registration file, parallel to the sidecar, e.g. `campaign.<id>.bth.toml` (or `.bth/catalog/sidecars/<campaign_id>/claim.toml`):

```toml
[claim]
headline    = "<falsifiable proposition>"
kill_condition = "<result that would falsify it>"      # written before any run
regime      = "<parameter range the claim ranges over; the sweet spot it must hold in>"

[[hypotheses]]                                          # >= 2 + a third-alternative
id = "H1"; statement = "..."; predicted_signature = "..."
[[hypotheses]]
id = "H0_misspec"; statement = "both wrong / measurement misspecified"; predicted_signature = "..."

[[assumptions]]                                         # load-bearing; halt on falsify
id = "A1"; statement = "generator is a faithful proxy"; halt_if = "self-consistency slope != 1"; status = "untested"
[[assumptions]]
id = "A2"; statement = "compared methods have symmetric information"; halt_if = "one method uses J_ij the other lacks"; status = "untested"

[[confounds]]                                           # one row per confound
id = "C1"; name = "information symmetry"; control = "factorial lambda_potts x reshuffle-info"; isolating_run = ""; status = "uncontrolled"

[discriminability]
# rows = hypotheses, cols = planned runs, cells = predicted outcome; flag zero-power runs
matrix = "..."   # or a path to a generated table

[union_gate]
# each headline clause -> run_id that tested it in-regime; conclude blocks if any unmapped
clauses = []
```

Per-run sidecars gain two fields tying them up to the claim tier:
```toml
[experiment]
discriminates = ["H1", "H0_misspec"]   # which slate hypotheses this run separates
isolates      = "C4"                    # the one confound/variable this run moves
```

Enforcement points (no new infra required to start — can be a `bth lint`/review-checklist before it's code):
- `bth campaign create` → require a `[claim]` block (or warn `BYPASSED`, mirroring `--no-sidecar`).
- `bth run` → warn if a confirmatory run declares no `discriminates`/`isolates`.
- `bth campaign conclude` → run the **Union Gate** + CONCLUDE-gate rubric; refuse a `pass` verdict with unmapped clauses, uncontrolled load-bearing confounds, or a falsified assumption.

## 8. Enforcement & tooling — what bathos could deploy

A rubric that lives only in a doc decays to a ritual. The design goal is to push each rigor item **down the automation ladder** to the cheapest mechanism that still bites — and to **reuse bathos primitives that already exist** rather than build new infrastructure. bathos already gives us the enforcement vocabulary:

- **artifact-required-at-lifecycle-event** (sidecar required at `bth run`),
- **bypass-with-log** (`--no-sidecar` → `BYPASSED` row; discipline through visibility, not hard walls),
- **static lint over the warm catalog** (`bth lint`),
- **conclude-time emission** (`figure_manifest.json` at `bth campaign conclude`),
- **scaffold/validate tool pair** (`postmortem_scaffold` / `postmortem_validate` MCP tools),
- **queryable provenance** (DuckDB warm tier; `campaign_id`, `git_hash`, `output_paths` on every run).

The whole claim-tier layer is, structurally, *"the Campaign gets a sidecar-equivalent, and `conclude` gets a `bth run`-equivalent gate."* Nothing here needs a new datastore.

### 8.1 The enforcement ladder (five levels)

- **L0 — Process/docs.** The rubrics as a skill section; `claim.md` template. Zero code.
- **L1 — Schema + validate.** A `claim.bth.toml` (§7) validated like a sidecar; `bth claim scaffold` / `bth claim validate` (mirror the postmortem pair); new sidecar fields `discriminates` / `isolates`.
- **L2 — Static lint.** `bth lint` rules over the catalog — no execution, pure smell detection. **This is where the EPIC-6 failure becomes a machine check.**
- **L3 — Lifecycle gate.** `bth campaign conclude` runs the Union Gate + CONCLUDE-rubric and refuses a positive verdict that fails (bypass-with-log, escalating to hard-block once trusted).
- **L4 — Provenance graph.** A claim→hypothesis→run→outcome DAG in the catalog; `bth claim trace <campaign>` answers the backward trace as a query.
- **L5 — Adversarial reviewer agent.** `bth campaign review --adversarial` dispatches an agent to verify the eval actually tested the *claim* (metric-mismatch, saturation-labeling, verify-on-raw-data-not-summaries).

### 8.2 Rigor item → mechanism map

| Rigor item (from §3–§4) | Mechanism | Level | bathos primitive reused | Catches EPIC-6? |
|---|---|---|---|---|
| Claim Ledger exists for a campaign with confirmatory runs | require `[claim]` block at `bth campaign create`; else `BYPASSED` | L1 | sidecar-required + `--no-sidecar` log | partial — forces the question |
| **Single-cell-gate smell** | `bth lint`: warn when a run gates on ONE cell of a grid the `claim.regime` ranges over | **L2** | warm-catalog lint | **yes** — the K=4,μ=10-only gate |
| **Disconfirming regime excluded** | `bth lint`: warn when `claim.regime` is not covered by the union of run grids | **L2** | grid fields in `result_schema` | **yes** — μ=5 was pre-registered out |
| Discriminability Map has a zero-power run | `bth claim validate`: flag any run whose predicted outcomes are identical across ≥2 hypotheses | L1/L2 | schema validation | yes — "advantage exists in one corner" ≠ headline |
| Assumption falsified → halt | `conclude` refuses `pass` if any load-bearing assumption marked `falsified` | L3 | outcome-condition eval | **yes** — info-symmetry assumption was false |
| Confound uncontrolled → not `pass` | `conclude` downgrades verdict to `confounded` if any load-bearing confound `uncontrolled` | L3 | conclude lifecycle | **yes** — 6 uncontrolled confounds |
| **Union Gate** (every headline clause → a tested-in-regime run) | `conclude` backward-traces clauses; emits a **claim-coverage report** (like `figure_manifest`); blocks unmapped clauses | **L3 + L4** | `campaign_id` links + DuckDB + conclude emission | **yes** — the union was never established |
| Credit ladder (mechanistic claim needs an ablation) | `claim validate`: if `claim.type=mechanistic`, require ≥1 run tagged `ablation`/`intervention` | L1/L3 | run `tags` | yes — 5 observational passes ≠ mechanism |
| Verify-on-raw-data; eval-tests-the-claim | `bth campaign review --adversarial` | L5 | praxia subagent + MCP | yes — catches the drift narrative |

### 8.3 What stays human judgment (do not over-automate)
Mechanically checkable: *does a `[claim]` block exist; does every clause map to a run; is the disconfirming regime in the grid; is a confound flagged controlled; does a mechanistic claim have an ablation run.* **Not** mechanically decidable — route to a human or the L5 agent, don't fake-enforce: *is this hypothesis genuinely discriminative; is this assumption truly load-bearing; is this control a real potency control or theatre.* The tooling's job is to **mechanize the checkable and prompt the judgment calls**, never to green-light judgment by passing a syntactic check.

## 9. MVP cut + the bathos backlog decomposition

**MVP = update the `/using-bathos` skill (L0) + the two L2 lints that catch the named failure.** Concretely: a "Long-horizon & claim-level rigor" skill section carrying §3–§6 and a `claim.md` template (pure docs, ships today), plus — as the first code — the **single-cell-gate** and **disconfirming-regime-excluded** lint rules, because those two alone would have stopped EPIC-6 five sprints early. Everything heavier is a staged bathos effort.

Proposed backlog chain (spec → brainstorm → dev → impl), ready to log in the bathos repo:

1. **`spec`** — *Claim-tier rigor: schema + lint + union-gate.* Acceptance: `claim.bth.toml` schema (§7), the §8.2 mechanism map as the requirement table, and the §8.3 automate/don't-automate boundary. **Input doc: this file.**
2. **`research`/brainstorm** — *Enforcement ergonomics.* The genuinely-open design calls: bypass-with-log vs hard-block policy and when to escalate; is the Discriminability Map authored by hand or generated from `[outcomes]`+`[[hypotheses]]`; how strict the Union Gate is at MVP; where the claim provenance DAG lives in DuckDB. (A contemplex session fits here.)
3. **`feature`** — *`bth claim scaffold` / `bth claim validate` + `claim.bth.toml` + sidecar `discriminates`/`isolates` fields.* Mirror the existing `postmortem_scaffold`/`postmortem_validate` MCP tools and `validate_sidecar`.
4. **`feature`** — *`bth lint` rules:* single-cell-gate smell; disconfirming-regime-coverage; uncontrolled-confound-at-conclude; zero-power-run. (The L2 layer; ships the MVP-critical two first.)
5. **`feature`** — *`bth campaign conclude` Union Gate + claim-coverage report emission* (L3), bypass-with-log initially.
6. **`feature` (later)** — *claim provenance DAG in the catalog + `bth claim trace`* (L4); *`bth campaign review --adversarial`* (L5).

Sequencing: 1→2 gate the rest; 3+4 are parallel; 4's two MVP lints can land before 3 (they only read existing run metadata). 5 depends on 3. 6 is post-MVP.

## 10. Adjacent rigor concerns to systematize (scope for the bathos research/brainstorm)

The EPIC-6 post-mortem surfaced three rigor concerns that sit *next to* claim-level pre-registration and belong in the same systematization. They are scoped here as open threads for the bathos research → brainstorm → spec pass, **not yet designed** — the point is that they are part of the target, not that this doc resolves them.

**10.1 Baseline & literature-reimplementation fidelity (the equivalence-testing problem).** A comparison is only as trustworthy as its baseline. When the baseline is *our reimplementation of a published method*, "our method beats baseline X" is confounded by "our X ≠ the published X" until proven otherwise. asr's reshuffle (`zeinaty_baseline.py`) is a reimplementation of Zeinaty 2026 that was **never validated against the paper's reported numbers or a reference implementation** — so the headline comparison rests on an unverified baseline (this is confound C7 in the worked-example doc). The needed primitive: a **reference-parity gate** — reproduce the paper's reported result on the paper's own setting (within tolerance), or cross-check against the authors' code, *before* the reimplementation is admitted as a baseline. This is the same discipline `/jax-port` applies to numerical ports (graded parity tests, jaxtyping contracts), but for *methods* rather than code. **Open scoping question for the brainstorm:** does this live in bathos (a `[baseline] reference_parity` block + a gate), in `/jax-port`, or in a dedicated equivalence-testing tool/skill repo? It is connected enough to experiment tracking that bathos is a defensible home.

**10.2 What makes a baseline admissible.** Beyond parity: the *strongest-reasonable-version* principle (do not beat a weakened baseline — cf. asr's M=500 vs the paper's M=1000), parameter fidelity, and **matched information** (the C1 lesson — the method and the baseline must have symmetric access to the ground-truth signal, or the comparison measures *information*, not *method*). A baseline-admissibility checklist belongs in the PLAN-gate (§4).

**10.3 Signal discrimination via minimal, structured, scaled perturbations & probes.** Ablations (§5 Stage 3) answer "is component X necessary?" as a binary. The finer instrument is a **dose-response design**: scan one factor on a graded scale (the divergence μ-sweep is the archetype; coupling strength K, restart count, baseline strength are others) and read the *shape* of the response, not a single contrast. **Structured probes** — minimal planted perturbations with a known expected signature — turn "does the pipeline see the signal?" into a calibrated measurement (the BATHOS synthetic-invariant rule, generalized from binary pass/fail to graded sensitivity). This is the constructive complement to the Discriminability Map: the Map says *which runs separate hypotheses*; perturbation/probe design says *how to build runs whose response curve is itself diagnostic*. **Scope for the brainstorm:** a small vocabulary of probe types (scaled-divergence, planted-mode, null-injection, information-ablation) each with an expected signature.

Together with §3–§9, these define the systematization target: **bathos as the layer that enforces not just per-run pre-registration but baseline trustworthiness, claim-level decomposition, and signal-isolation discipline across a long-horizon program.**

## 11. Adoption path (asr-local first, then port)
1. **Now (asr, zero-code):** run §3–§4 as a hand checklist on the EPIC-6 rebuild — a `claim.md` + PLAN-gate before Stage 1 + Union-Gate at the SP-6.7 verdict. Prove it catches the failure in live use.
2. **MVP (bathos):** port §3–§6 into the `/using-bathos` skill + ship the two L2 lints (§9).
3. **Staged (bathos backlog §9.3–§9.6):** schema, validate tools, conclude union-gate, provenance graph, adversarial reviewer.

---

**Worked example & cross-refs:** `decisions/260616_sp67-claim-and-controls-design.md` (the EPIC-6 failure that motivates every rubric item); grounding `research/260616_sp67-design-grounding.md`; current skill `~/.claude/skills/using-bathos/SKILL.md` (Run/Sidecar/Campaign primitives reused above); global rule `~/.claude/rules/BATHOS.md` (the SP-0.6 measurement-verification lesson, of which this is the campaign-level generalization).
