import shutil
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Unpack

import pytest

from pubanon import _filesystem
from pubanon._errors import PubanonError
from pubanon._run import GitResult
from pubanon._snapshots import (
    _candidate,
    _discard,
    _history,
    _records,
    _source,
    _store,
)
from tests.snapshots.test_candidate import (
    _build,
    _configs,
    _git,
    _oracle_commit,
    _RunGitOptions,
)


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
    store = _store.store_path(paths, project.name)
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
        _discard.discard(paths, project.name, force=True)
    assert (
        _git(source, 'for-each-ref', '--format=%(objectname)', bookmark.refname).strip().decode()
        == pinned.commit
    )
    assert _records.load_candidate(paths, project.name) == record
    assert _records.load_rewrite(paths, project.name) == progress


def test_discard_deletes_symbolic_private_ref_without_changing_main(tmp_path: Path) -> None:
    paths, project, record = _build(tmp_path)
    store = _store.store_path(paths, project.name)
    _git(store, 'update-ref', 'refs/heads/main', record.commit)
    _git(store, 'symbolic-ref', _candidate.CANDIDATE_REF, 'refs/heads/main')

    assert _discard.discard(paths, project.name, force=False)

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

    _discard.discard(paths, project.name, force=True)

    assert not record_path.exists()
    assert (inspect / 'edited.txt').read_text() == 'dirty\n'
    store = _store.store_path(paths, project.name)
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
    store = _store.store_path(paths, project.name)
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
        _discard.discard(paths, project.name, force=True)
    assert _records.load_candidate(paths, project.name) == record


def test_discard_ignores_similarly_prefixed_archive_reference(tmp_path: Path) -> None:
    paths, project, record = _build(tmp_path)
    store = _store.store_path(paths, project.name)
    ref = _records.publication_ref(record.commit) + '/other'
    _git(store, 'update-ref', ref, record.commit)
    assert _discard.discard(paths, project.name, force=False)
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
        _discard.discard(paths, project_config.name, force=False)
    assert error.value.recovery == 'pubanon publish --resolve'
    assert (inspect / 'edited.txt').exists()

    _discard.discard(paths, project_config.name, force=True)
    warning = capsys.readouterr().out
    assert 'snapshot recovery information' in warning
    assert 'does not undo any possible GitHub publication' in warning
    assert _records.load_candidate(paths, project_config.name) is None
    assert not inspect.exists()
    store = _store.store_path(paths, project_config.name)
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
            _store.store_path(paths, project.name),
            'update-ref',
            '-d',
            _records.publication_ref(record.commit),
        )
    _discard.check_recovery_state(paths, project.name, force=force)
    with pytest.raises(PubanonError, match='confirmed publication'):
        _discard.discard(paths, project.name, force=force)
    assert _records.load_candidate(paths, project.name) == record


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
            _discard.discard(paths, project.name, force=force)
        assert _records.load_candidate(paths, project.name) == record
    else:
        assert _discard.discard(paths, project.name, force=force)
        assert _records.load_candidate(paths, project.name) is None
        assert _git(source, 'for-each-ref', '--format=%(refname)', bookmark.refname) == b''
        assert _git(source, 'rev-parse', 'HEAD').decode().strip() == original.commit


@pytest.mark.parametrize('force', [False, True])
def test_discard_reports_ref_failure_and_preserves_cleanup_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force: bool
) -> None:
    paths, project, record = _build(tmp_path)
    real_git = _discard.run_git

    def _fail(args: Sequence[str], **kwargs: Unpack[_RunGitOptions]) -> GitResult:
        if args[2] == 'update-ref':
            return GitResult(7, b'', b'diagnostic\x1b\\path')
        return real_git(args, **kwargs)

    monkeypatch.setattr(_discard, 'run_git', _fail)
    with pytest.raises(PubanonError) as error:
        _discard.discard(paths, project.name, force=force)
    expected = r'exit status 7: diagnostic\x1b\\path'
    assert error.value.fact == (f'saved snapshot reference: {expected}' if force else expected)
    assert _records.load_candidate(paths, project.name) == (None if force else record)
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
    store = _store.candidate_store_path(paths, project.name)
    _git(store, 'update-ref', _candidate.REWRITE_MERGED_REF, record.commit)

    def _refuse_cleanup(_path: Path) -> None:
        raise OSError('busy')

    def _refuse_bookmark(*_args: object) -> None:
        raise PubanonError('bookmark busy')

    with monkeypatch.context() as fault:
        if boundary == 'directory':
            fault.setattr(_filesystem, 'remove_tree', _refuse_cleanup)
        elif boundary == 'bookmark':
            fault.setattr(_discard, 'remove_bookmark', _refuse_bookmark)
        else:
            (store / (_candidate.REWRITE_MERGED_REF + '.lock')).touch()
        with pytest.raises(PubanonError) as error:
            _discard.discard(paths, project.name, force=False)
    assert _records.load_candidate(paths, project.name) == record
    assert _records.load_rewrite(paths, project.name) == progress
    if boundary == 'directory':
        assert (directory / 'resolution').read_text() == 'retained decision'
        for bookmark in (provisional, correction):
            assert _git(source, 'rev-parse', bookmark.refname).strip().decode() == pinned.commit
    assert 'retry discard' in error.value.render()
    if boundary == 'ref':
        (store / (_candidate.REWRITE_MERGED_REF + '.lock')).unlink()
    assert _discard.discard(paths, project.name, force=False)
    assert not _records.rewrite_path(paths, project.name).exists()
    assert not directory.exists()
    assert _git(source, 'rev-parse', original.refname).strip().decode() == pinned.commit
    for bookmark in (provisional, correction):
        assert _git(source, 'for-each-ref', '--format=%(objectname)', bookmark.refname) == b''


def test_discard_rewrite_deletes_symbolic_private_ref_without_changing_main(
    tmp_path: Path,
) -> None:
    paths, project, record = _build(tmp_path)
    store = _store.store_path(paths, project.name)
    _git(store, 'update-ref', 'refs/heads/main', record.commit)
    _git(store, 'symbolic-ref', _candidate.REWRITE_MERGED_REF, 'refs/heads/main')

    assert _discard._discard_rewrite(paths, project.name, ())

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
    assert _discard.discard(paths, project.name, force=False)
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
        _discard.discard(paths, project.name, force=False)

    assert record_path.exists()
    assert inspect.exists()
    store = _store.store_path(paths, project.name)
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
        _discard.discard(paths, project.name, force=True)

    assert 'pubanon repo restore --to local' in exc_info.value.render()
    assert not (paths.data_dir / 'projects' / project.name / 'candidate.toml').exists()
    if nested:
        assert _git(tmp_path, 'for-each-ref') == ancestor_refs
        assert (
            _git(tmp_path, 'cat-file', '--batch-all-objects', '--batch-check') == ancestor_objects
        )
