# bathos CLAUDE.md

CLI tool: `bth` | Install: `uv tool install bathos` | Stack: Python 3.13, cyclopts, DuckDB, PyArrow, cisternal

## What This Is

bathos is a standalone experiment tracking CLI for a single researcher across 10+ projects and a SLURM cluster. Central goal: you never lose track of what ran, what it produced, or whether results are still valid.

**Core insight:** experiments already sit in a directory taxonomy (`scripts/experiments/`, `scripts/benchmarks/`, etc.) — bathos uses that structure to enforce per-directory schemas and pre-registration discipline at the tool level.

---

## Current Status (as of 2026-11-24)

**Latest: v0.18.0 (2026-11-24) — 5,449 tests collected.**

Full version history is in [`CHANGELOG.md`](CHANGELOG.md). Summary of recent versions:

- **Rigor gates, output tiers, catalog ops, `ty` ratchet & audit remediation (`v0.18.0`)** — `[lint.naming]`, `real_home_guard` & postmortem/prereg/validate/query/claim fixes (`#3323`/`#2820`/`#3061`/`#2916`, `#6645`/`#2224`/`#2220`/`#2218`), `campaign_edges` + compaction `flock` + `reap`/`repair` (`#3508`/`#2238`/`#2213`/`#2079`), `ledger.py`, non-code `blast_radius`, retention & `UnitPackReader`, skill updates (`#1474`/`#2686`/`#3495`/`idea-003`/`#2789`), unit-of-independence / source-batch confound / `correction_sweep` / `[hypothesis]` / continuous e-values + `E_n` viz chart / `campaign_formulation` / `handoff_reconciliation` (`#3094`/`#3095`/`#3099`/`#3098`/`#3315`/`#2345`/`#1179`), full `ty` error-strength ratchet (`#4122`), and 4-prong adversarial audit remediation (`#2322`, `#2314`, `#2677`, `#3096`, `#3097` remain held/open)
- **Scientific validity gates, Exploratory S3–S5 & scaling (`v0.17.0`)** — `[invariants]` / confirmatory `[sanity]` / `claim.assumptions` gate / prose data-quality & factor-coverage checks / `decision_scope` vs `regime` (`#2202`, `#2662`, `#2200`, `#2199`), `observations.py` & `appraisal.py` (`bth observe`/`appraise`, `[holdout]`, `--from-observation`, S5 calibration source lint, 7 domain error codes, 5 corpus rule cards), O(1) `bth verify` & `index.db` `submits` scaling (`#3052`, `#1182`), MCP `project_root` & `bth run --sidecar` & portable `sidecar_path` (`#3582`, `#2243`, `#2225`), and `get_run` prefix lookup + `using-bathos` hardening (`#3333`, `#2346`, `#2323`, `#2177`)
- **Sprint hygiene, integrity gates & C2 catalog hardening (`v0.16.0`)** — stoichiometry stage-graph/reagents/`verify_citations --strict-coords` (`#6687`/`#6688`), `bathos-progress-report` skill + literature-parity skill smoke tests (`#4379`), blast-status false-clean & `connect_read` fallback (`#3563`), stepback trigger fixes (`#3236`), postmortem-override decision-doc flagging (`#3494`), unresolved outcome & result JSON validation (`#3326`/`#3327`), and C2 catalog/sidecar/repair/ingest hardening (`#3051`/`#3118`/`#2819`)
- **Bearings v1 (`v0.15.0`)** — multi-scale goal awareness (observe-only), human-confirmed project charters (`bth charter confirm`), deterministic blind packets and reading records (`PacketLeakError`), AC-9 `[charter] purpose` labels, and shadow mode (`cisternal>=0.1.7`, `shadow.label`, `bth sync` shadow support)
- **Result Validity, Supersession & Archive (`v0.14.0`)** — `.bth/instruments/*.toml` defect & estimand-change registries, `version_resolver.py`, `validity_cache.db`, `--include-invalid`, `.bth/findings/*.toml` lifecycle (`bth finding create/list/show/settle/supersede/reopen`), `bth-session-start-hook`, `bth archive-sweep`, doc store archival, and `bth telemetry-rotate`
- **Runlog, Provenance & Preflight (`v0.13.x`)** — `bth lock` (`materialize`/`relocate`/`untrack`/`push`/`fetch`), `[breakdown]` distributional discipline & `near_bound` obligations, postmortem `format = 2` (`[fork]`/`[[amendments]]`), `bth backfill-declarations`, `[provenance] track_packages`, `[requires]`/`[budget]` `bth preflight`, strict unaffiliated project refusal & non-git checkout binding
- **Claim-tier rigor (`v0.12.0`)** — `bth claim scaffold/register/validate`, Union Gate at `bth campaign conclude`, `claim_coverage_<id>.json` sidecar (with `clause_labels`), AC-04/05/06 discriminability advisories, AC-13 `[baseline_parity]` confound lint, Signal 12 in sprint-audit, descriptive-label lint (`bth lint` on `.bth/claims/`), schema v8 (`claim_discriminates`, `claim_isolates`)
- **Literature-parity v1** — `parity_validate.py`, `attest_parity()` / `bth campaign attest-parity`, F2 conclude-gate + F3 submit-gate, `parity_confound_check()`, Signal 13, schema v9 (`parity_run_type` COOL column), skill protocol in `/using-bathos`
- **AC-20 SHA-drift** — `check_output_sha_drift()`; `bth check --check-outputs` flags drift; parity confound downgrades on artifact mutation
- **Experimental controls discipline** — `stage_name` write path, `[reproduction]`/`[controls]` sidecar blocks, reproduction prerequisite gate at `bth submit`, `control_arm_rate` Signal 9, `baseline_ref` Tier-2 lint, novel/reproduces Tier-1 lint, submit-provenance Parquet, bypass-rate Signal 10
- **Worktree-aware workspace resolution** — `resolve_workspace(cwd)`; `BTH_WORKSPACE_ROOT` env override for SLURM jobs
- v0.10.x: `bth repair`, POPPER e-value sequential campaigns, `bth outputs list/summary`, structured MCP error taxonomy (#793)

---

## Architecture

### Tiered Storage

```
SLURM job / bth run
      │
      ▼  atomic write-then-rename
 cool/   ~/.bth/catalog/runs/<slug>/run_<uuid>.parquet   ← minimal schema, SLURM-safe
      │
 bth compact  (lazy on query or explicit)
      │
      ▼
 warm/   ~/.bth/catalog/bathos.db                 ← DuckDB, full schema + metadata JSON
      │
 bth archive  (explicit)
      │
      ▼
 cold/   ~/.bth/catalog/archive/project=X/year=Y/month=M/runs.parquet
```

- **Hot:** in-memory `Run` object during execution
- **Cool:** per-run Parquet fragments, atomic write target, SLURM parallel-safe
- **Warm:** DuckDB database, primary interactive query target (`bth ls`, `bth find`)
- **Cold:** partitioned archive Parquet, historical bulk queries, sync-able to cluster remote

Cool schema is intentionally minimal (13 provenance fields). Warm adds `metadata TEXT` (JSON) and `outcome TEXT` columns.

### Pre-Registration Schemas (per script directory)

Every script in `scripts/experiments/` and `scripts/benchmarks/` should have a companion sidecar **`<script-stem>.bth.toml`** declaring its hypothesis, expected outcomes, and result schema. In v0.2+, `bth run` enforces this by default; pass `--no-sidecar` to bypass.

**Sidecar format (experiments):**
```toml
[experiment]
hypothesis = "NVT with dt=0.5fs maintains ±5K temperature stability over 50ps"

[outcomes.pass]
condition = "temp_std < 5"        # DuckDB SQL fragment
decision = "proceed to NPT validation"

[outcomes.marginal]
condition = "temp_std >= 5 AND temp_std < 10"
decision = "tune Langevin gamma, re-run"

[outcomes.fail]
condition = "temp_std >= 10"
decision = "debug thermostat, open issue"

[result_schema]
temp_mean = "float"
temp_std = "float"
n_steps = "int"
dt_fs = "float"
```

**Sidecar format (benchmarks):**
```toml
[benchmark]
baseline_ref = "run_abc123"
metric = "ns_per_day"
regression_threshold = 0.05
target = "> 50 ns/day on pi_so3"

[result_schema]
ns_per_day = "float"
system = "str"
n_atoms = "int"
```

**Sidecar format (debug):**
```toml
[debug]
symptom = "NaN forces after step 847 on 1uao"
suspected_cause = "PME grid aliasing with box < 2*cutoff"
verification = "reproduce with box=4nm, compare box=6nm"

[verdict_schema]
reproduced = "bool"
root_cause = "str"
fix = "str"
```

### Key Design Decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Pre-registration mechanism | **Sidecar TOML** (`<stem>.bth.toml`) | Language-agnostic; works for shell/Rust/Python; readable by CLI without import |
| `@bth.experiment` scope | **Provenance only** | Keeps decorator and sidecar as separate concerns with no sync problem |
| Outcome condition evaluation | **DuckDB SQL fragments**, stored label at run-end | Reuses existing stack; no custom DSL; no `eval()`; label queryable as `WHERE outcome='pass'` |
| Declaration indexing | **Content-addressed** at `bth run` time | Rename-safe; catalog records never depend on file paths |
| Sidecar lookup | `<script-stem>.bth.toml` adjacent to script | Consistent, predictable; resolve real path under symlinks |
| v0.2 option | **Frontmatter** (`# ---bth---` YAML block in-file) | Cleaner long-term (one file, language-agnostic via comment); ship sidecar first |
| Catalog storage | **DuckDB + Parquet** (not pyiceberg) | pyiceberg is for distributed data lakes; DuckDB+Parquet is simpler, no overhead |
| FastMCP | Mirror CLI tool-for-tool | `cli_cyclopts.py` and `mcp.py` both thin layers over same core modules |

### Source Layout

```
src/bathos/
  __init__.py
  cli_cyclopts.py # cyclopts app, the `bth` entry point (thin — calls core); no cli.py, no Typer
  cli_common.py   # shared CLI helpers
  cli_render.py   # CLI presentation for registry-driven cyclopts commands
  mcp.py          # FastMCP server (thin — mirrors cli_cyclopts.py)
  schema.py       # Run dataclass, RUN_SCHEMA PyArrow schema
  catalog.py      # DuckDB+Parquet write/read (cool tier)
  compact.py      # cool → warm compaction
  config.py       # .bth.toml + ~/.bth/config.toml parsing
  git.py          # git state capture
  init.py         # bth init logic
  runner.py       # bth run subprocess wrapper
  query.py        # list_runs, get_run, find_runs, run_sql
  checker.py      # bth check — git-drift freshness gate
  sidecar.py      # .bth.toml sidecar parse, content-hash, outcome eval
  campaigns.py    # bth campaign subcommands + Campaign dataclass
  sprint_audit.py # bth sprint-audit cross-project audit
  linter.py       # bth lint — Tier-1 and Tier-2 checks
  migrate.py      # bth migrate — schema version upgrades
  new_experiment.py # bth new-experiment scaffold
  export.py       # bth export — skill + MCP registration (UNCHANGED — do not touch)
  validate.py     # sidecar structural validation
  remote.py       # bth remote subcommands
  sync.py         # bth sync rsync wrapper
  archive.py      # bth archive warm→cold export
  cite.py         # bth cite — run citation generation
  cluster.py      # bth submit — ClusterConfig, resolve_cluster_config(), myxcel wrappers
  decorators.py   # @bth.experiment provenance decorator
  postmortem.py   # bth postmortem — *.bth.postmortem.toml validation + MCP tools
  claim.py       # bth claim scaffold/register/validate — claim-tier pre-registration and Union Gate
  prereg.py       # agentic integrity pre-registration gate
  provenance.py   # W3C PROV-JSON lineage serialization
  rich_fmt.py     # Rich CLI formatters (base dep) — render_runs_table, render_run_detail, render_campaign_table, render_campaign_review
  telemetry.py    # structured JSONL telemetry (9 event surfaces, SLURM-safe per-process files)
  verify.py       # bth verify — catalog integrity checks (--tier cool/warm/archive/all)
  viz/            # Optional extra: bathos[viz]
    __init__.py
    data.py       # RunDisplay/CampaignDisplay TypedDicts; project_run(), project_campaign()
    html.py       # render_html_report(), export_html() — static HTML renderer
    server.py     # create_app(), run_server() — FastAPI local dashboard
    templates/    # Jinja2 templates (SPA: index.html, _runs.html, _run_detail.html, _campaign.html)
    static/       # Vendored Alpine.js + Pico CSS (both MIT); VERSIONS.md with checksums
  templates/
    _bth_env.sh   # SLURM env helper template
    experiment.py # bth new-experiment script skeleton
```

### Viz Suite Architecture (v0.5)

- `rich_fmt.py` is a **base dependency** — always imported by CLI, no viz/ imports
- `viz/` is an **optional extra** (`bathos[viz]`) — lazy-imported inside command bodies only
- `viz/data.py` consumes only `query.py` + `campaigns.py` — never imports `catalog.py` or `compact.py`
- `bth view` cap: query `limit=1001`, slice `[:1000]`, show sticky banner if count > 1000
- `bth export --html` warns to stderr if rendered HTML > 5 MB
- `bth view` is read-only; never triggers compaction
- `importlib.resources.files("bathos.viz")` used to load templates and static assets at runtime

---

## Script Directory Convention

| Directory | Schema enforced | Naming | Tracked |
|---|---|---|---|
| `scripts/experiments/` | hypothesis + outcomes + result_schema | `verb_noun.py` | Yes |
| `scripts/benchmarks/` | baseline_ref + metric + regression_threshold | `verb_noun.py` | Yes |
| `scripts/validation/` | property + reference + tolerance | `verb_noun.py` | Optional |
| `scripts/analysis/` | none | `verb_noun.py` | Optional |
| `scripts/data/` | none | `verb_noun.py` | No |
| `scripts/slurm/` | none | `verb_noun.slurm` | Via wrapper |
| `scripts/debug/` | symptom + suspected_cause + verification | `YYMMDD_desc.py` | No |
| `scripts/explore/` | none | `YYMMDD_desc.py` | No |
| `scripts/scratch/` | none | `YYMMDD_desc.py` | No (gitignored) |

---

## Implementation Notes

- Use `uv run pytest` for all test runs
- `BTH_CATALOG_DIR` env var overrides default `~/.bth/catalog/` (used in tests)
- `BTH_PROJECT_SLUG` env var overrides `.bth.toml` lookup (used in SLURM jobs)
- Outcome conditions are DuckDB SQL; validate they parse at `bth run` start before job runs
- Content-hash sidecars at run time: store parsed declaration in run record or `declarations` table keyed by SHA256
- `bth run` on a script in `scripts/experiments/` without a sidecar: enforced by default; pass `--no-sidecar` to bypass
