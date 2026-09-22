import os
import shutil
import stat
from collections.abc import Callable, Mapping
from pathlib import Path

_CONFIG_OVERRIDES = (
    ('core.hooksPath', os.devnull),
    ('commit.gpgsign', 'false'),
    ('tag.gpgsign', 'false'),
    ('user.name', 'Test User'),
    ('user.email', 'test@example.invalid'),
)
_ISOLATED_GIT_ENV = {
    'GIT_CONFIG_GLOBAL': os.devnull,
    'GIT_CONFIG_NOSYSTEM': '1',
    'GIT_CONFIG_SYSTEM': os.devnull,
    'GIT_TEMPLATE_DIR': '',
    'GIT_TERMINAL_PROMPT': '0',
}


def git_env(overrides: Mapping[str, str] | None = None) -> dict[str, str]:
    environment = {key: value for key, value in os.environ.items() if not key.startswith('GIT_')}
    environment.update(_ISOLATED_GIT_ENV)
    environment['GIT_CONFIG_COUNT'] = str(len(_CONFIG_OVERRIDES))
    for index, (key, value) in enumerate(_CONFIG_OVERRIDES):
        environment[f'GIT_CONFIG_KEY_{index}'] = key
        environment[f'GIT_CONFIG_VALUE_{index}'] = value
    if overrides is not None:
        environment.update(overrides)
    return environment


def remove_tree(path: Path) -> None:
    def _retry(function: Callable[[str], object], filename: str, error: BaseException) -> None:
        if not isinstance(error, PermissionError):
            raise error
        target = Path(filename)
        target.chmod(target.stat().st_mode | stat.S_IWUSR)
        function(filename)

    shutil.rmtree(path, onexc=_retry)
