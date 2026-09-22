"""Run Git commands within the Pubanon execution boundary."""

import os
import re
import subprocess
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Literal, NamedTuple

from pubanon._errors import ObservationTimeoutError, OperationCancelled, PubanonError
from pubanon._terminal import escape

_GIT = 'git'
_GIT_FLOOR = (2, 41, 0)
_GIT_ISOLATION: dict[str, str] = {
    'GIT_CONFIG_GLOBAL': os.devnull,
    'GIT_CONFIG_SYSTEM': os.devnull,
    'GIT_CONFIG_NOSYSTEM': '1',
    'GIT_TERMINAL_PROMPT': '0',
}
OID_HEX = '[0-9a-f]{40}|[0-9a-f]{64}'
_PYTHON_FLOOR = (3, 13)


class GitResult(NamedTuple):
    """Capture a completed Git command's result.

    Attributes:
        returncode: Exit status reported by the Git process.
        stdout: Bytes captured from standard output.
        stderr: Bytes captured from standard error.
    """

    returncode: int
    stdout: bytes
    stderr: bytes


def _run_git(
    args: Iterable[str],
    *,
    cwd: Path | None,
    input_bytes: bytes | None,
    env_extra: Mapping[str, str] | None,
    timeout: float | None = None,
) -> GitResult:
    """Run Git with isolated configuration and captured byte streams."""
    env = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    env = env | _GIT_ISOLATION | dict(env_extra or {})
    try:
        completed = subprocess.run(  # noqa: S603
            [
                _GIT,
                *(('-C', str(cwd)) if cwd is not None else ()),
                '-c',
                f'core.hooksPath={os.devnull}',
                *args,
            ],
            check=False,
            capture_output=True,
            env=env,
            input=input_bytes,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, bytes) else b''
        stderr = exc.stderr if isinstance(exc.stderr, bytes) else b''
    except OSError as exc:
        raise PubanonError('cannot run Git') from exc
    else:
        return GitResult(completed.returncode, completed.stdout, completed.stderr)
    raise ObservationTimeoutError(stdout=stdout, stderr=stderr)


def check_floors() -> None:
    """Require supported Python and Git versions.

    Raises:
        PubanonError: Python is outdated, or Git cannot be executed or
            identified at a supported version.
    """
    if sys.version_info < _PYTHON_FLOOR:
        raise PubanonError('Python 3.13 or newer is required')

    try:
        completed = subprocess.run(  # noqa: S603
            [_GIT, '--version'],
            check=False,
            capture_output=True,
        )
    except OSError as exc:
        raise PubanonError('Git 2.41.0 or newer is required') from exc
    match = re.search(rb'git version (\d+)\.(\d+)\.(\d+)', completed.stdout)
    if completed.returncode != 0 or match is None:
        raise PubanonError('Git 2.41.0 or newer is required')
    version = tuple(int(part) for part in match.groups())
    if version < _GIT_FLOOR:
        raise PubanonError('Git 2.41.0 or newer is required')


def git_failure_fact(result: GitResult) -> str:
    """Return the exit status and terminal-escaped stderr of a Git result."""
    stderr = result.stderr.decode(errors='surrogateescape').strip()
    fact = f'exit status {result.returncode}'
    if stderr:
        fact += f': {escape(stderr)}'
    return fact


def run_credential_child(
    args: Iterable[str],
    *,
    cwd: Path | None,
    env_extra: Mapping[str, str] | None = None,
    timeout: float | None = None,
    operation: Literal['fetch', 'push', 'ls-remote', 'transport'] = 'transport',
) -> GitResult:
    """Run credential-bearing Git with escaped failure diagnostics.

    Args:
        args: Git arguments after the executable and optional repository
            selector.
        cwd: Repository selected with Git's `-C` option, or no repository.
        env_extra: Explicit child-environment additions or overrides.
        timeout: Optional maximum child duration in seconds.
        operation: Safe operation label supplied by the transport owner.

    Returns:
        The successful Git exit status and captured byte streams.

    Raises:
        PubanonError: Git cannot start or exits unsuccessfully.
        ObservationTimeoutError: The supplied child timeout expires. Captured
            streams and the originating exception context are discarded.
    """
    try:
        result = _run_git(
            args,
            cwd=cwd,
            input_bytes=None,
            env_extra=env_extra,
            timeout=timeout,
        )
    except ObservationTimeoutError:
        pass
    except OperationCancelled:
        raise
    except PubanonError as exc:
        raise type(exc)(
            f'Git {operation} could not start',
            fact=exc.fact,
            expected=exc.expected,
            actual=exc.actual,
            recovery=exc.recovery,
        ) from exc
    else:
        if result.returncode != 0:
            raise PubanonError(
                f'Git {operation} failed',
                fact=git_failure_fact(result),
            )
        return result
    raise ObservationTimeoutError


def run_git(  # noqa: PLR0913
    args: Iterable[str],
    *,
    cwd: Path | None,
    input_bytes: bytes | None = None,
    env_extra: Mapping[str, str] | None = None,
    check: bool = True,
    timeout: float | None = None,
) -> GitResult:
    """Run Git with inherited Git configuration removed.

    Args:
        args: Git arguments after the executable and optional repository
            selector.
        cwd: Repository selected with Git's `-C` option, or no repository.

        input_bytes: Optional bytes provided on standard input.
        env_extra: Explicit child-environment additions or overrides.
        check: Whether to raise when Git exits unsuccessfully. Launch failures
            always raise.
        timeout: Optional maximum child duration in seconds.

    Returns:
        The Git exit status and captured byte streams.

    Raises:
        PubanonError: Git cannot start, or Git exits unsuccessfully while
            `check` is true.
        ObservationTimeoutError: The supplied child timeout expires. Only
            escaped display detail is preserved; raw captured streams are not.
    """
    try:
        result = _run_git(
            args,
            cwd=cwd,
            input_bytes=input_bytes,
            env_extra=env_extra,
            timeout=timeout,
        )
    except ObservationTimeoutError as exc:
        output = exc.stdout.decode(errors='surrogateescape') + exc.stderr.decode(
            errors='surrogateescape'
        )
        detail = escape(output).strip() or None
    else:
        if check and result.returncode != 0:
            output = result.stdout.decode(errors='surrogateescape') + result.stderr.decode(
                errors='surrogateescape'
            )
            raise PubanonError(
                escape(output).strip() or 'Git failed', fact=git_failure_fact(result)
            )
        return result
    raise ObservationTimeoutError(detail=detail)
