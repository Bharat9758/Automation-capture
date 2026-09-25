"""Check escalation decisions and the bounded browser evidence they carry."""

from __future__ import annotations

import base64
from unittest.mock import Mock

import pytest
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement

from src.artifact.schema import ActionStep, AutomationArtifact, Locator
from src.escalation.stuck_detector import (
    StateTracker,
    calculate_state_signature,
    capture_escalation_context,
    count_matching_elements,
    detect_stuck_state,
    get_step_context,
    is_risky_action,
    is_state_repeated,
    record_step_in_history,
)
from src.replay.locator_strategy import LocatorResolver


@pytest.fixture
def sample_artifact() -> AutomationArtifact:
    """Build a minimal artifact with a reversible and a sensitive action.

    Returns:
        Valid artifact for detector tests.
    """
    return AutomationArtifact.from_dict({
        "id": "sample", "name": "Sample", "version": "1.0.0", "description": "Sample replay",
        "created_at": "2026-09-24T00:00:00Z", "updated_at": "2026-09-24T00:00:00Z",
        "created_by": "test", "target_url": "https://banking.example.com/app",
        "inputs": [], "outputs": [],
        "steps": [
            {"step_number": 1, "action": "click", "locator": {"strategy": "id", "value": "search", "robustness_notes": "Stable ID"},
             "value": None, "reasoning": "Open search", "expected_outcome": "Search appears"},
            {"step_number": 2, "action": "click", "locator": {"strategy": "id", "value": "transfer", "robustness_notes": "Stable ID"},
             "value": None, "reasoning": "Transfer funds", "expected_outcome": "Funds move"},
        ],
        "success_checkpoint": {"condition": "url_matches", "locator": None,
                               "expected_value": "/done", "error_message": "Not done"},
        "known_errors": {}, "discovery_run_id": "test", "success_rate": None,
    })


@pytest.fixture
def state() -> dict[str, object]:
    """Supply stable visible page signals.

    Returns:
        Current browser snapshot.
    """
    return {"current_url": "https://banking.example.com/app", "title": "Search",
            "visible_text": "Search for a member", "element_count": 2}


def test_signature_uses_page_signals_and_ignores_evidence(state: dict[str, object]) -> None:
    """Evidence changes do not cause false progress; visible changes do."""
    original = calculate_state_signature(state)
    assert len(original) == 64
    assert calculate_state_signature({**state, "screenshot": b"different", "dom": "changed"}) == original
    for key, changed in (("current_url", "/different"), ("title", "Other"),
                         ("visible_text", "Other text"), ("element_count", 3)):
        assert calculate_state_signature({**state, key: changed}) != original
    with pytest.raises(TypeError):
        calculate_state_signature(None)  # type: ignore[arg-type]


def test_repeat_threshold_and_bounded_tracker(state: dict[str, object], monkeypatch: pytest.MonkeyPatch) -> None:
    """The tracker reports the third observation and retains only recent steps."""
    monkeypatch.setenv("STUCK_HISTORY_LIMIT", "3")
    tracker = StateTracker()
    for step_num in range(1, 4):
        tracker.add_state(state, step_num)
        assert tracker.is_looping() == (step_num == 3)
    history = tracker.get_history()
    history[0]["step_number"] = 999
    assert tracker.get_history()[0]["step_number"] == 1
    tracker.add_state({**state, "title": "Results"}, 4)
    assert len(tracker.get_history()) == 3 and not tracker.is_looping()
    signature = calculate_state_signature(state)
    assert is_state_repeated(signature, [signature, "different", signature], 2)
    assert not is_state_repeated(signature, [signature], 3)
    with pytest.raises(ValueError):
        is_state_repeated(signature, [], 0)
    steps: list[dict[str, object]] = []
    for number in range(5):
        record_step_in_history(steps, {"step_number": number})
    assert [item["step_number"] for item in steps] == [2, 3, 4]


def test_risk_and_context(sample_artifact: AutomationArtifact) -> None:
    """Only potentially irreversible clicks require approval."""
    assert is_risky_action(sample_artifact.steps[1], sample_artifact)
    assert not is_risky_action(sample_artifact.steps[0], sample_artifact)
    safe_read = ActionStep(step_number=3, action="read_text", locator=Locator(strategy="id", value="transfer", robustness_notes="ID"),
                           value=None, reasoning="Read transfer status", expected_outcome="Display")
    assert not is_risky_action(safe_read, sample_artifact)
    assert get_step_context(sample_artifact, 0)["next"]["action"] == "click"
    assert get_step_context(sample_artifact, -1)["current"] is None


def test_count_matching_elements_uses_first_visible_fallback() -> None:
    """An absent primary yields to fallback; hidden matches do not count."""
    driver = Mock(spec=WebDriver)
    visible = Mock(spec=WebElement)
    visible.is_displayed.return_value = True
    hidden = Mock(spec=WebElement)
    hidden.is_displayed.return_value = False
    driver.find_elements.side_effect = lambda by, value: {
        (By.CSS_SELECTOR, ".missing"): [], (By.ID, "search"): [visible, hidden, visible],
    }.get((by, value), [])
    locator = Locator(strategy="css", value=".missing", robustness_notes="CSS", fallbacks=[Locator(strategy="id", value="search", robustness_notes="ID")])
    assert count_matching_elements(driver, locator, LocatorResolver()) == 2
    driver.find_elements.assert_any_call(By.ID, "search")


def test_detects_each_pause_trigger(sample_artifact: AutomationArtifact, state: dict[str, object]) -> None:
    """Each trigger produces a human readable reason and bounded context."""
    signature = calculate_state_signature(state)
    history = [{"step_number": index, "state_signature": signature, "value": "secret"} for index in (1, 2)]
    looping = detect_stuck_state(sample_artifact, 1, state, {}, history)
    assert looping.reason == "Same state repeated - no progress"
    assert "secret" not in str(looping.escalation_context)
    assert detect_stuck_state(sample_artifact, 1, state, {}, []).is_stuck is False
    failure = detect_stuck_state(sample_artifact, 0, state, {"classification": "hard_failure"}, [])
    assert failure.reason == "Hard failure during replay"
    assert failure.current_step == 1
    risky = detect_stuck_state(sample_artifact, 0, state, {}, [])
    assert risky.reason == "Risky action requires human approval" and risky.current_step == 2
    ambiguous = detect_stuck_state(sample_artifact, 1, {**state, "matching_element_count": 2}, {}, [])
    assert ambiguous.reason == "Ambiguous state - multiple matches"
    requested = detect_stuck_state(sample_artifact, -1, state, {"human_help_requested": True}, [])
    assert requested.reason == "Human requested intervention" and requested.current_step == 0
    assert requested.recommended_action == "Awaiting human input"


def test_capture_context_is_bounded_and_survives_failed_screenshot(
    sample_artifact: AutomationArtifact, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Context captures evidence and safe recent actions, even if one signal fails."""
    monkeypatch.setenv("STUCK_VISIBLE_ELEMENTS_LIMIT", "1")
    driver = Mock(spec=WebDriver)
    driver.page_source = "<html>visible</html>"
    driver.current_url = "https://banking.example.com/app"
    driver.title = "Search"
    driver.get_screenshot_as_png.return_value = b"png"
    visible = Mock(spec=WebElement)
    visible.is_displayed.return_value = True
    visible.tag_name = "button"
    visible.text = "Search"
    visible.get_attribute.return_value = "search"
    driver.find_elements.return_value = [visible, visible]
    context = capture_escalation_context(driver, sample_artifact, 0, "Help", [{"step_number": 1, "value": "private"}])
    assert context["screenshot"] == base64.b64encode(b"png").decode("ascii")
    assert context["dom"] == "<html>visible</html>"
    assert len(context["visible_elements"]) == 1
    assert "private" not in str(context["last_steps"])
    driver.get_screenshot_as_png.side_effect = WebDriverException("lost browser")
    assert capture_escalation_context(driver, sample_artifact, 0, "Help")["screenshot_error"] == "WebDriverException"
