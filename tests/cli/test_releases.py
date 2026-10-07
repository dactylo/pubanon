from pathlib import Path

import pytest

from pubanon import _cli, _config
from pubanon._cli import _releases
from pubanon._errors import PubanonError
from pubanon._remote import _host
from pubanon._remote import _releases as policy
from pubanon._snapshots import _candidate, _store
from pubanon._terminal import verbose_context


def _destination() -> _host.Destination:
    return _host.Destination('github', 'owner', 'repo', 'url', 'account', 'private')


def _page(
    *, notes: str = 'old', draft: bool = False, prerelease: bool = False
) -> _host.GitHubRelease:
    return _host.GitHubRelease(
        'v1.2.3', 'Title', notes, 'https://example.invalid/page', draft, prerelease
    )


def test_create_cancellation_never_applies(
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _paths, _global, project = configured_project
    notes_file = tmp_path / 'notes.md'
    notes_file.write_text('notes')

    def _prepare(
        _paths: _config.Paths,
        _g: _config.GlobalConfig,
        _p: _config.ProjectConfig,
        _request: policy.ReleaseRequest,
    ) -> policy.ReleaseChange:
        return policy.ReleaseChange(
            'create', _destination(), 'v1.2.3', 'a' * 40, 'Title', b'notes', None
        )

    def _no_write(
        _paths: _config.Paths,
        _g: _config.GlobalConfig,
        _p: _config.ProjectConfig,
        _change: policy.ReleaseChange,
    ) -> _host.GitHubRelease:
        pytest.fail('cancelled write')

    def _decline(_question: str) -> bool:
        return False

    monkeypatch.setattr(policy, 'prepare_change', _prepare)
    monkeypatch.setattr(_releases, 'confirm', _decline)
    monkeypatch.setattr(policy, 'apply_change', _no_write)
    assert (
        _cli.main(
            [
                'release',
                'create',
                'v1.2.3',
                '--project',
                project.name,
                '--notes-file',
                str(notes_file),
            ]
        )
        == 130
    )


def test_create_keeps_checked_bytes_and_lock_through_confirmation(
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _global, project = configured_project
    notes_file = tmp_path / 'notes.md'
    original = b'First\r\nSecond\n'
    notes_file.write_bytes(original)
    seen: list[bytes] = []
    commit = 'a' * 40
    _store.candidate_store_path(paths, project.name).mkdir(parents=True)
    page: list[_host.GitHubRelease | None] = [None]

    def _published(_paths: _config.Paths, _name: str) -> tuple[str, dict[str, str]]:
        return commit, {'v1.2.3': commit}

    def _verify(
        _paths: _config.Paths, _g: _config.GlobalConfig, _p: _config.ProjectConfig
    ) -> _host.Destination:
        return _destination()

    def _refs(
        _dest: _host.Destination,
        _paths: _config.Paths,
        _name: str,
        *,
        timeout: float | None = None,
    ) -> _host.RemoteRefs:
        assert timeout is None or 0 < timeout <= 10
        return _host.RemoteRefs(commit, {'v1.2.3': commit})

    def _read(
        _dest: _host.Destination,
        _tag: str,
        *,
        deadline: float | None = None,
    ) -> _host.GitHubRelease | None:
        assert deadline is None or deadline > 0
        return page[0]

    def _confirm(_question: str) -> bool:
        assert 'release' in _question.lower()
        notes_file.write_bytes(b'changed')
        with (
            pytest.raises(PubanonError, match='another pubanon command is running'),
            _config.project_lock(paths, project.name),
        ):
            pass
        return True

    def _write(
        _dest: _host.Destination,
        tag: str,
        *,
        title: str,
        notes: bytes,
        mark_latest: bool = True,
    ) -> None:
        assert mark_latest
        seen.append(notes)
        with (
            pytest.raises(PubanonError, match='another pubanon command is running'),
            _config.project_lock(paths, project.name),
        ):
            pass
        page[0] = _host.GitHubRelease(
            tag,
            title,
            notes.decode(),
            'https://example.invalid/page',
            draft=False,
            prerelease=False,
        )

    def _all(_dest: _host.Destination) -> tuple[_host.GitHubRelease, ...]:
        return ()

    monkeypatch.setattr(_candidate, 'published_refs', _published)
    monkeypatch.setattr(_host, 'verify_destination', _verify)
    monkeypatch.setattr(_host, 'ls_remote', _refs)
    monkeypatch.setattr(_host, 'read_release', _read)
    monkeypatch.setattr(_host, 'create_release', _write)
    monkeypatch.setattr(_host, 'all_releases', _all)
    monkeypatch.setattr(_releases, 'confirm', _confirm)
    assert (
        _cli.main(
            [
                'release',
                'create',
                'v1.2.3',
                '--project',
                project.name,
                '--notes-file',
                str(notes_file),
            ]
        )
        == 0
    )
    assert seen == [original]
    rendered = capsys.readouterr().out
    assert 'owner/repo' in rendered
    assert 'private' in rendered
    assert 'a' * 12 in rendered
    assert 'a' * 40 not in rendered
    assert 'First\\x0d\nSecond\n' in rendered
    with verbose_context(enabled=True):
        _releases._preview(
            policy.ReleaseChange(
                'create', _destination(), 'v1.2.3', commit, 'Title', original, None
            )
        )
    assert commit in capsys.readouterr().out


def test_create_reports_matching_page_without_claiming_write(
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _paths, _global, project = configured_project
    notes_file = tmp_path / 'notes.md'
    notes_file.write_bytes(b'old')

    def _prepare(*_args: object) -> policy.ReleaseChange:
        return policy.ReleaseChange(
            'existing',
            _destination(),
            'v1.2.3',
            'a' * 40,
            'Title',
            b'old',
            _page(),
        )

    def _apply(*_args: object) -> policy.ReleaseResult:
        pytest.fail('matching page was applied')

    def _confirm(_question: str) -> bool:
        pytest.fail('matching page prompted')

    monkeypatch.setattr(policy, 'prepare_change', _prepare)
    monkeypatch.setattr(policy, 'apply_change', _apply)
    monkeypatch.setattr(_releases, 'confirm', _confirm)
    assert (
        _cli.main(
            [
                'release',
                'create',
                'v1.2.3',
                '--project',
                project.name,
                '--notes-file',
                str(notes_file),
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert 'Release page already exists: https://example.invalid/page' in output
    assert 'Release page created:' not in output


def test_create_without_options_previews_defaults_and_missing_changelog_hint(
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _paths, _global, project = configured_project
    prepared: list[policy.ReleaseRequest] = []

    def _prepare(
        _paths: _config.Paths,
        _g: _config.GlobalConfig,
        _p: _config.ProjectConfig,
        request: policy.ReleaseRequest,
    ) -> policy.ReleaseChange:
        prepared.append(request)
        return policy.ReleaseChange(
            'create',
            _destination(),
            request.tag,
            'a' * 40,
            request.tag,
            b'',
            None,
            changelog_missing=True,
        )

    def _apply(*_args: object) -> policy.ReleaseResult:
        return policy.ReleaseResult(_page(notes=''), written=True)

    monkeypatch.setattr(policy, 'prepare_change', _prepare)
    monkeypatch.setattr(policy, 'apply_change', _apply)
    assert _cli.main(['release', 'create', 'v1.2.3', '--project', project.name, '--yes']) == 0
    assert prepared == [policy.ReleaseRequest('create', 'v1.2.3', None, None)]
    rendered = capsys.readouterr().out
    assert 'v1.2.3' in rendered
    assert (
        'No CHANGELOG.md in the published snapshot; set release_page.changelog to link one.'
        in rendered
    )


def test_edit_can_clear_notes_without_prompt(
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _paths, _global, project = configured_project
    notes_file = tmp_path / 'empty.md'
    notes_file.write_bytes(b'')
    seen: list[bytes] = []

    def _prepare(
        _paths: _config.Paths,
        _g: _config.GlobalConfig,
        _p: _config.ProjectConfig,
        request: policy.ReleaseRequest,
    ) -> policy.ReleaseChange:
        assert request.notes == b''
        assert request.notes_file == notes_file.absolute()
        return policy.ReleaseChange(
            'edit', _destination(), request.tag, 'a' * 40, 'Title', b'', _page()
        )

    def _apply(
        _paths: _config.Paths,
        _g: _config.GlobalConfig,
        _p: _config.ProjectConfig,
        change: policy.ReleaseChange,
    ) -> policy.ReleaseResult:
        seen.append(change.notes)
        return policy.ReleaseResult(_page(notes=''), written=True)

    def _unexpected_confirm(_question: str) -> bool:
        pytest.fail('--yes prompted')

    monkeypatch.setattr(policy, 'prepare_change', _prepare)
    monkeypatch.setattr(policy, 'apply_change', _apply)
    monkeypatch.setattr(_releases, 'confirm', _unexpected_confirm)
    assert (
        _cli.main(
            [
                'release',
                'edit',
                'v1.2.3',
                '--project',
                project.name,
                '--notes-file',
                str(notes_file),
                '--yes',
            ]
        )
        == 0
    )
    assert seen == [b'']


def test_empty_list_is_success(
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _paths, _global, project = configured_project

    def _verify(
        _paths: _config.Paths, _g: _config.GlobalConfig, _p: _config.ProjectConfig
    ) -> _host.Destination:
        return _destination()

    def _list(_dest: _host.Destination, *, limit: int) -> tuple[_host.GitHubRelease, ...]:
        assert limit == 30
        return ()

    monkeypatch.setattr(_host, 'verify_destination', _verify)
    monkeypatch.setattr(_host, 'list_releases', _list)
    assert _cli.main(['release', 'list', '--project', project.name]) == 0
    assert 'No release pages' in capsys.readouterr().out


def test_list_and_show_use_verified_destination_without_local_publication(
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _paths, _global, project = configured_project

    def _verify(
        _paths: _config.Paths, _g: _config.GlobalConfig, _p: _config.ProjectConfig
    ) -> _host.Destination:
        return _destination()

    def _list(_dest: _host.Destination, *, limit: int) -> tuple[_host.GitHubRelease, ...]:
        assert limit == 30
        return (_page(draft=True, prerelease=True),)

    def _read(_dest: _host.Destination, _tag: str) -> _host.GitHubRelease:
        return _page(notes='full\n\x1b[31mnotes')

    monkeypatch.setattr(_host, 'verify_destination', _verify)
    monkeypatch.setattr(_host, 'list_releases', _list)
    monkeypatch.setattr(_host, 'read_release', _read)
    assert _cli.main(['release', 'list', '--project', project.name]) == 0
    listed = capsys.readouterr().out
    assert 'owner/repo' in listed
    assert '30' in listed
    assert 'draft' in listed
    assert 'prerelease' in listed
    assert 'v1.2.3' in listed
    assert _cli.main(['release', 'show', 'v1.2.3', '--project', project.name]) == 0
    shown = capsys.readouterr().out
    assert 'full\n\\x1b[31mnotes' in shown
    assert 'https://example.invalid/page' in shown


@pytest.mark.parametrize('content', [b'\xff', None])
def test_notes_file_errors_precede_write(
    content: bytes | None,
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _paths, _global, project = configured_project
    notes_file = tmp_path / 'notes.md'
    if content is not None:
        notes_file.write_bytes(content)

    def _no_prepare(
        _paths: _config.Paths,
        _g: _config.GlobalConfig,
        _p: _config.ProjectConfig,
        _request: policy.ReleaseRequest,
    ) -> policy.ReleaseChange:
        pytest.fail('invalid input reached policy')

    monkeypatch.setattr(policy, 'prepare_change', _no_prepare)
    assert (
        _cli.main(
            [
                'release',
                'create',
                'v1.2.3',
                '--project',
                project.name,
                '--notes-file',
                str(notes_file),
            ]
        )
        == 1
    )
    assert 'notes' in capsys.readouterr().err.lower()


def test_release_recovery_qualifies_project_outside_its_directory(
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _paths, _global, project = configured_project
    monkeypatch.chdir(tmp_path)
    notes_file = tmp_path / 'notes.md'
    notes_file.write_text('public notes')

    def _already_exists(
        _paths: _config.Paths,
        _g: _config.GlobalConfig,
        _p: _config.ProjectConfig,
        _request: policy.ReleaseRequest,
    ) -> policy.ReleaseChange:
        raise PubanonError('release page already exists', recovery='pubanon release show v1.2.3')

    monkeypatch.setattr(policy, 'prepare_change', _already_exists)
    assert (
        _cli.main(
            [
                'release',
                'create',
                'v1.2.3',
                '--project',
                project.name,
                '--notes-file',
                str(notes_file),
            ]
        )
        == 1
    )
    assert 'pubanon release show --project=demo v1.2.3' in capsys.readouterr().err
