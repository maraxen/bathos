"""Test GateErrorCode enum and GateErrorPayload dataclass."""
from __future__ import annotations

import dataclasses
import pytest

from bathos.prereg import GateErrorCode, GateErrorPayload, _gate_failure_payload, GateError


class TestGateErrorCode:
    """Test GateErrorCode enum values and structure."""

    def test_error_codes_are_defined(self):
        """Verify all expected error codes exist."""
        expected_codes = {
            "SIDECAR_MISSING",
            "SIDECAR_INVALID",
            "SIDECAR_HASH_MISMATCH",
            "NOT_FIRST_OF_KIND",
            "MANIFEST_WRITE_FAILED",
            "ADVERSARIAL_CHECK_MISSING",
            "HYPOTHESIS_LOCK_MISSING",
            "OUTCOME_EVALUATION_ERROR",
            "RESULT_SCHEMA_MISMATCH",
            "OUTCOME_AMBIGUOUS",
            "INTERNAL",
        }
        actual_codes = {code.name for code in GateErrorCode}
        assert expected_codes == actual_codes

    def test_error_code_values_are_snake_case(self):
        """Verify error code values use snake_case."""
        assert GateErrorCode.SIDECAR_MISSING.value == "sidecar_missing"
        assert GateErrorCode.ADVERSARIAL_CHECK_MISSING.value == "adversarial_check_missing"
        assert GateErrorCode.OUTCOME_EVALUATION_ERROR.value == "outcome_evaluation_error"

    def test_error_code_is_str_enum(self):
        """Verify GateErrorCode is a str-based Enum."""
        assert isinstance(GateErrorCode.SIDECAR_MISSING, str)
        assert GateErrorCode.SIDECAR_MISSING == "sidecar_missing"


class TestGateErrorPayload:
    """Test GateErrorPayload dataclass."""

    def test_payload_creation(self):
        """Verify basic payload creation."""
        payload = GateErrorPayload(
            error_code=GateErrorCode.SIDECAR_MISSING,
            phase="pre_execution",
            taxonomy_label="sidecar_missing",
            errors=["sidecar not found"],
            agent_mode="autonomous",
            resolution_hint="Create a .bth.toml file",
        )
        assert payload.error_code == GateErrorCode.SIDECAR_MISSING
        assert payload.phase == "pre_execution"
        assert payload.gate_schema_version == 2

    def test_payload_serializes_to_dict(self):
        """Verify payload can be converted to dict for JSON serialization."""
        payload = GateErrorPayload(
            error_code=GateErrorCode.ADVERSARIAL_CHECK_MISSING,
            phase="pre_execution",
            taxonomy_label="adversarial_check_missing",
            errors=["adversarial_check missing on outcomes.pass"],
            agent_mode="autonomous",
            resolution_hint="Add adversarial_check field",
        )
        d = dataclasses.asdict(payload)
        assert d["error_code"] == "adversarial_check_missing"
        assert d["gate_schema_version"] == 2


class TestGateFailurePayload:
    """Test _gate_failure_payload function."""

    def test_gate_failure_payload_basic(self):
        """Test basic payload generation."""
        payload = _gate_failure_payload(
            error_code=GateErrorCode.SIDECAR_MISSING,
            phase="pre_execution",
            errors=["sidecar not found"],
            agent_mode="autonomous",
        )
        assert payload.error_code == GateErrorCode.SIDECAR_MISSING
        assert payload.phase == "pre_execution"
        assert len(payload.errors) == 1
        assert payload.agent_mode == "autonomous"

    def test_gate_failure_payload_has_resolution_hint(self):
        """Test that resolution hints are populated."""
        payload = _gate_failure_payload(
            error_code=GateErrorCode.SIDECAR_MISSING,
            phase="pre_execution",
            errors=["sidecar not found"],
            agent_mode="autonomous",
        )
        assert "create a .bth.toml" in payload.resolution_hint.lower()

    def test_gate_failure_payload_adversarial_check_hint(self):
        """Test adversarial_check error code hint."""
        payload = _gate_failure_payload(
            error_code=GateErrorCode.ADVERSARIAL_CHECK_MISSING,
            phase="pre_execution",
            errors=["missing"],
            agent_mode="autonomous",
        )
        assert "adversarial_check" in payload.resolution_hint.lower()

    def test_gate_error_with_payload(self):
        """Test GateError can hold a payload."""
        payload = _gate_failure_payload(
            error_code=GateErrorCode.SIDECAR_MISSING,
            phase="pre_execution",
            errors=["sidecar not found"],
            agent_mode="autonomous",
        )
        error = GateError("test message", payload=payload)
        assert error.payload == payload
        assert error.payload.error_code == GateErrorCode.SIDECAR_MISSING

    def test_gate_error_without_payload(self):
        """Test GateError works with None payload."""
        error = GateError("test message")
        assert error.payload is None
