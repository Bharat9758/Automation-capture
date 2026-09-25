"""Escalation request creation, redaction, validation, and persistence tests."""

from __future__ import annotations

import base64
import json
import os
import uuid
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.escalation.escalation_request import (
    EscalationRequest,
    create_escalation_request,
    escalation_to_json,
    format_previous_steps,
    get_available_elements,
    json_to_escalation,
    list_escalation_requests,
    load_escalation_request,
    redact_sensitive_params,
    save_escalation_request,
    validate_escalation_request,
)
from src.escalation.stuck_detector import StuckState
from src.replay.locator_strategy import LocatorResolver


@pytest.fixture
def stuck() -> StuckState:
    """Create a captured pause that contains an input in metadata.

    Returns:
        Hydrated stuck state.
    """
    return StuckState(is_stuck=True, reason="No result for member 12345", current_step=4,
                      current_screenshot=base64.b64encode(b"image-data"),
                      current_dom="<html><input value='12345'></html>",
                      recommended_action="Review member 12345",
                      escalation_context={"current_url": "/member/12345", "last_steps": [{"step_number": 3}],
                                          "dom": "<html><input value='12345'></html>"})


@pytest.fixture
def escalation_record(stuck: StuckState) -> EscalationRequest:
    """Build one request using raw inputs that must be masked.

    Args:
        stuck: Captured browser state.

    Returns:
        Valid request.
    """
    return create_escalation_request(
        "lookup-member", "run-123", stuck, "session-abc", "Find member 12345 and balance 500",
        {"action": "click", "locator": {"strategy": "css", "value": "#member-12345"}, "value": "12345", "amount": 500},
        [{"step_number": 1, "action": "type", "value": "12345", "success": True}],
        {"member_id": "12345", "amount": 500, "custom_secret": "hidden"},
    )


def test_creation_masks_values_and_keeps_evidence(escalation_record: EscalationRequest) -> None:
    """A human sees a useful description while input values remain masked."""
    assert uuid.UUID(escalation_record.escalation_id).version == 4
    assert datetime.fromisoformat(escalation_record.timestamp.replace("Z", "+00:00")).tzinfo is not None
    assert escalation_record.input_params == {"member_id": "***MEMBER***", "amount": "***AMOUNT***",
                                    "custom_secret": "***REDACTED***"}
    assert "12345" not in escalation_record.goal + escalation_record.context_notes + str(escalation_record.last_action) + str(escalation_record.previous_steps)
    assert "500" not in escalation_record.goal
    assert escalation_record.last_action["amount"] == "***AMOUNT***"
    assert "12345" not in str(escalation_record.stuck_state.escalation_context["current_url"])
    assert escalation_record.screenshot == base64.b64encode(b"image-data").decode("ascii")
    assert "12345" in escalation_record.dom_snapshot  # Raw DOM is required browser evidence.
    assert escalation_record.session_id == "session-abc" and escalation_record.current_step == 4
    assert validate_escalation_request(escalation_record) == (True, None)


def test_redaction_masks_all_keys_and_preserves_input(escalation_record: EscalationRequest) -> None:
    """Unknown names are still masked and caller data is unchanged."""
    original = {"member_id": "123", "account_id": "abc", "amount": 1, "ssn": "x", "email": "x@y.z", "token": "secret"}
    masked = redact_sensitive_params(original)
    assert list(masked) == list(original)
    assert list(masked.values()) == ["***MEMBER***", "***ACCOUNT***", "***AMOUNT***", "***SSN***",
                                     "***EMAIL***", "***REDACTED***"]
    assert original["token"] == "secret"
    assert escalation_record.input_params["custom_secret"] == "***REDACTED***"
    with pytest.raises(TypeError):
        redact_sensitive_params({1: "value"})  # type: ignore[arg-type]


def test_json_round_trip_and_invalid_documents(escalation_record: EscalationRequest) -> None:
    """JSON restores the nested frozen state and rejects missing evidence."""
    text = escalation_to_json(escalation_record)
    assert text.startswith("{\n  \"escalation_id\"")
    assert json_to_escalation(text) == escalation_record
    with pytest.raises(ValueError, match="Invalid escalation JSON"):
        json_to_escalation("{")
    data = json.loads(text)
    del data["artifact_id"]
    with pytest.raises(ValueError, match="artifact_id"):
        json_to_escalation(json.dumps(data))
    data = json.loads(text)
    data["screenshot"] = "not base64!"
    with pytest.raises(ValueError, match="base64"):
        json_to_escalation(json.dumps(data))
    data = json.loads(text)
    data["input_params"]["member_id"] = "12345"
    with pytest.raises(ValueError, match="redacted"):
        json_to_escalation(json.dumps(data))


def test_validation_rejects_missing_data(escalation_record: EscalationRequest) -> None:
    """The operator handoff requires identifiers, timestamps, and evidence."""
    for changed, expected in [
        ({"timestamp": "yesterday"}, "timestamp"), ({"dom_snapshot": ""}, "dom_snapshot"),
        ({"screenshot": ""}, "screenshot"), ({"session_id": ""}, "session_id"),
        ({"current_step": -1}, "current_step"), ({"escalation_id": "not-uuid"}, "escalation_id"),
        ({"stuck_state": replace(escalation_record.stuck_state, is_stuck=False)}, "stuck_state"),
    ]:
        valid, error = validate_escalation_request(replace(escalation_record, **changed))
        assert valid is False and expected in (error or "")


def test_atomic_save_load_and_sort(tmp_path: Path, escalation_record: EscalationRequest) -> None:
    """Private JSON files round-trip and list newest first."""
    directory = tmp_path / "evidence" / "escalations"
    one = directory / f"{escalation_record.escalation_id}.json"
    assert list_escalation_requests(str(directory)) == []
    assert save_escalation_request(escalation_record, str(one)) == str(one)
    assert load_escalation_request(str(one)) == escalation_record
    if os.name == "posix":
        assert one.stat().st_mode & 0o777 == 0o600
    later = replace(escalation_record, escalation_id=str(uuid.uuid4()), timestamp="2026-09-25T12:00:00Z")
    save_escalation_request(later, str(directory / f"{later.escalation_id}.json"))
    assert list_escalation_requests(str(directory))[0].escalation_id == later.escalation_id
    with pytest.raises(FileNotFoundError, match="not found"):
        load_escalation_request(str(directory / "missing.json"))
    (directory / "broken.json").write_text("bad JSON", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid escalation JSON"):
        list_escalation_requests(str(directory))


def test_format_steps_and_dom_suggestions() -> None:
    """Only control labels appear in suggested actions, never input values."""
    assert format_previous_steps([{"step_number": 1, "action": "navigate"},
                                  {"step_number": 2, "action": "click", "stuck": True}]) == (
                                      "Step 1: navigate\nStep 2: click [STUCK]")
    html = ("<main><button>Search</button><a href='/next'>Next</a>"
            "<div hidden><button>Hidden</button></div>"
            "<input type='text' name='member_id' value='12345' placeholder='Member ID'>"
            "<input type='hidden' value='secret'></main>")
    results = get_available_elements(html, LocatorResolver())
    assert results == ["button: Search", "a: Next", "input: Member ID"]
    assert "12345" not in str(results) and "secret" not in str(results)
    with pytest.raises(TypeError):
        get_available_elements(html, Mock())


def test_creation_requires_complete_browser_evidence(stuck: StuckState) -> None:
    """A missing screenshot fails rather than emitting an incomplete handoff."""
    with pytest.raises(ValueError, match="screenshot"):
        create_escalation_request("a", "d", replace(stuck, current_screenshot=b""), "session",
                                  "Find member", {"action": "click"}, [], {})
