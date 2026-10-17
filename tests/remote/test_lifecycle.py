import json
import re
import subprocess
from collections.abc import Callable, Sequence
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path

import pytest

from pubanon import (
    _config,
    _filesystem,
)
from pubanon._config import (
    GlobalConfig,
    PackageObservation,
    Paths,
    ProjectConfig,
    Settings,
)
from pubanon._errors import ObservationTimeoutError, OperationCanceled, PubanonError
from pubanon._gate import RuleClass
from pubanon._remote import _actions, _host, _immutable, _lifecycle, _package, _watch
from pubanon._remote._ci import RunEvidence, RunQuery
from pubanon._remote._host import CheckVerdict
from pubanon._snapshots import _audit, _candidate, _records, _source, _store
from pubanon._terminal import escape, verbose_context
from tests._cli import gh_calls
from tests._git import git_env
from tests._permissions import assert_private_mode

_ABSENCE_ARGS = [
    'api',
    'repos/anonymous-account/public-repo',
    '--jq',
    _host._REPOSITORY_QUERY,
]
_ACCOUNT_ARGS = ['api', 'user', '--jq', '.login']
_CREATE_ARGS = [
    'api',
    '-X',
    'POST',
    'user/repos',
    '-f',
    'name=public-repo',
    '-F',
    'private=true',
    '-f',
    'description=Public repository',
    '-F',
    'has_issues=true',
    '-F',
    'has_wiki=false',
    '-F',
    'has_projects=true',
    '-F',
    'has_discussions=false',
]
_PATCH_SETTINGS_ARGS = [
    'api',
    '-X',
    'PATCH',
    'repos/anonymous-account/public-repo',
    '-F',
    'has_pull_requests=false',
]
_REPOSITORY_ARGS = [
    'api',
    'repos/anonymous-account/public-repo',
    '--jq',
    '{owner: .owner.login, repo: .name, visibility: .visibility}',
]
_SETTINGS_ARGS = [
    'api',
    'repos/anonymous-account/public-repo',
    '--jq',
    _host._REPOSITORY_SETTINGS_QUERY,
]
_SET_VISIBILITY_ARGS = [
    'api',
    '-X',
    'PATCH',
    'repos/anonymous-account/public-repo',
    '-f',
    'visibility=public',
]
_CONVERGE_PUBLIC = _lifecycle._converge_public
_VERIFY_AFTER_FLIP = _lifecycle._verify_after_flip
_VERIFY_BEFORE_FLIP = _lifecycle._verify_before_flip
_PublishedReveal = tuple[Paths, GlobalConfig, ProjectConfig, _host.Destination, str]
_Script = Callable[[Sequence[str | tuple[str, str]]], None]


class _PostFlipClock:
    def __init__(self, oversleep: float = 0.0) -> None:
        self.now = 0.0
        self.oversleep = oversleep
        self.attempts: list[float] = []
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds + self.oversleep


def _configs(tmp_path: Path) -> tuple[Paths, GlobalConfig, ProjectConfig]:
    config_dir = tmp_path / 'config'
    data_dir = tmp_path / 'data'
    paths = Paths(config_dir, data_dir, config_dir / 'config.toml', data_dir / 'registry.json')
    global_config = GlobalConfig(
        anon_name='Anonymous Publisher',
        anon_email='anonymous@example.invalid',
        accounts={'github': 'anonymous-account'},
        sensitive_terms=(),
    )
    project = ProjectConfig(
        name='demo',
        source_path=(tmp_path / 'source').resolve(),
        host='github',
        owner='anonymous-account',
        repo='public-repo',
        visibility='private',
        allow=(),
        deny=(),
        secret_exceptions=(),
        extra_terms=(),
        hygiene_terms=(),
        self_publication=True,
        required_checks=(),
        publication_route='direct',
        settings=Settings(
            description='Public repository',
            default_branch='main',
            issues=True,
            wiki=False,
            projects=True,
            pull_requests=False,
            discussions=False,
        ),
    )
    return paths, global_config, project


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ['git', '-C', repo, *args],
        check=True,
        capture_output=True,
        env=git_env(),
    ).stdout


def _job_entry(identifier: int, run: int) -> dict[str, object]:
    return {
        'id': identifier,
        'run_id': run,
        'run_attempt': 1,
        'name': 'lint',
        'status': 'completed',
        'conclusion': 'success',
        'runner_id': 5,
        'runner_name': 'GitHub Actions 5',
        'runner_group_name': 'GitHub Actions',
        'labels': ['ubuntu-latest'],
        'steps': [],
    }


def _post_flip_unread() -> _lifecycle._PostFlipReadError:
    error = _lifecycle._PostFlipReadError(
        'the repository is now public but post-flip verification failed',
        fact=(
            'error: Git ls-remote failed\n  exit status 128: remote: Your repository is disabled.'
        ),
        recovery='fix the reported drift, then rerun pubanon repo reveal',
    )
    error.__cause__ = PubanonError('Git ls-remote failed')
    return error


def _responses(path: Path, responses: list[dict[str, object]]) -> None:
    path.write_text(
        json.dumps(
            [
                *responses,
                {
                    'args': [
                        'api',
                        '--method',
                        'GET',
                        'repos/anonymous-account/public-repo/releases?per_page=100&page=1',
                    ],
                    'stdout': '[]',
                },
            ]
        )
    )


def _run_entry(identifier: int, name: str) -> dict[str, object]:
    return {
        'id': identifier,
        'name': name,
        'run_attempt': 1,
        'status': 'completed',
        'event': 'push',
        'path': '.github/workflows/ci.yml',
        'head_branch': 'main',
        'head_sha': 'a' * 40,
        'created_at': '2026-09-22T10:00:00Z',
        'display_title': name,
        'head_commit': None,
        'actor': None,
        'triggering_actor': None,
    }


def _scripted_post_flip(
    monkeypatch: pytest.MonkeyPatch,
    results: list[PubanonError | str],
    *,
    oversleep: float = 0.0,
) -> tuple[_PostFlipClock, list[ProjectConfig]]:
    clock = _PostFlipClock(oversleep)
    saved: list[ProjectConfig] = []

    def _verify(_paths: Paths, _g: GlobalConfig, _p: ProjectConfig) -> str:
        clock.attempts.append(clock.now)
        result = results.pop(0)
        if isinstance(result, PubanonError):
            raise result
        return result

    def _save(_paths: Paths, public_project: ProjectConfig) -> None:
        saved.append(public_project)

    monkeypatch.setattr(_lifecycle, '_monotonic', clock.monotonic)
    monkeypatch.setattr(_lifecycle, '_sleep', clock.sleep)
    monkeypatch.setattr(_lifecycle, '_verify_after_flip', _verify)
    monkeypatch.setattr(_lifecycle, 'save_project', _save)
    return clock, saved


def _disclosure_responses(
    runs: tuple[str, ...] = (),
    artifacts: tuple[str, ...] = (),
    *,
    caches: int = 0,
    jobs: dict[int, list[dict[str, object]]] | None = None,
) -> list[dict[str, object]]:
    responses: list[dict[str, object]] = []
    for resource, collection, entries in (
        ('runs', 'workflow_runs', runs),
        ('artifacts', 'artifacts', artifacts),
    ):
        for offset in range(0, max(len(entries), 1), 100):
            page = offset // 100 + 1
            parsed = [
                (int(entry.partition(' ')[0]), entry.partition(' ')[2])
                for entry in entries[offset : offset + 100]
            ]
            responses.append(
                {
                    'args': [
                        'api',
                        (
                            'repos/anonymous-account/public-repo/actions/'
                            f'{resource}?per_page=100&page={page}'
                        ),
                    ],
                    'stdout': json.dumps(
                        {
                            'total_count': len(entries),
                            collection: [
                                _run_entry(identifier, name)
                                if resource == 'runs'
                                else {'id': identifier, 'name': name}
                                for identifier, name in parsed
                            ],
                        }
                    ),
                }
            )
    for entry in runs:
        identifier = int(entry.partition(' ')[0])
        run_jobs = (jobs or {}).get(identifier, [])
        responses.append(
            {
                'args': [
                    'api',
                    (
                        f'repos/anonymous-account/public-repo/actions/runs/{identifier}/jobs'
                        '?filter=all&per_page=100&page=1'
                    ),
                ],
                'stdout': json.dumps({'total_count': len(run_jobs), 'jobs': run_jobs}),
            }
        )
    responses.append(
        {
            'args': ['api', 'repos/anonymous-account/public-repo/actions/caches?per_page=1'],
            'stdout': json.dumps(
                {'total_count': caches, 'actions_caches': [{'id': 1}] if caches else []}
            ),
        }
    )
    return responses


def _published_fixture(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    *,
    message: str | None = None,
) -> tuple[Paths, GlobalConfig, ProjectConfig, _host.Destination, str]:
    paths, global_config, project = _configs(tmp_path)
    settings = Settings(
        description='Public repository',
        default_branch='main',
        issues=True,
        wiki=False,
        projects=True,
        pull_requests=False,
        discussions=False,
    )
    project = replace(project, allow=('**',), settings=settings)

    source = make_repo(files={'README.md': 'public\n'})
    pinned = _source.pin(source, 'HEAD')
    record = _candidate.build_candidate(
        paths,
        global_config,
        project,
        _candidate.CandidateInputs(
            _source.read_snapshot(source, pinned.commit),
            pinned,
            None,
            message,
        ),
    )
    store = _store.store_path(paths, project.name)
    _git(store, 'update-ref', 'refs/heads/main', record.commit)
    host = tmp_path / 'host.git'
    subprocess.run(['git', 'init', '--bare', '-q', host], check=True, env=git_env())
    _git(host, 'fetch', store.as_uri(), f'{record.commit}:refs/heads/main')
    destination = _host.Destination(
        adapter=project.host,
        owner=project.owner,
        repo=project.repo,
        url=host.as_uri(),
        account=global_config.accounts[project.host],
        visibility=project.visibility,
    )
    return paths, global_config, project, destination, record.commit


def _repository_response(
    visibility: str,
    *,
    owner: str = 'anonymous-account',
    repo: str = 'public-repo',
    once: bool = False,
) -> dict[str, object]:
    return {
        'args': _REPOSITORY_ARGS,
        'stdout': json.dumps(
            {
                'owner': owner,
                'repo': repo,
                'visibility': visibility,
            }
        ),
        'once': once,
    }


def _reveal_with_unreadable_remote(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Paths, GlobalConfig, ProjectConfig]:
    paths, global_config, project, destination, commit = _published_fixture(tmp_path, make_repo)
    _config.save_project(paths, project)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', destination.url)
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('private', once=True),
            _repository_response('private', once=True),
            {'args': _SET_VISIBILITY_ARGS},
            _repository_response('public'),
        ],
    )

    def _verified(*_args: object, **_kwargs: object) -> tuple[str, str]:
        return commit, ''

    def _disabled(*_args: object, **_kwargs: object) -> _host.RemoteRefs:
        raise PubanonError(
            'Git ls-remote failed',
            fact='exit status 128: remote: Your repository is disabled.\nfatal: 403',
        )

    monkeypatch.setattr(_lifecycle, '_verify_before_flip', _verified)
    monkeypatch.setattr(_host, 'read_remote_refs', _disabled)
    return paths, global_config, project


def _settings_response(settings: Settings) -> dict[str, object]:
    return {
        'args': _SETTINGS_ARGS,
        'stdout': json.dumps(
            {
                'description': settings.description,
                'default_branch': settings.default_branch,
                'issues': settings.issues,
                'wiki': settings.wiki,
                'projects': settings.projects,
                'pull_requests': settings.pull_requests,
                'discussions': settings.discussions,
            }
        ),
    }


def _success_responses(*, create_args: list[str] | None = None) -> list[dict[str, object]]:
    repository = {
        'owner': 'anonymous-account',
        'repo': 'public-repo',
        'visibility': 'private',
    }
    return [
        {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
        {
            'args': _ABSENCE_ARGS,
            'returncode': 1,
            'stdout': '{"status":"404"}\n',
            'once': True,
        },
        {'args': create_args or _CREATE_ARGS},
        {'args': _PATCH_SETTINGS_ARGS},
        _settings_response(
            Settings(
                'Public repository',
                'main',
                issues=True,
                wiki=False,
                projects=True,
                pull_requests=False,
                discussions=False,
            )
        ),
        {'args': _REPOSITORY_ARGS, 'stdout': json.dumps(repository) + '\n'},
    ]


@pytest.fixture(name='published_reveal')
def _published_reveal(
    tmp_path: Path, make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> _PublishedReveal:
    result = _published_fixture(tmp_path, make_repo, message='historical-private-canary release')
    monkeypatch.setattr(_host, '_URL_TEMPLATE', result[3].url)
    return result


def test_convergence_keeps_the_read_failure_message_when_reveal_did_not_change_visibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, global_config, project = _configs(tmp_path)

    def _unread(_paths: Paths, _g: GlobalConfig, _p: ProjectConfig) -> str:
        raise _lifecycle._PostFlipReadError(
            'the repository is now public but post-flip verification failed',
            fact='error: Git ls-remote failed',
            recovery='fix the reported drift, then rerun pubanon repo reveal',
        )

    monkeypatch.setattr(_lifecycle, '_verify_after_flip', _unread)

    def _unexpected_sleep(_seconds: float) -> None:
        raise AssertionError('convergence waited')

    monkeypatch.setattr(_lifecycle, '_sleep', _unexpected_sleep)

    with pytest.raises(PubanonError, match='post-flip verification failed') as error:
        _CONVERGE_PUBLIC(paths, global_config, project, host_changed=False)

    assert error.value.recovery is not None
    assert 'fix the reported drift' in error.value.recovery
    assert 'pubanon repo reveal' in error.value.recovery


def test_disclosure_inventory_preserves_complete_id_and_name_entry(tmp_path: Path) -> None:
    paths, _global_config, project = _configs(tmp_path)
    inventory = _host.Disclosure(
        (_host.DisclosureEntry(101, 'build'),),
        (_host.DisclosureEntry(201, 'wheel'),),
    )

    path = _lifecycle._save_disclosure(paths, project.name, inventory)

    assert json.loads(path.read_text(encoding='utf-8')) == {
        'runs': ['101 build'],
        'artifacts': ['201 wheel'],
    }


def test_disclosure_local_inventory_path_remains_usable_without_display_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _global_config, project = _configs(tmp_path)
    inventory = _host.Disclosure((), ())

    def _disclosure_inventory(_owner: str, _repo: str) -> _host.Disclosure:
        return inventory

    monkeypatch.setattr(_host, 'disclosure', _disclosure_inventory)

    def _no_caches(_owner: str, _repo: str) -> int:
        return 0

    monkeypatch.setattr(_host, 'actions_cache_count', _no_caches)
    prompts: list[str] = []

    def _decline(prompt: str) -> str:
        prompts.append(prompt)
        return 'n'

    monkeypatch.setattr('builtins.input', _decline)

    with pytest.raises(OperationCanceled, match=r'Canceled\. Nothing was changed.'):
        _lifecycle._confirm_disclosure(paths, project, (), already_public=False)

    assert prompts == ['Make it public? [y/n] ']
    disclosure_path = paths.data_dir / 'projects' / project.name / 'disclosure.json'
    assert disclosure_path.exists()
    assert 'Saved inventory:' not in capsys.readouterr().out
    with verbose_context(enabled=True), pytest.raises(OperationCanceled):
        _lifecycle._confirm_disclosure(paths, project, (), already_public=False)
    assert f'Saved inventory: {escape((disclosure_path).as_posix())}' in capsys.readouterr().out


def test_repo_create_declined_confirmation_creates_nothing(
    fake_gh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project = _configs(tmp_path)
    _responses(fake_gh, _success_responses())

    def _decline(prompt: str) -> str:
        assert prompt == 'Create anonymous-account/public-repo as a private repository? [y/n] '
        return 'n'

    monkeypatch.setattr('builtins.input', _decline)

    with pytest.raises(OperationCanceled, match=r'Canceled\. Nothing was created.'):
        _lifecycle.repo_create(paths, global_config, project, yes=False)

    assert gh_calls(tmp_path) == [_ACCOUNT_ARGS, _ABSENCE_ARGS]


def test_repo_create_failed_owner_lookup_is_safe_preflight_failure(
    fake_gh: Path,
    tmp_path: Path,
) -> None:
    term = 'private-canary'
    paths, global_config, project = _configs(tmp_path)
    project = replace(project, owner='publication-owner')
    absence_args = [
        'api',
        'repos/publication-owner/public-repo',
        '--jq',
        _host._REPOSITORY_QUERY,
    ]
    owner_args = ['api', 'users/publication-owner', '--jq', '.type']
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            {
                'args': absence_args,
                'returncode': 1,
                'stdout': '{"status":"404"}\n',
            },
            {
                'args': owner_args,
                'returncode': 1,
                'stderr': f'simulated {term} owner lookup failure\n',
            },
        ],
    )

    with pytest.raises(PubanonError) as exc_info:
        _lifecycle.repo_create(paths, global_config, project, yes=True)

    error = exc_info.value
    assert str(error) == f'simulated {term} owner lookup failure'
    assert 'unknown' not in error.render()
    assert 'partial' not in error.render()
    assert gh_calls(tmp_path) == [_ACCOUNT_ARGS, absence_args, owner_args]
    assert all('POST' not in call for call in gh_calls(tmp_path))
    assert not (paths.config_dir / 'projects' / f'{project.name}.toml').exists()


def test_repo_create_failed_post_reports_unknown_outcome_and_present_retry(
    fake_gh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    term = 'private-canary'
    paths, global_config, project = _configs(tmp_path)
    assert project.settings is not None
    remote = tmp_path / 'host.git'
    subprocess.run(['git', 'init', '--bare', '-q', remote], check=True, env=git_env())
    monkeypatch.setattr(_host, '_URL_TEMPLATE', remote.as_uri())
    absence: dict[str, object] = {
        'args': _ABSENCE_ARGS,
        'returncode': 1,
        'stdout': '{"status":"404"}\n',
        'once': True,
    }
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            absence,
            _repository_response('private'),
            _settings_response(project.settings),
            {
                'args': _CREATE_ARGS,
                'returncode': 1,
                'stderr': f'simulated {term} POST failure\n',
                'once': True,
            },
        ],
    )

    with pytest.raises(PubanonError) as exc_info:
        _lifecycle.repo_create(paths, global_config, project, yes=True)

    error = exc_info.value
    rendered = str(error)
    assert 'repository creation outcome is unknown' in rendered
    assert 'repository may already exist' in rendered
    assert error.recovery == 'pubanon repo create'
    rendered = error.render()
    assert term in rendered
    assert gh_calls(tmp_path).count(_CREATE_ARGS) == 1
    assert not (paths.config_dir / 'projects' / f'{project.name}.toml').exists()

    _lifecycle.repo_create(paths, global_config, project, yes=True)

    assert capsys.readouterr().out.rstrip().endswith('Next: pubanon prepare')
    assert gh_calls(tmp_path).count(_CREATE_ARGS) == 1
    assert not (paths.config_dir / 'projects' / f'{project.name}.toml').exists()


def test_repo_create_preserves_post_create_diagnostic(
    fake_gh: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, global_config, project = _configs(tmp_path)
    _responses(fake_gh, _success_responses())
    original = PubanonError('verification failed', fact=r'exit status 7: diagnostic\x1b\\path')

    def _fail(*_args: object) -> None:
        raise original

    monkeypatch.setattr(_host, 'verify_destination', _fail)
    with pytest.raises(PubanonError) as error:
        _lifecycle.repo_create(paths, global_config, project, yes=True)
    rendered = str(error.value)
    assert 'repository creation is partial' in rendered
    assert 'post-create verification failed' in rendered
    assert error.value.fact == original.render()
    assert error.value.recovery == 'pubanon repo create'
    assert _CREATE_ARGS in gh_calls(tmp_path)


def test_repo_create_refuses_user_type_for_organization_owner_before_post(
    fake_gh: Path,
    tmp_path: Path,
) -> None:
    paths, global_config, project = _configs(tmp_path)
    project = replace(project, owner='publication-owner')
    absence_args = [
        'api',
        'repos/publication-owner/public-repo',
        '--jq',
        _host._REPOSITORY_QUERY,
    ]
    owner_args = ['api', 'users/publication-owner', '--jq', '.type']
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            {
                'args': absence_args,
                'returncode': 1,
                'stdout': '{"status":"404"}\n',
            },
            {'args': owner_args, 'stdout': 'User\n'},
        ],
    )

    with pytest.raises(PubanonError, match='cannot create under the configured owner'):
        _lifecycle.repo_create(paths, global_config, project, yes=True)

    assert gh_calls(tmp_path) == [_ACCOUNT_ARGS, absence_args, owner_args]
    assert all('POST' not in call for call in gh_calls(tmp_path))


def test_repo_create_reports_gate_term_and_coordinate_field(
    tmp_path: Path,
) -> None:
    paths, global_config, project = _configs(tmp_path)
    term = 'hygiene-canary'
    project = replace(
        project,
        repo=f'public-repo-{term}',
        hygiene_terms=(term,),
    )

    with pytest.raises(
        PubanonError, match='repository name did not pass content checks'
    ) as exc_info:
        _lifecycle.repo_create(paths, global_config, project, yes=True)

    assert exc_info.value.fact == f'repo: public-repo-{term}'
    assert term in (exc_info.value.fact or '')
    assert gh_calls(tmp_path) == []


def test_repo_create_routes_account_requirement_with_configuration_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project = _configs(tmp_path)
    requirements: list[tuple[str, str]] = []

    def _require_account(expected: str, *, authority: str) -> str:
        requirements.append((expected, authority))
        return expected

    def _repository_visible(_owner: str, _repo: str) -> bool:
        raise PubanonError('visibility checked')

    monkeypatch.setattr(_host, 'require_authenticated_account', _require_account)
    monkeypatch.setattr(_host, 'repository_visible', _repository_visible)

    with pytest.raises(PubanonError, match='visibility checked'):
        _lifecycle.repo_create(paths, global_config, project, yes=True)

    assert requirements == [('anonymous-account', 'configuration')]


def test_repo_create_yes_does_not_prompt(
    fake_gh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project = _configs(tmp_path)
    remote = tmp_path / 'host.git'
    subprocess.run(['git', 'init', '--bare', '-q', remote], check=True, env=git_env())
    monkeypatch.setattr(_host, '_URL_TEMPLATE', remote.as_uri())

    def _unexpected_prompt(_prompt: str) -> str:
        pytest.fail('repo create --yes requested confirmation')

    monkeypatch.setattr('builtins.input', _unexpected_prompt)
    _responses(fake_gh, _success_responses())

    _lifecycle.repo_create(paths, global_config, project, yes=True)

    assert _CREATE_ARGS in gh_calls(tmp_path)


def test_reveal_ci_workflow_failure_retains_private_visibility_and_configuration(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project, destination, _commit = _published_fixture(tmp_path, make_repo)
    project = replace(project, ci_workflow='ci.yml')
    _config.save_project(paths, project)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', destination.url)
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('private'),
        ],
    )

    def _evidence(*_args: object, **_kwargs: object) -> RunEvidence:
        return RunEvidence('unacceptable', None, (), 'conclusion: failure')

    monkeypatch.setattr(_lifecycle, 'run_evidence', _evidence)

    with pytest.raises(PubanonError, match='CI run is not acceptable'):
        _lifecycle.reveal(paths, global_config, project)

    assert _SET_VISIBILITY_ARGS not in gh_calls(tmp_path)
    assert _config.load_project(paths, project.name) == project


@pytest.mark.parametrize('waited', [False, True])
def test_reveal_convergence_reports_a_failure_that_is_not_a_read_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, waited: bool
) -> None:
    paths, global_config, project = _configs(tmp_path)
    drift = PubanonError(
        'the repository is now public but post-flip verification failed',
        fact='published refs changed after the visibility flip',
        recovery='fix the reported drift, then rerun pubanon repo reveal',
    )
    results: list[PubanonError | str] = [drift, 'release-commit']
    if waited:
        results.insert(0, _post_flip_unread())
    clock, saved = _scripted_post_flip(monkeypatch, results)

    with pytest.raises(PubanonError) as error:
        _CONVERGE_PUBLIC(paths, global_config, project, host_changed=True)

    assert error.value is drift
    assert clock.sleeps == ([5] if waited else [])
    assert saved == []


@pytest.mark.parametrize('yes', [False, True])
def test_repo_create_accepts_matching_empty_repository_without_mutation(
    fake_gh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    yes: bool,
) -> None:
    paths, global_config, project = _configs(tmp_path)
    assert project.settings is not None
    remote = tmp_path / 'host.git'
    subprocess.run(['git', 'init', '--bare', '-q', remote], check=True, env=git_env())
    monkeypatch.setattr(_host, '_URL_TEMPLATE', remote.as_uri())
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('private'),
            _settings_response(replace(project.settings, default_branch='master')),
        ],
    )

    def _unexpected_prompt(_prompt: str) -> str:
        pytest.fail('checking an existing repository requested confirmation')

    monkeypatch.setattr('builtins.input', _unexpected_prompt)
    _lifecycle.repo_create(paths, global_config, project, yes=yes)

    output = capsys.readouterr().out
    assert 'Repository already exists and is empty' in output
    assert 'configuration matches' in output
    assert output.rstrip().endswith('Next: pubanon prepare')
    assert gh_calls(tmp_path) == [
        _ACCOUNT_ARGS,
        _ABSENCE_ARGS,
        _ACCOUNT_ARGS,
        _REPOSITORY_ARGS,
        _SETTINGS_ARGS,
    ]
    assert _git(remote, 'for-each-ref') == b''
    assert not _store.candidate_store_path(paths, project.name).exists()
    assert _records.load_candidate(paths, project.name) is None


@pytest.mark.parametrize(
    ('field', 'error'),
    [
        ('owner', 'repository owner did not pass content checks'),
        ('repo', 'repository name did not pass content checks'),
        ('settings.description', 'repository description did not pass content checks'),
    ],
)
def test_repo_create_gates_every_host_visible_string_before_host_contact(
    field: str,
    error: str,
    tmp_path: Path,
) -> None:
    paths, global_config, project = _configs(tmp_path)
    term = 'private-canary'
    global_config = replace(global_config, sensitive_terms=(term,))
    settings = Settings(
        description='Public repository',
        default_branch='main',
        issues=True,
        wiki=False,
        projects=True,
        pull_requests=False,
        discussions=False,
    )
    if field == 'owner':
        project = replace(project, owner=f'{term}-owner')
    elif field == 'repo':
        project = replace(project, repo=f'{term}-repo')
    elif field == 'settings.description':
        settings = replace(settings, description=f'{term} description')
    else:
        settings = replace(settings, default_branch=f'{term}-branch')
    project = replace(project, settings=settings)

    with pytest.raises(PubanonError, match=error) as exc_info:
        _lifecycle.repo_create(paths, global_config, project, yes=True)

    rendered = exc_info.value.render()
    assert field.rsplit('.', maxsplit=1)[-1].replace('_', ' ') in rendered
    assert term in rendered
    assert gh_calls(tmp_path) == []


@pytest.mark.parametrize(
    'case',
    [
        (ref, known)
        for ref in ('refs/heads/main', 'refs/heads/other', 'refs/tags/v1.0.0')
        for known in (False, True)
    ],
)
def test_repo_create_refuses_nonempty_existing_repository_with_relevant_recovery(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, bool],
) -> None:
    ref, known = case
    paths, global_config, project = _configs(tmp_path)
    source = make_repo(files={'README.md': 'existing\n'})
    tip = _git(source, 'rev-parse', 'HEAD').decode().strip()
    remote = tmp_path / 'host.git'
    subprocess.run(['git', 'init', '--bare', '-q', remote], check=True, env=git_env())
    _git(remote, 'fetch', '-q', source.as_uri(), f'{tip}:{ref}')
    before = _git(remote, 'for-each-ref')
    if known:
        store = _store.store_path(paths, project.name)
        _git(store, 'fetch', '-q', source.as_uri(), f'{tip}:refs/heads/main')
    monkeypatch.setattr(_host, '_URL_TEMPLATE', remote.as_uri())
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('private'),
        ],
    )

    with pytest.raises(PubanonError, match='already contains branches or tags') as exc_info:
        _lifecycle.repo_create(paths, global_config, project, yes=True)

    recovery = exc_info.value.recovery or ''
    if known:
        assert recovery == 'pubanon repo verify'
    else:
        assert 'empty repository for first publication' in recovery
        assert 'recovering published history' in recovery
        assert 'pubanon repo restore --to local' in recovery
        assert 'pubanon repo verify' not in recovery
        assert not _store.candidate_store_path(paths, project.name).exists()
    assert gh_calls(tmp_path) == [_ACCOUNT_ARGS, _ABSENCE_ARGS, _ACCOUNT_ARGS, _REPOSITORY_ARGS]
    assert _git(remote, 'for-each-ref') == before


@pytest.mark.parametrize('failure', ['visibility', 'description', 'issues', 'settings-read'])
def test_repo_create_refuses_unverified_existing_repository_without_mutation(
    fake_gh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    paths, global_config, project = _configs(tmp_path)
    assert project.settings is not None
    remote = tmp_path / 'host.git'
    subprocess.run(['git', 'init', '--bare', '-q', remote], check=True, env=git_env())
    monkeypatch.setattr(_host, '_URL_TEMPLATE', remote.as_uri())
    settings = project.settings
    if failure == 'description':
        settings = replace(settings, description='Other description')
    elif failure == 'issues':
        settings = replace(settings, issues=False)
    response = _settings_response(settings)
    if failure == 'settings-read':
        response.update(returncode=1, stderr='cannot read settings\n')
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('public' if failure == 'visibility' else 'private'),
            response,
        ],
    )

    with pytest.raises(PubanonError) as exc_info:
        _lifecycle.repo_create(paths, global_config, project, yes=True)

    error = exc_info.value
    if failure in {'description', 'issues'}:
        assert str(error) == 'repository settings do not match configuration'
        assert failure in (error.fact or '')
        assert 'GitHub settings or saved configuration' in (error.recovery or '')
        assert 'pubanon repo create' in (error.recovery or '')
    elif failure == 'visibility':
        assert 'visibility' in error.render()
    else:
        assert 'cannot read settings' in error.render()
    calls = gh_calls(tmp_path)
    assert all(method not in call for call in calls for method in ('POST', 'PATCH', 'DELETE'))
    assert calls.count(_SETTINGS_ARGS) == (failure != 'visibility')
    assert _git(remote, 'for-each-ref') == b''
    assert not _store.candidate_store_path(paths, project.name).exists()


@pytest.mark.parametrize('failure', ['update', 'readback', 'mismatch'])
def test_repo_create_settings_failure_reports_created_repository(
    failure: str,
    fake_gh: Path,
    tmp_path: Path,
) -> None:
    paths, global_config, project = _configs(tmp_path)
    responses = _success_responses()
    for response in responses:
        if response['args'] == (_PATCH_SETTINGS_ARGS if failure == 'update' else _SETTINGS_ARGS):
            if failure == 'mismatch':
                response['stdout'] = json.dumps(
                    {
                        'description': 'Host override',
                        'default_branch': 'master',
                        'issues': False,
                        'wiki': True,
                        'projects': False,
                        'pull_requests': True,
                        'discussions': True,
                    }
                )
            else:
                response.update(returncode=1, stderr='settings transport failed\n')
    _responses(fake_gh, responses)

    with pytest.raises(PubanonError) as error:
        _lifecycle.repo_create(paths, global_config, project, yes=True)

    assert 'repository was created' in str(error.value)
    assert 'GitHub settings' in (error.value.recovery or '')
    assert 'pubanon repo create' in (error.value.recovery or '')
    assert error.value.actual is None
    if failure == 'mismatch':
        assert 'settings do not match' in str(error.value)
        for difference in (
            'description: expected Public repository; actual Host override',
            'issues: expected true; actual false',
            'wiki: expected false; actual true',
            'projects: expected true; actual false',
            'pull_requests: expected false; actual true',
            'discussions: expected false; actual true',
        ):
            assert difference in (error.value.fact or '')
        assert 'default_branch' not in (error.value.fact or '')
    else:
        assert 'could not be verified' in str(error.value)
        assert 'settings transport failed' in (error.value.fact or '')
        assert 'observed' not in (error.value.fact or '')
    calls = gh_calls(tmp_path)
    assert calls.count(_CREATE_ARGS) == 1
    assert calls.count(_PATCH_SETTINGS_ARGS) == 1
    assert calls.count(_SETTINGS_ARGS) == (0 if failure == 'update' else 1)


@pytest.mark.parametrize('ref', ['refs/tags/v1.0.0', 'refs/heads/private-backup'])
def test_repo_create_reports_partial_creation_when_remote_is_not_empty(
    ref: str,
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project = _configs(tmp_path)
    source = make_repo(files={'README.md': 'public\n'})
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
        ['git', '-C', remote, 'fetch', '-q', str(source), f'{commit}:{ref}'],
        check=True,
        env=git_env(),
    )
    monkeypatch.setattr(_host, '_URL_TEMPLATE', remote.as_uri())
    _responses(fake_gh, _success_responses())

    with pytest.raises(PubanonError) as exc_info:
        _lifecycle.repo_create(paths, global_config, project, yes=True)

    assert 'partial' in str(exc_info.value)
    assert exc_info.value.recovery == 'pubanon repo create'
    assert _CREATE_ARGS in gh_calls(tmp_path)


@pytest.mark.parametrize(
    'case',
    [
        (state, action, existing)
        for state, action in (
            (None, 'pubanon prepare'),
            ('retained', 'pubanon publish'),
            ('unknown-outcome', 'pubanon publish --resolve'),
        )
        for existing in (False, True)
    ],
)
def test_repo_create_routes_after_verifying_new_or_existing_empty_repository(
    fake_gh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_repo: Callable[..., Path],
    case: tuple[str | None, str, bool],
) -> None:
    candidate_state, next_action, existing = case
    paths, global_config, project = _configs(tmp_path)
    settings = Settings(
        description='Public repository',
        default_branch='main',
        issues=True,
        wiki=False,
        projects=True,
        pull_requests=False,
        discussions=False,
    )
    project = replace(project, settings=settings)
    candidate_before = None
    if candidate_state is not None:
        source = make_repo(files={'README.md': 'public\n'})
        pinned = _source.pin(source, 'HEAD')
        record = _candidate.build_candidate(
            paths,
            global_config,
            replace(project, allow=('**',)),
            _candidate.CandidateInputs(
                _source.read_snapshot(source, pinned.commit),
                pinned,
                None,
                None,
            ),
        )
        _candidate.save_candidate(
            paths,
            project.name,
            replace(
                record,
                state=candidate_state,
                fingerprint=_candidate.fingerprint(global_config, project),
            ),
        )
        candidate_before = _records.candidate_path(paths, project.name).read_bytes()
    remote = tmp_path / 'host.git'
    subprocess.run(['git', 'init', '--bare', '-q', remote], check=True, env=git_env())
    monkeypatch.setattr(_host, '_URL_TEMPLATE', remote.as_uri())
    create_args = _CREATE_ARGS
    responses: list[dict[str, object]] = _success_responses(create_args=create_args)
    if existing:
        responses = [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('private'),
            _settings_response(settings),
        ]
    _responses(fake_gh, responses)

    captured = StringIO()
    with redirect_stdout(captured):
        _lifecycle.repo_create(paths, global_config, project, yes=True)

    assert gh_calls(tmp_path) == (
        [
            _ACCOUNT_ARGS,
            _ABSENCE_ARGS,
            _ACCOUNT_ARGS,
            _REPOSITORY_ARGS,
            _SETTINGS_ARGS,
        ]
        if existing
        else [
            _ACCOUNT_ARGS,
            _ABSENCE_ARGS,
            create_args,
            _PATCH_SETTINGS_ARGS,
            _SETTINGS_ARGS,
            _ACCOUNT_ARGS,
            _REPOSITORY_ARGS,
        ]
    )
    output = captured.getvalue()
    if existing:
        assert 'Repository already exists and is empty' in output
        assert 'configuration matches' in output
    else:
        assert 'Create repository anonymous-account/public-repo' in output
        assert 'Visibility:' in output
        assert 'private' in output
        assert 'Description:' in output
        assert 'Public repository' in output
        assert 'Default branch:' in output
        assert 'Features:' in output
        assert 'pull requests, discussions, and wiki off' in output
        assert 'Created private repository anonymous-account/public-repo.' in output
    assert output.rstrip().endswith(f'Next: {next_action}')
    if candidate_before is not None:
        assert _records.candidate_path(paths, project.name).read_bytes() == candidate_before
    else:
        assert _records.load_candidate(paths, project.name) is None


@pytest.mark.parametrize('stage', ['actions', 'inventory', 'save', 'caches', 'final'])
def test_reveal_acceptance_is_not_saved_when_a_later_step_stops(
    stage: str,
    fake_gh: Path,
    published_reveal: _PublishedReveal,
    monkeypatch: pytest.MonkeyPatch,
    answers: _Script,
) -> None:
    term = 'historical-private-canary'
    paths, global_config, project, _destination, _commit = published_reveal
    project = replace(project, extra_terms=(term,))
    assert project.settings is not None
    _config.save_project(paths, project)
    responses = _disclosure_responses()
    if stage == 'actions':
        responses = []
    elif stage in {'inventory', 'caches'}:
        resource = 'artifacts' if stage == 'inventory' else 'caches'
        responses = [
            response
            for response in responses
            if f'/actions/{resource}?' not in str(response['args'])
        ]
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('private', once=True),
            _repository_response('private'),
            _settings_response(project.settings),
            *responses,
        ],
    )
    if stage == 'save':
        real_chmod = _filesystem.chmod

        # create_data_dirs also chmods directories, so fail only the disclosure file.
        def _fail_disclosure_chmod(path: Path, mode: int, *, follow_symlinks: bool = True) -> None:
            if path.name == 'disclosure.json':
                raise OSError('injected disclosure save failure')
            real_chmod(path, mode, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(_filesystem, 'chmod', _fail_disclosure_chmod)
    answers(
        {
            'actions': [],
            'inventory': ['y'],
            'save': ['y'],
            'caches': ['y'],
            'final': ['y', 'n'],
        }[stage]
    )

    with pytest.raises(PubanonError) as exc_info:
        _lifecycle.reveal(paths, global_config, project)

    assert isinstance(exc_info.value, OperationCanceled) is (stage == 'final')
    if stage == 'save':
        assert str(exc_info.value) == 'cannot write complete disclosure list'
    assert _config.load_project(paths, project.name) == project
    assert _SET_VISIBILITY_ARGS not in gh_calls(paths.config_dir.parent)


@pytest.mark.slow
def test_reveal_accepts_history_findings_then_saves_digest_with_visibility(
    fake_gh: Path,
    published_reveal: _PublishedReveal,
    capsys: pytest.CaptureFixture[str],
    answers: _Script,
) -> None:
    term = 'historical-private-canary'
    paths, global_config, project, _destination, commit = published_reveal
    project = replace(project, extra_terms=(term,))
    assert project.settings is not None
    _config.save_project(paths, project)
    pre_settings = _settings_response(project.settings)
    pre_settings['once'] = True
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('private', once=True),
            _repository_response('private', once=True),
            pre_settings,
            *_disclosure_responses(),
            {'args': _SET_VISIBILITY_ARGS},
            _repository_response('public'),
            _settings_response(project.settings),
        ],
    )
    answers(
        [
            ('Make these public with the repository? [y/n] ', 'y'),
            ('Make it public? [y/n] ', 'y'),
        ]
    )

    _lifecycle.reveal(paths, global_config, project)

    saved = _config.load_project(paths, project.name)
    finding = _audit.AuditFinding(f'{commit[:12]}:message', RuleClass.IDENTITY_CRITICAL, term)
    assert saved.visibility == 'public'
    assert saved.acknowledged_exposure == _audit.exposure_digest((finding,))
    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'Published history: matches GitHub' in output
    assert 'findings listed below' in output
    assert 'Sensitive terms in published history: 1 term in 1 location' in output
    assert 'public publishes these matches' in output
    assert output.index('Sensitive terms in published history') < output.index('Not checked:')


@pytest.mark.slow
@pytest.mark.parametrize('replaced', [False, True])
def test_reveal_convergence_save_failure_reruns_from_saved_state(
    replaced: bool,
    fake_gh: Path,
    published_reveal: _PublishedReveal,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    term = 'historical-private-canary'
    paths, global_config, project, _destination, commit = published_reveal
    project = replace(project, extra_terms=(term,))
    assert project.settings is not None
    _config.save_project(paths, project)
    pre_settings = _settings_response(project.settings)
    pre_settings['once'] = True
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('private', once=True),
            _repository_response('private', once=True),
            pre_settings,
            *_disclosure_responses(),
            {'args': _SET_VISIBILITY_ARGS},
            _repository_response('public'),
            _settings_response(project.settings),
        ],
    )
    finding = _audit.AuditFinding(f'{commit[:12]}:message', RuleClass.IDENTITY_CRITICAL, term)
    digest = _audit.exposure_digest((finding,))
    real_save = _lifecycle.save_project

    def _failing_save(owned_paths: Paths, owned_project: ProjectConfig) -> None:
        if replaced:
            real_save(owned_paths, owned_project)
            raise PubanonError('project configuration may have been saved')
        raise PubanonError('cannot save project configuration')

    prompts: list[str] = []

    def _accept(prompt: str) -> str:
        prompts.append(prompt)
        return 'y'

    monkeypatch.setattr(_lifecycle, 'save_project', _failing_save)
    monkeypatch.setattr('builtins.input', _accept)
    with pytest.raises(PubanonError, match='configuration update failed'):
        _lifecycle.reveal(paths, global_config, project)
    monkeypatch.setattr(_lifecycle, 'save_project', real_save)

    saved = _config.load_project(paths, project.name)
    if replaced:
        assert (saved.visibility, saved.acknowledged_exposure) == ('public', digest)
    else:
        assert saved == project
    prompts.clear()
    capsys.readouterr()

    _lifecycle.reveal(paths, global_config, saved)

    final = _config.load_project(paths, project.name)
    assert (final.visibility, final.acknowledged_exposure) == ('public', digest)
    assert gh_calls(paths.config_dir.parent).count(_SET_VISIBILITY_ARGS) == 1
    if replaced:
        assert prompts == []
        assert 'is already public.' in capsys.readouterr().out
    else:
        assert prompts[0] == 'Record the repository as public with this history? [y/n] '


def test_reveal_convergence_saves_accepted_exposure_with_visibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, global_config, project = _configs(tmp_path)
    saved: list[ProjectConfig] = []

    def _verify(_paths: Paths, _g: GlobalConfig, _p: ProjectConfig) -> str:
        return 'release-commit'

    def _save(_paths: Paths, public_project: ProjectConfig) -> None:
        saved.append(public_project)

    monkeypatch.setattr(_lifecycle, '_verify_after_flip', _verify)
    monkeypatch.setattr(_lifecycle, 'save_project', _save)

    _CONVERGE_PUBLIC(paths, global_config, project, host_changed=True, accepted_exposure='d' * 64)

    assert saved == [replace(project, visibility='public', acknowledged_exposure='d' * 64)]


def test_reveal_convergence_starts_no_attempt_after_an_oversleeping_pause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, global_config, project = _configs(tmp_path)
    clock, _saved = _scripted_post_flip(
        monkeypatch, [_post_flip_unread() for _ in range(40)], oversleep=2
    )

    with pytest.raises(PubanonError, match='GitHub may still be applying the change'):
        _CONVERGE_PUBLIC(paths, global_config, project, host_changed=True)

    assert clock.attempts[-1] == 175
    assert clock.now == 182


def test_reveal_convergence_stops_waiting_after_three_minutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, global_config, project = _configs(tmp_path)
    clock, saved = _scripted_post_flip(monkeypatch, [_post_flip_unread() for _ in range(40)])

    with pytest.raises(PubanonError) as error:
        _CONVERGE_PUBLIC(paths, global_config, project, host_changed=True)

    rendered = str(error.value)
    assert 'repository is now public' in rendered
    assert 'GitHub may still be applying the change' in rendered
    assert clock.sleeps == [5] * 36
    assert error.value.fact is not None
    assert 'could not read the repository back' in error.value.fact
    assert 'within 3 minutes' in error.value.fact
    assert 'Your repository is disabled.' in (error.value.fact or '')
    assert error.value.recovery is not None
    assert 'wait a minute' in error.value.recovery
    assert 'pubanon repo reveal' in error.value.recovery
    assert clock.attempts[-1] == 180
    assert saved == []


@pytest.mark.parametrize(
    'host_changed',
    [
        True,
        False,
    ],
)
def test_reveal_convergence_tail_runs_the_same_ordered_steps(
    host_changed: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, global_config, project = _configs(tmp_path)
    steps: list[str] = []

    def _verify(
        _paths: Paths,
        _g: GlobalConfig,
        _p: ProjectConfig,
    ) -> str:
        steps.append('verify public remote')
        return 'release-commit'

    def _save(_paths: Paths, public_project: ProjectConfig) -> None:
        assert public_project == replace(project, visibility='public')
        steps.append('save public project')

    monkeypatch.setattr(_lifecycle, '_verify_after_flip', _verify)
    monkeypatch.setattr(_lifecycle, 'save_project', _save)

    _CONVERGE_PUBLIC(paths, global_config, project, host_changed=host_changed)

    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert steps == ['verify public remote', 'save public project']
    assert (
        'Made anonymous-account/public-repo public'
        if host_changed
        else 'Recorded public visibility for anonymous-account/public-repo'
    ) in output
    assert 'earlier Actions history and logs are now public' not in output
    assert 'verified commit release-commit' not in output
    with verbose_context(enabled=True):
        _CONVERGE_PUBLIC(paths, global_config, project, host_changed=host_changed)
    assert 'Verified commit: release-commit' in capsys.readouterr().out


@pytest.mark.parametrize(
    'host_changed',
    [
        True,
        False,
    ],
)
def test_reveal_convergence_tail_wraps_save_failure_before_either_completion_report(
    host_changed: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, global_config, project = _configs(tmp_path)

    def _verify(
        _paths: Paths,
        _g: GlobalConfig,
        _p: ProjectConfig,
    ) -> str:
        print('trace: verify public remote')
        return 'release-commit'

    def _fail_save(_paths: Paths, _public_project: ProjectConfig) -> None:
        raise PubanonError('simulated configuration write failure')

    monkeypatch.setattr(_lifecycle, '_verify_after_flip', _verify)
    monkeypatch.setattr(_lifecycle, 'save_project', _fail_save)

    with pytest.raises(PubanonError) as exc_info:
        _CONVERGE_PUBLIC(paths, global_config, project, host_changed=host_changed)

    error = exc_info.value
    rendered = str(error)
    assert 'repository is public and verified' in rendered
    assert 'configuration update failed' in rendered
    assert error.fact == 'error: simulated configuration write failure'
    assert error.recovery is not None
    assert 'fix the configuration write' in error.recovery
    assert 'pubanon repo reveal' in error.recovery
    assert capsys.readouterr().out.splitlines() == ['trace: verify public remote']


@pytest.mark.parametrize('required', [(), ('build', 'lint')])
def test_reveal_checks_judge_ci_workflow_run_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, required: tuple[str, ...]
) -> None:
    _paths, _global, project = _configs(tmp_path)
    project = replace(project, ci_workflow='ci.yml', required_checks=required)
    commit = 'a' * 40
    calls: list[tuple[tuple[str, str], RunQuery, tuple[str, ...]]] = []

    def _evidence(
        coordinates: tuple[str, str], query: RunQuery, *, required: tuple[str, ...]
    ) -> RunEvidence:
        calls.append((coordinates, query, required))
        return RunEvidence('success', None, (), '')

    monkeypatch.setattr(_lifecycle, 'run_evidence', _evidence)

    assert _lifecycle._verify_checks(project, commit) == 'passed (latest run of ci.yml)'
    assert calls == [((project.owner, project.repo), RunQuery('ci.yml', commit, 'push'), required)]


@pytest.mark.parametrize('state', ['pending', 'missing', 'ambiguous', 'unacceptable'])
def test_reveal_checks_refuse_unsuccessful_ci_workflow_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: _host.CheckState
) -> None:
    _paths, _global, project = _configs(tmp_path)
    project = replace(project, ci_workflow='ci.yml')
    detail = 'conclusion: failure\x1b[31m' if state == 'unacceptable' else ''

    def _evidence(*_args: object, **_kwargs: object) -> RunEvidence:
        return RunEvidence(state, None, (), detail)

    monkeypatch.setattr(_lifecycle, 'run_evidence', _evidence)

    with pytest.raises(PubanonError, match='CI run is not acceptable') as error:
        _lifecycle._verify_checks(project, 'a' * 40)

    assert error.value.fact == (escape(detail) if detail else state)
    assert error.value.recovery is not None
    assert 'pubanon repo watch' in error.value.recovery
    assert 'pubanon repo reveal' in error.value.recovery


def test_reveal_convergence_waits_for_github_after_its_own_flip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    paths, global_config, project = _configs(tmp_path)
    clock, saved = _scripted_post_flip(
        monkeypatch, [_post_flip_unread(), _post_flip_unread(), 'release-commit']
    )

    with verbose_context(enabled=True):
        _CONVERGE_PUBLIC(paths, global_config, project, host_changed=True)

    assert clock.sleeps == [5, 5]
    assert saved == [replace(project, visibility='public')]
    out = capsys.readouterr().out
    assert out.count('could not read it back yet') == 1
    assert 'GitHub may still be applying the change' in out
    assert 'Retrying for up to 3 minutes' in out
    assert 'After 5 s: Git ls-remote failed' in out
    assert 'Made anonymous-account/public-repo public.' in out


def test_reveal_ctrl_c_while_waiting_after_the_flip_reports_interruption(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project = _reveal_with_unreadable_remote(
        fake_gh, tmp_path, make_repo, monkeypatch
    )

    def _interrupt(_seconds: float) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(_lifecycle, '_sleep', _interrupt)

    with pytest.raises(OperationCanceled) as error:
        _lifecycle.reveal(paths, global_config, project)

    assert str(error.value) == _lifecycle._INTERRUPTED
    assert _config.load_project(paths, project.name).visibility == 'private'


def test_reveal_declined_final_confirmation_does_not_flip_or_record(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project, destination, _commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    assert project.settings is not None
    _config.save_project(paths, project)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', destination.url)
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('private', once=True),
            _repository_response('private'),
            _settings_response(project.settings),
            *_disclosure_responses(),
        ],
    )
    answers = iter(['n'])

    def _answer(_prompt: str) -> str:
        return next(answers)

    monkeypatch.setattr('builtins.input', _answer)

    with pytest.raises(OperationCanceled, match=r'Canceled\. Nothing was changed.'):
        _lifecycle.reveal(paths, global_config, project)

    assert _SET_VISIBILITY_ARGS not in gh_calls(tmp_path)
    assert _config.load_project(paths, project.name).visibility == 'private'


@pytest.mark.slow
def test_reveal_fresh_flips_once_then_verifies_and_records_public_intent(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, global_config, project, destination, commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    assert project.settings is not None
    _config.save_project(paths, project)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', destination.url)
    pre_settings = _settings_response(project.settings)
    pre_settings['once'] = True
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('private', once=True),
            _repository_response('private', once=True),
            pre_settings,
            *_disclosure_responses(),
            {'args': _SET_VISIBILITY_ARGS},
            _repository_response('public'),
            _settings_response(project.settings),
        ],
    )
    prompts: list[str] = []
    run_reads_at_prompt: list[int] = []

    def _accept(prompt: str) -> str:
        prompts.append(prompt)
        run_reads_at_prompt.append(
            sum('/actions/runs?' in ' '.join(call) for call in gh_calls(tmp_path))
        )
        return 'y'

    convergence_calls: list[bool] = []
    converge_public = _CONVERGE_PUBLIC

    def _record_convergence(
        owned_paths: Paths,
        owned_global: GlobalConfig,
        owned_project: ProjectConfig,
        *,
        host_changed: bool,
        accepted_exposure: str = '',
    ) -> None:
        convergence_calls.append(host_changed)
        converge_public(
            owned_paths,
            owned_global,
            owned_project,
            host_changed=host_changed,
            accepted_exposure=accepted_exposure,
        )

    monkeypatch.setattr('builtins.input', _accept)
    monkeypatch.setattr(_lifecycle, '_converge_public', _record_convergence)

    offers: list[str] = []

    def _offer(owned_project: ProjectConfig, destination: _host.Destination) -> None:
        assert _config.load_project(paths, owned_project.name).visibility == 'public'
        offers.append(destination.repo)

    monkeypatch.setattr(_immutable, 'offer', _offer)

    _lifecycle.reveal(paths, global_config, project)

    assert offers == ['public-repo']

    calls = gh_calls(tmp_path)
    assert len(run_reads_at_prompt) == 1
    assert sum('/actions/runs?' in ' '.join(call) for call in calls) == run_reads_at_prompt[0] + 1
    assert calls.count(_SET_VISIBILITY_ARGS) == 1
    assert calls.index(_SET_VISIBILITY_ARGS) > calls.index(
        ['api', 'repos/anonymous-account/public-repo/actions/artifacts?per_page=100&page=1']
    )
    assert calls[calls.index(_SET_VISIBILITY_ARGS) + 1] == _REPOSITORY_ARGS
    assert _config.load_project(paths, project.name).visibility == 'public'
    assert prompts == ['Make it public? [y/n] ']
    runs_args = ['api', 'repos/anonymous-account/public-repo/actions/runs?per_page=100&page=1']
    last_runs_read = max(index for index, call in enumerate(calls) if call == runs_args)
    assert last_runs_read < calls.index(_SET_VISIBILITY_ARGS)
    assert convergence_calls == [True]
    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'earlier Actions history and logs are now public' not in output
    assert output.startswith('Make anonymous-account/public-repo public\n')
    assert 'Published history:' in output
    assert 'Not checked:' in output
    assert 'Made anonymous-account/public-repo public.' in output
    assert 'Repository is public and verified.' not in output
    assert f'verified commit {commit}' not in output
    assert 'Next:' not in output


@pytest.mark.parametrize('field', ['owner', 'repo'])
def test_reveal_gates_current_coordinates_before_host_contact(
    field: str,
    tmp_path: Path,
) -> None:
    paths, global_config, project = _configs(tmp_path)
    term = f'private-{field}'
    global_config = replace(global_config, sensitive_terms=(term,))
    project = replace(project, **{field: f'public-{term}'})

    label = 'name' if field == 'repo' else field
    with pytest.raises(
        PubanonError, match=f'repository {label} did not pass content checks'
    ) as exc_info:
        _lifecycle.reveal(paths, global_config, project)

    assert term in exc_info.value.render()
    assert gh_calls(tmp_path) == []


@pytest.mark.parametrize('accept', [False, True])
def test_reveal_already_public_host_asks_about_published_history(
    accept: bool,
    fake_gh: Path,
    published_reveal: _PublishedReveal,
    capsys: pytest.CaptureFixture[str],
    answers: _Script,
) -> None:
    term = 'historical-private-canary'
    paths, global_config, project, _destination, _commit = published_reveal
    project = replace(project, hygiene_terms=(term,))
    assert project.settings is not None
    _config.save_project(paths, project)
    settings = _settings_response(project.settings)
    settings['once'] = True
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('public', once=True),
            settings,
            *_disclosure_responses(),
            _repository_response('public'),
            _settings_response(project.settings),
        ],
    )
    record = 'Record the repository as public with this history? [y/n] '

    if accept:
        answers(
            [
                (record, 'y'),
                ('Record anonymous-account/public-repo as public? [y/n] ', 'y'),
            ]
        )
        _lifecycle.reveal(paths, global_config, project)
        saved = _config.load_project(paths, project.name)
        assert saved.visibility == 'public'
        assert saved.acknowledged_exposure == ''
    else:
        answers([(record, 'n')])
        with pytest.raises(OperationCanceled) as exc_info:
            _lifecycle.reveal(paths, global_config, project)
        assert str(exc_info.value) == 'Canceled. Nothing was changed.'
        assert _config.load_project(paths, project.name) == project

    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'content is already public' in output
    assert 'Policy terms in published history: 1 term in 1 location' in output
    assert 'These matches are already public.' in output
    assert _SET_VISIBILITY_ARGS not in gh_calls(paths.config_dir.parent)


def test_reveal_interrupted_after_the_visibility_change_says_it_may_be_public(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project, destination, commit = _published_fixture(tmp_path, make_repo)
    _config.save_project(paths, project)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', destination.url)
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('private'),
            {'args': _SET_VISIBILITY_ARGS},
        ],
    )

    def _verified(*_args: object, **_kwargs: object) -> tuple[str, str]:
        return commit, ''

    def _interrupt(*_args: object, **_kwargs: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(_lifecycle, '_verify_before_flip', _verified)
    monkeypatch.setattr(_lifecycle, '_converge_public', _interrupt)

    with pytest.raises(OperationCanceled) as error:
        _lifecycle.reveal(paths, global_config, project)

    rendered = str(error.value)
    assert 'Interrupted' in rendered
    assert 'repository may already be public' in rendered
    assert 'pubanon repo reveal' in rendered
    assert _config.load_project(paths, project.name).visibility == 'private'


def test_reveal_package_timeout_refuses_without_visibility_mutation(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, global_config, project, destination, commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    project = replace(
        project,
        package=PackageObservation('pypi', 'demo', ('Release',)),
    )
    _config.save_project(paths, project)
    store = _store.store_path(paths, project.name)
    _git(store, 'update-ref', 'refs/tags/v1.2.3', commit)
    _git(
        tmp_path / 'host.git',
        'update-ref',
        'refs/tags/v1.2.3',
        commit,
    )
    monkeypatch.setattr(_host, '_URL_TEMPLATE', destination.url)
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('private', once=True),
            _repository_response('private'),
        ],
    )
    observations: list[str] = []

    def _checks(*_args: object) -> list[CheckVerdict]:
        observations.append('checks')
        return [CheckVerdict('Release', 'success', 'success')]

    def _timeout(*_args: object) -> _package.PackageRelease | None:
        observations.append('release')
        raise ObservationTimeoutError

    def _unexpected_prompt(_prompt: str) -> str:
        pytest.fail('package timeout reached a reveal confirmation')

    monkeypatch.setattr(_watch, 'ci_verdicts', _checks)
    monkeypatch.setattr(_package, 'release_state', _timeout)
    monkeypatch.setattr('builtins.input', _unexpected_prompt)

    with pytest.raises(PubanonError) as exc_info:
        _lifecycle.reveal(paths, global_config, project)

    error = exc_info.value
    assert str(error) == 'package release observation timed out'
    assert error.fact is None
    assert error.actual is None
    assert error.expected is None
    assert error.recovery is not None
    assert 'pubanon repo reveal' in error.recovery
    assert 'retry package observation' in error.recovery
    assert 'private timeout' not in error.render()
    assert observations == ['checks', 'release']
    assert _SET_VISIBILITY_ARGS not in gh_calls(tmp_path)
    assert _config.load_project(paths, project.name).visibility == 'private'
    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'Repository is public and verified.' not in output
    assert 'local configuration now records public visibility' not in output


@pytest.mark.slow
def test_reveal_patch_failure_reports_unknown_outcome_then_resumes_without_patch(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project, destination, _commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    assert project.settings is not None
    _config.save_project(paths, project)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', destination.url)
    first_settings = _settings_response(project.settings)
    first_settings['once'] = True
    resume_settings = _settings_response(project.settings)
    resume_settings['once'] = True
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('private', once=True),
            _repository_response('private', once=True),
            _repository_response('public', once=True),
            _repository_response('public'),
            first_settings,
            resume_settings,
            _settings_response(project.settings),
            *_disclosure_responses(),
            {
                'args': _SET_VISIBILITY_ARGS,
                'returncode': 1,
                'stderr': 'simulated PATCH failure\n',
                'once': True,
            },
        ],
    )

    def _accept(_prompt: str) -> str:
        return 'y'

    monkeypatch.setattr('builtins.input', _accept)

    with pytest.raises(PubanonError) as exc_info:
        _lifecycle.reveal(paths, global_config, project)

    rendered = str(exc_info.value)
    assert 'repository visibility outcome is unknown' in rendered
    assert 'repository may already be public' in rendered
    assert exc_info.value.recovery == 'rerun pubanon repo reveal'
    assert _config.load_project(paths, project.name).visibility == 'private'
    assert gh_calls(tmp_path).count(_SET_VISIBILITY_ARGS) == 1

    _lifecycle.reveal(paths, global_config, project)

    assert gh_calls(tmp_path).count(_SET_VISIBILITY_ARGS) == 1
    assert _config.load_project(paths, project.name).visibility == 'public'


@pytest.mark.slow
def test_reveal_post_flip_branch_drift_is_reported_and_keeps_private_intent(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    term = 'private-canary'
    paths, global_config, project, destination, commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    assert project.settings is not None
    _config.save_project(paths, project)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', destination.url)
    pre_settings = _settings_response(project.settings)
    pre_settings['once'] = True
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('private', once=True),
            _repository_response('private', once=True),
            pre_settings,
            *_disclosure_responses(),
            {'args': _SET_VISIBILITY_ARGS},
            _repository_response('public'),
        ],
    )

    def _accept(prompt: str) -> str:
        assert prompt == 'Make it public? [y/n] '
        host = tmp_path / 'host.git'
        _git(host, 'update-ref', f'refs/heads/{term}', commit)
        return 'y'

    monkeypatch.setattr('builtins.input', _accept)

    with pytest.raises(PubanonError) as exc_info:
        _lifecycle.reveal(paths, global_config, project)

    rendered = exc_info.value.render()
    assert 'repository is now public' in rendered
    assert 'post-flip verification failed' in rendered
    assert term in rendered
    assert _config.load_project(paths, project.name).visibility == 'private'
    assert gh_calls(tmp_path).count(_SET_VISIBILITY_ARGS) == 1


@pytest.mark.slow
def test_reveal_post_flip_settings_drift_resumes_without_second_patch(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project, destination, _commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    assert project.settings is not None
    _config.save_project(paths, project)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', destination.url)
    pre_settings = _settings_response(project.settings)
    pre_settings['once'] = True
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('private', once=True),
            _repository_response('private', once=True),
            pre_settings,
            *_disclosure_responses(),
            {'args': _SET_VISIBILITY_ARGS},
            _repository_response('public'),
            _settings_response(replace(project.settings, default_branch='trunk')),
        ],
    )

    def _accept(_prompt: str) -> str:
        return 'y'

    monkeypatch.setattr('builtins.input', _accept)

    with pytest.raises(PubanonError) as exc_info:
        _lifecycle.reveal(paths, global_config, project)

    rendered = str(exc_info.value)
    assert 'repository is now public' in rendered
    assert 'post-flip verification failed' in rendered
    assert _config.load_project(paths, project.name).visibility == 'private'

    resume_settings = _settings_response(project.settings)
    resume_settings['once'] = True
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('public', once=True),
            resume_settings,
            *_disclosure_responses(),
            _repository_response('public'),
            _settings_response(project.settings),
        ],
    )

    _lifecycle.reveal(paths, global_config, project)

    assert gh_calls(tmp_path).count(_SET_VISIBILITY_ARGS) == 1
    assert _config.load_project(paths, project.name).visibility == 'public'


@pytest.mark.slow
def test_reveal_prints_unchecked_statement_and_review_links(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    term = 'private-canary'
    private_root = tmp_path / term
    private_root.mkdir()
    paths, global_config, project, destination, _commit = _published_fixture(
        private_root,
        make_repo,
    )
    assert project.settings is not None
    _config.save_project(paths, project)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', destination.url)
    pre_settings = _settings_response(project.settings)
    pre_settings['once'] = True
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('private', once=True),
            _repository_response('private', once=True),
            pre_settings,
            *_disclosure_responses(('1 build',), ('201 wheel',)),
            {'args': _SET_VISIBILITY_ARGS},
            _repository_response('public'),
            _settings_response(project.settings),
        ],
    )

    def _accept(_prompt: str) -> str:
        return 'y'

    monkeypatch.setattr('builtins.input', _accept)

    _lifecycle.reveal(paths, global_config, project)

    output_lines = re.sub(r': +', ': ', capsys.readouterr().out).splitlines()
    assert not any('Saved inventory:' in line for line in output_lines)
    unchecked = next(line for line in output_lines if line.startswith('Not checked:'))
    assert re.search(r'\b1 artifact\b', unchecked)
    assert 'job summaries and annotations' in unchecked
    assert 'earlier pushes on the Activity page' in unchecked
    assert '  Actions: https://github.com/anonymous-account/public-repo/actions' in output_lines
    assert '  Activity: https://github.com/anonymous-account/public-repo/activity' in output_lines
    assert not any('actions/runs/1' in line for line in output_lines)


@pytest.mark.parametrize('exposure', ['Anonymous Publisher', 'anonymous@example.invalid'])
def test_reveal_lists_newly_sensitive_anonymous_identity_and_decline_changes_nothing(
    fake_gh: Path,
    published_reveal: _PublishedReveal,
    capsys: pytest.CaptureFixture[str],
    answers: _Script,
    exposure: str,
) -> None:
    paths, global_config, project, _destination, commit = published_reveal
    project = replace(project, extra_terms=(exposure,))
    assert project.settings is not None
    _config.save_project(paths, project)
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('private'),
            _settings_response(project.settings),
            *_disclosure_responses(),
        ],
    )
    answers([('Make these public with the repository? [y/n] ', 'n')])

    with pytest.raises(OperationCanceled, match=r'Canceled\. Nothing was changed\.'):
        _lifecycle.reveal(paths, global_config, project)

    output = capsys.readouterr().out
    assert f'First published in commit {commit[:12]}:' in output
    for field in ('author', 'committer'):
        assert re.search(rf'^    commit {field} +{re.escape(exposure)}$', output, re.MULTILINE)
    assert '  Rules: project extra_terms entry 1' in output
    calls = gh_calls(paths.config_dir.parent)
    assert _SET_VISIBILITY_ARGS not in calls
    assert any('/actions/runs?' in ' '.join(call) for call in calls)
    assert not any('/actions/artifacts?' in ' '.join(call) for call in calls)
    assert _config.load_project(paths, project.name) == project


def test_reveal_public_config_private_host_refuses_repo_verify_recovery(
    fake_gh: Path,
    tmp_path: Path,
) -> None:
    paths, global_config, project = _configs(tmp_path)
    project = replace(project, visibility='public')
    _config.save_project(paths, project)
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('private'),
        ],
    )

    with pytest.raises(PubanonError) as exc_info:
        _lifecycle.reveal(paths, global_config, project)

    assert str(exc_info.value) == (
        'declared visibility is public but the final repository is private'
    )
    assert exc_info.value.recovery is not None
    assert 'make the repository public' in exc_info.value.recovery
    assert 'visibility = "private"' in exc_info.value.recovery
    assert (
        escape((paths.config_dir / 'projects' / f'{project.name}.toml').as_posix())
        in exc_info.value.recovery
    )
    assert 'pubanon repo verify' not in exc_info.value.recovery
    assert gh_calls(tmp_path) == [_ACCOUNT_ARGS, _REPOSITORY_ARGS]


def test_reveal_public_config_public_host_is_idempotent(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, global_config, project, destination, _commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    project = replace(project, visibility='public')
    assert project.settings is not None
    _config.save_project(paths, project)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', destination.url)
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('public', once=True),
            _repository_response('public'),
            _settings_response(project.settings),
        ],
    )

    offers: list[str] = []

    def _offer(_project: ProjectConfig, destination: _host.Destination) -> None:
        offers.append(destination.repo)

    monkeypatch.setattr(_immutable, 'offer', _offer)

    _lifecycle.reveal(paths, global_config, project)

    assert offers == []

    assert _SET_VISIBILITY_ARGS not in gh_calls(tmp_path)
    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'anonymous-account/public-repo is already public' in output
    assert 'Verified now: public visibility' in output
    assert 'published branches and tags' in output
    assert 'repository settings' in output
    assert 'No local or host state was changed.' not in output
    assert 'Repository is already public and verified.' not in output
    assert 'Prior Actions history and logs are already public.' not in output
    assert 'Next: pubanon audit --remote' in output
    assert output.index('Next:') > output.index('Verified now:')


def test_reveal_read_failure_right_after_the_flip_says_github_may_still_be_applying_it(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project = _reveal_with_unreadable_remote(
        fake_gh, tmp_path, make_repo, monkeypatch
    )
    clock = _PostFlipClock()
    monkeypatch.setattr(_lifecycle, '_monotonic', clock.monotonic)
    monkeypatch.setattr(_lifecycle, '_sleep', clock.sleep)

    with pytest.raises(PubanonError) as error:
        _lifecycle.reveal(paths, global_config, project)

    rendered = str(error.value)
    assert 'repository is now public' in rendered
    assert 'GitHub may still be applying the change' in rendered
    assert error.value.fact is not None
    assert 'could not read the repository back' in error.value.fact
    assert 'within 3 minutes' in error.value.fact
    assert '  exit status 128: remote: Your repository is disabled.\n  fatal: 403' in (
        error.value.fact or ''
    )
    assert error.value.recovery is not None
    assert 'wait a minute' in error.value.recovery
    assert 'pubanon repo reveal' in error.value.recovery
    assert len(clock.sleeps) == 36
    assert _config.load_project(paths, project.name).visibility == 'private'


def test_reveal_refuses_a_sensitive_draft_release_page(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project, destination, _commit = _published_fixture(tmp_path, make_repo)
    project = replace(project, extra_terms=('later-sensitive',))
    assert project.settings is not None
    _config.save_project(paths, project)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', destination.url)
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('private'),
            _settings_response(project.settings),
        ],
    )
    page = _host.GitHubRelease(
        tag='v1.0.0',
        title='v1.0.0',
        notes='Thanks to later-sensitive.\n',
        url='https://github.com/anonymous-account/public-repo/releases/tag/untagged-1',
        draft=True,
        prerelease=False,
    )

    def _all_releases(_destination: _host.Destination) -> tuple[_host.GitHubRelease, ...]:
        return (page,)

    monkeypatch.setattr(_host, 'all_releases', _all_releases)

    with pytest.raises(PubanonError, match='release pages are blocked') as error:
        _lifecycle.reveal(paths, global_config, project)

    assert 'v1.0.0 (draft): notes line 1:' in (error.value.fact or '')
    assert _SET_VISIBILITY_ARGS not in gh_calls(tmp_path)
    assert _config.load_project(paths, project.name).visibility == 'private'


@pytest.mark.parametrize('field', ['tag', 'title', 'notes'])
def test_reveal_refuses_release_page_with_newly_sensitive_text_before_disclosure(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
) -> None:
    paths, global_config, project, destination, _commit = _published_fixture(
        tmp_path, make_repo, message='later-sensitive release'
    )
    project = replace(project, extra_terms=('later-sensitive',))
    assert project.settings is not None
    _config.save_project(paths, project)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', destination.url)
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('private'),
            _settings_response(project.settings),
        ],
    )
    page = _host.GitHubRelease(
        tag='later-sensitive' if field == 'tag' else 'v1.0.0',
        title='later-sensitive' if field == 'title' else 'v1.0.0',
        notes='Thanks to later-sensitive.\n' if field == 'notes' else '',
        url='https://github.com/anonymous-account/public-repo/releases/tag/v1.0.0',
        draft=False,
        prerelease=True,
    )

    def _all_releases(_destination: _host.Destination) -> tuple[_host.GitHubRelease, ...]:
        assert _destination == destination
        return (page,)

    monkeypatch.setattr(_host, 'all_releases', _all_releases)

    with pytest.raises(PubanonError, match='release pages are blocked') as error:
        _lifecycle.reveal(paths, global_config, project)

    location = 'notes line 1' if field == 'notes' else field
    assert f'{page.tag}: {location}:' in (error.value.fact or '')
    assert error.value.recovery is not None
    assert 'edit or delete the listed release pages' in error.value.recovery
    assert 'pubanon repo reveal' in error.value.recovery
    assert _SET_VISIBILITY_ARGS not in gh_calls(tmp_path)
    assert _config.load_project(paths, project.name).visibility == 'private'


def test_reveal_refuses_structural_history_before_disclosure_or_mutation(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project, destination, _commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    assert project.settings is not None
    _config.save_project(paths, project)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', destination.url)
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('private', once=True),
            _repository_response('private'),
            _settings_response(project.settings),
        ],
    )

    def _structural_audit(*_args: object, **_kwargs: object) -> _audit.HistoryAudit:
        return _audit.HistoryAudit(
            (), ('malformed commit header', 'tag:v1.0.0: tag target is outside main history'), ()
        )

    def _unexpected_prompt(_prompt: str) -> str:
        pytest.fail('structural reveal failure requested acknowledgment')

    monkeypatch.setattr(_audit, 'audit_history', _structural_audit)
    monkeypatch.setattr('builtins.input', _unexpected_prompt)

    with pytest.raises(PubanonError, match='published history has identity problems') as error:
        _lifecycle.reveal(paths, global_config, project)

    assert error.value.fact == (
        'malformed commit header\ntag:v1.0.0: tag target is outside main history'
    )
    assert error.value.recovery is not None
    assert 'pubanon audit' in error.value.recovery
    assert 'current identity' in error.value.recovery
    assert 'pubanon prepare --replace all' in error.value.recovery

    calls = gh_calls(tmp_path)
    assert _SET_VISIBILITY_ARGS not in calls
    assert not any('/actions/runs?' in ' '.join(call) for call in calls)
    assert not any('/actions/artifacts?' in ' '.join(call) for call in calls)
    assert _config.load_project(paths, project.name).visibility == 'private'


def test_reveal_resume_refuses_canonical_coordinate_drift_before_verification(
    fake_gh: Path,
    tmp_path: Path,
) -> None:
    term = 'private-canary'
    paths, global_config, project = _configs(tmp_path)
    assert project.settings is not None
    _config.save_project(paths, project)
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('public', repo=term),
        ],
    )

    with pytest.raises(PubanonError) as exc_info:
        _lifecycle.reveal(paths, global_config, project)

    error = exc_info.value
    assert str(error) == 'repository coordinates do not match configuration'
    assert error.recovery == 'pubanon repo verify'
    rendered = error.render()
    assert term in rendered
    assert _SET_VISIBILITY_ARGS not in gh_calls(tmp_path)
    assert gh_calls(tmp_path) == [_ACCOUNT_ARGS, _REPOSITORY_ARGS]
    assert _config.load_project(paths, project.name).visibility == 'private'


@pytest.mark.slow
def test_reveal_resumes_already_public_host_with_complete_read_only_verification(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, global_config, project, destination, commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    project = replace(project, required_checks=('build',))
    assert project.settings is not None
    _config.save_project(paths, project)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', destination.url)
    check_args = [
        'api',
        (
            f'repos/anonymous-account/public-repo/commits/{commit}/check-runs?'
            'check_name=build&filter=latest&per_page=100'
        ),
    ]
    settings = _settings_response(project.settings)
    settings['once'] = True
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('public', once=True),
            {
                'args': check_args,
                'stdout': json.dumps(
                    {
                        'total_count': 1,
                        'check_runs': [
                            {
                                'name': 'build',
                                'head_sha': commit,
                                'status': 'completed',
                                'conclusion': 'success',
                            }
                        ],
                    }
                ),
            },
            settings,
            *_disclosure_responses(),
            _repository_response('public'),
            _settings_response(project.settings),
        ],
    )
    prompts: list[str] = []
    run_reads_at_prompt: list[int] = []

    def _accept(prompt: str) -> str:
        prompts.append(prompt)
        run_reads_at_prompt.append(
            sum('/actions/runs?' in ' '.join(call) for call in gh_calls(tmp_path))
        )
        return 'y'

    monkeypatch.setattr('builtins.input', _accept)

    offers: list[str] = []

    def _offer(owned_project: ProjectConfig, destination: _host.Destination) -> None:
        assert _config.load_project(paths, owned_project.name).visibility == 'public'
        offers.append(destination.repo)

    monkeypatch.setattr(_immutable, 'offer', _offer)

    _lifecycle.reveal(paths, global_config, project)

    assert offers == ['public-repo']

    calls = gh_calls(tmp_path)
    assert len(run_reads_at_prompt) == 1
    assert sum('/actions/runs?' in ' '.join(call) for call in calls) == run_reads_at_prompt[0] + 1
    assert _SET_VISIBILITY_ARGS not in calls
    assert check_args in calls
    assert any('/actions/runs?' in ' '.join(call) for call in calls)
    assert any('/actions/artifacts?' in ' '.join(call) for call in calls)
    assert _config.load_project(paths, project.name).visibility == 'public'
    assert prompts == ['Record anonymous-account/public-repo as public? [y/n] ']
    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'Verify public repository anonymous-account/public-repo' in output
    assert 'content is already public' in output
    assert 'Recorded public visibility for anonymous-account/public-repo' in output
    assert 'already public' in output
    assert 'earlier Actions history and logs are now public' not in output
    assert f'verified commit {commit}' not in output


def test_reveal_routes_account_requirement_with_configuration_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project = _configs(tmp_path)
    requirements: list[tuple[str, str]] = []

    def _require_account(expected: str, *, authority: str) -> str:
        requirements.append((expected, authority))
        raise PubanonError('account authority observed')

    monkeypatch.setattr(_host, 'require_authenticated_account', _require_account)

    with pytest.raises(PubanonError, match='account authority observed'):
        _lifecycle.reveal(paths, global_config, project)

    assert requirements == [('anonymous-account', 'configuration')]


def test_reveal_unknown_outcome_refuses_before_host_contact(
    tmp_path: Path,
) -> None:
    paths, global_config, project = _configs(tmp_path)
    record = _records.CandidateRecord(
        commit='a' * 40,
        tree='b' * 40,
        snapshot_digest='c' * 64,
        tag_intent=None,
        fingerprint=_config.fingerprint(global_config, project),
        pinned=_source.PinnedSource('d' * 40, 'ref', 'refs/heads/main', 'd' * 40),
        destination=_records.RecordedDestination(
            project.host,
            project.owner,
            project.repo,
            project.visibility,
            global_config.accounts[project.host],
        ),
        prestate_main=None,
        prestate_tags={},
        state=_candidate.STATE_UNKNOWN_OUTCOME,
        message='Public snapshot',
    )
    _candidate.save_candidate(paths, project.name, record)

    with pytest.raises(
        PubanonError, match='the outcome of the last publication is unknown'
    ) as exc_info:
        _lifecycle.reveal(paths, global_config, project)

    assert exc_info.value.recovery == 'pubanon publish --resolve'
    assert gh_calls(tmp_path) == []


def test_settings_mismatches_reports_all_seven_fields_in_declared_order() -> None:
    expected = Settings(
        description='Public repository',
        default_branch='main',
        issues=True,
        wiki=False,
        projects=True,
        pull_requests=True,
        discussions=False,
    )
    actual = _host.SettingsFacts(
        description='',
        default_branch='trunk',
        issues=False,
        wiki=True,
        projects=False,
        pull_requests=False,
        discussions=True,
    )

    assert _lifecycle.settings_mismatches(expected, actual) == [
        'description: expected Public repository; actual ',
        'default_branch: expected main; actual trunk',
        'issues: expected true; actual false',
        'wiki: expected false; actual true',
        'projects: expected true; actual false',
        'pull_requests: expected true; actual false',
        'discussions: expected false; actual true',
    ]


def test_settings_refuse_non_main_before_host_mutation(tmp_path: Path) -> None:
    paths, global_config, project = _configs(tmp_path)
    project = replace(
        project,
        settings=Settings(
            description='Public repository',
            default_branch='trunk',
            issues=True,
            wiki=False,
            projects=False,
            pull_requests=False,
            discussions=False,
        ),
    )

    with pytest.raises(PubanonError, match=r'settings\.default_branch must be main') as exc_info:
        _lifecycle.repo_create(paths, global_config, project, yes=True)

    recovery = exc_info.value.recovery
    assert recovery is not None
    assert escape((paths.config_dir / 'projects' / 'demo.toml').as_posix()) in recovery
    assert gh_calls(tmp_path) == []


def test_verify_after_flip_accepts_public_repository_with_exact_refs_and_settings(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project, destination, _commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    assert project.settings is not None
    monkeypatch.setattr(_host, '_URL_TEMPLATE', destination.url)
    repository = {
        'owner': project.owner,
        'repo': project.repo,
        'visibility': 'public',
    }
    _responses(
        fake_gh,
        [
            {'args': _REPOSITORY_ARGS, 'stdout': json.dumps(repository)},
            _settings_response(project.settings),
        ],
    )

    _VERIFY_AFTER_FLIP(paths, global_config, project)

    assert gh_calls(tmp_path) == [_REPOSITORY_ARGS, _SETTINGS_ARGS]


def test_verify_after_flip_reads_remote_without_initializing_advisory_store(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project, destination, commit = _published_fixture(tmp_path, make_repo)
    assert project.settings is not None
    monkeypatch.setattr(_host, '_URL_TEMPLATE', destination.url)
    _responses(
        fake_gh,
        [
            _repository_response('public'),
            _settings_response(project.settings),
        ],
    )

    def _unavailable_store(_paths: Paths, _project: str) -> Path:
        raise PubanonError('local advisory store is unavailable')

    monkeypatch.setattr(_host, 'store_path', _unavailable_store)

    assert _VERIFY_AFTER_FLIP(paths, global_config, project) == commit
    assert gh_calls(tmp_path) == [_REPOSITORY_ARGS, _SETTINGS_ARGS]


def test_verify_after_flip_reports_settings_drift_as_public_partial_state(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project, destination, _commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    assert project.settings is not None
    monkeypatch.setattr(_host, '_URL_TEMPLATE', destination.url)
    repository = {
        'owner': project.owner,
        'repo': project.repo,
        'visibility': 'public',
    }
    _responses(
        fake_gh,
        [
            {'args': _REPOSITORY_ARGS, 'stdout': json.dumps(repository)},
            _settings_response(replace(project.settings, default_branch='trunk')),
        ],
    )

    with pytest.raises(PubanonError) as exc_info:
        _VERIFY_AFTER_FLIP(paths, global_config, project)

    error = exc_info.value
    rendered = str(error)
    assert 'repository is now public' in rendered
    assert 'post-flip verification failed' in rendered
    assert error.fact == 'default_branch: expected main; actual trunk'
    assert error.recovery is not None
    assert 'fix the reported drift' in error.recovery
    assert 'pubanon repo reveal' in error.recovery


def test_verify_after_flip_reports_unexpected_branch_in_public_partial_state(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    term = 'private-canary'
    paths, global_config, project, destination, commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    assert project.settings is not None
    monkeypatch.setattr(_host, '_URL_TEMPLATE', destination.url)
    host = tmp_path / 'host.git'
    _git(host, 'update-ref', f'refs/heads/{term}', commit)
    repository = {
        'owner': project.owner,
        'repo': project.repo,
        'visibility': 'public',
    }
    _responses(fake_gh, [{'args': _REPOSITORY_ARGS, 'stdout': json.dumps(repository)}])

    with pytest.raises(PubanonError) as exc_info:
        _VERIFY_AFTER_FLIP(paths, global_config, project)

    rendered = exc_info.value.render()
    assert 'repository is now public' in rendered
    assert 'post-flip verification failed' in rendered
    assert term in rendered
    assert exc_info.value.recovery is not None
    assert 'fix the reported drift' in exc_info.value.recovery
    assert 'pubanon repo reveal' in exc_info.value.recovery


def test_verify_after_flip_wraps_canonical_coordinate_drift(
    fake_gh: Path,
    tmp_path: Path,
) -> None:
    term = 'private-canary'
    paths, global_config, project = _configs(tmp_path)
    assert project.settings is not None
    _config.save_project(paths, project)
    _responses(fake_gh, [_repository_response('public', owner=term)])

    with pytest.raises(PubanonError) as exc_info:
        _VERIFY_AFTER_FLIP(paths, global_config, project)

    error = exc_info.value
    rendered = str(error)
    assert 'repository is now public' in rendered
    assert 'post-flip verification failed' in rendered
    assert error.recovery is not None
    assert 'fix the reported drift' in error.recovery
    assert 'pubanon repo reveal' in error.recovery
    rendered = error.render()
    assert term in rendered
    assert _SET_VISIBILITY_ARGS not in gh_calls(tmp_path)
    assert gh_calls(tmp_path) == [_REPOSITORY_ARGS]
    assert _config.load_project(paths, project.name).visibility == 'private'


def test_verify_after_flip_wraps_malformed_published_ref_read(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
) -> None:
    paths, global_config, project, _destination, _commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    assert project.settings is not None
    store = _store.store_path(paths, project.name)
    store.rename(tmp_path / 'malformed-store-backup')
    store.mkdir()
    _responses(fake_gh, [_repository_response('public')])

    with pytest.raises(PubanonError) as exc_info:
        _VERIFY_AFTER_FLIP(paths, global_config, project)

    rendered = exc_info.value.render()
    assert 'repository is now public' in str(exc_info.value)
    assert 'post-flip verification failed' in str(exc_info.value)
    assert 'cannot read snapshot store ref' in rendered
    assert exc_info.value.recovery is not None
    assert 'fix the reported drift' in exc_info.value.recovery
    assert 'pubanon repo reveal' in exc_info.value.recovery


def test_verify_before_flip_asks_about_hygiene_finding_from_any_published_commit(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    capsys: pytest.CaptureFixture[str],
    answers: _Script,
) -> None:
    term = 'style-canary'
    paths, global_config, project, destination, commit = _published_fixture(
        tmp_path, make_repo, message=f'{term} release'
    )
    project = replace(project, hygiene_terms=(term,))
    assert project.settings is not None
    _responses(fake_gh, [_settings_response(project.settings), *_disclosure_responses()])
    answers(
        [('Make these public with the repository? [y/n] ', 'y'), ('Make it public? [y/n] ', 'y')]
    )

    assert _VERIFY_BEFORE_FLIP(paths, global_config, project, destination) == (commit, '')

    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'Published history: matches GitHub' in output
    assert 'findings listed below' in output
    assert 'Policy terms in published history: 1 term in 1 location' in output
    assert f'correct the published history from {commit[:12]}' in output
    assert output.index('Policy terms in published history') < output.index('Not checked:')


@pytest.mark.parametrize(
    ('field', 'error'),
    [
        ('description', 'repository description did not pass content checks'),
        ('default_branch', 'repository default branch did not pass content checks'),
    ],
)
def test_verify_before_flip_gates_every_public_setting_before_disclosure_read(
    field: str,
    error: str,
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
) -> None:
    del fake_gh
    term = 'private-canary'
    paths, global_config, project, destination, _commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    global_config = replace(global_config, sensitive_terms=(term,))
    assert project.settings is not None
    settings = project.settings
    value = f'{term} details' if field == 'description' else f'{term}-branch'
    project = replace(project, settings=replace(settings, **{field: value}))

    with pytest.raises(PubanonError) as exc_info:
        _VERIFY_BEFORE_FLIP(paths, global_config, project, destination)

    rendered = exc_info.value.render()
    assert str(exc_info.value) == error
    assert term in rendered
    assert gh_calls(tmp_path) == []


def test_verify_before_flip_observes_selected_package_release_once(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, global_config, project, destination, commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    settings = project.settings
    assert settings is not None
    package = PackageObservation('pypi', 'demo', ('Build release', 'Publish release'))
    project = replace(project, package=package)
    _responses(
        fake_gh,
        [_settings_response(settings), *_disclosure_responses()],
    )
    selections: list[tuple[Paths, str, str | None]] = []
    check_calls: list[tuple[_host.Destination, str, tuple[str, ...]]] = []
    release_calls: list[tuple[str, str, str]] = []

    def _select(selected_paths: Paths, name: str, version: str | None) -> tuple[str, str]:
        selections.append((selected_paths, name, version))
        return '2.0.0', commit

    def _checks(
        selected_destination: _host.Destination,
        selected_commit: str,
        names: tuple[str, ...],
    ) -> list[CheckVerdict]:
        check_calls.append((selected_destination, selected_commit, names))
        return [
            CheckVerdict('Build release', 'success', 'success'),
            CheckVerdict('Publish release', 'success', 'success'),
        ]

    def _release(
        index: str,
        name: str,
        version: str,
    ) -> _package.PackageRelease | None:
        release_calls.append((index, name, version))
        return _package.PackageRelease(version, ('demo-2.0.0.tar.gz',))

    monkeypatch.setattr(_watch, 'select_package_release', _select)
    monkeypatch.setattr(_watch, 'ci_verdicts', _checks)
    monkeypatch.setattr(_package, 'release_state', _release)

    def _accept(_prompt: str) -> str:
        return 'y'

    monkeypatch.setattr('builtins.input', _accept)

    assert _VERIFY_BEFORE_FLIP(paths, global_config, project, destination) == (commit, '')

    assert selections == [(paths, project.name, None)]
    assert check_calls == [(destination, commit, package.release_checks)]
    assert release_calls == [('pypi', 'demo', '2.0.0')]
    assert re.search(r'Package: +demo 2.0.0 is published', capsys.readouterr().out)
    assert _SET_VISIBILITY_ARGS not in gh_calls(tmp_path)


def test_verify_before_flip_refuses_enabled_package_observation_without_a_tag(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project, destination, _commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    project = replace(
        project,
        package=PackageObservation('pypi', 'demo', ('Release',)),
    )

    def _unexpected(*_args: object) -> object:
        pytest.fail('package state was read without a published release tag')

    monkeypatch.setattr(_watch, 'ci_verdicts', _unexpected)
    monkeypatch.setattr(_package, 'release_state', _unexpected)

    with pytest.raises(PubanonError, match='no published release tag') as raised:
        _VERIFY_BEFORE_FLIP(paths, global_config, project, destination)

    assert raised.value.recovery is not None
    assert 'versioned' in raised.value.recovery
    assert 'package release' in raised.value.recovery


@pytest.mark.slow
@pytest.mark.parametrize(
    ('verdict', 'recovery'),
    [
        pytest.param(
            CheckVerdict('build', 'pending', 'queued'),
            'wait for the check, then rerun `pubanon repo reveal`',
            id='pending',
        ),
        pytest.param(
            CheckVerdict('build', 'missing', ''),
            ('check the configured name and workflow trigger, then rerun `pubanon repo reveal`'),
            id='missing',
        ),
        pytest.param(
            CheckVerdict('build', 'ambiguous', '2 check runs'),
            'resolve the multiple matching check runs, then rerun `pubanon repo reveal`',
            id='ambiguous',
        ),
        pytest.param(
            CheckVerdict('build', 'unacceptable', 'failure'),
            (
                'inspect the failed check and fix or rerun its workflow, then rerun '
                '`pubanon repo reveal`'
            ),
            id='failed',
        ),
    ],
)
def test_verify_before_flip_refuses_every_unsuccessful_required_check(
    verdict: CheckVerdict,
    recovery: str,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project, destination, _commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    project = replace(project, required_checks=('build',))
    published_before = _candidate.published_refs(paths, project.name)

    def _check(*_args: object) -> CheckVerdict:
        return verdict

    monkeypatch.setattr(_host, 'check_result', _check)

    with pytest.raises(PubanonError, match='required checks') as exc_info:
        _VERIFY_BEFORE_FLIP(paths, global_config, project, destination)

    expected = {
        'pending': 'pending',
        'missing': 'not found',
        'ambiguous': 'could not be determined',
        'unacceptable': 'failed',
    }[verdict.state]
    assert exc_info.value.fact == f'build: {expected}'
    assert exc_info.value.recovery == recovery
    assert _candidate.published_refs(paths, project.name) == published_before
    assert _SET_VISIBILITY_ARGS not in gh_calls(tmp_path)


def test_verify_before_flip_refuses_exact_settings_mismatch(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
) -> None:
    paths, global_config, project, destination, _commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    assert project.settings is not None
    actual = replace(project.settings, wiki=True)
    _responses(fake_gh, [_settings_response(actual)])

    with pytest.raises(PubanonError, match='settings') as exc_info:
        _VERIFY_BEFORE_FLIP(paths, global_config, project, destination)

    assert exc_info.value.fact == 'wiki: expected false; actual true'
    assert gh_calls(tmp_path) == [_SETTINGS_ARGS]


@pytest.mark.parametrize(
    'case',
    [
        pytest.param(
            (
                None,
                'package release is absent',
                'demo 1.2.3 is not published',
                'run the project release workflow, then rerun `pubanon repo reveal`',
            ),
            id='absent',
        ),
        pytest.param(
            (
                _package.PackageRelease('1.2.3', ()),
                'package release has no distribution files',
                'demo 1.2.3 metadata has no distribution files',
                'run `pubanon package watch`, then rerun `pubanon repo reveal`',
            ),
            id='metadata-only',
        ),
    ],
)
def test_verify_before_flip_refuses_incomplete_package_release(
    case: tuple[_package.PackageRelease | None, str, str, str],
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release, message, fact, recovery = case
    paths, global_config, project, destination, commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    project = replace(
        project,
        package=PackageObservation('pypi', 'demo', ('Release',)),
    )

    def _select(_paths: Paths, _name: str, _version: str | None) -> tuple[str, str]:
        return '1.2.3', commit

    def _checks(
        _destination: _host.Destination,
        _commit: str,
        _names: tuple[str, ...],
    ) -> list[CheckVerdict]:
        return [CheckVerdict('Release', 'success', 'success')]

    def _release_state(
        _index: str,
        _name: str,
        _version: str,
    ) -> _package.PackageRelease | None:
        return release

    monkeypatch.setattr(_watch, 'select_package_release', _select)
    monkeypatch.setattr(_watch, 'ci_verdicts', _checks)
    monkeypatch.setattr(_package, 'release_state', _release_state)

    with pytest.raises(PubanonError, match=message) as raised:
        _VERIFY_BEFORE_FLIP(paths, global_config, project, destination)

    assert raised.value.fact == fact
    assert raised.value.recovery == recovery


@pytest.mark.parametrize('host_has_main', [False, True])
def test_verify_before_flip_refuses_missing_local_published_main(
    host_has_main: bool,
    tmp_path: Path,
    make_repo: Callable[..., Path],
) -> None:
    paths, global_config, project = _configs(tmp_path)
    _store.store_path(paths, project.name)
    host = tmp_path / 'host.git'
    subprocess.run(['git', 'init', '--bare', '-q', host], check=True, env=git_env())
    if host_has_main:
        source = make_repo(files={'README.md': 'host only\n'})
        _git(host, 'fetch', source.as_uri(), 'HEAD:refs/heads/main')
    destination = _host.Destination(
        project.host,
        project.owner,
        project.repo,
        host.as_uri(),
        global_config.accounts[project.host],
        project.visibility,
    )

    with pytest.raises(PubanonError, match='nothing is published') as exc_info:
        _VERIFY_BEFORE_FLIP(paths, global_config, project, destination)

    if host_has_main:
        assert exc_info.value.recovery == 'pubanon repo restore --to local'
    else:
        assert exc_info.value.recovery is None


@pytest.mark.slow
def test_verify_before_flip_refuses_refs_drift_before_host_api_reads(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
) -> None:
    del fake_gh
    paths, global_config, project, destination, commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    host = tmp_path / 'host.git'
    _git(host, 'update-ref', 'refs/tags/v0.1.0', commit)

    with pytest.raises(PubanonError, match='saved published history') as exc_info:
        _VERIFY_BEFORE_FLIP(paths, global_config, project, destination)

    assert exc_info.value.expected is not None
    assert exc_info.value.actual is not None
    assert 'v0.1.0' not in exc_info.value.expected
    assert 'v0.1.0' in exc_info.value.actual
    assert exc_info.value.recovery == 'pubanon repo verify'
    assert not any('check-runs' in ' '.join(call) for call in gh_calls(tmp_path))
    assert not any('/actions/' in ' '.join(call) for call in gh_calls(tmp_path))


@pytest.mark.parametrize(
    'case',
    [
        pytest.param(
            (
                CheckVerdict('Release', 'pending', 'queued'),
                'package release checks are pending',
                'wait for the check, then run `pubanon package watch`',
            ),
            id='pending',
        ),
        pytest.param(
            (
                CheckVerdict('Release', 'unacceptable', 'failure'),
                'package release checks are not acceptable',
                (
                    'inspect the failed check and fix or rerun its workflow, then run '
                    '`pubanon package watch`'
                ),
            ),
            id='failed',
        ),
        pytest.param(
            (
                CheckVerdict('Release', 'missing', ''),
                'package release checks are not acceptable',
                (
                    'check the configured name and workflow trigger, then run '
                    '`pubanon package watch`'
                ),
            ),
            id='missing',
        ),
        pytest.param(
            (
                CheckVerdict('Release', 'ambiguous', '2 check runs'),
                'package release checks are not acceptable',
                ('resolve the multiple matching check runs, then run `pubanon package watch`'),
            ),
            id='ambiguous',
        ),
    ],
)
def test_verify_before_flip_refuses_unsuccessful_package_checks_before_index_read(
    case: tuple[CheckVerdict, str, str],
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verdict, message, recovery = case
    paths, global_config, project, destination, commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    project = replace(
        project,
        package=PackageObservation('pypi', 'demo', ('Release',)),
    )
    published_before = _candidate.published_refs(paths, project.name)
    calls = 0

    def _checks(*_args: object) -> list[CheckVerdict]:
        nonlocal calls
        calls += 1
        return [verdict]

    def _unexpected(*_args: object) -> _package.PackageRelease | None:
        pytest.fail('package index read occurred before release checks succeeded')

    def _select(_paths: Paths, _name: str, _version: str | None) -> tuple[str, str]:
        return '1.2.3', commit

    monkeypatch.setattr(_watch, 'select_package_release', _select)
    monkeypatch.setattr(_watch, 'ci_verdicts', _checks)
    monkeypatch.setattr(_package, 'release_state', _unexpected)

    with pytest.raises(PubanonError, match=message) as raised:
        _VERIFY_BEFORE_FLIP(paths, global_config, project, destination)

    expected = {
        'pending': 'pending',
        'missing': 'not found',
        'ambiguous': 'could not be determined',
        'unacceptable': 'failed',
    }[verdict.state]
    assert raised.value.fact == f'{verdict.name}: {expected}'
    assert raised.value.recovery == recovery
    assert calls == 1
    assert _candidate.published_refs(paths, project.name) == published_before
    assert _SET_VISIBILITY_ARGS not in gh_calls(tmp_path)


@pytest.mark.parametrize(
    'case',
    [
        (False, 'Make it public? [y/n] '),
        (True, 'Record anonymous-account/public-repo as public? [y/n] '),
    ],
)
def test_verify_before_flip_asks_the_final_question_after_the_unchecked_statement(
    case: tuple[bool, str],
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    already_public, question = case
    paths, global_config, project, destination, _commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    assert project.settings is not None
    _responses(
        fake_gh,
        [
            _settings_response(project.settings),
            *_disclosure_responses(('101 build',), ('201 wheel',)),
        ],
    )
    prompts: list[str] = []
    shown: list[str] = []
    output_buffer = StringIO()

    def _decline(prompt: str) -> str:
        prompts.append(prompt)
        shown.append(re.sub(r': +', ': ', output_buffer.getvalue()))
        return 'n'

    monkeypatch.setattr('builtins.input', _decline)

    with (
        redirect_stdout(output_buffer),
        pytest.raises(OperationCanceled, match=r'^Canceled\. Nothing was changed\.$'),
    ):
        _VERIFY_BEFORE_FLIP(
            paths, global_config, project, destination, already_public=already_public
        )

    assert prompts == [question]
    assert 'Actions logs: none' in shown[0]
    unchecked = next(line for line in shown[0].splitlines() if line.startswith('Not checked:'))
    assert re.search(r'\b1 artifact\b', unchecked)
    assert 'job summaries and annotations' in unchecked
    assert 'earlier pushes on the Activity page' in unchecked
    assert 'https://github.com/anonymous-account/public-repo/actions\n' in shown[0]
    assert 'actions/runs/101' not in shown[0]
    assert 'Saved inventory:' not in shown[0]
    assert not any('PATCH' in call for call in gh_calls(tmp_path))


@pytest.mark.parametrize(
    'choice',
    ['keep', 'delete', 'delete then cancel', 'delete then interrupt', 'delete then error'],
)
def test_reveal_reports_actions_log_matches_and_acts_on_the_choice(
    choice: str,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    answers: _Script,
) -> None:
    tmp_path = fake_gh.parent
    paths, global_config, project, destination, _commit = _published_fixture(tmp_path, make_repo)
    global_config = replace(global_config, sensitive_terms=('log-canary',))
    assert project.settings is not None
    _config.save_project(paths, project)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', destination.url)
    run = 40000000001
    log_args = [
        'api',
        '--allow-escape-sequences',
        'repos/anonymous-account/public-repo/actions/jobs/7/logs',
    ]
    delete_args = [
        'api',
        '-X',
        'DELETE',
        f'repos/anonymous-account/public-repo/actions/runs/{run}/logs',
    ]
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('private', once=True),
            _repository_response('private', once=True),
            _settings_response(project.settings) | {'once': True},
            *_disclosure_responses((f'{run} CI',), jobs={run: [_job_entry(7, run)]}),
            {'args': log_args, 'stdout': 'private log line with log-canary\n', 'once': True},
            {
                'args': log_args,
                'returncode': 1,
                'stdout': '{"message":"Not Found","status":"404"}',
                'stderr': 'gh: Not Found (HTTP 404)\n',
            },
            {
                'args': ['api', 'repos/anonymous-account/public-repo/actions/jobs/7'],
                'stdout': json.dumps(_job_entry(7, run)),
            },
            {'args': delete_args},
            {'args': _SET_VISIBILITY_ARGS},
            _repository_response('public'),
            _settings_response(project.settings),
        ],
    )
    menu = 'What should happen to these logs? [d]elete / [k]eep / [c]ancel: '
    delete = f'Delete the logs of 1 run ({run}, CI)? This cannot be undone. [y/n] '
    final = 'Make it public? [y/n] '
    output_buffer = StringIO()
    with redirect_stdout(output_buffer):
        if choice == 'keep':
            answers([(menu, 'k'), (final, 'y')])
            _lifecycle.reveal(paths, global_config, project)
        elif choice == 'delete':
            answers([(menu, 'd'), (delete, 'y'), (final, 'y')])
            _lifecycle.reveal(paths, global_config, project)
        elif choice == 'delete then cancel':
            answers([(menu, 'd'), (delete, 'y'), (final, 'n')])
            with pytest.raises(OperationCanceled) as error:
                _lifecycle.reveal(paths, global_config, project)
            assert all(
                fragment in str(error.value)
                for fragment in (
                    'Canceled',
                    'Deleted the logs of 1 run',
                    'nothing else was changed',
                )
            )
        else:
            failure: BaseException = (
                KeyboardInterrupt()
                if choice == 'delete then interrupt'
                else PubanonError('invalid Actions cache response')
            )

            def _fail(_owner: str, _repo: str) -> int:
                raise failure

            monkeypatch.setattr(_host, 'actions_cache_count', _fail)
            answers([(menu, 'd'), (delete, 'y')])
            with pytest.raises(PubanonError) as error:
                _lifecycle.reveal(paths, global_config, project)
            if choice == 'delete then interrupt':
                assert isinstance(error.value, OperationCanceled)
                assert all(
                    fragment in str(error.value)
                    for fragment in (
                        'Canceled',
                        'Deleted the logs of 1 run',
                        'nothing else was changed',
                    )
                )
            else:
                assert str(error.value) == 'invalid Actions cache response'
                assert error.value.fact == 'Deleted the logs of 1 run.'

    output = re.sub(r': +', ': ', output_buffer.getvalue())
    calls = gh_calls(tmp_path)
    assert 'Checking Actions logs: 1 run, 1 job.' in output
    assert 'Actions logs: 1 checked; findings listed below' in output
    assert 'Sensitive terms in Actions logs: 1 term in 1 location' in output
    assert 'private log line' not in output
    assert (delete_args in calls) is (choice != 'keep')
    assert (_SET_VISIBILITY_ARGS in calls) is (choice in {'keep', 'delete'})
    if choice == 'delete':
        assert calls.index(delete_args) < calls.index(_SET_VISIBILITY_ARGS)


@pytest.mark.parametrize(
    'case',
    [
        ('private', 'Make these public with the repository? [y/n] '),
        ('public', 'Record the repository as public with this history? [y/n] '),
    ],
)
def test_reveal_correction_advice_names_immutable_release_pages(
    case: tuple[str, str],
    fake_gh: Path,
    published_reveal: _PublishedReveal,
    monkeypatch: pytest.MonkeyPatch,
    answers: _Script,
) -> None:
    visibility, question = case
    paths, global_config, project, _destination, _commit = published_reveal
    project = replace(project, extra_terms=('historical-private-canary',))
    assert project.settings is not None
    _config.save_project(paths, project)
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response(visibility),
            _settings_response(project.settings),
            *_disclosure_responses(),
        ],
    )
    pages = (
        _host.GitHubRelease(
            'v1.1.0',
            'v1.1.0',
            '',
            'https://example.invalid/v1.1.0',
            draft=False,
            prerelease=False,
            identifier=2,
            immutable=False,
            created_at='2026-09-23T10:00:00Z',
        ),
        _host.GitHubRelease(
            'v1.0.0',
            'v1.0.0',
            '',
            'https://example.invalid/v1.0.0',
            draft=False,
            prerelease=False,
            identifier=1,
            immutable=True,
            created_at='2026-09-22T10:00:00Z',
        ),
    )

    def _all_releases(
        _destination: _host.Destination, *, deadline: float | None = None
    ) -> tuple[_host.GitHubRelease, ...]:
        del deadline
        return pages

    monkeypatch.setattr(_host, 'all_releases', _all_releases)
    answers([(question, 'n')])

    output_buffer = StringIO()
    with (
        redirect_stdout(output_buffer),
        pytest.raises(OperationCanceled, match=r'^Canceled\. Nothing was changed\.$'),
    ):
        _lifecycle.reveal(paths, global_config, project)

    output = output_buffer.getvalue()
    assert 'Release page v1.0.0 is immutable' in output
    assert 'cannot replace history that moves its tag' in output
    assert 'recreated repository cannot reuse that version' in output
    assert 'v1.1.0 is immutable' not in output


def test_verify_before_flip_reports_escaped_unexpected_branch_at_refs_step(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del fake_gh
    term = 'private-canary'
    paths, global_config, project, destination, commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    branch = f'{term}\u202ebackup'

    def _branch_refs(*_args: object, **_kwargs: object) -> _host.RemoteRefs:
        return _host.RemoteRefs(commit, {}, ((branch, commit),))

    monkeypatch.setattr(_host, 'ls_remote', _branch_refs)

    with pytest.raises(PubanonError, match='saved published history') as exc_info:
        _VERIFY_BEFORE_FLIP(paths, global_config, project, destination)

    rendered = exc_info.value.render()
    assert term in rendered
    assert '\u202e' not in rendered
    assert exc_info.value.expected is not None
    assert exc_info.value.actual is not None
    assert 'branches=none' in exc_info.value.expected
    assert f'branches={term}\\u202ebackup=' in exc_info.value.actual
    assert gh_calls(tmp_path) == []


def test_verify_before_flip_returns_release_oid_and_reports_six_steps_in_order(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, global_config, project, destination, commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    assert project.settings is not None
    _responses(
        fake_gh,
        [_settings_response(project.settings), *_disclosure_responses()],
    )

    def _accept(_prompt: str) -> str:
        return 'y'

    monkeypatch.setattr('builtins.input', _accept)

    result = _VERIFY_BEFORE_FLIP(paths, global_config, project, destination)

    assert result == (commit, '')
    output = re.sub(r': +', ': ', capsys.readouterr().out)
    reports = (
        'Published history: matches GitHub; no sensitive terms or identity problems',
        'Release pages: none',
        'Actions logs: none',
        'Required checks: none configured',
        'Package: not enabled',
        'Settings: match your configuration',
        'Not checked: earlier pushes on the Activity page.',
    )
    positions = [output.index(report) for report in reports]
    assert positions == sorted(positions)


def test_verify_before_flip_writes_complete_paginated_disclosure_owner_only(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, global_config, project, destination, commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    assert project.settings is not None
    runs = tuple(f'{index} {"build" if index <= 100 else "docs"}' for index in range(1, 151))
    artifacts = ('201 package', '202 report')
    _responses(
        fake_gh,
        [_settings_response(project.settings), *_disclosure_responses(runs, artifacts)],
    )

    def _accept(_prompt: str) -> str:
        return 'y'

    monkeypatch.setattr('builtins.input', _accept)

    def _no_actions(
        coordinates: tuple[str, str], _account: str, _rules: object
    ) -> _actions.ActionsReview:
        return _actions.ActionsReview(coordinates, (), (), frozenset(), 0, ())

    def _unchanged(_review: _actions.ActionsReview) -> None:
        return None

    monkeypatch.setattr(_actions, 'check', _no_actions)
    monkeypatch.setattr(_actions, 'require_unchanged', _unchanged)

    assert _VERIFY_BEFORE_FLIP(paths, global_config, project, destination) == (commit, '')

    disclosure_path = paths.data_dir / 'projects' / project.name / 'disclosure.json'
    payload = json.loads(disclosure_path.read_text(encoding='utf-8'))
    assert payload['runs'] == [
        f'{index} {"build" if index <= 100 else "docs"}' for index in range(1, 151)
    ]
    assert payload['artifacts'] == ['201 package', '202 report']
    assert_private_mode(disclosure_path, 0o600)
    output = re.sub(r': +', ': ', capsys.readouterr().out)
    unchecked = next(line for line in output.splitlines() if line.startswith('Not checked:'))
    assert re.search(r'\b2 artifacts\b', unchecked)
    assert 'job summaries and annotations' in unchecked
    assert 'earlier pushes on the Activity page' in unchecked
    assert escape((disclosure_path).as_posix()) not in output
