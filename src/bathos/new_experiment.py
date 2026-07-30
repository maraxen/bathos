from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

_SCRIPT_TEMPLATE = """\
#!/usr/bin/env python3
\"\"\"{name}: one-line description.\"\"\"
from __future__ import annotations

import json
import typer

app = typer.Typer()


@app.command()
def main(
    dry_run: bool = typer.Option(False, "--dry-run"),
):
    # TODO: implement experiment logic here

    # Write results as JSON to stdout for bathos outcome evaluation
    results = {{
        # "metric_name": value,
    }}
    print(json.dumps(results))

    # --- Output artifacts ---
    # bathos sets BTH_OUTPUT_DIR to a per-run directory (outputs/<run_id_short>/)
    # before launching this script. Any files written there are auto-registered.
    #
    #   import os; out_dir = os.environ["BTH_OUTPUT_DIR"]
    #   Path(out_dir, "results.json").write_text(json.dumps(results))
    #
    # For files outside BTH_OUTPUT_DIR, pass --out to `bth run` explicitly:
    #   bth run scripts/experiments/{name}.py --out path/to/file.json
    # Query registered outputs with:
    #   bth outputs list <run_id>
    #   bth outputs summary


if __name__ == "__main__":
    app()
"""

_SIDECAR_TEMPLATE = """\
[experiment]
hypothesis = "TODO: state your hypothesis"
stage_name = "exploration"
novel = false

[outcomes.pass]
condition = "metric < 5.0"
decision = "TODO: next step if pass"
reasoning = "metric below threshold indicates convergence"
is_residual = false
adversarial_check = "metric >= 5.0"

[outcomes.marginal]
condition = "metric >= 5.0 AND metric < 10.0"
decision = "TODO: next step if marginal"
reasoning = "metric in marginal range — tune parameters"
is_residual = false

[outcomes.fail]
condition = "metric >= 10.0"
decision = "TODO: next step if fail"
reasoning = "metric above threshold indicates failure"
is_residual = true

[result_schema]
metric = "float"

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


def scaffold_experiment(name: str, project_root: Path, force: bool = False) -> ScaffoldResult:
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

    script.write_text(_SCRIPT_TEMPLATE.format(name=name))
    script.chmod(0o755)
    sidecar.write_text(_SIDECAR_TEMPLATE + _REVIEW_STUB + _corpus_guidance())

    return ScaffoldResult(script=script, sidecar=sidecar, name_warning=warning)


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
