"""Fail-closed page and element authorization for deterministic replay."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from selenium.webdriver.remote.webdriver import WebDriver

from src.artifact.schema import ActionStep, AutomationArtifact, Locator
from src.logging import get_logger


LOGGER = get_logger(__name__)
_ACTIONS = frozenset({"click", "type", "read", "navigate", "wait", "checkpoint"})
_ELEMENT_ACTIONS = frozenset({"click", "type", "read", "wait", "checkpoint"})
_DEFAULT_FORBIDDEN = ("delete", "close_account", "remove", "transfer", "unsubscribe", "deactivate")
_DEFAULT_CONFIRM = ("delete", "close", "transfer", "remove")


class AllowlistViolation(PermissionError):
    """Describe an action rejected before browser interaction."""

    def __init__(self, reason: str, element_id: str = "", action: str = "", current_url: str = "",
                 recommendation: str = "Ask an operator to review the allowlist") -> None:
        """Store safe diagnostic context without copying input values.

        Args:
            reason: Human-readable denial reason.
            element_id: Configured identifier, when available.
            action: Attempted browser action.
            current_url: Browser URL.
            recommendation: Suggested operator action.
        """
        super().__init__(reason)
        self.reason = reason
        self.element_id = element_id
        self.action = action
        self.current_url = current_url
        self.recommendation = recommendation


@dataclass(frozen=True, kw_only=True)
class AllowlistedElement:
    """A reviewed locator and its permitted interaction."""

    element_id: str
    locator: Locator
    action: str
    description: str
    safe_to_click: bool = True
    safe_to_type: bool = True
    risky_keyword: bool = False
    requires_confirmation: bool = False


@dataclass(frozen=True, kw_only=True)
class AllowlistedPage:
    """An authorized HTTP page, actions, and element inventory."""

    url_pattern: str
    domain: str
    page_name: str
    description: str
    allowed_elements: list[AllowlistedElement] = field(default_factory=list)
    allowed_actions: list[str] = field(default_factory=list)


@dataclass(frozen=True, kw_only=True)
class AllowlistConfig:
    """Reviewed page rules and global action restrictions."""

    pages: list[AllowlistedPage]
    global_forbidden_keywords: list[str] = field(default_factory=lambda: list(_DEFAULT_FORBIDDEN))
    require_confirmation_for: list[str] = field(default_factory=lambda: list(_DEFAULT_CONFIRM))
    allow_new_urls: bool = False
    allow_unknown_elements: bool = False


def _locator(data: Any) -> Locator:
    """Decode a locator and its fallbacks, accepting omitted explanatory notes.

    Args:
        data: JSON locator object.

    Returns:
        Validated artifact locator.
    """
    if not isinstance(data, dict):
        raise ValueError("Allowlist locator must be an object")
    unknown = set(data) - {"strategy", "value", "fallbacks", "robustness_notes"}
    if unknown:
        raise ValueError("Allowlist locator contains unknown fields")
    fallbacks = data.get("fallbacks")
    if fallbacks is not None and not isinstance(fallbacks, list):
        raise ValueError("Locator fallbacks must be a list")
    return Locator(strategy=data["strategy"], value=data["value"],
                   fallbacks=[_locator(item) for item in fallbacks] if fallbacks else None,
                   robustness_notes=data.get("robustness_notes") or "Reviewed allowlist locator")


def _string_list(data: Any, label: str) -> list[str]:
    """Validate nonempty string lists without coercion.

    Args:
        data: Untrusted JSON field.
        label: Diagnostic field name.

    Returns:
        Validated string list.
    """
    if not isinstance(data, list) or any(not isinstance(item, str) or not item.strip() for item in data):
        raise ValueError(f"{label} must be a list of nonempty strings")
    return data


def load_allowlist(filepath: str) -> AllowlistConfig:
    """Load and validate a reviewed JSON allowlist; fail on malformed rules.

    Args:
        filepath: Path to an allowlist JSON file.

    Returns:
        Validated allowlist.

    Raises:
        FileNotFoundError: The configured file does not exist.
        ValueError: JSON or rules are invalid.
    """
    try:
        data = json.loads(Path(filepath).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid allowlist JSON at line {exc.lineno}") from exc
    if not isinstance(data, dict) or set(data) - {"pages", "global_forbidden_keywords", "require_confirmation_for", "allow_new_urls", "allow_unknown_elements"}:
        raise ValueError("Invalid allowlist root object")
    if not isinstance(data.get("pages"), list) or not data["pages"]:
        raise ValueError("Allowlist requires at least one page")
    for flag in ("allow_new_urls", "allow_unknown_elements"):
        if flag in data and not isinstance(data[flag], bool):
            raise ValueError(f"{flag} must be a boolean")
    pages: list[AllowlistedPage] = []
    for raw in data["pages"]:
        if not isinstance(raw, dict) or set(raw) - {"url_pattern", "domain", "page_name", "description", "allowed_elements", "allowed_actions"}:
            raise ValueError("Invalid allowlist page")
        for key in ("url_pattern", "domain", "page_name", "description"):
            if not isinstance(raw.get(key), str) or not raw[key].strip():
                raise ValueError(f"Page {key} must be nonempty text")
        domain = raw["domain"]
        if any(char in domain for char in "/@?#") or domain.startswith(".") or domain != domain.lower():
            raise ValueError("Page domain must be a lowercase hostname with optional port")
        pattern = raw["url_pattern"]
        if pattern.startswith("regex:"):
            try:
                re.compile(pattern[6:])
            except re.error as exc:
                raise ValueError("Invalid allowlist URL regex") from exc
        elif not pattern.startswith("/") or "?" in pattern or "#" in pattern:
            raise ValueError("Exact URL patterns must be absolute paths")
        actions = _string_list(raw.get("allowed_actions", []), "allowed_actions")
        if not set(actions) <= _ACTIONS:
            raise ValueError("Unsupported allowed action")
        elements: list[AllowlistedElement] = []
        if not isinstance(raw.get("allowed_elements", []), list):
            raise ValueError("allowed_elements must be a list")
        for item in raw.get("allowed_elements", []):
            if not isinstance(item, dict) or set(item) - {"element_id", "locator", "action", "description", "safe_to_click", "safe_to_type", "risky_keyword", "requires_confirmation"}:
                raise ValueError("Invalid allowed element")
            for key in ("element_id", "action", "description"):
                if not isinstance(item.get(key), str) or not item[key].strip():
                    raise ValueError(f"Element {key} must be nonempty text")
            if item["action"] not in _ELEMENT_ACTIONS or item["action"] not in actions:
                raise ValueError("Element action must be allowed on its page")
            for key in ("safe_to_click", "safe_to_type", "risky_keyword", "requires_confirmation"):
                if key in item and not isinstance(item[key], bool):
                    raise ValueError(f"{key} must be a boolean")
            elements.append(AllowlistedElement(locator=_locator(item.get("locator")), **{k: v for k, v in item.items() if k != "locator"}))
        if len({item.element_id for item in elements}) != len(elements):
            raise ValueError("Duplicate element IDs on page")
        pages.append(AllowlistedPage(url_pattern=pattern, domain=domain, page_name=raw["page_name"],
                                     description=raw["description"], allowed_actions=actions, allowed_elements=elements))
    forbidden = _string_list(data.get("global_forbidden_keywords", list(_DEFAULT_FORBIDDEN)), "global_forbidden_keywords")
    confirm = _string_list(data.get("require_confirmation_for", list(_DEFAULT_CONFIRM)), "require_confirmation_for")
    config = AllowlistConfig(pages=pages, global_forbidden_keywords=forbidden, require_confirmation_for=confirm,
                             allow_new_urls=data.get("allow_new_urls", False),
                             allow_unknown_elements=data.get("allow_unknown_elements", False))
    LOGGER.info("allowlist_loaded", extra={"event": "allowlist_loaded", "page_count": len(pages)})
    return config


def _matching_page(url: str, config: AllowlistConfig) -> AllowlistedPage | None:
    """Find a page by strict hostname and full path match.

    Args:
        url: Absolute browser URL.
        config: Reviewed rules.

    Returns:
        Matching page or None.
    """
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        return None
    for page in config.pages:
        if parsed.netloc.lower() != page.domain.lower():
            continue
        pattern = page.url_pattern
        if re.fullmatch(pattern[6:], parsed.path) if pattern.startswith("regex:") else parsed.path == pattern:
            return page
    return None


def is_url_allowed(current_url: str, config: AllowlistConfig) -> bool:
    """Authorize a URL using an exact hostname and reviewed page path.

    Args:
        current_url: Requested or current URL.
        config: Reviewed rules.

    Returns:
        True only for approved pages, or explicit new URL opt-in.
    """
    try:
        parsed = urlsplit(current_url)
    except ValueError:
        return False
    valid = parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not parsed.username and not parsed.password
    return bool(valid and (_matching_page(current_url, config) or config.allow_new_urls))


def _candidates(locator: Locator) -> list[Locator]:
    """Flatten reviewed primary and fallback locators.

    Args:
        locator: Primary locator.

    Returns:
        All candidates in declared order.
    """
    return [locator, *(candidate for fallback in locator.fallbacks or [] for candidate in _candidates(fallback))]


def _matched_element(locator: Locator, page: AllowlistedPage) -> AllowlistedElement | None:
    """Match a requested locator without permitting an unreviewed fallback.

    Args:
        locator: Recorded locator tree.
        page: Reviewed page.

    Returns:
        Matching reviewed element, if every fallback belongs to it.
    """
    requested = {(item.strategy, item.value) for item in _candidates(locator)}
    for element in page.allowed_elements:
        reviewed = {(item.strategy, item.value) for item in _candidates(element.locator)}
        if (locator.strategy, locator.value) in reviewed and requested <= reviewed:
            return element
    return None


def is_element_allowed(locator: Locator, current_url: str, action: str,
                       config: AllowlistConfig) -> tuple[bool, str | None]:
    """Validate a page action and the complete locator fallback tree.

    Args:
        locator: Recorded target.
        current_url: Browser URL.
        action: Requested interaction.
        config: Reviewed rules.

    Returns:
        Authorization flag and a safe denial reason.
    """
    page = _matching_page(current_url, config)
    if page is None:
        return False, "Element has no approved page rule"
    configured_action = "read" if action == "read_text" else action
    if configured_action not in page.allowed_actions:
        return False, "Action is not allowed on this page"
    element = _matched_element(locator, page)
    if element is None:
        return (True, None) if config.allow_unknown_elements else (False, "Element locator is not allowlisted")
    if configured_action != element.action and not (configured_action in {"wait", "checkpoint"} and element.action == "read"):
        return False, "Action is not approved for this element"
    if action == "click" and not element.safe_to_click or action == "type" and not element.safe_to_type:
        return False, "Element is marked unsafe for this action"
    if element.risky_keyword or element.requires_confirmation:
        return False, "Element requires human confirmation"
    safe, reason = check_for_forbidden_keywords(element.element_id, action, element.description, config)
    if not safe:
        return False, reason
    if _contains_keyword(element.element_id + " " + element.description, config.require_confirmation_for):
        return False, "Element requires human confirmation"
    return True, None


def _contains_keyword(text: str, keywords: list[str]) -> bool:
    """Find configured words at token boundaries, including snake case.

    Args:
        text: Reviewed action metadata.
        keywords: Configured safety keywords.

    Returns:
        Whether any keyword is present.
    """
    normalized = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", text).lower()
    return any(bool(re.search(r"(?<![a-z0-9])" + re.escape(word.lower()) + r"(?![a-z0-9])", normalized))
               for word in keywords)


def check_for_forbidden_keywords(element_id: str, action: str, reasoning: str,
                                 config: AllowlistConfig) -> tuple[bool, str | None]:
    """Deny irreversible keywords in reviewed metadata and step intent.

    Args:
        element_id: Reviewed element identifier.
        action: Browser action name.
        reasoning: Recorded step explanation.
        config: Reviewed rules.

    Returns:
        Safety flag and denial reason.
    """
    if _contains_keyword(" ".join((element_id, action, reasoning)), config.global_forbidden_keywords):
        return False, "Forbidden action keyword detected"
    return True, None


def requires_confirmation(action: str, config: AllowlistConfig) -> bool:
    """Check whether action intent contains a confirmation keyword.

    Args:
        action: Action or its reviewed description.
        config: Reviewed rules.

    Returns:
        Whether an operator must confirm this action.
    """
    return _contains_keyword(action, config.require_confirmation_for)


def get_allowed_actions_for_url(url: str, config: AllowlistConfig) -> list[str]:
    """Report approved actions on one reviewed page.

    Args:
        url: Browser URL.
        config: Reviewed rules.

    Returns:
        A copy of the page action list, or an empty list.
    """
    page = _matching_page(url, config)
    return list(page.allowed_actions) if page else []


def enforce_allowlist(driver: WebDriver, step: ActionStep, artifact: AutomationArtifact,
                      allowlist: AllowlistConfig) -> tuple[bool, str | None]:
    """Authorize an action before any browser mutation; raise on denial.

    Args:
        driver: Active browser.
        step: Substituted or recorded action.
        artifact: Artifact being executed.
        allowlist: Reviewed page and element rules.

    Returns:
        ``(True, None)`` for an approved action.

    Raises:
        AllowlistViolation: URL, action, or locator is not authorized.
    """
    del artifact  # The page rule, not artifact authorship, grants permission.
    url = driver.current_url
    element_id = ""

    def deny(reason: str) -> None:
        """Log a denial and raise a typed exception without sensitive inputs.

        Args:
            reason: Safe denial category.
        """
        LOGGER.warning("allowlist_denied", extra={"event": "allowlist_denied", "action": step.action,
                                                  "step": step.step_number, "reason": reason})
        raise AllowlistViolation(reason, element_id, step.action, url)

    if (os.environ.get("ALLOWLIST_MODE") == "development" and os.environ.get("ALLOWLIST_BYPASS_TOKEN")
            and os.environ.get("ALLOWLIST_BYPASS_PRESENTED_TOKEN") == os.environ.get("ALLOWLIST_BYPASS_TOKEN")):
        LOGGER.warning("allowlist_dev_bypass", extra={"event": "allowlist_dev_bypass", "step": step.step_number})
        return True, None
    if not is_url_allowed(url, allowlist):
        deny("Current URL is not allowlisted")
    if step.action == "navigate":
        if not step.value or not is_url_allowed(step.value, allowlist):
            deny("Navigation destination is not allowlisted")
        target = _matching_page(step.value, allowlist)
        if target is not None and "navigate" not in target.allowed_actions:
            deny("Navigation is not allowed on destination page")
        return True, None
    page = _matching_page(url, allowlist)
    if page is not None and ("read" if step.action == "read_text" else step.action) not in page.allowed_actions:
        deny("Action is not allowed on this page")
    safe, reason = check_for_forbidden_keywords("", step.action, step.reasoning, allowlist)
    if not safe:
        deny(reason or "Forbidden action")
    if requires_confirmation(step.reasoning, allowlist):
        deny("Action requires human confirmation")
    if step.locator is not None:
        if page is None:
            deny("Element has no approved page rule")
        element = _matched_element(step.locator, page)
        element_id = element.element_id if element else ""
        approved, reason = is_element_allowed(step.locator, url, step.action, allowlist)
        if not approved:
            deny(reason or "Element is not approved")
    elif step.action in {"click", "type", "read_text", "wait"}:
        deny("Action requires an approved element")
    LOGGER.info("allowlist_approved", extra={"event": "allowlist_approved", "action": step.action,
                                             "step": step.step_number, "element_id": element_id})
    return True, None
