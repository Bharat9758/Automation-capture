"""Known error detection, classification, and safe recovery tests."""

from __future__ import annotations

from typing import Any
from unittest.mock import Mock, call

import pytest
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException, TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement

from src.artifact.schema import AutomationArtifact, Locator
from src.replay.error_handler import (
    NavigationError,
    detect_and_classify_error,
    detect_element_count,
    detect_element_visible,
    detect_text_contains,
    detect_url_matches,
    execute_recovery_action,
)
from src.replay.locator_strategy import LocatorResolver


@pytest.fixture
def browser(monkeypatch: pytest.MonkeyPatch) -> Mock:
    """Create a browser with a visible business error and allowlisted URL.

    Args:
        monkeypatch: Pytest environment modifier.

    Returns:
        Mock WebDriver.
    """
    monkeypatch.setenv("ALLOWED_DOMAINS", "banking.example.com")
    monkeypatch.setenv("ERROR_DETECTION_TIMEOUT_SECONDS", "0.02")
    driver = Mock(spec=WebDriver)
    driver.current_url = "https://banking.example.com/members/search"
    error_element = Mock(spec=WebElement)
    error_element.text = "No such member"
    error_element.is_displayed.return_value = True
    driver.find_element.return_value = error_element
    return driver


def known_artifact(rules: dict[str, dict[str, Any]]) -> AutomationArtifact:
    """Build a valid artifact containing configured runtime detection rules.

    Args:
        rules: Known error definitions to test.

    Returns:
        Valid artifact.
    """
    return AutomationArtifact.from_dict({
        "id": "lookup", "name": "Lookup", "version": "1.0.0", "description": "Find member",
        "created_at": "2026-09-25T00:00:00Z", "updated_at": "2026-09-25T00:00:00Z",
        "created_by": "test", "target_url": "https://banking.example.com/app",
        "inputs": [], "outputs": [],
        "steps": [{"step_number": 1, "action": "click", "locator": {"strategy": "id", "value": "search", "robustness_notes": "Stable ID"},
                   "value": None, "reasoning": "Lookup", "expected_outcome": "Search complete"}],
        "success_checkpoint": {"condition": "url_matches", "locator": None, "expected_value": "/members/search", "error_message": "Not ready"},
        "known_errors": rules, "discovery_run_id": "run", "success_rate": None,
    })


def test_detector_signals_and_fallbacks(browser: Mock) -> None:
    """Text, visible element, URL, and count checks use the intended selectors."""
    locator = Locator(strategy="css", value=".missing", robustness_notes="Fallback",
                      fallbacks=[Locator(strategy="id", value="error-message", robustness_notes="Stable ID")])
    missing = NoSuchElementException("not present")
    matching = browser.find_element.return_value
    browser.find_element.side_effect = [missing, matching, missing, matching]
    assert detect_text_contains(browser, locator, "such member") is True
    assert detect_element_visible(browser, locator) is True
    assert browser.find_element.call_args_list == [
        call(By.CSS_SELECTOR, ".missing"), call(By.ID, "error-message"),
        call(By.CSS_SELECTOR, ".missing"), call(By.ID, "error-message"),
    ]
    assert detect_url_matches(browser, "/members/search") is True
    assert detect_url_matches(browser, "/login") is False
    browser.find_elements.side_effect = [[], [matching, matching]]
    assert detect_element_count(browser, locator, 2) is True
    assert browser.find_elements.call_args_list == [call(By.CSS_SELECTOR, ".missing"), call(By.ID, "error-message")]
    assert detect_element_count(browser, locator, -1) is False


def test_text_detector_checks_fallback_when_primary_text_differs(browser: Mock) -> None:
    """A stale primary selector cannot hide text present at its fallback."""
    locator = Locator(strategy="css", value=".old", robustness_notes="Old",
                      fallbacks=[Locator(strategy="id", value="error", robustness_notes="Stable")])
    other = Mock(spec=WebElement)
    other.text = "Please wait"
    other.is_displayed.return_value = True
    browser.find_element.side_effect = [other, browser.find_element.return_value]
    assert detect_text_contains(browser, locator, "No such member") is True
    assert browser.find_element.call_count == 2


def test_known_business_outcome_precedes_generic_exception(browser: Mock) -> None:
    """A known page message remains a legitimate result even on a timeout."""
    artifact = known_artifact({
        "member_not_found": {"detection": {"type": "text_contains", "locator": {"strategy": "css", "value": ".error-message"},
                                                   "expected_text": "No such member"},
                             "classification": "expected_business_outcome", "business_outcome": "member_not_found"},
    })
    result = detect_and_classify_error(browser, artifact, 2, TimeoutException("sensitive data"))
    assert result.classification == "expected_business_outcome"
    assert result.business_outcome == "member_not_found"
    assert result.should_continue is False
    assert "sensitive data" not in result.error_message


def test_recoverable_rule_and_hard_login_rule(browser: Mock) -> None:
    """A visible modal is recoverable and a login redirect is a hard failure."""
    rules = {
        "unexpected_dialog": {"detection": {"type": "element_visible", "locator": {"strategy": "css", "value": ".modal"}},
                              "classification": "recoverable_condition", "recovery_action": {"action": "click", "locator": {"strategy": "id", "value": "dismiss"}}},
        "session_timeout": {"detection": {"type": "url_matches", "expected_url": "/login"},
                            "classification": "hard_failure", "message": "Session expired - requires re-authentication"},
    }
    artifact = known_artifact(rules)
    recovery = detect_and_classify_error(browser, artifact, 1, NoSuchElementException())
    assert recovery.classification == "recoverable_condition"
    assert recovery.should_continue and recovery.recovery_action["action"] == "click"
    browser.find_element.side_effect = NoSuchElementException()
    browser.current_url = "https://banking.example.com/login"
    expired = detect_and_classify_error(browser, artifact, 1, TimeoutException())
    assert expired.classification == "hard_failure"
    assert expired.error_message == "Session expired - requires re-authentication"


@pytest.mark.parametrize(("exc", "classification"), [
    (NoSuchElementException(), "hard_failure"), (TimeoutException(), "recoverable_condition"),
    (StaleElementReferenceException(), "recoverable_condition"), (NavigationError(), "hard_failure"),
    (WebDriverException(), "hard_failure"),
])
def test_unknown_exception_taxonomy(browser: Mock, exc: Exception, classification: str) -> None:
    """Unmatched browser exceptions map to a stable default taxonomy.

    Args:
        browser: Mock WebDriver.
        exc: Selenium or navigation failure.
        classification: Expected category.
    """
    result = detect_and_classify_error(browser, known_artifact({}), 3, exc)
    assert result.classification == classification
    assert result.should_continue == (classification == "recoverable_condition")
    assert "Step 3" in result.error_message and type(exc).__name__ in result.error_message


def test_incomplete_recorder_metadata_does_not_match(browser: Mock) -> None:
    """A Phase 4 recorded attempt has no detection rule and stays diagnostic."""
    artifact = known_artifact({"attempt_2": {"action": "click", "error_type": "TimeoutException"}})
    result = detect_and_classify_error(browser, artifact, 2, NoSuchElementException())
    assert result.classification == "hard_failure"


def test_invalid_rule_cannot_claim_business_outcome(browser: Mock) -> None:
    """Incomplete rules never silently classify a missing member."""
    artifact = known_artifact({"bad": {"detection": {"type": "unrecognized"}, "classification": "expected_business_outcome"}})
    assert detect_and_classify_error(browser, artifact, 1, TimeoutException()).classification == "recoverable_condition"


def test_execute_click_type_and_navigation_repairs(browser: Mock) -> None:
    """Allowlisted repair actions run using the locator resolver."""
    resolver = Mock(spec=LocatorResolver)
    element = Mock(spec=WebElement)
    resolver.resolve.return_value = element
    assert execute_recovery_action(browser, {"action": "click", "locator": {"strategy": "id", "value": "dismiss"}}, resolver)
    element.click.assert_called_once()
    assert execute_recovery_action(browser, {"action": "type", "locator": {"strategy": "id", "value": "code"}, "value": "ok"}, resolver)
    assert element.method_calls[-2:] == [call.clear(), call.send_keys("ok")]
    browser.get.side_effect = lambda url: setattr(browser, "current_url", url)
    assert execute_recovery_action(browser, {"action": "navigate", "url": "https://banking.example.com/search"}, resolver)
    browser.get.assert_called_once_with("https://banking.example.com/search")


def test_recovery_rejects_unapproved_or_failed_actions(browser: Mock) -> None:
    """No action, malformed targets, and failed repairs return False."""
    resolver = Mock(spec=LocatorResolver)
    assert execute_recovery_action(browser, None, resolver) is False
    assert execute_recovery_action(browser, {"action": "navigate", "url": "https://evil.example.com/"}, resolver) is False
    assert execute_recovery_action(browser, {"action": "script", "value": "alert(1)"}, resolver) is False
    resolver.resolve.side_effect = NoSuchElementException("gone")
    assert execute_recovery_action(browser, {"action": "click", "locator": {"strategy": "id", "value": "missing"}}, resolver) is False


def test_detect_count_handles_aria_and_text(browser: Mock) -> None:
    """Non-CSS count selectors use escaped XPath strategies."""
    locator = Locator(strategy="aria_label", value="Bob's \"Review\"", robustness_notes="Label")
    browser.find_elements.return_value = [Mock(spec=WebElement)]
    assert detect_element_count(browser, locator, 1)
    assert "concat(" in browser.find_elements.call_args.args[1]
