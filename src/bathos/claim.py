"""Claim-tier rigor: discriminability maps and union gates for confirmatory campaigns."""

from __future__ import annotations

import hashlib
import json
import re
import tomllib
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


_OPAQUE_ID_RE = re.compile(r"^[A-Z][0-9]+$")


@dataclass
class ClaimFile:
    """Parsed claim.bth.toml file."""

    headline: str
    kill_condition: str
    regime: str | None
    hypotheses: list[dict]  # {id, label, predicted_signature?}
    assumptions: list[dict]
    confounds: list[dict]
    discriminability: list[dict]  # {hypothesis_a, hypothesis_b, planned_run_label, predicted_outcome}
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
        regime=regime,
        hypotheses=hypotheses,
        assumptions=assumptions,
        confounds=confounds,
        discriminability=discriminability,
        union_gate_clauses=union_gate_clauses,
        path=path,
        sha256=sha256_hex,
    )


def validate_claim(claim: ClaimFile, db: duckdb.DuckDBPyConnection | None = None) -> ValidationResult:
    """Validate a parsed claim file.

    Args:
        claim: ClaimFile to validate
        db: Optional DuckDB connection for regime coverage check (AC-07)

    Returns:
        ValidationResult with ok=True if no errors, False otherwise
    """
    errors = []

    # AC-03: Missing headline
    if not claim.headline or claim.headline.strip() == "":
        errors.append(ValidationError("Missing or blank headline"))

    # AC-03: Blank kill_condition
    if not claim.kill_condition or claim.kill_condition.strip() == "":
        errors.append(ValidationError("kill_condition is required and must not be blank"))

    # AC-03: Fewer than 2 hypotheses
    if len(claim.hypotheses) < 2:
        errors.append(ValidationError(f"At least 2 hypotheses required, found {len(claim.hypotheses)}"))

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
        if _OPAQUE_ID_RE.match(h_id):
            if not h_label or h_label.strip() == "":
                errors.append(
                    ValidationError(
                        f"Opaque hypothesis id '{h_id}' must have a descriptive label field (found blank)"
                    )
                )

    for c in claim.confounds:
        c_id = c.get("id", "")
        c_label = c.get("label", "")
        if _OPAQUE_ID_RE.match(c_id):
            if not c_label or c_label.strip() == "":
                errors.append(
                    ValidationError(
                        f"Opaque confound id '{c_id}' must have a descriptive label field (found blank)"
                    )
                )

    # AC-03: Check discriminability entries for missing predicted_outcome
    for disc in claim.discriminability:
        if "predicted_outcome" not in disc or not disc.get("predicted_outcome"):
            h_a = disc.get("hypothesis_a", "?")
            h_b = disc.get("hypothesis_b", "?")
            label = disc.get("planned_run_label", "?")
            errors.append(
                ValidationError(
                    f"Discriminability entry for {h_a} vs {h_b} (run {label}) missing predicted_outcome"
                )
            )

    return ValidationResult(ok=len(errors) == 0, errors=errors)


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
    from bathos.campaigns import _resolve_campaign_id, CampaignError

    try:
        full_id = _resolve_campaign_id(db, campaign_id)
    except CampaignError as e:
        raise RuntimeError(f"Campaign not found: {e}") from e

    # Get campaign details
    rows = db.execute(
        "SELECT name, hypothesis FROM campaigns WHERE id = ?", [full_id]
    ).fetchall()
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
regime = "Optional: Parameter ranges or conditions claimed to be covered"

[[hypotheses]]
id = "H_primary"
label = "REQUIRED: Descriptive label for primary hypothesis"
predicted_signature = "Optional: Expected metric fingerprint"

[[hypotheses]]
id = "H_null"
label = "REQUIRED: Null or misspecification hypothesis"
predicted_signature = "Optional: Expected metric fingerprint if null hypothesis is true"

[[assumptions]]
id = "A_1"
label = "REQUIRED: Descriptive assumption label"

[[confounds]]
id = "C_1"
label = "REQUIRED: Confound label"
[confounds.reference_parity]
reference_paper = "Optional: Citation if baseline from literature"
reference_metric = "Optional: metric key in baseline run"
reference_value = 1.0
equivalence_bound = 0.05

[claim.discriminability]
# Matrix indexed by hypothesis-pair × outcome-label
# predicted_outcome: any outcome label from the runs, or "??" for unspecified
[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "outcome_1"
predicted_outcome = "??  # EDIT: assign expected outcome if run exists"

[claim.union_gate]
[[claim.union_gate.clauses]]
id = "C_main"
description = "REQUIRED: What does this clause discriminate?"
hypothesis_ids = ["H_primary", "H_null"]
"""

    claim_path = claims_dir / f"{campaign_name}.claim.toml"
    claim_path.write_text(template)

    event("claim.scaffold", campaign_id=full_id, claim_path=str(claim_path))

    return claim_path


def register_claim(
    path: Path,
    campaign_id: str,
    db: duckdb.DuckDBPyConnection,
    workspace_root: Path,
    force: bool = False,
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
    from bathos.campaigns import _resolve_campaign_id, CampaignError

    # Resolve path to relative if absolute
    if path.is_absolute():
        try:
            rel_path = path.relative_to(workspace_root)
        except ValueError:
            raise RuntimeError(
                f"Claim file must be within workspace root. Path {path} escapes {workspace_root}"
            )
    else:
        rel_path = path

    # Verify file exists
    abs_path = workspace_root / rel_path
    if not abs_path.exists():
        raise FileNotFoundError(f"Claim file not found at {abs_path}")

    try:
        full_id = _resolve_campaign_id(db, campaign_id)
    except CampaignError as e:
        raise RuntimeError(f"Campaign not found: {e}") from e

    # Compute SHA256
    claim_content = abs_path.read_bytes()
    claim_sha256 = hashlib.sha256(claim_content).hexdigest()

    # Check if already registered
    existing = db.execute(
        "SELECT claim_sha256 FROM campaigns WHERE id = ?", [full_id]
    ).fetchall()
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

    event("claim.register", campaign_id=full_id, claim_path=str(rel_path), claim_sha256=claim_sha256)


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
    abs_path = workspace_root / path_relative
    if not abs_path.exists():
        raise FileNotFoundError(f"Claim file not found at {abs_path}")

    current_sha = hashlib.sha256(abs_path.read_bytes()).hexdigest()
    if current_sha != registered_sha:
        raise ValueError(
            f"Claim file SHA256 mismatch. File has been modified since registration. "
            f"Re-register with `bth claim register --force` to acknowledge the amendment."
        )


def run_union_gate(
    db: duckdb.DuckDBPyConnection, campaign_id: str, claim: ClaimFile
) -> tuple[str, list[str]]:
    """Run the union gate check for a campaign.

    Args:
        db: DuckDB connection
        campaign_id: Campaign ID
        claim: Parsed claim file

    Returns:
        Tuple of (verdict, uncovered_clause_ids) where verdict is 'covered' or 'confounded'
        and uncovered_clause_ids is a list of clause IDs that have no covering runs
    """
    uncovered_clauses = []

    for clause in claim.union_gate_clauses:
        clause_id = clause.get("id", "?")
        hypothesis_ids = clause.get("hypothesis_ids", [])

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
                    if isinstance(disc_list, list):
                        # Check if ALL hypothesis_ids are in this run
                        if all(h_id in disc_list for h_id in hypothesis_ids):
                            covered = True
                            break
                except (json.JSONDecodeError, TypeError):
                    pass

        if not covered:
            uncovered_clauses.append(clause_id)

    verdict = "covered" if not uncovered_clauses else "confounded"
    return (verdict, uncovered_clauses)
