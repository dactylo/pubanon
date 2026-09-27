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
from pubanon._errors import PubanonError
from pubanon._remote import _host, _lifecycle, _stage
from pubanon._remote import _publish as _publish_module
from pubanon._snapshots import _candidate
from tests._cli import prepare, save_workflows, stub_remote
from tests._git import git_env
from tests._setup import configure
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
        str(_candidate.store_path(paths, project.name)),
        f'{candidate_a.commit}:refs/heads/main',
    )
    _candidate.discard(paths, project.name, force=True)
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
    candidate_b = _candidate.load_candidate(paths, project.name)
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
    store = _candidate.store_path(paths, project.name)
    subprocess.run(
        ['git', '-C', store, 'update-ref', 'refs/heads/main', first.commit],
        check=True,
        env=git_env(),
    )
    _candidate.discard(paths, project.name, force=True)
    for args in [('rm', historic_path), ('commit', '-qm', 'remove historical file')]:
        subprocess.run(['git', '-C', source, *args], check=True, env=git_env())
    project = replace(
        project, hygiene_terms=('historical-hygiene-segment',), self_publication=False
    )
    _config.save_project(paths, project)
    assert _cli.main(['prepare', '--project', project.name, '--version', '1.0.0', '--yes']) == 0
    record = _candidate.load_candidate(paths, project.name)
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
        assert 'Inspection does not pause this workflow for file review.' in output
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

    monkeypatch.setattr(_publish_module, 'verify_destination', _host.verify_destination)
    monkeypatch.setattr(_publish_module, 'ls_remote', _remote_refs)
    monkeypatch.setattr(_publish_module, 'push', _push)
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
def test_composed_publication_requires_explicit_route_change_after_forgetting_staging(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _source, paths, project, candidate_b, stage_host, final_host = _prepare_stale_stage(
        tmp_path, make_repo, monkeypatch
    )
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

    class _HostModule:
        @staticmethod
        def _verify_destination(*_args: object, **_kwargs: object) -> _host.Destination:
            return destination

    def _forbidden_push(*_args: object, **_kwargs: object) -> _host.GitResult:
        pytest.fail('stale staging evidence reached publication push')

    monkeypatch.setattr(_host, 'verify_destination', _HostModule._verify_destination)
    monkeypatch.setattr(_publish_module, 'verify_destination', _HostModule._verify_destination)
    monkeypatch.setattr(_publish_module, 'push', _forbidden_push)
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
    retained = _candidate.load_candidate(paths, project.name)
    assert retained is not None
    assert retained == candidate_b
    assert _stage.load_stage(paths, project.name) == stage_record
    assert _git(final_host, 'for-each-ref') == b''
    assert _git(stage_host, 'show-ref') == stage_refs
    assert _cli.main(['stage', 'forget', '--project', project.name, '--yes']) == 0
    assert _stage.load_stage(paths, project.name) is None
    assert _cli.main(argv) == 1
    assert 'no staged commit is recorded' in capsys.readouterr().err
    assert _candidate.load_candidate(paths, project.name) == candidate_b
    _config.save_project(paths, replace(project, publication_route='direct'))
    capsys.readouterr()

    def _locked_push(
        destination: _host.Destination, cwd: Path, args: Iterable[str]
    ) -> _host.GitResult:
        with (
            pytest.raises(PubanonError, match='another Pubanon command is running'),
            _config.project_lock(paths, project.name),
        ):
            pytest.fail('composed publication released its project lock')
        return _host.push(destination, cwd, args)

    monkeypatch.setattr(_publish_module, 'push', _locked_push)
    assert _cli.main(argv) == 0
    output = capsys.readouterr().out
    assert _git(final_host, 'show-ref').decode().splitlines() == [
        f'{candidate_b.commit} refs/heads/main',
        f'{candidate_b.commit} refs/tags/v1.0.0',
    ]
    assert _git(stage_host, 'show-ref') == stage_refs
    assert _stage.load_stage(paths, project.name) is None
    assert _candidate.load_candidate(paths, project.name) is None
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
            assert _candidate.load_candidate(paths, project.name) is None
            return 'c'
        prompts.append(prompt)
        record = _candidate.load_candidate(paths, project.name)
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
        return 'no'

    monkeypatch.setattr(_publish_module, 'push', _unexpected_push)
    monkeypatch.setattr(_publish_module, 'verify_destination', _verify)
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
    retained = _candidate.load_candidate(paths, project.name)
    assert retained is not None
    assert prompts == [f'Publish v1.0.0 to {project.owner}/{project.repo}? [y/n] ']
    assert _candidate.load_candidate(paths, project.name) is not None
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

    monkeypatch.setattr(_lifecycle, 'require_authenticated_account', _account)
    monkeypatch.setattr(_lifecycle, 'repository_facts', _repository)
    monkeypatch.setattr(_lifecycle, 'verify_destination', _destination)
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
