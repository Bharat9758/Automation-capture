"""Tests for redacted, session-scoped JSON audit events."""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from src.logging.structured_logger import (
    EventType, LogEvent, StructuredLogger, get_log_statistics, load_logs_from_file,
    save_logs_to_file,
)


@pytest.fixture
def audit() -> StructuredLogger:
    """Create one audit stream with the default strict redaction policy.

    Returns:
        Session-bound logger.
    """
    return StructuredLogger("session-17", "artifact-17")


def test_step_events_are_timed_contextual_and_redacted(audit: StructuredLogger) -> None:
    """Typed values and selectors never reach the in-memory event buffer."""
    event = audit.log_step_executed(3, "type", {"strategy": "id", "value": "member_12345"},
                                    250, "CAUTION", True)
    payload = json.loads(event.to_json())
    assert payload["event_type"] == "STEP_EXECUTED"
    assert payload["session_id"] == "session-17" and payload["artifact_id"] == "artifact-17"
    assert payload["step_number"] == 3 and payload["data"]["duration_ms"] == 250
    assert payload["data"]["locator"]["strategy"] == "id"
    assert "12345" not in event.to_json()
    assert datetime.fromisoformat(event.timestamp.replace("Z", "+00:00")).tzinfo is not None
    assert audit.get_events() == [event]
    event.data["locator"]["value"] = "caller mutation"
    assert "caller mutation" not in audit.get_events_json()


def test_all_specialized_events_and_statistics(audit: StructuredLogger) -> None:
    """Errors, approvals, checkpoints, state and outcomes retain useful counts."""
    audit.log_session_state("SESSION_CREATED", "created")
    audit.log_session_state("SESSION_STARTED", "running")
    audit.log_step_executed(1, "click", None, 150, "SAFE", True)
    audit.log_step_executed(2, "click", None, 300, "RISKY", False)
    audit.log_error(2, "TimeoutException", "No reply for member_id=12345",
                    "recoverable_condition", True)
    audit.log_escalation("Dialog blocked progress", 2, "esc-17", "RISKY")
    audit.log_approval("esc-17", 2, True, "operator_1", "Reviewed balance $500.00")
    audit.log_checkpoint("text_contains", True, "Balance", "Balance $500.00")
    audit.log_business_outcome("member_found", {"savings_balance": 500}, 45)
    audit.log_session_state("SESSION_COMPLETED", "completed", 45)
    stats = get_log_statistics(audit.get_events())
    assert stats["event_types"]["ESCALATION_APPROVED"] == 1
    assert stats["errors"] >= 1 and stats["escalations"] == 1
    assert stats["approvals"] == 1 and stats["rejections"] == 0
    assert stats["duration_seconds"] == 45 and stats["average_step_duration_ms"] == 150
    assert stats["final_status"] == "business_outcome"
    assert audit.get_summary()["total_events"] == 10
    serialized = audit.get_events_json()
    assert "12345" not in serialized and "500.00" not in serialized
    assert "savings_balance" in serialized and "***REDACTED***" in serialized


def test_denial_and_failure_are_distinct(audit: StructuredLogger) -> None:
    """A denied escalation ends with a failed run status."""
    audit.log_approval("esc", 1, False, "operator", "Not authorized")
    audit.log_session_state("SESSION_FAILED", "failed", 3)
    stats = get_log_statistics(audit.get_events())
    assert stats["rejections"] == 1 and stats["final_status"] == "failure"


def test_private_atomic_persistence_and_resume(audit: StructuredLogger, tmp_path: Path) -> None:
    """A resumed run retains prior redacted events under its original ID."""
    audit.log_session_state("SESSION_PAUSED", "paused")
    path = tmp_path / "logs" / "session-17.json"
    assert save_logs_to_file(audit, str(path)) == str(path)
    assert os.stat(path).st_mode & 0o777 == 0o600
    restored = StructuredLogger("session-17", "artifact-17")
    restored.restore_events(load_logs_from_file(str(path)))
    restored.log_session_state("SESSION_RESUMED", "resumed")
    save_logs_to_file(restored, str(path))
    assert [item.event_type for item in load_logs_from_file(str(path))] == [
        EventType.SESSION_PAUSED, EventType.SESSION_RESUMED]


def test_reject_invalid_event_and_mismatched_restore(audit: StructuredLogger) -> None:
    """A corrupt audit file or another session cannot join this stream."""
    with pytest.raises(ValueError, match="Unknown"):
        LogEvent(event_type="OTHER", timestamp="2026-10-01T00:00:00Z", session_id="s",
                 artifact_id="a", step_number=None, level="INFO", message="message", data={})
    other = StructuredLogger("other", "artifact-17")
    other.log_session_state("SESSION_CREATED", "created")
    with pytest.raises(ValueError, match="match"):
        audit.restore_events(other.get_events())


def test_fail_closed_on_non_json_or_bad_config(monkeypatch: pytest.MonkeyPatch, audit: StructuredLogger) -> None:
    """Malformed data and configuration cannot silently bypass redaction."""
    with pytest.raises(TypeError, match="dictionary"):
        audit.log_event(EventType.ERROR_OCCURRED, "Error", data=["bad"])  # type: ignore[arg-type]
    assert audit.get_events() == []
    monkeypatch.setenv("STRUCTURED_LOG_MAX_FIELD_CHARS", "zero")
    with pytest.raises(ValueError, match="positive integer"):
        StructuredLogger("session", "artifact")
