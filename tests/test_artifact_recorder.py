"""Behavioral tests for artifact creation from agent traces."""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

import pytest

from src.agent import llm_agent
from src.artifact.recorder import (
    add_robustness_notes,
    classify_observed_value,
    infer_input_parameters,
    infer_output_fields,
    record_discovery_run,
    validate_artifact,
)
from src.artifact.schema import AutomationArtifact
from tests.test_agent import FakeClient, FakeDriver


@pytest.fixture
def discovery_steps() -> list[dict[str, Any]]:
    """Return a trace containing a failed attempt and two successful actions.

    Returns:
        Structured discovery steps.
    """
    return [
        {"number": 1, "action": "type", "selector": "#member_id", "locator_type": "css",
         "value": "private-123", "result": {"success": True, "length": 11}},
        {"number": 2, "action": "click", "selector": "#missing", "locator_type": "css",
         "result": {"success": False, "error": "TimeoutException"}},
        {"number": 3, "action": "read_text", "selector": "#balance", "locator_type": "css",
         "result": {"success": True, "observed_type": "number", "length": 7}},
    ]


@pytest.fixture
def discovery_logs() -> list[dict[str, Any]]:
    """Return completed run events.

    Returns:
        Structured discovery logs.
    """
    return [
        {"event": "discovery_started", "run_id": "run-123"},
        {"event": "loop_finished", "success": True, "final_url": "https://banking.example.com/account"},
    ]


def test_recorder_converts_successful_run_and_omits_literals(
    discovery_steps: list[dict[str, Any]], discovery_logs: list[dict[str, Any]]
) -> None:
    """Artifacts replay successful steps with placeholders and observed outputs.

    Args:
        discovery_steps: Successful mock trace.
        discovery_logs: Completed mock run logs.
    """
    artifact = record_discovery_run(discovery_steps, discovery_logs, "Read balance", "https://banking.example.com/")
    assert isinstance(artifact, AutomationArtifact)
    assert artifact.discovery_run_id == "run-123"
    assert artifact.version == "1.0.0"
    assert [step.step_number for step in artifact.steps] == [1, 2]
    assert artifact.steps[0].value == "{member_id}"
    assert artifact.inputs[0].name == "member_id"
    assert artifact.outputs[0].name == "balance"
    assert artifact.outputs[0].type == "number"
    assert artifact.success_checkpoint.condition == "element_visible"
    assert artifact.known_errors["attempt_2"]["error_type"] == "TimeoutException"
    assert artifact.steps[0].locator.fallbacks[0].strategy == "id"
    assert "private-123" not in json.dumps(artifact.to_dict())
    assert validate_artifact(artifact) == (True, "")
    assert AutomationArtifact.from_dict(artifact.to_dict()).validate()


def test_input_inference_from_placeholders_and_field_names() -> None:
    """Early typing steps produce unique typed inputs without storing values."""
    steps = [
        {"action": "type", "selector": "#member_id", "value": "{member_id}", "result": {"success": True}},
        {"action": "type", "selector": "#appointment_date", "result": {"success": True}},
        {"action": "type", "selector": "#member_id", "result": {"success": True}},
    ]
    inferred = infer_input_parameters(steps)
    assert [(item.name, item.type) for item in inferred] == [("member_id", "string"), ("appointment_date", "date")]
    assert all(item.example == "{" + item.name + "}" for item in inferred)


def test_output_field_inference_uses_values_without_persisting_them() -> None:
    """Numeric, list, and object observations map to output schema types."""
    steps = [{"number": 1, "action": "read_text", "selector": "#balance", "locator_type": "css",
              "result": {"success": True, "text": "$1,234.50"}}]
    outputs = infer_output_fields("Read balance", steps, [])
    assert outputs[0].type == "number"
    assert "$1,234.50" not in json.dumps(asdict(outputs[0]))
    assert classify_observed_value('["a", "b"]') == "list"
    assert classify_observed_value('{"balance": 10}') == "object"


def test_invalid_or_incomplete_trace_is_rejected(
    discovery_steps: list[dict[str, Any]], discovery_logs: list[dict[str, Any]]
) -> None:
    """A failed run or an output goal without extraction cannot be recorded.

    Args:
        discovery_steps: Successful mock trace.
        discovery_logs: Completed mock run logs.
    """
    with pytest.raises(ValueError, match="Only successful"):
        record_discovery_run(discovery_steps, [{"event": "loop_finished", "success": False}], "Read balance", "https://banking.example.com/")
    with pytest.raises(ValueError, match="no successful read_text"):
        record_discovery_run(discovery_steps[:1], discovery_logs, "Read balance", "https://banking.example.com/")
    with pytest.raises(ValueError, match="no replayable actions"):
        record_discovery_run([], discovery_logs, "Submit form", "https://banking.example.com/")


def test_action_only_run_uses_observed_destination(discovery_logs: list[dict[str, Any]]) -> None:
    """An action-only goal records its final URL as the fallback checkpoint.

    Args:
        discovery_logs: Completed mock run logs with a destination URL.
    """
    steps = [{"action": "click", "selector": "#submit", "result": {"success": True}}]
    artifact = record_discovery_run(steps, discovery_logs, "Submit form", "https://banking.example.com/")
    assert artifact.outputs == []
    assert artifact.success_checkpoint.condition == "url_matches"
    assert artifact.success_checkpoint.expected_value == r"^https://banking\.example\.com/account$"
    assert validate_artifact(artifact) == (True, "")


def test_validation_rejects_unbound_type_parameter(
    discovery_steps: list[dict[str, Any]], discovery_logs: list[dict[str, Any]]
) -> None:
    """Typed placeholders must reference a declared input.

    Args:
        discovery_steps: Successful mock trace.
        discovery_logs: Completed mock run logs.
    """
    artifact = record_discovery_run(discovery_steps, discovery_logs, "Read balance", "https://banking.example.com/")
    artifact.steps[0].value = "{missing_parameter}"
    valid, error = validate_artifact(artifact)
    assert valid is False
    assert "declared input" in error


def test_robustness_update_is_in_place_and_idempotent(
    discovery_steps: list[dict[str, Any]], discovery_logs: list[dict[str, Any]]
) -> None:
    """Equivalent ID fallbacks are added once and reused.

    Args:
        discovery_steps: Successful mock trace.
        discovery_logs: Completed mock run logs.
    """
    artifact = record_discovery_run(discovery_steps, discovery_logs, "Read balance", "https://banking.example.com/")
    add_robustness_notes(artifact)
    assert len(artifact.steps[0].locator.fallbacks) == 1
    assert validate_artifact(artifact)[0] is True


def test_loop_records_artifact_after_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful agent run returns a serializable artifact without typed text.

    Args:
        monkeypatch: Environment and SDK patcher.
    """
    monkeypatch.setenv("ALLOWED_DOMAINS", "localhost:5000")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    client = FakeClient([
        {"action": "type", "selector": "#member_id", "value": "secret-member-123"},
        {"action": "read_text", "selector": "#balance"},
        {"action": "done", "goal_met": True},
    ])
    monkeypatch.setattr(llm_agent, "Anthropic", lambda **kwargs: client)
    result = llm_agent.run_goal_driven_loop(FakeDriver(), "Read balance", "http://localhost:5000/", api_key="test-key")  # type: ignore[arg-type]
    assert result["success"] is True
    assert result["artifact_error"] is None
    artifact = AutomationArtifact.from_dict(result["artifact"])
    assert artifact.steps[0].value == "{member_id}"
    assert artifact.discovery_run_id == result["logs"][0]["run_id"]
    assert "secret-member-123" not in json.dumps(result["artifact"])


def test_zero_step_success_reports_recording_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent success remains visible when no replayable artifact can be built.

    Args:
        monkeypatch: Environment and SDK patcher.
    """
    monkeypatch.setenv("ALLOWED_DOMAINS", "localhost:5000")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    client = FakeClient([{"action": "done", "goal_met": True}])
    monkeypatch.setattr(llm_agent, "Anthropic", lambda **kwargs: client)
    result = llm_agent.run_goal_driven_loop(FakeDriver(), "See ready", "http://localhost:5000/", api_key="test-key")  # type: ignore[arg-type]
    assert result["success"] is True
    assert result["artifact"] is None
    assert "Artifact recording failed" in result["artifact_error"]
