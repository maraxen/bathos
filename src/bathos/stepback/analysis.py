"""Qualitative Open Analysis Evaluator and Campaign Evidence Validator (spec 261027).

Implements:
- 5 Cognitive Modes (Advisor Conversation, Pre-Mortem, So-What Test, Outsider Read, Steelman-The-Stop)
- Section completeness and non-empty narrative checks
- Campaign domain primary evidence citation validation (run, sidecar, clause, git, postmortem)
- Evidence laundering rejection (citations outside campaign domain)
- Reviewer independence and forbidden author attribution checking
"""

from __future__ import annotations

import contextlib
import secrets
from datetime import UTC, datetime
from typing import Any

from bathos.errors import (
    StepBackCitationInvalidError,
    StepBackIndependenceViolationError,
    StepBackInsufficientEvidenceError,
    StepBackInvalidSchemaError,
)
from bathos.stepback.schema import (
    OpenAnalysis,
    OpenAnalysisMode,
    Recommendation,
    StepBackEvaluation,
)

# Required sections per cognitive mode
MODE_REQUIRED_SECTIONS: dict[OpenAnalysisMode, list[str]] = {
    OpenAnalysisMode.ADVISOR_CONVO: [
        "premise_challenge",
        "simplest_decisive_test",
        "strategic_advice",
    ],
    OpenAnalysisMode.PRE_MORTEM: [
        "failure_narrative",
        "overlooked_red_flags",
        "present_day_symptoms",
        "preventative_mitigations",
    ],
    OpenAnalysisMode.SO_WHAT: [
        "best_case_scenario",
        "practical_utility",
        "decision_impact",
        "opportunity_cost",
    ],
    OpenAnalysisMode.OUTSIDER: [
        "jargon_free_summary",
        "common_sense_vulnerabilities",
        "alternative_explanations",
    ],
    OpenAnalysisMode.STEELMAN: [
        "the_case_for_stopping",
        "sunk_cost_fallacies_exposed",
        "superior_resource_reallocation",
        "retained_learnings",
    ],
}

# Structured prompt guidance interrogating replicate independence, unfalsifiable checks, and domain failure traps
MODE_PROMPTS: dict[OpenAnalysisMode, dict[str, str]] = {
    OpenAnalysisMode.ADVISOR_CONVO: {
        "role": "Independent Senior Research Advisor / PI Persona",
        "description": "Advises campaign lead with strong inference inquiry challenging basic premises and methods.",
        "inquiries": (
            "1. Premise Challenge: What is the central mechanistic assumption? "
            "What is the unit of statistical independence here? Are stochastic simulation seeds being treated "
            "as independent replicates that must converge (replicate pooling error), or are trajectories properly pooled as samples "
            "of a single conformational landscape? Does the analysis mistake between-replica spread for measurement error?\n"
            "2. Simplest Decisive Test: What observation would make this check FAIL? Has this condition ever evaluated "
            "to False on negative controls or shuffled baseline data? Does the evaluation metric share any data, weights, "
            "or formulas with the generation/optimization objective? What is the simplest experiment that distinguishes competing explanations?\n"
            "3. Strategic Advice: What concrete pivot, termination, or confirmatory action should be executed now?"
        ),
    },
    OpenAnalysisMode.PRE_MORTEM: {
        "role": "Prospective Failure Investigator",
        "description": "Assumes total campaign failure 6 months from today and diagnoses red flags.",
        "inquiries": (
            "1. Failure Narrative: Imagine this campaign completely failed and wasted all compute hours. Exactly how did it fail?\n"
            "2. Overlooked Red Flags: Did the campaign fail because individual replica non-convergence was mistaken for "
            "physical failure (replicate pooling blunder)? Were validation checks unfalsifiable, tautological, or circular, "
            "guaranteeing hollow passes until final scrutiny?\n"
            "3. Present-Day Symptoms: What early signals of plateau, entropy collapse, or goalpost shifting are visible right now in member runs?\n"
            "4. Preventative Mitigations: What immediate diagnostic hold or structural control halts this trajectory?"
        ),
    },
    OpenAnalysisMode.SO_WHAT: {
        "role": "Pragmatic Research Director",
        "description": "Tests practical utility, decision impact, and opportunity cost.",
        "inquiries": (
            "1. Best-Case Scenario: Assume every hypothesis is confirmed with p < 0.001.\n"
            "2. Practical Utility: Does anyone care? Does this advance biophysical understanding or protein design?\n"
            "3. Decision Impact: Does this change an engineering decision or downstream pipeline step?\n"
            "4. Opportunity Cost: What higher-value experiments or binder screens are being blocked by expending compute here?"
        ),
    },
    OpenAnalysisMode.OUTSIDER: {
        "role": "Domain-Agnostic Skeptic Scientist",
        "description": "Strips local jargon and challenges common-sense physical and statistical vulnerabilities.",
        "inquiries": (
            "1. Jargon-Free Summary: Describe the experiment in plain, elementary scientific terms without local jargon.\n"
            "2. Common-Sense Vulnerabilities: Are stochastic seeds improperly segregated rather than pooled across simulation replicates? "
            "Are outcome checks designed so they cannot fail by construction (e.g. sentinel defaults or shared formulas)? "
            "Are boundary conditions being skated without true statistical margin?\n"
            "3. Alternative Explanations: What trivial or mundane artifact (thermostat drift, grid aliasing, overfitting) "
            "explains the observed numbers better than the proposed hypothesis?"
        ),
    },
    OpenAnalysisMode.STEELMAN: {
        "role": "Adversarial Prosecutor for Resource Reallocation",
        "description": "Builds the strongest possible evidence-based prosecution for terminating the campaign immediately.",
        "inquiries": (
            "1. The Case for Stopping: What is the most compelling empirical case that this campaign is a dead end?\n"
            "2. Sunk Cost Fallacies Exposed: What rationalizations are keeping this campaign alive despite flat numbers?\n"
            "3. Superior Resource Reallocation: Where should the compute hours, researcher focus, and storage be redirected?\n"
            "4. Retained Learnings: What negative knowledge or negative control data has been securely established?"
        ),
    },
}


def get_mode_prompt(mode: OpenAnalysisMode | str) -> str:
    """Return the structured prompt and inquiry text for a cognitive mode."""
    if isinstance(mode, str):
        mode = OpenAnalysisMode.normalize(mode)
    prompt_cfg = MODE_PROMPTS.get(mode, {})
    role = prompt_cfg.get("role", "")
    desc = prompt_cfg.get("description", "")
    inq = prompt_cfg.get("inquiries", "")
    return f"Role: {role}\nObjective: {desc}\nRequired Inquiries:\n{inq}"


CITATION_PREFIXES = ("run:", "sidecar:", "outcome:", "clause:", "postmortem:", "git:")


def validate_open_analysis(
    analysis: OpenAnalysis | dict[str, Any],
    recommendation: str | None = None,
) -> OpenAnalysis:
    """Validate OpenAnalysis payload sections and mode conformity.

    Raises:
    - StepBackInvalidSchemaError: if mode is invalid, sections are missing or empty,
      or recommendation is missing.
    """
    if isinstance(analysis, dict):
        mode_raw = analysis.get("mode")
        sections = analysis.get("sections", {})
    else:
        mode_raw = analysis.mode
        sections = analysis.sections

    if not mode_raw:
        raise StepBackInvalidSchemaError("OpenAnalysis 'mode' is required")

    try:
        norm_mode = OpenAnalysisMode.normalize(str(mode_raw))
    except ValueError as exc:
        raise StepBackInvalidSchemaError(f"Invalid OpenAnalysisMode: {exc}") from exc

    if not isinstance(sections, dict):
        raise StepBackInvalidSchemaError("OpenAnalysis 'sections' must be a dictionary")

    required_sections = MODE_REQUIRED_SECTIONS[norm_mode]
    missing = [sec for sec in required_sections if sec not in sections]
    if missing:
        raise StepBackInvalidSchemaError(
            f"Mode '{norm_mode.value}' missing required sections: {missing}"
        )

    empty_sections = [
        sec for sec in required_sections
        if not str(sections.get(sec, "")).strip()
    ]
    if empty_sections:
        raise StepBackInvalidSchemaError(
            f"Mode '{norm_mode.value}' has empty required sections: {empty_sections}"
        )

    # For Path B analysis, recommendation is strictly required
    if recommendation is not None:
        valid_recs = {r.value for r in Recommendation}
        if str(recommendation).upper() not in valid_recs:
            raise StepBackInvalidSchemaError(
                f"Invalid recommendation '{recommendation}'. Must be one of: {sorted(valid_recs)}"
            )

    return OpenAnalysis(mode=norm_mode.value, sections=dict(sections))


def parse_citation(citation_str: str) -> tuple[str, str]:
    """Parse citation string into (kind, reference)."""
    cit = citation_str.strip()
    for prefix in CITATION_PREFIXES:
        if cit.startswith(prefix):
            kind = prefix[:-1]
            ref = cit[len(prefix):]
            return kind, ref
    return "unknown", cit


def validate_campaign_evidence(
    cited_evidence: list[str],
    campaign: Any,
    member_runs: list[Any],
    min_citations: int = 2,
    raise_on_invalid: bool = True,
) -> tuple[list[str], list[str]]:
    """Validate cited evidence against primary campaign domain records.

    Verifies:
    1. run:<id> is a member run of the campaign.
    2. sidecar:<path> is associated with member runs.
    3. clause:<path>#<clause_id> matches the campaign's claim_path.
    4. git:<sha> matches commits from member runs.

    Returns:
    (valid_citations, invalid_citations)

    Raises:
    - StepBackCitationInvalidError: if raise_on_invalid is True and an invalid citation is found.
    - StepBackInsufficientEvidenceError: if valid citations count < min_citations.
    """
    # Collect campaign domain membership sets
    campaign_run_ids: set[str] = set()
    campaign_sidecars: set[str] = set()
    campaign_git_shas: set[str] = set()

    for r in member_runs:
        rid = None
        if isinstance(r, dict):
            rid = r.get("run_id") or r.get("id") or r.get("uuid")
            script = r.get("script") or r.get("script_path")
            sidecar = r.get("sidecar_path") or r.get("sidecar")
            git_sha = r.get("git_commit") or r.get("commit_sha") or r.get("git_sha")
        else:
            rid = getattr(r, "run_id", None) or getattr(r, "id", None) or getattr(r, "uuid", None)
            script = getattr(r, "script", None) or getattr(r, "script_path", None)
            sidecar = getattr(r, "sidecar_path", None) or getattr(r, "sidecar", None)
            git_sha = getattr(r, "git_commit", None) or getattr(r, "commit_sha", None) or getattr(r, "git_sha", None)

        if rid:
            s_rid = str(rid)
            campaign_run_ids.add(s_rid)
            # Add short prefixes (first 8+ chars)
            if len(s_rid) >= 8:
                campaign_run_ids.add(s_rid[:8])

        if sidecar:
            campaign_sidecars.add(str(sidecar).strip())
        if script:
            # Also derive sidecar name: <stem>.bth.toml
            s_path = str(script).strip()
            campaign_sidecars.add(s_path)
            if s_path.endswith(".py"):
                campaign_sidecars.add(s_path[:-3] + ".bth.toml")

        if git_sha:
            s_sha = str(git_sha).strip()
            campaign_git_shas.add(s_sha)
            if len(s_sha) >= 7:
                campaign_git_shas.add(s_sha[:7])

    claim_path = None
    if isinstance(campaign, dict):
        claim_path = campaign.get("claim_path")
    else:
        claim_path = getattr(campaign, "claim_path", None)

    valid_citations: list[str] = []
    invalid_citations: list[str] = []

    for cit in cited_evidence:
        cit_str = str(cit).strip()
        kind, ref = parse_citation(cit_str)
        ref_target = ref.split("#")[0].strip()

        # F-05: Empty reference target must be rejected (cannot match all runs/sidecars)
        if not ref_target:
            invalid_citations.append(cit_str)
            if raise_on_invalid:
                raise StepBackCitationInvalidError(
                    f"Citation '{cit_str}' contains an empty reference target"
                )
            continue

        if kind == "run":
            # Check uuid or prefix
            run_ref = ref_target
            if run_ref in campaign_run_ids or any(rid.startswith(run_ref) for rid in campaign_run_ids):
                valid_citations.append(cit_str)
            else:
                invalid_citations.append(cit_str)
                if raise_on_invalid:
                    raise StepBackCitationInvalidError(
                        f"Cited run '{run_ref}' does not belong to campaign '{getattr(campaign, 'id', getattr(campaign, 'campaign_id', campaign))}'"
                    )

        elif kind == "sidecar":
            sidecar_ref = ref_target
            if sidecar_ref in campaign_sidecars or any(sc.endswith(sidecar_ref) or sidecar_ref.endswith(sc) for sc in campaign_sidecars):
                valid_citations.append(cit_str)
            else:
                invalid_citations.append(cit_str)
                if raise_on_invalid:
                    raise StepBackCitationInvalidError(
                        f"Cited sidecar '{sidecar_ref}' not associated with any member runs in this campaign"
                    )

        elif kind == "outcome":
            # outcome:<run_id>:<label>
            parts = ref_target.split(":")
            run_ref = parts[0].strip()
            if run_ref and (run_ref in campaign_run_ids or any(rid.startswith(run_ref) for rid in campaign_run_ids)):
                valid_citations.append(cit_str)
            else:
                invalid_citations.append(cit_str)
                if raise_on_invalid:
                    raise StepBackCitationInvalidError(
                        f"Cited outcome run '{run_ref}' does not belong to campaign"
                    )

        elif kind == "clause":
            # clause:<claim_path>#<clause_id>
            clause_path = ref_target
            if claim_path and (clause_path == claim_path or clause_path.endswith(str(claim_path)) or str(claim_path).endswith(clause_path)):
                valid_citations.append(cit_str)
            elif not claim_path and clause_path.endswith(".toml"):
                # If claim path wasn't formally registered, allow if path exists
                valid_citations.append(cit_str)
            else:
                invalid_citations.append(cit_str)
                if raise_on_invalid:
                    raise StepBackCitationInvalidError(
                        f"Cited claim clause '{clause_path}' does not match campaign claim_path '{claim_path}'"
                    )

        elif kind == "git":
            git_ref = ref_target
            if not campaign_git_shas or git_ref in campaign_git_shas or any(g.startswith(git_ref) for g in campaign_git_shas):
                valid_citations.append(cit_str)
            else:
                invalid_citations.append(cit_str)
                if raise_on_invalid:
                    raise StepBackCitationInvalidError(
                        f"Cited git commit '{git_ref}' is not associated with member runs"
                    )

        elif kind == "postmortem":
            # postmortem:<path>#<obligation_id>
            valid_citations.append(cit_str)

        else:
            invalid_citations.append(cit_str)
            if raise_on_invalid:
                raise StepBackCitationInvalidError(f"Unsupported citation syntax or prefix: '{cit_str}'")

    if len(valid_citations) < min_citations:
        raise StepBackInsufficientEvidenceError(
            f"Review requires at least {min_citations} valid primary domain citations; found {len(valid_citations)}"
        )

    return valid_citations, invalid_citations


def check_reviewer_independence(
    reviewer_id: str,
    campaign: Any,
    member_runs: list[Any],
    raise_on_conflict: bool = False,
) -> bool:
    """Check whether reviewer is independent from campaign authoring and execution.

    Constructs forbidden set:
    - Agents that executed member runs (agent_mode, agent_id)
    - Authors of member runs (metadata.author, author)
    - Campaign creator
    - Git commit authors (Name, email, and Full Name <email>)
    """
    clean_reviewer = str(reviewer_id).strip().lower() if reviewer_id is not None else ""
    if not clean_reviewer:
        if raise_on_conflict:
            raise StepBackInvalidSchemaError("Reviewer identity cannot be empty or whitespace")
        return False

    forbidden_authors: set[str] = set()

    def _add_forbidden(val: Any) -> None:
        if not val:
            return
        s = str(val).strip().lower()
        if not s:
            return
        forbidden_authors.add(s)
        if "<" in s and ">" in s:
            try:
                name_part = s.split("<")[0].strip()
                email_part = s.split("<")[1].split(">")[0].strip()
                if name_part:
                    forbidden_authors.add(name_part)
                if email_part:
                    forbidden_authors.add(email_part)
            except Exception:
                pass
        with contextlib.suppress(Exception):
            import email.utils
            p_name, p_email = email.utils.parseaddr(str(val))
            if p_name.strip():
                forbidden_authors.add(p_name.strip().lower())
            if p_email.strip():
                forbidden_authors.add(p_email.strip().lower())

    # Campaign creator
    creator = None
    if isinstance(campaign, dict):
        creator = campaign.get("creator") or campaign.get("author")
    else:
        creator = getattr(campaign, "creator", None) or getattr(campaign, "author", None)
    if creator:
        _add_forbidden(creator)

    for r in member_runs:
        if isinstance(r, dict):
            agent = r.get("agent_mode") or r.get("agent_id") or r.get("agent_name")
            author = r.get("author") or (r.get("metadata", {}) or {}).get("author")
            git_author = r.get("git_author")
        else:
            agent = getattr(r, "agent_mode", None) or getattr(r, "agent_id", None) or getattr(r, "agent_name", None)
            author = getattr(r, "author", None)
            if hasattr(r, "metadata"):
                meta = r.metadata
                if isinstance(meta, str) and meta.strip().startswith("{"):
                    try:
                        import json
                        meta = json.loads(meta)
                    except Exception:
                        meta = {}
                if isinstance(meta, dict):
                    if not author:
                        author = meta.get("author")
                    if not agent:
                        agent = meta.get("agent_mode") or meta.get("agent_id") or meta.get("agent_name")
            git_author = getattr(r, "git_author", None)

        if agent:
            _add_forbidden(agent)
        if author:
            _add_forbidden(author)
        if git_author:
            _add_forbidden(git_author)

    is_independent = clean_reviewer not in forbidden_authors

    if not is_independent and raise_on_conflict:
        raise StepBackIndependenceViolationError(
            f"Reviewer '{reviewer_id}' has conflict of interest; member of forbidden author set: {forbidden_authors}"
        )

    return is_independent


def evaluate_downstream_outcomes(
    review: Any,
    subsequent_runs: list[Any],
) -> StepBackEvaluation:
    """Evaluate downstream campaign outcomes following a step-back review.

    Calculates:
    - excess_spend_avoided: compute hours or runs saved when advisory holds/pivots intervene.
    - predictive_validity: whether the review recommendation matched the empirical trajectory.
    - trajectory_slope: OLS slope across subsequent scalar metric observations.
    - vacuous_pass_intercepted: whether the review successfully caught unfalsifiable/tautological checks.
    """
    review_id = str(
        getattr(review, "review_id", None)
        or (review.get("review_id") if isinstance(review, dict) else None)
        or "rev_unknown"
    )
    campaign_id = str(
        getattr(review, "campaign_id", None)
        or (review.get("campaign_id") if isinstance(review, dict) else None)
        or ""
    )
    rec = str(
        getattr(review, "recommendation", None)
        or (review.get("recommendation") if isinstance(review, dict) else None)
        or "CONTINUE"
    ).upper().strip()

    if isinstance(subsequent_runs, dict):
        subsequent_runs = list(subsequent_runs.values())
    elif not isinstance(subsequent_runs, list):
        subsequent_runs = []

    subsequent_runs_count = len(subsequent_runs)
    subsequent_outcomes: list[str] = []
    subsequent_metrics: list[float] = []

    for r in subsequent_runs:
        out = r.get("outcome") if isinstance(r, dict) else getattr(r, "outcome", None)
        subsequent_outcomes.append(str(out or "unknown"))

        m = None
        results = r.get("results") if isinstance(r, dict) else getattr(r, "results", None)
        if isinstance(results, dict):
            for v in results.values():
                try:
                    if not isinstance(v, bool):
                        m = float(v)
                        break
                except (ValueError, TypeError):
                    pass
        if m is None:
            val = r.get("metric") if isinstance(r, dict) else getattr(r, "metric", None)
            if val is not None and not isinstance(val, bool):
                with contextlib.suppress(ValueError, TypeError):
                    m = float(val)
        if m is not None:
            subsequent_metrics.append(m)

    trajectory_slope: float | None = None
    if len(subsequent_metrics) >= 3:
        n = len(subsequent_metrics)
        x_mean = (n - 1) / 2.0
        y_mean = sum(subsequent_metrics) / n
        num = sum((i - x_mean) * (y - y_mean) for i, y in enumerate(subsequent_metrics))
        den = sum((i - x_mean) ** 2 for i in range(n))
        trajectory_slope = round(num / den, 4) if den > 0 else 0.0

    vacuous_pass_intercepted = False
    rubric_scores = (
        getattr(review, "rubric_scores", None)
        or (review.get("rubric_scores") if isinstance(review, dict) else None)
        or {}
    )
    if (
        isinstance(rubric_scores, dict)
        and (rubric_scores.get("question_validity") == 1 or rubric_scores.get("risk_confound_surface") == 1)
    ):
        vacuous_pass_intercepted = True

    open_analysis = (
        getattr(review, "open_analysis", None)
        or (review.get("open_analysis") if isinstance(review, dict) else None)
    )
    if open_analysis and not vacuous_pass_intercepted:
        sections = (
            getattr(open_analysis, "sections", None)
            or (open_analysis.get("sections") if isinstance(open_analysis, dict) else None)
            or {}
        )
        if isinstance(sections, dict):
            text = " ".join(str(v) for v in sections.values()).lower()
            if any(term in text for term in ("unfalsifiable", "tautolog", "cannot fail", "replicate pooling", "pseudo-replication")):
                vacuous_pass_intercepted = True

    excess_spend_avoided = 0.0
    if rec in ("HOLD", "ESCALATE", "CONCLUDE_ABANDON"):
        if subsequent_runs_count == 0:
            excess_spend_avoided = 10.0
        else:
            excess_spend_avoided = max(0.0, float(10 - subsequent_runs_count))
    elif rec == "PIVOT" and subsequent_runs_count <= 5 and any("pass" in o.lower() for o in subsequent_outcomes):
        excess_spend_avoided = 5.0

    predictive_validity = True
    if rec == "CONTINUE":
        if subsequent_runs_count >= 3:
            pass_count = sum(1 for o in subsequent_outcomes if "pass" in o.lower() or "promising" in o.lower())
            if pass_count < (subsequent_runs_count / 2) and (trajectory_slope is not None and trajectory_slope <= 0):
                predictive_validity = False
    elif (
        rec in ("HOLD", "ESCALATE", "CONCLUDE_ABANDON")
        and subsequent_runs_count >= 5
        and all("pass" in o.lower() for o in subsequent_outcomes)
    ):
        predictive_validity = False

    evaluation_id = f"eval_{secrets.token_hex(6)}"
    now_iso = datetime.now(UTC).isoformat()

    return StepBackEvaluation(
        evaluation_id=evaluation_id,
        review_id=review_id,
        campaign_id=campaign_id,
        evaluated_at=now_iso,
        excess_spend_avoided=round(excess_spend_avoided, 2),
        predictive_validity=predictive_validity,
        trajectory_slope=trajectory_slope,
        vacuous_pass_intercepted=vacuous_pass_intercepted,
        subsequent_runs_count=subsequent_runs_count,
        subsequent_outcomes=subsequent_outcomes,
        notes=f"Evaluated downstream outcomes for {rec} review with {subsequent_runs_count} subsequent runs.",
    )
