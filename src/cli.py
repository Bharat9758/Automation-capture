"""Argument parser and user-facing handlers for AutomationCapture."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import platform
import re
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Iterator

from selenium.webdriver.remote.webdriver import WebDriver

from src.artifact.recorder import validate_artifact
from src.artifact.schema import AutomationArtifact
from src.artifact.serializer import load_artifact_from_file, save_artifact_to_file, to_json
from src.config import ConfigurationError
from src.safety.data_redactor import load_redaction_policy, redact_dict


class ArtifactInvalidError(ValueError):
    """Indicate a structurally valid file that cannot be safely executed."""


class CommandFailure(RuntimeError):
    """Describe a command that completed unsuccessfully."""


def _global_options(parser: argparse.ArgumentParser, *, root: bool = False) -> None:
    """Allow common flags both before and after a subcommand.

    Args:
        parser: Root or subcommand parser.
        root: Whether to supply default values.
    """
    fallback: Any = None if root else argparse.SUPPRESS
    parser.add_argument("--verbose", action="store_true", default=False if root else fallback,
                        help="Show diagnostic JSON logs")
    parser.add_argument("--quiet", action="store_true", default=False if root else fallback,
                        help="Suppress routine console output")
    parser.add_argument("--log-file", default=fallback, help="Append redacted JSON lines to a private file")
    parser.add_argument("--config", default=fallback, help="Path to a dotenv configuration file")


def build_parser() -> argparse.ArgumentParser:
    """Build the command parser with all supported subcommands.

    Returns:
        Configured argparse parser.
    """
    parser = argparse.ArgumentParser(prog="automation-capture",
                                     description="Computer-use automation recording and replay")
    _global_options(parser, root=True)
    commands = parser.add_subparsers(dest="command", required=True)

    discovery = commands.add_parser("discovery", aliases=["discover"], help="Discover and record a browser workflow")
    _global_options(discovery)
    discovery.add_argument("--url", required=True, help="Allowlisted start URL")
    discovery.add_argument("--goal", required=True, help="Goal sent to Claude")
    discovery.add_argument("--output", help="Destination artifact file")
    discovery.add_argument("--max-steps", type=int, default=20)
    discovery.add_argument("--timeout", type=int, default=300, help="Overall discovery timeout in seconds")

    replay = commands.add_parser("replay", help="Execute an artifact without the LLM")
    _global_options(replay)
    replay.add_argument("--artifact", required=True, help="Saved artifact path")
    replay.add_argument("--url", help="Override the artifact target URL")
    replay.add_argument("--params", nargs="+", action="extend", default=[], metavar="KEY=VALUE")
    replay.add_argument("--output", help="Write a redacted JSON result")
    replay.add_argument("--redaction-level", choices=["NONE", "BASIC", "STRICT", "PARANOID"])
    replay.add_argument("--evidence-capture", choices=["ALL", "ERRORS", "ESCALATIONS_ONLY"])

    test_app = commands.add_parser("test-app", help="Run the local Flask member demo")
    _global_options(test_app)
    test_app.add_argument("--port", type=int, help="Loopback listening port")
    test_app.add_argument("--debug", action="store_true", help="Enable Flask debugging on loopback only")
    test_app.add_argument("--scenario", choices=["success", "not_found", "invalid_input", "delayed", "flaky"],
                          default="success")

    validate = commands.add_parser("validate", help="Validate an artifact file")
    _global_options(validate)
    validate.add_argument("artifact")

    listing = commands.add_parser("list-artifacts", help="List saved artifacts")
    _global_options(listing)
    listing.add_argument("--directory", help="Directory containing artifact JSON files")

    inspect = commands.add_parser("inspect", help="Show artifact structure")
    _global_options(inspect)
    inspect.add_argument("artifact")

    export = commands.add_parser("export", help="Export an artifact as JSON, CSV, or Markdown")
    _global_options(export)
    export.add_argument("artifact")
    export.add_argument("--format", choices=["json", "markdown", "csv"], default="json")
    export.add_argument("--output", help="Destination; stdout when omitted")

    escalations = commands.add_parser("escalations", help="List pending or saved human handoffs")
    _global_options(escalations)
    escalations.add_argument("--directory", help="Escalation request directory")

    system = commands.add_parser("version", help="Print package and configuration status")
    _global_options(system)
    return parser


def create_driver(config: dict[str, Any]) -> WebDriver:
    """Launch a supported Selenium browser with configured timeouts.

    Args:
        config: Validated runtime settings.

    Returns:
        Active WebDriver; caller must quit it.
    """
    from selenium import webdriver

    if config["browser_driver"] == "chrome":
        options = webdriver.ChromeOptions()
        if config["selenium_headless"]:
            options.add_argument("--headless=new")
        driver = webdriver.Chrome(options=options)
    else:
        options = webdriver.FirefoxOptions()
        if config["selenium_headless"]:
            options.add_argument("-headless")
        driver = webdriver.Firefox(options=options)
    driver.set_page_load_timeout(config["selenium_timeout_ms"] / 1000)
    return driver


@contextmanager
def progress(label: str, quiet: bool) -> Iterator[None]:
    """Show an honest activity indicator during a blocking browser operation.

    Args:
        label: Current task.
        quiet: Whether to suppress status output.

    Yields:
        Control to the blocking operation.
    """
    if quiet:
        yield
        return
    if not sys.stderr.isatty():
        print(f"{label}...", file=sys.stderr)
        yield
        return
    stopped = threading.Event()

    def animate() -> None:
        """Update the terminal while the browser operation runs."""
        symbols = ("|", "/", "-", "\\")
        count = 0
        while not stopped.wait(0.15):
            print(f"\r{symbols[count % len(symbols)]} {label}", end="", file=sys.stderr, flush=True)
            count += 1

    worker = threading.Thread(target=animate, daemon=True)
    worker.start()
    try:
        yield
    finally:
        stopped.set()
        worker.join(timeout=1)
        print("\r" + " " * (len(label) + 3) + "\r", end="", file=sys.stderr, flush=True)


def _print(message: str, args: argparse.Namespace) -> None:
    """Print an ordinary status line unless quiet was selected.

    Args:
        message: Public status text.
        args: Parsed global options.
    """
    if not args.quiet:
        print(message)


def _save_private(path: str, body: str) -> str:
    """Atomically write a user-requested export with private permissions.

    Args:
        path: Output destination.
        body: UTF-8 content.

    Returns:
        Written path.
    """
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=destination.parent,
                                         prefix=".automation-", delete=False) as handle:
            temporary = handle.name
            os.chmod(temporary, 0o600)
            handle.write(body)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        os.chmod(destination, 0o600)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    return str(destination)


def _load_validated(path: str) -> AutomationArtifact:
    """Load an artifact and check replay-specific constraints.

    Args:
        path: Artifact file.

    Returns:
        Validated artifact.

    Raises:
        ArtifactInvalidError: If the artifact cannot replay safely.
    """
    artifact = load_artifact_from_file(path)
    valid, explanation = validate_artifact(artifact)
    if not valid:
        raise ArtifactInvalidError(explanation)
    return artifact


def _params(items: list[str], artifact: AutomationArtifact) -> dict[str, Any]:
    """Parse declared CLI inputs without printing their values.

    Args:
        items: Key=value arguments.
        artifact: Schema declaring input types.

    Returns:
        Named, typed replay inputs.

    Raises:
        ConfigurationError: If a parameter is missing, repeated, or malformed.
    """
    declarations = {item.name: item for item in artifact.inputs}
    parsed: dict[str, Any] = {}
    for item in items:
        name, separator, value = item.partition("=")
        if not separator or name not in declarations or name in parsed:
            raise ConfigurationError("--params must contain unique declared KEY=VALUE pairs")
        kind = declarations[name].type
        if kind == "number":
            try:
                number = Decimal(value)
            except InvalidOperation as exc:
                raise ConfigurationError(f"Input {name} must be a number") from exc
            if not number.is_finite():
                raise ConfigurationError(f"Input {name} must be finite")
            parsed[name] = number
        else:
            parsed[name] = value
    missing = [item.name for item in artifact.inputs if item.required and item.name not in parsed]
    if missing:
        raise ConfigurationError("Missing required inputs: " + ", ".join(missing))
    return parsed


def handle_discovery(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Run Claude-guided discovery and save a replayable artifact.

    Args:
        args: Discovery arguments.
        config: Validated settings.

    Returns:
        Process exit status.
    """
    from src.agent.llm_agent import run_goal_driven_loop

    if not config["anthropic_api_key"]:
        raise ConfigurationError("ANTHROPIC_API_KEY is required for discovery")
    if not config["allowed_domains"]:
        raise ConfigurationError("ALLOWED_DOMAINS is required for browser navigation")
    if args.max_steps <= 0 or args.timeout <= 0:
        raise ConfigurationError("--max-steps and --timeout must be positive")
    started = time.monotonic()
    with progress("Starting discovery", args.quiet):
        driver = create_driver(config)
        try:
            result = run_goal_driven_loop(driver, args.goal, args.url, args.max_steps,
                                          args.timeout, api_key=config["anthropic_api_key"])
        finally:
            driver.quit()
    if not result.get("success"):
        raise CommandFailure("Discovery did not reach its goal; check the URL, allowlist and agent logs")
    payload = result.get("artifact")
    if not isinstance(payload, dict):
        raise ArtifactInvalidError(result.get("artifact_error") or "Discovery did not produce a replayable artifact")
    artifact = AutomationArtifact.from_dict(payload)
    valid, explanation = validate_artifact(artifact)
    if not valid:
        raise ArtifactInvalidError(explanation)
    if args.output:
        output = args.output
    else:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", artifact.id):
            raise ArtifactInvalidError("Artifact ID cannot be used as a filename; specify --output")
        output = str(Path(config["artifact_directory"]) / f"{artifact.id}.json")
    save_artifact_to_file(artifact, output)
    _print(f"Discovery complete: {len(artifact.steps)} steps in {time.monotonic() - started:.2f}s", args)
    _print(f"Artifact saved: {output}", args)
    return 0


def handle_replay(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Replay one saved artifact and optionally write a redacted summary.

    Args:
        args: Replay command arguments.
        config: Validated settings.

    Returns:
        Zero on success or legitimate business outcome; one otherwise.
    """
    from src.replay.replay_engine import replay_artifact

    artifact = _load_validated(args.artifact)
    if args.url:
        try:
            artifact = replace(artifact, target_url=args.url)
        except ValueError as exc:
            raise ConfigurationError("--url must be an absolute HTTP(S) base URL") from exc
    if len(artifact.steps) > config["max_replay_steps"]:
        raise ConfigurationError("Artifact exceeds MAX_REPLAY_STEPS")
    if not config["allowlist_path"] or not Path(config["allowlist_path"]).is_file():
        raise ConfigurationError("ALLOWLIST_PATH must point to an existing reviewed allowlist")
    if not config["allowed_domains"]:
        raise ConfigurationError("ALLOWED_DOMAINS is required for browser navigation")
    params = _params(args.params, artifact)
    level = args.redaction_level or config["redaction_level"]
    os.environ["REDACTION_LEVEL"] = level
    policy = load_redaction_policy(level)
    capture = args.evidence_capture or config["evidence_capture_level"]
    started = time.monotonic()
    with progress("Replaying artifact", args.quiet):
        driver = create_driver(config)
        try:
            result = replay_artifact(driver, artifact, params, max_wait_ms=config["selenium_timeout_ms"],
                                     evidence_capture_level=capture,
                                     timeout_seconds=config["replay_timeout_seconds"])
        finally:
            driver.quit()
    summary = {"success": result.success, "status": result.status,
               "business_outcome": result.business_outcome, "outputs": result.outputs,
               "step_failed": result.step_failed, "duration_seconds": result.duration_seconds,
               "audit_log_path": result.audit_log_path,
               "evidence_manifest_path": result.evidence_manifest_path,
               "evidence_capture_errors": result.evidence_capture_errors}
    safe = redact_dict(summary, policy)
    if args.output:
        _save_private(args.output, json.dumps(safe, indent=2, default=str) + "\n")
        _print(f"Result saved: {args.output}", args)
    _print(f"Replay {result.status} in {time.monotonic() - started:.2f}s", args)
    _print(f"Executed steps: {len(result.session.step_executions) if result.session else 0}", args)
    if result.business_outcome:
        _print(f"Business outcome: {result.business_outcome}", args)
    return 0 if result.status in {"success", "business_outcome"} else 1


def handle_test_app(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Start the demo Flask application on a loopback interface.

    Args:
        args: Server flags.
        config: Validated settings.

    Returns:
        Zero when the server is stopped normally.
    """
    from src.target_app.local_app import create_app

    host = config["target_app_host"]
    port = args.port if args.port is not None else config["target_app_port"]
    if host not in {"127.0.0.1", "localhost", "::1"} or not 1 <= port <= 65535:
        raise ConfigurationError("Test app requires a loopback host and port 1–65535")
    if not config["flask_secret_key"]:
        raise ConfigurationError("FLASK_SECRET_KEY is required for the test app")
    options = {"FLASK_SECRET_KEY": config["flask_secret_key"], "DEFAULT_SCENARIO": args.scenario}
    if config["target_app_config_path"]:
        options["TARGET_APP_CONFIG_PATH"] = config["target_app_config_path"]
    app = create_app(options)
    _print(f"Test app running at http://{host}:{port}/ (scenario: {args.scenario})", args)
    app.run(host=host, port=port, debug=args.debug, use_reloader=False)
    return 0


def handle_validate(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Validate a saved artifact for replay.

    Args:
        args: Artifact path.
        config: Runtime settings, unused by schema validation.

    Returns:
        Success exit status.
    """
    artifact = _load_validated(args.artifact)
    _print(f"Valid artifact: {artifact.id} ({len(artifact.steps)} steps)", args)
    return 0


def handle_list_artifacts(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """List usable artifact files without printing recorded values.

    Args:
        args: Directory option.
        config: Default artifact directory.

    Returns:
        Success exit status.
    """
    directory = Path(args.directory or config["artifact_directory"])
    if not directory.is_dir():
        raise FileNotFoundError(f"Artifact directory not found: {directory}")
    _print("ID | Goal | Steps | Created", args)
    for path in sorted(directory.glob("*.json")):
        try:
            artifact = _load_validated(str(path))
        except (ValueError, TypeError):
            _print(f"Skipped invalid artifact: {path.name}", args)
            continue
        _print(f"{artifact.id} | {artifact.name} | {len(artifact.steps)} | {artifact.created_at}", args)
    return 0


def handle_inspect(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Explain a recorded artifact without showing typed values.

    Args:
        args: Artifact path.
        config: Runtime settings, unused by inspection.

    Returns:
        Success exit status.
    """
    artifact = _load_validated(args.artifact)
    for heading, value in (("ID", artifact.id), ("Goal", artifact.name),
                           ("Description", artifact.description), ("Target", artifact.target_url),
                           ("Version", artifact.version)):
        _print(f"{heading}: {value}", args)
    _print("Inputs: " + ", ".join(f"{item.name} ({item.type})" for item in artifact.inputs), args)
    _print("Outputs: " + ", ".join(f"{item.name} ({item.type})" for item in artifact.outputs), args)
    _print("Steps:", args)
    for step in artifact.steps:
        strategy = step.locator.strategy if step.locator else "none"
        _print(f"  {step.step_number}. {step.action} [{strategy}]", args)
    _print(f"Success checkpoint: {artifact.success_checkpoint.condition}", args)
    _print("Known errors: " + (", ".join(artifact.known_errors) or "none"), args)
    return 0


def _csv_safe(text: str) -> str:
    """Prevent spreadsheet formula execution in exported artifact cells.

    Args:
        text: Cell value.

    Returns:
        Escaped cell text.
    """
    return "'" + text if text.lstrip().startswith(("=", "+", "-", "@")) else text


def _export_text(artifact: AutomationArtifact, format_name: str) -> str:
    """Render a validated artifact in a requested interchange format.

    Args:
        artifact: Parsed artifact.
        format_name: json, markdown, or csv.

    Returns:
        UTF-8 export text.
    """
    if format_name == "json":
        return to_json(artifact) + "\n"
    if format_name == "csv":
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["step", "action", "locator_strategy", "locator_value", "value", "expected_outcome"])
        for step in artifact.steps:
            writer.writerow([_csv_safe(str(item)) for item in
                             (step.step_number, step.action, step.locator.strategy if step.locator else "",
                              step.locator.value if step.locator else "", step.value or "", step.expected_outcome)])
        return buffer.getvalue()

    def cell(value: str) -> str:
        """Keep recorded prose inside one Markdown table cell.

        Args:
            value: Raw artifact text.

        Returns:
            Escaped Markdown cell.
        """
        return value.replace("\\", "\\\\").replace("|", "\\|").replace("\n", " ")

    lines = [f"# {cell(artifact.name)}", "", cell(artifact.description), "",
             f"Target: {cell(artifact.target_url)}", "", "| Step | Action | Locator | Value |",
             "| --- | --- | --- | --- |"]
    for step in artifact.steps:
        lines.append("| " + " | ".join((str(step.step_number), step.action,
                                         cell(step.locator.value if step.locator else ""),
                                         cell(step.value or ""))) + " |")
    lines.extend(("", f"Checkpoint: {cell(artifact.success_checkpoint.condition)}"))
    return "\n".join(lines) + "\n"


def handle_export(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Write or print an artifact export.

    Args:
        args: Artifact path, format, and destination.
        config: Runtime settings, unused by export.

    Returns:
        Success exit status.
    """
    body = _export_text(_load_validated(args.artifact), args.format)
    if args.output:
        _save_private(args.output, body)
        _print(f"Exported {args.format}: {args.output}", args)
    elif not args.quiet:
        print(body, end="")
    return 0


def handle_escalations(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """List handoff IDs and steps without loading raw browser evidence.

    Args:
        args: Optional escalation directory.
        config: Configured default directory.

    Returns:
        Success exit status.
    """
    folder = Path(args.directory or os.environ.get("ESCALATION_DIRECTORY", "evidence/escalations"))
    if not folder.is_dir():
        raise FileNotFoundError(f"Escalation directory not found: {folder}")
    _print("Escalation ID | Artifact | Step | Timestamp", args)
    for file in sorted(folder.glob("*.json")):
        try:
            record = json.loads(file.read_text(encoding="utf-8"))
            _print(f"{record['escalation_id']} | {record['artifact_id']} | "
                   f"{record['current_step']} | {record['timestamp']}", args)
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            _print(f"Skipped invalid escalation: {file.name}", args)
    return 0


def handle_version(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Show package versions and configuration presence without secrets.

    Args:
        args: Global display flags.
        config: Validated runtime settings.

    Returns:
        Success exit status.
    """
    for name in ("automation-capture", "selenium", "anthropic", "flask", "pytest"):
        try:
            installed = version(name)
        except PackageNotFoundError:
            installed = "not installed"
        _print(f"{name}: {installed}", args)
    _print(f"Python: {platform.python_version()}", args)
    _print(f"Browser: {config['browser_driver']}", args)
    _print(f"Claude key configured: {bool(config['anthropic_api_key'])}", args)
    _print(f"Allowlist configured: {bool(config['allowlist_path'])}", args)
    return 0


def execute(args: argparse.Namespace, config: dict[str, Any]) -> int:
    """Dispatch a parsed command to its handler.

    Args:
        args: Parsed command namespace.
        config: Validated settings.

    Returns:
        Handler exit status.
    """
    handlers = {"discovery": handle_discovery, "discover": handle_discovery,
                "replay": handle_replay, "test-app": handle_test_app,
                "validate": handle_validate, "list-artifacts": handle_list_artifacts,
                "inspect": handle_inspect, "export": handle_export,
                "escalations": handle_escalations, "version": handle_version}
    return handlers[args.command](args, config)


def main(argv: list[str] | None = None) -> int:
    """Use the same entry point from the module and installed console script.

    Args:
        argv: Optional test-supplied command-line arguments.

    Returns:
        Process exit code.
    """
    from src.main import main as run_main

    return run_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
