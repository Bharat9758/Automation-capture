"""Assess cumulative browser-action risk without inspecting typed values."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from typing import Any, Mapping

from src.artifact.schema import ActionStep, AutomationArtifact, Locator
from src.logging import get_logger


LOGGER = get_logger(__name__)
_DEFAULT_LOCATOR = {"confirm": 1, "delete": 2, "permanently": 2, "close_account": 3,
                    "transfer": 2, "final": 1}
_DEFAULT_REASONING = {"confirm": 1, "irreversible": 2, "delete": 2, "close": 2,
                      "transfer": 2, "permanent": 2, "final": 1}
_WRITE_ACTIONS = frozenset({"click", "type"})
_IRREVERSIBLE = frozenset({"delete", "close", "close_account", "transfer", "remove",
                          "irreversible", "permanent", "permanently"})


class RiskLevel(IntEnum):
    """Ordered action risk levels used for replay policy decisions."""

    SAFE = 0
    CAUTION = 1
    RISKY = 2
    CRITICAL = 3


@dataclass(frozen=True, kw_only=True)
class ActionRiskAssessment:
    """Safe, value-free explanation of the action risk decision."""

    risk_level: RiskLevel
    reasoning: str
    evidence: list[str]
    recommended_action: str
    requires_approval: bool
    escalation_threshold: bool


@dataclass(frozen=True, kw_only=True)
class RiskApproval:
    """Explicit operator authorization for exactly one replay step."""

    step_number: int
    risk_level: RiskLevel
    approved_by: str
    approved_at: str
    approval_method: str

    def __post_init__(self) -> None:
        """Reject unauthored or timezone-naive approval records.

        Raises:
            ValueError: Approval metadata is incomplete or malformed.
        """
        if isinstance(self.step_number, bool) or not isinstance(self.step_number, int) or self.step_number < 1:
            raise ValueError("Approval step must be positive")
        if not isinstance(self.risk_level, RiskLevel):
            raise ValueError("Approval risk level is invalid")
        if not isinstance(self.approved_by, str) or not self.approved_by.strip():
            raise ValueError("Approval requires an operator ID")
        if self.approval_method not in {"human_interactive", "auto_approved", "session_preapproved"}:
            raise ValueError("Approval method is invalid")
        try:
            timestamp = datetime.fromisoformat(self.approved_at.replace("Z", "+00:00"))
        except (AttributeError, ValueError) as exc:
            raise ValueError("Approval timestamp must be ISO 8601") from exc
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            raise ValueError("Approval timestamp must have a timezone")


def _config_keywords(name: str, default: Mapping[str, int]) -> dict[str, int]:
    """Read strictly validated keyword scores from optional JSON configuration.

    Args:
        name: Environment variable name.
        default: Built-in example policy.

    Returns:
        Validated keyword-to-points mapping.

    Raises:
        ValueError: Invalid policy overrides.
    """
    raw = os.environ.get(name)
    try:
        mapping = json.loads(raw) if raw is not None else dict(default)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} must be valid JSON") from exc
    if (not isinstance(mapping, dict) or any(not isinstance(key, str) or not key.strip()
            or isinstance(points, bool) or not isinstance(points, int) or points < 1
            for key, points in mapping.items())):
        raise ValueError(f"{name} must map nonempty keywords to positive integers")
    return mapping


def _positive_setting(name: str, default: int) -> int:
    """Read a positive policy threshold.

    Args:
        name: Environment variable name.
        default: Fallback threshold.

    Returns:
        Configured positive integer.
    """
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _matched_keywords(text: str, keywords: Mapping[str, int]) -> list[str]:
    """Find whole words, also recognizing snake and camel case labels.

    Args:
        text: Locator metadata or action reasoning; never typed values.
        keywords: Reviewed keyword scores.

    Returns:
        Matched keyword names without copying source text.
    """
    normalized = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", text).lower()
    return [word for word in keywords if re.search(r"(?<![a-z0-9])" + re.escape(word.lower()) + r"(?![a-z0-9])", normalized)]


def get_risk_score(text: str, risk_keywords: dict[str, int]) -> int:
    """Sum the weights of distinct policy keywords appearing in text.

    Args:
        text: Reviewed text, not a typed value.
        risk_keywords: Keyword point mapping.

    Returns:
        Nonnegative cumulative score.
    """
    if not isinstance(text, str):
        raise TypeError("Risk text must be a string")
    return sum(risk_keywords[word] for word in _matched_keywords(text, risk_keywords))


def _locator_parts(locator: Locator) -> list[str]:
    """Read all candidate selectors while avoiding cyclic locator objects.

    Args:
        locator: Primary locator with optional fallbacks.

    Returns:
        Selector values in traversal order.
    """
    values: list[str] = []
    seen: set[int] = set()

    def visit(current: Locator) -> None:
        """Append one selector, then its previously unseen fallbacks.

        Args:
            current: Locator candidate.
        """
        if id(current) in seen:
            return
        seen.add(id(current))
        values.append(current.value)
        for fallback in current.fallbacks or []:
            visit(fallback)

    visit(locator)
    return values


def get_element_risk(locator: Locator) -> tuple[int, list[str]]:
    """Score every primary and fallback selector without logging selector values.

    Args:
        locator: Recorded target locator.

    Returns:
        Points and safe keyword evidence.
    """
    keywords = _config_keywords("RISK_LOCATOR_KEYWORDS", _DEFAULT_LOCATOR)
    matches = set().union(*(_matched_keywords(part, keywords) for part in _locator_parts(locator)))
    return sum(keywords[word] for word in matches), [f"locator keyword: {word}" for word in sorted(matches)]


def get_reasoning_risk(reasoning: str) -> tuple[int, list[str]]:
    """Score recorded intent using keyword names rather than copying its text.

    Args:
        reasoning: Recorded step rationale.

    Returns:
        Points and safe keyword evidence.
    """
    keywords = _config_keywords("RISK_REASONING_KEYWORDS", _DEFAULT_REASONING)
    matches = _matched_keywords(reasoning, keywords)
    return sum(keywords[word] for word in matches), [f"reasoning keyword: {word}" for word in matches]


def check_repeated_failures(previous_steps: list[ActionStep],
                            error_history: list[dict[str, Any]]) -> tuple[int, str]:
    """Quantify recent unsuccessful attempts; ignore success records.

    Args:
        previous_steps: Earlier recorded actions (for interface compatibility).
        error_history: Safe error metadata containing step_number or success=False.

    Returns:
        Added points and a value-free reason.
    """
    if not isinstance(previous_steps, list) or not isinstance(error_history, list):
        raise TypeError("Risk history must be lists")
    failures = sum(entry.get("success") is False or entry.get("classification") in {"hard_failure", "recoverable_condition"}
                   for entry in error_history if isinstance(entry, dict))
    threshold = _positive_setting("RISK_FAILURE_THRESHOLD", 3)
    if failures >= threshold:
        return 2, "Repeated failed attempts"
    if failures:
        return 1, "Earlier failed attempt"
    return 0, ""


def is_human_supplied_action(step: ActionStep, artifact: AutomationArtifact) -> bool:
    """Find matching audited interventions, without treating them as approval.

    Args:
        step: Action being classified.
        artifact: Artifact with prior human intervention records.

    Returns:
        True only for an explicitly recorded matching operator action.
    """
    for intervention in artifact.human_interventions:
        if intervention.at_step != step.step_number:
            continue
        for action in intervention.human_actions:
            locator = action.get("locator")
            if (action.get("action") == step.action
                    and (step.locator is None or isinstance(locator, dict)
                         and locator.get("strategy") == step.locator.strategy
                         and locator.get("value") == step.locator.value)):
                return True
    return False


def get_recommended_action(risk_level: RiskLevel) -> str:
    """Map an assessed level to the operator-facing replay response.

    Args:
        risk_level: Final level after all risk factors.

    Returns:
        Response recommendation.
    """
    return {
        RiskLevel.SAFE: "Execute normally, log action",
        RiskLevel.CAUTION: "Execute with careful logging, flag in session",
        RiskLevel.RISKY: "Log and warn; request approval when practical",
        RiskLevel.CRITICAL: "Escalate to human; require approval before execution",
    }[risk_level]


def classify_action_risk(step: ActionStep, artifact: AutomationArtifact, step_number: int,
                         previous_steps: list[ActionStep],
                         error_history: list[dict[str, Any]]) -> ActionRiskAssessment:
    """Classify cumulative action, locator, intent, history, and sequence risk.

    The classifier never examines ``step.value`` or browser inputs. Human
    provenance can reduce a score but does not create execution approval.

    Args:
        step: Recorded action (before placeholder substitution).
        artifact: Source artifact and human audit history.
        step_number: One-based step number.
        previous_steps: Completed recorded actions.
        error_history: Safe failure and recovery metadata.

    Returns:
        Risk grade, evidence labels, and escalation recommendation.

    Raises:
        ValueError: Step or configuration is invalid.
    """
    if not isinstance(step, ActionStep) or not isinstance(artifact, AutomationArtifact):
        raise TypeError("Valid step and artifact are required")
    if isinstance(step_number, bool) or not isinstance(step_number, int) or step_number < 1 or step_number != step.step_number:
        raise ValueError("step_number must match the one-based artifact step")
    if any(not isinstance(item, ActionStep) for item in previous_steps):
        raise TypeError("previous_steps must contain ActionStep objects")
    score = 0
    evidence: list[str] = []
    if step.action == "type":
        score += 1
        evidence.append("data entry action")
    elif step.action not in {"click", "navigate", "wait", "read_text", "checkpoint"}:
        raise ValueError("Unsupported action")
    if step.locator:
        points, factors = get_element_risk(step.locator)
        score += points
        evidence.extend(factors)
    points, factors = get_reasoning_risk(step.reasoning)
    score += points
    evidence.extend(factors)
    intrinsic_score = score
    failure_points, failure_reason = check_repeated_failures(previous_steps, error_history)
    score += failure_points
    if failure_reason:
        evidence.append(failure_reason)
    if any(item.get("recovered") is True for item in error_history if isinstance(item, dict)):
        score += 1
        evidence.append("after error recovery")
    streak = _positive_setting("RISK_WRITE_STREAK", 2)
    if step.action in _WRITE_ACTIONS and len(previous_steps) >= streak and all(
        prior.action in _WRITE_ACTIONS for prior in previous_steps[-streak:]
    ):
        score += 1
        evidence.append("multiple consecutive writes")
    if step_number <= _positive_setting("RISK_EARLY_STEP_LIMIT", 2):
        score += 1
        evidence.append("early in sequence")
    matched = {factor.split(": ", 1)[1] for factor in evidence
               if factor.startswith(("locator keyword: ", "reasoning keyword: "))}
    irreversible = bool(_IRREVERSIBLE.intersection(matched))
    if is_human_supplied_action(step, artifact):
        score = max(0, score - 1)
        evidence.append("human-supplied action; explicit approval still required")
    if step_number >= _positive_setting("RISK_LATE_STEP_START", 50):
        score = max(0, score - 1)
        evidence.append("late in sequence")
    # Time and provenance cannot turn an irreversible write into a routine action.
    if irreversible and step.action in _WRITE_ACTIONS:
        floor = RiskLevel.CRITICAL if intrinsic_score >= RiskLevel.CRITICAL else RiskLevel.RISKY
        score = max(score, floor)
    risky_sequence = step.action in _WRITE_ACTIONS and len(previous_steps) >= streak and all(
        prior.action in _WRITE_ACTIONS and
        (get_element_risk(prior.locator)[0] if prior.locator else 0) + get_reasoning_risk(prior.reasoning)[0] >= RiskLevel.RISKY
        for prior in previous_steps[-streak:]
    )
    if risky_sequence:
        score += 1
        evidence.append("multiple risky actions in sequence")
    level = RiskLevel(min(score, RiskLevel.CRITICAL))
    escalation = level == RiskLevel.CRITICAL or (level >= RiskLevel.RISKY and step.action in _WRITE_ACTIONS
                                               and (irreversible or bool(failure_points) or risky_sequence))
    assessment = ActionRiskAssessment(risk_level=level, reasoning="; ".join(evidence) or "Routine action",
                                      evidence=evidence, recommended_action=get_recommended_action(level),
                                      requires_approval=level == RiskLevel.CRITICAL or escalation,
                                      escalation_threshold=escalation)
    LOGGER.info("action_risk_assessed", extra={"event": "action_risk_assessed", "step": step_number,
                                              "risk_level": level.name.lower(), "factor_count": len(evidence),
                                              "escalate": escalation})
    return assessment
