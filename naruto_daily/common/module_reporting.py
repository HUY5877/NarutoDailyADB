"""Report module boundaries to the task parent without changing execution."""

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Callable, Iterator


EXEMPT_MODULES = frozenset({'enter_secret_realm', 'handle_secret_realm'})
_reporter: ContextVar[Callable[[dict], None] | None] = ContextVar('module_reporter', default=None)
_modules: ContextVar[tuple[str, ...]] = ContextVar('active_modules', default=())


@contextmanager
def module_reporting(send_event: Callable[[dict], None]) -> Iterator[None]:
    token = _reporter.set(send_event)
    active_token = _modules.set(())
    try:
        yield
    finally:
        _modules.reset(active_token)
        _reporter.reset(token)


@contextmanager
def module_scope(name: str) -> Iterator[None]:
    send_event = _reporter.get()
    active = _modules.get()
    if send_event is None or active and active[-1] == name:
        yield
        return
    token = _modules.set((*active, name))
    reported = False
    try:
        if name not in EXEMPT_MODULES:
            try:
                send_event({'type': 'module', 'state': 'started', 'name': name})
                reported = True
            except Exception:
                # Diagnostics must not turn a working game operation into a failure.
                pass
        yield
    finally:
        if reported:
            try:
                send_event({'type': 'module', 'state': 'finished', 'name': name})
            except Exception:
                pass
        _modules.reset(token)


def monitored_module(name: str):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with module_scope(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate
