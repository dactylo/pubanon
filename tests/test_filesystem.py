import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from pubanon import _filesystem


def test_durable_unlink_refuses_a_missing_file_without_residue(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        _filesystem.durable_unlink(tmp_path / 'missing')
    assert not list(tmp_path.glob('.pubanon-delete-*'))


def test_durable_unlink_removes_only_the_selected_file(tmp_path: Path) -> None:
    file = tmp_path / 'record.toml'
    neighbor = tmp_path / 'other.toml'
    file.write_bytes(b'private')
    neighbor.write_bytes(b'keep')
    _filesystem.durable_unlink(file)
    assert not file.exists()
    assert neighbor.read_bytes() == b'keep'
    assert list(tmp_path.glob('*.toml')) == [neighbor]
    assert not list(tmp_path.glob('.pubanon-delete-*'))


@pytest.mark.skipif(os.name != 'nt', reason='requires native Windows write-through replacement')
def test_failed_durable_removal_preserves_the_authoritative_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / 'record.toml'
    file.write_bytes(b'private')

    def _refuse_move(_source: Path, _target: Path) -> None:
        raise PermissionError('injected move failure')

    monkeypatch.setattr(_filesystem._windows_fs, 'replace', _refuse_move)
    with pytest.raises(PermissionError, match='injected move'):
        _filesystem.durable_unlink(file)
    assert file.read_bytes() == b'private'
    assert list(tmp_path.glob('*.toml')) == [file]
    assert not list(tmp_path.glob('.pubanon-delete-*'))


@pytest.mark.skipif(os.name != 'nt', reason='requires native Windows write-through replacement')
def test_failed_residue_cleanup_never_restores_the_authoritative_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    file = tmp_path / 'record.toml'
    file.write_bytes(b'private')
    original = Path.unlink

    def _refuse_cleanup(path: Path, *, missing_ok: bool = False) -> None:
        if path.name.startswith('.pubanon-delete-'):
            raise PermissionError('injected residue cleanup failure')
        original(path, missing_ok=missing_ok)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', _refuse_cleanup)
        with pytest.raises(PermissionError, match='residue cleanup'):
            _filesystem.durable_unlink(file)
    assert not file.exists()
    residue = list(tmp_path.glob('.pubanon-delete-*'))
    assert len(residue) == 1
    assert residue[0].name.startswith('.pubanon-delete-')
    assert residue[0].suffix == '.tmp'
    assert residue[0].read_bytes() == b'private'


def test_windows_cleanup_restores_file_access_without_changing_link_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / 'snapshot'
    nested = directory / 'nested'
    nested.mkdir(parents=True)
    artifact = nested / 'file'
    artifact.write_bytes(b'content')
    _filesystem.chmod(artifact, 0o400)
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'sentinel'
    sentinel.write_bytes(b'preserve')
    (nested / 'directory-link').symlink_to(outside, target_is_directory=True)
    (nested / 'file-link').symlink_to(sentinel)
    changes: list[tuple[Path, int, bool]] = []
    checked: list[Path] = []

    def _chmod(path: Path, mode: int, *, follow_symlinks: bool = True) -> None:
        changes.append((path, mode, follow_symlinks))

    monkeypatch.setattr(_filesystem, 'os', SimpleNamespace(name='nt', scandir=os.scandir))
    monkeypatch.setattr(_filesystem._windows_fs, 'check_directory', checked.append)
    monkeypatch.setattr(_filesystem, 'chmod', _chmod)
    _filesystem._restore_tree_access(directory)

    assert checked == [directory, nested]
    assert {path for path, _mode, _follow in changes} == {directory, nested, artifact}
    assert (artifact, 0o600, False) in changes
    assert all(not follow for _path, _mode, follow in changes)
    assert sentinel.read_bytes() == b'preserve'


def test_write_private_preserves_uncertainty_when_replace_finishes_then_interrupts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / 'private'
    directory.mkdir()
    path = directory / 'record'
    path.write_bytes(b'old')
    original_replace = _filesystem.replace

    def _replace_then_interrupt(source: Path, destination: Path) -> None:
        original_replace(source, destination)
        raise KeyboardInterrupt

    monkeypatch.setattr(_filesystem, 'replace', _replace_then_interrupt)
    with pytest.raises(_filesystem.DurableWriteError) as raised:
        _filesystem.write_private(path, b'new')

    assert not raised.value.replaced
    assert raised.value.replacement_attempted
    assert raised.value.interrupted
    assert path.read_bytes() == b'new'
    assert list(directory.iterdir()) == [path]


def test_write_private_replaces_content_with_private_mode(tmp_path: Path) -> None:
    directory = tmp_path / 'private'
    directory.mkdir()
    path = directory / 'record'
    path.write_bytes(b'old')
    path.chmod(0o644)
    neighbor = directory / 'record.tmp-other'
    neighbor.write_bytes(b'keep')

    _filesystem.write_private(path, b'new')

    assert path.read_bytes() == b'new'
    if os.name != 'nt':
        assert path.stat().st_mode & 0o777 == 0o600
    assert neighbor.read_bytes() == b'keep'
    assert set(directory.iterdir()) == {path, neighbor}


@pytest.mark.parametrize('failure', [OSError, KeyboardInterrupt])
@pytest.mark.parametrize('phase', ['file_sync', 'replace', 'directory_sync'])
def test_write_private_reports_failure_phase_and_cleans_temporary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    phase: str,
    failure: type[BaseException],
) -> None:
    directory = tmp_path / 'private'
    directory.mkdir()
    path = directory / 'record'
    path.write_bytes(b'old')
    error = failure('injected failure')

    def _fail(*_args: object) -> None:
        raise error

    if phase == 'file_sync':
        monkeypatch.setattr(os, 'fsync', _fail)
    else:
        monkeypatch.setattr(_filesystem, phase if phase == 'replace' else 'sync_directory', _fail)

    with pytest.raises(_filesystem.DurableWriteError) as raised:
        _filesystem.write_private(path, b'new')

    assert raised.value.replaced is (phase == 'directory_sync')
    assert raised.value.replacement_attempted is (phase != 'file_sync')
    assert raised.value.interrupted is (failure is KeyboardInterrupt)
    assert raised.value.__cause__ is error
    assert path.read_bytes() == (b'new' if phase == 'directory_sync' else b'old')
    assert list(directory.iterdir()) == [path]
