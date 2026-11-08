import os
import stat
from pathlib import Path

import pytest

from pubanon import _windows_fs

_WINDOWS = pytest.mark.skipif(
    os.name != 'nt', reason='requires native Windows filesystem behavior'
)


def test_read_entry_accepts_linked_parent_directory(tmp_path: Path) -> None:
    real = tmp_path / 'real'
    real.mkdir()
    (real / 'file').write_bytes(b'content')
    link = tmp_path / 'link'
    link.symlink_to(real, target_is_directory=True)
    mode, data = _windows_fs.read_entry(link / 'file')
    assert stat.S_ISREG(mode)
    assert data == b'content'


@pytest.mark.parametrize('target', ['file', './file', 'relative//file'])
def test_read_entry_preserves_exact_symlink_target(tmp_path: Path, target: str) -> None:
    link = tmp_path / 'link'
    link.symlink_to(target)
    mode, data = _windows_fs.read_entry(link)
    assert stat.S_ISLNK(mode)
    assert data == os.fsencode(target)


@_WINDOWS
def test_read_entry_reads_regular_file_and_symlink_bytes(tmp_path: Path) -> None:
    file = tmp_path / 'file'
    file.write_bytes(b'private\x00content')
    mode, data = _windows_fs.read_entry(file)
    assert stat.S_ISREG(mode)
    assert data == b'private\x00content'
    link = tmp_path / 'link'
    link.symlink_to('file')
    mode, data = _windows_fs.read_entry(link)
    assert stat.S_ISLNK(mode)
    assert data == b'file'


@_WINDOWS
def test_replace_commits_file_and_removes_source(tmp_path: Path) -> None:
    source = tmp_path / 'temporary'
    target = tmp_path / 'state'
    source.write_bytes(b'new')
    target.write_bytes(b'old')
    _windows_fs.replace(source, target)
    assert target.read_bytes() == b'new'
    assert not source.exists()
