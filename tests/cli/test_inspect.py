import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Never

import pytest

from pubanon import (
    _cli,
    _config,
    _filesystem,
)
from pubanon._remote import _host, _stage
from pubanon._snapshots import _candidate, _history, _source
from pubanon._terminal import escape
from tests._cli import prepare, record_published_main
from tests._git import git_env, remove_tree
from tests._permissions import assert_private_mode
from tests._setup import configure


@pytest.mark.parametrize(
    'case',
    [
        ('direct', 'malformed', 'pubanon publish --project=demo'),
        ('staged', 'absent', 'pubanon stage push --project=demo'),
        ('staged', 'matching', 'pubanon stage watch --project=demo'),
        ('staged', 'malformed', 'pubanon stage repair --project=demo'),
    ],
)
def test_inspect_guides_saved_route_without_host_reads_and_keeps_review_copy(
    case: tuple[str, str, str],
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    route, tracking, expected = case
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    project = replace(project, publication_route=route)
    _config.save_project(paths, project)
    if tracking == 'matching':
        _stage._save_stage(
            paths,
            project.name,
            _stage.StageRecord(record.commit, project.owner, _config.stage_repository(project)),
        )
        project = replace(project, required_checks=('build',))
        _config.save_project(paths, project)
        record = replace(
            record, fingerprint=_candidate.fingerprint(_config.load_global(paths), project)
        )
        _candidate.save_candidate(paths, project.name, record)
    elif tracking == 'malformed':
        stage_path = paths.data_dir / 'projects' / project.name / 'stage.toml'
        stage_path.parent.mkdir(parents=True, exist_ok=True)
        stage_path.write_text('invalid = true\n')

    def _unexpected_host(*_args: object, **_kwargs: object) -> Never:
        pytest.fail('host read')

    monkeypatch.setattr(_host, 'verify_destination', _unexpected_host)
    capsys.readouterr()

    assert _cli.main(['inspect', '--project', project.name]) == 0

    output = capsys.readouterr().out
    assert output.rstrip().endswith(f'Next: {expected}')
    inspection = paths.data_dir / 'projects' / project.name / 'inspections' / record.commit
    assert (inspection / 'README.md').read_text() == 'public\n'


def test_inspect_lists_preserved_copies_without_a_candidate(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    root = paths.data_dir / 'projects' / project.name
    copies = [root / 'inspections' / (oid * 40) for oid in ('a', 'b')]
    for copy in copies:
        copy.mkdir(parents=True)
        (copy / 'notes.txt').write_text('Keep my review edits.\n')

    assert _cli.main(['inspect', '--project', project.name]) == 0

    output = capsys.readouterr().out
    assert output.startswith(
        'No prepared snapshot is available.\nPreserved snapshot directories: 2\n'
    )
    for copy in copies:
        assert output.count(escape(str(copy)) + '\n') == 1
        assert (copy / 'notes.txt').read_text() == 'Keep my review edits.\n'
    assert 'Open these directories in your editor to review their files.' in output
    assert output.rstrip().endswith('`pubanon clean --project=demo`.')
    assert 'pubanon prepare' not in output
    assert _candidate.load_candidate(paths, project.name) is None

    assert _cli.main(['inspect', '--project', project.name, '--refresh']) == 1
    assert 'no prepared snapshot is retained' in capsys.readouterr().err
    for copy in copies:
        assert (copy / 'notes.txt').read_text() == 'Keep my review edits.\n'


def test_inspect_materializes_retained_candidate_after_configuration_drift(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, _record = prepare(tmp_path, source, monkeypatch)
    paths.global_config.write_text(
        'anon_name = "Changed Publisher"\n'
        'anon_email = "changed@example.invalid"\n'
        'sensitive_terms = ["public"]\n'
        'default_publication_route = "staged"\n'
        '[accounts]\n'
        'github = "changed-account"\n',
        encoding='utf-8',
    )
    _config.save_project(paths, replace(project, extra_terms=('new-private-term',)))
    (source / 'README.md').write_text('changed source\n', encoding='utf-8')
    subprocess.run(['git', '-C', source, 'add', 'README.md'], check=True, env=git_env())
    subprocess.run(
        ['git', '-C', source, 'commit', '-qm', 'change source'], check=True, env=git_env()
    )

    def _unexpected_authority_read(*_args: object, **_kwargs: object) -> Never:
        pytest.fail('inspection read current source or host authority')

    monkeypatch.setattr(_source, 'check_source_safety', _unexpected_authority_read)
    monkeypatch.setattr(_source, 'pin', _unexpected_authority_read)
    monkeypatch.setattr(_source, 'read_snapshot', _unexpected_authority_read)
    monkeypatch.setattr(_host, 'verify_destination', _unexpected_authority_read)
    older = paths.data_dir / 'projects' / project.name / 'inspections' / ('a' * 40)
    older.mkdir(parents=True)
    capsys.readouterr()

    assert _cli.main(['inspect', '--project', project.name]) == 0

    output = capsys.readouterr().out
    inspection = paths.data_dir / 'projects' / project.name / 'inspections' / _record.commit
    assert escape(str(inspection)) in output
    assert 'Publication configuration: changed since prepare' in output
    assert 'checks have not been rerun with these settings' in output
    assert output.index('Publication configuration:') < output.index(escape(str(inspection)))
    assert escape(str(older)) not in output
    assert output.count('Next:') == 1
    assert f'pubanon prepare --project=demo --source {_record.pinned.commit}' in output
    assert (inspection / 'README.md').read_text(encoding='utf-8') == 'public\n'
    assert_private_mode(inspection, 0o500)
    assert_private_mode(inspection / 'README.md', 0o400)

    assert _cli.main(['inspect', '--project', project.name, '--verbose']) == 0
    verbose = capsys.readouterr().out
    assert escape(str(inspection)) in verbose
    assert (inspection / 'README.md').read_text() == 'public\n'
    assert 'Directories from earlier snapshots\n' in verbose
    assert escape(str(older)) in verbose
    assert 'pubanon clean' in verbose
    assert verbose.rstrip().splitlines()[-1].startswith('Next:')


def test_inspect_published_materializes_files_without_source_or_candidate(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch)
    record_published_main(paths, project, candidate)
    _candidate.record_path(paths, project.name).unlink()
    monkeypatch.chdir(tmp_path)
    remove_tree(source)
    capsys.readouterr()

    assert (
        _cli.main(['inspect', '--project', project.name, '--published', candidate.commit[:12]])
        == 0
    )
    inspection = paths.data_dir / 'projects' / project.name / 'inspections' / candidate.commit
    assert (inspection / 'README.md').read_text() == 'public\n'
    output = capsys.readouterr().out
    assert escape(str(inspection)) in output
    assert output.rstrip().endswith(f'Next: pubanon show --project=demo {candidate.commit[:12]}')


@pytest.mark.slow
def test_inspect_published_refuses_modified_copy_until_refresh(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch)
    record_published_main(paths, project, candidate)
    assert candidate.pinned is not None
    bookmark = _source.describe_bookmark(source, project.name, candidate.commit)
    _source.ensure_bookmark(bookmark, candidate.pinned.commit)
    _history.record_publication(paths, project, replace(candidate, source_bookmark=bookmark))
    inspection = _candidate.materialize_inspect(paths, project.name, candidate)
    _filesystem.chmod(inspection / 'README.md', 0o600)
    (inspection / 'README.md').write_text('edited\n')
    older = inspection.parent / ('a' * 40)
    older.mkdir()
    (older / 'notes').write_text('keep\n')
    _candidate.record_path(paths, project.name).unlink()
    monkeypatch.chdir(tmp_path)

    def _unexpected_authority(*_args: object, **_kwargs: object) -> Never:
        pytest.fail('published inspection accessed current source or host')

    monkeypatch.setattr(_host, 'verify_destination', _unexpected_authority)
    monkeypatch.setattr(_source, 'pin', _unexpected_authority)
    capsys.readouterr()

    command = ['inspect', '--project', project.name, '--published']
    assert _cli.main(command) == 1
    error = capsys.readouterr().err
    assert f'--published {candidate.commit} --refresh' in error
    assert (inspection / 'README.md').read_text() == 'edited\n'
    assert _cli.main([*command, '--refresh']) == 0
    output = capsys.readouterr().out
    assert escape(str(inspection)) in output
    assert escape(str(older)) not in output
    assert (inspection / 'README.md').read_text() == 'public\n'
    assert_private_mode(inspection, 0o500)
    assert_private_mode(inspection / 'README.md', 0o400)
    assert (older / 'notes').read_text() == 'keep\n'
    assert output.rstrip().endswith(f'Next: pubanon show --project=demo {candidate.commit[:12]}')


def test_inspect_refresh_reports_exact_location_before_removal(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    inspection = _candidate.materialize_inspect(paths, project.name, record)
    older = inspection.parent / ('a' * 40)
    older.mkdir()
    (older / 'notes').write_text('older notes')
    original_rmtree = _candidate.shutil.rmtree
    removals: list[Path] = []
    notices: list[str] = []

    def _remove_after_notice(path: Path) -> None:
        output = capsys.readouterr().out
        notices.append(output)
        assert escape(str(path)) in ''.join(notices)
        assert 'All current contents of this directory will be replaced.' in ''.join(notices) or (
            'All listed directories and their contents will be removed.' in ''.join(notices)
        )
        original_rmtree(path)
        removals.append(path)

    monkeypatch.setattr(_candidate.shutil, 'rmtree', _remove_after_notice)
    capsys.readouterr()
    assert _cli.main(['inspect', '--project', project.name, '--refresh']) == 0
    assert removals == [inspection]
    assert (inspection / 'README.md').read_text() == 'public\n'
    assert (older / 'notes').read_text() == 'older notes'
    assert _candidate.load_candidate(paths, project.name) == record


def test_inspect_rewrite_delegates_selector_and_refresh(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    selected = paths.data_dir / 'projects' / project.name / 'inspections' / record.commit
    calls: list[tuple[str | None, bool]] = []

    def _inspect_rewritten(
        _paths: _config.Paths,
        _project: str,
        _record: _candidate.CandidateRecord,
        selector: str | None,
        *,
        refresh: bool = False,
    ) -> Path:
        calls.append((selector, refresh))
        return selected

    monkeypatch.setattr(_candidate, 'inspect_rewritten', _inspect_rewritten)

    def _select_rewritten_commit(_record: _candidate.CandidateRecord, _selector: str) -> str:
        return record.commit

    monkeypatch.setattr(_candidate, 'select_rewritten_commit', _select_rewritten_commit)

    def _inspect_report(
        _paths: _config.Paths,
        _record: _candidate.CandidateRecord,
        _global_config: _config.GlobalConfig,
        _project: _config.ProjectConfig,
        *,
        verbose: bool = False,
    ) -> str:
        assert verbose is False
        return 'Saved report'

    monkeypatch.setattr(_candidate, 'inspect_report', _inspect_report)
    capsys.readouterr()
    assert (
        _cli.main(['inspect', '--project', project.name, '--rewritten', 'v1.0.0', '--refresh'])
        == 0
    )
    assert calls == [('v1.0.0', True)]
    output = capsys.readouterr().out
    assert f'Replacing review copy: {escape(str(selected))}' in output
    assert escape(str(selected)) in output
    assert f'  Snapshot: {record.commit[:12]}\n' in output
    assert output.index('Saved report') < output.index('Review copy')
    assert (
        _cli.main(['inspect', '--project', project.name, '--rewritten', 'v1.0.0', '--published'])
        == 1
    )
    assert calls == [('v1.0.0', True)]
