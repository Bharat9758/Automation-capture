# AutomationCapture Project Report

## 1. Summary

Phases 1 through 7 establish the project structure, a bounded Claude-guided discovery loop, an artifact schema, recording, JSON persistence, locator resolution, and deterministic replay without LLM calls.

## 2. Scope

The agent observes one Selenium browser session and executes five action types on configured domains. The recorder converts successful traces to artifacts describing replay steps, inputs, outputs, and checkpoints. Artifacts can be saved to and loaded from disk. The replay engine executes those artifacts in Selenium with ordered locator fallbacks and explicit success verification. Error classification and escalation remain future phases.

## 3. Architecture

`Observer` collects browser signals; `llm_agent` sends the screenshot and state to Claude; `Actor` validates locators and domains before executing actions. The recorder maps completed traces into Pydantic dataclasses. The serializer validates JSON Schema and cross-field rules, then writes comment-prefixed JSON files atomically. `LocatorResolver` finds visible candidates by CSS, XPath, ID, direct text, or aria-label. `replay_artifact` validates parameters, navigates allowed domains, executes actions, checks the final checkpoint, and extracts typed outputs. Module loggers write JSON events.

## 4. Implementation

The loop enforces step and elapsed-time limits, detects three consecutive unchanged observations, validates model JSON, and returns action records without typed values. The recorder replaces typed literals with parameter placeholders, infers fields from successful reads, notes failed attempts, and adds locator robustness notes. The serializer provides plain JSON conversion and file round trips with field-specific errors. The resolver tries nested fallback locators in order, waits for visibility, and retries stale references. Replay runs the saved actions with no model call and returns structured results with browser evidence on hard failure.

## 5. Configuration

Copy `config.example.env` to `.env`. Choose an active `LLM_MODEL`, provide an API key to the discovery caller, and set `ALLOWED_DOMAINS` for discovery and replay navigation. Recorder settings control the input scan limit, step timeout, initial version, and creator label. Replay accepts a per-call maximum wait in milliseconds. The Anthropic SDK was upgraded from the Phase 1 pin because that old release has no Messages API.

## 6. Verification

Run `pytest -q`. Agent tests cover observation, domain restrictions, XPath escaping, typed-value omission, completion, dead-end, and step limits. Schema tests cover round trips and invalid fields. Recorder tests cover inference and integration. Serializer tests cover JSON and file round trips, metadata headers, invalid JSON, missing nested fields, and failed writes. Locator tests cover all strategies, fallbacks, visibility waits, stale recovery, and failure reports. Replay tests cover validation, substitution, actions, checkpoint conditions, output conversion, navigation restrictions, and evidence. No live Claude request or real browser integration is exercised yet.

## 7. Risks and Next Steps

Claude's discovery completion claim is not independently verified. Replay checks the recorded success checkpoint, which can be weaker than the intended business goal if discovery recorded only a URL or visible element. Descriptive step outcomes do not assert page state; use explicit condition directives for intermediate assertions. Replay failure evidence includes raw DOM and screenshots and may contain personal data. Basic replay errors are hard failures until Phase 8 classification. Disk serialization does not provide a shared artifact index or global ID uniqueness. The metadata header means saved files require the provided loader rather than an ordinary JSON parser. Text locator case matching covers ASCII letters. Real browser integration, business error handling, and detailed safety controls remain future work.
