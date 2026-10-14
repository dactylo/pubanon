import base64
import http.client
import io
import json
import subprocess
import sys
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from subprocess import Popen
from types import TracebackType
from typing import Self

import pytest

from pubanon._errors import ObservationTimeoutError, PubanonError
from pubanon._remote import _package
from pubanon._remote._package import PackageRelease, _normalize_name, release_state


class _FetchProcess:
    def __init__(self) -> None:
        self.returncode: int | None = None
        self.stdout = io.BytesIO()
        self.stderr = io.BytesIO()
        self.stdin = io.BytesIO()
        self.result = b'[200, "Ym9keQ=="]'
        self.exit_code = 0
        self.failure: BaseException | None = None
        self.calls: list[tuple[bytes | None, float | None]] = []
        self.killed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        self.stdin.close()
        self.stdout.close()
        self.stderr.close()

    def communicate(
        self, request_bytes: bytes | None = None, timeout: float | None = None
    ) -> tuple[bytes, bytes]:
        self.calls.append((request_bytes, timeout))
        if self.failure is not None:
            failure, self.failure = self.failure, None
            raise failure
        self.returncode = self.exit_code
        return self.result, b'child-stderr-canary'

    def kill(self) -> None:
        self.killed = True

    def poll(self) -> int | None:
        return self.returncode


class _Response:
    def __init__(self, body: bytes, status: int) -> None:
        self._body = body
        self.status = status
        self.closed = False

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.closed = True

    def read(self) -> bytes:
        return self._body


def _fetching_bytes(status: int, body: bytes) -> object:
    def _fetch(
        _url: str,
        *,
        timeout: float | None = None,
    ) -> tuple[int, bytes]:
        del timeout
        return status, body

    return _fetch


def _fetching(status: int, payload: object) -> object:
    return _fetching_bytes(status, json.dumps(payload).encode())


def _patch_fetch_process(
    monkeypatch: pytest.MonkeyPatch,
    process: _FetchProcess,
    times: list[float],
) -> list[list[str]]:
    ticks = iter(times)
    monkeypatch.setattr(_package, '_monotonic', lambda: next(ticks))
    commands: list[list[str]] = []

    def launch(command: list[str], **kwargs: object) -> _FetchProcess:
        commands.append(command)
        assert kwargs == {
            'stdin': subprocess.PIPE,
            'stdout': subprocess.PIPE,
            'stderr': subprocess.PIPE,
            'shell': False,
        }
        return process

    monkeypatch.setattr(_package.subprocess, 'Popen', launch)
    return commands


def test_fetch_actual_child_is_reaped_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    launched: list[Popen[bytes]] = []
    popen = subprocess.Popen

    def launch(
        command: list[str], *, stdin: int, stdout: int, stderr: int, shell: bool
    ) -> Popen[bytes]:
        process = popen(command, stdin=stdin, stdout=stdout, stderr=stderr, shell=shell)
        launched.append(process)
        return process

    monkeypatch.setattr(_package.subprocess, 'Popen', launch)
    monkeypatch.setattr(
        _package,
        '_fetch_command',
        lambda: [sys.executable, '-c', 'import threading; threading.Event().wait()'],
    )
    with pytest.raises(ObservationTimeoutError):
        _package._fetch('https://example.invalid', timeout=0.2)
    assert len(launched) == 1
    assert launched[0].returncode is not None


@pytest.mark.parametrize('http_error', [False, True])
@pytest.mark.parametrize('timeout_read', [False, True])
def test_fetch_http_closes_normal_and_error_bodies_including_timeout(
    monkeypatch: pytest.MonkeyPatch, http_error: bool, timeout_read: bool
) -> None:
    response = _Response(b'body', 200)
    error_body = io.BytesIO(b'error body')
    error = urllib.error.HTTPError(
        'https://example.invalid', 404, 'missing', Message(), error_body
    )

    def read() -> bytes:
        raise TimeoutError

    def _urlopen(_url: str, *, timeout: float) -> _Response:
        assert timeout == 7
        if http_error:
            raise error
        return response

    if timeout_read:
        monkeypatch.setattr(error if http_error else response, 'read', read)
    monkeypatch.setattr(_package.urllib.request, 'urlopen', _urlopen)
    if timeout_read:
        with pytest.raises(ObservationTimeoutError):
            _package._fetch_http('https://example.invalid', timeout=7)
    else:
        assert _package._fetch_http('https://example.invalid', timeout=7) == (
            (404, b'error body') if http_error else (200, b'body')
        )
    assert error_body.closed if http_error else response.closed


def test_fetch_http_refuses_bad_status_line_without_exposing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float | None]] = []

    def _urlopen(url: str, *, timeout: float | None = None) -> _Response:
        calls.append((url, timeout))
        raise http.client.BadStatusLine('status-line-canary')

    monkeypatch.setattr(_package.urllib.request, 'urlopen', _urlopen)

    with pytest.raises(PubanonError) as raised:
        _package._fetch_http('https://pypi.org/pypi/demo/1.2.3/json', timeout=30)

    assert str(raised.value) == 'package-index transport failed'
    assert raised.value.fact == 'reading a package release failed; cause unknown'
    assert 'status-line-canary' not in raised.value.render()
    assert calls == [('https://pypi.org/pypi/demo/1.2.3/json', 30)]


def test_fetch_http_refuses_incomplete_read_without_exposing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _Response(b'', 200)
    calls: list[tuple[str, float | None]] = []

    def _read() -> bytes:
        raise http.client.IncompleteRead(b'response-body-canary', 4)

    def _urlopen(url: str, *, timeout: float | None = None) -> _Response:
        calls.append((url, timeout))
        return response

    monkeypatch.setattr(response, 'read', _read)
    monkeypatch.setattr(_package.urllib.request, 'urlopen', _urlopen)

    with pytest.raises(PubanonError) as raised:
        _package._fetch_http('https://pypi.org/pypi/demo/1.2.3/json', timeout=30)

    assert str(raised.value) == 'package-index transport failed'
    assert raised.value.fact == 'reading a package release failed; cause unknown'
    assert 'response-body-canary' not in raised.value.render()
    assert calls == [('https://pypi.org/pypi/demo/1.2.3/json', 30)]


def test_fetch_http_refuses_response_read_error_without_exposing_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = _Response(b'', 200)
    calls: list[tuple[str, float | None]] = []

    def _read() -> bytes:
        raise OSError('response-read-canary')

    def _urlopen(url: str, *, timeout: float | None = None) -> _Response:
        calls.append((url, timeout))
        return response

    monkeypatch.setattr(response, 'read', _read)
    monkeypatch.setattr(_package.urllib.request, 'urlopen', _urlopen)

    with pytest.raises(PubanonError) as raised:
        _package._fetch_http('https://pypi.org/pypi/demo/1.2.3/json', timeout=30)

    assert str(raised.value) == 'package-index transport failed'
    assert raised.value.fact == 'reading a package release failed; cause unknown'
    assert 'response-read-canary' not in raised.value.render()
    assert calls == [('https://pypi.org/pypi/demo/1.2.3/json', 30)]


@pytest.mark.parametrize(
    'transport_error',
    [
        urllib.error.URLError('transport-error-canary'),
        OSError('transport-error-canary'),
    ],
)
def test_fetch_http_refuses_transport_errors_without_exposing_them(
    monkeypatch: pytest.MonkeyPatch,
    transport_error: OSError,
) -> None:
    calls: list[tuple[str, float | None]] = []

    def _urlopen(url: str, *, timeout: float | None = None) -> _Response:
        calls.append((url, timeout))
        raise transport_error

    monkeypatch.setattr(_package.urllib.request, 'urlopen', _urlopen)

    with pytest.raises(PubanonError) as raised:
        _package._fetch_http('https://pypi.org/pypi/demo/1.2.3/json', timeout=30)

    assert str(raised.value) == 'package-index transport failed'
    assert raised.value.fact == 'reading a package release failed; cause unknown'
    assert 'transport-error-canary' not in raised.value.render()
    assert calls == [('https://pypi.org/pypi/demo/1.2.3/json', 30)]


def test_fetch_http_timeout_raises_private_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _urlopen(_url: str, *, timeout: float | None = None) -> _Response:
        assert timeout == 7.0
        raise TimeoutError('private-transport-canary')

    monkeypatch.setattr(urllib.request, 'urlopen', _urlopen)

    with pytest.raises(ObservationTimeoutError) as exc_info:
        _package._fetch_http('https://example.invalid', timeout=7.0)

    assert type(exc_info.value).__name__ == 'ObservationTimeoutError'
    assert 'private-transport-canary' not in repr(exc_info.value)


@pytest.mark.parametrize('failure', [subprocess.TimeoutExpired('worker', 1), KeyboardInterrupt()])
def test_fetch_kills_and_reaps_on_timeout_or_interruption(
    monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    process = _FetchProcess()
    process.failure = failure
    _patch_fetch_process(monkeypatch, process, [0, 0, 0])
    expected = (
        ObservationTimeoutError
        if isinstance(failure, subprocess.TimeoutExpired)
        else type(failure)
    )
    with pytest.raises(expected):
        _package._fetch('https://example.invalid', timeout=1)
    assert process.killed
    assert process.returncode is not None
    assert process.calls[-1] == (None, None)
    assert process.stdout.closed
    assert process.stderr.closed
    assert process.stdin.closed


@pytest.mark.parametrize(('timeout', 'budget'), [(None, 30.0), (45.0, 30.0), (7.0, 7.0)])
def test_fetch_launch_time_reduces_remaining_budget(
    monkeypatch: pytest.MonkeyPatch, timeout: float | None, budget: float
) -> None:
    process = _FetchProcess()
    commands = _patch_fetch_process(monkeypatch, process, [10, 10, 12, 13, 13])
    assert _package._fetch('https://example.invalid', timeout=timeout) == (200, b'body')
    assert commands == [_package._fetch_command()]
    request, remaining = process.calls[0]
    assert request is not None
    assert json.loads(request) == {'url': 'https://example.invalid', 'timeout': budget}
    assert remaining == budget - 2
    assert process.stdout.closed
    assert process.stderr.closed
    assert process.stdin.closed


@pytest.mark.parametrize('exit_code', [0, 1, 2])
def test_fetch_main_emits_body_or_failure_exit(
    monkeypatch: pytest.MonkeyPatch, exit_code: int
) -> None:
    def fetch_http(url: str, *, timeout: float) -> tuple[int, bytes]:
        assert url == 'https://example.invalid'
        assert timeout == 4
        if exit_code == 1:
            raise PubanonError('transport-canary')
        if exit_code == 2:
            raise ObservationTimeoutError
        return 404, b'\xffbody'

    request = b'{"url":"https://example.invalid","timeout":4}'
    output = io.BytesIO()
    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(io.BytesIO(request)))
    monkeypatch.setattr(sys, 'stdout', io.TextIOWrapper(output))
    monkeypatch.setattr(_package, '_fetch_http', fetch_http)
    assert _package._fetch_main() == exit_code
    if exit_code == 0:
        assert json.loads(output.getvalue()) == [
            404,
            base64.b64encode(b'\xffbody').decode('ascii'),
        ]
    else:
        assert output.getvalue() == b''


@pytest.mark.parametrize(
    'request_bytes',
    [
        b'',
        b'{',
        b'[]',
        b'{}',
        b'{"url":1,"timeout":1}',
        b'{"url":"x","timeout":true}',
        b'{"url":"x","timeout":0}',
        b'{"url":"x","timeout":-1}',
        b'{"url":"x","timeout":"1"}',
        b'{"url":"x","timeout":NaN}',
        b'{"url":"x","timeout":1e999}',
    ],
)
def test_fetch_main_rejects_invalid_requests_before_http(
    monkeypatch: pytest.MonkeyPatch, request_bytes: bytes
) -> None:
    def unexpected(*_args: object, **_kwargs: object) -> tuple[int, bytes]:
        pytest.fail('invalid request reached HTTP')

    monkeypatch.setattr(_package, '_fetch_http', unexpected)
    monkeypatch.setattr(sys, 'stdin', io.TextIOWrapper(io.BytesIO(request_bytes)))
    assert _package._fetch_main() == 1


@pytest.mark.parametrize('times', [[0, 0, 2], [0, 0, 0, 2], [0, 0, 0, 0, 2]])
def test_fetch_refuses_late_launch_response_or_decode(
    monkeypatch: pytest.MonkeyPatch, times: list[float]
) -> None:
    process = _FetchProcess()
    _patch_fetch_process(monkeypatch, process, times)
    with pytest.raises(ObservationTimeoutError):
        _package._fetch('https://example.invalid', timeout=1)
    assert process.returncode is not None
    assert process.stdout.closed


@pytest.mark.parametrize(
    ('exit_code', 'result'),
    [
        (1, b'child-stdout-canary'),
        (0, b'{'),
        (0, b'[200, "!"]'),
        (0, b'[true, ""]'),
        (0, b'["200", ""]'),
        (0, b'[200, 1]'),
        (0, b'[]'),
    ],
)
def test_fetch_rejects_failure_and_malformed_results_without_child_output(
    monkeypatch: pytest.MonkeyPatch, exit_code: int, result: bytes
) -> None:
    process = _FetchProcess()
    process.exit_code, process.result = exit_code, result
    _patch_fetch_process(monkeypatch, process, [0, 0, 0, 0, 0])
    with pytest.raises(PubanonError) as raised:
        release_state('pypi', 'demo', '1.2.3')
    assert str(raised.value) == 'Could not check PyPI release demo 1.2.3'
    assert 'canary' not in raised.value.render()


def test_fetch_translates_worker_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    process = _FetchProcess()
    process.exit_code = 2
    _patch_fetch_process(monkeypatch, process, [0, 0, 0, 0])
    with pytest.raises(ObservationTimeoutError):
        _package._fetch('https://example.invalid')


def test_fetch_worker_uses_current_package_over_ambient_pythonpath(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = tmp_path / 'pubanon'
    fake.mkdir()
    (fake / '__init__.py').write_text('')
    remote = fake / '_remote'
    remote.mkdir()
    (remote / '__init__.py').write_text('')
    (remote / '_package.py').write_text('print("wrong-package-canary"); raise SystemExit(73)')
    monkeypatch.setenv('PYTHONPATH', str(tmp_path))
    result = subprocess.run(_package._fetch_command(), input=b'', capture_output=True, check=False)
    assert result.returncode == 1
    assert result.stdout == b''
    assert result.stderr == b''


def test_fetch_zero_budget_never_launches(monkeypatch: pytest.MonkeyPatch) -> None:
    commands = _patch_fetch_process(monkeypatch, _FetchProcess(), [10, 10])
    with pytest.raises(ObservationTimeoutError):
        _package._fetch('https://example.invalid', timeout=0)
    assert commands == []


@pytest.mark.parametrize(
    ('name', 'expected'),
    [
        ('My.Pkg_name', 'my-pkg-name'),
        ('already-normalized', 'already-normalized'),
        ('Runs...of___separators', 'runs-of-separators'),
    ],
)
def test_normalize_name_uses_pep_503_comparison_form(name: str, expected: str) -> None:
    assert _normalize_name(name) == expected


def test_release_state_default_fetch_uses_rendered_adapter_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, float | None]] = []

    def fetch(url: str, *, timeout: float | None = None) -> tuple[int, bytes]:
        calls.append((url, timeout))
        return 404, b''

    monkeypatch.setattr(
        _package,
        '_PYPI_RELEASE_URL',
        'https://packages.example.invalid/releases/{name}/{version}',
    )
    monkeypatch.setattr(_package, '_fetch', fetch)

    assert release_state('pypi', 'My.Pkg_name', '1.2.3') is None
    assert calls == [('https://packages.example.invalid/releases/my-pkg-name/1.2.3', None)]


@pytest.mark.parametrize(
    'payload',
    [
        b'{',
        b'[]',
        b'{"info":{"version":"9.9.9"},"urls":[]}',
        b'{"info":{"version":"1.2.3"}}',
        b'{"info":{"version":"1.2.3"},"urls":{}}',
        b'{"info":{"version":"1.2.3"},"urls":[null]}',
        b'{"info":{"version":"1.2.3"},"urls":[{"filename":1}]}',
    ],
)
def test_release_state_refuses_invalid_response_shapes(
    payload: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        _package,
        '_fetch',
        _fetching_bytes(200, payload),
    )
    with pytest.raises(PubanonError, match='invalid package-index response'):
        release_state('pypi', 'demo', '1.2.3')


@pytest.mark.parametrize('constant', [b'NaN', b'Infinity', b'-Infinity'])
def test_release_state_refuses_nonstandard_json_constants(
    constant: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = b'{"info":{"version":"1.2.3"},"urls":[],"serial":' + constant + b'}'
    monkeypatch.setattr(
        _package,
        '_fetch',
        _fetching_bytes(200, payload),
    )

    with pytest.raises(PubanonError, match='invalid package-index response'):
        release_state('pypi', 'demo', '1.2.3')


def test_release_state_refuses_unexpected_status_without_exposing_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response_canary = 'transport-response-canary'
    monkeypatch.setattr(
        _package,
        '_fetch',
        _fetching_bytes(500, response_canary.encode()),
    )

    with pytest.raises(PubanonError) as raised:
        release_state('pypi', 'demo', '1.2.3')

    assert str(raised.value) == 'Could not check PyPI release demo 1.2.3'
    assert raised.value.fact is not None
    assert 'PyPI returned HTTP 500' in raised.value.fact
    assert 'release status is unknown' in raised.value.fact
    assert raised.value.recovery is not None
    assert 'Retry the package check' in raised.value.recovery
    assert response_canary not in raised.value.render()


def test_release_state_refuses_unknown_index() -> None:
    with pytest.raises(PubanonError, match='unsupported package index'):
        release_state('other-index', 'demo', '1.2.3')


def test_release_state_returns_matching_release_files_in_response_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        'info': {'version': '1.2.3'},
        'urls': [
            {'filename': 'demo-1.2.3.tar.gz'},
            {'filename': 'demo-1.2.3-py3-none-any.whl'},
        ],
    }

    monkeypatch.setattr(_package, '_fetch', _fetching(200, payload))

    result = release_state('pypi', 'demo', '1.2.3')

    assert result == PackageRelease(
        '1.2.3',
        ('demo-1.2.3.tar.gz', 'demo-1.2.3-py3-none-any.whl'),
    )


def test_release_state_returns_none_for_missing_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(_package, '_fetch', _fetching(404, {'message': 'Not Found'}))

    assert release_state('pypi', 'demo', '1.2.3') is None
