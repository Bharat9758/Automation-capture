"""Capture browser signals for a goal-driven automation agent."""

from __future__ import annotations

import base64
import json
import os
from typing import Any

from selenium.webdriver.common.by import By
from selenium.webdriver.remote.webdriver import WebDriver

from src.logging import get_logger


LOGGER = get_logger(__name__)


class Observer:
    """Read the current page without modifying browser state."""

    def __init__(self, driver: WebDriver) -> None:
        """Initialize the observer.

        Args:
            driver: Active Selenium WebDriver.
        """
        self.driver = driver

    def capture_screenshot(self) -> bytes:
        """Return a PNG screenshot encoded as base64 bytes.

        Returns:
            Base64 encoded PNG bytes.

        Raises:
            WebDriverException: If the browser cannot capture a screenshot.
        """
        return base64.b64encode(self.driver.get_screenshot_as_png())

    def get_page_text(self) -> str:
        """Return visible text from the page body.

        Returns:
            Visible body text.
        """
        return self.driver.find_element(By.TAG_NAME, "body").text

    def get_html_source(self) -> str:
        """Return the current page's serialized HTML.

        Returns:
            HTML source from WebDriver.
        """
        return self.driver.page_source

    def get_accessibility_tree(self) -> str:
        """Return a cross-browser semantic snapshot of the document.

        This DOM-derived snapshot includes roles, accessible name candidates, and
        parent indices. It is not a browser-native accessibility tree.

        Returns:
            JSON text containing semantic element records.

        Raises:
            ValueError: If the configured node limit is invalid.
            WebDriverException: If the browser cannot execute JavaScript.
        """
        limit = int(os.environ.get("OBSERVATION_NODE_LIMIT", "300"))
        if limit < 1:
            raise ValueError("OBSERVATION_NODE_LIMIT must be positive")
        script = r"""
            const limit = arguments[0];
            const records = [];
            function walk(element, parent) {
              if (!element || records.length >= limit) return;
              const index = records.length;
              const labelIds = (element.getAttribute('aria-labelledby') || '').split(/\s+/);
              const labelled = labelIds.map(id => document.getElementById(id)?.textContent || '')
                .join(' ').trim();
              const label = element.labels?.[0]?.textContent?.trim() || '';
              records.push({
                parent, tag: element.tagName.toLowerCase(),
                role: element.getAttribute('role') || '',
                name: element.getAttribute('aria-label') || labelled || label ||
                  element.getAttribute('alt') || element.getAttribute('title') || '',
                id: element.id || '', disabled: Boolean(element.disabled)
              });
              for (const child of element.children) walk(child, index);
            }
            walk(document.body, null);
            return records;
        """
        records = self.driver.execute_script(script, limit)
        return json.dumps(records, ensure_ascii=False, separators=(",", ":"))

    def get_current_state(self) -> dict[str, Any]:
        """Collect JSON-serializable signals for the current page.

        Returns:
            URL, title, visible text, HTML, semantic snapshot, and PNG screenshot.

        Raises:
            ValueError: If the configured text limit is invalid.
            WebDriverException: If reading the browser state fails.
        """
        max_chars = int(os.environ.get("OBSERVATION_MAX_CHARS", "12000"))
        if max_chars < 1:
            raise ValueError("OBSERVATION_MAX_CHARS must be positive")
        state = {
            "url": self.driver.current_url,
            "title": self.driver.title,
            "page_text": self.get_page_text()[:max_chars],
            "html_source": self.get_html_source()[:max_chars],
            "accessibility_tree": self.get_accessibility_tree()[:max_chars],
            "screenshot": self.capture_screenshot().decode("ascii"),
        }
        LOGGER.info("observation_captured", extra={"event": "observation_captured", "url": state["url"]})
        return state
