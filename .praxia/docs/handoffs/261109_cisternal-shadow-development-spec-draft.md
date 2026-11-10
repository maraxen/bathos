---
title: "Handoff: cisternal shadow-development spec draft"
description: "Draft spec for a cisternal.shadow module and developing-in-shadow skill, for a cisternal session to file under its specs and review."
task_id: 261109_cisternal-shadow-development
status: draft
---

# Handoff: cisternal shadow development (draft spec)

**For:** a session started in `~/projects/cisternal`. File this as
`.praxia/docs/specs/261109_shadow-development.md` there (praxia `docs add`, category `specs`),
then run challenger review, then defender review on the findings (sequential, never parallel), before any code.

**Owner decisions (261109, AskUserQuestion, recorded from the bathos session for spec 261104):**
- The spec lives in cisternal, not bathos. Bearings AC-9 (bathos spec 261104) is the first adopter.
- Stage names are `off / shadow / advisory / enforce`.
- Bearings labels are readable slugs written in a sidecar `purpose` field at the time of work.

**Evidence:** survey of existing shadow mechanisms, report
`~/projects/bathos/.praxia/subagent_outputs/dsp_19162da8651e14ea.1.md` (read-only survey; re-verify every
file:line before relying on it; the survey was a Haiku agent and its citations are unverified).

## Problem

Shadow mode (a feature runs, records what it would have done, never acts, and is calibrated before it may act)
has been built four separate times, with no shared format:

| Where | Verdict record | Labels | Promotion | Isolation |
|---|---|---|---|---|
| praxia dispatch / mode state | `shadow_pair_id` on records; `.praxia/mode_state/*.toml` (rung by stage) | frozen pre-registered gold; D3f disagreement sampling | `check-mode-state` computes thresholds; most stage moves described only | `prove_isolation` on a separate clone |
| maraxiom `reader_text` | per-build `_reader_text/<build_id>.jsonl`, `classify` null | after the fact (fixed / false_positive / accepted) plus a frozen calibration corpus | owner decision OD-1 with C1-C3; `SHADOW_SUBRULES` hand-coded | build output unchanged |
| bathos blast-radius | `shadow_verdict` JSON column, "never applied" | none | none | caller sets state, never the verdict |
| cisternal | `ShadowExporter` is a test spy only; `tests/shadow/` is a pre-cutover parity harness | n/a | parity must pass before cutover | n/a |

Gaps: no common verdict schema or join key; no shared stage vocabulary; gates computed in one place only;
labels mostly collected after the fact, and no mechanism records where a label came from; trigger precision
unmeasured in bathos; isolation proof only in praxia; gold discipline only in the praxia
`distilling-decision-models` skill.

Known failures to design against: praxia's live T22 shadow runs failed three times on driver bugs (relative paths,
an early Route-stage failure, a leak detected by V6); the T3 joiner returned incomplete with one half because it
text-searches traces instead of reading dispatch records; a praxia tree gate fired at precision 0.10 against a base
rate of 0.56; maraxiom calibration failed on R1 (recall lower bound 0.29) and R7a.

## Design (proposed, for review)

1. **Verdict event `shadow.verdict`** emitted through cisternal's existing JSONL exporter: `feature`,
   `feature_version` (content hash), `subject_key` (join key), `verdict` (JSON), `stage`, `inputs_sha256`, `ts`.
   One log per project at a configured location (derived-data location rules: explicit argument > env var >
   `[tool.cisternal]` > user config > off; no default path in library code).
2. **Label event `shadow.label`**: `feature`, `subject_key`, `label`, `label_source` in
   `at_work | owner_confirmed | retrospective | frozen_gold`, `labeller`, `ts`. Gates may restrict which sources
   count (for example, only `frozen_gold` reaches a decision-quality claim, as in the distillation skill).
3. **`cisternal shadow score <feature>`**: joins verdicts to labels by `subject_key` (reading event records, never
   text-searching traces); reports confusion counts, precision and recall with interval bounds, each next to its
   base rate; refuses to score a feature with zero labels instead of reporting 0 or 1.
4. **Stage ladder `off -> shadow -> advisory -> enforce`** in a mode-state TOML per feature (praxia's shape):
   current stage, pre-registered gates (min verdicts, min labelled, metric bounds, `threshold_basis` saying where
   each threshold came from, "chosen; no evidence" allowed but visible). `cisternal shadow check` computes
   `promotion_permitted` and the blocking reasons. It never changes the stage; the owner does.
   `advisory` = shown to the actor, not enforced.
5. **Isolation check**: a pytest helper generalized from praxia's `prove_isolation` and cisternal's
   `tests/shadow/` harness: the actor's output is byte-identical with shadow on and off, and the shadow arm wrote
   nothing outside its own log.
6. **Skill `developing-in-shadow`** (cisternal plugin): when to start a feature in shadow; capture labels at the
   time of work; pre-register the gates as a bathos sidecar before the first verdict; run a dry pass of the driver
   first; never ask a model to judge what it cannot determine; report metrics against base rate; mark
   evidence-free thresholds.

## Adoption order

1. Bearings AC-9 (bathos 261104): verdict = a reader's mapping of a sidecar to a charter purpose; label = sidecar
   `purpose` slug (`at_work`, owner confirms in batches); packet builder strips the field so readers stay blind.
2. bathos blast-radius shadow trigger: already emits verdicts; add labels to measure trigger precision.
3. maraxiom `reader_text`: move the per-build log and classifications onto the shared events.
4. praxia mode state: map rung by stage onto the ladder, or keep praxia's axes and export the shared events.

Existing mechanisms keep working until each moves over.

## Open questions for review

- Is the per-feature mode-state TOML in the consuming repo, or in cisternal's config layer?
- Does `subject_key` need a namespace convention across projects (for example `<project>:<feature>:<id>`)?
- Interval method for small n (Wilson, or Clopper-Pearson as in maraxiom's C1/C2)?
- Does `advisory` need its own gate, or is it free to enter from `shadow`?
