"""Contract tests for reusable automation artifacts."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError as JSONValidationError
from pydantic import ValidationError as PydanticValidationError

from src.artifact.schema import ARTIFACT_JSON_SCHEMA, AutomationArtifact, Locator


@pytest.fixture
def artifact_data() -> dict[str, Any]:
    """Build a complete, nested artifact payload.

    Returns:
        Serializable artifact fields.
    """
    return {
        "id": "c4215213-4d33-4190-b84c-b6e6fd88148d",
        "name": "Read balance",
        "version": "1.2.3-rc.1+build.7",
        "description": "Read the displayed account balance",
        "created_at": "2026-09-24T00:00:00Z",
        "updated_at": "2026-09-24T01:00:00+00:00",
        "created_by": "discovery-agent",
        "target_url": "https://banking.example.com/app",
        "inputs": [{
            "name": "account", "type": "string", "description": "Account name",
            "required": True, "example": "checking",
        }],
        "outputs": [{
            "name": "balance", "type": "number", "description": "Displayed balance",
            "extraction_locator": {"strategy": "id", "value": "balance", "fallbacks": None,
                                   "robustness_notes": "Stable test ID"},
        }],
        "steps": [{
            "step_number": 1, "action": "read_text", "locator": {
                "strategy": "css", "value": "[data-test=balance]",
                "fallbacks": [{"strategy": "id", "value": "balance", "fallbacks": None,
                               "robustness_notes": "Stable ID"}],
                "robustness_notes": "Stable data attribute",
            },
            "value": None, "reasoning": "Read the balance", "expected_outcome": "Balance captured",
            "timeout_ms": 10000,
        }],
        "success_checkpoint": {
            "condition": "text_contains", "locator": None,
            "expected_value": "Balance", "error_message": "Balance was not shown",
        },
        "known_errors": {"session_expired": {"retryable": False, "message": "Sign in again"}},
        "discovery_run_id": "run-42",
        "success_rate": 0.9,
    }


def test_schema_round_trip_and_nested_fallbacks(artifact_data: dict[str, Any]) -> None:
    """Nested dataclasses survive JSON-shaped serialization."""
    Draft202012Validator.check_schema(ARTIFACT_JSON_SCHEMA)
    artifact = AutomationArtifact.from_dict(artifact_data)
    assert isinstance(artifact.steps[0].locator.fallbacks[0], Locator)
    assert artifact.to_dict() == artifact_data
    assert artifact.validate() is True
    assert AutomationArtifact.from_dict(artifact.to_dict()).to_dict() == artifact_data


@pytest.mark.parametrize("field,bad", [
    ("version", "01.2.3"),
    ("created_at", "2026-09-24T00:00:00"),
    ("target_url", "javascript:alert(1)"),
    ("success_rate", 1.1),
])
def test_invalid_artifact_field_is_rejected(artifact_data: dict[str, Any], field: str, bad: Any) -> None:
    """Version, time, URL, and rate constraints reject bad values.

    Args:
        artifact_data: Valid artifact fixture.
        field: Field to replace.
        bad: Invalid replacement value.
    """
    payload = deepcopy(artifact_data)
    payload[field] = bad
    with pytest.raises((JSONValidationError, PydanticValidationError)):
        AutomationArtifact.from_dict(payload)


def test_schema_rejects_unknown_fields_and_strategies(artifact_data: dict[str, Any]) -> None:
    """JSON Schema rejects extra keys and unsupported locator strategies."""
    payload = deepcopy(artifact_data)
    payload["unexpected"] = "data"
    with pytest.raises(JSONValidationError):
        AutomationArtifact.from_dict(payload)
    payload = deepcopy(artifact_data)
    payload["steps"][0]["locator"]["strategy"] = "image"
    with pytest.raises(JSONValidationError):
        AutomationArtifact.from_dict(payload)


def test_action_and_checkpoint_require_operands(artifact_data: dict[str, Any]) -> None:
    """Replay actions and checkpoints require their operands."""
    payload = deepcopy(artifact_data)
    payload["steps"][0]["locator"] = None
    with pytest.raises(PydanticValidationError):
        AutomationArtifact.from_dict(payload)
    payload = deepcopy(artifact_data)
    payload["success_checkpoint"]["expected_value"] = None
    with pytest.raises(PydanticValidationError):
        AutomationArtifact.from_dict(payload)


def test_step_numbers_and_dates_are_ordered(artifact_data: dict[str, Any]) -> None:
    """Step numbering and timestamps follow a deterministic order."""
    payload = deepcopy(artifact_data)
    payload["steps"][0]["step_number"] = 2
    with pytest.raises(PydanticValidationError):
        AutomationArtifact.from_dict(payload)
    payload = deepcopy(artifact_data)
    payload["updated_at"] = "2026-09-23T23:00:00Z"
    with pytest.raises(PydanticValidationError):
        AutomationArtifact.from_dict(payload)


def test_revalidation_detects_post_construction_corruption(artifact_data: dict[str, Any]) -> None:
    """The explicit validate method checks an altered artifact again."""
    artifact = AutomationArtifact.from_dict(artifact_data)
    object.__setattr__(artifact, "success_rate", 2.0)
    assert artifact.validate() is False
