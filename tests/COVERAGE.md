# Test coverage, Phases 22–26

Measured on 2026-10-12 with Python 3.12 and the pinned test dependencies:

```bash
pytest tests/ -q --cov=src --cov-report=html --cov-report=term-missing
```

**Result:** 300 passed in 12.89 seconds; 3,974 of 4,625 executable statements covered (85.92%, rounded to 86%). The baseline before these integration tests was 251 passing tests and approximately 85% overall coverage. The HTML report is generated locally in `htmlcov/index.html`; it is intentionally excluded from version control.

| Component | Covered / statements | Coverage |
| --- | ---: | ---: |
| Agent (`observer`, `actor`, LLM loop) | 231 / 284 | 81.3% |
| Artifact schema, recorder, serializer | 471 / 555 | 84.9% |
| Replay, locators, errors, checkpoints | 984 / 1,151 | 85.5% |
| Safety, redaction, risk | 544 / 625 | 87.0% |
| Escalation and session lifecycle | 804 / 895 | 89.8% |
| Structured logging and evidence | 426 / 484 | 88.0% |
| Local Flask app | 182 / 197 | 92.4% |
| CLI, configuration, and entry point | 332 / 434 | 76.5% |

Component totals group the application modules and sum to the overall measurement. Individual core modules: `replay_engine.py` 83%, `locator_strategy.py` 93%, `checkpoint.py` 91%, `allowlist.py` 84%, `risk_classifier.py` 90%, `structured_logger.py` 89%, and `evidence_capture.py` 85%. Consult the HTML report for the exact unexecuted lines.

## Critical workflow assertions

- A mocked Claude response drives real discovery recording, JSON persistence, parameterized deterministic replay, final checkpoint verification, and numeric extraction.
- A known no-result banner returns a business outcome with no human escalation. A failed final checkpoint prevents output extraction.
- Primary locators exercise CSS, XPath, ID, exact text, and ARIA label; replayed parameters reach Selenium without mutating the saved template.
- Reviewed actions succeed; forbidden intent, unknown elements, and unapproved navigation are blocked before browser interaction. Elevated risk emits an audit event, and critical risk pauses for an operator approval before continuing on the same WebDriver.
- Audit logs redact member IDs; private escalation evidence retains the original browser state. Error and escalation evidence include indexed snapshots and context, and persisted logs remain parseable.
- CLI help runs as a separate process. CLI discovery and replay exercise the real handlers while mocking external boundaries. Flask login, CSRF, member search, scenario state, and logout run through its test client.

The five requested workflow families and the four integration suites are present. **100% statement coverage on replay and 95% on safety are not yet achieved.** The remaining lines include rare WebDriver and filesystem exceptions, alternative recovery paths, allowlist configuration edge cases, and CLI error/printing paths. These are visible in `htmlcov/index.html` after running the coverage command. The suite mocks Selenium and Claude; it does not constitute a real browser, live API, or production banking end-to-end benchmark.

## Performance and reproducibility

The 12.89 second elapsed time above is a **mocked test-suite runtime**, not a browser automation latency benchmark. No Selenium browser or network service is launched. Run the command on the target Python 3.11+ environment to reproduce current numbers; timings and line totals can change as code evolves. There is no configured coverage failure threshold, since the requested per-module thresholds remain unmet and a pass should not imply otherwise.
