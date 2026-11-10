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

## Found clean (Cursor)

- A root-level `charter.purpose` and a root inline charter table are excluded.
- Quoted, spaced, array and subtable headers are removed from `reader/` bytes.
- Unlabelled sidecars, CRLF included, are byte-identical.
- A labelled file with a BOM is excluded.
- Packet hashes are deterministic.
- In `compare_reading`, duplicates are refused, null and `[]` give unmapped, a live-plus-retired list is handled, and a NULL budget does not set the flag.
