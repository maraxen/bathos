---
name: bathos-knob-reachability
description: Use when a run's configuration surface must be shown to actually reach the computation — enumerating every knob on a spec/config class, mapping it against a reference implementation, and proving by jaxpr inspection that each one either does or does not influence the result.
---

# bathos-knob-reachability

A sidecar pre-registers *what you expect to observe*. This pre-registers *that the thing you
configured was actually computed*. Those are different, and the gap between them is where
silent invalidity lives.

## The failure this exists for

A knob is accepted by the API, travels partway, and is dropped before the kernel. Nothing
errors. The run completes, the numbers look plausible, and the result is invalid in a way no
unit test notices — because the tests assert the knob was *stored*, not that it was *used*.

Three instances from one project, all found by accident, all after results existed:

| defect | what happened | what it looked like |
|---|---|---|
| discarded fusion knobs | a fusion object built with no strategy and no weights, so `multi_state_strategy`, `state_weights`, `temperature` were dropped on that path | a whole campaign sampled at an effective temperature × S off spec |
| dead conditioning branch | a `sidechain_conditioning=True` flag that built context and handed it to a model constructed without the branch that reads it | **bit-identical** logits, no error, an exact-0.0000 "clean null" |
| renamed semantics | a `product` fusion that applied no scale — a weighted geometric mean still called `product` | correct-looking numbers, wrong operator |

The pattern is one thing: **acceptance is not reachability.**

## Three tiers, and only the third is decisive

| tier | instrument | proves | cost |
|---|---|---|---|
| 1 · static | `ast` field enumeration + a **declared** correspondence table | completeness — no knob forgotten | minutes |
| 2 · dynamic | perturb the knob, observe the output | the knob does *something* | cheap, indirect |
| 3 · trace | **jaxpr comparison under two knob values** | the knob **reached the computation** | cheap, decisive |

Tier 1 cannot prove influence: a field can be read and then overwritten, or multiplied by
zero. Tier 2 is better but ambiguous — an unchanged output may mean the knob is inert, or
merely that *this input* was insensitive to it.

Tier 3 settles it. The jaxpr is what executes.

- jaxprs differ in **structure** → the knob changed the computation.
- jaxprs differ only in **constants** → it reached the computation, baked in at trace time.
  It works, but changing it forces a recompile, and a compilation cache keyed without it will
  silently reuse the old graph.
- jaxprs are **identical** → the knob provably did not reach the computation. Not "probably".

## Use

```python
from jaxpr_reachability import probe_knob

def trace(temperature):
    spec = MySpec(temperature=temperature)     # build the WHOLE graph inside the thunk
    return jax.make_jaxpr(build_fn(spec))(x)   # defects live in construction

verdict = probe_knob("temperature", trace, 0.1, 0.9)
assert verdict.reached, verdict.detail
```

**Build the whole object graph inside the thunk.** The defects in the table above all live in
*construction* — a thunk that reuses a pre-built model cannot see them.

**Pass two genuinely different values.** Equal values trace to identical jaxprs by
construction and would report `did_not_reach` for every knob including working ones;
`probe_knob` raises rather than let that happen.

## Declare the expectation per knob, and expect some to be inert

Not every knob should change the computation, so "reached" is not universally the pass
condition. Declare it:

| declared | meaning | example |
|---|---|---|
| `must_reach` | inert is a defect | `temperature`, `state_weights`, any conditioning flag |
| `must_not_reach` | reaching is a defect | a seed under a deterministic path; a label carried for provenance only |
| `conditional` | depends on another knob | `sc_mode` matters only when side-chain context is on |

A `must_not_reach` knob is not a formality. A path documented as key-invariant that turns out
to consume the key is the same class of bug pointing the other way — and one project found
exactly that: 100% of an apparent "seed noise" floor was an unpinned decoding order.

## The reference-parity half, and how NOT to do it

When the code reimplements a published method, enumerate the reference's parameter surface
too and map it onto yours. Three questions, and the third is the dangerous one:

1. reference flags with no counterpart — capability gap;
2. your fields with no reference counterpart — extension, fine, but unvalidated upstream;
3. **flags in both under a name suggesting equivalence** — this is where `product` came to
   mean a weighted geometric mean while still being called `product`.

**Do not infer the mapping by name matching.** Tried on a real pair, naive matching resolved
**3 of 49** reference flags — not because the implementation covered 3, but because the two
surfaces use different conventions (`fixed_residues` ↔ `fixed_mask`, `seed` ↔ `random_seed`,
`omit_AA` ↔ a bias field). A "0 risky knobs" reading from a 6%-hit-rate matcher is a blunt
instrument reporting a confident number.

The mapping must be **declared**: a checked-in table where every reference flag is either
mapped to a field or explicitly marked absent-by-design, which tooling then validates for
*completeness* and fails on anything that is neither. Same discipline as
**bathos-literature-parity**'s declared clauses, applied to the config surface.

## Coverage tracks past pain, not risk

Audit a real project and the shape recurs: the spec class whose defects already burned someone
has full coverage; the one with the highest historical defect density has the least. In the
worked example, the sampling spec had **89 fields and zero gaps** (its knobs had caused two
incidents), while the jacobian spec had **63 fields and 11 gaps** — including an entire
`combine*` family, which is a *fusion-shaped* surface, the exact defect class from the table
above, on a path whose core output had itself been identically zero until a late fix.

So: **enumerate the whole surface mechanically.** Coverage chosen by memory encodes which bugs
you happen to have survived.

## Tooling

| file | what it does |
|---|---|
| `tools/jaxpr_reachability.py` | tier 3 — `probe_knob()` returning a `KnobVerdict` |
| `tools/selftest_jaxpr_reachability.py` | synthetic ground truth; **run before trusting the probe** |

The self-test is not ceremony. On first run it exposed a real bug in the probe (a text-based
structural comparison that erased single-line jaxprs and called two different graphs
identical) and corrected a wrong expectation of the author's. An unverified oracle is exactly
what this skill argues against.

For tier 1, enumerate the field surface from the AST rather than by importing — it sees fields
regardless of runtime reachability, and it does not need the target's dependencies installed.
Make the matcher for "already covered" deliberately over-broad, so the reported gap is a
**lower** bound: under-reporting the hole is the safe direction for a coverage claim.

`libcst` is worth reaching for only if you want comments as declared intent (`ast` discards
them, and `# deliberately unwired` is exactly the signal you want) or codemod-generated stubs.
It does not help with reachability — it is as syntactic as `ast`.

## Related

- **using-bathos** — sidecars, controls discipline, the run catalog
- **bathos-campaigns** — claim-tier pre-registration; a knob audit belongs in
  `[[assumptions]]` as a load-bearing, testable one
- **bathos-literature-parity** — declared-clause reconstruction; this skill is its config-surface analogue
