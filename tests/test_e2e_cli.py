"""Exercise the installed CLI boundary, configuration, and local demo routes."""

from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.artifact.schema import AutomationArtifact
from src.artifact.serializer import load_artifact_from_file, save_artifact_to_file
from src.config import load_config
from src.main import main
from src.target_app.local_app import create_app
from tests.test_cli import settings
from tests.test_local_app import login, search
from tests.test_replay_engine import artifact, driver


@pytest.fixture
def cli_artifact(artifact: AutomationArtifact) -> AutomationArtifact:
    """Use absolute navigation as required by the CLI's artifact validator."""
    payload = artifact.to_dict()
    payload["steps"][0]["value"] = "https://banking.example.com/members/search"
    return AutomationArtifact.from_dict(payload)


@pytest.mark.parametrize("command", ["discovery", "replay", "test-app", "validate", "export"])
def test_command_help_runs_as_module(command: str) -> None:
    """Public commands can be invoked without starting a browser or server."""
    process = subprocess.run(
        [sys.executable, "-m", "src.cli", command, "--help"],
        cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
        check=False, timeout=15, env=os.environ.copy(),
    )
    assert process.returncode == 0, process.stderr
    assert "usage:" in process.stdout and command in process.stdout


def test_discovery_command_records_artifact(
    settings: dict[str, object], cli_artifact: AutomationArtifact, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A mocked Claude boundary still exercises CLI validation and persistence."""
    settings["allowed_domains"] = "banking.example.com"
    browser = Mock()
    agent = Mock(return_value={"success": True, "artifact": cli_artifact.to_dict()})
    monkeypatch.setattr("src.cli.create_driver", lambda _config: browser)
    monkeypatch.setattr("src.agent.llm_agent.run_goal_driven_loop", agent)
    destination = tmp_path / "recorded.json"

    status = main(["discovery", "--url", cli_artifact.target_url, "--goal", "Find balance",
                   "--output", str(destination)])

    assert status == 0 and load_artifact_from_file(str(destination)).id == cli_artifact.id
    assert agent.call_args.args[1:3] == ("Find balance", cli_artifact.target_url)
    browser.quit.assert_called_once()


def test_replay_command_runs_real_engine(
    settings: dict[str, object], cli_artifact: AutomationArtifact, driver: Mock,
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI loads an artifact, replays it, and writes its redacted result."""
    settings["allowed_domains"] = "banking.example.com"
    settings["allowlist_path"] = os.environ["ALLOWLIST_PATH"]
    monkeypatch.setattr("src.cli.create_driver", lambda _config: driver)
    saved = tmp_path / "lookup.json"
    result_path = tmp_path / "result.json"
    save_artifact_to_file(cli_artifact, str(saved))

    status = main(["replay", "--artifact", str(saved), "--params", "member_id=12345",
                   "--output", str(result_path)])

    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert status == 0 and result["status"] == "success"
    assert result["outputs"]["savings_balance"] == "***REDACTED***"
    assert "12345" not in result_path.read_text(encoding="utf-8")
    driver.quit.assert_called_once()


def test_config_defaults_and_dotenv_precedence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defaults, a selected dotenv file, and explicit environment settings compose."""
    monkeypatch.chdir(tmp_path)
    for name in ("BROWSER_DRIVER", "LOG_LEVEL", "MAX_REPLAY_STEPS", "TARGET_APP_PORT"):
        monkeypatch.delenv(name, raising=False)
    defaults = load_config()
    assert defaults["browser_driver"] == "chrome"
    assert defaults["max_replay_steps"] == 100
    dotenv = tmp_path / "settings.env"
    dotenv.write_text("BROWSER_DRIVER=firefox\nTARGET_APP_PORT=5051\nLOG_LEVEL=DEBUG\n", encoding="utf-8")
    monkeypatch.setenv("LOG_LEVEL", "ERROR")

    selected = load_config(str(dotenv))

    assert selected["browser_driver"] == "firefox"
    assert selected["target_app_port"] == 5051
    assert selected["log_level"] == "ERROR"


@pytest.mark.parametrize(("scenario", "member_id", "expected"), [
    ("success", "12345", b"$5000.00"),
    ("success", "54321", b"$10000.00"),
    ("not_found", "12345", b"No such member"),
    ("invalid_input", "12345", b"Member ID must be numeric"),
])
def test_flask_routes_work(scenario: str, member_id: str, expected: bytes) -> None:
    """Login, CSRF, search, scenario state, and detail rendering work together."""
    app = create_app({"TESTING": True, "FLASK_SECRET_KEY": "integration-test-key-32-characters-long"})
    client = app.test_client()
    assert client.get(f"/?scenario={scenario}").status_code == 200
    assert login(client).status_code == 302
    response = search(client, member_id)
    if scenario == "success":
        assert response.status_code == 302
        response = client.get(response.location)
    assert response.status_code == 200 and expected in response.data
    assert client.get("/logout").status_code == 302
    assert client.get("/members/search").status_code == 302


def test_missing_cli_input_never_starts_browser(
    settings: dict[str, object], cli_artifact: AutomationArtifact, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing declared inputs fail before creating a Selenium session."""
    saved = tmp_path / "lookup.json"
    save_artifact_to_file(cli_artifact, str(saved))
    browser_factory = Mock()
    monkeypatch.setattr("src.cli.create_driver", browser_factory)
    assert main(["replay", "--artifact", str(saved)]) == 2
    assert "Missing required inputs" in capsys.readouterr().err
    browser_factory.assert_not_called()


@pytest.mark.parametrize("format_name", ["json", "csv", "markdown"])
def test_export_formats_remain_readable(
    settings: dict[str, object], cli_artifact: AutomationArtifact,
    format_name: str, tmp_path: Path,
) -> None:
    """The same validated artifact exports through all documented formats."""
    saved = tmp_path / "lookup.json"
    destination = tmp_path / f"lookup.{format_name}"
    save_artifact_to_file(cli_artifact, str(saved))
    assert main(["export", str(saved), "--format", format_name,
                 "--output", str(destination)]) == 0
    content = destination.read_text(encoding="utf-8")
    if format_name == "json":
        assert json.loads(content)["id"] == cli_artifact.id
    elif format_name == "csv":
        assert len(list(csv.DictReader(content.splitlines()))) == len(cli_artifact.steps)
    else:
        assert cli_artifact.name in content and "Checkpoint:" in content


def test_test_app_command_wires_scenario_without_listening(
    settings: dict[str, object], monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI selects a scenario while the network listener remains mocked."""
    server = Mock()
    factory = Mock(return_value=server)
    monkeypatch.setattr("src.target_app.local_app.create_app", factory)
    assert main(["test-app", "--scenario", "delayed", "--port", "5111"]) == 0
    assert factory.call_args.args[0]["DEFAULT_SCENARIO"] == "delayed"
    server.run.assert_called_once_with(host="127.0.0.1", port=5111, debug=False,
                                       use_reloader=False)
