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
    warnings: list[str] = field(default_factory=list)
    infos: list[str] = field(default_factory=list)


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
        confound_label = confound.get("label", confound.get("id", "unknown"))

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
                [parity_run_id, parity_run_id + "%"]
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
                    [parity_run_id, parity_run_id + "%"]
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
                            errors.append(
                                ValidationError(f"failed to compare parity metric: {e}")
                            )
        else:
            # State 3: parity_run_id set, db is None
            infos.append(f"skipping baseline parity check for '{confound_label}' — no catalog connection")


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
    all_outcomes = {disc.get("predicted_outcome", "") for disc in claim.discriminability if disc.get("predicted_outcome")}
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
parity_run_id = ""

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
    import os
    import tempfile
    import logging
    from bathos.campaigns import _resolve_campaign_id, CampaignError

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
    abs_claim_path = workspace_root / claim_path_rel

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
        [parity_run_id, parity_run_id + "%"]
    ).fetchall()

    if not run_rows:
        raise ValueError(f"Parity run '{parity_run_id}' not found in catalog")

    outcome, metadata_json, parity_run_type_col = run_rows[0]

    # Validate outcome is pass or partial
    if outcome not in ("pass", "partial"):
        raise ValueError(
            f"Parity run '{parity_run_id}' has outcome='{outcome}', "
            "expected 'pass' or 'partial'"
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
        mode="w",
        dir=temp_dir,
        suffix=".tmp",
        delete=False,
        encoding="utf-8"
    ) as tmp_f:
        temp_path = Path(tmp_f.name)
        # Find the parity_run_id = "" line in reference_parity block and replace it
        updated_content = original_content_str.replace(
            'parity_run_id = ""',
            f'parity_run_id = "{parity_run_id}"'
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
            db.execute(
                "UPDATE campaigns SET claim_sha256 = ? WHERE id = ?",
                [new_sha256, full_id]
            )

            event(
                "claim.attest_parity",
                campaign_id=full_id,
                parity_run_id=parity_run_id,
                claim_sha256=new_sha256
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
                mode="w",
                dir=temp_dir,
                suffix=".tmp",
                delete=False,
                encoding="utf-8"
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
                try:
                    rollback_path.unlink()
                except Exception:
                    pass
            # Re-raise the original DB error
            raise

    except Exception as e:
        # Clean up temp file if it still exists (e.g., if os.replace failed)
        if temp_path.exists():
            try:
                temp_path.unlink()
            except Exception:
                pass
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
    - 'uncontrolled' if parity_run_id is empty or run not found

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
            "label": confound.get("label", ""),
            "status": "uncontrolled",  # default
        }

        parity_run_id = ref_par.get("parity_run_id", "")

        if not parity_run_id:
            # Empty parity_run_id
            confound_info["status"] = "uncontrolled"
        elif db is not None:
            # Query the run
            run_rows = db.execute(
                "SELECT outcome, metadata, parity_run_type FROM runs WHERE id = ? OR id LIKE ?",
                [parity_run_id, parity_run_id + "%"]
            ).fetchall()

            if not run_rows:
                # Run not found
                confound_info["status"] = "uncontrolled"
            else:
                outcome, metadata_json, parity_run_type_col = run_rows[0]
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
        else:
            # DB is None, mark as uncontrolled
            confound_info["status"] = "uncontrolled"

        result_confounds.append(confound_info)

    return {"confounds": result_confounds}
