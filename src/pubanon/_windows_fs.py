"""Provide Windows read-only attributes and durable file replacement."""

import ctypes
import os
import stat
from functools import cache
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from collections.abc import Callable


class _Function(Protocol):
    """Describe a dynamically bound ctypes function at the native ABI boundary."""

    argtypes: list[object]
    restype: object

    def __call__(self, *args: object) -> int:
        """Call a Windows ABI function returning an integer or handle."""
        ...


def _error(code: int | None = None) -> OSError:
    """Capture a Win32 error while its thread-local value is still current."""
    if code is None:
        code = cast('Callable[[], int]', getattr(ctypes, 'get_last_error', None))()
    return cast('Callable[[int], OSError]', getattr(ctypes, 'WinError', None))(code)


@cache
def _library(name: str) -> ctypes.CDLL:
    """Load Windows libraries only when a native operation is requested."""
    if os.name != 'nt':
        raise OSError('native Windows filesystem operations require Windows')
    factory = cast('Callable[..., ctypes.CDLL]', getattr(ctypes, 'WinDLL', None))
    return factory(name, use_last_error=True)


@cache
def _function(library: str, name: str, result: object, arguments: tuple[object, ...]) -> _Function:
    """Bind a documented Windows function with explicit pointer-width-safe types."""
    function = cast('_Function', getattr(_library(library), name))
    function.restype = result
    function.argtypes = list(arguments)
    return function


def check_directory(path: Path) -> None:
    """Require a directory entry without traversing a leaf link during cleanup."""
    if not stat.S_ISDIR(path.lstat().st_mode) or path.is_junction():
        raise OSError('not a plain directory')


def chmod(path: Path, mode: int, *, follow_symlinks: bool = True) -> None:
    """Set the ordinary read-only attribute without changing access control lists.

    Args:
        path: File or directory to update.
        mode: Owner-write selects writable; otherwise select read-only.
        follow_symlinks: Whether to update a link target instead of the link.
    """
    path.chmod(mode, follow_symlinks=follow_symlinks)


def fchmod(fd: int, mode: int) -> None:
    """Set an open file's read-only attribute without changing its access list."""
    os.chmod(fd, mode)


def read_entry(path: Path) -> tuple[int, bytes]:
    """Read an accepted entry as its file-type bits and exact byte payload.

    Regular files return `(stat.S_IFREG, content)`. Symbolic links return
    `(stat.S_IFLNK, os.fsencode(target))`, preserving the target spelling
    without following the leaf link.

    Raises:
        OSError: The entry is neither a regular file nor a symbolic link, or
            its payload cannot be read.
    """
    mode = path.lstat().st_mode
    if stat.S_ISLNK(mode):
        # Keep the exact target spelling; Path.readlink normalizes dot components.
        return stat.S_IFLNK, os.fsencode(os.readlink(path, dir_fd=None))
    if not stat.S_ISREG(mode):
        raise OSError('resolution requires a regular file or symlink')
    return stat.S_IFREG, path.read_bytes()


def replace(source: Path, target: Path) -> None:
    """Commit a flushed temporary file using a same-volume write-through replacement."""
    move = _function(
        'kernel32',
        'MoveFileExW',
        ctypes.c_int,
        (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32),
    )
    if not move(str(source), str(target), 0x9):
        raise _error()
