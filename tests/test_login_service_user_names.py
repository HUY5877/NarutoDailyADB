"""Exercise username lookup at the PostgreSQL connection boundary."""

import asyncio
import importlib
import threading
import traceback

import pytest


USER_ID = "12345678-1234-4abc-9234-123456789012"
DATABASE_URL = "postgresql://private-user:private-password@database.invalid/test"


def resolver_module():
    try:
        return importlib.import_module("naruto_daily.login_service.user_names")
    except ModuleNotFoundError as error:
        if error.name == "naruto_daily.login_service.user_names":
            pytest.fail("The PostgreSQL username resolver has not been implemented")
        raise


class DatabaseBoundary:
    """Replace network I/O while observing query and connection policy."""

    def __init__(self, row=("小鸣人",), *, failure=None):
        self.row = row
        self.failure = failure
        self.connections = []

    def fail(self, phase):
        if self.failure == phase:
            raise RuntimeError(f"{DATABASE_URL}: private-driver-error")

    def connect(self, conninfo, **kwargs):
        self.fail("connect")
        connection = DatabaseConnection(self)
        self.connections.append((conninfo, kwargs, threading.get_ident(), connection))
        return connection


class DatabaseConnection:
    def __init__(self, boundary):
        self.boundary = boundary
        self.read_only = False
        self.closed = False
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.closed = True
        self.boundary.fail("close")

    def cursor(self):
        self.boundary.fail("cursor")
        return DatabaseCursor(self)


class DatabaseCursor:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def execute(self, query, parameters):
        self.connection.queries.append((query, parameters, self.connection.read_only))
        self.connection.boundary.fail("execute")

    def fetchone(self):
        self.connection.boundary.fail("fetch")
        return self.connection.boundary.row


def make_resolver(monkeypatch, boundary):
    module = resolver_module()
    monkeypatch.setattr(module.psycopg, "connect", boundary.connect)
    return module, module.UserNameResolver(DATABASE_URL)


def test_lookup_binds_uuid_and_uses_bounded_read_only_connection(monkeypatch):
    # Catches interpolated SQL, write-capable transactions, missing timeouts or leaked connections.
    boundary = DatabaseBoundary(("  小鸣人  ",))
    _, resolver = make_resolver(monkeypatch, boundary)
    main_thread = threading.get_ident()

    assert asyncio.run(resolver.resolve(USER_ID.upper())) == "小鸣人"
    assert len(boundary.connections) == 1
    conninfo, options, thread_id, connection = boundary.connections[0]
    assert conninfo == DATABASE_URL
    assert options["connect_timeout"] == 5
    assert "statement_timeout=5000" in options["options"]
    assert thread_id != main_thread
    assert connection.queries == [
        ("SELECT username FROM naruto_login.users WHERE id = %s", (USER_ID,), True)
    ]
    assert connection.closed


@pytest.mark.parametrize("row", [None, (None,), ("",), ("  \t ",), (123,)])
def test_missing_or_unusable_user_name_returns_empty(monkeypatch, row):
    # Catches making up a name from missing database records or non-text values.
    boundary = DatabaseBoundary(row)
    _, resolver = make_resolver(monkeypatch, boundary)

    assert asyncio.run(resolver.resolve(USER_ID)) == ""
    assert boundary.connections[0][3].closed


def test_unexpectedly_long_database_name_has_a_finite_result(monkeypatch):
    # Catches letting an oversized database value become unbounded notification content.
    boundary = DatabaseBoundary(("鸣" * 10000,))
    _, resolver = make_resolver(monkeypatch, boundary)

    assert asyncio.run(resolver.resolve(USER_ID)) == "鸣" * 128
    assert boundary.connections[0][3].closed


@pytest.mark.parametrize("user_id", [None, 123, "", "not-a-uuid", "' OR true --"])
def test_invalid_user_id_fails_before_opening_database(monkeypatch, user_id):
    # Catches unnecessary external requests and unbound user-controlled identifiers.
    boundary = DatabaseBoundary()
    module, resolver = make_resolver(monkeypatch, boundary)

    with pytest.raises(module.UserNameLookupError):
        asyncio.run(resolver.resolve(user_id))
    assert boundary.connections == []


@pytest.mark.parametrize("phase", ["connect", "cursor", "execute", "fetch", "close"])
def test_database_failures_hide_credentials_and_driver_details(monkeypatch, phase):
    # Catches driver exceptions or chained traceback text exposing connection credentials.
    boundary = DatabaseBoundary(failure=phase)
    module, resolver = make_resolver(monkeypatch, boundary)

    with pytest.raises(module.UserNameLookupError) as failure:
        asyncio.run(resolver.resolve(USER_ID))
    diagnostic = "".join(traceback.format_exception(failure.value))
    assert DATABASE_URL not in diagnostic
    assert "private-password" not in diagnostic
    assert "private-driver-error" not in diagnostic
    if boundary.connections:
        assert boundary.connections[0][3].closed


def test_each_lookup_closes_its_own_connection(monkeypatch):
    # Catches sharing a synchronous connection between concurrent background lookups.
    boundary = DatabaseBoundary()
    _, resolver = make_resolver(monkeypatch, boundary)

    async def resolve_twice():
        return await asyncio.gather(resolver.resolve(USER_ID), resolver.resolve(USER_ID))

    assert asyncio.run(resolve_twice()) == ["小鸣人", "小鸣人"]
    assert len(boundary.connections) == 2
    assert all(item[3].closed for item in boundary.connections)
