# AutomationCapture

AutomationCapture discovers browser workflows with a Claude-guided Selenium agent and defines reusable artifacts for later deterministic replay. Phases 2 and 3 add page observation, validated browser actions, a bounded goal-driven loop, and a versioned artifact schema. Artifact storage, replay, escalation, and the Flask target application will be implemented in later phases.

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

The loop returns `success`, `steps`, `final_state` (base64 PNG), `logs`, and `error`. A success result means Claude reported `goal_met=true` based on the latest observation; it is not an independent verification of the outcome. Typed field values are excluded from returned steps and JSON logs. The accessibility signal is a cross-browser DOM-derived semantic snapshot, not a browser-native accessibility tree.

## Artifact format

`src.artifact.schema` defines Pydantic dataclasses for locators, steps, checkpoints, inputs, outputs, and the complete artifact. Use `AutomationArtifact.from_dict(data)` to parse a JSON-shaped dictionary, `artifact.to_dict()` to serialize it, and `artifact.validate()` to check an existing instance. `ARTIFACT_JSON_SCHEMA` exposes its Draft 2020-12 JSON Schema. Invalid input to `from_dict` raises a validation exception; `validate()` returns `False` for an invalid instance. The schema requires timezone-aware ISO timestamps, Semantic Versioning 2.0.0, consecutively numbered steps, and a success rate between 0 and 1 when present.
