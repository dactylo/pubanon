import argparse
import json
import os
import re
import shlex
import subprocess
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import cast

import pytest

from pubanon import (
    _cli,
    _config,
    _filesystem,
)
from pubanon._cli import _prepare, _workflow
from pubanon._config import PackageObservation, Settings
from pubanon._remote import _host, _package, _publish, _stage, _watch
from pubanon._run import GitResult
from pubanon._snapshots import _candidate, _records, _source
from pubanon._terminal import display_path
from tests._git import git_env

_ACCOUNT_CALL: dict[str, object] = {
    'args': ['api', 'user', '--jq', '.login'],
    'stdin': '',
}
_REPOSITORY_CALL: dict[str, object] = {
    'args': [
        'api',
        'repos/release-owner/public-repo',
        '--jq',
        '{owner: .owner.login, repo: .name, visibility: .visibility}',
    ],
    'stdin': '',
}
_REAL_ACCOUNT = 'pat-example'
_REAL_EMAIL = 'pat@example.com'
_REAL_NAME = 'Pat Example'
_REAL_SETTINGS = Settings(
    description='Example project',
    default_branch='main',
    issues=True,
    wiki=False,
    projects=False,
    pull_requests=False,
    discussions=False,
)
_REAL_TERM = 'internal-host.example'


@dataclass(frozen=True)
class _LifecycleFixture:
    _account: str
    _check: str
    _fake_gh: Path
    _owner: str
    _paths: _config.Paths
    _publication_host: Path
    _publication_repo: str
    _settings: Settings
    _source: Path
    _staging_host: Path
    _staging_repo: str
    _tmp_path: Path


@dataclass
class _ReleaseWalk:
    _fixture: _LifecycleFixture
    _publication_pushes: list[tuple[Path, tuple[str, ...]]]
    _stage_pushes: list[tuple[Path, tuple[str, ...]]]
    _events: list[tuple[str, str]]
    _executed_stages: list[str]
    _candidate_builds: int
    _host_call_cursor: int
    _first_oid: str = ''
    _second_oid: str = ''
    _third_oid: str = ''


def _account_response(account: str) -> dict[str, object]:
    return {
        'args': ['api', 'user', '--jq', '.login'],
        'stdout': f'{account}\n',
    }


def _append_response(path: Path, response: dict[str, object]) -> None:
    parsed: object = json.loads(path.read_text())
    assert isinstance(parsed, list)
    responses = cast('list[object]', parsed)
    responses.append(response)
    path.write_text(json.dumps(responses) + '\n')


def _assert_release_package_events(
    events: list[tuple[str, str]],
    release_path: str,
) -> None:
    labels = [
        'release-check'
        if kind == 'host' and release_path in detail
        else f'{kind}-{detail.partition(" ")[0]}'
        for kind, detail in events
        if kind != 'host' or release_path in detail
    ]
    assert labels == ['release-check', 'fetch-404', 'sleep-15', 'fetch-200']


def _assert_release_published_snapshot(
    walk: _ReleaseWalk,
    snapshot: tuple[dict[str, str], tuple[str | None, dict[str, str]]],
) -> None:
    remote, local = snapshot
    assert remote == {
        'refs/heads/main': walk._second_oid,
        'refs/tags/v0.1.0': walk._first_oid,
        'refs/tags/v0.2.0': walk._second_oid,
    }
    assert local == (
        walk._second_oid,
        {'v0.1.0': walk._first_oid, 'v0.2.0': walk._second_oid},
    )


def _assert_release_reveal_events(
    events: list[tuple[str, str]],
    acceptance_path: str,
    release_path: str,
) -> None:
    labels: list[str] = []
    for kind, detail in events:
        if kind == 'host' and acceptance_path in detail:
            labels.append('required-check')
        elif kind == 'host' and release_path in detail:
            labels.append('release-check')
        elif kind == 'fetch':
            labels.append(f'fetch-{detail.partition(" ")[0]}')
        elif kind == 'host' and '"PATCH"' in detail and 'visibility=public' in detail:
            labels.append('visibility-patch')
    assert labels == ['required-check', 'release-check', 'fetch-200', 'visibility-patch']


def _check_response(
    owner: str,
    repo: str,
    commit: str,
    check: str,
    *,
    conclusion: str = 'success',
) -> dict[str, object]:
    query = check.replace(' ', '+')
    return {
        'args': [
            'api',
            (
                f'repos/{owner}/{repo}/commits/{commit}/check-runs?'
                f'check_name={query}&filter=latest&per_page=100'
            ),
        ],
        'stdout': json.dumps(
            {
                'total_count': 1,
                'check_runs': [
                    {
                        'name': check,
                        'head_sha': commit,
                        'status': 'completed',
                        'conclusion': conclusion,
                    }
                ],
            }
        ),
    }


def _commit(source: Path, content: str, *, add_env: bool = False) -> None:
    (source / 'README.md').write_text(content, encoding='utf-8', newline='\n')
    if add_env:
        (source / '.env').write_bytes(b'PUBLIC_SETTING=enabled\n')
    subprocess.run(['git', '-C', source, 'add', '-A'], check=True, env=git_env())
    subprocess.run(
        [
            'git',
            '-C',
            source,
            '-c',
            'user.name=Source Author',
            '-c',
            'user.email=source@example.invalid',
            'commit',
            '-qm',
            'source update',
        ],
        check=True,
        env=git_env(),
    )


def _commit_headers(repo: Path, oid: str) -> tuple[str, str, list[str], bool]:
    raw = subprocess.run(
        ['git', '-C', repo, 'cat-file', 'commit', oid],
        check=True,
        capture_output=True,
        text=True,
        env=git_env(),
    ).stdout
    headers = raw.split('\n\n', maxsplit=1)[0].splitlines()
    author = next(line for line in headers if line.startswith('author '))
    committer = next(line for line in headers if line.startswith('committer '))
    parents = [line.split(' ', maxsplit=1)[1] for line in headers if line.startswith('parent ')]
    signed = any(line.startswith(('gpgsig ', 'gpgsig-sha256 ')) for line in headers)
    return author, committer, parents, signed


def _disclosure_responses(
    owner: str,
    repo: str,
    *,
    artifacts: tuple[tuple[int, str], ...] = (),
) -> list[dict[str, object]]:
    return [
        {
            'args': [
                'api',
                f'repos/{owner}/{repo}/actions/runs?per_page=100&page=1',
            ],
            'stdout': '{"total_count": 0, "workflow_runs": []}',
        },
        {
            'args': [
                'api',
                f'repos/{owner}/{repo}/actions/artifacts?per_page=100&page=1',
            ],
            'stdout': json.dumps(
                {
                    'total_count': len(artifacts),
                    'artifacts': [
                        {'id': identifier, 'name': name} for identifier, name in artifacts
                    ],
                }
            ),
        },
    ]


def _git_refs(repo: Path) -> dict[str, str]:
    output = subprocess.run(
        [
            'git',
            '-C',
            repo,
            'for-each-ref',
            '--format=%(refname) %(objectname)',
            'refs/heads/*',
            'refs/tags/*',
        ],
        check=True,
        capture_output=True,
        text=True,
        env=git_env(),
    ).stdout
    return dict(line.split(' ', maxsplit=1) for line in output.splitlines())


def _git_source_state(repo: Path) -> tuple[dict[str, str], str]:
    remotes = subprocess.run(
        ['git', '-C', repo, 'remote', '-v'],
        check=True,
        capture_output=True,
        text=True,
        env=git_env(),
    ).stdout
    return _git_refs(repo), remotes


def _host_calls() -> list[object]:
    calls = Path(os.environ['PUBANON_FAKE_GH_CALLS'])
    if not calls.exists():
        return []
    return [json.loads(line) for line in calls.read_text().splitlines()]


def _host_call_args() -> list[list[str]]:
    return [cast('dict[str, list[str]]', call)['args'] for call in _host_calls()]


def _init_answers(
    account: str, owner: str, checks: str, description: str, *, guided: bool
) -> list[tuple[str, str]]:
    return [
        ('Use this selection? [c]ontinue / [r]eview / [b]ack / [q]uit: ', 'c' if guided else 'r'),
        *(
            []
            if guided
            else [
                (
                    (
                        'Include these files? [i]nclude / [e]xclude / [c]ustomize / '
                        '[b]ack / [q]uit: '
                    ),
                    'c',
                ),
                ('Replacement pattern: ', '**'),
                ('Use this selection? [c]ontinue / [r]eview / [b]ack / [q]uit: ', 'c'),
            ]
        ),
        (f'GitHub owner [{account}]: ', owner),
        ('GitHub repository [demo]: ', 'public-repo'),
        ('Repository visibility (private/public) [private]: ', 'private'),
        ('Publication route (staged/direct) [direct]: ', ''),
        ('Create local tags marking the source of each publication? [y/N] ', ''),
        ('Required CI checks (comma separated, blank for none): ', checks),
        ('Repository description (blank for none): ', description),
        ('Enable issues? [Y/n] ', 'yes'),
        ('Enable wiki? [y/N] ', 'no'),
        ('Enable projects? [y/N] ', 'no'),
        ('Enable pull requests? [y/N] ', 'no'),
        ('Enable discussions? [y/N] ', 'no'),
        ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 'save'),
    ]


def _make_private_source_revision(source: Path) -> str:
    (source / 'private-notes.txt').write_text('private-only-content\n')
    subprocess.run(['git', '-C', source, 'add', 'private-notes.txt'], check=True, env=git_env())
    subprocess.run(
        [
            'git',
            '-C',
            source,
            '-c',
            'user.name=Private Author',
            '-c',
            'user.email=private@example.invalid',
            'commit',
            '-qm',
            'private-only-message',
        ],
        check=True,
        env=git_env(),
    )
    return subprocess.run(
        ['git', '-C', source, 'rev-parse', 'HEAD'],
        check=True,
        capture_output=True,
        text=True,
        env=git_env(),
    ).stdout.strip()


def _prepare_guided_review(
    fixture: _LifecycleFixture,
    capsys: pytest.CaptureFixture[str],
    cli_output: list[str],
) -> tuple[_candidate.CandidateRecord, Path]:
    (fixture._source / 'README.md').write_text('omitted staged edit\n')
    subprocess.run(['git', '-C', fixture._source, 'add', 'README.md'], check=True, env=git_env())
    (fixture._source / 'docs/guide.md').write_text('omitted unstaged edit\n')
    (fixture._source / 'untracked.txt').write_text('omitted untracked edit\n')
    assert _cli.main(['prepare', '--version', '1.0.0', '--yes']) == 0
    prepared = capsys.readouterr()
    cli_output.extend((prepared.out, prepared.err))
    assert 'left out: 1 staged change, 1 unstaged change, 1 untracked change' in prepared.out
    candidate = _candidate.load_candidate(fixture._paths, 'demo')
    assert candidate is not None

    assert _cli.main(['inspect']) == 0
    inspected = capsys.readouterr()
    cli_output.extend((inspected.out, inspected.err))
    inspection = fixture._paths.data_dir / 'projects' / 'demo' / 'inspections' / candidate.commit
    assert sorted(path.relative_to(inspection).as_posix() for path in inspection.rglob('*')) == [
        'README.md',
        'docs',
        'docs/guide.md',
    ]
    assert (inspection / 'README.md').read_text() == 'sanitized release\n'
    assert (inspection / 'docs/guide.md').read_text() == 'public guide\n'
    assert _cli.main(['inspect']) == 0
    reused = capsys.readouterr()
    cli_output.extend((reused.out, reused.err))
    assert f'Review copy: {display_path(inspection)}' in reused.out
    assert (inspection / 'README.md').read_text() == 'sanitized release\n'

    return candidate, inspection


def _record_release_host_events(walk: _ReleaseWalk) -> None:
    calls = _host_calls()
    for call in calls[walk._host_call_cursor :]:
        walk._events.append(('host', json.dumps(call)))
    walk._host_call_cursor = len(calls)


def _release_published_snapshot(
    walk: _ReleaseWalk,
) -> tuple[dict[str, str], tuple[str | None, dict[str, str]]]:
    fixture = walk._fixture
    return (
        _git_refs(fixture._publication_host),
        _candidate.published_refs(fixture._paths, 'demo'),
    )


def _repository_response(
    owner: str,
    repo: str,
    visibility: str,
    *,
    once: bool = False,
) -> dict[str, object]:
    return {
        'args': [
            'api',
            f'repos/{owner}/{repo}',
            '--jq',
            _host._REPOSITORY_QUERY,
        ],
        'stdout': json.dumps(
            {
                'owner': owner,
                'repo': repo,
                'visibility': visibility,
            }
        ),
        'once': once,
    }


def _reveal_after_disclosure_review(
    monkeypatch: pytest.MonkeyPatch,
    capture_output: Callable[[], tuple[str, str]],
    owner: str,
    repo: str,
) -> None:
    prior_calls = len(_host_call_args())
    reveal_prompts = [
        'These runs, logs, and artifacts become public with the repository. Continue? [y/n] ',
        'Make it public? [y/n] ',
    ]

    def _reveal_answer(prompt: str) -> str:
        assert reveal_prompts, f'unexpected prompt: {prompt}'
        assert prompt == reveal_prompts.pop(0)
        assert not any('PATCH' in call for call in _host_call_args()[prior_calls:])
        if reveal_prompts:
            before_disclosure, _ = capture_output()
            assert 'matches GitHub; no sensitive terms or identity problems' in before_disclosure
            assert 'Pubanon has not scanned Actions logs, artifact contents' in before_disclosure
            assert 'Saved inventory:' not in before_disclosure
            assert f'https://github.com/{owner}/{repo}/actions' in before_disclosure
        return 'yes'

    monkeypatch.setattr('builtins.input', _reveal_answer)
    assert _cli.main(['repo', 'reveal']) == 0
    assert reveal_prompts == []


def _rewrite_and_prune_source(source: Path, original: str) -> str:
    subprocess.run(
        [
            'git',
            '-C',
            source,
            '-c',
            'user.name=Private Author',
            '-c',
            'user.email=private@example.invalid',
            'commit',
            '--amend',
            '-qm',
            'rewritten-message',
        ],
        check=True,
        env=git_env(),
    )
    rewritten = subprocess.run(
        ['git', '-C', source, 'rev-parse', 'HEAD'],
        check=True,
        capture_output=True,
        text=True,
        env=git_env(),
    ).stdout.strip()
    assert rewritten != original
    subprocess.run(
        ['git', '-C', source, 'reflog', 'expire', '--expire=now', '--all'],
        check=True,
        env=git_env(),
    )
    subprocess.run(['git', '-C', source, 'gc', '--prune=now'], check=True, env=git_env())
    assert (
        subprocess.run(
            ['git', '-C', source, 'cat-file', '-e', f'{original}^{{commit}}'],
            check=False,
            env=git_env(),
        ).returncode
        == 0
    )
    return rewritten


def _run_release_unattended_reveal_refusal(
    walk: _ReleaseWalk,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _config.load_project(walk._fixture._paths, 'demo').visibility == 'public'

    calls_before_refusal = _host_calls()
    assert _cli.main(['run', 'reveal-declared', '--yes']) == 1
    refused = capsys.readouterr()
    assert '--yes cannot answer the repository disclosure acknowledgment' in refused.err
    assert 'run pubanon repo reveal interactively' in refused.err
    assert _host_calls() == calls_before_refusal


def _script_answers(
    monkeypatch: pytest.MonkeyPatch,
    answers: Sequence[tuple[str, str]],
) -> Callable[[], None]:
    remaining = list(answers)

    def _answer(prompt: str) -> str:
        assert remaining, f'unexpected prompt: {prompt}'
        expected, response = remaining.pop(0)
        assert prompt == expected
        return response

    def _assert_consumed() -> None:
        assert remaining == []

    monkeypatch.setattr('builtins.input', _answer)
    return _assert_consumed


def _assert_negative_gates(
    paths: _config.Paths,
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _commit(source, 'private-canary\n')
    assert_answers = _script_answers(
        monkeypatch,
        [],
    )
    assert _cli.main(['prepare', '--version', '1.2.0', '--yes']) == 1
    assert_answers()
    gate_error = capsys.readouterr().err
    assert 'cannot prepare: blocked text in included files' in gate_error
    assert 'private-canary' in gate_error
    assert _candidate.load_candidate(paths, 'demo') is None

    _commit(source, 'third release\n', add_env=True)
    assert_answers = _script_answers(
        monkeypatch, [('Include it? [i]nclude / [e]xclude / [q]uit: ', 'q')]
    )
    assert _cli.main(['prepare', '--version', '1.2.0', '--acknowledge-new-paths']) == 130
    assert_answers()
    credential_walk = capsys.readouterr()
    assert 'Credential-like file: .env' in credential_walk.out
    assert 'Canceled. Nothing was saved.' in credential_walk.err
    assert _candidate.load_candidate(paths, 'demo') is None


def _initialize_lifecycle(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    guided_continue: bool = False,
) -> _LifecycleFixture:
    account = 'release-account'
    home = tmp_path / 'm2-home'
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / 'config'))
    monkeypatch.setenv('XDG_DATA_HOME', str(home / 'data'))
    source = make_repo('m2-source', files={'README.md': 'historical-canary\n'})
    publication_host = tmp_path / 'public-repo.git'
    staging_host = tmp_path / 'staging-repo.git'
    url_template = f'{tmp_path.as_uri()}/{{repo}}.git'
    monkeypatch.setattr(_host, '_URL_TEMPLATE', url_template)
    monkeypatch.chdir(source)

    paths = _config.derive_paths(os.environ)
    paths.config_dir.mkdir(parents=True)
    paths.global_config.write_text(
        'anon_name = "Anonymous Publisher"\n'
        'anon_email = "anonymous@example.invalid"\n'
        'sensitive_terms = ["private-canary"]\n'
        'default_publication_route = "direct"\n'
        '[accounts]\n'
        f'github = "{account}"\n'
    )
    assert_answers = _script_answers(
        monkeypatch, _init_answers(account, account, 'Acceptance', '', guided=guided_continue)
    )
    assert _cli.main(['init', '--project', 'demo']) == 0
    assert_answers()
    settings = Settings(
        description='Offline acceptance repository',
        default_branch='main',
        issues=True,
        wiki=False,
        projects=False,
        pull_requests=False,
        discussions=False,
    )
    project = replace(
        _config.load_project(paths, 'demo'),
        settings=settings,
        _stage_repo='staging-repo',
    )
    _config.save_project(paths, project)
    assert project.required_checks == ('Acceptance',)
    assert _host_calls() == []
    return _LifecycleFixture(
        _account=account,
        _check='Acceptance',
        _fake_gh=fake_gh,
        _owner=account,
        _paths=paths,
        _publication_host=publication_host,
        _publication_repo='public-repo',
        _settings=settings,
        _source=source,
        _staging_host=staging_host,
        _staging_repo='staging-repo',
        _tmp_path=tmp_path,
    )


def _settings_response(
    owner: str,
    repo: str,
    settings: Settings,
    *,
    once: bool = False,
) -> dict[str, object]:
    return {
        'args': [
            'api',
            f'repos/{owner}/{repo}',
            '--jq',
            _host._REPOSITORY_SETTINGS_QUERY,
        ],
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
        'once': once,
    }


def _created_settings_responses(
    owner: str, repo: str, settings: Settings
) -> list[dict[str, object]]:
    return [
        {
            'args': [
                'api',
                '-X',
                'PATCH',
                f'repos/{owner}/{repo}',
                '-F',
                f'has_pull_requests={str(settings.pull_requests).lower()}',
            ],
        },
        _settings_response(owner, repo, settings),
    ]


def _verify_host_excludes_source(
    host: Path,
    source: Path,
    original: str,
    bookmark: str,
    private_record: bytes,
) -> None:
    objects = subprocess.run(
        ['git', '-C', host, 'cat-file', '--batch-all-objects', '--batch-check=%(objectname)'],
        check=True,
        capture_output=True,
        text=True,
        env=git_env(),
    ).stdout.splitlines()
    source_ancestry = subprocess.run(
        ['git', '-C', source, 'rev-list', original],
        check=True,
        capture_output=True,
        text=True,
        env=git_env(),
    ).stdout.splitlines()
    assert set(source_ancestry).isdisjoint(objects)
    hosted = b''.join(
        subprocess.run(
            ['git', '-C', host, 'cat-file', '-p', oid],
            check=True,
            capture_output=True,
            env=git_env(),
        ).stdout
        for oid in objects
    )
    for private_value in (
        original.encode(),
        b'private-only-message',
        b'private-only-content',
        b'private-notes.txt',
        str(source).encode(),
        b'[source_bookmark]',
        bookmark.encode(),
        private_record,
    ):
        assert private_value not in hosted


def _verify_publication_history(
    capsys: pytest.CaptureFixture[str],
    locations: tuple[_config.Paths, Path],
    published: str,
    original: str,
    rewritten: str,
) -> None:
    paths, source = locations
    capsys.readouterr()
    assert _cli.main(['list']) == 0
    listed = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'v1.0.0' in listed
    assert published[:12] in listed
    assert _cli.main(['show', 'v1.0.0', '--verbose']) == 0
    shown = re.sub(r': +', ': ', capsys.readouterr().out)
    assert f'Snapshot commit: {published}' in shown
    assert f'Original source: {original}' in shown
    assert f'Original source: {rewritten}' not in shown
    assert any(
        line.startswith('Latest match on ') and line.endswith(f': {rewritten}')
        for line in shown.splitlines()
    )
    assert 'Number of matches: 1\n' in shown
    assert 'Visibility at publication: private' in shown
    commands = [
        shlex.split(line.partition(': ')[2])
        for line in shown.splitlines()
        if line.startswith(('View source: git ', 'Remove source worktree: git '))
    ]
    destination = paths.data_dir / 'projects' / 'demo' / 'source-worktrees' / published
    assert commands == [
        ['git', '-C', str(source), 'worktree', 'add', '--detach', str(destination), original],
        ['git', '-C', str(source), 'worktree', 'remove', str(destination)],
    ]


def _verify_published_inspection(paths: _config.Paths, published: str) -> Path:
    assert _cli.main(['inspect', '--published', 'v1.0.0']) == 0
    copy = paths.data_dir / 'projects' / 'demo' / 'inspections' / published
    assert (copy / 'README.md').read_text() == 'first release\n'
    assert not (copy / '.git').exists()
    _filesystem.chmod(copy / 'README.md', 0o600)
    (copy / 'README.md').write_text('local edit\n')
    assert _cli.main(['inspect', '--published', 'v1.0.0']) == 1
    assert (copy / 'README.md').read_text() == 'local edit\n'
    assert _cli.main(['inspect', '--published', 'v1.0.0', '--refresh']) == 0
    assert (copy / 'README.md').read_text() == 'first release\n'
    assert _cli.main(['clean', '--yes']) == 0
    assert not copy.exists()
    assert _cli.main(['inspect', '--published', 'v1.0.0']) == 0
    assert (copy / 'README.md').read_text() == 'first release\n'
    return copy


def _write_host_responses(path: Path) -> None:
    path.write_text(
        json.dumps(
            [
                {
                    'args': ['api', 'user', '--jq', '.login'],
                    'stdout': 'release-account\n',
                },
                {
                    'args': [
                        'api',
                        'repos/release-owner/public-repo',
                        '--jq',
                        _host._REPOSITORY_QUERY,
                    ],
                    'stdout': json.dumps(
                        {
                            'owner': 'release-owner',
                            'repo': 'public-repo',
                            'visibility': 'private',
                        }
                    ),
                },
            ]
        )
        + '\n'
    )


def _initialize(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_config.Paths, Path, Path]:
    home = tmp_path / 'home'
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / 'config'))
    monkeypatch.setenv('XDG_DATA_HOME', str(home / 'data'))
    source = make_repo('source', files={'README.md': 'first release\n'})
    host = tmp_path / 'host.git'
    subprocess.run(['git', 'init', '--bare', '-q', host], check=True, env=git_env())
    _write_host_responses(fake_gh)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', str(host))
    monkeypatch.chdir(source)

    paths = _config.derive_paths(os.environ)
    paths.config_dir.mkdir(parents=True)
    paths.global_config.write_text(
        'anon_name = "Anonymous Publisher"\n'
        'anon_email = "anonymous@example.invalid"\n'
        'sensitive_terms = ["private-canary"]\n'
        'default_publication_route = "direct"\n'
        '[accounts]\n'
        'github = "release-account"\n'
    )
    assert_answers = _script_answers(
        monkeypatch, _init_answers('release-account', 'release-owner', 'none', '', guided=False)
    )
    assert _cli.main(['init', '--project', 'demo']) == 0
    assert_answers()
    assert _config.load_project(paths, 'demo').allow == ('**',)
    assert _host_calls() == []
    return paths, source, host


def _write_responses(path: Path, responses: list[dict[str, object]]) -> None:
    path.write_text(json.dumps(responses) + '\n')


def _assert_policy_growth_acknowledgment(
    fixture: _LifecycleFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    published_commit: str,
) -> None:
    assert _cli.main(['init', '--project', 'demo', '--publication-route', 'direct']) == 0
    assert _config.load_project(fixture._paths, 'demo').publication_route == 'direct'
    fixture._paths.global_config.write_text(
        'anon_name = "Anonymous Publisher"\n'
        'anon_email = "anonymous@example.invalid"\n'
        'sensitive_terms = ["private-canary", "historical-canary"]\n'
        'default_publication_route = "direct"\n'
        '[accounts]\n'
        f'github = "{fixture._account}"\n'
    )
    capsys.readouterr()
    assert _cli.main(['audit']) == 0
    exposure_audit = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'Warning: historical exposure' in exposure_audit
    assert f'Published snapshot: v1.0.0 ({published_commit[:12]})' in exposure_audit
    assert '  README.md:1\n' in exposure_audit
    assert (
        'Historical exposure unacknowledged; publish requires interactive acknowledgment.'
        in exposure_audit
    )
    assert 'Text: historical-canary' in exposure_audit

    new_path = 'release-notes.txt'
    (fixture._source / new_path).write_text('public release notes\n')
    _commit(fixture._source, 'fresh clean release\n')
    project = _config.load_project(fixture._paths, 'demo')
    _config.save_project(
        fixture._paths,
        replace(project, allow=(*project.allow, new_path)),
    )

    def _include_new_path(prompt: str) -> str:
        if prompt == 'Use this selection? [c]ontinue / [r]eview / [q]uit: ':
            return 'c'
        assert prompt == 'Include these 1 files that were not in the last release? [y/n] '
        displayed = re.sub(r': +', ': ', capsys.readouterr().out)
        assert f'  {new_path}\n' in displayed
        assert 'Saved list:' not in displayed
        return 'yes'

    monkeypatch.setattr('builtins.input', _include_new_path)
    assert _cli.main(['prepare', '--version', '1.1.0']) == 0
    fresh = _candidate.load_candidate(fixture._paths, 'demo')
    assert fresh is not None
    _write_responses(
        fixture._fake_gh,
        [
            _account_response(fixture._account),
            _repository_response(fixture._owner, fixture._publication_repo, 'public'),
        ],
    )

    def _unexpected_prompt(_prompt: str) -> str:
        pytest.fail('publish --yes requested an exposure acknowledgment')

    monkeypatch.setattr('builtins.input', _unexpected_prompt)
    capsys.readouterr()
    published_refs = _git_refs(fixture._publication_host)
    assert _cli.main(['publish', '--yes']) == 1
    refusal = capsys.readouterr().err
    assert '--yes cannot acknowledge historical exposure' in refusal
    assert _git_refs(fixture._publication_host) == published_refs
    retained = _candidate.load_candidate(fixture._paths, 'demo')
    assert retained is not None
    assert replace(retained, source_bookmark=None) == fresh
    bookmark = _source.describe_bookmark(fixture._source, 'demo', fresh.commit)
    assert retained.source_bookmark == bookmark
    assert _source.source_recovery(bookmark, fresh.pinned.commit).status == 'protected'

    assert_answers = _script_answers(
        monkeypatch,
        [
            ('Continue despite this earlier exposure? [y/n] ', 'yes'),
            (
                (f'Publish v1.1.0 to {fixture._owner}/{fixture._publication_repo}? [y/n] '),
                'yes',
            ),
        ],
    )
    assert _cli.main(['publish']) == 0
    assert_answers()
    assert _config.load_project(fixture._paths, 'demo').acknowledged_exposure
    assert _candidate.load_candidate(fixture._paths, 'demo') is None


def _complete_guided_publication(
    fixture: _LifecycleFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    cli_output: list[str],
) -> _candidate.CandidateRecord:
    candidate, inspection = _prepare_guided_review(fixture, capsys, cli_output)
    _write_responses(
        fixture._fake_gh,
        [
            _account_response(fixture._account),
            _repository_response(fixture._owner, fixture._publication_repo, 'private'),
        ],
    )
    answers = ['no', 'yes']

    def _publication_answer(prompt: str) -> str:
        assert prompt == (
            f'Publish v1.0.0 to {fixture._owner}/{fixture._publication_repo}? [y/n] '
        )
        assert answers, f'unexpected prompt: {prompt}'
        preview = capsys.readouterr()
        cli_output.extend((preview.out, preview.err))
        assert 'Publish v1.0.0' in preview.out
        assert candidate.commit not in preview.out
        assert 'Checked:' in preview.out
        assert 'content, identity, and destination' in preview.out
        assert f'{fixture._owner}/{fixture._publication_repo} (private)' in preview.out
        assert _git_refs(fixture._publication_host) == {}
        assert (inspection / 'README.md').read_text() == 'sanitized release\n'
        return answers.pop(0)

    monkeypatch.setattr('builtins.input', _publication_answer)
    assert _cli.main(['publish']) == 130
    declined = capsys.readouterr()
    assert 'Canceled. Nothing was published.' in declined.err
    cli_output.extend((declined.out, declined.err))
    assert _git_refs(fixture._publication_host) == {}
    retained = _candidate.load_candidate(fixture._paths, 'demo')
    assert retained is not None
    assert replace(retained, source_bookmark=None) == candidate
    bookmark = _source.describe_bookmark(fixture._source, 'demo', candidate.commit)
    assert retained.source_bookmark == bookmark
    assert _source.source_recovery(bookmark, candidate.pinned.commit).status == 'protected'
    assert _cli.main(['publish']) == 0
    assert answers == []
    assert (inspection / 'README.md').read_text() == 'sanitized release\n'
    published = capsys.readouterr()
    cli_output.extend((published.out, published.err))

    return candidate


def _create_lifecycle_repository(
    fixture: _LifecycleFixture,
) -> None:
    # The fake gh process cannot create a directory, so provide the empty bare
    # transport immediately before its scripted repository-creation response.
    subprocess.run(
        ['git', 'init', '--bare', '-q', fixture._publication_host], check=True, env=git_env()
    )
    create_publication = [
        'api',
        '-X',
        'POST',
        'user/repos',
        '-f',
        f'name={fixture._publication_repo}',
        '-F',
        'private=true',
        '-f',
        f'description={fixture._settings.description}',
        '-F',
        'has_issues=true',
        '-F',
        'has_wiki=false',
        '-F',
        'has_projects=false',
        '-F',
        'has_discussions=false',
    ]
    _write_responses(
        fixture._fake_gh,
        [
            _account_response(fixture._account),
            {
                'args': [
                    'api',
                    f'repos/{fixture._owner}/{fixture._publication_repo}',
                    '--jq',
                    _host._REPOSITORY_QUERY,
                ],
                'returncode': 1,
                'stdout': '{"status":"404"}\n',
                'once': True,
            },
            {'args': create_publication},
            *_created_settings_responses(
                fixture._owner, fixture._publication_repo, fixture._settings
            ),
            _repository_response(fixture._owner, fixture._publication_repo, 'private'),
        ],
    )
    assert _cli.main(['repo', 'create', '--yes']) == 0
    assert {'args': create_publication, 'stdin': ''} in _host_calls()


def _edit_real_name_identity(
    paths: _config.Paths,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture_output: Callable[[], tuple[str, str]],
) -> str:
    publication_name = 'Release Author'
    _write_responses(fake_gh, [_account_response(_REAL_ACCOUNT)])
    prompts = [
        ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 'e'),
        'i',
        (f'Publication name [{_REAL_NAME}]: ', publication_name),
        (f'Publication email [{_REAL_EMAIL}]: ', ''),
        ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
    ]

    def _answer(prompt: str) -> str:
        reply = prompts.pop(0)
        if isinstance(reply, str):
            response = reply
        else:
            expected, response = reply
            assert prompt == expected
        if not prompts:
            before_save, _ = capture_output()
            assert f'{_REAL_NAME} <{_REAL_EMAIL}>' in before_save
            assert f'{publication_name} <{_REAL_EMAIL}>' in before_save
        return response

    monkeypatch.setattr('builtins.input', _answer)
    assert _cli.main(['setup', '--edit']) == 0
    assert prompts == []
    capture_output()
    assert _config.load_global(paths).anon_name == publication_name
    return publication_name


def _initialize_guided_first_run(
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answers: Callable[[Sequence[str | tuple[str, str]]], None],
    *,
    login_needed: bool,
) -> tuple[_LifecycleFixture, list[str], dict[str, str], str]:
    tmp_path = Path(os.environ['PUBANON_FAKE_GH_RESPONSES']).parent
    account = 'release-account'
    fake_gh = Path(os.environ['PUBANON_FAKE_GH_RESPONSES'])
    home = tmp_path / 'first-run-home'
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / 'config'))
    monkeypatch.setenv('XDG_DATA_HOME', str(home / 'data'))
    source = make_repo(
        'first-run-source',
        files={
            'README.md': 'sanitized release\n',
            'docs/guide.md': 'public guide\n',
            'docs/private/notes.md': 'private-canary\n',
        },
    )
    subprocess.run(['git', '-C', source, 'branch', 'private-work'], check=True, env=git_env())
    subprocess.run(['git', '-C', source, 'tag', 'internal-checkpoint'], check=True, env=git_env())
    source_refs, source_remotes = _git_source_state(source)
    publication_host = tmp_path / 'public-repo.git'
    url_template = f'{tmp_path.as_uri()}/{{repo}}.git'
    monkeypatch.setattr(_host, '_URL_TEMPLATE', url_template)
    monkeypatch.chdir(source)

    paths = _config.derive_paths(os.environ)
    responses: list[dict[str, object]] = [
        *(
            [
                {
                    'args': ['api', 'user', '--jq', '.login'],
                    'returncode': 1,
                    'stderr': 'ghp_PRIVATE_CREDENTIAL',
                    'once': True,
                },
                {
                    'args': [
                        'auth',
                        'login',
                        '--hostname',
                        'github.com',
                        '--git-protocol',
                        'https',
                        '--web',
                    ]
                },
            ]
            if login_needed
            else []
        ),
        _account_response(account),
    ]
    _write_responses(fake_gh, responses)

    answers(
        [
            ('Publication name: ', 'Publication Alais'),
            ('Publication email: ', 'publication@example.invalid'),
            ('Sensitive term: ', 'private-canary'),
            ('Sensitive term: ', ''),
            ('Default publication route (staged/direct) [staged]: ', 'direct'),
            *([('Authentication: [r]etry / [l]ogin / [c]ancel: ', 'l')] if login_needed else []),
            ('Use this GitHub account? [Y/n] ', ''),
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 'e'),
            'i',
            ('Publication name [Publication Alais]: ', 'Publication Alias'),
            ('Publication email [publication@example.invalid]: ', ''),
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
        ],
    )

    output: list[str] = []
    assert _cli.main(['setup']) == 0
    setup_output = capsys.readouterr()
    output.extend((setup_output.out, setup_output.err))
    assert '  1. private-canary' in setup_output.out
    assert paths.global_config.exists(), 'setup did not create global configuration'
    assert not (paths.config_dir / 'projects').exists()
    setup_calls = _host_call_args()
    account_call = ['api', 'user', '--jq', '.login']
    assert setup_calls == (
        [
            account_call,
            [
                'auth',
                'login',
                '--hostname',
                'github.com',
                '--git-protocol',
                'https',
                '--web',
            ],
            account_call,
            account_call,
        ]
        if login_needed
        else [account_call, account_call]
    )
    assert not any(args[:1] == ['api'] and args[1].startswith('repos/') for args in setup_calls)

    assert _cli.main(['doctor']) == 0
    doctor_output = capsys.readouterr()
    output.extend((doctor_output.out, doctor_output.err))
    assert 'Your configuration and GitHub login are ready.' in doctor_output.out

    assert_answers = _script_answers(
        monkeypatch,
        [
            ('Use this selection? [c]ontinue / [r]eview / [b]ack / [q]uit: ', 'r'),
            (
                ('Include these files? [i]nclude / [e]xclude / [c]ustomize / [b]ack / [q]uit: '),
                'i',
            ),
            (
                ('Include these files? [i]nclude / [e]xclude / [c]ustomize / [b]ack / [q]uit: '),
                'i',
            ),
            (
                ('Include these files? [i]nclude / [e]xclude / [c]ustomize / [b]ack / [q]uit: '),
                'e',
            ),
            *_init_answers(account, account, 'none', 'Offline acceptance repository', guided=True),
        ],
    )
    assert _cli.main(['init', '--project', 'demo']) == 0
    assert_answers()
    init_output = capsys.readouterr()
    output.extend((init_output.out, init_output.err))
    project = _config.load_project(paths, 'demo')
    assert (project.allow, project.deny) == (('README.md', 'docs/guide.md'), ('docs/private/**',))
    assert project.settings is not None
    assert _git_source_state(source) == (source_refs, source_remotes)
    fixture = _LifecycleFixture(
        _account=account,
        _check='',
        _fake_gh=fake_gh,
        _owner=account,
        _paths=paths,
        _publication_host=publication_host,
        _publication_repo='public-repo',
        _settings=project.settings,
        _source=source,
        _staging_host=tmp_path / 'unused-staging.git',
        _staging_repo='unused-staging',
        _tmp_path=tmp_path,
    )
    return fixture, output, source_refs, source_remotes


def _initialize_real_name(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capture_output: Callable[[], tuple[str, str]],
) -> tuple[_config.Paths, Path, Path]:
    home = tmp_path / 'home'
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(home / 'config'))
    monkeypatch.setenv('XDG_DATA_HOME', str(home / 'data'))
    source = make_repo('source', files={'README.md': 'first release\n'})
    host = tmp_path / 'host.git'
    monkeypatch.setattr(_host, '_URL_TEMPLATE', str(host))
    monkeypatch.chdir(source)

    paths = _config.derive_paths(os.environ)
    paths.config_dir.mkdir(parents=True)
    paths.global_config.write_text(
        f'anon_name = "{_REAL_NAME}"\n'
        f'anon_email = "{_REAL_EMAIL}"\n'
        f'sensitive_terms = ["{_REAL_TERM}"]\n'
        'default_publication_route = "direct"\n'
        '[accounts]\n'
        f'github = "{_REAL_ACCOUNT}"\n'
    )
    _write_responses(fake_gh, [_account_response(_REAL_ACCOUNT)])
    monkeypatch.setattr('pubanon._run.check_floors', lambda: None)
    assert _cli.main(['doctor']) == 0
    capture_output()

    assert_answers = _script_answers(
        monkeypatch,
        _init_answers(
            _REAL_ACCOUNT, _REAL_ACCOUNT, 'none', _REAL_SETTINGS.description, guided=False
        ),
    )
    assert _cli.main(['init', '--project', 'demo']) == 0
    assert_answers()
    capture_output()
    project = _config.load_project(paths, 'demo')
    assert project.settings == _REAL_SETTINGS
    return paths, source, host


def _initialize_release_walk(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> _ReleaseWalk:
    fixture = _initialize_lifecycle(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
        guided_continue=True,
    )
    initialized = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'README.md' in initialized
    proposed = fixture._paths.data_dir / 'projects' / 'demo' / 'proposed-selection.json'
    assert json.loads(proposed.read_text(encoding='utf-8')) == ['README.md']
    assert _config.load_project(fixture._paths, 'demo').allow == ('README.md',)
    project = replace(
        _config.load_project(fixture._paths, 'demo'),
        package=PackageObservation('pypi', 'demo-package', ('Release',)),
        workflows={
            'ship': ('prepare', 'inspect', 'publish', 'repo watch'),
            'published': ('publish',),
            'staged': ('prepare', 'stage push', 'stage watch', 'publish'),
            'reveal-declared': ('repo reveal',),
        },
    )
    _config.save_project(fixture._paths, project)

    _create_lifecycle_repository(fixture)
    created = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'Created private repository release-account/public-repo.' in created

    walk = _ReleaseWalk(fixture, [], [], [], [], 0, 0)
    real_build_candidate = _prepare.build_candidate
    real_execute_run_stage = _workflow._execute_run_stage
    real_publication_push = _publish.push
    real_stage_push = _stage.push

    def _build_candidate(
        plan: _prepare.PreparePlan,
        options: _prepare.PrepareOptions,
    ) -> _candidate.CandidateRecord:
        walk._candidate_builds += 1
        return real_build_candidate(plan, options)

    def _execute_run_stage(
        args: argparse.Namespace,
        plan: _workflow._RunPlan,
        state: _workflow._RunState,
        stage: str,
    ) -> None:
        walk._executed_stages.append(stage)
        real_execute_run_stage(args, plan, state, stage)

    def _publication_push(
        destination: _host.Destination,
        cwd: Path,
        args: Sequence[str],
    ) -> GitResult:
        walk._publication_pushes.append((cwd, tuple(args)))
        for arg in args:
            commit, separator, destination_ref = arg.partition(':')
            if separator and destination_ref == 'refs/tags/v0.1.0':
                _append_response(
                    fixture._fake_gh,
                    _check_response(
                        fixture._owner,
                        fixture._publication_repo,
                        commit,
                        fixture._check,
                        conclusion='failure',
                    ),
                )
        return real_publication_push(destination, cwd, args)

    def _stage_push(
        destination: _host.Destination,
        cwd: Path,
        args: Sequence[str],
    ) -> GitResult:
        walk._stage_pushes.append((cwd, tuple(args)))
        commit, separator, destination_ref = args[-1].partition(':')
        assert separator
        assert destination_ref == 'refs/heads/main'
        _append_response(
            fixture._fake_gh,
            _check_response(
                fixture._owner,
                fixture._staging_repo,
                commit,
                fixture._check,
            ),
        )
        return real_stage_push(destination, cwd, args)

    monkeypatch.setattr(_prepare, 'build_candidate', _build_candidate)
    monkeypatch.setattr(_workflow, '_execute_run_stage', _execute_run_stage)
    monkeypatch.setattr(_publish, 'push', _publication_push)
    monkeypatch.setattr(_stage, 'push', _stage_push)
    return walk


def _reveal_clean_and_restore(
    fixture: _LifecycleFixture,
    monkeypatch: pytest.MonkeyPatch,
    staged_oid: str,
) -> None:
    private_settings = _settings_response(
        fixture._owner,
        fixture._publication_repo,
        fixture._settings,
        once=True,
    )
    _write_responses(
        fixture._fake_gh,
        [
            _account_response(fixture._account),
            _repository_response(
                fixture._owner,
                fixture._publication_repo,
                'private',
                once=True,
            ),
            _repository_response(
                fixture._owner,
                fixture._publication_repo,
                'private',
                once=True,
            ),
            _repository_response(fixture._owner, fixture._publication_repo, 'public'),
            _check_response(
                fixture._owner,
                fixture._publication_repo,
                staged_oid,
                fixture._check,
            ),
            private_settings,
            _settings_response(fixture._owner, fixture._publication_repo, fixture._settings),
            *_disclosure_responses(
                fixture._owner,
                fixture._publication_repo,
                artifacts=((201, 'release-package'), (202, 'checks-report')),
            ),
            {
                'args': [
                    'api',
                    '-X',
                    'PATCH',
                    f'repos/{fixture._owner}/{fixture._publication_repo}',
                    '-f',
                    'visibility=public',
                ]
            },
        ],
    )
    assert_answers = _script_answers(
        monkeypatch,
        [
            (
                (
                    'These runs, logs, and artifacts become public with the repository. '
                    'Continue? [y/n] '
                ),
                'yes',
            ),
            (
                'Make it public? [y/n] ',
                'yes',
            ),
        ],
    )
    assert _cli.main(['repo', 'reveal']) == 0
    assert_answers()
    assert _config.load_project(fixture._paths, 'demo').visibility == 'public'
    project_file = fixture._paths.config_dir / 'projects' / 'demo.toml'
    assert 'visibility = "public"' in project_file.read_text()

    delete_stage = [
        'api',
        '-X',
        'DELETE',
        f'repos/{fixture._owner}/{fixture._staging_repo}',
    ]
    _write_responses(
        fixture._fake_gh,
        [
            _account_response(fixture._account),
            _repository_response(
                fixture._owner,
                fixture._staging_repo,
                'private',
                once=True,
            ),
            _repository_response(
                fixture._owner,
                fixture._staging_repo,
                'private',
                once=True,
            ),
            {'args': delete_stage},
            {
                'args': [
                    'api',
                    f'repos/{fixture._owner}/{fixture._staging_repo}',
                    '--jq',
                    _host._REPOSITORY_QUERY,
                ],
                'returncode': 1,
                'stdout': '{"status":"404"}\n',
            },
        ],
    )
    assert _cli.main(['stage', 'delete', '--yes']) == 0
    assert {'args': delete_stage, 'stdin': ''} in _host_calls()
    assert _stage.load_stage(fixture._paths, 'demo') is None
    assert _config.load_project(fixture._paths, 'demo').publication_route == 'staged'

    original_host = fixture._tmp_path / 'published-before-recreation.git'
    fixture._publication_host.rename(original_host)
    subprocess.run(
        ['git', 'init', '--bare', '-q', fixture._publication_host], check=True, env=git_env()
    )
    _write_responses(
        fixture._fake_gh,
        [
            _account_response(fixture._account),
            _repository_response(fixture._owner, fixture._publication_repo, 'public'),
        ],
    )
    assert _cli.main(['repo', 'restore', '--to', 'remote', '--yes']) == 0
    assert _git_refs(fixture._publication_host) == _git_refs(
        _candidate.store_path(fixture._paths, 'demo')
    )


def _run_release_failed_ship(
    walk: _ReleaseWalk,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = walk._fixture

    _write_responses(
        fixture._fake_gh,
        [
            _account_response(fixture._account),
            _repository_response(fixture._owner, fixture._publication_repo, 'private'),
        ],
    )
    assert (
        _cli.main(
            [
                'run',
                'ship',
                '--version',
                '0.1.0',
                '--acknowledge-new-paths',
                '--yes',
            ]
        )
        == 1
    )
    first_run = capsys.readouterr()
    first_refs = _git_refs(fixture._publication_host)
    walk._first_oid = first_refs['refs/tags/v0.1.0']
    assert '  Acceptance: failed' in first_run.err
    assert 'The publication itself completed.' in first_run.err
    assert 'pubanon repo watch' in first_run.err
    assert '--version 0.1.0' in first_run.err
    assert len(walk._publication_pushes) == 1
    assert _candidate.load_candidate(fixture._paths, 'demo') is None
    assert f'/commits/{walk._first_oid}/check-runs?' in json.dumps(_host_calls())


def _run_release_green_release(
    walk: _ReleaseWalk,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = walk._fixture

    _commit(fixture._source, 'second release\n')
    _write_responses(
        fixture._fake_gh,
        [
            _account_response(fixture._account),
            _repository_response(fixture._owner, fixture._publication_repo, 'private'),
        ],
    )
    assert _cli.main(['prepare', '--version', '0.2.0', '--yes']) == 0
    assert _cli.main(['inspect']) == 0
    assert _cli.main(['publish', '--yes']) == 0
    second_run = re.sub(r': +', ': ', capsys.readouterr().out)
    second_refs = _git_refs(fixture._publication_host)
    walk._second_oid = second_refs['refs/tags/v0.2.0']
    assert 'Published v0.2.0 to ' in second_run
    assert walk._second_oid != walk._first_oid
    assert len(walk._publication_pushes) == 2


def _run_release_old_version_resume(
    walk: _ReleaseWalk,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = walk._fixture
    before = _release_published_snapshot(walk)
    _assert_release_published_snapshot(walk, before)
    assert _candidate.load_candidate(fixture._paths, 'demo') is None
    first_stage = len(walk._executed_stages)
    builds_before = walk._candidate_builds

    _write_responses(
        fixture._fake_gh,
        [
            _account_response(fixture._account),
            _repository_response(fixture._owner, fixture._publication_repo, 'private'),
            _check_response(
                fixture._owner,
                fixture._publication_repo,
                walk._first_oid,
                fixture._check,
            ),
        ],
    )
    calls_before_resume = len(_host_calls())
    assert _cli.main(['run', 'ship', '--version', '0.1.0', '--yes']) == 0
    resumed = re.sub(r': +', ': ', capsys.readouterr().out)
    resume_calls = _host_calls()[calls_before_resume:]
    resume_log = json.dumps(resume_calls)
    assert 'Workflow ship (direct route)' in resumed
    assert 'Repo watch' in resumed
    assert 'Required checks passed.' in resumed
    assert len(walk._publication_pushes) == 2
    assert _candidate.load_candidate(fixture._paths, 'demo') is None
    assert walk._executed_stages[first_stage:] == ['repo watch']
    assert walk._candidate_builds == builds_before
    assert f'/commits/{walk._first_oid}/check-runs?' in resume_log
    assert f'/commits/{walk._second_oid}/check-runs?' not in resume_log
    after = _release_published_snapshot(walk)
    _assert_release_published_snapshot(walk, after)
    assert after == before


def _run_release_package_watch(
    walk: _ReleaseWalk,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> list[str]:
    fixture = walk._fixture

    package_payload = json.dumps(
        {
            'info': {'version': '0.3.0'},
            'urls': [
                {'filename': 'demo_package-0.3.0-py3-none-any.whl'},
                {'filename': 'demo_package-0.3.0.tar.gz'},
            ],
        }
    ).encode()
    package_states = iter([(404, b''), (200, package_payload), (200, package_payload)])
    package_timeouts: list[float | None] = []
    package_urls: list[str] = []

    def _fetch_package(
        url: str,
        *,
        timeout: float | None = None,
    ) -> tuple[int, bytes]:
        package_timeouts.append(timeout)
        _record_release_host_events(walk)
        package_urls.append(url)
        status, body = next(package_states)
        walk._events.append(('fetch', f'{status} {url}'))
        return status, body

    now = 0.0

    def _monotonic() -> float:
        return now

    def _sleep(seconds: float) -> None:
        nonlocal now
        _record_release_host_events(walk)
        walk._events.append(('sleep', f'{seconds:g}'))
        now += seconds

    monkeypatch.setattr(_package, '_fetch', _fetch_package)
    monkeypatch.setattr(_watch, '_monotonic', _monotonic)
    monkeypatch.setattr(_watch, '_sleep', _sleep)
    walk._host_call_cursor = len(_host_calls())
    first_event = len(walk._events)
    _write_responses(
        fixture._fake_gh,
        [
            _account_response(fixture._account),
            _repository_response(fixture._owner, fixture._publication_repo, 'private'),
            _check_response(
                fixture._owner,
                fixture._publication_repo,
                walk._third_oid,
                'Release',
            ),
        ],
    )
    assert _cli.main(['package', 'watch', '--timeout', '30']) == 0
    _record_release_host_events(walk)
    package_watch = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'Package release v0.3.0 is published.' in package_watch
    assert 'demo_package-0.3.0-py3-none-any.whl' in package_watch
    assert 'demo_package-0.3.0.tar.gz' in package_watch
    assert len(package_urls) == 2
    assert package_timeouts == [30.0, 15.0]
    assert package_urls == [
        'https://pypi.org/pypi/demo-package/0.3.0/json',
        'https://pypi.org/pypi/demo-package/0.3.0/json',
    ]
    release_path = (
        f'repos/{fixture._owner}/{fixture._publication_repo}/commits/{walk._third_oid}/check-runs?'
        'check_name=Release&filter=latest&per_page=100'
    )
    _assert_release_package_events(walk._events[first_event:], release_path)
    return package_urls


def _run_release_published_noop(
    walk: _ReleaseWalk,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = walk._fixture
    before = _release_published_snapshot(walk)
    _assert_release_published_snapshot(walk, before)
    assert _candidate.load_candidate(fixture._paths, 'demo') is None
    first_stage = len(walk._executed_stages)
    builds_before = walk._candidate_builds

    _write_responses(
        fixture._fake_gh,
        [
            _account_response(fixture._account),
            _repository_response(fixture._owner, fixture._publication_repo, 'private'),
        ],
    )
    stage_before_noop = _stage.load_stage(fixture._paths, 'demo')
    assert _cli.main(['run', 'published', '--version', '0.1.0', '--yes']) == 0
    noop = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'Nothing to do: v0.1.0 is already published.' in noop
    assert len(walk._publication_pushes) == 2
    assert _stage.load_stage(fixture._paths, 'demo') == stage_before_noop
    assert _candidate.load_candidate(fixture._paths, 'demo') is None
    assert walk._executed_stages[first_stage:] == []
    assert walk._candidate_builds == builds_before
    after = _release_published_snapshot(walk)
    _assert_release_published_snapshot(walk, after)
    assert after == before


def _run_release_reveal_with_package_gate(
    walk: _ReleaseWalk,
    package_urls: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = walk._fixture
    first_event = len(walk._events)

    _write_responses(
        fixture._fake_gh,
        [
            _account_response(fixture._account),
            _repository_response(
                fixture._owner,
                fixture._publication_repo,
                'private',
                once=True,
            ),
            _repository_response(
                fixture._owner,
                fixture._publication_repo,
                'private',
                once=True,
            ),
            _repository_response(fixture._owner, fixture._publication_repo, 'public'),
            _check_response(
                fixture._owner,
                fixture._publication_repo,
                walk._third_oid,
                fixture._check,
            ),
            _check_response(
                fixture._owner,
                fixture._publication_repo,
                walk._third_oid,
                'Release',
            ),
            _settings_response(
                fixture._owner,
                fixture._publication_repo,
                fixture._settings,
                once=True,
            ),
            _settings_response(fixture._owner, fixture._publication_repo, fixture._settings),
            *_disclosure_responses(
                fixture._owner,
                fixture._publication_repo,
                artifacts=((201, 'release-package'), (202, 'checks-report')),
            ),
            {
                'args': [
                    'api',
                    '-X',
                    'PATCH',
                    f'repos/{fixture._owner}/{fixture._publication_repo}',
                    '-f',
                    'visibility=public',
                ]
            },
        ],
    )
    expected_prompts = [
        'These runs, logs, and artifacts become public with the repository. Continue? [y/n] ',
        'Make it public? [y/n] ',
    ]
    reveal_output: list[str] = []

    def _reveal_answer(prompt: str) -> str:
        assert prompt == expected_prompts.pop(0)
        visible = re.sub(r': +', ': ', capsys.readouterr().out)
        reveal_output.append(visible)
        if expected_prompts:
            assert '  release-package (id 201)\n  checks-report (id 202)' in visible
        else:
            assert 'Make it public?' in prompt
        return 'yes'

    monkeypatch.setattr('builtins.input', _reveal_answer)
    assert _cli.main(['repo', 'reveal']) == 0
    assert expected_prompts == []
    _record_release_host_events(walk)
    reveal = ''.join((*reveal_output, capsys.readouterr().out))
    assert re.search(r'Package: +demo-package 0.3.0 is published', reveal)
    assert f'Made {fixture._owner}/{fixture._publication_repo} public.' in reveal
    assert len(package_urls) == 3
    check_prefix = (
        f'repos/{fixture._owner}/{fixture._publication_repo}/commits/{walk._third_oid}/check-runs?'
    )
    _assert_release_reveal_events(
        walk._events[first_event:],
        f'{check_prefix}check_name={fixture._check}&filter=latest&per_page=100',
        f'{check_prefix}check_name=Release&filter=latest&per_page=100',
    )


def _run_release_package_and_reveal(
    walk: _ReleaseWalk,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package_urls = _run_release_package_watch(walk, monkeypatch, capsys)
    _run_release_reveal_with_package_gate(walk, package_urls, monkeypatch, capsys)


def _run_release_staged_release(
    walk: _ReleaseWalk,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = walk._fixture
    project = _config.load_project(fixture._paths, 'demo')
    _config.save_project(fixture._paths, replace(project, publication_route='staged'))

    _commit(fixture._source, 'third release\n')
    subprocess.run(
        ['git', 'init', '--bare', '-q', fixture._staging_host], check=True, env=git_env()
    )
    stage_missing: dict[str, object] = {
        'args': [
            'api',
            f'repos/{fixture._owner}/{fixture._staging_repo}',
            '--jq',
            _host._REPOSITORY_QUERY,
        ],
        'returncode': 1,
        'stdout': '{"status":"404"}\n',
        'once': True,
    }
    create_stage = [
        'api',
        '-X',
        'POST',
        'user/repos',
        '-f',
        f'name={fixture._staging_repo}',
        '-F',
        'private=true',
        '-f',
        f'description={fixture._settings.description}',
        '-F',
        'has_issues=true',
        '-F',
        'has_wiki=false',
        '-F',
        'has_projects=false',
        '-F',
        'has_discussions=false',
    ]
    create_stage_response: dict[str, object] = {'args': create_stage}
    _write_responses(
        fixture._fake_gh,
        [
            _account_response(fixture._account),
            _repository_response(fixture._owner, fixture._publication_repo, 'private'),
            stage_missing,
            stage_missing,
            stage_missing,
            create_stage_response,
            *_created_settings_responses(fixture._owner, fixture._staging_repo, fixture._settings),
            _repository_response(fixture._owner, fixture._staging_repo, 'private'),
        ],
    )
    prompts_seen: list[str] = []
    staged_output: list[str] = []

    def _staged_answer(prompt: str) -> str:
        if prompt.startswith('Use this selection?'):
            assert _candidate.load_candidate(fixture._paths, 'demo') is None
            return 'continue'
        record = _candidate.load_candidate(fixture._paths, 'demo')
        assert record is not None
        expected = [
            (
                f'Create the private staging repository '
                f'{fixture._owner}/{fixture._staging_repo}? [y/n] '
            ),
            f'Push {record.commit[:12]} to the staging repository? [y/n] ',
            (f'Publish v0.3.0 to {fixture._owner}/{fixture._publication_repo}? [y/n] '),
        ]
        assert prompt == expected[len(prompts_seen)]
        visible = re.sub(r': +', ': ', capsys.readouterr().out)
        staged_output.append(visible)
        assert record.commit[:12] in visible if len(prompts_seen) < 2 else 'v0.3.0' in visible
        prompts_seen.append(prompt)
        return 'yes'

    monkeypatch.setattr('builtins.input', _staged_answer)
    assert _cli.main(['run', 'staged', '--version', '0.3.0']) == 0
    assert len(prompts_seen) == 3
    staged_run = ''.join((*staged_output, capsys.readouterr().out))
    third_refs = _git_refs(fixture._publication_host)
    walk._third_oid = third_refs['refs/tags/v0.3.0']
    stage_record = _stage.load_stage(fixture._paths, 'demo')
    assert stage_record == _stage.StageRecord(
        walk._third_oid,
        fixture._owner,
        fixture._staging_repo,
    )
    assert _git_refs(fixture._staging_host) == {'refs/heads/main': walk._third_oid}
    assert 'create staging repository' in staged_run
    assert 'stage watch' in staged_run
    assert 'Required checks passed.' in staged_run
    assert len(walk._stage_pushes) == 1
    assert len(walk._publication_pushes) == 3
    assert (
        f'repos/{fixture._owner}/{fixture._staging_repo}/commits/{walk._third_oid}/check-runs?'
        in json.dumps(_host_calls())
    )


def _stage_and_publish_lifecycle_candidate(
    fixture: _LifecycleFixture,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> str:
    project = _config.load_project(fixture._paths, 'demo')
    _config.save_project(fixture._paths, replace(project, publication_route='staged'))
    assert _cli.main(['prepare', '--version', '1.0.0', '--yes']) == 0
    candidate = _candidate.load_candidate(fixture._paths, 'demo')
    assert candidate is not None

    # This is the same fake-create seam for the disposable staging transport.
    subprocess.run(
        ['git', 'init', '--bare', '-q', fixture._staging_host], check=True, env=git_env()
    )
    _write_responses(
        fixture._fake_gh,
        [
            _account_response(fixture._account),
            {
                'args': [
                    'api',
                    f'repos/{fixture._owner}/{fixture._staging_repo}',
                    '--jq',
                    _host._REPOSITORY_QUERY,
                ],
                'returncode': 1,
                'stdout': '{"status":"404"}\n',
                'once': True,
            },
            {
                'args': [
                    'api',
                    '-X',
                    'POST',
                    'user/repos',
                    '-f',
                    f'name={fixture._staging_repo}',
                    '-F',
                    'private=true',
                    '-f',
                    f'description={fixture._settings.description}',
                    '-F',
                    'has_issues=true',
                    '-F',
                    'has_wiki=false',
                    '-F',
                    'has_projects=false',
                    '-F',
                    'has_discussions=false',
                ]
            },
            *_created_settings_responses(fixture._owner, fixture._staging_repo, fixture._settings),
            _repository_response(fixture._owner, fixture._staging_repo, 'private'),
        ],
    )
    assert_answers = _script_answers(
        monkeypatch,
        [
            (
                (
                    f'Create the private staging repository '
                    f'{fixture._owner}/{fixture._staging_repo}?'
                    ' [y/n] '
                ),
                'yes',
            )
        ],
    )
    assert _cli.main(['stage', 'push', '--yes']) == 0
    assert_answers()
    staged_oid = _git_refs(fixture._staging_host)['refs/heads/main']
    assert staged_oid == candidate.commit

    _write_responses(
        fixture._fake_gh,
        [
            _account_response(fixture._account),
            _repository_response(fixture._owner, fixture._staging_repo, 'private'),
            _settings_response(fixture._owner, fixture._staging_repo, fixture._settings),
            _check_response(
                fixture._owner,
                fixture._staging_repo,
                staged_oid,
                fixture._check,
            ),
        ],
    )
    capsys.readouterr()
    assert _cli.main(['stage', 'status']) == 0
    staged = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'Checks: Acceptance passed' in staged
    assert f'Staged: v1.0.0 ({staged_oid[:12]})' in staged

    _write_responses(
        fixture._fake_gh,
        [
            _account_response(fixture._account),
            _repository_response(fixture._owner, fixture._publication_repo, 'private'),
            _repository_response(fixture._owner, fixture._staging_repo, 'private'),
            _settings_response(fixture._owner, fixture._staging_repo, fixture._settings),
            _check_response(fixture._owner, fixture._staging_repo, staged_oid, fixture._check),
        ],
    )
    assert _cli.main(['publish', '--yes']) == 0
    publication_output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'Published v1.0.0' in publication_output
    assert 'Verified staged commit:' not in publication_output
    assert _git_refs(fixture._publication_host) == {
        'refs/heads/main': staged_oid,
        'refs/tags/v1.0.0': staged_oid,
    }

    assert _cli.main(['audit']) == 0
    clean_audit = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'audit: local copy of published history' in clean_audit
    assert 'Warning: historical exposure' not in clean_audit
    assert 'Warning: policy mismatch' not in clean_audit
    assert 'identity problem:' not in clean_audit
    return staged_oid


@pytest.mark.slow
@pytest.mark.parametrize('login_needed', [False, True], ids=['authenticated', 'login-needed'])
def test_guided_first_publication_works_from_empty_roots(
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answers: Callable[[Sequence[str | tuple[str, str]]], None],
    login_needed: bool,
) -> None:
    fixture, cli_output, source_refs, source_remotes = _initialize_guided_first_run(
        make_repo,
        monkeypatch,
        capsys,
        answers,
        login_needed=login_needed,
    )
    config = _config.load_global(fixture._paths)
    assert config.anon_name == 'Publication Alias'
    assert config.anon_email == 'publication@example.invalid'
    assert config.sensitive_terms == ('private-canary',)
    assert config.accounts == {'github': fixture._account}

    _create_lifecycle_repository(fixture)
    created = capsys.readouterr()
    cli_output.extend((created.out, created.err))

    candidate = _complete_guided_publication(fixture, monkeypatch, capsys, cli_output)

    refs = _git_refs(fixture._publication_host)
    assert refs == {
        'refs/heads/main': candidate.commit,
        'refs/tags/v1.0.0': candidate.commit,
    }
    author, committer, parents, signed = _commit_headers(
        fixture._publication_host, candidate.commit
    )
    expected_identity = 'Publication Alias <publication@example.invalid>'
    assert author.startswith(f'author {expected_identity} ')
    assert committer.startswith(f'committer {expected_identity} ')
    assert parents == []
    assert signed is False
    tree = subprocess.run(
        ['git', '-C', fixture._publication_host, 'ls-tree', '-r', '--name-only', candidate.commit],
        check=True,
        capture_output=True,
        text=True,
        env=git_env(),
    ).stdout
    assert tree == 'README.md\ndocs/guide.md\n'
    content = subprocess.run(
        ['git', '-C', fixture._publication_host, 'show', f'{candidate.commit}:README.md'],
        check=True,
        capture_output=True,
        text=True,
        env=git_env(),
    ).stdout
    assert content == 'sanitized release\n'

    _write_responses(
        fixture._fake_gh,
        [
            _account_response(fixture._account),
            _repository_response(fixture._owner, fixture._publication_repo, 'private'),
        ],
    )
    assert _cli.main(['audit', '--remote']) == 0
    audited = capsys.readouterr()
    cli_output.extend((audited.out, audited.err))
    assert 'audit: local copy of published history; verified against GitHub' in audited.out
    assert 'Warning: historical exposure' not in audited.out
    assert 'Warning: policy mismatch' not in audited.out
    assert 'identity problem:' not in audited.out

    assert _git_source_state(fixture._source) == (source_refs, source_remotes)
    rendered = ''.join(cli_output)
    for expected in (
        'Publication Alias <publication@example.invalid>',
        'GitHub account: release-account',
        '  1. private-canary',
    ):
        assert expected in rendered
    assert 'private-canary' in rendered
    if login_needed:
        assert 'ghp_PRIVATE_CREDENTIAL' in rendered


@pytest.mark.slow
def test_guided_release_flow_works_offline_end_to_end(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    walk = _initialize_release_walk(tmp_path, fake_gh, make_repo, monkeypatch, capsys)
    _run_release_failed_ship(walk, capsys)
    _run_release_green_release(walk, capsys)
    _run_release_old_version_resume(walk, capsys)
    _run_release_published_noop(walk, capsys)
    _run_release_staged_release(walk, monkeypatch, capsys)
    _run_release_package_and_reveal(walk, monkeypatch, capsys)
    _run_release_unattended_reveal_refusal(walk, capsys)


@pytest.mark.slow
def test_lifecycle_and_policy_growth_work_offline(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    fixture = _initialize_lifecycle(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )
    capsys.readouterr()

    _create_lifecycle_repository(fixture)
    staged_oid = _stage_and_publish_lifecycle_candidate(
        fixture,
        monkeypatch,
        capsys,
    )
    _reveal_clean_and_restore(
        fixture,
        monkeypatch,
        staged_oid=staged_oid,
    )
    _assert_policy_growth_acknowledgment(
        fixture,
        monkeypatch,
        capsys,
        published_commit=staged_oid,
    )


@pytest.mark.slow
def test_publication_history_survives_rebase_and_cleanup(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, source, host = _initialize(tmp_path, fake_gh, make_repo, monkeypatch)
    project = _config.load_project(paths, 'demo')
    _config.save_project(paths, replace(project, allow=('README.md',)))
    original = _make_private_source_revision(source)

    assert_answers = _script_answers(
        monkeypatch,
        [
            ('Include these files? [i]nclude / [e]xclude / [c]ustomize / [q]uit: ', 'e'),
            ('Use this selection? [c]ontinue / [r]eview / [q]uit: ', 'c'),
        ],
    )
    assert _cli.main(['prepare', '--version', '1.0.0']) == 0
    assert_answers()
    candidate = _candidate.load_candidate(paths, 'demo')
    assert candidate is not None
    assert candidate.pinned.commit == original
    push_calls: list[tuple[str, ...]] = []
    real_push = _publish.push

    def _record_push(destination: _host.Destination, cwd: Path, args: Iterable[str]) -> GitResult:
        refspecs = tuple(args)
        push_calls.append(refspecs)
        return real_push(destination, cwd, refspecs)

    monkeypatch.setattr(_publish, 'push', _record_push)
    assert _cli.main(['publish', '--yes']) == 0
    published = candidate.commit
    record = _records.load_publication(paths, 'demo', published)
    assert record is not None
    assert record.source_bookmark is not None
    private_record = _records.publication_path(paths, 'demo', published).read_bytes()
    assert _candidate.load_candidate(paths, 'demo') is None
    assert len(push_calls) == 1
    host_calls = _host_calls()
    host_refs = _git_refs(host)
    assert host_refs == {
        'refs/heads/main': published,
        'refs/tags/v1.0.0': published,
    }

    rewritten = _rewrite_and_prune_source(source, original)

    _verify_publication_history(capsys, (paths, source), published, original, rewritten)
    copy = _verify_published_inspection(paths, published)

    assert _cli.main(['delete', 'v1.0.0', '--yes']) == 0
    assert _records.load_publication(paths, 'demo', published) is None
    assert _git_refs(host) == host_refs
    assert _host_calls() == host_calls
    assert len(push_calls) == 1
    assert _cli.main(['show', 'v1.0.0', '--verbose']) == 1
    assert not copy.exists()

    _verify_host_excludes_source(
        host, source, original, record.source_bookmark.refname, private_record
    )
    assert not any('refs/pubanon/' in ref for ref in host_refs)
    assert not any('refs/pubanon/' in arg for call in push_calls for arg in call)


@pytest.mark.slow
def test_real_name_identity_two_releases_audit_and_reveal_work_offline(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    cli_output: list[str] = []

    def _capture_output() -> tuple[str, str]:
        captured = capsys.readouterr()
        cli_output.extend((captured.out, captured.err))
        return captured.out, captured.err

    paths, source, host = _initialize_real_name(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
        _capture_output,
    )
    publication_name = _edit_real_name_identity(
        paths,
        fake_gh,
        monkeypatch,
        _capture_output,
    )
    owner, repo = _REAL_ACCOUNT, 'public-repo'

    subprocess.run(['git', 'init', '--bare', '-q', host], check=True, env=git_env())
    create_publication = [
        'api',
        '-X',
        'POST',
        'user/repos',
        '-f',
        f'name={repo}',
        '-F',
        'private=true',
        '-f',
        f'description={_REAL_SETTINGS.description}',
        '-F',
        'has_issues=true',
        '-F',
        'has_wiki=false',
        '-F',
        'has_projects=false',
        '-F',
        'has_discussions=false',
    ]
    _write_responses(
        fake_gh,
        [
            _account_response(_REAL_ACCOUNT),
            {
                'args': ['api', f'repos/{owner}/{repo}', '--jq', _host._REPOSITORY_QUERY],
                'returncode': 1,
                'stdout': '{"status":"404"}\n',
                'once': True,
            },
            {'args': create_publication},
            *_created_settings_responses(owner, repo, _REAL_SETTINGS),
            _repository_response(owner, repo, 'private'),
        ],
    )
    assert _cli.main(['repo', 'create', '--yes']) == 0
    _capture_output()

    _write_responses(
        fake_gh,
        [
            _account_response(_REAL_ACCOUNT),
            _repository_response(owner, repo, 'private'),
        ],
    )

    def _publish_first(prompt: str) -> str:
        record = _candidate.load_candidate(paths, 'demo')
        assert record is not None
        assert prompt == (f'Publish v0.1.0 to {owner}/{repo}? [y/n] ')
        return 'yes'

    assert _cli.main(['prepare', '--version', '0.1.0', '--yes']) == 0
    assert _cli.main(['inspect']) == 0
    monkeypatch.setattr('builtins.input', _publish_first)
    assert _cli.main(['publish']) == 0
    _capture_output()
    first = _git_refs(host)['refs/tags/v0.1.0']

    _commit(source, 'second release\n')
    _write_responses(
        fake_gh,
        [
            _account_response(_REAL_ACCOUNT),
            _repository_response(owner, repo, 'private'),
        ],
    )
    assert _cli.main(['prepare', '--version', '0.2.0', '--yes']) == 0
    assert _cli.main(['inspect']) == 0
    assert _cli.main(['publish', '--yes']) == 0
    _capture_output()
    second = _git_refs(host)['refs/tags/v0.2.0']

    assert _git_refs(host) == {
        'refs/heads/main': second,
        'refs/tags/v0.1.0': first,
        'refs/tags/v0.2.0': second,
    }
    expected_identity = f'{publication_name} <{_REAL_EMAIL}>'
    for oid, expected_parents in ((first, []), (second, [first])):
        author, committer, parents, signed = _commit_headers(host, oid)
        assert author.startswith(f'author {expected_identity} ')
        assert author.endswith(' +0000')
        assert committer.startswith(f'committer {expected_identity} ')
        assert committer.endswith(' +0000')
        assert parents == expected_parents
        assert signed is False

    assert _cli.main(['audit', '--remote']) == 0
    audit, _ = _capture_output()
    assert 'audit: local copy of published history; verified against GitHub' in audit
    assert 'historical exposure' not in audit
    assert 'identity problem' not in audit

    _write_responses(
        fake_gh,
        [
            _account_response(_REAL_ACCOUNT),
            _repository_response(owner, repo, 'private', once=True),
            _repository_response(owner, repo, 'private', once=True),
            _repository_response(owner, repo, 'public'),
            _settings_response(owner, repo, _REAL_SETTINGS, once=True),
            _settings_response(owner, repo, _REAL_SETTINGS),
            *_disclosure_responses(owner, repo),
            {
                'args': ['api', '-X', 'PATCH', f'repos/{owner}/{repo}', '-f', 'visibility=public'],
            },
        ],
    )
    _reveal_after_disclosure_review(monkeypatch, _capture_output, owner, repo)
    reveal, _ = _capture_output()
    assert f'Made {owner}/{repo} public.' in reveal
    assert _config.load_project(paths, 'demo').visibility == 'public'
    assert _REAL_TERM in ''.join(cli_output)


@pytest.mark.slow
def test_stepwise_publish_path_and_gates_work_offline(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, source, host = _initialize(tmp_path, fake_gh, make_repo, monkeypatch)

    assert _cli.main(['prepare', '--version', '1.0.0', '--yes']) == 0
    first = _candidate.load_candidate(paths, 'demo')
    assert first is not None
    assert _cli.main(['inspect']) == 0
    inspection = paths.data_dir / 'projects' / 'demo' / 'inspections' / first.commit
    assert sorted(path.relative_to(inspection).as_posix() for path in inspection.rglob('*')) == [
        'README.md'
    ]
    assert (inspection / 'README.md').read_text() == 'first release\n'
    assert _host_calls() == []
    assert _cli.main(['publish', '--yes']) == 0
    assert _host_calls() == [_ACCOUNT_CALL, _REPOSITORY_CALL]
    assert _git_refs(host) == {
        'refs/heads/main': first.commit,
        'refs/tags/v1.0.0': first.commit,
    }

    _commit(source, 'second release\n')
    assert _cli.main(['prepare', '--version', '1.1.0', '--yes']) == 0
    second = _candidate.load_candidate(paths, 'demo')
    assert second is not None
    assert second.prestate_main == first.commit
    assert _cli.main(['inspect']) == 0
    assert (inspection / 'README.md').read_text() == 'first release\n'
    second_inspection = paths.data_dir / 'projects' / 'demo' / 'inspections' / second.commit
    assert (second_inspection / 'README.md').read_text() == 'second release\n'
    assert _host_calls() == [_ACCOUNT_CALL, _REPOSITORY_CALL]
    assert _cli.main(['publish', '--yes']) == 0
    assert _host_calls() == [_ACCOUNT_CALL, _REPOSITORY_CALL, _ACCOUNT_CALL, _REPOSITORY_CALL]
    assert _git_refs(host) == {
        'refs/heads/main': second.commit,
        'refs/tags/v1.0.0': first.commit,
        'refs/tags/v1.1.0': second.commit,
    }

    capsys.readouterr()
    assert _cli.main(['status']) == 0
    status = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'Prepared: none' in status
    assert 'Published: v1.1.0 to release-owner/public-repo (private)' in status

    _assert_negative_gates(paths, source, monkeypatch, capsys)
