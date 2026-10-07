"""Provide platform-specific file attributes and durable state transitions."""

import os
import shutil
import stat
import tempfile
from contextlib import suppress
from pathlib import Path

from pubanon import _windows_fs


class DurableWriteError(Exception):
    """Report the phase and interruption state of a failed private write.

    Attributes:
        replaced: Whether replacement returned successfully.
        replacement_attempted: Whether replacement started; an interrupt can
            leave its outcome uncertain even when `replaced` is false.
        interrupted: Whether a keyboard interrupt caused the failure.
    """

    def __init__(self, *, replaced: bool, replacement_attempted: bool, interrupted: bool) -> None:
        """Record the failure phase for caller-specific recovery messages."""
        super().__init__('durable write failed')
        self.replaced = replaced
        self.replacement_attempted = replacement_attempted
        self.interrupted = interrupted


def chmod(path: Path, mode: int, *, follow_symlinks: bool = True) -> None:
    """Set read-only attributes on Windows and mode bits on POSIX.

    Args:
        path: File or directory to restrict.
        mode: POSIX mode; Windows uses only owner-write to select its read-only
            attribute.
        follow_symlinks: Whether to modify the leaf target rather than the link
            itself.
    """
    path.chmod(mode, follow_symlinks=follow_symlinks)


def _restore_tree_access(root: Path) -> None:
    """Restore removable entries without following links or junctions."""
    directories = [root]
    while directories:
        directory = directories.pop()
        if os.name == 'nt':
            _windows_fs.check_directory(directory)
        mode = directory.lstat().st_mode
        if not stat.S_ISDIR(mode):
            continue
        chmod(directory, stat.S_IMODE(mode) | stat.S_IRWXU, follow_symlinks=False)
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                if path.is_symlink() or path.is_junction():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    directories.append(path)
                elif os.name == 'nt':
                    chmod(path, 0o600, follow_symlinks=False)


def fchmod(fd: int, mode: int) -> None:
    """Set permissions or read-only attributes on an open descriptor.

    Args:
        fd: Borrowed descriptor that remains open.
        mode: POSIX mode; Windows uses only owner-write to select its read-only
            attribute.
    """
    os.fchmod(fd, mode)


def remove_tree(path: Path) -> bool:
    """Remove a directory tree safely, restoring owner write access if needed.

    Returns:
        Whether the directory existed and was removed.
    """
    try:
        if path.is_symlink() or path.is_junction():
            raise OSError('cannot remove directory through a link or junction')
        if os.name == 'nt':
            _restore_tree_access(path)
        try:
            shutil.rmtree(path)
        except PermissionError:
            _restore_tree_access(path)
            shutil.rmtree(path)
    except FileNotFoundError:
        return False
    return True


def replace(source: Path, target: Path) -> None:
    """Replace a file, committing the same-volume rename with write-through on Windows.

    Callers must flush and fsync the source first, then call `sync_directory`
    after replacement to commit the parent directory on POSIX.
    """
    if os.name == 'nt':
        _windows_fs.replace(source, target)
    else:
        source.replace(target)


def sync_directory(path: Path) -> None:
    """Complete a prior replacement's commit using the platform's durability primitive.

    On POSIX this fsyncs the directory. Windows callers must have already used
    `replace`, which commits the rename with write-through; this call only
    validates the directory. It cannot commit arbitrary Windows creates or
    deletes. Use `durable_unlink` for an authoritative file removal.
    """
    if os.name == 'nt':
        _windows_fs.check_directory(path)
        return
    descriptor: int | None = None
    try:
        descriptor = os.open(path, os.O_RDONLY)
        os.fsync(descriptor)
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)


def durable_unlink(path: Path) -> None:
    """Durably remove an authoritative file name before cleaning up its private bytes.

    Windows first commits a same-directory move to a unique temporary name.
    A crash or cleanup failure can leave a private `.pubanon-delete-*.tmp`
    residue, which is never an authoritative record. Successful return also
    removes that temporary name; no unrelated temporary files are scanned.
    """
    if os.name != 'nt':
        path.unlink()
        sync_directory(path.parent)
        return
    descriptor, name = tempfile.mkstemp(prefix='.pubanon-delete-', suffix='.tmp', dir=path.parent)
    temporary = Path(name)
    try:
        os.close(descriptor)
        _windows_fs.replace(path, temporary)
    finally:
        temporary.unlink(missing_ok=True)


def write_private(path: Path, payload: bytes) -> None:
    """Durably replace a file with owner-only content.

    Write a unique temporary in the existing parent directory with mode 0600,
    fsync it, replace the target, and sync the directory. Remove the owned
    temporary on failure without disturbing unrelated files.

    Raises:
        DurableWriteError: A write step failed; the original error is chained.
            An interrupted replacement attempt can have an unknown outcome.
    """
    descriptor: int | None = None
    temporary: Path | None = None
    replaced = False
    replacement_attempted = False
    try:
        descriptor, name = tempfile.mkstemp(
            prefix='.pubanon-write-', suffix='.tmp', dir=path.parent
        )
        temporary = Path(name)
        fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, 'wb')
        descriptor = None
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        except (OSError, KeyboardInterrupt):
            with suppress(OSError):
                handle.close()
            raise
        handle.close()
        replacement_attempted = True
        replace(temporary, path)
        replaced = True
        sync_directory(path.parent)
    except (OSError, KeyboardInterrupt) as exc:
        raise DurableWriteError(
            replaced=replaced,
            replacement_attempted=replacement_attempted,
            interrupted=isinstance(exc, KeyboardInterrupt),
        ) from exc
    finally:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)
