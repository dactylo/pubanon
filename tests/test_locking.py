import os
import subprocess
import sys
from pathlib import Path

import pytest

from pubanon import _locking


def test_lock_excludes_another_process_and_releases_on_close(tmp_path: Path) -> None:
    path = tmp_path / 'operation.lock'
    script = (
        'import sys\n'
        'from pubanon import _locking\n'
        'with open(sys.argv[1], "a+b") as stream:\n'
        '    try:\n'
        '        _locking.acquire(stream.fileno())\n'
        '    except BlockingIOError:\n'
        '        sys.exit(7)\n'
    )
    env = dict(os.environ, PYTHONPATH=str(Path(_locking.__file__).parents[1]))
    with path.open('a+b') as first:
        _locking.acquire(first.fileno())
        busy = subprocess.run([sys.executable, '-c', script, str(path)], check=False, env=env)
        assert busy.returncode == 7
    available = subprocess.run([sys.executable, '-c', script, str(path)], check=False, env=env)
    assert available.returncode == 0


def test_lock_release_allows_a_second_open_handle(tmp_path: Path) -> None:
    path = tmp_path / 'operation.lock'
    with path.open('a+b') as first, path.open('a+b') as second:
        _locking.acquire(first.fileno())
        with pytest.raises(BlockingIOError):
            _locking.acquire(second.fileno())
        _locking.release(first.fileno())
        _locking.acquire(second.fileno())
        _locking.release(second.fileno())
