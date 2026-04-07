"""Tests for authentication module: JWT, password hashing, AuthContext, and authz decorators."""

from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.gateway.auth import create_access_token, decode_token, hash_password, verify_password
from app.gateway.auth.models import User
from app.gateway.authz import (
    AuthContext,
    Permissions,
    get_auth_context,
    require_auth,
    require_permission,
)

# ── Password Hashing ────────────────────────────────────────────────────────


def test_hash_password_and_verify():
    """Hashing and verification round-trip."""
    password = "s3cr3tP@ssw0rd!"
    hashed = hash_password(password)
    assert hashed != password
    assert verify_password(password, hashed) is True
    assert verify_password("wrongpassword", hashed) is False


def test_hash_password_different_each_time():
    """bcrypt generates unique salts, so same password has different hashes."""
    password = "testpassword"
    h1 = hash_password(password)
    h2 = hash_password(password)
    assert h1 != h2  # Different salts
    # But both verify correctly
    assert verify_password(password, h1) is True
    assert verify_password(password, h2) is True


def test_verify_password_rejects_empty():
    """Empty password should not verify."""
    hashed = hash_password("nonempty")
    assert verify_password("", hashed) is False


# ── JWT ─────────────────────────────────────────────────────────────────────


def test_create_and_decode_token():
    """JWT creation and decoding round-trip."""
    user_id = str(uuid4())
    # Set a valid JWT secret for this test
    import os

    os.environ["AUTH_JWT_SECRET"] = "test-secret-key-for-jwt-testing-minimum-32-chars"
    token = create_access_token(user_id)
    assert isinstance(token, str)

    payload = decode_token(token)
    assert payload is not None
    assert payload.sub == user_id


def test_decode_token_expired():
    """Expired token returns TokenError.EXPIRED."""
    from app.gateway.auth.errors import TokenError

    user_id = str(uuid4())
    # Create token that expires immediately
    token = create_access_token(user_id, expires_delta=timedelta(seconds=-1))
    payload = decode_token(token)
    assert payload == TokenError.EXPIRED


def test_decode_token_invalid():
    """Invalid token returns TokenError."""
    from app.gateway.auth.errors import TokenError

    assert isinstance(decode_token("not.a.valid.token"), TokenError)
    assert isinstance(decode_token(""), TokenError)
    assert isinstance(decode_token("completely-wrong"), TokenError)


def test_create_token_custom_expiry():
    """Custom expiry is respected."""
    user_id = str(uuid4())
    token = create_access_token(user_id, expires_delta=timedelta(hours=1))
    payload = decode_token(token)
    assert payload is not None
    assert payload.sub == user_id


# ── AuthContext ────────────────────────────────────────────────────────────


def test_auth_context_unauthenticated():
    """AuthContext with no user."""
    ctx = AuthContext(user=None, permissions=[])
    assert ctx.is_authenticated is False
    assert ctx.has_permission("threads", "read") is False


def test_auth_context_authenticated_no_perms():
    """AuthContext with user but no permissions."""
    user = User(id=uuid4(), email="test@example.com", password_hash="hash")
    ctx = AuthContext(user=user, permissions=[])
    assert ctx.is_authenticated is True
    assert ctx.has_permission("threads", "read") is False


def test_auth_context_has_permission():
    """AuthContext permission checking."""
    user = User(id=uuid4(), email="test@example.com", password_hash="hash")
    perms = [Permissions.THREADS_READ, Permissions.THREADS_WRITE]
    ctx = AuthContext(user=user, permissions=perms)
    assert ctx.has_permission("threads", "read") is True
    assert ctx.has_permission("threads", "write") is True
    assert ctx.has_permission("threads", "delete") is False
    assert ctx.has_permission("runs", "read") is False


def test_auth_context_require_user_raises():
    """require_user raises 401 when not authenticated."""
    ctx = AuthContext(user=None, permissions=[])
    with pytest.raises(HTTPException) as exc_info:
        ctx.require_user()
    assert exc_info.value.status_code == 401


def test_auth_context_require_user_returns_user():
    """require_user returns user when authenticated."""
    user = User(id=uuid4(), email="test@example.com", password_hash="hash")
    ctx = AuthContext(user=user, permissions=[])
    returned = ctx.require_user()
    assert returned == user


# ── get_auth_context helper ─────────────────────────────────────────────────


def test_get_auth_context_not_set():
    """get_auth_context returns None when auth not set on request."""
    mock_request = MagicMock()
    # Make getattr return None (simulating attribute not set)
    mock_request.state = MagicMock()
    del mock_request.state.auth
    assert get_auth_context(mock_request) is None


def test_get_auth_context_set():
    """get_auth_context returns the AuthContext from request."""
    user = User(id=uuid4(), email="test@example.com", password_hash="hash")
    ctx = AuthContext(user=user, permissions=[Permissions.THREADS_READ])

    mock_request = MagicMock()
    mock_request.state.auth = ctx

    assert get_auth_context(mock_request) == ctx


# ── require_auth decorator ──────────────────────────────────────────────────


def test_require_auth_sets_auth_context():
    """require_auth sets auth context on request from cookie."""
    from fastapi import Request

    app = FastAPI()

    @app.get("/test")
    @require_auth
    async def endpoint(request: Request):
        ctx = get_auth_context(request)
        return {"authenticated": ctx.is_authenticated}

    with TestClient(app) as client:
        # No cookie → anonymous
        response = client.get("/test")
        assert response.status_code == 200
        assert response.json()["authenticated"] is False


def test_require_auth_requires_request_param():
    """require_auth raises ValueError if request parameter is missing."""
    import asyncio

    @require_auth
    async def bad_endpoint():  # Missing `request` parameter
        pass

    with pytest.raises(ValueError, match="require_auth decorator requires 'request' parameter"):
        asyncio.run(bad_endpoint())


# ── require_permission decorator ─────────────────────────────────────────────


def test_require_permission_requires_auth():
    """require_permission raises 401 when not authenticated."""
    from fastapi import Request

    app = FastAPI()

    @app.get("/test")
    @require_permission("threads", "read")
    async def endpoint(request: Request):
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/test")
        assert response.status_code == 401
        assert "Authentication required" in response.json()["detail"]


def test_require_permission_denies_wrong_permission():
    """User without required permission gets 403."""
    from fastapi import Request

    app = FastAPI()
    user = User(id=uuid4(), email="test@example.com", password_hash="hash")

    @app.get("/test")
    @require_permission("threads", "delete")
    async def endpoint(request: Request):
        return {"ok": True}

    mock_auth = AuthContext(user=user, permissions=[Permissions.THREADS_READ])

    with patch("app.gateway.authz._authenticate", return_value=mock_auth):
        with TestClient(app) as client:
            response = client.get("/test")
            assert response.status_code == 403
            assert "Permission denied" in response.json()["detail"]


# ── Weak JWT secret warning ──────────────────────────────────────────────────


# ── User Model Fields ──────────────────────────────────────────────────────


def test_user_model_has_needs_setup_default_false():
    """New users default to needs_setup=False."""
    user = User(email="test@example.com", password_hash="hash")
    assert user.needs_setup is False


def test_user_model_has_token_version_default_zero():
    """New users default to token_version=0."""
    user = User(email="test@example.com", password_hash="hash")
    assert user.token_version == 0


def test_user_model_needs_setup_true():
    """Auto-created admin has needs_setup=True."""
    user = User(email="admin@example.com", password_hash="hash", needs_setup=True)
    assert user.needs_setup is True


def test_sqlite_round_trip_new_fields():
    """needs_setup and token_version survive create → read round-trip."""
    import asyncio
    import os
    import tempfile
    from pathlib import Path

    from app.gateway.auth.repositories import sqlite as sqlite_mod

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_users.db")
        old_path = sqlite_mod._resolved_db_path
        old_init = sqlite_mod._table_initialized
        sqlite_mod._resolved_db_path = Path(db_path)
        sqlite_mod._table_initialized = False
        try:
            repo = sqlite_mod.SQLiteUserRepository()
            user = User(
                email="setup@test.com",
                password_hash="fakehash",
                system_role="admin",
                needs_setup=True,
                token_version=3,
            )
            created = asyncio.run(repo.create_user(user))
            assert created.needs_setup is True
            assert created.token_version == 3

            fetched = asyncio.run(repo.get_user_by_email("setup@test.com"))
            assert fetched is not None
            assert fetched.needs_setup is True
            assert fetched.token_version == 3

            fetched.needs_setup = False
            fetched.token_version = 4
            asyncio.run(repo.update_user(fetched))
            refetched = asyncio.run(repo.get_user_by_id(str(fetched.id)))
            assert refetched.needs_setup is False
            assert refetched.token_version == 4
        finally:
            sqlite_mod._resolved_db_path = old_path
            sqlite_mod._table_initialized = old_init


# ── Token Versioning ───────────────────────────────────────────────────────


def test_jwt_encodes_ver():
    """JWT payload includes ver field."""
    import os

    from app.gateway.auth.errors import TokenError

    os.environ["AUTH_JWT_SECRET"] = "test-secret-key-for-jwt-testing-minimum-32-chars"
    token = create_access_token(str(uuid4()), token_version=3)
    payload = decode_token(token)
    assert not isinstance(payload, TokenError)
    assert payload.ver == 3


def test_jwt_default_ver_zero():
    """JWT ver defaults to 0."""
    import os

    from app.gateway.auth.errors import TokenError

    os.environ["AUTH_JWT_SECRET"] = "test-secret-key-for-jwt-testing-minimum-32-chars"
    token = create_access_token(str(uuid4()))
    payload = decode_token(token)
    assert not isinstance(payload, TokenError)
    assert payload.ver == 0


def test_token_version_mismatch_rejects():
    """Token with stale ver is rejected by get_current_user_from_request."""
    import asyncio
    import os

    os.environ["AUTH_JWT_SECRET"] = "test-secret-key-for-jwt-testing-minimum-32-chars"

    user_id = str(uuid4())
    token = create_access_token(user_id, token_version=0)

    mock_user = User(id=user_id, email="test@example.com", password_hash="hash", token_version=1)

    mock_request = MagicMock()
    mock_request.cookies = {"access_token": token}

    with patch("app.gateway.deps.get_local_provider") as mock_provider_fn:
        mock_provider = MagicMock()
        mock_provider.get_user = AsyncMock(return_value=mock_user)
        mock_provider_fn.return_value = mock_provider

        from app.gateway.deps import get_current_user_from_request

        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(get_current_user_from_request(mock_request))
        assert exc_info.value.status_code == 401
        assert "revoked" in str(exc_info.value.detail).lower()


# ── change-password extension ──────────────────────────────────────────────


def test_change_password_request_accepts_new_email():
    """ChangePasswordRequest model accepts optional new_email."""
    from app.gateway.routers.auth import ChangePasswordRequest

    req = ChangePasswordRequest(
        current_password="old",
        new_password="newpassword",
        new_email="new@example.com",
    )
    assert req.new_email == "new@example.com"


def test_change_password_request_new_email_optional():
    """ChangePasswordRequest model works without new_email."""
    from app.gateway.routers.auth import ChangePasswordRequest

    req = ChangePasswordRequest(current_password="old", new_password="newpassword")
    assert req.new_email is None


def test_login_response_includes_needs_setup():
    """LoginResponse includes needs_setup field."""
    from app.gateway.routers.auth import LoginResponse

    resp = LoginResponse(expires_in=3600, needs_setup=True)
    assert resp.needs_setup is True
    resp2 = LoginResponse(expires_in=3600)
    assert resp2.needs_setup is False


# ── Rate Limiting ──────────────────────────────────────────────────────────


def test_rate_limiter_allows_under_limit():
    """Requests under the limit are allowed."""
    from app.gateway.routers.auth import _check_rate_limit, _login_attempts

    _login_attempts.clear()
    _check_rate_limit("192.168.1.1")  # Should not raise


def test_rate_limiter_blocks_after_max_failures():
    """IP is blocked after 5 consecutive failures."""
    from app.gateway.routers.auth import _check_rate_limit, _login_attempts, _record_login_failure

    _login_attempts.clear()
    ip = "10.0.0.1"
    for _ in range(5):
        _record_login_failure(ip)
    with pytest.raises(HTTPException) as exc_info:
        _check_rate_limit(ip)
    assert exc_info.value.status_code == 429


def test_rate_limiter_resets_on_success():
    """Successful login clears the failure counter."""
    from app.gateway.routers.auth import _check_rate_limit, _login_attempts, _record_login_failure, _record_login_success

    _login_attempts.clear()
    ip = "10.0.0.2"
    for _ in range(4):
        _record_login_failure(ip)
    _record_login_success(ip)
    _check_rate_limit(ip)  # Should not raise


# ── Client IP extraction ─────────────────────────────────────────────────


def test_get_client_ip_direct_connection():
    """Without nginx (no X-Real-IP), falls back to request.client.host."""
    from app.gateway.routers.auth import _get_client_ip

    req = MagicMock()
    req.client.host = "203.0.113.42"
    req.headers = {}
    assert _get_client_ip(req) == "203.0.113.42"


def test_get_client_ip_uses_x_real_ip():
    """X-Real-IP (set by nginx) is used when present."""
    from app.gateway.routers.auth import _get_client_ip

    req = MagicMock()
    req.client.host = "10.0.0.1"  # uvicorn may have replaced this with XFF[0]
    req.headers = {"x-real-ip": "203.0.113.42"}
    assert _get_client_ip(req) == "203.0.113.42"


def test_get_client_ip_xff_ignored():
    """X-Forwarded-For is never used; only X-Real-IP matters."""
    from app.gateway.routers.auth import _get_client_ip

    req = MagicMock()
    req.client.host = "10.0.0.1"
    req.headers = {"x-forwarded-for": "10.0.0.1, 198.51.100.5", "x-real-ip": "198.51.100.5"}
    assert _get_client_ip(req) == "198.51.100.5"


def test_get_client_ip_no_real_ip_fallback():
    """No X-Real-IP → falls back to client.host (direct connection)."""
    from app.gateway.routers.auth import _get_client_ip

    req = MagicMock()
    req.client.host = "127.0.0.1"
    req.headers = {}
    assert _get_client_ip(req) == "127.0.0.1"


def test_get_client_ip_x_real_ip_always_preferred():
    """X-Real-IP is always preferred over client.host regardless of IP."""
    from app.gateway.routers.auth import _get_client_ip

    req = MagicMock()
    req.client.host = "203.0.113.99"
    req.headers = {"x-real-ip": "198.51.100.7"}
    assert _get_client_ip(req) == "198.51.100.7"


# ── Weak JWT secret warning ──────────────────────────────────────────────────


def test_missing_jwt_secret_generates_ephemeral(monkeypatch, caplog):
    """get_auth_config() auto-generates an ephemeral secret when AUTH_JWT_SECRET is unset."""
    import logging

    import app.gateway.auth.config as config_module

    config_module._auth_config = None
    monkeypatch.delenv("AUTH_JWT_SECRET", raising=False)

    with caplog.at_level(logging.WARNING):
        config = config_module.get_auth_config()

    assert config.jwt_secret  # non-empty ephemeral secret
    assert any("AUTH_JWT_SECRET" in msg for msg in caplog.messages)

    # Cleanup
    config_module._auth_config = None
