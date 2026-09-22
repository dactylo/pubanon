import os
import shlex
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from pubanon import (
    _cli,
)
from pubanon._cli import _arguments
from pubanon._snapshots import _candidate, _records, _source
from pubanon._terminal import escape
from tests._cli import prepare
from tests._permissions import assert_private_mode
from tests._setup import configure
from tests.cli.test_prepare import replacement_context


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
    assert argv == ['pubanon', 'inspect', f'--project={name}', '--clean']


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
    assert 'rerun inspect --clean' in output
    assert '--project=line' not in output


def test_discard_reports_preserved_inspection_copies(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    inspection = _candidate.materialize_inspect(paths, project.name, record)
    reviewed = (inspection / 'README.md').read_bytes()
    capsys.readouterr()
    assert _cli.main(['discard', '--project', project.name]) == 0
    output = capsys.readouterr().out
    assert 'Discarded the prepared snapshot.\n\nPreserved snapshot directories\n' in output
    assert escape(str(inspection)) in output
    assert 'pubanon inspect --project=demo --clean' in output
    assert (inspection / 'README.md').read_bytes() == reviewed
    assert_private_mode(inspection, 0o500)
    assert_private_mode(inspection / 'README.md', 0o400)
    assert _cli.main(['status', '--project', project.name]) == 0
    status = capsys.readouterr().out
    assert 'Prepared snapshot: none' in status
    assert (
        'Snapshot directories: 1\nView directories: pubanon inspect --project=demo --verbose\n'
        in status
    )
    assert escape(str(inspection)) not in status
    assert '--clean' not in status


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


def test_discard_rewrite_removes_private_progress_and_preserves_inspection(
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
    reviewed = (inspection / 'README.md').read_bytes()
    assert _cli.main(['prepare', '--project', project.name, '--replace', 'v1.0.0', '--yes']) == 0
    assert _records.rewrite_path(paths, project.name).exists()
    assert _records.rewrite_directory(paths, project.name).is_dir()
    capsys.readouterr()
    assert _cli.main(['discard', '--project', project.name]) == 0
    output = capsys.readouterr().out
    assert 'Discarded the prepared snapshot.' in output
    assert 'pubanon inspect --project=demo --clean' in output
    assert _candidate.load_candidate(paths, project.name) is None
    assert not _records.rewrite_path(paths, project.name).exists()
    assert not _records.rewrite_directory(paths, project.name).exists()
    assert (inspection / 'README.md').read_bytes() == reviewed
