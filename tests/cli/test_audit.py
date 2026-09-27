import json
import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from pubanon import _cli, _config
from pubanon._cli import _audit as audit_cli
from pubanon._cli._context import load_context
from pubanon._gate import RuleClass
from pubanon._remote import _host
from pubanon._snapshots import _audit, _candidate
from pubanon._terminal import color_context
from tests._cli import prepare, record_published_main, stub_remote
from tests._git import git_env
from tests._setup import configure


def test_audit_discloses_blob_scan_skipped_for_merge_history(
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

    assert _cli.main(['audit', '--project', project.name]) == 0

    output = capsys.readouterr().out
    assert 'commit does not have exactly one parent' in output
    assert 'Content auditing could not be completed reliably.' in output
    assert 'No configured sensitive terms were found in the checked history.' not in output
    assert 'Warning: historical exposure' not in output


def test_audit_discloses_invalid_tree_header_result(
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

    assert _cli.main(['audit', '--project', project.name]) == 0

    output = capsys.readouterr().out
    assert f'identity problem: {problem}' in output
    assert 'Content auditing could not be completed reliably.' in output
    assert 'No configured sensitive terms were found in the checked history.' not in output


def test_audit_groups_findings_by_snapshot_with_release_tags(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, _global, project = configure(tmp_path, monkeypatch, tmp_path)
    assert project is not None
    ctx = load_context(paths, project.name)
    older, newer = 'a' * 40, 'b' * 40
    evidence = tuple(
        _audit.HistoryEvidence(
            _audit.AuditFinding(unit, RuleClass.IDENTITY_CRITICAL, 'Private Person'),
            commit,
            'blob bytes 0:14',
            label,
            'Private Person',
        )
        for unit, commit, label in (
            ('path:a.txt', older, 'a.txt:1'),
            ('path:b.txt', newer, 'b.txt:1'),
            ('path:c.txt', older, 'c.txt:1'),
            ('tag:unresolved', None, 'tag name'),
        )
    )
    result = _audit.HistoryAudit(tuple(item.finding for item in evidence), (), evidence)

    with color_context('never'):
        output = '\n'.join(
            audit_cli._finding_groups(result, ctx, (), [newer, older], {'v1.0.0': older})
        )

    newest_heading = f'Published snapshot: {newer[:12]}'
    older_heading = f'Published snapshot: v1.0.0 ({older[:12]})'
    unknown_heading = 'Published snapshot: unknown'
    assert output.count('Warning: historical exposure') == 1
    for heading in (newest_heading, older_heading, unknown_heading):
        assert output.count(heading) == 1
    newest, older_group, unknown = (
        output.index(heading) for heading in (newest_heading, older_heading, unknown_heading)
    )
    assert newest < output.index('  b.txt:1') < older_group
    assert older_group < output.index('  a.txt:1') < output.index('  c.txt:1') < unknown
    assert unknown < output.index('  tag unresolved')
    assert 'Commit:' not in output


@pytest.mark.parametrize(
    ('text', 'term', 'highlighted'),
    [
        ('before SECRET after', 'secret', 'before \x1b[33mSECRET\x1b[0m after'),
        ('before cafe\u0301 after', 'caf\u00e9', 'before \x1b[33mcafe\u0301\x1b[0m after'),
        (
            'x' * 80 + '...target',
            '...',
            '...' + 'x' * 60 + '\x1b[33m...\x1b[0mtarget',
        ),
        (
            '\x1b[31msecret\\suffix',
            'secret',
            r'\x1b[31m' + '\x1b[33msecret\x1b[0m' + r'\\suffix',
        ),
        ('\u00e9', '\u00c3', None),
    ],
)
def test_audit_highlights_source_match_without_changing_excerpt(
    text: str, term: str, highlighted: str | None
) -> None:
    findings: dict[_audit.AuditFinding, _audit.HistoryEvidence] = {}
    _audit._add_findings(
        findings,
        'path:notes.txt',
        text.encode(),
        {RuleClass.IDENTITY_CRITICAL: (term,)},
        ('a' * 40, 'blob'),
    )

    with color_context('always'):
        lines = audit_cli._finding_lines(
            next(iter(findings.values())), ('global sensitive_terms entry 1',)
        )

    assert lines[1] == '    Rule: global sensitive_terms, item 1'
    if highlighted is None:
        assert lines[2] == f'    Matched: \x1b[33m{term}\x1b[0m'
        assert lines[3] == f'    Text: {text}'
    else:
        assert lines[2] == f'    Text: {highlighted}'
        assert not any('Matched:' in line for line in lines)


def test_audit_keeps_full_tag_name_when_source_excerpt_is_shortened() -> None:
    name = 'v1.0.0-' + 'x' * 100 + '-blocked'
    findings: dict[_audit.AuditFinding, _audit.HistoryEvidence] = {}
    _audit._add_findings(
        findings,
        f'tag:{name}',
        name.encode(),
        {RuleClass.IDENTITY_CRITICAL: ('blocked',)},
        (None, 'tag name'),
    )

    with color_context('never'):
        lines = audit_cli._finding_lines(
            next(iter(findings.values())), ('global sensitive_terms entry 1',)
        )

    assert lines[0] == f'  tag {name}'
    assert lines[1] == '    Rule: global sensitive_terms, item 1'
    assert lines[2] == '    Matched: blocked'
    assert lines[3].startswith('    Text: ...')
    assert not any('Commit:' in line for line in lines)


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

    assert (
        'audit: local copy of published history; verified against GitHub'
        in capsys.readouterr().out
    )


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
    assert (
        'pubanon repo verify --project=demo; use pubanon repo restore --project=demo --to local '
        'only if GitHub is authoritative, '
        'or see "Accepting an outside fix" in the operator guide if GitHub changed '
        'outside Pubanon'
    ) in err
    assert 'merge' not in err
    assert 'pull request' not in err


def test_audit_remote_refuses_drift_before_audit_output(
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

    assert _cli.main(['audit', '--project', project.name, '--remote']) == 1

    captured = capsys.readouterr()
    assert (
        'pubanon repo verify --project=demo; use pubanon repo restore --project=demo --to local'
        in captured.err
    )
    assert 'if GitHub changed outside Pubanon' in captured.err
    assert 'outside Pubanon' in captured.err
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
    assert 'No configured sensitive terms were found in the checked history.' in output
    assert 'Historical exposure acknowledged.' not in output


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
            _candidate.store_path(paths, project.name),
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
    guidance = (
        'warning: if this history has been public, deleting or rewriting it does not erase '
        'copies, forks, or caches.'
    )
    assert output.count('Warning: historical exposure') == 1
    assert output.count('Warning: policy mismatch') == 1
    assert output.index('Warning: historical exposure') < output.index('Warning: policy mismatch')
    assert (
        'Historical exposure unacknowledged; publish requires interactive acknowledgment.'
        in output
    )
    assert 'Rule: global sensitive_terms, item 2; project extra_terms, item 1' in output
    assert 'Rule: project hygiene_terms, item 1' in output
    assert (
        output.count(
            f'Published snapshot: v1.0.0 ({record.commit if verbose else record.commit[:12]})'
        )
        == 2
    )
    assert 'Commit:' not in output
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
    assert guidance in output
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
            _candidate.store_path(paths, project.name),
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
    assert 'Content auditing could not be completed reliably.' in output
    assert 'No configured sensitive terms were found in the checked history.' not in output


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
        paths, project.name, commits, tags, rules, ctx.global_config, tip=main
    ).findings
    digest = _audit.exposure_digest(findings)
    assert digest
    _config.save_project(paths, replace(project, acknowledged_exposure=digest))
    capsys.readouterr()

    assert _cli.main(['audit', '--project', project.name]) == 0

    output = capsys.readouterr().out
    assert 'Historical exposure acknowledged.' in output
    assert 'exposure unacknowledged:' not in output


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
    assert (
        'Historical exposure unacknowledged; publish requires interactive acknowledgment.'
        in output
    )
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
