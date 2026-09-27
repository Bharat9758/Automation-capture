"""Authorization contracts for reviewed pages and replay actions."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from selenium.webdriver.remote.webdriver import WebDriver

from src.artifact.schema import ActionStep, Locator
from src.safety.allowlist import (
    AllowlistViolation, check_for_forbidden_keywords, enforce_allowlist,
    get_allowed_actions_for_url, is_element_allowed, is_url_allowed,
    load_allowlist, requires_confirmation,
)


@pytest.fixture
def rules(tmp_path: Path) -> Path:
    """Write a reviewed search page with a permitted fallback.

    Args:
        tmp_path: Temporary config directory.

    Returns:
        JSON configuration path.
    """
    path = tmp_path / "allowlist.json"
    path.write_text(json.dumps({
        "pages": [{"domain": "banking.example.com", "url_pattern": "/members/search", "page_name": "search",
                   "description": "Reviewed search", "allowed_actions": ["navigate", "click", "type", "read", "checkpoint"],
                   "allowed_elements": [
                       {"element_id": "query", "locator": {"strategy": "id", "value": "query",
                                                        "fallbacks": [{"strategy": "css", "value": "input.search"}]},
                        "action": "type", "description": "Search field"},
                       {"element_id": "submit", "locator": {"strategy": "id", "value": "submit"},
                        "action": "click", "description": "Submit search"},
                       {"element_id": "delete_account", "locator": {"strategy": "id", "value": "delete"},
                        "action": "click", "description": "Delete account"}] }],
        "global_forbidden_keywords": ["delete", "transfer", "unsubscribe"],
        "require_confirmation_for": ["close", "transfer"],
        "allow_new_urls": False, "allow_unknown_elements": False,
    }), encoding="utf-8")
    return path


def test_urls_match_exact_hosts_and_paths(rules: Path) -> None:
    """Reject lookalike hosts, suffix paths, credentials, and non-HTTP URLs."""
    config = load_allowlist(str(rules))
    assert is_url_allowed("https://banking.example.com/members/search?query=1", config)
    assert get_allowed_actions_for_url("https://banking.example.com/members/search", config) == ["navigate", "click", "type", "read", "checkpoint"]
    for url in ("https://banking.example.com.evil.test/members/search", "https://banking.example.com/members/search/delete",
                "https://evil.test@banking.example.com/members/search", "javascript:/members/search"):
        assert not is_url_allowed(url, config)


def test_regex_patterns_are_full_path_matches(rules: Path) -> None:
    """Explicit regular expressions cover approved pages without matching suffixes."""
    data = json.loads(rules.read_text(encoding="utf-8"))
    data["pages"][0]["url_pattern"] = "regex:/members/[0-9]+"
    rules.write_text(json.dumps(data), encoding="utf-8")
    config = load_allowlist(str(rules))
    assert is_url_allowed("https://banking.example.com/members/123", config)
    assert not is_url_allowed("https://banking.example.com/members/123/delete", config)


def test_elements_actions_and_unreviewed_fallbacks(rules: Path) -> None:
    """Authorize only the declared action and complete reviewed locator tree."""
    config = load_allowlist(str(rules))
    url = "https://banking.example.com/members/search"
    approved = Locator(strategy="id", value="query", robustness_notes="Stable ID",
                       fallbacks=[Locator(strategy="css", value="input.search", robustness_notes="Reviewed fallback")])
    assert is_element_allowed(approved, url, "type", config) == (True, None)
    assert not is_element_allowed(approved, url, "click", config)[0]
    approved.fallbacks = [Locator(strategy="css", value="button.delete", robustness_notes="Unreviewed")]
    assert not is_element_allowed(approved, url, "type", config)[0]
    assert not is_element_allowed(Locator(strategy="id", value="unknown", robustness_notes="Unknown"), url, "click", config)[0]


def test_forbidden_keywords_and_confirmation(rules: Path) -> None:
    """Irreversible terms are denied even when an element was declared."""
    config = load_allowlist(str(rules))
    assert check_for_forbidden_keywords("delete_account", "click", "submit", config)[0] is False
    assert check_for_forbidden_keywords("submit", "click", "TRANSFER funds", config)[0] is False
    assert check_for_forbidden_keywords("submit", "click", "Search member", config) == (True, None)
    assert requires_confirmation("close account", config)
    assert not requires_confirmation("search member", config)
    assert not is_element_allowed(Locator(strategy="id", value="delete", robustness_notes="Reviewed"),
                                  "https://banking.example.com/members/search", "click", config)[0]


def test_enforce_blocks_before_interaction_and_describes_violation(rules: Path) -> None:
    """An unauthorized target produces a typed exception without touching Selenium."""
    driver = Mock(spec=WebDriver)
    driver.current_url = "https://banking.example.com/members/search"
    step = ActionStep(step_number=1, action="click", locator=Locator(strategy="id", value="unknown", robustness_notes="Unknown"),
                      value=None, reasoning="Click result", expected_outcome="Done")
    with pytest.raises(AllowlistViolation) as error:
        enforce_allowlist(driver, step, Mock(), load_allowlist(str(rules)))
    assert error.value.action == "click"
    assert error.value.current_url == driver.current_url
    assert error.value.recommendation
    driver.find_element.assert_not_called()


def test_navigation_and_read_are_checked(rules: Path) -> None:
    """Navigation destinations and read targets must also have reviewed rules."""
    driver = Mock(spec=WebDriver)
    driver.current_url = "https://banking.example.com/members/search"
    config = load_allowlist(str(rules))
    navigation = ActionStep(step_number=1, action="navigate", locator=None, value="https://banking.example.com/admin",
                            reasoning="Open page", expected_outcome="Done")
    with pytest.raises(AllowlistViolation, match="destination"):
        enforce_allowlist(driver, navigation, Mock(), config)
    output = ActionStep(step_number=2, action="read_text", locator=Locator(strategy="id", value="submit", robustness_notes="Stable"),
                        value=None, reasoning="Read result", expected_outcome="Done")
    with pytest.raises(AllowlistViolation, match="Action"):
        enforce_allowlist(driver, output, Mock(), config)


@pytest.mark.parametrize("mutation", [
    lambda value: value.update(allow_unknown_elements="false"),
    lambda value: value["pages"][0].update(url_pattern="regex:["),
    lambda value: value["pages"][0]["allowed_elements"][0].update(action="erase"),
    lambda value: value["pages"][0]["allowed_elements"][0].update(locator={"strategy": "css"}),
])
def test_invalid_config_fails_closed(rules: Path, mutation: object) -> None:
    """Reject invalid flags, patterns, actions, and incomplete locators."""
    data = json.loads(rules.read_text(encoding="utf-8"))
    mutation(data)
    rules.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises((ValueError, KeyError)):
        load_allowlist(str(rules))


def test_development_bypass_never_works_in_production(rules: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A configured secret alone cannot disable production enforcement."""
    monkeypatch.setenv("ALLOWLIST_MODE", "production")
    monkeypatch.setenv("ALLOWLIST_BYPASS_TOKEN", "local-secret")
    monkeypatch.setenv("ALLOWLIST_BYPASS_PRESENTED_TOKEN", "local-secret")
    driver = Mock(spec=WebDriver)
    driver.current_url = "https://banking.example.com/members/search"
    step = ActionStep(step_number=1, action="click", locator=Locator(strategy="id", value="delete", robustness_notes="Reviewed"),
                      value=None, reasoning="Delete account", expected_outcome="Done")
    with pytest.raises(AllowlistViolation):
        enforce_allowlist(driver, step, Mock(), load_allowlist(str(rules)))
