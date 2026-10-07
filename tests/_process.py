import contextlib
import os
import re
import signal
import subprocess
import sys
import threading
from collections.abc import Mapping, Sequence
from typing import Protocol

# Hang guard, not a performance assertion. Allow for scheduling on shared CI hosts.
CHILD_TIMEOUT_SECONDS = 300
_COLLECT_AFTER_KILL_SECONDS = 10
_CONTROL_CHARACTERS = re.compile(r'[\x00-\x08\x0b-\x1f\x7f-\x9f]')
PROCESS_GROUP_FLAGS = subprocess.CREATE_NEW_PROCESS_GROUP if sys.platform == 'win32' else 0
_TRANSCRIPT_FIELD_CHARACTERS = 2048
_TRANSCRIPT_HEAD_BYTES = 2048
_TRANSCRIPT_TAIL_BYTES = 6144


class _Outcome(Protocol):
    @property
    def args(self) -> object: ...

    @property
    def returncode(self) -> int | None: ...

    @property
    def stdout(self) -> str | bytes | None: ...

    @property
    def stderr(self) -> str | bytes | None: ...


def _describe_status(returncode: int | None) -> str:
    if returncode is None:
        return 'not collected'
    if returncode < 0:
        try:
            return f'{returncode} ({signal.Signals(-returncode).name})'
        except ValueError:
            return str(returncode)
    return str(returncode)


def _escape_transcript_line(line: str) -> str:
    line = _CONTROL_CHARACTERS.sub(lambda match: f'\\x{ord(match.group()):02x}', line)
    line = line.replace('##[', '#\\#[')
    body = line.lstrip()
    if body.startswith('::'):
        line = line[: len(line) - len(body)] + '\\' + body
    return line


def _terminate_tree[T: (str, bytes)](process: subprocess.Popen[T]) -> None:
    if sys.platform == 'win32':
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            subprocess.run(
                ['taskkill', '/F', '/T', '/PID', str(process.pid)],
                capture_output=True,
                timeout=_COLLECT_AFTER_KILL_SECONDS,
                check=False,
            )
    else:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(process.pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        process.kill()


def _transcript_field(label: str, value: object) -> str:
    field = repr(value)
    if len(field) > _TRANSCRIPT_FIELD_CHARACTERS:
        omitted = len(field) - _TRANSCRIPT_FIELD_CHARACTERS
        field = f'{field[:_TRANSCRIPT_FIELD_CHARACTERS]} [... {omitted} characters omitted ...]'
    return f'{label}: {_escape_transcript_line(field)}'


def _transcript_stream(label: str, data: str | bytes | None) -> list[str]:
    if data is None:
        return [f'{label}: <not collected>']
    raw = data if isinstance(data, bytes) else data.encode('utf-8', 'backslashreplace')
    if not raw:
        return [f'{label}: <empty>']
    if len(raw) > _TRANSCRIPT_HEAD_BYTES + _TRANSCRIPT_TAIL_BYTES:
        omitted = len(raw) - _TRANSCRIPT_HEAD_BYTES - _TRANSCRIPT_TAIL_BYTES
        field = (
            raw[:_TRANSCRIPT_HEAD_BYTES].decode('utf-8', 'backslashreplace')
            + f'\n[... {omitted} bytes omitted ...]\n'
            + raw[-_TRANSCRIPT_TAIL_BYTES:].decode('utf-8', 'backslashreplace')
        )
    else:
        field = raw.decode('utf-8', 'backslashreplace')
    body = field.replace('\r\n', '\n').removesuffix('\n')
    return [f'{label}:', *(f'  | {_escape_transcript_line(line)}' for line in body.split('\n'))]


def process_transcript(result: _Outcome, *, note: str | None = None) -> str:
    lines = [_transcript_field('command', result.args)]
    if note is not None:
        lines.append(f'note: {_escape_transcript_line(note)}')
    lines.append(f'status: {_describe_status(result.returncode)}')
    lines.extend(_transcript_stream('stdout', result.stdout))
    lines.extend(_transcript_stream('stderr', result.stderr))
    return '\n'.join(lines)


def _kill_and_describe[T: (str, bytes)](process: subprocess.Popen[T], *, reason: str) -> str:
    _terminate_tree(process)
    try:
        stdout, stderr = process.communicate(timeout=_COLLECT_AFTER_KILL_SECONDS)
    except subprocess.TimeoutExpired:
        stdout = stderr = None
        reason = f'{reason}; a descendant outside the process group still holds the output pipes'
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=_COLLECT_AFTER_KILL_SECONDS)
    killed = subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)
    return process_transcript(killed, note=reason)


def finish_process[T: (str, bytes)](
    process: subprocess.Popen[T],
    *,
    timeout: float = CHILD_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[T]:
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        raise AssertionError(
            _kill_and_describe(process, reason=f'timed out after {timeout:g} s')
        ) from None
    return subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr)


def run_closing_after_first_line(
    arguments: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = CHILD_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    process = subprocess.Popen(
        [sys.executable, *arguments],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=None if env is None else dict(env),
        start_new_session=True,
        creationflags=PROCESS_GROUP_FLAGS,
    )
    stdout = process.stdout
    assert stdout is not None
    descriptor = stdout.fileno()

    def read_first_line() -> None:
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk or b'\n' in chunk:
                return

    reader = threading.Thread(target=read_first_line, daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        raise AssertionError(
            _kill_and_describe(process, reason=f'no stdout line within {timeout:g} s')
        )
    stdout.close()
    process.stdout = None  # communicate() must not read the closed pipe on Windows
    return finish_process(process, timeout=timeout)


def run_with_closed_output(
    arguments: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = CHILD_TIMEOUT_SECONDS,
) -> subprocess.CompletedProcess[bytes]:
    read_fd, write_fd = os.pipe()
    os.close(read_fd)  # every write to stdout happens after its reader closed
    try:
        process = subprocess.Popen(
            [sys.executable, *arguments],
            stdin=subprocess.DEVNULL,
            stdout=write_fd,
            stderr=subprocess.PIPE,
            env=None if env is None else dict(env),
            start_new_session=True,
            creationflags=PROCESS_GROUP_FLAGS,
        )
    finally:
        os.close(write_fd)
    return finish_process(process, timeout=timeout)
