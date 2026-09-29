"""Risk scoring, escalation, approval, and policy configuration tests."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from src.artifact.schema import ActionStep, AutomationArtifact, Locator
from src.safety.risk_classifier import (
    RiskApproval, RiskLevel, check_repeated_failures, classify_action_risk,
    get_element_risk, get_reasoning_risk, get_recommended_action,
    get_risk_score, is_human_supplied_action,
)


@pytest.fixture
def artifact() -> AutomationArtifact:
    """Create a valid, minimal workflow for independent scoring.

    Returns:
        Artifact with safe initial navigation and click.
    """
    return AutomationArtifact.from_dict({
        "id": "risk-example", "name": "Search", "version": "1.0.0", "description": "Read search result",
        "created_at": "2026-09-24T00:00:00Z", "updated_at": "2026-09-24T00:00:00Z",
        "created_by": "test", "target_url": "https://banking.example.com/app", "inputs": [], "outputs": [],
        "steps": [{"step_number": 1, "action": "navigate", "locator": None, "value": "/app",
                   "reasoning": "Open page", "expected_outcome": "Page loads"},
                  {"step_number": 2, "action": "click", "locator": {"strategy": "id", "value": "submit",
                                                                     "robustness_notes": "Stable"},
                   "value": None, "reasoning": "Submit", "expected_outcome": "Done"}],
        "success_checkpoint": {"condition": "url_matches", "locator": None, "expected_value": "/app",
                               "error_message": "Wrong page"},
        "known_errors": {}, "discovery_run_id": "test-run", "success_rate": None,
    })


def _step(number: int, action: str = "click", locator: str = "submit", reasoning: str = "Submit") -> ActionStep:
    """Create one validated action at a selected sequence position.

    Args:
        number: One-based position.
        action: Artifact action name.
        locator: Reviewed selector value.
        reasoning: Value-free intent.

    Returns:
        Fully validated action.
    """
    return ActionStep(step_number=number, action=action,
                      locator=None if action == "navigate" else Locator(strategy="id", value=locator, robustness_notes="Test"),
                      value="/app" if action == "navigate" else "{input}" if action == "type" else None,
                      reasoning=reasoning, expected_outcome="Done")


@pytest.mark.parametrize(("action", "expected"), [("click", RiskLevel.SAFE), ("navigate", RiskLevel.SAFE),
                                                    ("wait", RiskLevel.SAFE), ("read_text", RiskLevel.SAFE),
                                                    ("checkpoint", RiskLevel.SAFE), ("type", RiskLevel.CAUTION)])
def test_action_baselines(artifact: AutomationArtifact, action: str, expected: RiskLevel) -> None:
    """Routine actions have documented base scores outside early steps."""
    assessment = classify_action_risk(_step(3, action), artifact, 3, [], [])
    assert assessment.risk_level == expected
    assert not assessment.escalation_threshold


def test_cumulative_keywords_are_value_free(artifact: AutomationArtifact) -> None:
    """Independent locator and reasoning signals accumulate to critical."""
    step = _step(3, locator="delete_account_confirm_button", reasoning="confirm and close account")
    assessment = classify_action_risk(step, artifact, 3, [], [])
    assert assessment.risk_level == RiskLevel.CRITICAL
    assert assessment.requires_approval and assessment.escalation_threshold
    assert "delete" in assessment.reasoning and "close" in assessment.reasoning
    assert "delete_account_confirm_button" not in assessment.reasoning
    assert get_risk_score("Delete and confirm", {"delete": 2, "confirm": 1}) == 3
    assert get_recommended_action(RiskLevel.CRITICAL).startswith("Escalate")


def test_locator_fallbacks_and_reasoning(artifact: AutomationArtifact) -> None:
    """All fallbacks count, but repeating the same keyword does not double count."""
    locator = Locator(strategy="id", value="submit", robustness_notes="Primary",
                      fallbacks=[Locator(strategy="css", value="button.transfer-final", robustness_notes="Fallback")])
    points, evidence = get_element_risk(locator)
    assert points == 3 and "locator keyword: transfer" in evidence
    assert get_reasoning_risk("an irreversible final step")[0] == 3


def test_context_sequence_and_position(artifact: AutomationArtifact) -> None:
    """Failures, recovery, repeated writes, and early steps change scores."""
    prior = [_step(3, action="type"), _step(4, action="click")]
    step = _step(5)
    assert check_repeated_failures(prior, [{"success": False}] * 3) == (2, "Repeated failed attempts")
    assessment = classify_action_risk(step, artifact, 5, prior,
                                      [{"success": False}] * 3 + [{"recovered": True}])
    assert assessment.risk_level == RiskLevel.CRITICAL
    assert "multiple consecutive writes" in assessment.evidence
    assert "after error recovery" in assessment.evidence
    assert classify_action_risk(_step(1, "navigate"), artifact, 1, [], []).risk_level == RiskLevel.CAUTION
    assert classify_action_risk(_step(50, "type"), artifact, 50, [], []).risk_level == RiskLevel.SAFE


def test_irreversible_operations_stay_escalated_late_in_workflow(artifact: AutomationArtifact) -> None:
    """Sequence confidence cannot erase an intrinsic critical write."""
    assessment = classify_action_risk(_step(50, locator="close_account", reasoning="Submit"), artifact, 50, [], [])
    assert assessment.risk_level == RiskLevel.CRITICAL
    assert assessment.requires_approval and assessment.escalation_threshold


def test_multiple_risky_writes_trigger_escalation(artifact: AutomationArtifact) -> None:
    """A later write following two risky writes needs human attention."""
    prior = [_step(3, reasoning="Delete record"), _step(4, reasoning="Transfer funds")]
    assessment = classify_action_risk(_step(5), artifact, 5, prior, [])
    assert assessment.risk_level >= RiskLevel.RISKY
    assert assessment.escalation_threshold


def test_human_provenance_reduces_score_but_not_intrinsic_risk(artifact: AutomationArtifact) -> None:
    """An audit record lowers confidence score but is not approval to execute."""
    step = _step(3, reasoning="Delete record")
    payload = artifact.to_dict()
    payload["human_interventions"] = [{"at_step": 3, "reason": "Operator helped",
                                       "human_actions": [{"action": "click", "locator": {"strategy": "id", "value": "submit"}}],
                                       "timestamp": "2026-09-24T12:00:00Z", "operator_id": "reviewer"}]
    payload["updated_at"] = "2026-09-24T12:00:00Z"
    reviewed = AutomationArtifact.from_dict(payload)
    assert is_human_supplied_action(step, reviewed)
    assessment = classify_action_risk(step, reviewed, 3, [], [])
    assert assessment.risk_level >= RiskLevel.RISKY and assessment.escalation_threshold
    assert "human-supplied" in assessment.reasoning


def test_approval_record_requires_identity_method_and_timezone() -> None:
    """Only a well-formed, attributable decision is a risk approval record."""
    now = datetime.now(timezone.utc).isoformat()
    approval = RiskApproval(step_number=3, risk_level=RiskLevel.CRITICAL, approved_by="operator-1",
                            approved_at=now, approval_method="human_interactive")
    assert approval.approved_by == "operator-1"
    with pytest.raises(ValueError, match="timezone"):
        RiskApproval(step_number=3, risk_level=RiskLevel.CRITICAL, approved_by="operator-1",
                     approved_at="2026-09-24T12:00:00", approval_method="human_interactive")
    with pytest.raises(ValueError, match="operator"):
        RiskApproval(step_number=3, risk_level=RiskLevel.CRITICAL, approved_by="",
                     approved_at=now, approval_method="human_interactive")


def test_keyword_configuration_is_validated(artifact: AutomationArtifact, monkeypatch: pytest.MonkeyPatch) -> None:
    """Policy overrides work without code changes and malformed JSON fails closed."""
    monkeypatch.setenv("RISK_REASONING_KEYWORDS", '{"commit": 3}')
    assessment = classify_action_risk(_step(3, reasoning="Commit changes"), artifact, 3, [], [])
    assert assessment.risk_level == RiskLevel.CRITICAL
    monkeypatch.setenv("RISK_REASONING_KEYWORDS", '{"commit": -1}')
    with pytest.raises(ValueError, match="RISK_REASONING_KEYWORDS"):
        classify_action_risk(_step(3), artifact, 3, [], [])
