"""Validate environment and optional dotenv settings for CLI commands."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


class ConfigurationError(ValueError):
    """Indicate a missing or invalid application setting."""


def _positive_int(name: str, default: int) -> int:
    """Parse a positive integer setting.

    Args:
        name: Environment variable name.
        default: Fallback when absent.

    Returns:
        Validated integer.

    Raises:
        ConfigurationError: If absent content is invalid or nonpositive.
    """
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be a positive integer") from exc
    if value <= 0:
        raise ConfigurationError(f"{name} must be a positive integer")
    return value


def _boolean(name: str, default: bool) -> bool:
    """Read a strict boolean environment setting.

    Args:
        name: Environment variable name.
        default: Fallback value.

    Returns:
        Parsed boolean.

    Raises:
        ConfigurationError: If the configured text is not a boolean.
    """
    text = os.environ.get(name, str(default)).strip().lower()
    if text in {"true", "1", "yes"}:
        return True
    if text in {"false", "0", "no"}:
        return False
    raise ConfigurationError(f"{name} must be true or false")


def load_config(config_file: str | None = None) -> dict[str, Any]:
    """Load dotenv values without overriding explicit environment settings.

    Args:
        config_file: Optional dotenv path selected by ``--config``.

    Returns:
        Validated settings; credentials remain in memory only.

    Raises:
        FileNotFoundError: If the requested file is missing.
        ConfigurationError: If a setting is invalid.
    """
    if config_file is not None:
        path = Path(config_file)
        if not path.is_file():
            raise FileNotFoundError(f"Configuration file not found: {path}")
        load_dotenv(dotenv_path=path, override=False)
    else:
        # Resolve from the working directory, including an installed CLI used
        # outside the repository. An explicit environment setting still wins.
        load_dotenv(dotenv_path=Path.cwd() / ".env", override=False)
    driver = os.environ.get("BROWSER_DRIVER", "chrome").lower()
    redaction = os.environ.get("REDACTION_LEVEL", "STRICT").upper()
    evidence = os.environ.get("EVIDENCE_CAPTURE_LEVEL", "ERRORS").upper()
    level = os.environ.get("LOG_LEVEL", "INFO").upper()
    risk_approval = os.environ.get("RISK_APPROVAL_REQUIRED", "CRITICAL").upper()
    environment = os.environ.get("ENVIRONMENT", "development").lower()
    allowlist_mode = os.environ.get("ALLOWLIST_MODE", "production").lower()
    if driver not in {"chrome", "firefox"}:
        raise ConfigurationError("BROWSER_DRIVER must be chrome or firefox")
    if redaction not in {"NONE", "BASIC", "STRICT", "PARANOID"}:
        raise ConfigurationError("REDACTION_LEVEL must be NONE, BASIC, STRICT, or PARANOID")
    if evidence not in {"ALL", "ERRORS", "ESCALATIONS_ONLY"}:
        raise ConfigurationError("EVIDENCE_CAPTURE_LEVEL must be ALL, ERRORS, or ESCALATIONS_ONLY")
    if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        raise ConfigurationError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR, or CRITICAL")
    if risk_approval not in {"SAFE", "CAUTION", "RISKY", "CRITICAL"}:
        raise ConfigurationError("RISK_APPROVAL_REQUIRED must be SAFE, CAUTION, RISKY, or CRITICAL")
    if environment not in {"development", "staging", "production"}:
        raise ConfigurationError("ENVIRONMENT must be development, staging, or production")
    if allowlist_mode not in {"development", "production"}:
        raise ConfigurationError("ALLOWLIST_MODE must be development or production")
    if environment != "development" and allowlist_mode == "development":
        raise ConfigurationError("ALLOWLIST_MODE development requires ENVIRONMENT=development")
    if redaction == "NONE" and (environment != "development" or allowlist_mode != "development"):
        raise ConfigurationError("REDACTION_LEVEL=NONE requires development mode")
    port = _positive_int("TARGET_APP_PORT", 5000)
    if port > 65535:
        raise ConfigurationError("TARGET_APP_PORT must be between 1 and 65535")
    host = os.environ.get("TARGET_APP_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ConfigurationError("TARGET_APP_HOST must be a loopback address")
    return {
        "anthropic_api_key": os.environ.get("ANTHROPIC_API_KEY"),
        "browser_driver": driver,
        "allowlist_path": os.environ.get("ALLOWLIST_PATH", "config/allowlist.json"),
        "redaction_level": redaction,
        "evidence_capture_level": evidence,
        "log_level": level,
        "log_directory": os.environ.get("LOG_DIRECTORY", "evidence/logs"),
        "evidence_directory": os.environ.get("EVIDENCE_DIRECTORY", "evidence/capture"),
        "target_app_port": port,
        "target_app_host": host,
        "flask_secret_key": os.environ.get("FLASK_SECRET_KEY"),
        "target_app_config_path": os.environ.get("TARGET_APP_CONFIG_PATH"),
        "max_replay_steps": _positive_int("MAX_REPLAY_STEPS", 100),
        "replay_timeout_seconds": _positive_int("REPLAY_TIMEOUT_SECONDS", 300),
        "selenium_headless": _boolean("SELENIUM_HEADLESS", True),
        "selenium_timeout_ms": _positive_int("SELENIUM_TIMEOUT", 10000),
        "locator_timeout_seconds": _positive_int("LOCATOR_TIMEOUT_SECONDS", 10),
        "stuck_state_threshold": _positive_int("STUCK_STATE_THRESHOLD", 3),
        "risk_approval_required": risk_approval,
        "environment": environment,
        "allowlist_mode": allowlist_mode,
        "artifact_directory": os.environ.get("ARTIFACT_DIRECTORY", "artifacts"),
        "allowed_domains": os.environ.get("ALLOWED_DOMAINS", ""),
    }
