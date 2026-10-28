"""Read website QQ bindings within the configured bot and group scope."""

from __future__ import annotations

import asyncio
import re
from uuid import UUID

import psycopg


OPENID_PATTERN = re.compile(r'[a-zA-Z0-9_-]{1,128}')


class QQBindingLookupError(RuntimeError):
    """A binding lookup error containing no database or member details."""


class QQBindingResolver:
    def __init__(self, database_url: str, app_id: str, group_openid: str) -> None:
        self._database_url = database_url
        self._app_id = app_id
        self._group_openid = group_openid

    async def resolve(self, user_id: str) -> str:
        try:
            if not isinstance(user_id, str) or len(user_id) > 64:
                raise ValueError
            normalized_id = str(UUID(user_id))
        except (ValueError, AttributeError, TypeError):
            raise QQBindingLookupError('Invalid user identifier') from None
        return await asyncio.to_thread(self._lookup, normalized_id)

    def _lookup(self, user_id: str) -> str:
        try:
            with psycopg.connect(
                self._database_url, connect_timeout=5,
                options='-c statement_timeout=5000',
            ) as connection:
                connection.read_only = True
                with connection.cursor() as cursor:
                    cursor.execute(
                        'SELECT b.member_openid FROM naruto_login.qq_bindings b '
                        'JOIN naruto_login.users u ON u.id = b.user_id '
                        'WHERE b.user_id = %s AND b.bot_app_id = %s AND b.group_openid = %s '
                        'AND u.is_invalid = false',
                        (user_id, self._app_id, self._group_openid),
                    )
                    row = cursor.fetchone()
            if row is not None and isinstance(row[0], str) and OPENID_PATTERN.fullmatch(row[0]):
                return row[0]
            return ''
        except Exception:
            raise QQBindingLookupError('Could not look up QQ binding') from None
