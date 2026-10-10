import errno
import socket
import ssl
from urllib.error import URLError

import httpx2 as httpx
import pytest

from naruto_daily.multimodal.diagnostics import safe_exception_details


@pytest.mark.parametrize('reason,expected_type,expected_errno,expected_kind', [
    (TimeoutError('private-key'), 'TimeoutError', None, 'timeout'),
    (socket.gaierror(socket.EAI_NONAME, 'private-key'), 'gaierror', socket.EAI_NONAME, 'dns'),
    (ConnectionResetError(errno.ECONNRESET, 'private-key'), 'ConnectionResetError', errno.ECONNRESET,
     'connection_reset'),
    (ssl.SSLCertVerificationError(1, 'private-key'), 'SSLCertVerificationError', 1, 'tls_certificate'),
])
def test_url_error_preserves_typed_network_cause_without_unsafe_message(
        reason, expected_type, expected_errno, expected_kind):
    error = URLError(reason, 'https://private.invalid/private-key')

    details = safe_exception_details(error)

    assert 'URLError' in details and expected_type in details
    assert f'error_kind={expected_kind}' in details
    if expected_errno is not None:
        assert f'errno={expected_errno}' in details
    assert 'private-key' not in details and 'private.invalid' not in details


def test_explicit_exception_chain_reports_inner_os_code_without_traceback_text():
    underlying = ConnectionResetError(errno.ECONNRESET, 'private-key')
    underlying.winerror = 10054
    wrapped = RuntimeError('private-key https://private.invalid')
    wrapped.__cause__ = underlying

    details = safe_exception_details(wrapped)

    assert 'RuntimeError' in details and 'ConnectionResetError' in details
    assert f'errno={errno.ECONNRESET}' in details and 'winerror=10054' in details
    assert 'private-key' not in details and 'private.invalid' not in details


def test_string_only_url_reason_does_not_become_a_log_message():
    error = URLError('private-key https://private.invalid request body private-prompt')

    details = safe_exception_details(error)

    assert 'URLError' in details
    for secret in ['private-key', 'private.invalid', 'private-prompt']:
        assert secret not in details


def test_cyclic_exception_chain_is_bounded():
    first = RuntimeError('private-key')
    second = ConnectionError('private-key')
    first.__cause__ = second
    second.__cause__ = first

    details = safe_exception_details(first)

    assert 'RuntimeError' in details and 'ConnectionError' in details
    assert len(details) < 1000 and 'private-key' not in details


@pytest.mark.parametrize('error_class,kind', [(httpx.ConnectTimeout, 'connect_timeout'),
                                           (httpx.ReadTimeout, 'read_timeout')])
def test_specific_timeout_kind_survives_generic_inner_timeout(error_class, kind):
    error = error_class('private-key')
    error.__cause__ = TimeoutError('private-key')
    details = safe_exception_details(error)
    assert f'error_kind={kind}' in details and '->TimeoutError' in details
    assert 'private-key' not in details
