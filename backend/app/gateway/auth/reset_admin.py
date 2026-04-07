"""CLI tool to reset admin password.

Usage:
    python -m app.gateway.auth.reset_admin
    python -m app.gateway.auth.reset_admin --email admin@example.com
"""

import argparse
import secrets
import sys

from app.gateway.auth.password import hash_password
from app.gateway.auth.repositories.sqlite import SQLiteUserRepository


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset admin password")
    parser.add_argument("--email", help="Admin email (default: first admin found)")
    args = parser.parse_args()

    repo = SQLiteUserRepository()

    # Find admin user synchronously (CLI context, no event loop)
    import asyncio

    user = asyncio.run(_find_admin(repo, args.email))
    if user is None:
        if args.email:
            print(f"Error: user '{args.email}' not found.", file=sys.stderr)
        else:
            print("Error: no admin user found.", file=sys.stderr)
        sys.exit(1)

    new_password = secrets.token_urlsafe(16)
    user.password_hash = hash_password(new_password)
    user.token_version += 1
    user.needs_setup = True
    asyncio.run(repo.update_user(user))

    print(f"Password reset for: {user.email}")
    print(f"New password: {new_password}")
    print("Next login will require setup (new email + password).")


async def _find_admin(repo: SQLiteUserRepository, email: str | None):
    if email:
        return await repo.get_user_by_email(email)
    # Find first admin
    import asyncio

    from app.gateway.auth.repositories.sqlite import _get_users_conn

    def _find_sync():
        with _get_users_conn() as conn:
            cursor = conn.execute("SELECT id FROM users WHERE system_role = 'admin' LIMIT 1")
            row = cursor.fetchone()
            return dict(row)["id"] if row else None

    admin_id = await asyncio.to_thread(_find_sync)
    if admin_id:
        return await repo.get_user_by_id(admin_id)
    return None


if __name__ == "__main__":
    main()
