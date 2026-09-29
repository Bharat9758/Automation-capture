"""JSON logging helpers for AutomationCapture components."""

from __future__ import annotations

import logging

from pythonjsonlogger import jsonlogger


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
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(RedactingJsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
