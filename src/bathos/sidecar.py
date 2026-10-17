from __future__ import annotations

import builtins
import hashlib
import json
import logging
import math
import os
import tomllib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import duckdb

from bathos.telemetry import event

logger = logging.getLogger(__name__)


class SidecarError(Exception):
    pass


class SidecarKind(str, Enum):  # noqa: UP042 - inheriting str preserves str(Enum) returning Enum member value for serialization
    EXPERIMENT = "experiment"
    BENCHMARK = "benchmark"
    VALIDATION = "validation"
    DEBUG = "debug"


@dataclass
class OutcomeSpec:
    condition: str
    decision: str
    reasoning: str = ""
    is_residual: bool = False
    adversarial_check: str | None = None
    source: str = ""
    # Escape hatch for the multiple-comparisons lint (debt #1071): set to e.g. "holm" once
    # a real correction (bathos.stats_gates.holm_correction) has been applied upstream to
    # this outcome's underlying test battery, to suppress the static OR-chain heuristic.
    multiple_comparisons_correction: str = ""


@dataclass
class ReproductionBlock:
    """Reproduction metadata for experiment sidecars (optional [reproduction] block)."""

    reproduces_paper: str = ""  # DOI or citation string
    reproduces_run: str = ""  # run UUID
    tolerance_pct: float | None = None
    requires_pass_stem: str = ""  # script stem that must have outcome='pass' first
    requires_parity_stem: str = (
        ""  # script stem that must have a passing parity run first (F3 gate)
    )


@dataclass
class RequiresWaiver:
    """`[requires.waiver]` sub-table: a reasoned opt-out from a `[requires]` check."""

    reason: str = ""


@dataclass
class RequiresBlock:
    """Optional `[requires]` block (experiment sidecars only): a precondition on an artifact.

    Parsed only; enforcement is the B2 gate, not this module. The gate reads the JSON value at
    `json_key` in the artifact and compares `str(value)` to `must_equal`. `must_equal` is stored
    as given: a literal, or the token "HEAD", which the gate resolves to the current commit.
    A `[requires]` table with no `artifact` and no waiver is accepted by the parser but the gate
    reports it as unmet. `applies_if` is stored as given and not evaluated yet; the requirement
    always applies.
    """

    workload: str | None = None  # free string, e.g. "jax"
    artifact: str | None = None  # glob, relative to the sidecar's directory (stored as given)
    json_key: str | None = None  # dotted key path into the artifact JSON
    must_equal: str | None = None  # literal value, or "HEAD"
    reject_suffixes: list[str] = field(default_factory=lambda: ["-dirty", "-unverified"])
    applies_if: str | None = None  # free-form condition string (stored as given)
    waiver: RequiresWaiver | None = None
    hint: str | None = None  # echoed verbatim as the resolution hint when the requirement fails


@dataclass
class BudgetBlock:
    """Optional `[budget]` block (experiment sidecars only): a wall-time estimate for a run.

    Parsed only; `tolerance` is the relative slack the later budget check will apply, in (0, 1).
    """

    est_wall_s: float | None = None
    per_unit_s: float | None = None
    n_units: int | None = None
    probe_record: str | None = None  # path relative to the sidecar's directory (stored as given)
    record_per_unit_key: str | None = None  # dotted key path into probe_record's JSON
    tolerance: float = 0.5


@dataclass
class ControlsBlock:
    """Control arm specification for experiment sidecars (optional [controls] block)."""

    positive_outcome: list[str] = field(default_factory=list)
    negative_outcome: list[str] = field(default_factory=list)


@dataclass
class DifferentialBlock:
    """Instrument-sensitivity pre-flight spec for experiment sidecars (optional [differential]
    block, debt #1071). `bth run` executes this before the main run: run the script once with
    `knob` set to `off` and once to `on`, then assert the results `expect` (differ/are identical).
    If the invariant doesn't fire, the main run is recorded with outcome='invalid_measurement'
    instead of executing -- proof the measurement can actually detect a real effect, not just
    that it produced *a* result.
    """

    knob: str = ""
    off: str = ""
    on: str = ""
    expect: str = "differs"  # "differs" or "identical"
    metric: str = ""  # optional: a result_schema key to compare numerically
    min_effect: float | None = None  # required when metric is set and numeric


@dataclass
class StatusBlock:
    """Staleness declaration for sidecars of any kind (optional [status] block).

    `bth run`'s pre-registration gate (prereg.gate_check) denies execution of a script
    whose sidecar declares `stale = true`, unless the caller passes `--allow-stale` —
    this is what stops an agent (or a human) from re-running a script known to have a
    wrong default or a superseded hypothesis, independent of whether the file has been
    physically archived (see bathos.artifact_archive).
    """

    stale: bool = False
    stale_reason: str = ""
    superseded_by: str = ""  # free text: run id / claim id / archive item id


#: Legal `disposition` values, by review kind. Validated structurally; an unknown value is a
#: validation error rather than a silent pass, because disposition is what obligation trigger
#: (4) keys on — a typo would make a citation permanently unable to be contradicted.
LITERATURE_DISPOSITIONS = frozenset({"supports", "contradicts", "scope-differs"})
IMPLEMENTATION_DISPOSITIONS = frozenset({"matches", "diverges", "not-applicable"})


@dataclass
class LiteratureReview:
    """One `[[review.literature]]` entry: a prior result, and what it bears on."""

    ref: str = ""  # DOI, arXiv id, or a bth run UUID
    claim: str = ""  # what the reference actually reports
    bears_on: str = ""  # hypothesis/confound id from the claim file (optional pre-claim, D2)
    disposition: str = ""  # one of LITERATURE_DISPOSITIONS
    checked: str = ""  # ISO date the author read it


@dataclass
class ImplementationReview:
    """One `[[review.implementation]]` entry: a reference implementation, read at a pinned commit."""

    source: str = ""  # path or URL
    commit: str = ""  # pinned revision — makes the attestation falsifiable in principle
    what_was_checked: str = ""
    bears_on: str = ""
    disposition: str = ""  # one of IMPLEMENTATION_DISPOSITIONS


@dataclass
class ReviewBlock:
    """The optional `[review]` block: targeted literature and implementation review.

    Tier is **derived, never declared** — the spec calls for tiers "mechanically graded", the
    same posture as parity's cap-lattice, so an author cannot assert a stronger tier than the
    content supports. See :func:`review_tier`.
    """

    literature: list[LiteratureReview] = field(default_factory=list)
    implementation: list[ImplementationReview] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.literature and not self.implementation


def literature_entry_reviewed(entry: LiteratureReview) -> bool:
    """Does one `[[review.literature]]` entry meet the C1 bar?

    Requires `ref` **and** `bears_on` **and** `disposition`. The spec's tier table states C1 as
    "`bears_on` + disposition" and does not mention `ref`; requiring it here is a deliberate
    tightening, because without it the lattice inverts. C0 is defined as "DOI + claim,
    unverified", so an entry carrying `bears_on` + `disposition` but *no citation* would grade
    C1 while failing C0's own content bar — and an uncited attestation ("I reviewed something
    unnamed that supports H1") is unfalsifiable in principle, which is the "review theater"
    failure mode §8 of the spec names explicitly.
    """
    return bool(entry.ref and entry.bears_on and entry.disposition)


def implementation_entry_reviewed(entry: ImplementationReview) -> bool:
    """Does one `[[review.implementation]]` entry meet the C1 bar?

    Requires `source` **and** `commit` **and** `what_was_checked` — a pinned read of a named
    thing. `source` is the same deliberate tightening as `ref` above: a `commit` with no source
    pins a revision of nothing.

    `bears_on` is deliberately NOT required. The spec grades an implementation read at a pinned
    commit as C1 on its own; binding it to a claim id is what the *coverage* gate additionally
    demands, which is a different question from how good the entry is. See :func:`covering_id`.
    """
    return bool(entry.source and entry.commit and entry.what_was_checked)


def covering_id(entry: LiteratureReview | ImplementationReview) -> str:
    """The claim id this entry substantively covers, or ``""`` if it covers nothing.

    This is the Review Coverage Gate's definition of "real enough to count", and it is
    deliberately expressed here — beside the tier standard — rather than in `claim.py`, so
    there is exactly one notion of a substantive review entry rather than two that can drift.

    Coverage is **C1 plus a binding**: an entry must both meet the tier bar and name the id it
    bears on. For literature `bears_on` is already part of C1; for implementation it is the
    extra requirement, since a pinned read that names no hypothesis covers nothing even though
    it is a perfectly good C1 entry.
    """
    if isinstance(entry, ImplementationReview):
        return entry.bears_on if implementation_entry_reviewed(entry) else ""
    return entry.bears_on if literature_entry_reviewed(entry) else ""


def review_tier(review: ReviewBlock | None) -> str:
    """Grade a review block: ``""`` (none) | ``C0`` cited | ``C1`` reviewed.

    C1 requires an entry that is actually checkable — see :func:`literature_entry_reviewed` and
    :func:`implementation_entry_reviewed` for the per-entry bar and why each field is required.
    Anything less is C0 — a citation, not a review.

    C2 (`parity`) is deliberately NOT derivable here. It is earned by the existing five-phase
    literature-parity audit, not by anything a sidecar can assert about itself.
    """
    if review is None or review.is_empty():
        return ""
    if any(literature_entry_reviewed(e) for e in review.literature):
        return "C1"
    if any(implementation_entry_reviewed(e) for e in review.implementation):
        return "C1"
    return "C0"


@dataclass
class Sidecar:
    kind: SidecarKind
    result_schema: dict[str, str]
    outcomes: dict[str, OutcomeSpec] = field(default_factory=dict)
    # experiment fields
    hypothesis: str = ""
    stage_name: str = "exploration"
    # True iff [experiment] carries a stage_name key at all. `stage_name` above is coerced for
    # back-compat with other modules; class resolution reads `stage_name_raw` so that a typo fails
    # closed instead of silently becoming "exploration".
    stage_name_declared: bool = False
    stage_name_raw: str | None = None
    question: str | None = None
    observe: ObserveBlock | None = None
    calibrate: CalibrateBlock | None = None
    novel: bool = False
    # benchmark fields
    baseline_ref: str = ""
    metric: str = ""
    regression_threshold: float = 0.0
    regression_threshold_basis: str = ""
    target: str = ""
    # validation fields
    property: str = ""
    reference: str = ""
    tolerance: str = ""
    # debug fields
    symptom: str = ""
    suspected_cause: str = ""
    verification: str = ""
    # agent mode field (all kinds)
    agent_mode: str = ""
    # claim-tier discriminability (experiment sidecars only). These carry the hypothesis /
    # confound ids the run discriminates or isolates, and are what the Union Gate maps a run
    # onto a claim clause with. Parsed here and propagated to Run in runner.py -- without both
    # halves the gate reads an always-NULL column and downgrades every confirmation campaign. (#3717)
    claim_discriminates: list[str] = field(default_factory=list)
    claim_isolates: list[str] = field(default_factory=list)
    # [experiment] keys parse_sidecar did not recognise (e.g. `stage` for `stage_name`);
    # surfaced as validation warnings, never errors (debt #2195).
    unknown_experiment_keys: list[str] = field(default_factory=list)
    # popper sequential test fields (experiment sidecars only)
    popper_null_pass_rate: float | None = None
    popper_alt_pass_rate: float | None = None
    popper_stopping_threshold: float | None = None
    popper_weights: dict[str, float] = field(default_factory=dict)
    # reproduction metadata (experiment sidecars only)
    reproduction: ReproductionBlock | None = None
    # controls metadata (experiment sidecars only)
    controls: ControlsBlock | None = None
    # instrument-sensitivity pre-flight (experiment sidecars only, debt #1071)
    differential: DifferentialBlock | None = None
    # targeted literature / implementation review (build-order step 2). Advisory at parse
    # time; the Review Coverage Gate at campaign conclude is what consumes it.
    review: ReviewBlock | None = None
    # staleness declaration (all sidecar kinds) -- consumed by prereg.gate_check
    status: StatusBlock | None = None
    # precondition and wall-time budget (experiment sidecars only; parsed here, enforced later)
    requires: RequiresBlock | None = None
    budget: BudgetBlock | None = None

    # `builtins.property`: the class body has a dataclass field named `property` (the validation
    # kind's `property = "..."`), which shadows the bare decorator name here.
    @builtins.property
    def gate_stage(self) -> str:
        """The stage the enforcement gates (requires_pass_stem, parity submit gate, linter) read.

        A declared but non-canonical `stage_name` (a typo such as "validaton") fails closed to
        "validation", the strictest enforced stage, so a typo can never make a gate advisory.
        This is the gate-side twin of `resolve_epistemic_class` (spec 261015 §3.1). The stored
        `stage_name` is left as coerced for back-compat with other readers.
        """
        if self.stage_name_declared and self.stage_name_raw not in CANONICAL_STAGES:
            return "validation"
        return self.stage_name or "exploration"


@dataclass(frozen=True)
class ObserveBlock:
    """Top-level `[observe]` block: what an exploratory run commits to producing (spec §3.2).

    This is a pre-registration of *what you will look at*, not a test. `sanity` entries are
    instrument checks over `result_schema` fields, evaluated by `check_sanity_expr`.
    """

    outputs: list[str] = field(default_factory=list)
    looking_for: str | None = None
    sanity: list[str] = field(default_factory=list)
    planned_tests: int | None = None


@dataclass(frozen=True)
class CalibrateBlock:
    """Top-level `[calibrate]` block: the quantity a calibration run measures (spec §3.5)."""

    quantity: str


ENFORCED_DIRS = {"experiments", "benchmarks", "validation"}

# Outcome labels bathos assigns itself out-of-band, never via evaluate_outcome's condition
# matching against a user-authored [outcomes.<label>] block. Note "error"/"unknown" are NOT
# in this set despite also being runner.py fallback values -- both are long-standing,
# user-declarable outcome labels (e.g. a sidecar's own catch-all residual branch is
# conventionally named [outcomes.unknown]; see tests/test_validate.py), so reserving them
# here would be a breaking change to existing sidecars. invalid_measurement (debt #1071) is
# new and bathos-exclusive: set only by the [differential] pre-flight short-circuit in
# runner.py when the instrument-sensitivity invariant fails to fire -- declaring it yourself
# would let a sidecar silently redefine what "the measurement is broken" means.
#
# "observed" (spec §3.2, epistemic class) is what exploratory and calibrating runs store as their
# outcome: a value was recorded, no verdict was reached. It is bathos-exclusive for the same reason.
RESERVED_OUTCOME_LABELS = {"invalid_measurement", "observed"}

# Labels that never open an outcome_failed obligation (spec 261015 §3.2, C5). "observed" is what
# exploratory and calibrating runs store; "invalid_measurement" is the instrument-failure label
# (debt #1071). Both are neutral: no failure, no e-value evidence.
NEUTRAL_OUTCOME_LABELS = frozenset({"", "unknown", "observed", "invalid_measurement"})

# Canonical set: the advisory vocabulary for stage_name values.
# These are best-practice values discovered from real stage_name usage.
# Not enforced in the schema — only advisory in CI lint.
CANONICAL_STAGES = {
    "exploration",
    "calibration",
    "validation",
    "ablation",
    "production",
}

# Epistemic class of a run (spec §3.1). Resolved once at run start by resolve_epistemic_class.
EPISTEMIC_CLASSES = ("exploratory", "calibrating", "confirmatory", "legacy")


def _str_list(value) -> list[str]:
    """Coerce a sidecar list-of-ids field to list[str], tolerating a bare string. (#3717)

    Returns [] for None/absent so an unset field is indistinguishable from an empty one --
    both mean "this run declares no clause coverage".
    """
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return []


# Every key parse_sidecar reads from the [experiment] table. Keep in sync with the
# `section.get(...)` calls in the experiment branch of parse_sidecar.
EXPERIMENT_KEYS: frozenset[str] = frozenset(
    {
        "hypothesis",
        "question",  # exploratory sidecars state a question instead of a hypothesis (spec 261015)
        "stage_name",
        "novel",
        "agent_mode",
        "claim_discriminates",
        "claim_isolates",
    }
)


def parse_sidecar(path: Path) -> Sidecar:
    try:
        data = tomllib.loads(path.read_text())
    except Exception as e:
        event("sidecar.parse_error", path=str(path), exc_type=type(e).__name__, exc_msg=str(e))
        raise SidecarError(f"Failed to parse {path}: {e}") from e

    sidecar = None
    if "experiment" in data:
        kind = SidecarKind.EXPERIMENT
        section = data["experiment"]
        outcomes = _parse_outcomes(data)

        # Parse stage_name with validation and coercion. The literal is kept in stage_name_raw
        # so epistemic-class resolution can fail closed on a typo (spec §3.1).
        stage_declared = "stage_name" in section
        stage_raw = str(section["stage_name"]) if stage_declared else None
        stage_name = section.get("stage_name", "exploration")
        if not (isinstance(stage_name, str) and stage_name in CANONICAL_STAGES):
            logger.warning(
                f"Invalid stage_name '{stage_name}' in {path}; must be one of {CANONICAL_STAGES}. "
                "Coercing to 'exploration'."
            )
            stage_name = "exploration"

        # Parse novel field
        novel = bool(section.get("novel", False))

        question = section.get("question")
        if question is not None:
            question = str(question)

        sidecar = Sidecar(
            kind=kind,
            hypothesis=section.get("hypothesis", ""),
            stage_name=stage_name,
            stage_name_declared=stage_declared,
            stage_name_raw=stage_raw,
            question=question,
            novel=novel,
            outcomes=outcomes,
            result_schema=data.get("result_schema", {}),
            agent_mode=section.get("agent_mode", ""),
            claim_discriminates=_str_list(section.get("claim_discriminates")),
            claim_isolates=_str_list(section.get("claim_isolates")),
            unknown_experiment_keys=sorted(set(section) - EXPERIMENT_KEYS),
        )
        popper = data.get("popper", {})
        if popper:
            sidecar.popper_null_pass_rate = popper.get("null_pass_rate")
            sidecar.popper_alt_pass_rate = popper.get("alt_pass_rate")
            sidecar.popper_stopping_threshold = popper.get("stopping_threshold")
            weights = popper.get("weights", {})
            if isinstance(weights, dict):
                sidecar.popper_weights = {k: float(v) for k, v in weights.items()}

        # Parse [reproduction] block (optional)
        repro_data = data.get("reproduction", {})
        if repro_data:
            sidecar.reproduction = ReproductionBlock(
                reproduces_paper=repro_data.get("reproduces_paper", ""),
                reproduces_run=repro_data.get("reproduces_run", ""),
                tolerance_pct=repro_data.get("tolerance_pct", None),
                requires_pass_stem=repro_data.get("requires_pass_stem", ""),
                requires_parity_stem=repro_data.get("requires_parity_stem", ""),
            )
            # Warn on unknown keys in [reproduction]
            for key in repro_data:
                if key not in {
                    "reproduces_paper",
                    "reproduces_run",
                    "tolerance_pct",
                    "requires_pass_stem",
                    "requires_parity_stem",
                }:
                    logger.warning(f"Unknown key in [reproduction]: {key!r}")

        # Parse [controls] block (optional)
        if "controls" in data:
            controls_data = data.get("controls", {})
            sidecar.controls = ControlsBlock(
                positive_outcome=controls_data.get("positive_outcome", []),
                negative_outcome=controls_data.get("negative_outcome", []),
            )
            # Warn on unknown keys in [controls]
            for key in controls_data:
                if key not in {"positive_outcome", "negative_outcome"}:
                    logger.warning(f"Unknown key in [controls]: {key!r}")

        # Parse [observe] block (optional, exploratory class, spec §3.2)
        if "observe" in data:
            obs = data.get("observe", {})
            planned = obs.get("planned_tests")
            if planned is not None and (isinstance(planned, bool) or not isinstance(planned, int)):
                raise SidecarError(f"[observe].planned_tests must be an integer in {path}")
            sidecar.observe = ObserveBlock(
                outputs=_str_list(obs.get("outputs")),
                looking_for=obs.get("looking_for"),
                sanity=_str_list(obs.get("sanity")),
                planned_tests=planned,
            )
            for key in obs:
                if key not in {"outputs", "looking_for", "sanity", "planned_tests"}:
                    logger.warning(f"Unknown key in [observe]: {key!r}")

        # Parse [calibrate] block (optional, calibrating class, spec §3.5)
        if "calibrate" in data:
            cal = data.get("calibrate", {})
            sidecar.calibrate = CalibrateBlock(quantity=str(cal.get("quantity", "")))
            for key in cal:
                if key != "quantity":
                    logger.warning(f"Unknown key in [calibrate]: {key!r}")

        # Parse [differential] block (optional, debt #1071)
        if "differential" in data:
            diff_data = data.get("differential", {})
            sidecar.differential = DifferentialBlock(
                knob=diff_data.get("knob", ""),
                off=str(diff_data.get("off", "")),
                on=str(diff_data.get("on", "")),
                expect=diff_data.get("expect", "differs"),
                metric=diff_data.get("metric", ""),
                min_effect=diff_data.get("min_effect", None),
            )
            # Warn on unknown keys in [differential]
            for key in diff_data:
                if key not in {"knob", "off", "on", "expect", "metric", "min_effect"}:
                    logger.warning(f"Unknown key in [differential]: {key!r}")
    elif "benchmark" in data:
        kind = SidecarKind.BENCHMARK
        section = data["benchmark"]
        outcomes = _parse_outcomes(data)
        sidecar = Sidecar(
            kind=kind,
            baseline_ref=section.get("baseline_ref", ""),
            metric=section.get("metric", ""),
            regression_threshold=section.get("regression_threshold", 0.0),
            regression_threshold_basis=section.get("regression_threshold_basis", ""),
            target=section.get("target", ""),
            outcomes=outcomes,
            result_schema=data.get("result_schema", {}),
            agent_mode=section.get("agent_mode", ""),
        )
    elif "validation" in data:
        kind = SidecarKind.VALIDATION
        section = data["validation"]
        outcomes = _parse_outcomes(data)
        sidecar = Sidecar(
            kind=kind,
            property=section.get("property", ""),
            reference=section.get("reference", ""),
            tolerance=section.get("tolerance", ""),
            outcomes=outcomes,
            result_schema=data.get("result_schema", {}),
            agent_mode=section.get("agent_mode", ""),
        )
    elif "debug" in data:
        kind = SidecarKind.DEBUG
        section = data["debug"]
        outcomes = _parse_outcomes(data)
        sidecar = Sidecar(
            kind=kind,
            symptom=section.get("symptom", ""),
            suspected_cause=section.get("suspected_cause", ""),
            verification=section.get("verification", ""),
            outcomes=outcomes,
            result_schema=data.get("result_schema", {}) or data.get("verdict_schema", {}),
            agent_mode=section.get("agent_mode", ""),
        )
    else:
        raise SidecarError(
            f"{path}: must have one of [experiment], [benchmark], [validation], [debug] sections"
        )

    if sidecar:
        sha256_val = hashlib.sha256(path.read_bytes()).hexdigest()
        outcome_labels = list(sidecar.outcomes.keys())
        event(
            "sidecar.parsed",
            path=str(path),
            sha256=sha256_val,
            outcomes=outcome_labels,
            kind=sidecar.kind.value,
        )

    # Parse [review] block (optional, build-order step 2). Applies to every sidecar kind,
    # not just experiments: a benchmark or validation script can equally rest on prior work.
    if "review" in data:
        review_data = data.get("review") or {}
        # A bare scalar/array `review = "..."` is legal TOML and satisfies the `in` test;
        # calling .get() on it would raise AttributeError out of parse_sidecar rather than
        # SidecarError. Treat a non-table as absent.
        if not isinstance(review_data, dict):
            logger.warning("[review] is not a table; ignoring")
            review_data = {}
        lit = [
            LiteratureReview(
                ref=str(e.get("ref", "")),
                claim=str(e.get("claim", "")),
                bears_on=str(e.get("bears_on", "")),
                disposition=str(e.get("disposition", "")),
                checked=str(e.get("checked", "")),
            )
            for e in review_data.get("literature", [])
            if isinstance(e, dict)
        ]
        impl = [
            ImplementationReview(
                source=str(e.get("source", "")),
                commit=str(e.get("commit", "")),
                what_was_checked=str(e.get("what_was_checked", "")),
                bears_on=str(e.get("bears_on", "")),
                disposition=str(e.get("disposition", "")),
            )
            for e in review_data.get("implementation", [])
            if isinstance(e, dict)
        ]
        sidecar.review = ReviewBlock(literature=lit, implementation=impl)
        for key in review_data:
            if key not in {"literature", "implementation"}:
                logger.warning(f"Unknown key in [review]: {key!r}")

    # Parse [status] block (optional, all sidecar kinds).
    if "status" in data:
        status_data = data.get("status") or {}
        if not isinstance(status_data, dict):
            logger.warning("[status] is not a table; ignoring")
            status_data = {}
        sidecar.status = StatusBlock(
            stale=bool(status_data.get("stale", False)),
            stale_reason=str(status_data.get("stale_reason", "")),
            superseded_by=str(status_data.get("superseded_by", "")),
        )
        for key in status_data:
            if key not in {"stale", "stale_reason", "superseded_by"}:
                logger.warning(f"Unknown key in [status]: {key!r}")

    # Parse [requires] and [budget] (optional, experiment sidecars only). Parsing only: the
    # gate that enforces them reads these fields later.
    if ("requires" in data or "budget" in data) and sidecar.kind != SidecarKind.EXPERIMENT:
        raise SidecarError(
            f"{path}: [requires] and [budget] are only allowed on [experiment] sidecars, "
            f"not on [{sidecar.kind.value}]"
        )
    if "requires" in data:
        sidecar.requires = _parse_requires(data["requires"], path)
    if "budget" in data:
        sidecar.budget = _parse_budget(data["budget"], path)

    return sidecar


def _check_sidecar_relative_path(
    path: Path, field_name: str, value: str, noun: str = "path"
) -> None:
    """Reject an absolute path or one with '..' in a sidecar-relative glob or path."""
    parts = Path(value).parts
    if Path(value).is_absolute() or ".." in parts:
        raise SidecarError(
            f"{path}: {field_name} must be a {noun} relative to the sidecar's directory and stay "
            f"inside it (no absolute path, no '..'), got {value!r}"
        )


def _check_dotted_key(path: Path, field_name: str, value: str) -> None:
    """Reject a dotted key path with an empty segment (e.g. 'a..b', '.a', 'a.')."""
    if any(seg == "" for seg in value.split(".")):
        raise SidecarError(
            f"{path}: {field_name} must be a dotted key path with no empty segments, got {value!r}"
        )


def _parse_requires(raw, path: Path) -> RequiresBlock:
    if not isinstance(raw, dict):
        raise SidecarError(f"{path}: [requires] must be a table")

    def opt_str(key: str) -> str | None:
        value = raw.get(key)
        if value is not None and not isinstance(value, str):
            raise SidecarError(
                f"{path}: [requires].{key} must be a string, got {type(value).__name__}"
            )
        return value

    def opt_nonblank(key: str) -> str | None:
        value = opt_str(key)
        if value is not None and not value.strip():
            raise SidecarError(f"{path}: [requires].{key} must be a non-empty string")
        return value

    workload = opt_str("workload")
    artifact = opt_nonblank("artifact")
    json_key = opt_nonblank("json_key")
    must_equal = opt_nonblank("must_equal")
    applies_if = opt_str("applies_if")

    hint = opt_nonblank("hint")

    if artifact is not None:
        _check_sidecar_relative_path(path, "[requires].artifact", artifact, noun="glob")
    if json_key is not None:
        _check_dotted_key(path, "[requires].json_key", json_key)

    if "reject_suffixes" in raw:
        suffixes = raw["reject_suffixes"]
        if not (isinstance(suffixes, list) and all(isinstance(s, str) for s in suffixes)):
            raise SidecarError(f"{path}: [requires].reject_suffixes must be a list of strings")
        if any(s == "" for s in suffixes):
            raise SidecarError(
                f"{path}: [requires].reject_suffixes must not contain an empty string "
                "(an empty list means no suffix rejection)"
            )
        reject_suffixes = list(suffixes)
    else:
        reject_suffixes = ["-dirty", "-unverified"]

    if json_key is not None and artifact is None:
        raise SidecarError(f"{path}: [requires].json_key is set but [requires].artifact is not")
    if must_equal is not None and artifact is None:
        raise SidecarError(f"{path}: [requires].must_equal is set but [requires].artifact is not")
    if must_equal is not None and json_key is None:
        raise SidecarError(f"{path}: [requires].must_equal is set but [requires].json_key is not")

    if applies_if is not None:
        logger.warning("[requires] applies_if is not evaluated yet; the requirement always applies")

    waiver = None
    if "waiver" in raw:
        waiver_raw = raw["waiver"]
        if not isinstance(waiver_raw, dict):
            raise SidecarError(f"{path}: [requires.waiver] must be a table")
        reason = waiver_raw.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise SidecarError(f"{path}: [requires.waiver].reason must be a non-empty string")
        for key in waiver_raw:
            if key != "reason":
                logger.warning(f"Unknown key in [requires.waiver]: {key!r}")
        waiver = RequiresWaiver(reason=reason)

    for key in raw:
        if key not in {
            "workload",
            "artifact",
            "json_key",
            "must_equal",
            "reject_suffixes",
            "applies_if",
            "waiver",
            "hint",
        }:
            logger.warning(f"Unknown key in [requires]: {key!r}")

    return RequiresBlock(
        workload=workload,
        artifact=artifact,
        json_key=json_key,
        must_equal=must_equal,
        reject_suffixes=reject_suffixes,
        applies_if=applies_if,
        waiver=waiver,
        hint=hint,
    )


def _budget_float(path: Path, key: str, value) -> float:
    """Convert a validated TOML number to float; an int too large for float is a SidecarError."""
    try:
        return float(value)
    except OverflowError as exc:
        raise SidecarError(f"{path}: [budget].{key} is too large to be a finite number") from exc


def _parse_budget(raw, path: Path) -> BudgetBlock:
    if not isinstance(raw, dict):
        raise SidecarError(f"{path}: [budget] must be a table")

    def opt_positive_float(key: str) -> float | None:
        value = raw.get(key)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise SidecarError(
                f"{path}: [budget].{key} must be a number, got {type(value).__name__}"
            )
        number = _budget_float(path, key, value)
        if not math.isfinite(number) or number <= 0:
            raise SidecarError(f"{path}: [budget].{key} must be finite and > 0, got {value!r}")
        return number

    est_wall_s = opt_positive_float("est_wall_s")
    per_unit_s = opt_positive_float("per_unit_s")

    n_units = raw.get("n_units")
    if n_units is not None:
        if isinstance(n_units, bool) or not isinstance(n_units, int):
            raise SidecarError(
                f"{path}: [budget].n_units must be an integer, got {type(n_units).__name__}"
            )
        if n_units <= 0:
            raise SidecarError(f"{path}: [budget].n_units must be > 0, got {n_units}")

    probe_record = raw.get("probe_record")
    if probe_record is not None and not isinstance(probe_record, str):
        raise SidecarError(
            f"{path}: [budget].probe_record must be a string, got {type(probe_record).__name__}"
        )
    if probe_record is not None:
        _check_sidecar_relative_path(path, "[budget].probe_record", probe_record)

    record_per_unit_key = raw.get("record_per_unit_key")
    if record_per_unit_key is not None:
        if not isinstance(record_per_unit_key, str) or not record_per_unit_key.strip():
            raise SidecarError(
                f"{path}: [budget].record_per_unit_key must be a non-empty string, "
                f"got {record_per_unit_key!r}"
            )
        _check_dotted_key(path, "[budget].record_per_unit_key", record_per_unit_key)
        if probe_record is None:
            raise SidecarError(
                f"{path}: [budget].record_per_unit_key is set but [budget].probe_record is not"
            )

    # Relative slack for the later budget check: the estimate may undershoot per_unit_s * n_units
    # by at most this fraction. Must be in (0, 1): at >= 1 the consistency floor is non-positive
    # and the check is vacuous. Bools, NaN and inf are rejected.
    tolerance_raw = raw.get("tolerance", 0.5)
    if isinstance(tolerance_raw, bool) or not isinstance(tolerance_raw, (int, float)):
        raise SidecarError(
            f"{path}: [budget].tolerance must be a number, got {type(tolerance_raw).__name__}"
        )
    tolerance = _budget_float(path, "tolerance", tolerance_raw)
    if not math.isfinite(tolerance) or not (0 < tolerance < 1):
        raise SidecarError(
            f"{path}: [budget].tolerance must be finite and in (0, 1), got {tolerance_raw!r}"
        )

    # The estimate must not contradict its own parts (beyond the stated tolerance).
    if est_wall_s is not None and per_unit_s is not None and n_units is not None:
        overflow_msg = (
            f"{path}: [budget].per_unit_s and [budget].n_units are too large to check "
            "est_wall_s against their product"
        )
        try:
            floor = per_unit_s * n_units * (1 - tolerance)
        except OverflowError as exc:
            raise SidecarError(overflow_msg) from exc
        if not math.isfinite(floor):
            raise SidecarError(overflow_msg)
        if est_wall_s < floor:
            raise SidecarError(
                f"{path}: [budget].est_wall_s={est_wall_s} contradicts per_unit_s * n_units = "
                f"{per_unit_s * n_units} at tolerance {tolerance}; est_wall_s must be >= {floor}"
            )

    for key in raw:
        if key not in {
            "est_wall_s",
            "per_unit_s",
            "n_units",
            "probe_record",
            "record_per_unit_key",
            "tolerance",
        }:
            logger.warning(f"Unknown key in [budget]: {key!r}")

    return BudgetBlock(
        est_wall_s=est_wall_s,
        per_unit_s=per_unit_s,
        n_units=n_units,
        probe_record=probe_record,
        record_per_unit_key=record_per_unit_key,
        tolerance=tolerance,
    )


def _parse_outcomes(data: dict) -> dict[str, OutcomeSpec]:
    outcomes_data = data.get("outcomes", {})
    return {
        label: OutcomeSpec(
            condition=spec.get("condition", ""),
            decision=spec.get("decision", ""),
            reasoning=spec.get("reasoning", ""),
            is_residual=bool(spec.get("is_residual", False)),
            adversarial_check=spec.get("adversarial_check"),
            source=spec.get("source", ""),
            multiple_comparisons_correction=spec.get("multiple_comparisons_correction", ""),
        )
        for label, spec in outcomes_data.items()
    }


def single_row_projection(row: dict) -> str:
    """Render a dict as a DuckDB single-row SELECT projection.

    Produces the `{cols}` of `SELECT (<condition>) FROM (SELECT {cols})` — the shape used
    to evaluate a scalar SQL fragment against one row of in-memory facts, with no table
    or catalog access.

    Promoted to module level so the corpus's `applies_when` fragments evaluate through the
    *same* mechanism as `[outcomes].condition` rather than a copy of it. Note the two share
    the mechanism only: the columns differ entirely, because outcome conditions see a
    script's `result` dict while `applies_when` sees facts about a script and its sidecar.

    Values are rendered as SQL literals. Nested structures (dict/list) cannot be addressed
    by a scalar condition anyway, so they are encoded as inert JSON strings — the row still
    evaluates instead of raising on Python-repr syntax.

    Non-finite floats (`inf`, `-inf`, `nan`) are special-cased: `repr()` renders them as the
    bare tokens `inf`/`-inf`/`nan`, which DuckDB parses as unqualified column references, not
    float literals — `SELECT inf::DOUBLE` fails with "Referenced column \"inf\" was not found"
    rather than producing a value at all (debt #1941). Quoting them as string literals cast to
    DOUBLE (`'inf'::DOUBLE`) is the form DuckDB accepts, and DuckDB then applies IEEE 754
    semantics for the finite comparisons that matter here (`inf < 5` is `false`, `-inf < 5` is
    `true`). NaN is rendered as SQL `NULL`, not `'nan'::DOUBLE`: DuckDB orders NaN above
    +inf (so `'nan'::DOUBLE > 0.9` is TRUE), which would let a broken measurement satisfy a
    `pass` condition. As NULL, every comparison on it is NULL, no condition matches, and the
    outcome is `unknown` unless a condition tests `x IS NULL` explicitly.
    """

    return ", ".join(f"{_sql_scalar_expr(v)} AS {k}" for k, v in row.items())


def _sql_scalar_expr(v: object) -> str:
    """Render a non-nested value as a DuckDB scalar literal (see single_row_projection)."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, float):
        if v != v:  # NaN: NULL so it can never satisfy a comparison (see single_row_projection)
            return "NULL::DOUBLE"
        if v == float("inf"):
            return "'inf'::DOUBLE"
        if v == float("-inf"):
            return "'-inf'::DOUBLE"
        return f"{v!r}::DOUBLE"
    if isinstance(v, int):
        return f"{v!r}"
    if isinstance(v, (dict, list)):
        v = json.dumps(v)
    escaped = str(v).replace("'", "''")
    return f"'{escaped}'"


def _sql_struct_expr(d: dict) -> str:
    """Render a dict as a DuckDB STRUCT literal, recursing into nested dicts.

    Lists inside a struct stay inert JSON text, as they do at top level. An empty dict has no
    DuckDB STRUCT form (`{}` is rejected), so it is NULL: any `a.b` on it is NULL, not a match.
    """
    if not d:
        return "NULL"
    fields = []
    for key, value in d.items():
        value_sql = _sql_struct_expr(value) if isinstance(value, dict) else _sql_scalar_expr(value)
        fields.append(f"{_sql_scalar_expr(str(key))}: {value_sql}")
    return "{" + ", ".join(fields) + "}"


def _struct_row_projection(row: dict) -> str:
    """Like single_row_projection, but each dict-valued column is a STRUCT, so `a.b` addresses it."""
    return ", ".join(
        f"{_sql_struct_expr(v) if isinstance(v, dict) else _sql_scalar_expr(v)} AS {k}"
        for k, v in row.items()
    )


def _condition_value(condition: str, result: dict) -> object:
    """Evaluate one outcome SQL fragment against `result` and return its value (None if no row).

    Top-level values bind exactly as they always have (single_row_projection). Only when that
    raises AND the result holds a nested dict is the fragment retried with dicts bound as STRUCTs,
    so `a.b` addresses a nested field. A fragment that evaluates the legacy way is never re-bound,
    so every case that already worked keeps its meaning. A failure raises; callers must report it.
    """
    try:
        rows = duckdb.execute(
            f"SELECT ({condition}) FROM (SELECT {single_row_projection(result)})"
        ).fetchall()
    except Exception:
        if not any(isinstance(v, dict) for v in result.values()):
            raise
        rows = duckdb.execute(
            f"SELECT ({condition}) FROM (SELECT {_struct_row_projection(result)})"
        ).fetchall()
    return rows[0][0] if rows else None


def derive_pass_labels(sidecar: Sidecar) -> set[str]:
    """Outcome labels in the *pass* direction.

    Outcome labels have no canonical set — unlike `stage_name`, which is constrained to
    CANONICAL_STAGES, the author names each `[outcomes.<label>]` branch freely — so this is
    derived from the sidecar's own declarations rather than matched against a fixed vocabulary.

    Excluded: residual catch-all branches, the bookkeeping labels `marginal` / `error` /
    `unknown`, and every bathos-reserved label (`observed`, `invalid_measurement`), none of which
    is a declared scientific outcome.
    """
    return {
        label
        for label, spec in sidecar.outcomes.items()
        if not spec.is_residual
        and label not in ("marginal", "error", "unknown")
        and label not in RESERVED_OUTCOME_LABELS
    }


def is_failure_outcome(sidecar: Sidecar | None, outcome_label: str) -> bool:
    """Did this run land outside the pass direction? (obligation trigger 1, spec §5.1)

    **Not** a literal match on ``"fail"``: `unstable`, `rejected` and `no-go` are all legal
    names for the same thing and a string match would silently miss every one of them. The
    spec defines the trigger against the same `pass_labels` set `compute_evalue` derives,
    which is why that derivation now lives in :func:`derive_pass_labels`.

    ``""`` and ``"unknown"`` are **not** failures — they mean no outcome was evaluated at all,
    which is a different thing from an evaluated non-pass, and an obligation to explain a
    result that was never computed would be noise.

    ``"observed"`` is treated the same way: an exploratory or calibrating run records a value,
    not a verdict, so it never opens a `outcome_failed` obligation whatever its tentative label
    (spec §3.2, C5).

    `marginal`, `error`, and residual catch-all branches **are** failures here: none of them is
    a pass. That is the spec's rule applied literally, and it is the widest of the four
    triggers — see the calibration note in `obligations.TRIGGERS`.
    """
    if sidecar is None or outcome_label in NEUTRAL_OUTCOME_LABELS:
        return False
    return outcome_label not in derive_pass_labels(sidecar)


def compute_evalue(
    sidecar: Sidecar,
    outcome_label: str,
    pass_labels: set[str] | None = None,
) -> float:
    """Compute the likelihood-ratio e-value for a single run outcome.

    Returns 1.0 (neutral) if the sidecar has no [popper] block.
    Error, unknown and observed outcomes always return 1.0 (non-overridable).
    """
    if sidecar.popper_null_pass_rate is None:
        return 1.0

    # Same neutral set as before plus invalid_measurement; "" is deliberately not here (it
    # always fell through to the fail-direction formula, and still does).
    if outcome_label in ("error", "unknown", "observed", "invalid_measurement"):
        return 1.0

    # Explicit weight override takes priority
    if outcome_label in sidecar.popper_weights:
        return float(sidecar.popper_weights[outcome_label])

    null = sidecar.popper_null_pass_rate
    alt = sidecar.popper_alt_pass_rate

    # Marginal: hard default 1.0
    if outcome_label == "marginal":
        return 1.0

    # Determine pass-direction labels if not provided
    if pass_labels is None:
        pass_labels = derive_pass_labels(sidecar)

    evalue = alt / null if outcome_label in pass_labels else (1.0 - alt) / (1.0 - null)

    assert evalue > 0, (
        f"compute_evalue produced non-positive value {evalue} for outcome '{outcome_label}'"
    )
    return evalue


def resolve_epistemic_class(sidecar: Sidecar, *, stem_has_prior_runs: bool) -> str | None:
    """Resolve a run's epistemic class from its sidecar (spec §3.1 table, in order).

    Returns one of :data:`EPISTEMIC_CLASSES`, or ``None`` meaning "invalid": an exploration or
    calibration stage without its block, on a stem with no prior runs. The validator turns
    ``None`` into an error. Shape gating: a declared stage only takes the new class when the
    sidecar has the new shape, so the 66 legacy sidecars that declare `exploration`/`calibration`
    with real `[outcomes]` keep today's rules.
    """
    if sidecar.kind != SidecarKind.EXPERIMENT:
        return "legacy"
    if not sidecar.stage_name_declared:
        return "legacy"
    raw = sidecar.stage_name_raw
    if raw not in CANONICAL_STAGES:
        # A typo ("validaton") fails closed: it can never loosen the rules.
        return "confirmatory"
    if raw == "exploration":
        if sidecar.observe is not None:
            return "exploratory"
    elif raw == "calibration":
        if sidecar.calibrate is not None:
            return "calibrating"
    else:
        # validation / ablation / production
        return "confirmatory"
    # exploration or calibration, but its block is missing.
    return "legacy" if stem_has_prior_runs else None


# --- source grammar (spec §3.2 "source grammar", closed) ----------------------------------------


@dataclass(frozen=True)
class SourceRef:
    """A parsed `source = "..."` value. Only the fields its `kind` uses are set."""

    kind: str  # "lit" | "run" | "sesoi" | "provisional" | "card"
    ref: str = ""  # lit:<ref>, card:<id>
    field: str = ""  # run:<id>:<field>
    value: float | None = None  # sesoi:<value>:<why>
    why: str = ""  # sesoi / provisional


def parse_source(s: str) -> SourceRef | None:
    """Parse a `source` value against the closed grammar. Returns None when malformed.

    Accepted: ``lit:<ref>``, ``run:<id>:<field>``, ``sesoi:<finite float>:<why>``,
    ``provisional:<why>``, ``card:<id>``. Every part must be non-empty. Any ``${`` is rejected
    (an unexpanded template placeholder is never a source).
    """
    if not isinstance(s, str) or "${" in s:
        return None
    kind, sep, rest = s.partition(":")
    if not sep or not rest.strip():
        return None
    if kind == "lit":
        return SourceRef(kind="lit", ref=rest)
    if kind == "card":
        return SourceRef(kind="card", ref=rest)
    if kind == "provisional":
        return SourceRef(kind="provisional", why=rest)
    if kind == "run":
        parts = rest.split(":")
        if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
            return None
        return SourceRef(kind="run", ref=parts[0], field=parts[1])
    if kind == "sesoi":
        value_s, sep2, why = rest.partition(":")
        if not sep2 or not why.strip():
            return None
        try:
            value = float(value_s)
        except ValueError:
            return None
        if not math.isfinite(value):
            return None
        return SourceRef(kind="sesoi", value=value, why=why)
    return None


# --- sanity expressions (spec §3.2 validator rules) ---------------------------------------------

# Allowlist of serialized expression node classes (spec 261015 §3.2). Every class not named here
# is rejected, so a new DuckDB node type (CASE, SUBQUERY, BETWEEN, WINDOW, ...) cannot slip in.
# CAST is admitted only as the bare TRUE/FALSE literal form (see _sanity_bool_cast_ok). FUNCTION
# is admitted only over result fields with no literal anywhere beneath it (see _sanity_walk), so
# `isfinite(x)` is an instrument check while `x > 1+1+1` or `sum(x) FILTER (WHERE x > 5)` is not.
_SANITY_NODE_CLASSES = {
    "COLUMN_REF",
    "CONSTANT",
    "COMPARISON",
    "CONJUNCTION",
    "OPERATOR",
    "CAST",
    "FUNCTION",
}
# OPERATOR types admitted. `x IN (...)` is an OPERATOR of type COMPARE_IN, so it is rejected here.
_SANITY_OPERATOR_TYPES = {"OPERATOR_NOT", "OPERATOR_IS_NULL", "OPERATOR_IS_NOT_NULL"}
_SANITY_NUMERIC_TYPES = {
    "TINYINT",
    "SMALLINT",
    "INTEGER",
    "BIGINT",
    "HUGEINT",
    "UTINYINT",
    "USMALLINT",
    "UINTEGER",
    "UBIGINT",
    "FLOAT",
    "DOUBLE",
    "DECIMAL",
}


def _sanity_children(node: dict) -> list[dict]:
    """Every dict under `node`, whatever the key, including dicts inside lists.

    Deliberately generic. A fixed key list (left/right/child/children) let constants hide under
    other keys (FUNCTION `filter`, `order_bys`, CASE branches, window parts) and never be
    inspected. Walking every value means nothing in the serialized tree is skipped.
    """
    kids: list[dict] = []
    for value in node.values():
        if isinstance(value, dict):
            kids.append(value)
        elif isinstance(value, list):
            kids.extend(v for v in value if isinstance(v, dict))
    return kids


def _sanity_descendants(node: dict) -> list[dict]:
    out: list[dict] = []
    stack = _sanity_children(node)
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(_sanity_children(n))
    return out


def _sanity_constant_ok(node: dict) -> bool:
    """A literal is allowed only if it is 0, 1, NULL or a boolean (spec §3.2)."""
    literal = node.get("literal")
    if isinstance(literal, dict):
        # DuckDB 2.x serializes a constant as {"literal": {"kind", "text"}}; 1.x used "value"
        # (handled below). The text is parsed exactly, so `1e9`, `-5`, `0.5` stay rejected.
        kind = literal.get("kind")
        if kind in ("NULL_LITERAL", "BOOLEAN"):
            return True
        if kind in ("INTEGER", "NUMERIC"):
            from decimal import Decimal, InvalidOperation

            try:
                return Decimal(str(literal.get("text", ""))) in (0, 1)
            except InvalidOperation:
                return False
        return False
    value = node.get("value") or {}
    typ = value.get("type") or {}
    type_id = typ.get("id")
    if value.get("is_null") or type_id == "NULL":
        return True
    if type_id == "BOOLEAN":
        return True
    if type_id in _SANITY_NUMERIC_TYPES:
        raw = value.get("value")
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            return False
        if type_id == "DECIMAL":
            raw = raw / (10 ** int((typ.get("type_info") or {}).get("scale", 0)))
        return raw in (0, 1)
    return False


def _sanity_bool_cast_ok(node: dict) -> bool:
    """`TRUE` / `FALSE` parse as CAST(CONSTANT VARCHAR 't'|'f' AS BOOLEAN); accept only that."""
    child = node.get("child") or {}
    value = (child.get("value") or {}) if child.get("class") == "CONSTANT" else {}
    return (
        (node.get("cast_type") or {}).get("id") == "BOOLEAN"
        and (value.get("type") or {}).get("id") == "VARCHAR"
        and value.get("value") in ("t", "f")
    )


def _sanity_walk(node: dict, fields: set[str], errors: list[str], expr: str) -> None:
    cls = node.get("class")
    if cls is None:
        # A payload dict (a type, a value, a type_info), not an expression node. Carries no
        # semantics of its own, but may hold expression nodes, so keep descending.
        for kid in _sanity_children(node):
            _sanity_walk(kid, fields, errors, expr)
        return
    if cls not in _SANITY_NODE_CLASSES:
        errors.append(f"sanity {expr!r}: unsupported expression ({cls})")
        return
    if cls == "COLUMN_REF":
        names = node.get("column_names") or []
        if len(names) != 1:
            errors.append(f"sanity {expr!r}: qualified reference {'.'.join(names)!r} not allowed")
        elif names[0] not in fields:
            errors.append(f"sanity {expr!r}: unknown field {names[0]!r}")
    elif cls == "CONSTANT":
        if not _sanity_constant_ok(node):
            errors.append(f"sanity {expr!r}: literal not allowed (only 0, 1, NULL, true, false)")
    elif cls == "CAST":
        # TRUE / FALSE parse as CAST('t'|'f' AS BOOLEAN). That exact shape is the literal and its
        # child is consumed here; any other cast (CAST(x AS INT), ...) is rejected outright.
        if not _sanity_bool_cast_ok(node):
            errors.append(f"sanity {expr!r}: cast not allowed")
        return
    elif cls == "OPERATOR":
        op_type = node.get("type")
        if op_type not in _SANITY_OPERATOR_TYPES:
            errors.append(
                f"sanity {expr!r}: operator not allowed ({op_type}); only NOT, IS NULL, IS NOT NULL"
            )
            return
    elif cls == "FUNCTION":
        # Spec §3.2: a function (arithmetic included) may not carry any literal, even 0 or 1,
        # because that is how a cutoff hides (`x > 1+1+1`). The walk is over the whole subtree,
        # so a literal under `filter`/`order_bys` counts too. It must also read a result field.
        descendants = _sanity_descendants(node)
        if any(d.get("class") in ("CONSTANT", "CAST") for d in descendants):
            errors.append(
                f"sanity {expr!r}: literal inside a function or arithmetic expression "
                "(e.g. 'x > 1+1+1') is not allowed"
            )
            return
        if not any(d.get("class") == "COLUMN_REF" for d in descendants):
            errors.append(f"sanity {expr!r}: function call must reference a result field")
            return
    # COMPARISON, CONJUNCTION and an admitted FUNCTION are allowed as written; their operands are
    # checked below.
    for kid in _sanity_children(node):
        _sanity_walk(kid, fields, errors, expr)


def check_sanity_expr(expr: str, schema_fields: set[str]) -> list[str]:
    """Check one `[observe].sanity` entry. Returns error messages; empty means acceptable.

    The expression is parsed by DuckDB (not regex), so the check sees what will actually run.
    Only boolean combinations of `schema_fields` columns and the literals 0, 1, NULL and
    booleans pass. A literal inside arithmetic is rejected, so `x > 1+1+1` cannot hide a cutoff.
    """
    if not isinstance(expr, str) or not expr.strip():
        return ["sanity expression is empty"]
    try:
        row = (
            duckdb.connect()
            .execute("SELECT json_serialize_sql(?)", ["SELECT (" + expr + ")"])
            .fetchone()
        )
    except duckdb.Error as e:
        return [f"sanity {expr!r}: does not parse ({e})"]
    if row is None:
        return [f"sanity {expr!r}: does not parse (no serialization returned)"]
    parsed = json.loads(row[0])
    if parsed.get("error"):
        return [f"sanity {expr!r}: does not parse ({parsed.get('error_message', 'parse error')})"]
    statements = parsed.get("statements", [])
    if len(statements) != 1:
        return [f"sanity {expr!r}: must be a single expression"]
    node = statements[0].get("node", {})
    # Fail closed on anything but a bare `SELECT (<expr>)`: no FROM, WHERE, set ops or extra items.
    if (
        node.get("type") != "SELECT_NODE"
        or len(node.get("select_list", [])) != 1
        or (node.get("from_table") or {}).get("type") != "EMPTY"
        or node.get("where_clause") is not None
    ):
        return [f"sanity {expr!r}: must be a single expression"]
    errors: list[str] = []
    _sanity_walk(node["select_list"][0], schema_fields, errors, expr)
    return errors


def find_sidecar(script_path: Path) -> Path | None:
    """Return the .bth.toml adjacent to script_path, or None if absent."""
    candidate = script_path.parent / f"{script_path.stem}.bth.toml"
    return candidate if candidate.exists() else None


def is_in_enforced_dir(script_path: Path) -> bool:
    """Return True if script is inside an enforced directory.

    Enforced = a directory name in the built-in ``ENFORCED_DIRS`` (checked first, so a broken project config never
    affects those), or one the project adds in ``.bth.toml``::

        [enforcement]
        dirs = ["scripts/method", "release"]

    An entry without ``/`` is a directory NAME matched at any depth below the project root; an entry with ``/`` is a
    project-root-relative PREFIX (``scripts/method`` does not match ``scripts/methodology``). Additive only. Raises
    :class:`bathos.config.EnforcementConfigError` if the project's ``[enforcement]`` block is invalid -- a typo must not
    silently leave the gate off (protamer debt #1960 / bathos #2210: the gate is fail-open for any directory not named).
    """
    if any(part in ENFORCED_DIRS for part in script_path.parts):
        return True
    from bathos.config import load_enforcement

    names, prefixes, root = load_enforcement(script_path)
    if root is None or not (names or prefixes):
        return False
    # `root` is an ancestor of the script by construction (the config is found by walking up from its path).
    rel = Path(os.path.abspath(script_path)).relative_to(root)
    directories = rel.parts[:-1]  # the file name itself is never a directory match
    if any(part in names for part in directories):
        return True
    return any(directories[: len(p)] == p for p in prefixes)


def evaluate_adversarial_check(sidecar: Sidecar, outcome_label: str, result: dict) -> str | None:
    """Evaluate the selected branch's `adversarial_check`. Returns "passed" | "fired" | None.

    **Polarity — the check is a stricter bar, so it FIRES when it evaluates FALSE.**

    `adversarial_check` is an *additional* condition the outcome must also clear, not the
    negation of `condition`. The ADR (`260526_adversarial-check-policy.md`) defines it as a
    condition that "would flip the outcome if the hypothesis were wrong", and the spec's own
    example is a strengthened conjunct — `condition = "temp_std < 5"` paired with
    `adversarial_check = "temp_std < 5 AND n_steps >= 10000 AND dt_fs <= 0.5"`. A wrong
    hypothesis fails the stricter bar; that failure is the check *catching* something, which
    is what "fired" means here.

    The problem it exists to solve (ADR Context) is an agent pre-registering "a weak
    confirmatory test that never genuinely stresses the claim". The remedy for a weak test is
    a stronger one, so a check that merely negates `condition` is a contradiction that would
    fire on every single run of that branch — `linter.check_adversarial_checks` flags that
    shape via the distinct-column heuristic.

    Returns None (not "passed") when there is nothing to evaluate — no branch selected, no
    check declared, or no result data. Absent must stay distinguishable from cleared.

    A malformed check returns None rather than raising: it is an advisory signal, and a bad
    SQL fragment must not fail a run whose outcome already evaluated successfully. It is not
    silent, though: use evaluate_adversarial_check_detailed to get the error text.
    """
    status, _error = evaluate_adversarial_check_detailed(sidecar, outcome_label, result)
    return status


def evaluate_adversarial_check_detailed(
    sidecar: Sidecar, outcome_label: str, result: dict
) -> tuple[str | None, str]:
    """Same as evaluate_adversarial_check, but also returns why evaluation failed.

    Returns (status, error): status is "passed" | "fired" | None, and error is the SQL error text
    when a declared check could not be evaluated (status None), or "" otherwise. The error lets
    the runner record the failure in outcome_error_reason instead of dropping it silently.
    """
    if not outcome_label or outcome_label in ("error", "unknown") or not result:
        return None, ""
    spec = sidecar.outcomes.get(outcome_label)
    check = getattr(spec, "adversarial_check", None) if spec else None
    if not check:
        return None, ""

    try:
        value = _condition_value(check, result)
    except Exception as e:
        return None, str(e)
    if value is None:
        return None, ""
    return ("passed" if value else "fired"), ""


def evaluate_outcome(sidecar: Sidecar, result: dict) -> str:
    """Evaluate DuckDB SQL fragments against result dict; return matching label or 'unknown'.

    Nested dict fields are addressable with dotted struct access (`a.b`); see _condition_value.

    Raises SidecarError if a SQL condition is malformed.
    """
    if not sidecar.outcomes or not result:
        return "unknown"

    for label, spec in sidecar.outcomes.items():
        try:
            value = _condition_value(spec.condition, result)
            if value:
                return label
        except Exception as e:
            raise SidecarError(f"Failed to evaluate outcome '{label}': {e}") from e
    return "unknown"
