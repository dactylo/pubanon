import json
import os
import subprocess
from collections.abc import Callable, Sequence
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from typing import Any, Never, cast

import pytest

from pubanon._config import GlobalConfig, Paths, ProjectConfig
from pubanon._errors import PubanonError
from pubanon._remote import _host, _stage
from pubanon._remote._stage import _clear_stage, _save_stage
from pubanon._run import GitResult
from pubanon._snapshots import _candidate
from pubanon._snapshots._source import Snapshot, pin, read_snapshot
from tests._git import git_env
from tests._permissions import assert_private_mode
from tests.snapshots.test_candidate import _complete_replacement


def _build_candidate(
    paths: Paths,
    global_config: GlobalConfig,
    project: ProjectConfig,
) -> _candidate.CandidateRecord:
    pinned = pin(project.source_path, 'HEAD')
    return _candidate.build_candidate(
        paths,
        global_config,
        project,
        _candidate.CandidateInputs(
            read_snapshot(project.source_path, pinned.commit),
            pinned,
            None,
            None,
        ),
    )


def _calls(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / 'fake-gh-calls.jsonl'
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _clean_responses(
    fake_gh: Path,
    coordinates: tuple[str, str],
    *,
    outcome: str = 'deleted',
    actual: tuple[str, str, str] | None = None,
) -> None:
    owner, repo = coordinates
    facts = actual or (owner, repo, 'private')
    repository_args = [
        'api',
        f'repos/{owner}/{repo}',
        '--jq',
        _host._REPOSITORY_QUERY,
    ]
    responses: list[dict[str, object]] = [
        {
            'args': ['api', 'user', '--jq', '.login'],
            'stdout': 'stage-owner\n',
        },
        (
            {
                'args': repository_args,
                'stdout': '{"status":"404"}\n',
                'returncode': 1,
                'once': True,
            }
            if outcome == 'absent'
            else {
                'args': repository_args,
                'stdout': json.dumps({'owner': facts[0], 'repo': facts[1], 'visibility': facts[2]})
                + '\n',
                'once': True,
            }
        ),
    ]
    if outcome != 'absent':
        responses.append(
            {
                'args': repository_args,
                'stdout': json.dumps({'owner': facts[0], 'repo': facts[1], 'visibility': facts[2]})
                + '\n',
                'once': True,
            }
        )
        delete_response: dict[str, object] = {
            'args': ['api', '-X', 'DELETE', f'repos/{owner}/{repo}'],
        }
        if outcome == 'scope-failure':
            delete_response |= {
                'stdout': json.dumps({'status': '403'}) + '\n',
                'returncode': 1,
            }
        responses.append(delete_response)
        responses.append(
            {
                'args': repository_args,
                'stdout': '{"status":"404"}\n',
                'returncode': 1,
            }
            if outcome != 'incomplete'
            else {
                'args': repository_args,
                'stdout': json.dumps({'owner': facts[0], 'repo': facts[1], 'visibility': facts[2]})
                + '\n',
                'once': True,
            }
        )
    fake_gh.write_text(json.dumps(responses) + '\n')


def _configs(source: Path) -> tuple[GlobalConfig, ProjectConfig]:
    return (
        GlobalConfig(
            anon_name='Anonymous Publisher',
            anon_email='anonymous@example.invalid',
            accounts={'github': 'stage-owner'},
            sensitive_terms=('Private Person',),
        ),
        ProjectConfig(
            name='example',
            source_path=source.resolve(),
            host='github',
            owner='stage-owner',
            repo='public-repo',
            visibility='public',
            allow=('**',),
            deny=(),
            secret_exceptions=(),
            extra_terms=(),
            hygiene_terms=(),
            self_publication=True,
            required_checks=(),
            _stage_repo='staging-repo',
        ),
    )


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ['git', '-C', repo, *args],
        check=True,
        capture_output=True,
        env=git_env(),
    ).stdout


def _host_responses(
    fake_gh: Path,
    project: ProjectConfig,
    *,
    absent: bool = False,
    actual: tuple[str, str, str] | None = None,
) -> None:
    stage_repo = project._stage_repo
    assert stage_repo is not None
    facts = actual or (project.owner, stage_repo, 'private')
    repository_args = [
        'api',
        f'repos/{project.owner}/{stage_repo}',
        '--jq',
        _host._REPOSITORY_QUERY,
    ]
    responses: list[dict[str, object]] = [
        {
            'args': ['api', 'user', '--jq', '.login'],
            'stdout': 'stage-owner\n',
        },
        (
            {
                'args': repository_args,
                'stdout': '{"status":"404"}\n',
                'returncode': 1,
                'once': True,
            }
            if absent
            else {
                'args': repository_args,
                'stdout': json.dumps({'owner': facts[0], 'repo': facts[1], 'visibility': facts[2]})
                + '\n',
            }
        ),
    ]
    if absent:
        responses.append(
            {
                'args': [
                    'api',
                    '-X',
                    'POST',
                    'user/repos',
                    '-f',
                    f'name={stage_repo}',
                    '-F',
                    'private=true',
                ]
            }
        )
    responses.append(
        {
            'args': repository_args,
            'stdout': json.dumps({'owner': facts[0], 'repo': facts[1], 'visibility': facts[2]})
            + '\n',
        }
    )
    fake_gh.write_text(json.dumps(responses) + '\n')


def _new_host(tmp_path: Path) -> Path:
    host = tmp_path / 'stage.git'
    subprocess.run(['git', 'init', '--bare', '-q', host], check=True, env=git_env())
    return host


def _paths(tmp_path: Path) -> Paths:
    config_dir = tmp_path / 'config' / 'pubanon'
    data_dir = tmp_path / 'data' / 'pubanon'
    return Paths(config_dir, data_dir, config_dir / 'config.toml', data_dir / 'registry.json')


def _prepare_stage(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, Paths, GlobalConfig, ProjectConfig, _candidate.CandidateRecord]:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    record = _build_candidate(paths, global_config, project)
    _host_responses(fake_gh, project)
    monkeypatch.setattr(_stage, 'URL_TEMPLATE', host.as_uri())
    _stage.stage_push(paths, global_config, project, yes=True)
    return host, paths, global_config, project, record


def _status_responses(
    fake_gh: Path,
    project: ProjectConfig,
    commit: str,
    states: dict[str, str],
) -> None:
    stage_repo = project._stage_repo
    assert stage_repo is not None
    responses: list[dict[str, object]] = [
        {
            'args': ['api', 'user', '--jq', '.login'],
            'stdout': 'stage-owner\n',
        },
        {
            'args': [
                'api',
                f'repos/{project.owner}/{stage_repo}',
                '--jq',
                _host._REPOSITORY_QUERY,
            ],
            'stdout': json.dumps(
                {
                    'owner': project.owner,
                    'repo': stage_repo,
                    'visibility': 'private',
                }
            )
            + '\n',
        },
    ]
    for name, state in states.items():
        if state == 'missing':
            payload: dict[str, object] = {'total_count': 0, 'check_runs': []}
        else:
            run = {
                'name': name,
                'head_sha': commit,
                'status': 'in_progress' if state == 'pending' else 'completed',
                'conclusion': None
                if state == 'pending'
                else 'success'
                if state == 'ambiguous'
                else state,
            }
            payload = {
                'total_count': 101 if state == 'ambiguous' else 1,
                'check_runs': [run] * 100 if state == 'ambiguous' else [run],
            }
        query = name.replace(' ', '+')
        responses.append(
            {
                'args': [
                    'api',
                    (
                        f'repos/{project.owner}/{stage_repo}/commits/{commit}/check-runs?'
                        f'check_name={query}&filter=latest&per_page=100'
                    ),
                ],
                'stdout': json.dumps(payload) + '\n',
            }
        )
    fake_gh.write_text(json.dumps(responses) + '\n')


def test_added_sensitive_term_matching_candidate_refuses_stale_candidate_locally(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_gh
    added_term = 'new private content'
    source = make_repo(files={'README.md': f'{added_term}\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    record = _build_candidate(paths, global_config, project)
    changed_config = replace(
        global_config,
        sensitive_terms=(*global_config.sensitive_terms, added_term),
    )
    transport_calls: list[str] = []

    def _record_refs(*_args: object) -> tuple[str | None, tuple[str, ...]]:
        transport_calls.append('stage_refs')
        return None, ()

    def _record_push(*_args: object) -> GitResult:
        transport_calls.append('push')
        return GitResult(0, b'', b'')

    monkeypatch.setattr(_stage, 'stage_refs', _record_refs)
    monkeypatch.setattr(_stage, 'push', _record_push)

    with pytest.raises(PubanonError, match='configuration changed') as exc_info:
        _stage.stage_push(paths, changed_config, project, yes=True)

    assert exc_info.value.recovery == 'pubanon discard, then pubanon prepare'
    assert _calls(tmp_path) == []
    assert transport_calls == []
    assert _candidate.load_candidate(paths, project.name) == record


@pytest.mark.parametrize(
    ('candidate_state', 'error'),
    [
        ('absent', 'no prepared snapshot is retained'),
        ('unknown', 'snapshot has an unknown outcome'),
    ],
)
def test_candidate_state_refuses_before_host_access_with_named_recovery(
    candidate_state: str,
    error: str,
    tmp_path: Path,
    make_repo: Callable[..., Path],
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    if candidate_state == 'unknown':
        record = _build_candidate(paths, global_config, project)
        _candidate.save_candidate(
            paths,
            project.name,
            replace(record, state=_candidate.STATE_UNKNOWN_OUTCOME),
        )

    with pytest.raises(PubanonError, match=error) as exc_info:
        _stage.stage_push(paths, global_config, project, yes=True)

    expected = 'pubanon prepare' if candidate_state == 'absent' else 'pubanon publish --resolve'
    assert exc_info.value.recovery == expected
    assert _calls(tmp_path) == []


def test_case_only_publication_alias_refuses_before_host_access(
    tmp_path: Path,
    make_repo: Callable[..., Path],
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    project = replace(project, _stage_repo='PUBLIC-REPO')
    _build_candidate(paths, global_config, project)

    with pytest.raises(
        PubanonError,
        match='staging repository must differ from the publication repository',
    ):
        _stage.stage_push(paths, global_config, project, yes=True)

    assert _calls(tmp_path) == []


def test_creation_failure_after_not_visible_retains_candidate_and_stage_evidence(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    candidate = _build_candidate(paths, global_config, project)
    stage = _stage.StageRecord('a' * 40, project.owner, 'staging-repo')
    _save_stage(paths, project.name, stage)
    assert project._stage_repo is not None
    repository_args = [
        'api',
        f'repos/{project.owner}/{project._stage_repo}',
        '--jq',
        _host._REPOSITORY_QUERY,
    ]
    create_args = [
        'api',
        '-X',
        'POST',
        'user/repos',
        '-f',
        f'name={project._stage_repo}',
        '-F',
        'private=true',
    ]
    fake_gh.write_text(
        json.dumps(
            [
                {
                    'args': ['api', 'user', '--jq', '.login'],
                    'stdout': 'stage-owner\n',
                },
                {
                    'args': repository_args,
                    'returncode': 1,
                    'stdout': '{"status":"404"}\n',
                },
                {
                    'args': create_args,
                    'returncode': 1,
                    'stdout': '{"message":"repository already exists","status":"422"}\n',
                },
            ]
        )
        + '\n'
    )

    def _approve(_prompt: str) -> str:
        return 'yes'

    monkeypatch.setattr('builtins.input', _approve)

    with pytest.raises(PubanonError, match='repository already exists'):
        _stage.stage_push(paths, global_config, project, yes=True)

    assert _candidate.load_candidate(paths, project.name) == candidate
    assert _stage.load_stage(paths, project.name) == stage
    assert create_args in [call['args'] for call in _calls(tmp_path)]


def test_failed_readback_does_not_write_stage_record(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    _build_candidate(paths, global_config, project)
    _host_responses(fake_gh, project)
    reads = iter([(None, ()), ('b' * 40, ())])

    def _staged_refs(*_args: object) -> tuple[str | None, tuple[str, ...]]:
        return next(reads)

    def _successful_push(*_args: object) -> GitResult:
        return GitResult(0, b'', b'')

    monkeypatch.setattr(_stage, 'stage_refs', _staged_refs)
    monkeypatch.setattr(_stage, 'push', _successful_push)

    with pytest.raises(PubanonError, match='read-back') as exc_info:
        _stage.stage_push(paths, global_config, project, yes=True)

    assert exc_info.value.recovery == 'pubanon stage clean'
    assert _stage.load_stage(paths, project.name) is None


def test_first_push_uses_only_candidate_stage_ref_and_records_exact_coordinates(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, paths, _global_config, project, record = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    assert project._stage_repo is not None
    calls = _calls(tmp_path)

    assert calls[0]['args'] == ['api', 'user', '--jq', '.login']
    assert _git(host, 'rev-parse', 'refs/heads/stage') == f'{record.commit}\n'.encode()
    assert _git(host, 'for-each-ref', '--format=%(refname)') == b'refs/heads/stage\n'
    assert _stage.load_stage(paths, project.name) == _stage.StageRecord(
        record.commit,
        project.owner,
        project._stage_repo,
    )
    stage_path = paths.data_dir / 'projects' / project.name / 'stage.toml'
    assert_private_mode(stage_path, 0o600)


def test_first_use_creation_decline_leaves_host_refs_empty(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    _build_candidate(paths, global_config, project)
    _host_responses(fake_gh, project, absent=True)
    monkeypatch.setattr(_stage, 'URL_TEMPLATE', host.as_uri())

    def _decline(_prompt: str) -> str:
        assert _git(host, 'for-each-ref') == b''
        return 'no'

    monkeypatch.setattr('builtins.input', _decline)

    with pytest.raises(PubanonError, match='creation declined'):
        _stage.stage_push(paths, global_config, project, yes=False)
    assert not any(call['args'][2:4] == ['POST', 'user/repos'] for call in _calls(tmp_path))
    assert _git(host, 'for-each-ref') == b''


def test_first_use_creation_requires_interactive_yes_then_reads_facts_and_pushes(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    record = _build_candidate(paths, global_config, project)
    _host_responses(fake_gh, project, absent=True)
    monkeypatch.setattr(_stage, 'URL_TEMPLATE', host.as_uri())
    prompts: list[str] = []

    def _approve(prompt: str) -> str:
        prompts.append(prompt)
        return 'yes'

    monkeypatch.setattr('builtins.input', _approve)

    _stage.stage_push(paths, global_config, project, yes=True)

    calls = _calls(tmp_path)
    assert prompts == ['Create the private staging repository stage-owner/staging-repo? [y/n] ']
    assert calls[0]['args'] == ['api', 'user', '--jq', '.login']
    assert any(call['args'][2:4] == ['POST', 'user/repos'] for call in calls)
    assert _git(host, 'rev-parse', 'refs/heads/stage') == f'{record.commit}\n'.encode()
    output = capsys.readouterr().out
    lines = output.splitlines()
    assert lines[0]
    assert lines[lines.index('Stage prepared snapshot') - 1] == ''
    assert 'Prepared snapshot staged and verified in the private repository.\n' in output
    assert 'The source repository and publication repository were not changed.\n' in output
    completion = output.index('Prepared snapshot staged and verified in the private repository.\n')
    preview = output[:completion]
    assert 'Stage prepared snapshot' in preview
    assert 'Destination: GitHub stage-owner/staging-repo' in preview
    assert 'Staging uses a private repository and branch stage.' in preview
    assert 'Current staged commit: none' in preview
    assert f'{record.commit}:refs/heads/stage' in preview
    assert '  force-with-lease refs/heads/stage:absent\n' in preview
    assert output.index('Next:') > output.index('Staged commit:')
    assert f'Staged commit: {record.commit}\n' in output
    assert 'Next: pubanon publish\n' in output
    assert all(
        record.commit not in line
        for line in output[completion : output.index('Staged commit:')].splitlines()
    )


@pytest.mark.parametrize(
    ('field', 'error'),
    [
        ('owner', 'repository owner failed content gates'),
        ('name', 'repository repo failed content gates'),
    ],
)
def test_host_visible_stage_coordinate_is_gated_before_host_access(
    field: str,
    error: str,
    tmp_path: Path,
    make_repo: Callable[..., Path],
) -> None:
    term = f'private-{field}'
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    project = replace(
        project,
        owner=term if field == 'owner' else project.owner,
        _stage_repo=term if field == 'name' else project._stage_repo,
        extra_terms=(term,),
    )
    _build_candidate(paths, global_config, project)

    with pytest.raises(PubanonError, match=error) as exc_info:
        _stage.stage_push(paths, global_config, project, yes=True)

    rendered = exc_info.value.render()
    assert term in rendered
    assert _calls(tmp_path) == []


@pytest.mark.parametrize(
    ('case', 'error'),
    [
        ('hygiene', 'repository repo failed content gates'),
        ('same-repository', 'staging repository must differ from the publication repository'),
    ],
)
def test_invalid_stage_destination_refuses_before_host_access(
    case: str,
    error: str,
    tmp_path: Path,
    make_repo: Callable[..., Path],
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    if case == 'hygiene':
        project = replace(project, _stage_repo='private-stage', extra_terms=('private',))
    else:
        project = replace(project, _stage_repo=project.repo)
    _build_candidate(paths, global_config, project)

    with pytest.raises(PubanonError, match=error):
        _stage.stage_push(paths, global_config, project, yes=True)

    assert _calls(tmp_path) == []


def test_load_stage_refuses_unknown_schema_with_clean_recovery(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    stage_path = paths.data_dir / 'projects' / 'example' / 'stage.toml'
    stage_path.parent.mkdir(parents=True)
    stage_path.write_text('commit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\nextra = "x"\n')

    with pytest.raises(PubanonError, match='invalid stage record') as exc_info:
        _stage.load_stage(paths, 'example')

    assert exc_info.value.recovery == 'run interactively: pubanon stage clean'


@pytest.mark.parametrize(
    'actual',
    [
        ('stage-owner', 'staging-repo', 'public'),
        ('stage-owner', 'redirected-stage', 'private'),
    ],
)
def test_nonprivate_or_redirected_stage_repository_refuses_before_push(
    actual: tuple[str, str, str],
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    _build_candidate(paths, global_config, project)
    _host_responses(fake_gh, project, actual=actual)
    monkeypatch.setattr(_stage, 'URL_TEMPLATE', host.as_uri())

    def _unexpected_push(*_args: object, **_kwargs: object) -> GitResult:
        pytest.fail('unverified staging facts reached push')

    monkeypatch.setattr(_stage, 'push', _unexpected_push)

    with pytest.raises(PubanonError, match='staging repository facts') as exc_info:
        _stage.stage_push(paths, global_config, project, yes=True)

    assert exc_info.value.recovery == 'pubanon stage clean'


def test_post_push_ref_read_failure_names_clean_and_preserves_records(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = r'private-read-canary\x1b\\path'
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    record = _build_candidate(paths, global_config, project)
    old_stage = _stage.StageRecord('a' * 40, project.owner, 'staging-repo')
    _save_stage(paths, project.name, old_stage)
    _host_responses(fake_gh, project)
    reads = 0

    def _staged_refs(*_args: object) -> tuple[str | None, tuple[str, ...]]:
        nonlocal reads
        reads += 1
        if reads == 1:
            return old_stage.commit, ()
        raise PubanonError(canary)

    def _successful_push(*_args: object) -> GitResult:
        return GitResult(0, b'', b'')

    monkeypatch.setattr(_stage, 'stage_refs', _staged_refs)
    monkeypatch.setattr(_stage, 'push', _successful_push)

    with pytest.raises(PubanonError, match='staging push read-back') as exc_info:
        _stage.stage_push(paths, global_config, project, yes=True)

    assert exc_info.value.recovery == 'pubanon stage clean'
    assert canary in exc_info.value.render()
    assert exc_info.value.fact == f'error: {canary}'
    assert _candidate.load_candidate(paths, project.name) == record
    assert _stage.load_stage(paths, project.name) == old_stage


def test_private_clear_stage_removes_record_and_accepts_repeated_cleanup(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    record = _stage.StageRecord('a' * 40, 'stage-owner', 'staging-repo')
    _save_stage(paths, 'example', record)

    _clear_stage(paths, 'example')
    _clear_stage(paths, 'example')

    assert _stage.load_stage(paths, 'example') is None


def test_publication_coordinate_drift_refuses_stale_candidate_locally(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_gh
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    record = _build_candidate(paths, global_config, project)
    changed_project = replace(project, repo='changed-public-repo')
    transport_calls: list[str] = []

    def _record_refs(*_args: object) -> tuple[str | None, tuple[str, ...]]:
        transport_calls.append('stage_refs')
        return None, ()

    def _record_push(*_args: object) -> GitResult:
        transport_calls.append('push')
        return GitResult(0, b'', b'')

    monkeypatch.setattr(_stage, 'stage_refs', _record_refs)
    monkeypatch.setattr(_stage, 'push', _record_push)

    with pytest.raises(PubanonError, match='configuration changed') as exc_info:
        _stage.stage_push(paths, global_config, changed_project, yes=True)

    assert exc_info.value.recovery == 'pubanon discard, then pubanon prepare'
    assert _calls(tmp_path) == []
    assert transport_calls == []
    assert _candidate.load_candidate(paths, project.name) == record


@pytest.mark.parametrize('drift', ['repo', 'owner'])
def test_recorded_coordinate_drift_refuses_before_host_access(
    drift: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_path, paths, global_config, project, _record = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    calls_path = tmp_path / 'fake-gh-calls.jsonl'
    calls_path.unlink()
    changed = (
        replace(project, _stage_repo='renamed-stage')
        if drift == 'repo'
        else replace(project, owner='changed-owner')
    )
    expected_message = 'coordinates' if drift == 'repo' else 'configuration changed'
    expected_recovery = (
        'pubanon stage clean removes the recorded staging repository first'
        if drift == 'repo'
        else 'pubanon discard, then pubanon prepare'
    )

    with pytest.raises(PubanonError, match=expected_message) as exc_info:
        _stage.stage_push(paths, global_config, changed, yes=True)

    assert exc_info.value.recovery == expected_recovery
    assert _calls(tmp_path) == []


def test_replacement_push_uses_recorded_lease_and_candidate_only_refspec(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_path, paths, global_config, project, first = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    _candidate.discard(paths, project.name, force=True)
    (project.source_path / 'README.md').write_text('second\n')
    _git(project.source_path, 'add', 'README.md')
    _git(
        project.source_path,
        '-c',
        'user.name=Test User',
        '-c',
        'user.email=test@example.invalid',
        'commit',
        '-qm',
        'second',
    )
    second = _build_candidate(paths, global_config, project)
    pushed: list[list[str]] = []
    real_push = _stage.push

    def _record_push(
        dest: _host.Destination,
        cwd: Path,
        args: Sequence[str],
    ) -> GitResult:
        pushed.append(list(args))
        return real_push(dest, cwd, args)

    monkeypatch.setattr(_stage, 'push', _record_push)

    _stage.stage_push(paths, global_config, project, yes=True)

    assert pushed == [
        [
            '--atomic',
            f'--force-with-lease=refs/heads/stage:{first.commit}',
            '--no-follow-tags',
            f'{second.commit}:refs/heads/stage',
        ]
    ]
    assert 'refs/tags' not in ' '.join(pushed[0])
    assert 'refs/heads/main' not in ' '.join(pushed[0])


def test_replacement_stages_resulting_tip_with_distinct_tree(
    tmp_path: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project, candidate = _complete_replacement(tmp_path, stage=True)
    assert candidate.tip is not None
    assert candidate.tip != candidate.commit
    store = _candidate.store_path(paths, project.name)
    correction_tree = _git(store, 'rev-parse', f'{candidate.commit}^{{tree}}').strip()
    tip_tree = _git(store, 'rev-parse', f'{candidate.tip}^{{tree}}').strip()
    assert correction_tree != tip_tree
    host = _new_host(tmp_path)
    _host_responses(fake_gh, project)
    monkeypatch.setattr(_stage, 'URL_TEMPLATE', host.as_uri())

    _stage.stage_push(paths, global_config, project, yes=True)

    saved = _stage.load_stage(paths, project.name)
    assert saved is not None
    assert saved.commit == candidate.tip
    assert _git(host, 'rev-parse', 'refs/heads/stage').strip().decode() == candidate.tip


def test_stage_clean_authorization_failure_keeps_record_and_names_possible_cause(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_path, paths, global_config, project, _record = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    assert project._stage_repo is not None
    stage = _stage.load_stage(paths, project.name)
    assert stage is not None
    (tmp_path / 'fake-gh-calls.jsonl').unlink()
    _clean_responses(
        fake_gh,
        (project.owner, project._stage_repo),
        outcome='scope-failure',
    )

    with pytest.raises(PubanonError, match='not authorized') as exc_info:
        _stage.stage_clean(paths, global_config, project, yes=True)

    assert _stage.load_stage(paths, project.name) == stage
    assert exc_info.value.fact == 'HTTP 403: GitHub refused authorization'
    assert exc_info.value.recovery is not None
    assert 'missing delete_repo scope is one possible cause' in exc_info.value.recovery
    assert 'review the active account and repository permissions' in exc_info.value.recovery
    assert 'gh auth refresh' not in exc_info.value.render()
    assert not any(call['args'][:2] == ['auth', 'refresh'] for call in _calls(tmp_path))


def test_stage_clean_declined_confirmation_keeps_repository_and_record(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_path, paths, global_config, project, _record = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    assert project._stage_repo is not None
    stage = _stage.load_stage(paths, project.name)
    (tmp_path / 'fake-gh-calls.jsonl').unlink()
    _clean_responses(fake_gh, (project.owner, project._stage_repo))

    def _decline(prompt: str) -> str:
        assert prompt == 'Delete the staging repository stage-owner/staging-repo? [y/n] '
        return 'no'

    monkeypatch.setattr('builtins.input', _decline)

    with pytest.raises(PubanonError, match='deletion declined'):
        _stage.stage_clean(paths, global_config, project, yes=False)

    assert _stage.load_stage(paths, project.name) == stage
    assert not any('DELETE' in call['args'] for call in _calls(tmp_path))


def test_stage_clean_deletes_private_repository_verifies_absence_and_clears_record(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _host_path, paths, global_config, project, _record = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    assert project._stage_repo is not None
    (tmp_path / 'fake-gh-calls.jsonl').unlink()
    _clean_responses(fake_gh, (project.owner, project._stage_repo))
    capsys.readouterr()

    _stage.stage_clean(paths, global_config, project, yes=True)

    assert _stage.load_stage(paths, project.name) is None
    calls = [call['args'] for call in _calls(tmp_path)]
    assert (
        calls.count(
            [
                'api',
                f'repos/{project.owner}/{project._stage_repo}',
                '--jq',
                _host._REPOSITORY_QUERY,
            ]
        )
        == 3
    )
    assert ['api', '-X', 'DELETE', f'repos/{project.owner}/{project._stage_repo}'] in calls
    output = capsys.readouterr().out
    assert 'hygiene-class' not in output
    assert 'Deletion read-back confirmed the selected staging repository is absent.' in output
    assert 'Cleared the local staging record.' in output
    assert 'The source repository and publication repository were not changed.\n' in output
    assert 'harmless' not in output
    assert 'logs' not in output
    assert 'artifacts' not in output
    assert 'Next:' not in output


@pytest.mark.parametrize(
    'content',
    [
        pytest.param('malformed', id='toml'),
        pytest.param(
            'commit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n'
            'owner = "stage-owner"\n'
            'repo = "staging-repo"\n'
            'extra = "unknown"\n',
            id='schema',
        ),
        pytest.param(
            'commit = 1\nowner = "stage-owner"\nrepo = "staging-repo"\n',
            id='string',
        ),
        pytest.param(
            'commit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n'
            'owner = "two/segments"\n'
            'repo = "staging-repo"\n',
            id='coordinate',
        ),
        pytest.param(
            'commit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n'
            'owner = "stage-owner"\n'
            'repo = "staging-repo"\n',
            id='oid',
        ),
    ],
)
def test_stage_clean_explicitly_abandons_invalid_record_without_host_access(
    content: str,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    stage_path = paths.data_dir / 'projects' / project.name / 'stage.toml'
    stage_path.parent.mkdir(parents=True)
    stage_path.write_text(content, encoding='utf-8')

    def _approve(_prompt: str) -> str:
        return 'yes'

    monkeypatch.setattr('builtins.input', _approve)

    _stage.stage_clean(paths, global_config, project, yes=False)

    assert not stage_path.exists()
    assert _calls(tmp_path) == []
    assert capsys.readouterr().out == (
        'Abandoned unreadable local staging evidence.\n'
        'No host deletion was attempted because repository coordinates could not be trusted.\n'
        'The source repository and publication repository were not changed.\n\n'
        'Inspect the host account for any private staging repository.\n'
    )


def test_stage_clean_explicitly_abandons_record_for_not_visible_repository(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _host_path, paths, global_config, project, _record = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    assert project._stage_repo is not None
    (tmp_path / 'fake-gh-calls.jsonl').unlink()
    _clean_responses(
        fake_gh,
        (project.owner, project._stage_repo),
        outcome='absent',
    )
    capsys.readouterr()
    prompts: list[str] = []

    def _approve(prompt: str) -> str:
        prompts.append(prompt)
        return 'yes'

    monkeypatch.setattr('builtins.input', _approve)

    _stage.stage_clean(paths, global_config, project, yes=False)

    assert _stage.load_stage(paths, project.name) is None
    assert prompts == [
        'Forget the local staging record without deleting the staging repository? [y/n] '
    ]
    output = capsys.readouterr().out
    assert output == (
        'Abandoned local staging evidence for an absent or inaccessible repository.\n'
        'No host repository was deleted.\n'
        'The source repository and publication repository were not changed.\n\n'
        'Inspect the host account to confirm whether the staging repository exists.\n'
    )
    assert 'harmless' not in output
    assert 'logs' not in output
    assert 'artifacts' not in output
    assert not any('DELETE' in call['args'] for call in _calls(tmp_path))


def test_stage_clean_explicitly_abandons_unreadable_record_without_host_access(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    stage = _stage.StageRecord('a' * 40, project.owner, 'staging-repo')
    _save_stage(paths, project.name, stage)
    stage_path = paths.data_dir / 'projects' / project.name / 'stage.toml'

    def _fail_record_read(path: Path, *, encoding: str | None = None) -> str:
        assert path == stage_path
        assert encoding == 'utf-8'
        raise OSError('read failed')

    def _approve(_prompt: str) -> str:
        return 'yes'

    monkeypatch.setattr(Path, 'read_text', _fail_record_read)
    monkeypatch.setattr('builtins.input', _approve)

    _stage.stage_clean(paths, global_config, project, yes=False)

    assert not stage_path.exists()
    assert _calls(tmp_path) == []


def test_stage_clean_incomplete_deletion_keeps_record_with_rerun_recovery(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_path, paths, global_config, project, _record = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    assert project._stage_repo is not None
    stage = _stage.load_stage(paths, project.name)
    (tmp_path / 'fake-gh-calls.jsonl').unlink()
    _clean_responses(
        fake_gh,
        (project.owner, project._stage_repo),
        outcome='incomplete',
    )

    with pytest.raises(PubanonError, match='staging deletion did not complete') as exc_info:
        _stage.stage_clean(paths, global_config, project, yes=True)

    assert exc_info.value.recovery == 'rerun pubanon stage clean'
    assert _stage.load_stage(paths, project.name) == stage


@pytest.mark.parametrize(
    ('answer', 'error'),
    [('no', 'evidence abandonment declined'), (None, 'interactive input is required')],
)
def test_stage_clean_malformed_decline_or_eof_keeps_record_without_host_access(
    answer: str | None,
    error: str,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    stage_path = paths.data_dir / 'projects' / project.name / 'stage.toml'
    stage_path.parent.mkdir(parents=True)
    stage_path.write_text('malformed', encoding='utf-8')

    def _respond(_prompt: str) -> str:
        if answer is None:
            raise EOFError
        return answer

    monkeypatch.setattr('builtins.input', _respond)

    with pytest.raises(PubanonError, match=error):
        _stage.stage_clean(paths, global_config, project, yes=False)

    assert stage_path.read_text(encoding='utf-8') == 'malformed'
    assert _calls(tmp_path) == []


def test_stage_clean_malformed_yes_refuses_and_keeps_record_without_host_access(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    stage_path = paths.data_dir / 'projects' / project.name / 'stage.toml'
    stage_path.parent.mkdir(parents=True)
    stage_path.write_text('malformed', encoding='utf-8')

    def _unexpected_prompt(_prompt: str) -> str:
        pytest.fail('--yes requested local evidence abandonment')

    monkeypatch.setattr('builtins.input', _unexpected_prompt)

    with pytest.raises(
        PubanonError,
        match='local staging evidence abandonment requires interactive confirmation',
    ) as exc_info:
        _stage.stage_clean(paths, global_config, project, yes=True)

    assert exc_info.value.recovery == 'run interactively: pubanon stage clean'
    assert stage_path.read_text(encoding='utf-8') == 'malformed'
    assert _calls(tmp_path) == []


def test_stage_clean_no_record_and_not_visible_preserves_host_uncertainty(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    assert project._stage_repo is not None
    stage_path = paths.data_dir / 'projects' / project.name / 'stage.toml'
    _clean_responses(fake_gh, (project.owner, project._stage_repo), outcome='absent')

    def _unexpected_prompt(_prompt: str) -> str:
        pytest.fail('stage clean prompted without local evidence')

    monkeypatch.setattr('builtins.input', _unexpected_prompt)

    _stage.stage_clean(paths, global_config, project, yes=True)

    assert not stage_path.exists()
    assert not any('DELETE' in call['args'] for call in _calls(tmp_path))
    assert capsys.readouterr().out == (
        'The staging repository is absent or inaccessible to this request.\n'
        'No local staging evidence was found, and no host deletion was attempted.\n'
        'The source repository and publication repository were not changed.\n\n'
        'Inspect the host account to confirm whether the staging repository exists.\n'
    )


def test_stage_clean_no_record_previews_configured_fallback_before_deletion(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    assert project._stage_repo is not None
    _clean_responses(fake_gh, (project.owner, project._stage_repo))

    def _approve(_prompt: str) -> str:
        preview = capsys.readouterr().out
        assert f'{project.owner}/{project._stage_repo}' in preview
        assert 'Selection: configured fallback' in preview
        assert not any('DELETE' in call['args'] for call in _calls(tmp_path))
        return 'yes'

    monkeypatch.setattr('builtins.input', _approve)
    _stage.stage_clean(paths, global_config, project, yes=False)
    assert any('DELETE' in call['args'] for call in _calls(tmp_path))
    output = capsys.readouterr().out
    assert 'Deletion read-back confirmed the selected staging repository is absent.' in output
    assert 'No local staging record existed.' in output
    assert 'harmless' not in output


def test_stage_clean_not_visible_decline_keeps_record(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_path, paths, global_config, project, _record = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    assert project._stage_repo is not None
    stage = _stage.load_stage(paths, project.name)
    (tmp_path / 'fake-gh-calls.jsonl').unlink()
    _clean_responses(fake_gh, (project.owner, project._stage_repo), outcome='absent')

    def _decline(_prompt: str) -> str:
        return 'no'

    monkeypatch.setattr('builtins.input', _decline)

    with pytest.raises(PubanonError, match='evidence abandonment declined'):
        _stage.stage_clean(paths, global_config, project, yes=False)

    assert _stage.load_stage(paths, project.name) == stage
    assert not any('DELETE' in call['args'] for call in _calls(tmp_path))


def test_stage_clean_not_visible_eof_keeps_record(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_path, paths, global_config, project, _record = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    assert project._stage_repo is not None
    stage = _stage.load_stage(paths, project.name)
    (tmp_path / 'fake-gh-calls.jsonl').unlink()
    _clean_responses(fake_gh, (project.owner, project._stage_repo), outcome='absent')

    def _end_of_input(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr('builtins.input', _end_of_input)

    with pytest.raises(PubanonError, match='interactive input is required'):
        _stage.stage_clean(paths, global_config, project, yes=False)

    assert _stage.load_stage(paths, project.name) == stage
    assert not any('DELETE' in call['args'] for call in _calls(tmp_path))


def test_stage_clean_not_visible_yes_refuses_and_keeps_record(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_path, paths, global_config, project, _record = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    assert project._stage_repo is not None
    stage = _stage.load_stage(paths, project.name)
    (tmp_path / 'fake-gh-calls.jsonl').unlink()
    _clean_responses(fake_gh, (project.owner, project._stage_repo), outcome='absent')

    def _unexpected_prompt(_prompt: str) -> str:
        pytest.fail('--yes requested local evidence abandonment')

    monkeypatch.setattr('builtins.input', _unexpected_prompt)

    with pytest.raises(PubanonError, match='requires interactive confirmation') as exc_info:
        _stage.stage_clean(paths, global_config, project, yes=True)

    assert exc_info.value.recovery == 'run interactively: pubanon stage clean'
    assert _stage.load_stage(paths, project.name) == stage
    assert not any('DELETE' in call['args'] for call in _calls(tmp_path))


def test_stage_clean_preserves_recorded_authority_across_current_gate_drift(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_path, paths, global_config, project, _record = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    stage = _stage.load_stage(paths, project.name)
    assert stage is not None
    changed = replace(
        project,
        _stage_repo='renamed-stage',
        extra_terms=(stage._repo,),
    )
    (tmp_path / 'fake-gh-calls.jsonl').unlink()
    _clean_responses(fake_gh, (stage._owner, stage._repo))

    output = StringIO()
    with redirect_stdout(output):
        _stage.stage_clean(paths, global_config, changed, yes=True)

    calls = [call['args'] for call in _calls(tmp_path)]
    assert ['api', '-X', 'DELETE', f'repos/{stage._owner}/{stage._repo}'] in calls
    assert f'{stage._owner}/{stage._repo}' in output.getvalue()


def test_stage_clean_propagates_unrelated_pubanon_error_with_invalid_record_message(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    stage = _stage.StageRecord('a' * 40, project.owner, 'staging-repo')
    _save_stage(paths, project.name, stage)
    stage_path = paths.data_dir / 'projects' / project.name / 'stage.toml'
    original_record = stage_path.read_bytes()

    def _fail_load(*_args: object, **_kwargs: object) -> Never:
        raise PubanonError('invalid stage record')

    def _unexpected_prompt(_prompt: str) -> Never:
        pytest.fail('unrelated PubanonError selected evidence abandonment')

    monkeypatch.setattr(_stage, 'load_stage', _fail_load)
    monkeypatch.setattr('builtins.input', _unexpected_prompt)

    with pytest.raises(PubanonError, match='invalid stage record'):
        _stage.stage_clean(paths, global_config, project, yes=False)

    assert stage_path.read_bytes() == original_record


def test_stage_clean_refuses_public_repository_without_deleting(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_path, paths, global_config, project, _record = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    assert project._stage_repo is not None
    stage = _stage.load_stage(paths, project.name)
    (tmp_path / 'fake-gh-calls.jsonl').unlink()
    _clean_responses(
        fake_gh,
        (project.owner, project._stage_repo),
        actual=(project.owner, project._stage_repo, 'public'),
    )

    with pytest.raises(PubanonError, match='staging repository facts') as exc_info:
        _stage.stage_clean(paths, global_config, project, yes=True)

    assert exc_info.value.recovery == 'inspect and delete the staging repository manually'
    assert _stage.load_stage(paths, project.name) == stage
    assert not any('DELETE' in call['args'] for call in _calls(tmp_path))


@pytest.mark.parametrize('yes', [False, True], ids=['interactive', 'yes'])
@pytest.mark.parametrize('case_only', [False, True], ids=['exact', 'case-only'])
def test_stage_clean_refuses_recorded_final_destination_before_host_access(
    yes: bool,
    case_only: bool,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    stage = _stage.StageRecord('a' * 40, project.owner, 'staging-repo')
    _save_stage(paths, project.name, stage)
    stage_path = paths.data_dir / 'projects' / project.name / 'stage.toml'
    original_record = stage_path.read_bytes()
    changed = replace(
        project,
        owner=stage._owner.upper() if case_only else stage._owner,
        repo=stage._repo.upper() if case_only else stage._repo,
        _stage_repo='renamed-stage',
    )

    def _unexpected(*_args: object, **_kwargs: object) -> Never:
        pytest.fail('final staging cleanup target reached confirmation or host access')

    monkeypatch.setattr('builtins.input', _unexpected)
    monkeypatch.setattr(_stage, 'require_authenticated_account', _unexpected)
    monkeypatch.setattr(_stage, 'repository_visible', _unexpected)
    monkeypatch.setattr(_stage, 'repository_facts', _unexpected)
    monkeypatch.setattr(_stage, 'delete_repository', _unexpected)

    with pytest.raises(
        PubanonError,
        match='staging cleanup target is the configured publication repository',
    ) as exc_info:
        _stage.stage_clean(paths, global_config, changed, yes=yes)

    assert exc_info.value.recovery == (
        'inspect the recorded staging target and correct the configuration before retrying'
    )
    assert stage_path.read_bytes() == original_record


def test_stage_clean_routes_account_requirement_with_staging_authority(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo()
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    requirements: list[tuple[str, str]] = []

    def _require_account(expected: str, *, authority: str) -> str:
        requirements.append((expected, authority))
        return expected

    def _repository_visible(_owner: str, _repo: str) -> bool:
        return False

    monkeypatch.setattr(_stage, 'require_authenticated_account', _require_account)
    monkeypatch.setattr(_stage, 'repository_visible', _repository_visible)

    _stage.stage_clean(paths, global_config, project, yes=True)

    assert requirements == [('stage-owner', 'staging configuration')]


def test_stage_clean_routes_invalid_record_subtype_independently_of_message(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    stage = _stage.StageRecord('a' * 40, project.owner, 'staging-repo')
    _save_stage(paths, project.name, stage)
    stage_path = paths.data_dir / 'projects' / project.name / 'stage.toml'

    def _fail_load(*_args: object, **_kwargs: object) -> Never:
        raise _stage._InvalidStageRecordError('different private subtype message')

    def _approve(_prompt: str) -> str:
        return 'yes'

    monkeypatch.setattr(_stage, 'load_stage', _fail_load)
    monkeypatch.setattr('builtins.input', _approve)

    _stage.stage_clean(paths, global_config, project, yes=False)

    assert not stage_path.exists()
    assert _calls(tmp_path) == []


@pytest.mark.parametrize('drift', ['repo', 'owner'])
def test_stage_clean_uses_whole_recorded_coordinate_pair_after_configuration_drift(
    drift: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_path, paths, global_config, project, _record = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    assert project._stage_repo is not None
    stage = _stage.load_stage(paths, project.name)
    assert stage is not None
    changed = (
        replace(project, _stage_repo='renamed-stage')
        if drift == 'repo'
        else replace(project, owner='changed-owner')
    )
    (tmp_path / 'fake-gh-calls.jsonl').unlink()
    _clean_responses(fake_gh, (stage._owner, stage._repo))

    output = StringIO()
    with redirect_stdout(output):
        _stage.stage_clean(paths, global_config, changed, yes=True)

    calls = [call['args'] for call in _calls(tmp_path)]
    assert ['api', '-X', 'DELETE', f'repos/{stage._owner}/{stage._repo}'] in calls
    forbidden_paths = {
        f'repos/{changed.owner}/{stage._repo}',
        f'repos/{changed.owner}/{changed._stage_repo}',
        f'repos/{stage._owner}/{changed._stage_repo}',
    } - {f'repos/{stage._owner}/{stage._repo}'}
    assert not any(
        argument in forbidden_paths
        for call in calls
        for argument in call
        if isinstance(argument, str)
    )
    assert (
        'configured staging coordinates changed; cleaning the recorded repository'
    ) in output.getvalue()
    assert f'{stage._owner}/{stage._repo}' in output.getvalue()


def test_stage_clean_yes_skips_confirmation(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_path, paths, global_config, project, _record = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    assert project._stage_repo is not None
    (tmp_path / 'fake-gh-calls.jsonl').unlink()
    _clean_responses(fake_gh, (project.owner, project._stage_repo))

    def _unexpected_prompt(_prompt: str) -> str:
        pytest.fail('--yes requested staging deletion confirmation')

    monkeypatch.setattr('builtins.input', _unexpected_prompt)

    _stage.stage_clean(paths, global_config, project, yes=True)

    assert any('DELETE' in call['args'] for call in _calls(tmp_path))


def test_stage_destination_reports_gate_term_and_coordinate_field(
    tmp_path: Path,
    make_repo: Callable[..., Path],
) -> None:
    term = 'hygiene-canary'
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    project = replace(
        project,
        _stage_repo=f'stage-repo-{term}',
        hygiene_terms=(term,),
    )

    with pytest.raises(PubanonError, match='repository repo failed') as exc_info:
        _stage._stage_destination(paths, global_config, project)

    assert exc_info.value.fact == f'repo: stage-repo-{term}'
    assert term in (exc_info.value.fact or '')
    assert _calls(tmp_path) == []


@pytest.mark.parametrize('candidate_unit', ['path', 'data'])
def test_stage_push_reproves_dynamic_candidate_gates_before_every_host_access(
    candidate_unit: str,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    if candidate_unit == 'path' and os.name == 'nt':
        pytest.skip('an absolute Windows home cannot occur in a Git tree path')
    private_term = str(tmp_path / 'private-home') if os.name == 'nt' else '/private-home'
    files = (
        {'docs/private-home/README.md': 'public\n'}
        if candidate_unit == 'path'
        else {'README.md': f'{private_term}\n'}
    )
    source = make_repo(files=files)
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    monkeypatch.setenv('HOME', str(tmp_path / 'initial-home'))
    candidate = _build_candidate(paths, global_config, project)
    stage = _stage.StageRecord('a' * 40, project.owner, 'staging-repo')
    _save_stage(paths, project.name, stage)
    monkeypatch.setenv('HOME', private_term)

    def _unexpected_host(*_args: object, **_kwargs: object) -> Never:
        pytest.fail('host access occurred before retained-candidate proof')

    monkeypatch.setattr(_stage, 'require_authenticated_account', _unexpected_host)
    monkeypatch.setattr(_stage, 'create_repository', _unexpected_host)
    monkeypatch.setattr(_stage, 'repository_facts', _unexpected_host)
    monkeypatch.setattr(_stage, 'repository_visible', _unexpected_host)
    monkeypatch.setattr(_stage, 'stage_refs', _unexpected_host)
    monkeypatch.setattr(_stage, 'push', _unexpected_host)

    with pytest.raises(PubanonError, match='snapshot failed current content gates') as exc_info:
        _stage.stage_push(paths, global_config, project, yes=True)

    assert exc_info.value.recovery == 'pubanon discard, then pubanon prepare'
    assert _candidate.load_candidate(paths, project.name) == candidate
    assert _stage.load_stage(paths, project.name) == stage


def test_stage_push_routes_account_requirement_after_retained_proof(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    _build_candidate(paths, global_config, project)
    events: list[str] = []
    requirements: list[tuple[str, str]] = []
    real_verify = cast('Callable[..., Snapshot]', _stage.verify_retained_candidate)

    def _verify_candidate(*args: object, **kwargs: object) -> Snapshot:
        snapshot = real_verify(*args, **kwargs)
        events.append('proof')
        return snapshot

    def _require_account(expected: str, *, authority: str) -> str:
        events.append('account')
        requirements.append((expected, authority))
        raise PubanonError('account authority observed')

    monkeypatch.setattr(_stage, 'verify_retained_candidate', _verify_candidate)
    monkeypatch.setattr(_stage, 'require_authenticated_account', _require_account)

    with pytest.raises(PubanonError, match='account authority observed'):
        _stage.stage_push(paths, global_config, project, yes=True)

    assert events == ['proof', 'account']
    assert requirements == [('stage-owner', 'staging configuration')]


def test_stage_record_failure_names_clean_and_preserves_records(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    canary = 'private-save-canary'
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    record = _build_candidate(paths, global_config, project)
    old_stage = _stage.StageRecord('a' * 40, project.owner, 'staging-repo')
    _save_stage(paths, project.name, old_stage)
    _host_responses(fake_gh, project)
    reads = iter([(old_stage.commit, ()), (record.commit, ())])

    def _staged_refs(*_args: object) -> tuple[str | None, tuple[str, ...]]:
        return next(reads)

    def _fail_save(*_args: object) -> None:
        raise PubanonError(canary)

    def _successful_push(*_args: object) -> GitResult:
        return GitResult(0, b'', b'')

    monkeypatch.setattr(_stage, 'stage_refs', _staged_refs)
    monkeypatch.setattr(_stage, 'push', _successful_push)
    monkeypatch.setattr(_stage, '_save_stage', _fail_save)

    with pytest.raises(PubanonError, match='cannot record staging push') as exc_info:
        _stage.stage_push(paths, global_config, project, yes=True)

    assert exc_info.value.recovery == 'pubanon stage clean'
    assert canary not in exc_info.value.render()
    assert _candidate.load_candidate(paths, project.name) == record
    assert _stage.load_stage(paths, project.name) == old_stage


@pytest.mark.parametrize('field', ['owner', 'repo'])
@pytest.mark.parametrize(
    'value',
    ['', 'two/segments', 'repo?query', 'repo#fragment', 'repo\x01control', ' padded '],
)
def test_stage_record_refuses_unsafe_recorded_host_segments(
    field: str,
    value: str,
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    python_field = f'_{field}'
    record = replace(
        _stage.StageRecord('a' * 40, 'stage-owner', 'stage-repo'),
        **{python_field: value},
    )

    with pytest.raises(PubanonError, match='invalid stage record'):
        _save_stage(paths, 'example', record)

    assert not (paths.data_dir / 'projects' / 'example' / 'stage.toml').exists()


def test_stage_status_gates_current_coordinates_before_host_access(
    tmp_path: Path,
    make_repo: Callable[..., Path],
) -> None:
    term = 'private-stage'
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    project = replace(
        project,
        _stage_repo=f'public-{term}',
        extra_terms=(term,),
        required_checks=('build',),
    )
    assert project._stage_repo is not None
    _save_stage(
        paths,
        project.name,
        _stage.StageRecord('a' * 40, project.owner, project._stage_repo),
    )

    with pytest.raises(PubanonError, match='repository repo failed content gates') as exc_info:
        _stage.stage_status(paths, global_config, project)

    assert term in exc_info.value.render()
    assert _calls(tmp_path) == []


def test_stage_status_lists_required_checks_in_order_for_the_exact_staged_commit(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_path, paths, global_config, project, record = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    project = replace(project, required_checks=('Build check', 'Tests'))
    (tmp_path / 'fake-gh-calls.jsonl').unlink()
    _status_responses(
        fake_gh,
        project,
        record.commit,
        {'Build check': 'success', 'Tests': 'success'},
    )

    output = StringIO()
    with redirect_stdout(output):
        _stage.stage_status(paths, global_config, project)

    assert output.getvalue().splitlines() == [
        'All configured required checks succeeded for the staged snapshot.',
        '',
        'Details',
        f'  Staged commit: {record.commit}',
        '  Build check: success (success)',
        '  Tests: success (success)',
        '',
        'Next: pubanon publish',
    ]
    check_calls = [
        call['args'][1]
        for call in _calls(tmp_path)
        if call['args'][:1] == ['api'] and '/check-runs?' in call['args'][1]
    ]
    assert check_calls == [
        (
            f'repos/{project.owner}/{project._stage_repo}/commits/{record.commit}/check-runs?'
            'check_name=Build+check&filter=latest&per_page=100'
        ),
        (
            f'repos/{project.owner}/{project._stage_repo}/commits/{record.commit}/check-runs?'
            'check_name=Tests&filter=latest&per_page=100'
        ),
    ]
    assert all('refs/heads/stage' not in path for path in check_calls)


@pytest.mark.parametrize(
    'case',
    [
        (
            'pending',
            'pending',
            'wait for the check, then rerun `pubanon stage status`',
        ),
        (
            'missing',
            'missing',
            ('check the configured name and workflow trigger, then rerun `pubanon stage status`'),
        ),
        (
            'ambiguous',
            'ambiguous',
            'resolve the multiple matching check runs, then rerun `pubanon stage status`',
        ),
        (
            'cancelled',
            'unacceptable',
            (
                'inspect the failed check and fix or rerun its workflow, then rerun '
                '`pubanon stage status`'
            ),
        ),
        (
            'skipped',
            'unacceptable',
            (
                'inspect the failed check and fix or rerun its workflow, then rerun '
                '`pubanon stage status`'
            ),
        ),
        (
            'stale',
            'unacceptable',
            (
                'inspect the failed check and fix or rerun its workflow, then rerun '
                '`pubanon stage status`'
            ),
        ),
        (
            'failure',
            'unacceptable',
            (
                'inspect the failed check and fix or rerun its workflow, then rerun '
                '`pubanon stage status`'
            ),
        ),
    ],
)
def test_stage_status_refuses_every_non_success_check_state(
    case: tuple[str, str, str],
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_state, reported_state, recovery = case
    _host_path, paths, global_config, project, record = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    project = replace(project, required_checks=('CI',))
    _status_responses(fake_gh, project, record.commit, {'CI': host_state})
    stage_before = _stage.load_stage(paths, project.name)
    assert stage_before is not None

    output = StringIO()
    with (
        redirect_stdout(output),
        pytest.raises(PubanonError, match='not acceptable') as exc_info,
    ):
        _stage.stage_status(paths, global_config, project)

    rendered = output.getvalue()
    assert f'CI: {reported_state}' in rendered
    if host_state not in {'pending', 'missing', 'ambiguous'}:
        assert host_state in rendered
    assert exc_info.value.fact == f'CI:{reported_state}'
    assert exc_info.value.recovery == recovery
    assert _stage.load_stage(paths, project.name) == stage_before
    assert not any('-X' in call['args'] for call in _calls(tmp_path))


def test_stage_status_routes_account_requirement_with_staging_authority(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    project = replace(project, required_checks=('CI',))
    assert project._stage_repo is not None
    _save_stage(
        paths,
        project.name,
        _stage.StageRecord('a' * 40, project.owner, project._stage_repo),
    )
    _candidate.store_path(paths, project.name)
    requirements: list[tuple[str, str]] = []

    def _require_account(expected: str, *, authority: str) -> str:
        requirements.append((expected, authority))
        raise PubanonError('account authority observed')

    monkeypatch.setattr(_stage, 'require_authenticated_account', _require_account)

    with pytest.raises(PubanonError, match='account authority observed'):
        _stage.stage_status(paths, global_config, project)

    assert requirements == [('stage-owner', 'staging configuration')]


def test_stage_status_uses_shared_staged_target_verification_before_checks(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    project = replace(project, required_checks=('CI',))
    assert project._stage_repo is not None
    record = _stage.StageRecord('a' * 40, project.owner, project._stage_repo)
    destination = _host.Destination(
        adapter=project.host,
        owner=record._owner,
        repo=record._repo,
        url='https://github.com/stage-owner/staging-repo.git',
        account=global_config.accounts[project.host],
        visibility='private',
    )
    calls: list[str] = []

    def _verify(
        selected_paths: Paths,
        selected_global: GlobalConfig,
        selected_project: ProjectConfig,
        *,
        timeout: float | None = None,
    ) -> tuple[_host.Destination, _stage.StageRecord]:
        assert (selected_paths, selected_global, selected_project) == (
            paths,
            global_config,
            project,
        )
        assert timeout is None
        calls.append('verify')
        return destination, record

    def _check(
        _coordinates: tuple[str, str],
        commit: str,
        _name: str,
        *,
        timeout: float | None = None,
    ) -> _host.CheckVerdict:
        assert timeout is None
        assert commit == record.commit
        calls.append('check')
        return _host.CheckVerdict('CI', 'success', 'success')

    monkeypatch.setattr(_stage, 'verify_staged_target', _verify)
    monkeypatch.setattr(_stage, 'check_result', _check)

    _stage.stage_status(paths, global_config, project)

    assert calls == ['verify', 'check']


def test_stage_status_without_required_checks_recommends_publication(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_path, paths, global_config, project, record = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    (tmp_path / 'fake-gh-calls.jsonl').unlink()
    _status_responses(fake_gh, project, record.commit, {})

    output = StringIO()
    with redirect_stdout(output):
        _stage.stage_status(paths, global_config, project)

    assert output.getvalue().splitlines() == [
        'No required checks are configured for the staged snapshot.',
        '',
        f'Staged commit: {record.commit}',
        'Next: pubanon publish',
    ]
    assert not any('/check-runs?' in ' '.join(call['args']) for call in _calls(tmp_path))


def test_surprise_remote_tip_refuses_before_push_and_names_clean(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, paths, global_config, project, _record = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    (project.source_path / 'README.md').write_text('remote advance\n')
    _git(project.source_path, 'add', 'README.md')
    _git(
        project.source_path,
        '-c',
        'user.name=Test User',
        '-c',
        'user.email=test@example.invalid',
        'commit',
        '-qm',
        'remote advance',
    )
    remote_tip = _git(project.source_path, 'rev-parse', 'HEAD').decode().strip()
    _git(host, 'fetch', '-q', project.source_path.as_posix(), f'+{remote_tip}:refs/heads/stage')

    def _unexpected_push(*_args: object, **_kwargs: object) -> GitResult:
        pytest.fail('surprise staging state reached push')

    monkeypatch.setattr(_stage, 'push', _unexpected_push)

    with pytest.raises(PubanonError, match='surprise staging state') as exc_info:
        _stage.stage_push(paths, global_config, project, yes=True)

    assert exc_info.value.recovery == (
        'run `pubanon stage clean`, then restage with `pubanon stage push`'
    )


def test_unexpected_remote_ref_refuses_before_push(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    record = _build_candidate(paths, global_config, project)
    _git(
        host,
        'fetch',
        '-q',
        _candidate.store_path(paths, project.name).as_posix(),
        f'{record.commit}:refs/heads/main',
    )
    _host_responses(fake_gh, project)
    monkeypatch.setattr(_stage, 'URL_TEMPLATE', host.as_uri())

    def _unexpected_push(*_args: object, **_kwargs: object) -> GitResult:
        pytest.fail('unexpected staging ref reached push')

    monkeypatch.setattr(_stage, 'push', _unexpected_push)

    with pytest.raises(PubanonError, match='unexpected staging reference') as exc_info:
        _stage.stage_push(paths, global_config, project, yes=True)

    assert exc_info.value.recovery == 'pubanon stage clean'


def test_verify_stage_facts_preserves_staging_recovery_on_404(fake_gh: Path) -> None:
    destination = _host.Destination(
        adapter='github',
        owner='publication-owner',
        repo='stage-repo',
        url='https://github.com/publication-owner/stage-repo.git',
        account='publication-owner',
        visibility='private',
    )
    fake_gh.write_text(
        json.dumps(
            [
                {
                    'args': [
                        'api',
                        'repos/publication-owner/stage-repo',
                        '--jq',
                        '{owner: .owner.login, repo: .name, visibility: .visibility}',
                    ],
                    'returncode': 1,
                    'stdout': '{"status":"404"}',
                }
            ]
        )
    )

    with pytest.raises(PubanonError) as exc_info:
        _stage._verify_stage_facts(destination, recovery='pubanon stage clean')

    assert str(exc_info.value) == 'Cannot access GitHub repository publication-owner/stage-repo'
    assert exc_info.value.recovery == 'pubanon stage clean'


def test_verify_staged_target_propagates_timeout_to_ordered_host_reads(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    record = _stage.StageRecord('a' * 40, project.owner, 'staging-repo')
    _save_stage(paths, project.name, record)
    _candidate.store_path(paths, project.name)
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo='staging-repo',
        url='https://github.com/stage-owner/staging-repo.git',
        account='stage-owner',
        visibility='private',
    )
    calls: list[tuple[str, float | None]] = []

    def _stage_destination(
        _paths: Paths,
        _global_config: GlobalConfig,
        _project: ProjectConfig,
    ) -> _host.Destination:
        return destination

    monkeypatch.setattr(_stage, '_stage_destination', _stage_destination)

    def _require_account(
        expected: str,
        *,
        authority: str,
        timeout: float | None = None,
    ) -> str:
        assert (expected, authority) == ('stage-owner', 'staging configuration')
        calls.append(('account', timeout))
        return expected

    def _verify_facts(
        selected: _host.Destination,
        *,
        recovery: str,
        timeout: float | None = None,
    ) -> None:
        assert (selected, recovery) == (destination, 'pubanon stage clean')
        calls.append(('repository', timeout))

    def _refs(
        selected: _host.Destination,
        selected_paths: Paths,
        project_name: str,
        *,
        timeout: float | None = None,
    ) -> tuple[str | None, tuple[str, ...]]:
        assert (selected, selected_paths, project_name) == (destination, paths, project.name)
        calls.append(('refs', timeout))
        return record.commit, ()

    monkeypatch.setattr(_stage, 'require_authenticated_account', _require_account)
    monkeypatch.setattr(_stage, '_verify_stage_facts', _verify_facts)
    monkeypatch.setattr(_stage, 'stage_refs', _refs)
    times = iter([0.0, 0.0, 1.0, 2.0])
    monkeypatch.setattr(_stage, '_monotonic', lambda: next(times))

    assert _stage.verify_staged_target(paths, global_config, project, timeout=8.0) == (
        destination,
        record,
    )
    assert calls == [('account', 8.0), ('repository', 7.0), ('refs', 6.0)]


@pytest.mark.parametrize('drift', ['repo', 'owner'])
def test_verify_staged_target_refuses_coordinate_drift_before_host_access(
    drift: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_path, paths, global_config, project, _record = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    (tmp_path / 'fake-gh-calls.jsonl').unlink()
    changed = replace(
        project,
        required_checks=('CI',),
        _stage_repo='renamed-stage' if drift == 'repo' else project._stage_repo,
        owner='changed-owner' if drift == 'owner' else project.owner,
    )

    with pytest.raises(PubanonError, match='coordinates') as exc_info:
        _stage.verify_staged_target(paths, global_config, changed)

    assert exc_info.value.recovery == 'pubanon stage clean'
    assert _calls(tmp_path) == []


def test_verify_staged_target_refuses_drifted_remote_tip_with_push_recovery(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, paths, global_config, project, record = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    (project.source_path / 'README.md').write_text('remote advance\n')
    _git(project.source_path, 'add', 'README.md')
    _git(
        project.source_path,
        '-c',
        'user.name=Test User',
        '-c',
        'user.email=test@example.invalid',
        'commit',
        '-qm',
        'remote advance',
    )
    remote_tip = _git(project.source_path, 'rev-parse', 'HEAD').decode().strip()
    _git(host, 'fetch', '-q', project.source_path.as_posix(), f'+{remote_tip}:refs/heads/stage')
    _host_responses(fake_gh, project)

    with pytest.raises(PubanonError, match='staging tip') as exc_info:
        _stage.verify_staged_target(paths, global_config, project)

    assert exc_info.value.expected == record.commit
    assert exc_info.value.actual == remote_tip
    assert exc_info.value.recovery == 'pubanon stage push'


def test_verify_staged_target_refuses_malformed_record_before_host_access(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    stage_path = paths.data_dir / 'projects' / project.name / 'stage.toml'
    stage_path.parent.mkdir(parents=True)
    stage_path.write_text('malformed', encoding='utf-8')

    def _unexpected(*_args: object) -> object:
        pytest.fail('malformed staging evidence reached host access')

    monkeypatch.setattr(_stage, 'repository_facts', _unexpected)
    monkeypatch.setattr(_stage, 'stage_refs', _unexpected)

    with pytest.raises(PubanonError, match='invalid stage record') as exc_info:
        _stage.verify_staged_target(paths, global_config, project)

    assert exc_info.value.recovery == 'run interactively: pubanon stage clean'
    assert _calls(tmp_path) == []


def test_verify_staged_target_refuses_missing_stage_record_with_push_recovery(
    tmp_path: Path,
    make_repo: Callable[..., Path],
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    project = replace(project, required_checks=('CI',))

    with pytest.raises(PubanonError, match='no staged commit is recorded') as exc_info:
        _stage.verify_staged_target(paths, global_config, project)

    assert exc_info.value.recovery == 'pubanon stage push'
    assert _calls(tmp_path) == []


def test_verify_staged_target_refuses_missing_store_without_creating_it_or_reading_refs(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    project = replace(project, required_checks=('CI',))
    assert project._stage_repo is not None
    _save_stage(
        paths,
        project.name,
        _stage.StageRecord('a' * 40, project.owner, project._stage_repo),
    )
    _host_responses(fake_gh, project)
    store = _candidate.candidate_store_path(paths, project.name)
    assert not store.exists()

    def _unexpected_refs(*_args: object) -> tuple[str | None, tuple[str, ...]]:
        pytest.fail('missing store reached staging ref read')

    monkeypatch.setattr(_stage, 'stage_refs', _unexpected_refs)

    with pytest.raises(PubanonError, match='snapshot store') as exc_info:
        _stage.verify_staged_target(paths, global_config, project)

    assert exc_info.value.recovery == 'pubanon repo verify --repair-store'
    assert not store.exists()
    assert _calls(tmp_path) == []


def test_verify_staged_target_refuses_publication_repository_alias_before_host_reads(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    global_config, project = _configs(tmp_path / 'source')
    project = replace(project, _stage_repo=project.repo)
    _save_stage(
        paths,
        project.name,
        _stage.StageRecord('a' * 40, project.owner, project.repo),
    )

    def _unexpected(*_args: object) -> None:
        pytest.fail('host read occurred before the staging-alias precheck')

    monkeypatch.setattr(_stage, 'repository_facts', _unexpected)

    with pytest.raises(PubanonError, match='must differ'):
        _stage.verify_staged_target(paths, global_config, project, timeout=30)


def test_verify_staged_target_refuses_unexpected_remote_refs(
    tmp_path: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    global_config, project = _configs(tmp_path / 'source')
    _save_stage(
        paths,
        project.name,
        _stage.StageRecord('a' * 40, project.owner, 'staging-repo'),
    )

    def _refs(
        _dest: _host.Destination,
        _paths: Paths,
        _project: str,
        *,
        timeout: float | None = None,
    ) -> tuple[str | None, tuple[str, ...]]:
        assert timeout is not None
        assert timeout > 0
        return 'a' * 40, ('refs/tags/v1',)

    _candidate.store_path(paths, project.name)
    _host_responses(fake_gh, project)
    monkeypatch.setattr(_stage, 'stage_refs', _refs)

    with pytest.raises(PubanonError, match='unexpected staging reference') as raised:
        _stage.verify_staged_target(paths, global_config, project, timeout=30)

    assert raised.value.recovery == 'pubanon stage clean'


def test_verify_staged_target_requires_private_visibility_before_refs(
    tmp_path: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    global_config, project = _configs(tmp_path / 'source')
    _save_stage(
        paths,
        project.name,
        _stage.StageRecord('a' * 40, project.owner, 'staging-repo'),
    )
    _candidate.store_path(paths, project.name)
    _host_responses(fake_gh, project, actual=(project.owner, 'staging-repo', 'public'))

    def _unexpected(*_args: object) -> tuple[str | None, tuple[str, ...]]:
        pytest.fail('reference read occurred before private-visibility verification')

    monkeypatch.setattr(_stage, 'stage_refs', _unexpected)

    with pytest.raises(PubanonError, match='staging repository facts') as exc_info:
        _stage.verify_staged_target(paths, global_config, project, timeout=30)

    assert exc_info.value.expected is not None
    assert 'visibility=private' in exc_info.value.expected
    assert exc_info.value.actual is not None
    assert 'visibility=public' in exc_info.value.actual
    assert exc_info.value.recovery == 'pubanon stage clean'


def test_yes_cannot_create_absent_stage_repository_with_closed_stdin(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    _build_candidate(paths, global_config, project)
    _host_responses(fake_gh, project, absent=True)
    monkeypatch.setattr(_stage, 'URL_TEMPLATE', host.as_uri())

    def _closed(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr('builtins.input', _closed)

    with pytest.raises(PubanonError, match='interactive input is required'):
        _stage.stage_push(paths, global_config, project, yes=True)

    assert not any('POST' in call['args'] for call in _calls(tmp_path))
