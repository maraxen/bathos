---
title: "Bearings v1: multi-scale goal awareness for science agents"
description: "Owner-confirmed project charter, purpose ledger and blind cold reader over runs and docs, observe-only, with pre-registered shadow exit"
status: draft
task_id: "261104_multiscale-goal-awareness"
session_id: "9000eadf"
topic: "How should bathos give science agents working awareness of multiple scales of goals (run, campaign, claim, project aim, portfolio) and integrate reflection discipline, so that they stop getting lost in rabbit holes? Go beyond the campaign-scoped step-back feature. Must stay compatible with future adversarial collaboration, argumentation schemes and multiverse analysis work."
task_type: "architectural"
winner: "Bearings v1, observe-only. (1) Per-project charter: purposes, each live or retired, plus the owner's verbatim mandate. An agent drafts it from the record; it counts only when the owner confirms it. (2) Purpose ledger: every pivot is an event that reaffirms or retires each live purpose and records what happened to the cheapest pending next test. Skipping a live purpose is refused. (3) Blind cold reader on a cadence tied to activity, not to derived signals. It reads only primary records (runs, sidecars, ADRs, specs, backlog; docs scope per T2) and writes a 'what this project appears to be doing' record, compared with the charter. Disagreement reaches the owner's brief. (4) Every delivery and every owner label is an event, so precision and a pre-registered excess-spend metric can be measured before any push to agents. Out of v1: detours, the argument and multiverse features, decision-follow and drift signals, and run-end push."
created_at: "2026-11-04T17:03:34.800628+00:00"
---

# Brainstorm: How should bathos give science agents working awareness of multiple scales of goals (run, campaign, claim, project aim, portfolio) and integrate reflection discipline, so that they stop getting lost in rabbit holes? Go beyond the campaign-scoped step-back feature. Must stay compatible with future adversarial collaboration, argumentation schemes and multiverse analysis work.

## Problem Frame
Frame: agents do honest, pre-registered, green work that stops serving any larger goal, and nothing notices until the owner does. Design one bathos feature that makes a working agent aware, when it matters, of what its work serves across scales, and that records whether higher-level decisions moved. Fixed: reaches runs with no campaign; pushed, not remembered; warn by default, level 3 human-only; judgment is not mechanized into a pass/fail the agent can satisfy; no dependence on a hand-kept file without a forcing function; productive detours stay possible; scored by downstream outcomes; schema leaves room for allostery's atom link and for adversarial collaboration, argument schemes and multiverse work without building them. Negotiable: storage, whether a declared hierarchy exists at all, names, which scales ship first, extending versus wrapping step-back, coverage of non-run agent work.

## Idea Pool
- [ai] A. Declared goal tree: a project goals file (charter > aims > questions), sidecars declare [serves], rollups walk up. Top-down, in the Simon means-ends and OKR tradition.
- [ai] B. Decision ledger: the universal unit at every scale is an open decision, with horizon, owner, options and what each option's evidence looks like. Sidecars point at a decision. A rabbit hole is defined structurally as work that moves no decision at a higher scale. Hierarchy emerges from decisions naming a parent decision. Hubbard value of information plus the advisor's 'what would change your plan'.
- [ai] C. Inferred threads, no declaration: derive threads of work from sidecar lineage, stems, git history and sessions; a blind reader assigns them to goals. Nothing for the agent to game or for a human to keep fresh.
- [ai] D. Excursions with appetite and return address: any side investigation is opened as an explicit detour with a budget and the parent it returns to; expiry defaults to return, not extension. Soar impasse subgoaling plus Shape Up. Licenses productive detours instead of banning them.
- [ai] E. You-are-here packet, pushed by hooks at session start, after compaction, and after each bth run outcome: the chain upward, the registered decision for the outcome just observed, budget left, and last time anything above moved. Altmann and Trafton cues at resumption.
- [ai] F. Decision-follow audit: 97% of sidecars already state what each outcome should lead to. Compare the registered decision with what was actually done next. Divergence is not a violation, it is the signal. Gollwitzer implementation intentions meets Argyris double-loop.
- [ai] G. Project-scope triggers that see campaign-less runs: pivot churn, streaks with no decision moved, sidecar fan-out, hypothesis text drift, null followed by question change.
- [ai] H. Purpose ledger: generalize the colliculix charter. Pivots become first-class events that must reaffirm or retire purposes and say what happened to the cheapest recommended next test. Lakatos progressive versus degenerating check on each pivot.
- [ai] I. Recursive step-back: the same review at each scale, on clocks matched to that scale (Beer VSM recursion, pace layers), with Walton's practical-reasoning critical questions as the question set at every level.
- [ai] J. Scheduled lab meeting: a weekly portfolio brief the owner answers, doubling as the forcing function that keeps project_status.toml alive. Rotating one project per session.
- [ai] K. Forecast ledger: when a detour opens, forecast the probability it changes the parent decision. Score later. Gives calibration data on the agent's sense of value.
- [ai] L. Blind reconstruction: periodically a fresh agent reads only primary records and states what this project appears to be trying to do. Compare with the stated purpose. Disagreement is the drift signal, and it cannot be satisfied by writing better goal text.
- [ai] M. Literal-ask anchor: store the owner's verbatim mandate immutably. Any synthesis or narrowing must diff against it, and silent narrowing surfaces as a decision. From the tev substitution episode.
- [ai] N. Load-bearing premise registry: each goal node names the premises it depends on and whether each was verified at source. From the tev false-premise episode; Lakatos hard core, Meehl auxiliaries.
- [ai] O. WIP limit on open threads per project: opening a new line requires closing or parking one. Reinertsen.
- [ai] P. Circuit breaker: budgets on compute and runs per thread, with a loud state on expiry rather than a block.
- [ai] Q. Portfolio burn versus stated priority: compare compute and active days per project with priority, surfaced in the brief.
- [ai] R. Session-close field: handoffs must say what moved above this session's level; if nothing, say so. Sessions are where agents actually live.
- [ai] S. Heilmeier ladder at scaffold time: bth new-experiment asks why-how ladder questions in the sidecar, so purpose is written at the cheapest moment.
- [ai] T. Minimal: make the existing step-back push (wire triggers into run end), extend triggers to sidecar-derived threads, add nothing else.
- [ai] U. Goal nodes typed as practical-reasoning arguments so they can later be attacked, forked into a multiverse of defensible choices, or contested by rivals sharing a discriminating test. Seam for adversarial collaboration, Walton and Steegen.
- [ai] V. Indirectness tag: on each link record how far the measurement is from what the parent asks (GRADE indirectness, estimands), catching proxy substitution such as the k=4 vs k=9 compute framing.
- [ai] W. Episode replay suite: encode documented episodes as fixtures, and judge any design by whether it would have fired and whether its question would have changed the action.
- [critic] X1. Orphaned-null ledger: each fail or marginal outcome opens an implicit 'what did this null change?' entry, closed only when a later run names it as predecessor or a human drops it; old open ones appear in the run banner. Independent generator; clinical trial governance.
- [critic] X2. Output reachability: runs, outputs and figures as nodes with edges for consumed by, cited in a claim, shown on a review page. Report the share of recent runs with no downstream consumer within K days. Independent generator; bibliometrics.
- [critic] X3. Question lineage by text drift: compare sidecar hypothesis and decision vocabulary with the project's earlier text and show 'this question shares little with your last N' at run time. A measure, not a check. Independent generator; library science.
- [critic] X4. Hard-to-fake leading indicators: how often the primary metric name changes, how often target or system changes, vocabulary stability. Shown as small trend lines. Independent generator; ecology indicator species.
- [critic] X5. Rotating lens at pre-registration: one rotating question (who would use this answer, what would you do if null, what did you stop doing to make room), answers kept but never graded, compared later. Independent generator; lab-meeting ethnography.
- [critic] X6. Activity-tied owner handover page every N new runs instead of every N days, covering aim text, claims touched, recent nulls, unconsumed outputs, so the owner's own memory is the check. Independent generator; I-PASS clinical handoff.
- [critic] X7. Runs per consumed outcome compared across projects to surface portfolio outliers. Independent generator; queueing and bottleneck analysis.
- [critic] X8. Cold-reader briefing pushed into the agent's next bth output at natural boundaries, three lines from a stranger's reading of the recent sidecars. Independently converges with L. Independent generator; appellate bench memo.
- [critic] X9. Faction DERIVE: compute everything from what already exists (sidecars, decisions, outcomes, git), ask for no new authoring, and treat the record as the only source. Combines C, F, G, L, X1 to X4.
- [critic] X10. Faction DECLARE: make goals and open decisions first-class authored objects, and attach work to them. Combines A, B, D, H, M, N.
- [user] converge

## Decision Log
- [MERGE] Cold reader gated on derived signals (draft part 5): Critic, MAJOR. Colliculix had 34 runs, mostly outcome unknown, and its drift lived in ADRs, docs, branches and backlog, so the signals would not have fired and the gated reader never runs. Remove the gate. Run the reader on a cadence tied to activity, and have it read docs and decisions as well as runs. That is what the real diagnosis did.
- [REJECT] Seed standing decisions from project_status.toml (draft part 1): Critic, MAJOR. project_status.toml:688-690 still gives colliculix's superseded identity, 80 days after its charter replaced it. All 60 rows are unreviewed despite a 7-day cadence, and 21 reconsider dates have passed. Seeding from it would load stale purposes. Entries must be confirmed by the owner, not imported.
- [MERGE] Open-decision unit with [serves] label (draft parts 1-2): Critic, MAJOR. Drift lives in purposes nobody considers open; colliculix never retired its claimed purposes. The unit needs a retire state, and a pivot must name what it displaces (the colliculix purpose-ledger rule). A [serves] label carries no consequence, so it is deferred unless it states which result would change which option.
- [ACCEPT] Measurement and rollout (draft part 7): Critic, MAJOR. There is no success or kill criterion, and _advise (runner.py:103-109) leaves no event, so delivery is unrecorded. Replay on documented episodes is a positive control chosen after the fact. Required: a delivery event, negative-control episodes, a registered excess-spend metric that counts chains that paid off (261027 §8). The existing `excess_spend_avoided` heuristic at `src/bathos/stepback/analysis.py:667-675` is not that metric: it counts later runs and does not credit chains that paid off, and owner-labelled precision measured in shadow before any push. Base-rate arithmetic: about 88 runs a day means a 1% per-run false-alarm rate is roughly 26 false alarms a month.
- [REJECT] Rival T: wire existing step-back triggers into run end: Critic, FATAL as the whole answer: it represents no aims, so the multi-scale ask goes unmet. It would also push likely false positives to every agent: entropy_collapse probably fires on all-observed exploratory runs (triggers.py:365-375), and rule 3 fires on any exploration campaign with 25 or more runs (triggers.py:532). The wiring can be merged later, once those are fixed.
- [DEFER] Detours (part 6), argument and multiverse features (part 8); the seam itself (ext tables and reserved atom: kind) is preserved per items 1 and 5, decision-follow and question-drift signals: Critic. Detours need the rabbit hole to be named in advance, which by nature it is not. The seam rests on an unmerged branch with unstable atom ids. Decision follow and question drift need a semantic judge and an audit of TODO decision text, so they run in shadow only. None of these changes the colliculix episode.

- [OWNER 261104] **Frame deviation, recorded explicitly.** The confirmed frame fixed "pushed, not remembered" and "makes a working agent aware". v1 does neither for the working agent: it is observe-only, and the brief goes to the owner. Agent push is deferred to T5, after the shadow exit. The owner chose this through AskUserQuestion ("Cold reader + ledger, shadow"). Merging v1 does not give agents goal awareness.
- [OWNER 261104] Scope is runs plus project docs. Goals come from an owner-confirmed charter, and `project_status.toml` is not a seed.
- [OWNER 261104] **Build plan.** The build stacks on this spec branch, and PR #160 stays open until the owner merges it. It ships as three staged dispatches, each ending with tests and a review gate before the next starts: (1) charter and confirmation, AC-1 to AC-3; (2) packet and reading record, AC-4 and AC-5; (3) brief, labels, due notice and cadence, AC-6 and AC-8. AC-7 is kept out of the build. After slice 2, the orchestrator drafts the AC-7 sidecar, including how tev_design's local episodes are classified. The owner approves it, and it is committed before any replay runs. The agent that builds the reader does not define what counts as success for it.
- [OWNER 261104] **Build surface after slice 1.** Slices 2 and 3 use an in-session Claude fixer in an isolated worktree, followed by a Sonnet review. Slice 1 went to headless agy (dispatch `bc7fea2f`), whose sandbox auto-denied `RunCommand`. It could not run tests, commit or return a structured result, so admission rejected the dispatch, and the orchestrator recovered the code from praxia's rollback patch.
- [ORCHESTRATOR 261104, pending owner review] **Spec gap found while planning slice 2.** Design item 5 says bathos compares the reading with the charter, but a blind reader cannot name charter ids, and a text match would be a judgment proxy the frame rules out. The slice 2 plan splits the step: `bth bearings record` stores the blind reading, and `bth bearings compare` takes a mapping written by a different agent. Bathos only checks completeness and validity and computes `mapped`, `unmapped` and `silent`. The owner has not yet ruled on this.

## Assumptions

Tags follow the global spike-test rule. *Verified* means read at a file:line or measured in a tracked run. *Unverified* means not checked.

| # | Assumption | Status |
|---|---|---|
| A1 | Most recent runs have no campaign, so campaigns cannot be the attachment point. | Verified: run `da651e36`, 70.8% since 2026-10-01 |
| A2 | Step-back is pull-only. `runner.py` never calls a trigger. | Verified: critic read all of `runner.py`; grep of callers |
| A3 | Colliculix's drift lived mostly in documents and branches, not runs. | Verified: diagnosis memory lines 18-34 |
| A4 | `project_status.toml` is stale and would seed wrong purposes. | Verified: lines 688-690 against charter lines 20-34 |
| A5 | A human-only confirmation check exists and can be reused. | Verified: `stepback/escalation.py:49-54`, `164-184` |
| A6 | A citation resolver exists and can be extended with new kinds. | Verified: `stepback/analysis.py:196-355` |
| A7 | New run-log event kinds with their own fold are feasible. | Verified: `runlog/fold_output_copies.py` |
| A8 | `[outcomes].decision` text is meaningful. | **Weakened**: the scaffold writes `"TODO: ..."` (`new_experiment.py:161,175,182`) and the census counted those. Not used in v1. |
| A9 | A blind reader with primary records reproduces a diagnosis like colliculix's. | **Unverified.** AC-7 tests it. |
| A10 | The owner will confirm a charter for each active project. | **Unverified.** This is the pre-mortem's first failure path. AC-8 measures it. |
| A11 | The colliculix charter stopped the sprawl. | **Unverified.** The project went idle the day the charter was accepted (`project_status.toml:694`), so stopping sprawl and stopping work cannot be told apart. |

## TBDs

| # | Open question | Owner | Must be settled before |
|---|---|---|---|
| T1 | Shadow exit numbers: how many labelled readings, the precision needed to enable push, and the decision date. These numbers go in the evaluation sidecar. | Owner (gate-tier) | First reading |
| T2 | **Resolved by the owner 261104: exclude narratives.** The packet holds ADRs, specs, sidecars, runs, backlog titles and git subjects. Handoffs, daily logs and other agent-written narrative are left out, because they carry the doing agent's framing. | Owner | Resolved |
| T3 | **Resolved by the owner 261104: more than one way.** Readings can be run (a) by a dispatched cadence (a scheduled routine or praxia dispatch that calls `bth bearings due` and runs readings) or (b) on demand, prompted by a due notice that says a bearings reading is needed (Design item 7). Bathos still does not dispatch. It computes due state and reports it. | Owner | Resolved |
| T4 | Whether portfolio scale (burn against priority across projects) is v2, and whether it belongs in bathos or praxia. | Owner | v2 |
| T5 | Whether a run-end reminder for unconfirmed charter edits ships after shadow. It would be a fact rather than a judgment. The pre-mortem proposed a reminder for an "unreconciled pivot", but that state no longer exists because a pivot that skips a live purpose is refused (Design item 3). An unconfirmed charter edit is the nearest fact that remains. A pivot made with no ledger event at all can only be seen by the reader, as unmapped work. | Owner | After the shadow exit |

## Design (v1)

**Scope**, as decided by the owner 261104 (AskUserQuestion): runs plus project docs; goals from an owner-confirmed charter; the first ship is the cold reader and purpose ledger in observe-only mode. The narrowing away from portfolio scale and session-level work is deliberate and recorded in T4.

1. **Charter.** `.bth/charter.toml`, committed in the project repo.
   - The owner's verbatim mandate, which is immutable once confirmed.
   - `[purpose.<id>]` entries with `text`, `state` (`live` or `retired`) and `retired_reason`.
   - An `ext` table on each purpose that bathos preserves but does not interpret. This is the seam for future argument-scheme, rival and fork fields.
   - `bth charter draft` writes a proposal from the record. A draft has no standing.
2. **Confirmation.** `bth charter confirm` appends a `charter.confirmed` event carrying the file's sha256.
   - It reuses the Human Chair check: an interactive TTY or a chair token, refused under agent markers.
   - A charter counts as confirmed only while the file's sha256 matches the last confirmation. Any edit unconfirms it until the owner re-confirms. bth charter status reports whether the charter is confirmed or unconfirmed, and names the last confirmed sha256.
3. **Purpose ledger.** `bth charter pivot --reason ...` appends a `charter.pivot` event. It names every live purpose with `reaffirm` or `retire`, plus a `cheapest_next_test` field (`executed:<run>`, `cancelled:<reason>` or `none_pending`).
   - Skipping a live purpose is refused. This is a completeness rule, not a judgment.
   - A pivot is advisory until the owner confirms the resulting charter.
4. **Blind packet.** `bth bearings packet --project <slug>` writes a deterministic, hashed bundle of primary records only:
   - runs and their sidecars over the window;
   - ADRs, specs and backlog titles under the project's `.praxia/docs`. Handoffs, daily logs and other agent narrative are excluded (T2);
   - git log subjects.
   It never includes the charter, so the reader cannot echo it.
5. **Reading record.** `bth bearings record` takes the reader's structured output:
   - each purpose the record shows work toward, with at least 2 resolvable citations, using `run:`, `sidecar:`, `git:` and a new `doc:<path>@<blob-sha>#Lx-Ly`;
   - a reserved `atom:(slug, seal_sha256, start, end, claim_sha256)` kind that is parsed and, until allostery's ids are stable, always resolves to `flag_for_review` rather than failing;
   - open lines of work with no visible purpose.
   Bathos then compares the reading with the confirmed charter and stores `mapped`, `unmapped` (work toward no charter purpose) and `silent` (live purposes with no work in the window).
6. **Brief and labels.** `bth bearings brief` shows the owner the unmapped and silent items for each project. `bth bearings label <item> useful|noise|unsure` appends an owner-only event, using the same check as item 2.
7. **Cadence and due notice.** `bth bearings due` lists projects where a reading is due: active in the last 28 days and either 20 new runs or 7 days since the last reading. These numbers are starting values with no empirical basis, and T1 revisits them. The due state reaches people in two ways (T3):
   - **Dispatched cadence:** a scheduled routine or praxia dispatch calls `bth bearings due` and runs the readings.
   - **Due notice:** while a project is due, `bth run` returns one advisory line in its existing `warnings` channel: "bearings reading due for <slug>: N runs / D days since last reading". `bth sprint-audit` reports the same state as a signal. Each notice appends a `bearings.due_notified` event.
   - The notice says only that a reading is due, a fact. It carries no reading content, unmapped items or judgment, so it does not breach the observe-only decision: findings still go only to the owner's brief.
8. **Not in v1:** run-end push of reading content or findings (the due notice in item 7 is the one run-end line, and it carries no findings), detours, decision-follow and drift signals, the portfolio view and the rival/fork/argument features. Wiring step-back triggers to run end waits on debt #3236.

## Acceptance Criteria (v1, testable)

- **AC-1 Charter confirmation is human-only and hash-bound.**
  - Given a charter, when `bth charter confirm` runs with `CLAUDE_AGENT` set and no chair token, then it exits non-zero and writes no event.
  - Given a confirmed charter, when the file is edited, then `bth charter status` reports `unconfirmed` and names the sha256 it last confirmed.
- **AC-2 A pivot must dispose of every live purpose.** Given a confirmed charter with live purposes P1 and P2, when `bth charter pivot` names only P1, then it is refused and lists P2. When it names both and includes `cheapest_next_test`, then one `charter.pivot` event is appended and folds into the index.
- **AC-3 The seam is preserved.** Given `[purpose.p1.ext]` with arbitrary keys, when the charter is parsed, folded and drafted again, then the `ext` keys come back byte-identical. Given an `atom:` citation, when a reading is recorded, then that citation is stored as `flag_for_review`, never dropped and never counted toward the 2-citation minimum. That includes an `atom:` citation with extra fields, such as an ordered `context` list of `{start_line, end_line, role}` (allostery draft spec `261104_atom-context-chains`, not merged): the extra fields are stored verbatim and the citation resolves to `flag_for_review`.
- **AC-4 The packet is blind by content, deterministic, and free of narrative.**
  - Given a project with a handoff and a daily log under `.praxia/docs`, when the packet is built, then neither is in it and the manifest names both with reason `narrative`.
  - Given a project with a confirmed charter, when `bth bearings packet` runs twice over the same window, then both packets have the same sha256.
  - Blindness is defined by content, not path. The packet excludes `.bth/charter.toml`, `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, and any doc containing a purpose `text` or the mandate verbatim, using normalised whitespace. It lists each exclusion by path and reason in a manifest that is never shown to the reader.
  - Negative case: given a project whose ADR quotes a charter purpose (as colliculix's `.praxia/docs/decisions/260707_identity-charter.md` would), the packet must not contain that ADR, and the manifest must name it.
- **AC-5 Readings need evidence.** Given a reading where a purpose has fewer than 2 resolvable citations, when it is recorded, then that purpose is rejected with `STEPBACK_INSUFFICIENT_EVIDENCE`. Given a valid reading and a confirmed charter, then each reading purpose is stored as `mapped` or `unmapped`, and each live charter purpose with no mapped work as `silent`.
- **AC-6 Delivery and labels are recorded.** Every `bearings brief` render appends a `bearings.delivered` event listing the item ids shown. Labeling reuses the human-only check from AC-1; given an authorized session, every label appends a `bearings.labelled` event. Precision for any window can be computed from events alone with no other state.
- **AC-7 Replay with positive and negative controls, pre-registered.** A tracked script with a sidecar committed before the run builds packets from reconstructed history.
  - Positive control: colliculix cut at a **commit, not a date**: the tree at `14c3804` (2026-07-07 13:01:25 -0700). That precedes the earliest diagnosis artifact, the refoundation handoff `24ef9a6` (13:15), and the charter draft `588d175` (14:46). Both were checked with `git log --all` on 261104.
    - The packet reads only history reachable from the cut. Some of the record sat on `worktree-n4-calibration` before PR #1, so reachability is checked rather than assumed.
    - Negative check: a grep of the built packet for colliculix-drift-full-diagnosis, identity-charter, refoundation handoff, purpose ledger and .bth/charter.toml must return nothing. If it does, the control is contaminated and the run is invalid.
    - Expected: the seven purposes from the diagnosis show as multiple concurrent purposes, scored against the owner's labels.
  - Negative controls, named by the owner 261104: **tev_design** and **plegadx**, each over a window the sidecar pins by commit. tev_design is a hard negative: it has had local episodes such as k=4 vs k=9 and the target swap of 260714. The sidecar must say whether those count as drift at project scale or as claim-scale slips, before the reading is seen.
  - The sidecar states what each reading must contain, judged by the owner's labels, not by the reader itself. The A9 result is reported either way.
- **AC-8 Cadence and shadow exit are testable and pre-registered.** Given a set of projects, when bth bearings due runs, it lists exactly those active in the last 28 days with >= 20 new runs or >= 7 days since their last reading. Given a due project, `bth run` in it returns exactly one due advisory with no reading content and appends `bearings.due_notified`; given a project that is not due, it returns none. Before the first live reading, an evaluation sidecar fixes T1 and the excess-spend metric (261027 §8, including chains that paid off; not the `excess_spend_avoided` heuristic in `stepback/analysis.py`). It also counts confirmed charters among active projects, testing A10.

## Pre-mortem Record
**User:** Six months later it has failed. The likeliest story: charters were drafted for three projects and confirmed for one, because owner review was the bottleneck, so the cold reader compared against nothing elsewhere and its disagreements could not be told apart from noise. The owner's brief became one more unread page. Pivots happened inside sessions without a ledger event, because nothing at the moment of pivoting asked for one, and the ledger recorded 'unreconciled' quietly. A second failure path: the cold reader's three lines were plausible but generic ('project is developing X tooling'), passed every review, and never named a retired purpose. A third: the shadow phase never ended, because no exit criterion was pre-registered and nobody labelled the deliveries. Remediations for the spec: pre-register the shadow exit, with label counts and a decision date; limit the charter to the projects with activity in the last 28 days; give the cold reader output a mandatory structure (named purposes found in the record, each mapped to a charter purpose or marked unmapped, with citations); emit a run-end reminder only for an unreconciled pivot, which is a fact, not a judgment.
**AI:** _not recorded_

## Acceptance Criteria
**Given** Frame: agents do honest, pre-registered, green work that stops serving any larger goal, and nothing notices until the owner does. Design one bathos feature that makes a working agent aware, when it matters, of what its work serves across scales, and that records whether higher-level decisions moved. Fixed: reaches runs with no campaign; pushed, not remembered; warn by default, level 3 human-only; judgment is not mechanized into a pass/fail the agent can satisfy; no dependence on a hand-kept file without a forcing function; productive detours stay possible; scored by downstream outcomes; schema leaves room for allostery's atom link and for adversarial collaboration, argument schemes and multiverse work without building them. Negotiable: storage, whether a declared hierarchy exists at all, names, which scales ship first, extending versus wrapping step-back, coverage of non-run agent work.
**When** implementing Bearings v1, observe-only. (1) Per-project charter: purposes, each live or retired, plus the owner's verbatim mandate. An agent drafts it from the record; it counts only when the owner confirms it. (2) Purpose ledger: every pivot is an event that reaffirms or retires each live purpose and records what happened to the cheapest pending next test. Skipping a live purpose is refused. (3) Blind cold reader on a cadence tied to activity, not to derived signals. It reads only primary records (runs, sidecars, ADRs, specs, backlog; docs scope per T2) and writes a 'what this project appears to be doing' record, compared with the charter. Disagreement reaches the owner's brief. (4) Every delivery and every owner label is an event, so precision and a pre-registered excess-spend metric can be measured before any push to agents. Out of v1: detours, the argument and multiverse features, decision-follow and drift signals, and run-end push.
**Then** AC-1 to AC-8 under "Acceptance Criteria (v1, testable)" above hold.
