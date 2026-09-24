"""Tests for visible locator resolution and ordered fallbacks."""

from __future__ import annotations

from unittest.mock import Mock, call, patch

import pytest
from selenium.common.exceptions import NoSuchElementException, StaleElementReferenceException, TimeoutException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement

from src.artifact.schema import Locator
from src.replay.locator_strategy import ElementNotFoundError, LocatorResolver


def make_locator(strategy: str, value: str, fallbacks: list[Locator] | None = None) -> Locator:
    """Create a schema-valid test locator with optional alternatives.

    Args:
        strategy: Supported strategy.
        value: Selector or identifying text.
        fallbacks: Ordered alternative locators.

    Returns:
        A validated Locator instance.
    """
    return Locator(strategy=strategy, value=value, fallbacks=fallbacks, robustness_notes="Test locator")


def make_element(visible: bool = True) -> Mock:
    """Create a Selenium element double with a visibility state.

    Args:
        visible: Whether the element is currently displayed.

    Returns:
        Element mock.
    """
    element = Mock(spec=WebElement)
    element.is_displayed.return_value = visible
    return element


@pytest.mark.parametrize(
    ("strategy", "value", "by", "query"),
    [
        ("css", "button.search-btn", By.CSS_SELECTOR, "button.search-btn"),
        ("xpath", "//button[@id='search']", By.XPATH, "//button[@id='search']"),
        ("id", "search", By.ID, "search"),
        (
            "text",
            "  SeArCh   Now  ",
            By.XPATH,
            "//*[translate(normalize-space(text()), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='search now']",
        ),
        ("aria_label", "Search", By.XPATH, "//*[@aria-label='Search']"),
    ],
)
def test_resolve_each_strategy(strategy: str, value: str, by: str, query: str) -> None:
    """Each strategy translates to the intended Selenium lookup and visible element.

    Args:
        strategy: Locator strategy under test.
        value: Requested locator string.
        by: Expected Selenium By constant.
        query: Expected Selenium lookup argument.
    """
    driver = Mock(spec=WebDriver)
    element = make_element()
    driver.find_element.return_value = element

    assert LocatorResolver().resolve(driver, make_locator(strategy, value)) is element
    driver.find_element.assert_called_once_with(by, query)


def test_fallbacks_are_attempted_in_order() -> None:
    """A missing primary and first fallback lead to the next visible fallback."""
    driver = Mock(spec=WebDriver)
    found = make_element()
    driver.find_element.side_effect = [NoSuchElementException(), NoSuchElementException(), found]
    locator = make_locator(
        "css", "button.old",
        [make_locator("id", "former-search"), make_locator("aria_label", "Search")],
    )

    assert LocatorResolver().resolve(driver, locator) is found
    assert driver.find_element.call_args_list == [
        call(By.CSS_SELECTOR, "button.old"),
        call(By.ID, "former-search"),
        call(By.XPATH, "//*[@aria-label='Search']"),
    ]


def test_hidden_primary_waits_then_uses_fallback() -> None:
    """A hidden primary consumes a visibility wait before trying alternatives."""
    driver = Mock(spec=WebDriver)
    hidden, visible = make_element(False), make_element(True)
    driver.find_element.side_effect = [hidden, visible]
    with patch("src.replay.locator_strategy.WebDriverWait") as wait_class:
        wait_class.return_value.until.side_effect = [TimeoutException(), visible]
        locator = make_locator("css", ".hidden", [make_locator("id", "shown")])

        assert LocatorResolver(wait_timeout=2).resolve(driver, locator) is visible

    assert wait_class.call_args_list == [call(hidden, 2), call(visible, 2)]
    assert driver.find_element.call_args_list == [call(By.CSS_SELECTOR, ".hidden"), call(By.ID, "shown")]


def test_element_that_becomes_visible_during_wait_succeeds() -> None:
    """Visibility polling can return a reference that started out hidden."""
    driver = Mock(spec=WebDriver)
    element = make_element(False)
    driver.find_element.return_value = element
    element.is_displayed.side_effect = [False, True]
    locator = make_locator("id", "delayed")

    assert LocatorResolver(wait_timeout=1).resolve(driver, locator) is element
    assert element.is_displayed.call_count == 2


def test_stale_element_is_reacquired_and_logged() -> None:
    """Staleness during visibility polling reacquires a fresh reference."""
    driver = Mock(spec=WebDriver)
    stale, fresh = make_element(), make_element()
    stale.is_displayed.side_effect = StaleElementReferenceException("replaced")
    driver.find_element.side_effect = [stale, fresh]
    resolver = LocatorResolver()
    locator = make_locator("id", "refreshing")

    with patch("src.replay.locator_strategy.LOGGER") as logger:
        assert resolver.resolve(driver, locator) is fresh

    assert driver.find_element.call_count == 2
    assert any(args.args[0] == "locator_stale_retry" for args in logger.info.call_args_list)
    assert any(
        args.kwargs.get("extra", {}).get("success") is True
        for args in logger.info.call_args_list
    )


def test_stale_retries_are_bounded_then_fallback_succeeds() -> None:
    """After three stale references the next candidate may still succeed."""
    driver = Mock(spec=WebDriver)
    always_stale = make_element()
    always_stale.is_displayed.side_effect = StaleElementReferenceException()
    fresh = make_element()
    driver.find_element.side_effect = [always_stale] * 4 + [fresh]
    locator = make_locator("css", ".replaced", [make_locator("id", "stable")])

    assert LocatorResolver().resolve(driver, locator) is fresh
    assert driver.find_element.call_count == 5


def test_stale_recovery_tolerates_temporary_missing_element() -> None:
    """A replaced DOM node can disappear briefly before its new copy appears."""
    driver = Mock(spec=WebDriver)
    stale, fresh = make_element(), make_element()
    stale.is_displayed.side_effect = StaleElementReferenceException()
    driver.find_element.side_effect = [stale, NoSuchElementException(), fresh]

    assert LocatorResolver().resolve(driver, make_locator("id", "replaced")) is fresh
    assert driver.find_element.call_count == 3


def test_all_candidates_fail_with_actionable_details() -> None:
    """Exhaustion reports each selector, reason, wait limit, and suggestion."""
    driver = Mock(spec=WebDriver)
    driver.find_element.side_effect = NoSuchElementException("gone")
    locator = make_locator("css", ".missing", [make_locator("id", "missing-id")])

    with pytest.raises(ElementNotFoundError) as raised:
        LocatorResolver(wait_timeout=2).resolve(driver, locator)

    error = raised.value
    assert [item["strategy"] for item in error.attempts] == ["css", "id"]
    assert all(item["reason"] == "NoSuchElementException" for item in error.attempts)
    assert ".missing" in str(error) and "missing-id" in str(error)
    assert "2s" in str(error) and "Check whether the element exists" in str(error)


def test_dict_input_and_xpath_quoting() -> None:
    """The replay example shape works and embedded quotes cannot break XPath."""
    driver = Mock(spec=WebDriver)
    found = make_element()
    driver.find_element.side_effect = [NoSuchElementException(), found]
    locator = {
        "strategy": "css", "value": "button.old",
        "fallbacks": [{"strategy": "aria_label", "value": "Bob's \"Search\""}],
    }

    assert LocatorResolver().resolve(driver, locator) is found
    assert driver.find_element.call_args_list[1] == call(
        By.XPATH, '//*[@aria-label=concat(\'Bob\', "\'", \'s "Search"\')]',
    )


def test_invalid_timeout_is_rejected() -> None:
    """Zero and boolean wait limits cannot silently disable waiting."""
    for timeout in (0, -1, True):
        with pytest.raises(ValueError, match="wait_timeout"):
            LocatorResolver(wait_timeout=timeout)
