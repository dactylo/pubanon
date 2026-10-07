import os
import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Literal, NotRequired, TypedDict, Unpack
from unittest.mock import patch

import pytest

from pubanon._config import CommitMessageSettings, GlobalConfig, Paths, ProjectConfig
from pubanon._errors import PubanonError
from pubanon._run import GitResult, run_git
from pubanon._snapshots import (
    _candidate,
    _discard,
    _records,
    _reports,
    _store,
    _verification,
)
from pubanon._snapshots._candidate import _write_tree
from pubanon._snapshots._source import (
    Entry,
    PinnedSource,
    Snapshot,
    read_snapshot,
)
from pubanon._terminal import color_context
from tests._git import git_env, oracle_tree
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


def _paths(tmp_path: Path) -> Paths:
    config_dir = tmp_path / 'config' / 'pubanon'
    data_dir = tmp_path / 'data' / 'pubanon'
    return Paths(config_dir, data_dir, config_dir / 'config.toml', data_dir / 'registry.json')


def _pinned() -> PinnedSource:
    return PinnedSource('a' * 40, 'ref', 'refs/heads/main', 'a' * 40)


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
        store = _store.store_path(paths, project.name)
        _git(store, 'update-ref', 'refs/heads/main', record.commit)
        _records.candidate_path(paths, project.name).unlink()
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


def test_build_candidate_gates_rendered_configured_message(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    g, project = _configs(tmp_path)
    g = replace(g, commit_message=CommitMessageSettings(tagged='Private Person release {tag}'))

    with pytest.raises(PubanonError, match='snapshot commit message did not pass content checks'):
        _candidate.build_candidate(
            paths,
            g,
            project,
            _candidate.CandidateInputs(_snapshot(), _pinned(), '1.2.3', None),
        )


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


def test_build_candidate_preserves_explicit_message_with_configured_template(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    g, project = _configs(tmp_path)
    g = replace(g, commit_message=CommitMessageSettings(tagged='Release {tag}'))
    message = 'Keep {tag}\n\n'

    record = _candidate.build_candidate(
        paths,
        g,
        project,
        _candidate.CandidateInputs(_snapshot(), _pinned(), '1.2.3', message),
    )

    raw = _git(_store.store_path(paths, project.name), 'cat-file', 'commit', record.commit)
    assert record.message == message
    assert raw.partition(b'\n\n')[2] == message.encode() + b'\n'


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
    store = _store.store_path(paths, project.name)
    _records.candidate_path(paths, project.name).unlink()
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


@pytest.mark.parametrize(
    ('global_messages', 'project_messages', 'version', 'expected'),
    [
        (None, None, '1.2.3', 'release: v1.2.3'),
        (None, None, None, 'publication update'),
        (
            CommitMessageSettings(tagged='Release {tag} ({version})'),
            None,
            '1.2.3',
            'Release v1.2.3 (1.2.3)',
        ),
        (
            CommitMessageSettings(untagged='Fresh publication'),
            None,
            None,
            'Fresh publication',
        ),
        (
            CommitMessageSettings(tagged='Global {tag}', untagged='Global update'),
            CommitMessageSettings(tagged='Project {version}'),
            '1.2.3',
            'Project 1.2.3',
        ),
        (
            CommitMessageSettings(tagged='Global {tag}', untagged='Global update'),
            CommitMessageSettings(tagged='Project {version}'),
            None,
            'Global update',
        ),
    ],
)
def test_build_candidate_resolves_message_template_by_key(
    tmp_path: Path,
    global_messages: CommitMessageSettings | None,
    project_messages: CommitMessageSettings | None,
    version: str | None,
    expected: str,
) -> None:
    paths = _paths(tmp_path)
    g, project = _configs(tmp_path)
    g = replace(g, commit_message=global_messages)
    project = replace(project, commit_message=project_messages)

    record = _candidate.build_candidate(
        paths,
        g,
        project,
        _candidate.CandidateInputs(_snapshot(), _pinned(), version, None),
    )

    raw = _git(_store.store_path(paths, project.name), 'cat-file', 'commit', record.commit)
    assert record.message == expected
    assert raw.partition(b'\n\n')[2] == expected.encode() + b'\n'


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


@pytest.mark.parametrize(
    ('template', 'expected'),
    [
        ('Release {tag}\n\nNotes {{x}}\r\n\n', 'Release v1.2.3\n\nNotes {x}'),
        ('Release {tag}  \n', 'Release v1.2.3  '),
    ],
)
def test_build_candidate_trims_only_configured_final_line_breaks(
    tmp_path: Path, template: str, expected: str
) -> None:
    paths = _paths(tmp_path)
    g, project = _configs(tmp_path)
    g = replace(g, commit_message=CommitMessageSettings(tagged=template))

    record = _candidate.build_candidate(
        paths,
        g,
        project,
        _candidate.CandidateInputs(_snapshot(), _pinned(), '1.2.3', None),
    )

    raw = _git(_store.store_path(paths, project.name), 'cat-file', 'commit', record.commit)
    assert record.message == expected
    assert raw.partition(b'\n\n')[2] == expected.encode() + b'\n'


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
    raw = _git(_store.store_path(paths, project_config.name), 'cat-file', 'commit', record.commit)
    assert raw.partition(b'\n\n')[2] == b'public release message\n'

    _discard.discard(paths, project_config.name, force=False)
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

    raw = _git(_store.store_path(paths, project_config.name), 'cat-file', 'commit', record.commit)
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
    oracle_store = _store.store_path(_paths(tmp_path / 'oracle'), 'example')
    monkeypatch.setattr(_candidate.time, 'time', lambda: epoch)

    expected_tree = oracle_tree(oracle_store, snapshot)
    oracle_commit = _oracle_commit(
        oracle_store,
        expected_tree,
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
    candidate_store = _store.store_path(paths, project_config.name)

    assert record.tree == expected_tree
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


def test_candidate_subprocess_failure_reports_escaped_stderr(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    global_config, project_config = _configs(tmp_path)
    _store.store_path(paths, project_config.name)
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
    oracle_store = _store.store_path(_paths(tmp_path / 'oracle'), 'example')
    candidate_store = _store.store_path(_paths(tmp_path / 'candidate'), 'example')

    expected_tree = oracle_tree(oracle_store, snapshot)
    candidate_tree = _write_tree(candidate_store, snapshot)

    assert candidate_tree == expected_tree
    candidate_snapshot = read_snapshot(candidate_store, candidate_tree)
    oracle_snapshot = read_snapshot(oracle_store, expected_tree)
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
        store = _store.store_path(paths, 'candidate')
        oracle_store = _store.store_path(paths, 'oracle')
        expected_tree = oracle_tree(oracle_store, snapshot)

        # Count actual child launches, excluding setup and independent readback.
        with patch('subprocess.Popen', wraps=subprocess.Popen) as processes:
            tree = _write_tree(store, snapshot)
        counts.append(processes.call_count)

        assert tree == expected_tree
        assert _git(store, 'ls-tree', '-rz', tree) == _git(
            oracle_store, 'ls-tree', '-rz', expected_tree
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
            _store.store_path(_paths(tmp_path), 'example'),
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
            _store.store_path(_paths(tmp_path), 'example'),
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
    store = _store.store_path(_paths(tmp_path), 'example')
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
    store = _store.store_path(paths, project.name)
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
    store = _store.store_path(paths, project.name)
    parents = _git(store, 'show', '-s', '--format=%P', record.commit).strip().decode()
    assert parents == (commits[selected - 1] if selected and scope == 'one' else '')
    assert _candidate.published_refs(paths, project.name) == before
    assert record.tip == (record.commit if selected == 2 else None)
    if selected == 2:
        assert (
            _verification.verify_retained_candidate(paths, g, project, record, env={})
            == _snapshot()
        )
    else:
        with pytest.raises(PubanonError, match='incomplete'):
            _verification.verify_retained_candidate(paths, g, project, record, env={})


@pytest.mark.parametrize('explicit', [False, True], ids=['inherited', 'override'])
def test_correction_preserves_exact_message_with_configured_template(
    tmp_path: Path, explicit: bool
) -> None:
    paths = _paths(tmp_path)
    g, project = _configs(tmp_path)
    message = 'Keep {tag}\n\n'
    original = _candidate.build_candidate(
        paths,
        g,
        project,
        _candidate.CandidateInputs(_snapshot(), _pinned(), '1.2.3', message),
    )
    store = _store.store_path(paths, project.name)
    _git(store, 'update-ref', 'refs/heads/main', original.commit)
    _records.candidate_path(paths, project.name).unlink()
    g = replace(g, commit_message=CommitMessageSettings(tagged='Release {tag}'))

    record = _candidate.build_candidate(
        paths,
        g,
        project,
        _candidate.CandidateInputs(
            _snapshot(),
            _pinned(),
            '1.2.3',
            message if explicit else None,
            replaces=original.commit,
            replace_scope='one',
        ),
    )

    raw = _git(store, 'cat-file', 'commit', record.commit)
    assert record.message == message
    assert raw.partition(b'\n\n')[2] == message.encode() + b'\n'


@pytest.mark.parametrize('tags', [(), ('v1.2.3',), ('v1.2.3', 'v2.0.0')])
def test_correction_moves_all_selected_tags_without_saved_provenance(
    tmp_path: Path, tags: tuple[str, ...]
) -> None:
    paths, g, project, commits = _replacement_history(tmp_path)
    store = _store.store_path(paths, project.name)
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
    _verification.verify_publication_objects(paths, project.name, record)


@pytest.mark.parametrize('version', ['1.2.3', '9.0.0'])
def test_correction_version_must_match_one_of_several_moved_tags(
    tmp_path: Path,
    version: str,
) -> None:
    paths, g, project, commits = _replacement_history(tmp_path)
    store = _store.store_path(paths, project.name)
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


def test_initial_record_sync_failure_removes_commit_point_and_reruns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    global_config, project_config = _configs(tmp_path)
    record_path = paths.data_dir / 'projects' / project_config.name / 'candidate.toml'
    original_fsync = os.fsync
    failed = False

    def _fail_first_record_sync(descriptor: int) -> None:
        nonlocal failed
        if not failed:
            failed = True
            temporaries = list(record_path.parent.glob('.pubanon-write-*.tmp'))
            assert len(temporaries) == 1
            assert_private_mode(temporaries[0], 0o600)
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
    assert not list(record_path.parent.glob('.pubanon-write-*.tmp'))

    record = _candidate.build_candidate(
        paths,
        global_config,
        project_config,
        _candidate.CandidateInputs(_snapshot(), _pinned(), None, None),
    )
    assert _records.load_candidate(paths, project_config.name) == record


def test_published_refs_refuses_malformed_tag_line(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    _store.store_path(paths, 'example')
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
    store = _store.store_path(paths, 'example')

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


@pytest.mark.parametrize('nested', [('extra',), ('a', 'b')])
def test_read_ref_ignores_refs_below_the_requested_name(
    tmp_path: Path, nested: tuple[str, ...]
) -> None:
    paths, project, record = _build(tmp_path)
    store = _store.candidate_store_path(paths, project.name)
    ref = _candidate.REWRITE_MERGED_REF
    for name in nested:
        _git(store, 'update-ref', f'{ref}/{name}', record.commit)
    assert _candidate.read_ref(store, ref) is None
    for name in nested:
        _git(store, 'update-ref', '-d', f'{ref}/{name}')
    _git(store, 'update-ref', ref, record.commit)
    assert _candidate.read_ref(store, ref) == record.commit


@pytest.mark.parametrize('case', ['missing-oid', 'invalid-oid', 'duplicate', 'malformed-nested'])
def test_read_ref_refuses_malformed_output(tmp_path: Path, case: str) -> None:
    paths = _paths(tmp_path)
    store = _store.candidate_store_path(paths, 'example')
    ref = _candidate.REWRITE_MERGED_REF
    valid = ref.encode() + b' ' + b'a' * 40 + b'\n'
    outputs = {
        'missing-oid': ref.encode() + b'\n',
        'invalid-oid': ref.encode() + b' invalid\n',
        'duplicate': valid + valid,
        'malformed-nested': valid + ref.encode() + b'/extra invalid\n',
    }
    with (
        patch.object(_candidate, 'run_git', return_value=GitResult(0, outputs[case], b'')),
        pytest.raises(PubanonError, match='Git returned an invalid snapshot store ref'),
    ):
        _candidate.read_ref(store, ref)


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

    store = _store.store_path(paths, project_config.name)
    assert _records.load_candidate(paths, project_config.name) is None
    assert _git(store, 'rev-parse', _candidate.CANDIDATE_REF).strip()

    record = _candidate.build_candidate(
        paths,
        global_config,
        project_config,
        _candidate.CandidateInputs(_snapshot(), _pinned(), None, None),
    )
    assert _records.load_candidate(paths, project_config.name) == record


def test_regular_file_at_checks_exact_published_tree_entry(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = _store.candidate_store_path(paths, 'example')
    store.mkdir(parents=True)
    _git(store, 'init', '--bare', '-q')
    blob = _git_input(store, 'hash-object', '-w', '--stdin', input_bytes=b'content').strip()
    child_tree = _git_input(
        store, 'mktree', input_bytes=b'100644 blob ' + blob + b'\tchild\n'
    ).strip()
    submodule = _git_input(
        store, 'commit-tree', child_tree.decode(), input_bytes=b'Submodule\n'
    ).strip()
    tree = (
        _git_input(
            store,
            'mktree',
            input_bytes=(
                b'100644 blob ' + blob + b'\tplain\n'
                b'100755 blob ' + blob + b'\texecutable\n'
                b'120000 blob ' + blob + b'\tlink\n'
                b'040000 tree ' + child_tree + b'\tdirectory\n'
                b'160000 commit ' + submodule + b'\tsubmodule\n'
            ),
        )
        .decode()
        .strip()
    )
    commit = _git_input(store, 'commit-tree', tree, input_bytes=b'Published\n').decode().strip()

    assert _candidate.regular_file_at(paths, 'example', commit, 'plain')
    assert _candidate.regular_file_at(paths, 'example', commit, 'executable')
    assert _candidate.regular_file_at(paths, 'example', commit, 'directory/child')
    for path in ('missing', 'link', 'submodule', 'directory', 'link/child'):
        assert not _candidate.regular_file_at(paths, 'example', commit, path)


def test_regular_file_at_rejects_malformed_store_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _malformed(*_args: object, **_kwargs: object) -> bytes:
        return b'broken\0'

    monkeypatch.setattr(_candidate, 'git_result', _malformed)
    with pytest.raises(PubanonError, match='cannot read published file'):
        _candidate.regular_file_at(_paths(tmp_path), 'example', 'a' * 40, 'plain')


@pytest.mark.parametrize('visibility', ['public', 'private'])
def test_replace_all_retires_old_tags_and_creates_only_intended_tag(
    tmp_path: Path,
    visibility: str,
) -> None:
    paths, g, project, commits = _replacement_history(tmp_path)
    project = replace(project, visibility=visibility)
    store = _store.store_path(paths, project.name)
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
    _verification.verify_publication_objects(paths, project.name, record)
    report = _reports.inspect_report(
        paths, _verification.prove_objects(paths, project.name, record), g, project
    )
    assert re.search(r'Scope:\s+all published history \(3 snapshots\) -> one snapshot', report)
    assert re.search(r'Removed tags:\s+v0.0.0, v2.0.0', report)
    assert 'v1.0.0:' in report
    assert 'GitHub not rechecked' in report
    with color_context('always'):
        colored = _reports.inspect_report(
            paths, _verification.prove_objects(paths, project.name, record), g, project
        )
    assert '\x1b[33mv0.0.0, v2.0.0\x1b[0m' in colored


@pytest.mark.slow
def test_replacement_descendants_batches_selected_walk(tmp_path: Path) -> None:
    store = _store.store_path(_paths(tmp_path), 'example')
    tree = oracle_tree(store, ())
    commits: list[str] = []
    for index in range(4):
        parents = ('-p', commits[-1]) if commits else ()
        commit = (
            _git_input(
                store, 'commit-tree', tree, *parents, input_bytes=f'revision {index}\n'.encode()
            )
            .strip()
            .decode('ascii')
        )
        commits.append(commit)

    counts: list[int] = []
    for later in (0, 1, 3):
        with patch('subprocess.Popen', wraps=subprocess.Popen) as processes:
            descendants = _candidate.replacement_descendants(store, commits[0], commits[later])
        assert descendants == tuple(commits[1 : later + 1])
        counts.append(processes.call_count)
    assert counts == [1, 1, 1]


def test_replacement_descendants_checks_only_selected_ancestry(tmp_path: Path) -> None:
    store = _store.store_path(_paths(tmp_path), 'example')
    tree = oracle_tree(store, ())

    def _commit(message: str, *parents: str) -> str:
        parent_args = tuple(arg for parent in parents for arg in ('-p', parent))
        return (
            _git_input(store, 'commit-tree', tree, *parent_args, input_bytes=message.encode())
            .strip()
            .decode('ascii')
        )

    root = _commit('root')
    branch = _commit('branch', root)
    merge = _commit('merge', root, branch)
    selected = _commit('selected', merge)
    tip = _commit('tip', selected)
    unrelated = _commit('unrelated')

    assert _candidate.replacement_descendants(store, selected, tip) == (tip,)
    for chosen in (merge, root):
        with pytest.raises(PubanonError, match='replacement history is not linear'):
            _candidate.replacement_descendants(store, chosen, tip)
    with pytest.raises(PubanonError, match='outside published main history'):
        _candidate.replacement_descendants(store, unrelated, branch)
    with pytest.raises(PubanonError, match='nothing to replace'):
        _candidate.replacement_descendants(store, root, None)


def test_second_candidate_has_sole_parent_and_diff_summary(tmp_path: Path) -> None:
    paths, project_config, first = _build(tmp_path)
    store = _store.store_path(paths, project_config.name)
    _git(store, 'update-ref', 'refs/heads/main', first.commit)
    _git(store, 'update-ref', 'refs/tags/v0.1.0', first.commit)
    _discard.discard(paths, project_config.name, force=False)
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
    report = _reports.inspect_report(
        paths,
        _verification.prove_objects(paths, project_config.name, second),
        global_config,
        project_config,
    )
    assert 'README.md' in report
    assert '|' in report


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
        _candidate.git_result(
            args, store=store, failure='unusable snapshot store', input_bytes=b'new object\n'
        )

    assert 'not a git repository' in error.value.render()
    assert _git(ancestor, 'for-each-ref') == refs
    assert _git(ancestor, 'cat-file', '--batch-all-objects', '--batch-check') == objects


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
    assert _verification.verify_retained_candidate(paths, g, project, record, env={}) == snapshot


def test_write_tree_accepts_distinct_paths(tmp_path: Path) -> None:
    snapshot = (
        Entry('a.txt', 0o100644, 'blob', b'1'),
        Entry('b/a.txt', 0o100644, 'blob', b'2'),
        Entry('Docs/guide.txt', 0o100644, 'blob', b'3'),
        Entry('docs-archive/guide.txt', 0o100644, 'blob', b'4'),
    )
    store = _store.store_path(_paths(tmp_path), 'example')
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
    store = _store.store_path(_paths(tmp_path), 'example')

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
    store = _store.store_path(_paths(tmp_path), 'example')

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
    store = _store.store_path(_paths(tmp_path), 'example')

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
