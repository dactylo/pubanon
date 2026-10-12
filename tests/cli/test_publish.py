import json
import subprocess
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import assert_type

import pytest

from pubanon import (
    _cli,
    _config,
)
from pubanon._errors import PubanonError
from pubanon._remote import _host, _publish, _releases, _stage
from pubanon._snapshots import _candidate, _records, _source
from tests._cli import gh_calls, prepare, save_workflows
from tests._git import git_env
from tests._setup import configure
from tests.remote.test_publish import _configure_staged_candidate
from tests.remote.test_stage import _git


@pytest.fixture
def staged_publication_cli(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_publish.PublishContext, Path]:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    project = save_workflows(
        paths,
        replace(project, publication_route='staged', required_checks=('build',)),
        ship=('publish',),
    )
    assert (
        _cli.main(
            [
                'prepare',
                '--project',
                project.name,
                '--version',
                '1.0.0',
                '--acknowledge-new-paths',
                '--yes',
            ]
        )
        == 0
    )
    record = _records.load_candidate(paths, project.name)
    assert record is not None
    host = tmp_path / 'final.git'
    subprocess.run(['git', 'init', '--bare', '-q', host], check=True, env=git_env())
    monkeypatch.setattr(_host, '_URL_TEMPLATE', str(host))
    fake_gh.write_text(
        json.dumps(
            [
                {
                    'args': ['api', 'user', '--jq', '.login'],
                    'stdout': global_config.accounts['github'] + '\n',
                },
                {
                    'args': [
                        'api',
                        f'repos/{project.owner}/{project.repo}',
                        '--jq',
                        _host._REPOSITORY_SETTINGS_QUERY,
                    ],
                    'stdout': json.dumps(
                        {
                            'description': '',
                            'default_branch': 'main',
                            'issues': True,
                            'wiki': False,
                            'projects': False,
                            'pull_requests': False,
                            'discussions': False,
                        }
                    ),
                },
                {
                    'args': [
                        'api',
                        f'repos/{project.owner}/{project.repo}',
                        '--jq',
                        _host._REPOSITORY_QUERY,
                    ],
                    'stdout': json.dumps(
                        {
                            'owner': project.owner,
                            'repo': project.repo,
                            'visibility': project.visibility,
                        }
                    ),
                },
            ]
        )
    )
    ctx = _publish.PublishContext(paths, global_config, project, record, _yes=True)
    _configure_staged_candidate(ctx, fake_gh, monkeypatch)
    return ctx, host


def test_publish_decline_keeps_snapshot_and_does_not_push(
    staged_publication_cli: tuple[_publish.PublishContext, Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx, host = staged_publication_cli
    responses = json.loads(fake_gh.read_text())
    responses.append(
        {
            'args': [
                'api',
                f'repos/{ctx._project.owner}/{_config.stage_repository(ctx._project)}/commits/{ctx._record.commit}/check-runs?check_name=build&filter=latest&per_page=100',
            ],
            'stdout': json.dumps(
                {
                    'total_count': 1,
                    'check_runs': [
                        {
                            'name': 'build',
                            'head_sha': ctx._record.commit,
                            'status': 'completed',
                            'conclusion': 'success',
                        }
                    ],
                }
            ),
        }
    )
    fake_gh.write_text(json.dumps(responses))

    def _decline(_prompt: str) -> str:
        return 'n'

    monkeypatch.setattr('builtins.input', _decline)
    capsys.readouterr()

    assert _cli.main(['publish', '--project', ctx._project.name]) == 130

    error = capsys.readouterr().err
    assert 'Canceled. Nothing was published.' in error
    assert 'The prepared snapshot is kept.' in error
    assert f'Next: pubanon publish --project={ctx._project.name}' in error
    retained = _records.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert retained.commit == ctx._record.commit
    assert retained.state == _candidate.STATE_RETAINED
    assert _git(host, 'for-each-ref') == b''


def test_publish_gates_current_coordinates_before_host_access(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    term = 'private-coordinate'
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, _record = prepare(tmp_path, source, monkeypatch)
    _config.save_project(
        paths,
        replace(project, repo=f'public-{term}', extra_terms=(term,)),
    )

    def _verified_candidate(*_args: object, **_kwargs: object) -> _source.Snapshot:
        return ()

    def _verified_source(_ctx: _publish.PublishContext) -> None:
        return None

    monkeypatch.setattr(_publish, 'verify_retained_candidate', _verified_candidate)
    monkeypatch.setattr(_publish, '_verify_source', _verified_source)
    capsys.readouterr()

    assert _cli.main(['publish', '--project', project.name, '--yes']) == 1

    rendered = capsys.readouterr().err
    assert 'repository name did not pass content checks' in rendered
    assert term in rendered
    assert not (tmp_path / 'fake-gh-calls.jsonl').exists()


def test_publish_notes_are_read_once_before_lock_and_passed_exactly(
    tmp_path: Path, make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_repo()
    paths, _global, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    assert _cli.main(['prepare', '--project', project.name, '--version', '1.2.3', '--yes']) == 0
    notes_path = tmp_path / 'notes.md'
    notes_path.write_bytes(b'Exact\r\nnotes\n')
    monkeypatch.chdir(tmp_path)
    captured: list[_publish.PublishContext] = []
    original_lock = _config.project_lock

    @contextmanager
    def _lock(lock_paths: _config.Paths, name: str) -> Generator[None]:
        notes_path.write_bytes(b'changed after read')
        with original_lock(lock_paths, name):
            yield

    monkeypatch.setattr(_config, 'project_lock', _lock)
    monkeypatch.setattr(_publish, 'publish', captured.append)
    assert (
        _cli.main(['publish', '--project', project.name, '--release-notes-file', 'notes.md']) == 0
    )
    assert len(captured) == 1
    assert captured[0]._release_page.notes == b'Exact\r\nnotes\n'
    assert captured[0]._release_page.notes_file == notes_path
    assert not captured[0]._release_page.skip
    assert _records.load_candidate(paths, project.name) is not None


def test_publish_preserves_candidate_when_destination_is_inaccessible(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    fake_gh.write_text(
        json.dumps(
            [
                {
                    'args': ['api', 'user', '--jq', '.login'],
                    'stdout': record.destination.account + '\n',
                },
                {
                    'args': [
                        'api',
                        f'repos/{project.owner}/{project.repo}',
                        '--jq',
                        _host._REPOSITORY_QUERY,
                    ],
                    'returncode': 1,
                    'stdout': '{"message":"Not Found","status":"404"}',
                },
            ]
        )
    )

    def _unexpected_push(*_args: object, **_kwargs: object) -> None:
        pytest.fail('publication attempted after repository lookup failed')

    monkeypatch.setattr(_host, 'push', _unexpected_push)
    capsys.readouterr()

    assert _cli.main(['publish', '--project', project.name]) == 1

    error = capsys.readouterr().err
    assert f'Cannot access GitHub repository {project.owner}/{project.repo}' in error
    assert 'does not exist or this account lacks access' in error
    assert 'run `pubanon repo create --project=demo`, then retry' in error
    assert _records.load_candidate(paths, project.name) == record


def test_publish_release_page_option_requests_one_page(
    tmp_path: Path, make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_repo()
    _paths, _global, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    assert _cli.main(['prepare', '--project', project.name, '--version', '1.2.3', '--yes']) == 0
    captured: list[_publish.PublishContext] = []
    monkeypatch.setattr(_publish, 'publish', captured.append)
    assert _cli.main(['publish', '--project', project.name, '--release-page']) == 0
    assert len(captured) == 1
    assert captured[0]._release_page == _releases.PageRequest(requested=True)


def test_publish_resolve_preserves_recorded_authority_across_current_gate_drift(
    tmp_path: Path,
    fake_gh: Path,
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
    _config.save_project(paths, replace(project, extra_terms=(record.destination.repo,)))
    fake_gh.write_text(
        json.dumps(
            [
                {
                    'args': ['api', 'user', '--jq', '.login'],
                    'stdout': f'{record.destination.account}\n',
                },
                {
                    'args': [
                        'api',
                        f'repos/{record.destination.owner}/{record.destination.repo}',
                        '--jq',
                        _host._REPOSITORY_QUERY,
                    ],
                    'stdout': json.dumps(
                        {
                            'owner': record.destination.owner,
                            'repo': record.destination.repo,
                            'visibility': record.destination.visibility,
                        }
                    )
                    + '\n',
                },
            ]
        )
        + '\n'
    )
    remote_reads: list[_host.Destination] = []

    def _remote_refs(
        destination: _host.Destination,
        _paths: _config.Paths,
        _project: str,
    ) -> _host.RemoteRefs:
        remote_reads.append(destination)
        return _host.RemoteRefs(record.prestate_main, record.prestate_tags)

    monkeypatch.setattr(_host, 'ls_remote', _remote_refs)
    capsys.readouterr()

    assert _cli.main(['publish', '--project', project.name, '--resolve', '--yes']) == 0

    assert [(destination.owner, destination.repo) for destination in remote_reads] == [
        (record.destination.owner, record.destination.repo)
    ]
    calls = [
        json.loads(line)['args']
        for line in (tmp_path / 'fake-gh-calls.jsonl').read_text().splitlines()
    ]
    assert calls == [
        ['api', 'user', '--jq', '.login'],
        [
            'api',
            f'repos/{record.destination.owner}/{record.destination.repo}',
            '--jq',
            _host._REPOSITORY_QUERY,
        ],
    ]


def test_publish_resolve_refuses_malformed_recorded_coordinates_before_host_access(
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
    record_file = _records.candidate_path(paths, project.name)
    record_file.write_text(
        record_file.read_text(encoding='utf-8').replace(
            f'repo = "{record.destination.repo}"',
            'repo = "malformed?repo"',
        ),
        encoding='utf-8',
    )
    capsys.readouterr()

    assert _cli.main(['publish', '--project', project.name, '--resolve', '--yes']) == 1

    assert 'invalid snapshot record' in capsys.readouterr().err
    assert not (tmp_path / 'fake-gh-calls.jsonl').exists()


@pytest.mark.slow
@pytest.mark.parametrize('source_tags', [False, True])
def test_publish_resolve_reports_published_outcome_after_real_local_finalization(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source_tags: bool,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    _config.save_project(
        paths, replace(project, publication_route='staged', source_tags=source_tags)
    )
    bookmark = _source.describe_bookmark(source, project.name, record.commit)
    _source.ensure_bookmark(bookmark, record.pinned.commit)
    record = replace(record, source_bookmark=bookmark)
    _candidate.save_candidate(
        paths,
        project.name,
        replace(record, state=_candidate.STATE_UNKNOWN_OUTCOME),
    )
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account='anonymous-account',
        visibility=project.visibility,
    )

    def _verified_destination(
        _recorded: _host.Destination,
    ) -> _host.Destination:
        return destination

    def _remote_refs(
        _destination: _host.Destination,
        _paths: _config.Paths,
        _project: str,
    ) -> _host.RemoteRefs:
        return _host.RemoteRefs(record.commit, {})

    monkeypatch.setattr(_host, 'verify_recorded_destination', _verified_destination)
    monkeypatch.setattr(_host, 'ls_remote', _remote_refs)
    capsys.readouterr()

    assert _cli.main(['publish', '--project', project.name, '--resolve', '--yes']) == 0

    output = capsys.readouterr().out
    source_note = (
        'Source branches, HEAD, index, and files were not changed.'
        if source_tags
        else 'Source branches, tags, HEAD, index, and files were not changed.'
    )
    assert 'The snapshot was published and verified on the host repository.' in output
    assert 'Local snapshot state was finalized.' in output
    assert 'A private bookmark in your source repository keeps the original commit.' in output
    assert source_note not in output
    assert f'Details: snapshot commit {record.commit}' in output
    assert 'Next: pubanon repo reveal --project=demo' in output
    assert output.count(record.commit) == 1
    assert output.index('The snapshot was published') < output.index('Local snapshot state')
    assert output.index('Local snapshot state') < output.index('A private bookmark')
    assert output.index('Next:') > output.index('Details:')
    assert _records.load_candidate(paths, project.name) is None
    assert _candidate.published_refs(paths, project.name) == (record.commit, {})
    source_tag = _git(
        source, 'for-each-ref', '--format=%(objectname)', f'refs/tags/pubanon/{record.commit}'
    )
    assert source_tag.decode().strip() == (record.pinned.commit if source_tags else '')


@pytest.mark.parametrize(
    'case',
    [('direct', 'pubanon publish'), ('staged', 'pubanon stage push')],
)
def test_publish_resolve_reports_real_not_sent_state_update(
    case: tuple[str, str],
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    route, next_action = case
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    project = replace(project, publication_route=route)
    _config.save_project(paths, project)
    _candidate.save_candidate(
        paths,
        project.name,
        replace(record, state=_candidate.STATE_UNKNOWN_OUTCOME),
    )
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account='anonymous-account',
        visibility=project.visibility,
    )

    def _verified_destination(
        _recorded: _host.Destination,
    ) -> _host.Destination:
        return destination

    def _remote_refs(
        _destination: _host.Destination,
        _paths: _config.Paths,
        _project: str,
    ) -> _host.RemoteRefs:
        return _host.RemoteRefs(record.prestate_main, record.prestate_tags)

    monkeypatch.setattr(_host, 'verify_recorded_destination', _verified_destination)
    monkeypatch.setattr(_host, 'ls_remote', _remote_refs)
    capsys.readouterr()

    assert _cli.main(['publish', '--project', project.name, '--resolve', '--yes']) == 0

    output = capsys.readouterr().out
    assert 'The repository still points to the previous published snapshot.' in output
    assert 'Your snapshot is available to publish.' in output
    assert f'Details: snapshot commit {record.commit}' in output
    assert f'Next: {next_action} --project=demo' in output
    assert output.count(record.commit) == 1
    assert output.index('The repository still points') < output.index('Your snapshot is available')
    assert output.index('Your snapshot is available') < output.index('Details:')
    assert output.index('Details:') < output.index('Next:')
    retained = _records.load_candidate(paths, project.name)
    assert retained is not None
    assert retained.state == _candidate.STATE_RETAINED
    assert retained.commit == record.commit


def test_repo_restore_remote_skips_candidate_requirement_and_builds_sync_context(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    monkeypatch.chdir(source)
    received: list[_publish.SyncContext] = []

    def _sync(ctx: _publish.SyncContext) -> None:
        with (
            pytest.raises(
                PubanonError, match='another pubanon command is running for this project'
            ),
            _config.project_lock(paths, project.name),
        ):
            pytest.fail('repo restore did not hold its project lock')
        received.append(ctx)

    monkeypatch.setattr(_publish, 'sync', _sync)

    assert _cli.main(['repo', 'restore', '--to', 'remote', '--yes']) == 0
    assert len(received) == 1
    sync_context = received[0]
    assert_type(sync_context, _publish.SyncContext)
    assert sync_context._paths == paths
    assert sync_context._global_config == global_config
    assert sync_context._project == project
    assert sync_context._yes is True


@pytest.mark.slow
@pytest.mark.parametrize('case', [('publish', False), ('publish', True), ('run', True)])
def test_staged_publication_commands_recheck_ci_before_exact_final_push(
    case: tuple[str, bool],
    staged_publication_cli: tuple[_publish.PublishContext, Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    command, yes = case
    ctx, host = staged_publication_cli
    paths, project, record = (
        ctx._paths,
        ctx._project,
        ctx._record,
    )
    responses = json.loads(fake_gh.read_text())
    check_run = {
        'name': 'build',
        'head_sha': record.commit,
        'status': 'in_progress',
        'conclusion': None,
    }
    responses.append(
        {
            'args': [
                'api',
                f'repos/{project.owner}/{_config.stage_repository(project)}/commits/{record.commit}/check-runs?check_name=build&filter=latest&per_page=100',
            ],
            'stdout': json.dumps({'total_count': 1, 'check_runs': [check_run]}),
        }
    )
    fake_gh.write_text(json.dumps(responses))
    argv = (
        {
            'publish': ['publish'],
            'run': ['run', 'ship', '--version', '1.0.0'],
        }[command]
        + ['--project', project.name]
        + (['--yes'] if yes else [])
    )
    real_push = _host.push
    pushed: list[str] = []
    confirmed: list[str] = []

    def _confirm(question: str) -> bool:
        confirmed.append(question)
        return True

    def _push(destination: _host.Destination, cwd: Path, args: list[str]) -> _host.GitResult:
        pushed.append(destination.repo)
        return real_push(destination, cwd, args)

    monkeypatch.setattr(_host, 'push', _push)
    monkeypatch.setattr(_publish, 'confirm', _confirm)
    capsys.readouterr()
    assert _cli.main(argv) == 1
    assert f'pubanon stage watch --project={project.name}' in capsys.readouterr().err
    assert confirmed == []
    assert pushed == []
    assert _records.load_candidate(paths, project.name) == record
    assert _git(host, 'for-each-ref') == b''

    # A success response for another SHA is not evidence for this candidate.
    check_run.update(status='completed', conclusion='success', head_sha='a' * 40)
    responses[-1]['stdout'] = json.dumps({'total_count': 1, 'check_runs': [check_run]})
    fake_gh.write_text(json.dumps(responses))
    assert _cli.main(argv) == 1
    assert pushed == []
    assert _records.load_candidate(paths, project.name) == record

    check_run['head_sha'] = record.commit
    responses[-1]['stdout'] = json.dumps({'total_count': 1, 'check_runs': [check_run]})
    fake_gh.write_text(json.dumps(responses))
    capsys.readouterr()
    assert _cli.main(argv) == 0
    output = capsys.readouterr().out
    assert 'Published v1.0.0' in output
    assert pushed == [project.repo]
    assert bool(confirmed) is not yes
    assert _git(host, 'show-ref').decode().splitlines() == [
        f'{record.commit} refs/heads/main',
        f'{record.commit} refs/tags/v1.0.0',
    ]
    assert len([args for args in gh_calls(fake_gh.parent) if '/check-runs?' in args[1]]) == 3

    # Synchronization restores published history without staging prerequisites.
    _stage.stage_forget(paths, project, yes=True)
    _git(host, 'update-ref', '-d', 'refs/heads/main')
    _git(host, 'update-ref', '-d', 'refs/tags/v1.0.0')
    assert (
        _cli.main(['repo', 'restore', '--to', 'remote', '--project', project.name, '--yes']) == 0
    )
    assert _git(host, 'rev-parse', 'main').decode().strip() == record.commit
    if command == 'run':
        assert _cli.main(argv) == 0
        assert 'already published' in capsys.readouterr().out
    assert len([args for args in gh_calls(fake_gh.parent) if '/check-runs?' in args[1]]) == 3


@pytest.mark.parametrize('page_state', ['present', 'absent', 'failed', 'disabled'])
def test_publish_resolve_reports_page_without_writing(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    page_state: str,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.2.3')
    _config.save_project(
        paths,
        replace(
            project, release_page=_config.ReleasePageSettings(enabled=page_state != 'disabled')
        ),
    )
    bookmark = _source.describe_bookmark(source, project.name, record.commit)
    _source.ensure_bookmark(bookmark, record.pinned.commit)
    record = replace(record, source_bookmark=bookmark)
    _candidate.save_candidate(
        paths, project.name, replace(record, state=_candidate.STATE_UNKNOWN_OUTCOME)
    )
    destination = _publish._destination_from_record(record)
    page = _host.GitHubRelease(
        'v1.2.3', 'custom', '', 'https://example.invalid/page', draft=True, prerelease=False
    )
    reads: list[str] = []

    def _destination(*_args: object) -> _host.Destination:
        return destination

    def _refs(*_args: object, **_kwargs: object) -> _host.RemoteRefs:
        return _host.RemoteRefs(record.commit, {'v1.2.3': record.commit})

    def _read(_destination: _host.Destination, tag: str) -> _host.GitHubRelease | None:
        assert _records.load_candidate(paths, project.name) is None
        reads.append(tag)
        if page_state == 'failed':
            raise PubanonError('lookup failed')
        return page if page_state == 'present' else None

    def _write(*_args: object, **_kwargs: object) -> None:
        pytest.fail('resolve must not write a page')

    monkeypatch.setattr(_host, 'verify_recorded_destination', _destination)
    monkeypatch.setattr(_host, 'verify_destination', _destination)
    monkeypatch.setattr(_host, 'ls_remote', _refs)
    monkeypatch.setattr(_host, 'read_release', _read)
    monkeypatch.setattr(_host, 'create_release', _write)
    capsys.readouterr()
    assert _cli.main(['publish', '--project', project.name, '--resolve']) == 0
    captured = capsys.readouterr()
    assert 'The snapshot was published and verified' in captured.out
    assert _records.load_candidate(paths, project.name) is None
    assert reads == ([] if page_state == 'disabled' else ['v1.2.3'])
    if page_state == 'present':
        assert f'Release page: {page.url}' in captured.out
    elif page_state == 'absent':
        assert 'no page exists' in captured.out
        assert 'pubanon release create v1.2.3 --project=demo' in captured.out
    elif page_state == 'failed':
        assert 'warning:' in captured.out
        assert 'could not look up the release page' in captured.out
        assert 'no page exists' not in captured.out


def test_unknown_outcome_names_resolve(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    _config.save_project(paths, replace(project, publication_route='staged'))
    _candidate.save_candidate(
        paths,
        project.name,
        replace(record, state=_candidate.STATE_UNKNOWN_OUTCOME),
    )
    monkeypatch.chdir(source)

    assert _cli.main(['publish']) == 1
    assert '--resolve' in capsys.readouterr().err
