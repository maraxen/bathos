---
title: 'BP-2/BP-3: synthetic_recovery confound + negative_check field (claim-tier gate ports)'
description: Design decisions for porting asr's C1 invariant gate and C5 negative-claim check into bathos's native claim tier
status: shipped
task_id: 260721_bp2-bp3-bathos-port
date: '260721'
supersedes: ''
backlog_ids: 'asr#3413, asr#3414'
---
# BP-2/BP-3: synthetic_recovery confound + negative_check field (claim-tier gate ports)

## Context

BP-1 (C3 concentration alarm, `check_run_concentration()` in `linter.py`) already shipped as the
first native port from asr's retro-C1..C5 discipline. BP-2 (C1 synthetic-invariant gate) and BP-3
(C5 negative-claim falsification check) are the next two. Both are one-way concept ports — asr's
own gate scripts stay untouched.

## BP-2: `[confounds.synthetic_recovery]`

**The literal task framing ("hard-block at `bth campaign create`") doesn't fit bathos's actual
claim lifecycle, and I'm deviating from it deliberately.** In bathos, `campaign create` takes only
`name/mode/question/hypothesis/parent` — no claim is attached at creation time. A claim (and any
confound block inside it) is only bound to a campaign later via `bth claim register`, which
requires an *already-existing* `campaign_id`. There is nothing to check yet at `campaign create`.

The place bathos already does non-bypassable confound enforcement is `conclude_campaign()`
(`campaigns.py:208`), via the existing `[confounds.reference_parity]` pattern: `parity_confound_check()`
computes a live controlled/uncontrolled status per confound, and `conclude_campaign` downgrades
`outcome_label` to `'confounded'` for confirmation/sequential-mode campaigns (warns only for
exploration). I'm porting `synthetic_recovery` into this same architecture rather than inventing a
parallel create-time mechanism.

**Honest tradeoff, not a strict improvement.** Conclude-time enforcement does NOT reduce wasted
compute the way asr's create-time check aims to — a campaign can still run to completion on a
broken pipeline before the downgrade fires at conclude; that part of asr's pain point is not
solved here, only relocated to a later point in the same campaign's lifecycle. What conclude-time
enforcement adds instead is catching a class of staleness asr's one-shot create-time check
structurally cannot: guarded source edited *during* the campaign's run, after creation but before
conclusion. That's a different benefit, not a superset. Because `gate.py`'s ledger/staleness
machinery has to exist for conclude-time enforcement anyway, I'm also adding a cheap *advisory*
(non-blocking) `gate_state()` check at `bth claim register` time — surfaces RED/STALE/UNKNOWN
synthetic_recovery confounds as a warning the moment a claim is bound, well before conclude, so an
author gets a chance to fix a broken pipeline before sinking runs into it, without hard-blocking
anything at that point (register has no runs-so-far context to judge downgrade severity from).

**Registry shape: inline in the claim, no separate project-level registry file.** asr's
`.praxia/c1_invariant_gates.toml` is a separate project-level file because many asr campaigns
reuse the same ~20 shared component gates. bathos's `[confounds.reference_parity]` precedent
already duplicates its fields (reference_paper/metric/value/bound) per-claim with no shared
registry, and bathos's confound blocks are already claim-scoped by design. Introducing a second
config file format now would be a new bathos-wide convention for a problem (test/guard dedup
across many claims) this task doesn't actually report having yet. Keeping it inline is simpler,
consistent with the one existing precedent, and doesn't foreclose a future shared-registry
enhancement if duplication becomes a real pain point.

**bathos does not run the test itself.** bathos is stack-agnostic (Python/Rust/JAX projects
alike); asr's runner shells out to `uv run pytest <test>`, which bathos cannot assume. Splitting
the concern: the project's own test runner (asr's existing `c1_invariant_gate.py check`, or a CI
step, or anything else) is responsible for *proving* the test currently passes; bathos owns only
*recording* that fact (a ledger stamp) and *judging staleness* against it. This mirrors the
existing split between `bth run` (records provenance) and whatever produced the run.

**Shape:**
```toml
[[confounds]]
id = "C_pipeline_soundness"
label = "REQUIRED: what pipeline component this protects"
[confounds.synthetic_recovery]
gate_name = "potts_generator"                      # ledger key; author's choice, may be reused across claims
guards = ["src/asr/potts.py", "src/asr/potts_evolver.py"]  # paths whose change invalidates a recorded green
```

**Ledger:** `.bth/synthetic_recovery_ledger.json` (workspace-scoped, alongside `.bth/claims/`):
`{"gates": {"<gate_name>": {"result": "pass"|"fail", "sha": "<git HEAD sha at stamp time>", "ts": "<iso>"}}}`.
Written via a new `bth gate stamp <gate_name> --result pass|fail` CLI command (the project's CI/test
runner calls this after running its own test) — not by bathos executing anything itself.

**This stamp is self-attested, same trust model as `bth run`'s outcome field.** Nothing stops
`bth gate stamp foo --result pass` without ever running a test — bathos cannot verify the claim,
only record and later judge its staleness. This is an accepted, explicit limitation (bathos is
stack-agnostic and has no way to execute an arbitrary project's test), not a solved problem;
hardening the attestation itself (e.g. requiring a CI-only credential) is out of scope for this
port.

**Status semantics (`gate_state()`, new `bathos/gate.py`)** — same 4-state machine as asr:
- `UNKNOWN` — no ledger entry for `gate_name`.
- `RED` — last recorded result was `fail`.
- `STALE` — last result was `pass`, but any `guards` path has changed since the recorded sha
  (via a new `git.paths_changed_since(sha, guards, cwd)` helper — bathos has no existing
  "changed since sha X" primitive; `git diff --quiet <sha> -- <guards>`, any git error counts as
  changed, matching asr's fail-safe).
- `GREEN` — last result `pass` and no guarded path has changed since.

Only `GREEN` is `'controlled'` for confound purposes; `RED`/`STALE`/`UNKNOWN` are all
`'uncontrolled'` — fail-closed, matching asr and matching bathos's own `parity_confound_check`
default.

**Enforcement:** `synthetic_recovery_confound_check()` (new, `bathos/gate.py`, same signature
shape as `parity_confound_check`) is called from `conclude_campaign()` right alongside the
existing parity check, with identical downgrade semantics (confirmation/sequential →
`'confounded'` if any declared gate is uncontrolled; exploration → warning only). `validate_claim()`
gets a matching block (mirroring the AC-13 reference_parity block) that reports the same
controlled/uncontrolled status as info/error for interactive `bth claim validate` use.

## BP-3: negative-claim backing — structured field, not regex port

**Decision: build the structured field, not the regex-heuristic port.** asr's own design doc
(`260710_c4c5-claim-hygiene.md`) names the regex/marker-presence approach as a deliberately crude
stand-in and explicitly prefers "a verdict-schema `negative_check` field" as the eventual
bathos-native replacement. The regex heuristic also depends on an open-ended, hard-to-maintain
vocabulary (asr's own list has ~15 backing-marker patterns) whose false-negative/false-positive
rate nobody has measured; a required, explicit field has no such ambiguity — it swaps "did the
prose happen to contain a recognized word" for "did the author explicitly attest to backing."
That is a strictly better fit for a shared cross-project bathos feature than porting asr's
heuristic wordlist as a bathos default.

**Where the "verdict" concept already lives.** asr's separate `.praxia/verdicts/*.toml` file system
has no bathos equivalent to adopt wholesale — and doesn't need one. `bth campaign conclude
--outcome <label> --note <text>` (`campaigns.py: conclude_campaign`, `cli.py:732`) already *is*
bathos's per-conclusion verdict record: `outcome_label` is exactly asr's verdict `outcome` field,
`conclusion` is exactly the verdict narrative. No new verdict-file subsystem is needed; extend
this existing call.

**Design:**
- New `campaigns.negative_check TEXT` column, added the same way every other campaigns-table
  column was added (`compact.py`'s idempotent `ALTER TABLE campaigns ADD COLUMN IF NOT EXISTS`
  list) — not a new schema-version bump (the runs cool-tier Parquet schema is versioned;
  the campaigns DuckDB table is not, and every prior field on it — `claim_path`, `claim_mode`,
  `stopping_threshold` — was added this same way).
- New `--negative-check TEXT` option on `bth campaign conclude`.
- A configurable negative-outcome vocabulary (default seeded from asr's own list —
  `fail(ed)?|falsified|void|no-?go|not-a-fair-test|dead-?end|reversed|null|neutral|marginal`,
  overridable via `.bth.toml` `[claim] negative_outcome_pattern` so a project can use its own
  vocabulary instead of asr's) is matched against `--outcome` (case-insensitive).
- **Enforcement is gated on claim registration (`claim_path IS NOT NULL`), matching the existing
  opt-in adoption ladder** — not unconditional. My first draft of this doc proposed enforcing on
  every `conclude` call regardless of claim-tier adoption; an adversarial review of this doc
  correctly flagged that as inconsistent with how Union Gate and parity confounds already work
  (both explicitly skip via the `claim_path IS NULL` short-circuit at `campaigns.py:251`) and as a
  real backward-compat risk — existing callers/tests conclude campaigns with outcome labels like
  `"failed"` today with no claim attached, and an unconditional hard error would break them for no
  claim-tier benefit. Fixed: if a claim is registered AND its outcome matches the negative
  vocabulary AND `--negative-check` is blank, `conclude_campaign` raises. No claim registered →
  no negative-check requirement at all, identical in spirit to Union Gate's opt-in skip.
- `negative_check` content is not semantically validated (same limitation asr's own doc admits for
  its heuristic — presence, not sufficiency — except here presence is of an explicit author
  statement, not a lucky keyword match). It is persisted and surfaced in `bth campaign show` output
  and the existing claim-coverage sidecar for later human review.

## Both: verification

New tests in `tests/test_gate.py` (gate_state 4-state machine + ledger round-trip) and
`tests/test_claim.py`/`tests/test_campaigns.py` extensions covering: backed-negative conclude
(passes), unbacked-negative conclude (blocks), green/stale/red/unknown synthetic_recovery confound
→ conclude downgrade behavior for confirmation vs exploration mode, and validate_claim's new
diagnostic block. Every new code path gets a red-then-green test, per this session's own
discipline note about not shipping port logic untested.
