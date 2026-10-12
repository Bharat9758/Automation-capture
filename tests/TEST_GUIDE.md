# Testing guide

## Setup and commands

Install Python 3.11 or newer, then from the repository root:

```bash
python -m pip install -e '.[test]'
pytest tests/ -q
pytest tests/test_core_integration.py -q
pytest tests/test_safety_integration.py tests/test_logging_integration.py -q
pytest tests/test_e2e_cli.py -q
pytest tests/ --cov=src --cov-report=term-missing --cov-report=html
```

Open `htmlcov/index.html` locally for per-line coverage. Generated HTML and `.coverage` are ignored. `requirements.txt` also pins `pytest` and `pytest-cov` for a requirements-based installation.

## Test layout

Unit tests use `tests/test_<module>.py`. The four `tests/test_*_integration.py` and `tests/test_e2e_cli.py` files cross module boundaries: discovery to replay, policy to handoff, audit to evidence, and CLI to configuration and Flask. Name tests `test_<observable_behavior>` and write assertions on statuses, events, files, and browser calls rather than private implementation details. Add a Google-style docstring and annotations to new test functions and local helpers.

## Fixtures and boundaries

- `tests/test_replay_engine.py` provides a validated artifact and a mocked WebDriver with an isolated allowlist and evidence directories. Its `driver` fixture creates `ALLOWLIST_PATH`, `ALLOWED_DOMAINS`, and per-test output paths.
- `tests/test_agent.py` supplies a fake Claude client and browser state for discovery. Keep the actual recorder and serializer running in an integration test.
- `tests/test_cli.py` supplies isolated CLI settings. The integration suite normalizes navigation to an absolute URL because the CLI validates this stricter contract.
- `tests/test_local_app.py` has CSRF-aware login and search helpers. Flask's `test_client()` checks real routes and sessions without opening a port.
- Use `tmp_path` for saved artifacts, audit files, and evidence. Patch the Claude client, WebDriver, CLI driver factory, or Flask listener only at the external boundary. Do not call a live API or a live banking host in CI.

When adding a new workflow, make a fixture that reproduces the page state, invoke the public agent/replay/CLI entry point, and verify both the return value and the persisted audit or evidence. For a rejected action, assert that Selenium never receives the prohibited click or navigation. For an approved handoff, assert the identical WebDriver and session ID are reused.

## Coverage interpretation

The statement figures and known gaps are in [COVERAGE.md](COVERAGE.md). A successful test count does not mean every exception path is covered. Rerun coverage after changing application code and update the recorded measurement rather than keeping stale percentages. Avoid tests that only duplicate source predicates; prefer an observable state transition, persisted file, or blocked side effect.
