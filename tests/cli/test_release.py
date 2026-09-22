import json
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from pubanon import (
    _cli,
    _config,
)
from pubanon._cli import _inspect
from pubanon._remote import _host, _stage
from pubanon._remote import _publish as _publish_module
from pubanon._snapshots import _audit, _candidate, _source
from pubanon._terminal import escape
from tests._cli import prepare, record_published_main, save_workflows, stub_remote
from tests._git import git_env
from tests._setup import configure


@pytest.mark.parametrize('command', ['release', 'run'])
def test_automatic_publication_summary_explains_hands_on_review_stopping_point(
    command: str,
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
    assert _cli.main(['prepare', '--project', project.name, '--version', '1.0.0']) == 0
    record = _candidate.load_candidate(paths, project.name)
    assert record is not None
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
        _host.RemoteRefs(first.commit, {}),
    )
    capsys.readouterr()
    published: list[str] = []

    def _push(*_args: object, **_kwargs: object) -> _host.GitResult:
        output = capsys.readouterr().out
        assert 'after reviewing the snapshot directory' not in output
        assert 'Inspection does not pause this workflow for file review.' in output
        assert 'Publish prepared snapshot' in output
        report = (
            paths.data_dir / 'projects' / project.name / f'candidate-files-{record.commit}.txt'
        )
        assert escape(str(report)) in output
        assert historic_path in report.read_text()
        assert 'Automatic continuation:' in output
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
    argv = (
        ['release', '1.0.0', '--yes']
        if command == 'release'
        else [
            'run',
            'ship',
            '--version',
            '1.0.0',
            '--yes',
        ]
    )
    assert _cli.main(argv) == 0
    assert published == [record.commit]


@pytest.mark.parametrize('command', ['release', 'run'])
def test_composed_publication_refuses_stage_evidence_for_an_older_candidate(
    command: str,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'candidate A\n'})
    paths, project, candidate_a = prepare(
        tmp_path,
        source,
        monkeypatch,
        version='1.0.0',
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
    if command == 'run':
        project = save_workflows(paths, project, ship=('publish',))
    global_config = _config.load_global(paths)
    destination = _host.Destination(
        adapter=project.host,
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account=global_config.accounts[project.host],
        visibility=project.visibility,
    )

    class _HostModule:
        @staticmethod
        def _ls_remote(*_args: object, **_kwargs: object) -> _host.RemoteRefs:
            return _host.RemoteRefs(None, {})

        @staticmethod
        def _verify_destination(*_args: object, **_kwargs: object) -> _host.Destination:
            return destination

    def _forbidden_push(*_args: object, **_kwargs: object) -> _host.GitResult:
        pytest.fail('stale staging evidence reached publication push')

    monkeypatch.setattr(_host, 'verify_destination', _HostModule._verify_destination)
    monkeypatch.setattr(_host, 'ls_remote', _HostModule._ls_remote)
    monkeypatch.setattr(_publish_module, 'verify_destination', _HostModule._verify_destination)
    monkeypatch.setattr(_publish_module, 'ls_remote', _HostModule._ls_remote)
    monkeypatch.setattr(_publish_module, 'push', _forbidden_push)
    capsys.readouterr()

    argv = (
        ['release', '1.0.0', '--project', project.name, '--yes']
        if command == 'release'
        else [
            'run',
            'ship',
            '--project',
            project.name,
            '--version',
            '1.0.0',
            '--yes',
        ]
    )
    assert _cli.main(argv) == 1

    captured = capsys.readouterr()
    assert 'Publication preview:' not in captured.out
    assert 'staging evidence does not cover' in captured.err
    assert 'pubanon stage push' in captured.err
    assert 'pubanon stage clean' in captured.err
    retained = _candidate.load_candidate(paths, project.name)
    assert retained is not None
    assert replace(retained, source_bookmark=None) == candidate_b
    bookmark = _source.describe_bookmark(source, project.name, candidate_b.commit)
    assert retained.source_bookmark == bookmark
    assert _source.source_recovery(bookmark, candidate_b.pinned.commit).status == 'protected'


def test_release_composes_real_prepare_inspection_and_publication_output(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    monkeypatch.chdir(source)
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account=global_config.accounts['github'],
        visibility=project.visibility,
    )
    remote_reads = 0

    def _accept(prompt: str) -> str:
        record = _candidate.load_candidate(paths, project.name)
        assert record is not None
        assert prompt == (
            f'Publish main at {record.commit[:7]} and tag v2.0.0 to '
            f'{project.owner}/{project.repo} (private)? [y/n] '
        )
        return 'yes'

    def _verified_destination(
        _paths: _config.Paths,
        _global: _config.GlobalConfig,
        _project: _config.ProjectConfig,
    ) -> _host.Destination:
        return destination

    def _remote_refs(
        _destination: _host.Destination,
        _paths: _config.Paths,
        _project: str,
    ) -> _host.RemoteRefs:
        nonlocal remote_reads
        remote_reads += 1
        if remote_reads == 1:
            return _host.RemoteRefs(None, {})
        record = _candidate.load_candidate(paths, project.name)
        assert record is not None
        return _host.RemoteRefs(record.commit, {'v2.0.0': record.commit})

    def _push(
        _destination: _host.Destination,
        _cwd: Path,
        _args: object,
    ) -> _host.GitResult:
        return _host.GitResult(0, b'', b'')

    monkeypatch.setattr('builtins.input', _accept)
    monkeypatch.setattr(_publish_module, 'verify_destination', _verified_destination)
    monkeypatch.setattr(_publish_module, 'ls_remote', _remote_refs)
    monkeypatch.setattr(_publish_module, 'push', _push)

    assert _cli.main(['release', '2.0.0', '--project', project.name]) == 0

    output = capsys.readouterr().out
    main, tags = _candidate.published_refs(paths, project.name)
    assert main is not None
    assert tags == {'v2.0.0': main}
    assert _candidate.load_candidate(paths, project.name) is None
    assert 'release stages\n  prepare snapshot\n  inspect snapshot summary\n' in output
    assert 'Prepared snapshot\n' in output
    assert 'Next: pubanon inspect' not in output
    assert 'Content, identity, and destination rechecked.' in output
    assert 'after reviewing the snapshot directory' not in output
    assert 'Inspection does not pause this workflow for file review.' in output
    assert 'Published snapshot\nPublication verified\n' in output
    assert 'Next: pubanon repo reveal\n' in output
    assert output.count('Publish prepared snapshot') == 1
    assert output.count('Identity:') == 1
    assert output.index('Prepared snapshot') < output.index('Snapshot\n  State: prepared')
    assert output.index('Snapshot\n  State: prepared') < output.index('Published snapshot')


@pytest.mark.parametrize('command', ['release', 'run'])
def test_release_decline_after_preview_retains_candidate(
    command: str,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    if command == 'run':
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
        prompts.append(prompt)
        record = _candidate.load_candidate(paths, project.name)
        assert record is not None
        output = capsys.readouterr().out
        assert record.commit in output
        assert 'Anonymous Publisher <anonymous@example.invalid>' in output
        assert 'README.md' in output
        assert f'{project.owner}/{project.repo}' in output
        assert 'Publish prepared snapshot' in output
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
    monkeypatch.setattr(_host, 'URL_TEMPLATE', host.as_uri())
    monkeypatch.setattr('builtins.input', _decline)
    argv = (
        ['release', '1.0.0']
        if command == 'release'
        else [
            'run',
            'ship',
            '--version',
            '1.0.0',
            '--project',
            project.name,
        ]
    )
    assert _cli.main(argv) == 1
    assert len(prompts) == 1
    retained = _candidate.load_candidate(paths, project.name)
    assert retained is not None
    assert prompts == [
        (
            f'Publish main at {retained.commit[:7]} and tag v1.0.0 to '
            f'{project.owner}/{project.repo} (private)? [y/n] '
        )
    ]
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
    assert 'pubanon inspect' in capsys.readouterr().err


def test_release_invalid_version_is_silent_and_does_not_echo_effective_term(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    term = 'private-version-canary'
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    _config.save_project(paths, replace(project, extra_terms=(term,)))
    monkeypatch.chdir(source)

    assert _cli.main(['release', term, '--yes']) == 1

    captured = capsys.readouterr()
    assert captured.out == ''


@pytest.mark.parametrize('yes', [False, True])
def test_release_keeps_confirmation_and_yes_mode_distinct(
    yes: bool,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    prepare(tmp_path, source, monkeypatch, version='1.0.0')
    monkeypatch.chdir(source)
    prompts: list[str] = []
    contexts: list[_publish_module.PublishContext] = []

    def _approve(prompt: str) -> str:
        prompts.append(prompt)
        return 'yes'

    def _publish(ctx: _publish_module.PublishContext) -> None:
        contexts.append(ctx)

    monkeypatch.setattr('builtins.input', _approve)
    monkeypatch.setattr(_publish_module, 'publish', _publish)
    argv = ['release', '1.0.0']
    if yes:
        argv.append('--yes')

    assert _cli.main(argv) == 0

    assert len(contexts) == 1
    assert contexts[0]._yes is yes
    assert contexts[0]._unattended is yes
    assert prompts == []


def test_release_mismatch_reports_effective_intended_tag(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    term = 'v2.0.0'
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, _record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    _config.save_project(paths, replace(project, extra_terms=(term,)))
    monkeypatch.chdir(source)

    assert _cli.main(['release', '2.0.0', '--yes']) == 1

    rendered = capsys.readouterr().err
    assert term in rendered


def test_release_refuses_retained_candidate_for_a_different_version(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    prepare(tmp_path, source, monkeypatch, version='1.0.0')
    monkeypatch.chdir(source)

    assert _cli.main(['release', '2.0.0', '--yes']) == 1
    assert 'discard' in capsys.readouterr().err


@pytest.mark.parametrize('complete', [False, True])
def test_release_refuses_retained_rewrite_before_inspection_or_host(
    complete: bool,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    _candidate.save_candidate(
        paths,
        project.name,
        replace(
            record,
            prestate_main='a' * 40,
            prestate_tags={'v1.0.0': 'a' * 40},
            replaces='a' * 40,
            replace_scope='one',
            tip=record.commit if complete else None,
            poststate_tags={'v1.0.0': record.commit} if complete else None,
        ),
    )

    def _unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail('retained rewrite reached inspection or host')

    monkeypatch.setattr(_inspect, 'inspection_copy', _unexpected)
    monkeypatch.setattr(_publish_module, 'publish', _unexpected)
    capsys.readouterr()
    assert _cli.main(['release', '1.0.0', '--project', project.name, '--yes']) == 1
    error = capsys.readouterr().err
    assert ('--continue' if not complete else 'pubanon inspect') in error
    if complete:
        assert 'pubanon publish' in error


def test_release_unknown_rewrite_outcome_requires_resolution_before_inspection(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    _candidate.save_candidate(
        paths,
        project.name,
        replace(
            record,
            state=_candidate.STATE_UNKNOWN_OUTCOME,
            prestate_main='a' * 40,
            prestate_tags={'v1.0.0': 'a' * 40},
            replaces='a' * 40,
            replace_scope='one',
            tip=record.commit,
            poststate_tags={'v1.0.0': record.commit},
        ),
    )

    def _unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail('unknown outcome reached inspection or publication')

    monkeypatch.setattr(_inspect, 'inspection_copy', _unexpected)
    monkeypatch.setattr(_publish_module, 'publish', _unexpected)
    capsys.readouterr()
    assert _cli.main(['release', '1.0.0', '--project', project.name, '--yes']) == 1
    error = capsys.readouterr().err
    assert 'snapshot has an unknown publish outcome' in error
    assert 'pubanon publish --project=demo --resolve' in error


def test_release_uses_publish_exposure_acknowledgment_gate(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exposure = 'historical-private-canary'
    source = make_repo(files={'README.md': f'{exposure}\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    record_published_main(paths, project, record)
    _config.save_project(paths, replace(project, extra_terms=(exposure,)))
    monkeypatch.chdir(source)
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account='anonymous-account',
        visibility=project.visibility,
    )

    def _verified(_ctx: _publish_module.PublishContext) -> _publish_module._Verified:
        return _publish_module._Verified(destination, _host.RemoteRefs(record.commit, {}))

    calls: list[tuple[_audit.AuditFinding, ...]] = []

    def _acknowledge(
        _paths: _config.Paths,
        configs: tuple[_config.GlobalConfig, _config.ProjectConfig],
        audit: _audit.HistoryAudit,
        *,
        unattended: bool,
    ) -> _config.ProjectConfig:
        assert unattended
        assert _audit.exposure_digest(audit.findings)
        calls.append(audit.findings)
        return configs[1]

    def _skip_push(
        _ctx: _publish_module.PublishContext,
        _verified: _publish_module._Verified,
    ) -> None:
        pass

    monkeypatch.setattr(_publish_module, '_verify_for_publish', _verified)
    monkeypatch.setattr(_publish_module, '_acknowledge_exposure', _acknowledge)
    monkeypatch.setattr(_publish_module, '_push_candidate', _skip_push)

    assert _cli.main(['release', '1.0.0', '--yes']) == 0
    assert len(calls) == 1


def test_release_yes_allows_first_release_new_paths_without_prompt(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    monkeypatch.chdir(source)
    published: list[None] = []

    def _unexpected_prompt(_prompt: str) -> str:
        pytest.fail('release --yes requested interactive input')

    def _publish(_ctx: object) -> None:
        published.append(None)

    monkeypatch.setattr('builtins.input', _unexpected_prompt)
    monkeypatch.setattr(_publish_module, 'publish', _publish)

    assert _cli.main(['release', '1.0.0', '--yes']) == 0

    output = capsys.readouterr()
    assert '  README.md' in output.out
    saved = paths.data_dir / 'projects' / project.name / 'new-paths.json'
    assert json.loads(saved.read_text(encoding='utf-8')) == ['README.md']
    assert escape(str(saved)) in output.out
    assert output.err == ''
    assert _candidate.load_candidate(paths, project.name) is not None
    assert published == [None]
