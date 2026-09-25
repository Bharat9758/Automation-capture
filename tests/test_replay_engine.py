"""Deterministic replay contract tests with mocked Selenium browser state."""

from __future__ import annotations

import base64
from datetime import date
from typing import Any
from unittest.mock import Mock, call, patch

import pytest
from selenium.common.exceptions import (
    ElementClickInterceptedException,
    NoSuchElementException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement

from src.artifact.schema import ActionStep, AutomationArtifact, Locator, OutputField
from src.replay.checkpoint import CheckpointVerificationError
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


def test_risky_click_pauses_before_execution(driver: Mock, artifact: AutomationArtifact) -> None:
    """Approval is needed before clicking a transfer control."""
    payload = artifact.to_dict()
    payload["steps"][2]["reasoning"] = "Transfer funds"
    result = replay_artifact(driver, AutomationArtifact.from_dict(payload), {"member_id": "12345"})
    assert result.status == "recoverable_error" and result.step_failed == 3
    assert result.stuck_state is not None
    assert result.stuck_state.reason == "Risky action requires human approval"
    assert result.stuck_state.current_screenshot == base64.b64encode(b"png")
    assert result.evidence["escalation_context"]["artifact_id"] == artifact.id
    assert all(item.args[0] != "https://banking.example.com/transfer" for item in driver.get.call_args_list)


def test_ambiguous_click_pauses(driver: Mock, artifact: AutomationArtifact) -> None:
    """Two visible matches of a target selector require human choice."""
    one = Mock(spec=WebElement)
    one.is_displayed.return_value = True
    two = Mock(spec=WebElement)
    two.is_displayed.return_value = True
    driver.find_elements.side_effect = lambda by, value: [one, two] if (by, value) == (By.ID, "search") else []
    result = replay_artifact(driver, artifact, {"member_id": "12345"})
    assert result.status == "recoverable_error" and result.step_failed == 3
    assert result.stuck_state is not None and result.stuck_state.reason == "Ambiguous state - multiple matches"
    assert result.outputs == {}


def test_explicit_help_pauses_before_navigation(driver: Mock, artifact: AutomationArtifact) -> None:
    """An explicit help request leaves the browser untouched by replay actions."""
    result = replay_artifact(driver, artifact, {"member_id": "12345"}, request_human_help=True)
    assert result.status == "recoverable_error" and result.step_failed == 0
    assert result.stuck_state is not None and result.stuck_state.reason == "Human requested intervention"
    driver.get.assert_not_called()


def test_repeated_state_pauses_before_extraction(driver: Mock, artifact: AutomationArtifact) -> None:
    """Three meaningful actions that leave the same page signature pause replay."""
    payload = artifact.to_dict()
    payload["steps"] = [
        {"step_number": index, "action": "click", "locator": {"strategy": "id", "value": "search", "robustness_notes": "Stable ID"},
         "value": None, "reasoning": "Refresh search", "expected_outcome": "Click completed"}
        for index in (1, 2, 3)
    ]
    result = replay_artifact(driver, AutomationArtifact.from_dict(payload), {"member_id": "12345"})
    assert result.status == "recoverable_error" and result.step_failed == 3
    assert result.stuck_state is not None and result.stuck_state.reason == "Same state repeated - no progress"
    assert result.outputs == {}


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
    assert result.evidence["expected"]["condition"] == "url_matches"
    assert result.evidence["observed"] == "https://banking.example.com/members/search"


def test_verification_is_required_before_output_extraction(driver: Mock, artifact: AutomationArtifact) -> None:
    """A verifier failure stops replay before any extraction call."""
    rejected = CheckpointVerificationError(
        "Wrong state", expected_condition="element_visible", actual_state={"visible": False},
        step_number=5, evidence={"expected": {"condition": "element_visible"}, "observed": {"visible": False}},
    )
    with patch("src.replay.replay_engine.CheckpointVerifier.verify", side_effect=rejected) as verify, patch(
        "src.replay.replay_engine.extract_output"
    ) as extract:
        result = replay_artifact(driver, artifact, {"member_id": "12345"}, max_wait_ms=30)

    verify.assert_called_once()
    extract.assert_not_called()
    assert result.status == "hard_failure" and result.step_failed == 5
    assert result.evidence["observed"] == {"visible": False}


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
    driver.find_elements.side_effect = lambda by, selector: [Mock(spec=WebElement), Mock(spec=WebElement)] if (by, selector) == (By.CSS_SELECTOR, ".result") else []
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
    driver.find_elements.side_effect = None
    driver.find_elements.return_value = []

    payload = artifact.to_dict()
    payload["success_checkpoint"] = {"condition": "url_matches", "locator": None, "expected_value": "regex:[", "error_message": "Invalid regex"}
    result = replay_artifact(driver, AutomationArtifact.from_dict(payload), {"member_id": "12345"}, max_wait_ms=30)
    assert result.status == "hard_failure"
    assert result.step_failed == 5
    assert result.error == "Success checkpoint failed: Invalid regex"
    assert result.evidence["observed"]["error_type"] == "error"


def test_failure_evidence_survives_broken_screenshot(driver: Mock) -> None:
    """Evidence collection still returns DOM and URL when screenshot fails."""
    driver.get_screenshot_as_png.side_effect = WebDriverException("disconnected")
    evidence = capture_failure_evidence(driver, 3)
    assert evidence["screenshot"] is None
    assert evidence["screenshot_error"] == "WebDriverException"
    assert evidence["dom"] == "<html>Accounts</html>"


def test_member_not_found_is_a_business_outcome(driver: Mock, artifact: AutomationArtifact) -> None:
    """An observed negative business result is not reported as a system error."""
    payload = artifact.to_dict()
    payload["known_errors"] = {
        "member_not_found": {
            "detection": {"type": "text_contains", "locator": {"strategy": "css", "value": ".error-message"},
                          "expected_text": "No such member"},
            "classification": "expected_business_outcome", "business_outcome": "member_not_found",
        },
    }
    original = driver.find_element.side_effect

    def with_error(by: str, selector: str) -> Mock:
        """Show the business result after searching.

        Args:
            by: Selenium strategy.
            selector: Requested locator.

        Returns:
            Matching error text or normal element.
        """
        if by == By.CSS_SELECTOR and selector == ".error-message":
            error = Mock(spec=WebElement)
            error.is_displayed.return_value = True
            error.text = "No such member"
            return error
        if by == By.ID and selector == "balance":
            raise NoSuchElementException("balance not rendered")
        return original(by, selector)

    driver.find_element.side_effect = with_error
    result = replay_artifact(driver, AutomationArtifact.from_dict(payload), {"member_id": "missing"}, max_wait_ms=30)

    assert result.success is False and result.status == "business_outcome"
    assert result.business_outcome == "member_not_found"
    assert result.error is None and result.step_failed is None
    assert result.outputs == {} and result.evidence == {}
    assert "missing" not in str(result.logs)


def test_business_outcome_at_final_checkpoint(driver: Mock, artifact: AutomationArtifact) -> None:
    """An alternate business result is recognized after successful steps."""
    payload = artifact.to_dict()
    payload["known_errors"] = {
        "permission_denied": {"detection": {"type": "url_matches", "expected_url": "/members/search"},
                              "classification": "expected_business_outcome", "business_outcome": "permission_denied"},
    }
    payload["success_checkpoint"] = {"condition": "url_matches", "locator": None, "expected_value": "/never", "error_message": "Missing success page"}
    result = replay_artifact(driver, AutomationArtifact.from_dict(payload), {"member_id": "12345"}, max_wait_ms=30)
    assert result.status == "business_outcome" and result.business_outcome == "permission_denied"


def test_business_outcome_preempts_weak_success_checkpoint(driver: Mock, artifact: AutomationArtifact) -> None:
    """A passing URL checkpoint cannot hide a configured negative result."""
    payload = artifact.to_dict()
    payload["success_checkpoint"] = {"condition": "url_matches", "locator": None, "expected_value": "/members/search", "error_message": "Wrong page"}
    payload["known_errors"] = {
        "account_exists": {"detection": {"type": "text_contains", "locator": {"strategy": "css", "value": ".message"},
                                         "expected_text": "Account already exists"},
                           "classification": "expected_business_outcome"},
    }
    original = driver.find_element.side_effect

    def find_message(by: str, selector: str) -> Mock:
        """Expose a business result alongside the normal page elements.

        Args:
            by: Selenium strategy.
            selector: Requested locator.

        Returns:
            Browser element.
        """
        if by == By.CSS_SELECTOR and selector == ".message":
            element = Mock(spec=WebElement)
            element.is_displayed.return_value = True
            element.text = "Account already exists"
            return element
        return original(by, selector)

    driver.find_element.side_effect = find_message
    result = replay_artifact(driver, AutomationArtifact.from_dict(payload), {"member_id": "12345"})
    assert result.status == "business_outcome" and result.business_outcome == "account_exists"
    assert result.outputs == {}


def test_recovery_dismisses_dialog_and_retries_click(driver: Mock, artifact: AutomationArtifact) -> None:
    """A known modal repair runs once before retrying the blocked step."""
    payload = artifact.to_dict()
    payload["known_errors"] = {
        "unexpected_dialog": {
            "detection": {"type": "element_visible", "locator": {"strategy": "id", "value": "modal"}},
            "classification": "recoverable_condition",
            "recovery_action": {"action": "click", "locator": {"strategy": "id", "value": "dismiss"}},
        },
    }
    state = {"active": True, "search_attempts": 0, "dismissals": 0}
    original = driver.find_element.side_effect

    def find_dynamic(by: str, selector: str) -> Mock:
        """Expose the modal until its dismiss control is clicked.

        Args:
            by: Selenium strategy.
            selector: Requested locator.

        Returns:
            Element reflecting current modal state.
        """
        if by == By.ID and selector == "modal":
            if not state["active"]:
                raise NoSuchElementException()
            modal = Mock(spec=WebElement)
            modal.is_displayed.return_value = True
            return modal
        if by == By.ID and selector == "dismiss":
            dismiss = Mock(spec=WebElement)
            dismiss.is_displayed.return_value = True

            def close() -> None:
                """Dismiss the test dialog."""
                state["dismissals"] += 1
                state["active"] = False

            dismiss.click.side_effect = close
            return dismiss
        if by == By.ID and selector == "search":
            button = original(by, selector)

            def submit() -> None:
                """Block a submission while the dialog covers the button."""
                state["search_attempts"] += 1
                if state["active"]:
                    raise ElementClickInterceptedException("modal overlay")

            button.click.side_effect = submit
            return button
        return original(by, selector)

    driver.find_element.side_effect = find_dynamic
    result = replay_artifact(driver, AutomationArtifact.from_dict(payload), {"member_id": "12345"})
    assert result.success is True
    assert state == {"active": False, "search_attempts": 2, "dismissals": 1}
    assert any("recovery completed" in line for line in result.logs)


def test_transient_read_retry_is_bounded(driver: Mock, artifact: AutomationArtifact, monkeypatch: pytest.MonkeyPatch) -> None:
    """An idempotent read retries once, then reports repeated timeouts."""
    monkeypatch.setenv("REPLAY_MAX_RECOVERY_RETRIES", "1")
    original = driver.find_element.side_effect
    state = {"reads": 0}

    class FlakyBalance:
        """Visible element whose first text read times out."""

        def is_displayed(self) -> bool:
            """Report the element as visible.

            Returns:
                True.
            """
            return True

        @property
        def text(self) -> str:
            """Fail the first read and then expose the balance.

            Returns:
                Balance after the first read.

            Raises:
                TimeoutException: On the first read.
            """
            state["reads"] += 1
            if state["reads"] == 1:
                raise TimeoutException("slow page")
            return "$5,000.00"

    def find_flaky(by: str, selector: str) -> Any:
        """Return the flaky value only for the balance selector.

        Args:
            by: Selenium strategy.
            selector: Requested locator.

        Returns:
            Matching mock element.
        """
        return FlakyBalance() if by == By.ID and selector == "balance" else original(by, selector)

    driver.find_element.side_effect = find_flaky
    result = replay_artifact(driver, artifact, {"member_id": "12345"})
    assert result.success is True and result.outputs == {"savings_balance": 5000}
    assert state["reads"] >= 2
    assert any("retry 1/1" in line for line in result.logs)


def test_unknown_click_timeout_is_not_retried(driver: Mock, artifact: AutomationArtifact) -> None:
    """Potentially irreversible clicks require an explicit repair rule."""
    original = driver.find_element.side_effect
    attempts = {"count": 0}

    def blocked_click(by: str, selector: str) -> Mock:
        """Raise a timeout on the only click attempt.

        Args:
            by: Selenium strategy.
            selector: Requested locator.

        Returns:
            Selenium element double.
        """
        element = original(by, selector)
        if selector == "search":
            def time_out() -> None:
                """Count and fail an uncertain click."""
                attempts["count"] += 1
                raise TimeoutException("uncertain completion")

            element.click.side_effect = time_out
        return element

    driver.find_element.side_effect = blocked_click
    result = replay_artifact(driver, artifact, {"member_id": "12345"})
    assert result.status == "hard_failure" and result.step_failed == 3
    assert attempts["count"] == 1


def test_failed_explicit_recovery_stops_replay(driver: Mock, artifact: AutomationArtifact) -> None:
    """A broken dismiss control cannot trigger an unbounded click loop."""
    payload = artifact.to_dict()
    payload["known_errors"] = {
        "unexpected_dialog": {
            "detection": {"type": "element_visible", "locator": {"strategy": "id", "value": "modal"}},
            "classification": "recoverable_condition",
            "recovery_action": {"action": "click", "locator": {"strategy": "id", "value": "missing-dismiss"}},
        },
    }
    original = driver.find_element.side_effect
    state = {"search_attempts": 0}

    def find_modal(by: str, selector: str) -> Mock:
        """Expose a modal but no repair button.

        Args:
            by: Selenium strategy.
            selector: Requested locator.

        Returns:
            Matching element.
        """
        if by == By.ID and selector == "modal":
            element = Mock(spec=WebElement)
            element.is_displayed.return_value = True
            return element
        element = original(by, selector)
        if by == By.ID and selector == "search":
            def fail() -> None:
                """Reject one blocked search click."""
                state["search_attempts"] += 1
                raise ElementClickInterceptedException()

            element.click.side_effect = fail
        return element

    driver.find_element.side_effect = find_modal
    result = replay_artifact(driver, AutomationArtifact.from_dict(payload), {"member_id": "12345"}, max_wait_ms=30)
    assert result.status == "hard_failure" and result.step_failed == 3
    assert state["search_attempts"] == 1


def test_known_hard_failure_preempts_passing_checkpoint(driver: Mock, artifact: AutomationArtifact) -> None:
    """An authentication warning overrides an unrelated passing checkpoint."""
    payload = artifact.to_dict()
    payload["success_checkpoint"] = {"condition": "url_matches", "locator": None, "expected_value": "/members/search", "error_message": "Wrong page"}
    payload["known_errors"] = {
        "authentication_failed": {"detection": {"type": "url_matches", "expected_url": "/members/search"},
                                  "classification": "hard_failure", "message": "Authentication failed"},
    }
    result = replay_artifact(driver, AutomationArtifact.from_dict(payload), {"member_id": "12345"})
    assert result.status == "hard_failure" and result.error == "Authentication failed"
    assert result.outputs == {} and result.step_failed == 5
