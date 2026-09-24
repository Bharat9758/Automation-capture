# AutomationCapture Project Report

## 1. Summary

Phases 1 through 6 establish the project structure, a bounded Claude-guided discovery loop, an artifact schema, recording, JSON persistence, and replay locator resolution.

## 2. Scope

The agent observes one Selenium browser session and executes five action types on configured domains. The recorder converts successful traces to artifacts describing replay steps, inputs, outputs, and checkpoints. Artifacts can be saved to and loaded from disk. The locator resolver finds visible elements through ordered fallbacks. The replay engine and escalation remain future phases.

## 3. Architecture

`Observer` collects browser signals; `llm_agent` sends the screenshot and state to Claude; `Actor` validates locators and domains before executing actions. The recorder maps completed traces into Pydantic dataclasses. The serializer validates JSON Schema and cross-field rules, then writes comment-prefixed JSON files atomically. `LocatorResolver` looks up candidates by CSS, XPath, ID, exact direct text, or aria-label, and waits for visibility. Module loggers write JSON events.

## 4. Implementation

The loop enforces step and elapsed-time limits, detects three consecutive unchanged observations, validates model JSON, and returns action records without typed values. The recorder replaces typed literals with parameter placeholders, infers fields from successful reads, notes failed attempts, and adds locator robustness notes. The serializer provides plain JSON conversion and file round trips with field-specific errors. The resolver tries nested fallback locators in order, waits for visibility, retries stale references, and reports attempted selectors and failure reasons on exhaustion.

## 5. Configuration

Copy `config.example.env` to `.env`. Choose an active `LLM_MODEL`, provide an API key to the caller, and set `ALLOWED_DOMAINS` for the intended sites. Recorder settings control the input scan limit, step timeout, initial version, and creator label. The Anthropic SDK was upgraded from the Phase 1 pin because that old release has no Messages API.

## 6. Verification

Run `pytest -q`. Agent tests cover observation, domain restrictions, XPath escaping, typed-value omission, completion, dead-end, and step limits. Schema tests cover round trips and invalid fields. Recorder tests cover inference and integration. Serializer tests cover JSON and file round trips, metadata headers, invalid JSON, missing nested fields, and failed writes. Locator tests cover all strategies, fallbacks, visibility waits, stale recovery, and failure reports. No live Claude request or real browser integration is exercised yet.

## 7. Risks and Next Steps

Claude's completion claim is not independently verified. A fallback URL checkpoint may be weaker than a page-specific success condition; output checkpoints currently check visibility rather than the exact extracted value. Disk serialization does not provide a shared artifact index or global ID uniqueness. The three-line metadata header means saved files require the provided loader rather than an ordinary JSON parser. Text locator case matching covers ASCII letters; page text with non-ASCII casing can require CSS, XPath, ID, or aria-label fallbacks. Browser integration, independent goal checks, and more detailed safety controls remain to be implemented.
