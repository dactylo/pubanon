import os
import stat
from pathlib import Path

import pytest

from pubanon import _filesystem, _windows_fs

_WINDOWS = pytest.mark.skipif(
    os.name != 'nt', reason='requires native Windows filesystem behavior'
)


@_WINDOWS
def test_cleanup_permission_change_does_not_follow_link(tmp_path: Path) -> None:
    target = tmp_path / 'target'
    target.write_bytes(b'outside')
    _filesystem.chmod(target, 0o400)
    link = tmp_path / 'link'
    link.symlink_to(target)
    try:
        _filesystem.chmod(link, 0o600, follow_symlinks=False)
        link.unlink()
        with pytest.raises(PermissionError):
            target.write_bytes(b'changed')
    finally:
        _filesystem.chmod(target, 0o600)
    assert target.read_bytes() == b'outside'


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
def test_readonly_files_prevent_accidental_edits_and_allow_cleanup(tmp_path: Path) -> None:
    directory = tmp_path / 'inspection'
    directory.mkdir()
    _filesystem.chmod(directory, 0o700)
    file = directory / 'content'
    with file.open('wb') as stream:
        _filesystem.fchmod(stream.fileno(), 0o600)
        stream.write(b'private')
    _filesystem.chmod(file, 0o400)
    _filesystem.chmod(directory, 0o500)
    try:
        assert file.read_bytes() == b'private'
        with pytest.raises(PermissionError):
            file.write_bytes(b'changed')
        with pytest.raises(PermissionError):
            file.unlink()
    finally:
        _filesystem.chmod(directory, 0o700)
        _filesystem.chmod(file, 0o600)
    file.unlink()
    directory.rmdir()
    assert not directory.exists()


@_WINDOWS
def test_replace_commits_file_and_removes_source(tmp_path: Path) -> None:
    source = tmp_path / 'temporary'
    target = tmp_path / 'state'
    source.write_bytes(b'new')
    target.write_bytes(b'old')
    _windows_fs.replace(source, target)
    assert target.read_bytes() == b'new'
    assert not source.exists()


@_WINDOWS
@pytest.mark.parametrize('directory', [False, True])
def test_writable_cleanup_clears_existing_dos_readonly_attribute(
    tmp_path: Path, directory: bool
) -> None:
    path = tmp_path / 'readonly'
    if directory:
        path.mkdir()
    else:
        path.write_bytes(b'content')
    path.chmod(0o400)
    _filesystem.chmod(path, 0o700 if directory else 0o600)
    if directory:
        path.rmdir()
    else:
        path.write_bytes(b'updated')
        path.unlink()
    assert not path.exists()
