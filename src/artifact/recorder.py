"""Convert successful browser discovery traces into reusable artifacts."""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from jsonschema import ValidationError as JSONValidationError
from pydantic import ValidationError as PydanticValidationError

from src.artifact.schema import ActionStep, AutomationArtifact, Checkpoint, InputParameter, Locator, OutputField
from src.logging import get_logger


LOGGER = get_logger(__name__)
_PLACEHOLDER = re.compile(r"^\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_NAME_ATTRIBUTE = re.compile(r"(?:id|name|data-testid)\s*=\s*['\"]([^'\"]+)['\"]")
_SIMPLE_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
_INPUT_ACTIONS = {"type"}
_READ_ACTIONS = {"read_text"}
_SUPPORTED_ACTIONS = {"click", "type", "navigate", "read_text", "wait", "wait_for_element", "checkpoint"}
_LOCATOR_ACTIONS = {"click", "type", "read_text", "wait", "wait_for_element"}


def _slug(value: str, fallback: str) -> str:
    """Make a stable Python-style field name from a label.

    Args:
        value: Selector or text to normalize.
        fallback: Name to use if normalization yields nothing.

    Returns:
        Lowercase identifier.
    """
    name = re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_").lower()
    if not name:
        return fallback
    return name if not name[0].isdigit() else f"field_{name}"


def _selector_name(step: dict[str, Any], fallback: str) -> str:
    """Derive a parameter or output name from an observed selector.

    Args:
        step: Agent step or mock trace step.
        fallback: Name when the selector carries no usable signal.

    Returns:
        A normalized identifier.
    """
    locator = step.get("locator")
    locator_value = locator.value if isinstance(locator, Locator) else (locator.get("value") if isinstance(locator, dict) else "")
    selector = step.get("selector") or locator_value
    if not isinstance(selector, str) or not selector.strip():
        return fallback
    selector = selector.strip()
    if step.get("locator_type") == "id" or (selector.startswith("#") and _SIMPLE_ID.fullmatch(selector[1:])):
        return _slug(selector.lstrip("#"), fallback)
    attribute = _NAME_ATTRIBUTE.search(selector)
    if attribute:
        return _slug(attribute.group(1), fallback)
    if selector.startswith(".") and _SIMPLE_ID.fullmatch(selector[1:]):
        return _slug(selector[1:], fallback)
    if step.get("locator_type") in {"text", "aria_label"}:
        return _slug(selector, fallback)
    return fallback


def _parameter_name(step: dict[str, Any], index: int) -> str:
    """Choose a stable input name without persisting literal typed data.

    Args:
        step: Successful typing step.
        index: Position used if no name is discoverable.

    Returns:
        Caller-facing input name.
    """
    value = step.get("value")
    if isinstance(value, str) and (match := _PLACEHOLDER.fullmatch(value)):
        return match.group(1)
    hint = step.get("parameter_name")
    if isinstance(hint, str) and hint.strip():
        return _slug(hint, f"input_{index}")
    return _selector_name(step, f"input_{index}")


def _input_type(name: str, step: dict[str, Any]) -> str:
    """Infer an input type without treating numeric identifiers as amounts.

    Args:
        name: Derived input name.
        step: Recorded typing action.

    Returns:
        string, number, or date.
    """
    declared = step.get("input_type") or step.get("result", {}).get("input_type")
    if declared in {"string", "number", "date"}:
        return declared
    tokens = set(name.lower().split("_"))
    if tokens & {"date", "dob", "birthday"}:
        return "date"
    if tokens & {"amount", "quantity", "count", "price", "total"}:
        return "number"
    return "string"


def classify_observed_value(value: str) -> str:
    """Classify an extracted value without retaining its contents.

    Args:
        value: Text observed in the browser.

    Returns:
        A supported output type.
    """
    candidate = value.strip()
    if candidate.startswith(("[", "{")):
        try:
            parsed = json.loads(candidate)
        except ValueError:
            pass
        else:
            if isinstance(parsed, list):
                return "list"
            if isinstance(parsed, dict):
                return "object"
    if candidate:
        try:
            number = Decimal(candidate.replace(",", "").lstrip("$€£"))
        except InvalidOperation:
            pass
        else:
            if number.is_finite():
                return "number"
    return "string"


def _step_result(step: dict[str, Any]) -> dict[str, Any]:
    """Read a structured action result from a trace step.

    Args:
        step: Agent step.

    Returns:
        Valid result dictionary.

    Raises:
        ValueError: If the step lacks a successful action result.
    """
    result = step.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("success"), bool):
        raise ValueError("Every discovery step needs an explicit boolean result.success")
    return result


def infer_input_parameters(steps: list[dict[str, Any]]) -> list[InputParameter]:
    """Infer unique typed placeholders from early successful typing steps.

    Args:
        steps: Agent trace ordered by execution.

    Returns:
        Required input parameters with placeholder examples, never actual values.

    Raises:
        ValueError: If the configured scan limit is invalid.
    """
    limit = int(os.environ.get("RECORDER_PARAMETER_SCAN_LIMIT", "8"))
    if limit <= 0:
        raise ValueError("RECORDER_PARAMETER_SCAN_LIMIT must be positive")
    inputs: dict[str, InputParameter] = {}
    for index, step in enumerate(steps[:limit], start=1):
        if step.get("action") not in _INPUT_ACTIONS or not _step_result(step)["success"]:
            continue
        name = _parameter_name(step, index)
        if name not in inputs:
            inputs[name] = InputParameter(
                name=name, type=_input_type(name, step), description=f"Value for {name.replace('_', ' ')}",
                required=True, example="{" + name + "}",
            )
    return list(inputs.values())


def infer_output_fields(goal: str, steps: list[dict[str, Any]], agent_logs: list[dict[str, Any]]) -> list[OutputField]:
    """Infer extractable outputs from successful read_text steps.

    Args:
        goal: Discovery goal used for fallback names and descriptions.
        steps: Agent action trace.
        agent_logs: Optional structured output observations.

    Returns:
        Output fields whose extraction locators were actually observed.
    """
    outputs: dict[str, OutputField] = {}
    goal_name = _slug(re.sub(r"^(?:read|get|extract|retrieve|find)\s+", "", goal.strip(), flags=re.I), "output")
    for index, step in enumerate(steps, start=1):
        if step.get("action") not in _READ_ACTIONS or not _step_result(step)["success"]:
            continue
        result = _step_result(step)
        name = _selector_name(step, goal_name if goal_name != "output" else f"output_{index}")
        if name in {"body", "main", "div", "span", "p"}:
            name = goal_name
        if name in outputs:
            continue
        observed_type = result.get("observed_type")
        if observed_type not in {"string", "number", "list", "object"}:
            observed_value = result.get("text")
            if not isinstance(observed_value, str):
                observed_value = next((log.get("value") for log in agent_logs if log.get("event") == "output_observed" and log.get("number") == step.get("number") and isinstance(log.get("value"), str)), "")
            observed_type = classify_observed_value(observed_value)
        locator = _locator_from_step(step)
        outputs[name] = OutputField(
            name=name, type=observed_type, description=f"Value read for goal: {goal.strip()}",
            extraction_locator=locator,
        )
    return list(outputs.values())


def _locator_from_step(step: dict[str, Any]) -> Locator | None:
    """Build a schema locator from an observed step.

    Args:
        step: Agent action trace.

    Returns:
        Locator, or None for actions without selectors.

    Raises:
        ValueError: If a required locator is incomplete.
    """
    existing = step.get("locator")
    if isinstance(existing, Locator):
        return existing
    if isinstance(existing, dict):
        return Locator(**existing)
    selector = step.get("selector")
    if not isinstance(selector, str) or not selector.strip():
        if step.get("action") in _LOCATOR_ACTIONS:
            raise ValueError(f"{step.get('action')} requires a selector")
        return None
    strategy = step.get("locator_type", "css")
    return Locator(strategy=strategy, value=selector, robustness_notes="Observed during a successful discovery action")


def add_robustness_notes(artifact: AutomationArtifact) -> None:
    """Annotate locators and add only equivalent, defensible fallbacks.

    Args:
        artifact: Artifact to update in place.
    """
    locators = [step.locator for step in artifact.steps if step.locator is not None]
    locators.extend(field.extraction_locator for field in artifact.outputs if field.extraction_locator is not None)
    if artifact.success_checkpoint.locator is not None:
        locators.append(artifact.success_checkpoint.locator)
    for locator in locators:
        if locator.strategy == "id":
            locator.robustness_notes = "An element ID is concise; verify it remains stable across sessions."
            if _SIMPLE_ID.fullmatch(locator.value) and not locator.fallbacks:
                locator.fallbacks = [Locator(strategy="css", value=f"#{locator.value}", robustness_notes="Equivalent CSS ID lookup")]
        elif locator.strategy == "css" and (match := re.fullmatch(r"#([A-Za-z_][A-Za-z0-9_-]*)", locator.value)):
            locator.robustness_notes = "A CSS ID selector targets one element; verify the ID is stable."
            if not locator.fallbacks:
                locator.fallbacks = [Locator(strategy="id", value=match.group(1), robustness_notes="Equivalent WebDriver ID lookup")]
        elif locator.strategy == "css" and "data-test" in locator.value:
            locator.robustness_notes = "A test attribute is usually less affected by layout changes; confirm it is maintained."
        elif locator.strategy == "xpath":
            locator.robustness_notes = "XPath can depend on document structure; prefer a stable ID or test attribute if available."
        elif locator.strategy == "text":
            locator.robustness_notes = "Visible text may change with copy or localization; verify an accessible label before adding it as a fallback."
        elif locator.strategy == "aria_label":
            locator.robustness_notes = "An accessible label communicates purpose but can change with localization."
        else:
            locator.robustness_notes = "CSS selector observed in discovery; review for dynamic classes and hierarchy."


def _validation_error(exc: Exception) -> str:
    """Summarize a validation error without echoing potentially private values.

    Args:
        exc: Schema or Pydantic exception.

    Returns:
        Safe field path and error category.
    """
    if isinstance(exc, JSONValidationError):
        path = ".".join(map(str, exc.absolute_path)) or "artifact"
        return f"{path}: {exc.validator or 'schema'} validation failed"
    if isinstance(exc, PydanticValidationError):
        first = exc.errors()[0]
        path = ".".join(map(str, first.get("loc", ()))) or "artifact"
        return f"{path}: {first['type']} validation failed"
    return f"Artifact validation failed: {type(exc).__name__}"


def validate_artifact(artifact: AutomationArtifact) -> tuple[bool, str]:
    """Check schema, replay placeholders, and extractable outputs.

    Args:
        artifact: Candidate artifact.

    Returns:
        A validity flag and empty message on success, or a safe explanation.
    """
    if not isinstance(artifact, AutomationArtifact):
        return False, "Expected an AutomationArtifact"
    try:
        restored = AutomationArtifact.from_dict(artifact.to_dict())
        input_names = {item.name for item in restored.inputs}
        for step in restored.steps:
            if step.action == "type":
                match = _PLACEHOLDER.fullmatch(step.value or "")
                if not match or match.group(1) not in input_names:
                    return False, f"step {step.step_number}: type value must reference a declared input placeholder"
            if step.action == "navigate":
                url = urlsplit(step.value or "")
                if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                    return False, f"step {step.step_number}: navigate requires an absolute HTTP(S) URL"
        for output in restored.outputs:
            if output.extraction_locator is None:
                return False, f"output {output.name}: extraction locator is required"
        if restored.success_checkpoint.condition == "url_matches":
            re.compile(restored.success_checkpoint.expected_value or "")
    except Exception as exc:
        return False, _validation_error(exc)
    return True, ""


def record_discovery_run(
    steps: list[dict[str, Any]], agent_logs: list[dict[str, Any]], goal: str, target_url: str
) -> AutomationArtifact:
    """Record a successful agent trace as a validated reusable artifact.

    Literal typed values are replaced with named input placeholders. Failed
    attempts are noted as known errors and excluded from replay steps.

    Args:
        steps: Executed agent actions.
        agent_logs: Structured agent events including successful loop termination.
        goal: User-requested outcome.
        target_url: Initial HTTP(S) base URL.

    Returns:
        Complete artifact ready for JSON serialization and saving.

    Raises:
        ValueError: If success cannot be verified or the trace is not replayable.
    """
    if not isinstance(steps, list) or not isinstance(agent_logs, list) or not isinstance(goal, str) or not goal.strip():
        raise ValueError("A goal, step list, and log list are required")
    if not all(isinstance(step, dict) for step in steps) or not all(isinstance(log, dict) for log in agent_logs):
        raise ValueError("Steps and logs must contain dictionaries")
    if not any(log.get("event") == "loop_finished" and log.get("success") is True for log in agent_logs):
        raise ValueError("Only successful completed discovery runs can be recorded")
    if not isinstance(target_url, str) or not target_url.strip():
        raise ValueError("A base target URL is required")
    replay_steps: list[ActionStep] = []
    known_errors: dict[str, dict[str, Any]] = {}
    input_parameters = infer_input_parameters(steps)
    for index, step in enumerate(steps, start=1):
        action = step.get("action")
        if action not in _SUPPORTED_ACTIONS:
            raise ValueError(f"Unsupported discovered action at position {index}")
        result = _step_result(step)
        if not result["success"]:
            raw_error = result.get("error")
            error_type = raw_error if isinstance(raw_error, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", raw_error) else "ActionFailed"
            known_errors[f"attempt_{index}"] = {"action": action, "error_type": error_type}
            continue
        normalized_action = "wait" if action == "wait_for_element" else action
        locator = _locator_from_step(step)
        value: str | None = None
        if action == "type":
            name = _parameter_name(step, index)
            if name not in {item.name for item in input_parameters}:
                raise ValueError(f"Typed input at position {index} falls beyond the configured scan limit")
            value = "{" + name + "}"
        elif action == "navigate":
            value = result.get("url") or step.get("value")
            if not isinstance(value, str) or urlsplit(value).scheme not in {"http", "https"}:
                raise ValueError(f"Navigate step at position {index} has no safe absolute URL")
        timeout_ms = step.get("timeout_ms", int(os.environ.get("ARTIFACT_STEP_TIMEOUT_MS", "10000")))
        replay_steps.append(ActionStep(
            step_number=len(replay_steps) + 1, action=normalized_action, locator=locator, value=value,
            reasoning=step.get("reasoning") or f"Successful {action} observed during discovery",
            expected_outcome=step.get("expected_outcome") or f"{action} completes without error",
            timeout_ms=timeout_ms,
        ))
    if not replay_steps:
        raise ValueError("The successful run contains no replayable actions")
    outputs = infer_output_fields(goal, steps, agent_logs)
    if re.match(r"^\s*(?:read|get|extract|retrieve)\b", goal, flags=re.I) and not outputs:
        raise ValueError("The goal requests an output but no successful read_text step exists")
    read_locators = [step.locator for step in replay_steps if step.action == "read_text" and step.locator is not None]
    if read_locators:
        checkpoint = Checkpoint(
            condition="element_visible", locator=read_locators[-1], expected_value=None,
            error_message="Expected output element was not visible after replay",
        )
    else:
        final_url = next((log.get("final_url") for log in reversed(agent_logs) if log.get("event") == "loop_finished" and isinstance(log.get("final_url"), str) and log.get("final_url")), target_url)
        checkpoint = Checkpoint(
            condition="url_matches", locator=None, expected_value="^" + re.escape(final_url) + "$",
            error_message="Expected destination URL was not reached after replay",
        )
    run_id = next((log.get("run_id") for log in agent_logs if isinstance(log.get("run_id"), str) and log.get("run_id")), str(uuid4()))
    now = datetime.now(timezone.utc).isoformat()
    artifact = AutomationArtifact(
        id=str(uuid4()), name=goal.strip(), version=os.environ.get("ARTIFACT_INITIAL_VERSION", "1.0.0"),
        description=f"Discovered workflow for: {goal.strip()}", created_at=now, updated_at=now,
        created_by=os.environ.get("ARTIFACT_CREATED_BY", "automation-capture/agent"),
        target_url=target_url, inputs=input_parameters, outputs=outputs, steps=replay_steps,
        success_checkpoint=checkpoint, known_errors=known_errors, discovery_run_id=run_id,
        success_rate=None,
    )
    add_robustness_notes(artifact)
    valid, error = validate_artifact(artifact)
    if not valid:
        raise ValueError(error)
    LOGGER.info("artifact_recorded", extra={"event": "artifact_recorded", "artifact_id": artifact.id, "run_id": run_id, "step_count": len(replay_steps)})
    return artifact
