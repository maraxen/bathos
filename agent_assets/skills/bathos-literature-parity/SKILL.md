---
name: bathos-literature-parity
description: Validating a reimplemented baseline against a published method before trusting it as a claim-tier confound control
triggers: [literature parity, reimplemented baseline, parity.bth.toml, reference_parity, blind reconstruction, adversarial refutation]
---

# bathos-literature-parity

When you reimplement a method from a published paper — especially one that publishes no reference code — the reimplementation can silently diverge from the described method, confounding any downstream comparison (`[confounds.reference_parity]` in claim-tier language, see **bathos-campaigns**). A unit test cannot catch this: the reimplemented method runs, passes internal checks, and produces plausible numbers. This skill documents bathos's structured validation protocol.

## When to use literature-parity validation

**Use this workflow when:**
- Your project reimplements a method from a peer-reviewed publication
- The original paper publishes no reference code (or the code diverges significantly from the paper text)
- The reimplementation will be compared against the published results or other baselines
- You need to flag the `[confounds.reference_parity]` confound as *controlled* for downstream claim-tier gates

**Outcome:** A graded parity run with verdict PARITY (faithfully reimplemented), PARTIAL (controlled deviations documented), or FAIL (significant discrepancies). The verdict controls whether the F2 conclude-gate and F3 submit-gate allow downstream campaigns to proceed.

## The 5-phase protocol

The protocol is **operator-driven** (you orchestrate the agents) and **blind-first** (reconstructors see only the paper text, not your code or prior summaries). The steps are:

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

Phase templates (orchestrator-facing agent prompts) are bundled with this skill in `templates/` (`01_reconstruct.md` through `05_verdict.md`).

## Configuration: `parity.bth.toml`

Create a `parity.bth.toml` sidecar alongside the relevant script. A starter is bundled at `templates/parity.bth.toml.template`. Example structure:

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

An executable example of driving `parity_validate()` is bundled at `templates/example_parity_validate.py`.

## Orchestrator-owned re-derivation lock (Constraint 1)

**This is critical and skill-enforced only in v1** (no code-enforced gate):

> After the agents' phases complete, **you (the orchestrator) must independently re-derive the decisive findings using runnable tests.** Never trust an agent's assertion "parity is established"; run your own synthetic-ground-truth invariant tests to confirm.

In practice: if Phase 3 or 4 identifies a potential defect (e.g., "the method is invariant to coevolution signal"), write a `tests/test_<method>_invariants.py` that explicitly tests that claim and run it to failure and success.

**Why this matters:** The Zeinaty 2026 case in `asr` caught a mechanism-nullifying bug via this discipline: the paper's metric readout was mathematically invariant to the core mechanism (it contributed exactly zero to the reported result). Three sprints of unit tests missed this; an invariant-test specification locked in by orchestrator re-derivation found it immediately.

## Integration with claim-tier gates

Once a parity run completes successfully:

1. **Record the run ID** — `bth show <run-id>` to get its UUID
2. **Populate `[confounds.reference_parity]` in your campaign's `claim.bth.toml`** (see **bathos-campaigns**):
   ```toml
   [[confounds]]
   id = "C_baseline"
   label = "baseline is the published method, not a weak reimplementation"
   [confounds.reference_parity]
   parity_run_id = "run_12abc345..."  # from the parity run
   ```
3. **At campaign conclude** (F2 gate) — the Union Gate reads the parity run's verdict:
   - PARITY or PARTIAL → confound marked controlled; campaign proceeds
   - FAIL → confound uncontrolled; confirmation/sequential campaign downgrades to `confounded`
4. **At campaign submit** (F3 gate) — a reproduction sidecar can declare a prerequisite parity run:
   ```toml
   [reproduction]
   requires_parity = "run_12abc345..."  # hard-blocks validation/production if uncontrolled
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

- **`templates/`** — phase-orchestration prompts (`01_reconstruct.md` through `05_verdict.md`), `parity.bth.toml.template`, and `example_parity_validate.py`
- **bathos-campaigns** — claim-tier `[confounds.reference_parity]` integration, Union Gate
- **AC-16–AC-22** (epic-level acceptance criteria): all parity-related gates and integration points
- **Signal 13** (`bth sprint-audit`): flags a confirmation campaign citing a published-method baseline with uncontrolled `reference_parity`
