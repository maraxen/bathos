---
title: 'NLM research: exploratory vs confirmatory modes, holdouts, thresholds, agent skepticism'
description: NotebookLM (agentic-science notebook 3f0490aa) answers to 7 questions feeding the exploratory-mode contract spec; quotes spot-checked against raw responses
status: draft
task_id: 261015_exploratory-mode-contract
date: '261015'
confidence: ''
sources: ''
---
# NLM research: exploratory vs confirmatory modes, holdouts, thresholds, agent skepticism

> **Read this first: trust status.** Drafted by a Haiku research subagent; questions authored by the
> orchestrating session (see the spec's research-question list). Verified 261015 by the orchestrator:
> 13 quoted phrases grep-matched in the raw NotebookLM responses (no subagent fabrication). That
> proves only that **NotebookLM said it**, not that the primary source says it. NotebookLM is known
> to splice quotes. Nothing here is citable as a finding until the primary source is read.
> **Correction:** the holdout-strength ranking (Q2) is NotebookLM synthesis over *ML* literature,
> where "seed" means training initialization. In MD, seeds within one build are draws from one
> distribution (`~/.claude/rules/md-replicas-are-samples.md`); the spec remaps the ranking accordingly.
> Raw responses lived in `/tmp/nlm_exploratory_raw/`; that location is ephemeral, so the source IDs
> below are the durable handle. Not queried: notebook `1d786e78` (Scientific Rigor and Robustness in
> AI Research). It is the obvious cross-check before adversarial review.

Scope: query-only. Notebook `3f0490aa` ("agentic science bathos", 273 sources) was the only notebook queried. Raw answers and spill JSON are in `/tmp/nlm_exploratory_raw/`. Citation numbers in the answers were mapped to source IDs via the spill JSON. NotebookLM returned source IDs and text snippets, not source titles, so sources below are identified by source-ID prefix plus a snippet.

## Notebooks considered

| Notebook | id | Sources | Decision |
|---|---|---|---|
| agentic science bathos | 3f0490aa-7c70-4cd5-bfe7-94c3bd4a041f | 273 | KEPT and queried (all 7 questions) |
| Scientific Rigor and Robustness in Artificial Intelligence Research | 1d786e78-a2df-416b-aa2b-7ddf3524e012 | 57 | Kept as the best secondary notebook; NOT queried (gap) |
| Bayesian Inference | ebf033db-d1e4-44a4-8c18-075407504d57 | 17 | Considered; not queried |
| Molecular Dynamics: Theory, Methods & Practice | 9230d5f7-cff8-49a1-9ccd-8b65e8e207a7 | 161 | Considered for MD convergence; not queried (not in Q1-Q7) |
| Cascade router: selective prediction for agent verdict gating | fd47ef0a-a6da-44e7-8214-a15c0a9636dd | 185 | Considered for Q6; not queried |
| Cybernetics & AI Safety: Agentic Collapse + Viable System Model | d38e73be-97a1-4285-a706-24dea9323b99 | 192 | Considered for Q4; not queried |
| Soft Systems Methodology & AI-Assisted Analyst Cognition | 221aa1c6-d1d9-4d30-8a8c-88382b534ecc | 188 | Considered for Q4/Q5; not queried |
| Maraxiom - Literature Review Frameworks | 66c7345b-394b-47e1-95b1-ac94d6de8b83 | 18 | Considered; not queried |
| Research: Gate test targeting mismatch | 1ea288d8-9efb-4cf5-bb40-a6272ce9a89a | 66 | Considered; not queried |
| All other notebooks (music, food, habit, protein-design, hardware, etc.) | various | various | SKIPPED: off-topic |

## Per question

### Q1. Exploratory vs confirmatory in preregistration frameworks

- **Answer:** Preregistration does not forbid exploration. Its function is to keep the confirmatory/exploratory boundary visible. Exploratory work is labelled and reported separately, and is not pre-registered. `[cited]` source 497325f3 (COS summary): "Preregistration distinguishes confirmatory and exploratory analyses (Chambers et. al, 2014). Exploratory analysis is very important for discovery and hypothesis generation... results from exploratory analyses are more tentative, p-values are less diagnostic." Also `[cited]` source f03f4572: "Distinguishing Confirmatory..."
- **Answer:** The recommendation is to label, not to pre-register loosely and not to exempt. Exploratory analyses go in a separate "Exploratory Analyses" section. Registered Reports allow them at Stage 2 with justification. `[cited]` source ac76b3d3 (Ten simple rules for RR): "...clearly distinguished as exploratory... Results based on exploratory analyses should be considered tentative and in need of verification via further confirmatory research." `[cited]` source c323503e (Cambridge UP RR guidelines, as quoted): exploratory analyses at Stage 1 "will usually be desk rejected"; they "can be included at Stage 2 and need not be pre-registered."
- **Answer:** Loose pre-registration is discouraged. Planned exploratory analyses are an allowed middle ground. They are pre-registered with a fixed number of tests, and planned and unplanned analyses are reported separately. `[cited]` source 46c65544 / source 4db140ea-linked passage (COS template): "...researchers can preregister any planned exploratory analyses..." `[cited]` source db99913c: "a pre-analysis plan requires authors to register much more explicitly..."
- **Answer:** Reclassification is the rule when a plan changes. Once a confirmatory test is altered, it counts as exploratory. Papers should be sorted into (i) non-preregistered, (ii) preregistered confirmatory, (iii) non-preregistered but flagged exploratory. `[nlm-synthesis]` with supporting text in source 497325f3 and source 4db140ea.
- **Answer:** Yes, there is a hypothesis-generating registration concept. The COS FAQ says "At least one confirmatory test must be specified in each preregistration." Hypothesis-generating work has its own venues ("Exploratory Reports", e.g. Cortex, BMJ Open Science). Split-sample designs are the other route: wait to pre-register until exploration is done, or hold out part of the data. `[cited]` source 497325f3: "...randomly hold out a portion of the data and use part of it for exploration before opening up the reserved portion for confirmation."
- **Not in corpus:** de Groot (1956/1969 empirical cycle) and Tukey EDA. The answer says these sources are absent. The notebook has only a passing "EDA" mention (NeurIPS workshop FAQ). Nothing from the corpus on either.

### Q2. HARKing, forking paths, circularity; remedies; holdout strength

- **Answer:** HARKing is Kerr (1998) hypothesizing after results are known. It is distinct from fishing, which varies the analysis, not the hypothesis. `[cited]` source 93c8e221: "HARKing, also known as 'Hypothesizing After the Results are Known', is about the presentation of a post-hoc h..." `[cited]` source f03f4572: "Fishing Expeditions and Harking... Preregistration is often said to prevent fishing expeditions and so-called harking."
- **Answer:** The garden of forking paths (Gelman & Loken) invalidates classical p-values even without deliberate p-hacking. `[cited]` source 266f3f69 / 4a545b4f (Freedman's paradox passage): "Significance levels of standard procedures do not capture the va[riability]..."
- **Answer:** Circularity is a failure of data reuse. Test-set reuse and leakage are the common forms. Under sycophantic feedback, a conditional sample is treated as independent evidence. `[cited]` source 3af06144: "...an AI that generates responses by sampling examples consistent with..." (passage about sycophantic evidence generation, "Rational Analysis of the Effects of Sycophantic AI").
- **Answer (remedies):** Sample splitting is the first remedy: discovery/confirmation split, with the holdout used once. Reusable holdouts (Dwork et al., differential privacy / Sparse Vector) and the Ladder algorithm (Blum & Hardt) bound leaderboard error under adaptive reuse. `[cited]` source 04366ff7 / 5c443c8b: "There is a simple and natural holdout method that achieves small leaderboard error. It works like this: Given..." (Ladder). `[cited]` source ce75a9e1 (SVT / stage-wise DP guarantee).
- **Answer (holdout strength, weakest to strongest):** new seeds only check repeatability; new IID samples check in-distribution generalization; new systems, sites or physical experiments are the standard for scientific claims. Two AI systems sharing pretraining are not independent systems. `[cited]` source 497325f3: "Hold out data-sets or split samples. It may be difficult to fully prespecify your model until you have a chance..."
- **Answer (number to check):** "paired bootstrap and permutation tests reject the null (p<0.05) in 22% to 29% of comparisons" for identical architectures with different seeds. Sources c33291be / 9f05cea2 / 19c9dc7a list the statistics but I did not confirm the 22-29% figure in a snippet. `[nlm-synthesis]`-level until verified.

### Q3. Principled thresholds; SESOI; winner's curse

- **Answer:** Define a smallest effect size of interest (SESOI / MMES) before testing. Without it, hypotheses are unfalsifiable at large n. Pre-specifying it blocks HIMEing (shifting what counts as meaningful after seeing data). `[cited]` source e3af9da0: "The minimally meaningful effect size: A vital component of pre-registrations."
- **Answer:** Equivalence testing (TOST) is needed to claim no effect or equivalence. The bounds come from the SESOI. `[cited]` source 1f51d5d8 (Neyman-Pearson / power passage): "Studies involving Neyman-Pearson inference must include a statistical power analysis." Equivalence-test detail (±0.5 SD, 90% CI) is `[nlm-synthesis]`, with no snippet confirming it.
- **Answer:** The MDE must be at or below the SESOI. A design whose MDE exceeds the SESOI is underpowered by construction. `[nlm-synthesis]`.
- **Answer (number to check):** "Detecting d=0.2 typically requires 199 to 394 independent runs." This is a specific figure and I did not find it in a snippet. Treat as unverified.
- **Answer:** Calibrate against controls and null distributions: positive/negative controls, noise floors, permutation nulls, disjoint calibration partitions. `[cited]` source 19c9dc7a / 19c9dc7a-adjacent passages ("χ2 test with null as uniform distribution", "Data Leakage") and source 25c2aace (Elo / coin-flip null).
- **Answer (winner's curse):** Exploratory effect sizes are inflated by publication bias and by selection on significance. They must not set confirmatory thresholds or power. `[nlm-synthesis]` with a cited reference to Albers & Lakens (2018). The corpus snippet for this was not returned, so this is less well-sourced than the rest of Q3.

### Q4. Agent epistemics: literature over-trust, inflation, fabrication, skepticism

- **Answer:** Retrieval by embedding similarity merges opposing findings and yields plausibility-marker citations. `[cited]` source 0aea90a0 (Deep research agents passage): "Synthesis failures occur when agents generate fluent..." Citation accuracy in one benchmark was 40-80% (DeepTRACE, as cited in the notebook).
- **Answer:** Fabricated or misattributed citations are documented. `[cited]` source 9d55f7cb: "4.3.3. Citation Hallucination: Citation hallucination, the fabrication or misattribution of clinical references..."
- **Answer:** Affirmation bias and sycophancy: agents select evidence that flatters the user's hypothesis. `[cited]` source 3af06144 (sycophancy paper, see Q2).
- **Answer:** "Eureka instinct" and claim inflation. Autonomous agents call broken or trivial results breakthroughs. `[cited]` source 75458400: "This is the first ever comprehensive assessment of Semantic Entropy..." (from a paper-writing output).
- **Answer:** The AI Scientist's literature review was keyword matching; it called textbook techniques novel. `[cited]` source f7b784c3 / 59f44af1: "We evaluated the AI Scientist and found several critical shortcomings. The system's literature review process..."
- **Answer (mechanisms):** Claim-level provenance graphs (AAR standard: coverage, soundness, contradiction transparency, audit effort). Evidence-strength rubrics and credit ladders. Adversarial verification, asymmetric decomposition, debate. Negative-space and falsification-first queries. `[cited]` source f060bd24: "The starting point is a credit ladder... that separates three claims with different evidentiary..." `[cited]` source 12daede3 ("SOUND AGENTIC SCIENCE REQUIRES ADVERSARIAL EXPERIMENTS", Fa & Culjak).
- **Answer (number to check):** "over 146,000 invalid citations in 2025" and "23% more FLOPs" (AI Scientist efficiency claim). Specific figures, not confirmed in snippets. Unverified.

### Q5. Observations as records: logging patterns and anomalies

- **Answer:** Keep a dedicated anomaly register (quarantined exceptions, not folded into claims). Keep an assumption ledger (unchecked / challenged / verified / falsified). The Brenner-methodology attribution comes from the answer text; I did not find a snippet for it. Source b861d7bf is a GitHub-repo description ("operationalize a scientific method"). `[cited]` for the repo snippet only; the Brenner attribution is `[nlm-synthesis]`.
- **Answer:** Session logs must be append-only: never overwrite or summarize prior logs; record actions, decisions, and artifact hashes. `[cited]` source 75458400: "Create a new entry strictly and do not delete, overwrite, or summarise previous session logs. Focus..."
- **Answer:** Ground decisions in raw logs and execution traces, not summaries. `[nlm-synthesis]`. The 55% to 82% detection figure in the answer is unverified.
- **Answer:** Exploratory outputs go in an "Exploratory Analyses" section. p-values from them are non-diagnostic. `[cited]` source 497325f3 (see Q1).
- **Answer:** Evidence Packs with stable anchors (EV-001, EV-001#E1) let observations be cited precisely. `[nlm-synthesis]`, source not identified.
- **Answer:** K-Veritas-style nonrepudiation attestations bind reported numbers to a run. Mentioned in the answer; no snippet found. `[nlm-synthesis]`, unverified.

### Q6. Holding exploratory outcomes lightly; tentative labels; tiers

- **Answer:** Exploratory results are tentative and need confirmation. Unadjusted multiplicity means no confirmatory verdicts. Use descriptive labels and report effect sizes and CIs, not binary significance. `[cited]` source ac76b3d3 (RR rules): "Results based on exploratory analyses should be considered tentative and in need of verification..."
- **Answer:** Graduated bias-control taxonomy (PCI RR 2.0, 6 levels, by whether data were observed before IPA). `[cited]` source fda5f526 / e4244cb1: "Level-based taxonomy of bias control due to prior data observation: https://rr.peercommunityin.org/..."
- **Answer:** Claim ladders (decision prediction, rationale generation, decision explanation). `[cited]` source f060bd24 (credit ladder, Wang 2026).
- **Answer:** Three-state outcomes instead of binary: Supported / Contestable / Not supported (a forensic-media analogy); LABELING-ONLY / SPECIFIED / IRRESOLVABLE (an audit framework). `[nlm-synthesis]` for the tier names; the source for the tri-state is off-domain (synthetic media forensics) and is a weak analogy for this spec.
- **Answer:** Typed status in substrates: design targets vs unconfirmed estimates vs verified figures. `[nlm-synthesis]`.
- **Answer:** Uncertainty reported as GUM Type A / Type B. `[nlm-synthesis]`.

### Q7. bathos, POPPER e-values, sequential falsification, exploration to confirmation

- **Answer:** POPPER (Huang et al. 2025) turns hypotheses into measurable implications and runs sequential falsification with e-values. Anytime-valid error control follows from Ville's inequality. `[cited]` source 05f57c6c (arXiv 2502.09858, "Automated Hypothesis Validation...") and source 12daede3.
- **Answer:** Limitation: POPPER operates on static databases, so its experiments are re-analyses, not interventions. An eQTL false positive was reported. `[cited]` source 12daede3 (Fa & Culjak, "Sound agentic science requires adversarial experiments").
- **Answer:** Online FDR via LORD++ alpha-wealth, enforced structurally (a Haskell monad, Lean 4 and SPARK proofs). Naive testing of 2,000 hypotheses gave an FDR of 40.9%; the structured approach gave 1.1%. `[cited]` source 9e001ace (Sargsyan, "Structural Enforcement of Statistical Rigor in AI-Driven Discovery"). The specific numbers are not confirmed in snippets; the framework itself is confirmed.
- **Answer:** Reusable holdouts and the Ladder reduce adaptive overfitting; pre-registration via API before execution. `[cited]` source 04366ff7 / 5c443c8b (Ladder); Bisht et al. API pre-registration is `[nlm-synthesis]`.
- **Answer:** Promotion from observation to confirmation is through one of three gates: a fresh holdout, sequential error budgeting, or adversarial falsification. `[nlm-synthesis]`. This maps closely onto spec §3.4.
- **Not in corpus:** bathos itself. The notebook title is the only mention. No bathos design docs were found in the corpus.

## Implications for the spec

**Q1 (labelling vs exemption):**
- §3.2: The corpus supports the spec's direction: exploratory `[outcomes]` allowed, but labelled, and no confirmatory test required. Note the COS rule "at least one confirmatory test must be specified in each preregistration". That applies to a confirmatory registration, so an exploratory sidecar with no `[outcomes]` is consistent with it. State this explicitly in §3.2 so a reviewer does not read the exploratory class as a loophole.
- §3.2: Consider the "planned exploratory analyses" middle ground (pre-register the number of planned exploratory tests). This would strengthen `[observe]` with a fixed test count.
- §3.1: The reclassification rule ("a changed confirmatory plan becomes exploratory") should be a validator rule, not only a note.

**Q2 (holdouts):**
- §3.4: `[holdout]` kinds should be ranked by strength in the spec. The corpus ranks new seeds < new IID samples < new systems or physical experiments. Seeds should not be a valid kind for a promotion of a claim that is not about seed stability. Current list includes `new_seeds` and `new_builds` alongside `new_system`, with no ranking.
- §3.4: Add a rule that `held_out_split` must be a split defined before the observation, to rule out the discovery-to-holdout leak the corpus describes.

**Q3 (thresholds):**
- §3.4 and §3.5: The corpus supports `source` requirements and calibration runs. Add an explicit SESOI or equivalence-bound option in the `source` vocabulary (`sesoi:<why>`), since the corpus treats it as the primary principled source.
- §3.4: Lint rule "source = exploratory run" matches the winner's-curse warning. Keep it.

**Q4 (agents):**
- §3.3: `bth observe` should record provenance for each observation (which run, which output sha), as the spec already does. The corpus adds that an agent-generated observation should be tagged as agent-originated, so that the sprint-audit can measure the agent share.

**Q5 (observations):**
- §3.3: The corpus recommends append-only session logs and an anomaly register separate from claims. `bth observe` should be append-only (no update or delete), with corrections as new observation rows.

**Q6 (light holding):**
- §3.2 and §3.6: Consider a descriptive tier for observations (e.g. `tier = "descriptive"`) instead of only `outcome = 'observed'`, so that a single exploratory run can be marked as more or less tentative. The PCI RR bias-level idea (how much the data were already seen) maps onto this.

**Q7 (POPPER / sequential):**
- §3.4 and §7: Open question 3 (is `[holdout]` as attestation acceptable until #2686) is partly answered by the corpus: attestation without input hashes is the weakest form. Recommend stating in the spec that attested-only holdouts are capped at a lower tier of claim than hash-checked ones.

## Caveats

- **Source titles:** NotebookLM returned source IDs and snippets, not titles. I identified sources by ID prefix and snippet. The IDs are in the spill JSON files under `/tmp/nlm_exploratory_raw/`.
- **Only one notebook was queried.** The Scientific Rigor notebook (1d786e78) is the most obvious secondary source for Q1, Q2 and Q4 and was not queried. Its answers would be a useful cross-check.
- **Suspicious or unverified numbers** (do not cite without checking the primary source): 22-29% seed false positive rate (Q2); 199-394 runs for d=0.2 (Q3); 146,000 invalid citations in 2025 (Q4); 23% FLOPs increase in the AI Scientist case (Q4); 55% to 82% anomaly detection (Q5); 40.9% vs 1.1% FDR (Q7).
- **Plausible but not snippet-confirmed:** the Brenner anomaly-register attribution (Q5); K-Veritas nonrepudiation (Q5); the GRADE-like "Evidence Pack" anchors (Q5); equivalence-test details ±0.5 SD (Q3); Cortex/BMJ Open Science "Exploratory Reports" venues (Q1).
- **Off-domain analogies:** the tri-state forensic-media rubric and the LABELING-ONLY/SPECIFIED audit tiers (Q6) come from other domains. Use them as naming ideas, not as evidence.
- **Internal consistency:** the answers did not contradict each other on the core points (exploration is labelled, not forbidden; holdouts must be independent of the discovery data). Two answers used different names for the same credit ladder; this is naming only.
- **Not done:** no deep research, no sources added, no repo files edited. The Bash sandbox was not used. Other notebooks were not queried (see table).
