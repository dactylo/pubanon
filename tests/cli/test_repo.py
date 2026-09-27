import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Never

import pytest

from pubanon import (
    _cli,
    _config,
)
from pubanon._cli import _context
from pubanon._errors import ObservationTimeoutError, PubanonError
from pubanon._gate import RuleClass
from pubanon._remote import _host, _lifecycle, _package, _publish
from pubanon._snapshots import _audit, _candidate
from tests._cli import prepare, published_package_reveal_context, stub_remote
from tests._git import git_env
from tests._setup import configure


@pytest.mark.parametrize(
    'selection',
    ['private', 'public'],
)
def test_repo_create_uses_concrete_operation_and_holds_project_lock(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    selection: str,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    project = replace(project, visibility=selection)
    _config.save_project(paths, project)
    received: list[tuple[object, ...]] = []

    class _LifecycleModule:
        @staticmethod
        def _repo_create(*args: object, **kwargs: object) -> None:
            actual_paths, actual_global, actual_project = args

            with (
                pytest.raises(PubanonError, match='another Pubanon command is running'),
                _config.project_lock(paths, project.name),
            ):
                pytest.fail('repo create did not hold its project lock')
            received.append(
                (
                    actual_paths,
                    actual_global,
                    actual_project,
                    kwargs['visibility'],
                    kwargs['yes'],
                )
            )

    monkeypatch.setattr(_lifecycle, 'repo_create', _LifecycleModule._repo_create)

    assert _cli.main(['repo', 'create', '--project', project.name, '--yes']) == 0
    assert received == [(paths, global_config, project, selection, True)]


def test_repo_restore_local_prioritizes_unknown_candidate_recovery(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    _candidate.save_candidate(
        paths,
        project.name,
        replace(record, state=_candidate.STATE_UNKNOWN_OUTCOME),
    )
    host_accesses: list[None] = []

    def _unexpected_host_access(*_args: object, **_kwargs: object) -> Never:
        host_accesses.append(None)
        pytest.fail('repair contacted the host before snapshot recovery')

    monkeypatch.setattr(_host, 'verify_destination', _unexpected_host_access)
    monkeypatch.setattr(_host, 'ls_remote', _unexpected_host_access)

    assert _cli.main(['repo', 'restore', '--to', 'local', '--project', project.name]) == 1
    error = capsys.readouterr().err
    assert 'pubanon publish --project=demo --resolve' in error
    assert 'restore the host main branch' not in error
    assert host_accesses == []


def test_repo_restore_local_refuses_absent_host_main_before_rebuild(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    _paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account='anonymous-account',
        visibility=project.visibility,
    )

    def _verified_destination(
        _paths: _config.Paths,
        _global: _config.GlobalConfig,
        _project: _config.ProjectConfig,
    ) -> _host.Destination:
        return destination

    def _absent_main(
        _destination: _host.Destination,
        _paths: _config.Paths,
        _project: str,
    ) -> _host.RemoteRefs:
        return _host.RemoteRefs(None, {})

    def _unexpected_rebuild(
        _paths: _config.Paths,
        _project: str,
        _destination: _host.Destination,
    ) -> None:
        pytest.fail('repair ran without host main')

    monkeypatch.setattr(_host, 'verify_destination', _verified_destination)
    monkeypatch.setattr(_host, 'ls_remote', _absent_main)
    monkeypatch.setattr(_publish, 'rebuild_store', _unexpected_rebuild)

    assert _cli.main(['repo', 'restore', '--to', 'local', '--project', project.name]) == 1
    error = capsys.readouterr().err
    assert 'host main is absent' in error
    assert 'saved published history was not changed' in error


@pytest.mark.parametrize('settings_result', ['match', 'mismatch', 'unreadable'])
def test_repo_restore_local_reports_findings_and_late_settings_failure(
    settings_result: str,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    if settings_result != 'match':
        project = replace(
            project,
            settings=_config.Settings(
                'Public',
                'main',
                issues=True,
                wiki=False,
                projects=False,
                pull_requests=False,
                discussions=False,
            ),
        )
        _config.save_project(paths, project)
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account=global_config.accounts['github'],
        visibility=project.visibility,
    )
    stub_remote(monkeypatch, destination, _host.RemoteRefs('a' * 40, {}))
    findings = (
        _audit.AuditFinding('Private Alias/file', RuleClass.IDENTITY_CRITICAL, 'Private Alias'),
        _audit.AuditFinding('Internal Term/file', RuleClass.HYGIENE, 'Internal Term'),
    )
    evidence = tuple(
        _audit.HistoryEvidence(finding, 'c' * 40, 'filename bytes 0:13', 'filename', finding.unit)
        for finding in findings
    )

    def _rebuild(
        _paths: _config.Paths,
        _configs: tuple[_config.GlobalConfig, _config.ProjectConfig],
        _dest: _host.Destination,
        *,
        yes: bool,
    ) -> _publish.RepairResult:
        assert not yes
        return _publish.RepairResult(
            _host.RemoteRefs('c' * 40, {}), _audit.HistoryAudit(findings, (), evidence)
        )

    def _settings(_owner: str, _repo: str) -> _host.SettingsFacts:
        if settings_result == 'unreadable':
            raise PubanonError('simulated settings read failure')
        description = '' if settings_result == 'match' else 'Different'
        return _host.SettingsFacts(
            description,
            'main',
            issues=True,
            wiki=False,
            projects=False,
            pull_requests=False,
            discussions=False,
        )

    monkeypatch.setattr(_publish, 'rebuild_store', _rebuild)
    monkeypatch.setattr(_host, 'repository_settings', _settings)
    assert _cli.main(['repo', 'restore', '--to', 'local', '--project', project.name]) == (
        0 if settings_result == 'match' else 1
    )
    captured = capsys.readouterr()
    assert 'Rebuilt the saved published history' in captured.out
    assert 'historical exposure: Private Alias/file' in captured.out
    assert 'policy mismatch: Internal Term/file' in captured.out
    assert 'Repair did not acknowledge these findings.' in captured.out
    assert 'pubanon audit --project=demo --remote' in captured.out + captured.err
    assert 'no action is required' not in captured.out
    assert 'Private Alias' in captured.out + captured.err
    assert 'Internal Term' in captured.out + captured.err
    if settings_result != 'match':
        assert (
            'saved published history was rebuilt; host settings verification failed'
            in captured.err
        )
    assert _config.load_project(paths, project.name).acknowledged_exposure == ''


@pytest.mark.parametrize('noop', [False, True])
def test_repo_restore_local_reports_local_rebuild_without_claiming_host_change(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    noop: bool,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    _paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account=global_config.accounts['github'],
        visibility=project.visibility,
    )
    remote = _host.RemoteRefs('a' * 40, {})
    stub_remote(monkeypatch, destination, remote)

    def _repository_settings(_owner: str, _repo: str) -> _host.SettingsFacts:
        return _host.SettingsFacts(
            '',
            'main',
            issues=True,
            wiki=False,
            projects=False,
            pull_requests=False,
            discussions=False,
        )

    monkeypatch.setattr(_host, 'repository_settings', _repository_settings)
    rebuilt: list[None] = []

    def _rebuild_store(
        _paths: _config.Paths,
        _configs: tuple[_config.GlobalConfig, _config.ProjectConfig],
        _destination: _host.Destination,
        *,
        yes: bool,
    ) -> _publish.RepairResult:
        assert yes
        rebuilt.append(None)
        return _publish.RepairResult(
            _host.RemoteRefs('c' * 40, {}), _audit.HistoryAudit((), (), ())
        )

    def _local_refs(_ctx: _context.ProjectContext) -> tuple[str | None, dict[str, str]]:
        return ('c' * 40 if noop else None), {}

    monkeypatch.setattr(_context, 'local_refs', _local_refs)
    monkeypatch.setattr(_publish, 'rebuild_store', _rebuild_store)

    assert _cli.main(['repo', 'restore', '--to', 'local', '--project', project.name, '--yes']) == 0

    assert rebuilt == [None]
    output = capsys.readouterr().out
    expected = (
        'Local publication refs already match verified host refs; no replacement was needed.'
        if noop
        else 'Rebuilt the saved published history from verified host refs.'
    )
    assert output.startswith(expected + '\n')
    assert 'The host repository and source repository were not changed.\n' in output
    assert 'Next: no action is required.\n' not in output
    assert 'Expected refs:' not in output
    assert 'Host refs:' not in output
    assert 'Settings: match' in output
    assert 'a' * 40 not in output


def test_repo_reveal_renders_package_timeout(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, project = published_package_reveal_context(
        tmp_path,
        make_repo,
        monkeypatch,
        host_visibility='private',
    )
    private = 'private package timeout detail'

    def _timeout(*_args: object) -> _package.PackageRelease | None:
        raise ObservationTimeoutError(
            stdout=f'{private} stdout'.encode(),
            stderr=f'{private} stderr'.encode(),
        )

    def _unexpected_mutation(*_args: object) -> None:
        pytest.fail('package timeout reached the visibility mutation')

    monkeypatch.setattr(_package, 'release_state', _timeout)
    monkeypatch.setattr(_lifecycle, 'set_visibility', _unexpected_mutation)
    capsys.readouterr()

    assert _cli.main(['repo', 'reveal', '--project', project.name]) == 1

    result = capsys.readouterr()
    rendered = result.out + result.err
    assert 'error: package release observation timed out' in result.err
    assert 'Next: rerun pubanon repo reveal --project=demo' in result.err
    assert 'Traceback' not in rendered
    assert private not in rendered
    assert 'Repository is public and verified.' not in result.out
    assert _config.load_project(paths, project.name).visibility == 'private'


def test_repo_reveal_uses_concrete_operation_and_holds_project_lock(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    received: list[tuple[object, ...]] = []

    class _LifecycleModule:
        @staticmethod
        def _reveal(*args: object) -> None:
            actual_paths, actual_global, actual_project = args

            with (
                pytest.raises(PubanonError, match='another Pubanon command is running'),
                _config.project_lock(paths, project.name),
            ):
                pytest.fail('repo reveal did not hold its project lock')
            received.append(
                (
                    actual_paths,
                    actual_global,
                    actual_project,
                )
            )

    monkeypatch.setattr(_lifecycle, 'reveal', _LifecycleModule._reveal)

    assert _cli.main(['repo', 'reveal', '--project', project.name]) == 0
    assert received == [(paths, global_config, project)]


def test_repo_verify_mismatch_offers_both_recovery_directions(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    _candidate.store_path(paths, project.name)
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url=f'https://github.com/{project.owner}/{project.repo}.git',
        account=global_config.accounts['github'],
        visibility=project.visibility,
    )
    stub_remote(monkeypatch, destination, _host.RemoteRefs('a' * 40, {}))

    def _published_refs(_paths: _config.Paths, _project: str) -> tuple[str, dict[str, str]]:
        return 'b' * 40, {'v2.0.0': 'b' * 40}

    monkeypatch.setattr(_candidate, 'published_refs', _published_refs)
    assert _cli.main(['repo', 'verify', '--project', project.name]) == 1
    error = capsys.readouterr().err
    assert 'Saved snapshot history differs from GitHub.' in error
    assert 'If local saved publication history is correct' in error
    assert 'pubanon repo restore --project=demo --to remote' in error
    assert 'If GitHub history is correct' in error
    assert 'pubanon repo restore --project=demo --to local' in error
    assert 'outside-fix' in error
    assert not paths.global_config.with_suffix('.tmp').exists()


def test_repo_verify_refuses_only_drifted_declared_setting(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    settings = _config.Settings(
        description='Public repository',
        default_branch='main',
        issues=True,
        wiki=False,
        projects=True,
        pull_requests=False,
        discussions=False,
    )
    project = replace(project, settings=settings)
    _config.save_project(paths, project)
    _candidate.store_path(paths, project.name)
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account=global_config.accounts['github'],
        visibility=project.visibility,
    )
    stub_remote(monkeypatch, destination, _host.RemoteRefs(None, {}))

    def _repository_settings(_owner: str, _repo: str) -> _host.SettingsFacts:
        return _host.SettingsFacts(
            'Private Person details',
            settings.default_branch,
            settings.issues,
            settings.wiki,
            settings.projects,
            settings.pull_requests,
            settings.discussions,
        )

    monkeypatch.setattr(_host, 'repository_settings', _repository_settings)

    assert _cli.main(['repo', 'verify', '--project', project.name]) == 1
    error = capsys.readouterr().err
    assert 'error: host settings do not match the declared settings' in error
    assert '  description: expected Public repository; actual Private Person details' in error
    assert 'default_branch:' not in error
    assert 'issues:' not in error
    assert 'wiki:' not in error
    assert 'projects:' not in error
    assert 'Private Person' in error


def test_repo_verify_refuses_unexpected_destination_branch(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    _candidate.store_path(paths, project.name)
    commit = (
        subprocess.run(
            ['git', '-C', source, 'rev-parse', 'HEAD'],
            check=True,
            capture_output=True,
            env=git_env(),
        )
        .stdout.decode()
        .strip()
    )
    remote = tmp_path / 'host.git'
    subprocess.run(['git', 'init', '--bare', '-q', remote], check=True, env=git_env())
    subprocess.run(
        [
            'git',
            '-C',
            remote,
            'fetch',
            '-q',
            str(source),
            f'{commit}:refs/heads/main',
            f'{commit}:refs/heads/private-backup',
        ],
        check=True,
        env=git_env(),
    )
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url=remote.as_uri(),
        account=global_config.accounts['github'],
        visibility=project.visibility,
    )

    def _verified_destination(
        _paths: _config.Paths,
        _global: _config.GlobalConfig,
        _project: _config.ProjectConfig,
    ) -> _host.Destination:
        return destination

    monkeypatch.setattr(_host, 'verify_destination', _verified_destination)

    assert _cli.main(['repo', 'verify', '--project', project.name]) == 1
    assert 'unexpected branch' in capsys.readouterr().err


def test_repo_verify_reports_matching_declared_settings(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    settings = _config.Settings(
        description='Public repository',
        default_branch='main',
        issues=True,
        wiki=False,
        projects=True,
        pull_requests=False,
        discussions=False,
    )
    project = replace(project, settings=settings)
    _config.save_project(paths, project)
    _candidate.store_path(paths, project.name)
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account=global_config.accounts['github'],
        visibility=project.visibility,
    )
    stub_remote(monkeypatch, destination, _host.RemoteRefs(None, {}))

    def _repository_settings(_owner: str, _repo: str) -> _host.SettingsFacts:
        return _host.SettingsFacts(
            settings.description,
            settings.default_branch,
            settings.issues,
            settings.wiki,
            settings.projects,
            settings.pull_requests,
            settings.discussions,
        )

    monkeypatch.setattr(_host, 'repository_settings', _repository_settings)

    assert _cli.main(['repo', 'verify', '--project', project.name]) == 0
    output = capsys.readouterr().out
    assert 'Final repository' in output
    assert 'Settings: match' in output
    assert 'Expected refs:' not in output
    assert _cli.main(['repo', 'verify', '--project', project.name, '--verbose']) == 0
    assert 'Expected refs:' in capsys.readouterr().out
