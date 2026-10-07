import os
import tomllib
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import TracebackType
from typing import Self
from unittest.mock import patch

import pytest

from pubanon import _filesystem
from pubanon._config import Paths
from pubanon._errors import PubanonError
from pubanon._snapshots import _records
from pubanon._snapshots._source import PinnedSource, SourceBookmark
from pubanon._terminal import escape
from tests._permissions import assert_private_mode

_DIGEST = 'd' * 64
_FINGERPRINT = 'e' * 64
_OID = 'a' * 40
_RECORD_MUTATIONS: tuple[Callable[[str], str], ...] = (
    lambda text: text + '\n[unknown]\nvalue = "value"\n',
    lambda text: text.replace('selector_kind = "ref"', 'selector_kind = "explicit"'),
    lambda _text: 'commit = [\n',
)
_SOURCE = 'c' * 40
_TREE = 'b' * 64


class _FailingScandir:
    def __init__(self, first: os.DirEntry[str]) -> None:
        self._first = first

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> os.DirEntry[str]:
        if self._first is not None:
            first = self._first
            self._first = None
            return first
        raise PermissionError('injected iteration failure')


def _candidate(root: Path, *, bookmark: bool = False) -> _records.CandidateRecord:
    return _records.CandidateRecord(
        _OID,
        _TREE,
        _DIGEST,
        'v1.2.3',
        _FINGERPRINT,
        PinnedSource(_SOURCE, 'ref', 'refs/heads/main', _SOURCE),
        _records.RecordedDestination('github', 'owner', 'repo', 'public', 'account'),
        None,
        {'v1.0.0': _SOURCE},
        'retained',
        'release message',
        SourceBookmark(
            root / 'source', root / 'source' / '.git', f'refs/pubanon/publications/example/{_OID}'
        )
        if bookmark
        else None,
    )


def _paths(root: Path) -> Paths:
    return Paths(
        root / 'config',
        root / 'data',
        root / 'config' / 'config.toml',
        root / 'data' / 'registry.json',
    )


def _pause() -> _records.RewritePause:
    return _records.RewritePause(
        '1' * 40,
        _TREE,
        (
            _records.ConflictEntry(
                ('directory', 'other'),
                'CONFLICT (file/directory)',
                'diagnostic message',
                (
                    _records.ConflictStage('directory/file', 0o100644, _SOURCE, 1),
                    _records.ConflictStage('other', 0o120000, _OID, 3),
                ),
                ('directory/file', 'other'),
            ),
        ),
    )


def _publication(root: Path, *, bookmark: bool = True) -> _records.PublicationRecord:
    candidate = _candidate(root, bookmark=bookmark)
    return _records.PublicationRecord(
        candidate.commit,
        candidate.tree,
        candidate.snapshot_digest,
        candidate.fingerprint,
        candidate.message,
        candidate.tag_intent,
        candidate.pinned,
        candidate.destination,
        datetime(2026, 9, 16, 12, 34, 56, tzinfo=UTC),
        candidate.source_bookmark,
    )


def _replay(root: Path) -> _records.ReplayEntry:
    old, new = '1' * 40, '2' * 40
    return _records.ReplayEntry(
        old,
        new,
        _SOURCE,
        SourceBookmark(
            root / 'old', root / 'old/.git', f'refs/pubanon/publications/example/{old}'
        ),
        SourceBookmark(
            root / 'new', root / 'new/.git', f'refs/pubanon/publications/example/{new}'
        ),
        (
            _records.Resolution(('readme.md',), 'keep', None),
            _records.Resolution(('obsolete',), 'delete', None),
            _records.Resolution(('old-name',), 'rename', 'new-name'),
        ),
    )


def _replacement(root: Path) -> _records.CandidateRecord:
    entry = _replay(root)
    return replace(
        _candidate(root),
        replaces=_SOURCE,
        replace_scope='one',
        prestate_main=entry.old,
        prestate_tags={'v1.0.0': _SOURCE, 'v1.1.0': _SOURCE, 'v1.2.3': entry.old},
        tip=entry.new,
        poststate_tags={'v1.0.0': _OID, 'v1.1.0': _OID, 'v1.2.3': entry.new},
        replayed=(entry,),
    )


def _rewrite(root: Path) -> _records.RewriteRecord:
    entry = _replay(root)
    return _records.RewriteRecord(_OID, _SOURCE, entry.old, (entry.old,), 'replaying', (), None)


def test_absent_history_does_not_create_directory(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    assert _records.deleted_publications(paths, 'example') == set()
    assert _records.list_publications(paths, 'example') == ()
    assert _records.load_publication(paths, 'example', _OID) is None
    assert not paths.data_dir.exists()


def test_candidate_bookmark_round_trip(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    record = _candidate(tmp_path, bookmark=True)
    _records.save_candidate(paths, 'example', record)
    assert _records.load_candidate(paths, 'example') == record
    assert set(
        tomllib.loads(_records.candidate_path(paths, 'example').read_text())['source_bookmark']
    ) == {'source_path', 'git_common_dir', 'refname'}


@pytest.mark.parametrize(
    ('before', 'after'),
    [
        ('replace_scope = "one"\n', ''),
        ('tip = "' + '2' * 40 + '"\n', ''),
        ('[[replayed]]', '[[replayed]]\nextra = 1'),
        ('[replayed.original_bookmark]', '[replayed.original_bookmark]\nextra = 1'),
        ('[replayed.source_bookmark]', '[replayed.source_bookmark]\nextra = 1'),
        ('[[replayed.resolutions]]', '[[replayed.resolutions]]\nextra = 1'),
        ('paths = ["readme.md"]', 'paths = "readme.md"'),
        ('paths = ["readme.md"]', 'paths = [1]'),
        ('action = "keep"', 'action = "unresolved"'),
        ('[poststate_tags]', '[poststate_tags]\nwrong = []'),
    ],
)
def test_candidate_load_rejects_malformed_replacement_tables(
    tmp_path: Path, before: str, after: str
) -> None:
    paths = _paths(tmp_path)
    _records.save_candidate(paths, 'example', _replacement(tmp_path))
    path = _records.candidate_path(paths, 'example')
    text = path.read_text()
    assert before in text
    path.write_text(text.replace(before, after, 1))
    with pytest.raises(PubanonError):
        _records.load_candidate(paths, 'example')


def test_candidate_poststate_preserves_ordinary_rule(tmp_path: Path) -> None:
    record = _candidate(tmp_path)
    assert _records.candidate_tip(record) == _OID
    tip, tags = _records.candidate_poststate(record)
    assert (tip, tags) == (_OID, {'v1.0.0': _SOURCE, 'v1.2.3': _OID})
    tags.clear()
    assert record.prestate_tags == {'v1.0.0': _SOURCE}
    assert _records.candidate_poststate(replace(record, tag_intent=None)) == (
        _OID,
        {'v1.0.0': _SOURCE},
    )


def test_candidate_record_read_uses_explicit_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    record = _candidate(tmp_path)
    _records.save_candidate(paths, 'example', record)
    record_path = paths.data_dir / 'projects' / 'example' / 'candidate.toml'
    original_read_text = Path.read_text

    def _require_utf8_read(
        self: Path,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> str:
        if self == record_path:
            assert encoding == 'utf-8'
        return original_read_text(self, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(Path, 'read_text', _require_utf8_read)

    assert _records.load_candidate(paths, 'example') == record


@pytest.mark.parametrize('field', ['owner', 'repo'])
@pytest.mark.parametrize(
    'value',
    ['', 'two/segments', 'repo?query', 'repo#fragment', 'repo\x01control', ' padded '],
)
def test_candidate_record_refuses_unsafe_recorded_host_segments(
    field: str,
    value: str,
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    record = _candidate(tmp_path)
    _records.save_candidate(paths, 'example', record)
    destination = replace(record.destination, **{field: value})

    with pytest.raises(PubanonError, match='invalid snapshot record'):
        _records.save_candidate(
            paths,
            'example',
            replace(record, destination=destination),
        )


def test_candidate_record_round_trips_non_bmp_message_and_ref(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    record = _candidate(tmp_path)
    _records.save_candidate(paths, 'example', record)
    non_bmp_record = replace(
        record,
        message='public release \U0001f680',
        pinned=PinnedSource(
            record.pinned.commit,
            'ref',
            'refs/heads/release-\U0001f680',
            record.pinned.selector_oid,
        ),
    )

    _records.save_candidate(paths, 'example', non_bmp_record)

    assert _records.load_candidate(paths, 'example') == non_bmp_record


@pytest.mark.parametrize(
    'resolution',
    [
        (('file',), 'unresolved', None),
        (('../file',), 'keep', None),
        (('/file',), 'keep', None),
        (('.git/config',), 'delete', None),
        (('a//b',), 'keep', None),
        (('a', 'b'), 'keep', None),
        (('file',), 'rename', None),
        (('file',), 'rename', '../out'),
        (('file',), 'keep', 'another'),
        ((), 'delete', None),
    ],
)
def test_completed_provenance_refuses_invalid_resolution(
    tmp_path: Path, resolution: tuple[tuple[str, ...], str, str | None]
) -> None:
    entry = replace(
        _replay(tmp_path),
        resolutions=(
            replace(
                _records.Resolution(('file',), 'keep'),
                paths=resolution[0],
                action=resolution[1],
                new_path=resolution[2],
            ),
        ),
    )
    record = replace(_replacement(tmp_path), replayed=(entry,))
    with pytest.raises(PubanonError):
        _records.save_candidate(_paths(tmp_path), 'example', record)


@pytest.mark.parametrize('source_known', [False, True])
@pytest.mark.parametrize('bookmarked', [False, True])
def test_derived_publication_round_trip_and_first_confirmation(
    tmp_path: Path, source_known: bool, bookmarked: bool
) -> None:
    entry = _replay(tmp_path)
    record = replace(
        _publication(tmp_path),
        commit=entry.new,
        pinned=None,
        source_bookmark=entry.source_bookmark if source_known and bookmarked else None,
        derivation=_records.Derivation(
            entry.old, _SOURCE if source_known else None, _OID, entry.resolutions
        ),
    )
    paths = _paths(tmp_path)
    _records.save_publication(paths, 'example', record)
    assert _records.load_publication(paths, 'example', entry.new) == record
    data = tomllib.loads(_records.publication_path(paths, 'example', entry.new).read_text())
    assert 'pinned' not in data
    assert data['derivation']['original'] == entry.old
    _records.save_publication(
        paths, 'example', replace(record, confirmed_at=record.confirmed_at + timedelta(days=1))
    )
    assert _records.load_publication(paths, 'example', entry.new) == record


def test_existing_record_sync_failure_preserves_valid_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    record = _candidate(tmp_path)
    _records.save_candidate(paths, 'example', record)
    record_path = paths.data_dir / 'projects' / 'example' / 'candidate.toml'

    def _fail_record_sync(_descriptor: int) -> None:
        raise OSError('sync failure')

    monkeypatch.setattr(os, 'fsync', _fail_record_sync)

    with pytest.raises(PubanonError, match='cannot save snapshot record'):
        _records.save_candidate(
            paths,
            'example',
            replace(record, state=_records.STATE_UNKNOWN_OUTCOME),
        )

    assert record_path.exists()
    assert _records.load_candidate(paths, 'example') == record


def test_failed_replace_preserves_complete_candidate(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    record = _candidate(tmp_path)
    _records.save_candidate(paths, 'example', record)
    path = _records.candidate_path(paths, 'example')
    original = path.read_bytes()
    changed = replace(record, message='changed')
    with (
        patch.object(_filesystem, 'replace', side_effect=OSError('injected')),
        pytest.raises(PubanonError),
    ):
        _records.save_candidate(paths, 'example', changed)
    assert path.read_bytes() == original
    assert _records.load_candidate(paths, 'example') == record
    _records.save_candidate(paths, 'example', changed)
    assert _records.load_candidate(paths, 'example') == changed
    assert not list(path.parent.glob('*.tmp'))


def test_incomplete_replacement_round_trip_refuses_publishable_poststate(tmp_path: Path) -> None:
    record = replace(_replacement(tmp_path), tip=None, poststate_tags=None, replayed=())
    _records.save_candidate(_paths(tmp_path), 'example', record)
    assert _records.load_candidate(_paths(tmp_path), 'example') == record
    for operation in (_records.candidate_tip, _records.candidate_poststate):
        with pytest.raises(PubanonError, match='incomplete'):
            operation(record)


def test_list_publications_refuses_malformed_and_disappearing_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    _records.save_publication(paths, 'example', _publication(tmp_path))
    path = _records.publication_path(paths, 'example', _OID)
    path.write_text('malformed = [')
    with pytest.raises(PubanonError, match='invalid published snapshot record'):
        _records.list_publications(paths, 'example')

    path.unlink()
    _records.save_publication(paths, 'example', _publication(tmp_path))
    real_load = _records.load_publication

    def disappear(
        load_paths: Paths, load_project: str, commit: str
    ) -> _records.PublicationRecord | None:
        path.unlink()
        return real_load(load_paths, load_project, commit)

    monkeypatch.setattr(_records, 'load_publication', disappear)
    with pytest.raises(PubanonError, match='disappeared while listing'):
        _records.list_publications(paths, 'example')


def test_listing_invalid_publication_filename_reports_retained_path(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    path = paths.data_dir / 'projects' / 'example' / 'publications' / 'not-an-oid.toml'
    path.parent.mkdir(parents=True)
    path.write_text('malformed record')

    with pytest.raises(PubanonError, match='invalid published snapshot commit ID') as error:
        _records.list_publications(paths, 'example')

    assert error.value.fact == escape((path).as_posix())
    assert path.read_text() == 'malformed record'


def test_load_candidate_refuses_escaping_project_name(tmp_path: Path) -> None:
    with pytest.raises(PubanonError, match='invalid project name'):
        _records.load_candidate(_paths(tmp_path), '../outside')


@pytest.mark.parametrize(
    'mutate',
    _RECORD_MUTATIONS,
)
def test_load_candidate_refuses_malformed_unknown_or_ambiguous_state(
    tmp_path: Path,
    mutate: Callable[[str], str],
) -> None:
    paths = _paths(tmp_path)
    _records.save_candidate(paths, 'example', _candidate(tmp_path))
    record_path = paths.data_dir / 'projects' / 'example' / 'candidate.toml'
    record_path.write_text(mutate(record_path.read_text()))

    with pytest.raises(PubanonError, match='invalid snapshot record'):
        _records.load_candidate(paths, 'example')


@pytest.mark.parametrize('source_known', [False, True])
def test_multiple_replays_preserve_order_and_unavailable_source_locators(
    tmp_path: Path, source_known: bool
) -> None:
    paths = _paths(tmp_path)
    first = _replay(tmp_path)
    second = _records.ReplayEntry('3' * 40, '4' * 40, _SOURCE if source_known else None)
    record = replace(
        _replacement(tmp_path),
        prestate_main=second.old,
        prestate_tags={'before': '5' * 40, 'latest': second.old},
        tag_intent='v2.0.0',
        tip=second.new,
        poststate_tags={'before': '5' * 40, 'latest': second.new, 'v2.0.0': _OID},
        replayed=(first, second),
    )
    _records.save_candidate(paths, 'example', record)
    assert _records.load_candidate(paths, 'example') == record
    assert _records.candidate_poststate(record) == (
        second.new,
        {'before': '5' * 40, 'latest': second.new, 'v2.0.0': _OID},
    )
    rewrite = replace(
        _rewrite(tmp_path),
        prestate_main=second.old,
        descendants=(first.old, second.old),
        replayed=(first,),
        phase='exporting',
        pause=replace(_pause(), old=second.old),
    )
    _records.save_rewrite(paths, 'example', rewrite)
    assert _records.load_rewrite(paths, 'example') == rewrite


@pytest.mark.parametrize(
    'change',
    [
        {'tip': _OID},
        {'poststate_tags': {}},
        {'replace_scope': 'one'},
    ],
)
def test_ordinary_candidate_refuses_orphan_replacement_fields(
    tmp_path: Path, change: dict[str, object]
) -> None:
    with pytest.raises(PubanonError):
        _records.save_candidate(
            _paths(tmp_path), 'example', replace(_candidate(tmp_path), **change)
        )


def test_publication_inventory_ignores_unrelated_files(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    directory = _records.publication_path(paths, 'example', _OID).parent
    directory.mkdir(parents=True)
    (directory / 'notes.txt').write_text('unrelated')
    (directory / f'{_OID}.toml.tmp').write_text('temporary')
    (directory / f'{_OID}.deleted.tmp').write_text('temporary')

    assert _records.deleted_publications(paths, 'example') == set()
    assert _records.list_publications(paths, 'example') == ()


def test_publication_inventory_preserves_platform_suffix_matching(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    record = _publication(tmp_path)
    _records.save_publication(paths, 'example', record)
    record_path = _records.publication_path(paths, 'example', _OID)
    record_path.rename(record_path.with_suffix('.TOML'))
    deleted = record_path.with_suffix('.deleted')
    deleted.touch()
    deleted.rename(deleted.with_suffix('.DELETED'))

    expected_records = (record,) if os.name == 'nt' else ()
    expected_deletions: set[str] = {_OID} if os.name == 'nt' else set()
    assert _records.list_publications(paths, 'example') == expected_records
    assert _records.deleted_publications(paths, 'example') == expected_deletions


@pytest.mark.parametrize(
    ('operation', 'message'),
    [
        ('deletions', 'cannot list local snapshot deletions'),
        ('records', 'cannot list published snapshot records'),
    ],
)
def test_publication_inventory_refuses_scan_iteration_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    message: str,
) -> None:
    paths = _paths(tmp_path)
    if operation == 'deletions':
        _records.mark_publication_deleted(paths, 'example', _OID)
    else:
        _records.save_publication(paths, 'example', _publication(tmp_path))
    directory = _records.publication_path(paths, 'example', _OID).parent
    real_scandir = os.scandir
    with real_scandir(directory) as entries:
        first = next(entries)

    def fail_after_entry(
        path: os.PathLike[str] | str,
    ) -> object:
        if Path(path) == directory:
            return _FailingScandir(first)
        return real_scandir(path)

    monkeypatch.setattr(_records.os, 'scandir', fail_after_entry)
    inventory = (
        _records.deleted_publications if operation == 'deletions' else _records.list_publications
    )
    with pytest.raises(PubanonError, match=message):
        inventory(paths, 'example')


@pytest.mark.parametrize(
    ('operation', 'message'),
    [
        ('deletions', 'cannot list local snapshot deletions'),
        ('records', 'cannot list published snapshot records'),
    ],
)
def test_publication_inventory_refuses_scan_open_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    message: str,
) -> None:
    paths = _paths(tmp_path)
    directory = _records.publication_path(paths, 'example', _OID).parent
    directory.mkdir(parents=True)
    real_scandir = os.scandir

    def fail_target(path: os.PathLike[str] | str) -> object:
        if Path(path) == directory:
            raise PermissionError('injected opening failure')
        return real_scandir(path)

    monkeypatch.setattr(_records.os, 'scandir', fail_target)
    inventory = (
        _records.deleted_publications if operation == 'deletions' else _records.list_publications
    )
    with pytest.raises(PubanonError, match=message):
        inventory(paths, 'example')


@pytest.mark.parametrize(
    ('before', 'after'),
    [
        ('[derivation]', '[derivation]\nextra = 1'),
        ('original = "' + '1' * 40 + '"', 'original = "bad"'),
        ('correction = "' + '2' * 40 + '"\n', ''),
        ('resolutions = []', 'resolutions = [1]'),
    ],
)
def test_publication_load_rejects_malformed_derivation(
    tmp_path: Path, before: str, after: str
) -> None:
    paths = _paths(tmp_path)
    record = replace(
        _publication(tmp_path, bookmark=False),
        pinned=None,
        derivation=_records.Derivation('1' * 40, None, '2' * 40),
    )
    _records.save_publication(paths, 'example', record)
    path = _records.publication_path(paths, 'example', _OID)
    text = path.read_text()
    assert before in text
    path.write_text(text.replace(before, after, 1))
    with pytest.raises(PubanonError):
        _records.load_publication(paths, 'example', _OID)


def test_publication_rejects_filename_mismatch_and_naive_time(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    record = _publication(tmp_path)
    with pytest.raises(PubanonError):
        _records.save_publication(
            paths,
            'example',
            replace(record, confirmed_at=datetime(2026, 9, 16, tzinfo=UTC).replace(tzinfo=None)),
        )
    _records.save_publication(paths, 'example', record)
    wrong = 'f' * 40
    _records.publication_path(paths, 'example', _OID).rename(
        _records.publication_path(paths, 'example', wrong)
    )
    with pytest.raises(PubanonError):
        _records.load_publication(paths, 'example', wrong)


@pytest.mark.parametrize(
    ('before', 'after'),
    [
        ('schema_version = 1', 'schema_version = 2'),
        ('schema_version = 1', 'schema_version = 1\nextra = "bad"'),
        (f'tree = "{_TREE}"\n', ''),
        ('confirmed_at = 2026-09-16T12:34:56Z', 'confirmed_at = "yesterday"'),
        ('[pinned]', '[pinned]\nextra = "bad"'),
        ('owner = "owner"', 'owner = ""'),
        (f'refs/pubanon/publications/example/{_OID}', 'refs/heads/main'),
        ('[source_bookmark]', '[source_bookmark]\nextra = "bad"'),
    ],
)
def test_publication_rejects_invalid_fields(tmp_path: Path, before: str, after: str) -> None:
    paths = _paths(tmp_path)
    _records.save_publication(paths, 'example', _publication(tmp_path))
    path = _records.publication_path(paths, 'example', _OID)
    text = path.read_text()
    assert before in text
    path.write_text(text.replace(before, after, 1))
    with pytest.raises(PubanonError):
        _records.load_publication(paths, 'example', _OID)


def test_publication_rejects_pinned_source_without_bookmark(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    record = _publication(tmp_path)
    with pytest.raises(PubanonError, match='invalid published snapshot record'):
        _records.save_publication(paths, 'example', replace(record, source_bookmark=None))
    _records.save_publication(paths, 'example', record)
    path = _records.publication_path(paths, 'example', _OID)
    path.write_text(path.read_text().split('[source_bookmark]')[0])
    with pytest.raises(PubanonError, match='invalid published snapshot record'):
        _records.load_publication(paths, 'example', _OID)


@pytest.mark.parametrize('kind', ['both', 'neither', 'correction', 'unknown-source-bookmark'])
def test_publication_requires_exact_pinned_or_derived_form(tmp_path: Path, kind: str) -> None:
    record = _publication(tmp_path)
    if kind != 'both':
        record = replace(record, pinned=None)
    if kind != 'neither':
        record = replace(record, derivation=_records.Derivation('1' * 40, None, '2' * 40, ()))
    if kind == 'correction':
        record = replace(record, replaces=_SOURCE, replace_scope='one')
    if kind != 'unknown-source-bookmark':
        record = replace(record, source_bookmark=None)
    with pytest.raises(PubanonError):
        _records.save_publication(_paths(tmp_path), 'example', record)


@pytest.mark.parametrize('change', [{'replaces': _SOURCE}, {'replace_scope': 'one'}])
def test_publication_requires_paired_replacement_fields(
    tmp_path: Path, change: dict[str, object]
) -> None:
    with pytest.raises(PubanonError):
        _records.save_publication(
            _paths(tmp_path), 'example', replace(_publication(tmp_path), **change)
        )


@pytest.mark.parametrize('tagged', [True, False])
def test_publication_round_trip_has_only_provenance(tmp_path: Path, tagged: bool) -> None:
    paths = _paths(tmp_path)
    record = _publication(tmp_path)
    if not tagged:
        record = replace(record, tag_intent=None)
    _records.save_publication(paths, 'example', record)
    path = _records.publication_path(paths, 'example', _OID)
    data = tomllib.loads(path.read_text())
    expected = {
        'schema_version',
        'commit',
        'tree',
        'snapshot_digest',
        'fingerprint',
        'message',
        'confirmed_at',
        'pinned',
        'destination',
    }
    expected.add('source_bookmark')
    if tagged:
        expected.add('tag_intent')
    assert set(data) == expected
    assert data['schema_version'] == 1
    assert data['commit'] == _OID
    assert data['tree'] == _TREE
    assert data['confirmed_at'].tzinfo is not None
    assert_private_mode(path, 0o600)
    assert_private_mode(path.parent, 0o700)
    assert _records.load_publication(paths, 'example', _OID) == record
    assert _records.list_publications(paths, 'example') == (record,)


def test_relocated_publication_source_preserves_all_other_provenance(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    record = _publication(tmp_path)
    _records.save_publication(paths, 'example', record)
    assert record.source_bookmark is not None
    bookmark = replace(
        record.source_bookmark,
        source_path=tmp_path / 'moved',
        git_common_dir=tmp_path / 'moved/.git',
    )
    updated = _records.relocate_publication_source(paths, 'example', record, bookmark)
    assert updated == replace(record, source_bookmark=bookmark)
    assert _records.load_publication(paths, 'example', _OID) == updated
    assert_private_mode(_records.publication_path(paths, 'example', _OID), 0o600)
    with pytest.raises(PubanonError, match='changed'):
        _records.relocate_publication_source(paths, 'example', record, bookmark)
    with pytest.raises(PubanonError, match='conflicts'):
        _records.save_publication(paths, 'example', record)


def test_relocated_publication_source_refuses_different_bookmark(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    record = _publication(tmp_path)
    _records.save_publication(paths, 'example', record)
    assert record.source_bookmark is not None
    bookmark = replace(
        record.source_bookmark, refname='refs/pubanon/publications/example/' + 'd' * 40
    )
    with pytest.raises(PubanonError, match='conflicts'):
        _records.relocate_publication_source(paths, 'example', record, bookmark)
    assert _records.load_publication(paths, 'example', _OID) == record


def test_replacement_all_can_explicitly_remove_every_tag(tmp_path: Path) -> None:
    record = replace(
        _replacement(tmp_path),
        replace_scope='all',
        replaces='1' * 40,
        tag_intent=None,
        tip=_OID,
        poststate_tags={},
        replayed=(),
    )
    _records.save_candidate(_paths(tmp_path), 'example', record)
    assert _records.load_candidate(_paths(tmp_path), 'example') == record
    assert _records.candidate_poststate(record) == (_OID, {})


@pytest.mark.parametrize(
    'change',
    [
        {'replaces': None},
        {'replace_scope': None},
        {'replace_scope': 'first'},
        {'replaces': 'bad'},
        {'replaces': _OID},
        {'prestate_main': None},
        {'tip': None},
        {'poststate_tags': None},
        {'tip': 'bad'},
        {'poststate_tags': {'bad tag': _OID}},
        {'poststate_tags': {'v1.0.0': 'bad'}},
        {'tip': _OID},
        {'poststate_tags': {}},
        {'replace_scope': 'all'},
        {'pinned': None},
    ],
)
def test_replacement_candidate_rejects_inconsistent_state(
    tmp_path: Path, change: dict[str, object]
) -> None:
    with pytest.raises(PubanonError, match='invalid snapshot'):
        _records.save_candidate(
            _paths(tmp_path), 'example', replace(_replacement(tmp_path), **change)
        )


def test_replacement_earlier_round_trip_and_poststate(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    record = _replacement(tmp_path)
    _records.save_candidate(paths, 'example', record)
    assert _records.load_candidate(paths, 'example') == record
    assert _records.candidate_tip(record) == '2' * 40
    assert _records.candidate_poststate(record) == (
        '2' * 40,
        {'v1.0.0': _OID, 'v1.1.0': _OID, 'v1.2.3': '2' * 40},
    )


@pytest.mark.parametrize('scope', ['one', 'all'])
@pytest.mark.parametrize('tagged', [False, True])
def test_replacement_latest_and_all_round_trip(tmp_path: Path, scope: str, tagged: bool) -> None:
    paths = _paths(tmp_path)
    record = replace(
        _candidate(tmp_path),
        replaces=_SOURCE,
        replace_scope=scope,
        prestate_main=_SOURCE,
        prestate_tags={'v1.2.3': _SOURCE} if tagged else {},
        tag_intent='v1.2.3' if tagged else None,
        tip=_OID,
        poststate_tags={'v1.2.3': _OID} if tagged else {},
    )
    _records.save_candidate(paths, 'example', record)
    assert _records.load_candidate(paths, 'example') == record
    assert _records.candidate_poststate(record) == (_OID, record.poststate_tags)
    publication = replace(_publication(tmp_path), replaces=_SOURCE, replace_scope=scope)
    _records.save_publication(paths, 'example', publication)
    assert _records.load_publication(paths, 'example', _OID) == publication


@pytest.mark.parametrize('same', ['old', 'new'])
def test_replay_mapping_requires_unique_old_and_new(tmp_path: Path, same: str) -> None:
    entry = _replay(tmp_path)
    second = replace(
        entry,
        old=entry.old if same == 'old' else '3' * 40,
        new=entry.new if same == 'new' else '4' * 40,
        original_bookmark=None,
        source_bookmark=None,
    )
    record = replace(
        _replacement(tmp_path),
        tip=second.new,
        replayed=(entry, second),
        poststate_tags={'v1.0.0': _OID, 'v1.1.0': _OID, 'v1.2.3': second.new},
    )
    with pytest.raises(PubanonError):
        _records.save_candidate(_paths(tmp_path), 'example', record)


@pytest.mark.parametrize(
    'change',
    [
        {'old': 'bad'},
        {'new': 'bad'},
        {'original_source': 'bad'},
        {'resolutions': ()},
    ],
)
def test_replay_validation_checks_mapping_and_bookmark_ownership(
    tmp_path: Path, change: dict[str, object]
) -> None:
    entry = _replay(tmp_path)
    if change == {'resolutions': ()}:
        change = {'source_bookmark': entry.original_bookmark}
    record = replace(_replacement(tmp_path), replayed=(replace(entry, **change),))
    with pytest.raises(PubanonError):
        _records.save_candidate(_paths(tmp_path), 'example', record)


def test_replaying_phase_refuses_pause(tmp_path: Path) -> None:
    with pytest.raises(PubanonError):
        _records.save_rewrite(
            _paths(tmp_path), 'example', replace(_rewrite(tmp_path), pause=_pause())
        )


def test_retry_keeps_first_confirmation_and_refuses_conflict(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    record = _publication(tmp_path)
    _records.save_publication(paths, 'example', record)
    _records.save_publication(
        paths, 'example', replace(record, confirmed_at=record.confirmed_at + timedelta(days=1))
    )
    assert _records.load_publication(paths, 'example', _OID) == record
    with pytest.raises(PubanonError):
        _records.save_publication(paths, 'example', replace(record, message='different'))


@pytest.mark.parametrize(
    ('before', 'after'),
    [
        ('[pause]', '[pause]\nextra = 1'),
        ('[[pause.conflicts]]', '[[pause.conflicts]]\nextra = 1'),
        ('[[pause.conflicts.stages]]', '[[pause.conflicts.stages]]\nextra = 1'),
        ('decision_paths = ["directory/file", "other"]', 'decision_paths = [1]'),
        ('stage = 1', 'stage = true'),
        ('mode = 33188', 'mode = "100644"'),
    ],
)
def test_rewrite_load_refuses_malformed_nested_fields(
    tmp_path: Path, before: str, after: str
) -> None:
    paths = _paths(tmp_path)
    _records.save_rewrite(
        paths, 'example', replace(_rewrite(tmp_path), phase='awaiting', pause=_pause())
    )
    path = _records.rewrite_path(paths, 'example')
    text = path.read_text()
    assert before in text
    path.write_text(text.replace(before, after, 1))
    with pytest.raises(PubanonError):
        _records.load_rewrite(paths, 'example')


@pytest.mark.parametrize(
    'change',
    [
        {'decision_paths': ()},
        {'decision_paths': ('file', 'file')},
        {'paths': ('../out',)},
        {'decision_paths': ('../out',)},
        {'paths': ()},
    ],
)
def test_rewrite_refuses_invalid_conflict(tmp_path: Path, change: dict[str, object]) -> None:
    pause = _pause()
    conflict = replace(pause.conflicts[0], **change)
    record = replace(
        _rewrite(tmp_path), phase='awaiting', pause=replace(pause, conflicts=(conflict,))
    )
    with pytest.raises(PubanonError):
        _records.save_rewrite(_paths(tmp_path), 'example', record)


@pytest.mark.parametrize('change', [{'old': _SOURCE}, {'tree': 'bad'}, {'conflicts': ()}])
def test_rewrite_refuses_invalid_pause(tmp_path: Path, change: dict[str, object]) -> None:
    record = replace(_rewrite(tmp_path), phase='awaiting', pause=replace(_pause(), **change))
    with pytest.raises(PubanonError):
        _records.save_rewrite(_paths(tmp_path), 'example', record)


@pytest.mark.parametrize(
    'change',
    [
        {'phase': 'unknown-outcome'},
        {'phase': 'awaiting'},
        {'descendants': ('1' * 40, '1' * 40)},
        {'correction': 'bad'},
        {'replaces': 'bad'},
        {'prestate_main': 'bad'},
    ],
)
def test_rewrite_refuses_invalid_progress(tmp_path: Path, change: dict[str, object]) -> None:
    with pytest.raises(PubanonError):
        _records.save_rewrite(_paths(tmp_path), 'example', replace(_rewrite(tmp_path), **change))


@pytest.mark.parametrize(
    'change',
    [
        {'mode': 0o040000},
        {'mode': True},
        {'stage': 0},
        {'stage': True},
        {'oid': 'bad'},
        {'path': '../out'},
    ],
)
def test_rewrite_refuses_invalid_stage(tmp_path: Path, change: dict[str, object]) -> None:
    pause = _pause()
    conflict = pause.conflicts[0]
    stage = replace(conflict.stages[0], **change)
    conflict = replace(conflict, stages=(stage,))
    record = replace(
        _rewrite(tmp_path), phase='awaiting', pause=replace(pause, conflicts=(conflict,))
    )
    with pytest.raises(PubanonError):
        _records.save_rewrite(_paths(tmp_path), 'example', record)


def test_rewrite_refuses_out_of_order_replay(tmp_path: Path) -> None:
    record = replace(
        _rewrite(tmp_path), descendants=('3' * 40, '1' * 40), replayed=(_replay(tmp_path),)
    )
    with pytest.raises(PubanonError):
        _records.save_rewrite(_paths(tmp_path), 'example', record)


@pytest.mark.parametrize('phase', ['replaying', 'exporting', 'awaiting'])
def test_rewrite_round_trip_permissions_and_atomic_replace(tmp_path: Path, phase: str) -> None:
    paths = _paths(tmp_path)
    assert _records.load_rewrite(paths, 'example') is None
    assert not paths.data_dir.exists()
    record = replace(
        _rewrite(tmp_path), phase=phase, pause=None if phase == 'replaying' else _pause()
    )
    _records.save_rewrite(paths, 'example', record)
    path = _records.rewrite_path(paths, 'example')
    assert path == paths.data_dir / 'projects/example/rewrite.toml'
    assert _records.rewrite_directory(paths, 'example') == path.parent / 'rewrite'
    assert_private_mode(path, 0o600)
    assert_private_mode(path.parent, 0o700)
    assert _records.load_rewrite(paths, 'example') == record
    content = path.read_bytes()
    changed = replace(record, phase='replaying', pause=None, replayed=(_replay(tmp_path),))
    with (
        patch.object(_filesystem, 'replace', side_effect=OSError('injected')),
        pytest.raises(PubanonError),
    ):
        _records.save_rewrite(paths, 'example', changed)
    assert path.read_bytes() == content
    assert not list(path.parent.glob('*.tmp'))
    _records.save_rewrite(paths, 'example', changed)
    assert _records.load_rewrite(paths, 'example') == changed


def test_save_candidate_directory_failure_reports_possible_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    record = _candidate(tmp_path)

    def _fail_directory(_path: Path) -> None:
        raise OSError('directory sync failed')

    monkeypatch.setattr(_filesystem, 'sync_directory', _fail_directory)
    with pytest.raises(PubanonError, match='snapshot record may have been saved'):
        _records.save_candidate(paths, 'example', record)
    assert _records.load_candidate(paths, 'example') == record


def test_save_candidate_refuses_surrogate_message(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    record = _candidate(tmp_path)
    _records.save_candidate(paths, 'example', record)

    with pytest.raises(PubanonError, match='invalid snapshot record'):
        _records.save_candidate(
            paths,
            'example',
            replace(record, message='invalid\ud800message'),
        )


def test_save_candidate_restores_owner_only_mode(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    record = _candidate(tmp_path)
    _records.save_candidate(paths, 'example', record)
    _filesystem.chmod(_records.candidate_path(paths, 'example'), 0o644)
    _records.save_candidate(paths, 'example', record)
    assert_private_mode(_records.candidate_path(paths, 'example'), 0o600)


def test_shared_diagnostic_paths_require_consistent_recorded_decisions(tmp_path: Path) -> None:
    entry = _replay(tmp_path)
    first = entry.resolutions[0]
    consistent = replace(entry, resolutions=(first, first))
    record = replace(_replacement(tmp_path), replayed=(consistent,))
    _records.save_candidate(_paths(tmp_path), 'example', record)
    assert _records.load_candidate(_paths(tmp_path), 'example') == record
    conflicting = replace(entry, resolutions=(first, replace(first, action='delete')))
    with pytest.raises(PubanonError):
        _records.save_candidate(
            _paths(tmp_path), 'example', replace(record, replayed=(conflicting,))
        )
