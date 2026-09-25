"""Execute validated automation artifacts in Selenium without an LLM."""

from __future__ import annotations

import base64
import json
import os
import re
import time
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import urljoin

from selenium.common.exceptions import (
    NoSuchElementException,
    StaleElementReferenceException,
    TimeoutException,
    WebDriverException,
)
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support.ui import WebDriverWait

from src.agent.actor import Actor
from src.artifact.schema import ActionStep, AutomationArtifact, Checkpoint, Locator, OutputField
from src.logging import get_logger
from src.replay.checkpoint import CheckpointVerificationError, CheckpointVerifier
from src.replay.error_handler import (
    ErrorClassification,
    NavigationError,
    detect_and_classify_error,
    detect_known_error,
    execute_recovery_action,
)
from src.replay.locator_strategy import ElementNotFoundError, LocatorResolver


LOGGER = get_logger(__name__)
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
_OUTCOME_CONDITIONS = {"url_matches", "text_contains", "text_changed", "element_visible", "element_count"}
_IMPLICIT_RETRY_ACTIONS = {"navigate", "type", "read_text", "wait", "checkpoint"}


class ValidationError(ValueError):
    """Indicate invalid replay inputs or unresolved parameters."""


@dataclass(kw_only=True)
class ReplayResult:
    """Summarize the deterministic replay and its failure evidence."""

    success: bool
    status: Literal["success", "business_outcome", "recoverable_error", "hard_failure"]
    outputs: dict[str, Any] = field(default_factory=dict)
    business_outcome: str | None = None
    error: str | None = None
    step_failed: int | None = None
    logs: list[str] = field(default_factory=list)
    evidence: dict[str, Any] = field(default_factory=dict)
    duration_seconds: float = 0.0


def _validate_inputs(artifact: AutomationArtifact, input_params: dict[str, Any]) -> None:
    """Check declared inputs and every referenced placeholder without exposing values.

    Args:
        artifact: Valid replay artifact.
        input_params: Caller supplied arguments.

    Raises:
        ValidationError: If a required input is absent or has an invalid type.
    """
    if not isinstance(input_params, dict):
        raise ValidationError("input_params must be a dictionary")
    declared = {item.name: item for item in artifact.inputs}
    for name, parameter in declared.items():
        if name not in input_params:
            if parameter.required:
                raise ValidationError(f"Missing required input: {name}")
            continue
        value = input_params[name]
        if parameter.type == "string" and not isinstance(value, str):
            raise ValidationError(f"Input {name} must be a string")
        if parameter.type == "number":
            if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
                raise ValidationError(f"Input {name} must be a finite number")
            try:
                valid_number = Decimal(str(value)).is_finite()
            except (InvalidOperation, ValueError):
                valid_number = False
            if not valid_number:
                raise ValidationError(f"Input {name} must be a finite number")
        if parameter.type == "date":
            if isinstance(value, date) and not isinstance(value, datetime):
                continue
            if not isinstance(value, str):
                raise ValidationError(f"Input {name} must be an ISO date")
            try:
                date.fromisoformat(value)
            except ValueError as exc:
                raise ValidationError(f"Input {name} must be an ISO date") from exc
    for name in input_params:
        if name not in declared:
            raise ValidationError(f"Undeclared input: {name}")


def _substitute(value: str, input_params: dict[str, Any]) -> str:
    """Replace named placeholders in an arbitrary string without formatting input.

    Args:
        value: Template string.
        input_params: Validated input values.

    Returns:
        String with placeholders replaced exactly once.

    Raises:
        ValidationError: If a referenced input is missing.
    """
    def replacement(match: re.Match[str]) -> str:
        """Render one known input as text.

        Args:
            match: Matched placeholder.

        Returns:
            Value as browser text.
        """
        name = match.group(1)
        if name not in input_params:
            raise ValidationError(f"Missing referenced input: {name}")
        value = input_params[name]
        return value.isoformat() if isinstance(value, date) else str(value)

    return _PLACEHOLDER.sub(replacement, value)


def _substitute_locator(locator: Locator, input_params: dict[str, Any]) -> Locator:
    """Copy a locator tree with substituted selector strings.

    Args:
        locator: Primary and fallback locators.
        input_params: Validated input values.

    Returns:
        New locator tree; the artifact is never modified.
    """
    return replace(
        locator, value=_substitute(locator.value, input_params),
        fallbacks=[_substitute_locator(item, input_params) for item in locator.fallbacks] if locator.fallbacks else None,
    )


def substitute_input_parameters(steps: list[ActionStep], input_params: dict[str, Any]) -> list[ActionStep]:
    """Copy recorded steps while replacing values and all locator placeholders.

    Args:
        steps: Ordered artifact steps.
        input_params: Caller supplied values.

    Returns:
        Independent, substituted steps.

    Raises:
        ValidationError: If a placeholder has no supplied value.
    """
    if not isinstance(input_params, dict):
        raise ValidationError("input_params must be a dictionary")
    return [
        replace(
            step,
            value=_substitute(step.value, input_params) if step.value is not None else None,
            expected_outcome=_substitute(step.expected_outcome, input_params),
            locator=_substitute_locator(step.locator, input_params) if step.locator else None,
        )
        for step in steps
    ]


def _navigate(driver: WebDriver, url: str, timeout_seconds: float) -> None:
    """Visit an allowed URL and wait for document readiness.

    Args:
        driver: Active browser.
        url: Absolute HTTP(S) URL.
        timeout_seconds: Document ready deadline.

    Raises:
        ValueError: If the URL or redirect is outside the configured allowlist.
        TimeoutException: If the document stays unready.
    """
    if not Actor.is_allowed_url(url):
        raise NavigationError("Navigation URL is outside ALLOWED_DOMAINS")
    driver.get(url)
    if not Actor.is_allowed_url(driver.current_url):
        raise NavigationError("Navigation redirected outside ALLOWED_DOMAINS")
    WebDriverWait(driver, timeout_seconds).until(lambda browser: browser.execute_script("return document.readyState") == "complete")


def _by(locator: Locator) -> tuple[str, str]:
    """Translate one locator into a Selenium tuple for counting elements.

    Args:
        locator: Count target locator.

    Returns:
        A Selenium By strategy and expression.
    """
    if locator.strategy == "text":
        literal = LocatorResolver._xpath_literal(" ".join(locator.value.split()).lower())
        return By.XPATH, "//*[translate(normalize-space(text()), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')=" + literal + "]"
    if locator.strategy == "aria_label":
        return By.XPATH, f"//*[@aria-label={LocatorResolver._xpath_literal(locator.value)}]"
    return {"css": By.CSS_SELECTOR, "xpath": By.XPATH, "id": By.ID}[locator.strategy], locator.value


def _checkpoint_met(driver: WebDriver, checkpoint: Checkpoint, resolver: LocatorResolver) -> bool:
    """Evaluate a schema checkpoint against current browser state.

    Args:
        driver: Active browser.
        checkpoint: Condition to evaluate.
        resolver: Visible locator resolution.

    Returns:
        Whether the condition currently holds.
    """
    if checkpoint.condition == "url_matches":
        return re.search(checkpoint.expected_value or "", driver.current_url) is not None
    if checkpoint.condition == "element_visible":
        if checkpoint.locator is None:
            raise ValueError("element_visible requires a locator")
        try:
            resolver.resolve(driver, checkpoint.locator)
            return True
        except ElementNotFoundError:
            return False
    if checkpoint.condition == "text_contains":
        element = resolver.resolve(driver, checkpoint.locator) if checkpoint.locator else driver.find_element(By.TAG_NAME, "body")
        return (checkpoint.expected_value or "") in element.text
    if checkpoint.condition == "element_count":
        if checkpoint.locator is None:
            raise ValueError("element_count requires a locator")
        expected = int(checkpoint.expected_value or "0")
        return any(len(driver.find_elements(*_by(candidate))) == expected for candidate in resolver._candidates(checkpoint.locator))
    raise ValueError("Unsupported checkpoint condition")


def _wait_checkpoint(driver: WebDriver, checkpoint: Checkpoint, resolver: LocatorResolver, timeout_seconds: float) -> bool:
    """Poll an explicit checkpoint until it succeeds or its timeout expires.

    Args:
        driver: Active browser.
        checkpoint: Condition to await.
        resolver: Locator resolver.
        timeout_seconds: Maximum wait duration.

    Returns:
        True if the condition became true, otherwise False.
    """
    try:
        return bool(WebDriverWait(driver, timeout_seconds).until(lambda browser: _checkpoint_met(browser, checkpoint, resolver)))
    except re.error as exc:
        raise ValueError("Checkpoint URL pattern is invalid") from exc
    except (TimeoutException, NoSuchElementException, ElementNotFoundError, StaleElementReferenceException):
        return False


def wait_for_outcome(driver: WebDriver, expected_outcome: str, timeout: int) -> bool:
    """Wait for machine-readable outcome directives; accept descriptive prose.

    Supported directives are ``url_matches:<regex>``, ``text_contains:<text>``,
    ``text_changed:<css>``, ``element_visible:<css>``, and
    ``element_count:<css>=<count>``. Other text describes the action and is not
    a browser assertion. The artifact success checkpoint is always enforced.

    Args:
        driver: Active browser.
        expected_outcome: Directive or human-readable description.
        timeout: Wait in milliseconds.

    Returns:
        True on a met directive or a descriptive outcome; False on timeout.

    Raises:
        ValueError: If a recognized directive is malformed.
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive milliseconds")
    condition, separator, argument = expected_outcome.partition(":")
    if not separator or condition not in _OUTCOME_CONDITIONS:
        return True
    if not argument:
        raise ValueError(f"{condition} requires an argument")
    resolver = LocatorResolver(wait_timeout=timeout / 1000)
    if condition == "text_changed":
        def read_text(browser: WebDriver) -> str:
            """Read the current selector text, including a missing initial node.

            Args:
                browser: Active Selenium browser.

            Returns:
                Current text or an empty string if the node is absent.
            """
            elements = browser.find_elements(By.CSS_SELECTOR, argument)
            return elements[0].text if elements else ""

        baseline = read_text(driver)
        try:
            return bool(WebDriverWait(driver, timeout / 1000).until(lambda browser: read_text(browser) != baseline))
        except TimeoutException:
            return False
    elif condition == "element_count":
        selector, delim, count = argument.rpartition("=")
        if not delim or not selector:
            raise ValueError("element_count requires <css>=<count>")
        checkpoint = Checkpoint(condition="element_count", locator=Locator(strategy="css", value=selector, robustness_notes="Outcome selector"), expected_value=count, error_message="Expected count not reached")
    else:
        locator = Locator(strategy="css", value=argument, robustness_notes="Outcome selector") if condition == "element_visible" else None
        checkpoint = Checkpoint(condition=condition, locator=locator, expected_value=None if condition == "element_visible" else argument, error_message="Expected outcome not reached")
    return _wait_checkpoint(driver, checkpoint, resolver, timeout / 1000)


def execute_action(
    driver: WebDriver, action: ActionStep, locator_resolver: LocatorResolver, *, raise_on_error: bool = False
) -> dict[str, Any]:
    """Execute one substituted step and return structured timing and details.

    Args:
        driver: Active browser.
        action: A complete, substituted action.
        locator_resolver: Resolver configured for this step's timeout.
        raise_on_error: Preserve the original exception for replay classification.

    Returns:
        Success, elapsed milliseconds, and safe details. Read results contain
        text for the caller but text is never written to structured logs.

    Raises:
        ElementNotFoundError: When requested and no locator resolves.
        WebDriverException: When requested and Selenium fails.
        ValueError: When requested and the action is invalid.
    """
    started = time.monotonic()
    details: dict[str, Any] = {}
    try:
        if action.action == "navigate":
            if action.value is None:
                raise ValueError("navigate requires a URL")
            _navigate(driver, action.value, locator_resolver.wait_timeout)
        elif action.action == "checkpoint":
            if action.value:
                condition, separator, _ = action.value.partition(":")
                if not separator or condition not in _OUTCOME_CONDITIONS:
                    raise ValueError("checkpoint step requires a supported condition directive")
                if not wait_for_outcome(driver, action.value, int(locator_resolver.wait_timeout * 1000)):
                    raise TimeoutException("Step checkpoint was not met")
            elif action.locator is not None:
                locator_resolver.resolve(driver, action.locator)
            else:
                raise ValueError("checkpoint step requires a condition directive or locator")
        else:
            if action.locator is None:
                raise ValueError(f"{action.action} requires a locator")
            element = locator_resolver.resolve(driver, action.locator)
            if action.action == "click":
                element.click()
            elif action.action == "type":
                if action.value is None:
                    raise ValueError("type requires a value")
                element.clear()
                element.send_keys(action.value)
            elif action.action == "read_text":
                details["text"] = element.text
            elif action.action != "wait":
                raise ValueError("Unsupported replay action")
        duration = round((time.monotonic() - started) * 1000)
        LOGGER.info("replay_action", extra={"event": "replay_action", "step": action.step_number, "action": action.action, "success": True, "duration_ms": duration})
        return {"success": True, "duration_ms": duration, "details": details}
    except (ElementNotFoundError, WebDriverException, ValueError) as exc:
        duration = round((time.monotonic() - started) * 1000)
        LOGGER.warning("replay_action_failed", extra={"event": "replay_action_failed", "step": action.step_number, "action": action.action, "error_type": type(exc).__name__, "duration_ms": duration})
        if raise_on_error:
            raise
        return {"success": False, "duration_ms": duration, "details": {}, "error": type(exc).__name__}


def extract_output(driver: WebDriver, output_field: OutputField, locator_resolver: LocatorResolver) -> Any:
    """Read and convert a declared output without logging its content.

    Args:
        driver: Active browser.
        output_field: Declared field and extraction locator.
        locator_resolver: Visible element resolver.

    Returns:
        String, finite number, JSON list, or JSON object.

    Raises:
        ValueError: If extraction is unconfigured or its type is invalid.
    """
    if output_field.extraction_locator is None:
        raise ValueError(f"Output {output_field.name} lacks an extraction locator")
    element = locator_resolver.resolve(driver, output_field.extraction_locator)
    raw = element.text.strip() or (element.get_attribute("value") or "").strip()
    if output_field.type == "string":
        return raw
    if output_field.type == "number":
        try:
            number = Decimal(raw.replace(",", "").lstrip("$€£"))
        except InvalidOperation as exc:
            raise ValueError(f"Output {output_field.name} is not a number") from exc
        if not number.is_finite():
            raise ValueError(f"Output {output_field.name} must be finite")
        if number == number.to_integral_value():
            return int(number)
        try:
            converted = float(number)
        except OverflowError as exc:
            raise ValueError(f"Output {output_field.name} exceeds supported numeric range") from exc
        if not Decimal(str(converted)).is_finite():
            raise ValueError(f"Output {output_field.name} exceeds supported numeric range")
        return converted
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Output {output_field.name} is not valid JSON") from exc
    if output_field.type == "list" and isinstance(parsed, list):
        return parsed
    if output_field.type == "object" and isinstance(parsed, dict):
        return parsed
    raise ValueError(f"Output {output_field.name} has the wrong JSON type")


def capture_failure_evidence(driver: WebDriver, step_index: int) -> dict[str, Any]:
    """Collect browser state independently so a broken signal cannot hide others.

    Args:
        driver: Active browser.
        step_index: Artifact step number, or next step on final-check failure.

    Returns:
        Screenshot as base64, DOM, URL, title, and any capture errors.
    """
    evidence: dict[str, Any] = {"step_index": step_index}
    signals = {
        "screenshot": lambda: base64.b64encode(driver.get_screenshot_as_png()).decode("ascii"),
        "dom": lambda: driver.page_source,
        "current_url": lambda: driver.current_url,
        "title": lambda: driver.title,
    }
    for name, capture in signals.items():
        try:
            evidence[name] = capture()
        except (WebDriverException, AttributeError, TypeError, ValueError) as exc:
            evidence[name] = None
            evidence[f"{name}_error"] = type(exc).__name__
    return evidence


def replay_artifact(
    driver: WebDriver, artifact: AutomationArtifact, input_params: dict[str, Any], max_wait_ms: int = 10000
) -> ReplayResult:
    """Validate, navigate, execute, verify, and extract an artifact without Claude.

    Args:
        driver: Active Selenium WebDriver.
        artifact: Validated saved automation artifact.
        input_params: Named values for declared parameters.
        max_wait_ms: Maximum wait per step or checkpoint, in milliseconds.

    Returns:
        Replay result with outputs or a hard failure and browser evidence.

    Raises:
        ValidationError: If caller inputs, artifact, or wait configuration is invalid.
    """
    started = time.monotonic()
    if not isinstance(artifact, AutomationArtifact) or not artifact.validate():
        raise ValidationError("artifact must be a valid AutomationArtifact")
    if isinstance(max_wait_ms, bool) or not isinstance(max_wait_ms, int) or max_wait_ms <= 0:
        raise ValidationError("max_wait_ms must be a positive integer")
    try:
        recovery_limit = int(os.environ.get("REPLAY_MAX_RECOVERY_RETRIES", "1"))
        if recovery_limit < 0:
            raise ValueError("negative retries")
    except ValueError as exc:
        raise ValidationError("REPLAY_MAX_RECOVERY_RETRIES must be a nonnegative integer") from exc
    _validate_inputs(artifact, input_params)
    steps = substitute_input_parameters(artifact.steps, input_params)
    checkpoint = replace(
        artifact.success_checkpoint,
        expected_value=_substitute(artifact.success_checkpoint.expected_value, input_params) if artifact.success_checkpoint.expected_value is not None else None,
        locator=_substitute_locator(artifact.success_checkpoint.locator, input_params) if artifact.success_checkpoint.locator else None,
    )
    logs: list[str] = ["Input validation passed"]
    LOGGER.info("replay_inputs_valid", extra={"event": "replay_inputs_valid", "artifact_id": artifact.id, "input_count": len(input_params)})

    def failure(step_number: int, message: str, checkpoint_evidence: dict[str, Any] | None = None) -> ReplayResult:
        """Build a hard failure with safely summarized diagnostics.

        Args:
            step_number: Failed step or verification phase.
            message: Error category without input contents.
            checkpoint_evidence: Captured checkpoint state when available.

        Returns:
            Failed replay result with best-effort browser evidence.
        """
        logs.append(f"Step {step_number}: {message}")
        LOGGER.error("replay_failed", extra={"event": "replay_failed", "artifact_id": artifact.id, "step": step_number, "error": message})
        evidence = capture_failure_evidence(driver, step_number)
        if checkpoint_evidence is not None:
            evidence.update(checkpoint_evidence)
        return ReplayResult(success=False, status="hard_failure", error=message, step_failed=step_number,
                            logs=logs, evidence=evidence,
                            duration_seconds=time.monotonic() - started)

    def business_outcome(step_number: int, classification: ErrorClassification) -> ReplayResult:
        """Return a legitimate negative business result separately from failures.

        Args:
            step_number: Step where the known outcome appeared.
            classification: Matched known error rule.

        Returns:
            Completed, non-successful business outcome without a system error.
        """
        logs.append(f"Step {step_number}: business outcome {classification.business_outcome}")
        LOGGER.info("replay_business_outcome", extra={"event": "replay_business_outcome", "artifact_id": artifact.id, "step": step_number, "business_outcome": classification.business_outcome})
        return ReplayResult(success=False, status="business_outcome", business_outcome=classification.business_outcome,
                            logs=logs, duration_seconds=time.monotonic() - started)

    def maybe_recover(
        step_number: int, classification: ErrorClassification, resolver: LocatorResolver,
        attempt: int, implicit_safe: bool,
    ) -> bool:
        """Perform at most the configured number of safe retries for one phase.

        Args:
            step_number: Failing step or verification phase.
            classification: Classified recoverable condition.
            resolver: Bounded element resolver for explicit repair.
            attempt: Number of prior retries already used.
            implicit_safe: Whether repeating without a repair is safe.

        Returns:
            True if the caller should retry the phase.
        """
        if not classification.should_continue or attempt >= recovery_limit:
            return False
        if classification.recovery_action is not None:
            if not execute_recovery_action(driver, classification.recovery_action, resolver):
                return False
        elif not implicit_safe:
            return False
        logs.append(f"Step {step_number}: recovery completed; retry {attempt + 1}/{recovery_limit}")
        LOGGER.info("replay_retry", extra={"event": "replay_retry", "artifact_id": artifact.id, "step": step_number, "retry": attempt + 1})
        return True

    try:
        _navigate(driver, artifact.target_url, max_wait_ms / 1000)
        logs.append("Navigation to artifact target completed")
        LOGGER.info("replay_navigated", extra={"event": "replay_navigated", "artifact_id": artifact.id})
    except (WebDriverException, ValueError) as exc:
        return failure(0, f"Initial navigation failed: {type(exc).__name__}")

    for step in steps:
        timeout_ms = min(max_wait_ms, step.timeout_ms or max_wait_ms)
        resolver = LocatorResolver(wait_timeout=timeout_ms / 1000)
        if step.action == "navigate" and step.value:
            step = replace(step, value=urljoin(artifact.target_url, step.value))
        for attempt in range(recovery_limit + 1):
            phase = "action"
            try:
                action_result = execute_action(driver, step, resolver, raise_on_error=True)
                phase = "outcome"
                if not wait_for_outcome(driver, step.expected_outcome, timeout_ms):
                    raise TimeoutException("Expected outcome timed out")
            except (ElementNotFoundError, WebDriverException, ValueError) as exc:
                classification = detect_and_classify_error(driver, artifact, step.step_number, exc)
                if classification.classification == "expected_business_outcome":
                    return business_outcome(step.step_number, classification)
                if maybe_recover(step.step_number, classification, resolver, attempt, step.action in _IMPLICIT_RETRY_ACTIONS and phase == "action"):
                    continue
                default = (
                    f"{step.action} failed: {type(exc).__name__}" if phase == "action"
                    else "Expected outcome timed out" if isinstance(exc, TimeoutException)
                    else f"Outcome check failed: {type(exc).__name__}"
                )
                message = classification.error_message if classification.classification == "hard_failure" and classification.error_message and artifact.known_errors else default
                return failure(step.step_number, message)
            logs.append(f"Step {step.step_number}: {step.action} succeeded in {action_result['duration_ms']} ms")
            break

    final_index = len(steps) + 1
    final_resolver = LocatorResolver(wait_timeout=max_wait_ms / 1000)
    verifier = CheckpointVerifier()
    for attempt in range(recovery_limit + 1):
        try:
            verifier.verify(driver, checkpoint, final_resolver, timeout=max_wait_ms / 1000, step_number=final_index)
        except (CheckpointVerificationError, ValueError, WebDriverException) as exc:
            classification = detect_and_classify_error(driver, artifact, final_index, exc)
            if classification.classification == "expected_business_outcome":
                return business_outcome(final_index, classification)
            if maybe_recover(final_index, classification, final_resolver, attempt, False):
                continue
            default = f"Success checkpoint failed: {checkpoint.error_message}" if isinstance(exc, CheckpointVerificationError) else f"Success checkpoint error: {type(exc).__name__}"
            return failure(final_index, classification.error_message if classification.classification == "hard_failure" and classification.error_message and artifact.known_errors else default,
                           exc.evidence if isinstance(exc, CheckpointVerificationError) else None)
        break
    logs.append("Success checkpoint passed")

    # A weak recorded checkpoint can still pass on a negative-result page.
    # Detect explicit business and hard-failure banners before returning data.
    observed = detect_known_error(driver, artifact, final_index) if artifact.known_errors else None
    if observed is not None:
        if observed.classification == "expected_business_outcome":
            return business_outcome(final_index, observed)
        if observed.classification == "hard_failure":
            return failure(final_index, observed.error_message or "Known hard failure observed")

    outputs: dict[str, Any] = {}
    for output in artifact.outputs:
        resolved_output = replace(
            output,
            extraction_locator=_substitute_locator(output.extraction_locator, input_params) if output.extraction_locator else None,
        )
        output_resolver = LocatorResolver(wait_timeout=max_wait_ms / 1000)
        for attempt in range(recovery_limit + 1):
            try:
                outputs[output.name] = extract_output(driver, resolved_output, output_resolver)
            except (ElementNotFoundError, WebDriverException, ValueError) as exc:
                classification = detect_and_classify_error(driver, artifact, final_index, exc)
                if classification.classification == "expected_business_outcome":
                    return business_outcome(final_index, classification)
                if maybe_recover(final_index, classification, output_resolver, attempt, True):
                    continue
                default = f"Output {output.name} failed: {type(exc).__name__}"
                return failure(final_index, classification.error_message if classification.classification == "hard_failure" and classification.error_message and artifact.known_errors else default)
            break
        logs.append(f"Output {output.name} extracted")
        LOGGER.info("replay_output", extra={"event": "replay_output", "artifact_id": artifact.id, "field": output.name})
    duration = time.monotonic() - started
    LOGGER.info("replay_completed", extra={"event": "replay_completed", "artifact_id": artifact.id, "duration_seconds": duration, "output_count": len(outputs)})
    return ReplayResult(success=True, status="success", outputs=outputs, logs=logs, duration_seconds=duration)
