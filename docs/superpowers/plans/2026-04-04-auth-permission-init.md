# Auth Permission Initialization — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the remaining auth module features defined in `docs/superpowers/specs/2026-04-04-auth-module-design.md` — User model fields, token versioning, setup flow, rate limiting, thread migration.

**Architecture:** Incremental backend-first approach. Each task adds one isolated feature with tests. DB schema changes come first (foundation), then JWT/auth logic, then API extensions, then frontend. Every backend change has a matching test.

**Tech Stack:** Python 3.12, FastAPI, SQLite, PyJWT, bcrypt, Next.js 16, React 19, TypeScript

---

## File Map

| File | Responsibility | Tasks |
|------|---------------|-------|
| `backend/app/gateway/auth/models.py` | User model — add `needs_setup`, `token_version` | 1 |
| `backend/app/gateway/auth/repositories/sqlite.py` | DDL + ALTER TABLE + WAL + updated queries | 1 |
| `backend/app/gateway/auth/jwt.py` | JWT payload — add `ver` field | 2 |
| `backend/app/gateway/deps.py` | Token version check on decode | 2 |
| `backend/app/gateway/routers/auth.py` | change-password extension, rate limiting, login `needs_setup` | 3, 4 |
| `backend/app/gateway/app.py` | Thread migration in `_ensure_admin_user`, `needs_setup` logging | 5 |
| `backend/app/gateway/auth/reset_admin.py` | Set `needs_setup=True` + `token_version++` | 5 |
| `frontend/src/core/auth/types.ts` | AuthResult add `needs_setup` tag | 6 |
| `frontend/src/core/auth/server.ts` | SSR guard — detect `needs_setup` | 6 |
| `frontend/src/app/workspace/layout.tsx` | Redirect to `/setup` | 6 |
| `frontend/src/app/(auth)/setup/page.tsx` | New setup page | 6 |
| `backend/tests/test_auth.py` | Tests for all backend changes | 1–5 |

---

### Task 1: User Model + DB Schema — `needs_setup` and `token_version`

**Files:**
- Modify: `backend/app/gateway/auth/models.py:15-28`
- Modify: `backend/app/gateway/auth/repositories/sqlite.py:41-64,81-112,140-151,178-189`
- Test: `backend/tests/test_auth.py`

- [ ] **Step 1: Write failing test — User model has new fields**

Add to `backend/tests/test_auth.py` after the password hashing section:

```python
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
    user = User(email="admin@localhost", password_hash="hash", needs_setup=True)
    assert user.needs_setup is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_auth.py::test_user_model_has_needs_setup_default_false tests/test_auth.py::test_user_model_has_token_version_default_zero tests/test_auth.py::test_user_model_needs_setup_true -v`

Expected: FAIL — `User.__init__()` got unexpected keyword argument `needs_setup`

- [ ] **Step 3: Add fields to User model**

In `backend/app/gateway/auth/models.py`, add two fields to the `User` class after `oauth_id`:

```python
class User(BaseModel):
    """Internal user representation."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID = Field(default_factory=uuid4, description="Primary key")
    email: EmailStr = Field(..., description="Unique email address")
    password_hash: str | None = Field(None, description="bcrypt hash, nullable for OAuth users")
    system_role: Literal["admin", "user"] = Field(default="user")
    created_at: datetime = Field(default_factory=_utc_now)

    # OAuth linkage (optional)
    oauth_provider: str | None = Field(None, description="e.g. 'github', 'google'")
    oauth_id: str | None = Field(None, description="User ID from OAuth provider")

    # Auth lifecycle
    needs_setup: bool = Field(default=False, description="True for auto-created admin until setup completes")
    token_version: int = Field(default=0, description="Incremented on password change to invalidate old JWTs")
```

- [ ] **Step 4: Run model tests to verify they pass**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_auth.py::test_user_model_has_needs_setup_default_false tests/test_auth.py::test_user_model_has_token_version_default_zero tests/test_auth.py::test_user_model_needs_setup_true -v`

Expected: PASS

- [ ] **Step 5: Update SQLite DDL — CREATE TABLE + ALTER TABLE migration + WAL**

In `backend/app/gateway/auth/repositories/sqlite.py`:

1. Add WAL mode to `_get_connection()`:

```python
def _get_connection() -> sqlite3.Connection:
    """Get a SQLite connection for the users database."""
    db_path = _get_users_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn
```

2. Update `_init_users_table()` to add new columns in CREATE TABLE and ALTER TABLE for existing DBs:

```python
def _init_users_table(conn: sqlite3.Connection) -> None:
    """Initialize the users table if it doesn't exist."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id TEXT PRIMARY KEY,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT,
            system_role TEXT NOT NULL DEFAULT 'user',
            created_at REAL NOT NULL,
            oauth_provider TEXT,
            oauth_id TEXT,
            needs_setup INTEGER NOT NULL DEFAULT 0,
            token_version INTEGER NOT NULL DEFAULT 0
        )
    """
    )
    # Add unique constraint for OAuth identity to prevent duplicate social logins
    conn.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_users_oauth_identity
        ON users(oauth_provider, oauth_id)
        WHERE oauth_provider IS NOT NULL AND oauth_id IS NOT NULL
    """
    )
    # Migrate existing databases: add new columns if missing
    for col, default in [("needs_setup", "0"), ("token_version", "0")]:
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} INTEGER NOT NULL DEFAULT {default}")
        except sqlite3.OperationalError:
            pass  # Column already exists
    conn.commit()
```

3. Update `_create_user_sync()` to include new fields:

```python
    def _create_user_sync(self, user: User) -> User:
        """Synchronous user creation (runs in thread pool)."""
        with _get_users_conn() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO users (id, email, password_hash, system_role, created_at,
                                       oauth_provider, oauth_id, needs_setup, token_version)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(user.id),
                        user.email,
                        user.password_hash,
                        user.system_role,
                        datetime.now(UTC).timestamp(),
                        user.oauth_provider,
                        user.oauth_id,
                        int(user.needs_setup),
                        user.token_version,
                    ),
                )
                conn.commit()
            except sqlite3.IntegrityError as e:
                if "UNIQUE constraint failed: users.email" in str(e):
                    raise ValueError(f"Email already registered: {user.email}") from e
                raise
        return user
```

4. Update `_update_user_sync()` to include new fields:

```python
    def _update_user_sync(self, user: User) -> User:
        with _get_users_conn() as conn:
            conn.execute(
                """UPDATE users SET email = ?, password_hash = ?, system_role = ?,
                   oauth_provider = ?, oauth_id = ?, needs_setup = ?, token_version = ?
                   WHERE id = ?""",
                (user.email, user.password_hash, user.system_role,
                 user.oauth_provider, user.oauth_id,
                 int(user.needs_setup), user.token_version, str(user.id)),
            )
            conn.commit()
        return user
```

5. Update `_row_to_user()` to read new fields:

```python
    @staticmethod
    def _row_to_user(row: dict[str, Any]) -> User:
        """Convert a database row to a User model."""
        return User(
            id=UUID(row["id"]),
            email=row["email"],
            password_hash=row["password_hash"],
            system_role=row["system_role"],
            created_at=datetime.fromtimestamp(row["created_at"], tz=UTC),
            oauth_provider=row.get("oauth_provider"),
            oauth_id=row.get("oauth_id"),
            needs_setup=bool(row.get("needs_setup", 0)),
            token_version=int(row.get("token_version", 0)),
        )
```

- [ ] **Step 6: Write DB round-trip test**

Add to `backend/tests/test_auth.py`:

```python
import asyncio
import tempfile
import os

def test_sqlite_round_trip_new_fields():
    """needs_setup and token_version survive create → read round-trip."""
    from app.gateway.auth.repositories import sqlite as sqlite_mod

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "test_users.db")
        # Patch the DB path
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

            # Update
            fetched.needs_setup = False
            fetched.token_version = 4
            asyncio.run(repo.update_user(fetched))
            refetched = asyncio.run(repo.get_user_by_id(str(fetched.id)))
            assert refetched.needs_setup is False
            assert refetched.token_version == 4
        finally:
            sqlite_mod._resolved_db_path = old_path
            sqlite_mod._table_initialized = old_init
```

Add this import at the top of the test file if not present: `from pathlib import Path`

- [ ] **Step 7: Run all Task 1 tests**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_auth.py::test_user_model_has_needs_setup_default_false tests/test_auth.py::test_user_model_has_token_version_default_zero tests/test_auth.py::test_user_model_needs_setup_true tests/test_auth.py::test_sqlite_round_trip_new_fields -v`

Expected: PASS (all 4)

- [ ] **Step 8: Commit**

```bash
git add backend/app/gateway/auth/models.py backend/app/gateway/auth/repositories/sqlite.py backend/tests/test_auth.py
git commit -m "feat(auth): add needs_setup and token_version to User model + SQLite schema"
```

---

### Task 2: Token Invalidation — JWT `ver` field + deps check

**Files:**
- Modify: `backend/app/gateway/auth/jwt.py:12-35`
- Modify: `backend/app/gateway/deps.py:80-110`
- Test: `backend/tests/test_auth.py`

- [ ] **Step 1: Write failing test — JWT encodes ver**

Add to `backend/tests/test_auth.py`:

```python
# ── Token Versioning ───────────────────────────────────────────────────────

def test_jwt_encodes_ver():
    """JWT payload includes ver field."""
    import os
    os.environ["AUTH_JWT_SECRET"] = "test-secret-key-for-jwt-testing-minimum-32-chars"
    token = create_access_token(str(uuid4()), token_version=3)
    payload = decode_token(token)
    assert not isinstance(payload, TokenError)
    assert payload.ver == 3

def test_jwt_default_ver_zero():
    """JWT ver defaults to 0."""
    import os
    os.environ["AUTH_JWT_SECRET"] = "test-secret-key-for-jwt-testing-minimum-32-chars"
    token = create_access_token(str(uuid4()))
    payload = decode_token(token)
    assert not isinstance(payload, TokenError)
    assert payload.ver == 0
```

Add `TokenError` to the existing import at line 11 of test_auth.py:

```python
from app.gateway.auth import create_access_token, decode_token, hash_password, verify_password
from app.gateway.auth.errors import TokenError
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_auth.py::test_jwt_encodes_ver tests/test_auth.py::test_jwt_default_ver_zero -v`

Expected: FAIL — `create_access_token() got unexpected keyword argument 'token_version'`

- [ ] **Step 3: Update JWT module**

In `backend/app/gateway/auth/jwt.py`:

1. Add `ver` to `TokenPayload`:

```python
class TokenPayload(BaseModel):
    """JWT token payload."""

    sub: str  # user_id
    exp: datetime
    iat: datetime | None = None
    ver: int = 0  # token_version — must match User.token_version
```

2. Update `create_access_token()` to accept and encode `token_version`:

```python
def create_access_token(user_id: str, expires_delta: timedelta | None = None, token_version: int = 0) -> str:
    """Create a JWT access token.

    Args:
        user_id: The user's UUID as string
        expires_delta: Optional custom expiry, defaults to 7 days
        token_version: User's current token_version for invalidation

    Returns:
        Encoded JWT string
    """
    config = get_auth_config()
    expiry = expires_delta or timedelta(days=config.token_expiry_days)

    now = datetime.now(UTC)
    payload = {"sub": user_id, "exp": now + expiry, "iat": now, "ver": token_version}
    return jwt.encode(payload, config.jwt_secret, algorithm="HS256")
```

- [ ] **Step 4: Run JWT tests to verify they pass**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_auth.py::test_jwt_encodes_ver tests/test_auth.py::test_jwt_default_ver_zero -v`

Expected: PASS

- [ ] **Step 5: Update deps.py — check token_version on decode**

In `backend/app/gateway/deps.py`, modify `get_current_user_from_request()` to compare versions. After the line `user = await provider.get_user(payload.sub)` and the null check, add:

```python
async def get_current_user_from_request(request: Request):
    """Get the current authenticated user from the request cookie.

    Raises HTTPException 401 if not authenticated.
    """
    from app.gateway.auth import decode_token
    from app.gateway.auth.errors import AuthErrorCode, AuthErrorResponse, TokenError, token_error_to_code

    access_token = request.cookies.get("access_token")
    if not access_token:
        raise HTTPException(
            status_code=401,
            detail=AuthErrorResponse(code=AuthErrorCode.NOT_AUTHENTICATED, message="Not authenticated").model_dump(),
        )

    payload = decode_token(access_token)
    if isinstance(payload, TokenError):
        raise HTTPException(
            status_code=401,
            detail=AuthErrorResponse(code=token_error_to_code(payload), message=f"Token error: {payload.value}").model_dump(),
        )

    provider = get_local_provider()
    user = await provider.get_user(payload.sub)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail=AuthErrorResponse(code=AuthErrorCode.USER_NOT_FOUND, message="User not found").model_dump(),
        )

    # Token version mismatch → password was changed, token is stale
    if user.token_version != payload.ver:
        raise HTTPException(
            status_code=401,
            detail=AuthErrorResponse(code=AuthErrorCode.TOKEN_INVALID, message="Token revoked (password changed)").model_dump(),
        )

    return user
```

- [ ] **Step 6: Update all create_access_token call sites to pass token_version**

In `backend/app/gateway/routers/auth.py`, update the two call sites:

Login (line 95):
```python
    token = create_access_token(str(user.id), token_version=user.token_version)
```

Register (line 116):
```python
    token = create_access_token(str(user.id), token_version=user.token_version)
```

- [ ] **Step 7: Write test for token version mismatch rejection**

Add to `backend/tests/test_auth.py`:

```python
@pytest.mark.asyncio
async def test_token_version_mismatch_rejects():
    """Token with stale ver is rejected by get_current_user_from_request."""
    import os
    os.environ["AUTH_JWT_SECRET"] = "test-secret-key-for-jwt-testing-minimum-32-chars"

    user_id = str(uuid4())
    # Create token with ver=0
    token = create_access_token(user_id, token_version=0)

    # Mock user with token_version=1 (password was changed)
    mock_user = User(id=user_id, email="test@test.com", password_hash="hash", token_version=1)

    mock_request = MagicMock()
    mock_request.cookies = {"access_token": token}

    with patch("app.gateway.deps.get_local_provider") as mock_provider_fn:
        mock_provider = MagicMock()
        mock_provider.get_user = MagicMock(return_value=mock_user)
        mock_provider_fn.return_value = mock_provider

        from app.gateway.deps import get_current_user_from_request
        with pytest.raises(HTTPException) as exc_info:
            await get_current_user_from_request(mock_request)
        assert exc_info.value.status_code == 401
        assert "revoked" in str(exc_info.value.detail).lower()
```

- [ ] **Step 8: Run all Task 2 tests**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_auth.py::test_jwt_encodes_ver tests/test_auth.py::test_jwt_default_ver_zero tests/test_auth.py::test_token_version_mismatch_rejects -v`

Expected: PASS (all 3)

- [ ] **Step 9: Commit**

```bash
git add backend/app/gateway/auth/jwt.py backend/app/gateway/deps.py backend/app/gateway/routers/auth.py backend/tests/test_auth.py
git commit -m "feat(auth): add token versioning — JWT ver field + stale token rejection"
```

---

### Task 3: change-password Extension — `new_email` + `needs_setup` + `token_version`

**Files:**
- Modify: `backend/app/gateway/routers/auth.py:38-42,129-146`
- Test: `backend/tests/test_auth.py`

- [ ] **Step 1: Write failing tests**

Add to `backend/tests/test_auth.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_auth.py::test_change_password_request_accepts_new_email tests/test_auth.py::test_change_password_request_new_email_optional -v`

Expected: FAIL — unexpected keyword argument `new_email`

- [ ] **Step 3: Update ChangePasswordRequest model**

In `backend/app/gateway/routers/auth.py`, update the model:

```python
class ChangePasswordRequest(BaseModel):
    """Request model for password change (also handles setup flow)."""

    current_password: str
    new_password: str = Field(..., min_length=8)
    new_email: EmailStr | None = None
```

- [ ] **Step 4: Update change_password endpoint**

Replace the `change_password` function in `backend/app/gateway/routers/auth.py`:

```python
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

    # Update email if provided
    if body.new_email is not None:
        provider = get_local_provider()
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

    await get_local_provider().update_user(user)

    # Re-issue cookie with new token_version
    token = create_access_token(str(user.id), token_version=user.token_version)
    _set_session_cookie(response, token, request)

    return MessageResponse(message="Password changed successfully")
```

Note: add `Response` to the function signature (import already exists).

- [ ] **Step 5: Update LoginResponse to include needs_setup**

In `backend/app/gateway/routers/auth.py`, update the response model and the login endpoint:

```python
class LoginResponse(BaseModel):
    """Response model for login — token only lives in HttpOnly cookie."""

    expires_in: int  # seconds
    needs_setup: bool = False
```

Update the login endpoint return:

```python
    return LoginResponse(
        expires_in=get_auth_config().token_expiry_days * 24 * 3600,
        needs_setup=user.needs_setup,
    )
```

- [ ] **Step 6: Run model tests**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_auth.py::test_change_password_request_accepts_new_email tests/test_auth.py::test_change_password_request_new_email_optional -v`

Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add backend/app/gateway/routers/auth.py backend/tests/test_auth.py
git commit -m "feat(auth): extend change-password with new_email, token_version bump, and setup flow"
```

---

### Task 4: Login Rate Limiting

**Files:**
- Modify: `backend/app/gateway/routers/auth.py:76-98`
- Test: `backend/tests/test_auth.py`

- [ ] **Step 1: Write failing test**

Add to `backend/tests/test_auth.py`:

```python
# ── Rate Limiting ──────────────────────────────────────────────────────────

def test_rate_limiter_allows_under_limit():
    """Requests under the limit are allowed."""
    from app.gateway.routers.auth import _check_rate_limit, _login_attempts
    _login_attempts.clear()
    # Should not raise
    _check_rate_limit("192.168.1.1")

def test_rate_limiter_blocks_after_max_failures():
    """IP is blocked after 5 consecutive failures."""
    import time
    from app.gateway.routers.auth import _record_login_failure, _check_rate_limit, _login_attempts
    _login_attempts.clear()
    ip = "10.0.0.1"
    for _ in range(5):
        _record_login_failure(ip)
    with pytest.raises(HTTPException) as exc_info:
        _check_rate_limit(ip)
    assert exc_info.value.status_code == 429

def test_rate_limiter_resets_on_success():
    """Successful login clears the failure counter."""
    from app.gateway.routers.auth import _record_login_failure, _record_login_success, _check_rate_limit, _login_attempts
    _login_attempts.clear()
    ip = "10.0.0.2"
    for _ in range(4):
        _record_login_failure(ip)
    _record_login_success(ip)
    # Should not raise — counter was reset
    _check_rate_limit(ip)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_auth.py::test_rate_limiter_allows_under_limit tests/test_auth.py::test_rate_limiter_blocks_after_max_failures tests/test_auth.py::test_rate_limiter_resets_on_success -v`

Expected: FAIL — cannot import `_check_rate_limit`

- [ ] **Step 3: Implement rate limiting**

Add the following to `backend/app/gateway/routers/auth.py` after the `_set_session_cookie` helper (before endpoints):

```python
# ── Rate Limiting ────────────────────────────────────────────────────────

import time

_MAX_LOGIN_ATTEMPTS = 5
_LOCKOUT_SECONDS = 300  # 5 minutes

# ip → (fail_count, lock_until_timestamp)
_login_attempts: dict[str, tuple[int, float]] = {}


def _check_rate_limit(ip: str) -> None:
    """Raise 429 if the IP is currently locked out."""
    record = _login_attempts.get(ip)
    if record is None:
        return
    fail_count, lock_until = record
    if fail_count >= _MAX_LOGIN_ATTEMPTS and time.time() < lock_until:
        raise HTTPException(
            status_code=429,
            detail="Too many login attempts. Try again later.",
        )
    # Lockout expired — clear
    if fail_count >= _MAX_LOGIN_ATTEMPTS and time.time() >= lock_until:
        del _login_attempts[ip]


def _record_login_failure(ip: str) -> None:
    """Record a failed login attempt for the given IP."""
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
```

- [ ] **Step 4: Wire rate limiting into login endpoint**

Update the `login_local` function to call rate limiting. Add at the start of the function body:

```python
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)
```

After the `if user is None:` block, add `_record_login_failure(client_ip)` before the raise. After a successful login (before `return`), add `_record_login_success(client_ip)`.

The full login function becomes:

```python
@router.post("/login/local", response_model=LoginResponse)
async def login_local(
    request: Request,
    response: Response,
    form_data: OAuth2PasswordRequestForm = Depends(),
):
    """Local email/password login."""
    client_ip = request.client.host if request.client else "unknown"
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
```

- [ ] **Step 5: Run rate limiting tests**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_auth.py::test_rate_limiter_allows_under_limit tests/test_auth.py::test_rate_limiter_blocks_after_max_failures tests/test_auth.py::test_rate_limiter_resets_on_success -v`

Expected: PASS (all 3)

- [ ] **Step 6: Commit**

```bash
git add backend/app/gateway/routers/auth.py backend/tests/test_auth.py
git commit -m "feat(auth): add IP-based login rate limiting (5 attempts, 5-min lockout)"
```

---

### Task 5: Thread Migration + `_ensure_admin_user` + `reset_admin` Updates

**Files:**
- Modify: `backend/app/gateway/app.py:40-61`
- Modify: `backend/app/gateway/auth/reset_admin.py:34-36`
- Test: `backend/tests/test_auth.py`

- [ ] **Step 1: Write failing test for admin creation with needs_setup**

Add to `backend/tests/test_auth.py`:

```python
# ── Admin Bootstrap ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ensure_admin_sets_needs_setup():
    """_ensure_admin_user creates admin with needs_setup=True."""
    from unittest.mock import AsyncMock, patch

    mock_provider = MagicMock()
    mock_provider.count_users = AsyncMock(return_value=0)

    created_user = None
    async def capture_create(email, password, system_role):
        nonlocal created_user
        created_user = User(email=email, password_hash="hash", system_role=system_role, needs_setup=True)
        return created_user
    mock_provider.create_user = capture_create

    mock_app = MagicMock()
    mock_app.state = MagicMock()
    mock_app.state.store = None  # No store — skip thread migration

    with patch("app.gateway.app.get_local_provider", return_value=mock_provider):
        from app.gateway.app import _ensure_admin_user
        await _ensure_admin_user(mock_app)

    assert created_user is not None
    assert created_user.needs_setup is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_auth.py::test_ensure_admin_sets_needs_setup -v`

Expected: FAIL — `_ensure_admin_user` doesn't pass `needs_setup=True`

- [ ] **Step 3: Update `_ensure_admin_user` in app.py**

Replace the function in `backend/app/gateway/app.py`:

```python
async def _ensure_admin_user(app: FastAPI) -> None:
    """Auto-create the admin user on first boot if no users exist.

    Prints the generated password to stdout so the operator can log in.
    On subsequent boots, warns if any user still needs setup.
    """
    import secrets

    from app.gateway.deps import get_local_provider

    provider = get_local_provider()
    user_count = await provider.count_users()

    if user_count == 0:
        password = secrets.token_urlsafe(16)
        admin = await provider.create_user(email="admin@localhost", password=password, system_role="admin")

        # Set needs_setup flag (create_user defaults to False, update it)
        admin.needs_setup = True
        await provider.update_user(admin)

        # Migrate orphaned threads (no user_id) to this admin
        store = getattr(app.state, "store", None)
        if store is not None:
            await _migrate_orphaned_threads(store, str(admin.id))

        logger.info("=" * 60)
        logger.info("  Admin account created on first boot")
        logger.info("  Email:    %s", admin.email)
        logger.info("  Password: %s", password)
        logger.info("  Change it after login: Settings -> Account")
        logger.info("=" * 60)
        return

    # Check for users that still need setup
    admin = await provider.get_user_by_email("admin@localhost")
    if admin and admin.needs_setup:
        logger.warning("Admin account still needs setup. Log in or use: python -m app.gateway.auth.reset_admin")


async def _migrate_orphaned_threads(store, admin_user_id: str) -> None:
    """Migrate threads with no user_id to the given admin."""
    try:
        migrated = 0
        results = await store.asearch(("threads",), limit=1000)
        for item in results:
            metadata = item.value.get("metadata", {}) if hasattr(item, "value") else {}
            if not metadata.get("user_id"):
                metadata["user_id"] = admin_user_id
                if hasattr(item, "value"):
                    item.value["metadata"] = metadata
                    await store.aput(("threads",), item.key, item.value)
                    migrated += 1
        if migrated:
            logger.info("Migrated %d orphaned thread(s) to admin", migrated)
    except Exception:
        logger.exception("Thread migration failed (non-fatal)")
```

- [ ] **Step 4: Update reset_admin.py**

Replace the password reset section in `backend/app/gateway/auth/reset_admin.py`:

```python
    new_password = secrets.token_urlsafe(16)
    user.password_hash = hash_password(new_password)
    user.token_version += 1
    user.needs_setup = True
    asyncio.run(repo.update_user(user))

    print(f"Password reset for: {user.email}")
    print(f"New password: {new_password}")
    print("Next login will require setup (new email + password).")
```

- [ ] **Step 5: Run admin bootstrap test**

Run: `cd backend && PYTHONPATH=. uv run pytest tests/test_auth.py::test_ensure_admin_sets_needs_setup -v`

Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/gateway/app.py backend/app/gateway/auth/reset_admin.py backend/tests/test_auth.py
git commit -m "feat(auth): thread migration on first boot + reset_admin sets needs_setup + token_version"
```

---

### Task 6: Frontend — Setup Page + SSR Guard

**Files:**
- Modify: `frontend/src/core/auth/types.ts:15-19`
- Modify: `frontend/src/core/auth/server.ts:36-43`
- Modify: `frontend/src/app/workspace/layout.tsx:17-55`
- Create: `frontend/src/app/(auth)/setup/page.tsx`

- [ ] **Step 1: Add `needs_setup` tag to AuthResult**

In `frontend/src/core/auth/types.ts`, update the AuthResult type:

```typescript
export type AuthResult =
  | { tag: "authenticated"; user: User }
  | { tag: "needs_setup"; user: User }
  | { tag: "unauthenticated" }
  | { tag: "gateway_unavailable" }
  | { tag: "config_error"; message: string };
```

- [ ] **Step 2: Update SSR guard to detect needs_setup**

In `frontend/src/core/auth/server.ts`, update the `getServerSideUser()` function. After the successful `res.ok` block where user is parsed, add the needs_setup check:

```typescript
    if (res.ok) {
      const parsed = userSchema.safeParse(await res.json());
      if (!parsed.success) {
        console.error("[SSR auth] Malformed /auth/me response:", parsed.error);
        return { tag: "gateway_unavailable" };
      }
      // Check if user needs initial setup
      if (parsed.data.needs_setup) {
        return { tag: "needs_setup", user: parsed.data };
      }
      return { tag: "authenticated", user: parsed.data };
    }
```

Also update `userSchema` in `types.ts` to include `needs_setup`:

```typescript
export const userSchema = z.object({
  id: z.string(),
  email: z.string().email(),
  system_role: z.enum(["admin", "user"]),
  needs_setup: z.boolean().optional().default(false),
});
```

And update `UserResponse` in the backend `models.py` to include `needs_setup`:

```python
class UserResponse(BaseModel):
    """Response model for user info endpoint."""

    id: str
    email: str
    system_role: Literal["admin", "user"]
    needs_setup: bool = False
```

And update the `/me` endpoint in `backend/app/gateway/routers/auth.py`:

```python
@router.get("/me", response_model=UserResponse)
async def get_me(request: Request):
    """Get current authenticated user info."""
    user = await get_current_user_from_request(request)
    return UserResponse(id=str(user.id), email=user.email, system_role=user.system_role, needs_setup=user.needs_setup)
```

- [ ] **Step 3: Update workspace layout to handle needs_setup**

In `frontend/src/app/workspace/layout.tsx`:

```typescript
  switch (result.tag) {
    case "authenticated":
      return (
        <AuthProvider initialUser={result.user}>
          <WorkspaceContent>{children}</WorkspaceContent>
        </AuthProvider>
      );
    case "needs_setup":
      redirect("/setup");
    case "unauthenticated":
      redirect("/login");
    case "gateway_unavailable":
      return (
        <div className="flex h-screen flex-col items-center justify-center gap-4">
          <p className="text-muted-foreground">
            Service temporarily unavailable.
          </p>
          <p className="text-muted-foreground text-xs">
            The backend may be restarting. Please wait a moment and try again.
          </p>
          <div className="flex gap-3">
            <Link
              href="/workspace"
              className="bg-primary text-primary-foreground hover:bg-primary/90 rounded-md px-4 py-2 text-sm"
            >
              Retry
            </Link>
            <Link
              href="/api/v1/auth/logout"
              className="text-muted-foreground hover:bg-muted rounded-md border px-4 py-2 text-sm"
            >
              Logout &amp; Reset
            </Link>
          </div>
        </div>
      );
    case "config_error":
      throw new Error(result.message);
    default:
      assertNever(result);
  }
```

- [ ] **Step 4: Create the setup page**

Create `frontend/src/app/(auth)/setup/page.tsx`:

```tsx
"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { getCsrfHeaders } from "@/core/api/fetcher";
import { parseAuthError } from "@/core/auth/types";

export default function SetupPage() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [currentPassword, setCurrentPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const handleSetup = async (e: React.FormEvent) => {
    e.preventDefault();
    setError("");

    if (newPassword !== confirmPassword) {
      setError("Passwords do not match");
      return;
    }
    if (newPassword.length < 8) {
      setError("Password must be at least 8 characters");
      return;
    }

    setLoading(true);
    try {
      const res = await fetch("/api/v1/auth/change-password", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...getCsrfHeaders(),
        },
        credentials: "include",
        body: JSON.stringify({
          current_password: currentPassword,
          new_password: newPassword,
          new_email: email || undefined,
        }),
      });

      if (!res.ok) {
        const data = await res.json();
        const authError = parseAuthError(data);
        setError(authError.message);
        return;
      }

      router.push("/workspace");
    } catch {
      setError("Network error. Please try again.");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex min-h-screen items-center justify-center">
      <div className="w-full max-w-sm space-y-6 p-6">
        <div className="text-center">
          <h1 className="font-serif text-3xl">DeerFlow</h1>
          <p className="text-muted-foreground mt-2">
            Complete admin account setup
          </p>
          <p className="text-muted-foreground mt-1 text-xs">
            Set your real email and a new password.
          </p>
        </div>
        <form onSubmit={handleSetup} className="space-y-4">
          <Input
            type="email"
            placeholder="Your email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            required
          />
          <Input
            type="password"
            placeholder="Current password (from console log)"
            value={currentPassword}
            onChange={(e) => setCurrentPassword(e.target.value)}
            required
          />
          <Input
            type="password"
            placeholder="New password"
            value={newPassword}
            onChange={(e) => setNewPassword(e.target.value)}
            required
            minLength={8}
          />
          <Input
            type="password"
            placeholder="Confirm new password"
            value={confirmPassword}
            onChange={(e) => setConfirmPassword(e.target.value)}
            required
            minLength={8}
          />
          {error && <p className="text-sm text-red-500">{error}</p>}
          <Button type="submit" className="w-full" disabled={loading}>
            {loading ? "Setting up..." : "Complete Setup"}
          </Button>
        </form>
      </div>
    </div>
  );
}
```

- [ ] **Step 5: Run frontend type check**

Run: `cd frontend && pnpm typecheck`

Expected: PASS (no type errors)

- [ ] **Step 6: Commit**

```bash
git add frontend/src/core/auth/types.ts frontend/src/core/auth/server.ts frontend/src/app/workspace/layout.tsx frontend/src/app/\(auth\)/setup/page.tsx backend/app/gateway/auth/models.py backend/app/gateway/routers/auth.py
git commit -m "feat(auth): add setup page + SSR guard for needs_setup flow"
```

---

### Task 7: Full Regression — Run All Tests

- [ ] **Step 1: Run full backend test suite**

Run: `cd backend && make test`

Expected: All tests pass (including new tests from Tasks 1–5)

- [ ] **Step 2: Run frontend check**

Run: `cd frontend && pnpm check`

Expected: No lint or type errors

- [ ] **Step 3: Fix any regressions**

If any tests fail, diagnose and fix before proceeding.

- [ ] **Step 4: Final commit (if fixes were needed)**

```bash
git add -A
git commit -m "fix(auth): address test regressions from permission init changes"
```
