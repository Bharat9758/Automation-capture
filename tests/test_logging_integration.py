"""Cross-module replay audit and browser-evidence integration tests."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from selenium.common.exceptions import NoSuchElementException, WebDriverException
from selenium.webdriver.common.by import By

from src.artifact.schema import AutomationArtifact
from src.logging import configure_logging, get_logger
from src.logging.evidence_capture import EvidenceCollector, EvidenceType
from src.logging.structured_logger import (
    EventType, StructuredLogger, get_log_statistics, load_logs_from_file, save_logs_to_file,
)
from src.replay.replay_engine import replay_artifact
from tests.test_replay_engine import artifact, driver


def _events(result: object) -> list[str]:
    """Read event types from a completed replay result.

    Args:
        result: Replay result with an audit path.

    Returns:
        Ordered event names.
    """
    return [event.event_type for event in load_logs_from_file(result.audit_log_path)]


def test_all_step_events_logged(driver: Mock, artifact: AutomationArtifact) -> None:
    """Navigation, each step, checkpoint, extraction, and completion persist."""
    driver.get_log.side_effect = WebDriverException("unsupported")
    result = replay_artifact(driver, artifact, {"member_id": "12345"})
    events = _events(result)
    assert events.count(EventType.STEP_EXECUTED) == len(artifact.steps) + 1
    assert EventType.CHECKPOINT_VERIFIED in events
    assert EventType.OUTPUT_EXTRACTED in events
    assert events.index(EventType.SESSION_COMPLETED) > events.index(EventType.CHECKPOINT_VERIFIED)
    assert all("12345" not in event.to_json() for event in load_logs_from_file(result.audit_log_path))


@pytest.mark.parametrize("capture_level", ["ALL", "ERRORS"])
def test_error_evidence_captured(
    driver: Mock, artifact: AutomationArtifact, capture_level: str,
) -> None:
    """An action failure stores a screenshot, DOM, traceback, and indexed paths."""
    original = driver.find_element.side_effect

    def missing_search(by: str, selector: str) -> Mock:
        """Raise a browser lookup error only for the pending click."""
        if (by, selector) == (By.ID, "search"):
            raise NoSuchElementException("Search button is absent")
        return original(by, selector)

    driver.find_element.side_effect = missing_search
    driver.get_log.side_effect = WebDriverException("unsupported")
    result = replay_artifact(driver, artifact, {"member_id": "12345"},
                             evidence_capture_level=capture_level, max_wait_ms=30)
    assert result.status == "hard_failure"
    index = json.loads(Path(result.evidence_manifest_path).read_text(encoding="utf-8"))
    kinds = {entry["evidence_type"] for entry in index["evidence"]}
    assert {EvidenceType.SCREENSHOT, EvidenceType.DOM_SNAPSHOT, EvidenceType.ERROR_CONTEXT} <= kinds
    root = Path(result.evidence_manifest_path).parent
    context = next(entry for entry in index["evidence"] if entry["evidence_type"] == EvidenceType.ERROR_CONTEXT)
    details = json.loads((root / context["relative_path"]).read_text(encoding="utf-8"))
    assert details["exception_type"] and Path(details["screenshot_path"]).is_file()
    assert Path(details["dom_path"]).is_file()
    assert EventType.ERROR_OCCURRED in _events(result)
    assert EventType.SESSION_FAILED in _events(result)


def test_escalation_evidence_and_manifest(
    driver: Mock, artifact: AutomationArtifact,
) -> None:
    """A human pause captures complete evidence and a content-free index."""
    driver.page_source = "<html>member 12345</html>"
    driver.get_log.return_value = [{"level": "INFO", "message": "demo"}]
    result = replay_artifact(driver, artifact, {"member_id": "12345"},
                             request_human_help=True, evidence_capture_level="ESCALATIONS_ONLY")
    assert result.status == "escalated" and result.escalation_request is not None
    manifest = result.evidence["capture_paths"]
    written = json.loads(Path(manifest["manifest_path"]).read_text(encoding="utf-8"))
    for key in ("screenshot", "dom", "metrics", "driver_logs"):
        assert Path(written["evidence_paths"][key]).is_file()
    assert "12345" not in Path(result.evidence_manifest_path).read_text(encoding="utf-8")
    assert "12345" in Path(written["evidence_paths"]["dom"]).read_text(encoding="utf-8")
    assert EventType.ESCALATION_TRIGGERED in _events(result)
    assert EventType.SESSION_PAUSED in _events(result)


def test_logs_persist_to_disk(tmp_path: Path) -> None:
    """Independent event buffers survive JSON persistence and restoration."""
    logger = StructuredLogger("session-log-test", "artifact-log-test", private_values=["member-12345"])
    logger.log_session_state(EventType.SESSION_STARTED.value, "running")
    logger.log_step_executed(1, "type", {"strategy": "id", "value": "member-12345"},
                             150, "CAUTION", True)
    logger.log_session_state(EventType.SESSION_COMPLETED.value, "completed", 0.15)
    path = tmp_path / "logs" / "audit.json"
    save_logs_to_file(logger, str(path))
    loaded = load_logs_from_file(str(path))
    assert [item.event_type for item in loaded] == [
        EventType.SESSION_STARTED, EventType.STEP_EXECUTED, EventType.SESSION_COMPLETED,
    ]
    assert "member-12345" not in path.read_text(encoding="utf-8")
    restored = StructuredLogger("session-log-test", "artifact-log-test")
    restored.restore_events(loaded)
    assert restored.get_events_json() == logger.get_events_json()


def test_analytics_summary_generated(driver: Mock, artifact: AutomationArtifact) -> None:
    """The persisted replay stream yields timing and final status analytics."""
    result = replay_artifact(driver, artifact, {"member_id": "12345"})
    statistics = get_log_statistics(load_logs_from_file(result.audit_log_path))
    assert statistics["final_status"] == "success"
    assert statistics["average_step_duration_ms"] >= 0
    assert statistics["escalations"] == 0 and statistics["errors"] == 0
    assert statistics["event_types"][EventType.STEP_EXECUTED] == len(artifact.steps) + 1


@pytest.mark.parametrize(("mode", "expected_metrics"), [("ALL", True), ("ERRORS", False)])
def test_evidence_level_controls_success_metrics(
    driver: Mock, artifact: AutomationArtifact, mode: str, expected_metrics: bool,
) -> None:
    """Normal replay collects every-step metrics only in ALL mode."""
    driver.get_log.side_effect = WebDriverException("unsupported")
    result = replay_artifact(driver, artifact, {"member_id": "12345"}, evidence_capture_level=mode)
    assert result.success
    if expected_metrics:
        index = json.loads(Path(result.evidence_manifest_path).read_text(encoding="utf-8"))
        assert EvidenceType.PERFORMANCE_METRICS in {item["evidence_type"] for item in index["evidence"]}
    else:
        assert result.evidence_manifest_path is None


def test_manifest_detects_tampering(driver: Mock, artifact: AutomationArtifact) -> None:
    """A captured artifact cannot be silently modified before investigation."""
    result = replay_artifact(driver, artifact, {"member_id": "12345"}, evidence_capture_level="ALL")
    root = Path(result.evidence_manifest_path).parent
    index = json.loads(Path(result.evidence_manifest_path).read_text(encoding="utf-8"))
    recorded = root / index["evidence"][0]["relative_path"]
    recorded.write_bytes(recorded.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="changed after capture"):
        EvidenceCollector(result.session.session_id, str(root.parent))


def test_json_log_file_redacts_structured_private_data(tmp_path: Path) -> None:
    """The process logger writes masked JSON lines without private values."""
    path = tmp_path / "audit.jsonl"
    try:
        configure_logging("INFO", log_file=str(path), quiet=True)
        get_logger("src.integration.audit").info(
            "Logged member 123-45-6789", extra={"member_id": "12345", "email": "alice@example.com"},
        )
    finally:
        configure_logging("INFO", quiet=False)
    body = path.read_text(encoding="utf-8")
    assert "123-45-6789" not in body and "alice@example.com" not in body
    assert json.loads(body.splitlines()[0])["levelname"] == "INFO"
