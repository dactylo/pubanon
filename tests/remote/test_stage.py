import json
import os
import subprocess
from collections.abc import Callable, Sequence
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Never, cast

import pytest

from pubanon._config import (
    GlobalConfig,
    Paths,
    ProjectConfig,
    Settings,
    fingerprint,
    stage_repository,
)
from pubanon._errors import OperationCanceled, PubanonError
from pubanon._remote import _host, _stage
from pubanon._remote._stage import _clear_stage, _save_stage
from pubanon._run import GitResult
from pubanon._snapshots import _candidate, _records
from pubanon._snapshots._source import (
    PinnedSource,
    Snapshot,
    describe_bookmark,
    pin,
    read_snapshot,
)
from pubanon._terminal import verbose_context
from tests._cli import gh_calls as _calls
from tests._git import git_env
from tests._permissions import assert_private_mode
from tests._setup import configure
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


def _fail_stage_refs_read(*_args: object, **_kwargs: object) -> Never:
    raise PubanonError('cannot read remote refs')


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ['git', '-C', repo, *args],
        check=True,
        capture_output=True,
        env=git_env(),
    ).stdout


def _new_host(tmp_path: Path) -> Path:
    host = tmp_path / 'stage.git'
    subprocess.run(['git', 'init', '--bare', '-q', host], check=True, env=git_env())
    return host


def _paths(tmp_path: Path) -> Paths:
    config_dir = tmp_path / 'config' / 'pubanon'
    data_dir = tmp_path / 'data' / 'pubanon'
    return Paths(config_dir, data_dir, config_dir / 'config.toml', data_dir / 'registry.json')


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
    responses.append(
        {
            'args': [
                'api',
                f'repos/{project.owner}/{stage_repo}',
                '--jq',
                _host._REPOSITORY_SETTINGS_QUERY,
            ],
            'stdout': json.dumps(
                {
                    'description': 'host description',
                    'default_branch': 'main',
                    'issues': True,
                    'wiki': False,
                    'projects': True,
                    'pull_requests': False,
                    'discussions': False,
                }
            )
            + '\n',
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

    def _record_refs(*_args: object) -> _host.RemoteRefs:
        transport_calls.append('read_remote_refs')
        return _host.RemoteRefs(None, {}, ())

    def _record_push(*_args: object) -> GitResult:
        transport_calls.append('push')
        return GitResult(0, b'', b'')

    monkeypatch.setattr(_stage, 'read_remote_refs', _record_refs)
    monkeypatch.setattr(_stage, 'push', _record_push)

    with pytest.raises(PubanonError, match='configuration changed') as exc_info:
        _stage.stage_push(paths, changed_config, project, yes=True)

    assert exc_info.value.recovery == 'pubanon discard, then pubanon prepare'
    assert _calls(tmp_path) == []
    assert transport_calls == []
    assert _candidate.load_candidate(paths, project.name) == record


def test_candidate_next_action_prioritizes_snapshot_recovery_and_missing_candidate(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, g, configured = configure(tmp_path, monkeypatch, source)
    assert configured is not None
    p = replace(configured, publication_route='staged')
    record = _build_candidate(paths, g, p)
    assert _stage.candidate_next_action(paths, g, p, None) == 'pubanon prepare'
    assert (
        _stage.candidate_next_action(
            paths, g, p, replace(record, state=_candidate.STATE_UNKNOWN_OUTCOME)
        )
        == 'pubanon publish --resolve'
    )
    changed = replace(p, extra_terms=('new-sensitive-term',))
    assert _stage.candidate_next_action(paths, g, changed, record) == (
        _candidate.inspect_next_action(record, g, changed)
    )


@pytest.mark.parametrize(
    'case',
    [
        ('direct', 'malformed', True, None, 'pubanon publish'),
        ('staged', 'absent', True, None, 'pubanon stage push'),
        ('staged', 'wrong-tip', True, None, 'pubanon stage push'),
        ('staged', 'wrong-destination', True, None, 'pubanon stage repair'),
        ('staged', 'malformed', True, None, 'pubanon stage repair'),
        ('staged', 'matching', True, None, 'pubanon stage watch'),
        ('staged', 'matching', True, 'match', 'pubanon publish'),
        ('staged', 'matching', True, 'old', 'pubanon stage watch'),
        ('staged', 'matching', False, None, 'pubanon publish'),
    ],
)
def test_candidate_next_action_uses_local_route_and_exact_checked_tip(
    case: tuple[str, str, bool, str | None, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    route, stage_state, checks, checked, expected = case
    paths = _paths(tmp_path)
    g, configured = _configs(tmp_path / 'source')
    p = replace(
        configured,
        publication_route=route,
        required_checks=('build',) if checks else (),
    )
    record = _records.CandidateRecord(
        commit='a' * 40,
        tree='b' * 40,
        snapshot_digest='c' * 64,
        tag_intent=None,
        fingerprint=fingerprint(g, p),
        pinned=PinnedSource('d' * 40, 'ref', 'refs/heads/main', 'd' * 40),
        destination=_records.RecordedDestination(
            'github', p.owner, p.repo, p.visibility, g.accounts['github']
        ),
        prestate_main=None,
        prestate_tags={},
        state=_candidate.STATE_RETAINED,
        message='Public snapshot',
    )
    tip = _records.candidate_tip(record)
    if stage_state == 'matching':
        _save_stage(paths, p.name, _stage.StageRecord(tip, p.owner, stage_repository(p)))
    elif stage_state == 'wrong-tip':
        _save_stage(paths, p.name, _stage.StageRecord('f' * 40, p.owner, stage_repository(p)))
    elif stage_state == 'wrong-destination':
        _save_stage(paths, p.name, _stage.StageRecord(tip, p.owner, 'other-stage'))
    elif stage_state == 'malformed':
        stage_path = paths.data_dir / 'projects' / p.name / 'stage.toml'
        stage_path.parent.mkdir(parents=True, exist_ok=True)
        stage_path.write_text('invalid = true\n')

    def _unexpected_host(*_args: object, **_kwargs: object) -> Never:
        pytest.fail('network read')

    monkeypatch.setattr(_stage, 'repository_facts', _unexpected_host)
    monkeypatch.setattr(_stage, 'read_remote_refs', _unexpected_host)

    assert (
        _stage.candidate_next_action(
            paths,
            g,
            p,
            record,
            checked_commit=tip if checked == 'match' else 'e' * 40 if checked else None,
        )
        == expected
    )


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
    responses.append(
        {
            'args': [
                'api',
                f'repos/{project.owner}/{stage_repo}',
                '--jq',
                _host._REPOSITORY_SETTINGS_QUERY,
            ],
            'stdout': json.dumps(
                {
                    'description': 'host description',
                    'default_branch': 'main',
                    'issues': True,
                    'wiki': False,
                    'projects': True,
                    'pull_requests': False,
                    'discussions': False,
                }
            )
            + '\n',
        }
    )
    if project.settings is not None:
        settings = project.settings
        for response in responses:
            args = cast('list[str]', response['args'])
            if args[2:4] == ['POST', 'user/repos']:
                args.extend(['-f', f'description={settings.description}'])
                for name in ('issues', 'wiki', 'projects', 'discussions'):
                    args.extend(['-F', f'has_{name}={str(getattr(settings, name)).lower()}'])
            elif args[-1] == _host._REPOSITORY_SETTINGS_QUERY:
                response['stdout'] = json.dumps(
                    {
                        name: getattr(settings, name)
                        for name in (
                            'description',
                            'default_branch',
                            'issues',
                            'wiki',
                            'projects',
                            'pull_requests',
                            'discussions',
                        )
                    }
                )
        responses.append(
            {
                'args': [
                    'api',
                    '-X',
                    'PATCH',
                    f'repos/{project.owner}/{stage_repo}',
                    '-F',
                    f'has_pull_requests={str(settings.pull_requests).lower()}',
                ]
            }
        )
    fake_gh.write_text(json.dumps(responses) + '\n')


def _prepare_stage(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    checks: tuple[str, ...] = (),
) -> tuple[Path, Paths, GlobalConfig, ProjectConfig, _candidate.CandidateRecord]:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    project = replace(project, required_checks=checks)
    record = _build_candidate(paths, global_config, project)
    _host_responses(fake_gh, project)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', host.as_uri())
    _stage.stage_push(paths, global_config, project, yes=True)
    return host, paths, global_config, project, record


def test_candidate_next_action_uses_replacement_resulting_tip(tmp_path: Path) -> None:
    paths, g, configured, record = _complete_replacement(tmp_path, stage=True)
    tip = record.tip
    assert tip is not None
    assert tip != record.commit
    p = replace(configured, publication_route='staged', required_checks=('build',))
    record = replace(record, fingerprint=_candidate.fingerprint(g, p))
    _save_stage(
        paths,
        p.name,
        _stage.StageRecord(record.commit, p.owner, stage_repository(p)),
    )
    assert _stage.candidate_next_action(paths, g, p, record, checked_commit=record.commit) == (
        'pubanon stage push'
    )
    _save_stage(
        paths,
        p.name,
        _stage.StageRecord(tip, p.owner, stage_repository(p)),
    )
    assert _stage.candidate_next_action(paths, g, p, record, checked_commit=tip) == (
        'pubanon publish'
    )


@pytest.mark.parametrize(
    ('candidate_state', 'error'),
    [
        ('absent', 'no prepared snapshot is retained'),
        ('unknown', 'the outcome of the last publication is unknown'),
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
    assert create_args in _calls(tmp_path)


def test_delete_readback_cancellation_preserves_record(
    tmp_path: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    global_config, project = _configs(tmp_path / 'source')
    record = _stage.StageRecord('a' * 40, project.owner, 'staging-repo')
    _save_stage(paths, project.name, record)
    _clean_responses(fake_gh, (project.owner, 'staging-repo'))
    reads = 0

    def _visible(_owner: str, _repo: str) -> bool:
        nonlocal reads
        reads += 1
        if reads > 1:
            raise OperationCanceled('operator interrupted deletion read-back')
        return True

    monkeypatch.setattr(_stage, 'repository_visible', _visible)

    with pytest.raises(OperationCanceled, match='operator interrupted deletion read-back'):
        _stage.stage_delete(paths, global_config, project, yes=True)

    assert _stage.load_stage(paths, project.name) == record


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
    reads = iter([_host.RemoteRefs(None, {}, ()), _host.RemoteRefs('b' * 40, {}, ())])

    def _staged_refs(*_args: object) -> _host.RemoteRefs:
        return next(reads)

    def _successful_push(*_args: object) -> GitResult:
        return GitResult(0, b'', b'')

    monkeypatch.setattr(_stage, 'read_remote_refs', _staged_refs)
    monkeypatch.setattr(_stage, 'push', _successful_push)

    with pytest.raises(PubanonError, match='read-back') as exc_info:
        _stage.stage_push(paths, global_config, project, yes=True)

    assert exc_info.value.recovery == 'pubanon stage repair'
    assert _stage.load_stage(paths, project.name) is None


def test_first_push_uses_only_candidate_main_ref_and_records_exact_coordinates(
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

    assert calls[0] == ['api', 'user', '--jq', '.login']
    assert _git(host, 'rev-parse', 'refs/heads/main') == f'{record.commit}\n'.encode()
    assert _git(host, 'for-each-ref', '--format=%(refname)') == b'refs/heads/main\n'
    assert _stage.load_stage(paths, project.name) == _stage.StageRecord(
        record.commit,
        project.owner,
        project._stage_repo,
    )
    stage_path = paths.data_dir / 'projects' / project.name / 'stage.toml'
    assert_private_mode(stage_path, 0o600)


def test_load_stage_refuses_unknown_schema_with_repair_or_forget_recovery(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    stage_path = paths.data_dir / 'projects' / 'example' / 'stage.toml'
    stage_path.parent.mkdir(parents=True)
    stage_path.write_text('commit = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\nextra = "x"\n')

    with pytest.raises(PubanonError, match='invalid stage record') as exc_info:
        _stage.load_stage(paths, 'example')

    assert exc_info.value.recovery == 'pubanon stage repair or pubanon stage forget'


def test_post_push_ref_read_failure_names_repair_and_preserves_records(
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

    def _staged_refs(*_args: object) -> _host.RemoteRefs:
        nonlocal reads
        reads += 1
        if reads == 1:
            return _host.RemoteRefs(old_stage.commit, {}, ())
        raise PubanonError(canary)

    def _successful_push(*_args: object) -> GitResult:
        return GitResult(0, b'', b'')

    monkeypatch.setattr(_stage, 'read_remote_refs', _staged_refs)
    monkeypatch.setattr(_stage, 'push', _successful_push)

    with pytest.raises(PubanonError, match='staging push read-back') as exc_info:
        _stage.stage_push(paths, global_config, project, yes=True)

    assert exc_info.value.recovery == 'pubanon stage repair'
    assert canary in exc_info.value.render()
    assert exc_info.value.fact == f'error: {canary}'
    assert _candidate.load_candidate(paths, project.name) == record
    assert _stage.load_stage(paths, project.name) == old_stage


def test_post_replace_sync_failure_reports_unconfirmed_recording(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    candidate = _build_candidate(paths, global_config, project)
    _save_stage(paths, project.name, _stage.StageRecord('a' * 40, project.owner, 'staging-repo'))
    _host_responses(fake_gh, project)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', host.as_uri())

    def _fail_sync(_directory: Path) -> None:
        raise OSError('directory sync failed')

    monkeypatch.setattr(_stage._filesystem, 'sync_directory', _fail_sync)

    with pytest.raises(PubanonError, match='cannot confirm staging push recording') as raised:
        _stage.stage_push(paths, global_config, project, yes=True)

    assert 'cannot confirm durable stage record save' in raised.value.render()
    assert 'not saved' not in raised.value.render()
    assert raised.value.recovery == 'pubanon stage repair'
    assert _git(host, 'rev-parse', 'refs/heads/main').decode().strip() == candidate.commit
    assert _stage.load_stage(paths, project.name) == _stage.StageRecord(
        candidate.commit, project.owner, 'staging-repo'
    )


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

    def _record_refs(*_args: object) -> _host.RemoteRefs:
        transport_calls.append('read_remote_refs')
        return _host.RemoteRefs(None, {}, ())

    def _record_push(*_args: object) -> GitResult:
        transport_calls.append('push')
        return GitResult(0, b'', b'')

    monkeypatch.setattr(_stage, 'read_remote_refs', _record_refs)
    monkeypatch.setattr(_stage, 'push', _record_push)

    with pytest.raises(PubanonError, match='configuration changed') as exc_info:
        _stage.stage_push(paths, global_config, changed_project, yes=True)

    assert exc_info.value.recovery == 'pubanon discard, then pubanon prepare'
    assert _calls(tmp_path) == []
    assert transport_calls == []
    assert _candidate.load_candidate(paths, project.name) == record


def test_publication_owner_drift_refuses_before_host_access(
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
    changed = replace(project, owner='changed-owner')

    with pytest.raises(PubanonError, match='configuration changed') as exc_info:
        _stage.stage_push(paths, global_config, changed, yes=True)

    assert exc_info.value.recovery == 'pubanon discard, then pubanon prepare'
    assert _calls(tmp_path) == []


def test_repair_unreadable_tracking_stops_before_host_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    global_config, project = _configs(tmp_path / 'source')
    path = _stage._stage_path(paths, project.name)
    path.mkdir(parents=True)

    def _unexpected(*_args: object, **_kwargs: object) -> Never:
        pytest.fail('repair ignored unreadable tracking')

    monkeypatch.setattr(_stage, 'require_authenticated_account', _unexpected)
    with pytest.raises(PubanonError, match='cannot read stage record'):
        _stage.stage_repair(paths, global_config, project, yes=True)
    assert path.is_dir()


def test_save_stage_backs_up_exact_malformed_bytes_before_replacement(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    stage_path = paths.data_dir / 'projects' / 'example' / 'stage.toml'
    stage_path.parent.mkdir(parents=True)
    stage_path.write_bytes(b'\xff\x00malformed')

    new = _stage.StageRecord('a' * 40, 'stage-owner', 'staging-repo')
    _save_stage(paths, 'example', new)

    assert _stage.load_stage(paths, 'example') == new
    assert_private_mode(stage_path, 0o600)
    backups = list(stage_path.parent.glob('stage.toml.backup-*'))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b'\xff\x00malformed'
    assert_private_mode(backups[0], 0o600)


def test_save_stage_backup_failure_preserves_malformed_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    stage_path = paths.data_dir / 'projects' / 'example' / 'stage.toml'
    stage_path.parent.mkdir(parents=True)
    stage_path.write_bytes(b'malformed')

    def _fail_backup(*_args: object, **_kwargs: object) -> Never:
        raise OSError('backup failed')

    monkeypatch.setattr(_stage.tempfile, 'mkstemp', _fail_backup)
    with pytest.raises(PubanonError, match='cannot back up malformed stage record'):
        _save_stage(paths, 'example', _stage.StageRecord('a' * 40, 'stage-owner', 'staging-repo'))
    assert stage_path.read_bytes() == b'malformed'


def test_save_stage_read_failure_does_not_create_backup_or_replace_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    stage_path = paths.data_dir / 'projects' / 'example' / 'stage.toml'
    stage_path.parent.mkdir(parents=True)
    stage_path.write_bytes(b'malformed')
    original_read = Path.read_bytes

    def _fail_read(path: Path) -> bytes:
        if path == stage_path:
            raise OSError('read failed')
        return original_read(path)

    monkeypatch.setattr(Path, 'read_bytes', _fail_read)
    with pytest.raises(PubanonError, match='cannot read existing stage record'):
        _save_stage(paths, 'example', _stage.StageRecord('a' * 40, 'stage-owner', 'staging-repo'))
    assert original_read(stage_path) == b'malformed'
    assert list(stage_path.parent.glob('stage.toml.backup-*')) == []


def test_save_stage_replacement_failure_preserves_previous_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    old = _stage.StageRecord('a' * 40, 'stage-owner', 'staging-repo')
    _save_stage(paths, 'example', old)
    stage_path = paths.data_dir / 'projects' / 'example' / 'stage.toml'
    previous = stage_path.read_bytes()

    def _fail_replace(*_args: object) -> Never:
        raise OSError('replacement failed')

    monkeypatch.setattr(_stage._filesystem, 'replace', _fail_replace)
    with pytest.raises(PubanonError, match='cannot save stage record'):
        _save_stage(paths, 'example', _stage.StageRecord('b' * 40, 'stage-owner', 'staging-repo'))
    assert stage_path.read_bytes() == previous
    assert list(stage_path.parent.glob('stage.toml.tmp*')) == []


def test_save_stage_temporary_sync_failure_preserves_previous_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    old = _stage.StageRecord('a' * 40, 'stage-owner', 'staging-repo')
    _save_stage(paths, 'example', old)
    stage_path = paths.data_dir / 'projects' / 'example' / 'stage.toml'
    previous = stage_path.read_bytes()

    def _fail_sync(_descriptor: int) -> Never:
        raise OSError('sync failed')

    monkeypatch.setattr(_stage.os, 'fsync', _fail_sync)
    with pytest.raises(PubanonError, match='cannot save stage record'):
        _save_stage(paths, 'example', _stage.StageRecord('b' * 40, 'stage-owner', 'staging-repo'))
    assert stage_path.read_bytes() == previous
    assert list(stage_path.parent.glob('stage.toml.tmp*')) == []


def test_save_stage_valid_replacement_makes_no_backup(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    old = _stage.StageRecord('a' * 40, 'stage-owner', 'staging-repo')
    new = _stage.StageRecord('b' * 40, 'stage-owner', 'staging-repo')
    _save_stage(paths, 'example', old)
    _save_stage(paths, 'example', new)
    stage_path = paths.data_dir / 'projects' / 'example' / 'stage.toml'
    assert _stage.load_stage(paths, 'example') == new
    assert list(stage_path.parent.glob('stage.toml.backup-*')) == []


@pytest.mark.parametrize('failure', ['update', 'readback', 'mismatch'])
def test_stage_create_settings_failure_stops_before_push(
    failure: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    project = replace(
        project,
        settings=Settings(
            'Staging description',
            'main',
            issues=False,
            wiki=False,
            projects=False,
            pull_requests=False,
            discussions=False,
        ),
    )
    candidate = _build_candidate(paths, global_config, project)
    _host_responses(fake_gh, project, absent=True)
    responses = json.loads(fake_gh.read_text())
    for response in responses:
        args = response['args']
        target = (
            'PATCH' in args
            if failure == 'update'
            else args[-1] == _host._REPOSITORY_SETTINGS_QUERY
        )
        if target:
            if failure == 'mismatch':
                facts = json.loads(response['stdout'])
                facts['pull_requests'] = True
                response['stdout'] = json.dumps(facts)
            else:
                response.update(returncode=1, stderr='settings transport failed\n')
    fake_gh.write_text(json.dumps(responses))
    monkeypatch.setattr(_host, '_URL_TEMPLATE', host.as_uri())

    def _approve(_prompt: str) -> str:
        return 'yes'

    monkeypatch.setattr('builtins.input', _approve)

    def _unexpected_push(*_args: object) -> Never:
        pytest.fail('settings failure reached staging push')

    monkeypatch.setattr(_stage, '_push_stage_main', _unexpected_push)
    with pytest.raises(PubanonError) as error:
        _stage.stage_push(paths, global_config, project, yes=True)

    assert 'repository was created' in str(error.value)
    assert 'GitHub settings' in (error.value.recovery or '')
    assert 'pubanon stage push' in (error.value.recovery or '')
    assert error.value.actual is None
    if failure == 'mismatch':
        assert 'settings do not match' in str(error.value)
        assert 'pull_requests: declared false, observed true' in (error.value.fact or '')
    else:
        assert 'could not be verified' in str(error.value)
        assert 'settings transport failed' in (error.value.fact or '')
        assert 'observed' not in (error.value.fact or '')
    assert _git(host, 'for-each-ref') == b''
    assert _candidate.load_candidate(paths, project.name) == candidate
    assert _stage.load_stage(paths, project.name) is None


@pytest.mark.parametrize('yes', [False, True])
def test_first_use_creation_decline_leaves_host_refs_empty(
    yes: bool,
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
    monkeypatch.setattr(_host, '_URL_TEMPLATE', host.as_uri())

    def _decline(_prompt: str) -> str:
        assert _git(host, 'for-each-ref') == b''
        return 'no'

    monkeypatch.setattr('builtins.input', _decline)

    with pytest.raises(OperationCanceled, match=r'Canceled\. Nothing was created.'):
        _stage.stage_push(paths, global_config, project, yes=yes)
    assert not any(call[2:4] == ['POST', 'user/repos'] for call in _calls(tmp_path))
    assert _git(host, 'for-each-ref') == b''


def test_stage_delete_authorization_failure_keeps_record_and_names_possible_cause(
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
        _stage.stage_delete(paths, global_config, project, yes=True)

    assert _stage.load_stage(paths, project.name) == stage
    assert exc_info.value.fact == 'HTTP 403: GitHub refused authorization'
    assert exc_info.value.recovery is not None
    assert 'missing delete_repo scope is one possible cause' in exc_info.value.recovery
    assert 'review the active account and repository permissions' in exc_info.value.recovery
    assert 'gh auth refresh' not in exc_info.value.render()
    assert not any(call[:2] == ['auth', 'refresh'] for call in _calls(tmp_path))


def test_stage_delete_does_not_gate_extra_branches_or_tags(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    assert project._stage_repo is not None
    _clean_responses(fake_gh, (project.owner, project._stage_repo))

    def _unexpected_refs(*_args: object, **_kwargs: object) -> Never:
        pytest.fail('explicit whole-repository deletion checked branch or tag inventory')

    monkeypatch.setattr(_stage, 'read_remote_refs', _unexpected_refs)
    _stage.stage_delete(paths, global_config, project, yes=True)
    assert any('DELETE' in call for call in _calls(tmp_path))


def test_stage_delete_failed_readback_preserves_tracking(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    record = _stage.StageRecord('a' * 40, project.owner, 'staging-repo')
    _save_stage(paths, project.name, record)
    reads = 0

    def _visible(_owner: str, _repo: str) -> bool:
        nonlocal reads
        reads += 1
        if reads == 1:
            return True
        raise PubanonError('read-back failed')

    def _require_account(_expected: str, *, authority: str) -> None:
        assert authority == 'staging configuration'

    def _verify_facts(
        _destination: _host.Destination, *, recovery: str, timeout: float | None = None
    ) -> None:
        assert recovery == 'inspect and delete the staging repository manually'
        assert timeout is None

    def _delete(_owner: str, _repo: str) -> None:
        pass

    monkeypatch.setattr(_stage, 'require_authenticated_account', _require_account)
    monkeypatch.setattr(_stage, 'repository_visible', _visible)
    monkeypatch.setattr(_stage, '_verify_stage_facts', _verify_facts)
    monkeypatch.setattr(_stage, 'delete_repository', _delete)

    with pytest.raises(PubanonError, match='cannot verify staging deletion') as exc_info:
        _stage.stage_delete(paths, global_config, project, yes=True)
    assert 'inspect the host' in (exc_info.value.recovery or '')
    assert _stage.load_stage(paths, project.name) == record


def test_stage_delete_malformed_record_keeps_bytes_when_delete_fails(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    assert project._stage_repo is not None
    stage_path = paths.data_dir / 'projects' / project.name / 'stage.toml'
    stage_path.parent.mkdir(parents=True)
    stage_path.write_bytes(b'\xffbad coordinate bytes')
    _clean_responses(fake_gh, (project.owner, project._stage_repo), outcome='scope-failure')

    with pytest.raises(PubanonError, match='not authorized'):
        _stage.stage_delete(paths, global_config, project, yes=True)
    assert stage_path.read_bytes() == b'\xffbad coordinate bytes'


def test_stage_delete_malformed_record_uses_verified_configured_destination(
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
    stage_path.parent.mkdir(parents=True)
    stage_path.write_bytes(b'\xffbad coordinate bytes')
    _clean_responses(fake_gh, (project.owner, project._stage_repo))

    def _confirm(_prompt: str) -> str:
        assert stage_path.read_bytes() == b'\xffbad coordinate bytes'
        return 'yes'

    monkeypatch.setattr('builtins.input', _confirm)
    _stage.stage_delete(paths, global_config, project, yes=False)

    assert not stage_path.exists()
    assert 'and its local tracking.' in capsys.readouterr().out
    assert ['api', '-X', 'DELETE', f'repos/{project.owner}/{project._stage_repo}'] in _calls(
        tmp_path
    )


def test_stage_delete_no_record_and_not_visible_preserves_host_uncertainty(
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
        pytest.fail('stage delete prompted without local evidence')

    monkeypatch.setattr('builtins.input', _unexpected_prompt)

    with pytest.raises(PubanonError, match='absent or inaccessible before deletion'):
        _stage.stage_delete(paths, global_config, project, yes=True)

    assert not stage_path.exists()
    assert not any('DELETE' in call for call in _calls(tmp_path))
    assert capsys.readouterr().out == ''


def test_stage_delete_no_record_previews_configured_fallback_before_deletion(
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
        assert 'Delete staging repository' in preview
        assert not any('DELETE' in call for call in _calls(tmp_path))
        return 'yes'

    monkeypatch.setattr('builtins.input', _approve)
    _stage.stage_delete(paths, global_config, project, yes=False)
    assert any('DELETE' in call for call in _calls(tmp_path))
    output = capsys.readouterr().out
    assert 'Deleted staging repository' in output
    assert 'and its local tracking.' in output
    assert 'harmless' not in output


def test_stage_delete_propagates_unrelated_pubanon_error_with_invalid_record_message(
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
        _stage.stage_delete(paths, global_config, project, yes=False)

    assert stage_path.read_bytes() == original_record


def test_stage_delete_refuses_configured_final_repository_alias_before_host_access(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    project = replace(project, _stage_repo=project.repo.upper())

    def _unexpected(*_args: object, **_kwargs: object) -> Never:
        pytest.fail('final repository alias reached the host')

    monkeypatch.setattr(_stage, 'require_authenticated_account', _unexpected)
    with pytest.raises(PubanonError, match='staging repository must differ'):
        _stage.stage_delete(paths, global_config, project, yes=True)


def test_stage_delete_routes_account_requirement_with_staging_authority(
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

    with pytest.raises(PubanonError, match='absent or inaccessible before deletion'):
        _stage.stage_delete(paths, global_config, project, yes=True)

    assert requirements == [('stage-owner', 'staging configuration')]


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

    with pytest.raises(
        PubanonError, match='repository name did not pass content checks'
    ) as exc_info:
        _stage._stage_destination(paths, global_config, project)

    assert exc_info.value.fact == f'repo: stage-repo-{term}'
    assert term in (exc_info.value.fact or '')
    assert _calls(tmp_path) == []


@pytest.mark.parametrize('existing', [False, True])
def test_stage_push_decline_keeps_created_repository_and_snapshot(
    existing: bool,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    candidate = _build_candidate(paths, global_config, project)
    _host_responses(fake_gh, project, absent=not existing)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', host.as_uri())
    prompts: list[str] = []

    def _confirm(question: str) -> bool:
        prompts.append(question)
        return question.startswith('Create ')

    monkeypatch.setattr(_stage, 'confirm', _confirm)

    with pytest.raises(OperationCanceled, match=r'Canceled\. Nothing was pushed') as error:
        _stage.stage_push(paths, global_config, project, yes=False)

    assert len(prompts) == (1 if existing else 2)
    assert error.value.recovery == 'pubanon stage push'
    if not existing:
        assert 'repository was created and is kept' in (error.value.fact or '')
    assert _git(host, 'for-each-ref') == b''
    assert _candidate.load_candidate(paths, project.name) == candidate
    assert _stage.load_stage(paths, project.name) is None


@pytest.mark.slow
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
    monkeypatch.setattr(_host, '_URL_TEMPLATE', host.as_uri())
    prompts: list[str] = []

    def _approve(prompt: str) -> str:
        prompts.append(prompt)
        return 'yes'

    monkeypatch.setattr('builtins.input', _approve)

    _stage.stage_push(paths, global_config, project, yes=True)

    calls = _calls(tmp_path)
    assert prompts == ['Create the private staging repository stage-owner/staging-repo? [y/n] ']
    assert calls[0] == ['api', 'user', '--jq', '.login']
    assert any(call[2:4] == ['POST', 'user/repos'] for call in calls)
    assert _git(host, 'rev-parse', 'refs/heads/main') == f'{record.commit}\n'.encode()
    output = capsys.readouterr().out
    lines = output.splitlines()
    assert lines[0]
    assert 'Stage prepared snapshot' not in lines
    assert (
        f'Staged snapshot ({record.commit[:12]}) in private repository stage-owner/staging-repo.\n'
        in output
    )
    assert 'Recorded staged commit:' not in output
    assert 'Observed remote commit:' not in output
    assert 'force-with-lease' not in output
    assert 'Next: pubanon publish\n' in output
    assert record.commit not in output


@pytest.mark.parametrize(
    ('field', 'error'),
    [
        ('owner', 'repository owner did not pass content checks'),
        ('name', 'repository name did not pass content checks'),
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
        ('hygiene', 'repository name did not pass content checks'),
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
    monkeypatch.setattr(_host, '_URL_TEMPLATE', host.as_uri())

    def _unexpected_push(*_args: object, **_kwargs: object) -> GitResult:
        pytest.fail('unverified staging facts reached push')

    monkeypatch.setattr(_stage, 'push', _unexpected_push)

    with pytest.raises(PubanonError, match='staging repository facts') as exc_info:
        _stage.stage_push(paths, global_config, project, yes=True)

    assert exc_info.value.recovery == 'pubanon stage repair'


@pytest.mark.slow
def test_replacement_push_uses_observed_lease_and_candidate_only_refspec(
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
            f'--force-with-lease=refs/heads/main:{first.commit}',
            '--no-follow-tags',
            f'{second.commit}:refs/heads/main',
        ]
    ]
    assert 'refs/tags' not in ' '.join(pushed[0])
    assert 'refs/heads/stage' not in ' '.join(pushed[0])


@pytest.mark.slow
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
    monkeypatch.setattr(_host, '_URL_TEMPLATE', host.as_uri())

    _stage.stage_push(paths, global_config, project, yes=True)

    saved = _stage.load_stage(paths, project.name)
    assert saved is not None
    assert saved.commit == candidate.tip
    assert _git(host, 'rev-parse', 'refs/heads/main').strip().decode() == candidate.tip


@pytest.mark.slow
def test_stage_delete_declined_confirmation_keeps_repository_and_record(
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

    with pytest.raises(OperationCanceled, match=r'Canceled\. Nothing was deleted.'):
        _stage.stage_delete(paths, global_config, project, yes=False)

    assert _stage.load_stage(paths, project.name) == stage
    assert not any('DELETE' in call for call in _calls(tmp_path))


@pytest.mark.slow
def test_stage_delete_deletes_private_repository_verifies_absence_and_clears_record(
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

    _stage.stage_delete(paths, global_config, project, yes=True)

    assert _stage.load_stage(paths, project.name) is None
    calls = _calls(tmp_path)
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
    assert 'Deleted staging repository' in output
    assert 'and its local tracking.' in output
    assert 'The source and final repositories were not changed.\n' in output
    assert 'harmless' not in output
    assert 'logs' not in output
    assert 'artifacts' not in output
    assert 'Next:' not in output


@pytest.mark.slow
def test_stage_delete_incomplete_deletion_keeps_record_with_rerun_recovery(
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
        _stage.stage_delete(paths, global_config, project, yes=True)

    assert exc_info.value.recovery == 'inspect the host, then retry pubanon stage delete'
    assert _stage.load_stage(paths, project.name) == stage


@pytest.mark.slow
def test_stage_delete_refuses_public_repository_without_deleting(
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
        _stage.stage_delete(paths, global_config, project, yes=True)

    assert exc_info.value.recovery == 'inspect and delete the staging repository manually'
    assert _stage.load_stage(paths, project.name) == stage
    assert not any('DELETE' in call for call in _calls(tmp_path))


@pytest.mark.slow
def test_stage_delete_yes_skips_confirmation(
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

    _stage.stage_delete(paths, global_config, project, yes=True)

    assert any('DELETE' in call for call in _calls(tmp_path))


@pytest.mark.parametrize(
    'content',
    [
        None,
        b'malformed',
        b'commit = "' + b'a' * 40 + b'"\nowner = "stage-owner"\nrepo = "staging-repo"\n',
    ],
)
def test_stage_forget_only_removes_local_tracking(
    content: bytes | None,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    project = replace(project, publication_route='staged')
    candidate = _build_candidate(paths, global_config, project)
    git_objects_before = _git(source, 'count-objects', '-v')
    config_before = (global_config, project)
    stage_path = paths.data_dir / 'projects' / project.name / 'stage.toml'
    if content is not None:
        stage_path.parent.mkdir(parents=True, exist_ok=True)
        stage_path.write_bytes(content)
    sentinel = tmp_path / 'sentinel'
    sentinel.write_bytes(b'unchanged')

    def _unexpected(*_args: object, **_kwargs: object) -> Never:
        pytest.fail('forget accessed the host')

    monkeypatch.setattr(_stage, 'require_authenticated_account', _unexpected)
    monkeypatch.setattr(_stage, 'repository_visible', _unexpected)
    monkeypatch.setattr(_stage, 'delete_repository', _unexpected)
    _stage.stage_forget(paths, global_config, project, yes=True)

    assert not stage_path.exists()
    assert sentinel.read_bytes() == b'unchanged'
    assert _candidate.load_candidate(paths, project.name) == candidate
    assert _git(source, 'count-objects', '-v') == git_objects_before
    assert (global_config, project) == config_before
    assert _calls(tmp_path) == []
    assert _stage.candidate_next_action(paths, global_config, project, candidate) == (
        'pubanon stage push'
    )
    if content is not None:
        output = capsys.readouterr().out
        assert 'Forgetting tracking keeps the selected publication route.' in output
        assert 'pubanon init --publication-route direct' in output


@pytest.mark.parametrize('answer', ['no', 'yes'])
def test_stage_forget_confirmation_controls_tracking_removal(
    answer: str,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    record = _stage.StageRecord('a' * 40, project.owner, 'staging-repo')
    _save_stage(paths, project.name, record)

    def _respond(_prompt: str) -> str:
        return answer

    monkeypatch.setattr('builtins.input', _respond)

    if answer == 'no':
        with pytest.raises(OperationCanceled, match=r'Canceled\. Nothing was removed.'):
            _stage.stage_forget(paths, global_config, project, yes=False)
        assert _stage.load_stage(paths, project.name) == record
    else:
        _stage.stage_forget(paths, global_config, project, yes=False)
        assert _stage.load_stage(paths, project.name) is None
    assert _calls(tmp_path) == []


@pytest.mark.parametrize('yes', [False, True], ids=['interactive', 'yes'])
@pytest.mark.parametrize('case_only', [False, True], ids=['exact', 'case-only'])
def test_stage_delete_refuses_recorded_final_destination_before_host_access(
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
        match='recorded staging coordinates differ from configured destination',
    ) as exc_info:
        _stage.stage_delete(paths, global_config, changed, yes=yes)

    assert exc_info.value.recovery == (
        'inspect the recorded target; use pubanon stage forget to remove tracking'
    )
    assert stage_path.read_bytes() == original_record


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
    monkeypatch.setattr(_stage, 'read_remote_refs', _unexpected_host)
    monkeypatch.setattr(_stage, 'push', _unexpected_host)

    with pytest.raises(
        PubanonError, match='the snapshot did not pass current content checks'
    ) as exc_info:
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


def test_stage_record_failure_names_repair_and_preserves_records(
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
    reads = iter(
        [_host.RemoteRefs(old_stage.commit, {}, ()), _host.RemoteRefs(record.commit, {}, ())]
    )

    def _staged_refs(*_args: object) -> _host.RemoteRefs:
        return next(reads)

    def _fail_save(*_args: object) -> None:
        raise PubanonError(canary)

    def _successful_push(*_args: object) -> GitResult:
        return GitResult(0, b'', b'')

    monkeypatch.setattr(_stage, 'read_remote_refs', _staged_refs)
    monkeypatch.setattr(_stage, 'push', _successful_push)
    monkeypatch.setattr(_stage, '_save_stage', _fail_save)

    with pytest.raises(PubanonError, match='cannot confirm staging push recording') as exc_info:
        _stage.stage_push(paths, global_config, project, yes=True)

    assert exc_info.value.recovery == 'pubanon stage repair'
    assert canary in exc_info.value.render()
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

    with pytest.raises(
        PubanonError, match='repository name did not pass content checks'
    ) as exc_info:
        _stage.stage_status(paths, global_config, project)

    assert term in exc_info.value.render()
    assert _calls(tmp_path) == []


@pytest.mark.slow
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
    _candidate.save_candidate(
        paths,
        project.name,
        replace(record, fingerprint=_candidate.fingerprint(global_config, project)),
    )
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

    rendered = output.getvalue()
    assert 'Checks:' in rendered
    staged_commit = f'Staged: snapshot ({record.commit[:12]})'
    build_result = 'Build check passed'
    tests_result = 'Tests passed'
    next_action = 'Next: pubanon publish'
    assert rendered.rindex(staged_commit) < rendered.index(build_result)
    assert rendered.index(build_result) < rendered.index(tests_result)
    assert rendered.index(tests_result) < rendered.index(next_action)
    assert rendered.count(record.commit[:12]) == 1
    check_calls = [
        call[1] for call in _calls(tmp_path) if call[:1] == ['api'] and '/check-runs?' in call[1]
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
    assert all('refs/heads/main' not in path for path in check_calls)


@pytest.mark.slow
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
    display = {
        'pending': 'pending',
        'missing': 'not found',
        'ambiguous': 'could not be determined',
        'unacceptable': 'failed',
    }[reported_state]
    assert f'CI: {display}' in rendered
    if host_state not in {'pending', 'missing', 'ambiguous'}:
        assert (
            'canceled'
            if host_state == 'cancelled'
            else 'failed'
            if host_state == 'failure'
            else host_state
        ) in rendered
    assert exc_info.value.fact == f'CI:{reported_state}'
    assert exc_info.value.recovery == recovery
    assert _stage.load_stage(paths, project.name) == stage_before
    assert not any('-X' in call for call in _calls(tmp_path))


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


@pytest.mark.slow
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

    rendered = output.getvalue()
    no_checks = 'No required checks are configured'
    staged_commit = f'Staged: snapshot ({record.commit[:12]})'
    next_action = 'Next: pubanon publish'
    assert rendered.index(staged_commit) < rendered.rindex(no_checks)
    assert rendered.rindex(no_checks) < rendered.rindex(next_action)
    assert not any('/check-runs?' in ' '.join(call) for call in _calls(tmp_path))


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
        f'{record.commit}:refs/heads/other',
    )
    _host_responses(fake_gh, project)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', host.as_uri())

    def _unexpected_push(*_args: object, **_kwargs: object) -> GitResult:
        pytest.fail('unexpected staging ref reached push')

    monkeypatch.setattr(_stage, 'push', _unexpected_push)

    with pytest.raises(PubanonError, match='unexpected staging reference') as exc_info:
        _stage.stage_push(paths, global_config, project, yes=True)

    assert 'Git or GitHub' in (exc_info.value.recovery or '')


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
        _stage._verify_stage_facts(destination, recovery='pubanon stage repair')

    assert str(exc_info.value) == 'Cannot access GitHub repository publication-owner/stage-repo'
    assert exc_info.value.recovery == 'pubanon stage repair'


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
        assert (selected, recovery) == (destination, 'pubanon stage repair')
        calls.append(('repository', timeout))

    def _refs(
        selected: _host.Destination,
        *,
        timeout: float | None = None,
    ) -> _host.RemoteRefs:
        assert selected == destination
        calls.append(('refs', timeout))
        return _host.RemoteRefs(record.commit, {}, ())

    monkeypatch.setattr(_stage, 'require_authenticated_account', _require_account)
    monkeypatch.setattr(_stage, '_verify_stage_facts', _verify_facts)
    monkeypatch.setattr(_stage, 'read_remote_refs', _refs)

    def _settings(owner: str, repo: str, *, timeout: float | None = None) -> _host.SettingsFacts:
        assert (owner, repo) == (destination.owner, destination.repo)
        calls.append(('settings', timeout))
        return _host.SettingsFacts(
            '',
            'main',
            issues=True,
            wiki=False,
            projects=False,
            pull_requests=False,
            discussions=False,
        )

    monkeypatch.setattr(_stage, 'repository_settings', _settings)
    times = iter([0.0, 0.0, 1.0, 2.0, 3.0])
    monkeypatch.setattr(_stage, '_monotonic', lambda: next(times))

    assert _stage.verify_staged_target(paths, global_config, project, timeout=8.0) == (
        destination,
        record,
    )
    assert calls == [('account', 8.0), ('repository', 7.0), ('refs', 6.0), ('settings', 5.0)]


@pytest.mark.slow
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

    assert exc_info.value.recovery == 'pubanon stage repair'
    assert _calls(tmp_path) == []


@pytest.mark.slow
@pytest.mark.parametrize('tracking', ['absent', 'stale', 'malformed', 'other-destination'])
def test_push_reconnects_tracking_using_observed_main_without_adopting_unknown_tip(
    tracking: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    candidate = _build_candidate(paths, global_config, project)
    unknown = _git(source, 'rev-parse', 'HEAD').decode().strip()
    _git(host, 'fetch', '-q', source.as_posix(), f'{unknown}:refs/heads/main')
    record_path = paths.data_dir / 'projects' / project.name / 'stage.toml'
    old = _stage.StageRecord(
        'a' * 40,
        project.owner,
        'old-staging-repo' if tracking == 'other-destination' else 'staging-repo',
    )
    if tracking != 'absent':
        _save_stage(paths, project.name, old)
    if tracking == 'malformed':
        record_path.write_bytes(b'broken\xff record')
    _host_responses(fake_gh, project)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', host.as_uri())
    before_source = _git(source, 'for-each-ref')
    before_local = _git(_candidate.store_path(paths, project.name), 'for-each-ref')

    capture = StringIO()
    with redirect_stdout(capture):
        _stage.stage_push(paths, global_config, project, yes=True)

    assert _git(host, 'rev-parse', 'refs/heads/main').decode().strip() == candidate.commit
    assert _stage.load_stage(paths, project.name) == _stage.StageRecord(
        candidate.commit, project.owner, 'staging-repo'
    )
    assert _git(source, 'for-each-ref') == before_source
    assert _git(_candidate.store_path(paths, project.name), 'for-each-ref') == before_local
    objects = (
        _git(
            _candidate.store_path(paths, project.name),
            'cat-file',
            '--batch-all-objects',
            '--batch-check=%(objectname)',
        )
        .decode()
        .splitlines()
    )
    assert unknown not in objects
    rendered = capture.getvalue()
    assert unknown not in rendered
    assert candidate.commit[:12] in rendered
    assert 'stage-owner/staging-repo' in rendered
    if tracking == 'malformed':
        assert [f.read_bytes() for f in record_path.parent.glob('stage.toml.backup-*')] == [
            b'broken\xff record'
        ]
    assert not any(
        'POST' in call or 'DELETE' in call or 'PATCH' in call for call in _calls(tmp_path)
    )


@pytest.mark.parametrize('empty', [False, True])
def test_observed_main_lease_preserves_concurrent_remote_change(
    empty: bool,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    candidate = _build_candidate(paths, global_config, project)
    if not empty:
        _git(
            host,
            'fetch',
            '-q',
            _candidate.store_path(paths, project.name).as_posix(),
            f'{candidate.commit}:refs/heads/main',
        )
    concurrent = _git(source, 'rev-parse', 'HEAD').decode().strip()
    old = _stage.StageRecord('a' * 40, project.owner, 'staging-repo')
    _save_stage(paths, project.name, old)
    _host_responses(fake_gh, project)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', host.as_uri())
    actual_push = _stage.push

    def _race(dest: _host.Destination, cwd: Path, args: Sequence[str]) -> GitResult:
        expected = '' if empty else candidate.commit
        assert f'--force-with-lease=refs/heads/main:{expected}' in args
        _git(host, 'fetch', '-q', source.as_posix(), f'+{concurrent}:refs/heads/main')
        return actual_push(dest, cwd, args)

    monkeypatch.setattr(_stage, 'push', _race)

    with pytest.raises(PubanonError, match='staging push failed') as raised:
        _stage.stage_push(paths, global_config, project, yes=True)

    assert 'stale info' in raised.value.render()
    assert 'inspect' in (raised.value.recovery or '')
    assert _git(host, 'rev-parse', 'refs/heads/main').decode().strip() == concurrent
    assert _stage.load_stage(paths, project.name) == old
    assert _candidate.load_candidate(paths, project.name) == candidate


@pytest.mark.slow
@pytest.mark.parametrize('absent', [False, True])
def test_empty_stage_selects_default_after_main_exists_and_preserves_creation_settings(
    absent: bool,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    settings = Settings(
        'Staging description',
        'main',
        issues=False,
        wiki=True,
        projects=False,
        pull_requests=False,
        discussions=False,
    )
    project = replace(project, settings=settings)
    candidate = _build_candidate(paths, global_config, project)
    _host_responses(fake_gh, project, absent=absent)
    responses = json.loads(fake_gh.read_text())
    settings_args = [
        'api',
        'repos/stage-owner/staging-repo',
        '--jq',
        _host._REPOSITORY_SETTINGS_QUERY,
    ]
    responses.insert(
        0,
        {
            'args': settings_args,
            'once': True,
            'stdout': json.dumps(
                {
                    'description': settings.description,
                    'default_branch': 'master',
                    'issues': settings.issues,
                    'wiki': settings.wiki,
                    'projects': settings.projects,
                    'pull_requests': settings.pull_requests,
                    'discussions': settings.discussions,
                }
            ),
        },
    )
    if absent:
        responses.insert(0, responses[0].copy())
    responses.append(
        {
            'args': [
                'api',
                '-X',
                'PATCH',
                'repos/stage-owner/staging-repo',
                '-f',
                'default_branch=main',
            ]
        }
    )
    fake_gh.write_text(json.dumps(responses))
    monkeypatch.setattr(_host, '_URL_TEMPLATE', host.as_uri())

    def _approve(_prompt: str) -> str:
        return 'yes'

    monkeypatch.setattr('builtins.input', _approve)
    set_default = _stage.set_default_branch

    def _select_default(owner: str, repo: str, branch: str) -> None:
        assert _git(host, 'for-each-ref', '--format=%(refname)') == b'refs/heads/main\n'
        assert _git(host, 'rev-parse', 'refs/heads/main').decode().strip() == candidate.commit
        assert _stage.load_stage(paths, project.name) is None
        set_default(owner, repo, branch)

    monkeypatch.setattr(_stage, 'set_default_branch', _select_default)

    capture = StringIO()
    with redirect_stdout(capture):
        _stage.stage_push(paths, global_config, project, yes=True)
    assert 'Default branch change: master -> main after push' in capture.getvalue()
    assert _stage.load_stage(paths, project.name) == _stage.StageRecord(
        candidate.commit, project.owner, 'staging-repo'
    )
    first_calls = _calls(tmp_path)
    creations = [c for c in first_calls if c[2:4] == ['POST', 'user/repos']]
    assert len(creations) == int(absent)
    if creations:
        assert 'private=true' in creations[0]
        assert 'description=Staging description' in creations[0]
    assert len([c for c in first_calls if 'PATCH' in c]) == 1 + int(absent)

    _stage.stage_push(paths, global_config, project, yes=True)
    later_calls = _calls(tmp_path)[len(first_calls) :]
    assert not any(any(method in c for method in ('POST', 'DELETE', 'PATCH')) for c in later_calls)


@pytest.mark.parametrize('layout', ['legacy', 'both', 'extra-branch', 'tag', 'wrong-default'])
def test_push_refuses_unsupported_layout_without_mutation(
    layout: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    candidate = _build_candidate(paths, global_config, project)
    refs = ['refs/heads/stage'] if layout == 'legacy' else ['refs/heads/main']
    if layout == 'both':
        refs.append('refs/heads/stage')
    elif layout == 'extra-branch':
        refs.append('refs/heads/other')
    elif layout == 'tag':
        refs.append('refs/tags/v1')
    for ref in refs:
        _git(
            host,
            'fetch',
            '-q',
            _candidate.store_path(paths, project.name).as_posix(),
            f'{candidate.commit}:{ref}',
        )
    before_refs = _git(host, 'for-each-ref')
    _host_responses(fake_gh, project)
    if layout == 'wrong-default':
        fake_gh.write_text(fake_gh.read_text().replace('\\"main\\"', '\\"master\\"'))
    monkeypatch.setattr(_host, '_URL_TEMPLATE', host.as_uri())

    with pytest.raises(PubanonError) as raised:
        _stage.stage_push(paths, global_config, project, yes=True)

    if layout == 'wrong-default':
        assert raised.value.recovery == 'pubanon stage repair'
    elif layout == 'legacy':
        assert 'rename stage to main through GitHub' in (raised.value.recovery or '')
    else:
        assert 'Git or GitHub' in (raised.value.recovery or '')
    assert _git(host, 'for-each-ref') == before_refs
    assert _stage.load_stage(paths, project.name) is None
    assert not any(
        any(method in c for method in ('POST', 'DELETE', 'PATCH')) for c in _calls(tmp_path)
    )


@pytest.mark.parametrize('failure', ['default-update', 'default-readback', 'settings-readback'])
def test_default_failure_after_successful_push_preserves_tracking_and_candidate(
    failure: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    candidate = _build_candidate(paths, global_config, project)
    old = _stage.StageRecord('a' * 40, project.owner, 'staging-repo')
    _save_stage(paths, project.name, old)
    _host_responses(fake_gh, project)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', host.as_uri())
    reads = 0

    def _settings(_owner: str, _repo: str) -> _host.SettingsFacts:
        nonlocal reads
        reads += 1
        if reads > 1 and failure == 'settings-readback':
            raise PubanonError('settings unavailable')
        return _host.SettingsFacts(
            '',
            'master',
            issues=False,
            wiki=False,
            projects=False,
            pull_requests=False,
            discussions=False,
        )

    def _default(_owner: str, _repo: str, _branch: str) -> None:
        if failure == 'default-update':
            raise PubanonError('default update failed')

    monkeypatch.setattr(_stage, 'repository_settings', _settings)
    monkeypatch.setattr(_stage, 'set_default_branch', _default)

    with pytest.raises(PubanonError, match='remote update succeeded') as raised:
        _stage.stage_push(paths, global_config, project, yes=True)

    assert raised.value.recovery == 'pubanon stage repair'
    assert _git(host, 'rev-parse', 'refs/heads/main').decode().strip() == candidate.commit
    assert _stage.load_stage(paths, project.name) == old
    assert _candidate.load_candidate(paths, project.name) == candidate


@pytest.mark.slow
@pytest.mark.parametrize('operation', ['push', 'readback', 'save'])
def test_cancelled_push_or_readback_preserves_cancellation_and_tracking(
    operation: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_path, paths, global_config, project, _candidate_record = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    old = _stage.load_stage(paths, project.name)
    reads = 0
    actual_refs = _stage.read_remote_refs

    def _cancel(*_args: object, **_kwargs: object) -> Never:
        raise OperationCanceled('operator interrupted staging')

    def _refs(dest: _host.Destination) -> _host.RemoteRefs:
        nonlocal reads
        reads += 1
        if reads > 1:
            _cancel()
        return actual_refs(dest)

    if operation == 'push':
        monkeypatch.setattr(_stage, 'push', _cancel)
    elif operation == 'save':
        monkeypatch.setattr(_stage, '_save_stage', _cancel)
    else:
        monkeypatch.setattr(_stage, 'read_remote_refs', _refs)

    with pytest.raises(OperationCanceled, match='operator interrupted staging'):
        _stage.stage_push(paths, global_config, project, yes=True)

    assert _stage.load_stage(paths, project.name) == old


@pytest.mark.slow
@pytest.mark.parametrize('tracking', ['current', 'missing', 'stale', 'malformed', 'other-target'])
def test_repair_reconnects_recognized_main_without_remote_mutation(
    tracking: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, paths, global_config, project, candidate = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    path = _stage._stage_path(paths, project.name)
    if tracking == 'missing':
        path.unlink()
    elif tracking == 'stale':
        _save_stage(
            paths, project.name, _stage.StageRecord('a' * 40, project.owner, 'staging-repo')
        )
    elif tracking == 'malformed':
        path.write_bytes(b'broken\xff tracking')
    elif tracking == 'other-target':
        _save_stage(
            paths, project.name, _stage.StageRecord(candidate.commit, project.owner, 'old-stage')
        )
    before = path.read_bytes() if path.exists() else None
    writes: list[_stage.StageRecord] = []
    original_save = _stage._save_stage

    def _save(actual_paths: Paths, name: str, record: _stage.StageRecord) -> None:
        writes.append(record)
        original_save(actual_paths, name, record)

    def _forbidden(*_args: object, **_kwargs: object) -> Never:
        pytest.fail('local repair mutated the remote')

    monkeypatch.setattr(_stage, '_save_stage', _save)
    monkeypatch.setattr(_stage, 'push', _forbidden)
    monkeypatch.setattr(_stage, 'set_default_branch', _forbidden)
    monkeypatch.setattr(_stage, 'delete_repository', _forbidden)
    rendered = StringIO()
    with redirect_stdout(rendered):
        _stage.stage_repair(paths, global_config, project, yes=True)
    assert _stage.load_stage(paths, project.name) == _stage.StageRecord(
        candidate.commit, project.owner, 'staging-repo'
    )
    assert len(writes) == (0 if tracking == 'current' else 1)
    assert _git(host, 'for-each-ref', '--format=%(refname)') == b'refs/heads/main\n'
    if tracking == 'malformed':
        backups = list(path.parent.glob('stage.toml.backup-*'))
        assert len(backups) == 1
        assert backups[0].read_bytes() == before
        assert_private_mode(backups[0], 0o600)
    if tracking == 'other-target':
        assert 'old-stage' in rendered.getvalue()
        assert all('old-stage' not in ' '.join(call) for call in _calls(tmp_path))


@pytest.mark.slow
@pytest.mark.parametrize(
    'evidence', ['record', 'publication', 'object-only', 'missing-archive', 'conflicting-archive']
)
def test_repair_recognizes_independent_retained_evidence_without_new_candidate_gates(
    evidence: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, paths, global_config, project, candidate = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    path = _stage._stage_path(paths, project.name)
    if evidence != 'record':
        path.unlink()
    store = _candidate.candidate_store_path(paths, project.name)
    if evidence in {'publication', 'missing-archive', 'conflicting-archive'}:
        publication = _candidate.publication_record(
            paths, project.name, candidate, candidate.commit, datetime.now(UTC)
        )
        publication = replace(
            publication,
            source_bookmark=describe_bookmark(project.source_path, project.name, candidate.commit),
        )
        _records.save_publication(paths, project.name, publication)
        if evidence != 'missing-archive':
            target = candidate.commit if evidence == 'publication' else candidate.pinned.commit
            if evidence == 'conflicting-archive':
                _git(store, 'fetch', str(project.source_path), candidate.pinned.commit)
            _git(store, 'update-ref', _records.publication_ref(candidate.commit), target)
    _candidate.record_path(paths, project.name).unlink()
    if evidence == 'record':
        (project.source_path / 'README.md').write_text('new snapshot\n')
        _git(project.source_path, 'commit', '-am', 'change')
        _build_candidate(paths, global_config, project)
    changed = replace(
        global_config, sensitive_terms=(*global_config.sensitive_terms, 'new snapshot')
    )
    before = path.read_bytes() if path.exists() else None
    if evidence in {'record', 'publication'}:
        _stage.stage_repair(paths, changed, project, yes=True)
        assert _stage.load_stage(paths, project.name) == _stage.StageRecord(
            candidate.commit, project.owner, 'staging-repo'
        )
    else:
        with pytest.raises(PubanonError):
            _stage.stage_repair(paths, changed, project, yes=True)
        assert (path.read_bytes() if path.exists() else None) == before
    assert _git(host, 'rev-parse', 'main').strip().decode() == candidate.commit


@pytest.mark.slow
@pytest.mark.parametrize(
    'state',
    [
        'unknown',
        'empty',
        'legacy',
        'extra-branch',
        'tag',
        'both',
        'absent',
        'inaccessible',
        'read-failure',
    ],
)
def test_repair_unresolved_observations_preserve_tracking(
    state: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, paths, global_config, project, candidate = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    path = _stage._stage_path(paths, project.name)
    path.write_bytes(b'prior malformed evidence')
    if state == 'unknown':
        _candidate.record_path(paths, project.name).unlink()
    elif state == 'legacy':
        _git(host, 'branch', '-m', 'main', 'stage')
    elif state == 'empty':
        _git(host, 'update-ref', '-d', 'refs/heads/main')
    elif state in {'extra-branch', 'tag', 'both'}:
        ref = {
            'extra-branch': 'refs/heads/extra',
            'tag': 'refs/tags/v1',
            'both': 'refs/heads/stage',
        }[state]
        _git(host, 'update-ref', ref, candidate.commit)
    elif state in {'absent', 'inaccessible'}:
        responses = json.loads(fake_gh.read_text())
        for response in responses:
            if response['args'][-1] == _host._REPOSITORY_QUERY:
                response.update(
                    stdout=json.dumps({'status': '404' if state == 'absent' else '403'}),
                    returncode=1,
                )
        fake_gh.write_text(json.dumps(responses))
    elif state == 'read-failure':
        monkeypatch.setattr(_stage, 'read_remote_refs', _fail_stage_refs_read)
    refs = _git(host, 'show-ref') if state != 'empty' else b''
    with pytest.raises(PubanonError) as exc_info:
        _stage.stage_repair(paths, global_config, project, yes=True)
    assert path.read_bytes() == b'prior malformed evidence'
    assert (_git(host, 'show-ref') if state != 'empty' else b'') == refs
    assert exc_info.value.recovery
    if state in {'unknown', 'empty'}:
        assert 'stage push' in exc_info.value.recovery
        assert 'stage forget' not in exc_info.value.recovery
    assert not any('-X' in call for call in _calls(tmp_path))


@pytest.mark.slow
@pytest.mark.parametrize(
    'scenario',
    [
        ('lost-response', False),
        ('local-save', False),
        ('lost-response', True),
        ('local-save', True),
    ],
)
def test_repair_recovers_successful_push_without_repeating_it(
    scenario: tuple[str, bool],
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    failure, replacement = scenario
    if replacement:
        paths, global_config, project, candidate = _complete_replacement(tmp_path, stage=True)
    else:
        source = make_repo(files={'README.md': 'first\n'})
        paths = _paths(tmp_path)
        global_config, project = _configs(source)
        candidate = _build_candidate(paths, global_config, project)
    host = _new_host(tmp_path)
    _host_responses(fake_gh, project)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', host.as_uri())
    real_push = _stage.push
    pushes = 0

    def _push(dest: _host.Destination, store: Path, refspec: Sequence[str]) -> GitResult:
        nonlocal pushes
        pushes += 1
        result = real_push(dest, store, refspec)
        if failure == 'lost-response':
            raise PubanonError('push response lost')
        return result

    def _fail_save(*_args: object) -> Never:
        raise PubanonError('disk full')

    monkeypatch.setattr(_stage, 'push', _push)
    with monkeypatch.context() as fault:
        if failure == 'local-save':
            fault.setattr(_stage, '_save_stage', _fail_save)
        with pytest.raises(PubanonError):
            _stage.stage_push(paths, global_config, project, yes=True)
    _stage.stage_repair(paths, global_config, project, yes=True)
    assert pushes == 1
    assert _stage.load_stage(paths, project.name) == _stage.StageRecord(
        _records.candidate_tip(candidate), project.owner, project._stage_repo or ''
    )
    if replacement:
        assert candidate.tip != candidate.commit


@pytest.mark.slow
@pytest.mark.parametrize('yes', [False, True])
def test_repair_confirmation_covers_local_reconnection(
    yes: bool,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_path, paths, global_config, project, _candidate_record = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    path = _stage._stage_path(paths, project.name)
    path.write_bytes(b'broken tracking')
    confirmations: list[str] = []

    def _decline(prompt: str) -> bool:
        confirmations.append(prompt)
        return False

    monkeypatch.setattr(_stage, 'confirm', _decline)
    if yes:
        _stage.stage_repair(paths, global_config, project, yes=True)
        assert not confirmations
    else:
        with pytest.raises(OperationCanceled, match=r'Canceled\. Nothing was changed.'):
            _stage.stage_repair(paths, global_config, project, yes=False)
        assert len(confirmations) == 1
        assert path.read_bytes() == b'broken tracking'
        assert not list(path.parent.glob('stage.toml.backup-*'))


def _default_repair_responses(
    fake_gh: Path, project: ProjectConfig, *, outcome: str = 'success'
) -> list[str]:
    _host_responses(fake_gh, project)
    responses = json.loads(fake_gh.read_text())
    settings = next(
        response
        for response in responses
        if response['args'][-1] == _host._REPOSITORY_SETTINGS_QUERY
    )
    prior = json.loads(settings['stdout'])
    prior['default_branch'] = 'other'
    responses.insert(0, {**settings, 'stdout': json.dumps(prior), 'once': True})
    if outcome == 'wrong-default':
        settings['stdout'] = json.dumps(prior)
    elif outcome == 'read-error':
        settings['stdout'] = 'unreadable settings'
    if outcome == 'public':
        first = next(
            response for response in responses if response['args'][-1] == _host._REPOSITORY_QUERY
        )
        responses.insert(0, {**first, 'once': True})
        for response in responses[1:]:
            if response['args'][-1] == _host._REPOSITORY_QUERY:
                facts = json.loads(response['stdout'])
                facts['visibility'] = 'public'
                response['stdout'] = json.dumps(facts)
    args = [
        'api',
        '-X',
        'PATCH',
        f'repos/{project.owner}/{project._stage_repo}',
        '-f',
        'default_branch=main',
    ]
    responses.append(
        {
            'args': args,
            'returncode': 1 if outcome == 'permission' else 0,
            'stderr': '403 permission denied' if outcome == 'permission' else '',
        }
    )
    fake_gh.write_text(json.dumps(responses))
    return args


@pytest.mark.slow
@pytest.mark.parametrize('scenario', [(False, False), (False, True), (True, False), (True, True)])
def test_repair_sets_main_default_once_after_one_concrete_confirmation(
    scenario: tuple[bool, bool],
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    yes, current = scenario
    host, paths, global_config, project, candidate = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    if not current:
        _clear_stage(paths, project.name)
    patch_args = _default_repair_responses(fake_gh, project)
    original_save = _stage._save_stage
    writes: list[_stage.StageRecord] = []
    prompts: list[str] = []
    before_refs = _git(host, 'show-ref')
    before_source = _git(project.source_path, 'show-ref')
    output = StringIO()

    def _confirm(prompt: str) -> bool:
        assert 'Default branch change: other -> main' in output.getvalue()
        prompts.append(prompt)
        return True

    def _save(actual: Paths, name: str, record: _stage.StageRecord) -> None:
        writes.append(record)
        original_save(actual, name, record)

    monkeypatch.setattr(_stage, 'confirm', _confirm)
    monkeypatch.setattr(_stage, '_save_stage', _save)
    with redirect_stdout(output):
        _stage.stage_repair(paths, global_config, project, yes=yes)
    assert len(prompts) == (0 if yes else 1)
    assert len(writes) == (0 if current else 1)
    assert _calls(tmp_path).count(patch_args) == 1
    assert _stage.load_stage(paths, project.name) == _stage.StageRecord(
        candidate.commit, project.owner, 'staging-repo'
    )
    assert _git(host, 'show-ref') == before_refs
    assert _git(project.source_path, 'show-ref') == before_source


@pytest.mark.slow
@pytest.mark.parametrize(
    'outcome',
    [
        'permission',
        'wrong-default',
        'read-error',
        'public',
        'moved-tip',
        'extra-ref',
        'cancelled',
        'response-lost',
        'save-failure',
    ],
)
def test_repair_default_failure_preserves_tracking_and_fresh_retry_observes_main(
    outcome: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, paths, global_config, project, candidate = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    path = _stage._stage_path(paths, project.name)
    path.write_bytes(b'prior tracking evidence')
    patch_args = _default_repair_responses(fake_gh, project, outcome=outcome)
    original_default = _stage.set_default_branch

    def _default(owner: str, repo: str, branch: str) -> None:
        original_default(owner, repo, branch)
        if outcome == 'cancelled':
            raise OperationCanceled('default repair interrupted')
        if outcome == 'response-lost':
            raise PubanonError('default response lost')
        if outcome == 'moved-tip':
            _git(
                host,
                'fetch',
                str(project.source_path),
                f'{candidate.pinned.commit}:refs/heads/main',
                '--force',
            )
        elif outcome == 'extra-ref':
            _git(host, 'update-ref', 'refs/heads/extra', candidate.commit)

    def _save_failed(*_args: object) -> Never:
        raise PubanonError('disk full')

    monkeypatch.setattr(_stage, 'set_default_branch', _default)
    with monkeypatch.context() as fault:
        if outcome == 'save-failure':
            fault.setattr(_stage, '_save_stage', _save_failed)
        expected_error = OperationCanceled if outcome == 'cancelled' else PubanonError
        with pytest.raises(expected_error) as exc_info:
            _stage.stage_repair(paths, global_config, project, yes=True)
    assert path.read_bytes() == b'prior tracking evidence'
    assert 'stage repair' in (exc_info.value.recovery or '')
    assert _calls(tmp_path).count(patch_args) == 1
    if outcome in {'response-lost', 'save-failure', 'cancelled'}:
        _host_responses(fake_gh, project)
        _stage.stage_repair(paths, global_config, project, yes=True)
        assert _calls(tmp_path).count(patch_args) == 1
        assert _stage.load_stage(paths, project.name) == _stage.StageRecord(
            candidate.commit, project.owner, 'staging-repo'
        )


@pytest.mark.parametrize(
    'tracking', ['current', 'missing', 'malformed', 'unreadable', 'other-target']
)
def test_status_reports_available_facts_without_a_store_or_mutation(
    tracking: str,
    tmp_path: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _paths(tmp_path)
    global_config, project = _configs(tmp_path)
    commit = 'a' * 40
    path = _stage._stage_path(paths, project.name)
    if tracking != 'missing':
        _save_stage(
            paths,
            project.name,
            _stage.StageRecord(
                commit,
                project.owner,
                'old-stage' if tracking == 'other-target' else 'staging-repo',
            ),
        )
    if tracking == 'malformed':
        path.write_bytes(b'broken\xff tracking')
    elif tracking == 'unreadable':
        path.unlink()
        path.mkdir()
    _status_responses(fake_gh, project, commit, {})
    reads: list[str] = []

    def _refs(_dest: _host.Destination) -> _host.RemoteRefs:
        reads.append('refs')
        return _host.RemoteRefs(commit, {})

    def _forbidden(*_args: object, **_kwargs: object) -> Never:
        pytest.fail('status attempted a mutation')

    monkeypatch.setattr(_stage, 'read_remote_refs', _refs)
    for name in ('store_path', '_save_stage', 'push', 'set_default_branch', 'delete_repository'):
        monkeypatch.setattr(_stage, name, _forbidden)
    before = {
        p.relative_to(paths.data_dir): p.read_bytes()
        for p in paths.data_dir.rglob('*')
        if p.is_file()
    }
    if tracking == 'current':
        with verbose_context(enabled=True):
            _stage.stage_status(paths, global_config, project)
    else:
        with verbose_context(enabled=True), pytest.raises(PubanonError) as raised:
            _stage.stage_status(paths, global_config, project)
        assert raised.value.recovery
        assert 'stage' in raised.value.recovery
    rendered = capsys.readouterr().out
    assert 'Staging repository stage-owner/staging-repo (private)' in rendered
    assert 'Repository URL: https://github.com/stage-owner/staging-repo.git' in rendered
    assert 'Visibility: private' in rendered
    assert 'Default branch: main' in rendered
    observed = {
        'current': 'matches recorded staged commit',
        'other-target': 'matches recorded staged commit',
        'missing': commit,
        'malformed': commit,
        'unreadable': commit,
    }[tracking]
    assert f'Observed remote commit: {observed}' in rendered
    assert rendered.count(commit) == 1
    assert 'Prepared: none' in rendered
    if tracking == 'current':
        assert f'Staged: snapshot ({commit[:12]})' in rendered
    else:
        assert 'Next: pubanon publish' not in rendered
    assert reads == ['refs']
    assert not _candidate.candidate_store_path(paths, project.name).exists()
    assert before == {
        p.relative_to(paths.data_dir): p.read_bytes()
        for p in paths.data_dir.rglob('*')
        if p.is_file()
    }
    assert not any('-X' in call for call in _calls(tmp_path))


@pytest.mark.slow
def test_status_verbose_shows_matching_commit_once(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _host_path, paths, global_config, project, candidate = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    _status_responses(fake_gh, project, candidate.commit, {})
    capsys.readouterr()

    with verbose_context(enabled=True):
        _stage.stage_status(paths, global_config, project)

    rendered = capsys.readouterr().out
    assert 'Staging repository stage-owner/staging-repo (private)' in rendered
    assert f'Recorded staged commit: {candidate.commit}' in rendered
    assert 'Observed remote commit: matches recorded staged commit' in rendered
    assert 'Prepared candidate: matches staged commit' in rendered
    assert rendered.count(candidate.commit) == 1


@pytest.mark.slow
@pytest.mark.parametrize('state', ['absent', 'different', 'observed', 'all-distinct'])
def test_status_failure_does_not_repeat_prepared_commit_already_shown(
    state: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    candidate = _build_candidate(paths, global_config, project)
    recorded = candidate.commit if state in {'absent', 'different'} else 'a' * 40
    remote = None if state == 'absent' else candidate.commit if state == 'observed' else 'b' * 40
    _save_stage(paths, project.name, _stage.StageRecord(recorded, project.owner, 'staging-repo'))
    _status_responses(fake_gh, project, candidate.commit, {})

    def _refs(_dest: _host.Destination) -> _host.RemoteRefs:
        return _host.RemoteRefs(remote, {})

    monkeypatch.setattr(_stage, 'read_remote_refs', _refs)
    if state == 'observed':

        def _gate_failure(*_args: object) -> Never:
            raise PubanonError('candidate gate failed')

        monkeypatch.setattr(_stage, '_verified_stage_tip', _gate_failure)

    capture = StringIO()
    with (
        redirect_stdout(capture),
        verbose_context(enabled=True),
        pytest.raises(PubanonError) as raised,
    ):
        _stage.stage_status(paths, global_config, project)

    rendered = capture.getvalue()
    assert raised.value.recovery
    assert f'Recorded staged commit: {recorded}' in rendered
    assert rendered.count(candidate.commit) == 1
    if remote is not None and remote != recorded:
        assert f'Observed remote commit: {remote}' in rendered
        assert rendered.count(remote) == 1
    if state in {'absent', 'different'}:
        assert 'Prepared candidate: matches recorded staged commit' in rendered
    elif state == 'observed':
        assert 'Prepared candidate: matches observed remote commit' in rendered
    else:
        assert f'Prepared candidate: {candidate.commit}' in rendered
    assert 'Prepared candidate match: unknown' in rendered


@pytest.mark.parametrize(
    'state', ['legacy', 'empty', 'unknown', 'wrong-default', 'public', 'refs-failure']
)
def test_status_reports_observed_facts_before_actionable_errors(
    state: str,
    tmp_path: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths = _paths(tmp_path)
    global_config, project = _configs(tmp_path)
    commit = 'a' * 40
    _save_stage(paths, project.name, _stage.StageRecord(commit, project.owner, 'staging-repo'))
    _status_responses(fake_gh, project, commit, {})
    if state == 'wrong-default':
        fake_gh.write_text(fake_gh.read_text().replace('main', 'master'))
    elif state == 'public':
        fake_gh.write_text(fake_gh.read_text().replace('private', 'public'))

    def _refs(_dest: _host.Destination) -> _host.RemoteRefs:
        if state == 'refs-failure':
            raise PubanonError('refs unavailable')
        if state == 'legacy':
            return _host.RemoteRefs(None, {}, (('stage', commit),))
        return _host.RemoteRefs(
            None if state == 'empty' else 'b' * 40 if state == 'unknown' else commit, {}
        )

    monkeypatch.setattr(_stage, 'read_remote_refs', _refs)
    with verbose_context(enabled=True), pytest.raises(PubanonError) as raised:
        _stage.stage_status(paths, global_config, project)
    rendered = capsys.readouterr().out
    assert f'Recorded staged commit: {commit}' in rendered
    assert f'Visibility: {"public" if state == "public" else "private"}' in rendered
    assert raised.value.recovery
    if state == 'legacy':
        assert 'Remote branch stage: matches recorded staged commit' in rendered
        assert rendered.count(commit) == 1
        assert 'rename stage to main through GitHub' in raised.value.recovery
    elif state == 'unknown':
        assert f'Observed remote commit: {"b" * 40}' in rendered
        assert 'Staged commit:' not in rendered
    elif state == 'refs-failure':
        assert 'Observed remote commit:' not in rendered
    assert not any('-X' in call for call in _calls(tmp_path))


@pytest.mark.slow
@pytest.mark.parametrize('candidate_state', ['same', 'different', 'absent'])
def test_status_compares_prepared_candidate_but_checks_recorded_snapshot(
    candidate_state: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host, paths, global_config, project, candidate = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    project = replace(project, required_checks=('CI',))
    prepared = replace(candidate, commit='b' * 40) if candidate_state == 'different' else candidate
    prepared = replace(prepared, fingerprint=_candidate.fingerprint(global_config, project))

    def _candidate_read(_paths: Paths, _project: str) -> _candidate.CandidateRecord | None:
        return None if candidate_state == 'absent' else prepared

    monkeypatch.setattr(_stage, 'load_candidate', _candidate_read)
    _status_responses(fake_gh, project, candidate.commit, {'CI': 'success'})
    before_refs = _git(host, 'for-each-ref')
    before = _stage._stage_path(paths, project.name).read_bytes()
    capture = StringIO()
    with redirect_stdout(capture):
        _stage.stage_status(paths, global_config, project)
    rendered = capture.getvalue()
    assert f'Staged: snapshot ({candidate.commit[:12]})' in rendered
    assert 'Checks: CI passed' in rendered
    if candidate_state == 'different':
        assert f'Prepared: {prepared.commit[:12]}' in rendered
        assert 'do not cover the prepared candidate' in rendered
        assert rendered.rstrip().endswith('Next: pubanon stage push')
    elif candidate_state == 'same':
        assert 'same as the prepared snapshot' in rendered
    else:
        assert 'same as the prepared snapshot' not in rendered
    assert _git(host, 'for-each-ref') == before_refs
    assert _stage._stage_path(paths, project.name).read_bytes() == before


@pytest.mark.slow
@pytest.mark.parametrize('tracking', ['missing', 'malformed', 'stale', 'other-target'])
def test_status_identifies_candidate_ci_but_requires_tracking_repair(
    tracking: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _host_path, paths, global_config, project, candidate = _prepare_stage(
        tmp_path, fake_gh, make_repo, monkeypatch, checks=('CI',)
    )
    path = _stage._stage_path(paths, project.name)
    if tracking == 'missing':
        path.unlink()
    elif tracking == 'malformed':
        path.write_bytes(b'broken\xff tracking')
    else:
        _save_stage(
            paths,
            project.name,
            _stage.StageRecord(
                'b' * 40 if tracking == 'stale' else candidate.commit,
                project.owner,
                'old-stage' if tracking == 'other-target' else 'staging-repo',
            ),
        )
    before = path.read_bytes() if path.exists() else None
    _status_responses(fake_gh, project, candidate.commit, {'CI': 'success'})
    capture = StringIO()
    with (
        redirect_stdout(capture),
        verbose_context(enabled=True),
        pytest.raises(PubanonError) as raised,
    ):
        _stage.stage_status(paths, global_config, project)
    assert 'pubanon stage repair' in (raised.value.recovery or '')
    rendered = capture.getvalue()
    assert rendered.count(candidate.commit) == 1
    if tracking == 'stale':
        assert rendered.count('b' * 40) == 1
    assert f'Staged: snapshot ({candidate.commit[:12]})' in rendered
    assert 'Checks: CI passed' in rendered
    assert 'Next: pubanon publish' not in rendered
    assert (path.read_bytes() if path.exists() else None) == before


@pytest.mark.slow
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
    _git(host, 'fetch', '-q', project.source_path.as_posix(), f'+{remote_tip}:refs/heads/main')
    _host_responses(fake_gh, project)

    with pytest.raises(PubanonError, match='staging tip') as exc_info:
        _stage.verify_staged_target(paths, global_config, project)

    assert exc_info.value.expected == record.commit
    assert exc_info.value.actual == remote_tip
    assert exc_info.value.recovery == 'pubanon stage repair or pubanon stage push'


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
    monkeypatch.setattr(_stage, 'read_remote_refs', _unexpected)

    with pytest.raises(PubanonError, match='invalid stage record') as exc_info:
        _stage.verify_staged_target(paths, global_config, project)

    assert exc_info.value.recovery == 'pubanon stage repair or pubanon stage forget'
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

    assert exc_info.value.recovery == 'pubanon stage repair or pubanon stage push'
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

    def _unexpected_refs(*_args: object) -> _host.RemoteRefs:
        pytest.fail('missing store reached staging ref read')

    monkeypatch.setattr(_stage, 'read_remote_refs', _unexpected_refs)

    with pytest.raises(PubanonError, match='snapshot store') as exc_info:
        _stage.verify_staged_target(paths, global_config, project)

    assert exc_info.value.recovery == 'pubanon repo restore --to local'
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
        *,
        timeout: float | None = None,
    ) -> _host.RemoteRefs:
        assert timeout is not None
        assert timeout > 0
        return _host.RemoteRefs('a' * 40, {'v1': 'b' * 40}, ())

    _candidate.store_path(paths, project.name)
    _host_responses(fake_gh, project)
    monkeypatch.setattr(_stage, 'read_remote_refs', _refs)

    with pytest.raises(PubanonError, match='unexpected staging reference') as raised:
        _stage.verify_staged_target(paths, global_config, project, timeout=30)

    assert 'Git or GitHub' in (raised.value.recovery or '')


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

    def _unexpected(*_args: object) -> _host.RemoteRefs:
        pytest.fail('reference read occurred before private-visibility verification')

    monkeypatch.setattr(_stage, 'read_remote_refs', _unexpected)

    with pytest.raises(PubanonError, match='staging repository facts') as exc_info:
        _stage.verify_staged_target(paths, global_config, project, timeout=30)

    assert exc_info.value.expected is not None
    assert 'visibility=private' in exc_info.value.expected
    assert exc_info.value.actual is not None
    assert 'visibility=public' in exc_info.value.actual
    assert exc_info.value.recovery == 'pubanon stage repair'


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
    monkeypatch.setattr(_host, '_URL_TEMPLATE', host.as_uri())

    def _closed(_prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr('builtins.input', _closed)

    with pytest.raises(PubanonError, match='this step needs an interactive terminal'):
        _stage.stage_push(paths, global_config, project, yes=True)

    assert not any('POST' in call for call in _calls(tmp_path))
