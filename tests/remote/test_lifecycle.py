import json
import re
import subprocess
from collections.abc import Callable
from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path

import pytest

from pubanon import (
    _config,
)
from pubanon._config import (
    GlobalConfig,
    PackageObservation,
    Paths,
    ProjectConfig,
    Settings,
)
from pubanon._errors import ObservationTimeoutError, OperationCanceled, PubanonError
from pubanon._remote import _host, _lifecycle, _package, _watch
from pubanon._remote._host import CheckVerdict
from pubanon._snapshots import _audit, _candidate, _records, _source
from pubanon._terminal import escape, verbose_context
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


def _disclosure_responses(
    runs: tuple[str, ...] = (),
    artifacts: tuple[str, ...] = (),
) -> list[dict[str, object]]:
    responses: list[dict[str, object]] = []
    for resource, collection, entries in (
        ('runs', 'workflow_runs', runs),
        ('artifacts', 'artifacts', artifacts),
    ):
        for offset in range(0, max(len(entries), 1), 100):
            page = offset // 100 + 1
            page_entries = entries[offset : offset + 100]
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
                                {
                                    'id': int(entry.partition(' ')[0]),
                                    'name': entry.partition(' ')[2],
                                }
                                for entry in page_entries
                            ],
                        }
                    ),
                }
            )
    return responses


def _gh_calls(tmp_path: Path) -> list[list[str]]:
    path = tmp_path / 'fake-gh-calls.jsonl'
    if not path.exists():
        return []
    return [json.loads(line)['args'] for line in path.read_text().splitlines()]


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ['git', '-C', repo, *args],
        check=True,
        capture_output=True,
        env=git_env(),
    ).stdout


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
    store = _candidate.store_path(paths, project.name)
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


def _responses(path: Path, responses: list[dict[str, object]]) -> None:
    path.write_text(json.dumps(responses))


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

    monkeypatch.setattr(_lifecycle, 'disclosure', _disclosure_inventory)
    prompts: list[str] = []

    def _decline(prompt: str) -> str:
        prompts.append(prompt)
        return 'no'

    monkeypatch.setattr('builtins.input', _decline)

    with pytest.raises(OperationCanceled, match=r'Canceled\. Nothing was changed.'):
        _lifecycle._verify_disclosure(paths, project, already_public=False)

    assert prompts == [
        'These runs, logs, and artifacts become public with the repository. Continue? [y/n] '
    ]
    disclosure_path = paths.data_dir / 'projects' / project.name / 'disclosure.json'
    assert disclosure_path.exists()
    assert 'Saved inventory:' not in capsys.readouterr().out
    with verbose_context(enabled=True), pytest.raises(OperationCanceled):
        _lifecycle._verify_disclosure(paths, project, already_public=False)
    assert f'Saved inventory: {escape(str(disclosure_path))}' in capsys.readouterr().out


def test_repo_create_declined_confirmation_creates_nothing(
    fake_gh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, global_config, project = _configs(tmp_path)
    _responses(fake_gh, _success_responses())

    def _decline(prompt: str) -> str:
        assert prompt == 'Create anonymous-account/public-repo as a private repository? [y/n] '
        return 'no'

    monkeypatch.setattr('builtins.input', _decline)

    with pytest.raises(OperationCanceled, match=r'Canceled\. Nothing was created.'):
        _lifecycle.repo_create(paths, global_config, project, visibility='private', yes=False)

    assert _gh_calls(tmp_path) == [_ACCOUNT_ARGS, _ABSENCE_ARGS]


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
        _lifecycle.repo_create(paths, global_config, project, visibility='private', yes=True)

    error = exc_info.value
    assert str(error) == f'simulated {term} owner lookup failure'
    assert 'unknown' not in error.render()
    assert 'partial' not in error.render()
    assert _gh_calls(tmp_path) == [_ACCOUNT_ARGS, absence_args, owner_args]
    assert all('POST' not in call for call in _gh_calls(tmp_path))
    assert not (paths.config_dir / 'projects' / f'{project.name}.toml').exists()


def test_repo_create_failed_post_reports_unknown_outcome_and_present_retry(
    fake_gh: Path,
    tmp_path: Path,
) -> None:
    term = 'private-canary'
    paths, global_config, project = _configs(tmp_path)
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
            {
                'args': _CREATE_ARGS,
                'returncode': 1,
                'stderr': f'simulated {term} POST failure\n',
                'once': True,
            },
        ],
    )

    with pytest.raises(PubanonError) as exc_info:
        _lifecycle.repo_create(paths, global_config, project, visibility='private', yes=True)

    error = exc_info.value
    assert str(error) == (
        'repository creation outcome is unknown; the repository may already exist'
    )
    assert error.recovery == 'pubanon repo verify'
    rendered = error.render()
    assert term in rendered
    assert _gh_calls(tmp_path).count(_CREATE_ARGS) == 1
    assert not (paths.config_dir / 'projects' / f'{project.name}.toml').exists()

    with pytest.raises(PubanonError, match='already exists') as retry_info:
        _lifecycle.repo_create(paths, global_config, project, visibility='private', yes=True)

    assert retry_info.value.recovery == 'pubanon repo verify'
    assert _gh_calls(tmp_path).count(_CREATE_ARGS) == 1
    assert not (paths.config_dir / 'projects' / f'{project.name}.toml').exists()


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
        _lifecycle.repo_create(paths, global_config, project, visibility='private', yes=True)

    rendered = exc_info.value.render()
    assert field.rsplit('.', maxsplit=1)[-1].replace('_', ' ') in rendered
    assert term in rendered
    assert _gh_calls(tmp_path) == []


def test_repo_create_preserves_post_create_diagnostic(
    fake_gh: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, global_config, project = _configs(tmp_path)
    _responses(fake_gh, _success_responses())
    original = PubanonError('verification failed', fact=r'exit status 7: diagnostic\x1b\\path')

    def _fail(*_args: object) -> None:
        raise original

    monkeypatch.setattr(_lifecycle, 'verify_destination', _fail)
    with pytest.raises(PubanonError) as error:
        _lifecycle.repo_create(paths, global_config, project, visibility='private', yes=True)
    assert str(error.value) == 'repository creation is partial; post-create verification failed'
    assert error.value.fact == original.render()
    assert error.value.recovery == 'pubanon repo verify'
    assert _CREATE_ARGS in _gh_calls(tmp_path)


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
        _lifecycle.repo_create(paths, global_config, project, visibility='private', yes=True)

    assert _gh_calls(tmp_path) == [_ACCOUNT_ARGS, absence_args, owner_args]
    assert all('POST' not in call for call in _gh_calls(tmp_path))


def test_repo_create_refuses_visibility_mismatch_before_host_contact(tmp_path: Path) -> None:
    paths, global_config, project = _configs(tmp_path)

    with pytest.raises(
        PubanonError,
        match='requested repository visibility does not match configuration',
    ) as exc_info:
        _lifecycle.repo_create(paths, global_config, project, visibility='public', yes=True)

    error = exc_info.value
    assert error.expected == 'private'
    assert error.actual == 'public'
    assert error.recovery == ('align the requested visibility with the project configuration')
    assert _gh_calls(tmp_path) == []


def test_repo_create_refuses_visible_repository_before_creation(
    fake_gh: Path,
    tmp_path: Path,
) -> None:
    paths, global_config, project = _configs(tmp_path)
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('private'),
        ],
    )

    with pytest.raises(PubanonError) as exc_info:
        _lifecycle.repo_create(paths, global_config, project, visibility='private', yes=True)

    assert exc_info.value.recovery == 'pubanon repo verify'
    assert _gh_calls(tmp_path) == [_ACCOUNT_ARGS, _ABSENCE_ARGS]
    assert all('POST' not in call for call in _gh_calls(tmp_path))


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
        _lifecycle.repo_create(paths, global_config, project, visibility='private', yes=True)

    assert exc_info.value.fact == f'repo: public-repo-{term}'
    assert term in (exc_info.value.fact or '')
    assert _gh_calls(tmp_path) == []


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
        return True

    monkeypatch.setattr(_lifecycle, 'require_authenticated_account', _require_account)
    monkeypatch.setattr(_lifecycle, 'repository_visible', _repository_visible)

    with pytest.raises(PubanonError, match='configured repository already exists'):
        _lifecycle.repo_create(paths, global_config, project, visibility='private', yes=True)

    assert requirements == [('anonymous-account', 'configuration')]


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
        _lifecycle.repo_create(paths, global_config, project, visibility='private', yes=True)

    assert 'repository was created' in str(error.value)
    assert 'GitHub settings' in (error.value.recovery or '')
    assert 'pubanon repo verify' in (error.value.recovery or '')
    assert error.value.actual is None
    if failure == 'mismatch':
        assert 'settings do not match' in str(error.value)
        for difference in (
            'description: declared Public repository, observed Host override',
            'issues: declared true, observed false',
            'wiki: declared false, observed true',
            'projects: declared true, observed false',
            'pull_requests: declared false, observed true',
            'discussions: declared false, observed true',
        ):
            assert difference in (error.value.fact or '')
        assert 'default_branch' not in (error.value.fact or '')
    else:
        assert 'could not be verified' in str(error.value)
        assert 'settings transport failed' in (error.value.fact or '')
        assert 'observed' not in (error.value.fact or '')
    calls = _gh_calls(tmp_path)
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
        _lifecycle.repo_create(paths, global_config, project, visibility='private', yes=True)

    assert 'partial' in str(exc_info.value)
    assert exc_info.value.recovery == 'pubanon repo verify'
    assert _CREATE_ARGS in _gh_calls(tmp_path)


@pytest.mark.parametrize(
    'case',
    [
        (None, 'pubanon prepare'),
        ('retained', 'pubanon publish'),
        ('unknown-outcome', 'pubanon publish --resolve'),
    ],
)
def test_repo_create_user_owner_posts_settings_then_verifies_empty_repository(
    fake_gh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_repo: Callable[..., Path],
    case: tuple[str | None, str],
) -> None:
    candidate_state, next_action = case
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
    _responses(fake_gh, _success_responses(create_args=create_args))

    captured = StringIO()
    with redirect_stdout(captured):
        _lifecycle.repo_create(paths, global_config, project, visibility='private', yes=True)

    assert _gh_calls(tmp_path) == [
        _ACCOUNT_ARGS,
        _ABSENCE_ARGS,
        create_args,
        _PATCH_SETTINGS_ARGS,
        _SETTINGS_ARGS,
        _ACCOUNT_ARGS,
        _REPOSITORY_ARGS,
    ]
    output = captured.getvalue()
    assert 'Create repository anonymous-account/public-repo' in output
    assert 'Visibility:' in output
    assert 'private' in output
    assert 'Description:' in output
    assert 'Public repository' in output
    assert 'Default branch:' in output
    assert 'Features:' in output
    assert 'pull requests, and discussions off' in output
    assert 'Created private repository anonymous-account/public-repo.' in output
    assert output.rstrip().endswith(f'Next: {next_action}')
    if candidate_before is not None:
        assert _records.candidate_path(paths, project.name).read_bytes() == candidate_before
    else:
        assert _candidate.load_candidate(paths, project.name) is None


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

    _lifecycle.repo_create(paths, global_config, project, visibility='private', yes=True)

    assert _CREATE_ARGS in _gh_calls(tmp_path)


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
    changed = (
        'Made anonymous-account/public-repo public.'
        if host_changed
        else 'Recorded public visibility for anonymous-account/public-repo; it was already public.'
    )
    assert steps == ['verify public remote', 'save public project']
    assert changed in output
    assert 'warning: its earlier Actions history and logs are now public too.' in output
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
    assert str(error) == 'the repository is public and verified but configuration update failed'
    assert error.fact == 'error: simulated configuration write failure'
    assert error.recovery == 'fix the configuration write, then rerun pubanon repo reveal'
    assert capsys.readouterr().out.splitlines() == ['trace: verify public remote']


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
    answers = iter(['yes', 'no'])

    def _answer(_prompt: str) -> str:
        return next(answers)

    monkeypatch.setattr('builtins.input', _answer)

    with pytest.raises(OperationCanceled, match=r'Canceled\. Nothing was changed.'):
        _lifecycle.reveal(paths, global_config, project)

    assert _SET_VISIBILITY_ARGS not in _gh_calls(tmp_path)
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

    def _accept(prompt: str) -> str:
        prompts.append(prompt)
        return 'yes'

    convergence_calls: list[bool] = []
    converge_public = _CONVERGE_PUBLIC

    def _record_convergence(
        owned_paths: Paths,
        owned_global: GlobalConfig,
        owned_project: ProjectConfig,
        *,
        host_changed: bool,
    ) -> None:
        convergence_calls.append(host_changed)
        converge_public(owned_paths, owned_global, owned_project, host_changed=host_changed)

    monkeypatch.setattr('builtins.input', _accept)
    monkeypatch.setattr(_lifecycle, '_converge_public', _record_convergence)

    _lifecycle.reveal(paths, global_config, project)

    calls = _gh_calls(tmp_path)
    assert calls.count(_SET_VISIBILITY_ARGS) == 1
    assert calls.index(_SET_VISIBILITY_ARGS) > calls.index(
        ['api', 'repos/anonymous-account/public-repo/actions/artifacts?per_page=100&page=1']
    )
    assert calls[calls.index(_SET_VISIBILITY_ARGS) + 1] == _REPOSITORY_ARGS
    assert _config.load_project(paths, project.name).visibility == 'public'
    assert len(prompts) == 2
    assert 'become public with the repository' in prompts[0]
    assert prompts[1] == 'Make it public? [y/n] '
    assert convergence_calls == [True]
    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'warning: its earlier Actions history and logs are now public too.' in output
    assert output.startswith('Make anonymous-account/public-repo public\n')
    assert 'Published history:' in output
    assert 'Not checked by Pubanon:' in output
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
    assert _gh_calls(tmp_path) == []


def test_reveal_manual_flip_sanitization_failure_keeps_private_intent(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    term = 'style-canary'
    paths, global_config, project, destination, _commit = _published_fixture(
        tmp_path,
        make_repo,
        message=f'{term} release',
    )
    project = replace(project, hygiene_terms=(term,))
    assert project.settings is not None
    _config.save_project(paths, project)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', destination.url)
    _responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            _repository_response('public'),
            _settings_response(project.settings),
        ],
    )

    with pytest.raises(PubanonError) as exc_info:
        _lifecycle.reveal(paths, global_config, project)

    assert 'sanitization and identity' in exc_info.value.render()
    assert _config.load_project(paths, project.name).visibility == 'private'
    assert _SET_VISIBILITY_ARGS not in _gh_calls(tmp_path)
    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'content is already public' in output


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
    store = _candidate.store_path(paths, project.name)
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
        raise ObservationTimeoutError(
            stdout=b'private timeout stdout',
            stderr=b'private timeout stderr',
        )

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
    assert error.recovery == 'rerun pubanon repo reveal to retry package observation'
    assert 'private timeout' not in error.render()
    assert observations == ['checks', 'release']
    assert _SET_VISIBILITY_ARGS not in _gh_calls(tmp_path)
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
        return 'yes'

    monkeypatch.setattr('builtins.input', _accept)

    with pytest.raises(PubanonError) as exc_info:
        _lifecycle.reveal(paths, global_config, project)

    assert str(exc_info.value) == (
        'repository visibility outcome is unknown; the repository may already be public'
    )
    assert exc_info.value.recovery == 'rerun pubanon repo reveal'
    assert _config.load_project(paths, project.name).visibility == 'private'
    assert _gh_calls(tmp_path).count(_SET_VISIBILITY_ARGS) == 1

    _lifecycle.reveal(paths, global_config, project)

    assert _gh_calls(tmp_path).count(_SET_VISIBILITY_ARGS) == 1
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
    prompts = 0

    def _accept(_prompt: str) -> str:
        nonlocal prompts
        prompts += 1
        if prompts == 2:
            host = tmp_path / 'host.git'
            _git(host, 'update-ref', f'refs/heads/{term}', commit)
        return 'yes'

    monkeypatch.setattr('builtins.input', _accept)

    with pytest.raises(PubanonError) as exc_info:
        _lifecycle.reveal(paths, global_config, project)

    rendered = exc_info.value.render()
    assert 'the repository is now public but post-flip verification failed' in rendered
    assert term in rendered
    assert _config.load_project(paths, project.name).visibility == 'private'
    assert _gh_calls(tmp_path).count(_SET_VISIBILITY_ARGS) == 1


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
        return 'yes'

    monkeypatch.setattr('builtins.input', _accept)

    with pytest.raises(PubanonError) as exc_info:
        _lifecycle.reveal(paths, global_config, project)

    assert str(exc_info.value) == (
        'the repository is now public but post-flip verification failed'
    )
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

    assert _gh_calls(tmp_path).count(_SET_VISIBILITY_ARGS) == 1
    assert _config.load_project(paths, project.name).visibility == 'public'


@pytest.mark.slow
def test_reveal_prints_trusted_disclosure_path_and_safely_renders_inventory(
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
            *_disclosure_responses((f'1 {term}\x1bjob',), ()),
            {'args': _SET_VISIBILITY_ARGS},
            _repository_response('public'),
            _settings_response(project.settings),
        ],
    )

    def _accept(_prompt: str) -> str:
        return 'yes'

    monkeypatch.setattr('builtins.input', _accept)

    _lifecycle.reveal(paths, global_config, project)

    output_lines = re.sub(r': +', ': ', capsys.readouterr().out).splitlines()
    assert not any('Saved inventory:' in line for line in output_lines)
    assert f'  {term}\\x1bjob: 1' in output_lines
    assert '  Activity: https://github.com/anonymous-account/public-repo/activity' in output_lines
    assert '  Review: https://github.com/anonymous-account/public-repo/actions' in output_lines
    assert any(
        'warning: Pubanon has not scanned Actions logs, artifact contents, or other retained '
        'GitHub objects.' in line
        for line in output_lines
    )


def test_reveal_public_config_private_host_refuses_repo_verify_recovery(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
) -> None:
    paths, global_config, project, _destination, _commit = _published_fixture(
        tmp_path,
        make_repo,
    )
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
        escape(str(paths.config_dir / 'projects' / f'{project.name}.toml'))
        in exc_info.value.recovery
    )
    assert 'pubanon repo verify' not in exc_info.value.recovery
    assert _gh_calls(tmp_path) == [_ACCOUNT_ARGS, _REPOSITORY_ARGS]


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

    _lifecycle.reveal(paths, global_config, project)

    assert _SET_VISIBILITY_ARGS not in _gh_calls(tmp_path)
    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'Repository anonymous-account/public-repo is already public.' in output
    assert (
        'Verified now: public visibility, published branches and tags, and repository settings.'
        in output
    )
    assert 'No local or host state was changed.' not in output
    assert 'Repository is already public and verified.' not in output
    assert 'Prior Actions history and logs are already public.' not in output
    assert 'Next: pubanon audit --remote' in output
    assert output.index('Next:') > output.index('Verified now:')


@pytest.mark.parametrize('exposure', ['Anonymous Publisher', 'anonymous@example.invalid'])
def test_reveal_refuses_newly_sensitive_anonymous_identity_before_visibility_mutation(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    exposure: str,
) -> None:
    paths, global_config, project, destination, commit = _published_fixture(tmp_path, make_repo)
    project = replace(project, extra_terms=(exposure,))
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

    def _unexpected_prompt(_prompt: str) -> str:
        pytest.fail('sensitive historical identity reached disclosure acknowledgment')

    monkeypatch.setattr('builtins.input', _unexpected_prompt)

    with pytest.raises(PubanonError, match='sanitization and identity') as error:
        _lifecycle.reveal(paths, global_config, project)

    rendered = error.value.render()
    for field in ('author', 'committer'):
        assert f'{commit[:12]}:header:{field}' in rendered
    assert 'project extra_terms entry 1' in rendered
    assert f'commit {commit}' in rendered
    assert 'header author value; text ' in rendered
    assert 'header committer value; text ' in rendered
    assert exposure in (error.value.fact or '')
    assert exposure in rendered
    calls = _gh_calls(tmp_path)
    assert _SET_VISIBILITY_ARGS not in calls
    assert not any('/actions/' in ' '.join(call) for call in calls)
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
        return _audit.HistoryAudit((), ('malformed commit header',), ())

    def _unexpected_prompt(_prompt: str) -> str:
        pytest.fail('structural reveal failure requested acknowledgment')

    monkeypatch.setattr(_audit, 'audit_history', _structural_audit)
    monkeypatch.setattr('builtins.input', _unexpected_prompt)

    with pytest.raises(PubanonError, match='sanitization and identity'):
        _lifecycle.reveal(paths, global_config, project)

    calls = _gh_calls(tmp_path)
    assert _SET_VISIBILITY_ARGS not in calls
    assert not any('/actions/runs?' in ' '.join(call) for call in calls)
    assert not any('/actions/artifacts?' in ' '.join(call) for call in calls)
    assert _config.load_project(paths, project.name).visibility == 'private'


def test_reveal_resume_refuses_canonical_coordinate_drift_before_verification(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
) -> None:
    term = 'private-canary'
    paths, global_config, project, _destination, _commit = _published_fixture(
        tmp_path,
        make_repo,
    )
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
    assert _SET_VISIBILITY_ARGS not in _gh_calls(tmp_path)
    assert _gh_calls(tmp_path) == [_ACCOUNT_ARGS, _REPOSITORY_ARGS]
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

    def _accept(prompt: str) -> str:
        prompts.append(prompt)
        return 'yes'

    monkeypatch.setattr('builtins.input', _accept)

    _lifecycle.reveal(paths, global_config, project)

    calls = _gh_calls(tmp_path)
    assert _SET_VISIBILITY_ARGS not in calls
    assert check_args in calls
    assert any('/actions/runs?' in ' '.join(call) for call in calls)
    assert any('/actions/artifacts?' in ' '.join(call) for call in calls)
    assert _config.load_project(paths, project.name).visibility == 'public'
    assert prompts == ['These runs, logs, and artifacts are already public. Continue? [y/n] ']
    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'Verify public repository anonymous-account/public-repo' in output
    assert 'content is already public' in output
    assert (
        'Recorded public visibility for anonymous-account/public-repo; it was already public.\n'
        in output
    )
    assert 'warning: its earlier Actions history and logs are now public too.\n' in output
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

    monkeypatch.setattr(_lifecycle, 'require_authenticated_account', _require_account)

    with pytest.raises(PubanonError, match='account authority observed'):
        _lifecycle.reveal(paths, global_config, project)

    assert requirements == [('anonymous-account', 'configuration')]


def test_reveal_unknown_outcome_refuses_before_host_contact(
    tmp_path: Path,
    make_repo: Callable[..., Path],
) -> None:
    paths, global_config, project, _destination, _commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    record = _candidate.load_candidate(paths, project.name)
    assert record is not None
    _candidate.save_candidate(
        paths,
        project.name,
        replace(record, state=_candidate.STATE_UNKNOWN_OUTCOME),
    )

    with pytest.raises(
        PubanonError, match='the outcome of the last publication is unknown'
    ) as exc_info:
        _lifecycle.reveal(paths, global_config, project)

    assert exc_info.value.recovery == 'pubanon publish --resolve'
    assert _gh_calls(tmp_path) == []


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
        _lifecycle.repo_create(paths, global_config, project, visibility='private', yes=True)

    recovery = exc_info.value.recovery
    assert recovery is not None
    assert escape(str(paths.config_dir / 'projects' / 'demo.toml')) in recovery
    assert _gh_calls(tmp_path) == []


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

    assert _gh_calls(tmp_path) == [_REPOSITORY_ARGS, _SETTINGS_ARGS]


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
    assert str(error) == 'the repository is now public but post-flip verification failed'
    assert error.fact == 'default_branch: expected main; actual trunk'
    assert error.recovery == 'fix the reported drift, then rerun pubanon repo reveal'


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
    assert 'the repository is now public but post-flip verification failed' in rendered
    assert term in rendered
    assert exc_info.value.recovery == ('fix the reported drift, then rerun pubanon repo reveal')


def test_verify_after_flip_wraps_canonical_coordinate_drift(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
) -> None:
    term = 'private-canary'
    paths, global_config, project, _destination, _commit = _published_fixture(
        tmp_path,
        make_repo,
    )
    assert project.settings is not None
    _config.save_project(paths, project)
    _responses(fake_gh, [_repository_response('public', owner=term)])

    with pytest.raises(PubanonError) as exc_info:
        _VERIFY_AFTER_FLIP(paths, global_config, project)

    error = exc_info.value
    assert str(error) == 'the repository is now public but post-flip verification failed'
    assert error.recovery == 'fix the reported drift, then rerun pubanon repo reveal'
    rendered = error.render()
    assert term in rendered
    assert _SET_VISIBILITY_ARGS not in _gh_calls(tmp_path)
    assert _gh_calls(tmp_path) == [_REPOSITORY_ARGS]
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
    store = _candidate.store_path(paths, project.name)
    store.rename(tmp_path / 'malformed-store-backup')
    store.mkdir()
    _responses(fake_gh, [_repository_response('public')])

    with pytest.raises(PubanonError) as exc_info:
        _VERIFY_AFTER_FLIP(paths, global_config, project)

    rendered = exc_info.value.render()
    assert str(exc_info.value) == (
        'the repository is now public but post-flip verification failed'
    )
    assert 'cannot read snapshot store ref' in rendered
    assert exc_info.value.recovery == ('fix the reported drift, then rerun pubanon repo reveal')


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
    assert _gh_calls(tmp_path) == []


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
        return 'yes'

    monkeypatch.setattr('builtins.input', _accept)

    assert _VERIFY_BEFORE_FLIP(paths, global_config, project, destination) == commit

    assert selections == [(paths, project.name, None)]
    assert check_calls == [(destination, commit, package.release_checks)]
    assert release_calls == [('pypi', 'demo', '2.0.0')]
    assert re.search(r'Package: +demo 2.0.0 is published', capsys.readouterr().out)
    assert _SET_VISIBILITY_ARGS not in _gh_calls(tmp_path)


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

    monkeypatch.setattr(_lifecycle, 'check_result', _check)

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
    assert _SET_VISIBILITY_ARGS not in _gh_calls(tmp_path)


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
    assert _gh_calls(tmp_path) == [_SETTINGS_ARGS]


def test_verify_before_flip_refuses_hygiene_finding_from_any_published_commit(
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
) -> None:
    term = 'style-canary'
    paths, global_config, project, destination, _commit = _published_fixture(
        tmp_path,
        make_repo,
        message=f'{term} release',
    )
    project = replace(project, hygiene_terms=(term,))
    assert project.settings is not None
    _responses(fake_gh, [_settings_response(project.settings)])

    with pytest.raises(PubanonError, match='sanitization and identity') as exc_info:
        _VERIFY_BEFORE_FLIP(paths, global_config, project, destination)

    rendered = exc_info.value.render()
    assert term in rendered
    assert exc_info.value.recovery == (
        'pubanon audit shows the findings; adjust the configuration or the '
        'published content decision. To replace all published history under the current '
        'publication identity, use `pubanon prepare --replace all`; existing clones and caches '
        'may retain the old history'
    )
    assert not any('/actions/' in ' '.join(call) for call in _gh_calls(tmp_path))


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
    _candidate.store_path(paths, project.name)
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
    assert not any('check-runs' in ' '.join(call) for call in _gh_calls(tmp_path))
    assert not any('/actions/' in ' '.join(call) for call in _gh_calls(tmp_path))


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
    assert _SET_VISIBILITY_ARGS not in _gh_calls(tmp_path)


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

    monkeypatch.setattr(_lifecycle, 'ls_remote', _branch_refs)

    with pytest.raises(PubanonError, match='saved published history') as exc_info:
        _VERIFY_BEFORE_FLIP(paths, global_config, project, destination)

    rendered = exc_info.value.render()
    assert term in rendered
    assert '\u202e' not in rendered
    assert exc_info.value.expected is not None
    assert exc_info.value.actual is not None
    assert 'branches=none' in exc_info.value.expected
    assert f'branches={term}\\u202ebackup=' in exc_info.value.actual
    assert _gh_calls(tmp_path) == []


@pytest.mark.parametrize(
    'case',
    [
        (False, 'become public with the repository', 'are already public'),
        (True, 'are already public', 'become public with the repository'),
    ],
)
def test_verify_before_flip_requires_interactive_disclosure_acknowledgment(
    case: tuple[bool, str, str],
    fake_gh: Path,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    already_public, expected_wording, excluded_wording = case
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
    disclosure_output: list[str] = []
    output_buffer = StringIO()

    def _decline(prompt: str) -> str:
        prompts.append(prompt)
        disclosure_output.append(output_buffer.getvalue())
        return 'no'

    monkeypatch.setattr('builtins.input', _decline)

    with (
        redirect_stdout(output_buffer),
        pytest.raises(OperationCanceled, match=r'Canceled\. Nothing was changed.'),
    ):
        _VERIFY_BEFORE_FLIP(
            paths, global_config, project, destination, already_public=already_public
        )

    assert len(prompts) == 1
    assert expected_wording in prompts[0]
    assert excluded_wording not in prompts[0]
    rendered = disclosure_output[0]
    assert 'matches GitHub; no sensitive terms or identity problems' in rendered
    assert 'Not checked by Pubanon: 1 workflow run and 1 artifact' in rendered
    assert 'Pubanon has not scanned Actions logs, artifact contents' in rendered
    assert 'https://github.com/anonymous-account/public-repo/actions/runs/101' in rendered
    assert 'https://github.com/anonymous-account/public-repo/actions' in rendered
    assert 'Saved inventory:' not in rendered
    assert not any('PATCH' in call for call in _gh_calls(tmp_path))


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
        return 'yes'

    monkeypatch.setattr('builtins.input', _accept)

    result = _VERIFY_BEFORE_FLIP(paths, global_config, project, destination)

    assert result == commit
    output = re.sub(r': +', ': ', capsys.readouterr().out)
    reports = (
        'Published history: matches GitHub; no sensitive terms or identity problems',
        'Required checks: none configured',
        'Package: not enabled',
        'Settings: match your configuration',
        'Not checked by Pubanon: 0 workflow runs and 0 artifacts',
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
        return 'yes'

    monkeypatch.setattr('builtins.input', _accept)

    assert _VERIFY_BEFORE_FLIP(paths, global_config, project, destination) == commit

    disclosure_path = paths.data_dir / 'projects' / project.name / 'disclosure.json'
    payload = json.loads(disclosure_path.read_text(encoding='utf-8'))
    assert payload['runs'] == [
        f'{index} {"build" if index <= 100 else "docs"}' for index in range(1, 151)
    ]
    assert payload['artifacts'] == ['201 package', '202 report']
    assert_private_mode(disclosure_path, 0o600)
    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'Not checked by Pubanon: 150 workflow runs and 2 artifacts' in output
    assert '  build: 100' in output
    assert '  docs: 50' in output
    assert escape(str(disclosure_path)) not in output
