"""JSON logging helpers for AutomationCapture components."""

from __future__ import annotations

import logging

from pythonjsonlogger import jsonlogger


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
        handler.setFormatter(jsonlogger.JsonFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger
