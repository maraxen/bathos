# `literature-parity` — reference-parity-by-text validation workflow (SEED)

**Date:** 2026-06-17
**Status:** SEED for productization. Extracted from a worked instance in the `asr` project (build-by-doing). Input to a research → brainstorm → spec(adversarial) → impl sprint that turns this into a reusable, cross-project **bathos epic workflow** + a `/using-bathos` skill section + claim-tier wiring.
**Provenance:** validated on `asr` run `wf_12c972e0-9bb` (Zeinaty 2026 reshuffle reimplementation). The pattern **caught a mechanism-nullifying defect that 3 sprints of passing unit tests and a confirmatory N=30 run had missed** (see "Why this matters" below). Worked-instance verdict: `asr:.praxia/docs/audits/260617_zeinaty-parity-verdict.md`.

---

## 1. Problem

When we reimplement a method from a paper — especially one that **publishes no code** — "our X" silently becomes a different method than the published X. The downstream comparison ("our method beats/loses to baseline X") is then confounded by "our X ≠ their X," and the confound is invisible: the reimplementation runs, passes unit tests, and produces plausible numbers. This is a general, recurring failure (it is bathos confound **C7 / `[confounds.reference_parity]`**), and it is adjacent to but distinct from `/jax-port` graded parity (which assumes a reference *implementation* exists). Here the reference is **the text**.

**Two empirical proofs this is real and high-value:**
- *SMC generator* (asr, 2026-06-17): a root-detection bug mis-rooted every tree, passed 6 unit tests, produced plausible Hamming for ~3 sprints.
- *Zeinaty baseline* (asr, 2026-06-17): the reimplementation was wired to a readout that is **mathematically invariant** to the method's core mechanism — the entire coevolution reshuffle contributed **exactly zero** to every reported metric. The headline comparison was a *different experiment* than intended. No unit test caught it; a structured parity validation did, in one run.

## 2. The pattern (validated)

Build-by-doing first (validate one paper), then generalize. The redundancy is where the value is — a single reader laundering one set of errors is exactly the failure mode.

```
                 ┌─ reconstruct (math lens)    ┐
PDF (only) ──────┼─ reconstruct (algo lens)    ┼─→ reconcile ─→ clause checklist
                 └─ reconstruct (protocol lens)┘   (+ our code)   (MATCH/DEVIATION/
   (blind: no code, no prior summary, no peers)                    MISSING/AMBIGUOUS)
                                                                         │
   checklist + code + PDF ─→ refute (stats) ┐                           │
                          ─→ refute (hyper) ┼─→ adjudicate ─→ verdict ──┘
                          ─→ refute (struct)┘   (≥2-vote or    (graded + invariant-test
   (each assumes a hidden defect, tries to prove it)  hard-evidence)    spec + reproduce plan
                                                                         + reference_parity block)
```

**Phases:** (1) **blind reconstruction ×N** — independent readers reconstruct the method from the source-of-truth ONLY, with diverse lenses (math / hyperparameters / protocol); record ambiguities rather than guess. (2) **reconcile** — diff reconstructions (disagreement = paper ambiguity or misread) and map each clause onto our code with a verdict. (3) **adversarial refutation ×M** — diverse attack lenses (statistical correctness / hyperparameter fidelity / algorithmic structure), each told to *assume a defect and prove it*, defaulting to "deviation" when uncertain. (4) **adjudicate** — confirm by ≥2-vote or hard evidence; severity-rank; concrete fixes. (5) **verdict** — graded PARITY / PARTIAL / FAIL + an **executable invariant-test spec** (the parity is only *locked* by runnable synthetic-ground-truth tests, à la `tests/test_*_invariants.py`) + a reproduce-the-protocol plan + the `[confounds.reference_parity]` block.

**Graded by evidence available** (like `/jax-port`):
- **Mode A — code published:** run their code on shared inputs; diff outputs numerically; parity = within tolerance.
- **Mode B — text-only (the hard mode):** the phases above; parity is established by (i) clause-level text-parity, (ii) surviving adversarial refutation, (iii) passing invariant tests, (iv) reproducing the paper's *protocol* (often only qualitative direction-of-effect when the exact systems/metrics aren't reproducible on disk).

**Non-negotiable lock:** a verdict is not trusted until the orchestrator (not the agents) **re-derives the decisive findings with its own runnable tests.** In the Zeinaty instance, `tests/test_zeinaty_parity_invariants.py` (4/4) empirically reproduced the mechanism-nullifying defect and the faithful-engine result — the agents' claims were confirmed, not assumed.

## 3. Parameterized first-draft script (the extraction)

Generalize the worked instance by lifting the hardcoded paths/hypotheses into `args`:

```js
// args = {
//   paper_pdf: string,            // source of truth (PDF path)
//   impl_paths: string[],         // our reimplementation file(s)
//   prior_summary?: string,       // optional low-trust summary (cross-check only)
//   hypotheses?: string[],        // orchestrator's preliminary deviation hypotheses to verify
//   citation_note?: string,       // e.g. "arXiv:X is the *tool*, not the method"
//   recon_lenses?: [...], attack_lenses?: [...]   // defaults provided
// }
```
Phases, schemas, and prompts are identical to `asr` run `wf_12c972e0-9bb` (saved script: `asr:.../workflows/scripts/zeinaty-parity-validation-wf_12c972e0-9bb.js`) with `PDF`/`CODE`/`MD`/hypotheses read from `args`. The RECON / CHECKLIST / REFUTE / ADJ / VERDICT JSON schemas transfer verbatim.

## 4. Productization backlog (the sprint)

This seed is the input; the sprint should:
1. **Brainstorm** the abstraction boundary: what's domain-agnostic (reconstruction/reconcile/refute/adjudicate/verdict, schemas, the runnable-test lock) vs domain-specific (the invariant *kinds* — e.g. "marginal preservation," "detailed balance"). Decide whether invariant-test *authoring* is a workflow phase or an orchestrator step.
2. **Adversarial spec review** (spec-challenger/defender) of: redundancy counts (N,M) vs cost; how Mode A/B are selected; the reproduce-protocol phase when paper systems are absent; the PARITY/PARTIAL/FAIL grading rubric; how the verdict writes `[confounds.reference_parity]` + a `parity_run_id`.
3. **Impl** as a named bathos workflow (`literature-parity`) + a `/using-bathos` skill section ("Validating a reimplemented baseline") + a `bth` integration so a campaign's `[confounds.reference_parity]` block is gated on a passing parity run (mirror the claim-tier `register-before-runs` discipline).
4. **Self-documenting:** the workflow emits the clause checklist + verdict + invariant tests as durable artifacts (this run did: an audit doc + a pytest gate), so each application is reproducible and auditable.

## 5. Open design questions for the sprint

- **Invariant-test authoring:** workflow phase (an agent writes+runs them) vs orchestrator step (more control, the worked instance did the latter). The runnable-test *lock* is non-negotiable either way.
- **Redundancy economics:** 3+3 was decisive here at ~760k tokens / ~17 min. Scale N,M to paper complexity? Loop-until-no-new-defects?
- **Reproduce-protocol when systems are absent:** the Zeinaty paper's β-lactamase/ESMFold metrics weren't reproducible on disk → fell back to a faithful-*protocol* reproduction on an on-disk model. Codify this graceful degradation + its honest labeling.
- **Paper ambiguity handling:** reconstruction disagreements that resolve to "paper underspecified" must surface as explicit caveats in the verdict, not silently picked.
- **Relationship to `/jax-port`:** Mode A overlaps `/jax-port` graded parity — compose rather than duplicate.

**Cross-refs:** worked instance `asr:.praxia/docs/audits/260617_zeinaty-parity-verdict.md`; the broader rigor systematization seed `research/260616_bathos-long-horizon-rigor.md` §10.1 (this operationalizes its "literature-reimplementation equivalence" track); claim-tier `[confounds.reference_parity]` in the `using-bathos` skill.
