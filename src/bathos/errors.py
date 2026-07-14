"""Error codes and resolution hints for bathos MCP tools.

Provides a unified BathosErrorCode enum that includes:
- 11 aliased codes from GateErrorCode (pre-registration gate failures)
- 5 new codes for domain exceptions (catalog, campaign, sidecar, export, invalid params)

All MCP tools return envelopes with error_code values from this enum.
"""

from __future__ import annotations

from enum import Enum


class BathosErrorCode(str, Enum):
    """Unified error code enumeration for all bathos MCP tools.

    Aliases 11 codes from GateErrorCode, plus 5 new codes for domain exceptions.
    All codes are lowercase snake_case strings.
    """
    # Aliased from GateErrorCode (11 members)
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

    # New codes for domain exceptions (5 members)
    CATALOG_ERROR = "catalog_error"
    CAMPAIGN_ERROR = "campaign_error"
    SIDECAR_ERROR = "sidecar_error"
    EXPORT_ERROR = "export_error"
    INVALID_PARAM = "invalid_param"


# Resolution hints registry: every BathosErrorCode member must have a non-empty entry
RESOLUTION_HINTS: dict[BathosErrorCode, str] = {
    # Aliased from GateErrorCode (copied from prereg._RESOLUTION_HINTS)
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

    # New codes for domain exceptions
    BathosErrorCode.CATALOG_ERROR: "Check catalog integrity with 'bth verify'",
    BathosErrorCode.CAMPAIGN_ERROR: "Verify campaign state and re-run the campaign operation",
    BathosErrorCode.SIDECAR_ERROR: "Verify sidecar file exists and is valid TOML",
    BathosErrorCode.EXPORT_ERROR: "Check write permissions and available disk space",
    BathosErrorCode.INVALID_PARAM: "Verify all required parameters are provided with correct types",
}


# Exception class name to BathosErrorCode registry (AC-7 maintenance trap).
# Maps domain exception class names to their error codes for CI assertion.
EXCEPTION_TO_CODE: dict[str, BathosErrorCode] = {
    # Gate errors
    "GateError": BathosErrorCode.INTERNAL,
    # Catalog errors
    "CatalogError": BathosErrorCode.CATALOG_ERROR,
    "CorruptDatabaseError": BathosErrorCode.CATALOG_ERROR,
    # Campaign errors
    "CampaignError": BathosErrorCode.CAMPAIGN_ERROR,
    # Sidecar errors
    "SidecarError": BathosErrorCode.SIDECAR_ERROR,
    # Export errors
    "ExportError": BathosErrorCode.EXPORT_ERROR,
    # Figure registry errors (S7, item 3490): a figure_entry payload carried a
    # forbidden inline field (verdict/strength/content_hash/outcome/gate).
    "FigureEntrySchemaError": BathosErrorCode.INVALID_PARAM,
}
