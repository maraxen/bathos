---
name: bathos-literature-parity
description: Validating a reimplemented baseline against a published method — or against a vendored reference implementation — before trusting it as a claim-tier confound control. Covers the Mode A differential counterfactual, control design for liveness/knob checks, existence-vs-attribution, and verifying the parity instrument itself executes.
triggers: [literature parity, reimplemented baseline, parity.bth.toml, reference_parity, blind reconstruction, adversarial refutation, differential counterfactual, ported implementation diverges, knob control, liveness control, manipulation check, cross-implementation parity, reference implementation on disk, why does my port disagree]
---

# bathos-literature-parity

When you reimplement a method from a published paper — especially one that publishes no reference code — the reimplementation can silently diverge from the described method, confounding any downstream comparison (`[confounds.reference_parity]` in claim-tier language, see **bathos-campaigns**). A unit test cannot catch this: the reimplemented method runs, passes internal checks, and produces plausible numbers. This skill documents bathos's structured validation protocol.

## When to use literature-parity validation

**Use this workflow when:**
- Your project reimplements a method from a peer-reviewed publication
- The original paper publishes no reference code, or the code diverges significantly from the paper text — **Mode B**
- Your project **ports** an implementation that IS available (vendored clone, published repo, pinned checkpoint) — **Mode A**, and the protocol below changes shape; see the next section
- The reimplementation will be compared against published results or other baselines
- You need to flag the `[confounds.reference_parity]` confound as *controlled* for downstream claim-tier gates

**Also use it when a port is already suspected of diverging** — an anomaly in your own output (excess run-to-run variance, implausible recovery, a distribution flatter than the reference's) is a parity question even when no claim is pending. Reaching for this skill only at claim time is how a divergence survives for months.

**Outcome:** A graded parity run with verdict PARITY (faithfully reimplemented), PARTIAL (controlled deviations documented), or FAIL (significant discrepancies). The verdict controls whether the F2 conclude-gate, F3 submit-gate, and F4 binding-gate allow downstream campaigns to proceed.

## Mode A vs Mode B — pick before you start, because they need different protocols

`parity.bth.toml` already declares this (D3), but the 5-phase protocol below is **Mode B shaped**: blind reconstruction exists because there is no code to diff against. When `reference_code` is set, most of Phases 1–2 is wasted effort and a stronger instrument is available.

| | **Mode B** — text only | **Mode A** — reference code on disk |
|---|---|---|
| core instrument | blind reconstruction (N agents) → clause mapping | **differential counterfactual** against the running reference |
| what it establishes | your code matches the *described* method | your code matches the *executed* method, numerically |
| Phases 1–2 | load-bearing | largely skippable; keep only the ambiguity ledger |
| Phases 3–5 | as written | as written, with the counterfactual feeding Phase 4 |

### The differential counterfactual (Mode A's core move)

**Do not reason about whether a suspected defect matters. Introduce it into the reference and measure whether the reference then reproduces you.**

```
arm A: reference, unmodified           → must reproduce the observed disagreement
arm B: reference, crippled exactly as your port is crippled
```

If the suspected defect is the cause, arm B agrees with your port. This cannot be satisfied by a plausible-looking story, which is exactly what source-reading produces.

**Run arm A rather than quoting the published number.** Reproducing it from a fresh run is what proves your harness measures the same thing; quoting it lets a harness change masquerade as an effect of the intervention.

**Escalate in this order — the cheap layers are also the most decisive:**

1. **Constants on the loaded model vs the artifact's own declared values.** A model that derives a hyperparameter correctly and then fails to deliver it to a constructor is invisible to every shape check, because such values often size a *sequence* axis rather than a feature axis. If the library has a second code path that passes the same value correctly, that path is a ready-made discriminating control.
2. **Full parameter-inventory diff, in both directions.** Which reference tensors have no home in your skeleton? Which of yours have no source? A *missing trained* tensor and a *phantom untrained* one are different bugs with different fixes, and positional deserialisation hides both.
   - To show a suspect tensor is untrained init rather than a misassigned real tensor, compare it against the framework's init bound **and** against genuinely trained tensors of the same shape. *"0 of 83 trained tensors fall inside this bound, and this one does"* is evidence; "it looks small" is not.
3. **Input equality** — same items, same order, same encoding, asserted not assumed.
4. **Only then, activations.** The most expensive instrument, reached for last. For JAX ports see **`xtrax-activation-parity`**, which uses a boundary-op capture rather than a modified forward pass, so the computation under observation is not the one you edited.

   ⚠️ Two traps that cost a rewrite of this very paragraph, both verified in xtrax source — **check the equivalents in whatever framework you use**:
   - **A capture hook whose return value is spliced back into the graph is not a passive observer.** xtrax's `Tap` is declared `T -> T` but `executor.py` does `y = boundary.tap(y)`, so a tap that accidentally returns its callback's result silently perturbs everything downstream — the exact failure the approach exists to avoid. Its `Sink` discards the return structurally, and is therefore the safe capture point. Prefer the hook whose return is *structurally* ignored over the one that is *contractually* identity.
   - **A whole-store content digest is a self-verification primitive, not a cross-side equality oracle.** xtrax's `zarr_content_digest` folds each node's **path and attrs** into the hash, and the sink stamps a wall-clock `created_at`, so two stores holding bit-identical tensors produce different digests. Compare **arrays** (dtype, shape, canonicalized bytes) across sides; use the store digest only to prove a store has not changed under itself.

### Existence and attribution are different claims — gate them separately

A confirmed, indefensible defect can contribute **~0** to the failure you found it while chasing. Measured: a port discarding ~36% of its ligand context at every residue — internally self-contradictory and correctly filed — accounted for **0.19%** of the parity gap once the same handicap was applied to the reference.

- Never let a localisation stand in for a measurement, and do not write an issue whose framing implies causation before the attribution experiment has run.
- Write the negative result back to the issue if attribution fails. A real defect still needs fixing, but it must stop being cited as the explanation.
- Scope a negative attribution to the context measured. A defect a given input cannot *express* reads as harmless there and may be severe elsewhere.

### Within-implementation checks do not test correctness

All of these pass against a thoroughly wrong implementation, and a project can accumulate every one and still be broken:

- the conditioning reached the model (verified from the output's own recorded mask)
- the path is live (outputs move; the shift is spatially coherent)
- the knob is live (changing a seed or flag changes the output)
- the arms are mutually distinct
- the frame/projection is *identified* rather than merely consistent

Each establishes the implementation does **something**, consistently, in the right **place**. None asks whether it does the **same** thing as the reference. Only an external referent tests that — reference implementation, analytic solution, or synthetic ground truth with a known answer.

⚠️ **A correctness defect can INFLATE a distinctness statistic.** If an arm earns budget by differing from its siblings, a broken arm differs more. Before spending on a statistic, ask whether a defect would raise it.

## Designing the control — the failure mode that is invisible in the result

A liveness / knob / manipulation-check control answers *"did my intervention do anything at all?"*. Mis-specified, it **cannot fire**: the run grades FAIL and nothing looks wrong, because it reports a clean number and a clean verdict. That is strictly worse than a mis-gated effect, which at least looks suspicious.

1. **Match the control to its ceiling in BOTH pairing and n.** If the control's two arms share a seed (so a nuisance cancels by construction), the ceiling must also share a seed. If the control is computed at n, the ceiling must be at n. A mismatch in *either* biases it toward "dead".
2. **Run the structural tell before believing any control:** does the *"these differ"* statistic outscore the *"these are identical"* reference? A same-implementation/**different**-hyperparameter correlation cannot systematically beat a same-implementation/**same**-hyperparameter one. If it does, the two are not measured against the same noise — regardless of how the numbers look.
3. **The tell identifies the defect, not its mechanism. Decompose before asserting a cause.** Measured: a control carried both a pairing and an n mismatch; "the pairing" was asserted as the mechanism and written into an immutable pre-registration. Decomposed afterwards — **n/construction 98.9%, pairing 1.1%** — and the same-seed statistic judged against a correctly-built ceiling sat *below* it. Both defects were real; the stated mechanism was wrong in the dominant term.
4. **Never pin a gate as "well below" / "clearly exceeds".** An un-numbered comparison is a judgement call wearing a threshold's clothing, and lets the gate be adjudicated after the number exists — the same defect as a post-hoc outcome criterion.
5. **Anchor a materiality threshold to the quantity it must explain.** *"The gap is 0.68, so a knob moving less than 0.05 cannot be the driver"* is defensible; "0.05 seems small" is not.
6. **Give the control conjuncts and read them separately.** Statistical resolvability (does it move beyond the ceiling's own sampling uncertainty — a Fisher bound, not a guess) and material resolvability (is the move large enough to matter) answer different questions. A run where the statistical conjunct **passes** and only materiality fails is the finding *"the defect is real but not the driver"* — not a broken instrument. Write the fail-branch decision text so it cannot mis-route that case.
7. **When a control fails, withhold the statistics that depend on it, and say why.** A vacuous experiment's unflattering number is exactly as uninformative as its flattering one; reporting only the unflattering one is post-hoc selection running the other way.
8. **Fixing a mis-specified control is a NEW experiment needing its own pre-registration** — never a "plumbing fix" folded into the existing one, especially once the plumbing is measured working.
9. **Put a mis-specification detector in the self-test.** On synthetic data where the knob is live by construction, assert the correctly-specified control says LIVE *and* the mis-specified variant says DEAD. A control that cannot detect its own mis-specification is how this class ships.

## Before you trust a green gate: is the instrument running at all?

**A gate whose instrument cannot execute is an absent gate, not a passing one — and from the outside the two are identical.**

Measured: a parity-evidence collector imported four module paths that did not exist in the wheel built from its own tag (three path errors, one module deleted in that release), so a declared `correlation_min = 0.95` sat unenforced for **five months** while downstream work assumed a declared gate was an enforced gate.

- **Assert the collector imports against the RELEASED ARTIFACT** — the wheel — not the working tree. An import error at the tag is indistinguishable from "no failures found" in every downstream report.
- Treat *"no failing runs recorded"* as ambiguous between *passing* and *never evaluated*, and resolve it by finding a **positive record of evaluation**.
- A sidecar committed before a run is a pre-registration only if it was **bound** to that run. Verify discovery explicitly and check the recorded outcome is non-null. (Bathos resolves no script unless the wrapped command ends in `.py`, so a bash-wrapped executor records nothing gate-able while looking tracked.)
- **Never read a verdict from exit code or console text, and do not trust a single catalog surface.** A stale `running` row can block `compact` from ingesting a finalised cool-tier fragment; read the cool-tier parquet directly and **say which surface produced the number**. Observed twice in one session: console `"success": true` and exit 0 against a record that says `fail`.
- A guard that greps *text* can fire on text that documents the hazard. Prefer checks resolving symbols, imports and values over ones pattern-matching source.
- **Corollary for consumers:** a dependency bump validated by packaging shape (entry counts, file lists) plus one downstream gate returning bit-identical numbers says nothing about paths that gate does not touch.

## The 5-phase protocol

The protocol is **operator-driven** (you orchestrate the agents) and **blind-first** (reconstructors see only the paper text, not your code or prior summaries). The steps are:

> **In Mode A, replace Phases 1–2 with `assets/mode_a_differential_counterfactual.md`** and keep Phases 3–5 as written. Blind reconstruction is there because no code exists to diff against; running it anyway against an available reference spends agents on rederiving what you can simply execute.

**Phase 1: Blind reconstruction (N independent agents, default N=3)**
- Each agent independently reconstructs the method from the paper **only**
- Reconstruction follows diverse lenses: mathematical formulation, algorithmic detail, experimental protocol
- Agents record ambiguities they encounter rather than guessing
- No cross-talk; agents do not see each other's work or your code

**Phase 2: Reconcile**
- Compare the N reconstructions; flag disagreements (likely indicating paper ambiguity or misreads)
- Map each reconstructed clause onto your actual code with a verdict (MATCH / DEVIATION / MISSING / AMBIGUOUS)
- Produce a checklist of code-to-paper correspondences

**Phase 3: Adversarial refutation (M independent attacks, default M=3)**
- Each attacker assumes a defect and tries to prove it using different evidence channels
- Channels: statistical correctness, hyperparameter fidelity, algorithmic structure
- Each attacker must state its assumption upfront (honesty-tax); default to "deviation" if evidence is inconclusive
- Goal: find or rule out mechanism-nullifying defects that unit tests missed

**Phase 4: Adjudicate**
- Confirm findings by ≥2-vote or hard evidence (runnable invariant tests you write)
- Rank severity: does this deviation affect the method's core behavior?
- Recommend fixes (code changes to restore parity, or documented deviations)

**Phase 5: Graded verdict**
- Compute grade from evidence: PARITY (all clear), PARTIAL (controlled deviations), FAIL (significant discrepancies)
- Produce an executable **invariant-test spec** — synthetic ground-truth tests that lock in the verdict
  (see AC-15 / AC-20: the tests are registered in the run's `output_paths` and checksummed via SHA; drift is detectable via `bth check`)
- Write a reproduce-the-protocol plan (how to restore your implementation to parity if needed)
- Populate `[confounds.reference_parity]` block for the next campaign

Phase templates (orchestrator-facing agent prompts) are bundled with this skill in `assets/` (`01_reconstruct.md` through `05_verdict.md`).

## Configuration: `parity.bth.toml`

Create a `parity.bth.toml` sidecar alongside the relevant script. A starter is bundled at `assets/parity.bth.toml.template`. Example structure:

```toml
[parity]
paper_pdf              = "path/to/paper.pdf"          # Source of truth (required)
impl_paths             = [
  "src/myproject/method.py",
  "src/myproject/baseline.py"
]                                                      # Your implementation files (required)
reference_code         = null                          # Optional: if the paper published code, path to it
citation_note          = "arXiv:1234.5678 describes the method in §3.2–3.4"
recon_lenses           = [
  "math",
  "algo",
  "protocol"
]                                                      # Default if omitted; customize for your paper
attack_lenses          = [
  "stats",
  "hyper",
  "struct"
]                                                      # Default if omitted
hypotheses             = [
  "core mechanism (coevolution reshuffle) is implemented faithfully",
  "metric readout captures the intended signal"
]                                                      # Your upfront hypotheses about what could go wrong
equivalence_bound      = 0.05                          # Tolerance for numeric equivalence (if applicable)
N                      = 3                             # Number of reconstructors (default 3)
M                      = 3                             # Number of refutation attackers (default 3)
```

**Required fields:** `paper_pdf`, `impl_paths`
**Optional fields (with sensible defaults):** `recon_lenses`, `attack_lenses`, `equivalence_bound`, `N`, `M`, `hypotheses`, `citation_note`, `reference_code`

An executable example of driving `parity_validate()` is bundled at `assets/example_parity_validate.py`.

## Orchestrator-owned re-derivation lock (Constraint 1)

**This is critical and skill-enforced only in v1** (no code-enforced gate):

> After the agents' phases complete, **you (the orchestrator) must independently re-derive the decisive findings using runnable tests.** Never trust an agent's assertion "parity is established"; run your own synthetic-ground-truth invariant tests to confirm.

In practice: if Phase 3 or 4 identifies a potential defect (e.g., "the method is invariant to coevolution signal"), write a `tests/test_<method>_invariants.py` that explicitly tests that claim and run it to failure and success.

**Why this matters:** The Zeinaty 2026 case in `asr` caught a mechanism-nullifying bug via this discipline: the paper's metric readout was mathematically invariant to the core mechanism (it contributed exactly zero to the reported result). Three sprints of unit tests missed this; an invariant-test specification locked in by orchestrator re-derivation found it immediately.

## Integration with claim-tier gates

Once a parity run completes successfully:

1. **Scaffold the confound block** — `bth claim scaffold` (see **bathos-campaigns**) emits a
   `[confounds.reference_parity]` block with reference_paper/reference_metric/reference_value/
   equivalence_bound and an empty `parity_run_id = ""` placeholder. Fill in the metadata fields
   by hand; leave `parity_run_id` empty.
2. **Bind the run — never hand-edit `parity_run_id`** — `bth campaign attest-parity
   <campaign-id> <parity-run-id>` (MCP: `claim_attest_parity`, gate F4). It validates the run is a
   real passing parity run, then atomically replaces the literal `parity_run_id = ""` placeholder
   and re-anchors the claim's recorded SHA. It works by text-replacing that exact empty string —
   if you fill it in by hand first there is nothing left to bind, and the command refuses with
   "parity_run_id already set or TOML format mismatch."
3. **At campaign conclude** (F2 gate) — the Union Gate reads the parity run's verdict:
   - PARITY or PARTIAL → confound marked controlled; campaign proceeds
   - FAIL → confound uncontrolled; confirmation/sequential campaign downgrades to `confounded`
4. **At campaign submit** (F3 gate) — a reproduction sidecar can declare a prerequisite parity run
   by *script stem*, not run ID — any future passing `literature_parity` run whose command
   contains this stem satisfies the gate:
   ```toml
   [reproduction]
   requires_parity_stem = "parity_validate"  # hard-blocks validation/production if unmet
   ```

## Evidence channels and evidence severity

Literature-parity relies on multiple evidence channels working together:

- **C1 (reconstruction parity):** N independent agents converge on the same interpretation of the paper (agreement = higher confidence)
- **C4 (adversarial severity):** M attackers try diverse refutation strategies; if all fail or find only minor deviations, confidence increases
- **D2 (evidence channels):** reconstruction via math, algorithm, protocol; refutation via stats, hyperparameter, structure
- **E1 (reproduction rung):** R0 = text parity only; R1 = numeric equivalence; R2–R4 = partial/full reproducibility with published code
- **D3 (manifest-declared mode):** your `parity.bth.toml` declares whether Mode A (code-published) or Mode B (text-only) applies

## Grading: the cap-lattice ceiling table

The final verdict (PARITY / PARTIAL / FAIL) is **computed automatically** from evidence using a cap-lattice (no human adjudication):

- **Invariant-test failure** → FAIL (no override)
- **Clause-parity % below threshold** → caps to PARTIAL
- **Adversarial survival** — all refutations failed or found only minor issues → boosts toward PARITY
- **Ambiguity load (load-bearing)** — unresolved paper ambiguities in core mechanism → caps to PARTIAL
- **Reproduction rung R2 or worse** (partial reproducibility, missing systems) → caps to PARTIAL

The compute-grade function returns the minimum across all applicable ceilings.

## Related

- **`assets/`** — phase-orchestration prompts (`01_reconstruct.md` through `05_verdict.md`), **`mode_a_differential_counterfactual.md`** (replaces Phases 1–2 when `reference_code` is set), `parity.bth.toml.template`, and `example_parity_validate.py`
- **`xtrax-activation-parity`** — the activation-level first-divergence trace, i.e. escalation layer 4 for JAX ports. Reach for it only after constants, parameter inventory and input equality are exhausted
- **bathos-campaigns** — claim-tier `[confounds.reference_parity]` integration, Union Gate
- **AC-16–AC-22** (epic-level acceptance criteria): all parity-related gates and integration points
- **Signal 13** (`bth sprint-audit`): flags a confirmation campaign citing a published-method baseline with uncontrolled `reference_parity`
