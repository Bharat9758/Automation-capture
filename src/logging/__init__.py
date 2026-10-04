"""JSON logging helpers for AutomationCapture components."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from pythonjsonlogger import jsonlogger


_LOG_LEVEL = logging.INFO
_QUIET = False
_FILE_HANDLER: logging.Handler | None = None


def _managed_loggers() -> list[logging.Logger]:
    """Find existing AutomationCapture loggers.

    Returns:
        Module loggers currently registered in Python logging.
    """
    return [logger for name, logger in logging.Logger.manager.loggerDict.items()
            if name.startswith("src.") and isinstance(logger, logging.Logger)]


def configure_logging(level: str = "INFO", *, log_file: str | None = None,
                      quiet: bool = False) -> None:
    """Configure JSON console and private file logging across modules.

    Args:
        level: Minimum logging level.
        log_file: Optional append-only private JSONL destination.
        quiet: Suppress console handlers while retaining file logging.

    Raises:
        ValueError: If the level or log path is invalid.
    """
    global _LOG_LEVEL, _QUIET, _FILE_HANDLER
    normalized = level.upper()
    if normalized not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ValueError("Invalid log level")
    if log_file is not None and (not isinstance(log_file, str) or not log_file.strip()):
        raise ValueError("log_file must be a nonempty path")
    previous = _FILE_HANDLER
    if previous is not None:
        for logger in _managed_loggers():
            logger.removeHandler(previous)
        previous.flush()
        previous.stream.close()
        previous.close()
    _FILE_HANDLER = None
    if log_file is not None:
        path = Path(log_file)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags, 0o600)
        os.chmod(path, 0o600)
        stream = os.fdopen(descriptor, "a", encoding="utf-8")
        handler = logging.StreamHandler(stream)
        handler.setFormatter(RedactingJsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        _FILE_HANDLER = handler
    _LOG_LEVEL, _QUIET = logging.getLevelName(normalized), quiet
    for logger in _managed_loggers():
        logger.setLevel(_LOG_LEVEL)
        for handler in logger.handlers:
            if getattr(handler, "_automation_stream", False):
                handler.setLevel(logging.CRITICAL + 1 if quiet else logging.NOTSET)
        if _FILE_HANDLER is not None and _FILE_HANDLER not in logger.handlers:
            logger.addHandler(_FILE_HANDLER)


class RedactingJsonFormatter(jsonlogger.JsonFormatter):
    """Fail closed when formatting structured metadata for JSON logs."""

    def process_log_record(self, log_record: dict[str, object]) -> dict[str, object]:
        """Remove private fields and patterns before the JSON is written.

        Args:
            log_record: Fields assembled by the JSON formatter.

        Returns:
            Redacted record, or a minimal safe error event.
        """
        try:
            from dataclasses import replace

            from src.safety.data_redactor import load_redaction_policy, redact_dict

            policy = replace(load_redaction_policy(), preserve_screenshots=False, preserve_dom=False)
            logger_name = log_record.get("name", "src.logging")
            result = redact_dict({key: value for key, value in log_record.items() if key != "name"}, policy)
            result["name"] = logger_name if isinstance(logger_name, str) else "src.logging"
            return result
        except (TypeError, ValueError, AttributeError):
            return {"levelname": "ERROR", "name": "src.logging", "message": "redaction_failed"}


def get_logger(name: str) -> logging.Logger:
    """Return a logger with one JSON stream handler.

    Args:
        name: Module name used as the logger name.

    Returns:
        Configured module logger.
    """
    logger = logging.getLogger(name)
    if not any(getattr(handler, "_automation_stream", False) for handler in logger.handlers):
        handler = logging.StreamHandler()
        handler._automation_stream = True  # type: ignore[attr-defined]
        handler.setLevel(logging.CRITICAL + 1 if _QUIET else logging.NOTSET)
        handler.setFormatter(RedactingJsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
    if _FILE_HANDLER is not None and _FILE_HANDLER not in logger.handlers:
        logger.addHandler(_FILE_HANDLER)
    logger.setLevel(_LOG_LEVEL)
    logger.propagate = False
    return logger
