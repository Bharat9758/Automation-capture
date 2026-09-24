# AutomationCapture

AutomationCapture discovers browser workflows with a Claude-guided Selenium agent and records reusable artifacts for later deterministic replay. Phases 2 through 4 add page observation, browser actions, a bounded discovery loop, a versioned artifact schema, and an artifact recorder. Artifact persistence, replay, escalation, and the Flask target application will be implemented in later phases.

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

`record_discovery_run(steps, agent_logs, goal, target_url)` returns a validated `AutomationArtifact` for a successful run with replayable actions. It turns typed literals into named placeholders, infers output types from successful `read_text` actions, excludes failed attempts from replay steps, and records those attempts in `known_errors`. The scan limit, default step timeout, initial artifact version, and creator label are configurable in `.env`. A run that only reports `done`, or an output goal with no successful read, cannot yield a complete artifact. Artifacts are returned to the caller; saving them is a later phase.
