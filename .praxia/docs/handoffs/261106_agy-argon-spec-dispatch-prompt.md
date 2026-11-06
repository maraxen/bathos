---
title: "agy argon dispatch prompt: result validity / supersession / archive spec"
description: "Paste-ready /goal /teamwork /boost prompt for Antigravity (Gemini Argon) to run recon, prior-art research, specification and adversarial spec-review cycles for task 261106_archive-supersession, starting from the approved design doc."
task_id: 261106_archive-supersession
date: "2026-11-06"
status: READY
---

Paste everything inside the fence into agy.

```
/goal /teamwork /boost

task_id: 261106_archive-supersession
Repo: /home/marielle/projects/bathos (work in a new git worktree branched from
origin/wt/261106_archive-supersession, which carries the design doc; new branch
spec/261106_result-validity; PR base: main). Read-only research repos: /home/marielle/projects/asr,
/home/marielle/projects/tev_design. Do NOT modify asr or tev_design, and do NOT write to ~/.bth.

GOAL
Turn the approved design into an implementable, adversarially-reviewed specification for bathos.
Input design (owner-approved decisions, read it first and treat its section-4 decisions as fixed):
  .praxia/docs/specs/261106_result-validity-supersession-archive-design.md
The problem: results produced by defective or since-changed instruments (chiefly the aminx MPNN
wrapper) keep resurfacing as current in asr and tev_design, agents over-trust old readings, and
work circles because nothing records the current belief per question. bathos records blast-radius
flags and stale scripts but no read/cite/claim/graduate path consults them.

PHASES (run in order; each phase ends with a written artifact)
1. Code recon. Re-verify every row tagged "unverified" in the design's section 3 by reading code,
   and map each surface in D3's in-scope list to the exact function and file:line where the
   validity check must be inserted (ls, find, get_run/show, run_sql, cite, claim register/validate,
   campaign add/conclude/review, resolve_pin, get_trust_state, graduate_product, lineage, the
   maraxiom review_build bridge, the new SessionStart hook). Find out why `dependency_provenance`
   is null on every run in asr/tev_design (runner/dependency_provenance.py). Inventory the reusable
   machinery: blast_radius.py anchors + ledger + propagate, artifact_archive.py, archived_items.py,
   sidecar [status], epistemic classes, BathosErrorCode registry, migrate.py schema versioning.
   Output: .praxia/docs/research/261106_validity-surfaces-recon.md (every claim cited path:line).
2. Prior art. Research how others handle result invalidation, supersession and knowledge
   currency, and extract what to adopt or avoid: DVC/MLflow/W&B (run deprecation, lineage),
   Datalad/git-annex, provenance standards (W3C PROV invalidation, "wasInvalidatedBy"), Bazel/Nix
   style input-addressed invalidation, retraction/erratum practice (Crossref/Retraction Watch,
   CRediT), living systematic reviews and evidence-grading (GRADE certainty levels), lab ELN
   practices, software CVE/advisory ranges (OSV schema: affected version ranges + commit ranges
   — directly relevant to D1), and ADR "superseded by" conventions. Also read the in-house
   prototypes this must replace rather than duplicate: tev_design scripts/audit/claims_ledger.toml,
   scripts/audit/check_ledger_instrument_staleness.py, wheels/AMINX_PIN.md; asr .verdicts/ and
   .praxia/docs/experiments/INDEX.md; praxia docs registry
   (/home/marielle/projects/praxia/crates/praxia-tools/src/docs/default_registry.toml) for the
   superseded/archived doc vocabulary. Output: .praxia/docs/research/261106_validity-prior-art.md.
3. Specification. Write .praxia/docs/specs/261106_result-validity-supersession-archive.md, one
   section per phase (P1 stop the leak; P2 findings + tiers; P3 archive sweep), each with:
   data model + schema migration (new nullable fields only), CLI + MCP signatures (tool-for-tool
   parity), refusal codes + resolution hints, exact insertion points from phase 1, acceptance
   criteria as testable statements, test plan (including the invariant "hiding is read-time
   only; no ledger row is mutated or deleted"), fixer task decomposition with dependencies,
   risks. Settle every item in the design's section 7 with a stated rationale. Any threshold
   (sweep age N, session-start byte cap, telemetry rotation) needs a cited basis or an explicit
   "arbitrary, revisit after P1 dry-run counts" label. Keep the design's distinctions:
   defect vs estimand_change; void/affected/superseded/archived hide, suspect/unverifiable banner;
   version_source verified-from-lock vs inferred-from-commit (inferred caps at suspect).
4. Adversarial review, sequential, until coherent. Run a challenger pass (assume the spec is
   incomplete or unimplementable; every objection must cite spec section and code path:line),
   then a defender pass on the challenger's findings only (concede genuine gaps, rebut with
   evidence), then revise. Repeat challenger -> defender -> revise until a challenger pass raises
   no blocking or major finding, max 4 cycles. Never run challenger and defender in parallel.
   Re-check every code claim a reviewer makes by reading the code; reviewers have misquoted
   specs before (see .praxia/docs/specs/261023_output-tiering.md section 13). Record each
   cycle's findings and dispositions in .praxia/docs/audits/261106_validity-spec-review.md.

CONSTRAINTS
- Python 3.13, cyclopts CLI (cli_cyclopts.py) + FastMCP mirror (mcp.py), thin layers over core
  modules. Ledgers append-only with latest-wins folds; SLURM-safe atomic writes.
- Do not write product code in this dispatch. Specs, research and audit docs only.
- Do not run the whole test suite locally (it is blocked and kills the machine).
- Owner decisions in the design's section 4 are fixed; if research argues against one, record
  it as an "owner question" in the spec, do not silently change it.
- Every count you cite from asr/tev_design comes from a command you show, not an estimate.

DELIVERABLES (commit each on the branch; push; open a DRAFT PR titled
"spec: result validity, supersession and archive (261106)")
- research/261106_validity-surfaces-recon.md
- research/261106_validity-prior-art.md
- specs/261106_result-validity-supersession-archive.md (status: REVIEWED_SPEC when done)
- audits/261106_validity-spec-review.md
Final report: PR URL, number of review cycles, unresolved owner questions (ranked), and any
design decision the research suggests revisiting.
```
