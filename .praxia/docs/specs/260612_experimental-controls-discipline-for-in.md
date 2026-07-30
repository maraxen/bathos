---
session_id: f262cda9
topic: Experimental controls discipline for in silico experiments in bathos: designing sidecar fields, gate enforcement, and campaign machinery to support protocol validation controls, positive/negative specimen controls, baseline reproduction, and convergence requirements before novel experiments proceed
task_type: architectural
winner: Faction A (Sidecar-centric) modified: additive [reproduction] and [controls] blocks on existing sidecar types; enforcement boundary moved to bth submit time (warm-tier DuckDB, pre-sbatch); stage-name-gated severity (exploration=Tier-2 lint advisory, validation/production=hard submit gate); ctrl_pass/ctrl_fail outcome label convention for control arm sprint-audit tracking; control_role tagged at submit time on the Run record (not retroactively); baseline_ref validated at lint/audit time against warm-tier catalog
created_at: 2026-06-12T14:38:59.194520+00:00
---

# Brainstorm: Experimental controls discipline for in silico experiments in bathos: designing sidecar fields, gate enforcement, and campaign machinery to support protocol validation controls, positive/negative specimen controls, baseline reproduction, and convergence requirements before novel experiments proceed

## Problem Frame
Fixed constraints: (1) Sidecars stay TOML, adjacent to scripts. (2) Outcome conditions stay DuckDB SQL fragments — no custom DSL. (3) Gate check must run in SLURM job startup — no catalog network calls, only local filesystem reads. (4) MCP mirrors CLI tool-for-tool. (5) Python 3.12, Typer, DuckDB, PyArrow stack. (6) No breaking changes to existing sidecar schemas — all new fields are additive. Negotiable: (a) whether control discipline is enforced at gate-check time vs. lint-time vs. campaign-time, (b) whether positive/negative controls are declared in the sidecar or in a separate control manifest, (c) whether reproduction-first is a hard gate or a campaign-level precondition, (d) how much the campaign data model changes vs. stays advisory metadata.

## Idea Pool
- [user] Three competing approaches:
- [user] Faction A — Sidecar-centric: Controls are declared entirely in the sidecar TOML as new optional blocks: [controls.positive], [controls.negative], [reproduces]. Each block specifies expected outcome labels. Gate check at bth run time reads cool-tier Parquet to verify reproduction prerequisites exist and passed. Everything lives in one file adjacent to the script.
- [user] Faction B — Campaign-centric: The sidecar stays minimal (no new fields). A new campaign attribute `control_mode = "paired"` tags certain campaign runs as control vs. treatment via `bth run --role control`. Reproduction is tracked at the campaign level — a confirmation campaign cannot start until a named prerequisite campaign exists with a pass outcome. Disciplines lives in campaign config, not sidecar.
- [user] Faction C — Hybrid intent/tracking split: Sidecar declares design intent (what controls SHOULD be run, what paper/run is being reproduced). Campaign machinery tracks fulfillment (what WAS run as controls, whether preconditions were satisfied). These are separate concerns: sidecar = declaration, campaign = enforcement history. Reproduction gate is a campaign-level precondition checked at campaign creation time, not at individual run gate check.
- [ai] Faction A — Sidecar-centric: [controls.positive] / [controls.negative] / [reproduces] blocks in sidecar TOML; gate check reads cool-tier Parquet for reproduction prerequisite; all design intent and enforcement in one file
- [ai] Faction B — Campaign-centric: campaign gets control_mode="paired"; runs tagged --role control|treatment at bth run time; reproduction tracked at campaign level as prerequisite campaign; sidecar unchanged
- [ai] Faction C — Hybrid: sidecar = design intent (what controls should exist, what is being reproduced); campaign = fulfillment tracking (what was run, whether preconditions satisfied); reproduction gate is campaign-level not per-run gate
- [ai] Faction D — Lint-only: add Tier-2 lint signals for control coverage gaps (missing adversarial_check, missing baseline_ref comparison, no reproduction run in campaign history); no new sidecar fields, no new gate enforcement; purely advisory signals that surface at bth lint time
- [user] Probing Faction A: If controls are declared in the sidecar, what is the unit of a "control"? Is it a specific input file path (e.g., alanine_dipeptide.pdb), a named outcome label ("control_pass"), or an entire named script stem? And when a SLURM job runs on a cluster node, the cool-tier Parquet may live on a shared filesystem — does scanning it at job startup create a race condition with concurrent jobs writing to the same directory?
- [user] Probing Faction B: If --role control|treatment is passed at bth run time, nothing in the sidecar knows that control runs are required before treatment runs. A researcher could run treatment first and tag it later. What prevents post-hoc role assignment?
- [user] Probing Faction C: If reproduction is a campaign-level precondition checked at campaign creation (not at run time), then an autonomous agent that spawns individual bth run calls without creating a campaign first has no enforcement. How do you close that gap without requiring every run to be part of a campaign?
- [user] Additional idea to consider — Faction E: A new sidecar type `[protocol_validation]` (distinct from experiment/benchmark/validation/debug) specifically for reproduction runs, with fields: `reproduces_paper`, `reproduces_run`, `tolerance_pct`. This type is enforced by the Tier-1 linter as required before any [experiment] sidecar in the same script directory can be run for the first time. Creates explicit first-class citizen for reproduction, not bolted onto existing types.
- [ai] Faction E — New sidecar type [protocol_validation]: distinct from experiment/benchmark/validation/debug; fields: reproduces_paper, reproduces_run, tolerance_pct; Tier-1 linter requires this type to pass before any [experiment] in the same directory can run for the first time
- [ai] Control unit options: (a) input file path as control specimen, (b) named outcome label as control type, (c) script stem as protocol validation target — these are three orthogonal axes that can be combined
- [ai] Reproduction gate: require a named script stem to have produced a passing run in the current catalog before the gated script can run — check cool-tier Parquet by scanning for script_stem + outcome='pass' at gate time (no network, filesystem only)
- [ai] Control outcome tagging: add outcome label namespace — outcome labels prefixed 'ctrl_pass' / 'ctrl_fail' are treated as control outcomes by sprint-audit signals, enabling separate rate tracking for controls vs. treatment arms without new schema fields
- [user] Assumption reversal: we've been assuming controls must be declared before the run. What if instead bathos infers control status from outcome label patterns post-hoc and flags missing controls as a lint signal? That would be zero schema change, purely analytical. Counter to that: the whole bathos philosophy is pre-registration — inferring post-hoc would undermine the exact discipline we're trying to enforce.
- [user] Another assumption reversal: we've been assuming reproduction is a gate. What if reproduction is instead a required FIELD — the sidecar must declare what it reproduces (or explicitly assert 'novel=true' meaning no reproduction needed), but whether that reproduction actually passed is only checked at lint/audit time? This separates declaration discipline (hard) from execution order (advisory).
- [user] Key unresolved tension: the SLURM race condition for cool-tier Parquet reads during gate check. If 8 array jobs start simultaneously and each checks whether a control run has completed, and the control run is one of those 8 jobs, the check is meaningless. Resolution options: (a) control runs must complete before array submission (workflow gate at sbatch level, not bth run level), (b) gate check is skipped inside SLURM array jobs and only enforced for interactive/single runs, (c) the reproduction prerequisite is checked at campaign creation time only, not per-run.
- [user] Edge case: what happens when baseline_ref points to a run from 6 months ago that has been cold-archived? The cool-tier scan would find nothing. Need to either (a) fall back to warm/cold tier (requires DuckDB query, heavier), or (b) only validate baseline_ref at lint/audit time when full catalog is available.
- [user] converge

## Decision Log
- [REJECT] Faction B — Campaign-centric (control_mode="paired", --role control|treatment at bth run time): SLURM array jobs start concurrently — no ordering guarantee at bth run level. Post-hoc role assignment is structurally unavoidable. Enforcement is illusory.
- [REJECT] Faction C — Hybrid (campaign-level reproduction gate at campaign creation time): Assumes every bth run is wrapped in a campaign. Autonomous agents and exploratory runs frequently bypass campaigns. The sidecar-to-campaign enforcement seam is unspecified. Enforcement gap is structural.
- [REJECT] Faction D — Lint-only (no new schema, purely advisory signals): Cannot enforce anything in autonomous agent pipelines. Addresses the symptom (we want to know about control gaps) without addressing the cause (pre-registration of controls). Inconsistent with bathos philosophy of pre-run declaration gates.
- [REJECT] Faction E — New sidecar type [protocol_validation]: Substantial implementation cost (new schema branch, validate.py branch, linter checks, MCP mirroring) for what is an experiment sidecar with a reproduces_paper field. Directory-scoping assumption conflicts with flat scripts/experiments/ namespace. Better: additive [reproduction] block on existing [experiment] sidecar.
- [REJECT] Reproduction gate at bth run time (cool-tier Parquet scan): SLURM array jobs start simultaneously — gate check is meaningless if the required control run is one of the concurrent array jobs. Gate must move to bth submit time (pre-sbatch) where warm-tier DuckDB is available and jobs have not yet started.
- [ACCEPT] ctrl_pass/ctrl_fail outcome label namespacing for control arm tracking: Zero schema change. Enables sprint-audit signal to separately track control arm pass rates vs. treatment arm pass rates. Researcher declares control runs by naming outcomes with ctrl_ prefix — natural and low overhead. Retroactive re-labeling is out of scope by design.
- [ACCEPT] Stage-name-gated enforcement: exploration=advisory, validation=hard gate: Avoids high overhead in early exploration where controls aren't yet known. Enforces discipline precisely when it matters (validation, production stages). Consistent with existing stage_name field in sprint-audit.
- [ACCEPT → REVISED] Reproduction prerequisite matching: Round-1 accepted hash-pinning (sidecar_sha256 + script_stem). Round-2 adversarial review found (a) `script_stem` is not a Run column (must use `command LIKE`), and (b) hash-pinning the *prerequisite's* sidecar is complex in v1. Decision: v1 uses `command LIKE '%stem%' AND outcome='pass'` only; hash-pinning deferred to TBD-6. The scientific risk (stale pass from an old protocol) is documented in TBD-6 and the pre-mortem.

## Acceptance Criteria (Given-When-Then)

> **Prerequisite ordering:** AC-0 must be implemented first. AC-3, AC-4, AC-5, AC-7, and AC-9 are blocked until AC-0 lands. AC-1, AC-2, AC-6, AC-8 are independent.

---

**AC-0 — stage_name write path (prerequisite for all stage-gated ACs)**
*[Adversarial review finding: `stage_name` exists in Run schema but has no write path — always NULL today; every stage-gated AC is silently inert without this. Round-2 finding: two competing definitions exist (regex vs. canonical set) — behavior on non-canonical values must be specified.]*

Given a sidecar with `[experiment]` section and an optional `stage_name` field,
When `parse_sidecar` runs (`sidecar.py`),
Then `stage_name` is read from `[experiment].stage_name` and stored on the `Sidecar` dataclass; if absent, defaults to `'exploration'`.

When `bth run` constructs a Run record (`runner.py:271` `Run(...)` call),
Then `stage_name=sidecar.stage_name or 'exploration'` is passed as a kwarg — this is the only change needed since the `Run.stage_name` field already exists in `schema.py:172` and round-trips through `to_arrow`/`from_arrow_row`.

Canonical values (from `linter.py:597 CANONICAL_STAGES`): `'exploration'` | `'calibration'` | `'validation'` | `'ablation'` | `'production'`. Gate logic in AC-3/AC-4/AC-7 matches only these exact strings. Non-canonical but regex-valid values (e.g. `'validaton'` typo, `'my-stage'`): `parse_sidecar` emits a WARNING and coerces to `'exploration'` for gate purposes — this is the **safe direction** (advisory path, not hard-gate). Unrecognised values do NOT silently escape the hard gate by appearing to be in a canonical stage; they fall through to the advisory path.

When `bth submit` reads the sidecar for gate checks (AC-3/AC-4), it reads `stage_name` from the sidecar TOML directly (not from the Run record, which doesn't exist yet at submit time).

---

**AC-1 — [reproduction] block: parse, dataclass, validate (additive, optional)**
*[Adversarial review finding: `parse_sidecar` silently drops unknown sections; AC-1 must explicitly scope parse+dataclass+validate work, not just declare fields.]*

Given an experiment sidecar with an optional `[reproduction]` block containing any of: `reproduces_paper` (string, DOI or citation), `reproduces_run` (string, run UUID), `tolerance_pct` (float, 0–100), `requires_pass_stem` (string, script stem that must have outcome='pass' before this script submits),
When `parse_sidecar` runs (`sidecar.py`),
Then the `[reproduction]` block is parsed into a `ReproductionBlock` dataclass (following `[popper]` precedent at `sidecar.py:87–94`) with **all four named fields explicitly parsed as dataclass attributes**: `reproduces_paper: str = ""`, `reproduces_run: str = ""`, `tolerance_pct: float | None = None`, `requires_pass_stem: str = ""`; `validate_sidecar` range-checks `tolerance_pct` (0–100) and format-checks `reproduces_run` (UUID pattern `^[0-9a-f-]{36}$`); keys within `[reproduction]` that are not in this four-field set emit a WARNING (the WARNING rule does not apply to `requires_pass_stem` — it is a known field); absence of the block is valid (not required at this AC — AC-7 handles declaration requirements).

---

**AC-2 — [controls] block: parse, validate, label cross-reference (additive, optional)**
*[Adversarial review finding: same silent-drop problem as AC-1; also clarifies that ctrl_* outcome labels must be declared in [outcomes] before [controls] can reference them — AC-2 and AC-5 are consistent, not contradictory.]*

Given an experiment sidecar with an optional `[controls]` block containing `positive_outcome` (list of strings) and/or `negative_outcome` (list of strings),
When `parse_sidecar` runs,
Then the `[controls]` block is parsed into a `ControlsBlock` dataclass field on `Sidecar`; `validate_sidecar` checks that each label in `positive_outcome` and `negative_outcome` exists as a key in `sidecar.outcomes`; if any label is not found in `[outcomes]`, validation fails with `CONTROLS_LABEL_NOT_FOUND`; `[controls]` labels and `ctrl_*` outcome naming (AC-5) are compatible — a sidecar declares `ctrl_pass` in `[outcomes]` and references it in `[controls].positive_outcome`.

---

**AC-3 — Reproduction prerequisite gate at bth submit (validation/production stages)**
*[Round-2 finding: `script_stem` is not a Run column — must use `command LIKE` pattern (per `prereg.py:158`). Cool-tier fallback must specify pyarrow scan, not SQL.]*

Given a sidecar with `[reproduction].requires_pass_stem = "target_stem"` and `stage_name` of 'validation' or 'production' (read from sidecar per AC-0),
When `bth submit` is called (`cli.py:submit`),
Then `bth submit` queries the catalog for a passing run of `target_stem` using the **warm-then-cool fallback pattern from `prereg.py:151–175`**:
- **Warm path** (DuckDB available): `SELECT 1 FROM runs WHERE command LIKE ? AND outcome = 'pass' LIMIT 1` with parameter `'%' + target_stem + '%'` — exact same `command LIKE` idiom used by `check_first_of_kind` at `prereg.py:158`.
- **Cool-tier fallback** (no `bathos.db`): glob `catalog/runs/**/*.parquet`, read with `pyarrow.parquet.read_table` (already a core dep, `pyproject.toml:28`), filter rows where `command` column contains `target_stem` and `outcome == 'pass'` — same pattern as `catalog.py:43` + `prereg.py:168–175`.
If no matching run exists in either tier, exits non-zero with error code `REPRODUCTION_PREREQUISITE_UNMET` and a message identifying the missing stem.
Note: `sidecar_sha256` matching is intentionally NOT required in v1 — see TBD-6. Decision Log entry at spec line 45 is superseded: hash-pinning of prerequisites is deferred.

---

**AC-4 — Reproduction prerequisite advisory in exploration/calibration stages**
Given the same `[reproduction].requires_pass_stem` field and `stage_name` of 'exploration' or 'calibration' (read from sidecar per AC-0),
When `bth submit` is called,
Then `bth submit` emits a WARNING to stderr (does not exit non-zero) if the prerequisite is unmet.

---

**AC-5 — ctrl_pass / ctrl_fail outcome label convention (sprint-audit signal)**
Given any project with runs whose outcome labels begin with `ctrl_`,
When `bth sprint-audit` runs,
Then a new signal `control_arm_rate` reports `(runs with ctrl_* outcome label) / (total runs in project)`, with a WARNING if this rate is 0.0 for any campaign whose runs have `stage_name IN ('validation', 'production')` (using AC-0's populated `stage_name`).

---

**AC-6 — baseline_ref validation at lint time**
Given a benchmark sidecar with `baseline_ref` set to a non-empty string,
When `bth lint` runs (Tier-2, warm-tier DuckDB available — confirmed already opened by `check_residual_rates` et al.),
Then `bth lint` queries: `SELECT outcome, started_at FROM runs WHERE id = ? OR id LIKE ? LIMIT 1` with the raw value and `value%` to support both full UUIDs and short prefixes; if no run is found, emits WARNING `"baseline_ref '<value>' not found in warm-tier catalog"`; if found, emits INFO showing the run's outcome, date, and `script_stem`.

---

**AC-7 — novel / reproduces declaration discipline (Tier-1 lint, validation/production)**
*[Adversarial review finding: AC-7 is implementable at lint time because lint reads the sidecar TOML directly — it does NOT depend on the Run record's stage_name. `stage_name` is read from the sidecar's `[experiment].stage_name` field (AC-0).]*

Given any experiment sidecar with `[experiment].stage_name` of 'validation' or 'production',
When `bth lint` Tier-1 (`lint_project`, filesystem-only pass) runs,
Then the sidecar must contain either: `[reproduction]` with at least one of `reproduces_paper` or `reproduces_run` set to a non-empty string, OR `[experiment].novel = true`; if neither is present, Tier-1 lint emits ERROR with message `"validation/production experiment must declare [reproduction] or novel=true"`.

---

**AC-8 — bth new-experiment scaffold completeness (fixes latent validation failure)**
*[Adversarial review finding: current scaffold fails `validate_sidecar` — missing `reasoning` and `is_residual`. AC-8 fixes a latent bug.]*

Given `bth new-experiment` is called with any arguments,
When the scaffold is generated (`new_experiment.py:_SIDECAR_TEMPLATE`),
Then the generated sidecar: (a) **passes `bth validate-sidecar` out of the box** — this requires concrete, parseable SQL conditions (not TODO strings); (b) includes `reasoning` on all outcome branches (`validate.py:152–159`); (c) has `is_residual = true` on the fail branch and `is_residual = false` on pass (`validate.py:206–216`); (d) has a placeholder `adversarial_check` on the pass branch that is **valid SQL** (e.g. `adversarial_check = "metric >= 5.0"`); (e) ships a concrete `[result_schema]` with one example field (e.g. `metric = "float"`) and pass/fail conditions that reference it (e.g. `condition = "metric < 5.0"` / `condition = "metric >= 5.0"`) so the schema-reference check at `validate.py:197` passes; (f) has `stage_name = "exploration"` in `[experiment]`; (g) has `novel = false` in `[experiment]`; (h) has a commented-out `[reproduction]` block with field name hints; (i) has a commented-out `[controls]` block with field name hints.
*[Round-2 finding: TODO strings are not valid DuckDB SQL — `validate.py:162` will reject them. Scaffold must ship real parseable SQL with a real result_schema field that conditions reference.]*

---

**AC-9 — Submit bypass detection (new emission seam + sprint-audit signal)**
*[Adversarial review finding: `bth_submit_version` is never emitted and `bth submit` writes no Run record — AC-9 must scope the emission seam, not just the consuming signal.]*

Given `bth submit` is called for a bathos-tracked project (`cli.py:submit`),
When the submit succeeds (myxcel job ID returned),
Then `bth submit` writes a submit-provenance record (following `catalog.py:18–32` atomic write-then-rename pattern) to `~/.bth/catalog/submits/<project_slug>/<timestamp>_submit.parquet` with the following **exact PyArrow schema**:

| Field | Type | Source |
|---|---|---|
| `project_slug` | `pa.string()` | from `cluster.project` |
| `command` | `pa.string()` | the script path token (e.g. `scripts/experiments/foo.py`) |
| `sidecar_sha256` | `pa.string()` | hash of located sidecar file |
| `bth_submit_version` | `pa.string()` | `importlib.metadata.version("bathos")` |
| `submitted_at` | `pa.timestamp('us', tz='UTC')` | wall clock at submit time |
| `myxcel_job_id` | `pa.string()` | SLURM job ID returned by myxcel |
| `stage_name` | `pa.string()` | from sidecar `[experiment].stage_name` |

This file is NOT compacted into the Run table (separate provenance). All fields are in scope at `cli.py:997` (after myxcel returns `slurm_job_id`).

And when `bth sprint-audit` runs,
Then a new signal `submit_bypass_rate` is computed by:
1. Loading all submit-provenance records: `pyarrow.parquet.read_table(glob("~/.bth/catalog/submits/<slug>/**/*.parquet"))` (same pyarrow pattern as cool-tier reads).
2. Joining runs against provenance on **`runs.slurm_job_id = submits.myxcel_job_id`** — queue-time-invariant, not a time-window join. (`slurm_job_id` is already a Run column at `schema.py:49`.)
3. Runs with no matching provenance record are flagged as bypassed.
4. Signal reports `(bypassed runs) / total_runs` with WARNING if > 5% in validation/production stage runs.
*[Round-2 finding: ±5 minute time-window join fails for long-queued jobs. `slurm_job_id ↔ myxcel_job_id` is the correct queue-time-invariant join key.]*

## Assumptions

| # | Status | Assumption | Evidence / Mitigation |
|---|---|---|---|
| A1 | PARTIALLY REFUTED | `bth submit` is the universal cluster submission path | `bth submit` exists (`cli.py:894`) but writes no Run record and no provenance today. Mitigated by AC-9 adding an explicit submit-provenance write. Direct `sbatch` bypasses remain detectable only via the submit-provenance join. |
| A2 | VERIFIED | Warm-tier DuckDB is available on the local machine at `bth submit` time | `bth submit` runs locally (loads `find_project_config` from local FS, delegates to myxcel). Same warm-DB read pattern used by `linter.py:159` is reachable in the submit process. |
| A3 | REFUTED — resolved by AC-0 | `stage_name` populated on Run records and readable from sidecar at submit time | `stage_name` is in `schema.py:75,123` and the `Run` dataclass (default `None`) but **nothing writes it** — no `--stage` flag, no sidecar read in `runner.py`. AC-0 adds the write path. For submit-time use (AC-3/4), `stage_name` is read from the sidecar directly, not from the Run record (which doesn't exist yet). |
| A4 | VERIFIED — with caveat | All new sidecar fields additive; existing parsers ignore unknown sections | True: `parse_sidecar` silently drops unknown sections. Caveat: "ignored" means the block is never accessible — AC-1 and AC-2 must explicitly add parse branches for `[reproduction]` and `[controls]` (following `[popper]` precedent at `sidecar.py:87–94`). |
| A5 | VERIFIED — semantics clarified | `sidecar_sha256` stored per Run at `bth run` time | Confirmed: `schema.py:53,101`; written at `runner.py:283`. AC-3's prerequisite gate uses `script_stem + outcome='pass'` match only (not hash-pinning) — see AC-3 note. Hash-pinning of prerequisites is deferred to TBD-6. |

## TBDs

| # | Status | Open question | Blocks | Resolution |
|---|---|---|---|---|
| TBD-1 | CLOSED (resolved by AC-0) | How does `bth submit` learn `stage_name`? | AC-3, AC-4, AC-5, AC-7, AC-9 | Read `[experiment].stage_name` from sidecar TOML at both submit time and run time; fall back to 'exploration'. Implemented as part of AC-0. |
| TBD-2 | CLOSED (exact stem) | `requires_pass_stem` matching: exact stem vs. glob? | AC-3 | Exact stem. `check_first_of_kind` (`prereg.py:157`) uses `command LIKE ?` pattern — exact-stem query reuses this approach. |
| TBD-3 | CLOSED (confirmed label-refs) | `[controls]` block: SQL strings vs. outcome label references? | AC-2 | Label references only. Consistent with `validate.py:92–98` (popper.weights validated as label refs). No SQL in controls. |
| TBD-4 | CLOSED — needs emission seam | Is `bth_submit_version` already emitted by `bth submit`? | AC-9 | NO — confirmed zero occurrences in source. AC-9 scopes the new submit-provenance write as a cool-tier Parquet file (not a Run record field). |
| TBD-5 | CLOSED — already true | Does `bth lint` open warm-tier DuckDB? | AC-6 | YES — `linter.py` already opens `catalog_dir/bathos.db` read-only in five Tier-2 functions (`check_residual_rates:159`, `check_bypass_trend:223`, `check_unfired_branches:351`, `check_ephemeral_output_paths:525`, `check_canonical_stage_names:606`). AC-6 is a sixth instance. |
| TBD-6 | OPEN | Should `requires_pass_stem` matching pin to a specific `sidecar_sha256` of the prerequisite, or accept any passing run of that stem? | AC-3 (future enhancement) | Deferred. v1 matches stem + outcome='pass' only. Pinning to a specific sidecar version is a future hardening step. |

## Pre-mortem Record

Six months in, the system has failed in two ways:

1. **Submit bypass**: Researchers or automation call `sbatch` directly, bypassing `bth submit` entirely. The reproduction prerequisite gate has zero coverage for jobs submitted outside bathos tooling. Mitigation: AC-9 `submit_bypass_rate` signal; Tier-2 lint advisory if `bth_submit_version` absent in recent validation/production runs.

2. **Inert reproduces_paper field**: `reproduces_paper` stores a DOI but bathos never fetches reference values to compare against. After six months, hundreds of runs declare they reproduce a paper but no run has been automatically verified against the paper's actual numbers. Mitigation: AC-7 requires declaration discipline (field must be present or `novel=true`) but does NOT claim to validate against paper values — scope is intentionally limited to declaration enforcement, not automated reproducibility verification. The tolerance_pct field is structural-only in v1.
