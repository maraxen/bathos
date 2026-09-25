# Phase 2: Reconcile

**Role**: Compare reconstructions and map them to the actual code.

**Instructions for the orchestrator and reconciliation agent:**

1. **Gather the N reconstructions** from Phase 1.

2. **Compare reconstructions clause by clause**:
   - Where they agree → note it (agreement increases confidence)
   - Where they disagree → the disagreement likely flags a paper ambiguity or a misread
   - Document each disagreement with the source (which reconstruction(s) diverged and why)

3. **Map each reconstructed clause to your actual code**:
   - Read the implementation files specified in `parity.bth.toml`
   - For each reconstructed element (function, loop, hyperparameter choice), find its code equivalent
   - Assign a verdict: MATCH / DEVIATION / MISSING / AMBIGUOUS
     - **MATCH**: code implements the reconstructed element faithfully
     - **DEVIATION**: code differs intentionally or unintentionally (note the difference)
     - **MISSING**: reconstruction expects something not in the code (e.g., an ablation)
     - **AMBIGUOUS**: paper + reconstruction are unclear; code implements one interpretation

4. **Output format**: A clause checklist with columns:
   - Reconstructed element (with source: which lens, which reconstruction)
   - Code location (file:line or function name)
   - Verdict (MATCH / DEVIATION / MISSING / AMBIGUOUS)
   - Notes (discrepancy details if applicable)

5. **Severity note**: A MISSING or AMBIGUOUS verdict for a *core* mechanism (not a hyperparameter) should be flagged for Phase 3 adversarial refutation.
