import os
import shlex
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from pubanon import _filesystem
from pubanon._cli import _arguments
from pubanon._config import Paths, ProjectConfig
from pubanon._errors import OperationCanceled, PubanonError
from pubanon._run import GitResult
from pubanon._snapshots import (
    _candidate,
    _history,
    _inspection,
    _records,
    _rewrite,
    _source,
    _store,
)
from pubanon._snapshots._source import (
    Entry,
    PinnedSource,
    SourceBookmark,
    SourceRecovery,
    snapshot_digest,
)
from pubanon._terminal import escape
from tests._git import git_env
from tests.snapshots.test_rewrite import _history as _rewrite_history
from tests.snapshots.test_rewrite import _prepare as _prepare_rewrite


def _commit(store: Path, tree: str, *, parent: str | None = None, when: int) -> str:
    args = ['commit-tree', tree]
    if parent is not None:
        args.extend(('-p', parent))
    return (
        subprocess.run(
            ['git', '-C', store, *args],
            input=b'publication\n',
            capture_output=True,
            check=True,
            env=git_env(
                {
                    'GIT_AUTHOR_NAME': 'Publisher',
                    'GIT_AUTHOR_EMAIL': 'publisher@example.invalid',
                    'GIT_COMMITTER_NAME': 'Publisher',
                    'GIT_COMMITTER_EMAIL': 'publisher@example.invalid',
                    'GIT_AUTHOR_DATE': f'{when} +0000',
                    'GIT_COMMITTER_DATE': f'{when} +0000',
                }
            ),
        )
        .stdout.decode()
        .strip()
    )


def _git(store: Path, *args: str, input_bytes: bytes | None = None) -> str:
    return (
        subprocess.run(
            ['git', '-C', store, *args],
            input=input_bytes,
            capture_output=True,
            check=True,
            env=git_env(),
        )
        .stdout.decode()
        .strip()
    )


def _derived_entry(
    paths: Paths, project: ProjectConfig, original: _history.PublicationEntry, when: int
) -> _history.PublicationEntry:
    assert original.record is not None
    record = original.record
    source_commit = (
        record.pinned.commit
        if record.pinned
        else record.derivation.original_source
        if record.derivation
        else None
    )
    commit = _commit(_store.candidate_store_path(paths, project.name), record.tree, when=when)
    bookmark = None
    if source_commit is not None and record.source_bookmark is not None:
        bookmark = _source.describe_bookmark(project.source_path, project.name, commit)
        _source.ensure_bookmark(bookmark, source_commit)
    derived = replace(
        record,
        commit=commit,
        pinned=None,
        source_bookmark=bookmark,
        derivation=_records.Derivation(original.commit, source_commit, 'e' * 40, ()),
    )
    _records.save_publication(paths, project.name, derived)
    _git(
        _store.candidate_store_path(paths, project.name),
        'update-ref',
        _records.publication_ref(commit),
        commit,
    )
    return _history.PublicationEntry(commit, original.tags, None, derived)


def _guidance_project(tmp_path: Path) -> ProjectConfig:
    return ProjectConfig(
        name='example',
        source_path=tmp_path / 'configured-source',
        host='github',
        owner='owner',
        repo='repo',
        visibility='private',
        allow=('**',),
        deny=(),
        secret_exceptions=(),
        extra_terms=(),
        hygiene_terms=(),
        self_publication=False,
        required_checks=(),
    )


def _paths(tmp_path: Path) -> Paths:
    return Paths(
        tmp_path / 'config',
        tmp_path / 'data',
        tmp_path / 'config' / 'config.toml',
        tmp_path / 'data' / 'registry.json',
    )


def _record(commit: str, tree: str, *, tag: str, confirmed: int) -> _records.PublicationRecord:
    source = Path('/unavailable-source').absolute()
    return _records.PublicationRecord(
        commit=commit,
        tree=tree,
        snapshot_digest='a' * 64,
        fingerprint='b' * 64,
        message='publication',
        tag_intent=tag,
        pinned=PinnedSource('c' * 40, 'explicit', None, None),
        destination=_records.RecordedDestination('github', 'owner', 'repo', 'private', 'account'),
        confirmed_at=datetime.fromtimestamp(confirmed, UTC),
        source_bookmark=SourceBookmark(
            source,
            source / '.git',
            f'refs/pubanon/publications/example/{commit}',
        ),
    )


def _literal_publication(tmp_path: Path) -> tuple[Paths, _history.PublicationEntry]:
    paths = _paths(tmp_path)
    commit = 'a' * 40
    record = _record(commit, 'b' * 40, tag='v1.0.0', confirmed=1_700_000_100)
    return paths, _history.PublicationEntry(commit, ('v1.0.0',), None, record)


def _published_file(tmp_path: Path) -> tuple[Paths, _history.PublicationEntry]:
    paths = _paths(tmp_path)
    store = _store.store_path(paths, 'example')
    blob = _git(store, 'hash-object', '-w', '--stdin', input_bytes=b'published\n')
    tree = _git(store, 'mktree', input_bytes=f'100644 blob {blob}\tREADME.md\n'.encode())
    commit = _commit(store, tree, when=1_700_000_000)
    record = replace(
        _record(commit, tree, tag='v1.0.0', confirmed=1_700_000_100),
        snapshot_digest=snapshot_digest((Entry('README.md', 0o100644, 'blob', b'published\n'),)),
    )
    return paths, _history.PublicationEntry(commit, ('v1.0.0',), None, record)


def _delete_entry(
    tmp_path: Path, source: Path
) -> tuple[Paths, ProjectConfig, _history.PublicationEntry]:
    paths, entry = _published_file(tmp_path)
    project = replace(_guidance_project(tmp_path), source_path=source)
    assert entry.record is not None
    bookmark = _source.describe_bookmark(source, project.name, entry.commit)
    source_commit = _git(source, 'rev-parse', 'HEAD')
    _source.ensure_bookmark(bookmark, source_commit)
    record = replace(
        entry.record,
        pinned=PinnedSource(source_commit, 'explicit', None, None),
        source_bookmark=bookmark,
    )
    _records.save_publication(paths, project.name, record)
    _git(
        _store.candidate_store_path(paths, project.name),
        'update-ref',
        _records.publication_ref(entry.commit),
        entry.commit,
    )
    return paths, project, replace(entry, record=record)


@pytest.mark.parametrize('count', [1, 8])
def test_archive_roots_reads_owned_refs_with_one_git_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int
) -> None:
    paths = _paths(tmp_path)
    store = _store.store_path(paths, 'example')
    tree = _git(store, 'mktree', input_bytes=b'')
    commits = {_commit(store, tree, when=1_700_000_000 + offset) for offset in range(count)}
    for commit in commits:
        _git(store, 'update-ref', _records.publication_ref(commit), commit)

    original = _history.run_git
    calls = 0

    def _count_git(args: Iterable[str], *, cwd: Path, check: bool = True) -> GitResult:
        nonlocal calls
        calls += 1
        return original(args, cwd=cwd, check=check)

    monkeypatch.setattr(_history, 'run_git', _count_git)
    monkeypatch.setattr(_candidate, 'run_git', _count_git)
    assert _history._archive_roots(paths, 'example') == commits
    assert calls == 1


@pytest.mark.parametrize('changed', ['symbolic', 'wrong-target', 'malformed'])
def test_archive_roots_refuses_conflicting_refs(tmp_path: Path, changed: str) -> None:
    paths = _paths(tmp_path)
    store = _store.store_path(paths, 'example')
    tree = _git(store, 'mktree', input_bytes=b'')
    first = _commit(store, tree, when=1_700_000_000)
    second = _commit(store, tree, when=1_700_000_001)
    _git(store, 'update-ref', _records.publication_ref(first), first)
    if changed == 'symbolic':
        _git(
            store,
            'symbolic-ref',
            _records.publication_ref(second),
            _records.publication_ref(first),
        )
        message = 'not a direct ref'
    elif changed == 'wrong-target':
        _git(store, 'update-ref', _records.publication_ref(second), first)
        message = 'conflicts with snapshot'
    else:
        _git(store, 'update-ref', 'refs/pubanon/published/not-a-commit', first)
        message = 'invalid snapshot archive reference'
    with pytest.raises(PubanonError, match=message):
        _history._archive_roots(paths, 'example')


@pytest.mark.parametrize('recordless', [False, True])
@pytest.mark.parametrize('name', ['my project', '-leading'])
def test_delete_retry_selects_exact_project_in_both_cleanup_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    recordless: bool,
    name: str,
) -> None:
    paths, original = _literal_publication(tmp_path)
    entry = replace(original, record=None) if recordless else original
    project = replace(_guidance_project(tmp_path), name=name)
    if not recordless:
        path = _records.publication_path(paths, name, entry.commit)
        path.parent.mkdir(parents=True)
        path.write_text('retained')

    def _skip_preflight(*_args: object) -> None:
        return None

    monkeypatch.setattr(_history, '_delete_preflight', _skip_preflight)

    def _fail_cleanup(*_args: object, **_kwargs: object) -> bool:
        raise OSError('injected cleanup failure')

    monkeypatch.setattr(_inspection, 'clean_inspect', _fail_cleanup)
    with pytest.raises(PubanonError, match='cleanup is incomplete') as error:
        _history.delete_publication(paths, project, entry)
    assert error.value.recovery is not None
    argv = shlex.split(error.value.recovery)
    assert argv == ['pubanon', 'delete', entry.commit, f'--project={name}']
    assert _arguments.build_parser().parse_args(argv[1:]).project == name


def test_deleting_archive_tip_keeps_other_listed_snapshots(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    project = _guidance_project(tmp_path)
    store = _store.store_path(paths, project.name)
    tree = _git(store, 'mktree', input_bytes=b'')
    old = _commit(store, tree, when=1_700_000_000)
    tip = _commit(store, tree, parent=old, when=1_700_000_001)
    _git(store, 'update-ref', _records.publication_ref(tip), tip)
    selected = _history.select_publication(paths, project.name, tip)

    _history.delete_publication(paths, project, selected)

    assert [entry.commit for entry in _history.publications(paths, project.name)] == [old]
    _git(store, 'gc', '--prune=now')
    assert _history.select_publication(paths, project.name, old).commit == old


@pytest.mark.slow
@pytest.mark.parametrize(
    'changed', ['symbolic-source', 'symbolic-archive', 'wrong-archive', 'malformed-archive']
)
def test_derived_delete_refuses_conflicting_owned_refs(
    tmp_path: Path, make_repo: Callable[..., Path], changed: str
) -> None:
    source = make_repo(files={'README.md': 'private source\n'})
    paths, project, original = _delete_entry(tmp_path, source)
    derived = _derived_entry(paths, project, original, 1_700_000_001)
    assert derived.record is not None
    assert derived.record.source_bookmark is not None
    store = _store.candidate_store_path(paths, project.name)
    if changed == 'symbolic-source':
        _git(source, 'symbolic-ref', derived.record.source_bookmark.refname, 'refs/heads/main')
    elif changed == 'symbolic-archive':
        _git(
            store,
            'symbolic-ref',
            _records.publication_ref(derived.commit),
            _records.publication_ref(original.commit),
        )
    elif changed == 'wrong-archive':
        _git(store, 'update-ref', _records.publication_ref(derived.commit), original.commit)
    else:
        _git(store, 'update-ref', 'refs/pubanon/published/not-a-commit', original.commit)
        with pytest.raises(PubanonError, match='archive'):
            _history.publications(paths, project.name)
        return
    with pytest.raises(PubanonError):
        _history.delete_publication(paths, project, derived)
    assert _records.load_publication(paths, project.name, derived.commit) == derived.record
    assert any('protected' in line for line in _history.source_guidance(paths, project, original))


@pytest.mark.slow
@pytest.mark.parametrize('moved', [False, True])
def test_derived_delete_retries_partial_cleanup_after_verified_relocation(
    tmp_path: Path, make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch, moved: bool
) -> None:
    source = make_repo(files={'README.md': 'private source\n'})
    paths, project, original = _delete_entry(tmp_path, source)
    derived = _derived_entry(paths, project, original, 1_700_000_001)
    assert derived.record is not None
    assert original.record is not None
    assert original.record.pinned is not None
    if moved:
        destination = tmp_path / 'moved-source'
        source.rename(destination)
        project = replace(project, source_path=destination)
    real_git = _history.run_git

    def _interrupt(
        args: list[str], *, cwd: Path | None, check: bool = True, input_bytes: bytes | None = None
    ) -> GitResult:
        if args[2:3] == ['update-ref']:
            raise OperationCanceled('injected archive interruption')
        return real_git(args, cwd=cwd, check=check, input_bytes=input_bytes)

    with monkeypatch.context() as scoped:
        scoped.setattr(_history, 'run_git', _interrupt)
        with pytest.raises(OperationCanceled, match='cleanup is incomplete'):
            _history.delete_publication(paths, project, derived)
    saved = _records.load_publication(paths, project.name, derived.commit)
    assert saved is not None
    assert saved.source_bookmark is not None
    assert saved.pinned is None
    assert saved.derivation == derived.record.derivation
    assert saved.source_bookmark.source_path == project.source_path
    assert (
        _source.source_recovery(saved.source_bookmark, original.record.pinned.commit).status
        == 'available'
    )
    assert any('protected' in line for line in _history.source_guidance(paths, project, original))
    _history.delete_publication(
        paths, project, _history.select_publication(paths, project.name, derived.commit)
    )
    assert _records.load_publication(paths, project.name, derived.commit) is None
    assert any('protected' in line for line in _history.source_guidance(paths, project, original))


@pytest.mark.slow
def test_derived_source_cleanup_uses_input_commit_and_keeps_worktree(
    tmp_path: Path, make_repo: Callable[..., Path]
) -> None:
    source = make_repo(files={'README.md': 'private source\n'})
    paths, project, original = _delete_entry(tmp_path, source)
    derived = _derived_entry(paths, project, original, 1_700_000_001)
    assert original.record is not None
    assert original.record.pinned is not None
    linked = tmp_path / 'input-worktree'
    _git(source, 'worktree', 'add', '--detach', str(linked), original.record.pinned.commit)
    lines = _history.source_cleanup_guidance(project, derived)
    assert any('source input' in line.lower() for line in lines)
    assert any(
        shlex.split(line)[-2:] == ['remove', str(linked)]
        for line in lines
        if line.startswith('git ')
    )
    _history.delete_publication(paths, project, derived)
    assert (linked / 'README.md').read_text() == 'private source\n'


@pytest.mark.parametrize('known_source', [False, True])
def test_derived_source_guidance_keeps_input_provenance_without_locator(
    tmp_path: Path, known_source: bool
) -> None:
    paths, entry = _literal_publication(tmp_path)
    assert entry.record is not None
    source_commit = 'c' * 40 if known_source else None
    derivation = _records.Derivation('d' * 40, source_commit, 'e' * 40, ())
    entry = replace(
        entry,
        record=replace(entry.record, pinned=None, source_bookmark=None, derivation=derivation),
    )
    lines = _history.source_guidance(paths, _guidance_project(tmp_path), entry)
    text = '\n'.join(lines)
    assert f'Original snapshot: {derivation.original}' in lines
    assert f'Correction snapshot: {derivation.correction}' in lines
    assert 'input' in text.lower()
    if source_commit is None:
        assert 'unknown' in text.lower()
    else:
        assert source_commit in text
        assert 'location was not preserved' in text
    assert not any(line.startswith('git ') for line in lines)
    assert _history.source_cleanup_guidance(_guidance_project(tmp_path), entry) == ()


def test_history_ambiguous_commit_prefix_names_alternatives(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    first = 'a' * 40
    second = 'a' + 'b' * 39
    entries = (
        _history.PublicationEntry(first, ('v1.0.0',), None, None),
        _history.PublicationEntry(second, ('v2.0.0',), None, None),
    )

    with pytest.raises(PubanonError, match='ambiguous') as error:
        _history.select_publication(paths, 'example', first[0], entries=entries)
    assert first[:12] in error.value.render()
    assert second[:12] in error.value.render()


def test_history_combines_records_and_recorded_refs(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = _store.store_path(paths, 'example')
    tree = _git(store, 'mktree', input_bytes=b'')
    old = _commit(store, tree, when=1_700_000_200)
    current = _commit(store, tree, parent=old, when=1_700_000_100)
    archived = _commit(store, tree, when=1_700_000_300)
    candidate = _commit(store, tree, when=1_700_000_400)
    _git(store, 'update-ref', 'refs/heads/main', current)
    _git(store, 'update-ref', 'refs/tags/v1.0.0', old)
    _git(store, 'update-ref', 'refs/pubanon/candidate', candidate)
    _git(store, 'update-ref', 'refs/pubanon/repair/main', candidate)
    _records.save_publication(
        paths, 'example', _record(old, tree, tag='v1.0.0', confirmed=1_700_000_500)
    )
    _records.save_publication(
        paths, 'example', _record(archived, tree, tag='v2.0.0', confirmed=1_700_000_600)
    )

    entries = _history.publications(paths, 'example')

    assert {entry.commit for entry in entries} == {old, current, archived}
    assert [entry.commit for entry in entries].index(current) < [
        entry.commit for entry in entries
    ].index(old)
    by_commit = {entry.commit: entry for entry in entries}
    assert by_commit[old].tags == ('v1.0.0',)
    assert by_commit[old].record is not None
    assert by_commit[old].commit_time == datetime.fromtimestamp(1_700_000_200, UTC)
    assert by_commit[current].record is None
    assert by_commit[current].tags == ()
    assert by_commit[archived].tags == ('v2.0.0',)
    assert by_commit[archived].record is not None


@pytest.mark.slow
def test_history_delete_accepts_absent_archive_and_verified_relocation(
    tmp_path: Path, make_repo: Callable[..., Path]
) -> None:
    source = make_repo(files={'README.md': 'private source\n'})
    paths, project, entry = _delete_entry(tmp_path, source)
    moved = tmp_path / 'relocated'
    source.rename(moved)
    project = replace(project, source_path=moved)
    _git(
        _store.candidate_store_path(paths, project.name),
        'update-ref',
        '-d',
        _records.publication_ref(entry.commit),
    )
    _history.delete_publication(paths, project, entry)
    assert _records.load_publication(paths, project.name, entry.commit) is None
    assert entry.record is not None
    assert entry.record.pinned is not None
    bookmark = _source.describe_bookmark(moved, project.name, entry.commit)
    assert _source.source_recovery(bookmark, entry.record.pinned.commit).status == 'available'


def test_history_delete_derives_future_inputs_before_rewrite_record_exists(tmp_path: Path) -> None:
    paths, original = _published_file(tmp_path)
    assert original.record is not None
    assert original.record.pinned is not None
    assert original.record.source_bookmark is not None
    store = _store.candidate_store_path(paths, 'example')
    middle = _commit(store, original.record.tree, parent=original.commit, when=1_700_000_001)
    tip = _commit(store, original.record.tree, parent=middle, when=1_700_000_002)
    record = replace(
        original.record,
        commit=middle,
        source_bookmark=replace(
            original.record.source_bookmark,
            refname=f'refs/pubanon/publications/example/{middle}',
        ),
    )
    _records.save_publication(paths, 'example', record)
    _git(store, 'update-ref', _records.publication_ref(tip), tip)
    candidate = _records.CandidateRecord(
        commit='f' * 40,
        tree=record.tree,
        snapshot_digest=record.snapshot_digest,
        fingerprint=record.fingerprint,
        message=record.message,
        tag_intent=None,
        pinned=original.record.pinned,
        prestate_main=tip,
        prestate_tags={},
        destination=record.destination,
        state=_candidate.STATE_RETAINED,
        replaces=original.commit,
        replace_scope='one',
    )
    _records.save_candidate(paths, 'example', candidate)
    entry = _history.select_publication(paths, 'example', middle)
    with pytest.raises(PubanonError, match='snapshot'):
        _history.delete_publication(paths, _guidance_project(tmp_path), entry)
    assert _records.load_publication(paths, 'example', middle) == record


@pytest.mark.parametrize('case', ['absent-source-ref', 'unavailable-source'])
def test_history_delete_handles_source_states(
    tmp_path: Path, make_repo: Callable[..., Path], case: str
) -> None:
    source = make_repo(files={'README.md': 'private source\n'})
    paths, project, entry = _delete_entry(tmp_path, source)
    assert entry.record is not None
    assert entry.record.pinned is not None
    assert entry.record.source_bookmark is not None
    if case == 'absent-source-ref':
        _source.remove_bookmark(entry.record.source_bookmark, entry.record.pinned.commit)
    elif case == 'unavailable-source':
        source.rename(tmp_path / 'unregistered-move')
        with pytest.raises(PubanonError, match='unavailable') as error:
            _history.delete_publication(paths, project, entry)
        assert escape((source / '.git').as_posix()) in error.value.render()
        assert _records.load_publication(paths, project.name, entry.commit) == entry.record
        assert _candidate.publication_archive(paths, project.name, entry.commit)
        return
    _history.delete_publication(paths, project, entry)
    assert _records.load_publication(paths, project.name, entry.commit) is None


def test_history_delete_interrupt_after_unlink_reports_completed_cleanup(
    tmp_path: Path, make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_repo(files={'README.md': 'private source\n'})
    paths, project, entry = _delete_entry(tmp_path, source)
    original = _filesystem.durable_unlink

    def _interrupt_after_unlink(path: Path) -> None:
        if path == _records.publication_path(paths, project.name, entry.commit):
            path.unlink()
            raise KeyboardInterrupt
        original(path)

    with monkeypatch.context() as scoped:
        scoped.setattr(_filesystem, 'durable_unlink', _interrupt_after_unlink)
        with pytest.raises(OperationCanceled, match='durability could not be confirmed') as error:
            _history.delete_publication(paths, project, entry)
    assert 'private recovery record removed' in error.value.render()
    assert 'No further bookmark cleanup is needed' in error.value.render()
    assert _history.publications(paths, project.name) == ()


@pytest.mark.parametrize('cancelled', [False, True])
def test_history_delete_interrupt_reports_completed_source_deletion(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    cancelled: bool,
) -> None:
    source = make_repo(files={'README.md': 'private source\n'})
    paths, project, entry = _delete_entry(tmp_path, source)
    original = _history.run_git

    def _interrupt_archive(
        args: list[str], *, cwd: Path | None, check: bool = True, input_bytes: bytes | None = None
    ) -> GitResult:
        if args[2:3] == ['update-ref']:
            if cancelled:
                raise OperationCanceled('injected Git cancellation')
            raise KeyboardInterrupt
        return original(args, cwd=cwd, check=check, input_bytes=input_bytes)

    with monkeypatch.context() as scoped:
        scoped.setattr(_history, 'run_git', _interrupt_archive)
        with pytest.raises(OperationCanceled) as error:
            _history.delete_publication(paths, project, entry)
    assert 'source bookmark released' in error.value.render()
    assert 'Remaining: archive bookmark, private recovery record' in error.value.render()
    assert _records.load_publication(paths, project.name, entry.commit) == entry.record
    _history.delete_publication(paths, project, entry)


@pytest.mark.parametrize('changed', ['source', 'archive'])
def test_history_delete_preflights_changed_refs(
    tmp_path: Path, make_repo: Callable[..., Path], changed: str
) -> None:
    source = make_repo(files={'README.md': 'private source\n'})
    paths, project, entry = _delete_entry(tmp_path, source)
    assert entry.record is not None
    assert entry.record.pinned is not None
    assert entry.record.source_bookmark is not None
    repo = source if changed == 'source' else _store.candidate_store_path(paths, project.name)
    tree = _git(repo, 'rev-parse', 'HEAD^{tree}') if changed == 'source' else entry.record.tree
    other = _commit(repo, tree, when=1_700_001_000)
    ref = (
        entry.record.source_bookmark.refname
        if changed == 'source'
        else _records.publication_ref(entry.commit)
    )
    _git(repo, 'update-ref', ref, other)
    with pytest.raises(PubanonError):
        _history.delete_publication(paths, project, entry)
    assert _records.load_publication(paths, project.name, entry.commit) == entry.record
    assert _git(repo, 'rev-parse', ref) == other
    if changed == 'archive':
        assert (
            _source.source_recovery(
                entry.record.source_bookmark, entry.record.pinned.commit
            ).status
            == 'protected'
        )


@pytest.mark.parametrize(
    'dependency', ['selected', 'old-descendant', 'new-descendant', 'old-tip', 'prestate-tag']
)
@pytest.mark.parametrize('state', [_candidate.STATE_RETAINED, _candidate.STATE_UNKNOWN_OUTCOME])
def test_history_delete_preserves_complete_replacement_evidence(
    tmp_path: Path, make_repo: Callable[..., Path], dependency: str, state: str
) -> None:
    source = make_repo(files={'README.md': 'private source\n'})
    paths, project, entry = _delete_entry(tmp_path, source)
    assert entry.record is not None
    assert entry.record.pinned is not None
    record = entry.record
    assert record.pinned is not None
    ids: dict[str, str] = dict(
        zip(
            ('selected', 'old-descendant', 'new-descendant', 'old-tip', 'prestate-tag'),
            ('a' * 40, 'b' * 40, 'c' * 40, 'd' * 40, 'e' * 40),
            strict=True,
        )
    )
    ids[dependency] = entry.commit
    candidate = _records.CandidateRecord(
        commit='f' * 40,
        tree=record.tree,
        snapshot_digest=record.snapshot_digest,
        fingerprint=record.fingerprint,
        message=record.message,
        tag_intent=None,
        pinned=record.pinned,
        prestate_main=ids['old-tip'],
        prestate_tags={'v1.0.0': ids['prestate-tag']},
        destination=record.destination,
        state=state,
        replaces=ids['selected'],
        replace_scope='one',
        tip=ids['new-descendant'],
        poststate_tags={'v1.0.0': ids['prestate-tag']},
        replayed=(_records.ReplayEntry(ids['old-descendant'], ids['new-descendant']),),
    )
    _records.save_candidate(paths, project.name, candidate)
    with pytest.raises(PubanonError, match=r'snapshot|rewrite'):
        _history.delete_publication(paths, project, entry)
    assert _records.load_publication(paths, project.name, entry.commit) == record
    assert _candidate.publication_archive(paths, project.name, entry.commit)


@pytest.mark.parametrize('recorded', [False, True])
def test_history_delete_preserves_unreplayed_frozen_descendant(
    tmp_path: Path, make_repo: Callable[..., Path], recorded: bool
) -> None:
    source = make_repo(files={'README.md': 'private source\n'})
    paths, project, entry = _delete_entry(tmp_path, source)
    if not recorded:
        _records.publication_path(paths, project.name, entry.commit).unlink()
        entry = replace(entry, record=None)
    progress = _records.RewriteRecord(
        'a' * 40, 'b' * 40, 'c' * 40, (entry.commit, 'c' * 40), 'replaying'
    )
    _records.save_rewrite(paths, project.name, progress)
    with pytest.raises(PubanonError, match='rewrite'):
        _history.delete_publication(paths, project, entry)
    assert _candidate.publication_archive(paths, project.name, entry.commit)


@pytest.mark.parametrize(
    ('complete', 'state'),
    [
        (False, _candidate.STATE_RETAINED),
        (True, _candidate.STATE_RETAINED),
        (True, _candidate.STATE_UNKNOWN_OUTCOME),
    ],
)
def test_history_delete_preserves_untagged_ancestor_during_replace_all(
    tmp_path: Path, make_repo: Callable[..., Path], complete: bool, state: str
) -> None:
    source = make_repo(files={'README.md': 'private source\n'})
    paths, project, entry = _delete_entry(tmp_path, source)
    assert entry.record is not None
    record = replace(entry.record, tag_intent=None)
    assert record.pinned is not None
    assert record.source_bookmark is not None
    _records.publication_path(paths, project.name, entry.commit).unlink()
    _records.save_publication(paths, project.name, record)
    entry = replace(entry, tags=(), record=record)
    store = _store.candidate_store_path(paths, project.name)
    old_tip = _commit(store, record.tree, parent=entry.commit, when=1_700_000_001)
    correction = _commit(store, record.tree, when=1_700_000_002)
    _git(store, 'update-ref', 'refs/heads/main', old_tip)
    candidate = _records.CandidateRecord(
        commit=correction,
        tree=record.tree,
        snapshot_digest=record.snapshot_digest,
        fingerprint=record.fingerprint,
        message=record.message,
        tag_intent=None,
        pinned=record.pinned,
        prestate_main=old_tip,
        prestate_tags={},
        destination=record.destination,
        state=state,
        replaces=old_tip,
        replace_scope='all',
        tip=correction if complete else None,
        poststate_tags={} if complete else None,
    )
    _records.save_candidate(paths, project.name, candidate)
    with pytest.raises(PubanonError, match='snapshot'):
        _history.delete_publication(paths, project, entry)
    assert _records.load_publication(paths, project.name, entry.commit) == record
    assert _candidate.publication_archive(paths, project.name, entry.commit)
    assert _git(source, 'rev-parse', record.source_bookmark.refname) == record.pinned.commit
    assert (source / 'README.md').read_text() == 'private source\n'
    assert _records.load_candidate(paths, project.name) == candidate
    assert _candidate.published_refs(paths, project.name) == (old_tip, {})


@pytest.mark.parametrize('state', [_candidate.STATE_RETAINED, _candidate.STATE_UNKNOWN_OUTCOME])
def test_history_delete_refuses_active_candidate_evidence(
    tmp_path: Path, make_repo: Callable[..., Path], state: str
) -> None:
    source = make_repo(files={'README.md': 'private source\n'})
    paths, project, entry = _delete_entry(tmp_path, source)
    assert entry.record is not None
    record = entry.record
    assert record.pinned is not None
    candidate = _records.CandidateRecord(
        commit=record.commit,
        tree=record.tree,
        snapshot_digest=record.snapshot_digest,
        fingerprint=record.fingerprint,
        message=record.message,
        tag_intent=record.tag_intent,
        pinned=record.pinned,
        prestate_main=None,
        prestate_tags={},
        destination=record.destination,
        state=state,
        source_bookmark=record.source_bookmark,
    )
    _records.save_candidate(paths, project.name, candidate)
    with pytest.raises(PubanonError, match='snapshot'):
        _history.delete_publication(paths, project, entry)
    assert _records.load_publication(paths, project.name, entry.commit) == record
    assert _candidate.publication_archive(paths, project.name, entry.commit)


@pytest.mark.slow
@pytest.mark.parametrize('operational', [False, True])
def test_history_delete_releases_only_owned_preservation(
    tmp_path: Path, make_repo: Callable[..., Path], operational: bool
) -> None:
    source = make_repo(files={'README.md': 'private source\n'})
    paths, project, entry = _delete_entry(tmp_path, source)
    assert entry.record is not None
    assert entry.record.pinned is not None
    assert entry.record.source_bookmark is not None
    store = _store.candidate_store_path(paths, project.name)
    if operational:
        _git(store, 'update-ref', 'refs/heads/main', entry.commit)
        _git(store, 'update-ref', 'refs/tags/v1.0.0', entry.commit)
    other = _source.describe_bookmark(source, project.name, 'a' * 40)
    _source.ensure_bookmark(other, entry.record.pinned.commit)
    inspection = _history.inspect_publication(paths, project.name, entry)

    _history.delete_publication(paths, project, entry)

    assert _records.load_publication(paths, project.name, entry.commit) is None
    assert not _candidate.publication_archive(paths, project.name, entry.commit)
    assert (
        _source.source_recovery(entry.record.source_bookmark, entry.record.pinned.commit).status
        == 'available'
    )
    assert _source.source_recovery(other, entry.record.pinned.commit).status == 'protected'
    assert not inspection.exists()
    remaining = _history.publications(paths, project.name)
    assert remaining == ()
    if operational:
        assert _candidate.published_refs(paths, project.name) == (
            entry.commit,
            {'v1.0.0': entry.commit},
        )
        for selector in ('latest', 'v1.0.0', entry.commit):
            with pytest.raises(PubanonError):
                _history.select_publication(paths, project.name, selector)
            assert _rewrite.resolve_replacement(paths, project.name, selector) == (
                entry.commit,
                'one',
            )
        _git(store, 'update-ref', _records.publication_ref(entry.commit), entry.commit)
        assert _history.publications(paths, project.name) == ()


@pytest.mark.slow
def test_history_delete_relocated_partial_failure_retains_verified_locator(
    tmp_path: Path, make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_repo(files={'README.md': 'private source\n'})
    paths, project, entry = _delete_entry(tmp_path, source)
    assert entry.record is not None
    moved = tmp_path / 'moved'
    source.rename(moved)
    project = replace(project, source_path=moved)
    original = _history.run_git

    def _fail_archive(
        args: list[str], *, cwd: Path | None, check: bool = True, input_bytes: bytes | None = None
    ) -> GitResult:
        if args[2:3] == ['update-ref']:
            raise PubanonError('injected archive failure')
        return original(args, cwd=cwd, check=check, input_bytes=input_bytes)

    with monkeypatch.context() as scoped:
        scoped.setattr(_history, 'run_git', _fail_archive)
        with pytest.raises(PubanonError, match='cleanup is incomplete'):
            _history.delete_publication(paths, project, entry)
    updated = _records.load_publication(paths, project.name, entry.commit)
    assert updated is not None
    assert updated.pinned is not None
    assert updated.source_bookmark is not None
    assert updated.source_bookmark.git_common_dir == moved / '.git'
    assert replace(updated, source_bookmark=entry.record.source_bookmark) == entry.record
    assert (
        _source.source_recovery(updated.source_bookmark, updated.pinned.commit).status
        == 'available'
    )
    _history.delete_publication(
        paths, project, _history.select_publication(paths, project.name, entry.commit)
    )
    assert _records.load_publication(paths, project.name, entry.commit) is None


@pytest.mark.skipif(os.name == 'nt', reason='Windows uses a write-through tombstone rename')
def test_history_delete_reports_record_directory_durability_failure(
    tmp_path: Path, make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_repo(files={'README.md': 'private source\n'})
    paths, project, entry = _delete_entry(tmp_path, source)

    sync = _history._filesystem.sync_directory

    def _fail_sync(directory: Path) -> None:
        if not _records.publication_path(paths, project.name, entry.commit).exists():
            raise OSError('injected directory fsync failure')
        sync(directory)

    monkeypatch.setattr(_history._filesystem, 'sync_directory', _fail_sync)
    with pytest.raises(PubanonError, match='durability could not be confirmed') as error:
        _history.delete_publication(paths, project, entry)
    assert 'private recovery record removed' in error.value.render()
    assert 'No further bookmark cleanup is needed' in error.value.render()
    assert _records.load_publication(paths, project.name, entry.commit) is None
    assert _history.publications(paths, project.name) == ()


def test_history_delete_retries_failed_deletion_record(
    tmp_path: Path, make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_repo(files={'README.md': 'private source\n'})
    paths, project, entry = _delete_entry(tmp_path, source)
    store = _store.candidate_store_path(paths, project.name)
    _git(store, 'update-ref', 'refs/heads/main', entry.commit)

    def _fail_marker(_paths: Paths, _project: str, _commit: str) -> None:
        raise PubanonError('injected deletion record failure')

    with monkeypatch.context() as scoped:
        scoped.setattr(_records, 'mark_publication_deleted', _fail_marker)
        with pytest.raises(PubanonError, match='cleanup is incomplete'):
            _history.delete_publication(paths, project, entry)
    assert _history.select_publication(paths, project.name, 'latest').record == entry.record
    _history.delete_publication(paths, project, entry)
    assert _history.publications(paths, project.name) == ()


def test_history_delete_retries_partial_deletion(
    tmp_path: Path, make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_repo(files={'README.md': 'private source\n'})
    paths, project, entry = _delete_entry(tmp_path, source)
    assert entry.record is not None
    assert entry.record.pinned is not None
    assert entry.record.source_bookmark is not None
    original = _history.run_git

    def _fail_archive(
        args: list[str], *, cwd: Path | None, check: bool = True, input_bytes: bytes | None = None
    ) -> GitResult:
        if args[2:3] == ['update-ref']:
            raise PubanonError('injected archive failure')
        return original(args, cwd=cwd, check=check, input_bytes=input_bytes)

    with monkeypatch.context() as scoped:
        scoped.setattr(_history, 'run_git', _fail_archive)
        with pytest.raises(PubanonError) as error:
            _history.delete_publication(paths, project, entry)
    assert 'source bookmark released' in error.value.render()
    assert 'archive bookmark' in error.value.render()
    assert 'pubanon delete' in error.value.render()
    assert _records.load_publication(paths, project.name, entry.commit) == entry.record
    assert (
        _source.source_recovery(entry.record.source_bookmark, entry.record.pinned.commit).status
        == 'available'
    )
    _history.delete_publication(paths, project, entry)
    assert _records.load_publication(paths, project.name, entry.commit) is None


def test_history_delete_retries_record_unlink_failure(
    tmp_path: Path, make_repo: Callable[..., Path]
) -> None:
    source = make_repo(files={'README.md': 'private source\n'})
    paths, project, entry = _delete_entry(tmp_path, source)
    with (
        patch.object(
            _filesystem, 'durable_unlink', side_effect=OSError('injected record failure')
        ),
        pytest.raises(PubanonError) as error,
    ):
        _history.delete_publication(paths, project, entry)
    assert 'archive bookmark released' in error.value.render()
    assert 'Remaining: private recovery record' in error.value.render()
    assert _records.load_publication(paths, project.name, entry.commit) == entry.record
    _history.delete_publication(paths, project, entry)
    assert _records.load_publication(paths, project.name, entry.commit) is None


@pytest.mark.slow
@pytest.mark.parametrize('derived_first', [False, True])
def test_history_derived_recovery_survives_independent_delete_and_repeat_replacement(
    tmp_path: Path, make_repo: Callable[..., Path], derived_first: bool
) -> None:
    source = make_repo(files={'README.md': 'private source\n'})
    paths, project, original = _delete_entry(tmp_path, source)
    derived = _derived_entry(paths, project, original, 1_700_000_001)
    repeated = _derived_entry(paths, project, derived, 1_700_000_002)
    first, other = (derived, original) if derived_first else (original, derived)
    inspection = _history.inspect_publication(paths, project.name, derived)
    _history.delete_publication(paths, project, first)
    for entry in (other, repeated):
        lines = _history.source_guidance(paths, project, entry)
        assert any('protected' in line for line in lines)
        if entry.record and entry.record.derivation:
            assert any('input' in line.lower() for line in lines)
            assert any(entry.record.derivation.original in line for line in lines)
            assert any(entry.record.derivation.correction in line for line in lines)
            assert entry.record.pinned is None
        assert any(line.startswith('git ') for line in lines)
    _history.delete_publication(paths, project, other)
    assert any('protected' in line for line in _history.source_guidance(paths, project, repeated))
    assert not inspection.exists()
    _history.delete_publication(paths, project, repeated)


def test_history_lists_archived_recordless_chain_but_not_rewrite_refs(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = _store.store_path(paths, 'example')
    tree = _git(store, 'mktree', input_bytes=b'')
    old = _commit(store, tree, when=1_700_000_000)
    tip = _commit(store, tree, parent=old, when=1_700_000_001)
    current = _commit(store, tree, when=1_700_000_002)
    pending = _commit(store, tree, when=1_700_000_003)
    _git(store, 'update-ref', _records.publication_ref(tip), tip)
    _git(store, 'update-ref', 'refs/heads/main', current)
    _git(store, 'update-ref', 'refs/tags/v1.0.0', current)
    _git(store, 'update-ref', 'refs/tags/v2.0.0', current)
    _git(store, 'update-ref', 'refs/pubanon/rewrite/tip', pending)
    entries = {entry.commit: entry for entry in _history.publications(paths, 'example')}
    assert set(entries) == {old, tip, current}
    assert entries[old].replaced
    assert entries[tip].replaced
    assert not entries[current].replaced
    assert entries[current].tags == ('v1.0.0', 'v2.0.0')
    assert entries[old].record is None
    assert _history.select_publication(paths, 'example', old[:12]).commit == old
    project = _guidance_project(tmp_path)
    _history.delete_publication(paths, project, entries[old])
    assert _candidate.publication_archive(paths, project.name, tip)
    assert old not in {entry.commit for entry in _history.publications(paths, project.name)}
    _history.delete_publication(paths, project, entries[tip])
    assert not _candidate.publication_archive(paths, project.name, tip)
    assert [entry.commit for entry in _history.publications(paths, project.name)] == [current]


def test_history_operational_tag_wins_over_unrelated_saved_labels(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = _store.store_path(paths, 'example')
    tree = _git(store, 'mktree', input_bytes=b'')
    old = _commit(store, tree, when=1_700_000_000)
    new = _commit(store, tree, when=1_700_000_100)
    unrelated = _commit(store, tree, when=1_700_000_200)
    _git(store, 'update-ref', 'refs/tags/v1.0.0', new)
    for oid, replaces in ((old, None), (new, old), (unrelated, None)):
        _records.save_publication(
            paths,
            'example',
            replace(
                _record(oid, tree, tag='v1.0.0', confirmed=1_700_000_300),
                replaces=replaces,
                replace_scope='one' if replaces else None,
            ),
        )
    assert _history.select_publication(paths, 'example', 'v1.0.0').commit == new
    _git(store, 'update-ref', '-d', 'refs/tags/v1.0.0')
    with pytest.raises(PubanonError, match='ambiguous'):
        _history.select_publication(paths, 'example', 'v1.0.0')


def test_history_record_without_local_main_is_not_replaced(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    record = _record('a' * 40, 'b' * 40, tag='v1.0.0', confirmed=1_700_000_000)
    _records.save_publication(paths, 'example', record)

    (entry,) = _history.publications(paths, 'example')

    assert not entry.replaced


def test_history_record_without_store_does_not_create_one(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    record = _record('a' * 40, 'b' * 40, tag='v1.0.0', confirmed=1_700_000_000)
    _records.save_publication(paths, 'example', record)

    entry = _history.select_publication(paths, 'example', 'v1.0.0')

    assert entry.record == record
    assert entry.commit_time is None
    assert not _store.candidate_store_path(paths, 'example').exists()


@pytest.mark.slow
def test_history_records_repeated_replay_as_input_chain(tmp_path: Path) -> None:
    paths, g, project, _, _ = _prepare_rewrite(tmp_path)
    first = _rewrite.continue_rewrite(paths, g, project, env={})
    first = replace(
        first,
        source_bookmark=SourceBookmark(
            project.source_path,
            project.source_path / '.git',
            _source.bookmark_ref(project.name, first.commit),
        ),
    )
    _history.record_publication(paths, project, first)
    store = _store.candidate_store_path(paths, project.name)
    tip, tags = _records.candidate_poststate(first)
    _git(store, 'update-ref', 'refs/heads/main', tip)
    for tag, commit in tags.items():
        _git(store, 'update-ref', f'refs/tags/{tag}', commit)
    _git(store, 'update-ref', '-d', _candidate.CANDIDATE_REF)
    _records.candidate_path(paths, project.name).unlink()
    selected = first.replayed[0].new
    source = _source.read_snapshot(store, selected)
    snapshot = tuple(
        entry._replace(data=b'corrected again\n') if entry.path == 'base.txt' else entry
        for entry in source
    )
    _candidate.build_candidate(
        paths,
        g,
        project,
        _candidate.CandidateInputs(
            snapshot, first.pinned, None, None, replaces=selected, replace_scope='one'
        ),
    )
    second = _rewrite.continue_rewrite(paths, g, project, env={})
    second = replace(
        second,
        source_bookmark=SourceBookmark(
            project.source_path,
            project.source_path / '.git',
            _source.bookmark_ref(project.name, second.commit),
        ),
    )
    _history.record_publication(paths, project, second)
    correction = _records.load_publication(paths, project.name, second.commit)
    assert correction is not None
    assert correction.replaces == selected
    assert correction.pinned == first.pinned
    replay = second.replayed[0]
    record = _records.load_publication(paths, project.name, replay.new)
    assert record is not None
    assert record.pinned is None
    assert record.source_bookmark is None
    assert record.derivation == _records.Derivation(
        first.replayed[-1].new, first.pinned.commit, second.commit, ()
    )
    assert record.tree == _git(store, 'rev-parse', f'{replay.new}^{{tree}}')
    entry = _history.select_publication(paths, project.name, replay.new)
    lines = _history.source_guidance(paths, project, entry)
    assert any(first.replayed[-1].new in line for line in lines)
    assert any('may differ' in line for line in lines)


@pytest.mark.slow
@pytest.mark.parametrize('descendant', [1, 2], ids=['intermediate', 'tip'])
@pytest.mark.parametrize(
    ('tags', 'intent'),
    [
        (('latest',), None),
        (('latest', 'v1.2.3'), 'v1.2.3'),
        (('latest', 'v2.0.0', 'v1.2.3'), 'v1.2.3'),
        (('latest', 'v01.2.3', 'v1.2.3-rc1', 'v1.2.3+build', '1.2.3'), None),
    ],
)
def test_history_records_replay_release_intent_and_preserves_tags(
    tmp_path: Path, descendant: int, tags: tuple[str, ...], intent: str | None
) -> None:
    paths, g, project, old = _rewrite_history(tmp_path)
    store = _store.candidate_store_path(paths, project.name)
    _git(store, 'update-ref', '-d', f'refs/tags/v1.0.{descendant}')
    for tag in tags:
        _git(store, 'update-ref', f'refs/tags/{tag}', old[descendant])
    before = _candidate.published_refs(paths, project.name)
    original = _records.load_publication(paths, project.name, old[0])
    assert original is not None
    assert original.pinned is not None
    _candidate.build_candidate(
        paths,
        g,
        project,
        _candidate.CandidateInputs(
            (Entry('base.txt', 0o100644, 'blob', b'corrected\n'),),
            original.pinned,
            None,
            None,
            replaces=old[0],
            replace_scope='one',
        ),
    )
    candidate = _rewrite.continue_rewrite(paths, g, project, env={})
    candidate = replace(
        candidate,
        source_bookmark=SourceBookmark(
            project.source_path,
            project.source_path / '.git',
            _source.bookmark_ref(project.name, candidate.commit),
        ),
    )

    _history.record_publication(paths, project, candidate)

    commit = candidate.replayed[descendant - 1].new
    recorded = _records.load_publication(paths, project.name, commit)
    assert recorded is not None
    assert recorded.tag_intent == intent
    poststate = _records.candidate_poststate(candidate)
    assert set(poststate[1]) == set(before[1])
    assert all(poststate[1][tag] == commit for tag in tags)
    assert _candidate.published_refs(paths, project.name) == before
    for tag, target in poststate[1].items():
        _git(store, 'update-ref', f'refs/tags/{tag}', target)
    _git(store, 'update-ref', 'refs/heads/main', poststate[0])
    assert _candidate.published_refs(paths, project.name) == poststate
    entries = {entry.commit: entry for entry in _history.publications(paths, project.name)}
    if intent is not None:
        assert intent in entries[commit].tags
    if 'v2.0.0' in tags:
        assert 'v2.0.0' in entries[commit].tags


def test_history_replacement_chain_selects_current_tag(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = _store.store_path(paths, 'example')
    tree = _git(store, 'mktree', input_bytes=b'')
    commits = [_commit(store, tree, when=1_700_000_000 + i) for i in range(3)]
    _git(store, 'update-ref', 'refs/tags/v1.0.0', commits[-1])
    _git(store, 'update-ref', 'refs/heads/main', commits[-1])
    for index, oid in enumerate(commits):
        _records.save_publication(
            paths,
            'example',
            replace(
                _record(oid, tree, tag='v1.0.0', confirmed=1_700_000_100 + index),
                replaces=commits[index - 1] if index else None,
                replace_scope='one' if index else None,
            ),
        )
    assert _history.select_publication(paths, 'example', 'v1.0.0').commit == commits[-1]
    entries = {entry.commit: entry for entry in _history.publications(paths, 'example')}
    assert entries[commits[0]].replaced
    assert entries[commits[1]].replaced


def test_history_replacement_selects_current_tag_and_retains_old_record(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = _store.store_path(paths, 'example')
    tree = _git(store, 'mktree', input_bytes=b'')
    old = _commit(store, tree, when=1_700_000_000)
    new = _commit(store, tree, when=1_700_000_100)
    _git(store, 'update-ref', 'refs/heads/main', new)
    _git(store, 'update-ref', 'refs/tags/v1.0.0', new)
    old_record = _record(old, tree, tag='v1.0.0', confirmed=1_700_000_200)
    new_record = replace(
        _record(new, tree, tag='v1.0.0', confirmed=1_700_000_300),
        replaces=old,
        replace_scope='one',
    )
    _records.save_publication(paths, 'example', old_record)
    _records.save_publication(paths, 'example', new_record)

    entries = {entry.commit: entry for entry in _history.publications(paths, 'example')}
    assert set(entries) == {old, new}
    assert entries[old].record == old_record
    assert entries[old].replaced
    assert entries[new].record == new_record
    assert not entries[new].replaced
    assert _history.select_publication(paths, 'example', 'v1.0.0').commit == new
    assert _history.select_publication(paths, 'example', old).record == old_record

    _git(store, 'update-ref', _records.publication_ref(old), old)
    _records.publication_path(paths, 'example', old).unlink()
    recordless = _history.select_publication(paths, 'example', old)
    assert recordless.record is None
    _history.delete_publication(paths, _guidance_project(tmp_path), recordless)
    assert _history.select_publication(paths, 'example', 'v1.0.0').commit == new


def test_history_resolves_only_publication_selectors(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = _store.store_path(paths, 'example')
    tree = _git(store, 'mktree', input_bytes=b'')
    old = _commit(store, tree, when=1_700_000_000)
    current = _commit(store, tree, parent=old, when=1_700_000_100)
    candidate = _commit(store, tree, when=1_700_000_200)
    _git(store, 'update-ref', 'refs/heads/main', current)
    _git(store, 'update-ref', 'refs/tags/v1.0.0', old)
    _git(store, 'update-ref', 'refs/pubanon/candidate', candidate)
    _records.save_publication(
        paths, 'example', _record(old, tree, tag='v1.0.0', confirmed=1_700_000_300)
    )

    assert _history.select_publication(paths, 'example', 'latest').commit == current
    assert _history.select_publication(paths, 'example', 'v1.0.0').commit == old
    assert _history.select_publication(paths, 'example', old[:12]).commit == old
    for selector in ('HEAD~1', 'refs/heads/main', candidate, 'v9.0.0'):
        with pytest.raises(PubanonError):
            _history.select_publication(paths, 'example', selector)

    _git(store, 'update-ref', 'refs/tags/v1.0.0', current)
    assert _history.select_publication(paths, 'example', 'v1.0.0').commit == current
    _git(store, 'update-ref', '-d', 'refs/heads/main')
    with pytest.raises(PubanonError, match='latest') as error:
        _history.select_publication(paths, 'example', 'latest')
    assert 'v1.0.0' in error.value.render()


def test_history_retains_archives_after_store_repair(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = _store.store_path(paths, 'example')
    tree = _git(store, 'mktree', input_bytes=b'')
    archived = _commit(store, tree, when=1_700_000_000)
    current = _commit(store, tree, when=1_700_000_100)
    _git(store, 'update-ref', 'refs/heads/main', current)
    _git(store, 'update-ref', _records.publication_ref(archived), archived)
    _records.save_publication(
        paths, 'example', _record(archived, tree, tag='v1.0.0', confirmed=1_700_000_200)
    )

    entries = _history.publications(paths, 'example')
    assert {entry.commit for entry in entries} == {archived, current}
    assert _history.select_publication(paths, 'example', 'v1.0.0').commit == archived
    _git(store, 'update-ref', '-d', _records.publication_ref(archived))
    _git(store, 'prune', '--expire=now')
    missing = _history.select_publication(paths, 'example', 'v1.0.0')
    assert missing.commit_time is None
    assert missing.record is not None


def test_history_surfaces_malformed_record(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    record = _record('a' * 40, 'b' * 40, tag='v1.0.0', confirmed=1_700_000_000)
    _records.save_publication(paths, 'example', record)
    path = _records.publication_path(paths, 'example', record.commit)
    path.write_text('invalid = true\n')
    with pytest.raises(PubanonError, match='published snapshot record'):
        _history.publications(paths, 'example')


def test_inspect_missing_store_reports_unavailable_commit(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    record = _record('a' * 40, 'b' * 40, tag='v1.0.0', confirmed=1_700_000_000)
    entry = _history.PublicationEntry(record.commit, (), None, record)
    with pytest.raises(PubanonError, match='published commit is unavailable') as error:
        _history.inspect_publication(paths, 'example', entry)
    assert 'pubanon repo restore --to local' in error.value.render()


def test_inspect_publication_refuses_missing_or_conflicting_objects_before_refresh(
    tmp_path: Path,
) -> None:
    paths, entry = _published_file(tmp_path)
    copy = _history.inspect_publication(paths, 'example', entry)
    _filesystem.chmod(copy / 'README.md', 0o600)
    (copy / 'README.md').write_bytes(b'keep edits\n')
    assert entry.record is not None
    for bad in (
        replace(entry, record=replace(entry.record, tree='f' * 40)),
        replace(entry, record=replace(entry.record, snapshot_digest='f' * 64)),
    ):
        with pytest.raises(PubanonError):
            _history.inspect_publication(paths, 'example', bad, refresh=True)
        assert (copy / 'README.md').read_bytes() == b'keep edits\n'
    _git(_store.candidate_store_path(paths, 'example'), 'prune', '--expire=now')
    with pytest.raises(PubanonError) as error:
        _history.inspect_publication(paths, 'example', entry, refresh=True)
    assert 'pubanon repo restore --to local' in error.value.render()
    assert (copy / 'README.md').read_bytes() == b'keep edits\n'


def test_inspect_publication_without_candidate_or_source_verifies_and_refreshes_copy(
    tmp_path: Path,
) -> None:
    paths, entry = _published_file(tmp_path)
    assert entry.record is not None
    copy = _history.inspect_publication(paths, 'example', entry)
    assert (copy / 'README.md').read_bytes() == b'published\n'
    assert _history.inspect_publication(paths, 'example', entry) == copy
    _filesystem.chmod(copy / 'README.md', 0o600)
    (copy / 'README.md').write_bytes(b'edited\n')
    with pytest.raises(PubanonError, match='does not match the saved snapshot'):
        _history.inspect_publication(paths, 'example', entry)
    assert (copy / 'README.md').read_bytes() == b'edited\n'
    assert _history.inspect_publication(paths, 'example', entry, refresh=True) == copy
    assert (copy / 'README.md').read_bytes() == b'published\n'


def test_inspect_recordless_publication_uses_available_commit(tmp_path: Path) -> None:
    paths, entry = _published_file(tmp_path)
    recordless = replace(entry, record=None)
    copy = _history.inspect_publication(paths, 'example', recordless)
    assert (copy / 'README.md').read_bytes() == b'published\n'


def test_recordless_delete_retains_archive_when_tombstone_directory_is_a_file(
    tmp_path: Path,
) -> None:
    paths, published = _published_file(tmp_path)
    project = _guidance_project(tmp_path)
    entry = replace(published, record=None)
    store = _store.candidate_store_path(paths, project.name)
    archive = _records.publication_ref(entry.commit)
    _git(store, 'update-ref', archive, entry.commit)
    directory = _records.publication_path(paths, project.name, entry.commit).parent
    directory.write_text('blocked')

    with pytest.raises((PubanonError, OSError)) as error:
        _history.delete_publication(paths, project, entry)
    assert _candidate.publication_archive(paths, project.name, entry.commit)
    assert isinstance(error.value, PubanonError)
    assert 'cleanup is incomplete' in error.value.render()
    assert 'archive bookmark' in error.value.render()
    assert 'pubanon delete' in error.value.render()
    assert _git(store, 'rev-parse', archive) == entry.commit
    directory.unlink()
    _history.delete_publication(paths, project, entry)
    assert not _candidate.publication_archive(paths, project.name, entry.commit)
    assert entry.commit in _records.deleted_publications(paths, project.name)


@pytest.mark.parametrize('boundary', ['inspection', 'tombstone', 'archive'])
def test_recordless_delete_retries_without_releasing_other_publications(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    source = make_repo(files={'README.md': 'private source\n'})
    paths, project, original = _delete_entry(tmp_path, source)
    other = _derived_entry(paths, project, original, 1_700_000_001)
    assert other.record is not None
    assert other.record.source_bookmark is not None
    assert original.record is not None
    assert original.record.pinned is not None
    _records.publication_path(paths, project.name, original.commit).unlink()
    entry = replace(original, record=None)
    store = _store.candidate_store_path(paths, project.name)

    def _fail_cleanup(*_args: object, **_kwargs: object) -> None:
        raise OSError('injected cleanup failure')

    with monkeypatch.context() as fault:
        if boundary == 'inspection':
            fault.setattr(_inspection, 'clean_inspect', _fail_cleanup)
        elif boundary == 'tombstone':
            fault.setattr(_filesystem, 'sync_directory', _fail_cleanup)
        else:
            fault.setattr(_history, '_release_archive', _fail_cleanup)
        with pytest.raises(PubanonError, match='cleanup is incomplete') as error:
            _history.delete_publication(paths, project, entry)
    assert _candidate.publication_archive(paths, project.name, entry.commit)
    if boundary == 'inspection':
        assert 'Completed: none' in error.value.render()
        assert entry.commit not in _records.deleted_publications(paths, project.name)
    else:
        assert entry.commit in _records.deleted_publications(paths, project.name)
    selected = _history.select_publication(paths, project.name, entry.commit, include_deleted=True)
    _history.delete_publication(paths, project, selected)
    assert not _candidate.publication_archive(paths, project.name, entry.commit)
    assert _candidate.publication_archive(paths, project.name, other.commit)
    assert _records.load_publication(paths, project.name, other.commit) == other.record
    assert (
        _git(source, 'rev-parse', other.record.source_bookmark.refname)
        == original.record.pinned.commit
    )
    assert _git(store, 'rev-parse', _records.publication_ref(other.commit)) == other.commit


def test_source_guidance_distinguishes_recordless_entry_and_forwards_relocation(
    tmp_path: Path,
) -> None:
    paths, entry = _literal_publication(tmp_path)
    project = _guidance_project(tmp_path)
    assert (
        'not recorded' in _history.source_guidance(paths, project, replace(entry, record=None))[0]
    )
    assert entry.record is not None
    assert entry.record.pinned is not None
    source_commit = entry.record.pinned.commit
    source = tmp_path / 'source'
    bookmark = SourceBookmark(source, source, 'refs/pubanon/publications/example/' + entry.commit)
    entry = replace(entry, record=replace(entry.record, source_bookmark=bookmark))
    with patch.object(
        _source,
        'source_recovery',
        return_value=SourceRecovery('unavailable', None, 'source missing'),
    ) as recovery:
        _history.source_guidance(paths, project, entry)
    recovery.assert_called_once_with(bookmark, source_commit, relocated_source=project.source_path)


@pytest.mark.parametrize('state', ['matching', 'occupied', 'registered', 'available'])
def test_source_guidance_handles_existing_worktree_and_unprotected_source(
    tmp_path: Path, state: str
) -> None:
    paths, entry = _literal_publication(tmp_path)
    assert entry.record is not None
    assert entry.record.pinned is not None
    source_commit = entry.record.pinned.commit
    source = tmp_path / 'source'
    source.mkdir()
    bookmark = SourceBookmark(source, source, 'refs/pubanon/publications/example/' + entry.commit)
    entry = replace(entry, record=replace(entry.record, source_bookmark=bookmark))
    destination = paths.data_dir / 'projects/example/source-worktrees' / entry.commit
    if state in {'matching', 'occupied'}:
        destination.mkdir(parents=True)
    registration = (
        f'worktree {destination}\0HEAD {source_commit}\0detached\0\0'.encode()
        if state in {'matching', 'registered'}
        else b''
    )
    recovery = SourceRecovery(
        'available' if state == 'available' else 'protected',
        source,
        'source bookmark is missing' if state == 'available' else None,
    )
    with (
        patch.object(_source, 'source_recovery', return_value=recovery),
        patch.object(_history, 'run_git', return_value=GitResult(0, registration, b'')),
    ):
        lines = _history.source_guidance(paths, _guidance_project(tmp_path), entry)
    commands = [shlex.split(line) for line in lines if line.startswith('git ')]
    if state == 'matching':
        assert len(commands) == 1
        assert commands[0][-2:] == ['remove', str(destination)]
        assert any('Matching detached' in line for line in lines)
    elif state in {'occupied', 'registered'}:
        assert commands == []
        assert any('conflicting' in line for line in lines)
    else:
        assert len(commands) == 2
        assert any('bookmark is missing' in line for line in lines)


def test_source_guidance_omits_unsafe_command_and_handles_unavailable_source(
    tmp_path: Path,
) -> None:
    paths, entry = _literal_publication(tmp_path)
    assert entry.record is not None
    assert entry.record.pinned is not None
    bookmark = SourceBookmark(
        tmp_path / 'source\nnewline',
        tmp_path / 'git',
        'refs/pubanon/publications/example/' + entry.commit,
    )
    entry = replace(entry, record=replace(entry.record, source_bookmark=bookmark))
    with (
        patch.object(
            _source,
            'source_recovery',
            return_value=SourceRecovery('protected', bookmark.source_path, None),
        ),
        patch.object(_history, 'run_git', return_value=GitResult(0, b'', b'')),
    ):
        lines = _history.source_guidance(paths, _guidance_project(tmp_path), entry)
    assert not any(line.startswith('git ') for line in lines)
    assert any('cannot' in line.lower() for line in lines)
    with patch.object(
        _source,
        'source_recovery',
        return_value=SourceRecovery('unavailable', None, 'missing object'),
    ):
        lines = _history.source_guidance(paths, _guidance_project(tmp_path), entry)
    assert not any(line.startswith('git ') for line in lines)
    assert any('missing object' in line for line in lines)


@pytest.mark.parametrize('name', ['source', 'source space', "source'quote", 'repo/.git'])
def test_source_guidance_quotes_exact_commands(tmp_path: Path, name: str) -> None:
    paths, entry = _literal_publication(tmp_path)
    source = tmp_path / name
    source.mkdir(parents=True)
    assert entry.record is not None
    assert entry.record.pinned is not None
    source_commit = entry.record.pinned.commit
    bookmark = SourceBookmark(source, source, 'refs/pubanon/publications/example/' + entry.commit)
    entry = replace(entry, record=replace(entry.record, source_bookmark=bookmark))
    recovery = SourceRecovery('protected', source, None)
    with (
        patch.object(_source, 'source_recovery', return_value=recovery),
        patch.object(_history, 'run_git', return_value=GitResult(0, b'', b'')),
    ):
        lines = _history.source_guidance(paths, _guidance_project(tmp_path), entry)
    commands = [line for line in lines if line.startswith('git ')]
    destination = paths.data_dir / 'projects/example/source-worktrees' / entry.commit
    assert [shlex.split(command) for command in commands] == [
        [
            'git',
            '-C',
            str(source),
            'worktree',
            'add',
            '--detach',
            str(destination),
            source_commit,
        ],
        ['git', '-C', str(source), 'worktree', 'remove', str(destination)],
    ]
    assert not destination.parent.exists()


def test_source_guidance_refuses_symlinked_worktree_parent(tmp_path: Path) -> None:
    paths, entry = _literal_publication(tmp_path)
    assert entry.record is not None
    source = tmp_path / 'source'
    source.mkdir()
    bookmark = SourceBookmark(source, source, 'refs/pubanon/publications/example/' + entry.commit)
    entry = replace(entry, record=replace(entry.record, source_bookmark=bookmark))
    parent = paths.data_dir / 'projects/example/source-worktrees'
    parent.parent.mkdir(parents=True, exist_ok=True)
    parent.symlink_to(tmp_path / 'elsewhere', target_is_directory=True)
    with (
        patch.object(
            _source, 'source_recovery', return_value=SourceRecovery('protected', source, None)
        ),
        patch.object(_history, 'run_git', return_value=GitResult(0, b'', b'')),
    ):
        lines = _history.source_guidance(paths, _guidance_project(tmp_path), entry)
    assert not any(line.startswith('git ') for line in lines)
    assert any('parent is occupied' in line for line in lines)
