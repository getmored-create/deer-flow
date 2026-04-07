"""Authentication endpoints."""

import logging
import time

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel, EmailStr, Field

from app.gateway.auth import (
    UserResponse,
    create_access_token,
)
from app.gateway.auth.config import get_auth_config
from app.gateway.auth.errors import AuthErrorCode, AuthErrorResponse
from app.gateway.csrf_middleware import is_secure_request
from app.gateway.deps import get_current_user_from_request, get_local_provider

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


# ── Request/Response Models ──────────────────────────────────────────────


class LoginResponse(BaseModel):
    """Response model for login — token only lives in HttpOnly cookie."""

    expires_in: int  # seconds
    needs_setup: bool = False


class RegisterRequest(BaseModel):
    """Request model for user registration."""

    email: EmailStr
    password: str = Field(..., min_length=8)


class ChangePasswordRequest(BaseModel):
    """Request model for password change (also handles setup flow)."""

    current_password: str
    new_password: str = Field(..., min_length=8)
    new_email: EmailStr | None = None


class MessageResponse(BaseModel):
    """Generic message response."""

    message: str


# ── Helpers ───────────────────────────────────────────────────────────────


def _set_session_cookie(response: Response, token: str, request: Request) -> None:
    """Set the access_token HttpOnly cookie on the response."""
    config = get_auth_config()
    is_https = is_secure_request(request)
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        secure=is_https,
        samesite="lax",
        max_age=config.token_expiry_days * 24 * 3600 if is_https else None,
    )


# ── Rate Limiting ────────────────────────────────────────────────────────
# In-process dict — not shared across workers. Sufficient for single-worker deployments.

_MAX_LOGIN_ATTEMPTS = 5
_LOCKOUT_SECONDS = 300  # 5 minutes

# ip → (fail_count, lock_until_timestamp)
_login_attempts: dict[str, tuple[int, float]] = {}


def _get_client_ip(request: Request) -> str:
    """Extract the real client IP for rate limiting.

    Uses ``X-Real-IP`` header set by nginx (``proxy_set_header X-Real-IP
    $remote_addr``).  Nginx unconditionally overwrites any client-supplied
    ``X-Real-IP``, so the value seen by Gateway is always the TCP peer IP
    that nginx observed — it cannot be spoofed by the client.

    ``request.client.host`` is NOT reliable because uvicorn's default
    ``proxy_headers=True`` replaces it with the *first* entry from
    ``X-Forwarded-For``, which IS client-spoofable.

    ``X-Forwarded-For`` is intentionally NOT used for the same reason.
    """
    real_ip = request.headers.get("x-real-ip", "").strip()
    if real_ip:
        return real_ip

    # Fallback: direct connection without nginx (e.g. unit tests, dev).
    return request.client.host if request.client else "unknown"


def _check_rate_limit(ip: str) -> None:
    """Raise 429 if the IP is currently locked out."""
    record = _login_attempts.get(ip)
    if record is None:
        return
    fail_count, lock_until = record
    if fail_count >= _MAX_LOGIN_ATTEMPTS:
        if time.time() < lock_until:
            raise HTTPException(
                status_code=429,
                detail="Too many login attempts. Try again later.",
            )
        del _login_attempts[ip]


_MAX_TRACKED_IPS = 10000


def _record_login_failure(ip: str) -> None:
    """Record a failed login attempt for the given IP."""
    # Evict expired lockouts when dict grows too large
    if len(_login_attempts) >= _MAX_TRACKED_IPS:
        now = time.time()
        expired = [k for k, (c, t) in _login_attempts.items() if c >= _MAX_LOGIN_ATTEMPTS and now >= t]
        for k in expired:
            del _login_attempts[k]
        # If still too large, evict cheapest-to-lose half: below-threshold
        # IPs (lock_until=0.0) sort first, then earliest-expiring lockouts.
        if len(_login_attempts) >= _MAX_TRACKED_IPS:
            by_time = sorted(_login_attempts.items(), key=lambda kv: kv[1][1])
            for k, _ in by_time[: len(by_time) // 2]:
                del _login_attempts[k]

    record = _login_attempts.get(ip)
    if record is None:
        _login_attempts[ip] = (1, 0.0)
    else:
        new_count = record[0] + 1
        lock_until = time.time() + _LOCKOUT_SECONDS if new_count >= _MAX_LOGIN_ATTEMPTS else 0.0
        _login_attempts[ip] = (new_count, lock_until)


def _record_login_success(ip: str) -> None:
    """Clear failure counter for the given IP on successful login."""
    _login_attempts.pop(ip, None)


# ── Endpoints ─────────────────────────────────────────────────────────────


@router.post("/login/local", response_model=LoginResponse)
async def login_local(
    request: Request,
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
):
    """Local email/password login."""
    client_ip = _get_client_ip(request)
    _check_rate_limit(client_ip)

    user = await get_local_provider().authenticate({"email": form_data.username, "password": form_data.password})

    if user is None:
        _record_login_failure(client_ip)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=AuthErrorResponse(code=AuthErrorCode.INVALID_CREDENTIALS, message="Incorrect email or password").model_dump(),
        )

    _record_login_success(client_ip)
    token = create_access_token(str(user.id), token_version=user.token_version)
    _set_session_cookie(response, token, request)

    return LoginResponse(
        expires_in=get_auth_config().token_expiry_days * 24 * 3600,
        needs_setup=user.needs_setup,
    )


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(request: Request, response: Response, body: RegisterRequest):
    """Register a new user account (always 'user' role).

    Admin is auto-created on first boot. This endpoint creates regular users.
    Auto-login by setting the session cookie.
    """
    try:
        user = await get_local_provider().create_user(email=body.email, password=body.password, system_role="user")
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=AuthErrorResponse(code=AuthErrorCode.EMAIL_ALREADY_EXISTS, message="Email already registered").model_dump(),
        )

    token = create_access_token(str(user.id), token_version=user.token_version)
    _set_session_cookie(response, token, request)

    return UserResponse(id=str(user.id), email=user.email, system_role=user.system_role)


@router.post("/logout", response_model=MessageResponse)
async def logout(request: Request, response: Response):
    """Logout current user by clearing the cookie."""
    response.delete_cookie(key="access_token", secure=is_secure_request(request), samesite="lax")
    return MessageResponse(message="Successfully logged out")


@router.post("/change-password", response_model=MessageResponse)
async def change_password(request: Request, response: Response, body: ChangePasswordRequest):
    """Change password for the currently authenticated user.

    Also handles the first-boot setup flow:
    - If new_email is provided, updates email (checks uniqueness)
    - If user.needs_setup is True and new_email is given, clears needs_setup
    - Always increments token_version to invalidate old sessions
    - Re-issues session cookie with new token_version
    """
    from app.gateway.auth.password import hash_password_async, verify_password_async

    user = await get_current_user_from_request(request)

    if user.password_hash is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=AuthErrorResponse(code=AuthErrorCode.INVALID_CREDENTIALS, message="OAuth users cannot change password").model_dump())

    if not await verify_password_async(body.current_password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=AuthErrorResponse(code=AuthErrorCode.INVALID_CREDENTIALS, message="Current password is incorrect").model_dump())

    provider = get_local_provider()

    # Update email if provided
    if body.new_email is not None:
        existing = await provider.get_user_by_email(body.new_email)
        if existing and str(existing.id) != str(user.id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=AuthErrorResponse(code=AuthErrorCode.EMAIL_ALREADY_EXISTS, message="Email already in use").model_dump())
        user.email = body.new_email

    # Update password + bump version
    user.password_hash = await hash_password_async(body.new_password)
    user.token_version += 1

    # Clear setup flag if this is the setup flow
    if user.needs_setup and body.new_email is not None:
        user.needs_setup = False

    await provider.update_user(user)

    # Re-issue cookie with new token_version
    token = create_access_token(str(user.id), token_version=user.token_version)
    _set_session_cookie(response, token, request)

    return MessageResponse(message="Password changed successfully")


@router.get("/me", response_model=UserResponse)
async def get_me(request: Request):
    """Get current authenticated user info."""
    user = await get_current_user_from_request(request)
    return UserResponse(id=str(user.id), email=user.email, system_role=user.system_role, needs_setup=user.needs_setup)


@router.get("/setup-status")
async def setup_status():
    """Check if admin account exists. Always False after first boot."""
    user_count = await get_local_provider().count_users()
    return {"needs_setup": user_count == 0}


# ── OAuth Endpoints (Future/Placeholder) ─────────────────────────────────


@router.get("/oauth/{provider}")
async def oauth_login(provider: str):
    """Initiate OAuth login flow.

    Redirects to the OAuth provider's authorization URL.
    Currently a placeholder - requires OAuth provider implementation.
    """
    if provider not in ["github", "google"]:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported OAuth provider: {provider}",
        )

    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="OAuth login not yet implemented",
    )


@router.get("/callback/{provider}")
async def oauth_callback(provider: str, code: str, state: str):
    """OAuth callback endpoint.

    Handles the OAuth provider's callback after user authorization.
    Currently a placeholder.
    """
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail="OAuth callback not yet implemented",
    )
