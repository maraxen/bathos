# bathos Pre-Registration & Nonrepudiation Layer — Design Recommendations

*Synthesized from NotebookLM research queries (2026-05-20). Source notebook: 3f0490aa-7c70-4cd5-bfe7-94c3bd4a041f (~144 sources on agentic science and pre-registration)*

---

## Key Findings Summary

- Basic pre-registration has near-zero effect on p-hacking. What works is the three-component stack: **forced articulation** (specific predictions in writing) + **temporal lock** (commit before seeing results) + **immutable link** (cryptographic hash binding execution to plan). bathos already has the articulation layer; the lock and link are missing.
- No existing single-researcher tool bridges computational provenance and declared hypothesis. This is the niche bathos can own — `bth run` already captures the provenance side; the sidecar captures the declaration side; cryptographic binding is the missing bridge.
- Implementation drift — silently modifying the analysis without updating the plan — affects human researchers as much as AI agents. The fix is not blocking modification but making it auditable: amendment creates a new version with a timestamp and reason, not an overwrite.
- The existing `[outcomes]` DuckDB SQL conditions in the sidecar are already at the correct granularity for a complete pre-analysis plan. The missing pieces are enforcement (gate) and evaluation (stored verdict), not more articulation structure.

---

## Design Recommendations

### 1. Sidecar SHA-256 hash embedded in every run record

**What to build:** At `bth run` start, read the sidecar file, compute `sha256(sidecar_bytes)`, and store it as `sidecar_sha256` in the cool-tier Parquet record. Add `sidecar_path` as well (resolved real path, symlink-safe). No sidecar present means `sidecar_sha256 = NULL`.

**Why it changes behavior:** Without this, a researcher can modify the sidecar after a run and the catalog has no record of what was declared at execution time. The hash is the temporal lock — it makes "I always planned to check temp_std" checkable against the run record. Future self can run `bth show <run_id>` and see exactly what hypothesis was active when this data was produced.

**Implementation notes:** Add two columns to `RUN_SCHEMA` in `schema.py`: `sidecar_sha256 pa.string()` (nullable) and `sidecar_path pa.string()` (nullable). Compute in `runner.py` before subprocess launch. Also store the full parsed sidecar content as JSON in the warm-tier `metadata` column during compaction — this means the declaration survives even if the sidecar file is later renamed or deleted.

---

### 2. Gate enforcement in `bth run` for schema-enforced directories

**What to build:** In `runner.py`, before launching the subprocess, detect whether the script lives in `scripts/experiments/` or `scripts/benchmarks/`. If yes: require a sidecar present and validate it contains `[experiment]` (or `[benchmark]`), at least one `[outcomes.*]` block with a non-empty `condition`, and a non-empty `[result_schema]`. If validation fails, print a structured error naming the missing section and exit non-zero without running the script.

**Why it changes behavior:** High-level registration ("I will run a simulation") has near-zero effect. The `condition` field in `[outcomes]` is the confirmatory test specification — requiring it before execution is the mechanism that changes behavior. The directory scoping is critical: `scripts/explore/` and `scripts/scratch/` must remain ungated, or the tool becomes friction on legitimate exploratory work and gets abandoned.

**Implementation notes:** The validation logic belongs in `sidecar.py` as `validate_sidecar(path: Path) -> list[str]` returning a list of error strings (empty = valid). Keep it a pure function with no side effects — easier to test and reusable by `bth check`. Emit a warning (not an error) in v0.1 if sidecar is missing; enforce in v0.2. Do not gate `scripts/debug/` — debug scripts by definition have incomplete knowledge of the outcome.

**Override path:** `bth run --no-sidecar` as an explicit escape hatch that logs a `BYPASSED` flag in the run record. Protects against tool abandonment while preserving the audit trail.

---

### 3. Outcome verdict evaluation and storage

**What to build:** At run completion (after the subprocess exits with code 0), evaluate each `[outcomes.*]` condition against the result values the script emitted. Store the matched label (`pass`, `marginal`, `fail`, or `no_match`) as `outcome` in the run record. The evaluation uses DuckDB: create a single-row in-memory relation from the result JSON, run `SELECT CASE WHEN (condition) THEN 'label'...` in priority order. Never recompute at query time — store the label from the moment of evaluation.

**Why it changes behavior:** This closes the loop. The researcher declared "pass means temp_std < 5" before running; the run record now says "this run evaluated to marginal." The catalog becomes an audit trail of prediction accuracy, not just execution history. `bth ls --outcome=fail` becomes a meaningful query.

**Implementation notes:** Depends on warm-tier schema (backlog #139) for the `outcome` column. Can be prototyped in cool-tier Parquet first (add `outcome pa.string()` as nullable). The script needs a way to emit result values — see Open Question Q1 below.

---

### 4. Amendment workflow

**What to build:** `bth amend <run_id> --reason "text"` logs an amendment record to a new `amendments` table (warm tier) with columns: `run_id`, `amended_at`, `old_sidecar_sha256`, `new_sidecar_sha256`, `reason`. This command does NOT modify the run record — it appends to the amendment log. `bth show <run_id>` displays amendments inline.

**Why it changes behavior:** If modifying the sidecar mid-experiment overwrites the original with no trace, the hash loses its value. But if the only option is "never change the hypothesis," researchers will abandon the tool for anything iterative. The amendment log makes drift visible without making it punitive — the researcher can see "I changed the hypothesis twice before this run" and decide what to report. The friction of writing a reason string is the same forced articulation mechanism that makes PAPs effective.

**Implementation notes:** Keep the `amendments` table simple (no foreign key enforcement needed — `run_id` is a string UUID). `bth amend` should check that the current sidecar SHA differs from `run_id`'s recorded SHA before logging — otherwise it's a no-op. This is a v0.2/P3 feature; don't block sidecar locking on it.

---

### 5. What NOT to build

Skip: social sharing, OSF integration, team accountability dashboards, public pre-registration links, email notifications on outcome mismatch, gamification.

The research is explicit: OSF-style pre-reg without reviewer verification is easily gamed and has near-zero behavioral effect for solo researchers. The value of bathos is the researcher's audit trail with their future self, not external accountability. Every feature that adds complexity without tightening the articulation-lock-binding loop is overhead that accelerates tool abandonment.

Also skip: outcome condition DSLs, custom expression evaluators, probabilistic outcome matching. DuckDB SQL fragments already handle this and the decision to use them is locked in the design.

---

## Recommended Implementation Order

| Priority | Item | Depends on | Backlog status |
|---|---|---|---|
| 1 | `sidecar_sha256` + `sidecar_path` fields in cool-tier Parquet schema | #124 (done) | New P1 — add to backlog |
| 2 | `validate_sidecar()` in `sidecar.py` — completeness check for gate | above | New P1 — add to backlog |
| 3 | Gate enforcement in `bth run` for experiments/ and benchmarks/ | #126 (done), validate_sidecar | New P2, v0.2 gate |
| 4 | Result-emission protocol (how script returns result values to bth) | design decision Q1 | New P2, blocks verdict eval |
| 5 | Outcome verdict evaluation + `outcome` column in warm tier | #139, result-emission | New P2, depends on #139 |

Amendment workflow is P3 — implement after the lock is solid.

---

## Open Questions Requiring Design Decisions

**Q1: How does the script emit result values to `bth run`?**

The outcome verdict evaluator needs `{temp_mean: 1.2, temp_std: 3.4}` from the actual run. Three options:

- **(a)** Structured stdout line: `#bth-result: {"temp_mean": 1.2}`
- **(b)** Results JSON file written to a conventional path: `<script-stem>.bth-results.json`
- **(c)** `@bth.experiment` decorator (backlog #129) captures return values

Option (b) is the most language-agnostic and consistent with the sidecar-as-companion-file pattern already established. Decision needed before verdict evaluation can be implemented.

**Q2: Hard block vs. confirmation prompt for missing sidecar in v0.2?**

Currently: warn but don't block (v0.1). The research says enforcement is what changes behavior. The directory scoping (only experiments/ and benchmarks/) limits collateral friction. Recommendation: hard block in v0.2 with `bth run --no-sidecar` as an explicit override that logs a `BYPASSED` flag in the run record.

**Q3: Separate `declarations` table or inline `metadata` column for sidecar content in warm tier?**

The CLAUDE.md already mentions "content-addressed declarations table keyed by SHA256." If multiple runs reference the same sidecar (same SHA), a declarations table deduplicates storage but adds a join to `bth show`. For a solo researcher with hundreds of runs (not millions), the declarations table is architecturally cleaner. Decision needed before warm-tier schema is finalized (backlog #139/#140).

---

*Research basis: 4 NotebookLM queries across ~144 sources on pre-registration enforcement, scientific workflow provenance, AI scientist implementation drift, and pre-registration granularity.*
