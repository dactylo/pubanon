"""Read package release state from supported package indexes."""

import base64
import http.client
import json
import math
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from time import monotonic as _monotonic
from typing import NamedTuple, Never, cast

from pubanon._errors import ObservationTimeoutError, PubanonError
from pubanon._terminal import escape

_FETCH_BOOTSTRAP = (
    'import runpy, sys; '
    'sys.path.insert(0, sys.argv[1]); '
    'runpy.run_module("pubanon._remote._package", run_name="__main__")'
)
_FETCH_FAILURE = 1
_FETCH_SUCCESS = 0
_FETCH_TIMEOUT = 2
_HTTP_NOT_FOUND = 404
_HTTP_OK = 200
_PYPI_RELEASE_URL = 'https://pypi.org/pypi/{name}/{version}/json'
_PYPI_TIMEOUT = 30.0


class PackageRelease(NamedTuple):
    """Describe one published package release.

    Attributes:
        version: Exact published version.
        files: Published filenames in package-index response order. An empty
            tuple records version metadata without a complete distribution.
    """

    version: str
    files: tuple[str, ...]


def _decode_fetch_result(stdout: bytes) -> tuple[int, bytes]:
    """Validate the worker's status and strict base64 response body."""
    transport_failure = PubanonError(
        'package-index transport failed',
        fact='reading a package release failed; cause unknown',
    )
    try:
        result = json.loads(stdout)
    except ValueError as exc:
        raise transport_failure from exc
    if not isinstance(result, list):
        raise transport_failure
    try:
        status, body = cast('list[object]', result)
    except ValueError as exc:
        raise transport_failure from exc
    if not isinstance(status, int) or isinstance(status, bool) or not isinstance(body, str):
        raise transport_failure
    try:
        return status, base64.b64decode(body, validate=True)
    except ValueError as exc:
        raise transport_failure from exc


def _fetch_command() -> list[str]:
    """Build the worker command using this interpreter and package source root."""
    return [sys.executable, '-c', _FETCH_BOOTSTRAP, str(Path(__file__).resolve().parents[2])]


def _fetch_http(url: str, *, timeout: float) -> tuple[int, bytes]:
    """Read and close an HTTP response inside the deadline-controlled child."""
    transport_failure = PubanonError(
        'package-index transport failed',
        fact='reading a package release failed; cause unknown',
    )
    try:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
                return response.status, response.read()
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read()
    except TimeoutError as exc:
        raise ObservationTimeoutError from exc
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, TimeoutError):
            raise ObservationTimeoutError from exc
        raise transport_failure from exc
    except (OSError, http.client.HTTPException) as exc:
        raise transport_failure from exc


def _fetch_remaining(deadline: float) -> float:
    """Return the positive remaining fetch budget or signal its expiry."""
    remaining = deadline - _monotonic()
    if remaining <= 0:
        raise ObservationTimeoutError
    return remaining


def _fetch(url: str, *, timeout: float | None = None) -> tuple[int, bytes]:
    """Bound complete HTTP work by one elapsed deadline and reap the child."""
    budget = _PYPI_TIMEOUT if timeout is None else min(timeout, _PYPI_TIMEOUT)
    deadline = _monotonic() + budget
    _fetch_remaining(deadline)
    request = json.dumps({'url': url, 'timeout': budget}).encode('utf-8')
    try:
        with subprocess.Popen(  # noqa: S603
            _fetch_command(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
        ) as process:
            try:
                stdout, _stderr = process.communicate(request, timeout=_fetch_remaining(deadline))
            except BaseException as exc:
                if process.poll() is None:
                    process.kill()
                process.communicate()
                if isinstance(exc, subprocess.TimeoutExpired):
                    raise ObservationTimeoutError from exc
                raise
            _fetch_remaining(deadline)
            if process.returncode == _FETCH_TIMEOUT:
                raise ObservationTimeoutError
            if process.returncode != _FETCH_SUCCESS:
                raise PubanonError(
                    'package-index transport failed',
                    fact='reading a package release failed; cause unknown',
                )
            result = _decode_fetch_result(stdout)
            _fetch_remaining(deadline)
            return result
    except OSError as exc:
        raise PubanonError(
            'package-index transport failed',
            fact='reading a package release failed; cause unknown',
        ) from exc


def _normalize_name(name: str) -> str:
    """Normalize a package name for package-index comparison."""
    return re.sub(r'[-_.]+', '-', name).lower()


def _reject_constant(_constant: str) -> Never:
    """Reject nonstandard JSON numeric constants."""
    raise ValueError


def _fetch_main() -> int:
    """Read one request, perform HTTP work, and emit the private worker protocol."""
    try:
        parsed = json.loads(sys.stdin.buffer.read(), parse_constant=_reject_constant)
        if not isinstance(parsed, dict):
            return _FETCH_FAILURE
        request = cast('dict[object, object]', parsed)
        url, timeout = request.get('url'), request.get('timeout')
        if (
            not isinstance(url, str)
            or isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            return _FETCH_FAILURE
        status, body = _fetch_http(url, timeout=timeout)
        sys.stdout.buffer.write(
            json.dumps([status, base64.b64encode(body).decode('ascii')]).encode('utf-8')
        )
    except ObservationTimeoutError:
        return _FETCH_TIMEOUT
    except (PubanonError, OSError, ValueError, OverflowError):
        return _FETCH_FAILURE
    return _FETCH_SUCCESS


def _parse_release(body: bytes, version: str) -> PackageRelease:
    """Parse and validate an exact package release response."""
    invalid_response = PubanonError('invalid package-index response')
    try:
        parsed = json.loads(body, parse_constant=_reject_constant)
    except ValueError as exc:
        raise invalid_response from exc
    if not isinstance(parsed, dict):
        raise invalid_response
    payload = cast('dict[object, object]', parsed)
    info = payload.get('info')
    urls = payload.get('urls')
    if not isinstance(info, dict) or not isinstance(urls, list):
        raise invalid_response
    if cast('dict[object, object]', info).get('version') != version:
        raise invalid_response

    files: list[str] = []
    for raw_entry in cast('list[object]', urls):
        if not isinstance(raw_entry, dict):
            raise invalid_response
        filename = cast('dict[object, object]', raw_entry).get('filename')
        if not isinstance(filename, str):
            raise invalid_response
        files.append(filename)
    return PackageRelease(version, tuple(files))


def release_state(
    index: str,
    name: str,
    version: str,
    *,
    timeout: float | None = None,
) -> PackageRelease | None:
    """Read an exact release from a supported package index.

    Args:
        index: Package-index adapter; currently only `pypi` is supported.
        name: Distribution name, normalized to lowercase with runs of hyphens,
            underscores, and periods replaced by one hyphen.
        version: Exact release version.

        timeout: Complete fetch budget in seconds, capped at 30 seconds.
            `None` uses 30 seconds. Process scheduling and cleanup can add
            overhead; a response completed after the deadline is refused.

    Returns:
        The release metadata, including an empty file tuple while no
        distribution files are listed, or `None` for HTTP 404.

    Raises:
        PubanonError: The index is unsupported, transport fails, or its
            response is invalid.
        ObservationTimeoutError: The transport times out, including under the
            default timeout.
    """
    if index != 'pypi':
        raise PubanonError(escape(f'unsupported package index: {index}'))

    normalized_name = _normalize_name(name)
    url = _PYPI_RELEASE_URL.format(name=normalized_name, version=version)
    try:
        status, body = _fetch(url, timeout=timeout)
    except PubanonError as exc:
        raise PubanonError(
            f'Could not check PyPI release {escape(normalized_name)} {escape(version)}',
            fact='The release status is unknown because the PyPI request failed.',
            recovery='Check access to PyPI, then retry the package check.',
        ) from exc

    if status == _HTTP_NOT_FOUND:
        return None
    if status != _HTTP_OK:
        raise PubanonError(
            f'Could not check PyPI release {escape(normalized_name)} {escape(version)}',
            fact=f'PyPI returned HTTP {status}; the release status is unknown.',
            recovery='Retry the package check after PyPI responds normally.',
        )
    try:
        return _parse_release(body, version)
    except PubanonError as exc:
        raise PubanonError(
            f'PyPI returned an invalid package-index response for '
            f'{escape(normalized_name)} {escape(version)}',
            fact='The release status is unknown because the response could not be read.',
            recovery=(
                'Retry the package check; if this persists, inspect the PyPI release metadata.'
            ),
        ) from exc


if __name__ == '__main__':
    raise SystemExit(_fetch_main())
