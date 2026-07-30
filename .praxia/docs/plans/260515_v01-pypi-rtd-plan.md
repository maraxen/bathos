# bathos v0.1.0 PyPI/RTD Release Plan

**Date:** 2026-05-15  
**Task ID:** 20260515_pypi_rtd_plan  
**Status:** PLANNING  
**Sprint Goal:** Deliver bathos v0.1.0 on PyPI with documentation on Read the Docs, optionally including FastMCP MCP server as tech preview.

---

## 1. Overview

### Sprint Goal Statement

Produce a production-ready v0.1.0 release of bathos suitable for public PyPI publication and Read the Docs hosting. The release bundles the core experiment tracking CLI (already v0.1 complete: 44 tests passing, all features shipped) plus comprehensive documentation, GitHub Actions CI/CD, PyPI packaging, and RTD integration. Optional FastMCP MCP server is included as a tech preview, parallel to the CLI, to enable integration with Claude Code and other MCP-compatible tools.

### Key Deliverables (What Ships When)

**Phase 1: Release Prep (Atomic)**
- PyPI configuration (`pyproject.toml` fully specified with description, classifiers, URLs)
- LICENSE file (Apache 2.0)
- CHANGELOG.md documenting v0.1.0 features and v0.1.0-rcX iteration history
- MANIFEST.in (if needed for non-Python assets)
- Git version tags and release notes

**Phase 2: Documentation (Parallelizable)**
- RTD configuration (`docs/` structure, `conf.py`, `Makefile`)
- User guide: install, quick start, CLI commands reference, architecture
- API reference (autodoc from docstrings)
- SLURM integration guide (cluster workflows)
- Advanced topics: schema versioning, catalog layout, cool/warm tiers
- Contributing guide

**Phase 3: CI/CD & Build System (Parallelizable)**
- GitHub Actions workflow: test on Python 3.12+ on Linux/macOS/Windows (smoke test)
- Build & publish workflow: `uv build` → wheel upload to PyPI (manual or automated)
- RTD webhook integration (automatic docs rebuild on push to main)
- Pre-release checks: linting, type checking, test coverage

**Phase 4: FastMCP Server (Optional Tech Preview, Parallelizable)**
- Stub `src/bathos/mcp.py` with FastMCP server entry point
- Tool implementations mirroring CLI: `init`, `run`, `ls`, `show`, `find`, `sql`, `compact`, `archive`, `check`, `sync`
- Integration tests validating tool schemas and invocations
- E2E test with Claude Code or local MCP client

**Phase 5: Release Validation (Sequential)**
- All 60+ tests pass (including 15+ new RTD/FastMCP tests)
- No regressions in v0.1 core functionality
- Wheel builds successfully, installs cleanly
- `bth --help` works post-install
- `bth-mcp` starts and lists tools
- RTD builds zero warnings
- Clean git state (no uncommitted changes, version tags in place)

### Test Strategy Summary

**Test-first approach per kaizen principles:**
- RTD/documentation tests written first (validate Sphinx build, autodoc)
- FastMCP tests written before tool implementations (stubs with correct schemas)
- Build system tests (wheel metadata, entry points)
- E2E smoke tests after each batch

**Execution order:**
1. **Batch 1 (sequential foundation):** Release metadata + LICENSE, CHANGELOG → triggers core docs/CI setup
2. **Batch 2 (parallel-safe):** RTD docs, FastMCP stubs, GitHub Actions YAML
3. **Batch 3 (parallel-safe):** FastMCP tool implementations (1-2 per task)
4. **Batch 4 (sequential gate):** Validation, tagging, PyPI staging

---

## 2. Dependency Graph

```
A0: Bump version → pyproject.toml v0.1.0
  └─ A1: Create LICENSE (Apache 2.0)
      ├─ A2: Write CHANGELOG.md (v0.1.0 features)
      │   └─ A3: Update README.md links to docs/RTD
      │       └─ B1: Create docs/ directory structure (Sphinx scaffold)
      │           └─ B2: Write docs/conf.py + docs/index.rst (Sphinx config)
      │               ├─ B3: Write user guide docs (parallel with B4–B6)
      │               ├─ B4: Write API reference docs (autodoc)
      │               ├─ B5: Write architecture/schema docs
      │               └─ B6: Write contributing guide
      │           └─ B7: Create .readthedocs.yml (RTD webhook config)
      │
      ├─ C1: Create .github/workflows/test.yml (Python 3.12 smoke test)
      │   └─ C2: Create .github/workflows/publish.yml (build → PyPI, manual trigger)
      │       └─ C3: Update GitHub Actions secrets (PyPI token)
      │
      ├─ D1: Create src/bathos/mcp.py stub (FastMCP entry point + tool stubs)
      │   ├─ D2: Write tests/test_mcp.py (tool schema validation)
      │   ├─ D3: Implement init tool + tests
      │   ├─ D4: Implement run tool + tests
      │   ├─ D5: Implement ls/show/find tools + tests
      │   ├─ D6: Implement sql/compact/archive tools + tests
      │   ├─ D7: Implement check/sync tools + tests
      │   └─ D8: E2E integration test (FastMCP server startup + invocation)
      │
      └─ E1: Gate: Lint (ruff, pyright)
          └─ E2: Gate: All 60+ tests pass
              └─ E3: Gate: Wheel builds (uv build)
                  └─ E4: Gate: Clean install (uv tool install from wheel)
                      └─ E5: Gate: RTD dry-run (sphinx-build -W)
                          └─ E6: Tag release (git tag v0.1.0)
                              └─ E7: Draft release notes on GitHub
                                  └─ E8: Publish to PyPI (uv publish or gh release)
```

**Critical path:** A0 → A1 → A2 → A3 → B1 → B2 → (B3–B6 parallel) → B7 → C1 → C2 → C3 → D1 → (D2–D8 with dependencies) → E1–E8.

---

## 3. Atomic Tasks (Sequenced)

### Task T1: Version bump and package metadata

**Title:** Update pyproject.toml with v0.1.0, classifiers, URLs, metadata

**Description:** Bump version to v0.1.0, add long description, classifiers (Intended Audience, Topic, License, Python versions), add repository/documentation/bug-tracker URLs, and define `bth` and `bth-mcp` entrypoints for both CLI and FastMCP server.

**Files touched:**
- `pyproject.toml` (modify)

**Test gates:**
- `uv build` produces valid wheel metadata
- Wheel contains `name="bathos"` and `version="0.1.0"`
- Entry points: `bth = "bathos.cli:app"` and `bth-mcp = "bathos.mcp:mcp_server"`

**Dependencies:** None (first task)

**Estimate:** S (15 min)

---

### Task T2: Create LICENSE file

**Title:** Add Apache 2.0 LICENSE to repo root

**Description:** Copy Apache 2.0 text to `LICENSE` file at repo root. Reference this in `pyproject.toml` under `license`.

**Files touched:**
- `LICENSE` (create)
- `pyproject.toml` (update license field)

**Test gates:**
- File exists and contains Apache 2.0 standard text
- `pyproject.toml` references it: `license = { text = "Apache 2.0" }`

**Dependencies:** T1

**Estimate:** S (5 min)

---

### Task T3: Write CHANGELOG.md

**Title:** Document v0.1.0 release features, fixes, and breaking changes

**Description:** Create CHANGELOG.md following Keep a Changelog format. Document all v0.1 features (init, run, ls/show/find/sql, compact, archive, check, sync, schema versioning with v1→v2 migrations, SLURM integration). Include a v0.0.0 (initial commit) placeholder. Format: features, fixes, deprecated, removed, security.

**Files touched:**
- `CHANGELOG.md` (create)

**Test gates:**
- File follows Keep a Changelog format
- v0.1.0 section lists all 10 FastMCP tools: init, run, ls, show, find, sql, compact, archive, check, sync
- v0.1.0 section documents: schema v2 migration, SLURM integration, 44 passing tests, cool/warm/cold tiers
- No broken links or syntax errors

**Dependencies:** T1

**Estimate:** M (45 min)

---

### Task T4: Update README.md links and badges

**Title:** Add version badge, documentation link, PyPI badge

**Description:** Update README.md with PyPI badge (![PyPI - Version](https://img.shields.io/pypi/v/bathos)), documentation link (https://bathos.readthedocs.io), and GitHub badge. Keep existing quick start and architecture sections.

**Files touched:**
- `README.md` (modify)

**Test gates:**
- Badges render correctly (GitHub displays them)
- Links point to correct URLs (not 404)
- Quick start section still present and correct

**Dependencies:** T3 (need CHANGELOG context)

**Estimate:** S (10 min)

---

### Task T5: Scaffold Sphinx documentation directory

**Title:** Create docs/ structure with source/, build/, Makefile, conf.py, index.rst

**Description:** Create `docs/` directory with standard Sphinx layout: `source/` for `.rst` files, `build/` for build output, `Makefile` for build targets, `conf.py` for Sphinx config (enable autodoc, napoleon, html_theme="pydata_sphinx_theme"), `index.rst` with complete TOC skeleton including all planned sections (user-guide, api, architecture, slurm-integration, contributing).

**Files touched:**
- `docs/source/conf.py` (create)
- `docs/source/index.rst` (create with full TOC skeleton)
- `docs/source/` (directory)
- `docs/Makefile` (create)
- `.readthedocs.yml` (create, but not fully configured until T11)

**Test gates:**
- `docs/source/conf.py` imports `bathos` and extracts version
- `docs/Makefile` targets `html` and `clean` work locally
- `sphinx-build docs/source docs/build` succeeds with zero errors

**Dependencies:** T1 (need version in pyproject.toml)

**Estimate:** M (30 min)

---

### Task T6: Write user guide documentation

**Title:** Create docs/source/user-guide.rst with install, quick start, CLI reference

**Description:** Write comprehensive user guide covering: (1) installation via `uv tool install bathos`, (2) quick start with `bth init` / `bth run` example, (3) CLI command reference for init/run/ls/show/find/check/sql/compact/archive/sync with argument descriptions, (4) output format examples (JSON, tabular), (5) script directory convention table.

**Files touched:**
- `docs/source/user-guide.rst` (create)

**Test gates:**
- RST syntax valid (no Sphinx warnings)
- All CLI commands documented with examples
- Code blocks render correctly
- Links to API reference work

**Dependencies:** T5

**Estimate:** L (2 hours)

---

### Task T7: Write API reference documentation

**Title:** Create docs/source/api.rst with autodoc entries for all public modules

**Description:** Create `api.rst` with `.. automodule::` directives for `bathos.schema`, `bathos.catalog`, `bathos.query`, `bathos.cli`, `bathos.runner`, `bathos.compact`, `bathos.archive`, `bathos.checker`, `bathos.sync`. Enable `:members:` and `:undoc-members:` to auto-generate docstrings. Verify all public functions and classes have docstrings.

**Files touched:**
- `docs/source/api.rst` (create)
- `src/bathos/*.py` (add/improve docstrings as needed)

**Test gates:**
- `sphinx-build` with autodoc runs without warnings
- All public functions documented
- Code examples in docstrings render correctly

**Dependencies:** T5, existing codebase has docstrings

**Estimate:** M (60 min)

---

### Task T8: Write architecture documentation

**Title:** Create docs/source/architecture.rst documenting schema, tiered storage, cool/warm split

**Description:** Document the three-tier architecture: hot (in-memory), cool (Parquet fragments in `~/.bth/catalog/runs/`), warm (DuckDB `bathos.db`), cold (partitioned archive). Explain cool/warm schema split, `schema_version` field, SLURM atomic write-then-rename safety, migration path (v0→v1→v2). Include diagrams (ASCII or embedded PNG).

**Files touched:**
- `docs/source/architecture.rst` (create)

**Test gates:**
- RST syntax valid
- Schema tables render correctly
- Code examples compile

**Dependencies:** T5

**Estimate:** M (45 min)

---

### Task T9: Write SLURM integration guide

**Title:** Create docs/source/slurm-integration.rst with cluster workflows, _bth_env.sh, examples

**Description:** Document SLURM job workflows: sourcing `scripts/slurm/_bth_env.sh`, setting `BTH_PROJECT_SLUG`, `bth run` integration from batch job scripts, parallel job array safety, `bth sync` for catalog transfer between laptop and cluster, checking run validity with `bth check`.

**Files touched:**
- `docs/source/slurm-integration.rst` (create)

**Test gates:**
- RST syntax valid
- Example `.slurm` job file included and correct
- Links to API reference work

**Dependencies:** T5

**Estimate:** M (45 min)

---

### Task T10: Write contributing guide

**Title:** Create docs/source/contributing.rst with dev setup, testing, PR process

**Description:** Document contributing: cloning the repo, `uv sync --dev`, running `uv run pytest`, running tests with coverage, linting with `ruff check` and type checking with `pyright`, commit message conventions (feat, fix, docs, etc.), PR process, code review expectations.

**Files touched:**
- `docs/source/contributing.rst` (create)

**Test gates:**
- RST syntax valid
- Commands copy-paste directly

**Dependencies:** T5

**Estimate:** S (30 min)

---

### Task T11: Configure Read the Docs

**Title:** Create .readthedocs.yml with Python 3.12, dependencies, build settings

**Description:** Create `.readthedocs.yml` at repo root specifying: python version 3.12, install dependencies via `uv` (or fallback to `pip`), build command `sphinx-build docs/source docs/build`, docs root `docs/`, use pydata_sphinx_theme, enable PR previews.

**Files touched:**
- `.readthedocs.yml` (create)

**Test gates:**
- YAML syntax valid (pyaml check)
- Local RTD build succeeds: `sphinx-build -W -n docs/source docs/build` (fail on warnings)

**Dependencies:** T5, T6–T10

**Estimate:** S (15 min)

---

### Task T12: Create GitHub Actions test workflow

**Title:** Create .github/workflows/test.yml with pytest on Python 3.12

**Description:** Create GitHub Actions workflow triggered on push/PR to main. Matrix: Python 3.12 on ubuntu-latest. Steps: (1) checkout, (2) set up uv, (3) `uv sync --dev`, (4) linting with `ruff check`, (5) type checking with `pyright`, (6) `uv run pytest --cov -v`, (7) upload coverage to codecov (optional).

**Files touched:**
- `.github/workflows/test.yml` (create)

**Test gates:**
- YAML syntax valid
- Workflow triggers on PR
- All steps execute successfully locally (manual run)

**Dependencies:** T1 (pyproject.toml stable)

**Estimate:** M (30 min)

---

### Task T13: Create GitHub Actions publish workflow

**Title:** Create .github/workflows/publish.yml with uv build and PyPI upload

**Description:** Create GitHub Actions workflow triggered on manual dispatch or release tag. Steps: (1) checkout, (2) set up uv, (3) `uv build` (creates wheel + sdist), (4) publish to PyPI via `uv publish` with API token from GitHub Secrets. Make it conditional: only run on release tags matching `v*` OR manual trigger.

**Files touched:**
- `.github/workflows/publish.yml` (create)
- GitHub Secrets (configure `PYPI_API_TOKEN`)

**Test gates:**
- YAML syntax valid
- Manual trigger works
- Dry-run against test PyPI succeeds

**Dependencies:** T1, T12

**Estimate:** M (30 min)

---

### Task T14: Configure GitHub Secrets for PyPI

**Title:** Add PYPI_API_TOKEN to GitHub repo secrets

**Description:** Generate PyPI API token for account, add to GitHub repo settings as `PYPI_API_TOKEN` (scoped to bathos package only, if PyPI supports it).

**Files touched:** None (GitHub UI only)

**Test gates:**
- Secret appears in GitHub repo secrets list
- `uv publish --token ${{ secrets.PYPI_API_TOKEN }}` syntax correct in workflow

**Dependencies:** T13

**Estimate:** S (10 min)

---

### Task T15: Create FastMCP stub and tool scaffolds

**Title:** Create src/bathos/mcp.py with FastMCP server and tool stubs

**Description:** Create `mcp.py` with FastMCP server entry point (`mcp_server`) and stub tool implementations for all CLI commands: `init`, `run`, `ls`, `show`, `find`, `sql`, `compact`, `archive`, `check`, `sync`. Each tool has correct input/output schemas matching CLI arguments. Tools are empty stubs that raise `NotImplementedError` initially.

**Files touched:**
- `src/bathos/mcp.py` (create)
- `pyproject.toml` (add fastmcp>=2.0 to dependencies)
- `tests/test_mcp.py` (create, tool schema validation tests)

**Test gates:**
- `import bathos.mcp` succeeds
- `bth-mcp` entry point callable
- Tool schemas validate against fastmcp validation
- Stub tools raise NotImplementedError when invoked

**Dependencies:** T1 (pyproject.toml stable)

**Estimate:** M (60 min)

---

### Task T16: Implement FastMCP init and run tools

**Title:** Implement init and run tools in FastMCP with full feature parity to CLI

**Description:** Implement `init_tool(project_name: str, root_path: str)` and `run_tool(script_path: str, args: list[str], out: str = "")` in `mcp.py`. Both delegate to existing core functions (`bathos.init.init_project`, `bathos.runner.run_script`). Return structured JSON responses with run ID, status, exit code. Handle errors gracefully.

**Files touched:**
- `src/bathos/mcp.py` (implement init_tool, run_tool)
- `tests/test_mcp.py` (add test cases for both tools)

**Test gates:**
- `test_init_tool_creates_project` — tool creates .bth.toml
- `test_run_tool_captures_provenance` — tool returns run_id, status, exit_code
- `test_run_tool_handles_errors` — tool returns error message when script fails

**Dependencies:** T15

**Estimate:** M (60 min)

---

### Task T17: Implement FastMCP query tools (ls, show, find, sql)

**Title:** Implement ls, show, find, sql tools in FastMCP

**Description:** Implement query tools delegating to `bathos.query`: `ls_tool()`, `show_tool(run_id: str)`, `find_tool(pattern: str = "", since: str = "", status: str = "")`, `sql_tool(query: str)`. Return paginated JSON arrays for ls/find, structured object for show, raw result for sql.

**Files touched:**
- `src/bathos/mcp.py` (implement ls_tool, show_tool, find_tool, sql_tool)
- `tests/test_mcp.py` (add test cases)

**Test gates:**
- `test_ls_tool_returns_paginated_runs` — returns 10 runs per page with cursor
- `test_show_tool_returns_full_run` — returns complete Run object
- `test_find_tool_filters_by_status` — respects status filter
- `test_sql_tool_returns_rows` — returns query result

**Dependencies:** T15, T16

**Estimate:** M (60 min)

---

### Task T18: Implement FastMCP compact and archive tools

**Title:** Implement compact and archive tools in FastMCP

**Description:** Implement `compact_tool()` and `archive_tool(project_slug: str = "", dry_run: bool = False)` delegating to `bathos.compact.compact` and `bathos.archive.archive`. Return structured result with counts, duration, path information.

**Files touched:**
- `src/bathos/mcp.py` (implement compact_tool, archive_tool)
- `tests/test_mcp.py` (add test cases)

**Test gates:**
- `test_compact_tool_returns_ingestion_count` — returns count of compacted runs
- `test_archive_tool_returns_export_path` — returns path to exported archive

**Dependencies:** T15, T16

**Estimate:** M (45 min)

---

### Task T19: Implement FastMCP check and sync tools

**Title:** Implement check and sync tools in FastMCP

**Description:** Implement `check_tool(output_file: str = "")` and `sync_tool(remote: str, pull: bool = False)` delegating to `bathos.checker.check_runs` and `bathos.sync.sync_catalog`. Return structured results with status summary and transfer counts.

**Files touched:**
- `src/bathos/mcp.py` (implement check_tool, sync_tool)
- `tests/test_mcp.py` (add test cases)

**Test gates:**
- `test_check_tool_flags_stale_runs` — returns STALE status for old runs
- `test_sync_tool_returns_transfer_count` — returns count of files transferred

**Dependencies:** T15, T16, T17, T18

**Estimate:** M (45 min)

---

### Task T20: FastMCP E2E integration test

**Title:** Write integration test starting FastMCP server and invoking tools

**Description:** Write test that starts the FastMCP server, calls one representative tool (e.g., `init` then `run`), verifies response structure and execution. Use `subprocess` to start server, `json-rpc` or `stdio` client to invoke tools.

**Files touched:**
- `tests/test_mcp.py` (add E2E test)

**Test gates:**
- `test_mcp_server_starts_and_responds` — server starts, responds to `init` call, returns valid JSON
- `test_mcp_tool_sequence_init_then_run` — init then run tool works in sequence

**Dependencies:** T15–T19

**Estimate:** M (60 min)

---

### Task T21: Linting and type checking setup

**Title:** Configure ruff and pyright in pyproject.toml

**Description:** Add ruff (code linter) and pyright (type checker) configuration to `pyproject.toml`. Ruff: line length 100, target Python 3.12. Pyright: strict mode, exclude tests. Create `.pre-commit-config.yaml` (optional, for local developers).

**Files touched:**
- `pyproject.toml` (add [tool.ruff], [tool.pyright])
- `.pre-commit-config.yaml` (create, optional)

**Test gates:**
- `ruff check src/ tests/` returns 0 violations
- `pyright src/` returns 0 errors
- All changes above pass linting and type checking

**Dependencies:** All implementation tasks (T1–T20)

**Estimate:** S (20 min)

---

### Task T22: Run full test suite with coverage

**Title:** Execute all tests, verify 60+ pass, achieve >80% coverage

**Description:** Run `uv run pytest tests/ --cov=bathos --cov-report=term-missing --cov-report=html -v`. Verify all 60+ tests pass (44 existing + new RTD/FastMCP tests), coverage >80%, no skipped tests except intentional xfails.

**Files touched:** None (execution only)

**Test gates:**
- All 60+ tests pass (no failures, no regressions)
- Coverage report shows >80% line coverage
- HTML coverage report generated and readable

**Dependencies:** T1–T20 (all tasks implemented)

**Estimate:** S (15 min)

---

### Task T23: Build wheel and verify metadata

**Title:** Run uv build, inspect wheel contents, verify entry points

**Description:** Execute `uv build` to produce `dist/bathos-0.1.0-py3-none-any.whl` and sdist. Inspect wheel with `unzip -l` or `zipfile` module. Verify: (1) correct version, (2) entry points for `bth` and `bth-mcp`, (3) all source files included, (4) `METADATA` file correct.

**Files touched:** None (build artifacts only)

**Test gates:**
- `uv build` exits with code 0
- Wheel file exists: `dist/bathos-0.1.0-py3-none-any.whl`
- Entry points present in wheel METADATA: `bth = bathos.cli:app`, `bth-mcp = bathos.mcp:mcp_server`
- Version matches `0.1.0`

**Dependencies:** T1–T21

**Estimate:** S (15 min)

---

### Task T24: Clean install and smoke test

**Title:** Install wheel with uv tool, verify commands work

**Description:** In a fresh virtual environment, install wheel with `uv tool install --from dist/bathos-0.1.0-py3-none-any.whl bathos`. Test: (1) `bth --help`, (2) `bth --version`, (3) full smoke sequence: `bth init --slug test-smoke` (create temp project), `bth run tests/fixtures/hello.py` (run a simple script), `bth ls` (list runs), (4) `bth-mcp --help` (verify server startup). Verify no import errors, no missing dependencies, and end-to-end workflow succeeds.

**Files touched:** None (environment test only)

**Test gates:**
- `bth --help` returns usage text
- `bth --version` returns `bathos 0.1.0`
- `bth init --slug test-smoke` creates .bth.toml in temp dir
- `bth run tests/fixtures/hello.py` executes successfully and captures run metadata
- `bth ls` lists at least one run with correct fields
- `bth-mcp --help` returns MCP server help without errors
- All commands exit cleanly with no import errors or missing dependencies

**Dependencies:** T23

**Estimate:** S (15 min)

---

### Task T25: RTD dry-run build

**Title:** Run sphinx-build locally with warnings-as-errors

**Description:** Execute `sphinx-build -W -n docs/source docs/build` to build documentation with warnings promoted to errors. Verify no broken links, missing autodoc references, or RST syntax errors.

**Files touched:** None (build test only)

**Test gates:**
- `sphinx-build -W -n` exits with code 0
- HTML docs generated to `docs/build/html/`
- Index page includes all TOC sections

**Dependencies:** T5–T11

**Estimate:** S (10 min)

---

### Task T26: Final git state check and tagging

**Title:** Verify clean git state, create v0.1.0 tag and release notes

**Description:** Run `git status --short` to ensure no uncommitted changes. Create annotated git tag: `git tag -a v0.1.0 -m "Release v0.1.0: experiment tracking CLI with FastMCP server"`. Push tag to remote: `git push origin v0.1.0`.

**Files touched:** None (git operations only)

**Test gates:**
- `git status --short` returns empty
- `git tag -l v0.1.0` returns tag
- Tag message present: `git show v0.1.0`

**Dependencies:** All tasks T1–T25 merged to main

**Estimate:** S (10 min)

---

### Task T27: Create GitHub release notes

**Title:** Draft release on GitHub with links, changelog excerpt, downloads

**Description:** Use GitHub UI or `gh release create v0.1.0` to create release. Include: (1) release title "v0.1.0: Experiment Tracking CLI for SLURM Research", (2) release body with excerpted CHANGELOG.md, (3) feature highlights (init, run, ls, compact, archive, check, sync, FastMCP), (4) link to RTD docs, (5) download links to wheel/sdist.

**Files touched:** None (GitHub UI only)

**Test gates:**
- Release appears on GitHub releases page
- Release notes render without broken links
- Changelog excerpt is readable

**Dependencies:** T26

**Estimate:** S (15 min)

---

### Task T28: Publish to PyPI (manual or automated)

**Title:** Execute publish workflow or manual uv publish to PyPI

**Description:** Either trigger GitHub Actions publish workflow (if automated), or manually run `uv publish` from repo root with PyPI token. Verify package appears on https://pypi.org/project/bathos/ within 5 minutes.

**Files touched:** None (PyPI publication only)

**Test gates:**
- Package appears on PyPI: `pip index versions bathos` shows v0.1.0
- Installation works: `pip install bathos==0.1.0` succeeds
- Installed `bth` command works

**Dependencies:** T27

**Estimate:** S (10 min, gated on PyPI availability)

---

## 4. Batch Grouping (Parallelization)

### Batch 1 (Sequential Foundation, ~2 hours)

**Critical path:** All releases depend on these tasks completing first.

- T1: Version bump (15 min)
- T2: LICENSE (5 min)
- T3: CHANGELOG (45 min)
- T4: README update (10 min)

**Rationale:** These are immutable metadata. All downstream tasks (docs, CI, build) reference them.

**Sequential dependency:** T1 → T2 → T3 → T4

---

### Batch 2 (Documentation, ~5 hours)

**Parallelizable:** Can run 4–5 docs tasks concurrently after T5 scaffold.

**Tasks:**
- **Parallel Group 2a:** T6 (user guide), T7 (API reference), T8 (architecture), T9 (SLURM guide), T10 (contributing)
- **Blocker:** T5 (scaffold) must complete first
- **Merge:** T11 (RTD config) after all docs written

**Rationale:** Docs are independent; only RTD config depends on all of them.

**Parallelization:** 5 writers, each on one docs task.

---

### Batch 3 (CI/CD Setup, ~1.5 hours)

**Parallelizable:** T12 and T13 are independent after pyproject.toml stable (T1). T14 must follow T13.

**Tasks:**
- **Parallel Group 3a:** T12 (test workflow), T13 (publish workflow)
- **Sequential after T13:** T14 (configure secrets, depends on publish workflow YAML existing)

**Rationale:** T12 (test) and T13 (publish) are independent; only T14 (secrets) depends on T13 (the publish workflow YAML must exist before the secret reference makes sense).

**Parallelization:** 2 writers in parallel (T12, T13), then 1 writer for T14.

---

### Batch 4 (FastMCP Implementation, ~6 hours)

**Parallelizable:** Tool implementations are independent after T15 stub.

**Tasks:**
- **Sequential:** T15 (stub and scaffolds) — 60 min
- **Parallel Group 4a (after T15):** T16 (init+run), T17 (query tools), T18 (compact+archive), T19 (check+sync) — can overlap
- **Sequential (after 4a):** T20 (E2E integration test) — 60 min

**Rationale:** Stub establishes schemas; tool implementations are independent; E2E integration test depends on all tools.

**Parallelization:** 4 writers (one per group of tools) during parallel group, 1 writer for E2E.

---

### Batch 5 (Validation & Release, ~1.5 hours)

**Sequential:** Each gate depends on previous.

- T21: Linting/type checking (20 min) — depends on all code tasks (T1–T20)
- T22: Test suite (15 min) — depends on T21
- T23: Build wheel (15 min) — depends on T22
- T24: Clean install (15 min) — depends on T23
- T25: RTD dry-run (10 min) — depends on T5–T11
- T26: Git tagging (10 min) — depends on all merged to main
- T27: GitHub release (15 min) — depends on T26
- T28: PyPI publish (10 min) — depends on T27

**Rationale:** Sequential gates ensure quality. Each task verifies the previous one.

**Critical path:** T1 → T2 → T3 → T4 → T5 → (T6–T10 parallel) → T11 → T12/T13/T14 → T15 → (T16–T19 parallel) → T20 → T21 → T22 → T23 → T24 + (T25 parallel) → T26 → T27 → T28.

**Estimated total time on critical path:** ~20 hours (8 hours foundation + docs + CI + 6 hours FastMCP + 3 hours validation).

**With 3–4 parallel agents:**
- Agent 1: Batch 1 (foundation) + T5/T11 (RTD scaffold) + T15/T20 (FastMCP stubs + E2E) + T21–T28 (validation/release)
- Agent 2: T6/T7/T8/T9/T10 (docs in parallel) + T12/T13/T14 (CI in parallel) + T16/T18 (FastMCP tools 1–2)
- Agent 3: T17/T19 (FastMCP tools 3–4)

**Parallel speedup:** ~12 hours wall clock (vs. 20 sequential).

---

## 5. Risk & Mitigation

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| FastMCP library API changes | low | high | Pin `fastmcp>=2.0,<3.0`; test with latest stable at gate time; consider vendoring if needed |
| RTD build fails on imports | medium | high | Run `sphinx-build -W` locally as T25 gate; fix all warnings before publish; test autodoc in isolation |
| GitHub Actions YAML syntax errors | low | medium | Validate YAML with `yamllint .github/workflows/*.yml` during T12/T13; test workflow locally with `act` CLI if possible |
| PyPI token expires | low | high | Use fine-grained PyPI tokens scoped to `bathos` package only; document token rotation in CONTRIBUTING.md; test publish on test PyPI first |
| Coverage regression | medium | medium | Require >80% overall coverage as T22 gate; new FastMCP code should achieve >85% module coverage where possible |
| Wheel build fails | low | high | T23 gate includes `uv build` exit code check; if fails, revert most recent pyproject.toml change and investigate |
| Missing or broken docstrings | medium | medium | T7 requires all public functions documented; pyright type checking (T21) catches undefined types; autodoc build (T25) fails if docstrings missing |
| Dirty git state before tag | low | low | T26 checks `git status --short`; if not empty, block tagging until clean |

---

## 6. Verification Checklist (Pre-Release Gates)

- [ ] All 60+ tests pass: `uv run pytest tests/ -v` returns 0 failures, 0 errors
- [ ] All existing tests pass (no regression): Run full suite, ensure no skips except xfail
- [ ] Coverage >80%: `uv run pytest --cov=bathos --cov-report=term-missing` shows >80% line coverage
- [ ] `uv build` produces valid wheel: Wheel file exists, metadata correct, entry points present
- [ ] Wheel installs cleanly: `uv tool install --from dist/bathos-*.whl bathos` succeeds
- [ ] `bth --help` works: Command available post-install, returns usage text
- [ ] `bth --version` returns `0.1.0`: Version string correct
- [ ] `bth-mcp` starts: FastMCP server starts without errors, responds to version/help requests
- [ ] RTD builds cleanly: `sphinx-build -W -n docs/source docs/build` exits 0, zero warnings
- [ ] Linting passes: `ruff check src/ tests/` returns 0 violations
- [ ] Type checking passes: `pyright src/` returns 0 errors
- [ ] Git state clean: `git status --short` is empty, all changes committed
- [ ] Version tag exists: `git tag -l v0.1.0` shows tag
- [ ] GitHub release notes published: Release visible on GitHub Releases page
- [ ] Package on PyPI: `pip index versions bathos` shows v0.1.0, or browser https://pypi.org/project/bathos/ shows it
- [ ] Post-install smoke test passes: Fresh environment can run `bth init`, `bth run`, `bth ls`, `bth-mcp`

---

## 7. Critical Path Analysis

**Longest sequential dependency chain:**

```
T1 (15m) → T2 (5m) → T3 (45m) → T4 (10m) → T5 (30m) → T11 (15m)
                                                    ↘ T6–T10 (4h parallel)
T12/T13/T14 (1.5h parallel) → [merge main] →
T15 (60m) → [T16–T19 parallel, 3h total] → T20 (60m) →
T21 (20m) → T22 (15m) → T23 (15m) → T24 (15m) + [T25 parallel, 10m] →
T26 (10m) → T27 (15m) → T28 (10m)
```

**Wall-clock estimate:**
- Batch 1 (foundation): 1.25 hours sequential
- Batch 2 (docs): 4 hours (scaffold + parallel docs) = 2.5 hours with parallel (1 fast path + 4 writers)
- Batch 3 (CI): 1.5 hours parallel = 0.5 hours wall clock
- Batch 4 (FastMCP): 6 hours sequential = 3 hours wall clock with 4 parallel agents
- Batch 5 (validation): 1.5 hours (mostly sequential, T25 overlaps)

**Total:** ~8.75 hours wall clock with 4 parallel agents (vs. ~20 hours sequential).

**Bottleneck:** FastMCP tool implementation (T15–T20) is longest single phase. Docs (T6–T10) can parallelize to hide 2–3 hours.

**Critical path stays sequential:** Foundation → Docs → CI → FastMCP → Validation (nothing can start until previous batch completes due to git merge gates and metadata dependencies).
