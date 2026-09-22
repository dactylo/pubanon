import os
import stat
from pathlib import Path


def assert_private_mode(path: Path, mode: int) -> None:
    actual = path.stat().st_mode
    if os.name == 'nt':
        assert bool(actual & stat.S_IWUSR) == bool(mode & stat.S_IWUSR)
    else:
        assert stat.S_IMODE(actual) == mode
