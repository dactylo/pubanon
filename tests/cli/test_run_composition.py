import subprocess
from collections.abc import Callable, Iterable
from dataclasses import replace
from itertools import pairwise
from pathlib import Path

import pytest

from pubanon import (
    _cli,
    _config,
)
from pubanon._cli import _releases as cli_releases
from pubanon._errors import PubanonError
from pubanon._remote import _host, _lifecycle, _releases, _stage, _watch
from pubanon._snapshots import _candidate, _discard, _records, _store
from tests._cli import prepare, save_workflows, stub_remote
from tests._git import git_env
from tests._setup import configure
from tests.remote.test_releases import _Host as _ReleaseHost
from tests.remote.test_stage import _git


def _prepare_stale_stage(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, _config.Paths, _config.ProjectConfig, _candidate.CandidateRecord, Path, Path]:
    source = make_repo(files={'README.md': 'candidate A\n'})
    paths, project, candidate_a = prepare(
        tmp_path,
        source,
        monkeypatch,
        version='1.0.0',
    )
    stage_host = tmp_path / 'stage.git'
    final_host = tmp_path / 'final.git'
    monkeypatch.setattr(_host, '_URL_TEMPLATE', final_host.as_uri())
    for host in (stage_host, final_host):
        subprocess.run(['git', 'init', '--bare', '-q', host], check=True, env=git_env())
    _git(
        stage_host,
        'fetch',
        '-q',
        str(_store.store_path(paths, project.name)),
        f'{candidate_a.commit}:refs/heads/main',
    )
    _discard.discard(paths, project.name, force=True)
    (source / 'README.md').write_text('candidate B\n')
    subprocess.run(['git', '-C', source, 'add', 'README.md'], check=True, env=git_env())
    subprocess.run(
        ['git', '-C', source, 'commit', '-qm', 'candidate B'], check=True, env=git_env()
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
    candidate_b = _records.load_candidate(paths, project.name)
    assert candidate_b is not None
    assert candidate_a.commit != candidate_b.commit
    _stage._save_stage(
        paths,
        project.name,
        _stage.StageRecord(
            candidate_a.commit,
            project.owner,
            _config.stage_repository(project),
        ),
    )
    return source, paths, project, candidate_b, stage_host, final_host


def _stub_repository_settings(
    monkeypatch: pytest.MonkeyPatch, project: _config.ProjectConfig
) -> None:
    def _settings(owner: str, repo: str) -> _config.Settings:
        assert (owner, repo) == (project.owner, project.repo)
        assert project.settings is not None
        return project.settings

    monkeypatch.setattr(_host, 'repository_settings', _settings)


@pytest.mark.slow
def test_automatic_publication_summary_explains_hands_on_review_stopping_point(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    historic_path = 'historical-hygiene-segment-pubanon.txt'
    source = make_repo(files={'README.md': 'public\n', historic_path: 'old\n'})
    paths, project, first = prepare(tmp_path, source, monkeypatch)
    _stub_repository_settings(monkeypatch, project)
    store = _store.store_path(paths, project.name)
    subprocess.run(
        ['git', '-C', store, 'update-ref', 'refs/heads/main', first.commit],
        check=True,
        env=git_env(),
    )
    _discard.discard(paths, project.name, force=True)
    for args in [('rm', historic_path), ('commit', '-qm', 'remove historical file')]:
        subprocess.run(['git', '-C', source, *args], check=True, env=git_env())
    project = replace(
        project, hygiene_terms=('historical-hygiene-segment',), self_publication=False
    )
    _config.save_project(paths, project)
    assert _cli.main(['prepare', '--project', project.name, '--version', '1.0.0', '--yes']) == 0
    record = _records.load_candidate(paths, project.name)
    assert record is not None
    project = save_workflows(paths, project, ship=('inspect', 'publish'))
    monkeypatch.chdir(source)
    stub_remote(
        monkeypatch,
        _host.Destination(
            'github',
            project.owner,
            project.repo,
            'https://github.com/anonymous-account/public-repo.git',
            'anonymous-account',
            project.visibility,
        ),
        _host.RemoteRefs(first.commit, {}),
    )
    capsys.readouterr()
    published: list[str] = []

    def _push(*_args: object, **_kwargs: object) -> _host.GitResult:
        output = capsys.readouterr().out
        visible_lines = [line for line in output.splitlines() if line]

        def is_heading(line: str) -> bool:
            return line.startswith('\x1b[1;36m')

        assert not any(
            is_heading(first) and is_heading(second) for first, second in pairwise(visible_lines)
        )
        assert 'Workflow ship (direct route): inspect, publish' in visible_lines[0]
        assert '\x1b[1;36mChanges to GitHub\x1b[0m' in output
        assert '\x1b[1;36mInspect\x1b[0m' in output
        assert '\x1b[1;36mPublish v1.0.0\x1b[0m' in output
        assert '\x1b[1;36mPublish\x1b[0m' not in output
        assert 'after reviewing the review copy' not in output
        assert 'Inspection does not pause' in output
        assert 'file review' in output
        assert 'Publish v1.0.0' in output
        report = (
            paths.data_dir / 'projects' / project.name / f'candidate-files-{record.commit}.txt'
        )
        assert historic_path in report.read_text()
        assert '--yes: stages continue without asking.' in output
        assert record.commit in output
        published.append(record.commit)
        return _host.GitResult(0, b'', b'')

    def _remote_refs(*_args: object, **_kwargs: object) -> _host.RemoteRefs:
        if published:
            return _host.RemoteRefs(record.commit, {'v1.0.0': record.commit})
        return _host.RemoteRefs(first.commit, {})

    monkeypatch.setattr(_host, 'ls_remote', _remote_refs)
    monkeypatch.setattr(_host, 'push', _push)
    argv = [
        'run',
        'ship',
        '--version',
        '1.0.0',
        '--color',
        'always',
        '--yes',
    ]
    assert _cli.main(argv) == 0
    assert published == [record.commit]


@pytest.mark.slow
@pytest.mark.parametrize('command', ['publish', 'run'])
def test_verbose_publication_preview_in_direct_and_composed_workflows(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: str,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    _stub_repository_settings(monkeypatch, project)
    if command == 'run':
        project = save_workflows(paths, project, ship=('inspect', 'publish'))
    monkeypatch.chdir(source)
    stub_remote(
        monkeypatch,
        _host.Destination(
            'github',
            project.owner,
            project.repo,
            'https://github.com/anonymous-account/public-repo.git',
            'anonymous-account',
            project.visibility,
        ),
        _host.RemoteRefs(None, {}),
    )
    published: list[str] = []

    def _push(*_args: object, **_kwargs: object) -> _host.GitResult:
        preview = capsys.readouterr().out
        assert 'Publish v1.0.0' in preview
        publication_preview = preview.split('Publish v1.0.0', 1)[1]
        assert 'Snapshot tree:' in publication_preview
        assert record.tree in publication_preview
        published.append(record.commit)
        return _host.GitResult(0, b'', b'')

    def _remote_refs(*_args: object, **_kwargs: object) -> _host.RemoteRefs:
        if published:
            return _host.RemoteRefs(record.commit, {'v1.0.0': record.commit})
        return _host.RemoteRefs(None, {})

    monkeypatch.setattr(_host, 'ls_remote', _remote_refs)
    monkeypatch.setattr(_host, 'push', _push)
    capsys.readouterr()
    argv = (
        ['run', 'ship', '--project', project.name, '--version', '1.0.0', '--yes', '--verbose']
        if command == 'run'
        else ['publish', '--project', project.name, '--yes', '--verbose']
    )
    assert _cli.main(argv) == 0
    assert published == [record.commit]


@pytest.mark.slow
def test_run_suggests_repo_watch_when_ci_workflow_is_the_only_ci_requirement(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    paths, project, first = prepare(tmp_path, source, monkeypatch)
    _stub_repository_settings(monkeypatch, project)
    store = _store.store_path(paths, project.name)
    subprocess.run(
        ['git', '-C', store, 'update-ref', 'refs/heads/main', first.commit],
        check=True,
        env=git_env(),
    )
    _discard.discard(paths, project.name, force=True)
    (source / 'README.md').write_text('public\n')
    subprocess.run(['git', '-C', source, 'add', 'README.md'], check=True, env=git_env())
    subprocess.run(
        ['git', '-C', source, 'commit', '-qm', 'publish public file'],
        check=True,
        env=git_env(),
    )
    project = replace(project, ci_workflow='ci.yml', required_checks=())
    _config.save_project(paths, project)
    assert _cli.main(['prepare', '--project', project.name, '--version', '1.0.0', '--yes']) == 0
    record = _records.load_candidate(paths, project.name)
    assert record is not None
    project = save_workflows(paths, project, ship=('publish',))
    monkeypatch.chdir(source)
    stub_remote(
        monkeypatch,
        _host.Destination(
            'github',
            project.owner,
            project.repo,
            'https://github.com/anonymous-account/public-repo.git',
            'anonymous-account',
            project.visibility,
        ),
        _host.RemoteRefs(first.commit, {}),
    )
    published: list[str] = []

    def _push(*_args: object, **_kwargs: object) -> _host.GitResult:
        published.append(record.commit)
        return _host.GitResult(0, b'', b'')

    def _remote_refs(*_args: object, **_kwargs: object) -> _host.RemoteRefs:
        if published:
            return _host.RemoteRefs(record.commit, {'v1.0.0': record.commit})
        return _host.RemoteRefs(first.commit, {})

    def _no_final_ci_variable(coordinates: tuple[str, str], name: str) -> None:
        assert coordinates == (project.owner, project.repo)
        assert name == 'CI_RUN_ON_PUSH'

    monkeypatch.setattr(_host, 'ls_remote', _remote_refs)
    monkeypatch.setattr(_host, 'push', _push)
    monkeypatch.setattr(_host, 'repository_variable', _no_final_ci_variable)
    capsys.readouterr()

    assert (
        _cli.main(['run', 'ship', '--project', project.name, '--version', '1.0.0', '--yes']) == 0
    )
    assert published == [record.commit]
    assert 'Next: pubanon repo watch --version 1.0.0' in capsys.readouterr().out


@pytest.mark.slow
def test_composed_publication_requires_explicit_route_change_after_forgetting_staging(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _source, paths, project, candidate_b, stage_host, final_host = _prepare_stale_stage(
        tmp_path, make_repo, monkeypatch
    )
    _stub_repository_settings(monkeypatch, project)
    project = replace(project, publication_route='staged')
    _config.save_project(paths, project)
    stage_refs = _git(stage_host, 'show-ref')
    stage_record = _stage.load_stage(paths, project.name)
    project = save_workflows(paths, project, ship=('publish',))
    global_config = _config.load_global(paths)
    destination = _host.Destination(
        adapter=project.host,
        owner=project.owner,
        repo=project.repo,
        url=final_host.as_uri(),
        account=global_config.accounts[project.host],
        visibility=project.visibility,
    )

    def _verify_destination(*_args: object, **_kwargs: object) -> _host.Destination:
        return destination

    def _forbidden_push(*_args: object, **_kwargs: object) -> _host.GitResult:
        pytest.fail('stale staging evidence reached publication push')

    real_push = _host.push
    monkeypatch.setattr(_host, 'verify_destination', _verify_destination)
    monkeypatch.setattr(_host, 'push', _forbidden_push)
    capsys.readouterr()

    argv = [
        'run',
        'ship',
        '--project',
        project.name,
        '--version',
        '1.0.0',
        '--yes',
    ]
    assert _cli.main(argv) == 1

    captured = capsys.readouterr()
    assert 'Publication preview:' not in captured.out
    assert 'staging evidence does not cover' in captured.err
    assert 'pubanon stage push' in captured.err
    retained = _records.load_candidate(paths, project.name)
    assert retained is not None
    assert retained == candidate_b
    assert _stage.load_stage(paths, project.name) == stage_record
    assert _git(final_host, 'for-each-ref') == b''
    assert _git(stage_host, 'show-ref') == stage_refs
    assert _cli.main(['stage', 'forget', '--project', project.name, '--yes']) == 0
    assert _stage.load_stage(paths, project.name) is None
    assert _cli.main(argv) == 1
    assert 'no staged commit is recorded' in capsys.readouterr().err
    assert _records.load_candidate(paths, project.name) == candidate_b
    _config.save_project(paths, replace(project, publication_route='direct'))
    capsys.readouterr()

    def _locked_push(
        destination: _host.Destination, cwd: Path, args: Iterable[str]
    ) -> _host.GitResult:
        with (
            pytest.raises(PubanonError, match='another pubanon command is running'),
            _config.project_lock(paths, project.name),
        ):
            pytest.fail('composed publication released its project lock')
        return real_push(destination, cwd, args)

    monkeypatch.setattr(_host, 'push', _locked_push)
    assert _cli.main(argv) == 0
    output = capsys.readouterr().out
    assert _git(final_host, 'show-ref').decode().splitlines() == [
        f'{candidate_b.commit} refs/heads/main',
        f'{candidate_b.commit} refs/tags/v1.0.0',
    ]
    assert _git(stage_host, 'show-ref') == stage_refs
    assert _stage.load_stage(paths, project.name) is None
    assert _records.load_candidate(paths, project.name) is None
    assert 'Workflow ship (direct route): publish' in output


@pytest.mark.slow
def test_run_decline_after_preview_retains_candidate(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    project = save_workflows(paths, project, ship=('prepare', 'inspect', 'publish'))
    monkeypatch.chdir(source)
    host = tmp_path / 'host.git'
    subprocess.run(['git', 'init', '--bare', '-q', host], check=True, env=git_env())
    destination = _host.Destination(
        'github',
        project.owner,
        project.repo,
        host.as_uri(),
        global_config.accounts['github'],
        project.visibility,
    )
    prompts: list[str] = []

    def _unexpected_push(*_args: object) -> _host.GitResult:
        pytest.fail('publication push occurred without approval')

    def _verify(*_args: object) -> _host.Destination:
        return destination

    def _decline(prompt: str) -> str:
        if prompt.startswith('Use this selection?'):
            assert _records.load_candidate(paths, project.name) is None
            return 'c'
        prompts.append(prompt)
        record = _records.load_candidate(paths, project.name)
        assert record is not None
        output = capsys.readouterr().out
        assert record.commit in output
        assert f'{project.owner}/{project.repo}' in output
        assert 'Publish v1.0.0' in output
        assert (
            subprocess.run(
                ['git', '-C', host, 'for-each-ref'],
                check=True,
                capture_output=True,
                env=git_env(),
            ).stdout
            == b''
        )
        return 'n'

    monkeypatch.setattr(_host, 'push', _unexpected_push)
    monkeypatch.setattr(_host, 'verify_destination', _verify)
    monkeypatch.setattr(_host, '_URL_TEMPLATE', host.as_uri())
    monkeypatch.setattr('builtins.input', _decline)
    argv = [
        'run',
        'ship',
        '--version',
        '1.0.0',
        '--project',
        project.name,
    ]
    assert _cli.main(argv) == 130
    assert len(prompts) == 1
    retained = _records.load_candidate(paths, project.name)
    assert retained is not None
    assert prompts == [f'Publish v1.0.0 to {project.owner}/{project.repo}? [y/n] ']
    assert _records.load_candidate(paths, project.name) is not None
    assert _candidate.published_refs(paths, project.name) == (None, {})
    assert (
        subprocess.run(
            ['git', '-C', host, 'for-each-ref'],
            check=True,
            capture_output=True,
            env=git_env(),
        ).stdout
        == b''
    )
    error = capsys.readouterr().err
    assert 'Canceled. Nothing was published.' in error
    assert 'Next: pubanon publish' in error


@pytest.mark.parametrize('color', ['auto', 'always'])
def test_interactive_reveal_has_one_stage_heading(
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    color: str,
) -> None:
    paths, global_config, project = configured_project
    project = replace(project, visibility='private')
    save_workflows(paths, project, reveal=('repo reveal',))
    destination = _host.Destination(
        project.host,
        project.owner,
        project.repo,
        'https://example.invalid/repo.git',
        global_config.accounts[project.host],
        'private',
    )

    def _account(*_args: object, **_kwargs: object) -> str:
        return destination.account

    def _repository(*_args: object) -> tuple[str, str, str]:
        return project.owner, project.repo, 'private'

    def _destination(*_args: object) -> _host.Destination:
        return destination

    def _stop_before_mutation(*_args: object, **_kwargs: object) -> None:
        raise PubanonError('read-only preflight stop')

    monkeypatch.setattr(_host, 'require_authenticated_account', _account)
    monkeypatch.setattr(_host, 'repository_facts', _repository)
    monkeypatch.setattr(_host, 'verify_destination', _destination)
    monkeypatch.setattr(_lifecycle, '_verify_before_flip', _stop_before_mutation)
    capsys.readouterr()
    assert _cli.main(['run', 'reveal', '--project', project.name, '--color', color]) == 1
    result = capsys.readouterr()
    headline = f'Make {project.owner}/{project.repo} public'
    assert result.out.count(headline) == 1
    assert 'Repo reveal' not in result.out
    if color == 'always':
        assert f'\x1b[1;36m{headline}\x1b[0m' in result.out
    assert 'read-only preflight stop' in result.err


@pytest.fixture
def workflow_page(
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_config.Paths, _config.ProjectConfig, _ReleaseHost]:
    paths, global_config, project = configured_project
    project = replace(
        project, release_page=_config.ReleasePageSettings(enabled=True, notes='Fresh {tag}')
    )
    save_workflows(paths, project, ship=('publish', 'repo watch'))
    _store.candidate_store_path(paths, project.name).mkdir(parents=True)
    host = _ReleaseHost(
        _host.Destination(
            project.host,
            project.owner,
            project.repo,
            'https://example.invalid/repo.git',
            global_config.accounts[project.host],
            project.visibility,
        ),
        'a' * 40,
    )

    def _published(*_args: object) -> tuple[str, dict[str, str]]:
        return host.commit, {host.tag: host.commit}

    monkeypatch.setattr(_candidate, 'published_refs', _published)
    monkeypatch.setattr(_host, 'verify_destination', host._destination)
    monkeypatch.setattr(_host, 'ls_remote', host._refs)
    monkeypatch.setattr(_host, 'read_release', host._read)
    monkeypatch.setattr(_host, 'all_releases', host._all)
    monkeypatch.setattr(_host, 'create_release', host._create)
    monkeypatch.setattr(_releases, '_monotonic', host.clock._monotonic)
    monkeypatch.setattr(_releases, '_sleep', host.clock._sleep)
    return paths, project, host


def test_run_reads_notes_once_before_lock_and_keeps_exact_bytes(
    workflow_page: tuple[_config.Paths, _config.ProjectConfig, _ReleaseHost],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, project, host = workflow_page
    save_workflows(paths, project, ship=('publish',))
    notes_file = tmp_path / 'notes.md'
    notes = b'First\r\nSecond\n'
    notes_file.write_bytes(notes)
    read_notes = cli_releases.read_notes_file
    reads: list[str] = []

    def _read(path: str) -> bytes:
        with _config.project_lock(paths, project.name):
            reads.append(path)
        result = read_notes(path)
        notes_file.unlink()
        return result

    monkeypatch.setattr(cli_releases, 'read_notes_file', _read)
    assert (
        _cli.main(
            [
                'run',
                'ship',
                '--project',
                project.name,
                '--version',
                '1.2.3',
                '--yes',
                '--release-notes-file',
                str(notes_file),
            ]
        )
        == 0
    )
    assert reads == [str(notes_file)]
    assert host.writes == [('v1.2.3', 'v1.2.3', notes)]


def test_run_resume_creates_a_missing_draft_page_with_guidance(
    workflow_page: tuple[_config.Paths, _config.ProjectConfig, _ReleaseHost],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A rerun can create the draft after the release workflow finished (spec section 5.4).
    paths, project, host = workflow_page
    assert project.release_page is not None
    save_workflows(
        paths,
        replace(project, release_page=replace(project.release_page, draft=True)),
        ship=('publish',),
    )

    assert (
        _cli.main(['run', 'ship', '--project', project.name, '--version', '1.2.3', '--yes']) == 0
    )

    assert host.drafts == [True]
    output = capsys.readouterr().out
    assert 'create draft release page v1.2.3 if none exists' in output
    assert 'Release page created: https://example.invalid/release (draft)' in output
    assert 'rerun the workflow or publish the page on GitHub' in output


def test_run_resume_existing_page_ignores_current_defaults(
    workflow_page: tuple[_config.Paths, _config.ProjectConfig, _ReleaseHost],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, project, host = workflow_page
    host.page = _host.GitHubRelease(
        'v1.2.3', 'Custom', 'custom notes', 'url', draft=True, prerelease=True
    )
    save_workflows(
        paths,
        replace(
            project,
            release_page=_config.ReleasePageSettings(
                enabled=True,
                changelog='missing.md',
            ),
        ),
        ship=('publish', 'repo watch'),
    )
    watched: list[bool] = []

    def _watch_ci(*_args: object, **_kwargs: object) -> None:
        watched.append(True)

    monkeypatch.setattr(_watch, 'watch_ci', _watch_ci)
    assert (
        _cli.main(['run', 'ship', '--project', project.name, '--version', '1.2.3', '--yes']) == 0
    )
    assert watched == [True]
    assert host.writes == []


@pytest.mark.parametrize('only_publish', [False, True])
@pytest.mark.parametrize('yes', [False, True])
def test_run_resume_previews_and_creates_missing_page_under_lock(
    workflow_page: tuple[_config.Paths, _config.ProjectConfig, _ReleaseHost],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    only_publish: bool,
    yes: bool,
) -> None:
    paths, project, host = workflow_page
    if only_publish:
        save_workflows(paths, project, ship=('publish',))
    events: list[str] = []

    def _confirm(prompt: str) -> bool:
        assert not yes
        assert prompt == 'Create this release page?'
        output = capsys.readouterr().out
        assert 'create release page v1.2.3 if none exists' in output
        assert 'Release page create' in output
        assert 'Fresh v1.2.3' in output
        with (
            pytest.raises(PubanonError, match='another pubanon command is running'),
            _config.project_lock(paths, project.name),
        ):
            pass
        events.append('confirm')
        return True

    def _create(
        dest: _host.Destination,
        tag: str,
        *,
        title: str,
        notes: bytes,
        mode: _host.ReleaseCreationMode = 'latest',
    ) -> None:
        with (
            pytest.raises(PubanonError, match='another pubanon command is running'),
            _config.project_lock(paths, project.name),
        ):
            pass
        events.append('create')
        host._create(dest, tag, title=title, notes=notes, mode=mode)

    def _watch_ci(*_args: object, **_kwargs: object) -> None:
        assert host.page is not None
        events.append('watch')

    monkeypatch.setattr(cli_releases, 'confirm', _confirm)
    monkeypatch.setattr(_host, 'create_release', _create)
    monkeypatch.setattr(_watch, 'watch_ci', _watch_ci)
    assert (
        _cli.main(
            [
                'run',
                'ship',
                '--project',
                project.name,
                '--version',
                '1.2.3',
                *(['--yes'] if yes else []),
            ]
        )
        == 0
    )
    assert events == ([] if yes else ['confirm']) + ['create'] + (
        [] if only_publish else ['watch']
    )
    assert host.writes == [('v1.2.3', 'v1.2.3', b'Fresh v1.2.3\n')]
    output = capsys.readouterr().out
    if only_publish:
        assert output.index('Release page created') < output.index('Nothing to do:')
    else:
        assert 'Workflow ship completed.' in output


def test_run_resume_same_notes_converges_after_unverified_creation(
    workflow_page: tuple[_config.Paths, _config.ProjectConfig, _ReleaseHost],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, project, host = workflow_page
    save_workflows(
        paths,
        replace(project, release_page=_config.ReleasePageSettings(enabled=False)),
        ship=('publish', 'repo watch'),
    )
    notes = tmp_path / 'notes.md'
    notes.write_bytes(b'Exact\r\nnotes\n')
    watched: list[bool] = []

    def _watch_ci(*_args: object, **_kwargs: object) -> None:
        watched.append(True)

    monkeypatch.setattr(_watch, 'watch_ci', _watch_ci)
    argv = [
        'run',
        'ship',
        '--project',
        project.name,
        '--version',
        '1.2.3',
        '--release-notes-file',
        str(notes),
        '--yes',
    ]
    host.after_write = 'read-error'
    assert _cli.main(argv) == 1
    assert len(host.writes) == 1
    assert watched == []
    assert 'release create v1.2.3' in capsys.readouterr().err
    host.after_write = 'normal'
    assert _cli.main(argv) == 0
    assert len(host.writes) == 1
    assert watched == [True]
    assert 'Release page already exists' in capsys.readouterr().out
    notes.write_bytes(b'Different\n')
    assert _cli.main(argv) == 1
    assert 'release edit v1.2.3' in capsys.readouterr().err
    assert len(host.writes) == 1
    assert watched == [True]


@pytest.mark.parametrize('settings', [None, _config.ReleasePageSettings()])
def test_run_resume_with_pages_off_skips_page_lookup(
    workflow_page: tuple[_config.Paths, _config.ProjectConfig, _ReleaseHost],
    monkeypatch: pytest.MonkeyPatch,
    *,
    settings: _config.ReleasePageSettings | None,
) -> None:
    paths, project, host = workflow_page
    save_workflows(paths, replace(project, release_page=settings), ship=('publish', 'repo watch'))
    watched: list[bool] = []

    def _unexpected_read(*_args: object, **_kwargs: object) -> None:
        pytest.fail('page lookup ran with release pages off')

    def _watch_ci(*_args: object, **_kwargs: object) -> None:
        watched.append(True)

    monkeypatch.setattr(_host, 'read_release', _unexpected_read)
    monkeypatch.setattr(_watch, 'watch_ci', _watch_ci)
    assert (
        _cli.main(['run', 'ship', '--project', project.name, '--version', '1.2.3', '--yes']) == 0
    )
    assert watched == [True]
    assert host.writes == []


@pytest.mark.parametrize('skip', [False, True])
def test_run_resume_failed_page_lookup_stops_unless_skipped(
    workflow_page: tuple[_config.Paths, _config.ProjectConfig, _ReleaseHost],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    skip: bool,
) -> None:
    _paths, project, host = workflow_page
    watched: list[bool] = []

    def _failed_read(*_args: object, **_kwargs: object) -> None:
        raise PubanonError('page lookup failed')

    def _watch_ci(*_args: object, **_kwargs: object) -> None:
        watched.append(True)

    monkeypatch.setattr(_host, 'read_release', _failed_read)
    monkeypatch.setattr(_watch, 'watch_ci', _watch_ci)
    assert _cli.main(
        [
            'run',
            'ship',
            '--project',
            project.name,
            '--version',
            '1.2.3',
            '--yes',
            *(['--no-release-page'] if skip else []),
        ]
    ) == (0 if skip else 1)
    assert watched == ([True] if skip else [])
    assert host.writes == []
    if not skip:
        assert 'page lookup failed' in capsys.readouterr().err
