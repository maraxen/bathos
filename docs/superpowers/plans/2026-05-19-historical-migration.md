# Historical Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Initialize `.bth.toml` for prolix (missing), create experiment/benchmark sidecars for key scripts across prolix/oaf/demistify, and seed 30–40 forward runs in the catalog.

**Architecture:** Forward-seed only — no backfill of historical SLURM logs. Each project gets `bth init`, then sidecar files are created next to existing scripts, then `bth run` is called with real or dry-run invocations to populate the catalog. Depends on Plan A (sidecar enforcement must be in place).

**Tech Stack:** Python 3.12, bathos CLI (`bth`), TOML, uv

**Dependency:** `2026-05-19-v02-feature-wiring.md` must be complete before executing this plan.

---

## File Map

| Action | Path |
|--------|------|
| Create | `/home/marielle/projects/prolix/.bth.toml` |
| Create | `/home/marielle/projects/prolix/scripts/benchmarks/bench_nvt.bth.toml` |
| Create | `/home/marielle/projects/prolix/scripts/experiments/run_nvt_stability.bth.toml` |
| Create | `/home/marielle/projects/oaf/scripts/experiments/<existing_stem>.bth.toml` (per recon) |
| Create | `/home/marielle/projects/demistify/scripts/experiments/<existing_stem>.bth.toml` (per recon) |

> Note: exact script names in oaf/demistify must be confirmed by running `ls scripts/experiments/` in each project before writing sidecars. Steps below include that discovery step.

---

## Task 1: Verify bathos self-catalog is populated

**Files:** None (verification only)

- [ ] **Step 1.1: Check catalog has at least one run**

```bash
BTH_PROJECT_SLUG=bathos bth ls
```

Expected: rows showing bathos's own runs from Phase 5 integration. If empty, proceed to Step 1.2.

- [ ] **Step 1.2 (if empty): Seed a self-run**

```bash
cd /home/marielle/projects/bathos
BTH_PROJECT_SLUG=bathos bth run uv run pytest -x -q
```

Expected: tests run, run recorded in `~/.bth/catalog/`

- [ ] **Step 1.3: Verify**

```bash
BTH_PROJECT_SLUG=bathos bth ls -n 5
```

Expected: at least 1 row, STATUS=completed

---

## Task 2: Initialize prolix project

**Files:**
- Create: `/home/marielle/projects/prolix/.bth.toml`
- Create: `scripts/benchmarks/bench_nvt.bth.toml` (in prolix)
- Create: `scripts/experiments/run_nvt_stability.bth.toml` (in prolix, if script exists)

- [ ] **Step 2.1: Discover prolix script structure**

```bash
ls /home/marielle/projects/prolix/scripts/
ls /home/marielle/projects/prolix/scripts/benchmarks/ 2>/dev/null || echo "no benchmarks dir"
ls /home/marielle/projects/prolix/scripts/experiments/ 2>/dev/null || echo "no experiments dir"
```

Record actual directory and script names before writing sidecars.

- [ ] **Step 2.2: Run `bth init` for prolix**

```bash
cd /home/marielle/projects/prolix
bth init --slug prolix --remote engaging:~/projects/prolix
```

Expected: creates `/home/marielle/projects/prolix/.bth.toml`, output dirs, SLURM env helper.

- [ ] **Step 2.2a: Confirm script stems before writing sidecars**

Before running the heredocs below, verify that the scripts exist:
```bash
ls /home/marielle/projects/prolix/scripts/benchmarks/*.py 2>/dev/null | xargs -I{} basename {} .py
ls /home/marielle/projects/prolix/scripts/experiments/*.py 2>/dev/null | xargs -I{} basename {} .py
```
If the stems differ from `bench_nvt` / `run_nvt_stability`, update the heredoc filenames in Steps 2.3 and 2.4 before running them.

- [ ] **Step 2.3: Create benchmark sidecar (adjust script name from Step 2.2a if needed)**

Replace `bench_nvt` with the actual benchmark script stem confirmed in Step 2.2a.

```bash
cat > /home/marielle/projects/prolix/scripts/benchmarks/bench_nvt.bth.toml << 'EOF'
[benchmark]
baseline_ref = ""
metric = "ns_per_day"
regression_threshold = 0.05
target = "> 50 ns/day on pi_so3"

[result_schema]
ns_per_day = "float"
system = "str"
n_atoms = "int"
EOF
```

- [ ] **Step 2.4: Create experiment sidecar (adjust script name from Step 2.1)**

```bash
cat > /home/marielle/projects/prolix/scripts/experiments/run_nvt_stability.bth.toml << 'EOF'
[experiment]
hypothesis = "NVT with dt=0.5fs maintains ±5K temperature stability over 50ps"

[outcomes.pass]
condition = "temp_std < 5"
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
EOF
```

- [ ] **Step 2.5: Seed a prolix run**

```bash
cd /home/marielle/projects/prolix
BTH_PROJECT_SLUG=prolix bth run uv run pytest -x -q 2>/dev/null || \
BTH_PROJECT_SLUG=prolix bth run echo "prolix-seed-$(date +%s)"
```

Expected: run recorded, exit 0

- [ ] **Step 2.6: Verify prolix appears in catalog**

```bash
bth ls --project prolix
```

Expected: at least 1 row for project=prolix

- [ ] **Step 2.7: Commit prolix sidecar files**

```bash
cd /home/marielle/projects/prolix
git add .bth.toml scripts/
git commit -m "chore(bathos): add .bth.toml init and experiment/benchmark sidecars"
```

---

## Task 3: Seed oaf project

**Files:**
- Create: sidecar files adjacent to oaf's existing experiment scripts

- [ ] **Step 3.1: Discover oaf script structure**

```bash
ls /home/marielle/projects/oaf/scripts/experiments/ 2>/dev/null
ls /home/marielle/projects/oaf/scripts/benchmarks/ 2>/dev/null
cat /home/marielle/projects/oaf/.bth.toml
```

- [ ] **Step 3.1a: Confirm oaf script stems before writing sidecars**

```bash
ls /home/marielle/projects/oaf/scripts/experiments/*.py 2>/dev/null | xargs -I{} basename {} .py
```

Use the actual stems from this output as filenames in Step 3.2, not placeholder names.

- [ ] **Step 3.2: Create sidecars for each experiment script found**

For each `<stem>.py` found in `scripts/experiments/`, create `<stem>.bth.toml`:

```bash
# Example — repeat for each script found in Step 3.1
cat > /home/marielle/projects/oaf/scripts/experiments/<stem>.bth.toml << 'EOF'
[experiment]
hypothesis = "<fill in from script docstring or README>"

[outcomes.pass]
condition = "success = true"
decision = "results accepted"

[outcomes.fail]
condition = "success = false"
decision = "investigate and re-run"

[result_schema]
success = "bool"
EOF
```

- [ ] **Step 3.3: Seed 3 oaf runs**

```bash
cd /home/marielle/projects/oaf
for i in 1 2 3; do
  BTH_PROJECT_SLUG=oaf bth run echo "oaf-seed-$i"
done
```

Expected: 3 rows appear in catalog for oaf

- [ ] **Step 3.4: Verify**

```bash
bth ls --project oaf
```

Expected: 3 rows, all STATUS=completed

- [ ] **Step 3.5: Commit**

```bash
cd /home/marielle/projects/oaf
git add scripts/
git commit -m "chore(bathos): add experiment sidecars"
```

---

## Task 4: Seed demistify project

**Files:**
- Create: sidecar files adjacent to demistify's existing experiment scripts

- [ ] **Step 4.1: Discover demistify script structure**

```bash
ls /home/marielle/projects/demistify/scripts/experiments/ 2>/dev/null
cat /home/marielle/projects/demistify/.bth.toml
```

- [ ] **Step 4.1a: Confirm demistify script stems before writing sidecars**

```bash
ls /home/marielle/projects/demistify/scripts/experiments/*.py 2>/dev/null | xargs -I{} basename {} .py
```

Use the actual stems from this output in Step 4.2.

- [ ] **Step 4.2: Create sidecars for each experiment script**

Same pattern as Task 3 Step 3.2 — for each `<stem>.py` in `scripts/experiments/`:

```bash
cat > /home/marielle/projects/demistify/scripts/experiments/<stem>.bth.toml << 'EOF'
[experiment]
hypothesis = "<fill in from script or README>"

[outcomes.pass]
condition = "error_rate < 0.05"
decision = "ship to next stage"

[outcomes.fail]
condition = "error_rate >= 0.05"
decision = "investigate and re-run"

[result_schema]
error_rate = "float"
n_samples = "int"
EOF
```

- [ ] **Step 4.3: Seed 3 demistify runs**

```bash
cd /home/marielle/projects/demistify
for i in 1 2 3; do
  BTH_PROJECT_SLUG=demistify bth run echo "demistify-seed-$i"
done
```

- [ ] **Step 4.4: Verify**

```bash
bth ls --project demistify
```

Expected: 3 rows

- [ ] **Step 4.5: Commit**

```bash
cd /home/marielle/projects/demistify
git add scripts/
git commit -m "chore(bathos): add experiment sidecars"
```

---

## Task 5: Cross-project verification and compaction

- [ ] **Step 5.1: Cross-project query**

```bash
bth ls -n 50
```

Expected: runs from bathos, prolix, oaf, demistify all visible

- [ ] **Step 5.1a: Check for stale warm DB before compacting**

```bash
bth sql "PRAGMA table_info(runs)" 2>/dev/null || echo "no warm DB yet"
```

If a warm DB exists, confirm `outcome` and `output_metadata` columns are present in the output. If they are missing (schema from a prior sprint), delete the stale DB first:
```bash
rm -i ~/.bth/catalog/bathos.db
```

- [ ] **Step 5.2: Compact to warm tier**

```bash
bth compact
```

Expected: `Compacted N runs into ~/.bth/catalog/bathos.db`

- [ ] **Step 5.3: SQL cross-project query**

```bash
bth sql "SELECT project_slug, COUNT(*) as n, SUM(CASE WHEN status='completed' THEN 1 ELSE 0 END) as ok FROM runs GROUP BY project_slug ORDER BY project_slug"
```

Expected: rows for each of the 4 projects with n ≥ 1

- [ ] **Step 5.4: Verify total run count ≥ 10**

```bash
bth sql "SELECT COUNT(*) FROM runs"
```

Expected: count ≥ 10
