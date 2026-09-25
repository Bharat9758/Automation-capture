"""Resolve recorded locators with visibility waits and ordered fallbacks."""

from __future__ import annotations

import time
import math
import os
from collections.abc import Mapping
from typing import Any

from selenium.common.exceptions import (
    InvalidSelectorException,
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from src.artifact.schema import Locator
from src.logging import get_logger


LOGGER = get_logger(__name__)


class ElementNotFoundError(LookupError):
    """Report every attempted locator and why it could not become visible."""

    def __init__(self, attempts: list[dict[str, Any]], wait_timeout: float) -> None:
        """Build a diagnostic failure without hiding fallback attempts.

        Args:
            attempts: Ordered lookup results with strategy, value, and reason.
            wait_timeout: Maximum visible wait per locator in seconds.
        """
        self.attempts = attempts
        self.wait_timeout = wait_timeout
        self.suggestion = "Check whether the element exists, its locator is current, and it becomes visible."
        details = "; ".join(
            f"{item['strategy']}={item['value']!r}: {item['reason']} ({item['duration_ms']} ms)"
            for item in attempts
        )
        super().__init__(
            f"Element not found after {len(attempts)} locator attempts "
            f"(visibility timeout: {wait_timeout}s each). {details}. {self.suggestion}"
        )


class LocatorResolver:
    """Find the first visible element through primary and fallback locators."""

    def __init__(self, wait_timeout: int = 10) -> None:
        """Configure the per-locator visibility wait.

        Args:
            wait_timeout: Positive timeout in seconds.

        Raises:
            ValueError: If the timeout is not positive.
        """
        if isinstance(wait_timeout, bool) or not isinstance(wait_timeout, (int, float)) or not math.isfinite(wait_timeout) or wait_timeout <= 0:
            raise ValueError("wait_timeout must be a positive number of seconds")
        self.wait_timeout = wait_timeout

    @staticmethod
    def _coerce_locator(value: Locator | Mapping[str, Any]) -> Locator:
        """Accept schema objects and the dictionary form shown in replay examples.

        Args:
            value: Validated Locator or JSON-shaped locator dictionary.

        Returns:
            A Pydantic Locator with nested fallbacks.

        Raises:
            TypeError: If the locator has an unsupported shape.
            pydantic.ValidationError: If a field is invalid.
        """
        if isinstance(value, Locator):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("locator must be a Locator or mapping")
        fallbacks = value.get("fallbacks")
        if fallbacks is not None and (not isinstance(fallbacks, list) or not all(isinstance(item, (Locator, Mapping)) for item in fallbacks)):
            raise TypeError("fallbacks must be an ordered list of locators")
        return Locator(
            strategy=value.get("strategy"),
            value=value.get("value"),
            fallbacks=[LocatorResolver._coerce_locator(item) for item in fallbacks] if fallbacks is not None else None,
            robustness_notes=value.get("robustness_notes") or "Supplied during replay",
        )

    @staticmethod
    def _candidates(root: Locator) -> list[Locator]:
        """Flatten nested fallbacks in depth-first order without cycles.

        Args:
            root: Primary locator.

        Returns:
            Unique candidates in the order they should be attempted.
        """
        result: list[Locator] = []
        seen: set[int] = set()

        def visit(current: Locator) -> None:
            """Append a locator and visit its fallbacks.

            Args:
                current: Candidate locator.
            """
            if id(current) in seen:
                LOGGER.warning("locator_cycle_skipped", extra={"event": "locator_cycle_skipped", "strategy": current.strategy})
                return
            seen.add(id(current))
            result.append(current)
            for fallback in current.fallbacks or []:
                visit(fallback)

        visit(root)
        return result

    def resolve(self, driver: WebDriver, locator: Locator | Mapping[str, Any], *, timeout: float | None = None) -> WebElement:
        """Return the first visible element, trying fallbacks in order.

        Args:
            driver: Active Selenium WebDriver.
            locator: Primary locator and optional alternatives.
            timeout: Optional total deadline across all fallback candidates.

        Returns:
            A currently visible WebElement reference.

        Raises:
            ElementNotFoundError: If all locators are absent, hidden, or stale.
            ValueError: If an explicit timeout is not positive and finite.
        """
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0):
            raise ValueError("timeout must be a positive finite number")
        candidates = self._candidates(self._coerce_locator(locator))
        deadline = time.monotonic() + timeout if timeout is not None else None
        attempts: list[dict[str, Any]] = []
        for index, candidate in enumerate(candidates):
            started = time.monotonic()
            visible_timeout = min(self.wait_timeout, max(0.0, deadline - started)) if deadline is not None else self.wait_timeout
            reason = ""
            try:
                element = self._find(driver, candidate)
                if self._wait_for_visible(element, visible_timeout):
                    duration = round((time.monotonic() - started) * 1000)
                    self._log_resolution_attempt(candidate.strategy, True, duration)
                    return element
                reason = f"not visible within {visible_timeout}s"
            except StaleElementReferenceException:
                try:
                    remaining = max(0.0, deadline - time.monotonic()) if deadline is not None else None
                    element = self._handle_stale_element(driver, candidate, timeout=remaining)
                    duration = round((time.monotonic() - started) * 1000)
                    self._log_resolution_attempt(candidate.strategy, True, duration)
                    return element
                except (NoSuchElementException, TimeoutException, StaleElementReferenceException, WebDriverException) as exc:
                    reason = f"stale recovery failed: {type(exc).__name__}"
            except (NoSuchElementException, TimeoutException, InvalidSelectorException, WebDriverException) as exc:
                reason = type(exc).__name__
            duration = round((time.monotonic() - started) * 1000)
            attempts.append({"strategy": candidate.strategy, "value": candidate.value, "reason": reason, "duration_ms": duration})
            self._log_resolution_attempt(candidate.strategy, False, duration)
            if index + 1 < len(candidates):
                LOGGER.info("locator_fallback", extra={"event": "locator_fallback", "next_strategy": candidates[index + 1].strategy})
        raise ElementNotFoundError(attempts, timeout if timeout is not None else self.wait_timeout)

    def _find(self, driver: WebDriver, locator: Locator) -> WebElement:
        """Dispatch a candidate to its Selenium lookup strategy.

        Args:
            driver: Active browser.
            locator: Candidate to locate.

        Returns:
            Matching element; visibility is checked by resolve().
        """
        strategies = {
            "css": self._find_by_css,
            "xpath": self._find_by_xpath,
            "id": self._find_by_id,
            "text": self._find_by_text,
            "aria_label": self._find_by_aria_label,
        }
        return strategies[locator.strategy](driver, locator.value)

    def _find_by_css(self, driver: WebDriver, selector: str) -> WebElement:
        """Locate by CSS selector; resolve() subsequently waits for visibility.

        Args:
            driver: Active browser.
            selector: CSS selector.

        Returns:
            Matching element.
        """
        return driver.find_element(By.CSS_SELECTOR, selector)

    def _find_by_xpath(self, driver: WebDriver, xpath: str) -> WebElement:
        """Locate by XPath; resolve() subsequently waits for visibility.

        Args:
            driver: Active browser.
            xpath: XPath expression.

        Returns:
            Matching element.
        """
        return driver.find_element(By.XPATH, xpath)

    def _find_by_id(self, driver: WebDriver, element_id: str) -> WebElement:
        """Locate by element ID; resolve() subsequently waits for visibility.

        Args:
            driver: Active browser.
            element_id: Exact ID.

        Returns:
            Matching element.
        """
        return driver.find_element(By.ID, element_id)

    @staticmethod
    def _xpath_literal(value: str) -> str:
        """Escape text, including both kinds of quote, for XPath 1.0.

        Args:
            value: Untrusted text or attribute value.

        Returns:
            Valid XPath string literal.
        """
        if "'" not in value:
            return f"'{value}'"
        if '"' not in value:
            return f'"{value}"'
        return "concat(" + ', "\'", '.join(f"'{part}'" for part in value.split("'")) + ")"

    def _find_by_text(self, driver: WebDriver, text: str) -> WebElement:
        """Find an exact direct-text match with ASCII case insensitivity.

        Args:
            driver: Active browser.
            text: Visible text to match after whitespace normalization.

        Returns:
            Matching element.
        """
        value = self._xpath_literal(" ".join(text.split()).lower())
        expression = (
            "//*[translate(normalize-space(text()), "
            "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')=" + value + "]"
        )
        return driver.find_element(By.XPATH, expression)

    def _find_by_aria_label(self, driver: WebDriver, aria_label: str) -> WebElement:
        """Find an element with an exact aria-label attribute.

        Args:
            driver: Active browser.
            aria_label: Accessible label value.

        Returns:
            Matching element.
        """
        return driver.find_element(By.XPATH, f"//*[@aria-label={self._xpath_literal(aria_label)}]")

    def _wait_for_visible(self, element: WebElement, timeout: float) -> bool:
        """Wait on an existing reference until it becomes visible.

        ``visibility_of`` accepts a WebElement; Selenium's
        ``visibility_of_element_located`` requires a locator tuple instead.

        Args:
            element: Element reference to check.
            timeout: Maximum visibility wait in seconds.

        Returns:
            True if visible before timeout, False otherwise.

        Raises:
            StaleElementReferenceException: If the reference goes stale.
            ValueError: If locator polling is misconfigured.
        """
        try:
            poll = float(os.environ.get("LOCATOR_POLL_INTERVAL_SECONDS", "0.05"))
            if not math.isfinite(poll) or poll <= 0:
                raise ValueError("LOCATOR_POLL_INTERVAL_SECONDS must be positive and finite")
            return bool(WebDriverWait(element, timeout, poll_frequency=min(poll, timeout) if timeout > 0 else poll).until(EC.visibility_of(element)))
        except TimeoutException:
            return False

    def _handle_stale_element(
        self, driver: WebDriver, locator: Locator, retries: int = 3, *, timeout: float | None = None
    ) -> WebElement:
        """Find a fresh reference after staleness, retrying a bounded number of times.

        Args:
            driver: Active browser.
            locator: Same candidate that became stale.
            retries: Number of fresh lookup attempts.
            timeout: Remaining visibility budget for this candidate.

        Returns:
            Fresh visible WebElement.

        Raises:
            StaleElementReferenceException: If every retry is stale.
            NoSuchElementException: If every retry finds no matching element.
            TimeoutException: If every new reference stays hidden.
        """
        if retries < 1:
            raise ValueError("retries must be positive")
        deadline = time.monotonic() + timeout if timeout is not None else None
        last_error: NoSuchElementException | StaleElementReferenceException | TimeoutException | None = None
        for attempt in range(1, retries + 1):
            LOGGER.info("locator_stale_retry", extra={"event": "locator_stale_retry", "strategy": locator.strategy, "retry": attempt})
            try:
                fresh = self._find(driver, locator)
                remaining = max(0.0, deadline - time.monotonic()) if deadline is not None else self.wait_timeout
                if self._wait_for_visible(fresh, remaining):
                    return fresh
                raise TimeoutException("Fresh element did not become visible")
            except (NoSuchElementException, StaleElementReferenceException, TimeoutException) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise StaleElementReferenceException("Could not refresh stale element")

    def _log_resolution_attempt(self, strategy: str, success: bool, duration_ms: int) -> None:
        """Write a structured attempt event without selector contents.

        Args:
            strategy: Selenium locator strategy.
            success: Whether a visible element was resolved.
            duration_ms: Elapsed lookup and wait time in milliseconds.
        """
        LOGGER.info(
            "locator_resolution_attempt",
            extra={"event": "locator_resolution_attempt", "strategy": strategy, "success": success, "duration_ms": duration_ms},
        )
