---
title: 'NLM research: claim-appraisal structure (for/against, basis, non-compensatory grading)'
description: NotebookLM (Scientific Rigor notebook 1d786e78) answers on appraisal frameworks, doubt/trust taxonomies, grading and appraiser independence, feeding the spec's appraisal payload
status: draft
task_id: 261015_exploratory-mode-contract
date: '261015'
confidence: ''
sources: ''
---
# NLM research: claim-appraisal structure (for/against, basis, non-compensatory grading)

> **Read this first: trust status.** Drafted by a Haiku research subagent; questions P1–P6 authored
> by the orchestrating session for the spec's §3.3.2 appraisal payload. Verified 261015: 11 quoted
> phrases that the payload design relies on grep-matched in the raw spill files under the main
> checkout's `.praxia/nlm/261015-08{24..33}*.json`. That proves NotebookLM said them, not that the
> primary sources do. Not citable as findings until the primaries are read. Flagged in the
> caveats as unverified: Olszewski "~18%" (the answer's own arithmetic), and Oh ">30% of papers".

Notebook: `1d786e78-a2df-416b-aa2b-7ddf3524e012` ("Scientific Rigor and Robustness in Artificial Intelligence Research", 57 sources).
Queries run: notebook_describe x1, notebook_query x6 (P1-P6). No follow-ups, no source adds. Citation keys below are the bracket indices in each query's spill file (the notebook answer does not expose source UUIDs); spill paths are listed at the end.

## Notebook summary (notebook_describe)
ML benchmarking and methodology: statistical rigor, experimental protocols, preregistration, researcher degrees of freedom, multi-seed replication, variance decomposition, transparent reporting, standardized reproducible frameworks.

## P1. Structured appraisal frameworks in corpus
- ML Reproducibility Checklist (NeurIPS / Pineau et al.): dimensions = models & algorithms (math setting, complexity, linked code with dependencies); theoretical claims (statement, assumptions, proof); empirical results (data collection and sample size, data/simulation link, exclusions and preprocessing, train/val/test allocation, hyperparameter range and selection, exact number of runs, measures, error bars, central tendency and variation, compute infrastructure). [P1 spill, ref 7] "Clearly defined error bars."
- REFORMS checklist (Kapoor et al., 32 items, 19 researchers): data leakage, evaluation design, reporting of uncertainty. [P1 spill, refs 8-9]
- SOTA claim fragility diagnostic (Oh): magnitude (Cohen's d), consistency (win rate), stability (breakdown point), plus composite fragility rate. [P1 spill, refs 10-13]
- AsPredicted preregistration (8 questions): data collection status, hypothesis, DV, conditions, analyses, outliers/exclusions, sample size, other. [P1 spill, ref 14] "Specify exactly which analyses you will conduct."
- Qualitative preregistration (Haven & Van Grootel): aim, literature use, rationale, tradition, sampling, stopping rule, collection script. [P1 spill, ref 16]
- AI-agent preregistration template (Vaccaro): agent selection, scaffolding/environment, prompt engineering, experimental design, response processing, analysis pipeline, reporting. [P1 spill, ref 18]
- AbGen ablation evaluation (Zhao et al.): importance, faithfulness, soundness. [P1 spill, ref 21]
- Popperian appraisal protocol (INV-1..INV-6, four-way labels corroborated / partial / refuted / abstain). [P1 spill, refs 25, 29]
- ACM/cTuning artifact appendix checklist (components, resource constraints, dissemination). [P1 spill, ref 32]
- Nonrepudiation protocol (Keita & Homan): tamper-evidence, author-key separation, independent verifiability. [P1 spill, ref 35]
- Meta-research categories (Ioannidis et al.): methods, reporting, reproducibility, evaluation, incentives. [P1 spill, refs 37-38]
- Not in corpus: Toulmin, assurance/safety cases, CER, GRADE domains (GRADE only mentioned in passing), Cochrane RoB 2 / ROBINS-I. [nlm-synthesis of P1 answer's own NOT IN CORPUS lines]

## P2. Reasons to doubt a result (taxonomy as presented)
- Pineau et al. NeurIPS reproducibility report, seven failure classes: data access / distribution gaps; model or procedure underspecification; code unavailability or implementation errors; metric underspecification; improper statistics; selective reporting and adaptive overfitting; overclaiming beyond evidence (e.g. "insufficient number of experiments, mismatch between hypothesis & claim"). [P2 spill, refs 1-2]
- Lipton & Steinhardt flaw patterns: explanation vs speculation; unidentified sources of empirical gain (e.g. gains from hyperparameter tuning credited to architecture); mathiness; misuse of language. [P2 spill, ref 3]
- Vaccaro researcher degrees of freedom (AI experiments): tuning sampling seeds/temperature until results appear; unequal "thinking room" across conditions; toggling memory or context per outcome; prompt iteration without disclosure; post hoc task or stimulus choice; outcome-contingent parsing; post hoc DVs and exclusions; selective reporting, HARKing. [P2 spill, refs 4]
- Oh SOTA diagnostics: magnitude (d < 0.2 "negligible relative to cross-task variability"), consistency (win rate < 0.6 "outperforms on only a minority of tasks despite a higher average score"), stability (breakdown point < 0.2 "relies on a small subset of favorable datasets"). [P2 spill, refs 5-6]
- Bouthillier et al. variance decomposition: learning-procedure variance (data sampling, augmentation, initialization, dropout, visit order) and hyperparameter-optimization variance (search method, arbitrary boundaries). [P2 spill, refs 7-8]
- Baseline and benchmark pitfalls (Yang et al.): undertuned baselines ("basic GNNs can actually outperform... with careful hyperparameter tuning"); benchmarks that do not test the hypothesized challenge; no quantitative evaluation metric. [P2 spill, refs 10-11]
- Financial-forecasting benchmark gaps: unequal tuning budgets (G1); single-seed evaluation, "impossible to distinguish genuine architectural advantage from stochastic variation" (G2). [P2 spill, ref 12]
- AbGen ablation error taxonomy: misalignment with context; ambiguity / difficulty of reproduction; partial or incomplete ablation; insignificant module; logical inconsistency. [P2 spill, ref 13]
- Keita & Homan verification gap: no mechanism binds reported numbers to executed computation; checklists verify disclosure only. [P2 spill, refs 14-17]

## P3. Reasons that increase trust
- Preregistration and registered reports: separates confirmatory from exploratory; in-principle acceptance before data or compute. [P3 spill, refs 4-14]
- Multi-source variance accounting: multiple seeds, data splits, batch order, augmentation, HPO; variance decomposition with LMEM/VCA and ICC; nonparametric multi-dataset tests (Wilcoxon, Friedman with critical difference); P(A>B) with bootstrap CIs. [P3 spill, refs 16-33]
- Effect sizes with uncertainty; win rate across tasks; breakdown point. [P3 spill, refs 34-38]
- Adversarial testing and execution oracles: HumanEval+ style dense tests as severe tests; length-matched placebo and labels-only controls. [P3 spill, refs 39-46]
- Independent third-party reproduction and clean-room reimplementation. "Having a copy of the code ... is not a guarantee that this code is correct" (Pineau et al.). [P3 spill, refs 47-51]
- Complete versioned artifact release (data, code, checkpoints, frozen configs, seed routines). [P3 spill, refs 52-54]
- External validation: OOD data, external environments, different temporal horizons. [P3 spill, refs 55-59]
- Guidance to check, not assume: YES. "Artifact evaluation checks whether code runs, not whether it produced the reported numbers." [P3 spill, ref 2]. "Checklists verify claims about the paper... None of them bind reported results to real runs." [P3 spill, ref 62]. Olszewski et al. (cited there): of ML papers at security venues, 40% included artifacts and 44% of those ran, i.e. about 18% working (see caveats). Le Pochat & Joosen: badges "may give a false sense of research validity". [P3 spill, ref 1]. "High intra-rater reproducibility is consistency, not validity." [P3 spill, ref 3]

## P4. Grading strength and combining reasons
- Grade each dimension on explicit thresholds: d >= 0.2; win rate >= 0.6 (or P(A>B) >= 0.75); breakdown point >= 0.2. [P4 spill, refs 1-6]
- Fragility rate is non-compensatory: "when any of these elementary tests fails, the comparison is fragile". [P4 spill, refs 14-15]
- Severity floor per test (default 0.20; tests below it do not contribute to corroboration). [P4 spill, ref 9]
- Asymmetry of refutation: "The kappa value is a record of past test-survival, not a probability of truth." [P4 spill, refs 16-17]
- Dual threshold: significance and meaningfulness both required. [P4 spill, ref 18]
- Warnings against averaging: Demsar quoting Webb, "averaging error rates across domains is very meaningful" questioned; averages "susceptible to outliers". [P4 spill, refs 19-20]
- Keep dimensions separate: "aggregate scores alone cannot characterize the reliability of a comparison" [P4 spill, ref 23]; "prevents any single label from serving as an uncritical proxy for superiority" [P4 spill, ref 24]; "Never emit a monolithic 'this looks right' or 'LGTM' judgment" [P4 spill, ref 26]; Hagmann et al. caution against a single reproducibility statistic [P4 spill, ref 27].
- No source gives a combined overall confidence formula. Combining is by per-dimension reporting, not summation. [nlm-synthesis]

## P5. Who appraises; recording checked vs inferred
- Do not rely on author self-report: a checklist "verifies intention, not execution" [P5 spill, ref 4].
- Do not self-grade; use tool oracles; any LLM judge must not be the same instance that produced the answer. [P5 spill, ref 5]
- Attestation signing key held by an independent service, not the author; verifiable "without trusting the author". [P5 spill, ref 6]
- Independent artifact-evaluation committees and community reproducibility challenges (outside volunteers). [P5 spill, refs 7-8]
- Adversarial reviewers / red teams: NOT IN CORPUS as a prescribed appraisal role. The sources note reviewers' "mindset of looking for reasons to reject", i.e. hypercriticality is a problem, not a method. [P5 spill, refs 10-11]
- Record theory-load of readings: "record it in the trace as 'oracle_interpretation_disambiguated_in_favor_of_H': true" [P5 spill, ref 3]. Append-only audit trace. [P5 spill, ref 14]
- Artifact evaluation verifies usability, "not on verifying that exact numbers match" [P5 spill, ref 17].
- Blind-then-reveal staged protocol: evaluators score before seeing the reference, then may adjust. [P5 spill, ref 19]

## P6. Claim vs evidence gap
- Named "claim-evidence gap" / over-claiming; SOTA language used on narrow evidence, reported as >30% of papers at major conferences (claimed by Oh; treat number as unverified). [P6 spill, refs 2-3]
- Calibrate wording: "ranks first on this benchmark" rather than "state-of-the-art". [P6 spill, ref 7]
- Report win rates, effect sizes, breakdown point beside the mean. [P6 spill, refs 7-12]
- State scope boundaries and failure conditions; "we report an equivalence there, not a proof of zero". [P6 spill, refs 13-14]
- Separate verdicts: "not supported" (excluded) vs "not met" (CI does not rule out effect) vs "ceiling-limited non-detection". [P6 spill, ref 15]
- Label exploratory vs confirmatory; report the whole specification space including nulls. [P6 spill, refs 19-20]
- Reviewer check: "does the claim language accurately reflect the reported statistics?" [P6 spill, refs 21-22]

## Candidate fields (consolidated; source question in brackets)
- claim_text, claim_scope, claim_strength_wording [P1, P6]
- evidence_for: reproducibility items (code_released, code_ran, numbers_reproduced), n_seeds/runs, error_bars_reported, data_split_stated, hyperparameter_budget_matched [P1, P3, P5]
- evidence_against / doubt categories (closed taxonomy): seed_variance, weak_or_untuned_baseline, benchmark_not_diagnostic, small_n_or_few_tasks, outlier_dominance, missing_ablation, underspecified_metric, improper_statistics, selective_reporting, researcher_degrees_of_freedom, data_leakage, distribution_shift, overclaiming, code_error_or_unavailable [P2, P1]
- trust_increasers (closed taxonomy): preregistered, independent_replication, artifact_released, multi_seed_ci, external_validation, effect_size_with_uncertainty, adversarial_or_oracle_test, placebo_control [P3]
- per-dimension magnitude / consistency (win_rate) / stability (breakdown_point) as separate fields, never summed [P4, P2]
- severity or strength grade per item, with a floor [P4]
- verification_status per item: disclosed | executed_by_appraiser | reproduced_by_third_party | attested (signed) [P3, P5]
- checked_by / appraiser_role: author | independent | adversarial (adversarial not sourced) [P5]
- observed vs inferred separation, with theory-load note on interpretation [P5]
- claim_evidence_gap: scope_claimed vs scope_supported; boundary conditions; verdict label from {corroborated, partial, refuted, abstain} or {not supported, not met, ceiling-limited} [P6, P4]
- preregistration reference and exploratory flag [P1, P6]

## Caveats
- Spill files hold the full references. Bracket numbers in the answers index into those references, not into source UUIDs. Resolve before citing a source by title.
- Splice risk: the P2 answer attributes the seven-class taxonomy to Pineau et al. and the P3 answer quotes Olszewski's 40% / 44% / ~18% figures second-hand via Keita & Homan. The ~18% is an arithmetic product the answer computed itself. Verify against the original paper before quoting.
- The P6 ">30% of papers" claim is attributed to Oh and is unverified.
- Several answers mix in Popperian/K-Veritas and agent-protocol material (P1 refs 25-35, P5 refs 3, 14-15). Those are one author group's protocols, not consensus. Treat as one source family.
- The P4 "GRADE separates evidence quality from recommendation strength" is a passing mention, not a GRADE domain list. P1 explicitly says GRADE domains are NOT IN CORPUS.
- Adversarial/red-team appraisal is NOT IN CORPUS, so the "adversarial" field in P5 is design inference, not sourced.
- Some answers end with offers to continue; none were taken up. No follow-ups were run.

Spill files (full upstream payloads):
- P1: /home/marielle/projects/bathos/.praxia/nlm/261015-082402__p1-which-structured-frameworks-for-appraising-a-__0e416703.json
- P2: /home/marielle/projects/bathos/.praxia/nlm/261015-082632__p2-what-specific-categories-of-reasons-to-doubt-__ef607080.json
- P3: /home/marielle/projects/bathos/.praxia/nlm/261015-082741__p3-what-categories-of-reasons-that-legitimately-__9bcbf72c.json
- P4: /home/marielle/projects/bathos/.praxia/nlm/261015-083029__p4-how-should-the-strength-of-each-reason-for-or__1ae6106c.json
- P5: /home/marielle/projects/bathos/.praxia/nlm/261015-083220__p5-is-there-guidance-on-who-should-appraise-a-re__69d01ed6.json
- P6: /home/marielle/projects/bathos/.praxia/nlm/261015-083327__p6-do-these-sources-discuss-the-gap-between-what__5e79815d.json
