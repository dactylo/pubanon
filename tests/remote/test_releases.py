import re
import shlex
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Literal, cast

import pytest

from pubanon import _config, _gate
from pubanon._errors import ObservationTimeoutError, OperationCanceled, PubanonError
from pubanon._remote import _host, _releases
from pubanon._snapshots import _candidate, _records, _store
from pubanon._snapshots._source import PinnedSource
from tests._git import git_env

_TAG = 'v1.2.3'
type _Context = tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig, _Host]
type _Outcome = Literal['normal', 'missing', 'notes', 'status', 'tag', 'read-error']


@dataclass
class _Clock:
    now: float = 100.0
    waits: list[float] = field(default_factory=list[float])

    def _monotonic(self) -> float:
        return self.now

    def _sleep(self, seconds: float) -> None:
        self.waits.append(seconds)
        self.now += seconds


@dataclass
class _Host:
    destination: _host.Destination
    commit: str
    page: _host.GitHubRelease | None = None
    writes: list[tuple[str, str, bytes]] = field(default_factory=list[tuple[str, str, bytes]])
    after_write: _Outcome = 'normal'
    write_error: BaseException | None = None
    pages: tuple[_host.GitHubRelease, ...] = ()
    scans: int = 0
    latest: list[bool] = field(default_factory=list[bool])
    drafts: list[bool] = field(default_factory=list[bool])
    stale: list[_host.GitHubRelease | None] = field(
        default_factory=list[_host.GitHubRelease | None]
    )
    deadlines: list[float] = field(default_factory=list[float])
    timeouts: list[float] = field(default_factory=list[float])
    clock: _Clock = field(default_factory=_Clock)
    read_seconds: float = 0
    tag_seconds: float = 0
    read_error: BaseException | None = None
    tag: str = _TAG

    def _all(self, dest: _host.Destination) -> tuple[_host.GitHubRelease, ...]:
        assert dest == self.destination
        self.scans += 1
        return (*self.pages, *((self.page,) if self.page is not None else ()))

    def _destination(self, *_args: object) -> _host.Destination:
        return self.destination

    def _read(
        self, dest: _host.Destination, tag: str, *, deadline: float | None = None
    ) -> _host.GitHubRelease | None:
        assert dest == self.destination
        assert tag == self.tag
        if deadline is not None:
            assert self.clock.now < deadline
            self.deadlines.append(deadline)
            self.clock.now += min(self.read_seconds, deadline - self.clock.now)
            if self.read_error is not None:
                raise self.read_error
            if self.clock.now >= deadline:
                raise ObservationTimeoutError
            if self.stale:
                return self.stale.pop(0)
        if self.writes and self.after_write == 'read-error':
            raise PubanonError('read failed\x1b[31m')
        return self.page

    def _refs(
        self, dest: _host.Destination, *_args: object, timeout: float | None = None
    ) -> _host.RemoteRefs:
        assert dest == self.destination
        if timeout is not None:
            self.timeouts.append(timeout)
            self.clock.now += min(self.tag_seconds, timeout)
            if self.tag_seconds >= timeout:
                raise ObservationTimeoutError
        return _host.RemoteRefs(self.commit, {self.tag: self.commit})

    def _write(self, dest: _host.Destination, tag: str, *, title: str, notes: bytes) -> None:
        assert dest == self.destination
        assert tag == self.tag
        self.writes.append((tag, title, notes))
        if self.write_error is not None:
            raise self.write_error
        previous = self.page
        self.page = _host.GitHubRelease(
            tag,
            title,
            notes.decode(),
            'https://example.invalid/release',
            previous.draft if previous else (self.drafts[-1] if self.drafts else False),
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

    def _create(
        self,
        dest: _host.Destination,
        tag: str,
        *,
        title: str,
        notes: bytes,
        mode: _host.ReleaseCreationMode = 'latest',
    ) -> None:
        assert self.page is None, 'create requires an absent release'
        self.latest.append(mode == 'latest')
        self.drafts.append(mode == 'draft')
        self._write(dest, tag, title=title, notes=notes)

    def _edit(self, dest: _host.Destination, tag: str, *, title: str, notes: bytes) -> None:
        assert self.page is not None, 'edit requires an existing release'
        self._write(dest, tag, title=title, notes=notes)


@dataclass(frozen=True)
class _PageContext:
    _paths: _config.Paths
    _global_config: _config.GlobalConfig
    _project: _config.ProjectConfig
    _record: _records.CandidateRecord
    _release_page: _releases.PageRequest = field(default_factory=_releases.PageRequest)


def _drafting(context: _Context) -> _Context:
    paths, g, p, host = context
    page = _config.ReleasePageSettings(enabled=True, draft=True)
    return paths, g, replace(p, release_page=page), host


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


def _listed(tag: str, *, draft: bool) -> _host.GitHubRelease:
    return _host.GitHubRelease(
        tag, tag, '', f'https://example.invalid/{tag}', draft=draft, prerelease=False
    )


def _matching(change: _releases.ReleaseChange) -> _host.GitHubRelease:
    return _host.GitHubRelease(
        change.tag,
        change.title,
        change.notes.decode(),
        'https://example.invalid/page',
        draft=False,
        prerelease=False,
    )


def _page_plan(ctx: _PageContext) -> _releases.PagePlan:
    return _releases.plan_publication_page(
        ctx._paths, ctx._global_config, ctx._project, ctx._record, ctx._release_page
    )


def _release_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, published: bool
) -> _Context:
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
    commit = 'a' * 40
    if published:
        store = _store.candidate_store_path(paths, p.name)
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
    monkeypatch.setattr(_host, 'all_releases', host._all)
    monkeypatch.setattr(_releases, '_monotonic', host.clock._monotonic)
    monkeypatch.setattr(_releases, '_sleep', host.clock._sleep)
    monkeypatch.setattr(_host, 'create_release', host._create)
    monkeypatch.setattr(_host, 'edit_release', host._edit)
    return paths, g, p, host


@pytest.fixture
def literal_release_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Context:
    return _release_context(tmp_path, monkeypatch, published=False)


@pytest.fixture
def release_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Context:
    return _release_context(tmp_path, monkeypatch, published=True)


@pytest.fixture
def page_context(release_context: _Context) -> tuple[_PageContext, _Host]:
    paths, g, project, host = release_context
    project = replace(project, release_page=_config.ReleasePageSettings(enabled=True))
    host.destination = replace(
        host.destination, url=_host.repository_url(project.owner, project.repo)
    )
    record = _records.CandidateRecord(
        commit=host.commit,
        tree='b' * 40,
        snapshot_digest='c' * 64,
        tag_intent=_TAG,
        fingerprint=_config.fingerprint(g, project),
        pinned=PinnedSource('d' * 40, 'ref', 'refs/heads/main', 'd' * 40),
        destination=_records.RecordedDestination(
            project.host, project.owner, project.repo, project.visibility, g.accounts[project.host]
        ),
        prestate_main=None,
        prestate_tags={},
        state=_candidate.STATE_RETAINED,
        message='Public snapshot',
    )
    return _PageContext(paths, g, project, record), host


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


def _publish_changelog(context: _Context, path: str = 'CHANGELOG.md') -> None:
    paths, _g, p, host = context
    store = _store.candidate_store_path(paths, p.name)
    blob = _git(store, 'hash-object', '-w', '--stdin', data=b'Published notes\n')
    tree = _git(store, 'mktree', data=f'100644 blob {blob}\t{path}\n'.encode())
    host.commit = _git(store, 'commit-tree', tree, data=b'Published snapshot\n')
    _git(store, 'update-ref', 'refs/heads/main', host.commit)
    _git(store, 'update-ref', f'refs/tags/{_TAG}', host.commit)


def test_absence_exhausts_budget_and_preserves_recovery(release_context: _Context) -> None:
    paths, g, p, host = release_context
    request = _releases.ReleaseRequest(
        'create', _TAG, 'Explicit title', b'Public notes\r\n', paths.data_dir / 'checked notes.md'
    )
    change = _releases.prepare_change(paths, g, p, request)
    host.after_write = 'missing'
    with pytest.raises(_releases.ReleaseOutcomeUnknownError, match='could not verify') as caught:
        _releases.apply_change(paths, g, p, change)
    assert host.clock.now == 110.0
    assert host.clock.waits == [0.5, 1.0, 2.0, 4.0, 2.5]
    assert len(host.writes) == 1
    assert shlex.split(caught.value.recovery or '') == [
        'pubanon',
        'release',
        'create',
        _TAG,
        '--title',
        'Explicit title',
        '--notes-file',
        str(paths.data_dir / 'checked notes.md'),
        '--project=example',
    ]
    host.page = _matching(change)
    assert _releases.prepare_change(paths, g, p, request).operation == 'existing'


def test_complete_conflicting_page_recommends_show_and_edit(release_context: _Context) -> None:
    paths, g, p, host = release_context
    change = _prepare(release_context)
    host.page = replace(_matching(change), notes='Different')
    plan = _releases.PagePlan('create', 'new', tag=_TAG, change=change)
    with pytest.raises(PubanonError, match='the release page was not created') as caught:
        _releases.complete_publication_page(paths, g, p, plan)
    assert 'code was published' in (caught.value.fact or '')
    assert 'notes line 1' in (caught.value.fact or '')
    recovery = caught.value.recovery or ''
    assert f'release show {_TAG}' in recovery
    assert f'release edit {_TAG}' in recovery
    assert 'release create' not in recovery
    assert host.writes == []


@pytest.mark.parametrize('published_first', [False, True])
def test_complete_draft_creation_reports_the_observed_status(
    release_context: _Context, *, published_first: bool
) -> None:
    context = _drafting(release_context)
    paths, g, p, host = context
    change = _prepare(context)
    if published_first:
        # The release workflow published the draft before pubanon read it back.
        host.after_write = 'status'

    outcome = _releases.complete_publication_page(
        paths, g, p, _releases.PagePlan('create', 'new', tag=_TAG, change=change)
    )

    assert host.drafts == [True]
    assert outcome == _releases.PageOutcome(
        'created', 'https://example.invalid/release', draft=not published_first
    )


def test_complete_inactive_page_does_not_access_host(
    literal_release_context: _Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, g, p, host = literal_release_context

    def _unexpected(*_args: object) -> None:
        pytest.fail('inactive completion accessed host')

    monkeypatch.setattr(_host, 'verify_destination', _unexpected)
    assert _releases.complete_publication_page(
        paths, g, p, _releases.PagePlan('none', 'disabled')
    ) == _releases.PageOutcome('none', None)
    assert host.writes == []


def test_complete_kept_page_retains_custom_draft(release_context: _Context) -> None:
    paths, g, p, host = release_context
    host.page = replace(_matching(_prepare(release_context)), title='Custom', draft=True)
    plan = _releases.PagePlan('keep', 'exists', tag=_TAG, existing=host.page)
    assert _releases.complete_publication_page(paths, g, p, plan) == _releases.PageOutcome(
        'kept', host.page.url, draft=True
    )
    assert host.writes == []


def test_complete_preexisting_matching_page_reports_existing(release_context: _Context) -> None:
    paths, g, p, host = release_context
    change = _prepare(release_context)
    host.page = _matching(change)
    plan = _releases.PagePlan('keep', 'matches', tag=_TAG, existing=host.page, change=change)
    assert _releases.complete_publication_page(paths, g, p, plan) == _releases.PageOutcome(
        'existing', host.page.url
    )
    assert host.writes == []


def test_conflicting_page_appearing_before_write_refuses(release_context: _Context) -> None:
    paths, g, p, host = release_context
    change = _prepare(release_context)
    host.page = replace(_matching(change), notes='Different')
    with pytest.raises(PubanonError) as caught:
        _releases.apply_change(paths, g, p, change)
    assert 'notes line 1' in (caught.value.fact or '')
    assert f'release show {_TAG}' in (caught.value.recovery or '')
    assert f'release edit {_TAG}' in (caught.value.recovery or '')
    assert host.scans == 1
    assert host.writes == []


def test_create_preserves_checked_bytes_and_verifies_result(release_context: _Context) -> None:
    paths, g, p, host = release_context
    change = _prepare(release_context)
    assert change.commit == host.commit
    assert change.title == _TAG
    assert host.writes == []
    result = _releases.apply_change(paths, g, p, change)
    assert host.writes == [(_TAG, _TAG, b'Public notes\r\n')]
    assert host.page is not None
    assert result == _releases.ReleaseResult(host.page, written=True)


def test_create_rechecks_absence_without_rewriting(release_context: _Context) -> None:
    paths, g, p, host = release_context
    change = _prepare(release_context)
    host.stale = [None, None, None]
    result = _releases.apply_change(paths, g, p, change)
    assert host.page is not None
    assert result == _releases.ReleaseResult(host.page, written=True)
    assert host.writes == [(change.tag, change.title, change.notes)]
    assert host.clock.waits == [0.5, 1.0, 2.0]
    assert host.deadlines == [110.0] * 4
    assert host.timeouts == [10.0]


def test_create_refuses_existing_page_and_edit_requires_page(release_context: _Context) -> None:
    paths, g, p, host = release_context
    with pytest.raises(PubanonError, match='does not exist'):
        _prepare(release_context, operation='edit')
    expected = _prepare(release_context)
    host.page = _matching(expected)
    matching = _prepare(release_context)
    assert matching.operation == 'existing'
    assert matching.previous == host.page
    with pytest.raises(ValueError, match='no change to apply'):
        _releases.apply_change(paths, g, p, matching)
    host.page = replace(host.page, title='Different', notes='Different', draft=True)
    with pytest.raises(PubanonError, match='different') as caught:
        _prepare(release_context)
    assert 'title' in (caught.value.fact or '')
    assert 'notes line 1' in (caught.value.fact or '')
    assert 'draft' in (caught.value.fact or '')
    assert 'release show' in (caught.value.recovery or '')
    assert 'release edit' in (caught.value.recovery or '')
    assert host.writes == []


def test_draft_creation_requests_a_draft_and_reads_it_back(release_context: _Context) -> None:
    context = _drafting(release_context)
    paths, g, p, host = context
    change = _prepare(context)
    assert change.draft

    result = _releases.apply_change(paths, g, p, change)

    assert host.drafts == [True]
    assert result.written
    assert result.page.draft


def test_draft_ignores_higher_published_page_when_choosing_create_mode(
    release_context: _Context,
) -> None:
    context = _drafting(release_context)
    paths, g, p, host = context
    host.tag = 'v0.9.0'
    _git(_store.candidate_store_path(paths, p.name), 'update-ref', 'refs/tags/v0.9.0', host.commit)
    host.pages = (_host.GitHubRelease('v0.10.0', 'Title', '', '', draft=False, prerelease=False),)
    change = _prepare(context, tag=host.tag)

    result = _releases.apply_change(paths, g, p, change)

    assert result.page.draft
    assert host.drafts == [True]


def test_edit_rechecks_previous_page(release_context: _Context) -> None:
    paths, g, p, host = release_context
    host.page = _matching(_prepare(release_context))
    change = _prepare(release_context, operation='edit', title='New title')
    host.stale = [host.page, host.page]
    result = _releases.apply_change(paths, g, p, change)
    assert result.page.title == 'New title'
    assert result.written
    assert host.clock.waits == [0.5, 1.0]
    assert host.scans == 0
    assert len(host.writes) == 1


def test_failed_scan_never_writes(
    release_context: _Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, g, p, host = release_context
    change = _prepare(release_context)

    def _failed(_dest: _host.Destination) -> tuple[_host.GitHubRelease, ...]:
        raise PubanonError('scan failed')

    monkeypatch.setattr(_host, 'all_releases', _failed)
    with pytest.raises(PubanonError, match='scan failed'):
        _releases.apply_change(paths, g, p, change)
    assert host.writes == []


def test_matching_page_appearing_before_write_succeeds(release_context: _Context) -> None:
    paths, g, p, host = release_context
    change = _prepare(release_context)
    host.page = _matching(change)
    result = _releases.apply_change(paths, g, p, change)
    assert result == _releases.ReleaseResult(host.page, written=False)
    assert host.scans == 1
    assert host.writes == []


def test_metadata_findings_refuse_unknown_match_location(monkeypatch: pytest.MonkeyPatch) -> None:
    def _missing_span(_data: bytes, _term: str) -> None:
        return None

    monkeypatch.setattr(_gate, 'first_match', _missing_span)
    with pytest.raises(PubanonError, match='gate match location could not be determined'):
        _releases.release_metadata_findings(
            {_gate.RuleClass.IDENTITY_CRITICAL: ('private',)},
            tag='v1.2.3',
            title='private',
            notes=b'Public',
        )


def test_page_existing_ignores_unused_defaults_and_matches_supplied_notes(
    page_context: tuple[_PageContext, _Host],
) -> None:
    ctx, host = page_context
    host.page = _host.GitHubRelease(
        'v1.2.3', 'v1.2.3', 'notes', 'https://example.invalid/page', draft=False, prerelease=False
    )
    ctx = replace(
        ctx,
        _project=replace(
            ctx._project,
            release_page=_config.ReleasePageSettings(enabled=True, changelog='missing'),
        ),
    )
    assert _page_plan(ctx).reason == 'exists'
    assert (
        _page_plan(replace(ctx, _release_page=_releases.PageRequest(notes=b'notes'))).reason
        == 'matches'
    )
    with pytest.raises(PubanonError) as caught:
        _page_plan(replace(ctx, _release_page=_releases.PageRequest(notes=b'different')))
    assert 'release show v1.2.3 --project=example' in caught.value.render()
    assert 'release edit v1.2.3 --project=example' in caught.value.render()
    assert host.writes == []


def test_page_lookup_failure_has_skip_recovery(
    page_context: tuple[_PageContext, _Host], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, _host_facts = page_context

    def _fail(_dest: _host.Destination) -> tuple[_host.GitHubRelease, ...]:
        raise PubanonError('lookup failed')

    monkeypatch.setattr(_host, 'all_releases', _fail)
    with pytest.raises(PubanonError) as caught:
        _page_plan(ctx)
    recovery = caught.value.recovery or ''
    assert 'rerun with --no-release-page' in recovery
    assert 'pubanon release create v1.2.3 --project=example' in recovery
    assert 'pubanon publish' not in recovery


@pytest.mark.parametrize(
    'case',
    [
        (True, True, 'created', 'Release page: https://example.invalid/page (draft)', True),
        (True, False, 'created', 'Release page: https://example.invalid/page', False),
        (
            True,
            True,
            'existing',
            'Release page already exists: https://example.invalid/page (draft)',
            True,
        ),
        (False, True, 'kept', 'Release page: https://example.invalid/page (draft)', False),
        (False, False, 'kept', 'Release page: https://example.invalid/page', False),
    ],
)
def test_page_outcome_marks_drafts_and_guides_only_draft_projects(
    literal_release_context: _Context,
    capsys: pytest.CaptureFixture[str],
    case: tuple[bool, bool, Literal['created', 'existing', 'kept'], str, bool],
) -> None:
    drafts, draft, kind, line, guided = case
    _paths, _g, p, _host_facts = literal_release_context
    p = replace(p, release_page=_config.ReleasePageSettings(enabled=True, draft=drafts))

    _releases.show_page_outcome(
        p, _releases.PageOutcome(kind, 'https://example.invalid/page', draft=draft)
    )

    out = capsys.readouterr().out
    guidance = (
        'It appears when your release workflow publishes it after the package upload. If it is '
        'still a draft after that workflow finishes, rerun the workflow or publish the page on '
        'GitHub.'
    )
    assert f'{line}\n' in out
    assert (guidance in out) is guided


@pytest.mark.parametrize('enabled', [None, False])
def test_create_defaults_render_published_page_without_notes_file(
    release_context: _Context, enabled: bool | None
) -> None:
    paths, g, p, host = release_context
    if enabled is False:
        p = replace(p, release_page=_config.ReleasePageSettings(enabled=False))
    missing = _releases.prepare_change(
        paths, g, p, _releases.ReleaseRequest('create', _TAG, None, None)
    )
    assert missing.title == _TAG
    assert missing.notes == b''
    assert missing.changelog_missing
    _publish_changelog(release_context)
    present = _releases.prepare_change(
        paths, g, p, _releases.ReleaseRequest('create', _TAG, None, None)
    )
    assert present.notes == (
        b'See the [changelog](https://github.com/public-owner/public-repo/blob/'
        b'v1.2.3/CHANGELOG.md).\n'
    )
    assert not present.changelog_missing
    host.page = _host.GitHubRelease(
        _TAG,
        present.title,
        present.notes.decode(),
        'https://example.invalid/page',
        draft=False,
        prerelease=False,
    )
    assert (
        _releases.prepare_change(
            paths, g, p, _releases.ReleaseRequest('create', _TAG, None, None)
        ).operation
        == 'existing'
    )
    assert host.writes == []


@pytest.mark.parametrize(
    ('project', 'filename', 'title'),
    [
        ('line\nbreak', 'notes.md', 'Explicit title'),
        ('demo', 'notes\nfile.md', 'Explicit title'),
        ('demo', 'notes.md', 'Explicit\ttitle'),
        ('my project', 'notes file.md', 'Explicit title'),
    ],
)
def test_create_command_preserves_explicit_overrides(
    tmp_path: Path, project: str, filename: str, title: str
) -> None:
    notes_file = tmp_path / filename
    command = _releases.create_command(project, 'v1.2.3', title=title, notes_file=notes_file)
    if project == 'my project':
        assert shlex.split(command) == [
            'pubanon',
            'release',
            'create',
            'v1.2.3',
            '--title',
            title,
            '--notes-file',
            str(notes_file),
            '--project=my project',
        ]
        return
    assert command.startswith('Choose project ')
    assert 'release create v1.2.3 --title ' + title.replace('\t', r'\x09') in command
    displayed_path = str(notes_file).replace('\\', '\\\\').replace('\n', r'\x0a')
    assert '--notes-file ' + displayed_path in command
    assert '\n' not in command
    assert '\t' not in command


@pytest.mark.parametrize(
    'args', [('release', 'show', 'v1.2.3'), ('repo', 'restore', '--to', 'local')]
)
def test_command_fallback_preserves_required_arguments(args: tuple[str, ...]) -> None:
    command = _releases._command('line\nbreak', *args)
    assert ' '.join(args) in command
    assert 'line\\x0abreak' in command
    assert '\n' not in command


@pytest.mark.parametrize(
    ('drafts', 'existing_draft', 'same'),
    [(True, True, True), (True, False, True), (False, True, False), (False, False, True)],
)
def test_existing_page_matches_by_content_and_the_draft_setting(
    page_context: tuple[_PageContext, _Host],
    *,
    drafts: bool,
    existing_draft: bool,
    same: bool,
) -> None:
    ctx, host = page_context
    project = replace(
        ctx._project, release_page=_config.ReleasePageSettings(enabled=True, draft=drafts)
    )
    ctx = replace(ctx, _project=project, _release_page=_releases.PageRequest(notes=b'Notes\n'))
    paths, g = ctx._paths, ctx._global_config
    request = _releases.ReleaseRequest('create', _TAG, None, b'Notes\n')
    change = _releases.prepare_change(paths, g, project, request)
    host.page = replace(_matching(change), draft=existing_draft)

    if same:
        assert _page_plan(ctx).reason == 'matches'
        assert _releases.prepare_change(paths, g, project, request).operation == 'existing'
        assert not _releases.apply_change(paths, g, project, change).written
    else:
        with pytest.raises(PubanonError, match='different content or status'):
            _page_plan(ctx)
        with pytest.raises(PubanonError, match='different content or status'):
            _releases.prepare_change(paths, g, project, request)
        with pytest.raises(PubanonError, match='different content or status'):
            _releases.apply_change(paths, g, project, change)
    assert host.writes == []


def test_page_plan_keeping_a_page_lists_no_waiting_drafts(
    page_context: tuple[_PageContext, _Host],
) -> None:
    ctx, host = page_context
    host.pages = (_listed('v1.0.0', draft=True),)
    host.page = _listed(_TAG, draft=False)

    plan = _page_plan(ctx)

    assert (plan.action, plan.waiting) == ('keep', ())


def test_page_plan_lists_lower_version_drafts_oldest_first(
    page_context: tuple[_PageContext, _Host],
) -> None:
    ctx, host = page_context
    host.pages = (
        _listed('v1.0.0', draft=True),
        _listed('v2.0.0', draft=True),
        _listed('v0.10.0', draft=True),
        _listed('v1.1.0', draft=False),
        _listed('v0.9.0', draft=True),
        _listed('nightly', draft=True),
    )

    plan = _page_plan(ctx)

    assert (plan.action, plan.waiting) == ('create', ('v0.9.0', 'v0.10.0', 'v1.0.0'))
    assert host.scans == 1


def test_page_preview_empty_defaults_and_supplied_file(
    page_context: tuple[_PageContext, _Host], capsys: pytest.CaptureFixture[str]
) -> None:
    ctx, _host_facts = page_context
    plan = _page_plan(ctx)
    _releases.show_page_plan(plan)
    rendered = capsys.readouterr().out
    assert 'create "v1.2.3" with empty notes' in rendered
    assert 'no CHANGELOG.md' in rendered
    assert 'release_page.changelog' in rendered
    path = ctx._paths.data_dir / 'notes.md'
    ctx = replace(
        ctx, _release_page=_releases.PageRequest(notes=b'full\r\nlast\n', notes_file=path)
    )
    _releases.show_page_plan(_page_plan(ctx))
    out = capsys.readouterr().out
    assert 'create "v1.2.3" with notes from ' in out
    assert 'notes.md' in out
    assert 'full\\x0d\nlast\n' in out


def test_page_replacement_uses_correction_tag_not_replayed_tags(
    page_context: tuple[_PageContext, _Host],
) -> None:
    ctx, host = page_context
    record = replace(
        ctx._record,
        tag_intent=None,
        replaces='a' * 40,
        replace_scope='one',
        prestate_main='b' * 40,
        tip='c' * 40,
        poststate_tags={'v1.0.0': ctx._record.commit, 'v2.0.0': 'c' * 40},
    )
    ctx = replace(ctx, _record=record)
    assert _releases.page_tags(record) == ('v1.0.0',)
    plan = _page_plan(ctx)
    assert plan.tag == 'v1.0.0'
    assert plan.change is not None
    assert plan.change.commit == record.commit
    assert host.scans == 1
    assert _releases.page_tags(replace(record, tag_intent='v3.0.0')) == ('v3.0.0',)


def test_rejects_invalid_utf8_metadata_after_publication_verification(
    release_context: _Context,
) -> None:
    with pytest.raises(PubanonError):
        _prepare(release_context, operation='create', title=None, notes=b'\xff')
    assert release_context[3].writes == []


def test_render_page_defaults_use_only_published_tree(release_context: _Context) -> None:
    paths, _g, p, host = release_context
    p.source_path.mkdir()
    (p.source_path / 'CHANGELOG.md').write_text('Unpublished\n')
    missing = _releases._render_page(paths, p, _TAG, host.commit)
    assert missing == _releases._PlannedPage(_TAG, b'', None, changelog_missing=True)

    _publish_changelog(release_context)
    (p.source_path / 'CHANGELOG.md').write_text('Changed again\n')
    present = _releases._render_page(paths, p, _TAG, host.commit)
    assert present == _releases._PlannedPage(
        _TAG,
        b'See the [changelog](https://github.com/public-owner/public-repo/blob/v1.2.3/CHANGELOG.md).\n',
        None,
        changelog_missing=False,
    )


def test_render_page_rejects_blank_rendered_title(literal_release_context: _Context) -> None:
    paths, _g, p, host = literal_release_context
    p = replace(p, release_page=_config.ReleasePageSettings(title='   ', notes=''))
    with pytest.raises(PubanonError, match='title'):
        _releases._render_page(paths, p, _TAG, host.commit)


def test_render_page_requires_explicit_changelog_when_link_needed(
    release_context: _Context,
) -> None:
    paths, _g, p, host = release_context
    p = replace(p, release_page=_config.ReleasePageSettings(changelog='MISSING.md', title='   '))
    with pytest.raises(PubanonError, match=r'MISSING\.md') as caught:
        _releases._render_page(paths, p, _TAG, host.commit)
    assert 'release_page.changelog' in caught.value.render()

    p = replace(
        p,
        release_page=_config.ReleasePageSettings(notes='See {changelog_url}.', changelog=None),
    )
    with pytest.raises(PubanonError, match=r'\{changelog_url\}.*CHANGELOG\.md') as caught:
        _releases._render_page(paths, p, _TAG, host.commit)
    assert 'release_page.changelog' in (caught.value.recovery or '')
    assert 'release_page.changelog' not in str(caught.value)


def test_render_page_templates_and_overrides(literal_release_context: _Context) -> None:
    paths, _g, p, host = literal_release_context
    one_line = replace(
        p,
        release_page=_config.ReleasePageSettings(
            title='Version {version} {{stable}}',
            notes='Release {tag}: {version}.',
        ),
    )
    block = replace(
        one_line,
        release_page=_config.ReleasePageSettings(
            title='Version {version} {{stable}}',
            notes='Release {tag}: {version}.\r\n\n',
        ),
    )
    expected = _releases._PlannedPage(
        'Version 1.2.3 {stable}', b'Release v1.2.3: 1.2.3.\n', None, changelog_missing=False
    )
    assert _releases._render_page(paths, one_line, _TAG, host.commit) == expected
    assert _releases._render_page(paths, block, _TAG, host.commit) == expected
    empty = replace(p, release_page=_config.ReleasePageSettings(notes=''))
    assert _releases._render_page(paths, empty, _TAG, host.commit).notes == b''
    notes_file = paths.data_dir / 'notes.md'
    assert _releases._render_page(
        paths,
        one_line,
        _TAG,
        host.commit,
        request=_releases.ReleaseRequest('create', _TAG, 'Supplied', b'CRLF\r\nNo LF', notes_file),
    ) == _releases._PlannedPage('Supplied', b'CRLF\r\nNo LF', notes_file, changelog_missing=False)


def test_render_page_without_link_never_reads_changelog(
    literal_release_context: _Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, _g, p, host = literal_release_context
    p = replace(
        p,
        release_page=_config.ReleasePageSettings(changelog='MISSING.md', notes='Release {tag}'),
    )

    def _unexpected_lookup(*_args: object) -> bool:
        pytest.fail('unused changelog was inspected')

    monkeypatch.setattr(_candidate, 'regular_file_at', _unexpected_lookup)
    assert _releases._render_page(paths, p, _TAG, host.commit).notes == b'Release v1.2.3\n'
    assert (
        _releases._render_page(
            paths,
            p,
            _TAG,
            host.commit,
            request=_releases.ReleaseRequest('create', _TAG, None, b'Override'),
        ).notes
        == b'Override'
    )
    literal = replace(p, release_page=_config.ReleasePageSettings(notes='{{changelog_url}}'))
    assert _releases._render_page(paths, literal, _TAG, host.commit).notes == b'{changelog_url}\n'


@pytest.mark.parametrize(
    'boundary', ['local-tag', 'local-main', 'remote-tag', 'remote-mismatch', 'store']
)
def test_requires_recorded_matching_publication(
    release_context: _Context, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    paths, g, p, host = release_context
    store = _store.candidate_store_path(paths, p.name)
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

        def _without_tag(*_args: object, **_kwargs: object) -> _host.RemoteRefs:
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
        assert not _store.candidate_store_path(paths, p.name).exists()


@pytest.mark.parametrize('tag', ['1.2.3', 'v01.2.3', 'v1.2.3-rc1'])
def test_requires_strict_tag(literal_release_context: _Context, tag: str) -> None:
    with pytest.raises(PubanonError, match=r'vX\.Y\.Z'):
        _prepare(literal_release_context, tag=tag)


@pytest.mark.parametrize(
    ('operation', 'title', 'notes'),
    [('edit', None, None), ('create', '', b'')],
)
def test_rejects_invalid_metadata_before_publication_verification(
    literal_release_context: _Context,
    operation: Literal['create', 'edit'],
    title: str | None,
    notes: bytes | None,
) -> None:
    with pytest.raises(PubanonError):
        _prepare(literal_release_context, operation=operation, title=title, notes=notes)
    assert literal_release_context[3].writes == []


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
    release_context = _drafting(release_context)
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
    assert not change.draft
    result = _releases.apply_change(paths, g, p, change)
    assert result.written
    assert result.page.title == ('New title' if title_only else 'Old title')
    assert result.page.notes == ('Retained\r\n' if title_only else '')
    assert result.page.draft
    assert result.page.prerelease


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
    assert f'release create {_TAG}' in (caught.value.recovery or '')
    assert '--project=example' in (caught.value.recovery or '')
    assert '\x1b' not in caught.value.render()
    if outcome == 'cancelled':
        assert isinstance(caught.value, OperationCanceled)


def test_unknown_publication_requires_resolution(
    literal_release_context: _Context, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _unknown(*_args: object) -> SimpleNamespace:
        return SimpleNamespace(state=_records.STATE_UNKNOWN_OUTCOME)

    monkeypatch.setattr(_records, 'load_candidate', _unknown)
    with pytest.raises(PubanonError, match='unknown') as caught:
        _prepare(literal_release_context)
    assert 'publish' in (caught.value.recovery or '')
    assert '--resolve' in (caught.value.recovery or '')
    assert literal_release_context[3].writes == []


@pytest.mark.parametrize(('tag_seconds', 'read_seconds'), [(3.0, 2.0), (10.0, 0.0)])
def test_verification_budget_includes_requests_and_tag(
    release_context: _Context,
    tag_seconds: float,
    read_seconds: float,
) -> None:
    paths, g, p, host = release_context
    change = _prepare(release_context)
    host.after_write = 'missing'
    host.tag_seconds, host.read_seconds = tag_seconds, read_seconds
    with pytest.raises(_releases.ReleaseOutcomeUnknownError):
        _releases.apply_change(paths, g, p, change)
    assert host.clock.now == 110.0
    assert host.timeouts == [10.0]
    assert all(deadline == 110.0 for deadline in host.deadlines)
    assert len(host.writes) == 1
    if tag_seconds == 10.0:
        assert host.deadlines == []


@pytest.mark.parametrize('failure', [ObservationTimeoutError(), PubanonError('read failed')])
def test_read_failure_is_not_absence(release_context: _Context, failure: Exception) -> None:
    paths, g, p, host = release_context
    change = _prepare(release_context)
    host.read_error = failure
    with pytest.raises(_releases.ReleaseOutcomeUnknownError, match='could not verify'):
        _releases.apply_change(paths, g, p, change)
    assert host.clock.waits == []
    assert len(host.writes) == 1
    assert len(host.deadlines) == 1


@pytest.mark.parametrize('stale', [True, False])
def test_unverified_edit_uses_show_recovery(release_context: _Context, stale: bool) -> None:
    paths, g, p, host = release_context
    host.page = _matching(_prepare(release_context))
    change = _prepare(release_context, operation='edit', title='New title')
    host.stale = [host.page] * 20 if stale else [None]
    with pytest.raises(_releases.ReleaseOutcomeUnknownError) as caught:
        _releases.apply_change(paths, g, p, change)
    assert f'release show {_TAG}' in (caught.value.recovery or '')
    assert 'release create' not in (caught.value.recovery or '')
    assert host.clock.waits == ([0.5, 1.0, 2.0, 4.0, 2.5] if stale else [])
    assert len(host.writes) == 1


@pytest.mark.parametrize('operation', ['create', 'edit'])
def test_mismatch_names_fields_with_bounded_escaped_excerpts(
    release_context: _Context,
    operation: Literal['create', 'edit'],
) -> None:
    paths, g, p, host = release_context
    initial = _prepare(release_context, notes=b'First\nOriginal\n')
    if operation == 'edit':
        host.page = _matching(initial)
    change = _prepare(
        release_context, operation=operation, title='Wanted', notes=b'First\nWanted\n'
    )
    host.stale = [
        replace(
            _matching(change),
            title='\x1b' * 200,
            notes='First\n' + '\r' * 200,
            draft=True,
            prerelease=True,
            tag='v9.9.9',
        )
    ]
    with pytest.raises(_releases.ReleaseOutcomeUnknownError) as caught:
        _releases.apply_change(paths, g, p, change)
    fact = caught.value.fact or ''
    for field_name in ('tag', 'title', 'notes line 2', 'draft', 'prerelease'):
        assert field_name in fact
    assert '\x1b' not in fact
    assert '\\x1b' in fact
    assert '\\x0d' in fact
    assert '\\x1b' * 31 not in fact
    assert '\\x0d' * 31 not in fact
    assert r'\\x1b' not in fact
    assert r'\\x0d' not in fact
    assert all(
        len(excerpt) <= 120 for excerpt in re.findall(r"(?:expected|observed) '([^']*)'", fact)
    )
    assert host.clock.waits == []
    assert len(host.writes) == 1


@pytest.mark.parametrize(
    ('other', 'expected'),
    [
        (('v0.10.0', False, False), False),
        (('v0.8.0', False, False), True),
        (('v0.10.0', True, False), True),
        (('v0.10.0', False, True), True),
        (('v0.10.0-rc1', False, False), True),
        (('other', False, False), True),
    ],
)
def test_latest_uses_published_numeric_versions(
    release_context: _Context,
    other: tuple[str, bool, bool],
    expected: bool,
) -> None:
    paths, g, p, host = release_context
    host.tag = 'v0.9.0'
    _git(
        _store.candidate_store_path(paths, p.name),
        'update-ref',
        'refs/tags/v0.9.0',
        host.commit,
    )
    change = _prepare(release_context, tag=host.tag)
    tag, draft, prerelease = other
    host.pages = (_host.GitHubRelease(tag, 'Title', '', '', draft, prerelease),)
    result = _releases.apply_change(paths, g, p, change)
    assert result.written
    assert host.scans == 1
    assert host.latest == [expected]


@pytest.mark.parametrize('mode', ['untagged', 'ambiguous', 'skipped', 'disabled'])
def test_page_inactive_actions_do_not_lookup_or_render(
    page_context: tuple[_PageContext, _Host], mode: str
) -> None:
    ctx, host = page_context
    settings = _config.ReleasePageSettings(enabled=mode != 'disabled', changelog='missing.md')
    ctx = replace(ctx, _project=replace(ctx._project, release_page=settings))
    if mode == 'untagged':
        ctx = replace(ctx, _record=replace(ctx._record, tag_intent=None))
    elif mode == 'ambiguous':
        ctx = replace(
            ctx,
            _record=replace(
                ctx._record,
                tag_intent=None,
                prestate_tags={
                    'v2.0.0': ctx._record.commit,
                    'v1.0.0': ctx._record.commit,
                    'other': ctx._record.commit,
                },
            ),
        )
    elif mode == 'skipped':
        ctx = replace(ctx, _release_page=_releases.PageRequest(skip=True))
    plan = _page_plan(ctx)
    assert (plan.action, plan.reason) == ('none', mode)
    assert host.scans == 0
    assert plan.candidates == (('v1.0.0', 'v2.0.0') if mode == 'ambiguous' else ())
    if mode == 'untagged':
        assert _page_plan(replace(ctx, _release_page=_releases.PageRequest(skip=True))) == plan
    if mode in {'untagged', 'ambiguous'}:
        ctx = replace(ctx, _release_page=_releases.PageRequest(notes=b'notes'))
        with pytest.raises(PubanonError) as caught:
            _page_plan(ctx)
        text = caught.value.render()
        if mode == 'untagged':
            assert '--release-notes-file requires a tagged publication' in text
        else:
            assert 'release create v1.0.0' in text
            assert 'release create v2.0.0' in text
        assert host.scans == 0

        ctx = replace(ctx, _release_page=_releases.PageRequest(requested=True))
        with pytest.raises(PubanonError) as caught:
            _page_plan(ctx)
        text = caught.value.render()
        if mode == 'untagged':
            assert '--release-page requires a tagged publication' in text
        else:
            assert '--release-page requires one unambiguous publication tag' in text
        assert host.scans == 0


@pytest.mark.parametrize('settings', [None, _config.ReleasePageSettings(changelog='missing.md')])
def test_page_plan_is_disabled_unless_pages_are_enabled(
    page_context: tuple[_PageContext, _Host], settings: _config.ReleasePageSettings | None
) -> None:
    ctx, host = page_context
    ctx = replace(ctx, _project=replace(ctx._project, release_page=settings))
    plan = _page_plan(ctx)
    assert (plan.action, plan.reason, plan.tag) == ('none', 'disabled', 'v1.2.3')
    assert host.scans == 0


@pytest.mark.parametrize('settings', [None, _config.ReleasePageSettings(enabled=False)])
def test_requested_page_is_planned_when_pages_are_off(
    page_context: tuple[_PageContext, _Host], settings: _config.ReleasePageSettings | None
) -> None:
    ctx, host = page_context
    ctx = replace(
        ctx,
        _project=replace(ctx._project, release_page=settings),
        _release_page=_releases.PageRequest(requested=True),
    )
    plan = _page_plan(ctx)
    assert (plan.action, plan.reason, plan.tag) == ('create', 'new', 'v1.2.3')
    assert host.scans == 1
    assert host.writes == []


@pytest.mark.parametrize('reason', ['disabled', 'skipped'])
def test_finished_publication_without_page_shows_create_command(
    reason: str, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _releases.PagePlan(
        'none',
        cast("Literal['disabled', 'skipped']", reason),
        tag='v1.0.0',
        project='example',
    )
    _releases.show_page_plan(plan, published=True)
    out = capsys.readouterr().out
    assert 'Release page' in out
    assert 'not created' in out
    assert 'Add one later with: ' in out
    assert 'release create v1.0.0 --project=example' in out


@pytest.mark.parametrize('draft', [False, True])
def test_page_preview_names_a_draft_creation(
    draft: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = _host.Destination('github', 'owner', 'repo', 'url', 'account', 'public')
    change = _releases.ReleaseChange(
        'create', destination, _TAG, 'a' * 40, 'Version 1.2.3', b'', None, draft=draft
    )

    _releases.show_page_plan(_releases.PagePlan('create', 'new', tag=_TAG, change=change))

    out = capsys.readouterr().out
    assert ('create draft "Version 1.2.3"' if draft else 'create "Version 1.2.3"') in out
    assert ('create draft' in out) is draft


@pytest.mark.parametrize(
    ('waiting', 'expected'),
    [
        (
            ('v0.6.0',),
            (
                'warning: The release page for v0.6.0 is still a draft. '
                'Rerun its release workflow, or publish the page on GitHub.'
            ),
        ),
        (
            ('v0.6.0', 'v0.6.1'),
            (
                'warning: The release pages for v0.6.0 and v0.6.1 are still drafts. '
                'Rerun their release workflows, or publish the pages on GitHub.'
            ),
        ),
        (
            ('v0.5.0', 'v0.6.0', 'v0.6.1'),
            (
                'warning: The release pages for v0.5.0, v0.6.0, and v0.6.1 are still drafts. '
                'Rerun their release workflows, or publish the pages on GitHub.'
            ),
        ),
    ],
)
def test_page_preview_warns_about_waiting_drafts(
    waiting: tuple[str, ...], expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    destination = _host.Destination('github', 'owner', 'repo', 'url', 'account', 'public')
    change = _releases.ReleaseChange(
        'create', destination, 'v0.7.0', 'a' * 40, 'v0.7.0', b'', None
    )

    _releases.show_page_plan(
        _releases.PagePlan('create', 'new', tag='v0.7.0', change=change, waiting=waiting)
    )

    assert f'{expected}\n' in capsys.readouterr().out


@pytest.mark.parametrize('draft', [False, True])
def test_page_preview_existing_status(draft: bool, capsys: pytest.CaptureFixture[str]) -> None:
    page = _host.GitHubRelease(
        'v1.2.3', 'custom', '', 'https://example.invalid/page', draft=draft, prerelease=False
    )
    _releases.show_page_plan(_releases.PagePlan('keep', 'exists', tag=page.tag, existing=page))
    out = capsys.readouterr().out
    assert f'keep existing page ({"draft" if draft else "published"}) {page.url}' in out
    assert 'Notes' not in out


@pytest.mark.parametrize(
    ('reason', 'expected'),
    [
        ('untagged', ''),
        ('disabled', 'not created; release pages are off for this project'),
        ('skipped', 'skipped for this publication'),
        ('ambiguous', 'not created: v1.0.0 and v2.0.0 point here'),
    ],
)
def test_page_preview_inactive_rows(
    reason: str, expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    plan = _releases.PagePlan(
        'none',
        cast("Literal['untagged', 'disabled', 'skipped', 'ambiguous']", reason),
        candidates=('v1.0.0', 'v2.0.0') if reason == 'ambiguous' else (),
        project='example',
    )
    _releases.show_page_plan(plan)
    out = capsys.readouterr().out
    if reason == 'untagged':
        assert out == ''
    else:
        assert 'Release page' in out
        assert expected in out
    if reason == 'ambiguous':
        assert 'release create v1.0.0 --project=example' in out
        assert 'release create v2.0.0 --project=example' in out


@pytest.mark.parametrize('enabled', [True, False])
def test_page_supplied_notes_plan_exact_gated_candidate(
    page_context: tuple[_PageContext, _Host], enabled: bool
) -> None:
    ctx, host = page_context
    ctx = replace(
        ctx,
        _project=replace(ctx._project, release_page=_config.ReleasePageSettings(enabled=enabled)),
        _release_page=_releases.PageRequest(notes=b'Public\r\nnotes\n'),
    )
    plan = _page_plan(ctx)
    assert (plan.action, plan.reason, plan.tag) == ('create', 'new', 'v1.2.3')
    assert plan.change is not None
    assert plan.change.commit == ctx._record.commit
    assert plan.change.notes == b'Public\r\nnotes\n'
    assert host.scans == 1
    assert host.writes == []
    with pytest.raises(PubanonError, match='release content is blocked'):
        _page_plan(replace(ctx, _release_page=_releases.PageRequest(notes=b'Private Person')))


@pytest.mark.parametrize('present', [False, True])
def test_page_status_verifies_publication_without_writing(
    release_context: _Context, present: bool
) -> None:
    paths, g, p, host = release_context
    if present:
        host.page = _matching(_prepare(release_context))
    assert _releases.page_status(paths, g, p, _TAG) == host.page
    assert host.writes == []
    host.commit = 'b' * 40
    with pytest.raises(PubanonError, match='tag does not match'):
        _releases.page_status(paths, g, p, _TAG)


@pytest.mark.parametrize(
    'failure', [KeyboardInterrupt(), PubanonError('read failed'), ObservationTimeoutError()]
)
def test_complete_kept_page_read_failure_never_creates(
    release_context: _Context, monkeypatch: pytest.MonkeyPatch, failure: BaseException
) -> None:
    paths, g, p, host = release_context

    def _read(_destination: _host.Destination) -> tuple[_host.GitHubRelease, ...]:
        raise failure

    monkeypatch.setattr(_host, 'all_releases', _read)
    error = OperationCanceled if isinstance(failure, KeyboardInterrupt) else PubanonError
    with pytest.raises(error, match='the release page was not created') as caught:
        _releases.complete_publication_page(
            paths, g, p, _releases.PagePlan('keep', 'exists', tag=_TAG)
        )
    assert 'code was published' in caught.value.render()
    assert caught.value.recovery == 'pubanon release create v1.2.3 --project=example'
    assert host.writes == []


@pytest.mark.parametrize('field_name', ['tag', 'title', 'notes'])
def test_metadata_findings_bound_context_and_escape_controls_once(field_name: str) -> None:
    data = 'x' * 100 + '\x1bprivate' + 'y' * 100
    findings = _releases.release_metadata_findings(
        {_gate.RuleClass.IDENTITY_CRITICAL: ('private',)},
        tag=data if field_name == 'tag' else 'v1.2.3',
        title=data if field_name == 'title' else 'Public',
        notes=('first line\n' + data).encode() if field_name == 'notes' else b'Public',
    )
    location = 'notes line 2' if field_name == 'notes' else field_name
    assert findings == [
        f'{location}: identity_critical match private: ...'
        + 'x' * 59
        + r'\x1bprivate'
        + 'y' * 60
        + '...'
    ]
