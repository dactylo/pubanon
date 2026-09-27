from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pubanon._config import GlobalConfig, Paths, ProjectConfig
from pubanon._errors import PubanonError
from pubanon._snapshots import _candidate, _records, _rewrite, _source
from pubanon._snapshots._candidate import CandidateInputs
from pubanon._snapshots._records import CandidateRecord
from pubanon._snapshots._source import Entry, read_snapshot
from tests.snapshots.test_candidate import _configs, _git, _paths, _pinned


def _history(
    tmp_path: Path, *, conflict: bool = False
) -> tuple[Paths, GlobalConfig, ProjectConfig, tuple[str, ...]]:
    paths = _paths(tmp_path)
    g, project = _configs(tmp_path)
    commits: list[str] = []
    for index in range(3):
        files = [Entry('base.txt', 0o100644, 'blob', b'base\n')]
        if index >= 1:
            files.append(Entry('second.txt', 0o100644, 'blob', b'second\n'))
        if index >= 2:
            files.append(Entry('third.txt', 0o100644, 'blob', b'third\n'))
            if conflict:
                files[0] = files[0]._replace(data=b'old edit\n')
        record = _candidate.build_candidate(
            paths, g, project, CandidateInputs(tuple(files), _pinned(), f'1.0.{index}', None)
        )
        _records.save_publication(
            paths,
            project.name,
            _records.PublicationRecord(
                record.commit,
                record.tree,
                record.snapshot_digest,
                record.fingerprint,
                record.message,
                record.tag_intent,
                record.pinned,
                record.destination,
                datetime(2026, 1, 1, tzinfo=UTC),
                _source.SourceBookmark(
                    project.source_path,
                    project.source_path / '.git',
                    _source.bookmark_ref(project.name, record.commit),
                ),
            ),
        )
        commits.append(record.commit)
        store = _candidate.store_path(paths, project.name)
        _git(store, 'update-ref', 'refs/heads/main', record.commit)
        _git(store, 'update-ref', f'refs/tags/v1.0.{index}', record.commit)
        _records.candidate_path(paths, project.name).unlink()
    return paths, g, project, tuple(commits)


def _install_faults(fault: pytest.MonkeyPatch, boundary: str) -> None:
    save_rewrite = _records.save_rewrite
    save_candidate = _records.save_candidate
    export = _rewrite._conflicts.export_conflicts

    def _interrupted_progress(
        save_paths: Paths, name: str, progress: _records.RewriteRecord
    ) -> None:
        if boundary == 'initial-record' or (
            boundary == 'advanced-ref' and len(progress.replayed) == 2
        ):
            raise RuntimeError('interrupted')
        if boundary == 'manifest-export' and progress.phase == 'awaiting':
            raise RuntimeError('interrupted')
        save_rewrite(save_paths, name, progress)
        if boundary == 'pause-record' and progress.phase == 'exporting':
            raise RuntimeError('interrupted')
        if boundary == 'resolved-entry' and len(progress.replayed) == 2:
            raise RuntimeError('interrupted')

    def _interrupted_export(store: Path, target: Path, pause: _records.RewritePause) -> None:
        if boundary == 'partial-export':
            target.mkdir(exist_ok=True)
            (target / 'partial').write_text('partial export')
            raise RuntimeError('interrupted')
        export(store, target, pause)

    def _interrupted_candidate(save_paths: Paths, name: str, record: CandidateRecord) -> None:
        save_candidate(save_paths, name, record)
        if boundary == 'complete-candidate':
            raise RuntimeError('interrupted')

    fault.setattr(_records, 'save_rewrite', _interrupted_progress)
    fault.setattr(_records, 'save_candidate', _interrupted_candidate)
    fault.setattr(_rewrite._conflicts, 'export_conflicts', _interrupted_export)


def _prepare(
    tmp_path: Path, *, conflict: bool = False, empty: bool = False
) -> tuple[Paths, GlobalConfig, ProjectConfig, tuple[str, ...], CandidateRecord]:
    paths, g, project, old = _history(tmp_path, conflict=conflict)
    files = [Entry('base.txt', 0o100644, 'blob', b'corrected\n')]
    if empty:
        files.append(Entry('second.txt', 0o100644, 'blob', b'second\n'))
    record = _candidate.build_candidate(
        paths,
        g,
        project,
        CandidateInputs(tuple(files), _pinned(), None, None, replaces=old[0], replace_scope='one'),
    )
    return paths, g, project, old, record


def _resolve(paths: Paths, project: ProjectConfig) -> None:
    directory = _records.rewrite_directory(paths, project.name)
    (directory / 'tree' / 'base.txt').write_bytes(b'resolved\n')
    manifest = directory / 'resolutions.toml'
    manifest.write_text(manifest.read_text().replace('action = "unresolved"', 'action = "keep"'))


def _run_to_completion(paths: Paths, g: GlobalConfig, project: ProjectConfig) -> None:
    _rewrite.continue_rewrite(paths, g, project, env={})
    _resolve(paths, project)
    _rewrite.continue_rewrite(paths, g, project, env={})


@pytest.mark.slow
def test_awaiting_refusal_preserves_edits_and_progress(tmp_path: Path) -> None:
    paths, g, project, _, _ = _prepare(tmp_path, conflict=True)
    _rewrite.continue_rewrite(paths, g, project, env={})
    directory = _records.rewrite_directory(paths, project.name)
    (directory / 'tree' / 'base.txt').write_bytes(b'unfinished edit\n')
    saved = _records.rewrite_path(paths, project.name).read_bytes()
    with pytest.raises(PubanonError, match='unresolved'):
        _rewrite.continue_rewrite(paths, g, project, env={})
    assert (directory / 'tree' / 'base.txt').read_bytes() == b'unfinished edit\n'
    assert _records.rewrite_path(paths, project.name).read_bytes() == saved


def test_clear_pause_deletes_symbolic_private_ref_without_changing_main(tmp_path: Path) -> None:
    paths, _, project, commits = _history(tmp_path)
    store = _candidate.store_path(paths, project.name)
    _git(store, 'symbolic-ref', _candidate.REWRITE_MERGED_REF, 'refs/heads/main')

    _rewrite._clear_pause(paths, project.name, store)

    assert _git(store, 'rev-parse', 'refs/heads/main').strip().decode() == commits[-1]
    assert _git(store, 'for-each-ref', '--format=%(refname)', _candidate.REWRITE_MERGED_REF) == b''


@pytest.mark.slow
def test_completion_gate_refusal_retains_complete_provenance(tmp_path: Path) -> None:
    paths, g, project, _, correction = _prepare(tmp_path, conflict=True)
    _rewrite.continue_rewrite(paths, g, project, env={})
    _resolve(paths, project)
    directory = _records.rewrite_directory(paths, project.name)
    (directory / 'tree' / 'base.txt').write_bytes(b'Private Person\n')
    with pytest.raises(PubanonError, match='did not pass current content checks'):
        _rewrite.continue_rewrite(paths, g, project, env={})
    complete = _records.load_candidate(paths, project.name)
    assert complete is not None
    assert complete.commit == correction.commit
    assert complete.tip is not None
    assert complete.replayed[-1].resolutions
    assert _records.load_rewrite(paths, project.name) is not None
    assert _candidate.discard(paths, project.name, force=False)


@pytest.mark.slow
def test_discard_removes_paused_rewrite_without_published_refs_or_inspection(
    tmp_path: Path,
) -> None:
    paths, g, project, old, correction = _prepare(tmp_path, conflict=True)
    _rewrite.continue_rewrite(paths, g, project, env={})
    inspect = _candidate.materialize_snapshot(
        paths,
        project.name,
        correction.commit,
        read_snapshot(_candidate.store_path(paths, project.name), correction.commit),
    )
    _candidate.discard(paths, project.name, force=False)
    assert not _records.rewrite_path(paths, project.name).exists()
    assert not _records.rewrite_directory(paths, project.name).exists()
    assert not inspect.exists()
    assert _candidate.published_refs(paths, project.name)[0] == old[-1]
    assert (
        _git(
            _candidate.store_path(paths, project.name),
            'for-each-ref',
            '--format=%(refname)',
            'refs/pubanon',
        ).strip()
        == b''
    )


@pytest.mark.slow
@pytest.mark.parametrize(
    'boundary',
    [
        'initial-record',
        'advanced-ref',
        'pause-record',
        'partial-export',
        'manifest-export',
        'resolved-entry',
        'complete-candidate',
    ],
)
def test_every_durable_boundary_resumes_without_losing_decisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    paths, g, project, old, correction = _prepare(tmp_path, conflict=True)
    directory = _records.rewrite_directory(paths, project.name)
    with monkeypatch.context() as fault:
        _install_faults(fault, boundary)
        with pytest.raises(RuntimeError, match='interrupted'):
            _run_to_completion(paths, g, project)
    if boundary in {'advanced-ref', 'resolved-entry'}:
        assert (directory / 'tree' / 'base.txt').read_bytes() == b'resolved\n'
    if boundary == 'pause-record':
        store = _candidate.store_path(paths, project.name)
        _git(store, 'reflog', 'expire', '--expire=now', '--all')
        _git(store, 'gc', '--prune=now')
    if boundary == 'initial-record':
        store = _candidate.store_path(paths, project.name)
        _git(store, 'update-ref', '-d', _candidate.CANDIDATE_REF, correction.commit)
    if boundary in {'partial-export', 'manifest-export'}:
        # Exporting never treats these apparent user decisions as resolutions.
        if (directory / 'resolutions.toml').exists():
            _resolve(paths, project)
        else:
            (directory / 'resolutions.toml').write_text('invented decisions')
    result = _rewrite.continue_rewrite(paths, g, project, env={})
    if result.tip is None:
        if boundary in {'partial-export', 'manifest-export'}:
            assert 'unresolved' in (directory / 'resolutions.toml').read_text()
        _resolve(paths, project)
        result = _rewrite.continue_rewrite(paths, g, project, env={})
    assert tuple(entry.old for entry in result.replayed) == old[1:]
    assert len(result.replayed[-1].resolutions) == 1
    assert result.replayed[-1].resolutions[0].action == 'keep'
    assert not directory.exists()
    assert not _records.rewrite_path(paths, project.name).exists()
    assert _records.load_candidate(paths, project.name) == result


@pytest.mark.slow
def test_completed_rewrite_cleanup_failure_preserves_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, g, project, _, _ = _prepare(tmp_path)
    with monkeypatch.context() as fault:
        _install_faults(fault, 'complete-candidate')
        with pytest.raises(RuntimeError, match='interrupted'):
            _rewrite.continue_rewrite(paths, g, project, env={})
    completed = _records.load_candidate(paths, project.name)
    progress = _records.load_rewrite(paths, project.name)
    assert completed is not None
    assert completed.tip is not None
    assert progress is not None
    unlink = Path.unlink

    def _fail_remove(path: Path, *, missing_ok: bool = False) -> None:
        if path == _records.rewrite_path(paths, project.name):
            raise OSError('interrupted completed rewrite cleanup')
        unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as fault:
        fault.setattr(Path, 'unlink', _fail_remove)
        with pytest.raises(PubanonError, match='cannot clean completed rewrite record') as error:
            _rewrite.continue_rewrite(paths, g, project, env={})
    assert error.value.recovery == 'pubanon prepare --continue'
    assert isinstance(error.value.__cause__, OSError)
    assert _records.load_candidate(paths, project.name) == completed
    assert _records.load_rewrite(paths, project.name) == progress
    assert _rewrite.continue_rewrite(paths, g, project, env={}) == completed
    assert _records.load_rewrite(paths, project.name) is None


@pytest.mark.parametrize('scope', ['one', 'all'])
def test_latest_and_all_are_complete_without_replay(tmp_path: Path, scope: str) -> None:
    paths, g, project, old = _history(tmp_path)
    selected, resolved_scope = _rewrite.resolve_replacement(
        paths, project.name, 'latest' if scope == 'one' else 'all'
    )
    record = _candidate.build_candidate(
        paths,
        g,
        project,
        CandidateInputs(
            (Entry('corrected.txt', 0o100644, 'blob', b'corrected\n'),),
            _pinned(),
            None,
            None,
            replaces=selected,
            replace_scope=resolved_scope,
        ),
    )
    assert record.tip == record.commit
    assert not record.replayed
    assert _records.load_rewrite(paths, project.name) is None
    if scope == 'one':
        assert record.poststate_tags == {
            'v1.0.0': old[0],
            'v1.0.1': old[1],
            'v1.0.2': record.commit,
        }
    else:
        assert record.poststate_tags == {}
    _candidate.verify_retained_candidate(paths, g, project, record, env={})
    with pytest.raises(PubanonError, match='no rewrite'):
        _rewrite.continue_rewrite(paths, g, project, env={})


@pytest.mark.slow
def test_pause_retains_partial_chain_through_gc_and_preserves_edits(tmp_path: Path) -> None:
    paths, g, project, old, correction = _prepare(tmp_path, conflict=True)
    assert _rewrite.continue_rewrite(paths, g, project, env={}) == correction
    progress = _records.load_rewrite(paths, project.name)
    assert progress is not None
    assert progress.pause is not None
    assert len(progress.replayed) == 1
    store = _candidate.store_path(paths, project.name)
    _git(store, 'reflog', 'expire', '--expire=now', '--all')
    _git(store, 'gc', '--prune=now')
    _resolve(paths, project)
    result = _rewrite.continue_rewrite(paths, g, project, env={})
    assert tuple(entry.old for entry in result.replayed) == old[1:]
    assert result.replayed[0] == progress.replayed[0]
    assert result.replayed[-1].resolutions == (_records.Resolution(('base.txt',), 'keep'),)
    assert result.tip is not None
    assert {entry.path: entry.data for entry in read_snapshot(store, result.tip)}[
        'base.txt'
    ] == b'resolved\n'
    assert not _records.rewrite_directory(paths, project.name).exists()


def test_replay_configuration_change_refuses_before_merge(tmp_path: Path) -> None:
    paths, g, project, _, _ = _prepare(tmp_path)
    with pytest.raises(PubanonError, match='configuration changed'):
        _rewrite.continue_rewrite(paths, replace(g, anon_name='Other Publisher'), project, env={})


@pytest.mark.slow
def test_replay_preserves_each_release_including_empty_descendant(tmp_path: Path) -> None:
    paths, g, project, old, correction = _prepare(tmp_path, empty=True)
    store = _candidate.store_path(paths, project.name)
    result = _rewrite.continue_rewrite(paths, g, project, env={})
    assert tuple(entry.old for entry in result.replayed) == old[1:]
    assert result.tip is not None
    assert result.tip == result.replayed[-1].new
    assert all(entry.original_source == _pinned().commit for entry in result.replayed)
    assert all(
        entry.original_bookmark
        == _source.SourceBookmark(
            project.source_path,
            project.source_path / '.git',
            _source.bookmark_ref(project.name, entry.old),
        )
        for entry in result.replayed
    )
    assert all(entry.source_bookmark is None for entry in result.replayed)
    assert result.poststate_tags == dict(
        zip(
            ('v1.0.0', 'v1.0.1', 'v1.0.2'),
            (correction.commit, *(entry.new for entry in result.replayed)),
            strict=True,
        )
    )
    previous = correction.commit
    for entry in result.replayed:
        assert _git(store, 'rev-parse', entry.new + '^').strip().decode() == previous
        assert _git(store, 'show', '-s', '--format=%B%aI', entry.new) == _git(
            store, 'show', '-s', '--format=%B%aI', entry.old
        )
        previous = entry.new
    assert read_snapshot(store, correction.commit) == read_snapshot(store, result.replayed[0].new)
    assert {item.path: item.data for item in read_snapshot(store, result.tip)} == {
        'base.txt': b'corrected\n',
        'second.txt': b'second\n',
        'third.txt': b'third\n',
    }
    assert _candidate.published_refs(paths, project.name) == (
        old[-1],
        {f'v1.0.{index}': oid for index, oid in enumerate(old)},
    )
    assert _records.load_rewrite(paths, project.name) is None
    assert _records.load_candidate(paths, project.name) == result


@pytest.mark.slow
def test_replay_retains_original_locator_without_reading_or_reusing_it(
    tmp_path: Path, make_repo: Callable[..., Path]
) -> None:
    source = make_repo(files={'README.md': 'original source'})
    paths, g, project, old, _ = _prepare(tmp_path)
    pinned = _source.pin(source, 'HEAD')
    bookmark = _source.describe_bookmark(source, project.name, old[1])
    _source.ensure_bookmark(bookmark, pinned.commit)
    publication = _records.load_publication(paths, project.name, old[1])
    assert publication is not None
    _records.publication_path(paths, project.name, old[1]).unlink()
    _records.save_publication(
        paths, project.name, replace(publication, pinned=pinned, source_bookmark=bookmark)
    )
    moved = source.with_name('moved-original')
    source.rename(moved)
    result = _rewrite.continue_rewrite(paths, g, project, env={})
    entry = result.replayed[0]
    assert entry.original_source == pinned.commit
    assert entry.original_bookmark == bookmark
    assert entry.source_bookmark is None
    assert (
        _git(moved, 'for-each-ref', '--format=%(refname)', 'refs/pubanon')
        == (bookmark.refname + '\n').encode()
    )


def test_resolve_replacement_preserves_ambiguous_prefix_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, _, project, old = _history(tmp_path)
    entries = (
        _rewrite._history.PublicationEntry(old[0], (), None, None),
        _rewrite._history.PublicationEntry(old[0][0] + '0' * 39, (), None, None),
    )

    def _publications(
        _paths: Paths, _project: str, **_options: bool
    ) -> tuple[_rewrite._history.PublicationEntry, ...]:
        return entries

    store = _candidate.store_path(paths, project.name)
    refs = _git(store, 'for-each-ref', '--format=%(refname) %(objectname)')
    source_entries = tuple(project.source_path.iterdir())
    monkeypatch.setattr(_rewrite._history, 'publications', _publications)
    with pytest.raises(PubanonError, match='ambiguous') as error:
        _rewrite.resolve_replacement(paths, project.name, old[0][0])
    assert error.value.fact is not None
    assert all(entry.commit in error.value.fact for entry in entries)
    assert _git(store, 'for-each-ref', '--format=%(refname) %(objectname)') == refs
    assert tuple(project.source_path.iterdir()) == source_entries
    assert not _records.candidate_path(paths, project.name).exists()
    assert not _records.rewrite_path(paths, project.name).exists()


def test_resolve_replacement_rejects_unknown_and_archived_nonancestor(tmp_path: Path) -> None:
    paths, _, project, old = _history(tmp_path)
    store = _candidate.store_path(paths, project.name)
    refs = _git(store, 'for-each-ref', '--format=%(refname) %(objectname)')
    source_entries = tuple(project.source_path.iterdir())
    with pytest.raises(PubanonError, match='unknown') as error:
        _rewrite.resolve_replacement(paths, project.name, 'unknown-selector')
    assert error.value.fact == 'unknown-selector'
    assert _git(store, 'for-each-ref', '--format=%(refname) %(objectname)') == refs
    _git(store, 'update-ref', 'refs/heads/main', old[0], old[-1])
    _git(store, 'update-ref', _records.publication_ref(old[-1]), old[-1])
    assert _rewrite._history.select_publication(paths, project.name, old[-1]).commit == old[-1]
    archived_refs = _git(store, 'for-each-ref', '--format=%(refname) %(objectname)')
    with pytest.raises(PubanonError):
        _rewrite.resolve_replacement(paths, project.name, old[-1])
    assert _git(store, 'for-each-ref', '--format=%(refname) %(objectname)') == archived_refs
    assert tuple(project.source_path.iterdir()) == source_entries
    assert not _records.candidate_path(paths, project.name).exists()
    assert not _records.rewrite_path(paths, project.name).exists()


@pytest.mark.parametrize(
    ('selector', 'index', 'scope'), [('latest', 2, 'one'), ('v1.0.1', 1, 'one'), ('all', 2, 'all')]
)
def test_resolve_replacement_uses_current_local_history(
    tmp_path: Path, selector: str, index: int, scope: str
) -> None:
    paths, _, project, old = _history(tmp_path)
    assert _rewrite.resolve_replacement(paths, project.name, selector) == (old[index], scope)
    assert _rewrite.resolve_replacement(paths, project.name, old[index][:12]) == (
        old[index],
        'one',
    )


def test_resume_rejects_conflicting_frozen_sequence(tmp_path: Path) -> None:
    paths, g, project, old, correction = _prepare(tmp_path)
    _records.save_rewrite(
        paths,
        project.name,
        _records.RewriteRecord(correction.commit, old[1], old[-1], (old[-1],), 'replaying'),
    )
    with pytest.raises(PubanonError, match='conflicts with retained'):
        _rewrite.continue_rewrite(paths, g, project, env={})


def test_update_ref_replaces_symbolic_private_ref_without_changing_main(tmp_path: Path) -> None:
    paths, _, project, commits = _history(tmp_path)
    store = _candidate.store_path(paths, project.name)
    _git(store, 'update-ref', 'refs/heads/main', commits[0], commits[-1])
    _git(store, 'symbolic-ref', _candidate.CANDIDATE_REF, 'refs/heads/main')

    _rewrite._update_ref(store, _candidate.CANDIDATE_REF, commits[1], commits[0])

    assert _git(store, 'rev-parse', 'refs/heads/main').strip().decode() == commits[0]
    assert _git(store, 'rev-parse', _candidate.CANDIDATE_REF).strip().decode() == commits[1]
    assert _git(store, 'for-each-ref', '--format=%(symref)', _candidate.CANDIDATE_REF) == b'\n'
