"""Materialize and verify read-only review copies."""

import ntpath
import os
import posixpath
import re
import stat
from pathlib import Path, PurePosixPath

from pubanon import _filesystem, _windows_fs
from pubanon._config import (
    Paths,
    create_data_dirs,
)
from pubanon._errors import PubanonError
from pubanon._run import OID_HEX
from pubanon._snapshots import _records
from pubanon._snapshots._source import (
    Entry,
    Snapshot,
    path_portability_keys,
    validate_symlink_graph,
)
from pubanon._terminal import (
    escape,
)

_ERROR_PRIVILEGE_NOT_HELD = 1314
_OID_PATTERN = re.compile(OID_HEX)


def _clean_inspection_path(inspect: Path) -> bool:
    """Remove one inspection directory without following its root symlink."""
    try:
        return _filesystem.remove_tree(inspect)
    # remove_tree can raise NotImplementedError for no-follow chmod.
    except (OSError, RuntimeError) as exc:
        raise PubanonError('cannot clean review copy') from exc


def _inspection_mismatch() -> PubanonError:
    """Tell the operator how to replace a changed review directory explicitly."""
    return PubanonError(
        'review copy does not match the saved snapshot',
        recovery='pubanon inspect --refresh',
    )


def _lock_inspection_copy(inspect: Path, snapshot: Snapshot) -> None:
    """Remove owner write access from verified inspection entries."""
    directories = {inspect}
    for entry in snapshot:
        target = inspect / entry.path
        if entry.kind == 'blob':
            _filesystem.chmod(target, 0o500 if entry.mode & 0o111 else 0o400)
        parent = target.parent
        while parent != inspect:
            directories.add(parent)
            parent = parent.parent
    for directory in sorted(directories, key=lambda path: len(path.parts), reverse=True):
        _filesystem.chmod(directory, 0o500)


def _materialize_inspection_files(inspect: Path, snapshot: Snapshot) -> None:
    """Write verified candidate entries exclusively into a new inspection copy."""
    created_directories = {inspect}
    snapshot_directories = {
        str(parent) for entry in snapshot for parent in PurePosixPath(entry.path).parents
    }
    for entry in snapshot:
        if '.git' in entry.path.split('/'):
            raise PubanonError('snapshot cannot materialize a .git path')
        parent = inspect
        components = entry.path.split('/')
        for component in components[:-1]:
            parent /= component
            if parent in created_directories:
                continue
            parent.mkdir(mode=0o700)
            _filesystem.chmod(parent, 0o700)
            created_directories.add(parent)
        target = parent / components[-1]
        if entry.kind == 'symlink':
            link = os.fsdecode(entry.data)
            destination_path = posixpath.normpath(
                posixpath.join(posixpath.dirname(entry.path), link)
            )
            try:
                target.symlink_to(
                    link, target_is_directory=destination_path in snapshot_directories
                )
            except OSError as exc:
                if getattr(exc, 'winerror', None) == _ERROR_PRIVILEGE_NOT_HELD:
                    raise PubanonError(
                        'Windows cannot create snapshot symlinks',
                        recovery='enable Windows Developer Mode or run with symlink privileges',
                    ) from exc
                raise
        else:
            with target.open('xb') as destination:
                destination.write(entry.data)
            _filesystem.chmod(target, entry.mode & 0o777)


def _path_collision(first: str, second: str) -> PubanonError:
    """Build a terminal-safe portability collision error."""
    first_display = escape(first)
    second_display = escape(second)
    return PubanonError(
        'unsupported snapshot path collision',
        fact=f'paths: {first_display}; {second_display}',
    )


def _validate_windows_materialization(snapshot: Snapshot) -> None:
    """Refuse names whose Windows interpretation differs from the saved POSIX path."""
    if os.name != 'nt':
        return
    for entry in snapshot:
        names = [entry.path]
        if entry.kind == 'symlink':
            try:
                names.append(os.fsdecode(entry.data))
            except UnicodeError as exc:
                raise PubanonError(
                    'Windows cannot represent snapshot symlink targets',
                    recovery='inspect this snapshot on a POSIX filesystem',
                ) from exc
        for name in names:
            if (
                '\\' in name
                or ':' in name
                or any(
                    ntpath.isreserved(component)
                    for component in name.split('/')
                    if component not in {'', '.', '..'}
                )
            ):
                raise PubanonError(
                    'Windows cannot represent snapshot paths or symlink targets',
                    recovery='inspect this snapshot on a POSIX filesystem',
                )


def _verify_inspection_entry(child: os.DirEntry[str], saved: Entry) -> None:
    """Check one saved file or link without following its directory entry."""
    if os.name == 'nt':
        kind, data = _windows_fs.read_entry(Path(child.path))
        expected_kind = stat.S_IFLNK if saved.kind == 'symlink' else stat.S_IFREG
        if kind != expected_kind or data != saved.data:
            raise _inspection_mismatch()
        return
    mode = child.stat(follow_symlinks=False).st_mode
    if saved.kind == 'symlink':
        if (
            not stat.S_ISLNK(mode)
            or os.fsencode(os.readlink(child.path, dir_fd=None)) != saved.data
        ):
            raise _inspection_mismatch()
    else:
        executable = bool(saved.mode & stat.S_IXUSR)
        if (
            not stat.S_ISREG(mode)
            or bool(mode & stat.S_IXUSR) != executable
            or (not executable and bool(mode & 0o011))
            or Path(child.path).read_bytes() != saved.data
        ):
            raise _inspection_mismatch()


def _verify_inspection_copy(inspect: Path, snapshot: Snapshot) -> None:
    """Require an existing inspection tree to match every saved entry exactly."""
    expected: dict[str, dict[str, Entry | None]] = {'': {}}
    for entry in snapshot:
        parts = entry.path.split('/')
        parent = ''
        for component in parts[:-1]:
            expected[parent][component] = None
            parent = f'{parent}/{component}' if parent else component
            expected.setdefault(parent, {})
        expected[parent][parts[-1]] = entry

    def _verify(directory: Path, relative: str) -> None:
        if os.name == 'nt':
            _windows_fs.check_directory(directory)
        with os.scandir(directory) as entries:
            actual = {entry.name: entry for entry in entries}
        if actual.keys() != expected[relative].keys():
            raise _inspection_mismatch()
        for name, saved in expected[relative].items():
            child = actual[name]
            mode = child.stat(follow_symlinks=False).st_mode
            if saved is None:
                if not stat.S_ISDIR(mode):
                    raise _inspection_mismatch()
                child_relative = f'{relative}/{name}' if relative else name
                _verify(Path(child.path), child_relative)
            else:
                _verify_inspection_entry(child, saved)

    try:
        _verify(inspect, '')
    # Recursive _verify can raise RecursionError for deeply nested trees.
    except (OSError, RuntimeError) as exc:
        raise PubanonError('cannot verify review directory') from exc


def inspection_path(paths: Paths, project: str, commit: str) -> Path:
    """Return a validated inspection location without creating directories.

    Raises:
        PubanonError: The project name or snapshot commit is invalid.
    """
    project_dir = _records.project_dir(paths, project)
    if _OID_PATTERN.fullmatch(commit) is None:
        raise PubanonError('invalid review copy commit')
    return project_dir / 'inspections' / commit


def inspection_paths(paths: Paths, project: str) -> tuple[Path, ...]:
    """List existing candidate copies for explicit cleanup.

    Recognized names are full lowercase object IDs and results are sorted. An
    absent parent returns an empty tuple. Matching entries are listed even when
    their type will later be refused by the cleaner. Unrecognized entries are
    left alone, and symlink roots are never traversed.
    """
    root = _records.project_dir(paths, project) / 'inspections'
    copies: list[Path] = []
    try:
        if root.is_symlink():
            raise PubanonError('cannot read snapshot directories through a symlink')
        if root.exists():
            if os.name == 'nt':
                _windows_fs.check_directory(root)
            copies.extend(path for path in root.iterdir() if _OID_PATTERN.fullmatch(path.name))
    except OSError as exc:
        raise PubanonError('cannot list snapshot directories') from exc
    return tuple(sorted(copies))


def clean_inspect(paths: Paths, project: str, *, commit: str | None = None) -> bool:
    """Remove known project inspection copies, optionally selecting one commit.

    The caller must hold the project lock for the complete operation.

    Returns:
        Whether an inspection copy was removed.

    Raises:
        PubanonError: Copies cannot be listed or safely removed.
    """
    removed = False
    for inspection in inspection_paths(paths, project):
        if commit is not None and inspection.name != commit:
            continue
        removed = _clean_inspection_path(inspection) or removed
    return removed


def validate_tree_paths(snapshot: Snapshot) -> None:
    """Validate all candidate paths before object creation.

    Args:
        snapshot: Entries whose paths, types, and modes must be portable.

    Raises:
        PubanonError: An entry is unsupported or its path collides with
            another.
    """
    entry_paths: dict[str, str] = {}
    directory_paths: dict[str, str] = {}
    valid_modes = {'blob': {0o100644, 0o100755}, 'symlink': {0o120000}}
    for entry in snapshot:
        parts = entry.path.split('/')
        key_parts = path_portability_keys(entry.path)
        key = '/'.join(key_parts)
        if (
            not entry.path
            or entry.path.startswith('/')
            or any(part in {'', '.', '..'} for part in parts)
            or '.git' in key_parts
            or entry.mode not in valid_modes.get(entry.kind, set())
        ):
            raise PubanonError('unsupported snapshot entry')
        previous_entry = entry_paths.get(key)
        if previous_entry is not None:
            if previous_entry == entry.path:
                raise PubanonError('unsupported snapshot entry')
            raise _path_collision(previous_entry, entry.path)
        previous_directory = directory_paths.get(key)
        if previous_directory is not None:
            raise _path_collision(previous_directory, entry.path)
        for depth in range(1, len(key_parts)):
            ancestor = '/'.join(key_parts[:depth])
            directory = '/'.join(parts[:depth])
            previous_entry = entry_paths.get(ancestor)
            if previous_entry is not None:
                raise _path_collision(previous_entry, directory)
            previous_directory = directory_paths.get(ancestor)
            if previous_directory is not None and previous_directory != directory:
                raise _path_collision(previous_directory, directory)
            directory_paths.setdefault(ancestor, directory)
        entry_paths[key] = entry.path


def _validate_materialization(commit: str, snapshot: Snapshot) -> None:
    """Reject unsafe commit names and snapshot paths before touching a copy."""
    if _OID_PATTERN.fullmatch(commit) is None:
        raise PubanonError('invalid review copy commit')
    validate_tree_paths(snapshot)
    _validate_windows_materialization(snapshot)


def materialize_directory(directory: Path, snapshot: Snapshot) -> None:
    """Create an exclusive directory containing exact snapshot entries.

    POSIX mode bits restrict the directory to owner-only access.

    Validate paths, modes, collisions, and symlink targets before creation.
    The parent directory must exist. Existing files or directories are refused;
    a newly created partial directory is removed on failure or interruption.
    """
    validate_tree_paths(snapshot)
    validate_symlink_graph(snapshot)
    _validate_windows_materialization(snapshot)
    try:
        if os.name == 'nt':
            _windows_fs.check_directory(directory.parent)
        directory.mkdir(mode=0o700)
    except OSError as exc:
        raise PubanonError('cannot create review copy') from exc
    try:
        _filesystem.chmod(directory, 0o700)
        _materialize_inspection_files(directory, snapshot)
    except KeyboardInterrupt:
        _clean_inspection_path(directory)
        raise
    except FileExistsError as exc:
        _clean_inspection_path(directory)
        raise PubanonError('snapshot paths collide in the review copy') from exc
    except (OSError, PubanonError) as exc:
        _clean_inspection_path(directory)
        if isinstance(exc, PubanonError):
            raise
        raise PubanonError('cannot materialize review copy') from exc


def materialize_snapshot(
    paths: Paths, project: str, commit: str, snapshot: Snapshot, *, refresh: bool = False
) -> Path:
    """Create or reuse an exact, read-only commit-keyed copy.

    POSIX mode bits restrict the copy to owner-only access.

    `commit` must be a full accepted object ID. The caller supplies the
    corresponding snapshot; this function validates but does not derive that
    association from Git. Invalid snapshots, unsafe paths, and changed copies
    are refused.

    Ordinary reuse verifies all saved entries before enforcing read-only
    permissions. Refresh explicitly replaces only the selected copy.

    The caller must hold the project lock for the complete operation.
    """
    _validate_materialization(commit, snapshot)
    create_data_dirs(paths, project)
    inspect = inspection_path(paths, project, commit)
    root = inspect.parent
    if root.is_symlink() or inspect.is_symlink():
        raise PubanonError('cannot materialize review copy through a symlink')
    try:
        root.mkdir(mode=0o700, exist_ok=True)
        if os.name == 'nt':
            _windows_fs.check_directory(root)
        _filesystem.chmod(root, 0o700, follow_symlinks=False)
        if inspect.exists():
            if os.name == 'nt':
                _windows_fs.check_directory(inspect)
            if not inspect.is_dir():
                raise PubanonError('snapshot path is not a directory')
            if not refresh:
                _verify_inspection_copy(inspect, snapshot)
                try:
                    _lock_inspection_copy(inspect, snapshot)
                except OSError as exc:
                    raise PubanonError('cannot lock review directory') from exc
                return inspect
            _clean_inspection_path(inspect)
    # _filesystem.chmod with no-follow can raise NotImplementedError.
    except (OSError, RuntimeError) as exc:
        raise PubanonError('cannot create review copy') from exc
    materialize_directory(inspect, snapshot)
    try:
        _lock_inspection_copy(inspect, snapshot)
    except OSError as exc:
        _clean_inspection_path(inspect)
        raise PubanonError('cannot lock review directory') from exc
    return inspect
