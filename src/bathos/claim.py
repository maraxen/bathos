from __future__ import annotations

import tomllib
import hashlib
import json
from pathlib import Path
from dataclasses import dataclass, field
from typing import Optional

@dataclass
class ValidationError:
    message: str

@dataclass
class ValidationResult:
    ok: bool
    errors: list[ValidationError] = field(default_factory=list)

@dataclass
class ClaimFile:
    """Represents a parsed claim.bth.toml file."""
    headline: str
    kill_condition: str
    regime: Optional[str]
    hypotheses: list[dict]
    assumptions: list[dict]
    confounds: list[dict]
    discriminability: list[dict]
    union_gate_clauses: list[dict]
    path: Path
    sha256: str


def parse_claim(path: Path) -> ClaimFile:
    """Parse a claim.bth.toml file and compute its SHA256 hash.

    Raises:
        ValueError: If the file cannot be parsed or required sections are missing.
        FileNotFoundError: If the file does not exist.
    """
    if not path.exists():
        raise FileNotFoundError(f"Claim file not found: {path}")

    try:
        with open(path, "rb") as f:
            content = f.read()
        data = tomllib.loads(content.decode('utf-8'))
    except Exception as e:
        raise ValueError(f"Failed to parse TOML: {e}") from e

    # Extract claim section
    claim_section = data.get("claim", {})

    headline = claim_section.get("headline", "")
    kill_condition = claim_section.get("kill_condition", "")
    regime = claim_section.get("regime")

    hypotheses = data.get("hypotheses", [])
    assumptions = data.get("assumptions", [])
    confounds = data.get("confounds", [])
    discriminability = claim_section.get("discriminability", [])
    union_gate = claim_section.get("union_gate", {})
    union_gate_clauses = union_gate.get("clauses", []) if isinstance(union_gate, dict) else []

    # Compute SHA256 of file content
    sha256_hash = hashlib.sha256(content).hexdigest()

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
        sha256=sha256_hash,
    )


def validate_claim(claim: ClaimFile, db=None) -> ValidationResult:
    """Validate the structural integrity of a claim file.

    Args:
        claim: Parsed ClaimFile object
        db: Optional DuckDB connection for regime coverage checks

    Returns:
        ValidationResult with list of errors (empty list if valid)
    """
    errors = []

    # AC-03: Missing headline
    if not claim.headline or claim.headline.strip() == "":
        errors.append(ValidationError("Missing or blank headline"))

    # AC-03: Blank kill_condition
    if not claim.kill_condition or claim.kill_condition.strip() == "":
        errors.append(ValidationError("kill_condition is mandatory and cannot be blank"))

    # AC-03: Fewer than 2 hypotheses
    if len(claim.hypotheses) < 2:
        errors.append(ValidationError("At least 2 hypotheses are required"))

    # AC-03: No null/misspec hypothesis
    has_null_or_misspec = any(
        "null" in h.get("id", "").lower() or "misspec" in h.get("id", "").lower()
        for h in claim.hypotheses
    )
    if not has_null_or_misspec:
        errors.append(ValidationError("No hypothesis with id containing 'null' or 'misspec'"))

    # AC-03: Opaque IDs (matching /^[A-Z][0-9]+$/) without label
    for h in claim.hypotheses + claim.confounds:
        h_id = h.get("id", "")
        h_label = h.get("label", "")
        # Check if ID matches pattern ^[A-Z][0-9]+$
        if h_id and len(h_id) >= 2 and h_id[0].isupper() and h_id[1].isdigit():
            # Only digits and first char uppercase
            if all(c.isdigit() or c.isupper() for c in h_id):
                if not h_label or h_label.strip() == "":
                    errors.append(ValidationError(
                        f"Opaque ID '{h_id}' requires mandatory label field with non-blank value"
                    ))

    # AC-03: Missing predicted_outcome in discriminability
    for disc_entry in claim.discriminability:
        if "predicted_outcome" not in disc_entry or disc_entry["predicted_outcome"] is None:
            errors.append(ValidationError(
                "Discriminability entry missing predicted_outcome: "
                f"hypothesis_a={disc_entry.get('hypothesis_a')}, "
                f"hypothesis_b={disc_entry.get('hypothesis_b')}, "
                f"planned_run_label={disc_entry.get('planned_run_label')}"
            ))

    # AC-07: Disconfirming regime lint (requires db)
    if db and claim.regime:
        try:
            # Query the union of result_schema values for this campaign
            # This is deferred to Phase 2 when we have full campaign context
            pass
        except Exception:
            pass

    ok = len(errors) == 0
    return ValidationResult(ok=ok, errors=errors)


def scaffold_claim(campaign_id: str, db, workspace_root: Path) -> Path:
    """Generate a pre-populated claim.bth.toml template for a campaign.

    Args:
        campaign_id: UUID of the campaign
        db: DuckDB connection
        workspace_root: Root of the workspace

    Returns:
        Path to the created claim file

    Raises:
        RuntimeError: If campaign not found or .bth/claims dir cannot be created
    """
    try:
        row = db.execute(
            "SELECT name, mode FROM campaigns WHERE id=?", [campaign_id]
        ).fetchone()
    except Exception as e:
        raise RuntimeError(f"Failed to query campaign: {e}") from e

    if not row:
        raise RuntimeError(f"Campaign not found: {campaign_id}")

    campaign_name, campaign_mode = row

    # Create .bth/claims directory
    claims_dir = workspace_root / ".bth" / "claims"
    try:
        claims_dir.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        raise RuntimeError(f"Failed to create claims directory: {e}") from e

    claim_path = claims_dir / f"{campaign_name}.claim.toml"

    # Generate template with 2 placeholder hypotheses
    template = f"""# Claim file for campaign: {campaign_name}
# Mode: {campaign_mode}

[claim]
headline = "TODO: State the primary claim being tested"
kill_condition = "TODO: Define the outcome that would falsify this claim"
regime = "TODO: Specify the parameter range or conditions under which claim applies"

[[hypotheses]]
id = "H_primary"
label = "Primary hypothesis (the effect you expect to observe)"
predicted_signature = "TODO: metric values when this hypothesis is true"

[[hypotheses]]
id = "H_null"
label = "Null hypothesis (no effect)"
predicted_signature = "TODO: metric values under null"

[[assumptions]]
id = "A1"
label = "TODO: Key assumption that could invalidate the result"

[[confounds]]
id = "C1"
label = "TODO: Known confound and how you control for it"

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "main"
predicted_outcome = "discriminates"

[claim.union_gate]
[[claim.union_gate.clauses]]
id = "C_main"
description = "Primary hypothesis distinguishable from null"
hypothesis_ids = ["H_primary", "H_null"]
"""

    try:
        with open(claim_path, "w") as f:
            f.write(template)
    except Exception as e:
        raise RuntimeError(f"Failed to write claim template: {e}") from e

    # Print sidecar snippets for the researcher to copy
    sidecar_snippet = f"""# Add these lines to your sidecar files for runs testing this campaign:

[run]
# discriminates: list of hypothesis IDs this run distinguishes
# isolates: list of hypothesis IDs this run isolates as primary causal factor
discriminates = ["H_primary", "H_null"]
isolates = []
"""
    print(sidecar_snippet)

    return claim_path


def register_claim(path: Path, campaign_id: str, db, workspace_root: Path, force: bool = False) -> None:
    """Register a claim file with a campaign (stores path and SHA256).

    Args:
        path: Path to claim file (relative to workspace_root)
        campaign_id: UUID of the campaign
        db: DuckDB connection
        workspace_root: Root of the workspace
        force: If True, allow re-registration with audit event

    Raises:
        ValueError: If path is absolute, does not exist, or escapes workspace
        RuntimeError: If campaign not found or file already registered (without --force)
    """
    # AC-02: Reject absolute paths
    if path.is_absolute():
        raise ValueError("claim file must be stored relative to workspace root (not absolute path)")

    # Resolve relative path
    abs_path = workspace_root / path

    # Check that file exists
    if not abs_path.exists():
        raise FileNotFoundError(f"Claim file not found: {abs_path}")

    # Check that path doesn't escape workspace
    try:
        abs_path.resolve().relative_to(workspace_root.resolve())
    except ValueError:
        raise ValueError(f"Claim file escapes workspace root: {path}")

    # Compute SHA256
    sha256_hash = hashlib.sha256(abs_path.read_bytes()).hexdigest()

    # Check if already registered
    try:
        existing = db.execute(
            "SELECT claim_sha256 FROM campaigns WHERE id=?", [campaign_id]
        ).fetchone()
    except Exception as e:
        raise RuntimeError(f"Failed to query campaign: {e}") from e

    if existing and existing[0] is not None:
        if not force:
            raise RuntimeError(
                f"Campaign already has a registered claim. "
                f"Re-register with `bth claim register --force` to update."
            )
        # Write audit event for forced re-registration
        try:
            db.execute(
                "INSERT INTO amendments (run_id, amended_at, reason) VALUES (?, ?, ?)",
                [campaign_id, str(Path.cwd()), "claim re-registration (forced)"]
            )
        except Exception:
            pass

    # Store relative path and SHA256 in campaigns table
    try:
        db.execute(
            "UPDATE campaigns SET claim_path=?, claim_sha256=? WHERE id=?",
            [str(path), sha256_hash, campaign_id]
        )
        db.commit()
    except Exception as e:
        raise RuntimeError(f"Failed to register claim: {e}") from e


def check_sha(path_relative: str, registered_sha: str, workspace_root: Path) -> None:
    """Verify that a claim file's SHA256 matches the registered value.

    Raises:
        FileNotFoundError: If file does not exist
        ValueError: If SHA256 does not match
    """
    abs_path = workspace_root / path_relative

    if not abs_path.exists():
        raise FileNotFoundError(f"Claim file not found at {abs_path}")

    current_sha = hashlib.sha256(abs_path.read_bytes()).hexdigest()

    if current_sha != registered_sha:
        raise ValueError(
            "claim.bth.toml has been modified since registration — "
            "re-register with `bth claim register --force` to acknowledge the amendment."
        )


def run_union_gate(db, campaign_id: str, claim: ClaimFile) -> tuple[str, list[str]]:
    """Check if all union gate clauses are covered by runs in the campaign.

    Args:
        db: DuckDB connection
        campaign_id: UUID of the campaign
        claim: Parsed ClaimFile

    Returns:
        Tuple of (verdict, uncovered_clause_ids) where:
        - verdict is "covered" or "confounded"
        - uncovered_clause_ids is list of clause IDs with no covering run
    """
    uncovered = []

    for clause in claim.union_gate_clauses:
        clause_id = clause.get("id", "")
        hypothesis_ids = clause.get("hypothesis_ids", [])

        if not hypothesis_ids:
            continue

        # Build query: find run where claim_discriminates contains all hypothesis_ids
        # claim_discriminates is a JSON array string like '["H1","H2"]'
        try:
            # Query for a run that has all hypothesis IDs in its discriminates field
            conditions = []
            for h_id in hypothesis_ids:
                conditions.append(f"json_contains(claim_discriminates, '{json.dumps(h_id)}')")

            where_clause = " AND ".join(conditions) if conditions else "1=1"

            matching_runs = db.execute(
                f"SELECT COUNT(*) FROM runs WHERE campaign_id=? AND {where_clause}",
                [campaign_id]
            ).fetchone()

            if matching_runs and matching_runs[0] > 0:
                # Clause is covered
                pass
            else:
                uncovered.append(clause_id)
        except Exception:
            # Query failed; mark clause as uncovered
            uncovered.append(clause_id)

    verdict = "confounded" if uncovered else "covered"
    return verdict, uncovered
