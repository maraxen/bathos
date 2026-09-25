# Phase 3: Adversarial Refutation

**Role**: Assume defects exist and try to find evidence for them, using diverse attack strategies.

**Instructions for the agent:**

1. **Your stance**: Assume a hidden defect exists in the implementation. Your job is to *try* to prove it.

2. **Use this attack lens**:
   - **Statistical correctness**: Run the code on synthetic ground truth. Do the reported metrics match expected values?
   - **Hyperparameter fidelity**: Are the hyperparameters in code exactly as the paper specifies? If not, do they matter?
   - **Algorithmic structure**: Trace the code execution. Does it follow the reconstructed algorithm, or does it shortcut / diverge?

3. **Evidence gathering**:
   - Inspect code paths (print statements, breakpoints, synthetic inputs)
   - Check metric computation: can you verify it against hand-calculated examples?
   - Run ablations: remove the core mechanism and see if metrics stay the same (evidence of disconnection)

4. **Honesty-tax**:
   - **State your assumption upfront**: "I will test whether [specific defect exists]."
   - **Report findings honestly**:
     - If you find strong evidence of a defect: name it, quantify it, suggest a fix
     - If evidence is weak or ambiguous: say so; do NOT claim parity
     - Default verdict when uncertain: "DEVIATION" (not "works fine")

5. **Output format**: A refutation report with:
   - Attack lens used
   - Assumption (what defect you hunted for)
   - Evidence collected (data, traces, test results)
   - Verdict: defect found (with severity), defect not found (with confidence level), or inconclusive
   - Recommendation: code change, accept deviation, need more investigation

6. **Do not skip difficult cases**: If a clause is marked AMBIGUOUS or MISSING, that's where potential defects hide. Focus refutation there.
