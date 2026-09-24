"""Round-trip and failure tests for artifact JSON persistence."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.artifact.recorder import record_discovery_run
from src.artifact.schema import AutomationArtifact, Locator
from src.artifact.serializer import (
    InvalidArtifactError,
    SchemaValidationError,
    artifact_to_dict,
    dict_to_artifact,
    from_json,
    load_artifact_from_file,
    save_artifact_to_file,
    to_json,
    validate_artifact_schema,
)


@pytest.fixture
def artifact() -> AutomationArtifact:
    """Create an artifact from the Phase 4 recorder.

    Returns:
        Valid artifact with nested locators and a parameter.
    """
    steps = [
        {"action": "type", "selector": "#member_id", "locator_type": "css",
         "result": {"success": True}},
        {"action": "read_text", "selector": "#balance", "locator_type": "css",
         "result": {"success": True, "observed_type": "number"}},
    ]
    logs = [{"event": "discovery_started", "run_id": "run-serializer"},
            {"event": "loop_finished", "success": True, "final_url": "http://localhost:5000/account"}]
    return record_discovery_run(steps, logs, "Read balance", "http://localhost:5000/")


def test_json_round_trip_preserves_nested_objects(artifact: AutomationArtifact) -> None:
    """Plain JSON round trips through nested Pydantic dataclasses.

    Args:
        artifact: Valid artifact fixture.
    """
    serialized = to_json(artifact)
    assert serialized.startswith('{\n  "id":')
    assert json.loads(serialized) == artifact_to_dict(artifact)
    restored = from_json(serialized)
    assert restored.to_dict() == artifact.to_dict()
    assert isinstance(restored.steps[0].locator, Locator)
    assert restored.steps[0].locator.fallbacks[0].strategy == "id"


def test_save_and_load_with_metadata_header(tmp_path: Path, artifact: AutomationArtifact) -> None:
    """Persistence creates parents, writes header, and reconstructs the artifact.

    Args:
        tmp_path: Isolated test directory.
        artifact: Valid artifact fixture.
    """
    destination = tmp_path / "artifacts" / "lookup_member_savings.json"
    returned = save_artifact_to_file(artifact, str(destination))
    assert returned == str(destination)
    lines = destination.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "# AutomationCapture Artifact"
    assert lines[1].startswith("# Generated: ")
    assert lines[2] == f"# Version: {artifact.version}"
    assert json.loads("\n".join(lines[3:])) == artifact.to_dict()
    assert load_artifact_from_file(str(destination)).to_dict() == artifact.to_dict()


def test_plain_json_file_is_also_loadable(tmp_path: Path, artifact: AutomationArtifact) -> None:
    """A headerless JSON artifact remains valid input.

    Args:
        tmp_path: Isolated test directory.
        artifact: Valid artifact fixture.
    """
    destination = tmp_path / "plain.json"
    destination.write_text(to_json(artifact), encoding="utf-8")
    assert load_artifact_from_file(str(destination)).id == artifact.id


def test_invalid_json_reports_body_and_file_lines(tmp_path: Path) -> None:
    """Malformed JSON is distinct from schema errors and uses file line numbers.

    Args:
        tmp_path: Isolated test directory.
    """
    with pytest.raises(InvalidArtifactError) as direct:
        from_json('{\n  "id":,\n}')
    assert direct.value.line == 2
    destination = tmp_path / "invalid.json"
    destination.write_text(
        "# AutomationCapture Artifact\n# Generated: 2026-09-24T10:30:00Z\n# Version: 1.0.0\n{\n  \"id\":,\n}",
        encoding="utf-8",
    )
    with pytest.raises(InvalidArtifactError) as saved:
        load_artifact_from_file(str(destination))
    assert saved.value.line == 5
    assert "line 5" in str(saved.value)


def test_missing_fields_and_wrong_nested_types_are_reported(artifact: AutomationArtifact) -> None:
    """Schema errors identify missing fields and nested property paths.

    Args:
        artifact: Valid artifact fixture.
    """
    data = artifact.to_dict()
    del data["id"]
    del data["steps"][0]["locator"]["strategy"]
    valid, errors = validate_artifact_schema(data)
    assert valid is False
    assert any("$.id: required" in error for error in errors)
    assert any("$.steps[0].locator.strategy" in error for error in errors)
    with pytest.raises(SchemaValidationError, match="id"):
        dict_to_artifact(data)
    with pytest.raises(SchemaValidationError, match="id"):
        from_json(json.dumps(data))
    data = artifact.to_dict()
    data["inputs"][0]["required"] = "yes"
    assert any("$.inputs[0].required" in message for message in validate_artifact_schema(data)[1])


def test_cross_field_validation_rejects_bad_step(artifact: AutomationArtifact) -> None:
    """A structurally valid click still needs a locator.

    Args:
        artifact: Valid artifact fixture.
    """
    data = artifact.to_dict()
    data["steps"][0]["action"] = "click"
    data["steps"][0]["locator"] = None
    assert validate_artifact_schema(data)[0] is False
    with pytest.raises(SchemaValidationError):
        dict_to_artifact(data)


def test_file_errors_and_header_mismatch(tmp_path: Path, artifact: AutomationArtifact) -> None:
    """Missing files, unknown comments, and inconsistent headers fail clearly.

    Args:
        tmp_path: Isolated test directory.
        artifact: Valid artifact fixture.
    """
    destination = tmp_path / "artifact.json"
    with pytest.raises(FileNotFoundError, match="Artifact file not found"):
        load_artifact_from_file(str(destination))
    destination.write_text("# Unknown\n{}", encoding="utf-8")
    with pytest.raises(InvalidArtifactError, match="Unknown artifact header"):
        load_artifact_from_file(str(destination))
    save_artifact_to_file(artifact, str(destination))
    content = destination.read_text(encoding="utf-8").replace(
        f"# Version: {artifact.version}", "# Version: 9.9.9", 1
    )
    destination.write_text(content, encoding="utf-8")
    with pytest.raises(SchemaValidationError, match="header version"):
        load_artifact_from_file(str(destination))


def test_invalid_artifact_does_not_overwrite_existing_file(tmp_path: Path, artifact: AutomationArtifact) -> None:
    """Validation occurs before any destination file is replaced.

    Args:
        tmp_path: Isolated test directory.
        artifact: Valid artifact fixture.
    """
    destination = tmp_path / "artifact.json"
    save_artifact_to_file(artifact, str(destination))
    original = destination.read_bytes()
    object.__setattr__(artifact, "version", "invalid")
    with pytest.raises(SchemaValidationError):
        save_artifact_to_file(artifact, str(destination))
    assert destination.read_bytes() == original


def test_nonstandard_json_numbers_are_rejected() -> None:
    """Reject NaN rather than silently accepting nonstandard JSON."""
    with pytest.raises(InvalidArtifactError, match="Nonstandard JSON number"):
        from_json('{"success_rate": NaN}')
