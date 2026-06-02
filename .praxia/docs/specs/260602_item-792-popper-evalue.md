# Specification: POPPER E-value Multi-Run Campaign Primitive (Backlog #792)

**Item:** #792
**task_id:** 260602_bathos-v08-sprint
**Date:** 2026-06-02
**Status:** Ready to implement
**Track:** implement
**Supersedes:** exploratory design doc (same file, overwritten)

---

> **Cross-cutting fence (oracle, 260602):** POPPER campaigns require per-run output artifact provenance for the e-value audit trail. The expected CLI surface for artifact queries is `bth outputs list <run_id>` (from backlog #791, results management). This spec locks POPPER's storage model (`campaign_runs.evalue`, `campaign_runs.seq_position`). `bth outputs prune` in #791 must NOT be implemented until this spec is merged and the POPPER storage model is confirmed, because pruning output artifacts that are referenced by an active sequential e-value sequence would break the audit trail. Dependency direction: #792 locks storage model, #791 reads it.

---

## 1. Summary

This spec ships `mode="sequential"` POPPER campaigns in bathos: a statistical accumulator that converts the existing bookkeeping-only `Campaign` primitive into a sequential test of a pre-specified null hypothesis. Each run in a sequential campaign contributes a likelihood-ratio e-value derived from its outcome label; the product of e-values across all non-error runs is compared against a locked stopping threshold. A campaign is eligible for conclusion when the product crosses that threshold for all contributing scripts.

What ships: sidecar `[popper]` block parsing and validation; `campaign_runs` schema extension (`evalue`, `seq_position`); `campaigns` table extension (`stopping_threshold`); `Campaign` dataclass extension; `create_campaign` / `add_run_to_campaign` / `conclude_campaign` logic changes; `review_campaign` POPPER summary; `bth campaign create --sequential` CLI flag; `bth campaign review` text summary; `sprint_audit` `premature_stopping_rate` signal (signal 8); `bth lint` Tier-2 advisory for POPPER runs missing `adversarial_check` (advisory, depends on #760 Tier-2 infrastructure).

What does NOT ship: continuous metric e-values, HTML viz for E_n sequence, run removal from POPPER campaigns, `bth outputs prune` integration. See Section 11.

---

## 2. Sidecar Schema

### 2.1 `[popper]` block within `[experiment]`

The `[popper]` block is an optional sub-section within a TOML file that already has an `[experiment]` section. Its presence opts the sidecar into POPPER mode. No new `SidecarKind` is added — the sidecar remains `SidecarKind.EXPERIMENT`.

```toml
[experiment]
hypothesis = "NVT thermostat with gamma=1.0 maintains ±5K stability over 50ps"

[outcomes.pass]
condition = "temp_std < 5"
decision = "proceed to NPT validation"
reasoning = "within thermostat specification"
is_residual = false
source = "instrument spec IEC 68-2-1:2007"

[outcomes.marginal]
condition = "temp_std >= 5 AND temp_std < 10"
decision = "tune Langevin gamma, re-run"
reasoning = "borderline — retry before promotion"
is_residual = false

[outcomes.fail]
condition = "temp_std >= 10"
decision = "debug thermostat, open issue"
reasoning = "exceeds acceptable range"
is_residual = true

[result_schema]
temp_mean = "float"
temp_std = "float"
n_steps = "int"

[popper]
null_pass_rate     = 0.30   # REQUIRED: P(pass | H0), e.g. baseline pass rate under null
alt_pass_rate      = 0.75   # REQUIRED: P(pass | H1), the intervention's expected pass rate
stopping_threshold = 20.0   # REQUIRED: E_n must reach this; alpha = 1/20 = 0.05

# Optional: override per-label likelihood ratio weights.
# If absent, weights are computed from null_pass_rate / alt_pass_rate (see Section 3).
[popper.weights]
pass     = 2.5   # explicit override; if absent, computed as alt_pass_rate / null_pass_rate
marginal = 1.0   # marginal contributes no evidence
fail     = 0.4   # explicit override; if absent, computed as (1-alt)/(1-null)
error    = 1.0   # must equal 1.0 exactly if present; validation error otherwise
```

### 2.2 Field reference

| Field | Type | Required | Validation rules |
|---|---|---|---|
| `null_pass_rate` | float | Yes | `0 < null_pass_rate < 1`; error if absent or out of range |
| `alt_pass_rate` | float | Yes | `0 < alt_pass_rate < 1`; error if absent or out of range |
| `stopping_threshold` | float | Yes | `>= 1.0`; error if absent or < 1.0; WARNING if < 10.0 |
| `[popper.weights]` | table | No | Each value > 0.0; `error` key must equal 1.0 if present; all keys must be declared outcome labels |

### 2.3 Validation rules

- If `[popper]` is present, all three required fields must be present. Missing any is a validation ERROR.
- `null_pass_rate` and `alt_pass_rate` must be distinct. If equal: validation ERROR ("null and alternative rates are identical; no test power").
- If `[popper.weights]` is present, every key must match a declared outcome label in `[outcomes.*]`. Unknown keys are validation ERRORs.
- If `[popper.weights]` contains `error = X` where `X != 1.0`: validation ERROR.
- `stopping_threshold < 10.0` produces a validation WARNING (field `"popper.stopping_threshold"`, message prefixed `"WARNING:"`). Not an error; consistent with the threshold epistemic hygiene policy from #760/#143.
- `[popper]` on a non-experiment sidecar (benchmark, debug, validation): validation ERROR ("popper block is only valid in [experiment] sidecars").

---

## 3. E-value Computation

### 3.1 Per-run e-value formula

New function: `compute_evalue(sidecar: Sidecar, outcome_label: str) -> float` in `sidecar.py`.

**Case 1:** `[popper.weights]` is present and `outcome_label` has an entry:
```
e_i = popper.weights[outcome_label]
```

**Case 2:** `[popper.weights]` absent, or `outcome_label` not in `[popper.weights]`, and `outcome_label` is a declared pass-direction label. Pass-direction is determined by: the label that is not `is_residual`, not `"marginal"`, not `"error"`, not `"unknown"`.
```
e_i = alt_pass_rate / null_pass_rate
```

**Case 3:** `outcome_label` is a fail-direction label (not pass-direction, not error/unknown/marginal) with no explicit weight:
```
e_i = (1 - alt_pass_rate) / (1 - null_pass_rate)
```

**Special rule — `marginal` with no explicit weight:** `e_i = 1.0`. This is a hard default.

**Special rule — `error` and `unknown` outcomes:** `e_i = 1.0` always, non-overridable (even if `[popper.weights]` provides an override, `error` is validated to be 1.0 at parse time). Error and unknown runs do NOT increment the effective sequence count `n`. Their `evalue` is stored as 1.0 in `campaign_runs` for audit completeness, but are excluded from the E_n product query.

**If sidecar has no `[popper]` block:** `compute_evalue()` returns `1.0` (neutral). The run is still added to the campaign with `evalue=1.0`.

### 3.2 E-value product query

Per-script `E_n` is computed lazily on query from `campaign_runs`. DuckDB does not have a PRODUCT aggregate; use `EXP(SUM(LN(...)))`:

```sql
SELECT
    COALESCE(NULLIF(r.script_sha256, ''), r.sidecar_path, '_ungrouped') AS script_key,
    COUNT(*) FILTER (WHERE r.outcome != 'error' AND r.outcome != 'unknown') AS n_effective,
    COUNT(*) FILTER (WHERE r.outcome = 'error' OR r.outcome = 'unknown')   AS n_excluded,
    EXP(SUM(LN(cr.evalue)) FILTER (WHERE r.outcome != 'error' AND r.outcome != 'unknown')) AS evalue_product
FROM campaign_runs cr
INNER JOIN runs r ON cr.run_id = r.id
WHERE cr.campaign_id = ?
GROUP BY script_key
ORDER BY script_key
```

If `n_effective = 0` for a script, `evalue_product` will be NULL (no rows in the LN aggregate). Treat NULL as `E_n = 1.0` in the conclusion check.

### 3.3 Campaign conclusion condition (code form)

```python
def _campaign_threshold_met(db, campaign_id: str, stopping_threshold: float) -> bool:
    rows = db.execute("""
        SELECT EXP(SUM(LN(cr.evalue)) FILTER (WHERE r.outcome != 'error' AND r.outcome != 'unknown'))
        FROM campaign_runs cr
        INNER JOIN runs r ON cr.run_id = r.id
        WHERE cr.campaign_id = ?
        GROUP BY COALESCE(NULLIF(r.script_sha256, ''), r.sidecar_path, '_ungrouped')
    """, [campaign_id]).fetchall()
    if not rows:
        return False
    return all((row[0] is not None and row[0] >= stopping_threshold) for row in rows)
```

For single-script campaigns this reduces to `E_n >= stopping_threshold`. For multi-script campaigns: ALL scripts must exceed the threshold.

---

## 4. Schema Changes

### 4.1 `campaign_runs` DDL extension

Current DDL (`compact.py:_CAMPAIGN_RUNS_TABLE_SCHEMA`):
```sql
CREATE TABLE IF NOT EXISTS campaign_runs (
    campaign_id TEXT NOT NULL,
    run_id      TEXT NOT NULL,
    PRIMARY KEY (campaign_id, run_id)
)
```

New DDL:
```sql
CREATE TABLE IF NOT EXISTS campaign_runs (
    campaign_id  TEXT NOT NULL,
    run_id       TEXT NOT NULL,
    evalue       REAL CHECK (evalue IS NULL OR evalue > 0),
    seq_position INTEGER,
    PRIMARY KEY (campaign_id, run_id)
)
```

- `evalue`: NULL for non-sequential campaigns; positive float for sequential campaigns; 1.0 for error/unknown runs.
- `seq_position`: NULL for non-sequential campaigns; 1-based integer within the campaign's run sequence (monotonically increasing per campaign, not per script).

### 4.2 `campaigns` DDL extension

New column:
```sql
stopping_threshold REAL   -- NULL for exploration/confirmation; float for sequential
```

Full updated `_CAMPAIGNS_TABLE_SCHEMA`:
```sql
CREATE TABLE IF NOT EXISTS campaigns (
    id                  TEXT PRIMARY KEY,
    project_slug        TEXT NOT NULL,
    name                TEXT NOT NULL,
    mode                TEXT NOT NULL,   -- "exploration"|"confirmation"|"sequential"
    question            TEXT,
    hypothesis          TEXT,
    status              TEXT NOT NULL,
    started_at          TEXT NOT NULL,
    concluded_at        TEXT,
    conclusion          TEXT,
    outcome_label       TEXT,
    parent_campaign_id  TEXT,
    stopping_threshold  REAL
)
```

### 4.3 `Campaign` dataclass

Add one field to `campaigns.py:Campaign`:
```python
stopping_threshold: float | None = None
```

All SELECT statements that read campaigns must include `stopping_threshold` as the 13th column. Update `get_campaign()`, `list_campaigns()`, and the INSERT in `create_campaign()`.

### 4.4 Schema version bump

`schema.py:CURRENT_SCHEMA_VERSION` bumps from `"5"` to `"6"`.

Add `_migrate_v5()` in `compact.py`:
```python
def _migrate_v5(run_dict: dict) -> dict:
    """Migrate v5 fragment to v6 — no new run-level fields; version stamp only."""
    run_dict["schema_version"] = "6"
    return run_dict
```

Register: `MIGRATIONS["5"] = _migrate_v5`.

The `campaign_runs` and `campaigns` table DDL changes are handled by `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` calls inside `compact()`, issued immediately after the `CREATE TABLE IF NOT EXISTS` calls. This handles existing warm DBs that lack the new columns. Example:
```python
con.execute("ALTER TABLE campaign_runs ADD COLUMN IF NOT EXISTS evalue REAL")
con.execute("ALTER TABLE campaign_runs ADD COLUMN IF NOT EXISTS seq_position INTEGER")
con.execute("ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS stopping_threshold REAL")
```

If DuckDB's version on the target system does not support `IF NOT EXISTS` in `ALTER TABLE`, wrap in a try/except that checks `PRAGMA table_info(campaign_runs)` for column existence before issuing the ALTER.

### 4.5 Backward-compatibility guarantee

Existing `exploration` and `confirmation` campaign rows get `stopping_threshold = NULL`. Existing `campaign_runs` rows get `evalue = NULL` and `seq_position = NULL`. All new logic is gated on `campaign.mode == "sequential"` or `cr.evalue IS NOT NULL`. `review_campaign()` returns `"popper": None` for non-sequential campaigns. No existing tests should break.

---

## 5. Per-script Product Semantics

### 5.1 Script grouping key

Runs are grouped by `runs.script_sha256` (content-addressed hash, present since v0.4). Fallback chain if `script_sha256` is empty: use `runs.sidecar_path`. If both empty: use `'_ungrouped'` as a sentinel key. This is display-only; the e-value still contributes to the campaign product.

### 5.2 Rationale for script-level grouping

Multiple scripts in one sequential campaign each have their own hypothesis and null model (declared in their respective sidecar `[popper]` blocks). The campaign concludes only when all scripts' per-script products exceed `stopping_threshold`. This prevents one high-performing script from masking a non-result in another.

When adding a run from script A to a campaign, the sidecar's `stopping_threshold` must match the campaign's `stopping_threshold`. If script A and script B have different `stopping_threshold` values in their sidecars, the second script added raises `CampaignError`. All scripts in a multi-script sequential campaign must declare the same stopping threshold.

---

## 6. Threshold Lock Behavior

### 6.1 When the lock fires

The threshold locks after the first non-error run is successfully inserted into `campaign_runs` for this `campaign_id`. Error and unknown runs do not lock the threshold (they contribute no information).

At `create_campaign()` time: `stopping_threshold` is written as NULL to the campaigns row (it is not a CLI argument).

At `add_run_to_campaign()` time for a sequential campaign:
1. Load the sidecar. If sidecar has `[popper]`, extract `stopping_threshold`.
2. If `campaigns.stopping_threshold IS NULL` and this is a non-error run: write `sidecar.popper_stopping_threshold` to `campaigns.stopping_threshold`.
3. If `campaigns.stopping_threshold IS NOT NULL` and `sidecar.popper_stopping_threshold != campaigns.stopping_threshold`: raise `CampaignError`.

### 6.2 Error on rejected threshold change

```
CampaignError: "Cannot change stopping_threshold for campaign {id[:8]}: "
               "{n} non-error run(s) already added (threshold locked at {threshold}). "
               "To use a different threshold, create a new campaign with "
               "--parent {id[:8]} to preserve lineage."
```

### 6.3 Campaign restart with `parent_campaign_id`

`bth campaign create <new_name> --sequential --parent <old_id>` creates a new campaign that records the old one as its logical predecessor. The old campaign is NOT automatically concluded; the researcher must run `bth campaign conclude <old_id> --outcome abandoned --note "threshold changed"`. The new campaign begins a fresh e-value sequence from E_0 = 1. `parent_campaign_id` is already in the schema; no new DDL is needed.

---

## 7. CLI Changes

### 7.1 `bth campaign create`

New flag:
```
--sequential    Shorthand for --mode sequential.
```

Full signature:
```
bth campaign create <name>
    [--mode exploration|confirmation|sequential]
    [--sequential]
    [--question TEXT]
    [--hypothesis TEXT]
    [--parent CAMPAIGN_ID]
```

Conflict resolution: `--sequential` and `--mode exploration|confirmation` together raises `typer.BadParameter`. `stopping_threshold` is NOT a CLI argument (sidecar-first design). Output: `Created campaign {id[:8]} — {name} (sequential)`.

### 7.2 `bth campaign add` (unchanged surface, new behavior)

When adding a run to a `mode="sequential"` campaign, `add_run_to_campaign()` now:
1. Fetches `sidecar_path` from `runs` table.
2. If sidecar exists and has `[popper]`, computes `evalue = compute_evalue(sidecar, run.outcome)`.
3. If sidecar absent or has no `[popper]`, uses `evalue = 1.0`.
4. Assigns `seq_position = (SELECT COALESCE(MAX(seq_position), 0) + 1 FROM campaign_runs WHERE campaign_id = ?)`.
5. Inserts `(campaign_id, run_id, evalue, seq_position)`.
6. Applies threshold lock logic (Section 6.1).

For non-sequential campaigns, `add_run_to_campaign()` inserts `(campaign_id, run_id, NULL, NULL)` and skips all e-value logic.

### 7.3 `bth campaign review` (extended output)

For `mode="sequential"` campaigns, append after the existing output:

```
POPPER Sequential Test
  Stopping threshold : 20.0  (alpha ≈ 0.05)
  Scripts in campaign: 2

  script_key       n_eff  n_excl  E_n      threshold_met
  ─────────────    ─────  ──────  ───────  ─────────────
  abc123 (sha)        12       1   34.7    YES
  def456 (sha)         8       0    6.2    NO

  Campaign conclusion: NOT YET REACHED (1 of 2 scripts below threshold)
```

The `review_campaign()` return dict gains:
```python
{
    "popper": {
        "mode": "sequential",
        "stopping_threshold": 20.0,
        "threshold_met": False,
        "scripts": [
            {
                "script_key": "abc123...",
                "n_effective": 12,
                "n_excluded": 1,
                "evalue_product": 34.7,
                "threshold_met": True,
            },
        ],
    }
}
```

For non-sequential campaigns: `"popper": None`.

### 7.4 `bth campaign conclude` (soft threshold guard)

New flags:
```
--force                     Skip threshold warning (for scripted use)
--abort-if-below-threshold  Exit 1 if threshold not met (strict mode)
```

For `mode="sequential"` campaigns, before concluding:
1. Compute `_campaign_threshold_met()`.
2. If NOT met and `--force` not passed: print `"WARNING: E_n has not reached stopping_threshold ({ep:.1f} < {threshold:.1f}). This will be flagged as premature stopping in sprint-audit."` and continue.
3. If NOT met and `--abort-if-below-threshold` passed: `raise typer.Exit(1)`.
4. If met: conclude normally.

---

## 8. Fixer Task Decomposition

Tasks are ordered by dependency. Each is sized for a single-fixer dispatch.

---

### Task 1: `sidecar.py` — parse `[popper]` block + `compute_evalue()`

**Files:** `src/bathos/sidecar.py` (modify, lines 36–131)

**Changes:**
- Add four fields to `Sidecar` dataclass: `popper_null_pass_rate: float | None = None`, `popper_alt_pass_rate: float | None = None`, `popper_stopping_threshold: float | None = None`, `popper_weights: dict[str, float] = field(default_factory=dict)`.
- In `parse_sidecar()` inside the `"experiment"` branch, extract `data.get("popper", {})` and populate the four new fields.
- Add `compute_evalue(sidecar: Sidecar, outcome_label: str, pass_labels: set[str] | None = None) -> float` implementing Section 3.1. Default `pass_labels` if not provided: infer from `sidecar.outcomes` as labels that are not `is_residual` and not in `{"marginal", "error", "unknown"}`. Returns `1.0` if sidecar has no `[popper]` block.

**Gate:** `uv run pytest tests/test_sidecar.py -k popper` (new tests must cover: parse with [popper] block, parse without, compute_evalue for pass/fail/marginal/error, explicit weight override)

**Scope estimate:** ~65 LOC

**Dependencies:** none

---

### Task 2: `validate.py` — `validate_popper_block()`

**Files:** `src/bathos/validate.py` (modify, lines 35–139)

**Changes:**
- Add `validate_popper_block(sidecar: Sidecar, sidecar_path: Path | None = None) -> list[ValidationError]` with all rules from Section 2.3.
- Call from `validate_sidecar()` when `sidecar.popper_null_pass_rate is not None`. Extend the returned `errors` list.
- WARNING-level entry for `stopping_threshold < 10.0`: use `ValidationError(field="popper.stopping_threshold", message="WARNING: stopping_threshold < 10.0 — consider a stricter threshold (alpha < 0.10)")`.

**Gate:** `uv run pytest tests/test_validate.py -k popper` (tests: missing required fields, out-of-range rates, null==alt, unknown weight key, error weight != 1.0, threshold < 10.0 warning, threshold >= 10.0 no warning)

**Scope estimate:** ~55 LOC

**Dependencies:** Task 1

---

### Task 3: `compact.py` + `schema.py` — DDL extension + schema version bump

**Files:** `src/bathos/compact.py` (modify), `src/bathos/schema.py` (modify, line 9)

**Changes:**
- `schema.py`: `CURRENT_SCHEMA_VERSION = "5"` → `"6"`.
- `compact.py:_CAMPAIGNS_TABLE_SCHEMA`: add `stopping_threshold REAL` column.
- `compact.py:_CAMPAIGN_RUNS_TABLE_SCHEMA`: add `evalue REAL CHECK (evalue IS NULL OR evalue > 0)` and `seq_position INTEGER` columns.
- `compact.py:compact()`: after the `CREATE TABLE IF NOT EXISTS` calls, add three `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` statements (with try/except fallback as noted in Section 4.4).
- `compact.py:_migrate_v5()`: new function, bumps schema_version to "6". Register in `MIGRATIONS["5"]`.

**Gate:** `uv run pytest tests/test_compact.py` (all existing tests pass; new test: compact on a pre-existing DB without new columns succeeds; new columns present after compact)

**Scope estimate:** ~35 LOC

**Dependencies:** none (parallel with Tasks 1 and 2)

---

### Task 4: `campaigns.py` — `Campaign` dataclass + create/add/conclude with e-value logic

**Files:** `src/bathos/campaigns.py` (modify, lines 17–117)

**Changes:**
- `Campaign` dataclass: add `stopping_threshold: float | None = None`.
- `create_campaign()`: accept `mode="sequential"`; add `stopping_threshold: float | None = None` parameter; include in INSERT; update mode validation.
- Update all SELECT statements in `get_campaign()` and `list_campaigns()` to include `stopping_threshold` (13th column).
- `add_run_to_campaign()`: when `campaign.mode == "sequential"`, load sidecar from `sidecar_path` (fetched via `SELECT sidecar_path FROM runs WHERE id = ?`), call `compute_evalue()`, assign `seq_position`, apply threshold lock logic (Section 6.1), insert with evalue and seq_position. For non-sequential campaigns, insert with NULL evalue and NULL seq_position.
- Add `_campaign_threshold_met(db, campaign_id: str, stopping_threshold: float) -> bool` (Section 3.3).
- `conclude_campaign()`: threshold guard is in the CLI layer (Task 6), not here.

**Gate:** `uv run pytest tests/test_campaigns.py` (all existing tests pass; new tests: sequential create, threshold lock fires on second conflicting sidecar, evalue stored correctly, seq_position increments, non-sequential campaigns unaffected)

**Scope estimate:** ~100 LOC

**Dependencies:** Tasks 1, 3

---

### Task 5: `campaigns.py` — `review_campaign()` POPPER summary

**Files:** `src/bathos/campaigns.py` (modify, lines 147–186)

**Changes:**
- Add campaign mode lookup at start of `review_campaign()`: `SELECT mode, stopping_threshold FROM campaigns WHERE id = ?`.
- If `mode == "sequential"`: execute the per-script product query (Section 3.2) and populate `result["popper"]` with the dict structure from Section 7.3.
- If not sequential: set `result["popper"] = None`.

**Gate:** `uv run pytest tests/test_campaigns.py -k review` (popper key present for sequential campaigns; None for non-sequential; per-script breakdown correct)

**Scope estimate:** ~45 LOC

**Dependencies:** Task 4

---

### Task 6: `cli.py` — campaign create/review/conclude CLI changes

**Files:** `src/bathos/cli.py` (modify, lines 506–528, 551–569, 618–640)

**Changes:**
- `campaign_create()`: add `sequential: bool = typer.Option(False, "--sequential")`. Resolve effective mode; pass to `create_campaign()`.
- `campaign_review()`: call `render_popper_summary(result["popper"])` (from Task 7) after existing `render_campaign_review()` call, if `result["popper"]` is not None.
- `campaign_conclude()`: add `force: bool = typer.Option(False, "--force")` and `abort_if_below_threshold: bool = typer.Option(False, "--abort-if-below-threshold")`. For sequential campaigns, implement threshold check + warning logic from Section 7.4.

**Gate:** `uv run pytest tests/test_cli.py -k campaign` (--sequential flag, review shows POPPER block, conclude warns on below-threshold, --abort-if-below-threshold exits 1)

**Scope estimate:** ~55 LOC

**Dependencies:** Tasks 4, 5, 7

---

### Task 7: `rich_fmt.py` — `render_popper_summary()`

**Files:** `src/bathos/rich_fmt.py` (modify)

**Changes:**
- Add `render_popper_summary(popper_data: dict) -> None`: renders the text table from Section 7.3 using existing Rich `Table` / `Console` patterns. Prints nothing (returns immediately) if `popper_data is None`.

**Gate:** `uv run pytest tests/test_rich_fmt.py -k popper` (snapshot: output contains "POPPER Sequential Test", threshold line, per-script rows with YES/NO)

**Scope estimate:** ~45 LOC

**Dependencies:** Task 5 (needs data shape)

---

### Task 8: `sprint_audit.py` — signal 8 `premature_stopping_rate`

**Files:** `src/bathos/sprint_audit.py` (modify, lines 290–345)

**Changes:**
- After Signal 7 (`post_hoc_bias_flag`) computation, add Signal 8:

```python
# Signal 8: premature_stopping_rate
# Fraction of concluded sequential campaigns where final E_n < stopping_threshold.
# Domain rationale: POPPER (arXiv 2502.09858) — sequential stopping below pre-specified
# threshold invalidates the anytime-valid guarantee. Calibration-target warning, consistent
# with threshold ADR (260601_sprint-audit-threshold-rationale.md).
```

- Query: from the warm DB, `SELECT id, stopping_threshold FROM campaigns WHERE mode='sequential' AND status='concluded'`. For each, call `_campaign_threshold_met()`. Count those where threshold NOT met.
- `signals["premature_stopping_rate"] = n_premature / max(n_sequential_concluded, 1)`. Set to 0.0 if no concluded sequential campaigns.
- Anomaly threshold: `premature_stopping_rate > 0.0` (any premature stopping is flagged). Message: `"Project: premature_stopping_rate {rate:.1%} — {n} sequential campaign(s) concluded before reaching stopping_threshold (sequential test validity compromised)"`.

**Gate:** `uv run pytest tests/test_sprint_audit.py -k premature` (concluded sequential campaign with E_n < threshold detected; campaign with E_n >= threshold not flagged; 0.0 when no sequential campaigns)

**Scope estimate:** ~40 LOC

**Dependencies:** Tasks 3, 4

---

### Task 9: `linter.py` — Tier-2 advisory for POPPER runs missing `adversarial_check`

**Files:** `src/bathos/linter.py` (modify)

**Advisory note:** This task depends on the `IssueSeverity.WARNING` Tier-2 infrastructure shipped in #760/#143. That infrastructure is already present in `linter.py`. This task can proceed independently.

**Changes:**
- Add `check_popper_adversarial(project_root: Path) -> list[LintIssue]`: scans `scripts/experiments/**/*.bth.toml`, parses each via `tomllib`, and warns for any sidecar that has a `[popper]` section but has no `adversarial_check` field in any `[outcomes.*]` branch. Severity: `IssueSeverity.WARNING`.
- Wire into `bth lint` CLI command alongside existing Tier-2 checks.

**Gate:** `uv run pytest tests/test_linter.py -k popper_adversarial` (warning for POPPER sidecar without adversarial_check; no warning when at least one outcome has adversarial_check; no warning for non-POPPER sidecar)

**Scope estimate:** ~30 LOC

**Dependencies:** Task 1 (sidecar structure)

---

### Task 10: Integration test files

**Files:** `tests/test_campaigns_popper.py` (create), `tests/test_sidecar_popper.py` (create)

**Changes:** New test files covering the full test plan from Section 9. `test_sidecar_popper.py` focuses on `compute_evalue()` and `validate_popper_block()`. `test_campaigns_popper.py` covers the full sequential campaign lifecycle: create, add runs, threshold lock, multi-script grouping, conclude, review.

**Gate:** `uv run pytest tests/test_campaigns_popper.py tests/test_sidecar_popper.py` (all pass)

**Scope estimate:** ~160 LOC

**Dependencies:** Tasks 1–8

---

## 9. Test Plan Sketch

**Happy path — single script:**
Create `mode="sequential"` campaign. Add 10 runs: 7 `pass`, 2 `fail`, 1 `error`. `null_pass_rate=0.3`, `alt_pass_rate=0.75`, no explicit weights. Verify `E_n = (0.75/0.3)^7 * (0.25/0.7)^2 ≈ 14.6`. Verify `n_effective = 9`. Verify threshold NOT met for `stopping_threshold = 20.0`.

**Happy path — multi-script:**
Create `mode="sequential"` campaign. Add runs from 2 distinct `script_sha256` values (script A: 8 pass; script B: 3 pass, 2 fail). Verify per-script products computed independently. Verify `threshold_met = False` if script B product is below threshold. Add 5 more pass runs to script B. Verify `threshold_met = True` when both scripts exceed threshold.

**Threshold lock violation:**
Add one non-error run to sequential campaign (threshold locks from first sidecar's `stopping_threshold = 20.0`). Attempt to add a run whose sidecar declares `stopping_threshold = 50.0`. Verify `CampaignError` is raised with the exact message from Section 6.2.

**Error-run neutralization:**
Add only error runs (outcome="error") to a sequential campaign. Verify `n_effective = 0`, `evalue_product = NULL`, threshold NOT met. Verify threshold does NOT lock after adding only error runs. Now add one non-error run; verify threshold locks.

**Marginal default weight:**
Sidecar has `pass`, `marginal`, `fail` outcomes, no `[popper.weights]` block. Add runs with outcome `marginal`. Verify each contributes `evalue = 1.0` in `campaign_runs`.

**Per-script product grouping:**
Three runs from the same script_sha256, all `pass`. `alt_pass_rate=0.8`, `null_pass_rate=0.4`. Expected E_n = (0.8/0.4)^3 = 8.0.

**Explicit weight override:**
Sidecar has `[popper.weights] pass = 3.0`. Add a `pass` run. Verify `evalue = 3.0` stored.

**`validate_popper_block` error cases:**
- `null_pass_rate = 1.5` → ERROR.
- `null_pass_rate == alt_pass_rate` → ERROR.
- `stopping_threshold = 5.0` → WARNING message.
- `stopping_threshold = 10.0` → no WARNING.
- `[popper.weights]` contains `error = 2.0` → ERROR.
- `[popper.weights]` contains unknown label `"great"` → ERROR.
- `[popper]` on a benchmark sidecar → ERROR.

**Premature stopping detection:**
Create and conclude a sequential campaign where final `E_n = 8.0 < stopping_threshold = 20.0`. Run `sprint_audit()`. Verify `signals["premature_stopping_rate"] > 0` and anomaly message is present. Create and conclude another campaign where `E_n = 25.0 >= 20.0`; verify it is not flagged.

**Backward compatibility:**
Run `compact()` on a warm DB that has existing `exploration` campaign rows. Verify `stopping_threshold = NULL` for existing rows. Verify `review_campaign()` returns `"popper": None`. Verify `list_campaigns()` works without error.

---

## 10. Risk Table

| Risk | Mitigation |
|---|---|
| `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` may not be supported in older DuckDB versions | Wrap in try/except; use `PRAGMA table_info(campaign_runs)` to check column existence as fallback. Add explicit test that runs compact on an existing DB without the new columns. |
| `EXP(SUM(LN(evalue)))` produces NaN or -Inf if any `evalue <= 0` is stored due to a migration bug | Enforce `CHECK (evalue IS NULL OR evalue > 0)` in DDL. `compute_evalue()` must never return a value <= 0; add assertion. Test evalue=0 path to confirm it never reaches storage. |
| Multi-script conclusion blocks indefinitely if one script has no runs | Document in `bth campaign review` output: "1 of 2 scripts has no effective runs." No timeout in v0.8. |
| `[popper]` block on benchmark/debug sidecar silently ignored by `parse_sidecar()`; researcher might add it expecting behavior | `validate_popper_block()` must check `sidecar.kind == SidecarKind.EXPERIMENT` and emit ERROR for non-experiment kinds. Wire into `validate_sidecar()`. |
| `sprint_audit` query for concluded sequential campaigns may be slow without an index | Add `CREATE INDEX IF NOT EXISTS idx_campaigns_mode_status ON campaigns (mode, status)` in `compact()`. |

---

## 11. Out of Scope

The following are explicitly NOT in this spec and must not be implemented:

- **Continuous metric e-values**: label-only for v0.8; deriving e-values from continuous `result_schema` fields deferred.
- **HTML viz for E_n sequence**: chart of `E_n` vs. run sequence position in `bth export --html` or `bth view`. Text summary only in v0.8.
- **Run removal from POPPER campaigns**: `campaign_runs` is insert-only. Invalid runs must be neutralized by setting `outcome="error"` on the run, not by deletion.
- **`bth outputs prune` interaction**: per the oracle fence, `bth outputs prune` (#791) must not be implemented until this spec is merged.
- **Betting score / Kelly e-value formulation**: only the likelihood ratio formula (Section 3.1) is supported in v0.8.
- **Campaign-level adversarial check enforcement gate**: per-run only. Task 9 adds lint advisory only.
- **MCP tool surface changes**: `mcp__bathos__campaign_*` tools will naturally return the new `"popper"` key from `review_campaign()` without explicit changes.
- **Universal inference e-values**: deferred.

---

## References

- POPPER: Automated Hypothesis Validation with Agentic Sequential Falsifications — arXiv 2502.09858
- Structural Enforcement of Statistical Rigor in AI-Driven Discovery — arXiv 2511.06701
- Sound Agentic Science Requires Adversarial Experiments — arXiv 2604.22080
- Agentic Science Research Synthesis — `.praxia/docs/research/260526_agentic-science-nlm-synthesis.md`, section 2.6
- ADR for sprint-audit threshold rationale — `.praxia/docs/decisions/260601_sprint-audit-threshold-rationale.md`
- Spec #760 (threshold epistemic hygiene) — `.praxia/docs/specs/260602_item-760-threshold-lint.md`
- Spec #791 (results management) — `.praxia/docs/specs/260602_item-791-results-management.md`
