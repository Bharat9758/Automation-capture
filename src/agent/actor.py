"""Execute validated Selenium actions selected by the agent."""

from __future__ import annotations

import os
from typing import Any
from urllib.parse import urlsplit

from selenium.common.exceptions import TimeoutException, WebDriverException
from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from src.logging import get_logger


LOGGER = get_logger(__name__)


class Actor:
    """Perform actions with bounded waits and an exact host allowlist."""

    def __init__(self, driver: WebDriver) -> None:
        """Initialize the actor for a browser session.

        Args:
            driver: Active Selenium WebDriver.
        """
        self.driver = driver

    @staticmethod
    def _xpath_literal(value: str) -> str:
        """Escape a string for an XPath string literal.

        Args:
            value: Untrusted element text.

        Returns:
            A valid XPath literal.
        """
        if "'" not in value:
            return f"'{value}'"
        if '"' not in value:
            return f'"{value}"'
        return "concat(" + ', "\'", '.join(f"'{part}'" for part in value.split("'")) + ")"

    @classmethod
    def _by(cls, locator: str, locator_type: str) -> tuple[str, str]:
        """Convert a supported locator to a Selenium strategy.

        Args:
            locator: Nonempty element locator.
            locator_type: css, xpath, id, or text.

        Returns:
            Selenium strategy and locator expression.

        Raises:
            ValueError: If the locator or strategy is unsupported.
        """
        if not isinstance(locator, str) or not locator.strip():
            raise ValueError("Locator must be a nonempty string")
        strategies = {"css": By.CSS_SELECTOR, "xpath": By.XPATH, "id": By.ID}
        if locator_type == "text":
            literal = cls._xpath_literal(locator.strip())
            return By.XPATH, f"//*[normalize-space(.)={literal} and not(.//*[normalize-space(.)={literal}])]"
        if locator_type not in strategies:
            raise ValueError("locator_type must be css, xpath, id, or text")
        return strategies[locator_type], locator

    @staticmethod
    def is_allowed_url(url: str) -> bool:
        """Check an HTTP(S) URL against ALLOWED_DOMAINS.

        Args:
            url: Absolute URL to check.

        Returns:
            Whether the host and, when supplied, port are allowed.
        """
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
                return False
            host = parsed.hostname.lower().rstrip(".")
            port = parsed.port
            allowed = {entry.strip().lower().rstrip(".") for entry in os.environ.get("ALLOWED_DOMAINS", "").split(",") if entry.strip()}
            return host in allowed or (port is not None and f"{host}:{port}" in allowed)
        except ValueError:
            return False

    @staticmethod
    def _outcome(action: str, success: bool, **details: Any) -> dict[str, Any]:
        """Create and log a structured action outcome without typed values.

        Args:
            action: Action name.
            success: Whether the action succeeded.
            **details: Safe diagnostic fields.

        Returns:
            JSON-serializable outcome.
        """
        result = {"action": action, "success": success, **details}
        LOGGER.info("action_completed", extra={"event": "action_completed", **result})
        return result

    def click(self, locator: str, locator_type: str = "css") -> dict[str, Any]:
        """Wait for and click an interactable element.

        Args:
            locator: Element locator.
            locator_type: css, xpath, id, or text.

        Returns:
            Structured success or failure details.
        """
        try:
            by = self._by(locator, locator_type)
            timeout = float(os.environ.get("ACTION_WAIT_TIMEOUT_SECONDS", "10"))
            element = WebDriverWait(self.driver, timeout).until(EC.element_to_be_clickable(by))
            element.click()
            return self._outcome("click", True, locator=locator, locator_type=locator_type)
        except (ValueError, TimeoutException, WebDriverException) as exc:
            return self._outcome("click", False, locator=locator, locator_type=locator_type, error=type(exc).__name__)

    def type(self, locator: str, text: str, locator_type: str = "css") -> dict[str, Any]:
        """Replace an element's text without recording the value in logs.

        Args:
            locator: Element locator.
            text: Text to enter.
            locator_type: css, xpath, id, or text.

        Returns:
            Structured success or failure details, omitting text.
        """
        try:
            if not isinstance(text, str):
                raise ValueError("text must be a string")
            by = self._by(locator, locator_type)
            timeout = float(os.environ.get("ACTION_WAIT_TIMEOUT_SECONDS", "10"))
            element = WebDriverWait(self.driver, timeout).until(EC.element_to_be_clickable(by))
            element.clear()
            element.send_keys(text)
            return self._outcome("type", True, locator=locator, locator_type=locator_type, length=len(text))
        except (ValueError, TimeoutException, WebDriverException) as exc:
            return self._outcome("type", False, locator=locator, locator_type=locator_type, error=type(exc).__name__)

    def navigate(self, url: str) -> dict[str, Any]:
        """Navigate only to an allowed HTTP(S) host.

        Args:
            url: Absolute target URL.

        Returns:
            Structured success or failure details.
        """
        if not self.is_allowed_url(url):
            return self._outcome("navigate", False, error="URL is outside ALLOWED_DOMAINS")
        try:
            self.driver.get(url)
            if not self.is_allowed_url(self.driver.current_url):
                return self._outcome("navigate", False, error="Redirect left ALLOWED_DOMAINS")
            return self._outcome("navigate", True, url=self.driver.current_url)
        except WebDriverException as exc:
            return self._outcome("navigate", False, error=type(exc).__name__)

    def wait_for_element(self, locator: str, timeout: int = 10, locator_type: str = "css") -> dict[str, Any]:
        """Wait for an element to exist in the DOM.

        Args:
            locator: Element locator.
            timeout: Maximum wait in seconds.
            locator_type: css, xpath, id, or text.

        Returns:
            Structured success or failure details.
        """
        try:
            if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
                raise ValueError("timeout must be a positive number")
            by = self._by(locator, locator_type)
            WebDriverWait(self.driver, timeout).until(EC.presence_of_element_located(by))
            return self._outcome("wait_for_element", True, locator=locator, locator_type=locator_type)
        except (ValueError, TimeoutException, WebDriverException) as exc:
            return self._outcome("wait_for_element", False, locator=locator, locator_type=locator_type, error=type(exc).__name__)

    def read_text(self, locator: str, locator_type: str = "css") -> str:
        """Read element text, returning an empty string on lookup failure.

        Args:
            locator: Element locator.
            locator_type: css, xpath, id, or text.

        Returns:
            Visible text or an empty string on failure.
        """
        try:
            by = self._by(locator, locator_type)
            value = self.driver.find_element(*by).text
            LOGGER.info("text_read", extra={"event": "text_read", "locator": locator, "length": len(value)})
            return value
        except (ValueError, WebDriverException) as exc:
            LOGGER.warning("text_read_failed", extra={"event": "text_read_failed", "error": type(exc).__name__})
            return ""
