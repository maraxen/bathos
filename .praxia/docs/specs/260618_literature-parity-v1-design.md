---
title: Literature-Parity v1 — Technical Design + Child-Task DAG (post-adversarial-review)
category: specs
created: 260618
task_id: 260618_literature-parity-epic
status: reconciled (ready for register checkpoint)
source_spec: .praxia/docs/specs/260618_abstraction-boundary-open-design-questio.md
seed: .praxia/docs/research/260617_literature-parity-workflow-seed.md
reviews: spec-challenger + spec-defender (both Opus) — 2 BLOCKER + 5 MAJOR reconciled below
---

# Literature-Parity v1 — Technical Design (reconciled)

## Epic

**Literature-Parity v1 — Reusable Cross-Project Baseline Validation Workflow.**
When a project reimplements a method from a paper that publishes no code, the
reimplementation silently diverges from the published method, confounding any
downstream comparison (bathos confound C7 / `[confounds.reference_parity]`). Ships a
reusable, claim-tier-integrated workflow: a skill-first protocol (5-phase blind
reconstruction -> reconcile -> adversarial refutation -> adjudicate -> graded verdict)
driven by a declarative `parity.bth.toml` (zero per-project code); a mechanical X1
cap-lattice grader (PARITY/PARTIAL/FAIL, no adjudication); F2 conclude-gate + F3
submit-gate enforcement; F4 `attest-parity` verb + sprint-audit Signal 13. Validated on
the Zeinaty 2026 case (asr) where it caught a mechanism-nullifying defect 3 sprints of
unit tests missed.

## Locked architecture (from brainstorm winner — not re-litigated)

A2 thin-config seam · 5-phase skeleton, C1 3+3 floor + C4 escalation (N/M skill-tunable)
· D2 evidence channels, D3 manifest-declared, D1 numeric channel -> /jax-port (optional)
· H2 disagreement detector -> H1 PARTIAL cap · E1 rung ladder R0-R4 + E3 honesty-tax ·
B2 agent-writes / orchestrator-re-derives over B1 floor · X1 cap-lattice grade · G1
SHA-anchored triple, G3 placement · F2 + F3 + F4 enforcement.
**v1 code = X1 grader + F2/F3 gates + G1 SHA-anchor + parity.bth.toml schema ONLY.**
Deferred graduation paths: A4 callbacks, X2 library, F1 `bth parity` subcommands, D4, E2,
G2, H3.

## Reconciliation log (adversarial review → resolutions)

- **B1 [BLOCKER] phantom `parity_run_type`.** No schema home; cool schema is intentionally
  minimal (v9 migration out of scope). **RESOLVED:** `parity_run_type='literature_parity'`
  is written into the existing **`metadata` JSON** blob, not a new column. F2/AC-12/AC-19
  query `json_extract(metadata, '$.parity_run_type')`. `validate_claim` already reads
  `metadata` JSON at `claim.py:220`, so the read path exists.
- **B2/R6 [BLOCKER] `bth submit` has no `--campaign`.** **RESOLVED (sidecar-driven, mirrors
  `requires_pass_stem`):** the experiment sidecar declares a parity prerequisite
  (`requires_parity_stem` or `[reproduction].requires_parity`); F3 resolves it from
  `sidecar_data` exactly like the reproduction gate at `prereg.py:178` — NO `--campaign`
  arg, NO new CLI surface. F3 never needs a campaign at submit time.
- **F-1 [MAJOR] F2 vs legacy AC-13 equivalence check.** The existing AC-13 block
  (`claim.py:182-254`) does numeric value-equivalence (`reference_value`/`equivalence_bound`).
  **RESOLVED:** the new graded-parity-run check runs **beside** it. For a `reference_parity`
  confound that carries a `parity_run_id`, the graded run's `outcome` + metadata
  `parity_run_type` is authoritative; the legacy equivalence-bound path is retained for
  confounds that use it (not deprecated). Both can fire; a confound is `controlled` iff its
  applicable path passes.
- **F-2 [MAJOR] / R5 scaffold contradiction.** **RESOLVED (R5 option a — infer from DB):**
  `scaffold_claim` adds only `parity_run_id = ""` (empty) under `[confounds.reference_parity]`.
  It does **NOT** write a `parity_status` field — status is always inferred from the live run
  record, never read from the TOML (no drift surface).
- **F-3 [MAJOR] W3 write-collisions.** T6+T8 both touch `claim.py`; T7+T8 both touch `cli.py`.
  **RESOLVED:** re-serialized waves (see DAG) — T6 ∥ T7, then T8 after both; T9 after T5+T6.
- **R2 [MAJOR/high] attest atomicity undesigned.** **RESOLVED:** `attest_parity()` uses
  write-temp-then-rename for the claim file and updates the warm DB SHA **last** (matching the
  catalog's documented atomic write-then-rename). On any failure before rename, the original
  file + DB row are untouched; `check_sha` (`claim.py:444`) never sees divergence. Covered by
  AC-21.
- **F-4 [MAJOR] missing ACs.** Added AC-20 (SHA-drift detection), AC-21 (attest mid-mutation
  rollback), AC-22 (F3 warm-absent fail-open advisory).
- **F-5 [MINOR] MCP anchor.** `claim_attest_parity` registers after `claim_validate`
  (`mcp.py:1117`), not 1170; drop the claim-register-mirror framing (no such tool).
- **F-6 [MINOR] naming.** Canonical key is `reference_parity`; the `[baseline_parity]` comment
  at `claim.py:182` is a pre-existing mislabel — fix the comment as a one-liner in T6.
- **N-1 [NIT] validation sidecar kind.** T1 confirms whether `parity_validate.bth.toml` reuses
  an existing sidecar kind or introduces one; prefer reuse to avoid touching `sidecar.py`.
- **N-2 [NIT] Constraint 1.** The orchestrator-owned re-derivation lock is **skill-enforced
  only** in v1 (no code gate) — stated explicitly so reviewers don't expect one.

## Component design (anchors verified by both reviewers)

- **`src/bathos/parity.py` (new)** — `parse_parity_toml()` + validation; `ParityEvidence`,
  `ParityGradeResult`, `compute_grade()` (X1 cap-lattice), `evidence_from_result()`;
  `check_parity_confounds_for_submit()` (warm+cool fallback, fail-open advisory when warm
  absent — mirrors `prereg.py:178`).
- **`parity.bth.toml` schema** — `paper_pdf` (str, req), `impl_paths` (list, req),
  `reference_code` (str|null; D3, no auto-sniff), `citation_note`, `recon_lenses`
  (default math/algo/protocol), `attack_lenses` (default stats/hyper/struct), `hypotheses`,
  `equivalence_bound` (float), `N`/`M` (int, default 3/3).
- **X1 cap-lattice ceiling table** — grade = min over ceilings: invariant_pass=False -> FAIL;
  clause-parity% low -> caps; adversarial-survival fail -> caps; reproduction_rung worse than
  R1 (R2/R3/R4) -> max PARTIAL; ambiguity_load=load_bearing -> max PARTIAL. Computed from the
  artifact, no human adjudication.
- **F2 conclude-gate** — `parity_confound_check()` inserted in `campaigns.py:conclude_campaign()`
  before `run_union_gate()` (line 242); the AC-13 block in `validate_claim()` (`claim.py:182`)
  gains the beside-the-legacy graded-run check (B1 metadata query). confirmation/sequential ->
  downgrade to `confounded` on uncontrolled; exploration -> warn.
- **F3 submit-gate** — parity confound gate in `bth submit` after the reproduction gate
  (`cli.py:~999`); reads the sidecar-declared parity prerequisite (B2); hard-block
  validation/production, advisory exploration/calibration + warm-absent.
- **F4 attest-parity + MCP** — `attest_parity()` (atomic, R2) + `parity_confound_check()` in
  `claim.py`; `bth campaign attest-parity` in `campaign_app` (`cli.py:19`); `claim_attest_parity`
  MCP tool after `claim_validate` (`mcp.py:1117`). Re-anchors the claim SHA atomically.
  `scaffold_claim` (`claim.py:286/346`) adds `parity_run_id=""` only.
- **Signal 13** — in `sprint_audit.py:sprint_audit()` after Signal 12 (line 526): flag a
  confirmation campaign citing a published-method baseline with uncontrolled reference_parity /
  empty parity_run_id / cited run not a parity run.
- **G1 + G3** — SHA-anchored triple (checklist JSON + verdict md + invariant pytest) pinned into
  the parity run's `output_paths` (`schema.py`); verdict ->
  `.praxia/docs/audits/YYMMDD_<paper>-parity-verdict.md`, machine artifacts -> catalog,
  cross-linked by run_id; drift detectable via `bth check` + SHA (AC-20).
- **Skill** — `## Validating a reimplemented baseline` in `agent_assets/skills/using-bathos/SKILL.md`
  + `using-bathos/literature-parity/` subdir (phase templates, parity.bth.toml.template, example).

## Child-task DAG (reconciled)

| Task | Scope | Difficulty | Depends on | workflow_hint |
|---|---|---|---|---|
| T1 | parity.bth.toml schema + validator in parity.py; confirm validation sidecar kind (N-1) | quick | — | tdd |
| T2 | X1 cap-lattice grader (ParityEvidence/ParityGradeResult/compute_grade/evidence_from_result) | standard | — | tdd |
| T3 | Skill section + `literature-parity/` helper-scripts subdir; state Constraint-1 skill-enforced (N-2) | standard | — | docs |
| T4 | scripts/validation/parity_validate.py + companion sidecar; writes metadata.parity_run_type (B1) | standard | T1, T2 | tdd |
| T5 | attest_parity() ATOMIC (R2) + parity_confound_check() in claim.py; scaffold adds parity_run_id only (F-2) | standard | T1, T2 | tdd |
| T6 | F2 conclude-gate: campaigns.py:242 + validate_claim AC-13 beside-legacy (F-1, B1 metadata query); fix [baseline_parity] comment (F-6) | standard | T5 | tdd |
| T7 | F3 submit-gate: cli.py + check_parity_confounds_for_submit; sidecar-declared prereq (B2), warm-absent fail-open | standard | T5 | tdd |
| T8 | F4 CLI attest-parity + MCP claim_attest_parity (mcp.py:1117, F-5) | standard | T5, T6, T7 | tdd |
| T9 | Signal 13 + sprint-audit integration | quick | T5, T6 | tdd |

**Parallel waves (collision-free):**
- W1 = {T1, T2, T3}
- W2 = {T4, T5}  (after T1+T2)
- W3 = {T6, T7}  (after T5; T6=claim.py+campaigns.py, T7=cli.py — disjoint files)
- W4 = {T8, T9}  (T8 after T6+T7 since it edits both claim.py & cli.py; T9 after T5+T6)

Critical path: T2 -> T5 -> T6 -> T8. T3 is dependency-free (floats).

## Epic-level acceptance criteria (22)

AC-01 validator rejects missing required fields · AC-02 validator passes valid file ·
AC-03 compute_grade -> FAIL when invariant_pass=False · AC-04 -> PARTIAL cap for
load-bearing ambiguity · AC-05 -> PARITY on all-clear · AC-06 F2 downgrades confirmation
w/ uncontrolled reference_parity · AC-07 F2 warns-only for exploration · AC-08 F2 passes
when controlled · AC-09 F3 hard-blocks validation/production · AC-10 F3 advisory for
exploration · AC-11 attest-parity binds passing run + re-anchors SHA · AC-12 attest-parity
rejects run missing metadata.parity_run_type · AC-13 attest-parity on partial ->
controlled-by-protocol · AC-14 Signal 13 flags uncontrolled reference_parity · AC-15
`bth run --out` registers the SHA-anchored triple in output_paths · AC-16 SKILL.md has the
section · AC-17 subdir has all templates/examples · AC-18 compute_grade -> PARTIAL cap for
rung R2 · AC-19 parity_validate.py writes metadata.parity_run_type='literature_parity' ·
**AC-20 `bth check` detects a drifted parity verdict via SHA mismatch** · **AC-21
attest_parity mid-mutation failure leaves file-SHA == DB-SHA (atomic rollback)** · **AC-22
F3 fails open (advisory) when the warm DB is absent at submit**.

## Remaining notes for implementation

- Constraint 1 (re-derivation lock) is skill-enforced only in v1 (N-2).
- T1 should reuse an existing sidecar kind for the validation companion if possible (N-1).

---

## B1 Resolution (post-W3-audit) — RECONCILED

`task_id: 260618_lit-parity-b1-redesign` · **supersedes/OVERRIDES the B1 reconciliation-log entry** (`parity_run_type` no longer lives in `metadata` JSON). Reconciles staff design + Opus spec-challenger (`260618_lit-parity-b1-review_spec-challenger`, NEEDS_WORK) + Opus spec-defender (NEEDS_WORK). Status: **APPROVED-PENDING-PI-GO-AHEAD**. No code written yet.

### Decision
Adopt **R-A**: promote `parity_run_type` to a first-class **COOL column** (schema **v9**). R-B (keep metadata JSON + fix write path) and R-C (metadata in cool) both rejected Risk-HIGH (verified by both reviewers). Both reviewers tried and FAILED to break the column-promotion premise.

### Exemplar correction (defender D1, challenger B-1) — load-bearing
The note's "clone the `claim_isolates` triple, Risk LOW" framing is **WRONG**. `claim_discriminates`/`claim_isolates` are **NULL-on-ingest today**: absent from the compact INSERT column list/VALUES/params (`compact.py:625-673`), so every locally-compacted run writes them NULL regardless of the cool fragment value — the *exact* bug class B1 exists to fix. The **correct exemplar is `stage_name`**, which is complete end-to-end: `runner.py` write → `compact.py:631/672` INSERT → `:271` table schema → `:483` ALTER → `:201` migrate. Re-anchor every clone on `stage_name`.

### P0 change set (reconciled — all BLOCKER/MAJOR folded)

**1. `schema.py`** (all anchors verified): COOL_SCHEMA `pa.field("parity_run_type", pa.string(), nullable=True)` after :71; WARM_SCHEMA same after :121; `Run` dataclass `parity_run_type: str | None = None` after :172; `to_arrow()` `"parity_run_type": [self.parity_run_type]` after :219; `from_arrow_row()` `parity_run_type=pydict.get("parity_run_type",[None])[i] if "parity_run_type" in pydict else None` after :277; bump `CURRENT_SCHEMA_VERSION = "9"` (:10).

**2. `compact.py` — INSERT fix is P0 (challenger B-1, defender D1):** add `parity_run_type` AND the two NULL-on-ingest siblings `claim_discriminates`, `claim_isolates` to the INSERT column list (:625-631), add 3 `?` to VALUES (:632), append `run.claim_discriminates, run.claim_isolates, run.parity_run_type` to params (after :672). Plus: `parity_run_type TEXT` in `_RUNS_TABLE_SCHEMA` (after :271); warm ALTER `ADD COLUMN IF NOT EXISTS parity_run_type TEXT` (after :485); `_migrate_v8` (clone `_migrate_v7` :206-215) sets `run_dict["parity_run_type"]=None, schema_version="9"`; register `MIGRATIONS["8"]=_migrate_v8` (after :225). **Required test:** runner→compact→warm round-trip asserting all THREE columns survive non-null.

**3. `migrate.py` (challenger M-2, defender D2):** add `parity_run_type` to the `_default_array` NULL-allowlist tuple at `:153` (else old runs backfill `""` not NULL). No per-version edit otherwise — `migrate_catalog` is schema-driven (verified).

**4. Write path (challenger B-2) — exact extraction, no hand-wave:** `parity_validate.py:215-221` emits `parity_run_type` **doubly nested** (`result["metadata"]["parity_run_type"]`); `runner.py:381` stores the whole emission JSON string in `Run.metadata`. So at `runner.py:439-450`: `parity_run_type = (json.loads(metadata) or {}).get("metadata", {}).get("parity_run_type")` → `dataclasses.replace(run, ..., parity_run_type=parity_run_type)`. Column is authoritative; keep the metadata copy for readability. **Required test:** run `parity_validate.py` end-to-end through the runner, assert column populated pre-compaction.

**5. F-1 `validate_claim` graded check — IMPLEMENT (was never built; only a comment changed in W3):** beside-legacy at `claim.py:182-254`, query `SELECT outcome, parity_run_type FROM runs WHERE id=?`; controlled iff `parity_run_type='literature_parity'` AND `outcome IN ('pass','partial')` (partial → controlled-by-protocol).

**6. claim.py metadata-read sites (challenger M-1) — DO NOT blanket-switch:** switch to `parity_run_type` column at `:595` (attest_parity), `:785` (infer-status), F2 `campaigns.parity_confound_check`, F3 `parity.py`. **PRESERVE `claim.py:205` (AC-13)** — it reads a numeric `reference_metric` from metadata JSON, a legitimate metadata use; switching it breaks AC-13.

**7. F2 conclude-gate (`campaigns.py`):** replace `json_extract(metadata,'$.parity_run_type')` with `parity_run_type = 'literature_parity'`.

**8. F3 submit-gate + AC-22 guard (challenger B-3) — try/except restructure:** warm query gets `AND parity_run_type='literature_parity'`. Cool fallback (`parity.py:282-306`) read `columns=["command","outcome","parity_run_type"]` (was the non-existent `metadata` cool column → spurious hard-block). Track `fragments_read_ok`; after loop: `fragments_read_ok == 0` → `return {satisfied:None, tier_enforced:False}` (unsearchable → fail-open per AC-22); ≥1 read OK with no match → `satisfied:False, tier_enforced:is_validation_or_production`. The present inner `except…continue` cannot distinguish these — restructure required.

**9. Test fixtures (challenger M-2/M-3):** bump ~15 hard-coded `"8"` asserts to `"9"` (`test_compact.py:154,169,253,268,282,466,501,525,540`; `test_schema.py:46,87,103,236,353,427` + stale :424 docstring; `test_migrate.py:142` docstring); extend migration-chain test (`test_compact.py:140-154`, comment :150) v0→…→v8→**v9**; DELETE the W3 `UPDATE runs SET metadata=...` post-compaction fixture hacks, replace with runs that set `parity_run_type` through the runner path.

**10. Harden tautological tests (W3 audit M1/M2):** AC-07 (exploration advisory) + AC-08 (controlled no-downgrade) currently pass even if the F2 gate were deleted. Add capsys/mock assertions that the gate code path was reached (advisory WARNING emitted / `parity_confound_check` returned `status='controlled'`).

**11. AC-20 — DEFER to T8 (both reviewers concur, no live hole):** remove the bare-`pass` stub from W3; AC-20 (verdict-artifact SHA-drift via `bth check`) is an orthogonal new feature, unshipped, AC-21 atomic-rollback already covers mid-mutation. Mark AC-20 status `deferred-T8` in the epic; do not claim W3 coverage.

### Back-compat (defended, §9): idempotent ALTER; NULL=non-parity (gates already treat NULL correctly); no retro-classification needed (no shipped parity runs); cold/archive lacks column → `if "parity_run_type" in pydict` guard handles gracefully.

### Sibling-bug follow-up
The `claim_discriminates`/`claim_isolates` NULL-on-ingest fix is FOLDED into step 2 (P0). A separate follow-up tracks verifying downstream impact on the Phase-2b claim-tier discriminability lints (AC-04/05/06), which read these columns from warm and would have seen NULL.
