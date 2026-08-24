#!/usr/bin/env python3
"""Does a configuration knob actually reach the computation?

THE FAILURE THIS EXISTS FOR. A knob is accepted by the API, travels partway, and is dropped
before the kernel. Nothing errors. The run completes, the numbers look plausible, and the
result is invalid in a way no unit test notices because the unit tests assert the knob was
*stored*, not that it was *used*.

Observed instances, all in one project, all found by accident after results existed:
  * a fusion object constructed with no strategy and no weights, so `multi_state_strategy`,
    `state_weights` and `temperature` were silently discarded on that path;
  * a `sidechain_conditioning=True` flag that built context and handed it to a model
    constructed without the branch that reads it -- bit-identical logits, no error;
  * a `product` fusion that applied no scale, computing a weighted geometric mean while
    still being called `product`.

WHY THE JAXPR IS THE RIGHT ORACLE. Syntactic analysis (ast, libcst) can prove a field is
READ. It cannot prove the value influenced the output -- a value can be read and then
multiplied by zero, overwritten, or closed over as a constant that never varies. A
perturbation test is better but still indirect: an unchanged output may mean the knob is
inert, or merely that this input was insensitive to it.

The jaxpr settles it. Trace the function twice under two knob values and compare the jaxprs:

  * the knob appears as a differing CONSTANT      -> it reached the computation, baked in at
                                                     trace time (recompiles when it changes);
  * the jaxprs differ in STRUCTURE               -> it reached the computation and changed
                                                     the graph;
  * the jaxprs are IDENTICAL                     -> the knob provably did not reach this
                                                     computation. Not "probably" -- the
                                                     traced graph is what executes.

The last case is the decisive one, and it is what none of the incidents above had.

USE. Import `probe_knob` and give it a thunk that builds and traces your function for a knob
value. Everything is host-side; nothing here runs inside a traced region.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

__all__ = ["KnobVerdict", "REACHED_CONST", "REACHED_STRUCTURE", "UNREACHED", "probe_knob"]

REACHED_CONST = "reached_as_constant"
REACHED_STRUCTURE = "reached_changed_structure"
UNREACHED = "did_not_reach"


@dataclass(frozen=True)
class KnobVerdict:
  """What tracing under two values of one knob established."""

  knob: str
  verdict: str
  value_a: str
  value_b: str
  jaxpr_digest_a: str
  jaxpr_digest_b: str
  n_eqns_a: int
  n_eqns_b: int
  n_consts_a: int
  n_consts_b: int
  detail: str = ""
  notes: list[str] = field(default_factory=list)

  @property
  def reached(self) -> bool:
    return self.verdict != UNREACHED

  def as_dict(self) -> dict[str, Any]:
    return {
      "knob": self.knob,
      "verdict": self.verdict,
      "reached": self.reached,
      "value_a": self.value_a,
      "value_b": self.value_b,
      "jaxpr_digest_a": self.jaxpr_digest_a,
      "jaxpr_digest_b": self.jaxpr_digest_b,
      "n_eqns_a": self.n_eqns_a,
      "n_eqns_b": self.n_eqns_b,
      "n_consts_a": self.n_consts_a,
      "n_consts_b": self.n_consts_b,
      "detail": self.detail,
      "notes": list(self.notes),
    }


def _digest(text: str) -> str:
  return hashlib.sha256(text.encode()).hexdigest()[:16]


def _structure_signature(closed_jaxpr: Any) -> str:
  """A signature of the GRAPH SHAPE, independent of constant values.

  Reads the jaxpr OBJECT rather than munging its printed text. An earlier text-based version
  blanked any line beginning with ``{ lambda`` to drop the constvars binder -- which erased
  the ENTIRE graph for short jaxprs, since those render on a single line, and so reported two
  structurally different graphs as identical. The synthetic ground-truth check caught it,
  which is precisely what that check is for.

  Primitive names and parameter KEYS are included; parameter VALUES are not, because a knob
  baked in as a literal appears there and would otherwise make every constant-only difference
  look structural.
  """
  jaxpr = getattr(closed_jaxpr, "jaxpr", closed_jaxpr)
  parts: list[str] = []
  for eqn in jaxpr.eqns:
    parts.append(eqn.primitive.name)
    parts.append(",".join(sorted(str(k) for k in eqn.params)))
  parts.append("in:" + ";".join(str(getattr(v, "aval", "lit")) for v in jaxpr.invars))
  parts.append("out:" + ";".join(str(getattr(v, "aval", "lit")) for v in jaxpr.outvars))
  return "|".join(parts)


def probe_knob(
  knob: str,
  trace_fn: Callable[[Any], Any],
  value_a: Any,
  value_b: Any,
) -> KnobVerdict:
  """Trace under two knob values and report whether the knob reached the computation.

  Args:
    knob: Name of the knob, for the report.
    trace_fn: Called as ``trace_fn(value)``; must return a ``jax.core.ClosedJaxpr`` --
      typically ``jax.make_jaxpr(f)(*args)`` with the knob applied. Build the WHOLE object
      graph inside this thunk: the defects this catches live in construction, so a thunk that
      reuses a pre-built model cannot see them.
    value_a: First knob value.
    value_b: Second knob value. MUST be semantically different; two equal values make the
      probe vacuously report UNREACHED.

  Returns:
    A :class:`KnobVerdict`.

  Raises:
    ValueError: If the two values are equal, which would make the result meaningless.
  """
  if value_a == value_b:
    msg = (
      f"probe_knob({knob!r}) needs two DIFFERENT values; got {value_a!r} twice. Equal values "
      f"trace to identical jaxprs by construction and would report UNREACHED for every knob, "
      f"including working ones."
    )
    raise ValueError(msg)

  jaxpr_a = trace_fn(value_a)
  jaxpr_b = trace_fn(value_b)

  text_a, text_b = str(jaxpr_a), str(jaxpr_b)
  struct_a, struct_b = _structure_signature(jaxpr_a), _structure_signature(jaxpr_b)

  eqns_a = len(getattr(jaxpr_a, "eqns", getattr(jaxpr_a.jaxpr, "eqns", [])))
  eqns_b = len(getattr(jaxpr_b, "eqns", getattr(jaxpr_b.jaxpr, "eqns", [])))
  consts_a = len(getattr(jaxpr_a, "consts", []))
  consts_b = len(getattr(jaxpr_b, "consts", []))

  notes: list[str] = []
  if text_a == text_b:
    verdict = UNREACHED
    detail = (
      "Traced graphs are byte-identical under both values. The knob provably did not reach "
      "this computation -- the jaxpr is what executes."
    )
  elif struct_a == struct_b:
    verdict = REACHED_CONST
    detail = (
      "Graph structure identical, constants differ: the knob reached the computation as a "
      "trace-time constant. It takes effect, but changing it forces a recompile -- and a "
      "cached compilation keyed on the wrong value would silently reuse the old one."
    )
    notes.append("baked in at trace time; verify the cache key includes this knob")
  else:
    verdict = REACHED_STRUCTURE
    detail = "Graph structure differs: the knob changed the computation itself."

  return KnobVerdict(
    knob=knob,
    verdict=verdict,
    value_a=repr(value_a),
    value_b=repr(value_b),
    jaxpr_digest_a=_digest(text_a),
    jaxpr_digest_b=_digest(text_b),
    n_eqns_a=eqns_a,
    n_eqns_b=eqns_b,
    n_consts_a=consts_a,
    n_consts_b=consts_b,
    detail=detail,
    notes=notes,
  )
