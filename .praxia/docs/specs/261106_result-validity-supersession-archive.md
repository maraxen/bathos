---
title: "Result Validity, Supersession, and Archive: Production Specification"
description: "Comprehensive production specification for task 261106_archive-supersession: instrument defect ranges with version backfill from uv.lock, read-time validity filtering & refusal gates across 13 surfaces, evidence tiers including spike, distance-from-present annotations, a lightweight findings register with supersession edges, session-start current beliefs view, batch archive sweeps, praxia-compatible document store, and backfill migration for asr and tev_design."
status: REVIEWED_SPEC
task_id: 261106_archive-supersession
phase: 3
authors:
  - "worker_spec (teamwork_worker)"
date: "2026-11-06"
version: "0.3.0"
---

# Result Validity, Supersession, and Archive: Production Specification

## 1. Executive Summary & Core Architecture

### 1.1 The Problem: Contamination and Circling in Research Repositories

Long-horizon scientific campaigns conducted across distributed clusters and autonomous agent sessions in `asr` and `tev_design` suffer from two chronic, crippling failure modes:

1. **Contamination (Silent Zombie Results)**:
   Results produced by a defective, uncalibrated, or since-changed instrument (chiefly the `aminx` MPNN wrapper and the corrupted `asr.lg_model` rate matrix) persist indefinitely in catalog files. When agents query the catalog via `bth ls`, `bth find`, `bth get`, `bth sql`, or `cite`, these invalid readings are dredged up and cited as authoritative truth. Because bathos previously recorded blast-radius flags and stale markers strictly on the write seam without consulting them on read paths, a fatally flawed result was indistinguishable from a pristine one at the moment an agent touched it.
2. **Circling (Amnestic Re-litigation)**:
   Because the repository lacked a machine-readable register of current beliefs ("what do we currently believe about scientific question $Q$, and why?"), settled design questions were repeatedly re-opened and re-litigated. Each agent session rebuilt the picture from scattered, dated markdown notes, landing on mutually contradictory conclusions and redundantly re-executing identical runs (e.g. `tev_design`'s `claims_ledger.toml` explicitly records an item *"twice proposed as new work because nothing here pointed at it"*).

### 1.2 System Invariants

This specification enforces four immutable architectural invariants:

1. **Invariant I1 — Read-Time Projection (21 CFR Part 11 Non-Obliteration)**:
   Hiding invalid, void, affected, superseded, or archived results is **strictly a read-time projection**. Historical ledger rows and cool-tier run fragments are **never mutated, overwritten, or deleted** to implement hiding (Anti-Goal A2). Historical runs remain permanently intact and accessible via explicit `--include-superseded` / `include_superseded=true` overrides.
2. **Invariant I2 — Gate Refusal Ratchet**:
   Any downstream gate that vouchsafes epistemic authority (`bth cite`, `claim register`, `claim validate`, `campaign add`, `campaign conclude`, `resolve_pin`, `get_trust_state`, `graduate_product`, and Maraxiom `review_build`) **must refuse** when presented with a result whose validity is `void`, `affected`, `superseded`, or `archived`. It cannot be bypassed by silent omission.
3. **Invariant I3 — Strict Provenance Distinction (Anti-Goal A1)**:
   Low-provenance history must not be erased. A run with an *inferred* dependency version (`version_source = "inferred-from-commit"`) matching a defect range caps at `suspect`—it is displayed with a warning banner and permitted through gates with an explicit acknowledgment token, but is **never silently hidden or voided**. Only cryptographically verified toolchains (`version_source = "verified-from-lock"` or recorded `dependency_provenance`) can resolve to `void`.
4. **Invariant I4 — Bounded Context Overhead (Anti-Goal A3)**:
   Agent startup information must never grow unbounded. The session-start current beliefs payload is strictly byte-capped at 2,048 bytes (2 KB / ~500 tokens), contains zero superseded or void items, and executes in $\le 100\text{ ms}$ (implemented via a lightweight stdlib-only fast-path script `bth-session-start-hook` reading `.bth/cache/session_start.json` in $\le 15\text{ ms}$).

---

### 1.3 Ontological Classifications & Validity States

#### A. Defect vs. Estimand Change

The system establishes a strict ontological separation between calculation flaws and measurement definition changes:

- **`defect` (Fatal Instrument Bug)**:
  The algorithm or code produced an incorrect mathematical result (e.g. `aminx <= 0.1.0a22` multi-sample AR sampling logits corruption when $S > 1$; `asr.lg_model` rate matrix where 152/190 exchangeabilities were mathematically erroneous). Runs matching a defect range are epistemically unsound: their validity resolves to `void` (or `suspect` if inferred).
- **`estimand_change` (Comparability Boundary Marker)**:
  The instrument was modified such that it computes a *different mathematical quantity*, but the calculation is mathematically sound under its own definition (e.g. `aminx` between a24 and a25 eliminated a diagonal self-leak of +0.036243 nats and switched to the conditional estimand charging all 21 tokens).
  **Rule**: An estimand change **MUST NEVER VOID OR HIDE A RUN**. Both sides of the boundary remain `current`. However, cross-boundary comparisons (e.g. pooling runs in a campaign, delta metrics, or findings citing runs from both sides) are strictly non-comparable and are refused at gates with error code `ESTIMAND_BOUNDARY_MIXED`.

#### B. The Seven Validity States

| State | Definition / Trigger | Default Read Visibility (`ls`, `find`, `get`) | Gate Behavior (`cite`, `claim`, `campaign`, `graduate`) |
|---|---|---|---|
| `void` | Matched instrument defect range with severity `void` on verified toolchain. | **Hidden** (counter incremented) | **Refused** (`RESULT_VOID`) |
| `affected` | Implicated by blast-radius ledger (`to_state = "affected"`). | **Hidden** (counter incremented) | **Refused** (`RESULT_AFFECTED`) |
| `superseded` | Replaced by a newer run/finding via a typed supersession edge (D5). | **Hidden** (counter incremented) | **Refused** (`RESULT_SUPERSEDED`) |
| `archived` | Script or outputs archived via `artifact_archive.py` (`archived_items`). | **Hidden** (counter incremented) | **Refused** (`RESULT_ARCHIVED`) |
| `suspect` | Defect match on *inferred* version, or explicit severity `suspect`. | **Shown + Banner** | **Allowed only with acknowledgment** (`acknowledged_suspect=True`) |
| `unverifiable` | Blast assessment inconclusive or lockfile hash corrupted. | **Shown + Banner** | **Allowed only with acknowledgment** (`acknowledged_unverifiable=True`) |
| `current` | Clean: no defects, no blast flags, unarchived, active finding/claim. | **Shown clean** | **Allowed clean** |

#### C. Version Source Resolution Hierarchy

```
[Run Execution Record]
        │
        ├── 1. `dependency_provenance` populated? ──────> YES: `recorded` (Highest confidence)
        │                      │ NO
        ├── 2. `git show <hash>:uv.lock` SHA matches ──> YES: `verified-from-lock`
        │      recorded `dependency_lock_sha256`?
        │                      │ NO
        ├── 3. `uv.lock` at `git_hash` parses ─────────> YES: `inferred-from-commit`
        │      (Note: `git_dirty = True` tags dirty)          (Defect matches cap at `suspect`)
        │                      │ NO
        └── 4. Toolchain unresolvable ─────────────────> YES: `unknown` (Treated as `unverifiable`)
```

---

### 1.4 Architectural Decisions: Owner Decisions vs. Designer Calls

Per project directives, decisions marked `(owner: ...)` are fixed and fully implemented. Designer calls from Section 4 and Section 7 of the input design document are evaluated, attacked, and settled below:

#### Attack 1: Shadowing `runs` as a Current-Only View in `run_sql` (§4 D10)
- **Designer Call**: Replace the base DuckDB `runs` table with a current-only view, renaming full history to `runs_all`.
- **Attack & Critique**: Silently shadowing `runs` fundamentally breaks external scripts, saved queries, and automated audits across `asr` and `tev_design`. For example, historical sanity checks (`SELECT count(*) FROM runs`) or scripts verifying run IDs from SLURM logs would return fewer rows or fail unexpectedly. Furthermore, internal maintenance tools (`migrate`, `repair`, `compact`, `verify`) could accidentally query the filtered view if not rigorously rewired.
- **Settlement & Resolution**:
  1. Create a dedicated view **`runs_current`** filtering out hidden states:
     ```sql
     CREATE VIEW runs_current AS
     SELECT r.* FROM runs r
     WHERE r.id NOT IN (
         SELECT run_id FROM validity_cache
         WHERE validity_state IN ('void', 'affected', 'superseded', 'archived')
     );
     ```
  2. In all documentation, agent instructions, CLI help text, and MCP tool hints, establish `runs_current` as the primary query target.
  3. Keep the base table **`runs`** queryable as the complete, unmuted historical ledger.
  4. Notice Banner on Base Table Queries: Banner inspection cannot depend on inspecting returned row IDs, because aggregate queries (`SELECT count(*) FROM runs`, `SELECT avg(duration_s) FROM runs`) and non-ID projections return scalars or metrics without row IDs. Instead, `run_sql` inspects the query string at parse/analysis time: if the query references the unmuted base table `runs` (`\bFROM\s+runs\b|\bJOIN\s+runs\b`), the execution engine checks if any void, affected, superseded, or archived rows exist in the project catalog's `validity_cache`. If hidden rows exist:
     - For `bth sql` CLI: print a prominent stderr notice:
       `"Notice: Query targets unmuted historical table 'runs' (N void/superseded/archived runs present). Query 'runs_current' for current results, or pass --allow-hidden to suppress this notice."`
     - For MCP `run_sql_tool`: include `"notice"` in the return envelope.
     - Passing `allow_hidden=True` (`--allow-hidden`) suppresses the notice banner.

#### Attack 2: `estimand_change-as-comparability-marker` Semantics (§4 D1)
- **Designer Call**: Separate `defect` from `estimand_change`, marking estimand boundaries as comparability markers.
- **Confirmation & Rigor**: Confirmed. An estimand change (such as `aminx` moving from conditional to joint sampling, or eliminating an intentional score-path diagonal) is mathematically valid under its revised assumptions. Voiding historical runs would falsely brand sound physics as software bugs. The system records the estimand boundary, preserves `validity = "current"` on both sides, and strictly blocks cross-boundary delta calculations.

#### Settlement of Section 7 Open Items:
- **7.1 Defect Registry Storage & Hierarchy**:
  Stored as TOML files under `.bth/instruments/<pkg>.toml` for project-local defects, with fallback/overlay support for user-global defects at `${XDG_CONFIG_HOME:-~/.config}/bathos/instruments/<pkg>.toml` (or `~/.bth/instruments/<pkg>.toml`). Active entries automatically populate `track_packages` in the runner.
- **7.2 Version Resolution Caching**:
  Cached in the DuckDB index database under table `version_cache` and mirrored in an atomic JSON file at `.bth/cache/version_cache.json` for cool-tier readers. Keyed on `(git_hash, lock_sha256, package_name)`.
- **7.3 Findings Storage Format**:
  Stored as individual TOML files in `.bth/findings/<slug>.toml` containing stable question slugs, answers, evidence tier, and machine-followable UUIDs.
- **7.4 Reopening Protocol for Settled Findings**:
  Attempting to declare a new finding with a question slug matching an existing `settled` finding without an explicit `--reopen-reason` parameter is strictly refused (`FINDING_REOPEN_REASON_REQUIRED`). Reopening is initiated at finding creation time (not raw run execution time), recording intent to challenge: the newly created finding enters `tentative` status linked via `supersedes`. The predecessor finding remains `settled` until the replacement finding concludes and reaches `status = "settled"` with confirmatory evidence, at which point the supersession edge activates and the predecessor transitions to `superseded`.
- **7.5 SessionStart Performance Budget**:
  Hard cap: **2,048 bytes** payload; maximum **100 ms** execution timeout. Executed via a dedicated stdlib-only console script `bth-session-start-hook` reading pre-rendered cache `.bth/cache/session_start.json`, completing in $\le 15\text{ ms}$ and emitting Claude Code hook JSON `{"systemMessage": "..."}`.
- **7.6 Telemetry Schema**:
  Emits `validity.resolved`, `validity.filter_applied`, and `validity.override_used` events to `.bth/log/`.

#### Numerical Threshold Justifications:
- **Session-Start Byte Cap (2,048 bytes / 2 KB)**:
  *Justification*: 2 KB corresponds to ~500 tokens. In multi-turn subagent sessions using compact models (e.g. Claude 3.5 Sonnet or Haiku), a session-start preamble must consume $< 1\%$ of the context window to prevent early context truncation while conveying all active alerts and settled beliefs.
- **SessionStart Hook Timeout (100 ms)**:
  *Justification*: Shell hook execution occurs synchronously on every session initialization or tool dispatch. Latencies $> 150\text{ ms}$ introduce noticeable interactive typing stutter and agent execution lag.
- **Archive Sweep Age $N = 90$ days**:
  *Justification*: Explicitly labeled **arbitrary, revisit after P1 dry-run counts**. 90 days represents a standard quarterly research cycle; scripts unreferenced for $> 90$ days without active findings are prime candidates for archival bundling.
- **Telemetry Rotation Threshold (10 MB)**:
  *Justification*: A 10 MB uncompressed JSONL log file contains ~25,000–35,000 events. Parsing files $> 10\text{ MB}$ with `jq` or Python standard library incurs multi-second latency spikes. Rotating at 10 MB into `.tar.zst` maintains instant grep/scan speeds while preserving complete event history.

---

## 2. Core Architectural Substrates & Choke Points

### 2.1 The Central Choke Point: `resolve_validity`

To guarantee that CLI commands, MCP tools, and internal gates evaluate identical validity logic without semantic drift, all surfaces call a single batched resolver in `src/bathos/validity.py`:

```python
@dataclass(frozen=True)
class ValidityResult:
    run_id: str
    state: str  # "current" | "suspect" | "unverifiable" | "void" | "affected" | "superseded" | "archived"
    reason: str | None
    replacement_id: str | None
    version_source: str  # "recorded" | "verified-from-lock" | "inferred-from-commit" | "unknown"
    evidence_tier: str   # "confirmatory" | "calibrating" | "exploratory" | "spike" | "undeclared"
    is_hidden: bool      # True if state in ("void", "affected", "superseded", "archived")
    defect_id: str | None = None
    estimand_marker: str | None = None

def resolve_validity(
    run_ids: list[str],
    catalog_dir: Path | str,
    db: duckdb.DuckDBPyConnection | None = None,
) -> dict[str, ValidityResult]:
    """Resolve validity states for a batch of run IDs via derived cache and active ledgers."""
```

#### Evaluation Precedence within `resolve_validity`:
1. Check `archived_items` ledger: if latest status is `"archived"`, state is `archived`.
2. Check `supersession_edges`: if an active supersession edge targets this run, state is `superseded` (capturing `replacement_id`).
3. Check `blast_radius_ledger`: if latest fold is `"affected"`, state is `affected`; if `"unverifiable"`, state is `unverifiable`.
4. Check active instrument defect registries against run's resolved package versions:
   - If version matches defect interval: evaluate SQL predicate condition.
     - **Safe Predicate Evaluation & BinderException Guard**:
       Predicates evaluate DuckDB SQL expressions against run metadata. To prevent `_duckdb.BinderException` crashes when parameters (such as `sample_s`) are missing from a run's metadata, evaluation is guarded:
       1. Predicate queries use safe extraction, e.g. `COALESCE(try_cast(json_extract(metadata, '$.sample_s') AS DOUBLE), 0.0) > 1.0`.
       2. In Python `src/bathos/validity.py`, predicate execution wraps `duckdb.execute(...)` in `try ... except duckdb.BinderException:`.
       3. If a column referenced in the condition is missing:
          - If the defect entry defines a default value under `[entries.predicate.defaults]` (e.g. `sample_s = 1`), the evaluator binds that default value and re-evaluates.
          - If no default is defined, missing attributes are treated as non-matching (evaluates to `False`), ensuring `resolve_validity` never crashes on older or exploratory runs lacking the parameter.
          - If a condition is marked required/strict and cannot be verified due to missing attributes, state resolves to `suspect` with reason `"missing predicate field: <field>"`.
   - If version matches defect interval AND predicate evaluates to `True`:
     - If `version_source` is `"verified-from-lock"` or `"recorded"`: state is `void` (or entry's `severity`).
     - If `version_source` is `"inferred-from-commit"`: state is capped at `suspect`.
5. If no defect, supersession, blast flag, or archive record matches: state is `current`.

---

### 2.2 Materialized Cache Architecture

To maintain sub-millisecond query latencies across catalogs with $> 10,000$ runs without violating Invariant I1 (no mutation of historical rows), derived states are cached in DuckDB table `validity_cache`:

```sql
CREATE TABLE IF NOT EXISTS validity_cache (
    run_id TEXT PRIMARY KEY,
    validity_state TEXT NOT NULL,
    reason TEXT,
    replacement_id TEXT,
    version_source TEXT NOT NULL,
    evidence_tier TEXT NOT NULL,
    defect_id TEXT,
    estimand_marker TEXT,
    inputs_hash TEXT NOT NULL,
    cached_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_validity_state ON validity_cache(validity_state);
```

- **Cache Invalidation Rule**:
  `inputs_hash = SHA256(registry_dir_mtime || blast_ledger_max_amended || archive_ledger_max_recorded || supersession_max_declared)`.
  If the computed `inputs_hash` differs from the cache generation hash, the cache is refreshed via batch evaluation.

---

## 3. Phase 1 (P1): Stop the Leak

### 3.1 Scope & Objectives
P1 stops contaminated results from reaching agents. It implements the defect registry (D1), version resolution and runner auto-population (D2), read-time filtering and gate refusals across all 13 mapped surfaces (D3), and distance-from-present calculation (D5).

---

### 3.2 Data Models & Schemas

#### A. Instrument Defect Registry Schema (`.bth/instruments/<package>.toml`)
```toml
# Schema: bathos-instrument-defect-v1.0
schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "AMINX-2026-001"
kind = "defect"
severity = "void"
summary = "Autoregressive multi-sample corruption when S > 1 on <= a22"
evidence = "wheels/AMINX_PIN.md:400"
declared_at = "2026-11-06T12:00:00Z"
declared_by = "owner"

[[entries.ranges]]
type = "ECOSYSTEM"
events = [
    { introduced = "0.1.0a0" },
    { fixed = "0.1.0a23" }
]

[entries.predicate]
condition = "sample_s > 1"
reasoning = "Single-sample (S=1) argmax sampling is mathematically unaffected"

[entries.predicate.defaults]
sample_s = 1

[[entries]]
id = "AMINX-2026-002"
kind = "estimand_change"
severity = "current"
summary = "Diagonal self-leak elimination between a24 and a25 changed computed quantity"
evidence = "tev_design/scripts/audit/claims_ledger.toml"
declared_at = "2026-11-06T12:00:00Z"
declared_by = "owner"

[[entries.ranges]]
type = "ECOSYSTEM"
events = [
    { introduced = "0.1.0a25" }
]
```

#### B. Schema Evolution & Log-Mode Integration (Version 20)

##### Invariant I1 Reaffirmation: No Cached Validity in Run Records
Per Invariant I1, hiding and validity states are strictly read-time projections. Writing a `cached_validity_state` column into historical run records or cool-tier Parquet fragments would violate Invariant I1 and embed stale snapshots. Therefore, **`cached_validity_state` is completely omitted from the `Run` dataclass, `COOL_SCHEMA`, and `WARM_SCHEMA`**. Validity state lives exclusively in the derived `validity_cache` table.

##### Log-Mode vs. Legacy Migration Reconciliation
Modern bathos repositories (`asr`, `tev_design`) operate in **log mode** (`.bth/log/*.jsonl` folded into `index.db`). In log mode, `src/bathos/migrate.py:58-66` actively raises `ValueError` ("fragment schema upgrade is refused in log mode: cool-tier fragments are frozen legacy sources...").

Schema evolution for log mode is handled as follows:
1. **`src/bathos/index.py:RUNS_DDL` & `RUNS_COLUMNS`**:
   Add new nullable column `toolchain_version_source VARCHAR` to `RUNS_DDL` and `RUNS_COLUMNS`.
2. **`src/bathos/runlog/fold_runs.py:fold_run()`**:
   Extract `toolchain_version_source` from run events or backfills (`row["toolchain_version_source"] = ...`) during fold. When `index.db` is refreshed or regenerated, DuckDB reflects the new column with zero fragment rewriting.
3. **Legacy Cool-Tier Migration (`src/bathos/migrate.py`)**:
   In legacy non-log mode, `src/bathos/schema.py` bumps `CURRENT_SCHEMA_VERSION = "20"` with only:
   ```python
   pa.field("toolchain_version_source", pa.string(), nullable=True),
   ```
   and `_migrate_v19_to_v20` sets `toolchain_version_source = None` across existing fragments when not in log mode.

---

### 3.3 Runner Fix: Automatic Population of `dependency_provenance`

- **Root Cause (from Recon)**: `resolve_tracked_packages()` returns `[]` because neither repo declared `track_packages`.
- **Fix in `src/bathos/runner.py` and `src/bathos/dependency_provenance.py`**:
  Update `resolve_tracked_packages` to union declared packages with **all packages declared in active defect registries**:
  ```python
  def resolve_tracked_packages(sidecar_path: Path | None, start: Path) -> list[str]:
      explicit = _resolve_explicit_tracked_packages(sidecar_path, start)
      registry_pkgs = load_instrument_registry_packages(start)
      return sorted(set(explicit) | set(registry_pkgs))
  ```

---

### 3.4 Exact Insertion Points for D3 Validity Checks Across All 13 Surfaces

```
                                  ┌────────────────────────┐
                                  │   resolve_validity()   │
                                  │ (Batched Cache / Fold) │
                                  └───────────┬────────────┘
                                              │
         ┌────────────────────────────────────┼────────────────────────────────────┐
         │ Read Surfaces                      │ Gate / Registration Surfaces       │ Downstream Bridges
         ▼                                    ▼                                    ▼
┌──────────────────┐                 ┌──────────────────┐                 ┌──────────────────┐
│ 1. list_runs     │                 │ 5. cite_run      │                 │ 8. resolve_pin   │
│ (query.py:259)   │                 │ (mcp.py:2501)    │                 │ (readback.py:113)│
├──────────────────┤                 ├──────────────────┤                 ├──────────────────┤
│ 2. find_runs     │                 │ 6. claim_register│                 │ 9. get_trust_stat│
│ (query.py:610)   │                 │ (claim.py:604)   │                 │ (readback.py:253)│
├──────────────────┤                 ├──────────────────┤                 ├──────────────────┤
│ 3. get_run / show│                 │ 6. claim_validate│                 │ 10. graduate_prod│
│ (query.py:466)   │                 │ (claim.py:232)   │                 │ (trust_ledger:308│
├──────────────────┤                 ├──────────────────┤                 ├──────────────────┤
│ 4. run_sql       │                 │ 7. campaign_add  │                 │ 11. lineage      │
│ (query.py:773)   │                 │ (campaigns:420)  │                 │ (query.py:833)   │
├──────────────────┤                 ├──────────────────┤                 ├──────────────────┤
│                  │                 │ 7. campaign_concl│                 │ 12. review_build │
│                  │                 │ (campaigns:1139) │                 │ (maraxiom bridge)│
│                  │                 ├──────────────────┤                 ├──────────────────┤
│                  │                 │ 7. campaign_rev  │                 │ 13. SessionStart │
│                  │                 │ (campaigns:1947) │                 │ (hooks.json)     │
└──────────────────┘                 └──────────────────┘                 └──────────────────┘
```

1. **`list_runs` (`src/bathos/query.py:259`, `src/bathos/mcp.py:330`)**:
   Add `include_superseded: bool = False` parameter. To avoid under-filling pagination when filtering after `LIMIT` (Finding M2), push validity filtering directly down into the DuckDB query in `_index_list_runs` and `_warm_list_runs`:
   ```sql
   WHERE id NOT IN (
       SELECT run_id FROM validity_cache
       WHERE validity_state IN ('void', 'affected', 'superseded', 'archived')
   )
   ```
   (or query view `runs_current`) *before* applying `LIMIT {limit}`. This guarantees that queries return the requested number of active runs rather than truncated pages. Append notice metadata if hidden runs exist.
2. **`find_runs` (`src/bathos/query.py:610`, `src/bathos/mcp.py:402`)**:
   Add `include_superseded: bool = False`. Push the validity predicate directly into the DuckDB query before `LIMIT`.
3. **`get_run` / `show` (`src/bathos/query.py:466`, `src/bathos/mcp.py:457`)**:
   In `get_run_tool()`, resolve validity for the run. If `is_hidden=True` and `include_superseded=False`, refuse with `BathosErrorCode.RESULT_HIDDEN`. If `suspect` or `unverifiable`, return run with an explicit warning banner dictionary.
4. **`run_sql` (`src/bathos/query.py:773`)**:
   Define `runs_current` view filtering out hidden IDs. Keep `runs` base table intact. Notice banner trigger operates at query analysis time (parsing for `\bFROM\s+runs\b|\bJOIN\s+runs\b`) rather than inspecting returned row IDs, ensuring aggregate queries (`SELECT count(*) FROM runs`) also trigger warnings if hidden runs exist in the catalog. Emits stderr notice or attaches `"notice"` in MCP return envelope unless `allow_hidden=True`.
5. **`cite` (`src/bathos/cite.py:13`, `src/bathos/mcp.py:2501`)**:
   In `cite_run_tool()`, call `resolve_validity([run_id])`. If `is_hidden=True`, refuse with `BathosErrorCode.RESULT_VOID` (or `RESULT_HIDDEN`). If `suspect` or `unverifiable`, refuse unless `acknowledged_suspect=True` or `acknowledged_unverifiable=True`.
6. **`claim register` (`src/bathos/claim.py:604`) & `claim validate` (`src/bathos/claim.py:232`)**:
   In `register_claim()` (line 657) and `validate_claim()` (line 248), call `resolve_validity(member_ids)`. If any member run backing a clause is hidden, refuse with `BathosErrorCode.RESULT_HIDDEN` naming the invalid runs and defect reasons.
7. **`campaign add` (`src/bathos/campaigns.py:420`), `conclude` (`:1139`), `review` (`:1947`)**:
   - `add_run_to_campaign`: Refuse adding a run if `is_hidden=True`.
   - `conclude_campaign`: Union Gate check refuses conclusion if any covering run is hidden.
   - `review_campaign`: Report breakdown of current vs hidden runs; display banners for hidden members.
8. **`resolve_pin` (`src/bathos/readback.py:113`)**:
   At line 140, resolve validity of producing `run_id`. If hidden, return `fresh=False`, `trust_state="unknown"`, and populate `validity_state` with defect summary.
9. **`get_trust_state` (`src/bathos/readback.py:253`)**:
   At line 262, evaluate validity across producing runs. When multiple runs produced the same content hash (e.g. an original run followed by a clean reproduction), scan all producing runs: drop trust state from `promoted` to `candidate` (or `unknown`) ONLY if ALL producing runs are invalid. If at least one producing run is `current`, trust state is preserved.
10. **`graduate_product` (`src/bathos/trust_ledger.py:308`)**:
    At line 364, resolve validity of producing `run_id`. Refuse graduation with `BathosErrorCode.GRADUATION_REFUSED` if run is hidden.
11. **`lineage` (`src/bathos/query.py:833`, `src/bathos/mcp.py:2547` `lineage_prov_tool`)**:
    At `src/bathos/query.py:855`, update the catalog existence check from `(catalog_dir / "bathos.db").exists()` to `if not catalog_readable(catalog_dir): return []` so log-mode catalogs (`index.db`) are supported. At line 880, annotate each ancestor in CTE chain with `validity_state`. Retain complete causal history but attach banner metadata to hidden ancestors.
12. **Maraxiom `review_build` Bridge (`maraxiom/src/maraxiom/bathos_bridge.py:1240`)**:
    Refuse review page build (`BathosErrorCode.RESULT_HIDDEN`) if the target run or supporting run is hidden.
13. **SessionStart Hook (`plugin/hooks/hooks.json`)**:
    Dispatches fast-path console script `bth-session-start-hook` (see Phase 2).

---

### 3.5 Distance From Present Calculation (D5)

In `src/bathos/validity.py`, compute distance facts on every run read:
- **`age_days`**: Elapsed days since `run.timestamp`.
- **`versions_behind`**: Number of released package versions between run's resolved version and current workspace pin.
- **`script_commits_since`**: Count of git commits modifying `run.sidecar_path` or script between `run.git_hash` and `HEAD`.
- **`reproduction_count`**: Number of subsequent `current` runs sharing `sidecar_sha256` that achieved the identical outcome.
- **`version_source`**: Exact resolution provenance.

---

### 3.6 CLI & FastMCP Function Signatures (P1)

```python
# In src/bathos/mcp.py:

@cisternal.tool(registry="bathos-cli", name="list_runs", cli_group="run", cli_name="list")
def list_runs_tool(
    project: str = "",
    status: str = "",
    limit: int = 50,
    include_superseded: bool = False,
    catalog_dir: str = "",
) -> dict: ...

@cisternal.tool(registry="bathos-cli", name="find_runs", cli_group="run", cli_name="find")
def find_runs_tool(
    project: str = "",
    status: str = "",
    since: str = "",
    tags: list[str] | None = None,
    slurm_job: str = "",
    output_file: str = "",
    include_superseded: bool = False,
    catalog_dir: str = "",
) -> dict: ...

@cisternal.tool(registry="bathos-cli", name="get_run", cli_group="run", cli_name="show")
def get_run_tool(
    run_id: str,
    include_superseded: bool = False,
    catalog_dir: str = "",
) -> dict: ...

@cisternal.tool(registry="bathos-cli", name="cite_run", cli_group="run", cli_name="cite")
def cite_run_tool(
    run_id: str,
    format: str = "markdown",
    acknowledged_suspect: bool = False,
    acknowledged_unverifiable: bool = False,
    catalog_dir: str = "",
) -> dict: ...

@cisternal.tool(registry="bathos-cli", name="defect_declare", cli_group="instrument", cli_name="declare-defect")
def defect_declare_tool(
    package: str,
    defect_id: str,
    introduced: str,
    fixed: str,
    kind: str = "defect",
    severity: str = "void",
    predicate: str = "",
    summary: str = "",
    evidence: str = "",
    catalog_dir: str = "",
) -> dict: ...

@cisternal.tool(registry="bathos-cli", name="defect_list", cli_group="instrument", cli_name="list-defects")
def defect_list_tool(
    package: str = "",
    catalog_dir: str = "",
) -> dict: ...
```

---

### 3.7 Refusal Codes & Resolution Hints (P1)

In `src/bathos/errors.py`:
```python
RESULT_VOID = "result_void"
RESULT_AFFECTED = "result_affected"
RESULT_SUPERSEDED = "result_superseded"
RESULT_ARCHIVED = "result_archived"
RESULT_HIDDEN = "result_hidden"
RESULT_SUSPECT_UNACKNOWLEDGED = "result_suspect_unacknowledged"
RESULT_UNVERIFIABLE_UNACKNOWLEDGED = "result_unverifiable_unacknowledged"
ESTIMAND_BOUNDARY_MIXED = "estimand_boundary_mixed"
```

Resolution hints in `RESOLUTION_HINTS`:
- `RESULT_VOID`: *"The requested run was produced by a defective instrument version and is void. Query runs_current or use a replacement run. Pass --include-superseded to inspect history."*
- `RESULT_AFFECTED`: *"The requested run is implicated by an active blast-radius ledger entry. Re-run or clear the flag with audit justification."*
- `RESULT_SUPERSEDED`: *"The requested run was superseded by a newer run. Use --include-superseded to inspect, or query the successor run."*
- `RESULT_ARCHIVED`: *"The requested script or outputs were archived. Use `bth archive restore <id>` to restore files to workspace."*
- `RESULT_HIDDEN`: *"The requested run is hidden by default due to void, affected, superseded, or archived status. Pass --include-superseded to inspect."*
- `RESULT_SUSPECT_UNACKNOWLEDGED`: *"Run was produced by an unverified toolchain matching a defect range. To cite, pass acknowledged_suspect=True with an explicit rationale."*
- `RESULT_UNVERIFIABLE_UNACKNOWLEDGED`: *"Run provenance cannot be verified against repository lockfiles. To cite, pass acknowledged_unverifiable=True."*
- `ESTIMAND_BOUNDARY_MIXED`: *"Attempted to combine runs across an estimand change boundary. Results compute fundamentally different quantities and cannot be pooled."*

---

### 3.8 Acceptance Criteria (P1)

- **AC-P1-01**: Declaring a defect range in `.bth/instruments/aminx.toml` with `introduced = "0.1.0a0"`, `fixed = "0.1.0a23"`, and predicate `sample_s > 1` causes all matching runs on verified lockfiles to resolve to `state = "void"`.
- **AC-P1-02**: A run matching AC-P1-01 with `sample_s = 1` evaluates the predicate to False and resolves to `state = "current"`.
- **AC-P1-03**: A run matching AC-P1-01 executed with an unverified git commit (`version_source = "inferred-from-commit"`) resolves to `state = "suspect"` and is never hidden.
- **AC-P1-04**: `bth ls` and `list_runs_tool` exclude `void`, `affected`, `superseded`, and `archived` runs by default and print `N results hidden — pass --include-superseded to show`.
- **AC-P1-05**: `bth show <id>` on a void run raises `CatalogError` / returns error code `RESULT_HIDDEN` naming defect ID and evidence.
- **AC-P1-06**: `bth show <id> --include-superseded` displays the void run with a prominent `[VOID]` banner.
- **AC-P1-07**: `bth cite <id>` refuses a void run with `RESULT_VOID`.
- **AC-P1-08**: `bth cite <id>` on a `suspect` run refuses with `RESULT_SUSPECT_UNACKNOWLEDGED` unless `--acknowledged-suspect` is provided.
- **AC-P1-09**: `claim register` refuses if any supporting member run resolves to a hidden state.
- **AC-P1-10**: `campaign conclude` refuses at Union Gate if any member run resolves to a hidden state.
- **AC-P1-11**: `graduate_product` refuses graduation if producing run resolves to a hidden state.
- **AC-P1-12**: `get_trust_state` drops a previously promoted product to `candidate` at read time if producing run becomes void, with zero modifications to `trust_ledger` files.
- **AC-P1-13**: Invariant test: executing `resolve_validity` across 1,000 runs produces zero file modifications in cool-tier Parquet or warm DuckDB files.
- **AC-P1-14**: `SELECT * FROM runs_current` never returns a row where `validity_state IN ('void', 'affected', 'superseded', 'archived')`.
- **AC-P1-15**: `run_sql` on base table `runs` when catalog contains hidden rows emits a structured warning banner to stderr (CLI) or attaches `"notice"` in the MCP response envelope unless `allow_hidden=True`.
- **AC-P1-16**: Runner automatically populates `dependency_provenance` when executed in a workspace with active instrument defect files.
- **AC-P1-17**: `lineage` returns all ancestors but flags void ancestors with banner metadata.
- **AC-P1-18**: Pooling runs across an `estimand_change` boundary in `campaign add` or `conclude` fails with `ESTIMAND_BOUNDARY_MIXED`.

---

### 3.9 Fixer Task Decomposition (P1)

```
[Task P1.1: Error Codes & Schemas]
       │
       ▼
[Task P1.2: Validity Engine & Cache] ◄── [Task P1.3: Version Resolver & Runner Fix]
       │
       ├─────────────────────────────────┬─────────────────────────────────┐
       ▼                                 ▼                                 ▼
[Task P1.4: Query Surfaces Filtering]  [Task P1.5: Citation & Claim Gates] [Task P1.6: Trust Ledger Seams]
       │                                 │                                 │
       └─────────────────────────────────┼─────────────────────────────────┘
                                         ▼
                               [Task P1.7: P1 Integration Tests]
```

1. **Task P1.1**: Add P1 error codes to `src/bathos/errors.py` and register non-empty resolution hints. Bump schema version to 20 in `src/bathos/schema.py` and `migrate.py` (legacy fragments: nullable `toolchain_version_source` only; no `cached_validity_state`); for log mode, add `toolchain_version_source` to `src/bathos/index.py:RUNS_DDL` and `fold_runs.py`.
2. **Task P1.2**: Author `src/bathos/validity.py` containing `ValidityResult`, `resolve_validity()`, safe DuckDB predicate evaluator with `BinderException` guard, and `validity_cache` management.
3. **Task P1.3**: Author `src/bathos/version_resolver.py` implementing 4-tier version resolution. Update `src/bathos/runner.py` and `dependency_provenance.py` to auto-track registry packages.
4. **Task P1.4**: Wire `resolve_validity` push-down filtering into `src/bathos/query.py` (`list_runs`, `find_runs`, `get_run`, `run_sql`, `lineage`) and update `src/bathos/mcp.py` tools (`list_runs_tool:330`, `find_runs_tool:402`, `get_run_tool:457`, `lineage_prov_tool:2547`). Update `src/bathos/cli_cyclopts.py:expected` with new `@cisternal.tool` names `defect_declare` and `defect_list`. Create `runs_current` view in `src/bathos/index.py`.
5. **Task P1.5**: Wire validity gates into `src/bathos/cite.py`, `src/bathos/claim.py`, and `src/bathos/campaigns.py`.
6. **Task P1.6**: Wire validity check into `src/bathos/readback.py` (`resolve_pin`, `get_trust_state`) and `src/bathos/trust_ledger.py` (`graduate_product`).
7. **Task P1.7**: Comprehensive test suite under `tests/test_validity_p1.py`.

---

## 4. Phase 2 (P2): Findings + Evidence Tiers

### 4.1 Scope & Objectives
P2 provides the positive counterweight to invalidation: recording current beliefs and evidence certainty to prevent circular re-litigation. It implements evidence tiers including `spike` (D4), typed supersession edges (D6), the lightweight findings register with an automated downgrade ladder (D7), and the byte-capped session-start view (D7).

---

### 4.2 Evidence Tiers & Vocabulary Reconciliation (D4)

#### A. Canonical Epistemic Hierarchy (GRADE-Inspired)
1. **`confirmatory`**: Pre-registered, hypothesis-testing runs with rigorous controls and replication.
2. **`calibrating`**: Parameter sweeps, assay titrations, and baseline calibrations.
3. **`exploratory`**: Hypothesis-generating probes, initial feasibility screens.
4. **`spike` (New)**: Throwaway assumption probes; provenance is recorded, but a spike run **CAN NEVER SUPPORT A FINDING ABOVE `tentative`** and is excluded from session-start summaries.

#### B. Reconciliation of Stage Names to Epistemic Tiers
In `src/bathos/sidecar.py`:
```python
STAGE_TO_EPISTEMIC_TIER: dict[str, str] = {
    # Canonical stages
    "validation": "confirmatory",
    "ablation": "confirmatory",
    "production": "confirmatory",
    "calibration": "calibrating",
    "exploration": "exploratory",
    "spike": "spike",
    # Aliased in-the-wild stages (E9 reconciliation)
    "confirmatory": "confirmatory",
    "power": "confirmatory",
}

def resolve_epistemic_tier(stage_name: str | None, sidecar_class: str | None) -> str:
    if sidecar_class in ("confirmatory", "calibrating", "exploratory", "spike"):
        return sidecar_class
    if stage_name and stage_name in STAGE_TO_EPISTEMIC_TIER:
        return STAGE_TO_EPISTEMIC_TIER[stage_name]
    return "undeclared"  # Displayed explicitly; does not silently default to exploration
```

---

### 4.3 Typed Supersession Edges (D6)

Supersession edges stop being free-text strings in sidecars. They are stored as append-only records in DuckDB and folded from the event log:

```sql
CREATE TABLE IF NOT EXISTS supersession_edges (
    id TEXT PRIMARY KEY,
    entity_type TEXT NOT NULL,         -- "run" | "finding" | "sidecar" | "doc"
    entity_id TEXT NOT NULL,
    superseded_by_type TEXT NOT NULL,
    superseded_by_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    declared_by TEXT NOT NULL,
    declared_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_supersession_entity ON supersession_edges(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_supersession_successor ON supersession_edges(superseded_by_type, superseded_by_id);
```

#### Log-Mode Event Emission & Concurrency (Finding M8)
In log mode, declaring a supersession edge wraps execution in `unit_of_work(catalog_dir)` and appends an event to `.bth/log/*.jsonl`:
```python
emit_event(
    "supersession.declared",
    {
        "edge_id": edge_id,
        "entity_type": entity_type,
        "entity_id": entity_id,
        "superseded_by_type": superseded_by_type,
        "superseded_by_id": superseded_by_id,
        "reason": reason,
        "declared_by": declared_by,
    },
    catalog_dir=catalog_dir,
)
```
In `src/bathos/index.py`, `init_index_schema()` adds the `supersession_edges` DDL, and `src/bathos/runlog/fold_runs.py` / `ingest.py` folds `supersession.declared` events into the `supersession_edges` table during index generation swaps. In legacy non-log mode, edges are written directly to DuckDB table `supersession_edges`. This maintains strict log-mode concurrency safety on cluster nodes and local processes.

---

### 4.4 Findings Register & Automated Downgrade Ladder (D7)

#### A. Data Model (`.bth/findings/<slug>.toml`)
```toml
# Schema: bathos-finding-v1.0
id = "finding-4f9a-8e2b-11ec"
question_slug = "tev-site-stability"
question = "Does the engineered TEV protease loop maintain structural stability at 37C?"
answer = "Yes. Loop conformation remains rigid with RMSD < 1.2A across 50ns NVT simulations."
status = "settled"  # "open" | "tentative" | "settled" | "superseded" | "refuted" | "needs-recheck"
supporting_runs = [
    "run_7b12d8a4-1234-5678-90ab-cdef12345678",
    "run_8c23e9b5-2345-6789-01bc-def234567890"
]
evidence_tier = "confirmatory"
supersedes = "finding-1a2b-3c4d-5e6f"
declared_at = "2026-11-06T14:30:00Z"
declared_by = "marielle"
last_reviewed = "2026-11-06T14:30:00Z"
```

#### B. Dynamic Downgrade Ladder
When reading a finding via `bth findings get` or `session-start`:
1. Resolve validity of all `supporting_runs`.
2. **Automated Downgrade Ladder (Finding M5)**:
   - If `supporting_runs` is non-empty (`len(supporting_runs) > 0`) AND **ALL** supporting runs are hidden (`void`, `affected`, `superseded`, or `archived`), finding status dynamically resolves to **`needs-recheck`** (applicable only to active findings with status `settled`, `tentative`, or `refuted`).
   - An `open` finding with `supporting_runs = []` retains `status = "open"` (preventing vacuous truth `all(...) == True` from falsely downgrading open questions before experiments run).
   - Findings with `status = "superseded"` never downgrade to `needs-recheck` (retired findings stay retired).
3. If supporting runs contain only `exploratory` or `spike` runs, finding status is capped at **`tentative`** (cannot be `settled`).
4. To reach **`settled`**, a finding requires $\ge 1$ active `confirmatory` run.

#### C. The Reopen Guard & Lifecycle (Finding M6)
Attempting to create a new finding whose `question_slug` matches an existing `settled` finding without an explicit `--reopen-reason` parameter is strictly refused:
```
Refusal: Question 'tev-site-stability' was settled on 2026-11-06:
Answer: "Yes. Loop conformation remains rigid with RMSD < 1.2A across 50ns NVT simulations."
To reopen a settled question, pass --reopen-reason "<rationale>" linking to new evidence.
```
Error code: `FINDING_REOPEN_REASON_REQUIRED`.

**Reopen Lifecycle & Knowledge Preservation**:
Reopening is declared at finding creation time, NOT raw experiment launch time. Providing `--reopen-reason` creates a new finding in `tentative` status linked to the predecessor via `supersedes`. The existing `settled` finding remains intact and settled until the challenging investigation concludes and the new finding achieves `confirmatory` evidence and transitions to `status = "settled"`. Only upon reaching `settled` does the supersession edge activate to mark the predecessor finding as `superseded` (preventing premature destruction of institutional knowledge if the challenge experiment fails or crashes).

---

### 4.5 Session-Start Current Beliefs View (D7)

#### Fast-Path Hook Execution Architecture (Finding B1)
Because cold initialization of the full `bth` CLI (`bathos.cli_cyclopts`) requires importing heavy numerical libraries (`duckdb`, `pyarrow`, `cyclopts`, `cisternal`) and 78+ MCP tools—taking ~2,755 ms—invoking `bth session-start` directly inside a shell hook will reliably trigger execution timeouts ($> 100\text{ ms}$) and freeze agent sessions.

Following the established architecture of `bth-preflight-hook` (`src/bathos/hooks/preflight_hook.py`):
1. **Dedicated Stdlib-Only Hook Script**:
   Author `src/bathos/hooks/session_start_hook.py` installed as console script `bth-session-start-hook` in `pyproject.toml:[project.scripts]`. It imports only Python standard library modules (`sys`, `os`, `json`, `pathlib`).
2. **Pre-Rendered Cache Artifact**:
   The script reads a pre-rendered cache JSON file at `.bth/cache/session_start.json`. This cache is maintained asynchronously during catalog ingest, compaction, finding creation, or defect declaration by `src/bathos/session_start.py:generate_session_start_cache()`.
3. **Structured Claude Code Hook Protocol**:
   Claude Code hooks require a structured JSON envelope on stdout: `{"systemMessage": "..."}`. Emitting raw text crashes the Claude Code hook parser. `bth-session-start-hook` formats its output inside this envelope and exits 0 in $< 15\text{ ms}$, well under the 100 ms budget. If the cache file does not exist, it emits nothing and exits 0.

- **Hook Configuration (`plugin/hooks/hooks.json`)**:
  ```json
  {
    "hooks": {
      "SessionStart": [
        {
          "hooks": [
            {
              "command": "bth-session-start-hook",
              "type": "command"
            }
          ],
          "matcher": ".*"
        }
      ],
      "PreToolUse": [
        {
          "hooks": [
            {
              "command": "bth-preflight-hook",
              "type": "command"
            }
          ],
          "matcher": "Bash|mcp__.*__submit_job"
        }
      ]
    }
  }
  ```
- **Deterministic Content Ordering (Byte-Capped at 2,048 bytes)**:
  1. **Active Defect Alerts** (max 3 lines):
     `! ALERT: aminx <= 0.1.0a22 void range active (S > 1)`
  2. **Needs-Recheck Items** (immediate action required):
     `? NEEDS RECHECK: 'tev-site-stability' (all supporting runs voided by AMINX-2026-001)`
  3. **Settled Beliefs Summary** (current knowledge):
     `* SETTLED: 'pdz-affinity-ceiling' -> Kd floor is 120nM [confirmatory, 14d ago]`
  4. **Open Questions**:
     `o OPEN: 'necklace-poe-opt' [tentative, 2d ago]`
- Superseded or void items are **NEVER** listed.

---

### 4.6 CLI & FastMCP Function Signatures (P2)

```python
# In src/bathos/mcp.py:

@cisternal.tool(registry="bathos-cli", name="finding_create", cli_group="findings", cli_name="create")
def finding_create_tool(
    question_slug: str,
    question: str,
    answer: str,
    supporting_runs: list[str],
    status: str = "tentative",
    supersedes: str = "",
    reopen_reason: str = "",
    catalog_dir: str = "",
) -> dict: ...

@cisternal.tool(registry="bathos-cli", name="finding_get", cli_group="findings", cli_name="get")
def finding_get_tool(
    slug_or_id: str,
    catalog_dir: str = "",
) -> dict: ...

@cisternal.tool(registry="bathos-cli", name="finding_list", cli_group="findings", cli_name="list")
def finding_list_tool(
    status: str = "",
    include_superseded: bool = False,
    catalog_dir: str = "",
) -> dict: ...

@cisternal.tool(registry="bathos-cli", name="supersede_edge_add", cli_group="supersede", cli_name="add")
def supersede_edge_add_tool(
    entity_type: str,
    entity_id: str,
    superseded_by_type: str,
    superseded_by_id: str,
    reason: str,
    catalog_dir: str = "",
) -> dict: ...

@cisternal.tool(registry="bathos-cli", name="session_start", cli_group="session", cli_name="start")
def session_start_tool(
    byte_cap: int = 2048,
    catalog_dir: str = "",
) -> dict: ...
```

---

### 4.7 Refusal Codes & Resolution Hints (P2)

In `src/bathos/errors.py`:
```python
FINDING_REOPEN_REASON_REQUIRED = "finding_reopen_reason_required"
FINDING_SETTLED_EVIDENCE_INSUFFICIENT = "finding_settled_evidence_insufficient"
FINDING_NOT_FOUND = "finding_not_found"
SUPERSEDED_TARGET_NOT_FOUND = "superseded_target_not_found"
SPIKE_TIER_CANNOT_SETTLE = "spike_tier_cannot_settle"
```

Resolution hints in `RESOLUTION_HINTS`:
- `FINDING_REOPEN_REASON_REQUIRED`: *"The question is already settled. Provide --reopen-reason explaining why prior confirmatory evidence is being challenged."*
- `FINDING_SETTLED_EVIDENCE_INSUFFICIENT`: *"Settling a finding requires at least one active confirmatory run. Elevate supporting evidence before setting status to settled."*
- `FINDING_NOT_FOUND`: *"The specified finding slug or ID was not found in .bth/findings/. Query finding_list to inspect available findings."*
- `SUPERSEDED_TARGET_NOT_FOUND`: *"The target entity specified in supersedes does not exist. Verify the predecessor ID or slug."*
- `SPIKE_TIER_CANNOT_SETTLE`: *"Spike runs are preliminary probes and cannot support a settled finding. Elevate evidence via a confirmatory run."*

---

### 4.8 Acceptance Criteria (P2)

- **AC-P2-01**: Sidecar declaring `stage_name = "spike"` resolves to `epistemic_class = "spike"`.
- **AC-P2-02**: A finding declaring `status = "settled"` backed exclusively by spike runs is refused with `SPIKE_TIER_CANNOT_SETTLE`.
- **AC-P2-03**: Undeclared stage names resolve to `epistemic_tier = "undeclared"` and are displayed as such.
- **AC-P2-04**: Adding a supersession edge `run_A -> run_B` causes `run_A` to resolve to `state = "superseded"` and hide from default reads.
- **AC-P2-05**: Creating a finding with an existing settled `question_slug` without `--reopen-reason` is refused with `FINDING_REOPEN_REASON_REQUIRED`.
- **AC-P2-06**: When all supporting runs of an active finding are hidden (`len(supporting_runs) > 0` and all hidden), querying the finding returns `status = "needs-recheck"`. Open findings with `supporting_runs = []` retain `status = "open"`.
- **AC-P2-07**: `bth-session-start-hook` fast-path script reads `.bth/cache/session_start.json` and emits `{"systemMessage": "..."}` formatted under 2,048 bytes in $\le 15\text{ ms}$, well under the 100 ms budget.
- **AC-P2-08**: `bth session-start` lists all `needs-recheck` findings and active defect ranges.
- **AC-P2-09**: `bth session-start` never includes items with status `superseded` or `void`.
- **AC-P2-10**: `finding_list_tool` hides superseded findings unless `include_superseded=True`.
- **AC-P2-11**: W3C PROV-JSON output in `src/bathos/provenance.py` via `format_prov_json(runs, validity_map=...)` emits `prov:wasInvalidatedBy` for void runs and `prov:wasRevisionOf` for superseded entities.
- **AC-P2-12**: Non-canonical stages `"confirmatory"` and `"power"` map correctly to tier `"confirmatory"`.
- **AC-P2-13**: Supersession edges support bidirectional querying (`replaces` vs `replaced_by`).
- **AC-P2-14**: Finding status flips dynamically at read time without modifying the underlying finding TOML file.
- **AC-P2-15**: SessionStart hook is registered in `plugin/hooks/hooks.json` pointing to `bth-session-start-hook`.

---

### 4.9 Fixer Task Decomposition (P2)

1. **Task P2.1**: Update `src/bathos/sidecar.py` with `spike` epistemic tier and stage mapping. Update `errors.py`.
2. **Task P2.2**: Author `src/bathos/supersession.py` managing typed supersession edges, `unit_of_work` log-mode event emission (`supersession.declared`), and index DDL/folding.
3. **Task P2.3**: Author `src/bathos/findings.py` implementing finding creation, downgrade ladder (with empty-list guard), and reopen guard.
4. **Task P2.4**: Author `src/bathos/session_start.py` generating `.bth/cache/session_start.json` and author `src/bathos/hooks/session_start_hook.py` stdlib-only fast-path runner.
5. **Task P2.5**: Wire tools into `cli_cyclopts.py` and `mcp.py`. Update `src/bathos/cli_cyclopts.py:expected` with new `@cisternal.tool` names `finding_create`, `finding_get`, `finding_list`, `supersede_edge_add`, `session_start`. Register `bth-session-start-hook` console script in `pyproject.toml` and hook in `plugin/hooks/hooks.json`.
6. **Task P2.6**: Extend `src/bathos/provenance.py:format_prov_json(runs, *, run_parent_edges=None, validity_map=None)` with PROV invalidation/revision edges.
7. **Task P2.7**: Comprehensive test suite in `tests/test_findings_p2.py`.

---

## 5. Phase 3 (P3): Archive Sweep & Migration

### 5.1 Scope & Objectives
P3 performs system cleanup: batch archival sweep of dead scripts and void outputs (D8), praxia-compatible document store integration (D8/D9), telemetry rotation, and historical backfill of `asr` and `tev_design` (D9/D10).

---

### 5.2 Batch Archive Sweep (D8)

#### A. Sweep Candidate Selection Criteria
A file or script is proposed for archival if it satisfies any of:
1. Sidecar carries `[status] stale = true`.
2. Produces runs that are 100% `void` or `superseded`.
3. Script unreferenced for $\ge N$ days (default $N = 90$, explicitly arbitrary) AND cited by zero current findings or claims.
4. Documentation files citing exclusively voided or archived runs/findings.
5. Telemetry log files older than 30 days or exceeding 10 MB.

#### B. Execution Workflow (Single Atomic Commit & .tar.zst Bundles — Finding M7)
1. `bth archive sweep --dry-run`: Scans workspace and emits candidate manifest `.bth/archive_proposals/sweep_<date>.toml`.
2. Owner inspects proposal and approves: `bth archive sweep --apply --proposal .bth/archive_proposals/sweep_<date>.toml`.
3. **Atomic Batch Archive Execution**:
   - Rather than creating an individual git commit for each archived script (which would pollute git history with dozens of single-file commits and risk unrollbackable partial states upon failure), `archive_sweep` creates `.tar.zst` bundles for script outputs and documents per AC-P3-02, stubs all targeted tracked files in workspace, and stages all changes into a **single atomic git commit**:
     `archive: sweep N obsolete scripts and void outputs (proposal: <path>)`
   - Git notes are attached for all swept items in batch.
   - Records items in `archived_items` ledger.
4. `bth restore <id>` restores any archived item cleanly from its bundle.

---

### 5.3 Praxia-Compatible Document Store Integration (D8/D9)

#### A. Configuration Resolution Chain
Where bathos searches for technical documentation:
1. Explicit CLI flag: `--doc-store <path>`
2. Environment variable: `BTH_DOC_STORE` (empty or `"none"` disables external sharing)
3. `pyproject.toml`: `[tool.bathos] doc_store = ".praxia/docs"`
4. `${XDG_CONFIG_HOME:-~/.config}/bathos/config.toml`
5. Default fallback: `.bth/docs`

#### B. Compatibility Invariant
When pointed at `.praxia/docs`:
- Reuses Praxia frontmatter metadata: `status = "superseded" | "archived"`, `supersedes = "..."`.
- Employs Praxia's archival format: `.tar.zst` archive accompanied by an adjacent `.md` manifest sidecar.
- Bathos writes to `.praxia/docs` **ONLY** when explicitly configured (Anti-Goal A5).

---

### 5.4 Telemetry Rotation & Indexing

- **Threshold**: 10 MB per `.bth/log/*.jsonl` file.
- **Rotation**: Compressed via `zstandard` to `.bth/log/archive/<year>/events_<timestamp>.jsonl.zst`.
- **Index**: An append-only SQLite/DuckDB table `telemetry_index` records `(archive_path, start_time, end_time, event_count, sha256)`. Events are never deleted.

---

### 5.5 Backfill Sequencing for `asr` and `tev_design` (D9/D10)

```
[Step 1: Defect Declaration & Resolution]
 - Declare aminx <= a22 void & a24-a25 estimand change
 - Declare asr.lg_model 2026-06-08 -> 2026-10-23 void
 - Execute D2 version recovery dry-run -> Owner approves counts
       │
       ▼
[Step 2: Findings Seeding & Parity Verification]
 - Seed 43 findings from tev claims_ledger.toml
 - Seed 41 findings from asr .verdicts/
 - Show 100% parity -> Freeze legacy files as read-only pointers
       │
       ▼
[Step 3: Archive Sweep Triage]
 - Run bth archive sweep --dry-run
 - Owner reviews candidate batches -> Execute archive bundles
```

---

### 5.6 CLI & FastMCP Function Signatures (P3)

```python
# In src/bathos/mcp.py:

@cisternal.tool(registry="bathos-cli", name="archive_sweep", cli_group="archive", cli_name="sweep")
def archive_sweep_tool(
    dry_run: bool = True,
    age_days: int = 90,
    proposal_path: str = "",
    apply: bool = False,
    catalog_dir: str = "",
) -> dict: ...

@cisternal.tool(registry="bathos-cli", name="archive_restore", cli_group="archive", cli_name="restore")
def archive_restore_tool(
    item_id: str,
    catalog_dir: str = "",
) -> dict: ...

@cisternal.tool(registry="bathos-cli", name="telemetry_rotate", cli_group="telemetry", cli_name="rotate")
def telemetry_rotate_tool(
    max_size_mb: int = 10,
    catalog_dir: str = "",
) -> dict: ...
```

---

### 5.7 Refusal Codes & Resolution Hints (P3)

In `src/bathos/errors.py`:
```python
ARCHIVE_SWEEP_UNAPPROVED = "archive_sweep_unapproved"
ARCHIVE_PROPOSAL_STALE = "archive_proposal_stale"
DOC_STORE_UNAVAILABLE = "doc_store_unavailable"
RESTORE_CONFLICT = "restore_conflict"
```

Resolution hints in `RESOLUTION_HINTS`:
- `ARCHIVE_SWEEP_UNAPPROVED`: *"Archive sweep requires an approved proposal file. Run with --dry-run first to generate a proposal."*
- `ARCHIVE_PROPOSAL_STALE`: *"The archive proposal references files or git states that have changed since proposal generation. Re-run --dry-run to regenerate."*
- `DOC_STORE_UNAVAILABLE`: *"The configured documentation store path is not accessible or writable. Check permissions or configuration."*
- `RESTORE_CONFLICT`: *"Restoring this item would overwrite an existing untracked file. Move or rename the destination file first."*

---

### 5.8 Acceptance Criteria (P3)

- **AC-P3-01**: `bth archive sweep --dry-run` detects scripts with `[status] stale = true` and outputs a proposal TOML file without modifying workspace files.
- **AC-P3-02**: Applying an approved sweep archives targeted files into `.tar.zst` bundles, stubs tracked files within a single atomic git commit, and logs entries in `archived_items`.
- **AC-P3-03**: Restoring an archived bundle via `bth archive restore <id>` completely restores original file bytes and SHA256 checksums.
- **AC-P3-04**: Pointing bathos at `.praxia/docs` correctly parses and updates `status = "superseded"` in document frontmatter.
- **AC-P3-05**: Backfill step 1 reproduces the exact verified counts in `tev_design` (24/25 sample lock matches; aminx wheel resolution).
- **AC-P3-06**: Backfill step 2 ingests all 43 claims from `claims_ledger.toml` into native findings with 100% field parity.
- **AC-P3-07**: Ingested claims from `claims_ledger.toml` with `evidential_status = "refuted_or_superseded"` map to `status = "superseded"`.
- **AC-P3-08**: Telemetry files exceeding 10 MB are compressed into `.tar.zst` with entries logged in `telemetry_index`.
- **AC-P3-09**: Archived scripts hide their historical runs from default reads via `archived_items` fold.
- **AC-P3-10**: Sweep dry-run flags docs citing voided runs.
- **AC-P3-11**: Bathos refuses to write into `.praxia/docs` when configuration is unset.
- **AC-P3-12**: Backfill scripts are executed under tracked sidecars in accordance with experimental rigor rules.

---

### 5.9 Fixer Task Decomposition (P3)

1. **Task P3.1**: Author `src/bathos/archive_sweep.py` implementing sweep candidate detection, proposal generation, single atomic commit execution, and `.tar.zst` bundle generation.
2. **Task P3.2**: Author `src/bathos/doc_store.py` providing abstraction for `.bth/docs` vs `.praxia/docs`.
3. **Task P3.3**: Implement telemetry rotation and indexing in `src/bathos/telemetry.py`.
4. **Task P3.4**: Author backfill migration script `scripts/backfill_findings.py` for `tev_design` and `asr`.
5. **Task P3.5**: Wire CLI commands and MCP tools in `cli_cyclopts.py` and `mcp.py`. Update `src/bathos/cli_cyclopts.py:expected` with new `@cisternal.tool` names `archive_sweep`, `archive_restore`, `telemetry_rotate`.
6. **Task P3.6**: Comprehensive test suite in `tests/test_archive_p3.py`.

---

## 6. Traceability, Error Taxonomy & Verification Summary

### 6.1 Consolidated Error Code Taxonomy

| Code Value | Enum Identifier | Primary Trigger Surface | Actionable Remediation Hint |
|---|---|---|---|
| `result_void` | `BathosErrorCode.RESULT_VOID` | `cite`, `claim`, `campaign` | The requested run was produced by a defective instrument version and is void. Query runs_current or use a replacement run. Pass --include-superseded to inspect history. |
| `result_affected` | `BathosErrorCode.RESULT_AFFECTED` | `cite`, `claim`, `campaign` | The requested run is implicated by an active blast-radius ledger entry. Re-run or clear the flag with audit justification. |
| `result_superseded` | `BathosErrorCode.RESULT_SUPERSEDED` | `cite`, `claim`, `show` | The requested run was superseded by a newer run. Use --include-superseded to inspect, or query the successor run. |
| `result_archived` | `BathosErrorCode.RESULT_ARCHIVED` | `cite`, `get`, `outputs` | The requested script or outputs were archived. Use `bth archive restore <id>` to restore files to workspace. |
| `result_hidden` | `BathosErrorCode.RESULT_HIDDEN` | `get_run`, `show` | The requested run is hidden by default due to void, affected, superseded, or archived status. Pass --include-superseded to inspect. |
| `result_suspect_unacknowledged` | `BathosErrorCode.RESULT_SUSPECT_UNACKNOWLEDGED` | `cite`, `claim` | Run was produced by an unverified toolchain matching a defect range. To cite, pass acknowledged_suspect=True with an explicit rationale. |
| `result_unverifiable_unacknowledged` | `BathosErrorCode.RESULT_UNVERIFIABLE_UNACKNOWLEDGED` | `cite`, `claim` | Run provenance cannot be verified against repository lockfiles. To cite, pass acknowledged_unverifiable=True. |
| `estimand_boundary_mixed` | `BathosErrorCode.ESTIMAND_BOUNDARY_MIXED` | `campaign add/conclude` | Attempted to combine runs across an estimand change boundary. Results compute fundamentally different quantities and cannot be pooled. |
| `finding_reopen_reason_required` | `BathosErrorCode.FINDING_REOPEN_REASON_REQUIRED` | `findings create` | The question is already settled. Provide --reopen-reason explaining why prior confirmatory evidence is being challenged. |
| `finding_settled_evidence_insufficient` | `BathosErrorCode.FINDING_SETTLED_EVIDENCE_INSUFFICIENT` | `findings create` | Settling a finding requires at least one active confirmatory run. Elevate supporting evidence before setting status to settled. |
| `finding_not_found` | `BathosErrorCode.FINDING_NOT_FOUND` | `findings get` | The specified finding slug or ID was not found in .bth/findings/. Query finding_list to inspect available findings. |
| `superseded_target_not_found` | `BathosErrorCode.SUPERSEDED_TARGET_NOT_FOUND` | `supersede add` | The target entity specified in supersedes does not exist. Verify the predecessor ID or slug. |
| `spike_tier_cannot_settle` | `BathosErrorCode.SPIKE_TIER_CANNOT_SETTLE` | `findings create` | Spike runs are preliminary probes and cannot support a settled finding. Elevate evidence via a confirmatory run. |
| `archive_sweep_unapproved` | `BathosErrorCode.ARCHIVE_SWEEP_UNAPPROVED` | `archive sweep` | Archive sweep requires an approved proposal file. Run with --dry-run first to generate a proposal. |
| `archive_proposal_stale` | `BathosErrorCode.ARCHIVE_PROPOSAL_STALE` | `archive sweep` | The archive proposal references files or git states that have changed since proposal generation. Re-run --dry-run to regenerate. |
| `doc_store_unavailable` | `BathosErrorCode.DOC_STORE_UNAVAILABLE` | `doc_store` | The configured documentation store path is not accessible or writable. Check permissions or configuration. |
| `restore_conflict` | `BathosErrorCode.RESTORE_CONFLICT` | `archive restore` | Restoring this item would overwrite an existing untracked file. Move or rename the destination file first. |

---

### 6.2 Telemetry Event Schema

```json
{
  "event": "validity.resolved",
  "run_ids_count": 50,
  "hidden_count": 12,
  "void_count": 8,
  "affected_count": 2,
  "superseded_count": 1,
  "archived_count": 1,
  "duration_ms": 4
}
```

---

### 6.3 Open Owner Questions for Phase 4 Review

1. **Owner Question OQ-1: Visibility of `suspect` Runs Post-P1 Dry-Run**:
   - *Issue*: Currently, `suspect` runs (defect matches on inferred commit versions) are shown with warning banners rather than hidden.
   - *Trade-off*: Hiding them immediately might conceal valid exploratory work where git trees were dirty; keeping them visible risks minor banner fatigue.
   - *Recommendation*: Maintain banner-only visibility in P1; revisit after reviewing backfill dry-run counts in Step 1.
2. **Owner Question OQ-2: Automatic Archival Proposal Threshold ($N = 90$ days)**:
   - *Issue*: 90 days is declared as an arbitrary baseline.
   - *Trade-off*: Shorter windows (e.g. 45 days) keep catalogs leaner; longer windows (180 days) prevent premature archiving of slow-turnaround structural biology projects.
   - *Recommendation*: Adopt 90 days default with configurable `--age-days` flag.
