import re
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from pubanon import (
    _cli,
    _config,
    _run,
)
from pubanon._cli import _context, _status
from pubanon._errors import ObservationTimeoutError, PubanonError
from pubanon._remote import _host
from pubanon._snapshots import _candidate, _records, _store, _verification
from pubanon._snapshots._source import PinnedSource, SourceBookmark
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
    _store.store_path(paths, project.name)
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

    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert output.startswith('Project demo (direct route)\n')
    assert any(line.split() == ['Prepared:', 'none'] for line in output.splitlines())
    assert any(line.split() == ['Published:', 'none'] for line in output.splitlines())
    assert 'state was changed' not in output
    assert 'Details:' not in output
    assert 'Next: pubanon prepare --project=demo\n' in output
    assert output.rstrip().splitlines()[-1].startswith('Next:')


@pytest.mark.parametrize(
    'case',
    [
        ('manual', False),
        ('manual', True),
        ('package', False),
        ('package', True),
        ('timeout', True),
        ('excluded', True),
    ],
    ids=[
        'manual-current',
        'manual-invalidated',
        'package-current',
        'package-invalidated',
        'package-read-timeout',
        'metadata-excluded',
    ],
)
def test_status_changed_source_offers_existing_candidate_unless_settings_changed(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[str, bool],
) -> None:
    version_source, blocked = case
    files = {'README.md': 'public\n'}
    if version_source != 'manual':
        files['pyproject.toml'] = '[project]\nversion = "0.0.1"\n'
    source = make_repo(files=files)
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='0.0.1')
    (source / 'README.md').write_text('updated public content\n')
    subprocess.run(['git', '-C', source, 'add', 'README.md'], check=True, env=git_env())
    subprocess.run(
        ['git', '-C', source, 'commit', '-qm', 'Update content'], check=True, env=git_env()
    )
    if version_source == 'excluded':
        _config.save_project(paths, replace(project, deny=('pyproject.toml',)))
    elif blocked:
        _config.save_project(paths, replace(project, extra_terms=('new-private-term',)))
    if version_source == 'timeout':
        run_git = _run.run_git

        def _timeout_package_read(
            args: Iterable[str],
            *,
            cwd: Path | None,
            check: bool = True,
            timeout: float | None = None,
        ) -> _run.GitResult:
            arguments = list(args)
            if arguments == ['show', 'HEAD:pyproject.toml']:
                assert timeout == 5.0
                raise ObservationTimeoutError
            return run_git(
                arguments,
                cwd=cwd,
                check=check,
                timeout=timeout,
            )

        monkeypatch.setattr(_run, 'run_git', _timeout_package_read)
    capsys.readouterr()

    assert _cli.main(['status', '--project', project.name]) == 0

    expected_action = (
        'Next: pubanon discard --project=demo && pubanon prepare --project=demo'
        + ('' if version_source == 'package' else ' --version 0.0.1')
        + '\n'
        if blocked
        else 'Next: pubanon inspect --project=demo\n'
    )
    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert output.startswith('Project demo (direct route)\n')
    assert 'Prepared: v0.0.1' in output
    assert re.search(r'The source (branch|tag) \S+ has changed', output)
    assert 'Source revision changed since preparation' not in output
    assert ('pubanon settings have changed' in output) is blocked
    assert any(line.split() == ['Published:', 'none'] for line in output.splitlines())
    assert output.endswith(expected_action)
    assert _records.load_candidate(paths, project.name) == record


def test_status_cli_reports_one_publication_and_next_action(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch, version='1.2.3')
    record_published_main(paths, project, candidate)
    record_publication(paths, project, candidate)
    capsys.readouterr()

    assert _cli.main(['status', '--project', project.name]) == 0

    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert output.count('Published:') == 1
    assert output.rstrip().splitlines()[-1].startswith('Next:')


@pytest.mark.slow
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
        assert (
            _cli.main(['prepare', '--project', project.name, '--replace', 'v1.0.0', '--yes']) == 1
        )
    error = capsys.readouterr().err
    assert 'cannot clean completed rewrite record' in error
    assert 'Next: pubanon prepare --project=demo --continue' in error
    record = _records.load_candidate(paths, project.name)
    progress = _records.load_rewrite(paths, project.name)
    assert record is not None
    assert record.tip is not None
    assert progress is not None
    capsys.readouterr()
    assert _cli.main(['status', '--project', project.name]) == 0
    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'Replacement preparation completed' in output
    assert 'local cleanup is pending' in output
    assert 'Prepared snapshot\n' not in output
    assert record.tip not in output
    assert output.rstrip().endswith('Next: pubanon prepare --project=demo --continue')
    assert _records.load_candidate(paths, project.name) == record
    assert _records.load_rewrite(paths, project.name) == progress
    assert _cli.main(['prepare', '--project', project.name, '--continue']) == 0
    assert _records.load_candidate(paths, project.name) == record
    assert _records.load_rewrite(paths, project.name) is None


@pytest.mark.slow
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

    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'Snapshot published' in output
    assert 'local recording needs completion' in output
    assert output.rstrip().endswith('Next: pubanon publish --project=demo --resolve')


@pytest.mark.slow
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
        _host.RemoteRefs('f' * 40 if result == 'mismatch' else candidate.commit, {}),
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
        assert 'Configured visibility: public' in captured.out
        assert re.search(r'Current visibility: +public', captured.out)
    else:
        assert 'Current visibility: public' not in captured.out
    if result == 'refused':
        assert 'repository visibility does not match configuration' in captured.err
    elif result == 'mismatch':
        assert 'GitHub differs' in captured.out
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

    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'pubanon settings have changed' in output
    assert 'Next: pubanon discard --project=demo && pubanon prepare --project=demo' in output
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

    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'Prepared: untagged' in output
    assert 'Next: pubanon inspect --project=demo\n' in output
    assert output.rstrip().splitlines()[-1].startswith('Next:')


@pytest.mark.parametrize('correction', [False, pytest.param(True, marks=pytest.mark.slow)])
def test_status_remote_empty_destination_routes_to_restore(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    correction: bool,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    if correction:
        paths, project, _original = replacement_context(tmp_path, source, monkeypatch)
        assert _cli.main(['prepare', '--project', project.name, '--replace', '--yes']) == 0
    else:
        paths, project, record = prepare(tmp_path, source, monkeypatch)
        record_published_main(paths, project, record)
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account='anonymous-account',
        visibility=project.visibility,
    )
    stub_remote(monkeypatch, destination, _host.RemoteRefs(None, {}))
    capsys.readouterr()

    assert _cli.main(['status', '--project', project.name, '--remote']) == 0

    output = capsys.readouterr().out
    assert output.rstrip().endswith('Next: pubanon repo restore --project=demo --to remote')
    assert ('Recreating the final repository' in output) is correction


def test_status_remote_mismatch_resolves_unknown_publication_first(
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

    assert _cli.main(['status', '--project', project.name, '--remote']) == 0

    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'GitHub differs' in output
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
    _store.store_path(paths, project.name)
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
    assert 'remove every destination branch except main' in error


@pytest.mark.slow
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
    record = _records.load_candidate(paths, project.name)
    assert record is not None
    _config.save_project(paths, replace(project, extra_terms=('new-private-term',)))
    capsys.readouterr()
    assert _cli.main(['status', '--project', project.name]) == 0
    output = re.sub(r': +', ': ', capsys.readouterr().out)
    selected = 'all' if selector == 'all' else original.commit
    assert (
        f'pubanon prepare --project=demo --replace {selected} '
        f'--source {record.pinned.commit} --version 1.0.0' in output
    )
    assert output.count('Next:') == 1


@pytest.mark.slow
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
    store = _store.candidate_store_path(paths, project.name)
    subprocess.run(
        ['git', '-C', store, 'update-ref', '-d', _candidate.CANDIDATE_REF],
        check=True,
        env=git_env(),
    )
    record = _records.load_candidate(paths, project.name)
    saved = _records.load_rewrite(paths, project.name)

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail('status invoked mutating rewrite verification')

    monkeypatch.setattr(_verification, 'verify_rewrite_progress', _forbidden)
    capsys.readouterr()
    assert _cli.main(['status', '--project', project.name]) == 0
    output = re.sub(r': +', ': ', capsys.readouterr().out)
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
            escape(
                (_records.rewrite_directory(paths, project.name) / 'resolutions.toml').as_posix()
            )
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
    assert _records.load_candidate(paths, project.name) == record
    assert _records.load_rewrite(paths, project.name) == saved
    assert (
        subprocess.run(
            ['git', '-C', store, 'show-ref', '--verify', '--quiet', _candidate.CANDIDATE_REF],
            check=False,
            env=git_env(),
        ).returncode
        == 1
    )


@pytest.mark.parametrize(
    ('selector', 'label'),
    [
        ('refs/heads/main\x1b[31m', 'source branch main'),
        ('refs/tags/v1\x1b[31m', 'source tag v1'),
        ('refs/custom/name\x1b[31m', 'source ref refs/custom/name'),
    ],
)
def test_status_selector_text_escapes_hostile_names(selector: str, label: str) -> None:
    assert _status._selector_text(selector) == f'{label}\\x1b[31m'


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
    concise = re.sub(r': +', ': ', capsys.readouterr().out)
    assert concise.startswith('Project demo (direct route)\n')
    assert 'Prepared: v1.2.3' in concise
    assert 'since preparation' not in concise
    assert 'has changed since this snapshot was prepared' not in concise
    assert record.commit not in concise
    assert escape((source).as_posix()) not in concise
    assert '0 replayed' not in concise
    assert _cli.main(['status', '--project', project.name, '--verbose']) == 0
    verbose = re.sub(r': +', ': ', capsys.readouterr().out)
    assert record.pinned.selector_kind == 'ref'
    assert record.pinned.selector_name is not None
    assert (
        f'Source selector: {escape(record.pinned.selector_name)} (unchanged since preparation)'
        in verbose
    )
    assert record.commit in verbose
    assert record.pinned.commit in verbose
    assert escape((source).as_posix()) in verbose
    assert escape((_store.candidate_store_path(paths, project.name)).as_posix()) in verbose


def test_status_summarizes_copies_without_listing_paths(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    copy = paths.data_dir / 'projects' / project.name / 'inspections' / ('a' * 40)
    copy.mkdir(parents=True)

    assert _cli.main(['status', '--project', project.name]) == 0

    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'Review copies: 1' in output
    assert 'View directories:' not in output
    assert 'Next: pubanon prepare --project=demo' in output
    assert str(copy) not in output


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
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str | None, str],
) -> None:
    version, change = case
    source = tmp_path / 'source'
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    commit = 'a' * 40
    record = _records.PublicationRecord(
        commit=commit,
        tree='b' * 40,
        snapshot_digest='c' * 64,
        fingerprint='d' * 64,
        message='Public snapshot',
        tag_intent=f'v{version}' if version else None,
        pinned=PinnedSource('e' * 40, 'ref', 'refs/heads/main', 'e' * 40),
        destination=_records.RecordedDestination(
            'github', project.owner, project.repo, project.visibility, 'anonymous-account'
        ),
        confirmed_at=datetime(2026, 9, 25, tzinfo=UTC),
        source_bookmark=SourceBookmark(
            source.resolve(),
            source.resolve() / '.git',
            f'refs/pubanon/publications/{project.name}/{commit}',
        ),
    )
    _records.save_publication(paths, project.name, record)
    if change == 'visibility':
        project = replace(project, visibility='public')
    elif change == 'destination':
        project = replace(project, repo='another-repo', visibility='public')

    rows = _status._publication_status(
        _context.ProjectContext(paths, global_config, project),
        commit,
        {record.tag_intent: commit} if record.tag_intent else {},
        None,
    )
    output = '\n'.join(f'{label}: {value}' for label, value in rows)
    identity = f'v{version}' if version else 'untagged'
    assert f'Published: {identity} to anonymous-account/public-repo (private)' in output
    assert (
        datetime(2026, 9, 25, tzinfo=UTC).astimezone().strftime('%Y-%m-%d %H:%M:%S %Z') in output
    )
    assert (
        'Configured visibility: public' in output
        if change == 'visibility'
        else 'Configured visibility: public' not in output
    )
    if change == 'destination':
        assert 'Configured destination: anonymous-account/another-repo (public)' in output
    assert 'currently ' not in output
    assert commit not in output
    assert output.count('Published:') == 1


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

    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'pubanon settings have changed' in output
    assert 'Snapshot with unknown publish outcome' in output
    assert 'Next: pubanon publish --project=demo --resolve\n' in output
    assert output.rstrip().splitlines()[-1].startswith('Next:')
