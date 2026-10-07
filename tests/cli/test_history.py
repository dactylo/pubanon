import os
import re
import shlex
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Never

import pytest

from pubanon import _cli, _config
from pubanon._cli import _history as cli_history
from pubanon._remote import _host
from pubanon._snapshots import (
    _candidate,
    _discard,
    _history,
    _inspection,
    _records,
    _source,
    _store,
    _verification,
)
from pubanon._terminal import escape
from tests._cli import prepare, record_publication, record_published_main
from tests._git import git_env, remove_tree
from tests._setup import configure


def test_delete_prepared_directs_to_discard_before_any_change(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch)
    inspection = _verification.materialize_inspect(
        paths, project.name, _verification.prove_objects(paths, project.name, candidate)
    )
    capsys.readouterr()

    assert _cli.main(['delete', 'prepared', '--project', project.name]) == 1
    captured = capsys.readouterr()
    assert 'pubanon discard' in captured.err
    assert _records.load_candidate(paths, project.name) == candidate
    assert inspection.exists()


def test_history_ambiguous_recorded_tag_lists_exact_choices(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch, version='1.2.3')
    record = record_publication(paths, project, candidate)
    other_commit = (
        subprocess.run(
            [
                'git',
                '-C',
                _store.candidate_store_path(paths, project.name),
                'commit-tree',
                candidate.tree,
                '-p',
                candidate.commit,
                '-m',
                'Second publication',
            ],
            check=True,
            capture_output=True,
            env=git_env(),
        )
        .stdout.decode()
        .strip()
    )
    _records.save_publication(
        paths,
        project.name,
        replace(
            record,
            commit=other_commit,
            source_bookmark=_source.describe_bookmark(source, project.name, other_commit),
        ),
    )
    capsys.readouterr()

    assert _cli.main(['show', 'v1.2.3', '--project', project.name]) == 1
    captured = capsys.readouterr()
    assert 'ambiguous published snapshot selector' in captured.err
    assert candidate.commit in captured.err
    assert other_commit in captured.err
    assert captured.out == ''


def test_history_confirmation_uses_local_date_and_offset(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    if sys.platform == 'win32':
        pytest.skip('requires POSIX TZ and tzset to change the process timezone')
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch)
    record_published_main(paths, project, candidate)
    record = record_publication(paths, project, candidate)
    _records.publication_path(paths, project.name, candidate.commit).unlink()
    _records.save_publication(
        paths, project.name, replace(record, confirmed_at=datetime(2026, 9, 11, 1, 30, tzinfo=UTC))
    )
    capsys.readouterr()
    try:
        with monkeypatch.context() as local:
            local.setenv('TZ', 'America/Los_Angeles')
            time.tzset()
            assert _cli.main(['show', 'latest', '--project', project.name]) == 0
            expected_time = '2026-09-10 18:30:00 PDT'
            assert any(
                'Published:' in line and expected_time in line
                for line in capsys.readouterr().out.splitlines()
            )
            assert _cli.main(['list', '--project', project.name]) == 0
            assert any(
                candidate.commit[:12] in line and expected_time in line
                for line in capsys.readouterr().out.splitlines()
            )
            assert _cli.main(['status', '--project', project.name]) == 0
            assert any(
                'Published:' in line and expected_time in line
                for line in capsys.readouterr().out.splitlines()
            )
    finally:
        time.tzset()


def test_history_deduplicates_confirmed_snapshot_awaiting_local_finalization(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    published = record_publication(paths, project, record)
    pending = replace(
        record, state=_candidate.STATE_UNKNOWN_OUTCOME, source_bookmark=published.source_bookmark
    )
    _candidate.save_candidate(paths, project.name, pending)
    capsys.readouterr()
    assert _cli.main(['list', '--project', project.name]) == 0
    rendered = re.sub(r': +', ': ', capsys.readouterr().out)
    assert rendered.count(record.commit[:12]) == 1
    assert rendered.startswith('Snapshots\nID ')
    assert 'Version' in rendered
    assert 'Published' in rendered
    assert [record.commit[:12], 'v1.0.0', 'published'] in [
        line.split()[:3] for line in rendered.splitlines()
    ]
    assert 'Local recording: incomplete\n' in rendered
    assert 'unresolved' not in rendered
    assert rendered.rstrip().endswith('Next: pubanon publish --project=demo --resolve')
    assert _records.load_candidate(paths, project.name) == pending


def test_history_delete_hides_recordless_entry(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch)
    record_published_main(paths, project, candidate)
    _discard.discard(paths, project.name, force=False)
    capsys.readouterr()
    assert _cli.main(['delete', 'latest', '--project', project.name, '--yes']) == 0
    assert 'Deleted the local snapshot' in capsys.readouterr().out
    assert not _history.publications(paths, project.name)
    assert _cli.main(['show', candidate.commit, '--project', project.name]) == 1


@pytest.mark.slow
def test_history_delete_preserves_unrelated_pending_candidate(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch)
    entry = record_publication(paths, project, candidate)
    unrelated = replace(candidate, commit='d' * 40, state=_candidate.STATE_UNKNOWN_OUTCOME)
    _candidate.save_candidate(paths, project.name, unrelated)
    assert _cli.main(['delete', entry.commit, '--project', project.name, '--yes']) == 0
    assert _records.load_candidate(paths, project.name) == unrelated


@pytest.mark.slow
def test_history_delete_preserves_user_managed_source_worktree(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch)
    bookmark = _source.describe_bookmark(source, project.name, candidate.commit)
    _source.ensure_bookmark(bookmark, candidate.pinned.commit)
    record_publication(paths, project, replace(candidate, source_bookmark=bookmark))
    _records.candidate_path(paths, project.name).unlink()
    worktree = tmp_path / 'user managed source'
    subprocess.run(
        ['git', '-C', source, 'checkout', '--detach', candidate.pinned.commit],
        check=True,
        capture_output=True,
        env=git_env(),
    )
    subprocess.run(
        ['git', '-C', source, 'worktree', 'add', '--detach', worktree, candidate.pinned.commit],
        check=True,
        capture_output=True,
        env=git_env(),
    )
    (worktree / 'notes').write_text('keep notes')
    capsys.readouterr()
    assert _cli.main(['delete', candidate.commit, '--project', project.name, '--yes']) == 0
    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'Preserved source worktree (can still retain objects)' in output
    assert shlex.join(['git', '-C', str(source), 'worktree', 'remove', str(worktree)]) in output
    assert output.count(' worktree remove ') == 1
    assert 'worktree add' not in output
    assert (worktree / 'notes').read_text() == 'keep notes'
    assert _source.source_recovery(bookmark, candidate.pinned.commit).status == 'available'


@pytest.mark.slow
def test_history_delete_rechecks_refs_after_confirmation(
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answers: Callable[[list[tuple[str, str]]], None],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(source.parent, source, monkeypatch)
    bookmark = _source.describe_bookmark(source, project.name, candidate.commit)
    _source.ensure_bookmark(bookmark, candidate.pinned.commit)
    record_publication(paths, project, replace(candidate, source_bookmark=bookmark))
    _records.candidate_path(paths, project.name).unlink()
    subprocess.run(
        ['git', '-C', source, 'commit', '--allow-empty', '-qm', 'later source'],
        check=True,
        env=git_env(),
    )
    original = cli_history.confirm

    def _change_then_confirm(question: str) -> bool:
        subprocess.run(
            ['git', '-C', source, 'update-ref', bookmark.refname, 'HEAD'],
            check=True,
            env=git_env(),
        )
        return original(question)

    monkeypatch.setattr(cli_history, 'confirm', _change_then_confirm)
    answers([(f'Delete local snapshot {candidate.commit[:12]}? [y/n] ', 'y')])
    capsys.readouterr()
    assert _cli.main(['delete', candidate.commit, '--project', project.name]) == 1
    assert 'source bookmark changed' in capsys.readouterr().err
    assert _records.load_publication(paths, project.name, candidate.commit) is not None
    assert _candidate.publication_archive(paths, project.name, candidate.commit)


@pytest.mark.slow
def test_history_delete_retries_recordless_archive_after_tombstone(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch)
    _discard.discard(paths, project.name, force=False)
    store = _store.candidate_store_path(paths, project.name)
    archive = _records.publication_ref(candidate.commit)
    subprocess.run(
        ['git', '-C', store, 'update-ref', archive, candidate.commit], check=True, env=git_env()
    )
    archive_lock = store / (archive + '.lock')
    archive_lock.touch()
    args = ['delete', candidate.commit, '--project', project.name, '--yes']

    assert _cli.main(args) == 1
    assert candidate.commit in _records.deleted_publications(paths, project.name)
    assert _candidate.publication_archive(paths, project.name, candidate.commit)
    assert not _history.publications(paths, project.name)
    failure = capsys.readouterr().err
    assert 'deletion saved' in failure
    assert 'Remaining: archive bookmark' in failure
    archive_lock.unlink()
    assert _cli.main(args) == 0
    assert not _candidate.publication_archive(paths, project.name, candidate.commit)
    assert not _history.publications(paths, project.name)


def test_history_empty_does_not_create_store(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    monkeypatch.chdir(tmp_path)

    assert _cli.main(['list', '--project', project.name]) == 0

    assert capsys.readouterr().out == (
        'No recorded snapshot history.\n\nNext: pubanon prepare --project=demo\n'
    )
    assert not _store.candidate_store_path(paths, project.name).exists()


@pytest.mark.slow
def test_history_missing_objects_retains_known_record(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch)
    record_publication(paths, project, candidate)
    remove_tree(_store.candidate_store_path(paths, project.name))
    capsys.readouterr()

    assert _cli.main(['show', candidate.commit, '--project', project.name, '--verbose']) == 0
    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert f'Snapshot commit: {candidate.commit}\n' in output
    assert 'Published snapshot' in output
    assert f'Original source: {candidate.pinned.commit}\n' in output
    assert (
        'Change summary and published inspection unavailable: local Git objects are missing'
        in output
    )
    assert 'pubanon repo restore --project=demo --to local' in output
    assert _records.publication_path(paths, project.name, candidate.commit).exists()


@pytest.mark.slow
def test_history_renders_replaced_derived_source_input_and_recorded_visibility(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    record_published_main(paths, project, candidate)
    recorded = record_publication(paths, project, candidate)
    _records.publication_path(paths, project.name, candidate.commit).unlink()
    _records.save_publication(
        paths,
        project.name,
        replace(
            recorded,
            pinned=None,
            derivation=_records.Derivation(
                original='b' * 40,
                original_source=candidate.pinned.commit,
                correction='c' * 40,
            ),
        ),
    )
    store = _store.candidate_store_path(paths, project.name)
    replacement = (
        subprocess.run(
            ['git', '-C', store, 'commit-tree', candidate.tree, '-m', 'Current publication'],
            check=True,
            capture_output=True,
            env=git_env(),
        )
        .stdout.decode()
        .strip()
    )
    subprocess.run(
        ['git', '-C', store, 'update-ref', 'refs/heads/main', replacement],
        check=True,
        env=git_env(),
    )
    _config.save_project(paths, replace(project, visibility='public'))
    capsys.readouterr()

    assert _cli.main(['list', '--project', project.name]) == 0
    listing = re.sub(r': +', ': ', capsys.readouterr().out)
    assert any(
        line.split()[:3] == [candidate.commit[:12], 'v1.0.0', 'superseded']
        for line in listing.splitlines()
    )
    assert _cli.main(['show', '--project', project.name, candidate.commit, '--verbose']) == 0
    detail = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'Visibility at publication: private' in detail
    assert f'Original snapshot: {"b" * 40}' in detail
    assert f'Correction snapshot: {"c" * 40}' in detail
    assert f'Source input: {candidate.pinned.commit}' in detail
    assert 'Source commit:' not in detail


def test_history_reports_deletion_receipt_scan_failure(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch)
    record_published_main(paths, project, candidate)
    _discard.discard(paths, project.name, force=False)
    _records.mark_publication_deleted(paths, project.name, candidate.commit)
    directory = _records.publication_path(paths, project.name, candidate.commit).parent
    real_scandir = os.scandir
    target_scans = 0

    def fail_second_target(path: os.PathLike[str] | str) -> object:
        nonlocal target_scans
        if Path(path) == directory:
            target_scans += 1
            if target_scans == 2:
                raise PermissionError('injected deletion listing failure')
        return real_scandir(path)

    monkeypatch.setattr(_records.os, 'scandir', fail_second_target)
    capsys.readouterr()

    assert _cli.main(['list', '--project', project.name]) == 1
    captured = capsys.readouterr()
    assert 'cannot list local snapshot deletions' in captured.err
    assert candidate.commit[:12] not in captured.out


def test_history_reports_publication_record_scan_failure(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    directory = _records.publication_path(paths, project.name, 'a' * 40).parent
    directory.mkdir(parents=True)
    real_scandir = os.scandir

    def fail_target(path: os.PathLike[str] | str) -> object:
        if Path(path) == directory:
            raise PermissionError('injected record listing failure')
        return real_scandir(path)

    monkeypatch.setattr(_records.os, 'scandir', fail_target)
    capsys.readouterr()

    assert _cli.main(['list', '--project', project.name]) == 1
    captured = capsys.readouterr()
    assert 'cannot list published snapshot records' in captured.err
    assert 'No recorded snapshot history.' not in captured.out


@pytest.mark.slow
def test_history_summary_uses_selected_parent_and_bounds_file_listing(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch)
    record_published_main(paths, project, candidate)
    _records.candidate_path(paths, project.name).unlink()
    for index in range(25):
        (source / f'file-{index:02}.txt').write_text('added\n')
    subprocess.run(['git', '-C', source, 'add', '.'], check=True, env=git_env())
    subprocess.run(['git', '-C', source, 'commit', '-qm', 'Add files'], check=True, env=git_env())
    assert (
        _cli.main(['prepare', '--project', project.name, '--acknowledge-new-paths', '--yes']) == 0
    )
    selected = _records.load_candidate(paths, project.name)
    assert selected is not None
    record_published_main(paths, project, selected)
    capsys.readouterr()

    assert _cli.main(['show', selected.commit, '--project', project.name]) == 0
    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert '25 files changed, 25 insertions(+)' in output
    assert output.count('.txt |') == 20
    assert 'README.md' not in output


@pytest.mark.slow
def test_show_prepared_snapshot_by_explicit_selector(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch)
    capsys.readouterr()

    assert _cli.main(['-p', project.name, 'show', 'prepared', '--verbose']) == 0
    shown = re.sub(r': +', ': ', capsys.readouterr().out)
    assert 'Prepared snapshot' in shown
    assert candidate.commit in shown
    assert _cli.main(['inspect', '--project', project.name]) == 0
    assert _inspection.inspection_paths(paths, project.name)


@pytest.mark.slow
@pytest.mark.parametrize(
    'case',
    [
        (command, tags, verbose)
        for command in ('list', 'show')
        for tags in (False, True)
        for verbose in (False, True)
    ],
)
def test_verbose_history_finds_latest_source_contents_after_rewrite(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[str, bool, bool],
) -> None:
    command, source_tags, verbose = case
    source = make_repo(files={'README.md': 'published\n'})
    subprocess.run(['git', '-C', source, 'branch', '-m', 'main'], check=True, env=git_env())
    paths, project, candidate = prepare(tmp_path, source, monkeypatch, version='1.2.3')
    project = replace(project, source_tags=source_tags)
    _config.save_project(paths, project)
    published = record_publication(paths, project, candidate)
    record_published_main(paths, project, candidate)
    _records.candidate_path(paths, project.name).unlink()
    if source_tags:
        subprocess.run(
            ['git', '-C', source, 'tag', 'pubanon/v1.2.3', candidate.pinned.commit],
            check=True,
            env=git_env(),
        )
    subprocess.run(
        ['git', '-C', source, 'commit', '--amend', '-qm', 'rewritten message'],
        check=True,
        env=git_env(),
    )
    subprocess.run(
        ['git', '-C', source, 'commit', '--allow-empty', '-qm', 'identical contents again'],
        check=True,
        env=git_env(),
    )
    latest = (
        subprocess.check_output(['git', '-C', source, 'rev-parse', 'HEAD'], env=git_env())
        .decode()
        .strip()
    )
    capsys.readouterr()

    argv = [command, '--project', project.name]
    if verbose:
        argv.append('--verbose')
    if command == 'show':
        argv.append('latest')
    assert _cli.main(argv) == 0
    raw_output = capsys.readouterr().out
    output = re.sub(r': +', ': ', raw_output)
    if verbose:
        columns: set[int] = set()
        for line in [*raw_output.splitlines(), '']:
            field = re.match(r'[^:]+:( +)\S', line)
            if field:
                columns.add(field.end() - 1)
            else:
                assert len(columns) <= 1
                columns.clear()

    assert (f'Original source: {candidate.pinned.commit}\n' in output) == verbose
    assert (f'Latest match on main: {latest}\n' in output) == verbose
    assert ('Number of matches: 2\n' in output) == verbose
    if command == 'show':
        changes = output.split('Changes\n', 1)[1].splitlines()
        assert changes[0].startswith('  ')
        assert changes[1].startswith('  ')
        if verbose:
            source_fields = raw_output.split('Latest match on main:', 1)[1]
            assert '\n\nSource:' in source_fields
    assert _records.load_publication(paths, project.name, candidate.commit) == published
    tags = subprocess.run(
        ['git', '-C', source, 'show-ref', '--tags'],
        check=False,
        capture_output=True,
        env=git_env(),
    ).stdout.decode()
    assert tags == (f'{latest} refs/tags/pubanon/v1.2.3\n' if source_tags else '')


@pytest.mark.slow
@pytest.mark.parametrize('source_tags', [False, True])
def test_verbose_history_reports_zero_matches_without_losing_original(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    source_tags: bool,
) -> None:
    source = make_repo(files={'README.md': 'published\n'})
    subprocess.run(['git', '-C', source, 'branch', '-m', 'main'], check=True, env=git_env())
    paths, project, candidate = prepare(tmp_path, source, monkeypatch)
    project = replace(project, source_tags=source_tags)
    _config.save_project(paths, project)
    published = record_publication(paths, project, candidate)
    record_published_main(paths, project, candidate)
    _records.candidate_path(paths, project.name).unlink()
    (source / 'README.md').write_text('different\n')
    subprocess.run(['git', '-C', source, 'add', 'README.md'], check=True, env=git_env())
    subprocess.run(
        ['git', '-C', source, 'commit', '--amend', '-qm', 'changed'], check=True, env=git_env()
    )
    capsys.readouterr()

    assert _cli.main(['show', 'latest', '--project', project.name, '--verbose']) == 0
    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert f'Original source: {candidate.pinned.commit}\n' in output
    assert 'Latest match on main: none\n' in output
    assert 'Number of matches: 0\n' in output
    assert _records.load_publication(paths, project.name, candidate.commit) == published
    if source_tags:
        target = (
            subprocess.check_output(
                ['git', '-C', source, 'rev-parse', f'refs/tags/pubanon/{candidate.commit}'],
                env=git_env(),
            )
            .decode()
            .strip()
        )
        assert target == candidate.pinned.commit


@pytest.mark.slow
@pytest.mark.parametrize('choice', ['decline', 'confirm', 'yes'])
def test_history_delete_confirms_exact_local_cleanup(
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answers: Callable[[list[tuple[str, str]]], None],
    choice: str,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(source.parent, source, monkeypatch, version='1.2.3')
    bookmark = _source.describe_bookmark(source, project.name, candidate.commit)
    _source.ensure_bookmark(bookmark, candidate.pinned.commit)
    candidate = replace(candidate, source_bookmark=bookmark)
    record_publication(paths, project, candidate)
    record_published_main(paths, project, candidate)
    _records.candidate_path(paths, project.name).unlink()
    if choice != 'yes':
        answers(
            [
                (
                    'Delete local snapshot v1.2.3? [y/n] ',
                    'n' if choice == 'decline' else 'y',
                )
            ]
        )
    capsys.readouterr()
    args = ['delete', '--project', project.name, 'v1.2.3']
    if choice == 'yes':
        args.append('--yes')
    assert _cli.main(args) == (130 if choice == 'decline' else 0)
    captured = capsys.readouterr()
    output = captured.out
    assert candidate.commit[:12] in output
    assert (
        'Delete this local snapshot entry, review copy, and source recovery information.' in output
    )
    assert 'GitHub and source working directories are unchanged.' in output
    if choice == 'decline':
        assert 'Canceled. Nothing was deleted.' in captured.err
        assert _records.load_publication(paths, project.name, candidate.commit) is not None
        assert _source.source_recovery(bookmark, candidate.pinned.commit).status == 'protected'
    else:
        assert 'Deleted the local snapshot and recovery information.' in output
        assert _records.load_publication(paths, project.name, candidate.commit) is None
        assert _source.source_recovery(bookmark, candidate.pinned.commit).status == 'available'
        assert not _history.publications(paths, project.name)


@pytest.mark.parametrize('verbose', [False, True])
def test_history_displays_incomplete_replacement_without_a_publishable_tip(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verbose: bool,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    incomplete = replace(record, replaces='a' * 40, replace_scope='one', tip=None)

    def _load_candidate(_paths: _config.Paths, _project: str) -> _candidate.CandidateRecord:
        return incomplete

    monkeypatch.setattr(_records, 'load_candidate', _load_candidate)
    capsys.readouterr()
    argv = ['list', '--project', project.name]
    if verbose:
        argv.append('--verbose')
    assert _cli.main(argv) == 0
    rendered = re.sub(r': +', ': ', capsys.readouterr().out)
    snapshot = record.commit if verbose else record.commit[:12]
    assert [snapshot, 'untagged', 'incomplete', '-'] in [
        line.split() for line in rendered.splitlines()
    ]
    assert 'Preparation stopped before the replacement was ready.' in rendered
    assert ('Operation: replacement\n' in rendered) == verbose
    assert rendered.rstrip().endswith('Next: pubanon prepare --project=demo --continue')
    assert _records.load_rewrite(paths, project.name) is None


@pytest.mark.parametrize('unknown', [False, True])
def test_history_empty_keeps_candidate_recovery_priority(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    unknown: bool,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch)
    if unknown:
        _candidate.save_candidate(
            paths, project.name, replace(candidate, state=_candidate.STATE_UNKNOWN_OUTCOME)
        )
    capsys.readouterr()

    assert _cli.main(['list', '--project', project.name]) == 0
    expected = (
        'pubanon publish --project=demo --resolve' if unknown else 'pubanon inspect --project=demo'
    )
    rendered = re.sub(r': +', ': ', capsys.readouterr().out)
    state = 'unresolved' if unknown else 'prepared'
    assert [candidate.commit[:12], 'untagged', state, '-'] in [
        line.split() for line in rendered.splitlines()
    ]
    if unknown:
        assert 'pubanon could not confirm whether publication completed.' in rendered
    assert rendered.rstrip().endswith(f'Next: {expected}')


@pytest.mark.slow
@pytest.mark.parametrize('case', [(False, None), (False, '1.2.3'), (True, None), (True, '1.2.3')])
def test_history_lists_and_shows_local_publication(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[bool, str | None],
) -> None:
    recorded, version = case
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch, version=version)
    record_published_main(paths, project, candidate)
    if version and not recorded:
        subprocess.run(
            [
                'git',
                '-C',
                _store.candidate_store_path(paths, project.name),
                'update-ref',
                f'refs/tags/v{version}',
                candidate.commit,
            ],
            check=True,
            env=git_env(),
        )
    if recorded:
        bookmark = _source.describe_bookmark(source, project.name, candidate.commit)
        _source.ensure_bookmark(bookmark, candidate.pinned.commit)
        candidate = replace(candidate, source_bookmark=bookmark)
        _candidate.save_candidate(paths, project.name, candidate)
        record_publication(paths, project, candidate)
    _records.candidate_path(paths, project.name).unlink()
    monkeypatch.chdir(tmp_path)

    def _unexpected_host(*_args: object, **_kwargs: object) -> Never:
        pytest.fail('local snapshot history contacted the host')

    monkeypatch.setattr(_host, 'verify_destination', _unexpected_host)
    monkeypatch.setattr(_host, 'ls_remote', _unexpected_host)
    capsys.readouterr()

    assert _cli.main(['list', '--project', project.name]) == 0
    output = re.sub(r': +', ': ', capsys.readouterr().out)
    identity = 'v1.2.3' if version else 'untagged'
    row = next(
        line.split() for line in output.splitlines() if line.startswith(candidate.commit[:12])
    )
    assert row[:3] == [candidate.commit[:12], identity, 'published']
    assert 'Source:' not in output
    assert (row[3] != '-') == recorded
    assert 'Next: pubanon show --project=demo latest' in output

    assert _cli.main(['show', '--project', project.name, 'latest', '--verbose']) == 0
    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert f'Snapshot commit: {candidate.commit}\n' in output
    assert 'Changes\n  README.md | 1 +\n  1 file changed, 1 insertion(+)\n' in output
    if recorded:
        assert f'Original source: {candidate.pinned.commit}\n' in output
        assert 'Destination: anonymous-account/public-repo' in output
        assert 'Visibility at publication: private' in output
        assert output.count('Source: protected at ') == 1
        assert (
            shlex.join(['git', '-C', str(source), 'worktree', 'add', '--detach']) + ' ' in output
        )
        assert 'Published: ' in output
    else:
        assert 'Source: not recorded' in output
        assert 'Commit time: ' in output
        assert 'Destination:' not in output
        assert 'Published:' not in output
    assert output.rstrip().endswith(
        f'Next: pubanon inspect --project=demo --published {candidate.commit}'
    )


@pytest.mark.parametrize('complete', [False, True])
def test_history_lists_correction_and_rebuilt_snapshots_with_their_own_versions(
    complete: bool,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    _paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    replacement = replace(
        record,
        replaces='a' * 40,
        replace_scope='one',
        tip='b' * 40,
        prestate_tags={'v1.0.0': 'a' * 40, 'v1.1.0': 'c' * 40},
        poststate_tags={'v1.0.0': record.commit, 'v1.1.0': 'b' * 40},
        replayed=(_records.ReplayEntry(old='c' * 40, new='b' * 40, original_source='d' * 40),),
    )

    if not complete:
        progress = _records.RewriteRecord(
            correction=record.commit,
            replaces='a' * 40,
            prestate_main='c' * 40,
            descendants=('c' * 40,),
            phase='replaying',
            replayed=replacement.replayed,
        )
        replacement = replace(replacement, tip=None, poststate_tags=None, replayed=())

        def _load_rewrite(_paths: _config.Paths, _project: str) -> _records.RewriteRecord:
            return progress

        monkeypatch.setattr(_records, 'load_rewrite', _load_rewrite)

    def _load_candidate(_paths: _config.Paths, _project: str) -> _candidate.CandidateRecord:
        return replacement

    monkeypatch.setattr(_records, 'load_candidate', _load_candidate)
    capsys.readouterr()
    assert _cli.main(['list', '--project', project.name]) == 0
    rendered = re.sub(r': +', ': ', capsys.readouterr().out)
    state = 'prepared' if complete else 'incomplete'
    rows = [line.split() for line in rendered.splitlines()]
    assert [record.commit[:12], 'v1.0.0', *state.split(), '-'] in rows
    assert ['bbbbbbbbbbbb', 'v1.1.0', *state.split(), '-'] in rows
    assert 'Operation:' not in rendered
    assert record.commit not in rendered
    assert _cli.main(['list', '--project', project.name, '--verbose']) == 0
    verbose = re.sub(r': +', ': ', capsys.readouterr().out)
    assert f'Source input: {"d" * 40}\n' in verbose
    assert f'Source commit: {record.pinned.commit}\n' in verbose


@pytest.mark.slow
@pytest.mark.parametrize('missing_source', [False, True])
def test_history_reports_unprotected_or_missing_original_source(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    missing_source: bool,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch)
    bookmark = _source.describe_bookmark(source, project.name, candidate.commit)
    _source.ensure_bookmark(bookmark, candidate.pinned.commit)
    record_publication(paths, project, replace(candidate, source_bookmark=bookmark))
    monkeypatch.chdir(tmp_path)
    if missing_source:
        remove_tree(source)
    else:
        _source.remove_bookmark(bookmark, candidate.pinned.commit)
    capsys.readouterr()

    assert _cli.main(['list', '--project', project.name, '--verbose']) == 0
    state = 'unavailable' if missing_source else 'available'
    assert f'Source: {state}' in re.sub(r': +', ': ', capsys.readouterr().out)
    assert _cli.main(['show', candidate.commit, '--project', project.name, '--verbose']) == 0
    output = re.sub(r': +', ': ', capsys.readouterr().out)
    if missing_source:
        assert f'Recorded source repository: {escape((source).as_posix())}\n' in output
        assert 'Source unavailable:' in output
    else:
        assert f'Source: available at {escape((source).as_posix())}\n' in output
        assert 'View source: git ' in output
        assert 'Remove source worktree: git ' in output


@pytest.mark.parametrize('mode', ['redirected', 'terminal', 'no_color'])
def test_history_styles_labels_without_changing_copy(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch)
    record_published_main(paths, project, candidate)
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: mode != 'redirected')
    monkeypatch.delenv('NO_COLOR', raising=False)
    if mode == 'no_color':
        monkeypatch.setenv('NO_COLOR', '1')
    capsys.readouterr()

    assert _cli.main(['list', '--project', project.name]) == 0
    output = re.sub(r': +', ': ', capsys.readouterr().out)
    assert ('\x1b[' in output) == (mode == 'terminal')
    assert output.count(candidate.commit[:12]) == 1
    assert 'published' in output
    assert output.count('Next:') == 1
