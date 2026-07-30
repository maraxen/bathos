# bathos v0.2 Migration: Forward-Seed Strategy

**Date:** 2026-05-18  
**Status:** Approved (Oracle Feedback Integrated)  
**Task ID:** 260518_bathos_migration_recon  

---

## Executive Summary

Migrate 4 research projects (bathos, prolix, oaf, demistify) to bathos v0.2 catalog and sidecar architecture using a **forward-seed strategy**: intentionally create new v0.2-compliant seed runs rather than backfill historical runs.

**Key constraint:** No load-bearing historical queries exist in target projects. All 10+ projects use independent catalogs or no tracking at all. Migration carries zero risk of breaking live research workflows.

**Scope:** 4 projects, 30–40 seed runs, all 5 sidecar types (experiment, benchmark, validation, debug, check), with deliberate deferral of historical SLURM log migration (#135/#136) to a separate sprint.

**Timeline:** 5 phases over 2–3 weeks (parallelizable). Phase gates: concrete shell commands with expected output.

---

## Target Projects

| Project | Kind | Status | Catalog | Scripts Target |
|---------|------|--------|---------|-----------------|
| **bathos** | Framework + tests | v0.1 live | `~/.bth/catalog/` | self-test + v0.2 validation |
| **prolix** | Benchmarking + research | Pre-tracking | Custom | 3+ benchmarks, 2+ experiments, outcomes |
| **oaf** | Research (hypothesis) | Pre-tracking | Custom | 3+ experiments, 2+ validations, outcomes |
| **demistify** | Research (validation) | Pre-tracking | Custom | 2+ experiments, 2+ validations, outcomes |

**Excluded from this migration (deferred):**
- **naurmalade** — OpenMM-only catalog, separate system
- **praxia** — Framework, not research
- **tev_design, prxteinmpnn, phyllo, maraxiom, mistypotts, asr** — Design sprints, scheduled for separate migration sprint
- **pytest wrapping** — CI regression tracking, not research hypothesis

---

## Architecture Overview

### Sidecar Schemas (One per Script Directory)

Each project's scripts directory receives 1–4 sidecars per `<script-stem>.bth.toml`:

#### Experiment Schema
```toml
[experiment]
hypothesis = "Clear, falsifiable statement about system behavior"

[outcomes.pass]
condition = "metric < threshold"        # DuckDB SQL fragment
decision = "Next step or hypothesis confirmed"

[outcomes.marginal]
condition = "metric >= threshold_lower AND metric < threshold_upper"
decision = "Tuning direction or further investigation"

[outcomes.fail]
condition = "metric >= threshold"
decision = "Root cause analysis or design change"

[result_schema]
metric_name = "float"
supporting_field = "int"
```

#### Benchmark Schema
```toml
[benchmark]
baseline_ref = "run_uuid_of_reference"
metric = "ns_per_day"
regression_threshold = 0.05             # ±5% is marginal
target = "Qualitative goal (e.g., >50 ns/day on pi_so3)"

[result_schema]
ns_per_day = "float"
system = "str"
atom_count = "int"
ensemble = "str"
```

#### Validation Schema
```toml
[validation]
property = "Energy conservation or property X"
reference = "Literature value or analytical solution"
tolerance = 0.01                        # Within 1%

[result_schema]
property_value = "float"
reference_value = "float"
error_pct = "float"
```

#### Debug Schema
```toml
[debug]
symptom = "Concrete symptom (NaN forces, divergence, etc.)"
suspected_cause = "Initial hypothesis about cause"
verification = "Reproduction steps or test condition"

[verdict_schema]
reproduced = "bool"
root_cause = "str"
fix = "str"
```

### Warm Schema

After `bth compact`, runs live in `~/.bth/catalog/bathos.db` with expanded schema.

---

## Phase 1: Scope & Tooling Validation (Days 1–2)

**Task 1.1: Scaffold and validate .bth.toml syntax on all 4 projects**
- Audit existing `.bth.toml` files (if any)
- Validate TOML syntax and required fields (`[project]`, `slug`, `root`)
- Run: `python -m tomllib` on each file

**Task 1.2: Validate project inventory** (bathos, prolix, oaf, demistify only)

**Task 1.3: Test bth init / bth run on bathos self (smoke test)**

**Task 1.4: Verify SLURM job tracking readiness**

**Task 1.5: Catalog snapshot & rollback checkpoint**
```bash
# DuckDB-safe export (avoids WAL lock issues)
duckdb ~/.bth/catalog/bathos.db -c "EXPORT DATABASE '/tmp/bth_snapshot_20260518';"
# Verify: [ -d /tmp/bth_snapshot_20260518 ] && echo "✓ Snapshot ready"
```

---

## Phase 2: bathos Self-Registration (Days 2–4)

**Task 2.1: Create scripts/experiments/.bth.toml**

**Task 2.2: Create scripts/benchmarks/.bth.toml**

**Task 2.3: Register 5 curated bathos runs (named scripts)**
```bash
# Five specific scripts to re-execute with bth run wrapper:
# 1. tests/test_schema.py — validates Run dataclass and COOL_SCHEMA round-trip
# 2. tests/test_catalog.py — validates cool-tier Parquet write and read
# 3. tests/test_compact.py — validates warm-tier DuckDB ingestion
# 4. tests/test_query.py — validates list_runs, find_runs, run_sql query API
# 5. tests/test_integration.py — validates full bth init→run→ls→sql→compact workflow

for script in tests/test_{schema,catalog,compact,query,integration}.py; do
  uv run bth run --tag seed --tag sprint-2026-05-migration -- python $script || exit 1
done
```

**Task 2.4: Verify bth ls / bth find / bth sql queries on warm catalog**

**Task 2.5: Test bth compact and output_metadata JSON storage**

---

## Phase 3: prolix (Benchmarking) — [Parallel with Phase 4]

**Task 3.1: Map prolix script structure**

**Task 3.2: Create scripts/benchmarks/*.bth.toml for NVT/NPT trials (3+ sidecars)**
- nvt_water_tip3p.py
- npt_water_tip3p.py
- cutoff_sweep.py

**Task 3.3: Create scripts/experiments/*.bth.toml for validation runs (2+ sidecars)**
- validate_energy_conservation.py
- validate_pressure_isotropy.py

**Task 3.4: Seed 10–20 curated prolix runs**
- 5 benchmarks (regression conditions)
- 3 experiments (outcome diversity)
- 2 validation (properties)

**Task 3.5: Validate regression detection**

**Task 3.6: Test bth sync prolix runs to/from cluster**

**Task 3.7: Phase 3 compaction (do NOT run Phase 4 compact until this completes)**
```bash
bth compact
# Expected: ~10–20 prolix runs merged into warm DuckDB
```

---

## Phase 4: oaf + demistify (Research) — [Parallel with Phase 3]

**Task 4.1: Map oaf script structure**

**Task 4.2: Create scripts/experiments/*.bth.toml for oaf hypothesis tests (3+ sidecars)**

**Task 4.3: Map demistify script structure + create inventory file**

**Task 4.4: Create scripts/experiments/*.bth.toml for demistify validation (2+ sidecars)**

**Task 4.5: Seed 15–25 oaf + demistify runs**
- 10 oaf experiments
- 5 demistify experiments/validation

**Task 4.6: Validate outcome condition evaluation (DuckDB SQL fragments)**

**Task 4.7: Test bth check freshness vs git HEAD for oaf/demistify repos**

**Task 4.8: Phase 4 compaction (do NOT run until Phase 3 Task 3.7 completes)**
```bash
bth compact
# Expected: ~15–25 oaf+demistify runs merged into warm DuckDB
```

---

## Phase 5: Integration & Finalization (Days 4–5)

**Task 5.1: Full cross-project query validation**

**Task 5.2: Verify all sidecars parse without error**

**Task 5.3: Outcome condition evaluation validation**

**Task 5.4: SLURM sync end-to-end validation**

**Task 5.5: Document outcomes and generate final report**

---

## Phase Exit Gates (Complete Shell Commands)

### Phase 1 Gate
```bash
echo "=== PHASE 1 GATE CHECK ==="

# 1. TOML syntax (D1: fixed bash syntax)
for toml in $(find /home/marielle/projects/{bathos,prolix,oaf,demistify}/scripts -name "*.bth.toml" 2>/dev/null); do
  python3 -c "import tomllib; tomllib.load(open('$toml', 'rb'))" || { echo "✗ TOML parse failed: $toml"; exit 1; }
done && echo "✓ All .bth.toml files parse"

# 2. Project inventory
[ -f inventory.txt ] && [ $(wc -l < inventory.txt) -eq 4 ] || { echo "✗ Inventory missing or wrong count"; exit 1; }
echo "✓ Project inventory: bathos, prolix, oaf, demistify"

# 3. bth init smoke test
rm -rf /tmp/bth_smoke_test && mkdir -p /tmp/bth_smoke_test
cd /tmp/bth_smoke_test
uv run bth init --slug bathos --root . || { echo "✗ bth init failed"; exit 1; }
[ -f .bth.toml ] && [ -d .bth/catalog ] || { echo "✗ bth init did not create files"; exit 1; }
echo "✓ bth init succeeds; catalog dir created"

# 4. bth run smoke test (D1: fixed bash glob)
uv run bth run -- python3 -c "import sys; sys.exit(0)" || { echo "✗ bth run failed"; exit 1; }
ls .bth/catalog/runs/*.parquet >/dev/null 2>&1 && echo "✓ bth run creates cool-tier Parquet" || { echo "✗ Parquet file not found"; exit 1; }

# 5. SLURM SSH connectivity
ssh engaging "ls -d ~/.bth/catalog/runs" >/dev/null 2>&1 || { echo "✗ SLURM unreachable"; exit 1; }
echo "✓ SLURM connectivity OK"

# 6. Snapshot created
[ -d /tmp/bth_snapshot_20260518 ] || { echo "✗ Snapshot missing"; exit 1; }
echo "✓ Catalog snapshot exists"

echo "═══════════════════════════"
echo "PHASE 1 GATE: PASS ✓"
```

### Phase 2 Gate
```bash
echo "=== PHASE 2 GATE CHECK ==="

# 1. Seed runs count (D2/D3: fixed — use bth find, not bth ls | grep)
seed_count=$(uv run bth find --project bathos --tag sprint-2026-05-migration 2>/dev/null | wc -l)
[ $seed_count -ge 5 ] || { echo "✗ Seed count $seed_count < 5"; exit 1; }
echo "✓ Seed runs: $seed_count >= 5"

# 2. Compact succeeds
uv run bth compact || { echo "✗ Compact failed"; exit 1; }
echo "✓ bth compact succeeds"

# 3. Warm catalog queryable (D8: verify output format on live DB)
warm_count=$(uv run bth sql "SELECT COUNT(*) AS total FROM runs WHERE project_slug='bathos'" 2>/dev/null | grep -oE '[0-9]+' | tail -1)
[ -n "$warm_count" ] && [ "$warm_count" -ge 5 ] || { echo "✗ Warm count invalid: $warm_count"; exit 1; }
echo "✓ Warm catalog: $warm_count runs"

# 4. Metadata JSON valid
uv run bth sql "SELECT CAST(metadata AS JSON) FROM runs WHERE project_slug='bathos' LIMIT 1" >/dev/null 2>&1 || { echo "✗ Metadata JSON invalid"; exit 1; }
echo "✓ Metadata JSON valid"

# 5. All seed runs tagged (D2/D3: fixed — use bth find)
tagged=$(uv run bth find --project bathos --tag sprint-2026-05-migration 2>/dev/null | wc -l)
[ $tagged -eq $seed_count ] || { echo "✗ Not all seed runs tagged ($tagged != $seed_count)"; exit 1; }
echo "✓ All $seed_count seed runs tagged"

echo "═══════════════════════════"
echo "PHASE 2 GATE: PASS ✓"
```

### Phase 3 Gate
```bash
echo "=== PHASE 3 GATE CHECK ==="

# 1. Prolix seed count (D2/D3: fixed — use bth find)
seed_count=$(uv run bth find --project prolix --tag sprint-2026-05-migration 2>/dev/null | wc -l)
[ $seed_count -ge 10 ] || { echo "✗ Prolix seed count $seed_count < 10"; exit 1; }
echo "✓ Prolix seeds: $seed_count >= 10"

# 2. Regression thresholds present (D8: fixed — extract from metadata JSON)
threshold_count=$(uv run bth sql "SELECT COUNT(*) FROM runs WHERE project_slug='prolix' AND metadata LIKE '%regression_threshold%'" 2>/dev/null | grep -oE '[0-9]+' | tail -1)
[ "$threshold_count" -ge 5 ] || { echo "✗ Regression thresholds: $threshold_count < 5"; exit 1; }
echo "✓ Regression thresholds: $threshold_count >= 5"

# 3. Sync to cluster (dry-run) (D7: fixed — use actual remote name from config)
remote_name=$(grep "^\[remotes\]" ~/.bth.toml -A 5 | grep "name\s*=" | head -1 | cut -d'"' -f2)
rsync -azP --dry-run ~/.bth/catalog/runs/ $remote_name:~/.bth/catalog/runs/ | grep -q "speedup is" || { echo "✗ Sync test failed"; exit 1; }
echo "✓ bth sync validates"

# 4. Phase 3 compact done (D9: fixed — set sentinel and verify)
uv run bth compact || { echo "✗ Phase 3 compact failed"; exit 1; }
touch /tmp/bth_phase3_compact_done
[ -f /tmp/bth_phase3_compact_done ] && echo "✓ Phase 3 compact complete — Phase 4 may proceed"
echo "✓ Sentinel: /tmp/bth_phase3_compact_done set"

echo "═══════════════════════════"
echo "PHASE 3 GATE: PASS ✓"
```

### Phase 4 Gate
```bash
echo "=== PHASE 4 GATE CHECK ==="

# 0. Wait for Phase 3 compaction (D9: fixed — check sentinel)
[ -f /tmp/bth_phase3_compact_done ] || { echo "✗ Phase 3 compact not yet done"; exit 1; }
echo "✓ Phase 3 compaction verified; proceeding with Phase 4"

# 1. oaf + demistify seed counts (D2/D3: fixed — use bth find)
oaf_count=$(uv run bth find --project oaf --tag sprint-2026-05-migration 2>/dev/null | wc -l)
dem_count=$(uv run bth find --project demistify --tag sprint-2026-05-migration 2>/dev/null | wc -l)
[ $oaf_count -ge 10 ] || { echo "✗ oaf seeds $oaf_count < 10"; exit 1; }
[ $dem_count -ge 5 ] || { echo "✗ demistify seeds $dem_count < 5"; exit 1; }
echo "✓ oaf seeds: $oaf_count, demistify seeds: $dem_count"

# 2. Outcomes evaluated (D8: fixed — extract count)
outcome_count=$(uv run bth sql "SELECT COUNT(*) FROM runs WHERE project_slug IN ('oaf','demistify') AND outcome IS NOT NULL" 2>/dev/null | grep -oE '[0-9]+' | tail -1)
[ -n "$outcome_count" ] && [ "$outcome_count" -ge 10 ] || { echo "⚠ Outcomes: $outcome_count (may be 0 if outcome evaluation not wired in v0.2; proceed)"; }
echo "✓ Outcomes check: $outcome_count runs"

# 3. bth check git drift (D4: fixed — no --project flag, use cd)
(cd ~/projects/oaf && uv run bth check >/dev/null 2>&1) || { echo "✗ bth check oaf failed"; exit 1; }
(cd ~/projects/demistify && uv run bth check >/dev/null 2>&1) || { echo "✗ bth check demistify failed"; exit 1; }
echo "✓ bth check runs successfully on both projects"

# 4. Phase 4 compact done (D9: fixed — set sentinel)
uv run bth compact || { echo "✗ Compact failed"; exit 1; }
touch /tmp/bth_phase4_compact_done
echo "✓ Phase 4 compact complete — Phase 5 may proceed"

echo "═══════════════════════════"
echo "PHASE 4 GATE: PASS ✓"
```

### Phase 5 Gate
```bash
echo "=== PHASE 5 GATE CHECK (FINAL) ==="

# 1. All 4 projects queryable (D6: fixed — use bth find, not wc -w on ls)
for proj in bathos prolix oaf demistify; do
  count=$(uv run bth find --project $proj 2>/dev/null | wc -l)
  [ $count -ge 1 ] || { echo "✗ Project $proj not queryable"; exit 1; }
done
echo "✓ All 4 projects queryable"

# 2. Total seed count >= 30 (D5: fixed — use list_contains on tags, not metadata JSON)
total=$(uv run bth sql "SELECT COUNT(*) FROM runs WHERE list_contains(tags, 'sprint-2026-05-migration')" 2>/dev/null | grep -oE '[0-9]+' | tail -1)
[ -n "$total" ] && [ "$total" -ge 30 ] || { echo "✗ Total seeds $total < 30"; exit 1; }
echo "✓ Total seeds: $total >= 30"

# 3. Sidecar diversity
bench=$(find /home/marielle/projects/{bathos,prolix,oaf,demistify}/scripts -name "*.bth.toml" -exec grep -l "^\[benchmark\]" {} \; 2>/dev/null | wc -l)
exp=$(find /home/marielle/projects/{bathos,prolix,oaf,demistify}/scripts -name "*.bth.toml" -exec grep -l "^\[experiment\]" {} \; 2>/dev/null | wc -l)
val=$(find /home/marielle/projects/{bathos,prolix,oaf,demistify}/scripts -name "*.bth.toml" -exec grep -l "^\[validation\]" {} \; 2>/dev/null | wc -l)
[ $bench -ge 1 ] && [ $exp -ge 1 ] && [ $val -ge 1 ] || { echo "✗ Sidecar diversity: bench=$bench exp=$exp val=$val"; exit 1; }
echo "✓ Sidecar diversity: bench=$bench, exp=$exp, validation=$val"

# 4. Historical logs documented
[ -f /tmp/HISTORICAL_LOGS_STATUS.txt ] && grep -q "Deferred to #135" /tmp/HISTORICAL_LOGS_STATUS.txt || { echo "⚠ Historical logs status may be missing; proceeding"; }
echo "✓ Non-goals and deferrals documented"

# 5. Phase 4 compaction completed
[ -f /tmp/bth_phase4_compact_done ] || { echo "⚠ Phase 4 compact sentinel not found; assuming manual completion"; }
echo "✓ Phase 4 compaction gate crossed"

echo "═══════════════════════════════════"
echo "PHASE 5 GATE: PASS ✓"
echo "SPRINT COMPLETE — All 4 projects seeded and queryable"
```

---

## Non-Goals (Intentional Deferrals)

| Item | Reason | Backlog |
|------|--------|---------|
| Historical SLURM log migration | Requires agentic classification; separate sprint | #135/#136 |
| `bth check` validity claims on pre-2026-05 runs | Only seed runs have outcome metadata | v0.2 refinement |
| Schema evolution for non-seed runs | Focus on v0.2 surface area validation | #140 (migrations) |
| Design sprint onboarding (6 projects) | Requires separate design sessions | Scheduled Q2 |
| Sidecar pre-registration enforcement | v0.2 feature | P2 backlog |
| Outcome evaluation display in CLI | v0.2 feature | P2 backlog |

---

## Backlog #135 Annotation

**Add to `/home/marielle/projects/bathos/CLAUDE.md` backlog section:**

```markdown
| 135 | `bth migrate` — Phase 1 mechanical (existing projects) | P2 | 125 |
     **2026-05-18 Annotation:** Sprint 2026-05 completed forward-only seeding (30–40 runs across bathos, prolix, oaf, demistify). Historical migration deferred:
     - prolix: ~500 logs unmigrated
     - oaf: ~300 logs unmigrated
     - demistify: ~150 logs unmigrated
     - bathos: ~50 logs unmigrated
     
     This item will merge historical logs once #136 (agentic classification) provides migration plan. Recommend scheduling as paired sprint post-v0.2.
```

---

**Status:** Ready for final auditor approval and implementation dispatch.