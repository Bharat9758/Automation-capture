"""Safe JSON conversion and atomic disk persistence for automation artifacts."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema import ValidationError as JSONValidationError
from pydantic import ValidationError as PydanticValidationError

from src.artifact.schema import ARTIFACT_JSON_SCHEMA, AutomationArtifact
from src.logging import get_logger


LOGGER = get_logger(__name__)
_SCHEMA_VALIDATOR = Draft202012Validator(ARTIFACT_JSON_SCHEMA, format_checker=FormatChecker())
_HEADER = "# AutomationCapture Artifact"
_GENERATED = "# Generated: "
_VERSION = "# Version: "


class InvalidArtifactError(ValueError):
    """Raised when artifact text or a file header cannot be parsed."""

    def __init__(self, message: str, *, line: int | None = None, column: int | None = None) -> None:
        """Keep parse location available for file-relative error reporting.

        Args:
            message: Safe explanation without artifact contents.
            line: One-based line number, if known.
            column: One-based column number, if known.
        """
        self.message = message
        self.line = line
        self.column = column
        location = f" (line {line}, column {column})" if line is not None and column is not None else f" (line {line})" if line is not None else ""
        super().__init__(message + location)


class SchemaValidationError(ValueError):
    """Raised when an artifact violates its JSON Schema or dataclass rules."""


def _path(parts: list[Any]) -> str:
    """Render a JSON validation path without including any field values.

    Args:
        parts: Object keys and array indices.

    Returns:
        Human-readable path rooted at $.
    """
    return "$" + "".join(f"[{part}]" if isinstance(part, int) else f".{part}" for part in parts)


def _specific_errors(error: JSONValidationError) -> Iterator[JSONValidationError]:
    """Resolve optional nested objects to useful field-level errors.

    Args:
        error: JSON Schema validation failure.

    Yields:
        Specific failures, excluding the irrelevant null branch of a union.
    """
    if error.validator in {"anyOf", "oneOf"} and error.context:
        children = [child for child in error.context if not (child.validator == "type" and child.validator_value == "null")]
        for child in children:
            yield from _specific_errors(child)
        if children:
            return
    yield error


def validate_artifact_schema(data: dict[str, Any]) -> tuple[bool, list[str]]:
    """Validate structure and Pydantic's cross-field artifact rules.

    Args:
        data: JSON-shaped artifact dictionary.

    Returns:
        A validity flag and safe field-level explanations.
    """
    if not isinstance(data, dict):
        return False, ["$: expected a JSON object"]
    messages: list[str] = []
    errors = sorted(_SCHEMA_VALIDATOR.iter_errors(data), key=lambda error: (tuple(map(str, error.absolute_path)), str(error.validator)))
    for parent in errors:
        for error in _specific_errors(parent):
            location = _path(list(error.absolute_path))
            if error.validator == "required" and isinstance(error.instance, dict):
                for name in error.validator_value:
                    if name not in error.instance:
                        messages.append(f"{location}.{name}: required field is missing")
            else:
                messages.append(f"{location}: {error.validator or 'schema'} validation failed")
    if messages:
        return False, list(dict.fromkeys(messages))
    try:
        AutomationArtifact.from_dict(data)
    except PydanticValidationError as exc:
        for detail in exc.errors():
            messages.append(f"{_path(list(detail.get('loc', ()) ))}: {detail['type']} validation failed")
    except (TypeError, ValueError) as exc:
        messages.append(f"$: {type(exc).__name__} validation failed")
    return not messages, messages


def artifact_to_dict(artifact: AutomationArtifact) -> dict[str, Any]:
    """Convert a Pydantic dataclass and nested objects into JSON values.

    Pydantic dataclasses use TypeAdapter-backed ``to_dict`` rather than the
    BaseModel-only ``model_dump`` method.

    Args:
        artifact: Candidate automation artifact.

    Returns:
        Fully nested JSON-shaped dictionary.

    Raises:
        SchemaValidationError: If the artifact is invalid.
    """
    if not isinstance(artifact, AutomationArtifact):
        raise SchemaValidationError("Expected an AutomationArtifact")
    try:
        data = artifact.to_dict()
    except Exception as exc:
        raise SchemaValidationError(f"Artifact could not be serialized: {type(exc).__name__}") from exc
    valid, errors = validate_artifact_schema(data)
    if not valid:
        raise SchemaValidationError("; ".join(errors))
    return data


def dict_to_artifact(data: dict[str, Any]) -> AutomationArtifact:
    """Reconstruct nested Pydantic dataclasses from JSON-shaped data.

    Args:
        data: Parsed JSON object.

    Returns:
        Validated AutomationArtifact.

    Raises:
        SchemaValidationError: If structure or cross-field rules are invalid.
    """
    valid, errors = validate_artifact_schema(data)
    if not valid:
        raise SchemaValidationError("; ".join(errors))
    return AutomationArtifact.from_dict(data)


def to_json(artifact: AutomationArtifact) -> str:
    """Serialize an artifact as plain, two-space-indented JSON.

    Args:
        artifact: Valid artifact.

    Returns:
        Standards-compliant JSON string containing every artifact field.

    Raises:
        SchemaValidationError: If serialization or validation fails.
    """
    try:
        return json.dumps(artifact_to_dict(artifact), indent=2, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        if isinstance(exc, SchemaValidationError):
            raise
        raise SchemaValidationError(f"Artifact is not valid JSON: {type(exc).__name__}") from exc


def _reject_nonstandard_number(value: str) -> None:
    """Reject NaN and Infinity, which are not JSON numbers.

    Args:
        value: Nonstandard numeric token.

    Raises:
        InvalidArtifactError: Always.
    """
    raise InvalidArtifactError(f"Nonstandard JSON number: {value}")


def from_json(json_string: str) -> AutomationArtifact:
    """Parse plain JSON and reconstruct a fully validated artifact.

    Args:
        json_string: JSON text without file metadata comments.

    Returns:
        Validated AutomationArtifact.

    Raises:
        InvalidArtifactError: If JSON is malformed or has a non-object root.
        SchemaValidationError: If artifact data fails validation.
    """
    if not isinstance(json_string, str):
        raise InvalidArtifactError("Artifact JSON must be a string")
    try:
        data = json.loads(json_string, parse_constant=_reject_nonstandard_number)
    except json.JSONDecodeError as exc:
        raise InvalidArtifactError(f"Invalid artifact JSON: {exc.msg}", line=exc.lineno, column=exc.colno) from exc
    if not isinstance(data, dict):
        raise InvalidArtifactError("Artifact JSON root must be an object")
    return dict_to_artifact(data)


def _parse_header(content: str) -> tuple[str, int, str | None]:
    """Strip the exact three-line metadata header from a saved artifact.

    Args:
        content: Complete file contents.

    Returns:
        JSON body, header line count, and header version when present.

    Raises:
        InvalidArtifactError: If a comment header is malformed.
    """
    lines = content.splitlines(keepends=True)
    if not lines or not lines[0].startswith("#"):
        return content, 0, None
    if lines[0].rstrip("\r\n") != _HEADER:
        raise InvalidArtifactError("Unknown artifact header", line=1)
    if len(lines) < 4 or not lines[1].startswith(_GENERATED) or not lines[2].startswith(_VERSION):
        raise InvalidArtifactError("Incomplete artifact metadata header", line=min(len(lines), 3))
    generated = lines[1][len(_GENERATED):].strip()
    try:
        timestamp = datetime.fromisoformat(generated.replace("Z", "+00:00"))
    except ValueError as exc:
        raise InvalidArtifactError("Invalid generated timestamp", line=2) from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise InvalidArtifactError("Generated timestamp needs a UTC offset", line=2)
    version = lines[2][len(_VERSION):].strip()
    if not version:
        raise InvalidArtifactError("Missing artifact header version", line=3)
    return "".join(lines[3:]), 3, version


def save_artifact_to_file(artifact: AutomationArtifact, filepath: str) -> str:
    """Atomically write a validated artifact with the requested comment header.

    Parent directories are created if needed. The temporary file is replaced
    atomically and removed on errors. Its default permissions restrict access.

    Args:
        artifact: Artifact to persist.
        filepath: Destination JSON file path.

    Returns:
        Destination path as supplied.

    Raises:
        SchemaValidationError: If the artifact is invalid.
        OSError: If directory creation or writing fails.
    """
    body = to_json(artifact)
    path = Path(filepath)
    if not path.name:
        raise ValueError("Artifact filepath must name a file")
    header = "\n".join((_HEADER, f"{_GENERATED}{datetime.now(timezone.utc).isoformat()}", f"{_VERSION}{artifact.version}"))
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False) as handle:
            temporary = handle.name
            handle.write(header + "\n" + body + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and os.path.exists(temporary):
            os.unlink(temporary)
    LOGGER.info("artifact_saved", extra={"event": "artifact_saved", "artifact_id": artifact.id})
    return filepath


def load_artifact_from_file(filepath: str) -> AutomationArtifact:
    """Load an artifact from disk, including optional metadata comments.

    Args:
        filepath: Saved artifact path.

    Returns:
        Reconstructed, validated artifact.

    Raises:
        FileNotFoundError: If the path does not exist.
        InvalidArtifactError: If metadata or JSON is malformed, with file line.
        SchemaValidationError: If the artifact data or header version is invalid.
    """
    path = Path(filepath)
    try:
        content = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Artifact file not found: {path}") from exc
    header_lines = 0
    try:
        body, header_lines, header_version = _parse_header(content)
        artifact = from_json(body)
    except InvalidArtifactError as exc:
        if exc.line is not None and header_lines:
            raise InvalidArtifactError(exc.message, line=exc.line + header_lines, column=exc.column) from exc
        raise
    if header_version is not None and header_version != artifact.version:
        raise SchemaValidationError("Artifact header version does not match the JSON version")
    LOGGER.info("artifact_loaded_from_file", extra={"event": "artifact_loaded_from_file", "artifact_id": artifact.id})
    return artifact
