import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from pubanon import (
    _cli,
    _config,
)
from pubanon._cli._context import load_context
from pubanon._errors import PubanonError
from pubanon._remote import _host
from pubanon._snapshots import _audit, _candidate, _records
from pubanon._terminal import escape
from tests._cli import prepare, record_publication, record_published_main, stub_remote
from tests._git import git_env, remove_tree
from tests._setup import configure
from tests.cli.test_prepare import replacement_context


@pytest.mark.parametrize(
    'options',
    [
        [],
        ['--remote'],
        ['--audit'],
        ['--audit', '--remote'],
    ],
)
def test_every_status_mode_reports_read_only_result_and_next_action_last(
    options: list[str],
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    _candidate.store_path(paths, project.name)
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account=global_config.accounts['github'],
        visibility=project.visibility,
    )
    stub_remote(monkeypatch, destination, _host.RemoteRefs(None, {}))

    assert _cli.main(['status', '--project', project.name, *options]) == 0

    output = capsys.readouterr().out
    assert output.startswith('Project: demo\n\nPrepared snapshot: none\n')
    assert 'Last published snapshot: none\n' in output
    assert 'state was changed' not in output
    assert 'Details:' not in output
    assert 'Next: pubanon prepare --project=demo\n' in output
    assert output.rstrip().splitlines()[-1].startswith('Next:')


def test_status_audit_discloses_blob_scan_skipped_for_merge_history(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exposure = 'historical-private-canary'
    source = make_repo(files={'README.md': f'{exposure}\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    store = _candidate.store_path(paths, project.name)
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

    assert _cli.main(['status', '--project', project.name, '--audit']) == 0

    output = capsys.readouterr().out
    assert 'commit does not have exactly one parent' in output
    assert 'Content auditing could not be completed reliably.' in output
    assert 'No configured sensitive terms were found in the checked history.' not in output
    assert 'historical exposure:' not in output


def test_status_audit_discloses_invalid_tree_header_result(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    record_published_main(paths, project, record)
    problem = f'{record.commit[:12]}: invalid tree object ID'

    def incomplete_audit(*_args: object, **_kwargs: object) -> _audit.HistoryAudit:
        return _audit.HistoryAudit((), (problem,), ())

    monkeypatch.setattr(
        _audit,
        'audit_history',
        incomplete_audit,
    )
    capsys.readouterr()

    assert _cli.main(['status', '--project', project.name, '--audit']) == 0

    output = capsys.readouterr().out
    assert f'identity problem: {problem}' in output
    assert 'Content auditing could not be completed reliably.' in output
    assert 'No configured sensitive terms were found in the checked history.' not in output


def test_status_audit_remote_labels_verified_history(
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

    assert _cli.main(['status', '--project', project.name, '--audit', '--remote']) == 0

    assert (
        'audit: local copy of published history; verified against the host'
        in capsys.readouterr().out
    )


def test_status_audit_remote_mismatch_reports_refs_without_cause(
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
    monkeypatch.setattr(_host, 'URL_TEMPLATE', str(host))
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

    assert _cli.main(['status', '--project', project.name, '--audit', '--remote']) == 1

    err = capsys.readouterr().err
    assert 'host refs do not match local published state' in err
    assert record.commit in err
    assert foreign in err
    assert (
        'pubanon repo verify --project=demo; use --repair-store only if the host '
        'is authoritative, '
        'or see "Accepting an outside fix" in the operator guide if the host changed '
        'outside Pubanon'
    ) in err
    assert 'merge' not in err
    assert 'pull request' not in err


def test_status_audit_remote_refuses_drift_before_audit_output(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    project = replace(project, extra_terms=('outside Pubanon',))
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

    assert _cli.main(['status', '--project', project.name, '--audit', '--remote']) == 1

    captured = capsys.readouterr()
    assert 'pubanon repo verify --project=demo; use --repair-store' in captured.err
    assert f'if the host changed {"outside Pubanon"}' in captured.err
    assert 'outside Pubanon' in captured.err
    assert 'audit:' not in captured.out


def test_status_audit_remote_refuses_unexpected_branch_before_audit_output(
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

    assert _cli.main(['status', '--project', project.name, '--audit', '--remote']) == 1

    captured = capsys.readouterr()
    assert 'unexpected branch' in captured.err
    assert f'expected: main={record.commit}; tags=none; branches=none' in captured.err
    assert (
        f'actual: main={record.commit}; tags=none; '
        f'branches=private-backup={record.commit}' in captured.err
    )
    assert 'audit:' not in captured.out


@pytest.mark.parametrize('stale_acknowledgment', [False, True])
def test_status_audit_reports_clean_published_history(
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

    assert _cli.main(['status', '--project', project.name, '--audit']) == 0

    output = capsys.readouterr().out
    assert 'audit: local copy of published history' in output
    assert 'historical exposure:' not in output
    assert 'policy mismatch:' not in output
    assert 'identity problem:' not in output
    assert 'No configured sensitive terms were found in the checked history.' in output
    assert 'exposure acknowledged' not in output


def test_status_audit_reports_empty_history(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    _paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None

    assert _cli.main(['status', '--project', project.name, '--audit']) == 0

    assert 'No recorded snapshot history to check.' in capsys.readouterr().out


def test_status_audit_reports_findings_and_public_history_guidance(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exposure = 'historical-private-canary'
    hygiene = 'policy-canary'
    published_path = f'{exposure}.txt'
    source = make_repo(files={published_path: f'{exposure}\n{hygiene}\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    record_published_main(paths, project, record)
    _config.save_project(
        paths,
        replace(project, extra_terms=(exposure,), hygiene_terms=(hygiene,)),
    )
    capsys.readouterr()

    assert _cli.main(['status', '--project', project.name, '--audit']) == 0

    output = capsys.readouterr().out
    guidance = (
        'If this history has been public, deleting or rewriting it does not erase '
        'copies, forks, or caches'
    )
    assert f'historical exposure: path:{exposure}.txt' in output
    assert f'policy mismatch: path:{exposure}.txt' in output
    assert 'exposure unacknowledged: publish will require interactive acknowledgment' in output
    assert 'project extra_terms entry 1' in output
    assert 'project hygiene_terms entry 1' in output
    assert f'commit {record.commit}' in output
    assert f'filename bytes 0:{len(exposure.encode())}' in output
    assert f'blob bytes {len(exposure.encode()) + 1}:' in output
    assert exposure in output
    assert hygiene in output
    assert guidance in output
    for line in output.splitlines():
        if line.strip() != guidance:
            assert 'delet' not in line.casefold()
            assert 'rewrit' not in line.casefold()


def test_status_audit_reports_hygiene_only_path_findings(
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

    assert _cli.main(['status', '--project', project.name, '--audit']) == 0

    output = capsys.readouterr().out
    assert f'policy mismatch: path:docs/{term}.txt' in output
    assert term in output


def test_status_audit_reports_identity_problems(
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
            _candidate.store_path(paths, project.name),
            'fetch',
            '-q',
            str(source),
            f'{commit}:refs/heads/main',
        ],
        check=True,
        env=git_env(),
    )

    assert _cli.main(['status', '--project', project.name, '--audit']) == 0

    output = capsys.readouterr().out
    assert 'identity problem:' in output
    assert 'Content auditing could not be completed reliably.' in output
    assert 'No configured sensitive terms were found in the checked history.' not in output


def test_status_audit_reports_matching_nonempty_exposure_acknowledgment(
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
        paths, project.name, commits, tags, rules, ctx.global_config, tip=main
    ).findings
    digest = _audit.exposure_digest(findings)
    assert digest
    _config.save_project(paths, replace(project, acknowledged_exposure=digest))
    capsys.readouterr()

    assert _cli.main(['status', '--project', project.name, '--audit']) == 0

    output = capsys.readouterr().out
    assert 'exposure acknowledged' in output
    assert 'exposure unacknowledged:' not in output


@pytest.mark.parametrize('exposure', ['Anonymous Publisher', 'anonymous@example.invalid'])
def test_status_audit_reports_newly_sensitive_anonymous_identity(
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

    assert _cli.main(['status', '--project', project.name, '--audit']) == 0

    output = capsys.readouterr().out
    for field in ('author', 'committer'):
        assert f'historical exposure: {record.commit[:12]}:header:{field}' in output
    assert 'exposure unacknowledged: publish will require interactive acknowledgment' in output
    assert 'exposure acknowledged' not in output
    assert 'identity problem:' not in output
    assert exposure in output


@pytest.mark.parametrize('blocked', [False, True])
def test_status_changed_source_offers_existing_candidate_unless_settings_changed(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    blocked: bool,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='0.0.1')
    (source / 'README.md').write_text('updated public content\n')
    subprocess.run(['git', '-C', source, 'add', 'README.md'], check=True, env=git_env())
    subprocess.run(
        ['git', '-C', source, 'commit', '-qm', 'Update content'], check=True, env=git_env()
    )
    if blocked:
        _config.save_project(paths, replace(project, extra_terms=('new-private-term',)))
    capsys.readouterr()

    assert _cli.main(['status', '--project', project.name]) == 0

    expected_action = (
        'Next: pubanon discard --project=demo && '
        f'pubanon prepare --project=demo --source {record.pinned.commit} --version 0.0.1\n'
        if blocked
        else 'Next: pubanon inspect --project=demo\n'
    )
    output = capsys.readouterr().out
    assert output.startswith(
        'Project: demo\n\nPrepared snapshot\nVersion: v0.0.1\nOperation: new\n'
        'Source revision changed since preparation: yes\n'
    )
    assert ('Pubanon settings have changed since this snapshot was prepared.' in output) is blocked
    assert 'Last published snapshot: none\n' in output
    assert output.endswith(expected_action)
    assert _candidate.load_candidate(paths, project.name) == record


def test_status_completed_rewrite_still_requires_cleanup(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    paths, project, _original = replacement_context(tmp_path, source, monkeypatch)
    unlink = Path.unlink

    def _fail_remove(path: Path, *, missing_ok: bool = False) -> None:
        if path == _records.rewrite_path(paths, project.name):
            raise OSError('interrupted completed rewrite cleanup')
        unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as fault:
        fault.setattr(Path, 'unlink', _fail_remove)
        with pytest.raises(OSError, match='interrupted completed rewrite cleanup'):
            _cli.main(['prepare', '--project', project.name, '--replace', 'v1.0.0', '--yes'])
    record = _candidate.load_candidate(paths, project.name)
    progress = _records.load_rewrite(paths, project.name)
    assert record is not None
    assert record.tip is not None
    assert progress is not None
    capsys.readouterr()
    assert _cli.main(['status', '--project', project.name]) == 0
    output = capsys.readouterr().out
    assert 'Replacement preparation completed; local cleanup is pending.' in output
    assert 'Prepared snapshot\n' not in output
    assert record.tip not in output
    assert output.rstrip().endswith('Next: pubanon prepare --project=demo --continue')
    assert _candidate.load_candidate(paths, project.name) == record
    assert _records.load_rewrite(paths, project.name) == progress


def test_status_confirmed_incomplete_publication_does_not_need_source(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch)
    published = record_publication(paths, project, candidate)
    _candidate.save_candidate(
        paths,
        project.name,
        replace(
            candidate,
            state=_candidate.STATE_UNKNOWN_OUTCOME,
            source_bookmark=published.source_bookmark,
        ),
    )
    monkeypatch.chdir(tmp_path)
    remove_tree(source)
    capsys.readouterr()

    assert _cli.main(['status', '--project', project.name]) == 0

    output = capsys.readouterr().out
    assert 'Snapshot published; local recording needs completion.' in output
    assert output.rstrip().endswith('Next: pubanon publish --project=demo --resolve')


@pytest.mark.parametrize('result', ['match', 'mismatch', 'refused', 'different_destination'])
def test_status_current_visibility_requires_successful_same_destination_check(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    result: str,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch)
    record_published_main(paths, project, candidate)
    record_publication(paths, project, candidate)
    configured = replace(
        project,
        visibility='public',
        repo='different-repo' if result == 'different_destination' else project.repo,
    )
    _config.save_project(paths, configured)
    destination = _host.Destination(
        adapter='github',
        owner=configured.owner,
        repo=configured.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account='anonymous-account',
        visibility='public',
    )
    stub_remote(
        monkeypatch,
        destination,
        _host.RemoteRefs(None if result == 'mismatch' else candidate.commit, {}),
    )
    if result == 'refused':

        def _refuse(*_args: object, **_kwargs: object) -> _host.Destination:
            raise PubanonError('repository visibility does not match configuration')

        monkeypatch.setattr(_host, 'verify_destination', _refuse)
    capsys.readouterr()

    assert _cli.main(['status', '--project', project.name, '--remote']) == (
        1 if result == 'refused' else 0
    )
    captured = capsys.readouterr()
    if result == 'match':
        assert 'Configured visibility: PUBLIC\nCurrent visibility: PUBLIC' in captured.out
    else:
        assert 'Current visibility: PUBLIC' not in captured.out
    if result == 'refused':
        assert 'repository visibility does not match configuration' in captured.err
    elif result == 'mismatch':
        assert 'GitHub differs from the recorded published snapshot.' in captured.out
        assert captured.out.rstrip().endswith('Next: pubanon repo verify --project=demo')


def test_status_invalidated_candidate_routes_to_discard_then_prepare(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, _record = prepare(tmp_path, source, monkeypatch)
    _config.save_project(paths, replace(project, extra_terms=('new-private-term',)))
    capsys.readouterr()

    assert _cli.main(['status', '--project', project.name]) == 0

    output = capsys.readouterr().out
    assert 'Pubanon settings have changed since this snapshot was prepared.' in output
    assert f'pubanon prepare --project=demo --source {_record.pinned.commit}' in output
    assert output.rstrip().splitlines()[-1].startswith('Next:')


def test_status_ordinary_retained_candidate_routes_to_inspection(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    _paths, project, _record = prepare(tmp_path, source, monkeypatch)
    capsys.readouterr()

    assert _cli.main(['status', '--project', project.name]) == 0

    output = capsys.readouterr().out
    assert 'Prepared snapshot\nVersion: untagged\nOperation: new' in output
    assert 'Next: pubanon inspect --project=demo\n' in output
    assert output.rstrip().splitlines()[-1].startswith('Next:')


@pytest.mark.parametrize('audit', [False, True])
def test_status_remote_mismatch_resolves_unknown_publication_first(
    audit: bool,
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
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account='anonymous-account',
        visibility=project.visibility,
    )
    stub_remote(monkeypatch, destination, _host.RemoteRefs(record.commit, {}))
    capsys.readouterr()

    assert (
        _cli.main(
            ['status', '--project', project.name, '--remote', *(['--audit'] if audit else [])]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert 'GitHub differs from the recorded published snapshot.' in output
    assert 'pubanon publish --project=demo --resolve' in output
    assert 'repo verify' not in output
    assert output.rstrip().splitlines()[-1].startswith('Next:')


def test_status_remote_refuses_unexpected_destination_branch_with_recovery(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    _candidate.store_path(paths, project.name)
    monkeypatch.chdir(source)
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
        owner=project.owner,
        repo=project.repo,
        url=remote.as_uri(),
        account=global_config.accounts['github'],
        visibility=project.visibility,
    )

    def _verified_destination(
        _paths: _config.Paths,
        _global: _config.GlobalConfig,
        _project: _config.ProjectConfig,
    ) -> _host.Destination:
        return destination

    monkeypatch.setattr(_host, 'verify_destination', _verified_destination)

    assert _cli.main(['status', '--project', project.name, '--remote']) == 1
    error = capsys.readouterr().err
    assert 'unexpected branch' in error
    assert 'remove every destination branch except main, then rerun' in error


@pytest.mark.parametrize('selector', ['v1.0.0', 'all'])
def test_status_replacement_reprepare_preserves_scope_source_and_version(
    selector: str,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    paths, project, original = replacement_context(tmp_path, source, monkeypatch)
    assert (
        _cli.main(
            [
                'prepare',
                '--project',
                project.name,
                '--replace',
                selector,
                '--version',
                '1.0.0',
                '--yes',
            ]
        )
        == 0
    )
    record = _candidate.load_candidate(paths, project.name)
    assert record is not None
    _config.save_project(paths, replace(project, extra_terms=('new-private-term',)))
    capsys.readouterr()
    assert _cli.main(['status', '--project', project.name]) == 0
    output = capsys.readouterr().out
    selected = 'all' if selector == 'all' else original.commit
    assert (
        f'pubanon prepare --project=demo --replace {selected} '
        f'--source {record.pinned.commit} --version 1.0.0' in output
    )
    assert output.count('Next:') == 1


@pytest.mark.parametrize('phase', ['initial', 'replaying', 'exporting', 'awaiting'])
def test_status_rewrite_progress_is_read_only(
    phase: str,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    paths, project, _original = replacement_context(tmp_path, source, monkeypatch, conflict=True)
    assert _cli.main(['prepare', '--project', project.name, '--replace', 'v1.0.0', '--yes']) == 0
    progress = _records.load_rewrite(paths, project.name)
    assert progress is not None
    if phase == 'initial':
        _records.rewrite_path(paths, project.name).unlink()
    elif phase == 'replaying':
        _records.save_rewrite(
            paths, project.name, replace(progress, phase='replaying', pause=None)
        )
    elif phase == 'exporting':
        _records.save_rewrite(paths, project.name, replace(progress, phase='exporting'))
    elif phase == 'awaiting':
        assert progress.pause is not None
        conflicts = tuple(
            replace(
                conflict,
                conflict_type=conflict.conflict_type + '\x1b',
                message=conflict.message + '\nreview\\path',
            )
            for conflict in progress.pause.conflicts
        )
        _records.save_rewrite(
            paths,
            project.name,
            replace(progress, pause=replace(progress.pause, conflicts=conflicts)),
        )
    store = _candidate.candidate_store_path(paths, project.name)
    subprocess.run(
        ['git', '-C', store, 'update-ref', '-d', _candidate.CANDIDATE_REF],
        check=True,
        env=git_env(),
    )
    record = _candidate.load_candidate(paths, project.name)
    saved = _records.load_rewrite(paths, project.name)

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail('status invoked mutating rewrite verification')

    monkeypatch.setattr(_candidate, 'verify_rewrite_progress', _forbidden)
    capsys.readouterr()
    assert _cli.main(['status', '--project', project.name]) == 0
    output = capsys.readouterr().out
    expected = {
        'initial': 'Replacement initialization incomplete',
        'replaying': 'Preparation interrupted while rebuilding',
        'exporting': 'Conflict export incomplete',
        'awaiting': 'Path decisions awaiting resolution: 1',
    }
    assert expected[phase] in output
    assert 'Tags to move:' not in output
    assert 'Replaces: published snapshot for v1.0.0' in output
    if phase == 'awaiting':
        assert (
            escape(str(_records.rewrite_directory(paths, project.name) / 'resolutions.toml'))
            in output
        )
    if phase in {'exporting', 'awaiting'}:
        assert saved is not None
        assert saved.pause is not None
        assert 'Rebuilding snapshot: v1.0.1' in output
        for conflict in saved.pause.conflicts:
            assert f'{escape(conflict.conflict_type)}: {escape(conflict.message)}' in output
    assert output.count('Next:') == 1
    assert output.rstrip().endswith('Next: pubanon prepare --project=demo --continue')
    assert _candidate.load_candidate(paths, project.name) == record
    assert _records.load_rewrite(paths, project.name) == saved
    assert (
        subprocess.run(
            ['git', '-C', store, 'show-ref', '--verify', '--quiet', _candidate.CANDIDATE_REF],
            check=False,
            env=git_env(),
        ).returncode
        == 1
    )


def test_status_snapshot_fields_are_concise_and_verbose_adds_provenance(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.2.3')
    capsys.readouterr()
    assert _cli.main(['status', '--project', project.name]) == 0
    concise = capsys.readouterr().out
    assert concise.startswith('Project: demo\n\nPrepared snapshot\n')
    assert 'Version: v1.2.3\nOperation: new' in concise
    assert 'Source revision changed since preparation: no' in concise
    assert record.commit not in concise
    assert escape(str(source)) not in concise
    assert '0 replayed' not in concise
    assert _cli.main(['status', '--project', project.name, '--verbose']) == 0
    verbose = capsys.readouterr().out
    assert record.commit in verbose
    assert record.pinned.commit in verbose
    assert escape(str(source)) in verbose
    assert escape(str(_candidate.candidate_store_path(paths, project.name))) in verbose


@pytest.mark.parametrize('mode', ['redirected', 'terminal', 'no_color'])
def test_status_summarizes_copies_without_listing_paths(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    copy = paths.data_dir / 'projects' / project.name / 'inspections' / ('a' * 40)
    copy.mkdir(parents=True)

    monkeypatch.setattr(sys.stdout, 'isatty', lambda: mode != 'redirected')
    monkeypatch.delenv('NO_COLOR', raising=False)
    if mode == 'no_color':
        monkeypatch.setenv('NO_COLOR', '1')

    assert _cli.main(['status', '--project', project.name]) == 0

    output = capsys.readouterr().out
    styled = mode == 'terminal'
    bold = '\x1b[1m' if styled else ''
    cyan = '\x1b[36m' if styled else ''
    reset = '\x1b[0m' if styled else ''
    assert output == (
        f'{bold}Project:{reset} demo\n\n'
        f'{bold}Prepared snapshot:{reset} none\n\n'
        f'{bold}Last published snapshot:{reset} none\n\n'
        f'{bold}Snapshot directories:{reset} 1\n'
        f'{bold}View directories:{reset} '
        f'{cyan}pubanon inspect --project=demo --verbose{reset}\n\n'
        f'{bold}Next:{reset} {cyan}pubanon prepare --project=demo{reset}\n'
    )


@pytest.mark.parametrize(
    'case',
    [
        (version, change)
        for version in (None, '1.2.3')
        for change in ('none', 'visibility', 'destination')
    ],
)
def test_status_summarizes_recorded_publication_and_configuration(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[str | None, str],
) -> None:
    version, change = case
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch, version=version)
    record_published_main(paths, project, candidate)
    record_publication(paths, project, candidate)
    if change == 'visibility':
        _config.save_project(paths, replace(project, visibility='public'))
    elif change == 'destination':
        _config.save_project(paths, replace(project, repo='another-repo', visibility='public'))
    capsys.readouterr()

    assert _cli.main(['status', '--project', project.name]) == 0

    output = capsys.readouterr().out
    identity = f'v{version}' if version else 'untagged'
    assert (
        f'Last published snapshot\nVersion: {identity}\n'
        'Destination: anonymous-account/public-repo\n'
        'Visibility at publication: PRIVATE\nPublished: ' in output
    )
    assert (
        'Configured visibility: PUBLIC' in output
        if change == 'visibility'
        else 'Configured visibility: PUBLIC' not in output
    )
    if change == 'destination':
        assert 'Configured destination: anonymous-account/another-repo (PUBLIC)' in output
    assert 'currently ' not in output
    assert candidate.commit not in output
    assert output.count('Last published snapshot') == 1
    assert output.rstrip().splitlines()[-1].startswith('Next:')


def test_status_unknown_outcome_routes_to_resolution_before_invalidation(
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
    _config.save_project(paths, replace(project, extra_terms=('new-private-term',)))
    capsys.readouterr()

    assert _cli.main(['status', '--project', project.name]) == 0

    output = capsys.readouterr().out
    assert 'Pubanon settings have changed since this snapshot was prepared.' in output
    assert 'Snapshot with unknown publish outcome' in output
    assert 'Publish outcome: unknown' in output
    assert 'Next: pubanon publish --project=demo --resolve\n' in output
    assert output.rstrip().splitlines()[-1].startswith('Next:')
