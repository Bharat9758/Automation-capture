"""Command-line parsing, safety, and handler integration tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.artifact.schema import AutomationArtifact
from src.artifact.serializer import save_artifact_to_file
from src.cli import _csv_safe, build_parser
from src.config import ConfigurationError, load_config
from src.main import main


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, object]:
    """Supply isolated CLI configuration without an on-disk dotenv file.

    Args:
        monkeypatch: Environment and module patch helper.
        tmp_path: Isolated test output directory.

    Returns:
        Runtime configuration used by command tests.
    """
    config: dict[str, object] = {
        "anthropic_api_key": "test-key", "allowed_domains": "localhost:5000",
        "browser_driver": "chrome", "selenium_headless": True,
        "selenium_timeout_ms": 1000, "allowlist_path": str(tmp_path / "allowlist.json"),
        "redaction_level": "STRICT", "evidence_capture_level": "ERRORS",
        "log_level": "INFO", "max_replay_steps": 100, "replay_timeout_seconds": 300,
        "artifact_directory": str(tmp_path / "artifacts"), "target_app_port": 5000,
        "target_app_host": "127.0.0.1", "flask_secret_key": "x" * 40,
        "target_app_config_path": None,
    }
    monkeypatch.setattr("src.main.load_config", lambda _path=None: config)
    monkeypatch.setattr("src.main.configure_logging", lambda *_args, **_kwargs: None)
    (tmp_path / "allowlist.json").write_text('{"pages":[]}', encoding="utf-8")
    return config


@pytest.fixture
def artifact(tmp_path: Path) -> tuple[AutomationArtifact, Path]:
    """Create a valid saved artifact for read-only CLI commands.

    Args:
        tmp_path: Isolated artifact directory.

    Returns:
        Parsed artifact and serialized path.
    """
    item = AutomationArtifact.from_dict({
        "id": "lookup", "name": "Lookup", "version": "1.0.0",
        "description": "Read a balance", "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z", "created_by": "test",
        "target_url": "http://localhost:5000/members/search",
        "inputs": [{"name": "member_id", "type": "string", "description": "Member",
                    "required": True, "example": "12345"}],
        "outputs": [{"name": "balance", "type": "string", "description": "Balance",
                     "extraction_locator": {"strategy": "id", "value": "balance",
                                            "robustness_notes": "Stable ID"}}],
        "steps": [{"step_number": 1, "action": "type", "locator": {"strategy": "id",
                   "value": "member-id", "robustness_notes": "Stable ID"},
                   "value": "{member_id}", "reasoning": "Enter ID",
                   "expected_outcome": "Field accepts input"}],
        "success_checkpoint": {"condition": "element_visible",
                               "locator": {"strategy": "id", "value": "balance",
                                           "robustness_notes": "Stable ID"},
                               "expected_value": None, "error_message": "No balance"},
        "known_errors": {}, "discovery_run_id": "test-run", "success_rate": None,
    })
    path = tmp_path / "artifacts" / "lookup.json"
    save_artifact_to_file(item, str(path))
    return item, path


def test_parser_accepts_commands_and_global_flags() -> None:
    """Common flags work on either side of a subcommand."""
    parser = build_parser()
    assert parser.parse_args(["--verbose", "discover", "--url", "http://localhost:5000",
                              "--goal", "Search"]).verbose
    assert parser.parse_args(["replay", "--artifact", "a.json", "--quiet",
                              "--params", "member_id=123"]).quiet
    for command in ("test-app", "version", "list-artifacts", "escalations"):
        assert parser.parse_args([command]).command == command


def test_load_config_validates_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Configuration rejects invalid driver, numeric limits, and missing files."""
    with pytest.raises(FileNotFoundError):
        load_config(str(tmp_path / "missing.env"))
    monkeypatch.setenv("BROWSER_DRIVER", "safari")
    with pytest.raises(ConfigurationError, match="BROWSER_DRIVER"):
        load_config()
    monkeypatch.setenv("BROWSER_DRIVER", "firefox")
    monkeypatch.setenv("MAX_REPLAY_STEPS", "0")
    with pytest.raises(ConfigurationError, match="MAX_REPLAY_STEPS"):
        load_config()


def test_artifact_commands(settings: dict[str, object], artifact: tuple[AutomationArtifact, Path],
                           tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Validate, inspect, list, and export the same saved artifact."""
    _, path = artifact
    assert main(["validate", str(path)]) == 0
    assert "Valid artifact: lookup" in capsys.readouterr().out
    assert main(["inspect", str(path)]) == 0
    assert "Success checkpoint: element_visible" in capsys.readouterr().out
    assert main(["list-artifacts", "--directory", str(path.parent)]) == 0
    assert "lookup | Lookup | 1" in capsys.readouterr().out
    markdown = tmp_path / "lookup.md"
    assert main(["export", str(path), "--format", "markdown", "--output", str(markdown)]) == 0
    assert "| 1 | type |" in markdown.read_text(encoding="utf-8")
    capsys.readouterr()
    assert main(["export", str(path), "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["id"] == "lookup"
    assert _csv_safe("=1+1") == "'=1+1"


def test_discovery_saves_artifact_and_closes_driver(settings: dict[str, object],
                                                     artifact: tuple[AutomationArtifact, Path],
                                                     tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful discovery persists the recorder result and closes Selenium."""
    item, _ = artifact
    browser = Mock()
    agent = Mock(return_value={"success": True, "artifact": item.to_dict()})
    monkeypatch.setattr("src.cli.create_driver", lambda _config: browser)
    monkeypatch.setattr("src.agent.llm_agent.run_goal_driven_loop", agent)
    output = tmp_path / "discovered.json"
    assert main(["discovery", "--url", "http://localhost:5000", "--goal", "Read balance",
                 "--output", str(output)]) == 0
    assert output.exists()
    assert agent.call_args.kwargs["api_key"] == "test-key"
    browser.quit.assert_called_once()


def test_replay_passes_options_and_redacts_output(settings: dict[str, object],
                                                   artifact: tuple[AutomationArtifact, Path],
                                                   tmp_path: Path,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    """Replay passes typed input and policy to the engine and persists a private result."""
    _, path = artifact
    browser = Mock()
    engine = Mock(return_value=SimpleNamespace(
        success=True, status="success", business_outcome=None,
        outputs={"balance": "123456789"}, step_failed=None, duration_seconds=1.0,
        audit_log_path=None, evidence_manifest_path=None, evidence_capture_errors=[],
        session=None))
    monkeypatch.setattr("src.cli.create_driver", lambda _config: browser)
    monkeypatch.setattr("src.replay.replay_engine.replay_artifact", engine)
    output = tmp_path / "result.json"
    assert main(["replay", "--artifact", str(path), "--params", "member_id=12345",
                 "--evidence-capture", "ESCALATIONS_ONLY", "--output", str(output)]) == 0
    assert engine.call_args.args[2] == {"member_id": "12345"}
    assert engine.call_args.kwargs["evidence_capture_level"] == "ESCALATIONS_ONLY"
    assert engine.call_args.kwargs["timeout_seconds"] == 300
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "success"
    browser.quit.assert_called_once()


def test_test_app_uses_loopback_and_selected_scenario(settings: dict[str, object],
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    """Test app command supplies its explicit scenario and bind settings."""
    app = Mock()
    factory = Mock(return_value=app)
    monkeypatch.setattr("src.target_app.local_app.create_app", factory)
    assert main(["test-app", "--port", "5050", "--scenario", "not_found"]) == 0
    assert factory.call_args.args[0]["DEFAULT_SCENARIO"] == "not_found"
    app.run.assert_called_once_with(host="127.0.0.1", port=5050, debug=False, use_reloader=False)
    assert main(["test-app", "--port", "0"]) == 2


def test_documented_exit_codes(settings: dict[str, object], tmp_path: Path,
                               capsys: pytest.CaptureFixture[str]) -> None:
    """Report missing files, invalid artifacts, and conflicting global settings."""
    assert main(["validate", str(tmp_path / "missing.json")]) == 3
    invalid = tmp_path / "invalid.json"
    invalid.write_text('{"id": "x"}', encoding="utf-8")
    assert main(["validate", str(invalid)]) == 4
    assert main(["--quiet", "--verbose", "version"]) == 2
    assert "Configuration error" in capsys.readouterr().err
