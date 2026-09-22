"""Export grouped merge conflicts and accept only declared file decisions."""

import ntpath
import os
import re
import shutil
import stat
import sys
import tempfile
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal, cast

from pubanon import _filesystem, _windows_fs
from pubanon._errors import PubanonError
from pubanon._run import OID_HEX, GitResult, run_git
from pubanon._snapshots._candidate import materialize_directory
from pubanon._snapshots._records import ConflictEntry, ConflictStage, Resolution, RewritePause
from pubanon._snapshots._source import (
    Entry,
    Snapshot,
    decode_tree_path,
    path_portability_keys,
    read_snapshot,
    tree_mode,
    validate_symlink_graph,
)
from pubanon._terminal import escape, output
from pubanon._toml import toml_array, toml_string

_LABELS = {1: 'base', 2: 'ours', 3: 'theirs'}
_MIN_MERGE_FIELDS = 2
_MODE_EXECUTABLE = 0o100755
_MODE_FILE = 0o100644
_MODE_LINK = 0o120000
_STAGE_FIELDS = 3


@dataclass(frozen=True)
class MergeResult:
    """Retain the merged tree and structured diagnostics with authoritative exit status.

    Parsed entries have no frozen decisions until `expand_conflicts` examines
    the saved Git objects. A conflicted result can have no representable
    groups.
    """

    tree: str
    conflicted: bool
    conflicts: tuple[ConflictEntry, ...]


def _oid(raw: bytes) -> str:
    """Require a complete object identifier without echoing untrusted output."""
    if re.fullmatch(OID_HEX.encode(), raw) is None:
        raise PubanonError('Git returned a malformed merge object ID')
    return raw.decode('ascii')


def _open_directory(path: Path, *, trusted_parent: Path) -> int:
    """Open owned components without following symlinks below a trusted parent."""
    if sys.platform == 'win32':
        raise OSError('directory descriptors require POSIX')
    relative = path.absolute().relative_to(trusted_parent.absolute())
    if '..' in relative.parts:
        raise ValueError('directory is outside its trusted parent')
    descriptor = os.open(trusted_parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in relative.parts:
            following = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor
            )
            os.close(descriptor)
            descriptor = following
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _check_directory(path: Path, *, trusted_parent: Path) -> None:
    """Validate plain directory components with the host's no-follow mechanism."""
    if sys.platform == 'win32':
        _windows_fs.check_directory(path)
    else:
        os.close(_open_directory(path, trusted_parent=trusted_parent))


def _path(raw: bytes) -> str:
    """Apply the source path grammar and refuse repository metadata components."""
    path = decode_tree_path(raw)
    if '\0' in path or '.git' in path_portability_keys(path):
        raise PubanonError('invalid conflict path')
    if sys.platform == 'win32' and (
        '\\' in path or ':' in path or any(ntpath.isreserved(part) for part in path.split('/'))
    ):
        raise PubanonError('conflict path cannot be represented safely on Windows')
    return path


def _decision(value: object, expected_path: str) -> Resolution:
    """Accept an explicit keep, delete, or rename declaration for one frozen path."""
    if not isinstance(value, dict):
        raise PubanonError('manifest conflict decision paths changed')
    value = cast('dict[str, object]', value)
    if value.get('path') != expected_path:
        raise PubanonError('manifest conflict decision paths changed')
    if set(value) == {'path', 'rename'} and isinstance(value['rename'], str):
        return Resolution((expected_path,), 'rename', _path(value['rename'].encode()))
    if (
        set(value) != {'path', 'action'}
        or not isinstance(value['action'], str)
        or value['action'] not in {'keep', 'delete'}
    ):
        raise PubanonError('manifest contains an unresolved or unknown action')
    action: Literal['keep', 'delete'] = 'keep' if value['action'] == 'keep' else 'delete'
    return Resolution((expected_path,), action)


def _group_decisions(
    group: object, original: dict[str, object], conflict: ConflictEntry
) -> tuple[Resolution, ...]:
    """Validate one diagnostic group and decode its ordered frozen declarations."""
    if not isinstance(group, dict):
        raise PubanonError('manifest conflict metadata changed')
    group = cast('dict[str, object]', group)
    if set(group) != set(original):
        raise PubanonError('manifest conflict metadata changed')
    if any(group[key] != original[key] for key in original if key != 'decisions'):
        raise PubanonError('manifest conflict metadata changed')
    # Equality alone accepts a floating-point mode equal to the saved integer.
    versions = cast('list[dict[str, object]]', group['versions'])
    if any(type(version['mode']) is not int for version in versions):
        raise PubanonError('manifest conflict metadata types changed')
    values = group['decisions']
    if not isinstance(values, list):
        raise PubanonError('manifest conflict decision paths changed')
    values = cast('list[object]', values)
    if len(values) != len(conflict.decision_paths):
        raise PubanonError('manifest conflict decision paths changed')
    return tuple(
        _decision(value, path) for path, value in zip(conflict.decision_paths, values, strict=True)
    )


def _messages(fields: list[bytes], stages: tuple[ConflictStage, ...]) -> tuple[ConflictEntry, ...]:
    """Consume counted message paths without interpreting Git diagnostic prose."""
    entries: list[ConflictEntry] = []
    index = 0
    while index < len(fields):
        raw_count = fields[index]
        if not raw_count.isdigit() or len(raw_count) > len(str(len(fields))):
            raise PubanonError('Git returned a malformed merge message count')
        count = int(raw_count)
        end = index + count + _STAGE_FIELDS
        if end > len(fields):
            raise PubanonError('Git returned a truncated merge message')
        paths = tuple(_path(raw) for raw in fields[index + 1 : index + count + 1])
        kind = fields[end - 2].decode('utf-8', errors='replace')
        message = fields[end - 1].decode('utf-8', errors='replace')
        if kind.startswith('CONFLICT'):
            attached = tuple(stage for stage in stages if stage.path in paths)
            entries.append(ConflictEntry(paths, kind, message, attached))
        index = end
    named = {path for entry in entries for path in entry.paths}
    entries.extend(
        ConflictEntry(
            (path,),
            'contents',
            'Unmerged file',
            tuple(stage for stage in stages if stage.path == path),
        )
        for path in sorted({stage.path for stage in stages} - named, key=os.fsencode)
    )
    return tuple(entries)


def _read_entry(
    root: Path, path: str, frozen_mode: int = _MODE_FILE, *, trusted_parent: Path
) -> Entry:
    """Read a declared file without following parent or leaf symlinks."""
    _path(path.encode('utf-8'))
    if sys.platform == 'win32':
        parent = root
        _windows_fs.check_directory(parent)
        for part in Path(path).parts[:-1]:
            parent /= part
            _windows_fs.check_directory(parent)
        mode, data = _windows_fs.read_entry(root / path)
        if stat.S_ISLNK(mode):
            target = os.fsdecode(data)
            if (
                '\\' in target
                or ':' in target
                or any(
                    ntpath.isreserved(part)
                    for part in target.split('/')
                    if part not in {'', '.', '..'}
                )
            ):
                raise PubanonError(
                    'resolution symlink target is not safely representable on Windows'
                )
            return Entry(path, _MODE_LINK, 'symlink', data)
        if not stat.S_ISREG(mode):
            raise PubanonError('resolution requires a regular file or symlink')
        git_mode = _MODE_EXECUTABLE if frozen_mode == _MODE_EXECUTABLE else _MODE_FILE
        return Entry(path, git_mode, 'blob', data)
    parent = _open_directory(root / Path(path).parent, trusted_parent=trusted_parent)
    try:
        name = Path(path).name
        mode = os.stat(name, dir_fd=parent, follow_symlinks=False).st_mode
        if stat.S_ISLNK(mode):
            data = os.fsencode(os.readlink(name, dir_fd=parent))
            return Entry(path, _MODE_LINK, 'symlink', data)
        if not stat.S_ISREG(mode):
            raise PubanonError('resolution requires a regular file or symlink')
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        with os.fdopen(descriptor, 'rb') as stream:
            actual = os.fstat(stream.fileno()).st_mode
            if not stat.S_ISREG(actual):
                raise PubanonError('resolution requires a regular file')
            data = stream.read()
        git_mode = _MODE_EXECUTABLE if actual & stat.S_IXUSR else _MODE_FILE
        return Entry(path, git_mode, 'blob', data)
    finally:
        os.close(parent)


def _skip_directory(path: Path) -> bool:
    """Keep discovery out of symlinks and unsafe Windows directory entries."""
    if path.is_symlink():
        return True
    if sys.platform == 'win32':
        try:
            _windows_fs.check_directory(path)
        except OSError:
            return True
    return False


def _report_undeclared(directory: Path, snapshot: Snapshot, declared: set[str]) -> None:
    """Report ignored edits without allowing filesystem discovery to authorize inputs."""
    original = {entry.path: entry for entry in snapshot}
    observed: set[str] = set()
    changed: set[str] = set()
    root = directory / 'tree'
    for parent, directories, files in os.walk(root, followlinks=False):
        links = [name for name in directories if _skip_directory(Path(parent) / name)]
        directories[:] = [name for name in directories if name not in links]
        for name in files + links:
            path = (Path(parent) / name).relative_to(root).as_posix()
            observed.add(path)
            if path in declared:
                continue
            try:
                previous = original.get(path)
                entry = _read_entry(
                    root,
                    path,
                    previous.mode if previous else _MODE_FILE,
                    trusted_parent=directory.parent,
                )
            except (OSError, PubanonError):
                changed.add(path)
            else:
                if original.get(path) != entry:
                    changed.add(path)
    changed.update(set(original) - observed - declared)
    for path in sorted(changed, key=os.fsencode):
        output(f'Ignoring undeclared conflict export edit: {escape(path)}')


def _stage(raw: bytes) -> ConflictStage:
    """Decode one NUL-delimited unmerged stage record."""
    header, separator, raw_path = raw.partition(b'\t')
    fields = header.split(b' ')
    if not separator or len(fields) != _STAGE_FIELDS or fields[2] not in {b'1', b'2', b'3'}:
        raise PubanonError('Git returned a malformed merge stage')
    path = _path(raw_path)
    stage: Literal[1, 2, 3] = 1 if fields[2] == b'1' else 2 if fields[2] == b'2' else 3
    return ConflictStage(path, tree_mode(fields[0], path), _oid(fields[1]), stage)


def _text_conflicts(store: Path, pause: RewritePause) -> set[str]:
    """Identify regular text paths with both ours and theirs stages for marker courtesy checks."""
    stages = {stage for group in pause.conflicts for stage in group.stages}
    text_stages: dict[str, set[int]] = {}
    for stage in stages:
        if stage.stage in {2, 3} and stage.mode != _MODE_LINK:
            data = run_git(
                ['--git-dir', str(store.absolute()), 'cat-file', 'blob', stage.oid], cwd=store
            ).stdout
            if b'\0' not in data:
                text_stages.setdefault(stage.path, set()).add(stage.stage)
    return {path for path, numbers in text_stages.items() if numbers == {2, 3}}


def _validate_outputs(entries: Snapshot) -> None:
    """Refuse portable path aliases and file/directory collisions before index writes."""
    files: set[str] = set()
    directories: dict[str, str] = {}
    for entry in sorted(entries, key=lambda item: os.fsencode(item.path)):
        parts = path_portability_keys(entry.path)
        key = '/'.join(parts)
        if key in files or key in directories:
            raise PubanonError('resolution destinations collide')
        for depth in range(1, len(parts)):
            ancestor = '/'.join(parts[:depth])
            literal = '/'.join(entry.path.split('/')[:depth])
            if ancestor in files or (ancestor in directories and directories[ancestor] != literal):
                raise PubanonError('resolution destinations collide')
            directories[ancestor] = literal
        files.add(key)
    validate_symlink_graph(entries)


def _resolved_entries(
    store: Path,
    directory: Path,
    pause: RewritePause,
    snapshot: Snapshot,
    decisions: tuple[Resolution, ...],
) -> dict[str, Entry]:
    """Read declared outputs and reject overwrites and unresolved textual markers."""
    selected = {decision.paths[0] for decision in decisions}
    retained = {entry.path: entry for entry in snapshot if entry.path not in selected}
    additions: dict[str, Entry] = {}
    modes = {entry.path: entry.mode for entry in snapshot}
    for number in (2, 3, 1):
        for conflict in pause.conflicts:
            for stage in conflict.stages:
                if stage.stage == number:
                    modes.setdefault(stage.path, stage.mode)
    text_paths = _text_conflicts(store, pause)
    for decision in decisions:
        if decision.action == 'delete':
            continue
        path = decision.new_path or decision.paths[0]
        if path in retained or path in additions:
            raise PubanonError('resolution destination would overwrite a retained entry')
        # Windows has no executable bit; keep the saved source mode across renames.
        entry = _read_entry(
            directory / 'tree',
            path,
            modes.get(decision.paths[0], _MODE_FILE),
            trusted_parent=directory.parent,
        )
        if (
            decision.paths[0] in text_paths
            and entry.kind == 'blob'
            and b'\0' not in entry.data
            and any(
                line.startswith((b'<<<<<<<', b'=======', b'>>>>>>>', b'|||||||'))
                for line in entry.data.splitlines()
            )
        ):
            raise PubanonError('resolution still contains conflict markers')
        additions[path] = entry
    _validate_outputs(tuple((retained | additions).values()))
    return additions


def _version_metadata(stage: ConflictStage, location: str) -> dict[str, object]:
    """Describe one immutable exported stage without making its bytes editable inputs."""
    return {
        'path': stage.path,
        'mode': stage.mode,
        'oid': stage.oid,
        'label': _LABELS[stage.stage],
        'file': location,
    }


def _manifest(pause: RewritePause, versions: dict[ConflictStage, str]) -> str:
    """Serialize grouped diagnostics with one explicit declaration per frozen leaf."""
    lines = [f'tree = {toml_string(pause.tree)}']
    for conflict in pause.conflicts:
        lines.extend(
            (
                '',
                '[[conflicts]]',
                f'paths = {toml_array(conflict.paths)}',
                f'type = {toml_string(conflict.conflict_type)}',
                f'message = {toml_string(conflict.message)}',
                f'decision_paths = {toml_array(conflict.decision_paths)}',
                '',
            )
        )
        if not conflict.stages:
            lines.append('versions = []')
        for stage in conflict.stages:
            lines.append('[[conflicts.versions]]')
            for key, value in _version_metadata(stage, versions[stage]).items():
                rendered = str(value) if isinstance(value, int) else toml_string(str(value))
                lines.append(f'{key} = {rendered}')
        for path in conflict.decision_paths:
            lines.extend(
                ('[[conflicts.decisions]]', f'path = {toml_string(path)}', 'action = "unresolved"')
            )
    return '\n'.join(lines) + '\n'


def _versions(pause: RewritePause) -> dict[ConflictStage, str]:
    """Assign stable numbered slots to raw-path-sorted unique stage paths."""
    stages = {stage for conflict in pause.conflicts for stage in conflict.stages}
    paths = sorted({stage.path for stage in stages}, key=os.fsencode)
    slots = {path: f'{index:04d}' for index, path in enumerate(paths)}
    return {
        stage: f'versions/{slots[stage.path]}/{_LABELS[stage.stage]}'
        for stage in sorted(stages, key=lambda item: (os.fsencode(item.path), item.stage))
    }


def _read_manifest(directory: Path, pause: RewritePause) -> tuple[Resolution, ...]:
    """Compare authoritative metadata to durable state and collect consistent actions."""
    entry = _read_entry(directory, 'resolutions.toml', trusted_parent=directory.parent)
    if entry.kind != 'blob':
        raise PubanonError('resolution manifest must be a regular file')
    data: dict[str, object] = tomllib.loads(entry.data.decode('utf-8'))
    expected = tomllib.loads(_manifest(pause, _versions(pause)))
    groups = data.get('conflicts')
    if (
        set(data) != {'tree', 'conflicts'}
        or data['tree'] != pause.tree
        or not isinstance(groups, list)
    ):
        raise PubanonError('manifest conflict metadata changed')
    groups = cast('list[object]', groups)
    if len(groups) != len(pause.conflicts):
        raise PubanonError('manifest conflict groups changed')
    decisions: dict[str, Resolution] = {}
    for group, original, conflict in zip(
        groups, expected['conflicts'], pause.conflicts, strict=True
    ):
        for decision in _group_decisions(group, original, conflict):
            path = decision.paths[0]
            if path in decisions and decisions[path] != decision:
                raise PubanonError('manifest repeats inconsistent decisions')
            decisions[path] = decision
    return tuple(decisions[path] for path in sorted(decisions, key=os.fsencode))


def _write_resolved_tree(
    store: Path, tree: str, decisions: tuple[Resolution, ...], additions: dict[str, Entry]
) -> str:
    """Apply validated literal paths through an isolated temporary index initialized from T."""
    with tempfile.TemporaryDirectory(prefix='pubanon-resolve-') as temporary:
        env = {'GIT_INDEX_FILE': str(Path(temporary) / 'index')}
        run_git(['--git-dir', str(store.absolute()), 'read-tree', tree], cwd=store, env_extra=env)
        records = b''.join(
            b'0 ' + b'0' * len(tree) + b'\t' + os.fsencode(item.paths[0]) + b'\0'
            for item in decisions
        )
        for path, entry in additions.items():
            oid = run_git(
                [
                    '--git-dir',
                    str(store.absolute()),
                    'hash-object',
                    '-w',
                    '--no-filters',
                    '--stdin',
                ],
                cwd=store,
                input_bytes=entry.data,
            ).stdout.strip()
            records += f'{entry.mode:o} '.encode() + oid + b'\t' + os.fsencode(path) + b'\0'
        run_git(
            ['--git-dir', str(store.absolute()), 'update-index', '-z', '--index-info'],
            cwd=store,
            env_extra=env,
            input_bytes=records,
        )
        return _oid(
            run_git(
                ['--git-dir', str(store.absolute()), 'write-tree'], cwd=store, env_extra=env
            ).stdout.strip()
        )


def expand_conflicts(store: Path, result: MergeResult) -> tuple[ConflictEntry, ...]:
    """Freeze actionable leaves from the merged tree and saved stage paths.

    Diagnostic groups are unchanged. Empty or unrepresentable conflicts are
    refused before callers can save a durable pause.
    """
    if not result.conflicted:
        return ()
    if not result.conflicts:
        raise PubanonError('merge conflict has no representable decisions')
    leaves = {entry.path for entry in read_snapshot(store, result.tree, git_dir=store)}
    stage_paths = {stage.path for conflict in result.conflicts for stage in conflict.stages}
    expanded: list[ConflictEntry] = []
    for conflict in result.conflicts:
        decisions: set[str] = set()
        for path in conflict.paths:
            _path(path.encode())
            below = {
                leaf
                for leaf in leaves | stage_paths
                if leaf == path or leaf.startswith(path + '/')
            }
            decisions.update(below or {path})
        if not decisions:
            raise PubanonError('merge conflict has no representable decisions')
        expanded.append(
            replace(conflict, decision_paths=tuple(sorted(decisions, key=os.fsencode)))
        )
    return tuple(expanded)


def _validate_pause(store: Path, pause: RewritePause) -> Snapshot:
    """Require the frozen decision set to match the immutable saved Git inputs."""
    expanded = expand_conflicts(
        store, MergeResult(pause.tree, conflicted=True, conflicts=pause.conflicts)
    )
    if expanded != pause.conflicts:
        raise PubanonError('saved conflict decision paths do not match Git objects')
    unique: dict[tuple[str, int], ConflictStage] = {}
    for conflict in pause.conflicts:
        for stage in conflict.stages:
            _path(stage.path.encode())
            tree_mode(f'{stage.mode:o}'.encode(), stage.path)
            _oid(stage.oid.encode())
            if stage.stage not in _LABELS:
                raise PubanonError('invalid saved conflict stage')
            key = stage.path, stage.stage
            if key in unique and unique[key] != stage:
                raise PubanonError('inconsistent saved conflict stages')
            unique[key] = stage
    return read_snapshot(store, pause.tree, git_dir=store)


def export_conflicts(store: Path, directory: Path, pause: RewritePause) -> None:
    """Replace the owned rewrite export from saved objects and write its manifest last.

    The caller supplies only its private rewrite directory, never a source or
    store worktree. Re-export discards partial exports from the exporting
    phase.
    A successful return includes a flushed manifest and directory entry.
    The caller must hold the project lock for the complete operation.
    """
    snapshot = _validate_pause(store, pause)
    versions = _versions(pause)
    try:
        content = _manifest(pause, versions).encode('utf-8')
        _check_directory(directory.parent, trusted_parent=directory.parent)
        if directory.is_symlink():
            raise PubanonError('cannot export conflicts through a symlink')
        if directory.exists():
            _check_directory(directory, trusted_parent=directory.parent)
            shutil.rmtree(directory)
        directory.mkdir(mode=0o700)
        _filesystem.chmod(directory, 0o700)
        materialize_directory(directory / 'tree', snapshot)
        (directory / 'versions').mkdir(mode=0o700)
        _filesystem.chmod(directory / 'versions', 0o700)
        for stage, location in versions.items():
            target = directory / location
            target.parent.mkdir(mode=0o700, exist_ok=True)
            _filesystem.chmod(target.parent, 0o700)
            blob = run_git(
                ['--git-dir', str(store.absolute()), 'cat-file', 'blob', stage.oid], cwd=store
            ).stdout
            with target.open('xb') as stream:
                stream.write(blob)
            _filesystem.chmod(target, 0o600)
        temporary = directory / 'resolutions.toml.tmp'
        with temporary.open('xb') as manifest:
            _filesystem.fchmod(manifest.fileno(), 0o600)
            manifest.write(content)
            manifest.flush()
            os.fsync(manifest.fileno())
        _filesystem.replace(temporary, directory / 'resolutions.toml')
        _filesystem.sync_directory(directory)
    except (OSError, ValueError) as exc:
        raise PubanonError('cannot export saved merge conflicts') from exc


def parse_merge_result(result: GitResult) -> MergeResult:
    """Parse byte-only `merge-tree --write-tree -z --messages` output.

    Exit 1 remains conflicted even when neither structured section supplies
    actionable files. Other exits and malformed records are refused.
    """
    if result.returncode not in {0, 1}:
        detail = escape((result.stdout + result.stderr)[:512].decode(errors='replace'))
        raise PubanonError('Git merge failed', fact=f'exit status {result.returncode}: {detail}')
    fields = result.stdout.split(b'\0')
    if fields[-1] != b'' or len(fields) < _MIN_MERGE_FIELDS:
        raise PubanonError('Git returned truncated merge output')
    tree = _oid(fields.pop(0))
    fields.pop()
    if not fields:
        if result.returncode == 0:
            return MergeResult(tree, conflicted=False, conflicts=())
        raise PubanonError('Git returned truncated merge stages')
    try:
        separator = fields.index(b'')
    except ValueError as exc:
        raise PubanonError('Git returned unterminated merge stages') from exc
    stages = tuple(_stage(field) for field in fields[:separator])
    if len({(stage.path, stage.stage) for stage in stages}) != len(stages):
        raise PubanonError('Git returned duplicate merge stages')
    return MergeResult(tree, result.returncode == 1, _messages(fields[separator + 1 :], stages))


def read_resolutions(
    store: Path, directory: Path, pause: RewritePause
) -> tuple[str, tuple[Resolution, ...]]:
    """Build a tree using only explicit decisions validated against the frozen pause.

    Undeclared export edits are reported and ignored. Errors retain the export
    and saved state; no publication refs, ordinary index, or host are modified.

    Returns:
        The new Git tree ID and validated, deduplicated decisions in canonical
        path order, suitable for recording replay provenance.
    """
    snapshot = _validate_pause(store, pause)
    try:
        _check_directory(directory, trusted_parent=directory.parent)
        _check_directory(directory / 'tree', trusted_parent=directory.parent)
        decisions = _read_manifest(directory, pause)
        additions = _resolved_entries(store, directory, pause, snapshot, decisions)
        declared = {item.paths[0] for item in decisions} | set(additions)
        _report_undeclared(directory, snapshot, declared)
        tree = _write_resolved_tree(store, pause.tree, decisions, additions)
    except (OSError, UnicodeError, ValueError) as exc:
        raise PubanonError(
            'cannot read conflict resolution; keep or rename requires an existing '
            'safe file and a valid manifest'
        ) from exc
    return tree, decisions
