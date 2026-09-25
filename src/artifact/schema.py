"""Validated, JSON-serializable automation artifact definitions."""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ConfigDict, Field, TypeAdapter, field_validator, model_validator
from pydantic.dataclasses import dataclass

from src.logging import get_logger


LOGGER = get_logger(__name__)
_CONFIG = ConfigDict(extra="forbid", validate_assignment=True)
_IDENTIFIER = r"(?:0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)"
_SEMVER = re.compile(
    rf"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)"
    rf"(?:-{_IDENTIFIER}(?:\.{_IDENTIFIER})*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


def _timestamp(value: str) -> datetime:
    """Parse an ISO 8601 timestamp with an explicit UTC offset.

    Args:
        value: Timestamp text, optionally ending in Z.

    Returns:
        A timezone-aware datetime.

    Raises:
        ValueError: If the timestamp is invalid or lacks an offset.
    """
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Timestamp must be ISO 8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Timestamp must include a UTC offset")
    return parsed


@dataclass(config=_CONFIG, kw_only=True)
class Locator:
    """Primary element locator and ordered fallback locators."""

    strategy: Literal["css", "xpath", "id", "text", "aria_label"]
    value: str = Field(min_length=1)
    fallbacks: list[Locator] | None = None
    robustness_notes: str = Field(min_length=1)


@dataclass(config=_CONFIG, kw_only=True)
class ActionStep:
    """One deterministic action in a discovered workflow."""

    step_number: int = Field(gt=0, strict=True)
    action: Literal["click", "type", "navigate", "read_text", "wait", "checkpoint"]
    locator: Locator | None
    value: str | None
    reasoning: str = Field(min_length=1)
    expected_outcome: str = Field(min_length=1)
    timeout_ms: int | None = Field(default=10000, gt=0, strict=True)

    @model_validator(mode="after")
    def check_action_arguments(self) -> ActionStep:
        """Ensure actions contain the information needed for replay.

        Returns:
            This validated step.

        Raises:
            ValueError: If the selected action lacks required fields.
        """
        if self.action in {"click", "type", "read_text", "wait"} and self.locator is None:
            raise ValueError(f"{self.action} requires a locator")
        if self.action in {"type", "navigate"} and self.value is None:
            raise ValueError(f"{self.action} requires a value")
        return self


@dataclass(config=_CONFIG, kw_only=True)
class Checkpoint:
    """Expected browser condition with a useful failure message."""

    condition: Literal["element_visible", "element_exists", "text_contains", "url_matches", "element_count"]
    locator: Locator | None
    expected_value: str | None
    error_message: str = Field(min_length=1)

    @model_validator(mode="after")
    def check_condition_arguments(self) -> Checkpoint:
        """Check fields required to evaluate this condition.

        Returns:
            This validated checkpoint.

        Raises:
            ValueError: If the condition cannot be evaluated.
        """
        if self.condition in {"element_visible", "element_exists", "element_count"} and self.locator is None:
            raise ValueError(f"{self.condition} requires a locator")
        if self.condition in {"text_contains", "url_matches", "element_count"} and self.expected_value is None:
            raise ValueError(f"{self.condition} requires an expected value")
        if self.condition in {"text_contains", "url_matches"} and not self.expected_value:
            raise ValueError(f"{self.condition} requires a nonempty expected value")
        if self.condition == "element_count":
            try:
                if not self.expected_value.isdecimal() or int(self.expected_value) < 0:
                    raise ValueError("element_count must be nonnegative")
            except (TypeError, ValueError) as exc:
                raise ValueError("element_count must be a nonnegative integer") from exc
        return self


@dataclass(config=_CONFIG, kw_only=True)
class InputParameter:
    """Caller-supplied input for an automation capability."""

    name: str = Field(min_length=1)
    type: Literal["string", "number", "date"]
    description: str = Field(min_length=1)
    required: bool = Field(strict=True)
    example: str


@dataclass(config=_CONFIG, kw_only=True)
class OutputField:
    """Field to extract after the workflow completes."""

    name: str = Field(min_length=1)
    type: Literal["string", "number", "list", "object"]
    description: str = Field(min_length=1)
    extraction_locator: Locator | None


@dataclass(config=_CONFIG, kw_only=True)
class AutomationArtifact:
    """Reusable, versioned workflow with inputs, outputs, and checkpoints."""

    id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: str = Field(pattern=_SEMVER.pattern)
    description: str = Field(min_length=1)
    created_at: str = Field(json_schema_extra={"format": "date-time"})
    updated_at: str = Field(json_schema_extra={"format": "date-time"})
    created_by: str = Field(min_length=1)
    target_url: str = Field(json_schema_extra={"format": "uri"})
    inputs: list[InputParameter]
    outputs: list[OutputField]
    steps: list[ActionStep] = Field(min_length=1)
    success_checkpoint: Checkpoint
    known_errors: dict[str, dict[str, Any]]
    discovery_run_id: str = Field(min_length=1)
    success_rate: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("version")
    @classmethod
    def check_version(cls, value: str) -> str:
        """Require Semantic Versioning 2.0.0 syntax.

        Args:
            value: Artifact version.

        Returns:
            Valid version string.

        Raises:
            ValueError: If the version is invalid.
        """
        if not _SEMVER.fullmatch(value):
            raise ValueError("version must follow Semantic Versioning 2.0.0")
        return value

    @field_validator("created_at", "updated_at")
    @classmethod
    def check_timestamp(cls, value: str) -> str:
        """Validate a timestamp while preserving its serialized spelling.

        Args:
            value: ISO 8601 timestamp.

        Returns:
            Valid timestamp string.
        """
        _timestamp(value)
        return value

    @field_validator("target_url")
    @classmethod
    def check_target_url(cls, value: str) -> str:
        """Require a safe absolute HTTP(S) base URL.

        Args:
            value: Browser target URL.

        Returns:
            Valid URL.

        Raises:
            ValueError: If the URL is invalid or contains credentials.
        """
        try:
            url = urlsplit(value)
            if url.port is not None and not 1 <= url.port <= 65535:
                raise ValueError("Invalid port")
        except ValueError as exc:
            raise ValueError("target_url has an invalid port") from exc
        if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password or url.query or url.fragment:
            raise ValueError("target_url must be an HTTP(S) base URL without credentials, query, or fragment")
        return value

    @model_validator(mode="after")
    def check_consistency(self) -> AutomationArtifact:
        """Validate ordering and uniqueness inside the artifact.

        Returns:
            This validated artifact.

        Raises:
            ValueError: If dates, steps, or field names conflict.
        """
        if _timestamp(self.updated_at) < _timestamp(self.created_at):
            raise ValueError("updated_at cannot precede created_at")
        if [step.step_number for step in self.steps] != list(range(1, len(self.steps) + 1)):
            raise ValueError("steps must be numbered consecutively from 1")
        for fields in (self.inputs, self.outputs):
            names = [field.name for field in fields]
            if len(names) != len(set(names)):
                raise ValueError("input and output names must be unique within each list")
        return self

    def to_dict(self) -> dict[str, Any]:
        """Serialize the artifact and its nested dataclasses to JSON values.

        Returns:
            A plain dictionary suitable for json.dumps.
        """
        return _ADAPTER.dump_python(self, mode="json")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AutomationArtifact:
        """Validate a dictionary and construct an artifact.

        Args:
            data: Serialized artifact dictionary.

        Returns:
            A validated artifact.

        Raises:
            TypeError: If data is not a dictionary.
            jsonschema.ValidationError: If the JSON shape is invalid.
            pydantic.ValidationError: If field or cross-field rules fail.
        """
        if not isinstance(data, dict):
            raise TypeError("Artifact payload must be a dictionary")
        _JSON_VALIDATOR.validate(data)
        artifact = _ADAPTER.validate_python(data)
        LOGGER.info("artifact_loaded", extra={"event": "artifact_loaded", "artifact_id": artifact.id})
        return artifact

    def validate(self) -> bool:
        """Revalidate an artifact, including changes made since construction.

        Returns:
            True for a valid artifact; False otherwise.
        """
        try:
            payload = self.to_dict()
            _JSON_VALIDATOR.validate(payload)
            _ADAPTER.validate_python(payload)
        except Exception as exc:
            LOGGER.warning("artifact_invalid", extra={"event": "artifact_invalid", "error": type(exc).__name__})
            return False
        LOGGER.info("artifact_valid", extra={"event": "artifact_valid", "artifact_id": self.id})
        return True


_ADAPTER = TypeAdapter(AutomationArtifact)
ARTIFACT_JSON_SCHEMA: dict[str, Any] = _ADAPTER.json_schema()
Draft202012Validator.check_schema(ARTIFACT_JSON_SCHEMA)
_JSON_VALIDATOR = Draft202012Validator(ARTIFACT_JSON_SCHEMA, format_checker=FormatChecker())
