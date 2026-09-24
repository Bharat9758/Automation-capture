"""Behavioral tests for observation, actions, and loop termination."""

from __future__ import annotations

import base64
import json
from types import SimpleNamespace
from typing import Any

import pytest

from src.agent import llm_agent
from src.agent.actor import Actor
from src.agent.observer import Observer


class FakeElement:
    """Minimal interactive Selenium element for agent tests."""

    def __init__(self, text: str = "ready") -> None:
        """Store visible text.

        Args:
            text: Visible element text.
        """
        self.text = text
        self.typed = ""

    def is_displayed(self) -> bool:
        """Report visible state.

        Returns:
            True for this test element.
        """
        return True

    def is_enabled(self) -> bool:
        """Report enabled state.

        Returns:
            True for this test element.
        """
        return True

    def click(self) -> None:
        """Simulate a click without changing the page."""

    def clear(self) -> None:
        """Remove previously typed text."""
        self.typed = ""

    def send_keys(self, text: str) -> None:
        """Store typed text.

        Args:
            text: Text entered into the fake field.
        """
        self.typed += text


class FakeDriver:
    """Minimal deterministic WebDriver for unit tests."""

    def __init__(self) -> None:
        """Initialize a simulated local page."""
        self.current_url = "http://localhost:5000/"
        self.title = "Example"
        self.page_source = "<html><body>ready</body></html>"
        self.element = FakeElement()
        self.last_locator: tuple[str, str] | None = None

    def get(self, url: str) -> None:
        """Navigate to a test URL.

        Args:
            url: Target URL.
        """
        self.current_url = url

    def set_page_load_timeout(self, seconds: int) -> None:
        """Accept a page load limit.

        Args:
            seconds: Timeout in seconds.
        """

    def find_element(self, by: str, locator: str) -> FakeElement:
        """Return the fake element and record its locator.

        Args:
            by: Selenium locator strategy.
            locator: Locator expression.

        Returns:
            Test element.
        """
        self.last_locator = (by, locator)
        return self.element

    def execute_script(self, script: str, limit: int) -> list[dict[str, str]]:
        """Return a semantic snapshot.

        Args:
            script: JavaScript source.
            limit: Configured maximum node count.

        Returns:
            One accessible element.
        """
        return [{"role": "button", "name": "ready"}]

    def get_screenshot_as_png(self) -> bytes:
        """Return stable PNG-like bytes for encoding tests.

        Returns:
            Binary screenshot fixture.
        """
        return b"\x89PNG\r\n\x1a\n"


class FakeClient:
    """Return successive canned Anthropic messages."""

    def __init__(self, decisions: list[dict[str, Any]]) -> None:
        """Store planned decisions.

        Args:
            decisions: JSON objects to return in sequence.
        """
        self.decisions = iter(decisions)
        self.messages = self
        self.requests: list[dict[str, Any]] = []

    def with_options(self, *, timeout: float) -> FakeClient:
        """Accept the remaining time budget.

        Args:
            timeout: Per-request timeout.

        Returns:
            This fake client.
        """
        assert timeout > 0
        return self

    def create(self, **kwargs: Any) -> SimpleNamespace:
        """Record a request and return a decision.

        Args:
            **kwargs: Claude request parameters.

        Returns:
            Message with a JSON text block.
        """
        self.requests.append(kwargs)
        return SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(next(self.decisions)))])


@pytest.fixture(autouse=True)
def configure_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provide isolated settings for each agent test.

    Args:
        monkeypatch: Pytest environment patcher.
    """
    monkeypatch.setenv("ALLOWED_DOMAINS", "localhost:5000,banking.example.com")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("ACTION_WAIT_TIMEOUT_SECONDS", "1")


def test_observer_collects_json_signals() -> None:
    """Screenshots are base64 and every observation is JSON serializable."""
    state = Observer(FakeDriver()).get_current_state()  # type: ignore[arg-type]
    assert base64.b64decode(state["screenshot"]).startswith(b"\x89PNG")
    assert state["page_text"] == "ready"
    assert json.loads(state["accessibility_tree"])[0]["role"] == "button"
    json.dumps(state)


def test_actor_blocks_external_navigation_and_escapes_text() -> None:
    """Unsupported domains fail and XPath text cannot change the selector."""
    driver = FakeDriver()
    actor = Actor(driver)  # type: ignore[arg-type]
    assert actor.navigate("https://evil.example/path")["success"] is False
    assert driver.current_url == "http://localhost:5000/"
    assert actor.click('Bob\'s "account"', "text")["success"] is True
    assert "concat(" in driver.last_locator[1]  # type: ignore[index]


def test_actor_does_not_return_typed_secret() -> None:
    """Action results never include entered field values."""
    driver = FakeDriver()
    result = Actor(driver).type("#pin", "private-123")  # type: ignore[arg-type]
    assert result["success"] is True
    assert driver.element.typed == "private-123"
    assert "private-123" not in json.dumps(result)


def test_wait_rejects_invalid_timeout() -> None:
    """Invalid waits return a structured failure without blocking."""
    result = Actor(FakeDriver()).wait_for_element("#button", timeout=0)  # type: ignore[arg-type]
    assert result["success"] is False
    assert result["error"] == "ValueError"


def test_loop_sends_image_and_accepts_observed_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    """The request uses an image block and completion needs goal_met."""
    client = FakeClient([{"action": "done", "goal_met": True}])
    monkeypatch.setattr(llm_agent, "Anthropic", lambda **kwargs: client)
    result = llm_agent.run_goal_driven_loop(FakeDriver(), "See ready", "http://localhost:5000/", api_key="test-key")  # type: ignore[arg-type]
    assert result["success"] is True
    assert result["steps"] == []
    content = client.requests[0]["messages"][0]["content"]
    assert content[0]["source"]["media_type"] == "image/png"


def test_loop_detects_unchanged_page(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three identical observations terminate a stuck workflow."""
    client = FakeClient([{"action": "click", "selector": "#button"}] * 5)
    monkeypatch.setattr(llm_agent, "Anthropic", lambda **kwargs: client)
    result = llm_agent.run_goal_driven_loop(FakeDriver(), "Change page", "http://localhost:5000/", api_key="test-key")  # type: ignore[arg-type]
    assert result["success"] is False
    assert "Dead-end" in result["error"]
    assert len(result["steps"]) == 2


def test_loop_stops_at_step_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    """The step cap applies to executed actions, even on an unchanged page."""
    client = FakeClient([{"action": "click", "selector": "#button"}])
    monkeypatch.setattr(llm_agent, "Anthropic", lambda **kwargs: client)
    result = llm_agent.run_goal_driven_loop(FakeDriver(), "Change page", "http://localhost:5000/", max_steps=1, api_key="test-key")  # type: ignore[arg-type]
    assert result["error"] == "Maximum steps reached"
    assert len(result["steps"]) == 1


def test_loop_rejects_unverified_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    """A done decision without goal_met never reports success."""
    client = FakeClient([{"action": "done"}])
    monkeypatch.setattr(llm_agent, "Anthropic", lambda **kwargs: client)
    result = llm_agent.run_goal_driven_loop(FakeDriver(), "Change page", "http://localhost:5000/", api_key="test-key")  # type: ignore[arg-type]
    assert result["success"] is False
    assert result["error"] == "Agent failed: ValueError"


def test_loop_rejects_unlisted_start_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """No Claude call or navigation occurs for an unlisted start URL."""
    driver = FakeDriver()
    result = llm_agent.run_goal_driven_loop(driver, "Do work", "https://external.example/", api_key="test-key")  # type: ignore[arg-type]
    assert result["success"] is False
    assert result["error"] == "Start URL is outside ALLOWED_DOMAINS"
    assert driver.current_url == "http://localhost:5000/"
