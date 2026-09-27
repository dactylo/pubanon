import os
import re
import shlex
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, NotRequired, TypedDict, Unpack, cast
from unittest.mock import patch

import pytest

from pubanon import _filesystem
from pubanon._cli import _arguments
from pubanon._config import GlobalConfig, Paths, ProjectConfig, fingerprint
from pubanon._errors import PubanonError
from pubanon._run import GitResult, run_git
from pubanon._snapshots import _candidate, _history, _records, _source, _store
from pubanon._snapshots._candidate import _write_tree
from pubanon._snapshots._source import (
    Entry,
    PinnedSource,
    Snapshot,
    read_snapshot,
    snapshot_digest,
)
from pubanon._terminal import color_context, escape
from tests._git import git_env
from tests._permissions import assert_private_mode

_PROCESS_BOUND_CASES = (
    ('root', ''),
    ('same-directory', 'nested/'),
    ('sibling-directories', 'directory-{index}/'),
    ('deep-siblings', 'a/b/directory-{index}/'),
)
_TREE_ORACLE_CASES: tuple[tuple[str, Snapshot], ...] = (
    ('empty', ()),
    (
        'binary-and-duplicate',
        (
            Entry('empty.txt', 0o100644, 'blob', b''),
            Entry('binary.bin', 0o100644, 'blob', b'\x00\xff\n'),
            Entry('copy.bin', 0o100644, 'blob', b'\x00\xff\n'),
        ),
    ),
    (
        'paths',
        (
            Entry('plain.txt', 0o100644, 'blob', b'plain\n'),
            Entry('unicode/\u03c0.txt', 0o100644, 'blob', b'pi\n'),
            Entry('line\nbreak.txt', 0o100644, 'blob', b'newline\n'),
            Entry('a/b/c/deep.txt', 0o100644, 'blob', b'deep\n'),
        ),
    ),
    (
        'modes',
        (
            Entry('bin/tool', 0o100755, 'blob', b'#!/bin/sh\n'),
            Entry('link', 0o120000, 'symlink', b'bin/tool'),
        ),
    ),
    (
        'mixed-depths',
        (
            Entry('root.txt', 0o100644, 'blob', b'root\n'),
            Entry('one/first.txt', 0o100644, 'blob', b'first\n'),
            Entry('two/second.txt', 0o100644, 'blob', b'second\n'),
            Entry('one/deep/third.txt', 0o100644, 'blob', b'third\n'),
            Entry('far/down/more/fourth.txt', 0o100644, 'blob', b'fourth\n'),
        ),
    ),
)


@dataclass(frozen=True)
class _RetainedVerification:
    _calls: list[list[str]]
    _global_config: GlobalConfig
    _paths: Paths
    _project: ProjectConfig
    _record: _candidate.CandidateRecord
    _snapshot: Snapshot


class _RunGitOptions(TypedDict):
    check: NotRequired[bool]
    cwd: Path | None
    env_extra: NotRequired[Mapping[str, str] | None]
    input_bytes: NotRequired[bytes | None]


def _configs(tmp_path: Path, name: str = 'example') -> tuple[GlobalConfig, ProjectConfig]:
    source = tmp_path / 'source'
    source.mkdir(exist_ok=True)
    return (
        GlobalConfig(
            anon_name='Anonymous Publisher',
            anon_email='anonymous@example.invalid',
            accounts={'github': 'release-account'},
            sensitive_terms=('Private Person',),
        ),
        ProjectConfig(
            name=name,
            source_path=source.resolve(),
            host='github',
            owner='public-owner',
            repo='public-repo',
            visibility='public',
            allow=('**',),
            deny=(),
            secret_exceptions=('.env.example',),
            extra_terms=(),
            hygiene_terms=(),
            self_publication=True,
            required_checks=(),
        ),
    )


def _dirty_inspection(paths: Paths, project: str) -> Path:
    inspect = paths.data_dir / 'projects' / project / 'inspections' / ('a' * 40)
    inspect.mkdir(parents=True)
    marker = inspect / 'edited.txt'
    marker.write_text('keep this edit\n', encoding='utf-8')
    return marker


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ['git', '-C', repo, *args],
        check=True,
        capture_output=True,
        env=git_env(),
    ).stdout


def _git_input(repo: Path, *args: str, input_bytes: bytes) -> bytes:
    return subprocess.run(
        ['git', '-C', repo, *args],
        input=input_bytes,
        check=True,
        capture_output=True,
        env=git_env(),
    ).stdout


def _oracle_commit(
    store: Path,
    tree: str,
    global_config: GlobalConfig,
    message: str,
    epoch: int,
) -> str:
    date = f'{epoch} +0000'
    env = git_env(
        {
            'GIT_AUTHOR_DATE': date,
            'GIT_AUTHOR_EMAIL': global_config.anon_email,
            'GIT_AUTHOR_NAME': global_config.anon_name,
            'GIT_COMMITTER_DATE': date,
            'GIT_COMMITTER_EMAIL': global_config.anon_email,
            'GIT_COMMITTER_NAME': global_config.anon_name,
        }
    )
    return (
        subprocess.run(
            ['git', '-C', store, 'commit-tree', tree],
            input=message.encode() + b'\n',
            env=env,
            check=True,
            capture_output=True,
        )
        .stdout.strip()
        .decode('ascii')
    )


def _oracle_tree(store: Path, snapshot: Snapshot) -> str:
    contents: dict[str, list[tuple[bytes, bytes]]] = {'': []}
    for entry in sorted(snapshot, key=lambda item: item.path.encode('utf-8')):
        parent = Path(entry.path).parent
        while str(parent) != '.':
            contents.setdefault(parent.as_posix(), [])
            parent = parent.parent
        oid = _git_input(
            store,
            'hash-object',
            '-w',
            '--stdin',
            input_bytes=entry.data,
        ).strip()
        directory, _, name = entry.path.rpartition('/')
        raw_name = name.encode()
        contents[directory].append(
            (raw_name, f'{entry.mode:o} blob '.encode() + oid + b'\t' + raw_name + b'\0')
        )

    root_oid: bytes | None = None
    for directory in sorted(
        contents,
        key=lambda item: item.count('/') + bool(item),
        reverse=True,
    ):
        tree_oid = _git_input(
            store,
            'mktree',
            '-z',
            input_bytes=b''.join(record for _name, record in sorted(contents[directory])),
        ).strip()
        if directory:
            parent, _, name = directory.rpartition('/')
            raw_name = name.encode()
            contents[parent].append(
                (raw_name, b'040000 tree ' + tree_oid + b'\t' + raw_name + b'\0')
            )
        else:
            root_oid = tree_oid
    assert root_oid is not None
    return root_oid.decode('ascii')


def _paths(tmp_path: Path) -> Paths:
    config_dir = tmp_path / 'config' / 'pubanon'
    data_dir = tmp_path / 'data' / 'pubanon'
    return Paths(config_dir, data_dir, config_dir / 'config.toml', data_dir / 'registry.json')


def _pinned() -> PinnedSource:
    return PinnedSource('a' * 40, 'ref', 'refs/heads/main', 'a' * 40)


def _replace_candidate_commit(
    paths: Paths,
    project: ProjectConfig,
    record: _candidate.CandidateRecord,
    raw: bytes,
) -> _candidate.CandidateRecord:
    store = _candidate.candidate_store_path(paths, project.name)
    commit = _git_input(
        store,
        'hash-object',
        '--literally',
        '-t',
        'commit',
        '-w',
        '--stdin',
        input_bytes=raw,
    )
    commit_text = commit.strip().decode('ascii')
    _git(store, 'update-ref', _candidate.CANDIDATE_REF, commit_text)
    changed = replace(record, commit=commit_text)
    _candidate.save_candidate(paths, project.name, changed)
    return changed


def _replacement_history(
    tmp_path: Path,
    *,
    stage: bool = False,
) -> tuple[Paths, GlobalConfig, ProjectConfig, tuple[str, ...]]:
    paths = _paths(tmp_path)
    g, project = _configs(tmp_path)
    if stage:
        g = replace(g, accounts={'github': 'stage-owner'})
        project = replace(project, owner='stage-owner', _stage_repo='staging-repo')
    commits: list[str] = []
    for index in range(3):
        record = _candidate.build_candidate(
            paths,
            g,
            project,
            _candidate.CandidateInputs(
                (Entry('file.txt', 0o100644, 'blob', f'old {index}'.encode()),),
                _pinned(),
                None,
                f'release {index}',
            ),
        )
        commits.append(record.commit)
        store = _candidate.store_path(paths, project.name)
        _git(store, 'update-ref', 'refs/heads/main', record.commit)
        _candidate.record_path(paths, project.name).unlink()
    return paths, g, project, tuple(commits)


def _snapshot() -> tuple[Entry, ...]:
    return (
        Entry('README.md', 0o100644, 'blob', b'public\n'),
        Entry('bin/tool', 0o100755, 'blob', b'#!/bin/sh\n'),
    )


def _build(tmp_path: Path) -> tuple[Paths, ProjectConfig, _candidate.CandidateRecord]:
    paths = _paths(tmp_path)
    global_config, project_config = _configs(tmp_path)
    record = _candidate.build_candidate(
        paths,
        global_config,
        project_config,
        _candidate.CandidateInputs(_snapshot(), _pinned(), None, None),
    )
    return paths, project_config, record


def _complete_replacement(
    tmp_path: Path,
    *,
    changed: Snapshot | None = None,
    stage: bool = False,
    selected: int = 0,
    tagged: bool = False,
) -> tuple[Paths, GlobalConfig, ProjectConfig, _records.CandidateRecord]:
    paths, g, project, commits = _replacement_history(tmp_path, stage=stage)
    store = _candidate.store_path(paths, project.name)
    if tagged:
        for index, old in enumerate(commits):
            _git(store, 'update-ref', f'refs/tags/v{index}.0.0', old)
    record = _candidate.build_candidate(
        paths,
        g,
        project,
        _candidate.CandidateInputs(
            _snapshot(),
            _pinned(),
            None,
            None,
            replaces=commits[selected],
            replace_scope='one',
        ),
    )
    replayed: list[_records.ReplayEntry] = []
    previous = record.commit
    for old in commits[selected + 1 :]:
        raw = _git(store, 'cat-file', 'commit', old)
        headers, _, message = raw.partition(b'\n\n')
        old_parent = _git(store, 'show', '-s', '--format=%P', old).strip()
        headers = headers.replace(b'parent ' + old_parent, b'parent ' + previous.encode())
        if changed is not None:
            tree = _write_tree(store, changed)
            old_tree = _git(store, 'show', '-s', '--format=%T', old).strip()
            headers = headers.replace(b'tree ' + old_tree, b'tree ' + tree.encode())
        result = run_git(
            ['hash-object', '--literally', '-t', 'commit', '-w', '--stdin'],
            cwd=store,
            input_bytes=headers + b'\n\n' + message,
        )
        new = result.stdout.strip().decode()
        replayed.append(_records.ReplayEntry(old=old, new=new))
        previous = new
    tags = {}
    if tagged:
        rewritten = (record.commit, *(entry.new for entry in replayed))
        tags = {
            f'v{index}.0.0': commits[index] if index < selected else rewritten[index - selected]
            for index in range(len(commits))
        }
    record = replace(record, replayed=tuple(replayed), tip=previous, poststate_tags=tags)
    _candidate.save_candidate(paths, project.name, record)
    _git(store, 'update-ref', _candidate.CANDIDATE_REF, previous)
    return paths, g, project, record


def _retain_alias_symlink_candidate(
    tmp_path: Path,
) -> tuple[Paths, ProjectConfig, _candidate.CandidateRecord]:
    paths, project, record = _build(tmp_path)
    store = _candidate.candidate_store_path(paths, project.name)
    snapshot = (
        Entry('A', 0o120000, 'symlink', b'.'),
        Entry('B', 0o120000, 'symlink', b'a/../sibling'),
    )
    tree = _oracle_tree(store, snapshot)
    raw = _git(store, 'cat-file', 'commit', record.commit)
    raw = raw.replace(f'tree {record.tree}\n'.encode(), f'tree {tree}\n'.encode(), 1)
    changed = replace(record, tree=tree, snapshot_digest=snapshot_digest(snapshot))
    changed = _replace_candidate_commit(paths, project, changed, raw)
    return paths, project, changed


@pytest.fixture
def retained_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _RetainedVerification:
    paths = _paths(tmp_path)
    global_config, project = _configs(tmp_path)
    snapshot = _snapshot()
    record = _candidate.build_candidate(
        paths, global_config, project, _candidate.CandidateInputs(snapshot, _pinned(), None, None)
    )
    calls: list[list[str]] = []
    real_run_git = _candidate.run_git

    def _record_run_git(args: Sequence[str], **kwargs: Unpack[_RunGitOptions]) -> GitResult:
        calls.append(list(args))
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(_candidate, 'run_git', _record_run_git)
    return _RetainedVerification(calls, global_config, paths, project, record, snapshot)


def test_build_candidate_gates_tag_intent_with_custom_message(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    global_config, project_config = _configs(tmp_path)
    global_config = replace(global_config, sensitive_terms=('v1.2.3',))

    with pytest.raises(PubanonError, match='the release tag did not pass content checks'):
        _candidate.build_candidate(
            paths,
            global_config,
            project_config,
            _candidate.CandidateInputs(
                _snapshot(),
                _pinned(),
                '1.2.3',
                'safe custom message',
            ),
        )


def test_build_candidate_refuses_git_metadata_inspection_path(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    global_config, project_config = _configs(tmp_path)

    with pytest.raises(PubanonError, match='unsupported snapshot'):
        _candidate.build_candidate(
            paths,
            global_config,
            project_config,
            _candidate.CandidateInputs(
                (Entry('.git/config', 0o100644, 'blob', b'not metadata\n'),),
                _pinned(),
                None,
                None,
            ),
        )


def test_build_candidate_refuses_identity_containing_private_path(tmp_path: Path) -> None:
    global_config, project = _configs(tmp_path)
    global_config = replace(global_config, anon_name=f'Publisher {project.source_path}')
    with pytest.raises(PubanonError, match='publication identity matches') as error:
        _candidate.build_candidate(
            _paths(tmp_path),
            global_config,
            project,
            _candidate.CandidateInputs(_snapshot(), _pinned(), None, None),
        )
    assert error.value.fact == 'rule source: private path'
    assert str(project.source_path) not in error.value.render()


@pytest.mark.parametrize(
    ('global_changes', 'project_changes', 'expected_fact'),
    [
        (
            {'anon_email': 'anon@example.invalid', 'sensitive_terms': ('anon@example.invalid',)},
            {},
            'rule source: sensitive_terms',
        ),
        (
            {'anon_email': 'anon@example.invalid'},
            {'extra_terms': ('anon@example.invalid',)},
            'rule source: extra_terms',
        ),
        (
            {'anon_name': 'Pubanon Publisher'},
            {'self_publication': False},
            'rule source: hygiene term',
        ),
        (
            {'anon_email': 'anon@example.invalid', 'sensitive_terms': ('anon@example.invalid',)},
            {'extra_terms': ('anon@example.invalid',), 'hygiene_terms': ('example.invalid',)},
            'rule source: sensitive_terms, extra_terms, hygiene term',
        ),
    ],
)
def test_build_candidate_refuses_identity_naming_each_rule_source(
    tmp_path: Path,
    global_changes: dict[str, object],
    project_changes: dict[str, object],
    expected_fact: str,
) -> None:
    global_config, project = _configs(tmp_path)
    global_config = replace(global_config, **global_changes)
    project = replace(project, **project_changes)
    paths = _paths(tmp_path)
    with pytest.raises(PubanonError, match='publication identity matches') as error:
        _candidate.build_candidate(
            paths,
            global_config,
            project,
            _candidate.CandidateInputs(_snapshot(), _pinned(), None, None),
        )
    assert error.value.fact == expected_fact
    assert error.value.recovery is not None
    assert 'example.invalid' not in error.value.render()
    assert 'Pubanon Publisher' not in error.value.render()


def test_build_candidate_refuses_identity_without_disclosing_active_terms(
    tmp_path: Path,
) -> None:
    global_config, project = _configs(tmp_path)
    global_config = replace(
        global_config,
        anon_email='anon@identity.invalid',
        sensitive_terms=('identity',),
    )
    project = replace(project, extra_terms=('publication',))
    with pytest.raises(PubanonError) as error:
        _candidate.build_candidate(
            _paths(tmp_path),
            global_config,
            project,
            _candidate.CandidateInputs(_snapshot(), _pinned(), None, None),
        )
    assert error.value.fact == 'rule source: sensitive_terms'
    assert str(error.value) == 'publication identity matches an active disclosure rule'
    assert error.value.recovery == (
        'remove the value from sensitive_terms or choose another publication identity'
    )


def test_build_candidate_refuses_unsupported_snapshot_state(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    global_config, project_config = _configs(tmp_path)

    with pytest.raises(PubanonError, match='unsupported snapshot'):
        _candidate.build_candidate(
            paths,
            global_config,
            project_config,
            _candidate.CandidateInputs(
                (Entry('special', 0o100644, 'unknown', b'data'),),
                _pinned(),
                None,
                None,
            ),
        )


def test_build_candidate_refuses_when_record_exists(tmp_path: Path) -> None:
    paths, project_config, _record = _build(tmp_path)
    global_config, _ = _configs(tmp_path)

    with pytest.raises(PubanonError, match='snapshot is already prepared'):
        _candidate.build_candidate(
            paths,
            global_config,
            project_config,
            _candidate.CandidateInputs(_snapshot(), _pinned(), None, None),
        )


def test_build_candidate_replaces_symbolic_private_ref_without_changing_main(
    tmp_path: Path,
) -> None:
    paths, project, first = _build(tmp_path)
    store = _candidate.store_path(paths, project.name)
    _candidate.record_path(paths, project.name).unlink()
    _git(store, 'update-ref', 'refs/heads/main', first.commit)
    _git(store, 'symbolic-ref', _candidate.CANDIDATE_REF, 'refs/heads/main')
    global_config, _ = _configs(tmp_path)

    second = _candidate.build_candidate(
        paths,
        global_config,
        project,
        _candidate.CandidateInputs(
            (Entry('second.txt', 0o100644, 'blob', b'second\n'),), _pinned(), None, None
        ),
    )

    assert second.commit != first.commit
    assert _git(store, 'rev-parse', 'refs/heads/main').strip().decode() == first.commit
    assert _git(store, 'rev-parse', _candidate.CANDIDATE_REF).strip().decode() == second.commit
    assert _git(store, 'for-each-ref', '--format=%(symref)', _candidate.CANDIDATE_REF) == b'\n'


def test_build_candidate_tree_is_deterministic_for_same_snapshot(tmp_path: Path) -> None:
    first_paths = _paths(tmp_path / 'first')
    second_paths = _paths(tmp_path / 'second')
    global_config, project_config = _configs(tmp_path)

    first = _candidate.build_candidate(
        first_paths,
        global_config,
        project_config,
        _candidate.CandidateInputs(_snapshot(), _pinned(), None, None),
    )
    second = _candidate.build_candidate(
        second_paths,
        global_config,
        project_config,
        _candidate.CandidateInputs(
            tuple(reversed(_snapshot())),
            _pinned(),
            None,
            None,
        ),
    )

    assert first.tree == second.tree


def test_build_candidate_uses_exact_gated_message_override(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    global_config, project_config = _configs(tmp_path)
    record = _candidate.build_candidate(
        paths,
        global_config,
        project_config,
        _candidate.CandidateInputs(
            _snapshot(),
            _pinned(),
            None,
            'public release message',
        ),
    )
    raw = _git(
        _candidate.store_path(paths, project_config.name), 'cat-file', 'commit', record.commit
    )
    assert raw.partition(b'\n\n')[2] == b'public release message\n'

    _candidate.discard(paths, project_config.name, force=False)
    with pytest.raises(PubanonError, match='content checks'):
        _candidate.build_candidate(
            paths,
            global_config,
            project_config,
            _candidate.CandidateInputs(
                _snapshot(),
                _pinned(),
                None,
                'Private Person release',
            ),
        )


def test_build_candidate_writes_anonymous_unsigned_utc_commit(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    global_config, project_config = _configs(tmp_path)

    record = _candidate.build_candidate(
        paths,
        global_config,
        project_config,
        _candidate.CandidateInputs(_snapshot(), _pinned(), '1.2.3', None),
    )

    raw = _git(
        _candidate.store_path(paths, project_config.name), 'cat-file', 'commit', record.commit
    )
    assert re.search(
        rb'^author Anonymous Publisher <anonymous@example\.invalid> \d+ \+0000$',
        raw,
        re.MULTILINE,
    )
    assert re.search(
        rb'^committer Anonymous Publisher <anonymous@example\.invalid> \d+ \+0000$',
        raw,
        re.MULTILINE,
    )
    assert b'\ngpgsig ' not in raw
    assert raw.partition(b'\n\n')[2] == b'release: v1.2.3\n'
    assert record.message == 'release: v1.2.3'
    assert record.tag_intent == 'v1.2.3'


def test_candidate_commit_matches_independent_fixed_metadata_git_oracle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    epoch = 1_700_000_000
    message = 'deterministic candidate'
    snapshot = (
        Entry('empty.txt', 0o100644, 'blob', b''),
        Entry('binary.bin', 0o100644, 'blob', b'\x00\xff\n'),
        Entry('bin/tool', 0o100755, 'blob', b'#!/bin/sh\n'),
        Entry('link', 0o120000, 'symlink', b'bin/tool'),
    )
    paths = _paths(tmp_path / 'candidate')
    global_config, project_config = _configs(tmp_path)
    oracle_store = _candidate.store_path(_paths(tmp_path / 'oracle'), 'example')
    monkeypatch.setattr(_candidate.time, 'time', lambda: epoch)

    oracle_tree = _oracle_tree(oracle_store, snapshot)
    oracle_commit = _oracle_commit(
        oracle_store,
        oracle_tree,
        global_config,
        message,
        epoch,
    )
    record = _candidate.build_candidate(
        paths,
        global_config,
        project_config,
        _candidate.CandidateInputs(snapshot, _pinned(), None, message, (None, {})),
    )
    candidate_store = _candidate.store_path(paths, project_config.name)

    assert record.tree == oracle_tree
    assert record.commit == oracle_commit
    assert _git(candidate_store, 'cat-file', 'commit', record.commit) == _git(
        oracle_store,
        'cat-file',
        'commit',
        oracle_commit,
    )
    assert read_snapshot(candidate_store, record.commit) == read_snapshot(
        oracle_store, oracle_commit
    )


def test_candidate_record_round_trips_strictly(tmp_path: Path) -> None:
    paths, project_config, record = _build(tmp_path)

    assert _candidate.load_candidate(paths, project_config.name) == record


def test_candidate_store_initialization_reports_child_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail(_args: Sequence[str], **_kwargs: object) -> GitResult:
        return GitResult(7, b'', b'diagnostic\x1b\\path')

    monkeypatch.setattr(_store, 'run_git', _fail)
    with pytest.raises(PubanonError, match='cannot initialize snapshot store') as error:
        _candidate.store_path(_paths(tmp_path), 'example')
    assert error.value.fact == r'exit status 7: diagnostic\x1b\\path'


@pytest.mark.parametrize('project', ['', '../outside', 'nested/project'])
def test_candidate_store_path_refuses_invalid_project_names(
    tmp_path: Path,
    project: str,
) -> None:
    paths = _paths(tmp_path)

    with pytest.raises(PubanonError, match='invalid project name'):
        _candidate.candidate_store_path(paths, project)

    assert not paths.data_dir.exists()


def test_candidate_store_path_returns_absent_store_without_creating_it(tmp_path: Path) -> None:
    paths = _paths(tmp_path)

    store = _candidate.candidate_store_path(paths, 'example')

    assert store == paths.data_dir / 'projects' / 'example' / 'store.git'
    assert not store.exists()


def test_candidate_subprocess_failure_reports_escaped_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    global_config, project_config = _configs(tmp_path)
    _candidate.store_path(paths, project_config.name)
    original_run_git = _candidate.run_git

    def _fail_blob_import(args: Sequence[str], **kwargs: Unpack[_RunGitOptions]) -> GitResult:
        if args[2] == 'fast-import':
            return GitResult(1, b'private-output-canary', b'/private/path-canary\x1b\\detail')
        return original_run_git(args, **kwargs)

    monkeypatch.setattr(_candidate, 'run_git', _fail_blob_import)

    with pytest.raises(PubanonError) as exc_info:
        _candidate.build_candidate(
            paths,
            global_config,
            project_config,
            _candidate.CandidateInputs(_snapshot(), _pinned(), None, None),
        )

    rendered = exc_info.value.render()
    assert 'private-output-canary' not in rendered
    assert exc_info.value.fact == r'exit status 1: /private/path-canary\x1b\\detail'


@pytest.mark.parametrize(
    'snapshot',
    [snapshot for _case, snapshot in _TREE_ORACLE_CASES],
    ids=[case for case, _snapshot_case in _TREE_ORACLE_CASES],
)
def test_candidate_tree_matches_independent_git_oracle(
    snapshot: Snapshot,
    tmp_path: Path,
) -> None:
    oracle_store = _candidate.store_path(_paths(tmp_path / 'oracle'), 'example')
    candidate_store = _candidate.store_path(_paths(tmp_path / 'candidate'), 'example')

    oracle_tree = _oracle_tree(oracle_store, snapshot)
    candidate_tree = _write_tree(candidate_store, snapshot)

    assert candidate_tree == oracle_tree
    candidate_snapshot = read_snapshot(candidate_store, candidate_tree)
    oracle_snapshot = read_snapshot(oracle_store, oracle_tree)
    assert candidate_snapshot == oracle_snapshot
    assert frozenset(candidate_snapshot) == frozenset(snapshot)


@pytest.mark.parametrize(
    'directory',
    [directory for _case, directory in _PROCESS_BOUND_CASES],
    ids=[case for case, _directory in _PROCESS_BOUND_CASES],
)
def test_candidate_tree_process_count_scales_with_depth_not_objects(
    directory: str,
    tmp_path: Path,
) -> None:
    counts: list[int] = []
    for size in (2, 16):
        snapshot = (
            *(
                Entry(
                    f'{directory.format(index=index)}file-{index}.txt',
                    0o100755 if index % 2 else 0o100644,
                    'blob',
                    f'payload {index}\n'.encode(),
                )
                for index in range(size)
            ),
            Entry(f'{directory.format(index=0)}link', 0o120000, 'symlink', b'file-0.txt'),
        )
        paths = _paths(tmp_path / str(size))
        store = _candidate.store_path(paths, 'candidate')
        oracle_store = _candidate.store_path(paths, 'oracle')
        oracle_tree = _oracle_tree(oracle_store, snapshot)

        # Count actual child launches, excluding setup and independent readback.
        with patch('subprocess.Popen', wraps=subprocess.Popen) as processes:
            tree = _write_tree(store, snapshot)
        counts.append(processes.call_count)

        assert tree == oracle_tree
        assert _git(store, 'ls-tree', '-rz', tree) == _git(
            oracle_store, 'ls-tree', '-rz', oracle_tree
        )
        assert frozenset(read_snapshot(store, tree)) == frozenset(snapshot)
        depth = max(entry.path.count('/') for entry in snapshot)
        # Allow constant setup/batching overhead and work for each directory depth.
        assert processes.call_count <= depth + 4

    assert counts[1] <= counts[0] + 2


@pytest.mark.parametrize(
    'output',
    [
        b'',
        b'a' * 40,
        b'a' * 40 + b'\n' + b'b' * 40 + b'\n',
        b'not-an-object-id\n',
    ],
)
def test_candidate_tree_refuses_malformed_blob_batch_output(
    output: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_run_git = _candidate.run_git

    def _malformed_run_git(args: Sequence[str], **kwargs: Unpack[_RunGitOptions]) -> GitResult:
        if args[2:] == ['fast-import', '--quiet']:
            return GitResult(0, output, b'private-output-canary')
        return original_run_git(args, **kwargs)

    monkeypatch.setattr(_candidate, 'run_git', _malformed_run_git)

    with pytest.raises(PubanonError, match='invalid snapshot blob ID') as exc_info:
        _write_tree(
            _candidate.store_path(_paths(tmp_path), 'example'),
            (Entry('file.txt', 0o100644, 'blob', b'data\n'),),
        )

    assert 'private-output-canary' not in exc_info.value.render()


@pytest.mark.parametrize(
    'output',
    [
        b'',
        b'a' * 40,
        b'a' * 40 + b'\n' + b'b' * 40 + b'\n',
        b'not-an-object-id\n',
    ],
)
def test_candidate_tree_refuses_malformed_tree_batch_output(
    output: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_run_git = _candidate.run_git

    def _malformed_run_git(args: Sequence[str], **kwargs: Unpack[_RunGitOptions]) -> GitResult:
        if args[2:] == ['mktree', '-z', '--batch']:
            return GitResult(0, output, b'private-output-canary')
        return original_run_git(args, **kwargs)

    monkeypatch.setattr(_candidate, 'run_git', _malformed_run_git)

    with pytest.raises(PubanonError, match='invalid snapshot tree ID') as exc_info:
        _write_tree(
            _candidate.store_path(_paths(tmp_path), 'example'),
            (Entry('file.txt', 0o100644, 'blob', b'data\n'),),
        )

    assert 'private-output-canary' not in exc_info.value.render()


def test_candidate_tree_validates_complete_snapshot_before_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []
    original_run_git = _candidate.run_git

    def _record_run_git(args: Sequence[str], **kwargs: Unpack[_RunGitOptions]) -> GitResult:
        calls.append(tuple(args))
        return original_run_git(args, **kwargs)

    monkeypatch.setattr(_candidate, 'run_git', _record_run_git)
    store = _candidate.store_path(_paths(tmp_path), 'example')
    calls.clear()

    with pytest.raises(PubanonError, match='unsupported snapshot entry'):
        _write_tree(
            store,
            (
                Entry('safe.txt', 0o100644, 'blob', b'already valid\n'),
                Entry('z/../escape.txt', 0o100644, 'blob', b'invalid\n'),
            ),
        )

    assert calls == []


@pytest.mark.parametrize('command', ['rev-parse', 'cat-file'])
def test_candidate_verification_reports_child_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    paths, project, record = _build(tmp_path)
    global_config, _ = _configs(tmp_path)
    real_git = _candidate.run_git

    def _fail(args: Sequence[str], **kwargs: Unpack[_RunGitOptions]) -> GitResult:
        if args[2] == command:
            return GitResult(7, b'', b'diagnostic\x1b\\path')
        return real_git(args, **kwargs)

    monkeypatch.setattr(_candidate, 'run_git', _fail)
    with pytest.raises(PubanonError) as error:
        _candidate.verify_retained_candidate(paths, global_config, project, record, env={})
    assert error.value.fact == r'exit status 7: diagnostic\x1b\\path'
    assert error.value.recovery == 'pubanon discard, then pubanon prepare'
    assert _candidate.load_candidate(paths, project.name) == record


def test_clean_inspect_refuses_a_symlink_root(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    inspect = paths.data_dir / 'projects' / 'example' / 'inspections' / ('a' * 40)
    inspect.parent.mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'sentinel'
    sentinel.write_bytes(b'preserve')
    inspect.symlink_to(outside, target_is_directory=True)
    _filesystem.chmod(outside, 0o555)

    try:
        with pytest.raises(PubanonError, match='cannot clean review copy'):
            _candidate.clean_inspect(paths, 'example')
        assert inspect.is_symlink()
        assert sentinel.read_bytes() == b'preserve'
        assert_private_mode(outside, 0o555)
    finally:
        _filesystem.chmod(outside, 0o700)


@pytest.mark.parametrize('directory_mode', [0o000, 0o444, 0o555, 0o755])
def test_clean_inspect_removes_restricted_directories_without_changing_link_targets(
    tmp_path: Path,
    directory_mode: int,
) -> None:
    paths = _paths(tmp_path)
    inspect = paths.data_dir / 'projects' / 'example' / 'inspections' / ('a' * 40)
    nested = inspect / 'build' / 'nested'
    nested.mkdir(parents=True)
    (nested / 'artifact').write_bytes(b'build output')
    _filesystem.chmod(nested / 'artifact', 0o444)
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'sentinel'
    sentinel.write_bytes(b'preserve')
    _filesystem.chmod(sentinel, 0o444)
    _filesystem.chmod(outside, 0o555)
    (nested / 'directory-link').symlink_to(outside, target_is_directory=True)
    (nested / 'file-link').symlink_to(sentinel)
    restricted = (inspect, inspect / 'build', nested)
    for directory in reversed(restricted):
        _filesystem.chmod(directory, directory_mode)

    try:
        assert _candidate.clean_inspect(paths, 'example')
        assert not inspect.exists()
        assert sentinel.read_bytes() == b'preserve'
        assert_private_mode(sentinel, 0o444)
        assert_private_mode(outside, 0o555)
        assert not _candidate.clean_inspect(paths, 'example')
    finally:
        for directory in restricted:
            if directory.exists():
                _filesystem.chmod(directory, 0o700)
        _filesystem.chmod(outside, 0o700)


@pytest.mark.parametrize(
    ('tags', 'expected'),
    [
        (('latest',), None),
        (('v1.2.3',), 'v1.2.3'),
        (('v1.2.3', 'latest'), None),
    ],
)
def test_correction_infers_only_one_strict_release_tag(
    tmp_path: Path,
    tags: tuple[str, ...],
    expected: str | None,
) -> None:
    paths, g, project, commits = _replacement_history(tmp_path)
    store = _candidate.store_path(paths, project.name)
    for tag in tags:
        _git(store, 'update-ref', f'refs/tags/{tag}', commits[-1])

    record = _candidate.build_candidate(
        paths,
        g,
        project,
        _candidate.CandidateInputs(
            _snapshot(),
            _pinned(),
            None,
            None,
            replaces=commits[-1],
            replace_scope='one',
        ),
    )

    assert record.tag_intent == expected


@pytest.mark.parametrize(('selected', 'scope'), [(0, 'one'), (1, 'one'), (2, 'one'), (2, 'all')])
def test_correction_inherits_selected_parents_and_message(
    tmp_path: Path, selected: int, scope: Literal['one', 'all']
) -> None:
    paths, g, project, commits = _replacement_history(tmp_path)
    before = _candidate.published_refs(paths, project.name)
    record = _candidate.build_candidate(
        paths,
        g,
        project,
        _candidate.CandidateInputs(
            _snapshot(),
            _pinned(),
            None,
            None,
            replaces=commits[selected],
            replace_scope=scope,
        ),
    )
    assert record.message == f'release {selected}'
    store = _candidate.store_path(paths, project.name)
    parents = _git(store, 'show', '-s', '--format=%P', record.commit).strip().decode()
    assert parents == (commits[selected - 1] if selected and scope == 'one' else '')
    assert _candidate.published_refs(paths, project.name) == before
    assert record.tip == (record.commit if selected == 2 else None)
    if selected == 2:
        assert (
            _candidate.verify_retained_candidate(paths, g, project, record, env={}) == _snapshot()
        )
    else:
        with pytest.raises(PubanonError, match='incomplete'):
            _candidate.verify_retained_candidate(paths, g, project, record, env={})


@pytest.mark.parametrize('tags', [(), ('v1.2.3',), ('v1.2.3', 'v2.0.0')])
def test_correction_moves_all_selected_tags_without_saved_provenance(
    tmp_path: Path, tags: tuple[str, ...]
) -> None:
    paths, g, project, commits = _replacement_history(tmp_path)
    store = _candidate.store_path(paths, project.name)
    for tag in tags:
        _git(store, 'update-ref', f'refs/tags/{tag}', commits[-1])
    record = _candidate.build_candidate(
        paths,
        g,
        project,
        _candidate.CandidateInputs(
            _snapshot(),
            _pinned(),
            None,
            None,
            replaces=commits[-1],
            replace_scope='one',
        ),
    )
    assert record.tag_intent == (tags[0] if len(tags) == 1 else None)
    assert record.poststate_tags == dict.fromkeys(tags, record.commit)
    _candidate.verify_publication_objects(paths, project.name, record)


@pytest.mark.parametrize('version', ['1.2.3', '9.0.0'])
def test_correction_version_must_match_one_of_several_moved_tags(
    tmp_path: Path,
    version: str,
) -> None:
    paths, g, project, commits = _replacement_history(tmp_path)
    store = _candidate.store_path(paths, project.name)
    for tag in ('v1.2.3', 'v2.0.0'):
        _git(store, 'update-ref', f'refs/tags/{tag}', commits[-1])
    inputs = _candidate.CandidateInputs(
        _snapshot(),
        _pinned(),
        version,
        None,
        replaces=commits[-1],
        replace_scope='one',
    )
    if version == '9.0.0':
        with pytest.raises(PubanonError, match='match a moved tag'):
            _candidate.build_candidate(paths, g, project, inputs)
    else:
        record = _candidate.build_candidate(paths, g, project, inputs)
        assert record.tag_intent is None
        assert record.poststate_tags == dict.fromkeys(('v1.2.3', 'v2.0.0'), record.commit)


def test_discard_confirmed_rewrite_preserves_correction_bookmark(
    tmp_path: Path, make_repo: Callable[..., Path]
) -> None:
    source = make_repo(files={'README.md': 'source'})
    paths, project, record = _build(tmp_path)
    pinned = _source.pin(source, 'HEAD')
    bookmark = _source.describe_bookmark(source, project.name, record.commit)
    _source.ensure_bookmark(bookmark, pinned.commit)
    record = replace(record, pinned=pinned, source_bookmark=bookmark)
    _records.save_candidate(paths, project.name, record)
    g, _ = _configs(tmp_path)
    store = _candidate.store_path(paths, project.name)
    derived = _oracle_commit(store, record.tree, g, 'derived release', 1_700_000_123)
    _git(store, 'update-ref', _records.publication_ref(derived), derived)
    progress = _records.RewriteRecord(
        record.commit,
        'b' * 40,
        'c' * 40,
        ('c' * 40,),
        'replaying',
        (_records.ReplayEntry('c' * 40, derived),),
    )
    _records.save_rewrite(paths, project.name, progress)
    with pytest.raises(PubanonError, match='confirmed publication'):
        _candidate.discard(paths, project.name, force=True)
    assert (
        _git(source, 'for-each-ref', '--format=%(objectname)', bookmark.refname).strip().decode()
        == pinned.commit
    )
    assert _records.load_candidate(paths, project.name) == record
    assert _records.load_rewrite(paths, project.name) == progress


def test_discard_deletes_symbolic_private_ref_without_changing_main(tmp_path: Path) -> None:
    paths, project, record = _build(tmp_path)
    store = _candidate.store_path(paths, project.name)
    _git(store, 'update-ref', 'refs/heads/main', record.commit)
    _git(store, 'symbolic-ref', _candidate.CANDIDATE_REF, 'refs/heads/main')

    assert _candidate.discard(paths, project.name, force=False)

    assert _git(store, 'rev-parse', 'refs/heads/main').strip().decode() == record.commit
    assert _git(store, 'for-each-ref', '--format=%(refname)', _candidate.CANDIDATE_REF) == b''


@pytest.mark.parametrize('corrupt', [b'', b'commit = "partial'])
def test_discard_force_removes_unreadable_candidate_state(
    corrupt: bytes,
    tmp_path: Path,
) -> None:
    paths, project, record = _build(tmp_path)
    project_dir = paths.data_dir / 'projects' / project.name
    record_path = project_dir / 'candidate.toml'
    inspect = project_dir / 'inspections' / record.commit
    inspect.mkdir(parents=True)
    (inspect / 'edited.txt').write_text('dirty\n', encoding='utf-8')
    record_path.write_bytes(corrupt)

    _candidate.discard(paths, project.name, force=True)

    assert not record_path.exists()
    assert (inspect / 'edited.txt').read_text() == 'dirty\n'
    store = _candidate.store_path(paths, project.name)
    assert _git(store, 'for-each-ref', '--format=%(refname)', _candidate.CANDIDATE_REF) == b''


@pytest.mark.parametrize('conflict', ['symbolic', 'wrong-object', 'provenance'])
def test_discard_force_retains_conflicting_confirmation_evidence(
    tmp_path: Path, conflict: str
) -> None:
    paths, project, record = _build(tmp_path)
    bookmark = _source.SourceBookmark(
        project.source_path,
        project.source_path / '.git',
        _source.bookmark_ref(project.name, record.commit),
    )
    record = replace(record, source_bookmark=bookmark)
    _candidate.save_candidate(paths, project.name, record)
    _history.record_publication(paths, project, record)
    store = _candidate.store_path(paths, project.name)
    ref = _records.publication_ref(record.commit)
    if conflict != 'provenance':
        _records.publication_path(paths, project.name, record.commit).unlink()
        if conflict == 'symbolic':
            _git(store, 'symbolic-ref', ref, _candidate.CANDIDATE_REF)
        else:
            _git(store, 'update-ref', ref, record.tree)
    else:
        record = replace(record, pinned=record.pinned._replace(commit='c' * 40))
        _candidate.save_candidate(paths, project.name, record)
    with pytest.raises(PubanonError):
        _candidate.discard(paths, project.name, force=True)
    assert _candidate.load_candidate(paths, project.name) == record


def test_discard_ignores_similarly_prefixed_archive_reference(tmp_path: Path) -> None:
    paths, project, record = _build(tmp_path)
    store = _candidate.store_path(paths, project.name)
    ref = _records.publication_ref(record.commit) + '/other'
    _git(store, 'update-ref', ref, record.commit)
    assert _candidate.discard(paths, project.name, force=False)
    assert _git(store, 'rev-parse', ref).decode().strip() == record.commit


def test_discard_protects_unknown_outcome_until_forced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths, project_config, record = _build(tmp_path)
    inspect = paths.data_dir / 'projects' / project_config.name / 'inspections' / record.commit
    inspect.mkdir(parents=True)
    (inspect / 'edited.txt').write_text('dirty\n')
    _candidate.save_candidate(
        paths,
        project_config.name,
        replace(record, state=_candidate.STATE_UNKNOWN_OUTCOME),
    )

    with pytest.raises(
        PubanonError, match='the outcome of the last publication is unknown'
    ) as error:
        _candidate.discard(paths, project_config.name, force=False)
    assert error.value.recovery == 'pubanon publish --resolve'
    assert (inspect / 'edited.txt').exists()

    _candidate.discard(paths, project_config.name, force=True)
    warning = capsys.readouterr().out
    assert 'snapshot recovery information' in warning
    assert 'does not undo any possible GitHub publication' in warning
    assert _candidate.load_candidate(paths, project_config.name) is None
    assert not inspect.exists()
    store = _candidate.store_path(paths, project_config.name)
    assert _git(store, 'for-each-ref', '--format=%(refname)', _candidate.CANDIDATE_REF) == b''


@pytest.mark.parametrize('force', [False, True])
@pytest.mark.parametrize('confirmed', ['record', 'archive'])
def test_discard_refuses_confirmed_publication(
    tmp_path: Path, force: bool, confirmed: str
) -> None:
    paths, project, record = _build(tmp_path)
    bookmark = _source.SourceBookmark(
        project.source_path,
        project.source_path / '.git',
        _source.bookmark_ref(project.name, record.commit),
    )
    record = replace(record, source_bookmark=bookmark)
    _candidate.save_candidate(paths, project.name, record)
    _history.record_publication(paths, project, record)
    if confirmed == 'archive':
        _records.publication_path(paths, project.name, record.commit).unlink()
    else:
        _git(
            _candidate.store_path(paths, project.name),
            'update-ref',
            '-d',
            _records.publication_ref(record.commit),
        )
    _candidate.check_recovery_state(paths, project.name, force=force)
    with pytest.raises(PubanonError, match='confirmed publication'):
        _candidate.discard(paths, project.name, force=force)
    assert _candidate.load_candidate(paths, project.name) == record


@pytest.mark.parametrize('condition', ['owned', 'missing', 'conflict', 'inaccessible'])
@pytest.mark.parametrize('force', [False, True])
def test_discard_removes_only_owned_provisional_bookmark(
    tmp_path: Path, make_repo: Callable[..., Path], condition: str, force: bool
) -> None:
    source = make_repo(files={'README.md': 'source'})
    paths, project, record = _build(tmp_path)
    bookmark = _source.describe_bookmark(source, project.name, record.commit)
    original = _source.pin(source, 'HEAD')
    record = replace(record, pinned=original, source_bookmark=bookmark)
    _candidate.save_candidate(paths, project.name, record)
    _source.ensure_bookmark(bookmark, original.commit)
    if condition == 'missing':
        _source.remove_bookmark(bookmark, original.commit)
    elif condition == 'conflict':
        _git(source, 'commit', '--allow-empty', '-qm', 'different')
        _git(source, 'update-ref', bookmark.refname, 'HEAD')
    elif condition == 'inaccessible':
        source.rename(source.with_name('moved-source'))
    if condition in {'conflict', 'inaccessible'}:
        with pytest.raises(PubanonError, match='snapshot retained'):
            _candidate.discard(paths, project.name, force=force)
        assert _candidate.load_candidate(paths, project.name) == record
    else:
        assert _candidate.discard(paths, project.name, force=force)
        assert _candidate.load_candidate(paths, project.name) is None
        assert _git(source, 'for-each-ref', '--format=%(refname)', bookmark.refname) == b''
        assert _git(source, 'rev-parse', 'HEAD').decode().strip() == original.commit


@pytest.mark.parametrize('force', [False, True])
def test_discard_reports_ref_failure_and_preserves_cleanup_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force: bool
) -> None:
    paths, project, record = _build(tmp_path)
    real_git = _candidate.run_git

    def _fail(args: Sequence[str], **kwargs: Unpack[_RunGitOptions]) -> GitResult:
        if args[2] == 'update-ref':
            return GitResult(7, b'', b'diagnostic\x1b\\path')
        return real_git(args, **kwargs)

    monkeypatch.setattr(_candidate, 'run_git', _fail)
    with pytest.raises(PubanonError) as error:
        _candidate.discard(paths, project.name, force=force)
    expected = r'exit status 7: diagnostic\x1b\\path'
    assert error.value.fact == (f'saved snapshot reference: {expected}' if force else expected)
    assert _candidate.load_candidate(paths, project.name) == (None if force else record)
    assert error.value.recovery == ('pubanon repo restore --to local' if force else None)


@pytest.mark.slow
@pytest.mark.parametrize('boundary', ['directory', 'bookmark', 'ref'])
def test_discard_rewrite_cleanup_failure_retains_locator_for_retry(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    source = make_repo(files={'README.md': 'source'})
    paths, project, record = _build(tmp_path)
    pinned = _source.pin(source, 'HEAD')
    original = _source.describe_bookmark(source, project.name, 'b' * 40)
    provisional = _source.describe_bookmark(source, project.name, 'c' * 40)
    correction = _source.describe_bookmark(source, project.name, record.commit)
    for bookmark in (original, provisional, correction):
        _source.ensure_bookmark(bookmark, pinned.commit)
    record = replace(record, pinned=pinned, source_bookmark=correction)
    _candidate.save_candidate(paths, project.name, record)
    replayed = _records.ReplayEntry('b' * 40, 'c' * 40, pinned.commit, original, provisional)
    progress = _records.RewriteRecord(
        record.commit, 'd' * 40, 'b' * 40, ('b' * 40,), 'replaying', (replayed,)
    )
    _records.save_rewrite(paths, project.name, progress)
    directory = _records.rewrite_directory(paths, project.name)
    directory.mkdir()
    (directory / 'resolution').write_text('retained decision')
    store = _candidate.candidate_store_path(paths, project.name)
    _git(store, 'update-ref', _candidate.REWRITE_MERGED_REF, record.commit)

    def _refuse_cleanup(_path: Path) -> None:
        raise OSError('busy')

    def _refuse_bookmark(*_args: object) -> None:
        raise PubanonError('bookmark busy')

    with monkeypatch.context() as fault:
        if boundary == 'directory':
            fault.setattr(_candidate.shutil, 'rmtree', _refuse_cleanup)
        elif boundary == 'bookmark':
            fault.setattr(_candidate, 'remove_bookmark', _refuse_bookmark)
        else:
            (store / (_candidate.REWRITE_MERGED_REF + '.lock')).touch()
        with pytest.raises(PubanonError) as error:
            _candidate.discard(paths, project.name, force=False)
    assert _records.load_candidate(paths, project.name) == record
    assert _records.load_rewrite(paths, project.name) == progress
    if boundary == 'directory':
        assert (directory / 'resolution').read_text() == 'retained decision'
        for bookmark in (provisional, correction):
            assert _git(source, 'rev-parse', bookmark.refname).strip().decode() == pinned.commit
    assert 'retry discard' in error.value.render()
    if boundary == 'ref':
        (store / (_candidate.REWRITE_MERGED_REF + '.lock')).unlink()
    assert _candidate.discard(paths, project.name, force=False)
    assert not _records.rewrite_path(paths, project.name).exists()
    assert not directory.exists()
    assert _git(source, 'rev-parse', original.refname).strip().decode() == pinned.commit
    for bookmark in (provisional, correction):
        assert _git(source, 'for-each-ref', '--format=%(objectname)', bookmark.refname) == b''


def test_discard_rewrite_deletes_symbolic_private_ref_without_changing_main(
    tmp_path: Path,
) -> None:
    paths, project, record = _build(tmp_path)
    store = _candidate.store_path(paths, project.name)
    _git(store, 'update-ref', 'refs/heads/main', record.commit)
    _git(store, 'symbolic-ref', _candidate.REWRITE_MERGED_REF, 'refs/heads/main')

    assert _candidate._discard_rewrite(paths, project.name, ())

    assert _git(store, 'rev-parse', 'refs/heads/main').strip().decode() == record.commit
    assert _git(store, 'for-each-ref', '--format=%(refname)', _candidate.REWRITE_MERGED_REF) == b''


def test_discard_rewrite_releases_derived_bookmark_but_preserves_original(
    tmp_path: Path, make_repo: Callable[..., Path]
) -> None:
    source = make_repo(files={'README.md': 'source'})
    paths, project, record = _build(tmp_path)
    original = _source.pin(source, 'HEAD')
    old = 'b' * 40
    derived = 'c' * 40
    original_bookmark = _source.describe_bookmark(source, project.name, old)
    derived_bookmark = _source.describe_bookmark(source, project.name, derived)
    _source.ensure_bookmark(original_bookmark, original.commit)
    _source.ensure_bookmark(derived_bookmark, original.commit)
    progress = _records.RewriteRecord(
        record.commit,
        'd' * 40,
        old,
        (old,),
        'replaying',
        (
            _records.ReplayEntry(
                old, derived, original.commit, original_bookmark, derived_bookmark
            ),
        ),
    )
    _records.save_rewrite(paths, project.name, progress)
    assert _candidate.discard(paths, project.name, force=False)
    assert _git(source, 'rev-parse', original_bookmark.refname).strip().decode() == original.commit
    assert _git(source, 'for-each-ref', '--format=%(objectname)', derived_bookmark.refname) == b''
    assert _git(source, 'rev-parse', 'HEAD').strip().decode() == original.commit


def test_discard_without_force_preserves_unreadable_candidate_state(tmp_path: Path) -> None:
    paths, project, record = _build(tmp_path)
    project_dir = paths.data_dir / 'projects' / project.name
    record_path = project_dir / 'candidate.toml'
    inspect = project_dir / 'inspections' / record.commit
    inspect.mkdir(parents=True)
    record_path.write_bytes(b'commit = "partial')

    with pytest.raises(PubanonError, match='invalid snapshot record'):
        _candidate.discard(paths, project.name, force=False)

    assert record_path.exists()
    assert inspect.exists()
    store = _candidate.store_path(paths, project.name)
    assert _git(store, 'rev-parse', _candidate.CANDIDATE_REF).strip()


@pytest.mark.parametrize('nested', [False, True])
def test_force_discard_removes_record_with_broken_store(
    tmp_path: Path, make_repo: Callable[..., Path], *, nested: bool
) -> None:
    if nested:
        tmp_path = make_repo()
        _git(tmp_path, 'update-ref', _candidate.CANDIDATE_REF, 'HEAD')
    ancestor_refs = _git(tmp_path, 'for-each-ref') if nested else None
    ancestor_objects = (
        _git(tmp_path, 'cat-file', '--batch-all-objects', '--batch-check') if nested else None
    )
    paths, project, _record = _build(tmp_path)
    store = paths.data_dir / 'projects' / project.name / 'store.git'
    shutil.rmtree(store / 'refs')

    with pytest.raises(PubanonError) as exc_info:
        _candidate.discard(paths, project.name, force=True)

    assert 'pubanon repo restore --to local' in exc_info.value.render()
    assert not (paths.data_dir / 'projects' / project.name / 'candidate.toml').exists()
    if nested:
        assert _git(tmp_path, 'for-each-ref') == ancestor_refs
        assert (
            _git(tmp_path, 'cat-file', '--batch-all-objects', '--batch-check') == ancestor_objects
        )


@pytest.mark.slow
def test_frozen_publication_verification_rejects_substituted_tag_mapping(tmp_path: Path) -> None:
    paths, _g, project, record = _complete_replacement(tmp_path)
    changed = replace(record, poststate_tags={'v1.0.0': record.commit})
    with pytest.raises(PubanonError, match='poststate'):
        _candidate.verify_publication_objects(paths, project.name, changed)


def test_incomplete_replacement_conflicts_with_archive_only(tmp_path: Path) -> None:
    paths, _global_config, project, record = _complete_replacement(tmp_path)
    store = _candidate.store_path(paths, project.name)
    assert _records.load_publication(paths, project.name, record.commit) is None
    _git(store, 'update-ref', _records.publication_ref(record.commit), record.commit)

    with pytest.raises(
        PubanonError, match='the incomplete snapshot conflicts with saved published history'
    ):
        _candidate.confirmed_publication(paths, project.name, replace(record, tip=None))


def test_initial_record_sync_failure_removes_commit_point_and_reruns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    global_config, project_config = _configs(tmp_path)
    record_path = paths.data_dir / 'projects' / project_config.name / 'candidate.toml'
    temporary = record_path.with_name('candidate.toml.tmp')
    original_fsync = os.fsync
    failed = False

    def _fail_first_record_sync(descriptor: int) -> None:
        nonlocal failed
        if not failed:
            failed = True
            assert temporary.exists()
            assert_private_mode(temporary, 0o600)
            raise OSError('/private/sync-failure-canary')
        original_fsync(descriptor)

    monkeypatch.setattr(_candidate.os, 'fsync', _fail_first_record_sync)

    with pytest.raises(PubanonError) as exc_info:
        _candidate.build_candidate(
            paths,
            global_config,
            project_config,
            _candidate.CandidateInputs(_snapshot(), _pinned(), None, None),
        )

    assert exc_info.value.render() == 'error: cannot save snapshot record'
    assert '/private/sync-failure-canary' not in exc_info.value.render()
    assert not record_path.exists()
    assert not temporary.exists()

    record = _candidate.build_candidate(
        paths,
        global_config,
        project_config,
        _candidate.CandidateInputs(_snapshot(), _pinned(), None, None),
    )
    assert _candidate.load_candidate(paths, project_config.name) == record


def test_inspect_clean_removes_known_copies_and_preserves_other_state(tmp_path: Path) -> None:
    paths, project, record = _build(tmp_path)
    previous_marker = _dirty_inspection(paths, project.name)
    selected = _candidate.materialize_inspect(paths, project.name, record)
    unrelated = selected.parent / 'notes'
    unrelated.mkdir()
    (unrelated / 'keep').write_text('unrelated')
    assert _candidate.clean_inspect(paths, project.name)
    assert not selected.exists()
    assert not previous_marker.parent.exists()
    assert (unrelated / 'keep').read_text() == 'unrelated'
    assert _candidate.load_candidate(paths, project.name) == record
    assert not _candidate.clean_inspect(paths, project.name)


def test_inspect_creates_regular_files_exclusively(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    global_config, project_config = _configs(tmp_path)
    record = _candidate.build_candidate(
        paths,
        global_config,
        project_config,
        _candidate.CandidateInputs(_snapshot(), _pinned(), None, None),
    )
    inspect = paths.data_dir / 'projects' / project_config.name / 'inspections' / record.commit
    original_open = cast('Callable[..., object]', Path.open)
    modes: list[str] = []

    def _tracked_open(self: Path, *args: object, **kwargs: object) -> object:
        mode = args[0] if args else kwargs.get('mode', 'r')
        assert isinstance(mode, str)
        if self.is_relative_to(inspect):
            modes.append(mode)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', _tracked_open)

    _candidate.materialize_inspect(paths, project_config.name, record)

    assert modes == ['xb', 'xb']


def test_inspect_interruption_removes_new_partial_copy_and_retry_completes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, project, record = _build(tmp_path)
    inspection = paths.data_dir / 'projects' / project.name / 'inspections' / record.commit
    previous_marker = _dirty_inspection(paths, project.name)
    original_chmod = _filesystem.chmod
    interruption = KeyboardInterrupt()
    written: list[Path] = []

    def _interrupt_after_first_file(
        path: Path, mode: int, *, follow_symlinks: bool = True
    ) -> None:
        original_chmod(path, mode, follow_symlinks=follow_symlinks)
        if path == inspection / 'README.md' and not written:
            assert path.read_bytes() == b'public\n'
            written.append(path)
            raise interruption

    monkeypatch.setattr(_filesystem, 'chmod', _interrupt_after_first_file)
    with pytest.raises(KeyboardInterrupt) as error:
        _candidate.materialize_inspect(paths, project.name, record)
    assert error.value is interruption
    assert written == [inspection / 'README.md']
    assert not inspection.exists()
    assert previous_marker.read_text() == 'keep this edit\n'
    assert _candidate.load_candidate(paths, project.name) == record

    monkeypatch.setattr(_filesystem, 'chmod', original_chmod)
    assert _candidate.materialize_inspect(paths, project.name, record) == inspection
    for entry in _snapshot():
        assert (inspection / entry.path).read_bytes() == entry.data
        assert_private_mode(inspection / entry.path, 0o500 if entry.mode == 0o100755 else 0o400)


def test_inspect_materializes_exact_files_modes_and_symlink_without_authority(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    global_config, project_config = _configs(tmp_path)
    snapshot = (
        Entry('README.md', 0o100644, 'blob', b'public\x00bytes\n'),
        Entry('bin/tool', 0o100755, 'blob', b'#!/bin/sh\n'),
        Entry('readme-link', 0o120000, 'symlink', b'README.md'),
    )
    record = _candidate.build_candidate(
        paths,
        global_config,
        project_config,
        _candidate.CandidateInputs(snapshot, _pinned(), None, None),
    )

    inspect = _candidate.materialize_inspect(paths, project_config.name, record)

    assert (inspect / 'README.md').read_bytes() == b'public\x00bytes\n'
    assert_private_mode(inspect, 0o500)
    assert_private_mode(inspect / 'bin', 0o500)
    assert_private_mode(inspect / 'README.md', 0o400)
    assert_private_mode(inspect / 'bin/tool', 0o500)
    assert os.fsencode((inspect / 'readme-link').readlink()) == b'README.md'
    assert not (inspect / '.git').exists()
    assert _candidate.materialize_inspect(paths, project_config.name, record) == inspect
    assert (inspect / 'README.md').read_bytes() == b'public\x00bytes\n'
    store = _candidate.store_path(paths, project_config.name)
    assert _git(store, 'rev-parse', f'{record.commit}^{{tree}}').strip().decode() == record.tree


@pytest.mark.skipif(os.name == 'nt', reason='POSIX symlink target bytes')
def test_inspect_preserves_raw_symlink_targets_across_reuse_and_refresh(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    global_config, project_config = _configs(tmp_path)
    snapshot = (
        Entry('file', 0o100644, 'blob', b'root\n'),
        Entry('relative/file', 0o100644, 'blob', b'nested\n'),
        Entry('dot-link', 0o120000, 'symlink', b'./file'),
        Entry('double-link', 0o120000, 'symlink', b'relative//file'),
    )
    record = _candidate.build_candidate(
        paths,
        global_config,
        project_config,
        _candidate.CandidateInputs(snapshot, _pinned(), None, None),
    )

    inspect = _candidate.materialize_inspect(paths, project_config.name, record)

    def _assert_raw_targets() -> None:
        assert os.fsencode(os.readlink(inspect / 'dot-link', dir_fd=None)) == b'./file'
        assert os.fsencode(os.readlink(inspect / 'double-link', dir_fd=None)) == b'relative//file'

    _assert_raw_targets()
    assert _candidate.materialize_inspect(paths, project_config.name, record) == inspect
    _assert_raw_targets()
    assert (
        _candidate.materialize_inspect(paths, project_config.name, record, refresh=True) == inspect
    )
    _assert_raw_targets()
    assert _candidate.materialize_inspect(paths, project_config.name, record) == inspect
    _assert_raw_targets()


def test_inspect_refresh_replaces_only_selected_candidate_copy(tmp_path: Path) -> None:
    paths, project, previous_record = _build(tmp_path)
    previous = _candidate.materialize_inspect(paths, project.name, previous_record)
    _candidate.discard(paths, project.name, force=False)
    global_config, _project = _configs(tmp_path)
    record = _candidate.build_candidate(
        paths,
        global_config,
        project,
        _candidate.CandidateInputs(_snapshot(), _pinned(), None, 'next candidate'),
    )
    selected = _candidate.materialize_inspect(paths, project.name, record)
    assert selected != previous
    assert selected.name == record.commit
    previous.mkdir()
    notes = previous / 'notes.txt'
    notes.write_text('keep previous notes')
    _filesystem.chmod(selected, 0o700)
    _filesystem.chmod(selected / 'README.md', 0o600)
    (selected / 'README.md').write_text('edited copy')
    (selected / 'build').mkdir()
    (selected / 'build/output').write_bytes(b'build output')

    assert _candidate.materialize_inspect(paths, project.name, record, refresh=True) == selected

    assert (selected / 'README.md').read_bytes() == b'public\n'
    assert not (selected / 'build').exists()
    assert notes.read_text() == 'keep previous notes'
    assert _candidate.load_candidate(paths, project.name) == record


def test_inspect_refuses_alias_symlink_before_replacing_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, project, record = _retain_alias_symlink_candidate(tmp_path)
    marker = _dirty_inspection(paths, project.name)

    def _refuse_materialization(*_args: object, **_kwargs: object) -> None:
        pytest.fail('unsafe retained graph reached symlink materialization')

    monkeypatch.setattr(Path, 'symlink_to', _refuse_materialization)

    with pytest.raises(PubanonError, match='unresolvable symlink target'):
        _candidate.materialize_inspect(paths, project.name, record)

    assert marker.read_text(encoding='utf-8') == 'keep this edit\n'
    assert sorted(path.name for path in marker.parent.iterdir()) == ['edited.txt']


@pytest.mark.parametrize('part', ['name', 'email'])
def test_inspect_refuses_bare_cr_in_shared_identity_before_cleaning(
    part: str,
    tmp_path: Path,
) -> None:
    paths, project, record = _build(tmp_path)
    marker = _dirty_inspection(paths, project.name)
    store = _candidate.candidate_store_path(paths, project.name)
    raw = _git(store, 'cat-file', 'commit', record.commit)
    records = raw.split(b'\n')
    if part == 'name':
        old, new = b'Anonymous Publisher', b'Anonymous\r Publisher'
    else:
        old, new = b'anonymous@example.invalid', b'anonymous\r@example.invalid'
    for field in (b'author ', b'committer '):
        index = next(index for index, value in enumerate(records) if value.startswith(field))
        records[index] = records[index].replace(old, new, 1)
    changed = _replace_candidate_commit(paths, project, record, b'\n'.join(records))

    with pytest.raises(
        PubanonError,
        match='snapshot commit metadata does not match the record',
    ) as exc_info:
        _candidate.materialize_inspect(paths, project.name, changed)

    assert exc_info.value.recovery == 'pubanon discard, then pubanon prepare'
    assert marker.read_text(encoding='utf-8') == 'keep this edit\n'


def test_inspect_refuses_candidate_ref_tamper_before_cleaning(tmp_path: Path) -> None:
    paths, project, record = _build(tmp_path)
    marker = _dirty_inspection(paths, project.name)
    _git(
        _candidate.candidate_store_path(paths, project.name),
        'update-ref',
        '-d',
        _candidate.CANDIDATE_REF,
    )

    with pytest.raises(PubanonError, match='saved snapshot commit could not be verified'):
        _candidate.materialize_inspect(paths, project.name, record)

    assert marker.read_text(encoding='utf-8') == 'keep this edit\n'


@pytest.mark.parametrize(
    'tamper',
    ['additional-header', 'identity', 'timestamp', 'offset'],
)
def test_inspect_refuses_commit_header_tamper_before_cleaning(
    tamper: str,
    tmp_path: Path,
) -> None:
    paths, project, record = _build(tmp_path)
    marker = _dirty_inspection(paths, project.name)
    store = _candidate.candidate_store_path(paths, project.name)
    raw = _git(store, 'cat-file', 'commit', record.commit)
    if tamper == 'additional-header':
        raw = raw.replace(b'\nauthor ', b'\ngpgsig forged-signature\nauthor ', 1)
    elif tamper == 'identity':
        raw = raw.replace(
            b'committer Anonymous Publisher <anonymous@example.invalid>',
            b'committer Different Publisher <different@example.invalid>',
            1,
        )
    elif tamper == 'timestamp':
        raw = re.sub(rb'(?m)^(author .+ )\d+( \+0000)$', rb'\1not-a-time\2', raw, count=1)
    else:
        raw = raw.replace(b' +0000\n', b' -0700\n', 1)
    changed = _replace_candidate_commit(paths, project, record, raw)

    with pytest.raises(PubanonError, match='snapshot commit metadata does not match the record'):
        _candidate.materialize_inspect(paths, project.name, changed)

    assert marker.read_text(encoding='utf-8') == 'keep this edit\n'


def test_inspect_refuses_commit_tree_tamper_before_cleaning(tmp_path: Path) -> None:
    paths, project, record = _build(tmp_path)
    marker = _dirty_inspection(paths, project.name)
    store = _candidate.candidate_store_path(paths, project.name)
    other_tree = _oracle_tree(
        store,
        (Entry('replacement.txt', 0o100644, 'blob', b'replacement\n'),),
    )
    changed = replace(record, tree=other_tree)
    _candidate.save_candidate(paths, project.name, changed)

    with pytest.raises(PubanonError, match='snapshot commit metadata does not match the record'):
        _candidate.materialize_inspect(paths, project.name, changed)

    assert marker.read_text(encoding='utf-8') == 'keep this edit\n'


@pytest.mark.parametrize('field', ['author', 'committer'])
def test_inspect_refuses_crlf_identity_header_before_cleaning(
    field: str,
    tmp_path: Path,
) -> None:
    paths, project, record = _build(tmp_path)
    marker = _dirty_inspection(paths, project.name)
    store = _candidate.candidate_store_path(paths, project.name)
    raw = _git(store, 'cat-file', 'commit', record.commit)
    records = raw.split(b'\n')
    index = next(
        index for index, value in enumerate(records) if value.startswith(f'{field} '.encode())
    )
    records[index] += b'\r'
    changed = _replace_candidate_commit(paths, project, record, b'\n'.join(records))

    with pytest.raises(
        PubanonError,
        match='snapshot commit metadata does not match the record',
    ) as exc_info:
        _candidate.materialize_inspect(paths, project.name, changed)

    assert exc_info.value.recovery == 'pubanon discard, then pubanon prepare'
    assert marker.read_text(encoding='utf-8') == 'keep this edit\n'


def test_inspect_refuses_invalid_tag_intent_before_cleaning(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    global_config, project = _configs(tmp_path)
    record = _candidate.build_candidate(
        paths,
        global_config,
        project,
        _candidate.CandidateInputs(_snapshot(), _pinned(), '1.2.3', None),
    )
    marker = _dirty_inspection(paths, project.name)
    candidate_path = _candidate.record_path(paths, project.name)
    candidate_path.write_text(
        candidate_path.read_text(encoding='utf-8').replace('v1.2.3', 'v01.2.3'),
        encoding='utf-8',
    )
    invalid = replace(record, tag_intent='v01.2.3', message='release: v01.2.3')

    with pytest.raises(PubanonError, match='invalid snapshot record'):
        _candidate.materialize_inspect(paths, project.name, invalid)

    assert marker.read_text(encoding='utf-8') == 'keep this edit\n'


def test_inspect_refuses_physical_alias_symlink_escape(tmp_path: Path) -> None:
    probe = tmp_path / 'probe'
    probe.mkdir()
    (probe / 'A').symlink_to('.', target_is_directory=True)
    if not (probe / 'a').exists():
        pytest.skip('requires a case-insensitive filesystem')
    sibling = tmp_path / 'sibling'
    sibling.write_bytes(b'synthetic sibling sentinel\n')
    (probe / 'B').symlink_to(Path('a/../sibling'))
    try:
        resolved = (probe / 'B').read_bytes()
    except OSError:
        pytest.skip('filesystem does not resolve parent traversal through this symlink alias')
    assert resolved == sibling.read_bytes()

    paths, project, record = _retain_alias_symlink_candidate(tmp_path)
    marker = _dirty_inspection(paths, project.name)
    inspection_sibling = marker.parent.parent / 'sibling'
    inspection_sibling.write_bytes(sibling.read_bytes())

    with pytest.raises(PubanonError, match='unresolvable symlink target'):
        _candidate.materialize_inspect(paths, project.name, record)

    assert marker.read_text(encoding='utf-8') == 'keep this edit\n'
    assert not (marker.parent / 'B').is_symlink()
    assert inspection_sibling.read_bytes() == b'synthetic sibling sentinel\n'


def test_inspect_refuses_record_change_during_proof_before_cleaning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, project, record = _build(tmp_path)
    marker = _dirty_inspection(paths, project.name)
    original_read_snapshot = _candidate.read_snapshot

    def _change_record(store: Path, commit: str, *, git_dir: Path | None = None) -> Snapshot:
        snapshot = original_read_snapshot(store, commit, git_dir=git_dir)
        _candidate.save_candidate(
            paths,
            project.name,
            replace(record, message='changed during proof'),
        )
        return snapshot

    monkeypatch.setattr(_candidate, 'read_snapshot', _change_record)

    with pytest.raises(PubanonError, match='snapshot record changed during verification'):
        _candidate.materialize_inspect(paths, project.name, record)

    assert marker.read_text(encoding='utf-8') == 'keep this edit\n'


@pytest.mark.parametrize('field', ['parent', 'message'])
def test_inspect_refuses_recorded_commit_fact_tamper_before_cleaning(
    field: str,
    tmp_path: Path,
) -> None:
    paths, project, record = _build(tmp_path)
    marker = _dirty_inspection(paths, project.name)
    changed = (
        replace(record, prestate_main='f' * 40)
        if field == 'parent'
        else replace(record, message='changed message')
    )
    _candidate.save_candidate(paths, project.name, changed)

    with pytest.raises(PubanonError, match='snapshot commit metadata does not match the record'):
        _candidate.materialize_inspect(paths, project.name, changed)

    assert marker.read_text(encoding='utf-8') == 'keep this edit\n'


def test_inspect_refuses_snapshot_digest_tamper_before_cleaning(tmp_path: Path) -> None:
    paths, project, record = _build(tmp_path)
    marker = _dirty_inspection(paths, project.name)
    changed = replace(record, snapshot_digest='0' * 64)
    _candidate.save_candidate(paths, project.name, changed)

    with pytest.raises(PubanonError, match='saved snapshot files do not match what was prepared'):
        _candidate.materialize_inspect(paths, project.name, changed)

    assert marker.read_text(encoding='utf-8') == 'keep this edit\n'


@pytest.mark.parametrize('symlink_parent', [False, True])
def test_inspect_refuses_symlink_copy_or_parent_without_touching_target(
    tmp_path: Path, symlink_parent: bool
) -> None:
    paths, project, record = _build(tmp_path)
    root = paths.data_dir / 'projects' / project.name / 'inspections'
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'notes').write_text('keep outside notes')
    if symlink_parent:
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir()
        (root / record.commit).symlink_to(outside, target_is_directory=True)
    with pytest.raises(PubanonError, match='symlink'):
        _candidate.materialize_inspect(paths, project.name, record)
    with pytest.raises(PubanonError, match=r'symlink|cannot clean review copy'):
        _candidate.clean_inspect(paths, project.name)
    assert (outside / 'notes').read_text() == 'keep outside notes'


def test_inspect_refuses_unexpected_filesystem_directory_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    global_config, project_config = _configs(tmp_path)
    snapshot = (
        Entry('A/first.txt', 0o100644, 'blob', b'first\n'),
        Entry('B/second.txt', 0o100644, 'blob', b'second\n'),
    )
    record = _candidate.build_candidate(
        paths,
        global_config,
        project_config,
        _candidate.CandidateInputs(snapshot, _pinned(), None, None),
    )
    inspect = paths.data_dir / 'projects' / project_config.name / 'inspections' / record.commit
    original_mkdir = Path.mkdir

    def _case_insensitive_mkdir(
        self: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if self == inspect / 'B':
            raise FileExistsError
        original_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, 'mkdir', _case_insensitive_mkdir)

    with pytest.raises(PubanonError, match='paths collide in the review copy'):
        _candidate.materialize_inspect(paths, project_config.name, record)
    assert not inspect.exists()
    monkeypatch.setattr(Path, 'mkdir', original_mkdir)
    assert _candidate.materialize_inspect(paths, project_config.name, record) == inspect
    assert (inspect / 'B/second.txt').read_bytes() == b'second\n'


def test_inspect_report_bounds_file_list_and_saves_complete_private_report(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    global_config, project = _configs(tmp_path)
    snapshot = tuple(Entry(f'file-{i:03}.txt', 0o100644, 'blob', b'line\n') for i in range(30))
    record = _candidate.build_candidate(
        paths, global_config, project, _candidate.CandidateInputs(snapshot, _pinned(), None, None)
    )

    report = _candidate.inspect_report(paths, record, global_config, project)

    assert '30 files changed' in report
    assert 'file-000.txt' in report
    assert 'file-029.txt' not in report
    complete = paths.data_dir / 'projects' / project.name / f'candidate-files-{record.commit}.txt'
    assert escape(str(complete)) in report
    assert_private_mode(complete, 0o600)
    assert 'file-000.txt' in complete.read_text()
    assert 'file-029.txt' in complete.read_text()
    assert 'Snapshot untagged (prepared)' in report
    assert 'Checks:' in report


def test_inspect_report_marks_changed_configuration_without_claiming_current_gates(
    tmp_path: Path,
) -> None:
    paths, project_config, record = _build(tmp_path)
    global_config, _ = _configs(tmp_path)
    changed = replace(project_config, extra_terms=('new-private-term',))

    report = _candidate.inspect_report(paths, record, global_config, changed)

    assert 'no sensitive terms found when prepared' in report
    assert 'snapshot verified' in report
    assert 'Publication configuration: changed since prepare' in report
    assert 'checks have not been rerun with these settings' in report
    assert 'snapshot cannot publish' in report
    assert (
        f'pubanon prepare --project={project_config.name} --source {record.pinned.commit}'
        in _candidate.inspect_next_action(record, global_config, changed)
    )
    if record.tag_intent is not None:
        assert f'--version {record.tag_intent[1:]}' in _candidate.inspect_next_action(
            record, global_config, changed
        )


def test_inspect_report_renders_first_release_facts_and_credential_exceptions(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    global_config, project_config = _configs(tmp_path)
    project_config = replace(project_config, secret_exceptions=('canaryx.env',))
    record = _candidate.build_candidate(
        paths,
        global_config,
        project_config,
        _candidate.CandidateInputs(
            _snapshot(), _pinned(), '1.2.3', 'release: v1.2.3\n\nDetails: forged\x1b\rlabel'
        ),
    )

    report = _candidate.inspect_report(paths, record, global_config, project_config)

    assert report.startswith('Snapshot ')
    assert 'no sensitive terms found when prepared' in report
    assert 'snapshot verified' in report
    assert 'Publication configuration:' not in report
    assert 'GitHub not rechecked' in report
    assert _candidate.inspect_next_action(record, global_config, project_config) == (
        'pubanon publish'
    )
    assert 'Snapshot commit:' not in report
    assert 'Source commit:' not in report
    assert 'Anonymous Publisher <anonymous@example.invalid>' in report
    assert 'Message:' in report
    assert '  release: v1.2.3\n  \n' in report
    assert r'  Details: forged\x1b\x0dlabel' in report
    assert 'Destination: public-owner/public-repo (public)' in report
    assert 'Snapshot v1.2.3 (prepared)' in report
    assert 'first release, 2 files' in report
    assert '2 files changed' in report
    assert 'credential-like' not in report
    assert 'canaryx' not in report

    changed_report = _candidate.inspect_report(
        paths,
        record,
        replace(global_config, anon_name='Replacement Publisher'),
        replace(project_config, owner='replacement-owner', visibility='private'),
    )
    assert 'Anonymous Publisher <anonymous@example.invalid>' in changed_report
    assert re.search(r'Destination:\s+public-owner/public-repo \(public\)', changed_report)
    assert 'checks have not been rerun with these settings' in changed_report
    assert 'Replacement Publisher' not in changed_report


def test_inspect_report_renders_untrusted_fields_without_escaping_trusted_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    global_config, project_config = _configs(tmp_path)
    term = 'private-canary'
    project_config = replace(
        project_config,
        secret_exceptions=(f'fixtures/allowed\x1b{term}.key',),
    )
    record = _candidate.build_candidate(
        paths,
        global_config,
        project_config,
        _candidate.CandidateInputs(_snapshot(), _pinned(), None, None),
    )

    real_git_result = _candidate._git_result

    def _git_result(
        _args: Sequence[str],
        *,
        store: Path,
        failure: str,
        input_bytes: bytes | None = None,
        env_extra: Mapping[str, str] | None = None,
    ) -> bytes:
        if '--stat' not in _args and '--numstat' not in _args:
            return real_git_result(
                _args, store=store, failure=failure, input_bytes=input_bytes, env_extra=env_extra
            )
        return f'docs/changed\x1b{term}.txt | 1 +\nInjected: false detail\n'.encode()

    monkeypatch.setattr(_candidate, '_git_result', _git_result)

    report = _candidate.inspect_report(paths, record, global_config, project_config)

    assert report.startswith('Snapshot ')
    assert '\x1b' not in report
    assert term in report
    assert f'  docs/changed\\x1b{term}.txt | 1 +' in report
    assert '  Injected: false detail' in report
    assert 'Credential-like exceptions:' not in report
    assert sum(line.startswith('Snapshot ') for line in report.splitlines()) == 1


def test_inspect_report_routes_unknown_outcome_to_resolution_before_configuration_drift(
    tmp_path: Path,
) -> None:
    paths, project_config, record = _build(tmp_path)
    global_config, _ = _configs(tmp_path)
    changed = replace(project_config, extra_terms=('new-private-term',))
    record = replace(record, state=_candidate.STATE_UNKNOWN_OUTCOME)
    _candidate.save_candidate(paths, project_config.name, record)

    report = _candidate.inspect_report(paths, record, global_config, changed)

    assert 'Publication configuration: changed since prepare' in report
    assert 'resolve the publication outcome first' in report
    assert 'discard then prepare' not in report
    assert _candidate.inspect_next_action(record, global_config, changed) == (
        'pubanon publish --resolve'
    )
    assert 'Next: run `pubanon discard`' not in report


@pytest.mark.parametrize('change', ['content', 'extra', 'missing', 'executable', 'symlink'])
def test_inspect_reuse_refuses_changed_copy_until_explicit_refresh(
    tmp_path: Path, change: str
) -> None:
    paths = _paths(tmp_path)
    global_config, project = _configs(tmp_path)
    snapshot = (*_snapshot(), Entry('link', 0o120000, 'symlink', b'README.md'))
    record = _candidate.build_candidate(
        paths, global_config, project, _candidate.CandidateInputs(snapshot, _pinned(), None, None)
    )
    inspect = _candidate.materialize_inspect(paths, project.name, record)
    _filesystem.chmod(inspect, 0o700)
    if change == 'content':
        _filesystem.chmod(inspect / 'README.md', 0o600)
        (inspect / 'README.md').write_bytes(b'changed')
    elif change == 'extra':
        (inspect / 'extra.txt').write_bytes(b'extra')
    elif change == 'missing':
        _filesystem.chmod(inspect / 'README.md', 0o600)
        (inspect / 'README.md').unlink()
    elif change == 'executable':
        _filesystem.chmod(inspect / 'bin/tool', 0o400)
        if os.name == 'nt':
            assert _candidate.materialize_inspect(paths, project.name, record) == inspect
            return
    else:
        (inspect / 'link').unlink()
        (inspect / 'link').symlink_to('bin/tool')

    with pytest.raises(PubanonError) as error:
        _candidate.materialize_inspect(paths, project.name, record)
    assert error.value.recovery == 'pubanon inspect --refresh'
    assert _candidate.materialize_inspect(paths, project.name, record, refresh=True) == inspect
    assert_private_mode(inspect, 0o500)
    assert (inspect / 'README.md').read_bytes() == b'public\n'
    assert_private_mode(inspect / 'bin/tool', 0o500)
    assert inspect.joinpath('link').readlink() == Path('README.md')


def test_inspect_reuse_refuses_symlink_directory_without_touching_external_tree(
    tmp_path: Path,
) -> None:
    paths, project, record = _build(tmp_path)
    inspect = _candidate.materialize_inspect(paths, project.name, record)
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'tool'
    sentinel.write_bytes(b'outside')
    _filesystem.chmod(inspect, 0o700)
    _filesystem.chmod(inspect / 'bin', 0o700)
    _filesystem.chmod(inspect / 'bin/tool', 0o700)
    (inspect / 'bin/tool').unlink()
    (inspect / 'bin').rmdir()
    (inspect / 'bin').symlink_to(outside, target_is_directory=True)

    with pytest.raises(PubanonError) as error:
        _candidate.materialize_inspect(paths, project.name, record)
    assert error.value.recovery == 'pubanon inspect --refresh'
    assert sentinel.read_bytes() == b'outside'
    _candidate.materialize_inspect(paths, project.name, record, refresh=True)
    assert sentinel.read_bytes() == b'outside'
    assert (inspect / 'bin/tool').read_bytes() == b'#!/bin/sh\n'


@pytest.mark.parametrize('operation', ['read', 'lock'])
def test_inspect_reuse_reports_access_failures_separately_from_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    paths, project, record = _build(tmp_path)
    inspect = _candidate.materialize_inspect(paths, project.name, record)
    if operation == 'read':
        original_read = Path.read_bytes

        def _fail_read(path: Path) -> bytes:
            if path == inspect / 'README.md':
                raise PermissionError('cannot read')
            return original_read(path)

        if os.name == 'nt':
            original_entry_read = _candidate._windows_fs.read_entry

            def _fail_entry_read(path: Path) -> tuple[int, bytes]:
                if path == inspect / 'README.md':
                    raise PermissionError('cannot read')
                return original_entry_read(path)

            monkeypatch.setattr(_candidate._windows_fs, 'read_entry', _fail_entry_read)
        else:
            monkeypatch.setattr(Path, 'read_bytes', _fail_read)
        expected = 'cannot verify review directory'
    else:
        original_chmod = _filesystem.chmod

        def _fail_lock(path: Path, mode: int, *, follow_symlinks: bool = True) -> None:
            if path == inspect / 'README.md':
                raise PermissionError('cannot chmod')
            original_chmod(path, mode, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(_filesystem, 'chmod', _fail_lock)
        expected = 'cannot lock review directory'

    with pytest.raises(PubanonError, match=expected) as error:
        _candidate.materialize_inspect(paths, project.name, record)
    assert error.value.recovery is None


def test_inspect_reuse_verifies_existing_editable_copy_then_locks_it(tmp_path: Path) -> None:
    paths, project, record = _build(tmp_path)
    inspect = _candidate.materialize_inspect(paths, project.name, record)
    _filesystem.chmod(inspect, 0o700)
    _filesystem.chmod(inspect / 'bin', 0o700)
    _filesystem.chmod(inspect / 'README.md', 0o600)
    _filesystem.chmod(inspect / 'bin/tool', 0o700)

    assert _candidate.materialize_inspect(paths, project.name, record) == inspect
    assert_private_mode(inspect, 0o500)
    assert_private_mode(inspect / 'bin', 0o500)
    assert_private_mode(inspect / 'README.md', 0o400)
    assert_private_mode(inspect / 'bin/tool', 0o500)


@pytest.mark.parametrize('refresh', [False, True])
def test_inspect_verifies_retained_objects_before_reusing_or_refreshing_copy(
    tmp_path: Path, refresh: bool
) -> None:
    paths, project, record = _build(tmp_path)
    inspection = _candidate.materialize_inspect(paths, project.name, record)
    _filesystem.chmod(inspection, 0o700)
    (inspection / 'notes').write_text('keep notes')
    _git(_candidate.store_path(paths, project.name), 'update-ref', '-d', _candidate.CANDIDATE_REF)
    with pytest.raises(PubanonError, match='saved snapshot commit could not be verified'):
        _candidate.materialize_inspect(paths, project.name, record, refresh=refresh)
    assert (inspection / 'notes').read_text() == 'keep notes'


def test_inspect_warns_only_about_credential_files_actually_in_snapshot(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    global_config, project = _configs(tmp_path)
    project = replace(project, secret_exceptions=('kept.key', 'absent.key'))
    record = _candidate.build_candidate(
        paths,
        global_config,
        project,
        _candidate.CandidateInputs(
            (*_snapshot(), Entry('kept.key', 0o100644, 'blob', b'public fixture')),
            _pinned(),
            None,
            None,
        ),
    )
    changed = replace(project, secret_exceptions=())
    report = _candidate.inspect_report(paths, record, global_config, changed)
    assert 'warning: Snapshot includes 1 credential-like file: kept.key.' in report
    assert 'absent.key' not in report


def test_inspection_path_validates_components_without_creating_directories(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    commit = 'a' * 40

    inspection = _candidate.inspection_path(paths, 'example', commit)

    assert inspection == paths.data_dir / 'projects' / 'example' / 'inspections' / commit
    assert not paths.data_dir.exists()
    with pytest.raises(PubanonError, match='invalid project name'):
        _candidate.inspection_path(paths, '../outside', commit)
    with pytest.raises(PubanonError, match='invalid review copy commit'):
        _candidate.inspection_path(paths, 'example', '../outside')
    assert not paths.data_dir.exists()


def test_materialization_explains_missing_windows_symlink_privilege(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = OSError('privilege not held')
    monkeypatch.setattr(failure, 'winerror', 1314, raising=False)

    def _symlink_to(_self: Path, _target: str, target_is_directory: bool = False) -> None:
        del target_is_directory
        raise failure

    monkeypatch.setattr(Path, 'symlink_to', _symlink_to)
    directory = tmp_path / 'snapshot'
    with pytest.raises(PubanonError, match='Windows cannot create snapshot symlinks') as error:
        _candidate.materialize_directory(
            directory, (Entry('link', 0o120000, 'symlink', b'missing'),)
        )
    assert error.value.recovery is not None
    assert 'Developer Mode' in error.value.recovery
    assert not directory.exists()


def test_materialization_preserves_forward_directory_symlink(tmp_path: Path) -> None:
    directory = tmp_path / 'snapshot'
    snapshot = (
        Entry('link', 0o120000, 'symlink', b'nested'),
        Entry('nested/file', 0o100644, 'blob', b'content'),
    )
    _candidate.materialize_directory(directory, snapshot)
    assert (directory / 'link').is_symlink()
    assert (directory / 'link').is_dir()
    assert (directory / 'link/file').read_bytes() == b'content'
    assert (directory / 'link').readlink() == Path('nested')


def test_materialization_protects_each_directory_before_writing_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / 'snapshot'
    original_open = cast('Callable[..., object]', Path.open)

    def _checked_open(self: Path, *args: object, **kwargs: object) -> object:
        if self.is_relative_to(directory):
            parent = self.parent
            while parent.is_relative_to(directory):
                assert_private_mode(parent, 0o700)
                parent = parent.parent
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', _checked_open)
    _candidate.materialize_directory(
        directory, (Entry('nested/deep/file', 0o100644, 'blob', b'content'),)
    )
    assert (directory / 'nested/deep/file').read_bytes() == b'content'


def test_materialize_directory_is_exclusive_private_and_cleans_partial_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / 'resolution'
    _candidate.materialize_directory(target, _snapshot())
    assert_private_mode(target, 0o700)
    with pytest.raises(PubanonError):
        _candidate.materialize_directory(target, _snapshot())
    assert target.exists()
    broken = tmp_path / 'broken'

    def _fail(_directory: Path, _snapshot: Snapshot) -> None:
        raise OSError('write failed')

    monkeypatch.setattr(_candidate, '_materialize_inspection_files', _fail)
    with pytest.raises(PubanonError):
        _candidate.materialize_directory(broken, _snapshot())
    assert not broken.exists()


@pytest.mark.parametrize(
    'snapshot',
    [
        (Entry('escape', 0o120000, 'symlink', b'../outside'),),
        (Entry('bad', 0o100600, 'blob', b'bytes'),),
        (Entry('A', 0o100644, 'blob', b'a'), Entry('a', 0o100644, 'blob', b'b')),
    ],
)
def test_materialize_directory_validates_before_creation(
    tmp_path: Path, snapshot: Snapshot
) -> None:
    target = tmp_path / 'invalid'
    with pytest.raises(PubanonError):
        _candidate.materialize_directory(target, snapshot)
    assert not target.exists()


@pytest.mark.slow
def test_multi_snapshot_replacement_keeps_correction_and_final_facts_together(
    tmp_path: Path,
) -> None:
    paths, global_config, project, record = _complete_replacement(tmp_path, tagged=True)
    assert record.tip is not None
    assert record.tip != record.commit

    for report in (
        _candidate.inspect_report(paths, record, global_config, project),
        _candidate.publication_report(paths, record, global_config, project),
    ):
        assert report.startswith('Prepared snapshots (prepared)\n')
        assert re.search(r'Correction version:\s+v0.0.0', report)
        assert re.search(r'Correction message:\s+release 0', report)
        assert 'Correction source commit:' not in report
        assert re.search(rf'Correction snapshot ID:\s+{record.commit[:12]}', report)
        assert re.search(r'Final version:\s+v2.0.0', report)
        assert re.search(rf'Final snapshot ID:\s+{record.tip[:12]}', report)
        assert re.search(r'Final snapshot changes:\s+since recorded publication base', report)
        assert 'Snapshot commit:' not in report


def test_publication_report_leaves_live_facts_to_caller(tmp_path: Path) -> None:
    paths, project, record = _build(tmp_path)
    global_config, _project = _configs(tmp_path)
    report = _candidate.publication_report(paths, record, global_config, project)
    assert 'Next:' not in report
    assert 'Live destination and visibility: not rechecked now' not in report
    assert 'Verified destination' not in report
    assert 'Snapshot commit:' not in report
    assert record.message in report
    assert 'Anonymous Publisher <anonymous@example.invalid>' in report
    assert 'File list:' in report


@pytest.mark.parametrize('directory', ['publications', 'source-worktrees'])
def test_publication_state_in_committed_retained_snapshot_is_refused(
    tmp_path: Path, make_repo: Callable[..., Path], directory: str
) -> None:
    selected = f'state/pubanon/projects/example/{directory}/private.txt'
    source = make_repo(files={selected: 'private state', 'publications/ordinary.txt': 'ordinary'})
    paths = _paths(source)
    paths = paths._replace(data_dir=source / 'state' / 'pubanon')
    global_config, project = _configs(tmp_path)
    project = replace(project, source_path=source.resolve(), allow=('**',))
    pinned = _source.pin(source, 'HEAD')
    snapshot = read_snapshot(source, pinned.commit)
    record = _candidate.build_candidate(
        paths, global_config, project, _candidate.CandidateInputs(snapshot, pinned, None, None)
    )
    with pytest.raises(PubanonError, match='publication state'):
        _candidate.verify_retained_candidate(paths, global_config, project, record, env={})


def test_published_refs_refuses_malformed_tag_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    _candidate.store_path(paths, 'example')
    original_run_git = _candidate.run_git

    def _malformed_tags(args: Sequence[str], **kwargs: Unpack[_RunGitOptions]) -> GitResult:
        if args[-1] == 'refs/tags':
            return GitResult(0, b'malformed\n', b'')
        return original_run_git(args, **kwargs)

    monkeypatch.setattr(_candidate, 'run_git', _malformed_tags)

    with pytest.raises(PubanonError, match='Git returned invalid snapshot store tags'):
        _candidate.published_refs(paths, 'example')


def test_published_refs_returns_empty_without_creating_store(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = paths.data_dir / 'projects' / 'example' / 'store.git'

    assert _candidate.published_refs(paths, 'example') == (None, {})
    assert not store.exists()


def test_published_refs_returns_main_and_complete_tag_map(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = _candidate.store_path(paths, 'example')

    environment = {
        'GIT_AUTHOR_DATE': '1700000000 +0000',
        'GIT_AUTHOR_EMAIL': 'anonymous@example.invalid',
        'GIT_AUTHOR_NAME': 'Anonymous Publisher',
        'GIT_COMMITTER_DATE': '1700000000 +0000',
        'GIT_COMMITTER_EMAIL': 'anonymous@example.invalid',
        'GIT_COMMITTER_NAME': 'Anonymous Publisher',
    }
    tree = (
        run_git(['hash-object', '-t', 'tree', '-w', '--stdin'], cwd=store, input_bytes=b'')
        .stdout.strip()
        .decode('ascii')
    )
    main = (
        run_git(['commit-tree', tree], cwd=store, input_bytes=b'main\n', env_extra=environment)
        .stdout.strip()
        .decode('ascii')
    )
    other = (
        run_git(['commit-tree', tree], cwd=store, input_bytes=b'other\n', env_extra=environment)
        .stdout.strip()
        .decode('ascii')
    )
    run_git(['update-ref', 'refs/heads/main', main], cwd=store)
    run_git(['update-ref', 'refs/tags/v1.0.0', main], cwd=store)
    run_git(['update-ref', 'refs/tags/v2.0.0', other], cwd=store)

    assert _candidate.published_refs(paths, 'example') == (
        main,
        {'v1.0.0': main, 'v2.0.0': other},
    )


def test_record_write_failure_leaves_rerunnable_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    global_config, project_config = _configs(tmp_path)
    original_save = _candidate.save_candidate
    attempts = 0

    def _fail_once(
        candidate_paths: Paths,
        project: str,
        record: _candidate.CandidateRecord,
    ) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PubanonError('injected record failure')
        original_save(candidate_paths, project, record)

    monkeypatch.setattr(_candidate, 'save_candidate', _fail_once)

    with pytest.raises(PubanonError, match='injected record failure'):
        _candidate.build_candidate(
            paths,
            global_config,
            project_config,
            _candidate.CandidateInputs(_snapshot(), _pinned(), None, None),
        )

    store = _candidate.store_path(paths, project_config.name)
    assert _candidate.load_candidate(paths, project_config.name) is None
    assert _git(store, 'rev-parse', _candidate.CANDIDATE_REF).strip()

    record = _candidate.build_candidate(
        paths,
        global_config,
        project_config,
        _candidate.CandidateInputs(_snapshot(), _pinned(), None, None),
    )
    assert _candidate.load_candidate(paths, project_config.name) == record


@pytest.mark.parametrize('visibility', ['public', 'private'])
def test_replace_all_retires_old_tags_and_creates_only_intended_tag(
    tmp_path: Path,
    visibility: str,
) -> None:
    paths, g, project, commits = _replacement_history(tmp_path)
    project = replace(project, visibility=visibility)
    store = _candidate.store_path(paths, project.name)
    for index, commit in enumerate(commits):
        _git(store, 'update-ref', f'refs/tags/v{index}.0.0', commit)
    before = _candidate.published_refs(paths, project.name)
    record = _candidate.build_candidate(
        paths,
        g,
        project,
        _candidate.CandidateInputs(
            _snapshot(),
            _pinned(),
            '1.0.0',
            None,
            replaces=commits[-1],
            replace_scope='all',
        ),
    )
    assert record.poststate_tags == {'v1.0.0': record.commit}
    assert record.replayed == ()
    assert _candidate.published_refs(paths, project.name) == before
    _candidate.verify_publication_objects(paths, project.name, record)
    report = _candidate.inspect_report(paths, record, g, project)
    assert re.search(r'Scope:\s+all published history \(3 snapshots\) -> one snapshot', report)
    assert re.search(r'Removed tags:\s+v0.0.0, v2.0.0', report)
    assert 'v1.0.0:' in report
    assert 'GitHub not rechecked' in report
    with color_context('always'):
        colored = _candidate.inspect_report(paths, record, g, project)
    assert '\x1b[33mv0.0.0, v2.0.0\x1b[0m' in colored


def test_replacement_audits_untouched_earlier_commit_headers(tmp_path: Path) -> None:
    paths, g, project, commits = _replacement_history(tmp_path)
    store = _candidate.store_path(paths, project.name)
    raw = _git(store, 'cat-file', 'commit', commits[0])
    raw = raw.replace(b'\nauthor ', b'\ngpgsig invalid-signature\nauthor ')
    signed = run_git(
        ['hash-object', '--literally', '-t', 'commit', '-w', '--stdin'], cwd=store, input_bytes=raw
    ).stdout.strip()
    raw = _git(store, 'cat-file', 'commit', commits[1])
    raw = raw.replace(b'parent ' + commits[0].encode(), b'parent ' + signed)
    selected = (
        run_git(
            ['hash-object', '--literally', '-t', 'commit', '-w', '--stdin'],
            cwd=store,
            input_bytes=raw,
        )
        .stdout.strip()
        .decode()
    )
    _git(store, 'update-ref', 'refs/heads/main', selected)
    record = _candidate.build_candidate(
        paths,
        g,
        project,
        _candidate.CandidateInputs(
            _snapshot(),
            _pinned(),
            None,
            None,
            replaces=selected,
            replace_scope='one',
        ),
    )
    with pytest.raises(PubanonError, match='history has identity problems'):
        _candidate.verify_retained_candidate(paths, g, project, record, env={})


@pytest.mark.slow
def test_replacement_checks_reserved_paths_in_every_replayed_tree(tmp_path: Path) -> None:
    relative = 'pubanon/projects/example/publications/private.txt'
    paths, g, project, record = _complete_replacement(
        tmp_path,
        changed=(Entry(relative, 0o100644, 'blob', b'public bytes'),),
    )
    # Put the existing data directory under the configured source root without
    # changing any frozen objects or introducing private bytes into correction.
    project = replace(project, source_path=paths.data_dir.parent)
    actual = paths.data_dir.name + '/projects/example/publications/private.txt'
    assert actual == relative
    record = replace(record, fingerprint=fingerprint(g, project))
    _candidate.save_candidate(paths, project.name, record)
    with pytest.raises(PubanonError, match='publication state'):
        _candidate.verify_retained_candidate(paths, g, project, record, env={})


@pytest.mark.slow
def test_replacement_gates_complete_changed_path_names(tmp_path: Path) -> None:
    paths, g, project, record = _complete_replacement(
        tmp_path,
        changed=(Entry('old-name.txt', 0o100644, 'blob', b'public bytes'),),
    )
    g = replace(g, sensitive_terms=('old-name',))
    record = replace(record, fingerprint=fingerprint(g, project))
    _candidate.save_candidate(paths, project.name, record)
    with pytest.raises(
        PubanonError, match='the replayed snapshot did not pass current content checks'
    ) as error:
        _candidate.verify_retained_candidate(paths, g, project, record, env={})
    assert 'old-name.txt' in str(error.value.fact)


@pytest.mark.slow
@pytest.mark.parametrize('resolved', [False, True])
@pytest.mark.parametrize('filename', ['new.txt', 'file.txt'])
def test_replacement_gates_full_changed_blob_even_when_term_occurred_elsewhere(
    tmp_path: Path,
    resolved: bool,
    filename: str,
) -> None:
    # On file.txt, the private first line is unchanged from the first old
    # release. A patch-lines-only scan would miss it in this changed blob.
    changed = (Entry(filename, 0o100644, 'blob', b'old 1\nnew public line'),)
    paths, g, project, record = _complete_replacement(tmp_path, changed=changed)
    g = replace(g, sensitive_terms=('old',))
    if resolved:
        entries = tuple(
            replace(entry, resolutions=(_records.Resolution(paths=(filename,), action='keep'),))
            for entry in record.replayed
        )
        record = replace(record, replayed=entries)
    record = replace(record, fingerprint=fingerprint(g, project))
    _candidate.save_candidate(paths, project.name, record)
    with pytest.raises(
        PubanonError, match='the replayed snapshot did not pass current content checks'
    ) as error:
        _candidate.verify_retained_candidate(paths, g, project, record, env={})
    assert filename in str(error.value.fact)
    assert record.replayed[0].old[:12] in str(error.value.fact)


@pytest.mark.slow
def test_replacement_inspection_defaults_to_tip(
    tmp_path: Path,
) -> None:
    paths, _g, project, record = _complete_replacement(tmp_path)
    tip = _candidate.materialize_inspect(paths, project.name, record)
    assert tip.name == record.tip
    assert (tip / 'file.txt').read_bytes() == b'old 2'


@pytest.mark.slow
def test_replacement_inspection_keeps_complete_per_release_changes_privately(
    tmp_path: Path,
) -> None:
    changed = (
        *_snapshot(),
        *(Entry(f'changed/{index:02d}.txt', 0o100644, 'blob', b'public\n') for index in range(25)),
    )
    paths, g, project, record = _complete_replacement(tmp_path, changed=changed)

    preview = _candidate.inspect_report(paths, record, g, project)
    complete_path = (
        paths.data_dir / 'projects' / project.name / f'candidate-files-{record.commit}.txt'
    )
    complete = complete_path.read_text()
    replay = record.replayed[0]
    assert 'changed/24.txt' not in preview
    assert f'{replay.old[:12]} -> {replay.new[:12]}' in preview
    assert escape(str(complete_path)) in preview
    assert f'{replay.old} -> {replay.new}' in complete
    assert all(f'changed/{index:02d}.txt' in complete for index in range(25))
    assert '    A  changed/00.txt' in preview
    assert '    A  changed/24.txt' in complete
    assert '1  0  changed/24.txt' in complete


@pytest.mark.slow
def test_replacement_inspection_rejects_untouched_earlier_tag(tmp_path: Path) -> None:
    paths, _g, project, record = _complete_replacement(tmp_path, selected=1, tagged=True)
    selected = _candidate.inspect_rewritten(paths, project.name, record, 'v1.0.0')
    replayed = _candidate.inspect_rewritten(paths, project.name, record, 'v2.0.0')
    assert selected.name == record.commit
    assert replayed.name == record.tip
    copies = _candidate.inspection_paths(paths, project.name)
    refs = _candidate.published_refs(paths, project.name)
    with pytest.raises(PubanonError) as error:
        _candidate.inspect_rewritten(paths, project.name, record, 'v0.0.0')
    assert error.value.fact == 'v0.0.0'
    assert _candidate.inspection_paths(paths, project.name) == copies
    assert _candidate.published_refs(paths, project.name) == refs
    assert _records.load_candidate(paths, project.name) == record


@pytest.mark.slow
def test_replacement_inspection_reports_each_original_to_new_change(tmp_path: Path) -> None:
    paths, g, project, record = _complete_replacement(tmp_path)
    report = _candidate.inspect_report(paths, record, g, project)
    assert record.replaces is not None
    expected = [(record.replaces[:12], record.commit[:12])]
    expected.extend((entry.old[:12], entry.new[:12]) for entry in record.replayed)
    assert (
        re.findall(r'^\s*([0-9a-f]{12}) -> ([0-9a-f]{12})\s*$', report, re.MULTILINE) == expected
    )
    assert 'README.md' in report
    assert 'file.txt' in report


def test_replacement_inspection_reports_manual_resolution_paths(tmp_path: Path) -> None:
    paths, _g, project, record = _complete_replacement(tmp_path)
    replay = replace(
        record.replayed[0],
        resolutions=(_records.Resolution(paths=('resolved.txt',), action='keep'),),
    )
    report = _candidate._rewritten_change_report(
        paths, project.name, replace(record, replayed=(replay, *record.replayed[1:]))
    )
    assert 'Manual resolution: resolved.txt' in report
    complete = _candidate._rewritten_change_report(
        paths,
        project.name,
        replace(record, replayed=(replay, *record.replayed[1:])),
        complete=True,
    )
    assert 'Manual resolution: resolved.txt' in complete


@pytest.mark.slow
def test_replacement_inspection_selects_only_rewritten_commits(tmp_path: Path) -> None:
    paths, _g, project, record = _complete_replacement(tmp_path)
    assert record.replaces is not None
    correction = _candidate.inspect_rewritten(paths, project.name, record, record.commit[:12])
    replay = _candidate.inspect_rewritten(paths, project.name, record, record.replayed[0].new)
    assert _candidate.select_rewritten_commit(record, record.commit.upper()) == record.commit
    assert correction.name == record.commit
    assert replay.name == record.replayed[0].new
    assert (correction / 'README.md').read_bytes() == b'public\n'
    assert (replay / 'file.txt').read_bytes() == b'old 1'
    _filesystem.chmod(correction, 0o700)
    _filesystem.chmod(replay, 0o700)
    (correction / 'notes').write_text('discard on refresh')
    (replay / 'notes').write_text('preserve other copy')
    with pytest.raises(PubanonError) as error:
        _candidate.inspect_rewritten(paths, project.name, record, record.replayed[0].new)
    assert error.value.recovery == (
        f'pubanon inspect --rewritten {record.replayed[0].new} --refresh'
    )
    assert (
        _candidate.inspect_rewritten(paths, project.name, record, record.commit, refresh=True)
        == correction
    )
    assert not (correction / 'notes').exists()
    assert (replay / 'notes').read_text() == 'preserve other copy'
    with pytest.raises(PubanonError, match='rewritten'):
        _candidate.inspect_rewritten(paths, project.name, record, record.replaces[:12])


@pytest.mark.slow
def test_replacement_publication_proof_needs_neither_ref_nor_current_configuration(
    tmp_path: Path,
) -> None:
    paths, _g, project, record = _complete_replacement(tmp_path)
    store = _candidate.store_path(paths, project.name)
    _git(store, 'update-ref', '-d', _candidate.CANDIDATE_REF)
    _candidate.record_path(paths, project.name).unlink()
    _candidate.verify_publication_objects(paths, project.name, record)


@pytest.mark.slow
def test_replacement_rejects_missing_untagged_descendant(tmp_path: Path) -> None:
    paths, g, project, record = _complete_replacement(tmp_path)
    store = _candidate.store_path(paths, project.name)
    last = record.replayed[-1]
    raw = _git(store, 'cat-file', 'commit', last.new)
    raw = raw.replace(
        f'parent {record.replayed[0].new}'.encode(), f'parent {record.commit}'.encode()
    )
    new = (
        run_git(
            ['hash-object', '-t', 'commit', '-w', '--stdin'],
            cwd=store,
            input_bytes=raw,
        )
        .stdout.strip()
        .decode()
    )
    # The remaining old/new pair has the expected message/date and a valid
    # correction -> tip chain. Only complete old-sequence proof catches the gap.
    record = replace(record, tip=new, replayed=(replace(last, new=new),))
    _candidate.save_candidate(paths, project.name, record)
    _git(store, 'update-ref', _candidate.CANDIDATE_REF, new)
    with pytest.raises(PubanonError, match='complete descendant chain'):
        _candidate.verify_retained_candidate(paths, g, project, record, env={})


@pytest.mark.slow
@pytest.mark.parametrize('anchor', ['correction', 'old-tip', 'intermediate'])
def test_replacement_rejects_stale_or_substituted_candidate_anchor(
    tmp_path: Path,
    anchor: str,
) -> None:
    paths, g, project, record = _complete_replacement(tmp_path)
    store = _candidate.store_path(paths, project.name)
    anchors = {
        'correction': record.commit,
        'old-tip': record.replayed[-1].old,
        'intermediate': record.replayed[0].new,
    }
    _git(store, 'update-ref', _candidate.CANDIDATE_REF, anchors[anchor])
    with pytest.raises(PubanonError, match='saved snapshot commit'):
        _candidate.verify_retained_candidate(paths, g, project, record, env={})
    with pytest.raises(PubanonError, match='saved snapshot commit'):
        _candidate.materialize_inspect(paths, project.name, record)


@pytest.mark.parametrize('mutation', ['parent', 'message', 'author-date', 'identity', 'signature'])
def test_replacement_rejects_substituted_replay_metadata(tmp_path: Path, mutation: str) -> None:
    paths, g, project, record = _complete_replacement(tmp_path)
    store = _candidate.store_path(paths, project.name)
    last = record.replayed[-1]
    raw = _git(store, 'cat-file', 'commit', last.new)
    if mutation == 'parent':
        raw = raw.replace(
            f'parent {record.replayed[0].new}'.encode(), f'parent {record.commit}'.encode()
        )
    elif mutation == 'message':
        raw = raw.replace(b'\n\nrelease 2\n', b'\n\nsubstituted message\n')
    elif mutation == 'author-date':
        raw = re.sub(rb'(author [^\n]+>) [0-9]+', rb'\1 1234567890', raw)
    elif mutation == 'identity':
        raw = raw.replace(b'Anonymous Publisher', b'Other Publisher')
    else:
        raw = raw.replace(b'\nauthor ', b'\ngpgsig forged\nauthor ')
    result = run_git(
        ['hash-object', '--literally', '-t', 'commit', '-w', '--stdin'], cwd=store, input_bytes=raw
    )
    new = result.stdout.strip().decode()
    assert new != last.new
    record = replace(record, tip=new, replayed=(*record.replayed[:-1], replace(last, new=new)))
    _candidate.save_candidate(paths, project.name, record)
    _git(store, 'update-ref', _candidate.CANDIDATE_REF, new)
    with pytest.raises(
        PubanonError, match=r'metadata|original author date or message|anonymous identity'
    ):
        _candidate.verify_retained_candidate(paths, g, project, record, env={})
    with pytest.raises(PubanonError):
        _candidate.verify_publication_objects(paths, project.name, record)


def test_replacement_report_identifies_final_tip_and_correction_input(tmp_path: Path) -> None:
    paths, g, project, record = _complete_replacement(tmp_path)
    report = _candidate.inspect_report(paths, record, g, project)
    assert record.tip is not None
    assert re.search(rf'Final snapshot ID:\s+{record.tip[:12]}', report)
    assert re.search(rf'Correction snapshot ID:\s+{record.commit[:12]}', report)
    assert 'Correction source commit:' not in report
    assert re.search(r'Final snapshot changes:\s+since recorded publication base', report)


def test_replacement_snapshot_report_shows_tag_movements(tmp_path: Path) -> None:
    paths, global_config, project, record = _complete_replacement(tmp_path, tagged=True)

    report = _candidate.inspect_report(paths, record, global_config, project)

    assert '(prepared)' in report
    assert 'Operation:' not in report
    assert 'one published snapshot and its replayed descendants' in report
    assert 'Tags moved\n' in report
    assert 'Replaced snapshots\n' in report


@pytest.mark.slow
def test_replacement_verification_returns_tip_and_allows_unchanged_historical_exposure(
    tmp_path: Path,
) -> None:
    paths, g, project, record = _complete_replacement(tmp_path)
    g = replace(g, sensitive_terms=('old',))
    record = replace(record, fingerprint=fingerprint(g, project))
    _candidate.save_candidate(paths, project.name, record)
    with patch.object(_candidate, 'read_snapshot', wraps=_candidate.read_snapshot) as reads:
        snapshot = _candidate.verify_retained_candidate(paths, g, project, record, env={})
    assert snapshot == (Entry('file.txt', 0o100644, 'blob', b'old 2'),)
    assert record.snapshot_digest == snapshot_digest(_snapshot())
    read_commits = [call.args[1] for call in reads.call_args_list]
    assert read_commits.count(record.commit) == 1
    for entry in record.replayed:
        assert read_commits.count(entry.old) == 1
        assert read_commits.count(entry.new) == 1
    assert len(read_commits) == 1 + 2 * len(record.replayed)


@pytest.mark.parametrize('side', ['old', 'new'])
@pytest.mark.parametrize('object_kind', ['missing', 'tree'])
def test_replay_metadata_requires_available_commit_objects(
    tmp_path: Path, side: str, object_kind: str
) -> None:
    paths, g, project, record = _complete_replacement(tmp_path)
    entry = record.replayed[0]
    oid = 'f' * 40 if object_kind == 'missing' else record.tree
    invalid = replace(entry, old=oid) if side == 'old' else replace(entry, new=oid)
    record = replace(record, replayed=(invalid, *record.replayed[1:]))
    with pytest.raises(PubanonError, match=r'commit.*unavailable'):
        _candidate._verify_replayed_commits(
            _candidate.candidate_store_path(paths, project.name),
            record,
            f'{g.anon_name} <{g.anon_email}>'.encode(),
        )


@pytest.mark.slow
def test_replay_metadata_verification_uses_bounded_processes(tmp_path: Path) -> None:
    paths, g, project, record = _complete_replacement(tmp_path)
    store = _candidate.candidate_store_path(paths, project.name)
    identity = f'{g.anon_name} <{g.anon_email}>'.encode()
    counts: list[int] = []
    for size in (0, 1, len(record.replayed)):
        selected = replace(record, replayed=record.replayed[:size])
        with patch('subprocess.Popen', wraps=subprocess.Popen) as processes:
            tip = _candidate._verify_replayed_commits(store, selected, identity)
        assert tip == (selected.replayed[-1].new if size else record.commit)
        counts.append(processes.call_count)
    assert counts == [0, 1, 1]


@pytest.mark.parametrize('name', ['my project', "it's mine", r'a\b', '-leading'])
def test_reprepare_advice_selects_exact_project(tmp_path: Path, name: str) -> None:
    _paths, _project, record = _build(tmp_path)
    advice = _candidate.reprepare_advice(record, project=name)
    for command in advice.split(' && '):
        argv = shlex.split(command)
        assert _arguments.build_parser().parse_args(argv[1:]).project == name


def test_reprepare_advice_shows_unrenderable_project(tmp_path: Path) -> None:
    _paths, _project, record = _build(tmp_path)
    advice = _candidate.reprepare_advice(record, project='line\nbreak')
    assert 'line\\x0abreak' in advice
    assert 'select' in advice.lower()
    assert '&&' not in advice


def test_retained_verification_reads_commit_metadata_once(
    retained_verification: _RetainedVerification,
) -> None:
    case = retained_verification

    _candidate.verify_retained_candidate(
        case._paths, case._global_config, case._project, case._record, env=os.environ
    )

    store = _candidate.candidate_store_path(case._paths, case._project.name)
    metadata_read = ['--git-dir', str(store.absolute()), 'cat-file', 'commit', case._record.commit]
    assert [args for args in case._calls if args == metadata_read] == [metadata_read]


def test_rewritten_inspection_explains_invalid_utf8_retained_identity(tmp_path: Path) -> None:
    paths, _g, project, record = _complete_replacement(tmp_path, selected=2)
    store = _candidate.candidate_store_path(paths, project.name)
    raw = _git(store, 'cat-file', 'commit', record.commit)
    malformed = raw.replace(b'Anonymous Publisher', b'Anonymous \xffPublisher')
    assert malformed != raw
    commit = (
        _git_input(
            store,
            'hash-object',
            '--literally',
            '-t',
            'commit',
            '-w',
            '--stdin',
            input_bytes=malformed,
        )
        .strip()
        .decode('ascii')
    )
    record = replace(record, commit=commit, tip=commit)
    _candidate.save_candidate(paths, project.name, record)
    _git(store, 'update-ref', _candidate.CANDIDATE_REF, commit)

    with pytest.raises(PubanonError, match=r'retained replacement.*UTF-8') as error:
        _candidate.inspect_rewritten(paths, project.name, record, commit)

    assert error.value.recovery == 'pubanon discard, then pubanon prepare'
    assert _candidate.inspection_paths(paths, project.name) == ()
    assert _records.load_candidate(paths, project.name) == record
    assert b'\xff' in _git(store, 'cat-file', 'commit', commit)


@pytest.mark.slow
def test_rewritten_inspection_verifies_before_reusing_editable_copy(tmp_path: Path) -> None:
    paths, _g, project, record = _complete_replacement(tmp_path)
    inspection = _candidate.inspect_rewritten(paths, project.name, record, record.commit)
    _filesystem.chmod(inspection, 0o700)
    (inspection / 'notes').write_text('keep')
    _git(_candidate.store_path(paths, project.name), 'update-ref', '-d', _candidate.CANDIDATE_REF)
    with pytest.raises(PubanonError):
        _candidate.inspect_rewritten(paths, project.name, record, record.commit, refresh=True)
    assert (inspection / 'notes').read_text() == 'keep'


def test_second_candidate_has_sole_parent_and_diff_summary(tmp_path: Path) -> None:
    paths, project_config, first = _build(tmp_path)
    store = _candidate.store_path(paths, project_config.name)
    _git(store, 'update-ref', 'refs/heads/main', first.commit)
    _git(store, 'update-ref', 'refs/tags/v0.1.0', first.commit)
    _candidate.discard(paths, project_config.name, force=False)
    global_config, _ = _configs(tmp_path)
    changed = (Entry('README.md', 0o100644, 'blob', b'changed\n'),)

    second = _candidate.build_candidate(
        paths,
        global_config,
        project_config,
        _candidate.CandidateInputs(changed, _pinned(), None, None),
    )

    raw = _git(store, 'cat-file', 'commit', second.commit)
    assert raw.splitlines().count(f'parent {first.commit}'.encode()) == 1
    assert second.prestate_main == first.commit
    assert second.prestate_tags == {'v0.1.0': first.commit}
    _git(store, 'update-ref', 'refs/heads/main', second.commit)
    report = _candidate.inspect_report(paths, second, global_config, project_config)
    assert 'README.md' in report
    assert '|' in report


def test_snapshot_reports_hide_diagnostics_and_align_diffstat(tmp_path: Path) -> None:
    paths, project, record = _build(tmp_path)
    global_config, _ = _configs(tmp_path)
    report = _candidate.inspect_report(paths, record, global_config, project)
    verbose = _candidate.inspect_report(paths, record, global_config, project, verbose=True)
    assert 'GitHub not rechecked' in report
    assert 'Source commit:' not in report
    assert 'Snapshot commit:' not in report
    assert 'Operation:' not in report
    field_lines = [
        line
        for line in report.splitlines()
        if line.startswith(('Destination:', 'Identity:', 'Changes:', 'File list:', 'Checks:'))
    ]
    assert len(field_lines) == 5
    assert len({len(line) - len(line.split(':', 1)[1].lstrip()) for line in field_lines}) == 1
    assert record.pinned.commit in verbose
    assert record.commit in verbose
    assert 'Operation:' in verbose
    diffstat = [line for line in report.splitlines() if ' | ' in line]
    assert len(diffstat) == 2
    assert all(line.startswith('  ') and not line.startswith('   ') for line in diffstat)
    assert not any(line.lstrip().startswith('|') for line in diffstat)


def test_store_path_initializes_project_bare_repository(tmp_path: Path) -> None:
    paths = _paths(tmp_path)

    store = _candidate.store_path(paths, 'example')

    assert store == paths.data_dir / 'projects' / 'example' / 'store.git'
    result = subprocess.run(
        ['git', '-C', store, 'rev-parse', '--is-bare-repository'],
        check=True,
        capture_output=True,
        env=git_env(),
    )
    assert result.stdout == b'true\n'


@pytest.mark.parametrize('operation', ['ref', 'object'])
def test_store_write_refuses_damaged_store_inside_ancestor_repository(
    make_repo: Callable[..., Path], operation: str
) -> None:
    ancestor = make_repo()
    store = ancestor / 'state' / 'store.git'
    store.mkdir(parents=True)
    commit = _git(ancestor, 'rev-parse', 'HEAD').decode().strip()
    refs = _git(ancestor, 'for-each-ref')
    objects = _git(ancestor, 'cat-file', '--batch-all-objects', '--batch-check')
    args = (
        ['update-ref', _candidate.CANDIDATE_REF, commit]
        if operation == 'ref'
        else ['hash-object', '-w', '--stdin']
    )

    with pytest.raises(PubanonError, match='unusable snapshot store') as error:
        _candidate._git_result(
            args, store=store, failure='unusable snapshot store', input_bytes=b'new object\n'
        )

    assert 'not a git repository' in error.value.render()
    assert _git(ancestor, 'for-each-ref') == refs
    assert _git(ancestor, 'cat-file', '--batch-all-objects', '--batch-check') == objects


def test_tree_paths_returns_git_order_without_reading_blob_bodies(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    global_config, project = _configs(tmp_path)
    record = _candidate.build_candidate(
        paths,
        global_config,
        project,
        _candidate.CandidateInputs(
            (
                Entry('a', 0o100644, 'blob', b'a\n'),
                Entry('b/c', 0o100644, 'blob', b'c\n'),
            ),
            _pinned(),
            None,
            None,
        ),
    )

    store = _candidate.candidate_store_path(paths, project.name)
    blob = run_git(['rev-parse', f'{record.commit}:a'], cwd=store).stdout.decode().strip()
    # The tree remains readable, but any attempt to load this blob must fail.
    object_path = store / 'objects' / blob[:2] / blob[2:]
    _filesystem.chmod(object_path, 0o600)
    object_path.unlink()

    assert _candidate.tree_paths(paths, project.name, record.commit) == ['a', 'b/c']


@pytest.mark.slow
def test_unchanged_correction_warns_and_stays_distinct_with_same_second_timestamp(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(_candidate.time, 'time', lambda: 1700000000)
    paths, g, project, commits = _replacement_history(tmp_path)
    snapshot = (Entry('file.txt', 0o100644, 'blob', b'old 2'),)
    record = _candidate.build_candidate(
        paths,
        g,
        project,
        _candidate.CandidateInputs(
            snapshot,
            _pinned(),
            None,
            None,
            replaces=commits[-1],
            replace_scope='one',
        ),
    )
    assert record.commit != commits[-1]
    assert 'unchanged files and commit message' in capsys.readouterr().out
    assert _candidate.verify_retained_candidate(paths, g, project, record, env={}) == snapshot


def test_verify_retained_candidate_refuses_intact_utf8_term_in_mixed_blob(
    tmp_path: Path,
) -> None:
    term = 'Privat\u00e9Alias'
    snapshot = (Entry('mixed.bin', 0o100644, 'blob', b'\xffPrivat\xc3\xa9Alias\n'),)
    paths = _paths(tmp_path)
    global_config, project = _configs(tmp_path)
    record = _candidate.build_candidate(
        paths, global_config, project, _candidate.CandidateInputs(snapshot, _pinned(), None, None)
    )
    current_project = replace(project, extra_terms=(term,))
    current_record = replace(record, fingerprint=fingerprint(global_config, current_project))
    _candidate.save_candidate(paths, project.name, current_record)

    with pytest.raises(
        PubanonError, match='the snapshot did not pass current content checks'
    ) as exc_info:
        _candidate.verify_retained_candidate(
            paths, global_config, current_project, current_record, env=os.environ
        )

    assert exc_info.value.recovery == 'pubanon discard, then pubanon prepare'

    assert 'project extra_terms entry 1' in (exc_info.value.fact or '')
    assert f'blob bytes 1:{1 + len(term.encode())}' in (exc_info.value.fact or '')
    assert term in exc_info.value.render()
    assert term in (exc_info.value.fact or '')


def test_verify_retained_candidate_reloads_exact_record(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    global_config, project = _configs(tmp_path)
    record = _candidate.build_candidate(
        paths,
        global_config,
        project,
        _candidate.CandidateInputs(_snapshot(), _pinned(), None, None),
    )
    _candidate.save_candidate(paths, project.name, replace(record, message='changed record'))

    with pytest.raises(PubanonError, match='snapshot record changed') as exc_info:
        _candidate.verify_retained_candidate(paths, global_config, project, record, env=os.environ)

    assert exc_info.value.recovery == 'pubanon discard, then pubanon prepare'


def test_verify_retained_candidate_requires_current_commit_identity(
    retained_verification: _RetainedVerification,
) -> None:
    case = retained_verification
    current_config = replace(
        case._global_config,
        anon_name='Changed Publisher',
        anon_email='changed@example.invalid',
    )
    current_record = replace(
        case._record,
        fingerprint=fingerprint(current_config, case._project),
    )
    _candidate.save_candidate(case._paths, case._project.name, current_record)

    with pytest.raises(
        PubanonError,
        match='snapshot commit metadata does not match the record',
    ) as exc_info:
        _candidate.verify_retained_candidate(
            case._paths, current_config, case._project, current_record, env=os.environ
        )

    assert exc_info.value.recovery == 'pubanon discard, then pubanon prepare'


def test_verify_retained_candidate_returns_exact_snapshot(
    retained_verification: _RetainedVerification,
) -> None:
    case = retained_verification
    assert (
        _candidate.verify_retained_candidate(
            case._paths, case._global_config, case._project, case._record, env=os.environ
        )
        == case._snapshot
    )


@pytest.mark.parametrize(
    ('snapshot', 'version', 'message', 'term'),
    [
        (
            (Entry('classified/README.md', 0o100644, 'blob', b'public\n'),),
            None,
            None,
            'classified',
        ),
        ((Entry('README.md', 0o100644, 'blob', b'classified\n'),), None, None, 'classified'),
        ((_snapshot()[0],), None, 'classified release', 'classified'),
        ((_snapshot()[0],), None, None, 'anonymous@example.invalid'),
        ((_snapshot()[0],), '1.2.3', 'public release', 'v1.2.3'),
    ],
    ids=('path', 'data', 'message', 'identity', 'tag'),
)
def test_verify_retained_candidate_runs_every_current_gate(
    snapshot: tuple[Entry, ...],
    version: str | None,
    message: str | None,
    term: str,
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    global_config, project = _configs(tmp_path)
    record = _candidate.build_candidate(
        paths,
        global_config,
        project,
        _candidate.CandidateInputs(snapshot, _pinned(), version, message),
    )
    current_config = replace(global_config, sensitive_terms=(term,))
    current_record = replace(record, fingerprint=fingerprint(current_config, project))
    _candidate.save_candidate(paths, project.name, current_record)

    with pytest.raises(
        PubanonError, match='the snapshot did not pass current content checks'
    ) as exc_info:
        _candidate.verify_retained_candidate(
            paths, current_config, project, current_record, env=os.environ
        )

    assert exc_info.value.recovery == 'pubanon discard, then pubanon prepare'


def test_windows_cleanup_restores_file_access_without_changing_link_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / 'snapshot'
    nested = directory / 'nested'
    nested.mkdir(parents=True)
    artifact = nested / 'file'
    artifact.write_bytes(b'content')
    _filesystem.chmod(artifact, 0o400)
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'sentinel'
    sentinel.write_bytes(b'preserve')
    (nested / 'directory-link').symlink_to(outside, target_is_directory=True)
    (nested / 'file-link').symlink_to(sentinel)
    changes: list[tuple[Path, int, bool]] = []
    checked: list[Path] = []

    def _chmod(path: Path, mode: int, *, follow_symlinks: bool = True) -> None:
        changes.append((path, mode, follow_symlinks))

    monkeypatch.setattr(_filesystem, 'os', SimpleNamespace(name='nt', scandir=os.scandir))
    monkeypatch.setattr(_filesystem._windows_fs, 'check_directory', checked.append)
    monkeypatch.setattr(_filesystem, 'chmod', _chmod)
    _filesystem._restore_tree_access(directory)

    assert checked == [directory, nested]
    assert {path for path, _mode, _follow in changes} == {directory, nested, artifact}
    assert (artifact, 0o600, False) in changes
    assert all(not follow for _path, _mode, follow in changes)
    assert sentinel.read_bytes() == b'preserve'


@pytest.mark.skipif(os.name != 'nt', reason='requires native Windows directory junctions')
@pytest.mark.parametrize('junction_root', [False, True])
def test_windows_inspection_cleanup_never_traverses_junctions(
    tmp_path: Path, junction_root: bool
) -> None:
    inspection = tmp_path / 'inspection'
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'sentinel'
    sentinel.write_bytes(b'preserve')
    _filesystem.chmod(outside, 0o500)
    if junction_root:
        junction = inspection
    else:
        inspection.mkdir()
        junction = inspection / 'junction'
    subprocess.run(
        ['cmd.exe', '/c', 'mklink', '/J', str(junction), str(outside)],
        check=True,
        capture_output=True,
    )
    try:
        if junction_root:
            with pytest.raises(PubanonError, match='cannot clean review copy'):
                _candidate._clean_inspection_path(inspection)
            assert junction.is_junction()
        else:
            assert _candidate._clean_inspection_path(inspection)
            assert not inspection.exists()
        assert sentinel.read_bytes() == b'preserve'
        assert_private_mode(outside, 0o500)
    finally:
        _filesystem.chmod(outside, 0o700)


@pytest.mark.parametrize('saved_mode', [0o100644, 0o100755])
def test_windows_inspection_verification_preserves_saved_git_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, saved_mode: int
) -> None:
    (tmp_path / 'other-directory').mkdir()
    target = tmp_path / 'file'
    target.write_bytes(b'content')
    _filesystem.chmod(target, 0o600)
    with os.scandir(tmp_path) as entries:
        child = next(entry for entry in entries if entry.name == target.name)
    saved = Entry('file', saved_mode, 'blob', b'content')
    reads: list[Path] = []

    def _read_entry(path: Path) -> tuple[int, bytes]:
        reads.append(path)
        return stat.S_IFREG, path.read_bytes()

    monkeypatch.setattr(_candidate, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(_candidate._windows_fs, 'read_entry', _read_entry)
    _candidate._verify_inspection_entry(child, saved)
    target.write_bytes(b'changed')
    with pytest.raises(PubanonError, match='saved snapshot'):
        _candidate._verify_inspection_entry(child, saved)
    assert reads == [target, target]


@pytest.mark.parametrize(
    'entry',
    [
        Entry('C:escape', 0o100644, 'blob', b'content'),
        Entry(r'nested\escape', 0o100644, 'blob', b'content'),
        Entry('trailing.', 0o100644, 'blob', b'content'),
        Entry('CON', 0o100644, 'blob', b'content'),
        Entry('link', 0o120000, 'symlink', b'C:escape'),
        Entry('link', 0o120000, 'symlink', rb'..\escape'),
        Entry('link', 0o120000, 'symlink', b'target-\xff'),
    ],
)
def test_windows_materialization_refuses_native_path_aliases_before_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: Entry
) -> None:
    target = tmp_path / 'snapshot'

    def _windows_decode(raw: bytes) -> str:
        return raw.decode('utf-8', errors='surrogatepass')

    monkeypatch.setattr(_candidate, 'os', SimpleNamespace(name='nt', fsdecode=_windows_decode))
    with pytest.raises(PubanonError, match='cannot represent snapshot'):
        _candidate.materialize_directory(target, (entry,))
    assert not target.exists()


def test_write_tree_accepts_distinct_paths(tmp_path: Path) -> None:
    snapshot = (
        Entry('a.txt', 0o100644, 'blob', b'1'),
        Entry('b/a.txt', 0o100644, 'blob', b'2'),
        Entry('Docs/guide.txt', 0o100644, 'blob', b'3'),
        Entry('docs-archive/guide.txt', 0o100644, 'blob', b'4'),
    )
    store = _candidate.store_path(_paths(tmp_path), 'example')
    assert _write_tree(store, snapshot)


@pytest.mark.parametrize(
    ('paths', 'conflicting'),
    [
        (['A', 'a'], ('A', 'a')),
        (['A', 'a/x'], ('A', 'a')),
        (['A/x', 'a'], ('A', 'a')),
        (['Docs/a', 'docs/b'], ('Docs', 'docs')),
        (['root/Docs/a', 'root/docs/b'], ('root/Docs', 'root/docs')),
        (['caf\u00e9.txt', 'cafe\u0301.txt'], ('cafe\u0301.txt', 'caf\u00e9.txt')),
    ],
)
def test_write_tree_refuses_every_portability_collision_before_object_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    paths: list[str],
    conflicting: tuple[str, str],
) -> None:
    snapshot = tuple(Entry(path, 0o100644, 'blob', b'data') for path in paths)
    store = _candidate.store_path(_paths(tmp_path), 'example')

    def _forbidden_write(_store: Path, _snapshot: Snapshot) -> list[str]:
        pytest.fail('portability validation wrote candidate objects')

    monkeypatch.setattr(_candidate, '_write_blobs', _forbidden_write)

    with pytest.raises(PubanonError) as exc_info:
        _write_tree(store, snapshot)

    rendered = exc_info.value.render()
    assert conflicting[0] in rendered
    assert conflicting[1] in rendered


def test_write_tree_refuses_git_metadata_path_before_object_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _candidate.store_path(_paths(tmp_path), 'example')

    def _forbidden_write(_store: Path, _snapshot: Snapshot) -> list[str]:
        pytest.fail('snapshot validation wrote candidate objects')

    monkeypatch.setattr(_candidate, '_write_blobs', _forbidden_write)

    with pytest.raises(PubanonError, match='unsupported snapshot entry'):
        _write_tree(store, (Entry('.GIT/config', 0o100644, 'blob', b'data'),))


def test_write_tree_terminal_renders_both_portability_collision_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    term = 'private'
    snapshot = (
        Entry(f'{term}-ONE\x1b', 0o100644, 'blob', b'one'),
        Entry(f'{term}-one\x1b', 0o100644, 'blob', b'two'),
    )
    store = _candidate.store_path(_paths(tmp_path), 'example')

    def _forbidden_write(_store: Path, _snapshot: Snapshot) -> list[str]:
        pytest.fail('portability validation wrote candidate objects')

    monkeypatch.setattr(_candidate, '_write_blobs', _forbidden_write)

    with pytest.raises(PubanonError) as exc_info:
        _write_tree(store, snapshot)

    rendered = exc_info.value.render()
    assert term in rendered.casefold()
    assert '\x1b' not in rendered
    assert rendered.count(f'{term}') == 2
    assert rendered.count(r'\x1b') == 2
