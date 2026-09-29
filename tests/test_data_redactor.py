"""Privacy policy and raw evidence separation contracts."""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import uuid
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from selenium.webdriver.remote.webdriver import WebDriver

from src.artifact.schema import ActionStep, Locator
from src.escalation.escalation_request import create_escalation_request, validate_escalation_request
from src.escalation.session_manager import SessionLifecycleManager, load_session_metadata, save_session_metadata
from src.escalation.stuck_detector import StuckState
from src.logging import get_logger
from src.safety.data_redactor import (
    EvidencePreservation, MASK, RedactionLevel, RedactionPolicy,
    apply_pattern_redaction, is_sensitive_field, is_test_data, load_redaction_policy,
    redact_action_step, redact_dict, redact_escalation_request, redact_session_metadata,
    redact_string, save_raw_evidence, should_redact_in_context,
)


@pytest.fixture
def escalation_record() -> object:
    """Create a complete, already sanitized handoff with raw browser evidence.

    Returns:
        Valid Phase 11 escalation request.
    """
    state = StuckState(is_stuck=True, reason="Search failed", current_step=2,
                       current_screenshot=base64.b64encode(b"raw-png"),
                       current_dom="<input value='12345'>", recommended_action="Review page",
                       escalation_context={"screenshot": base64.b64encode(b"raw-png").decode(),
                                           "dom": "<input value='12345'>", "note": "Email alice@example.com"})
    return create_escalation_request("lookup", "run-1", state, "browser-1", "Look up member",
                                     {"action": "click", "value": "alice@example.com"}, [], {"member_id": "12345"})


def test_sensitive_fields_and_patterns() -> None:
    """Recognize field aliases and mask SSN, card, email, phone, money, and dates."""
    policy = RedactionPolicy()
    assert is_sensitive_field("memberId") and is_sensitive_field("credit-card")
    assert not is_sensitive_field("step_number")
    text = "123-45-6789 4111-1111-1111-1111 alice@example.com 314-555-1234 $5,000.00 1990-04-20"
    redacted = redact_string(text, policy)
    for private in ("123-45-6789", "4111-1111-1111-1111", "alice@example.com", "314-555-1234",
                    "$5,000.00", "1990-04-20"):
        assert private not in redacted
    assert redacted.count(MASK) == 6
    assert "$" + MASK in redacted
    assert apply_pattern_redaction("Reach alice@example.com", policy) == f"Reach {MASK}"


def test_levels_test_data_and_context() -> None:
    """Each level masks more; explicit fixture labels never exempt strict fields."""
    raw = {"ssn": "123-45-6789", "member_id": "member_123", "name": "Alice", "amount": 50,
           "session_id": str(uuid.uuid4()), "timestamp": "2026-09-29T12:00:00Z", "step_number": 2}
    none = redact_dict(raw, RedactionPolicy(redaction_level=RedactionLevel.NONE))
    basic = redact_dict(raw, RedactionPolicy(redaction_level=RedactionLevel.BASIC))
    strict = redact_dict(raw, RedactionPolicy(redaction_level=RedactionLevel.STRICT))
    paranoid = redact_dict(raw, RedactionPolicy(redaction_level=RedactionLevel.PARANOID))
    assert none == raw and none is not raw
    assert basic["ssn"] == basic["amount"] == MASK and basic["member_id"] == "member_123"
    assert strict["member_id"] == strict["name"] == MASK
    assert paranoid["session_id"] == raw["session_id"] and paranoid["timestamp"] == raw["timestamp"]
    assert paranoid["step_number"] == 2 and paranoid["member_id"] == MASK
    assert redact_dict({"reference": 12345}, RedactionPolicy())["reference"] == MASK
    assert is_test_data("test_account") and is_test_data("demo_user") and not is_test_data("12345")
    assert should_redact_in_context("member_id", "member_123", RedactionPolicy())


def test_nested_dict_keeps_shape_and_masks_evidence_when_requested() -> None:
    """Nested private values disappear while private evidence can be retained."""
    payload = {"input_params": {"member_id": {"raw": "12345"}, "email": "alice@example.com"},
               "previous_steps": [{"step_number": 1, "note": "SSN 123-45-6789"}],
               "locator": {"strategy": "id", "value": "unknown-selector-12345",
                           "fallbacks": [{"strategy": "css", "value": "unknown-fallback-12345"}]},
               "screenshot": "raw-base64", "dom_snapshot": "<input value='12345'>"}
    original = json.dumps(payload)
    result = redact_dict(payload, RedactionPolicy())
    assert result["input_params"]["member_id"] == {"raw": MASK}
    assert result["input_params"]["email"] == MASK
    assert "123-45-6789" not in str(result["previous_steps"])
    assert result["locator"] == {"strategy": "id", "value": MASK,
                                 "fallbacks": [{"strategy": "css", "value": MASK}]}
    assert result["screenshot"] == payload["screenshot"] and result["dom_snapshot"] == payload["dom_snapshot"]
    assert json.dumps(payload) == original
    log_policy = replace(RedactionPolicy(), preserve_screenshots=False, preserve_dom=False)
    assert redact_dict(payload, log_policy)["screenshot"] == MASK
    assert redact_dict(payload, log_policy)["dom_snapshot"] == MASK


def test_action_copy_cannot_reveal_typed_values_or_selectors() -> None:
    """Audit copies keep action and strategy but not entered text or locator values."""
    step = ActionStep(step_number=1, action="type",
                      locator=Locator(strategy="id", value="member_12345", robustness_notes="Stable",
                                      fallbacks=[Locator(strategy="css", value="input[data-id='12345']",
                                                         robustness_notes="Fallback")]),
                      value="John Doe", reasoning="Type alice@example.com", expected_outcome="Complete")
    cleaned = redact_action_step(step, RedactionPolicy())
    assert cleaned.action == "type" and cleaned.locator.strategy == "id"
    assert cleaned.locator.value == MASK and cleaned.locator.fallbacks[0].value == MASK
    assert cleaned.value == MASK and "alice@example.com" not in cleaned.reasoning
    assert step.value == "John Doe" and step.locator.value == "member_12345"


def test_request_preserves_raw_browser_state(escalation_record: object) -> None:
    """Handoff metadata masks values while screenshots and DOM remain exact."""
    request = escalation_record
    altered = replace(request, context_notes="Contact alice@example.com", goal="Find 123-45-6789")
    result = redact_escalation_request(altered, RedactionPolicy())
    assert result.screenshot == request.screenshot and result.dom_snapshot == request.dom_snapshot
    assert result.stuck_state.current_screenshot == request.stuck_state.current_screenshot
    assert result.stuck_state.current_dom == request.stuck_state.current_dom
    assert result.stuck_state.escalation_context["dom"] == request.stuck_state.escalation_context["dom"]
    assert "alice@example.com" not in result.context_notes
    assert "123-45-6789" not in result.goal
    assert validate_escalation_request(result) == (True, None)
    paranoid = redact_escalation_request(request, RedactionPolicy(redaction_level=RedactionLevel.PARANOID))
    assert paranoid.goal == MASK and paranoid.screenshot == request.screenshot


def test_session_persistence_masks_outputs_without_changing_live_result(tmp_path: Path) -> None:
    """Saved audits omit extracted data while the caller retains its in-memory result."""
    browser = Mock(spec=WebDriver)
    browser.session_id = "browser-1"
    manager = SessionLifecycleManager()
    session = manager.create_session("lookup", browser, {"member_id": "12345"})
    manager.start_session(session)
    manager.complete_session(session, {"balance": 5000, "private": {"email": "alice@example.com"}})
    cleaned = redact_session_metadata(session, RedactionPolicy())
    assert cleaned.session_id == session.session_id and cleaned.outputs == {"balance": MASK, "private": {"email": MASK}}
    assert session.outputs["balance"] == 5000
    path = tmp_path / "sessions" / f"{session.session_id}.json"
    save_session_metadata(session, str(path))
    assert "alice@example.com" not in path.read_text(encoding="utf-8")
    assert load_session_metadata(str(path)).outputs == cleaned.outputs


def test_private_evidence_file_is_unmodified_and_owner_only(tmp_path: Path) -> None:
    """A separate file retains raw signals without placing them in metadata."""
    evidence_id = str(uuid.uuid4())
    path = Path(save_raw_evidence("cG5n", "<input value='12345'>", str(tmp_path / "raw"), evidence_id,
                                  driver_logs=["raw browser diagnostics"]))
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["screenshots"] == "cG5n" and saved["dom_snapshot"] == "<input value='12345'>"
    assert saved["driver_logs"] == ["raw browser diagnostics"]
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
    bundle = EvidencePreservation(raw_evidence=saved, redacted_metadata={"member_id": MASK})
    assert bundle.redacted_metadata["member_id"] == MASK
    with pytest.raises(ValueError, match="UUID"):
        save_raw_evidence("cG5n", "<html>", str(tmp_path), "../unsafe")


def test_policy_loading_and_fail_closed_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Production rejects NONE and the JSON formatter never emits raw evidence."""
    monkeypatch.setenv("ALLOWLIST_MODE", "production")
    monkeypatch.setenv("REDACTION_LEVEL", "NONE")
    with pytest.raises(ValueError, match="development"):
        load_redaction_policy()
    monkeypatch.setenv("REDACTION_LEVEL", "STRICT")
    logger = get_logger("test_phase16_redaction")
    stream = io.StringIO()
    handler = logger.handlers[0]
    original_stream = handler.stream
    handler.setStream(stream)
    try:
        logger.info("private_data", extra={"email": "alice@example.com", "screenshot": "raw-base64",
                                           "details": "SSN 123-45-6789; member 12345",
                                           "input_params": {"custom_key": "unpatterned-secret"}})
    finally:
        handler.setStream(original_stream)
    text = stream.getvalue()
    assert "alice@example.com" not in text and "raw-base64" not in text and "123-45-6789" not in text
    assert "member 12345" not in text and "unpatterned-secret" not in text
    assert json.loads(text)["email"] == MASK
    assert json.loads(text)["input_params"]["custom_key"] == MASK
    monkeypatch.setenv("REDACTION_PATTERNS", '["["]')
    with pytest.raises(ValueError, match="regex"):
        load_redaction_policy()
    monkeypatch.delenv("REDACTION_PATTERNS")
    monkeypatch.setenv("REDACTION_FIELDS", "not-json")
    with pytest.raises(ValueError, match="JSON lists"):
        load_redaction_policy()
