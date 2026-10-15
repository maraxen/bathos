---
title: Adversarial review of the exploratory-mode contract spec
description: Spec-challenger findings C1-C25 and spec-defender rulings for specs/261015_exploratory-mode-contract.md, run sequentially
status: complete
task_id: 261015_exploratory-mode-contract
date: '261015'
---
# Adversarial review: exploratory-mode contract

The challenger ran first. The defender then ruled on its findings: 10 conceded, 14 partly conceded, 1 rebutted (C15). The orchestrator re-checked the code claims behind blockers C1, C2, C4 and C5, and the defender's C17 correction (`query.py:345`), by reading the code. It also ran the C7 session-id spike (spec §2). Both reports below are model output, appended verbatim.


## Challenger

SPEC CHALLENGE: 261015_exploratory-mode-contract. Verdict: not_ready (confidence high). No files edited.

Verified correct: sidecar.py:215/273 (stage default and canonical set), validate.py:578-711, new_experiment.py:51-79, campaigns.py modes, mcp.py:1990 (#2080), sidecar.py:37 OutcomeSpec.source, linter.py:870 (warning-only), prereg.py:181, mcp.py:1782 derived_from, cli_cyclopts.py:752-763, parity.py:249-252.

== BLOCKERS ==

C1 [blocker] §3.1/§4, migration. If stage_name is absent, the sidecar is exploratory (sidecar.py:311). The catalog also writes 'exploration' when the value is absent (catalog.py:176). Every new-experiment scaffold explicitly sets exploration together with pass/marginal/fail (new_experiment.py:53-79). Evidence from a sibling project: ~/projects/ksweep-bench/denxity/scripts/experiments has 18 [experiment] sidecars, most named *_confirmatory. They contain 0 stage_name keys and 0 `source` lines. Under S1 every one of them becomes exploratory, fails validation on bare numeric cutoffs (so bth run refuses), its whole history renders as tentative, and the Union Gate rejects it. History cannot tell declared exploration from defaulted exploration.
FIX: add a slice S0 that ships first.
- Class derives from an explicit stage_name key. If the key is absent and [outcomes] is present, the class is legacy-confirmatory.
- Runs older than the S1 release (schema_version bump, or a cutoff timestamp) get class 'legacy' and render as they do today.
- `bth lint` lists sidecars that have explicit or implicit exploration plus [outcomes] plus campaign/claim membership, so the author can restage them.
- The exploratory source rule is a warning for one release, then becomes an error.

C2 [blocker] §3.1. An unknown stage_name is silently coerced to exploration with a log warning (sidecar.py:312-317). A typo such as stage_name="validaton" turns a confirmatory sidecar into an exploratory one: outcomes become optional and verdicts become tentative.
FIX: a non-canonical stage_name is a validation error. authoring/models.py:36-41 already hard-errors on the write path.

C3 [blocker] §3.2.1/§3.4, HARKing laundering. [holdout] is required only when derived_from_observation is set. The drift rule explicitly exempts a rename ("unless the stem is renamed"), and drift is keyed on the resolved script path (prereg.py:201-210).
Path: explore under exploration and tune cutoffs freely (drift is advisory). Copy the script to a new stem with stage_name=validation and the tuned cutoffs, set source="provisional:...", and skip --from-observation. Result: a clean confirmatory verdict on the same data.
FIX:
(a) Lint ERROR when a confirmatory sidecar's script_sha256 (schema.py:55 already records it), or its canonicalized [outcomes], matches a prior exploratory run in the project, and the sidecar has no [holdout].
(b) Every confirmatory sidecar declares either [holdout] or an explicit `data_unseen = true` attestation.
(c) Forbid `provisional:` when derived_from_observation is set or (a) fires.
(d) Warn when a confirmatory literal equals, to 2 significant figures, a value in the source exploratory run's metadata. This is what makes the rule "Lint rejects run:<exploratory>" non-trivial; today it only catches an explicit citation.

C4 [blocker] §3.2.1. "tentative:" is render-only and the stored outcome stays 'pass'. Machine consumers read the raw value:
- requires_pass_stem: `outcome = 'pass'` with no stage filter (prereg.py:318), used as a HARD gate for validation (cli_cyclopts.py:754-762). An exploratory tentative pass satisfies a confirmatory prerequisite.
- claim.py:355 (parity).
- sequential e-values (campaigns.py:408,421).
- bth sql users.
FIX: exploratory runs always store outcome='observed'. The evaluated label goes in a separate field (a `tentative_outcome` column or a metadata key). Every `outcome='pass'` consumer is then safe by construction. §4's "no new column" must change.

C5 [blocker] §3.2 runner. derive_pass_labels on an outcome-less sidecar is empty, so is_failure_outcome('observed') is True (sidecar.py:584-602). Consequences:
- runner.py:1128 opens a post-mortem obligation on every exploratory run.
- compute_evalue scores the run as fail evidence, (1-alt)/(1-null) (sidecar.py:636).
- The campaigns.py:421 neutral list doesn't include 'observed', so the sequential threshold locks.
- A tentative 'fail' also opens an obligation, which contradicts "held lightly".
FIX: S1 must
- add 'observed' to the is_failure_outcome early-return, compute_evalue's neutral cases, campaigns.py:421, runner.py:1049 and RESERVED_OUTCOME_LABELS;
- skip obligations for the exploratory class;
- make sequential campaigns refuse exploratory runs. §3.6 is silent on sequential.

C6 [blocker] §3.2.1 vs §4. "Later runs under the drifted hash are recorded as exploratory" needs either overwriting stage_name (falsifying the declared stage) or a new column. §4 forbids the column. Further problems:
- Autonomous mode already DENIES on drift (prereg.py:480-496), so the rule only ever applies in collaborative mode.
- Drift is a whole-file sha, so a comment edit or an added appraisal (C7) triggers reclassification.
- It is unspecified whose class governs the drift: the baseline run's or the current sidecar's.
FIX:
- Add an `epistemic_class` run column, written at run time from stage_name, drift and holdout.
- Define drift for this rule as a change to the canonicalized stage_name + [outcomes] + [result_schema].
- State that the autonomous deny stays for confirmatory sidecars.
- The class governing a stem's drift is the strictest class ever declared on it.

C7 [blocker] §3.3.2 / §7.8, independence. Validation sees a hand-written TOML file:
- "appraiser_session recorded automatically" cannot happen in a file written by Edit.
- The validator has no record of "the session citing the value".
- `[review.literature.appraisal]` is a single table, so the "second appraisal by a different appraiser" has nowhere to live.
- Adding an appraisal changes the sidecar sha, which triggers drift and reclassification (C6).
FIX: appraisals are ledger records written only via `bth appraise` (CLI and MCP), which stamps session id, timestamp and sha. The sidecar holds `appraisal_ids = [...]`. The citing session is the one stamped on the bth run that first executes the confirmatory sidecar, so add `agent_session` to runs.
SPIKE (unverified): does Claude Code expose a session id env var to subprocesses, and do subagents get distinct ids?

== MAJOR ==

C8 §3.4 ladder. literature_prediction is rank 3, "independent of our data entirely". That conflates where the hypothesis came from with the independence of the test data. A run-backed observation "promoted" as literature_prediction can test on the same data.
FIX: holdout kinds describe the test data only. literature_prediction is a hypothesis-source tag. Run-backed observations always need a data holdout of rank ≥ 1.

C9 §3.4 vs md-replicas rule. held_out_split has no unit, so a time or frame split of the same trajectories ranks 2. There is also no n.
FIX: `[holdout] unit ∈ {frame, seed, build, system}` and `n_units`. Rank equals the level of the unit; a split counts as rank 2 only when its unit is ≥ build. Pre-register the unit of analysis, testing contrasts at build level per the rule. A rank-1 holdout with n_units=1 cannot support a condition contrast.

C10 §3.4 split timing. The check uses obs.created_at, which the agent controls: look, declare the split, then call `bth observe`. "Manifest sha recorded with an earlier timestamp" has no registry.
FIX: compare against the timestamps of the observation's source run(s). The split manifest must be the sha-recorded output of a bth run that predates them.

C11 §3.4 table vs §7.6. The table allows new_seeds for seed-stability claims; the decision says seeds are a kill test only. Also "append refuted as a superseding row" conflates a correction (supersedes) with a status.
FIX: drop the table carve-out. Add a separate status event kind.

C12 §3.3.2 gaming.
- severity is self-assigned. A generic single-source / asserted / minor entry always satisfies rule 1.
- addressed_by can point at the same appraiser's own `for` entry, which neutralizes a decisive objection.
- Rule 5's byte-equality is beaten by a trailing period.
- The read_depth order is ambiguous (full-text vs methods).
- "uses_as exceeds use_tier" needs a total order, and contrast-context has no place in it.
FIX:
- Category severity floors: leakage-or-circularity, measurement-validity and unconverged-sampling are ≥ major unless dismissed with evidence of basis ≥ inspected.
- Addressing a decisive or major entry needs a `for` from a different appraiser, or basis ≥ executed.
- Normalize case, whitespace and punctuation before comparing.
- Publish explicit ordinal tables.
- New sprint-audit signal: the share of appraisals whose `against` entries are all asserted.

C13 §3.2/§3.6 cutoff hiding. The threshold lint keys on numeric literals (linter.py:38,907). Two escapes:
- Move the cutoff into the script as a bool result field (`condition = "is_hub"`). This dodges both the error and the §3.6 signal.
- In a sanity entry, `x > 1+1+1` passes the "only 0/1" rule.
Also, `source` is one per branch (sidecar.py:37), so "each literal needs a source" can't be represented. And source is free text: the scaffold invites rule-card ids (new_experiment.py:166) while the spec uses lit:/run:/sesoi:/provisional:.
FIX:
- Require `source` on every non-residual branch whether or not it has literals.
- A closed grammar: lit:<ref> | run:<id>:<field> | sesoi:<value>:<why> | provisional:<why> | card:<id>.
- Sanity entries may compare fields with 0/1 only, with no arithmetic on literals. Check via DuckDB's parsed AST.

C14 §3.6. Union Gate coverage reads only claim_discriminates and ignores class and outcome (claim.py:908-931). Calibration runs, which are allowed into confirmation campaigns, can therefore cover clauses. In exploration campaigns the gate only warns (campaigns.py:1214).
FIX: filter coverage to the confirmatory class. Forbid claim_discriminates on exploration and calibration sidecars. Specify the behaviour for each mode, including sequential.

C15 §3.1. campaigns.py:399-408 re-parses the current sidecar from sidecar_path to compute e-values. If class is computed the same way, editing stage_name retroactively reclassifies past runs: a way to dodge a failure.
FIX: class comes only from the immutable run row.

C16 §3.5 calibration.
- S1 adds only an exploratory branch, so calibration sidecars without outcomes still fail validation (validate.py:579).
- The calibration outcome label is undefined.
- run:<id>:<field> resolution doesn't require a successful run.
- The author can cherry-pick among many calibration runs.
- Calibration code can drift freely.
FIX: add a calibration validator branch. Resolve run:<id> only when exit 0 and the outcome is not error or invalid_measurement. Cite by stem with all runs and their spread reported. Apply confirmatory drift rules to calibration.

C17 §3.3 ledger. A separate Parquet ledger bypasses the runlog event store: emit_or_legacy (runlog/emit.py:172) is the single flag branch every write site uses. There is no warm table, compaction or fold spec. `bth observe <run_id>` can fail on a run that hasn't been compacted yet.
FIX: observation.* event kinds with fold rules, plus a cool-tier fallback for run lookup.

C18 §3.2 runtime semantics are unspecified:
- precedence among exit-code error, differential, sanity and outcome evaluation;
- sanity that evaluates to NULL or references a missing field;
- declared [observe].outputs that are missing, and how names match BTH_OUTPUT_DIR paths;
- reusing invalid_measurement contradicts its documented exclusivity (sidecar.py:264-268), and differential_status consumers (claim.py:853) are affected.
FIX: a precedence table, a sanity_status column, and a decided outcome for each case.

C19 Verification plan (§8) is inadequate. Add negative controls for C1, C2, C3 (rename laundering), C4 (a tentative pass must not satisfy requires_pass_stem), C5 (no obligation; neutral e-value), C12 (filler and self-rebuttal rejected), figure-read never reaching threshold-source, and C14. The dogfood step must check the record via `bth compact` + `bth sql`.

== MINOR ==

C20 `planned_tests` has no counting unit (a run evaluates one label).
C21 The SESOI-vs-MDE lint can't be deterministic while sesoi is free text; it needs a number.
C22 §2 errors:
- "stage_name changes behaviour in only two places" is false: linter.py:233 (ERROR) and sprint_audit.py:166,262 also use it.
- ablation falls through both requires_pass_stem branches (cli_cyclopts.py:754/763) and parity.py:252, although the spec classes it confirmatory.
- Citation sidecar.py:285 should be :268; :129 should be review_tier at :189.
- hypothesis is not validated today (validate.py:485-731), so "question in place of hypothesis" alters nothing.
C23 Slices:
- S1 must co-ship with S2, and S3b must merge into S1 or exploratory sources render as verdicts.
- S4 depends on S3 and S5.
- S3 is undersized.
- mcp.py:3828 new_experiment_tool and agent_plugin.json are missing from the S2/S4 file lists.
- postmortem.unexpected_observations overlaps `bth observe`.
C24 There is no Assumption Ledger. Tags:
- verified false/contradicting: C1, C2, C4, C5, C6, C14.
- unverified: session ids (C7); the output sha available for --figure; NotebookLM claims.
C25 Run-less observations: the `bth observe --ref` example has no appraisal, so it reads as optional (blind acceptance). Make it required. Research Q7 also recommends capping attested-only holdouts at a lower claim tier; the spec only labels them.

## Defender

SPEC DEFENSE: 261015_exploratory-mode-contract. Verdict: needs_revision (confidence high). No files edited.

Challenger claims I checked and found wrong:
- C17: "bth observe <run_id> can fail on an uncompacted run" is wrong. get_run(..., allow_cool_fallback=True) already exists at query.py:345 (PR #92). The spec only needs to say observe calls it.
- C1: the sidecar count is 18 (challenger) vs ~19 (orchestrator). Immaterial.
Every other code citation I re-read held up: sidecar.py:268/311-317/584-636, prereg.py:181-233/318/480-496, campaigns.py:399-421, claim.py:908-931, cli_cyclopts.py:752-763, linter.py:233/907, sprint_audit.py:166/262, schema.py:55, models.py:41, emit.py:172, postmortem.py:35.

KEYSTONE: §4's "no new run column" assumption is the real defect. It was a design preference, not a user decision. Adding one immutable column, epistemic_class, written at run time resolves C1, C4, C6 and C15 together. Filtering on stage_name cannot work, because catalog.py:176 writes 'exploration' for an absent key, so legacy confirmatory rows are indistinguishable from real exploration.

== Per finding ==

C1 CONCEDE. §3.1 says stage_name "defaults to the right value", and §4 says "loosens nothing". Yet the 18 denxity *_confirmatory sidecars with no stage_name would become exploratory, fail on bare cutoffs, and render as tentative. The spec is silent on migration.
Minimal fix:
- Class comes from an explicit stage_name.
- Absent key → class 'legacy', which keeps today's confirmatory rules and today's rendering. Rows written before S1 (column NULL) are also legacy.
- The exploratory source rule warns for sidecars that already have runs, and errors only for new stems. No timestamp-cutoff machinery is needed.

C2 CONCEDE. Coercion of a typo to exploration (sidecar.py:312-317) fails open.
Minimal fix: a non-canonical stage_name is classed confirmatory (fail closed) plus a lint error. This is smaller than a hard parse error and keeps the HYG-002 growth posture that §3.3.2 rule 6 invokes. (A hard error is also fine; models.py:41 is already a Literal.)

C3 PARTIAL. The holdout rule is scoped to declared derivation, and no tool can stop a deliberate undeclared copy. The user's concern is agents, though, and a cheap tripwire exists: script_sha256 is recorded on every run (schema.py:55).
Minimal fix: a lint WARNING, matching the attestation posture of §3.4, when a confirmatory stem's script_sha256 or canonicalized [outcomes] matches a prior exploratory run's and there is no [holdout].
Reject (b) data_unseen on every confirmatory sidecar: it violates §4 "confirmatory rules untouched". Reject (d), the 2-significant-figure heuristic: it is noisy. (c) is optional.

C4 CONCEDE. §3.2.1 makes "tentative" render-only. prereg.py:318 counts any 'pass', and the validation-stage hard gate at cli_cyclopts.py:754 relies on it.
Minimal fix: exploratory runs store outcome='observed', with the evaluated label in a tentative_outcome field. Every outcome='pass' consumer is then safe by construction, including bth sql users. Enumerating consumers would be more fragile.

C5 CONCEDE. With no outcomes, the pass set is empty, so is_failure_outcome('observed') is True (sidecar.py:600-602). runner.py:1128 then opens an obligation.
Minimal fix:
- Treat 'observed' like ''/'unknown' in is_failure_outcome, in compute_evalue (:618), at campaigns.py:421 and at runner.py:1049, and add it to RESERVED_OUTCOME_LABELS.
- No outcome_failed obligation for the exploratory class.
- Sequential campaigns refuse exploratory runs, alongside the §3.6 confirmation-campaign rule.

C6 CONCEDE. "Recorded as exploratory" with no column would mean overwriting the declared stage_name. Two readings are possible, and autonomous mode already denies on drift (prereg.py:480-496).
Minimal fix:
- epistemic_class is set to exploratory on drift; stage_name stays as declared.
- The autonomous deny stays.
- Drift for this rule means a change to the canonical stage_name, [outcomes] or [result_schema], not the whole-file sha.
- The stem's strictest declared class governs.

C7 CONCEDE. "appraiser_session recorded automatically" cannot happen in a sidecar written by hand. [review.literature.appraisal] is a single table, so a second appraisal has nowhere to live. Adding an appraisal changes the sha, which triggers drift (C6).
Minimal fix:
- Appraisals are ledger records written by `bth appraise` (CLI and MCP), stamped with session, time and sha. Sidecars carry appraisal_ids.
- The citing session is stamped on the run (new agent_session field).
- Fallback if the spike fails: an attested appraiser_session field.
- Assumption Ledger row (UNVERIFIED): does Claude Code or the MCP client expose a per-session id to subprocesses, and do subagents get distinct ids? This needs a spike under .praxia/spikes/; I cannot settle it.

C8 PARTIAL. §3.3.2 (last bullet) scopes literature promotion to run-less observations, but §3.4 never restricts the kind.
Minimal fix (one sentence): literature_prediction is valid only when derived_from_observation points at a run-less observation. Run-backed observations need a data holdout of rank ≥ 1.

C9 PARTIAL. A frame or time split ranking 2 contradicts md-replicas-are-samples.md.
Minimal fix: held_out_split requires `unit ∈ {seed, build, system}`, and rank = min(2, unit level), where seed → 0 and build → 1. Defer n_units and the contrast unit to estimand discipline (#2677), which §2 already puts out of scope.

C10 PARTIAL. obs.created_at is under the observer's control.
Minimal fix:
- Compare against the timestamp of the observation's source run.
- The manifest's time must come from its first git commit or from a recorded bth run output; otherwise it is rank 0.

C11 CONCEDE. The rank-0 carve-out ("unless the claim is about seed stability") contradicts §7.6.
Minimal fix:
- Delete the clause.
- Add an observation `status ∈ {open, refuted, survived-seed-check, promoted}` carried on the superseding row, separate from a correction.

C12 PARTIAL.
- Rebut the generic or asserted entry: rule 1 intends exactly that ("lack of scrutiny then shows up as weak basis"). The high-stakes path (threshold-source) is already gated by independence. Reject severity floors as scope creep.
- Concede: self-addressing a decisive or major objection needs a `for` entry from a different appraiser, or basis ≥ executed.
- Concede: rule 5 compares after normalizing case, whitespace and punctuation.
- Concede: state that listed order is the ordinal for read_depth and use_tier.
- Concede: place contrast-context in the order. It requires tier ≥ contested.

C13 PARTIAL.
- Rebut "one source per branch can't represent per-literal": §3.2 says it "promotes check_threshold_basis" (linter.py:907), which pins per-branch semantics. Reword "each numeric literal" to "each branch with a numeric literal".
- Concede (small): exploratory non-residual branches always need a source. This closes the boolean-field dodge and the §3.6 signal gap.
- Concede: sanity entries allow no arithmetic on literals.
- Concede: a closed prefix grammar (lit:, run:, sesoi:, provisional:, card:) for the prefixes bathos resolves.

C14 PARTIAL. §3.6 already rejects exploratory runs. Add one sentence: coverage at claim.py:908 counts confirmatory-class runs only, so calibration is excluded, and the same applies in every campaign mode.

C15 REBUT. §4 says class is "derived from stage_name, which is already in cool and warm", i.e. the run row, not a re-parse. The e-value re-parse at campaigns.py:399-408 is pre-existing behaviour, not something this spec introduces. With epistemic_class added, a clarifying sentence is optional.

C16 PARTIAL.
- §3.1 already says calibration outcomes are optional, but S1 lists only an exploratory branch. Add the calibration branch to S1, recording 'observed'.
- Resolve run:<id> only when the run exited 0 and its outcome is not error or invalid_measurement.
- Rebut drift: only exploratory drift is made advisory (§3.2.1), so calibration keeps today's rules by default.
- Cherry-picking among calibration runs is a suggestion only.

C17 PARTIAL. The cool-fallback half is wrong (see above). Concede that observation writes should go through emit_or_legacy (emit.py:172), with an observation.created kind and the Parquet write as the legacy write, matching the project's write-site convention.

C18 PARTIAL. The precedence already exists (runner.py:855-1037: differential short-circuit, then exit-code guard, then evaluation). The spec only needs to:
- slot sanity in after the exit guard and before evaluation;
- treat NULL or a missing field as a sanity failure;
- make missing declared outputs a warning, matched by basename under BTH_OUTPUT_DIR;
- for a sanity failure, record invalid_measurement with reason 'sanity:<expr>' and leave differential_status untouched.

C19 PARTIAL. Add negative controls for each conceded item:
- C1/C2: legacy and typo sidecars stay confirmatory.
- C4: a tentative pass must not satisfy requires_pass_stem.
- C5: no obligation and a neutral e-value.
- C12: self-rebuttal is rejected.
- figure-read never reaches threshold-source.
- C14: excluded classes do not cover clauses.
Dogfood must be checked via bth compact + bth sql.

C20 CONCEDE (minor). Define the count as distinct non-residual branch conditions declared across the stem's sidecar revisions.

C21 CONCEDE (minor). Use `sesoi:<value>:<why>`.

C22 CONCEDE.
- §2's "only two places" row is wrong: linter.py:233 and sprint_audit.py:166/262 also read stage_name.
- Citations: :285 should be :268, and :129 should be review_tier at :189.
- Ablation is not hard-gated today. State that the confirmatory class does not change the stage lists in those gates.
- Rebut the hypothesis point: making "question required" is a new exploratory-only rule and is coherent even though hypothesis is not validated today.

C23 PARTIAL.
- S1 must co-ship S2.
- C4's storage change lands in S1; rendering stays in S3b.
- S4 depends on S3 and S5.
- Add mcp.py new_experiment_tool to the file lists.
- Note the postmortem unexpected_observations field (postmortem.py:35) as related.

C24 PARTIAL. The §2 table is a tagged ledger. Add rows for the falsified design assumptions (C1, C4, C5, C6) and the unverified session-id assumption (C7).

C25 PARTIAL.
- Concede: an appraisal is required on run-less observations. Without one, the observation cannot be promoted or used as a source.
- Rebut capping the claim tier for attested holdouts: §7.3 is the user's decision, and it chose labelling.

== Consolidated edit list (in order) ==
1. Amend §4: add an epistemic_class run column (plus tentative_outcome and agent_session). Class rules: explicit key; absent key → legacy; non-canonical → confirmatory (C1, C2, C4, C6, C15).
2. Exploratory runs store outcome='observed' and keep the evaluated label in tentative_outcome (C4).
3. Make 'observed' neutral at the five call sites; no obligations for the exploratory class; sequential campaigns refuse exploratory runs (C5).
4. Drift reclassification: canonical-block drift sets epistemic_class; the autonomous deny stays; the strictest class governs (C6).
5. Migration: legacy class, a warn-first source rule for stems with existing runs, and a restage lint (C1).
6. Appraisals become ledger records via bth appraise, with appraisal_ids in sidecars; add a session-id spike row (C7).
7. Holdout rules:
   - literature_prediction only for run-less observations (C8);
   - a unit field on held_out_split (C9);
   - split timing checked against the source run, with a git or bth timestamp (C10);
   - delete the rank-0 carve-out and add an observation status (C11);
   - the script-sha laundering warning (C3).
8. Appraisal rules: self-address limit, normalized comparison, ordinal tables, place for contrast-context (C12). Run-less observations require an appraisal (C25).
9. Source rules:
   - per-branch wording, and exploratory non-residual branches always need a source (C13);
   - no arithmetic on literals in sanity entries (C13);
   - closed prefix grammar (C13);
   - sesoi carries a value (C21).
10. Calibration branch in S1; run:<id> requires a successful run (C16).
11. Union Gate coverage counts confirmatory runs only (C14).
12. Runtime precedence, including sanity (C18); planned_tests counting unit (C20).
13. Observations go through emit_or_legacy and get_run(allow_cool_fallback) (C17).
14. Fix the §2 table rows and citations, and add Assumption Ledger rows (C22, C24).
15. Slice dependencies and file lists (C23).
16. Verification plan negative controls (C19).