"""Tests for private, indexed Selenium evidence capture."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from unittest.mock import Mock

import pytest
from selenium.common.exceptions import WebDriverException
from selenium.webdriver.remote.webdriver import WebDriver

from src.artifact.schema import ActionStep, Locator
from src.escalation.stuck_detector import StuckState
from src.logging.evidence_capture import EvidenceCollector, EvidenceType


@pytest.fixture
def driver() -> Mock:
    """Provide a browser exposing small sensitive page signals.

    Returns:
        Selenium-like driver.
    """
    browser = Mock(spec=WebDriver)
    browser.current_url = "https://banking.example.com/members/12345?token=secret"
    browser.page_source = "<html>member_id=12345 account balance $500.00</html>"
    browser.get_screenshot_as_png.return_value = b"\x89PNG\r\n\x1a\nimage"
    browser.get_log.return_value = [{"level": "ERROR", "message": "member_id=12345"}]
    return browser


@pytest.fixture
def collector(tmp_path: Path) -> EvidenceCollector:
    """Create an isolated private session directory.

    Args:
        tmp_path: Temporary storage.

    Returns:
        Evidence collector.
    """
    return EvidenceCollector("session-18", str(tmp_path / "evidence"))


def test_screenshot_dom_and_manifest_are_private_and_verified(
    collector: EvidenceCollector, driver: Mock,
) -> None:
    """A binary PNG and full HTML are saved without leaking into the index."""
    screenshot = collector.capture_screenshot(driver, 2, "member_id=12345")
    dom = collector.capture_dom(driver, 2, "checkpoint")
    assert screenshot.evidence_type == EvidenceType.SCREENSHOT
    assert screenshot.data == base64.b64encode(driver.get_screenshot_as_png.return_value).decode()
    assert Path(screenshot.path).read_bytes() == driver.get_screenshot_as_png.return_value
    assert Path(dom.path).read_text(encoding="utf-8") == driver.page_source
    assert screenshot.size_bytes == len(driver.get_screenshot_as_png.return_value)
    assert os.stat(screenshot.path).st_mode & 0o777 == 0o600
    assert os.stat(collector.root).st_mode & 0o777 == 0o700
    index = Path(collector.manifest_path).read_text(encoding="utf-8")
    assert "12345" not in index and "$500.00" not in index and "token=secret" not in index
    assert json.loads(index)["evidence"][0]["relative_path"] == "screenshots/2.png"
    assert len(collector.get_all_evidence()) == 2
    assert EvidenceCollector("session-18", str(collector.root.parent)).get_evidence_summary()["total_evidence"] == 2


def test_repeated_step_capture_preserves_both_versions(collector: EvidenceCollector, driver: Mock) -> None:
    """Retries use unique filenames rather than overwriting an earlier attempt."""
    first = collector.capture_screenshot(driver, 1, "attempt")
    second = collector.capture_screenshot(driver, 1, "retry")
    assert first.path != second.path and Path(first.path).read_bytes() == Path(second.path).read_bytes()
    assert collector.get_evidence_summary()["types"]["SCREENSHOT"] == 2


def test_driver_logs_and_optional_har(collector: EvidenceCollector, driver: Mock) -> None:
    """Unavailable channels are skipped; real HAR exports are stored as HAR."""
    logs = collector.capture_driver_logs(driver, 3)
    assert logs is not None and json.loads(Path(logs.path).read_text())[0]["level"] == "ERROR"
    assert collector.capture_network_har(driver, 3) is None
    driver.get_log.side_effect = WebDriverException("unsupported")
    assert collector.capture_driver_logs(driver, 4) is None

    class HarDriver:
        """Minimal browser extension exposing an actual HAR document."""

        current_url = "https://banking.example.com/"

        def get_network_har(self) -> dict[str, object]:
            """Return an export from a HAR-capable driver.

            Returns:
                Valid HAR shape.
            """
            return {"log": {"version": "1.2", "entries": []}}

    har = collector.capture_network_har(HarDriver(), 3)  # type: ignore[arg-type]
    assert har is not None and har.evidence_type == EvidenceType.NETWORK_HAR
    assert json.loads(Path(har.path).read_text())["log"]["version"] == "1.2"


def test_metrics_and_error_context_preserve_raw_debug_information(
    collector: EvidenceCollector, driver: Mock,
) -> None:
    """Timing stays numeric; exception details are private raw context."""
    metrics = collector.capture_performance_metrics(4, 125, 7, 1024)
    assert json.loads(Path(metrics.path).read_text())["step_duration_ms"] == 125
    step = ActionStep(step_number=4, action="click", locator=Locator(strategy="id", value="search", robustness_notes="Stable ID"), value=None,
                      reasoning="Click search", expected_outcome="Results")
    try:
        raise RuntimeError("member_id=12345 failed")
    except RuntimeError as exc:
        context = collector.capture_error_context(4, exc, driver, step)
    record = json.loads(Path(context.path).read_text())
    assert record["exception_type"] == "RuntimeError"
    assert "12345" in record["traceback"]
    assert Path(record["screenshot_path"]).is_file() and Path(record["dom_path"]).is_file()
    assert "12345" not in Path(collector.manifest_path).read_text()


def test_escalation_organizes_partial_capture_without_hiding_errors(
    collector: EvidenceCollector, driver: Mock,
) -> None:
    """A broken screenshot does not suppress DOM, logs, metrics, or diagnosis."""
    driver.get_screenshot_as_png.side_effect = WebDriverException("camera unavailable")
    state = StuckState(is_stuck=True, reason="member_id=12345", current_step=2,
                       recommended_action="Human review")
    result = collector.capture_on_escalation(driver, 2, "escalation-18", state)
    assert result["capture_errors"]["screenshot"] == "WebDriverException"
    assert "dom" in result["evidence_paths"] and "metrics" in result["evidence_paths"]
    folder = collector.root / "escalations" / "escalation-18"
    assert Path(result["evidence_paths"]["dom"]).is_relative_to(folder)
    assert json.loads(Path(result["manifest_path"]).read_text())["reason"] == "***REDACTED***"


def test_invalid_ids_and_tampered_file_fail_closed(tmp_path: Path, collector: EvidenceCollector,
                                                     driver: Mock) -> None:
    """Neither path traversal nor changed raw evidence can be silently indexed."""
    with pytest.raises(ValueError, match="safe identifier"):
        EvidenceCollector("../../outside", str(tmp_path))
    with pytest.raises(ValueError, match="safe identifier"):
        collector.capture_on_escalation(driver, 1, "../outside", StuckState(is_stuck=True))
    image = collector.capture_screenshot(driver, 1, "initial")
    Path(image.path).write_bytes(b"tampered")
    with pytest.raises(ValueError, match="changed after capture"):
        EvidenceCollector("session-18", str(collector.root.parent))
