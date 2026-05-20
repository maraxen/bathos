# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed

- **`bth sync` indefinite hang** — `sync_catalog()` now passes SSH options (`ConnectTimeout=10`, `BatchMode=yes`) to rsync so unreachable hosts fail in ≤10 s instead of blocking forever; adds `subprocess.run(timeout=120)` as a safety net; raises `RuntimeError` with actionable message on timeout; drops silent `--progress` flag (output was captured, not displayed)

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
