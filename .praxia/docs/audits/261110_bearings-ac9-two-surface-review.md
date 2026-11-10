---
title: 'Bearings AC-9 review: Cursor (correctness, blindness) and Antigravity (measurement validity)'
description: Two-surface review of the AC-9 commits on wt/261104_bearings-ac7; slug leaks and two scoring defects confirmed
status: draft
task_id: 261104_multiscale-goal-awareness
date: '261110'
verdict: 'NEEDS_WORK'
base_sha: 'f1708528'
---
# Bearings AC-9 review: Cursor (correctness, blindness) and Antigravity (measurement validity)

## What was reviewed and how

- **Commits:** `f1708528..9eda0d2b` on `wt/261104_bearings-ac7`: 86200477, 6717c962, a6d37e96, 7cefd7aa, 9eda0d2b.
- **Readers:** two reviewers, read-only, sent through praxia `surface_dispatch`, each in its own detached worktree.
  - Cursor (dispatch `45f7fab8`) looked at correctness and blindness.
  - Antigravity (dispatch `d5ac6611`) looked at measurement validity.
- **Dispatch records:** both say `admission: admitted`, `containment: agent_sandbox`, and no files changed.
- **Checked here, 261110:** the orchestrator re-checked the starred findings itself, by probe or by reading the code. The other findings are the reviewers' claims, not yet checked.

## Confirmed

1. **\* Slugs reach readers (critical; Cursor).** `strip_purpose_label` removes the `[charter]` table, but the slug itself survives in three places. Probe result, 261110:
   - in hypothesis text (`hypothesis = "serves lagoon-simulation"`);
   - in a misplaced label (`charter.purpose` dotted under `[experiment]`, or an `[experiment.charter]` table), which is copied through unchanged and also never reaches the answer key;
   - in a comment.

   On top of that, the packet's leak filter (`packet.py:134`) looks only for the mandate and the purpose texts. A slug in a git subject, a doc or a sidecar path also passes through.

   This follows from choosing readable slugs: charter ids are now ordinary words. The fix is to add charter ids and declared slugs to the leak filter with whole-token matching, and to exclude a sidecar if any slug is left after stripping.
2. **\* Off-charter work mapped to a retired purpose is credited as unmapped (high; both reviewers).**
   - `compare_reading` writes a retired target as `kind = "unmapped"` with `matched_retired` (`reading.py:489`).
   - `ac9_metrics` builds `mapped_by_rp` from `kind == "mapped"` only.

   So mapping off-charter work to a retired purpose scores as a correct unmapped call. Cursor built a case where this, together with item 3, gives `meets_pass` true for a wrong mapping. The fix: an off-charter reading purpose counts only when its target list is empty.
3. **\* `revived_flagged` ignores which reading purpose raised the flag (high; both reviewers).** `flagged` is one set built from every item. The fix: count a revived id only when the reading purpose that maps to it has that id in its gold.
4. **\* The spec's slug pattern was wrong (low; both reviewers).** The code uses `^[a-z0-9][a-z0-9-]*[a-z0-9]$|^[a-z0-9]$`. The spec text has been corrected. Cursor also notes that `a--b` is accepted although the error text says single hyphens.

## Reported, not yet checked here

- **Medium (Cursor):**
  - A purpose served only by a sidecar excluded as `label_unstrippable` counts as silent. The excluded slugs should go in the manifest and come out of the silent and false-silent denominators.
  - `[[charter]]`, `[charter.note]` or `purpose = "L5"` are stripped from `reader/`, but `read_purpose_label` then raises, so `answer_key` aborts. Such a sidecar should be excluded instead.
  - Labels for one path are combined across all its blobs, so different labels at different commits all count as agreement. A list mixing `off-charter` with a real slug is never counted as off-charter.
- **Medium (Antigravity):**
  - Gold agreement counts mapped pairs only, so it ignores retired targets. Sparse mapping is partly caught by false-silent, so this is a judgment call.
  - A reading purpose that cites only `doc:` or `git:` gets empty gold and is penalised if mapped.
  - The decoy charter comes from an unrelated field (software tooling), so it is too easy a control; an adjacent biology project's charter would test it properly. Also, the decoy's over-20%-mapped rule is not implemented in `ac9.py`.
- **High (Antigravity), contested:** a budget-cut packet makes `meets_pass` false.
  - Cursor and the code agree this is what T7 says (an unscorable metric is never a pass).
  - The real gap is pooling: the AC-9 sidecar must score silent cases on the packets that were not budget-cut.
- **Low (Cursor):**
  - Stripping deletes a comment or blank line between the label table and the next header.
  - `degenerate_mappings` raises IndexError when no purpose is live.
  - The `bearings_items` fold drops `budget_limited`.

## Missing tests (Cursor's list)

- a slug left in a hypothesis or comment;
- a nested or misplaced label;
- a slug in a git subject, doc or sidecar path;
- `[[charter]]`, `[charter.note]`, `L5`;
- off-charter work mapped to a retired purpose;
- two blobs of one path with different labels;
- a list mixing `off-charter` with a real slug;
- a purpose served only by a `label_unstrippable` sidecar;
- a NULL `budget_json` row;
- no live purpose;
- the slugs `7ab`, `a-` and `a--b`.

## Remediation (261110, commits c386606c and 105b22be)

| Finding | Status |
|---|---|
| Slugs reach readers | **fixed**: charter ids are whole-token leak needles across every record kind (`names_charter_id`) |
| Off-charter mapped to retired credited | **fixed**: needs no target at all |
| Revived lineage | **fixed**: the flagging reading purpose's gold must name the retired id |
| Spec slug pattern | **fixed**: spec corrected; pattern now single hyphens only (`a--b` refused) |
| `label_unstrippable`-only purpose counted silent | **fixed**: withheld evidence makes a purpose indeterminate |
| `[[charter]]` / `[charter.note]` / `L5` abort the scorer | **fixed**: refused at strip, sidecar excluded; a withheld malformed label raises `Ac9LabelError` (owner fixes it) |
| Two blobs, different labels | **fixed**: disagreeing slugs are indeterminate (labelled blobs only) |
| Mixed `off-charter` list | **fixed**: refused as a malformed label |
| Gold agreement ignores retired pairs | **fixed**: retired pairs included; sparse mapping caught by false-silent (test) |
| `doc:`/`git:`-only citations get empty gold | **open**: labels live on sidecars; left for the AC-9 sidecar to treat |
| Decoy too easy; 0.2 rule missing | **fixed**: nanobody-drawn draft (owner to confirm), `decoy_mapped_share`, `decoy_invalid` |
| Budget-cut packet fails `meets_pass` | **as designed**; pooling across packets belongs to the AC-9 sidecar |
| Comment/blank line after the label table deleted | **open, low**: harmless; the deleted lines are not shown to readers |
| `degenerate_mappings` IndexError | **fixed** |
| `bearings_items` drops `budget_limited` | **fixed**: column added, migrated by `_add_missing_columns` |

Tests: the touched test files (Bearings, charter, validate, fold, index migration) pass.

## Round 2 (261110, base dca7a914)

- **Antigravity** (dispatch c2913f7a, a retry of 8340f5d9). The first attempt failed with no report: it called an MCP tool and headless mode denied it. The retry's record reads `admission: rejected`, but the only reason given is the first attempt's leftover `.praxia/patches/8340f5d9….diff`, a host artifact, so the report is usable.
- **Cursor** (dispatch 0a8e1390): admitted. Its base predates 507afc46, so its run-withholding finding was already fixed there.

| Finding (round 2) | Severity | Status |
|---|---|---|
| A run withheld by its sidecar path or a field loses its sidecar label; the purpose counts as silent | high (both) | **fixed** 507afc46: `withheld_run_sidecars` |
| Unicode or encoded hyphens, zero-width characters, line-broken slugs pass the matcher | high (Cursor) | **fixed** f7da0728: `fold_for_ids` |
| Project slug in `reader/index.json` is not scanned; there is no last check over every reader byte | high (Cursor) | **fixed** f7da0728: slug refused; `PacketLeakError` scan |
| Disagreement between commits is project-wide, not per path | low (Antigravity) | **fixed** 507afc46 |
| One path shown under one label and withheld under another stays present | medium (Cursor) | **fixed** f7da0728 |
| A `not_text` sidecar loses its label | medium (Cursor) | **fixed** f7da0728 |
| Packet and scorer use different id sets; one-word ids over-exclude | medium (Cursor) | **fixed** f7da0728: shared `id_patterns`; one-word ids unmatched |
| A stored id containing `_` does not match its hyphen spelling | low (Cursor) | **fixed** f7da0728 |
| Two decoy purposes are generic | medium (Antigravity) | **fixed** 507afc46 |
| Decoy comment says tev_design's draft is "6 live" | spec (Antigravity) | **fixed** 507afc46: it is 7 live |
| `marginal` is not implemented | spec (Antigravity) | **fixed** 507afc46: `meets_marginal` (false-silent ≤ 0.4 reading, owner to confirm) |
| Indeterminate can shrink denominators; metrics with no items make a run unscorable | high/medium (Antigravity) | **moved to the AC-9 sidecar**: bounds proposed in spec T7 |
| Working-tree or confirmation ids change the packet hash | medium (Cursor) | **as designed**: same sources as the purpose-text needles |
| Off-charter cited together with an unlabelled sidecar is penalised twice | (Antigravity) | **open** |

## Advisor check after round 2 (261110)

- **JSON-encoded leak, fixed 5c33dff2.** A run's tags hold JSON inside a JSON string, so a `‑` escape decoded to the slug for a reader. The id check now tries JSON-decoded forms, and the last scan parses `.json` and `.jsonl` files.
- **Slug count extended to sidecar text (run f35d5826):** 1 of 132 tev_design sidecars names a slug in ordinary prose. The spec now calls the count an unregistered descriptive count.
- **Withheld-run count fixed:** it now covers the blindness filters only, with every exclusion also listed by reason.

## Round 3 (261110, base 581a22c5; focuses swapped)

- **Antigravity** (dispatch af3e5309) looked at correctness and blindness. Admitted.
- **Cursor** (dispatch 938017d2) looked at measurement validity. Admitted.
- **Antigravity claims rejected:**
  - "no tests for `ac9.py`" is false; the planted-set file covers it.
  - "a literal `‑` in a plain field decodes to the character" is false; `canonical_json` escapes the backslash. Those escapes are now decoded anyway, since an LLM reads them as hyphens.

| Finding (round 3) | Severity | Status |
|---|---|---|
| Soft hyphen deleted, not mapped, so the slug slips through | critical (Antigravity) | **fixed** 20839b23 (all round-3 fixes): dash category (Pd), soft hyphen and U+2043 all map to `-` |
| U+2043; named or zero-padded HTML entities; escaped dashes; `%5F` | high (Antigravity) | **fixed**: `html.unescape` plus a wider escape pattern |
| A withheld run with an unresolvable sidecar leaves its purpose silent | high (Antigravity) | **fixed**: listed as `withheld_runs_unresolved`, counted as `n_withheld_runs_unresolved` |
| Deep JSON nesting crashes the build (`RecursionError`) | high (Antigravity) | **fixed**: depth limit, walk without recursion, error caught |
| Last scan cost on large packets | high (Antigravity) | **fixed**: skips non-ASCII handling for ASCII text, deduplicates strings, folds whole text once |
| Line-end dash joins unrelated words | medium (Antigravity) | **fixed**: joins only between letters or digits |
| Cyrillic look-alike letters; combining marks | medium (Antigravity) | **out of scope**: deliberate evasion, not accidental leakage |
| A shared sidecar counts as withheld | low (both) | **fixed** |
| A path mixing off-charter and a slug gives union gold, so off-charter is unscorable and a wrong mapping counts as agreement | high (Cursor) | **fixed**: `ambiguous_paths` give no gold; citing reading purposes are not scored on gold |
| No shown denominators for the withheld-share bounds | medium (Cursor) | **fixed**: `n_shown_sidecars`, `n_shown_runs` |
| Running or unreachable runs' labels are not read | low (Cursor) | **open, by design**: out of the window; the AC-9 run should have no running runs |

Tests: 348 pass across the Bearings, charter, validation, fold and index-migration files.

## Round 4 (261110, scope: commit 20839b23 only; focuses as in rounds 1–2)

- **Cursor** (dispatch 2d3a003d) looked at correctness and blindness. Admitted. No high-severity finding.
- **Antigravity** (dispatch 384efd33) looked at measurement validity. Admitted.

| Finding (round 4) | Severity | Status |
|---|---|---|
| Dropping a whole reading purpose that cites an ambiguous path hides mapper errors, including off-charter ones, and can empty the gold-agreement denominator | high (Antigravity, ×2) | **fixed** 3cba000d: every purpose stays in gold; a target only among the ambiguous labels is unscored (`n_unscored_pairs`), any other target is a miss |
| A revived flag from a purpose citing an ambiguous path is missed | medium (both) | **fixed** 3cba000d and 1ca85ea2: flagged from firm or ambiguous labels; agreed labels of a path stay firm gold |
| A withheld run with an unresolvable sidecar corrupts silent ground truth; reporting it is not enough | high (Antigravity) | **fixed** 3cba000d: `invalid_reasons(key)` makes the comparison invalid |
| Withheld-sidecar share denominator mixes populations | medium (Antigravity) | **fixed** 3cba000d: `n_shown_sidecars_labelled` |
| `reading_gold` defaults to empty gold for ambiguous citers | medium (Cursor) | **fixed** 3cba000d: no dropping; ambiguity passed as `maybe` |
| `&shy` without a semicolon is decoded; fullwidth `＆` or `％` not decoded; fixed list of `\uXXXX` dash escapes | low (Cursor) | **fixed** 1ca85ea2 |

Tests: 356 pass across the Bearings, charter, validation, fold and index-migration files. **Stopping rule met:** round 4 found nothing high-severity in the round-3 code beyond the ambiguous-gold design, and that design is replaced. The round-4 fixes themselves have not had an independent review.

## Found clean (Cursor)

- A root-level `charter.purpose` and a root inline charter table are excluded.
- Quoted, spaced, array and subtable headers are removed from `reader/` bytes.
- Unlabelled sidecars, CRLF included, are byte-identical.
- A labelled file with a BOM is excluded.
- Packet hashes are deterministic.
- In `compare_reading`, duplicates are refused, null and `[]` give unmapped, a live-plus-retired list is handled, and a NULL budget does not set the flag.
