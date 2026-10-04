"""Validated process entry point for every AutomationCapture CLI command."""

from __future__ import annotations

import argparse
import sys

from jsonschema import ValidationError as JsonSchemaValidationError
from pydantic import ValidationError as PydanticValidationError

from src.artifact.serializer import InvalidArtifactError, SchemaValidationError
from src.cli import ArtifactInvalidError, CommandFailure, build_parser, execute
from src.config import ConfigurationError, load_config
from src.logging import configure_logging, get_logger


LOGGER = get_logger(__name__)


def main(argv: list[str] | None = None) -> int:
    """Parse arguments, configure JSON logs, and return a documented exit code.

    Args:
        argv: Optional test arguments; defaults to process arguments.

    Returns:
        0 success, 1 runtime failure, 2 configuration failure, 3 missing
        file, 4 invalid artifact, or 130 keyboard interruption.
    """
    parser: argparse.ArgumentParser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    try:
        if args.verbose and args.quiet:
            raise ConfigurationError("--verbose and --quiet cannot be used together")
        config = load_config(args.config)
        configure_logging("DEBUG" if args.verbose else config["log_level"],
                          log_file=args.log_file, quiet=args.quiet)
        return execute(args, config)
    except FileNotFoundError as exc:
        print(f"File not found: {exc}. Check the supplied path.", file=sys.stderr)
        return 3
    except (ArtifactInvalidError, InvalidArtifactError, SchemaValidationError,
            JsonSchemaValidationError, PydanticValidationError) as exc:
        LOGGER.warning("artifact_invalid", extra={"event": "artifact_invalid", "error_type": type(exc).__name__})
        detail = str(exc).splitlines()[0] if isinstance(exc, (ArtifactInvalidError, InvalidArtifactError, SchemaValidationError)) else "schema validation failed"
        print(f"Invalid artifact: {detail}", file=sys.stderr)
        return 4
    except (ConfigurationError, ValueError) as exc:
        print(f"Configuration error: {str(exc).splitlines()[0]}", file=sys.stderr)
        return 2
    except CommandFailure as exc:
        print(f"Operation failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        LOGGER.error("cli_failed", extra={"event": "cli_failed", "error_type": type(exc).__name__})
        print(f"Operation failed: {type(exc).__name__}. Check the JSON log for details.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
