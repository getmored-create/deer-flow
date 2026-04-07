"""Global authentication middleware — fail-closed safety net.

Rejects unauthenticated requests to non-public paths with 401.
Fine-grained permission checks remain in authz.py decorators.
"""

from collections.abc import Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse
from starlette.types import ASGIApp

from app.gateway.auth.errors import AuthErrorCode

# Paths that never require authentication.
_PUBLIC_PATH_PREFIXES: tuple[str, ...] = (
    "/health",
    "/docs",
    "/redoc",
    "/openapi.json",
)

# Exact auth paths that are public (login/register/status check).
# /api/v1/auth/me, /api/v1/auth/change-password etc. are NOT public.
_PUBLIC_EXACT_PATHS: frozenset[str] = frozenset(
    {
        "/api/v1/auth/login/local",
        "/api/v1/auth/register",
        "/api/v1/auth/logout",
        "/api/v1/auth/setup-status",
    }
)


def _is_public(path: str) -> bool:
    stripped = path.rstrip("/")
    if stripped in _PUBLIC_EXACT_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in _PUBLIC_PATH_PREFIXES)


class AuthMiddleware(BaseHTTPMiddleware):
    """Coarse-grained auth gate: reject requests without a valid session cookie.

    This does NOT verify JWT signature or user existence — that is the job of
    ``get_current_user_from_request`` in deps.py (called by ``@require_auth``).
    The middleware only checks *presence* of the cookie so that new endpoints
    that forget ``@require_auth`` are not completely exposed.
    """

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if _is_public(request.url.path):
            return await call_next(request)

        # Non-public path: require session cookie
        if not request.cookies.get("access_token"):
            return JSONResponse(
                status_code=401,
                content={
                    "detail": {
                        "code": AuthErrorCode.NOT_AUTHENTICATED,
                        "message": "Authentication required",
                    }
                },
            )

        return await call_next(request)
