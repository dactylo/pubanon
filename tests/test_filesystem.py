import os
from pathlib import Path

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
