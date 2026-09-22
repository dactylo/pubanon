import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Never, cast

import pytest

from pubanon._config import (
    GlobalConfig,
    PackageObservation,
    Paths,
    ProjectConfig,
    stage_repository,
)
from pubanon._errors import ObservationTimeoutError, PubanonError
from pubanon._remote import _host, _stage, _watch
from pubanon._remote._host import CheckVerdict, Destination, RemoteRefs
from pubanon._remote._package import PackageRelease
from pubanon._remote._stage import _save_stage
from pubanon._snapshots import _candidate


class _Clock:
    def __init__(self) -> None:
        self._now = 0.0
        self._sleeps: list[float] = []

    def _monotonic(self) -> float:
        return self._now

    def _sleep(self, seconds: float) -> None:
        self._sleeps.append(seconds)
        self._now += seconds


class _FailIfTouched:
    def __getattribute__(self, name: str) -> object:
        pytest.fail(f'configuration was touched: {name}')


def _absent_release(
    _index: str,
    _name: str,
    _version: str,
    *,
    timeout: float | None = None,
) -> PackageRelease | None:
    del timeout
    return None


def _calls(tmp_path: Path) -> list[list[str]]:
    path = tmp_path / 'fake-gh-calls.jsonl'
    if not path.exists():
        return []
    return [json.loads(line)['args'] for line in path.read_text().splitlines()]


def _check_response(
    commit: str,
    name: str,
    *,
    status: str,
    conclusion: str | None,
    once: bool = False,
) -> dict[str, object]:
    query = f'check_name={name}&filter=latest&per_page=100'
    response: dict[str, object] = {
        'args': [
            'api',
            f'repos/publication-owner/public-repo/commits/{commit}/check-runs?{query}',
        ],
        'stdout': json.dumps(
            {
                'total_count': 1,
                'check_runs': [
                    {
                        'name': name,
                        'head_sha': commit,
                        'status': status,
                        'conclusion': conclusion,
                    }
                ],
            }
        )
        + '\n',
    }
    if once:
        response['once'] = True
    return response


def _configs(
    tmp_path: Path,
    *,
    package: PackageObservation | None = None,
    required_checks: tuple[str, ...] = ('CI',),
) -> tuple[Paths, GlobalConfig, ProjectConfig]:
    config_dir = tmp_path / 'config'
    data_dir = tmp_path / 'data'
    return (
        Paths(config_dir, data_dir, config_dir / 'config.toml', data_dir / 'registry.json'),
        GlobalConfig(
            anon_name='Anonymous Publisher',
            anon_email='anonymous@example.invalid',
            accounts={'github': 'anonymous-account'},
            sensitive_terms=(),
        ),
        ProjectConfig(
            name='example',
            source_path=tmp_path / 'source',
            host='github',
            owner='publication-owner',
            repo='public-repo',
            visibility='private',
            allow=('**',),
            deny=(),
            secret_exceptions=(),
            extra_terms=(),
            hygiene_terms=(),
            self_publication=False,
            required_checks=required_checks,
            _stage_repo='staging-repo',
            package=package,
        ),
    )


def _destination(
    *,
    owner: str = 'publication-owner',
    repo: str = 'public-repo',
    visibility: str = 'private',
) -> Destination:
    return Destination(
        adapter='github',
        owner=owner,
        repo=repo,
        url=f'https://github.com/{owner}/{repo}.git',
        account='anonymous-account',
        visibility=visibility,
    )


def _failed_check(
    _coordinates: tuple[str, str],
    _commit: str,
    name: str,
    *,
    timeout: float | None = None,
) -> CheckVerdict:
    del timeout
    return CheckVerdict(name, 'unacceptable', 'failure')


def _host_responses(
    fake_gh: Path,
    *checks: dict[str, object],
    repo: str = 'public-repo',
    visibility: str = 'private',
) -> None:
    fake_gh.write_text(
        json.dumps(
            [
                {
                    'args': ['api', 'user', '--jq', '.login'],
                    'stdout': 'anonymous-account\n',
                },
                {
                    'args': [
                        'api',
                        f'repos/publication-owner/{repo}',
                        '--jq',
                        _host._REPOSITORY_QUERY,
                    ],
                    'stdout': json.dumps(
                        {
                            'owner': 'publication-owner',
                            'repo': repo,
                            'visibility': visibility,
                        }
                    )
                    + '\n',
                },
                *checks,
            ]
        )
        + '\n'
    )


def _missing_check_response(commit: str, name: str, *, once: bool = False) -> dict[str, object]:
    query = f'check_name={name}&filter=latest&per_page=100'
    response: dict[str, object] = {
        'args': [
            'api',
            f'repos/publication-owner/public-repo/commits/{commit}/check-runs?{query}',
        ],
        'stdout': json.dumps({'total_count': 0, 'check_runs': []}) + '\n',
    }
    if once:
        response['once'] = True
    return response


def _patch_clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    clock = _Clock()
    monkeypatch.setattr(_watch, '_monotonic', clock._monotonic)
    monkeypatch.setattr(_watch, '_sleep', clock._sleep)
    return clock


def _pending_check(
    _coordinates: tuple[str, str],
    _commit: str,
    name: str,
    *,
    timeout: float | None = None,
) -> CheckVerdict:
    del timeout
    return CheckVerdict(name, 'pending', 'queued')


def _raise_observation_timeout(*, stage: str | None = None) -> Never:
    raise ObservationTimeoutError(stage=stage)


def _record_stage(paths: Paths, project: str, record: _stage.StageRecord) -> None:
    _candidate.store_path(paths, project)
    _save_stage(paths, project, record)


def _stub_published_refs(
    monkeypatch: pytest.MonkeyPatch,
    main: str | None,
    tags: dict[str, str],
) -> None:
    def _refs(_paths: Paths, _project: str) -> tuple[str | None, dict[str, str]]:
        return main, tags

    monkeypatch.setattr(_watch, 'published_refs', _refs)


def _stub_remote_refs(monkeypatch: pytest.MonkeyPatch, refs: RemoteRefs) -> None:
    def _read_refs(
        _dest: Destination,
        _paths: Paths,
        _project: str,
        *,
        timeout: float | None = None,
    ) -> RemoteRefs:
        del timeout
        return refs

    monkeypatch.setattr(_watch, 'ls_remote', _read_refs)


def _published_refs(
    monkeypatch: pytest.MonkeyPatch,
    main: str | None,
    tags: dict[str, str],
) -> None:
    _stub_published_refs(monkeypatch, main, tags)
    _stub_remote_refs(monkeypatch, RemoteRefs(main, tags, ()))


def _stub_staged_destination(
    monkeypatch: pytest.MonkeyPatch,
    verifier: Callable[[Paths, GlobalConfig, ProjectConfig], Destination],
) -> None:

    def _staging_destination(
        paths: Paths,
        global_config: GlobalConfig,
        project: ProjectConfig,
    ) -> Destination:
        selected = replace(
            project,
            repo=stage_repository(project),
            visibility='private',
        )
        return verifier(paths, global_config, selected)

    def _require_account(
        _expected: str,
        *,
        authority: str,
        timeout: float | None = None,
    ) -> str:
        del authority, timeout
        return 'publication-owner'

    def _verify_facts(
        _destination: Destination,
        *,
        recovery: str,
        timeout: float | None = None,
    ) -> None:
        del recovery, timeout

    monkeypatch.setattr(_stage, '_stage_destination', _staging_destination)
    monkeypatch.setattr(_stage, 'require_authenticated_account', _require_account)
    monkeypatch.setattr(_stage, '_verify_stage_facts', _verify_facts)


def _successful_check(
    _coordinates: tuple[str, str],
    _commit: str,
    name: str,
    *,
    timeout: float | None = None,
) -> CheckVerdict:
    del timeout
    return CheckVerdict(name, 'success', 'success')


def _verify_selected(
    _paths: Paths,
    _g: GlobalConfig,
    selected: ProjectConfig,
    *,
    timeout: float | None = None,
) -> Destination:
    del timeout
    return _destination(
        owner=selected.owner,
        repo=selected.repo,
        visibility=selected.visibility,
    )


@pytest.mark.parametrize(
    ('verdict', 'recovery'),
    [
        pytest.param(
            CheckVerdict('CI', 'pending', 'in_progress'),
            'wait for the check, then rerun the watch',
            id='pending',
        ),
        pytest.param(
            CheckVerdict('CI', 'missing', ''),
            'check the configured name and workflow trigger, then rerun the watch',
            id='missing',
        ),
        pytest.param(
            CheckVerdict('CI', 'ambiguous', '2 check runs'),
            'resolve the multiple matching check runs, then rerun the watch',
            id='ambiguous',
        ),
        pytest.param(
            CheckVerdict('CI', 'unacceptable', 'failure'),
            'inspect the failed check and fix or rerun its workflow, then rerun the watch',
            id='failed',
        ),
    ],
)
def test_check_recovery_matches_observed_state(
    verdict: CheckVerdict,
    recovery: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _patch_clock(monkeypatch)

    def _check(*_args: object, timeout: float | None = None) -> CheckVerdict:
        assert timeout is not None
        assert timeout > 0
        return verdict

    monkeypatch.setattr(_watch, 'check_result', _check)

    with pytest.raises(PubanonError) as raised:
        _watch._wait_for_checks(_destination(), 'a' * 40, ('CI',), 1)

    assert raised.value.recovery == recovery
    assert clock._sleeps == ([] if verdict.state in {'ambiguous', 'unacceptable'} else [1])


def test_ci_verdicts_reads_each_configured_check_once_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, str, str, str]] = []

    def _check(
        coordinates: tuple[str, str],
        commit: str,
        name: str,
        *,
        timeout: float | None = None,
    ) -> CheckVerdict:
        assert timeout is None
        calls.append((*coordinates, commit, name))
        return CheckVerdict(name, 'success', 'success')

    monkeypatch.setattr(_watch, 'check_result', _check)

    assert _watch.ci_verdicts(_destination(), 'a' * 40, ('Build', 'Test')) == [
        CheckVerdict('Build', 'success', 'success'),
        CheckVerdict('Test', 'success', 'success'),
    ]
    assert calls == [
        ('publication-owner', 'public-repo', 'a' * 40, 'Build'),
        ('publication-owner', 'public-repo', 'a' * 40, 'Test'),
    ]


def test_default_ci_selector_requires_published_main(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _global_config, project = _configs(tmp_path)
    _published_refs(monkeypatch, None, {})

    with pytest.raises(PubanonError, match='published commit') as raised:
        _watch._select_ci_commit(paths, project.name, None)

    assert raised.value.recovery == 'pubanon publish'


def test_default_package_selector_requires_a_published_release_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _global_config, project = _configs(tmp_path)
    _published_refs(monkeypatch, 'a' * 40, {'unrelated': 'b' * 40})

    with pytest.raises(PubanonError, match='published release tag'):
        _watch.select_package_release(paths, project.name, None)


def test_default_selectors_keep_ci_main_and_latest_semantic_tag_separate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _global_config, project = _configs(tmp_path)
    tags = {
        'z-release': 'a' * 40,
        'v1.9.0': 'b' * 40,
        'v1.10.0': 'c' * 40,
    }
    _published_refs(monkeypatch, 'd' * 40, tags)

    assert _watch._select_ci_commit(paths, project.name, None) == 'd' * 40
    assert _watch.select_package_release(paths, project.name, None) == (
        '1.10.0',
        'c' * 40,
    )


def test_explicit_selectors_carry_the_selected_tags_exact_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _global_config, project = _configs(tmp_path)
    tags = {'v0.2.0': 'b' * 40, 'v0.3.0': 'c' * 40}
    _published_refs(monkeypatch, 'd' * 40, tags)

    assert _watch._select_ci_commit(paths, project.name, '0.2.0') == 'b' * 40
    assert _watch.select_package_release(paths, project.name, '0.2.0') == (
        '0.2.0',
        'b' * 40,
    )


def test_package_watch_passes_decreasing_positive_remaining_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = 'a' * 40
    package = PackageObservation('pypi', 'demo', ('Release',))
    paths, global_config, project = _configs(tmp_path, package=package)
    destination = _destination()
    clock = _patch_clock(monkeypatch)
    events: list[tuple[str, float | None]] = []
    _stub_published_refs(monkeypatch, 'b' * 40, {'v1.2.3': commit})

    def _verify(
        _paths: Paths,
        _g: GlobalConfig,
        _p: ProjectConfig,
        *,
        timeout: float | None = None,
    ) -> Destination:
        events.append(('account/repository verification', timeout))
        clock._now += 1
        return destination

    def _refs(
        _dest: Destination,
        _paths: Paths,
        _project: str,
        *,
        timeout: float | None = None,
    ) -> RemoteRefs:
        events.append(('refs', timeout))
        clock._now += 1
        return RemoteRefs('b' * 40, {'v1.2.3': commit})

    def _check(
        _coordinates: tuple[str, str],
        _commit: str,
        name: str,
        *,
        timeout: float | None = None,
    ) -> CheckVerdict:
        events.append((name, timeout))
        clock._now += 1
        return CheckVerdict(name, 'success', 'success')

    def _release(
        _index: str,
        _name: str,
        version: str,
        *,
        timeout: float | None = None,
    ) -> PackageRelease:
        events.append(('package release', timeout))
        clock._now += 1
        return PackageRelease(version, (f'demo-{version}.tar.gz',))

    monkeypatch.setattr(_watch, 'verify_destination', _verify)
    monkeypatch.setattr(_watch, 'ls_remote', _refs)
    monkeypatch.setattr(_watch, 'check_result', _check)
    monkeypatch.setattr(_watch, 'release_state', _release)

    _watch.watch_package(paths, global_config, project, version=None, timeout=30)

    assert events == [
        ('account/repository verification', 30.0),
        ('refs', 29.0),
        ('Release', 28.0),
        ('package release', 27.0),
    ]
    assert all(timeout is not None and timeout > 0 for _stage, timeout in events)
    assert clock._sleeps == []


def test_published_ci_watch_passes_decreasing_positive_remaining_time(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = 'a' * 40
    paths, global_config, project = _configs(
        tmp_path,
        required_checks=('Build', 'Test'),
    )
    destination = _destination()
    clock = _patch_clock(monkeypatch)
    events: list[tuple[str, float | None]] = []
    _stub_published_refs(monkeypatch, commit, {})

    def _verify(
        _paths: Paths,
        _g: GlobalConfig,
        _p: ProjectConfig,
        *,
        timeout: float | None = None,
    ) -> Destination:
        events.append(('account/repository verification', timeout))
        clock._now += 1
        return destination

    def _refs(
        _dest: Destination,
        _paths: Paths,
        _project: str,
        *,
        timeout: float | None = None,
    ) -> RemoteRefs:
        events.append(('refs', timeout))
        clock._now += 1
        return RemoteRefs(commit, {})

    def _check(
        _coordinates: tuple[str, str],
        _commit: str,
        name: str,
        *,
        timeout: float | None = None,
    ) -> CheckVerdict:
        events.append((name, timeout))
        clock._now += 1
        return CheckVerdict(name, 'success', 'success')

    monkeypatch.setattr(_watch, 'verify_destination', _verify)
    monkeypatch.setattr(_watch, 'ls_remote', _refs)
    monkeypatch.setattr(_watch, 'check_result', _check)

    _watch.watch_ci(paths, global_config, project, staged=False, version=None, timeout=30)

    assert events == [
        ('account/repository verification', 30.0),
        ('refs', 29.0),
        ('Build', 28.0),
        ('Test', 27.0),
    ]
    assert all(timeout is not None and timeout > 0 for _stage, timeout in events)
    assert clock._sleeps == []


def test_published_ci_watch_refuses_moved_main_before_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_main = 'a' * 40
    paths, global_config, project = _configs(tmp_path)
    _published_refs(monkeypatch, local_main, {})
    monkeypatch.setattr(_watch, 'verify_destination', _verify_selected)
    _stub_remote_refs(monkeypatch, RemoteRefs('b' * 40, {}, ()))

    def _unexpected(*_args: object) -> CheckVerdict:
        pytest.fail('check read occurred after published main drift')

    monkeypatch.setattr(_watch, 'check_result', _unexpected)

    with pytest.raises(PubanonError, match='host refs do not match'):
        _watch.watch_ci(paths, global_config, project, staged=False, version=None, timeout=30)


def test_published_ci_watch_refuses_unexpected_branch_before_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = 'a' * 40
    paths, global_config, project = _configs(tmp_path)
    _published_refs(monkeypatch, main, {})
    monkeypatch.setattr(_watch, 'verify_destination', _verify_selected)
    _stub_remote_refs(monkeypatch, RemoteRefs(main, {}, (('backup', main),)))

    def _unexpected(*_args: object) -> CheckVerdict:
        pytest.fail('check read occurred with an unexpected published branch')

    monkeypatch.setattr(_watch, 'check_result', _unexpected)

    with pytest.raises(PubanonError, match='host refs do not match') as raised:
        _watch.watch_ci(paths, global_config, project, staged=False, version=None, timeout=30)

    assert raised.value.recovery == ('remove every destination branch except main, then rerun')
    assert raised.value.expected == f'main={main}; tags=none; branches=none'
    assert raised.value.actual == f'main={main}; tags=none; branches=backup={main}'


def test_published_package_watch_refuses_deleted_tag_before_checks_or_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = 'b' * 40
    tag_commit = 'a' * 40
    package = PackageObservation('pypi', 'demo', ('Release',))
    paths, global_config, project = _configs(tmp_path, package=package)
    _published_refs(monkeypatch, main, {'v1.2.3': tag_commit})
    monkeypatch.setattr(_watch, 'verify_destination', _verify_selected)
    _stub_remote_refs(monkeypatch, RemoteRefs(main, {}, ()))

    def _unexpected(*_args: object) -> object:
        pytest.fail('downstream observation occurred after published tag deletion')

    monkeypatch.setattr(_watch, 'check_result', _unexpected)
    monkeypatch.setattr(_watch, 'release_state', _unexpected)

    with pytest.raises(PubanonError, match='host refs do not match'):
        _watch.watch_package(paths, global_config, project, version='1.2.3', timeout=30)


def test_published_package_watch_refuses_unexpected_tag_before_checks_or_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = 'b' * 40
    tags = {'v1.2.3': 'a' * 40}
    package = PackageObservation('pypi', 'demo', ('Release',))
    paths, global_config, project = _configs(tmp_path, package=package)
    _published_refs(monkeypatch, main, tags)
    monkeypatch.setattr(_watch, 'verify_destination', _verify_selected)
    _stub_remote_refs(
        monkeypatch,
        RemoteRefs(main, {**tags, 'v2.0.0': 'c' * 40}, ()),
    )

    def _unexpected(*_args: object) -> object:
        pytest.fail('downstream observation occurred with an unexpected published tag')

    monkeypatch.setattr(_watch, 'check_result', _unexpected)
    monkeypatch.setattr(_watch, 'release_state', _unexpected)

    with pytest.raises(PubanonError, match='host refs do not match'):
        _watch.watch_package(paths, global_config, project, version='1.2.3', timeout=30)


def test_staged_watch_polls_missing_check_until_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project = _configs(tmp_path)
    record = _stage.StageRecord('a' * 40, 'publication-owner', 'staging-repo')
    states = iter(
        [
            CheckVerdict('CI', 'missing', ''),
            CheckVerdict('CI', 'success', 'success'),
        ]
    )
    calls = 0

    def _verify(
        *_args: object, timeout: float | None = None
    ) -> tuple[Destination, _stage.StageRecord]:
        assert timeout is not None
        assert timeout > 0
        return _destination(repo=record._repo), record

    def _check(*_args: object, timeout: float | None = None) -> CheckVerdict:
        nonlocal calls
        assert timeout is not None
        assert timeout > 0
        calls += 1
        return next(states)

    monkeypatch.setattr(_watch, 'verify_staged_target', _verify)
    monkeypatch.setattr(_watch, 'check_result', _check)
    clock = _patch_clock(monkeypatch)

    _watch.watch_ci(paths, global_config, project, staged=True, version=None, timeout=30)

    assert calls == 2
    assert clock._sleeps == [15]


def test_staged_watch_propagates_verification_failure_before_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project = _configs(tmp_path)
    failure = PubanonError('staging tip mismatch', recovery='pubanon stage push')

    def _verify(*_args: object, **_kwargs: object) -> Never:
        raise failure

    def _unexpected(*_args: object, **_kwargs: object) -> Never:
        pytest.fail('check read occurred after staging verification failed')

    monkeypatch.setattr(_watch, 'verify_staged_target', _verify)
    monkeypatch.setattr(_watch, 'check_result', _unexpected)

    with pytest.raises(PubanonError) as raised:
        _watch.watch_ci(paths, global_config, project, staged=True, version=None, timeout=30)

    assert raised.value is failure


def test_staged_watch_requires_configured_checks_before_stage_reads(
    tmp_path: Path,
) -> None:
    paths, global_config, project = _configs(tmp_path, required_checks=())

    with pytest.raises(PubanonError, match='no required checks') as raised:
        _watch.watch_ci(paths, global_config, project, staged=True, version=None, timeout=30)

    assert raised.value.recovery == 'add required_checks to the project configuration'


def test_staged_watch_uses_recorded_coordinates_and_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    commit = 'a' * 40
    paths, global_config, project = _configs(tmp_path)
    _record_stage(
        paths,
        project.name,
        _stage.StageRecord(commit, 'publication-owner', 'staging-repo'),
    )
    verified: list[tuple[str, str, str]] = []
    checked: list[tuple[str, str, str]] = []

    def _verify(
        _paths: Paths,
        _g: GlobalConfig,
        selected: ProjectConfig,
    ) -> Destination:
        verified.append((selected.owner, selected.repo, selected.visibility))
        return _destination(
            owner=selected.owner, repo=selected.repo, visibility=selected.visibility
        )

    def _refs(
        dest: Destination,
        _paths: Paths,
        _project: str,
        *,
        timeout: float | None = None,
    ) -> tuple[str | None, tuple[str, ...]]:
        assert (dest.owner, dest.repo) == ('publication-owner', 'staging-repo')
        assert timeout is not None
        assert timeout > 0
        return commit, ()

    def _check(
        coordinates: tuple[str, str],
        selected_commit: str,
        name: str,
        *,
        timeout: float | None = None,
    ) -> CheckVerdict:
        assert timeout is not None
        assert timeout > 0
        checked.append((*coordinates, selected_commit))
        return CheckVerdict(name, 'success', 'success')

    _stub_staged_destination(monkeypatch, _verify)
    monkeypatch.setattr(_stage, 'stage_refs', _refs)
    monkeypatch.setattr(_watch, 'check_result', _check)

    _watch.watch_ci(paths, global_config, project, staged=True, version=None, timeout=30)

    assert verified == [('publication-owner', 'staging-repo', 'private')]
    assert checked == [('publication-owner', 'staging-repo', commit)]
    output = capsys.readouterr().out
    assert 'Next: pubanon publish\n' in output
    assert 'Next: no action is required.\n' not in output
    assert f'Selected commit: {commit}\n' in output
    assert output.rstrip().splitlines()[-1].startswith('Next:')
    assert 'Watch snapshot CI\n' in output
    assert 'Repository: publication-owner/staging-repo\n' in output


def test_staged_watch_uses_shared_staged_target_verification_before_checks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project = _configs(tmp_path)
    record = _stage.StageRecord('a' * 40, 'publication-owner', 'staging-repo')
    destination = _destination(repo=record._repo)
    calls: list[str] = []

    def _verify(
        selected_paths: Paths,
        selected_global: GlobalConfig,
        selected_project: ProjectConfig,
        *,
        timeout: float | None = None,
    ) -> tuple[Destination, _stage.StageRecord]:
        assert (selected_paths, selected_global, selected_project) == (
            paths,
            global_config,
            project,
        )
        assert timeout is not None
        assert timeout > 0
        calls.append('verify')
        return destination, record

    def _check(
        _coordinates: tuple[str, str],
        commit: str,
        name: str,
        *,
        timeout: float | None = None,
    ) -> CheckVerdict:
        assert commit == record.commit
        assert timeout is not None
        assert timeout > 0
        calls.append('check')
        return CheckVerdict(name, 'success', 'success')

    monkeypatch.setattr(_watch, 'verify_staged_target', _verify)
    monkeypatch.setattr(_watch, 'check_result', _check)

    _watch.watch_ci(paths, global_config, project, staged=True, version=None, timeout=30)

    assert calls == ['verify', 'check']


def test_staged_watch_uses_timeout_signal_stage_and_makes_no_later_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project = _configs(tmp_path)

    def _verify(*_args: object, timeout: float | None = None) -> Never:
        assert timeout is not None
        assert timeout > 0
        _raise_observation_timeout(stage='refs')

    def _unexpected(*_args: object, **_kwargs: object) -> Never:
        pytest.fail('check read occurred after staged ref timeout')

    monkeypatch.setattr(_watch, 'verify_staged_target', _verify)
    monkeypatch.setattr(_watch, 'check_result', _unexpected)
    clock = _patch_clock(monkeypatch)

    with pytest.raises(PubanonError, match='observation timed out') as exc_info:
        _watch.watch_ci(paths, global_config, project, staged=True, version=None, timeout=30)

    assert exc_info.value.fact == 'refs: pending'
    assert exc_info.value.recovery == 'rerun `pubanon watch ci --staged`'
    assert clock._sleeps == []


@pytest.mark.parametrize('selector', [_watch._select_ci_commit, _watch.select_package_release])
def test_supplied_version_refuses_an_absent_published_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selector: Callable[[Paths, str, str | None], object],
) -> None:
    paths, _global_config, project = _configs(tmp_path)
    _published_refs(monkeypatch, 'a' * 40, {'v0.3.0': 'b' * 40})

    with pytest.raises(PubanonError, match='--version'):
        selector(paths, project.name, '0.2.0')


@pytest.mark.parametrize('selector', [_watch._select_ci_commit, _watch.select_package_release])
def test_supplied_version_requires_semantic_xyz_form(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    selector: Callable[[Paths, str, str | None], object],
) -> None:
    paths, _global_config, project = _configs(tmp_path)
    _published_refs(monkeypatch, 'a' * 40, {})

    with pytest.raises(PubanonError, match=r'X\.Y\.Z'):
        selector(paths, project.name, '01.2.3')


@pytest.mark.parametrize(
    ('verdicts', 'expected_fact'),
    [
        (
            (CheckVerdict('Build', 'unacceptable', 'failure'),),
            'Build: unacceptable (failure)',
        ),
        (
            (CheckVerdict('Build', 'ambiguous', '2 check runs'),),
            'Build: ambiguous (2 check runs)',
        ),
        (
            (
                CheckVerdict('Build', 'missing', ''),
                CheckVerdict('Test', 'unacceptable', 'failure'),
            ),
            'Test: unacceptable (failure)',
        ),
    ],
)
def test_wait_stops_before_later_request_after_failed_check(
    verdicts: tuple[CheckVerdict, ...],
    expected_fact: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checks = ('Build', 'Test', 'Later')[: len(verdicts) + 1]
    package = PackageObservation('pypi', 'demo', checks)
    paths, global_config, project = _configs(tmp_path, package=package)
    _published_refs(monkeypatch, 'b' * 40, {'v1.2.3': 'a' * 40})
    monkeypatch.setattr(_watch, 'verify_destination', _verify_selected)
    calls: list[str] = []

    def _check(
        _coordinates: tuple[str, str],
        _commit: str,
        name: str,
        *,
        timeout: float | None = None,
    ) -> CheckVerdict:
        assert timeout is not None
        assert timeout > 0
        calls.append(name)
        try:
            index = checks.index(name)
            return verdicts[index]
        except IndexError:
            pytest.fail('later check was requested after a terminal check result')

    def _unexpected(*_args: object, **_kwargs: object) -> Never:
        pytest.fail('package index was observed after a terminal check result')

    monkeypatch.setattr(_watch, 'check_result', _check)
    monkeypatch.setattr(_watch, 'release_state', _unexpected)
    clock = _patch_clock(monkeypatch)

    with pytest.raises(PubanonError, match='not acceptable') as raised:
        _watch.watch_package(paths, global_config, project, version=None, timeout=30)

    assert raised.value.fact == expected_fact
    assert calls == list(checks[: len(verdicts)])
    assert clock._sleeps == []


def test_watch_ci_failed_check_refuses_without_sleep_or_another_poll(
    tmp_path: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = 'a' * 40
    paths, global_config, project = _configs(tmp_path)
    _published_refs(monkeypatch, commit, {})
    _host_responses(
        fake_gh,
        _check_response(commit, 'CI', status='completed', conclusion='failure'),
    )
    clock = _patch_clock(monkeypatch)

    with pytest.raises(PubanonError, match='not acceptable') as raised:
        _watch.watch_ci(paths, global_config, project, staged=False, version=None, timeout=30)

    assert raised.value.fact == 'CI: unacceptable (failure)'
    assert clock._sleeps == []


def test_watch_ci_pending_past_deadline_fails_closed_without_an_extra_poll(
    tmp_path: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = 'a' * 40
    paths, global_config, project = _configs(tmp_path)
    _published_refs(monkeypatch, commit, {})
    _host_responses(
        fake_gh,
        _check_response(commit, 'CI', status='queued', conclusion=None),
    )
    clock = _patch_clock(monkeypatch)

    with pytest.raises(PubanonError, match='pending') as raised:
        _watch.watch_ci(paths, global_config, project, staged=False, version=None, timeout=10)

    assert raised.value.fact == 'CI: pending (queued)'
    assert raised.value.recovery == 'wait for the check, then rerun the watch'
    check_path = f'/commits/{commit}/check-runs?'
    assert len([call for call in _calls(tmp_path) if check_path in call[-1]]) == 1
    assert clock._sleeps == [10]


def test_watch_ci_polls_missing_then_pending_check_until_success(
    tmp_path: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = 'a' * 40
    paths, global_config, project = _configs(tmp_path)
    _published_refs(monkeypatch, commit, {})
    _host_responses(
        fake_gh,
        _missing_check_response(commit, 'CI', once=True),
        _check_response(
            commit,
            'CI',
            status='in_progress',
            conclusion=None,
            once=True,
        ),
        _check_response(commit, 'CI', status='completed', conclusion='success'),
    )
    clock = _patch_clock(monkeypatch)

    _watch.watch_ci(paths, global_config, project, staged=False, version=None, timeout=45)

    check_path = f'/commits/{commit}/check-runs?'
    assert len([call for call in _calls(tmp_path) if check_path in call[-1]]) == 3
    assert clock._sleeps == [15, 15]


def test_watch_ci_polls_pending_check_then_succeeds(
    tmp_path: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = 'a' * 40
    paths, global_config, project = _configs(tmp_path)
    _published_refs(monkeypatch, commit, {})
    _host_responses(
        fake_gh,
        _check_response(
            commit,
            'CI',
            status='in_progress',
            conclusion=None,
            once=True,
        ),
        _check_response(commit, 'CI', status='completed', conclusion='success'),
    )
    clock = _patch_clock(monkeypatch)

    _watch.watch_ci(paths, global_config, project, staged=False, version=None, timeout=30)

    check_path = f'/commits/{commit}/check-runs?'
    assert len([call for call in _calls(tmp_path) if check_path in call[-1]]) == 2
    assert clock._sleeps == [15]


def test_watch_ci_preserves_selected_version_in_package_next_step(
    tmp_path: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    selected_commit = 'a' * 40
    package = PackageObservation('pypi', 'demo', ('Release',))
    paths, global_config, project = _configs(tmp_path, package=package)
    _published_refs(
        monkeypatch,
        'b' * 40,
        {'v1.2.3': selected_commit, 'v2.0.0': 'c' * 40},
    )
    _host_responses(
        fake_gh,
        _check_response(selected_commit, 'CI', status='completed', conclusion='success'),
    )
    _patch_clock(monkeypatch)

    _watch.watch_ci(paths, global_config, project, staged=False, version='1.2.3', timeout=30)

    output = capsys.readouterr().out
    assert f'Selected commit: {selected_commit}\n' in output
    assert output.rstrip().endswith('Next: pubanon watch package --version 1.2.3')


def test_watch_ci_refuses_conflicting_selectors_before_observation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail('observation seam was touched')

    monkeypatch.setattr(_watch, '_monotonic', _unexpected)
    monkeypatch.setattr(_watch, 'verify_staged_target', _unexpected)
    monkeypatch.setattr(_watch, '_select_ci_commit', _unexpected)
    monkeypatch.setattr(_watch, '_verified_published_destination', _unexpected)
    monkeypatch.setattr(_watch, '_wait_for_checks', _unexpected)
    untouched = _FailIfTouched()

    with pytest.raises(PubanonError) as raised:
        _watch.watch_ci(
            cast('Paths', untouched),
            cast('GlobalConfig', untouched),
            cast('ProjectConfig', untouched),
            staged=True,
            version='1.2.3',
            timeout=30,
        )

    assert str(raised.value) == 'the staging repository holds prepared snapshots, not releases'


def test_watch_ci_reports_target_state_changes_and_elapsed_before_waiting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    commit = 'a' * 40
    paths, global_config, project = _configs(tmp_path)
    _published_refs(monkeypatch, commit, {})
    monkeypatch.setattr(_watch, 'verify_destination', _verify_selected)
    states = iter(
        [
            CheckVerdict('CI', 'pending', 'queued'),
            CheckVerdict('CI', 'pending', 'queued'),
            CheckVerdict('CI', 'pending', 'queued'),
            CheckVerdict('CI', 'success', 'success'),
        ]
    )

    def _check(*_args: object, timeout: float | None = None) -> CheckVerdict:
        assert timeout is not None
        assert timeout > 0
        return next(states)

    clock = _patch_clock(monkeypatch)
    observed_before_first_sleep: list[str] = []

    def _sleep(seconds: float) -> None:
        if not observed_before_first_sleep:
            observed_before_first_sleep.append(capsys.readouterr().out)
        clock._sleeps.append(seconds)
        clock._now += seconds

    monkeypatch.setattr(_watch, '_sleep', _sleep)
    monkeypatch.setattr(_watch, 'check_result', _check)

    _watch.watch_ci(paths, global_config, project, staged=False, version=None, timeout=60)

    first = observed_before_first_sleep[0]
    assert 'Watch snapshot CI\n' in first
    assert 'Repository: publication-owner/public-repo\n' in first
    assert 'Required checks: CI\n' in first
    assert 'Timeout: 60 seconds. Ctrl-C stops waiting.\n' in first
    phases = [
        line.removeprefix('Phase: ').rstrip('.')
        for line in first.splitlines()
        if line.startswith('Phase: ')
    ]
    assert phases == [
        'verifying account and repository',
        'reading published refs',
        'reading CI checks',
    ]
    assert '  CI: pending (queued)\n' in first
    output = capsys.readouterr().out
    assert 'Elapsed: 30 seconds. Still waiting for CI.\n' in output
    assert '  CI: success (success)\n' in output
    assert clock._sleeps == [15, 15, 15]


def test_watch_ci_success_reports_answers_and_keeps_oid_in_details(
    tmp_path: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    commit = 'a' * 40
    paths, global_config, project = _configs(tmp_path)
    _published_refs(monkeypatch, commit, {})
    _host_responses(
        fake_gh,
        _check_response(commit, 'CI', status='completed', conclusion='success'),
    )
    clock = _patch_clock(monkeypatch)

    _watch.watch_ci(paths, global_config, project, staged=False, version=None, timeout=30)

    output = capsys.readouterr().out
    assert output.startswith('Watch snapshot CI\n')
    assert 'Repository: publication-owner/public-repo\n' in output
    assert 'Required checks: CI\n' in output
    assert 'Timeout: 30 seconds. Ctrl-C stops waiting.\n' in output
    assert 'Required CI checks succeeded for the selected snapshot.\n' in output
    assert '  CI: success (success)\n' in output
    assert 'Nothing was changed.\n' not in output
    assert 'Revealing the repository makes its history public.\n' in output
    assert 'Next: pubanon repo reveal\n' in output
    assert f'Selected commit: {commit}\n' in output
    assert output.rstrip().splitlines()[-1].startswith('Next:')
    assert clock._sleeps == []


@pytest.mark.parametrize(
    'case',
    [
        pytest.param(
            (
                'account/repository verification',
                ('account/repository verification',),
                'account/repository verification: pending',
                'verifying account and repository',
            ),
            id='account',
        ),
        pytest.param(
            (
                'refs',
                ('account/repository verification', 'refs'),
                'refs: pending',
                'reading published refs',
            ),
            id='refs',
        ),
        pytest.param(
            (
                'CI',
                ('account/repository verification', 'refs', 'CI'),
                'CI: pending',
                'reading CI checks',
            ),
            id='check',
        ),
    ],
)
def test_watch_ci_translates_in_flight_expiry_and_makes_no_later_call(
    case: tuple[str, tuple[str, ...], str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    failed_stage, expected_events, fact, phase = case
    commit = 'a' * 40
    paths, global_config, project = _configs(tmp_path)
    destination = _destination()
    events: list[str] = []
    _stub_published_refs(monkeypatch, commit, {})

    def _verify(*_args: object, timeout: float | None = None) -> Destination:
        assert timeout is not None
        assert timeout > 0
        events.append('account/repository verification')
        if failed_stage == events[-1]:
            _raise_observation_timeout()
        return destination

    def _refs(*_args: object, timeout: float | None = None) -> RemoteRefs:
        assert timeout is not None
        assert timeout > 0
        events.append('refs')
        if failed_stage == events[-1]:
            _raise_observation_timeout()
        return RemoteRefs(commit, {})

    def _check(
        _coordinates: tuple[str, str],
        _commit: str,
        name: str,
        *,
        timeout: float | None = None,
    ) -> CheckVerdict:
        assert timeout is not None
        assert timeout > 0
        events.append(name)
        if failed_stage == name:
            _raise_observation_timeout()
        return CheckVerdict(name, 'success', 'success')

    monkeypatch.setattr(_watch, 'verify_destination', _verify)
    monkeypatch.setattr(_watch, 'ls_remote', _refs)
    monkeypatch.setattr(_watch, 'check_result', _check)
    clock = _patch_clock(monkeypatch)

    with pytest.raises(PubanonError, match='observation timed out') as exc_info:
        _watch.watch_ci(paths, global_config, project, staged=False, version=None, timeout=30)

    assert exc_info.value.fact == fact
    assert exc_info.value.recovery == 'rerun the watch'
    assert events == list(expected_events)
    phases = [
        line.removeprefix('Phase: ').rstrip('.')
        for line in capsys.readouterr().out.splitlines()
        if line.startswith('Phase: ')
    ]
    assert phases[-1] == phase
    assert len(phases) == len(expected_events)
    assert clock._sleeps == []
    assert not paths.config_dir.exists()
    assert not paths.data_dir.exists()


def test_watch_ci_zero_timeout_starts_no_network_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = 'a' * 40
    paths, global_config, project = _configs(tmp_path)
    _stub_published_refs(monkeypatch, commit, {})

    def _unexpected(*_args: object, **_kwargs: object) -> Never:
        pytest.fail('network call started with no remaining observation time')

    monkeypatch.setattr(_watch, 'verify_destination', _unexpected)
    monkeypatch.setattr(_watch, 'ls_remote', _unexpected)
    monkeypatch.setattr(_watch, 'check_result', _unexpected)
    clock = _patch_clock(monkeypatch)

    with pytest.raises(PubanonError, match='observation timed out') as exc_info:
        _watch.watch_ci(paths, global_config, project, staged=False, version=None, timeout=0)

    assert exc_info.value.fact == 'account/repository verification: pending'
    assert exc_info.value.recovery == 'rerun the watch'
    assert clock._sleeps == []


def test_watch_package_failed_check_never_reads_index_or_polls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = PackageObservation('pypi', 'demo', ('Release',))
    paths, global_config, project = _configs(tmp_path, package=package)
    _published_refs(monkeypatch, 'b' * 40, {'v1.2.3': 'a' * 40})
    monkeypatch.setattr(_watch, 'verify_destination', _verify_selected)
    monkeypatch.setattr(_watch, 'check_result', _failed_check)

    def _unexpected(*_args: object) -> PackageRelease | None:
        pytest.fail('package index read occurred after a failed release check')

    monkeypatch.setattr(_watch, 'release_state', _unexpected)
    clock = _patch_clock(monkeypatch)

    with pytest.raises(PubanonError, match='not acceptable'):
        _watch.watch_package(paths, global_config, project, version=None, timeout=30)

    assert clock._sleeps == []


def test_watch_package_metadata_without_files_remains_incomplete_until_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package = PackageObservation('pypi', 'demo', ('Release',))
    paths, global_config, project = _configs(tmp_path, package=package)
    _published_refs(monkeypatch, 'b' * 40, {'v1.2.3': 'a' * 40})
    monkeypatch.setattr(_watch, 'verify_destination', _verify_selected)
    monkeypatch.setattr(_watch, 'check_result', _successful_check)

    def _metadata_only(
        _index: str,
        _name: str,
        version: str,
        *,
        timeout: float | None = None,
    ) -> PackageRelease:
        assert timeout is not None
        assert timeout > 0
        return PackageRelease(version, ())

    monkeypatch.setattr(_watch, 'release_state', _metadata_only)
    clock = _patch_clock(monkeypatch)

    with pytest.raises(PubanonError, match='pending') as raised:
        _watch.watch_package(paths, global_config, project, version=None, timeout=45)

    assert raised.value.fact == 'demo 1.2.3 has metadata but no distribution files'
    assert raised.value.recovery == 'wait for distribution files, then rerun the watch'
    assert 'Elapsed: 30 seconds. Still waiting for package files.\n' in capsys.readouterr().out
    assert clock._sleeps == [15, 15, 15]


def test_watch_package_missing_check_expires_without_reading_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = PackageObservation('pypi', 'demo', ('Release',))
    paths, global_config, project = _configs(tmp_path, package=package)
    _published_refs(monkeypatch, 'b' * 40, {'v1.2.3': 'a' * 40})
    monkeypatch.setattr(_watch, 'verify_destination', _verify_selected)
    calls = 0

    def _check(
        _coordinates: tuple[str, str],
        _commit: str,
        name: str,
        *,
        timeout: float | None = None,
    ) -> CheckVerdict:
        nonlocal calls
        assert timeout is not None
        assert timeout > 0
        calls += 1
        return CheckVerdict(name, 'missing', '')

    def _unexpected(*_args: object, **_kwargs: object) -> Never:
        pytest.fail('package index read occurred while a release check was missing')

    monkeypatch.setattr(_watch, 'check_result', _check)
    monkeypatch.setattr(_watch, 'release_state', _unexpected)
    clock = _patch_clock(monkeypatch)

    with pytest.raises(PubanonError, match='pending') as raised:
        _watch.watch_package(paths, global_config, project, version=None, timeout=10)

    assert raised.value.fact == 'Release: missing ()'
    assert raised.value.recovery == (
        'check the configured name and workflow trigger, then rerun the watch'
    )
    assert calls == 1
    assert clock._sleeps == [10]


def test_watch_package_pending_check_never_reads_index(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = PackageObservation('pypi', 'demo', ('Release',))
    paths, global_config, project = _configs(tmp_path, package=package)
    _published_refs(monkeypatch, 'b' * 40, {'v1.2.3': 'a' * 40})
    monkeypatch.setattr(_watch, 'verify_destination', _verify_selected)
    monkeypatch.setattr(_watch, 'check_result', _pending_check)

    def _unexpected(*_args: object) -> PackageRelease | None:
        pytest.fail('package index read occurred while release checks were pending')

    monkeypatch.setattr(_watch, 'release_state', _unexpected)
    clock = _patch_clock(monkeypatch)

    with pytest.raises(PubanonError, match='pending'):
        _watch.watch_package(paths, global_config, project, version=None, timeout=10)

    assert clock._sleeps == [10]


def test_watch_package_persistent_absence_expires_as_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    package = PackageObservation('pypi', 'demo', ('Release',))
    paths, global_config, project = _configs(tmp_path, package=package)
    _published_refs(monkeypatch, 'b' * 40, {'v1.2.3': 'a' * 40})
    monkeypatch.setattr(_watch, 'verify_destination', _verify_selected)
    monkeypatch.setattr(_watch, 'check_result', _successful_check)
    monkeypatch.setattr(_watch, 'release_state', _absent_release)
    clock = _patch_clock(monkeypatch)

    with pytest.raises(PubanonError, match='pending') as raised:
        _watch.watch_package(paths, global_config, project, version=None, timeout=10)

    assert raised.value.fact == 'demo 1.2.3 is not yet published'
    assert raised.value.recovery == 'wait for the package release, then rerun the watch'
    assert clock._sleeps == [10]


def test_watch_package_polls_absence_then_reports_published_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    commit = 'a' * 40
    package = PackageObservation('pypi', 'demo-private-canary\x1b', ('Release',))
    paths, global_config, project = _configs(tmp_path, package=package)
    _published_refs(monkeypatch, 'b' * 40, {'v1.2.3': commit})
    startup: list[str] = []

    def _verify(
        selected_paths: Paths,
        selected_global: GlobalConfig,
        selected: ProjectConfig,
        *,
        timeout: float | None = None,
    ) -> Destination:
        output = capsys.readouterr().out
        assert f'Package name: demo-{"private-canary"}\\x1b\n' in output
        assert 'private-canary' in output
        assert '\x1b' not in output
        startup.append(output)
        return _verify_selected(selected_paths, selected_global, selected, timeout=timeout)

    monkeypatch.setattr(_watch, 'verify_destination', _verify)
    check_calls = 0

    def _check(*_args: object, timeout: float | None = None) -> CheckVerdict:
        nonlocal check_calls
        assert timeout is not None
        assert timeout > 0
        check_calls += 1
        return CheckVerdict('Release', 'success', 'success')

    states = iter(
        [
            None,
            PackageRelease('1.2.3', ()),
            PackageRelease(
                '1.2.3',
                ('demo-1.2.3.tar.gz', 'demo-1.2.3\x1bprivate-canary.whl'),
            ),
        ]
    )
    release_calls: list[tuple[str, str, str]] = []

    def _release(
        index: str,
        name: str,
        version: str,
        *,
        timeout: float | None = None,
    ) -> PackageRelease | None:
        assert timeout is not None
        assert timeout > 0
        release_calls.append((index, name, version))
        return next(states)

    monkeypatch.setattr(_watch, 'check_result', _check)
    monkeypatch.setattr(_watch, 'release_state', _release)
    clock = _patch_clock(monkeypatch)

    _watch.watch_package(paths, global_config, project, version=None, timeout=45)

    assert len(startup) == 1
    output = startup[0] + capsys.readouterr().out
    assert output.startswith('Watch package release\n')
    assert all(
        item in output
        for item in (
            'Repository: publication-owner/public-repo\n',
            f'Selected commit: {commit}\n',
            'Package version: 1.2.3\n',
            'Release checks: Release\n',
            'Timeout: 45 seconds. Ctrl-C stops waiting.\n',
        )
    )
    assert '  Package: absent\n' in output
    assert '  Package: metadata found; no distribution files\n' in output
    assert 'Package release 1.2.3 is published.\n' in output
    assert '  Release: success (success)\n' in output
    assert 'Published files\n' in output
    assert '  demo-1.2.3.tar.gz\n' in output
    assert f'  demo-1.2.3\\x1b{"private-canary"}.whl' in output
    assert '\x1b' not in output
    assert 'private-canary' in output
    assert 'Nothing was changed.\n' not in output
    assert 'Revealing the repository makes its history public.\n' in output
    assert 'Next: pubanon repo reveal\n' in output
    assert 'Release tag: v1.2.3\n' in output
    assert output.rstrip().splitlines()[-1].startswith('Next:')
    assert check_calls == 1
    assert release_calls == [
        ('pypi', package.name, '1.2.3'),
        ('pypi', package.name, '1.2.3'),
        ('pypi', package.name, '1.2.3'),
    ]
    assert clock._sleeps == [15, 15]


def test_watch_package_rejects_release_returned_after_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package = PackageObservation('pypi', 'demo', ('Release',))
    paths, global_config, project = _configs(tmp_path, package=package)
    _published_refs(monkeypatch, 'b' * 40, {'v1.2.3': 'a' * 40})
    monkeypatch.setattr(_watch, 'verify_destination', _verify_selected)
    monkeypatch.setattr(_watch, 'check_result', _successful_check)
    clock = _patch_clock(monkeypatch)
    calls = 0

    def _release(*_args: object, timeout: float | None = None) -> PackageRelease:
        nonlocal calls
        assert timeout == 30
        calls += 1
        clock._now = 31
        return PackageRelease('1.2.3', ('demo.whl',))

    monkeypatch.setattr(_watch, 'release_state', _release)
    with pytest.raises(PubanonError, match='observation timed out') as raised:
        _watch.watch_package(paths, global_config, project, version=None, timeout=30)

    assert raised.value.fact == 'package release: pending'
    assert calls == 1
    assert clock._sleeps == []
    assert 'is published' not in capsys.readouterr().out


def test_watch_package_requires_explicit_package_configuration(tmp_path: Path) -> None:
    paths, global_config, project = _configs(tmp_path)

    with pytest.raises(PubanonError, match=r'\[package\]'):
        _watch.watch_package(paths, global_config, project, version=None, timeout=30)


def test_watch_package_translates_in_flight_release_expiry_and_stops(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commit = 'a' * 40
    package = PackageObservation('pypi', 'demo', ('Release',))
    paths, global_config, project = _configs(tmp_path, package=package)
    destination = _destination()
    events: list[str] = []
    _stub_published_refs(monkeypatch, 'b' * 40, {'v1.2.3': commit})

    def _verify(*_args: object, timeout: float | None = None) -> Destination:
        assert timeout is not None
        assert timeout > 0
        events.append('account/repository verification')
        return destination

    def _refs(*_args: object, timeout: float | None = None) -> RemoteRefs:
        assert timeout is not None
        assert timeout > 0
        events.append('refs')
        return RemoteRefs('b' * 40, {'v1.2.3': commit})

    def _check(*_args: object, timeout: float | None = None) -> CheckVerdict:
        assert timeout is not None
        assert timeout > 0
        events.append('Release')
        return CheckVerdict('Release', 'success', 'success')

    def release(*_args: object, timeout: float | None = None) -> Never:
        assert timeout is not None
        assert timeout > 0
        events.append('package release')
        _raise_observation_timeout()

    monkeypatch.setattr(_watch, 'verify_destination', _verify)
    monkeypatch.setattr(_watch, 'ls_remote', _refs)
    monkeypatch.setattr(_watch, 'check_result', _check)
    monkeypatch.setattr(_watch, 'release_state', release)
    clock = _patch_clock(monkeypatch)

    with pytest.raises(PubanonError, match='observation timed out') as exc_info:
        _watch.watch_package(paths, global_config, project, version=None, timeout=30)

    assert exc_info.value.fact == 'package release: pending'
    assert exc_info.value.recovery == 'rerun the watch'
    assert events == [
        'account/repository verification',
        'refs',
        'Release',
        'package release',
    ]
    assert clock._sleeps == []
