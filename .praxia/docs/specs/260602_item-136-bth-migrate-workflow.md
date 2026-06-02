# Item #136: bth-migrate Phase 2 — Agentic Script Classification and git mv Plan

**Date:** 2026-06-02
**Status:** Draft — 10 open questions; NOT ready to implement (see §Open Questions before dispatching fixer)
**Task ID:** 260602_bathos-v08-sprint
**Depends on:** #135 (Phase 1 — catalog schema migration, shipped in v0.7)

---

## Executive Summary

Phase 2 of `bth migrate` introduces agentic classification of flat scripts (files sitting directly under `scripts/` rather than in any named subdirectory) into the correct target subdirectory as defined by `linter.py`'s `_DIR_RULES` schema, followed by generation of a `git mv` plan and optional sidecar scaffolding for newly-classified scripts that require one.

This is distinct from Phase 1 (which upgrades cool-tier Parquet fragments in `~/.bth/catalog/`) and addresses a structural problem in real research projects: scripts accumulate at `scripts/*.py` over time, outside the taxonomy, and the linter cannot enforce naming or sidecar requirements on them. Classifying them into the right subdirectory is a necessary prerequisite for bathos tracking to work as designed.

**Primary target:** `prolix` (52 flat scripts at `scripts/*.py`). Secondary: `oaf`, `demistify`, and any future project in the same situation.

**Cross-repo concern:** The classification logic is general enough to be useful as a workflow artifact in praxia (a `bth-migrate.yaml` workflow template) as well as a CLI subcommand in bathos (`bth classify` or `bth migrate --classify`). This spec covers both surfaces.

---

## Background and Motivation

### The Problem

The `prolix` project has 52 Python scripts sitting directly under `scripts/`:

```
scripts/benchmark_efa_vs_pme.py
scripts/debug_bonds.py
scripts/validate_langevin_settle_dt05fs.py
scripts/simulate_1uao_explicit.py
scripts/ablation_efa.py
... (47 more)
```

The linter only inspects files inside known subdirectories (`scripts/experiments/`, `scripts/benchmarks/`, etc.) — flat scripts are completely invisible to it. As a result:

1. `bth run` on these scripts does not enforce sidecar discipline (sidecar lookup uses `script_path.parent / stem.bth.toml`, which would place the sidecar at `scripts/` root — the linter never checks it).
2. `bth lint` produces no output for these files, giving a false clean bill of health.
3. Researchers cannot `bth run --tag experiment` and have outcome evaluation fire, because `is_in_enforced_dir()` returns `False` and the sidecar is optional.

### Why This is Phase 2

Phase 1 (`migrate_catalog` and `migrate_to_project_subdirs`) addressed the cool-tier Parquet catalog — moving run fragments from flat `runs/*.parquet` to `runs/<slug>/*.parquet`. That work is complete and shipped in v0.7.

Phase 2 addresses the complementary problem at the source level: getting scripts themselves into the correct directory taxonomy so that the linter, sidecar enforcement, and outcome evaluation work as intended.

### Relationship Between #135 and #136

The original backlog annotation stated "#136 depends on #135," but the actual dependency is weak. #135 (historical SLURM log ingestion) and #136 (script classification) are independent tracks. #136 does not need to wait for SLURM log migration. The `depends_on` field in the backlog can be cleared.

---

## Scope

### In Scope

1. **Classification heuristics engine** — a function that takes a `Path` to a flat script and returns a `ClassificationResult` (target directory, confidence, rationale, required name transformation if the current stem does not match the target naming convention).

2. **git mv plan generator** — takes a list of `ClassificationResult` objects, validates for conflicts, and emits a structured list of `MoveAction` objects (source, destination, renames required, conflicts detected).

3. **Sidecar scaffold integration** — for scripts classified into `experiments/` or `benchmarks/` (where `sidecar = IssueSeverity.ERROR`), generate a minimal scaffold sidecar at the destination path.

4. **`bth classify` CLI command** — dry-run by default, `--apply` flag to execute `git mv` and write scaffolds.

5. **`bth-migrate.yaml` praxia workflow template** — a DAG template in `praxia/agent_assets/workflows/` that sequences: recon → classify → plan-review → apply → lint-verify.

6. **`bth migrate --classify` alias** — optional; adds `--classify` flag to the existing `bth migrate` command for discoverability, delegating to the same logic as `bth classify`.

### Out of Scope

- Historical SLURM log ingestion (#135).
- Automated sidecar _completion_ (generating the hypothesis, outcome conditions, etc.) — scaffolding only produces structural stubs with TODO markers; content authorship remains human or agent-delegated.
- Cross-project batch classification (classifying multiple projects in a single command invocation) — each run is per-project from the project root.
- Renaming scripts that are already inside a subdirectory but have wrong naming conventions — that is a `bth lint --fix` concern, which does not yet exist.
- Non-Python files (shell scripts, notebooks) — classified only if they match an extension in `_DIR_RULES` (currently `.py` and `.slurm`).

---

## Classification Heuristics

The classification engine must map filenames (and optionally file content) to a target directory from `_DIR_RULES`. This is the most design-sensitive part of the spec. Two approaches are available:

### Option A: Filename-Pattern Heuristics (Pure Lexical)

Map filename prefixes and characteristic words to directories using a priority-ordered rule table:

| Filename pattern | Target directory | Confidence |
|---|---|---|
| `benchmark_*`, `bench_*` | `benchmarks` | HIGH |
| `debug_*`, `diagnose_*` | `debug` | HIGH |
| `validate_*`, `verify_*` | `validation` | HIGH |
| `analyze_*`, `analyse_*` | `analysis` | HIGH |
| `profile_*` | `analysis` | MEDIUM (could be benchmarks) |
| `simulate_*`, `run_*` | `experiments` | MEDIUM |
| `generate_*`, `export_*`, `convert_*`, `extract_*` | `data` | MEDIUM |
| `visualize_*`, `inspect_*` | `analysis` | MEDIUM |
| `smoke_*`, `check_*`, `test_*` | `validation` | MEDIUM |
| `ablation_*` | `experiments` | MEDIUM |
| `compare_*` | `experiments` or `analysis` | LOW (ambiguous) |
| `phase*`, `update_*`, `write_*`, `sync_*` | `analysis` | LOW |
| Anything else | `analysis` | LOW (safe default) |

Confidence levels: HIGH = rule is highly predictive, MEDIUM = likely correct but requires review, LOW = educated guess; user/agent review required before applying.

**Tradeoff:** Purely lexical — fast, deterministic, testable, no LLM dependency. Misclassifications are possible for ambiguous names like `compare_minimizers.py` (is this an experiment or analysis?).

### Option B: Content-Augmented Heuristics

Read the first N lines (up to 50) of each flat script and look for structural signals:

- Contains a `bth.experiment` decorator call → `experiments`
- Contains `assert` statements that compare against a reference value → `validation`
- Contains timing/benchmarking code (`timeit`, `time.perf_counter`, "ns/day") → `benchmarks`
- Contains `plt.savefig`, `matplotlib`, `py3dmol` → `analysis` or `data`

**Tradeoff:** More accurate for ambiguous cases. Adds file-read I/O. Still not a full AST analysis. Introduces the possibility of brittle pattern matching on import names.

**Recommended approach:** Option A as the primary engine, with Option B content signals as a tiebreaker when Option A yields LOW confidence. The implementation should make it easy to add new filename-pattern rules without touching the content-analysis path.

### Naming Convention Enforcement

After classification, the engine must check whether the current filename stem matches the target directory's naming convention:

- `experiments/`, `benchmarks/`, `validation/`, `analysis/`, `data/` → `verb_noun` (regex `^[a-z][a-z0-9]*_[a-z][a-z0-9_]*$`)
- `debug/`, `explore/`, `scratch/` → `YYMMDD_desc` (regex `^\d{6}_[a-z][a-z0-9_]*$`)

For `benchmark_efa_vs_pme.py`:
- Target: `benchmarks/`
- Current stem: `benchmark_efa_vs_pme` — matches `verb_noun` → no rename needed.

For `diagnose_fire_nan.py` classified to `debug/`:
- Current stem: `diagnose_fire_nan` — does **not** match `YYMMDD_desc`
- Required rename: `scripts/debug/YYMMDD_diagnose_fire_nan.py` (where YYMMDD is inferred from git log `--follow --diff-filter=A` for the file)

This date-inference step is important: the `YYMMDD` prefix for debug/explore/scratch files must come from the file's first git commit date, not the current date — consistent with the internal docs convention already in use.

---

## Data Structures

```python
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

class ClassificationConfidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

@dataclass
class ClassificationResult:
    source: Path                    # e.g. scripts/benchmark_efa_vs_pme.py
    target_dir: str                 # e.g. "benchmarks"
    confidence: ClassificationConfidence
    rationale: str                  # Human-readable explanation
    rename_required: bool           # True if stem doesn't match target naming convention
    suggested_stem: str | None      # New stem if rename required (e.g. "260526_diagnose_fire_nan")
    sidecar_required: bool          # True if target dir has sidecar=ERROR
    sidecar_path: Path | None       # Where the sidecar would live post-move
    existing_sidecar: Path | None   # Adjacent .bth.toml if one already exists at source

@dataclass
class MoveAction:
    source: Path
    destination: Path               # Full path including new stem
    classification: ClassificationResult
    conflict: bool                  # True if a file already exists at destination
    conflict_path: Path | None

@dataclass
class ClassifyPlanResult:
    project_root: Path
    actions: list[MoveAction]
    skipped: list[tuple[Path, str]]  # (path, reason) for files not classified
    high_confidence: int
    medium_confidence: int
    low_confidence: int
    conflicts: int
    sidecars_to_scaffold: int
    dry_run: bool
```

---

## New Module: `classifier.py`

A new module `src/bathos/classifier.py` will house all classification logic. This keeps `migrate.py` focused on catalog-level concerns and avoids conflating script-filesystem migration with catalog-schema migration.

Key public functions:

```python
def classify_flat_scripts(
    project_root: Path,
    min_confidence: ClassificationConfidence = ClassificationConfidence.LOW,
    content_augment: bool = True,
) -> list[ClassificationResult]:
    """Scan scripts/ root for flat .py files and classify each into a target subdir."""
    ...

def build_move_plan(
    results: list[ClassificationResult],
    project_root: Path,
) -> ClassifyPlanResult:
    """Validate classifications, check for conflicts, return MoveAction list."""
    ...

def apply_move_plan(plan: ClassifyPlanResult, scaffold_sidecars: bool = True) -> None:
    """Execute git mv commands and optionally write sidecar stubs."""
    ...
```

`apply_move_plan` uses `subprocess.run(["git", "mv", str(src), str(dst)])` per action — not `Path.rename()` — to preserve git history.

---

## CLI Surface

### Primary: `bth classify`

```
bth classify [OPTIONS]

  Classify flat scripts into the correct scripts/ subdirectory.

  Scans scripts/ root for .py files not already in a subdirectory,
  infers the correct target directory, and prints a git mv plan.
  Apply the plan with --apply.

Options:
  --min-confidence [high|medium|low]
                                  Only include classifications at or above
                                  this confidence level.  [default: low]
  --no-content                    Skip content-augmented classification
                                  (filename patterns only).
  --no-scaffold                   Do not scaffold sidecar stubs when applying.
  --apply                         Execute git mv commands and write sidecars.
                                  Without this flag, dry-run only.
  --project PATH                  Project root. Defaults to cwd.
  --json                          Output as JSON (machine-readable).
  -h, --help                      Show this message and exit.
```

Default output (dry-run, table):

```
bth classify
Scanning scripts/ for unclassified files...

  Source                             Target                                Confidence  Rename  Sidecar
  scripts/benchmark_efa_vs_pme.py    scripts/benchmarks/                   HIGH        no      scaffold
  scripts/debug_bonds.py             scripts/debug/260121_debug_bonds.py   HIGH        yes     no
  scripts/simulate_1uao_explicit.py  scripts/experiments/                  MEDIUM      no      scaffold
  scripts/compare_minimizers.py      scripts/analysis/                     LOW         no      no
  ...

  52 scripts  |  8 HIGH 24 MEDIUM 20 LOW  |  3 conflicts  |  12 sidecars to scaffold
  Run with --apply to execute.
```

Conflicts (destination file already exists) block the apply and are shown prominently. The user must resolve them manually before `--apply` can proceed.

### Alias: `bth migrate --classify`

For discoverability, `bth migrate` gains a `--classify` flag that delegates to the same logic. This is optional and can be a thin wrapper:

```python
@app.command()
def migrate(
    ...
    classify: bool = typer.Option(False, "--classify", help="Classify flat scripts into subdirs (Phase 2)."),
    ...
):
```

### Rationale for Separate `bth classify` Command

`bth migrate` currently handles two catalog-level operations (`migrate_catalog`, `migrate_to_project_subdirs`). Mixing a git-touching, script-filesystem operation into the same command creates a conceptual mismatch: `bth migrate` is about upgrading catalog artifacts, not reorganizing source files. A separate `bth classify` command is semantically cleaner, easier to document, and avoids surprising users who run `bth migrate` expecting only catalog changes.

The `--classify` alias on `bth migrate` exists purely for discoverability (users who associate "migration" with this workflow), but `bth classify` is the canonical command.

---

## Sidecar Scaffolding

When `--apply` is used and `scaffold_sidecars=True`, for each script classified into `experiments/` or `benchmarks/`, `apply_move_plan` writes a stub sidecar at the destination:

**Experiment scaffold:**
```toml
# AUTO-GENERATED by bth classify -- fill in all TODO fields before running bth run
[experiment]
hypothesis = "TODO: clear, falsifiable statement"

[outcomes.pass]
condition = "TODO: DuckDB SQL fragment e.g. metric < 0.01"
decision = "TODO: next step if hypothesis confirmed"
is_residual = false

[outcomes.fail]
condition = "TODO: DuckDB SQL fragment"
decision = "TODO: root cause step"
is_residual = false

[outcomes.residual]
condition = "true"
decision = "TODO: marginal/inconclusive disposition"
is_residual = true

[result_schema]
# TODO: add typed output fields e.g. metric_name = "float"
```

**Benchmark scaffold:**
```toml
# AUTO-GENERATED by bth classify -- fill in all TODO fields before running bth run
[benchmark]
baseline_ref = "TODO: run_uuid of reference run"
metric = "TODO: e.g. ns_per_day"
regression_threshold = 0.05
target = "TODO: qualitative goal e.g. >50 ns/day on pi_so3"

[result_schema]
# TODO: add typed output fields e.g. ns_per_day = "float"
```

Scripts classified into `validation/` receive a scaffold only if `sidecar = IssueSeverity.WARNING` fires (i.e., the directory rule requires a warning-level sidecar). That scaffold uses the `[validation]` schema.

`debug/`, `explore/`, `scratch/`, `analysis/`, and `data/` do not receive scaffolds — `_DIR_RULES` has `sidecar = None` for those.

---

## praxia Workflow Template: `bth-migrate.yaml`

A new workflow YAML artifact lives in `praxia/agent_assets/workflows/bth-migrate.yaml`. It describes the DAG for a human-supervised agentic classification sprint.

The nodes in order are:

1. **recon_scripts** (role: recon) — Map all flat scripts at `scripts/*.py`. Extract filename stems and infer domain from content. Produce a candidate classification table.
2. **classify_and_plan** (role: fixer) — Run `bth classify --json` to get the machine-generated plan. For any LOW-confidence or rename-required entries, apply domain knowledge to confirm or override. Produce a final MoveAction list with all conflicts resolved. Do not apply yet.
3. **human_plan_review** (role: oracle) — Present the plan table to the user for approval. All LOW-confidence items and renames must be explicitly confirmed. Gate: advance only when user signs off.
4. **apply_plan** (role: fixer) — Run `bth classify --apply`. Commit as a standalone commit ("chore(scripts): classify flat scripts into taxonomy subdirs"). Do NOT mix sidecar stubs into the same commit as git mv operations.
5. **lint_verify** (role: plan-auditor) — Run `bth lint`. Confirm zero ERROR-severity issues for moved files. WARNING issues for missing sidecars are acceptable if `--no-scaffold` was used.

**Edge logic:** recon_scripts can loop; classify_and_plan can loop on unresolved conflicts; human_plan_review can revert to classify_and_plan if plan needs revision; lint_verify can revert to apply_plan if verification fails.

**Placement:** `praxia/agent_assets/workflows/bth-migrate.yaml` — not in the bathos repo.

---

## Key Design Decisions

### Decision 1: `bth classify` vs. `bth migrate --classify`

**Choice:** Separate `bth classify` command is canonical; `bth migrate --classify` is a thin alias.

**Rationale:** `bth migrate` operates on the catalog (Parquet fragments, DuckDB). `bth classify` operates on source files and git history. These are different concerns with different failure modes. A user running `bth migrate` to upgrade Parquet schema fragments should not accidentally move their scripts. Separation makes `--dry-run` semantics unambiguous.

**Trade-off:** Two entry points to maintain. Mitigated by making the alias a 3-line wrapper.

### Decision 2: Filename-Pattern-First, Content-Augmented as Tiebreaker

**Choice:** Option A (filename patterns) as primary, Option B (content signals) as tiebreaker for LOW confidence.

**Rationale:** The prolix script naming is highly convention-following (e.g., `benchmark_efa_vs_pme.py` is unambiguously a benchmark). Content analysis adds complexity and I/O overhead for diminishing returns on a well-named corpus. For genuinely ambiguous cases (`compare_minimizers.py`), a LOW-confidence classification with a human review step is more honest than an overconfident content-based heuristic.

**Trade-off:** Will misclassify edge cases like `phase1_diagnostic_runner.py` (starts with `phase`, not a recognized verb). These surface as LOW confidence and land in `analysis/` by default — a safe fallback.

### Decision 3: Date Inference for debug/explore/scratch Renames

**Choice:** Use `git log --follow --diff-filter=A --format=%ai -- <file>` to infer the `YYMMDD` prefix for files that need renaming to match the `YYMMDD_desc` convention.

**Rationale:** The internal docs convention already uses this approach. Using today's date would be actively misleading.

**Trade-off:** Files not yet committed to git have no first-commit date. Fallback: use file mtime (`stat -c %Y`). If neither is available, emit a LOW-confidence result with `rename_required=True` and `suggested_stem=None`, requiring manual resolution.

### Decision 4: Conflicts Block Apply, Not Warn

**Choice:** If `destination` already exists, the `MoveAction` is marked `conflict=True` and `--apply` refuses to proceed until all conflicts are resolved.

**Rationale:** Silently overwriting an existing file in a sibling directory would be silent data loss. A conflict means there is a genuine design question (same logical thing split into two scripts?) that requires human judgment. A hard block is the right posture.

**Trade-off:** More friction when conflicts exist. Mitigated by: (a) the dry-run output shows conflicts prominently; (b) `--apply` with no conflicts is one command.

### Decision 5: Sidecar Scaffolds Are Stubs, Not Filled-In

**Choice:** Generated sidecars contain `TODO` placeholders, not inferred hypothesis text or outcome conditions.

**Rationale:** Generating plausible-sounding hypotheses from a filename is an LLM hallucination risk. The goal of the sidecar system is pre-registration integrity — a pre-filled hypothesis that the researcher did not write defeats this purpose entirely. Better to make the gap obvious (TODOs) than to pollute the catalog with synthetic pre-registrations.

**Trade-off:** More work for the researcher post-classification. Acceptable: batch scaffolding of 12 stubs still saves significant effort vs. creating them from scratch. The linter will flag unfilled TODO fields once a Tier-2 lint rule for TODO detection is added.

### Decision 6: Single Project Per Invocation

**Choice:** `bth classify` operates on the project in the current working directory.

**Rationale:** `_DIR_RULES` is universal across projects; the `scripts/` structure is per-project. Multi-project batch runs would require looping at the shell level, which is appropriate — it lets the user review the plan for one project before proceeding to the next.

**Trade-off:** More steps for users with multiple projects to migrate. Mitigated by: the workflow YAML makes the loop explicit.

---

## Implementation Plan

### Phase 1: Core Classifier Module (bathos)

Files to create/modify:
- `src/bathos/classifier.py` (new) — `classify_flat_scripts`, `build_move_plan`, `apply_move_plan`, all dataclasses
- `src/bathos/cli.py` — add `bth classify` command and `--classify` flag on `bth migrate`
- `tests/test_classifier.py` (new)

Test cases required:
- `test_classify_benchmark_prefix` — `benchmark_*.py` → `benchmarks/`, HIGH confidence, no rename
- `test_classify_debug_prefix` — `debug_*.py` → `debug/`, HIGH confidence, rename required (YYMMDD inference)
- `test_classify_validate_prefix` — `validate_*.py` → `validation/`, HIGH confidence, no rename
- `test_classify_ambiguous_compare` — `compare_*.py` → `analysis/` or `experiments/`, LOW confidence
- `test_conflict_detection` — existing file at destination → `conflict=True`, apply raises
- `test_rename_uses_git_date` — mock git log output → correct YYMMDD prefix extracted
- `test_rename_fallback_to_mtime` — uncommitted file → mtime used as fallback
- `test_apply_dry_run_noop` — `apply_move_plan` with `dry_run=True` makes no filesystem changes
- `test_sidecar_scaffold_written` — experiment-classified script gets stub sidecar on apply
- `test_no_scaffold_for_analysis` — analysis-classified script gets no sidecar on apply
- `test_already_in_subdir_skipped` — script already under `scripts/experiments/` not included in flat scan
- `test_json_output_roundtrip` — `--json` output parses cleanly as `ClassifyPlanResult`

### Phase 2: praxia Workflow YAML

File to create:
- `praxia/agent_assets/workflows/bth-migrate.yaml`

No bathos code changes required for this phase. The workflow YAML is consumed by the praxia orchestration layer, not by bathos itself.

### Phase 3: Documentation Updates

- `CLAUDE.md` backlog: mark #136 as in-progress once implementation begins; update depends_on to remove #135 dependency
- `.praxia/docs/INDEX.md`: add this spec entry
- `CLAUDE.md` command table: add `bth classify` to the CLI reference

---

## Open Questions

The following questions need resolution before implementation can begin:

1. **Content-augment threshold:** For the content-augmented tiebreaker, what signals are definitive enough to override a filename-pattern LOW classification? Is "contains `time.perf_counter`" enough to promote `compare_minimizers.py` to `benchmarks/`, or does this risk false positives? The implementer needs a firm policy, not a best-effort judgment.

2. **Ambiguous `validate_*` vs `verify_*` vs `check_*`:** The prolix corpus has `validate_adaptive_rattle_temperature.py`, `verify_openmm_physics.py`, `verify_pme_parity.py`, and `check_py2dmol.py`. Should `verify_*` map to `validation/` (sidecar required at WARNING level) or to `analysis/` (no sidecar)? Is the decision purely lexical (all `verify_*` → `validation/`) or do we need a size/content gate?

3. **`simulate_*` classification:** The prolix corpus has 5 `simulate_*.py` scripts. The natural mapping is `experiments/` (they test a hypothesis about a physical system). But `experiments/` requires a sidecar at ERROR level, and writing hypothesis pre-registrations for simulation scripts is significant authorship work. Should `simulate_*` map to `experiments/` (correct semantically, high friction) or `analysis/` (lower friction, semantically imprecise)? This decision determines how many sidecar stubs the classify step generates for prolix.

4. **`phase1_*` and composite-name scripts:** `phase1_diagnostic_runner.py` and `phase1_plotting_and_verdict.py` do not match any prefix heuristic. They would fall to LOW confidence and be routed to `analysis/`. Is that correct, or do they belong in `debug/`? Should "phase" be a recognized classification hint?

5. **`__init__.py` handling:** `scripts/__init__.py` is present in prolix, making `scripts/` a Python package. Should `bth classify` skip files starting with `_` unconditionally (consistent with the linter), or emit a WARNING that `__init__.py` presence may complicate `git mv` (removing it breaks import paths if anything imports from `scripts` as a package)?

6. **Uncommitted files and git mv:** If a flat script has never been committed to git (untracked state), `git mv` will fail. Should `bth classify --apply` detect untracked files and use `os.rename` instead, or require the user to commit/stage first? The latter (hard block on untracked files) is safer from a git-history-preservation standpoint but is more friction.

7. **`--apply` atomicity:** Should all `git mv` operations be pre-validated before any are executed, aborting the entire apply if any would fail? If one `git mv` fails mid-sequence, the working tree is in a partially-migrated state. Options: (a) pre-validate all moves before executing any; (b) run `git mv` for each and abort on first failure with a recovery hint; (c) stage in a temporary branch. This needs a firm decision before implementation.

8. **Sidecar scaffold TODO lint rule:** Without a Tier-2 lint rule that flags `TODO` strings in sidecar hypothesis and outcome fields, scaffolded stubs will silently pass `bth lint` even though they are unfilled. Should this lint rule be in #136 scope, or tracked as a separate backlog item?

9. **Workflow YAML placement and capability declaration:** The `bth-migrate.yaml` workflow lives in the praxia repo. Does the praxia plugin system need a new capability declaration (e.g., a `[capabilities.bth_migrate]` section in `manifest.toml`) so the orchestrator knows this workflow is applicable to bathos projects, or is the `trigger_predicates.keywords` field sufficient?

10. **Confirm preferred CLI surface:** This spec recommends a separate `bth classify` command with a `--classify` alias on `bth migrate`. If the user prefers the alias pattern exclusively (no separate command), the implementation changes slightly. Confirm preferred surface before implementation.

---

## Acceptance Criteria

Implementation is complete when all of the following are true:

1. `bth classify` (dry-run) scans a project with flat scripts and produces a table with source, target dir, confidence level, and rename status for every flat `.py` file.
2. `bth classify --apply` executes `git mv` for all non-conflicting HIGH and MEDIUM confidence moves; writes sidecar stubs for scripts moved into `experiments/` or `benchmarks/`.
3. After `--apply`, `bth lint` on the project produces zero ERROR-severity issues for the moved files.
4. Conflicts are detected pre-apply and reported clearly; `--apply` refuses to proceed until conflicts are resolved.
5. Date inference for `debug/` renames uses `git log --follow --diff-filter=A` and falls back to mtime.
6. All 12+ test cases listed above pass under `uv run pytest`.
7. `bth-migrate.yaml` workflow YAML is present in `praxia/agent_assets/workflows/` and passes YAML lint.
8. `CLAUDE.md` backlog entry for #136 is updated with implementation status and #135 dependency cleared.

---

## Risk Assessment

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| `git mv` fails on untracked files | MEDIUM | MEDIUM | Pre-flight check; hard block with clear error message |
| Misclassification of ambiguous scripts (LOW confidence) | HIGH | LOW | Dry-run output and human review step in workflow; conflicts with existing files are blocked |
| Sidecar stubs silently pass lint without content | HIGH | MEDIUM | Add TODO-detection lint rule (Open Question 8); flag explicitly in scaffold output |
| `simulate_*` to `experiments/` forces large sidecar authorship burden | MEDIUM | HIGH | Resolve via Open Question 3 before implementation; if `analysis/` is chosen, note as a deliberate semantic compromise |
| Partial apply leaves working tree in mixed state | LOW | HIGH | Pre-validate all moves before executing any; abort on first `git mv` failure |

---

**Status:** Ready for user review. Open Questions 1-10 must be resolved before dispatching an implementation agent.
