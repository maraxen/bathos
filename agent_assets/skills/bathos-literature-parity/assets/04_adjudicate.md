# Phase 4: Adjudicate

**Role**: Confirm findings and rank severity.

**Instructions for the orchestrator:**

1. **Gather the M refutation reports** from Phase 3.

2. **Confirm findings**:
   - Where ≥2 attackers reported the same defect → high confidence, likely real
   - Where only 1 attacker found a defect → might be a false alarm; re-check
   - Where attackers disagree → note the disagreement; may need more investigation

3. **Collect hard evidence** (if available):
   - **Runnable invariant tests**: If Phase 3 or the reconstruction identified a core mechanism, can you write a synthetic test that isolates it?
   - Example: If the method claims "coevolution signals improve accuracy," run the method with coevolution signals on vs. off on a controlled synthetic task. The deviation should be measurable.
   - These tests are *your* (the orchestrator's) contribution; agents propose, you verify.

4. **Rank severity**:
   - **Critical**: core mechanism missing or inverted; method is fundamentally different
   - **Major**: important detail absent or wrong; affects reported metrics
   - **Minor**: hyperparameter mismatch or edge-case handling; unlikely to affect conclusions
   - **Accepted deviation**: code differs intentionally for reproducibility, performance, or clarity (documented)

5. **Output format**: An adjudication summary with:
   - Confirmed defects (≥2-vote or hard-evidence corroboration)
   - Severity ranking (critical / major / minor / accepted)
   - Recommended fixes (code changes, documentation updates, or planned deviations)
   - Verdict direction for Phase 5 (towards PARITY, PARTIAL, or FAIL)

6. **Critical guardrail**: Do NOT skip runnable tests for claimed core-mechanism defects. Agent assertions alone are not enough (Constraint 1).
