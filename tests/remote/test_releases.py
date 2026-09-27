import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Literal

import pytest

from pubanon import _config
from pubanon._errors import OperationCanceled, PubanonError
from pubanon._remote import _host, _releases
from pubanon._snapshots import _candidate, _records
from tests._git import git_env

_TAG = 'v1.2.3'
type _Context = tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig, _Host]
type _Outcome = Literal['normal', 'missing', 'notes', 'status', 'tag', 'read-error']


@dataclass
class _Host:
    destination: _host.Destination
    commit: str
    page: _host.GitHubRelease | None = None
    writes: list[tuple[str, str, bytes]] = field(default_factory=list[tuple[str, str, bytes]])
    after_write: _Outcome = 'normal'
    write_error: BaseException | None = None

    def _destination(self, *_args: object) -> _host.Destination:
        return self.destination

    def _read(self, dest: _host.Destination, tag: str) -> _host.GitHubRelease | None:
        assert dest == self.destination
        assert tag == _TAG
        if self.writes and self.after_write == 'read-error':
            raise PubanonError('read failed\x1b[31m')
        return self.page

    def _refs(self, dest: _host.Destination, *_args: object) -> _host.RemoteRefs:
        assert dest == self.destination
        return _host.RemoteRefs(self.commit, {_TAG: self.commit})

    def _write(self, dest: _host.Destination, tag: str, *, title: str, notes: bytes) -> None:
        assert dest == self.destination
        assert tag == _TAG
        self.writes.append((tag, title, notes))
        if self.write_error is not None:
            raise self.write_error
        previous = self.page
        self.page = _host.GitHubRelease(
            tag,
            title,
            notes.decode(),
            'https://example.invalid/release',
            previous.draft if previous else False,
            previous.prerelease if previous else False,
        )
        if self.after_write == 'missing':
            self.page = None
        elif self.after_write == 'notes':
            self.page = replace(self.page, notes='different')
        elif self.after_write == 'status':
            self.page = replace(self.page, draft=not self.page.draft)
        elif self.after_write == 'tag':
            self.commit = 'b' * 40

    def _create(self, dest: _host.Destination, tag: str, *, title: str, notes: bytes) -> None:
        assert self.page is None, 'create requires an absent release'
        self._write(dest, tag, title=title, notes=notes)

    def _edit(self, dest: _host.Destination, tag: str, *, title: str, notes: bytes) -> None:
        assert self.page is not None, 'edit requires an existing release'
        self._write(dest, tag, title=title, notes=notes)


def _git(store: Path, *args: str, data: bytes | None = None) -> str:
    return (
        subprocess.run(
            ['git', '-C', str(store), *args],
            input=data,
            capture_output=True,
            check=True,
            env=git_env(),
        )
        .stdout.decode()
        .strip()
    )


@pytest.fixture
def release_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Context:
    paths = _config.Paths(
        tmp_path / 'config', tmp_path / 'data', tmp_path / 'global', tmp_path / 'registry'
    )
    g = _config.GlobalConfig(
        'Publisher', 'publisher@example.invalid', {'github': 'publisher'}, ('Private Person',)
    )
    p = _config.ProjectConfig(
        'example',
        tmp_path / 'source',
        'github',
        'public-owner',
        'public-repo',
        'public',
        ('**',),
        (),
        (),
        (),
        ('internal-only',),
        self_publication=False,
        required_checks=(),
    )
    store = _candidate.candidate_store_path(paths, p.name)
    store.mkdir(parents=True)
    _git(store, 'init', '--bare', '-q')
    tree = _git(store, 'mktree', data=b'')
    commit = _git(store, 'commit-tree', tree, data=b'Published snapshot\n')
    _git(store, 'update-ref', 'refs/heads/main', commit)
    _git(store, 'update-ref', f'refs/tags/{_TAG}', commit)
    host = _Host(
        _host.Destination(
            'github', p.owner, p.repo, 'https://example.invalid/repo.git', 'publisher', 'public'
        ),
        commit,
    )
    monkeypatch.setattr(_host, 'verify_destination', host._destination)
    monkeypatch.setattr(_host, 'ls_remote', host._refs)
    monkeypatch.setattr(_host, 'read_release', host._read)
    monkeypatch.setattr(_host, 'create_release', host._create)
    monkeypatch.setattr(_host, 'edit_release', host._edit)
    return paths, g, p, host


def _prepare(
    context: _Context,
    *,
    operation: Literal['create', 'edit'] = 'create',
    title: str | None = None,
    notes: bytes | None = b'Public notes\r\n',
    tag: str = _TAG,
) -> _releases.ReleaseChange:
    paths, g, p, _host_facts = context
    return _releases.prepare_change(
        paths, g, p, _releases.ReleaseRequest(operation, tag, title, notes)
    )


def test_create_preserves_checked_bytes_and_verifies_result(release_context: _Context) -> None:
    paths, g, p, host = release_context
    change = _prepare(release_context)
    assert change.commit == host.commit
    assert change.title == _TAG
    assert host.writes == []
    result = _releases.apply_change(paths, g, p, change)
    assert host.writes == [(_TAG, _TAG, b'Public notes\r\n')]
    assert result == host.page


def test_create_refuses_existing_page_and_edit_requires_page(release_context: _Context) -> None:
    host = release_context[3]
    with pytest.raises(PubanonError, match='does not exist'):
        _prepare(release_context, operation='edit')
    host.page = _host.GitHubRelease(
        _TAG, 'Existing', 'Public notes', 'https://example.invalid', draft=False, prerelease=False
    )
    with pytest.raises(PubanonError, match='already exists'):
        _prepare(release_context)
    assert host.writes == []


@pytest.mark.parametrize(
    'boundary', ['local-tag', 'local-main', 'remote-tag', 'remote-mismatch', 'store']
)
def test_requires_recorded_matching_publication(
    release_context: _Context, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    paths, g, p, host = release_context
    store = _candidate.candidate_store_path(paths, p.name)
    if boundary == 'store':
        p = replace(p, name='absent')
    elif boundary.startswith('local-'):
        ref = f'refs/tags/{_TAG}' if boundary == 'local-tag' else 'refs/heads/main'
        _git(store, 'update-ref', '-d', ref)
        if boundary == 'local-tag':
            _git(store, 'clone', '-q', str(store), str(p.source_path))
            _git(p.source_path, 'tag', _TAG, host.commit)

            def _prepared(*_args: object) -> SimpleNamespace:
                return SimpleNamespace(
                    state=_records.STATE_RETAINED, tag_intent=_TAG, commit=host.commit
                )

            monkeypatch.setattr(_records, 'load_candidate', _prepared)
    elif boundary == 'remote-tag':

        def _without_tag(*_args: object) -> _host.RemoteRefs:
            return _host.RemoteRefs(host.commit, {})

        monkeypatch.setattr(_host, 'ls_remote', _without_tag)
    else:
        host.commit = 'b' * 40
    expected = {
        'local-tag': 'absent from local published refs',
        'local-main': 'no main branch',
        'remote-tag': 'does not match',
        'remote-mismatch': 'does not match',
        'store': 'no saved published history was found',
    }
    with pytest.raises(PubanonError, match=expected[boundary]):
        _prepare((paths, g, p, host))
    assert host.writes == []
    if boundary == 'store':
        assert not _candidate.candidate_store_path(paths, p.name).exists()


@pytest.mark.parametrize('tag', ['1.2.3', 'v01.2.3', 'v1.2.3-rc1'])
def test_requires_strict_tag(release_context: _Context, tag: str) -> None:
    with pytest.raises(PubanonError, match=r'vX\.Y\.Z'):
        _prepare(release_context, tag=tag)


@pytest.mark.parametrize(
    ('operation', 'title', 'notes'),
    [('create', None, None), ('edit', None, None), ('create', '', b''), ('create', None, b'\xff')],
)
def test_rejects_invalid_metadata_inputs(
    release_context: _Context,
    operation: Literal['create', 'edit'],
    title: str | None,
    notes: bytes | None,
) -> None:
    with pytest.raises(PubanonError):
        _prepare(release_context, operation=operation, title=title, notes=notes)
    assert release_context[3].writes == []


@pytest.mark.parametrize(
    'field_name', ['title', 'retained-notes', 'hygiene', 'source-path', 'tag']
)
def test_gates_complete_result(release_context: _Context, field_name: str) -> None:
    paths, g, p, host = release_context
    operation: Literal['create', 'edit'] = 'create'
    title = None
    notes: bytes | None = b'Public'
    if field_name == 'retained-notes':
        host.page = _host.GitHubRelease(
            _TAG,
            'Old title',
            'Public\nPrivate Person\n',
            'https://example.invalid',
            draft=False,
            prerelease=False,
        )
        operation, title, notes = 'edit', 'New title', None
    elif field_name == 'title':
        title = 'Private Person'
    elif field_name == 'hygiene':
        notes = b'internal-only'
    elif field_name == 'source-path':
        notes = str(p.source_path).encode()
    else:
        p = replace(p, extra_terms=(_TAG,))
    with pytest.raises(PubanonError, match='content') as caught:
        _prepare((paths, g, p, host), operation=operation, title=title, notes=notes)
    assert (
        'title' if field_name == 'title' else 'tag' if field_name == 'tag' else 'notes'
    ) in caught.value.render()
    if field_name == 'retained-notes':
        assert 'line 2' in caught.value.render()
        assert 'Private Person' in caught.value.render()
    assert host.writes == []


@pytest.mark.parametrize('title_only', [True, False])
def test_edit_preserves_omitted_field_and_status(
    release_context: _Context, title_only: bool
) -> None:
    paths, g, p, host = release_context
    host.page = _host.GitHubRelease(
        _TAG, 'Old title', 'Retained\r\n', 'https://example.invalid', draft=True, prerelease=True
    )
    change = _prepare(
        release_context,
        operation='edit',
        title='New title' if title_only else None,
        notes=None if title_only else b'',
    )
    result = _releases.apply_change(paths, g, p, change)
    assert result.title == ('New title' if title_only else 'Old title')
    assert result.notes == ('Retained\r\n' if title_only else '')
    assert result.draft
    assert result.prerelease


@pytest.mark.parametrize('boundary', ['destination', 'tag', 'page', 'status', 'created'])
def test_confirmation_interval_changes_refuse_write(
    release_context: _Context, boundary: str
) -> None:
    paths, g, p, host = release_context
    if boundary != 'created':
        host.page = _host.GitHubRelease(
            _TAG, 'Old title', 'Public', 'https://example.invalid', draft=False, prerelease=False
        )
    change = _prepare(release_context, operation='create' if boundary == 'created' else 'edit')
    if boundary == 'destination':
        host.destination = replace(host.destination, visibility='private')
    elif boundary == 'tag':
        host.commit = 'b' * 40
    elif boundary == 'status':
        assert host.page is not None
        host.page = replace(host.page, draft=True)
    else:
        host.page = _host.GitHubRelease(
            _TAG,
            'Concurrent title',
            'Public',
            'https://example.invalid',
            draft=False,
            prerelease=False,
        )
    with pytest.raises(PubanonError):
        _releases.apply_change(paths, g, p, change)
    assert host.writes == []


def test_self_publication_rules_are_reused(release_context: _Context) -> None:
    paths, g, p, host = release_context
    with pytest.raises(PubanonError, match='content'):
        _prepare(release_context, notes=b'pubanon')
    change = _prepare((paths, g, replace(p, self_publication=True), host), notes=b'pubanon')
    assert change.notes == b'pubanon'


@pytest.mark.parametrize(
    'outcome', ['missing', 'notes', 'status', 'tag', 'read-error', 'write-error', 'cancelled']
)
def test_uncertain_outcomes_never_report_success_or_retry(
    release_context: _Context, outcome: _Outcome | Literal['write-error', 'cancelled']
) -> None:
    paths, g, p, host = release_context
    change = _prepare(release_context)
    if outcome == 'write-error':
        host.write_error = PubanonError('write failed\x1b[31m')
    elif outcome == 'cancelled':
        host.write_error = KeyboardInterrupt()
    else:
        host.after_write = outcome
    with pytest.raises(PubanonError, match='may have changed') as caught:
        _releases.apply_change(paths, g, p, change)
    assert len(host.writes) == 1
    assert f'release show {_TAG}' in (caught.value.recovery or '')
    assert '--project=example' in (caught.value.recovery or '')
    assert '\x1b' not in caught.value.render()
    if outcome == 'cancelled':
        assert isinstance(caught.value, OperationCanceled)


def test_unknown_publication_requires_resolution(
    release_context: _Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _unknown(*_args: object) -> SimpleNamespace:
        return SimpleNamespace(state=_records.STATE_UNKNOWN_OUTCOME)

    monkeypatch.setattr(_records, 'load_candidate', _unknown)
    with pytest.raises(PubanonError, match='unknown') as caught:
        _prepare(release_context)
    assert 'publish' in (caught.value.recovery or '')
    assert '--resolve' in (caught.value.recovery or '')
    assert release_context[3].writes == []
