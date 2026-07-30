# Sprint 260601_v061: bathos v0.6.1 — Gate taxonomy cleanup + sprint-audit threshold hardening

## Rationale

v0.6 shipped the full agentic science surface (440 tests passing) but left three correctness gaps: a 1-line validate.py bug already fixed in tree; a structural inconsistency in the GateErrorCode taxonomy where ADVERSARIAL_CHECK_MISSING raises an unhandled exception while all other denials return GateResult, 5 dead codes, and the post-execution gate phase is completely unwired; and sprint-audit anomaly thresholds with no documented rationale and zero boundary test coverage for 4 of 7 signals. S3 is an independent infra item wiring the NLM PostToolUse hook that was explicitly flagged in the v0.6 CLAUDE.md as needing manual wiring. POPPER e-value and 11b-11d gate wiring are deferred: POPPER has no spec and six open design questions; 11b-11d live in the praxia repo and are blocked on an API design decision.

## Items (DAG order)

### S0. Commit validate.py nested-table guard

- **Agent:** fixer
- **Complexity:** trivial
- **Depends on:** (none)
- **Exit criteria:** git log shows the fix committed with message `fix(validate): skip nested result_schema tables in DuckDB column builder`; existing validate tests pass; no new test needed (the guard is exercised by any sidecar with `[result_schema.provenance]`).

One line already in tree at `src/bathos/validate.py:92` — `if isinstance(v, str)` guard in `validate_sidecar()` DuckDB column-builder. Without it, passing a non-string nested TOML table value to `_map_type_to_sql()` raises `TypeError` for any sidecar using `[result_schema.provenance]`. Commit as S0 before any other dispatch.

---

### S1. GateErrorCode taxonomy cleanup — normalize exception path, broaden adversarial enforcement, mark dead codes, wire post-execution stub

- **Agent:** fixer
- **Complexity:** small
- **Depends on:** S0
- **Exit criteria:**
  - (a) `ADVERSARIAL_CHECK_MISSING` returns `GateResult(ok=False)` instead of raising `GateError`, so `runner.py` never receives an unhandled exception from the gate path.
  - (b) `adversarial_check` enforcement fires for all non-residual outcome labels, not only `'pass'`.
  - (c) Dead codes `SIDECAR_HASH_MISMATCH`, `MANIFEST_WRITE_FAILED`, `HYPOTHESIS_LOCK_MISSING`, `RESULT_SCHEMA_MISMATCH`, `OUTCOME_AMBIGUOUS` are annotated with `# reserved` comments explaining which future feature will wire them.
  - (d) `runner.py` outcome-eval except block emits a `GateErrorPayload` with `code=OUTCOME_EVALUATION_ERROR` `phase=post_execution` and serializes it into `outcome_error_reason`.
  - (e) `test_gate_error_taxonomy.py` and `test_prereg.py` remain green; test count does not decrease.

Four concrete changes:
1. `prereg.py` line 241 — replace `raise GateError(...)` with `return GateResult(ok=False, error_payload=payload)`.
2. `prereg.py` line 231 — remove `label == 'pass'` filter so all non-residual branches are checked for `adversarial_check`.
3. Add `# reserved` comments on five dead `GateErrorCode` values pointing to their future wiring points.
4. In `runner.py` outcome-eval except block, call `_gate_failure_payload(OUTCOME_EVALUATION_ERROR, phase='post_execution')` and store JSON in `outcome_error_reason` field.

Key files: `src/bathos/prereg.py`, `src/bathos/runner.py`, `tests/test_gate_error_taxonomy.py`, `tests/test_prereg.py`.

---

### S2. Sprint-audit threshold boundary tests — ADR then synthetic boundary coverage

- **Agent:** spec-writer then test-writer
- **Complexity:** medium
- **Depends on:** S0
- **Exit criteria:**
  - (a) ADR at `.praxia/docs/decisions/260601_sprint-audit-threshold-rationale.md` documents domain rationale for all 7 signal thresholds and resolves `schema_overflow_rate` semantics (does normal experiment metadata count as overflow?).
  - (b) `sprint_audit.py` threshold lines each have a rationale comment.
  - (c) `test_sprint_audit_signals.py` has 14 new synthetic boundary tests (2 per signal at the boundary ± epsilon):
    - `error_rate` at 9% / 11%
    - `bypass_explicit` at 29% / 31%
    - `bypass_in_agent_mode` at 4% / 6%
    - `outcome_entropy` at 0.50 / 0.49 nats
    - `unfired_branches` at 39% / 41%
    - `schema_overflow_rate` at 19% / 21%
    - `post_hoc_bias_flag` True case with first-third worst-label count crossing 10%
  - (d) All 14 new tests pass.
  - (e) Total test count increases by at least 14.

Two-phase dispatch: Phase A is spec-writer producing the ADR (exit gate before Phase B). Phase B is test-writer writing boundary tests using the ADR as input.

`schema_overflow_rate` ambiguity: `sprint_audit.py` line 235 counts any run with non-empty metadata dict as overflow — this conflates deliberate experiment metadata with actual schema violations; the ADR must resolve whether this is intentional.

Synthetic test construction: build minimal dict rows with only the columns needed for each signal; call signal-computation paths directly rather than the full `sprint_audit()` function to avoid needing a live DuckDB catalog.

`post_hoc_bias_flag` True case: 12-run sequence with 2 `'fail'` outcomes in first 4 runs (first third) = 2/12 = 16.7% > 10% threshold.

Key files: `src/bathos/sprint_audit.py`, `tests/test_sprint_audit_signals.py`, `.praxia/docs/decisions/260601_sprint-audit-threshold-rationale.md`.

---

### S3. NLM PostToolUse hook wiring in ~/.claude/settings.json

- **Agent:** fixer
- **Complexity:** trivial
- **Depends on:** (none)
- **Exit criteria:** `~/.claude/settings.json` PostToolUse array contains entry with matcher `mcp__notebooklm__` pointing to `/home/marielle/projects/praxia/scripts/posttool-nlm.sh`; existing PostToolUse entries are unmodified; jq query confirms insertion.

Insert:
```json
{
  "matcher": "mcp__notebooklm__",
  "hooks": [{"type": "command", "command": "/home/marielle/projects/praxia/scripts/posttool-nlm.sh"}]
}
```
into the `hooks.PostToolUse` array in `~/.claude/settings.json`. This covers the direct NotebookLM MCP (`mcp__notebooklm__*`). The praxia wrapper tools (`mcp__praxia__nlm_*`) require separate matchers to be added in the NLM async-by-default sprint (`260526_nlm-async-by-default-design.md`) — the two sets of matchers are complementary.

Key files: `~/.claude/settings.json`, `/home/marielle/projects/praxia/scripts/posttool-nlm.sh`.

---

## Deferred

- **11b-11d sprint-composer pre-registration gate wiring (praxia):** Lives in the praxia Rust repo, not bathos scope; blocked on unresolved API design question — `compose_sprint()` is a pure in-memory function and threading gate logic in requires either breaking the pure-function contract (inject DB pool) or pre-flight checks in the MCP layer; `PcwError` lacks a `GateErrorPayload` variant. Schedule as dedicated praxia sprint after the design decision is recorded in an ADR.

- **POPPER e-value multi-run campaign primitive:** No spec exists; six design questions remain open (null model representation, e-value formula per run, mixed-outcome handling, error-run factor=1 confirmation, schema migration for campaigns table, `[popper]` placement as sidecar section vs campaign-level config, interaction with `adversarial_check`). Estimated complexity once designed is large (new accumulation module + schema migration + CLI surface + sprint-audit signal). Schedule as v0.7 candidate after a dedicated design session produces a reviewed spec.

## Risks

**S1 risk:** Broadening `adversarial_check` enforcement from `label=='pass'` only to all non-residual outcome labels may break existing sidecars that have non-pass outcomes without `adversarial_check` set. The fixer must verify that autonomous-mode tests using non-pass outcomes either (a) have `adversarial_check` set, or (b) are in collaborative mode (enforcement only fires in autonomous). If existing tests break, the fix is to add `adversarial_check` to the relevant test sidecars rather than narrowing the enforcement back.

**S2 risk:** The `schema_overflow_rate` semantic decision in the ADR may require a code change to `sprint_audit.py` line 235 before the boundary tests can be written — if normal metadata should NOT count as overflow, the signal computation changes and previously-passing tests may need updating. Phase A ADR must be reviewed before Phase B begins to avoid rework.
