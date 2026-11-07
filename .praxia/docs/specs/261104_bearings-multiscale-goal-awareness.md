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

- [OWNER 261105] **AC-7 failed; project structure is rhizomatic.** AC-7 (run `530b1a32`, sidecar `scripts/analysis/replay_bearings_ac7.bth.toml`) recorded `fail`. The colliculix readers recovered all 7 diagnosed purposes (median 7), but the labels counted 2 project-level purposes, against 2 for tev_design and 4 for plegadx. Reading the readings, the owner ruled:
  - The colliculix drift is visible, and the labels' "sub" rule was too strict.
  - plegadx genuinely has several purposes.
  - tev_design has several interacting aims: the lagoon simulation, the design libraries and active learning. A probe on 261105 found those first appear on 2026-08-05, after the AC-7 tev_design cut of 2026-07-28, so that packet could not show them.
  - So a count of purposes cannot tell drift from a healthy project with several aims.

  Purposes form a graph, not a tree. Hierarchy is allowed, but cross-relevance that defies a strict hierarchy must be representable. **Results flow binds:** two aims belong together when one's results are used by another (`feeds`, `serves`). A shared method or code (`shares`) does not bind. Drift is effort split into groups that pass no results to each other. The reading record gains typed links and uncertainty (Design item 5), and AC-7b re-tests A9 under this rule.

- [261105] **AC-7b failed: the graph count runs opposite to the hypothesis.** Run `b85be024` (sidecar `scripts/analysis/replay_bearings_ac7b.bth.toml`, sha `13827865…`) recorded `fail`, with every instrument check clean:
  - controls pass;
  - 0 contamination;
  - 0 hash mismatches;
  - 0 rejected readings;
  - 9 readings;
  - 0 unlabelled bridges.

  | | colliculix (positive) | demistify (hard negative) | plegadx (negative) |
  |---|---|---|---|
  | Median drift groups, strict | 1 | 2 | 3 |
  | Median drift groups, lenient | 1 | 2 | 2 |
  | Groups in each reading (strict) | 1, 1, 1 | 3, 2, 1 | 3, 2, 5 |

  - Every colliculix reading gave one group under both rules, so the positive arm's failure does not depend on the unsure labels. plegadx reaches 3 only when unsure bridges are removed.
  - The readers were 2 Cursor and 1 Antigravity per arm, all at praxia's power tier. The labeller was 1 Opus agent: of 44 bridges, 29 supported, 12 unsure, 3 unsupported.
  - The registered decision applies: **do not build slice 3 on the blind reader. Re-open the mechanism with the owner.**
  - This is the second consecutive fail of A9. Neither run can tell "colliculix's drift does not show as disconnected effort in its record" from "the readers and scorer cannot detect disconnected effort at all". The synthetic controls test only the graph arithmetic, not readers plus scorer on a case known to drift.
  - **Not a finding:** group counts rise with packet size (colliculix 7 docs and 35 runs; demistify 20 and 5; plegadx 48 and 95). That ordering was noticed after the result, over 3 arms. It is a hypothesis for a next experiment, not an explanation of this one.
  - **Process faults found while running:**
    - praxia's agy executor cannot take a prompt over 128 KiB (debt 3289).
    - praxia rejects Cursor answers without a fence, and its retry trips on its own lock (debt 3290).
    - Both were worked around under registered rules; see the sidecar notes.
    - Both debts moved to praxia's store 261106 (now praxia 3298 and 3299).
- [261106] **AC-7c passed: readers plus scorer do see a planted drift.** Run `2032d774` (sidecar `scripts/analysis/replay_bearings_ac7c.bth.toml`, sha `b370eff0…`) recorded `pass`, with every instrument check clean:
  - controls pass, including the new merge and seam controls;
  - 0 leaked names;
  - 0 hash mismatches;
  - 0 rejected readings;
  - 9 readings;
  - 0 unlabelled bridges;
  - size matched.

  Design, chosen by the owner 261106: isochore and alynxr pass no results between them. The planted arm splices both over the same 14 days into one merged tree, with both names scrubbed to `proj`. Each control is one project alone over 28 days, scrubbed the same way. The planted arm has 92 items against 96 for the alynxr control, and fewer bytes.

  | | k1: isochore alone | k2: planted splice | k3: alynxr alone |
  |---|---|---|---|
  | Median drift groups, strict | 1 | 2 | 1 |
  | Median drift groups, lenient | 1 | 2 | 1 |
  | Groups in each reading (strict) | 1, 1, 2 | 2, 2, 2 | 1, 1, 1 |
  | Groups in each reading, no labels | 1, 1, 1 | 2, 2, 3 | 1, 1, 1 |

  - All 3 planted readings split along the true seam, with a purity of 1.0 (descriptive, fixed before the scored run).
  - Table run `c456be51` (outcome consistent) shows the readers made the split themselves. With no labels applied, the planted median is still 2, and no reader drew a binding bridge across the seam. The labeller did not create the split.
  - Readers: 2 Cursor and 1 Antigravity per arm, 0 tool events. The Opus labeller marked 36 bridges: 26 supported, 6 unsupported, 4 unsure.
  - Registered decision: the instrument sees a planted split, so AC-7b's result is about colliculix. Its drift does not show as disconnected effort in its record. **Re-open with the owner what drift means for Bearings before slice 3 is built on any signal.**
  - **Qualifier:** this plant is strong. The two halves differ completely in domain and vocabulary (free energy against sequence alignment). The pass shows a cross-domain split can be detected. It does not show that a split within one domain, like colliculix's, can be. Synthetic planted-structure work reports an "undetectable" regime for weak plants (Shi et al. 2019).
  - Review page (private): https://claude.ai/artifact/GFZAw4H4nkZRu8ZKJZEZtZ.
- [261106] **AC-7d passed, by the minimum margin: readers split a weaker, neighbouring-field plant.** Run `3e6410b2` (sidecar `scripts/analysis/replay_bearings_ac7d.bth.toml`, sha `5f4ef8ae…`, commit `f0e4a26f`) recorded `pass`, with every instrument check clean: controls pass, 0 leaked identifiers, 0 hash mismatches, 0 rejected readings, 12 readings, 0 unlabelled bridges, size matched, anchor hash matched.

  Design, chosen by the owner 261106: sweetprots and nanobody, two neighbouring-field projects that pass no results between them. The neighbour splice (m4) covers both over about 3.75 days. Each project alone (m1 nanobody, m3 sweetprots) covers 28 days. AC-7c's strong splice is rerun unchanged as a positive anchor (m2). Only identifiers are scrubbed; the word "nanobody" stays.

  | | m1: field A alone | m2: anchor (strong splice) | m3: field B alone | m4: neighbour splice |
  |---|---|---|---|---|
  | Median drift groups, strict | 1 | 2 | 2 | 3 |
  | Median, unsure counted as supported | 2 | 2 | 2 | 3 |
  | Median, no labels | 2 | 2 | 2 | 3 |
  | Groups in each reading (strict) | 1, 2, 1 | 2, 2, 2 | 2, 1, 3 | 3, 3, 3 |
  | Groups in each reading, no labels | 2, 2, 1 | 3, 2, 2 | 2, 1, 3 | 3, 2, 3 |

  - The anchor's median is 2, so the run is valid. m4 beats both single-project arms by exactly 1 group under all three label variants (3 against 2), so the outcome does not depend on the labeller.
  - All 3 m4 readings split along the true seam, with a purity of 1.0. **No feeds or serves link of any kind crossed the seam** (cross_seam_links 0, not only bridges), and no reading joined the halves.
  - m1's strict median (1) is below its lenient and no-label medians (2) although strict removes more bridges. Table run `93e2b7e3` (outcome consistent) shows the mechanism, the 10% effort-share rule rather than a defect: m1/reader1 goes from 2 groups in 4 components with no labels to 1 group in 5 components under strict removal, because one qualifying group was cut into pieces that each hold under a tenth of the effort. Drift groups therefore do not always rise as more bridges are removed, so the strict count can undercount.
  - Readers: 2 Cursor and 1 Antigravity per arm, 0 tool events. Two Cursor readings were format failures (no fence), not size, and were taken by the extraction rule. Antigravity start-up conflicts (praxia registered twice) were ruled infrastructure by the owner. A stale-file watcher false positive caused a second m2/m3 launch; the owner ruled the first complete reading counts. Full history in the sidecar's readers note.
  - Labeller: one Opus agent, no redo. 60 bridges: 39 supported, 8 unsupported, 13 unsure.
  - **Qualifiers.** (1) The margin is the minimum the rule allows. (2) The halves differ in record type as well as vocabulary: only sweetprots contributes docs, and sweetprots has no commits in the window, so the seam may be separable by source type alone, and a purity of 1.0 is partly easy by construction. (3) The pass shows the detection limit lies below this plant's contrast. It does not locate the limit, and it says nothing about a split within one project (literature note `research/261106_bearings-ac7d-literature-notes.md`).
  - Still open: the colliculix question and **the owner item on what drift means for Bearings, before slice 3 is built on any signal.** Owner 261106, asked after the pass: test a within-project split next (AC-7e). The definition of drift is not yet settled.
- [261106] **AC-7e pre-registered, no reader run yet** (`scripts/analysis/replay_bearings_ac7e.bth.toml`, commits `4d3d8d4d`, corrected in `c3045f8e`). Owner choices: within-project split; two sub-areas of one project; tev_design (lagoon simulation vs design libraries, selected by sidecar path; commits and docs left out); cap the larger aim by a deterministic hash-ranked sample. Same pass rule and anchor rule as AC-7d (p4 median at least 2 and at least 1 above each single-aim arm; AC-7c's splice as anchor).
  - **Correction, disclosed in the sidecar.** I first described the two aims to the owner as "owner-confirmed separate". The owner's 261105 review said tev_design's aims have "sub-goals that interact", and AC-7b used tev_design as a connected-aims negative. On being asked again, the owner ruled that the two aims pass no results to each other. That is the owner's statement and not shown by the record, and it conflicts with the earlier review. Supported cross-seam links in the readings would count against it.
  - Packets: p1 lagoon alone 54 items, p2 anchor 92, p3 library alone 27, p4 splice 47. The lagoon-alone size (45 runs) was fitted to the size rule from counts. 37 of p1's 45 runs come from one repeated script, so one group is cheap there.
  - Review page (private): https://claude.ai/artifact/2tqLFZWRyezKKGd4N2j7kQ. Both panels carry "dirty run, data not pinned" (untracked local files at run time); the audit is clean.

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
| A9 | A blind reader with primary records reproduces a diagnosis like colliculix's. | **Not supported as a count of purposes.** AC-7 run `530b1a32` failed: the readers found all 7 colliculix purposes, but purpose counts did not separate colliculix from the controls. |
| A9b | A blind reader's typed purpose graph separates colliculix's drift from projects with several connected aims: more groups of effort with no results passing between them. | **Unverified.** AC-7b tests it. |
| A12 | Readers can state, with citations, where one line's results are used by another. | **Unverified.** AC-7 readings were mostly flat lists; a few entries named links ("motivated the later library design", "no stated link"). AC-7b measures how many links are supported. **AC-7c (261106, descriptive, post-hoc):** in two unrelated projects spliced together, no reader's binding bridge crossed the seam, and every counted group cited one project only. |
| A10 | The owner will confirm a charter for each active project. | **Unverified.** This is the pre-mortem's first failure path. AC-8 measures it. |
| A11 | The colliculix charter stopped the sprawl. | **Unverified.** The project went idle the day the charter was accepted (`project_status.toml:694`), so stopping sprawl and stopping work cannot be told apart. |

## TBDs

| # | Open question | Owner | Must be settled before |
|---|---|---|---|
| T1 | Shadow exit numbers: how many labelled readings, the precision needed to enable push, and the decision date. These numbers go in the evaluation sidecar. | Owner (gate-tier) | First reading |
| T2 | **Resolved by the owner 261104: exclude narratives.** The packet holds ADRs, specs, sidecars, runs, backlog titles and git subjects. Handoffs, daily logs and other agent-written narrative are left out, because they carry the doing agent's framing. | Owner | Resolved |
| T3 | **Resolved by the owner 261104: more than one way.** Readings can be run (a) by a dispatched cadence (a scheduled routine or praxia dispatch that calls `bth bearings due` and runs readings) or (b) on demand, prompted by a due notice that says a bearings reading is needed (Design item 7). Bathos still does not dispatch. It computes due state and reports it. | Owner | Resolved |
| T4 | Whether portfolio scale (burn against priority across projects) is v2, and whether it belongs in bathos or praxia. | Owner | v2 |
| T6 | **A packet must fit one reader's reading budget.** AC-7b pilot (261105): the 28-day tev_design packet at `5f4856c7` is 2.84 MB (350 runs, 204 sidecars, 22 docs), and one Sonnet reader read 1.00 of runs and commits but only 0.23 of docs and 0.02 of sidecars. Cutting sidecars to their hypothesis and outcomes leaves 2.52 MB, because the hypotheses are long, and the docs alone are 0.71 MB. An ordinary active project can therefore outgrow a single blind reader. Options: split or staged reading, or a packet budget with a declared selection rule. | Owner | Before Bearings readings run on active projects |
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
   - open lines of work with no visible purpose;
   - **v2 (owner 261105):** a typed graph over purposes and open lines.
     - Every purpose and open line may carry `uncertain` (bool) and `uncertain_reason` (text): the reader's own channel for flagging doubt.
     - `links: [{from, to, type, citations, uncertain}]`, where `type` is one of:
       - `feeds`: a result of `from` is used by `to`.
       - `serves`: `from` exists for `to`'s sake.
       - `shares`: common method, code or data, with no result passing.
       - `competes`: a stated trade-off of effort, such as a deferral, a decline or a scheduling choice.
     - `from` and `to` name purpose ids, or open lines as `open:<i>`. A node may have several parents, and a link may cross any level.
     - Each link needs at least 1 resolvable citation. A self-link or a link to an unknown id is refused.
     - `links` is optional, so a v1 reading stays valid. Bathos validates and stores the graph and never judges it.
   - **Effort**, computed mechanically and never by the reader: each node's effort is the number of distinct resolved records it cites, counting runs and commits only. Docs and sidecars show intent, not spent effort.
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
- **AC-5b The graph is validated, not judged.** Given a reading with a link whose `from` or `to` is not a purpose id or `open:<i>`, or a self-link, or a link with no resolvable citation, when it is recorded, then it is rejected with `BearingsReadingInvalidError`, and nothing is recorded. Given a v1 reading with no `links`, it records exactly as before. Given a valid v2 reading, every link is stored with its type, uncertainty and resolved citations. `uncertain` and `uncertain_reason` are stored verbatim on every node.
- **AC-6 Delivery and labels are recorded.** Every `bearings brief` render appends a `bearings.delivered` event listing the item ids shown. Labeling reuses the human-only check from AC-1; given an authorized session, every label appends a `bearings.labelled` event. Precision for any window can be computed from events alone with no other state.
- **AC-7 Replay with positive and negative controls, pre-registered.** A tracked script with a sidecar committed before the run builds packets from reconstructed history.
  - Positive control: colliculix cut at a **commit, not a date**: the tree at `14c3804` (2026-07-07 13:01:25 -0700). That precedes the earliest diagnosis artifact, the refoundation handoff `24ef9a6` (13:15), and the charter draft `588d175` (14:46). Both were checked with `git log --all` on 261104.
    - The packet reads only history reachable from the cut. Some of the record sat on `worktree-n4-calibration` before PR #1, so reachability is checked rather than assumed.
    - Negative check: a grep of the built packet for colliculix-drift-full-diagnosis, identity-charter, refoundation handoff, purpose ledger and .bth/charter.toml must return nothing. If it does, the control is contaminated and the run is invalid.
    - Expected: the seven purposes from the diagnosis show as multiple concurrent purposes, scored against the owner's labels.
  - Negative controls, named by the owner 261104: **tev_design** and **plegadx**, each over a window the sidecar pins by commit. tev_design is a hard negative: it has had local episodes such as k=4 vs k=9 and the target swap of 260714. The sidecar must say whether those count as drift at project scale or as claim-scale slips, before the reading is seen.
  - The sidecar states what each reading must contain, judged by the owner's labels, not by the reader itself. The A9 result is reported either way.
  - **Result 261105: fail** (run `530b1a32`). See the decision log, OWNER 261105.
- **AC-7b Graph replay, pre-registered** (`scripts/analysis/replay_bearings_ac7b.bth.toml`, committed and approved before any reader runs).
  - Fresh readers write v2 readings with typed links, from the same packet builder.
  - A labeller who is not the reader checks only **bridges**: binding links (`feeds`, `serves`) whose removal would split a group. Each is marked `supported`, `unsupported` or `unsure`, and unsure bridges are scored both ways and reported. Nodes and non-bridge links are not labelled (owner 261105), because only a bridge can change the score.
  - The scorer counts **drift groups**: connected components under binding links, after removing unsupported bridges and ignoring direction, that hold at least a set share of the reading's effort. `shares` and `competes` links never join groups.
  - The scorer passes synthetic controls before any real score: a planted two-island graph must give 2, a planted fully-fed graph 1, and a graph joined only by `shares` links must give 2.
  - Controls are colliculix (positive) and two negatives with several connected aims. The tev_design cut moves to a date when the lagoon simulation, design libraries and active learning are all active, so the negative is the hard case the rule must survive.
  - **Result 261105: fail** (run `b85be024`; per-reading table run `2eeaeca9`, outcome consistent). See the decision log, AC-7b 261105.
- **AC-7c Planted-drift control, pre-registered** (`scripts/analysis/replay_bearings_ac7c.bth.toml`, committed before any reader ran). Two projects that pass no results between them are spliced into one neutralised packet, size-matched to a single-project control. The planted arm must reach a median of at least 2 groups and at least 1 above either project read alone.
  - **Result 261106: pass** (run `2032d774`; per-reading table run `c456be51`, outcome consistent). See the decision log, AC-7c 261106.
- **AC-7d Graded planted-drift control, pre-registered** (`scripts/analysis/replay_bearings_ac7d.bth.toml`, committed before any reader ran). Two neighbouring-field projects with no results flow are spliced over a short window and compared with each project alone, with AC-7c's strong splice as a positive anchor (median at least 2, or the run is invalid). Same thresholds as AC-7c.
  - **Result 261106: pass, by the minimum margin** (run `3e6410b2`; per-reading table run `93e2b7e3`, outcome consistent). See the decision log, AC-7d 261106.
- **AC-8 Cadence and shadow exit are testable and pre-registered.** Given a set of projects, when bth bearings due runs, it lists exactly those active in the last 28 days with >= 20 new runs or >= 7 days since their last reading. Given a due project, `bth run` in it returns exactly one due advisory with no reading content and appends `bearings.due_notified`; given a project that is not due, it returns none. Before the first live reading, an evaluation sidecar fixes T1 and the excess-spend metric (261027 §8, including chains that paid off; not the `excess_spend_avoided` heuristic in `stepback/analysis.py`). It also counts confirmed charters among active projects, testing A10.

## Pre-mortem Record
**User:** Six months later it has failed. The likeliest story: charters were drafted for three projects and confirmed for one, because owner review was the bottleneck, so the cold reader compared against nothing elsewhere and its disagreements could not be told apart from noise. The owner's brief became one more unread page. Pivots happened inside sessions without a ledger event, because nothing at the moment of pivoting asked for one, and the ledger recorded 'unreconciled' quietly. A second failure path: the cold reader's three lines were plausible but generic ('project is developing X tooling'), passed every review, and never named a retired purpose. A third: the shadow phase never ended, because no exit criterion was pre-registered and nobody labelled the deliveries. Remediations for the spec: pre-register the shadow exit, with label counts and a decision date; limit the charter to the projects with activity in the last 28 days; give the cold reader output a mandatory structure (named purposes found in the record, each mapped to a charter purpose or marked unmapped, with citations); emit a run-end reminder only for an unreconciled pivot, which is a fact, not a judgment.
**AI:** _not recorded_

## Acceptance Criteria
**Given** Frame: agents do honest, pre-registered, green work that stops serving any larger goal, and nothing notices until the owner does. Design one bathos feature that makes a working agent aware, when it matters, of what its work serves across scales, and that records whether higher-level decisions moved. Fixed: reaches runs with no campaign; pushed, not remembered; warn by default, level 3 human-only; judgment is not mechanized into a pass/fail the agent can satisfy; no dependence on a hand-kept file without a forcing function; productive detours stay possible; scored by downstream outcomes; schema leaves room for allostery's atom link and for adversarial collaboration, argument schemes and multiverse work without building them. Negotiable: storage, whether a declared hierarchy exists at all, names, which scales ship first, extending versus wrapping step-back, coverage of non-run agent work.
**When** implementing Bearings v1, observe-only. (1) Per-project charter: purposes, each live or retired, plus the owner's verbatim mandate. An agent drafts it from the record; it counts only when the owner confirms it. (2) Purpose ledger: every pivot is an event that reaffirms or retires each live purpose and records what happened to the cheapest pending next test. Skipping a live purpose is refused. (3) Blind cold reader on a cadence tied to activity, not to derived signals. It reads only primary records (runs, sidecars, ADRs, specs, backlog; docs scope per T2) and writes a 'what this project appears to be doing' record, compared with the charter. Disagreement reaches the owner's brief. (4) Every delivery and every owner label is an event, so precision and a pre-registered excess-spend metric can be measured before any push to agents. Out of v1: detours, the argument and multiverse features, decision-follow and drift signals, and run-end push.
**Then** AC-1 to AC-8 under "Acceptance Criteria (v1, testable)" above hold.
