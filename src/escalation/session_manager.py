"""Track the complete lifecycle and audit summary of a browser replay."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from selenium.webdriver.remote.webdriver import WebDriver

from src.escalation.escalation_request import EscalationRequest, escalation_to_json, json_to_escalation
from src.logging import get_logger


LOGGER = get_logger(__name__)


class SessionLifecycle(StrEnum):
    """Valid lifecycle states for one automation run."""

    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    HUMAN_CONTROL = "human_control"
    RESUMED = "resumed"
    COMPLETED = "completed"
    FAILED = "failed"
    ABANDONED = "abandoned"


_TRANSITIONS: dict[SessionLifecycle, set[SessionLifecycle]] = {
    SessionLifecycle.CREATED: {SessionLifecycle.RUNNING, SessionLifecycle.ABANDONED},
    SessionLifecycle.RUNNING: {SessionLifecycle.PAUSED, SessionLifecycle.COMPLETED,
                               SessionLifecycle.FAILED, SessionLifecycle.ABANDONED},
    SessionLifecycle.PAUSED: {SessionLifecycle.HUMAN_CONTROL, SessionLifecycle.FAILED, SessionLifecycle.ABANDONED},
    SessionLifecycle.HUMAN_CONTROL: {SessionLifecycle.PAUSED, SessionLifecycle.RESUMED,
                                     SessionLifecycle.FAILED, SessionLifecycle.ABANDONED},
    SessionLifecycle.RESUMED: {SessionLifecycle.RUNNING, SessionLifecycle.PAUSED,
                               SessionLifecycle.COMPLETED, SessionLifecycle.FAILED, SessionLifecycle.ABANDONED},
    SessionLifecycle.COMPLETED: set(),
    SessionLifecycle.FAILED: set(),
    SessionLifecycle.ABANDONED: set(),
}


def _now() -> str:
    """Return a timezone-aware ISO timestamp.

    Returns:
        Current UTC timestamp.
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _datetime(value: str) -> datetime:
    """Parse a timezone-aware ISO timestamp.

    Args:
        value: UTC or offset timestamp.

    Returns:
        Aware datetime.

    Raises:
        ValueError: If the timestamp is naive or malformed.
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Session timestamps require a timezone")
    return parsed


@dataclass(kw_only=True)
class SessionMetadata:
    """Durable audit metadata bound to an optional live WebDriver."""

    session_id: str
    artifact_id: str
    created_at: str
    started_at: str | None = None
    paused_at: str | None = None
    resumed_at: str | None = None
    completed_at: str | None = None
    lifecycle_state: SessionLifecycle = SessionLifecycle.CREATED
    driver_instance: WebDriver | None = field(default=None, repr=False, compare=False)
    webdriver_session_id: str | None = None
    escalations: list[EscalationRequest] = field(default_factory=list)
    human_actions_total: int = 0
    step_executions: list[dict[str, Any]] = field(default_factory=list)
    human_started_at: str | None = None
    human_duration_seconds: float = 0.0
    error_reason: str | None = None
    outcome: str | None = None
    outputs: dict[str, Any] = field(default_factory=dict)
    input_names: list[str] = field(default_factory=list)


class SessionLifecycleManager:
    """Enforce legal lifecycle transitions and maintain run statistics."""

    @staticmethod
    def _transition(session: SessionMetadata, state: SessionLifecycle) -> None:
        """Reject invalid or duplicate state transitions.

        Args:
            session: Existing lifecycle record.
            state: Desired state.

        Raises:
            RuntimeError: If the transition is invalid.
        """
        if state not in _TRANSITIONS[session.lifecycle_state]:
            raise RuntimeError(f"Cannot transition from {session.lifecycle_state} to {state}")
        session.lifecycle_state = state
        LOGGER.info("session_transition", extra={"event": "session_transition", "session_id": session.session_id,
                                                 "state": state.value})

    def create_session(self, artifact_id: str, driver: WebDriver, input_params: dict[str, Any]) -> SessionMetadata:
        """Create a new UUID-identified lifecycle on the supplied browser.

        Args:
            artifact_id: Recorded automation artifact ID.
            driver: Already-created browser (never recreated from an ID).
            input_params: Caller inputs; only field names are retained.

        Returns:
            New session in created state.

        Raises:
            ValueError: If artifact ID, browser ID, or inputs are invalid.
        """
        browser_id = getattr(driver, "session_id", None)
        if (not isinstance(artifact_id, str) or not artifact_id or not isinstance(browser_id, str)
                or not browser_id or not isinstance(input_params, dict)
                or any(not isinstance(name, str) for name in input_params)):
            raise ValueError("Valid artifact ID, live WebDriver session ID, and input names are required")
        session = SessionMetadata(session_id=str(uuid.uuid4()), artifact_id=artifact_id, created_at=_now(),
                                  driver_instance=driver, webdriver_session_id=browser_id,
                                  input_names=sorted(input_params))
        LOGGER.info("session_created", extra={"event": "session_created", "session_id": session.session_id,
                                               "artifact_id": artifact_id})
        return session

    def start_session(self, session: SessionMetadata) -> SessionMetadata:
        """Start deterministic replay on a created browser.

        Args:
            session: New session.

        Returns:
            Running session with a start timestamp.
        """
        _live(session)
        self._transition(session, SessionLifecycle.RUNNING)
        session.started_at = session.started_at or _now()
        return session

    def add_escalation_to_session(self, session: SessionMetadata, escalation: EscalationRequest) -> SessionMetadata:
        """Add an escalation exactly once without duplicating pause history.

        Args:
            session: Active or paused session.
            escalation: Valid recorded escalation.

        Returns:
            Session with the escalation recorded.
        """
        if session.lifecycle_state not in {SessionLifecycle.RUNNING, SessionLifecycle.RESUMED, SessionLifecycle.PAUSED}:
            raise RuntimeError("Escalations can only be added during or after an active replay")
        if not isinstance(escalation, EscalationRequest) or escalation.artifact_id != session.artifact_id:
            raise ValueError("Escalation must match the artifact")
        if escalation.session_id != session.webdriver_session_id:
            raise ValueError("Escalation must reference the original browser session")
        if all(item.escalation_id != escalation.escalation_id for item in session.escalations):
            session.escalations.append(escalation)
        return session

    def pause_session(self, session: SessionMetadata, escalation: EscalationRequest) -> SessionMetadata:
        """Pause running replay and record its escalation.

        Args:
            session: Running or resumed session.
            escalation: Cause of the pause.

        Returns:
            Paused session with a recorded escalation.
        """
        _live(session)
        if session.lifecycle_state not in {SessionLifecycle.RUNNING, SessionLifecycle.RESUMED}:
            raise RuntimeError("Only a running session can be paused")
        self.add_escalation_to_session(session, escalation)
        self._transition(session, SessionLifecycle.PAUSED)
        session.paused_at = _now()
        return session

    def give_control_to_human(self, session: SessionMetadata, *, started_at: str | None = None) -> SessionMetadata:
        """Mark the lifecycle as human controlled after a Phase 12 handoff.

        Args:
            session: Paused lifecycle.
            started_at: Actual handoff timestamp, if the lifecycle is loaded later.

        Returns:
            Human-controlled lifecycle.
        """
        _live(session)
        begun = started_at or _now()
        _datetime(begun)
        self._transition(session, SessionLifecycle.HUMAN_CONTROL)
        session.human_started_at = begun
        return session

    def resume_session(self, session: SessionMetadata, *, human_actions: int = 0) -> SessionMetadata:
        """Resume only after the human has taken control.

        Args:
            session: Human-controlled lifecycle.
            human_actions: Newly completed actions, never cumulative input.

        Returns:
            Resumed lifecycle.
        """
        _live(session)
        if isinstance(human_actions, bool) or not isinstance(human_actions, int) or human_actions < 0:
            raise ValueError("human_actions must be nonnegative")
        self._transition(session, SessionLifecycle.RESUMED)
        session.resumed_at = _now()
        if session.human_started_at:
            session.human_duration_seconds += max(0.0, (_datetime(session.resumed_at) - _datetime(session.human_started_at)).total_seconds())
            session.human_started_at = None
        session.human_actions_total += human_actions
        return session

    def complete_session(self, session: SessionMetadata, outputs: dict[str, Any] | None = None,
                         *, outcome: str = "success") -> SessionMetadata:
        """Close a successful run or a legitimate business outcome.

        Args:
            session: Running or resumed lifecycle.
            outputs: Extracted values, if any.
            outcome: Success or business-outcome code.

        Returns:
            Completed session.
        """
        if session.lifecycle_state not in {SessionLifecycle.RUNNING, SessionLifecycle.RESUMED}:
            raise RuntimeError("Only running automation can complete")
        self._transition(session, SessionLifecycle.COMPLETED)
        session.completed_at, session.outcome = _now(), outcome
        session.outputs = dict(outputs or {})
        return session

    def fail_session(self, session: SessionMetadata, error_reason: str) -> SessionMetadata:
        """Close a run that cannot proceed safely.

        Args:
            session: Nonterminal session.
            error_reason: Safe failure description.

        Returns:
            Failed session.
        """
        if not isinstance(error_reason, str) or not error_reason.strip():
            raise ValueError("error_reason is required")
        self._transition(session, SessionLifecycle.FAILED)
        session.completed_at, session.outcome, session.error_reason = _now(), "failure", error_reason
        return session

    def abandon_session(self, session: SessionMetadata) -> SessionMetadata:
        """End a cancelled run without quitting the caller-owned driver.

        Args:
            session: Nonterminal session.

        Returns:
            Abandoned session.
        """
        self._transition(session, SessionLifecycle.ABANDONED)
        session.completed_at, session.outcome = _now(), "abandoned"
        return session

    def track_step_execution(self, session: SessionMetadata, step_num: int,
                             success: bool, duration_ms: int) -> SessionMetadata:
        """Record step outcome and timing without values or DOM.

        Args:
            session: Running or resumed lifecycle.
            step_num: One-based artifact step.
            success: Whether the action and outcome completed.
            duration_ms: Total action and expected-outcome time.

        Returns:
            Updated session.
        """
        if session.lifecycle_state not in {SessionLifecycle.RUNNING, SessionLifecycle.RESUMED}:
            raise RuntimeError("Step execution requires running automation")
        if (isinstance(step_num, bool) or not isinstance(step_num, int) or step_num < 1
                or type(success) is not bool or isinstance(duration_ms, bool)
                or not isinstance(duration_ms, int) or duration_ms < 0):
            raise ValueError("Invalid step number, success, or duration_ms")
        session.step_executions.append({"step_number": step_num, "success": success,
                                        "duration_ms": duration_ms, "timestamp": _now()})
        LOGGER.info("session_step", extra={"event": "session_step", "session_id": session.session_id,
                                           "step": step_num, "success": success, "duration_ms": duration_ms})
        return session

    def get_escalations_for_session(self, session: SessionMetadata) -> list[EscalationRequest]:
        """Return escalations ordered by timestamp.

        Args:
            session: Tracked run.

        Returns:
            Independent ascending list of escalation objects.
        """
        return sorted(session.escalations, key=lambda item: _datetime(item.timestamp))

    def get_session_escalation_summary(self, session: SessionMetadata) -> dict[str, Any]:
        """Summarize recorded escalations and human recovery time.

        Args:
            session: Tracked run.

        Returns:
            Count, reasons, actions, recovery duration, and outcome.
        """
        return {"total_escalations": len(session.escalations),
                "reasons": [item.reason_escalated for item in self.get_escalations_for_session(session)],
                "human_actions": session.human_actions_total,
                "total_recovery_time": round(session.human_duration_seconds, 3),
                "successful_recovery": bool(session.escalations and session.lifecycle_state == SessionLifecycle.COMPLETED
                                            and session.outcome == "success")}

    def calculate_session_metrics(self, session: SessionMetadata) -> dict[str, Any]:
        """Calculate durations and outcome statistics without exposing page data.

        Args:
            session: Tracked run.

        Returns:
            Timing, step counts, escalation counts, and reusability flag.
        """
        end = _datetime(session.completed_at) if session.completed_at else datetime.now(timezone.utc)
        started = _datetime(session.started_at or session.created_at)
        elapsed = max(0.0, (end - started).total_seconds())
        steps = session.step_executions
        success = sum(item["success"] for item in steps)
        human = session.human_duration_seconds
        if session.human_started_at:
            human += max(0.0, (end - _datetime(session.human_started_at)).total_seconds())
        return {"total_duration_seconds": round(elapsed, 3), "automation_duration_seconds": round(max(0.0, elapsed - human), 3),
                "human_duration_seconds": round(human, 3), "automation_steps": len(steps),
                "successful_steps": success, "failed_steps": len(steps) - success,
                "escalations": len(session.escalations), "human_interventions": session.human_actions_total,
                "final_status": session.outcome or session.lifecycle_state.value,
                "reuse_ready": session.lifecycle_state == SessionLifecycle.COMPLETED and not session.escalations and success == len(steps)}

    def is_artifact_improvement_candidate(self, session: SessionMetadata) -> bool:
        """Flag runs whose recorded failures or human intervention suggest repair.

        Args:
            session: Completed or in-progress run.

        Returns:
            True if the artifact deserves review.
        """
        return bool(session.lifecycle_state == SessionLifecycle.FAILED or session.escalations
                    or any(not item["success"] for item in session.step_executions))

    def get_session_summary(self, session: SessionMetadata) -> dict[str, Any]:
        """Produce a compact operator-facing status.

        Args:
            session: Tracked run.

        Returns:
            Identifiers, state, timing, and outcome.
        """
        metrics = self.calculate_session_metrics(session)
        return {"session_id": session.session_id, "artifact_id": session.artifact_id,
                "state": session.lifecycle_state.value, "duration_seconds": metrics["total_duration_seconds"],
                "escalations": metrics["escalations"], "human_actions": session.human_actions_total,
                "outcome": session.outcome or session.lifecycle_state.value}

    def get_full_session_report(self, session: SessionMetadata) -> dict[str, Any]:
        """Produce a detailed report without copying screenshots into its summary.

        Args:
            session: Tracked run.

        Returns:
            State, durations, step audit, escalation IDs, and outputs.
        """
        metrics = self.calculate_session_metrics(session)
        return {"session_id": session.session_id, "artifact_id": session.artifact_id,
                "created_at": session.created_at, "started_at": session.started_at,
                "paused_at": session.paused_at, "resumed_at": session.resumed_at,
                "completed_at": session.completed_at, "total_duration": metrics["total_duration_seconds"],
                "automation_duration": metrics["automation_duration_seconds"],
                "human_duration": metrics["human_duration_seconds"],
                "escalations": [item.escalation_id for item in session.escalations],
                "steps": [entry.copy() for entry in session.step_executions],
                "final_status": session.lifecycle_state.value, "outputs": dict(session.outputs)}


def _live(session: SessionMetadata) -> WebDriver:
    """Require the exact original browser before automation or human control.

    Args:
        session: Current lifecycle metadata.

    Returns:
        Original live WebDriver.

    Raises:
        RuntimeError: If detached or browser ID differs.
    """
    driver = session.driver_instance
    if driver is None or getattr(driver, "session_id", None) != session.webdriver_session_id:
        raise RuntimeError("Original WebDriver session is unavailable or has changed")
    return driver


def save_session_metadata(session: SessionMetadata, filepath: str) -> str:
    """Atomically persist metadata with private file permissions.

    Args:
        session: Lifecycle state and audit to persist.
        filepath: JSON destination.

    Returns:
        Destination filepath.
    """
    payload: dict[str, Any] = {key: value for key, value in vars(session).items()
                               if key not in {"driver_instance", "escalations"}}
    payload["lifecycle_state"] = session.lifecycle_state.value
    payload["escalations"] = [json.loads(escalation_to_json(item)) for item in session.escalations]
    body = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False)
    target = Path(filepath)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix=f".{target.name}.", delete=False) as handle:
            temporary = handle.name
            os.chmod(temporary, 0o600)
            handle.write(body + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    LOGGER.info("session_saved", extra={"event": "session_saved", "session_id": session.session_id,
                                       "state": session.lifecycle_state.value})
    return filepath


def load_session_metadata(filepath: str, driver: WebDriver | None = None) -> SessionMetadata:
    """Load a lifecycle record; attach only its original still-live WebDriver.

    Args:
        filepath: Existing session JSON.
        driver: Optional original browser instance.

    Returns:
        Detached metadata or a matching live session.

    Raises:
        ValueError: If JSON, state, timestamps, or browser identity is invalid.
    """
    try:
        data = json.loads(Path(filepath).read_text(encoding="utf-8"))
        if not isinstance(data, dict) or set(data) != set(SessionMetadata.__dataclass_fields__) - {"driver_instance"}:
            raise ValueError("Session metadata fields are incomplete")
        data["lifecycle_state"] = SessionLifecycle(data["lifecycle_state"])
        data["escalations"] = [json_to_escalation(json.dumps(item)) for item in data["escalations"]]
        session = SessionMetadata(**data, driver_instance=driver)
        uuid.UUID(session.session_id)
        if not session.artifact_id or not session.webdriver_session_id:
            raise ValueError("Session identifiers must be nonempty")
        for value in (session.created_at, session.started_at, session.paused_at, session.resumed_at,
                      session.completed_at, session.human_started_at):
            if value is not None:
                _datetime(value)
        if driver is not None and getattr(driver, "session_id", None) != session.webdriver_session_id:
            raise ValueError("Original WebDriver session ID mismatch")
        if (not isinstance(session.step_executions, list) or not isinstance(session.outputs, dict)
                or isinstance(session.human_actions_total, bool) or not isinstance(session.human_actions_total, int)
                or session.human_actions_total < 0):
            raise ValueError("Invalid session audit fields")
        return session
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError, ValueError) as exc:
        raise ValueError(f"Invalid session metadata: {type(exc).__name__}") from exc


def list_sessions(directory: str | None = None) -> list[SessionMetadata]:
    """List saved session records newest first without reviving browsers.

    Args:
        directory: Metadata directory, default SESSION_DIRECTORY.

    Returns:
        Newest-first detached sessions; empty for absent directory.
    """
    folder = Path(directory or os.environ.get("SESSION_DIRECTORY", "evidence/sessions"))
    if not folder.exists():
        return []
    if not folder.is_dir():
        raise NotADirectoryError(str(folder))
    sessions = [load_session_metadata(str(path)) for path in folder.glob("*.json") if path.is_file()]
    return sorted(sessions, key=lambda item: _datetime(item.created_at), reverse=True)


def cleanup_old_sessions(directory: str, days: int | None = None) -> int:
    """Remove only terminal session records older than the retention window.

    Active, paused, and human-controlled sessions remain for audit and resume.
    Escalation evidence is retained separately; this function removes only
    session metadata JSON files in the requested directory.

    Args:
        directory: Session metadata directory.
        days: Minimum age in days; defaults to SESSION_RETENTION_DAYS.

    Returns:
        Count of deleted terminal session metadata files.

    Raises:
        ValueError: If retention is invalid or a record fails validation.
    """
    retention = int(os.environ.get("SESSION_RETENTION_DAYS", "30")) if days is None else days
    if isinstance(retention, bool) or not isinstance(retention, int) or retention < 0:
        raise ValueError("days must be a nonnegative integer")
    folder = Path(directory)
    if not folder.exists():
        return 0
    if not folder.is_dir():
        raise NotADirectoryError(str(folder))
    # Validate every record first; malformed evidence must not cause a partial purge.
    candidates = [(path, load_session_metadata(str(path))) for path in folder.glob("*.json") if path.is_file()]
    cutoff = datetime.now(timezone.utc) - timedelta(days=retention)
    deleted = 0
    for path, session in candidates:
        if session.lifecycle_state in {SessionLifecycle.COMPLETED, SessionLifecycle.FAILED, SessionLifecycle.ABANDONED} and _datetime(session.created_at) < cutoff:
            path.unlink()
            deleted += 1
    LOGGER.info("sessions_cleaned", extra={"event": "sessions_cleaned", "deleted": deleted})
    return deleted
