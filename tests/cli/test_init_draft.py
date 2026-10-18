import json
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from pubanon import _config, _filesystem
from pubanon._cli import _init_draft
from pubanon._errors import PubanonError
from tests._permissions import assert_private_mode

_MISSING = object()


def _draft(tmp_path: Path, *, commit: str = 'a' * 40) -> _init_draft.Draft:
    return _init_draft.Draft(
        source=tmp_path / 'source',
        commit=commit,
        self_publication=True,
        pending=('repo', 'visibility', 'review'),
        phase='groups',
        reviewing=True,
        flagged=(('config/private.key', 'exclude'),),
        groups=(('src/**', 'include'), ('notes/**', 'exclude')),
        owner='public-owner',
        repo='public-repo',
        visibility='private',
        publication_route='direct',
        source_tags=True,
        required_checks=('test', 'lint'),
        description='Public repository',
        issues=True,
        wiki=False,
        projects=True,
        pull_requests=False,
        discussions=True,
        release_pages=True,
        release_page_drafts=True,
        release_page_sections=False,
    )


def _paths(tmp_path: Path) -> _config.Paths:
    return _config.derive_paths({'HOME': str(tmp_path)})


def test_directory_sync_failure_reports_possible_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    draft = _draft(tmp_path)
    draft_path = _init_draft._path(paths, 'demo')
    sync_calls = 0

    def _fail_directory_sync(directory: Path) -> None:
        nonlocal sync_calls
        assert directory == draft_path.parent
        sync_calls += 1
        raise OSError('injected directory sync failure')

    monkeypatch.setattr(_filesystem, 'sync_directory', _fail_directory_sync)

    with pytest.raises(PubanonError, match='init draft may have been saved'):
        _init_draft.save(paths, 'demo', draft)

    assert sync_calls == 1
    assert _init_draft.load(paths, 'demo') == draft
    assert list(draft_path.parent.glob('.pubanon-write-*.tmp')) == []


def test_load_reads_a_draft_saved_without_release_page_drafts_as_false(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    draft = _draft(tmp_path)
    _init_draft.save(paths, 'demo', draft)
    draft_path = _init_draft._path(paths, 'demo')
    payload = cast('dict[str, object]', json.loads(draft_path.read_text(encoding='utf-8')))
    del payload['release_page_drafts']
    draft_path.write_text(json.dumps(payload), encoding='utf-8')

    assert _init_draft.load(paths, 'demo') == replace(draft, release_page_drafts=False)


def test_load_reads_a_draft_saved_without_release_page_sections_as_unanswered(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    draft = _draft(tmp_path)
    _init_draft.save(paths, 'demo', draft)
    draft_path = _init_draft._path(paths, 'demo')
    payload = cast('dict[str, object]', json.loads(draft_path.read_text(encoding='utf-8')))
    del payload['release_page_sections']
    draft_path.write_text(json.dumps(payload), encoding='utf-8')

    assert _init_draft.load(paths, 'demo') == replace(draft, release_page_sections=None)


@pytest.mark.parametrize(
    ('key', 'value'),
    [
        ('version', 99),
        ('publication_route', _MISSING),
        ('publication_route', 'other'),
        ('publication_route', 3),
        ('source_tags', _MISSING),
        ('source_tags', 'yes'),
        ('release_pages', 'yes'),
        ('release_pages', _MISSING),
        ('release_page_drafts', 'yes'),
        ('release_page_sections', 'yes'),
        # Drafts without pages cannot come from an answer.
        ('release_pages', False),
        ('phase', _MISSING),
        ('phase', 'done'),
        ('pending', ['selection', 1]),
        ('pending', []),
        ('pending', ['selection', 'unknown', 'review']),
        ('pending', ['review', 'owner']),
        ('flagged', [['README.md']]),
        ('flagged', [['README.md', 'ask']]),
        ('groups', [['src/**', 'ask']]),
        ('reviewing', 'false'),
        ('pull_requests', 'yes'),
        ('discussions', 1),
    ],
)
def test_load_refuses_invalid_shape_types_and_version(
    tmp_path: Path,
    key: str,
    value: object,
) -> None:
    paths = _paths(tmp_path)
    _init_draft.save(paths, 'demo', _draft(tmp_path))
    draft_path = _init_draft._path(paths, 'demo')
    payload = cast('dict[str, object]', json.loads(draft_path.read_text(encoding='utf-8')))
    if value is _MISSING:
        del payload[key]
    else:
        payload[key] = value
    draft_path.write_text(json.dumps(payload), encoding='utf-8')

    with pytest.raises(_init_draft.InvalidDraftError, match='invalid init draft'):
        _init_draft.load(paths, 'demo')


def test_load_reports_io_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paths = _paths(tmp_path)
    draft_path = _init_draft._path(paths, 'demo')
    draft_path.parent.mkdir(parents=True)
    draft_path.write_text('{}', encoding='utf-8')
    original_read_text = Path.read_text

    def _fail_draft_read(
        self: Path,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> str:
        if self == draft_path:
            raise OSError('injected read failure')
        return original_read_text(self, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(Path, 'read_text', _fail_draft_read)

    with pytest.raises(PubanonError, match='cannot load init draft') as exc_info:
        _init_draft.load(paths, 'demo')

    assert isinstance(exc_info.value.__cause__, OSError)


def test_load_reports_malformed_json(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    draft_path = _init_draft._path(paths, 'demo')
    draft_path.parent.mkdir(parents=True)
    draft_path.write_text('{not JSON', encoding='utf-8')

    with pytest.raises(_init_draft.InvalidDraftError, match='invalid init draft') as exc_info:
        _init_draft.load(paths, 'demo')

    assert isinstance(exc_info.value.__cause__, json.JSONDecodeError)


def test_missing_draft_loads_as_none_and_remove_is_idempotent(tmp_path: Path) -> None:
    paths = _paths(tmp_path)

    assert _init_draft.load(paths, 'demo') is None
    _init_draft.remove(paths, 'demo')

    assert _init_draft.load(paths, 'demo') is None


def test_replace_failure_preserves_prior_draft(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    original = _draft(tmp_path)
    _init_draft.save(paths, 'demo', original)

    def _fail_replace(_source: Path, target: Path) -> None:
        raise OSError(f'injected replacement failure for {target.name}')

    monkeypatch.setattr(_filesystem, 'replace', _fail_replace)

    with pytest.raises(PubanonError, match='cannot save init draft') as exc_info:
        _init_draft.save(paths, 'demo', _draft(tmp_path, commit='b' * 40))

    assert isinstance(exc_info.value.__cause__, OSError)
    assert _init_draft.load(paths, 'demo') == original
    assert list(_init_draft._path(paths, 'demo').parent.glob('.pubanon-write-*.tmp')) == []


def test_round_trip_preserves_complete_draft_owner_only(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    draft = _draft(tmp_path)

    _init_draft.save(paths, 'demo', draft)

    draft_path = paths.data_dir / 'projects' / 'demo' / 'init-draft.json'
    assert _init_draft._path(paths, 'demo') == draft_path
    assert _init_draft.load(paths, 'demo') == draft
    assert_private_mode(draft_path, 0o600)
    assert json.loads(draft_path.read_text(encoding='utf-8'))['version'] == 1
    _init_draft.remove(paths, 'demo')
    assert _init_draft.load(paths, 'demo') is None


def test_save_syncs_directory_after_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    draft = _draft(tmp_path)
    draft_path = _init_draft._path(paths, 'demo')
    events: list[str] = []
    original_replace = _filesystem.replace
    original_sync_directory = _filesystem.sync_directory

    def _track_replace(source: Path, target: Path) -> None:
        assert target == draft_path
        events.append('replace')
        original_replace(source, target)

    def _track_directory(directory: Path) -> None:
        assert directory == draft_path.parent
        events.append('directory sync')
        original_sync_directory(directory)

    monkeypatch.setattr(_filesystem, 'replace', _track_replace)
    monkeypatch.setattr(_filesystem, 'sync_directory', _track_directory)

    _init_draft.save(paths, 'demo', draft)

    assert events == ['replace', 'directory sync']


def test_unsupported_draft_version_is_rejected_without_rewriting_bytes(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    _init_draft.save(paths, 'demo', _draft(tmp_path))
    draft_path = _init_draft._path(paths, 'demo')
    payload = json.loads(draft_path.read_text(encoding='utf-8'))
    payload['version'] = 99
    old_bytes = json.dumps(payload).encode()
    draft_path.write_bytes(old_bytes)

    with pytest.raises(_init_draft.InvalidDraftError, match='invalid init draft version'):
        _init_draft.load(paths, 'demo')
    assert draft_path.read_bytes() == old_bytes
