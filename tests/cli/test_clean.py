import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from pubanon import _cli, _config
from pubanon._errors import PubanonError
from pubanon._filesystem import remove_tree
from pubanon._run import run_git
from pubanon._snapshots import _candidate, _history, _records, _source, _store, _verification
from pubanon._terminal import escape
from tests._cli import gh_calls, prepare, record_publication, record_published_main
from tests._setup import configure


@pytest.fixture
def cleanup_history(
    tmp_path: Path, make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> tuple[_config.Paths, _config.ProjectConfig, list[_candidate.CandidateRecord]]:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch)
    snapshots: list[_candidate.CandidateRecord] = []
    for index in range(3):
        if index:
            assert (
                _cli.main(
                    [
                        'prepare',
                        '--project',
                        project.name,
                        '--message',
                        f'Publication {index}',
                        '--yes',
                    ]
                )
                == 0
            )
            candidate = _records.load_candidate(paths, project.name)
            assert candidate is not None
        publication = record_publication(paths, project, candidate)
        candidate = replace(candidate, source_bookmark=publication.source_bookmark)
        snapshots.append(candidate)
        if index == 2:
            record_published_main(paths, project, candidate)
        _records.candidate_path(paths, project.name).unlink()
    assert (
        _cli.main(['prepare', '--project', project.name, '--message', 'Next publication', '--yes'])
        == 0
    )
    return paths, project, snapshots


@pytest.mark.slow
def test_clean_can_resume_after_partial_failure(
    cleanup_history: tuple[_config.Paths, _config.ProjectConfig, list[_candidate.CandidateRecord]],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answers: Callable[[list[tuple[str, str]]], None],
) -> None:
    paths, project, snapshots = cleanup_history
    entries = [entry for entry in _history.publications(paths, project.name) if entry.replaced]
    original = _history.delete_publication

    def _fail_second(
        selected_paths: _config.Paths,
        selected_project: _config.ProjectConfig,
        entry: _history.PublicationEntry,
    ) -> None:
        if entry.commit == entries[1].commit:
            raise PubanonError('injected cleanup failure')
        original(selected_paths, selected_project, entry)

    capsys.readouterr()
    with monkeypatch.context() as scoped:
        scoped.setattr(_history, 'delete_publication', _fail_second)
        assert _cli.main(['clean', '--project', project.name, '--yes']) == 1
    output = capsys.readouterr()
    assert f'Deleted local snapshot {entries[0].commit[:12]}.' in output.out
    assert 'injected cleanup failure' in output.err
    answers([('Delete this superseded local snapshot? [y/n] ', 'y')])
    assert _cli.main(['clean', '--project', project.name]) == 0
    assert {entry.commit for entry in _history.publications(paths, project.name)} == {
        snapshots[2].commit
    }


def test_clean_empty_does_not_create_store(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _global, project = configure(tmp_path, monkeypatch, make_repo())
    assert project is not None
    assert _cli.main(['clean', '--project', project.name]) == 0
    assert 'Nothing to remove.' in capsys.readouterr().out
    assert not _store.candidate_store_path(paths, project.name).exists()


def test_clean_only_review_copies_preserves_prepared_snapshot(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answers: Callable[[list[str]], None],
) -> None:
    paths, project, record = prepare(tmp_path, make_repo(), monkeypatch)
    inspection = _verification.materialize_inspect(
        paths, project.name, _verification.prove_objects(paths, project.name, record)
    )
    older = inspection.parent / ('a' * 40)
    older.mkdir()
    (older / 'notes').write_text('private review notes')
    answers(['n'])
    capsys.readouterr()

    assert _cli.main(['clean', '--project', project.name]) == 130
    captured = capsys.readouterr()
    assert 'Canceled. Nothing was deleted.' in captured.err
    output = captured.out
    assert escape((inspection).as_posix()) in output
    assert escape((older).as_posix()) in output
    assert 'The listed review copies will be removed.' in output
    assert 'source recovery information will be removed' not in output
    assert inspection.exists()
    assert older.exists()
    assert _records.load_candidate(paths, project.name) == record

    answers(['y'])
    assert _cli.main(['clean', '--project', project.name]) == 0
    assert not inspection.exists()
    assert not older.exists()
    assert _records.load_candidate(paths, project.name) == record


def test_clean_only_review_copies_yes_skips_prompt(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _global, project = configure(tmp_path, monkeypatch, make_repo())
    assert project is not None
    copy = paths.data_dir / 'projects' / project.name / 'inspections' / ('a' * 40)
    copy.mkdir(parents=True)
    (copy / 'notes').write_text('notes')
    capsys.readouterr()

    assert _cli.main(['clean', '--project', project.name, '--yes']) == 0
    assert not copy.exists()
    output = capsys.readouterr().out
    assert escape((copy).as_posix()) in output
    assert 'The listed review copies will be removed.' in output


@pytest.mark.slow
def test_clean_refuses_without_local_published_history(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch)
    publication = record_publication(paths, project, candidate)
    _records.candidate_path(paths, project.name).unlink()
    remove_tree(_store.candidate_store_path(paths, project.name))
    capsys.readouterr()

    assert _cli.main(['clean', '--project', project.name, '--yes']) == 1

    error = capsys.readouterr().err
    assert 'without the local published history' in error
    assert '--to local' in error
    assert _records.publication_path(paths, project.name, candidate.commit).exists()
    bookmark = publication.source_bookmark
    assert bookmark is not None
    assert (
        run_git(['show-ref', '--verify', bookmark.refname], cwd=source, check=False).returncode
        == 0
    )


@pytest.mark.slow
def test_clean_preflights_every_snapshot_before_deleting(
    cleanup_history: tuple[_config.Paths, _config.ProjectConfig, list[_candidate.CandidateRecord]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, project, snapshots = cleanup_history
    entries = [entry for entry in _history.publications(paths, project.name) if entry.replaced]
    protected = next(item for item in snapshots if item.commit == entries[-1].commit)
    pending = replace(protected, state=_candidate.STATE_UNKNOWN_OUTCOME)
    _candidate.save_candidate(paths, project.name, pending)
    before = _history.publications(paths, project.name)
    capsys.readouterr()
    assert _cli.main(['clean', '--project', project.name, '--yes']) == 1
    assert 'required by an active snapshot or rewrite' in capsys.readouterr().err
    assert _history.publications(paths, project.name) == before
    assert _records.load_candidate(paths, project.name) == pending


@pytest.mark.slow
@pytest.mark.parametrize('choice', ['decline', 'confirm', 'yes'])
def test_clean_removes_only_superseded_snapshots(
    cleanup_history: tuple[_config.Paths, _config.ProjectConfig, list[_candidate.CandidateRecord]],
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    answers: Callable[[list[tuple[str, str]]], None],
    choice: str,
) -> None:
    paths, project, snapshots = cleanup_history
    pending = _records.load_candidate(paths, project.name)
    assert pending is not None
    copies = [
        _history.inspect_publication(
            paths, project.name, _history.select_publication(paths, project.name, item.commit)
        )
        for item in snapshots
    ]
    pending_copy = _verification.materialize_inspect(
        paths, project.name, _verification.prove_objects(paths, project.name, pending)
    )
    source_refs = run_git(['show-ref', '--heads', '--tags'], cwd=project.source_path).stdout
    published_refs = _candidate.published_refs(paths, project.name)

    if choice != 'yes':
        answers(
            [
                (
                    'Delete these 2 superseded local snapshots? [y/n] ',
                    'n' if choice == 'decline' else 'y',
                )
            ]
        )
    capsys.readouterr()
    args = ['clean', '--project', project.name]
    if choice == 'yes':
        args.append('--yes')
    with patch('subprocess.run', wraps=subprocess.run) as commands:
        assert _cli.main(args) == (130 if choice == 'decline' else 0)
    assert not any(
        argument in {'gc', 'prune', 'reflog'}
        for call in commands.call_args_list
        for argument in call.args[0]
    )
    output = capsys.readouterr().out
    assert all(item.commit[:12] in output for item in snapshots[:2])
    assert all(escape((copy).as_posix()) in output for copy in (*copies, pending_copy))
    remaining = {entry.commit for entry in _history.publications(paths, project.name)}
    assert remaining == {
        item.commit for item in (snapshots if choice == 'decline' else snapshots[2:])
    }
    assert _records.load_candidate(paths, project.name) == pending
    assert pending_copy.exists() == (choice == 'decline')
    assert copies[2].exists() == (choice == 'decline')
    assert _candidate.published_refs(paths, project.name) == published_refs
    assert (
        run_git(['show-ref', '--heads', '--tags'], cwd=project.source_path).stdout == source_refs
    )
    for candidate, directory in zip(snapshots[:2], copies[:2], strict=True):
        assert directory.exists() == (choice == 'decline')
        assert candidate.source_bookmark is not None
        assert _source.source_recovery(
            candidate.source_bookmark, candidate.pinned.commit
        ).status == ('protected' if choice == 'decline' else 'available')
    assert gh_calls(tmp_path) == []
    if choice != 'decline':
        assert _cli.main(['clean', '--project', project.name]) == 0
        assert 'Nothing to remove.' in capsys.readouterr().out
