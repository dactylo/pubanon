import os
import subprocess
import sys
from pathlib import Path

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
        busy = subprocess.run(
            [sys.executable, '-c', script, str(path)], check=False, env=env, timeout=5
        )
        assert busy.returncode == 7
    available = subprocess.run(
        [sys.executable, '-c', script, str(path)], check=False, env=env, timeout=5
    )
    assert available.returncode == 0


def test_lock_release_allows_a_second_open_handle(tmp_path: Path) -> None:
    path = tmp_path / 'operation.lock'
    script = (
        'import sys\n'
        'from pubanon import _locking\n'
        'with open(sys.argv[1], "a+b") as first, open(sys.argv[1], "a+b") as second:\n'
        '    _locking.acquire(first.fileno())\n'
        '    try:\n'
        '        _locking.acquire(second.fileno())\n'
        '    except BlockingIOError:\n'
        '        pass\n'
        '    else:\n'
        '        raise AssertionError("conflicting handle acquired the lock")\n'
        '    _locking.release(first.fileno())\n'
        '    _locking.acquire(second.fileno())\n'
        '    _locking.release(second.fileno())\n'
        'print("released")\n'
    )
    result = subprocess.run(
        [sys.executable, '-c', script, str(path)],
        check=True,
        capture_output=True,
        env=dict(os.environ, PYTHONPATH=str(Path(_locking.__file__).parents[1])),
        timeout=5,
    )
    assert result.stdout.splitlines() == [b'released']
