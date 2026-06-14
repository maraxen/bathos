from __future__ import annotations
from enum import Enum
from bathos.prereg import GateErrorCode, _RESOLUTION_HINTS as _GATE_HINTS


class BathosErrorCode(str, Enum):
    # 11 aliased from GateErrorCode (same .value strings -- wire-compatible)
    SIDECAR_MISSING = GateErrorCode.SIDECAR_MISSING.value
    SIDECAR_INVALID = GateErrorCode.SIDECAR_INVALID.value
    SIDECAR_HASH_MISMATCH = GateErrorCode.SIDECAR_HASH_MISMATCH.value
    NOT_FIRST_OF_KIND = GateErrorCode.NOT_FIRST_OF_KIND.value
    MANIFEST_WRITE_FAILED = GateErrorCode.MANIFEST_WRITE_FAILED.value
    ADVERSARIAL_CHECK_MISSING = GateErrorCode.ADVERSARIAL_CHECK_MISSING.value
    HYPOTHESIS_LOCK_MISSING = GateErrorCode.HYPOTHESIS_LOCK_MISSING.value
    OUTCOME_EVALUATION_ERROR = GateErrorCode.OUTCOME_EVALUATION_ERROR.value
    RESULT_SCHEMA_MISMATCH = GateErrorCode.RESULT_SCHEMA_MISMATCH.value
    OUTCOME_AMBIGUOUS = GateErrorCode.OUTCOME_AMBIGUOUS.value
    INTERNAL = GateErrorCode.INTERNAL.value
    # 5 new codes for domain exceptions
    CATALOG_ERROR = "catalog_error"
    CAMPAIGN_ERROR = "campaign_error"
    SIDECAR_ERROR = "sidecar_error"
    EXPORT_ERROR = "export_error"
    INVALID_PARAM = "invalid_param"


RESOLUTION_HINTS: dict[BathosErrorCode, str] = {
    BathosErrorCode.SIDECAR_MISSING: _GATE_HINTS[GateErrorCode.SIDECAR_MISSING],
    BathosErrorCode.SIDECAR_INVALID: _GATE_HINTS[GateErrorCode.SIDECAR_INVALID],
    BathosErrorCode.SIDECAR_HASH_MISMATCH: _GATE_HINTS[GateErrorCode.SIDECAR_HASH_MISMATCH],
    BathosErrorCode.NOT_FIRST_OF_KIND: _GATE_HINTS[GateErrorCode.NOT_FIRST_OF_KIND],
    BathosErrorCode.MANIFEST_WRITE_FAILED: _GATE_HINTS[GateErrorCode.MANIFEST_WRITE_FAILED],
    BathosErrorCode.ADVERSARIAL_CHECK_MISSING: _GATE_HINTS[GateErrorCode.ADVERSARIAL_CHECK_MISSING],
    BathosErrorCode.HYPOTHESIS_LOCK_MISSING: _GATE_HINTS[GateErrorCode.HYPOTHESIS_LOCK_MISSING],
    BathosErrorCode.OUTCOME_EVALUATION_ERROR: _GATE_HINTS[GateErrorCode.OUTCOME_EVALUATION_ERROR],
    BathosErrorCode.RESULT_SCHEMA_MISMATCH: _GATE_HINTS[GateErrorCode.RESULT_SCHEMA_MISMATCH],
    BathosErrorCode.OUTCOME_AMBIGUOUS: _GATE_HINTS[GateErrorCode.OUTCOME_AMBIGUOUS],
    BathosErrorCode.INTERNAL: _GATE_HINTS[GateErrorCode.INTERNAL],
    BathosErrorCode.CATALOG_ERROR: "A catalog read/write operation failed. Check the DuckDB warm store and cool-tier Parquet files.",
    BathosErrorCode.CAMPAIGN_ERROR: "A campaign operation failed. Call bth_campaign_list to verify campaign state.",
    BathosErrorCode.SIDECAR_ERROR: "Sidecar parsing or validation failed. Check the .bth.toml file adjacent to the script.",
    BathosErrorCode.EXPORT_ERROR: "Export operation failed. Check disk space and output path permissions.",
    BathosErrorCode.INVALID_PARAM: "A required parameter was missing or invalid. Check the tool input schema.",
}

# Maps exception class name (str) -> BathosErrorCode for traced_tool dispatch and AC-7 AST test
EXCEPTION_TO_CODE: dict[str, BathosErrorCode] = {
    "GateError": BathosErrorCode.INTERNAL,
    "CatalogError": BathosErrorCode.CATALOG_ERROR,
    "CampaignError": BathosErrorCode.CAMPAIGN_ERROR,
    "SidecarError": BathosErrorCode.SIDECAR_ERROR,
    "ExportError": BathosErrorCode.EXPORT_ERROR,
    "CorruptDatabaseError": BathosErrorCode.CATALOG_ERROR,
    "CompactionLockedError": BathosErrorCode.CATALOG_ERROR,
    "PostmortemError": BathosErrorCode.INVALID_PARAM,
}
