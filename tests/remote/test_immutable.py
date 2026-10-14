import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from pubanon._config import ProjectConfig, ReleasePageSettings
from pubanon._errors import OperationCanceled, PubanonError
from pubanon._remote import _host, _immutable
from pubanon._remote._host import Destination, GitHubRelease, ImmutableReleases
from pubanon._terminal import color_context
from tests._setup import configure

_DESTINATION = Destination(
    'github',
    'anonymous-account',
    'public-repo',
    'https://github.com/anonymous-account/public-repo.git',
    'anonymous-account',
    'public',
)
_Script = Callable[[Sequence[str | tuple[str, str]]], None]


class _Releases:
    def __init__(self) -> None:
        self.account = 'anonymous-account'
        self.calls: list[str] = []
        self.fail: dict[str, list[BaseException | None]] = {}
        self.pages: dict[int, GitHubRelease] = {}
        self.setting = ImmutableReleases(enabled=False, enforced_by_owner=False)
        self.switch_after: str | None = None

    def _step(self, name: str) -> None:
        self.calls.append(name)
        outcomes = self.fail.get(name)
        error = outcomes.pop(0) if outcomes else None
        if error is not None:
            raise error
        if name == self.switch_after:
            self.account = 'other-account'

    def all_releases(
        self, _dest: Destination, *, deadline: float | None = None
    ) -> tuple[GitHubRelease, ...]:
        assert deadline is None
        self._step('list')
        return tuple(sorted(self.pages.values(), key=lambda page: page.created_at, reverse=True))

    def convert_release(self, _dest: Destination, release_id: int, title: str) -> None:
        self._step(f'convert {release_id} {title}')
        self.pages[release_id] = replace(self.pages[release_id], immutable=True)

    def enable_immutable_releases(self, _owner: str, _repo: str) -> None:
        self._step('enable')
        self.setting = ImmutableReleases(enabled=True, enforced_by_owner=False)

    def immutable_releases(self, _owner: str, _repo: str) -> ImmutableReleases:
        self._step('read setting')
        return self.setting

    def read_release_by_id(self, _dest: Destination, release_id: int) -> GitHubRelease:
        self._step(f'read {release_id}')
        return self.pages[release_id]

    def require_authenticated_account(self, expected: str, *, authority: str) -> str:
        self._step('account')
        if self.account != expected:
            raise PubanonError(f'authenticated account does not match {authority}')
        return expected


def _page(
    identifier: int,
    tag: str,
    *,
    title: str | None = None,
    draft: bool = False,
    immutable: bool = False,
) -> GitHubRelease:
    day = 20 + identifier
    return GitHubRelease(
        tag,
        tag if title is None else title,
        'Notes',
        f'https://github.com/anonymous-account/public-repo/releases/tag/{tag}',
        draft,
        prerelease=False,
        identifier=identifier,
        immutable=immutable,
        created_at=f'2026-09-{day}T10:00:00Z',
        published_at=None if draft else f'2026-09-{day}T10:05:00Z',
    )


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ProjectConfig:
    _paths, _global_config, project = configure(tmp_path, monkeypatch, tmp_path)
    assert project is not None
    return replace(project, release_page=ReleasePageSettings(enabled=True))


@pytest.fixture
def releases(monkeypatch: pytest.MonkeyPatch) -> _Releases:
    fake = _Releases()
    for name in (
        'all_releases',
        'convert_release',
        'enable_immutable_releases',
        'immutable_releases',
        'read_release_by_id',
        'require_authenticated_account',
    ):
        monkeypatch.setattr(_host, name, getattr(fake, name))
    return fake


@pytest.mark.parametrize('case', ['no pages and pages off', 'all immutable', 'enforced'])
def test_offer_asks_nothing_when_no_question_applies(
    project: ProjectConfig, releases: _Releases, case: str
) -> None:
    enabled = case != 'no pages and pages off'
    if case == 'all immutable':
        releases.setting = ImmutableReleases(enabled=True, enforced_by_owner=False)
        releases.pages = {1: _page(1, 'v0.1.0', immutable=True)}
    elif case == 'enforced':
        releases.setting = ImmutableReleases(enabled=False, enforced_by_owner=True)
        releases.pages = {1: _page(1, 'v0.1.0')}

    _immutable.offer(
        replace(project, release_page=ReleasePageSettings(enabled=enabled)), _DESTINATION
    )

    assert releases.calls == (
        ['list'] if case == 'no pages and pages off' else ['list', 'read setting']
    )


def test_offer_converts_remaining_pages_when_the_setting_is_on(
    project: ProjectConfig,
    releases: _Releases,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    releases.setting = ImmutableReleases(enabled=True, enforced_by_owner=False)
    releases.pages = {1: _page(1, 'v0.1.0', immutable=True), 2: _page(2, 'v0.2.0')}
    answers([('Make the 1 existing release page immutable? [y/n] ', 'y')])

    with color_context('never'):
        _immutable.offer(project, _DESTINATION)

    assert 'enable' not in releases.calls
    assert releases.pages[2].immutable
    lines = capsys.readouterr().out.splitlines()
    assert 'Immutable releases: on' in lines[0]
    assert re.search(r'\b1 release page is still mutable\b', lines[0])
    assert 'Immutable releases: on' in lines[-1]
    assert re.search(r'\b1 existing release page\b', lines[-1])


def test_offer_converts_using_the_current_title(
    project: ProjectConfig,
    releases: _Releases,
    answers: _Script,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    releases.pages = {1: _page(1, 'v0.1.0')}
    original = releases.read_release_by_id

    def _read(dest: Destination, release_id: int) -> GitHubRelease:
        releases.pages[release_id] = replace(releases.pages[release_id], title='Current title')
        return original(dest, release_id)

    monkeypatch.setattr(_host, 'read_release_by_id', _read)
    answers([('Turn on immutable releases, including the 1 existing release page? [y/n] ', 'y')])

    _immutable.offer(project, _DESTINATION)

    assert 'convert 1 Current title' in releases.calls
    assert releases.pages[1].notes == 'Notes'


def test_offer_ctrl_c_at_the_question_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, project: ProjectConfig, releases: _Releases
) -> None:
    releases.pages = {1: _page(1, 'v0.1.0')}

    def _interrupt(_prompt: object = '') -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr('builtins.input', _interrupt)

    with pytest.raises(OperationCanceled) as error:
        _immutable.offer(project, _DESTINATION)

    rendered = str(error.value)
    assert 'Canceled' in rendered
    assert 'repository is public' in rendered
    assert 'immutable releases are unchanged' in rendered
    assert releases.calls == ['list', 'read setting']


def test_offer_ctrl_c_during_conversion_reports_and_cancels(
    project: ProjectConfig, releases: _Releases, answers: _Script
) -> None:
    releases.pages = {1: _page(1, 'v0.1.0'), 2: _page(2, 'v0.2.0')}
    releases.fail['convert 2 v0.2.0'] = [KeyboardInterrupt()]
    answers([('Turn on immutable releases, including the 2 existing release pages? [y/n] ', 'y')])

    with pytest.raises(OperationCanceled) as error:
        _immutable.offer(project, _DESTINATION)

    assert str(error.value) == 'Canceled. The repository is public.'
    assert error.value.fact == ('Immutable releases: on\nImmutable: v0.1.0\nStill mutable: v0.2.0')


@pytest.mark.parametrize(
    'initial_failure',
    [PubanonError('enable failed'), KeyboardInterrupt()],
    ids=['mutation-failure', 'initial-interrupt'],
)
def test_offer_ctrl_c_during_outcome_recovery_reports_unknown_and_cancels(
    project: ProjectConfig,
    releases: _Releases,
    answers: _Script,
    initial_failure: BaseException,
) -> None:
    releases.fail['enable'] = [initial_failure]
    releases.fail['read setting'] = [None, KeyboardInterrupt()]
    answers([('Turn on immutable releases? [y/n] ', 'y')])

    with pytest.raises(OperationCanceled) as error:
        _immutable.offer(project, _DESTINATION)

    assert str(error.value) == 'Canceled. The repository is public.'
    assert error.value.fact is not None
    assert 'could not read the result' in error.value.fact
    assert 'Requests sent: turn on immutable releases' in error.value.fact
    assert 'outcome is unknown' in error.value.fact
    assert releases.calls == ['list', 'read setting', 'account', 'enable', 'read setting']


@pytest.mark.parametrize('enabled', [False, True])
def test_offer_decline_message_depends_on_the_setting(
    project: ProjectConfig,
    releases: _Releases,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
    enabled: bool,
) -> None:
    releases.setting = ImmutableReleases(enabled=enabled, enforced_by_owner=False)
    releases.pages = {1: _page(1, 'v0.1.0'), 2: _page(2, 'v0.2.0')}
    question = (
        'Make the 2 existing release pages immutable? [y/n] '
        if enabled
        else 'Turn on immutable releases, including the 2 existing release pages? [y/n] '
    )
    answers([(question, 'n')])

    _immutable.offer(project, _DESTINATION)

    out = capsys.readouterr().out
    assert ('Immutable releases stay on' if enabled else 'Immutable releases stay off') in out
    if enabled:
        assert re.search(r'\b2 existing release pages stay mutable\b', out)
    assert (
        'guide explains how to convert them' if enabled else 'guide explains how to turn them on'
    ) in out
    assert releases.calls == ['list', 'read setting']


def test_offer_escapes_host_text_in_failure_reports(
    project: ProjectConfig,
    releases: _Releases,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tag = 'v0.1.0\x1b[31m'
    releases.pages = {1: _page(1, tag)}
    releases.fail[f'convert 1 {tag}'] = [PubanonError('remote failure\x1b[31m')]
    answers([('Turn on immutable releases, including the 1 existing release page? [y/n] ', 'y')])

    with color_context('never'):
        _immutable.offer(project, _DESTINATION)

    output = capsys.readouterr().out
    assert '\x1b' not in output
    assert 'remote failure\\x1b[31m' in output


def test_offer_leaves_pages_without_a_title_mutable_and_names_them(
    project: ProjectConfig,
    releases: _Releases,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    releases.pages = {1: _page(1, 'v0.1.0', title=''), 2: _page(2, 'v0.2.0')}
    answers([('Turn on immutable releases, including the 1 existing release page? [y/n] ', 'y')])

    _immutable.offer(project, _DESTINATION)

    assert not releases.pages[1].immutable
    assert 'convert 1 ' not in ' '.join(releases.calls)
    rendered = capsys.readouterr().out
    assert 'Release pages without a title stay mutable: v0.1.0' in rendered
    assert '"Immutable releases"' in rendered


@pytest.mark.parametrize(
    'field', ['title', 'notes', 'tag', 'draft', 'prerelease', 'created_at', 'published_at']
)
def test_offer_refuses_changed_content_after_conversion(
    project: ProjectConfig,
    releases: _Releases,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    field: str,
) -> None:
    releases.pages = {1: _page(1, 'v0.1.0')}
    original = releases.convert_release

    def _convert(dest: Destination, release_id: int, title: str) -> None:
        original(dest, release_id, title)
        changes = {field: True if field in {'draft', 'prerelease'} else 'Changed'}
        releases.pages[release_id] = replace(releases.pages[release_id], **changes)

    monkeypatch.setattr(_host, 'convert_release', _convert)

    def _accept(_prompt: str) -> str:
        return 'y'

    monkeypatch.setattr('builtins.input', _accept)

    _immutable.offer(project, _DESTINATION)

    assert 'did not read back immutable and unchanged' in capsys.readouterr().out


def test_offer_reports_unknown_outcomes_when_rereading_fails(
    project: ProjectConfig,
    releases: _Releases,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    releases.pages = {1: _page(1, 'v0.1.0')}
    releases.fail['convert 1 v0.1.0'] = [PubanonError('release edit failed')]
    releases.fail['list'] = [None, PubanonError('listing failed')]
    answers([('Turn on immutable releases, including the 1 existing release page? [y/n] ', 'y')])

    with color_context('never'):
        _immutable.offer(project, _DESTINATION)

    out = capsys.readouterr().out
    assert 'could not read the result' in out
    assert 'Requests sent: turn on immutable releases; convert v0.1.0' in out
    assert 'outcome is unknown' in out


def test_offer_reports_what_it_reads_after_a_failure(
    project: ProjectConfig,
    releases: _Releases,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    releases.pages = {1: _page(1, 'v0.1.0'), 2: _page(2, 'v0.2.0')}
    releases.fail['convert 2 v0.2.0'] = [PubanonError('release edit failed')]
    answers([('Turn on immutable releases, including the 2 existing release pages? [y/n] ', 'y')])

    with color_context('never'):
        _immutable.offer(project, _DESTINATION)

    lines = capsys.readouterr().out.splitlines()
    assert lines[-5:] == [
        'warning: immutable releases did not finish: release edit failed',
        '  Immutable releases: on',
        '  Immutable: v0.1.0',
        '  Still mutable: v0.2.0',
        '  See "Immutable releases" in the guide.',
    ]


def test_offer_turns_on_the_setting_and_converts_pages_oldest_first(
    project: ProjectConfig,
    releases: _Releases,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    releases.pages = {
        1: _page(1, 'v0.1.0'),
        2: _page(2, 'v0.2.0'),
        3: _page(3, 'v0.3.0', draft=True),
    }
    answers([('Turn on immutable releases, including the 2 existing release pages? [y/n] ', 'y')])

    with color_context('never'):
        _immutable.offer(project, _DESTINATION)

    assert releases.calls == [
        'list',
        'read setting',
        'account',
        'enable',
        'read setting',
        'read 1',
        'account',
        'convert 1 v0.1.0',
        'read 1',
        'read 2',
        'account',
        'convert 2 v0.2.0',
        'read 2',
    ]
    before, explanation, after = capsys.readouterr().out.splitlines()
    assert before == 'Immutable releases: off'
    assert 'tag and files fixed' in explanation
    assert 'signs a record' in explanation
    assert 'titles and notes stay editable' in explanation
    assert 'version can never be used again' in explanation
    assert 'even in a recreated repository' in explanation
    assert 'cannot replace history that moves these tags' in explanation
    assert 'Immutable releases: on' in after
    assert re.search(r'\b2 existing release pages\b', after)


def test_offer_turns_on_the_setting_without_existing_pages(
    project: ProjectConfig,
    releases: _Releases,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers([('Turn on immutable releases? [y/n] ', 'y')])

    _immutable.offer(project, _DESTINATION)

    assert releases.setting.enabled
    assert 'Immutable releases: on.' in capsys.readouterr().out


@pytest.mark.parametrize('failure', ['setting', 'account', 'account switched'])
def test_offer_warns_instead_of_failing(
    project: ProjectConfig,
    releases: _Releases,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    releases.pages = {1: _page(1, 'v0.1.0')}
    if failure == 'setting':
        releases.fail['read setting'] = [PubanonError('read failed')]
    else:
        answers(
            [('Turn on immutable releases, including the 1 existing release page? [y/n] ', 'y')]
        )
    if failure == 'account':
        releases.account = 'other-account'
    elif failure == 'account switched':
        releases.switch_after = 'enable'
    with color_context('never'):
        _immutable.offer(project, _DESTINATION)

    output = capsys.readouterr().out
    if failure == 'setting':
        assert 'warning: could not read the immutable releases setting' in output
        assert '"Immutable releases"' in output
    elif failure == 'account':
        assert 'enable' not in releases.calls
        assert 'did not finish: authenticated account does not match configuration' in output
    elif failure == 'account switched':
        assert 'enable' in releases.calls
        assert not any(call.startswith('convert') for call in releases.calls)
        assert 'did not finish: authenticated account does not match configuration' in output


def test_offer_warns_when_the_terminal_cannot_answer(
    monkeypatch: pytest.MonkeyPatch,
    project: ProjectConfig,
    releases: _Releases,
    capsys: pytest.CaptureFixture[str],
) -> None:
    releases.pages = {1: _page(1, 'v0.1.0')}

    def _ended(_prompt: object = '') -> str:
        raise EOFError

    monkeypatch.setattr('builtins.input', _ended)

    with color_context('never'):
        _immutable.offer(project, _DESTINATION)

    assert releases.calls == ['list', 'read setting']
    rendered = capsys.readouterr().out
    assert 'warning: could not ask about immutable releases' in rendered
    assert 'repository is public' in rendered
    assert 'immutable releases are unchanged' in rendered
    assert '"Immutable releases"' in rendered
