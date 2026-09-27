"""Human control and same-session replay contract tests."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from selenium.webdriver.remote.webdriver import WebDriver

from src.artifact.schema import AutomationArtifact
from src.escalation.human_handoff import (
    HandoffSession,
    HumanAction,
    MockOperatorInterface,
    SessionManager,
    get_human_actions_summary,
    load_handoff_session,
    merge_human_actions_into_artifact,
    record_human_actions,
    save_handoff_session,
)
from src.escalation.stuck_detector import StuckState
from src.escalation.escalation_request import create_escalation_request


@pytest.fixture
def driver() -> Mock:
    """Supply a live-looking Selenium session.

    Returns:
        Stable browser double.
    """
    browser = Mock(spec=WebDriver)
    browser.session_id = "browser-123"
    return browser


@pytest.fixture
def session(driver: Mock) -> HandoffSession:
    """Pause automation on the original browser.

    Args:
        driver: Existing WebDriver double.

    Returns:
        Paused session.
    """
    return SessionManager().create_handoff_session(driver, "escalation-123", driver.session_id,
                                                    resume_step=2, artifact_id="sample")


@pytest.fixture
def escalation() -> object:
    """Produce a valid escalation request for a demo operator.

    Returns:
        Request with captured browser evidence.
    """
    return create_escalation_request(
        "sample", "discovery", StuckState(is_stuck=True, current_step=2,
                                            reason="Human review", recommended_action="Click search",
                                            current_screenshot=b"cG5n", current_dom="<html>Search</html>",
                                            escalation_context={"available_elements": ["button: Search"]}),
        "browser-123", "Search records", {"action": "click"}, [], {},
    )


def _action(action: str, *, value: str | None = None, locator: dict[str, str] | None = None) -> HumanAction:
    """Build one auditable operator action.

    Args:
        action: Supported action name.
        value: Optional typed or navigated value.
        locator: Optional target selector.

    Returns:
        Valid action with UTC timestamp.
    """
    return HumanAction(action=action, locator=locator, value=value, reasoning="Operator decision",
                       timestamp=datetime.now(timezone.utc).isoformat(), operator_id="operator-1")


def test_paused_human_approved_automation_transition(session: HandoffSession, driver: Mock) -> None:
    """The original browser remains attached and unapproved resumes fail."""
    manager = SessionManager()
    assert session.driver is driver and session.in_control == "paused"
    with pytest.raises(RuntimeError, match="approval"):
        manager.resume_session(session)
    manager.give_control_to_human(session)
    with pytest.raises(RuntimeError, match="approval"):
        manager.give_control_to_automation(session)
    manager.approve_resume(session, resume_step=3)
    manager.give_control_to_automation(session)
    assert session.driver is driver and session.resume_step == 3
    assert manager.get_session_status(session)["in_control"] == "automation"
    assert manager.get_session_status(session)["control_seconds"] >= 0
    manager.pause_session(session)
    assert session.in_control == "paused" and not session.automation_can_resume


def test_invalid_control_transitions_and_mismatched_browser(session: HandoffSession, driver: Mock) -> None:
    """No operator can take over another WebDriver session or bypass pause."""
    manager = SessionManager()
    with pytest.raises(ValueError, match="matching session ID"):
        manager.create_handoff_session(driver, "other", "wrong")
    with pytest.raises(RuntimeError, match="human control"):
        record_human_actions(session, [_action("wait")])
    manager.give_control_to_human(session)
    with pytest.raises(RuntimeError, match="paused"):
        manager.give_control_to_human(session)
    with pytest.raises(ValueError, match="at or after"):
        manager.approve_resume(session, resume_step=1)
    driver.session_id = "replacement"
    with pytest.raises(RuntimeError, match="unavailable"):
        manager.approve_resume(session)


def test_operator_executes_only_configured_actions_and_audits(
    session: HandoffSession, driver: Mock, escalation: object, capsys: pytest.CaptureFixture[str],
) -> None:
    """The mock operator clicks on the same driver and never prints typed data."""
    from src.escalation.escalation_request import EscalationRequest

    assert isinstance(escalation, EscalationRequest)
    session.escalation_id = escalation.escalation_id
    SessionManager().give_control_to_human(session)
    actions = [_action("click", locator={"strategy": "css", "value": ".search"}), _action("wait")]
    operator = MockOperatorInterface(operator_id="operator-1", actions=actions)
    assert not operator.signal_resume()
    with patch("src.escalation.human_handoff.Actor.click", return_value={"success": True}) as click:
        returned = operator.take_control(driver, escalation, session)
    assert returned is session and session.in_control == "human"
    click.assert_called_once_with(".search", locator_type="css")
    assert len(session.human_actions) == 2
    assert "Escalation:" in capsys.readouterr().out
    assert "Screenshot: [base64 image" in operator_display(escalation, capsys)
    operator.confirm_resume()
    assert operator.signal_resume()
    SessionManager().approve_resume(session, resume_step=3)
    SessionManager().give_control_to_automation(session)
    assert session.driver is driver
    assert "click" in get_human_actions_summary(session.human_actions)


def operator_display(escalation: object, capsys: pytest.CaptureFixture[str]) -> str:
    """Capture the operator's evidence indicator.

    Args:
        escalation: Valid escalation request.
        capsys: Pytest stdout capture.

    Returns:
        Console output.
    """
    MockOperatorInterface().display_escalation_info(escalation)  # type: ignore[arg-type]
    return capsys.readouterr().out


def test_operator_failure_never_records_action(session: HandoffSession, driver: Mock, escalation: object) -> None:
    """A failed click cannot be audited as completed or signal resume."""
    session.escalation_id = escalation.escalation_id  # type: ignore[attr-defined]
    SessionManager().give_control_to_human(session)
    operator = MockOperatorInterface("operator-1", [_action("click", locator={"strategy": "css", "value": ".missing"})])
    with patch("src.escalation.human_handoff.Actor.click", return_value={"success": False}):
        with pytest.raises(RuntimeError, match="Operator action failed"):
            operator.take_control(driver, escalation, session)  # type: ignore[arg-type]
    assert session.human_actions == [] and not operator.signal_resume()


def test_operator_types_on_original_driver_and_audit_masks_value(
    session: HandoffSession, driver: Mock, escalation: object, tmp_path: Path,
) -> None:
    """Typing reaches Selenium, while persisted audit omits the entered value."""
    session.escalation_id = escalation.escalation_id  # type: ignore[attr-defined]
    SessionManager().give_control_to_human(session)
    action = _action("type", value="private-member", locator={"strategy": "id", "value": "member"})
    operator = MockOperatorInterface("operator-1", [action])
    with patch("src.escalation.human_handoff.Actor.type", return_value={"success": True}) as typed:
        operator.take_control(driver, escalation, session)  # type: ignore[arg-type]
    typed.assert_called_once_with("member", "private-member", locator_type="id")
    path = tmp_path / "handoff.json"
    save_handoff_session(session, str(path))
    assert "private-member" not in path.read_text(encoding="utf-8")
    assert session.human_actions[0].value == "private-member"


def test_record_actions_sorted_and_schema_merge(session: HandoffSession) -> None:
    """Audit actions are sorted, persisted without typed text, and merge into a copy."""
    manager = SessionManager()
    manager.give_control_to_human(session)
    later = _action("type", value="private-value", locator={"strategy": "id", "value": "member"})
    earlier = HumanAction(action="wait", locator=None, value=None, reasoning="Wait",
                          timestamp="2024-01-01T00:00:00Z", operator_id="operator-1")
    record_human_actions(session, [later, earlier])
    assert session.human_actions == [earlier, later]
    artifact = AutomationArtifact.from_dict({
        "id": "sample", "name": "Sample", "version": "1.0.0", "description": "Sample",
        "created_at": "2024-01-01T00:00:00Z", "updated_at": "2024-01-01T00:00:00Z",
        "created_by": "test", "target_url": "https://banking.example.com/app", "inputs": [], "outputs": [],
        "steps": [{"step_number": 1, "action": "click", "locator": {"strategy": "id", "value": "search", "robustness_notes": "ID"},
                   "value": None, "reasoning": "Search", "expected_outcome": "Results"}],
        "success_checkpoint": {"condition": "url_matches", "locator": None,
                               "expected_value": "/app", "error_message": "Wrong page"},
        "known_errors": {}, "discovery_run_id": "run-1", "success_rate": None,
    })
    updated = merge_human_actions_into_artifact(artifact, session.human_actions,
                                                 at_step=1, reason="Stuck search")
    assert artifact.human_interventions == []
    assert updated.human_interventions[0].at_step == 1
    assert "private-value" not in json.dumps(updated.to_dict())
    assert AutomationArtifact.from_dict(updated.to_dict()).human_interventions == updated.human_interventions


def test_save_load_requires_original_driver(tmp_path: Path, session: HandoffSession, driver: Mock) -> None:
    """Serialized metadata alone cannot recreate a logged-in browser."""
    manager = SessionManager()
    manager.give_control_to_human(session)
    record_human_actions(session, [_action("type", value="private-value", locator={"strategy": "id", "value": "member"})])
    path = tmp_path / "evidence" / "handoffs" / "session.json"
    save_handoff_session(session, str(path))
    assert "private-value" not in path.read_text(encoding="utf-8")
    assert path.stat().st_mode & 0o777 == 0o600
    detached = load_handoff_session(str(path))
    assert detached.driver is None
    with pytest.raises(RuntimeError, match="unavailable"):
        manager.approve_resume(detached)
    with pytest.raises(ValueError, match="mismatch"):
        load_handoff_session(str(path), Mock(session_id="other"))
    attached = load_handoff_session(str(path), driver)
    assert attached.driver is driver and attached.human_actions[0].value == "***REDACTED***"
    (tmp_path / "invalid.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid handoff JSON"):
        load_handoff_session(str(tmp_path / "invalid.json"))
