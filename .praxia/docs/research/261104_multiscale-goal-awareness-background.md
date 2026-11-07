---
title: 'Multi-scale goal awareness for science agents: background'
description: From-memory literature grounding, measured bathos surface and documented drift episodes behind the multi-scale goal awareness brainstorm
status: draft
task_id: 261104_multiscale-goal-awareness
date: '261104'
confidence: 'mixed: measurements are from a tracked run; episodes were read at source; citations are from memory and none was checked'
sources: 'bth run da651e36; bathos source at origin/main 893634e1; memory notes and docs named inline; literature from memory, at the owner''s request'
---
# Multi-scale goal awareness for science agents: background

Companion to the step-back research note [261027](261027_advisor-checkpoints-step-back-research.md) and the success-modes catalog [261028](261028_success-modes-catalog.md). Those notes ask when to step back and what good looks like. This one asks what an agent has to be aware of, across scales, and what bathos can actually see today.

## How far to trust each part

- **Measured.** Section 1 comes from run `da651e36-7f36-4a54-ba49-41e553fdc48e` (`scripts/analysis/census_goal_attachment.py`, outcome `pass`, synthetic controls passed). Spike queries had already shown the headline values, so that run is a reproducible record, not a blind test.
- **Read at source.** Sections 2 and 3 name the file for each statement.
- **From memory, unchecked.** Everything in section 4 is cited from memory, as the owner asked. Author and year may be wrong, and any effect size quoted is approximate. Items marked *(corpus)* have a copy in `~/scratch/litreview_corpus`. A copy there means the source exists, not that the summary here is right. Check before citing outside this repo.

## 1. What bathos can see

| Measure | Value |
|---|---|
| Runs with no campaign, all time | 4,710 of 8,318 (56.6%) |
| Runs with no campaign, since 2026-10-01 | 2,110 of 2,982 (70.8%) |
| Recent runs with a sidecar | 96.1% |
| Recent runs with `agent_session` | 29.7% |
| Readable recent sidecars naming a `decision` on an outcome | 843 of 868 (97%) |
| Distinct recent sidecar paths no longer on disk | 352 of 1,220 |
| Campaign rows with null mode and name | 245 of 373 |
| Rows in `campaign_edges` | 0 |
| Step-back artifact files under `~/.bth/catalog` and `~/projects/*/.bth` | 0 |

Read together:

- **Campaigns are the wrong attachment point.** Most recent work is outside them, and the share is rising. Sidecars reach almost all of it.
- **The next step is often written down, but the 97% overstates it.** The confirm scaffold writes `decision = "TODO: ..."` (`new_experiment.py:161,175,182`), and the census counted any non-empty string, so placeholders are included. The share that is placeholder text was not measured. A sample of 16 real strings was specific but local ("read which gate failed", "do not retune"), so it says little about higher scales. Nothing compares the stated decision with what happened next.
- **Step-back has no record on this machine.** It is 8 days old, so this may be early. It does not show the feature is unhelpful: the owner reports it helps, and a skill can change behaviour without leaving a record. It does show that nothing can yet measure that help.
- **Limits.** The artifact count covers this machine only. The 352 missing paths are probably removed worktrees, which was not checked; the 97% is over the readable ones. A sidecar naming a `decision` says the field was filled, not that the text is any good. Some scaffolded text may be generic.

## 2. How the current upward check works

- **Pull-only.** `run_stepback_triggers` and `evaluate_all_triggers` are called from `cli_cyclopts.py`, `mcp.py` and inside `stepback/` (`brief.py`, `engine.py`). `runner.py` has no step-back reference, though the `bathos-step-back` skill says triggers run after runs complete. `campaigns.add_run` (`campaigns.py:546`) reads escalation state that already exists and never evaluates a trigger. So an agent in a rabbit hole has to remember to ask whether it is in one.
- **Campaign-keyed.** Every trigger takes a campaign. A run with no campaign has nothing to fire on.
- **A push channel exists.** `bth run` and the MCP `run` tool return `warnings` in their result, so text can reach the agent at run end without a new hook. `hooks/preflight_hook.py` is a PreToolUse pattern for the moments before a run.
- **Lineage is mostly absent.** `parent_run_id` is set on 7 of 2,983 recent runs (spike query). "What came next" has to be inferred.
- **The log can grow.** New event kinds with their own fold have precedent (`runlog/fold_output_copies.py`).
- **The only cross-project goal record is flat and unconfirmed.** `~/projects/project_status.toml` holds goal, endstate, priority and reconsider per project. All 21 rows in the advisor view were still drafts when it was generated on 09-28.

## 3. What has gone wrong, by scale

| Scale lost | Episode | Source | What surfaced it |
|---|---|---|---|
| Project aim | colliculix: 7 claimed purposes in 27 days. "Each honest null was spent changing the question, not answering a standing one." | `~/.claude/projects/-home-marielle-projects-colliculix/memory/colliculix-drift-full-diagnosis-260707.md` | A deliberate diagnosis session |
| Project aim | tev-design: 5 review passes hardened a false premise; the synthesis swapped the literal ask for an easier target. | `~/.claude/projects/-home-marielle-projects-tev-design/memory/feedback_verify_foundational_claims_and_target.md` | Owner |
| Claim | tev_design: k=4 vs k=9 read as a compute question; it was about substate signal. | 261028 catalog, S3 | Owner |
| Run | praxia #381: 74 minutes and 2.5M tokens on a measurement null on every row. | 261027 note, section 5 | Not restated here |
| Across sessions | Replicate pooling corrected in 6 sessions, 3 after the global rule. | 261027 note, section 10 | Owner |

Colliculix also supplies the positive case: a charter, a purpose ledger where every ADR reaffirms or retires each purpose, a pivot protocol, and gate-tier reservation (`~/projects/colliculix/.praxia/docs/decisions/260707_identity-charter.md`, section 7). It worked, was built by hand, and was built after the damage.

The pattern across these: **discipline holds at the scale where a check exists, and the failure lands one scale above it.** This repeats the 261027 finding that mechanizing a lesson moves the failure.

## 4. Literature, grouped by what it contributes

### Goals form a hierarchy, and attention slides down it

- **Simon, *Administrative Behavior* (1947)** *(corpus)*: means-ends chains, where each level's end is a means to the next. **Simon, "On the concept of organizational goal" (1964)**: goals act as constraints as much as targets. **"The architecture of complexity" (1962)**: near-decomposable systems have levels with different clock rates.
- **Rasmussen, abstraction hierarchy (1986)**: at any level, the level above answers *why* and the level below answers *how*.
- **Vallacher & Wegner, action identification (1987)**: people drop to lower-level descriptions of what they are doing when an action becomes difficult, and rise when it flows. This is close to a mechanism for the rabbit hole: difficulty is what pulls attention downward.
- **Powers, perceptual control (1973); Carver & Scheier (1998)**: higher levels set reference values for lower ones.
- **Altmann & Trafton, "Memory for goals" (2002)**: suspended goals decay and are recovered through cues at the moment of resumption, not through a stored stack. So the timing of a reminder matters more than the existence of a record.

### Which changes of direction are legitimate

- **Lakatos (1970)**: a research programme is progressive if its changes predict something new and degenerating if they only absorb failures. This is a close description of colliculix.
- **Laudan (1977)**: pursuit and acceptance are different appraisals. Bathos has a lot of acceptance machinery (outcomes, claims, the Union Gate) and nearly nothing for pursuit.
- **Fujimura, "Constructing do-able problems" (1987)**: a problem is do-able when it is aligned at once across the experiment, the laboratory and the wider social world. An ethnographic account of multi-scale alignment.
- **Meehl (1990); Scheel et al. (2021)**: the derivation chain from theory through auxiliaries to the observation. The weak link is usually not the test itself.
- **Platt (1964); Hamming, "You and Your Research" (1986)**: which experiment would disprove this; what are the important problems, and why are you not working on them.
- **Chalmers & Glasziou (2009)**: research waste begins at choosing the question. **GRADE indirectness (Guyatt et al. 2011); estimands (ICH E9(R1); Lundberg et al. 2021)**: the gap between what was measured and what was asked.
- **The guard against over-correcting. Steinle (1997); Rheinberger (1997); Merton & Barber**: exploratory experimentation and serendipity are real. **Dunbar (1995, 2001)**: in vivo lab studies found a large share of findings were unexpected and that how a lab handles them matters. Some rabbit holes are the discovery. A design that treats all detours as waste would be wrong.

### Reflection, and how often to reconsider

- **Argyris & Schön, double-loop learning (1978); Schön (1983)**: single-loop learning changes the action; double-loop changes the governing goal. Reflection in action differs from reflection on action.
- **Bratman (1987); Cohen & Levesque (1990); Kinny & Georgeff (1991); Schut & Wooldridge (2001)**: intentions exist to spare constant replanning. Never reconsidering and always reconsidering both lose, and the best rate depends on how fast the world changes. So reconsideration should be scarce and triggered, and a chatty feature would be as wrong as a silent one.
- **Staw, "Knee-deep in the big muddy" (1976); Arkes & Blumer (1985)**: escalation of commitment. **Klein, pre-mortem (2007)** *(corpus has Klein's books)*. **Kahneman & Lovallo (1993)**: inside versus outside view. **Kahneman, *Thinking, Fast and Slow*** *(corpus)*.
- **Gollwitzer, implementation intentions (1999); Gollwitzer & Sheeran (2006)**: if-then plans made in advance raise follow-through (roughly d = 0.65, from memory). A sidecar's `[outcomes].decision` is an implementation intention, and it is already written.

### Project management

- **Singer, *Shape Up* (2019)**: appetite (decide what the question is worth before starting), circuit breakers, and naming rabbit holes in the pitch. Closest practical source for detours with a budget.
- **Reinertsen, *Principles of Product Development Flow* (2009)**: WIP limits, cost of delay.
- **Hubbard, *How to Measure Anything*** *(corpus)*; **Howard (1966)**: value of information. A measurement is worth what it could change in a decision.
- **Cooper, Stage-Gate (1990); the Heilmeier catechism.** **Gersick (1988)**: teams reassess at their temporal midpoint.
- **Locke & Latham (2002)**: proximal goals work best nested under distal ones. **Ordóñez et al., "Goals Gone Wild" (2009); Kerr (1975); Goodhart; Manheim & Garrabrant (2019)**: narrow goals invite gaming and neglect.
- **Beer, *Brain of the Firm* (1972)**: the viable system model recurs at every level, with an algedonic channel that bypasses routine reporting. Already the frame for step-back (261027, section 3).
- **Rasmussen (1997); Vaughan (1996); Dekker** *(corpus has the Field Guide)*: drift into failure is built of locally reasonable steps.
- **Gigerenzer et al.; Gawande, *The Checklist Manifesto*** *(both corpus)*: simple rules and short checklists work when used at the right moment. Already applied to gates in praxia.

### Agents

- **Laird, Newell & Rosenbloom, Soar (1987)**: an impasse creates a subgoal, and the subgoal has a return. **Sutton, Precup & Singh, options (1999); Dayan & Hinton, feudal RL (1993); HTN planning**: hierarchical control with explicit termination.
- **Cemri et al. (2025), on multi-agent failure; Arike et al. (2025), on goal drift in LLM agents**: task derailment and drift are measurable. Titles and findings recalled loosely.
- **Context-engineering write-ups (Anthropic 2025; Manus 2025)**: goal recitation and just-in-time retrieval, because compaction loses goals. Practitioner reports, not controlled tests.
- **From 261027, grounded in notebooks there**: PAGRL, Carl and Zochi, POPPER; no source shows an agent re-grounding its own goal without a human. **That gap still stands in this note**, since nothing here was searched.

### Metascience, and the allostery corpus

- **Clark & Tetlock, adversarial collaboration (2022)** *(corpus)*: rivals agree on a discriminating test before seeing data.
- **Steegen et al., multiverse analysis (2016)** *(corpus)*; **Simonsohn et al., specification curve (2020); Gelman & Loken, forking paths (2013)**: report across defensible choices rather than picking one.
- **Walton, Reed & Macagno, *Argumentation Schemes* (2008)** *(corpus, seal weak)*: the argument from practical reasoning has critical questions: are there other means, what are the side effects, does this conflict with other goals, is the goal still wanted. Asked at each scale, these are step-back questions with a formal pedigree.
- **Mercier & Sperber (2011)**: reasoning performs better in adversarial exchange, so a critic should be a different agent.

**Why this matters for the seam.** All three treat a choice point as an object: a goal can be attacked (Walton), forked (Steegen) or contested (Clark & Tetlock). A design whose unit is an *open decision with options* can later take all three as additions, because options and the evidence that discriminates them already exist on the node.

**Seam facts from the allostery session (261104, by message; not read at source).** Atom ids are not stable. The intended link key is `(doi or slug, seal text_sha256, start_line, end_line, claim_sha256)`, a non-match should flag for review and never drop silently, argument-scheme typing is undecided and should be an additive optional field, and a verified quote does not mean a checked claim (2 of 7 sampled atoms needed fixes). The work is on an unmerged branch.

## 5. What this adds up to

These are syntheses, not findings.

1. **Scale is a clock as much as a level.** Runs turn over in minutes, aims in months. A single cadence is wrong at all but one scale.
2. **Attention slides down under difficulty.** So the moment to surface the upward view is when work is going badly, and that moment has a signature in the record.
3. **Storing a goal does little. Cueing it at resumption does more.** This argues for push at run end, session start and after compaction.
4. **The load-bearing object is the link, not the node.** A goal tree says what exists. The link says what this work would change above it. Value of information, derivation chains and "what would change your plan" all point at the link.
5. **Reconsideration has an optimum.** Scarcity is a design requirement, since warnings that always fire get ignored.
6. **Detours need a licence, not a ban.** Soar and Shape Up both give a detour a budget and a way back.
7. **Whatever the agent can see, it will satisfy.** So an independent reading and downstream outcomes have to carry the measurement.

## Open gaps

- No literature was checked. A proper search is still needed before any of section 4 is cited outside this repo.
- The allostery port inventory (`261104_lit-review-bathos-port-inventory.md`) was not found from this session.
- Bathos sees runs. A rabbit hole with no runs, such as the tev spec-hardening episode, leaves no trace here.
