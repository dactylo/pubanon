"""Build and inspect retained publication candidates."""

# pyright: reportPrivateUsage=false

import ntpath
import os
import posixpath
import re
import shutil
import stat
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pubanon import _filesystem, _policy, _windows_fs
from pubanon._config import (
    GlobalConfig,
    Paths,
    ProjectConfig,
    assemble_rules,
    check_publication_state_paths,
    create_data_dirs,
    derived_private_terms,
    fingerprint,
    gate_details,
    identity_refusal,
    identity_rule_sources,
    require_project_name,
)
from pubanon._errors import PubanonError
from pubanon._gate import gate_unit
from pubanon._run import OID_HEX, git_failure_fact, run_git
from pubanon._snapshots import _audit, _records, _store
from pubanon._snapshots._records import (
    STATE_RETAINED,
    STATE_UNKNOWN_OUTCOME,
    CandidateRecord,
    RecordedDestination,
    load_candidate,
    save_candidate,
)
from pubanon._snapshots._source import (
    Entry,
    PinnedSource,
    Snapshot,
    path_portability_keys,
    read_snapshot,
    remove_bookmark,
    snapshot_digest,
    validate_symlink_graph,
)
from pubanon._terminal import (
    display_path,
    escape,
    output,
    render_fields,
    shell_command,
    style,
)

CANDIDATE_REF = 'refs/pubanon/candidate'
REWRITE_MERGED_REF = 'refs/pubanon/rewrite/merged'
_ERROR_PRIVILEGE_NOT_HELD = 1314
_OID_PATTERN = re.compile(OID_HEX)
_REWRITE_REPORT_FILE_LIMIT = 20
_VERSION_PATTERN = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')


@dataclass(frozen=True)
class CandidateInputs:
    """Group the source facts used to build one candidate.

    The first four positional constructor inputs provide the exact source
    snapshot, pinned selector, optional release version, and optional commit
    message. The version is omitted or has strict `X.Y.Z` form. Callers select
    the source and apply all path and blob gates before construction; these
    inputs do not establish complete host or publication acceptance.

    Replacement selection and scope are supplied together or both omitted.
    Omitting `prestate` makes construction read the current advisory-store main
    and tags.

    Attributes:
        prestate: Optional published main and tags captured before
            construction.
        replaces: Resolved old publication commit to correct.
        replace_scope: Correct one publication or replace all history.
    """

    _snapshot: Snapshot
    _pinned: PinnedSource
    _version: str | None
    _message: str | None
    prestate: tuple[str | None, dict[str, str]] | None = None
    replaces: str | None = None
    replace_scope: Literal['one', 'all'] | None = None


@dataclass(frozen=True)
class _BuildMetadata:
    """Hold resolved construction metadata without consulting saved provenance."""

    _parents: tuple[str, ...]
    _descendants: tuple[str, ...]
    _old_tree: str | None
    _old_message: str | None
    _tags: tuple[str, ...]
    _version: str | None
    _message: str | None


def _candidate_gate_units(
    global_config: GlobalConfig,
    record: CandidateRecord,
    snapshot: Snapshot,
) -> list[tuple[str, bytes, str]]:
    """Return every retained-candidate unit subject to current gates."""
    units = [(entry.path, entry.path.encode(), 'filename') for entry in snapshot]
    units.extend((entry.path, entry.data, 'blob') for entry in snapshot)
    units.append(('commit message', record.message.encode(), 'message'))
    identity = f'{global_config.anon_name} <{global_config.anon_email}>'.encode()
    units.append(('publication identity', identity, 'header identity value'))
    if record.tag_intent is not None:
        units.append(('tag intent', record.tag_intent.encode(), 'tag name'))
    return units


def _clean_inspection_path(inspect: Path) -> bool:
    """Remove one inspection directory without following its root symlink."""
    try:
        return _filesystem.remove_tree(inspect)
    except (OSError, RuntimeError) as exc:
        raise PubanonError('cannot clean review copy') from exc


def _complete_unreplayed(record: CandidateRecord, metadata: _BuildMetadata) -> CandidateRecord:
    """Complete all-history and latest-publication corrections without replay."""
    if record.replaces is None or (record.replace_scope != 'all' and metadata._descendants):
        return record
    tags = dict(record.prestate_tags) if record.replace_scope == 'one' else {}
    for tag in metadata._tags:
        tags[tag] = record.commit
    if record.tag_intent is not None:
        tags[record.tag_intent] = record.commit
    return replace(record, tip=record.commit, poststate_tags=tags)


def _discard_source_bookmark(record: CandidateRecord | None) -> None:
    """Release the candidate bookmark after rewrite directory cleanup."""
    if record is not None and record.source_bookmark is not None:
        try:
            remove_bookmark(record.source_bookmark, record.pinned.commit)
        except PubanonError as exc:
            raise PubanonError(
                'cannot discard source bookmark; snapshot retained',
                fact=f'{escape(str(record.source_bookmark.source_path))}: {exc.render()}',
                recovery='Restore access to the recorded source repository or correct '
                'the conflicting bookmark, then retry discard.',
            ) from exc
        output(
            "Removed the snapshot's private source bookmark. "
            'Branches, tags, and files were not changed.'
        )


def _git_result(
    args: Sequence[str],
    *,
    store: Path,
    failure: str,
    input_bytes: bytes | None = None,
    env_extra: Mapping[str, str] | None = None,
) -> bytes:
    """Run candidate plumbing and retain escaped failure diagnostics."""
    result = run_git(
        ['--git-dir', str(store.absolute()), *args],
        cwd=store,
        input_bytes=input_bytes,
        env_extra=env_extra,
        check=False,
    )
    if result.returncode != 0:
        raise PubanonError(failure, fact=git_failure_fact(result))
    return result.stdout


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


def _oid(output: bytes, failure: str) -> str:
    """Parse one object ID without exposing malformed command output."""
    if not output.endswith(b'\n') or b'\n' in output[:-1]:
        raise PubanonError(failure)
    try:
        oid = output[:-1].decode('ascii')
    except UnicodeDecodeError as exc:
        raise PubanonError(failure) from exc
    if _OID_PATTERN.fullmatch(oid) is None:
        raise PubanonError(failure)
    return oid


def _oids(output: bytes, count: int, failure: str) -> list[str]:
    """Parse an exact number of object ID response lines."""
    lines = output.splitlines(keepends=True)
    if len(lines) != count:
        raise PubanonError(failure)
    return [_oid(line, failure) for line in lines]


def _parse_commit_fields(raw: bytes) -> tuple[str, tuple[str, ...], bytes, bytes]:
    """Parse immutable tree, parents, author date, and message from one commit."""
    headers, separator, message = raw.partition(b'\n\n')
    lines = headers.split(b'\n')
    trees = [line[5:] for line in lines if line.startswith(b'tree ')]
    authors = [line.rsplit(b' ', 2)[-2:] for line in lines if line.startswith(b'author ')]
    raw_parents = [line[7:] for line in lines if line.startswith(b'parent ')]
    if not separator or len(trees) != 1 or len(authors) != 1:
        raise PubanonError('replacement commit metadata is invalid')
    try:
        tree = trees[0].decode('ascii')
        parents = tuple(parent.decode('ascii') for parent in raw_parents)
    except UnicodeDecodeError as exc:
        raise PubanonError('replacement commit metadata is invalid') from exc
    if any(_OID_PATTERN.fullmatch(oid) is None for oid in (tree, *parents)):
        raise PubanonError('replacement commit metadata is invalid')
    return tree, parents, b' '.join(authors[0]), message


def _commit_fields(store: Path, commit: str) -> tuple[str, tuple[str, ...], bytes, bytes]:
    """Read immutable tree, parents, author date, and message from one commit."""
    return _parse_commit_fields(
        _git_result(
            ['cat-file', 'commit', commit],
            store=store,
            failure='replacement commit is unavailable',
        )
    )


def _correction_parents(store: Path, record: CandidateRecord) -> tuple[str, ...]:
    """Return the exact correction parents implied by its frozen scope."""
    if record.replaces is None:
        return () if record.prestate_main is None else (record.prestate_main,)
    if record.replace_scope == 'all':
        if record.replaces != record.prestate_main:
            raise PubanonError('replacement all must select the published tip')
        return ()
    if record.replace_scope != 'one':
        raise PubanonError('replacement scope is invalid')
    return _commit_fields(store, record.replaces)[1]


def _path_collision(first: str, second: str) -> PubanonError:
    """Build a terminal-safe portability collision error."""
    first_display = escape(first)
    second_display = escape(second)
    return PubanonError(
        'unsupported snapshot path collision',
        fact=f'paths: {first_display}; {second_display}',
    )


def _project_dir(paths: Paths, project: str) -> Path:
    """Return one path-safe project data directory."""
    require_project_name(project)
    return paths.data_dir / 'projects' / project


def _read_ref(store: Path, ref: str) -> str | None:
    """Read one optional direct ref from the advisory store."""
    output = _git_result(
        ['for-each-ref', '--format=%(objectname)', ref],
        store=store,
        failure='cannot read snapshot store ref',
    )
    if not output:
        return None
    return _oid(output, 'Git returned an invalid snapshot store ref')


def _read_tags(store: Path) -> dict[str, str]:
    """Read the complete advisory store tag set."""
    output = _git_result(
        ['for-each-ref', '--format=%(refname:strip=2) %(objectname)', 'refs/tags'],
        store=store,
        failure='cannot read snapshot store tags',
    )
    tags: dict[str, str] = {}
    for line in output.splitlines():
        raw_tag, separator, raw_oid = line.rpartition(b' ')
        try:
            tag = raw_tag.decode('utf-8')
            oid = raw_oid.decode('ascii')
        except UnicodeDecodeError as exc:
            raise PubanonError('Git returned invalid snapshot store tags') from exc
        if not separator or not tag or tag in tags or _OID_PATTERN.fullmatch(oid) is None:
            raise PubanonError('Git returned invalid snapshot store tags')
        tags[tag] = oid
    return tags


def _replacement_chain(store: Path, selected: str, main: str | None) -> tuple[str, ...]:
    """Prove selected reachability and return every descendant in history order."""
    if main is None:
        raise PubanonError('nothing to replace; this would be the first publication')
    chain: list[str] = []
    current = main
    while True:
        _tree, parents, _date, _message = _commit_fields(store, current)
        if len(parents) > 1:
            raise PubanonError('replacement history is not linear')
        if current == selected:
            return tuple(reversed(chain))
        chain.append(current)
        if not parents:
            raise PubanonError('selected replacement is outside published main history')
        current = parents[0]


def _replacement_scope_rows(
    paths: Paths, project: str, record: CandidateRecord
) -> list[tuple[str, str]]:
    """Describe the replacement extent and tags absent from its completed poststate."""
    if record.replace_scope == 'all':
        if record.prestate_main is None:
            raise PubanonError('replacement snapshot is incomplete')
        count = len(_audit.published_commits(paths, project, new=record.prestate_main))
        scope = f'all published history ({count} snapshots) -> one snapshot'
    else:
        scope = 'one published snapshot and its replayed descendants'
    rows = [('Scope', scope)]
    if record.poststate_tags is not None:
        removed = sorted(record.prestate_tags.keys() - record.poststate_tags.keys())
        if removed:
            rows.append(('Removed tags', style(escape(', '.join(removed)), 'warning')))
    return rows


def _validate_tree_paths(snapshot: Snapshot) -> None:
    """Validate all candidate paths before object creation."""
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


def _validate_materialization(commit: str, snapshot: Snapshot) -> None:
    """Reject unsafe commit names and snapshot paths before touching a copy."""
    if _OID_PATTERN.fullmatch(commit) is None:
        raise PubanonError('invalid review copy commit')
    _validate_tree_paths(snapshot)
    _validate_windows_materialization(snapshot)


def _verify_commit_metadata(
    record: CandidateRecord,
    raw: bytes,
    recovery: str,
    parents: tuple[str, ...] | None = None,
) -> bytes:
    """Require exact retained commit headers and return their shared identity."""
    headers, separator, message = raw.partition(b'\n\n')
    expected_headers = [f'tree {record.tree}'.encode()]
    if parents is None:
        parents = () if record.prestate_main is None else (record.prestate_main,)
    expected_headers.extend(f'parent {parent}'.encode() for parent in parents)
    lines = headers.split(b'\n')
    if (
        not separator
        or len(lines) != len(expected_headers) + 2
        or lines[: len(expected_headers)] != expected_headers
    ):
        raise PubanonError(
            'snapshot commit metadata does not match the record',
            recovery=recovery,
        )
    identities: list[bytes] = []
    for label, line in zip((b'author', b'committer'), lines[-2:], strict=True):
        match = re.fullmatch(
            label + rb' (?P<identity>[^<>\r\n]+ <[^<>\r\n]+>) [0-9]+ \+0000',
            line,
        )
        if match is None:
            raise PubanonError(
                'snapshot commit metadata does not match the record',
                recovery=recovery,
            )
        identities.append(match.group('identity'))
    if identities[0] != identities[1]:
        raise PubanonError(
            'snapshot commit metadata does not match the record',
            recovery=recovery,
        )
    if message != record.message.encode() + b'\n':
        raise PubanonError(
            'snapshot commit metadata does not match the record',
            recovery=recovery,
        )
    return identities[0]


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
    except (OSError, RuntimeError) as exc:
        raise PubanonError('cannot verify review directory') from exc


def _verify_replayed_commits(store: Path, record: CandidateRecord, identity: bytes) -> str:
    """Prove each replay's parent, message, author date, and anonymous metadata."""
    queries = tuple(
        dict.fromkeys(
            oid.encode('ascii') for entry in record.replayed for oid in (entry.old, entry.new)
        )
    )
    objects = _audit.read_objects(store, queries)
    previous = record.commit
    for entry in record.replayed:
        old = objects[entry.old.encode('ascii')]
        new = objects[entry.new.encode('ascii')]
        if old is None or new is None or old[0] != b'commit' or new[0] != b'commit':
            raise PubanonError('replayed commit is unavailable')
        _old_tree, _old_parents, old_date, old_message = _parse_commit_fields(old[1])
        raw = new[1]
        new_tree, _parents, new_date, new_message = _parse_commit_fields(raw)
        if new_date != old_date or new_message != old_message:
            raise PubanonError('replayed commit changed the original author date or message')
        try:
            message = old_message.removesuffix(b'\n').decode('utf-8')
        except UnicodeDecodeError as exc:
            raise PubanonError('replayed commit message is invalid') from exc
        replay_record = replace(record, commit=entry.new, tree=new_tree, message=message)
        if _verify_commit_metadata(replay_record, raw, 'pubanon discard', (previous,)) != identity:
            raise PubanonError('replayed commit changed the anonymous identity')
        previous = entry.new
    return previous


def _write_blobs(store: Path, snapshot: Snapshot) -> list[str]:
    """Write all candidate blobs through one import stream."""
    if not snapshot:
        return []
    stream = bytearray(b'feature get-mark\nfeature done\n')
    for mark, entry in enumerate(snapshot, start=1):
        stream.extend(f'blob\nmark :{mark}\ndata {len(entry.data)}\n'.encode())
        stream.extend(entry.data)
        stream.extend(f'\nget-mark :{mark}\n'.encode())
    stream.extend(b'done\n')
    output = _git_result(
        ['fast-import', '--quiet'],
        store=store,
        failure='cannot write snapshot blob',
        input_bytes=bytes(stream),
    )
    return _oids(output, len(snapshot), 'Git returned an invalid snapshot blob ID')


def _write_trees(store: Path, contents: dict[str, list[tuple[bytes, bytes]]]) -> str:
    """Write candidate trees in one batch per directory depth."""
    root_oid: str | None = None
    depths = sorted(
        {directory.count('/') + bool(directory) for directory in contents},
        reverse=True,
    )
    for depth in depths:
        directories = sorted(
            (
                directory
                for directory in contents
                if directory.count('/') + bool(directory) == depth
            ),
            key=lambda item: item.encode('utf-8'),
        )
        tree_input = bytearray()
        for directory in directories:
            tree_input.extend(b''.join(record for _name, record in sorted(contents[directory])))
            tree_input.extend(b'\0')
        tree_oids = _oids(
            _git_result(
                ['mktree', '-z', '--batch'],
                store=store,
                failure='cannot write snapshot tree',
                input_bytes=bytes(tree_input),
            ),
            len(directories),
            'Git returned an invalid snapshot tree ID',
        )
        for directory, tree_oid in zip(directories, tree_oids, strict=True):
            if directory:
                parent, _, name = directory.rpartition('/')
                raw_name = name.encode('utf-8')
                contents[parent].append(
                    (
                        raw_name,
                        b'040000 tree ' + tree_oid.encode() + b'\t' + raw_name + b'\0',
                    )
                )
            else:
                root_oid = tree_oid
    if root_oid is None:
        raise PubanonError('cannot write snapshot root tree')
    return root_oid


def _write_tree(store: Path, snapshot: Snapshot) -> str:
    """Write snapshot blobs and deterministic bottom-up trees."""
    sorted_snapshot = tuple(sorted(snapshot, key=lambda item: item.path.encode('utf-8')))
    _validate_tree_paths(sorted_snapshot)
    contents: dict[str, list[tuple[bytes, bytes]]] = {'': []}
    blob_oids = _write_blobs(store, sorted_snapshot)
    for entry, oid in zip(sorted_snapshot, blob_oids, strict=True):
        parent = PurePosixPath(entry.path).parent
        while str(parent) != '.':
            contents.setdefault(parent.as_posix(), [])
            parent = parent.parent
        directory, _, name = entry.path.rpartition('/')
        raw_name = name.encode('utf-8')
        contents[directory].append(
            (raw_name, f'{entry.mode:o} blob {oid}\t'.encode() + raw_name + b'\0')
        )
    return _write_trees(store, contents)


def candidate_store_path(paths: Paths, project: str) -> Path:
    """Return the project's advisory bare-store path without creating it.

    Raises:
        PubanonError: The project name is invalid.
    """
    return _store.candidate_store_path(paths, project)


def _discard_rewrite(
    paths: Paths, project: str, entries: tuple[_records.ReplayEntry, ...]
) -> bool:
    """Release unfinished replay artifacts, retaining records on cleanup failure."""
    directory = _records.rewrite_directory(paths, project)
    present = directory.exists() or _records.rewrite_path(paths, project).exists()
    if directory.is_symlink():
        raise PubanonError('cannot discard rewrite directory through a symlink')
    try:
        if directory.exists():
            shutil.rmtree(directory)
    except OSError as exc:
        raise PubanonError('cannot discard rewrite directory; snapshot retained') from exc
    for entry in entries:
        if entry.source_bookmark is not None and entry.original_source is not None:
            remove_bookmark(entry.source_bookmark, entry.original_source)
    store = candidate_store_path(paths, project)
    if (
        store.exists()
        and run_git(
            [
                '--git-dir',
                str(store.absolute()),
                'show-ref',
                '--verify',
                '--quiet',
                REWRITE_MERGED_REF,
            ],
            cwd=store,
            check=False,
        ).returncode
        == 0
    ):
        old = _read_ref(store, REWRITE_MERGED_REF)
        if old is not None:
            _git_result(
                ['update-ref', '--no-deref', '-d', REWRITE_MERGED_REF, old],
                store=store,
                failure='cannot discard rewrite ref',
            )
            present = True
    return present


def _rewritten_change_report(
    paths: Paths, project: str, record: CandidateRecord, *, complete: bool = False
) -> str:
    """Describe each replacement against its own original release."""
    if record.replaces is None:
        return ''
    store = candidate_store_path(paths, project)
    pairs: list[tuple[str, str, tuple[_records.Resolution, ...]]] = [
        (record.replaces, record.commit, ())
    ]
    pairs.extend((entry.old, entry.new, entry.resolutions) for entry in record.replayed)
    lines = ['Replaced snapshots:' if complete else style('Replaced snapshots', 'heading')]
    for old, new, resolutions in pairs:
        lines.append(f'  {old if complete else old[:12]} -> {new if complete else new[:12]}')
        changed = (
            _git_result(
                ['diff', '--name-status', '--no-renames', old, new],
                store=store,
                failure='cannot describe rewritten release changes',
            )
            .decode(errors='surrogateescape')
            .splitlines()
        )
        shown = changed if complete else changed[:_REWRITE_REPORT_FILE_LIMIT]
        # Git quotes tabs within filenames; literal tabs separate report columns.
        lines.extend('    ' + escape(line.replace('\t', '  ')) for line in shown)
        if not complete and len(changed) > _REWRITE_REPORT_FILE_LIMIT:
            lines.append(
                f'    ... and {len(changed) - _REWRITE_REPORT_FILE_LIMIT} more file changes'
            )
        if not changed:
            lines.append('    No file changes relative to the original release.')
        lines.extend(
            f'    Manual resolution: {escape(", ".join(resolution.paths))}'
            for resolution in resolutions
        )
    before = record.prestate_tags
    after = record.poststate_tags or {}
    moved = [tag for tag in sorted(after) if tag in before and before[tag] != after[tag]]
    if moved:
        lines.append('')
        lines.append('Tags moved:' if complete else style('Tags moved', 'heading'))
        lines.extend(
            f'  {escape(tag)}: {before[tag] if complete else before[tag][:12]} -> '
            f'{after[tag] if complete else after[tag][:12]}'
            for tag in moved
        )
    return '\n\n' + '\n'.join(lines)


def _candidate_change_summary(
    paths: Paths, project: str, record: CandidateRecord
) -> tuple[str, Path]:
    """Bound terminal diff statistics and retain a complete private file report."""
    store = candidate_store_path(paths, project)
    args = ['diff-tree', '--no-commit-id', '-r', '--root', '--no-renames']
    tip = record.commit if record.tip is None else _records.candidate_tip(record)
    revisions = [tip] if record.prestate_main is None else [record.prestate_main, tip]
    summary = (
        _git_result(
            [*args, '--stat', '--stat-count=20', *revisions],
            store=store,
            failure='cannot render snapshot changes',
        )
        .decode(errors='surrogateescape')
        .rstrip('\n')
    )
    complete = _git_result(
        [*args, '--numstat', *revisions],
        store=store,
        failure='cannot read complete snapshot file report',
    ).decode(errors='surrogateescape')
    rewritten = _rewritten_change_report(paths, project, record, complete=True)
    path = create_data_dirs(paths, project) / f'candidate-files-{record.commit}.txt'
    descriptor = None
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        _filesystem.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            descriptor = None
            handle.write('Added lines, removed lines, path (binary files use -):\n')
            for line in complete.split('\n'):
                if line:
                    handle.write(escape(line.replace('\t', '  ')) + '\n')
            if rewritten:
                handle.write(rewritten + '\n')
    except (OSError, RuntimeError, UnicodeError) as exc:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        raise PubanonError('cannot write complete snapshot file report') from exc
    heading = (
        'first release' if record.prestate_main is None else 'since recorded publication base'
    )
    lines = '\n'.join(
        f'  {escape(line.removeprefix(" "))}'
        for line in (summary or '0 files changed').split('\n')
    )
    return f'{heading}\n{lines}', path


def _verify_candidate_objects(
    paths: Paths,
    project: str,
    record: CandidateRecord,
    *,
    recovery: str,
    anchor: str | None = None,
) -> tuple[Snapshot, bytes]:
    """Prove one stable retained record against its immutable Git objects."""
    if load_candidate(paths, project) != record:
        raise PubanonError('snapshot record changed before verification', recovery=recovery)
    store = candidate_store_path(paths, project)
    candidate_ref = run_git(
        ['--git-dir', str(store.absolute()), 'rev-parse', '--verify', CANDIDATE_REF],
        cwd=store,
        check=False,
    )
    if anchor is None:
        anchor = record.commit if record.tip is None else _records.candidate_tip(record)
    if candidate_ref.returncode != 0 or candidate_ref.stdout != f'{anchor}\n'.encode():
        raise PubanonError(
            'saved snapshot commit could not be verified',
            fact=git_failure_fact(candidate_ref) if candidate_ref.returncode != 0 else None,
            recovery=recovery,
        )
    result = run_git(
        ['--git-dir', str(store.absolute()), 'cat-file', 'commit', record.commit],
        cwd=store,
        check=False,
    )
    if result.returncode != 0:
        raise PubanonError(
            'snapshot commit cannot be read', fact=git_failure_fact(result), recovery=recovery
        )
    verified_identity = _verify_commit_metadata(
        record, result.stdout, recovery, _correction_parents(store, record)
    )
    snapshot = read_snapshot(store, record.commit, git_dir=store)
    if snapshot_digest(snapshot) != record.snapshot_digest:
        raise PubanonError(
            'saved snapshot files do not match what was prepared', recovery=recovery
        )
    if load_candidate(paths, project) != record:
        raise PubanonError('snapshot record changed during verification', recovery=recovery)
    return snapshot, verified_identity


def _verify_replacement_content(
    paths: Paths,
    global_config: GlobalConfig,
    project: ProjectConfig,
    record: CandidateRecord,
    derived: tuple[str, ...],
) -> Snapshot:
    """Audit poststate history and gate complete entries introduced by replay."""
    tip, tags = _records.candidate_poststate(record)
    rules = assemble_rules(global_config, project, derived)
    audit = _audit.audit_history(
        paths,
        project.name,
        _audit.published_commits(paths, project.name, new=tip),
        tags,
        rules,
        global_config,
        tip=tip,
    )
    if audit.identity_problems:
        raise PubanonError(
            'replacement history has identity problems',
            fact=escape('; '.join(audit.identity_problems)),
        )
    # Historical content findings belong to the existing exposure acknowledgment
    # flow. Only complete changed entries are new publication content here.
    store = candidate_store_path(paths, project.name)
    last_new: Snapshot | None = None
    for entry in record.replayed:
        old = read_snapshot(store, entry.old, git_dir=store)
        new = read_snapshot(store, entry.new, git_dir=store)
        _validate_tree_paths(new)
        check_publication_state_paths(paths, project, [item.path for item in new])
        changed = _audit.changed_entries(old, new)
        units = [(item.path, item.path.encode(), 'filename') for item in changed]
        units.extend((item.path, item.data, 'blob') for item in changed)
        details = gate_details(units, global_config, project, derived)
        if details:
            raise PubanonError(
                'the replayed snapshot did not pass current content checks',
                fact=escape(f'{entry.old[:12]}: ' + '; '.join(details.values())),
                recovery='pubanon discard, then pubanon prepare',
            )
        last_new = new
    if last_new is not None and record.replayed[-1].new == tip:
        return last_new
    return read_snapshot(store, tip, git_dir=store)


def _verify_replacement_objects(
    paths: Paths,
    project: str,
    record: CandidateRecord,
    identity: bytes,
) -> None:
    """Verify the full frozen replacement independently of mutable configuration."""
    if record.replaces is None:
        return
    tip, tags = _records.candidate_poststate(record)
    store = candidate_store_path(paths, project)
    descendants = _replacement_chain(store, record.replaces, record.prestate_main)
    expected = descendants if record.replace_scope == 'one' else ()
    if tuple(entry.old for entry in record.replayed) != expected:
        raise PubanonError('replacement replay does not contain the complete descendant chain')
    previous = _verify_replayed_commits(store, record, identity)
    mapping = {record.replaces: record.commit}
    mapping.update((entry.old, entry.new) for entry in record.replayed)
    expected_tags = (
        {tag: mapping.get(oid, oid) for tag, oid in record.prestate_tags.items()}
        if record.replace_scope == 'one'
        else {}
    )
    if record.tag_intent is not None and (
        record.replace_scope == 'all' or record.replaces not in record.prestate_tags.values()
    ):
        if record.replace_scope == 'one' and record.tag_intent in expected_tags:
            raise PubanonError('intended tag is already published')
        expected_tags[record.tag_intent] = record.commit
    if tags != expected_tags or tip != previous:
        raise PubanonError('replacement poststate does not match the complete replay')
    name, email = identity.decode('utf-8').rsplit(' <', 1)
    frozen = GlobalConfig(anon_name=name, anon_email=email[:-1], accounts={}, sensitive_terms=())
    audit = _audit.audit_history(
        paths,
        project,
        _audit.published_commits(paths, project, new=tip),
        tags,
        {},
        frozen,
        tip=tip,
    )
    if audit.identity_problems:
        raise PubanonError(
            'replacement history has identity problems',
            fact=escape('; '.join(audit.identity_problems)),
        )


def check_recovery_state(paths: Paths, project: str, *, force: bool) -> None:
    """Refuse unreadable or unresolved publication evidence before local removal.

    The caller holds the project lock. `force` abandons this evidence without
    undoing any possible host publication; confirmed publications are accepted.
    """
    if force:
        return
    try:
        record = load_candidate(paths, project)
    except PubanonError as exc:
        raise PubanonError(
            str(exc),
            fact=f'{escape(str(_records.candidate_path(paths, project)))}: {exc.render()}',
        ) from exc
    try:
        _records.load_rewrite(paths, project)
    except PubanonError as exc:
        raise PubanonError(
            str(exc), fact=f'{escape(str(_records.rewrite_path(paths, project)))}: {exc.render()}'
        ) from exc
    if record is not None and record.state == STATE_UNKNOWN_OUTCOME:
        raise PubanonError(
            'the outcome of the last publication is unknown',
            recovery='pubanon publish --resolve',
        )


def _candidate_for_discard(paths: Paths, project: str, *, force: bool) -> CandidateRecord | None:
    """Read trusted discard metadata or explicitly abandon malformed evidence."""
    check_recovery_state(paths, project, force=force)
    try:
        return load_candidate(paths, project)
    except PubanonError:
        if not force:
            raise
        output(
            'Source-bookmark cleanup could not be verified from the malformed snapshot record.',
            kind='warning',
        )
        return None


def inspection_path(paths: Paths, project: str, commit: str) -> Path:
    """Return a validated inspection location without creating directories.

    Raises:
        PubanonError: The project name or snapshot commit is invalid.
    """
    project_dir = _project_dir(paths, project)
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
    root = _project_dir(paths, project) / 'inspections'
    copies: list[Path] = []
    try:
        if root.is_symlink():
            raise PubanonError('cannot read snapshot directories through a symlink')
        if root.exists():
            if os.name == 'nt':
                _windows_fs.check_directory(root)
            copies.extend(path for path in root.iterdir() if _OID_PATTERN.fullmatch(path.name))
    except (OSError, RuntimeError) as exc:
        raise PubanonError('cannot list snapshot directories') from exc
    return tuple(sorted(copies))


def materialize_directory(directory: Path, snapshot: Snapshot) -> None:
    """Create an exclusive directory containing exact snapshot entries.

    POSIX mode bits restrict the directory to owner-only access.

    Validate paths, modes, collisions, and symlink targets before creation.
    The parent directory must exist. Existing files or directories are refused;
    a newly created partial directory is removed on failure or interruption.
    """
    _validate_tree_paths(snapshot)
    validate_symlink_graph(snapshot)
    _validate_windows_materialization(snapshot)
    try:
        if os.name == 'nt':
            _windows_fs.check_directory(directory.parent)
        directory.mkdir(mode=0o700)
    except (OSError, RuntimeError) as exc:
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
    except (OSError, RuntimeError, PubanonError) as exc:
        _clean_inspection_path(directory)
        if isinstance(exc, PubanonError):
            raise
        raise PubanonError('cannot materialize review copy') from exc


def publication_archive(paths: Paths, project: str, commit: str) -> bool:
    """Verify an owned direct archive ref or report its absence.

    Conflicting or symbolic references are refused; similarly prefixed child
    references are not evidence for the exact publication.
    """
    store = candidate_store_path(paths, project)
    if not store.exists():
        return False
    ref = _records.publication_ref(commit)
    symbolic = run_git(
        ['--git-dir', str(store.absolute()), 'symbolic-ref', '--quiet', ref],
        cwd=store,
        check=False,
    )
    if symbolic.returncode != 1:
        raise PubanonError('publication archive reference is not a direct ref', fact=ref)
    refs = run_git(
        [
            '--git-dir',
            str(store.absolute()),
            'for-each-ref',
            '--format=%(refname) %(objectname)',
            ref,
        ],
        cwd=store,
    ).stdout.splitlines()
    for line in refs:
        name, separator, target = line.partition(b' ')
        if name != ref.encode():
            continue
        if not separator or target != commit.encode():
            raise PubanonError('publication archive reference conflicts with snapshot', fact=ref)
        return True
    return False


def _replayed_for_discard(
    paths: Paths, project: str, *, force: bool
) -> tuple[_records.ReplayEntry, ...]:
    """Read replay cleanup locators and refuse confirmed outputs before any mutation."""
    record = _candidate_for_discard(paths, project, force=force)
    try:
        progress = _records.load_rewrite(paths, project)
    except PubanonError:
        if not force:
            raise
        progress = None
        output('Rewrite provenance could not be verified for source cleanup.', kind='warning')
    entries = (
        *(() if record is None else record.replayed),
        *(() if progress is None else progress.replayed),
    )
    for entry in entries:
        if _records.load_publication(paths, project, entry.new) is not None or publication_archive(
            paths, project, entry.new
        ):
            raise PubanonError(
                'confirmed publication cannot be discarded', recovery='pubanon publish --resolve'
            )
    return entries


def publication_record(
    paths: Paths, project: str, candidate: CandidateRecord, commit: str, confirmed_at: datetime
) -> _records.PublicationRecord:
    """Derive one confirmation record from frozen candidate facts and objects.

    `commit` must be the correction or one replay entry. A replay record
    derives its tree, message, digest, provenance, and matching strict release
    tag from the retained poststate and omits pinned-source data. The result is
    not persisted.
    """
    entry = next((entry for entry in candidate.replayed if entry.new == commit), None)
    if commit != candidate.commit and entry is None:
        raise PubanonError('publication is outside the snapshot transaction')
    tree = candidate.tree
    digest = candidate.snapshot_digest
    message = candidate.message
    tag = candidate.tag_intent
    if entry is not None:
        store = candidate_store_path(paths, project)
        raw = run_git(
            ['--git-dir', str(store.absolute()), 'cat-file', 'commit', commit], cwd=store
        ).stdout
        tree = raw.splitlines()[0].removeprefix(b'tree ').decode('ascii')
        message = raw.partition(b'\n\n')[2].decode('utf-8')
        digest = snapshot_digest(read_snapshot(store, commit, git_dir=store))
        tags = _records.candidate_poststate(candidate)[1]
        tag = next(
            (
                name
                for name in sorted(tags)
                if tags[name] == commit
                and name.startswith('v')
                and _VERSION_PATTERN.fullmatch(name[1:]) is not None
            ),
            None,
        )
    return _records.PublicationRecord(
        commit=commit,
        tree=tree,
        snapshot_digest=digest,
        fingerprint=candidate.fingerprint,
        message=message,
        tag_intent=tag,
        pinned=candidate.pinned if entry is None else None,
        destination=candidate.destination,
        confirmed_at=confirmed_at,
        source_bookmark=candidate.source_bookmark if entry is None else entry.source_bookmark,
        replaces=candidate.replaces if entry is None else None,
        replace_scope=candidate.replace_scope if entry is None else None,
        derivation=None
        if entry is None
        else _records.Derivation(
            entry.old, entry.original_source, candidate.commit, entry.resolutions
        ),
    )


def published_refs(paths: Paths, project: str) -> tuple[str | None, dict[str, str]]:
    """Read the published main and tags from one advisory store.

    Args:
        paths: Derived Pubanon configuration and data paths.
        project: Registered project name.

    Returns:
        The optional main tip and complete tag map.

    Raises:
        PubanonError: The project name or stored references are invalid.
    """
    store = candidate_store_path(paths, project)
    if not store.exists():
        return None, {}
    return _read_ref(store, 'refs/heads/main'), _read_tags(store)


def record_path(paths: Paths, project: str) -> Path:
    """Return one candidate record path.

    Raises:
        PubanonError: The project name is invalid.
    """
    return _records.candidate_path(paths, project)


def replacement_descendants(store: Path, selected: str, main: str | None) -> tuple[str, ...]:
    """Prove linear main ancestry and return descendants after the selected commit.

    Returns:
        Descendants in oldest-first order, excluding `selected`; empty when
        `selected` equals `main`.

    Raises:
        PubanonError: Main is missing, history is nonlinear, or `selected` is
            outside main's ancestry.
    """
    return _replacement_chain(store, selected, main)


def select_rewritten_commit(record: CandidateRecord, selector: str | None) -> str:
    """Resolve a rewritten commit or moved tag from one candidate record.

    Args:
        record: Candidate containing the rewritten commits and moved tags.
        selector: `None` for the candidate tip, or an exact moved tag or unique
            case-insensitive object-ID prefix for a rewritten commit.

    Raises:
        PubanonError: The candidate is not a replacement, the selector is
            unknown or ambiguous, or a tag points to untouched history.
    """
    if record.replaces is None:
        raise PubanonError('no replaced snapshot is retained')
    rewritten = (record.commit, *(entry.new for entry in record.replayed))
    if selector is None:
        selected = _records.candidate_tip(record)
    elif record.poststate_tags is not None and selector in record.poststate_tags:
        selected = record.poststate_tags[selector]
    else:
        matches = (
            [oid for oid in rewritten if oid.startswith(selector.lower())] if selector else []
        )
        if len(matches) != 1:
            raise PubanonError('rewritten selector is unknown or ambiguous', fact=escape(selector))
        selected = matches[0]
    if selected not in rewritten:
        raise PubanonError(
            'selector does not name a replaced snapshot', fact=escape(selector or '')
        )
    return selected


def store_path(paths: Paths, project: str) -> Path:
    """Create and return the project's advisory bare store.

    Raises:
        PubanonError: Project selection is invalid or the store cannot be
            initialized.
    """
    return _store.store_path(paths, project)


def tag_intent_for(version: str | None) -> str | None:
    """Return the validated tag intent for one optional version.

    Args:
        version: Optional `X.Y.Z` release version.

    Returns:
        `vX.Y.Z`, or `None` for an untagged candidate.

    Raises:
        PubanonError: The version is not `X.Y.Z`.
    """
    if version is None:
        return None
    if _VERSION_PATTERN.fullmatch(version) is None:
        raise PubanonError('version must have X.Y.Z form')
    return f'v{version}'


def _commit_message(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
    version: str | None,
    message: str | None,
) -> tuple[str | None, str]:
    """Validate tag intent and gate the controlled commit message."""
    tag_intent = tag_intent_for(version)
    controlled = (
        message
        if message is not None
        else (f'release: {tag_intent}' if tag_intent is not None else 'publication update')
    )
    if not controlled:
        raise PubanonError('commit message must not be empty')
    derived = derived_private_terms(paths, p, os.environ)
    rules = assemble_rules(g, p, derived)

    identity = f'{g.anon_name} <{g.anon_email}>'
    identity_hits = gate_unit('publication identity', identity.encode(), rules)
    if identity_hits:
        raise identity_refusal(identity_rule_sources(identity_hits, g, p, derived))
    if tag_intent is not None and gate_unit('tag intent', tag_intent.encode(), rules):
        raise PubanonError('the release tag did not pass content checks')
    if gate_unit('commit message', controlled.encode(), rules):
        raise PubanonError('the snapshot commit message did not pass content checks')
    return tag_intent, controlled


def tree_paths(paths: Paths, project: str, commit: str) -> list[str]:
    """Return the paths of one store commit without reading blob bodies.

    Args:
        paths: Derived Pubanon configuration and data paths.
        project: Registered project name.
        commit: Commit or tree object ID in the advisory store.

    Returns:
        Snapshot-relative posix paths in Git order.

    Raises:
        PubanonError: The store read fails or returns malformed paths.
    """
    output = _git_result(
        ['ls-tree', '-r', '-z', '--name-only', '--full-tree', commit],
        store=candidate_store_path(paths, project),
        failure='cannot read published paths',
    )
    records = output.split(b'\0')
    if records[-1:] != [b'']:
        raise PubanonError('cannot read published paths')
    try:
        return [record.decode('utf-8') for record in records[:-1]]
    except UnicodeDecodeError as exc:
        raise PubanonError('cannot read published paths') from exc


def credential_warning(paths: Paths, project: str, record: CandidateRecord) -> str:
    """Describe credential-like files included in the prepared publication history."""
    commits = (record.commit, *(entry.new for entry in record.replayed))
    secrets = sorted(
        {
            path
            for commit in commits
            for path in _policy.secret_like(tree_paths(paths, project, commit), ())
        }
    )
    if not secrets:
        return ''
    count = len(secrets)
    return style(
        f'warning: Snapshot includes {count} credential-like file{"s" if count != 1 else ""}: '
        + ', '.join(escape(path) for path in secrets)
        + '.',
        'warning',
    )


def _candidate_report_details(
    paths: Paths,
    record: CandidateRecord,
    _g: GlobalConfig,
    p: ProjectConfig,
    *,
    verbose: bool = False,
    extra_rows: Sequence[tuple[str, str]] = (),
) -> str:
    """Verify retained objects and render their common factual report."""
    snapshot, verified_identity = _verify_candidate_objects(
        paths, p.name, record, recovery='pubanon discard, then pubanon prepare'
    )
    identity = escape(verified_identity.decode(errors='surrogateescape'))
    summary, file_list = _candidate_change_summary(paths, p.name, record)
    tip = record.tip if record.tip is not None else record.commit
    if tip != record.commit:
        snapshot = read_snapshot(candidate_store_path(paths, p.name), tip)
    tag = 'untagged' if record.tag_intent is None else escape(record.tag_intent)
    if record.state == STATE_UNKNOWN_OUTCOME:
        state = 'publication outcome unknown'
    elif record.replaces is not None and record.tip is None:
        state = 'preparation incomplete'
    else:
        state = 'prepared'
    multi_snapshot = record.replaces is not None and tip != record.commit
    title = f'Prepared snapshots ({state})' if multi_snapshot else f'Snapshot {tag} ({state})'
    rows = [
        (
            'Destination',
            (
                f'{escape(record.destination.owner)}/{escape(record.destination.repo)} '
                f'({escape(record.destination.visibility)})'
            ),
        ),
        ('Identity', identity),
    ]
    if multi_snapshot:
        rows.append(('Correction version', tag))
        final_tags = sorted(
            tag for tag, commit in (record.poststate_tags or {}).items() if commit == tip
        )
        rows.append(('Final version', ', '.join(escape(tag) for tag in final_tags) or 'untagged'))
    message_label = 'Correction message' if multi_snapshot else 'Message'
    message_lines = record.message.rstrip('\n').split('\n')
    message = (
        escape(message_lines[0])
        if len(message_lines) == 1
        else ('\n' + '\n'.join('  ' + escape(line) for line in message_lines))
    )
    rows.append((message_label, message))
    summary_heading, _, rest = summary.partition('\n')
    if record.prestate_main is None:
        summary_heading += f', {len(snapshot)} file{"s" if len(snapshot) != 1 else ""}'
    rows.append(
        ('Final snapshot changes' if multi_snapshot else 'Changes', summary_heading + '\n' + rest)
    )
    if record.replaces is not None:
        width = 40 if verbose else 12
        rows.append(('Replaces', record.replaces[:width]))
        rows.extend(_replacement_scope_rows(paths, p.name, record))
        if multi_snapshot:
            rows.extend(
                [
                    ('Correction snapshot ID', record.commit[:width]),
                    ('Final snapshot ID', tip[:width]),
                ]
            )
    rows.extend(extra_rows)
    rows.append(('File list', display_path(file_list)))
    if verbose:
        rows.extend(
            [
                ('Operation', 'replacement' if record.replaces is not None else 'new'),
                (
                    'Correction source commit' if multi_snapshot else 'Source commit',
                    record.pinned.commit,
                ),
                ('Snapshot commit', tip),
                ('Snapshot tree', record.tree),
            ]
        )
    lines = [style(title, 'heading'), render_fields(rows)]
    warning = credential_warning(paths, p.name, record)
    if warning:
        lines.append(warning)
    return '\n'.join(lines)


def inspect_report(
    paths: Paths,
    record: CandidateRecord,
    g: GlobalConfig,
    p: ProjectConfig,
    *,
    verbose: bool = False,
) -> str:
    """Render the complete candidate inspection report.

    Verify the retained objects and compare their snapshot with the recorded
    publication prestate. Current policy and live host checks are not rerun.
    The returned terminal report bounds its displayed file list and writes a
    separate complete private file report. The caller must hold the project
    lock for the complete operation.

    Args:
        paths: Derived Pubanon paths.
        record: Retained candidate to report.
        g: Active global configuration.
        p: Active project configuration.
        verbose: Include full Git object IDs and the snapshot tree.

    Returns:
        Publication identity, diff, exception, and gate facts.

    Raises:
        PubanonError: Retained objects disagree with the record, or the
            candidate diff and complete private file report cannot be produced.
    """
    configuration_unchanged = fingerprint(g, p) == record.fingerprint
    if configuration_unchanged:
        configuration = 'unchanged since prepare'
    elif record.state == STATE_UNKNOWN_OUTCOME:
        configuration = (
            'changed since prepare; checks have not been rerun with these settings; '
            'resolve the publication outcome first'
        )
    else:
        configuration = (
            'changed since prepare; checks have not been rerun with these settings; '
            'snapshot cannot publish'
        )
    configuration_kind = 'success' if configuration_unchanged else 'warning'
    checks = 'no sensitive terms found when prepared; snapshot verified; '
    checks += style('GitHub not rechecked', 'warning')
    rows = [('Checks', checks)]
    if verbose or not configuration_unchanged:
        rows.append(('Publication configuration', style(configuration, configuration_kind)))
    details = _candidate_report_details(paths, record, g, p, verbose=verbose, extra_rows=rows)
    return details + _rewritten_change_report(paths, p.name, record)


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
                except (OSError, RuntimeError) as exc:
                    raise PubanonError('cannot lock review directory') from exc
                return inspect
            _clean_inspection_path(inspect)
    except (OSError, RuntimeError) as exc:
        raise PubanonError('cannot create review copy') from exc
    materialize_directory(inspect, snapshot)
    try:
        _lock_inspection_copy(inspect, snapshot)
    except (OSError, RuntimeError) as exc:
        _clean_inspection_path(inspect)
        raise PubanonError('cannot lock review directory') from exc
    return inspect


def materialize_inspect(
    paths: Paths,
    project: str,
    record: CandidateRecord,
    *,
    refresh: bool = False,
) -> Path:
    """Create or reuse the verified resulting tip's read-only inspection copy.

    Existing copies must match the saved snapshot on ordinary reuse. Refresh
    explicitly replaces the selected copy. The copy is not publication
    authority.

    The caller must hold the project lock for the complete operation.

    Args:
        paths: Derived Pubanon paths.
        project: Registered project name.
        record: Retained candidate to inspect.

        refresh: Replace the selected copy, including any local changes.

    Returns:
        Path to the candidate-specific inspection directory.

    Raises:
        PubanonError: Exact materialization fails.
    """
    snapshot, verified_identity = _verify_candidate_objects(
        paths, project, record, recovery='pubanon discard, then pubanon prepare'
    )
    _verify_replacement_objects(paths, project, record, verified_identity)
    commit = _records.candidate_tip(record)
    if commit != record.commit:
        store = candidate_store_path(paths, project)
        snapshot = read_snapshot(store, commit, git_dir=store)
    return materialize_snapshot(paths, project, commit, snapshot, refresh=refresh)


def publication_report(
    paths: Paths,
    record: CandidateRecord,
    g: GlobalConfig,
    p: ProjectConfig,
    *,
    verbose: bool = False,
) -> str:
    """Render retained candidate facts for a publication preview.

    Verify object integrity and report retained metadata, file changes, and
    credential exceptions. The caller supplies current gate and live-host
    verification facts; this report does not perform or claim those checks.
    The returned terminal report bounds its displayed file list and writes a
    separate complete private file report. The caller must hold the project
    lock for the complete operation.

    Args:
        paths: Derived Pubanon paths.
        record: Retained candidate to report.
        g: Active global configuration.
        p: Active project configuration.
        verbose: Include full Git object IDs and the snapshot tree.

    Returns:
        The shared factual report without inspection-only instructions.

    Raises:
        PubanonError: Retained objects or the complete file report cannot be
            verified.
    """
    return _candidate_report_details(paths, record, g, p, verbose=verbose)


def reprepare_advice(record: CandidateRecord, *, project: str | None = None) -> str:
    """Describe how to rebuild the selected source revision and release tag."""
    selection = [f'--project={project}'] if project is not None else []
    prepare = ['pubanon', 'prepare', *selection]
    if record.replaces is not None:
        selector = 'all' if record.replace_scope == 'all' else record.replaces
        prepare.extend(('--replace', selector))
    prepare.extend(('--source', record.pinned.commit))
    if record.tag_intent is not None:
        prepare.extend(('--version', record.tag_intent[1:]))
    discard_command = shell_command('pubanon', 'discard', *selection)
    prepare_command = shell_command(*prepare)
    if discard_command is None or prepare_command is None:
        return (
            f'Select project {escape(project or "")}, then rerun discard and prepare '
            'for this snapshot.'
        )
    return f'{discard_command} && {prepare_command}'


def inspect_next_action(record: CandidateRecord, g: GlobalConfig, p: ProjectConfig) -> str:
    """Return the next inspection action, prioritizing an unknown publish outcome."""
    if record.state == STATE_UNKNOWN_OUTCOME:
        return 'pubanon publish --resolve'
    if fingerprint(g, p) == record.fingerprint:
        return 'pubanon publish'
    return reprepare_advice(record, project=p.name)


def inspect_rewritten(
    paths: Paths,
    project: str,
    record: CandidateRecord,
    selector: str | None,
    *,
    refresh: bool = False,
) -> Path:
    """Materialize a verified corrected or replayed commit selected by tag or OID.

    The caller must hold the project lock for the complete operation.

    Args:
        paths: Paths derived for this command.
        project: Registered project name.
        record: Candidate containing the rewritten commits.
        selector: `None` for the candidate tip; other selectors follow
            `select_rewritten_commit`.
        refresh: Replace the selected copy instead of refusing local edits.

    Returns:
        Path to the selected commit's read-only inspection directory.

    Raises:
        PubanonError: Retained replacement evidence, selection, or exact
            materialization fails.
    """
    snapshot, identity = _verify_candidate_objects(
        paths, project, record, recovery='pubanon discard, then pubanon prepare'
    )
    try:
        _verify_replacement_objects(paths, project, record, identity)
    except UnicodeDecodeError as exc:
        raise PubanonError(
            'retained replacement commit identity is not valid UTF-8',
            fact=record.commit,
            recovery='pubanon discard, then pubanon prepare',
        ) from exc
    selected = select_rewritten_commit(record, selector)
    if selected != record.commit:
        store = candidate_store_path(paths, project)
        snapshot = read_snapshot(store, selected, git_dir=store)
    try:
        return materialize_snapshot(paths, project, selected, snapshot, refresh=refresh)
    except PubanonError as exc:
        if exc.recovery != 'pubanon inspect --refresh':
            raise
        raise PubanonError(
            str(exc), recovery=f'pubanon inspect --rewritten {selected} --refresh'
        ) from exc


def verify_publication_objects(paths: Paths, project: str, record: CandidateRecord) -> None:
    """Verify exact correction objects and the complete replacement poststate.

    This works after operational candidate refs have been removed and does not
    consult mutable configuration or the source repository.

    Raises:
        PubanonError: Required correction or replay objects are unavailable or
            conflict with retained evidence.
    """
    store = candidate_store_path(paths, project)
    result = run_git(
        ['--git-dir', str(store.absolute()), 'cat-file', 'commit', record.commit],
        cwd=store,
        check=False,
    )
    if result.returncode != 0:
        raise PubanonError('confirmed publication commit is unavailable', fact=record.commit)
    identity = _verify_commit_metadata(
        record, result.stdout, 'pubanon publish --resolve', _correction_parents(store, record)
    )
    _verify_replacement_objects(paths, project, record, identity)
    if (
        snapshot_digest(read_snapshot(store, record.commit, git_dir=store))
        != record.snapshot_digest
    ):
        raise PubanonError('confirmed publication content conflicts with snapshot')


def confirmed_publication(paths: Paths, project: str, record: CandidateRecord) -> bool:
    """Validate durable proof that a candidate was previously confirmed.

    A strict matching publication record or an owned direct archive ref is
    sufficient. Conflicting provenance or refs raise `PubanonError`; absent
    evidence returns false. This does not observe the host's current state.
    """
    if record.replaces is not None and record.tip is None:
        if _records.load_publication(
            paths, project, record.commit
        ) is not None or publication_archive(paths, project, record.commit):
            raise PubanonError('the incomplete snapshot conflicts with saved published history')
        return False
    tip = _records.candidate_tip(record)
    published = _records.load_publication(paths, project, tip)
    archive = publication_archive(paths, project, tip)
    if published is None and not archive:
        return False
    verify_publication_objects(paths, project, record)
    for commit in (record.commit, *(entry.new for entry in record.replayed)):
        existing = _records.load_publication(paths, project, commit)
        if existing is not None and existing != publication_record(
            paths, project, record, commit, existing.confirmed_at
        ):
            raise PubanonError(
                'publication record conflicts with snapshot provenance', fact=commit
            )
        publication_archive(paths, project, commit)
    return True


def _discard_preflight(paths: Paths, project: str, *, force: bool) -> CandidateRecord | None:
    """Refuse confirmed or protected candidates before any cleanup."""
    record = _candidate_for_discard(paths, project, force=force)
    if record is not None:
        try:
            confirmed = confirmed_publication(paths, project, record)
        except PubanonError:
            store = candidate_store_path(paths, project)
            if (
                not force
                or record.source_bookmark is not None
                or _records.load_publication(paths, project, record.commit) is not None
                or run_git(
                    ['--git-dir', str(store.absolute()), 'rev-parse', '--git-dir'],
                    cwd=store,
                    check=False,
                ).returncode
                == 0
            ):
                raise
            # Forced abandonment may proceed for an unbookmarked candidate
            # with an unreadable store and no recorded publication.
            # It never removes publication records or archive refs.
            confirmed = False
        if confirmed:
            raise PubanonError(
                'confirmed publication cannot be discarded', recovery='pubanon publish --resolve'
            )
        if force and record.state == STATE_UNKNOWN_OUTCOME:
            output(
                'Abandoning unresolved snapshot recovery information; this does not undo any '
                'possible GitHub publication.',
                kind='warning',
            )
    return record


def discard(paths: Paths, project: str, *, force: bool) -> bool:
    """Discard one candidate and its review copies.

    The caller must hold the project lock for the complete operation.

    Args:
        paths: Derived Pubanon paths.
        project: Registered project name.
        force: Whether to abandon unreadable or unknown-outcome candidate
            evidence and continue cleanup after a candidate-ref deletion
            failure. This does not undo any publication on the host.

    Returns:
        Whether a candidate artifact was removed.

    Raises:
        PubanonError: Confirmed publication evidence, conflicting or
            inaccessible owned bookmarks, or cleanup prevents removal.
            `force` never bypasses confirmed-publication or bookmark
            protection. Forced cleanup can remove the record before reporting
            an undeleted advisory candidate ref.
    """
    replayed = _replayed_for_discard(paths, project, force=force)
    record = _discard_preflight(paths, project, force=force)
    commits = [entry.new for entry in replayed]
    if record is not None:
        commits.extend((record.commit, record.tip or record.commit))
    for commit in dict.fromkeys(commits):
        clean_inspect(paths, project, commit=commit)
    try:
        rewrite_present = _discard_rewrite(paths, project, replayed)
    except PubanonError as exc:
        raise PubanonError(
            'cannot finish rewrite cleanup; snapshot records retained',
            fact=exc.render(),
            recovery='Correct the reported problem, then retry discard. '
            'The retained records locate any remaining rewrite artifacts and bookmarks.',
        ) from exc
    _discard_source_bookmark(record)
    leftovers: list[str] = []
    store = candidate_store_path(paths, project)
    ref_present = False
    if store.exists():
        ref_present = (
            run_git(
                [
                    '--git-dir',
                    str(store.absolute()),
                    'show-ref',
                    '--verify',
                    '--quiet',
                    CANDIDATE_REF,
                ],
                cwd=store,
                check=False,
            ).returncode
            == 0
        )
        old = _read_ref(store, CANDIDATE_REF) if ref_present else None
        args = ['update-ref', '--no-deref', '-d', CANDIDATE_REF]
        if old is not None:
            args.append(old)
        result = run_git(['--git-dir', str(store.absolute()), *args], cwd=store, check=False)
        if result.returncode != 0:
            if not force:
                raise PubanonError('cannot discard snapshot ref', fact=git_failure_fact(result))
            leftovers.append(f'saved snapshot reference: {git_failure_fact(result)}')
    candidate_record = record_path(paths, project)
    record_present = candidate_record.exists()
    try:
        _records.rewrite_path(paths, project).unlink(missing_ok=True)
        candidate_record.unlink(missing_ok=True)
    except (OSError, RuntimeError) as exc:
        raise PubanonError('cannot discard snapshot record') from exc
    if leftovers:
        raise PubanonError(
            'snapshot record discarded; local cleanup is incomplete',
            fact=', '.join(leftovers),
            recovery='pubanon repo restore --to local',
        )
    return ref_present or record_present or rewrite_present


def verify_retained_candidate(
    paths: Paths,
    global_config: GlobalConfig,
    project: ProjectConfig,
    record: CandidateRecord,
    *,
    env: Mapping[str, str],
) -> Snapshot:
    """Re-prove one retained candidate against current content gates.

    Replacement verification proves all required history identity metadata
    while applying content gates to changed replay content. Unchanged
    historical disclosure is allowed.

    Args:
        paths: Derived Pubanon paths.
        global_config: Active global configuration.
        project: Active project configuration.
        record: Candidate record loaded by the current command.

        env: Current command environment for dynamic private-path terms.

    Returns:
        The verified resulting tip snapshot. The record's tree, digest, and
        pinned source continue to describe the correction snapshot.

    Raises:
        PubanonError: The record, candidate objects, or current gates fail.
    """
    recovery = 'pubanon discard, then pubanon prepare'
    snapshot, identity = _verify_candidate_objects(paths, project.name, record, recovery=recovery)
    _verify_replacement_objects(paths, project.name, record, identity)
    if record.state == STATE_UNKNOWN_OUTCOME:
        raise PubanonError(
            'the outcome of the last publication is unknown',
            recovery='pubanon publish --resolve',
        )
    if fingerprint(global_config, project) != record.fingerprint:
        raise PubanonError(
            'the configuration changed after the snapshot was prepared',
            recovery=recovery,
        )
    current_identity = f'{global_config.anon_name} <{global_config.anon_email}>'.encode()
    if identity != current_identity:
        raise PubanonError(
            'snapshot commit metadata does not match the record',
            recovery=recovery,
        )

    check_publication_state_paths(paths, project, [entry.path for entry in snapshot])
    derived = derived_private_terms(paths, project, env)
    details = gate_details(
        _candidate_gate_units(global_config, record, snapshot), global_config, project, derived
    )
    if details:
        raise PubanonError(
            'the snapshot did not pass current content checks',
            fact=escape('; '.join(details.values())),
            recovery=recovery,
        )
    if record.replaces is not None:
        return _verify_replacement_content(paths, global_config, project, record, derived)
    return snapshot


def verify_rewrite_progress(
    paths: Paths, project: str, record: CandidateRecord, progress: _records.RewriteRecord
) -> str:
    """Verify a frozen partial replay and return its current retained anchor.

    An anchor one unsaved step ahead is verified against the next old commit;
    the caller may reset it to the saved base with an expected-old ref update
    and replay that step. Missing anchors are restored from verified progress.

    The caller must hold the project lock for the complete operation.

    Raises:
        PubanonError: Saved progress conflicts, descendant proof is incomplete,
            replay metadata is invalid, or the retained anchor is unrelated.
    """
    if (
        record.tip is not None
        or record.replaces != progress.replaces
        or record.commit != progress.correction
        or record.prestate_main != progress.prestate_main
        or _records.load_rewrite(paths, project) != progress
    ):
        raise PubanonError('rewrite record conflicts with retained snapshot')
    store = candidate_store_path(paths, project)
    expected = replacement_descendants(store, progress.replaces, progress.prestate_main)
    if (
        progress.descendants != expected
        or tuple(entry.old for entry in progress.replayed) != expected[: len(progress.replayed)]
    ):
        raise PubanonError('rewrite descendants do not match frozen publication history')
    raw = _git_result(
        ['cat-file', 'commit', record.commit], store=store, failure='correction is unavailable'
    )
    identity = _verify_commit_metadata(
        record, raw, 'pubanon discard', _correction_parents(store, record)
    )
    previous = _verify_replayed_commits(
        store, replace(record, replayed=progress.replayed), identity
    )
    anchor = _read_ref(store, CANDIDATE_REF)
    if anchor is None:
        _git_result(
            ['update-ref', '--no-deref', CANDIDATE_REF, previous, '0' * len(previous)],
            store=store,
            failure='cannot restore rewrite anchor',
        )
        anchor = previous
    elif anchor != previous:
        if len(progress.replayed) == len(expected):
            raise PubanonError('rewrite anchor differs from completed progress')
        pending = _records.ReplayEntry(expected[len(progress.replayed)], anchor)
        _verify_replayed_commits(
            store, replace(record, replayed=(*progress.replayed, pending)), identity
        )
    _verify_candidate_objects(paths, project, record, recovery='pubanon discard', anchor=anchor)
    return anchor


def version_from_tag(tag: str) -> str | None:
    """Return the version from one strict `vX.Y.Z` release tag.

    Args:
        tag: Candidate Git tag name.

    Returns:
        The `X.Y.Z` version, or `None` for a nonrelease tag.
    """
    if not tag.startswith('v'):
        return None
    version = tag[1:]
    return version if _VERSION_PATTERN.fullmatch(version) is not None else None


def replacement_version(version: str | None, tags: Sequence[str]) -> str | None:
    """Resolve correction version policy from every tag on the selected release.

    Args:
        version: Explicit release version, if supplied.
        tags: Tags moving with the corrected publication.

    Returns:
        The explicit or unambiguous inferred release version.

    Raises:
        PubanonError: An explicit version does not name a moved tag.
    """
    if version is None and len(tags) == 1:
        return version_from_tag(tags[0])
    if version is not None and tags and tag_intent_for(version) not in tags:
        raise PubanonError(
            'replacement version must match a moved tag',
            recovery='Commit the version correction or publish a new version.',
        )
    return version


def _build_metadata(
    store: Path,
    inputs: CandidateInputs,
    parent: str | None,
    prestate_tags: dict[str, str],
) -> _BuildMetadata:
    """Resolve parents and message from frozen Git objects and release selections."""
    if (inputs.replaces is None) != (inputs.replace_scope is None):
        raise PubanonError('replacement selection and scope must be supplied together')
    if inputs.replaces is None:
        parents = () if parent is None else (parent,)
        return _BuildMetadata(parents, (), None, None, (), inputs._version, inputs._message)
    descendants = _replacement_chain(store, inputs.replaces, parent)
    old_tree, old_parents, _date, raw_message = _commit_fields(store, inputs.replaces)
    if inputs.replace_scope == 'all' and inputs.replaces != parent:
        raise PubanonError('replacement all must select the published tip')
    parents = old_parents if inputs.replace_scope == 'one' else ()
    try:
        old_message = raw_message.removesuffix(b'\n').decode('utf-8')
    except UnicodeDecodeError as exc:
        raise PubanonError('replacement commit message is invalid') from exc
    tags = tuple(
        tag
        for tag, oid in prestate_tags.items()
        if oid == inputs.replaces and inputs.replace_scope == 'one'
    )
    version = replacement_version(inputs._version, tags)
    return _BuildMetadata(
        parents,
        descendants,
        old_tree,
        old_message,
        tags,
        version,
        old_message if inputs._message is None else inputs._message,
    )


def build_candidate(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
    inputs: CandidateInputs,
) -> CandidateRecord:
    """Build and retain one publication candidate.

    The caller must hold the project lock for the complete operation and must
    first select the source and apply source path and blob gates. Construction
    gates identity, commit message, and tag intent; it does not establish
    complete source, content, host, or publication acceptance. Replacement
    selection and scope must be paired. Earlier replacements produce an
    incomplete candidate that requires replay.

    Args:
        paths: Derived Pubanon configuration and data paths.
        g: Global publication identity configuration.
        p: Project publication configuration.
        inputs: Exact source facts and optional release selections.

    Returns:
        The retained candidate record.

    Raises:
        PubanonError: A candidate exists or construction fails closed.
    """
    if record_path(paths, p.name).exists():
        raise PubanonError('a snapshot is already prepared')
    store = store_path(paths, p.name)
    if inputs.prestate is None:
        parent = _read_ref(store, 'refs/heads/main')
        prestate_tags = _read_tags(store)
    else:
        parent, prestate_tags = inputs.prestate
    metadata = _build_metadata(store, inputs, parent, prestate_tags)
    tag_intent, controlled_message = _commit_message(
        paths, g, p, metadata._version, metadata._message
    )
    if len(metadata._tags) > 1:
        tag_intent = None
    if inputs.replace_scope == 'one' and not metadata._tags and tag_intent in prestate_tags:
        raise PubanonError('intended tag is already published')
    tree = _write_tree(store, inputs._snapshot)
    if tree == metadata._old_tree and controlled_message == metadata._old_message:
        output('Replacement has unchanged files and commit message.', kind='warning')
    epoch = int(time.time())
    date = f'{epoch} +0000'
    env = {
        'GIT_AUTHOR_DATE': date,
        'GIT_AUTHOR_EMAIL': g.anon_email,
        'GIT_AUTHOR_NAME': g.anon_name,
        'GIT_COMMITTER_DATE': date,
        'GIT_COMMITTER_EMAIL': g.anon_email,
        'GIT_COMMITTER_NAME': g.anon_name,
    }
    args = ['commit-tree', tree]
    for correction_parent in metadata._parents:
        args.extend(['-p', correction_parent])
    commit = _oid(
        _git_result(
            args,
            store=store,
            failure='cannot write snapshot commit',
            input_bytes=controlled_message.encode() + b'\n',
            env_extra=env,
        ),
        'Git returned an invalid snapshot commit ID',
    )
    if commit == inputs.replaces:
        # Equal objects in the same second still need a distinct correction ID.
        env['GIT_AUTHOR_DATE'] = env['GIT_COMMITTER_DATE'] = f'{epoch + 1} +0000'
        commit = _oid(
            _git_result(
                args,
                store=store,
                failure='cannot write snapshot commit',
                input_bytes=controlled_message.encode() + b'\n',
                env_extra=env,
            ),
            'Git returned an invalid snapshot commit ID',
        )
    _git_result(
        ['update-ref', '--no-deref', CANDIDATE_REF, commit],
        store=store,
        failure='cannot anchor snapshot commit',
    )
    record = CandidateRecord(
        commit=commit,
        tree=tree,
        snapshot_digest=snapshot_digest(inputs._snapshot),
        tag_intent=tag_intent,
        fingerprint=fingerprint(g, p),
        pinned=inputs._pinned,
        destination=RecordedDestination(
            p.host,
            p.owner,
            p.repo,
            p.visibility,
            g.accounts[p.host],
        ),
        prestate_main=parent,
        prestate_tags=prestate_tags,
        state=STATE_RETAINED,
        message=controlled_message,
        replaces=inputs.replaces,
        replace_scope=inputs.replace_scope,
    )
    record = _complete_unreplayed(record, metadata)
    save_candidate(paths, p.name, record)
    return record
