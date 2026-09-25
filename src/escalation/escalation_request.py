"""Package and persist browser replay pauses for human review."""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import tempfile
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from src.escalation.stuck_detector import StuckState
from src.logging import get_logger
from src.replay.locator_strategy import LocatorResolver


LOGGER = get_logger(__name__)
_MASKS = {"member_id": "***MEMBER***", "account_id": "***ACCOUNT***",
          "amount": "***AMOUNT***", "ssn": "***SSN***", "email": "***EMAIL***"}
_GENERIC_MASK = "***REDACTED***"


@dataclass(frozen=True, kw_only=True)
class EscalationRequest:
    """Serializable, validated evidence for one paused automation run."""

    escalation_id: str
    timestamp: str
    artifact_id: str
    discovery_run_id: str
    goal: str
    current_step: int
    reason_escalated: str
    screenshot: str
    dom_snapshot: str
    last_action: dict[str, Any]
    previous_steps: list[dict[str, Any]]
    context_notes: str
    stuck_state: StuckState
    session_id: str
    input_params: dict[str, Any]


def redact_sensitive_params(input_params: dict[str, Any]) -> dict[str, Any]:
    """Mask every input value, including fields with unfamiliar names.

    Args:
        input_params: Raw caller inputs.

    Returns:
        Dictionary with original keys and category-specific masks.

    Raises:
        TypeError: If the input is not a dictionary with string keys.
    """
    if not isinstance(input_params, dict) or any(not isinstance(key, str) for key in input_params):
        raise TypeError("input_params must be a dictionary with string keys")
    return {key: _MASKS.get(key.lower(), _GENERIC_MASK) for key in input_params}


def _redact_metadata(value: Any, input_params: dict[str, Any]) -> Any:
    """Replace input values recursively in human-facing metadata.

    Args:
        value: JSON-shaped metadata.
        input_params: Raw input values to replace.

    Returns:
        Sanitized, independent value.
    """
    if isinstance(value, dict):
        return {key: _redact_metadata(item, input_params) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact_metadata(item, input_params) for item in value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        for key, raw in input_params.items():
            if isinstance(raw, (int, float)) and not isinstance(raw, bool) and value == raw:
                return _MASKS.get(key.lower(), _GENERIC_MASK)
    if not isinstance(value, str):
        return value
    result = value
    replacements = [(str(raw), _MASKS.get(key.lower(), _GENERIC_MASK))
                    for key, raw in input_params.items() if raw is not None and str(raw)]
    for raw, mask in sorted(replacements, key=lambda pair: len(pair[0]), reverse=True):
        # A one- or two-character input should not destroy every URL or word.
        pattern = re.escape(raw) if len(raw) >= 3 else rf"(?<!\w){re.escape(raw)}(?!\w)"
        result = re.sub(pattern, lambda _: mask, result)
    return result


def format_previous_steps(steps: list[dict[str, Any]]) -> str:
    """Render recent actions without displaying typed values or URLs.

    Args:
        steps: Successful action records, already sanitized by the caller.

    Returns:
        One short description per step.
    """
    if not isinstance(steps, list) or any(not isinstance(step, dict) for step in steps):
        raise TypeError("steps must be a list of dictionaries")
    return "\n".join(f"Step {step.get('step_number', step.get('step', '?'))}: "
                     f"{step.get('action', 'unknown')}" + (" [STUCK]" if step.get("stuck") else "")
                     for step in steps)


class _ControlsParser(HTMLParser):
    """Collect visible-looking control labels from an offline DOM snapshot."""

    def __init__(self) -> None:
        """Initialize a parser with independent control buffers."""
        super().__init__(convert_charrefs=True)
        self.items: list[str] = []
        self._stack: list[tuple[str, bool]] = []
        self._active: list[tuple[str, list[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        """Register a visible button, link, or input.

        Args:
            tag: HTML element name.
            attrs: Raw element attributes.
        """
        attributes = dict(attrs)
        hidden = bool("hidden" in attributes or attributes.get("aria-hidden") == "true"
                      or re.search(r"display\s*:\s*none|visibility\s*:\s*hidden", attributes.get("style") or "", re.I))
        hidden = hidden or (self._stack[-1][1] if self._stack else False)
        if tag not in {"input", "button", "a"}:
            self._stack.append((tag, hidden))
            return
        if tag == "input":
            if not hidden and attributes.get("type", "").lower() != "hidden":
                label = attributes.get("aria-label") or attributes.get("placeholder") or attributes.get("name") or "input"
                self.items.append(f"input: {label}")
            return
        self._stack.append((tag, hidden))
        if not hidden:
            self._active.append((tag, [attributes.get("aria-label") or ""]))

    def handle_data(self, data: str) -> None:
        """Accumulate control labels without reading input values.

        Args:
            data: Text node content.
        """
        for _, parts in self._active:
            parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        """Close a control label on its matching end tag.

        Args:
            tag: HTML element name.
        """
        if tag in {"input", "br", "img", "meta", "hr"}:
            return
        if self._stack and self._stack[-1][0] == tag:
            self._stack.pop()
            if self._active and self._active[-1][0] == tag:
                name, parts = self._active.pop()
                label = " ".join(" ".join(parts).split())
                self.items.append(f"{name}: {label or name}")


def get_available_elements(dom_snapshot: str, locator_resolver: LocatorResolver) -> list[str]:
    """Suggest controls from HTML without claiming DOM nodes are visible.

    Static HTML cannot prove computed visibility, so hidden attributes and
    inline styles are only a best-effort filter. The resolver is reserved for
    future live-browser checks and is not invoked on an offline snapshot.

    Args:
        dom_snapshot: HTML captured at the pause.
        locator_resolver: Active locator strategy for future live checks.

    Returns:
        Bounded list of control descriptions without input values.
    """
    if not isinstance(dom_snapshot, str) or not isinstance(locator_resolver, LocatorResolver):
        raise TypeError("dom_snapshot must be HTML and locator_resolver a LocatorResolver")
    parser = _ControlsParser()
    parser.feed(dom_snapshot)
    limit = int(os.environ.get("ESCALATION_ELEMENT_LIMIT", "20"))
    if limit <= 0:
        raise ValueError("ESCALATION_ELEMENT_LIMIT must be positive")
    return parser.items[:limit]


def _stuck_as_dict(stuck: StuckState) -> dict[str, Any]:
    """Encode the screenshot bytes so a dataclass can be written as JSON.

    Args:
        stuck: Valid stuck state.

    Returns:
        JSON-shaped state.
    """
    data = asdict(stuck)
    data["current_screenshot"] = stuck.current_screenshot.decode("ascii")
    return data


def validate_escalation_request(request: EscalationRequest) -> tuple[bool, str | None]:
    """Check required identifiers, evidence, and nested state.

    Args:
        request: Candidate handoff record.

    Returns:
        Pair of validity and a precise error message, if invalid.
    """
    if not isinstance(request, EscalationRequest):
        return False, "request must be an EscalationRequest"
    try:
        if str(uuid.UUID(request.escalation_id)) != request.escalation_id:
            return False, "escalation_id must be a UUID"
    except (ValueError, TypeError, AttributeError):
        return False, "escalation_id must be a UUID"
    try:
        parsed = datetime.fromisoformat(request.timestamp.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return False, "timestamp must include a timezone"
    except (ValueError, TypeError, AttributeError):
        return False, "timestamp must be an ISO timestamp"
    for name in ("artifact_id", "discovery_run_id", "goal", "reason_escalated", "session_id", "context_notes"):
        value = getattr(request, name)
        if not isinstance(value, str) or not value.strip():
            return False, f"{name} must be a nonempty string"
    if isinstance(request.current_step, bool) or not isinstance(request.current_step, int) or request.current_step < 0:
        return False, "current_step must be a nonnegative integer"
    if not isinstance(request.screenshot, str) or not request.screenshot:
        return False, "screenshot must be nonempty base64"
    try:
        if not base64.b64decode(request.screenshot, validate=True):
            return False, "screenshot must contain image bytes"
    except (binascii.Error, ValueError):
        return False, "screenshot must be valid base64"
    if not isinstance(request.dom_snapshot, str) or not request.dom_snapshot.strip():
        return False, "dom_snapshot must be nonempty HTML"
    if not isinstance(request.last_action, dict) or not isinstance(request.last_action.get("action"), str) or not request.last_action["action"]:
        return False, "last_action must contain an action"
    if not isinstance(request.previous_steps, list) or any(not isinstance(step, dict) for step in request.previous_steps):
        return False, "previous_steps must contain dictionaries"
    if not isinstance(request.stuck_state, StuckState) or request.stuck_state.is_stuck is not True:
        return False, "stuck_state must describe a stuck replay"
    if not isinstance(request.stuck_state.reason, str) or not isinstance(request.stuck_state.recommended_action, str) or not isinstance(request.stuck_state.escalation_context, dict):
        return False, "stuck_state has invalid text or context"
    if not isinstance(request.stuck_state.current_screenshot, bytes) or request.stuck_state.current_screenshot != request.screenshot.encode("ascii"):
        return False, "stuck_state screenshot must match screenshot"
    if request.stuck_state.current_dom != request.dom_snapshot:
        return False, "stuck_state DOM must match dom_snapshot"
    if request.stuck_state.current_step != request.current_step:
        return False, "stuck_state.current_step must match current_step"
    if not isinstance(request.input_params, dict) or any(not isinstance(key, str) or not isinstance(value, str) or value not in {*_MASKS.values(), _GENERIC_MASK} for key, value in request.input_params.items()):
        return False, "input_params must contain only redacted values"
    return True, None


def create_escalation_request(
    artifact_id: str, discovery_run_id: str, stuck_state: StuckState, current_session_id: str,
    goal: str, last_action: dict[str, Any], previous_steps: list[dict[str, Any]], input_params: dict[str, Any],
) -> EscalationRequest:
    """Construct a sanitized, validated request for a human operator.

    Args:
        artifact_id: Source artifact ID.
        discovery_run_id: Original discovery run ID.
        stuck_state: Hydrated detector result.
        current_session_id: WebDriver session ID.
        goal: Original replay goal or artifact description.
        last_action: Last attempted or pending action metadata.
        previous_steps: Prior successful step metadata.
        input_params: Raw caller inputs, never persisted as values.

    Returns:
        Complete, validated request.

    Raises:
        ValueError: If evidence or mandatory metadata is missing.
    """
    if not isinstance(stuck_state, StuckState) or not isinstance(last_action, dict) or not isinstance(previous_steps, list):
        raise TypeError("stuck_state, last_action, and previous_steps must have their declared types")
    masks = redact_sensitive_params(input_params)
    context = _redact_metadata({key: value for key, value in stuck_state.escalation_context.items()
                                if key not in {"dom", "screenshot"}}, input_params)
    for raw_key in ("dom", "screenshot"):
        if raw_key in stuck_state.escalation_context:
            context[raw_key] = stuck_state.escalation_context[raw_key]
    if stuck_state.current_dom and not context.get("available_elements"):
        context["available_elements"] = _redact_metadata(
            get_available_elements(stuck_state.current_dom, LocatorResolver()), input_params)
    cleaned = replace(stuck_state, reason=_redact_metadata(stuck_state.reason, input_params),
                      recommended_action=_redact_metadata(stuck_state.recommended_action, input_params),
                      escalation_context=context)
    goal_safe = _redact_metadata(goal, input_params)
    last_safe = _redact_metadata(last_action, input_params)
    previous_safe = _redact_metadata(previous_steps, input_params)
    notes = (f"Automation was trying to: {goal_safe}\n"
             f"Stuck at step {cleaned.current_step}: {cleaned.reason}\n"
             f"Last action: {last_safe.get('action', 'unknown')}\n"
             f"Current state: {cleaned.reason}\n"
             f"Recommendation: {cleaned.recommended_action}")
    request = EscalationRequest(
        escalation_id=str(uuid.uuid4()), timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        artifact_id=artifact_id, discovery_run_id=discovery_run_id, goal=goal_safe,
        current_step=cleaned.current_step, reason_escalated=cleaned.reason,
        screenshot=cleaned.current_screenshot.decode("ascii"), dom_snapshot=cleaned.current_dom,
        last_action=last_safe, previous_steps=previous_safe, context_notes=notes,
        stuck_state=cleaned, session_id=current_session_id, input_params=masks,
    )
    valid, error = validate_escalation_request(request)
    if not valid:
        raise ValueError(f"Invalid escalation request: {error}")
    LOGGER.info("escalation_created", extra={"event": "escalation_created", "escalation_id": request.escalation_id,
                                            "artifact_id": artifact_id, "step": request.current_step})
    return request


def escalation_to_json(request: EscalationRequest) -> str:
    """Serialize a valid handoff with two-space indentation.

    Args:
        request: Valid escalation request.

    Returns:
        JSON containing all fields.

    Raises:
        ValueError: If the request is incomplete.
    """
    valid, error = validate_escalation_request(request)
    if not valid:
        raise ValueError(f"Invalid escalation request: {error}")
    data = asdict(request)
    data["stuck_state"] = _stuck_as_dict(request.stuck_state)
    return json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False)


def json_to_escalation(json_str: str) -> EscalationRequest:
    """Parse and validate a persisted escalation request.

    Args:
        json_str: JSON request document.

    Returns:
        Reconstructed request and nested stuck state.

    Raises:
        ValueError: If JSON or a required field is invalid.
    """
    try:
        data = json.loads(json_str)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"Invalid escalation JSON: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("stuck_state"), dict):
        raise ValueError("Escalation document must contain a stuck_state object")
    expected = set(EscalationRequest.__dataclass_fields__)
    if set(data) != expected:
        raise ValueError(f"Escalation fields mismatch: missing {sorted(expected - set(data))}, unexpected {sorted(set(data) - expected)}")
    state = data["stuck_state"]
    fields = set(StuckState.__dataclass_fields__)
    if set(state) != fields or not isinstance(state.get("current_screenshot"), str):
        raise ValueError("stuck_state fields are incomplete or invalid")
    try:
        state_obj = StuckState(**{**state, "current_screenshot": state["current_screenshot"].encode("ascii")})
        request = EscalationRequest(**{**data, "stuck_state": state_obj})
    except (TypeError, UnicodeEncodeError) as exc:
        raise ValueError("Escalation field types are invalid") from exc
    valid, error = validate_escalation_request(request)
    if not valid:
        raise ValueError(f"Invalid escalation request: {error}")
    return request


def save_escalation_request(request: EscalationRequest, filepath: str) -> str:
    """Atomically save a request as owner-readable JSON.

    Args:
        request: Valid request to persist.
        filepath: Destination path.

    Returns:
        Supplied filepath after a successful replacement.

    Raises:
        OSError: If directory creation or write fails.
        ValueError: If the request is invalid.
    """
    body = escalation_to_json(request)
    target = Path(filepath)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent,
                                         prefix=f".{target.name}.", delete=False) as handle:
            temporary = handle.name
            os.chmod(temporary, 0o600)
            handle.write(body)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
    LOGGER.info("escalation_saved", extra={"event": "escalation_saved", "escalation_id": request.escalation_id})
    return filepath


def load_escalation_request(filepath: str) -> EscalationRequest:
    """Load and validate one saved escalation request.

    Args:
        filepath: Existing JSON path.

    Returns:
        Validated escalation request.

    Raises:
        FileNotFoundError: If the request does not exist.
        ValueError: If the file contents are invalid.
    """
    try:
        return json_to_escalation(Path(filepath).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Escalation request not found: {filepath}") from exc


def list_escalation_requests(directory: str) -> list[EscalationRequest]:
    """Return validated requests newest first; fail on invalid evidence.

    Args:
        directory: Existing request directory.

    Returns:
        Descending timestamp order, or an empty list if directory is absent.

    Raises:
        ValueError: If the directory contains an invalid request file.
    """
    folder = Path(directory)
    if not folder.exists():
        return []
    if not folder.is_dir():
        raise NotADirectoryError(str(folder))
    requests = [load_escalation_request(str(path)) for path in folder.glob("*.json")]
    return sorted(requests, key=lambda item: datetime.fromisoformat(item.timestamp.replace("Z", "+00:00")), reverse=True)
