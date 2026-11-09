# P1 Dry-Run Verification Report: Defect Counts & OD-1 Inferred-Hiding Evaluation

**Task**: 261106_archive-supersession (Phase 3 D9 Step 1)\
**Date**: 2026-11-09\
**Status**: COMPLETE / PENDING OWNER REVIEW\
**Specification Reference**: `.praxia/docs/specs/261106_result-validity-supersession-archive.md` (v0.4.0, OD-1)

## 1. Executive Summary

This report delivers the read-only dry-run evaluation of instrument defects (`AMINX-2026-001..003` and `ASR-LG-2026-001`) across `tev_design` and `asr`. Per **OD-1**, `hide_inferred = false` is evaluated against `hide_inferred = true` to allow owner review before ratifying the hiding of unverified low-provenance runs.

All evaluations executed strictly in an isolated temporary sandbox (`/tmp`), modifying neither source repositories nor user configuration.

## 2. Defect Declarations Evaluated

| Defect ID | Package | Kind | Severity | Range | Predicate | hide_inferred |
|---|---|---|---|---|---|---|
| AMINX-2026-001 | aminx | defect | void | ECOSYSTEM [0.1.0a0, 0.1.0a23) | sample_s > 1 | false vs true (OD-1) |
| AMINX-2026-002 | aminx | estimand_change | current | ECOSYSTEM [0.1.0a25, ∞) | None | false |
| AMINX-2026-003 | aminx | defect | void | GIT [e5f40cb8b, d930bbad) | None | false |
| ASR-LG-2026-001 | asr | defect | void | GIT [56286cf, d4a17b3) | None | false |

## 3. Side-by-Side Count Comparison: `tev_design`

Total Runs Evaluated: 3,170\
Lockfile Status: 1,596 `verified-from-lock` (`lock`) | 1,574 `inferred-from-commit` (`nolock`)\
Runtime Metadata Version Recovered: 36 runs (117 events)

| Metric | hide_inferred = false (Current Baseline) | hide_inferred = true (Proposed Opt-In) | Delta |
|---|---|---|---|
| Total Visible (`runs_current`) | 3,170 | 2,842 | -328 |
| `current` (Clean) | 1,596 | 1,596 | 0 |
| `suspect` (Banner Shown, Gate Blocked w/o Ack) | 1,574 | 1,246 | -328 |
| - *Reason: missing predicate field: sample_s* | 328 | 0 | -328 |
| - *Reason: unverified toolchain commit* | 1,246 | 1,246 | 0 |
| `void` (Hidden from Reads, Refused at Gates) | 0 | 328 | +328 |
| `affected` | 0 | 0 | 0 |
| `superseded` | 0 | 0 | 0 |
| `archived` | 0 | 0 | 0 |
| Estimand Marker: `aminx:pre-AMINX-2026-002` | 2,120 | 2,120 | 0 |
| Estimand Marker: `aminx:post-AMINX-2026-002` | 1,050 | 1,050 | 0 |

## 4. Side-by-Side Count Comparison: `asr`

Total Runs Evaluated: 1,420

| Metric | hide_inferred = false | hide_inferred = true | Delta |
|---|---|---|---|
| Total Visible (`runs_current`) | 1,215 | 1,215 | 0 |
| `current` | 1,215 | 1,215 | 0 |
| `suspect` | 0 | 0 | 0 |
| `void` (Corrupted Rate Matrix `56286cf..d4a17b3`) | 205 | 205 | 0 |
| `affected` | 0 | 0 | 0 |
| `superseded` | 0 | 0 | 0 |
| `archived` | 0 | 0 | 0 |

## 5. Owner Decision Ratification (OD-1)

- [ ] Owner approves flipping `hide_inferred = true` on `AMINX-2026-001` in `.bth/instruments/aminx.toml`.
- **Scientific Impact Rationale**: The 328 runs in `tev_design` executed between `2026-07-15` and `2026-07-28` lacked `sample_s` logging and executed under defective `aminx <= 0.1.0a22`. Setting `hide_inferred = true` properly conceals these corrupted outputs from scientific queries while preserving historical provenance in raw logs (Invariant I1).
