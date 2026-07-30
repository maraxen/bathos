# bathos v0.6 Agentic Science Evolution: Implementation Spec

**Date:** 2026-05-26  
**Task ID:** 260526_agentic-science-evolution  
**Source synthesis:** `.praxia/docs/research/260526_agentic-science-nlm-synthesis.md`  
**Prior design context:** `.praxia/docs/specs/260520_agentic-science-design.md`  
**Version:** v0.6.0-draft

---

## 1. Executive Summary

This spec operationalizes the oracle-approved NLM synthesis into 12 implementation items across bathos (Python) and praxia (Rust), sequenced over 5 phases targeting approximately three weeks of solo-researcher sprint work. The central thesis: bathos already commits the sidecar hash before subprocess execution (`runner.py:194, 203`) but three systematic gaps defeat that commitment in practice — exception swallowing masks gate failures, no human-readable manifest exists at commitment time, and condition evaluation fires regardless of exit code.

Phase 2 closes all three gaps in a single coherent diff. Phases 3–4 layer adversarial checks, structured error taxonomy, and sprint-audit signals on that foundation. Phase 5 extends enforcement into the praxia orchestration layer. Maraxiom §3 is retracted; its citation and provenance capabilities fold into bathos as `bth cite` and `bth lineage --format prov`.

This is a personal-tool sprint: no semver migration ceremony, no warn-then-enforce gradient for the human-facing surface, no release notes. Design coherence and integrity guarantees are the optimization targets.

---

## 2. In-Scope / Out-of-Scope

**In scope (12 items, 2 repos):**

- bathos: exception-swallowing remediation, pre-execution manifest (self-signed), `outcome="error"` first-class, structured MCP error taxonomy, sprint-audit signal extension (5→7+ signals, bypass split), `adversarial_check` field + lint + agent-mode enforcement, `bth cite <run_id>`, `bth lineage --format prov`
- praxia (Rust): PostToolUse NLM hook, structured gate error taxonomy, sprint-composer pre-registration gate node (locate-then-scope), loop step budget enforcement

**Out of scope:**
- Maraxiom (any capability — §3 retracted; see D1)
- External TSA / OSF pre-registration API (v0.7+)
- POPPER e-value multi-run campaign primitive (exploratory, no design yet)
- Tournament-elo hypothesis quality signal (Q5 from synthesis — not addressed by reviewed literature)
- Cross-researcher collaboration, public sharing, UI dashboard, execution orchestration

---

## 3. Locked Decisions

**D1 — Maraxiom §3 retracted.** Maraxiom is a Bun/TypeScript + Python protein-presentation platform, never a citation/PROV host. `bth cite` and `bth lineage --format prov` fold into bathos directly.

**D2 (Q1) — Manifest hash granularity: full sidecar.** The manifest hashes the full sidecar (hypothesis + outcomes + adversarial_check + result_schema). Outcome refinement mid-experiment requires a REVISION via `--derived-from`, not a silent sidecar update. Partial hash allows retroactive outcome tuning while appearing pre-registered.

**D3 (Q2) — `adversarial_check` policy: required for `--agent-mode`, warn-only for human runs.** `bth lint` emits WARNING if missing on human runs. `--agent-mode` runs are blocked by `prereg.gate_check` if any `outcomes.pass` block lacks `adversarial_check`. Agent pipelines have zero incremental cost for producing falsification conditions; absence is a signal, not an oversight.

**D4 (Q3) — Bypass rate: two metrics separately.** `bypass_explicit` = `--no-sidecar` count / total. `bypass_in_agent_mode` = missing-sidecar runs in `--agent-mode` / agent-mode-total. Conflating deliberate bypass with unexpected agentic bypass hides the more dangerous pattern.

**D5 (Q4) — Nonrepudiation: self-signed manifest only for v0.6.** Manifest contains `sidecar_sha256`, `git_sha`, `script_sha256`, `agent_id` (nullable), `written_at` ISO timestamp. No external TSA. Solo researcher has no independent key holder; self-signed manifest provides tamper-evidence without author-key separation.

**D6 — Personal-tools recalibration: exception-swallowing remediation is P1.** The brief cited 7 swallow sites; code inspection found 10–12 (see §4.1). The integrity-critical subset defeats the sidecar hash gate at the root; bundle the fix with manifest work in Phase 2.

---

## 4. Per-Item Spec

### Item 1: Exception-Swallowing Remediation

**Goal:** Replace integrity-defeating bare `except Exception: pass` / silent-continue patterns with explicit error surfaces on data-integrity paths. Degraded-mode fallbacks on config reads and cleanup are acceptable; data-integrity paths are not.

**Anchored targets (current state → delta):**

| File:line | Current behavior | Required change |
|---|---|---|
| `sidecar.py:160–161` | `except Exception: continue` inside `evaluate_outcome` SQL loop | Raise `SidecarError`; let caller in `runner.py` map to `outcome="error"` + `outcome_error_reason="outcome_evaluation_error: <message>"` |
| `prereg.py:91–92` | `except Exception: pass` in warm-DB gate check fallback | Log + raise `GateError`; do not silently pass to cool-tier scan |
| `compact.py:287–288` | `except Exception: pass` in postmortem parse loop | Log warning + skip with explicit message; not silent |
| `runner.py:117–118` | `except Exception: pass` in script sha256 computation | Log warning; `sha256=""` is acceptable, silence is not |
| `compact.py:44–45` | `except Exception: sha256_hash = None` in output-file hash | Acceptable (large-file guard exists at line 35); add comment |
| `config.py:63–64` | `except Exception: pass` in registry write | Log warning; behavior (best-effort) unchanged |
| `mcp.py:62–63` | `except Exception: pass` in project slug lookup | Log warning; return `"default"` explicitly |
| `query.py:440–441` | `except Exception: return []` in lineage query | Raise `CatalogError`; callers must handle explicitly |
| `runner.py:137–143`, `146–152` | Config-read swallows for agent_mode resolution | Log warning; continue with default (degraded mode is acceptable) |

*Recon anchor drift: brief cited 7 sites in config.py, compact.py, sidecar.py, prereg.py. Code inspection finds 10–12 including runner.py config reads and query.py lineage. The brief was a conservative count of the worst-severity sites. All integrity-critical sites are addressed above.*

**Schema changes:** See Item 3 for `outcome_error_reason` field.

**Fixer task decomposition:**
1. `atom-1a`: Add `SidecarError` raise at `sidecar.py:160`; update `runner.py:226–232` caller to catch and map to `outcome="error"` with `outcome_error_reason` prefix `"outcome_evaluation_error: <message>"`
2. `atom-1b`: Replace `prereg.py:91–92` swallow with logged `GateError`; define `GateError` exception in `prereg.py` with initial signature `(message: str)`
3. `atom-1c`: Replace `compact.py:287–288` postmortem swallow with explicit warning + skip
4. `atom-1d`: Add warning logs to `runner.py:117`, `config.py:63`, `mcp.py:62` — keep fallback behavior, remove silence
5. `atom-1e`: Replace `query.py:440–441` swallow with `CatalogError`; update `bth lineage` CLI handler

**Verification gate:** `uv run pytest -k "swallow or exception or gate or outcome_error"` passes. Manual: introduce malformed DuckDB SQL in a sidecar outcome condition; `bth run` must surface an error with `outcome="error"` and `outcome_error_reason="outcome_evaluation_error: ..."`, not silently return `outcome="unknown"`.

**Evolution note:** `GateError` exception is initially defined in atom-1b with simple signature `GateError(message: str)`. Later, atom-4b extends the signature to support `GateError(payload: GateErrorPayload)` for structured error codes. Atom-1b uses the simple signature; atom-4b adds the overloaded constructor. Both must coexist for backward compat during Phase 2 → Phase 3 transition.

**Risk:** Callers of `evaluate_outcome` that accept `"unknown"` may now receive exceptions. Audit all callers before shipping atom-1a; update tests first. Rollback: revert atom-1a independently (atoms are ordered by impact, not by dependency).

---

### Item 2: Pre-Execution Manifest

**Goal:** Write a human-readable `.bth.lock.toml` manifest file between `write_run` (line 203) and `subprocess.run` (line 215), providing auditor-visible proof that hypothesis was recorded before execution. Under `--agent-mode`, absent manifest blocks the run.

**Anchored targets:**

Insert `_write_manifest()` call at `runner.py:204` (between `write_run(run, catalog_dir)` at line 203 and temp path setup at line 206):

Manifest filename is **per-run, immutable**: `<script-stem>.<run_id>.bth.lock.toml` (e.g., `fit_nvt.a1b2c3d4-e5f6-4g7h-8i9j-0k1l2m3n4o5p.bth.lock.toml`). This ensures each execution has a distinct manifest record, which aligns with the synthesis requirement for per-execution commitment.

Example:
```toml
# fit_nvt.a1b2c3d4-e5f6-4g7h-8i9j-0k1l2m3n4o5p.bth.lock.toml — written at run time, never modified after this point
[manifest]
written_at = "2026-05-26T14:32:11Z"    # ISO 8601 UTC
sidecar_sha256 = "sha256:<hex>"
sidecar_path = "/absolute/path/fit_nvt.bth.toml"
git_sha = "<hex>"
script_sha256 = "<hex>"
run_id = "a1b2c3d4-e5f6-4g7h-8i9j-0k1l2m3n4o5p"
agent_id = null                         # populated if --agent-mode
```

Manifest lives adjacent to the sidecar in the same directory. Add to `.gitignore` template in `bth init`.

**Self-signed manifest semantics (D5):** "Self-signed" in the v0.6 context means content-hash + git-commit-bound, NOT a separate cryptographic signing key. The manifest contains `sidecar_sha256`, `git_sha`, `script_sha256`, `agent_id`, and `written_at` timestamp. No external signature field or TSA needed for solo researcher; content hash + git commit provides tamper-evidence without author-key separation.

**Schema changes (v4 → v5, bundle with items 2, 3, 6):**

Add to `Run` dataclass (`schema.py:94`) and `COOL_SCHEMA` (all four columns added simultaneously in atom-2b, regardless of when Items 3 and 6 logic lands):
- `manifest_sha256: str` — sha256 of written manifest file; `""` if not written
- `manifest_path: str` — absolute path to manifest file; `""` if not written
- `outcome_error_reason: str` — structured reason for `outcome="error"` with prefix structure (e.g., `"exit_code=1"` or `"outcome_evaluation_error: ..."`) — empty otherwise
- `adversarial_check_status: str` — one of `"present"` / `"missing"` / `"n/a"` (non-experiment sidecars)

Add `_migrate_v4()` to `compact.py` setting all four fields to `""`.

**CLI surface:** `bth hypothesis lock <script>` — optional explicit pre-run command that writes the manifest without executing the script.

**Fixer task decomposition:**
1. `atom-2a`: Write `_write_manifest()` helper in `runner.py`; insert between lines 203–205; under `--agent-mode`, failure raises `GateError(GateErrorCode.MANIFEST_WRITE_FAILED)`
2. `atom-2b`: Add all four schema v5 columns to `schema.py` Run dataclass + COOL_SCHEMA (schema version bump to `"5"`): `manifest_sha256`, `manifest_path`, `outcome_error_reason`, `adversarial_check_status`. Atomicity: all four declared up-front, even if Items 3 and 6 logic lands later.
3. `atom-2c`: Add `_migrate_v4()` to `compact.py` with all four fields set to `""` / `"n/a"`
4. `atom-2d`: Add `bth hypothesis lock` CLI command in `cli.py`

**Verification gate:** Run `bth run scripts/experiments/<stem>.py`; verify `<stem>.bth.lock.toml` exists before subprocess process starts (check mtime). `bth migrate` on v4 catalog succeeds. `bth show <run_id>` displays `manifest_sha256`.

**Risk:** Manifest write failure in agent mode blocks run; this is the intended behavior. For human runs, write failure warns but continues. Rollback: `--no-manifest` flag (mirrors `--no-sidecar`).

---

### Item 3: `outcome="error"` First-Class

**Goal:** When subprocess exits nonzero (`exit_code != 0`), force `outcome="error"` and skip condition evaluation entirely. This is the PRIMARY integrity bug fix.

**Anchored targets:**

Current bug at `runner.py:225–232` — outcome evaluation block runs unconditionally after subprocess:

```python
outcome = ""
if sidecar is not None:
    try:
        meta = json.loads(metadata)
    except (json.JSONDecodeError, TypeError):
        meta = {}
    outcome = evaluate_outcome(sidecar, meta)
```

Required delta — insert exit_code guard before line 225:

```python
outcome = ""
if sidecar is not None:
    if exit_code != 0:
        outcome = "error"
    else:
        try:
            meta = json.loads(metadata)
        except (json.JSONDecodeError, TypeError):
            meta = {}
        outcome = evaluate_outcome(sidecar, meta)
```

*Anchor drift note: the brief locates "THE BUG" at line 232 (the `evaluate_outcome` call). The actual fix point is inserting a guard at line 225 — line 232 itself is correct; it simply must not be reached when exit_code != 0.*

**Schema changes:** `outcome_error_reason` is declared in schema v5 (atom-2b); atom-3b populates it.

**Fixer task decomposition:**
1. `atom-3a`: Insert exit_code guard at `runner.py:225`
2. `atom-3b`: Populate `outcome_error_reason` in `runner.py` with `f"exit_code={exit_code}"` for error outcomes; `""` otherwise (schema column declared in atom-2b, logic only)
3. `atom-3c`: Update `sprint_audit.py:100` to track error outcomes separately
4. `atom-3d`: Update `bth ls` / `bth show` output formatting to surface `outcome="error"` distinctly

**Verification gate:** Create a script that exits with `sys.exit(1)`. Run via `bth run`. Verify `bth show <run_id>` shows `outcome="error"` and `outcome_error_reason="exit_code=1"`. Verify `evaluate_outcome` is never called (add tracing log at function entry; confirm it does not appear in output).

**Risk:** Existing tests expecting `outcome="unknown"` on failed runs need updating — there should be few since failed runs previously fell through to "unknown" only when no result data was available. Low regression risk.

---

### Item 4: Structured MCP Error Taxonomy

**Goal:** Replace `_gate_failure_payload` freeform dict at `prereg.py:152–160` with a typed `GateErrorCode` enum and `GateErrorPayload` dataclass. All bathos MCP tools return structured error codes.

**Anchored targets:**

Current at `prereg.py:152–160`:
```python
def _gate_failure_payload(gate: str, errors: list[str], mode: str, script_path: Path) -> dict:
    return {"status": "gate_failure", "gate": gate, "errors": errors, ...}
```

Replacement:
```python
class GateErrorCode(str, Enum):
    SIDECAR_MISSING = "sidecar_missing"
    SIDECAR_INVALID = "sidecar_invalid"
    SIDECAR_HASH_MISMATCH = "sidecar_hash_mismatch"
    NOT_FIRST_OF_KIND = "not_first_of_kind"
    MANIFEST_WRITE_FAILED = "manifest_write_failed"
    ADVERSARIAL_CHECK_MISSING = "adversarial_check_missing"
    HYPOTHESIS_LOCK_MISSING = "hypothesis_lock_missing"
    OUTCOME_EVALUATION_ERROR = "outcome_evaluation_error"
    RESULT_SCHEMA_MISMATCH = "result_schema_mismatch"
    OUTCOME_AMBIGUOUS = "outcome_ambiguous"
    INTERNAL = "internal"  # catch-all for unmapped errors

@dataclass
class GateErrorPayload:
    error_code: GateErrorCode
    phase: Literal["pre_execution", "post_execution"]  # disambiguates execution timing
    taxonomy_label: str
    errors: list[str]
    agent_mode: str
    resolution_hint: str
    gate_schema_version: int = 2
```

**Version transition (v1 → v2):** Existing `_gate_failure_payload` at `prereg.py:159` emits version 1 (freeform dict). Version 2 replaces this with typed enum + dataclass. Since only MCP client is in-repo (Claude Code), no backward-compatibility shim needed; document as breaking change in D4 ADR.

**Fixer task decomposition:**
1. `atom-4a`: Add `GateErrorCode` enum + `GateErrorPayload` dataclass to `prereg.py`; define `phase` disambiguates pre-execution (gate check at entry) vs. post-execution (runner-time outcome evaluation)
2. `atom-4b`: Replace `_gate_failure_payload` freeform dict with `GateErrorPayload`; update `GateResult.error_payload` type annotation; note that `OUTCOME_EVALUATION_ERROR` is post-execution (runner), `OUTCOME_AMBIGUOUS` is pre-execution (gate)
3. `atom-4c`: Update all MCP error-returning tools in `mcp.py` to serialize `GateErrorPayload` with correct `phase` field
4. `atom-4d`: Add static `resolution_hint` per error code

**Verification gate:** `bth run` a script missing its sidecar in agent mode. MCP tool JSON response contains `"error_code": "sidecar_missing"` with `"phase": "pre_execution"`. Manually modify a sidecar outcome condition to have invalid SQL and run in agent mode; response contains `"error_code": "outcome_evaluation_error"` with `"phase": "post_execution"`. Valid sidecar run: no `error_code` in response.

**Risk:** MCP clients pattern-matching on `"gate_failure"` string will break. Since the only MCP client is Claude Code in the same repo, risk is low.

---

### Item 5: Sprint-Audit Signals Extension

**Goal:** Extend `sprint_audit.py:96–127` from 3 signals to 7+ without refactoring the existing scaffolding structure.

**Anchored targets (`sprint_audit.py:96–127`):**

Current three signals: `unknown_count`, `bypassed_count`, `residual_count` with hardcoded 10% threshold.

Add (extending the existing `anomalies` list and adding a structured `signals: dict[str, float|bool]`):

| Signal | Formula | Flag condition | Literature basis |
|---|---|---|---|
| `error_rate` | count(outcome="error") / total | > 0.10 | internal |
| `bypass_explicit` | `--no-sidecar` count / total | > 0.30 | arXiv 2509.08713 |
| `bypass_in_agent_mode` | bypassed in agent-mode / agent-mode-total | > 0.05 | internal |
| `outcome_entropy` | Shannon entropy of outcome label distribution (nats) | < 0.5 nats | arXiv 2501.10421 |
| `unfired_branches` | fraction of distinct outcome labels never selected | > 0.40 | arXiv 2509.08713 |
| `schema_overflow_rate` | runs with extra metadata keys / total | > 0.20 | arXiv 2510.21652 |
| `post_hoc_bias_flag` | worst-outcome selected > 10% of ordered trials | True | arXiv 2510.21652 (χ²(4,200)=61.99, p<10⁻¹⁰)[^m6] |

[^m6]: AstaBench χ² statistic is derived from synthesis §1.4 analysis; not a primary literature source. Threshold (worst-outcome > 10% of ordered trials) is grounded; the χ² metric is illustrative of the signal strength observed in the synthesis corpus.

Optional signal 8 (behind `--entropic-anchoring` flag): `flat_trace_with_erratic_transitions` — campaigns where outcome labels are uniform but per-run duration variance is high (proxy for pre-committed answer with surface exploration). Metric: `duration_cv = std(run_duration) / mean(run_duration) > 0.5` AND `outcome_entropy < 0.5 nats`. This is the "Entropic Anchoring" pattern from `.praxia/nlm.jsonl` Q-5 note; cleaner diagnostic than outcome entropy alone.

**All thresholds are CALIBRATION TARGETS, not hard gates in v0.6.** Emit as `[WARN]` annotations referencing affected run IDs and literature source.

**Fixer task decomposition:**
1. `atom-5a`: Add `error_count`, `bypass_explicit`, `bypass_in_agent_mode` to `sprint_audit.py:96–127` computation block
2. `atom-5b`: Add `outcome_entropy` + `unfired_branches` + `schema_overflow_rate` (requires parsing `metadata` JSON column)
3. `atom-5c`: Add `post_hoc_bias_flag` (sort outcomes by run `timestamp` within campaign; count worst-outcome selections)
4. `atom-5d`: Add `--entropic-anchoring` flag; compute `flat_trace_with_erratic_transitions` when enabled

**Verification gate:** Campaign with 10 runs all having `outcome="pass"`. `bth sprint-audit` reports `outcome_entropy < 0.5 [WARN]`. `bypass_explicit` and `bypass_in_agent_mode` are two separate values in output.

**Risk:** Entropy computation loads all outcome labels per campaign. For large campaigns (>1000 runs), add row count guard. `schema_overflow_rate` requires parsing the `metadata` JSON column for every run — expensive for large datasets; add `LIMIT` or time-window scoping.

---

### Item 6: `adversarial_check` Field + Lint + Agent-Mode Enforcement

**Goal:** Add `adversarial_check: str | None` to `OutcomeSpec`; validate in lint; block agent-mode runs missing it on pass conditions.

**Anchored targets:**

- `sidecar.py:22–27` (`OutcomeSpec` dataclass) — add field:
  ```python
  adversarial_check: str | None = None
  ```
- `sidecar.py:118–128` (`_parse_outcomes`) — parse `adversarial_check = spec.get("adversarial_check")` for each outcome
- `prereg.py:103–149` (`gate_check`) — add check: if `mode == "autonomous"` and any `outcomes.pass` block has `adversarial_check is None` → `GateErrorPayload(error_code=GateErrorCode.ADVERSARIAL_CHECK_MISSING, ...)`
- `linter.py` — add `check_adversarial_checks(project_root: Path) -> list[LintIssue]` function (wire into `lint_project` at `linter.py:82`):
  - (a) `adversarial_check` is valid DuckDB SQL (reject tautologies: deny-list heuristic rejects `AND 1=1`, `AND TRUE`, `AND <col> = <col>`)
  - (b) Syntactic heuristic: `adversarial_check` must reference at least one column NOT in `condition` (distinct-column preference; same-column threshold tightening is weak. Reference to different variable is stronger because synthesis intent is "a condition that would flip outcome if hypothesis wrong" — typically requires different observed variable). Emit WARNING (not ERROR) if all columns overlap with `condition`.
  - (c) Surface gameability in lint message itself: "this is a syntactic proxy; humans must verify the check actually strengthens the claim." Do not rely on heuristic alone.
  - WARNING severity for human runs; ERROR for agent-mode context

**Schema changes:** `adversarial_check_status: str` (one of `"present"` / `"missing"` / `"n/a"`) is declared in schema v5 (atom-2b); atom-6d populates it. No additional schema changes needed beyond v5.

**Sidecar format delta:**
```toml
[outcomes.pass]
condition = "temp_std < 5"
decision = "proceed to NPT validation"
reasoning = "..."
is_residual = false
adversarial_check = "temp_std < 5 AND n_steps >= 10000 AND dt_fs <= 0.5"
```

*Lint anchor note: `linter.py:82–127` is Tier-1 naming/sidecar-presence hooks. Tier-2 hooks (`check_residual_rates`, `check_bypass_trend`) are at lines 137–280. The adversarial_check hook is a new Tier-2 function added alongside them, not inserted into the 82–127 block.*

**Fixer task decomposition:**
1. `atom-6a`: Add `adversarial_check` field to `OutcomeSpec` dataclass + `_parse_outcomes`
2. `atom-6b`: Add gate check to `prereg.gate_check` for autonomous mode; use `GateErrorCode.ADVERSARIAL_CHECK_MISSING`
3. `atom-6c`: Add `check_adversarial_checks()` Tier-2 function to `linter.py`; wire into `lint_project`
4. `atom-6d`: Populate `adversarial_check_status` in runtime logic (schema column declared in atom-2b, logic only)

**Verification gate:** Sidecar with `adversarial_check` on all pass blocks → `bth lint` no warnings. Sidecar missing `adversarial_check` in `--agent-mode` → `GateErrorCode.ADVERSARIAL_CHECK_MISSING` returned. Same sidecar in human mode → WARNING not ERROR from `bth lint`.

**Risk:** Existing sidecars have no `adversarial_check`. Human runs: lint warns, no blocking — existing workflows unchanged. Agent-mode runs: enforcement is immediate on v0.6 deploy; write migration guide for existing agent-mode scripts.

---

### Item 7: `bth cite <run_id>`

**Goal:** Emit a structured citation for a run linking reported numbers to hypothesis hash, manifest hash, git SHA, and sidecar path.

**CLI surface:** `bth cite <run_id> [--format markdown|json]`

Default markdown output:
```
Run run_abc123 — fit_nvt experiment
  Hypothesis hash: sha256:deadbeef...
  Manifest hash:   sha256:cafebabe... (or: not recorded — pre-v0.6)
  Git SHA:         abc123
  Sidecar:         scripts/experiments/fit_nvt.bth.toml
  Outcome:         pass (temp_std=3.2)
  Timestamp:       2026-05-26T14:32:11Z
```

**Anchored targets:** New `cite.py` module; `bth cite` command in `cli.py`; `mcp__bathos__cite_run` tool in `mcp.py`.

**Fixer task decomposition:**
1. `atom-7a`: Add `cite.py` with `format_citation(run: Run, fmt: str) -> str`
2. `atom-7b`: Add `bth cite` command to `cli.py` (reads from warm DB via `query.get_run()`)
3. `atom-7c`: Add `mcp__bathos__cite_run` MCP tool to `mcp.py`

**Verification gate:** `bth cite <valid_run_id>` produces output containing `manifest_sha256`. Pre-v0.6 run (no manifest): displays "not recorded — pre-v0.6" instead of sha. `bth cite <invalid_run_id>` returns clear error.

**Risk:** If manifest was not written (runs before v0.6), `manifest_sha256 == ""` — display graceful fallback, not an empty string.

---

### Item 8: `bth lineage --format prov`

**Goal:** Extend existing `bth lineage <run_id>` with PROV-JSON output. Existing lineage via recursive CTE over `parent_run_id` is confirmed at `query.py:405–443`.

**CLI surface:** `bth lineage <run_id> [--format text|prov|dot]`

PROV-JSON uses W3C PROV-JSON 1.0 with `bth:` namespace for bathos-specific fields:
```json
{
  "entity": {
    "run_abc123": {"prov:type": "bth:Run", "bth:outcome": "pass", "bth:sidecar_sha256": "..."}
  },
  "wasDerivedFrom": {
    "_:d1": {"prov:generatedEntity": "run_xyz", "prov:usedEntity": "run_abc123"}
  }
}
```

**PROV mapping (atom-8a):**

| PROV Concept | Bathos Mapping | Notes |
|---|---|---|
| `prov:Entity` | `Run`, sidecar file, manifest file | Each run is an entity; outputs (sidecar, manifest) are entities linked to the run via `wasGeneratedBy` |
| `prov:Activity` | `subprocess.run` execution (identified by run_id) | The subprocess invocation itself; scoped by run_id for uniqueness |
| `prov:Agent` | `agent_id` (nullable, falls back to `git_sha`-derived author identity) | Identifies who/what triggered the run. In agent-mode, `agent_id` populated; otherwise null → derive from git author. |
| `prov:used` | sidecar entity → activity | The activity (subprocess.run) used the sidecar hypothesis as input |
| `prov:wasGeneratedBy` | run entity → activity | The run outcome was generated by the subprocess activity |
| `prov:wasDerivedFrom` | run entity → parent run entity | Tracks lineage: current run derived from parent run (if `parent_run_id` is set) |

**Anchored targets:** New `provenance.py` module; extend `bth lineage` CLI in `cli.py`; add `mcp__bathos__lineage_prov` tool in `mcp.py`.

**Fixer task decomposition:**
1. `atom-8a`: Add `provenance.py` with `format_prov_json(runs: list[Run]) -> dict`
2. `atom-8b`: Extend `bth lineage` CLI to accept `--format` flag; `text` remains default
3. `atom-8c`: Add `mcp__bathos__lineage_prov` MCP tool

**Verification gate:** `bth lineage <run_id> --format prov` produces valid JSON with `entity` and `wasDerivedFrom` keys. `bth lineage <run_id> --format text` still works (backward compat). Single-run lineage (no parent) produces valid PROV with single entity entry.

**Risk:** W3C PROV-JSON schema is permissive — use `bth:` namespace for all bathos fields to avoid conflicts with other PROV producers.

---

### Item 9: PostToolUse NLM Hook (praxia)

**Goal:** Log `mcp__notebooklm__*` tool calls to `nlm.jsonl` via Claude Code's PostToolUse hook mechanism.

**Anchored targets:**

Current: `/home/marielle/projects/praxia/.claude/settings.json` — `{"hooks": {}, "worktree": {"bgIsolation": "none"}}` (empty hooks confirmed).

Required delta:
```json
{
  "hooks": {
    "PostToolUse": [{
      "matcher": "mcp__notebooklm__",
      "hooks": [{"type": "command", "command": "/home/marielle/projects/praxia/scripts/posttool-nlm.sh"}]
    }]
  }
}
```

`posttool-nlm.sh`: appends `{"tool": "$TOOL_NAME", "input": ..., "ts": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"}` to `.praxia/nlm.jsonl`.

Note: `bathos/src/bathos/scripts/analysis/backfill_nlm_log.py` (267 lines) is the historical backfill script. There is an untracked `bathos/scripts/analysis/` directory at the repo root — these are distinct paths; the backfill script lives inside `src/bathos/scripts/`.

**Fixer task decomposition:**
1. `atom-9a`: Write `scripts/posttool-nlm.sh` hook script
2. `atom-9b`: Update `.claude/settings.json` with PostToolUse entry

**Verification gate:** Call any `mcp__notebooklm__*` tool from a Claude Code session in the praxia project. Verify `.praxia/nlm.jsonl` receives a new entry.

**Risk:** Hook script uses hardcoded absolute path — fails if repo moves. Use `$CLAUDE_PROJECT_DIR` or relative resolution if available in the hook execution context.

---

### Item 10: Structured Gate Error Taxonomy (praxia, Rust)

**Goal:** Define a `GateErrorCode` enum in `praxia-core` (Rust) mirroring the bathos Python enum. Praxia MCP gate-checking tools return typed error codes.

**Proposed enum:**
```rust
pub enum GateErrorCode {
    HypothesisLockMissing,
    SidecarHashMismatch,
    OutcomeEvaluationError,
    ResultSchemaViolation,
    AnomalyFlagTriggered,
    AdversarialCheckMissing,
    RevisionUnlogged,
}
```

**Anchored targets:** New `gate_error.rs` module in `praxia-core`. Sprint-composer pre-registration node (item 11) consumes this.

**Fixer task decomposition:**
1. `atom-10a`: Add `gate_error.rs` to `praxia-core/src/` with enum + `GateErrorPayload` struct
2. `atom-10b`: Update praxia MCP tool error returns to use typed codes
3. `atom-10c`: Add unit tests for error code serialization round-trip

**Verification gate:** `cargo nextest run -p praxia-core -E 'test(gate_error)'` passes. MCP tool error JSON contains `error_code` field with valid enum variant name.

**Risk:** Praxia crate boundaries may require placing the enum in `praxia-fsm` rather than `praxia-core` depending on where MCP tools are defined. Locate during implementation; the crate assignment is a fixer decision.

---

### Item 11: Locate Sprint-Composer Pre-Registration Gate (praxia, Rust)

**Goal:** Time-boxed (½-day) locate spike to find sprint-composer location and architecture in praxia codebase.

**Current state:** Sprint-composer not located under that name in praxia codebase as of recon. May live in `praxia-fsm` executor, `apps/`, or workflow YAMLs. 

**Output:** A written finding in `.praxia/docs/research/` containing:
- Component name and file path(s)
- Architecture diagram or pseudo-code for DAG structure
- Entry point for hypothesis_lock gate wiring
- OR explicit statement "Does not exist; design sprint required"

**Fixer task decomposition:**
1. `atom-11a`: Locate sprint-composer in praxia codebase. Search patterns: "sprint" in `.rs` files, workflow YAML schema in `apps/`, FSM task dispatch in `praxia-fsm`. Document findings in `.praxia/docs/research/260526_sprint-composer-locate.md`. If found: document component name, file path, DAG structure. If not found: "Does not exist; new design required."

**Verification gate:** `.praxia/docs/research/260526_sprint-composer-locate.md` exists and contains explicit location OR "not found" verdict.

**Scope decision gate:** If sprint-composer exists and is well-bounded, proceed to Phase 5 implementation (items 11b-11d). If not found or design required, defer implementation to v0.6.1 / separate design sprint.

---

### Item 12: Loop Step Budget Enforcement (praxia, Rust)

**Goal:** Enforce per-task step budget in praxia orchestrator to prevent self-referential verification loops.

*Note: DR-10 "LoopTrap" citation linkage — See synthesis §1.6 (unverification discussion) for the documented defensive behavior: self-referential verification gates ("verify the verification methodology") create irresolvable execution paths. The arXiv ID cited in synthesis ("ae193ddf") is unverified (UUID-shaped, not arXiv format). Cite the defensive behavior explicitly; do not cite external paper until ID is confirmed.*

**Proposed defaults (tunable per task type):**
- Science tasks: 20 steps
- Complex orchestration: 50 steps
- Budget exhausted: emit structured blocker with step log + decision tree; no recursive re-escalation
- Loop signature: same tool called > 3 times consecutively with no measurable state change

**Fixer task decomposition:**
1. `atom-12a`: Add step counter to FSM task state in `praxia-fsm`
2. `atom-12b`: Add `step_budget` config field per task type (science / orchestration / default)
3. `atom-12c`: Implement consecutive-same-tool loop signature detection
4. `atom-12d`: Add budget exhaustion handler: emit structured blocker, prevent recursive verification chains

**Verification gate:** Configure science task with `step_budget = 3`. Dispatch task requiring > 3 steps. Verify structured blocker emitted after step 3, not infinite retry.

**Risk:** Too-tight budgets block legitimate long-running tasks. Default 20/50 are conservative; expose as per-project config knob. Loop signature detection (3 consecutive same-tool calls) may fire on legitimate parallel patterns; tune with state-change detection.

---

## 5. Phase Plan

| Phase | Duration | Items | Dependencies |
|---|---|---|---|
| 0 | Hours | Worktree stabilization (clean `M report.html`, `?? .claude/`, `?? scripts/analysis/`) | None |
| 1 | 1 day | #9 (NLM hook) | None; independent |
| 2 | 3–4 days | #1 + #2 + #3 (exception-swallowing + manifest + outcome=error); schema v5 bump lands here | Phase 0 |
| 3 | ~1 week | Bathos: #4 + #6 | Phase 2 (schema v5 now available) |
| 3 | ~1 week | Praxia: #10 | None; parallel to bathos Phase 3 |
| 4 | ~1 week | #5 + #7 + #8 | Phase 2 (#5 needs outcome=error; #7/#8 need manifest fields) |
| 5 | ~3 days | #12 (loop step budget) | Phase 3 praxia (#10 provides error types); conditional on locate-pass (atom-11a) outcome |

**Dependency arrows:**
```
Phase 0 ──► Phase 2 ──► Phase 3 (bathos) ──► Phase 4
                   ╲
                    ╲──► Phase 3 (praxia) ──► Phase 5 (conditional)
Phase 1 (independent)
atom-11a (locate, independent) → Phase 5 gate decision
```

Phase 5 entry gate: if atom-11a locates sprint-composer and design fits Phase 5, implement 11b-11d alongside 12. Otherwise defer to v0.6.1.

---

## 6. ADR Candidates

The following locked decisions warrant standalone `.praxia/docs/decisions/` files:

| Slug | Decision |
|---|---|
| `260526_manifest-hash-granularity` | D2: full-sidecar hash (not hypothesis-only); rationale for requiring REVISION on outcome tuning |
| `260526_adversarial-check-policy` | D3: required for agent-mode, warn-only for human; rationale for asymmetric enforcement |
| `260526_bypass-rate-split` | D4: two-metric bypass reporting; rationale for not conflating deliberate vs. unexpected bypass |
| `260526_nonrepudiation-v06` | D5: self-signed manifest only; explicit deferral of external TSA to v0.7+ with rationale |

---

## 7. Risks and Unknowns

| Risk | Likelihood | Mitigation |
|---|---|---|
| Sprint-composer not found in praxia (atom-11a locate fails) → Phase 5 implementation deferred | Medium | Atom-11a is ½-day time-boxed locate pass (Phase 1 or independent). If not found, defer 11b-11d to v0.6.1 separate design sprint; Phase 5 contains only Item 12. |
| Schema v5 migration breaks existing v4 catalogs | Low | `_migrate_v4()` sets all four new fields to `""` / `"n/a"`; test on real catalog before merge. Partial rollback of Items 3 or 6 alone does NOT require schema downgrade (columns remain, default to empty); only rollback of Item 2 requires full v5 revert. |
| `atom-1a` (evaluate_outcome raises) breaks callers expecting `"unknown"` | Medium | Audit all callers of `evaluate_outcome` before implementation; update tests first |
| `adversarial_check` AND-clause heuristic produces false positives | Low | Heuristic is WARNING-only for human runs; document as "syntactic proxy, not soundness proof" in lint message |
| DR-10 LoopTrap arXiv ID unverified | Confirmed | Cite defensive behavior; do not cite paper until arXiv ID confirmed |
| PostToolUse hook not available in Claude Code version used in praxia | Low | Check Claude Code hook API docs before dispatch; fallback: manual nlm.jsonl append script |
| Three of seven sprint-audit signals may fire on legitimate calibrated workloads | Confirmed | `outcome_entropy`, `unfired_branches`, `schema_overflow_rate` are sensitive to domain-specific outcome distributions. All thresholds are CALIBRATION TARGETS in v0.6; all signals emit `[WARN]` only — never gate. Document signal noise floor and known v0.6 calibration debt prominently in output. |

---

## 8. Out-of-Scope / Deferred

### v0.6.0 (This Sprint)

- **Item 11b-11d (Sprint-Composer Pre-Registration Gate Node wiring):** Deferred pending locate-pass outcome (atom-11a). If sprint-composer exists and is well-bounded, move to Phase 5. If not found, defer to v0.6.1 separate design sprint.

### v0.6.x / v0.7+

- **Maraxiom §3** (citation + PROV integration into Maraxiom): retracted; capabilities fold into bathos
- **External TSA / OSF pre-registration API**: deferred to v0.7+; no independent key holder design available for solo researcher context
- **POPPER e-value multi-run campaign primitive**: exploratory; needs dedicated design session before backlogging
- **Tournament-elo hypothesis quality signal**: Q5 from synthesis — not addressed by reviewed literature
- **DR-10 LoopTrap specific citation**: unverified arXiv ID (`ae193ddf` is UUID-shaped); cite defensive behavior only
- **`bth hypothesis lock` as mandatory step** (vs. auto-written at run time): optional convenience; auto-write is primary
- **`bth amend` amendment log**: listed as P3 in prior design; not blocking v0.6; deferred

---

*Spec v0.6.0-draft. Source: oracle-approved synthesis `260526_agentic-science-nlm-synthesis.md`. Ready for oracle critique-revise cycle before sprint composition.*
