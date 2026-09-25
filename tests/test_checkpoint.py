"""Checkpoint gating, matching modes, waits, and failure evidence tests."""

from __future__ import annotations

import base64
from unittest.mock import Mock, call

import pytest
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement

from src.artifact.schema import Checkpoint, Locator
from src.replay.checkpoint import CheckpointVerificationError, CheckpointVerifier
from src.replay.locator_strategy import ElementNotFoundError, LocatorResolver


@pytest.fixture
def browser() -> Mock:
    """Build a Selenium double with available URL, body, and PNG evidence.

    Returns:
        Browser mock.
    """
    driver = Mock(spec=WebDriver)
    driver.current_url = "https://banking.example.com/members/123"
    driver.get_screenshot_as_png.return_value = b"png-bytes"
    driver.page_source = "<html><body>Account Ready</body></html>"
    body = Mock(spec=WebElement)
    body.text = "Account Ready"
    driver.find_element.return_value = body
    return driver


@pytest.fixture
def resolver() -> Mock:
    """Provide a typed double for the Phase 6 visible resolver.

    Returns:
        Mocked LocatorResolver.
    """
    return Mock(spec=LocatorResolver)


def locator(strategy: str = "id", value: str = "balance") -> Locator:
    """Create a valid typed locator for a checkpoint.

    Args:
        strategy: Selenium locator strategy.
        value: Selector string.

    Returns:
        Schema locator.
    """
    return Locator(strategy=strategy, value=value, robustness_notes="Stable locator")


def checkpoint(condition: str, target: Locator | None, expected: str | None) -> Checkpoint:
    """Construct a complete schema checkpoint.

    Args:
        condition: Requested assertion type.
        target: Optional element locator.
        expected: Expected text, URL, or count.

    Returns:
        Valid checkpoint.
    """
    return Checkpoint(condition=condition, locator=target, expected_value=expected, error_message="Expected state not reached")


def test_visible_checkpoint_uses_resolver(browser: Mock, resolver: Mock) -> None:
    """The visible check uses Phase 6's fallback-aware locator resolver."""
    target = locator()
    resolver.resolve.return_value = Mock(spec=WebElement)
    assert CheckpointVerifier().verify(browser, checkpoint("element_visible", target, None), resolver, timeout=0.03, step_number=4)
    resolver.resolve.assert_called_once()
    assert resolver.resolve.call_args.args == (browser, target)
    assert 0 < resolver.resolve.call_args.kwargs["timeout"] <= 0.03
    browser.find_elements.assert_not_called()


def test_visible_timeout_includes_actual_and_evidence(browser: Mock, resolver: Mock) -> None:
    """A missing visible element raises a structured error after a short wait."""
    resolver.resolve.side_effect = ElementNotFoundError([{"strategy": "id", "value": "balance", "reason": "absent", "duration_ms": 0}], 1)
    target = checkpoint("element_visible", locator(), None)
    with pytest.raises(CheckpointVerificationError) as raised:
        CheckpointVerifier(poll_interval_seconds=0.005).verify(browser, target, resolver, timeout=0.03, step_number=5)

    error = raised.value
    assert error.expected_condition == "element_visible"
    assert error.actual_state == {"visible": False}
    assert error.step_number == 5
    assert error.evidence["expected"] == {"condition": "element_visible", "value": None}
    assert error.evidence["observed"] == {"visible": False}
    assert error.evidence["current_url"] == browser.current_url
    assert error.evidence["page_text"] == "Account Ready"
    assert error.evidence["screenshot"] == base64.b64encode(b"png-bytes").decode()
    assert "Expected state not reached" in error.message


def test_element_exists_accepts_hidden_dom_nodes(browser: Mock, resolver: Mock) -> None:
    """Existence is a DOM check and does not require visibility."""
    hidden = Mock(spec=WebElement)
    hidden.is_displayed.return_value = False
    browser.find_elements.return_value = [hidden]
    assert CheckpointVerifier().verify(browser, checkpoint("element_exists", locator(), None), resolver, timeout=0.03)
    browser.find_elements.assert_called_with(By.ID, "balance")
    resolver.resolve.assert_not_called()


def test_element_exists_missing_fails(browser: Mock, resolver: Mock) -> None:
    """A missing DOM node yields an existence failure."""
    browser.find_elements.return_value = []
    with pytest.raises(CheckpointVerificationError) as raised:
        CheckpointVerifier(poll_interval_seconds=0.005).verify(browser, checkpoint("element_exists", locator(), None), resolver, timeout=0.02)
    assert raised.value.actual_state == {"exists": False}


def test_text_contains_polls_element_and_supports_casefold(browser: Mock, resolver: Mock) -> None:
    """Text matching waits for the value and can ignore case when configured."""
    pending = Mock(spec=WebElement)
    pending.text = "Waiting"
    ready = Mock(spec=WebElement)
    ready.text = "SAVINGS Balance Ready"
    resolver.resolve.side_effect = [pending, ready]
    assert CheckpointVerifier(case_sensitive=False, poll_interval_seconds=0.005).verify(
        browser, checkpoint("text_contains", locator(), "savings balance"), resolver, timeout=0.05,
    )
    resolver.resolve.side_effect = None
    resolver.resolve.return_value = ready
    with pytest.raises(CheckpointVerificationError):
        CheckpointVerifier(case_sensitive=True, poll_interval_seconds=0.005).verify(
            browser, checkpoint("text_contains", locator(), "savings balance"), resolver, timeout=0.02,
        )


def test_text_contains_page_body_without_locator(browser: Mock, resolver: Mock) -> None:
    """An unscoped text checkpoint reads visible body text."""
    assert CheckpointVerifier().verify(browser, checkpoint("text_contains", None, "Account Ready"), resolver, timeout=0.03)
    browser.find_element.assert_called_with(By.TAG_NAME, "body")


@pytest.mark.parametrize(("value", "mode", "matches"), [
    ("/members/", "auto", True),
    ("exact:https://banking.example.com/members/123", "auto", True),
    ("exact:/members/", "auto", False),
    (r"^https://banking\.example\.com/members/\d+$", "auto", True),
    (r"regex:/members/\d+$", "auto", True),
    ("/members/", "exact", False),
    ("https://banking.example.com/members/123", "exact", True),
    ("/members/", "partial", True),
])
def test_url_modes(browser: Mock, resolver: Mock, value: str, mode: str, matches: bool) -> None:
    """URL checks support explicit exact, partial, and regular-expression modes.

    Args:
        browser: Active browser mock.
        resolver: Mock resolver.
        value: Recorded URL expectation.
        mode: Configured URL mode.
        matches: Whether the checkpoint should pass.
    """
    verifier = CheckpointVerifier(url_match_mode=mode, poll_interval_seconds=0.005)
    target = checkpoint("url_matches", None, value)
    if matches:
        assert verifier.verify(browser, target, resolver, timeout=0.03)
    else:
        with pytest.raises(CheckpointVerificationError) as raised:
            verifier.verify(browser, target, resolver, timeout=0.02)
        assert raised.value.actual_state == browser.current_url


def test_url_changes_during_wait(browser: Mock, resolver: Mock) -> None:
    """The verifier polls until a delayed navigation reaches the expected URL."""
    state = {"reads": 0}

    def current() -> str:
        """Return a new URL on the second poll.

        Returns:
            Current simulated URL.
        """
        state["reads"] += 1
        return "/pending" if state["reads"] == 1 else "/members/123"

    type(browser).current_url = property(lambda _: current())
    try:
        assert CheckpointVerifier(poll_interval_seconds=0.005).verify(
            browser, checkpoint("url_matches", None, "partial:/members/"), resolver, timeout=0.04,
        )
    finally:
        del type(browser).current_url


def test_element_count_fallbacks_and_timeout(browser: Mock, resolver: Mock) -> None:
    """Count all matching elements and try recorded fallback selectors."""
    target = Locator(strategy="css", value=".old", robustness_notes="Old selector",
                     fallbacks=[locator("id", "row")])
    browser.find_elements.side_effect = [[Mock(spec=WebElement)], [Mock(spec=WebElement), Mock(spec=WebElement)]]
    assert CheckpointVerifier().verify(browser, checkpoint("element_count", target, "2"), resolver, timeout=0.03)
    assert browser.find_elements.call_args_list == [call(By.CSS_SELECTOR, ".old"), call(By.ID, "row")]
    browser.find_elements.side_effect = None
    browser.find_elements.return_value = []
    with pytest.raises(CheckpointVerificationError) as raised:
        CheckpointVerifier(poll_interval_seconds=0.005).verify(browser, checkpoint("element_count", target, "2"), resolver, timeout=0.02)
    assert raised.value.actual_state == {"counts": [0, 0]}


def test_zero_count_does_not_pass_when_lookup_fails(browser: Mock, resolver: Mock) -> None:
    """A broken selector is not evidence that a list contains zero entries."""
    browser.find_elements.side_effect = WebDriverException("browser disconnected")
    with pytest.raises(CheckpointVerificationError) as raised:
        CheckpointVerifier(poll_interval_seconds=0.005).verify(
            browser, checkpoint("element_count", locator(), "0"), resolver, timeout=0.02,
        )
    assert raised.value.actual_state == {"counts": [None]}


def test_invalid_regex_and_broken_screenshot_are_reported(browser: Mock, resolver: Mock) -> None:
    """A bad regex fails promptly, and missing screenshot does not hide context."""
    browser.get_screenshot_as_png.side_effect = WebDriverException("disconnected")
    with pytest.raises(CheckpointVerificationError) as raised:
        CheckpointVerifier().verify(browser, checkpoint("url_matches", None, "regex:["), resolver, timeout=0.03, step_number=6)
    assert raised.value.actual_state["error_type"] == "error"
    assert raised.value.evidence["screenshot"] is None
    assert raised.value.evidence["screenshot_error"] == "WebDriverException"
    assert raised.value.evidence["page_text"] == "Account Ready"


def test_empty_url_directive_cannot_succeed(browser: Mock, resolver: Mock) -> None:
    """A regex directive without a pattern fails closed."""
    with pytest.raises(CheckpointVerificationError) as raised:
        CheckpointVerifier().verify(browser, checkpoint("url_matches", None, "regex:"), resolver, timeout=0.03)
    assert raised.value.actual_state == {"error_type": "ValueError"}


@pytest.mark.parametrize("timeout", [0, -1, float("nan"), True])
def test_invalid_timeout_rejected(browser: Mock, resolver: Mock, timeout: float) -> None:
    """Bad timeout values fail before any browser lookup.

    Args:
        browser: Browser mock.
        resolver: Mock resolver.
        timeout: Invalid maximum wait.
    """
    with pytest.raises(ValueError, match="timeout"):
        CheckpointVerifier().verify(browser, checkpoint("element_visible", locator(), None), resolver, timeout=timeout)
    browser.find_elements.assert_not_called()


def test_page_text_snippet_limit(browser: Mock, resolver: Mock) -> None:
    """Failure context bounds page text while preserving screenshot evidence."""
    body = Mock(spec=WebElement)
    body.text = "0123456789" * 100
    browser.find_element.return_value = body
    with pytest.raises(CheckpointVerificationError) as raised:
        CheckpointVerifier(page_text_chars=12, poll_interval_seconds=0.005).verify(
            browser, checkpoint("url_matches", None, "/missing"), resolver, timeout=0.02,
        )
    assert raised.value.evidence["page_text"] == "012345678901"
