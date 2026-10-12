import json
import os
import re
import shlex
import subprocess
from collections.abc import Callable, Generator
from contextlib import contextmanager, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from typing import Literal, Never, assert_type, cast

import pytest

from pubanon import (
    _cli,
    _config,
    _filesystem,
    _terminal,
)
from pubanon._config import GlobalConfig, Paths, ProjectConfig, Settings
from pubanon._errors import OperationCanceled, PubanonError
from pubanon._gate import RuleClass
from pubanon._remote import _host, _publish, _releases, _stage, _watch
from pubanon._remote._publish import (
    _acknowledge_exposure,
    _verify_for_publish,
    _verify_remote,
    _verify_sync_prefix,
)
from pubanon._remote._stage import _save_stage
from pubanon._run import GitResult
from pubanon._snapshots import (
    _audit,
    _candidate,
    _discard,
    _history,
    _records,
    _rewrite,
    _source,
    _store,
    _verification,
)
from pubanon._snapshots._source import PinnedSource, Snapshot, pin, read_snapshot
from pubanon._terminal import color_context, escape, verbose_context
from tests._cli import gh_calls
from tests._git import git_env, remove_tree
from tests._permissions import assert_private_mode
from tests.remote.test_releases import _Host as _ReleaseHost
from tests.snapshots.test_rewrite import _install_faults


def _assert_page_completion_report(
    captured: tuple[str, str], outcome: str, host: _ReleaseHost, notes_file: Path
) -> None:
    out, error = captured
    if outcome in {'created', 'existing'}:
        assert host.page is not None
        result = out[out.index('Published v1.2.3') :]
        assert (
            f'Release page{" already exists" if outcome == "existing" else ""}: {host.page.url}'
            in result
        )
        assert result.index(host.page.url) < result.index('Next:')
    else:
        message = (
            'may not have been created'
            if outcome in {'unknown', 'interrupt'}
            else 'was not created'
        )
        assert f'the release page {message}' in error
        assert 'code was published' in error
        assert '--project=example' in error
        assert 'Nothing was published' not in error
        if outcome == 'different':
            # Creating again would refuse the different page, so only inspection or editing helps.
            assert 'release show v1.2.3' in error
            assert 'release edit v1.2.3' in error
            assert 'release create' not in error
        else:
            assert f'--notes-file {shlex.quote(str(notes_file))}' in error
            assert 'release create v1.2.3' in error


def _configs(source: Path) -> tuple[GlobalConfig, ProjectConfig]:
    return (
        GlobalConfig(
            anon_name='Anonymous Publisher',
            anon_email='anonymous@example.invalid',
            accounts={'github': 'release-account'},
            sensitive_terms=('Private Person',),
        ),
        ProjectConfig(
            name='example',
            source_path=source.resolve(),
            host='github',
            owner='public-owner',
            repo='public-repo',
            visibility='public',
            allow=('**',),
            deny=(),
            secret_exceptions=(),
            extra_terms=(),
            hygiene_terms=(),
            self_publication=True,
            required_checks=(),
            publication_route='direct',
            release_page=_config.ReleasePageSettings(enabled=False),
            settings=Settings(
                '',
                'main',
                issues=True,
                wiki=False,
                projects=False,
                pull_requests=False,
                discussions=False,
            ),
        ),
    )


def _new_host(tmp_path: Path) -> Path:
    host = tmp_path / 'host.git'
    subprocess.run(['git', 'init', '--bare', '-q', host], check=True, env=git_env())
    return host


def _page_plan(ctx: _publish.PublishContext) -> _releases.PagePlan:
    return _releases.plan_publication_page(
        ctx._paths, ctx._global_config, ctx._project, ctx._record, ctx._release_page
    )


def _paths(tmp_path: Path) -> Paths:
    config_dir = tmp_path / 'config' / 'pubanon'
    data_dir = tmp_path / 'data' / 'pubanon'
    return Paths(config_dir, data_dir, config_dir / 'config.toml', data_dir / 'registry.json')


def _ci_context(
    tmp_path: Path,
    source: Path,
    *,
    required: tuple[str, ...] = ('hosted-validation',),
    route: Literal['staged', 'direct'] = 'staged',
    ci_workflow: str | None = 'ci.yml',
) -> _publish.PublishContext:
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    project = replace(
        project, ci_workflow=ci_workflow, required_checks=required, publication_route=route
    )
    pinned = pin(source, 'HEAD')
    record = _candidate.build_candidate(
        paths,
        global_config,
        project,
        _candidate.CandidateInputs(read_snapshot(source, pinned.commit), pinned, None, None),
    )
    return _publish.PublishContext(paths, global_config, project, record, _yes=True)


def _configure_ci_evidence(
    ctx: _publish.PublishContext,
    fake_gh: Path,
    *,
    state: str = 'success',
    variable: str | None = 'true',
) -> None:
    prefix = f'repos/{ctx._project.owner}/{_config.stage_repository(ctx._project)}'
    run = {
        'id': 9,
        'run_number': 9,
        'run_attempt': 1,
        'path': '.github/workflows/ci.yml',
        'event': 'workflow_dispatch',
        'head_branch': 'main',
        'head_sha': _records.candidate_tip(ctx._record),
        'status': 'in_progress' if state == 'pending' else 'completed',
        'conclusion': 'failure' if state == 'failure' else 'success',
        'display_title': 'CI / hosted\x1b[31m',
        'html_url': 'https://example.invalid/runs/9\nend',
    }
    runs = [] if state == 'missing' else [run]
    if state == 'success':
        runs.append({**run, 'id': 8, 'run_number': 8, 'conclusion': 'failure'})
    if state == 'ambiguous':
        runs.append({**run, 'id': 10})
    jobs = [
        {
            'run_id': 9,
            'run_attempt': 1,
            'name': name,
            'status': 'completed',
            'conclusion': 'skipped' if state == 'skipped' else 'success',
        }
        for name in ctx._project.required_checks
    ]
    variable_response: dict[str, object] = {
        'args': ['api', 'repos/public-owner/public-repo/actions/variables/CI_RUN_ON_PUSH'],
        'stdout': json.dumps({'name': 'CI_RUN_ON_PUSH', 'value': variable}),
    }
    if variable is None:
        variable_response.update(stdout='', stderr='HTTP 404: Not Found', returncode=1)
    responses = json.loads(fake_gh.read_text())
    responses.extend(
        [
            variable_response,
            {
                'args': [
                    'api',
                    (
                        f'{prefix}/actions/workflows/ci.yml/runs?'
                        f'head_sha={_records.candidate_tip(ctx._record)}'
                        '&event=workflow_dispatch&branch=main&per_page=100&page=1'
                    ),
                ],
                'stdout': json.dumps({'total_count': len(runs), 'workflow_runs': runs}),
            },
            {
                'args': ['api', f'{prefix}/actions/runs/9/attempts/1/jobs?per_page=100&page=1'],
                'stdout': json.dumps({'total_count': len(jobs), 'jobs': jobs}),
            },
        ]
    )
    fake_gh.write_text(json.dumps(responses))


@contextmanager
def _failed_readback(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    real_ls_remote = _host.ls_remote
    calls = 0

    def _fail_after_push(
        dest: _host.Destination,
        paths: Paths,
        project: str,
    ) -> _host.RemoteRefs:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise PubanonError('simulated remote read failure')
        return real_ls_remote(dest, paths, project)

    monkeypatch.setattr(_host, 'ls_remote', _fail_after_push)
    try:
        yield
    finally:
        monkeypatch.setattr(_host, 'ls_remote', real_ls_remote)


def _git_command(
    repo: Path,
    *args: str,
    env: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
) -> bytes:
    return subprocess.run(
        ['git', '-C', repo, *args],
        check=True,
        capture_output=True,
        env=git_env(env),
        input=input_bytes,
    ).stdout


def _literal_history_commit(
    store: Path,
    tree: str,
    *,
    author_name: str = 'Anonymous Publisher',
    message: bytes,
    parent: str | None = None,
) -> str:
    headers = [f'tree {tree}']
    if parent is not None:
        headers.append(f'parent {parent}')
    headers.extend(
        (
            f'author {author_name} <anonymous@example.invalid> 1700000000 +0000',
            'committer Anonymous Publisher <anonymous@example.invalid> 1700000000 +0000',
        )
    )
    return (
        _git_command(
            store,
            'hash-object',
            '-t',
            'commit',
            '-w',
            '--stdin',
            input_bytes='\n'.join(headers).encode() + b'\n\n' + message,
        )
        .decode()
        .strip()
    )


def _context(
    tmp_path: Path,
    source: Path,
    *,
    extra_terms: tuple[str, ...] = (),
    selector: str = 'HEAD',
    version: str | None = None,
) -> _publish.PublishContext:
    paths = _paths(tmp_path)
    global_config, project_config = _configs(source)
    project_config = replace(project_config, extra_terms=extra_terms)

    pinned = pin(source, selector)
    record = _candidate.build_candidate(
        paths,
        global_config,
        project_config,
        _candidate.CandidateInputs(
            read_snapshot(source, pinned.commit),
            pinned,
            version,
            None,
        ),
    )
    return _publish.PublishContext(
        paths,
        global_config,
        project_config,
        record,
        _yes=True,
    )


def _description_context(
    tmp_path: Path, source: Path, description: str
) -> _publish.PublishContext:
    ctx = _context(tmp_path, source)
    assert ctx._project.settings is not None
    settings = replace(ctx._project.settings, description=description)
    return replace(ctx, _project=replace(ctx._project, settings=settings))


def _literal_context(
    tmp_path: Path, *, extra_terms: tuple[str, ...] = (), version: str | None = None
) -> _publish.PublishContext:
    paths = _paths(tmp_path)
    global_config, project = _configs(tmp_path / 'source')
    project = replace(project, extra_terms=extra_terms)
    record = _records.CandidateRecord(
        commit='5' * 40,
        tree='b' * 40,
        snapshot_digest='c' * 64,
        tag_intent=f'v{version}' if version is not None else None,
        fingerprint=_config.fingerprint(global_config, project),
        pinned=PinnedSource('d' * 40, 'ref', 'refs/heads/main', 'd' * 40),
        destination=_records.RecordedDestination(
            project.host,
            project.owner,
            project.repo,
            project.visibility,
            global_config.accounts[project.host],
        ),
        prestate_main=None,
        prestate_tags={},
        state=_candidate.STATE_RETAINED,
        message='Public snapshot',
    )
    return _publish.PublishContext(paths, global_config, project, record, _yes=True)


def _publish_mode(
    ctx: _publish.PublishContext, mode: Literal['interactive', 'unattended']
) -> _publish.PublishContext:
    unattended = mode == 'unattended'
    return replace(ctx, _yes=unattended)


def _record_path(ctx: _publish.PublishContext) -> Path:
    return ctx._paths.data_dir / 'projects' / ctx._project.name / 'candidate.toml'


def _remote_ref(host: Path, ref: str) -> str | None:
    output = _git_command(host, 'for-each-ref', '--format=%(objectname)', ref)
    return output.decode().strip() or None


def _assert_offline_replacement_history(
    ctx: _publish.PublishContext, host: Path, originals: list[_records.CandidateRecord]
) -> None:
    paths, project = ctx._paths, ctx._project
    source = project.source_path
    old_b = originals[1]
    b2, c2 = ctx._record.replayed
    expected_tags = {'v1.0.0': ctx._record.commit, 'v1.0.1': b2.new, 'v1.0.2': c2.new}
    assert _candidate.published_refs(paths, project.name) == (c2.new, expected_tags)
    assert _remote_ref(host, 'refs/heads/main') == c2.new
    assert _git_command(host, 'rev-list', '--reverse', 'main').decode().splitlines() == [
        ctx._record.commit,
        b2.new,
        c2.new,
    ]
    assert _git_command(
        host, 'for-each-ref', '--format=%(refname:strip=2)', 'refs/tags'
    ).decode().splitlines() == sorted(expected_tags)
    for tag, commit in expected_tags.items():
        assert _remote_ref(host, f'refs/tags/{tag}') == commit
    assert _git_command(host, 'show', 'v1.0.0:README.md') == b'corrected\n'
    assert _git_command(host, 'show', 'v1.0.1:README.md') == b'corrected and later edit\n'
    assert _git_command(host, 'ls-tree', '--name-only', 'v1.0.1').splitlines() == [b'README.md']
    assert _git_command(
        host, 'ls-tree', '-r', '--format=%(objectmode) %(objecttype) %(path)', 'main'
    ).splitlines() == [b'100644 blob README.md', b'100644 blob third.txt']
    assert _git_command(host, 'show', 'main:README.md') == b'corrected and later edit\n'
    assert _git_command(host, 'show', 'main:third.txt') == b'third release\n'

    entries = {entry.commit: entry for entry in _history.publications(paths, project.name)}
    assert all(entries[old.commit].replaced for old in originals)
    assert all(not entries[commit].replaced for commit in expected_tags.values())
    assert _history.select_publication(paths, project.name, 'v1.0.1').commit == b2.new
    old_entry = _history.select_publication(paths, project.name, old_b.commit)
    old_copy = _history.inspect_publication(paths, project.name, old_entry)
    current_copy = _history.inspect_publication(paths, project.name, entries[b2.new])
    assert (old_copy / 'README.md').read_text() == 'later edit\n'
    assert (current_copy / 'README.md').read_text() == 'corrected and later edit\n'
    derived = entries[b2.new].record
    assert derived is not None
    assert derived.pinned is None
    assert derived.derivation == _records.Derivation(
        old_b.commit, old_b.pinned.commit, ctx._record.commit, b2.resolutions
    )
    assert old_entry.record is not None
    old_bookmark = old_entry.record.source_bookmark
    new_bookmark = derived.source_bookmark
    assert old_bookmark is not None
    assert new_bookmark is not None
    assert old_bookmark.refname != new_bookmark.refname
    _history.delete_publication(paths, project, old_entry)
    assert _records.load_publication(paths, project.name, old_b.commit) is None
    assert _remote_ref(source, old_bookmark.refname) is None
    assert _remote_ref(source, new_bookmark.refname) == old_b.pinned.commit
    assert _records.load_publication(paths, project.name, b2.new) == derived
    assert not old_copy.exists()
    assert current_copy.exists()
    assert _remote_ref(host, 'refs/heads/main') == c2.new


def _assert_provisional_bookmark(ctx: _publish.PublishContext) -> None:
    retained = _records.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    bookmark = retained.source_bookmark
    assert bookmark is not None
    assert retained == replace(ctx._record, source_bookmark=bookmark)
    assert _remote_ref(ctx._project.source_path, bookmark.refname) == ctx._record.pinned.commit
    assert _records.load_publication(ctx._paths, ctx._project.name, ctx._record.commit) is None


def _resume_offline_conflict(
    ctx: _publish.PublishContext, old_b: str, monkeypatch: pytest.MonkeyPatch
) -> _records.CandidateRecord:
    paths, g, project = ctx._paths, ctx._global_config, ctx._project
    with monkeypatch.context() as fault:
        _install_faults(fault, 'partial-export')
        with pytest.raises(RuntimeError, match='interrupted'):
            _rewrite.continue_rewrite(paths, g, project, env={})
    progress = _records.load_rewrite(paths, project.name)
    assert progress is not None
    assert progress.phase == 'exporting'
    assert progress.pause is not None
    assert progress.pause.old == old_b
    directory = _records.rewrite_directory(paths, project.name)
    assert (directory / 'partial').exists()

    paused = _rewrite.continue_rewrite(paths, g, project, env={})
    assert paused.tip is None
    progress = _records.load_rewrite(paths, project.name)
    assert progress is not None
    assert progress.phase == 'awaiting'
    assert not (directory / 'partial').exists()
    manifest = directory / 'resolutions.toml'
    assert 'action = "unresolved"' in manifest.read_text()
    with pytest.raises(PubanonError, match='unresolved'):
        _rewrite.continue_rewrite(paths, g, project, env={})
    (directory / 'tree' / 'README.md').write_bytes(b'corrected and later edit\n')
    manifest.write_text(manifest.read_text().replace('action = "unresolved"', 'action = "keep"'))
    completed = _rewrite.continue_rewrite(paths, g, project, env={})
    assert completed.commit == ctx._record.commit
    assert len(completed.replayed) == 2
    b2, c2 = completed.replayed
    assert b2.resolutions == (_records.Resolution(('README.md',), 'keep'),)
    assert c2.resolutions == ()
    assert completed.tip == c2.new
    assert _records.load_rewrite(paths, project.name) is None
    assert not directory.exists()
    return completed


def _with_source_tags(ctx: _publish.PublishContext) -> _publish.PublishContext:
    return replace(ctx, _project=replace(ctx._project, source_tags=True))


@pytest.fixture
def page_context(
    tmp_path: Path, make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> tuple[_publish.PublishContext, _ReleaseHost]:
    ctx = _context(tmp_path, make_repo(), version='1.2.3')
    ctx = replace(
        ctx,
        _project=replace(ctx._project, release_page=_config.ReleasePageSettings(enabled=True)),
    )
    host = _ReleaseHost(_publish._destination_from_record(ctx._record), ctx._record.commit)
    monkeypatch.setattr(_host, 'all_releases', host._all)
    monkeypatch.setattr(_host, 'create_release', host._create)
    return ctx, host


def _second_source_commit(source: Path, content: str = 'second\n') -> None:
    (source / 'README.md').write_text(content, encoding='utf-8', newline='\n')
    _git_command(source, 'add', 'README.md')
    _git_command(
        source,
        '-c',
        'user.name=Test User',
        '-c',
        'user.email=test@example.invalid',
        'commit',
        '-qm',
        'second',
    )


def _sync_context(
    ctx: _publish.PublishContext,
    *,
    mode: Literal['interactive', 'unattended'] = 'unattended',
) -> _publish.SyncContext:
    unattended = mode == 'unattended'
    context = _publish.SyncContext(ctx._paths, ctx._global_config, ctx._project, unattended)
    assert_type(context, _publish.SyncContext)
    return context


def _write_host_responses(path: Path, visibility: str = 'public') -> None:
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
                        'repos/public-owner/public-repo',
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
                        'repos/public-owner/public-repo',
                        '--jq',
                        _host._REPOSITORY_QUERY,
                    ],
                    'stdout': json.dumps(
                        {
                            'owner': 'public-owner',
                            'repo': 'public-repo',
                            'visibility': visibility,
                        }
                    ),
                },
            ]
        )
        + '\n'
    )


def _configure_host(
    host: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_host_responses(fake_gh)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', str(host))


def _configure_staged_candidate(
    ctx: _publish.PublishContext,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    commit: str | None = None,
    failure: str = '',
) -> Path:
    stage_host = fake_gh.parent / 'staging.git'
    subprocess.run(['git', 'init', '--bare', '-q', stage_host], check=True, env=git_env())
    commit = commit or _records.candidate_tip(ctx._record)
    _git_command(
        stage_host,
        'fetch',
        str(_store.store_path(ctx._paths, ctx._project.name)),
        f'{commit}:refs/heads/main',
    )
    repo = _config.stage_repository(ctx._project)
    _save_stage(
        ctx._paths, ctx._project.name, _stage.StageRecord(commit, ctx._project.owner, repo)
    )
    repository_url = _host.repository_url

    def _repository_url(owner: str, name: str) -> str:
        return str(stage_host) if name == repo else repository_url(owner, name)

    monkeypatch.setattr(_host, 'repository_url', _repository_url)
    responses = json.loads(fake_gh.read_text())
    responses.extend(
        [
            {
                'args': [
                    'api',
                    f'repos/{ctx._project.owner}/{repo}',
                    '--jq',
                    _host._REPOSITORY_QUERY,
                ],
                'stdout': json.dumps(
                    {
                        'owner': ctx._project.owner,
                        'repo': repo,
                        'visibility': 'public' if failure == 'public' else 'private',
                    }
                ),
            },
            {
                'args': [
                    'api',
                    f'repos/{ctx._project.owner}/{repo}',
                    '--jq',
                    _host._REPOSITORY_SETTINGS_QUERY,
                ],
                'stdout': json.dumps(
                    {
                        'description': '',
                        'default_branch': 'other' if failure == 'default-branch' else 'main',
                        'issues': False,
                        'wiki': False,
                        'projects': False,
                        'pull_requests': False,
                        'discussions': False,
                    }
                ),
            },
        ]
    )
    fake_gh.write_text(json.dumps(responses))
    return stage_host


def _context_after_first_publish(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    history: tuple[dict[str, str], tuple[str, ...]],
) -> tuple[Path, _publish.PublishContext]:
    first_files, extra_terms = history
    source = make_repo(files=first_files)
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    _publish.publish(_context(tmp_path, source))
    for path in first_files:
        if path != 'README.md':
            _git_command(source, 'rm', '-q', '--', path)
    _second_source_commit(source, 'public successor\n')
    ctx = _context(
        tmp_path,
        source,
        extra_terms=extra_terms,
    )
    _config.save_project(ctx._paths, ctx._project)
    return host, ctx


def _drifted_context(
    drift: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> _publish.PublishContext:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    if drift in {'rewound', 'diverged'}:
        _second_source_commit(source)
    ctx = _context(tmp_path, source)
    selector = ctx._record.pinned.selector_name
    assert selector is not None
    if drift == 'advanced':
        _second_source_commit(source)
    elif drift == 'rewound':
        _git_command(source, 'update-ref', selector, f'{ctx._record.pinned.commit}^')
    elif drift == 'diverged':
        _git_command(source, 'checkout', '-q', '--detach', f'{ctx._record.pinned.commit}^')
        _second_source_commit(source, 'diverged\n')
        _git_command(source, 'update-ref', selector, 'HEAD')
    else:
        _git_command(source, 'update-ref', '-d', selector)
    return ctx


@pytest.fixture(name='publication_cli')
def _publication_cli(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, _publish.PublishContext]:
    source = make_repo(files={'README.md': 'public\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    visibility, version = cast(
        'tuple[str, str | None]', getattr(request, 'param', ('public', '1.0.0'))
    )
    _write_host_responses(fake_gh, visibility)
    ctx = _context(tmp_path, source, version=version)
    project = replace(ctx._project, visibility=visibility)
    record = replace(
        ctx._record,
        destination=replace(ctx._record.destination, visibility=visibility),
        fingerprint=_config.fingerprint(ctx._global_config, project),
    )
    _candidate.save_candidate(ctx._paths, project.name, record)
    ctx = replace(ctx, _project=project, _record=record)
    _config.save_global(ctx._paths, ctx._global_config, None)
    _config.save_project(ctx._paths, replace(ctx._project, workflows={'ship': ('publish',)}))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(ctx._paths.config_dir.parent))
    monkeypatch.setenv('XDG_DATA_HOME', str(ctx._paths.data_dir.parent))
    return host, ctx


def _published_history(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    first_content: str = 'first\n',
) -> tuple[Path, Path, _publish.PublishContext, _publish.PublishContext]:
    source = make_repo(files={'README.md': first_content})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    first = _context(tmp_path, source, version='1.0.0')
    _publish.publish(first)
    _second_source_commit(source)
    second = _context(tmp_path, source, version='2.0.0')
    _publish.publish(second)
    return source, host, first, second


def _replacement_fixture(
    tmp_path: Path,
    source: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str | None,
) -> tuple[Path, _candidate.CandidateRecord, _publish.PublishContext]:
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    project = replace(project, visibility='private')
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    _write_host_responses(fake_gh, 'private')
    pinned = pin(source, 'HEAD')
    first = _candidate.build_candidate(
        paths,
        global_config,
        project,
        _candidate.CandidateInputs(read_snapshot(source, pinned.commit), pinned, version, None),
    )
    _publish.publish(
        _publish.PublishContext(
            paths,
            global_config,
            project,
            first,
            _yes=True,
        )
    )
    _second_source_commit(source, 'corrected publication\n')
    pinned = pin(source, 'HEAD')
    record = _candidate.build_candidate(
        paths,
        global_config,
        project,
        _candidate.CandidateInputs(
            read_snapshot(source, pinned.commit),
            pinned,
            version,
            None,
            replaces=first.commit,
            replace_scope='one',
        ),
    )
    return (
        host,
        first,
        _publish.PublishContext(
            paths,
            global_config,
            project,
            record,
            _yes=True,
        ),
    )


def _rewrite_fixture(
    tmp_path: Path,
    source: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    selection: int | None = 1,
) -> tuple[Path, _publish.PublishContext, tuple[_records.CandidateRecord, ...]]:
    paths = _paths(tmp_path)
    g, project = _configs(source)
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    originals: list[_records.CandidateRecord] = []
    store = _store.store_path(paths, project.name)
    for index in range(3):
        if index:
            (source / f'addition-{index}.txt').write_bytes(f'addition {index}\n'.encode())
            _git_command(source, 'add', '.')
            _git_command(source, 'commit', '-qm', f'addition {index}')
        pinned = pin(source, 'HEAD')
        record = _candidate.build_candidate(
            paths,
            g,
            project,
            _candidate.CandidateInputs(
                read_snapshot(source, pinned.commit), pinned, f'1.0.{index}', None
            ),
        )
        _bookmark = _source.describe_bookmark(source, project.name, record.commit)
        record = replace(record, source_bookmark=_bookmark)
        _source.ensure_bookmark(_bookmark, pinned.commit)
        _history.record_publication(paths, project, record)
        store = _store.store_path(paths, project.name)
        _git_command(store, 'update-ref', 'refs/heads/main', record.commit)
        _git_command(store, 'update-ref', f'refs/tags/v1.0.{index}', record.commit)
        _records.candidate_path(paths, project.name).unlink()
        originals.append(record)
    _git_command(
        host, 'fetch', str(store), 'refs/heads/main:refs/heads/main', 'refs/tags/*:refs/tags/*'
    )
    all_history = selection is None
    selected = originals[-1] if selection is None else originals[selection]
    _git_command(source, 'checkout', '--detach', selected.pinned.commit)
    _second_source_commit(source, 'corrected\n')
    pinned = pin(source, 'HEAD')
    record = _candidate.build_candidate(
        paths,
        g,
        project,
        _candidate.CandidateInputs(
            read_snapshot(source, pinned.commit),
            pinned,
            '1.0.2' if all_history else None,
            None,
            replaces=selected.commit,
            replace_scope='all' if all_history else 'one',
        ),
    )
    if record.tip is None:
        record = _rewrite.continue_rewrite(paths, g, project, env={})
    return (
        host,
        _publish.PublishContext(paths, g, project, record, _yes=True),
        tuple(originals),
    )


@pytest.fixture
def advanced_source_context(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> _publish.PublishContext:
    return _publish_mode(
        _drifted_context('advanced', tmp_path, fake_gh, make_repo, monkeypatch),
        'interactive',
    )


def test_acknowledged_digest_skips_matching_findings_and_changes_for_new_exposure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = 'first-private-canary'
    second = 'second-private-canary'
    ctx = _literal_context(tmp_path, extra_terms=(first, second))
    _config.save_project(ctx._paths, ctx._project)
    findings = (
        _audit.AuditFinding('path:first.txt', RuleClass.IDENTITY_CRITICAL, first),
        _audit.AuditFinding('path:style.txt', RuleClass.HYGIENE, 'style-canary'),
    )

    prompts = ['Continue despite this earlier exposure? [y/n] ']

    def _accept(prompt: str) -> str:
        assert prompts
        assert prompts.pop(0) == prompt
        return 'y'

    monkeypatch.setattr('builtins.input', _accept)
    acknowledged = _acknowledge_exposure(
        ctx._paths,
        (ctx._global_config, ctx._project),
        _audit.HistoryAudit(
            findings,
            (),
            tuple(_audit.HistoryEvidence(item, None, item.unit, item.unit) for item in findings),
        ),
        ((), {}),
        unattended=False,
    )

    assert not prompts

    def _unexpected_prompt(_prompt: str) -> str:
        pytest.fail('matching exposure digest prompted again')

    monkeypatch.setattr('builtins.input', _unexpected_prompt)
    assert (
        _acknowledge_exposure(
            ctx._paths,
            (ctx._global_config, acknowledged),
            _audit.HistoryAudit(
                findings,
                (),
                tuple(
                    _audit.HistoryEvidence(item, None, item.unit, item.unit) for item in findings
                ),
            ),
            ((), {}),
            unattended=False,
        )
        == acknowledged
    )
    changed = (
        *findings,
        _audit.AuditFinding('path:second.txt', RuleClass.IDENTITY_CRITICAL, second),
    )

    prompts = ['Continue despite this earlier exposure? [y/n] ']

    def _decline(prompt: str) -> str:
        assert prompts
        assert prompts.pop(0) == prompt
        return 'n'

    monkeypatch.setattr('builtins.input', _decline)
    with pytest.raises(OperationCanceled, match=r'Canceled\. Nothing was published.'):
        _acknowledge_exposure(
            ctx._paths,
            (ctx._global_config, acknowledged),
            _audit.HistoryAudit(
                changed,
                (),
                tuple(
                    _audit.HistoryEvidence(item, None, item.unit, item.unit) for item in changed
                ),
            ),
            ((), {}),
            unattended=False,
        )

    assert not prompts


def test_changed_source_tag_is_named_without_claiming_branch_movement(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    _git_command(source, 'tag', 'release')
    ctx = _publish_mode(_context(tmp_path, source, selector='refs/tags/release'), 'interactive')
    _second_source_commit(source)
    _git_command(source, 'tag', '-f', 'release')

    captured = StringIO()
    with redirect_stdout(captured):
        _verify_for_publish(ctx)
    assert captured.getvalue() == 'Warning: source has changed since snapshot was prepared.\n'
    assert _records.load_candidate(ctx._paths, ctx._project.name) == ctx._record


@pytest.mark.slow
def test_changed_source_uses_one_publication_confirmation(
    advanced_source_context: _publish.PublishContext,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = advanced_source_context
    prompts: list[str] = []

    def _decline(prompt: str) -> str:
        prompts.append(prompt)
        return 'n'

    monkeypatch.setattr('builtins.input', _decline)
    with pytest.raises(OperationCanceled, match=r'Canceled\. Nothing was published.'):
        _publish.publish(ctx)

    assert len(prompts) == 1
    assert prompts[0].startswith('Publish untagged snapshot to ')
    assert capsys.readouterr().out.startswith(
        'Warning: source has changed since snapshot was prepared.\n'
    )
    destination = _host.verify_destination(ctx._paths, ctx._global_config, ctx._project)
    assert _host.ls_remote(destination, ctx._paths, ctx._project.name) == _host.RemoteRefs(
        None, {}
    )
    _assert_provisional_bookmark(ctx)


def test_clean_push_rejection_restores_retained_state(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)

    def _reject(_dest: _host.Destination, _cwd: Path, _args: list[str]) -> GitResult:
        raise PubanonError('credential-bearing command failed')

    monkeypatch.setattr(_host, 'push', _reject)

    with pytest.raises(PubanonError, match='credential-bearing command failed'):
        _publish.publish(ctx)

    retained = _records.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert retained.state == _candidate.STATE_RETAINED


@pytest.mark.parametrize('presentation', [(False, 'never'), (True, 'always')])
def test_exposure_review_shows_rule_and_location_before_acknowledgment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    presentation: tuple[bool, str],
) -> None:
    accept, color = presentation
    term = 'private-canary'
    ctx = _literal_context(tmp_path, extra_terms=(term,))
    _config.save_project(ctx._paths, ctx._project)
    finding = _audit.AuditFinding('path:private-canary.txt', RuleClass.IDENTITY_CRITICAL, term)
    audit = _audit.HistoryAudit(
        (finding,),
        (),
        (
            _audit.HistoryEvidence(
                finding,
                'a' * 40,
                'blob bytes 4:18',
                'private-canary.txt:2',
                'before private-canary after',
                (7, 21),
            ),
        ),
    )
    prompts = [('Continue despite this earlier exposure? [y/n] ', 'y' if accept else 'n')]

    def _answer(prompt: str) -> str:
        assert prompts, f'unexpected prompt: {prompt}'
        expected, response = prompts.pop(0)
        assert expected == re.sub(r'\x1b\[[0-9;]*m', '', prompt)
        output = capsys.readouterr().out
        plain = re.sub(r'\x1b\[[0-9;]*m', '', output)
        assert plain.count('Warning: historical exposure') == 1
        assert 'Published snapshot: v1.0.0 (' + 'a' * 12 + ')' in plain
        assert '  private-canary.txt:2\n    Rule: project extra_terms, item 1\n' in plain
        assert '    Text: before private-canary after\n' in plain
        assert plain.index('Rule:') < plain.index('Text:')
        if color == 'always':
            assert 'Text: before \x1b[33mprivate-canary\x1b[0m after' in output
        else:
            assert '\x1b' not in output
            assert '    Matched: private-canary\n' in output
        assert 'a' * 40 not in output
        assert 'does not remove published content' in output
        assert 'new snapshot content' in output
        return response

    monkeypatch.setattr('builtins.input', _answer)
    with color_context(color):
        if accept:
            result = _acknowledge_exposure(
                ctx._paths,
                (ctx._global_config, ctx._project),
                audit,
                (('a' * 40,), {'v1.0.0': 'a' * 40}),
                unattended=False,
            )
            assert result.acknowledged_exposure == _audit.exposure_digest(audit.findings)
        else:
            with pytest.raises(OperationCanceled, match=r'Canceled\. Nothing was published.'):
                _acknowledge_exposure(
                    ctx._paths,
                    (ctx._global_config, ctx._project),
                    audit,
                    (('a' * 40,), {'v1.0.0': 'a' * 40}),
                    unattended=False,
                )
    assert not prompts
    saved = _config.load_project(ctx._paths, ctx._project.name)
    assert bool(saved.acknowledged_exposure) is accept


def test_indeterminate_diagnostic_names_both_escapes(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    unknown = replace(ctx._record, state=_candidate.STATE_UNKNOWN_OUTCOME)
    _candidate.save_candidate(ctx._paths, ctx._project.name, unknown)
    _git_command(host, 'fetch', str(source), 'HEAD:refs/heads/main')

    with pytest.raises(PubanonError) as excinfo:
        _publish.resolve(replace(ctx, _record=unknown))

    rendered = excinfo.value.render()
    assert 'publish --resolve' in rendered
    assert 'discard --force' in rendered
    assert 'abandons' in rendered


@pytest.mark.slow
def test_indeterminate_diagnostic_separates_managed_refs_from_unrelated_drift(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    first = _context(tmp_path, source, version='1.0.0')
    _publish.publish(first)
    _second_source_commit(source)
    ctx = _context(tmp_path, source, version='2.0.0')
    unknown = replace(ctx._record, state=_candidate.STATE_UNKNOWN_OUTCOME)
    _candidate.save_candidate(ctx._paths, ctx._project.name, unknown)
    source_commit = _git_command(source, 'rev-parse', 'HEAD').decode().strip()
    _git_command(
        host,
        'fetch',
        str(source),
        '+HEAD:refs/heads/main',
        'HEAD:refs/heads/private-backup',
    )

    with pytest.raises(PubanonError, match='indeterminate') as excinfo:
        _publish.resolve(replace(ctx, _record=unknown))

    expected = excinfo.value.expected
    assert expected is not None
    assert first._record.commit in expected
    assert unknown.commit in expected
    assert 'v2.0.0' in expected
    assert 'private-backup' not in expected
    actual = excinfo.value.actual
    assert actual is not None
    assert f'main={source_commit}' in actual
    assert 'private-backup' not in actual
    assert 'v1.0.0' not in actual
    assert 'v2.0.0' not in actual
    fact = excinfo.value.fact
    assert fact is not None
    assert 'unrelated remote drift' in fact
    assert f'private-backup={source_commit}' in fact
    assert 'main=' not in fact
    assert _records.load_candidate(ctx._paths, ctx._project.name) == unknown


@pytest.mark.slow
@pytest.mark.parametrize(
    'case',
    [
        (command, boundary)
        for command in ('publish', 'run')
        for boundary in ('refs', 'record', 'archive', 'publication-record')
    ],
)
@pytest.mark.parametrize('interruption', ['none', 'raw', 'structured'])
def test_landed_publication_finalization_failure_is_resolvable(
    publication_cli: tuple[Path, _publish.PublishContext],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[str, str],
    interruption: str,
) -> None:
    host, ctx = publication_cli
    command, boundary = case
    store = _store.store_path(ctx._paths, ctx._project.name)
    inspection = _verification.materialize_inspect(
        ctx._paths,
        ctx._project.name,
        _verification.prove_objects(ctx._paths, ctx._project.name, ctx._record),
    )
    reviewed = (inspection / 'README.md').read_bytes()
    real_git = _publish.run_git
    real_unlink = Path.unlink

    def _cancel() -> None:
        if interruption == 'raw':
            raise KeyboardInterrupt
        if interruption == 'structured':
            raise OperationCanceled('operation cancelled', fact='local write interrupted')

    def _fail_refs(
        args: list[str],
        *,
        cwd: Path,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> GitResult:
        if args[2:] == ['update-ref', '--no-deref', '--stdin']:
            _cancel()
            return GitResult(7, b'', b'diagnostic\x1b\\path')
        return real_git(args, cwd=cwd, input_bytes=input_bytes, check=check)

    def _fail_record(path: Path, missing_ok: bool = False) -> None:
        if path == _record_path(ctx):
            _cancel()
            raise OSError('simulated record removal failure')
        real_unlink(path, missing_ok=missing_ok)

    def _fail_preservation(*_args: object, **_kwargs: object) -> None:
        _cancel()
        raise OSError('preservation write interrupted')

    args = (['publish'] if command == 'publish' else ['run', 'ship', '--version', '1.0.0']) + [
        '--project',
        ctx._project.name,
        '--yes',
        '--color',
        'always' if interruption == 'structured' else 'auto',
    ]
    with monkeypatch.context() as fault:
        target, name, replacement = {
            'refs': (_publish, 'run_git', _fail_refs),
            'record': (Path, 'unlink', _fail_record),
            'archive': (_history, 'run_git', _fail_preservation),
            'publication-record': (_records, 'save_publication', _fail_preservation),
        }[boundary]
        fault.setattr(target, name, replacement)
        assert _cli.main(args) == (1 if interruption == 'none' else 130)

    error = capsys.readouterr().err
    assert f'Published and verified commit {ctx._record.commit}' in error
    assert 'Local finalization is incomplete' in error
    assert error.startswith(
        ('\x1b[1;31m' if interruption == 'structured' else '')
        + 'error: Local finalization is incomplete'
    )
    assert 'Canceled.' not in error
    assert 'operation cancelled' not in error
    assert (
        boundary != 'refs'
        or interruption != 'none'
        or r'exit status 7: diagnostic\x1b\\path' in error
    )
    assert 'Do not publish a new snapshot' in error
    assert f'pubanon publish --project={ctx._project.name} --resolve' in error
    assert 'Traceback' not in error
    retained = _records.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert retained.state == _candidate.STATE_UNKNOWN_OUTCOME
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit
    assert _remote_ref(host, 'refs/tags/v1.0.0') == ctx._record.commit
    assert _remote_ref(store, _candidate.CANDIDATE_REF) == (
        None if boundary == 'record' else ctx._record.commit
    )
    assert _cli.main(['publish', '--project', ctx._project.name, '--resolve']) == 0
    for ref in ('refs/heads/main', 'refs/tags/v1.0.0'):
        assert _remote_ref(store, ref) == _remote_ref(host, ref) == ctx._record.commit
    assert _records.load_candidate(ctx._paths, ctx._project.name) is None
    assert _remote_ref(store, _candidate.CANDIDATE_REF) is None
    assert (inspection / 'README.md').read_bytes() == reviewed


@pytest.mark.slow
@pytest.mark.parametrize('tracking', ['absent', 'stale', 'malformed'])
def test_direct_publication_ignores_incidental_staging_tracking(
    tracking: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    source_refs = _git_command(source, 'show-ref', '--heads', '--tags')
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    stage_path = ctx._paths.data_dir / 'projects' / ctx._project.name / 'stage.toml'
    if tracking == 'stale':
        _save_stage(ctx._paths, ctx._project.name, _stage.StageRecord('a' * 40, 'other', 'repo'))
    elif tracking == 'malformed':
        stage_path.write_text('malformed staging record')
    original = stage_path.read_bytes() if stage_path.exists() else None

    def _unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail('direct publication read staging evidence')

    monkeypatch.setattr(_stage, 'load_stage', _unexpected)
    monkeypatch.setattr(_watch, 'ci_verdicts', _unexpected)
    _publish.publish(ctx)
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit
    assert _git_command(source, 'show-ref', '--heads', '--tags') == source_refs
    assert (stage_path.read_bytes() if stage_path.exists() else None) == original
    assert not any(
        'stage' in ' '.join(args) or '/check-runs?' in ' '.join(args)
        for args in gh_calls(tmp_path)
    )


def test_landed_push_with_unrelated_tag_records_published(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    term = 'private-tag-name'
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source, extra_terms=(term,))
    real_push = _host.push

    def _push_then_drift(
        dest: _host.Destination,
        cwd: Path,
        args: list[str],
    ) -> GitResult:
        result = real_push(dest, cwd, args)
        _git_command(host, 'update-ref', f'refs/tags/{term}', ctx._record.commit)
        return result

    monkeypatch.setattr(_host, 'push', _push_then_drift)

    with pytest.raises(PubanonError, match='snapshot landed') as error:
        _publish.publish(ctx)

    assert term in error.value.render()
    assert _records.load_candidate(ctx._paths, ctx._project.name) is None
    store = _store.store_path(ctx._paths, ctx._project.name)
    assert _remote_ref(store, 'refs/heads/main') == ctx._record.commit


def test_landed_resolution_failure_reports_current_private_term(
    publication_cli: tuple[Path, _publish.PublishContext],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    host, ctx = publication_cli
    ctx = _publish._preserve_source(ctx)
    store = _store.store_path(ctx._paths, ctx._project.name)
    _git_command(
        host,
        'fetch',
        str(store),
        f'{ctx._record.commit}:refs/heads/main',
        f'{ctx._record.commit}:refs/tags/v1.0.0',
    )
    _candidate.save_candidate(
        ctx._paths, ctx._project.name, replace(ctx._record, state=_candidate.STATE_UNKNOWN_OUTCOME)
    )
    term = ctx._record.commit[:12]
    _config.save_global(
        ctx._paths,
        replace(ctx._global_config, sensitive_terms=(*ctx._global_config.sensitive_terms, term)),
        ctx._paths.global_config.read_bytes(),
    )

    def _fail_finalization(*_args: object) -> None:
        raise PubanonError('cannot record published refs locally')

    with monkeypatch.context() as fault:
        fault.setattr(_publish, '_finalize', _fail_finalization)
        assert _cli.main(['publish', '--project', ctx._project.name, '--resolve']) == 1
    error = capsys.readouterr().err
    assert term in error
    assert f'Published and verified commit {term}' in error
    assert f'pubanon publish --project={ctx._project.name} --resolve' in error
    assert _cli.main(['publish', '--project', ctx._project.name, '--resolve']) == 0
    assert _candidate.published_refs(ctx._paths, ctx._project.name) == (
        ctx._record.commit,
        {'v1.0.0': ctx._record.commit},
    )
    assert _records.load_candidate(ctx._paths, ctx._project.name) is None


def test_mixed_managed_state_is_indeterminate(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source, version='1.0.0')
    real_push = _host.push

    def _push_then_delete_tag(
        dest: _host.Destination,
        cwd: Path,
        args: list[str],
    ) -> GitResult:
        result = real_push(dest, cwd, args)
        _git_command(host, 'update-ref', '-d', 'refs/tags/v1.0.0')
        return result

    monkeypatch.setattr(_host, 'push', _push_then_delete_tag)

    with pytest.raises(PubanonError, match='indeterminate'):
        _publish.publish(ctx)

    record = _records.load_candidate(ctx._paths, ctx._project.name)
    assert record is not None
    assert record.state == _candidate.STATE_UNKNOWN_OUTCOME


@pytest.mark.slow
@pytest.mark.parametrize('selector', ['latest', 'all'])
def test_offline_replacement_latest_and_all_exact_host_state(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    selector: str,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    host, ctx, originals = _rewrite_fixture(
        tmp_path, source, fake_gh, monkeypatch, selection=None if selector == 'all' else 2
    )
    selected, scope = _rewrite.resolve_replacement(ctx._paths, ctx._project.name, selector)
    assert selected == originals[-1].commit
    assert scope == ('all' if selector == 'all' else 'one')
    assert ctx._record.replayed == ()
    assert ctx._record.tip == ctx._record.commit
    _publish.publish(ctx)
    expected_tags = {'v1.0.2': ctx._record.commit}
    expected_chain = [ctx._record.commit]
    if selector == 'latest':
        expected_tags.update({'v1.0.0': originals[0].commit, 'v1.0.1': originals[1].commit})
        expected_chain = [originals[0].commit, originals[1].commit, ctx._record.commit]
    assert (
        _git_command(host, 'rev-list', '--reverse', 'main').decode().splitlines() == expected_chain
    )
    assert _git_command(
        host, 'for-each-ref', '--format=%(refname:strip=2)', 'refs/tags'
    ).decode().splitlines() == sorted(expected_tags)
    for tag, commit in expected_tags.items():
        assert _remote_ref(host, f'refs/tags/{tag}') == commit
    assert _git_command(host, 'show', 'main:README.md') == b'corrected\n'
    assert _git_command(host, 'show', 'main:addition-1.txt') == b'addition 1\n'
    assert _git_command(host, 'show', 'main:addition-2.txt') == b'addition 2\n'
    assert _candidate.published_refs(ctx._paths, ctx._project.name) == (
        ctx._record.commit,
        expected_tags,
    )
    saved = _records.load_publication(ctx._paths, ctx._project.name, ctx._record.commit)
    assert saved is not None
    assert saved.replaces == originals[-1].commit
    assert saved.replace_scope == scope
    assert saved.pinned == ctx._record.pinned


@pytest.mark.slow
def test_offline_replacement_recovers_conflict_export_and_lost_readback(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    originals: list[_records.CandidateRecord] = []
    ctx = None
    for index in range(3):
        if index == 1:
            _second_source_commit(source, 'later edit\n')
        elif index == 2:
            (source / 'third.txt').write_bytes(b'third release\n')
            _git_command(source, 'add', '.')
            _git_command(source, 'commit', '-qm', 'third')
        ctx = _context(tmp_path, source, version=f'1.0.{index}')
        _publish.publish(ctx)
        originals.append(ctx._record)
        assert _remote_ref(host, f'refs/tags/v1.0.{index}') == ctx._record.commit

    assert ctx is not None
    paths, g, project = ctx._paths, ctx._global_config, ctx._project
    old_a, old_b, old_c = originals
    _git_command(source, 'checkout', '--detach', old_a.pinned.commit)
    _second_source_commit(source, 'corrected\n')
    pinned = pin(source, 'HEAD')
    selected, scope = _rewrite.resolve_replacement(paths, project.name, 'v1.0.0')
    correction = _candidate.build_candidate(
        paths,
        g,
        project,
        _candidate.CandidateInputs(
            read_snapshot(source, pinned.commit),
            pinned,
            '1.0.0',
            None,
            replaces=selected,
            replace_scope=scope,
        ),
    )
    ctx = replace(ctx, _record=correction)
    completed = _resume_offline_conflict(ctx, old_b.commit, monkeypatch)
    assert _candidate.published_refs(paths, project.name)[0] == old_c.commit
    assert _remote_ref(host, 'refs/heads/main') == old_c.commit
    tip_copy = _verification.materialize_inspect(
        paths, project.name, _verification.prove_objects(paths, project.name, completed)
    )
    b2_copy = _verification.inspect_rewritten(
        paths, project.name, _verification.prove_objects(paths, project.name, completed), 'v1.0.1'
    )
    assert tip_copy != b2_copy
    assert (tip_copy / 'third.txt').read_text() == 'third release\n'
    assert not (b2_copy / 'third.txt').exists()
    assert (b2_copy / 'README.md').read_text() == 'corrected and later edit\n'

    calls: list[list[str]] = []
    real_push = _host.push

    def _push(dest: _host.Destination, store: Path, args: list[str]) -> None:
        calls.append(args)
        real_push(dest, store, args)

    monkeypatch.setattr(_host, 'push', _push)
    ctx = replace(ctx, _record=completed)
    with (
        _failed_readback(monkeypatch),
        pytest.raises(PubanonError, match='the outcome of the last publication is unknown'),
    ):
        _publish.publish(ctx)
    retained = _records.load_candidate(paths, project.name)
    assert retained is not None
    assert retained.state == _candidate.STATE_UNKNOWN_OUTCOME
    assert _publish.resolve(replace(ctx, _record=retained)) == 'published'
    assert len(calls) == 1
    assert _records.load_candidate(paths, project.name) is None
    _assert_offline_replacement_history(ctx, host, originals)


def test_ordinary_publication_preview_and_result_are_compact(
    publication_cli: tuple[Path, _publish.PublishContext],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _host_path, ctx = publication_cli
    capsys.readouterr()
    _publish.publish(ctx)
    output = capsys.readouterr().out
    assert f'Publish {ctx._record.tag_intent}' in output
    assert 'Destination:' in output
    assert 'Changes:' in output
    assert 'Checked:' in output
    assert 'content, identity, and destination' in output
    assert 'Snapshot commit:' not in output
    assert 'Source commit:' not in output
    assert 'stage push' not in output
    assert 'warning: GitHub keeps a record of this upload' in output
    assert (
        f'Published {ctx._record.tag_intent} to public-owner/public-repo '
        f'({ctx._project.visibility}).'
    ) in output


def test_page_ambiguity_is_reported_after_publication(
    page_context: tuple[_publish.PublishContext, _ReleaseHost],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx, host = page_context
    _configure_host(_new_host(ctx._paths.data_dir.parent), fake_gh, monkeypatch)
    plan = _releases.PagePlan(
        'none', 'ambiguous', candidates=('v1.0.0', 'v2.0.0'), project=ctx._project.name
    )

    def _plan(*_args: object) -> _releases.PagePlan:
        return plan

    monkeypatch.setattr(_releases, 'plan_publication_page', _plan)
    capsys.readouterr()
    _publish.publish(ctx)
    result = capsys.readouterr().out.split('Published v1.2.3', 1)[1]
    assert 'Release page: not created: v1.0.0 and v2.0.0 point here' in result
    for tag in plan.candidates:
        assert result.count(f'pubanon release create {tag} --project=example') == 1
    assert host.writes == []
    assert host.scans == 0


def test_page_replacement_question_creates_page(
    page_context: tuple[_publish.PublishContext, _ReleaseHost], capsys: pytest.CaptureFixture[str]
) -> None:
    ctx, host = page_context
    ctx = replace(
        ctx,
        _record=replace(
            ctx._record,
            replaces='a' * 40,
            prestate_main='a' * 40,
            replace_scope='one',
            tip=ctx._record.commit,
            poststate_tags={'v1.2.3': ctx._record.commit},
        ),
    )
    plan = _page_plan(ctx)
    verified = _publish._Verified(host.destination, _host.RemoteRefs(None, {}))
    assert (
        _publish._replacement_review(ctx, verified, plan)
        == 'Publish this replacement and create its release page?'
    )
    assert 'Release page' in capsys.readouterr().out


def test_publication_finalization_checks_old_values_atomically(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    ctx = _context(tmp_path, source, version='1.0.0')
    store = _store.store_path(ctx._paths, ctx._project.name)
    real_git = _publish.run_git

    def _git(
        args: list[str], *, cwd: Path, input_bytes: bytes | None = None, check: bool = True
    ) -> GitResult:
        if args[2] == 'update-ref':
            _git_command(store, 'update-ref', 'refs/tags/v1.0.0', ctx._record.commit)
        return real_git(args, cwd=cwd, input_bytes=input_bytes, check=check)

    monkeypatch.setattr(_publish, 'run_git', _git)
    with pytest.raises(PubanonError, match='cannot record published refs locally'):
        _publish._finalize(ctx)
    assert _remote_ref(store, 'refs/heads/main') is None
    assert _remote_ref(store, _candidate.CANDIDATE_REF) == ctx._record.commit
    assert _records.load_candidate(ctx._paths, ctx._project.name) == ctx._record
    with pytest.raises(PubanonError, match='saved published history matches neither'):
        _publish._finalize(ctx)
    assert _remote_ref(store, 'refs/heads/main') is None
    assert _remote_ref(store, _candidate.CANDIDATE_REF) == ctx._record.commit
    assert _records.load_candidate(ctx._paths, ctx._project.name) == ctx._record


def test_publication_next_action_watches_integrated_project(tmp_path: Path) -> None:
    _global_config, project = _configs(tmp_path)
    assert (
        _publish.publication_next_action(replace(project, ci_workflow='ci.yml'))
        == 'pubanon repo watch'
    )


@pytest.mark.parametrize('skip', [False, True])
def test_publication_without_page_shows_later_create_command(
    page_context: tuple[_publish.PublishContext, _ReleaseHost],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    skip: bool,
) -> None:
    ctx, host = page_context
    _configure_host(_new_host(ctx._paths.data_dir.parent), fake_gh, monkeypatch)
    ctx = replace(
        ctx,
        _project=replace(ctx._project, release_page=None),
        _release_page=_releases.PageRequest(skip=skip),
    )
    capsys.readouterr()
    _publish.publish(ctx)
    review, finished = capsys.readouterr().out.split('Published v1.2.3', 1)
    assert 'Add one later with:' not in review
    assert 'Release page: not created' in finished
    assert 'Add one later with: pubanon release create v1.2.3 --project=example' in finished
    assert host.scans == 0
    assert host.writes == []


@pytest.mark.slow
@pytest.mark.parametrize('boundary', ['intent', 'bookmark', 'unknown', 'unknown-saved'])
def test_publication_bookmark_failure_prevents_push(
    publication_cli: tuple[Path, _publish.PublishContext],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    host, ctx = publication_cli
    real_save = _publish.save_candidate

    def _fail(*_args: object) -> None:
        raise OSError('write denied')

    def _fail_save(paths: Paths, project: str, record: _candidate.CandidateRecord) -> None:
        if boundary.startswith('unknown') and record.state != _candidate.STATE_UNKNOWN_OUTCOME:
            real_save(paths, project, record)
            return
        if boundary == 'unknown-saved':
            real_save(paths, project, record)
        raise OSError('write denied')

    with monkeypatch.context() as fault:
        if boundary != 'bookmark':
            fault.setattr(_publish, 'save_candidate', _fail_save)
        else:
            fault.setattr(_source, 'ensure_bookmark', _fail)
        with pytest.raises(PubanonError) as error:
            _publish.publish(ctx)
    rendered = error.value.render()
    assert rendered.count('Nothing was published') == 1
    assert escape((ctx._project.source_path).as_posix()) in rendered
    if boundary in ('intent', 'bookmark'):
        assert 'No push was attempted' in rendered
        assert 'snapshot recovery information and any source bookmark are retained' in rendered
    assert 'pubanon publish' in rendered
    assert _remote_ref(host, 'refs/heads/main') is None
    retained = _records.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert (retained.source_bookmark is None) == (boundary == 'intent')
    if boundary == 'unknown-saved':
        assert 'pubanon publish --resolve' in error.value.render()
        assert _publish.resolve(replace(ctx, _record=retained)) == 'not-sent'
        retained = _records.load_candidate(ctx._paths, ctx._project.name)
        assert retained is not None
    _publish.publish(replace(ctx, _record=retained))
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit


@pytest.mark.slow
@pytest.mark.parametrize('structured', [False, True])
def test_publication_interrupt_after_record_removal_reports_completed_finalization(
    publication_cli: tuple[Path, _publish.PublishContext],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    structured: bool,
) -> None:
    host, ctx = publication_cli
    real_unlink = Path.unlink

    def _interrupt_unlink(path: Path, missing_ok: bool = False) -> None:
        real_unlink(path, missing_ok=missing_ok)
        if path == _record_path(ctx):
            if structured:
                raise OperationCanceled('operation cancelled')
            raise KeyboardInterrupt

    with monkeypatch.context() as fault:
        fault.setattr(Path, 'unlink', _interrupt_unlink)
        assert _cli.main(['publish', '--project', ctx._project.name, '--yes']) == 130
    error = capsys.readouterr().err
    assert f'Published and verified commit {ctx._record.commit}' in error
    assert 'local finalization completed' in error
    assert 'Canceled.' not in error
    assert 'cancellation' not in error
    assert 'incomplete' not in error
    assert '--resolve' not in error
    assert f'pubanon status --project={ctx._project.name} --remote' in error
    assert 'Traceback' not in error
    assert _records.load_candidate(ctx._paths, ctx._project.name) is None
    store = _store.store_path(ctx._paths, ctx._project.name)
    for ref in ('refs/heads/main', 'refs/tags/v1.0.0'):
        assert _remote_ref(store, ref) == _remote_ref(host, ref) == ctx._record.commit
    assert _cli.main(['status', '--project', ctx._project.name, '--remote']) == 0


@pytest.mark.slow
@pytest.mark.parametrize('structured', [False, True])
def test_publication_interrupt_before_push_preserves_retained_candidate(
    publication_cli: tuple[Path, _publish.PublishContext],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    structured: bool,
) -> None:
    host, ctx = publication_cli
    prompts: list[str] = []

    def _cancel_prompt(prompt: str) -> str:
        assert prompt == ('Publish v1.0.0 to public-owner/public-repo? [y/n] ')
        assert not prompts
        prompts.append(prompt)
        if structured:
            raise OperationCanceled('operation cancelled')
        raise KeyboardInterrupt

    with monkeypatch.context() as fault:
        fault.setattr('builtins.input', _cancel_prompt)
        assert _cli.main(['publish', '--project', ctx._project.name]) == 130
    assert len(prompts) == 1
    error = capsys.readouterr().err
    assert error.startswith('Canceled. Nothing was published.')
    assert 'The prepared snapshot is kept.' in error
    assert f'Next: pubanon publish --project={ctx._project.name}' in error
    assert 'Traceback' not in error
    _assert_provisional_bookmark(ctx)
    assert _remote_ref(host, 'refs/heads/main') is None
    assert _cli.main(['publish', '--project', ctx._project.name, '--yes']) == 0


@pytest.mark.slow
@pytest.mark.parametrize('phase', ['before-push', 'not-pushed'])
@pytest.mark.parametrize('saved', [False, True])
def test_publication_interrupt_during_state_write_has_working_recovery(
    publication_cli: tuple[Path, _publish.PublishContext],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    phase: str,
    saved: bool,
) -> None:
    host, ctx = publication_cli
    real_save = _publish.save_candidate
    target_state = (
        _candidate.STATE_UNKNOWN_OUTCOME if phase == 'before-push' else _candidate.STATE_RETAINED
    )

    def _interrupt_save(paths: Paths, project: str, record: _candidate.CandidateRecord) -> None:
        current = _records.load_candidate(paths, project)
        if record.state != target_state or (
            phase == 'not-pushed'
            and current is not None
            and current.state != _candidate.STATE_UNKNOWN_OUTCOME
        ):
            real_save(paths, project, record)
            return
        if saved:
            real_save(paths, project, record)
        raise KeyboardInterrupt

    def _fail_push(*_args: object) -> None:
        raise PubanonError('transport refused before sending')

    with monkeypatch.context() as fault:
        fault.setattr(_publish, 'save_candidate', _interrupt_save)
        fault.setattr(_host, 'push', _fail_push)
        assert _cli.main(['publish', '--project', ctx._project.name, '--yes']) == 130
    error = capsys.readouterr().err
    assert (
        'No push was attempted'
        if phase == 'before-push'
        else 'still points to the previous published snapshot'
    ) in error
    assert _remote_ref(host, 'refs/heads/main') is None
    record = _records.load_candidate(ctx._paths, ctx._project.name)
    assert record is not None
    args = ['publish', '--project', ctx._project.name, '--yes']
    if record.state == _candidate.STATE_UNKNOWN_OUTCOME:
        assert f'pubanon publish --project={ctx._project.name} --resolve' in error
        args.append('--resolve')
    else:
        assert '--resolve' not in error
        assert 'pubanon publish' in error
    assert _cli.main(args) == 0


@pytest.mark.slow
@pytest.mark.parametrize('phase', ['push-unsent', 'push-sent', 'readback'])
@pytest.mark.parametrize('structured', [False, True])
def test_publication_interrupt_reports_outcome_and_working_recovery(
    publication_cli: tuple[Path, _publish.PublishContext],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    phase: str,
    structured: bool,
) -> None:
    host, ctx = publication_cli
    real_push = _host.push
    real_read = _host.ls_remote
    reads = 0

    def _cancel() -> None:
        if structured:
            raise OperationCanceled('operation cancelled', fact='interrupted at boundary')
        raise KeyboardInterrupt

    def _cancel_push(dest: _host.Destination, store: Path, args: list[str]) -> None:
        if phase != 'push-unsent':
            real_push(dest, store, args)
        _cancel()

    def _cancel_read(dest: _host.Destination, paths: Paths, project: str) -> _host.RemoteRefs:
        nonlocal reads
        reads += 1
        if reads == 2:
            _cancel()
        return real_read(dest, paths, project)

    with monkeypatch.context() as fault:
        if phase == 'readback':
            fault.setattr(_host, 'ls_remote', _cancel_read)
        else:
            fault.setattr(_host, 'push', _cancel_push)
        args = ['publish', '--project', ctx._project.name, '--yes']
        if structured:
            args.extend(['--color', 'always'])
        assert _cli.main(args) == 130

    error = capsys.readouterr().err
    assert 'Traceback' not in error
    record = _records.load_candidate(ctx._paths, ctx._project.name)
    assert record is not None
    sent = phase in {'push-sent', 'readback'}
    assert _remote_ref(host, 'refs/heads/main') == (ctx._record.commit if sent else None)
    prefix = '\x1b[1;31m' if structured else ''
    assert error.startswith(prefix + 'error: the outcome of the last publication is unknown')
    error = re.sub(r'\x1b\[[0-9;]*m', '', error)
    assert 'Canceled.' not in error
    assert f'pubanon publish --project={ctx._project.name} --resolve' in error
    assert 'Published and verified' not in error
    assert record.state == _candidate.STATE_UNKNOWN_OUTCOME
    assert _cli.main(['publish', '--project', ctx._project.name, '--resolve']) == 0
    if sent:
        assert _records.load_candidate(ctx._paths, ctx._project.name) is None
    else:
        restored = _records.load_candidate(ctx._paths, ctx._project.name)
        assert restored is not None
        assert restored.state == _candidate.STATE_RETAINED


@pytest.mark.slow
@pytest.mark.parametrize(
    'publication_cli',
    [('public', '1.0.0'), ('private', '1.0.0'), ('public', None), ('private', None)],
    indirect=True,
)
def test_publication_preserves_source_and_record(
    publication_cli: tuple[Path, _publish.PublishContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    host, ctx = publication_cli
    calls: list[list[str]] = []
    real_push = _host.push

    def _counted(dest: _host.Destination, store: Path, args: list[str]) -> None:
        calls.append(args)
        real_push(dest, store, args)

    monkeypatch.setattr(_host, 'push', _counted)
    before = _git_command(ctx._project.source_path, 'show-ref', '--heads', '--tags')
    _publish.publish(ctx)
    record = _records.load_publication(ctx._paths, ctx._project.name, ctx._record.commit)
    assert record is not None
    assert record.source_bookmark is not None
    assert record.pinned == ctx._record.pinned
    assert record.destination.visibility == ctx._project.visibility
    assert record.tag_intent == ctx._record.tag_intent
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit
    assert (
        _remote_ref(ctx._project.source_path, record.source_bookmark.refname)
        == ctx._record.pinned.commit
    )
    assert _git_command(ctx._project.source_path, 'show-ref', '--heads', '--tags') == before
    assert (
        _remote_ref(
            _store.store_path(ctx._paths, ctx._project.name),
            _records.publication_ref(record.commit),
        )
        == record.commit
    )
    assert len(calls) == 1
    assert _records.load_candidate(ctx._paths, ctx._project.name) is None


@pytest.mark.slow
@pytest.mark.parametrize(
    ('boundary', 'drift'),
    [
        ('archive', False),
        ('publication-record', False),
        ('publication-record', True),
        ('refs', False),
        ('refs', True),
        ('unlink', False),
        ('unlink', True),
    ],
)
def test_publication_recovery_replays_recording_without_push(
    publication_cli: tuple[Path, _publish.PublishContext],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    drift: bool,
) -> None:
    host, ctx = publication_cli
    real_git = _history.run_git
    real_unlink = Path.unlink
    calls: list[list[str]] = []
    real_push = _host.push

    def _counted(dest: _host.Destination, store: Path, args: list[str]) -> None:
        calls.append(args)
        real_push(dest, store, args)

    def _fail_git(
        args: list[str], *, cwd: Path, input_bytes: bytes | None = None, check: bool = True
    ) -> GitResult:
        if args[2] == 'update-ref':
            raise OSError('ref write denied')
        return real_git(args, cwd=cwd, input_bytes=input_bytes, check=check)

    def _fail_record(*_args: object) -> None:
        raise OSError('record write denied')

    def _fail_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == _record_path(ctx):
            raise OSError('unlink denied')
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(_host, 'push', _counted)
    with monkeypatch.context() as fault:
        target, name, replacement = {
            'archive': (_history, 'run_git', _fail_git),
            'publication-record': (_records, 'save_publication', _fail_record),
            'refs': (_publish, 'run_git', _fail_git),
            'unlink': (Path, 'unlink', _fail_unlink),
        }[boundary]
        fault.setattr(target, name, replacement)
        with pytest.raises(PubanonError, match='Local finalization is incomplete'):
            _publish.publish(ctx)
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit
    retained = _records.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert retained.source_bookmark is not None
    assert (
        _remote_ref(ctx._project.source_path, retained.source_bookmark.refname)
        == retained.pinned.commit
    )
    first = _records.load_publication(ctx._paths, ctx._project.name, ctx._record.commit)
    if drift and boundary != 'archive':
        _git_command(host, 'update-ref', '-d', 'refs/heads/main')
        with pytest.raises(PubanonError, match='must not be retried'):
            _publish.resolve(replace(ctx, _record=retained))
    else:
        assert _publish.resolve(replace(ctx, _record=retained)) == 'published'
    saved = _records.load_publication(ctx._paths, ctx._project.name, ctx._record.commit)
    assert saved is not None
    if first is not None:
        assert saved.confirmed_at == first.confirmed_at
    assert _records.load_candidate(ctx._paths, ctx._project.name) is None
    assert len(calls) == 1


@pytest.mark.slow
@pytest.mark.parametrize('structured', [False, True])
def test_publication_reporting_cancellation_preserves_completed_record(
    publication_cli: tuple[Path, _publish.PublishContext],
    monkeypatch: pytest.MonkeyPatch,
    structured: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    host, ctx = publication_cli
    real_output = _publish.output

    def _interrupted(text: str = '', *, kind: _terminal.StyleKind = 'plain') -> None:
        if text.startswith('Published '):
            if structured:
                raise OperationCanceled('operation cancelled')
            raise KeyboardInterrupt
        real_output(text, kind=kind)

    monkeypatch.setattr(_publish, 'output', _interrupted)
    args = ['publish', '--project', ctx._project.name, '--yes']
    if structured:
        args.extend(['--color', 'always'])
    assert _cli.main(args) == 130
    error = capsys.readouterr().err
    prefix = '\x1b[32m' if structured else ''
    assert error.startswith(prefix + 'Publication and local finalization completed.')
    error = re.sub(r'\x1b\[[0-9;]*m', '', error)
    assert f'Published and verified commit {ctx._record.commit}' in error
    assert 'pubanon status --project=' in error
    assert '--remote; do not publish a new snapshot' in error
    assert 'operation cancelled' not in error
    assert 'Canceled.' not in error
    assert _records.load_candidate(ctx._paths, ctx._project.name) is None
    assert _records.load_publication(ctx._paths, ctx._project.name, ctx._record.commit) is not None
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit


@pytest.mark.slow
def test_publication_source_disappears_after_push_preserves_recorded_locator(
    publication_cli: tuple[Path, _publish.PublishContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    _host_path, ctx = publication_cli
    with _failed_readback(monkeypatch), pytest.raises(PubanonError, match='unknown'):
        _publish.publish(ctx)
    retained = _records.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert retained.source_bookmark is not None
    remove_tree(ctx._project.source_path)
    assert _publish.resolve(replace(ctx, _record=retained)) == 'published'
    saved = _records.load_publication(ctx._paths, ctx._project.name, ctx._record.commit)
    assert saved is not None
    assert saved.source_bookmark == retained.source_bookmark


@pytest.mark.slow
def test_publish_acknowledges_visible_exposure_before_confirmation(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exposure = 'historical-private-canary'
    _host_path, ctx = _context_after_first_publish(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
        ({f'{exposure}.txt': f'{exposure}\n'}, (exposure,)),
    )
    prompts: list[str] = []
    answers = [
        ('Continue despite this earlier exposure? [y/n] ', 'y'),
        (
            ('Publish untagged snapshot to public-owner/public-repo? [y/n] '),
            'n',
        ),
    ]

    def _answer(prompt: str) -> str:
        if not prompts:
            ordinary = capsys.readouterr().out
            assert exposure in ordinary
            assert 'item 1' in ordinary
        prompts.append(prompt)
        assert answers, f'unexpected prompt: {prompt}'
        expected, response = answers.pop(0)
        assert expected == prompt
        return response

    monkeypatch.setattr('builtins.input', _answer)
    capsys.readouterr()

    with pytest.raises(OperationCanceled, match=r'Canceled\. Nothing was published.'):
        _publish.publish(_publish_mode(ctx, 'interactive'))

    assert not answers
    output = capsys.readouterr().out
    assert prompts == [
        'Continue despite this earlier exposure? [y/n] ',
        'Publish untagged snapshot to public-owner/public-repo? [y/n] ',
    ]
    assert 'Publish untagged snapshot' in output
    saved = _config.load_project(ctx._paths, ctx._project.name)
    assert saved.acknowledged_exposure
    _assert_provisional_bookmark(ctx)
    retained = _records.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    _verify_for_publish(replace(ctx, _project=saved, _record=retained))


@pytest.mark.slow
def test_publish_acknowledgment_displays_identity_and_hygiene_in_one_path(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    identity = 'historical-private-segment'
    hygiene = 'historical-hygiene-segment'
    path = f'docs/{identity}-{hygiene}.txt'
    source = make_repo(files={'README.md': 'first\n', path: 'public\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    _publish.publish(_context(tmp_path, source))
    _git_command(source, 'rm', '-q', '--', path)
    _second_source_commit(source)
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    project = replace(
        project,
        extra_terms=(identity,),
        hygiene_terms=(hygiene,),
    )

    pinned = pin(source, 'HEAD')
    record = _candidate.build_candidate(
        paths,
        global_config,
        project,
        _candidate.CandidateInputs(
            read_snapshot(source, pinned.commit),
            pinned,
            None,
            None,
        ),
    )
    ctx = _publish.PublishContext(paths, global_config, project, record, _yes=False)
    prompts: list[str] = []
    answers = [
        ('Continue despite this earlier exposure? [y/n] ', 'y'),
        (
            'Publish untagged snapshot to public-owner/public-repo? [y/n] ',
            'n',
        ),
    ]
    before = _git_command(host, 'for-each-ref')

    def _answer(prompt: str) -> str:
        if not prompts:
            ordinary = capsys.readouterr().out
            assert identity in ordinary
            assert hygiene in ordinary
            assert 'item 1' in ordinary
        prompts.append(prompt)
        assert answers, f'unexpected prompt: {prompt}'
        expected, response = answers.pop(0)
        assert prompt == expected
        return response

    monkeypatch.setattr('builtins.input', _answer)
    capsys.readouterr()

    with pytest.raises(OperationCanceled, match=r'Canceled\. Nothing was published.'):
        _publish.publish(ctx)

    output = capsys.readouterr().out
    assert prompts == [
        'Continue despite this earlier exposure? [y/n] ',
        'Publish untagged snapshot to public-owner/public-repo? [y/n] ',
    ]
    assert 'Publish untagged snapshot' in output

    assert answers == []
    assert _git_command(host, 'for-each-ref') == before
    complete = paths.data_dir / 'projects' / project.name / f'candidate-files-{record.commit}.txt'
    assert_private_mode(complete, 0o600)
    assert path in complete.read_text()


@pytest.mark.slow
def test_publish_declined_exposure_acknowledgment_retains_candidate(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exposure = 'historical-private-canary'
    host, ctx = _context_after_first_publish(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
        ({'README.md': f'{exposure}\n'}, (exposure,)),
    )

    expected = ['Continue despite this earlier exposure? [y/n] ']

    def _decline(prompt: str) -> str:
        assert expected, f'unexpected prompt: {prompt}'
        assert expected.pop(0) == prompt
        return 'n'

    monkeypatch.setattr('builtins.input', _decline)

    with pytest.raises(OperationCanceled, match=r'Canceled\. Nothing was published.') as error:
        _publish.publish(_publish_mode(ctx, 'interactive'))

    assert 'Next: pubanon publish' in error.value.render()
    assert not expected
    assert _config.load_project(ctx._paths, ctx._project.name).acknowledged_exposure == ''
    _assert_provisional_bookmark(ctx)
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.prestate_main


@pytest.mark.slow
def test_publish_first_and_second_release_enforce_parent(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    first = _context(tmp_path, source, version='1.0.0')

    _publish.publish(first)

    assert _remote_ref(host, 'refs/heads/main') == first._record.commit
    assert _remote_ref(host, 'refs/tags/v1.0.0') == first._record.commit
    assert _records.load_candidate(first._paths, first._project.name) is None
    output = capsys.readouterr().out
    assert 'GitHub keeps a record of this upload' in output
    assert 'pubanon stage push' not in output
    assert 'Destination: public-owner/public-repo (public)' in output
    assert 'Published v1.0.0 to public-owner/public-repo (public).' in output
    assert 'Snapshot commit:' not in output

    _second_source_commit(source)
    second = _context(tmp_path, source)
    assert second._record.prestate_main == first._record.commit

    _publish.publish(second)
    assert 'GitHub keeps a record of this upload' not in capsys.readouterr().out

    assert _remote_ref(host, 'refs/heads/main') == second._record.commit
    assert (
        _git_command(
            _store.store_path(second._paths, second._project.name),
            'rev-parse',
            f'{second._record.commit}^',
        )
        .decode()
        .strip()
        == first._record.commit
    )


@pytest.mark.parametrize('unattended', [False, True])
def test_publish_fills_existing_repository_description_before_push(
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    unattended: bool,
) -> None:
    tmp_path = fake_gh.parent
    source = make_repo(files={'README.md': 'public\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    description = 'Public project'
    ctx = replace(_description_context(tmp_path, source, description), _yes=unattended)
    actual = _host.SettingsFacts(
        '', 'main', issues=True, wiki=False, projects=False, pull_requests=False, discussions=False
    )
    real_gh = _host._gh
    events: list[str] = []

    def _settings(_owner: str, _repo: str) -> _host.SettingsFacts:
        return actual

    def _gh(
        args: list[str], *, check: bool = True, timeout: float | None = None
    ) -> bytes | tuple[int, bytes, bytes]:
        nonlocal actual
        if args[:3] == ['api', '-X', 'PATCH']:
            assert args == [
                'api',
                '-X',
                'PATCH',
                'repos/public-owner/public-repo',
                '-f',
                f'description={description}',
            ]
            assert unattended or events == ['confirmed']
            assert _remote_ref(host, 'refs/heads/main') is None
            actual = actual._replace(description=description)
            events.append('description updated')
            return b''
        return real_gh(args, check=check, timeout=timeout)

    def _confirm(_prompt: str) -> str:
        assert actual.description == ''
        assert f'Description: {description}' in capsys.readouterr().out
        events.append('confirmed')
        return 'y'

    monkeypatch.setattr(_host, 'repository_settings', _settings)
    monkeypatch.setattr(_host, '_gh', _gh)
    monkeypatch.setattr('builtins.input', _confirm)

    _publish.publish(ctx)

    assert actual.description == description
    assert events == ([] if unattended else ['confirmed']) + ['description updated']
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit
    assert _records.load_candidate(ctx._paths, ctx._project.name) is None


@pytest.mark.parametrize('failure', ['read', 'write', 'verify', 'interrupt'])
def test_publish_description_failure_keeps_snapshot_for_retry(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _description_context(tmp_path, source, 'Public project')
    actual = _host.SettingsFacts(
        '', 'main', issues=True, wiki=False, projects=False, pull_requests=False, discussions=False
    )
    attempted: list[str] = []
    real_gh = _host._gh

    def _settings(_owner: str, _repo: str) -> _host.SettingsFacts:
        if failure == 'read':
            raise PubanonError('read failed')
        return actual

    def _gh(
        args: list[str], *, check: bool = True, timeout: float | None = None
    ) -> bytes | tuple[int, bytes, bytes]:
        nonlocal actual
        if args[:3] == ['api', '-X', 'PATCH']:
            attempted.append('patch')
            if failure == 'write':
                raise PubanonError('write failed')
            if failure == 'interrupt':
                actual = actual._replace(description='Public project')
                raise KeyboardInterrupt
            return b''
        return real_gh(args, check=check, timeout=timeout)

    with monkeypatch.context() as fault:
        fault.setattr(_host, 'repository_settings', _settings)
        fault.setattr(_host, '_gh', _gh)
        with pytest.raises(PubanonError) as error:
            _publish.publish(ctx)

    assert attempted == ([] if failure == 'read' else ['patch'])
    assert isinstance(error.value, OperationCanceled) is (failure == 'interrupt')
    assert 'No code was pushed' in error.value.render()
    assert 'description may have changed' in error.value.render()
    assert 'pubanon publish' in (error.value.recovery or '')
    assert _remote_ref(host, 'refs/heads/main') is None
    retained = _records.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert retained.state == _candidate.STATE_RETAINED
    assert retained.commit == ctx._record.commit

    def _updated_settings(_owner: str, _repo: str) -> _host.SettingsFacts:
        return actual._replace(description='Public project')

    monkeypatch.setattr(_host, 'repository_settings', _updated_settings)
    _publish.publish(replace(ctx, _record=retained))
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit


@pytest.mark.slow
def test_publish_hygiene_audit_result_does_not_block(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hygiene = 'historical-style-canary'
    _host_path, ctx = _context_after_first_publish(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
        ({'README.md': 'public history\n'}, ()),
    )
    real_audit = cast('Callable[..., _audit.HistoryAudit]', _audit.audit_history)

    def _audit_with_hygiene_finding(*args: object, **kwargs: object) -> _audit.HistoryAudit:
        audited = real_audit(*args, **kwargs)
        hygiene_finding = _audit.AuditFinding(
            'path:README.md',
            RuleClass.HYGIENE,
            hygiene,
        )
        return _audit.HistoryAudit(
            (*audited.findings, hygiene_finding),
            (),
            (
                *audited.evidence,
                _audit.HistoryEvidence(hygiene_finding, None, 'README.md', 'README.md:1'),
            ),
        )

    prompts: list[str] = []

    def _decline_mutation(prompt: str) -> str:
        prompts.append(prompt)
        return 'n'

    monkeypatch.setattr(_audit, 'audit_history', _audit_with_hygiene_finding)
    monkeypatch.setattr('builtins.input', _decline_mutation)

    with pytest.raises(OperationCanceled, match=r'Canceled\. Nothing was published.'):
        _publish.publish(_publish_mode(ctx, 'interactive'))

    assert prompts == ['Publish untagged snapshot to public-owner/public-repo? [y/n] ']


@pytest.mark.slow
def test_publish_ignores_host_managed_pull_refs(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    store = _store.store_path(ctx._paths, ctx._project.name)
    _git_command(host, 'fetch', str(store), f'{ctx._record.commit}:refs/pull/1/head')

    _publish.publish(ctx)

    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit
    assert _remote_ref(host, 'refs/pull/1/head') == ctx._record.commit


@pytest.mark.slow
@pytest.mark.parametrize('case', [(None, False), ('1.0.0', False), (None, True), ('1.0.0', True)])
def test_publish_marks_source_only_when_enabled(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str | None, bool],
) -> None:
    version, source_tags = case
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source, version=version)
    if source_tags:
        ctx = _with_source_tags(ctx)

    _publish.publish(ctx)

    name = f'pubanon/{ctx._record.tag_intent or ctx._record.commit}'
    source_ref = f'refs/tags/{name}'
    expected = ctx._record.pinned.commit if source_tags else None
    assert _remote_ref(source, source_ref) == expected
    assert _remote_ref(host, source_ref) is None
    assert _remote_ref(_store.store_path(ctx._paths, ctx._project.name), source_ref) is None
    assert _records.load_candidate(ctx._paths, ctx._project.name) is None


@pytest.mark.slow
def test_publish_proceeds_when_staging_evidence_covers_the_candidate(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    ctx = replace(ctx, _project=replace(ctx._project, publication_route='staged'))
    _configure_staged_candidate(ctx, fake_gh, monkeypatch)

    output = StringIO()
    with redirect_stdout(output):
        _publish.publish(ctx)

    assert ctx._record.commit not in output.getvalue()
    assert 'No required staging checks configured' in output.getvalue()
    assert 'CI checks succeeded' not in output.getvalue()
    assert 'GitHub keeps a record of this upload' not in output.getvalue()
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit
    assert _records.load_candidate(ctx._paths, ctx._project.name) is None


def test_publish_rechecks_ci_after_confirmation(
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(fake_gh.parent)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _publish_mode(_ci_context(fake_gh.parent, source), 'interactive')
    assert ctx._project.settings is not None
    ctx = replace(
        ctx,
        _project=replace(
            ctx._project, settings=replace(ctx._project.settings, description='Public project')
        ),
    )
    _configure_staged_candidate(ctx, fake_gh, monkeypatch)
    _configure_ci_evidence(ctx, fake_gh)
    prompts: list[str] = []

    def _confirm(prompt: str) -> str:
        prompts.append(prompt)
        responses = json.loads(fake_gh.read_text())
        for response in responses:
            if '/actions/workflows/ci.yml/runs?' in ' '.join(response['args']):
                data = json.loads(response['stdout'])
                run = data['workflow_runs'][0]
                data['workflow_runs'].append(
                    {
                        **run,
                        'id': 10,
                        'run_number': 10,
                        'status': 'in_progress',
                        'conclusion': None,
                    }
                )
                data['total_count'] = len(data['workflow_runs'])
                response['stdout'] = json.dumps(data)
        fake_gh.write_text(json.dumps(responses))
        return 'y'

    monkeypatch.setattr('builtins.input', _confirm)
    with pytest.raises(PubanonError, match='the latest staging CI run is not acceptable') as error:
        _publish.publish(ctx)
    assert len(prompts) == 1
    assert '#10' in error.value.render()
    assert 'Nothing was published. No push was attempted' in error.value.render()
    assert error.value.recovery == 'wait for the run to complete, then pubanon stage watch'
    assert _remote_ref(host, 'refs/heads/main') is None
    saved = _records.load_candidate(ctx._paths, ctx._project.name)
    assert saved is not None
    assert saved.commit == ctx._record.commit
    assert saved.state == _candidate.STATE_RETAINED
    assert not any('PATCH' in call for call in gh_calls(fake_gh.parent))


def test_publish_refuses_existing_intended_tag(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source, version='1.0.0')
    store = _store.store_path(ctx._paths, ctx._project.name)
    _git_command(host, 'fetch', str(store), f'{ctx._record.commit}:refs/tags/v1.0.0')

    with pytest.raises(PubanonError, match='intended tag already exists'):
        _publish.publish(ctx)


def test_publish_refuses_record_edited_before_review(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source, version='1.0.0')
    preserve_previous_tip = _history.preserve_previous_tip

    def _preserve_then_edit(
        paths: Paths, project: str, record: _candidate.CandidateRecord
    ) -> None:
        preserve_previous_tip(paths, project, record)
        current = _records.load_candidate(paths, project)
        assert current is not None
        _records.save_candidate(
            paths, project, replace(current, message=current.message + ' (edited)')
        )

    def _refuse_push(*_args: object) -> Never:
        pytest.fail('edited snapshot record reached publication transport')

    monkeypatch.setattr(_history, 'preserve_previous_tip', _preserve_then_edit)
    monkeypatch.setattr(_host, 'push', _refuse_push)

    with pytest.raises(PubanonError, match='snapshot record changed'):
        _publish.publish(ctx)


def test_publish_refuses_remote_prestate_mismatch_before_push(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    store = _store.store_path(ctx._paths, ctx._project.name)
    _git_command(host, 'fetch', str(store), f'{ctx._record.commit}:refs/heads/main')

    with pytest.raises(PubanonError, match='what this snapshot expects') as error:
        _publish.publish(ctx)

    rendered = error.value.render()
    assert 'Expected:' in rendered
    assert 'Found:' in rendered
    assert 'merge' not in rendered
    assert 'pull request' not in rendered
    assert _records.load_candidate(ctx._paths, ctx._project.name) == ctx._record


@pytest.mark.slow
def test_publish_refuses_repaired_structural_history_before_acknowledgment_or_preview(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exposure = 'historical-private-canary'
    source, host, _first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )
    store = _store.store_path(second._paths, second._project.name)
    tree = _git_command(store, 'rev-parse', f'{second._record.commit}^{{tree}}').decode().strip()
    invalid = _literal_history_commit(
        store,
        tree,
        author_name='Foreign Author',
        message=f'{exposure}\n'.encode(),
        parent=second._record.commit,
    )
    _git_command(store, 'update-ref', 'refs/heads/main', invalid)
    _git_command(host, 'fetch', '-q', str(store), f'+{invalid}:refs/heads/main')
    _second_source_commit(source, 'third\n')
    ctx = _context(tmp_path, source, extra_terms=(exposure,))
    remote_before = _remote_ref(host, 'refs/heads/main')

    def _unexpected_prompt(_prompt: str) -> str:
        pytest.fail('structural publication failure requested acknowledgment')

    def _unexpected_push(*_args: object, **_kwargs: object) -> GitResult:
        pytest.fail('structural publication failure attempted a push')

    monkeypatch.setattr('builtins.input', _unexpected_prompt)
    monkeypatch.setattr(_host, 'push', _unexpected_push)
    capsys.readouterr()

    with pytest.raises(PubanonError, match='published history has identity problems'):
        _publish.publish(ctx)

    assert 'Publish prepared snapshot' not in capsys.readouterr().out
    assert _remote_ref(host, 'refs/heads/main') == remote_before
    _assert_provisional_bookmark(ctx)


def test_publish_refuses_sensitive_description_before_host_access(
    tmp_path: Path,
    make_repo: Callable[..., Path],
) -> None:
    ctx = _description_context(tmp_path, make_repo(), 'By Private Person')

    with pytest.raises(PubanonError, match='repository description did not pass content checks'):
        _publish.publish(ctx)

    assert gh_calls(tmp_path) == []
    assert _records.load_candidate(ctx._paths, ctx._project.name) == ctx._record


def test_publish_refuses_stale_stage_without_preview_or_push(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    candidate_a = ctx._record
    _discard.discard(ctx._paths, ctx._project.name, force=True)
    (source / 'README.md').write_text('candidate B\n')
    _git_command(source, 'add', 'README.md')
    _git_command(
        source,
        '-c',
        'user.name=Test User',
        '-c',
        'user.email=test@example.invalid',
        'commit',
        '-qm',
        'candidate B',
    )
    ctx = _context(tmp_path, source)
    stale_commit = candidate_a.commit
    assert stale_commit != ctx._record.commit
    ctx = replace(ctx, _project=replace(ctx._project, publication_route='staged'))
    _configure_staged_candidate(ctx, fake_gh, monkeypatch, commit=stale_commit)

    output = StringIO()
    with (
        redirect_stdout(output),
        pytest.raises(PubanonError, match='staging evidence does not cover') as exc_info,
    ):
        _publish.publish(ctx)

    assert output.getvalue() == ''
    rendered = exc_info.value.render()
    assert stale_commit in rendered
    assert 'pubanon stage push' in rendered
    assert _remote_ref(host, 'refs/heads/main') is None
    assert _records.load_candidate(ctx._paths, ctx._project.name) == ctx._record


def test_publish_refuses_unexpected_destination_branch_before_push(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    term = 'private-branch-name'
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source, extra_terms=(term,))
    store = _store.store_path(ctx._paths, ctx._project.name)
    _git_command(
        host,
        'fetch',
        str(store),
        f'{ctx._record.commit}:refs/heads/{term}',
    )

    with pytest.raises(PubanonError, match='what this snapshot expects') as error:
        _publish.publish(ctx)

    assert term in error.value.render()
    assert error.value.actual == (f'main=absent; tags=none; branches={term}={ctx._record.commit}')
    assert _remote_ref(host, 'refs/heads/main') is None
    assert _records.load_candidate(ctx._paths, ctx._project.name) == ctx._record


@pytest.mark.slow
def test_publish_reports_hygiene_only_tag_in_identity_failure(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    term = 'historical-hygiene-tag'
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    first = _context(tmp_path, source, version='1.0.0')
    _publish.publish(first)
    store = _store.store_path(first._paths, first._project.name)
    blob = (
        _git_command(store, 'hash-object', '-w', '--stdin', input_bytes=b'not a commit\n')
        .decode()
        .strip()
    )
    _git_command(store, 'update-ref', f'refs/tags/{term}', blob)
    _git_command(host, 'fetch', '-q', str(store), f'refs/tags/{term}:refs/tags/{term}')
    _second_source_commit(source)
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    project = replace(project, hygiene_terms=(term,))

    pinned = pin(source, 'HEAD')
    record = _candidate.build_candidate(
        paths,
        global_config,
        project,
        _candidate.CandidateInputs(
            read_snapshot(source, pinned.commit),
            pinned,
            None,
            None,
        ),
    )
    ctx = _publish.PublishContext(paths, global_config, project, record, _yes=True)
    capsys.readouterr()

    with pytest.raises(PubanonError, match='published history has identity problems') as error:
        _publish.publish(ctx)

    rendered = error.value.render()
    assert f'tag:{term}: tag does not point directly to a commit' in rendered
    assert term in rendered
    assert 'Publish prepared snapshot' not in capsys.readouterr().out


@pytest.mark.slow
def test_publish_requires_completed_rewrite_cleanup_before_any_external_work(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    paths = _paths(tmp_path)
    g, project = _configs(source)
    unlink = Path.unlink

    def _fail_remove(path: Path, *, missing_ok: bool = False) -> None:
        if path == _records.rewrite_path(paths, project.name):
            raise OSError('interrupted completed rewrite cleanup')
        unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as fault:
        fault.setattr(Path, 'unlink', _fail_remove)
        with pytest.raises(PubanonError, match='cannot clean completed rewrite record') as error:
            _rewrite_fixture(tmp_path, source, fake_gh, fault)
    assert error.value.recovery == 'pubanon prepare --continue'
    assert isinstance(error.value.__cause__, OSError)
    record = _records.load_candidate(paths, project.name)
    assert record is not None
    assert record.tip is not None
    assert _records.load_rewrite(paths, project.name) is not None
    host = tmp_path / 'host.git'
    _configure_host(host, fake_gh, monkeypatch)
    before = _git_command(host, 'show-ref')
    ctx = _publish.PublishContext(paths, g, project, record, _yes=True)

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail('unfinished rewrite reached host or preservation work')

    with monkeypatch.context() as blocked:
        blocked.setattr(_host, 'verify_destination', _forbidden)
        blocked.setattr(_publish, '_preserve_source', _forbidden)
        blocked.setattr(_history, 'preserve_previous_tip', _forbidden)
        with pytest.raises(PubanonError, match='rewrite completion is pending') as error:
            _publish.publish(ctx)
    assert 'pubanon prepare --project=example --continue' in error.value.render()
    assert _git_command(host, 'show-ref') == before
    assert _records.load_candidate(paths, project.name) == record
    complete = _rewrite.continue_rewrite(paths, g, project, env={})
    assert _records.load_rewrite(paths, project.name) is None
    _publish.publish(replace(ctx, _record=complete))
    assert _git_command(host, 'rev-parse', 'main').decode().strip() == complete.tip
    assert _records.load_candidate(paths, project.name) is None


def test_publish_requires_confirmation_but_retains_declined_candidate(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _publish_mode(_description_context(tmp_path, source, 'Public project'), 'interactive')

    def _decline(prompt: str) -> str:
        assert prompt == ('Publish untagged snapshot to public-owner/public-repo? [y/n] ')
        return 'n'

    monkeypatch.setattr('builtins.input', _decline)

    with pytest.raises(OperationCanceled, match=r'Canceled\. Nothing was published.'):
        _publish.publish(ctx)

    _assert_provisional_bookmark(ctx)
    assert _remote_ref(host, 'refs/heads/main') is None
    assert not any('PATCH' in call for call in gh_calls(tmp_path))


def test_publish_verification_reads_snapshot_once(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    reads: list[int] = []
    original = _verification.read_snapshot

    def _counting(root: Path, commit: str, *, git_dir: Path | None = None) -> Snapshot:
        reads.append(1)
        return original(root, commit, git_dir=git_dir)

    monkeypatch.setattr(_verification, 'read_snapshot', _counting)

    _verify_for_publish(ctx)

    assert len(reads) == 1


def test_publish_without_ci_workflow_keeps_check_runs(
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(fake_gh.parent)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _ci_context(fake_gh.parent, source, ci_workflow=None, required=('build',))
    _configure_staged_candidate(ctx, fake_gh, monkeypatch)
    prefix = f'repos/{ctx._project.owner}/{_config.stage_repository(ctx._project)}'
    responses = json.loads(fake_gh.read_text())
    responses.append(
        {
            'args': [
                'api',
                f'{prefix}/commits/{ctx._record.commit}/check-runs?check_name=build&filter=latest&per_page=100',
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
    _publish.publish(ctx)
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit
    calls = gh_calls(fake_gh.parent)
    assert any('/check-runs?' in ' '.join(call) for call in calls)
    assert not any('/actions/' in ' '.join(call) for call in calls)


@pytest.mark.slow
def test_publish_yes_refuses_unacknowledged_exposure_without_prompt(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exposure = 'historical-private-canary'
    _host_path, ctx = _context_after_first_publish(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
        ({'README.md': f'{exposure}\n'}, (exposure,)),
    )

    def _unexpected_prompt(_prompt: str) -> str:
        pytest.fail('--yes requested an exposure acknowledgment')

    monkeypatch.setattr('builtins.input', _unexpected_prompt)

    with pytest.raises(
        PubanonError,
        match='--yes cannot acknowledge historical exposure',
    ) as error:
        _publish.publish(ctx)

    assert error.value.recovery == (
        'run pubanon audit to review the findings, then rerun without --yes'
    )
    _assert_provisional_bookmark(ctx)


@pytest.mark.slow
def test_push_writes_unknown_outcome_before_transport_runs(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source, version='1.0.0')
    real_push = _host.push

    def _inspect_then_push(
        dest: _host.Destination,
        cwd: Path,
        args: list[str],
    ) -> GitResult:
        current = _records.load_candidate(ctx._paths, ctx._project.name)
        assert current is not None
        assert current.state == _candidate.STATE_UNKNOWN_OUTCOME
        assert args == [
            '--atomic',
            '--force-with-lease=refs/heads/main:',
            '--force-with-lease=refs/tags/v1.0.0:',
            '--no-follow-tags',
            f'{ctx._record.commit}:refs/heads/main',
            f'{ctx._record.commit}:refs/tags/v1.0.0',
        ]
        return real_push(dest, cwd, args)

    monkeypatch.setattr(_host, 'push', _inspect_then_push)

    _publish.publish(ctx)

    assert not _record_path(ctx).exists()


def test_readback_failure_leaves_unknown_outcome_and_publish_refuses_resend(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)

    with (
        _failed_readback(monkeypatch),
        pytest.raises(
            PubanonError, match='the outcome of the last publication is unknown'
        ) as raised,
    ):
        _publish.publish(ctx)

    assert raised.value.fact == 'error: simulated remote read failure'
    assert raised.value.recovery == 'pubanon publish --resolve'
    retained = _records.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert retained.state == _candidate.STATE_UNKNOWN_OUTCOME
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit
    assert _remote_ref(_store.store_path(ctx._paths, ctx._project.name), 'refs/heads/main') is None
    retry = replace(ctx, _record=retained)

    def _refuse_resend(*_args: object) -> None:
        pytest.fail('unknown publication outcome attempted another push')

    monkeypatch.setattr(_host, 'push', _refuse_resend)
    with pytest.raises(PubanonError) as error:
        _publish.publish(retry)
    assert error.value.recovery == 'pubanon publish --resolve'


@pytest.mark.slow
def test_recreated_destination_refuses_correction_until_history_is_restored(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host, _first, ctx = _replacement_fixture(tmp_path, source, fake_gh, monkeypatch, '1.0.0')
    remove_tree(host)
    _new_host(tmp_path)

    with pytest.raises(PubanonError, match='final repository is empty') as error:
        _publish.publish(ctx)

    recovery = error.value.recovery
    assert recovery is not None
    assert 'pubanon repo restore --to remote' in recovery
    assert 'Recreating the final repository' in recovery
    assert _records.load_candidate(ctx._paths, ctx._project.name) == ctx._record

    _publish.sync(_sync_context(ctx))
    _publish.publish(ctx)

    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit
    assert _remote_ref(host, 'refs/tags/v1.0.0') == ctx._record.commit


def test_rejected_push_with_unrelated_tag_keeps_candidate_reusable(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)

    def _reject_after_drift(
        _dest: _host.Destination,
        _cwd: Path,
        _args: list[str],
    ) -> GitResult:
        store = _store.store_path(ctx._paths, ctx._project.name)
        _git_command(host, 'fetch', str(store), f'{ctx._record.commit}:refs/tags/unrelated')
        raise PubanonError('credential-bearing command failed')

    monkeypatch.setattr(_host, 'push', _reject_after_drift)

    with pytest.raises(PubanonError, match='credential-bearing command failed'):
        _publish.publish(ctx)

    retained = _records.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert retained.state == _candidate.STATE_RETAINED


def test_remote_prestate_empty_host_recovery_restores_published_history(
    tmp_path: Path,
) -> None:
    ctx = _literal_context(tmp_path)
    release = replace(ctx._record, prestate_main='1' * 40, prestate_tags={'v0.1.0': '1' * 40})

    with pytest.raises(PubanonError, match='final repository is empty') as excinfo:
        _verify_remote(release, _host.RemoteRefs(None, {}))

    recovery = excinfo.value.recovery
    assert recovery is not None
    assert 'pubanon repo restore --to remote' in recovery
    assert 'pubanon publish' in recovery


def test_remote_prestate_recovery_names_operator_guide(
    tmp_path: Path,
) -> None:
    ctx = _literal_context(tmp_path)
    stale = replace(ctx._record, prestate_main='1' * 40)
    remote = _host.RemoteRefs('2' * 40, {})

    with pytest.raises(PubanonError) as excinfo:
        _verify_remote(stale, remote)

    recovery = excinfo.value.recovery
    assert recovery is not None
    assert 'pubanon status --remote' in recovery
    assert 'Accepting an outside fix' in recovery
    assert 'operator guide' in recovery


def test_remote_prestate_renders_complete_terminal_safe_ref_fields(
    tmp_path: Path,
) -> None:
    term = 'private-canary'
    ctx = _literal_context(tmp_path, extra_terms=(term,))
    branch = f'backup\x1b{term}'
    remote = _host.RemoteRefs(None, {}, ((branch, 'a' * 40),))

    with pytest.raises(PubanonError, match='what this snapshot expects') as exc_info:
        _verify_remote(ctx._record, remote)

    assert exc_info.value.expected == 'main=absent; tags=none; branches=none'
    actual = exc_info.value.actual
    assert actual is not None
    assert actual == f'main=absent; tags=none; branches=backup\\x1b{term}={"a" * 40}'
    assert '\x1b' not in actual
    assert term in actual


@pytest.mark.slow
def test_replace_all_confirmation_handles_no_removed_tags(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    _host_path, ctx, originals = _rewrite_fixture(
        tmp_path, source, fake_gh, monkeypatch, selection=None
    )
    record = replace(
        ctx._record,
        prestate_tags={'v1.0.2': originals[-1].commit},
    )
    destination = _host.verify_destination(ctx._paths, ctx._global_config, ctx._project)
    with redirect_stdout(StringIO()):
        question = _publish._replacement_review(
            replace(ctx, _record=record),
            _publish._Verified(
                destination, _host.RemoteRefs(record.prestate_main, record.prestate_tags)
            ),
            _page_plan(ctx),
        )
    assert question == 'Publish this replacement?'


@pytest.mark.slow
def test_replace_all_preview_shows_removed_tags_and_scope(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    _host_path, ctx, originals = _rewrite_fixture(
        tmp_path, source, fake_gh, monkeypatch, selection=None
    )
    destination = _host.verify_destination(ctx._paths, ctx._global_config, ctx._project)
    questions: list[str] = []

    def _approve(question: str) -> bool:
        questions.append(question)
        return True

    monkeypatch.setattr(_publish, 'confirm', _approve)
    capsys.readouterr()
    _publish._review_publication(
        _publish_mode(ctx, 'interactive'),
        _publish._Verified(
            destination, _host.RemoteRefs(ctx._record.prestate_main, ctx._record.prestate_tags)
        ),
        _page_plan(ctx),
    )
    preview = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'Scope: all published history (3 snapshots) -> one snapshot' in preview
    assert 'Removed tags: v1.0.0, v1.0.1\n' in preview
    assert f'Commit: {originals[-1].commit[:12]} -> {ctx._record.commit[:12]}' in preview
    assert questions == ['Publish this replacement?']


@pytest.mark.slow
def test_replace_all_publishes_updated_identity_in_exact_new_history(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    host, ctx, originals = _rewrite_fixture(tmp_path, source, fake_gh, monkeypatch, selection=None)
    _discard.discard(ctx._paths, ctx._project.name, force=False)
    identity = replace(ctx._global_config, anon_name='Corrected Publisher')
    pinned = pin(source, 'HEAD')
    candidate = _candidate.build_candidate(
        ctx._paths,
        identity,
        ctx._project,
        _candidate.CandidateInputs(
            read_snapshot(source, pinned.commit),
            pinned,
            '1.0.2',
            None,
            replaces=originals[-1].commit,
            replace_scope='all',
        ),
    )
    _publish.publish(replace(ctx, _global_config=identity, _record=candidate))
    assert _git_command(host, 'rev-list', 'main').decode().splitlines() == [candidate.commit]
    assert _git_command(host, 'tag', '--list').decode().splitlines() == ['v1.0.2']
    assert _git_command(host, 'rev-parse', 'v1.0.2').decode().strip() == candidate.commit
    headers = _git_command(host, 'show', '-s', '--format=%an%n%ae%n%cn%n%ce', 'main')
    assert headers.decode().splitlines() == [
        identity.anon_name,
        identity.anon_email,
        identity.anon_name,
        identity.anon_email,
    ]


def test_replaced_tag_object_reports_unchanged_selected_commit(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    _git_command(source, 'tag', 'release')
    ctx = _publish_mode(_context(tmp_path, source, selector='refs/tags/release'), 'interactive')
    _git_command(source, 'tag', '-fa', 'release', '-m', 'annotated release')

    captured = StringIO()
    with redirect_stdout(captured):
        _verify_for_publish(ctx)
    assert (
        captured.getvalue()
        == 'Warning: source tag has changed; it still selects the prepared source commit.\n'
    )
    assert _records.load_candidate(ctx._paths, ctx._project.name) == ctx._record


@pytest.mark.slow
def test_replacement_completion_reports_tip_and_watches_published_main(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    _host_path, ctx, _originals = _rewrite_fixture(tmp_path, source, fake_gh, monkeypatch)
    assert ctx._record.tip is not None
    project = replace(ctx._project, required_checks=('build',))

    capsys.readouterr()
    _publish._report_publication(replace(ctx, _project=project))
    report = re.sub(r': +', ': ', capsys.readouterr().out)
    assert f'Snapshot commit: {ctx._record.tip[:12]}' in report
    assert 'Corrected snapshot: v1.0.1' in report
    assert 'Replayed snapshots: 1' in report
    assert 'Moved tags: v1.0.1, v1.0.2' in report
    assert 'Next:' not in report
    assert _publish.publication_next_action(project, ctx._record) == 'pubanon repo watch'


@pytest.mark.slow
def test_replacement_refreshes_source_tag(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    host, first, ctx = _replacement_fixture(tmp_path, source, fake_gh, monkeypatch, '1.0.0')
    ctx = _with_source_tags(ctx)
    _history.refresh_source_tags(
        ctx._project, _history.publications(ctx._paths, ctx._project.name)
    )
    source_ref = 'refs/tags/pubanon/v1.0.0'
    assert _remote_ref(source, source_ref) == first.pinned.commit

    _publish.publish(ctx)

    assert _remote_ref(source, source_ref) == ctx._record.pinned.commit
    assert _remote_ref(host, source_ref) is None
    assert _remote_ref(_store.store_path(ctx._paths, ctx._project.name), source_ref) is None
    assert _records.load_candidate(ctx._paths, ctx._project.name) is None


def test_resolution_requires_saved_source_bookmark(
    publication_cli: tuple[Path, _publish.PublishContext],
) -> None:
    host, ctx = publication_cli
    _git_command(
        host,
        'fetch',
        str(_store.store_path(ctx._paths, ctx._project.name)),
        f'{ctx._record.commit}:refs/heads/main',
        f'{ctx._record.commit}:refs/tags/v1.0.0',
    )
    unknown = replace(ctx._record, state=_candidate.STATE_UNKNOWN_OUTCOME, source_bookmark=None)
    _candidate.save_candidate(ctx._paths, ctx._project.name, unknown)
    with pytest.raises(PubanonError, match='source bookmark is missing'):
        _publish.resolve(replace(ctx, _record=unknown))
    assert _records.load_candidate(ctx._paths, ctx._project.name) == unknown
    assert _records.load_publication(ctx._paths, ctx._project.name, ctx._record.commit) is None
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit


def test_resolve_reads_remote_exactly_once(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _publish._preserve_source(_context(tmp_path, source))
    unknown = replace(ctx._record, state=_candidate.STATE_UNKNOWN_OUTCOME)
    _candidate.save_candidate(ctx._paths, ctx._project.name, unknown)
    store = _store.store_path(ctx._paths, ctx._project.name)
    _git_command(host, 'fetch', str(store), f'{unknown.commit}:refs/heads/main')
    reads: list[int] = []
    real_ls_remote = _host.ls_remote

    def _counting(
        dest: _host.Destination,
        paths: Paths,
        project: str,
    ) -> _host.RemoteRefs:
        reads.append(1)
        return real_ls_remote(dest, paths, project)

    monkeypatch.setattr(_host, 'ls_remote', _counting)

    assert _publish.resolve(replace(ctx, _record=unknown)) == 'published'
    assert len(reads) == 1


def test_resolve_refuses_account_mismatch_before_remote_read(
    tmp_path: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_host(tmp_path / 'host.git', fake_gh, monkeypatch)
    ctx = _literal_context(tmp_path)
    unknown = replace(ctx._record, state=_candidate.STATE_UNKNOWN_OUTCOME)
    _candidate.save_candidate(ctx._paths, ctx._project.name, unknown)
    fake_gh.write_text(
        json.dumps(
            [
                {
                    'args': ['api', 'user', '--jq', '.login'],
                    'stdout': 'real-login-canary\n',
                }
            ]
        )
        + '\n',
        encoding='utf-8',
    )

    def _unexpected_remote_read(*_args: object, **_kwargs: object) -> _host.RemoteRefs:
        pytest.fail('resolve read remote refs before destination verification')

    monkeypatch.setattr(_host, 'ls_remote', _unexpected_remote_read)

    with pytest.raises(PubanonError, match='authenticated account') as exc_info:
        _publish.resolve(replace(ctx, _record=unknown))

    rendered = exc_info.value.render()
    assert 'release-account' in rendered
    assert 'real-login-canary' in rendered
    assert _records.load_candidate(ctx._paths, ctx._project.name) == unknown


def test_resolve_refuses_unsupported_recorded_adapter_before_host_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _literal_context(tmp_path)
    unknown = replace(
        ctx._record,
        destination=replace(
            ctx._record.destination,
            adapter='unsupported-adapter-canary',
        ),
        state=_candidate.STATE_UNKNOWN_OUTCOME,
    )
    _candidate.save_candidate(ctx._paths, ctx._project.name, unknown)
    loaded = _records.load_candidate(ctx._paths, ctx._project.name)
    assert loaded is not None

    def _unexpected_verification(*_args: object, **_kwargs: object) -> _host.Destination:
        pytest.fail('resolve verified an unsupported recorded adapter')

    def _unexpected_remote_read(*_args: object, **_kwargs: object) -> _host.RemoteRefs:
        pytest.fail('resolve read remote refs for an unsupported recorded adapter')

    monkeypatch.setattr(_host, '_verify_destination', _unexpected_verification)
    monkeypatch.setattr(_host, 'ls_remote', _unexpected_remote_read)

    with pytest.raises(PubanonError, match='unsupported recorded destination adapter') as exc_info:
        _publish.resolve(replace(ctx, _record=loaded))

    assert 'unsupported-adapter-canary' not in exc_info.value.render()
    assert _records.load_candidate(ctx._paths, ctx._project.name) == unknown


def test_resolve_reports_unexpected_destination_branch_and_retains_candidate(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    term = 'private-branch-name'
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source, extra_terms=(term,))
    unknown = replace(ctx._record, state=_candidate.STATE_UNKNOWN_OUTCOME)
    _candidate.save_candidate(ctx._paths, ctx._project.name, unknown)
    store = _store.store_path(ctx._paths, ctx._project.name)
    _git_command(host, 'fetch', str(store), f'{unknown.commit}:refs/heads/{term}')

    assert _publish.resolve(replace(ctx, _record=unknown)) == 'not-sent'

    assert capsys.readouterr().out == (
        f'unrelated remote drift: branches: {term}={unknown.commit}\n'
    )
    retained = _records.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert retained.state == _candidate.STATE_RETAINED


def test_resolve_uses_recorded_destination_when_configuration_drifted(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    unknown = replace(ctx._record, state=_candidate.STATE_UNKNOWN_OUTCOME)
    _candidate.save_candidate(ctx._paths, ctx._project.name, unknown)
    drifted = replace(
        ctx,
        _global_config=replace(ctx._global_config, accounts={'github': 'changed-account'}),
        _project=replace(
            ctx._project,
            owner='changed-owner',
            repo='changed-repo',
            visibility='private',
        ),
        _record=unknown,
    )

    assert _publish.resolve(drifted) == 'not-sent'

    retained = _records.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert retained.state == _candidate.STATE_RETAINED


@pytest.mark.slow
def test_rewrite_known_source_without_locator_remains_input_provenance(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    _, ctx, originals = _rewrite_fixture(tmp_path, source, fake_gh, monkeypatch)
    entry = replace(ctx._record.replayed[-1], original_bookmark=None)
    record = replace(ctx._record, replayed=(entry,))
    _records.save_candidate(ctx._paths, ctx._project.name, record)
    _publish.publish(replace(ctx, _record=record))
    derived = _records.load_publication(ctx._paths, ctx._project.name, entry.new)
    assert derived is not None
    assert derived.pinned is None
    assert derived.source_bookmark is None
    assert derived.derivation is not None
    assert derived.derivation.original_source == originals[-1].pinned.commit
    assert _remote_ref(source, _source.bookmark_ref(ctx._project.name, entry.new)) is None


@pytest.mark.slow
@pytest.mark.parametrize('fault', ['refresh', 'history'])
def test_source_tag_failure_warns_after_confirmed_publication(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _with_source_tags(_context(tmp_path, source, version='1.0.0'))

    def _fail(*_args: object) -> Never:
        if fault == 'history':
            raise PubanonError('publication history unavailable')
        raise OSError('source tag write denied')

    monkeypatch.setattr(
        _history, 'publications' if fault == 'history' else 'refresh_source_tags', _fail
    )
    captured = StringIO()
    with redirect_stdout(captured):
        _publish.publish(ctx)

    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit
    assert _records.load_candidate(ctx._paths, ctx._project.name) is None
    assert _records.load_publication(ctx._paths, ctx._project.name, ctx._record.commit) is not None
    assert _remote_ref(source, 'refs/tags/pubanon/v1.0.0') is None
    warning = captured.getvalue()
    assert 'source tags' in warning
    assert (
        'publication history unavailable' if fault == 'history' else 'source tag write denied'
    ) in warning


def test_source_tag_waits_for_confirmed_publication(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _with_source_tags(_context(tmp_path, source, version='1.0.0'))

    def _reject_push(*_args: object) -> Never:
        raise PubanonError('transport rejected')

    monkeypatch.setattr(_host, 'push', _reject_push)
    with pytest.raises(PubanonError, match='transport rejected'):
        _publish.publish(ctx)

    assert _remote_ref(host, 'refs/heads/main') is None
    assert _remote_ref(source, 'refs/tags/pubanon/v1.0.0') is None
    assert _records.load_candidate(ctx._paths, ctx._project.name) is not None


@pytest.mark.parametrize(
    'failure',
    [
        'absent',
        'malformed',
        'coordinates',
        'public',
        'refs',
        'default-branch',
        'missing',
        'pending',
        'unacceptable',
        'ambiguous',
        'unreadable',
    ],
)
def test_staged_publication_failure_preserves_candidate_before_preview_or_transport(
    failure: str,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tmp_path = fake_gh.parent
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    ctx = replace(
        ctx,
        _project=replace(
            ctx._project, publication_route='staged', required_checks=('build', 'test')
        ),
    )
    _discard.discard(ctx._paths, ctx._project.name, force=True)
    record = _candidate.build_candidate(
        ctx._paths,
        ctx._global_config,
        ctx._project,
        _candidate.CandidateInputs(
            read_snapshot(source, ctx._record.pinned.commit),
            ctx._record.pinned,
            None,
            None,
        ),
    )
    ctx = replace(ctx, _record=record)
    stage_host = _configure_staged_candidate(
        ctx,
        fake_gh,
        monkeypatch,
        failure=failure,
    )
    stage_path = ctx._paths.data_dir / 'projects' / ctx._project.name / 'stage.toml'
    if failure == 'absent':
        stage_path.unlink()
    elif failure == 'malformed':
        stage_path.write_text('malformed staging record')
    elif failure == 'coordinates':
        _save_stage(
            ctx._paths, ctx._project.name, _stage.StageRecord(ctx._record.commit, 'other', 'repo')
        )
    elif failure == 'refs':
        _git_command(stage_host, 'update-ref', '-d', 'refs/heads/main')
    reads: list[str] = []

    def _check(
        _coordinates: tuple[str, str], commit: str, name: str, *, timeout: float | None = None
    ) -> _host.CheckVerdict:
        assert commit == ctx._record.commit
        assert _coordinates == (ctx._project.owner, _config.stage_repository(ctx._project))
        del timeout
        reads.append(name)
        if failure == 'unreadable':
            raise PubanonError('cannot read required check')
        assert failure in ('missing', 'pending', 'unacceptable', 'ambiguous')
        return _host.CheckVerdict(name, 'success' if name == 'build' else failure, 'check detail')

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail('staging prerequisite failure reached preservation, preview, or transport')

    monkeypatch.setattr(_host, 'check_result', _check)
    monkeypatch.setattr(_publish, '_preserve_source', _forbidden)
    monkeypatch.setattr(_publish, '_review_publication', _forbidden)
    monkeypatch.setattr(_host, 'push', _forbidden)
    capsys.readouterr()
    with pytest.raises(PubanonError) as error:
        _publish.publish(ctx)
    if failure in {'missing', 'pending', 'unacceptable', 'ambiguous'}:
        assert {
            'unacceptable': 'failed',
            'ambiguous': 'could not be determined',
            'missing': 'not found',
        }.get(failure, failure) in error.value.render()
        assert 'pubanon stage watch' in error.value.render()
        assert reads == ['build', 'test']
    elif failure == 'unreadable':
        assert 'cannot read required check' in error.value.render()
    else:
        expected = {
            'absent': 'no staged commit is recorded',
            'malformed': 'invalid stage record',
            'coordinates': 'configured staging coordinates differ',
            'public': 'private',
            'refs': 'staging tip does not match',
            'default-branch': 'default branch is not main',
        }[failure]
        assert expected in error.value.render()
        assert reads == []
    assert _records.load_candidate(ctx._paths, ctx._project.name) == ctx._record
    assert _remote_ref(host, 'refs/heads/main') is None
    assert 'Publish prepared snapshot' not in capsys.readouterr().out


@pytest.mark.skipif(os.name == 'nt', reason='POSIX symlink target bytes')
@pytest.mark.parametrize('edit_target', [False, True])
def test_publish_reuses_raw_symlink_inspection_and_refuses_actual_edit(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    edit_target: bool,
) -> None:
    source = make_repo(
        files={'file': 'root\n', 'relative/file': 'nested\n'},
        symlinks={'dot-link': './file', 'double-link': 'relative//file'},
    )
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    inspection = _verification.materialize_inspect(
        ctx._paths,
        ctx._project.name,
        _verification.prove_objects(ctx._paths, ctx._project.name, ctx._record),
    )
    assert os.fsencode(os.readlink(inspection / 'dot-link', dir_fd=None)) == b'./file'
    assert os.fsencode(os.readlink(inspection / 'double-link', dir_fd=None)) == b'relative//file'
    if edit_target:
        _filesystem.chmod(inspection, 0o700)
        (inspection / 'dot-link').unlink()
        (inspection / 'dot-link').symlink_to('file')
        with pytest.raises(PubanonError, match='review copy does not match'):
            _publish.publish(ctx)
        assert _remote_ref(host, 'refs/heads/main') is None
        return

    _publish.publish(ctx)

    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit
    assert _git_command(host, 'show', 'main:dot-link') == b'./file'
    assert _git_command(host, 'show', 'main:double-link') == b'relative//file'


@pytest.mark.slow
def test_readonly_inspection_survives_publish(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    inspection = _verification.materialize_inspect(
        ctx._paths,
        ctx._project.name,
        _verification.prove_objects(ctx._paths, ctx._project.name, ctx._record),
    )
    store = _store.store_path(ctx._paths, ctx._project.name)
    original_commit = _git_command(store, 'cat-file', 'commit', ctx._record.commit)
    _publish.publish(ctx)
    assert _git_command(host, 'show', 'main:README.md') == b'public\n'
    assert _git_command(store, 'cat-file', 'commit', ctx._record.commit) == original_commit
    assert _git_command(store, 'show', f'{ctx._record.commit}:README.md') == b'public\n'
    assert (inspection / 'README.md').read_text() == 'public\n'
    assert_private_mode(inspection, 0o500)
    assert_private_mode(inspection / 'README.md', 0o400)


@pytest.mark.parametrize(
    ('tags', 'expected'),
    [
        (('latest',), 'pubanon package watch'),
        (
            ('v1.9.0', 'v1.10.0', 'z-release'),
            'pubanon package watch --version 1.10.0',
        ),
    ],
)
def test_replacement_completion_selects_latest_strict_release_version(
    tmp_path: Path,
    tags: tuple[str, ...],
    expected: str,
) -> None:
    ctx = _literal_context(tmp_path)
    tip = ctx._record.commit
    record = replace(
        ctx._record,
        replaces=tip,
        replace_scope='one',
        tip=tip,
        poststate_tags=dict.fromkeys(tags, tip),
    )
    project = replace(
        ctx._project,
        required_checks=(),
        package=_config.PackageObservation('pypi', 'demo', ()),
    )

    assert _publish.publication_next_action(project, record) == expected


@pytest.mark.slow
@pytest.mark.parametrize('visibility', ['private', 'public'])
def test_replacement_preview_names_ref_changes_warning_and_single_confirmation(
    visibility: str,
    *,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    _host_path, ctx, originals = _rewrite_fixture(tmp_path, source, fake_gh, monkeypatch)
    assert ctx._record.tip is not None
    destination = replace(
        _host.verify_destination(ctx._paths, ctx._global_config, ctx._project),
        visibility=visibility,
    )
    questions: list[str] = []

    def _approve(question: str) -> bool:
        questions.append(question)
        return True

    monkeypatch.setattr(_publish, 'confirm', _approve)
    rendered = StringIO()
    with redirect_stdout(rendered):
        _publish._review_publication(
            _publish_mode(ctx, 'interactive'),
            _publish._Verified(
                destination, _host.RemoteRefs(ctx._record.prestate_main, ctx._record.prestate_tags)
            ),
            _page_plan(ctx),
        )
    preview = re.sub(r': +', ': ', rendered.getvalue())
    assert preview.startswith('Replace published snapshot\n')
    assert f'Commit: {originals[1].commit[:12]} -> {ctx._record.commit[:12]}' in preview
    assert 'Later snapshots: 1 replayed' in preview
    assert f'{originals[-1].commit[:12]} -> {ctx._record.tip[:12]}' in preview
    assert 'Scope:' in preview
    assert 'Moved tags:' in preview
    assert f'Visibility: {visibility}' in preview
    assert (
        'Existing clones may retain old history; GitHub Releases and uploaded '
        'packages are unchanged.' in preview
    )
    assert 'This changes branches and tags; old commits and GitHub activity' in preview
    assert questions == ['Publish this replacement?']

    detailed = StringIO()
    with redirect_stdout(detailed), verbose_context(enabled=True):
        _publish._review_publication(
            ctx,
            _publish._Verified(
                destination, _host.RemoteRefs(ctx._record.prestate_main, ctx._record.prestate_tags)
            ),
            _page_plan(ctx),
        )
    assert f'Commit: {originals[1].commit} -> {ctx._record.commit}' in re.sub(
        r': +', ': ', detailed.getvalue()
    )
    assert f'Replayed commit: {originals[-1].commit} -> {ctx._record.tip}' in re.sub(
        r': +', ': ', detailed.getvalue()
    )
    assert 'Review copy: ' in re.sub(r': +', ': ', detailed.getvalue())

    latest = replace(
        ctx._record,
        replaces=originals[-1].commit,
        replayed=(),
        tip=ctx._record.commit,
        poststate_tags={**ctx._record.prestate_tags, 'v1.0.2': ctx._record.commit},
    )
    compact = StringIO()
    with redirect_stdout(compact):
        _publish._replacement_review(
            replace(ctx, _record=latest),
            _publish._Verified(
                destination, _host.RemoteRefs(latest.prestate_main, latest.prestate_tags)
            ),
            _page_plan(ctx),
        )
    compact_output = re.sub(r': +', ': ', compact.getvalue())
    assert 'Replace published snapshot' in compact_output
    assert 'Repository: public-owner/public-repo' in compact_output
    assert f'Visibility: {visibility}' in compact_output
    assert 'Version: v1.0.2' in compact_output
    assert f'Commit: {originals[-1].commit[:12]} -> {ctx._record.commit[:12]}' in compact_output
    assert 'Updates: main, v1.0.2' in compact_output
    assert 'Later snapshots:' not in compact_output
    assert 'Replayed commit:' not in compact_output
    assert 'This changes branches and tags; old commits and GitHub activity' in compact_output
    assert 'Actions logs, and artifacts may remain.' in compact_output
    assert (
        'Existing clones may retain old history; GitHub Releases and uploaded '
        'packages are unchanged.' in compact_output
    )
    if visibility == 'public':
        assert 'Public forks and caches may also retain the old commits.' in compact_output
    else:
        assert 'Public forks and caches may also retain the old commits.' not in compact_output


@pytest.mark.slow
@pytest.mark.parametrize('version', [None, '1.0.0'])
def test_replacement_push_moves_existing_tag_and_preserves_history(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    version: str | None,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    host, first, ctx = _replacement_fixture(tmp_path, source, fake_gh, monkeypatch, version)
    old_record = _records.load_publication(ctx._paths, ctx._project.name, first.commit)
    assert old_record is not None

    _publish.publish(ctx)

    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit
    assert _git_command(host, 'rev-list', '--parents', 'main').decode().splitlines() == [
        ctx._record.commit
    ]
    if version:
        assert _remote_ref(host, 'refs/tags/v1.0.0') == ctx._record.commit
    assert _git_command(host, 'show', 'main:README.md') == b'corrected publication\n'
    assert _records.load_candidate(ctx._paths, ctx._project.name) is None
    saved = _records.load_publication(ctx._paths, ctx._project.name, ctx._record.commit)
    assert saved is not None
    assert saved.replaces == first.commit
    assert _records.load_publication(ctx._paths, ctx._project.name, first.commit) == old_record
    assert (
        _remote_ref(
            _store.store_path(ctx._paths, ctx._project.name),
            _records.publication_ref(first.commit),
        )
        == first.commit
    )
    assert old_record.source_bookmark is not None
    assert old_record.pinned is not None
    assert (
        _source.source_recovery(old_record.source_bookmark, old_record.pinned.commit).status
        == 'protected'
    )
    assert _git_command(host, 'for-each-ref', '--format=%(refname)').decode().splitlines() == (
        ['refs/heads/main', 'refs/tags/v1.0.0'] if version else ['refs/heads/main']
    )


@pytest.mark.slow
@pytest.mark.parametrize('landed', [False, True])
def test_replacement_resolves_lost_readback_without_repeating_push(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    landed: bool,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    host, first, ctx = _replacement_fixture(tmp_path, source, fake_gh, monkeypatch, '1.0.0')
    real_push = _host.push
    calls: list[list[str]] = []

    def _push(dest: _host.Destination, store: Path, args: list[str]) -> None:
        calls.append(args)
        if landed:
            real_push(dest, store, args)
        else:
            raise PubanonError('simulated transport failure')

    monkeypatch.setattr(_host, 'push', _push)
    with (
        _failed_readback(monkeypatch),
        pytest.raises(PubanonError, match='the outcome of the last publication is unknown'),
    ):
        _publish.publish(ctx)
    retained = _records.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert retained.state == _candidate.STATE_UNKNOWN_OUTCOME
    outcome = _publish.resolve(replace(ctx, _record=retained))
    assert outcome == ('published' if landed else 'not-sent')
    assert len(calls) == 1
    assert f'--force-with-lease=refs/heads/main:{first.commit}' in calls[0]
    assert f'--force-with-lease=refs/tags/v1.0.0:{first.commit}' in calls[0]
    assert '--atomic' in calls[0]
    expected = ctx._record.commit if landed else first.commit
    assert _remote_ref(host, 'refs/heads/main') == expected
    assert _remote_ref(host, 'refs/tags/v1.0.0') == expected


@pytest.mark.parametrize('matches_tip', [False, True])
def test_replacement_staging_evidence_must_cover_resulting_tip(
    matches_tip: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _literal_context(tmp_path)
    correction = ctx._record.commit
    tip = 'b' * 40 if correction != 'b' * 40 else 'c' * 40
    replacement = replace(ctx._record, replaces='a' * 40, tip=tip, poststate_tags={})
    project = replace(ctx._project, publication_route='staged', required_checks=('build',))
    ctx = replace(ctx, _record=replacement, _project=project)
    observed: list[str] = []
    stage = _stage.StageRecord(tip if matches_tip else correction, project.owner, 'staging')
    _save_stage(ctx._paths, project.name, stage)

    def _target(*_args: object) -> tuple[_host.Destination, _stage.StageRecord]:
        return (
            _host.Destination(
                'github', project.owner, 'staging', 'unused', 'release-account', 'private'
            ),
            stage,
        )

    def _checks(
        _dest: _host.Destination, commit: str, _checks: object
    ) -> list[_host.CheckVerdict]:
        observed.append(commit)
        return [_host.CheckVerdict('build', 'success', 'success')]

    monkeypatch.setattr(_stage, 'verify_staged_target', _target)
    monkeypatch.setattr(_watch, 'ci_verdicts', _checks)
    if matches_tip:
        _publish._verify_publication_route(ctx)
        assert observed == [tip]
    else:
        with pytest.raises(PubanonError, match='staging evidence') as error:
            _publish._verify_publication_route(ctx)
        assert error.value.expected == tip
        assert observed == []


@pytest.mark.slow
@pytest.mark.parametrize('tags', [('latest',), ('latest', 'v1.2.3')])
def test_replay_nonrelease_tags_recover_finalization_without_another_push(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    tags: tuple[str, ...],
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    host, ctx, originals = _rewrite_fixture(tmp_path, source, fake_gh, monkeypatch)
    store = _store.candidate_store_path(ctx._paths, ctx._project.name)
    before = dict(ctx._record.prestate_tags)
    after = dict(_records.candidate_poststate(ctx._record)[1])
    del before['v1.0.2']
    del after['v1.0.2']
    for repo in (store, host):
        _git_command(repo, 'update-ref', '-d', 'refs/tags/v1.0.2')
        for tag in tags:
            _git_command(repo, 'update-ref', f'refs/tags/{tag}', originals[-1].commit)
    for tag in tags:
        before[tag] = originals[-1].commit
        after[tag] = _records.candidate_tip(ctx._record)
    ctx = replace(ctx, _record=replace(ctx._record, prestate_tags=before, poststate_tags=after))
    _records.save_candidate(ctx._paths, ctx._project.name, ctx._record)
    real_push = _host.push
    real_save = _records.save_publication
    pushes: list[list[str]] = []
    failed = False

    def _push(dest: _host.Destination, store: Path, args: list[str]) -> None:
        pushes.append(args)
        real_push(dest, store, args)

    def _save(paths: Paths, project: str, record: _records.PublicationRecord) -> None:
        nonlocal failed
        if record.commit == ctx._record.tip and not failed:
            failed = True
            raise OSError('record write interrupted')
        real_save(paths, project, record)

    monkeypatch.setattr(_host, 'push', _push)
    monkeypatch.setattr(_records, 'save_publication', _save)
    with pytest.raises(PubanonError, match='Local finalization is incomplete'):
        _publish.publish(ctx)
    assert failed
    retained = _records.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    tip = _records.candidate_tip(retained)
    assert _verification.confirmed_publication(ctx._paths, ctx._project.name, retained)
    assert _candidate.publication_archive(ctx._paths, ctx._project.name, tip)
    assert _records.load_publication(ctx._paths, ctx._project.name, tip) is None
    with pytest.raises(PubanonError, match='confirmed publication cannot be discarded'):
        _discard.discard(ctx._paths, ctx._project.name, force=True)

    assert _publish.resolve(replace(ctx, _record=retained)) == 'published'

    assert len(pushes) == 1
    assert _records.load_candidate(ctx._paths, ctx._project.name) is None
    saved = _records.load_publication(ctx._paths, ctx._project.name, tip)
    assert saved is not None
    assert saved.tag_intent == ('v1.2.3' if 'v1.2.3' in tags else None)
    poststate = _records.candidate_poststate(retained)
    assert _candidate.published_refs(ctx._paths, ctx._project.name) == poststate
    assert _remote_ref(host, 'refs/heads/main') == tip
    for tag, commit in poststate[1].items():
        assert _remote_ref(host, f'refs/tags/{tag}') == commit


@pytest.mark.slow
@pytest.mark.parametrize('sent', [False, True])
def test_resolve_classifies_prestate_and_pushed_state(
    sent: bool,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _publish._preserve_source(_context(tmp_path, source))
    unknown = replace(ctx._record, state=_candidate.STATE_UNKNOWN_OUTCOME)
    _candidate.save_candidate(ctx._paths, ctx._project.name, unknown)
    if sent:
        store = _store.store_path(ctx._paths, ctx._project.name)
        _git_command(host, 'fetch', str(store), f'{unknown.commit}:refs/heads/main')

    outcome = _publish.resolve(replace(ctx, _record=unknown))

    assert outcome == ('published' if sent else 'not-sent')
    current = _records.load_candidate(ctx._paths, ctx._project.name)
    if sent:
        assert current is None
        assert (
            _remote_ref(_store.store_path(ctx._paths, ctx._project.name), 'refs/heads/main')
            == unknown.commit
        )
    else:
        assert current is not None
        assert current.state == _candidate.STATE_RETAINED


@pytest.mark.parametrize('structured', [False, True])
def test_resolve_interrupt_before_readback_retains_unknown_outcome(
    publication_cli: tuple[Path, _publish.PublishContext],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    structured: bool,
) -> None:
    _host_path, ctx = publication_cli
    _candidate.save_candidate(
        ctx._paths, ctx._project.name, replace(ctx._record, state=_candidate.STATE_UNKNOWN_OUTCOME)
    )

    def _interrupt_read(*_args: object) -> _host.RemoteRefs:
        if structured:
            raise OperationCanceled('operation cancelled')
        raise KeyboardInterrupt

    with monkeypatch.context() as fault:
        fault.setattr(_host, 'ls_remote', _interrupt_read)
        assert _cli.main(['publish', '--project', ctx._project.name, '--resolve']) == 130
    error = capsys.readouterr().err
    assert error.startswith('error: the outcome of the last publication is unknown')
    assert 'Canceled.' not in error
    assert f'pubanon publish --project={ctx._project.name} --resolve' in error
    assert 'Traceback' not in error
    assert _cli.main(['publish', '--project', ctx._project.name, '--resolve']) == 0
    record = _records.load_candidate(ctx._paths, ctx._project.name)
    assert record is not None
    assert record.state == _candidate.STATE_RETAINED


@pytest.mark.slow
@pytest.mark.parametrize('race', ['leased-tag', 'deletion-refused', 'mixed'])
def test_rewrite_atomic_transport_refuses_races_and_mixed_readback(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    race: str,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    host, ctx, originals = _rewrite_fixture(
        tmp_path,
        source,
        fake_gh,
        monkeypatch,
        selection=None if race == 'deletion-refused' else 1,
    )
    if race == 'deletion-refused':
        hook = host / 'hooks' / 'update'
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(
            '#!/bin/sh\ncase "$3" in 000*) exit 1;; esac\n',
            encoding='utf-8',
            newline='\n',
        )
        hook.chmod(0o755)
    real_push = _host.push
    calls: list[list[str]] = []

    def _push(dest: _host.Destination, store: Path, args: list[str]) -> None:
        calls.append(args)
        if race == 'leased-tag':
            _git_command(host, 'update-ref', 'refs/tags/v1.0.1', originals[0].commit)
        elif race == 'mixed':
            _git_command(
                host,
                'fetch',
                str(store),
                f'+{_records.candidate_tip(ctx._record)}:refs/heads/main',
            )
            return
        real_push(dest, store, args)

    monkeypatch.setattr(_host, 'push', _push)
    with pytest.raises(PubanonError):
        _publish.publish(ctx)
    retained = _records.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    if race == 'deletion-refused':
        assert retained.state == _records.STATE_RETAINED
        assert _remote_ref(host, 'refs/tags/v1.0.0') == originals[0].commit
    else:
        assert retained.state == _records.STATE_UNKNOWN_OUTCOME
        with pytest.raises(PubanonError, match='indeterminate'):
            _publish.resolve(replace(ctx, _record=retained))
    if race != 'mixed':
        assert _remote_ref(host, 'refs/heads/main') == originals[-1].commit
    assert _remote_ref(host, 'refs/tags/v1.0.2') == originals[-1].commit
    assert len(calls) == 1
    assert not _verification.confirmed_publication(ctx._paths, ctx._project.name, retained)


@pytest.mark.slow
@pytest.mark.parametrize('boundary', ['locator', 'bookmark', 'old-tip'])
def test_rewrite_preservation_failure_never_pushes_and_retains_locators(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    host, ctx, _ = _rewrite_fixture(tmp_path, source, fake_gh, monkeypatch)
    real_save, real_bookmark = _publish.save_candidate, _source.ensure_bookmark

    def _save(paths: Paths, name: str, record: _records.CandidateRecord) -> None:
        if any(entry.source_bookmark is not None for entry in record.replayed):
            raise OSError('locator persistence refused')
        real_save(paths, name, record)

    def bookmark(locator: _source.SourceBookmark, oid: str) -> None:
        retained = _records.load_candidate(ctx._paths, ctx._project.name)
        assert retained is not None
        if locator.refname.endswith(_records.candidate_tip(ctx._record)):
            assert retained.replayed[-1].source_bookmark == locator
            raise PubanonError('bookmark creation refused')
        real_bookmark(locator, oid)

    def _forbidden(*_args: object) -> None:
        pytest.fail('preservation failure must not send a push')

    def _archive(*_args: object) -> None:
        raise PubanonError('archive creation refused')

    monkeypatch.setattr(_host, 'push', _forbidden)
    if boundary == 'locator':
        monkeypatch.setattr(_publish, 'save_candidate', _save)
    elif boundary == 'bookmark':
        monkeypatch.setattr(_source, 'ensure_bookmark', bookmark)
    else:
        monkeypatch.setattr(_history, 'preserve_previous_tip', _archive)
    with pytest.raises(PubanonError) as error:
        _publish.publish(ctx)
    assert 'No push was attempted' in error.value.render()
    retained = _records.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert retained.state == _records.STATE_RETAINED
    assert (retained.replayed[-1].source_bookmark is None) == (boundary == 'locator')
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.prestate_main


@pytest.mark.slow
@pytest.mark.parametrize(
    'boundary', ['tip-record', 'correction-record', 'derived-record', 'refs', 'unlink']
)
def test_rewrite_resolve_retains_tip_proof_after_interruption_and_remote_drift(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    host, ctx, _ = _rewrite_fixture(
        tmp_path,
        source,
        fake_gh,
        monkeypatch,
        selection=0 if boundary == 'derived-record' else 1,
    )
    real_save, real_git, real_unlink = _records.save_publication, _publish.run_git, Path.unlink
    pushes: list[list[str]] = []
    real_push = _host.push

    def _push(dest: _host.Destination, store: Path, args: list[str]) -> None:
        pushes.append(args)
        real_push(dest, store, args)

    def _save(paths: Paths, project: str, record: _records.PublicationRecord) -> None:
        interrupted = {
            'tip-record': ctx._record.tip,
            'correction-record': ctx._record.commit,
            'derived-record': ctx._record.replayed[0].new,
        }[boundary]
        if record.commit == interrupted:
            raise OSError('record interrupted')
        real_save(paths, project, record)

    def _git(
        args: list[str], *, cwd: Path, input_bytes: bytes | None = None, check: bool = True
    ) -> GitResult:
        if args[2] == 'update-ref':
            raise OSError('refs interrupted')
        return real_git(args, cwd=cwd, input_bytes=input_bytes, check=check)

    def _unlink(path: Path, missing_ok: bool = False) -> None:
        if path == _record_path(ctx):
            raise OSError('candidate removal interrupted')
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(_host, 'push', _push)
    with monkeypatch.context() as fault:
        if boundary.endswith('record'):
            fault.setattr(_records, 'save_publication', _save)
        elif boundary == 'refs':
            fault.setattr(_publish, 'run_git', _git)
        else:
            fault.setattr(Path, 'unlink', _unlink)
        with pytest.raises(PubanonError, match='Local finalization is incomplete'):
            _publish.publish(ctx)
    retained = _records.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert _verification.confirmed_publication(ctx._paths, ctx._project.name, retained)
    assert _candidate.publication_archive(
        ctx._paths, ctx._project.name, _records.candidate_tip(retained)
    )
    _git_command(host, 'update-ref', '-d', 'refs/heads/main')
    changed_project = replace(ctx._project, source_path=tmp_path / 'missing', repo='changed')
    with pytest.raises(PubanonError, match='must not be retried'):
        _publish.resolve(replace(ctx, _record=retained, _project=changed_project))
    assert len(pushes) == 1
    assert _records.load_candidate(ctx._paths, ctx._project.name) is None
    assert _candidate.published_refs(
        ctx._paths, ctx._project.name
    ) == _records.candidate_poststate(retained)


@pytest.mark.slow
@pytest.mark.parametrize('all_history', [False, True])
def test_rewrite_transaction_preserves_old_chain_and_moves_only_changed_tags(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    all_history: bool,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    host, ctx, originals = _rewrite_fixture(
        tmp_path, source, fake_gh, monkeypatch, selection=None if all_history else 1
    )
    before = (
        _git_command(source, 'status', '--porcelain'),
        _git_command(source, 'rev-parse', 'HEAD'),
        _git_command(source, 'for-each-ref', 'refs/heads', 'refs/tags'),
        _git_command(source, 'ls-files', '--stage'),
        tuple(
            (path.name, path.read_bytes()) for path in sorted(source.iterdir()) if path.is_file()
        ),
    )
    store = _store.store_path(ctx._paths, ctx._project.name)
    old_bytes = _git_command(store, 'cat-file', 'commit', originals[0].commit)
    if all_history:
        # A recovered tip has no record or archive; pretransport preservation must retain it.
        _records.publication_path(ctx._paths, ctx._project.name, originals[-1].commit).unlink()
        _git_command(store, 'update-ref', '-d', _records.publication_ref(originals[-1].commit))
    calls: list[list[str]] = []
    real_push = _host.push

    def _push(dest: _host.Destination, path: Path, args: list[str]) -> None:
        assert _candidate.publication_archive(ctx._paths, ctx._project.name, originals[-1].commit)
        calls.append(args)
        real_push(dest, path, args)

    monkeypatch.setattr(_host, 'push', _push)
    _publish.publish(ctx)
    tip, tags = _records.candidate_poststate(ctx._record)
    assert _remote_ref(host, 'refs/heads/main') == tip
    assert _candidate.published_refs(ctx._paths, ctx._project.name) == (tip, tags)
    assert _git_command(store, 'cat-file', 'commit', originals[0].commit) == old_bytes
    assert before == (
        _git_command(source, 'status', '--porcelain'),
        _git_command(source, 'rev-parse', 'HEAD'),
        _git_command(source, 'for-each-ref', 'refs/heads', 'refs/tags'),
        _git_command(source, 'ls-files', '--stage'),
        tuple(
            (path.name, path.read_bytes()) for path in sorted(source.iterdir()) if path.is_file()
        ),
    )
    assert len(calls) == 1
    if all_history:
        assert tags == {'v1.0.2': tip}
        assert ':refs/tags/v1.0.0' in calls[0]
        assert ':refs/tags/v1.0.1' in calls[0]
        assert _git_command(host, 'rev-list', '--parents', 'main').decode().strip() == tip
    else:
        assert all('refs/tags/v1.0.0' not in arg for arg in calls[0])
        assert _git_command(host, 'rev-parse', 'main~2').decode().strip() == originals[0].commit
        derived = _records.load_publication(ctx._paths, ctx._project.name, tip)
        assert derived is not None
        assert derived.pinned is None
        assert derived.derivation == _records.Derivation(
            originals[-1].commit,
            originals[-1].pinned.commit,
            ctx._record.commit,
            (),
        )
        assert derived.source_bookmark is not None
        assert originals[-1].source_bookmark is not None
        assert derived.source_bookmark.refname != originals[-1].source_bookmark.refname
        assert (
            _source.source_recovery(derived.source_bookmark, originals[-1].pinned.commit).status
            == 'protected'
        )


@pytest.mark.parametrize('drift', ['advanced', 'rewound', 'diverged', 'deleted'])
def test_source_drift_warns_without_changing_snapshot(
    drift: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _publish_mode(
        _drifted_context(drift, tmp_path, fake_gh, make_repo, monkeypatch), 'interactive'
    )
    captured = StringIO()
    with redirect_stdout(captured):
        _verify_for_publish(ctx)
    assert captured.getvalue() == 'Warning: source has changed since snapshot was prepared.\n'
    assert _records.load_candidate(ctx._paths, ctx._project.name) == ctx._record


@pytest.mark.slow
def test_sync_acknowledgment_displays_identity_and_hygiene_in_one_path(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    identity = 'historical-private-segment'
    hygiene = 'historical-hygiene-segment'
    path = f'docs/{identity}-{hygiene}.txt'
    source = make_repo(files={'README.md': 'first\n', path: 'public\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    first = _context(tmp_path, source, version='1.0.0')
    _publish.publish(first)
    _git_command(source, 'rm', '-q', '--', path)
    _second_source_commit(source)
    second = _context(tmp_path, source, version='2.0.0')
    _publish.publish(second)
    _git_command(host, 'update-ref', '-d', 'refs/heads/main')
    _git_command(host, 'update-ref', '-d', 'refs/tags/v1.0.0')
    _git_command(host, 'update-ref', '-d', 'refs/tags/v2.0.0')
    project = replace(
        second._project,
        extra_terms=(identity,),
        hygiene_terms=(hygiene,),
    )
    ctx = replace(second, _project=project)
    prompts: list[str] = []
    answers = [
        ('Continue despite this earlier exposure? [y/n] ', 'y'),
        ('Restore these branches and tags to public-owner/public-repo? [y/n] ', 'n'),
    ]

    def _answer(prompt: str) -> str:
        if not prompts:
            ordinary = capsys.readouterr().out
            assert identity in ordinary
            assert hygiene in ordinary
            assert 'item 1' in ordinary
        prompts.append(prompt)
        assert answers, f'unexpected prompt: {prompt}'
        expected, response = answers.pop(0)
        assert expected == prompt
        return response

    monkeypatch.setattr('builtins.input', _answer)
    capsys.readouterr()

    with pytest.raises(OperationCanceled, match=r'Canceled\. Nothing was pushed.'):
        _publish.sync(_sync_context(ctx, mode='interactive'))

    assert not answers
    output = capsys.readouterr().out
    assert prompts == [
        'Continue despite this earlier exposure? [y/n] ',
        'Restore these branches and tags to public-owner/public-repo? [y/n] ',
    ]
    assert 'from saved published history' in output


@pytest.mark.parametrize('status', [1, 7])
def test_sync_ancestry_failure_retains_classification_and_diagnostics(
    publication_cli: tuple[Path, _publish.PublishContext],
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    _host_path, ctx = publication_cli

    def _fail(args: list[str], **_kwargs: object) -> GitResult:
        return GitResult(status if args[2] == 'merge-base' else 0, b'', b'diagnostic\x1b\\path')

    monkeypatch.setattr(_publish, 'run_git', _fail)
    remote_commit = 'b' * 40
    with pytest.raises(PubanonError) as error:
        _verify_sync_prefix(
            _sync_context(ctx), ctx._record.commit, {}, _host.RemoteRefs(remote_commit, {})
        )
    assert str(error.value) == (
        'host main diverges from local published history'
        if status == 1
        else 'cannot classify host main ancestry'
    )
    assert error.value.fact == (None if status == 1 else r'exit status 7: diagnostic\x1b\\path')
    assert error.value.expected == ctx._record.commit
    assert error.value.actual == remote_commit
    assert error.value.recovery == 'pubanon repo verify'
    assert _records.load_candidate(ctx._paths, ctx._project.name) == ctx._record


@pytest.mark.slow
def test_sync_declined_confirmation_leaves_remote_unchanged(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _source_path, host, first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )
    _git_command(host, 'update-ref', 'refs/heads/main', first._record.commit)

    capsys.readouterr()

    def _decline(prompt: str) -> str:
        assert prompt == 'Restore these branches and tags to public-owner/public-repo? [y/n] '
        preview = capsys.readouterr().out
        assert f'{first._record.commit[:12]} -> {second._record.commit[:12]}' in preview
        assert 'Snapshots:       1' in preview
        assert 'Author:' not in preview
        assert second._record.message not in preview
        assert f'{second._project.owner}/{second._project.repo}' in preview
        assert _remote_ref(host, 'refs/heads/main') == first._record.commit
        return 'n'

    monkeypatch.setattr('builtins.input', _decline)

    with pytest.raises(OperationCanceled, match=r'Canceled\. Nothing was pushed.'):
        _publish.sync(_sync_context(second, mode='interactive'))

    assert _remote_ref(host, 'refs/heads/main') == first._record.commit


@pytest.mark.slow
def test_sync_empty_host_restores_full_history_and_tags(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _source_path, host, first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )
    _git_command(host, 'update-ref', '-d', 'refs/heads/main')
    _git_command(host, 'update-ref', '-d', 'refs/tags/v1.0.0')
    _git_command(host, 'update-ref', '-d', 'refs/tags/v2.0.0')
    capsys.readouterr()

    _publish.sync(_sync_context(second))

    assert _remote_ref(host, 'refs/heads/main') == second._record.commit
    assert _remote_ref(host, 'refs/tags/v1.0.0') == first._record.commit
    assert _remote_ref(host, 'refs/tags/v2.0.0') == second._record.commit
    assert _records.load_candidate(second._paths, second._project.name) is None
    output = capsys.readouterr().out
    assert f'empty -> {second._record.commit[:12]}' in output
    assert 'v1.0.0, v2.0.0' in output
    assert ':refs/' not in output


@pytest.mark.slow
def test_sync_equal_host_is_noop(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _source_path, _host_path, _first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )

    def _unexpected_push(*_args: object, **_kwargs: object) -> GitResult:
        pytest.fail('equal sync attempted a push')

    monkeypatch.setattr(_host, 'push', _unexpected_push)
    capsys.readouterr()

    _publish.sync(_sync_context(second))

    output = capsys.readouterr().out
    assert 'already matches saved published history' in output
    assert 'The final repository already matches saved published history.' in output


@pytest.mark.slow
@pytest.mark.parametrize(
    'history',
    [
        ('empty', 'Anonymous Publisher'),
        ('empty', 'anonymous@example.invalid'),
        ('behind', 'Anonymous Publisher'),
        ('behind', 'anonymous@example.invalid'),
    ],
)
def test_sync_newly_sensitive_anonymous_identity_requires_explicit_acknowledgment(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    history: tuple[str, str],
) -> None:
    host_state, exposure = history
    _source_path, host, first, second = _published_history(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    if host_state == 'empty':
        for ref in ('refs/heads/main', 'refs/tags/v1.0.0', 'refs/tags/v2.0.0'):
            _git_command(host, 'update-ref', '-d', ref)
    else:
        _git_command(host, 'update-ref', 'refs/heads/main', first._record.commit)
        _git_command(host, 'update-ref', '-d', 'refs/tags/v2.0.0')
    before = _git_command(host, 'for-each-ref', '--format=%(objectname) %(refname)')
    project = replace(second._project, extra_terms=(exposure,))
    _config.save_project(second._paths, project)
    ctx = replace(second, _project=project)

    def _unexpected_prompt(_prompt: str) -> str:
        pytest.fail('unattended synchronization requested acknowledgment')

    monkeypatch.setattr('builtins.input', _unexpected_prompt)
    with pytest.raises(PubanonError, match='--yes cannot acknowledge historical exposure'):
        _publish.sync(_sync_context(ctx))

    assert _git_command(host, 'for-each-ref', '--format=%(objectname) %(refname)') == before
    assert _config.load_project(ctx._paths, project.name).acknowledged_exposure == ''
    prompts: list[str] = []
    expected = [
        ('Continue despite this earlier exposure? [y/n] ', 'y'),
        ('Restore these branches and tags to public-owner/public-repo? [y/n] ', 'y'),
    ]
    output_buffer = StringIO()

    def _accept(prompt: str) -> str:
        assert expected, f'unexpected prompt: {prompt}'
        expected_prompt, response = expected.pop(0)
        assert prompt == expected_prompt
        assert _git_command(host, 'for-each-ref', '--format=%(objectname) %(refname)') == before
        assert exposure in output_buffer.getvalue()
        prompts.append(prompt)
        return response

    monkeypatch.setattr('builtins.input', _accept)
    with redirect_stdout(output_buffer):
        _publish.sync(_sync_context(ctx, mode='interactive'))

    assert expected == []
    assert len(prompts) == 2
    assert _remote_ref(host, 'refs/heads/main') == second._record.commit
    assert _remote_ref(host, 'refs/tags/v2.0.0') == second._record.commit
    assert _config.load_project(ctx._paths, project.name).acknowledged_exposure
    output = output_buffer.getvalue()
    assert 'Historical exposure review\n' in output
    for version, record in (('v2.0.0', second._record), ('v1.0.0', first._record)):
        heading = f'Published snapshot: {version} ({record.commit[:12]})'
        assert output.count(heading) == 1
        group = output.split(heading)[1].split('Published snapshot:')[0]
        for field in ('author', 'committer'):
            assert group.count(f'  header {field} value\n') == 1
    assert exposure in output


@pytest.mark.slow
def test_sync_preserves_retained_candidate_byte_for_byte_then_publishes_it(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, host, first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )
    _second_source_commit(source, 'third\n')
    retained = _context(tmp_path, source)
    record_path = _record_path(retained)
    before = record_path.read_bytes()
    _git_command(host, 'update-ref', 'refs/heads/main', first._record.commit)
    _git_command(host, 'update-ref', '-d', 'refs/tags/v2.0.0')

    _publish.sync(_sync_context(retained))

    assert record_path.read_bytes() == before
    assert _records.load_candidate(retained._paths, retained._project.name) == retained._record
    assert _remote_ref(host, 'refs/heads/main') == second._record.commit

    _publish.publish(retained)

    assert _remote_ref(host, 'refs/heads/main') == retained._record.commit
    assert not record_path.exists()


@pytest.mark.slow
def test_sync_refuses_diverged_host_showing_both_tips_before_push(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source_path, host, _first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )
    tree = _git_command(host, 'mktree', input_bytes=b'').decode().strip()
    environment = {
        'GIT_AUTHOR_DATE': '1700000000 +0000',
        'GIT_AUTHOR_EMAIL': 'foreign@example.invalid',
        'GIT_AUTHOR_NAME': 'Foreign Author',
        'GIT_COMMITTER_DATE': '1700000000 +0000',
        'GIT_COMMITTER_EMAIL': 'foreign@example.invalid',
        'GIT_COMMITTER_NAME': 'Foreign Author',
    }
    foreign = (
        _git_command(
            host,
            'commit-tree',
            tree,
            env=environment,
            input_bytes=b'foreign\n',
        )
        .decode()
        .strip()
    )
    _git_command(host, 'update-ref', 'refs/heads/main', foreign)

    def _unexpected_push(*_args: object, **_kwargs: object) -> GitResult:
        pytest.fail('diverged sync attempted a push')

    monkeypatch.setattr(_host, 'push', _unexpected_push)

    with pytest.raises(PubanonError, match='diverg') as error:
        _publish.sync(_sync_context(second))

    rendered = error.value.render()
    assert foreign in rendered
    assert second._record.commit in rendered
    assert error.value.recovery == 'pubanon repo verify'


@pytest.mark.slow
def test_sync_refuses_mismatched_readback_with_complete_refs(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source_path, host, first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )
    _git_command(host, 'update-ref', 'refs/heads/main', first._record.commit)
    _git_command(host, 'update-ref', '-d', 'refs/tags/v2.0.0')
    local_main, local_tags = _candidate.published_refs(second._paths, second._project.name)
    assert local_main == second._record.commit
    prestate = _host.RemoteRefs(
        first._record.commit,
        {'v1.0.0': first._record.commit},
        (),
    )
    branch = 'private-readback-branch'
    mismatched = _host.RemoteRefs(
        local_main,
        local_tags,
        ((branch, first._record.commit),),
    )
    responses = (prestate, mismatched)
    reads: list[None] = []

    def _remote_refs(
        _destination: _host.Destination,
        _paths: Paths,
        _project: str,
    ) -> _host.RemoteRefs:
        if len(reads) == len(responses):
            pytest.fail('sync read the remote more than twice')
        response = responses[len(reads)]
        reads.append(None)
        return response

    monkeypatch.setattr(_host, 'ls_remote', _remote_refs)
    sensitive_term_ctx = replace(
        second, _project=replace(second._project, extra_terms=(first._record.commit, branch))
    )

    with pytest.raises(PubanonError, match='after restoration') as error:
        _publish.sync(_sync_context(sensitive_term_ctx))

    assert len(reads) == 2
    assert error.value.expected is not None
    assert error.value.actual is not None
    assert first._record.commit in error.value.expected
    assert branch in error.value.actual
    assert first._record.commit in error.value.actual
    assert error.value.recovery == 'rerun pubanon repo restore --to remote'


def test_sync_refuses_missing_local_published_main(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    _discard.discard(ctx._paths, ctx._project.name, force=False)

    with pytest.raises(PubanonError, match='local published history') as error:
        _publish.sync(_sync_context(ctx))

    assert error.value.recovery == 'pubanon repo restore --to local'


@pytest.mark.slow
def test_sync_refuses_structural_problem_outside_exact_range_before_preview(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _source_path, host, first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )
    store = _store.store_path(second._paths, second._project.name)
    tree = _git_command(store, 'rev-parse', f'{second._record.commit}^{{tree}}').decode().strip()
    invalid_root = _literal_history_commit(
        store,
        tree,
        author_name='Foreign Author',
        message=b'invalid root\n',
    )
    valid_tip = _literal_history_commit(
        store,
        tree,
        message=b'valid tip\n',
        parent=invalid_root,
    )
    _git_command(store, 'update-ref', 'refs/heads/main', valid_tip)
    _git_command(store, 'update-ref', 'refs/tags/v1.0.0', invalid_root)
    _git_command(store, 'update-ref', 'refs/tags/v2.0.0', valid_tip)
    _git_command(host, 'fetch', '-q', str(store), f'+{invalid_root}:refs/heads/main')
    _git_command(host, 'fetch', '-q', str(store), f'+{invalid_root}:refs/tags/v1.0.0')
    _git_command(host, 'update-ref', '-d', 'refs/tags/v2.0.0')
    remote_before = _remote_ref(host, 'refs/heads/main')

    def _unexpected_push(*_args: object, **_kwargs: object) -> GitResult:
        pytest.fail('structural sync failure attempted a push')

    monkeypatch.setattr(_host, 'push', _unexpected_push)
    capsys.readouterr()

    with pytest.raises(PubanonError, match='published history has identity problems'):
        _publish.sync(_sync_context(second))

    assert 'Synchronization preview:' not in capsys.readouterr().out
    assert _remote_ref(host, 'refs/heads/main') == remote_before
    assert remote_before == invalid_root
    assert first._record.commit != invalid_root


@pytest.mark.slow
def test_sync_refuses_unknown_outcome_candidate_naming_resolve(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _host_path, _first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )
    _second_source_commit(source, 'third\n')
    retained = _context(tmp_path, source)
    _candidate.save_candidate(
        retained._paths,
        retained._project.name,
        replace(retained._record, state=_candidate.STATE_UNKNOWN_OUTCOME),
    )

    def _refuse_push(*_args: object) -> None:
        pytest.fail('unknown publication outcome attempted a sync push')

    monkeypatch.setattr(_host, 'push', _refuse_push)
    with pytest.raises(PubanonError) as error:
        _publish.sync(_sync_context(second))

    assert error.value.recovery == 'pubanon publish --resolve'
    saved = _records.load_candidate(retained._paths, retained._project.name)
    assert saved is not None
    assert saved.state == _candidate.STATE_UNKNOWN_OUTCOME


@pytest.mark.slow
def test_sync_refuses_unknown_remote_tag(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source_path, host, _first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )
    _git_command(host, 'update-ref', 'refs/tags/foreign', second._record.commit)

    with pytest.raises(PubanonError, match='remote tag') as error:
        _publish.sync(_sync_context(second))

    rendered = error.value.render()
    assert 'foreign' in rendered
    assert 'absent' in rendered


@pytest.mark.slow
def test_sync_reports_hygiene_only_path_finding(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    term = 'new-hygiene-path'
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    first = _context(tmp_path, source, version='1.0.0')
    _publish.publish(first)
    path = f'docs/{term}.txt'
    (source / 'docs').mkdir()
    (source / path).write_text('public\n')
    _git_command(source, 'add', path)
    _git_command(
        source,
        '-c',
        'user.name=Test User',
        '-c',
        'user.email=test@example.invalid',
        'commit',
        '-qm',
        'second',
    )
    second = _context(tmp_path, source, version='2.0.0')
    _publish.publish(second)
    _git_command(host, 'update-ref', 'refs/heads/main', first._record.commit)
    _git_command(host, 'update-ref', '-d', 'refs/tags/v2.0.0')
    project = replace(second._project, hygiene_terms=(term,))
    capsys.readouterr()

    _publish.sync(_sync_context(replace(second, _project=project)))

    output = capsys.readouterr().out
    assert f'policy mismatch: path:docs/{term}.txt' in output
    assert term in output


@pytest.mark.slow
def test_sync_reports_unexpected_branch_before_noop_or_push(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    term = 'private-branch-canary'
    _source_path, host, _first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )
    _git_command(host, 'update-ref', f'refs/heads/{term}', second._record.commit)
    project = replace(second._project, extra_terms=(term,))
    ctx = replace(second, _project=project)

    def _unexpected_push(*_args: object, **_kwargs: object) -> GitResult:
        pytest.fail('branch-bearing sync attempted a push')

    monkeypatch.setattr(_host, 'push', _unexpected_push)

    with pytest.raises(PubanonError, match='unexpected branch') as error:
        _publish.sync(_sync_context(ctx))

    rendered = error.value.render()
    assert term in rendered


@pytest.mark.slow
@pytest.mark.parametrize('approve', [False, True])
def test_sync_sent_exposure_requires_interactive_acknowledgment(
    approve: bool,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exposure = 'historical-private-canary'
    _source_path, host, _first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
        first_content=f'{exposure}\n',
    )
    _git_command(host, 'update-ref', '-d', 'refs/heads/main')
    _git_command(host, 'update-ref', '-d', 'refs/tags/v1.0.0')
    _git_command(host, 'update-ref', '-d', 'refs/tags/v2.0.0')
    project = replace(second._project, extra_terms=(exposure,))
    _config.save_project(second._paths, project)
    ctx = replace(second, _project=project)
    prompts: list[str] = []
    expected = [
        'Continue despite this earlier exposure? [y/n] ',
        'Restore these branches and tags to public-owner/public-repo? [y/n] ',
    ]

    def _accept(prompt: str) -> str:
        assert expected, f'unexpected prompt: {prompt}'
        assert expected.pop(0) == prompt
        prompts.append(prompt)
        return 'y' if approve else 'n'

    monkeypatch.setattr('builtins.input', _accept)

    if not approve:
        with pytest.raises(OperationCanceled) as error:
            _publish.sync(_sync_context(ctx, mode='interactive'))
        assert str(error.value).startswith('Canceled.')
        assert 'pubanon publish' not in error.value.render()
        assert prompts == ['Continue despite this earlier exposure? [y/n] ']
        assert _git_command(host, 'for-each-ref') == b''
        assert _config.load_project(ctx._paths, project.name).acknowledged_exposure == ''
        return

    _publish.sync(_sync_context(ctx, mode='interactive'))

    assert not expected
    assert prompts == [
        'Continue despite this earlier exposure? [y/n] ',
        'Restore these branches and tags to public-owner/public-repo? [y/n] ',
    ]
    assert _config.load_project(ctx._paths, ctx._project.name).acknowledged_exposure


@pytest.mark.slow
def test_sync_strict_prefix_audits_and_pushes_exact_missing_range(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _source_path, host, first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )
    _git_command(host, 'update-ref', 'refs/heads/main', first._record.commit)
    _git_command(host, 'update-ref', '-d', 'refs/tags/v2.0.0')
    audits: list[
        tuple[
            tuple[str, ...],
            dict[str, str],
            frozenset[str],
            dict[str, str],
        ]
    ] = []
    real_audit = cast(
        'Callable[..., tuple[_audit.HistoryAudit, _audit.HistoryAudit]]',
        _audit.audit_history_pair,
    )
    real_push = _host.push
    pushed_args: list[str] = []

    def _audit_range(
        *args: object, **kwargs: object
    ) -> tuple[_audit.HistoryAudit, _audit.HistoryAudit]:
        audits.append(
            (
                cast('tuple[str, ...]', args[2]),
                dict(cast('dict[str, str]', args[3])),
                frozenset(cast('frozenset[str]', args[4])),
                dict(cast('dict[str, str]', args[5])),
            )
        )
        return real_audit(*args, **kwargs)

    def _record_push(
        destination: _host.Destination,
        cwd: Path,
        args: list[str],
    ) -> GitResult:
        pushed_args.extend(args)
        return real_push(destination, cwd, args)

    monkeypatch.setattr(_audit, 'audit_history_pair', _audit_range)
    monkeypatch.setattr(_host, 'push', _record_push)
    capsys.readouterr()

    _publish.sync(_sync_context(second))

    assert audits == [
        (
            (second._record.commit, first._record.commit),
            {
                'v1.0.0': first._record.commit,
                'v2.0.0': second._record.commit,
            },
            frozenset({second._record.commit}),
            {'v2.0.0': second._record.commit},
        ),
    ]
    assert pushed_args == [
        '--atomic',
        f'--force-with-lease=refs/heads/main:{first._record.commit}',
        '--force-with-lease=refs/tags/v2.0.0:',
        '--no-follow-tags',
        f'{second._record.commit}:refs/heads/main',
        f'{second._record.commit}:refs/tags/v2.0.0',
    ]
    output = capsys.readouterr().out
    assert f'{first._record.commit[:12]} -> {second._record.commit[:12]}' in output
    assert 'v2.0.0' in output
    assert '--force-with-lease' not in output


@pytest.mark.slow
def test_sync_suffix_preserves_full_history_acknowledgment_bytes(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exposure = 'historical-private-canary'
    source = make_repo(files={'README.md': f'{exposure}\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    first = _context(tmp_path, source, version='1.0.0')
    _publish.publish(first)
    project = replace(first._project, extra_terms=(exposure,))
    _config.save_project(first._paths, project)
    _second_source_commit(source)
    second = _publish_mode(
        _context(tmp_path, source, extra_terms=(exposure,), version='2.0.0'),
        'interactive',
    )
    expected_publication_prompts = [
        'Continue despite this earlier exposure? [y/n] ',
        ('Publish v2.0.0 to public-owner/public-repo? [y/n] '),
    ]

    def _accept_publication(prompt: str) -> str:
        assert expected_publication_prompts
        assert expected_publication_prompts.pop(0) == prompt
        return 'y'

    monkeypatch.setattr('builtins.input', _accept_publication)
    _publish.publish(second)
    assert not expected_publication_prompts
    project_path = second._paths.config_dir / 'projects' / f'{second._project.name}.toml'
    before = project_path.read_bytes()
    _git_command(host, 'update-ref', 'refs/heads/main', first._record.commit)
    _git_command(host, 'update-ref', '-d', 'refs/tags/v2.0.0')
    prompts: list[str] = []

    def _accept_push(prompt: str) -> str:
        assert prompt == 'Restore these branches and tags to public-owner/public-repo? [y/n] '
        prompts.append(prompt)
        return 'y'

    monkeypatch.setattr('builtins.input', _accept_push)
    saved = _config.load_project(second._paths, second._project.name)

    _publish.sync(_sync_context(replace(second, _project=saved), mode='interactive'))

    assert prompts == ['Restore these branches and tags to public-owner/public-repo? [y/n] ']
    assert project_path.read_bytes() == before


@pytest.mark.slow
def test_sync_yes_refuses_unacknowledged_sent_exposure(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exposure = 'historical-private-canary'
    _source_path, host, _first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
        first_content=f'{exposure}\n',
    )
    _git_command(host, 'update-ref', '-d', 'refs/heads/main')
    project = replace(second._project, extra_terms=(exposure,))
    ctx = replace(second, _project=project)

    with pytest.raises(
        PubanonError,
        match='--yes cannot acknowledge historical exposure',
    ):
        _publish.sync(_sync_context(ctx))

    assert _remote_ref(host, 'refs/heads/main') is None


def test_verify_explicit_revision_skips_ref_drift_acknowledgment(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    commit = _git_command(source, 'rev-parse', 'HEAD').decode().strip()
    ctx = _context(tmp_path, source, selector=commit)
    _second_source_commit(source)

    def _unexpected_prompt(_prompt: str) -> str:
        raise AssertionError('explicit revision must not prompt for ref drift')

    monkeypatch.setattr('builtins.input', _unexpected_prompt)

    verified = _verify_for_publish(ctx)

    assert verified._remote == _host.RemoteRefs(None, {})


def test_verify_refuses_changed_publication_fingerprint(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    changed = replace(ctx._global_config, sensitive_terms=('Different Person',))

    with pytest.raises(PubanonError, match='configuration changed') as error:
        _verify_for_publish(replace(ctx, _global_config=changed))

    assert error.value.recovery == 'pubanon discard, then pubanon prepare'


@pytest.mark.parametrize('drift', ['advanced', 'rewound', 'diverged', 'deleted'])
def test_verify_refuses_ref_drift_under_yes_without_prompt(
    drift: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _drifted_context(drift, tmp_path, fake_gh, make_repo, monkeypatch)

    def _unexpected_prompt(_prompt: str) -> str:
        raise AssertionError('--yes must not prompt for source-ref drift')

    monkeypatch.setattr('builtins.input', _unexpected_prompt)

    with pytest.raises(
        PubanonError, match='--yes cannot approve publishing after the selected source changed'
    ) as error:
        _verify_for_publish(ctx)

    assert error.value.recovery == 'rerun pubanon publish in an interactive terminal without --yes'


@pytest.mark.parametrize('state', ['missing', 'pending', 'failure', 'skipped', 'ambiguous'])
def test_publish_refuses_when_latest_requested_run_is_not_acceptable(
    state: str,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(fake_gh.parent)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _ci_context(fake_gh.parent, source)
    _configure_staged_candidate(ctx, fake_gh, monkeypatch)
    _configure_ci_evidence(ctx, fake_gh, state=state)
    before = _record_path(ctx).read_bytes()
    with pytest.raises(PubanonError, match='the latest staging CI run is not acceptable') as error:
        _publish.publish(ctx)
    assert _record_path(ctx).read_bytes() == before
    assert _remote_ref(host, 'refs/heads/main') is None
    assert 'No push was attempted' in error.value.render()
    assert error.value.recovery is not None
    assert (
        'pubanon stage push --hosted' in error.value.recovery
        if state == 'skipped'
        else 'pubanon stage' in error.value.recovery
    )
    if state in {'failure', 'skipped', 'pending'}:
        assert r'CI / hosted\x1b[31m' in error.value.render()
        assert r'https://example.invalid/runs/9\x0aend' in error.value.render()


@pytest.mark.parametrize('required', [(), ('hosted-validation',)])
def test_publish_accepts_latest_hosted_pass(
    required: tuple[str, ...],
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(fake_gh.parent)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _ci_context(fake_gh.parent, source, required=required)
    _configure_staged_candidate(ctx, fake_gh, monkeypatch)
    _configure_ci_evidence(ctx, fake_gh)
    rendered = StringIO()
    with redirect_stdout(rendered), verbose_context(enabled=True):
        _publish.publish(ctx)
    assert 'Accepted CI run' in rendered.getvalue()
    assert r'CI / hosted\x1b[31m' in rendered.getvalue()
    assert r'https://example.invalid/runs/9\x0aend' in rendered.getvalue()
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit
    assert _records.load_candidate(ctx._paths, ctx._project.name) is None
    calls = gh_calls(fake_gh.parent)
    assert sum('/actions/workflows/ci.yml/runs?' in ' '.join(call) for call in calls) == 4
    assert not any('/check-runs?' in ' '.join(call) for call in calls)


@pytest.mark.parametrize('variable', ['false', 'FALSE', 'true', None])
def test_publish_refuses_final_repository_with_push_ci_turned_off(
    variable: str | None,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(fake_gh.parent)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _ci_context(fake_gh.parent, source, route='direct')
    _configure_ci_evidence(ctx, fake_gh, variable=variable)
    before = _record_path(ctx).read_bytes()
    if variable is not None and variable.casefold() == 'false':
        with pytest.raises(PubanonError, match='the final repository turns off push CI') as error:
            _publish.publish(ctx)
        assert 'CI_RUN_ON_PUSH is false in the final repository.' in error.value.render()
        assert error.value.recovery == (
            'delete CI_RUN_ON_PUSH from the final repository on GitHub, then rerun pubanon publish'
        )
        assert _record_path(ctx).read_bytes() == before
        assert _remote_ref(host, 'refs/heads/main') is None
    else:
        _publish.publish(ctx)
        assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit
    assert not any('/actions/workflows/' in ' '.join(call) for call in gh_calls(fake_gh.parent))


@pytest.mark.parametrize('failure', [False, True])
def test_page_planning_precedes_push_and_decline_has_no_later_lookup(
    page_context: tuple[_publish.PublishContext, _ReleaseHost],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: bool,
) -> None:
    ctx, host = page_context
    _configure_host(_new_host(ctx._paths.data_dir.parent), fake_gh, monkeypatch)
    host.destination = _publish._destination_from_record(ctx._record)
    notes = b'Private Person' if failure else b'Full\x1b[31m notes\nlast line\n'
    ctx = replace(ctx, _yes=False, _release_page=_releases.PageRequest(notes=notes))

    def _decline(question: str) -> bool:
        assert (
            question == 'Publish v1.2.3 to public-owner/public-repo and create its release page?'
        )
        assert host.scans == 1
        return False

    def _push(*_args: object) -> None:
        pytest.fail('push must not happen')

    monkeypatch.setattr(_publish, 'confirm', _decline)
    monkeypatch.setattr(_publish, '_push_candidate', _push)
    with pytest.raises(PubanonError) as caught:
        _publish.publish(ctx)
    assert 'Nothing was published' in caught.value.render()
    assert host.scans == 1
    assert host.writes == []
    if not failure:
        out = capsys.readouterr().out
        assert 'Release page' in out
        assert 'create "v1.2.3"' in out
        for line in notes.decode().split('\n'):
            assert escape(line) in out


@pytest.mark.parametrize(
    'case',
    [
        ('created', 0),
        ('existing', 0),
        ('different', 1),
        ('missing', 1),
        ('unknown', 1),
        ('interrupt', 130),
    ],
)
def test_page_completion_follows_finalized_publication(
    page_context: tuple[_publish.PublishContext, _ReleaseHost],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[str, int],
) -> None:
    outcome, status = case
    ctx, host = page_context
    ctx = replace(
        ctx,
        _project=replace(
            ctx._project, package=_config.PackageObservation('pypi', 'example', ('Release',))
        ),
    )
    git_host = _new_host(ctx._paths.data_dir.parent)
    _configure_host(git_host, fake_gh, monkeypatch)
    host.destination = _publish._destination_from_record(ctx._record)
    notes_file = ctx._paths.data_dir.parent / 'notes.md'
    notes_file.write_bytes(b'Exact\r\nnotes\n')
    page = _host.GitHubRelease(
        'v1.2.3',
        'v1.2.3',
        'Exact\r\nnotes\n',
        'https://example.invalid/page',
        draft=False,
        prerelease=False,
    )
    if outcome == 'missing':
        host.page = page
    _config.save_global(ctx._paths, ctx._global_config, None)
    _config.save_project(ctx._paths, ctx._project)
    monkeypatch.setenv('XDG_CONFIG_HOME', str(ctx._paths.config_dir.parent))
    monkeypatch.setenv('XDG_DATA_HOME', str(ctx._paths.data_dir.parent))
    monkeypatch.setattr(_host, 'read_release', host._read)
    push_candidate = _publish._push_candidate
    pushed = False

    def _push(context: _publish.PublishContext, verified: _publish._Verified) -> None:
        nonlocal pushed
        assert host.writes == []
        push_candidate(context, verified)
        assert _records.load_candidate(ctx._paths, ctx._project.name) is None
        assert _candidate.published_refs(ctx._paths, ctx._project.name) == (
            ctx._record.commit,
            {'v1.2.3': ctx._record.commit},
        )
        pushed = True
        if outcome in {'existing', 'different'}:
            host.page = replace(page, notes='Different') if outcome == 'different' else page
        elif outcome == 'missing':
            host.page = None
        elif outcome == 'unknown':
            host.write_error = PubanonError('connection lost')
        elif outcome == 'interrupt':
            host.write_error = KeyboardInterrupt()
        notes_file.write_bytes(b'changed after confirmation')

    def _create(
        destination: _host.Destination,
        tag: str,
        *,
        title: str,
        notes: bytes,
        mark_latest: bool = True,
    ) -> None:
        assert pushed
        host._create(destination, tag, title=title, notes=notes, mark_latest=mark_latest)

    monkeypatch.setattr(_publish, '_push_candidate', _push)
    monkeypatch.setattr(_host, 'create_release', _create)
    capsys.readouterr()
    assert (
        _cli.main(
            [
                'publish',
                '--project',
                ctx._project.name,
                '--yes',
                '--release-notes-file',
                str(notes_file),
            ]
        )
        == status
    )
    captured = capsys.readouterr()
    assert 'Published v1.2.3 to public-owner/public-repo (public).' in captured.out
    assert _records.load_candidate(ctx._paths, ctx._project.name) is None
    assert _candidate.published_refs(ctx._paths, ctx._project.name) == (
        ctx._record.commit,
        {'v1.2.3': ctx._record.commit},
    )
    assert (
        _git_command(git_host, 'rev-parse', 'refs/tags/v1.2.3').decode().strip()
        == ctx._record.commit
    )
    _assert_page_completion_report(captured, outcome, host, notes_file)
    assert len(host.writes) == (1 if outcome in {'created', 'unknown', 'interrupt'} else 0)
    if host.writes:
        assert host.writes[0][2] == b'Exact\r\nnotes\n'


def test_verify_refuses_tampered_candidate_commit_author(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    store = _store.store_path(ctx._paths, ctx._project.name)
    timestamp = (
        _git_command(store, 'show', '-s', '--format=%at', ctx._record.commit).decode().strip()
    )
    env = {
        'GIT_AUTHOR_DATE': f'{timestamp} +0000',
        'GIT_AUTHOR_EMAIL': 'tampered@example.invalid',
        'GIT_AUTHOR_NAME': 'Tampered Author',
        'GIT_COMMITTER_DATE': f'{timestamp} +0000',
        'GIT_COMMITTER_EMAIL': ctx._global_config.anon_email,
        'GIT_COMMITTER_NAME': ctx._global_config.anon_name,
    }
    tampered = (
        _git_command(
            store,
            'commit-tree',
            ctx._record.tree,
            env=env,
            input_bytes=ctx._record.message.encode() + b'\n',
        )
        .decode()
        .strip()
    )
    record = replace(ctx._record, commit=tampered)
    _git_command(store, 'update-ref', _candidate.CANDIDATE_REF, tampered)
    _candidate.save_candidate(ctx._paths, ctx._project.name, record)

    with pytest.raises(PubanonError, match='snapshot commit metadata'):
        _verify_for_publish(replace(ctx, _record=record))


def test_verify_reruns_content_gates_over_retained_bytes(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'Private Person\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)

    with pytest.raises(PubanonError, match='current content checks'):
        _verify_for_publish(ctx)
