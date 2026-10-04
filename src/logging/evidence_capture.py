"""Capture private browser evidence and maintain a session-scoped audit index."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import tempfile
import traceback
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from selenium.common.exceptions import WebDriverException
from selenium.webdriver.remote.webdriver import WebDriver

from src.artifact.schema import ActionStep
from src.logging import get_logger
from src.safety.data_redactor import MASK, RedactionPolicy, load_redaction_policy, redact_dict

if TYPE_CHECKING:
    from src.escalation.stuck_detector import StuckState


LOGGER = get_logger(__name__)
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


class EvidenceType(StrEnum):
    """Supported raw evidence formats."""

    SCREENSHOT = "SCREENSHOT"
    DOM_SNAPSHOT = "DOM_SNAPSHOT"
    DRIVER_LOGS = "DRIVER_LOGS"
    NETWORK_HAR = "NETWORK_HAR"
    PERFORMANCE_METRICS = "PERFORMANCE_METRICS"
    ERROR_CONTEXT = "ERROR_CONTEXT"


@dataclass(frozen=True, kw_only=True)
class Evidence:
    """An immutable record of one captured signal and its private contents."""

    evidence_type: EvidenceType
    timestamp: str
    step_number: int | None
    session_id: str
    url: str
    data: bytes | str = field(repr=False)
    size_bytes: int
    metadata: dict[str, Any] = field(default_factory=dict)
    path: str = ""

    def __post_init__(self) -> None:
        """Reject malformed evidence before it is indexed."""
        if not isinstance(self.evidence_type, EvidenceType) or not _SAFE_ID.fullmatch(self.session_id):
            raise ValueError("Evidence type and session ID must be valid")
        if self.step_number is not None and (type(self.step_number) is not int or self.step_number < 0):
            raise ValueError("Evidence step must be a nonnegative integer")
        if type(self.size_bytes) is not int or self.size_bytes < 0 or not isinstance(self.data, (bytes, str)):
            raise ValueError("Evidence size and data must be valid")
        if not isinstance(self.metadata, dict) or not isinstance(self.url, str):
            raise ValueError("Evidence metadata and URL must have valid types")
        try:
            captured = datetime.fromisoformat(self.timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Evidence timestamp must be ISO formatted") from exc
        if captured.tzinfo is None or captured.utcoffset() is None:
            raise ValueError("Evidence timestamp requires a timezone")


def _utc_now() -> str:
    """Return a timezone-aware ISO timestamp.

    Returns:
        Current UTC time.
    """
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _private_directory(path: Path) -> None:
    """Create a private directory tree and restrict its final component.

    Args:
        path: Directory for raw evidence.
    """
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(path, 0o700)


def _atomic_private_write(path: Path, content: bytes) -> None:
    """Replace an evidence file atomically with owner-only permissions.

    Args:
        path: Private target path.
        content: Exact bytes to persist.
    """
    _private_directory(path.parent)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, prefix=".evidence-",
                                         suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            os.chmod(temporary, 0o600)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


class EvidenceCollector:
    """Organize raw Selenium signals into one private session directory."""

    def __init__(self, session_id: str, evidence_dir: str,
                 policy: RedactionPolicy | None = None) -> None:
        """Create or reopen a validated session evidence directory.

        Args:
            session_id: Stable lifecycle ID without path separators.
            evidence_dir: Root for private session evidence.
            policy: Redaction policy for the content-free index.
        """
        if not isinstance(session_id, str) or _SAFE_ID.fullmatch(session_id) is None:
            raise ValueError("session_id must be a safe identifier")
        if not isinstance(evidence_dir, str) or not evidence_dir.strip():
            raise ValueError("evidence_dir must be a nonempty path")
        self.session_id = session_id
        base = Path(evidence_dir).expanduser().resolve()
        self.root = base / session_id
        if self.root.is_symlink() or not self.root.resolve().is_relative_to(base):
            raise ValueError("Session evidence directory cannot be a symlink")
        self.policy = policy or load_redaction_policy()
        self._evidence: list[Evidence] = []
        _private_directory(self.root)
        self.manifest_path = str(self.root / "index.json")
        if Path(self.manifest_path).exists():
            self._load_manifest()

    def _safe_metadata(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """Produce an index entry without raw page or exception details.

        Args:
            metadata: Collector-produced contextual fields.

        Returns:
            Redacted JSON metadata.
        """
        safe = {key: (MASK if key in {"reason", "exception", "url", "step_value"} else value)
                for key, value in metadata.items()}
        return redact_dict(safe, self.policy)

    def _index_entry(self, item: Evidence, digest: str) -> dict[str, Any]:
        """Build a content-free manifest entry.

        Args:
            item: Captured evidence.
            digest: SHA-256 of its persisted file.

        Returns:
            Relative path and audit fields.
        """
        return {"evidence_type": item.evidence_type.value, "timestamp": item.timestamp,
                "step_number": item.step_number, "session_id": item.session_id,
                "relative_path": str(Path(item.path).relative_to(self.root)),
                "size_bytes": item.size_bytes, "sha256": digest,
                "url": MASK, "metadata": self._safe_metadata(item.metadata)}

    def _persist_index(self) -> None:
        """Refresh the private manifest after every successful capture."""
        entries = []
        for item in self._evidence:
            contents = Path(item.path).read_bytes()
            entries.append(self._index_entry(item, hashlib.sha256(contents).hexdigest()))
        body = json.dumps({"session_id": self.session_id, "evidence": entries},
                          indent=2, ensure_ascii=False, allow_nan=False).encode("utf-8")
        _atomic_private_write(Path(self.manifest_path), body)

    def _load_manifest(self) -> None:
        """Reopen prior captures and verify each file's size and digest.

        Raises:
            ValueError: When the index or an indexed file was altered.
        """
        try:
            document = json.loads(Path(self.manifest_path).read_text(encoding="utf-8"))
            if document["session_id"] != self.session_id or not isinstance(document["evidence"], list):
                raise ValueError("Evidence manifest belongs to a different session")
            for entry in document["evidence"]:
                path = self.root / entry["relative_path"]
                if not path.resolve().is_relative_to(self.root) or not path.is_file():
                    raise ValueError("Evidence path escapes the session")
                raw = path.read_bytes()
                if len(raw) != entry["size_bytes"] or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
                    raise ValueError("Evidence file changed after capture")
                kind = EvidenceType(entry["evidence_type"])
                self._evidence.append(Evidence(
                    evidence_type=kind, timestamp=entry["timestamp"],
                    step_number=entry["step_number"], session_id=self.session_id,
                    url=entry.get("url", MASK),
                    data=base64.b64encode(raw).decode("ascii") if kind == EvidenceType.SCREENSHOT
                    else raw.decode("utf-8"),
                    size_bytes=len(raw), metadata=entry.get("metadata", {}), path=str(path)))
        except (OSError, KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("Evidence manifest is invalid") from exc

    def _capture(self, kind: EvidenceType, step_number: int, data: bytes | str,
                 url: str, metadata: dict[str, Any], folder: str, filename: str,
                 *, binary: bool = False) -> Evidence:
        """Persist a signal and index it without raw content in the manifest.

        Args:
            kind: Evidence format.
            step_number: Source replay step.
            data: PNG bytes or textual evidence.
            url: Live browser URL.
            metadata: Capture context.
            folder: Evidence category subdirectory.
            filename: Requested base filename.
            binary: True for PNG data represented as base64 in Evidence.

        Returns:
            Indexed evidence record.
        """
        if type(step_number) is not int or step_number < 0:
            raise ValueError("step_number must be a nonnegative integer")
        raw = data if isinstance(data, bytes) else data.encode("utf-8")
        destination = self.root / folder / filename
        if not destination.resolve().is_relative_to(self.root):
            raise ValueError("Evidence path escapes the session directory")
        if destination.exists():
            stem, suffix = destination.stem, destination.suffix
            destination = destination.with_name(f"{stem}_{uuid.uuid4().hex}{suffix}")
        _atomic_private_write(destination, raw)
        item = Evidence(evidence_type=kind, timestamp=_utc_now(), step_number=step_number,
                        session_id=self.session_id, url=url,
                        data=base64.b64encode(raw).decode("ascii") if binary else raw.decode("utf-8"),
                        size_bytes=len(raw), metadata=self._safe_metadata(metadata), path=str(destination))
        self._evidence.append(item)
        try:
            self._persist_index()
        except (OSError, ValueError, TypeError):
            self._evidence.pop()
            destination.unlink(missing_ok=True)
            raise
        LOGGER.info("evidence_captured", extra={"event": "evidence_captured", "session_id": self.session_id,
                                                 "evidence_type": kind.value, "step_number": step_number,
                                                 "size_bytes": item.size_bytes})
        return item

    def capture_screenshot(self, driver: WebDriver, step_number: int, reason: str,
                           *, subdir: str = "") -> Evidence:
        """Save raw PNG bytes and return a base64 evidence record.

        Args:
            driver: Original Selenium browser.
            step_number: Current replay step.
            reason: Capture reason; masked in the index.
            subdir: Optional escalation subdirectory.

        Returns:
            Private screenshot evidence.
        """
        png = driver.get_screenshot_as_png()
        if not isinstance(png, bytes) or not png:
            raise ValueError("WebDriver returned an empty screenshot")
        return self._capture(EvidenceType.SCREENSHOT, step_number, png, driver.current_url,
                             {"reason": reason}, f"{subdir}/screenshots" if subdir else "screenshots",
                             f"{step_number}.png", binary=True)

    def capture_dom(self, driver: WebDriver, step_number: int, reason: str,
                    *, subdir: str = "") -> Evidence:
        """Save full HTML privately and return its evidence record.

        Args:
            driver: Original Selenium browser.
            step_number: Current replay step.
            reason: Capture reason; masked in the index.
            subdir: Optional escalation subdirectory.

        Returns:
            HTML evidence.
        """
        html = driver.page_source
        if not isinstance(html, str):
            raise ValueError("WebDriver returned a non-text DOM")
        return self._capture(EvidenceType.DOM_SNAPSHOT, step_number, html, driver.current_url,
                             {"reason": reason}, f"{subdir}/dom" if subdir else "dom", f"{step_number}.html")

    def capture_driver_logs(self, driver: WebDriver, step_number: int,
                            *, subdir: str = "") -> Evidence | None:
        """Capture browser console logs when the driver supports them.

        Args:
            driver: Active WebDriver.
            step_number: Current replay step.
            subdir: Optional escalation subdirectory.

        Returns:
            Log evidence or None for an unsupported or empty channel.
        """
        try:
            logs = driver.get_log("browser")
        except (WebDriverException, AttributeError, NotImplementedError):
            return None
        if not logs:
            return None
        if not isinstance(logs, list):
            raise ValueError("Browser logs must be a list")
        body = json.dumps(logs, ensure_ascii=False, allow_nan=False, default=str, indent=2)
        return self._capture(EvidenceType.DRIVER_LOGS, step_number, body, driver.current_url,
                             {"entries": len(logs)}, f"{subdir}/logs" if subdir else "logs",
                             f"driver_{step_number}.json")

    def capture_network_har(self, driver: WebDriver, step_number: int,
                            *, subdir: str = "") -> Evidence | None:
        """Save a real HAR export if the driver supplies one.

        Selenium performance events are not HAR and are never relabeled here.

        Args:
            driver: WebDriver with an optional HAR-export extension.
            step_number: Current replay step.
            subdir: Optional escalation subdirectory.

        Returns:
            HAR evidence or None if unsupported.
        """
        exporter = getattr(driver, "get_network_har", None)
        if not callable(exporter):
            return None
        try:
            document = exporter()
        except (WebDriverException, NotImplementedError):
            return None
        if document is None:
            return None
        if isinstance(document, str):
            document = json.loads(document)
        if not isinstance(document, dict) or not isinstance(document.get("log"), dict):
            raise ValueError("HAR export must contain a log object")
        body = json.dumps(document, ensure_ascii=False, allow_nan=False, indent=2)
        return self._capture(EvidenceType.NETWORK_HAR, step_number, body, driver.current_url,
                             {"format": "HAR"}, f"{subdir}/network" if subdir else "network",
                             f"{step_number}.har")

    def capture_performance_metrics(self, step_number: int, step_duration_ms: int,
                                    element_count: int, page_size_bytes: int,
                                    *, subdir: str = "") -> Evidence:
        """Record bounded numeric metrics without page content.

        Args:
            step_number: Current step.
            step_duration_ms: Elapsed step time.
            element_count: Visible or matching elements.
            page_size_bytes: UTF-8 DOM byte count.
            subdir: Optional escalation subdirectory.

        Returns:
            JSON metrics evidence.
        """
        measures = {"step_duration_ms": step_duration_ms, "element_count": element_count,
                    "page_size_bytes": page_size_bytes}
        if any(type(value) is not int or value < 0 for value in measures.values()):
            raise ValueError("Performance metrics must be nonnegative integers")
        return self._capture(EvidenceType.PERFORMANCE_METRICS, step_number,
                             json.dumps(measures, indent=2), "", measures,
                             f"{subdir}/metrics" if subdir else "metrics", f"{step_number}.json")

    def capture_error_context(self, step_number: int, exception: Exception,
                              driver: WebDriver, step: ActionStep) -> Evidence:
        """Preserve traceback and immediate browser state as private raw evidence.

        Args:
            step_number: Failed step.
            exception: Caught Selenium or replay exception.
            driver: Browser at the moment of failure.
            step: Recorded action that was attempted.

        Returns:
            Context record with paths to the screenshot and DOM.
        """
        if not isinstance(exception, Exception) or not isinstance(step, ActionStep):
            raise ValueError("An exception and ActionStep are required")
        paths: dict[str, str] = {}
        errors: dict[str, str] = {}
        for name, capture in (("screenshot", self.capture_screenshot), ("dom", self.capture_dom)):
            try:
                paths[name] = capture(driver, step_number, "error").path
            except (OSError, WebDriverException, ValueError, AttributeError) as exc:
                errors[name] = type(exc).__name__
        try:
            url = driver.current_url
        except (WebDriverException, AttributeError):
            url = ""
        context = {"exception_type": type(exception).__name__, "exception_message": str(exception),
                   "traceback": "".join(traceback.format_exception(exception)),
                   "action": step.action, "step_number": step_number, "url": url,
                   "screenshot_path": paths.get("screenshot"), "dom_path": paths.get("dom"),
                   "capture_errors": errors}
        return self._capture(EvidenceType.ERROR_CONTEXT, step_number,
                             json.dumps(context, ensure_ascii=False, indent=2), url,
                             {"error_type": type(exception).__name__, "capture_errors": errors},
                             "errors", f"{step_number}_context.json")

    def capture_on_escalation(self, driver: WebDriver, step_number: int,
                              escalation_id: str, stuck_state: StuckState) -> dict[str, Any]:
        """Collect every available browser signal for the human handoff.

        Args:
            driver: Original live browser.
            step_number: Paused replay step.
            escalation_id: Unique escalation identifier.
            stuck_state: Reason the replay paused.

        Returns:
            Private paths and any separately recorded capture errors.
        """
        if not isinstance(escalation_id, str) or not _SAFE_ID.fullmatch(escalation_id):
            raise ValueError("escalation_id must be a safe identifier")
        folder = self.root / "escalations" / escalation_id
        _private_directory(folder)
        subdir = f"escalations/{escalation_id}"
        paths: dict[str, str] = {}
        errors: dict[str, str] = {}
        captures = (("screenshot", lambda: self.capture_screenshot(driver, step_number, "escalation", subdir=subdir)),
                    ("dom", lambda: self.capture_dom(driver, step_number, "escalation", subdir=subdir)),
                    ("driver_logs", lambda: self.capture_driver_logs(driver, step_number, subdir=subdir)),
                    ("network_har", lambda: self.capture_network_har(driver, step_number, subdir=subdir)))
        for name, capture in captures:
            try:
                item = capture()
                if item is not None:
                    paths[name] = item.path
            except (OSError, WebDriverException, ValueError, AttributeError, TypeError) as exc:
                errors[name] = type(exc).__name__
        try:
            raw_html = driver.page_source
            page_bytes = len(raw_html.encode("utf-8")) if isinstance(raw_html, str) else 0
            metrics = self.capture_performance_metrics(step_number, 0, 0, page_bytes, subdir=subdir)
            paths["metrics"] = metrics.path
        except (OSError, WebDriverException, ValueError, AttributeError) as exc:
            errors["metrics"] = type(exc).__name__
        manifest = {"session_id": self.session_id, "escalation_id": escalation_id,
                    "step_number": step_number, "timestamp": _utc_now(),
                    "reason": MASK if stuck_state.reason else "",
                    "evidence_paths": paths, "capture_errors": errors}
        manifest_path = folder / "manifest.json"
        _atomic_private_write(manifest_path, json.dumps(manifest, indent=2).encode("utf-8"))
        return {**manifest, "manifest_path": str(manifest_path)}

    def get_all_evidence(self) -> list[Evidence]:
        """Return the captured evidence in capture order.

        Returns:
            Detached records for this session.
        """
        return [Evidence(**asdict(item)) for item in self._evidence]

    def get_evidence_summary(self) -> dict[str, Any]:
        """Summarize capture types, sizes, and manifest location.

        Returns:
            Content-free counts and private index path.
        """
        counts = {kind.value: sum(item.evidence_type == kind for item in self._evidence)
                  for kind in EvidenceType}
        return {"session_id": self.session_id, "total_evidence": len(self._evidence),
                "types": counts, "total_size_bytes": sum(item.size_bytes for item in self._evidence),
                "manifest_path": self.manifest_path}
