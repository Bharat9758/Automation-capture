"""Lifecycle tracking, persistence, and retention tests with a browser double."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock

import pytest
from selenium.webdriver.remote.webdriver import WebDriver

from src.escalation.escalation_request import create_escalation_request
from src.escalation.session_manager import (
    SessionLifecycle,
    SessionLifecycleManager,
    cleanup_old_sessions,
    list_sessions,
    load_session_metadata,
    save_session_metadata,
)
from src.escalation.stuck_detector import StuckState
from src.safety.risk_classifier import ActionRiskAssessment, RiskLevel


@pytest.fixture
def driver() -> Mock:
    """Supply a browser with an existing, stable Selenium session ID.

    Returns:
        Browser double.
    """
    browser = Mock(spec=WebDriver)
    browser.session_id = "webdriver-live"
    return browser


@pytest.fixture
def manager() -> SessionLifecycleManager:
    """Provide an independent lifecycle manager.

    Returns:
        Session manager.
    """
    return SessionLifecycleManager()


def _escalation() -> object:
    """Build a validated request linked to the fixture browser.

    Returns:
        Escalation for one paused run.
    """
    return create_escalation_request(
        "sample", "discovery", StuckState(is_stuck=True, current_step=2, reason="Needs review",
                                            recommended_action="Check the page", current_screenshot=b"cG5n",
                                            current_dom="<html>Search</html>"),
        "webdriver-live", "Search", {"action": "click"}, [], {"member_id": "private"},
    )


def test_create_start_pause_human_resume_complete(manager: SessionLifecycleManager, driver: Mock) -> None:
    """A recovered run records transitions and metrics without raw inputs."""
    session = manager.create_session("sample", driver, {"member_id": "private"})
    assert uuid.UUID(session.session_id).version == 4
    assert session.driver_instance is driver and session.webdriver_session_id == driver.session_id
    assert session.lifecycle_state == SessionLifecycle.CREATED and session.input_names == ["member_id"]
    assert "private" not in str(vars(session))
    manager.start_session(session)
    manager.record_risk_assessment(session, 1, ActionRiskAssessment(
        risk_level=RiskLevel.CAUTION, reasoning="Data entry", evidence=["data entry action"],
        recommended_action="Log carefully", requires_approval=False, escalation_threshold=False))
    manager.track_step_execution(session, 1, True, 125)
    escalation = _escalation()
    manager.pause_session(session, escalation)  # type: ignore[arg-type]
    manager.add_escalation_to_session(session, escalation)  # type: ignore[arg-type]
    assert len(session.escalations) == 1
    manager.give_control_to_human(session)
    manager.resume_session(session, human_actions=2)
    manager.track_step_execution(session, 2, True, 300)
    manager.complete_session(session, {"balance": 100})
    metrics = manager.calculate_session_metrics(session)
    assert session.lifecycle_state == SessionLifecycle.COMPLETED
    assert (metrics["automation_steps"], metrics["successful_steps"], metrics["failed_steps"]) == (2, 2, 0)
    assert metrics["escalations"] == 1 and metrics["human_interventions"] == 2
    assert metrics["total_duration_seconds"] >= 0 and metrics["reuse_ready"] is False
    assert manager.is_artifact_improvement_candidate(session)
    assert manager.get_session_summary(session)["outcome"] == "success"
    assert manager.get_full_session_report(session)["outputs"] == {"balance": "***REDACTED***"}
    assert manager.get_full_session_report(session)["risk_assessments"][0]["risk_level"] == "caution"
    assert manager.get_session_escalation_summary(session)["successful_recovery"] is True
    assert manager.get_escalations_for_session(session) == [escalation]


def test_invalid_transitions_and_browser_identity(manager: SessionLifecycleManager, driver: Mock) -> None:
    """Terminal states and a replaced browser reject further automation."""
    session = manager.create_session("sample", driver, {})
    with pytest.raises(RuntimeError, match="running automation"):
        manager.track_step_execution(session, 1, True, 20)
    with pytest.raises(RuntimeError, match="transition"):
        manager.resume_session(session)
    manager.start_session(session)
    with pytest.raises(ValueError, match="Invalid step"):
        manager.track_step_execution(session, 1, True, -1)
    driver.session_id = "replaced"
    with pytest.raises(RuntimeError, match="unavailable"):
        manager.pause_session(session, _escalation())  # type: ignore[arg-type]
    driver.session_id = "webdriver-live"
    manager.fail_session(session, "Browser unavailable")
    assert session.completed_at and session.error_reason == "Browser unavailable"
    assert manager.get_session_summary(session)["state"] == "failed"
    with pytest.raises(RuntimeError, match="transition"):
        manager.start_session(session)


def test_abandoned_and_business_outcome(manager: SessionLifecycleManager, driver: Mock) -> None:
    """Cancellation and legitimate negative results get distinct terminal outcomes."""
    cancelled = manager.create_session("sample", driver, {})
    manager.abandon_session(cancelled)
    assert cancelled.lifecycle_state == SessionLifecycle.ABANDONED
    result = manager.create_session("sample", driver, {})
    manager.start_session(result)
    manager.track_step_execution(result, 1, False, 5)
    manager.complete_session(result, outcome="member_not_found")
    assert manager.get_session_summary(result)["outcome"] == "member_not_found"
    assert manager.is_artifact_improvement_candidate(result)


def test_metadata_roundtrip_listing_and_detached_browser(
    manager: SessionLifecycleManager, driver: Mock, tmp_path: Path,
) -> None:
    """Private JSON audits list newest first and cannot revive a browser."""
    directory = tmp_path / "evidence" / "sessions"
    first = manager.create_session("sample", driver, {"member_id": "secret"})
    manager.start_session(first)
    manager.pause_session(first, _escalation())  # type: ignore[arg-type]
    path = directory / f"{first.session_id}.json"
    assert list_sessions(str(directory)) == []
    assert save_session_metadata(first, str(path)) == str(path)
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert "secret" not in path.read_text(encoding="utf-8")
    assert load_session_metadata(str(path)).driver_instance is None
    attached = load_session_metadata(str(path), driver)
    assert attached.driver_instance is driver
    assert attached.escalations[0].escalation_id == first.escalations[0].escalation_id
    assert attached.escalations[0].dom_snapshot == "<redacted>"
    assert first.escalations[0].dom_snapshot != attached.escalations[0].dom_snapshot
    with pytest.raises(ValueError, match="Invalid session metadata"):
        load_session_metadata(str(path), Mock(session_id="other"))
    second = manager.create_session("sample", driver, {})
    manager.start_session(second)
    save_session_metadata(second, str(directory / f"{second.session_id}.json"))
    assert list_sessions(str(directory))[0].session_id == second.session_id
    (directory / "broken.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid session metadata"):
        list_sessions(str(directory))


def test_cleanup_only_terminal_old_records(
    manager: SessionLifecycleManager, driver: Mock, tmp_path: Path,
) -> None:
    """Retention removes completed audits and keeps live paused sessions."""
    directory = tmp_path / "sessions"
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    done = manager.create_session("sample", driver, {})
    manager.start_session(done)
    manager.complete_session(done)
    done.created_at = old
    save_session_metadata(done, str(directory / f"{done.session_id}.json"))
    paused = manager.create_session("sample", driver, {})
    manager.start_session(paused)
    manager.pause_session(paused, _escalation())  # type: ignore[arg-type]
    paused.created_at = old
    save_session_metadata(paused, str(directory / f"{paused.session_id}.json"))
    recent = manager.create_session("sample", driver, {})
    manager.start_session(recent)
    manager.complete_session(recent)
    save_session_metadata(recent, str(directory / f"{recent.session_id}.json"))
    assert cleanup_old_sessions(str(directory), days=30) == 1
    assert {item.session_id for item in list_sessions(str(directory))} == {paused.session_id, recent.session_id}
    with pytest.raises(ValueError, match="days"):
        cleanup_old_sessions(str(directory), days=-1)


def test_invalid_json_fields_fail_closed(manager: SessionLifecycleManager, driver: Mock, tmp_path: Path) -> None:
    """Tampered state and missing fields cannot be silently loaded."""
    session = manager.create_session("sample", driver, {})
    path = tmp_path / "session.json"
    save_session_metadata(session, str(path))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["lifecycle_state"] = "unknown"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid session metadata"):
        load_session_metadata(str(path))


def test_cleanup_validates_all_records_before_removing_anything(
    manager: SessionLifecycleManager, driver: Mock, tmp_path: Path,
) -> None:
    """One corrupt audit blocks deletion of otherwise expired records."""
    old = manager.create_session("sample", driver, {})
    manager.start_session(old)
    manager.complete_session(old)
    old.created_at = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    directory = tmp_path / "sessions"
    old_path = directory / f"{old.session_id}.json"
    save_session_metadata(old, str(old_path))
    (directory / "broken.json").write_text("{", encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid session metadata"):
        cleanup_old_sessions(str(directory), days=30)
    assert old_path.exists()
