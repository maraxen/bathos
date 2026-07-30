# Sprint 260623: claim-tier descriptive labels UX (#2604)

## Rationale

`validate_claim()` already **errors** on opaque IDs (`/^[A-Z][0-9]+$/`) missing a `label` field (AC-03/AC-14 in `claim.py:147–168`). That blocks bad scaffolds at register/conclude time but does not help day-to-day readability: discriminability messages, Union Gate prints, sprint-audit Signal 12/13, and `claim_coverage_*.json` still surface raw IDs (`H_primary`, `C_1`). The scaffold template also seeds opaque IDs (`H_primary`, `H_null`, `C_1`) with placeholder labels.

This sprint closes the **human-facing gap** without changing claim schema: introduce a shared display helper, wire it through lint/conclude/coverage surfaces, and add a Tier-1 `bth lint` scan of `.bth/claims/*.toml` that mirrors the validate rule as an early WARNING.

## Scope boundary

| In scope | Out of scope |
|----------|--------------|
| `display_label(id, label)` helper + wiring | Renaming claim ID convention globally |
| Tier-1 lint for claim TOMLs under `.bth/claims/` | Breaking change to require snake_case IDs |
| Human-facing output uses label (fallback id) | New schema fields |
| Scaffold: descriptive ID examples + lint for placeholder labels | `bth lint --campaign` CLI flag (defer) |
| Tests + skill note | Assumptions block (only hypotheses/confounds in v1) |

## Items (DAG order)

### S0. Recon — audit label vs ID usage

- **Agent:** recon
- **Complexity:** trivial
- **Depends on:** (none)
- **Exit criteria:** Markdown table in sprint notes listing every human-facing call site that prints hypothesis/confound IDs; each row tagged `wire` or `ok`.

Grep targets: `claim.py` (validate messages, discriminability), `campaigns.py` (conclude prints, `emit_claim_coverage_report`), `sprint_audit.py` (Signals 12–13), `cli.py` (`claim validate` output).

---

### S1. `display_label()` helper in `claim.py`

- **Agent:** fixer
- **Complexity:** trivial
- **Depends on:** S0
- **Exit criteria:**
  - `display_label(entity: dict) -> str` returns non-blank `label` when present, else `id`
  - `format_hypothesis_ref(claim, hypothesis_id) -> str` resolves ID → `display_label` for cross-references in messages
  - Unit tests in `tests/test_claim.py` (3 cases: label present, label blank, unknown id)

---

### S2. Wire human-facing surfaces to `display_label`

- **Agent:** fixer
- **Complexity:** small
- **Depends on:** S1
- **Exit criteria:**
  - Discriminability validation errors use `format_hypothesis_ref` instead of raw `hypothesis_a` / `hypothesis_b`
  - `campaigns.py` conclude-gate parity prints use confound `display_label`
  - `emit_claim_coverage_report` JSON adds optional `clause_labels: {id: display_label}` map for union-gate clauses (backward-compatible additive field)
  - Existing claim tests green; ≥4 new assertions on message text containing label strings

Key files: `src/bathos/claim.py`, `src/bathos/campaigns.py`, `tests/test_claim.py`, `tests/test_t6_parity_conclude_gate.py`.

---

### S3. Tier-1 lint: `check_claim_opaque_labels(project_root)`

- **Agent:** fixer
- **Complexity:** small
- **Depends on:** S1
- **Exit criteria:**
  - New function in `linter.py` scans `workspace_root/.bth/claims/**/*.toml` (and `*.claim.toml`)
  - Reuses `_OPAQUE_ID_RE` logic (import from `claim` or duplicate with comment pointing to canonical regex)
  - Emits `LintIssue` WARNING for opaque hypothesis/confound IDs with missing/blank label
  - Emits WARNING when `label` matches placeholder pattern (`REQUIRED:` prefix or equals `id`)
  - Wired into `bth lint` via `lint_project` or post-pass in `cli.py`
  - ≥6 tests in `tests/test_linter.py` or `tests/test_claim_lint_labels.py`

**Note:** Do not duplicate AC-03/AC-14 as ERROR in lint — validate_claim remains the hard gate at register/conclude. Lint is early feedback only.

---

### S4. Scaffold + skill polish

- **Agent:** fixer
- **Complexity:** trivial
- **Depends on:** S1
- **Exit criteria:**
  - `scaffold_claim()` template uses descriptive IDs (`H_information_symmetry`, `H_null_misspec`, `C_topology_coupling`) with matching example labels
  - `agent_assets/skills/using-bathos/SKILL.md` — one subsection under claim-tier: opaque IDs require labels; lint warns; human output prefers labels

---

### S5. Outcome audit

- **Agent:** reviewer
- **Complexity:** trivial
- **Depends on:** S2, S3, S4
- **Exit criteria:** `uv run pytest` green; manual spot-check `bth lint` on a claim with `id = "H1"` and no label shows WARNING; `bth claim validate` still ERROR on same file.

---

## Deferred

- **`bth lint --campaign <id>`** — run `check_single_cell_gate` + claim lint for one campaign (already stubbed in Phase 2b notes)
- **Assumptions / discriminability `planned_run_label` opacity lint** — extend regex to `A_1`, `outcome_1` patterns in a follow-up
- **Debt #71** changelog table for `output_metadata` history

## Risks

**False-positive lint on legacy claims:** Projects with intentional short IDs (`H1`) and valid labels should pass; only missing/placeholder labels warn. Test with both legacy (opaque id + good label) and broken (opaque id + blank label) fixtures.

**Coverage report schema:** `clause_labels` is additive; consumers ignoring unknown keys remain compatible.

## Definition of done

- Backlog **#2604** closable
- No change to `validate_claim` error semantics (AC-03/AC-14 unchanged)
- ≥15 new tests; total suite still green
- Skill documents the label convention for agents
