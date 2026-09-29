"""Mask private metadata while retaining explicitly private browser evidence."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import tempfile
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from src.artifact.schema import ActionStep, Locator

if TYPE_CHECKING:
    from src.escalation.escalation_request import EscalationRequest
    from src.escalation.session_manager import SessionMetadata


MASK = "***REDACTED***"
SENSITIVE_FIELDS: dict[str, bool] = {name: True for name in (
    "member_id", "account_id", "customer_id", "ssn", "social_security_number", "amount",
    "balance", "email", "phone", "name", "first_name", "last_name", "dob", "date_of_birth",
    "address", "zip_code", "credit_card", "routing_number", "account_number", "password", "pin",
)}
REDACTION_PATTERNS: dict[str, str] = {
    "ssn": r"\b\d{3}-\d{2}-\d{4}\b",
    "credit_card": r"\b(?:\d{4}[\s-]?){3}\d{4}\b",
    "email": r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    "phone": r"\b\d{3}[-.\s]?\d{3}[-.\s]?\d{4}\b",
    "amount": r"\$\s?\d[\d,]*(?:\.\d{2})\b",
    "date": r"\b\d{4}-\d{2}-\d{2}\b",
    "member_id": r"\b(?:member|account|customer)[ _-]?(?:id)?\s*[:=#-]?\s*\d{3,}\b",
}
_BASIC_FIELDS = frozenset({"ssn", "social_security_number", "credit_card", "account_number",
                           "routing_number", "password", "pin", "amount", "balance", "email", "phone"})
_BASIC_PATTERNS = frozenset({"ssn", "credit_card", "email", "phone", "amount"})
_EVIDENCE_KEYS = frozenset({"screenshot", "screenshots", "current_screenshot", "dom", "dom_snapshot", "current_dom", "driver_logs"})
_STRUCTURAL_KEYS = frozenset({"session_id", "escalation_id", "discovery_run_id", "timestamp", "created_at",
                              "updated_at", "started_at", "paused_at", "resumed_at", "completed_at",
                              "approved_at", "lifecycle_state", "state", "status", "success", "is_stuck",
                              "pending", "escalate", "visible", "matching_element_count",
                              "step", "step_number", "current_step", "step_failed", "duration_ms",
                              "duration_seconds", "human_duration_seconds", "automation_duration_seconds",
                              "total_duration_seconds", "human_actions_total", "error_count", "escalations",
                              "action", "strategy", "risk_level", "classification", "event", "levelname",
                              "asctime", "taskName", "type", "required",
                              "requires_approval", "escalation_threshold", "retry", "output_count", "input_count"})


class RedactionLevel(IntEnum):
    """Increasingly conservative metadata masking levels."""

    NONE = 0
    BASIC = 1
    STRICT = 2
    PARANOID = 3


@dataclass(frozen=True, kw_only=True)
class RedactionPolicy:
    """Explicit masking rules used by serialization and structured logging."""

    redaction_level: RedactionLevel = RedactionLevel.STRICT
    fields_to_redact: list[str] = field(default_factory=lambda: list(SENSITIVE_FIELDS))
    patterns_to_redact: list[str] = field(default_factory=lambda: list(REDACTION_PATTERNS.values()))
    preserve_structure: bool = True
    preserve_screenshots: bool = True
    preserve_dom: bool = True

    def __post_init__(self) -> None:
        """Reject invalid patterns and malformed policy fields before use."""
        if not isinstance(self.redaction_level, RedactionLevel):
            raise ValueError("redaction_level must be a RedactionLevel")
        if not isinstance(self.fields_to_redact, list) or any(type(item) is not str or not item for item in self.fields_to_redact):
            raise ValueError("fields_to_redact must contain nonempty strings")
        if not isinstance(self.patterns_to_redact, list) or any(type(item) is not str or not item for item in self.patterns_to_redact):
            raise ValueError("patterns_to_redact must contain nonempty regular expressions")
        if any(type(flag) is not bool for flag in (self.preserve_structure, self.preserve_screenshots, self.preserve_dom)):
            raise ValueError("Evidence and structure flags must be booleans")
        try:
            for pattern in self.patterns_to_redact:
                re.compile(pattern)
        except re.error as exc:
            raise ValueError("Invalid redaction regex") from exc


@dataclass(frozen=True, kw_only=True)
class EvidencePreservation:
    """Separate private raw browser signals from shareable metadata."""

    raw_evidence: dict[str, Any]
    redacted_metadata: dict[str, Any]


def load_redaction_policy(level: str | RedactionLevel | None = None) -> RedactionPolicy:
    """Load a validated policy from REDACTION_* configuration.

    Args:
        level: Optional explicit level, otherwise REDACTION_LEVEL or STRICT.

    Returns:
        Parsed redaction policy.

    Raises:
        ValueError: Invalid level or unsafe production NONE setting.
    """
    candidate = level if level is not None else os.environ.get("REDACTION_LEVEL", "STRICT")
    try:
        selected = candidate if isinstance(candidate, RedactionLevel) else RedactionLevel[str(candidate).upper()]
    except KeyError as exc:
        raise ValueError("REDACTION_LEVEL must be NONE, BASIC, STRICT, or PARANOID") from exc
    if selected == RedactionLevel.NONE and os.environ.get("ALLOWLIST_MODE", "production") != "development":
        raise ValueError("NONE redaction is available only in development mode")
    try:
        fields = json.loads(os.environ["REDACTION_FIELDS"]) if "REDACTION_FIELDS" in os.environ else list(SENSITIVE_FIELDS)
        patterns = json.loads(os.environ["REDACTION_PATTERNS"]) if "REDACTION_PATTERNS" in os.environ else list(REDACTION_PATTERNS.values())
    except json.JSONDecodeError as exc:
        raise ValueError("REDACTION_FIELDS and REDACTION_PATTERNS must be JSON lists") from exc
    if not isinstance(fields, list) or not isinstance(patterns, list):
        raise ValueError("REDACTION_FIELDS and REDACTION_PATTERNS must be JSON lists")
    if selected >= RedactionLevel.STRICT:
        fields = list(dict.fromkeys([*SENSITIVE_FIELDS, *fields]))
        patterns = list(dict.fromkeys([*REDACTION_PATTERNS.values(), *patterns]))
    return RedactionPolicy(redaction_level=selected, fields_to_redact=fields, patterns_to_redact=patterns)


def is_sensitive_field(field_name: str) -> bool:
    """Recognize common private field names, including camel and snake case.

    Args:
        field_name: Metadata key.

    Returns:
        Whether the key names a protected value.
    """
    normalized = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", field_name).lower().replace("-", "_")
    return SENSITIVE_FIELDS.get(normalized, False)


def is_test_data(value: str) -> bool:
    """Recognize explicit fixture labels; never infer that an ordinary ID is fake.

    Args:
        value: Candidate fixture string.

    Returns:
        Whether a clear test/demo prefix is present.
    """
    return bool(re.fullmatch(r"(?:test_|demo_|member_\d+)[A-Za-z0-9_-]*", value, flags=re.IGNORECASE))


def _patterns_for(policy: RedactionPolicy) -> list[str]:
    """Select configured patterns appropriate to the requested level.

    Args:
        policy: Current policy.

    Returns:
        Active regular expressions.
    """
    if policy.redaction_level == RedactionLevel.NONE:
        return []
    if policy.redaction_level == RedactionLevel.BASIC:
        return [pattern for pattern in policy.patterns_to_redact
                if pattern not in REDACTION_PATTERNS.values() or
                any(pattern == REDACTION_PATTERNS[name] for name in _BASIC_PATTERNS)]
    return list(policy.patterns_to_redact)


def apply_pattern_redaction(text: str, policy: RedactionPolicy) -> str:
    """Replace configured private substrings without changing unrelated prose.

    Args:
        text: Free text to inspect.
        policy: Active pattern policy.

    Returns:
        Masked string.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    result = text
    for pattern in _patterns_for(policy):
        replacement = (lambda match: "$" + MASK) if pattern == REDACTION_PATTERNS["amount"] else MASK
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    return result


def redact_string(text: str, policy: RedactionPolicy) -> str:
    """Mask PII-like text; PARANOID hides all nonstructural free text.

    Args:
        text: Candidate metadata text.
        policy: Active policy.

    Returns:
        Masked copy.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if policy.redaction_level == RedactionLevel.NONE or text.startswith("***") and text.endswith("***"):
        return text
    if policy.redaction_level == RedactionLevel.PARANOID:
        return MASK
    result = apply_pattern_redaction(text, policy)
    if policy.redaction_level >= RedactionLevel.STRICT:
        result = re.sub(r"\b(?i:name|customer|member)\s*[:=]?\s+[A-Z][a-z]+\s+[A-Z][a-z]+\b", MASK, result)
    return result


def should_redact_in_context(field_name: str, value: str, policy: RedactionPolicy) -> bool:
    """Decide whether a scalar belongs to a protected field or pattern.

    Args:
        field_name: Metadata key.
        value: Scalar rendered as text.
        policy: Active policy.

    Returns:
        True when this value should be masked.
    """
    if policy.redaction_level == RedactionLevel.NONE:
        return False
    if isinstance(value, str) and value.startswith("***") and value.endswith("***"):
        return False
    if policy.redaction_level == RedactionLevel.PARANOID:
        return field_name not in _STRUCTURAL_KEYS
    normalized = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", field_name).lower().replace("-", "_")
    selected = {item.lower() for item in policy.fields_to_redact}
    if normalized in selected and (policy.redaction_level >= RedactionLevel.STRICT or normalized in _BASIC_FIELDS):
        return True
    if policy.redaction_level == RedactionLevel.BASIC and is_test_data(value):
        return False
    return apply_pattern_redaction(value, policy) != value


def _mask_tree(value: Any) -> Any:
    """Mask all non-null leaves while retaining nested container structure.

    Args:
        value: Protected JSON-shaped value.

    Returns:
        Masked tree.
    """
    if isinstance(value, dict):
        return {key: _mask_tree(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_mask_tree(item) for item in value]
    return None if value is None else MASK


def redact_dict(data: dict[str, Any], policy: RedactionPolicy) -> dict[str, Any]:
    """Recursively mask JSON metadata without altering the caller's objects.

    Args:
        data: Arbitrary metadata dictionary.
        policy: Active policy.

    Returns:
        Independent dictionary retaining keys unless preserve_structure is false.
    """
    if not isinstance(data, dict) or any(not isinstance(key, str) for key in data):
        raise TypeError("Metadata must be a dictionary with string keys")

    def visit(value: Any, key: str, ancestors: frozenset[int]) -> Any:
        """Visit a metadata node, rejecting cyclic objects.

        Args:
            value: Node to redact.
            key: Parent field name.
            ancestors: Active container identities.

        Returns:
            Redacted JSON-shaped node.
        """
        if isinstance(value, (dict, list, tuple)):
            if id(value) in ancestors:
                raise ValueError("Cyclic metadata cannot be redacted")
            ancestors = ancestors | {id(value)}
        if key in _EVIDENCE_KEYS and policy.redaction_level != RedactionLevel.NONE:
            preserve = policy.preserve_screenshots if "screenshot" in key else policy.preserve_dom if "dom" in key else False
            return value if preserve else _mask_tree(value)
        if key in {"input_params", "outputs", "credentials", "secrets"} and policy.redaction_level != RedactionLevel.NONE:
            return _mask_tree(value)
        if key == "locator" and isinstance(value, dict) and policy.redaction_level >= RedactionLevel.STRICT:
            return {part: (MASK if part == "value" else
                           [visit(child, "locator", ancestors) for child in item]
                           if part == "fallbacks" and isinstance(item, list) else visit(item, part, ancestors))
                    for part, item in value.items()}
        if key in _STRUCTURAL_KEYS:
            return value
        if policy.redaction_level == RedactionLevel.PARANOID and key not in _STRUCTURAL_KEYS and key:
            return _mask_tree(value)
        if isinstance(value, (dict, list, tuple)) and key and should_redact_in_context(key, "", policy):
            return _mask_tree(value)
        if value is not None and not isinstance(value, (dict, list, tuple)) and should_redact_in_context(key, str(value), policy):
            return MASK
        if isinstance(value, dict):
            return {subkey: visit(item, subkey, ancestors) for subkey, item in value.items()
                    if isinstance(subkey, str) and (policy.preserve_structure or
                    not should_redact_in_context(subkey, str(item), policy))}
        if isinstance(value, (list, tuple)):
            return [visit(item, key, ancestors) for item in value]
        if isinstance(value, str):
            return redact_string(value, policy)
        if policy.redaction_level >= RedactionLevel.STRICT and value is not None:
            if isinstance(value, (int, float, bool)) and (key.endswith(("_count", "_ms", "_seconds"))
                                                         or key in {"count", "page_count", "deleted", "actions_taken"}):
                return value
            return MASK
        return value

    return {key: visit(value, key, frozenset({id(data)})) for key, value in data.items()
            if policy.preserve_structure or not should_redact_in_context(key, str(value), policy)}


def _redact_locator(locator: Locator, policy: RedactionPolicy) -> Locator:
    """Copy an artifact locator with safe audit-only selector text.

    Args:
        locator: Recorded primary and fallback locators.
        policy: Active policy.

    Returns:
        Redacted locator tree.
    """
    if policy.redaction_level == RedactionLevel.NONE:
        return locator
    return replace(locator, value=MASK, robustness_notes=redact_string(locator.robustness_notes, policy),
                   fallbacks=[_redact_locator(item, policy) for item in locator.fallbacks] if locator.fallbacks else None)


def redact_action_step(step: ActionStep, policy: RedactionPolicy) -> ActionStep:
    """Create a non-executable audit copy without typed values or selectors.

    Args:
        step: Recorded browser action.
        policy: Active policy.

    Returns:
        Validated copy retaining action type and locator strategy.
    """
    if not isinstance(step, ActionStep):
        raise TypeError("step must be an ActionStep")
    if policy.redaction_level == RedactionLevel.NONE:
        return replace(step)
    value = MASK if step.action == "type" and step.value is not None else redact_string(step.value, policy) if step.value else step.value
    return replace(step, locator=_redact_locator(step.locator, policy) if step.locator else None,
                   value=value, reasoning=redact_string(step.reasoning, policy),
                   expected_outcome=redact_string(step.expected_outcome, policy))


def redact_escalation_request(request: EscalationRequest, policy: RedactionPolicy) -> EscalationRequest:
    """Redact handoff metadata while retaining the raw browser evidence.

    Args:
        request: EscalationRequest instance.
        policy: Active policy.

    Returns:
        Validated redacted copy with unchanged screenshot and DOM.
    """
    from src.escalation.escalation_request import EscalationRequest, validate_escalation_request

    if not isinstance(request, EscalationRequest):
        raise TypeError("request must be an EscalationRequest")
    if policy.redaction_level == RedactionLevel.NONE:
        return request
    # Phase 11 already masks every input, including undeclared names.
    params = {key: value if isinstance(value, str) and value.startswith("***") and value.endswith("***")
              else MASK for key, value in request.input_params.items()}
    context = redact_dict(request.stuck_state.escalation_context, policy)
    state = replace(request.stuck_state, reason=redact_string(request.stuck_state.reason, policy),
                    recommended_action=redact_string(request.stuck_state.recommended_action, policy),
                    escalation_context=context)
    result = replace(request, input_params=params,
                     goal=redact_string(request.goal, policy),
                     context_notes=redact_string(request.context_notes, policy),
                     reason_escalated=state.reason,
                     last_action=redact_dict(request.last_action, policy),
                     previous_steps=[redact_dict(item, policy) for item in request.previous_steps],
                     stuck_state=state)
    valid, error = validate_escalation_request(result)
    if not valid:
        raise ValueError(f"Redacted escalation invalid: {error}")
    return result


def redact_session_metadata(session: SessionMetadata, policy: RedactionPolicy) -> SessionMetadata:
    """Return a detached metadata copy with masked outputs and preserved IDs.

    Args:
        session: SessionMetadata instance.
        policy: Active policy.

    Returns:
        Redacted copy; original live session and outputs remain unchanged.
    """
    from src.escalation.session_manager import SessionMetadata

    if not isinstance(session, SessionMetadata):
        raise TypeError("session must be SessionMetadata")
    if policy.redaction_level == RedactionLevel.NONE:
        return replace(session, driver_instance=None)
    masked_outputs = {key: _mask_tree(value) for key, value in session.outputs.items()}
    audit_escalations = []
    for item in session.escalations:
        cleaned = redact_escalation_request(item, policy)
        marker = base64.b64encode(b"REDACTED").decode("ascii")
        state = replace(cleaned.stuck_state, current_screenshot=marker.encode("ascii"),
                        current_dom="<redacted>", escalation_context=redact_dict(
                            cleaned.stuck_state.escalation_context,
                            replace(policy, preserve_screenshots=False, preserve_dom=False)))
        audit_escalations.append(replace(cleaned, screenshot=marker, dom_snapshot="<redacted>", stuck_state=state))
    return replace(session, driver_instance=None, outputs=masked_outputs,
                   risk_assessments=[redact_dict(item, policy) for item in session.risk_assessments],
                   risk_approvals=[redact_dict(item, policy) for item in session.risk_approvals],
                   step_executions=[redact_dict(item, policy) for item in session.step_executions],
                   escalations=audit_escalations,
                   error_reason=redact_string(session.error_reason, policy) if session.error_reason else None)


def save_raw_evidence(screenshot: str, dom_snapshot: str, evidence_dir: str,
                      evidence_id: str, driver_logs: list[str] | None = None) -> str:
    """Write unredacted browser evidence to a separate owner-only JSON file.

    Args:
        screenshot: Base64 PNG from Selenium.
        dom_snapshot: Captured HTML.
        evidence_dir: Restricted evidence directory.
        evidence_id: UUID for a specific escalation.
        driver_logs: Optional raw browser diagnostics.

    Returns:
        Absolute destination path.

    Raises:
        ValueError: Evidence ID or content is invalid.
    """
    import uuid

    try:
        uuid.UUID(evidence_id)
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError("evidence_id must be a UUID") from exc
    if not isinstance(screenshot, str) or not screenshot or not isinstance(dom_snapshot, str) or not dom_snapshot:
        raise ValueError("Screenshot and DOM are required")
    try:
        if not base64.b64decode(screenshot, validate=True):
            raise ValueError("Screenshot must contain image bytes")
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Screenshot must be base64 encoded") from exc
    if driver_logs is not None and (not isinstance(driver_logs, list) or any(not isinstance(item, str) for item in driver_logs)):
        raise ValueError("driver_logs must be a list of strings")
    folder = Path(evidence_dir)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        os.chmod(folder, 0o700)
    target = folder / f"{evidence_id}.json"
    body = json.dumps({"screenshots": screenshot, "dom_snapshot": dom_snapshot,
                       "driver_logs": driver_logs or [],
                       "timestamp": datetime.now(timezone.utc).isoformat()}, ensure_ascii=False)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=folder,
                                         prefix=f".{evidence_id}.", delete=False) as handle:
            temporary = handle.name
            os.chmod(temporary, 0o600)
            handle.write(body + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
    return str(target)
