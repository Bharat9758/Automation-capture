"""Configuration examples, dotenv precedence, and safety allowlists."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
from dotenv import dotenv_values

from src.config import ConfigurationError, load_config
from src.safety.allowlist import is_url_allowed, load_allowlist


ROOT = Path(__file__).resolve().parents[1]


def test_default_configuration_from_empty_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The installed command can start without an optional dotenv file."""
    monkeypatch.chdir(tmp_path)
    with patch.dict(os.environ, {}, clear=True):
        config = load_config()
    assert config["browser_driver"] == "chrome"
    assert config["log_level"] == "INFO"
    assert config["redaction_level"] == "STRICT"
    assert config["evidence_capture_level"] == "ERRORS"
    assert config["target_app_port"] == 5000
    assert config["max_replay_steps"] == 100
    assert config["replay_timeout_seconds"] == 300
    assert config["locator_timeout_seconds"] == 10
    assert config["stuck_state_threshold"] == 3
    assert config["risk_approval_required"] == "CRITICAL"
    assert config["environment"] == "development"
    assert config["allowlist_mode"] == "production"
    assert config["allowlist_path"] == "config/allowlist.json"


def test_dotenv_from_working_directory_and_explicit_env_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copied .env loads from the current directory; exported values win."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "BROWSER_DRIVER=firefox\nTARGET_APP_PORT=5050\n"
        "RISK_APPROVAL_REQUIRED=RISKY\nENVIRONMENT=staging\n",
        encoding="utf-8",
    )
    with patch.dict(os.environ, {"TARGET_APP_PORT": "5051"}, clear=True):
        config = load_config()
    assert config["browser_driver"] == "firefox"
    assert config["target_app_port"] == 5051
    assert config["risk_approval_required"] == "RISKY"
    assert config["environment"] == "staging"


def test_explicit_config_file_and_missing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The --config path can supply settings without a current-directory .env."""
    monkeypatch.chdir(tmp_path)
    chosen = tmp_path / "chosen.env"
    chosen.write_text("LOG_LEVEL=DEBUG\nLOCATOR_TIMEOUT_SECONDS=8\n", encoding="utf-8")
    with patch.dict(os.environ, {}, clear=True):
        config = load_config(str(chosen))
    assert config["log_level"] == "DEBUG"
    assert config["locator_timeout_seconds"] == 8
    with pytest.raises(FileNotFoundError):
        load_config(str(tmp_path / "missing.env"))


@pytest.mark.parametrize(("key", "value"), [
    ("BROWSER_DRIVER", "safari"), ("LOG_LEVEL", "TRACE"),
    ("REDACTION_LEVEL", "PUBLIC"), ("EVIDENCE_CAPTURE_LEVEL", "OFF"),
    ("TARGET_APP_PORT", "65536"), ("TARGET_APP_HOST", "0.0.0.0"),
    ("MAX_REPLAY_STEPS", "0"), ("REPLAY_TIMEOUT_SECONDS", "-1"),
    ("LOCATOR_TIMEOUT_SECONDS", "0"), ("STUCK_STATE_THRESHOLD", "none"),
    ("RISK_APPROVAL_REQUIRED", "SKIP"), ("ENVIRONMENT", "testing"),
    ("ALLOWLIST_MODE", "disabled"),
])
def test_configuration_rejects_invalid_settings(
    key: str, value: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Invalid safety and runtime options fail before browser startup."""
    monkeypatch.chdir(tmp_path)
    with patch.dict(os.environ, {key: value}, clear=True):
        with pytest.raises(ConfigurationError, match=key):
            load_config()


def test_production_disallows_development_bypass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Deployment mode cannot accidentally enable the testing bypass."""
    monkeypatch.chdir(tmp_path)
    with patch.dict(os.environ, {"ENVIRONMENT": "production", "ALLOWLIST_MODE": "development"}, clear=True):
        with pytest.raises(ConfigurationError, match="ALLOWLIST_MODE"):
            load_config()
    with patch.dict(os.environ, {"ENVIRONMENT": "production", "REDACTION_LEVEL": "NONE"}, clear=True):
        with pytest.raises(ConfigurationError, match="REDACTION_LEVEL"):
            load_config()


def test_example_environment_lists_supported_options() -> None:
    """The public template is parseable, complete, and matches its legacy copy."""
    template = ROOT / ".env.example"
    settings = dotenv_values(template)
    required = {
        "ANTHROPIC_API_KEY", "BROWSER_DRIVER", "LOG_LEVEL", "REDACTION_LEVEL",
        "EVIDENCE_CAPTURE_LEVEL", "TARGET_APP_PORT", "FLASK_SECRET_KEY",
        "MAX_REPLAY_STEPS", "REPLAY_TIMEOUT_SECONDS", "ALLOWLIST_PATH",
        "LOCATOR_TIMEOUT_SECONDS", "STUCK_STATE_THRESHOLD",
        "RISK_APPROVAL_REQUIRED", "ENVIRONMENT",
    }
    assert required <= settings.keys()
    assert settings["ANTHROPIC_API_KEY"] == ""
    assert settings["FLASK_SECRET_KEY"] == ""
    assert settings["ENVIRONMENT"] == "development"
    assert dotenv_values(ROOT / "config.example.env") == settings
    ignored = (ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "\n.env\n" in ignored
    assert "\n!.env.example\n" in ignored


def test_json_examples_validate_and_only_allow_reviewed_pages() -> None:
    """Both sample allowlists load and restrict navigation and actions."""
    for path in sorted((ROOT / "config").glob("*.json")):
        parsed = json.loads(path.read_text(encoding="utf-8"))
        assert isinstance(parsed, dict)
    basic = load_allowlist(str(ROOT / "config/allowlist.example.json"))
    demo = load_allowlist(str(ROOT / "config/target_app.allowlist.example.json"))
    assert is_url_allowed("http://localhost:5000/members/search", basic)
    assert not is_url_allowed("http://localhost:5000/login", basic)
    assert is_url_allowed("http://localhost:5000/login", demo)
    assert is_url_allowed("http://127.0.0.1:5000/members/12345", demo)
    assert not is_url_allowed("https://banking.example.com/members/12345", demo)
    assert not demo.allow_new_urls and not demo.allow_unknown_elements
