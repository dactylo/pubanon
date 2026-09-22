import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, assert_type

import pytest

from pubanon import (
    _cli,
    _config,
)
from pubanon._remote import _host, _publish
from pubanon._snapshots import _candidate, _source
from tests._cli import prepare
from tests._setup import configure

if TYPE_CHECKING:
    from pubanon._cli._context import ProjectContext
    from pubanon._cli._remote import publish_context

    def _assert_publish_context_type(
        ctx: ProjectContext,
        record: _candidate.CandidateRecord,
    ) -> None:
        assert_type(
            publish_context(ctx, record, yes=True, unattended=True),
            _publish.PublishContext,
        )

    assert callable(_assert_publish_context_type)


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
    assert 'repository repo failed content gates' in rendered
    assert term in rendered
    assert not (tmp_path / 'fake-gh-calls.jsonl').exists()


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

    monkeypatch.setattr(_publish, 'push', _unexpected_push)
    capsys.readouterr()

    assert _cli.main(['publish', '--project', project.name]) == 1

    error = capsys.readouterr().err
    assert f'Cannot access GitHub repository {project.owner}/{project.repo}' in error
    assert 'does not exist or this account lacks access' in error
    assert 'run `pubanon repo create --project=demo`, then retry' in error
    assert _candidate.load_candidate(paths, project.name) == record


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

    monkeypatch.setattr(_publish, 'ls_remote', _remote_refs)
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
    record_file = _candidate.record_path(paths, project.name)
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


def test_publish_resolve_reports_published_outcome_after_real_local_finalization(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
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

    monkeypatch.setattr(_publish, 'verify_recorded_destination', _verified_destination)
    monkeypatch.setattr(_publish, 'ls_remote', _remote_refs)
    capsys.readouterr()

    assert _cli.main(['publish', '--project', project.name, '--resolve', '--yes']) == 0

    output = capsys.readouterr().out
    assert output == (
        'The snapshot was published and verified on the host repository.\n'
        'Local snapshot state was finalized.\n'
        'Pubanon left a private bookmark on this revision in your source repository.\n'
        'Source branches, tags, HEAD, index, and files were not changed.\n\n'
        f'Details: snapshot commit {record.commit}\n'
        'Next: pubanon repo reveal --project=demo\n'
    )
    assert output.count(record.commit) == 1
    assert output.index('Next:') > output.index('Details:')
    assert _candidate.load_candidate(paths, project.name) is None
    assert _candidate.published_refs(paths, project.name) == (record.commit, {})


def test_publish_resolve_reports_real_not_sent_state_update(
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

    monkeypatch.setattr(_publish, 'verify_recorded_destination', _verified_destination)
    monkeypatch.setattr(_publish, 'ls_remote', _remote_refs)
    capsys.readouterr()

    assert _cli.main(['publish', '--project', project.name, '--resolve', '--yes']) == 0

    output = capsys.readouterr().out
    assert output == (
        'The repository still points to the previous published snapshot.\n'
        'Your snapshot is available to publish.\n\n'
        f'Details: snapshot commit {record.commit}\n'
        'Next: pubanon publish --project=demo\n'
    )
    retained = _candidate.load_candidate(paths, project.name)
    assert retained is not None
    assert retained.state == _candidate.STATE_RETAINED
    assert retained.commit == record.commit


def test_publish_sync_skips_candidate_requirement_and_builds_sync_context(
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
        received.append(ctx)

    monkeypatch.setattr(_publish, 'sync', _sync)

    assert _cli.main(['publish', '--sync', '--yes']) == 0
    assert len(received) == 1
    sync_context = received[0]
    assert_type(sync_context, _publish.SyncContext)
    assert sync_context._paths == paths
    assert sync_context._global_config == global_config
    assert sync_context._project == project
    assert sync_context._yes is True
    assert sync_context._unattended is True


@pytest.mark.parametrize('command', [['publish'], ['release', '1.0.0']])
def test_unknown_outcome_names_resolve(
    command: list[str],
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    _candidate.save_candidate(
        paths,
        project.name,
        replace(record, state=_candidate.STATE_UNKNOWN_OUTCOME),
    )
    monkeypatch.chdir(source)

    assert _cli.main(command) == 1
    assert '--resolve' in capsys.readouterr().err
