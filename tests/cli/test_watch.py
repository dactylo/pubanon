from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from pubanon import (
    _cli,
    _config,
)
from tests._cli import prepare, record_published_main


def test_watch_gates_current_coordinates_before_host_access(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    term = 'private-coordinate'
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    record_published_main(paths, project, record)
    _config.save_project(
        paths,
        replace(
            project,
            repo=f'public-{term}',
            extra_terms=(term,),
            required_checks=('build',),
        ),
    )
    capsys.readouterr()

    assert _cli.main(['repo', 'watch', '--project', project.name, '--timeout', '1']) == 1

    rendered = capsys.readouterr().err
    assert 'repository name did not pass content checks' in rendered
    assert term in rendered
    assert not (tmp_path / 'fake-gh-calls.jsonl').exists()
