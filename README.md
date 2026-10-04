# AutomationCapture

## Metadata redaction (Phase 16)

`REDACTION_LEVEL=STRICT` is the default. `BASIC` masks obvious financial and identity fields; `STRICT` also masks names, member IDs, and pattern matches; `PARANOID` retains identifiers, timestamps, states, and metrics while masking other metadata. `NONE` is accepted only when `ALLOWLIST_MODE=development`. Optional `REDACTION_FIELDS` and `REDACTION_PATTERNS` are JSON lists. Malformed policies stop replay before browser actions.

Escalation requests and saved session reports use redacted metadata. Saved session outputs are masked, while the caller's live `ReplayResult.outputs` remains available for the requested workflow. JSON log records are filtered before emission. Screenshots and DOM remain unmodified in private escalation evidence and in the separate owner-only `RAW_EVIDENCE_DIRECTORY`; these files can contain personal data and must not be shared as redacted reports. Redacted action steps are audit copies and must never be replayed.

Field and regex masking cannot reliably identify every person or arbitrary ID in free prose. Review metadata before external sharing. Saved session records contain only redacted placeholders for nested escalation screenshots and DOM; the original raw evidence is kept in restricted escalation files.

## Action risk classification (Phase 15)

`src.safety.risk_classifier.classify_action_risk` assigns safe, caution, risky, or critical risk to each recorded replay step. Scores accumulate from action type, reviewed locator and reasoning keywords, prior failures and recovery, and sequence position. It does not inspect typed parameter values. The `RISK_*` settings in `config.example.env` configure keyword weights and sequence thresholds; malformed policy configuration stops replay. Every assessment is attached to `ReplayResult` and persisted in session metadata with a per-step risk level.

Replay checks the allowlist first. Caution actions log and continue; risky actions warn, and elevated risky cases pause for review. Critical actions pause before execution. A human operator can take control of the original session and call `SessionManager.approve_risk_action(session, RiskApproval(...))` while controlling it, followed by `approve_resume(session, resume_step=<paused step>)` and `give_control_to_automation(session)`. The resumed replay verifies that this explicit approval belongs to the paused step and persists it before executing the step. An operator can instead complete the step manually and approve resuming at the next step. A general resume signal without a step approval cannot execute a critical action. The allowlist still denies forbidden actions even when a risk approval exists.

## Replay allowlist (Phase 14)

Set `ALLOWLIST_PATH` to a reviewed JSON file for the target application. Copy `config/allowlist.example.json` and edit its exact domains, paths, allowed actions, and element locators. Replay refuses to start if the file is missing or invalid. Every initial or recorded navigation, element action, recovery action, output read, and checkpoint locator is checked. Unapproved redirects stop replay. `ALLOWED_DOMAINS` remains a separate navigation restriction. Exact paths start with `/`; use `regex:<expression>` for a full-path regular expression. Locator fallbacks must also be reviewed.

Global forbidden keywords deny actions. Actions requiring confirmation pause for human review; an operator can perform the step in the existing session and explicitly resume at the next step. Keep `allow_new_urls` and `allow_unknown_elements` false. An optional local bypass requires `ALLOWLIST_MODE=development` and identical nonempty `ALLOWLIST_BYPASS_TOKEN` and `ALLOWLIST_BYPASS_PRESENTED_TOKEN`. Production mode ignores bypass tokens. The example covers only member search; add every target and destination path for other workflows.

AutomationCapture discovers browser workflows with a Claude-guided Selenium agent and records reusable artifacts for later deterministic replay. Phases 2 through 13 add page observation, browser actions, a bounded discovery loop, a versioned artifact schema, recording, JSON file persistence, robust locator resolution, deterministic replay, runtime outcome classification, checkpoint verification, stuck state detection, escalation requests, a mock human handoff, and session lifecycle tracking. The Flask target application will be implemented in later phases.

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

A pause returns `ReplayResult(status="escalated", success=False, stuck_state=..., escalation_request=...)` with the reason, one-based pending step, recommendation, and browser snapshot. Hard failures keep `status="hard_failure"` and carry an escalation request when evidence can be captured and saved. Business outcomes keep their own status. The signature truncation, repeat threshold, history length, risky keywords, and number of visible elements in evidence are configured with `STUCK_*` variables in `config.example.env`. Evidence contains full DOM and screenshots and may contain sensitive data.

## Escalation requests

Replay saves one JSON request per pause to `ESCALATION_DIRECTORY` (default `evidence/escalations`). Each request contains a UUID, UTC timestamp, artifact and discovery IDs, session ID, last action, prior successful steps, reason, recommendation, screenshot, and DOM. `escalation_directory=` can override the destination per replay call. Input values are masked in `input_params` and human-facing metadata; screenshots and DOM remain raw browser evidence and must be stored and shared carefully. Files are written atomically with owner-only permissions on POSIX systems, and the default evidence directory is excluded from Git. If evidence is incomplete or cannot be saved, a pause returns a hard failure with `evidence["escalation_error"]`; it is never reported as a completed human handoff.

Use `create_escalation_request(...)` directly for other callers, `escalation_to_json`/`json_to_escalation` for JSON conversion, and `save_escalation_request`/`load_escalation_request`/`list_escalation_requests` for persistence. `get_available_elements` suggests controls from a static DOM snapshot; it cannot determine computed CSS visibility. Saving a request does not notify an operator or authorize resuming a risky action.

## Human handoff and resume

`ReplayResult.handoff_session` is a paused session retaining the original WebDriver object. Replay saves a JSON snapshot of the control state to `HANDOFF_DIRECTORY` (default `evidence/handoffs`). The snapshot excludes the driver; `load_handoff_session(path)` returns a detached record that cannot operate the browser. Reattach only with `load_handoff_session(path, original_driver)` while that exact session is still live. No browser is created from the session ID.

```python
from src.escalation.human_handoff import SessionManager, MockOperatorInterface, save_handoff_session

paused = replay_artifact(driver, artifact, inputs)
session = paused.handoff_session  # Only present when a live handoff was created.
manager = SessionManager()
manager.give_control_to_human(session)
operator = MockOperatorInterface(operator_id="operator_1", actions=prepared_human_actions)
operator.take_control(driver, paused.escalation_request, session)
# An authorized human reviews the result and explicitly chooses the next pending step.
operator.confirm_resume()
if operator.signal_resume():
    manager.approve_resume(session, resume_step=paused.step_failed + 1)
    manager.give_control_to_automation(session)
    save_handoff_session(session, "evidence/handoffs/approved.json")
    resumed = replay_artifact(driver, artifact, inputs, handoff_session=session)
```

The mock runs only the supplied actions; it does not approve or resume automatically. For a risky click completed by the operator, select the **next** pending step. Replaying the same risky step can escalate again. Replay verifies the artifact ID, browser object, session ID, and human approval, skips already completed steps and initial navigation, and still verifies the final checkpoint. Successful resumed results include `updated_artifact` with redacted human intervention records; save it explicitly if you want to replace the recorded artifact. `HumanAction.value` for typing is masked in saved audits. Save the session again after control changes to persist its latest state. The mock offers no remote viewing or operator authentication; these belong to later phases.

## Session lifecycle

Every `replay_artifact` run creates a UUID-based `ReplayResult.session` on the original Selenium driver. `SessionLifecycleManager` tracks `created → running → paused → human_control → resumed → completed` as well as failed and abandoned outcomes; the Phase 12 `SessionManager` remains responsible for the separate control transfer. The lifecycle stores step timings and success flags, escalation records, total human actions, duration, and final outputs. Saved JSON is written atomically to `SESSION_DIRECTORY` (default `evidence/sessions`) when a run pauses or reaches a terminal state. Raw input values are never stored in lifecycle metadata, but nested escalation browser evidence and outputs can be sensitive.

On resume, pass the approved `handoff_session` to `replay_artifact`. It loads its matching lifecycle record, verifies the artifact and original live driver, and continues the same run ID. You may also pass `lifecycle_session=` to use an in-memory record. `load_session_metadata(path)` returns a detached audit; `load_session_metadata(path, original_driver)` reattaches the live browser after verifying its Selenium session ID. Saving a session cannot restore a browser that has closed.

`list_sessions()` lists saved audits newest first. `cleanup_old_sessions(directory, days=None)` removes only **terminal** lifecycle JSON files older than the configured `SESSION_RETENTION_DAYS` (default 30). It leaves paused/running records, handoff files, and escalation evidence intact. Use `get_session_summary`, `get_full_session_report`, and `calculate_session_metrics` for reports without duplicating screenshots.

## Phase 17 audit events

Replay writes a redacted, session-scoped JSON event array to
`LOG_DIRECTORY/<lifecycle-session-id>.json` (default `evidence/logs`). Events
include the browser step, risk level, error classification, escalation,
approval, checkpoint, and final outcome. Paused runs save their events; an
approved resume appends to the same audit file. Logs and session metadata use
the configured `REDACTION_LEVEL`, and screenshots and DOM snapshots stay in
the separate private evidence path. `STRUCTURED_LOG_LEVEL` controls console
output while preserving the complete event array.

## Phase 18 evidence capture

Replay saves private browser evidence under `EVIDENCE_DIRECTORY/<session-id>`
(default `evidence/capture`). Step timings are recorded in `metrics/`; a passed
final checkpoint adds a PNG and DOM snapshot. Failures retain immediate
exception context, traceback, screenshot, and DOM. Escalations create a
dedicated folder with all available browser signals and a capture manifest.
`index.json` lists evidence paths, timestamps, byte sizes, and SHA-256 hashes
without duplicating page content. Selenium browser logs and HAR exports are
optional; raw performance events are not represented as HAR. Evidence files
contain unredacted page content and are written with owner-only permissions on
POSIX. `ReplayResult.evidence_manifest_path` and `evidence_capture_errors`
show where evidence was saved and which optional signals were unavailable.
