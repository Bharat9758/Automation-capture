"""Detect known browser outcomes and classify runtime replay failures."""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Literal

from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver

from src.agent.actor import Actor
from src.artifact.schema import AutomationArtifact, Locator
from src.logging import get_logger
from src.replay.locator_strategy import ElementNotFoundError, LocatorResolver


LOGGER = get_logger(__name__)
Classification = Literal["expected_business_outcome", "recoverable_condition", "hard_failure"]


class NavigationError(ValueError):
    """Indicate that a navigation or redirect violates replay policy."""


@dataclass(frozen=True, kw_only=True)
class ErrorClassification:
    """State the observed error category and any permitted recovery action."""

    classification: Classification
    business_outcome: str | None = None
    recovery_action: dict[str, Any] | None = None
    error_message: str | None = None
    should_continue: bool = False


def _as_locator(locator: Locator | Mapping[str, Any]) -> Locator:
    """Convert an artifact locator or compact JSON locator to a typed locator.

    Args:
        locator: Locator or dictionary with a strategy and value.

    Returns:
        Validated locator, including ordered fallbacks.
    """
    return LocatorResolver._coerce_locator(locator)


def _detection_resolver() -> LocatorResolver:
    """Create a short, configurable resolver for observing current UI state.

    Returns:
        A resolver limited by ERROR_DETECTION_TIMEOUT_SECONDS.

    Raises:
        ValueError: If the configured timeout is invalid.
    """
    timeout = float(os.environ.get("ERROR_DETECTION_TIMEOUT_SECONDS", "0.25"))
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("ERROR_DETECTION_TIMEOUT_SECONDS must be a positive finite number")
    return LocatorResolver(wait_timeout=timeout)


def detect_text_contains(driver: WebDriver, locator: Locator, expected_text: str) -> bool:
    """Check visible element text for a configured business message.

    Args:
        driver: Browser to observe.
        locator: Primary locator and fallbacks.
        expected_text: Exact substring to look for.

    Returns:
        Whether visible text contains the configured substring.
    """
    try:
        if not expected_text:
            return False
        resolved = _as_locator(locator)
        for candidate in LocatorResolver._candidates(resolved):
            try:
                element = _detection_resolver().resolve(driver, replace(candidate, fallbacks=None))
                if expected_text in element.text:
                    return True
            except (ElementNotFoundError, WebDriverException):
                continue
        return False
    except (ElementNotFoundError, WebDriverException, TypeError, ValueError):
        return False


def detect_element_visible(driver: WebDriver, locator: Locator) -> bool:
    """Check whether any primary or fallback locator resolves visibly.

    Args:
        driver: Browser to observe.
        locator: Visible-element target.

    Returns:
        True when an element is visible.
    """
    try:
        _detection_resolver().resolve(driver, _as_locator(locator))
        return True
    except (ElementNotFoundError, WebDriverException, TypeError, ValueError):
        return False


def detect_url_matches(driver: WebDriver, expected_url: str) -> bool:
    """Match an exact or partial current URL without interpreting regex code.

    Args:
        driver: Browser to observe.
        expected_url: Absolute URL or distinctive URL fragment.

    Returns:
        True when the configured fragment appears in the current URL.
    """
    try:
        return bool(expected_url) and expected_url in driver.current_url
    except (WebDriverException, TypeError, ValueError):
        return False


def _locator_by(locator: Locator) -> tuple[str, str]:
    """Translate a supported locator to a Selenium find_elements tuple.

    Args:
        locator: Validated candidate locator.

    Returns:
        Selenium strategy and escaped selector.
    """
    if locator.strategy == "text":
        literal = LocatorResolver._xpath_literal(" ".join(locator.value.split()).lower())
        return By.XPATH, "//*[translate(normalize-space(text()), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')=" + literal + "]"
    if locator.strategy == "aria_label":
        return By.XPATH, f"//*[@aria-label={LocatorResolver._xpath_literal(locator.value)}]"
    return {"css": By.CSS_SELECTOR, "xpath": By.XPATH, "id": By.ID}[locator.strategy], locator.value


def detect_element_count(driver: WebDriver, locator: Locator, expected_count: int) -> bool:
    """Check the number of matching nodes for any configured fallback.

    Args:
        driver: Browser to observe.
        locator: Primary and fallback locators.
        expected_count: Required number of matching elements.

    Returns:
        Whether any locator finds the requested count.
    """
    if isinstance(expected_count, bool) or not isinstance(expected_count, int) or expected_count < 0:
        return False
    try:
        for candidate in LocatorResolver._candidates(_as_locator(locator)):
            try:
                if len(driver.find_elements(*_locator_by(candidate))) == expected_count:
                    return True
            except WebDriverException:
                continue
        return False
    except (WebDriverException, TypeError, ValueError):
        return False


def _matches_detection(driver: WebDriver, detection: Mapping[str, Any]) -> bool:
    """Evaluate one explicitly configured detection rule.

    Args:
        driver: Browser at failure time.
        detection: Known error's condition configuration.

    Returns:
        True only for a supported and satisfied condition.
    """
    condition = detection.get("type")
    locator = detection.get("locator")
    if condition == "text_contains" and locator is not None and isinstance(detection.get("expected_text"), str):
        return detect_text_contains(driver, locator, detection["expected_text"])
    if condition == "element_visible" and locator is not None:
        return detect_element_visible(driver, locator)
    if condition == "url_matches" and isinstance(detection.get("expected_url"), str):
        return detect_url_matches(driver, detection["expected_url"])
    if condition == "element_count" and locator is not None:
        try:
            raw_count = detection.get("expected_count")
            if isinstance(raw_count, bool) or not (
                isinstance(raw_count, int) or isinstance(raw_count, str) and raw_count.isdecimal()
            ):
                return False
            return detect_element_count(driver, locator, int(raw_count))
        except (ValueError, TypeError):
            return False
    return False


def detect_and_classify_error(
    driver: WebDriver, artifact: AutomationArtifact, step_index: int, caught_exception: Exception
) -> ErrorClassification:
    """Classify a failure from verified known rules or its exception type.

    A known error matches only when its detection condition holds on the live
    browser. Incomplete discovery metadata is ignored. Error texts never echo
    Selenium exception messages or substituted input values.

    Args:
        driver: Browser at the failure point.
        artifact: Source artifact containing optional known errors.
        step_index: Current artifact step (zero for initial navigation).
        caught_exception: Original runtime exception.

    Returns:
        A business outcome, recoverable condition, or hard failure.
    """
    matched = detect_known_error(driver, artifact, step_index)
    if matched is not None:
        return matched

    if isinstance(caught_exception, (NavigationError, ElementNotFoundError, NoSuchElementException)):
        classification = "hard_failure"
    elif isinstance(caught_exception, (TimeoutException, StaleElementReferenceException)):
        classification = "recoverable_condition"
    else:
        classification = "hard_failure"
    result = ErrorClassification(
        classification=classification,
        error_message=(f"Step {step_index} did not meet its expected result; observed {type(caught_exception).__name__}"),
        should_continue=classification == "recoverable_condition",
    )
    LOGGER.info("replay_error_classified", extra={"event": "replay_error_classified", "step": step_index, "classification": classification, "exception_type": type(caught_exception).__name__})
    return result


def detect_known_error(driver: WebDriver, artifact: AutomationArtifact, step_index: int) -> ErrorClassification | None:
    """Detect an explicitly recorded outcome independently of step exceptions.

    Args:
        driver: Browser state at the current replay step.
        artifact: Validated artifact with optional known error rules.
        step_index: Current artifact step.

    Returns:
        Matched known rule, or None if no configured condition holds.
    """
    for name, rule in artifact.known_errors.items():
        if not isinstance(rule, dict) or not isinstance(rule.get("detection"), dict):
            continue
        if not _matches_detection(driver, rule["detection"]):
            continue
        classification = rule.get("classification")
        if classification not in {"expected_business_outcome", "recoverable_condition", "hard_failure"}:
            LOGGER.warning("invalid_known_error", extra={"event": "invalid_known_error", "rule": name})
            continue
        recovery = rule.get("recovery_action")
        result = ErrorClassification(
            classification=classification,
            business_outcome=str(rule.get("business_outcome") or name) if classification == "expected_business_outcome" else None,
            recovery_action=recovery if classification == "recoverable_condition" and isinstance(recovery, dict) else None,
            error_message=str(rule.get("message") or f"Observed {name} at step {step_index}; expected recorded step to succeed"),
            should_continue=classification == "recoverable_condition",
        )
        LOGGER.info("replay_error_classified", extra={"event": "replay_error_classified", "step": step_index, "classification": result.classification, "rule": name})
        return result
    return None


def execute_recovery_action(
    driver: WebDriver, recovery_action: dict[str, Any] | None, locator_resolver: LocatorResolver
) -> bool:
    """Run one allowlisted repair action without logging selectors or values.

    Args:
        driver: Active browser.
        recovery_action: Click, type, or navigation instruction, or None.
        locator_resolver: Locator resolution with bounded visibility waits.

    Returns:
        True if the repair action completed, False on invalid or failed repair.
    """
    if not isinstance(recovery_action, dict):
        return False
    action = recovery_action.get("action")
    try:
        if action == "navigate":
            url = recovery_action.get("url", recovery_action.get("value"))
            if not isinstance(url, str) or not Actor.is_allowed_url(url):
                return False
            driver.get(url)
            if not Actor.is_allowed_url(driver.current_url):
                return False
        elif action in {"click", "type"}:
            raw_locator = recovery_action.get("locator")
            if raw_locator is None:
                return False
            element = locator_resolver.resolve(driver, _as_locator(raw_locator))
            if action == "click":
                element.click()
            else:
                value = recovery_action.get("value")
                if not isinstance(value, str):
                    return False
                element.clear()
                element.send_keys(value)
        else:
            return False
    except (ElementNotFoundError, WebDriverException, TypeError, ValueError) as exc:
        LOGGER.warning("replay_recovery_failed", extra={"event": "replay_recovery_failed", "action": action, "error_type": type(exc).__name__})
        return False
    LOGGER.info("replay_recovery_completed", extra={"event": "replay_recovery_completed", "action": action})
    return True
