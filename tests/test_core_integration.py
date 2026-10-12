"""Cross-module tests for discovery, persistence, replay, and checkpoints."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from selenium.common.exceptions import NoSuchElementException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webelement import WebElement

from src.agent import llm_agent
from src.artifact.schema import AutomationArtifact, Locator
from src.artifact.serializer import load_artifact_from_file, save_artifact_to_file
from src.logging.structured_logger import EventType, load_logs_from_file
from src.replay.locator_strategy import LocatorResolver
from src.replay.replay_engine import replay_artifact
from tests.test_agent import FakeClient, FakeDriver
from tests.test_replay_engine import artifact, driver


def test_discovery_to_replay_flow(
    driver: Mock, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real agent recorder and serializer produce a replayable artifact."""
    monkeypatch.setenv("LLM_MODEL", "test-model")
    source = FakeDriver()
    source.element.text = "$5,000.00"
    client = FakeClient([
        {"action": "type", "selector": "member-id", "locator_type": "id", "value": "12345"},
        {"action": "read_text", "selector": "balance", "locator_type": "id"},
        {"action": "done", "goal_met": True},
    ])
    monkeypatch.setattr(llm_agent, "Anthropic", lambda **_kwargs: client)
    trace = llm_agent.run_goal_driven_loop(
        source, "Read member savings balance", "https://banking.example.com/app",
        api_key="test-key",
    )
    assert trace["success"] and trace["artifact_error"] is None
    recorded = AutomationArtifact.from_dict(trace["artifact"])
    path = tmp_path / "artifacts" / "discovered.json"
    save_artifact_to_file(recorded, str(path))
    rules_path = Path(os.environ["ALLOWLIST_PATH"])
    rules = json.loads(rules_path.read_text(encoding="utf-8"))
    for page in rules["pages"]:
        for element in page["allowed_elements"]:
            if element["element_id"] in {"member-id", "balance"}:
                element["locator"]["fallbacks"] = [
                    {"strategy": "css", "value": f"#{element['element_id']}"}
                ]
    rules_path.write_text(json.dumps(rules), encoding="utf-8")

    replayed = replay_artifact(driver, load_artifact_from_file(str(path)), {"member_id": "12345"})

    assert replayed.success and replayed.outputs == {"balance": 5000}
    assert recorded.steps[0].value == "{member_id}"
    assert "12345" not in path.read_text(encoding="utf-8")
    assert len(client.requests) == 3
    assert any(call.args == (By.ID, "member-id") for call in driver.find_element.call_args_list)


def test_business_outcome_detected(driver: Mock, artifact: AutomationArtifact) -> None:
    """A known negative result stops extraction without creating a handoff."""
    payload = artifact.to_dict()
    payload["known_errors"] = {"member_not_found": {
        "detection": {"type": "text_contains", "locator": {"strategy": "css", "value": ".error-message"},
                      "expected_text": "No such member"},
        "classification": "expected_business_outcome", "business_outcome": "member_not_found",
    }}
    original = driver.find_element.side_effect

    def find_result(by: str, selector: str) -> Mock:
        """Expose the observed business banner after a missing result."""
        if (by, selector) == (By.CSS_SELECTOR, ".error-message"):
            element = Mock(spec=WebElement)
            element.is_displayed.return_value = True
            element.text = "No such member"
            return element
        if (by, selector) == (By.ID, "balance"):
            raise NoSuchElementException("not present")
        return original(by, selector)

    driver.find_element.side_effect = find_result
    result = replay_artifact(driver, AutomationArtifact.from_dict(payload), {"member_id": "00000"}, max_wait_ms=30)
    events = load_logs_from_file(result.audit_log_path)

    assert result.status == "business_outcome" and result.business_outcome == "member_not_found"
    assert result.outputs == {} and result.escalation_request is None
    assert EventType.BUSINESS_OUTCOME in {event.event_type for event in events}
    assert EventType.ESCALATION_TRIGGERED not in {event.event_type for event in events}


def test_checkpoint_gates_output(driver: Mock, artifact: AutomationArtifact) -> None:
    """Even completed actions cannot extract outputs before final verification."""
    payload = artifact.to_dict()
    payload["success_checkpoint"] = {
        "condition": "text_contains", "locator": None,
        "expected_value": "approved result", "error_message": "Missing result text",
    }
    with patch("src.replay.replay_engine.extract_output") as extraction:
        result = replay_artifact(driver, AutomationArtifact.from_dict(payload), {"member_id": "12345"},
                                 max_wait_ms=30)
    assert result.status == "hard_failure" and result.outputs == {}
    extraction.assert_not_called()
    events = load_logs_from_file(result.audit_log_path)
    assert EventType.CHECKPOINT_FAILED in {event.event_type for event in events}
    assert EventType.OUTPUT_EXTRACTED not in {event.event_type for event in events}


@pytest.mark.parametrize(("strategy", "selector", "by"), [
    ("css", "#save", By.CSS_SELECTOR),
    ("xpath", "//button[@id='save']", By.XPATH),
    ("id", "save", By.ID),
    ("text", "Save", By.XPATH),
    ("aria_label", "Save", By.XPATH),
])
def test_all_locator_strategies(strategy: str, selector: str, by: str) -> None:
    """Every schema strategy reaches the corresponding Selenium lookup."""
    browser = Mock()
    element = Mock(spec=WebElement)
    element.is_displayed.return_value = True
    browser.find_element.return_value = element
    locator = Locator(strategy=strategy, value=selector, robustness_notes="Reviewed in integration test")
    assert LocatorResolver(wait_timeout=1).resolve(browser, locator) is element
    assert browser.find_element.call_args.args[0] == by


@pytest.mark.parametrize("member_id", ["12345", "54321"])
def test_parameter_substitution(driver: Mock, artifact: AutomationArtifact, member_id: str) -> None:
    """Distinct replay inputs reach Selenium without mutating the saved template."""
    original = driver.find_element.side_effect
    input_field = Mock(spec=WebElement)
    input_field.is_displayed.return_value = True

    def capture_input(by: str, selector: str) -> Mock:
        """Provide one observable input while preserving other browser lookups."""
        return input_field if (by, selector) == (By.ID, "member-id") else original(by, selector)

    driver.find_element.side_effect = capture_input
    result = replay_artifact(driver, artifact, {"member_id": member_id})
    assert result.success and result.outputs["savings_balance"] == 5000
    input_field.send_keys.assert_called_once_with(member_id)
    assert artifact.steps[1].value == "{member_id}"


def test_discovery_navigation_denied_before_api_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unapproved start page cannot be smuggled into the discovery trace."""
    monkeypatch.setenv("ALLOWED_DOMAINS", "banking.example.com")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    client = FakeClient([{"action": "done", "goal_met": True}])
    monkeypatch.setattr(llm_agent, "Anthropic", lambda **_kwargs: client)
    result = llm_agent.run_goal_driven_loop(
        FakeDriver(), "Read balance", "https://unlisted.example/app", api_key="test-key",
    )
    assert result["success"] is False
    assert client.requests == [] and result["steps"] == []
