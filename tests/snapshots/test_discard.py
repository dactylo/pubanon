import os
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
    _inspection,
    _records,
    _rewrite,
    _source,
    _store,
)
from tests._git import RunGitOptions, git_bytes
from tests.snapshots.test_candidate import (
    build,
    build_oracle_commit,
    configs,
)
from tests.snapshots.test_rewrite import prepare


def test_discard_confirmed_rewrite_preserves_correction_bookmark(
    tmp_path: Path, make_repo: Callable[..., Path]
) -> None:
    source = make_repo(files={'README.md': 'source'})
    paths, project, record = build(tmp_path)
    pinned = _source.pin(source, 'HEAD')
    bookmark = _source.describe_bookmark(source, project.name, record.commit)
    _source.ensure_bookmark(bookmark, pinned.commit)
    record = replace(record, pinned=pinned, source_bookmark=bookmark)
    _records.save_candidate(paths, project.name, record)
    g, _ = configs(tmp_path)
    store = _store.store_path(paths, project.name)
    derived = build_oracle_commit(store, record.tree, g, 'derived release', 1_700_000_123)
    git_bytes(store, 'update-ref', _records.publication_ref(derived), derived)
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
        git_bytes(source, 'for-each-ref', '--format=%(objectname)', bookmark.refname)
        .strip()
        .decode()
        == pinned.commit
    )
    assert _records.load_candidate(paths, project.name) == record
    assert _records.load_rewrite(paths, project.name) == progress


def test_discard_deletes_symbolic_private_ref_without_changing_main(tmp_path: Path) -> None:
    paths, project, record = build(tmp_path)
    store = _store.store_path(paths, project.name)
    git_bytes(store, 'update-ref', 'refs/heads/main', record.commit)
    git_bytes(store, 'symbolic-ref', _candidate.CANDIDATE_REF, 'refs/heads/main')

    assert _discard.discard(paths, project.name, force=False)

    assert git_bytes(store, 'rev-parse', 'refs/heads/main').strip().decode() == record.commit
    assert git_bytes(store, 'for-each-ref', '--format=%(refname)', _candidate.CANDIDATE_REF) == b''


@pytest.mark.parametrize('corrupt', [b'', b'commit = "partial'])
def test_discard_force_removes_unreadable_candidate_state(
    corrupt: bytes,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, project, record = build(tmp_path)
    project_dir = paths.data_dir / 'projects' / project.name
    record_path = project_dir / 'candidate.toml'
    inspect = project_dir / 'inspections' / record.commit
    inspect.mkdir(parents=True)
    (inspect / 'edited.txt').write_text('dirty\n', encoding='utf-8')
    record_path.write_bytes(corrupt)

    _discard.discard(paths, project.name, force=True)

    assert capsys.readouterr().out.count('could not be verified from the malformed') == 1
    assert not record_path.exists()
    assert (inspect / 'edited.txt').read_text() == 'dirty\n'
    store = _store.store_path(paths, project.name)
    assert git_bytes(store, 'for-each-ref', '--format=%(refname)', _candidate.CANDIDATE_REF) == b''


@pytest.mark.parametrize('conflict', ['symbolic', 'wrong-object', 'provenance'])
def test_discard_force_retains_conflicting_confirmation_evidence(
    tmp_path: Path, conflict: str
) -> None:
    paths, project, record = build(tmp_path)
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
            git_bytes(store, 'symbolic-ref', ref, _candidate.CANDIDATE_REF)
        else:
            git_bytes(store, 'update-ref', ref, record.tree)
    else:
        record = replace(record, pinned=record.pinned._replace(commit='c' * 40))
        _candidate.save_candidate(paths, project.name, record)
    with pytest.raises(PubanonError):
        _discard.discard(paths, project.name, force=True)
    assert _records.load_candidate(paths, project.name) == record


def test_discard_ignores_similarly_prefixed_archive_reference(tmp_path: Path) -> None:
    paths, project, record = build(tmp_path)
    store = _store.store_path(paths, project.name)
    ref = _records.publication_ref(record.commit) + '/other'
    git_bytes(store, 'update-ref', ref, record.commit)
    assert _discard.discard(paths, project.name, force=False)
    assert git_bytes(store, 'rev-parse', ref).decode().strip() == record.commit


def test_discard_protects_unknown_outcome_until_forced(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths, project_config, record = build(tmp_path)
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
    assert git_bytes(store, 'for-each-ref', '--format=%(refname)', _candidate.CANDIDATE_REF) == b''


@pytest.mark.parametrize('force', [False, True])
@pytest.mark.parametrize('confirmed', ['record', 'archive'])
def test_discard_refuses_confirmed_publication(
    tmp_path: Path, force: bool, confirmed: str
) -> None:
    paths, project, record = build(tmp_path)
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
        git_bytes(
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
    paths, project, record = build(tmp_path)
    bookmark = _source.describe_bookmark(source, project.name, record.commit)
    original = _source.pin(source, 'HEAD')
    record = replace(record, pinned=original, source_bookmark=bookmark)
    _candidate.save_candidate(paths, project.name, record)
    _source.ensure_bookmark(bookmark, original.commit)
    if condition == 'missing':
        _source.remove_bookmark(bookmark, original.commit)
    elif condition == 'conflict':
        git_bytes(source, 'commit', '--allow-empty', '-qm', 'different')
        git_bytes(source, 'update-ref', bookmark.refname, 'HEAD')
    elif condition == 'inaccessible':
        source.rename(source.with_name('moved-source'))
    if condition in {'conflict', 'inaccessible'}:
        with pytest.raises(PubanonError, match='snapshot retained'):
            _discard.discard(paths, project.name, force=force)
        assert _records.load_candidate(paths, project.name) == record
    else:
        assert _discard.discard(paths, project.name, force=force)
        assert _records.load_candidate(paths, project.name) is None
        assert git_bytes(source, 'for-each-ref', '--format=%(refname)', bookmark.refname) == b''
        assert git_bytes(source, 'rev-parse', 'HEAD').decode().strip() == original.commit


@pytest.mark.parametrize('force', [False, True])
def test_discard_reports_ref_failure_and_preserves_cleanup_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, force: bool
) -> None:
    paths, project, record = build(tmp_path)
    real_git = _discard.run_git

    def _fail(args: Sequence[str], **kwargs: Unpack[RunGitOptions]) -> GitResult:
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
    paths, project, record = build(tmp_path)
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
    git_bytes(store, 'update-ref', _candidate.REWRITE_MERGED_REF, record.commit)

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
            assert (
                git_bytes(source, 'rev-parse', bookmark.refname).strip().decode() == pinned.commit
            )
    assert 'retry discard' in error.value.render()
    if boundary == 'ref':
        (store / (_candidate.REWRITE_MERGED_REF + '.lock')).unlink()
    assert _discard.discard(paths, project.name, force=False)
    assert not _records.rewrite_path(paths, project.name).exists()
    assert not directory.exists()
    assert git_bytes(source, 'rev-parse', original.refname).strip().decode() == pinned.commit
    for bookmark in (provisional, correction):
        assert git_bytes(source, 'for-each-ref', '--format=%(objectname)', bookmark.refname) == b''


def test_discard_rewrite_deletes_symbolic_private_ref_without_changing_main(
    tmp_path: Path,
) -> None:
    paths, project, record = build(tmp_path)
    store = _store.store_path(paths, project.name)
    git_bytes(store, 'update-ref', 'refs/heads/main', record.commit)
    git_bytes(store, 'symbolic-ref', _candidate.REWRITE_MERGED_REF, 'refs/heads/main')

    assert _discard._discard_rewrite(paths, project.name, ())

    assert git_bytes(store, 'rev-parse', 'refs/heads/main').strip().decode() == record.commit
    assert (
        git_bytes(store, 'for-each-ref', '--format=%(refname)', _candidate.REWRITE_MERGED_REF)
        == b''
    )


def test_discard_rewrite_releases_derived_bookmark_but_preserves_original(
    tmp_path: Path, make_repo: Callable[..., Path]
) -> None:
    source = make_repo(files={'README.md': 'source'})
    paths, project, record = build(tmp_path)
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
    assert (
        git_bytes(source, 'rev-parse', original_bookmark.refname).strip().decode()
        == original.commit
    )
    assert (
        git_bytes(source, 'for-each-ref', '--format=%(objectname)', derived_bookmark.refname)
        == b''
    )
    assert git_bytes(source, 'rev-parse', 'HEAD').strip().decode() == original.commit


def test_discard_without_force_preserves_unreadable_candidate_state(tmp_path: Path) -> None:
    paths, project, record = build(tmp_path)
    project_dir = paths.data_dir / 'projects' / project.name
    record_path = project_dir / 'candidate.toml'
    inspect = project_dir / 'inspections' / record.commit
    inspect.mkdir(parents=True)
    record_path.write_bytes(b'commit = "partial')

    with pytest.raises(PubanonError, match='invalid snapshot record') as exc_info:
        _discard.discard(paths, project.name, force=False)

    assert record_path.exists()
    assert inspect.exists()
    store = _store.store_path(paths, project.name)
    assert git_bytes(store, 'rev-parse', _candidate.CANDIDATE_REF).strip()

    assert 'pubanon discard --force' in (exc_info.value.recovery or '')


@pytest.mark.parametrize('nested', [False, True])
def test_force_discard_removes_record_with_broken_store(
    tmp_path: Path, make_repo: Callable[..., Path], *, nested: bool
) -> None:
    if nested:
        tmp_path = make_repo()
        git_bytes(tmp_path, 'update-ref', _candidate.CANDIDATE_REF, 'HEAD')
    ancestor_refs = git_bytes(tmp_path, 'for-each-ref') if nested else None
    ancestor_objects = (
        git_bytes(tmp_path, 'cat-file', '--batch-all-objects', '--batch-check') if nested else None
    )
    paths, project, _record = build(tmp_path)
    store = paths.data_dir / 'projects' / project.name / 'store.git'
    shutil.rmtree(store / 'refs')

    with pytest.raises(PubanonError) as exc_info:
        _discard.discard(paths, project.name, force=True)

    assert 'pubanon repo restore --to local' in exc_info.value.render()
    assert not (paths.data_dir / 'projects' / project.name / 'candidate.toml').exists()
    if nested:
        assert git_bytes(tmp_path, 'for-each-ref') == ancestor_refs
        assert (
            git_bytes(tmp_path, 'cat-file', '--batch-all-objects', '--batch-check')
            == ancestor_objects
        )


@pytest.mark.slow
def test_discard_removes_paused_rewrite_without_published_refs_or_inspection(
    tmp_path: Path,
) -> None:
    paths, g, project, old, correction = prepare(tmp_path, conflict=True)
    _rewrite.continue_rewrite(paths, g, project, env={})
    inspect = _inspection.materialize_snapshot(
        paths,
        project.name,
        correction.commit,
        _source.read_snapshot(_store.store_path(paths, project.name), correction.commit),
    )
    _discard.discard(paths, project.name, force=False)
    assert not _records.rewrite_path(paths, project.name).exists()
    assert not _records.rewrite_directory(paths, project.name).exists()
    assert not inspect.exists()
    assert _candidate.published_refs(paths, project.name)[0] == old[-1]
    assert (
        git_bytes(
            _store.store_path(paths, project.name),
            'for-each-ref',
            '--format=%(refname)',
            'refs/pubanon',
        ).strip()
        == b''
    )


@pytest.mark.slow
@pytest.mark.skipif(os.name == 'nt', reason='POSIX directory permissions')
def test_discard_removes_read_only_conflict_export(tmp_path: Path) -> None:
    paths, g, project, _, _ = prepare(tmp_path, conflict=True)
    _rewrite.continue_rewrite(paths, g, project, env={})
    directory = _records.rewrite_directory(paths, project.name)
    tree = directory / 'tree'
    _filesystem.chmod(tree / 'base.txt', 0o400)
    _filesystem.chmod(tree, 0o500)
    try:
        try:
            (tree / '.permission-probe').touch(exist_ok=False)
        except PermissionError:
            pass
        else:
            (tree / '.permission-probe').unlink()
            pytest.skip('filesystem does not enforce read-only directory permissions')
        assert _discard.discard(paths, project.name, force=False)
        assert not directory.exists()
        assert not _records.rewrite_path(paths, project.name).exists()
    finally:
        if directory.exists():
            _filesystem.remove_tree(directory)
