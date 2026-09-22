import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path, PureWindowsPath
from typing import NotRequired, TypedDict, Unpack

import pytest

from pubanon._errors import PubanonError
from pubanon._run import GitResult
from pubanon._snapshots import _source
from pubanon._snapshots._source import check_source_safety
from pubanon._terminal import escape
from tests._git import git_env


class _RunGitOptions(TypedDict):
    check: NotRequired[bool]
    cwd: Path | None
    env_extra: NotRequired[Mapping[str, str] | None]
    input_bytes: NotRequired[bytes | None]


def _git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        ['git', '-C', repo, *args],
        check=True,
        capture_output=True,
        input=input_bytes,
        env=git_env(),
    ).stdout


def _commit_file(repo: Path, path: str, content: str) -> str:
    (repo / path).write_text(content)
    _git(repo, 'add', path)
    return (
        _git(
            repo,
            '-c',
            'user.name=Test User',
            '-c',
            'user.email=test@example.invalid',
            'commit',
            '-qm',
            f'add {path}',
        )
        .strip()
        .decode()
    )


def _commit_tree(repo: Path, records: bytes) -> str:
    tree = _git(repo, 'mktree', '-z', input_bytes=records).strip().decode()
    return (
        _git(
            repo,
            '-c',
            'user.name=Test User',
            '-c',
            'user.email=test@example.invalid',
            'commit-tree',
            tree,
            '-m',
            'snapshot',
        )
        .strip()
        .decode()
    )


def _git_out(repo: Path, *args: str) -> str:
    return subprocess.run(
        ['git', '-C', repo, *args],
        check=True,
        capture_output=True,
        text=True,
        env=git_env(),
    ).stdout


def _refs(repo: Path) -> dict[str, str]:
    output = _git_out(
        repo, 'for-each-ref', '--format=%(refname) %(objectname)', 'refs/heads/', 'refs/tags/'
    )
    return dict(line.split(' ', maxsplit=1) for line in output.splitlines())


def _source_with_branches(make_repo: Callable[..., Path]) -> Path:
    source = make_repo('backup-source', files={'README.md': 'main\n'})
    env = git_env()
    identity = ['-c', 'user.name=Test User', '-c', 'user.email=test@example.invalid']
    subprocess.run(['git', '-C', source, 'branch', '-m', 'main'], check=True, env=env)
    subprocess.run(['git', '-C', source, 'tag', 'v0.1.0'], check=True, env=env)
    subprocess.run(['git', '-C', source, 'checkout', '-q', '-b', 'topic'], check=True, env=env)
    (source / 'topic.txt').write_text('topic\n')
    subprocess.run(['git', '-C', source, 'add', 'topic.txt'], check=True, env=env)
    subprocess.run(
        ['git', '-C', source, *identity, 'commit', '-qm', 'topic work'], check=True, env=env
    )
    subprocess.run(['git', '-C', source, 'checkout', '-q', 'main'], check=True, env=env)
    return source


@pytest.mark.parametrize(
    ('output', 'matches'),
    [
        (b'C:/source/.git\n', True),
        (b'C:\\source\\.git\n', False),
        (b'C:/source/.git', False),
        (b'C:/source/.git\nextra\n', False),
        (b'C:/source/.git\0\n', False),
    ],
)
def test_bookmark_common_dir_matches_unambiguous_git_path_output(
    output: bytes, matches: bool
) -> None:
    common = PureWindowsPath('C:/source/.git')

    assert _source._matches_git_path_output(output, common) is matches


def test_clone_restore_loses_secondary_branches_and_is_not_documented(
    tmp_path: Path, make_repo: Callable[..., Path]
) -> None:
    source = _source_with_branches(make_repo)
    bundle = tmp_path / 'backup.bundle'
    env = git_env()
    subprocess.run(
        ['git', '-C', source, 'bundle', 'create', str(bundle), '--all'], check=True, env=env
    )
    cloned = tmp_path / 'cloned'
    subprocess.run(['git', 'clone', '-q', str(bundle), str(cloned)], check=True, env=env)
    with pytest.raises(PubanonError, match='not safe to read'):
        check_source_safety(cloned)
    subprocess.run(['git', '-C', cloned, 'remote', 'remove', 'origin'], check=True, env=env)

    assert 'topic' not in _git_out(cloned, 'branch', '--list')


def test_documented_bundle_restore_preserves_branches_without_remote(
    tmp_path: Path, make_repo: Callable[..., Path]
) -> None:
    source = _source_with_branches(make_repo)
    commit = (
        _git(source, 'commit-tree', 'HEAD^{tree}', '-p', 'HEAD', '-m', 'bookmark-only revision')
        .strip()
        .decode()
    )
    bookmark = _source.describe_bookmark(source, 'example', 'a' * 40)
    _source.ensure_bookmark(bookmark, commit)
    bundle = tmp_path / 'backup.bundle'
    env = git_env()
    subprocess.run(
        ['git', '-C', source, 'bundle', 'create', str(bundle), '--all'], check=True, env=env
    )
    restored = tmp_path / 'restored'

    # The exact documented restore sequence from docs/operations.md.
    subprocess.run(
        ['git', 'init', '-q', '-b', 'restore-in-progress', restored], check=True, env=env
    )
    subprocess.run(
        [
            'git',
            '-C',
            restored,
            'fetch',
            '-q',
            str(bundle),
            'refs/heads/*:refs/heads/*',
            'refs/tags/*:refs/tags/*',
            'refs/pubanon/publications/*:refs/pubanon/publications/*',
        ],
        check=True,
        env=env,
    )
    subprocess.run(['git', '-C', restored, 'checkout', '-q', 'main'], check=True, env=env)

    assert _git_out(restored, 'remote') == ''
    assert _refs(restored) == _refs(source)
    assert 'restore-in-progress' not in _git_out(restored, 'branch', '--list')
    assert _git_out(restored, 'config', '--list', '--local').count('remote.') == 0
    check_source_safety(restored)
    assert _git_out(restored, 'rev-parse', bookmark.refname).strip() == commit
    source.rename(tmp_path / 'original-offline')
    recovery = _source.source_recovery(bookmark, commit, relocated_source=restored)
    assert recovery.status == 'protected'
    assert recovery.source_path == restored.resolve()


def test_pin_records_annotated_tag_name_and_original_oid(
    make_repo: Callable[..., Path],
) -> None:
    repo = make_repo()
    commit = _git(repo, 'rev-parse', 'HEAD').strip().decode()
    _git(
        repo,
        '-c',
        'user.name=Test User',
        '-c',
        'user.email=test@example.invalid',
        'tag',
        '-a',
        'release',
        '-m',
        'release',
    )
    tag_oid = _git(repo, 'rev-parse', 'refs/tags/release').strip().decode()

    pinned = _source.pin(repo, 'release')

    assert pinned == _source.PinnedSource(commit, 'ref', 'refs/tags/release', tag_oid)


def test_pin_records_detached_head_as_explicit(
    make_repo: Callable[..., Path],
) -> None:
    repo = make_repo()
    oid = _git(repo, 'rev-parse', 'HEAD').strip().decode()
    _git(repo, 'checkout', '--detach', '-q', oid)

    pinned = _source.pin(repo, 'HEAD')

    assert pinned == _source.PinnedSource(oid, 'explicit', None, None)


def test_pin_records_explicit_oid(make_repo: Callable[..., Path]) -> None:
    repo = make_repo()
    oid = _git(repo, 'rev-parse', 'HEAD').strip().decode()

    pinned = _source.pin(repo, oid)

    assert pinned.commit == oid
    assert pinned.selector_kind == 'explicit'
    assert pinned.selector_name is None
    assert pinned.selector_oid is None


def test_pin_records_parent_expression_as_explicit(
    make_repo: Callable[..., Path],
) -> None:
    repo = make_repo()
    parent = _git(repo, 'rev-parse', 'HEAD').strip().decode()
    _commit_file(repo, 'second.txt', 'second')

    pinned = _source.pin(repo, 'HEAD~1')

    assert pinned == _source.PinnedSource(parent, 'explicit', None, None)


def test_pin_records_ref_name_and_oid(make_repo: Callable[..., Path]) -> None:
    repo = make_repo()
    oid = _git(repo, 'rev-parse', 'HEAD').strip().decode()
    ref_name = _git(repo, 'symbolic-ref', 'HEAD').strip().decode()

    pinned = _source.pin(repo, 'HEAD')

    assert pinned.commit == oid
    assert pinned.selector_kind == 'ref'
    assert pinned.selector_name == ref_name
    assert pinned.selector_oid == oid


def test_pin_records_short_oid_as_explicit(make_repo: Callable[..., Path]) -> None:
    repo = make_repo()
    oid = _git(repo, 'rev-parse', 'HEAD').strip().decode()

    pinned = _source.pin(repo, oid[:12])

    assert pinned == _source.PinnedSource(oid, 'explicit', None, None)


def test_pin_reports_invalid_selector(make_repo: Callable[..., Path]) -> None:
    repo = make_repo()

    with pytest.raises(PubanonError) as exc_info:
        _source.pin(repo, 'canaryx')

    rendered = exc_info.value.render()
    assert str(exc_info.value) == "cannot resolve source selector 'canaryx'"
    assert exc_info.value.fact is not None
    status, _separator, detail = exc_info.value.fact.partition(': ')
    assert status == 'exit status 128'
    assert detail
    assert 'canaryx' in rendered.casefold()


def test_pin_reports_malformed_revision_stderr(
    make_repo: Callable[..., Path],
) -> None:
    repo = make_repo()

    with pytest.raises(PubanonError) as exc_info:
        _source.pin(repo, 'canaryx^{')

    rendered = exc_info.value.render()
    assert str(exc_info.value) == "cannot resolve source selector 'canaryx^{'"
    assert exc_info.value.fact is not None
    status, _separator, detail = exc_info.value.fact.partition(': ')
    assert status == 'exit status 128'
    assert detail
    assert 'canaryx' in rendered.casefold()


@pytest.mark.parametrize('boundary', ['ref', 'commit', 'explicit'])
def test_pin_reports_terminal_child_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    def _fail(args: Sequence[str], **_kwargs: object) -> GitResult:
        if '--symbolic-full-name' in args:
            return GitResult(0, b'' if boundary == 'explicit' else b'refs/heads/main\n', b'')
        if boundary == 'commit' and args[-1] == 'refs/heads/main':
            return GitResult(0, b'a' * 40 + b'\n', b'')
        return GitResult(7, b'', b'diagnostic\x1b\\path')

    monkeypatch.setattr(_source, 'run_git', _fail)
    with pytest.raises(PubanonError) as error:
        _source.pin(tmp_path, 'main')
    assert error.value.fact == r'exit status 7: diagnostic\x1b\\path'
    assert (
        str(error.value)
        == {
            'ref': 'Could not read source reference refs/heads/main',
            'commit': 'Could not read a commit from source reference refs/heads/main',
            'explicit': "cannot resolve source selector 'main'",
        }[boundary]
    )
    if boundary != 'explicit':
        assert error.value.recovery == (
            'Check that the selected branch or tag exists and points to an available '
            'commit, then retry preparation.'
        )
    else:
        assert error.value.recovery == (
            'Select an existing branch, tag, or commit and retry preparation.'
        )


def test_pin_terminal_renders_invalid_selector_without_changing_git_argument(
    make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo()
    selector = 'feature\nprivate-canary'
    seen: list[str] = []
    real_run_git = _source.run_git

    def _record_run(args: Sequence[str], **kwargs: Unpack[_RunGitOptions]) -> GitResult:
        seen.extend(args)
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(_source, 'run_git', _record_run)

    with pytest.raises(PubanonError) as exc_info:
        _source.pin(repo, selector)

    assert selector in seen
    assert str(exc_info.value) == "cannot resolve source selector 'feature\\x0aprivate-canary'"
    assert exc_info.value.fact is not None
    status, _separator, detail = exc_info.value.fact.partition(': ')
    assert status == 'exit status 128'
    assert detail


def test_read_snapshot_accepts_contained_parent_relative_link(
    make_repo: Callable[..., Path],
) -> None:
    repo = make_repo(
        'ok-link', files={'README.md': 'x'}, symlinks={'docs/current': '../README.md'}
    )
    commit = _git(repo, 'rev-parse', 'HEAD').strip().decode()
    snapshot = _source.read_snapshot(repo, commit)
    assert any(entry.kind == 'symlink' for entry in snapshot)


def test_read_snapshot_refuses_empty_symlink_target(
    make_repo: Callable[..., Path],
) -> None:
    repo = make_repo('empty-link')
    blob = _git(repo, 'hash-object', '-w', '--stdin', input_bytes=b'').strip().decode()
    commit = _commit_tree(repo, f'120000 blob {blob}\tlink\0'.encode())
    with pytest.raises(PubanonError, match='symlink'):
        _source.read_snapshot(repo, commit)


def test_read_snapshot_refuses_symlink_chain(make_repo: Callable[..., Path]) -> None:
    repo = make_repo('chain', files={'c': 'x'}, symlinks={'b': 'c', 'a': 'b'})
    commit = _git(repo, 'rev-parse', 'HEAD').strip().decode()
    with pytest.raises(PubanonError, match='symlink'):
        _source.read_snapshot(repo, commit)


def test_read_snapshot_selects_store_for_tree_and_blob_reads(
    make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_repo(files={'source.txt': 'source'})
    store = source / '.git'
    commit = _git(source, 'rev-parse', 'HEAD').decode().strip()
    expected = _source.read_snapshot(source, commit)
    calls: list[list[str]] = []
    original = _source.run_git

    def _record(args: Sequence[str], **kwargs: Unpack[_RunGitOptions]) -> GitResult:
        calls.append(list(args))
        return original(args, **kwargs)

    monkeypatch.chdir(source)
    monkeypatch.setattr(_source, 'run_git', _record)

    assert _source.read_snapshot(source, commit, git_dir=Path('.git')) == expected
    assert [args[:3] for args in calls] == [
        ['--git-dir', str(store), 'ls-tree'],
        ['--git-dir', str(store), 'cat-file'],
    ]


@pytest.mark.parametrize(
    'output',
    [b'', b'relative\n', b'/first\n/second\n', b'/root\0suffix\n'],
)
def test_repository_root_refuses_ambiguous_or_nonabsolute_git_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    output: bytes,
) -> None:
    def _fake_run_git(args: Sequence[str], **_kwargs: object) -> GitResult:
        assert args == ['rev-parse', '--show-toplevel']
        return GitResult(0, output, b'')

    monkeypatch.setattr(_source, 'run_git', _fake_run_git)

    with pytest.raises(PubanonError, match='canonical Git worktree root'):
        _source.repository_root(tmp_path)


@pytest.mark.parametrize('kind', ['bare', 'non-repository'])
def test_repository_root_refuses_unsupported_source(
    tmp_path: Path,
    kind: str,
) -> None:
    selected = tmp_path / kind
    if kind == 'bare':
        subprocess.run(['git', 'init', '--bare', '-q', selected], check=True, env=git_env())
    else:
        selected.mkdir()

    with pytest.raises(PubanonError, match='canonical Git worktree root'):
        _source.repository_root(selected)


def test_repository_root_reports_child_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail(_args: Sequence[str], **_kwargs: object) -> GitResult:
        return GitResult(7, b'', b'diagnostic\x1b\\path')

    monkeypatch.setattr(_source, 'run_git', _fail)
    with pytest.raises(PubanonError, match='cannot identify one canonical') as error:
        _source.repository_root(tmp_path)
    assert error.value.fact == r'exit status 7: diagnostic\x1b\\path'


def test_repository_root_returns_same_canonical_root_from_subdirectory(
    make_repo: Callable[..., Path],
) -> None:
    repo = make_repo(files={'nested/file.txt': 'content\n'})

    assert _source.repository_root(repo) == repo.resolve()
    assert _source.repository_root(repo / 'nested') == repo.resolve()


def test_snapshot_digest_changes_for_one_byte() -> None:
    original = (_source.Entry('file', 0o100644, 'blob', b'alpha'),)
    changed = (_source.Entry('file', 0o100644, 'blob', b'alphb'),)

    assert _source.snapshot_digest(original) != _source.snapshot_digest(changed)


def test_snapshot_digest_orders_records_by_path_bytes() -> None:
    first = _source.Entry('a', 0o100644, 'blob', b'a')
    second = _source.Entry('b', 0o100755, 'blob', b'b')

    assert _source.snapshot_digest((second, first)) == _source.snapshot_digest((first, second))


def test_snapshot_preserves_blob_bytes_and_exact_file_modes(
    make_repo: Callable[..., Path],
) -> None:
    repo = make_repo(files={'regular.bin': b'\x00\xff', 'script.sh': '#!/bin/sh\n'})
    _git(repo, 'update-index', '--chmod=+x', 'script.sh')
    _git(
        repo,
        '-c',
        'user.name=Test User',
        '-c',
        'user.email=test@example.invalid',
        'commit',
        '--amend',
        '--no-edit',
    )
    commit = _git(repo, 'rev-parse', 'HEAD').strip().decode()

    snapshot = _source.read_snapshot(repo, commit)

    assert snapshot == (
        _source.Entry('regular.bin', 0o100644, 'blob', b'\x00\xff'),
        _source.Entry('script.sh', 0o100755, 'blob', b'#!/bin/sh\n'),
    )


@pytest.mark.parametrize(
    'target',
    [
        'target.txt',
        pytest.param(
            'target-\udcff',
            marks=pytest.mark.skipif(
                os.name == 'nt', reason='Windows paths cannot encode surrogates'
            ),
        ),
    ],
)
def test_snapshot_preserves_in_tree_symlink_target_bytes(
    make_repo: Callable[..., Path],
    target: str,
) -> None:
    repo = make_repo(
        files={'nested/target.txt': 'content'},
        symlinks={'nested/link': target},
    )
    commit = _git(repo, 'rev-parse', 'HEAD').strip().decode()

    snapshot = _source.read_snapshot(repo, commit)

    link = next(entry for entry in snapshot if entry.path == 'nested/link')
    assert link.mode == 0o120000
    assert link.kind == 'symlink'
    assert link.data == target.encode('utf-8', errors='surrogateescape')


def test_snapshot_refuses_absolute_symlink(make_repo: Callable[..., Path]) -> None:
    repo = make_repo(symlinks={'link': '/canaryx/private'})
    commit = _git(repo, 'rev-parse', 'HEAD').strip().decode()

    with pytest.raises(PubanonError) as exc_info:
        _source.read_snapshot(repo, commit)

    rendered = exc_info.value.render()
    assert 'link' in rendered
    assert 'absolute' in rendered.casefold()


def test_snapshot_refuses_gitlink_with_both_recovery_choices(
    make_repo: Callable[..., Path],
) -> None:
    repo = make_repo()
    oid = _git(repo, 'rev-parse', 'HEAD').strip()
    commit = _commit_tree(repo, b'160000 commit ' + oid + b'\tvendor\0')

    with pytest.raises(PubanonError) as exc_info:
        _source.read_snapshot(repo, commit)

    rendered = exc_info.value.render()
    assert 'vendor' in rendered
    assert 'select a commit without the submodule' in rendered
    assert 'vendor its content' in rendered


def test_snapshot_refuses_mismatched_batch_object(
    make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo()
    expected_oid = b'a' * 40
    actual_oid = b'b' * 40

    def _fake_run_git(args: Sequence[str], **_kwargs: object) -> GitResult:
        if args[0] == 'ls-tree':
            return GitResult(0, b'100644 blob ' + expected_oid + b'\tprivate\0', b'')
        return GitResult(0, actual_oid + b' blob 4\ndata\n', b'')

    monkeypatch.setattr(_source, 'run_git', _fake_run_git)

    with pytest.raises(PubanonError, match='ambiguous object response'):
        _source.read_snapshot(repo, 'c' * 40)


def test_snapshot_refuses_non_utf8_tree_name(
    make_repo: Callable[..., Path],
) -> None:
    repo = make_repo()
    blob = _git(repo, 'hash-object', '-w', '--stdin', input_bytes=b'content').strip()
    commit = _commit_tree(repo, b'100644 blob ' + blob + b'\tbad-\xff.txt\0')

    with pytest.raises(PubanonError) as exc_info:
        _source.read_snapshot(repo, commit)

    rendered = exc_info.value.render()
    assert 'UTF-8' in rendered
    assert r'bad-\\xff.txt' in rendered


def test_snapshot_refuses_nul_containing_symlink_target(
    make_repo: Callable[..., Path],
) -> None:
    repo = make_repo()
    target_canary = b'canaryx-private-target'
    blob = _git(
        repo,
        'hash-object',
        '-w',
        '--stdin',
        input_bytes=target_canary + b'\0name',
    ).strip()
    commit = _commit_tree(repo, b'120000 blob ' + blob + b'\tlink\0')

    with pytest.raises(PubanonError) as exc_info:
        _source.read_snapshot(repo, commit)

    rendered = exc_info.value.render()
    assert 'invalid symlink target' in rendered
    assert 'fact: symlink: link' in rendered
    assert target_canary.decode() not in rendered
    assert 'replace the unsafe symlink and rerun' in rendered


def test_snapshot_refuses_root_escaping_symlink(
    make_repo: Callable[..., Path],
) -> None:
    repo = make_repo(symlinks={'nested/link': '../../canaryx-private'})
    commit = _git(repo, 'rev-parse', 'HEAD').strip().decode()

    with pytest.raises(PubanonError) as exc_info:
        _source.read_snapshot(repo, commit)

    rendered = exc_info.value.render()
    assert 'nested/link' in rendered
    assert 'escap' in rendered.casefold()


def test_snapshot_refuses_root_escaping_tree_path_with_visible_control(
    make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo()
    oid = b'a' * 40

    def _fake_run_git(args: Sequence[str], **_kwargs: object) -> GitResult:
        if args[0] == 'ls-tree':
            return GitResult(0, b'100644 blob ' + oid + b'\t../private\x1bname\0', b'')
        return GitResult(0, oid + b' blob 4\ndata\n', b'')

    monkeypatch.setattr(_source, 'run_git', _fake_run_git)

    with pytest.raises(PubanonError, match='malformed tree path') as exc_info:
        _source.read_snapshot(repo, 'b' * 40)

    assert exc_info.value.render().endswith(f'fact: entry: ../{"private"}\\x1bname')


def test_snapshot_refuses_unsupported_object_kind(
    make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo()
    oid = b'a' * 40

    def _fake_run_git(_args: Sequence[str], **_kwargs: object) -> GitResult:
        return GitResult(0, b'100644 tree ' + oid + b'\tprivate\0', b'')

    monkeypatch.setattr(_source, 'run_git', _fake_run_git)

    with pytest.raises(PubanonError, match='unsupported object kind'):
        _source.read_snapshot(repo, 'b' * 40)


def test_snapshot_reports_failed_batch_stderr(
    make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo('private-repository')
    oid = b'a' * 40
    private_document = b'unconfigured-private-document-canary'
    private_path = b'diagnostic\x1b\\path'

    def _fake_run_git(args: Sequence[str], **_kwargs: object) -> GitResult:
        if args[0] == 'ls-tree':
            return GitResult(0, b'100644 blob ' + oid + b'\tpublic.txt\0', b'')
        stdout = oid + b' blob 36\n' + private_document + b'\n'
        return GitResult(1, stdout, private_path)

    monkeypatch.setattr(_source, 'run_git', _fake_run_git)

    with pytest.raises(PubanonError) as exc_info:
        _source.read_snapshot(repo, 'b' * 40)

    rendered = exc_info.value.render()
    assert str(exc_info.value) == 'source object read failed'
    assert private_document.decode() not in rendered
    assert exc_info.value.fact == r'exit status 1: diagnostic\x1b\\path'
    assert str(repo) not in rendered


def test_snapshot_reports_failed_tree_stderr(
    make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo('private-repository')
    oid = b'a' * 40
    private_path = b'diagnostic\x1b\\path'

    def _fake_run_git(_args: Sequence[str], **_kwargs: object) -> GitResult:
        stdout = b'100644 blob ' + oid + b'\tunconfigured-private-name.txt\0'
        return GitResult(1, stdout, private_path)

    monkeypatch.setattr(_source, 'run_git', _fake_run_git)

    with pytest.raises(PubanonError) as exc_info:
        _source.read_snapshot(repo, 'b' * 40)

    rendered = exc_info.value.render()
    assert str(exc_info.value) == 'source tree read failed'
    assert 'unconfigured-private-name' not in rendered
    assert exc_info.value.fact == r'exit status 1: diagnostic\x1b\\path'
    assert str(repo) not in rendered


def test_snapshot_reports_unreadable_object_path(
    make_repo: Callable[..., Path],
) -> None:
    repo = make_repo('canaryx-private', files={'canaryx.txt': 'private'})
    commit = _git(repo, 'rev-parse', 'HEAD').strip().decode()
    blob = _git(repo, 'rev-parse', 'HEAD:canaryx.txt').strip().decode()
    object_path = repo / '.git' / 'objects' / blob[:2] / blob[2:]
    object_path.chmod(0o600)
    object_path.unlink()

    with pytest.raises(PubanonError) as exc_info:
        _source.read_snapshot(repo, commit)

    rendered = exc_info.value.render()
    assert 'source object could not be read' in rendered
    assert f'{"canaryx"}.txt' in rendered
    assert 'canaryx' in rendered.casefold()
    assert str(repo) not in rendered


@pytest.mark.parametrize('verification_fails', [False, True])
def test_source_alternates_recovery_preserves_branches_and_tags(
    tmp_path: Path, make_repo: Callable[..., Path], verification_fails: bool
) -> None:
    origin = _source_with_branches(make_repo)
    source = tmp_path / 'shared'
    subprocess.run(
        ['git', 'clone', '-q', '--shared', str(origin), str(source)], check=True, env=git_env()
    )
    _git(source, 'branch', 'topic', 'origin/topic')
    _git(source, 'remote', 'remove', 'origin')
    _git(
        source,
        '-c',
        'user.name=Test User',
        '-c',
        'user.email=test@example.invalid',
        'tag',
        '-a',
        'annotated',
        '-m',
        'release',
        'topic',
    )
    before = _refs(source)
    with pytest.raises(PubanonError) as error:
        check_source_safety(source)
    assert 'git repack -a' in error.value.render()
    alternates = Path(
        _git_out(source, 'rev-parse', '--git-path', 'objects/info/alternates').strip()
    )
    if not alternates.is_absolute():
        alternates = source / alternates
    assert escape(str(alternates)) in error.value.render()
    saved = alternates.with_name('alternates.saved')
    operations = Path(__file__).resolve().parents[2] / 'docs/operations.md'
    recovery = operations.read_text().split('## Disconnecting an alternate object store', 1)[1]
    script = recovery.split('```sh\n', 1)[1].split('```', 1)[0]
    if verification_fails:
        script = script.replace('git fsck --full', 'false')
    result = subprocess.run(
        ['sh'], cwd=source, input=script.encode(), capture_output=True, check=False, env=git_env()
    )
    assert result.returncode == (1 if verification_fails else 0)
    assert alternates.exists() == verification_fails
    assert not saved.exists()
    assert _refs(source) == before
    for oid in before.values():
        _git(source, 'cat-file', '-e', oid)
        objects = _git_out(source, 'rev-list', '--objects', oid).splitlines()
        for item in objects:
            _git(source, 'cat-file', '-e', item.split(' ', maxsplit=1)[0])
    _git(source, 'fsck', '--full')
    if not verification_fails:
        check_source_safety(source)


@pytest.mark.parametrize(
    ('project', 'encoded'),
    [
        ('pubanon', 'pubanon'),
        ('a b', 'a%20b'),
        ('a.b', 'a%2Eb'),
        ('a%b', 'a%25b'),
        ('a\u00e9b', 'a%C3%A9b'),
    ],
)
def test_source_bookmark_encodes_existing_project_names(
    make_repo: Callable[..., Path], project: str, encoded: str
) -> None:
    repo = make_repo()
    original = _git(repo, 'rev-parse', 'HEAD').strip().decode()
    publication = '1' * 40
    bookmark = _source.describe_bookmark(repo, project, publication)
    assert bookmark.refname == f'refs/pubanon/publications/{encoded}/{publication}'
    _source.ensure_bookmark(bookmark, original)
    assert _source.source_recovery(bookmark, original).status == 'protected'


def test_source_bookmark_preserves_rewritten_commit(make_repo: Callable[..., Path]) -> None:
    repo = make_repo(files={'original.txt': 'private revision\n'})
    original = _git(repo, 'rev-parse', 'HEAD').strip().decode()
    publication = 'a' * 40
    bookmark = _source.describe_bookmark(repo, 'project', publication)
    assert bookmark.source_path == repo.resolve()
    assert bookmark.git_common_dir == (repo / '.git').resolve()
    assert bookmark.refname == f'refs/pubanon/publications/project/{publication}'

    _source.ensure_bookmark(bookmark, original)
    _source.ensure_bookmark(bookmark, original)
    assert _git(repo, 'rev-parse', bookmark.refname).strip().decode() == original
    _git(
        repo,
        '-c',
        'user.name=Test User',
        '-c',
        'user.email=test@example.invalid',
        'commit',
        '--amend',
        '-qm',
        'rewritten',
    )
    _git(repo, 'reflog', 'expire', '--expire=now', '--all')
    _git(repo, 'gc', '--prune=now')
    assert _git(repo, 'cat-file', '-t', original) == b'commit\n'
    assert _source.source_recovery(bookmark, original).status == 'protected'

    _git(repo, 'update-ref', 'refs/keep/other', original)
    _source.remove_bookmark(bookmark, original)
    _source.remove_bookmark(bookmark, original)
    _git(repo, 'gc', '--prune=now')
    assert _git(repo, 'cat-file', '-t', original) == b'commit\n'
    _git(repo, 'update-ref', '-d', 'refs/keep/other', original)
    _git(repo, 'gc', '--prune=now')
    missing = subprocess.run(
        ['git', '-C', repo, 'cat-file', '-e', original],
        capture_output=True,
        env=git_env(),
        check=False,
    )
    assert missing.returncode != 0
    assert _source.source_recovery(bookmark, original).status == 'unavailable'


def test_source_bookmark_recovers_moved_repository_only_with_owned_ref(
    tmp_path: Path, make_repo: Callable[..., Path]
) -> None:
    original_repo = make_repo(files={'same.txt': 'content\n'})
    original = _git(original_repo, 'rev-parse', 'HEAD').strip().decode()
    bookmark = _source.describe_bookmark(original_repo, 'project', 'e' * 40)
    _source.ensure_bookmark(bookmark, original)
    moved = tmp_path / 'moved'
    original_repo.rename(moved)

    assert _source.source_recovery(bookmark, original).status == 'unavailable'
    relocated = _source.source_recovery(bookmark, original, relocated_source=moved)
    assert relocated.status == 'protected'
    assert relocated.source_path == moved.resolve()

    unrelated = make_repo('unrelated', files={'same.txt': 'content\n'})
    refused = _source.source_recovery(bookmark, original, relocated_source=unrelated)
    assert refused.status == 'unavailable'
    _git(moved, 'update-ref', '-d', bookmark.refname, original)
    assert (
        _source.source_recovery(bookmark, original, relocated_source=moved).status == 'unavailable'
    )


def test_source_bookmark_refuses_conflicting_owner(
    make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo()
    original = _git(repo, 'rev-parse', 'HEAD').strip().decode()
    other = (
        _git(
            repo,
            'commit-tree',
            _git(repo, 'rev-parse', 'HEAD^{tree}').strip().decode(),
            '-m',
            'other',
        )
        .strip()
        .decode()
    )
    bookmark = _source.describe_bookmark(repo, 'project', 'b' * 40)
    _git(repo, 'update-ref', bookmark.refname, other)
    with pytest.raises(PubanonError, match='bookmark'):
        _source.ensure_bookmark(bookmark, original)
    with pytest.raises(PubanonError, match='bookmark'):
        _source.remove_bookmark(bookmark, original)
    assert _source.source_recovery(bookmark, original).status == 'available'
    assert 'changed' in (_source.source_recovery(bookmark, original).reason or '')
    assert _git(repo, 'rev-parse', bookmark.refname).strip().decode() == other

    _git(repo, 'update-ref', '-d', bookmark.refname, other)
    _git(repo, 'symbolic-ref', bookmark.refname, 'refs/heads/master')
    with pytest.raises(PubanonError, match='symbolic'):
        _source.ensure_bookmark(bookmark, original)
    with pytest.raises(PubanonError, match='symbolic'):
        _source.remove_bookmark(bookmark, original)
    symbolic_recovery = _source.source_recovery(bookmark, original)
    assert symbolic_recovery.status == 'available'
    assert 'symbolic' in (symbolic_recovery.reason or '')

    _git(repo, 'symbolic-ref', '--delete', bookmark.refname)
    original_run = _source.run_git

    def _fail_write(args: Sequence[str], **kwargs: Unpack[_RunGitOptions]) -> GitResult:
        if 'update-ref' in args:
            return GitResult(1, b'', b'read-only fixture')
        return original_run(args, **kwargs)

    monkeypatch.setattr(_source, 'run_git', _fail_write)
    with pytest.raises(PubanonError, match='bookmark') as error:
        _source.ensure_bookmark(bookmark, original)
    assert 'read-only fixture' in error.value.render()


def test_source_bookmark_refuses_missing_source_commit(make_repo: Callable[..., Path]) -> None:
    repo = make_repo()
    bookmark = _source.describe_bookmark(repo, 'project', 'd' * 40)
    with pytest.raises(PubanonError, match='source commit is unavailable'):
        _source.ensure_bookmark(bookmark, 'f' * 40)
    assert _source.source_recovery(bookmark, 'f' * 40).status == 'unavailable'


def test_source_bookmark_relocation_requires_recorded_evidence(
    tmp_path: Path, make_repo: Callable[..., Path]
) -> None:
    original_repo = make_repo(files={'same.txt': 'content\n'})
    original = _git(original_repo, 'rev-parse', 'HEAD').strip().decode()
    linked = tmp_path / 'linked'
    _git(original_repo, 'worktree', 'add', '--detach', str(linked), original)
    bookmark = _source.describe_bookmark(linked, 'project', 'c' * 40)
    other_repo = make_repo('other', files={'same.txt': 'content\n'})
    assert (
        _source.source_recovery(bookmark, original, relocated_source=other_repo).status
        == 'available'
    )
    _source.ensure_bookmark(bookmark, original)
    recovery = _source.source_recovery(bookmark, original, relocated_source=other_repo)
    assert recovery.status == 'protected'
    assert recovery.source_path != other_repo

    _git(original_repo, 'worktree', 'remove', str(linked))
    relocated = _source.source_recovery(bookmark, original, relocated_source=original_repo)
    assert relocated.status == 'protected'
    assert relocated.source_path == original_repo.resolve()
    _source.remove_bookmark(bookmark, original)
    assert (
        _source.source_recovery(bookmark, original, relocated_source=original_repo).status
        == 'available'
    )


def test_source_bookmark_skips_repository_reference_hook(
    make_repo: Callable[..., Path],
) -> None:
    repo = make_repo()
    commit = _git(repo, 'rev-parse', 'HEAD').strip().decode()
    bookmark = _source.describe_bookmark(repo, 'project', 'a' * 40)
    marker = repo / 'hook-invoked'
    hook = repo / '.git' / 'hooks' / 'reference-transaction'
    hook.parent.mkdir(exist_ok=True)
    hook.write_text('#!/bin/sh\nprintf invoked > hook-invoked\nexit 1\n')
    hook.chmod(0o755)

    _source.ensure_bookmark(bookmark, commit)

    assert _git(repo, 'rev-parse', bookmark.refname).strip().decode() == commit
    assert not marker.exists()


def test_source_safety_refuses_alternates_file(make_repo: Callable[..., Path]) -> None:
    repo = make_repo()
    alternates = repo / '.git' / 'objects' / 'info' / 'alternates'
    alternates.parent.mkdir(exist_ok=True)
    alternates.write_text('/canaryx/private/objects\n')

    with pytest.raises(PubanonError) as exc_info:
        check_source_safety(repo)

    rendered = exc_info.value.render()
    assert 'alternate' in rendered.casefold()
    assert escape(str(alternates)) in rendered


def test_source_safety_refuses_configured_remote(
    make_repo: Callable[..., Path],
) -> None:
    repo = make_repo()
    subprocess.run(
        ['git', '-C', repo, 'remote', 'add', 'origin', 'https://canaryx.invalid/private'],
        check=True,
        env=git_env(),
    )
    with pytest.raises(PubanonError) as exc_info:
        check_source_safety(repo)

    rendered = exc_info.value.render()
    assert 'remote' in rendered.casefold()


def test_source_safety_refuses_grafts_file(make_repo: Callable[..., Path]) -> None:
    repo = make_repo()
    grafts = repo / '.git' / 'info' / 'grafts'
    grafts.parent.mkdir(exist_ok=True)
    grafts.touch()

    with pytest.raises(PubanonError) as exc_info:
        check_source_safety(repo)

    rendered = exc_info.value.render()
    assert 'grafts' in rendered.casefold()
    assert str(repo) not in rendered


def test_source_safety_refuses_replacement_ref(make_repo: Callable[..., Path]) -> None:
    repo = make_repo()
    oid = _git(repo, 'rev-parse', 'HEAD').strip().decode()
    _git(repo, 'update-ref', f'refs/replace/{oid}', oid)

    with pytest.raises(PubanonError) as exc_info:
        check_source_safety(repo)

    assert 'replace' in exc_info.value.render().casefold()


def test_source_safety_refuses_shallow_repository(
    make_repo: Callable[..., Path],
) -> None:
    repo = make_repo()
    (repo / '.git' / 'shallow').write_bytes(_git(repo, 'rev-parse', 'HEAD'))

    with pytest.raises(PubanonError) as exc_info:
        check_source_safety(repo)

    assert 'shallow' in exc_info.value.render().casefold()


@pytest.mark.parametrize(
    ('path', 'target'),
    [
        ('A', b'a/../sibling'),
        ('Nested/A', b'nested/a/../../sibling'),
        ('\u00c9', 'E\u0301/../sibling'.encode()),
        ('Nested/E\u0301', 'nested/\u00e9/../../sibling'.encode()),
    ],
)
def test_symlink_graph_refuses_alias_prefix(path: str, target: bytes) -> None:
    snapshot = (
        _source.Entry(path, 0o120000, 'symlink', b'.'),
        _source.Entry('B', 0o120000, 'symlink', target),
    )

    with pytest.raises(PubanonError, match='unresolvable symlink target'):
        _source.validate_symlink_graph(snapshot)


@pytest.mark.parametrize(
    ('raw_mode', 'error'),
    [
        (b'160000', 'source tree contains an unsupported gitlink'),
        (b'100600', 'source tree contains an unsupported mode'),
    ],
)
def test_tree_mode_validator_owns_snapshot_mode_grammar(
    raw_mode: bytes,
    error: str,
) -> None:
    with pytest.raises(PubanonError, match=error):
        _source.tree_mode(raw_mode, 'entry.txt')


@pytest.mark.parametrize(
    ('raw_path', 'error'),
    [
        (b'bad-\xff.txt', 'source tree contains a non-UTF-8 path'),
        (b'../private.txt', 'Git returned a malformed tree path'),
    ],
)
def test_tree_path_validator_owns_snapshot_path_grammar(
    raw_path: bytes,
    error: str,
) -> None:
    with pytest.raises(PubanonError, match=error):
        _source.decode_tree_path(raw_path)


def test_working_changes_counts_both_columns_and_consumes_rename_paths(
    make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    separator = ' ' if os.name == 'nt' else '\n'
    source = make_repo(files={f'old{separator}name.txt': 'original\n', 'both.txt': 'original\n'})
    _git(source, 'mv', f'old{separator}name.txt', f'new{separator}name.txt')
    (source / 'both.txt').write_text('staged\n')
    _git(source, 'add', 'both.txt')
    (source / 'both.txt').write_text('unstaged\n')
    untracked = (
        f'untracked private{separator}path.txt'
        if os.name == 'nt'
        else f'?? private{separator}path.txt'
    )
    (source / untracked).write_text('untracked\n')
    original = _source.run_git
    status_environments: list[Mapping[str, str] | None] = []

    def _observed(args: Sequence[str], **kwargs: Unpack[_RunGitOptions]) -> GitResult:
        if 'status' in args:
            assert '-z' in args
            status_environments.append(kwargs.get('env_extra'))
        return original(args, **kwargs)

    monkeypatch.setattr(_source, 'run_git', _observed)
    index_before = (source / '.git' / 'index').read_bytes()

    changes = _source.working_changes(source)

    assert changes.staged == 2
    assert changes.unstaged == 1
    assert changes.untracked == 1
    assert status_environments == [{'GIT_OPTIONAL_LOCKS': '0'}]
    assert (source / '.git' / 'index').read_bytes() == index_before
