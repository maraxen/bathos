# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
