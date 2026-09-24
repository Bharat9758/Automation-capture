# AutomationCapture Project Report

## 1. Summary

Phases 1 through 4 establish the project structure, a bounded Claude-guided discovery loop, a reusable artifact schema, and a recorder for successful traces.

## 2. Scope

The agent observes one Selenium browser session and executes five action types on configured domains. The recorder converts successful traces to artifacts describing replay steps, inputs, outputs, and checkpoints. Persistence, replay, and escalation remain future phases.

## 3. Architecture

`Observer` collects browser signals; `llm_agent` sends the screenshot and state to Claude; `Actor` validates locators and domains before executing actions. The recorder maps completed traces into Pydantic dataclasses and validates their JSON shape. Module loggers write JSON events.

## 4. Implementation

The loop enforces step and elapsed-time limits, detects three consecutive unchanged observations, validates model JSON, and returns action records without typed values. The recorder replaces typed literals with parameter placeholders, infers fields from successful reads, notes failed attempts, and adds locator robustness notes. Artifacts validate field types, semantic versions, dates, URLs, step ordering, and success rates.

## 5. Configuration

Copy `config.example.env` to `.env`. Choose an active `LLM_MODEL`, provide an API key to the caller, and set `ALLOWED_DOMAINS` for the intended sites. Recorder settings control the input scan limit, step timeout, initial version, and creator label. The Anthropic SDK was upgraded from the Phase 1 pin because that old release has no Messages API.

## 6. Verification

Run `pytest -q`. Agent tests cover observation, domain restrictions, XPath escaping, typed-value omission, completion, dead-end, and step limits. Schema tests cover round trips and invalid fields. Recorder tests cover parameter and output inference, robustness notes, trace validation, and agent-loop integration. No live Claude request or real browser integration is exercised yet.

## 7. Risks and Next Steps

Claude's completion claim is not independently verified. A fallback URL checkpoint may be weaker than a page-specific success condition; output checkpoints currently check visibility rather than the exact extracted value. Artifact IDs cannot be checked for uniqueness across a repository until persistence exists. Browser integration, artifact persistence, independent goal checks, and more detailed safety controls remain to be implemented.
