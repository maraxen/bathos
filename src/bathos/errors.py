"""Structured error codes for bathos MCP tools.

This module defines BathosErrorCode as a flat enum that:
- Aliases all GateErrorCode members (11 codes from prereg.py)
- Adds 5 new domain exception codes (catalog, campaign, sidecar, export, invalid_param)
- Provides a centralized RESOLUTION_HINTS registry keyed by error code
- Supports traced_tool's exception-to-code mapping and telemetry emission

BathosErrorCode.SIDECAR_MISSING and GateErrorCode.SIDECAR_MISSING have the same
.value string ("sidecar_missing"), making them wire-compatible aliases.
"""

from enum import Enum


class BathosErrorCode(str, Enum):
    """Enumeration of structured error codes for all bathos MCP tool failures.

    Members are split into:
    - 11 aliased from GateErrorCode (pre-registration gate failures)
    - 5 new codes for domain exceptions (catalog, campaign, sidecar, export, param validation)
    - 1 catch-all internal error code

    Total: 16 codes, all strings for use in JSON envelopes and telemetry.
    """

    # Aliased from GateErrorCode (11 codes, same .value strings)
    SIDECAR_MISSING = "sidecar_missing"
    SIDECAR_INVALID = "sidecar_invalid"
    SIDECAR_HASH_MISMATCH = "sidecar_hash_mismatch"
    NOT_FIRST_OF_KIND = "not_first_of_kind"
    MANIFEST_WRITE_FAILED = "manifest_write_failed"
    ADVERSARIAL_CHECK_MISSING = "adversarial_check_missing"
    HYPOTHESIS_LOCK_MISSING = "hypothesis_lock_missing"
    OUTCOME_EVALUATION_ERROR = "outcome_evaluation_error"
    RESULT_SCHEMA_MISMATCH = "result_schema_mismatch"
    OUTCOME_AMBIGUOUS = "outcome_ambiguous"
    INTERNAL = "internal"

    # New domain exception codes (5 codes)
    CATALOG_ERROR = "catalog_error"
    CAMPAIGN_ERROR = "campaign_error"
    SIDECAR_ERROR = "sidecar_error"
    EXPORT_ERROR = "export_error"
    INVALID_PARAM = "invalid_param"


# Static registry of resolution hints keyed by BathosErrorCode.
# Every member of BathosErrorCode must have a non-empty entry.
# These hints are returned to agentic callers in the MCP envelope.
RESOLUTION_HINTS: dict[BathosErrorCode, str] = {
    # Aliased from GateErrorCode (same text as prereg._RESOLUTION_HINTS)
    BathosErrorCode.SIDECAR_MISSING: "Create a .bth.toml sidecar adjacent to the script",
    BathosErrorCode.SIDECAR_INVALID: "Fix the sidecar TOML syntax or missing required sections",
    BathosErrorCode.SIDECAR_HASH_MISMATCH: "Re-run 'bth hypothesis lock' to regenerate the manifest",
    BathosErrorCode.NOT_FIRST_OF_KIND: "Use --derived-from to link to the parent run",
    BathosErrorCode.MANIFEST_WRITE_FAILED: "Check write permissions in the script directory",
    BathosErrorCode.ADVERSARIAL_CHECK_MISSING: "Add adversarial_check to all outcomes.pass blocks in the sidecar",
    BathosErrorCode.HYPOTHESIS_LOCK_MISSING: "Run 'bth hypothesis lock <script>' before executing",
    BathosErrorCode.OUTCOME_EVALUATION_ERROR: "Fix the DuckDB SQL condition in the sidecar outcomes block",
    BathosErrorCode.RESULT_SCHEMA_MISMATCH: "Ensure script output JSON matches the result_schema in the sidecar",
    BathosErrorCode.OUTCOME_AMBIGUOUS: "Ensure exactly one outcome condition evaluates to true",
    BathosErrorCode.INTERNAL: "File a bug report with the full error message",
    # New domain exception codes
    BathosErrorCode.CATALOG_ERROR: "Check catalog state with 'bth verify' and repair with 'bth repair'",
    BathosErrorCode.CAMPAIGN_ERROR: "Verify campaign state and ensure all runs are persisted",
    BathosErrorCode.SIDECAR_ERROR: "Check sidecar file format and permissions",
    BathosErrorCode.EXPORT_ERROR: "Verify export destination and permissions",
    BathosErrorCode.INVALID_PARAM: "Check required parameters and types in the MCP tool call",
}


# Mapping of exception class names (from raise statements) to BathosErrorCode.
# Used by AC-7 CI test to verify all domain exceptions have registered codes.
# Builtin exceptions (ValueError, RuntimeError, etc.) are not subject to this registry —
# they implicitly map to INTERNAL at traced_tool dispatch time.
EXCEPTION_TO_CODE: dict[str, BathosErrorCode] = {
    "GateError": BathosErrorCode.INTERNAL,
    "CatalogError": BathosErrorCode.CATALOG_ERROR,
    "CampaignError": BathosErrorCode.CAMPAIGN_ERROR,
    "SidecarError": BathosErrorCode.SIDECAR_ERROR,
    "ExportError": BathosErrorCode.EXPORT_ERROR,
    "CorruptDatabaseError": BathosErrorCode.CATALOG_ERROR,
}
