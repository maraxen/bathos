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
    sidecar.write_text(_SIDECAR_TEMPLATE)

    return ScaffoldResult(script=script, sidecar=sidecar, name_warning=warning)
