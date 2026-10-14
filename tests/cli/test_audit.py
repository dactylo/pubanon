import json
import os
import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from pubanon import _cli, _config
from pubanon._cli._context import load_context
from pubanon._remote import _host
from pubanon._snapshots import _audit, _candidate, _store
from tests._cli import prepare, record_published_main, stub_remote
from tests._git import git_env
from tests._setup import configure

_RemoteAudit = Callable[[str], tuple[_config.Paths, _config.ProjectConfig]]
_Script = Callable[[Sequence[str | tuple[str, str]]], None]


@pytest.fixture
def remote_audit(
    tmp_path: Path, make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> _RemoteAudit:
    def _setup(visibility: str) -> tuple[_config.Paths, _config.ProjectConfig]:
        source = make_repo(files={'README.md': 'public\n'})
        paths, project, record = prepare(tmp_path, source, monkeypatch)
        global_config = _config.load_global(paths)
        record_published_main(paths, project, record)
        destination = _host.Destination(
            adapter='github',
            owner=project.owner,
            repo=project.repo,
            url='https://github.com/anonymous-account/public-repo.git',
            account=global_config.accounts['github'],
            visibility=visibility,
        )
        stub_remote(monkeypatch, destination, _host.RemoteRefs(record.commit, {}))
        return paths, project

    return _setup


def _stub_actions(
    monkeypatch: pytest.MonkeyPatch, log: bytes, *, status: str = 'completed'
) -> list[int]:
    run = _host.ActionsRun(41, 1, status, 'CI', 'push', 'a' * 40, '2026-09-22T10:00:00Z', b'CI')
    job = _host.ActionsJob(42, 41, 1, 'lint', 5, b'GitHub Actions 5')
    deleted: list[int] = []

    def _runs(_owner: str, _repo: str, *, command: str) -> tuple[_host.ActionsRun, ...]:
        assert command == 'pubanon audit --remote'
        return (run,)

    def _jobs(_owner: str, _repo: str, _run: int, *, command: str) -> tuple[_host.ActionsJob, ...]:
        assert command == 'pubanon audit --remote'
        return (job,)

    def _log(_owner: str, _repo: str, _job: int, *, command: str) -> bytes | _host.LogAbsence:
        assert command == 'pubanon audit --remote'
        return 'api-404' if deleted else log

    def _delete(_owner: str, _repo: str, run_id: int) -> None:
        deleted.append(run_id)

    def _read(_owner: str, _repo: str, _job: int, _run: int) -> _host.ActionsJob:
        return job

    def _account(expected: str, *, authority: str) -> str:
        del authority
        return expected

    monkeypatch.setattr(_host, 'actions_runs', _runs)
    monkeypatch.setattr(_host, 'actions_jobs', _jobs)
    monkeypatch.setattr(_host, 'job_log', _log)
    monkeypatch.setattr(_host, 'delete_run_logs', _delete)
    monkeypatch.setattr(_host, 'read_actions_job', _read)
    monkeypatch.setattr(_host, 'require_authenticated_account', _account)
    return deleted


def test_audit_discloses_blob_scan_skipped_for_merge_history(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exposure = 'historical-private-canary'
    source = make_repo(files={'README.md': f'{exposure}\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    store = _store.store_path(paths, project.name)
    tree = subprocess.run(
        ['git', '-C', store, 'rev-parse', f'{record.commit}^{{tree}}'],
        check=True,
        capture_output=True,
        text=True,
        env=git_env(),
    ).stdout.strip()
    commit_env = git_env(
        {
            'GIT_AUTHOR_NAME': 'Anonymous Publisher',
            'GIT_AUTHOR_EMAIL': 'anonymous@example.invalid',
            'GIT_AUTHOR_DATE': '1700000000 +0000',
            'GIT_COMMITTER_NAME': 'Anonymous Publisher',
            'GIT_COMMITTER_EMAIL': 'anonymous@example.invalid',
            'GIT_COMMITTER_DATE': '1700000000 +0000',
        }
    )

    def commit(*parents: str, message: str) -> str:
        args = ['git', '-C', store, 'commit-tree', tree]
        for parent in parents:
            args.extend(('-p', parent))
        return subprocess.run(
            args,
            check=True,
            capture_output=True,
            input=f'{message}\n',
            text=True,
            env=commit_env,
        ).stdout.strip()

    child = commit(record.commit, message='child')
    merge = commit(record.commit, child, message='merge')
    subprocess.run(
        ['git', '-C', store, 'update-ref', 'refs/heads/main', merge],
        check=True,
        env=git_env(),
    )
    _config.save_project(paths, replace(project, extra_terms=(exposure,)))
    capsys.readouterr()

    assert _cli.main(['audit', '--project', project.name]) == 0

    output = capsys.readouterr().out
    assert 'commit does not have exactly one parent' in output
    assert 'Content auditing could not be completed' in output
    assert 'No configured sensitive terms were found' not in output
    assert 'Warning: historical exposure' not in output


def test_audit_omits_reveal_line_for_public_project(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    term = 'historical-hygiene-path'
    source = make_repo(files={f'docs/{term}.txt': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    record_published_main(paths, project, record)
    _config.save_project(paths, replace(project, hygiene_terms=(term,), visibility='public'))
    capsys.readouterr()

    assert _cli.main(['audit', '--project', project.name]) == 0

    output = capsys.readouterr().out
    assert 'Warning: policy mismatch' in output
    assert 'pubanon repo reveal lists these findings' not in output


def test_audit_remote_labels_verified_history(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    global_config = _config.load_global(paths)
    record_published_main(paths, project, record)
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account=global_config.accounts['github'],
        visibility=project.visibility,
    )

    stub_remote(monkeypatch, destination, _host.RemoteRefs(record.commit, {}))
    capsys.readouterr()

    assert _cli.main(['audit', '--project', project.name, '--remote']) == 0

    rendered = capsys.readouterr().out
    assert 'local copy of published history' in rendered
    assert 'verified against GitHub' in rendered


def test_audit_remote_mismatch_reports_refs_without_cause(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    record_published_main(paths, project, record)
    host = tmp_path / 'host.git'
    subprocess.run(['git', 'init', '--bare', '-q', host], check=True, env=git_env())
    foreign = (
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
        ['git', '-C', host, 'fetch', '-q', str(source), f'{foreign}:refs/heads/main'],
        check=True,
        env=git_env(),
    )
    monkeypatch.setattr(_host, '_URL_TEMPLATE', str(host))
    fake_gh.write_text(
        json.dumps(
            [
                {'args': ['api', 'user', '--jq', '.login'], 'stdout': 'anonymous-account\n'},
                {
                    'args': [
                        'api',
                        'repos/anonymous-account/public-repo',
                        '--jq',
                        _host._REPOSITORY_QUERY,
                    ],
                    'stdout': json.dumps(
                        {
                            'owner': 'anonymous-account',
                            'repo': 'public-repo',
                            'visibility': 'private',
                        }
                    ),
                },
            ]
        )
        + '\n'
    )

    assert _cli.main(['audit', '--project', project.name, '--remote']) == 1

    err = capsys.readouterr().err
    assert 'GitHub branches and tags do not match saved published history' in err
    assert record.commit in err
    assert foreign in err
    assert 'pubanon repo verify --project=demo' in err
    assert 'pubanon repo restore --project=demo --to local' in err
    assert 'Accepting an outside fix' in err
    assert 'operator guide' in err
    assert 'merge' not in err
    assert 'pull request' not in err


@pytest.mark.parametrize('visibility', ['private', 'public'])
def test_audit_remote_offers_to_delete_matching_actions_logs(
    remote_audit: _RemoteAudit,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answers: _Script,
    visibility: str,
) -> None:
    paths, project = remote_audit(visibility)
    deleted = _stub_actions(monkeypatch, f'cache {paths.data_dir}\n'.encode())
    answers(
        [
            ('What should happen to these logs? [d]elete / [k]eep: ', 'd'),
            ('Delete the logs of 1 run (41, CI)? This cannot be undone. [y/n] ', 'y'),
        ]
    )
    capsys.readouterr()

    assert _cli.main(['audit', '--project', project.name, '--remote']) == 0

    out = capsys.readouterr().out
    assert deleted == [41]
    assert 'Sensitive terms in Actions logs' in out
    assert re.search(r'pubanon audit (--project=demo )?--remote --verbose', out)
    assert (
        'These logs are already public'
        if visibility == 'public'
        else 'public publishes these logs'
    ) in out
    assert out.index('Deleted the logs of 1 run.') < out.index('Not checked:')


def test_audit_remote_recovery_names_audit_while_runs_are_running(
    remote_audit: _RemoteAudit,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _paths, project = remote_audit('private')
    _stub_actions(monkeypatch, b'ok\n', status='in_progress')
    capsys.readouterr()

    assert _cli.main(['audit', '--project', project.name, '--remote']) == 1

    err = capsys.readouterr().err
    assert 'workflow runs are still running' in err
    assert re.search(r'rerun pubanon audit (--project=demo )?--remote', err)


def test_audit_remote_refuses_drift_before_audit_output(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    project = replace(project, extra_terms=('outside pubanon',))
    _config.save_project(paths, project)
    global_config = _config.load_global(paths)
    record_published_main(paths, project, record)
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account=global_config.accounts['github'],
        visibility=project.visibility,
    )

    stub_remote(monkeypatch, destination, _host.RemoteRefs('1' * 40, {}))
    capsys.readouterr()

    assert _cli.main(['audit', '--project', project.name, '--remote']) == 1

    captured = capsys.readouterr()
    assert 'pubanon repo verify --project=demo' in captured.err
    assert 'pubanon repo restore --project=demo --to local' in captured.err
    assert 'outside pubanon' in captured.err
    assert 'audit:' not in captured.out


def test_audit_remote_refuses_unexpected_branch_before_audit_output(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    global_config = _config.load_global(paths)
    record_published_main(paths, project, record)
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
        _host.RemoteRefs(record.commit, {}, (('private-backup', record.commit),)),
    )
    capsys.readouterr()

    assert _cli.main(['audit', '--project', project.name, '--remote']) == 1

    captured = capsys.readouterr()
    assert 'unexpected branch' in captured.err
    assert f'Expected: main={record.commit}; tags=none; branches=none' in captured.err
    assert (
        f'Found: main={record.commit}; tags=none; '
        f'branches=private-backup={record.commit}' in captured.err
    )
    assert 'audit:' not in captured.out


def test_audit_remote_reports_no_actions_runs_and_what_stays_unchecked(
    remote_audit: _RemoteAudit, capsys: pytest.CaptureFixture[str]
) -> None:
    _paths, project = remote_audit('private')
    capsys.readouterr()

    assert _cli.main(['audit', '--project', project.name, '--remote']) == 0

    out = capsys.readouterr().out
    assert 'No Actions runs to check.' in out
    assert 'Not checked: earlier pushes on the Activity page.' in out
    assert out.index('verified against GitHub') < out.index('No Actions runs to check.')


@pytest.mark.parametrize('stale_acknowledgment', [False, True])
def test_audit_reports_clean_published_history(
    stale_acknowledgment: bool,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    record_published_main(paths, project, record)
    if stale_acknowledgment:
        _config.save_project(paths, replace(project, acknowledged_exposure='a' * 64))
    capsys.readouterr()

    assert _cli.main(['audit', '--project', project.name]) == 0

    output = capsys.readouterr().out
    assert 'audit: local copy of published history' in output
    assert 'Warning: historical exposure' not in output
    assert 'Warning: policy mismatch' not in output
    assert 'identity problem:' not in output
    assert 'No configured sensitive terms were found' in output
    assert 'checked history' in output
    assert 'Historical exposure acknowledged.' not in output
    assert 'pubanon repo reveal' not in output


def test_audit_reports_empty_history(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    _paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None

    assert _cli.main(['audit', '--project', project.name]) == 0

    assert 'No recorded snapshot history to check.' in capsys.readouterr().out


@pytest.mark.parametrize('presentation', [(False, 'never'), (True, 'always')])
def test_audit_reports_findings_and_public_history_guidance(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    presentation: tuple[bool, str],
) -> None:
    verbose, color = presentation
    exposure = 'historical-private-canary'
    hygiene = 'policy-canary'
    published_path = f'evidence; text {exposure}.txt'
    source = make_repo(
        files={
            published_path: f'{exposure}\n{hygiene}\x1b[31m\n',
            'binary.dat': b'\x00' + exposure.encode(),
        }
    )
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    record_published_main(paths, project, record)
    subprocess.run(
        [
            'git',
            '-C',
            _store.store_path(paths, project.name),
            'update-ref',
            'refs/tags/v1.0.0',
            record.commit,
        ],
        check=True,
        env=git_env(),
    )
    _config.save_project(
        paths,
        replace(project, extra_terms=(exposure,), hygiene_terms=(hygiene,)),
    )
    _config.save_global(
        paths,
        replace(_config.load_global(paths), sensitive_terms=('Private Person', exposure)),
        paths.global_config.read_bytes(),
    )
    capsys.readouterr()

    assert (
        _cli.main(
            [
                'audit',
                '--project',
                project.name,
                '--color',
                color,
                *(['--verbose'] if verbose else []),
            ]
        )
        == 0
    )

    rendered = capsys.readouterr().out
    if color == 'always':
        assert '\x1b[1m\x1b[33mWarning: historical exposure\x1b[0m\x1b[0m' in rendered
        assert f'\x1b[1m{published_path}:2\x1b[0m' in rendered
        assert f'Text: \x1b[33m{hygiene}\x1b[0m' + r'\x1b[31m' in rendered
        assert rendered.count('Matched:') == 1
    else:
        assert '\x1b' not in rendered
        assert rendered.count('Matched:') == 3
    output = re.sub(r'\x1b\[[0-9;]*m', '', rendered)
    guidance = next(
        line.strip() for line in output.splitlines() if line.startswith('warning: if this history')
    )
    assert output.count('Warning: historical exposure') == 1
    assert output.count('Warning: policy mismatch') == 1
    assert output.index('Warning: historical exposure') < output.index('Warning: policy mismatch')
    assert 'Historical exposure unacknowledged' in output
    assert 'interactive acknowledgment' in output
    assert 'Rule: global sensitive_terms, item 2; project extra_terms, item 1' in output
    assert 'Rule: project hygiene_terms, item 1' in output
    assert (
        output.count(
            f'Published snapshot: v1.0.0 ({record.commit if verbose else record.commit[:12]})'
        )
        == 2
    )
    if not verbose:
        assert record.commit not in output
    assert f'  {published_path} (filename)\n    Rule:' in output
    assert f'    Text: {published_path}\n' in output
    assert f'  {published_path}:2\n    Rule: project hygiene_terms, item 1\n' in output
    assert f'    Text: {hygiene}' + r'\x1b[31m' in output
    assert f'  binary.dat (blob bytes 1:{len(exposure) + 1})\n    Rule:' in output
    assert f'    Matched: {exposure}\n' in output
    assert output.count('    Text:') == 2
    assert '\x1b' not in output
    assert exposure in output
    assert hygiene in output
    assert 'pubanon repo reveal' in output
    assert 'asks before making them public' in output
    assert 'deleting or rewriting it does not erase' in guidance
    assert 'copies, forks, or caches' in guidance
    for line in output.splitlines():
        if line.strip() != guidance:
            assert 'delet' not in line.casefold()
            assert 'rewrit' not in line.casefold()


def test_audit_reports_hygiene_only_path_findings(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    term = 'historical-hygiene-path'
    source = make_repo(files={f'docs/{term}.txt': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    record_published_main(paths, project, record)
    _config.save_project(paths, replace(project, hygiene_terms=(term,)))
    capsys.readouterr()

    assert _cli.main(['audit', '--project', project.name]) == 0

    output = capsys.readouterr().out
    assert 'Warning: policy mismatch' in output
    assert 'Warning: historical exposure' not in output
    assert f'  docs/{term}.txt (filename)' in output
    assert term in output
    assert 'pubanon repo reveal' in output
    assert 'asks before making them public' in output


def test_audit_reports_identity_problems(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
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
        [
            'git',
            '-C',
            _store.store_path(paths, project.name),
            'fetch',
            '-q',
            str(source),
            f'{commit}:refs/heads/main',
        ],
        check=True,
        env=git_env(),
    )

    assert _cli.main(['audit', '--project', project.name]) == 0

    output = capsys.readouterr().out
    assert 'identity problem:' in output
    assert 'Content auditing could not be completed' in output
    assert 'No configured sensitive terms were found' not in output


def test_audit_reports_matching_nonempty_exposure_acknowledgment(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exposure = 'historical-private-canary'
    source = make_repo(files={'README.md': f'{exposure}\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    record_published_main(paths, project, record)
    project = replace(project, extra_terms=(exposure,))
    _config.save_project(paths, project)
    ctx = load_context(paths, project.name)
    main, tags = _candidate.published_refs(paths, project.name)
    assert main is not None
    commits = _audit.published_commits(paths, project.name, new=main)
    rules = _config.assemble_rules(
        ctx.global_config,
        ctx.project,
        _config.derived_private_terms(paths, project, os.environ),
    )
    findings = _audit.audit_history(
        paths,
        project.name,
        commits,
        tags,
        rules,
        f'{ctx.global_config.anon_name} <{ctx.global_config.anon_email}>',
        tip=main,
    ).findings
    digest = _audit.exposure_digest(findings)
    assert digest
    _config.save_project(paths, replace(project, acknowledged_exposure=digest))
    capsys.readouterr()

    assert _cli.main(['audit', '--project', project.name]) == 0

    output = capsys.readouterr().out
    assert 'Historical exposure acknowledged.' in output


@pytest.mark.parametrize('exposure', ['Anonymous Publisher', 'anonymous@example.invalid'])
def test_audit_reports_newly_sensitive_anonymous_identity(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exposure: str,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    record_published_main(paths, project, record)
    _config.save_project(paths, replace(project, extra_terms=(exposure,)))
    capsys.readouterr()

    assert _cli.main(['audit', '--project', project.name]) == 0

    output = capsys.readouterr().out
    for field in ('author', 'committer'):
        assert f'  header {field} value\n    Rule: project extra_terms, item 1\n' in output
    assert f'    Matched: {exposure}\n' in output
    assert output.count('Warning: historical exposure') == 1
    assert output.count(f'Published snapshot: {record.commit[:12]}') == 1
    assert 'Historical exposure unacknowledged' in output
    assert 'interactive acknowledgment' in output
    assert 'Historical exposure acknowledged.' not in output
    assert 'identity problem:' not in output
    assert exposure in output


def test_audit_resolves_unknown_publication_before_remote_or_report(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    _candidate.save_candidate(
        paths,
        project.name,
        replace(record, state=_candidate.STATE_UNKNOWN_OUTCOME),
    )

    def unexpected_remote(*_args: object) -> None:
        pytest.fail('unknown publication must be resolved before host access')

    monkeypatch.setattr(_host, 'verify_destination', unexpected_remote)
    capsys.readouterr()

    assert _cli.main(['audit', '--project', project.name, '--remote']) == 1

    captured = capsys.readouterr()
    assert 'the outcome of the last publication is unknown' in captured.err
    assert 'pubanon publish --project=demo --resolve' in captured.err
    assert 'audit:' not in captured.out


def test_audit_without_remote_reads_no_actions_content(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    record_published_main(paths, project, record)

    def _unexpected(*_args: object, **_kwargs: object) -> object:
        raise AssertionError('audit read Actions content')

    for name in ('actions_runs', 'disclosure', 'actions_cache_count'):
        monkeypatch.setattr(_host, name, _unexpected)
    capsys.readouterr()

    assert _cli.main(['audit', '--project', project.name]) == 0
    assert 'Actions' not in capsys.readouterr().out
