"""Recognize replay states that require human review before continuing."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from selenium.common.exceptions import StaleElementReferenceException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver

from src.artifact.schema import ActionStep, AutomationArtifact, Locator
from src.logging import get_logger
from src.replay.locator_strategy import LocatorResolver


LOGGER = get_logger(__name__)


def _positive_setting(name: str, default: int) -> int:
    """Read a positive integer setting shared by state and context helpers.

    Args:
        name: Environment variable name.
        default: Default positive value.

    Returns:
        Configured positive integer.

    Raises:
        ValueError: If the setting is not a positive integer.
    """
    try:
        value = int(os.environ.get(name, str(default)))
        if value > 0:
            return value
    except ValueError:
        pass
    raise ValueError(f"{name} must be a positive integer")


@dataclass(frozen=True, kw_only=True)
class StuckState:
    """Describe a paused replay and provide context for a human handoff."""

    is_stuck: bool
    reason: str = ""
    current_step: int = 0
    current_screenshot: bytes = b""
    current_dom: str = ""
    recommended_action: str = ""
    escalation_context: dict[str, Any] = field(default_factory=dict)


def calculate_state_signature(state: dict[str, Any]) -> str:
    """Hash stable page signals without storing screenshots or typed inputs.

    Args:
        state: Snapshot containing URL, title, visible text, and element count.

    Returns:
        SHA-256 hexadecimal signature of normalized page signals.

    Raises:
        TypeError: If state is not a mapping.
    """
    if not isinstance(state, dict):
        raise TypeError("state must be a dictionary")
    limit = _positive_setting("STUCK_SIGNATURE_TEXT_CHARS", 500)
    text = state.get("visible_text", state.get("page_text", ""))
    signals = {
        "url": str(state.get("current_url") or state.get("url") or ""),
        "title": str(state.get("title") or ""),
        "visible_text": str(text or "")[:limit],
        "element_count": state.get("element_count", 0),
    }
    payload = json.dumps(signals, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def is_state_repeated(current_signature: str, history: list[str], threshold: int = 3) -> bool:
    """Check whether a signature occurs often enough in the supplied series.

    The history must include the current signature; callers with prior-only
    history append the current signature before this check.

    Args:
        current_signature: Current page signature.
        history: Series including the current observation.
        threshold: Number of occurrences required to flag a loop.

    Returns:
        True when the signature occurs at least threshold times.

    Raises:
        ValueError: If threshold is not positive.
    """
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold <= 0:
        raise ValueError("threshold must be a positive integer")
    return bool(current_signature) and history.count(current_signature) >= threshold


def is_risky_action(step: ActionStep, artifact: AutomationArtifact) -> bool:
    """Flag a potentially irreversible click using configured action words.

    Args:
        step: Recorded action under consideration.
        artifact: Source artifact; available for future policy extensions.

    Returns:
        True if a click's reasoning, expected result, or locator names a risk.
    """
    if step.action != "click":
        return False
    words = [item.strip() for item in os.environ.get(
        "STUCK_RISKY_KEYWORDS", "delete,close,transfer,remove,unsubscribe"
    ).split(",") if item.strip()]
    if not words:
        return False
    target = " ".join((step.reasoning, step.expected_outcome, step.locator.value if step.locator else ""))
    pattern = r"\b(?:" + "|".join(re.escape(word) for word in words) + r")\b"
    return re.search(pattern, target, flags=re.IGNORECASE) is not None


def get_step_context(artifact: AutomationArtifact, step_index: int) -> dict[str, Any]:
    """Describe the preceding, current, and next recorded steps without values.

    Step indices are zero-based; -1 denotes before the first step.

    Args:
        artifact: Replay artifact.
        step_index: Index of most recently attempted step.

    Returns:
        Action and reasoning for nearby steps plus artifact position.
    """
    def summarize(index: int) -> dict[str, Any] | None:
        """Summarize a step without its potentially sensitive typed value.

        Args:
            index: Zero-based artifact step index.

        Returns:
            Action details, or None when outside the artifact.
        """
        if index < 0 or index >= len(artifact.steps):
            return None
        step = artifact.steps[index]
        return {"step_number": step.step_number, "action": step.action, "reasoning": step.reasoning,
                "expected_outcome": step.expected_outcome}

    return {"artifact_id": artifact.id, "artifact_name": artifact.name,
            "step_index": step_index, "total_steps": len(artifact.steps),
            "previous": summarize(step_index - 1), "current": summarize(step_index),
            "next": summarize(step_index + 1)}


def _by(locator: Locator) -> tuple[str, str]:
    """Convert a candidate locator to Selenium's find_elements arguments.

    Args:
        locator: One supported selector.

    Returns:
        Selenium By strategy and safely escaped query.
    """
    if locator.strategy == "text":
        value = LocatorResolver._xpath_literal(" ".join(locator.value.split()).lower())
        return By.XPATH, "//*[translate(normalize-space(text()), 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')=" + value + "]"
    if locator.strategy == "aria_label":
        return By.XPATH, f"//*[@aria-label={LocatorResolver._xpath_literal(locator.value)}]"
    return {"css": By.CSS_SELECTOR, "xpath": By.XPATH, "id": By.ID}[locator.strategy], locator.value


def count_matching_elements(driver: WebDriver, locator: Locator, locator_resolver: LocatorResolver) -> int:
    """Count visible matches for the first usable locator in fallback order.

    Args:
        driver: Active browser.
        locator: Primary locator and alternatives.
        locator_resolver: Resolver providing the ordered fallback strategy.

    Returns:
        Number of visible matches, or zero when no candidate can be queried.
    """
    for candidate in LocatorResolver._candidates(locator):
        try:
            elements = driver.find_elements(*_by(candidate))
            visible = sum(bool(element.is_displayed()) for element in elements)
            if visible:
                return visible
        except (WebDriverException, TypeError, StaleElementReferenceException):
            continue
    return 0


def record_step_in_history(step_history: list[dict[str, Any]], new_step: dict[str, Any]) -> None:
    """Append a step observation while retaining only recent history.

    Args:
        step_history: Mutable series of step records.
        new_step: Recorded step data.

    Raises:
        TypeError: If history or new step is not correctly shaped.
    """
    if not isinstance(step_history, list) or not isinstance(new_step, dict):
        raise TypeError("step_history must be a list and new_step must be a dictionary")
    limit = _positive_setting("STUCK_HISTORY_LIMIT", 20)
    step_history.append(new_step.copy())
    del step_history[:-limit]


class StateTracker:
    """Keep bounded page signatures to detect repetition during replay."""

    def __init__(self, threshold: int | None = None) -> None:
        """Configure repetition threshold and bounded history.

        Args:
            threshold: Override STUCK_REPEAT_THRESHOLD.

        Raises:
            ValueError: If threshold is not positive.
        """
        self.threshold = threshold if threshold is not None else _positive_setting("STUCK_REPEAT_THRESHOLD", 3)
        if isinstance(self.threshold, bool) or not isinstance(self.threshold, int) or self.threshold <= 0:
            raise ValueError("threshold must be a positive integer")
        self._history: list[dict[str, Any]] = []

    def add_state(self, state: dict[str, Any], step_num: int) -> None:
        """Record a page signature without retaining page text or screenshots.

        Args:
            state: Browser signals to hash.
            step_num: One-based artifact step number.
        """
        record_step_in_history(self._history, {"step_number": step_num,
                                               "state_signature": calculate_state_signature(state)})

    def get_history(self) -> list[dict[str, Any]]:
        """Return independent copies of recent signature records.

        Returns:
            Bounded sequence of step numbers and signatures.
        """
        return [item.copy() for item in self._history]

    def is_looping(self) -> bool:
        """Check the current signature against the recorded series.

        Returns:
            True if the latest state occurred at least threshold times.
        """
        if not self._history:
            return False
        current = self._history[-1]["state_signature"]
        return is_state_repeated(current, [item["state_signature"] for item in self._history], self.threshold)


def _history_context(step_history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return safe metadata from recent steps without typed browser values.

    Args:
        step_history: Prior step records.

    Returns:
        Bounded list of step numbers, actions, and signatures.
    """
    limit = _positive_setting("STUCK_CONTEXT_STEPS", 5)
    allowed = {"step_number", "action", "success", "state_signature"}
    return [{key: value for key, value in step.items() if key in allowed} for step in step_history[-limit:]]


def detect_stuck_state(
    artifact: AutomationArtifact, step_index: int, current_state: dict[str, Any],
    error_classification: dict[str, Any], step_history: list[dict[str, Any]],
) -> StuckState:
    """Decide whether browser replay should pause for a human.

    Step index is zero-based and refers to the last attempted step; -1 means
    replay is about to execute its first step. Prior history excludes the
    current snapshot, which is included once in the repetition check.

    Args:
        artifact: Validated replay artifact.
        step_index: Last attempted step index.
        current_state: Screenshot, DOM, URL, title, text, and match counts.
        error_classification: Error category or explicit help flag.
        step_history: Prior bounded signature and action records.

    Returns:
        A complete stuck-state decision and handoff context.
    """
    if not isinstance(current_state, dict) or not isinstance(step_history, list):
        raise TypeError("current_state must be a dict and step_history a list")
    classification = error_classification if isinstance(error_classification, dict) else {}
    next_index = step_index + 1
    signature = calculate_state_signature(current_state)
    prior_signatures = [item.get("state_signature") for item in step_history if isinstance(item, dict)]
    threshold = _positive_setting("STUCK_REPEAT_THRESHOLD", 3)
    reason, action, stopped_at = "", "", max(0, step_index + 1)
    if classification.get("human_help_requested") or current_state.get("human_help_requested"):
        reason, action = "Human requested intervention", "Awaiting human input"
    elif classification.get("classification") == "hard_failure":
        reason, action = "Hard failure during replay", "Check if page structure changed"
    elif 0 <= next_index < len(artifact.steps) and is_risky_action(artifact.steps[next_index], artifact):
        reason, action, stopped_at = "Risky action requires human approval", "Review action before proceeding", artifact.steps[next_index].step_number
    elif isinstance(current_state.get("matching_element_count"), int) and current_state["matching_element_count"] > 1:
        reason, action = "Ambiguous state - multiple matches", "Human must select correct element"
        if 0 <= next_index < len(artifact.steps):
            stopped_at = artifact.steps[next_index].step_number
    elif not current_state.get("skip_no_progress") and is_state_repeated(signature, prior_signatures + [signature], threshold):
        reason, action = "Same state repeated - no progress", "Review page for missing action"

    screenshot = current_state.get("screenshot") or current_state.get("current_screenshot") or b""
    if isinstance(screenshot, str):
        screenshot = screenshot.encode("ascii", errors="ignore")
    dom = current_state.get("dom") or current_state.get("current_dom") or ""
    context = {
        "artifact_id": artifact.id, "current_step": stopped_at, "reason": reason,
        "state_signature": signature, "step_context": get_step_context(artifact, step_index),
        "last_steps": _history_context(step_history),
        "current_url": current_state.get("current_url") or current_state.get("url"),
        "visible_elements": current_state.get("visible_elements", []),
        "matching_element_count": current_state.get("matching_element_count"),
    }
    if reason:
        LOGGER.warning("replay_stuck", extra={"event": "replay_stuck", "artifact_id": artifact.id,
                                              "step": stopped_at, "reason": reason})
    return StuckState(is_stuck=bool(reason), reason=reason, current_step=stopped_at,
                      current_screenshot=screenshot, current_dom=str(dom),
                      recommended_action=action, escalation_context=context)


def capture_escalation_context(
    driver: WebDriver, artifact: AutomationArtifact, step_index: int, reason: str,
    step_history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Capture a browser snapshot and safe recent-step metadata for review.

    Args:
        driver: Active browser.
        artifact: Artifact under replay.
        step_index: Zero-based last attempted step (-1 before the first).
        reason: Escalation reason.
        step_history: Optional recent actions and signatures.

    Returns:
        JSON-ready screenshot, full DOM, URL, title, and visible controls.
    """
    limit = _positive_setting("STUCK_VISIBLE_ELEMENTS_LIMIT", 20)
    text_limit = _positive_setting("STUCK_VISIBLE_TEXT_CHARS", 120)
    context: dict[str, Any] = {
        "artifact_id": artifact.id, "current_step": step_index + 1,
        "reason": reason, "step_context": get_step_context(artifact, step_index),
        "last_steps": _history_context(step_history or []), "visible_elements": [],
    }
    signals = {
        "screenshot": lambda: base64.b64encode(driver.get_screenshot_as_png()).decode("ascii"),
        "dom": lambda: driver.page_source,
        "current_url": lambda: driver.current_url,
        "title": lambda: driver.title,
    }
    for name, capture in signals.items():
        try:
            context[name] = capture()
        except (WebDriverException, AttributeError, TypeError, ValueError) as exc:
            context[name] = None
            context[f"{name}_error"] = type(exc).__name__
    selector = os.environ.get("STUCK_VISIBLE_SELECTOR", "button,input,select,textarea,a,[role=button]")
    try:
        for element in driver.find_elements(By.CSS_SELECTOR, selector):
            if len(context["visible_elements"]) >= limit:
                break
            try:
                if element.is_displayed():
                    context["visible_elements"].append({
                        "tag": element.tag_name,
                        "text": element.text[:text_limit],
                        "id": element.get_attribute("id"),
                        "aria_label": element.get_attribute("aria-label"),
                    })
            except (WebDriverException, AttributeError, TypeError):
                continue
    except (WebDriverException, TypeError, ValueError) as exc:
        context["visible_elements_error"] = type(exc).__name__
    return context
