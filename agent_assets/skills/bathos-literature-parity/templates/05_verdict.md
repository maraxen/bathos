# Phase 5: Graded Verdict

**Role**: Synthesize all evidence, compute grade, and produce the final verdict.

**Instructions for the orchestrator:**

1. **Collect all evidence**:
   - Phase 1 reconstructions + ambiguity log
   - Phase 2 reconciliation checklist (MATCH / DEVIATION / MISSING / AMBIGUOUS counts)
   - Phase 3 refutation reports (defects found, confidence levels)
   - Phase 4 adjudication summary (severity ranking)

2. **Compute grade using the cap-lattice**:
   The final grade is the minimum across these ceilings:
   - **Invariant-test failure** → FAIL (no override)
   - **Clause-parity %** (MATCH count / total clauses):
     - ≥90% MATCH → no cap
     - 70–89% MATCH → caps to PARTIAL
     - <70% MATCH → caps to FAIL
   - **Adversarial survival**:
     - All M refutations found no defects → boost toward PARITY
     - 1+ defect found but ranked minor → no penalty
     - ≥1 major/critical defect → caps to PARTIAL or FAIL
   - **Ambiguity load**:
     - Unresolved ambiguities in *core mechanism* → caps to PARTIAL
     - Ambiguities in hyperparameters only → no cap
   - **Reproduction rung**:
     - R0 (text parity only) → no penalty
     - R1 (numeric equivalence on shared inputs) → no penalty
     - R2–R4 (partial/incomplete reproducibility) → caps to PARTIAL

   **Grade** = min(PARITY, ceiling 1, ceiling 2, ..., ceiling N)

3. **Write the verdict report** with:
   - Summary (one paragraph: PARITY / PARTIAL / FAIL with key evidence)
   - Clause-by-clause scorecard (checklist showing MATCH % and ambiguities)
   - Confirmed defects (if any), ranked by severity
   - Recommended fixes or documented deviations

4. **Produce the invariant-test spec**:
   - Identify 2–4 synthetic test cases that isolate core mechanisms (e.g., "coevolution signals improve accuracy")
   - Write a pytest file `tests/test_<method>_invariants.py` or similar that embodies these tests
   - The tests should pass if parity holds; fail if the mechanism is broken
   - Register this file in `output_paths` for the run (see AC-15)

5. **Write a reproduce-the-protocol plan**:
   - If fixes are needed, outline steps to restore parity
   - If deviations are accepted, explain why and document trade-offs
   - Include instructions for future maintainers to re-run this protocol

6. **Populate the claim-tier `[confounds.reference_parity]` block**:
   ```toml
   [[confounds]]
   id = "C_baseline"
   label = "baseline is the published method, not a weak reimplementation"
   [confounds.reference_parity]
   reference_paper = "Author YEAR"
   reference_metric = "relevant_metric"
   reference_value = 0.0
   equivalence_bound = 0.05
   parity_run_id = "run_<uuid>"  # the ID of THIS parity run
   ```

7. **Register outputs**:
   - Verdict report → `.praxia/docs/audits/YYMMDD_<paper-slug>-parity-verdict.md`
   - Invariant tests → `tests/test_<method>_invariants.py` (checksummed via `bth run --out`)
   - Reproduce plan → as appendix in verdict or as standalone document
   - All paths registered in the parity run's metadata via `output_paths`

8. **Final gate (Constraint 1)**: Confirm that the invariant tests pass locally on your implementation before declaring the run complete.
