# Mode A: Differential Counterfactual

**Use instead of Phases 1–2 when `reference_code` is set in `parity.bth.toml`.** Blind reconstruction exists because there is no code to diff against; when there is, a stronger instrument is available and reconstruction is largely wasted effort. Keep only the ambiguity ledger, then feed this into Phase 4 (Adjudicate).

**Role**: Establish, by measurement rather than by reading, whether a suspected defect in the port actually causes the observed disagreement.

---

## The move

Do not reason about whether a suspected defect matters. **Introduce it into the reference and measure whether the reference then reproduces the port.**

```
arm A: reference, unmodified            → must reproduce the observed disagreement
arm B: reference, crippled exactly as the port is crippled
```

If the defect is the cause, arm B agrees with the port. A plausible-looking story cannot satisfy this, which is the whole point.

**Run arm A rather than quoting the published number.** Reproducing it from a fresh run is what proves the harness measures the same thing. Quoting it lets a harness change — a different context, a different pooling order, a different position set — masquerade as an effect of the intervention.

---

## Instructions for the agent

1. **Escalate in this order. Stop as soon as a layer answers the question; the cheap layers are also the most decisive.**

   1. **Constants on the LOADED model vs the artifact's own declared values.** Hyperparameters that size a *sequence* axis rather than a feature axis are invisible to every shape check — deserialisation succeeds, nothing warns. Ask the library for its own derived value and compare against what the constructed object actually holds. *If a second code path in the same library passes the same value correctly, that path is a ready-made discriminating control: it shows the value is derivable and deliverable and only one site loses it.*
   2. **Full parameter-inventory diff, BOTH directions.** Which reference tensors have no home in the port's skeleton? Which port tensors have no source in the reference? A missing *trained* tensor and a phantom *untrained* one are different bugs with different fixes — and the second additionally needs a re-serialised artifact, not just a code change.
      - To show a suspect tensor is untrained initialisation rather than a misassigned real one: compare it against the framework's init bound **and** against the distribution of genuinely trained tensors of the same shape. *"0 of 83 trained tensors fall inside this bound, and this one does"* is evidence. "It looks small" is not.
      - Positional deserialisation means a phantom leaf could shift everything after it. **Spot-check several same-named tensors for exact equality** to rule out a cascade before treating the findings as independent.
   3. **Input equality** — same items, same order, same encoding, **asserted rather than assumed**. Alphabet, frame and index-convention mismatches masquerade as numerical divergence.
   4. **Only then, activations** — the most expensive instrument. For JAX ports see the `xtrax-activation-parity` skill.

2. **Design the control before you design the comparison.** The counterfactual is worthless unless arm B genuinely differs from arm A. See "Designing the control" in `SKILL.md` — in particular: match the control to its ceiling in **both pairing and n**, and run the structural tell (*does the "these differ" statistic outscore the "these are identical" reference?*). A mis-specified control **cannot fire** and fails invisibly, reporting a clean number and a clean verdict.

3. **Verify the intervention is live, independently of the result.** Log the shape or value the reference actually received under each arm, and probe the pre-edit code path out of version control to confirm it is unchanged when the override is absent. Bit-identity without the override plus a measurable difference with it makes the plumbing *identified*, not merely consistent.

4. **Report existence and attribution as separate claims.**
   - *Existence*: the defect is real, with a reproducer that fails today and passes when fixed.
   - *Attribution*: the defect accounts for X% of the observed gap, measured.
   - A defect can be indefensible and account for ~0. Never let the first imply the second, and do not write an upstream issue whose framing implies causation before attribution has run.

5. **Scope a negative attribution to the context measured.** A defect a given input cannot *express* — too few ligand atoms, too short a sequence, a feature never exercised — reads as harmless there and may be severe elsewhere. State the context in the same breath as the number.

6. **Output format**: a report carrying
   - the escalation layer that answered the question (and why the cheaper layers did not)
   - the control's values, reported **before** the attribution numbers, with each conjunct separately
   - arm A's reproduction check against the published/observed value
   - existence and attribution as two clearly separated verdicts
   - what the result **cannot** exonerate, enumerated rather than left implicit

7. **If the control fails, withhold the statistics that depend on it** and say why. A vacuous experiment's unflattering number is exactly as uninformative as its flattering one.
