import json
import os
import subprocess
import threading
from collections.abc import Callable, Iterable
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from pubanon._config import GlobalConfig, Paths, ProjectConfig, Settings
from pubanon._errors import ObservationTimeoutError, OperationCanceled, PubanonError
from pubanon._remote import _host
from pubanon._remote._host import _remote_refs
from pubanon._run import GitResult
from tests._git import git_env

_ACCOUNT_ARGS = ['api', 'user', '--jq', '.login']
_LOGIN_ARGS = [
    'auth',
    'login',
    '--hostname',
    'github.com',
    '--git-protocol',
    'https',
    '--web',
]
_REPOSITORY_ARGS = [
    'api',
    'repos/publication-owner/public-repo',
    '--jq',
    '{owner: .owner.login, repo: .name, visibility: .visibility}',
]
_SETTINGS_ARGS = [
    'api',
    'repos/publication-owner/public-repo',
    '--jq',
    (
        '{description: .description, default_branch: .default_branch, '
        'issues: .has_issues, wiki: .has_wiki, projects: .has_projects, '
        'pull_requests: .has_pull_requests, discussions: .has_discussions}'
    ),
]


class _UnauthorizedHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_response(401, 'transport-stderr-canary')
        self.send_header('WWW-Authenticate', 'Basic realm="pubanon-test"')
        self.end_headers()


def _configs(tmp_path: Path) -> tuple[GlobalConfig, ProjectConfig]:
    return (
        GlobalConfig(
            anon_name='Public Name',
            anon_email='public@example.invalid',
            accounts={'github': 'anonymous-account'},
            sensitive_terms=(),
        ),
        ProjectConfig(
            name='demo',
            source_path=tmp_path / 'source',
            host='github',
            owner='publication-owner',
            repo='public-repo',
            visibility='private',
            allow=(),
            deny=(),
            secret_exceptions=(),
            extra_terms=(),
            hygiene_terms=(),
            self_publication=False,
            required_checks=(),
        ),
    )


def _destination(
    *, account: str = 'anonymous-account', visibility: str = 'private'
) -> _host.Destination:
    return _host.Destination(
        adapter='github',
        owner='publication-owner',
        repo='public-repo',
        url='https://github.com/publication-owner/public-repo.git',
        account=account,
        visibility=visibility,
    )


def _disclosure_pass_responses(
    resource: str,
    collection: str,
    entries: tuple[tuple[int, str], ...],
    *,
    once: bool,
) -> list[dict[str, object]]:
    responses: list[dict[str, object]] = []
    for offset in range(0, max(len(entries), 1), 100):
        page = offset // 100 + 1
        page_entries = entries[offset : offset + 100]
        response: dict[str, object] = {
            'args': [
                'api',
                (
                    'repos/publication-owner/public-repo/actions/'
                    f'{resource}?per_page=100&page={page}'
                ),
            ],
            'stdout': json.dumps(
                {
                    'total_count': len(entries),
                    collection: [
                        {'id': entry_id, 'name': name} for entry_id, name in page_entries
                    ],
                }
            ),
        }
        if once:
            response['once'] = True
        responses.append(response)
    return responses


def _disclosure_responses(
    *,
    first_runs: tuple[tuple[int, str], ...] = (),
    second_runs: tuple[tuple[int, str], ...] | None = None,
    first_artifacts: tuple[tuple[int, str], ...] = (),
    second_artifacts: tuple[tuple[int, str], ...] | None = None,
) -> list[dict[str, object]]:
    if second_runs is None:
        second_runs = first_runs
    if second_artifacts is None:
        second_artifacts = first_artifacts
    responses: list[dict[str, object]] = []
    for resource, collection, first, second in (
        ('runs', 'workflow_runs', first_runs, second_runs),
        ('artifacts', 'artifacts', first_artifacts, second_artifacts),
    ):
        responses.extend(_disclosure_pass_responses(resource, collection, first, once=True))
        responses.extend(_disclosure_pass_responses(resource, collection, second, once=True))
    return responses


def _exception_graph(error: BaseException) -> tuple[BaseException, ...]:
    graph: list[BaseException] = []
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        graph.append(current)
        pending.extend(
            linked for linked in (current.__cause__, current.__context__) if linked is not None
        )
    return tuple(graph)


def _gh_calls(tmp_path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line) for line in (tmp_path / 'fake-gh-calls.jsonl').read_text().splitlines()
    ]


def _paths(tmp_path: Path) -> Paths:
    config_dir = tmp_path / 'config'
    data_dir = tmp_path / 'data'
    return Paths(config_dir, data_dir, config_dir / 'config.toml', data_dir / 'registry.json')


def _release(tag: str, *, title: str = 'Page', notes: str = 'Notes\n') -> dict[str, object]:
    return {
        'tag_name': tag,
        'name': title,
        'body': notes,
        'html_url': f'https://github.com/publication-owner/public-repo/releases/tag/{tag}',
        'draft': False,
        'prerelease': False,
    }


def _write_responses(path: Path, responses: list[dict[str, object]]) -> None:
    path.write_text(json.dumps(responses))


def test_authenticated_account_hides_malformed_response(fake_gh: Path) -> None:
    _write_responses(fake_gh, [{'args': _ACCOUNT_ARGS, 'stdout': 'first\nsecond\n'}])

    with pytest.raises(PubanonError) as exc_info:
        _host.authenticated_account()

    assert str(exc_info.value) == 'GitHub authentication could not be verified'
    assert 'first' not in exc_info.value.render()
    assert 'second' not in exc_info.value.render()


def test_authenticated_account_hides_missing_executable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_executable = str(tmp_path / 'private-gh-canary')
    monkeypatch.setattr(_host, '_GH', private_executable)

    with pytest.raises(PubanonError) as exc_info:
        _host.authenticated_account()

    assert str(exc_info.value) == 'GitHub authentication could not be verified'
    assert private_executable not in exc_info.value.render()


def test_authenticated_account_hides_timeout_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    def _timeout_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(
            'private-executable-canary',
            2.5,
            output=b'private-account-canary',
            stderr=b'ghp_token-canary',
        )

    monkeypatch.setattr(_host.subprocess, 'run', _timeout_run)

    with pytest.raises(PubanonError) as exc_info:
        _host.authenticated_account(timeout=2.5)

    graph = _exception_graph(exc_info.value)
    rendered = repr([vars(error) for error in graph]) + exc_info.value.render()
    assert str(exc_info.value) == 'GitHub authentication could not be verified'
    assert graph == (exc_info.value,)
    assert 'private-executable-canary' not in rendered
    assert 'private-account-canary' not in rendered
    assert 'ghp_token-canary' not in rendered


def test_authenticated_account_preserves_cancellation_private_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = OperationCanceled('cancelled', actual='private-account')

    def _cancel(*_args: object, **_kwargs: object) -> str:
        raise error

    monkeypatch.setattr(_host, '_authenticated_account', _cancel)
    with pytest.raises(OperationCanceled) as raised:
        _host.authenticated_account()
    assert raised.value is error
    assert error.actual == 'private-account'
    assert 'private-account' in error.render()


def test_authenticated_account_reports_escaped_failed_probe_streams(fake_gh: Path) -> None:
    _write_responses(
        fake_gh,
        [
            {
                'args': _ACCOUNT_ARGS,
                'returncode': 1,
                'stdout': 'private-account-canary\n',
                'stderr': 'ghp_token-canary\x1b[31m\\path\n',
            }
        ],
    )

    with pytest.raises(PubanonError) as exc_info:
        _host.authenticated_account()

    rendered = exc_info.value.render()
    assert str(exc_info.value) == 'GitHub authentication could not be verified'
    assert 'logged out' not in rendered
    assert 'private-account-canary' in rendered
    assert r'ghp_token-canary\x1b[31m\\path' in rendered
    assert 'exit status 1' in rendered
    assert '\x1b' not in rendered


def test_authenticated_account_returns_verified_login(fake_gh: Path) -> None:
    _write_responses(fake_gh, [{'args': _ACCOUNT_ARGS, 'stdout': 'verified-account\n'}])

    assert _host.authenticated_account() == 'verified-account'


def test_check_recovery_accepts_one_pass_verdicts() -> None:
    verdicts = (
        verdict
        for verdict in (
            _host.CheckVerdict('CI', 'pending', 'in_progress'),
            _host.CheckVerdict('release', 'missing', ''),
        )
    )

    assert _host.check_recovery(verdicts, retry='retry the operation') == (
        'check the configured name and workflow trigger, then retry the operation'
    )


def test_check_result_percent_encodes_check_name(fake_gh: Path, tmp_path: Path) -> None:
    path = (
        'repos/publication-owner/public-repo/commits/'
        f'{"a" * 40}/check-runs?check_name=Quality+gate&filter=latest&per_page=100'
    )
    _write_responses(
        fake_gh,
        [
            {
                'args': ['api', path],
                'stdout': '{"total_count":0,"check_runs":[]}\n',
            }
        ],
    )

    _host.check_result(('publication-owner', 'public-repo'), 'a' * 40, 'Quality gate')

    call = json.loads((tmp_path / 'fake-gh-calls.jsonl').read_text())
    assert call['args'] == ['api', path]


def test_check_result_refuses_mismatched_commit(fake_gh: Path) -> None:
    args = [
        'api',
        (
            'repos/publication-owner/public-repo/commits/'
            f'{"a" * 40}/check-runs?check_name=CI&filter=latest&per_page=100'
        ),
    ]
    payload = {
        'total_count': 1,
        'check_runs': [
            {
                'name': 'CI',
                'head_sha': 'b' * 40,
                'status': 'completed',
                'conclusion': 'success',
            }
        ],
    }
    _write_responses(fake_gh, [{'args': args, 'stdout': json.dumps(payload) + '\n'}])

    with pytest.raises(PubanonError, match='GitHub returned check results Pubanon could not read'):
        _host.check_result(('publication-owner', 'public-repo'), 'a' * 40, 'CI')


def test_create_repository_owner_lookup_failure_is_not_creation_uncertainty(
    fake_gh: Path,
    tmp_path: Path,
) -> None:
    owner_args = ['api', 'users/publication-owner', '--jq', '.type']
    _write_responses(
        fake_gh,
        [
            {
                'args': owner_args,
                'returncode': 1,
                'stderr': 'simulated owner lookup failure\n',
            }
        ],
    )

    with pytest.raises(PubanonError) as exc_info:
        _host.create_repository(_destination(), None)

    assert type(exc_info.value) is PubanonError
    assert _gh_calls(tmp_path) == [{'args': owner_args, 'stdin': ''}]


def test_create_repository_post_failure_is_creation_uncertainty(
    fake_gh: Path,
    tmp_path: Path,
) -> None:
    term = 'private-canary'
    args = [
        'api',
        '-X',
        'POST',
        'user/repos',
        '-f',
        'name=public-repo',
        '-F',
        'private=true',
    ]
    _write_responses(
        fake_gh,
        [
            {
                'args': args,
                'returncode': 1,
                'stderr': f'simulated {term} creation failure\n',
            }
        ],
    )

    with pytest.raises(_host.RepositoryCreationUncertainError) as exc_info:
        _host.create_repository(_destination(account='publication-owner'), None)

    assert str(exc_info.value) == f'simulated {term} creation failure'
    assert _gh_calls(tmp_path) == [{'args': args, 'stdin': ''}]


def test_create_repository_posts_empty_private_user_repository(
    fake_gh: Path,
    tmp_path: Path,
) -> None:
    args = [
        'api',
        '-X',
        'POST',
        'user/repos',
        '-f',
        'name=public-repo',
        '-F',
        'private=true',
    ]
    _write_responses(fake_gh, [{'args': args}])

    _host.create_repository(_destination(account='publication-owner'), None)

    assert _gh_calls(tmp_path) == [{'args': args, 'stdin': ''}]
    assert 'visibility' not in ' '.join(args)
    assert 'auto_init' not in ' '.join(args)


def test_create_repository_posts_org_visibility_and_settings(
    fake_gh: Path,
    tmp_path: Path,
) -> None:
    owner_args = ['api', 'users/publication-owner', '--jq', '.type']
    create_args = [
        'api',
        '-X',
        'POST',
        'orgs/publication-owner/repos',
        '-f',
        'name=public-repo',
        '-F',
        'private=false',
        '-f',
        'visibility=public',
        '-f',
        'description=Public repository',
        '-F',
        'has_issues=true',
        '-F',
        'has_wiki=false',
        '-F',
        'has_projects=true',
    ]
    patch_args = [
        'api',
        '-X',
        'PATCH',
        'repos/publication-owner/public-repo',
        '-F',
        'has_pull_requests=false',
        '-F',
        'has_discussions=false',
    ]
    _write_responses(
        fake_gh,
        [
            {'args': owner_args, 'stdout': 'Organization\n'},
            {'args': create_args},
            {'args': patch_args},
            {
                'args': _SETTINGS_ARGS,
                'stdout': json.dumps(
                    {
                        'description': 'Public repository',
                        'default_branch': 'master',
                        'issues': True,
                        'wiki': False,
                        'projects': True,
                        'pull_requests': False,
                        'discussions': False,
                    }
                ),
            },
        ],
    )

    _host.create_repository(
        _destination(visibility='public'),
        Settings(
            description='Public repository',
            default_branch='main',
            issues=True,
            wiki=False,
            projects=True,
            pull_requests=False,
            discussions=False,
        ),
    )

    assert _gh_calls(tmp_path) == [
        {'args': owner_args, 'stdin': ''},
        {'args': create_args, 'stdin': ''},
        {'args': patch_args, 'stdin': ''},
        {'args': _SETTINGS_ARGS, 'stdin': ''},
    ]
    assert 'default_branch' not in ' '.join(create_args)
    assert 'auto_init' not in ' '.join(create_args)


def test_create_repository_refuses_nonorganization_owner_before_post(
    fake_gh: Path,
    tmp_path: Path,
) -> None:
    owner_args = ['api', 'users/publication-owner', '--jq', '.type']
    _write_responses(fake_gh, [{'args': owner_args, 'stdout': 'User\n'}])

    with pytest.raises(PubanonError, match='cannot create under the configured owner'):
        _host.create_repository(_destination(), None)

    assert _gh_calls(tmp_path) == [{'args': owner_args, 'stdin': ''}]


def test_delete_repository_accepts_success(fake_gh: Path, tmp_path: Path) -> None:
    args = ['api', '-X', 'DELETE', 'repos/publication-owner/public-repo']
    _write_responses(fake_gh, [{'args': args}])

    _host.delete_repository('publication-owner', 'public-repo')

    assert _gh_calls(tmp_path) == [{'args': args, 'stdin': ''}]


def test_delete_repository_refuses_absent_repository(fake_gh: Path) -> None:
    args = ['api', '-X', 'DELETE', 'repos/publication-owner/public-repo']
    _write_responses(
        fake_gh,
        [
            {
                'args': args,
                'returncode': 1,
                'stdout': '{"message":"Not Found","status":"404"}\n',
            }
        ],
    )

    with pytest.raises(PubanonError, match='repository is absent or inaccessible') as exc_info:
        _host.delete_repository('publication-owner', 'public-repo')

    assert exc_info.value.recovery == (
        'verify repository access and state, then retry `pubanon stage delete`'
    )


def test_delete_repository_reports_scope_as_possible_cause(fake_gh: Path) -> None:
    args = ['api', '-X', 'DELETE', 'repos/publication-owner/public-repo']
    _write_responses(
        fake_gh,
        [
            {
                'args': args,
                'returncode': 1,
                'stdout': '{"message":"Forbidden","status":"403"}\n',
            }
        ],
    )

    with pytest.raises(PubanonError, match='staging deletion is not authorized') as exc_info:
        _host.delete_repository('publication-owner', 'public-repo')

    assert exc_info.value.fact == 'HTTP 403: GitHub refused authorization'
    assert 'not granted' not in exc_info.value.render()
    assert exc_info.value.recovery == (
        'review the active account and repository permissions; a missing delete_repo '
        'scope is one possible cause. You may delete the staging repository manually '
        'and use pubanon stage forget to remove local tracking'
    )


def test_delete_repository_reports_unclassified_failure(fake_gh: Path) -> None:
    args = ['api', '-X', 'DELETE', 'repos/publication-owner/public-repo']
    _write_responses(
        fake_gh,
        [
            {
                'args': args,
                'returncode': 1,
                'stderr': 'deletion failed for private-canary\n',
            }
        ],
    )

    with pytest.raises(PubanonError) as exc_info:
        _host.delete_repository('publication-owner', 'public-repo')

    assert 'private-canary' in exc_info.value.render()


def test_disclosure_accepts_reordered_passes_and_sorts_by_id(fake_gh: Path) -> None:
    _write_responses(
        fake_gh,
        _disclosure_responses(
            first_runs=((2, 'publish'), (1, 'build')),
            second_runs=((1, 'build'), (2, 'publish')),
            first_artifacts=((12, 'source'), (11, 'wheel')),
            second_artifacts=((11, 'wheel'), (12, 'source')),
        ),
    )

    result = _host.disclosure('publication-owner', 'public-repo')

    assert result.runs == (
        _host.DisclosureEntry(1, 'build'),
        _host.DisclosureEntry(2, 'publish'),
    )
    assert result.artifacts == (
        _host.DisclosureEntry(11, 'wheel'),
        _host.DisclosureEntry(12, 'source'),
    )


def test_disclosure_accepts_stable_multi_page_inventory(
    fake_gh: Path,
    tmp_path: Path,
) -> None:
    runs = tuple((index, f'workflow-{index}') for index in range(1, 151))
    artifacts = tuple((index, f'artifact-{index}') for index in range(701, 851))
    _write_responses(
        fake_gh,
        _disclosure_responses(first_runs=runs, first_artifacts=artifacts),
    )

    result = _host.disclosure('publication-owner', 'public-repo')

    assert result.runs == tuple(
        _host.DisclosureEntry(index, f'workflow-{index}') for index in range(1, 151)
    )
    assert result.artifacts == tuple(
        _host.DisclosureEntry(index, f'artifact-{index}') for index in range(701, 851)
    )
    assert [call['args'] for call in _gh_calls(tmp_path)] == [
        [
            'api',
            'repos/publication-owner/public-repo/actions/runs?per_page=100&page=1',
        ],
        [
            'api',
            'repos/publication-owner/public-repo/actions/runs?per_page=100&page=2',
        ],
        [
            'api',
            'repos/publication-owner/public-repo/actions/runs?per_page=100&page=1',
        ],
        [
            'api',
            'repos/publication-owner/public-repo/actions/runs?per_page=100&page=2',
        ],
        [
            'api',
            'repos/publication-owner/public-repo/actions/artifacts?per_page=100&page=1',
        ],
        [
            'api',
            'repos/publication-owner/public-repo/actions/artifacts?per_page=100&page=2',
        ],
        [
            'api',
            'repos/publication-owner/public-repo/actions/artifacts?per_page=100&page=1',
        ],
        [
            'api',
            'repos/publication-owner/public-repo/actions/artifacts?per_page=100&page=2',
        ],
    ]
    assert json.loads(fake_gh.read_text()) == []


def test_disclosure_allows_equal_ids_across_resource_namespaces(fake_gh: Path) -> None:
    _write_responses(
        fake_gh,
        _disclosure_responses(
            first_runs=((7, 'build'),),
            first_artifacts=((7, 'wheel'),),
        ),
    )

    assert _host.disclosure('publication-owner', 'public-repo') == (
        (_host.DisclosureEntry(7, 'build'),),
        (_host.DisclosureEntry(7, 'wheel'),),
    )


def test_disclosure_refuses_duplicate_id_on_later_page_in_second_pass(fake_gh: Path) -> None:
    first = tuple((index, f'entry-{index}') for index in range(1, 102))
    second = (*first[:100], (1, 'duplicate'))
    _write_responses(
        fake_gh,
        _disclosure_responses(first_runs=first, second_runs=second),
    )

    with pytest.raises(PubanonError, match='invalid disclosure response'):
        _host.disclosure('publication-owner', 'public-repo')


def test_disclosure_refuses_short_page_before_total(fake_gh: Path) -> None:
    short_page = [{'id': index, 'name': f'workflow-{index}'} for index in range(1, 100)]
    _write_responses(
        fake_gh,
        [
            {
                'args': [
                    'api',
                    'repos/publication-owner/public-repo/actions/runs?per_page=100&page=1',
                ],
                'stdout': json.dumps({'total_count': 150, 'workflow_runs': short_page}),
            }
        ],
    )

    with pytest.raises(PubanonError, match='invalid disclosure response') as error:
        _host.disclosure('publication-owner', 'public-repo')
    assert 'workflow runs for publication-owner/public-repo' in str(error.value)
    assert error.value.fact == 'The GitHub Actions inventory is unknown.'
    assert error.value.recovery is not None


def test_fake_gh_autouse_records_and_refuses_unexpected_call(tmp_path: Path) -> None:
    args = ['api', 'unexpected']

    with pytest.raises(PubanonError) as exc_info:
        _host._gh(args)

    assert str(exc_info.value) == 'unexpected fake gh invocation: ["api", "unexpected"]'
    assert _gh_calls(tmp_path) == [{'args': args, 'stdin': ''}]


def test_fake_gh_consume_once_response_disappears(fake_gh: Path) -> None:
    args = _REPOSITORY_ARGS
    _write_responses(
        fake_gh,
        [
            {
                'args': args,
                'returncode': 1,
                'stdout': '{"status":"404"}\n',
                'once': True,
            },
            {
                'args': args,
                'stdout': (
                    '{"owner":"publication-owner","repo":"public-repo","visibility":"private"}\n'
                ),
            },
        ],
    )

    assert not _host.repository_visible('publication-owner', 'public-repo')
    assert _host.repository_visible('publication-owner', 'public-repo')


def test_fetch_failure_preserves_existing_managed_refs(
    make_repo: Callable[..., Path],
    tmp_path: Path,
) -> None:
    source = make_repo('source')
    old = (
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
    subprocess.run(['git', 'clone', '--bare', '-q', source, remote], check=True, env=git_env())
    for ref in ('refs/heads/main', 'refs/tags/v1.0.0'):
        subprocess.run(['git', '-C', remote, 'update-ref', ref, old], check=True, env=git_env())
    store = tmp_path / 'store.git'
    subprocess.run(['git', 'init', '--bare', '-q', store], check=True, env=git_env())
    destination = _host.Destination(
        adapter='github',
        owner='publication-owner',
        repo='public-repo',
        url=remote.as_uri(),
        account='anonymous-account',
        visibility='private',
    )
    refspecs = ['+refs/heads/main:refs/heads/main', '+refs/tags/*:refs/tags/*']
    _host.fetch(destination, store, refspecs)

    (source / 'new.txt').write_text('new\n')
    subprocess.run(['git', '-C', source, 'add', 'new.txt'], check=True, env=git_env())
    subprocess.run(
        [
            'git',
            '-C',
            source,
            '-c',
            'user.name=Test User',
            '-c',
            'user.email=test@example.invalid',
            'commit',
            '-qm',
            'new',
        ],
        check=True,
        env=git_env(),
    )
    new = (
        subprocess.run(
            ['git', '-C', source, 'rev-parse', 'HEAD'],
            check=True,
            capture_output=True,
            env=git_env(),
        )
        .stdout.decode()
        .strip()
    )
    subprocess.run(
        [
            'git',
            '-C',
            remote,
            'fetch',
            '-q',
            str(source),
            f'+{new}:refs/heads/main',
            f'+{new}:refs/tags/v1.0.0',
        ],
        check=True,
        env=git_env(),
    )
    lock = store / 'refs' / 'tags' / 'v1.0.0.lock'
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.touch()

    try:
        with pytest.raises(PubanonError):
            _host.fetch(destination, store, refspecs)
    finally:
        lock.unlink()

    for ref in ('refs/heads/main', 'refs/tags/v1.0.0'):
        assert (
            subprocess.run(
                ['git', '-C', store, 'rev-parse', ref],
                check=True,
                capture_output=True,
                env=git_env(),
            )
            .stdout.decode()
            .strip()
            == old
        )


def test_gh_metadata_pins_github_host(
    fake_gh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = str(tmp_path / 'distinct-gh-config')
    monkeypatch.setenv('GH_CONFIG_DIR', config_dir)
    monkeypatch.setenv('GH_HOST', 'evil.example')
    monkeypatch.setenv('PUBANON_FAKE_GH_RECORD_HOST', '1')
    _write_responses(
        fake_gh,
        [{'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'}],
    )

    assert _host._authenticated_account() == 'anonymous-account'
    calls_path = Path(os.environ['PUBANON_FAKE_GH_CALLS'])
    last = json.loads(calls_path.read_text().splitlines()[-1])
    assert last['gh_config_dir'] == config_dir
    assert last['gh_host'] == 'github.com'


def test_gh_timeout_discards_child_streams(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[float | None] = []

    def _timeout_run(*_args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        timeout = kwargs.get('timeout')
        assert isinstance(timeout, float)
        calls.append(timeout)
        raise subprocess.TimeoutExpired(
            'gh',
            timeout,
            output=b'stdout-private-canary',
            stderr=b'stderr-private-canary',
        )

    monkeypatch.setattr(_host.subprocess, 'run', _timeout_run)

    with pytest.raises(ObservationTimeoutError) as exc_info:
        _host._gh(['api', 'user'], timeout=2.5)

    assert type(exc_info.value).__name__ == 'ObservationTimeoutError'
    assert exc_info.value.stdout == b''
    assert exc_info.value.stderr == b''
    graph = _exception_graph(exc_info.value)
    assert graph == (exc_info.value,)
    assert 'private-canary' not in repr([vars(error) for error in graph])
    assert calls == [2.5]


@pytest.mark.parametrize(
    'response',
    [
        _release('v1.0.0') | {'name': None, 'body': None},
        {key: value for key, value in _release('v1.0.0').items() if key != 'body'},
    ],
)
def test_list_releases_normalizes_nullable_content(
    fake_gh: Path,
    response: dict[str, object],
) -> None:
    _write_responses(
        fake_gh,
        [
            {
                'args': [
                    'api',
                    '--method',
                    'GET',
                    'repos/publication-owner/public-repo/releases?per_page=30&page=1',
                ],
                'stdout': json.dumps([response]),
            }
        ],
    )

    (release,) = _host.list_releases(_destination(), limit=30)

    assert release.title == (response.get('name') or '')
    assert release.notes == ''


@pytest.mark.parametrize(
    'response',
    [
        {'tag_name': 'v1.0.0', 'name': 'Page', 'body': 'Notes'},
        _release('v1.0.0') | {'draft': 'false'},
        _release('v1.0.0') | {'body': 42},
        {key: value for key, value in _release('v1.0.0').items() if key != 'name'},
    ],
)
def test_list_releases_rejects_malformed_page(fake_gh: Path, response: dict[str, object]) -> None:
    _write_responses(
        fake_gh,
        [
            {
                'args': [
                    'api',
                    '--method',
                    'GET',
                    'repos/publication-owner/public-repo/releases?per_page=30&page=1',
                ],
                'stdout': json.dumps([response]),
            }
        ],
    )

    with pytest.raises(PubanonError, match='invalid release response'):
        _host.list_releases(_destination(), limit=30)


def test_list_releases_respects_limit_across_pages(fake_gh: Path, tmp_path: Path) -> None:
    first = [_release(f'v1.0.{index}') for index in range(100)]
    second = [_release(f'v1.0.{index}', title='Second') for index in range(100, 104)]
    second[0]['body'] = 'Second notes\n'
    _write_responses(
        fake_gh,
        [
            {
                'args': [
                    'api',
                    '--method',
                    'GET',
                    'repos/publication-owner/public-repo/releases?per_page=100&page=1',
                ],
                'stdout': json.dumps(first),
            },
            {
                'args': [
                    'api',
                    '--method',
                    'GET',
                    'repos/publication-owner/public-repo/releases?per_page=100&page=2',
                ],
                'stdout': json.dumps(second),
            },
        ],
    )

    releases = _host.list_releases(_destination(), limit=101)

    assert len(releases) == 101
    assert [release.tag for release in releases] == [f'v1.0.{index}' for index in range(101)]
    assert releases[-1] == _host.GitHubRelease(
        tag='v1.0.100',
        title='Second',
        notes='Second notes\n',
        url='https://github.com/publication-owner/public-repo/releases/tag/v1.0.100',
        draft=False,
        prerelease=False,
    )
    assert len(_gh_calls(tmp_path)) == 2


def test_login_inherits_terminal_with_fixed_safe_arguments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    config_dir = str(tmp_path / 'gh-config')
    monkeypatch.setenv('GH_CONFIG_DIR', config_dir)

    def _run(
        args: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[bytes]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(_host.subprocess, 'run', _run)

    _host.login()

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ['gh', *_LOGIN_ARGS]
    assert kwargs['check'] is False
    env = kwargs['env']
    assert isinstance(env, dict)
    assert env['GH_CONFIG_DIR'] == config_dir
    assert env['GH_HOST'] == 'github.com'
    assert (
        not {
            'capture_output',
            'input',
            'shell',
            'stderr',
            'stdin',
            'stdout',
            'text',
        }
        & kwargs.keys()
    )
    assert 'setup-git' not in args
    assert 'token' not in args


def test_login_preserves_interruption_status_without_chaining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise KeyboardInterrupt

    monkeypatch.setattr(_host.subprocess, 'run', _run)

    with pytest.raises(OperationCanceled) as exc_info:
        _host.login()

    assert str(exc_info.value) == 'Canceled.'
    assert _exception_graph(exc_info.value) == (exc_info.value,)


def test_login_reports_nonzero_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    def _run(args: list[str], **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, 1)

    monkeypatch.setattr(_host.subprocess, 'run', _run)

    with pytest.raises(PubanonError, match='GitHub login did not complete'):
        _host.login()


def test_ls_remote_ignores_repository_local_url_rewrite(
    make_repo: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo('source')
    real = tmp_path / 'real.git'
    decoy = tmp_path / 'decoy.git'
    for remote in (real, decoy):
        subprocess.run(['git', 'init', '--bare', '-q', remote], check=True, env=git_env())
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
    subprocess.run(
        ['git', '-C', real, 'fetch', '-q', str(source), f'{commit}:refs/heads/main'],
        check=True,
        env=git_env(),
    )
    subprocess.run(
        ['git', '-C', source, 'config', f'url.{decoy}.insteadOf', str(real)],
        check=True,
        env=git_env(),
    )
    monkeypatch.chdir(source)
    destination = _host.Destination(
        adapter='github',
        owner='publication-owner',
        repo='public-repo',
        url=str(real),
        account='anonymous-account',
        visibility='private',
    )

    refs = _host.ls_remote(destination, _paths(tmp_path), 'demo')

    assert refs.main == commit


def test_ls_remote_reads_only_main_and_tags_despite_planted_pull_ref(
    make_repo: Callable[..., Path],
    tmp_path: Path,
) -> None:
    source = make_repo('source')
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
        ],
        check=True,
        env=git_env(),
    )
    for ref in ('refs/tags/v1.0.0', 'refs/pull/1/head'):
        subprocess.run(['git', '-C', remote, 'update-ref', ref, commit], check=True, env=git_env())
    destination = _host.Destination(
        adapter='github',
        owner='publication-owner',
        repo='public-repo',
        url=remote.as_uri(),
        account='anonymous-account',
        visibility='private',
    )

    refs = _host.ls_remote(destination, _paths(tmp_path), 'demo')

    assert refs == _host.RemoteRefs(commit, {'v1.0.0': commit})


def test_ls_remote_reads_refs_from_created_advisory_store(
    make_repo: Callable[..., Path],
    tmp_path: Path,
) -> None:
    source = make_repo('source')
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
        ],
        check=True,
        env=git_env(),
    )
    paths = _paths(tmp_path)
    destination = _host.Destination(
        adapter='github',
        owner='publication-owner',
        repo='public-repo',
        url=remote.as_uri(),
        account='anonymous-account',
        visibility='private',
    )

    refs = _host.ls_remote(destination, paths, 'demo')

    assert refs == _host.RemoteRefs(commit, {})
    store = paths.data_dir / 'projects' / 'demo' / 'store.git'
    assert (
        subprocess.run(
            ['git', '-C', store, 'rev-parse', '--is-bare-repository'],
            check=True,
            capture_output=True,
            env=git_env(),
        ).stdout
        == b'true\n'
    )


def test_ls_remote_records_destination_branch_other_than_main(
    make_repo: Callable[..., Path],
    tmp_path: Path,
) -> None:
    source = make_repo('source')
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
        owner='publication-owner',
        repo='public-repo',
        url=remote.as_uri(),
        account='anonymous-account',
        visibility='private',
    )

    refs = _host.ls_remote(destination, _paths(tmp_path), 'demo')

    assert refs == _host.RemoteRefs(commit, {}, (('private-backup', commit),))


def test_new_gh_reads_pin_github_host(
    fake_gh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('GH_HOST', 'evil.example')
    monkeypatch.setenv('PUBANON_FAKE_GH_RECORD_HOST', '1')
    check_args = [
        'api',
        (
            'repos/publication-owner/public-repo/commits/'
            f'{"a" * 40}/check-runs?check_name=CI&filter=latest&per_page=100'
        ),
    ]
    runs_args = [
        'api',
        'repos/publication-owner/public-repo/actions/runs?per_page=100&page=1',
    ]
    artifacts_args = [
        'api',
        'repos/publication-owner/public-repo/actions/artifacts?per_page=100&page=1',
    ]
    _write_responses(
        fake_gh,
        [
            {
                'args': _REPOSITORY_ARGS,
                'stdout': (
                    '{"owner":"publication-owner","repo":"public-repo","visibility":"private"}\n'
                ),
            },
            {
                'args': _SETTINGS_ARGS,
                'stdout': (
                    '{"description":null,"default_branch":"main",'
                    '"issues":true,"wiki":false,"projects":true,'
                    '"pull_requests":false,"discussions":false}\n'
                ),
            },
            {
                'args': check_args,
                'stdout': '{"total_count":0,"check_runs":[]}\n',
            },
            {
                'args': runs_args,
                'stdout': '{"total_count":0,"workflow_runs":[]}\n',
                'once': True,
            },
            {
                'args': runs_args,
                'stdout': '{"total_count":0,"workflow_runs":[]}\n',
            },
            {
                'args': artifacts_args,
                'stdout': '{"total_count":0,"artifacts":[]}\n',
                'once': True,
            },
            {
                'args': artifacts_args,
                'stdout': '{"total_count":0,"artifacts":[]}\n',
            },
        ],
    )

    assert _host.repository_visible('publication-owner', 'public-repo')
    assert _host.repository_settings('publication-owner', 'public-repo').default_branch == 'main'
    assert (
        _host.check_result(('publication-owner', 'public-repo'), 'a' * 40, 'CI').state == 'missing'
    )
    assert _host.disclosure('publication-owner', 'public-repo') == (
        (),
        (),
    )

    calls = [
        json.loads(line) for line in (tmp_path / 'fake-gh-calls.jsonl').read_text().splitlines()
    ]
    assert len(calls) == 7
    assert {call['gh_host'] for call in calls} == {'github.com'}


def test_push_invokes_gh_credential_helper_and_reports_transport_diagnostics(
    fake_gh: Path,
    make_repo: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = str(tmp_path / 'distinct-gh-config')
    monkeypatch.setenv('GH_CONFIG_DIR', config_dir)
    monkeypatch.setenv('GH_HOST', 'evil.example')
    monkeypatch.setenv('PUBANON_FAKE_GH_RECORD_HOST', '1')
    source = make_repo('source')
    _write_responses(
        fake_gh,
        [
            {
                'args': ['auth', 'git-credential', 'get'],
                'stdout': 'username=helper-stdout-canary\npassword=test-password\n',
                'stderr': 'helper-stderr-canary\n',
            }
        ],
    )
    server = ThreadingHTTPServer(('127.0.0.1', 0), _UnauthorizedHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        port = server.server_address[1]
        destination = _host.Destination(
            adapter='github',
            owner='publication-owner',
            repo='public-repo',
            url=f'http://127.0.0.1:{port}/transport-stdout-canary.git',
            account='anonymous-account',
            visibility='private',
        )

        with pytest.raises(PubanonError) as exc_info:
            _host.push(destination, source / '.git', ['HEAD:refs/heads/main'])
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    assert str(exc_info.value) == 'Git push failed'
    assert (exc_info.value.fact or '').startswith('exit status 128: ')
    assert 'helper-stderr-canary' in exc_info.value.render()
    assert 'transport-stdout-canary.git' in exc_info.value.render()
    calls = [
        json.loads(line) for line in (tmp_path / 'fake-gh-calls.jsonl').read_text().splitlines()
    ]
    helper_calls = [call for call in calls if call['args'][:2] == ['auth', 'git-credential']]
    assert helper_calls
    assert {call['gh_config_dir'] for call in helper_calls} == {config_dir}
    assert {call['gh_host'] for call in helper_calls} == {'github.com'}


def test_read_release_finds_exact_tag_after_full_page(fake_gh: Path) -> None:
    first = [_release(f'v1.0.{index}') for index in range(100)]
    target = _release('v2.0.0', title='Target')
    _write_responses(
        fake_gh,
        [
            {
                'args': [
                    'api',
                    '--method',
                    'GET',
                    'repos/publication-owner/public-repo/releases?per_page=100&page=1',
                ],
                'stdout': json.dumps(first),
            },
            {
                'args': [
                    'api',
                    '--method',
                    'GET',
                    'repos/publication-owner/public-repo/releases?per_page=100&page=2',
                ],
                'stdout': json.dumps([_release('v2.0.00'), target]),
            },
        ],
    )

    release = _host.read_release(_destination(), 'v2.0.0')

    assert release is not None
    assert release.tag == 'v2.0.0'
    assert release.title == 'Target'


def test_read_release_propagates_host_failure(fake_gh: Path) -> None:
    _write_responses(
        fake_gh,
        [
            {
                'args': [
                    'api',
                    '--method',
                    'GET',
                    'repos/publication-owner/public-repo/releases?per_page=100&page=1',
                ],
                'returncode': 1,
                'stderr': 'authentication failed',
            }
        ],
    )

    with pytest.raises(PubanonError, match='authentication failed'):
        _host.read_release(_destination(), 'v1.0.0')


def test_read_release_returns_none_only_after_successful_exhaustion(fake_gh: Path) -> None:
    _write_responses(
        fake_gh,
        [
            {
                'args': [
                    'api',
                    '--method',
                    'GET',
                    'repos/publication-owner/public-repo/releases?per_page=100&page=1',
                ],
                'stdout': json.dumps([_release('v1.0.0')]),
            }
        ],
    )

    assert _host.read_release(_destination(), 'v2.0.0') is None


def test_read_remote_refs_pins_credential_helper_to_github_host(
    fake_gh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = str(tmp_path / 'distinct-gh-config')
    monkeypatch.setenv('GH_CONFIG_DIR', config_dir)
    monkeypatch.setenv('GH_HOST', 'evil.example')
    monkeypatch.setenv('PUBANON_FAKE_GH_RECORD_HOST', '1')
    _write_responses(
        fake_gh,
        [
            {
                'args': ['auth', 'git-credential', 'get'],
                'stdout': 'username=test-user\npassword=test-password\n',
            }
        ],
    )
    server = ThreadingHTTPServer(('127.0.0.1', 0), _UnauthorizedHandler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    try:
        destination = _host.Destination(
            adapter='github',
            owner='publication-owner',
            repo='public-repo',
            url=f'http://127.0.0.1:{server.server_address[1]}/repo.git',
            account='anonymous-account',
            visibility='private',
        )

        with pytest.raises(PubanonError, match='Git ls-remote failed'):
            _host.read_remote_refs(destination)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()

    calls = [
        json.loads(line) for line in (tmp_path / 'fake-gh-calls.jsonl').read_text().splitlines()
    ]
    helper_calls = [call for call in calls if call['args'][:2] == ['auth', 'git-credential']]
    assert helper_calls
    assert {call['gh_config_dir'] for call in helper_calls} == {config_dir}
    assert {call['gh_host'] for call in helper_calls} == {'github.com'}


def test_read_remote_refs_propagates_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def _timeout(*_args: object, **kwargs: object) -> GitResult:
        assert kwargs['timeout'] == 2.5
        raise ObservationTimeoutError

    monkeypatch.setattr(_host, 'run_credential_child', _timeout)

    with pytest.raises(ObservationTimeoutError):
        _host.read_remote_refs(_destination(), timeout=2.5)


@pytest.mark.parametrize('operation', ['create', 'edit'])
def test_release_write_uses_verified_tag_and_retained_stdin(
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []

    def _run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, b'', b'')

    monkeypatch.setattr(_host.subprocess, 'run', _run)
    notes = b'Line one\r\nLine two\n'

    if operation == 'create':
        _host.create_release(_destination(), 'v1.0.0', title='-leading', notes=notes)
    else:
        _host.edit_release(_destination(), 'v1.0.0', title='-leading', notes=notes)

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == [
        'gh',
        'release',
        operation,
        'v1.0.0',
        '--repo',
        'publication-owner/public-repo',
        '--verify-tag',
        '--title=-leading',
        '--notes-file',
        '-',
    ]
    assert kwargs['input'] == notes


@pytest.mark.parametrize(
    ('payload', 'expected'),
    [
        (
            {
                'total_count': 1,
                'check_runs': [
                    {
                        'name': 'CI',
                        'head_sha': 'a' * 40,
                        'status': 'completed',
                        'conclusion': 'success',
                    }
                ],
            },
            ('CI', 'success', 'success'),
        ),
        (
            {
                'total_count': 1,
                'check_runs': [
                    {
                        'name': 'CI',
                        'head_sha': 'a' * 40,
                        'status': 'in_progress',
                        'conclusion': None,
                    }
                ],
            },
            ('CI', 'pending', 'in_progress'),
        ),
        (
            {'total_count': 0, 'check_runs': []},
            ('CI', 'missing', ''),
        ),
        (
            {
                'total_count': 2,
                'check_runs': [
                    {
                        'name': 'CI',
                        'head_sha': 'a' * 40,
                        'status': 'completed',
                        'conclusion': 'success',
                    },
                    {
                        'name': 'CI',
                        'head_sha': 'a' * 40,
                        'status': 'completed',
                        'conclusion': 'failure',
                    },
                ],
            },
            ('CI', 'unacceptable', 'failure'),
        ),
    ],
)
def test_check_result_classifies_run_state(
    payload: dict[str, object],
    expected: tuple[str, str, str],
    fake_gh: Path,
) -> None:
    args = [
        'api',
        (
            'repos/publication-owner/public-repo/commits/'
            f'{"a" * 40}/check-runs?check_name=CI&filter=latest&per_page=100'
        ),
    ]
    _write_responses(fake_gh, [{'args': args, 'stdout': json.dumps(payload) + '\n'}])

    assert _host.check_result(('publication-owner', 'public-repo'), 'a' * 40, 'CI') == expected


@pytest.mark.parametrize(('total', 'returned'), [(2, 1), (101, 100)])
def test_check_result_never_accepts_an_incomplete_set(
    total: int,
    returned: int,
    fake_gh: Path,
) -> None:
    run = {'name': 'CI', 'head_sha': 'a' * 40, 'status': 'completed', 'conclusion': 'success'}
    args = [
        'api',
        'repos/publication-owner/public-repo/commits/'
        + 'a' * 40
        + '/check-runs?check_name=CI&filter=latest&per_page=100',
    ]
    _write_responses(
        fake_gh,
        [
            {
                'args': args,
                'stdout': json.dumps({'total_count': total, 'check_runs': [run] * returned}),
            }
        ],
    )
    if total == 2:
        with pytest.raises(PubanonError):
            _host.check_result(('publication-owner', 'public-repo'), 'a' * 40, 'CI')
    else:
        assert (
            _host.check_result(('publication-owner', 'public-repo'), 'a' * 40, 'CI').state
            == 'ambiguous'
        )


@pytest.mark.parametrize(
    'conclusion',
    [
        'failure',
        'neutral',
        'cancelled',
        'skipped',
        'timed_out',
        'action_required',
        'stale',
        None,
    ],
)
def test_check_result_refuses_every_non_success_conclusion(
    conclusion: str | None,
    fake_gh: Path,
) -> None:
    args = [
        'api',
        (
            'repos/publication-owner/public-repo/commits/'
            f'{"a" * 40}/check-runs?check_name=CI&filter=latest&per_page=100'
        ),
    ]
    payload = {
        'total_count': 1,
        'check_runs': [
            {
                'name': 'CI',
                'head_sha': 'a' * 40,
                'status': 'completed',
                'conclusion': conclusion,
            }
        ],
    }
    _write_responses(fake_gh, [{'args': args, 'stdout': json.dumps(payload) + '\n'}])

    verdict = _host.check_result(('publication-owner', 'public-repo'), 'a' * 40, 'CI')

    assert verdict == _host.CheckVerdict(
        'CI', 'unacceptable', conclusion if conclusion is not None else 'null'
    )


@pytest.mark.parametrize(
    'payload',
    [
        {'total_count': 1, 'check_runs': []},
        {
            'total_count': 0,
            'check_runs': [
                {
                    'name': 'CI',
                    'head_sha': 'a' * 40,
                    'status': 'completed',
                    'conclusion': 'success',
                }
            ],
        },
    ],
)
def test_check_result_refuses_inconsistent_cardinality(
    payload: dict[str, object],
    fake_gh: Path,
) -> None:
    args = [
        'api',
        (
            'repos/publication-owner/public-repo/commits/'
            f'{"a" * 40}/check-runs?check_name=CI&filter=latest&per_page=100'
        ),
    ]
    _write_responses(fake_gh, [{'args': args, 'stdout': json.dumps(payload) + '\n'}])

    with pytest.raises(PubanonError, match='GitHub returned check results Pubanon could not read'):
        _host.check_result(('publication-owner', 'public-repo'), 'a' * 40, 'CI')


@pytest.mark.parametrize(
    ('outcomes', 'expected'),
    [
        (['success', 'success'], 'success'),
        (['success', None], 'pending'),
        ([None, 'success'], 'pending'),
        (['success', 'failure'], 'unacceptable'),
        (['failure', 'success'], 'unacceptable'),
        ([None, 'failure'], 'unacceptable'),
        (['cancelled', 'success'], 'unacceptable'),
    ],
)
def test_check_result_requires_every_matching_run_to_succeed(
    outcomes: list[str | None],
    expected: str,
    fake_gh: Path,
) -> None:
    runs = [
        {
            'name': 'CI',
            'head_sha': 'a' * 40,
            'status': 'in_progress' if result is None else 'completed',
            'conclusion': result,
        }
        for result in outcomes
    ]
    args = [
        'api',
        'repos/publication-owner/public-repo/commits/'
        + 'a' * 40
        + '/check-runs?check_name=CI&filter=latest&per_page=100',
    ]
    _write_responses(
        fake_gh,
        [{'args': args, 'stdout': json.dumps({'total_count': len(runs), 'check_runs': runs})}],
    )
    assert (
        _host.check_result(('publication-owner', 'public-repo'), 'a' * 40, 'CI').state == expected
    )


@pytest.mark.parametrize('field', ['name', 'head_sha'])
def test_check_result_validates_every_matching_run(field: str, fake_gh: Path) -> None:
    valid = {'name': 'CI', 'head_sha': 'a' * 40, 'status': 'completed', 'conclusion': 'success'}
    runs = [valid, {**valid, field: 'wrong'}]
    args = [
        'api',
        'repos/publication-owner/public-repo/commits/'
        + 'a' * 40
        + '/check-runs?check_name=CI&filter=latest&per_page=100',
    ]
    _write_responses(
        fake_gh, [{'args': args, 'stdout': json.dumps({'total_count': 2, 'check_runs': runs})}]
    )
    with pytest.raises(PubanonError):
        _host.check_result(('publication-owner', 'public-repo'), 'a' * 40, 'CI')


@pytest.mark.parametrize('cancelled', [False, True])
def test_create_repository_failure_preserves_private_details_and_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    cancelled: bool,
) -> None:
    error_type = OperationCanceled if cancelled else PubanonError
    error = error_type('host operation failed', actual='private-repo')
    destination = _host.Destination(
        adapter='github',
        owner='public-owner',
        repo='public-repo',
        account='public-owner',
        visibility='private',
        url='https://github.com/public-owner/public-repo.git',
    )

    def _fail(*_args: object, **_kwargs: object) -> bytes:
        raise error

    monkeypatch.setattr(_host, '_gh', _fail)
    with pytest.raises(PubanonError) as raised:
        _host.create_repository(destination, None)
    assert raised.value.actual == error.actual
    assert 'private-repo' in raised.value.render()
    if cancelled:
        assert raised.value is error
    else:
        assert isinstance(raised.value, _host.RepositoryCreationUncertainError)


@pytest.mark.parametrize('resource', ['runs', 'artifacts'])
def test_disclosure_refuses_duplicate_id_on_later_page(
    resource: str,
    fake_gh: Path,
) -> None:
    entries = (*((index, f'entry-{index}') for index in range(1, 101)), (1, 'duplicate'))
    responses = (
        _disclosure_responses(first_runs=entries)
        if resource == 'runs'
        else _disclosure_responses(first_artifacts=entries)
    )
    _write_responses(fake_gh, responses)

    with pytest.raises(PubanonError, match='invalid disclosure response'):
        _host.disclosure('publication-owner', 'public-repo')


@pytest.mark.parametrize('resource', ['runs', 'artifacts'])
def test_disclosure_refuses_duplicate_ids(resource: str, fake_gh: Path) -> None:
    duplicate_entries = ((7, 'first'), (7, 'second'))
    responses = (
        _disclosure_responses(first_runs=duplicate_entries)
        if resource == 'runs'
        else _disclosure_responses(first_artifacts=duplicate_entries)
    )
    _write_responses(fake_gh, responses)

    with pytest.raises(PubanonError, match='invalid disclosure response'):
        _host.disclosure('publication-owner', 'public-repo')


@pytest.mark.parametrize(
    ('resource', 'first', 'second'),
    [
        ('runs', ((1, 'build'),), ((1, 'build'), (2, 'publish'))),
        ('runs', ((1, 'build'), (2, 'publish')), ((1, 'build'),)),
        ('runs', ((1, 'build'),), ((1, 'renamed'),)),
        ('artifacts', ((1, 'wheel'),), ((1, 'wheel'), (2, 'source'))),
        ('artifacts', ((1, 'wheel'), (2, 'source')), ((1, 'wheel'),)),
        ('artifacts', ((1, 'wheel'),), ((1, 'renamed'),)),
    ],
)
def test_disclosure_refuses_inventory_changes_between_passes(
    resource: str,
    first: tuple[tuple[int, str], ...],
    second: tuple[tuple[int, str], ...],
    fake_gh: Path,
) -> None:
    responses = (
        _disclosure_responses(first_runs=first, second_runs=second)
        if resource == 'runs'
        else _disclosure_responses(first_artifacts=first, second_artifacts=second)
    )
    _write_responses(fake_gh, responses)

    with pytest.raises(PubanonError, match='unstable disclosure response') as exc_info:
        _host.disclosure('publication-owner', 'public-repo')

    assert exc_info.value.recovery == 'stop workflow or artifact changes, then rerun'


@pytest.mark.parametrize('private_refs', [False, True])
def test_fetch_reads_main_and_tags_through_destination(
    private_refs: bool,
    make_repo: Callable[..., Path],
    tmp_path: Path,
) -> None:
    source = make_repo('source')
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
    subprocess.run(['git', 'clone', '--bare', '-q', source, remote], check=True, env=git_env())
    for ref in ('refs/heads/main', 'refs/tags/v1.0.0'):
        subprocess.run(['git', '-C', remote, 'update-ref', ref, commit], check=True, env=git_env())
    store = tmp_path / 'store.git'
    subprocess.run(['git', 'init', '--bare', '-q', store], check=True, env=git_env())
    subprocess.run(
        ['git', '-C', store, 'fetch', '-q', str(source), commit],
        check=True,
        env=git_env(),
    )
    subprocess.run(
        ['git', '-C', store, 'update-ref', 'refs/tags/local-only', commit],
        check=True,
        env=git_env(),
    )
    destination = _host.Destination(
        adapter='github',
        owner='publication-owner',
        repo='public-repo',
        url=remote.as_uri(),
        account='anonymous-account',
        visibility='private',
    )

    prefix = 'refs/pubanon/repair/' if private_refs else 'refs/'
    main_ref = prefix + ('main' if private_refs else 'heads/main')
    tag_ref = prefix + 'tags/v1.0.0'
    _host.fetch(
        destination,
        store,
        [f'+refs/heads/main:{main_ref}', f'+refs/tags/*:{prefix}tags/*'],
    )

    assert (
        subprocess.run(
            ['git', '-C', store, 'rev-parse', tag_ref],
            check=True,
            capture_output=True,
            env=git_env(),
        )
        .stdout.decode()
        .strip()
        == commit
    )
    assert (
        subprocess.run(
            ['git', '-C', store, 'rev-parse', main_ref],
            check=True,
            capture_output=True,
            env=git_env(),
        )
        .stdout.decode()
        .strip()
        == commit
    )
    assert subprocess.run(
        [
            'git',
            '-C',
            store,
            'show-ref',
            '--verify',
            '--quiet',
            'refs/tags/local-only',
        ],
        check=False,
        env=git_env(),
    ).returncode == (0 if private_refs else 1)

    if private_refs:
        assert (
            subprocess.run(
                ['git', '-C', store, 'show-ref', '--verify', '--quiet', 'refs/tags/v1.0.0'],
                check=False,
                env=git_env(),
            ).returncode
            == 1
        )


@pytest.mark.parametrize(
    ('heads', 'tags'),
    [
        ((), ()),
        (('main',), ()),
        (('stage',), ()),
        (('main', 'stage'), ()),
        (('main', 'stage', 'backup'), ()),
        (('main', 'stage', 'backup'), ('v1.0.0',)),
    ],
)
def test_read_remote_refs_preserves_branches_and_tags_without_local_store(
    heads: tuple[str, ...],
    tags: tuple[str, ...],
    make_repo: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo('source')
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
    real = tmp_path / 'real.git'
    subprocess.run(['git', 'init', '--bare', '-q', real], check=True, env=git_env())
    for head in heads:
        subprocess.run(
            ['git', '-C', real, 'fetch', '-q', str(source), f'{commit}:refs/heads/{head}'],
            check=True,
            env=git_env(),
        )
    for tag in tags:
        subprocess.run(
            ['git', '-C', real, 'update-ref', f'refs/tags/{tag}', commit],
            check=True,
            env=git_env(),
        )
    before = subprocess.run(
        ['git', '-C', source, 'status', '--porcelain'],
        check=True,
        capture_output=True,
        env=git_env(),
    ).stdout
    monkeypatch.chdir(source)
    destination = replace(_destination(), url=str(real))

    refs = _host.read_remote_refs(destination)

    assert refs == _host.RemoteRefs(
        commit if 'main' in heads else None,
        dict.fromkeys(tags, commit),
        tuple((head, commit) for head in sorted(head for head in heads if head != 'main')),
    )
    assert not (tmp_path / 'data').exists()
    assert (
        subprocess.run(
            ['git', '-C', source, 'status', '--porcelain'],
            check=True,
            capture_output=True,
            env=git_env(),
        ).stdout
        == before
    )


@pytest.mark.parametrize(
    'output',
    [
        b'not-a-reference\n',
        b'a' * 40 + b'\trefs/heads/main',
        b'a' * 40 + b'\trefs/heads/\n',
        b'a' * 40 + b'\trefs/tags/\n',
        (b'a' * 40 + b'\trefs/heads/stage\n') * 2,
        (b'a' * 40 + b'\trefs/tags/v1\n') * 2,
    ],
)
def test_read_remote_refs_refuses_malformed_output(
    output: bytes,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_credential_child(*_args: object, **_kwargs: object) -> GitResult:
        return GitResult(0, output, b'')

    monkeypatch.setattr(_host, 'run_credential_child', _fake_credential_child)

    with pytest.raises(PubanonError, match='invalid remote reference response'):
        _host.read_remote_refs(_destination())


@pytest.mark.parametrize(
    'output',
    [
        b'not-a-reference\n',
        (
            b'0000000000000000000000000000000000000000\trefs/heads/main\n'
            b'1111111111111111111111111111111111111111\trefs/heads/main\n'
        ),
        (
            b'0000000000000000000000000000000000000000\trefs/tags/v1.0.0\n'
            b'1111111111111111111111111111111111111111\trefs/tags/v1.0.0\n'
        ),
    ],
)
def test_ls_remote_refuses_malformed_or_duplicate_direct_output(
    output: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = _host.Destination(
        adapter='github',
        owner='publication-owner',
        repo='public-repo',
        url='file:///host.git',
        account='anonymous-account',
        visibility='private',
    )

    def _fake_credential_child(*_args: object, **_kwargs: object) -> GitResult:
        return GitResult(0, output, b'')

    monkeypatch.setattr(_host, 'run_credential_child', _fake_credential_child)

    with pytest.raises(PubanonError, match='invalid remote reference response'):
        _host.ls_remote(destination, _paths(tmp_path), 'demo')


def test_remote_refs_accepts_peeled_annotated_tag() -> None:
    oid_a, oid_b = 'a' * 40, 'b' * 40
    output = (
        f'{oid_a}\trefs/heads/main\n{oid_b}\trefs/tags/v1.0.0\n{oid_a}\trefs/tags/v1.0.0^{{}}\n'
    ).encode()

    refs = _remote_refs(output)

    assert refs == _host.RemoteRefs(oid_a, {'v1.0.0': oid_b})


def test_remote_refs_records_unexpected_branch_as_fact() -> None:
    oid_a, oid_b = 'a' * 40, 'b' * 40
    output = (f'{oid_a}\trefs/heads/main\n{oid_b}\trefs/heads/feature\n').encode()

    refs = _remote_refs(output)

    assert refs.branches == (('feature', oid_b),)


def test_remote_refs_still_refuses_malformed_and_duplicates() -> None:
    oid_a = 'a' * 40
    outputs = (
        b'garbage\n',
        f'{oid_a}\trefs/heads/main\n{oid_a}\trefs/heads/main\n'.encode(),
    )
    for output in outputs:
        with pytest.raises(PubanonError):
            _remote_refs(output)


@pytest.mark.parametrize(
    ('refs', 'expected'),
    [
        (_host.RemoteRefs(None, {}), 'main=absent; tags=none; branches=none'),
        (
            _host.RemoteRefs('a' * 40, {'v2.0.0': 'c' * 40, 'v1.0.0': 'b' * 40}),
            f'main={"a" * 40}; tags=v1.0.0={"b" * 40}, v2.0.0={"c" * 40}; branches=none',
        ),
        (
            _host.RemoteRefs('a' * 40, {}, (('backup', 'b' * 40),)),
            f'main={"a" * 40}; tags=none; branches=backup={"b" * 40}',
        ),
    ],
)
def test_remote_refs_text_renders_complete_deterministic_projection(
    refs: _host.RemoteRefs,
    expected: str,
) -> None:
    assert _host.remote_refs_text(refs) == expected


def test_repository_settings_forwards_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def _timed_gh(args: list[str], *, timeout: float | None = None) -> bytes:
        assert args == _SETTINGS_ARGS
        assert timeout == 2.5
        raise ObservationTimeoutError

    monkeypatch.setattr(_host, '_gh', _timed_gh)

    with pytest.raises(ObservationTimeoutError):
        _host.repository_settings('publication-owner', 'public-repo', timeout=2.5)


@pytest.mark.parametrize(('description', 'expected'), [(None, ''), ('Public repo', 'Public repo')])
def test_repository_settings_reads_strict_facts(
    description: str | None,
    expected: str,
    fake_gh: Path,
) -> None:
    _write_responses(
        fake_gh,
        [
            {
                'args': _SETTINGS_ARGS,
                'stdout': json.dumps(
                    {
                        'description': description,
                        'default_branch': 'main',
                        'issues': True,
                        'wiki': False,
                        'projects': True,
                        'pull_requests': False,
                        'discussions': True,
                    }
                )
                + '\n',
            }
        ],
    )

    facts = _host.repository_settings('publication-owner', 'public-repo')

    assert facts == (expected, 'main', True, False, True, False, True)


@pytest.mark.parametrize(
    'payload',
    [
        {'default_branch': 'main', 'issues': True, 'wiki': False, 'projects': True},
        {
            'description': '',
            'default_branch': 'main',
            'issues': 1,
            'wiki': False,
            'projects': True,
            'pull_requests': False,
            'discussions': False,
        },
        {
            'description': '',
            'default_branch': None,
            'issues': True,
            'wiki': False,
            'projects': True,
            'pull_requests': False,
            'discussions': False,
        },
        {
            'description': '',
            'default_branch': 'main',
            'issues': True,
            'wiki': False,
            'projects': True,
            'pull_requests': 1,
            'discussions': False,
        },
        {
            'description': '',
            'default_branch': 'main',
            'issues': True,
            'wiki': False,
            'projects': True,
            'pull_requests': False,
            'discussions': 'yes',
        },
    ],
)
def test_repository_settings_refuses_missing_or_wrongly_typed_facts(
    payload: dict[str, object],
    fake_gh: Path,
) -> None:
    _write_responses(
        fake_gh,
        [{'args': _SETTINGS_ARGS, 'stdout': json.dumps(payload) + '\n'}],
    )

    with pytest.raises(PubanonError, match='invalid repository settings response'):
        _host.repository_settings('publication-owner', 'public-repo')


@pytest.mark.parametrize(
    'stdout',
    [
        'not-json\n',
        '{}\n',
        '{"owner":"publication-owner","repo":"public-repo"}\n',
        (
            '{"owner":"publication-owner","repo":"public-repo",'
            '"visibility":"private","extra":true}\n'
        ),
        '{"owner":"","repo":"public-repo","visibility":"private"}\n',
    ],
)
def test_repository_visible_refuses_invalid_success_response(
    stdout: str,
    fake_gh: Path,
) -> None:
    _write_responses(
        fake_gh,
        [{'args': _REPOSITORY_ARGS, 'stdout': stdout}],
    )

    with pytest.raises(PubanonError, match='invalid repository response'):
        _host.repository_visible('publication-owner', 'public-repo')


@pytest.mark.parametrize(
    ('stdout', 'stderr'),
    [
        ('{"message":"private-canary","status":"401"}\n', ''),
        ('malformed-private-canary\n', ''),
        ('', 'network-private-canary\n'),
        ('{"message":"private-canary","status":"500"}\n', ''),
    ],
)
def test_repository_visible_reports_and_raises_non_404_failure(
    stdout: str,
    stderr: str,
    fake_gh: Path,
) -> None:
    _write_responses(
        fake_gh,
        [
            {
                'args': _REPOSITORY_ARGS,
                'returncode': 1,
                'stdout': stdout,
                'stderr': stderr,
            }
        ],
    )

    with pytest.raises(PubanonError) as exc_info:
        _host.repository_visible('publication-owner', 'public-repo')

    assert 'private-canary' in exc_info.value.render()


def test_repository_visible_returns_false_for_http_404_fallback(fake_gh: Path) -> None:
    _write_responses(
        fake_gh,
        [
            {
                'args': _REPOSITORY_ARGS,
                'returncode': 1,
                'stderr': 'gh: Not Found (HTTP 404)\n',
            }
        ],
    )

    assert not _host.repository_visible('publication-owner', 'public-repo')


def test_repository_visible_returns_false_for_json_404(fake_gh: Path) -> None:
    _write_responses(
        fake_gh,
        [
            {
                'args': _REPOSITORY_ARGS,
                'returncode': 1,
                'stdout': '{"message":"Not Found","status":"404"}\n',
            }
        ],
    )

    assert not _host.repository_visible('publication-owner', 'public-repo')


def test_repository_visible_returns_true_on_success(fake_gh: Path) -> None:
    _write_responses(
        fake_gh,
        [
            {
                'args': _REPOSITORY_ARGS,
                'stdout': (
                    '{"owner":"publication-owner","repo":"public-repo","visibility":"private"}\n'
                ),
            }
        ],
    )

    assert _host.repository_visible('publication-owner', 'public-repo')


def test_require_authenticated_account_reports_mismatch_and_names_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = 'configured-account-canary'
    actual = 'observed-account-canary'

    def _account(*, timeout: float | None = None) -> str:
        assert timeout is None
        return actual

    monkeypatch.setattr(_host, '_authenticated_account', _account)

    with pytest.raises(PubanonError) as exc_info:
        _host.require_authenticated_account(expected, authority='snapshot record')

    error = exc_info.value
    assert str(error) == 'authenticated account does not match snapshot record'
    assert error.fact == 'authenticated account'
    assert error.expected == expected
    assert error.actual == actual
    assert expected in error.render()
    assert actual in error.render()


def test_require_authenticated_account_returns_exact_host_account(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = 'matched-account'
    actual = b'matched-account'.decode()
    assert actual is not expected

    def _account(*, timeout: float | None = None) -> str:
        assert timeout is None
        return actual

    monkeypatch.setattr(_host, '_authenticated_account', _account)

    returned = _host.require_authenticated_account(expected, authority='configuration')

    assert returned == actual


def test_set_default_branch_forwards_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def _timed_gh(args: list[str], *, timeout: float | None = None) -> bytes:
        assert args[0] == 'api'
        assert timeout == 2.5
        raise ObservationTimeoutError

    monkeypatch.setattr(_host, '_gh', _timed_gh)

    with pytest.raises(ObservationTimeoutError):
        _host.set_default_branch('publication-owner', 'public-repo', 'main', timeout=2.5)


@pytest.mark.parametrize(
    'stderr', ['gh: Resource not accessible by integration (HTTP 403)', 'gh: Not Found (HTTP 404)']
)
def test_set_default_branch_propagates_host_failure(
    stderr: str,
    fake_gh: Path,
    tmp_path: Path,
) -> None:
    args = [
        'api',
        '-X',
        'PATCH',
        'repos/publication-owner/public-repo',
        '-f',
        'default_branch=main',
    ]
    _write_responses(fake_gh, [{'args': args, 'returncode': 1, 'stderr': stderr}])

    with pytest.raises(PubanonError) as exc_info:
        _host.set_default_branch('publication-owner', 'public-repo', 'main')

    assert stderr in str(exc_info.value)
    assert exc_info.value.fact == 'exit status 1'
    assert _gh_calls(tmp_path) == [{'args': args, 'stdin': ''}]


def test_set_default_branch_sends_single_request(fake_gh: Path, tmp_path: Path) -> None:
    args = [
        'api',
        '-X',
        'PATCH',
        'repos/publication-owner/public-repo',
        '-f',
        'default_branch=main',
    ]
    _write_responses(fake_gh, [{'args': args}])

    _host.set_default_branch('publication-owner', 'public-repo', 'main')

    assert _gh_calls(tmp_path) == [{'args': args, 'stdin': ''}]


def test_set_visibility_patches_public(fake_gh: Path, tmp_path: Path) -> None:
    args = [
        'api',
        '-X',
        'PATCH',
        'repos/publication-owner/public-repo',
        '-f',
        'visibility=public',
    ]
    _write_responses(fake_gh, [{'args': args}])

    _host.set_visibility('publication-owner', 'public-repo', 'public')

    assert _gh_calls(tmp_path) == [{'args': args, 'stdin': ''}]


@pytest.mark.parametrize('operation', ['fetch', 'push', 'ls-remote'])
def test_transport_selects_absolute_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    destination = _host.Destination(
        'github',
        'public-owner',
        'public-repo',
        'https://example.invalid/repo',
        'account',
        'private',
    )
    paths = _paths(tmp_path)
    store = paths.data_dir / 'projects' / 'demo' / 'store.git'
    calls: list[list[str]] = []

    def _capture(args: Iterable[str], **_kwargs: object) -> GitResult:
        calls.append(list(args))
        return GitResult(0, b'', b'')

    monkeypatch.setattr(_host, 'run_credential_child', _capture)
    monkeypatch.chdir(tmp_path)
    relative = store.relative_to(tmp_path)
    if operation == 'fetch':
        _host.fetch(destination, relative, ['refs/heads/main:refs/heads/main'])
    elif operation == 'push':
        _host.push(destination, relative, ['HEAD:refs/heads/main'])
    else:
        _host.ls_remote(destination, paths, 'demo')

    assert len(calls) == 1
    assert calls[0][:2] == ['--git-dir', str(store)]


def test_verify_destination_delegates_configuration_account_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    global_config, project_config = _configs(tmp_path)
    requirements: list[tuple[str, str]] = []

    def _require_account(expected: str, *, authority: str) -> str:
        requirements.append((expected, authority))
        raise PubanonError('account authority observed')

    monkeypatch.setattr(_host, 'require_authenticated_account', _require_account)

    with pytest.raises(PubanonError, match='account authority observed'):
        _host.verify_destination(_paths(tmp_path), global_config, project_config)

    assert requirements == [('anonymous-account', 'configuration')]


def test_verify_destination_derives_url_from_the_template(
    fake_gh: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    global_config, project_config = _configs(tmp_path)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', 'file:///host/{owner}/{repo}.git')
    _write_responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            {
                'args': _REPOSITORY_ARGS,
                'stdout': json.dumps(
                    {
                        'owner': 'publication-owner',
                        'repo': 'public-repo',
                        'visibility': 'private',
                    }
                )
                + '\n',
            },
        ],
    )

    destination = _host.verify_destination(_paths(tmp_path), global_config, project_config)

    assert destination.url == 'file:///host/publication-owner/public-repo.git'
    assert destination.owner == 'publication-owner'
    assert destination.account == 'anonymous-account'


@pytest.mark.parametrize(
    ('stdout', 'stderr'),
    [
        ('{"message":"Not Found","status":"404"}', 'gh: Not Found (HTTP 404)'),
        ('', 'gh: Not Found (HTTP 404)'),
    ],
)
def test_verify_destination_explains_missing_or_inaccessible_repository(
    fake_gh: Path,
    tmp_path: Path,
    stdout: str,
    stderr: str,
) -> None:
    global_config, project_config = _configs(tmp_path)
    _write_responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            {'args': _REPOSITORY_ARGS, 'returncode': 1, 'stdout': stdout, 'stderr': stderr},
        ],
    )

    with pytest.raises(PubanonError) as exc_info:
        _host.verify_destination(_paths(tmp_path), global_config, project_config)

    error = exc_info.value
    assert str(error) == 'Cannot access GitHub repository publication-owner/public-repo'
    assert error.fact == (
        'GitHub returned 404: the repository does not exist or this account lacks access.'
    )
    assert error.recovery == (
        'Check the repository name and GitHub permissions. '
        'If it has not been created, run `pubanon repo create`, then retry.'
    )


@pytest.mark.parametrize('field', ['owner', 'repo'])
def test_verify_destination_gates_current_coordinates_before_host_access(
    field: str,
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    global_config, project = _configs(tmp_path)
    term = f'private-{field}'
    global_config = replace(global_config, sensitive_terms=(term,))
    project = replace(project, **{field: f'public-{term}'})

    label = 'name' if field == 'repo' else field
    with pytest.raises(
        PubanonError, match=f'repository {label} did not pass content checks'
    ) as exc_info:
        _host.verify_destination(paths, global_config, project)

    assert term in exc_info.value.render()
    assert not (tmp_path / 'fake-gh-calls.jsonl').exists()


def test_verify_destination_hides_subprocess_launch_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    global_config, project_config = _configs(tmp_path)
    private_executable = str(tmp_path / 'private-gh')

    def _fail_spawn(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise FileNotFoundError(private_executable)

    monkeypatch.setattr(_host.subprocess, 'run', _fail_spawn)

    with pytest.raises(PubanonError) as exc_info:
        _host.verify_destination(_paths(tmp_path), global_config, project_config)

    assert str(exc_info.value) == 'cannot run GitHub CLI'
    assert private_executable not in exc_info.value.render()


def test_verify_destination_propagates_timeout_to_each_host_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    global_config, project_config = _configs(tmp_path)
    calls: list[tuple[str, float | None]] = []

    def _require_account(
        expected: str,
        *,
        authority: str,
        timeout: float | None = None,
    ) -> str:
        assert (expected, authority) == ('anonymous-account', 'configuration')
        calls.append(('account', timeout))
        return expected

    def _facts(
        owner: str,
        repo: str,
        *,
        timeout: float | None = None,
    ) -> tuple[str, str, str]:
        calls.append(('repository', timeout))
        return owner, repo, 'private'

    monkeypatch.setattr(_host, 'require_authenticated_account', _require_account)
    monkeypatch.setattr(_host, 'repository_facts', _facts)
    times = iter([0.0, 0.0, 0.0, 0.0, 1.0])
    monkeypatch.setattr(_host, '_monotonic', lambda: next(times))

    _host.verify_destination(_paths(tmp_path), global_config, project_config, timeout=9.0)

    assert calls == [('account', 9.0), ('repository', 8.0)]


def test_verify_destination_refuses_duplicate_account_output(
    fake_gh: Path,
    tmp_path: Path,
) -> None:
    global_config, project_config = _configs(tmp_path)
    _write_responses(
        fake_gh,
        [{'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\nanonymous-account\n'}],
    )

    with pytest.raises(PubanonError, match='invalid authenticated account response'):
        _host.verify_destination(_paths(tmp_path), global_config, project_config)


def test_verify_destination_refuses_duplicate_repository_facts(
    fake_gh: Path,
    tmp_path: Path,
) -> None:
    global_config, project_config = _configs(tmp_path)
    _write_responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            {
                'args': _REPOSITORY_ARGS,
                'stdout': (
                    '{"owner":"publication-owner","repo":"public-repo",'
                    '"visibility":"public","visibility":"private"}\n'
                ),
            },
        ],
    )

    with pytest.raises(PubanonError, match='invalid repository response'):
        _host.verify_destination(_paths(tmp_path), global_config, project_config)


def test_verify_destination_refuses_malformed_repository_facts(
    fake_gh: Path,
    tmp_path: Path,
) -> None:
    global_config, project_config = _configs(tmp_path)
    _write_responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            {'args': _REPOSITORY_ARGS, 'stdout': '{"visibility":"private"}\n'},
        ],
    )

    with pytest.raises(PubanonError, match='invalid repository response'):
        _host.verify_destination(_paths(tmp_path), global_config, project_config)


@pytest.mark.parametrize(
    ('field', 'actual', 'fact'),
    [
        ('owner', 'other-owner', 'repository owner'),
        ('repo', 'other-repo', 'repository name'),
    ],
)
def test_verify_destination_refuses_repository_coordinate_mismatch(
    field: str,
    actual: str,
    fact: str,
    fake_gh: Path,
    tmp_path: Path,
) -> None:
    global_config, project_config = _configs(tmp_path)
    repository = {
        'owner': 'publication-owner',
        'repo': 'public-repo',
        'visibility': 'private',
    }
    repository[field] = actual
    _write_responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            {'args': _REPOSITORY_ARGS, 'stdout': json.dumps(repository) + '\n'},
        ],
    )

    with pytest.raises(PubanonError) as exc_info:
        _host.verify_destination(_paths(tmp_path), global_config, project_config)

    assert exc_info.value.fact == fact
    assert exc_info.value.expected == getattr(project_config, field)
    assert exc_info.value.actual == actual
    assert actual in exc_info.value.render()


def test_verify_destination_refuses_visibility_mismatch(
    fake_gh: Path,
    tmp_path: Path,
) -> None:
    global_config, project_config = _configs(tmp_path)
    _write_responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            {
                'args': _REPOSITORY_ARGS,
                'stdout': json.dumps(
                    {
                        'owner': 'publication-owner',
                        'repo': 'public-repo',
                        'visibility': 'public',
                    }
                )
                + '\n',
            },
        ],
    )

    with pytest.raises(PubanonError) as exc_info:
        _host.verify_destination(_paths(tmp_path), global_config, project_config)

    error = exc_info.value
    assert error.fact == 'repository visibility'
    assert error.expected == 'private'
    assert error.actual == 'public'


def test_verify_destination_reports_account_mismatch(
    fake_gh: Path,
    tmp_path: Path,
) -> None:
    global_config, project_config = _configs(tmp_path)
    global_config = replace(global_config, sensitive_terms=('private-canary',))
    _write_responses(
        fake_gh,
        [{'args': _ACCOUNT_ARGS, 'stdout': 'private-canary\n'}],
    )

    with pytest.raises(PubanonError) as exc_info:
        _host.verify_destination(_paths(tmp_path), global_config, project_config)

    error = exc_info.value
    assert error.fact == 'authenticated account'
    assert error.expected == 'anonymous-account'
    assert error.actual == 'private-canary'
    assert 'anonymous-account' in error.render()
    assert 'private-canary' in error.render()


def test_verify_destination_reports_repository_lookup_failure(
    fake_gh: Path,
    tmp_path: Path,
) -> None:
    global_config, project_config = _configs(tmp_path)
    _write_responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            {
                'args': _REPOSITORY_ARGS,
                'returncode': 1,
                'stderr': 'repository private-canary was not found\n',
            },
        ],
    )

    with pytest.raises(PubanonError) as exc_info:
        _host.verify_destination(_paths(tmp_path), global_config, project_config)

    assert str(exc_info.value) == 'Could not check GitHub repository publication-owner/public-repo'
    assert exc_info.value.recovery == 'Check GitHub access and connectivity, then retry.'
    assert 'private-canary' in exc_info.value.render()


def test_verify_recorded_destination_delegates_candidate_record_account_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    destination = _destination()
    requirements: list[tuple[str, str]] = []

    def _require_account(expected: str, *, authority: str) -> str:
        requirements.append((expected, authority))
        raise PubanonError('account authority observed')

    monkeypatch.setattr(_host, 'require_authenticated_account', _require_account)

    with pytest.raises(PubanonError, match='account authority observed'):
        _host.verify_recorded_destination(destination)

    assert requirements == [('anonymous-account', 'snapshot record')]


def test_verify_recorded_destination_refuses_account_mismatch(
    fake_gh: Path,
) -> None:
    destination = _host.Destination(
        adapter='github',
        owner='publication-owner',
        repo='public-repo',
        url='file:///recorded/publication-owner/public-repo.git',
        account='anonymous-account',
        visibility='private',
    )
    _write_responses(fake_gh, [{'args': _ACCOUNT_ARGS, 'stdout': 'real-login-canary\n'}])

    with pytest.raises(PubanonError) as exc_info:
        _host.verify_recorded_destination(destination)

    error = exc_info.value
    assert error.fact == 'authenticated account'
    assert error.expected == 'anonymous-account'
    assert error.actual == 'real-login-canary'
    assert 'anonymous-account' in error.render()
    assert 'real-login-canary' in error.render()


@pytest.mark.parametrize('field', ['owner', 'repo'])
@pytest.mark.parametrize('value', ['two/segments', 'repo?query', 'repo#fragment', ' padded '])
def test_verify_recorded_destination_refuses_unsafe_segments_before_host_access(
    field: str,
    value: str,
    tmp_path: Path,
) -> None:
    destination = replace(_destination(), **{field: value})

    label = 'GitHub owner' if field == 'owner' else 'repository name'
    with pytest.raises(PubanonError, match=f'invalid {label}'):
        _host.verify_recorded_destination(destination)

    assert not (tmp_path / 'fake-gh-calls.jsonl').exists()


@pytest.mark.parametrize(
    ('field', 'actual', 'fact'),
    [
        ('owner', 'unexpected-owner-canary', 'repository owner'),
        ('repo', 'unexpected-repo-canary', 'repository name'),
    ],
)
def test_verify_recorded_destination_reports_repository_coordinate_mismatch(
    field: str,
    actual: str,
    fact: str,
    fake_gh: Path,
) -> None:
    destination = _host.Destination(
        adapter='github',
        owner='recorded-owner-canary',
        repo='recorded-repo-canary',
        url='file:///recorded/recorded-owner-canary/recorded-repo-canary.git',
        account='anonymous-account',
        visibility='private',
    )
    repository_args = [
        'api',
        'repos/recorded-owner-canary/recorded-repo-canary',
        '--jq',
        _host._REPOSITORY_QUERY,
    ]
    repository = {
        'owner': 'recorded-owner-canary',
        'repo': 'recorded-repo-canary',
        'visibility': 'private',
    }
    repository[field] = actual
    _write_responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            {
                'args': repository_args,
                'stdout': json.dumps(repository) + '\n',
            },
        ],
    )

    with pytest.raises(PubanonError) as exc_info:
        _host.verify_recorded_destination(destination)

    error = exc_info.value
    assert error.fact == fact
    assert error.expected == getattr(destination, field)
    assert error.actual == actual
    rendered = error.render()
    assert actual in rendered


def test_verify_recorded_destination_reports_repository_lookup_failure(
    fake_gh: Path,
) -> None:
    destination = _host.Destination(
        adapter='github',
        owner='recorded-owner-canary',
        repo='recorded-repo-canary',
        url='file:///recorded/recorded-owner-canary/recorded-repo-canary.git',
        account='anonymous-account',
        visibility='private',
    )
    repository_args = [
        'api',
        'repos/recorded-owner-canary/recorded-repo-canary',
        '--jq',
        _host._REPOSITORY_QUERY,
    ]
    _write_responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            {
                'args': repository_args,
                'returncode': 1,
                'stderr': (
                    'lookup failed for recorded-owner-canary/recorded-repo-canary '
                    'and existing-private-canary\n'
                ),
            },
        ],
    )

    with pytest.raises(PubanonError) as exc_info:
        _host.verify_recorded_destination(destination)

    rendered = exc_info.value.render()
    assert 'private-canary' in rendered
    assert 'recorded-owner-canary' in rendered
    assert 'recorded-repo-canary' in rendered
    assert 'existing-private-canary' in rendered


def test_verify_recorded_destination_uses_recorded_facts(
    fake_gh: Path,
) -> None:
    destination = _host.Destination(
        adapter='github',
        owner='publication-owner',
        repo='public-repo',
        url='file:///recorded/publication-owner/public-repo.git',
        account='anonymous-account',
        visibility='private',
    )
    _write_responses(
        fake_gh,
        [
            {'args': _ACCOUNT_ARGS, 'stdout': 'anonymous-account\n'},
            {
                'args': _REPOSITORY_ARGS,
                'stdout': json.dumps(
                    {
                        'owner': 'publication-owner',
                        'repo': 'public-repo',
                        'visibility': 'private',
                    }
                )
                + '\n',
            },
        ],
    )

    verified = _host.verify_recorded_destination(destination)

    assert verified == destination
