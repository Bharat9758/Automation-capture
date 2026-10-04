"""Flask client tests for the demo search app and replay selectors."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from flask import Flask
from flask.testing import FlaskClient
from werkzeug.test import TestResponse

from src.artifact.schema import Locator
from src.safety.allowlist import is_element_allowed, is_url_allowed, load_allowlist
from src.target_app.local_app import create_app


@pytest.fixture
def app() -> Flask:
    """Build an isolated test application.

    Returns:
        Configured Flask app.
    """
    return create_app({"TESTING": True, "FLASK_SECRET_KEY": "local-demo-test-secret-key-for-pytest-only"})


@pytest.fixture
def client(app: Flask) -> FlaskClient:
    """Open a browser client with cookie state.

    Args:
        app: Local fixture.

    Returns:
        Flask test client.
    """
    return app.test_client()


def csrf(client: FlaskClient, path: str) -> str:
    """Load a form and read its session-bound token.

    Args:
        client: Browser client.
        path: Login or search form path.

    Returns:
        Current token.
    """
    response = client.get(path)
    assert response.status_code == 200
    with client.session_transaction() as state:
        token = state["csrf_token"]
    assert token.encode() in response.data
    return token


def login(client: FlaskClient, username: str = "member", password: str = "password") -> TestResponse:
    """Submit the demo login form.

    Args:
        client: Browser client.
        username: Demo username.
        password: Demo password.

    Returns:
        Flask response.
    """
    return client.post("/login", data={"username": username, "password": password,
                                       "csrf_token": csrf(client, "/login")})


def search(client: FlaskClient, member_id: str) -> TestResponse:
    """Submit a member search in this client session.

    Args:
        client: Logged-in browser client.
        member_id: Requested identifier.

    Returns:
        Flask response.
    """
    return client.post("/members/search", data={"member_id": member_id,
                                                "csrf_token": csrf(client, "/members/search")})


def test_home_scenarios_and_assets(client: FlaskClient) -> None:
    """Home, scenario links and static assets have browser-ready markup."""
    home = client.get("/")
    assert home.status_code == 200 and b"Go to Login" in home.data
    assert b"Test Member Search" in home.data
    for name in ("success", "not_found", "invalid_input", "delayed", "flaky"):
        assert name.encode() in home.data
    assert client.get("/static/style.css").status_code == 200
    assert client.get("/static/script.js").status_code == 200
    assert client.get("/?scenario=unknown").status_code == 400


def test_login_errors_auth_guard_and_logout(client: FlaskClient) -> None:
    """Credentials and member pages follow the configured demo session."""
    assert client.get("/members/search").status_code == 302
    assert client.get("/members/12345").status_code == 302
    bad_user = login(client, "unknown", "password")
    assert bad_user.status_code == 401 and b"Username not found" in bad_user.data
    bad_password = login(client, "member", "incorrect")
    assert bad_password.status_code == 401 and b"Invalid password" in bad_password.data
    assert login(client).location.endswith("/members/search")
    assert b'member_id_search' in client.get("/members/search").data
    assert client.get("/logout").location == "/"
    assert client.get("/members/search").status_code == 302


def test_csrf_blocks_direct_posts(client: FlaskClient) -> None:
    """An unrendered POST cannot establish a session or search."""
    assert client.post("/login", data={"username": "member", "password": "password"}).status_code == 400
    login(client)
    assert client.post("/members/search", data={"member_id": "12345"}).status_code == 400


def test_search_details_recent_history(client: FlaskClient) -> None:
    """Both configured members expose stable replay output IDs."""
    assert login(client).status_code == 302
    for member_id, balance in (("12345", "$5000.00"), ("54321", "$10000.00")):
        result = search(client, member_id)
        assert result.status_code == 302 and result.location.endswith(f"/members/{member_id}")
        details = client.get(result.location)
        for element_id in ("member_name", "member_id_display", "account_number",
                           "savings_balance", "checking_balance", "logout_btn"):
            assert f'id="{element_id}"'.encode() in details.data
        assert balance.encode() in details.data
    recent = client.get("/members/search").data
    assert b"54321" in recent and b"12345" in recent
    assert client.get("/members/99999").status_code == 404


@pytest.mark.parametrize(("member_id", "message"), [
    ("", "Member ID required"), ("12A45", "Member ID must be numeric"),
    ("99999", "No such member")])
def test_search_errors(client: FlaskClient, member_id: str, message: str) -> None:
    """Input and legitimate no-result cases have distinct inline messages."""
    login(client)
    response = search(client, member_id)
    assert response.status_code == 200 and message.encode() in response.data
    assert b'id="search_error"' in response.data


def test_scenarios_are_session_bound(client: FlaskClient, app: Flask) -> None:
    """Forced outcomes survive redirects but stay within one browser."""
    client.get("/?scenario=not_found")
    login(client)
    assert b"No such member" in search(client, "12345").data
    client.get("/?scenario=invalid_input")
    assert b"Member ID must be numeric" in search(client, "12345").data
    other = app.test_client()
    login(other)
    assert search(other, "12345").status_code == 302


def test_delay_flakiness_and_error_escaping(client: FlaskClient, app: Flask) -> None:
    """Transient behavior can be made deterministic for replay tests."""
    login(client)
    client.get("/?scenario=delayed")
    with patch("src.target_app.local_app.time.sleep") as sleeper:
        assert search(client, "12345").status_code == 302
        sleeper.assert_called_once_with(0.25)
    client.get("/?scenario=flaky")
    app.config["FLAKY_RANDOM"] = lambda: 0.0
    assert search(client, "12345").status_code == 503
    app.config["FLAKY_RANDOM"] = lambda: 1.0
    assert search(client, "12345").status_code == 302
    assert b"&lt;script&gt;" in client.get("/error?message=%3Cscript%3E").data
    assert b'id="success_message"' in client.get("/success").data


def test_factory_rejects_unsafe_config(tmp_path: Path) -> None:
    """Startup fails for weak secrets and invalid balance fixtures."""
    with pytest.raises(ValueError, match="FLASK_SECRET_KEY"):
        create_app({"FLASK_SECRET_KEY": "short"})
    fixture = tmp_path / "bad.json"
    fixture.write_text(json.dumps({"demo_username": "member", "demo_password": "password",
                                   "members": {"12345": {"name": "Demo", "account_number": "ACC",
                                                           "savings_balance": "NaN", "checking_balance": 0}},
                                   "recent_search_limit": 5, "delayed_seconds": 0,
                                   "flaky_failure_rate": 0.5}), encoding="utf-8")
    with pytest.raises(ValueError, match="Balances"):
        create_app({"FLASK_SECRET_KEY": "a" * 32, "TARGET_APP_CONFIG_PATH": str(fixture)})


def test_local_allowlist_covers_search_and_outputs() -> None:
    """The reviewed localhost rules authorize exact demo selectors."""
    path = Path(__file__).resolve().parents[1] / "config" / "target_app.allowlist.example.json"
    policy = load_allowlist(str(path))
    details = "http://127.0.0.1:5000/members/12345"
    assert is_url_allowed("http://127.0.0.1:5000/login", policy)
    assert is_url_allowed(details, policy)
    locator = Locator(strategy="id", value="savings_balance", robustness_notes="Stable ID")
    assert is_element_allowed(locator, details, "read", policy)[0]
    assert not is_url_allowed("http://evil.test/members/12345", policy)
