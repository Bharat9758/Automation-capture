"""Session-scoped, redacted JSON events for replay auditing and analytics."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from src.logging import get_logger
from src.safety.data_redactor import MASK, RedactionPolicy, load_redaction_policy, redact_dict, redact_string


class EventType(StrEnum):
    """Stable names used in persisted audit streams."""

    STEP_EXECUTED = "STEP_EXECUTED"
    STEP_FAILED = "STEP_FAILED"
    STEP_SKIPPED = "STEP_SKIPPED"
    ERROR_OCCURRED = "ERROR_OCCURRED"
    ERROR_RECOVERED = "ERROR_RECOVERED"
    HARD_FAILURE = "HARD_FAILURE"
    ESCALATION_TRIGGERED = "ESCALATION_TRIGGERED"
    ESCALATION_APPROVED = "ESCALATION_APPROVED"
    ESCALATION_DENIED = "ESCALATION_DENIED"
    ALLOWLIST_VIOLATION = "ALLOWLIST_VIOLATION"
    RISK_CRITICAL = "RISK_CRITICAL"
    RISK_ELEVATED = "RISK_ELEVATED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    SESSION_CREATED = "SESSION_CREATED"
    SESSION_STARTED = "SESSION_STARTED"
    SESSION_PAUSED = "SESSION_PAUSED"
    SESSION_RESUMED = "SESSION_RESUMED"
    SESSION_COMPLETED = "SESSION_COMPLETED"
    SESSION_FAILED = "SESSION_FAILED"
    CHECKPOINT_VERIFIED = "CHECKPOINT_VERIFIED"
    CHECKPOINT_FAILED = "CHECKPOINT_FAILED"
    OUTPUT_EXTRACTED = "OUTPUT_EXTRACTED"
    BUSINESS_OUTCOME = "BUSINESS_OUTCOME"


@dataclass(frozen=True, kw_only=True)
class LogEvent:
    """One immutable, JSON-ready event with its full run context."""

    event_type: str
    timestamp: str
    session_id: str
    step_number: int | None
    artifact_id: str
    level: str
    message: str
    data: dict[str, Any]

    def __post_init__(self) -> None:
        """Reject malformed events before they enter the audit buffer."""
        if self.event_type not in EventType.__members__:
            raise ValueError("Unknown audit event type")
        if self.level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Invalid log level")
        if not self.session_id or not self.artifact_id or not isinstance(self.message, str):
            raise ValueError("Session, artifact, and message are required")
        if self.step_number is not None and (type(self.step_number) is not int or self.step_number < 0):
            raise ValueError("step_number must be a nonnegative integer")
        if not isinstance(self.data, dict):
            raise TypeError("event data must be a dictionary")
        try:
            parsed = datetime.fromisoformat(self.timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("timestamp must be an ISO datetime") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timestamp must include a timezone")

    def to_dict(self) -> dict[str, Any]:
        """Return a detached dictionary suitable for JSON persistence.

        Returns:
            All event fields, including a nullable step number.
        """
        return {"event_type": self.event_type, "timestamp": self.timestamp,
                "session_id": self.session_id, "step_number": self.step_number,
                "artifact_id": self.artifact_id, "level": self.level,
                "message": self.message, "data": json.loads(json.dumps(self.data, allow_nan=False))}

    def to_json(self) -> str:
        """Serialize this event as JSON.

        Returns:
            A JSON object string.
        """
        return json.dumps(self.to_dict(), ensure_ascii=False, allow_nan=False)


def _limit_strings(value: Any, limit: int, secrets: tuple[str, ...] = ()) -> Any:
    """Bound event size after masking and reject non-JSON objects.

    Args:
        value: Redacted event data.
        limit: Maximum characters per string.
        secrets: Caller-supplied input values to hide even without regex matches.

    Returns:
        A detached, bounded JSON value.
    """
    if isinstance(value, str):
        for secret in secrets:
            if len(secret) >= 3:
                value = value.replace(secret, "***REDACTED***")
            elif value == secret:
                value = "***REDACTED***"
        return value[:limit]
    if isinstance(value, dict):
        return {key: _limit_strings(item, limit, secrets) for key, item in value.items()}
    if isinstance(value, list):
        return [_limit_strings(item, limit, secrets) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise TypeError("Audit event contains a non-JSON value")


class StructuredLogger:
    """Record session events in memory and emit safe JSON through Python logging."""

    def __init__(self, session_id: str, artifact_id: str, log_level: str = "INFO",
                 policy: RedactionPolicy | None = None,
                 private_values: list[Any] | None = None) -> None:
        """Initialize a session logger with a fail-closed redaction policy.

        Args:
            session_id: Lifecycle identifier for this replay.
            artifact_id: Artifact being executed.
            log_level: Minimum level for the process stream.
            policy: Optional validated policy already loaded by replay.
            private_values: Values entered into this replay, masked in free text.
        """
        if not session_id or not artifact_id:
            raise ValueError("session_id and artifact_id are required")
        if log_level.upper() not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("Unknown structured log level")
        self.session_id = session_id
        self.artifact_id = artifact_id
        self.log_level = log_level.upper()
        self.policy = replace(policy or load_redaction_policy(), preserve_screenshots=False, preserve_dom=False)
        self._secrets = tuple(sorted({str(value) for value in (private_values or []) if value is not None
                                      and str(value)}, key=len, reverse=True))
        self._events: list[LogEvent] = []
        self._stream = get_logger("src.logging.structured_logger")
        if self._stream.level > logging.getLevelName(self.log_level):
            self._stream.setLevel(logging.getLevelName(self.log_level))
        try:
            self._max_chars = int(os.getenv("STRUCTURED_LOG_MAX_FIELD_CHARS", "512"))
        except ValueError as exc:
            raise ValueError("STRUCTURED_LOG_MAX_FIELD_CHARS must be a positive integer") from exc
        if self._max_chars <= 0:
            raise ValueError("STRUCTURED_LOG_MAX_FIELD_CHARS must be a positive integer")

    def log_event(self, event_type: EventType, message: str, *, level: str = "INFO",
                  step_number: int | None = None, data: dict[str, Any] | None = None) -> LogEvent:
        """Redact, validate, buffer, and emit an event.

        Args:
            event_type: Stable event category.
            message: Human-readable summary.
            level: Standard Python log level.
            step_number: Current one-based step or zero for navigation.
            data: Additional structured context; never supply raw evidence.

        Returns:
            The safely buffered event.
        """
        if not isinstance(event_type, EventType):
            raise ValueError("event_type must be an EventType")
        if data is not None and not isinstance(data, dict):
            raise TypeError("event data must be a dictionary")
        safe_data = _limit_strings(redact_dict(data or {}, self.policy), self._max_chars, self._secrets)
        safe_message = _limit_strings(redact_string(message, self.policy), self._max_chars, self._secrets)
        event = LogEvent(event_type=event_type.value,
                         timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                         session_id=self.session_id, artifact_id=self.artifact_id,
                         step_number=step_number, level=level.upper(), message=safe_message,
                         data=safe_data)
        # Force JSON validation before buffering or emitting any event.
        event.to_json()
        self._events.append(event)
        if logging.getLevelName(event.level) >= logging.getLevelName(self.log_level):
            self._stream.log(logging.getLevelName(event.level), event.message,
                             extra={"event_type": event.event_type, "session_id": event.session_id,
                                    "artifact_id": event.artifact_id, "step_number": event.step_number,
                                    "data": event.data})
        return replace(event, data=json.loads(json.dumps(event.data)))

    def log_step_executed(self, step_number: int, action: str, locator: dict[str, Any] | None,
                          duration_ms: int, risk_level: str, success: bool) -> LogEvent:
        """Record action timing, outcome, and locator strategy.

        Args:
            step_number: Recorded action number.
            action: Browser action name.
            locator: Selector dictionary, masked before buffering.
            duration_ms: Elapsed milliseconds.
            risk_level: Risk assessment level.
            success: Whether the action completed.

        Returns:
            Redacted action event.
        """
        return self.log_event(EventType.STEP_EXECUTED if success else EventType.STEP_FAILED,
                              f"Step {step_number}: {action} {'succeeded' if success else 'failed'}",
                              level="INFO" if success else "ERROR", step_number=step_number,
                              data={"action": action, "locator": locator, "duration_ms": duration_ms,
                                    "risk_level": risk_level, "success": success})

    def log_error(self, step_number: int, error_type: str, error_message: str,
                  classification: str, recovery_attempted: bool) -> LogEvent:
        """Record a classified failure without raw exception data.

        Args:
            step_number: Affected step.
            error_type: Exception class name.
            error_message: Error summary subject to configured redaction.
            classification: Business outcome, recoverable, or hard failure.
            recovery_attempted: Whether a repair was attempted.

        Returns:
            Classified error event.
        """
        return self.log_event(EventType.ERROR_OCCURRED, f"Step {step_number}: {error_type}",
                              level="CRITICAL" if classification == "hard_failure" else "WARNING",
                              step_number=step_number,
                              data={"error_type": error_type, "error_message": MASK if error_message else "",
                                    "classification": classification, "recovery_attempted": recovery_attempted})

    def log_escalation(self, reason: str, step_number: int, escalation_id: str,
                       risk_level: str) -> LogEvent:
        """Record a human handoff request.

        Args:
            reason: Safe or redacted pause reason.
            step_number: Paused step.
            escalation_id: Durable handoff identifier.
            risk_level: Assessed risk at the pause.

        Returns:
            Escalation event.
        """
        return self.log_event(EventType.ESCALATION_TRIGGERED, "Escalation triggered",
                              level="WARNING", step_number=step_number,
                              data={"reason": reason, "escalation_id": escalation_id,
                                    "risk_level": risk_level})

    def log_approval(self, escalation_id: str, step_number: int, approved: bool,
                     operator_id: str, reason: str) -> LogEvent:
        """Record an operator's step-bound decision.

        Args:
            escalation_id: Handoff identifier.
            step_number: Reviewed step.
            approved: Whether the operator authorized this step.
            operator_id: Human operator identifier.
            reason: Operator's explanation.

        Returns:
            Approval or denial event.
        """
        return self.log_event(EventType.ESCALATION_APPROVED if approved else EventType.ESCALATION_DENIED,
                              "Escalation approved" if approved else "Escalation denied",
                              level="INFO" if approved else "WARNING", step_number=step_number,
                              data={"escalation_id": escalation_id, "operator_id": operator_id,
                                    "reason": reason})

    def log_checkpoint(self, checkpoint_condition: str, passed: bool, expected_value: str | None,
                       actual_value: str | None) -> LogEvent:
        """Record the gate between replay and output extraction.

        Args:
            checkpoint_condition: Condition evaluated.
            passed: Whether it held.
            expected_value: Redacted expected value.
            actual_value: Redacted observed value.

        Returns:
            Verification event.
        """
        return self.log_event(EventType.CHECKPOINT_VERIFIED if passed else EventType.CHECKPOINT_FAILED,
                              "Success checkpoint passed" if passed else "Success checkpoint failed",
                              level="INFO" if passed else "ERROR",
                              data={"condition": checkpoint_condition,
                                    "expected_value": MASK if expected_value is not None else None,
                                    "actual_value": MASK if actual_value is not None else None, "passed": passed})

    def log_session_state(self, event: str, state: str,
                          duration_seconds: int | float | None = None) -> LogEvent:
        """Record a lifecycle transition and its elapsed time.

        Args:
            event: SESSION_* event name.
            state: Resulting lifecycle state.
            duration_seconds: Optional elapsed run time.

        Returns:
            Session event.
        """
        event_type = EventType(event)
        if not event_type.name.startswith("SESSION_"):
            raise ValueError("Session state requires a SESSION_* event")
        return self.log_event(event_type, f"Session {state}",
                              level="ERROR" if event_type == EventType.SESSION_FAILED else "INFO",
                              data={"state": state, "duration_seconds": duration_seconds})

    def log_business_outcome(self, outcome: str, extracted_values: dict[str, Any],
                             duration_seconds: int | float) -> LogEvent:
        """Record the outcome with protected extracted values.

        Args:
            outcome: Business outcome code.
            extracted_values: Output values to mask before storage.
            duration_seconds: Elapsed run time.

        Returns:
            Redacted business outcome event.
        """
        return self.log_event(EventType.BUSINESS_OUTCOME, "Business outcome observed",
                              data={"outcome": outcome, "outputs": extracted_values,
                                    "duration_seconds": duration_seconds})

    def get_events(self) -> list[LogEvent]:
        """Get a snapshot of buffered events.

        Returns:
            Independent event list.
        """
        return [replace(event, data=json.loads(json.dumps(event.data))) for event in self._events]

    def restore_events(self, events: list[LogEvent]) -> None:
        """Restore a prior session's events before a human-approved resume.

        Args:
            events: Previously persisted events from this same run.

        Raises:
            ValueError: If any event belongs to another session or artifact.
        """
        if any(not isinstance(event, LogEvent) or event.session_id != self.session_id
               or event.artifact_id != self.artifact_id for event in events):
            raise ValueError("Prior audit events do not match this session")
        self._events = [replace(event,
                                message=_limit_strings(redact_string(event.message, self.policy), self._max_chars,
                                                       self._secrets),
                                data=_limit_strings(redact_dict(event.data, self.policy), self._max_chars,
                                                    self._secrets))
                        for event in events]

    def get_events_json(self) -> str:
        """Serialize all buffered events as a pretty JSON array.

        Returns:
            Pretty JSON containing all events.
        """
        return json.dumps([event.to_dict() for event in self._events], indent=2, ensure_ascii=False,
                          allow_nan=False)

    def get_summary(self) -> dict[str, Any]:
        """Summarize recorded events.

        Returns:
            Counts, timing, and final outcome.
        """
        summary = get_log_statistics(self.get_events())
        return {key: summary[key] for key in ("total_events", "errors", "escalations", "approvals",
                                                "duration_seconds", "final_status")}


def save_logs_to_file(logger: StructuredLogger, filepath: str) -> str:
    """Atomically persist a private JSON event stream.

    Args:
        logger: Source session logger.
        filepath: Destination under the configured evidence directory.

    Returns:
        The written file path.

    Raises:
        OSError: If private persistence fails.
        ValueError: If the destination is empty.
    """
    if not isinstance(logger, StructuredLogger) or not isinstance(filepath, str) or not filepath.strip():
        raise ValueError("logger and filepath are required")
    target = Path(filepath)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".audit-",
                                         suffix=".tmp", dir=target.parent, delete=False) as stream:
            temporary = stream.name
            os.chmod(temporary, 0o600)
            stream.write(logger.get_events_json())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
        return str(target)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def load_logs_from_file(filepath: str) -> list[LogEvent]:
    """Load a previous audit stream for a resumed browser session.

    Args:
        filepath: Path to an existing event array.

    Returns:
        Validated, immutable events in recorded order.
    """
    with open(filepath, encoding="utf-8") as stream:
        data = json.load(stream)
    if not isinstance(data, list):
        raise ValueError("Audit file must contain a JSON array")
    return [LogEvent(**item) for item in data]


def get_log_statistics(events: list[LogEvent]) -> dict[str, Any]:
    """Aggregate event counts and timing for one run.

    Args:
        events: Buffered or loaded session events.

    Returns:
        Event distribution, error and escalation counts, and final outcome.
    """
    if not all(isinstance(event, LogEvent) for event in events):
        raise TypeError("events must contain LogEvent objects")
    counts = Counter(event.event_type for event in events)
    step_durations = [event.data.get("duration_ms") for event in events
                      if event.event_type == EventType.STEP_EXECUTED and event.data.get("success") is True
                      and type(event.data.get("duration_ms")) in {int, float}]
    duration = next((event.data.get("duration_seconds") for event in reversed(events)
                     if event.event_type in {EventType.SESSION_COMPLETED, EventType.SESSION_FAILED}
                     and event.data.get("duration_seconds") is not None), None)
    if duration is None and len(events) > 1:
        duration = max(0.0, (datetime.fromisoformat(events[-1].timestamp.replace("Z", "+00:00"))
                             - datetime.fromisoformat(events[0].timestamp.replace("Z", "+00:00"))).total_seconds())
    final = next((event.event_type for event in reversed(events)
                  if event.event_type in {EventType.SESSION_COMPLETED, EventType.SESSION_FAILED,
                                          EventType.SESSION_PAUSED, EventType.BUSINESS_OUTCOME}), None)
    statuses = {EventType.SESSION_COMPLETED: "success", EventType.SESSION_FAILED: "failure",
                EventType.SESSION_PAUSED: "paused", EventType.BUSINESS_OUTCOME: "business_outcome"}
    business_steps = {event.step_number for event in events if event.event_type == EventType.ERROR_OCCURRED
                      and event.data.get("classification") == "expected_business_outcome"}
    error_steps = {event.step_number for event in events if event.step_number not in business_steps
                   and event.event_type in {
        EventType.ERROR_OCCURRED, EventType.HARD_FAILURE, EventType.STEP_FAILED,
        EventType.CHECKPOINT_FAILED}}
    if final == EventType.SESSION_COMPLETED and any(event.event_type == EventType.BUSINESS_OUTCOME
                                                   and event.data.get("outcome") != "success" for event in events):
        final_status = "business_outcome"
    else:
        final_status = statuses.get(final, "in_progress")
    return {"total_events": len(events), "event_types": dict(counts),
            "errors": len(error_steps),
            "escalations": counts[EventType.ESCALATION_TRIGGERED],
            "approvals": counts[EventType.ESCALATION_APPROVED],
            "rejections": counts[EventType.ESCALATION_DENIED],
            "duration_seconds": duration if duration is not None else 0.0,
            "average_step_duration_ms": (sum(step_durations) / len(step_durations)) if step_durations else 0.0,
            "final_status": final_status}
