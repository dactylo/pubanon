import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from pubanon import _run as _run_module
from pubanon._errors import ObservationTimeoutError, PubanonError
from pubanon._run import check_floors, run_credential_child, run_git
from tests._git import git_env


def _exception_graph(error: BaseException) -> tuple[BaseException, ...]:
    graph: list[BaseException] = []
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        graph.append(current)
        pending.extend(
            linked for linked in (current.__cause__, current.__context__) if linked is not None
        )
    return tuple(graph)


def _patch_git_version_result(
    monkeypatch: pytest.MonkeyPatch,
    completed: subprocess.CompletedProcess[bytes],
) -> None:
    def _run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return completed

    monkeypatch.setattr(_run_module.subprocess, 'run', _run)


def test_check_floors_accepts_minimum_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_run_module.sys, 'version_info', (3, 13, 0))
    _patch_git_version_result(
        monkeypatch,
        subprocess.CompletedProcess(
            ['git', '--version'],
            0,
            stdout=b'git version 2.41.0\n',
            stderr=b'',
        ),
    )

    check_floors()


def test_check_floors_accepts_real_environment() -> None:
    check_floors()


def test_check_floors_rejects_malformed_git_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_run_module.sys, 'version_info', (3, 13, 0))
    _patch_git_version_result(
        monkeypatch,
        subprocess.CompletedProcess(
            ['git', '--version'],
            0,
            stdout=b'not a Git version\n',
            stderr=b'',
        ),
    )

    with pytest.raises(PubanonError) as exc_info:
        check_floors()

    assert str(exc_info.value) == 'Git 2.41.0 or newer is required'
    assert exc_info.value.recovery is None


def test_check_floors_rejects_old_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_run_module.sys, 'version_info', (3, 13, 0))
    _patch_git_version_result(
        monkeypatch,
        subprocess.CompletedProcess(
            ['git', '--version'],
            0,
            stdout=b'git version 2.40.9\n',
            stderr=b'',
        ),
    )

    with pytest.raises(PubanonError) as exc_info:
        check_floors()

    assert str(exc_info.value) == 'Git 2.41.0 or newer is required'
    assert exc_info.value.recovery is None


def test_check_floors_rejects_old_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_run_module.sys, 'version_info', (3, 12, 9))
    _patch_git_version_result(
        monkeypatch,
        subprocess.CompletedProcess(
            ['git', '--version'],
            0,
            stdout=b'git version 2.41.0\n',
            stderr=b'',
        ),
    )

    with pytest.raises(PubanonError) as exc_info:
        check_floors()

    assert str(exc_info.value) == 'Python 3.13 or newer is required'
    assert exc_info.value.recovery is None


def test_check_floors_rejects_unsuccessful_git(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_run_module.sys, 'version_info', (3, 13, 0))
    _patch_git_version_result(
        monkeypatch,
        subprocess.CompletedProcess(
            ['git', '--version'],
            1,
            stdout=b'git version 2.41.0\n',
            stderr=b'Git failed\n',
        ),
    )

    with pytest.raises(PubanonError) as exc_info:
        check_floors()

    assert str(exc_info.value) == 'Git 2.41.0 or newer is required'
    assert exc_info.value.recovery is None


@pytest.mark.parametrize('credential_bearing', [False, True])
def test_command_iterator_preserves_argument_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    credential_bearing: bool,
) -> None:
    calls: list[list[str]] = []

    def _capture(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout=b'ordered output', stderr=b'')

    monkeypatch.setattr(_run_module.subprocess, 'run', _capture)
    arguments = iter(('show', '--format=%s', 'HEAD'))
    if credential_bearing:
        result = run_credential_child(arguments, cwd=tmp_path)
    else:
        result = run_git(arguments, cwd=tmp_path)

    assert calls == [
        [
            'git',
            '-C',
            str(tmp_path),
            '-c',
            f'core.hooksPath={os.devnull}',
            'show',
            '--format=%s',
            'HEAD',
        ]
    ]
    assert result == _run_module.GitResult(0, b'ordered output', b'')


def test_credential_child_failure_reports_escaped_git_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _failing(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            ['git'], 128, b'', b'fatal: could not read \x1b[31mremote\\path\n'
        )

    monkeypatch.setattr(subprocess, 'run', _failing)
    with pytest.raises(PubanonError, match='Git push failed') as error:
        _run_module.run_credential_child(['push'], cwd=None, operation='push')
    assert error.value.fact == 'exit status 128: fatal: could not read \\x1b[31mremote\\\\path'
    assert '\x1b' not in error.value.render()


def test_credential_child_reports_failure_stderr(
    make_repo: Callable[..., Path],
) -> None:
    repo = make_repo()
    alias = '!echo stdout-secret; echo stderr-secret >&2; exit 1'

    with pytest.raises(PubanonError) as exc_info:
        run_credential_child(['-c', f'alias.leak={alias}', 'leak'], cwd=repo)

    message = str(exc_info.value)
    assert message == 'Git transport failed'
    assert exc_info.value.fact == 'exit status 1: stderr-secret'
    assert 'stdout-secret' not in message
    assert 'stderr-secret' in exc_info.value.render()


def test_credential_child_timeout_discards_both_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[float | None] = []

    def _timeout_run(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        timeout = kwargs.get('timeout')
        assert isinstance(timeout, float)
        calls.append(timeout)
        raise subprocess.TimeoutExpired(
            'git',
            timeout,
            output=b'stdout-private-canary',
            stderr=b'stderr-private-canary',
        )

    monkeypatch.setattr(_run_module.subprocess, 'run', _timeout_run)

    with pytest.raises(ObservationTimeoutError) as exc_info:
        run_credential_child(['ls-remote', 'example'], cwd=None, timeout=3.25)

    assert type(exc_info.value).__name__ == 'ObservationTimeoutError'
    assert exc_info.value._detail is None
    assert exc_info.value.stdout == b''
    assert exc_info.value.stderr == b''
    graph = _exception_graph(exc_info.value)
    assert graph == (exc_info.value,)
    assert 'private-canary' not in repr([vars(error) for error in graph])
    assert calls == [3.25]


def test_git_spawn_failure_is_a_domain_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_run_module, '_GIT', '/nonexistent/pubanon-test-git')

    with pytest.raises(PubanonError, match='cannot run Git'):
        run_git(['version'], cwd=None)
    with pytest.raises(PubanonError, match='Git transport could not start'):
        run_credential_child(['version'], cwd=None)


def test_run_git_ignores_global_config(
    make_repo: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = make_repo()
    home = tmp_path / 'home'
    home.mkdir()
    (home / '.gitconfig').write_text(
        '[core]\n    hooksPath = /poisoned/hooks\n[user]\n    name = Poisoned User\n'
    )
    monkeypatch.setenv('HOME', str(home))

    result = run_git(['config', 'user.name'], cwd=repo, check=False)

    assert result.stdout == b''


def test_run_git_ignores_inherited_git_dir(
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected = make_repo('selected', files={'selected.txt': 'selected'})
    decoy = make_repo('decoy', files={'decoy.txt': 'decoy'})
    selected_oid = subprocess.run(
        ['git', '-C', selected, 'rev-parse', 'HEAD'],
        check=True,
        capture_output=True,
        env=git_env(),
    ).stdout.strip()
    decoy_oid = subprocess.run(
        ['git', '-C', decoy, 'rev-parse', 'HEAD'],
        check=True,
        capture_output=True,
        env=git_env(),
    ).stdout.strip()
    monkeypatch.setenv('GIT_DIR', str(decoy / '.git'))

    result = run_git(['rev-parse', 'HEAD'], cwd=selected)

    assert result.stdout.strip() == selected_oid
    assert result.stdout.strip() != decoy_oid


def test_run_git_merge_ignores_global_config(
    make_repo: Callable[..., Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(files={'f': 'base\n', '.gitattributes': 'f merge=hostile\n'})
    base = run_git(['rev-parse', 'HEAD'], cwd=repo).stdout.decode().strip()
    commits: list[str] = []
    for content in ('ours\n', 'theirs\n'):
        subprocess.run(
            ['git', '-C', repo, 'reset', '--hard', base],
            check=True,
            capture_output=True,
            env=git_env(),
        )
        (repo / 'f').write_text(content)
        subprocess.run(
            ['git', '-C', repo, 'commit', '-am', 'fixture'],
            check=True,
            capture_output=True,
            env=git_env(),
        )
        commits.append(run_git(['rev-parse', 'HEAD'], cwd=repo).stdout.decode().strip())
    args = ['merge-tree', '--write-tree', '--merge-base', base, '-z', '--messages', *commits]
    baseline = run_git(args, cwd=repo, check=False)
    hostile = tmp_path / 'hostile.gitconfig'
    hostile.write_text('[merge "hostile"]\n    driver = true\n')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(hostile))
    wrapped = run_git(args, cwd=repo, check=False)
    control = subprocess.run(
        ['git', '-C', repo, *args],
        check=False,
        capture_output=True,
        env=git_env({'GIT_CONFIG_GLOBAL': str(hostile)}),
    )
    assert baseline.returncode == 1
    assert wrapped.returncode == baseline.returncode
    assert wrapped.stdout.split(b'\0')[0] == baseline.stdout.split(b'\0')[0]
    assert control.returncode == 0
    assert control.stdout.split(b'\0')[0] != baseline.stdout.split(b'\0')[0]


def test_run_git_reports_failure_output(make_repo: Callable[..., Path]) -> None:
    repo = make_repo()

    with pytest.raises(PubanonError) as exc_info:
        run_git(['show', 'canaryx'], cwd=repo)

    message = str(exc_info.value)
    assert f'{"canaryx"}' in message
    assert 'canaryx' in message.casefold()


def test_run_git_reports_filesystem_terms_and_escapes_unprintable_path(tmp_path: Path) -> None:
    if os.name == 'nt':
        missing = tmp_path / 'canaryx-\u202e'
        escaped = r'\u202e'
    else:
        missing = tmp_path / os.fsdecode(b'canaryx-\xff')
        escaped = r'\udcff'

    with pytest.raises(PubanonError) as exc_info:
        run_git(['status'], cwd=missing)

    message = str(exc_info.value)
    assert f'{"canaryx"}' in message
    assert 'canaryx' in message.casefold()
    assert escaped in message
    assert '\ufffd' not in message


@pytest.mark.parametrize('stderr', [b'', b'diagnostic\x1b\\path'])
def test_run_git_reports_status_and_escaped_stderr(
    monkeypatch: pytest.MonkeyPatch, stderr: bytes
) -> None:
    _patch_git_version_result(monkeypatch, subprocess.CompletedProcess(['git'], 7, b'', stderr))
    with pytest.raises(PubanonError) as error:
        run_git(['status'], cwd=None)
    assert error.value.fact == (
        r'exit status 7: diagnostic\x1b\\path' if stderr else 'exit status 7'
    )


def test_run_git_skips_repository_reference_hook(
    make_repo: Callable[..., Path],
) -> None:
    repo = make_repo()
    commit = (
        subprocess.run(
            ['git', '-C', repo, 'rev-parse', 'HEAD'],
            check=True,
            capture_output=True,
            env=git_env(),
        )
        .stdout.strip()
        .decode()
    )
    marker = repo / 'hook-invoked'
    hook = repo / '.git' / 'hooks' / 'reference-transaction'
    hook.parent.mkdir(exist_ok=True)
    hook.write_text('#!/bin/sh\nprintf invoked > hook-invoked\nexit 1\n')
    hook.chmod(0o755)

    result = run_git(['update-ref', 'refs/test/internal', commit], cwd=repo, check=False)
    assert result.returncode == 0
    assert not marker.exists()

    ordinary = subprocess.run(
        ['git', '-C', repo, 'update-ref', 'refs/test/ordinary', commit],
        check=False,
        capture_output=True,
        env=git_env({'GIT_CONFIG_COUNT': '0'}),
    )
    assert ordinary.returncode != 0
    assert marker.read_text() == 'invoked'


def test_run_git_timeout_keeps_only_safely_rendered_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[float | None] = []

    def _timeout_run(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        timeout = kwargs.get('timeout')
        assert isinstance(timeout, float)
        calls.append(timeout)
        raise subprocess.TimeoutExpired(
            'git',
            timeout,
            output=b'stdout-private-canary-\xff',
            stderr=b'stderr-private-canary',
        )

    monkeypatch.setattr(_run_module.subprocess, 'run', _timeout_run)

    with pytest.raises(ObservationTimeoutError) as exc_info:
        run_git(['ls-remote', 'example'], cwd=None, timeout=4.5)

    assert type(exc_info.value).__name__ == 'ObservationTimeoutError'
    assert exc_info.value._detail == f'stdout-{"private-canary"}-\\udcffstderr-{"private-canary"}'
    assert exc_info.value.stdout == b''
    assert exc_info.value.stderr == b''
    graph = _exception_graph(exc_info.value)
    assert graph == (exc_info.value,)
    assert 'private-canary' in (exc_info.value._detail or '')
    assert calls == [4.5]
