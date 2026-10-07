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

    def _stage_operation(*args: object, yes: bool) -> None:
        with (
            pytest.raises(PubanonError, match='another pubanon command is running'),
            _config.project_lock(paths, project.name),
        ):
            pytest.fail('stage operation did not hold its project lock')
        received.append((*args, yes))

    monkeypatch.setattr(_stage, f'stage_{operation}', _stage_operation)

    argv = ['stage', operation, '--project', project.name]
    if yes:
        argv.append('--yes')
    assert _cli.main(argv) == 0
    expected = (
        (paths, project, yes)
        if operation == 'forget'
        else (
            paths,
            global_config,
            project,
            yes,
        )
    )
    assert received == [expected]


@pytest.mark.parametrize(
    ('full_tests', 'hosted'), [(False, False), (True, False), (False, True), (True, True)]
)
def test_stage_push_forwards_ci_options(
    full_tests: bool,
    hosted: bool,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    received: list[tuple[object, ...]] = []

    def _stage_push(
        actual_paths: _config.Paths,
        actual_global: _config.GlobalConfig,
        actual_project: _config.ProjectConfig,
        *,
        yes: bool,
        ci: _stage.StageCiOptions,
    ) -> None:
        with (
            pytest.raises(PubanonError, match='another pubanon command is running'),
            _config.project_lock(paths, project.name),
        ):
            pytest.fail('stage push did not hold its project lock')
        received.append((actual_paths, actual_global, actual_project, yes, ci))

    monkeypatch.setattr(_stage, 'stage_push', _stage_push)

    argv = ['stage', 'push', '--project', project.name, '--yes']
    if full_tests:
        argv.append('--full-tests')
    if hosted:
        argv.append('--hosted')
    assert _cli.main(argv) == 0
    assert received == [
        (
            paths,
            global_config,
            project,
            True,
            _stage.StageCiOptions(full_tests=full_tests, hosted=hosted),
        )
    ]


def test_stage_status_uses_concrete_operation_and_holds_project_lock(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    received: list[tuple[object, ...]] = []

    def _stage_status(
        actual_paths: _config.Paths,
        actual_global: _config.GlobalConfig,
        actual_project: _config.ProjectConfig,
    ) -> None:
        with (
            pytest.raises(PubanonError, match='another pubanon command is running'),
            _config.project_lock(paths, project.name),
        ):
            pytest.fail('stage status did not hold its project lock')
        received.append((actual_paths, actual_global, actual_project))

    monkeypatch.setattr(_stage, 'stage_status', _stage_status)

    assert _cli.main(['stage', 'status', '--project', project.name]) == 0
    assert received == [(paths, global_config, project)]
