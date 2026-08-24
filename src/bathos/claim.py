"""Claim-tier rigor: discriminability maps and union gates for confirmatory campaigns."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from bathos.telemetry import event


class ValidationError:
    """Single validation error."""

    def __init__(self, message: str):
        self.message = message

    def __repr__(self):
        return f"ValidationError({self.message!r})"


@dataclass
class ValidationResult:
    """Result of claim validation."""

    ok: bool
    errors: list[ValidationError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    infos: list[str] = field(default_factory=list)


_OPAQUE_ID_RE = re.compile(r"^[A-Z][0-9]+$")
_PLACEHOLDER_LABEL_RE = re.compile(r"^REQUIRED:\s*", re.IGNORECASE)

# BP-3: default vocabulary of outcome labels considered a "strong negative" claim, requiring a
# --negative-check attestation at `bth campaign conclude`. Ported from asr's C5 NEGATIVE_PAT
# (scripts/gates/claim_hygiene.py); override per-project via `.bth.toml` [claim] negative_outcome_pattern.
DEFAULT_NEGATIVE_OUTCOME_PATTERN = re.compile(
    r"\b(fail(?:ed)?|falsified|void|no-?go|not-a-fair-test|dead[- ]?end|reversed|null|neutral|marginal)\b",
    re.IGNORECASE,
)


def is_negative_outcome(outcome_label: str, pattern: re.Pattern | None = None) -> bool:
    """True if outcome_label matches the negative-outcome vocabulary (BP-3, C5 port).

    Args:
        outcome_label: The --outcome value passed to `bth campaign conclude`
        pattern: Optional compiled override pattern (from .bth.toml [claim] negative_outcome_pattern);
            defaults to DEFAULT_NEGATIVE_OUTCOME_PATTERN
    """
    effective = pattern or DEFAULT_NEGATIVE_OUTCOME_PATTERN
    return bool(effective.search(outcome_label or ""))


def display_label(entity: dict) -> str:
    """Return human-facing label for a claim entity, falling back to id."""
    label = str(entity.get("label") or "").strip()
    entity_id = str(entity.get("id") or "").strip()
    if label:
        return label
    return entity_id if entity_id else "unknown"


def format_hypothesis_ref(claim: ClaimFile, hypothesis_id: str) -> str:
    """Resolve a hypothesis id to a human-readable reference string."""
    for hypothesis in claim.hypotheses:
        if hypothesis.get("id") == hypothesis_id:
            label = str(hypothesis.get("label") or "").strip()
            if label and label != hypothesis_id:
                return f"{label} ({hypothesis_id})"
            return display_label(hypothesis)
    return hypothesis_id


def format_clause_ref(clause: dict) -> str:
    """Return human-facing union-gate clause reference."""
    clause_id = str(clause.get("id") or "?").strip()
    description = str(clause.get("description") or "").strip()
    if description and description != clause_id:
        return f"{description} ({clause_id})"
    return description if description else clause_id


def format_clause_list(claim: ClaimFile, clause_ids: list[str]) -> list[str]:
    """Map union-gate clause ids to human-readable references."""
    by_id = {c.get("id"): c for c in claim.union_gate_clauses}
    return [format_clause_ref(by_id.get(cid, {"id": cid})) for cid in clause_ids]


def is_placeholder_label(entity_id: str, label: str) -> bool:
    """True when label is still a scaffold placeholder."""
    stripped = label.strip()
    if not stripped:
        return False
    if stripped == entity_id:
        return True
    return bool(_PLACEHOLDER_LABEL_RE.match(stripped))


@dataclass
class ClaimFile:
    """Parsed claim.bth.toml file."""

    headline: str
    kill_condition: str
    kill_condition_satisfiable_by_null: bool | None  # debt #1071, AC-23
    regime: str | None
    hypotheses: list[dict]  # {id, label, predicted_signature?}
    assumptions: list[dict]
    confounds: list[dict]
    discriminability: list[
        dict
    ]  # {hypothesis_a, hypothesis_b, planned_run_label, predicted_outcome}
    union_gate_clauses: list[dict]  # {id, description, hypothesis_ids}
    path: Path
    sha256: str


def parse_claim(path: Path) -> ClaimFile:
    """Parse a claim.bth.toml file.

    Args:
        path: Path to claim.bth.toml

    Returns:
        ClaimFile dataclass

    Raises:
        ValueError: If file cannot be parsed or is malformed
        FileNotFoundError: If file does not exist
    """
    if not path.exists():
        raise FileNotFoundError(f"Claim file not found at {path}")

    try:
        with open(path, "rb") as f:
            content = f.read()
            data = tomllib.loads(content.decode("utf-8"))
    except Exception as e:
        raise ValueError(f"Failed to parse claim TOML at {path}: {e}") from e

    claim_section = data.get("claim", {})

    headline = claim_section.get("headline", "")
    kill_condition = claim_section.get("kill_condition", "")
    kill_condition_satisfiable_by_null = claim_section.get("kill_condition_satisfiable_by_null")
    regime = claim_section.get("regime")

    hypotheses = data.get("hypotheses", [])
    assumptions = data.get("assumptions", [])
    confounds = data.get("confounds", [])
    discriminability = claim_section.get("discriminability", [])
    union_gate = claim_section.get("union_gate", {})
    union_gate_clauses = union_gate.get("clauses", [])

    # Compute SHA256 of file content
    sha256_hex = hashlib.sha256(content).hexdigest()

    return ClaimFile(
        headline=headline,
        kill_condition=kill_condition,
        kill_condition_satisfiable_by_null=kill_condition_satisfiable_by_null,
        regime=regime,
        hypotheses=hypotheses,
        assumptions=assumptions,
        confounds=confounds,
        discriminability=discriminability,
        union_gate_clauses=union_gate_clauses,
        path=path,
        sha256=sha256_hex,
    )


def load_registered_claim(
    db: duckdb.DuckDBPyConnection, campaign_id: str, workspace_root: Path | None = None
) -> ClaimFile | None:
    """Resolve + load the claim registered to a campaign, or None if it has none. (#3719)

    Mirrors the resolution `conclude_campaign` already does inline (campaigns.claim_path /
    claim_sha256 -> check_sha -> parse_claim), factored out so other call sites -- like
    `validate_sidecar`'s new claim-discriminability cross-check -- don't duplicate it.

    Args:
        db: DuckDB connection
        campaign_id: Campaign ID (prefix or full UUID)
        workspace_root: Project workspace root; defaults to resolve_workspace().fs_root

    Returns:
        The parsed ClaimFile, or None if the campaign has no registered claim
        (claim_path IS NULL — the opt-in adoption ladder, same as conclude_campaign).

    Raises:
        CampaignError: If campaign_id does not resolve
        FileNotFoundError: If the registered claim file is missing
        ValueError: If the claim file's SHA256 no longer matches the registered value
    """
    from bathos.campaigns import _resolve_campaign_id
    from bathos.workspace import resolve_workspace

    full_id = _resolve_campaign_id(db, campaign_id)

    row = db.execute(
        "SELECT claim_path, claim_sha256 FROM campaigns WHERE id=?", [full_id]
    ).fetchone()
    if not row or not row[0] or not row[1]:
        return None
    claim_path_rel, registered_sha = row[0], row[1]

    if workspace_root is None:
        workspace_root = resolve_workspace(Path.cwd()).fs_root

    abs_path = resolve_claim_path(claim_path_rel, workspace_root)
    if not abs_path.exists():
        raise FileNotFoundError(
            f"claim.bth.toml not found at {abs_path} — file may have been moved or deleted."
        )

    check_sha(claim_path_rel, registered_sha, workspace_root)
    return parse_claim(abs_path)


def validate_claim(
    claim: ClaimFile,
    db: duckdb.DuckDBPyConnection | None = None,
    workspace_root: Path | None = None,
) -> ValidationResult:
    """Validate a parsed claim file.

    Args:
        claim: ClaimFile to validate
        db: Optional DuckDB connection for regime coverage check (AC-07)
        workspace_root: Optional project workspace root, enables the BP-2
            [confounds.synthetic_recovery] gate-state check

    Returns:
        ValidationResult with ok=True if no errors, False otherwise
    """
    errors = []
    warnings = []
    infos = []

    # AC-03: Missing headline
    if not claim.headline or claim.headline.strip() == "":
        errors.append(ValidationError("Missing or blank headline"))

    # AC-03: Blank kill_condition
    if not claim.kill_condition or claim.kill_condition.strip() == "":
        errors.append(ValidationError("kill_condition is required and must not be blank"))

    # AC-03: Fewer than 2 hypotheses
    if len(claim.hypotheses) < 2:
        errors.append(
            ValidationError(f"At least 2 hypotheses required, found {len(claim.hypotheses)}")
        )

    # AC-03: No null/misspec hypothesis
    has_null_or_misspec = any(
        "null" in h.get("id", "").lower() or "misspec" in h.get("id", "").lower()
        for h in claim.hypotheses
    )
    if not has_null_or_misspec:
        errors.append(
            ValidationError(
                "No hypothesis with 'null' or 'misspec' in id — expected a null/misspecification hypothesis"
            )
        )

    # AC-03/AC-14: Check for opaque IDs with no label (using corrected regex pattern)
    for h in claim.hypotheses:
        h_id = h.get("id", "")
        h_label = h.get("label", "")
        if _OPAQUE_ID_RE.match(h_id) and (not h_label or h_label.strip() == ""):
            errors.append(
                ValidationError(
                    f"Opaque hypothesis id '{h_id}' must have a descriptive label field (found blank)"
                )
            )

    for c in claim.confounds:
        c_id = c.get("id", "")
        c_label = c.get("label", "")
        if _OPAQUE_ID_RE.match(c_id) and (not c_label or c_label.strip() == ""):
            errors.append(
                ValidationError(
                    f"Opaque confound id '{c_id}' must have a descriptive label field (found blank)"
                )
            )

    # AC-03: Check discriminability entries for missing predicted_outcome
    for disc in claim.discriminability:
        if "predicted_outcome" not in disc or not disc.get("predicted_outcome"):
            h_a = format_hypothesis_ref(claim, disc.get("hypothesis_a", "?"))
            h_b = format_hypothesis_ref(claim, disc.get("hypothesis_b", "?"))
            label = disc.get("planned_run_label", "?")
            errors.append(
                ValidationError(
                    f"Discriminability entry for {h_a} vs {h_b} (run {label}) missing predicted_outcome"
                )
            )

    # AC-13: Validate [confounds.reference_parity] sub-blocks in confounds
    for confound in claim.confounds:
        ref_par = confound.get("reference_parity", {})
        if not ref_par:
            # No parity block for this confound
            continue

        parity_run_id = ref_par.get("parity_run_id", "")
        reference_metric = ref_par.get("reference_metric", "")
        reference_value = ref_par.get("reference_value")
        equivalence_bound = ref_par.get("equivalence_bound")
        confound_label = display_label(confound)

        # State 1: parity_run_id empty or missing
        if not parity_run_id:
            errors.append(
                ValidationError(f"baseline admissibility not established for '{confound_label}'")
            )
            continue

        # State 2: parity_run_id set AND db is not None
        if db is not None:
            # F-1 graded-parity-run check: query BOTH metadata (for numeric metric, legacy path)
            # AND parity_run_type column (for graded path). The column is authoritative for
            # literature_parity runs; the legacy equivalence-bound path is retained for confounds
            # that use reference_metric/equivalence_bound without a parity run type.
            row = db.execute(
                "SELECT outcome, parity_run_type FROM runs WHERE id=? OR id LIKE ?",
                [parity_run_id, parity_run_id + "%"],
            ).fetchone()

            if row is None:
                # Run not compacted (not in warm DB)
                errors.append(
                    ValidationError(
                        f"parity run '{parity_run_id}' not compacted — run `bth compact` to enable baseline parity check"
                    )
                )
            else:
                run_outcome, run_parity_type = row

                # GRADED PATH (F-1): if the run is a literature_parity run, use graded verdict
                # (controlled/controlled-by-protocol/uncontrolled). This fires beside the legacy path.
                if run_parity_type == "literature_parity":
                    if run_outcome in ("pass", "partial"):
                        status = "controlled" if run_outcome == "pass" else "controlled-by-protocol"
                        infos.append(
                            f"reference_parity {status} for '{confound_label}' "
                            f"(parity_run_type='literature_parity', outcome='{run_outcome}')"
                        )
                    else:
                        errors.append(
                            ValidationError(
                                f"parity run '{parity_run_id}' is a literature_parity run but outcome='{run_outcome}' "
                                f"— not controlled for '{confound_label}'"
                            )
                        )
                    continue  # graded path handled; skip legacy equivalence-bound path

                # LEGACY PATH: numeric reference_metric / equivalence_bound check.
                # Only fires when parity_run_type != 'literature_parity' (non-parity or NULL).
                # Requires metadata JSON for numeric comparison.
                meta_row = db.execute(
                    "SELECT metadata FROM runs WHERE id=? OR id LIKE ?",
                    [parity_run_id, parity_run_id + "%"],
                ).fetchone()

                if meta_row is None:
                    errors.append(
                        ValidationError(
                            f"parity run '{parity_run_id}' not compacted — run `bth compact` to enable baseline parity check"
                        )
                    )
                else:
                    try:
                        meta = json.loads(meta_row[0] or "{}")
                    except json.JSONDecodeError as e:
                        errors.append(
                            ValidationError(f"failed to parse run metadata for parity check: {e}")
                        )
                        continue

                    # State 2b: metric missing from metadata (HARD ERROR, not swallowed by exception)
                    if reference_metric not in meta:
                        errors.append(
                            ValidationError(
                                f"parity_metric key '{reference_metric}' not found in baseline run metadata — check field name"
                            )
                        )
                    else:
                        # Metric found, check equivalence bound
                        try:
                            result_val = float(meta[reference_metric])
                            if abs(result_val - reference_value) < equivalence_bound:
                                infos.append(
                                    f"baseline parity PASS for '{confound_label}' (|{result_val:.4f} - {reference_value}| < {equivalence_bound})"
                                )
                            else:
                                errors.append(
                                    ValidationError(
                                        f"parity run '{parity_run_id}' does not satisfy equivalence bound for '{confound_label}'"
                                    )
                                )
                        except (ValueError, TypeError) as e:
                            errors.append(ValidationError(f"failed to compare parity metric: {e}"))
        else:
            # State 3: parity_run_id set, db is None
            infos.append(
                f"skipping baseline parity check for '{confound_label}' — no catalog connection"
            )

    # AC-23: kill_condition_satisfiable_by_null must be declared, and if true, a union_gate
    # clause tagged positive_control=true must exist (debt #1071). Schema-enforced instead of
    # remembered -- previously this was an ad hoc clause-naming convention with zero validation.
    if claim.kill_condition_satisfiable_by_null is None:
        errors.append(
            ValidationError(
                "kill_condition_satisfiable_by_null is required (AC-23) — declare whether a "
                "null result is a live possibility this claim's kill_condition needs to rule out"
            )
        )
    elif claim.kill_condition_satisfiable_by_null:
        has_positive_control_clause = any(
            clause.get("positive_control") is True for clause in claim.union_gate_clauses
        )
        if not has_positive_control_clause:
            errors.append(
                ValidationError(
                    "kill_condition_satisfiable_by_null=true but no union_gate clause is marked "
                    "positive_control=true (AC-23) — add one proving the instrument can detect a "
                    "known-real effect via the [differential] pre-flight"
                )
            )
    # BP-2: Validate [confounds.synthetic_recovery] sub-blocks in confounds
    for confound in claim.confounds:
        synth = confound.get("synthetic_recovery", {})
        if not synth:
            # No synthetic_recovery block for this confound
            continue

        confound_label = display_label(confound)
        gate_name = synth.get("gate_name", "")
        guards = synth.get("guards", [])

        if not gate_name:
            errors.append(
                ValidationError(
                    f"synthetic_recovery block for '{confound_label}' missing required 'gate_name'"
                )
            )
            continue
        if not guards:
            errors.append(
                ValidationError(
                    f"synthetic_recovery block for '{confound_label}' (gate '{gate_name}') "
                    "missing required 'guards' (list of guarded source paths)"
                )
            )
            continue

        if workspace_root is not None:
            from bathos.gate import gate_state

            state = gate_state(workspace_root, gate_name, guards)
            if state == "GREEN":
                infos.append(f"synthetic_recovery gate '{gate_name}' GREEN for '{confound_label}'")
            elif state == "STALE":
                warnings.append(
                    f"synthetic_recovery gate '{gate_name}' STALE for '{confound_label}' — "
                    "a guarded source path changed since the last recorded pass; re-stamp with "
                    "`bth gate stamp` after re-verifying"
                )
            elif state == "RED":
                warnings.append(
                    f"synthetic_recovery gate '{gate_name}' RED for '{confound_label}' — "
                    "last recorded result was a failure"
                )
            else:
                warnings.append(
                    f"synthetic_recovery gate '{gate_name}' UNKNOWN for '{confound_label}' — "
                    "never stamped; run `bth gate stamp` after verifying the pipeline component"
                )
        else:
            infos.append(
                f"skipping synthetic_recovery gate-state check for '{confound_label}' — no workspace root"
            )

    # AC-04: zero-power lint — planned_run_label where all hypothesis pairs predict identical outcome
    from collections import defaultdict

    outcomes_by_label: dict[str, set[str]] = defaultdict(set)
    count_by_label: dict[str, int] = defaultdict(int)
    for disc in claim.discriminability:
        label = disc.get("planned_run_label", "")
        outcome = disc.get("predicted_outcome", "")
        if label and outcome:
            outcomes_by_label[label].add(outcome)
            count_by_label[label] += 1
    for label, outcome_set in outcomes_by_label.items():
        # Only fire if there are >=2 discriminability entries for that label
        if count_by_label[label] >= 2 and len(outcome_set) == 1:
            warnings.append(
                f"zero discriminative power for run '{label}' — all {count_by_label[label]} "
                f"hypothesis pairs predict identical outcome '{next(iter(outcome_set))}'"
            )

    # AC-05: positive-testing-bias lint — all rows predict the same outcome
    all_outcomes = {
        disc.get("predicted_outcome", "")
        for disc in claim.discriminability
        if disc.get("predicted_outcome")
    }
    if len(claim.discriminability) >= 2 and len(all_outcomes) == 1:
        warnings.append(
            f"positive-testing bias detected — all {len(claim.discriminability)} discriminability entries predict the same outcome '{next(iter(all_outcomes))}'; no run in the matrix challenges the primary hypothesis"
        )

    return ValidationResult(ok=len(errors) == 0, errors=errors, warnings=warnings, infos=infos)


def scaffold_claim(campaign_id: str, db: duckdb.DuckDBPyConnection, workspace_root: Path) -> Path:
    """Create a claim.bth.toml template for a campaign.

    Args:
        campaign_id: Campaign ID (short or full UUID)
        db: DuckDB connection
        workspace_root: Root of project workspace

    Returns:
        Path to created claim.bth.toml file

    Raises:
        RuntimeError: If campaign not found or directory cannot be created
    """
    from bathos.campaigns import CampaignError, _resolve_campaign_id

    try:
        full_id = _resolve_campaign_id(db, campaign_id)
    except CampaignError as e:
        raise RuntimeError(f"Campaign not found: {e}") from e

    # Get campaign details
    rows = db.execute("SELECT name, hypothesis FROM campaigns WHERE id = ?", [full_id]).fetchall()
    if not rows:
        raise RuntimeError(f"Campaign {campaign_id} not found")

    campaign_name, campaign_hypothesis = rows[0]

    # Create .bth/claims directory
    claims_dir = workspace_root / ".bth" / "claims"
    claims_dir.mkdir(parents=True, exist_ok=True)

    # Generate template
    template = f"""# Claim for campaign: {campaign_name}
# Generated via bth claim scaffold

[claim]
headline = "REQUIRED: One-sentence summary of what this campaign tests"
kill_condition = "REQUIRED: Under what conditions would the result contradict the hypothesis?"
# REQUIRED (debt #1071): is a null result a live possibility this kill_condition needs to
# rule out? If true, at least one [[claim.union_gate.clauses]] entry below must set
# positive_control = true, proving (via a [differential] pre-flight) that the instrument
# can actually detect a known-real effect -- otherwise a null result is unfalsifiable-by-
# instrument-failure.
kill_condition_satisfiable_by_null = false
regime = "Optional: Parameter ranges or conditions claimed to be covered"

[[hypotheses]]
id = "H_information_symmetry"
label = "REQUIRED: Descriptive label for primary hypothesis"
predicted_signature = "Optional: Expected metric fingerprint"

[[hypotheses]]
id = "H_null_misspec"
label = "REQUIRED: Null or misspecification hypothesis"
predicted_signature = "Optional: Expected metric fingerprint if null hypothesis is true"

[[assumptions]]
id = "A_measurement_valid"
label = "REQUIRED: Descriptive assumption label"

[[confounds]]
id = "C_topology_coupling"
label = "REQUIRED: Confound label"
[confounds.reference_parity]
reference_paper = "Optional: Citation if baseline from literature"
reference_metric = "Optional: metric key in baseline run"
reference_value = 1.0
equivalence_bound = 0.05
parity_run_id = ""

[[confounds]]
id = "C_pipeline_soundness"
label = "REQUIRED: which pipeline component this campaign's runs depend on"
[confounds.synthetic_recovery]
gate_name = "REQUIRED: a name for the known-answer invariant test that proves this component sound"
guards = ["REQUIRED: source paths whose change invalidates a recorded green stamp"]
# Prove the invariant test passes yourself, then: bth gate stamp <gate_name> --result pass

[claim.discriminability]
# Matrix indexed by hypothesis-pair × outcome-label
# predicted_outcome: any outcome label from the runs, or "??" for unspecified
[[claim.discriminability]]
hypothesis_a = "H_information_symmetry"
hypothesis_b = "H_null_misspec"
planned_run_label = "outcome_1"
predicted_outcome = "??  # EDIT: assign expected outcome if run exists"

[claim.union_gate]
[[claim.union_gate.clauses]]
id = "C_main"
description = "REQUIRED: What does this clause discriminate?"
hypothesis_ids = ["H_information_symmetry", "H_null_misspec"]
# positive_control = true  # set true (+ kill_condition_satisfiable_by_null=true above) if
                           # this clause is proven by a [differential] pre-flight run
"""

    claim_path = claims_dir / f"{campaign_name}.claim.toml"
    claim_path.write_text(template)

    event("claim.scaffold", campaign_id=full_id, claim_path=str(claim_path))

    return claim_path


def resolve_claim_path(path_relative: str, workspace_root: Path) -> Path:
    """Resolve a claim path and reject escapes of workspace_root."""
    root = workspace_root.resolve()
    raw = Path(path_relative)
    candidate = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    if not candidate.is_relative_to(root):
        raise RuntimeError(
            f"Claim file must be within workspace root. Path {path_relative} escapes {workspace_root}"
        )
    return candidate


def register_claim(
    path: Path,
    campaign_id: str,
    db: duckdb.DuckDBPyConnection,
    workspace_root: Path,
    force: bool = False,
    catalog_dir: Path | None = None,
) -> None:
    """Register a claim file with a campaign.

    Args:
        path: Path to claim.bth.toml (relative or absolute)
        campaign_id: Campaign ID
        db: DuckDB connection
        workspace_root: Project workspace root
        force: If True, allow re-registration and write audit event

    Raises:
        RuntimeError: If path is absolute or escapes workspace, or campaign not found
    """
    from bathos.campaigns import CampaignError, _resolve_campaign_id, ingest_cool_campaigns

    abs_path = resolve_claim_path(str(path), workspace_root)
    rel_path = abs_path.relative_to(workspace_root.resolve())

    if not abs_path.exists():
        raise FileNotFoundError(f"Claim file not found at {abs_path}")

    try:
        full_id = _resolve_campaign_id(db, campaign_id, catalog_dir=catalog_dir)
    except CampaignError as e:
        raise RuntimeError(f"Campaign not found: {e}") from e

    if catalog_dir is not None:
        ingest_cool_campaigns(db, catalog_dir)

    # Compute SHA256
    claim_content = abs_path.read_bytes()
    claim_sha256 = hashlib.sha256(claim_content).hexdigest()

    # Check if already registered
    existing = db.execute("SELECT claim_sha256 FROM campaigns WHERE id = ?", [full_id]).fetchall()
    if not existing:
        if catalog_dir is None:
            raise RuntimeError(f"Campaign not found in warm catalog: {full_id}")
        from bathos.campaigns import read_cool_campaigns

        cool = next((c for c in read_cool_campaigns(catalog_dir) if c.id == full_id), None)
        if cool is None:
            raise RuntimeError(f"Campaign not found in warm catalog: {full_id}")
        db.execute(
            """
            INSERT INTO campaigns (
                id, project_slug, name, mode, question, hypothesis, status,
                started_at, concluded_at, conclusion, outcome_label,
                parent_campaign_id, stopping_threshold, negative_check
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                cool.id,
                cool.project_slug,
                cool.name,
                cool.mode,
                cool.question,
                cool.hypothesis,
                cool.status,
                cool.started_at,
                cool.concluded_at,
                cool.conclusion,
                cool.outcome_label,
                cool.parent_campaign_id,
                cool.stopping_threshold,
                cool.negative_check,
            ],
        )
        existing = [(None,)]
    if existing and existing[0][0] is not None:
        if not force:
            raise RuntimeError(
                f"Campaign {campaign_id[:8]} already has a registered claim. "
                "Use --force to re-register."
            )
        # Write audit event for re-registration
        event("claim.register_force", campaign_id=full_id, claim_path=str(rel_path))

    # Update campaigns table
    db.execute(
        "UPDATE campaigns SET claim_path = ?, claim_sha256 = ? WHERE id = ?",
        [str(rel_path), claim_sha256, full_id],
    )
    stored = db.execute("SELECT claim_sha256 FROM campaigns WHERE id = ?", [full_id]).fetchone()
    if stored is None or stored[0] != claim_sha256:
        raise RuntimeError(f"Campaign not found in warm catalog; cannot register claim: {full_id}")
    if catalog_dir is not None:
        from bathos.campaigns import get_campaign, write_campaign_cool

        refreshed = get_campaign(db, full_id, catalog_dir=catalog_dir)
        if refreshed is not None:
            write_campaign_cool(refreshed, catalog_dir)

    event(
        "claim.register", campaign_id=full_id, claim_path=str(rel_path), claim_sha256=claim_sha256
    )

    # BP-2: advisory-only synthetic_recovery gate check at register time. This is NOT a hard
    # block -- register has no run-history context to judge downgrade severity from -- it just
    # surfaces a not-yet-GREEN gate as early as possible, well before conclude time.
    from bathos.gate import synthetic_recovery_confound_check

    claim = parse_claim(abs_path)
    synth_result = synthetic_recovery_confound_check(claim, workspace_root)
    for confound in synth_result.get("confounds", []):
        if confound["status"] == "uncontrolled":
            print(
                f"WARNING: synthetic_recovery gate '{confound['gate_name']}' is "
                f"{confound['gate_state']} for '{confound['label']}' — this will downgrade the "
                "verdict at `bth campaign conclude` unless re-stamped GREEN with `bth gate stamp` "
                "before then."
            )


def check_sha(path_relative: str, registered_sha: str, workspace_root: Path) -> None:
    """Check that claim file SHA256 matches registered value.

    Args:
        path_relative: Relative path to claim file (from campaigns.claim_path)
        registered_sha: SHA256 registered at claim_register time
        workspace_root: Project workspace root

    Raises:
        FileNotFoundError: If claim file not found
        ValueError: If SHA256 mismatch
    """
    abs_path = resolve_claim_path(path_relative, workspace_root)
    if not abs_path.exists():
        raise FileNotFoundError(f"Claim file not found at {abs_path}")

    current_sha = hashlib.sha256(abs_path.read_bytes()).hexdigest()
    if current_sha != registered_sha:
        raise ValueError(
            "Claim file SHA256 mismatch. File has been modified since registration. "
            "Re-register with `bth claim register --force` to acknowledge the amendment."
        )


def differential_confound_check(
    db: duckdb.DuckDBPyConnection,
    campaign_id: str,
    claim: ClaimFile,
    workspace_root: Path | None = None,
) -> dict:
    """Check positive_control union_gate clauses for live instrument-sensitivity proof (debt #1071).

    Same output shape as `parity_confound_check`: one status per relevant clause, re-derived
    from live run state at call time (not cached at register time), so a clause that was
    "controlled" when a run first covered it can go "uncontrolled" later if the dependency
    environment has since drifted -- exactly the failure mode this debt was filed after (a
    package re-pin silently invalidating every prior differential/SC result).

    A clause tagged `positive_control = true` is "controlled" iff at least one run covering
    its `hypothesis_ids` (same covering-run search `run_union_gate` uses) has BOTH:
      - `differential_status == "passed"` (its own inline [differential] pre-flight fired
        the declared invariant -- proof the instrument could detect a real effect at run time)
      - a `dependency_lock_sha256` that still matches the current `uv.lock` (not stale)
    Otherwise "uncontrolled". Clauses not tagged `positive_control` are omitted entirely.

    Args:
        db: DuckDB connection
        campaign_id: Campaign ID (full or prefix)
        claim: Parsed claim file
        workspace_root: Project workspace root for dependency-lock drift comparison; defaults
            to `resolve_workspace(Path.cwd()).fs_root`

    Returns:
        {"clauses": [{"id": str, "label": str, "status": "controlled"|"uncontrolled"}, ...]}
    """
    from bathos.checker import check_dependency_lock_drift

    if workspace_root is None:
        from bathos.workspace import resolve_workspace

        workspace_root = resolve_workspace(Path.cwd()).fs_root

    results = []
    for clause in claim.union_gate_clauses:
        if clause.get("positive_control") is not True:
            continue

        clause_id = clause.get("id", "?")
        hypothesis_ids = clause.get("hypothesis_ids", [])
        label = format_clause_ref(clause)

        covered_runs = db.execute(
            """
            SELECT cr.run_id FROM campaign_runs cr
            JOIN runs r ON cr.run_id = r.id
            WHERE cr.campaign_id = ?
              AND r.claim_discriminates IS NOT NULL
            """,
            [campaign_id],
        ).fetchall()

        status = "uncontrolled"
        for (run_id,) in covered_runs:
            rows = db.execute(
                "SELECT claim_discriminates, differential_status, dependency_lock_sha256 "
                "FROM runs WHERE id = ?",
                [run_id],
            ).fetchall()
            if not rows or not rows[0][0]:
                continue
            disc_json, differential_status, dependency_lock_sha256 = rows[0]
            try:
                disc_list = json.loads(disc_json)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(disc_list, list) or not all(h in disc_list for h in hypothesis_ids):
                continue
            if differential_status != "passed":
                continue
            if check_dependency_lock_drift(dependency_lock_sha256, workspace_root):
                continue
            status = "controlled"
            break

        results.append({"id": clause_id, "label": label, "status": status})

    return {"clauses": results}


def run_union_gate(
    db: duckdb.DuckDBPyConnection,
    campaign_id: str,
    claim: ClaimFile,
    workspace_root: Path | None = None,
) -> tuple[str, list[str]]:
    """Run the union gate check for a campaign.

    Args:
        db: DuckDB connection
        campaign_id: Campaign ID
        claim: Parsed claim file
        workspace_root: Project workspace root, threaded to `differential_confound_check`
            for clauses tagged `positive_control` (debt #1071); defaults to cwd-resolved
            workspace when omitted.

    Returns:
        Tuple of (verdict, uncovered_clause_ids) where verdict is 'covered' or 'confounded'
        and uncovered_clause_ids is a list of clause IDs that have no covering runs
    """
    uncovered_clauses = []
    positive_control_status: dict[str, str] | None = None

    for clause in claim.union_gate_clauses:
        clause_id = clause.get("id", "?")
        hypothesis_ids = clause.get("hypothesis_ids", [])

        if clause.get("positive_control") is True:
            # Lazily computed once, memoized across however many positive_control clauses
            # this claim declares (usually one) -- avoids re-running the covering-run query
            # per clause.
            if positive_control_status is None:
                positive_control_status = {
                    c["id"]: c["status"]
                    for c in differential_confound_check(db, campaign_id, claim, workspace_root)[
                        "clauses"
                    ]
                }
            if positive_control_status.get(clause_id) != "controlled":
                uncovered_clauses.append(clause_id)
            continue

        # Find a run that covers ALL hypothesis_ids in this clause
        covered_runs = db.execute(
            """
            SELECT cr.run_id FROM campaign_runs cr
            JOIN runs r ON cr.run_id = r.id
            WHERE cr.campaign_id = ?
              AND r.claim_discriminates IS NOT NULL
            """,
            [campaign_id],
        ).fetchall()

        covered = False
        for (run_id,) in covered_runs:
            # Get claim_discriminates JSON array
            rows = db.execute(
                "SELECT claim_discriminates FROM runs WHERE id = ?", [run_id]
            ).fetchall()
            if rows and rows[0][0]:
                try:
                    disc_list = json.loads(rows[0][0])
                    if isinstance(disc_list, list) and all(
                        h_id in disc_list for h_id in hypothesis_ids
                    ):
                        covered = True
                        break
                except (json.JSONDecodeError, TypeError):
                    pass

        if not covered:
            uncovered_clauses.append(clause_id)

    verdict = "covered" if not uncovered_clauses else "confounded"
    return (verdict, uncovered_clauses)


def attest_parity(
    campaign_id: str,
    parity_run_id: str,
    db: duckdb.DuckDBPyConnection,
    workspace_root: Path,
) -> None:
    """Bind a parity run to a campaign's claim and re-anchor the claim SHA (atomic).

    AC-11, AC-12, AC-13, AC-21: Validates that the cited run is a real passing
    parity run (outcome='pass' or 'partial', metadata.parity_run_type='literature_parity'),
    binds its ID into the claim's [confounds.reference_parity] block, and updates the
    DB SHA atomically via temp-write → fsync → os.replace → DB-update-last.

    ATOMICITY & RECOVERY CONTRACT (AC-21, R2):
    - Write new content to temp file, fsync, os.replace (atomic on POSIX).
    - Compute new SHA and UPDATE DB last.
    - On DB-update failure: ROLL BACK the file to original content (best-effort true rollback).
    - Reconcile-on-entry backstop: if file SHA != DB SHA at entry, log warning and proceed
      (a prior crash that even rollback didn't catch is recovered by re-running attest_parity).
    After recovery, file and DB are always consistent at either the OLD state or the NEW state,
    never diverged.

    Args:
        campaign_id: Campaign ID (short or full UUID)
        parity_run_id: Run ID of the parity run to bind
        db: DuckDB connection
        workspace_root: Project workspace root

    Raises:
        ValueError: If run not found, missing parity_run_type, wrong type, or outcome not pass/partial
        RuntimeError: If claim file not found or campaign not found
    """
    import logging
    import os
    import tempfile

    from bathos.campaigns import CampaignError, _resolve_campaign_id

    logger = logging.getLogger(__name__)

    try:
        full_id = _resolve_campaign_id(db, campaign_id)
    except CampaignError as e:
        raise RuntimeError(f"Campaign not found: {e}") from e

    # Get campaign's claim path and current DB SHA
    rows = db.execute(
        "SELECT claim_path, claim_sha256 FROM campaigns WHERE id = ?", [full_id]
    ).fetchall()
    if not rows or rows[0][0] is None:
        raise RuntimeError(f"Campaign {campaign_id} has no registered claim")

    claim_path_rel = rows[0][0]
    stored_db_sha = rows[0][1]
    abs_claim_path = resolve_claim_path(claim_path_rel, workspace_root)

    if not abs_claim_path.exists():
        raise FileNotFoundError(f"Claim file not found at {abs_claim_path}")

    # RECONCILE-ON-ENTRY BACKSTOP: if file SHA != DB SHA, log warning and proceed
    # (evidence of a prior crash; re-running attest_parity recovers it)
    original_content = abs_claim_path.read_bytes()
    original_content_str = original_content.decode("utf-8")
    file_sha_at_entry = hashlib.sha256(original_content).hexdigest()
    if file_sha_at_entry != stored_db_sha:
        logger.warning(
            f"Reconciling claim SHA after prior interrupted attestation for campaign {full_id}: "
            f"file SHA {file_sha_at_entry} != DB SHA {stored_db_sha}. "
            f"Proceeding with attest_parity, which will re-anchor the DB SHA."
        )

    # AC-12: Validate that parity_run_id is a real passing parity run
    run_rows = db.execute(
        "SELECT outcome, metadata, parity_run_type FROM runs WHERE id = ? OR id LIKE ?",
        [parity_run_id, parity_run_id + "%"],
    ).fetchall()

    if not run_rows:
        raise ValueError(f"Parity run '{parity_run_id}' not found in catalog")

    outcome, metadata_json, parity_run_type_col = run_rows[0]

    # Validate outcome is pass or partial
    if outcome not in ("pass", "partial"):
        raise ValueError(
            f"Parity run '{parity_run_id}' has outcome='{outcome}', expected 'pass' or 'partial'"
        )

    # Step 6a: Use parity_run_type column instead of metadata JSON
    # The column is now authoritative; metadata JSON is kept for readability
    parity_type = parity_run_type_col
    if not parity_type:
        raise ValueError(
            f"Run '{parity_run_id}' metadata missing 'parity_run_type' key. "
            "Ensure run was executed with parity_run_type set."
        )

    if parity_type != "literature_parity":
        raise ValueError(
            f"Run '{parity_run_id}' has parity_run_type='{parity_type}', "
            "expected 'literature_parity'"
        )

    # Parse the current claim
    claim = parse_claim(abs_claim_path)

    # Find the confound with reference_parity and update it
    updated = False
    for confound in claim.confounds:
        if "reference_parity" in confound:
            confound["reference_parity"]["parity_run_id"] = parity_run_id
            updated = True
            break

    if not updated:
        raise ValueError(
            f"Campaign {campaign_id}'s claim has no [confounds.reference_parity] block"
        )

    # R2: Atomic write-then-rename pattern with best-effort rollback on DB failure
    # Write to temp file in same directory (ensure same filesystem for atomic rename)
    temp_dir = abs_claim_path.parent
    with tempfile.NamedTemporaryFile(
        mode="w", dir=temp_dir, suffix=".tmp", delete=False, encoding="utf-8"
    ) as tmp_f:
        temp_path = Path(tmp_f.name)
        # Find the parity_run_id = "" line in reference_parity block and replace it
        updated_content = original_content_str.replace(
            'parity_run_id = ""', f'parity_run_id = "{parity_run_id}"'
        )

        # Assertion: ensure replacement actually occurred (prevent silent no-op)
        if updated_content == original_content_str:
            raise ValueError(
                "parity_run_id already set or TOML format mismatch — use force to re-attest. "
                "The claim file does not contain the expected 'parity_run_id = \"\"' line."
            )

        tmp_f.write(updated_content)
        tmp_f.flush()
        os.fsync(tmp_f.fileno())

    try:
        # Atomic rename (this is atomic on POSIX systems)
        os.replace(temp_path, abs_claim_path)

        # Now compute the new SHA256
        new_content = abs_claim_path.read_bytes()
        new_sha256 = hashlib.sha256(new_content).hexdigest()

        # DB update LAST (after file is safely renamed)
        try:
            db.execute("UPDATE campaigns SET claim_sha256 = ? WHERE id = ?", [new_sha256, full_id])

            event(
                "claim.attest_parity",
                campaign_id=full_id,
                parity_run_id=parity_run_id,
                claim_sha256=new_sha256,
            )
        except Exception as db_error:
            # DB update failed AFTER file was already renamed.
            # BEST-EFFORT TRUE ROLLBACK: restore the file to original content,
            # so file and DB are consistent at the OLD state again.
            logger.error(
                f"DB update failed for campaign {full_id}; rolling back file to original state. "
                f"Error: {db_error}"
            )
            with tempfile.NamedTemporaryFile(
                mode="w", dir=temp_dir, suffix=".tmp", delete=False, encoding="utf-8"
            ) as rollback_f:
                rollback_path = Path(rollback_f.name)
                rollback_f.write(original_content_str)
                rollback_f.flush()
                os.fsync(rollback_f.fileno())

            try:
                os.replace(rollback_path, abs_claim_path)
                logger.info(
                    f"File successfully rolled back to original state. "
                    f"File and DB are now consistent at the original SHA {file_sha_at_entry}."
                )
            except Exception as rollback_error:
                logger.critical(
                    f"Rollback itself failed! File may be in inconsistent state. "
                    f"Manual recovery required. Original error: {db_error}, Rollback error: {rollback_error}"
                )
                with suppress(Exception):
                    rollback_path.unlink()
            # Re-raise the original DB error
            raise

    except Exception:
        # Clean up temp file if it still exists (e.g., if os.replace failed)
        if temp_path.exists():
            with suppress(Exception):
                temp_path.unlink()
        raise


def parity_confound_check(
    claim_path: Path,
    db: duckdb.DuckDBPyConnection | None = None,
) -> dict:
    """Check confounds with reference_parity blocks and infer their status from live runs.

    For each confound with a [confounds.reference_parity] block carrying a parity_run_id,
    queries the run's outcome and metadata.parity_run_type to infer:
    - 'controlled' if outcome='pass' and parity_run_type='literature_parity'
    - 'controlled-by-protocol' if outcome='partial' and parity_run_type='literature_parity'
    - 'uncontrolled' if parity_run_id is empty, run not found, or output SHA drifted (AC-20)

    Args:
        claim_path: Path to claim.bth.toml
        db: Optional DuckDB connection (if None, all parity confounds marked 'uncontrolled')

    Returns:
        Dict with 'confounds' key containing list of confound dicts with 'status' inferred
    """
    claim = parse_claim(claim_path)
    result_confounds = []

    for confound in claim.confounds:
        ref_par = confound.get("reference_parity", {})
        if not ref_par:
            # No parity block, skip
            continue

        confound_info = {
            "id": confound.get("id", "unknown"),
            "label": display_label(confound),
            "status": "uncontrolled",  # default
        }

        parity_run_id = ref_par.get("parity_run_id", "")

        if not parity_run_id:
            # Empty parity_run_id
            confound_info["status"] = "uncontrolled"
        elif db is not None:
            # Query the run (output_metadata column may be absent in minimal test schemas)
            output_metadata_json: str | None = None
            try:
                run_rows = db.execute(
                    "SELECT outcome, metadata, parity_run_type, output_metadata FROM runs WHERE id = ? OR id LIKE ?",
                    [parity_run_id, parity_run_id + "%"],
                ).fetchall()
                if run_rows:
                    outcome, metadata_json, parity_run_type_col, output_metadata_json = run_rows[0]
            except Exception:
                run_rows = db.execute(
                    "SELECT outcome, metadata, parity_run_type FROM runs WHERE id = ? OR id LIKE ?",
                    [parity_run_id, parity_run_id + "%"],
                ).fetchall()
                if run_rows:
                    outcome, metadata_json, parity_run_type_col = run_rows[0]
                else:
                    run_rows = []

            if not run_rows:
                # Run not found
                confound_info["status"] = "uncontrolled"
            else:
                # Use the parity_run_type COLUMN as authoritative — it survives cool→warm
                # compaction. The metadata JSON path is unreliable (NULL after compact).
                parity_type = parity_run_type_col or ""

                # Infer status from outcome and parity_type
                if parity_type == "literature_parity":
                    if outcome == "pass":
                        confound_info["status"] = "controlled"
                    elif outcome == "partial":
                        confound_info["status"] = "controlled-by-protocol"
                    else:
                        confound_info["status"] = "uncontrolled"
                else:
                    confound_info["status"] = "uncontrolled"

                # AC-20: downgrade controlled parity when verdict artifacts drift on disk
                if confound_info["status"] in ("controlled", "controlled-by-protocol"):
                    from bathos.checker import output_metadata_has_sha_drift

                    if output_metadata_has_sha_drift(output_metadata_json):
                        confound_info["status"] = "uncontrolled"
        else:
            # DB is None, mark as uncontrolled
            confound_info["status"] = "uncontrolled"

        result_confounds.append(confound_info)

    return {"confounds": result_confounds}


def review_coverage_check(db, campaign_id: str, claim, workspace_root=None) -> dict:
    """Review Coverage Gate (build-order step 3).

    Walks the claim's hypotheses and confounds and requires each to be covered by at least one
    *substantive* `[review]` entry — literature or implementation — whose `bears_on` names it,
    drawn from the sidecars of the campaign's member runs.

    "Substantive" is `sidecar.covering_id`, which is the C1 tier bar plus the `bears_on`
    binding. Reusing it is load-bearing: this gate previously counted any entry carrying a
    `bears_on` and inspected no other field, so a `[[review.literature]]` entry consisting of
    nothing but `bears_on = "H1"` marked H1 covered and let a confirmation campaign conclude.
    Authoring empty placeholders was therefore a way to satisfy the gate outright. The standard
    lives in `sidecar.py` beside `review_tier` so there is one definition, not two that drift.

    Returns ``{"verdict": "covered"|"uncovered"|"empty_slate", "uncovered": [...],
    "covered": [...], "entries_seen": int, "sidecars_read": int, "sidecars_unreadable": int}``.

    Binary by construction — there is no threshold to calibrate. §7 gated this step behind
    observing real `[review]` data specifically so a *numeric* gate would not be guessed; a
    covered/not-covered gate has no such parameter, so building it now introduces no
    uncalibrated constant.

    An EMPTY slate returns ``empty_slate``, never ``covered``. §4: "A campaign in a gated mode
    whose claim declares zero hypotheses and zero confounds satisfies 'each is covered'
    trivially. The gate must treat an empty required-set as uncovered/error, not covered."

    `sidecars_unreadable` is reported rather than swallowed: a sidecar that could not be parsed
    is not evidence of absent review, and a caller must be able to tell the two apart.
    """
    from pathlib import Path

    from bathos.sidecar import covering_id, parse_sidecar

    required: list[tuple[str, str]] = []
    for h in claim.hypotheses:
        if isinstance(h, dict) and h.get("id"):
            required.append(("hypothesis", h["id"]))
    for c in claim.confounds:
        if isinstance(c, dict) and c.get("id"):
            required.append(("confound", c["id"]))

    if not required:
        return {
            "verdict": "empty_slate",
            "uncovered": [],
            "covered": [],
            "entries_seen": 0,
            "sidecars_read": 0,
            "sidecars_unreadable": 0,
        }

    rows = db.execute(
        "SELECT DISTINCT r.sidecar_path FROM runs r "
        "JOIN campaign_runs cr ON r.id = cr.run_id "
        "WHERE cr.campaign_id = ? AND r.sidecar_path IS NOT NULL AND r.sidecar_path != ''",
        [campaign_id],
    ).fetchall()

    seen_ids: set[str] = set()
    entries_seen = 0
    read = 0
    unreadable = 0

    for (sidecar_path,) in rows:
        p = Path(sidecar_path)
        if workspace_root and not p.is_absolute():
            p = Path(workspace_root) / p
        if not p.exists():
            unreadable += 1
            continue
        try:
            sc = parse_sidecar(p)
        except Exception:
            unreadable += 1
            continue
        read += 1
        if sc.review is None:
            continue
        for entry in list(sc.review.literature) + list(sc.review.implementation):
            entries_seen += 1
            if covered_id := covering_id(entry):
                seen_ids.add(covered_id)

    covered = [f"{kind}:{rid}" for kind, rid in required if rid in seen_ids]
    uncovered = [f"{kind}:{rid}" for kind, rid in required if rid not in seen_ids]

    return {
        "verdict": "covered" if not uncovered else "uncovered",
        "uncovered": uncovered,
        "covered": covered,
        "entries_seen": entries_seen,
        "sidecars_read": read,
        "sidecars_unreadable": unreadable,
    }


def citation_contradicted(claim, bears_on: str, observed_label: str) -> str:
    """§8b objection 2's truth table: does an observed outcome contradict a `supports` citation?

    Returns one of:

    - ``"contradicted"`` — a discriminability row for `observed_label` predicts the hypothesis
      named by `bears_on` is *disfavoured*. The citation said the prior work supports it; the
      run says otherwise. This is what opens a `citation_contradicted` obligation.
    - ``"consistent"`` — a row covers the label and does not disfavour the hypothesis.
    - ``"indeterminate"`` — **no discriminability row covers the observed label**, so nothing
      can be concluded either way.

    The third case is the point of this function. §8b: "Silence must not read as confirmation,
    and it must not read as refutation either." `discriminability` is optional
    (`claim.py` defaults it to `[]`) and AC-04 only lints once there are >=2 entries, so a
    confirmatory claim can legitimately carry an empty map — for which every citation is
    indeterminate rather than silently consistent.

    Evaluated at conclude, never at run-end: only conclude has the catalog and the claim in
    hand (decision D1 puts the sole binding site there).
    """
    if not bears_on or not observed_label:
        return "indeterminate"

    rows = [
        d
        for d in (claim.discriminability or [])
        if isinstance(d, dict) and d.get("planned_run_label") == observed_label
    ]
    if not rows:
        return "indeterminate"

    covers = False
    for row in rows:
        a, b = row.get("hypothesis_a"), row.get("hypothesis_b")
        if bears_on not in (a, b):
            continue
        covers = True
        predicted = row.get("predicted_outcome", "")
        # The row predicts which hypothesis this label favours. If it names the OTHER
        # hypothesis, the observed label disfavours the one the citation vouched for.
        favoured = predicted if predicted in (a, b) else None
        if favoured is not None and favoured != bears_on:
            return "contradicted"

    return "consistent" if covers else "indeterminate"


def contradicted_citations(db, campaign_id: str, claim, workspace_root=None) -> dict:
    """Apply :func:`citation_contradicted` across a campaign's member runs.

    Returns ``{"contradicted": [...], "indeterminate": [...], "evaluable": int,
    "supports_seen": int}``.

    `evaluable` is reported deliberately: §8b requires that a trigger which *cannot* fire is
    distinguishable from one that fired and found nothing. An empty `discriminability` map
    makes every citation indeterminate, and that must not look like a clean bill of health.
    """
    from pathlib import Path

    from bathos.sidecar import parse_sidecar

    rows = db.execute(
        "SELECT r.id, r.sidecar_path, r.outcome FROM runs r "
        "JOIN campaign_runs cr ON r.id = cr.run_id "
        "WHERE cr.campaign_id = ? AND r.sidecar_path IS NOT NULL AND r.sidecar_path != ''",
        [campaign_id],
    ).fetchall()

    contradicted: list[dict] = []
    indeterminate: list[dict] = []
    supports_seen = 0

    for run_id, sidecar_path, outcome in rows:
        p = Path(sidecar_path)
        if workspace_root and not p.is_absolute():
            p = Path(workspace_root) / p
        if not p.exists():
            continue
        try:
            sc = parse_sidecar(p)
        except Exception:
            continue
        if sc.review is None:
            continue
        for entry in sc.review.literature:
            if entry.disposition != "supports" or not entry.bears_on:
                continue
            supports_seen += 1
            verdict = citation_contradicted(claim, entry.bears_on, outcome or "")
            record = {
                "run_id": run_id,
                "ref": entry.ref,
                "bears_on": entry.bears_on,
                "observed_outcome": outcome or "",
            }
            if verdict == "contradicted":
                contradicted.append(record)
            elif verdict == "indeterminate":
                indeterminate.append(record)

    return {
        "contradicted": contradicted,
        "indeterminate": indeterminate,
        "evaluable": supports_seen - len(indeterminate),
        "supports_seen": supports_seen,
    }
