"""Read website usernames through short, read-only PostgreSQL connections."""

from __future__ import annotations

import asyncio
from uuid import UUID

import psycopg


class UserNameLookupError(RuntimeError):
    """A lookup failure safe to report without database credentials or details."""


class UserNameResolver:
    def __init__(self, database_url: str) -> None:
        self._database_url = database_url

    async def resolve(self, user_id: str) -> str:
        try:
            if not isinstance(user_id, str) or len(user_id) > 64:
                raise ValueError
            normalized_id = str(UUID(user_id))
        except (ValueError, AttributeError, TypeError):
            raise UserNameLookupError("Invalid user identifier") from None
        return await asyncio.to_thread(self._lookup, normalized_id)

    def _lookup(self, user_id: str) -> str:
        try:
            with psycopg.connect(
                self._database_url,
                connect_timeout=5,
                options="-c statement_timeout=5000",
            ) as connection:
                connection.read_only = True
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT username FROM naruto_login.users WHERE id = %s",
                        (user_id,),
                    )
                    row = cursor.fetchone()
            if row is None or not isinstance(row[0], str):
                return ""
            return row[0].strip()[:128]
        except Exception:
            raise UserNameLookupError("Could not look up website user name") from None
