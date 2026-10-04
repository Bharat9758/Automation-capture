"""Local member-search fixture with reproducible Selenium selectors and errors."""

from __future__ import annotations

import hmac
import json
import os
import random
import re
import secrets
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping

from flask import Flask, abort, current_app, flash, redirect, render_template, request, session, url_for
from werkzeug.wrappers import Response

from src.logging import get_logger


LOGGER = get_logger(__name__)
SCENARIOS = ("success", "not_found", "invalid_input", "delayed", "flaky")
_ASCII_NUMBER = re.compile(r"^[0-9]+$")


@dataclass(frozen=True, kw_only=True)
class Member:
    """Validated demo account data displayed on the details page."""

    member_id: str
    name: str
    account_number: str
    savings_balance: Decimal
    checking_balance: Decimal


@dataclass(frozen=True, kw_only=True)
class TargetAppSettings:
    """Demo configuration loaded from a reviewed local JSON file."""

    username: str
    password: str
    members: dict[str, Member]
    recent_search_limit: int
    delayed_seconds: float
    flaky_failure_rate: float


def _load_settings(filepath: str) -> TargetAppSettings:
    """Load and validate the fixture file without recording its credentials.

    Args:
        filepath: Configured JSON path.

    Returns:
        Fully validated demo settings.

    Raises:
        ValueError: If a field is missing, malformed, or unsafe.
        OSError: If the file cannot be opened.
    """
    with open(filepath, encoding="utf-8") as stream:
        raw = json.load(stream)
    if not isinstance(raw, dict) or not isinstance(raw.get("members"), dict) or not raw["members"]:
        raise ValueError("Target app needs a nonempty member fixture")
    username, password = raw.get("demo_username"), raw.get("demo_password")
    if not isinstance(username, str) or not username or not isinstance(password, str) or not password:
        raise ValueError("Demo credentials must be nonempty strings")
    members: dict[str, Member] = {}
    try:
        for member_id, value in raw["members"].items():
            if not isinstance(member_id, str) or not _ASCII_NUMBER.fullmatch(member_id) or not isinstance(value, dict):
                raise ValueError("Fixture member IDs must be numeric strings")
            name, account = value["name"], value["account_number"]
            if not isinstance(name, str) or not name.strip() or not isinstance(account, str) or not account.strip():
                raise ValueError("Member names and account numbers must be nonempty")
            savings = Decimal(str(value["savings_balance"]))
            checking = Decimal(str(value["checking_balance"]))
            if any(not amount.is_finite() or amount < 0 for amount in (savings, checking)):
                raise ValueError("Balances must be finite, nonnegative numbers")
            members[member_id] = Member(member_id=member_id, name=name,
                                        account_number=account, savings_balance=savings,
                                        checking_balance=checking)
        limit = raw["recent_search_limit"]
        delay = float(raw["delayed_seconds"])
        failure_rate = float(raw["flaky_failure_rate"])
    except (KeyError, TypeError, InvalidOperation, OverflowError) as exc:
        raise ValueError("Target app fixture contains invalid fields") from exc
    if type(limit) is not int or not 0 <= limit <= 100:
        raise ValueError("recent_search_limit must be between 0 and 100")
    if not 0 <= delay <= 5 or not 0 <= failure_rate <= 1:
        raise ValueError("Delay must be 0–5 seconds and failure rate 0–1")
    return TargetAppSettings(username=username, password=password, members=members,
                             recent_search_limit=limit, delayed_seconds=delay,
                             flaky_failure_rate=failure_rate)


def _csrf_token() -> str:
    """Generate or retrieve a token bound to the current browser session.

    Returns:
        Opaque form token.
    """
    token = session.get("csrf_token")
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _require_csrf() -> None:
    """Reject POSTs from outside the rendered form.

    Raises:
        HTTPException: If the submitted token is invalid.
    """
    expected, supplied = session.get("csrf_token"), request.form.get("csrf_token")
    if not isinstance(expected, str) or not isinstance(supplied, str) or not hmac.compare_digest(expected, supplied):
        abort(400, description="Invalid form token")


def _safe_scenario() -> str:
    """Read the session's selected test scenario.

    Returns:
        One supported scenario.
    """
    default = current_app.config.get("DEFAULT_SCENARIO", "success")
    value = session.get("scenario", default)
    return value if value in SCENARIOS else default


def _login_required() -> Response | None:
    """Require the configured user to access member data.

    Returns:
        Login redirect for anonymous browsers, otherwise None.
    """
    if session.get("username") is None:
        flash("Please log in to continue.", "warning")
        return redirect(url_for("login"))
    return None


def create_app(config_override: Mapping[str, Any] | None = None) -> Flask:
    """Build an isolated, local Flask application.

    Supply ``FLASK_SECRET_KEY`` and optionally ``TARGET_APP_CONFIG_PATH`` in
    the environment. The override is intended for tests and controlled local
    embedding; the JSON fixture never contains real member data.

    Args:
        config_override: Optional settings for a test or local runner.

    Returns:
        Flask app with member-search routes and static assets.

    Raises:
        ValueError: If the secret or fixture configuration is invalid.
    """
    config = dict(config_override or {})
    secret = config.get("FLASK_SECRET_KEY", os.environ.get("FLASK_SECRET_KEY"))
    if not isinstance(secret, str) or len(secret) < 32 or secret.startswith("replace-"):
        raise ValueError("FLASK_SECRET_KEY must be a unique secret of at least 32 characters")
    fixture_path = config.get("TARGET_APP_CONFIG_PATH", os.environ.get("TARGET_APP_CONFIG_PATH"))
    if fixture_path is None:
        fixture_path = str(Path(__file__).resolve().parent / "fixtures" / "example.json")
    if not isinstance(fixture_path, str) or not fixture_path:
        raise ValueError("TARGET_APP_CONFIG_PATH must be a valid path")
    settings = _load_settings(fixture_path)
    scenario = config.get("DEFAULT_SCENARIO", "success")
    if scenario not in SCENARIOS:
        raise ValueError("DEFAULT_SCENARIO must be a supported test scenario")
    app = Flask(__name__, template_folder="templates", static_folder="static")
    app.config.update(SECRET_KEY=secret, SESSION_COOKIE_HTTPONLY=True,
                      SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=bool(config.get("SESSION_COOKIE_SECURE", False)),
                      TARGET_SETTINGS=settings, DEFAULT_SCENARIO=scenario)
    app.config.update({key: value for key, value in config.items()
                       if key in {"TESTING", "FLAKY_RANDOM"}})

    @app.before_request
    def select_scenario() -> None:
        """Validate a requested scenario and remember it across redirects."""
        requested = request.args.get("scenario")
        if requested is not None:
            if requested not in SCENARIOS:
                abort(400, description="Unknown test scenario")
            session["scenario"] = requested

    @app.context_processor
    def template_context() -> dict[str, Any]:
        """Expose only safe shared display values to templates.

        Returns:
            CSRF token and selected scenario.
        """
        return {"csrf_token": _csrf_token, "scenario": _safe_scenario()}

    @app.get("/")
    def index() -> str:
        """Show supported demo scenarios.

        Returns:
            Home page.
        """
        return render_template("index.html", scenarios=SCENARIOS)

    @app.route("/login", methods=["GET", "POST"])
    def login() -> str | Response | tuple[str, int]:
        """Authenticate the configured demo user and rotate session state.

        Returns:
            Login form with inline errors or a member-search redirect.
        """
        if request.method == "GET":
            return render_template("login.html", error=None)
        _require_csrf()
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if not hmac.compare_digest(username.encode("utf-8"), settings.username.encode("utf-8")):
            LOGGER.warning("demo_login_rejected", extra={"event": "demo_login_rejected", "reason": "unknown_username"})
            return render_template("login.html", error="Username not found"), 401
        if not hmac.compare_digest(password.encode("utf-8"), settings.password.encode("utf-8")):
            LOGGER.warning("demo_login_rejected", extra={"event": "demo_login_rejected", "reason": "invalid_password"})
            return render_template("login.html", error="Invalid password"), 401
        scenario = _safe_scenario()
        session.clear()
        session["username"] = settings.username
        session["scenario"] = scenario
        LOGGER.info("demo_login_success", extra={"event": "demo_login_success"})
        return redirect(url_for("member_search"))

    @app.route("/members/search", methods=["GET", "POST"])
    def member_search() -> str | Response | tuple[str, int]:
        """Validate a member ID and display a known or negative outcome.

        Returns:
            Search form, error, or details redirect.
        """
        auth_redirect = _login_required()
        if auth_redirect is not None:
            return auth_redirect
        recent = session.get("recent_searches", [])
        if request.method == "GET":
            return render_template("member_search.html", error=None, recent=recent, member_id="")
        _require_csrf()
        member_id = request.form.get("member_id", "").strip()
        error: str | None = None
        if not member_id:
            error = "Member ID required"
        elif _ASCII_NUMBER.fullmatch(member_id) is None or _safe_scenario() == "invalid_input":
            error = "Member ID must be numeric"
        elif _safe_scenario() == "not_found" or member_id not in settings.members:
            error = "No such member"
        else:
            if _safe_scenario() == "delayed":
                time.sleep(settings.delayed_seconds)
            elif _safe_scenario() == "flaky":
                random_source: Callable[[], float] = app.config.get("FLAKY_RANDOM", random.random)
                if random_source() < settings.flaky_failure_rate:
                    LOGGER.warning("demo_transient_failure", extra={"event": "demo_transient_failure"})
                    return render_template("member_search.html", error="Temporary service error. Please retry.",
                                           recent=recent, member_id=member_id), 503
            if settings.recent_search_limit:
                session["recent_searches"] = ([member_id] + [item for item in recent if item != member_id])[:settings.recent_search_limit]
            LOGGER.info("demo_member_found", extra={"event": "demo_member_found", "scenario": _safe_scenario()})
            return redirect(url_for("member_details", member_id=member_id))
        LOGGER.info("demo_search_outcome", extra={"event": "demo_search_outcome", "outcome": error})
        return render_template("member_search.html", error=error, recent=recent, member_id=member_id), 200

    @app.get("/members/<member_id>")
    def member_details(member_id: str) -> str | Response:
        """Display configured balances for an authenticated member.

        Args:
            member_id: Numeric member identifier.

        Returns:
            Details HTML, login redirect, or a 404 page.
        """
        auth_redirect = _login_required()
        if auth_redirect is not None:
            return auth_redirect
        member = settings.members.get(member_id)
        if member is None:
            abort(404, description="No such member")
        return render_template("member_details.html", member=member)

    @app.get("/logout")
    def logout() -> Response:
        """Clear demo session state and return home.

        Returns:
            Redirect to the index page.
        """
        session.clear()
        return redirect(url_for("index"))

    @app.get("/error")
    def error() -> str:
        """Render a safely escaped demo error message.

        Returns:
            Error page.
        """
        return render_template("error.html", message=request.args.get("message", "An error occurred"))

    @app.get("/success")
    def success() -> str:
        """Render a test success state.

        Returns:
            Success page.
        """
        return render_template("success.html", message="The local test completed successfully.")

    @app.errorhandler(400)
    @app.errorhandler(404)
    @app.errorhandler(500)
    def handle_error(exception: Exception) -> tuple[str, int]:
        """Return a stable error page without disclosing internal exceptions.

        Args:
            exception: Flask HTTP or internal error.

        Returns:
            Rendered error page and its status code.
        """
        code = getattr(exception, "code", 500)
        message = exception.description if code in {400, 404} else "Unexpected application error"
        LOGGER.warning("demo_http_error", extra={"event": "demo_http_error", "status_code": code})
        return render_template("error.html", message=message), code

    return app


if __name__ == "__main__":
    from dotenv import load_dotenv

    load_dotenv()
    local_host = os.environ.get("TARGET_APP_HOST", "127.0.0.1")
    if local_host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("The demo server must bind to a loopback host")
    create_app().run(host=local_host, port=int(os.environ.get("TARGET_APP_PORT", "5000")), debug=False)
