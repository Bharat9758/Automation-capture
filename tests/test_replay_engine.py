"""Deterministic replay contract tests with mocked Selenium browser state."""

from __future__ import annotations

import base64
from datetime import date
from typing import Any
from unittest.mock import Mock, call

import pytest
from selenium.common.exceptions import NoSuchElementException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement

from src.artifact.schema import ActionStep, AutomationArtifact, Locator, OutputField
from src.replay.replay_engine import (
    ValidationError,
    capture_failure_evidence,
    execute_action,
    extract_output,
    replay_artifact,
    substitute_input_parameters,
    wait_for_outcome,
)


@pytest.fixture
def artifact() -> AutomationArtifact:
    """Build a replayable artifact with one typed input and numeric output.

    Returns:
        Fully validated artifact.
    """
    return AutomationArtifact.from_dict({
        "id": "lookup-savings", "name": "Lookup savings", "version": "1.0.0",
        "description": "Read a member savings balance", "created_at": "2026-09-24T00:00:00Z",
        "updated_at": "2026-09-24T00:00:00Z", "created_by": "test",
        "target_url": "https://banking.example.com/app",
        "inputs": [{"name": "member_id", "type": "string", "description": "ID", "required": True, "example": "123"}],
        "outputs": [{"name": "savings_balance", "type": "number", "description": "Balance",
                     "extraction_locator": {"strategy": "id", "value": "balance", "robustness_notes": "Stable ID"}}],
        "steps": [
            {"step_number": 1, "action": "navigate", "locator": None, "value": "/members/search",
             "reasoning": "Open search", "expected_outcome": "navigate completes without error"},
            {"step_number": 2, "action": "type", "locator": {"strategy": "id", "value": "member-id", "robustness_notes": "Stable ID"},
             "value": "{member_id}", "reasoning": "Enter ID", "expected_outcome": "type completes without error"},
            {"step_number": 3, "action": "click", "locator": {"strategy": "id", "value": "search", "robustness_notes": "Stable ID"},
             "value": None, "reasoning": "Submit", "expected_outcome": "click completes without error"},
            {"step_number": 4, "action": "read_text", "locator": {"strategy": "id", "value": "balance", "robustness_notes": "Stable ID"},
             "value": None, "reasoning": "Read balance", "expected_outcome": "read_text completes without error"},
        ],
        "success_checkpoint": {"condition": "element_visible", "locator": {"strategy": "id", "value": "balance", "robustness_notes": "Stable ID"},
                               "expected_value": None, "error_message": "Balance missing"},
        "known_errors": {}, "discovery_run_id": "run-test", "success_rate": None,
    })


@pytest.fixture
def driver(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Create a browser double with navigation and a visible savings output.

    Args:
        monkeypatch: Environment test fixture.

    Returns:
        Mock browser.
    """
    monkeypatch.setenv("ALLOWED_DOMAINS", "banking.example.com")
    browser = Mock(spec=WebDriver)
    browser.current_url = "https://banking.example.com/app"
    browser.title = "Accounts"
    browser.page_source = "<html>Accounts</html>"
    browser.get_screenshot_as_png.return_value = b"png"
    browser.execute_script.return_value = "complete"
    browser.get.side_effect = lambda url: setattr(browser, "current_url", url)

    def find_element(by: str, selector: str) -> Mock:
        """Return a visible element for each known selector.

        Args:
            by: Selenium location strategy.
            selector: Element selector.

        Returns:
            Test element.
        """
        if by != By.ID or selector not in {"member-id", "search", "balance"}:
            raise NoSuchElementException("unknown")
        element = Mock(spec=WebElement)
        element.is_displayed.return_value = True
        element.text = "$5,000.00" if selector == "balance" else ""
        return element

    browser.find_element.side_effect = find_element
    return browser


def test_replay_end_to_end_without_llm(driver: Mock, artifact: AutomationArtifact) -> None:
    """Inputs flow through navigation, typing, clicking, checkpoint, and output."""
    result = replay_artifact(driver, artifact, {"member_id": "12345"})

    assert result.success is True and result.status == "success"
    assert result.outputs == {"savings_balance": 5000}
    assert result.error is None and result.evidence == {}
    assert result.step_failed is None and result.duration_seconds >= 0
    assert len(result.logs) == 8
    assert all("12345" not in message and "5,000" not in message for message in result.logs)
    assert driver.get.call_args_list == [call("https://banking.example.com/app"), call("https://banking.example.com/members/search")]
    assert "12345" not in str(artifact.to_dict())


def test_substitution_copies_fallbacks_and_preserves_braces(artifact: AutomationArtifact) -> None:
    """Every selector and value is substituted without editing saved steps."""
    locator = Locator(strategy="css", value="[data-id='{member_id}']", robustness_notes="ID",
                      fallbacks=[Locator(strategy="id", value="row-{member_id}", robustness_notes="ID")])
    step = ActionStep(step_number=1, action="type", locator=locator, value="{member_id}",
                      reasoning="Enter ID", expected_outcome="text_contains:{member_id}")
    replaced = substitute_input_parameters([step], {"member_id": "a{other}"})[0]

    assert replaced.value == "a{other}"
    assert replaced.locator.value == "[data-id='a{other}']"
    assert replaced.locator.fallbacks[0].value == "row-a{other}"
    assert replaced.expected_outcome == "text_contains:a{other}"
    assert step.value == "{member_id}" and step.locator.value == "[data-id='{member_id}']"
    with pytest.raises(ValidationError, match="member_id"):
        substitute_input_parameters([step], {})


@pytest.mark.parametrize("invalid", [{}, {"member_id": 123}, {"member_id": "1", "extra": "x"}])
def test_invalid_inputs_raise_before_navigation(driver: Mock, artifact: AutomationArtifact, invalid: dict[str, Any]) -> None:
    """Missing, wrong-type, and undeclared inputs never touch the browser.

    Args:
        driver: Browser double.
        artifact: Valid artifact.
        invalid: Invalid input map.
    """
    with pytest.raises(ValidationError):
        replay_artifact(driver, artifact, invalid)
    driver.get.assert_not_called()


def test_number_and_date_validation(driver: Mock, artifact: AutomationArtifact) -> None:
    """Typed numeric and date values are checked before browser activity."""
    payload = artifact.to_dict()
    payload["inputs"] = [
        {"name": "amount", "type": "number", "description": "Amount", "required": True, "example": "1"},
        {"name": "as_of", "type": "date", "description": "Date", "required": True, "example": "2026-09-24"},
    ]
    payload["steps"][1]["value"] = "{amount}"
    typed = AutomationArtifact.from_dict(payload)
    with pytest.raises(ValidationError, match="amount"):
        replay_artifact(driver, typed, {"amount": True, "as_of": date(2026, 9, 24)})
    with pytest.raises(ValidationError, match="as_of"):
        replay_artifact(driver, typed, {"amount": 4.5, "as_of": "yesterday"})
    driver.get.assert_not_called()


def test_execute_action_uses_resolver_and_does_not_echo_secret(driver: Mock) -> None:
    """Typing clears an element then sends a value without returning the value."""
    element = Mock(spec=WebElement)
    resolver = Mock()
    resolver.resolve.return_value = element
    step = ActionStep(step_number=1, action="type", locator=Locator(strategy="id", value="member-id", robustness_notes="ID"),
                      value="private-value", reasoning="Type", expected_outcome="type completes without error")

    result = execute_action(driver, step, resolver)

    assert result["success"] is True
    assert "private-value" not in str(result)
    assert element.method_calls == [call.clear(), call.send_keys("private-value")]


def test_checkpoint_step_uses_recorded_locator(driver: Mock) -> None:
    """A discovery checkpoint with a locator works without a directive."""
    resolver = Mock()
    resolver.resolve.return_value = Mock(spec=WebElement)
    step = ActionStep(step_number=1, action="checkpoint", locator=Locator(strategy="id", value="done", robustness_notes="ID"),
                      value=None, reasoning="Verify result", expected_outcome="checkpoint completes without error")

    assert execute_action(driver, step, resolver)["success"] is True
    resolver.resolve.assert_called_once_with(driver, step.locator)


def test_checkpoint_step_rejects_unsupported_value(driver: Mock) -> None:
    """A checkpoint cannot silently pass with descriptive text as its test."""
    step = ActionStep(step_number=1, action="checkpoint", locator=None, value="looks good",
                      reasoning="Verify result", expected_outcome="checkpoint completes without error")
    assert execute_action(driver, step, Mock())["error"] == "ValueError"


@pytest.mark.parametrize(("output_type", "text", "expected"), [
    ("string", " Ready ", "Ready"), ("number", "$1,234.50", 1234.5),
    ("list", '["a", 2]', ["a", 2]), ("object", '{"ready":true}', {"ready": True}),
])
def test_extract_output_types(output_type: str, text: str, expected: Any) -> None:
    """Extraction produces typed values with deterministic parsing.

    Args:
        output_type: Declared schema type.
        text: Element text.
        expected: Converted value.
    """
    element = Mock(spec=WebElement)
    element.text = text
    resolver = Mock()
    resolver.resolve.return_value = element
    output = OutputField(name="value", type=output_type, description="Test", extraction_locator=Locator(strategy="id", value="value", robustness_notes="ID"))
    assert extract_output(Mock(spec=WebDriver), output, resolver) == expected


def test_failure_on_missing_element_captures_evidence(driver: Mock, artifact: AutomationArtifact) -> None:
    """An unresolvable step fails with correct step and browser evidence."""
    payload = artifact.to_dict()
    payload["steps"][1]["locator"]["value"] = "missing"
    broken = AutomationArtifact.from_dict(payload)

    result = replay_artifact(driver, broken, {"member_id": "12345"}, max_wait_ms=30)

    assert result.status == "hard_failure" and result.step_failed == 2
    assert result.outputs == {} and result.error == "type failed: ElementNotFoundError"
    assert result.evidence["screenshot"] == base64.b64encode(b"png").decode()
    assert result.evidence["dom"] == "<html>Accounts</html>"
    assert result.evidence["current_url"] == "https://banking.example.com/members/search"
    assert result.evidence["title"] == "Accounts"
    assert "12345" not in str(result.logs)


def test_navigation_allowlist_rejects_redirect(driver: Mock, artifact: AutomationArtifact) -> None:
    """A redirect outside configured domains stops before any replay actions."""
    driver.get.side_effect = lambda url: setattr(driver, "current_url", "https://outside.example.com/")
    result = replay_artifact(driver, artifact, {"member_id": "12345"})

    assert result.status == "hard_failure" and result.step_failed == 0
    assert driver.get.call_count == 1


def test_final_checkpoint_failure_blocks_outputs(driver: Mock, artifact: AutomationArtifact) -> None:
    """A valid action trace cannot succeed without the final artifact condition."""
    payload = artifact.to_dict()
    payload["success_checkpoint"] = {"condition": "url_matches", "locator": None, "expected_value": r"^https://banking\.example\.com/never$", "error_message": "Wrong page"}
    changed = AutomationArtifact.from_dict(payload)
    result = replay_artifact(driver, changed, {"member_id": "12345"}, max_wait_ms=30)

    assert result.status == "hard_failure" and result.step_failed == 5
    assert result.outputs == {} and result.evidence["step_index"] == 5


def test_output_conversion_failure_is_hard_failure(driver: Mock, artifact: AutomationArtifact) -> None:
    """An invalid numeric output cannot be reported as a successful replay."""
    original = driver.find_element.side_effect

    def wrong_balance(by: str, selector: str) -> Mock:
        """Change only the output text in the mock browser.

        Args:
            by: Lookup strategy.
            selector: Lookup string.

        Returns:
            Matching browser element.
        """
        element = original(by, selector)
        if selector == "balance":
            element.text = "invalid money"
        return element

    driver.find_element.side_effect = wrong_balance
    result = replay_artifact(driver, artifact, {"member_id": "12345"})
    assert result.status == "hard_failure" and result.step_failed == 5
    assert result.error == "Output savings_balance failed: ValueError"
    assert result.outputs == {}


def test_url_and_count_checkpoints(driver: Mock, artifact: AutomationArtifact) -> None:
    """Final URL and element-count conditions are evaluated deterministically."""
    payload = artifact.to_dict()
    payload["success_checkpoint"] = {"condition": "url_matches", "locator": None, "expected_value": r"/members/search$", "error_message": "Wrong page"}
    assert replay_artifact(driver, AutomationArtifact.from_dict(payload), {"member_id": "12345"}).success
    payload["success_checkpoint"] = {"condition": "element_count", "locator": {"strategy": "css", "value": ".result", "robustness_notes": "Result selector"}, "expected_value": "2", "error_message": "Wrong count"}
    driver.find_elements.return_value = [Mock(spec=WebElement), Mock(spec=WebElement)]
    assert replay_artifact(driver, AutomationArtifact.from_dict(payload), {"member_id": "12345"}).success


def test_machine_readable_outcomes_and_descriptive_text(driver: Mock) -> None:
    """Explicit conditions are awaited; ordinary recorder prose is descriptive."""
    assert wait_for_outcome(driver, "url_matches:members/search$", 30) is False
    assert wait_for_outcome(driver, "url_matches:banking\\.example\\.com/app$", 30) is True
    assert wait_for_outcome(driver, "click completes without error", 30) is True
    driver.find_elements.return_value = [Mock(text="before")]
    assert wait_for_outcome(driver, "element_count:.row=1", 30) is True
    assert wait_for_outcome(driver, "text_changed:.row", 30) is False


def test_text_change_and_invalid_url_pattern(driver: Mock, artifact: AutomationArtifact) -> None:
    """Changes are detected, and invalid checkpoint regexes fail cleanly."""
    before = Mock(spec=WebElement)
    before.text = "loading"
    after = Mock(spec=WebElement)
    after.text = "ready"
    driver.find_elements.side_effect = [[before], [after]]
    assert wait_for_outcome(driver, "text_changed:.status", 30) is True

    payload = artifact.to_dict()
    payload["success_checkpoint"] = {"condition": "url_matches", "locator": None, "expected_value": "[", "error_message": "Invalid regex"}
    result = replay_artifact(driver, AutomationArtifact.from_dict(payload), {"member_id": "12345"})
    assert result.status == "hard_failure"
    assert result.step_failed == 5
    assert result.error == "Success checkpoint error: ValueError"


def test_failure_evidence_survives_broken_screenshot(driver: Mock) -> None:
    """Evidence collection still returns DOM and URL when screenshot fails."""
    driver.get_screenshot_as_png.side_effect = WebDriverException("disconnected")
    evidence = capture_failure_evidence(driver, 3)
    assert evidence["screenshot"] is None
    assert evidence["screenshot_error"] == "WebDriverException"
    assert evidence["dom"] == "<html>Accounts</html>"
