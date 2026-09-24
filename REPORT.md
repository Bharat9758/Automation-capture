# AutomationCapture Project Report

## 1. Summary

Phases 1 and 2 establish the project structure and a bounded Claude-guided discovery loop.

## 2. Scope

The agent observes one Selenium browser session and executes five action types on configured domains. Replay and escalation remain future phases.

## 3. Architecture

`Observer` collects browser signals; `llm_agent` sends the screenshot and state to Claude; `Actor` validates locators and domains before executing actions. Module loggers write JSON events.

## 4. Implementation

The loop enforces step and elapsed-time limits, detects three consecutive unchanged observations, validates model JSON, and returns action records without typed values.

## 5. Configuration

Copy `config.example.env` to `.env`. Choose an active `LLM_MODEL`, provide an API key to the caller, and set `ALLOWED_DOMAINS` for the intended sites. The Anthropic SDK was upgraded from the Phase 1 pin because that old release has no Messages API.

## 6. Verification

Run `pytest -q`. Mock-based tests check observation serialization, domain restrictions, XPath escaping, typed-value omission, completion, dead-end, invalid inputs, and step limits. No live Claude request or real browser integration is exercised yet.

## 7. Risks and Next Steps

Claude's completion claim is not independently verified. The semantic snapshot is derived from the DOM. Browser integration, artifact persistence, independent goal checks, and more detailed safety controls remain to be implemented.
