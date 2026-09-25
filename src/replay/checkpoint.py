"""Verify a recorded browser checkpoint before replay extracts outputs."""

from __future__ import annotations

import base64
import math
import os
import re
import time
from collections.abc import Callable
from typing import Any, Literal

from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.remote.webelement import WebElement
from selenium.webdriver.support.ui import WebDriverWait

from src.artifact.schema import Checkpoint, Locator
from src.logging import get_logger
from src.replay.locator_strategy import ElementNotFoundError, LocatorResolver


LOGGER = get_logger(__name__)
UrlMatchMode = Literal["auto", "exact", "partial", "regex"]


class CheckpointVerificationError(Exception):
    """Carry the failed condition, last observation, and browser evidence."""

    def __init__(
        self, message: str, *, expected_condition: str, actual_state: Any,
        step_number: int | None, evidence: dict[str, Any],
    ) -> None:
        """Create a structured checkpoint failure.

        Args:
            message: Human-readable reason.
            expected_condition: Requested checkpoint condition.
            actual_state: Last value observed in the browser.
            step_number: Associated artifact step, if supplied.
            evidence: Best-effort browser snapshot.
        """
        super().__init__(message)
        self.message = message
        self.expected_condition = expected_condition
        self.actual_state = actual_state
        self.step_number = step_number
        self.evidence = evidence


class CheckpointVerifier:
    """Wait for browser state to satisfy one explicit artifact checkpoint."""

    def __init__(
        self, *, case_sensitive: bool | None = None,
        url_match_mode: UrlMatchMode | None = None,
        poll_interval_seconds: float | None = None,
        page_text_chars: int | None = None,
    ) -> None:
        """Configure text matching, URL semantics, polling, and evidence size.

        Args:
            case_sensitive: Override CHECKPOINT_CASE_SENSITIVE.
            url_match_mode: Override CHECKPOINT_URL_MATCH_MODE.
            poll_interval_seconds: Override CHECKPOINT_POLL_INTERVAL_SECONDS.
            page_text_chars: Override CHECKPOINT_PAGE_TEXT_CHARS.

        Raises:
            ValueError: If a configuration setting is invalid.
        """
        setting = os.environ.get("CHECKPOINT_CASE_SENSITIVE", "true").lower()
        if case_sensitive is None:
            if setting not in {"true", "false"}:
                raise ValueError("CHECKPOINT_CASE_SENSITIVE must be true or false")
            case_sensitive = setting == "true"
        if not isinstance(case_sensitive, bool):
            raise ValueError("case_sensitive must be boolean")
        mode = url_match_mode if url_match_mode is not None else os.environ.get("CHECKPOINT_URL_MATCH_MODE", "auto")
        if mode not in {"auto", "exact", "partial", "regex"}:
            raise ValueError("CHECKPOINT_URL_MATCH_MODE must be auto, exact, partial, or regex")
        poll = poll_interval_seconds if poll_interval_seconds is not None else float(os.environ.get("CHECKPOINT_POLL_INTERVAL_SECONDS", "0.05"))
        chars = page_text_chars if page_text_chars is not None else int(os.environ.get("CHECKPOINT_PAGE_TEXT_CHARS", "512"))
        if isinstance(poll, bool) or not math.isfinite(poll) or poll <= 0:
            raise ValueError("CHECKPOINT_POLL_INTERVAL_SECONDS must be positive and finite")
        if isinstance(chars, bool) or not isinstance(chars, int) or chars <= 0:
            raise ValueError("CHECKPOINT_PAGE_TEXT_CHARS must be a positive integer")
        self.case_sensitive = case_sensitive
        self.url_match_mode: UrlMatchMode = mode
        self.poll_interval_seconds = poll
        self.page_text_chars = chars

    @staticmethod
    def _by(locator: Locator) -> tuple[str, str]:
        """Map a schema locator to a Selenium find_elements tuple.

        Args:
            locator: Candidate locator.

        Returns:
            Selenium strategy and query, with XPath values escaped.
        """
        if locator.strategy == "text":
            value = LocatorResolver._xpath_literal(" ".join(locator.value.split()).lower())
            return By.XPATH, "//*[translate(normalize-space(text()), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')=" + value + "]"
        if locator.strategy == "aria_label":
            return By.XPATH, f"//*[@aria-label={LocatorResolver._xpath_literal(locator.value)}]"
        return {"css": By.CSS_SELECTOR, "xpath": By.XPATH, "id": By.ID}[locator.strategy], locator.value

    def _matching_elements(self, driver: WebDriver, locator: Locator) -> list[list[WebElement] | None]:
        """Query every fallback without requiring visible elements.

        Args:
            driver: Active browser.
            locator: Primary and fallback locators.

        Returns:
            Per-candidate DOM matches, or None for a failed lookup.
        """
        matches: list[list[WebElement] | None] = []
        for candidate in LocatorResolver._candidates(locator):
            try:
                matches.append(driver.find_elements(*self._by(candidate)))
            except (NoSuchElementException, StaleElementReferenceException, WebDriverException):
                matches.append(None)
        return matches

    def _url_matches(self, actual: str, expected: str) -> bool:
        """Match a URL by explicit prefix or configured default mode.

        Args:
            actual: Current browser URL.
            expected: Saved URL expectation.

        Returns:
            Whether the URL satisfies the expectation.

        Raises:
            re.error: If a regex directive contains an invalid pattern.
        """
        mode = self.url_match_mode
        value = expected
        for prefix in ("exact:", "partial:", "regex:"):
            if expected.startswith(prefix):
                mode, value = prefix[:-1], expected[len(prefix):]
                break
        if not value:
            raise ValueError("URL checkpoint requires a nonempty match value")
        if mode == "auto":
            mode = "regex" if value.startswith("^") or value.endswith("$") else "partial"
        if mode == "exact":
            return actual == value
        if mode == "partial":
            return value in actual
        return re.search(value, actual) is not None

    def _evaluate(self, driver: WebDriver, checkpoint: Checkpoint, resolver: LocatorResolver, timeout: float) -> tuple[bool, Any]:
        """Evaluate one checkpoint and retain a useful last observed value.

        Args:
            driver: Active browser.
            checkpoint: Recorded condition.
            resolver: Phase 6 visible-element resolver.
            timeout: Remaining deadline for a fallback-aware visible lookup.

        Returns:
            Whether it holds, and the current state or count.
        """
        if checkpoint.condition == "url_matches":
            actual_url = driver.current_url
            return self._url_matches(actual_url, checkpoint.expected_value or ""), actual_url
        if checkpoint.condition == "element_exists":
            assert checkpoint.locator is not None
            matches = self._matching_elements(driver, checkpoint.locator)
            found = any(group for group in matches if group is not None)
            return found, {"exists": found}
        if checkpoint.condition == "element_count":
            assert checkpoint.locator is not None
            expected = int(checkpoint.expected_value or "0")
            counts = [len(group) if group is not None else None for group in self._matching_elements(driver, checkpoint.locator)]
            return expected in counts, {"counts": counts}
        if checkpoint.condition == "element_visible":
            assert checkpoint.locator is not None
            try:
                resolver.resolve(driver, checkpoint.locator, timeout=timeout)
                return True, {"visible": True}
            except (ElementNotFoundError, NoSuchElementException, StaleElementReferenceException, TimeoutException):
                return False, {"visible": False}
        if checkpoint.condition == "text_contains":
            try:
                text = resolver.resolve(driver, checkpoint.locator, timeout=timeout).text if checkpoint.locator else driver.find_element(By.TAG_NAME, "body").text
            except (ElementNotFoundError, NoSuchElementException, StaleElementReferenceException, TimeoutException):
                return False, {"text": "element unavailable"}
            actual = text if self.case_sensitive else text.casefold()
            expected_text = (checkpoint.expected_value or "") if self.case_sensitive else (checkpoint.expected_value or "").casefold()
            return expected_text in actual, {"text": text[:self.page_text_chars]}
        raise ValueError("Unsupported checkpoint condition")

    def _wait_for_condition(self, driver: WebDriver, condition_func: Callable[[], bool], timeout: float) -> bool:
        """Poll a browser condition until true or the deadline expires.

        Args:
            driver: Active browser.
            condition_func: Function checking current state.
            timeout: Wait duration in seconds.

        Returns:
            Whether the condition became true before timeout.
        """
        try:
            return bool(WebDriverWait(driver, timeout, poll_frequency=min(timeout, self.poll_interval_seconds)).until(lambda _: condition_func()))
        except TimeoutException:
            return False

    def _get_failure_context(self, driver: WebDriver, checkpoint: Checkpoint, actual_state: Any = None) -> dict[str, Any]:
        """Collect a bounded page text snippet and a base64 PNG when available.

        Args:
            driver: Active browser.
            checkpoint: Failed condition.
            actual_state: Last observed value.

        Returns:
            Expected and observed fields plus best-effort browser evidence.
        """
        context: dict[str, Any] = {
            "expected": {"condition": checkpoint.condition, "value": checkpoint.expected_value},
            "observed": actual_state,
        }
        for key, getter in (
            ("current_url", lambda: driver.current_url),
            ("page_text", lambda: driver.find_element(By.TAG_NAME, "body").text[:self.page_text_chars]),
            ("screenshot", lambda: base64.b64encode(driver.get_screenshot_as_png()).decode("ascii")),
        ):
            try:
                context[key] = getter()
            except (WebDriverException, AttributeError, TypeError, ValueError) as exc:
                context[key] = None
                context[f"{key}_error"] = type(exc).__name__
        if context["page_text"] is None:
            try:
                context["page_text"] = driver.page_source[:self.page_text_chars]
            except (WebDriverException, AttributeError, TypeError):
                pass
        return context

    def verify(
        self, driver: WebDriver, checkpoint: Checkpoint, locator_resolver: LocatorResolver,
        timeout: float = 10, *, step_number: int | None = None,
    ) -> bool:
        """Verify the condition within a deadline or raise with browser evidence.

        Args:
            driver: Active Selenium WebDriver.
            checkpoint: Schema-validated condition to verify.
            locator_resolver: Visible locator resolver and fallback provider.
            timeout: Maximum polling time in seconds.
            step_number: Step associated with this checkpoint.

        Returns:
            True if the condition is met.

        Raises:
            ValueError: If timeout or arguments are invalid.
            CheckpointVerificationError: If the condition fails or browser errors.
        """
        if not isinstance(checkpoint, Checkpoint) or not isinstance(locator_resolver, LocatorResolver):
            raise ValueError("checkpoint and locator_resolver must be validated instances")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a positive finite number of seconds")
        started = time.monotonic()
        deadline = started + timeout
        actual: Any = None

        def condition() -> bool:
            """Remember the last observation while the wait helper polls.

            Returns:
                Whether the checkpoint currently holds.
            """
            nonlocal actual
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            matched, actual = self._evaluate(driver, checkpoint, locator_resolver, remaining)
            return matched

        try:
            if self._wait_for_condition(driver, condition, timeout):
                LOGGER.info("checkpoint_verified", extra={"event": "checkpoint_verified", "condition": checkpoint.condition,
                                                          "step": step_number, "duration_ms": round((time.monotonic() - started) * 1000)})
                return True
        except (WebDriverException, ValueError, TypeError, re.error) as exc:
            actual = {"error_type": type(exc).__name__}
        context = self._get_failure_context(driver, checkpoint, actual)
        LOGGER.warning("checkpoint_failed", extra={"event": "checkpoint_failed", "condition": checkpoint.condition,
                                                    "step": step_number, "duration_ms": round((time.monotonic() - started) * 1000)})
        raise CheckpointVerificationError(
            f"{checkpoint.error_message}: expected {checkpoint.condition} ({checkpoint.expected_value}); observed {actual}",
            expected_condition=checkpoint.condition, actual_state=actual, step_number=step_number, evidence=context,
        )
