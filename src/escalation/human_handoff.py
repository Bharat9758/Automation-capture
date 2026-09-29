"""Coordinate an explicit human handoff on the original Selenium session."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.remote.webdriver import WebDriver

from src.agent.actor import Actor
from src.artifact.schema import AutomationArtifact
from src.escalation.escalation_request import EscalationRequest
from src.logging import get_logger
from src.safety.risk_classifier import RiskApproval, RiskLevel
from src.replay.locator_strategy import LocatorResolver


LOGGER = get_logger(__name__)
Control = Literal["paused", "human", "automation"]
ActionName = Literal["click", "type", "navigate", "wait", "read"]


def _now() -> datetime:
    """Get a timezone-aware UTC instant.

    Returns:
        Current UTC datetime.
    """
    return datetime.now(timezone.utc)


def _parse_time(value: str) -> datetime:
    """Require an ISO timestamp with an offset.

    Args:
        value: ISO 8601 timestamp.

    Returns:
        Aware datetime.

    Raises:
        ValueError: If the timestamp is invalid or naive.
    """
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp needs a timezone")
    return parsed


@dataclass(frozen=True, kw_only=True)
class HumanAction:
    """One operator action, recorded without typed input or extracted text."""

    action: ActionName
    locator: dict[str, Any] | None
    value: str | None
    reasoning: str
    timestamp: str
    operator_id: str

    def __post_init__(self) -> None:
        """Validate operator identity, timestamp, and action arguments."""
        if self.action not in {"click", "type", "navigate", "wait", "read"}:
            raise ValueError("Unsupported operator action")
        if not self.operator_id or not self.reasoning:
            raise ValueError("Operator identity and reasoning are required")
        _parse_time(self.timestamp)
        if self.action in {"click", "type", "read"} and not isinstance(self.locator, dict):
            raise ValueError(f"{self.action} requires a locator")
        if self.action in {"type", "navigate"} and (not isinstance(self.value, str) or not self.value):
            raise ValueError(f"{self.action} requires a value")


@dataclass(kw_only=True)
class HandoffSession:
    """Control and audit metadata bound to one live WebDriver instance."""

    session_id: str
    escalation_id: str
    driver: WebDriver | None = field(repr=False, compare=False)
    in_control: Control
    in_control_since: datetime
    human_actions: list[HumanAction]
    automation_can_resume: bool
    started_at: datetime
    paused_at: datetime
    resume_step: int = 0
    paused_step: int = 0
    artifact_id: str = ""
    lifecycle_id: str = ""
    human_control_started_at: datetime | None = None
    risk_approvals: list[RiskApproval] = field(default_factory=list)


def _require_driver(session: HandoffSession) -> WebDriver:
    """Ensure persisted sessions have been reattached to their original browser.

    Args:
        session: Current handoff state.

    Returns:
        Live WebDriver.

    Raises:
        RuntimeError: If no browser is attached or the ID differs.
    """
    driver = session.driver
    if driver is None or getattr(driver, "session_id", None) != session.session_id:
        raise RuntimeError("Original WebDriver session is unavailable or has changed")
    return driver


class SessionManager:
    """Allow only explicit, ordered control transitions."""

    def record_operator_actions(self, session: HandoffSession, actions: list[HumanAction]) -> HandoffSession:
        """Record completed actions on the human-controlled session.

        Args:
            session: Current live handoff.
            actions: Successfully performed human actions.

        Returns:
            Session with a timestamp-ordered audit trail.
        """
        return record_human_actions(session, actions)

    def create_handoff_session(
        self, driver: WebDriver, escalation_id: str, session_id: str,
        *, resume_step: int = 0, artifact_id: str = "",
    ) -> HandoffSession:
        """Pause the supplied live browser without creating a new one.

        Args:
            driver: Original browser.
            escalation_id: Persisted escalation ID.
            session_id: WebDriver session ID.
            resume_step: First pending artifact step (one-based, zero for start).
            artifact_id: Artifact bound to this pause.

        Returns:
            A paused handoff session.

        Raises:
            ValueError: If an ID or resume step is invalid.
        """
        if not session_id or not escalation_id or getattr(driver, "session_id", None) != session_id:
            raise ValueError("A live browser with the matching session ID is required")
        if isinstance(resume_step, bool) or not isinstance(resume_step, int) or resume_step < 0:
            raise ValueError("resume_step must be a nonnegative integer")
        now = _now()
        session = HandoffSession(session_id=session_id, escalation_id=escalation_id, driver=driver,
                                 in_control="paused", in_control_since=now, human_actions=[],
                                 automation_can_resume=False, started_at=now, paused_at=now,
                                 resume_step=resume_step, paused_step=resume_step, artifact_id=artifact_id)
        LOGGER.info("handoff_created", extra={"event": "handoff_created", "escalation_id": escalation_id})
        return session

    def give_control_to_human(self, session: HandoffSession) -> HandoffSession:
        """Transfer a paused live browser to an operator.

        Args:
            session: Paused handoff.

        Returns:
            Same session with human control.
        """
        _require_driver(session)
        if session.in_control != "paused":
            raise RuntimeError("Only a paused session can be handed to a human")
        session.in_control, session.in_control_since = "human", _now()
        session.human_control_started_at = session.in_control_since
        LOGGER.info("handoff_human_control", extra={"event": "handoff_human_control", "escalation_id": session.escalation_id})
        return session

    def approve_resume(self, session: HandoffSession, *, resume_step: int | None = None) -> HandoffSession:
        """Record the human's explicit decision about where replay can continue.

        Args:
            session: Human-controlled live browser.
            resume_step: First pending step; must not precede the pause.

        Returns:
            Session approved for automation control.
        """
        _require_driver(session)
        if session.in_control != "human":
            raise RuntimeError("Only the controlling human can authorize replay")
        requested = session.resume_step if resume_step is None else resume_step
        if isinstance(requested, bool) or not isinstance(requested, int) or requested < session.resume_step:
            raise ValueError("resume_step must be at or after the paused step")
        session.resume_step = requested
        session.automation_can_resume = True
        LOGGER.info("handoff_resume_approved", extra={"event": "handoff_resume_approved", "escalation_id": session.escalation_id, "resume_step": requested})
        return session

    def approve_risk_action(self, session: HandoffSession, approval: RiskApproval) -> HandoffSession:
        """Bind an escalated step approval to the current human-owned handoff.

        Args:
            session: Paused original browser currently controlled by a human.
            approval: Explicit approval of the paused step.

        Returns:
            Same session with the approval recorded.

        Raises:
            RuntimeError: Human does not control the live session.
            ValueError: The approval does not match this pause.
        """
        _require_driver(session)
        if session.in_control != "human":
            raise RuntimeError("Only the controlling human can approve an escalated step")
        if (not isinstance(approval, RiskApproval) or approval.step_number != session.paused_step
                or approval.risk_level < RiskLevel.RISKY or approval.approval_method != "human_interactive"
                or _parse_time(approval.approved_at) < session.paused_at):
            raise ValueError("Risk approval must match the current paused step and handoff")
        if any(item.step_number == approval.step_number for item in session.risk_approvals):
            raise ValueError("Critical step already has an approval")
        session.risk_approvals.append(approval)
        LOGGER.info("handoff_risk_approved", extra={"event": "handoff_risk_approved", "step": approval.step_number,
                                                  "escalation_id": session.escalation_id,
                                                  "operator_id": approval.approved_by})
        return session

    def give_control_to_automation(self, session: HandoffSession) -> HandoffSession:
        """Return control only after explicit human approval.

        Args:
            session: Human-controlled and approved session.

        Returns:
            Automation-controlled session.
        """
        _require_driver(session)
        if session.in_control != "human" or not session.automation_can_resume:
            raise RuntimeError("Human approval is required to resume automation")
        session.in_control, session.in_control_since = "automation", _now()
        LOGGER.info("handoff_automation_control", extra={"event": "handoff_automation_control", "escalation_id": session.escalation_id})
        return session

    def get_session_status(self, session: HandoffSession) -> dict[str, Any]:
        """Expose timing and action counts without typed values.

        Args:
            session: Tracked handoff.

        Returns:
            Safe status record.
        """
        return {"session_id": session.session_id, "escalation_id": session.escalation_id,
                "in_control": session.in_control,
                "control_seconds": max(0.0, (_now() - session.in_control_since).total_seconds()),
                "actions_taken": len(session.human_actions),
                "automation_can_resume": session.automation_can_resume, "resume_step": session.resume_step}

    def pause_session(self, session: HandoffSession) -> HandoffSession:
        """Stop replay while preserving its original browser.

        Args:
            session: Active session.

        Returns:
            Paused session.
        """
        _require_driver(session)
        now = _now()
        session.in_control, session.in_control_since, session.paused_at = "paused", now, now
        session.automation_can_resume = False
        return session

    def resume_session(self, session: HandoffSession) -> HandoffSession:
        """Resume only a human-approved paused session on the same driver.

        Args:
            session: Paused, approved session.

        Returns:
            Session owned by automation.
        """
        _require_driver(session)
        if session.in_control != "paused" or not session.automation_can_resume:
            raise RuntimeError("Paused session lacks an explicit resume approval")
        session.in_control, session.in_control_since = "automation", _now()
        return session


def record_human_actions(session: HandoffSession, actions: list[HumanAction]) -> HandoffSession:
    """Audit completed operator actions while the human owns the browser.

    Args:
        session: Human-controlled session.
        actions: Completed actions with operator identity and timestamps.

    Returns:
        Updated session sorted by timestamp.

    Raises:
        RuntimeError: If the operator does not own the live session.
    """
    _require_driver(session)
    if session.in_control != "human":
        raise RuntimeError("Human actions require human control")
    if not isinstance(actions, list) or any(not isinstance(action, HumanAction) for action in actions):
        raise TypeError("actions must be a list of HumanAction")
    session.human_actions = sorted([*session.human_actions, *actions], key=lambda item: _parse_time(item.timestamp))
    LOGGER.info("handoff_actions_recorded", extra={"event": "handoff_actions_recorded", "escalation_id": session.escalation_id, "count": len(actions)})
    return session


class MockOperatorInterface:
    """Run explicitly supplied demo actions on the same WebDriver."""

    def __init__(self, operator_id: str = "demo_operator", actions: list[HumanAction] | None = None) -> None:
        """Configure mock operator actions; the default performs no actions.

        Args:
            operator_id: Audit identity for demo actions.
            actions: Actions explicitly supplied by the caller.
        """
        if not operator_id:
            raise ValueError("operator_id is required")
        self.operator_id = operator_id
        self._actions = list(actions or [])
        self._resume_signaled = False

    def get_operator_actions(self) -> list[HumanAction]:
        """Return independent copies of pending demo actions.

        Returns:
            Configured actions, with no implicit clicks.
        """
        return list(self._actions)

    def display_escalation_info(self, request: EscalationRequest) -> None:
        """Show a sanitized console summary without printing the raw DOM.

        Args:
            request: Current escalation request.
        """
        context = request.stuck_state.escalation_context
        print(f"Escalation: {request.escalation_id}\nGoal: {request.goal}\n"
              f"Step: {request.current_step}\nReason: {request.reason_escalated}\n"
              f"Recommendation: {request.stuck_state.recommended_action}\n"
              f"Screenshot: [base64 image, {len(request.screenshot)} chars]\n"
              f"Available elements: {context.get('available_elements', [])}")

    def signal_resume(self) -> bool:
        """Report an explicit operator resume signal.

        Returns:
            Whether the operator has signaled completion.
        """
        return self._resume_signaled

    def confirm_resume(self) -> None:
        """Record the demo operator's explicit completion signal."""
        self._resume_signaled = True

    def record_operator_action(self, action: HumanAction, handoff_session: HandoffSession) -> HandoffSession:
        """Append one completed action to the audit trail.

        Args:
            action: Successfully performed action.
            handoff_session: Human-controlled session.

        Returns:
            Updated session.
        """
        return record_human_actions(handoff_session, [action])

    def take_control(
        self, driver: WebDriver, escalation_request: EscalationRequest,
        handoff_session: HandoffSession,
    ) -> HandoffSession:
        """Execute configured actions only after control was granted.

        Args:
            driver: Exact WebDriver from the handoff session.
            escalation_request: Escalation presented to the operator.
            handoff_session: Session with human control.

        Returns:
            Human-controlled session with recorded successful actions.

        Raises:
            RuntimeError: If ownership, identity, or an action fails.
        """
        if driver is not _require_driver(handoff_session) or handoff_session.in_control != "human":
            raise RuntimeError("Operator does not control this browser")
        if escalation_request.escalation_id != handoff_session.escalation_id:
            raise RuntimeError("Escalation ID does not match session")
        self.display_escalation_info(escalation_request)
        actor = Actor(driver)
        for action in self.get_operator_actions():
            if action.operator_id != self.operator_id:
                raise RuntimeError("Action operator ID does not match the controlling operator")
            try:
                if action.action == "navigate":
                    outcome = actor.navigate(action.value or "")
                    success = bool(outcome["success"])
                elif action.action == "wait":
                    if action.locator:
                        locator = action.locator
                        outcome = actor.wait_for_element(locator["value"], locator_type=locator["strategy"])
                        success = bool(outcome["success"])
                    else:
                        # A wait without a locator is a no-op: no arbitrary sleep.
                        success = True
                else:
                    if not action.locator:
                        raise ValueError("Action requires a locator")
                    selector = action.locator["value"]
                    strategy = action.locator["strategy"]
                    if action.action == "click":
                        success = bool(actor.click(selector, locator_type=strategy)["success"])
                    elif action.action == "type":
                        success = bool(actor.type(selector, action.value or "", locator_type=strategy)["success"])
                    else:
                        _ = LocatorResolver().resolve(driver, action.locator).text
                        success = True  # Read result omitted from audit.
            except (KeyError, ValueError, TypeError, WebDriverException) as exc:
                raise RuntimeError(f"Operator action failed: {type(exc).__name__}") from exc
            if not success:
                raise RuntimeError(f"Operator action failed: {action.action}")
            self.record_operator_action(action, handoff_session)
        return handoff_session


def get_human_actions_summary(actions: list[HumanAction]) -> str:
    """Summarize audited actions without typed values or page text.

    Args:
        actions: Recorded human actions.

    Returns:
        Readable action list.
    """
    return "\n".join(f"Human {index}: {action.action} by {action.operator_id}"
                     for index, action in enumerate(actions, start=1))


def merge_human_actions_into_artifact(
    artifact: AutomationArtifact, human_actions: list[HumanAction],
    *, at_step: int = 0, reason: str = "Human intervention",
) -> AutomationArtifact:
    """Return a new validated artifact containing redacted intervention audit.

    Args:
        artifact: Original artifact, never mutated.
        human_actions: Recorded actions.
        at_step: Step where automation paused.
        reason: Reason shown to the operator.

    Returns:
        New artifact with an intervention entry.
    """
    if any(not isinstance(item, HumanAction) for item in human_actions):
        raise TypeError("human_actions must contain HumanAction values")
    if isinstance(at_step, bool) or not isinstance(at_step, int) or at_step < 0:
        raise ValueError("at_step must be nonnegative")
    data = artifact.to_dict()
    current_updated = _parse_time(data["updated_at"])
    data["updated_at"] = max(_now(), current_updated).isoformat()
    actions = [{"action": item.action, "locator": item.locator,
                "value": "***REDACTED***" if item.action == "type" else item.value,
                "reasoning": item.reasoning, "timestamp": item.timestamp,
                "operator_id": item.operator_id} for item in human_actions]
    data.setdefault("human_interventions", []).append({"at_step": at_step, "reason": reason,
                                                      "human_actions": actions,
                                                      "timestamp": _now().isoformat(),
                                                      "operator_id": human_actions[0].operator_id if human_actions else "unavailable"})
    return AutomationArtifact.from_dict(data)


def save_handoff_session(session: HandoffSession, filepath: str) -> str:
    """Persist audit metadata atomically, excluding live driver and typed values.

    Args:
        session: Session state to save.
        filepath: JSON destination.

    Returns:
        Destination filepath.
    """
    payload = {"session_id": session.session_id, "escalation_id": session.escalation_id,
               "in_control": session.in_control, "in_control_since": session.in_control_since.isoformat(),
               "started_at": session.started_at.isoformat(), "paused_at": session.paused_at.isoformat(),
               "automation_can_resume": session.automation_can_resume,
               "resume_step": session.resume_step, "paused_step": session.paused_step,
               "artifact_id": session.artifact_id, "lifecycle_id": session.lifecycle_id,
               "human_control_started_at": session.human_control_started_at.isoformat() if session.human_control_started_at else None,
               "human_actions": [{**asdict(item), "value": "***REDACTED***" if item.action == "type" else item.value}
                                 for item in session.human_actions],
               "risk_approvals": [{**asdict(item), "risk_level": item.risk_level.name.lower()}
                                  for item in session.risk_approvals]}
    target = Path(filepath)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix=f".{target.name}.", delete=False) as handle:
            temp = handle.name
            os.chmod(temp, 0o600)
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, target)
    finally:
        if temp and os.path.exists(temp):
            os.unlink(temp)
    LOGGER.info("handoff_saved", extra={"event": "handoff_saved", "escalation_id": session.escalation_id})
    return filepath


def load_handoff_session(filepath: str, driver: WebDriver | None = None) -> HandoffSession:
    """Load metadata; require the original driver before any control transfer.

    A driver cannot be serialized or restored from a browser session ID. Without
    ``driver``, the returned object is detached and cannot operate or resume.

    Args:
        filepath: Existing handoff JSON.
        driver: Optional original, still-live WebDriver to reattach.

    Returns:
        Handoff session, detached when driver is omitted.

    Raises:
        ValueError: If the JSON is invalid or an attached driver differs.
    """
    try:
        data = json.loads(Path(filepath).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("Handoff must be a JSON object")
        if driver is not None and getattr(driver, "session_id", None) != data["session_id"]:
            raise ValueError("WebDriver session ID mismatch")
        actions = [HumanAction(**action) for action in data["human_actions"]]
        approvals = [RiskApproval(**{**item, "risk_level": RiskLevel[item["risk_level"].upper()]})
                     for item in data.get("risk_approvals", [])]
        session = HandoffSession(session_id=data["session_id"], escalation_id=data["escalation_id"],
                                 driver=driver, in_control=data["in_control"],
                                 in_control_since=_parse_time(data["in_control_since"]),
                                 human_actions=actions, risk_approvals=approvals,
                                 automation_can_resume=data["automation_can_resume"],
                                 started_at=_parse_time(data["started_at"]), paused_at=_parse_time(data["paused_at"]),
                                 resume_step=data["resume_step"], paused_step=data["paused_step"],
                                 artifact_id=data["artifact_id"], lifecycle_id=data.get("lifecycle_id", ""),
                                 human_control_started_at=_parse_time(data["human_control_started_at"]) if data.get("human_control_started_at") else None)
        if session.in_control not in {"paused", "human", "automation"} or not isinstance(session.automation_can_resume, bool):
            raise ValueError("Invalid handoff control state")
        if (not isinstance(session.session_id, str) or not session.session_id
                or not isinstance(session.escalation_id, str) or not session.escalation_id
                or isinstance(session.resume_step, bool) or not isinstance(session.resume_step, int)
                or isinstance(session.paused_step, bool) or not isinstance(session.paused_step, int)
                or session.paused_step < 0 or session.resume_step < session.paused_step):
            raise ValueError("Invalid handoff identifiers or replay step")
        if any(approval.step_number != session.paused_step or approval.risk_level < RiskLevel.RISKY
               or approval.approval_method != "human_interactive" or
               _parse_time(approval.approved_at) < session.paused_at for approval in session.risk_approvals):
            raise ValueError("Invalid risk approval for handoff")
        return session
    except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
        raise ValueError(f"Invalid handoff JSON: {type(exc).__name__}") from exc
