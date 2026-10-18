from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_SCRIPT_TEMPLATE = """\
#!/usr/bin/env python3
\"\"\"{name}: one-line description.\"\"\"
# Standard library only: bathos does not install this script's dependencies, so anything
# imported here must already be present in a fresh project.
from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path

log = logging.getLogger("{name}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="{name}: one-line description.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="mark this as a dry run (the template does the same work either way)",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    # TODO: implement experiment logic here
    if args.dry_run:
        log.info("dry run: no experiment logic is implemented yet")

    # Results keys match [result_schema] in the sidecar. None (JSON null) is a deliberate
    # placeholder: an unedited run emits nulls, so the outcome conditions compare against NULL
    # and no branch matches. Replace every None with a real measurement before trusting a run.
    results = {{
        "metric": None,  # TODO: float, the measured value
        "n_samples": None,  # TODO: int, the number of samples behind it
    }}
    payload = json.dumps(results)

    # bathos sets BTH_RESULTS_PATH to a per-run temp file; it is the primary result channel
    # for outcome evaluation.
    results_path = os.environ.get("BTH_RESULTS_PATH")
    if results_path:
        Path(results_path).write_text(payload)

    # --- Output artifacts ---
    # bathos sets BTH_OUTPUT_DIR to a per-run directory (outputs/<run_id_short>/)
    # before launching this script. Any files written there are auto-registered.
    # results.json is the file the exploratory sidecar declares under [observe].outputs.
    out_dir = os.environ.get("BTH_OUTPUT_DIR")
    if out_dir:
        Path(out_dir, "results.json").write_text(payload)

    # For files outside BTH_OUTPUT_DIR, pass --out to `bth run` explicitly:
    #   bth run scripts/experiments/{name}.py --out path/to/file.json
    # Query registered outputs with:
    #   bth outputs list <run_id>
    #   bth outputs summary

    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""

_SCAFFOLD_MODES = ("explore", "confirm")
_WORKLOADS = ("jax",)

# `--workload jax` appends the launch-gate tables (spec 261016 B3). [requires] is live; [budget] is
# commented because placeholder numbers fail validation by design, so the author fills it from the
# `xtrax profile` report first. `{stem}` is replaced with the experiment name; the TOML hint is a
# basic string, so its inner quotes are escaped.
_JAX_GATE_TEMPLATE = r'''
# JAX launch gate (spec 261016 B3). [requires] is checked by `bth preflight` and `bth submit`:
# the probe record must exist, carry git_sha = HEAD, and be committed. Paths resolve from the
# project root. Unmet at validation/ablation/production blocks once [gate] requires_enforce_after
# is active; before that, and in exploration, it only warns.
[requires]
workload = "jax"
artifact = "outputs/profiling/{stem}/*.json"
json_key = "git_sha"
must_equal = "HEAD"
hint = "Profile one unit at production shapes: xtrax profile --fn <module>:<unit> --shapes \"<name>=(<dims>)f32\" --extent-axis <name> --n-units <N> --out outputs/profiling/{stem}, commit, then re-run bth preflight. See the xtrax-probing skill."

# [budget] -- fill from the `xtrax profile` report (`est. wall per unit s`, `est. total wall s`), then uncomment.
# [budget]
# per_unit_s = 0.0
# n_units = 0
# est_wall_s = 0.0
# probe_record = "outputs/profiling/{stem}/<record>.json"
# record_per_unit_key = "metrics.total_step_seconds"
'''

# Exploratory scaffold (the default). It validates clean as written: no uncommented [outcomes]
# (optional for exploration, spec §3.2), and [observe].outputs names the file the script
# template writes into BTH_OUTPUT_DIR.
_EXPLORE_SIDECAR_TEMPLATE = """\
[experiment]
stage_name = "exploration"
question = "TODO: state what you want to see"
novel = false

[observe]
# What you commit to producing and looking at BEFORE you look. This is not a test.
outputs = ["results.json"]
looking_for = "TODO: what you expect to notice, if anything (free text, optional)"
# Instrument checks: DuckDB booleans over [result_schema] fields. They guard the measurement,
# not the hypothesis. A false one marks the run invalid_measurement.
sanity = []
# planned_tests = 2   # optional: how many distinct non-residual branch conditions you plan

[result_schema]
metric = "float"
n_samples = "int"

# Exploration runs under heavy epistemic and operational uncertainty: you don't yet know what the
# right quantity, scale or cutoff is, and the pipeline itself may still be wrong. Treat these labels
# as sorting hints for what to look at next, not results. Expect to change them, and change them
# freely; a changed exploratory outcome is not a deviation. Uncomment only if you want triage labels.
# Every non-residual branch needs a `source`: lit:<ref> | run:<id>:<field> | sesoi:<value>:<why> |
# provisional:<why> | card:<id>.
#
# [outcomes.pass]
# condition = "metric < 5.0"
# decision = "TODO: what to look at next if this holds"
# reasoning = "TODO: why this label sorts runs usefully"
# is_residual = false
# source = "provisional:<why this cutoff>"
#
# [outcomes.fail]
# condition = "metric >= 5.0"
# decision = "TODO: what to look at next otherwise"
# reasoning = "TODO: why the remainder is the other bin"
# is_residual = true
"""

# Confirmatory scaffold (`--mode confirm`): today's template. Thresholds and sources are
# ${PLACEHOLDER} tokens on purpose. validate_sidecar reports each unfilled source as a
# "fill these in" item, and the scaffold is only complete once they are replaced.
_CONFIRM_SIDECAR_TEMPLATE = """\
[experiment]
hypothesis = "TODO: state your hypothesis"
stage_name = "validation"
novel = false

# ${THRESHOLD} and ${SOURCE} are placeholders: replace every one before running.
# A source is lit:<ref> | run:<id>:<field> | sesoi:<value>:<why> | provisional:<why> | card:<id>.

[outcomes.pass]
condition = "metric < ${THRESHOLD}"
decision = "TODO: next step if pass"
reasoning = "metric below threshold indicates convergence"
is_residual = false
source = "${SOURCE}"
# A STRICTER bar the pass must ALSO clear — not the negation of `condition`.
# It fires (opens a post-mortem obligation) when it evaluates false on a run that
# otherwise passed, i.e. the pass was too cheap to believe.
# Reference a variable that `condition` does NOT use: tightening the same one is weak,
# and negating `condition` outright is a contradiction that fires on every pass.
# TODO: replace n_samples with your own control variable, and justify the floor.
adversarial_check = "metric < ${THRESHOLD} AND n_samples >= 100"

[outcomes.marginal]
condition = "metric >= ${THRESHOLD} AND metric < 2 * ${THRESHOLD}"
decision = "TODO: next step if marginal"
reasoning = "metric in marginal range — tune parameters"
is_residual = false
source = "${SOURCE}"

[outcomes.fail]
condition = "metric >= 2 * ${THRESHOLD}"
decision = "TODO: next step if fail"
reasoning = "metric above threshold indicates failure"
is_residual = true

[result_schema]
metric = "float"
n_samples = "int"

# Uncomment and fill in if this experiment reproduces a prior result:
# [reproduction]
# reproduces_paper = ""   # DOI or citation
# reproduces_run = ""     # UUID of the run being reproduced
# tolerance_pct = 5.0     # acceptable deviation (%)
# requires_pass_stem = "" # script stem that must have outcome='pass' first

# Uncomment if using control arms (outcome labels prefixed ctrl_ declared in [outcomes]):
# [controls]
# positive_outcome = ["ctrl_pass"]
# negative_outcome = ["ctrl_fail"]

# Uncomment if a null/fail result must be distinguishable from a broken measurement pipeline:
# bth run executes this pre-flight before the main run, and refuses to run (recording
# outcome='invalid_measurement' instead) if the instrument doesn't discriminate as expected.
# [differential]
# knob = ""          # TODO: the input parameter this experiment varies
# off = ""           # value representing "effect absent"
# on = ""            # value representing "effect present"
# expect = "differs" # or "identical"
# metric = "metric"  # must be a key in [result_schema]
# min_effect = 0.05  # required when metric is numeric
"""


@dataclass
class ScaffoldResult:
    script: Path
    sidecar: Path
    name_warning: str = ""
    mode: str = "explore"
    #: Unfilled `${...}` placeholders in the sidecar (confirm mode). Expected, not a defect:
    #: the author fills each one in before the sidecar can validate.
    needs_filling: list[str] = field(default_factory=list)
    #: Epistemic class resolved from the written sidecar (spec §3.1).
    epistemic_class: str | None = None


def _is_placeholder_error(message: str, field_name: str) -> bool:
    return "${" in message or "${" in field_name


def scaffold_experiment(
    name: str,
    project_root: Path,
    force: bool = False,
    mode: str = "explore",
    workload: str | None = None,
) -> ScaffoldResult:
    """Write `scripts/experiments/<name>.py` and its `<name>.bth.toml` sidecar.

    ``mode="explore"`` (default) writes an exploratory sidecar: a ``question``, an ``[observe]``
    block, and no uncommented ``[outcomes]``. It validates clean as written.

    ``mode="confirm"`` writes a confirmatory sidecar with ``${THRESHOLD}``/``${SOURCE}``
    placeholders. Their validation errors are reported in ``needs_filling``, not raised.
    Any other validation error means the scaffold itself is broken, and the scaffold is
    removed again and ``ValueError`` is raised.

    ``workload="jax"`` appends the launch-gate tables (``[requires]`` live, ``[budget]`` commented).
    ``workload=None`` writes exactly the same sidecar as before the option existed.
    """
    if mode not in _SCAFFOLD_MODES:
        raise ValueError(f"unknown mode {mode!r}: expected one of {', '.join(_SCAFFOLD_MODES)}")
    if workload is not None and workload not in _WORKLOADS:
        raise ValueError(f"unknown workload {workload!r}: expected one of {', '.join(_WORKLOADS)}")

    warning = ""
    if not _NAME_RE.match(name):
        warning = f"Name {name!r} should be lowercase with underscores (verb_noun style)."

    experiments_dir = project_root / "scripts" / "experiments"
    experiments_dir.mkdir(parents=True, exist_ok=True)

    script = experiments_dir / f"{name}.py"
    sidecar = experiments_dir / f"{name}.bth.toml"

    if not force:
        existing = [p for p in (script, sidecar) if p.exists()]
        if existing:
            paths = ", ".join(str(p) for p in existing)
            raise FileExistsError(f"Already exists (use --force to overwrite): {paths}")

    sidecar_template = _EXPLORE_SIDECAR_TEMPLATE if mode == "explore" else _CONFIRM_SIDECAR_TEMPLATE

    script.write_text(_SCRIPT_TEMPLATE.format(name=name))
    script.chmod(0o755)
    workload_block = _JAX_GATE_TEMPLATE.replace("{stem}", name) if workload == "jax" else ""
    sidecar.write_text(sidecar_template + workload_block + _REVIEW_STUB + _corpus_guidance())

    from bathos.sidecar import parse_sidecar, resolve_epistemic_class
    from bathos.validate import validate_sidecar

    try:
        sidecar_obj = parse_sidecar(sidecar)
        result = validate_sidecar(sidecar_obj, sidecar_path=sidecar)
    except Exception as exc:
        script.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        raise ValueError(f"scaffold for {name!r} does not parse: {exc}") from exc

    needs_filling = [
        f"{e.field}: {e.message}" for e in result.errors if _is_placeholder_error(e.message, e.field)
    ]
    broken = [
        f"{e.field}: {e.message}"
        for e in result.errors
        if not _is_placeholder_error(e.message, e.field)
    ]
    if broken:
        script.unlink(missing_ok=True)
        sidecar.unlink(missing_ok=True)
        raise ValueError(f"scaffold for {name!r} is broken: " + "; ".join(broken))

    return ScaffoldResult(
        script=script,
        sidecar=sidecar,
        name_warning=warning,
        mode=mode,
        needs_filling=needs_filling,
        epistemic_class=resolve_epistemic_class(sidecar_obj, stem_has_prior_runs=False),
    )


def _corpus_guidance() -> str:
    """Append the shipped rule cards as commented guidance in the scaffolded sidecar.

    This is where the corpus earns its keep: at authoring time, in the file the researcher is
    already editing, rather than in documentation they would have to go find.

    Every card is listed rather than only those whose `applies_when` fires, because a fresh
    scaffold has no outcomes, no stage and no history — every context flag is at its default,
    so `applies_when` evaluation here would say almost nothing. `bth ref applicable <script>`
    is the targeted view once the sidecar has real content.

    Failure is silent by design: a scaffold must not break because the corpus is unreadable.
    """
    try:
        from bathos.corpus import load_corpus

        load = load_corpus()
    except Exception:
        return ""
    if not load.cards:
        return ""

    lines = [
        "",
        "# ─────────────────────────────────────────────────────────────────────────",
        "# Rule cards shipped with bathos. Cite one by id in a threshold's `source`,",
        "# or in a post-mortem's `violated_cards`. Full text: `bth ref show <id>`.",
        "# Targeted to this script once it has content: `bth ref applicable <script>`.",
        "#",
    ]
    for card in load.cards:
        # Collapse any control characters: an embedded newline would split this into a
        # second physical line with no leading "#", corrupting the sidecar TOML silently.
        title = " ".join(str(card.title).split())
        lines.append(f"#   {card.id:<12} [{card.severity:<7}] {title}")
    lines.append("# ─────────────────────────────────────────────────────────────────────────")
    return "\n".join(lines) + "\n"


#: §6 requires the scaffold to pre-fill [review] stubs. Commented rather than live so a fresh
#: sidecar still validates: an uncommented [[review.literature]] with empty ref/claim would be
#: a validation ERROR on a file the author has not touched yet.
_REVIEW_STUB = """
# ── Targeted review (optional; see `bth ref show DSGN-002`) ──────────────────
# Uncomment and fill in what prior work this experiment rests on. `bears_on`
# names a hypothesis or confound id from the campaign's claim file; it is only
# required once a claim exists and the campaign is confirmatory (decision D2).
#
# [[review.literature]]
# ref = "10.1234/example"          # DOI, arXiv id, or a bth run UUID
# claim = "what that reference actually reports"
# bears_on = "H1"
# disposition = "supports"         # supports | contradicts | scope-differs
# checked = "YYYY-MM-DD"
#
# [[review.implementation]]
# source = "https://github.com/org/repo"
# commit = "abc1234"               # pin it — an unpinned read grades C0, not C1
# what_was_checked = "which specific behaviour you read, and where"
# bears_on = "C1"
# disposition = "matches"          # matches | diverges | not-applicable
"""
