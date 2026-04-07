"""SQLite implementation of UserRepository."""

import asyncio
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from app.gateway.auth.config import get_auth_config
from app.gateway.auth.models import User
from app.gateway.auth.repositories.base import UserRepository

_resolved_db_path: Path | None = None
_table_initialized: bool = False


def _get_users_db_path() -> Path:
    """Get the users database path (resolved and cached once)."""
    global _resolved_db_path
    if _resolved_db_path is not None:
        return _resolved_db_path
    config = get_auth_config()
    if config.users_db_path:
        _resolved_db_path = Path(config.users_db_path)
    else:
        _resolved_db_path = Path(".deer-flow/users.db")
    _resolved_db_path.parent.mkdir(parents=True, exist_ok=True)
    return _resolved_db_path


def _get_connection() -> sqlite3.Connection:
    """Get a SQLite connection for the users database."""
    db_path = _get_users_db_path()
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _init_users_table(conn: sqlite3.Connection) -> None:
    """Initialize the users table if it doesn't exist."""
    conn.execute("PRAGMA journal_mode=WAL")
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
    conn.commit()


@contextmanager
def _get_users_conn():
    """Context manager for users database connection."""
    global _table_initialized
    conn = _get_connection()
    try:
        if not _table_initialized:
            _init_users_table(conn)
            _table_initialized = True
        yield conn
    finally:
        conn.close()


class SQLiteUserRepository(UserRepository):
    """SQLite implementation of UserRepository."""

    async def create_user(self, user: User) -> User:
        """Create a new user in SQLite."""
        return await asyncio.to_thread(self._create_user_sync, user)

    def _create_user_sync(self, user: User) -> User:
        """Synchronous user creation (runs in thread pool)."""
        with _get_users_conn() as conn:
            try:
                conn.execute(
                    """
                    INSERT INTO users (id, email, password_hash, system_role, created_at, oauth_provider, oauth_id, needs_setup, token_version)
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

    async def get_user_by_id(self, user_id: str) -> User | None:
        """Get user by ID from SQLite."""
        return await asyncio.to_thread(self._get_user_by_id_sync, user_id)

    def _get_user_by_id_sync(self, user_id: str) -> User | None:
        """Synchronous get by ID (runs in thread pool)."""
        with _get_users_conn() as conn:
            cursor = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,))
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_user(dict(row))

    async def get_user_by_email(self, email: str) -> User | None:
        """Get user by email from SQLite."""
        return await asyncio.to_thread(self._get_user_by_email_sync, email)

    def _get_user_by_email_sync(self, email: str) -> User | None:
        """Synchronous get by email (runs in thread pool)."""
        with _get_users_conn() as conn:
            cursor = conn.execute("SELECT * FROM users WHERE email = ?", (email,))
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_user(dict(row))

    async def update_user(self, user: User) -> User:
        """Update an existing user in SQLite."""
        return await asyncio.to_thread(self._update_user_sync, user)

    def _update_user_sync(self, user: User) -> User:
        with _get_users_conn() as conn:
            conn.execute(
                "UPDATE users SET email = ?, password_hash = ?, system_role = ?, oauth_provider = ?, oauth_id = ?, needs_setup = ?, token_version = ? WHERE id = ?",
                (user.email, user.password_hash, user.system_role, user.oauth_provider, user.oauth_id, int(user.needs_setup), user.token_version, str(user.id)),
            )
            conn.commit()
        return user

    async def count_users(self) -> int:
        """Return total number of registered users."""
        return await asyncio.to_thread(self._count_users_sync)

    def _count_users_sync(self) -> int:
        with _get_users_conn() as conn:
            cursor = conn.execute("SELECT COUNT(*) FROM users")
            return cursor.fetchone()[0]

    async def get_user_by_oauth(self, provider: str, oauth_id: str) -> User | None:
        """Get user by OAuth provider and ID from SQLite."""
        return await asyncio.to_thread(self._get_user_by_oauth_sync, provider, oauth_id)

    def _get_user_by_oauth_sync(self, provider: str, oauth_id: str) -> User | None:
        """Synchronous get by OAuth (runs in thread pool)."""
        with _get_users_conn() as conn:
            cursor = conn.execute(
                "SELECT * FROM users WHERE oauth_provider = ? AND oauth_id = ?",
                (provider, oauth_id),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            return self._row_to_user(dict(row))

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
            needs_setup=bool(row["needs_setup"]),
            token_version=int(row["token_version"]),
        )
