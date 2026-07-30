---
title: Post-mortem obligations, targeted review schema, and a shipped rule-card corpus
description: Unifies post-mortem workflow hooks, a [review] sidecar block for targeted literature/implementation review, and a machine-addressable reference corpus shipped in agent_assets/
status: draft
task_id: 260729_bathos-postmortem-corpora
date: '260729'
backlog_ids: ''
adversarial_review: ''
---
# Post-mortem obligations, targeted review schema, and a shipped rule-card corpus

**Status:** design agreed, nothing implemented. Adversarially reviewed (verdict REVISE); all four
blocking objections resolved — see §8b. Owner decisions taken 260730 are recorded in §10.
**Grounding:** every "currently" claim below was verified against source on 260729 by three
independent read-only passes (two code, one design-doc). Line citations are to
`src/bathos/` at commit `abcfc0a`.

---

## 1. What is actually true today

Four findings frame the whole design.

**Post-mortems are an isolated island.** `postmortem.py` ships a working scaffold/validate/get
triple with real validation (hypothesis_status↔verdict_override consistency, asset-link
containment, SHA mismatch, git-drift — `postmortem.py:117-201`). But *nothing requires one*.
The call sites outside `postmortem.py` are `cli.py` (three user-driven commands),
`repair.py:443-499` (an advisory count that warns before a warm-DB rebuild but does not block
it), `schema.py:239-248`, `compact.py:287-297`, `query.py:283-290`, and `rich_fmt.py:135-144`
(display only). Every field defaults to an inert value — `"unassigned"`,
`"none"`, `""`. No gate reads them. `rg "postmortem" src/bathos/campaigns.py` returns nothing.
It is a filing cabinet nobody is obliged to open.

**Experiment setup is the exact opposite** — it is the most heavily gated surface in the tool.
`prereg.gate_check()` (`prereg.py:392-476`) enforces sidecar presence, validity, hash-drift
(deny in autonomous mode), first-of-kind, and adversarial-check presence. On top sit the F3
submit-gate, F2 conclude-gate, and the Union Gate. The asymmetry is the point: **setup is
gated, learning-from-outcome is not.**

**Literature review exists only at two extremes, with nothing between them.** At the cheap end,
`[reproduction].reproduces_paper` — a bare optional DOI string (`sidecar.py:177`). At the
expensive end, the full literature-parity audit: blind reconstruction ×N, adversarial refutation
×M, cap-lattice grading over five dimensions. The only thing that *requires* either is one
Tier-1 lint (`check_novel_or_reproduces_declared`, `linter.py:153-217`) demanding
validation/production experiments declare `[reproduction]` **or** `novel=true` — which the
one-line DOI satisfies. There is no middle rung, which is why ordinary work uses neither.

And **implementation review has no representation at all.** There is no schema anywhere for
"I read the reference implementation at commit X and checked property Y." `parity.bth.toml`
takes `impl_paths`, but only as input to the full five-phase audit.

**The shipping mechanism is already proven.** `pyproject.toml:92-93` force-includes the entire
`agent_assets/` tree into the wheel at `bathos/agent_assets`; `export.py:58` reads it back via
`importlib.resources.files("bathos")`. The precedent is `agent_assets/skills/using-bathos/
literature-parity/` — five phase docs, a template, and a reference implementation, shipped
together. What does *not* exist is any statistics, experimental-design, or pre-registration
reference content: `rg "corpus|corpora|checklist|primer|handbook|glossary"` finds only the
parity docs and `snippets/rules.md`.

---

## 2. The unifying observation

Bathos already runs on **citable identifiers** — hypothesis IDs, confound IDs, `clause_labels`,
run UUIDs, content-addressed sidecar SHAs. Gates work because declarations bind to IDs that
something else can check.

Knowledge is the one thing with no IDs. Methodological rules, prior work, and reasons are all
prose. Nothing can cite them, so no gate can check a citation, so none is ever required.

The design is therefore one move applied three times:

> **Make knowledge addressable. Make the schemas cite it. Make post-mortems the channel that
> fires when a citation turns out to have been wrong.**

That single move answers all four asks, and — importantly — it lets each piece ship
independently.

---

## 3. Piece 1 — the corpus as rule cards, not prose

Ship `agent_assets/corpus/`, one card per file, markdown body with TOML frontmatter:

```
agent_assets/corpus/
  stat/STAT-012_multiple-comparisons.md
  design/DSGN-004_positive-control-required.md
  prereg/PREREG-007_outcome-condition-falsifiability.md
```

```toml
id = "STAT-012"
title = "Multiple comparisons inflate the false-positive rate"
applies_when = "n_outcome_branches > 2 OR campaign_run_count > 10"   # DuckDB SQL fragment
severity = "warning"
see_also = ["STAT-014", "DSGN-004"]
```

**Why cards and not a prose handbook.** A handbook is documentation nobody reads at the moment
it matters. A card has an ID, so:

- `linter.py` advisories can cite the card that justifies them, turning every warning into a
  pointer at its own rationale — and retiring the standing complaint that thresholds are
  unjustified (cf. `.praxia/docs/decisions/260601_sprint-audit-threshold-rationale.md`).
- A sidecar can cite a card as the *basis* for a threshold, which is exactly what the existing
  Tier-2 `check_threshold_basis` (`linter.py:616-686`) already asks for and currently accepts
  as freeform text.
- A post-mortem can record which card was violated (§5), giving the corpus a usage signal.

**What is genuinely reused, and what is not.** `severity` reuses `IssueSeverity`
(`linter.py:12-14`). Packaging reuses the `agent_assets/` force-include. Those are real.

`applies_when` is **not** a reuse of the `[outcomes].condition` evaluation path, and an earlier
draft of this document wrongly claimed it was. `evaluate_outcome()` (`sidecar.py:321-354`)
builds a *single literal row* out of the script's own `result` dict — `SELECT ({condition}) FROM
(SELECT {cols})` — with no catalog access and no access to sidecar fields. The worked example
above references `n_outcome_branches` (a property of the sidecar's `outcomes` table, not a
`result` field) and `campaign_run_count` (an aggregate over `runs`, which is not a column on
`Run` at all — `schema.py:27-67`). Only the **SQL dialect** is shared.

So the evaluation context for `applies_when` is **new machinery that this design must specify**,
not something inherited. It needs a defined row shape joining sidecar-derived fields with
campaign-level aggregates, and that shape is a prerequisite for Piece 1 rather than a detail.
`bth ref applicable` is specified below as evaluating "against the sidecar + catalog history",
which is a categorically different input than `evaluate_outcome` receives — designing that
context is the first real task in step 1.

**Surface:**

| Command | Behavior |
| --- | --- |
| `bth ref show STAT-012` | Render one card |
| `bth ref search "multiple comparisons"` | Full-text over card bodies |
| `bth ref applicable <script>` | Evaluate every `applies_when` against that script's sidecar + catalog history; list the cards that fire |

Plus MCP mirrors (`reference_get`, `reference_applicable`), per the "cli.py and mcp.py both thin
layers over the same core" rule in CLAUDE.md.

`bth ref applicable` is the one that earns the corpus its keep — it is the difference between
shipping guidance and *delivering* it at the moment a specific experiment is being designed.

---

## 4. Piece 2 — the `[review]` sidecar block

Fills the empty middle rung, and introduces implementation review for the first time.

```toml
[[review.literature]]
ref = "10.1038/s41586-021-03819-2"    # DOI, arXiv id, or a bth run UUID
claim = "reports 87% GDT-TS on CASP14 free-modeling targets"
bears_on = "H1"                        # hypothesis or confound id from the claim file
disposition = "supports"               # supports | contradicts | scope-differs
checked = "2026-07-29"

[[review.implementation]]
source = "https://github.com/org/repo"
commit = "a1b2c3d"                     # pinned, therefore drift-checkable
what_was_checked = "loss reduction over the batch dim, model.py:214"
bears_on = "C2"
disposition = "diverges"               # matches | diverges | not-applicable
```

**`bears_on` is what makes this enforceable rather than aspirational.** It reuses precisely the
mechanism `claim_discriminates` / `claim_isolates` already use: a run-level declaration binding
to a claim-level ID, validated against the real hypothesis/confound slate at
`bth claim validate` (`claim.py:204-228`). An entry that names a nonexistent ID fails validation
the same way a bad `claim_discriminates` does today.

That in turn yields a **Review Coverage Gate** structurally identical to the existing Union Gate
(`claim.py:600-652`): walk the claim's hypotheses and confounds, and require each to be covered
by ≥1 review entry before the campaign may conclude positively. Uncovered ⇒ downgrade, exactly
as the parity confound check already downgrades.

Two specifics the gate must pin down, both surfaced by adversarial review:

- **"Confirmatory" is not a real concept in the code.** `rg "confirmatory|campaign_type"` over
  `campaigns.py` returns nothing. The actual field is campaign `mode`, and the value is
  `"confirmation"` — and `conclude_campaign`'s own downgrade logic groups
  `confirmation`/`sequential` together (`campaigns.py:221`). Read every "confirmatory" in this
  document as **`mode in ("confirmation", "sequential")`**, and say so in the implementation.
- **Empty slates must not pass vacuously.** A campaign in a gated mode whose claim declares zero
  hypotheses and zero confounds satisfies "each is covered" trivially. The gate must treat an
  empty required-set as `uncovered`/error, not `covered`.
- **`bears_on` is required only here** (D1/D2, §10). A `[review]` block is legal on any run;
  `bears_on` becomes mandatory only when a claim is registered *and* the mode is gated. Without
  a claim the coverage gate does not run at all, matching how `validate_sidecar` already skips
  claim-dependent checks (`validate.py:232-243`) rather than inventing a second rule.

**Three declared tiers, mechanically graded** — the same cap-lattice logic parity already uses:

| Tier | Means | Required at |
| --- | --- | --- |
| C0 `cited` | DOI + claim, unverified | exploration (advisory) |
| C1 `reviewed` | `bears_on` + disposition, or an implementation read at a pinned commit | validation / production |
| C2 `parity` | the existing five-phase audit | wherever a confound is `reference_parity` |

**Deliberately named C-not-R.** An earlier draft called these R0/R1/R2, which collides badly
with the existing `reproduction_rung` scale (`parity.py:31,38,103-109`) — that scale runs R0–R4
and is **inverted relative to this one: its R0 is the _best_ rung** ("R0/R1 → ceiling PARITY"),
whereas the draft's R0 was the weakest tier. Since this section explicitly borrows parity's
rung-ladder and cap-lattice idioms, reusing its token with the opposite severity direction was
a live misreading hazard for anyone moving between the two schemas. Renamed to C0/C1/C2.

This reframes the existing parity subsystem as the **top rung of a ladder** rather than the only
option — which is the most likely reason it sees little use for ordinary work.

The pinned `commit` makes the attestation *falsifiable in principle* — but an earlier draft
wrongly claimed it is "checkable by the existing `check_output_sha_drift()` machinery." It is
not. `check_output_sha_drift()` (`checker.py:121-159`) compares a run's **own** recorded
`output_metadata` — files that run wrote, hashed at run time — against on-disk state. It has no
concept of an external repository, and nothing in the codebase clones or fetches a remote ref.

Checking an external commit pin therefore requires new network- and VCS-aware code that this
design does not currently specify. Until that exists the `commit` field is a **recorded
attestation, not a verified one**, and should be described that way. A cheap first version that
avoids the network entirely: require the reference to be vendored or already present locally,
and check the pin against the local clone.

---

## 5. Piece 3 — the post-mortem obligation ledger

Post-mortems are an island because nothing ever *creates an obligation* to write one. So derive
obligations from events bathos already computes.

**An obligation opens automatically when:**

1. a run's computed `outcome` label indicates failure. **Not** a literal match on `"fail"`:
   unlike `stage_name`, which is constrained to `CANONICAL_STAGES` (`sidecar.py:104-110`),
   outcome labels have **no canonical set** — the author names each `[outcomes.<label>]` branch
   freely, so `unstable` / `rejected` / `no-go` are all legal and a string match would silently
   miss them. Define the trigger against the `pass_labels` set that `compute_evalue`
   (`sidecar.py:293-299`) already derives, i.e. non-pass and non-residual;
2. a campaign concludes `confounded`, or is downgraded by the Union Gate or the parity gate;
3. an `adversarial_check` fires;
4. **a `[review]` entry with `disposition = "supports"` is contradicted by the run's outcome.**

(4) is the highest-value trigger and is *only detectable because Piece 2 made the citation
machine-readable*. It catches the specific failure where the literature was read, believed, and
wrong — which is exactly the case worth a written retrospective and the case least likely to
get one voluntarily.

**Store:** `.bth/obligations/<run_or_campaign_id>.json`, mirroring the existing `.bth/claims/`
layout. **Discharge:** a valid `*.bth.postmortem.toml` referencing the obligation id.

**Gates — DECIDED (D1, 260730): downgrade at conclude, warn at submit. An obligation never
blocks.**

- `bth campaign conclude` — **the only binding site.** Open obligations on member runs downgrade
  the campaign verdict, exactly as the parity confound check and the Union Gate already do. It
  produces a queryable record ("this claim was made with unexplained failures") rather than an
  error someone re-runs past.
- `bth submit` — **warns only, never blocks**, at every stage.
- **Signal 11 `open_obligation_age`** in sprint-audit. (The numbered `Signal N` markers in
  `sprint_audit.py` run 1, 2, 4–10, 12, 13 — **11 is absent entirely**, so this fills the gap
  rather than extending to 14. Signal 3 exists as `bypass_in_agent_mode` but carries no marker
  comment; add it while you are there.)

*Why not block.* Block-at-conclude and downgrade-at-conclude converge in effect — both stop an
unsupported positive verdict — so the real question was whether enforcement leaves a record or
an error, and this codebase has answered it twice already. Blocking `bth submit` was rejected
for a second, independent reason: it would put the fragile script-stem key behind a hard
decision (see the retired objection 3 in §8b), and it would manufacture bypass pressure against
metrics bathos itself relies on (Signals 2, 3, 10). Conclude keys on campaign membership — a
real foreign key — so nothing binding depends on stem matching.

*The accepted cost.* A campaign that is never concluded never pays. Exploratory campaigns often
are never concluded, so obligations arising from them will age in the ledger unaddressed. Signal
11 is what makes that visible rather than silent; it is a reporting answer, not an enforcement
one, and that is deliberate.

**Schema addition closing the loop to Piece 1:** the post-mortem gains
`violated_cards = ["STAT-012"]`. The cards most frequently cited in post-mortems are, by
construction, the ones worth promoting from advisory prose into a hard lint. The corpus acquires
an empirical maintenance signal instead of accreting forever.

---

## 6. Piece 4 — corpus-aware experiment setup

`new_experiment.py` currently emits a static skeleton. Make it evaluate `applies_when` across
the corpus for the declared stage and kind, then emit the firing cards as commented guidance
*inside the scaffolded sidecar*, alongside pre-filled `[review]` stubs for each hypothesis in
the campaign's claim slate.

This is the point where the corpus changes behavior — at authoring time, in the file the
researcher is already editing, rather than in documentation they would have to go find.

---

## 7. Build order

Ordered by real dependency, not ambition:

1. **Corpus cards + `bth ref`** — standalone, no schema change, no gate touched. Immediately
   useful and independently shippable. Seeded per **D4**: convert `linter.py`'s twelve Tier-2
   checks into cards *first*, as v1. Every one is a rule the codebase already enforces, so the
   batch carries no methodological content to argue about and v1 becomes a clean test of the
   mechanism. The authored statistics / experimental-design cards land as a **separate, second
   batch, reviewed as content on its merits** — deliberately not bundled, so "does the mechanism
   work" and "is this claim about statistics correct" are never debated in the same change.
2. **`[review]` block: parse + validate only, advisory.** No gate. Establishes the schema and
   lets real entries accumulate before anything depends on them.
3. **Review Coverage Gate** at conclude, for `mode in ("confirmation","sequential")` only.
4. **Obligation ledger + conclude-time downgrade + submit warning** (Signal 11), plus the
   `Postmortem` `campaign_id` / `discharges` schema change from §8b objection 4.
5. **`new-experiment` corpus integration.**

Steps 1 and 2 touch no existing gate and can land in any order. Nothing after step 2 should
start until step 2 has produced real `[review]` entries on real experiments — the coverage gate
should be calibrated against observed data, not guessed thresholds.

**Two structural requirements this design initially omitted**, both surfaced by plan audit and
both required by conventions this repo already enforces everywhere else:

- **A core `corpus.py` module is part of step 1, not an implementation detail.** Every other
  subsystem here keeps `cli.py` thin over a dedicated core module — `sidecar.py`, `claim.py`,
  `postmortem.py`, `campaigns.py` all follow it, and CLAUDE.md states it as a rule. Card
  parsing, search, and `applies_when` evaluation belong in `corpus.py`; `bth ref` is a wrapper.
  Without naming the module explicitly, the obvious implementation puts business logic in
  `cli.py` and breaks the convention.
- **Every new CLI surface needs its `mcp.py` mirror in the same step.** "FastMCP: Mirror CLI
  tool-for-tool" is a locked decision in CLAUDE.md, and `claim`/`attestation`/`query`/`anchor`
  all honour it. That means `bth ref show|search|applicable` ships with `reference_get` /
  `reference_search` / `reference_applicable`, and the obligation and coverage gates surface
  through MCP too — or the omission is scoped out explicitly with a stated reason.

Each step should also name the `tests/test_*.py` file it extends. The repo's dominant
verification idiom is a test file, not a CLI invocation; a plan derived from this document that
verifies only by running commands is under-specifying.

---

## 8. Risks, stated plainly

**Corpus staleness.** A shipped corpus is a maintenance liability. Mitigation: cards are short
and rule-shaped, not literature reviews; and `violated_cards` gives a usage signal for pruning
dead ones. This does not eliminate the liability, it makes it measurable.

**Review theater.** `[review]` entries can be fabricated wholesale. `bears_on` binding and
`commit` pinning raise the cost and make some fabrications mechanically detectable, but neither
proves anyone read anything. This is the same limitation the entire pre-registration system
already has, and it should be documented as such rather than papered over — a gate that is
described as proof will be trusted as proof.

**Obligation fatigue.** If every `fail` opens a blocking obligation, researchers will route
around it (and `bth` already tracks bypass rates in Signals 2/3/10, so the routing-around would
be visible). Mitigation: obligations only *gate* at validation/production, matching the existing
stage tiering; exploration failures open an advisory obligation that ages out.

**Threshold discipline.** Every numeric in this document — the R0/R1/R2 stage assignments, any
future `open_obligation_age` cutoff — is currently arbitrary. Per project convention these must
either be derived from observed data after step 2, or shipped with an explicit
acknowledgement that they are conventional. They should not be presented as calibrated.

---

## 8b. Adversarial review — objections and their resolutions

This spec was put through an adversarial review (a `spec_adversarial` rig-run flow plus an
independent Claude `spec-challenger`, run in parallel as a control). Verdict: **REVISE.** Two
false reuse-claims were corrected inline above (§3 `applies_when`, §4 `commit` pinning), as was
the R-rung collision (§4) and the outcome-label and "confirmatory" vocabulary (§4, §5).

**All four blocking objections are now resolved** — 1 and 2 by owner decisions taken 260730, 3
retired as a consequence of decision D1, and 4 specified below. Recorded here rather than
deleted, because the reasoning is the load-bearing part:

1. **`bears_on` on claimless runs — the analogy in §4 is backwards.** §4 argues a bad `bears_on`
   "fails the same way a bad `claim_discriminates` does today." It does not.
   `validate_sidecar` documents that omitting `claim` *skips the check entirely*
   (`validate.py:232-243`), and `load_registered_claim` returns `None` whenever
   `campaigns.claim_path IS NULL` (`claim.py:186-187`). So a bad `claim_discriminates` on a
   claimless run **passes silently today** — meaning `bears_on` would too.

   **RESOLVED (D2, 260730) — optional until confirmatory.** A `[review]` block is legal on any
   run. `bears_on` is required only when a claim is registered *and* the campaign mode is
   `confirmation` or `sequential`; the Review Coverage Gate simply does not run without a claim.
   This matches the precedent `validate_sidecar` already sets rather than inventing a second
   rule. Rejected alternatives, and why: binding `bears_on` to an outcome label when no claim
   exists *looks* stronger but checks a free-text string against another free-text string the
   same author wrote in the same file — a consistency check, not grounding — at the cost of a
   second ID namespace; requiring a claim for any `[review]` entry pushes claim-tier ceremony
   onto exploration, which is the friction most likely to make the tier get skipped (the
   five-phase parity audit is the cautionary precedent). **Accepted cost:** exploration-tier
   `[review]` is unenforced prose. Its value is that the author wrote it down. Review is
   note-taking at exploration and a gate at confirmatory — if that split is wrong, this design
   is wrong, not its default.

2. **Obligation trigger (4) "contradicted" is not computable as written.** Nothing in bathos
   maps an outcome label to "contradicts hypothesis H". The only stored expectation is
   `claim.discriminability`, keyed by `(hypothesis_a, hypothesis_b, planned_run_label)`
   (`claim.py:96`, validated `claim.py:264-274`) — a different shape than `bears_on` +
   `disposition`. Must specify which stored value is compared, an explicit truth table, and at
   which point it evaluates (run-end has only the `result` dict; conclude has the catalog).

   **RESOLVED — specified below, now that D1 and D2 fix its two free variables.** D1 puts the
   only binding evaluation at conclude, where the full catalog is available; D2 guarantees that
   wherever `bears_on` is *required*, a registered claim exists. Both preconditions the trigger
   needed are therefore met, and the rule becomes:

   > **Evaluated at `bth campaign conclude`, never at run-end.** For each `[review]` entry on a
   > member run where `disposition = "supports"` and `bears_on = H`, look up the
   > `claim.discriminability` row whose `planned_run_label` matches that run's recorded outcome
   > label. If that row predicts `H` is *disfavoured* by the observed label, the citation is
   > **contradicted** and an obligation opens against the run.
   >
   > If no discriminability row covers the observed label, the result is **`indeterminate`, not
   > contradicted** — it opens no obligation and is reported in the conclude summary. Silence
   > must not read as confirmation, and it must not read as refutation either.

   This deliberately reuses `discriminability` as the single place a hypothesis-to-outcome
   expectation is stored, rather than adding a second one.

   **The cost, stated precisely because it is easy to overstate the coverage here.**
   `discriminability` is *optional*: `claim.py:134` reads
   `claim_section.get("discriminability", [])`, and AC-04's zero-power lint only fires once
   there are ≥2 entries (`claim.py:408`). So a confirmatory claim with an empty discriminability
   map is valid today, and for such a claim trigger (4) can never fire. That is a silent
   coverage hole, so the conclude summary must report **how many `supports` citations were
   evaluable** alongside how many were contradicted — a trigger that cannot fire must not look
   like a trigger that found nothing. Making `discriminability` mandatory for confirmatory
   claims would close the hole, but that is a claim-tier change with its own blast radius and is
   deliberately out of scope here.

3. **Script stem is a fragile gating key, and §5 inherits the fragility silently.** The
   reproduction gate §5 claims to mirror matches on a substring of the whole recorded command
   line, on **both** its paths (`prereg.py:296-349`): the warm path runs
   `SELECT 1 FROM runs WHERE command LIKE ? AND outcome = 'pass'` bound to `%<stem>%`, and the
   cool-tier Parquet fallback does a plain Python `requires_pass_stem in cmd`. That
   yields false positives on common stems, orphans history on rename, and cannot distinguish
   `scripts/experiments/foo.py` from `scripts/debug/foo.py`. Acceptable for an advisory check;
   this design would put it behind a **blocking** validation/production gate.

   **RETIRED (consequence of D1, 260730).** Nothing binding depends on the stem key any more.
   `bth submit` only warns, and the sole binding site — `bth campaign conclude` — resolves member
   runs through campaign membership, a real foreign key, never through a stem substring. The
   fragility still exists in the *existing* reproduction gate and is still worth fixing there,
   but it is no longer this design's problem and no longer blocks it. Worth filing separately
   against `prereg.py:296-349` rather than carrying here.

4. **The postmortem schema cannot discharge a campaign-scoped obligation.** `Postmortem` is
   mandatorily `run_id`-keyed — `parse_postmortem` raises on a missing `run_id`
   (`postmortem.py:51-52`) — and has **no `obligation_id` field at all**. But trigger (2) opens
   a *campaign*-scoped obligation. Must specify: a new campaign-scoped postmortem kind, or
   whether one member run's postmortem discharges it, or N-of-M.

   **RESOLVED — a campaign-scoped postmortem kind, which D1 makes the coherent choice.** Since
   conclude is now the only binding site, the discharge has to be addressable at campaign scope;
   "one member run's postmortem discharges the campaign obligation" would let an unrelated
   run's write-up clear a campaign-level confound. Concretely:

   - `Postmortem` gains an optional `campaign_id`, and **exactly one of `run_id` or
     `campaign_id` must be present** — replacing the current unconditional raise on a missing
     `run_id` (`postmortem.py:51-52`) with an exclusive-or check. This is the one genuinely
     breaking change in the design; every existing postmortem stays valid because `run_id` is
     still accepted, and the parse error message changes rather than the accepted input.
   - `Postmortem` gains `discharges = ["<obligation_id>", ...]`. An obligation is discharged
     when a *valid* postmortem naming it exists — validity meaning it already passes the
     `hypothesis_status`↔`verdict_override` consistency rules at `postmortem.py:117-201`, so no
     new validation tier is introduced.
   - Run-scoped obligations are discharged by run-scoped postmortems; campaign-scoped ones by
     campaign-scoped postmortems. No N-of-M rule, because there is no case where a fraction of
     an explanation is the right bar.

Non-blocking but tracked: the obligation path `.bth/obligations/<run_or_campaign_id>.json`
conflates two ID namespaces with no discriminator and does not say whether multiple concurrent
triggers on one entity produce one file or clobber each other; `bth ref applicable` has no
stated behavior for zero cards firing, no ordering guarantee, and no error contract for a
malformed card (note `evaluate_outcome` raises `SidecarError` on a bad condition —
`sidecar.py:352-353` — so one bad card must not abort the whole corpus); and §6's "commented
guidance" is prose with no testable acceptance criterion.

## 9. Relationship to prior design work

This does not re-propose anything already decided. Checked against
`260616_bathos-long-horizon-rigor.md`, `260616_bathos-claim-tier-rigor-open-design-call.md`,
`260618_abstraction-boundary-open-design-questio.md`, `260618_literature-parity-v1-design.md`,
`260617_literature-parity-workflow-seed.md`, and the three `agentic-science` docs:

- **Post-mortems:** none of the prior docs propose post-mortem gating at all. `260616 §8.1`
  cites `postmortem.py` only as a reusable *scaffold/validate pattern*. §5 here is new ground.
- **Literature-parity:** its architecture is LOCKED (A2 thin-config, D2 unified evidence
  channels, D3 manifest-declared, E1 rung ladder, X1 cap-lattice) and shipped with F2/F3/F4
  enforcement. §4 here deliberately sits *below* it and feeds it, adopting its rung-ladder and
  cap-lattice idioms rather than competing.
- **Shipped content:** prior work ships skill sections and rubrics. A machine-addressable card
  corpus with `applies_when` evaluation is new.

Two prior open questions this design touches: "when does sidecar enforcement become a hard
block" (`260520` Open Q5, unresolved) is answered here for the review-coverage case only, by
stage tiering. The `parent_run_id` / lineage proposal (`260520 §4 P1`, still deferred) is
adjacent but out of scope.

---

## 10. Decisions taken (260730)

All three questions this section originally posed are answered; recorded with their reasoning so
a later reader can tell a decision from a default.

| | Decision | Why |
| --- | --- | --- |
| **D1** Obligation posture | Downgrade at conclude, warn at submit. Never blocks. | Block-at-conclude and downgrade converge in effect, so the real choice was record vs. error — and the Union Gate and parity check both already downgrade. Also keeps the fragile stem key out of anything binding (§8b obj. 3). |
| **D2** `bears_on` scope | Optional until confirmatory. | Matches `validate_sidecar`'s existing claim-skip precedent. Binding to outcome labels is grounding-shaped but not grounding; requiring a claim adds ceremony to the tier least able to absorb it. |
| **D4** Corpus v1 seed | Convert `linter.py`'s twelve checks first; authored statistics cards as a reviewed second batch. | The converted set contains no invented methodology, so v1 tests the mechanism and nothing else. Sequencing is the decision; both batches ship. |

**What remains genuinely open** — smaller, and none of it blocks implementation:

1. **`discriminability` is optional, so obligation trigger (4) has a silent coverage hole** for
   confirmatory claims that leave the map empty (§8b obj. 2). Mitigated by reporting evaluable
   count at conclude; closing it properly means making the map mandatory for confirmatory
   claims, which is a claim-tier change with its own blast radius.
2. **Card-authoring standard for the second batch.** Any card asserting a numeric threshold needs
   a citable basis or an explicit "this is conventional" marker — the project's standing rule
   about unjustified cutoffs applies to the corpus itself, and the corpus is where such
   assertions would otherwise accumulate unexamined.
3. **Obligation ledger file layout** — `.bth/obligations/<id>.json` still conflates run and
   campaign ID namespaces with no discriminator, and does not say whether concurrent triggers on
   one entity produce one file or clobber. Now partly forced by objection 4's resolution
   (obligations are addressable at both scopes), so worth settling at implementation time.
