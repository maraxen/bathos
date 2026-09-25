# Phase 1: Blind Reconstruction

**Role**: Independently reconstruct the method from the source paper, working blind (no code access, no prior summaries).

**Instructions for the agent:**

1. **Read the paper ONLY** — your source of truth is the PDF provided. Do not access the project's code, documentation, or any prior summaries.

2. **Reconstruct the method using this lens**:
   - **Mathematical formulation**: the equations, constraints, and objective functions
   - **Algorithmic detail**: pseudocode, loop structure, data flow
   - **Experimental protocol**: hyperparameters, initialization, stopping criteria, metric computation

3. **Record ambiguities, not guesses**:
   - If the paper underspecifies something (e.g., "appropriate learning rate"), flag it explicitly
   - If notation is overloaded or unclear, state your interpretation and note the ambiguity
   - If the paper refers to appendices that are absent, record this

4. **Output format**: A structured reconstruction document with:
   - Section for each lens (math, algorithm, protocol)
   - Explicit list of ambiguities discovered
   - Any assumptions you made to fill gaps

5. **Guardrail**: If you find yourself thinking "the code probably does this," stop and record that as an ambiguity instead.
