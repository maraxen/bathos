# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- **`[confounds.synthetic_recovery]` claim-tier gate (BP-2)** — ports asr's C1 pre-run
  synthetic-invariant gate into a native, project-agnostic bathos primitive. New `bathos.gate`
  module: a self-attested pass/fail ledger (`.bth/synthetic_recovery_ledger.json`, `bth gate
  stamp <name> --result pass|fail`) plus a GREEN/STALE/RED/UNKNOWN staleness state machine
  (`bth gate status`) keyed on whether a gate's declared guarded source paths changed since the
  last recorded pass (`git.paths_changed_since`). Wired into `bth claim validate` (diagnostic),
  `bth claim register` (advisory warning), and `bth campaign conclude` (hard downgrade to
  `'confounded'` for confirmation/sequential campaigns, warning-only for exploration — same
  pattern as the existing `[confounds.reference_parity]` check). See
  `.praxia/docs/decisions/260721_bp2-bp3-claim-tier-gate-ports.md`.
- **`campaigns.negative_check` + `bth campaign conclude --negative-check` (BP-3)** — ports asr's
  C5 negative-claim falsification check as a structured attestation field (not a regex-heuristic
  port, per the asr design doc's own stated preference). A registered claim's `conclude` call with
  a negative-sounding `--outcome` (configurable vocabulary, default ported from asr's C5 wordlist,
  overridable via `.bth.toml [claim] negative_outcome_pattern`) now requires a non-blank
  `--negative-check` backing/hedge, mirroring Union Gate's opt-in-on-claim-registration adoption
  ladder — campaigns with no claim attached are unaffected.

---

## [0.13.0a1] - 2026-07-17

**Alpha pre-release.** Ships work merged since 0.12.0 that had accumulated without a
version bump; see the README's alpha notice — APIs here may still change without a
deprecation period.

### Added

- **Statistical battery + baseline-budget gates (B2-01, `stats_gates.py`)** — `run_stats_battery`
  (Wilcoxon/Friedman+Nemenyi, alpha=0.05 Holm; Cohen's d, win-rate, breakdown-point, ICC) and
  `check_baseline_budget_equivalence`, plus `wilcoxon_signed_rank`/`win_rate`/`breakdown_point`
  primitives.
- **`Run.seed` + HPO budget schema (B2-02)** — `Run.seed`, `Run.baseline_hpo_trials`,
  `Run.baseline_hpo_compute_budget` columns, and `campaigns.count_seeds_for_script`/
  `count_runs_for_script` conclude-time counting helpers (feed the seed/trial power floor).
- **Capability probe MCP endpoint (B2-06)** — liveness check for `Run.seed` + stats-battery
  availability, for callers gating on capability before dispatch.
- **Multi-parent campaign & run DAG + PROV emission (B2-03, `campaign_edges.py`)**.
- **Sidecar drift detection (B2-04)** — promotes `SIDECAR_HASH_MISMATCH` to a first-class check.
- **`stdout_sha256` + self-signed manifest verification (B2-07)**.
- **Bathos-side component-level sidecar binding (B2-08)** — cross-repo bridge support for
  component-granular attestation.
- **Bathos declared as a readback capability provider** (plugin registration).
- **S1-S7 readback/anchor/attestation/figure-registry/harness series** — read-back/query API
  (`resolve_pin`, campaign/figure sidecar readers), generic sidecar anchor insert-by-`(path,
  sha256)`, attestation sidecar kind + verdict-aware query, harness-as-bathos-run wrapper for
  `port/` T1-T5, typed pointer-only `figure_entry` schema, S3 durable (S3-backed) trust ledger
  with ratchet-enforced graduation, C3 concentration-alarm catalog-backed lint check.
- **`bth claim` subcommands** closing CLI/MCP parity gaps; env-gated cisterna telemetry cutover.

### Fixed

- Shared-secret token auth on write-verb MCP tools (debt #619).
- `validate_attestation` now enforced at register time (debt #638).
- Three audit debts closed in readback/attestation/trust-ledger (#639, #640, #645).
- Five cross-project bathos debt items resolved (#478, #477, #491, #479, #485/#487/#369).

---

## [0.12.0] - 2026-06-23

### Added

- **Claim-tier rigor** — pre-registered `claim.bth.toml` workflow for confirmation campaigns:
  - **`bth claim scaffold/register/validate`** — campaign-linked claim files with SHA integrity at register/conclude
  - **Union Gate** at `bth campaign conclude` — clause-coverage check; soft-block downgrade to `confounded` for confirmation/sequential modes
  - **`claim_coverage_<id>.json` sidecar** (AC-12) — emitted after conclude with covered/uncovered clause lists
  - **Heuristic discriminability lints** (AC-04/05/06) — zero-power, positive-testing-bias, and single-cell-gate advisories in `validate_claim` / `linter.py`
  - **AC-13 `[baseline_parity]` confound lint** — baseline admissibility and equivalence-bound checks
  - **Signal 12** in sprint-audit — flags confirmation campaigns with missing/unregistered claims
  - **Schema v8** — `claim_discriminates` and `claim_isolates` columns on warm tier (compact INSERT fix verified in #2276)
- **Literature-parity v1** — reusable cross-project baseline validation workflow (epic #2214):
  - **`parity.bth.toml` schema** + validator; **X1 cap-lattice grader** (`compute_grade`)
  - **`parity_validate.py`** runner + companion sidecar; **`attest_parity()`** atomic claim binding with R2 rollback (AC-21)
  - **`parity_confound_check()`** — infers `controlled` / `controlled-by-protocol` / `uncontrolled` from `parity_run_type` column + run outcome
  - **F2 conclude-gate** — downgrades confirmation verdict when `reference_parity` is uncontrolled
  - **F3 submit-gate** — `check_parity_confounds_for_submit()` for sidecar-declared parity prerequisites
  - **`bth campaign attest-parity`** + MCP **`claim_attest_parity`** (T8)
  - **Signal 13** — flags uncontrolled `reference_parity` on confirmation campaigns with registered claims (T9)
  - **Schema v9** — `parity_run_type` promoted to first-class COOL column (survives cool→warm compaction)
  - **Skill section** — literature-parity protocol in `/using-bathos`
- **AC-20 SHA-drift detection** (`#2580`) — `check_output_sha_drift()` compares catalog `output_metadata` SHA256 to on-disk files; `parity_confound_check()` downgrades controlled parity on drift; `bth check --check-outputs` reports drift and exits non-zero
- **Structured MCP error taxonomy** (#793) — `BathosErrorCode`, `traced_tool` catch-and-shape envelope, `tests/test_mcp_envelope.py`

### Fixed

- **`claim_discriminates` / `claim_isolates` NULL-on-ingest** in compact INSERT (#2276)
- **Parity gate remediation (B1)** — graded-path vs legacy equivalence-bound routing; `validate_claim` and conclude/submit gates aligned
- **`rich_fmt` test stability** — assert cell values instead of Rich-truncated column headers (debt #237)

### Notes

- Total suite: 908 tests passing. Literature-parity spec: `.praxia/docs/specs/260618_literature-parity-v1-design.md`. Claim-tier spec: `.praxia/docs/specs/260616_bathos-claim-tier-rigor-open-design-call.md`.

---

### Added

- **Experimental controls discipline** — enforcement of scientific controls at the sidecar, lint, submit, and sprint-audit levels:
  - **`stage_name` write path** — parsed from `[experiment].stage_name`, written to `Run` record at `runner.py`; non-canonical values coerced to `exploration`
  - **`[reproduction]` sidecar block** — `ReproductionBlock` dataclass with `tolerance_pct`, `reproduces_run`, and `requires_pass_stem` fields; structural validation in `validate.py`
  - **`[controls]` sidecar block** — `ControlsBlock` dataclass; `positive_outcome`/`negative_outcome` labels cross-validated against `[outcomes]` keys
  - **Reproduction prerequisite gate** (`bth submit`) — hard exit `REPRODUCTION_PREREQUISITE_UNMET` for `validation`/`production` stage experiments when a required predecessor run with `outcome=pass` is not found; advisory WARNING (no exit) for `exploration`/`calibration` stage
  - **`control_arm_rate` sprint-audit signal** (Signal 9) — flags campaigns at `validation`/`production` stage with zero `ctrl_*` outcome runs
  - **`baseline_ref` Tier-2 lint check** — queries warm-tier for referenced benchmark run UUID; emits WARNING if not found
  - **Novel/reproduces Tier-1 lint enforcement** — `ERROR` for `validation`/`production` sidecars missing `[reproduction]` block or `novel=true`
  - **`bth new-experiment` scaffold fix** — generated sidecar now passes `validate_sidecar` out of the box (fixed `reasoning`, `is_residual`, SQL conditions, `result_schema`, `stage_name`, `novel`, commented `[reproduction]`/`[controls]`)
  - **Submit-provenance Parquet** — atomic write to `submits/<slug>/` at `bth submit` time; accumulates all submission metadata
  - **`signal_submit_bypass_rate`** sprint-audit Signal 10 — fraction of campaigns where `bth submit` was bypassed (no matching provenance record)
- **Worktree-aware workspace resolution** (`src/bathos/workspace.py`). New `resolve_workspace(cwd)` returns a `WorkspaceContext` separating stable catalog **identity** (project slug, recorded `[project] root`) from the live **filesystem root** (`fs_root`) used for workspace-relative file operations. When `bth` runs from inside a git worktree, postmortem asset validation/scan now resolve against the live worktree checkout instead of the recorded main-checkout root. `fs_root` precedence: `BTH_WORKSPACE_ROOT` (new env var, must be absolute) → `git rev-parse --show-toplevel` → recorded `[project] root` → `cwd`. No schema change. Spec: `.praxia/docs/specs/260611_worktree-workspace-resolution.md`.
- **`BTH_WORKSPACE_ROOT`** env override, mirroring `BTH_PROJECT_SLUG`/`BTH_CATALOG_DIR`; now exported by the SLURM env helper (`templates/_bth_env.sh`) so cluster jobs in spool dirs resolve deterministically.

### Changed

- The MCP postmortem mirrors (`postmortem_template`, `postmortem_validate`, `postmortem_get`) now honor an explicit `workspace_root` argument as **top precedence**. Previously a discoverable `.bth.toml` recorded root silently overrode the caller-supplied `workspace_root`.

### Notes

- Total suite: 753 tests passing (4 skipped). Controls discipline spec: `.praxia/docs/specs/260612_experimental-controls-discipline-for-in.md`. Workspace resolution spec: `.praxia/docs/specs/260611_worktree-workspace-resolution.md`.

---

## [0.10.0] - 2026-06-08

### Added

- **`bth repair`** — catalog corruption scanner and repairer across all storage tiers. Runs in dry-run mode by default (`--dry-run`); pass `--apply` to execute. Flags: `--tier {cool,warm,archive,all}` (default `all`), `--from-warm` (detect runs present in the warm DB but missing from cool fragments), `--acknowledge-warm-loss` (required gate before a warm-DB rebuild that would destroy postmortem annotations or `output_metadata`).
  - **Sentinel cleanup** — removes stale write-in-progress sentinel files left by interrupted cool-tier writes.
  - **Corrupt-fragment quarantine** — unreadable cool Parquet fragments are moved aside to a quarantine directory instead of blocking compaction.
  - **Warm backup + loss gate** — before a `force_rebuild`, `bathos.db` is backed up to `bathos.db.bak-<timestamp>` (rotation keeps the most recent 3); the rebuild is gated behind `--acknowledge-warm-loss` whenever warm-only data is at risk. An unreadable warm DB is treated as data-at-risk and triggers the gate (`SystemExit(1)`) rather than silently proceeding.
  - **Warm→cool re-export** — `--from-warm` reconstructs missing cool fragments from warm-DB rows.
  - **Archive-tier quarantine** — archive partitions failing SHA256 or row-count verification are quarantined and recorded in the archive `manifest.json`.
- **MCP mirrors** — `repair_scan` (read-only scan) and `repair` (apply) tools in `mcp.py`, mirroring the CLI.
- **Test suite** — `tests/test_repair.py`, `tests/test_gwt1112_review.py`, and `tests/test_repair_archive_quarantine.py` covering all repair tracks (sentinel, quarantine, warm-loss gate, re-export, archive verification).

### Notes

- Supersedes the undocumented `0.9.1` tag, whose commits were an earlier parallel pass of the repair module now folded into this release. Total suite: 620 tests passing.

---

## [0.9.0] - 2026-06-04

### Added

- **`bth outputs list <run_id> [--live]`** — per-file listing of registered output artifacts (path, status, size, sha256) from the compact-time snapshot; `--live` re-stats from filesystem without writing back to catalog
- **`bth outputs summary [--project <slug>] [--since <period>]`** — aggregate output file counts, total bytes, and missing-file rate across all projects (or filtered); requires warm catalog (`bth compact` first)
- **`render_output_list` / `render_outputs_summary`** in `rich_fmt.py` — Rich-formatted output tables for the new commands
- **MCP mirrors** — `list_outputs(run_id, live=False)` and `outputs_summary(project=None, since=None)` in `mcp.py`
- **Scaffold two-phase comment** in `bth new-experiment` template — documents scalar-metrics (stdout JSON) vs. artifact-files (`--out` / `bth outputs`) distinction
- **`docs` optional extra** in `pyproject.toml` — `pip install bathos[docs]` installs Sphinx for building documentation; required by Read the Docs

### Fixed

- **`output_metadata` key inconsistency** in `query.py` — `_filter_runs_by_output_file` was checking `metadata.output_files` as a warm-tier fallback key; corrected to `output_metadata`
- **`.readthedocs.yaml` invalid key** — removed `python.version` (not valid in RTD config v2; version already specified under `build.tools.python`)
- **`docs/source/conf.py` stale release string** — updated from `0.1.0` to `0.9.0`
- **`pyproject.toml` stale version** — bumped from `0.7.0` to `0.9.0` to match CHANGELOG

### Notes

- Output metadata is a point-in-time snapshot captured once at first compact. Use `--live` for current filesystem state. Refresh-on-compact with change history is tracked as debt #71.

---

## [0.8.0] - 2026-06-02

### Added

- **POPPER sequential campaigns** — `mode="sequential"` for `bth campaign create --sequential`; converts Campaign into an anytime-valid sequential test based on e-value products (arXiv 2502.09858)
- **`[popper]` sidecar block** — `null_pass_rate`, `alt_pass_rate`, `stopping_threshold` (all required); optional `[popper.weights]` per-label likelihood-ratio overrides
- **`compute_evalue()`** in `sidecar.py` — likelihood-ratio e-value per run: alt/null for pass-direction, (1-alt)/(1-null) for fail-direction, 1.0 hard default for marginal/error/unknown
- **`campaign_runs` schema extension** — `evalue REAL CHECK (evalue IS NULL OR evalue > 0)`, `seq_position INTEGER`
- **`campaigns` schema extension** — `stopping_threshold REAL`; schema v6
- **Threshold lock** — `stopping_threshold` locks after the first non-error run is added; `CampaignError` on mismatch with restart-via-parent instructions
- **`bth campaign conclude --force` / `--abort-if-below-threshold`** — soft and strict premature-stopping guards
- **`bth campaign review` POPPER table** — per-script E_n product, n_effective, n_excluded, threshold_met via `render_popper_summary()`
- **Sprint-audit signal 8** — `premature_stopping_rate`: fraction of concluded sequential campaigns where final E_n < stopping_threshold
- **`check_popper_adversarial()`** — Tier-2 lint advisory for POPPER sidecars missing `adversarial_check` in all outcome branches
- **`validate_popper_block()`** — structural validation: range checks, null≠alt guard, threshold < 10.0 WARNING, weight key/value constraints

---

## [0.7.0] - 2026-06-01

### Added

- **`bth verify`** — catalog integrity command; `--tier cool/warm/archive/all`
- **`compact` transaction safety** — `BEGIN`/`COMMIT`/`ROLLBACK` wrapping; `PRAGMA integrity_check` on every DuckDB connect
- **`compact force_rebuild`** — removes existing `bathos.db` before compacting (corruption recovery)
- **Pre-migration `.bak` backup** — `bth migrate` writes a `.bak` before in-place rewrite
- **Archive SHA256 checksums** — per-file in `manifest.json`
- **`sync` truncation detection** — post-rsync check via `--itemize-changes`

### Changed

- **`fastmcp` promoted to main dependency**

---

## [0.6.1] - 2026-06-01

### Added

- **Sprint-audit threshold ADR** — domain rationale for all 7 signal thresholds; fixes `schema_overflow_rate` semantics
- **Tier-2 lint: `check_threshold_basis`** — warns for `regression_threshold` without `regression_threshold_basis`
- **`OutcomeSpec.source`** and **`Sidecar.regression_threshold_basis`** fields

---

## [0.6.0] - 2026-05-29

### Added

- **`outcome="error"` first-class** — `GateErrorCode` / `GateErrorPayload` taxonomy (11 codes)
- **Pre-execution manifest** — `.bth.lock.toml` schema v5; content-hashes sidecar + script at run start
- **`adversarial_check` field** — per-outcome text field; Tier-2 lint enforcement in `--agent-mode`
- **`bth cite`** — structured run citations (BibTeX / plain text)
- **`bth lineage --format prov`** — W3C PROV-JSON 1.0 lineage export
- **Sprint-audit 7-signal extension** — `error_rate`, `bypass_explicit`, `bypass_in_agent_mode`, `outcome_entropy`, `unfired_branches`, `schema_overflow_rate`, `post_hoc_bias_flag`
- **Cluster submission** — `bth submit` via myxcel; `slurm_array_task_id` field; `ClusterConfig`
- **Schema v5** — `manifest_sha256`, `manifest_path`, `outcome_error_reason`, `adversarial_check_status`

---

## [0.5.0] - 2026-05-25

### Added

- **Telemetry** — structured JSONL with 9 event surfaces; SLURM-safe per-process files; rides `bth sync`
- **`bth view`** — local FastAPI dashboard (`bathos[viz]`); read-only; 1000-run cap
- **`bth export --html`** — static HTML report from warm catalog
- **`bathos[viz]` optional extra** — FastAPI + Jinja2 + Alpine.js + Pico CSS (vendored MIT)
- **Rich formatters** (`rich_fmt.py`) — `render_runs_table`, `render_run_detail`, `render_campaign_table`, `render_campaign_review`

---

## [0.4.1] - 2026-05-21

### Added

- **Postmortem tracking** — `*.bth.postmortem.toml` format for capturing experiment retrospectives; tracked in git (not gitignored); supports `[postmortem]`, `[decisions]`, `[asset_links]`, and `[anomalies]` sections
- **`bth postmortem validate <file>`** — CLI command to validate a postmortem TOML file; checks refutation consistency, asset path containment, sha256 checksums, and git drift; exits non-zero on violations with structured error output
- **`postmortem_scaffold` MCP tool** — scaffolds a new `*.bth.postmortem.toml` with pre-populated sections from the associated run record
- **`postmortem_validate` MCP tool** — validates a postmortem file, returning structured violations for agentic workflows
- **`postmortem_get` MCP tool** — retrieves postmortem data for a run by run ID or file path
- **Schema v4** — 10 new DuckDB columns for postmortem metadata; `compact` syncs postmortem data via both INSERT and UPDATE paths
- **`script_sha256`** — SHA-256 hash of the script file computed in `runner.py` at experiment launch and stored in the run record

### Fixed

- **`catalog_dir` tilde not expanded** — `load_project_config` now calls `.expanduser()` on `root` and `catalog_dir` paths; previously `Path("~/projects/asr/.bth/catalog")` was treated as a relative path starting with a literal `~`, causing `bth compact` to report "Compacted 0 runs", `bth sql` to report "No warm catalog", and `bth find` to stall — all despite the catalog existing at the correct absolute path
- **`bth sync` indefinite hang** — `sync_catalog()` now passes SSH options (`ConnectTimeout=10`, `BatchMode=yes`) to rsync so unreachable hosts fail in ≤10 s instead of blocking forever; adds `subprocess.run(timeout=120)` as a safety net; raises `RuntimeError` with actionable message on timeout
- **Parquet timestamp timezone-aware** — timestamps loaded from Parquet are now always timezone-aware (UTC) to prevent `TypeError` when comparing with aware datetimes in query functions

---

## [0.4.0] - 2026-05-21

### Added

- **Per-project sync filtering** — `bth sync` now pushes/pulls only the current project's runs; output reports filtered count (e.g., `Pushed 47 runs (filtered 275 from other projects)`)
- **`bth migrate-to-project-subdirs`** — migrates flat cool-tier catalogs to the `runs/<slug>/run_<uuid>.parquet` layout
- **`sync_filter` config knob** — set `sync_filter = "none"` in `.bth.toml` to disable per-project filtering and sync all projects

### Changed

- **Cool-tier layout** — per-run Parquet fragments now stored at `runs/<slug>/run_<uuid>.parquet` instead of flat `runs/run_<uuid>.parquet`; old layout still readable, use `bth migrate-to-project-subdirs` to upgrade

---

## [0.3.0] - 2026-05-20

### Added

- **Agentic integrity gate layer** — `prereg.py` enforces sidecar presence and structural validity before any run in `scripts/experiments/`, `scripts/benchmarks/`, or `scripts/validation/`; structured JSON gate errors returned on failure with `gate_schema_version: 1`, `errors[]`, and `remediation` guidance
- **Run modes: collaborative / autonomous** — `bth run --agent-mode <mode>`; priority chain: CLI flag → sidecar `[experiment] agent_mode` → project `.bth.toml` `[defaults]` → global config → `"collaborative"`; autonomous mode enforces first-of-kind check (blocks re-running the same script at the same git HEAD)
- **Sidecar structural validator** (`validate.py`) — checks `[outcomes.*]` blocks for `condition`, `decision`, `reasoning` fields; validates DuckDB SQL conditions; requires at least one `is_residual = true` fallback branch; requires at least one `result_schema` field referenced in conditions
- **Schema v3** — 8 new fields on `Run`: `sidecar_sha256`, `sidecar_path`, `parent_run_id`, `agent_mode`, `sidecar_mode`, `outcome_is_residual`, `skill_sha256`, `campaign_id`; full migration chain v0→v1→v2→v3
- **Result emission pipeline** — `$BTH_RESULTS_PATH` env var (or `<stem>.bth-results.json` fallback) connects script output to outcome evaluation; `run.outcome` now populated from sidecar conditions at run-end
- **Lineage tracking** — `bth run --derived-from <run-id>` links runs into a DAG; `bth lineage <run-id>` shows ancestor chain via recursive CTE with cycle protection
- **Campaigns** — `bth campaign create/add/conclude/ls/show/review`; two modes: `exploration` (no temporal constraint) and `confirmation` (enforces runs must postdate campaign creation); campaign membership written to cool fragment at run time and auto-populated to warm `campaign_runs` table at compaction
- **Sprint audit** — `bth sprint-audit [--hours N]` cross-project audit across all registered projects; skips projects with incompatible schema version with warning; detects unknown outcomes, bypass spikes, residual rate anomalies
- **Tier-2 lint checks** (`bth lint` extended) — `check_residual_rates` (warn >10% per campaign), `check_bypass_trend` (warn if bypass rate increasing week-over-week), `check_unfired_branches` (warn if all runs in a group map to same outcome)
- **MCP campaign tools** — `campaign_create`, `campaign_list`, `campaign_review`, `campaign_conclude` exposed as MCP tools; `run` tool extended with `agent_mode`, `campaign_id`, `derived_from`, `no_sidecar` parameters; gate failures returned as structured tool results
- **CLI flags** — `bth run --agent-mode`, `--no-sidecar`, `--derived-from`, `--campaign`; `bth ls --outcome`, `--sidecar-mode`
- **SKILL.md agentic integrity section** — three-tier taxonomy (Tier 1: validate, Tier 2: lint, Tier 3: principles) + campaign workflow examples

### Fixed

- `compact.py` — outcome labels from cool fragments now preserved during warm-tier promotion (previously silently set to NULL)
- `prereg.py` — `check_first_of_kind()` SQL query now parameterised (previously used f-string interpolation, SQL injection risk)
- `compact.py` — `_migrate_v1()` now hardcodes target version `"2"` (previously referenced `CURRENT_SCHEMA_VERSION` dynamically, which would cause v1→v3 skip when version bumped)
- `query.py` — `lineage()` CTE now handles NULL `parent_run_id` via `COALESCE` and guards against cycles with `depth < 50` limit
- `campaigns.py` — temporal ordering comparison now uses parsed datetimes with timezone normalisation (previously compared heterogeneous types as strings)

---

## [0.2.1] - 2026-05-19

### Fixed

- **Skill export** — skill is now written as `using-bathos/SKILL.md` (directory format) with proper YAML frontmatter, matching the Claude Code skill loader convention; previously exported as a flat `.md` file with no frontmatter
- **Version stamp placement** — HTML version comment is now inserted after the frontmatter closing `---` rather than before it, so frontmatter parsers see clean YAML
- **Wheel packaging** — `agent_assets/` is now bundled into the wheel via `force-include`, so `bth export` works from an installed (non-editable) copy

### Added

- **Public alpha notice** — README now prominently notes that bathos is experimental, WIP, and should be treated as a public alpha

---

## [0.2.0] - 2026-05-19

### Added

- **`bth remote`** — new subcommand group for managing sync remotes
  - `bth remote add <name> <host:path>` — add a remote, written to `.bth.toml` via `tomlkit` (preserves comments and formatting)
  - `bth remote list` — tabular display of configured remotes
  - `bth remote remove <name>` — remove a remote, cleans up empty `[remotes]` section
  - `bth remote test <name>` — SSH connectivity check with latency measurement
- **`bth sync` optional remote** — remote argument is now optional; auto-selected when exactly one remote is configured, error with names listed when multiple are present
- **`@bth.experiment` decorator** — provenance-capturing decorator for Typer-based scripts; records git state, timing, exit code, and output paths without modifying script behaviour
- **`bth lint`** — naming convention and sidecar enforcement checker across all `scripts/` subdirectories
- **`bth new-experiment`** — scaffolds a new experiment script and companion sidecar in `scripts/experiments/`
- **`bth migrate`** — upgrades cool-tier Parquet fragments to current schema without data loss
- **`bth catalog-version`** — reports current schema version and migration status of cool and warm tiers
- **`bth export`** — exports the `using-bathos` Claude Code / Gemini skill and registers the MCP server in the tool's config file (`~/.claude.json` for Claude Code user level)
- **Schema versioning** — `CURRENT_SCHEMA_VERSION` constant centralised in `schema.py`; `_schema_migrations` audit table written on every `bth compact`

---

## [0.1.0] - 2026-05-18

### Added

#### Core CLI Commands (v0.1 feature-complete)

- **`bth init`** — Initialize experiment tracking in a project directory
  - Creates script subdirectories with required structure
  - Generates `.bth.toml` project configuration
  - Exports `_bth_env.sh` for SLURM integration
  - Sets up `.gitignore` for catalog and cache directories

- **`bth run`** — Execute a tracked script with automatic provenance capture
  - Wraps script execution with subprocess isolation
  - Captures git state (repo, HEAD SHA, dirty flag)
  - Records SLURM job context when available
  - Writes atomic Parquet fragments to cool-tier catalog
  - Supports environment variable overrides

- **`bth ls`** — List recent experiment runs with rich formatting
  - Displays run metadata (timestamp, script, git SHA, outcome)
  - Filters by script directory or project slug
  - Shows active/failed runs with visual indicators
  - Banner displays catalog path and warm DB status

- **`bth show`** — Display detailed metadata for a specific run
  - Prints all provenance fields (git state, SLURM context, timing)
  - Shows result schema fields and outcome label
  - Expands metadata JSON for human-readable inspection

- **`bth find`** — Query runs with SQL-like filtering
  - Filter by script name, git SHA, outcome, or custom fields
  - Output format options: table (default), json, parquet
  - Optional `--output-file` parameter for file-level filtering
  - Uses DuckDB backend (warm DB or cool-tier Parquet scan)

- **`bth sql`** — Execute arbitrary SQL against the catalog
  - Queries both cool-tier (Parquet fragments) and warm-tier (DuckDB)
  - Returns results in json, table, or parquet format
  - Error messaging for missing warm DB with remediation hint

#### Tiered Storage & Compaction

- **Cool tier** — Per-run atomic Parquet files
  - SLURM-safe parallel writes via write-then-rename
  - Minimal schema: 13 provenance fields + result JSON column
  - Files located at `~/.bth/catalog/runs/run_<uuid>.parquet`

- **Warm tier** — DuckDB database for interactive queries
  - **`bth compact`** — Ingest cool→warm with schema migrations
  - Executes in background or foreground
  - Fragment count reporting and caching

- **Schema versioning** — v1→v2 migration with chaining
  - v1: core provenance fields
  - v2: adds hostname field (defaults to `socket.gethostname()`)
  - **`bth check`** — Validate run git-drift and schema freshness
  - Reports stale runs vs current HEAD
  - Supports `--check-outputs` for file-level verification

#### Archive & Cleanup

- **`bth archive`** — Export warm→cold partitioned Parquet
  - Partitions by project, year, month for historical bulk export
  - Supports `--dry-run` for preview
  - Ready for cluster remote archival

#### Extended Metadata

- **Hostname capture** — Tracks execution machine
- **SLURM integration** — Records job ID, partition, node when available
- **Outcome column** — Pre-registration outcome labels at run-end
- **Output metadata** — File-level tracking during `bth compact`

### Architecture

- **Stack**: Python 3.12, Typer, DuckDB, PyArrow, Parquet
- **Entry points**: `bth` (CLI), `bth-mcp` (FastMCP server, optional)
- **Tests**: 44 passing tests covering all CLI commands, schema migrations, and integration

### Documentation

- Comprehensive `pyproject.toml` with classifiers, URLs, and entry points
- Apache 2.0 LICENSE file with copyright notice
- README with quick-start guide and architecture overview

---

## Notes

**What's not in v0.1:**
- FastMCP server (planned for v0.2)
- Sidecar pre-registration enforcement
- Global instruction portability
- Frontmatter support (planned for v0.2)

**Backlog (v0.2 candidates):**
- #128: FastMCP server implementation
- #131: Enhanced SLURM integration
- #132: `bth new-experiment` scaffold command
- #133: PyPI release & RTD setup
- #135–142: Schema versioning, results management, archive tier

See `.praxia/specs/bathos-design.md` and project backlog for full roadmap.
