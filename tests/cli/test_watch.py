from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from pubanon import (
    _cli,
    _config,
)
from pubanon._remote import _watch
from tests._cli import prepare, record_published_main
from tests._setup import configure


def test_watch_defaults_reach_observation_functions(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    received: list[tuple[object, ...]] = []

    class _WatchModule:
        @staticmethod
        def _watch_ci(
            *args: object,
            staged: bool,
            version: str | None,
            timeout: int,
        ) -> None:
            actual_paths, actual_global, actual_project = args

            received.append(
                (
                    actual_paths,
                    actual_global,
                    actual_project,
                    {'staged': staged, 'version': version, 'timeout': timeout},
                )
            )

        @staticmethod
        def _watch_package(
            *args: object,
            version: str | None,
            timeout: int,
        ) -> None:
            actual_paths, actual_global, actual_project = args

            received.append(
                (
                    actual_paths,
                    actual_global,
                    actual_project,
                    {'version': version, 'timeout': timeout},
                )
            )

    monkeypatch.setattr(_watch, 'watch_ci', _WatchModule._watch_ci)
    monkeypatch.setattr(_watch, 'watch_package', _WatchModule._watch_package)

    assert _cli.main(['watch', 'ci', '--project', project.name]) == 0
    assert _cli.main(['watch', 'package', '--project', project.name]) == 0
    assert received == [
        (
            paths,
            global_config,
            project,
            {'staged': False, 'version': None, 'timeout': _watch.DEFAULT_TIMEOUT},
        ),
        (
            paths,
            global_config,
            project,
            {'version': None, 'timeout': _watch.DEFAULT_TIMEOUT},
        ),
    ]


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

    assert _cli.main(['watch', 'ci', '--project', project.name, '--timeout', '1']) == 1

    rendered = capsys.readouterr().err
    assert 'repository repo failed content gates' in rendered
    assert term in rendered
    assert not (tmp_path / 'fake-gh-calls.jsonl').exists()
