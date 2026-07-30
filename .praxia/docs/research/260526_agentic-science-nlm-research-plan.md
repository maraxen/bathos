# Bathos Agentic Science — NLM Research Plan
*Date: 2026-05-26 | Notebook: `3f0490aa-7c70-4cd5-bfe7-94c3bd4a041f`*

Gap: 7 new sources post-date the May-2026 synthesis (~144 sources, notebook 3f0490aa).
None are cited in the current design docs. This plan adds them and extracts targeted
insights for the bathos open design questions.

---

## Mode Taxonomy

- **Deep Research** (`start_deep_research`) — broad landscape; NLM finds and synthesizes
  sources you have not loaded yet. Use when discovering what exists, not interrogating what
  is already loaded.
- **Query** (`notebook_query`) — targeted synthesis against sources already in the notebook.
  Use when specific papers are loaded and you want precise answers.

Add sources first; then run the queries for those sources. Deep Research prompts run
independently — they find their own sources.

---

## Sources to Add

Add to notebook `3f0490aa` via `source_add(source_type=url, ...)`:

| Tag | URL | Paper | Maps to |
|-----|-----|-------|---------|
| S1 | `https://arxiv.org/abs/2502.18864` | Co-Scientist — Towards an AI co-scientist (Google, Feb 2025) | Hypothesis capture mechanism Q |
| S2 | `https://arxiv.org/abs/2502.09858` | POPPER — Agentic Sequential Falsifications (Stanford, ICML 2025) | Outcome evaluation design |
| S3 | `https://arxiv.org/abs/2504.08066` | AI Scientist-v2 — Agentic Tree Search (April 2025) | Drift detection signals |
| S4 | `https://arxiv.org/pdf/2604.22080` | Sound Agentic Science Requires Adversarial Experiments (April 2026) | Enforcement scope limits — ⚠️ may already be loaded; check before adding |
| S5 | `https://arxiv.org/pdf/2603.29632` | Multi-Agent Collaboration for Automated Research (March 2026) | Gate bypass failure modes |
| S6 | `https://arxiv.org/pdf/2601.12607` | Cloud-based Multi-Agentic Workflow for Science (Jan 2026) | Orchestrator-worker patterns |
| S7 | `https://arxiv.org/pdf/2510.21652` | AstaBench — Benchmarking AI Agents for Scientific Research (Oct 2025) | Rigor signal discriminability |

---

## Deep Research Prompts
*Broad landscape queries. Run independently — NLM finds relevant sources.*

---

### DR-1 — Hypothesis Integrity Mechanisms: What Works Empirically
**Mode:** Deep Research
**Finding target:** Evidence on which commitment mechanisms actually reduce post-hoc
rationalization; empirical comparisons between approaches, not just system descriptions.

> I need to compare three approaches for making an AI agent's hypothesis commitment
> tamper-evident: (a) single-shot hypothesis declaration before experiment execution,
> (b) multi-round debate or critique before commitment, and (c) cryptographic or hash-based
> locking of declared intent. What does the research literature say about which of these
> approaches actually reduces post-hoc rationalization by AI agents? I am looking for
> empirical evidence or principled arguments — not just descriptions of systems that use
> each approach. What has been shown to work versus being procedural theater?

---

### DR-2 — Computational Provenance + Scientific Integrity: The Nonrepudiation Gap
**Mode:** Deep Research
**Finding target:** Systems or papers that bind execution records to declared hypotheses
in a tamper-evident, machine-queryable way; "experiment nonrepudiation" as a concept.

> What work exists on bridging the gap between computational provenance systems (which
> record what ran) and scientific pre-registration (which records what was intended)?
> Are there systems or papers that cryptographically or otherwise bind execution records
> to pre-declared hypotheses, enabling a machine-queryable audit for whether reported
> results match declared intent? Search for work on "experiment nonrepudiation,"
> hypothesis-execution binding, or pre-registration enforcement for computational or
> agentic experiments — particularly from 2024 to 2026.

---

### DR-3 — Where Agentic Research Systems Fail at Rigor
**Mode:** Deep Research
**Finding target:** Empirical failure mode data — observed behavioral patterns where AI
research agents diverge from scientific rigor, especially under execution pressure or
across iterations.

> What does the empirical literature from 2024–2026 show about specific failure modes
> of agentic AI research systems with respect to scientific rigor? I am interested in
> documented cases or systematic studies where AI agents drift from their stated
> hypotheses, inflate results, selectively report, or otherwise fail the standards of
> sound science. What behavioral signatures in execution traces predict these failures?
> What tool-level or architectural interventions have been shown to reduce them?

---

## Queries
*Targeted synthesis from already-loaded sources. Add the tagged sources before running.*

---

### Q-1 — Co-Scientist: Commitment Artifacts vs. Rationalization
**Mode:** Query
**Load first:** S1
**Maps to:** §6 open Q — hypothesis capture mechanism: option (a) MCP `hypothesis`
param at call-time vs. option (b) pre-generated sidecar file whose hash is stored

> In the Co-Scientist system, what mechanisms prevent an agent from retroactively
> adjusting its stated hypothesis to match results already obtained? Does the
> generate→debate→evolve tournament commit hypotheses at any point before experiment
> execution, and if so, what artifact represents that commitment? What does this tell
> us about the minimum artifact that provides meaningful integrity evidence?

---

### Q-2 — POPPER: What Single-Run Pre-Registration Can and Cannot Guarantee
**Mode:** Query
**Load first:** S2
**Maps to:** Statistical validity of DuckDB SQL outcome conditions evaluated against
a single run; "outcome evaluation on failure" open question

> POPPER uses e-values in a sequential testing framework to control Type-I error across
> multiple falsification experiments. For a system that evaluates pre-registered outcome
> conditions against a single run's results: (a) what does POPPER's framework say about
> the statistical validity of single-run confirmation — is a single pre-registered pass
> condition meaningful? (b) what is the minimum requirement (number of conditions,
> structure of falsification tests) to avoid the weakness of basic pre-registration?
> (c) when an experiment exits with an error but result files still exist, how does
> POPPER's falsification framing handle partial results?

---

### Q-3 — AI Scientist-v2: Hypothesis Persistence vs. Abandonment in Tree Search
**Mode:** Query
**Load first:** S3
**Maps to:** §8 drift detection signals — behavioral audit artifacts that distinguish
legitimate hypothesis revision from post-hoc rationalization

> In AI Scientist-v2's agentic tree search, how does the system handle the decision to
> revise versus abandon a hypothesis mid-search? Are there audit artifacts in the
> execution trace that mark when a hypothesis revision occurred? How does v2 differ from
> v1 in its treatment of hypothesis persistence across search iterations? What would a
> researcher need to log at run time to distinguish deliberate revision from drift?

---

### Q-4 — Adversarial Experiments: What Pre-Registration Cannot Achieve
**Mode:** Query
**Load first:** S4
**Maps to:** §11 Q3 enforcement scope — what tool-level pre-registration can and cannot
guarantee; the gap between "experiment was pre-registered" and "experiment was designed
to falsify"

> What does this paper mean by "adversarial experiments" — adversarial testing of
> hypotheses (designed to disprove), adversarial agents (red-teamers), or adversarial
> framing of experimental design? What is the gap between an experiment that was
> pre-registered and one that was designed adversarially? For a solo researcher with no
> second agent to challenge their hypothesis, what is the practical implication — can
> pre-registration alone substitute for adversarial design, or does something structural
> change?

---

### Q-5 — AstaBench + Multi-Agent Collab: Which Rigor Signals Are Discriminating
**Mode:** Query
**Load first:** S7, S5
**Maps to:** §11 Q4 bypass rate threshold; `bth sprint-audit` anomaly flags — which
are actually informative vs. noise

> AstaBench evaluates agentic research ability across 2400+ problems. The multi-agent
> collaboration study examines failure patterns empirically. What evaluation dimensions
> do these sources identify as most discriminating between agents that genuinely test
> hypotheses versus agents that confirm their priors? Which dimensions map to observable
> signals in an experiment catalog: outcome label entropy across runs, residual branch
> rate, sidecar bypass rate, unfired decision branches, extra result fields outside the
> declared schema? What distributions or patterns from these sources suggest when a
> signal is informative rather than noise?

---

### Q-6 — Multi-Agentic Orchestration: Gate Bypass Failure Modes
**Mode:** Query
**Load first:** S5, S6
**Maps to:** §3.4 MCP gate error format — what makes structured error payloads
actionable vs. bypassed by orchestrating agents

> From these papers on multi-agent orchestration for science: what behavioral patterns
> lead worker agents to bypass validation gates rather than remediate them? What
> properties of error payloads — structure, fields, error taxonomy — correlate with
> reliable automated remediation versus confusion or silent bypass? Are there documented
> cases where gate errors caused agent loops or stalls rather than correct remediation?

---

## Execution Order

1. **Add sources** — S1 through S7 (check if S4 is already in the notebook before adding)
2. **Run DR-1, DR-2, DR-3** — independent, any order; save each result as a named note
3. **Run Q-1 through Q-6** — sequentially; each builds on accumulated note context

## Output Convention

After each prompt, save result as a notebook note:
- `[DR-1] Hypothesis integrity mechanisms`
- `[DR-2] Nonrepudiation gap`
- `[DR-3] Agentic rigor failure modes`
- `[Q-1] Co-Scientist commitment artifacts`
- `[Q-2] POPPER single-run validity`
- `[Q-3] AI Scientist-v2 drift signals`
- `[Q-4] Adversarial experiments scope`
- `[Q-5] Rigor signal discriminability`
- `[Q-6] Gate bypass failure modes`

These notes become queryable context for the bathos design update session.
