from collections.abc import Callable
from pathlib import Path

import pytest

from pubanon import (
    _cli,
    _config,
)
from pubanon._errors import PubanonError
from pubanon._remote import _stage
from tests._setup import configure


@pytest.mark.parametrize('operation', ['delete', 'forget', 'repair'])
@pytest.mark.parametrize('yes', [False, True])
def test_stage_operation_uses_concrete_operation_and_holds_project_lock(
    operation: str,
    yes: bool,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    received: list[tuple[object, ...]] = []

    class _StageModule:
        @staticmethod
        def _stage_operation(
            actual_paths: _config.Paths,
            actual_global: _config.GlobalConfig,
            actual_project: _config.ProjectConfig,
            *,
            yes: bool,
        ) -> None:
            with (
                pytest.raises(PubanonError, match='another Pubanon command is running'),
                _config.project_lock(paths, project.name),
            ):
                pytest.fail('stage operation did not hold its project lock')
            received.append((actual_paths, actual_global, actual_project, yes))

    monkeypatch.setattr(_stage, f'stage_{operation}', _StageModule._stage_operation)

    argv = ['stage', operation, '--project', project.name]
    if yes:
        argv.append('--yes')
    assert _cli.main(argv) == 0
    assert received == [(paths, global_config, project, yes)]


def test_stage_push_uses_concrete_operation_and_holds_project_lock(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    received: list[tuple[object, ...]] = []

    class _StageModule:
        @staticmethod
        def _stage_push(
            actual_paths: _config.Paths,
            actual_global: _config.GlobalConfig,
            actual_project: _config.ProjectConfig,
            *,
            yes: bool,
        ) -> None:
            with (
                pytest.raises(PubanonError, match='another Pubanon command is running'),
                _config.project_lock(paths, project.name),
            ):
                pytest.fail('stage push did not hold its project lock')
            received.append((actual_paths, actual_global, actual_project, yes))

    monkeypatch.setattr(_stage, 'stage_push', _StageModule._stage_push)

    assert _cli.main(['stage', 'push', '--project', project.name, '--yes']) == 0
    assert received == [(paths, global_config, project, True)]


def test_stage_status_uses_concrete_operation_and_holds_project_lock(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    received: list[tuple[object, ...]] = []

    class _StageModule:
        @staticmethod
        def _stage_status(
            actual_paths: _config.Paths,
            actual_global: _config.GlobalConfig,
            actual_project: _config.ProjectConfig,
        ) -> None:
            with (
                pytest.raises(PubanonError, match='another Pubanon command is running'),
                _config.project_lock(paths, project.name),
            ):
                pytest.fail('stage status did not hold its project lock')
            received.append((actual_paths, actual_global, actual_project))

    monkeypatch.setattr(_stage, 'stage_status', _StageModule._stage_status)

    assert _cli.main(['stage', 'status', '--project', project.name]) == 0
    assert received == [(paths, global_config, project)]
