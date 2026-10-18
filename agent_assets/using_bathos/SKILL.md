---
name: using-bathos
description: Experiment tracking with bathos — run tracking, sidecar pre-registration, controls discipline, catalog queries. Use when writing or editing a .bth.toml sidecar or its [outcomes], running `bth run` or `bth submit`, citing or verifying a run (outcome, catalog, `bth sql`, `bth find`, `bth compact`), or deciding whether a metric can be trusted (synthetic positive/negative controls).
when_to_use: 'Triggers: bth run, sidecar, .bth.toml, [outcomes], pre-registration, bth compact, bth find, catalog, controls, stage_name, bth init, bth check, bathos, bth, experiment, run, outcomes, compact, init, lint.'
---
# using-bathos

bathos (`bth`) is a standalone experiment tracking CLI for researchers running 10+ projects across local and SLURM cluster environments. It tracks script runs, pre-registers hypotheses via sidecars, syncs results to/from clusters, and provides rich query and reporting interfaces.

This skill covers the daily-driver workflow: installation, run tracking, sidecar pre-registration, controls discipline, and catalog queries. For cluster submission and sync, campaigns and claim-tier rigor, literature-parity validation, or MCP tool integration, see the sibling skills listed under **Related** below.

## Core Concepts

**Run** — A single script execution tracked in the catalog. Fields: `id`, `project_slug`, `command`, `argv`, `git_hash`, `git_branch`, `timestamp`, `duration_s`, `exit_code`, `status`, `output_paths`, `tags`, `outcome`, `sidecar_sha256`, `campaign_id`, `slurm_job_id`, `slurm_array_task_id`, postmortem metadata. Dirty runs (with uncommitted changes) get a real snapshot commit via a throwaway index rather than a hash of a possibly-nonexistent tree; remote/cluster dirty runs bundle provenance home via `outputs/provenance/`.

**Sidecar** — A `.bth.toml` file alongside a script that pre-registers hypothesis, expected outcome conditions (DuckDB SQL), and result schema. Enforced by default at `bth run` time (use `--no-sidecar` to bypass, logs `BYPASSED`).

**Outcome** — Evaluated at run-end by matching result JSON against DuckDB SQL conditions in the sidecar. Values: `pass`, `marginal`, `fail`, `error`, plus any custom label you declare. One outcome must be marked `is_residual = true`. A non-zero exit code does **not** automatically force `outcome='error'`: if the script still emitted a result payload containing every declared `[result_schema]` field, that payload is evaluated as usual and the honest pre-registered label (e.g. `fail`) is recorded — a script that signals its own fail/marginal branch by exiting non-zero after writing results is not the same thing as an infrastructure crash. The one exception is a payload that evaluates to `pass`: that is **never** recorded as `pass` despite a non-zero exit — it is recorded as `error` with a reason noting both facts, since a crashed process is never a pass regardless of what its payload claims. A non-zero exit with no result, a partial result, or no sidecar at all keeps the plain `error` / `exit_code=N` behavior. **Freshness (debt #1977 CRITICAL):** on the non-zero-exit path only, a result payload from a fallback source (the `<script_stem>.bth-results.json` file adjacent to the script, or a single registered `--out` JSON) is trusted only if its mtime is at or after the moment this run's subprocess was spawned — neither fallback file is removed before a run, so either can otherwise be a stale leftover from an earlier invocation adopted by a crash that wrote nothing new. The primary `$BTH_RESULTS_PATH` channel is always fresh (its path is unique per run id) and is never subject to this check; the zero-exit path is also unaffected and still trusts all three sources regardless of age. **Unknown-on-crash (debt #1977 HIGH):** if a fresh, complete payload evaluates to `unknown` (no `[outcomes]` condition matched) after a non-zero exit, it is recorded as `outcome='error'` with reason `exit_code=N; no outcome condition matched`, not `outcome='unknown'` with an empty reason. Any other honest non-pass label recorded on a non-zero exit always carries `outcome_error_reason = "exit_code=N"`, so the crash fact stays queryable.

**Campaign** — A named group of related runs. Accessible via `bth campaign` subcommands; queries via `campaign_id` field. See **bathos-campaigns** for campaign, claim-tier, postmortem, and lineage workflows.

**Catalog** — Tiered Parquet + DuckDB store at `~/.bth/catalog/` (or `.bth.toml` `[project].catalog_dir`). Cool tier (per-run fragments) → compacted to warm tier (DuckDB database) → optionally archived to cold tier (partitioned Parquet).

**@bth.experiment** — An alternative Python-embedding entry point to provenance capture. Decorate a function with `@bth.experiment` (`bathos.decorators`) to get the same provenance capture as `bth run`, without a CLI invocation. Silently no-ops with a warning if `BTH_PROJECT_SLUG` is unset.

## Discipline: pre-register, run as `bth`, verify by the record

*(Moved here from the global `~/.claude/CLAUDE.md` bullet "STAGE EVERY EXPERIMENT WITH BATHOS" on 2026-10-16, at the user's direction; the global file keeps only a three-line core: pre-register before the run, use `bth` never `uv run bth`, verify by the run record never by exit code. The text below keeps the global bullet's wording, failure modes, measurements and dates. Merged with `~/.claude/rules/BATHOS.md`.)*

### The sidecar is a PRE-registration, not a receipt

Any run that will produce a finding (a number, a breakdown, a statistic, a pass/fail, anything that could end up in a doc, a deck, a ledger claim or a decision) gets a tracked script plus a `.bth.toml` sidecar **written and committed before the run**, stating the hypothesis and the `[outcomes]` pass/fail criteria. Then `bth run` (local) or `bth submit` (cluster).

Writing the sidecar after seeing the numbers is the failure this rule exists to stop: outcome criteria chosen post-hoc always pass. If an exploratory run has already happened, say so explicitly and re-run under a sidecar before citing a single number from it.

Promote findings to a ledger **claim** (`bth claim`) rather than citing a dated doc; dated docs are an append-only log and go stale (see **bathos-campaigns**).

### Verify the measurement pipeline on synthetic ground truth first — positive AND negative control

When introducing or first using a metric/eval function, run a sanity-check on synthetic ground truth before trusting it for any research conclusion. A positive control that can only pass is not a check. Pair it with a **negative** control that must fail, or the instrument is untested.

- Measured 2026-09-25: a six-site ligation fidelity model scored a lone junction at 1.000 — trivially, by construction — and only a deliberately cross-reactive pair at 0.728 proved it fires at all.
- Example: if a function claims `small T → sharp softmax`, test it — feed one-hot logits, call with `temperature=0.01`, and assert `max(output) > 0.9`. If the assertion fails, the temperature convention is inverted. These checks take 30 seconds. **They would have caught the SP-0.6 `update_seq` temperature-inversion bug** — `softmax(logits * 0.01)` silently uniformized posteriors instead of sharpening them, costing 2+ hours across four runs before it was traced.
- **Heuristic:** if you have falsified 3+ research hypotheses in a row and each falsification yields numerically suspicious results (everything uniform, metrics agree by coincidence), stop and verify the measurement pipeline against synthetic invariants. The probability that "every hypothesis was wrong" drops sharply with each falsification — the measurement is probably broken.
- For the post-Union-Gate version (instrument-sensitivity positive controls, synthetic-recovery gates) see **bathos-rigor-gates**.

### Run it as `bth`, NOT `uv run bth`

The project-pinned bathos (0.4.1) **silently skips sidecar enforcement** under `uv run bth` — it exits 0 and tracks nothing, which is indistinguishable from a clean tracked run.

### Pick the invocation that gets the PROJECT venv

`bth run --script-path foo.py` (as of the 2026-09 builds) prepends bathos's OWN interpreter (its isolated uv-tool Python), so any script importing project-local packages dies with ModuleNotFoundError. Use the wrapped form instead:

```bash
bth run --project-slug <slug> -- uv run --no-sync python3 scripts/foo.py --args
```

`--no-sync` because bare `uv run` silently re-syncs and reverts hand-installed wheels.

**Current-code check, 2026-10-16 (read from the source, not from a memory note).** Two older claims are superseded, one in each direction:

1. *The wrapped form does bind the script and its adjacent sidecar.* `_find_script_path` (`src/bathos/runner.py:122`) walks `uv run --no-sync python3 scripts/foo.py` and returns `scripts/foo.py`: `run`/`python`/`python3` are passthrough tokens (`runner.py:189`), and a valueless flag such as `--no-sync` advances by exactly one token (`runner.py:197-202`, from commit a5ea72a4, 2026-09-25; test `tests/test_runner.py:1862`). The result feeds `resolve_sidecar` / `parse_sidecar` (`runner.py:847-850`). Behind an `env`/`timeout`/`nice`/`taskset` prefix the script is also found (`runner.py:130-132`, commit 4d7f7823, 2026-10-14, debt #2725); if no script can be bound but a sidecar-governed `.py` is named in argv, `bth run` now prints a stderr `WARNING ... SKIPPED` (`runner.py:816-830`, via `_unbound_enforced_scripts`, `runner.py:211`). A praxia measurement of 260926 recorded the wrapped form attaching no sidecar (`script_path` was `"uv"`, `outcome=unknown`) while `--script-path` evaluated the pre-registered outcome. That is **not reproducible against current source**; the likeliest explanation is an install that predated a5ea72a4, but the installed build was not recorded, so it is unproven. Keep the posture it taught: after any wrapped run, confirm the record's `outcome` is not `unknown` before citing it.
2. *`--script-path` no longer always uses bathos's own interpreter.* `_bare_script_argv` (`src/bathos/mcp.py:1788`, commit 58813250, 2026-10-14, debt #2236) runs a bare `.py` as `uv run --no-sync python <script>` when the working directory has a `.venv` and `uv` is on PATH (`mcp.py:1801`); only otherwise does it fall back to bathos's interpreter, and it then says so on stderr (`mcp.py:1810`). A `pyproject.toml` without a `.venv` does not count. So the ModuleNotFoundError above applies to builds before 2026-10-14, or to a cwd with no `.venv`.

### Verify a run by its RECORD, never by exit code or console text

Resolve the returned id and check its `outcome` actually evaluated:

```bash
bth sql "SELECT id, status, outcome, exit_code, command FROM runs WHERE id LIKE '<prefix>%'"
```

Spot-check that its claimed paths resolve. A script emitting confident, schema-valid, entirely fabricated output exits 0.

- **In log mode a finished run is visible at once; only a legacy catalog needs `bth compact`.** A catalog is in log mode once `~/.bth/catalog/cutover.json` exists (the real catalog since 2026-10-16). There, `bth run` appends its events to the project log and, as its last step, folds them into the derived index `index.db` (skipped on SLURM or with `BTH_NO_INGEST=1`). `sql`/`show`/`find`/`ls` read that index, so no compact is needed. Measured 2026-10-16 (B4, run `27fffb85-ba69-4b21-8208-b51e50b446f9`, pass): 20 of 20 just-finished runs visible to all six readers, p95 CLI read 0.96 s, run-end ingest median 1.8 s. If a run still looks missing in log mode, `bth compact` runs the same ingest on demand and reports `ingest_ran`/`quarantined`; it never creates `bathos.db`, and `--force-rebuild` is refused (delete `index.db` and run `bth compact` to rebuild it from the logs). Other catalogs (a remote or an old install without `cutover.json`) are still tiered: `bth run` writes the cool tier, the warm `bathos.db` only updates on `bth compact`, and a run finished seconds ago is invisible until then.
  - Measured 2026-09-25 (legacy catalog): a run was unretrievable by `bth show` for minutes, then appeared on its own; a later one stayed invisible past 20 s and `bth compact` made it visible. **Nothing was ever lost.** Do not file a data-loss bug off a probe taken right after a write.
- **`bth find` takes FILTERS, not a search term** (`--project`, `--since`, `--status`, `--tags`, `--output-file`). `bth find <script-name>` matches nothing and returns `{"runs": [], "count": 0}` — that is malformed usage, not a missing run. Likewise `bth ls` is not newest-first unless you pass `--since`.

### `bth --version` cannot tell you whether an install is current

It prints pyproject's `version` field, which is not bumped between releases — 0.13.0a4 reports identically at the tag and 9 commits past it, on every machine. bathos **is on PyPI** (0.13.0a1–a5 as of 2026-10-01), but dev installs come from a local dir or a git rev, where the same version string covers many commits. To know what an install actually has, grep the installed code. Same trap as aminx `__version__`.

The wrapped-command fix is the guard `script_path.endswith(".py")` in `mcp.py` (`src/bathos/mcp.py:1887`). Locate the installed `mcp.py` through the tool venv's own import system, not by guessing a `site-packages` path:

```bash
F=$("$(uv tool dir)/bathos/bin/python" -I -c 'import importlib.util as u; print(u.find_spec("bathos.mcp").origin)')
grep -nF 'endswith(".py")' "$F"      # a hit = the guard is installed
```

Verified 2026-10-16 on a machine where `bth` is an editable install of `~/projects/bathos` (`$F` resolves to `~/projects/bathos/src/bathos/mcp.py`, hit at line 1887). The older form, `grep -q 'endswith(".py")' $(ls -d ~/.local/share/uv/tools/bathos/lib/python*/site-packages/bathos)/mcp.py`, works only for a PyPI or wheel install: for an editable install `site-packages/bathos/` holds only `agent_assets/` and there is no `mcp.py` there to grep. Note that this guard is the 2026-09 `.py` check; the later `_bare_script_argv` behaviour above (commit 58813250) is a separate, newer marker (`grep -nF '_bare_script_argv' "$F"`).

### Inline Python is for inspection, not findings

Ephemeral inline `uv run python3 <<PY` is for inspection that produces **no finding** only — `ls`/`wc`/schema peeks. Anything that yields a number someone might cite is a tracked script plus sidecar. The full boundary is in `~/.claude/rules/ephemeral_scripts.md`.

## Installation

```bash
uv tool install bathos
bth --version
```

`bth --version` does not prove the install is current — see "`bth --version` cannot tell you whether an install is current" above.

## Project Initialization

```bash
bth init --slug myproject --slurm-partition mit_normal
```

Creates `.bth.toml` and initializes catalog. Defines project slug (required for all other commands), cluster remote, and SLURM defaults.

## Run Tracking

### Basic Tracking

```bash
bth run -- uv run --no-sync python scripts/experiments/train_model.py --epochs 10 --out outputs/result.json
```

(Run it as `bth`, never `uv run bth`, and prefer `uv run --no-sync`: see **Discipline** above.) Runs script, captures git state, and records run in catalog with auto-generated UUID.

### With Metadata

```bash
bth run \
  --output-paths outputs/result.json \
  --tags experiment:baseline \
  --tags date:2026-06-01 \
  --campaign-id my-campaign-id \
  -- uv run python scripts/train.py
```

**Options:**
- `--output-paths PATH` — Register output file path (can repeat)
- `--tags TAG` — Add tag (can repeat)
- `--campaign-id ID` — Associate with campaign
- `--agent-mode collaborative|autonomous` — Mark collaborative (human-in-loop) or autonomous runs
- `--derived-from RUN_ID` — Link lineage to parent run
- `--no-sidecar` — Bypass sidecar enforcement (logs `BYPASSED`)
- `--allow-stale` — Run anyway despite the sidecar's `[status] stale = true` flag; without it, `bth run` refuses to execute a script marked stale
- `--component-id ID` / `--component-sidecar-sha256 SHA` — Bind this run to an xtrax composition-node/StageBundle slot (cross-tool bridge; niche)

Exit code is script's exit code.

## Output Path Convention

Output JSON files registered with `bth run --output-paths` must **never** be in ephemeral directories (`/tmp`, `/var/tmp`, or `$TMPDIR`). Bathos catalogs these paths as durable references; a temp-dir path will be lost on reboot or system cleanup, making the catalog entry unreproducible.

Non-JSON files (PNG, SVG, PDF figures) are equally valid `--output-paths` targets; bathos stores them in `output_paths` as opaque file references alongside result JSON. Repeat the flag for each path.

```bash
# ✓ Correct — persistent project-relative path
bth run --output-paths outputs/run_abc.json -- uv run python scripts/experiments/train.py

# ✗ Wrong — /tmp is ephemeral; catalog entry becomes invalid after reboot
bth run --output-paths /tmp/result.json -- uv run python scripts/experiments/train.py
```

Smoke-test validation runs (pre-flight checks before a real run) should be executed **directly**, not via `bth run`, so they are not tracked:

```bash
# ✓ Correct — smoke test run directly, not cataloged
uv run python scripts/experiments/train.py --smoke --out /tmp/test.json

# Then the real tracked run uses a persistent path
bth run --output-paths outputs/run_abc.json -- uv run python scripts/experiments/train.py
```

## Sidecar Pre-Registration

Every script in `scripts/experiments/` and `scripts/benchmarks/` should have a sidecar `.bth.toml` declaring hypothesis and expected outcomes.

**Hard enforcement is path-gated, and fail-open for any directory not named.** `bth run` refuses to launch a script with a missing, invalid, hash-drifted or stale sidecar ONLY when the script is under a built-in enforced directory (`experiments`, `benchmarks`, `validation`). A script anywhere else (e.g. `scripts/method/`, `scripts/release/`) still gets its outcome evaluated when a sidecar binds, but a missing or drifted sidecar does NOT stop the run: it records `sidecar_mode=""` and exits 0. A project that keeps tracked experiment scripts elsewhere adds its directories in the project's `.bth.toml` (additive to the built-ins):

```toml
[enforcement]
dirs = ["scripts/method", "scripts/release"]
```

An entry without `/` is a directory NAME matched at any depth below the project root (`release` also gates `vendor/release/` and `data/release/`; prefer a prefix when that matters); an entry with `/` is a project-root-relative PREFIX (`scripts/method` does not match `scripts/methodology`). Matching is case-sensitive, like the built-ins. An invalid `[enforcement]` block (non-list `dirs`, empty/absolute/`..` entries, unknown keys, or TOML that does not parse but mentions `enforcement`) refuses the run with `invalid_param` instead of being ignored, so a typo cannot silently leave the gate off. A `.bth.toml` that does not parse and never mentions `enforcement` keeps bathos's long-standing tolerance: it is logged and no setting is applied. `--no-sidecar` remains the explicit, recorded bypass (`sidecar_mode='bypassed'`).

Limits worth knowing. (1) The project is the NEAREST `.bth.toml` above the script, as everywhere else in bathos: a nested `.bth.toml` without `[enforcement]` hides an outer one's dirs, and a worktree that has no `.bth.toml` of its own (untracked or ignored there) inherits its parent checkout's, whose project-root-relative prefixes will not match paths under the worktree directory. Track `.bth.toml` so every worktree has its own. (2) The built-in directories are checked first and never read the project config, so a broken config does not affect `scripts/experiments/` scripts but does affect a configured dir such as `scripts/method/`. (3) The runner refuses before writing any run record, exactly as it does for other gate failures.

### Experiment Sidecar Format

```toml
[experiment]
hypothesis = "NVT with dt=0.5fs maintains ±5K temperature stability over 50ps"

[outcomes.pass]
condition = "temp_std < 5"
is_residual = false

[outcomes.marginal]
condition = "temp_std >= 5 AND temp_std < 10"
is_residual = false

[outcomes.fail]
condition = "temp_std >= 10"
is_residual = true

[result_schema]
temp_mean = "float"
temp_std = "float"
n_steps = "int"
```

**Key Rules:**
- Outcome `condition` fields are DuckDB SQL fragments evaluated on result JSON
- Exactly one outcome must have `is_residual = true`
- `result_schema` declares all columns referenced by outcome conditions
- No Python-style chained comparisons: use `AND` instead of `0.4 <= x < 0.7`

**How outcome evaluation actually finds your result JSON** — `bth run` reads it from, in order:
1. `$BTH_RESULTS_PATH` — an env var `bth run` sets for the subprocess. **This is the
   reliable mechanism; write your result dict here.**
2. `<script_stem>.bth-results.json` adjacent to the script (fallback).
3. A single registered `--output-paths` JSON path (fallback) — only used when *exactly one*
   `--output-paths` path ends in `.json`; with zero or multiple candidates this is skipped
   rather than guessing, and outcome stays `unknown`.

Writing only to `--output-paths` and never to `$BTH_RESULTS_PATH` is a common mistake that
silently leaves `outcome='unknown'` for every run, even when the run completes
successfully and the sidecar conditions would otherwise evaluate to `pass`. Prefer
writing to both:

```python
import json
import os

result = {"temp_mean": 300.5, "temp_std": 2.3, "n_steps": 1000}

with open(args.out, "w") as f:
    json.dump(result, f)

results_path = os.environ.get("BTH_RESULTS_PATH")
if results_path:
    with open(results_path, "w") as f:
        json.dump(result, f)
```

### Benchmark Sidecar Format

```toml
[benchmark]
baseline_ref = "run_abc123"
metric = "ns_per_day"
regression_threshold = 0.05
target = "> 50 ns/day on pi_so3"

[outcomes.pass]
condition = "ns_per_day >= 50"
decision = "accept benchmark"
reasoning = "meets the target"
is_residual = false

[outcomes.fail]
condition = "ns_per_day < 50"
decision = "investigate regression"
reasoning = "below the target"
is_residual = true

[result_schema]
ns_per_day = "float"
system = "str"
n_atoms = "int"
```

### Scaffold a Template

```bash
bth new-experiment my_experiment
bth new-experiment my_experiment --force  # overwrite existing files
```

Creates `scripts/experiments/my_experiment.py` and `scripts/experiments/my_experiment.bth.toml` skeleton. `name` is a positional argument — `bth new-experiment --name my_experiment` fails with "no such option". (MCP mirror: `new_experiment` tool.)

### Validate Sidecar

```bash
bth validate-sidecar scripts/experiments/train.bth.toml
bth validate-sidecar scripts/experiments/train.bth.toml --campaign-id my-campaign-id  # also cross-checks claim_discriminates/claim_isolates against the campaign's registered claim
```

Checks TOML syntax, schema completeness, DuckDB condition validity, and residual outcome presence. Prints `field: message` for each error and exits 1 if invalid; prints `✓ <path> is valid` on success.

Note: `bth check` (see **Query and Inspect** below) is a *different* command — it checks already-recorded runs for git-drift against current HEAD, it does not validate sidecars.

## Controls Discipline (v0.11.0)

### Stage Classification

Every experiment sidecar can declare a `stage_name` field (default: `"exploration"`) to classify the maturity and intent of the run. Canonical values are advisory — non-canonical values are logged as a warning and coerced to `"exploration"` at parse time.

**Canonical stages:**
- `exploration` — hypothesis generation, parameter sensitivity, proof-of-concept (default)
- `calibration` — tuning hyperparameters before validation; outcome refinement
- `validation` — testing hypothesis with controlled parameters; reproducibility required
- `ablation` — isolating contributions of components
- `production` — final tested run ready for publication

```toml
[experiment]
hypothesis = "..."
stage_name = "validation"  # optional, defaults to "exploration"
```

Non-canonical values (e.g., `"pilot"`, `"final"`) trigger a warning and are coerced to `"exploration"`:
```
WARNING: Invalid stage_name 'pilot' in scripts/experiments/train.bth.toml; must be one of {'exploration', 'calibration', 'validation', 'ablation', 'production'}. Coercing to 'exploration'.
```

### Novel Flag

Mark a run as novel (a new claim, not reproduction of prior work) with the `novel` flag:

```toml
[experiment]
hypothesis = "..."
novel = true
```

Setting `novel = true` satisfies Tier-1 lint requirements for validation/production experiments (see Lint Checks below).

### Reproduction Block

Declare reproduction metadata (optional `[reproduction]` block) to link your experiment to prior work or control runs:

```toml
[reproduction]
reproduces_paper = "doi:10.1234/example"    # DOI or citation string (or "")
reproduces_run = "run_abc123"               # Bathos run UUID (or "")
tolerance_pct = 5.0                         # Allowed deviation in outcome metrics (optional)
requires_pass_stem = "baseline_train"       # Script stem that must pass first (optional)
```

**Fields:**
- `reproduces_paper` — DOI or full citation if this experiment reproduces published work. Leave empty if not reproducing a paper.
- `reproduces_run` — Bathos run UUID if this experiment reproduces a prior bathos run. Leave empty if not reproducing a bathos run.
- `tolerance_pct` — Allowed percentage deviation in outcome metrics when comparing to the reproduced reference. Optional; omit if not doing quantitative reproduction.
- `requires_pass_stem` — Script stem (e.g., `"baseline_train"`) that must have at least one passing run before this script can be submitted. Enforced at `bth submit` time. Optional; omit if no prerequisite.

### Controls Block

Declare which outcome labels count as "control arm success" and "control arm failure" (optional `[controls]` block):

```toml
[controls]
positive_outcome = ["pass"]
negative_outcome = ["fail", "marginal"]
```

**Fields:**
- `positive_outcome` — List of outcome labels that indicate the control arm succeeded (e.g., `["pass"]`).
- `negative_outcome` — List of outcome labels that indicate the control arm failed (e.g., `["fail", "marginal"]`).

The control block is declarative; it feeds Sprint Audit Signal 9 (see below).

### bth submit Gate

Two separate gate mechanisms protect experimental discipline (see **bathos-cluster** for `bth submit` itself):

**1. Run-time gate** (`gate_check()` in `bth run`) — happens automatically at run start, validates sidecar presence, hash, and first-of-kind properties. Does NOT touch `stage_name`, `novel`, or `[reproduction]` fields.

**2. Submit-time gate** (at `bth submit`) — keyed ONLY on `[reproduction].requires_pass_stem`. Operates in two modes:

**Hard gate for validation/production:**
- If `requires_pass_stem` is set AND `stage_name` is in `("validation", "production")`, **exit with error** if no passing run of that script stem exists.
- Error code: `REPRODUCTION_PREREQUISITE_UNMET`

```bash
$ bth submit --preset gpu -- bth run --project-slug <slug> -- uv run --no-sync python3 scripts/validate.py
REPRODUCTION_PREREQUISITE_UNMET: no passing run of 'baseline_train' found
# Exit 1 — submission blocked
```

**Advisory warning for exploration/calibration:**
- If `requires_pass_stem` is set AND `stage_name` is in `("exploration", "calibration")`, **warn but continue**.

```bash
$ bth submit --preset gpu -- bth run --project-slug <slug> -- uv run --no-sync python3 scripts/calibrate.py
WARNING: no passing run of 'baseline_train' found (advisory for calibration stage)
# Exit 0 — submission proceeds
```

**Silent skip:**
- If `[reproduction]` block is absent or `requires_pass_stem` is empty, gate is skipped silently.

### `[requires]`, `[budget]` and the preflight gate

Experiment sidecars may declare a precondition on an artifact and a wall-time budget. `artifact` and `probe_record` resolve from the project root (the outermost `.bth.toml` within the git repository, else the git root), not from the sidecar's directory, and a match that resolves outside the project root (symlink escape) is unmet. `bth preflight <script>` checks them; `bth submit` runs the same check before pushing or queueing and fails closed on any error in it. `bth run` checks them in enforced directories through `gate_check`.

```toml
[requires]
workload = "jax"
artifact = "outputs/profiling/run_*.json"    # glob relative to the project root (where .bth.toml sits)
json_key = "git_sha"           # dotted path into the artifact JSON
must_equal = "HEAD"            # a literal, or "HEAD" (a short-sha prefix of HEAD matches)
reject_suffixes = ["-dirty", "-unverified"]   # the default
hint = "Run scripts/experiments/make_probe.py at the current commit first"

[requires.waiver]              # reasoned opt-out of [requires] only; [budget] is still checked
reason = "pilot run, not cited"

[budget]
per_unit_s = 4.0
record_per_unit_key = "timing.per_unit_s"   # dotted path into the probe record
probe_record = "outputs/profiling/probe.json"   # relative to the project root
tolerance = 0.5                             # per_unit_s may not undershoot the measured value by more
```

Severity: an unmet requirement **blocks** at `validation`, `ablation` or `production` stage once enforcement is active, and **warns** otherwise. A sidecar with no `stage_name` resolves to exploration and only warns. Enforcement is active from a date set in the project's `.bth.toml`:

```toml
[gate]
requires_enforce_after = 2026-11-01   # TOML date or "YYYY-MM-DD"; unset means warn-only
```

A malformed `[gate]` value fails closed. A JAX script with no `[requires]` table only warns; profile one unit with `xtrax profile` and cite it.

Exit codes for `bth preflight`: 0 pass or warn (and pass for a script outside an enforced directory, as `gate_check` does), 1 block, 2 the script or its sidecar is missing in an enforced directory. `--json` prints `{"status", "error_code", "errors", "hint", "waiver_reason"}`. `--sidecar PATH` names the sidecar explicitly.

`bth submit` scans every `.py` token of the command for an adjacent `<stem>.bth.toml`. When enforcement is active and no sidecar can be found (no `.py` in the command, as with `bash run.sh` or `python -m x`, or an enforced-directory script without one), submit blocks; pass `--sidecar PATH`. Without enforcement it warns and continues.


#### JAX workloads: the launch gate

`bth new-experiment <stem> --workload jax` scaffolds the sidecar with a live `[requires]` table: `artifact = "outputs/profiling/<stem>/*.json"`, `json_key = "git_sha"`, `must_equal = "HEAD"`, and a hint naming the profiling command. The `[budget]` table ships commented, because zero placeholders fail validation by design. The scaffold validates as written, and `bth preflight` on it reports unmet (no record yet) as a warning.

The loop: profile one unit at production shapes with `xtrax profile --fn <module>:<unit> --shapes "<name>=(<dims>)f32" --extent-axis <name> --n-units <N> --out outputs/profiling/<stem>` (see the `xtrax-probing` skill), commit the record, copy `est. wall per unit s` and `est. total wall s` into `[budget]` and uncomment it, then `bth preflight <script>`. Use `[requires.waiver] reason = "..."` only for a pilot that will not be cited; `[budget]` is still checked.

Enforcement follows the same rule as the rest of the gate: unmet blocks at `validation`, `ablation` or `production` once `[gate] requires_enforce_after` is reached, and warns everywhere else. Until that date is set, the gate warns.

### Lint Checks

#### Tier-1 (Error)

**`check_novel_or_reproduces_declared`** — Enforces reproducibility documentation for validation/production runs.

Triggered by: `bth lint` (Tier-1 block)

**Rule:** Any experiment with `stage_name` in `{"validation", "production"}` must have either:
- `[reproduction]` block with non-empty `reproduces_paper` or `reproduces_run`, OR
- `novel = true`

**Example violations:**
```toml
[experiment]
hypothesis = "Testing X"
stage_name = "validation"
# ✗ FAIL: No [reproduction] block, no novel=true
```

**Fixes:**
```toml
# Option 1: Declare reproduction
[experiment]
hypothesis = "Testing X"
stage_name = "validation"

[reproduction]
reproduces_paper = "doi:10.1234/example"

# Option 2: Declare as novel
[experiment]
hypothesis = "Testing X"
stage_name = "validation"
novel = true
```

#### Tier-2 (Warning)

**`check_bypass_trend`** — Flags increasing use of `--no-sidecar` (tracked as `sidecar_mode='bypassed'`).

Triggered by: `bth lint` (Tier-2 advisory)

Warns if latest week's bypass rate is higher than prior week's.

**`check_canonical_stage_names`** — Flags non-canonical `stage_name` values already in the warm catalog.

Triggered by: `bth lint` (Tier-2 advisory)

Example:
```
WARNING: Non-canonical stage_name 'pilot' in warm catalog (1 run). Consider renaming to one of: exploration, calibration, validation, ablation, production
```

### Sprint Audit Signals

Run sprint audit to check project health across controls:

```bash
bth sprint-audit
```

This section covers Signals 9 and 10 (controls-discipline-relevant); the audit computes 14 signals total — Signals 1-8 and 11/13/14 cover error/bypass rates, outcome entropy, claim registration, and stale-script backlog, and aren't documented in this skill (Signal 12 is covered in **bathos-campaigns**).

#### Signal 9: control_arm_rate

**Definition:** Fraction of all runs in the project with outcome labels matching the pattern `ctrl_%` (e.g., `ctrl_pass`, `ctrl_fail`).

**Status values:**
- **OK** if `control_arm_rate > 0.0` (at least some control runs exist)
- **WARNING** if `control_arm_rate == 0.0` AND validation/production runs exist (no control runs despite having main runs)
- **INFO** if no runs exist or catalog unavailable

**Usage:** Not based on `[controls]` sidecar block presence; it scans actual outcome values across the catalog.

```bash
$ bth sprint-audit
Signal 9 (control_arm_rate): control_arm_rate=12.5% (5/40 runs with ctrl_* outcome)
Status: OK
```

#### Signal 10: submit_bypass_rate

**Definition:** Fraction of validation/production cluster runs (those with `slurm_job_id` populated) that **lack** a matching submit-provenance record.

Provenance stored at: `~/.bth/catalog/submits/<project_slug>/**/*.parquet`

**Status values:**
- **OK** if `submit_bypass_rate <= 5%` (0.05)
- **WARNING** if `submit_bypass_rate > 5%` (more than 1 in 20 V/P jobs lack provenance)
- **INFO** if no validation/production cluster runs exist

**Usage:** Detects when jobs are submitted outside `bth submit` workflow (e.g., raw `sbatch`), bypassing provenance tracking.

```bash
$ bth sprint-audit
Signal 10 (submit_bypass_rate): submit_bypass_rate=3.0% (1/34 validation/production cluster runs without provenance)
Status: OK
```

### Before binding a claim, audit any review pages (maraxiom projects)

If this project shows run results to a reviewer on a maraxiom review page, run `mrx review audit` (for example `mrx review audit --campaign <campaign_id>`; over MCP the tool is `review_audit`) BEFORE `bth claim register` and BEFORE `bth campaign add` of a shown run into a claim-bound campaign.

Why: bathos does not check timing. `claim register` records the claim path and sha256 and emits `campaign.claim_bound`; it does not compare that time with when a run was first looked at. A run that a person saw on a page and that is then added to a claim-bound campaign is a laundering path (the hypothesis can be chosen after the data was seen). The audit finds it from the review ledger.

This is a manual pre-step: bathos does not enforce it. A pre-bind hook that would let the audit refuse the bind is tracked in maraxen/bathos#104.

Exit 3 (`LAUNDERING` over MCP) means do not bind and do not add. Fix the pre-registration or re-run; never override by editing the ledger. The command needs `MRX_REVIEW_ROOT` or a configured review root. It is read-only: exit 3 on a hit, exit 2 on a refusal.

## Query and Inspect

### List Recent Runs

```bash
bth ls --limit 20 --since 7d
bth ls --status completed --project myproject
```

Shows table of recent runs (project, command, outcome, duration, timestamp).

### Show Run Details

```bash
bth show abc123-uuid
```

Full run metadata, git state, outcome, sidecar hash, postmortem status.

### Check Git-Drift Validity

```bash
bth check
bth check --status STALE
bth check --check-outputs
```

Compares each recorded run's git hash against current HEAD, reporting `OK` / `STALE` / `DIRTY_RUN` / `UNKNOWN_CODE`. `--check-outputs` additionally verifies registered output files still exist and re-checks their SHA against the recorded hash. Exits 1 if any run is `STALE` or has drift. Not a sidecar validator — see `bth validate-sidecar` above.

### Find Runs by Condition

```bash
bth find --project myproject --status completed
bth find --tags experiment:baseline --tags date:2026-06-01
bth find --slurm-job 12345678
bth find --output-file "outputs/run_*.json"
```

**Options:** `--project`, `--since` (e.g. `7d`, `24h`), `--status`, `--tags` (repeatable), `--slurm-job`, `--output-file` (glob against registered output paths). There is **no `--filter` and no `--limit`** — these are fixed equality/glob filters and every match is printed. For an arbitrary DuckDB `WHERE` clause, use `bth sql` instead:

```bash
bth sql "SELECT * FROM runs WHERE outcome='pass' AND project_slug='myproject'"
```

### Run Arbitrary SQL

```bash
bth sql "SELECT outcome, COUNT(*) FROM runs WHERE project_slug='myproject' GROUP BY outcome"
```

Query catalog directly. Useful for analytics and audits.

## Catalog Management

### Compact Cool Tier → Warm Tier

```bash
bth compact
bth compact --strict          # exit non-zero if any cool fragment is corrupt (CI/automation)
bth compact --force-rebuild   # rebuild bathos.db from cool fragments if the warm DB is corrupt
```

In log mode (`cutover.json` present) this instead folds new log events into `index.db` (the same ingest `bth run` does at its end) and never touches `bathos.db`. In a legacy catalog it merges per-run Parquet fragments into the DuckDB database. Automatic (banner shown) on `bth ls` if fragmentation is high. A corrupt/unreadable fragment is skipped and reported by default rather than aborting compaction catalog-wide (`bth repair --tier cool` quarantines it); `--strict` restores the old hard-fail behavior.

### Archive Old Runs

```bash
bth archive --project myproject --dry-run   # preview
bth archive --project myproject --archive-dir ~/backups/archive
```

Exports warm-tier runs to cold-tier partitioned Parquet (by project/year/month, default root `~/.bth/archive`) plus a manifest. There is **no `--before` age filter and no `--out` file target** — `archive` exports every run for `--project` (default: all projects); `--dry-run` shows counts without writing. Requires a warm catalog (`bth compact` first).

**Not the same as** the stale-script archive gate (`bth archive-artifact`/`bth restore`) — see "Stale-Script Archive Gate" below.

### Stale-Script Archive Gate

Distinct from `bth archive` above (catalog cold-tier export). A sidecar's `[status] stale = true`
blocks `bth run` unless `--allow-stale` is passed (see Run Tracking Options). `bth archive-artifact`
moves the stale script's tracked bytes into a git-notes-backed ledger for exact later recovery via
`bth restore`.

### Migrate Schema

```bash
bth migrate                      # scans and migrates ALL projects' cool fragments
bth migrate --project myproject  # scope to runs/myproject/ only
bth migrate --dry-run            # preview without writing
```

Upgrades cool-tier Parquet fragments to the current schema. Run after updating bathos. Note: `bth compact` does **not** require this to run first — it already tolerates old-schema fragments on its own.

### Catalog Maintenance & Repair

```bash
bth verify --tier cool        # read-only diagnostic: cool | warm | archive | all
bth repair --tier cool --dry-run
bth repair --tier cool --apply
```

`bth verify` diagnoses catalog integrity issues (sentinel files, corrupt fragments, warm/cool
mismatches) without changing anything. `bth repair` fixes what `verify` finds: sentinel cleanup,
corrupt-fragment quarantine, and (with `--acknowledge-warm-loss`) rebuilding a corrupted warm DB
from cool fragments (`--from-warm` for the reverse direction). MCP mirrors: `repair_scan`, `repair`.

### Project Subdirectories (v0.4+)

```bash
bth migrate-to-project-subdirs
```

Reorganizes flat catalog to per-project subdirectories (`runs/<slug>/run_<uuid>.parquet`). Enables per-project filtering in `bth sync`.

## Visualization (v0.5+)

### Local Dashboard

```bash
bth view
bth view --port 9000 --project myproject --no-open
```

Opens FastAPI dashboard (default: `http://localhost:8080`; `--port`/`--host` override, `--no-open` skips auto-launching a browser, `--project` scopes to one project) with interactive run browser, campaign summaries, outcome histograms.

### Static HTML Export

```bash
bth export --html --out ~/reports/report.html
```

Generates self-contained HTML report of all runs. Warns if > 5 MB.

## Linting and Validation

### Lint Catalog and Scripts

```bash
bth lint
```

Tier-1 checks: missing sidecars, schema violations, outcome condition validity.
Tier-2 checks: adversarial conditions (always true/false), unbound columns, drift detection.

### Linting in Agent Mode

Scripts run with `--agent-mode` enforce stricter validation and flag adversarial conditions.

## Project Configuration (`.bth.toml`)

```toml
[project]
slug = "myproject"
root = "."
catalog_dir = "~/.bth/catalog"

[slurm]
remote = "engaging"
preset = "gpu"
project = "myproject"

[remotes.engaging]
host = "engaging"
path = "~/projects/myproject"
```

## Key Rules

- **Always use `uv run python`** in sbatch scripts, never bare `python` (cluster nodes have no global python)
- **Verify sidecar before submission** — run `bth validate-sidecar <path>` to validate outcome conditions; `bth check` checks *recorded runs* against current git HEAD, it does not validate sidecars
- **Result schema must include all outcome columns** — if `condition = "metric >= 0.9"`, declare `metric = "float"` in `[result_schema]`
- **Exactly one residual outcome** — one outcome must have `is_residual = true` for gate evaluation
- **No `--no-sidecar` in production** — bypassing logs `BYPASSED` and breaks pre-registration discipline
- **Test measurement pipeline on synthetic data** — verify metrics work before trusting research conclusions, with a positive AND a negative control (see **Discipline** above)
- **Pre-register before the run; run as `bth`; verify by the record** — the three-line core of the **Discipline** section
- **Postmortem colocated with script** — `<script>.bth.postmortem.toml` alongside `<script>.py` (see **bathos-campaigns**)

## Typical Workflow

```bash
# 1. Initialize project
bth init --slug myproject --slurm-partition mit_normal

# 2. Create experiment
bth new-experiment baseline_training
# Edit scripts/experiments/baseline_training.py and .bth.toml

# 3. Validate locally
bth validate-sidecar scripts/experiments/baseline_training.bth.toml
uv run python scripts/experiments/baseline_training.py --smoke --out /tmp/test.json  # NOT via bth run — smoke outputs are ephemeral, not tracked

# 4. Run locally or submit to cluster (see bathos-cluster for bth submit)
bth run -- uv run --no-sync python scripts/experiments/baseline_training.py --out outputs/run.json
# Then verify by the RECORD (outcome != unknown), `bth compact` only on a legacy (no cutover.json) catalog

# 5. Query results
bth sql "SELECT * FROM runs WHERE outcome='pass' AND project_slug='myproject'"

# 6. Export report
bth export --html --out ~/reports/latest.html
```

## Related

- **bathos-cluster** — SLURM submission (`bth submit`), catalog sync (`bth sync`), remote profiles
- **bathos-campaigns** — campaigns, figure manifests, lineage/citation, postmortems, claim-tier pre-registration, signal discrimination and probe design
- **bathos-literature-parity** — validating a reimplemented baseline against a published method
- **bathos-blast-radius** — trace which past runs a bug fix/commit implicates
- **bathos-mcp** — MCP tool error envelope and integration contract
- **Writing a `bth run` / `bth submit` command line, an `[outcomes]` condition, or choosing `--output-paths`** → `references/run-invocation-rules.md` (`--` placement, `uv run python`, DuckDB-valid conditions, `bth sync` ownership, campaigns, `--no-sidecar`)
- **AGENTS.md** (bathos repo): bathos architecture, schema versions, backlog
- **Global rules**: `~/.claude/rules/ephemeral_scripts.md` — inline vs tracked scripts (the synthetic-ground-truth rule formerly in `~/.claude/rules/BATHOS.md` is folded into **Discipline** above)
