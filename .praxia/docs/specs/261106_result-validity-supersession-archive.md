---
title: "Result Validity, Supersession, and Archive: Production Specification"
description: "Comprehensive production specification for task 261106_archive-supersession: instrument defect ranges with version backfill from uv.lock and metadata, read-time validity filtering & refusal gates across 13 surfaces, evidence tiers including spike, distance-from-present annotations, a lightweight findings register with supersession edges, session-start current beliefs view, batch archive sweeps, praxia-compatible document store, and backfill migration for asr and tev_design."
status: REVIEWED_SPEC
task_id: 261106_archive-supersession
phase: 3
authors:
  - "worker_spec (teamwork_worker)"
date: "2026-11-08"
version: "0.4.0"
---

# Result Validity, Supersession, and Archive: Production Specification

## 1. Executive Summary & Core Architecture

### 1.1 The Problem: Contamination and Circling in Research Repositories

Long-horizon scientific campaigns conducted across distributed clusters and autonomous agent sessions in `asr` and `tev_design` suffer from two chronic, crippling failure modes:

1. **Contamination (Silent Zombie Results)**:
   Results produced by a defective, uncalibrated, or since-changed instrument (chiefly the `aminx` MPNN wrapper and the corrupted `asr.lg_model` rate matrix) persist indefinitely in catalog files. When agents query the catalog via `bth ls`, `bth find`, `bth show`, `bth sql`, or `bth cite`, these invalid readings are dredged up and cited as authoritative truth. Because bathos previously recorded blast-radius flags and stale markers strictly on the write seam without consulting them on read paths, a fatally flawed result was indistinguishable from a pristine one at the moment an agent touched it.
2. **Circling (Amnestic Re-litigation)**:
   Because the repository lacked a machine-readable register of current beliefs ("what do we currently believe about scientific question $Q$, and why?"), settled design questions were repeatedly re-opened and re-litigated. Each agent session rebuilt the picture from scattered, dated markdown notes, landing on mutually contradictory conclusions and redundantly re-executing identical runs (e.g. `tev_design`'s `claims_ledger.toml` explicitly records an item *"twice proposed as new work because nothing here pointed at it"*).

### 1.2 System Invariants

This specification enforces four immutable architectural invariants:

1. **Invariant I1 — Read-Time Projection (21 CFR Part 11 Non-Obliteration)**:
   Hiding invalid, void, affected, superseded, or archived results is **strictly a read-time projection**. Historical ledger rows (`runs`, `blast_radius_ledger`, `trust_ledger`, `archived_items`), `.bth/log/*.jsonl` run events, and cool-tier run Parquet fragments are **never mutated, overwritten, or deleted** to implement hiding (Anti-Goal A2). Historical runs remain permanently intact and accessible via explicit `--include-superseded` / `include_superseded=True` overrides (with `--allow-hidden` / `allow_hidden=True` accepted as a backward-compatible alias on `bth sql` / `run_sql`).
2. **Invariant I2 — Gate Refusal Ratchet**:
   Any downstream gate that vouchsafes epistemic authority (`bth cite`, `claim register`, `claim validate`, `campaign attest-parity`, `campaign add`, `campaign conclude`, `resolve_pin`, `get_trust_state`, `graduate_product`, and Maraxiom `review_build`) **must refuse** when presented with a result whose validity is hidden (`void`, `affected`, `superseded`, or `archived`). It cannot be bypassed by silent omission.
3. **Invariant I3 — Provenance Distinction & Opt-In Inferred Hiding (Anti-Goal A1 + `OD-1` `(owner: 2026-11-06)`)**:
   Low-provenance history must not be erased by default. A run with an *inferred* dependency version (`version_source = "inferred-from-commit"`) matching a defect range caps at `suspect` by default—it is displayed with a warning banner and permitted through gates only when accompanied by an explicit non-empty rationale string (`--ack-suspect "<reason>"` / `ack_suspect: str`), recorded append-only (`OD-5`). However, per **`OD-1` `(owner: 2026-11-06)`**, a registry entry may explicitly opt in with `hide_inferred = true` (e.g. for `AMINX-2026-001` on `aminx <= 0.1.0a22`, enabled **only after the P1 dry-run counts are reviewed by the owner**), in which case an inferred match resolves to the entry's hidden state (`void` / `is_hidden = True`).
4. **Invariant I4 — Bounded Context Overhead (Anti-Goal A3)**:
   Agent startup information must never grow unbounded. The session-start current beliefs payload is byte-capped at 2,048 bytes (an initial engineering heuristic; revisit after P1/P2 operational data), contains zero superseded or void items, and executes within a 100 ms timeout via a lightweight stdlib-only fast-path script `bth-session-start-hook` reading `.bth/cache/session_start.json` in $\le 15\text{ ms}$ and emitting Claude Code's `hookSpecificOutput.additionalContext` JSON envelope.

---

### 1.3 Ontological Classifications & Validity States

#### A. Defect vs. Estimand Change (`OD-6` `(owner: 2026-11-06)`)

The system establishes a strict ontological separation between calculation flaws and measurement definition changes:

- **`defect` (Fatal Instrument Bug)**:
   The algorithm or code produced an incorrect mathematical result (e.g. `aminx <= 0.1.0a22` multi-sample AR sampling logits corruption when $S > 1$; `asr.lg_model` rate matrix where 152/190 exchangeabilities were mathematically erroneous between `56286cf` on `2026-06-08` and `d4a17b3` / `ef66b99` on `2026-10-23`). Runs matching a defect range are epistemically unsound: their validity resolves to `void` (or `suspect` if `version_source == "inferred-from-commit"` and `hide_inferred == False`, or if a required predicate field is missing per `OD-2`).
- **`estimand_change` (Comparability Boundary Marker)**:
   The instrument was modified such that it computes a *different mathematical quantity*, but the calculation is mathematically sound under its own definition (e.g. `aminx` between `0.1.0a24` and `0.1.0a25` eliminated a diagonal self-leak of +0.036243 nats and switched to the conditional estimand charging all 21 tokens).
   **Rule (`OD-6` `(owner: 2026-11-06)`)**:
   1. An estimand change **MUST NEVER VOID OR HIDE A RUN**. Both sides of the boundary remain `current`.
   2. **Refusal Gates**: Mixing runs across an estimand change boundary is refused with `BathosErrorCode.ESTIMAND_BOUNDARY_MIXED` at three confirmatory gates:
      - `campaign conclude` (`src/bathos/campaigns.py:1139`),
      - `claim register` (`src/bathos/claim.py:604`), and
      - Settling a finding (`finding_create` / `finding_update` when `status == "settled"` and `len(supporting_runs) > 1`).
   3. **Recorded Justification Override**: All three refusal gates accept `allow_mixed_estimand: str = ""` (`--allow-mixed-estimand "<reason>"`). A non-empty justification string overrides `ESTIMAND_BOUNDARY_MIXED` and appends an audit event `validity.estimand_override_recorded` (recording `actor`, `timestamp`, `surface`, `entity_id`, `epochs`, and `reason`).
   4. **Banner-Only Surfaces**: Read surfaces (`ls`, `find`, `show`, `campaign review`), `lineage`, and **`campaign add`** (`src/bathos/campaigns.py:420`) do **not** refuse on mixed estimand epochs; they emit an informational/warning banner only.
   5. **Epoch Rule for `unknown`-Version Runs**: For any package with an active `estimand_change` entry, a run whose resolved version is `"unknown"` receives `estimand_marker = "<pkg>:unknown"`. Pooling a `"<pkg>:unknown"` run with runs from any known epoch (e.g. `"aminx:pre-AMINX-2026-002"` or `"aminx:post-AMINX-2026-002"`) triggers `ESTIMAND_BOUNDARY_MIXED` at the three confirmatory gates unless overridden with `--allow-mixed-estimand "<reason>"`.

#### B. The Seven Validity States

| State | Definition / Trigger | Default Read Visibility (`ls`, `find`, `show`) | Gate Behavior (`cite`, `claim`, `campaign`, `graduate`) |
|---|---|---|---|
| `void` | Matched instrument defect range with severity `void` on verified toolchain (`recorded` or `verified-from-lock`), OR on `inferred-from-commit` when entry sets `hide_inferred = true` (`OD-1`). | **Hidden** (counter incremented) | **Refused** (`RESULT_VOID`) |
| `affected` | Implicated by `blast_radius_ledger` (`to_state = "affected"`) or defect entry with `severity = "affected"`. | **Hidden** (counter incremented) | **Refused** (`RESULT_AFFECTED`) |
| `superseded` | Replaced by a newer run/finding via a typed supersession edge (D5). | **Hidden** (counter incremented) | **Refused** (`RESULT_SUPERSEDED`) |
| `archived` | Script or outputs archived via `artifact_archive.py` (`archived_items` latest status `"archived"`). | **Hidden** (counter incremented) | **Refused** (`RESULT_ARCHIVED`) |
| `suspect` | Defect match on *inferred* version (`hide_inferred = false`), or missing predicate field `<f>` (`reason = "missing predicate field: <f>"`, `OD-2`), or explicit `severity = "suspect"`. | **Shown + Banner** | **Allowed only with non-empty rationale** (`--ack-suspect "<reason>"` / `ack_suspect: str`, recorded append-only per `OD-5`; else `RESULT_SUSPECT_UNACKNOWLEDGED`) |
| `unverifiable` | Blast assessment inconclusive (`to_state = "unverifiable"`), lockfile hash corrupted, or repo root unavailable when an unpredicated defect could apply. | **Shown + Banner** | **Allowed only with non-empty rationale** (`--ack-unverifiable "<reason>"` / `ack_unverifiable: str`, recorded append-only per `OD-5`; else `RESULT_UNVERIFIABLE_UNACKNOWLEDGED`) |
| `current` | Clean: no active defect match (or exempted at current entry `revision`), no blast flags, unarchived, active finding/claim. | **Shown clean** | **Allowed clean** |

#### C. Version Source Resolution Hierarchy (D2 + Runtime Metadata Recovery)

```
[Run Execution Record]
        │
        ├── 1. `dependency_provenance.<pkg>` populated? ──────────────────────> YES: `recorded`
        │      OR `metadata.<pkg>_version` present and verified by lock SHA?         (Highest confidence)
        │                      │ NO
        ├── 2. Local repo root exists AND `sha256(git show <hash>:uv.lock)` ──> YES: `verified-from-lock`
        │      matches recorded `dependency_lock_sha256`?
        │      (Note: if `metadata.<pkg>_version` is also present and differs
        │       from `uv.lock`, e.g. a hand-installed wheel, use the metadata
        │       version and downgrade `version_source` to `inferred-from-commit`
        │       unless the wheel SHA matches `dependency_lock_sha256`)
        │                      │ NO
        ├── 3. `metadata.<pkg>_version` (or `<pkg>_installed_dist_version`, ──> YES: `inferred-from-commit`
        │      `tev_pinned_<pkg>_wheel`, `scores_<pkg>_version`) present in          (Caps at `suspect` unless
        │      `run.metadata`, OR `git show <hash>:uv.lock` at `git_hash`             `hide_inferred = true`)
        │      parses without a matching `dependency_lock_sha256`?
        │      (Note: `git_dirty = True` records dirty-tree flag)
        │                      │ NO
        └── 4. Toolchain unresolvable (or repo root unavailable locally) ─────> YES: `unknown`
```

- **Empirical Justification for Runtime Metadata Recovery**:
  Live inspection of `tev_design/.bth/log/*.jsonl` revealed that while `dependency_provenance` is `null` on all 3,170 run events, **117 run events across 36 distinct runs** record `.data.metadata.aminx_version` directly (`0.2.0a1` on 13 runs, `0.2.0a3` on 4 runs, `0.2.1` on 17 runs, `0.2.1+armask` on 1 run, `0.4.0` on 1 run), plus `aminx_installed_dist_version` (7 runs), `tev_pinned_aminx_wheel` (3 runs), and `scores_aminx_version` (3 runs). Consulting these metadata keys in Tiers 1–3 recovers direct runtime version evidence instead of discarding it.
- **PEP 440 Version & Local Identifier Comparison Rules**:
  All `ECOSYSTEM = "PyPI"` version strings are parsed via `packaging.version.Version`. Ranges `events = [{ introduced = A }, { fixed = B }]` use half-open interval semantics: `Version(A) <= v < Version(B)` (and `Version(A) <= v <= Version(C)` when `last_affected = C` is used). When a run's resolved version carries a PEP 440 local version label (such as `0.2.1+armask` in `tev_design`), comparison uses `Version(v.base_version)` (`0.2.1`) unless the registry boundary itself explicitly specifies a `+local` segment (`"+" in boundary_str`). Thus `0.2.1+armask` has base version `0.2.1`, which is `>= fixed = "0.2.1"` (unaffected by defects fixed in `0.2.1`) and `<= last_affected = "0.2.1"` (affected by defects covering `0.2.1`).

---

### 1.4 Change Log & Settled Owner Decisions (`OD-1`–`OD-6`)

#### A. Change Log of Settled Owner Decisions & Revisions to Designer Calls

All 6 open owner questions raised during adversarial review were settled by the owner on `2026-11-06` and are binding across this specification:

| ID | Provenance | Decision Summary | Specification Impact & Designer Call Revision |
|---|---|---|---|
| **`OD-1`** | `(owner: 2026-11-06)` | Keep the global `suspect` cap (A1). A registry entry may opt in with `hide_inferred = true`; use it for `aminx <= 0.1.0a22` **only after the P1 dry-run counts are reviewed**. | Refines D2/D3/D10: adds `hide_inferred: bool = False` to `[[entries]]` (§3.2.A) and `defect_declare_tool` (§3.6). In `tev_design`, `dependency_lock_sha256` recording began `2026-08-13` (`>= 0.1.0a24`), so all `<= 0.1.0a22` runs (`2026-07-15..2026-07-28`) resolve to `inferred-from-commit`. P1 dry-run (§5.5 Step 1) reports counts with `hide_inferred = false` vs `true` before enabling `hide_inferred = true` on `AMINX-2026-001`. |
| **`OD-2`** | `(owner: 2026-11-06)` | When a run lacks a field the defect predicate needs, resolve to `suspect` with reason `'missing predicate field: <f>'`. Never silent non-match, never `void`. | Restores D10 predicate rule and removes v0.3.0's `[entries.predicate.defaults]` and `COALESCE(..., 0.0) -> False` rule (§2.1, §3.2.A, AC-P1-02). Because 0 of 3,170 `tev_design` run events have `metadata.sample_s`, runs matching `<= 0.1.0a22` without `sample_s` resolve to `suspect` (`reason = "missing predicate field: sample_s"`), or `void` when `hide_inferred = true` is opted in after dry-run review. |
| **`OD-3`** | `(owner: 2026-11-06)` | **No shadowing** of `runs` in `run_sql`. `runs_current` view is the documented target; `runs` stays full history; print a notice when `runs` is queried and hidden rows exist. | **Reverses D10's designer call** (`runs` as current-only view / `runs_all` rename). Ratifies exposing `runs_current` as the filtered view while keeping `runs` as the full historical table with a parse-time notice banner when hidden rows exist (§1.4.B, §3.4 item 4). |
| **`OD-4`** | `(owner: 2026-11-06)` | Per-user registry `${XDG_CONFIG_HOME:-~/.config}/bathos/instruments/` that projects opt into, plus project-local `.bth/instruments/` entries that override it. | Settles D1/D10/§7.1: drops `~/.bth/instruments/`. Projects opt in via `.bth.toml` `[instruments] use_user_registry = true` (or `pyproject.toml` `[tool.bathos.instruments] use_user_registry = true`). Project-local `.bth/instruments/<pkg>.toml` entries override user-global entries with the same `id`. Both locations feed `inputs_hash` (§2.2). |
| **`OD-5`** | `(owner: 2026-11-06)` | Acknowledging a `suspect` or `unverifiable` run at a gate requires a rationale string (e.g. `--ack-suspect "reason"` / `ack_suspect: str`), recorded append-only with actor + timestamp. | Settles D3/§7.2: replaces bare booleans `acknowledged_suspect: bool` / `acknowledged_unverifiable: bool` with `ack_suspect: str = ""` and `ack_unverifiable: str = ""` across `cite_run`, `claim_register`, `campaign_conclude`, and `graduate_product`. Appends `validity.acknowledged` event to `.bth/log/*.jsonl` and folds into `acknowledgment_ledger`. |
| **`OD-6`** | `(owner: 2026-11-06)` | Mixing runs across an estimand change is refused at `campaign conclude`, `claim register`, and settling a finding; allow with a recorded justification (`--allow-mixed-estimand "reason"`). Reads, `campaign add`, and `lineage` show a banner only. | Refines D1/D10: `campaign add` emits a warning banner instead of refusing; `campaign conclude`, `claim register`, and `finding_create(status="settled")` refuse with `ESTIMAND_BOUNDARY_MIXED` unless `allow_mixed_estimand: str` is non-empty (recording `validity.estimand_override_recorded`). Defines `"<pkg>:unknown"` epoch for `unknown`-version runs. |

#### B. Rationale for `OD-3` (No Shadowing of `runs` in `run_sql`)
- **Initial Designer Call (§4 D10)**: Replace the base DuckDB `runs` table with a current-only view, renaming full history to `runs_all`.
- **Owner-Ratified Settlement (`OD-3` `(owner: 2026-11-06)`)**:
  1. Create a dedicated view **`runs_current`** filtering out hidden states (`void`, `affected`, `superseded`, `archived`).
  2. In all documentation, agent instructions, CLI help text, and MCP tool hints, establish `runs_current` as the primary query target.
  3. Keep the base table **`runs`** queryable as the complete, unmuted historical ledger so existing scripts, audits, and internal tools (`migrate`, `repair`, `compact`, `verify`) never break.
  4. **Notice Banner on Base Table Queries**: `run_sql` inspects the SQL string using the case-insensitive regex:
     ```python
     _RAW_RUNS_RE = re.compile(r'(?i)\b(?:FROM|JOIN)\s+(?:[a-zA-Z_]\w*\s*\.\s*)?"?runs"?(?!_current\b)\b')
     ```
     which matches `FROM runs`, `JOIN runs`, `FROM "runs"`, `FROM idx.runs`, and `FROM main."runs"` across arbitrary whitespace/newlines without matching `runs_current`. When matched and hidden rows exist in `validity_cache`:
     - For `bth sql` CLI: print a notice to `stderr`:
       `"Notice: Query targets unmuted historical table 'runs' (N void/affected/superseded/archived runs present). Query 'runs_current' for current results, or pass --include-superseded (alias: --allow-hidden) to suppress this notice."`
     - For MCP `run_sql_tool`: include `"notice"` and `"hidden_count"` in the return dict.
     - Passing `include_superseded=True` (`--include-superseded`) or `allow_hidden=True` (`--allow-hidden`) suppresses the notice.

#### C. Settlement of Design Section 7 Open Builder Items
1. **Registry File Format, Location, and Cross-Project Sharing (Design §7.1, `OD-4`)**:
   - Stored as OSV-inspired TOML files per package under project-local `<main_root>/.bth/instruments/<pkg>.toml` and user-global `${XDG_CONFIG_HOME:-~/.config}/bathos/instruments/<pkg>.toml`.
   - Projects opt into the user-global registry by setting `[instruments] use_user_registry = true` in `.bth.toml` (or `[tool.bathos.instruments] use_user_registry = true` in `pyproject.toml`). Both `asr` and `tev_design` enable `use_user_registry = true` in P1 so shared `aminx` defect declarations live in `${XDG_CONFIG_HOME:-~/.config}/bathos/instruments/aminx.toml`.
   - Override precedence: project-local `.bth/instruments/<pkg>.toml` entries override user-global entries sharing the same entry `id`.
2. **`run_sql` Hiding Mechanism & Acknowledgment Field Shape (Design §7.2, `OD-3`, `OD-5`)**:
   - `run_sql` exposes `runs_current` and keeps `runs` as full history with a notice banner (`OD-3`).
   - Gate acknowledgments require non-empty rationale strings `ack_suspect: str = ""` (`--ack-suspect "<reason>"`) and `ack_unverifiable: str = ""` (`--ack-unverifiable "<reason>"`), recorded append-only in `acknowledgment_ledger` (`OD-5`).
3. **`"Reproduced"` Definition & Reopen Guard Question-Matching Method (Design §7.3)**:
   - **`reproduction_count`**: For a given run $R$, `reproduction_count` is the count of subsequent runs $R'$ in the same project where: (a) $R'.\text{timestamp} > R.\text{timestamp}$, (b) $R'.\text{status} == \text{"completed"}$, (c) $R'$ resolves to `validity.state == "current"`, (d) $R'.\text{sidecar\_sha256} == R.\text{sidecar\_sha256}$ (non-null), and (e) $R'.\text{outcome} == R.\text{outcome}$ where $R.\text{outcome} \notin \{\text{""}, \text{"unknown"}, \text{"observed"}, \text{"invalid\_measurement"}, \text{"error"}\}$ (`NEUTRAL_OUTCOME_LABELS` in `src/bathos/sidecar.py:456`). Because exploratory and calibrating runs always store `outcome = "observed"` (`sidecar.py:449-457`), they never count as confirmatory reproductions.
   - **Reopen Guard Matching Method**: Exact match on normalized `question_slug` (lowercase ASCII alphanumeric + hyphens) first, followed by normalized exact match on `question` text (lowercased, punctuation/whitespace collapsed). Enforced at `finding_create` (`FINDING_REOPEN_REASON_REQUIRED` unless `reopen_reason` is non-empty) and checked on `bth new-experiment` / `bth preflight` / `bth run` when a sidecar declares `[experiment] question_slug` matching a `settled` finding (requiring `reopen_reason` in `[experiment]` or `--reopen-reason` on CLI while leaving the existing `settled` finding intact until a replacement finding reaches `status = "settled"`).
4. **Why `dependency_provenance` Is Null Today & Runner Auto-Population (Design §7.4, E19)**:
   - `capture_dependency_provenance(tracked_packages, argv, cwd)` (`src/bathos/dependency_provenance.py:688-698`) returns `None` immediately when `tracked_packages` is empty, and neither `asr` nor `tev_design` ever configured `track_packages`.
   - Fixed in P1 (§3.3): `resolve_tracked_packages()` automatically unions explicitly configured `track_packages` with all package names present in active user-global (when opted in) and project-local instrument registries, plus P1 sets `track_packages` in `.bth.toml` for both repos.
5. **Telemetry Destination & Rotation Thresholds (Design §7.5)**:
   - Diagnostic telemetry (`validity.resolved`, `validity.filter_applied`, `validity.override_used`) is emitted exclusively via `bathos.telemetry.event()` into `~/.bth/catalog/logs/events.<hostname>.<pid>.jsonl` (`src/bathos/telemetry.py:271, 448-461`), **never** into the project runlog `<main_root>/.bth/log/*.jsonl`.
   - Telemetry rotation compresses closed files in `~/.bth/catalog/logs/` older than 30 days or exceeding 10 MB into `~/.bth/catalog/logs/archive/<year>/events_<timestamp>.jsonl.zst` and indexes them in `telemetry_index`.
6. **Session-Start Byte Cap, Ordering, and Hook Protocol (Design §7.6)**:
   - Byte cap: `2,048` bytes; execution timeout: `100 ms` (executed in $\le 15\text{ ms}$ from `.bth/cache/session_start.json` via stdlib-only `bth-session-start-hook`).
   - Deterministic ordering: (1) Active Defect Alerts (`! ALERT:`), (2) Needs-Recheck Findings (`? NEEDS RECHECK:`), (3) Settled Findings (`* SETTLED:`), (4) Open/Tentative Findings (`o OPEN:`). Never lists `superseded` or `void` items.
   - Emits Claude Code's `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}` JSON envelope so the summary is injected directly into agent model context.
7. **Classification of Numerical Cutoffs (Anti-Goal A6 Compliance)**:
   - Per Anti-Goal A6 (*"Arbitrary thresholds presented as principled"*), the following four numerical parameters are explicitly declared as **initial engineering heuristics / arbitrary cutoffs to be revisited after P1–P3 operational data**:
     1. Session-start payload cap: `byte_cap = 2048` bytes (configurable via `--byte-cap`).
     2. Session-start hook timeout budget: `100 ms`.
     3. Archive sweep unreferenced script age threshold: `age_days = 90` days (configurable via `--age-days`).
     4. Telemetry rotation thresholds: `max_size_mb = 10` MB and `max_age_days = 30` days (configurable via `--max-size-mb` / `--max-age-days`).

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
    is_hidden: bool      # True iff state in ("void", "affected", "superseded", "archived")
    defect_id: str | None = None
    defect_revision: int | None = None
    estimand_marker: str | None = None  # e.g. "aminx:pre-AMINX-2026-002" | "aminx:post-AMINX-2026-002" | "aminx:unknown"

def resolve_validity(
    run_ids: list[str],
    catalog_dir: Path | str,
    db: duckdb.DuckDBPyConnection | None = None,
    workspace_root: Path | None = None,
) -> dict[str, ValidityResult]:
    """Resolve validity states for a batch of run IDs via derived cache and active ledgers."""
```

#### Mapping Runs in Global `index.db` to Project Repository Roots (`main_root`):
Because `index.db` (`~/.bth/catalog/index.db`, `src/bathos/index.py:357-358`) is user-global and `RUNS_DDL` (`src/bathos/index.py:94-163`) stores `project_slug` rather than `main_root`, `resolve_validity` resolves each run's filesystem `main_root` deterministically in batch:
1. **Primary Resolution**:
   - Query `events.main_root` for the run's `run.started` / `run.imported` event (`src/bathos/index.py:60`), and cross-reference live registered roots from `~/.bth/projects.toml` via `src/bathos/runlog/project_id.py:list_registered_roots()` (`:216`) and `src/bathos/config.py:list_registered_projects()` (`:341`), matching `.bth.toml` `[project] slug` / `id` (or `workspace_root` when passed explicitly).
2. **Fallback for Missing, Remote, or Unaffiliated Roots**:
   - If a run is unaffiliated (`src/bathos/runlog/ingest.py:325-327`), or its `main_root` directory no longer exists on the local machine, or `git -C <main_root> cat-file -e <git_hash>` fails:
     - Registry loading falls back to the user-global registry `${XDG_CONFIG_HOME:-~/.config}/bathos/instruments/*.toml` (plus `workspace_root/.bth/instruments/*.toml` if `workspace_root` was provided).
     - Version resolution checks Tier 1 (`dependency_provenance`) and Tier 3 runtime `metadata.<pkg>_version` keys first; if neither is present, `version_source` resolves to `"unknown"` without raising a subprocess or filesystem error.
     - If an active defect entry exists for `<pkg>` and the run's version is `"unknown"`, the run resolves to `state = "unverifiable"` (`reason = "toolchain version unresolvable for defect <id>"`); if no defect entry exists, it resolves to `state = "current"`.

#### Evaluation Precedence within `resolve_validity`:
1. **Check `archived_items` ledger (Join Key Specification)**:
   - Fold `archived_items` (`src/bathos/archived_items.py:30-48`, `src/bathos/index.py:ARCHIVED_ITEMS_DDL`) latest-wins per `id` (`ROW_NUMBER() OVER (PARTITION BY id ORDER BY recorded_at DESC)` where `event = 'archived'`).
   - A run matches an active `'archived'` item if **any** of the following hold:
     a. `run.id` is present in `archived_items.run_ids` (JSON-encoded `list[str]` populated at archive time), OR
     b. `run.script_sha256` is non-null and equals `archived_items.script_sha256` or `archived_items.pre_archive_sha`, OR
     c. `run.project_slug == archived_items.project_slug` and `run.sidecar_path` or any path in `run.output_paths` (or the script path in `run.command` / `run.argv`) is contained in the JSON list `archived_items.paths`.
   - If matched, state is `archived` (`is_hidden = True`, `replacement_id = archived_items.superseded_by`).
2. **Check `supersession_edges`**:
   - If an active supersession edge (`entity_type = "run"`, `entity_id = run.id`) exists in `supersession_edges`, state is `superseded` (`is_hidden = True`, `replacement_id = superseded_by_id`, `reason = edge.reason`).
3. **Check `blast_radius_ledger` (Including `dependency_range` Anchors & Per-Revision Exemptions)**:
   - Fold `blast_radius_ledger` (`src/bathos/blast_radius.py:411-428`) for `entity_type = "run"` and `entity_id = run.id`:
     - Non-defect-range blast records (`anchor_kind in ("commit", "commit_range", "file")`): if latest `to_state == "affected"`, state is `affected`; if `"unverifiable"`, state is `unverifiable`.
     - Defect-range records (`anchor_kind == "dependency_range"`, `anchor_value == "<entry_id>@r<revision>"`): if the latest record for `<entry_id>@r<current_entry_revision>` has `to_state == "cleared"`, the run is **exempted** from `<entry_id>` at revision `<current_entry_revision>` (per Design §4 D10). If `<entry_id>`'s `revision` in `.bth/instruments/<pkg>.toml` is later incremented to `revision + 1`, the prior `@r<revision>` exemption does not match `@r<revision + 1>` and the run is re-evaluated.
4. **Check Active Instrument Defect & Estimand Registries (`.bth/instruments/<pkg>.toml`)**:
   - Load merged registry entries for the run's project (per **`OD-4`**: user-global `${XDG_CONFIG_HOME:-~/.config}/bathos/instruments/*.toml` when `[instruments] use_user_registry = true`, overlaid by project-local `<main_root>/.bth/instruments/*.toml` by entry `id`).
   - Resolve the run's package version and commit via the 4-tier hierarchy (§1.3.C).
   - **Range Matching (OSV `ECOSYSTEM` and `GIT` Ranges — Design §4 D1 & D10)**:
     - **`type = "GIT"` (Source-Commit Range)**: Matched when the package's source commit is resolvable—specifically: (i) `dependency_provenance.<pkg>.commit`, (ii) a tracked wheel in `wheels/` whose SHA/metadata maps to a commit, or (iii) `run.git_hash` when `<pkg>` is the repository's own editable package (e.g. `package = "asr"` in `/home/marielle/projects/asr` for the `asr.lg_model` defect window `56286cf..d4a17b3`, or `tev_design` commit `d930bbad` while pinned at `0.1.0a24`). A commit $c$ falls in `{ introduced = I, fixed = F }` iff `git merge-base --is-ancestor I c` is true and `git merge-base --is-ancestor F c` is false.
     - **`type = "ECOSYSTEM"` (PEP 440 Version Range)**: Matched using `packaging.version.Version` half-open intervals `[introduced, fixed)` (or closed `[introduced, last_affected]`), applying the base-version rule (§1.3.C) for local version identifiers like `0.2.1+armask`.
   - **Estimand Change Marker (`kind == "estimand_change"`)**:
     - Never alters `state` (`state` remains `"current"` unless flagged by a defect/blast/supersession/archive rule).
     - Populates `estimand_marker`: `"<pkg>:pre-<entry_id>"` if version/commit precedes `introduced`, `"<pkg>:post-<entry_id>"` if version/commit is $\ge$ `introduced`, or `"<pkg>:unknown"` if `version_source == "unknown"` (`OD-6`).
   - **Defect Entry Evaluation (`kind == "defect"`)**:
     - If the run is exempted for `<entry_id>@r<entry.revision>` in `blast_radius_ledger`, skip this entry.
     - If the run's version/commit matches the entry's range:
       - **Case A — Entry has no `[entries.predicate]`**:
         - If `version_source in ("recorded", "verified-from-lock")`: `state = entry.severity` (`"void"` or `"affected"`), `defect_id = entry.id`, `defect_revision = entry.revision`.
         - If `version_source == "inferred-from-commit"`:
           - Per **`OD-1` `(owner: 2026-11-06)`**: if `entry.hide_inferred is True`, `state = entry.severity` (`"void"` / hidden); otherwise `state = "suspect"`, `reason = f"inferred version matches defect {entry.id}"`.
       - **Case B — Entry defines `[entries.predicate] condition = "<sql_expr>"` (`OD-2` `(owner: 2026-11-06)`)**:
         1. Extract all field identifiers referenced by `<sql_expr>` via DuckDB's `json_serialize_sql` AST walker (`COLUMN_REF` nodes, reusing `src/bathos/sidecar.py:1514-1520`).
         2. For each referenced field `<f>`, check whether `<f>` exists with a non-null value in the run's top-level columns, `run.metadata` JSON (`json_extract(metadata, '$.<f>') IS NOT NULL`), or parsed CLI `--<f>` in `run.argv`.
         3. **Missing Field Rule (`OD-2` `(owner: 2026-11-06)`)**: If **any** referenced field `<f>` is missing or null:
            - Never treat as a silent non-match (`False`), and never resolve to `void` on a missing field alone unless `entry.hide_inferred is True` is explicitly opted in for that cohort after dry-run review (`OD-1`).
            - Resolve to `state = "suspect"`, `reason = f"missing predicate field: {f}"`, `defect_id = entry.id`, `defect_revision = entry.revision` (or if `entry.hide_inferred is True`, `state = entry.severity` with `reason = f"inferred defect {entry.id} (missing predicate field: {f})"`).
         4. **Present Field Evaluation**: When all referenced fields `<f>` are present and non-null, evaluate `<sql_expr>` in DuckDB over the extracted field struct:
            - If `<sql_expr>` evaluates to `False`: this entry does **not** match (the run is unaffected by this defect, e.g. `sample_s == 1` on `AMINX-2026-001` resolves to `current`).
            - If `<sql_expr>` evaluates to `True`:
              - If `version_source in ("recorded", "verified-from-lock")`: `state = entry.severity` (`"void"`).
              - If `version_source == "inferred-from-commit"`: `state = entry.severity` (`"void"`) if `entry.hide_inferred is True` (`OD-1`), else `state = "suspect"`.
     - **Blast-Radius Ledger Integration (Design §4 D1)**:
       - When `bth instrument declare-defect` or `bth blast-radius assess --dependency-ranges` runs, matching runs are recorded in `blast_radius_ledger` with `anchor_kind = "dependency_range"` and `anchor_value = "<entry_id>@r<revision>"`, automatically triggering `propagate_to_campaigns()` (`src/bathos/blast_radius.py:1200-1235`) and `propagate_to_claims()` (`:1286-1363`).
5. **Default Clean State**:
   - If no archive record, supersession edge, blast-radius flag, or defect range implicates the run, `state = "current"`.

---

### 2.2 Derived Materialized Cache Architecture (`validity_cache.db`)

#### A. Separation of Derived Cache from Read-Only `index.db` / `bathos.db`
In log mode, `connect_read()` (`src/bathos/index.py:476-510`) opens an in-memory DuckDB connection (`duckdb.connect(":memory:")`) and executes `ATTACH '<index.db>' AS idx (READ_ONLY)`. In legacy mode (`:468-474`), `connect_read()` opens `bathos.db` with `read_only=True`. Because DuckDB forbids `CREATE TABLE` or `INSERT` on a `READ_ONLY` attached database (and because **AC-P1-13** requires zero file modifications to `bathos.db`, `index.db`, `.bth/log/*.jsonl`, or cool-tier Parquet fragments during read-time validity resolution), the derived caches (`validity_cache` and `version_cache`) are stored in a dedicated derived DuckDB file:
- **Path**: `<catalog_dir>/validity_cache.db` (e.g. `~/.bth/catalog/validity_cache.db`, with an atomic JSON mirror `<main_root>/.bth/cache/version_cache.json` for git-lock version lookups).
- **Schema in `validity_cache.db`**:
  ```sql
  CREATE TABLE IF NOT EXISTS validity_cache_meta (
      singleton INTEGER PRIMARY KEY DEFAULT 1,
      inputs_hash TEXT NOT NULL,
      updated_at TEXT NOT NULL
  );

  CREATE TABLE IF NOT EXISTS validity_cache (
      run_id TEXT PRIMARY KEY,
      validity_state TEXT NOT NULL,
      reason TEXT,
      replacement_id TEXT,
      version_source TEXT NOT NULL,
      evidence_tier TEXT NOT NULL,
      defect_id TEXT,
      defect_revision INTEGER,
      estimand_marker TEXT,
      inputs_hash TEXT NOT NULL,
      cached_at TEXT NOT NULL
  );
  CREATE INDEX IF NOT EXISTS idx_validity_state ON validity_cache(validity_state);

  CREATE TABLE IF NOT EXISTS version_cache (
      main_root TEXT NOT NULL,
      git_hash TEXT NOT NULL,
      lock_sha256 TEXT NOT NULL,
      package_name TEXT NOT NULL,
      resolved_version TEXT,
      resolved_commit TEXT,
      version_source TEXT NOT NULL,
      PRIMARY KEY (main_root, git_hash, lock_sha256, package_name)
  );
  ```

#### B. Sound `inputs_hash` Computation
To ensure in-place edits to `.bth/instruments/*.toml`, changes to the user-global registry (`OD-4`), newly ingested runs, and ledger updates always invalidate stale cache entries, `compute_validity_inputs_hash(catalog_dir, con)` computes the `SHA256` digest over a canonical JSON tuple of:
1. **Instrument Registry Content Hashes (`OD-4`)**:
   - Sorted list of `(canonical_path, sha256(file_bytes))` for every `.toml` file in `${XDG_CONFIG_HOME:-~/.config}/bathos/instruments/*.toml` and every registered project's `<main_root>/.bth/instruments/*.toml`, plus each project's `[instruments] use_user_registry` boolean.
2. **Run Catalog State**:
   - In log mode: `(count(*), max(rowid))` from `events` and sorted `(root_kind, root_id, path, byte_offset)` from `ingest_watermarks`, plus `count(*)` from `runs`.
   - In legacy warm mode: `(count(*), max(timestamp), max(id))` from `runs`.
3. **Ledger States**:
   - `(count(*), max(amended_at))` from `blast_radius_ledger` (guarded via `duckdb_tables()`; in legacy mode, also include sorted `(filename, mtime_ns, size)` of cool-tier `<catalog_dir>/blast_radius/*.parquet` fragments),
   - `(count(*), max(recorded_at))` from `archived_items` (guarded via `duckdb_tables()`; in legacy mode, also include sorted `(filename, mtime_ns, size)` of cool-tier `<catalog_dir>/archived_items/*.parquet` fragments),
   - `(count(*), max(declared_at))` from `supersession_edges` (if present in `duckdb_tables()`, else `(0, None)`).

#### C. `connect_read()` Integration, Pre-Existing DB Guard & Completeness Guarantee
1. **Pre-Existing `index.db` / `bathos.db` Table Guard (`src/bathos/index.py:500-507`)**:
   Because `idx` is attached `READ_ONLY` in `connect_read()`, tables added in P1/P2 (`acknowledgment_ledger`, `supersession_edges`) may not yet exist in a pre-existing `index.db` or `bathos.db` before the next write/ingest runs `init_index_schema`. Following the `output_copies` pattern at `src/bathos/index.py:500-507`, `connect_read()` checks `SELECT count(*) FROM duckdb_tables() WHERE database_name = 'idx' AND table_name = '<tbl>'` and falls back to `CREATE VIEW <tbl> AS {_empty_select(<TBL>_DDL)}` when the table is absent, preventing `CatalogException` on existing catalogs.
2. Before returning `con` from `connect_read(catalog_dir)` — in **both** the log-mode branch (`src/bathos/index.py:476-510`) and the legacy warm-mode branch (`src/bathos/index.py:468-474` when `db_path.exists()`) — `ensure_validity_cache(catalog_dir, con)` computes `current_hash = compute_validity_inputs_hash(catalog_dir, con)`.
3. If `<catalog_dir>/validity_cache.db` does not exist, or `validity_cache_meta.inputs_hash != current_hash`, or `SELECT count(*) FROM runs WHERE id NOT IN (SELECT run_id FROM vcache.validity_cache)` is $> 0$:
   - `ensure_validity_cache` acquires a file lock `<catalog_dir>/validity_cache.lock`, opens `validity_cache.db` read-write, evaluates `resolve_validity` in batch for all uncached or invalidated runs in `runs`, writes the updated rows and `current_hash`, and closes the write handle.
4. `connect_read()` then attaches `validity_cache.db` read-only (`ATTACH '<validity_cache.db>' AS vcache (READ_ONLY)`), exposes `CREATE TEMP VIEW validity_cache AS SELECT * FROM vcache.validity_cache`, and defines `runs_current` (using `CREATE TEMP VIEW` so it succeeds even when legacy `bathos.db` is opened `read_only=True`):
   ```sql
   CREATE TEMP VIEW runs_current AS
   SELECT r.*
   FROM runs r
   LEFT JOIN vcache.validity_cache vc ON r.id = vc.run_id
   WHERE COALESCE(vc.validity_state, 'current') NOT IN ('void', 'affected', 'superseded', 'archived');
   ```
   Because `ensure_validity_cache` populates a row in `vcache.validity_cache` for every `r.id` in `runs` before `runs_current` is queried, no newly ingested defective run can ever slip through `runs_current` uninspected.

---

## 3. Phase 1 (P1): Stop the Leak

### 3.1 Scope & Objectives
P1 stops contaminated results from reaching agents. It implements the defect registry with `ECOSYSTEM` and `GIT` ranges, `revision`, and `hide_inferred` (D1, `OD-1`, `OD-4`), 4-tier version resolution with runtime metadata recovery and runner auto-population (D2), read-time filtering and gate refusals with required rationale strings and estimand boundary checks across all 13 mapped surfaces (D3, `OD-2`, `OD-3`, `OD-5`, `OD-6`), and distance-from-present calculation (D6).

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
revision = 1
kind = "defect"
severity = "void"
hide_inferred = false  # OD-1: set to true only after owner reviews P1 dry-run counts
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
reasoning = "Single-sample (S=1) argmax sampling is mathematically unaffected; missing sample_s resolves to suspect per OD-2"

[[entries]]
id = "AMINX-2026-002"
revision = 1
kind = "estimand_change"
severity = "current"
summary = "Diagonal self-leak elimination between a24 and a25 changed computed quantity"
evidence = "tev_design/scripts/audit/claims_ledger.toml:33-37"
declared_at = "2026-11-06T12:00:00Z"
declared_by = "owner"

[[entries.ranges]]
type = "ECOSYSTEM"
events = [
    { introduced = "0.1.0a25" }
]

[[entries]]
id = "AMINX-2026-003"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = false
summary = "Pre-d930bbad commit defect while pinned at 0.1.0a24"
evidence = "wheels/AMINX_PIN.md:212-288"
declared_at = "2026-11-06T12:00:00Z"
declared_by = "owner"

[[entries.ranges]]
type = "GIT"
events = [
    { introduced = "e5f40cb8b" },
    { fixed = "d930bbad" }
]
```

And for `asr`'s editable repository package (`asr/.bth/instruments/asr.toml`):
```toml
# Schema: bathos-instrument-defect-v1.0
schema_version = "1.0.0"
package = "asr"
ecosystem = "GIT"

[[entries]]
id = "ASR-LG-2026-001"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = false
summary = "asr.lg_model rate matrix had 152/190 erroneous exchangeabilities (2026-06-08 to 2026-10-23)"
evidence = ".praxia/docs/plans/261007_sp107_structural-em-t0-t1-prereg.md:262-277"
declared_at = "2026-11-06T12:00:00Z"
declared_by = "owner"

[[entries.ranges]]
type = "GIT"
events = [
    { introduced = "56286cf" },
    { fixed = "d4a17b3" }
]
```

#### B. Project Opt-In Configuration (`OD-4` `(owner: 2026-11-06)`)
In `<main_root>/.bth.toml` (parsed in `src/bathos/config.py`):
```toml
[instruments]
use_user_registry = true
```
- When `use_user_registry = true` (default `false` unless set in `.bth.toml` or `pyproject.toml` `[tool.bathos.instruments]`), `load_instrument_registries(main_root)` loads `${XDG_CONFIG_HOME:-~/.config}/bathos/instruments/<pkg>.toml` and merges `<main_root>/.bth/instruments/<pkg>.toml`.
- If an entry `id` appears in both files, the project-local `<main_root>/.bth/instruments/<pkg>.toml` entry takes precedence.

#### C. Acknowledgment & Estimand-Override Audit Ledger (`OD-5`, `OD-6`)
In `src/bathos/index.py` (`init_index_schema` and `connect_read`) and `src/bathos/catalog.py`:
```sql
CREATE TABLE IF NOT EXISTS acknowledgment_ledger (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,          -- "ack_suspect" | "ack_unverifiable" | "allow_mixed_estimand"
    entity_type TEXT NOT NULL,   -- "run" | "campaign" | "claim" | "finding"
    entity_id TEXT NOT NULL,
    run_ids TEXT NOT NULL,       -- JSON list of run IDs covered
    surface TEXT NOT NULL,       -- "cite_run" | "claim_register" | "campaign_conclude" | "graduate_product" | "finding_create"
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    recorded_at TEXT NOT NULL
);
```
In log mode, each acknowledgment or mixed-estimand override emits an append-only event (`validity.acknowledged` or `validity.estimand_override_recorded`) via `emit_event()` inside `unit_of_work(catalog_dir)`, which folds into `acknowledgment_ledger`.

#### D. Schema Evolution & Log-Mode Integration (Version 20)
1. **Invariant I1 Reaffirmation**:
   `cached_validity_state` is **never** added to `Run`, `COOL_SCHEMA`, or `WARM_SCHEMA`. Validity state lives exclusively in `validity_cache.db`.
2. **`src/bathos/index.py:RUNS_DDL` & `RUNS_COLUMNS`**:
   Add nullable column `toolchain_version_source VARCHAR` to `RUNS_DDL` and `RUNS_COLUMNS`, folded in `src/bathos/runlog/fold_runs.py:fold_run()`.
3. **`src/bathos/archived_items.py` & `src/bathos/index.py:ARCHIVED_ITEMS_DDL` (Finding `M3-5`)**:
   Add nullable columns `script_sha256 TEXT` and `run_ids TEXT` (JSON list of run IDs associated with the script/outputs at archive time) to `ArchivedItemRecord`, `_TABLE_SCHEMA` (`src/bathos/archived_items.py:30-48`), `_FRAGMENT_SCHEMA` (`:52-70`), and `ARCHIVED_ITEMS_DDL` (`src/bathos/index.py`).
4. **Legacy Cool-Tier Migration (`src/bathos/migrate.py`)**:
   In legacy non-log mode (`src/bathos/migrate.py:58-66`), bump `CURRENT_SCHEMA_VERSION = "20"` in `src/bathos/schema.py` adding `pa.field("toolchain_version_source", pa.string(), nullable=True)`.

---

### 3.3 Runner Fix: Automatic Population of `dependency_provenance` (E19)

In `src/bathos/runner.py:1205-1208` and `src/bathos/dependency_provenance.py:100-125`:
```python
def resolve_tracked_packages(sidecar_path: Path | None, start: Path) -> list[str]:
    explicit = _resolve_explicit_tracked_packages(sidecar_path, start)
    registry_pkgs = load_instrument_registry_packages(start)
    return sorted(set(explicit) | set(registry_pkgs))
```
`load_instrument_registry_packages(start)` returns all `package` names from active entries in `<main_root>/.bth/instruments/*.toml` and (when `use_user_registry = true`) `${XDG_CONFIG_HOME:-~/.config}/bathos/instruments/*.toml`.

---

### 3.4 Exact Insertion Points for D3 Validity Checks Across All 13 Surfaces

```
                                  ┌────────────────────────┐
                                  │   resolve_validity()   │
                                  │ (validity_cache.db)    │
                                  └───────────┬────────────┘
                                              │
         ┌────────────────────────────────────┼────────────────────────────────────┐
         │ Read Surfaces                      │ Gate / Registration Surfaces       │ Downstream Bridges
         ▼                                    ▼                                    ▼
┌──────────────────┐                 ┌──────────────────┐                 ┌──────────────────┐
│ 1. list_runs / ls│                 │ 5. cite_run/cite │                 │ 8. resolve_pin   │
│ (query.py:259)   │                 │ (mcp.py:2500)    │                 │ (readback.py:113)│
│ (mcp.py:329)     │                 ├──────────────────┤                 ├──────────────────┤
├──────────────────┤                 │ 6. claim_register│                 │ 9. get_trust_stat│
│ 2. find_runs/find│                 │ (claim.py:604)   │                 │ (readback.py:253)│
│ (query.py:610)   │                 │ claim_validate   │                 ├──────────────────┤
│ (mcp.py:401)     │                 │ (claim.py:232)   │                 │ 10. graduate_prod│
├──────────────────┤                 │ attest_parity    │                 │ (trust_ledger:308│
│ 3. get_run / show│                 │ (claim.py:966)   │                 ├──────────────────┤
│ (query.py:466)   │                 ├──────────────────┤                 │ 11. lineage      │
│ (mcp.py:456)     │                 │ 7. campaign_add  │                 │ (query.py:833)   │
├──────────────────┤                 │ (campaigns:420)  │                 │ (mcp.py:2546)    │
│ 4. run_sql / sql │                 │ campaign_conclude│                 ├──────────────────┤
│ (query.py:773)   │                 │ (campaigns:1139) │                 │ 12. review_build │
│ (mcp.py:513)     │                 │ campaign_review  │                 │ (verify_drawable)│
└──────────────────┘                 │ (campaigns:1947) │                 ├──────────────────┤
                                     └──────────────────┘                 │ 13. SessionStart │
                                                                          │ (manifest.toml)  │
                                                                          └──────────────────┘
```

#### Deterministic State-to-Error-Code Mapping for Gates (`m3-1`):
Whenever a gate refuses a hidden run, it maps `ValidityResult.state` deterministically to `BathosErrorCode`:
```python
HIDDEN_STATE_TO_ERROR_CODE: dict[str, BathosErrorCode] = {
    "void": BathosErrorCode.RESULT_VOID,
    "affected": BathosErrorCode.RESULT_AFFECTED,
    "superseded": BathosErrorCode.RESULT_SUPERSEDED,
    "archived": BathosErrorCode.RESULT_ARCHIVED,
}
```
Whereas `get_run_tool` (`bth show`) on any hidden run when `include_superseded=False` returns `BathosErrorCode.RESULT_HIDDEN` (with `validity_state`, `defect_id`, `reason`, and `replacement_id` in the structured error payload).

#### Surface-by-Surface Contracts:
1. **`list_runs` (`src/bathos/query.py:259`, `src/bathos/mcp.py:329` `list_runs_tool`, `mcp.py:2469` `mcp_list_runs_tool`)**:
   - Add `include_superseded: bool = False`.
   - In `_index_list_runs` (`query.py:277`) and `_warm_list_runs` (`query.py:299`), call `ensure_validity_cache(catalog_dir)` and query `FROM runs_current` (when `not include_superseded`) *before* `LIMIT {limit}` so pagination is never under-filled. In `_cool_list_runs` (`query.py:244-256`, uncompacted cool-tier fallback), filter via `resolve_validity` *before* `return runs[:limit]`.
   - In `list_runs_tool` (`mcp.py:353-364`), pass `include_superseded=include_superseded` to **both** its `find_runs(...)` branch (used when `project`, `since`, or `status` is set) and its `list_runs(...)` branch.
   - Attach `evidence_tier`, `version_source`, `validity_state`, `estimand_marker`, and `distance` to each returned run dict, plus top-level `hidden_count: int` and `notice: str | None` (`"N superseded/void/archived results hidden — --include-superseded to show"`).
2. **`find_runs` (`src/bathos/query.py:610`, `src/bathos/mcp.py:401` `find_runs_tool`, `mcp.py:2479` `mcp_find_runs_tool`)**:
   - Add `include_superseded: bool = False`. Query `runs_current` before `LIMIT` when `not include_superseded` in `_index_find_runs` (`query.py:652`) and `_warm_find_runs` (`query.py:701`), filter via `resolve_validity` in `_cool_find_runs` (`query.py:587-608`), and attach `hidden_count` and `notice`.
3. **`get_run` / `show` (`src/bathos/query.py:466`, `src/bathos/mcp.py:456` `get_run_tool`, `mcp.py:2490` `mcp_get_run_tool`)**:
   - Add `include_superseded: bool = False`.
   - Call `resolve_validity([run.id], cat_dir)`. If `v.is_hidden and not include_superseded`, raise `ResultHiddenError` (`BathosErrorCode.RESULT_HIDDEN`) naming `v.state`, `v.defect_id`, `v.reason`, and `v.replacement_id`.
   - If `v.state in ("suspect", "unverifiable")` or `include_superseded=True` or `v.estimand_marker is not None`, include `"validity"` and `"validity_banner"` in the returned dict.
4. **`run_sql` / `sql` (`src/bathos/query.py:773`, `src/bathos/mcp.py:513` `run_sql_tool`, `mcp.py:2598` `mcp_run_sql_tool`) (`OD-3`)**:
   - Add `include_superseded: bool = False` and `allow_hidden: bool = False` (CLI alias `--allow-hidden` for `--include-superseded`).
   - `connect_read()` exposes `runs_current` alongside full-history `runs`.
   - If `_RAW_RUNS_RE` (`r'(?i)\b(?:FROM|JOIN)\s+(?:[a-zA-Z_]\w*\s*\.\s*)?"?runs"?(?!_current\b)\b'`) matches `sql` and `not (include_superseded or allow_hidden)`, check `SELECT count(*) FROM vcache.validity_cache WHERE validity_state IN ('void', 'affected', 'superseded', 'archived')`. If $> 0$, emit the stderr notice (CLI) and attach `"notice"` and `"hidden_count"` in `run_sql_tool`'s return dict.
5. **`cite` (`src/bathos/cite.py:13`, `src/bathos/mcp.py:2500` `cite_run_tool`, `mcp.py:2526` `mcp_cite_run_tool`) (`OD-5`)**:
   - Add `ack_suspect: str = ""` and `ack_unverifiable: str = ""`.
   - At `mcp.py:2521`, call `v = resolve_validity([run.id], cat_dir)[run.id]`.
   - If `v.is_hidden`: raise domain exception mapped to `HIDDEN_STATE_TO_ERROR_CODE[v.state]` (`RESULT_VOID`, `RESULT_AFFECTED`, `RESULT_SUPERSEDED`, or `RESULT_ARCHIVED`).
   - If `v.state == "suspect"`: if `not ack_suspect.strip()`, refuse with `BathosErrorCode.RESULT_SUSPECT_UNACKNOWLEDGED`; otherwise record append-only `validity.acknowledged` event (`surface="cite_run"`, `reason=ack_suspect.strip()`) and prepend `[SUSPECT — ACKNOWLEDGED: <reason>]` banner to the formatted citation.
   - If `v.state == "unverifiable"`: if `not ack_unverifiable.strip()`, refuse with `BathosErrorCode.RESULT_UNVERIFIABLE_UNACKNOWLEDGED`; otherwise record append-only `validity.acknowledged` event and prepend `[UNVERIFIABLE — ACKNOWLEDGED: <reason>]` banner.
6. **`claim register` (`src/bathos/claim.py:604`), `claim validate` (`claim.py:232`), and `attest_parity` (`claim.py:966`) (`OD-5`, `OD-6`)**:
   - In `register_claim()` (`claim.py:657`), after `member_ids = union_campaign_member_ids(db, full_id, catalog_dir)`, call `validity_map = resolve_validity(member_ids, catalog_dir, db=db, workspace_root=workspace_root)`:
     - If any member run has `v.is_hidden`: refuse with `HIDDEN_STATE_TO_ERROR_CODE[v.state]` naming all implicated runs and reasons.
     - If any member run is `suspect` or `unverifiable`: require non-empty `ack_suspect` / `ack_unverifiable` rationale strings and record `validity.acknowledged`.
     - **Estimand Boundary Check (`OD-6`)**: If member runs span $> 1$ distinct non-null `estimand_marker` for the same package (including `"<pkg>:unknown"` mixed with a known epoch), refuse with `BathosErrorCode.ESTIMAND_BOUNDARY_MIXED` unless `allow_mixed_estimand.strip()` is non-empty (recording `validity.estimand_override_recorded`).
   - In `validate_claim()` (`claim.py:344`) and `_attest_parity_impl()` (`claim.py:1063`): resolve validity of `parity_run_id`. If `v.is_hidden`, append a `ValidationError` / raise `ValueError` with `HIDDEN_STATE_TO_ERROR_CODE[v.state]`.
   - In `differential_confound_check()` (`claim.py:851`) and `run_union_gate()` (`claim.py:932`): replace `JOIN runs r ON cr.run_id = r.id` with `JOIN runs_current r ON cr.run_id = r.id` so hidden runs never satisfy Union Gate or positive-control clauses.
7. **`campaign add` (`src/bathos/campaigns.py:420`), `campaign conclude` (`campaigns.py:1139`), and `campaign review` (`campaigns.py:1947`) (`OD-5`, `OD-6`)**:
   - **`add_run_to_campaign` (`campaigns.py:532`)**: Call `resolve_validity([run_id], catalog_dir)`. If `v.is_hidden`, refuse with `HIDDEN_STATE_TO_ERROR_CODE[v.state]`. If adding `run_id` introduces a second `estimand_marker` epoch among the campaign's runs, **do not refuse** (`OD-6`): allow the add and return `"estimand_warning"` in the response / print warning banner to stderr.
   - **`_conclude_campaign_impl` (`campaigns.py:1280`, unconditionally BEFORE `if claim_path_rel and registered_sha:` at line 1298)**:
     - Resolve `member_ids = union_campaign_member_ids(db, full_id, catalog_dir)` and `validity_map = resolve_validity(member_ids, catalog_dir, db=db, workspace_root=workspace_root)` unconditionally (even when the campaign has no registered claim file!).
     - If any member run has `v.is_hidden`: refuse conclusion with `HIDDEN_STATE_TO_ERROR_CODE[v.state]`.
     - If any member run has `v.state in ("suspect", "unverifiable")`: refuse unless non-empty `ack_suspect` / `ack_unverifiable` is passed, recording `validity.acknowledged`.
     - If member runs span multiple `estimand_marker` epochs for any package: refuse with `BathosErrorCode.ESTIMAND_BOUNDARY_MIXED` unless non-empty `allow_mixed_estimand` is passed, recording `validity.estimand_override_recorded`.
   - **`review_campaign` (`campaigns.py:2075-2097`)**: Resolve validity for all member runs; include `validity_summary` (counts by state, hidden run banners, and estimand epoch breakdown) in the review dict.
8. **`resolve_pin` (`src/bathos/readback.py:96-175`, `src/bathos/mcp.py:550` `resolve_pin_tool`)**:
   - Extend `ResolvedPin` frozen dataclass (`src/bathos/readback.py:96-111`) with two new fields with defaults:
     ```python
     validity_state: str = "current"
     validity_reason: str | None = None
     ```
   - At `readback.py:141`, call `v = resolve_validity([run_id], cat_dir)[run_id]`. If `v.is_hidden`, return `ResolvedPin(..., trust_state=ProductTrustState.UNKNOWN, fresh=False, validity_state=v.state, validity_reason=v.reason)`. Otherwise populate `validity_state=v.state, validity_reason=v.reason`.
9. **`get_trust_state` (`src/bathos/readback.py:253`, `src/bathos/mcp.py:578` `get_trust_state_tool`)**:
   - Add helper `_find_all_runs_with_output_sha256(catalog_dir, content_hash) -> list[str]` scanning `runs` (and `trust_ledger.run_id`).
   - When `fold_trust_state` returns `"promoted"`, resolve validity across all producing `run_id`s. If producing runs exist and **ALL** producing runs have `is_hidden=True`, downgrade the returned trust state at read time to `ProductTrustState.CANDIDATE` without mutating `trust_ledger`. If at least one producing run is `current`, preserve `ProductTrustState.PROMOTED`.
10. **`graduate_product` (`src/bathos/trust_ledger.py:308`, `src/bathos/mcp.py:1212` `graduate_product_tool`) (`M3-7`, `OD-5`)**:
    - Insert the validity check at `src/bathos/trust_ledger.py:359` **BEFORE** the idempotency early return (`if existing is not None and existing.to_state == "promoted": return existing` at lines 360–362).
    - Gather producing run IDs from: caller `run_id`, `existing.run_id` (if `existing` is not None), and `_find_all_runs_with_output_sha256(catalog_dir, content_hash)`.
    - Call `resolve_validity(producing_run_ids, catalog_dir)`. If producing runs exist and all are hidden (`is_hidden=True`), raise `GraduationRefused` (`BathosErrorCode.GRADUATION_REFUSED`)—even if `existing.to_state == "promoted"`. If the only non-hidden producing runs are `suspect` or `unverifiable`, require non-empty `ack_suspect` / `ack_unverifiable` rationale (`OD-5`).
11. **`lineage` (`src/bathos/query.py:833`, `src/bathos/mcp.py:2546` `lineage_prov_tool`, `mcp.py:2577` `mcp_lineage_prov_tool`)**:
    - At `src/bathos/query.py:855`, replace `if not (catalog_dir / "bathos.db").exists(): return []` with `if not catalog_readable(catalog_dir): return []`.
    - At `query.py:880` and `mcp.py:2568`, call `validity_map = resolve_validity([r.id for r in ancestors], cat_dir)`. Retain all ancestors in causal order, attach `validity_map` to `format_prov_json(ancestors, validity_map=validity_map)`, and include `"validity_banners"` and `"estimand_warnings"` in the return dict (`OD-6`).
12. **Maraxiom `review_build` Bridge (`src/bathos/readback.py` export + `maraxiom/src/maraxiom/bathos_bridge.py:1240`)**:
    - In `src/bathos/readback.py`, export `verify_run_drawable(run_id: str, catalog_dir: Path | str | None = None) -> Run`: calls `get_run` and `resolve_validity([run_id], cat_dir)` and raises `ResultHiddenError` (`BathosErrorCode.RESULT_HIDDEN`) if `v.is_hidden`.
    - Additionally, `get_run_tool` (`bth show`) refuses hidden runs by default when `include_superseded=False`, so external consumers calling `bth show` or `verify_run_drawable` are automatically protected. (A companion cross-repo PR in `maraxiom` wires `bathos_bridge.py:1240` to call `verify_run_drawable`.)
13. **SessionStart Hook (`.praxia/manifest.toml:96-100` & `plugin/hooks/hooks.json`)**:
    - Dispatches stdlib-only `bth-session-start-hook` (specified in §4.5).

---

### 3.5 Distance From Present Calculation (D6)

In `src/bathos/validity.py`, `compute_distance_from_present(run: Run, validity: ValidityResult, main_root: Path | None, db: duckdb.DuckDBPyConnection)` returns a structured dict on every run read:
- **`age_days`**: `int` — elapsed days between `run.timestamp` and `datetime.now(UTC)`.
- **`versions_behind`**: `dict[str, int]` — per tracked package, number of distinct released versions in `wheels/` or PyPI metadata between the run's resolved version and the current workspace lockfile pin.
- **`script_commits_since`**: `int | None` — count of git commits touching `run.sidecar_path` or the script path (`git -C <main_root> rev-list --count <run.git_hash>..HEAD -- <script_path> <sidecar_path>`), or `None` if `main_root` is unavailable.
- **`reproduction_count`**: `int` — number of subsequent (`timestamp > run.timestamp`) completed runs with `validity.state == "current"` sharing non-null `sidecar_sha256` whose `outcome == run.outcome` and `run.outcome not in ("", "unknown", "observed", "invalid_measurement", "error")` (§1.4.C item 3).
- **`n_units`**: `int | None` — sample size extracted from `result_schema` / `metadata` when exposed.
- **`version_source`**: `str` — `"recorded" | "verified-from-lock" | "inferred-from-commit" | "unknown"`.

---

### 3.6 CLI & FastMCP Function Signatures (P1)

All existing tool names, CLI command names (`ls`, `find`, `show`, `sql`, `cite`), default limits (`limit: int = 20`), and short flag annotations (`-n`/`-p` at `src/bathos/mcp.py:393-398`) are preserved verbatim. Both the sync `registry="bathos-cli"` functions and the async `registry="bathos"` MCP wrappers are specified:

```python
# In src/bathos/mcp.py:

@cisternal.tool(registry="bathos-cli", name="list_runs", cli_name="ls")
def list_runs_tool(
    catalog_dir: str = "",
    limit: int = 20,
    project: str = "",
    since: str = "",
    status: str = "",
    include_superseded: bool = False,
) -> dict: ...

@cisternal.tool(registry="bathos", name="list_runs")
@traced_tool
async def mcp_list_runs_tool(
    catalog_dir: str = "",
    limit: int = 10,
    include_superseded: bool = False,
) -> dict:
    return list_runs_tool(
        catalog_dir=catalog_dir, limit=limit, include_superseded=include_superseded
    )

@cisternal.tool(registry="bathos-cli", name="find_runs", cli_name="find")
def find_runs_tool(
    catalog_dir: str = "",
    project: str = "",
    tags: list[str] | None = None,
    since: str = "",
    status: str = "",
    slurm_job: str = "",
    output_file: str = "",
    include_superseded: bool = False,
) -> dict: ...

@cisternal.tool(registry="bathos", name="find_runs")
@traced_tool
async def mcp_find_runs_tool(
    catalog_dir: str = "",
    pattern: str = "",
    tags: list[str] | None = None,
    include_superseded: bool = False,
) -> dict:
    return find_runs_tool(
        catalog_dir=catalog_dir,
        project=pattern,
        tags=tags,
        include_superseded=include_superseded,
    )

@cisternal.tool(registry="bathos-cli", name="get_run", cli_name="show")
def get_run_tool(
    run_id: str = "",
    catalog_dir: str = "",
    include_superseded: bool = False,
) -> dict: ...

@cisternal.tool(registry="bathos", name="get_run")
@traced_tool
async def mcp_get_run_tool(
    catalog_dir: str = "",
    run_id: str = "",
    include_superseded: bool = False,
) -> dict:
    return get_run_tool(
        run_id=run_id, catalog_dir=catalog_dir, include_superseded=include_superseded
    )

@cisternal.tool(registry="bathos-cli", name="run_sql", cli_name="sql")
def run_sql_tool(
    sql: str = "",
    catalog_dir: str = "",
    include_superseded: bool = False,
    allow_hidden: bool = False,
) -> dict: ...

@cisternal.tool(registry="bathos", name="run_sql")
@traced_tool
async def mcp_run_sql_tool(
    catalog_dir: str = "",
    sql: str = "",
    include_superseded: bool = False,
    allow_hidden: bool = False,
) -> dict:
    return run_sql_tool(
        sql=sql,
        catalog_dir=catalog_dir,
        include_superseded=include_superseded,
        allow_hidden=allow_hidden,
    )

@cisternal.tool(registry="bathos-cli", name="cite_run", cli_name="cite")
def cite_run_tool(
    run_id: str,
    catalog_dir: str = "",
    format: str = "markdown",
    ack_suspect: str = "",
    ack_unverifiable: str = "",
) -> dict: ...

@cisternal.tool(registry="bathos", name="cite_run")
@traced_tool
async def mcp_cite_run_tool(
    run_id: str,
    catalog_dir: str = "",
    format: str = "markdown",
    ack_suspect: str = "",
    ack_unverifiable: str = "",
) -> dict:
    return cite_run_tool(
        run_id=run_id,
        catalog_dir=catalog_dir,
        format=format,
        ack_suspect=ack_suspect,
        ack_unverifiable=ack_unverifiable,
    )

@cisternal.tool(
    registry="bathos-cli",
    name="defect_declare",
    cli_group="instrument",
    cli_name="declare-defect",
)
def defect_declare_tool(
    package: str,
    defect_id: str,
    introduced: str = "",
    fixed: str = "",
    commit_introduced: str = "",
    commit_fixed: str = "",
    kind: str = "defect",
    severity: str = "void",
    hide_inferred: bool = False,
    revision: int = 1,
    predicate: str = "",
    summary: str = "",
    evidence: str = "",
    user_scope: bool = False,
    catalog_dir: str = "",
) -> dict: ...

@cisternal.tool(registry="bathos", name="defect_declare")
@traced_tool
@require_write_token
async def mcp_defect_declare_tool(
    package: str,
    defect_id: str,
    introduced: str = "",
    fixed: str = "",
    commit_introduced: str = "",
    commit_fixed: str = "",
    kind: str = "defect",
    severity: str = "void",
    hide_inferred: bool = False,
    revision: int = 1,
    predicate: str = "",
    summary: str = "",
    evidence: str = "",
    user_scope: bool = False,
    catalog_dir: str = "",
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token
) -> dict: ...

@cisternal.tool(
    registry="bathos-cli",
    name="defect_list",
    cli_group="instrument",
    cli_name="list-defects",
)
def defect_list_tool(
    package: str = "",
    catalog_dir: str = "",
) -> dict: ...

@cisternal.tool(registry="bathos", name="defect_list")
@traced_tool
async def mcp_defect_list_tool(
    package: str = "",
    catalog_dir: str = "",
) -> dict:
    return defect_list_tool(package=package, catalog_dir=catalog_dir)
```

Additionally:
- `campaign_conclude_tool` (`mcp.py:2334`), `claim_register_tool`, and `graduate_product_tool` (`mcp.py:1212`) gain optional parameters `ack_suspect: str = ""`, `ack_unverifiable: str = ""`, and (on `campaign_conclude_tool` and `claim_register_tool`) `allow_mixed_estimand: str = ""`.
- **`expected=[...]` Registry Wiring (`M3-4`)**: Append `"defect_declare"` and `"defect_list"` to both `wire(None, app, registry="bathos-cli", expected=[...])` in `src/bathos/cli_cyclopts.py:60-147` and `_WIRED = cisternal.wire(app, registry="bathos", expected=[...])` in `src/bathos/mcp.py:6145-6240`.

---

### 3.7 Refusal Codes, Domain Exceptions & Resolution Hints (P1)

In `src/bathos/errors.py`, add enum members to `BathosErrorCode`, register non-empty hints in `RESOLUTION_HINTS`, and map any new domain exception class (`ResultHiddenError`, `EstimandBoundaryMixedError`, `UnacknowledgedValidityError`) in `EXCEPTION_TO_CODE` (`src/bathos/errors.py:278-375`) so `tests/test_errors.py::test_every_domain_exception_has_registered_code` passes:

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
- `RESULT_AFFECTED`: *"The requested run is implicated by an active blast-radius ledger entry. Re-run or clear the flag with audit justification via `bth blast-radius clear`."*
- `RESULT_SUPERSEDED`: *"The requested run was superseded by a newer run. Use --include-superseded to inspect, or query the successor run."*
- `RESULT_ARCHIVED`: *"The requested script or outputs were archived. Use `bth restore --item-id <id>` to restore files to workspace."*
- `RESULT_HIDDEN`: *"The requested run is hidden by default due to void, affected, superseded, or archived status. Pass --include-superseded to inspect."*
- `RESULT_SUSPECT_UNACKNOWLEDGED`: *"Run was produced by an unverified toolchain matching a defect range or lacks a required predicate field. To proceed, pass --ack-suspect '<rationale>' (ack_suspect='<rationale>')."*
- `RESULT_UNVERIFIABLE_UNACKNOWLEDGED`: *"Run provenance cannot be verified against repository lockfiles. To proceed, pass --ack-unverifiable '<rationale>' (ack_unverifiable='<rationale>')."*
- `ESTIMAND_BOUNDARY_MIXED`: *"Attempted to combine runs across an estimand change boundary at a confirmatory gate. Results compute different quantities; separate by epoch or pass --allow-mixed-estimand '<rationale>'."*

---

### 3.8 Acceptance Criteria (P1)

- **AC-P1-01**: Declaring a defect range in `.bth/instruments/aminx.toml` with `introduced = "0.1.0a0"`, `fixed = "0.1.0a23"`, and predicate `sample_s > 1` causes a run on a verified lockfile (`version_source = "verified-from-lock"`) with `metadata.sample_s = 4` to resolve to `state = "void"`.
- **AC-P1-02 (`OD-2`)**: A run matching the version range of AC-P1-01 with `metadata.sample_s = 1` evaluates the predicate to `False` and resolves to `state = "current"`, whereas a run matching the version range whose `metadata` lacks `sample_s` resolves to `state = "suspect"` with `reason = "missing predicate field: sample_s"` (never `current`, never `void` unless `hide_inferred = true` is set).
- **AC-P1-03 (`OD-1`)**: A run matching AC-P1-01 executed with an unverified git commit (`version_source = "inferred-from-commit"`) resolves to `state = "suspect"` when `hide_inferred = false`, and resolves to `state = "void"` (`is_hidden = True`) when the registry entry sets `hide_inferred = true`.
- **AC-P1-04**: `bth ls` and `list_runs_tool` exclude `void`, `affected`, `superseded`, and `archived` runs by default (pushing the filter down before `LIMIT` so pages are never under-filled) and print `N superseded/void/archived results hidden — --include-superseded to show`.
- **AC-P1-05**: `bth show <id>` (`get_run_tool`) on a hidden run returns error code `RESULT_HIDDEN` naming `validity_state`, `defect_id`, `reason`, and `replacement_id`.
- **AC-P1-06**: `bth show <id> --include-superseded` displays the void run with a prominent `[VOID]` banner.
- **AC-P1-07**: `bth cite <id>` refuses hidden runs with their specific state code (`RESULT_VOID`, `RESULT_AFFECTED`, `RESULT_SUPERSEDED`, `RESULT_ARCHIVED`).
- **AC-P1-08 (`OD-5`)**: `bth cite <id>` on a `suspect` run refuses with `RESULT_SUSPECT_UNACKNOWLEDGED` when `ack_suspect` is empty or whitespace-only, and succeeds when `--ack-suspect "<reason>"` is supplied while appending a `validity.acknowledged` record to `acknowledgment_ledger`.
- **AC-P1-09**: `claim register`, `claim validate`, and `campaign attest-parity` refuse if any supporting member run or `parity_run_id` resolves to a hidden state.
- **AC-P1-10**: `campaign conclude` refuses if any campaign member run resolves to a hidden state—both when a claim is registered (Union Gate) and when no claim file is registered (`claim_path IS NULL`).
- **AC-P1-11**: `graduate_product` checks producing run validity *before* the idempotency early return (`trust_ledger.py:360`) and refuses graduation with `GRADUATION_REFUSED` if all producing runs resolve to a hidden state.
- **AC-P1-12**: `get_trust_state` drops a previously promoted product to `candidate` at read time if all producing runs become hidden, with zero modifications to `trust_ledger` files; if at least one producing run is `current`, `promoted` is preserved.
- **AC-P1-13 (Non-Obliteration Invariant Test)**: Executing `resolve_validity` across 1,000 runs produces zero byte modifications or row mutations/deletions in cool-tier run Parquet fragments, `.bth/log/*.jsonl` run events, or historical rows in `runs`, `blast_radius_ledger`, `trust_ledger`, and `archived_items` (derived cache writes occur solely in `validity_cache.db`).
- **AC-P1-14 (`OD-3`)**: `SELECT * FROM runs_current` never returns a row where `validity_state IN ('void', 'affected', 'superseded', 'archived')`, even for runs ingested after the previous cache build.
- **AC-P1-15 (`OD-3`)**: `run_sql` on base table `runs` (including `FROM "runs"` and `FROM idx.runs`) when the catalog contains hidden rows emits a warning notice to stderr (CLI) or attaches `"notice"` in the MCP response envelope unless `include_superseded=True` or `allow_hidden=True`.
- **AC-P1-16**: Runner automatically populates `dependency_provenance` when executed in a workspace with active project-local or opted-in user-global (`OD-4`) instrument defect files.
- **AC-P1-17**: `lineage` works in both log mode (`index.db`) and legacy warm mode (`bathos.db`), returning all ancestors while flagging hidden ancestors and mixed estimand epochs with banner metadata.
- **AC-P1-18 (`OD-6`)**: Pooling runs across an `estimand_change` boundary (including `<pkg>:unknown` mixed with a known epoch) emits a warning banner without refusing in `campaign add`, and fails with `ESTIMAND_BOUNDARY_MIXED` in `campaign conclude` and `claim register` unless overridden with a non-empty `--allow-mixed-estimand "<reason>"` justification (which records an append-only `validity.estimand_override_recorded` event).
- **AC-P1-19 (D1 / D10)**: Commit-range defects (`type = "GIT"`) match against `dependency_provenance.<pkg>.commit` or `run.git_hash` (for repo-own packages like `asr`), and clearing a `dependency_range` flag via `bth blast-radius clear` exempts the run for `<entry_id>@r<revision>` until `revision` is incremented.

---

### 3.9 Test Plan & Risks (P1)

#### A. P1 Test Plan (`tests/test_validity_p1.py`)
All tests run in isolated `tmp_path` fixtures with `BTH_CATALOG_DIR` and `HOME` redirected (never touching `~/.bth`, `asr`, or `tev_design`), executed via `uv run pytest tests/test_validity_p1.py tests/test_errors.py`:
1. **Unit — Predicate & Version Resolution (`test_predicate_and_version_hierarchy`)**:
   - Verify Tier 1 (`dependency_provenance`), Tier 2 (`verified-from-lock`), Tier 3 (`metadata.aminx_version` including `0.2.1+armask` PEP 440 local version base comparison and `inferred-from-commit`), and Tier 4 (`unknown`, including missing `main_root` fallback).
   - Verify `OD-2`: present `sample_s = 4` $\rightarrow$ `void`; present `sample_s = 1` $\rightarrow$ `current`; missing `sample_s` $\rightarrow$ `suspect` (`reason = "missing predicate field: sample_s"`).
   - Verify `OD-1`: `inferred-from-commit` caps at `suspect` when `hide_inferred = False`, and resolves to `void` (`is_hidden = True`) when `hide_inferred = True`.
2. **Unit — Git Commit Ranges & Per-Revision Exemptions (`test_git_range_and_exemption_revision`)**:
   - Create a synthetic git repo with 3 commits (`c1` introduced, `c2` defective, `c3` fixed); verify `type = "GIT"` flags `c1`/`c2` and spares `c3`.
   - Call `clear_blast_radius_flag` for `AMINX-2026-001@r1`; verify run resolves to `current` at `revision = 1`, and re-resolves to `void` when entry is bumped to `revision = 2`.
3. **Unit — Read-Only Log-Mode Cache & `inputs_hash` (`test_log_mode_connect_read_validity_cache`)**:
   - In log mode (`index.db` attached `READ_ONLY`), verify `connect_read()` refreshes `validity_cache.db` without `InvalidInputException`.
   - Modify `.bth/instruments/aminx.toml` in place and ingest a new defective run; verify `inputs_hash` changes and `SELECT * FROM runs_current` immediately excludes the new defective run.
4. **Integration — All 13 Surfaces & Gate Refusals (`test_all_p1_surfaces`)**:
   - Exercise `ls` (pagination with hidden rows before `LIMIT`), `find`, `show`, `run_sql` (`OD-3` notice on `FROM runs`, `"runs"`, `idx.runs`), `cite` (`OD-5` rationale enforcement + ledger check), `claim register`, `claim validate`, `attest_parity`, `campaign add` (`OD-6` banner), `campaign conclude` (with and without registered claim, plus `OD-6` `allow_mixed_estimand`), `resolve_pin` (`ResolvedPin.validity_state`), `get_trust_state` (multi-run reproduction preservation vs single-run downgrade), `graduate_product` (refusal before line 360 early return), `lineage`, and `verify_run_drawable`.
5. **Invariance Test (`test_non_obliteration_invariant_ac_p1_13`)**:
   - Record SHA256 digests of all cool-tier Parquet fragments, `.bth/log/*.jsonl` files, and row counts/hashes of `runs`, `blast_radius_ledger`, `trust_ledger`, and `archived_items` before and after `resolve_validity` over 1,000 runs; assert 100% byte and row identity.

#### B. P1 Risks & Mitigations
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| **R-P1-1**: Subprocess overhead of `git show <hash>:uv.lock` across thousands of historical runs slows down initial `bth ls`. | High | Medium | Deduplicate `(main_root, git_hash, dependency_lock_sha256)` pairs before invoking `git` (in `tev_design`, 1,596 lock-hashed runs collapse to <150 unique pairs) and persist results in `validity_cache.db:version_cache` and `.bth/cache/version_cache.json`. |
| **R-P1-2**: Concurrent readers in log mode contend when refreshing `validity_cache.db`. | Medium | High | Keep `index.db` strictly `READ_ONLY` in `connect_read()`; guard `validity_cache.db` writes with `<catalog_dir>/validity_cache.lock` and atomic transaction swap so readers never fail with DuckDB lock errors. |
| **R-P1-3**: Historical runs in `tev_design` lack `metadata.sample_s` and lack `dependency_lock_sha256` on `<= 0.1.0a22`, resulting in 0 hidden runs if `hide_inferred` is not configured. | Confirmed | High | Solved by `OD-2` (`missing predicate field: sample_s` $\rightarrow$ `suspect`) and `OD-1` (`hide_inferred = true` opt-in after owner reviews side-by-side P1 dry-run counts). |

---

### 3.10 Fixer Task Decomposition (P1)

```
[Task P1.1: Error Codes, Exceptions & Schemas]
       │
       ▼
[Task P1.2: Validity Engine, Cache & Blast Anchor] ◄── [Task P1.3: Version Resolver & Runner Fix]
       │
       ├─────────────────────────────────┬─────────────────────────────────┐
       ▼                                 ▼                                 ▼
[Task P1.4: Query Surfaces & SQL View]  [Task P1.5: Citation, Claim &     [Task P1.6: Trust Ledger, Pin &
                                         Campaign Gates (OD-5, OD-6)]      Maraxiom Bridge Seams]
       │                                 │                                 │
       └─────────────────────────────────┼─────────────────────────────────┘
                                         ▼
                               [Task P1.7: P1 Integration & Invariant Tests]
```

1. **Task P1.1**: Add P1 error codes (`RESULT_VOID`..`ESTIMAND_BOUNDARY_MIXED`) and domain exceptions (`ResultHiddenError`, `EstimandBoundaryMixedError`, `UnacknowledgedValidityError`) to `src/bathos/errors.py` (registering all in `RESOLUTION_HINTS` and `EXCEPTION_TO_CODE`). Add `acknowledgment_ledger` DDL and `toolchain_version_source` to `src/bathos/index.py` and `fold_runs.py`; add `script_sha256` and `run_ids` to `src/bathos/archived_items.py` and `ARCHIVED_ITEMS_DDL`; bump legacy `schema.py` / `migrate.py` to v20.
2. **Task P1.2**: Author `src/bathos/validity.py` (`ValidityResult`, `resolve_validity()`, `OD-2` AST column-presence check, `OD-1` `hide_inferred` logic, `OD-4` user+project registry loader, `validity_cache.db` management with `compute_validity_inputs_hash`, and `compute_distance_from_present`). Extend `src/bathos/blast_radius.py:BlastRadiusRecord` (`:97`) with `anchor_kind = "dependency_range"` and per-revision clear semantics (`<entry_id>@r<revision>`).
3. **Task P1.3**: Author `src/bathos/version_resolver.py` implementing the 4-tier hierarchy (including `metadata.<pkg>_version`, PEP 440 local-version `.base_version` comparison, `type = "GIT"` commit ancestry matching, and missing-`main_root` fallback). Update `src/bathos/runner.py` and `src/bathos/dependency_provenance.py` to auto-track registry packages.
4. **Task P1.4**: Wire `ensure_validity_cache` and `runs_current` view into `src/bathos/index.py:connect_read` (`:476-510`) and `src/bathos/query.py` (`list_runs`, `find_runs`, `get_run`, `run_sql` with `_RAW_RUNS_RE` notice, `lineage` with `catalog_readable`). Update `src/bathos/mcp.py` (`list_runs_tool:329`, `find_runs_tool:401`, `get_run_tool:456`, `run_sql_tool:513`, `lineage_prov_tool:2546`, plus async `mcp_*` wrappers and new `defect_declare` / `defect_list` tools) and add `defect_declare`, `defect_list` to `expected` in `src/bathos/cli_cyclopts.py:60` and `src/bathos/mcp.py:6145`.
5. **Task P1.5**: Wire validity gates, `OD-5` rationale strings (`ack_suspect`, `ack_unverifiable`), and `OD-6` estimand checks (`allow_mixed_estimand`) into `src/bathos/cite.py`, `src/bathos/claim.py` (`register_claim:657`, `validate_claim:344`, `attest_parity:1063`, `differential_confound_check:851`, `run_union_gate:932`), and `src/bathos/campaigns.py` (`add_run_to_campaign:532`, `_conclude_campaign_impl:1280` before line 1298, `review_campaign:2075`).
6. **Task P1.6**: Add `validity_state` and `validity_reason` to `ResolvedPin` (`src/bathos/readback.py:96`), wire `resolve_pin` (`:140`), `get_trust_state` (`:262`), and `verify_run_drawable` (for Maraxiom `bathos_bridge.py:1240`), and wire `graduate_product` (`src/bathos/trust_ledger.py:359`, before the line 360 early return).
7. **Task P1.7**: Author and run `tests/test_validity_p1.py` and `tests/test_errors.py`.

---

## 4. Phase 2 (P2): Findings + Evidence Tiers

### 4.1 Scope & Objectives
P2 provides the positive counterweight to invalidation: recording current beliefs and evidence certainty to prevent circular re-litigation. It implements evidence tiers including `spike` and `undeclared` (D4), typed supersession edges (D5), the lightweight findings register with an automated downgrade ladder, reopen guard, and estimand-mixing refusal on settled findings (D7, `OD-6`), and the byte-capped session-start view injected via `hookSpecificOutput.additionalContext` (D7).

---

### 4.2 Evidence Tiers & Vocabulary Reconciliation (D4, `m3-4`)

#### A. Canonical Epistemic Hierarchy (GRADE-Inspired)
1. **`confirmatory`**: Pre-registered, hypothesis-testing runs with rigorous controls and replication.
2. **`calibrating`**: Parameter sweeps, assay titrations, and baseline calibrations.
3. **`exploratory`**: Hypothesis-generating probes, initial feasibility screens.
4. **`spike` (New)**: Throwaway assumption probes; provenance is recorded, but a spike run **CAN NEVER SUPPORT A FINDING ABOVE `tentative`** and is excluded from session-start summaries.
5. **`undeclared` (Read-Time Display)**: Runs whose sidecar omitted `stage_name` (`stage_name_declared == False`). Displayed explicitly as `"undeclared"` on reads so an omitted stage is never mistaken for an intentional `"exploratory"` choice.

#### B. Integration with `src/bathos/sidecar.py` (`:461-470, 570-593, 1317-1345`)
1. In `src/bathos/sidecar.py:461-470`, add `"spike"` to `CANONICAL_STAGES` and `EPISTEMIC_CLASSES`:
   ```python
   CANONICAL_STAGES = {
       "exploration",
       "calibration",
       "validation",
       "ablation",
       "production",
       "spike",
   }
   EPISTEMIC_CLASSES = ("spike", "exploratory", "calibrating", "confirmatory", "legacy")
   ```
2. In `src/bathos/sidecar.py:570-578`, accept `"confirmatory"` and `"power"` (the 8 in-the-wild sidecars in `asr`, E9) as recognized stage aliases without coercing them to `"exploration"` before `resolve_epistemic_class` / `resolve_epistemic_tier` inspects `stage_name_raw`:
   ```python
   STAGE_ALIASES: dict[str, str] = {
       "confirmatory": "validation",
       "power": "validation",
   }
   STAGE_TO_EPISTEMIC_TIER: dict[str, str] = {
       "validation": "confirmatory",
       "ablation": "confirmatory",
       "production": "confirmatory",
       "confirmatory": "confirmatory",
       "power": "confirmatory",
       "calibration": "calibrating",
       "exploration": "exploratory",
       "spike": "spike",
   }

   def resolve_epistemic_tier(
       *,
       stage_name_declared: bool,
       stage_name_raw: str | None,
       epistemic_class: str | None = None,
   ) -> str:
       if not stage_name_declared:
           return "undeclared"
       if stage_name_raw and stage_name_raw in STAGE_TO_EPISTEMIC_TIER:
           return STAGE_TO_EPISTEMIC_TIER[stage_name_raw]
       if epistemic_class in ("confirmatory", "calibrating", "exploratory", "spike"):
           return epistemic_class
       return "undeclared"
   ```
3. In `src/bathos/sidecar.py:542`, add `"question_slug"` and `"reopen_reason"` to `EXPERIMENT_KEYS` so sidecars can optionally link to a finding question slug.

---

### 4.3 Typed Supersession Edges (D5)

Supersession edges are stored as append-only records in DuckDB and folded from `.bth/log/*.jsonl`:

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

In log mode, `supersede_edge_add` wraps execution in `unit_of_work(catalog_dir)` and emits `"supersession.declared"` via `emit_event()`, which `src/bathos/runlog/fold_runs.py` / `ingest.py` folds into `supersession_edges`. Existing `[status] superseded_by` strings in sidecars are parsed into `sidecar -> sidecar` edges when they resolve to an existing sidecar stem/path, or flagged by `bth lint` otherwise.

---

### 4.4 Findings Register, Downgrade Ladder & Reopen Guard (D7, `OD-6`)

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
reopen_reason = ""
allow_mixed_estimand = ""
declared_at = "2026-11-06T14:30:00Z"
declared_by = "marielle"
last_reviewed = "2026-11-06T14:30:00Z"
```

#### B. Dynamic Downgrade Ladder & Settling Gate (`OD-6`)
When reading a finding via `bth findings get`, `bth findings list`, or `session-start`:
1. Resolve validity of all `supporting_runs` via `resolve_validity(supporting_runs, catalog_dir)`.
2. **Automated Downgrade Ladder**:
   - If `len(supporting_runs) > 0` AND **ALL** supporting runs are hidden (`void`, `affected`, `superseded`, or `archived`), finding status dynamically resolves at read time to **`needs-recheck`** (for findings whose stored status is `settled`, `tentative`, or `refuted`), without mutating the `.bth/findings/<slug>.toml` file on disk.
   - An `open` finding with `supporting_runs = []` retains `status = "open"`.
   - A finding with `status = "superseded"` retains `status = "superseded"`.
3. **Settling Gate (`status == "settled"`)**:
   - If all active supporting runs have `evidence_tier == "spike"`, refuse with `BathosErrorCode.SPIKE_TIER_CANNOT_SETTLE`.
   - Require $\ge 1$ active (`state == "current"`) supporting run with `evidence_tier == "confirmatory"`; otherwise refuse with `BathosErrorCode.FINDING_SETTLED_EVIDENCE_INSUFFICIENT`.
   - **Estimand Mixing Check (`OD-6` `(owner: 2026-11-06)`)**: If `status == "settled"` and `supporting_runs` span multiple `estimand_marker` epochs for any package (including `"<pkg>:unknown"` mixed with a known epoch), refuse with `BathosErrorCode.ESTIMAND_BOUNDARY_MIXED` unless `allow_mixed_estimand.strip()` is non-empty (recording `validity.estimand_override_recorded`).

#### C. Reopen Guard Across Findings and Experiment Scaffolding (`m3-4`)
1. **On `finding_create`**: If `question_slug` (or normalized `question` text) matches an existing `settled` finding and `not reopen_reason.strip()`, refuse with `BathosErrorCode.FINDING_REOPEN_REASON_REQUIRED` and display the settled answer. When `reopen_reason` is provided, the new finding is created in `tentative` status with `supersedes = <settled_finding.id>`. The prior `settled` finding remains `settled` until the new finding reaches `status = "settled"` with confirmatory evidence, at which point the `finding -> finding` supersession edge activates and transitions the predecessor to `superseded`.
2. **On `bth new-experiment`, `bth preflight`, and `bth run`**: When `--question-slug <slug>` (on `new-experiment`) or `[experiment] question_slug = "<slug>"` (in `.bth.toml`) matches an existing `settled` finding, bathos prints the settled finding's question and answer and refuses with `FINDING_REOPEN_REASON_REQUIRED` unless `[experiment] reopen_reason = "<why>"` (or `--reopen-reason "<why>"`) is supplied. Supplying `reopen_reason` on a sidecar/run logs the reopen intent without mutating the settled finding's status.

---

### 4.5 Session-Start Current Beliefs View (D7, `B3-5`)

#### Fast-Path Hook Execution & Claude Code Context Injection
Because importing the full `bathos.cli_cyclopts` / `bathos.mcp` stack takes ~2.7 s, `SessionStart` uses the same stdlib-only fast-path pattern as `bth-preflight-hook` (`src/bathos/hooks/preflight_hook.py`):
1. **Dedicated Stdlib-Only Script**: `src/bathos/hooks/session_start_hook.py`, installed as console script `bth-session-start-hook` in `pyproject.toml:[project.scripts]`. Imports only `sys`, `os`, `json`, `pathlib`.
2. **Pre-Rendered Cache**: Reads `<main_root>/.bth/cache/session_start.json` (maintained by `src/bathos/session_start.py:generate_session_start_cache()` whenever findings, defects, or runs are updated). If `.bth/cache/session_start.json` does not exist, exits `0` with empty stdout.
3. **Claude Code `SessionStart` Context Injection Protocol (`B3-5`)**:
   As noted in `src/bathos/hooks/preflight_hook.py:21`, top-level `{"systemMessage": "..."}` is shown only to the human user in the terminal UI and is **not** injected into the model's context. To inject the current-beliefs summary directly into the agent's context window on `SessionStart`, `bth-session-start-hook` outputs:
   ```json
   {
     "hookSpecificOutput": {
       "hookEventName": "SessionStart",
       "additionalContext": "! ALERT: aminx <= 0.1.0a22 void range active (S > 1)\n? NEEDS RECHECK: 'tev-site-stability' (all supporting runs voided by AMINX-2026-001)\n* SETTLED: 'pdz-affinity-ceiling' -> Kd floor is 120nM [confirmatory, 14d ago]\no OPEN: 'necklace-poe-opt' [tentative, 2d ago]"
     }
   }
   ```
   and exits `0` in $\le 15\text{ ms}$.
4. **Source-of-Truth Registration in `.praxia/manifest.toml` (`:96-100`) and `plugin/hooks/hooks.json`**:
   Because `cisternal` generates `plugin/hooks/hooks.json` from `.praxia/manifest.toml`, register the hook in **both** files:
   ```toml
   # In .praxia/manifest.toml:
   [[plugin.hook_specs]]
   event    = "SessionStart"
   matcher  = ".*"
   script   = "bth-session-start-hook"
   surfaces = ["claude"]
   ```
   and in `plugin/hooks/hooks.json` under `"SessionStart"`.

---

### 4.6 CLI & FastMCP Function Signatures (P2)

```python
# In src/bathos/mcp.py:

@cisternal.tool(registry="bathos-cli", name="finding_create", cli_group="findings", cli_name="create")
def finding_create_tool(
    question_slug: str,
    question: str,
    answer: str,
    supporting_runs: list[str] | None = None,
    status: str = "tentative",
    supersedes: str = "",
    reopen_reason: str = "",
    allow_mixed_estimand: str = "",
    catalog_dir: str = "",
) -> dict: ...

@cisternal.tool(registry="bathos", name="finding_create")
@traced_tool
@require_write_token
async def mcp_finding_create_tool(
    question_slug: str,
    question: str,
    answer: str,
    supporting_runs: list[str] | None = None,
    status: str = "tentative",
    supersedes: str = "",
    reopen_reason: str = "",
    allow_mixed_estimand: str = "",
    catalog_dir: str = "",
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token
) -> dict: ...

@cisternal.tool(registry="bathos-cli", name="finding_get", cli_group="findings", cli_name="get")
def finding_get_tool(
    slug_or_id: str,
    catalog_dir: str = "",
) -> dict: ...

@cisternal.tool(registry="bathos", name="finding_get")
@traced_tool
async def mcp_finding_get_tool(
    slug_or_id: str,
    catalog_dir: str = "",
) -> dict:
    return finding_get_tool(slug_or_id=slug_or_id, catalog_dir=catalog_dir)

@cisternal.tool(registry="bathos-cli", name="finding_list", cli_group="findings", cli_name="list")
def finding_list_tool(
    status: str = "",
    include_superseded: bool = False,
    catalog_dir: str = "",
) -> dict: ...

@cisternal.tool(registry="bathos", name="finding_list")
@traced_tool
async def mcp_finding_list_tool(
    status: str = "",
    include_superseded: bool = False,
    catalog_dir: str = "",
) -> dict:
    return finding_list_tool(
        status=status, include_superseded=include_superseded, catalog_dir=catalog_dir
    )

@cisternal.tool(registry="bathos-cli", name="supersede_edge_add", cli_group="supersede", cli_name="add")
def supersede_edge_add_tool(
    entity_type: str,
    entity_id: str,
    superseded_by_type: str,
    superseded_by_id: str,
    reason: str,
    catalog_dir: str = "",
) -> dict: ...

@cisternal.tool(registry="bathos", name="supersede_edge_add")
@traced_tool
@require_write_token
async def mcp_supersede_edge_add_tool(
    entity_type: str,
    entity_id: str,
    superseded_by_type: str,
    superseded_by_id: str,
    reason: str,
    catalog_dir: str = "",
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token
) -> dict: ...

@cisternal.tool(registry="bathos-cli", name="session_start", cli_name="session-start")
def session_start_tool(
    byte_cap: int = 2048,
    catalog_dir: str = "",
) -> dict: ...

@cisternal.tool(registry="bathos", name="session_start")
@traced_tool
async def mcp_session_start_tool(
    byte_cap: int = 2048,
    catalog_dir: str = "",
) -> dict:
    return session_start_tool(byte_cap=byte_cap, catalog_dir=catalog_dir)
```

- **`expected=[...]` Registry Wiring (`M3-4`)**: Append `"finding_create"`, `"finding_get"`, `"finding_list"`, `"supersede_edge_add"`, and `"session_start"` to both `wire(None, app, registry="bathos-cli", expected=[...])` in `src/bathos/cli_cyclopts.py:60-147` and `_WIRED = cisternal.wire(app, registry="bathos", expected=[...])` in `src/bathos/mcp.py:6145-6240`.

---

### 4.7 Refusal Codes & Resolution Hints (P2)

In `src/bathos/errors.py` (registered in `BathosErrorCode`, `RESOLUTION_HINTS`, and `EXCEPTION_TO_CODE`):
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

- **AC-P2-01**: Sidecar declaring `stage_name = "spike"` resolves to `epistemic_class = "spike"` and `evidence_tier = "spike"`.
- **AC-P2-02**: A finding declaring `status = "settled"` backed exclusively by spike runs is refused with `SPIKE_TIER_CANNOT_SETTLE`.
- **AC-P2-03 (`m3-4`)**: Sidecars with undeclared `stage_name` (`stage_name_declared == False`) resolve to `evidence_tier = "undeclared"` and are displayed as `"undeclared"` on reads rather than `"exploratory"`.
- **AC-P2-04**: Adding a supersession edge `run_A -> run_B` causes `run_A` to resolve to `state = "superseded"` and hide from default reads.
- **AC-P2-05 (`m3-4`)**: Creating a finding or scaffolding/running an experiment declaring a `question_slug` matching an existing `settled` finding without `--reopen-reason` / `reopen_reason` is refused with `FINDING_REOPEN_REASON_REQUIRED` and prints the settled answer. When `--reopen-reason` is provided on `finding_create`, the new finding starts in `tentative` and the predecessor remains `settled` until the new finding reaches `settled`.
- **AC-P2-06**: When all supporting runs of an active finding are hidden (`len(supporting_runs) > 0` and all hidden), querying the finding dynamically returns `status = "needs-recheck"`. Open findings with `supporting_runs = []` retain `status = "open"`.
- **AC-P2-07 (`B3-5`)**: `bth-session-start-hook` reads `.bth/cache/session_start.json` and emits `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}` with `additionalContext` $\le 2,048$ bytes in $\le 15\text{ ms}$.
- **AC-P2-08**: `bth session-start` lists active defect alerts and `needs-recheck` findings first, followed by settled and open findings.
- **AC-P2-09**: `bth session-start` never includes items with status `superseded` or `void`, and never includes `spike` runs.
- **AC-P2-10**: `finding_list_tool` hides superseded findings unless `include_superseded=True`.
- **AC-P2-11**: W3C PROV-JSON output in `src/bathos/provenance.py` via `format_prov_json(runs, validity_map=...)` emits `prov:wasInvalidatedBy` for void runs and `prov:wasRevisionOf` for superseded entities.
- **AC-P2-12 (`m3-4`)**: Non-canonical in-the-wild stages `"confirmatory"` and `"power"` (`stage_name_raw`) map to tier `"confirmatory"` without being coerced to `"exploratory"`.
- **AC-P2-13**: Supersession edges support bidirectional querying (`replaces` vs `replaced_by`).
- **AC-P2-14**: Finding status flips dynamically at read time without modifying the underlying `.bth/findings/<slug>.toml` file.
- **AC-P2-15 (`B3-5`)**: `SessionStart` hook is registered in both `.praxia/manifest.toml` (`[[plugin.hook_specs]]`) and `plugin/hooks/hooks.json` pointing to `bth-session-start-hook`.
- **AC-P2-16 (`OD-6`)**: Settling a finding (`status = "settled"`) whose `supporting_runs` mix runs across an `estimand_change` boundary fails with `ESTIMAND_BOUNDARY_MIXED` unless `allow_mixed_estimand` rationale is provided.

---

### 4.9 Test Plan & Risks (P2)

#### A. P2 Test Plan (`tests/test_findings_p2.py`)
Executed via `uv run pytest tests/test_findings_p2.py tests/test_errors.py`:
1. **Unit — Epistemic Tiers & Sidecar Parsing (`test_epistemic_tiers_and_aliases`)**:
   - Verify `stage_name = "spike"` $\rightarrow$ `"spike"`, omitted `stage_name` (`stage_name_declared=False`) $\rightarrow$ `"undeclared"`, and `stage_name_raw in ("confirmatory", "power")` $\rightarrow$ `"confirmatory"`.
2. **Unit — Supersession Edges in Log Mode (`test_supersession_edges_log_mode`)**:
   - Emit `supersession.declared` under `unit_of_work`, verify fold into `supersession_edges`, bidirectional lookup, and read-time hiding of superseded runs/findings.
3. **Unit — Findings Lifecycle, Downgrade Ladder, Reopen Guard & `OD-6` (`test_findings_lifecycle`)**:
   - Verify `SPIKE_TIER_CANNOT_SETTLE`, `FINDING_SETTLED_EVIDENCE_INSUFFICIENT`, dynamic `needs-recheck` when supporting runs become `void` (with TOML file on disk unchanged), empty `supporting_runs = []` staying `open`, reopen guard on both `finding_create` and `bth run`/`preflight` with `question_slug`, and `ESTIMAND_BOUNDARY_MIXED` on settling across an estimand boundary.
4. **Integration — SessionStart Hook Contract & Latency (`test_session_start_hook`)**:
   - Invoke `bth-session-start-hook` as a subprocess; assert exit code `0`, wall time $< 100\text{ ms}$, valid JSON matching `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ...}}`, byte length $\le 2,048$, and `.praxia/manifest.toml` parity with `plugin/hooks/hooks.json`.

#### B. P2 Risks & Mitigations
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| **R-P2-1**: `cisternal` export overwrites `plugin/hooks/hooks.json` and drops `SessionStart`. | High (if omitted) | High | Register `[[plugin.hook_specs]]` in `.praxia/manifest.toml:96-100` alongside `plugin/hooks/hooks.json` and assert parity in `test_session_start_hook`. |
| **R-P2-2**: `parse_sidecar` coerces `"confirmatory"`/`"power"` to `"exploration"` before tier resolution. | High (if unaddressed) | Medium | Resolve tier from `sidecar.stage_name_declared` and `sidecar.stage_name_raw` (`STAGE_TO_EPISTEMIC_TIER`) before fallback coercion. |

---

### 4.10 Fixer Task Decomposition (P2)

1. **Task P2.1**: Update `src/bathos/sidecar.py` (`CANONICAL_STAGES`, `EPISTEMIC_CLASSES`, `EXPERIMENT_KEYS`, `STAGE_TO_EPISTEMIC_TIER`, `resolve_epistemic_tier`). Add P2 error codes and exceptions to `src/bathos/errors.py`.
2. **Task P2.2**: Author `src/bathos/supersession.py` managing typed supersession edges, `unit_of_work` log-mode event emission (`supersession.declared`), and `src/bathos/index.py` DDL/folding.
3. **Task P2.3**: Author `src/bathos/findings.py` implementing finding CRUD, dynamic downgrade ladder, `OD-6` estimand boundary check on `status == "settled"`, and reopen guard (plus `question_slug` preflight check in `src/bathos/prereg.py` / `new_experiment.py`).
4. **Task P2.4**: Author `src/bathos/session_start.py` (`generate_session_start_cache`) and `src/bathos/hooks/session_start_hook.py` emitting `hookSpecificOutput.additionalContext`.
5. **Task P2.5**: Wire P2 tools into `src/bathos/mcp.py` (both `registry="bathos-cli"` and `@require_write_token` `registry="bathos"` wrappers) and update `expected` in `src/bathos/cli_cyclopts.py:60` and `src/bathos/mcp.py:6145`. Register `bth-session-start-hook` in `pyproject.toml`, `.praxia/manifest.toml`, and `plugin/hooks/hooks.json`.
6. **Task P2.6**: Extend `src/bathos/provenance.py:format_prov_json(runs, *, run_parent_edges=None, validity_map=None)` with `prov:wasInvalidatedBy` and `prov:wasRevisionOf`.
7. **Task P2.7**: Author and run `tests/test_findings_p2.py`.

---

## 5. Phase 3 (P3): Archive Sweep & Migration

### 5.1 Scope & Objectives
P3 performs system cleanup: batch archival sweep of dead scripts, void outputs, and stale docs (D8), praxia-compatible document store integration (D8/D9), diagnostic telemetry rotation (`~/.bth/catalog/logs/`), and historical backfill of `asr` and `tev_design` (D9/D10, `OD-1`).

---

### 5.2 Batch Archive Sweep (D8, `M3-5`, `M3-10`)

#### A. Sweep Candidate Selection Criteria
A file, script, or log is proposed for archival if it satisfies any of:
1. Sidecar carries `[status] stale = true`.
2. Produces runs that are 100% `void` or `superseded`.
3. Script unreferenced for $\ge N$ days (default $N = 90$, an arbitrary initial engineering cutoff; configurable via `--age-days`) AND cited by zero current findings or claims.
4. Documentation files in the configured doc store citing exclusively voided, superseded, or archived runs/findings.
5. Diagnostic telemetry log files in `~/.bth/catalog/logs/events.*.jsonl*` older than 30 days or exceeding 10 MB.

#### B. Execution Workflow (Reconciled with `artifact_archive.py` & `restore_tool` — `M3-10`)
1. `bth archive-sweep --dry-run`: Scans workspace and emits candidate manifest `.bth/archive_proposals/sweep_<date>.toml` without modifying any files.
2. Owner inspects proposal and approves: `bth archive-sweep --apply --proposal .bth/archive_proposals/sweep_<date>.toml`.
3. **Atomic Batch Archive Execution (Preserving `restore_archived_item` Compatibility)**:
   - **Experiment Scripts, Sidecars & Untracked Outputs (`kind = "experiment"`)**:
     - Reuses `src/bathos/artifact_archive.py:archive_experiment_bundle` (`:236-375`) with a new `commit: bool = True` parameter (passed as `commit=False` per item during the loop, then committed once at the end of the batch).
     - Untracked experiment outputs are packed into git bundles (`.bth/archive_bundles/<item_id>.bundle`) via `_build_untracked_bundle()` (`:379-410`) so `bth restore --item-id <id>` (`restore_archived_item` at `:450-554`, which runs `git clone` on `.bundle`) works without regression!
     - Before overwriting the script with its `# bathos:archived` stub (`:294-296`), `archive_experiment_bundle` records `script_sha256` and the matched `run_ids` list into `ArchivedItemRecord` (`archived_items` ledger, `M3-5`).
   - **Documentation Files (`kind = "doc"`)**:
     - Archived into `<doc_store>/archive/<year>/<slug>.tar.zst` with an adjacent `<slug>.md` manifest sidecar (matching Praxia's `[categories.archive]` `manifest_pair = true` convention), and recorded in `archived_items`. `restore_archived_item` is extended to extract `.tar.zst` bundles when `bundle_path.endswith(".tar.zst")`.
   - **Single Atomic Git Commit**:
     - All stubbed scripts/sidecars, `.bth/ARCHIVE_INDEX.md`, and doc manifest changes across the batch are staged and committed in a **single atomic git commit**:
       `archive: sweep N obsolete items (proposal: <path>)`
     - Git notes are attached for all swept items.
4. **Restoration**:
   - Reuses the existing `restore_tool` (`src/bathos/mcp.py:1437`, CLI `bth restore`) and `mcp_restore_tool` (`src/bathos/mcp.py:3186`). No duplicate `archive_restore` tool is created (`M3-4`).

---

### 5.3 Praxia-Compatible Document Store Integration (D8/D9)

#### A. Configuration Resolution Chain
Where bathos resolves technical documentation (`src/bathos/doc_store.py`):
1. Explicit CLI/tool argument: `doc_store: str` (`--doc-store <path>`)
2. Environment variable: `BTH_DOC_STORE` (`"none"` or empty string disables external sharing)
3. Nearest `pyproject.toml`: `[tool.bathos] doc_store = ".praxia/docs"`
4. User config `${XDG_CONFIG_HOME:-~/.config}/bathos/config.toml`: `[docs] doc_store = "..."`
5. Default self-contained fallback: `<main_root>/.bth/docs` (classified as primary project data inside `.bth/`, like `.bth/archive_bundles/`).

#### B. Compatibility Invariant (E18)
When pointed at `.praxia/docs`:
- Uses Praxia's verified `default_registry.toml` vocabulary: `status_enum = ["draft", "active", "complete", "superseded", "archived"]` and optional frontmatter key `supersedes`.
- Uses Praxia's `[categories.archive]` convention: `.tar.zst` archive + paired `.md` manifest sidecar (`manifest_pair = true`).
- Bathos writes into `.praxia/docs` **ONLY** when explicitly configured via layers 1–4 (Anti-Goal A5).

---

### 5.4 Diagnostic Telemetry Rotation & Indexing (`B3-3`)

- **Target Directory**: `~/.bth/catalog/logs/events.<hostname>.<pid>.jsonl*` (`src/bathos/telemetry.py:271, 448-461`, resolved via `telemetry._get_default_log_dir()`).
- **Hard Invariant (`B3-3`)**: The authoritative project run-event log `<main_root>/.bth/log/events.*.jsonl` (and `~/.bth/log-mirror/`, tracked by byte-offset watermarks in `ingest_watermarks`, `src/bathos/index.py:77-85`) is **NEVER** rotated, compressed, truncated, or moved by telemetry rotation.
- **Thresholds**: Closed diagnostic telemetry files in `~/.bth/catalog/logs/` exceeding `max_size_mb = 10` MB or older than `max_age_days = 30` days (arbitrary initial engineering heuristics; configurable).
- **Rotation & Index**: Compressed via `zstandard` into `~/.bth/catalog/logs/archive/<year>/events_<timestamp>.jsonl.zst` and recorded in append-only DuckDB table `telemetry_index (archive_path TEXT PRIMARY KEY, start_time TEXT, end_time TEXT, event_count BIGINT, sha256 TEXT, rotated_at TEXT)`.

---

### 5.5 Backfill Sequencing for `asr` and `tev_design` (D9/D10, `OD-1`)

All backfill counts and migrations execute under tracked scripts + `.bth.toml` sidecars:

```
[Step 1: Defect Declaration & Side-by-Side Dry-Run (OD-1)]
 - Enable [instruments] use_user_registry = true in asr & tev_design .bth.toml
 - Declare AMINX-2026-001 (<=0.1.0a22, sample_s > 1), AMINX-2026-002 (a24->a25 estimand_change),
   AMINX-2026-003 (e5f40cb8b..d930bbad GIT range) in user-global instruments/aminx.toml
 - Declare ASR-LG-2026-001 (56286cf..d4a17b3 GIT range) in asr/.bth/instruments/asr.toml
 - Execute D2 version recovery dry-run -> Report side-by-side counts with
   hide_inferred = false vs hide_inferred = true (OD-1) -> Owner reviews & approves
   setting hide_inferred = true on AMINX-2026-001
       │
       ▼
[Step 2: Findings Seeding & Parity Verification]
 - Seed 43 claims from tev_design/scripts/audit/claims_ledger.toml into .bth/findings/
 - Seed 41 verdicts from asr/.praxia/verdicts/ into .bth/findings/
 - Verify 100% field & count parity -> Replace legacy hand ledgers with read-only pointers
       │
       ▼
[Step 3: Archive Sweep Triage]
 - Run bth archive-sweep --dry-run in asr and tev_design
 - Owner reviews candidate batches -> Execute approved archive sweeps
```

---

### 5.6 CLI & FastMCP Function Signatures (P3)

Reusing the existing `restore_tool` (`src/bathos/mcp.py:1437`) and `mcp_restore_tool` (`src/bathos/mcp.py:3186`) for restoration, and using top-level `cli_name="archive-sweep"` (matching `archive_artifact_tool` at `src/bathos/mcp.py:1345` so it does not collide with top-level `archive_tool` (`cli_name="archive"`) at `src/bathos/mcp.py:1310`), P3 adds `archive_sweep` and `telemetry_rotate`:

```python
# In src/bathos/mcp.py:

@cisternal.tool(registry="bathos-cli", name="archive_sweep", cli_name="archive-sweep")
def archive_sweep_tool(
    dry_run: bool = True,
    age_days: int = 90,
    proposal_path: str = "",
    apply: bool = False,
    doc_store: str = "",
    project_root: str = "",
    catalog_dir: str = "",
) -> dict: ...

@cisternal.tool(registry="bathos", name="archive_sweep")
@traced_tool
@require_write_token_when_applying
async def mcp_archive_sweep_tool(
    dry_run: bool = True,
    age_days: int = 90,
    proposal_path: str = "",
    apply: bool = False,
    doc_store: str = "",
    project_root: str = "",
    catalog_dir: str = "",
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token_when_applying
) -> dict: ...

@cisternal.tool(
    registry="bathos-cli", name="telemetry_rotate", cli_group="telemetry", cli_name="rotate"
)
def telemetry_rotate_tool(
    max_size_mb: int = 10,
    max_age_days: int = 30,
    dry_run: bool = False,
    catalog_dir: str = "",
) -> dict: ...

@cisternal.tool(registry="bathos", name="telemetry_rotate")
@traced_tool
@require_write_token
async def mcp_telemetry_rotate_tool(
    max_size_mb: int = 10,
    max_age_days: int = 30,
    dry_run: bool = False,
    catalog_dir: str = "",
    token: str = "",  # noqa: ARG001 — consumed by @require_write_token
) -> dict: ...
```

- **`expected=[...]` Registry Wiring (`M3-4`)**: Append `"archive_sweep"` and `"telemetry_rotate"` to both `wire(None, app, registry="bathos-cli", expected=[...])` in `src/bathos/cli_cyclopts.py:60-147` and `_WIRED = cisternal.wire(app, registry="bathos", expected=[...])` in `src/bathos/mcp.py:6145-6240`.

---

### 5.7 Refusal Codes & Resolution Hints (P3)

In `src/bathos/errors.py` (registered in `BathosErrorCode`, `RESOLUTION_HINTS`, and `EXCEPTION_TO_CODE`):
```python
ARCHIVE_SWEEP_UNAPPROVED = "archive_sweep_unapproved"
ARCHIVE_PROPOSAL_STALE = "archive_proposal_stale"
DOC_STORE_UNAVAILABLE = "doc_store_unavailable"
RESTORE_CONFLICT = "restore_conflict"
```

Resolution hints in `RESOLUTION_HINTS`:
- `ARCHIVE_SWEEP_UNAPPROVED`: *"Archive sweep requires an approved proposal file when --apply is set. Run with --dry-run first to generate a proposal."*
- `ARCHIVE_PROPOSAL_STALE`: *"The archive proposal references files or git states that have changed since proposal generation. Re-run --dry-run to regenerate."*
- `DOC_STORE_UNAVAILABLE`: *"The configured documentation store path is not accessible or writable. Check permissions or configuration."*
- `RESTORE_CONFLICT`: *"Restoring this item would overwrite an existing modified or untracked file. Move or rename the destination file first."*

---

### 5.8 Acceptance Criteria (P3)

- **AC-P3-01**: `bth archive-sweep --dry-run` detects scripts with `[status] stale = true`, scripts with 100% hidden runs, unreferenced scripts ($\ge 90$ days, 0 current findings), and docs citing only hidden runs, outputting a proposal TOML file without modifying workspace files.
- **AC-P3-02 (`M3-10`)**: Applying an approved sweep (`--apply --proposal <path>`) packs untracked experiment outputs into `.bth/archive_bundles/<id>.bundle`, packs archived docs into `<doc_store>/archive/<year>/<slug>.tar.zst` + `.md` manifest, stubs tracked files, commits all workspace changes in a **single atomic git commit**, and logs entries (including `script_sha256` and `run_ids`) in `archived_items`.
- **AC-P3-03 (`M3-10`)**: Restoring an archived experiment item or doc bundle via `bth restore --item-id <id>` (`restore_tool`) completely restores original file bytes and SHA256 checksums, and flips the item's latest status in `archived_items` to `"restored"`.
- **AC-P3-04**: Pointing bathos at `.praxia/docs` parses and updates Praxia frontmatter (`status_enum = ["draft", "active", "complete", "superseded", "archived"]`, `supersedes`) and writes `.tar.zst` + `.md` manifest pairs.
- **AC-P3-05 (`OD-1`)**: Backfill step 1 dry-run script reports side-by-side counts for `tev_design` and `asr` with `hide_inferred = false` vs `hide_inferred = true` (showing the 1,574 `nolock` vs 1,596 `lock` split in `tev_design`, the 36 runs with `metadata.aminx_version`, and the `<= 0.1.0a22` cohort) before enabling `hide_inferred = true`.
- **AC-P3-06**: Backfill step 2 ingests all 43 claims from `tev_design/scripts/audit/claims_ledger.toml` and all 41 verdicts from `asr/.praxia/verdicts/` into native `.bth/findings/*.toml` with 100% field parity.
- **AC-P3-07**: Ingested claims from `claims_ledger.toml` with `evidential_status = "refuted_or_superseded"` map to `status = "superseded"`.
- **AC-P3-08 (`B3-3`)**: `bth telemetry rotate` compresses closed files in `~/.bth/catalog/logs/events.*.jsonl*` exceeding 10 MB or 30 days into `~/.bth/catalog/logs/archive/<year>/events_<timestamp>.jsonl.zst` with entries logged in `telemetry_index`, and **never** touches `<main_root>/.bth/log/*.jsonl` runlog files.
- **AC-P3-09 (`M3-5`)**: Archiving a script causes its historical runs (matched via `archived_items.run_ids`, `script_sha256`, or `paths`) to resolve to `state = "archived"` (`is_hidden = True`), and restoring the script via `bth restore` returns those runs to `state = "current"`.
- **AC-P3-10**: Sweep dry-run flags docs citing exclusively voided or archived runs.
- **AC-P3-11**: Bathos refuses to write into `.praxia/docs` when `doc_store` configuration is unset or `"none"`.
- **AC-P3-12**: Backfill scripts are executed under tracked `.bth.toml` sidecars in accordance with experimental rigor rules.

---

### 5.9 Test Plan & Risks (P3)

#### A. P3 Test Plan (`tests/test_archive_p3.py`)
Executed via `uv run pytest tests/test_archive_p3.py tests/test_errors.py`:
1. **Unit — Telemetry Rotation Isolation (`test_telemetry_rotation_never_touches_runlog`)**:
   - Create both `<project>/.bth/log/events.001.jsonl` (12 MB) and `<catalog_dir>/logs/events.host.123.jsonl` (12 MB). Run `telemetry_rotate_tool(max_size_mb=10)`. Assert `<project>/.bth/log/events.001.jsonl` is 100% untouched (same inode, bytes, and `ingest_watermarks` offsets), while `<catalog_dir>/logs/events.host.123.jsonl` is compressed to `.jsonl.zst` and indexed in `telemetry_index`.
2. **Integration — Batch Archive Sweep & `bth restore` Round-Trip (`test_archive_sweep_and_restore_roundtrip`)**:
   - Create a synthetic project git repo with 3 stale/void scripts, untracked outputs, and a doc citing void runs.
   - Run `archive_sweep_tool(dry_run=True)` $\rightarrow$ inspect proposal TOML $\rightarrow$ run `archive_sweep_tool(apply=True, proposal_path=...)`.
   - Assert exactly 1 new git commit was created, historical runs of the archived scripts resolve to `state = "archived"`, and calling `restore_tool(item_id=...)` restores exact bytes/SHAs and returns runs to `state = "current"`.
3. **Unit — Doc Store Resolution & Praxia Frontmatter (`test_doc_store_praxia_compat`)**:
   - Test all 5 configuration layers, verify Anti-Goal A5 (no writes to `.praxia/docs` unless configured), and verify `.tar.zst` + `.md` manifest creation.
4. **Unit — Backfill Importers (`test_backfill_claims_and_verdicts_importers`)**:
   - Feed synthetic fixtures mirroring `claims_ledger.toml` and `.praxia/verdicts/*.toml` into the backfill migration functions; verify 100% field parity and `hide_inferred` dry-run table generation.

#### B. P3 Risks & Mitigations
| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| **R-P3-1**: Rotating `.bth/log/*.jsonl` by mistake would corrupt `ingest_watermarks` and lose run history. | Eliminated | Critical | `telemetry_rotate` is hard-coded to operate only on `telemetry._get_default_log_dir()` (`~/.bth/catalog/logs/`) and explicitly rejects any path under `.bth/log/`. Tested by `test_telemetry_rotation_never_touches_runlog`. |
| **R-P3-2**: Workspace changes between `--dry-run` proposal generation and `--apply` cause partial or conflicting git commits. | Medium | Medium | `archive_sweep --apply` verifies pre-archive file SHA256s and `git status --porcelain` before mutating any file, refusing with `ARCHIVE_PROPOSAL_STALE` on mismatch. |

---

### 5.10 Fixer Task Decomposition (P3)

1. **Task P3.1**: Update `src/bathos/artifact_archive.py` (`archive_experiment_bundle` `commit: bool = True` batch mode, recording `script_sha256` and `run_ids` in `ArchivedItemRecord`, and `.tar.zst` support in `restore_archived_item`). Author `src/bathos/archive_sweep.py` implementing candidate detection, proposal generation, and single-commit batch application.
2. **Task P3.2**: Author `src/bathos/doc_store.py` implementing the 5-layer resolution chain and Praxia `.praxia/docs` frontmatter + `.tar.zst` manifest pair support.
3. **Task P3.3**: Implement `rotate_telemetry_logs()` and `telemetry_index` in `src/bathos/telemetry.py` targeting `~/.bth/catalog/logs/events.*.jsonl*`.
4. **Task P3.4**: Author tracked backfill scripts (`scripts/experiments/backfill_validity_dry_run.py` + `.bth.toml` and `scripts/experiments/backfill_findings.py` + `.bth.toml`) for `tev_design` and `asr`.
5. **Task P3.5**: Wire `archive_sweep` and `telemetry_rotate` in `src/bathos/mcp.py` (with `@require_write_token_when_applying` and `@require_write_token` on the `registry="bathos"` wrappers) and add them to `expected` in `src/bathos/cli_cyclopts.py:60` and `src/bathos/mcp.py:6145`.
6. **Task P3.6**: Author and run `tests/test_archive_p3.py`.

---

## 6. Traceability, Error Taxonomy & Telemetry Summary

### 6.1 Consolidated Error Code Taxonomy

| Code Value | Enum Identifier | Primary Trigger Surface | Actionable Remediation Hint |
|---|---|---|---|
| `result_void` | `BathosErrorCode.RESULT_VOID` | `cite`, `claim`, `campaign` | The requested run was produced by a defective instrument version and is void. Query runs_current or use a replacement run. Pass --include-superseded to inspect history. |
| `result_affected` | `BathosErrorCode.RESULT_AFFECTED` | `cite`, `claim`, `campaign` | The requested run is implicated by an active blast-radius ledger entry. Re-run or clear the flag with audit justification via `bth blast-radius clear`. |
| `result_superseded` | `BathosErrorCode.RESULT_SUPERSEDED` | `cite`, `claim`, `campaign` | The requested run was superseded by a newer run. Use --include-superseded to inspect, or query the successor run. |
| `result_archived` | `BathosErrorCode.RESULT_ARCHIVED` | `cite`, `claim`, `campaign` | The requested script or outputs were archived. Use `bth restore --item-id <id>` to restore files to workspace. |
| `result_hidden` | `BathosErrorCode.RESULT_HIDDEN` | `get_run`, `show`, `verify_run_drawable` | The requested run is hidden by default due to void, affected, superseded, or archived status. Pass --include-superseded to inspect. |
| `result_suspect_unacknowledged` | `BathosErrorCode.RESULT_SUSPECT_UNACKNOWLEDGED` | `cite`, `claim`, `campaign`, `graduate` | Run was produced by an unverified toolchain matching a defect range or lacks a required predicate field. To proceed, pass --ack-suspect '<rationale>' (ack_suspect='<rationale>'). |
| `result_unverifiable_unacknowledged` | `BathosErrorCode.RESULT_UNVERIFIABLE_UNACKNOWLEDGED` | `cite`, `claim`, `campaign`, `graduate` | Run provenance cannot be verified against repository lockfiles. To proceed, pass --ack-unverifiable '<rationale>' (ack_unverifiable='<rationale>'). |
| `estimand_boundary_mixed` | `BathosErrorCode.ESTIMAND_BOUNDARY_MIXED` | `campaign conclude`, `claim register`, `findings create` | Attempted to combine runs across an estimand change boundary at a confirmatory gate. Results compute different quantities; separate by epoch or pass --allow-mixed-estimand '<rationale>'. |
| `finding_reopen_reason_required` | `BathosErrorCode.FINDING_REOPEN_REASON_REQUIRED` | `findings create`, `new-experiment`, `run` | The question is already settled. Provide --reopen-reason explaining why prior confirmatory evidence is being challenged. |
| `finding_settled_evidence_insufficient` | `BathosErrorCode.FINDING_SETTLED_EVIDENCE_INSUFFICIENT` | `findings create` | Settling a finding requires at least one active confirmatory run. Elevate supporting evidence before setting status to settled. |
| `finding_not_found` | `BathosErrorCode.FINDING_NOT_FOUND` | `findings get` | The specified finding slug or ID was not found in .bth/findings/. Query finding_list to inspect available findings. |
| `superseded_target_not_found` | `BathosErrorCode.SUPERSEDED_TARGET_NOT_FOUND` | `supersede add` | The target entity specified in supersedes does not exist. Verify the predecessor ID or slug. |
| `spike_tier_cannot_settle` | `BathosErrorCode.SPIKE_TIER_CANNOT_SETTLE` | `findings create` | Spike runs are preliminary probes and cannot support a settled finding. Elevate evidence via a confirmatory run. |
| `archive_sweep_unapproved` | `BathosErrorCode.ARCHIVE_SWEEP_UNAPPROVED` | `archive sweep` | Archive sweep requires an approved proposal file when --apply is set. Run with --dry-run first to generate a proposal. |
| `archive_proposal_stale` | `BathosErrorCode.ARCHIVE_PROPOSAL_STALE` | `archive sweep` | The archive proposal references files or git states that have changed since proposal generation. Re-run --dry-run to regenerate. |
| `doc_store_unavailable` | `BathosErrorCode.DOC_STORE_UNAVAILABLE` | `doc_store` | The configured documentation store path is not accessible or writable. Check permissions or configuration. |
| `restore_conflict` | `BathosErrorCode.RESTORE_CONFLICT` | `restore` | Restoring this item would overwrite an existing modified or untracked file. Move or rename the destination file first. |

---

### 6.2 Diagnostic Telemetry Event Schema (`~/.bth/catalog/logs/events.<host>.<pid>.jsonl`)

Emitted via `bathos.telemetry.event()` (never to `.bth/log/*.jsonl`):
```json
{
  "event": "validity.resolved",
  "run_ids_count": 50,
  "hidden_count": 12,
  "void_count": 8,
  "affected_count": 2,
  "superseded_count": 1,
  "archived_count": 1,
  "suspect_count": 3,
  "unverifiable_count": 0,
  "cache_hit": true,
  "duration_ms": 4
}
```
