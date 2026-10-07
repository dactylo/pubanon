import os
import shutil
import stat
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from pubanon._snapshots._source import Snapshot

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


def oracle_tree(store: Path, snapshot: Snapshot) -> str:
    contents: dict[str, list[tuple[bytes, bytes]]] = {'': []}
    for entry in sorted(snapshot, key=lambda item: item.path.encode('utf-8')):
        parent = Path(entry.path).parent
        while str(parent) != '.':
            contents.setdefault(parent.as_posix(), [])
            parent = parent.parent
        oid = subprocess.run(
            ['git', '-C', store, 'hash-object', '-w', '--stdin'],
            input=entry.data,
            check=True,
            capture_output=True,
            env=git_env(),
        ).stdout.strip()
        directory, _, name = entry.path.rpartition('/')
        raw_name = name.encode()
        contents[directory].append(
            (raw_name, f'{entry.mode:o} blob '.encode() + oid + b'\t' + raw_name + b'\0')
        )

    root_oid: bytes | None = None
    for directory in sorted(
        contents,
        key=lambda item: item.count('/') + bool(item),
        reverse=True,
    ):
        tree_oid = subprocess.run(
            ['git', '-C', store, 'mktree', '-z'],
            input=b''.join(record for _name, record in sorted(contents[directory])),
            check=True,
            capture_output=True,
            env=git_env(),
        ).stdout.strip()
        if directory:
            parent, _, name = directory.rpartition('/')
            raw_name = name.encode()
            contents[parent].append(
                (raw_name, b'040000 tree ' + tree_oid + b'\t' + raw_name + b'\0')
            )
        else:
            root_oid = tree_oid
    assert root_oid is not None
    return root_oid.decode('ascii')


def remove_tree(path: Path) -> None:
    def _retry(function: Callable[[str], object], filename: str, error: BaseException) -> None:
        if not isinstance(error, PermissionError):
            raise error
        target = Path(filename)
        target.chmod(target.stat().st_mode | stat.S_IWUSR)
        function(filename)

    shutil.rmtree(path, onexc=_retry)
