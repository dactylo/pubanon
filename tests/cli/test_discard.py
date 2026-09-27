import os
import shlex
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from pubanon import (
    _cli,
    _filesystem,
)
from pubanon._cli import _arguments
from pubanon._snapshots import _candidate, _records, _source
from pubanon._terminal import escape
from tests._cli import prepare
from tests._setup import configure
from tests.cli.test_prepare import replacement_context


def test_discard_copy_failure_keeps_record_for_retry(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, project, record = prepare(tmp_path, make_repo(), monkeypatch)
    inspection = _candidate.materialize_inspect(paths, project.name, record)
    with patch.object(_filesystem, 'remove_tree', side_effect=PermissionError('injected')):
        assert _cli.main(['discard', '--project', project.name]) == 1
    assert 'cannot clean review copy' in capsys.readouterr().err
    assert inspection.exists()
    assert _candidate.load_candidate(paths, project.name) == record

    assert _cli.main(['discard', '--project', project.name]) == 0
    assert not inspection.exists()
    assert _candidate.load_candidate(paths, project.name) is None


@pytest.mark.skipif(os.name != 'nt', reason='requires native Windows path aliases')
def test_discard_refuses_windows_project_aliases_without_mutating_candidate(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    assert record.source_bookmark is None
    record_path = _candidate.record_path(paths, project.name)
    ref_path = _candidate.store_path(paths, project.name) / _candidate.CANDIDATE_REF
    retained = record_path.read_bytes(), ref_path.read_bytes()
    capsys.readouterr()

    for alias in ('demo.', 'demo '):
        assert _cli.main(['discard', '--project', alias]) == 1
        assert 'invalid project name' in capsys.readouterr().err
        assert (record_path.read_bytes(), ref_path.read_bytes()) == retained

    assert _cli.main(['inspect', '--project', project.name]) == 0


@pytest.mark.parametrize('name', ['my project', "it's mine", '-leading'])
def test_discard_recovery_command_selects_project(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, source, project=name)
    assert project is not None
    copy = paths.data_dir / 'projects' / name / 'inspections' / ('a' * 40)
    copy.mkdir(parents=True)
    assert _cli.main(['discard', f'--project={name}']) == 0
    output = capsys.readouterr().out
    command = output.split('Next: ', 1)[1].strip()
    argv = shlex.split(command)
    assert _arguments.build_parser().parse_args(argv[1:]).project == name
    assert argv == ['pubanon', 'clean', f'--project={name}']


@pytest.mark.skipif(os.name == 'nt', reason='Windows filenames cannot contain newlines')
def test_discard_recovery_shows_unrenderable_project(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    name = 'line\nbreak'
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, source, project=name)
    assert project is not None
    (paths.data_dir / 'projects' / name / 'inspections' / ('a' * 40)).mkdir(parents=True)
    assert _cli.main(['discard', f'--project={name}']) == 0
    output = capsys.readouterr().out
    assert 'Choose project line\\x0abreak' in output
    assert 'rerun clean' in output
    assert '--project=line' not in output


def test_discard_removes_prepared_review_copies(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    inspection = _candidate.materialize_inspect(paths, project.name, record)
    capsys.readouterr()
    assert _cli.main(['discard', '--project', project.name]) == 0
    output = capsys.readouterr().out
    assert 'Discarded the prepared snapshot.' in output
    assert not inspection.exists()
    assert _cli.main(['status', '--project', project.name]) == 0
    status = capsys.readouterr().out
    assert any(line.split() == ['Prepared:', 'none'] for line in status.splitlines())
    assert 'Snapshot directories:' not in status
    assert escape(str(inspection)) not in status


@pytest.mark.slow
def test_discard_reports_removed_provisional_source_bookmark(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    bookmark = _source.describe_bookmark(source, project.name, record.commit)
    _candidate.save_candidate(paths, project.name, replace(record, source_bookmark=bookmark))
    _source.ensure_bookmark(bookmark, record.pinned.commit)
    capsys.readouterr()
    assert _cli.main(['discard', '--project', project.name]) == 0
    output = capsys.readouterr().out
    assert "Removed the snapshot's private source bookmark" in output
    assert 'Branches, tags, and files were not changed.' in output
    assert _source.source_recovery(bookmark, record.pinned.commit).status == 'available'


def test_discard_reports_when_candidate_and_inspection_directory_are_already_absent(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    _paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None

    assert _cli.main(['discard', '--project', project.name]) == 0

    assert capsys.readouterr().out == 'No prepared snapshot was found.\n'


@pytest.mark.slow
def test_discard_rewrite_removes_private_progress_and_keeps_publication_copy(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    paths, project, original = replacement_context(tmp_path, source, monkeypatch, conflict=True)
    inspection = _candidate.materialize_snapshot(
        paths,
        project.name,
        original.commit,
        _source.read_snapshot(
            _candidate.candidate_store_path(paths, project.name), original.commit
        ),
    )
    assert _cli.main(['prepare', '--project', project.name, '--replace', 'v1.0.0', '--yes']) == 0
    assert _records.rewrite_path(paths, project.name).exists()
    assert _records.rewrite_directory(paths, project.name).is_dir()
    capsys.readouterr()
    assert _cli.main(['discard', '--project', project.name]) == 0
    output = capsys.readouterr().out
    assert 'Discarded the prepared snapshot.' in output
    assert _candidate.load_candidate(paths, project.name) is None
    assert not _records.rewrite_path(paths, project.name).exists()
    assert not _records.rewrite_directory(paths, project.name).exists()
    assert inspection.exists()


def test_discard_unknown_outcome_keeps_record_and_review_copy(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, project, record = prepare(tmp_path, make_repo(), monkeypatch)
    inspection = _candidate.materialize_inspect(paths, project.name, record)
    pending = replace(record, state=_candidate.STATE_UNKNOWN_OUTCOME)
    _candidate.save_candidate(paths, project.name, pending)

    assert _cli.main(['discard', '--project', project.name]) == 1
    assert 'pubanon publish --project=demo --resolve' in capsys.readouterr().err
    assert inspection.exists()
    assert _candidate.load_candidate(paths, project.name) == pending
