# AutomationCapture: Design and Delivery Report

This report describes the implementation through Phases 27–28. It distinguishes the tested local prototype from deployment work still required for a live banking environment. The full test suite measured **300 passing tests and 86% overall statement coverage** in Phases 22–26; see [tests/COVERAGE.md](tests/COVERAGE.md) for component results and gaps.

## 1. Architecture

AutomationCapture has four cooperating subsystems:

| Subsystem | Modules | Responsibility |
| --- | --- | --- |
| Discovery | `src/agent/`, `src/artifact/recorder.py` | Observe the page, ask Claude for the next action, use Selenium to act, and record a successful trace. |
| Replay | `src/replay/`, `src/artifact/serializer.py` | Load and validate an artifact, substitute inputs, execute saved steps without Claude, verify a checkpoint, and extract outputs. |
| Escalation | `src/escalation/` | Detect a blocked or risky state, persist a request, transfer control of the live driver, and track the session lifecycle. |
| Safety and audit | `src/safety/`, `src/logging/` | Check destinations and elements, grade action risk, redact persisted metadata, and collect JSON events and browser evidence. |

Discovery and replay communicate through a versioned artifact. This boundary makes a recorded automation inspectable and reusable with new inputs. The local Flask target app is a separate test fixture; configuration and CLI modules provide the user-facing entry point.

A legitimate negative result, such as “member not found,” is a **business outcome**, not a broken automation. The replay result separates `success`, `business_outcome`, `recoverable_error`, `hard_failure`, and `escalated` statuses as applicable. The operator handoff retains the same live Selenium driver and browser session, avoiding a new login. Logs and session metadata are redacted, while raw browser evidence is stored separately with restricted local permissions.

## 2. Artifact Schema

`AutomationArtifact` is a Pydantic dataclass with a unique ID, semantic version, timezone-aware timestamps, creator and discovery-run metadata, target URL, named inputs, declared outputs, ordered steps, a final success checkpoint, known-error rules, and optional human-intervention history. Its nested `Locator` accepts CSS, XPath, ID, text, or ARIA label plus ordered fallback locators and robustness notes. `ActionStep` records the action, optional locator and value, expected outcome, timeout, and reasoning.

Recording replaces successful typed literals with named placeholders such as `{member_id}`; callers provide input values at replay time. Output fields identify the element and type to extract. Known-error detections can identify a valid business result, a recoverable condition, or a hard failure. Recorder inference is a starting point: reviewers must check step intent, selectors, error rules, and checkpoint strength before production reuse.

`to_json()` emits plain JSON; `save_artifact_to_file()` writes an atomic file with three `#` metadata header lines before the JSON body. The provided `load_artifact_from_file()` handles those headers, validates nested schema data, and checks the stored version. A generic JSON parser must use `to_json()` output, not the complete header-prefixed file. The CLI validator also requires absolute HTTP(S) values for navigation steps.

## 3. Determinism & Error Handling

Replay reads only the artifact and runtime inputs; it does not call Claude. It validates required inputs and URL policy, navigates, substitutes placeholders in a copy of the steps, resolves locators, executes actions, and waits for applicable outcomes. A resolver tries the primary locator and its ordered fallbacks, waits for visibility, and refreshes stale references. CSS, XPath, ID, text, and ARIA-label strategies are supported. A stable plan yields the same **intended actions** for the same artifact and inputs; external page or data changes can yield different observed outcomes.

The error handler evaluates reviewed `known_errors` detections before applying exception-based classifications. Matched business outcomes return a valid negative result without extracting outputs or creating an unnecessary handoff. Recoverable conditions can execute an allowlisted repair and retry within `REPLAY_MAX_RECOVERY_RETRIES`. Clicks are not automatically retried without an explicit recovery path because their effect may already have occurred. Hard failures retain the failed step and available screenshot, DOM, URL, and title.

The final `CheckpointVerifier` gates output extraction. It supports visible or existing elements, text, URL expectations, and element counts with bounded waits. **A failed checkpoint produces no extracted outputs.** Descriptive `expected_outcome` strings alone do not prove a business goal; use explicit condition directives and a specific final checkpoint. Discovery completion is Claude's assessment of its observation, so the artifact and replay checkpoint need independent review.

## 4. Heterogeneity & Multi-Tenant

Target-specific behavior lives in artifacts and reviewed JSON allowlists rather than in a bank-specific replay branch. Each allowlist names exact domains, paths, elements, locator fallbacks, and allowed actions. Environment variables configure browser choice, allowed domains, risk thresholds, redaction, logging, timeouts, and evidence paths. The same event and evidence formats apply to the local test app and to another approved browser application.

Sessions use UUIDs and per-session evidence directories; reports can be retrieved by session ID. This is **organization and trace separation**, not a multi-tenant security boundary. The current process has no tenant identity, role-based access control, per-tenant key management, isolated workers, or remotely shared session store. A deployment serving independent tenants must add those controls, validate paths and ownership, define retention policies, and protect raw evidence separately.

The included example allowlist covers the local app's primary selectors. Discovery may add an equivalent fallback such as a CSS ID lookup, but replay checks **every fallback** against the reviewed allowlist too. New applications and recorded artifacts therefore require policy review before use.

## 5. Escalation & Handoff

The stuck detector hashes URL, title, visible text, and control count, using a configurable repetition threshold (default three). It also considers hard failure, ambiguity, risky upcoming actions, and an explicit request for help. On a pause, replay persists an escalation request with its reason, pending step, recommendation, recent actions, screenshot, DOM, URL, and redacted input metadata. A session-scoped evidence collector stores available signals and a manifest.

`SessionManager` in `human_handoff.py` transfers control from automation to a human and back on the **same live WebDriver**. Operator actions are timestamped. A human must explicitly approve the pending step or confirm that the step was completed manually before automation resumes at the chosen next step; a critical risk approval is bound to that step. Resumed replay verifies the artifact, browser, session, and approval before continuing, then checks the final success condition.

`SessionLifecycleManager` in `session_manager.py` records creation, running, pause, human control, resume, completion, failure, and abandonment, along with step timings and escalations. JSON snapshots preserve an audit trail but cannot recreate a closed browser. The bundled `MockOperatorInterface` executes only prepared actions; it does not offer co-browsing, authenticate an operator, or automatically grant approval. The CLI closes its WebDriver after discovery or replay, so a live pause/resume integration must use the Python API in a persistent process.

## 6. Safety

The three layers have different jobs:

| Layer | Enforcement | Result |
| --- | --- | --- |
| Reviewed allowlist | Exact domain/path checks, permitted page actions and elements, forbidden keywords, and approval rules before navigation or interaction. | Deny an unapproved operation before the browser acts. |
| Action risk | Cumulative scoring from action type, locator and reasoning, failures, recovery, and sequence; SAFE, CAUTION, RISKY, CRITICAL. | Log or warn; pause elevated actions for explicit human approval. |
| Data redaction | Configured field and pattern masks on process logs, structured audit events, session records, and exported summaries. | Limit sensitive values in shareable metadata. |

An approval does not override a forbidden or unknown element. `ALLOWED_DOMAINS` also limits navigation independently of the replay allowlist. Development bypass tokens require explicit development mode; production mode ignores them. `REDACTION_LEVEL=STRICT` is the default, while `NONE` is limited by environment validation.

Raw screenshots and DOM snapshots remain unredacted so a human can diagnose the actual page. They can contain personal and financial information. Local evidence writes use owner-only permissions where supported and evidence paths are ignored by Git, but deployments still need access controls, encryption and retention suited to their data. Keyword-based risk and masking cannot detect every unfamiliar irreversible action or every identifier in free prose; policy review remains necessary.

## 7. Cuts & Future Work

| Current boundary | Reason for the cut | Next step |
| --- | --- | --- |
| Mock operator rather than remote co-browsing | Core same-session control and approvals can be tested without building streaming UI. | Authenticated operator console with controlled session sharing and action audit. |
| JSON files and in-memory demo data rather than a database | Local tests need no external service. | Transactional storage, artifact registry, retention, and access rules for multiple deployments. |
| Optional driver-provided HAR, no built-in proxy or video | Screenshots, DOM, console logs, timings, and error context cover the current debugging path. | Introduce a reviewed network capture/proxy and optional video if required. |
| Single-process live handoff | A saved session ID cannot reconnect to a closed WebDriver. | Session broker or remote driver with authentication and explicit reconnection semantics. |
| Mocked browser and Claude in automated integration tests | CI remains repeatable and self-contained. | Run an opt-in real-browser test against the local Flask app and a separate, controlled Claude smoke test. |
| Incomplete per-module coverage targets | Current measured coverage is 86% overall; replay engine is 83%, safety package 87%. | Add targeted tests for remaining recovery, policy, and filesystem exception branches. |

Work was prioritized around the discovery-to-artifact-to-replay contract, checkpoint-gated extraction, legitimate business outcomes, allowlist and approval enforcement, session continuity, audit evidence, and a self-contained test application. Phases 29–31 remain future delivery work; this report does not label them completed.
