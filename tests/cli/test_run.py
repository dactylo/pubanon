import argparse
import json
import os
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Never

import pytest

from pubanon import (
    _cli,
    _config,
)
from pubanon._cli import _prepare, _workflow
from pubanon._cli._context import load_context
from pubanon._errors import ObservationTimeoutError, OperationCanceled, PubanonError
from pubanon._remote import _host, _lifecycle, _package, _stage, _watch
from pubanon._remote import _publish as _publish_module
from pubanon._snapshots import _candidate
from pubanon._terminal import escape, next_step
from tests._cli import (
    gh_calls,
    prepare,
    published_package_reveal_context,
    save_workflows,
    stub_remote,
)
from tests._git import git_env
from tests._permissions import assert_private_mode
from tests._setup import configure
from tests.remote.test_stage import _git


def _assert_staged_check_blocks_publish(
    project: _config.ProjectConfig,
    candidate: _candidate.CandidateRecord,
    paths: _config.Paths,
    final_host: Path,
    capsys: pytest.CaptureFixture[str],
) -> str:
    fake_gh = Path(os.environ['PUBANON_FAKE_GH_RESPONSES'])
    responses = json.loads(fake_gh.read_text())
    stage_repo = _config.stage_repository(project)
    check_args = (
        f'repos/{project.owner}/{stage_repo}/commits/{candidate.commit}'
        '/check-runs?check_name=build&filter=latest&per_page=100'
    )
    for status, conclusion, diagnostic in (
        ('in_progress', None, 'pending'),
        ('completed', 'failure', 'failed'),
    ):
        responses.append(
            {
                'args': ['api', check_args],
                'stdout': json.dumps(
                    {
                        'total_count': 1,
                        'check_runs': [
                            {
                                'name': 'build',
                                'head_sha': candidate.commit,
                                'status': status,
                                'conclusion': conclusion,
                            }
                        ],
                    }
                ),
            }
        )
        fake_gh.write_text(json.dumps(responses))
        capsys.readouterr()
        assert _cli.main(['publish', '--project', project.name, '--yes']) == 1
        assert diagnostic in capsys.readouterr().err
        assert _git(final_host, 'for-each-ref') == b''
        assert _candidate.load_candidate(paths, project.name) == candidate
        responses.pop()
    return check_args


def _require_test_account(
    expected: str,
    *,
    authority: str,
    timeout: float | None = None,
) -> str:
    del authority, timeout
    return expected


def test_run_candidate_bound_stage_refuses_mismatched_tag_before_staging_preflight(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, _record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    save_workflows(paths, project, ship=('stage push', 'publish'))

    def _unexpected_host() -> object:
        pytest.fail('mismatched snapshot reached staging or publication host reads')

    monkeypatch.setattr(_host, 'verify_destination', _unexpected_host)
    capsys.readouterr()

    assert (
        _cli.main(['run', 'ship', '--project', project.name, '--version', '2.0.0', '--yes']) == 1
    )

    error = capsys.readouterr().err
    assert 'the prepared snapshot has a different release tag' in error
    assert 'Expected: v2.0.0' in error
    assert 'Found: v1.0.0' in error
    assert 'pubanon discard' in error


def test_run_does_not_relax_visibility_mismatch_without_reveal_suffix(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    store = _candidate.store_path(paths, project.name)
    for ref in ('refs/heads/main', 'refs/tags/v1.0.0'):
        subprocess.run(
            ['git', '-C', store, 'update-ref', ref, record.commit], check=True, env=git_env()
        )
    _candidate.discard(paths, project.name, force=True)
    project = save_workflows(paths, project, observe=('publish', 'repo watch'))
    verified_visibilities: list[str] = []

    class _HostModule:
        @staticmethod
        def _verify_destination(
            _paths: _config.Paths,
            _global: _config.GlobalConfig,
            selected: _config.ProjectConfig,
        ) -> _host.Destination:
            verified_visibilities.append(selected.visibility)
            raise PubanonError(
                'repository visibility does not match configuration',
                fact='repository visibility',
                expected='private',
                actual='public',
            )

    monkeypatch.setattr(_host, 'verify_destination', _HostModule._verify_destination)
    capsys.readouterr()

    assert (
        _cli.main(['run', 'observe', '--project', project.name, '--version', '1.0.0', '--yes'])
        == 1
    )

    assert verified_visibilities == ['private']
    assert 'repository visibility does not match configuration' in capsys.readouterr().err


def test_run_ending_at_already_published_version_is_a_noop(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    store = _candidate.store_path(paths, project.name)
    for ref in ('refs/heads/main', 'refs/tags/v1.0.0'):
        subprocess.run(
            ['git', '-C', store, 'update-ref', ref, record.commit], check=True, env=git_env()
        )
    _candidate.discard(paths, project.name, force=True)
    project = save_workflows(paths, project, ship=('prepare', 'publish'))
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account='anonymous-account',
        visibility=project.visibility,
    )
    stub_remote(
        monkeypatch,
        destination,
        _host.RemoteRefs(record.commit, {'v1.0.0': record.commit}),
    )
    capsys.readouterr()

    assert (
        _cli.main(['run', 'ship', '--project', project.name, '--version', '1.0.0', '--yes']) == 0
    )

    output = capsys.readouterr().out
    assert 'Nothing to do: v1.0.0 is already published.' in output
    assert 'Next: no action is required.' not in output


def test_run_fresh_publish_watches_the_exact_published_version_commit(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    project = save_workflows(
        paths,
        project,
        ship=('prepare', 'inspect', 'publish', 'repo watch'),
    )
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account=global_config.accounts['github'],
        visibility=project.visibility,
    )
    stub_remote(monkeypatch, destination, _host.RemoteRefs(None, {}))
    published: list[str] = []
    prompts: list[str] = []
    watched: list[str] = []

    def _publish(ctx: _publish_module.PublishContext) -> None:
        inspection = (
            paths.data_dir / 'projects' / project.name / 'inspections' / ctx._record.commit
        )
        assert (inspection / 'README.md').read_text(encoding='utf-8') == 'public\n'
        assert ctx._yes is False
        assert ctx._unattended is False
        published.append(ctx._record.commit)
        store = _candidate.store_path(paths, project.name)
        subprocess.run(
            ['git', '-C', store, 'update-ref', 'refs/heads/main', ctx._record.commit],
            check=True,
            env=git_env(),
        )
        subprocess.run(
            ['git', '-C', store, 'update-ref', 'refs/tags/v1.0.0', ctx._record.commit],
            check=True,
            env=git_env(),
        )
        _candidate.discard(paths, project.name, force=True)

    class _WatchModule:
        @staticmethod
        def _watch_ci(
            _paths: _config.Paths,
            _global: _config.GlobalConfig,
            _project: _config.ProjectConfig,
            *,
            staged: bool,
            version: str | None,
            timeout: int,
        ) -> None:
            del timeout
            assert staged is False
            assert version == '1.0.0'
            main, tags = _candidate.published_refs(paths, project.name)
            assert main == tags['v1.0.0']
            watched.append(tags['v1.0.0'])

    def _approve(prompt: str) -> str:
        if prompt.startswith('Use this selection?'):
            assert _candidate.load_candidate(paths, project.name) is None
            return 'c'
        prompts.append(prompt)
        return 'yes'

    monkeypatch.setattr('builtins.input', _approve)
    monkeypatch.setattr(_publish_module, 'publish', _publish)
    monkeypatch.setattr(_watch, 'watch_ci', _WatchModule._watch_ci)

    assert (
        _cli.main(
            [
                'run',
                'ship',
                '--project',
                project.name,
                '--version',
                '1.0.0',
                '--acknowledge-new-paths',
            ]
        )
        == 0
    )

    assert watched == published
    assert prompts == []
    output = capsys.readouterr().out
    assert f'publish to {project.owner}/{project.repo} on main' in output
    assert 'create release tag v1.0.0' in output
    assert 'Workflow ship completed.' in output


def test_run_holds_project_lock_for_the_complete_workflow(
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _global_config, project = configured_project
    assert project is not None
    save_workflows(paths, project, checks=('repo watch',))

    class _WatchModule:
        @staticmethod
        def _watch_ci(*_args: object, **_kwargs: object) -> None:
            with (
                pytest.raises(PubanonError, match='another Pubanon command is running'),
                _config.project_lock(paths, project.name),
            ):
                pytest.fail('run released its project lock before the last stage')

    monkeypatch.setattr(_watch, 'watch_ci', _WatchModule._watch_ci)

    assert _cli.main(['run', 'checks', '--project', project.name, '--yes']) == 0


def test_run_inspect_refuses_invalid_retained_objects_before_replacing_copy(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    save_workflows(paths, project, review=('inspect',))
    inspection = _candidate.materialize_inspect(paths, project.name, record)
    subprocess.run(
        [
            'git',
            '-C',
            _candidate.store_path(paths, project.name),
            'update-ref',
            '-d',
            _candidate.CANDIDATE_REF,
        ],
        check=True,
        env=git_env(),
    )
    capsys.readouterr()

    assert _cli.main(['run', 'review', '--project', project.name, '--yes']) == 1

    assert 'saved snapshot commit could not be verified' in capsys.readouterr().err
    assert (inspection / 'README.md').read_text(encoding='utf-8') == 'public\n'
    assert_private_mode(inspection / 'README.md', 0o400)


def test_run_observation_requires_no_mutation_confirmation(
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _global_config, project = configured_project
    assert project is not None
    save_workflows(paths, project, checks=('repo watch',))

    watched: list[None] = []

    class _WatchModule:
        @staticmethod
        def _watch_ci(*_args: object, **_kwargs: object) -> None:
            watched.append(None)

    def _decline(_prompt: str) -> str:
        pytest.fail('observation workflow requested mutation approval')

    monkeypatch.setattr(_watch, 'watch_ci', _WatchModule._watch_ci)
    monkeypatch.setattr('builtins.input', _decline)

    assert _cli.main(['run', 'checks', '--project', project.name]) == 0
    assert watched == [None]
    assert 'completed' in capsys.readouterr().out


@pytest.mark.parametrize('raw', [False, True])
def test_run_post_publish_cancellation_preserves_private_details_and_completed_fact(
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    monkeypatch: pytest.MonkeyPatch,
    raw: bool,
) -> None:
    paths, _global_config, project = configured_project
    assert project is not None
    ctx = load_context(paths, project.name)
    plan = _workflow._RunPlan(
        ctx,
        ('publish', 'repo watch'),
        ('repo watch',),
        None,
        'v1.0.0',
        _already_published=True,
        _stage_creation_needed=False,
    )
    error = OperationCanceled('operation cancelled', fact='watch stopped', actual='private-canary')

    def _cancel(*_args: object) -> None:
        if raw:
            raise KeyboardInterrupt
        raise error

    monkeypatch.setattr(_workflow, '_execute_run_stage', _cancel)
    with pytest.raises(OperationCanceled) as raised:
        _workflow._execute_run(argparse.Namespace(version='1.0.0'), plan)
    if not raw:
        assert raised.value is error
        assert error.actual == 'private-canary'
    error = raised.value
    assert error.fact is not None
    assert 'The publication itself completed.' in error.fact
    if not raw:
        assert 'watch stopped' in error.fact
    assert error.recovery is not None
    assert 'pubanon repo watch --project=demo --version 1.0.0' in error.recovery


@pytest.mark.parametrize('case', [('1.0.0', False), ('1.0.0', True), (None, True)])
def test_run_post_publish_failure_names_every_remaining_stepwise_command(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[str | None, bool],
) -> None:
    version, prepared = case
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    project = save_workflows(
        paths,
        project,
        ship=('prepare', 'publish', 'repo watch', 'repo reveal', 'package watch'),
    )
    version_args = ['--version', version] if version is not None else []
    if prepared:
        assert _cli.main(['prepare', '--project', project.name, *version_args, '--yes']) == 0
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account=global_config.accounts['github'],
        visibility=project.visibility,
    )
    stub_remote(monkeypatch, destination, _host.RemoteRefs(None, {}))

    def _publish(ctx: _publish_module.PublishContext) -> None:
        store = _candidate.store_path(paths, project.name)
        refs = ['refs/heads/main']
        if version is not None:
            refs.append(f'refs/tags/v{version}')
        for ref in refs:
            subprocess.run(
                ['git', '-C', store, 'update-ref', ref, ctx._record.commit],
                check=True,
                env=git_env(),
            )
        _candidate.discard(paths, project.name, force=True)

    class _WatchModule:
        @staticmethod
        def _watch_ci(*_args: object, **_kwargs: object) -> None:
            raise PubanonError('CI observation failed', recovery='rerun the watch')

    monkeypatch.setattr(_publish_module, 'publish', _publish)
    monkeypatch.setattr(_watch, 'watch_ci', _WatchModule._watch_ci)

    assert (
        _cli.main(
            [
                'run',
                'ship',
                '--project',
                project.name,
                *(version_args if not prepared else []),
                '--acknowledge-new-paths',
                '--yes',
            ]
        )
        == 1
    )

    error = capsys.readouterr().err
    version_option = f' --version {version}' if version is not None else ''
    assert 'Next:' in error
    assert f'pubanon repo watch --project=demo{version_option}\n' in error
    assert 'pubanon repo reveal --project=demo\n' in error
    assert f'pubanon package watch --project=demo{version_option}\n' in error


@pytest.mark.parametrize('implicit_project', [False, True])
def test_run_post_publish_failure_preserves_real_shaped_fact_and_recovery(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    implicit_project: bool,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    monkeypatch.chdir(source if implicit_project else tmp_path)
    store = _candidate.store_path(paths, project.name)
    for ref in ('refs/heads/main', 'refs/tags/v1.0.0'):
        subprocess.run(
            ['git', '-C', store, 'update-ref', ref, record.commit], check=True, env=git_env()
        )
    _candidate.discard(paths, project.name, force=True)
    project = save_workflows(
        paths,
        project,
        observe=('publish', 'repo watch', 'package watch'),
    )
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account='anonymous-account',
        visibility=project.visibility,
    )
    stub_remote(
        monkeypatch,
        destination,
        _host.RemoteRefs(record.commit, {'v1.0.0': record.commit}),
    )

    class _WatchModule:
        @staticmethod
        def _watch_ci(*_args: object, **_kwargs: object) -> None:
            raise PubanonError(
                'required CI checks are not acceptable',
                fact='build: failed',
                recovery=(
                    'rerun the failed workflow, then run `pubanon repo watch --version 1.0.0`'
                ),
            )

    monkeypatch.setattr(_watch, 'watch_ci', _WatchModule._watch_ci)
    capsys.readouterr()

    assert (
        _cli.main(['run', 'observe', '--project', project.name, '--version', '1.0.0', '--yes'])
        == 1
    )

    error = capsys.readouterr().err
    assert '  build: failed' in error
    assert 'Next: rerun the failed workflow, then run' in error
    qualifier = '' if implicit_project else ' --project=demo'
    assert error.count(f'pubanon repo watch{qualifier} --version 1.0.0') == 1
    assert f'pubanon package watch{qualifier} --version 1.0.0\n' in error


def test_run_prepare_and_inspect_materializes_exact_read_only_candidate_copy(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(
        files={
            'README.md': b'public\x00bytes\n',
            'pyproject.toml': b'[project]\nversion = "0.0.1"\n',
            'bin/tool': b'#!/bin/sh\nprintf "public\\n"\n',
        },
        symlinks={'readme-link': 'README.md'},
    )
    subprocess.run(['git', '-C', source, 'add', 'bin/tool'], check=True, env=git_env())
    subprocess.run(
        ['git', '-C', source, 'update-index', '--chmod=+x', 'bin/tool'],
        check=True,
        env=git_env(),
    )
    subprocess.run(
        ['git', '-C', source, 'commit', '-qm', 'make tool executable'],
        check=True,
        env=git_env(),
    )
    paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    save_workflows(paths, project, review=('prepare', 'inspect'))

    assert (
        _cli.main(
            [
                'run',
                'review',
                '--project',
                project.name,
                '--acknowledge-new-paths',
                '--yes',
            ]
        )
        == 0
    )

    record = _candidate.load_candidate(paths, project.name)
    assert record is not None
    inspection = paths.data_dir / 'projects' / project.name / 'inspections' / record.commit
    output = capsys.readouterr().out
    assert 'Review copy' in output
    assert escape(str(inspection)) in output
    assert 'The snapshot is prepared but has not been published.' in output
    assert 'no action is required' not in output
    assert (inspection / 'README.md').read_bytes() == b'public\x00bytes\n'
    assert (inspection / 'bin/tool').read_bytes() == b'#!/bin/sh\nprintf "public\\n"\n'
    assert_private_mode(inspection, 0o500)
    assert_private_mode(inspection / 'bin', 0o500)
    assert_private_mode(inspection / 'README.md', 0o400)
    assert_private_mode(inspection / 'bin/tool', 0o500)
    assert os.fsencode((inspection / 'readme-link').readlink()) == b'README.md'

    assert _cli.main(['run', 'review', '--project', project.name, '--yes']) == 0

    assert (inspection / 'README.md').read_bytes() == b'public\x00bytes\n'
    output = capsys.readouterr().out
    assert 'Review copy' in output
    assert escape(str(inspection)) in output
    assert record.commit in output


def test_run_prepare_refuses_damaged_retained_candidate(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, _record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    save_workflows(paths, project, local=('prepare',))
    subprocess.run(
        [
            'git',
            '-C',
            _candidate.store_path(paths, project.name),
            'update-ref',
            '-d',
            _candidate.CANDIDATE_REF,
        ],
        check=True,
        env=git_env(),
    )
    capsys.readouterr()

    assert (
        _cli.main(['run', 'local', '--project', project.name, '--version', '1.0.0', '--yes']) == 1
    )
    error = capsys.readouterr().err
    assert 'saved snapshot commit could not be verified' in error
    assert 'pubanon discard' in error


def test_run_prepare_refuses_stale_fingerprint(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, _record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    changed = replace(
        project,
        extra_terms=('new-private-term',),
        workflows={'local': ('prepare',)},
    )
    _config.save_project(paths, changed)
    capsys.readouterr()

    assert (
        _cli.main(['run', 'local', '--project', project.name, '--version', '1.0.0', '--yes']) == 1
    )
    error = capsys.readouterr().err
    assert 'the configuration changed after the snapshot was prepared' in error
    assert 'pubanon discard' in error


def test_run_prepare_resumes_matching_verified_candidate_without_rebuild_or_host_contact(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    save_workflows(paths, project, local=('prepare',))

    def _unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail('matching retained snapshot was rebuilt or contacted the host')

    monkeypatch.setattr(_prepare, 'build_candidate', _unexpected)
    monkeypatch.setattr(_host, 'verify_destination', _unexpected)
    capsys.readouterr()

    assert (
        _cli.main(['run', 'local', '--project', project.name, '--version', '1.0.0', '--yes']) == 0
    )

    assert _candidate.load_candidate(paths, project.name) == record
    assert 'Workflow local (direct route): prepare' in capsys.readouterr().out


def test_run_preserves_publish_exception_when_reclassification_is_not_published(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    project = save_workflows(paths, project, ship=('publish', 'repo watch'))
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account='anonymous-account',
        visibility=project.visibility,
    )
    remote_reads = 0

    class _HostModule:
        @staticmethod
        def _ls_remote(*_args: object) -> _host.RemoteRefs:
            nonlocal remote_reads
            remote_reads += 1
            return _host.RemoteRefs(None, {})

        @staticmethod
        def _verify_destination(*_args: object) -> _host.Destination:
            return destination

    def _publish(_ctx: _publish_module.PublishContext) -> None:
        raise PubanonError(
            'publication did not land',
            fact='original publication fact',
            recovery='pubanon publish --project demo --version 1.0.0',
        )

    monkeypatch.setattr(_host, 'verify_destination', _HostModule._verify_destination)
    monkeypatch.setattr(_host, 'ls_remote', _HostModule._ls_remote)
    monkeypatch.setattr(_publish_module, 'publish', _publish)
    capsys.readouterr()

    assert (
        _cli.main(['run', 'ship', '--project', project.name, '--version', '1.0.0', '--yes']) == 1
    )

    # The retained candidate keeps recovery with the publication primitive;
    # workflow error handling must not reclassify away its evidence.
    assert remote_reads == 1
    assert _candidate.load_candidate(paths, project.name) == record
    error = capsys.readouterr().err
    assert error.startswith('error: publication did not land\n')
    assert 'original publication fact' in error
    assert 'Next: pubanon publish --project demo --version 1.0.0' in error


def test_run_publish_requires_valid_version_before_host_reads(
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _global_config, project = configured_project
    assert project is not None
    save_workflows(paths, project, ship=('publish',))

    def _unexpected_host() -> object:
        pytest.fail('invalid run version reached host reads')

    monkeypatch.setattr(_host, 'verify_destination', _unexpected_host)

    assert _cli.main(['run', 'ship', '--project', project.name]) == 1
    assert 'a prepared snapshot or --version is required' in capsys.readouterr().err
    assert _cli.main(['run', 'ship', '--project', project.name, '--version', 'not-a-version']) == 1
    assert 'version must have X.Y.Z form' in capsys.readouterr().err


def test_run_published_resume_allows_public_host_partial_to_reach_reveal(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    store = _candidate.store_path(paths, project.name)
    for ref in ('refs/heads/main', 'refs/tags/v1.0.0'):
        subprocess.run(
            ['git', '-C', store, 'update-ref', ref, record.commit], check=True, env=git_env()
        )
    _candidate.discard(paths, project.name, force=True)
    project = save_workflows(paths, project, reveal=('publish', 'repo reveal'))
    verified_visibilities: list[str] = []
    revealed: list[str] = []

    class _HostModule:
        @staticmethod
        def _ls_remote(*_args: object) -> _host.RemoteRefs:
            return _host.RemoteRefs(record.commit, {'v1.0.0': record.commit})

        @staticmethod
        def _verify_destination(
            _paths: _config.Paths,
            _global: _config.GlobalConfig,
            selected: _config.ProjectConfig,
        ) -> _host.Destination:
            verified_visibilities.append(selected.visibility)
            if selected.visibility == 'private':
                raise PubanonError(
                    'repository visibility does not match configuration',
                    fact='repository visibility',
                    expected='private',
                    actual='public',
                )
            return _host.Destination(
                adapter=selected.host,
                owner=selected.owner,
                repo=selected.repo,
                url='https://github.com/anonymous-account/public-repo.git',
                account='anonymous-account',
                visibility='public',
            )

    class _LifecycleModule:
        @staticmethod
        def _reveal(
            _paths: _config.Paths,
            _global: _config.GlobalConfig,
            selected: _config.ProjectConfig,
        ) -> None:
            revealed.append(selected.visibility)

    def _approve(_prompt: str) -> str:
        return 'yes'

    monkeypatch.setattr(_host, 'verify_destination', _HostModule._verify_destination)
    monkeypatch.setattr(_host, 'ls_remote', _HostModule._ls_remote)
    monkeypatch.setattr(_lifecycle, 'reveal', _LifecycleModule._reveal)
    monkeypatch.setattr('builtins.input', _approve)

    assert _cli.main(['run', 'reveal', '--project', project.name, '--version', '1.0.0']) == 0

    assert verified_visibilities == ['private', 'public']
    assert revealed == ['private']


def test_run_published_resume_names_interactive_reveal_before_stepwise_command(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    store = _candidate.store_path(paths, project.name)
    for ref in ('refs/heads/main', 'refs/tags/v1.0.0'):
        subprocess.run(
            ['git', '-C', store, 'update-ref', ref, record.commit], check=True, env=git_env()
        )
    _candidate.discard(paths, project.name, force=True)
    project = save_workflows(paths, project, reveal=('publish', 'repo reveal'))
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account='anonymous-account',
        visibility=project.visibility,
    )
    stub_remote(
        monkeypatch,
        destination,
        _host.RemoteRefs(record.commit, {'v1.0.0': record.commit}),
    )
    capsys.readouterr()

    assert (
        _cli.main(['run', 'reveal', '--project', project.name, '--version', '1.0.0', '--yes']) == 1
    )

    error = capsys.readouterr().err
    assert 'run pubanon repo reveal --project=demo interactively' in error
    assert 'Next:' in error
    assert error.count('pubanon repo reveal --project=demo') == 1


def test_run_reclassifies_landed_publish_exception_before_stepwise_recovery(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    project = save_workflows(paths, project, ship=('publish', 'repo watch', 'package watch'))
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account='anonymous-account',
        visibility=project.visibility,
    )
    remote_reads = 0

    class _HostModule:
        @staticmethod
        def _ls_remote(*_args: object) -> _host.RemoteRefs:
            nonlocal remote_reads
            remote_reads += 1
            if remote_reads == 1:
                return _host.RemoteRefs(None, {})
            return _host.RemoteRefs(record.commit, {'v1.0.0': record.commit})

        @staticmethod
        def _verify_destination(*_args: object) -> _host.Destination:
            return destination

    def _publish(ctx: _publish_module.PublishContext) -> None:
        store = _candidate.store_path(paths, project.name)
        for ref in ('refs/heads/main', 'refs/tags/v1.0.0'):
            subprocess.run(
                ['git', '-C', store, 'update-ref', ref, ctx._record.commit],
                check=True,
                env=git_env(),
            )
        _candidate.discard(paths, project.name, force=True)
        raise PubanonError(
            'publication landed and must not be retried',
            fact='remote contains unrelated drift',
            recovery='pubanon repo verify',
        )

    monkeypatch.setattr(_host, 'verify_destination', _HostModule._verify_destination)
    monkeypatch.setattr(_host, 'ls_remote', _HostModule._ls_remote)
    monkeypatch.setattr(_publish_module, 'publish', _publish)
    capsys.readouterr()

    assert (
        _cli.main(['run', 'ship', '--project', project.name, '--version', '1.0.0', '--yes']) == 1
    )

    error = capsys.readouterr().err
    assert remote_reads == 2
    assert 'publication landed and must not be retried' in error
    assert 'Next: pubanon repo verify' in error
    assert 'pubanon publish' not in error
    assert 'Next:' in error
    assert 'pubanon repo watch --project=demo --version 1.0.0\n' in error
    assert 'pubanon package watch --project=demo --version 1.0.0\n' in error


def test_run_refuses_remote_and_local_version_mismatch_showing_both_sides(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    save_workflows(paths, project, ship=('publish',))
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account=global_config.accounts['github'],
        visibility=project.visibility,
    )
    stub_remote(monkeypatch, destination, _host.RemoteRefs('a' * 40, {'v1.0.0': 'a' * 40}))

    assert (
        _cli.main(['run', 'ship', '--project', project.name, '--version', '1.0.0', '--yes']) == 1
    )

    error = capsys.readouterr().err
    assert 'published version state is ambiguous' in error
    assert 'Expected: local v1.0.0: absent' in error
    assert f'Found: host v1.0.0: {"a" * 40}' in error


@pytest.mark.parametrize('complete', [False, True])
def test_run_refuses_retained_rewrite_before_host_or_stage(
    complete: bool,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    save_workflows(paths, project, ship=('stage push', 'publish'))
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
        pytest.fail('retained rewrite reached host or staging')

    monkeypatch.setattr(_host, 'verify_destination', _unexpected)
    monkeypatch.setattr(_stage, 'staging_repository_visible', _unexpected)
    capsys.readouterr()
    assert (
        _cli.main(['run', 'ship', '--project', project.name, '--version', '1.0.0', '--yes']) == 1
    )
    error = capsys.readouterr().err
    assert ('--continue' if not complete else 'pubanon inspect') in error
    if complete:
        assert 'follow its next step' in error


def test_run_reloads_public_configuration_after_reveal_before_watch_package(
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _global_config, project = configured_project
    assert project is not None
    save_workflows(paths, project, observe=('repo reveal', 'package watch'))
    watched_visibilities: list[str] = []

    class _LifecycleModule:
        @staticmethod
        def _reveal(
            actual_paths: _config.Paths,
            _global: _config.GlobalConfig,
            selected: _config.ProjectConfig,
        ) -> None:
            _config.save_project(actual_paths, replace(selected, visibility='public'))

    class _WatchModule:
        @staticmethod
        def _watch_package(
            _paths: _config.Paths,
            _global: _config.GlobalConfig,
            selected: _config.ProjectConfig,
            *,
            version: str | None,
            timeout: int,
        ) -> None:
            assert version is None
            assert timeout == _watch.DEFAULT_TIMEOUT
            watched_visibilities.append(selected.visibility)

    monkeypatch.setattr(_lifecycle, 'reveal', _LifecycleModule._reveal)
    monkeypatch.setattr(_watch, 'watch_package', _WatchModule._watch_package)

    def _approve(_prompt: str) -> str:
        return 'yes'

    monkeypatch.setattr('builtins.input', _approve)

    assert _cli.main(['run', 'observe', '--project', project.name]) == 0
    assert watched_visibilities == ['public']
    assert _config.load_project(paths, project.name).visibility == 'public'


def test_run_rerun_after_publication_executes_only_post_publish_stage(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    store = _candidate.store_path(paths, project.name)
    for ref in ('refs/heads/main', 'refs/tags/v1.0.0'):
        subprocess.run(
            ['git', '-C', store, 'update-ref', ref, record.commit], check=True, env=git_env()
        )
    _candidate.discard(paths, project.name, force=True)
    project = save_workflows(
        paths,
        replace(project, publication_route='staged'),
        ship=('prepare', 'inspect', 'publish', 'repo watch'),
    )
    assert _stage.load_stage(paths, project.name) is None
    global_config = _config.load_global(paths)
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account=global_config.accounts['github'],
        visibility=project.visibility,
    )
    stub_remote(
        monkeypatch,
        destination,
        _host.RemoteRefs(record.commit, {'v1.0.0': record.commit}),
    )
    watched: list[str | None] = []

    def _unexpected(*_args: object, **_kwargs: object) -> None:
        pytest.fail('published workflow repeated prepare or publish')

    class _WatchModule:
        @staticmethod
        def _watch_ci(*_args: object, version: str | None, **_kwargs: object) -> None:
            watched.append(version)

    monkeypatch.setattr(_prepare, 'build_candidate', _unexpected)
    monkeypatch.setattr(_publish_module, 'publish', _unexpected)
    monkeypatch.setattr(_watch, 'watch_ci', _WatchModule._watch_ci)
    capsys.readouterr()

    assert (
        _cli.main(['run', 'ship', '--project', project.name, '--version', '1.0.0', '--yes']) == 0
    )

    assert watched == ['1.0.0']
    output = capsys.readouterr().out
    assert 'Workflow ship (staged route): prepare, inspect, publish, repo watch' in output
    assert 'Repo watch' in output
    assert '\nPrepare\n' not in output
    assert '\nPublish\n' not in output


def test_run_reveal_leaves_both_decisions_to_the_primitive(
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _global_config, project = configured_project
    assert project is not None
    save_workflows(paths, project, reveal=('repo reveal',))
    prompts: list[str] = []
    received: list[tuple[str, str]] = []

    class _LifecycleModule:
        @staticmethod
        def _reveal(
            _paths: _config.Paths,
            _global: _config.GlobalConfig,
            selected: _config.ProjectConfig,
        ) -> None:
            received.append((selected.name, selected.visibility))

    def _approve(prompt: str) -> str:
        prompts.append(prompt)
        return 'yes'

    monkeypatch.setattr(_lifecycle, 'reveal', _LifecycleModule._reveal)
    monkeypatch.setattr('builtins.input', _approve)

    assert _cli.main(['run', 'reveal', '--project', project.name]) == 0

    assert received == [(project.name, project.visibility)]
    assert prompts == []
    assert 'make the final repository public' in capsys.readouterr().out


def test_run_reveal_package_timeout_keeps_already_public_recovery_read_only(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, project = published_package_reveal_context(
        tmp_path,
        make_repo,
        monkeypatch,
        host_visibility='public',
        workflows={'reveal': ('publish', 'repo reveal')},
    )
    private = 'private composed timeout detail'

    def _timeout(*_args: object) -> _package.PackageRelease | None:
        raise ObservationTimeoutError(
            stdout=f'{private} stdout'.encode(),
            stderr=f'{private} stderr'.encode(),
        )

    def _unexpected_mutation(*_args: object) -> None:
        pytest.fail('already-public package recovery mutated host visibility')

    def _approve(_prompt: str) -> str:
        return 'yes'

    monkeypatch.setattr(_package, 'release_state', _timeout)
    monkeypatch.setattr(_lifecycle, 'set_visibility', _unexpected_mutation)
    monkeypatch.setattr('builtins.input', _approve)
    capsys.readouterr()

    assert _cli.main(['run', 'reveal', '--project', project.name, '--version', '1.0.0']) == 1

    result = capsys.readouterr()
    rendered = result.out + result.err
    assert 'error: the repository is already public but verification failed' in result.err
    assert 'package release observation timed out' in result.err
    assert 'rerun pubanon repo reveal --project=demo to retry package observation' in result.err
    assert result.err.endswith('then rerun pubanon repo reveal --project=demo\n')
    assert 'Then finish stepwise:' not in result.err
    assert 'Traceback' not in rendered
    assert private not in rendered
    assert 'Workflow reveal completed.' not in result.out
    assert 'Repository is public and verified.' not in result.out
    assert _config.load_project(paths, project.name).visibility == 'private'


def test_run_reveal_partial_absent_tags_preserves_private_verification_error(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, _record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    project = save_workflows(
        paths,
        project,
        reveal=('prepare', 'publish', 'repo reveal'),
    )
    verified_visibilities: list[str] = []

    class _HostModule:
        @staticmethod
        def _ls_remote(*_args: object) -> _host.RemoteRefs:
            return _host.RemoteRefs(None, {})

        @staticmethod
        def _verify_destination(
            _paths: _config.Paths,
            _global: _config.GlobalConfig,
            selected: _config.ProjectConfig,
        ) -> _host.Destination:
            verified_visibilities.append(selected.visibility)
            if selected.visibility == 'private':
                raise PubanonError(
                    'original private destination mismatch',
                    fact='destination verification failed',
                    recovery='pubanon repo verify',
                )
            return _host.Destination(
                adapter=selected.host,
                owner=selected.owner,
                repo=selected.repo,
                url='https://github.com/anonymous-account/public-repo.git',
                account='anonymous-account',
                visibility='public',
            )

    monkeypatch.setattr(_host, 'verify_destination', _HostModule._verify_destination)
    monkeypatch.setattr(_host, 'ls_remote', _HostModule._ls_remote)
    capsys.readouterr()

    assert (
        _cli.main(['run', 'reveal', '--project', project.name, '--version', '1.0.0', '--yes']) == 1
    )

    assert verified_visibilities == ['private', 'public']
    captured = capsys.readouterr()
    assert 'error: original private destination mismatch' in captured.err
    assert 'destination verification failed' in captured.err
    assert 'Next: pubanon repo verify --project=demo' in captured.err
    assert 'Workflow:' not in captured.out


@pytest.mark.parametrize('visible', [False, True])
def test_run_stage_push_preflights_creation_and_leaves_approval_to_primitive(
    visible: bool,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, _record = prepare(tmp_path, source, monkeypatch)
    project = save_workflows(paths, project, staging=('stage push',))
    visibility_reads: list[tuple[str, str]] = []
    received: list[tuple[_config.ProjectConfig, bool]] = []

    class _HostModule:
        @staticmethod
        def _repository_visible(owner: str, repo: str) -> bool:
            visibility_reads.append((owner, repo))
            return visible

    class _StageModule:
        @staticmethod
        def _stage_push(
            _paths: _config.Paths,
            _global: _config.GlobalConfig,
            selected: _config.ProjectConfig,
            *,
            yes: bool,
        ) -> None:
            received.append((selected, yes))

    def _approve(_prompt: str) -> str:
        return 'yes'

    monkeypatch.setattr(_stage, 'require_authenticated_account', _require_test_account)
    monkeypatch.setattr(_stage, 'repository_visible', _HostModule._repository_visible)
    monkeypatch.setattr(_stage, 'stage_push', _StageModule._stage_push)
    monkeypatch.setattr('builtins.input', _approve)
    capsys.readouterr()

    assert _cli.main(['run', 'staging', '--project', project.name]) == 0

    assert visibility_reads == [(project.owner, f'{project.repo}-stage')] * 2
    assert received == [(project, False)]
    output = capsys.readouterr().out
    assert 'replace the files in the private staging repository' in output
    assert ('create staging repository' in output) is (not visible)


def test_run_stage_push_refuses_visibility_drift_before_primitive(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, _record = prepare(tmp_path, source, monkeypatch)
    save_workflows(paths, project, staging=('stage push',))
    visible = iter([False, True])

    class _HostModule:
        @staticmethod
        def _repository_visible(_owner: str, _repo: str) -> bool:
            return next(visible)

    class _StageModule:
        @staticmethod
        def _stage_push(*_args: object, **_kwargs: object) -> None:
            pytest.fail('visibility drift reached staging mutation')

    def _approve(_prompt: str) -> str:
        return 'yes'

    monkeypatch.setattr(_stage, 'require_authenticated_account', _require_test_account)
    monkeypatch.setattr(_stage, 'repository_visible', _HostModule._repository_visible)
    monkeypatch.setattr(_stage, 'stage_push', _StageModule._stage_push)
    monkeypatch.setattr('builtins.input', _approve)
    capsys.readouterr()

    assert _cli.main(['run', 'staging', '--project', project.name]) == 1
    error = capsys.readouterr().err
    assert 'staging repository visibility changed after preview' in error
    assert 'rerun the workflow' in error


@pytest.mark.parametrize(
    'stages',
    [('stage push',), ('prepare', 'stage push')],
    ids=['retained-candidate', 'prepare'],
)
def test_run_staging_preview_checks_account_before_repository_request(
    stages: tuple[str, ...],
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answers: Callable[[Sequence[tuple[str, str]]], None],
) -> None:
    tmp_path = Path(os.environ['PUBANON_FAKE_GH_RESPONSES']).parent
    source = make_repo(files={'README.md': 'public\n'})
    if stages[0] == 'prepare':
        paths, _global_config, project = configure(tmp_path, monkeypatch, source)
        assert project is not None
    else:
        paths, project, _record = prepare(tmp_path, source, monkeypatch)
    save_workflows(paths, project, staging=stages)
    Path(os.environ['PUBANON_FAKE_GH_RESPONSES']).write_text(
        json.dumps(
            [
                {
                    'args': ['api', 'user', '--jq', '.login'],
                    'stdout': 'different-account\n',
                }
            ]
        )
        + '\n'
    )

    def _unexpected_stage(*_args: object, **_kwargs: object) -> Never:
        pytest.fail('account mismatch reached the staging primitive')

    monkeypatch.setattr(_stage, 'stage_push', _unexpected_stage)
    capsys.readouterr()

    answers([])
    assert (
        _cli.main(
            [
                'run',
                'staging',
                '--project',
                project.name,
                '--acknowledge-new-paths',
            ]
        )
        == 1
    )

    error = capsys.readouterr().err
    assert error.startswith('error: authenticated account does not match staging configuration\n')
    assert 'authenticated account' in error
    assert 'Expected: anonymous-account' in error
    assert 'Found: different-account' in error
    assert gh_calls(tmp_path) == [['api', 'user', '--jq', '.login']]
    if stages[0] == 'prepare':
        assert _candidate.load_candidate(paths, project.name) is None


@pytest.mark.parametrize(
    'stages',
    [('stage push',), ('prepare', 'stage push')],
    ids=['retained-candidate', 'prepare'],
)
def test_run_staging_preview_gates_coordinates_before_host_access(
    stages: tuple[str, ...],
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    term = 'private-stage'
    source = make_repo(files={'README.md': 'public\n'})
    if stages[0] == 'prepare':
        paths, _global_config, project = configure(tmp_path, monkeypatch, source)
        assert project is not None
    else:
        paths, project, _record = prepare(tmp_path, source, monkeypatch)
    project = replace(
        project,
        _stage_repo=f'public-{term}',
        extra_terms=(term,),
    )
    save_workflows(paths, project, staging=stages)
    capsys.readouterr()

    assert (
        _cli.main(
            [
                'run',
                'staging',
                '--project',
                project.name,
                '--acknowledge-new-paths',
            ]
        )
        == 1
    )

    error = capsys.readouterr().err
    assert error.startswith('error: repository name did not pass content checks\n')
    assert f'repo: public-{term}' in error
    assert gh_calls(tmp_path) == []
    if stages[0] == 'prepare':
        assert _candidate.load_candidate(paths, project.name) is None


def test_run_staging_recheck_repeats_preflight_before_visibility(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answers: Callable[[Sequence[tuple[str, str]]], None],
) -> None:
    fake_gh = Path(os.environ['PUBANON_FAKE_GH_RESPONSES'])
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, _record = prepare(tmp_path, source, monkeypatch)
    save_workflows(paths, project, staging=('stage push',))
    repository_args = [
        'api',
        f'repos/{project.owner}/{project.repo}-stage',
        '--jq',
        _host._REPOSITORY_QUERY,
    ]
    fake_gh.write_text(
        json.dumps(
            [
                {
                    'args': ['api', 'user', '--jq', '.login'],
                    'stdout': f'{project.owner}\n',
                    'once': True,
                },
                {
                    'args': repository_args,
                    'stdout': json.dumps(
                        {
                            'owner': project.owner,
                            'repo': f'{project.repo}-stage',
                            'visibility': 'private',
                        }
                    )
                    + '\n',
                },
                {
                    'args': ['api', 'user', '--jq', '.login'],
                    'stdout': 'different-account\n',
                },
            ]
        )
        + '\n'
    )

    def _unexpected_stage(*_args: object, **_kwargs: object) -> Never:
        pytest.fail('second staging preflight failure reached the staging primitive')

    monkeypatch.setattr(_stage, 'stage_push', _unexpected_stage)
    capsys.readouterr()

    answers([])
    assert _cli.main(['run', 'staging', '--project', project.name]) == 1

    error = capsys.readouterr().err
    assert error.startswith('error: authenticated account does not match staging configuration\n')
    assert 'authenticated account' in error
    assert 'Expected: anonymous-account' in error
    assert 'Found: different-account' in error
    assert gh_calls(tmp_path) == [
        ['api', 'user', '--jq', '.login'],
        repository_args,
        ['api', 'user', '--jq', '.login'],
    ]


@pytest.mark.slow
def test_run_staging_watch_and_publish_share_the_exact_candidate_commit(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(
        files={'README.md': 'candidate A\n', 'pyproject.toml': '[project]\nversion = "1.0.0"\n'}
    )
    _git(source, 'tag', 'source-only')
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    project = save_workflows(
        paths,
        replace(project, publication_route='staged', required_checks=('build',)),
        review=('prepare', 'stage push'),
        ship=('stage push', 'stage watch', 'publish', 'repo watch'),
    )
    stage_repo = _config.stage_repository(project)
    stage_host = tmp_path / f'{stage_repo}.git'
    final_host = tmp_path / f'{project.repo}.git'
    for host in (stage_host, final_host):
        subprocess.run(['git', 'init', '--bare', '-q', host], check=True, env=git_env())
    url_template = tmp_path.as_uri() + '/{repo}.git'
    monkeypatch.setattr(_host, '_URL_TEMPLATE', url_template)
    responses: list[dict[str, object]] = [
        {
            'args': ['api', 'user', '--jq', '.login'],
            'stdout': global_config.accounts['github'] + '\n',
        },
    ]
    responses.extend(
        {
            'args': ['api', f'repos/{project.owner}/{repo}', '--jq', _host._REPOSITORY_QUERY],
            'stdout': json.dumps({'owner': project.owner, 'repo': repo, 'visibility': 'private'}),
        }
        for repo in (stage_repo, project.repo)
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
                    'description': 'retained settings',
                    'default_branch': 'main',
                    'issues': False,
                    'wiki': True,
                    'projects': True,
                    'pull_requests': False,
                    'discussions': False,
                }
            ),
        }
    )
    fake_gh.write_text(json.dumps(responses))
    assert (
        _cli.main(
            [
                'run',
                'review',
                '--project',
                project.name,
                '--acknowledge-new-paths',
                '--yes',
            ]
        )
        == 0
    )
    first = _candidate.load_candidate(paths, project.name)
    assert first is not None
    assert _git(stage_host, 'show-ref').decode().splitlines() == [
        f'{first.commit} refs/heads/main'
    ]
    _candidate.discard(paths, project.name, force=True)
    (source / 'README.md').write_text('candidate B\n')
    _git(source, 'add', 'README.md')
    _git(source, 'commit', '-qm', 'candidate B')
    assert (
        _cli.main(['prepare', '--project', project.name, '--acknowledge-new-paths', '--yes']) == 0
    )
    second = _candidate.load_candidate(paths, project.name)
    assert second is not None
    assert second.commit != first.commit
    source_refs = _git(source, 'show-ref', '--heads', '--tags')
    assert _cli.main(['inspect', '--project', project.name]) == 0
    assert 'pubanon stage push' in capsys.readouterr().out
    assert _cli.main(['stage', 'push', '--project', project.name, '--yes']) == 0
    assert _git(stage_host, 'show-ref').decode().splitlines() == [
        f'{second.commit} refs/heads/main'
    ]
    staged_dates = _git(stage_host, 'show', '-s', '--format=%aI%n%cI', second.commit)
    check_args = _assert_staged_check_blocks_publish(project, second, paths, final_host, capsys)
    responses.extend(
        {
            'args': [
                'api',
                f'repos/{project.owner}/{repo}/commits/{second.commit}/check-runs?check_name=build&filter=latest&per_page=100',
            ],
            'stdout': json.dumps(
                {
                    'total_count': 1,
                    'check_runs': [
                        {
                            'name': 'build',
                            'head_sha': second.commit,
                            'status': 'completed',
                            'conclusion': 'success',
                        }
                    ],
                }
            ),
        }
        for repo in (stage_repo, project.repo)
    )
    fake_gh.write_text(json.dumps(responses))
    capsys.readouterr()
    assert _cli.main(['run', 'ship', '--project', project.name, '--yes']) == 0
    output = capsys.readouterr().out
    assert 'Workflow ship (staged route): stage push, stage watch, publish, repo watch' in output
    assert _git(stage_host, 'show-ref').decode().splitlines() == [
        f'{second.commit} refs/heads/main'
    ]
    assert _git(final_host, 'show-ref').decode().splitlines() == [
        f'{second.commit} refs/heads/main',
        f'{second.commit} refs/tags/v1.0.0',
    ]
    assert _git(final_host, 'rev-list', '--all').decode().splitlines() == [second.commit]
    assert _git(final_host, 'show', '-s', '--format=%aI%n%cI', second.commit) == staged_dates
    assert _git(source, 'show-ref', '--heads', '--tags') == source_refs
    calls = gh_calls(tmp_path)
    check_calls = [args[1] for args in calls if '/check-runs?' in args[1]]
    assert check_calls == [
        check_args,
        check_args,
        check_args,
        check_args,
        check_args.replace(f'/{stage_repo}/', f'/{project.repo}/'),
    ]
    assert not any(method in args for args in calls for method in ('POST', 'PATCH', 'DELETE'))


def test_run_unknown_name_lists_declared_workflows(
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _global_config, project = configured_project
    assert project is not None
    save_workflows(paths, project, checks=('repo watch',), ship=('publish',))

    assert _cli.main(['run', 'missing', '--project', project.name]) == 1

    error = capsys.readouterr().err
    assert 'workflow is not declared: missing' in error
    assert 'declared workflows: checks, ship' in error


@pytest.mark.parametrize(
    'case',
    [
        ('missing', []),
        ('ship', []),
        ('ship', ['--version', 'not-a-version']),
    ],
)
def test_run_unknown_outcome_precedes_name_and_version_validation(
    case: tuple[str, list[str]],
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow, version_args = case
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    _candidate.save_candidate(
        paths,
        project.name,
        replace(record, state=_candidate.STATE_UNKNOWN_OUTCOME),
    )
    save_workflows(paths, project, ship=('publish',))
    capsys.readouterr()

    assert _cli.main(['run', workflow, '--project', project.name, *version_args]) == 1

    error = capsys.readouterr().err
    assert 'the outcome of the last publication is unknown' in error
    assert 'pubanon publish --project=demo --resolve' in error
    assert 'workflow is not declared' not in error
    assert 'requires --version' not in error
    assert 'version must have X.Y.Z form' not in error


def test_run_unknown_outcome_refuses_before_any_declared_stage(
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
        replace(record, state=_candidate.STATE_UNKNOWN_OUTCOME),
    )
    save_workflows(paths, project, checks=('repo watch',))

    def _unexpected_watch() -> object:
        pytest.fail('unknown-outcome snapshot reached a declared stage')

    monkeypatch.setattr(_watch, 'watch_ci', _unexpected_watch)
    capsys.readouterr()

    assert _cli.main(['run', 'checks', '--project', project.name, '--yes']) == 1
    assert 'pubanon publish --project=demo --resolve' in capsys.readouterr().err


def test_run_unknown_rewrite_outcome_requires_resolution_before_host_or_stage(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    save_workflows(paths, project, ship=('stage push', 'publish'))
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
        pytest.fail('unknown outcome reached host or staging')

    monkeypatch.setattr(_host, 'verify_destination', _unexpected)
    monkeypatch.setattr(_stage, 'staging_repository_visible', _unexpected)
    capsys.readouterr()
    assert (
        _cli.main(['run', 'ship', '--project', project.name, '--version', '1.0.0', '--yes']) == 1
    )
    error = capsys.readouterr().err
    assert 'the outcome of the last publication is unknown' in error
    assert 'pubanon publish --project=demo --resolve' in error


def test_run_unknown_workflow_reports_requested_and_declared_names(
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    capsys: pytest.CaptureFixture[str],
) -> None:
    term = 'style-canary'
    paths, _global_config, project = configured_project
    assert project is not None
    _config.save_project(
        paths,
        replace(
            project,
            extra_terms=(term,),
            workflows={f'{term}-flow': ('repo watch',)},
        ),
    )

    assert _cli.main(['run', f'missing-{term}', '--project', project.name]) == 1

    error = capsys.readouterr().err
    assert term in error
    assert error.count(f'{term}') == 2


def test_run_watch_package_is_bound_to_invocation_version(
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _global_config, project = configured_project
    assert project is not None
    save_workflows(paths, project, observe=('package watch',))
    received: list[str | None] = []

    class _WatchModule:
        @staticmethod
        def _watch_package(
            *_args: object,
            version: str | None,
            **_kwargs: object,
        ) -> None:
            received.append(version)

    monkeypatch.setattr(_watch, 'watch_package', _WatchModule._watch_package)

    assert (
        _cli.main(['run', 'observe', '--project', project.name, '--version', '1.2.3', '--yes'])
        == 0
    )
    assert received == ['1.2.3']


def test_run_yes_refuses_first_use_staging_before_any_stage(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, _record = prepare(tmp_path, source, monkeypatch)
    save_workflows(paths, project, staging=('stage push',))

    class _HostModule:
        @staticmethod
        def _repository_visible(_owner: str, _repo: str) -> bool:
            return False

    class _StageModule:
        @staticmethod
        def _stage_push(*_args: object, **_kwargs: object) -> None:
            pytest.fail('unattended first-use staging executed')

    monkeypatch.setattr(_stage, 'require_authenticated_account', _require_test_account)
    monkeypatch.setattr(_stage, 'repository_visible', _HostModule._repository_visible)
    monkeypatch.setattr(_stage, 'stage_push', _StageModule._stage_push)
    capsys.readouterr()

    assert _cli.main(['run', 'staging', '--project', project.name, '--yes']) == 1
    result = capsys.readouterr()
    assert 'create staging repository' in result.out
    assert 'run interactively: pubanon stage push' in result.err


def test_run_yes_still_prints_preview_and_only_declared_stage_runs(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    project = save_workflows(paths, project, checks=('repo watch',))
    received: list[tuple[object, ...]] = []

    class _WatchModule:
        @staticmethod
        def _watch_ci(
            *args: object,
            staged: bool,
            version: str | None,
            timeout: int,
        ) -> None:
            received.append((*args, staged, version, timeout))
            next_step('pubanon package watch')

    monkeypatch.setattr(_watch, 'watch_ci', _WatchModule._watch_ci)

    assert _cli.main(['run', 'checks', '--project', project.name, '--yes']) == 0

    output = capsys.readouterr().out
    assert 'Workflow checks (direct route): repo watch' in output
    assert '--yes: stages continue without asking.' in output
    assert 'Workflow checks completed.' in output
    assert 'Next: no action is required.' not in output
    assert output.count('Next:') == 1
    assert 'pubanon package watch' not in output
    assert len(received) == 1
    actual_paths, actual_global, actual_project, staged, version, timeout = received[0]

    assert (actual_paths, actual_global, actual_project) == (paths, global_config, project)
    assert (staged, version, timeout) == (False, None, 1800)


def test_run_yes_stops_before_reveal_and_names_interactive_command(
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _global_config, project = configured_project
    assert project is not None
    save_workflows(paths, project, reveal=('repo reveal',))

    class _LifecycleModule:
        @staticmethod
        def _reveal(*_args: object, **_kwargs: object) -> None:
            pytest.fail('unattended workflow entered the reveal decision')

    monkeypatch.setattr(_lifecycle, 'reveal', _LifecycleModule._reveal)

    assert _cli.main(['run', 'reveal', '--project', project.name, '--yes']) == 1

    result = capsys.readouterr()
    assert 'make the final repository public' in result.out
    assert 'run pubanon repo reveal --project=demo interactively' in result.err
