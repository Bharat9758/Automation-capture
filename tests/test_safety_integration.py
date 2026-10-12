"""Cross-module tests for allowlists, risk, privacy, and human handoff."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from selenium.webdriver.common.by import By

from src.artifact.schema import AutomationArtifact
from src.escalation.human_handoff import SessionManager
from src.escalation.session_manager import SessionLifecycle, load_session_metadata
from src.logging.structured_logger import EventType, load_logs_from_file
from src.replay.replay_engine import replay_artifact
from src.safety.allowlist import AllowlistViolation, enforce_allowlist, load_allowlist
from src.safety.risk_classifier import RiskApproval, RiskLevel
from tests.test_replay_engine import artifact, driver


def _changed_artifact(artifact: AutomationArtifact, *, reasoning: str | None = None,
                      locator: str | None = None) -> AutomationArtifact:
    """Change a reviewed click's intent or selector while preserving the schema.

    Args:
        artifact: Original recorded workflow.
        reasoning: Replacement intent.
        locator: Replacement target ID.

    Returns:
        New validated artifact.
    """
    payload = artifact.to_dict()
    if reasoning is not None:
        payload["steps"][2]["reasoning"] = reasoning
    if locator is not None:
        payload["steps"][2]["locator"]["value"] = locator
    return AutomationArtifact.from_dict(payload)


def test_allowlist_permits_reviewed_action(driver: Mock, artifact: AutomationArtifact) -> None:
    """A reviewed workflow still reaches the verified output."""
    result = replay_artifact(driver, artifact, {"member_id": "12345"})
    assert result.success and result.outputs == {"savings_balance": 5000}
    events = load_logs_from_file(result.audit_log_path)
    assert EventType.ALLOWLIST_VIOLATION not in {event.event_type for event in events}


def test_allowlist_blocks_forbidden_action(driver: Mock, artifact: AutomationArtifact) -> None:
    """Forbidden intent is rejected before the click and logged as a violation."""
    unsafe = _changed_artifact(artifact, reasoning="Delete this account")
    rules = load_allowlist(os.environ["ALLOWLIST_PATH"])
    with pytest.raises(AllowlistViolation):
        enforce_allowlist(driver, unsafe.steps[2], unsafe, rules)
    result = replay_artifact(driver, unsafe, {"member_id": "12345"})
    assert result.status == "hard_failure" and result.step_failed == 3
    assert not any(call.args == (By.ID, "search") for call in driver.find_element.call_args_list)
    assert EventType.ALLOWLIST_VIOLATION in {
        event.event_type for event in load_logs_from_file(result.audit_log_path)
    }


@pytest.mark.parametrize("unreviewed", ["unknown-button", "admin-link"])
def test_unreviewed_element_cannot_be_clicked(
    driver: Mock, artifact: AutomationArtifact, unreviewed: str,
) -> None:
    """An allowed page cannot authorize an unknown target by association."""
    changed = _changed_artifact(artifact, locator=unreviewed)
    result = replay_artifact(driver, changed, {"member_id": "12345"})
    assert result.status == "hard_failure" and result.outputs == {}
    assert not any(call.args == (By.ID, unreviewed) for call in driver.find_element.call_args_list)


def test_risky_action_warns_but_does_not_pause(driver: Mock, artifact: AutomationArtifact) -> None:
    """A reversible elevated action emits an audit warning and completes."""
    changed = _changed_artifact(artifact, reasoning="Confirm final search")
    result = replay_artifact(driver, changed, {"member_id": "12345"})
    kinds = {event.event_type for event in load_logs_from_file(result.audit_log_path)}
    assert result.success and result.escalation_request is None
    assert EventType.RISK_ELEVATED in kinds and EventType.ESCALATION_TRIGGERED not in kinds


def test_risk_critical_escalates(driver: Mock, artifact: AutomationArtifact) -> None:
    """A critical click pauses on the same driver before touching its target."""
    changed = _changed_artifact(artifact, reasoning="Confirm and close account permanently")
    result = replay_artifact(driver, changed, {"member_id": "12345"})
    kinds = {event.event_type for event in load_logs_from_file(result.audit_log_path)}
    assert result.status == "escalated" and result.risk_assessments[-1].risk_level == RiskLevel.CRITICAL
    assert result.handoff_session.driver is driver
    assert EventType.APPROVAL_REQUIRED in kinds and EventType.ESCALATION_TRIGGERED in kinds
    assert not any(call.args == (By.ID, "search") for call in driver.find_element.call_args_list)


def test_escalation_approval_resumes(
    driver: Mock, artifact: AutomationArtifact, tmp_path: Path,
) -> None:
    """A human-approved paused step resumes without a new browser session."""
    changed = _changed_artifact(artifact, reasoning="Confirm and close account permanently")
    paused = replay_artifact(driver, changed, {"member_id": "12345"})
    assert paused.status == "escalated"
    session = paused.handoff_session
    manager = SessionManager()
    manager.give_control_to_human(session)
    manager.approve_risk_action(session, RiskApproval(
        step_number=session.paused_step, risk_level=RiskLevel.CRITICAL,
        approved_by="reviewer", approved_at=datetime.now(timezone.utc).isoformat(),
        approval_method="human_interactive",
    ))
    manager.approve_resume(session, resume_step=session.paused_step)
    manager.give_control_to_automation(session)
    before = driver.get.call_count
    resumed = replay_artifact(driver, changed, {"member_id": "12345"}, handoff_session=session)
    assert resumed.success and driver.get.call_count == before
    assert resumed.session.session_id == paused.session.session_id
    assert resumed.session.lifecycle_state == SessionLifecycle.COMPLETED
    stored = load_session_metadata(str(tmp_path / "evidence" / "sessions" /
                                       f"{resumed.session.session_id}.json"))
    assert stored.risk_approvals[0]["approved_by"] == "reviewer"


def test_redaction_masks_pii_but_retains_private_evidence(
    driver: Mock, artifact: AutomationArtifact, tmp_path: Path,
) -> None:
    """Audit data is masked while operator evidence retains the raw page."""
    driver.page_source = "<html>Member 12345 secret account</html>"
    paused = replay_artifact(driver, artifact, {"member_id": "12345"}, request_human_help=True)
    assert paused.status == "escalated" and paused.escalation_request is not None
    session_path = tmp_path / "evidence" / "sessions" / f"{paused.session.session_id}.json"
    audit = Path(paused.audit_log_path).read_text(encoding="utf-8")
    assert "12345" not in audit and "12345" not in session_path.read_text(encoding="utf-8")
    assert "12345" in paused.escalation_request.dom_snapshot
    raw = tmp_path / "evidence" / "escalations" / "raw" / f"{paused.escalation_request.escalation_id}.json"
    assert "12345" in raw.read_text(encoding="utf-8")
    assert paused.escalation_request.input_params["member_id"] != "12345"


def test_configured_caution_threshold_requires_operator(
    driver: Mock, artifact: AutomationArtifact, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stricter Phase 21 policy requests approval even for a routine step."""
    monkeypatch.setenv("RISK_APPROVAL_REQUIRED", "CAUTION")
    paused = replay_artifact(driver, artifact, {"member_id": "12345"})
    assert paused.status == "escalated" and paused.handoff_session.paused_step == 1
    assert paused.risk_assessments[-1].risk_level >= RiskLevel.CAUTION
    session = paused.handoff_session
    manager = SessionManager()
    manager.give_control_to_human(session)
    manager.approve_risk_action(session, RiskApproval(
        step_number=1, risk_level=paused.risk_assessments[-1].risk_level,
        approved_by="reviewer", approved_at=datetime.now(timezone.utc).isoformat(),
        approval_method="human_interactive",
    ))
    manager.approve_resume(session, resume_step=1)
    manager.give_control_to_automation(session)
    resumed = replay_artifact(driver, artifact, {"member_id": "12345"}, handoff_session=session)
    assert resumed.status == "escalated" and resumed.handoff_session.paused_step > 1


@pytest.mark.parametrize("url", [
    "https://banking.example.com/admin",
    "https://unapproved.example/app",
])
def test_navigation_restrictions_hold_before_browser_access(
    driver: Mock, artifact: AutomationArtifact, url: str,
) -> None:
    """Neither a new path nor a new domain becomes authorized by an artifact."""
    payload = artifact.to_dict()
    payload["target_url"] = url
    result = replay_artifact(driver, AutomationArtifact.from_dict(payload), {"member_id": "12345"})
    assert result.status == "hard_failure" and result.outputs == {}
    driver.get.assert_not_called()
