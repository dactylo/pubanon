import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from pubanon._errors import PubanonError
from pubanon._run import GitResult
from pubanon._snapshots import _store
from tests._git import git_env
from tests.snapshots.test_candidate import _paths


def test_candidate_store_initialization_reports_child_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _fail(_args: Sequence[str], **_kwargs: object) -> GitResult:
        return GitResult(7, b'', b'diagnostic\x1b\\path')

    monkeypatch.setattr(_store, 'run_git', _fail)
    with pytest.raises(PubanonError, match='cannot initialize snapshot store') as error:
        _store.store_path(_paths(tmp_path), 'example')
    assert error.value.fact == r'exit status 7: diagnostic\x1b\\path'


@pytest.mark.parametrize('project', ['', '../outside', 'nested/project'])
def test_candidate_store_path_refuses_invalid_project_names(
    tmp_path: Path,
    project: str,
) -> None:
    paths = _paths(tmp_path)

    with pytest.raises(PubanonError, match='invalid project name'):
        _store.candidate_store_path(paths, project)

    assert not paths.data_dir.exists()


def test_candidate_store_path_returns_absent_store_without_creating_it(tmp_path: Path) -> None:
    paths = _paths(tmp_path)

    store = _store.candidate_store_path(paths, 'example')

    assert store == paths.data_dir / 'projects' / 'example' / 'store.git'
    assert not store.exists()


def test_store_path_initializes_project_bare_repository(tmp_path: Path) -> None:
    paths = _paths(tmp_path)

    store = _store.store_path(paths, 'example')

    assert store == paths.data_dir / 'projects' / 'example' / 'store.git'
    result = subprocess.run(
        ['git', '-C', store, 'rev-parse', '--is-bare-repository'],
        check=True,
        capture_output=True,
        env=git_env(),
    )
    assert result.stdout == b'true\n'
