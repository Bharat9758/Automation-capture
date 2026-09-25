# AutomationCapture

AutomationCapture discovers browser workflows with a Claude-guided Selenium agent and records reusable artifacts for later deterministic replay. Phases 2 through 10 add page observation, browser actions, a bounded discovery loop, a versioned artifact schema, recording, JSON file persistence, robust locator resolution, deterministic replay, runtime outcome classification, checkpoint verification, and stuck state detection. The human handoff and Flask target application will be implemented in later phases.

## Development setup

Use Python 3.11 or newer:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp config.example.env .env
pytest -q
```

Set a real `ANTHROPIC_API_KEY` in `.env` before making live Claude requests. The calling program must create a Selenium WebDriver and pass its API key to `run_goal_driven_loop(..., api_key=...)`. `ALLOWED_DOMAINS` controls the exact hosts (and optional ports) the agent can visit; navigation is denied when this list is empty. `LLM_MODEL`, request timeout, token limit, observation limits, and action wait timeout are configurable in `.env`.

The loop returns `success`, `steps`, `final_state` (base64 PNG), `logs`, and `error`. On success it also returns `artifact` as a JSON-ready dictionary or `artifact_error` if the trace cannot produce a replayable artifact. A success result means Claude reported `goal_met=true` based on the latest observation; it is not an independent verification of the outcome. Typed field values are excluded from returned steps and JSON logs. The accessibility signal is a cross-browser DOM-derived semantic snapshot, not a browser-native accessibility tree.

## Artifact format

`src.artifact.schema` defines Pydantic dataclasses for locators, steps, checkpoints, inputs, outputs, and the complete artifact. Use `AutomationArtifact.from_dict(data)` to parse a JSON-shaped dictionary, `artifact.to_dict()` to serialize it, and `artifact.validate()` to check an existing instance. `ARTIFACT_JSON_SCHEMA` exposes its Draft 2020-12 JSON Schema. Invalid input to `from_dict` raises a validation exception; `validate()` returns `False` for an invalid instance. The schema requires timezone-aware ISO timestamps, Semantic Versioning 2.0.0, consecutively numbered steps, and a success rate between 0 and 1 when present.

`record_discovery_run(steps, agent_logs, goal, target_url)` returns a validated `AutomationArtifact` for a successful run with replayable actions. It turns typed literals into named placeholders, infers output types from successful `read_text` actions, excludes failed attempts from replay steps, and records those attempts in `known_errors`. The scan limit, default step timeout, initial artifact version, and creator label are configurable in `.env`. A run that only reports `done`, or an output goal with no successful read, cannot yield a complete artifact. The caller chooses when and where to save the returned artifact.

## JSON persistence

`src.artifact.serializer.to_json(artifact)` returns standard, two-space-indented JSON. `from_json(text)`, `artifact_to_dict(artifact)`, `dict_to_artifact(data)`, and `validate_artifact_schema(data)` validate and restore all nested fields. Malformed JSON raises `InvalidArtifactError`; a missing or invalid artifact field raises `SchemaValidationError`.

`save_artifact_to_file(artifact, "artifacts/example.json")` creates parent directories and atomically replaces the destination. Saved files have three requested `#` metadata lines before the JSON body, so the entire saved file is **comment-prefixed JSON**, not a plain JSON document. Use `load_artifact_from_file(path)` to read it; the loader also accepts plain JSON files. Use `to_json()` when a consumer requires standard JSON. The loader checks the header version against the artifact and reports JSON parse errors using file line numbers.

To save an agent-loop result, pass its `artifact` dictionary through `dict_to_artifact(result["artifact"])`, then call `save_artifact_to_file(...)` with your chosen path.

## Locator resolution

`LocatorResolver(wait_timeout=10).resolve(driver, locator)` returns the first visible Selenium element found by the primary locator or an ordered fallback. It accepts the Phase 3 `Locator` dataclass or a JSON-shaped locator dictionary, including nested fallbacks. Supported strategies are `css`, `xpath`, `id`, case-insensitive direct `text` (ASCII case mapping), and exact `aria_label`. A hidden element gets a visibility wait; a stale reference gets up to three fresh lookup attempts before the next fallback. The timeout applies to each visibility check, so several hidden candidates can extend the total time. An exhausted search raises `ElementNotFoundError` with every attempted locator and its reason. The replay engine uses this resolver before element actions and output extraction.

For time-limited checkpoint lookups, pass `timeout=<seconds>` to `resolve` to bound the total visibility budget across fallbacks. `LOCATOR_POLL_INTERVAL_SECONDS` controls visibility polling and is capped at the remaining wait.

## Deterministic replay

Load an artifact and run it with an existing Selenium driver; the replay engine makes no LLM calls:

```python
from src.artifact.serializer import load_artifact_from_file
from src.replay.replay_engine import replay_artifact

artifact = load_artifact_from_file("artifacts/lookup_member.json")
result = replay_artifact(driver, artifact, {"member_id": "12345"})
if result.success:
    print(result.outputs)
else:
    print(result.error, result.step_failed)
```

Configure `ALLOWED_DOMAINS` to include the artifact target and all navigation destinations. Replay validates input types before navigating, substitutes named placeholders without changing the artifact, waits for document readiness, resolves every element through fallbacks, and verifies the final success checkpoint before extracting typed outputs. Missing or invalid inputs raise `src.replay.replay_engine.ValidationError`. Browser, action, checkpoint, and extraction failures return `ReplayResult(status="hard_failure")` with a screenshot, DOM, URL, and title when available. Failure evidence can contain page content and input data; handle it as sensitive data.

Step `expected_outcome` values can use `url_matches:<regex>`, `text_contains:<text>`, `text_changed:<css>`, `element_visible:<css>`, or `element_count:<css>=<count>` to assert an observable condition. Existing descriptive recorder text is treated as a description; the final artifact checkpoint is always enforced. Checkpoint actions accept a locator as an element-visible check, or an explicit condition directive in their `value`. `max_wait_ms` and each step's `timeout_ms` bound individual waits; nested locator fallbacks can increase total elapsed time.

## Runtime outcomes and recovery

Add a detection rule in `known_errors` to classify an observed page state. For example:

```json
{
  "member_not_found": {
    "detection": {
      "type": "text_contains",
      "locator": {"strategy": "css", "value": ".error-message", "robustness_notes": "Visible search feedback"},
      "expected_text": "No such member"
    },
    "classification": "expected_business_outcome",
    "business_outcome": "member_not_found"
  }
}
```

The supported detection types are `text_contains`, `element_visible`, `url_matches` (substring match), and `element_count` (integer `expected_count`). A matched business result returns `success=False`, `status="business_outcome"`, its `business_outcome`, and no system error. A matched `hard_failure` stops replay. A `recoverable_condition` may define a `recovery_action` with `action` set to `click`, `type`, or `navigate`; navigation uses `ALLOWED_DOMAINS`. Recovery and retries are bounded by `REPLAY_MAX_RECOVERY_RETRIES` (default 1). Stale elements and timeouts without a known rule retry only read, wait, type, checkpoint, or navigation actions. Clicks do not retry automatically because their effects might have completed before the exception. `ERROR_DETECTION_TIMEOUT_SECONDS` bounds each visible-element detection attempt. A matched business result also takes precedence over a passing but weak success checkpoint. Older recorder entries containing only `action` and `error_type` are diagnostics and cannot match a business result without a detection rule.

## Checkpoint verification

`CheckpointVerifier().verify(driver, checkpoint, resolver, timeout=10)` waits for a saved checkpoint before the replay engine extracts any outputs. Schema checkpoints support `element_visible`, `element_exists` (a hidden DOM node counts), `text_contains` (visible element or body text), `url_matches`, and `element_count` (exact number, with locator fallbacks). A timeout or browser error raises `CheckpointVerificationError` with the expected condition, last observed state, step number, and evidence. Replay returns a hard failure with that evidence unless a configured known business result is visible; output extraction is skipped.

URL expectations use substring matching by default, with exact and regex choices available as `exact:<url>` and `regex:<pattern>`. Existing anchored regex expectations (`^...$`) continue to work. Use `partial:<fragment>` to explicitly request substring matching. `CHECKPOINT_URL_MATCH_MODE` sets the default (`auto`, `exact`, `partial`, or `regex`), and `CHECKPOINT_CASE_SENSITIVE` controls text comparisons. `CHECKPOINT_POLL_INTERVAL_SECONDS` controls how often checks repeat; `CHECKPOINT_PAGE_TEXT_CHARS` limits the page text snippet captured on failure. Checkpoint evidence can contain sensitive page content.

## Stuck state detection

`src.escalation.stuck_detector` fingerprints URL, title, visible text, and visible control count to detect repeated page states. Replay checks an upcoming click for risky words (such as `transfer` or `delete`) and multiple visible target matches before clicking. Three unchanged observations after meaningful actions also pause replay. Call `replay_artifact(..., request_human_help=True)` to pause before navigation.

A pause returns `ReplayResult(status="recoverable_error", success=False, stuck_state=...)` with the reason, one-based pending step, recommendation, and browser snapshot. Hard failures keep `status="hard_failure"` and also carry `stuck_state` for escalation. No human notification or approval handling occurs yet; Phase 11 will consume this state. Business outcomes keep their own status. The signature truncation, repeat threshold, history length, risky keywords, and number of visible elements in evidence are configured with `STUCK_*` variables in `config.example.env`. Evidence contains full DOM and screenshots and may contain sensitive data.
