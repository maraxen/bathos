# bathos Agentic Integrity Design

**Date:** 2026-05-20  
**Status:** Under review — v3 (addressing arch-advisor + oracle second-pass feedback)  
**Scope:** Pre-registration enforcement, nonrepudiation layer, run modes, campaigns, sprint audit, SKILL.md principles taxonomy  
**Research basis:** NotebookLM field synthesis (~144 sources, notebook 3f0490aa), code-architecture-advisor review (x2), oracle strategic assessment (x2), brainstorming session with researcher

---

## 1. Context and Unique Position

bathos occupies a specific niche no existing tool fills: **audit substrate for agentic experiment orchestration at solo-researcher SLURM scale**. Three properties combine to define it:

- **Tiered serverless storage** — SLURM-safe atomic Parquet writes; no daemon required
- **Sidecar pre-registration** — hypothesis + outcome decision tree as machine-readable artifact before execution
- **MCP server** — Claude can call `bth run`, `bth ls`, `bth find` programmatically; the audit trail for agentic runs is the same catalog as human runs

Existing systems each cover one leg: Nextflow/Snakemake handle execution graphs but not hypothesis binding; OSF handles pre-registration but not computational provenance; the AI Scientist has no persistent audit substrate at all. The "experiment nonrepudiation" gap — binding reported numbers to declared intent in a tamper-evident, machine-queryable way — is what bathos closes.

**Core design philosophy:** The tool's value is in the legibility of its artifacts to a future reader, not in the friction of its gates at write time. Every mitigation that depends on the researcher noticing something at the moment of action will fail under deadline pressure. Every mitigation that surfaces the right signal in the artifact the researcher consults later compounds.

---

## 2. What Is Already Built (Confirmed by Recon)

The following are complete and do not need to be designed:

- **Warm tier** — DuckDB `bathos.db`, schema migrations (v0→v1→v2), compaction pipeline (`compact.py`)
- **`outcome` column** — present in both `WARM_SCHEMA` and DuckDB `runs` table
- **`evaluate_outcome()`** — in `sidecar.py`; evaluates DuckDB SQL conditions against result dict
- **`sidecar.py`** — parse, find, `is_in_enforced_dir` all complete
- **Query dispatch** — `query.py` routes to warm/cool transparently
- **`output_metadata`** — file presence, size, SHA256, mtime collected at compaction

**Critical latent bug:** `run.metadata` is never populated by `runner.py`, so `evaluate_outcome()` always returns `"unknown"` regardless of sidecar quality. The result emission pipeline must be fixed before any outcome-dependent feature works.

---

## 3. The Gate Layer

### 3.1 `validate.py` — new module (build first)

Separate from `sidecar.py` (which owns parse + locate + runtime eval). `validate.py` owns structural completeness checking. **Must be implemented before `prereg.py`**, since `gate_check` depends on it.

Checks performed:
- Required sections present (`[experiment]`/`[benchmark]`, `[outcomes]`, `[result_schema]`)
- Each outcome branch has `condition`, `decision`, and `reasoning` fields
- `condition` DuckDB SQL parses without error
- At least one `result_schema` field referenced in conditions
- At least one `is_residual = true` fallback branch present (exhaustiveness rule — require a named fallback rather than symbolic analysis; near-zero false-positive rate)
- `reasoning` must cite either a constant/threshold from the script source or an external reference (URL, DOI, lab notebook ID) — bare narrative is rejected

Exhaustiveness is validated **empirically against run history** by `bth campaign review`, not at write time. A campaign where residual_rate exceeds the configured threshold is flagged as gate-malformed — the pre-registration didn't anticipate the outcome space.

### 3.2 `prereg.py` — new module

Factor pre-registration concerns out of `runner.py` (which already conflates argv parsing, subprocess lifecycle, sidecar gating, and outcome evaluation). `prereg.py` owns:

- `resolve_sidecar(script_path) -> SidecarBundle` — finds sidecar, computes `sha256(sidecar_bytes)`, resolves real path, determines `sidecar_generated` flag
- `resolve_agent_mode(cli_flag, sidecar, project_config, global_config) -> Mode` — priority: CLI flag → sidecar `[experiment] agent_mode` → project `.bth.toml` `[defaults]` → global config. Default: `collaborative`.
- `gate_check(script_path, mode) -> GateResult` — calls `validate.validate_sidecar()`; returns `OK` or structured `GateFailure` payload
- `check_first_of_kind(script_path, catalog_dir) -> bool` — **bth-internal function** that queries the warm/cool catalog for prior runs matching the script's content-hash family. This is not agent-performed; the agent is not given access to result history during autonomous sidecar generation. `prereg.py` runs this check before allowing autonomous mode to proceed.

`runner.py` shrinks to: parse argv → call prereg → subprocess → call outcome eval → `write_run`.

### 3.3 Gate scope

Enforced directories: `scripts/experiments/`, `scripts/benchmarks/`, `scripts/validation/`.  
Ungated: `scripts/explore/`, `scripts/scratch/`, `scripts/debug/`.  
Escape hatch: `bth run --no-sidecar` sets `sidecar_mode = "bypassed"` in the run record. Nothing hidden, nothing blocked forever.

### 3.4 MCP gate error format

Gate failures return as **structured tool results, not exceptions**. Exceptions signal "tool broke"; domain failures need a parseable payload Claude can act on:

```json
{
  "status": "gate_failure",
  "gate": "sidecar_missing | sidecar_invalid | not_first_of_kind | agent_mode_mismatch",
  "errors": ["[outcomes.pass] missing required field: reasoning", "..."],
  "required_format": { "comment": "annotated TOML skeleton" },
  "agent_mode": "collaborative",
  "remediation": "Create <path>.bth.toml with the sections above, then retry.",
  "gate_schema_version": 1
}
```

`gate_schema_version` is included so future payload changes don't silently break older agent prompts. MCP server defaults to v1; future versions will add a client-declared version cap. Unexpected failures (DuckDB corrupt, disk full) remain exceptions.

---

## 4. Run Mode

Two values: `collaborative` (default) and `autonomous`.

### Collaborative mode (gate fires)
Surface structured error to researcher. Present the required sidecar template. Draft the hypothesis *together* before writing. Retry only after researcher-authored sidecar is on disk.

### Autonomous mode (gate fires)
Agent generates the sidecar. Constraints are classified by enforcement tier:

**Mechanically enforced by `prereg.py` / `validate.py`:**
1. **No result history access** — `prereg.py` does not pass catalog query results to the agent during sidecar generation. The agent receives script source and docstring only. `check_first_of_kind()` is a bth-internal guard, not agent-performed.
2. **First-of-kind scripts only** — `check_first_of_kind()` queries the catalog; if prior runs exist in the script's content-hash family, gate fires with `not_first_of_kind`. Autonomous generation is disallowed for iterated scripts.
3. **≥3 outcome branches required** — `validate.py` enforces minimum branch count.
4. **`is_residual = true` fallback branch required** — `validate.py` enforces.
5. **`decision` and `reasoning` present on every branch** — `validate.py` enforces.
6. **`source_provenance` block required** — `validate.py` checks section presence.
7. **`sidecar_generated = true` + agent model ID + session ID logged** — `prereg.py` writes to `[provenance]` section of the generated sidecar and to the run record.

**Enforced via SKILL.md (Tier 3 — interpretive):**
- `reasoning` should cite a specific constant from the script or a specific external reference; bare narrative is a quality signal the researcher should inspect at `bth campaign review`
- `source_provenance` entries should flag any threshold that appears in a comment referencing prior results (potential data leakage)

**Note on autonomous mode validity:** Autonomous generation is defensible only for truly first-of-kind scripts. If the script source contains constants derived from prior exploratory runs (e.g., `temp_threshold = 5.0` because prior runs showed clustering at 3-4K), the temporal lock is broken even without querying the catalog. The `source_provenance` block surfaces this for researcher inspection, but the tool cannot mechanically detect implicit leakage. This is the fundamental limit of autonomous mode — it reduces theater risk but cannot eliminate it for iterated work.

### `sidecar_mode` enum (first-class field)

`declared | generated | bypassed` — on every run, visible in `bth ls` default output. **Not** a hidden flag. Campaign verdicts show the distribution. A campaign cannot conclude with verdict `confirmed` if any constituent run has `sidecar_mode != declared`. This is the one place access control is justified: the verdict label, not execution.

---

## 5. Schema Extensions

All new fields added via the existing migration chain. New migration: v3.

**`runs` table (cool Parquet + warm DuckDB):**

| Field | Type | Notes |
|---|---|---|
| `sidecar_sha256` | `TEXT` nullable | SHA-256 of sidecar bytes at `bth run` invocation |
| `sidecar_path` | `TEXT` nullable | Resolved real path (symlink-safe) |
| `parent_run_id` | `TEXT` nullable | Lineage graph edge; highest-ROI schema addition |
| `agent_mode` | `TEXT` nullable | `collaborative` \| `autonomous` |
| `sidecar_mode` | `TEXT` nullable | `declared` \| `generated` \| `bypassed` |
| `outcome_is_residual` | `BOOL` nullable | True when matched outcome branch has `is_residual = true` — enables residual_rate computation without string label conventions |
| `skill_sha256` | `TEXT` nullable | SHA of `agent_assets/using_bathos/SKILL.md` when `sidecar_generated=true`; computed by MCP layer at generation time |
| `campaign_id` | `TEXT` nullable | Set at run time for confirmation campaigns (required); post-hoc for exploration via `bth campaign add` |

**New warm DuckDB tables (DDL owned by `compact.py`, CRUD in `campaigns.py`):**

```sql
CREATE TABLE campaigns (
    id TEXT PRIMARY KEY,
    project_slug TEXT NOT NULL,
    name TEXT NOT NULL,
    mode TEXT NOT NULL,          -- 'exploration' | 'confirmation'
    question TEXT,               -- exploration mode: one-sentence question
    hypothesis TEXT,             -- confirmation mode: full PAP hypothesis
    status TEXT NOT NULL,        -- 'open' | 'concluded'
    started_at TEXT NOT NULL,
    concluded_at TEXT,
    conclusion TEXT,
    outcome_label TEXT,
    parent_campaign_id TEXT      -- exploration → confirmation transition
);

CREATE TABLE campaign_runs (
    campaign_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    PRIMARY KEY (campaign_id, run_id)
);

CREATE TABLE amendments (
    run_id TEXT NOT NULL,
    amended_at TEXT NOT NULL,
    old_sidecar_sha256 TEXT,
    new_sidecar_sha256 TEXT,
    reason TEXT NOT NULL
);
```

**Campaign membership — resolved Q1:**

- **Confirmation campaigns**: `campaign_id` written to the cool fragment at run time via `bth run --campaign <name>`. The campaign must exist before submission. This is the pre-registration contract: you declare membership before executing.
- **Exploration campaigns**: `campaign_id` may be set at run time or post-hoc via `bth campaign add`. Retroactive grouping is permitted for exploratory work.
- `campaign_runs` is always populated idempotently at compaction (keyed on `(campaign_id, run_id)`), regardless of how membership was declared. This is the authoritative join table.
- `campaign.created_at` must precede all member run timestamps for confirmation campaigns — enforced at `bth campaign add` and at compaction.

**Two-writer safety:** Cool fragment writes (including `campaign_id`) are atomic write-then-rename, SLURM-safe. Warm `campaign_runs` population happens at compaction — idempotent, serialized by DuckDB's exclusive write lock. Concurrent compaction processes serialize automatically; no additional locking needed.

---

## 6. Result Emission (Blocking Fix — P0)

Before any outcome evaluation works, the result emission pipeline must exist.

**`$BTH_RESULTS_PATH` is the canonical contract.** `bth run` sets this env var to a temp path before launching the subprocess. Any script or binary (Python, Rust, shell) writes a JSON object of result metrics to this path. At subprocess exit, `runner.py` reads the file and populates `run.metadata`.

**Conventional fallback:** If `$BTH_RESULTS_PATH` is unset or the file is not written, `runner.py` falls back to `<script-stem>.bth-results.json` adjacent to the script. This is the convenience path for scripts that don't read env vars. For multi-binary SLURM jobs, `$BTH_RESULTS_PATH` is the only correct interface — the conventional path is ambiguous when there is no single "stem."

`evaluate_outcome()` is called against the result dict after file read. `outcome_is_residual` is populated from the matched branch's `is_residual` field.

This must be implemented before outcome evaluation, autonomous mode outcome checking, or campaign verdict computation.

---

## 7. Campaigns — Two Modes

### Exploration mode
- Created with: `bth campaign create <name> --mode exploration --question "What drives temperature instability at dt>0.5fs?"`
- No sidecar enforcement beyond what `bth run` normally requires for the directory
- Outcomes are free-form; `sidecar_mode` distribution tracked but not gating
- Cannot conclude as `confirmed`
- Retroactive run membership permitted
- Produces: a named set of runs + a question answered, feeding the confirmation phase

### Confirmation mode
- Created with: `bth campaign create <name> --mode confirmation --hypothesis "..." --parent <exploration-campaign-name>`
- Full PAP enforcement: all member runs must be in enforced directories with complete sidecars (`sidecar_mode = declared`)
- `campaign.created_at` must precede all constituent run timestamps — **no retroactive backfilling**
- Must reference the exploratory campaign(s) that motivated it (via `parent_campaign_id`)
- Can conclude as `confirmed | refuted | inconclusive`
- Campaign verdict is blocked if any run has `sidecar_mode != declared`

**The exploration→confirmation transition is the load-bearing scientific event.** The system forces the confirmation hypothesis to be written before any confirmation run executes. No other moment in the workflow is as structurally important.

**HARKing-across-campaigns risk (acknowledged, not fully solvable at tool level):** Nothing prevents a researcher from reviewing exploration results, then writing a confirmation hypothesis they already know will be confirmed. The tool records that confirmation followed exploration and requires the `parent_campaign_id` link, but cannot verify the hypothesis was formed independently of the exploration data. This is a known limitation. The SKILL.md Tier 3 guidance addresses it: "The exploration→confirmation hypothesis must be one you would have written before seeing the exploration results, not after. If you are writing a hypothesis that the exploration data already answers, you are confirming, not pre-registering."

### CLI

```
bth campaign create <name> [--mode exploration|confirmation] [--question "..."] [--hypothesis "..."] [--parent <name>]
bth campaign add <run-id> [--campaign <name>]
bth campaign conclude <name> --outcome "..." --note "..."
bth campaign ls [--status open|concluded]
bth campaign show <name>
bth campaign review <name>   # surfaces sidecar reasoning text + outcomes + residual rate
```

`bth ls` / `bth find` gain `--campaign <name>`, `--outcome <label>`, `--sidecar-mode <value>` filters.

---

## 8. Sprint Audit and Observability

### `bth sprint-audit`

Cross-project query using DuckDB `ATTACH ... (READ_ONLY)`. The `READ_ONLY` flag is required — concurrent compaction in another project would otherwise block or corrupt the ATTACH connection.

**Project discovery:** `bth sprint-audit` reads the global project registry at `~/.bth/projects.toml` (a list of `[project]` entries with `slug` and `catalog_dir`). This registry is populated by `bth init` and `bth register`. Projects with no `bathos.db` (warm tier not yet compacted) are skipped with a warning.

**Schema version gating:** Before attaching, `bth sprint-audit` checks `warm_version` in each project's `_schema_meta` table. Projects with incompatible warm schema versions are skipped with "run `bth compact` in project X." DuckDB engine version skew is handled by requiring the same `duckdb` package version across all projects (enforced by uv lock); projects built with a different DuckDB engine are skipped with a warning.

Output grouped by project and campaign:
- All runs in last N hours (default 24h, configurable)
- `sidecar_mode` distribution per campaign
- Anomaly flags: `outcome = 'unknown'`, `sidecar_mode = 'bypassed'`, runs with no campaign membership, `outcome_is_residual = true` rate > threshold

### `bth lineage <run-id>`

Recursive CTE over `parent_run_id` in DuckDB. Returns full ancestor chain with outcomes at each step. Lives in `query.py` — not a new module (one recursive CTE, one caller).

### Drift detection signals

In order of robustness:

1. **Schema population fidelity** — fraction of `result_schema` fields actually populated per run
2. **Extra fields** — run produced metrics outside `result_schema` (signature of "went looking for something else")
3. **SHA chain integrity** — `parent_run_id` chain where sidecar SHA changes without an amendment record; *note: `bth amend` CLI is P3 (#17) — this signal is non-functional until P3 ships*
4. **Discriminative power decay** — all runs in a campaign labeled the same outcome (gate doing no work)
5. **Outcome-label entropy decay** — same script + same sidecar SHA produces wildly different outcome distributions across run batches; flags nondeterminism or selective re-running
6. **Decision-action coupling** (strongest) — branch fired `decision = "proceed to NPT validation"` but no subsequent campaign run is an NPT experiment. Bridges declaration to behavior.

### `bth compare <run-a> <run-b> --metric <field>`

DuckDB join over `metadata` JSON columns. Requires result emission to be working. P3.

---

## 9. SKILL.md Principles Taxonomy

Principles are classified into three tiers — determines where they live, not what they say.

**Tier 1: Structurally enforceable → `validate.py`**
If the validator can check it, it must. Principles in prose that could be code are aspirational, not operative. Examples: ≥1 `is_residual` branch required; `decision` and `reasoning` required on each branch; `source_provenance` block required for autonomous generation; sidecar SHA stored with every run; `outcome_is_residual` stored with every evaluated run.

**Tier 2: Post-hoc checkable → `bth lint` / `bth campaign review` (P2, not P3)**
Cannot be enforced at write time; enforced as gates Claude must pass before reporting a phase complete. These are **load-bearing** for the integrity system — if they ship at P3, Tier 2 has no enforcement vehicle until then. Examples: residual_rate below configured threshold; no decision branch unfired across N>5 runs; no orphan result fields; bypass_rate trend (weekly/monthly cross-campaign report surfacing ratchet erosion).

**Tier 3: Genuinely interpretive → SKILL.md**
Resist mechanization. Examples: "thresholds should be scientifically justified," "hypothesis should be falsifiable," "outcome labels should be researcher-meaningful," "the exploration→confirmation hypothesis must be one you would have written before seeing the exploration data." For these, the skill text is supported by `bth campaign review` as a forcing function — the researcher periodically reads what their past self committed to.

**SKILL.md versioning:** `skill_sha256` is computed by the MCP layer at autonomous sidecar generation time, hashing `agent_assets/using_bathos/SKILL.md`. Stored with every `sidecar_generated=true` run. Principle drift across sessions is then auditable on the same substrate as declaration drift.

---

## 10. Implementation Order

| # | Item | Module | Depends on | Priority |
|---|---|---|---|---|
| 1 | Result emission — `$BTH_RESULTS_PATH` canonical, `<stem>.bth-results.json` fallback | `runner.py` | — | **P0 — blocker** |
| 2 | Schema migration v3: all new `runs` fields including `outcome_is_residual` | `schema.py`, `compact.py` | migration chain | P1 |
| 3 | `validate.py` — structural + reasoning + fallback branch checks | new module | `sidecar.py` | P1 |
| 4 | `prereg.py` — `resolve_sidecar`, `resolve_agent_mode`, `gate_check`, `check_first_of_kind` | new module | schema v3, `validate.py` | P1 |
| 5 | Wire gate + outcome eval + `outcome_is_residual` in `runner.py` | `runner.py` | `prereg.py`, result emission | P1 |
| 6 | `bth ls` — `sidecar_mode` column, `--outcome` and `--sidecar-mode` filters | `cli.py`, `query.py` | schema v3 | P1 |
| 7 | Campaigns DDL in `compact.py`, CRUD in `campaigns.py` | new module | warm tier | P2 |
| 8 | `bth campaign` subcommand group | `cli.py` | `campaigns.py` | P2 |
| 9 | `campaign_id` in cool fragment (confirmation); `campaign_runs` at compaction | `runner.py`, `compact.py` | campaigns DDL | P2 |
| 10 | Global project registry `~/.bth/projects.toml` populated by `bth init` / `bth register` | `init.py`, `config.py` | — | P2 |
| 11 | `bth sprint-audit` — DuckDB ATTACH READ_ONLY, project registry, schema version gating | `cli.py`, `query.py` | campaigns, #10 | P2 |
| 12 | `bth lineage <run-id>` — recursive CTE in `query.py` | `query.py`, `cli.py` | parent_run_id field | P2 |
| 13 | `bth lint` / `bth campaign review` — residual rate, bypass trend, unfired branches | `cli.py` | campaigns, schema v3 | **P2** |
| 14 | MCP tools: campaign CRUD, structured gate error, `skill_sha256` hashing | `mcp.py` | campaign CLI, gate | P2 |
| 15 | SKILL.md updates — three-tier taxonomy, run mode protocol, campaign workflow, HARKing note | `agent_assets/` | all above | P2 |
| 16 | `bth compare` + `bth annotate` | `cli.py`, `query.py` | result emission | P3 |
| 17 | Amendment log (`bth amend`) — enables drift signal #3 | `cli.py`, warm tier | sidecar_sha256 | P3 |

---

## 11. Remaining Open Questions

**Q2: `parent_run_id` — manual or inferred?**  
Manual (`bth run --derived-from <run-id>`) is reliable but requires discipline. Auto-inference (if a run reads a registered output file from a prior run, auto-link) is powerful but fragile. Recommendation: manual first; defer inference.

**Q3: Sidecar enforcement timeline.**  
v0.1 warns; v0.2 blocks. When does the hard block go live? Early enforcement breaks existing scripts in registered projects; late enforcement means pre-registration never gets adopted. The `--no-sidecar` escape hatch (`sidecar_mode = bypassed`) makes hard enforcement less dangerous.

**Q4: Bypass rate threshold for campaign `review` flagging.**  
10% residual rate flagged as gate-malformed is a reasonable default. What is the acceptable bypass rate in a confirmation campaign before the verdict is considered unreliable? This may be project-configurable.

**Q5: `check_first_of_kind` definition.**  
"Script content-hash family" needs a precise definition. Options: (a) SHA256 of the script file at submission time, (b) git-tracked file identity (path + last-commit SHA), (c) directory class (any prior run from `scripts/experiments/` for the same project). Option (b) is most robust for researchers who rename scripts; option (a) is simplest. Decide before implementing `prereg.py`.

---

*Synthesized from: NotebookLM field research (2026-05-20, ~144 sources); code-architecture-advisor review (rounds 1–2); oracle strategic assessment (rounds 1–2); brainstorming session with researcher.*
