#!/usr/bin/env python3
"""Synthetic ground truth for the jaxpr reachability probe. Run this before trusting it.

Per BATHOS.md: verify a measurement pipeline against synthetic invariants before using it for
any conclusion. That applies with extra force here, because this instrument's entire job is to
distinguish a knob that reaches the computation from one that does not -- so it must be shown
to do that on cases where the answer is known by construction.

It has already earned its keep. On first run it exposed a real bug: the structural comparison
was text-based and blanked any line starting with ``{ lambda``, which erased the whole graph
for short jaxprs (they render on one line) and reported two structurally different graphs as
identical. It also corrected a wrong expectation of the author's -- see the annihilated-by-zero
case below.

Run:  python selftest_jaxpr_reachability.py
Exit: 0 if every case matches, 1 otherwise.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jax
import jax.numpy as jnp
from jaxpr_reachability import REACHED_CONST, REACHED_STRUCTURE, UNREACHED, probe_knob

X = jnp.ones((4,))


def trace_used(scale):
  """Knob genuinely multiplies the input."""
  return jax.make_jaxpr(lambda x: x * scale)(X)


def trace_ignored(scale):  # noqa: ARG001
  """The discarded-knob shape: accepted by the signature, then dropped on the floor."""
  return jax.make_jaxpr(lambda x: x * 2.0)(X)


def trace_annihilated(scale):
  """Read, then annihilated.

  JAX constant-folds ``scale * 0.0`` at trace time, so the knob is genuinely INERT and
  UNREACHED is the CORRECT verdict. This is the case that shows the jaxpr beating syntactic
  analysis: ``ast`` sees the name being read and would score it covered.
  """
  return jax.make_jaxpr(lambda x: x + scale * 0.0)(X)


def trace_branch(flag):
  """The dead-branch shape: a boolean selecting a whole sub-computation."""
  if flag:
    return jax.make_jaxpr(lambda x: jnp.tanh(x) + 1.0)(X)
  return jax.make_jaxpr(lambda x: x)(X)


CASES = [
  ("knob multiplies the input", trace_used, 2.0, 3.0, {REACHED_CONST, REACHED_STRUCTURE}),
  ("knob accepted then discarded", trace_ignored, 2.0, 3.0, {UNREACHED}),
  ("knob read but annihilated by zero", trace_annihilated, 2.0, 3.0, {UNREACHED}),
  ("boolean selects a branch", trace_branch, True, False, {REACHED_STRUCTURE}),
]


def main() -> int:
  failures = 0
  for label, fn, a, b, expected in CASES:
    verdict = probe_knob(label, fn, a, b)
    ok = verdict.verdict in expected
    failures += not ok
    print(f"{'PASS' if ok else 'FAIL'}  {label}")
    print(f"        verdict={verdict.verdict}  expected one of {sorted(expected)}")
    print(f"        eqns {verdict.n_eqns_a}->{verdict.n_eqns_b}")

  try:
    probe_knob("equal values", trace_used, 2.0, 2.0)
  except ValueError:
    print("PASS  equal values raise instead of reporting a vacuous UNREACHED")
  else:
    print("FAIL  equal values did NOT raise -- the probe can report UNREACHED vacuously")
    failures += 1

  print(f"\n{failures} failure(s)")
  return 1 if failures else 0


if __name__ == "__main__":
  sys.exit(main())
