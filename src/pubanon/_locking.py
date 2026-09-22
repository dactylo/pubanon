"""Hold process-scoped exclusive locks on local state files."""

import errno
import os
import sys

if sys.platform == 'win32':
    import msvcrt
else:
    import fcntl


def acquire(descriptor: int) -> None:
    """Acquire a nonblocking exclusive lock until release or descriptor close.

    Raises:
        BlockingIOError: Another process holds the lock.
    """
    if sys.platform == 'win32':
        os.lseek(descriptor, 0, os.SEEK_SET)
        try:
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise BlockingIOError(exc.errno, exc.strerror) from exc
            raise
    else:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def release(descriptor: int) -> None:
    """Release an exclusive lock without closing its descriptor."""
    if sys.platform == 'win32':
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
