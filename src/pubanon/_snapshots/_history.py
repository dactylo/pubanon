"""Preserve and catalog confirmed publications with their private provenance."""

import heapq
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pubanon import _filesystem
from pubanon._config import Paths, ProjectConfig
from pubanon._errors import OperationCancelled, PubanonError
from pubanon._run import OID_HEX, git_failure_fact, run_git
from pubanon._snapshots import _candidate, _records, _source
from pubanon._snapshots._records import CandidateRecord, PublicationRecord
from pubanon._terminal import escape, shell_command
from pubanon._terminal import field as display_field

_OID_PREFIX = re.compile(r'[0-9a-fA-F]+')
_PUBLICATION_REPAIR = (
    'Restore the local publication store from a backup, or use '
    '`pubanon repo verify --repair-store` if the host still contains this published snapshot.'
)
_VERSION_TAG = re.compile(r'v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')


@dataclass(frozen=True)
class PublicationEntry:
    """Describe one locally recorded published commit.

    Attributes:
        commit: Full sanitized publication commit ID.
        tags: Known release labels from records and operational tags.
        commit_time: Git committer time, absent when the object is unavailable.
        record: Private confirmation and source provenance, when retained.
        replaced: Whether absent from the ancestry of the recorded current
            main.
    """

    commit: str
    tags: tuple[str, ...]
    commit_time: datetime | None
    record: PublicationRecord | None
    replaced: bool = False


def _archive_publication(paths: Paths, project: str, commit: str) -> None:
    """Create or verify one exact owned archive reference."""
    operation = 'verify' if _candidate.publication_archive(paths, project, commit) else 'create'
    store = _candidate.store_path(paths, project)
    run_git(
        ['--git-dir', str(store.absolute()), 'update-ref', '--no-deref', '--stdin'],
        cwd=store,
        input_bytes=f'{operation} {_records.publication_ref(commit)} {commit}\n'.encode(),
    )


def _archive_roots(paths: Paths, project: str) -> set[str]:
    """Read only verified owned publication archives as preserved history roots."""
    store = _candidate.candidate_store_path(paths, project)
    if not store.exists():
        return set()
    refs = run_git(
        [
            '--git-dir',
            str(store.absolute()),
            'for-each-ref',
            '--format=%(refname)%00%(objectname)%00%(symref)',
            'refs/pubanon/published/',
        ],
        cwd=store,
    )
    roots: set[str] = set()
    for raw in refs.stdout.splitlines():
        try:
            name, target, symbolic = raw.split(b'\0')
        except ValueError as exc:
            raise PubanonError('invalid snapshot archive reference') from exc
        try:
            ref = name.decode('ascii')
        except UnicodeDecodeError as exc:
            raise PubanonError('invalid snapshot archive reference') from exc
        commit = ref.removeprefix('refs/pubanon/published/')
        if re.fullmatch(OID_HEX, commit) is None:
            raise PubanonError('invalid snapshot archive reference', fact=escape(ref))
        if symbolic:
            raise PubanonError('publication archive reference is not a direct ref', fact=ref)
        if target != commit.encode():
            raise PubanonError('publication archive reference conflicts with snapshot', fact=ref)
        roots.add(commit)
    return roots


def _catalog_ancestors(
    entries: dict[str, PublicationEntry], graph: dict[str, tuple[int, tuple[str, ...]]]
) -> dict[str, set[str]]:
    """Find nearest listed ancestors through the available commit graph."""
    ancestors: dict[str, set[str]] = {oid: set() for oid in entries}
    for oid in entries:
        visited: set[str] = set()
        pending = list(graph.get(oid, (0, ()))[1])
        while pending:
            parent = pending.pop()
            if parent in visited:
                continue
            visited.add(parent)
            if parent in entries:
                ancestors[oid].add(parent)
            else:
                pending.extend(graph.get(parent, (0, ()))[1])
    return ancestors


def _ordered_entries(
    entries: dict[str, PublicationEntry], graph: dict[str, tuple[int, tuple[str, ...]]]
) -> tuple[PublicationEntry, ...]:
    """Order known publications after descendants, using dates across tips."""
    ancestors = _catalog_ancestors(entries, graph)
    incoming = dict.fromkeys(entries, 0)
    for predecessors in ancestors.values():
        for parent in predecessors:
            incoming[parent] += 1

    def _priority(oid: str) -> tuple[int, str]:
        entry = entries[oid]
        when = entry.record.confirmed_at if entry.record is not None else entry.commit_time
        return (-int(when.timestamp()) if when is not None else 0, oid)

    ready = [_priority(oid) for oid, count in incoming.items() if count == 0]
    heapq.heapify(ready)
    ordered: list[PublicationEntry] = []
    while ready:
        _, oid = heapq.heappop(ready)
        ordered.append(entries[oid])
        for parent in ancestors[oid]:
            incoming[parent] -= 1
            if incoming[parent] == 0:
                heapq.heappush(ready, _priority(parent))
    return tuple(ordered)


def _parse_worktree_registration(block: bytes) -> tuple[Path, str | None, bool]:
    """Decode one NUL-separated porcelain registration."""
    location: Path | None = None
    head: str | None = None
    detached = False
    for field in block.strip(b'\0').split(b'\0'):
        if field.startswith(b'worktree '):
            location = Path(os.fsdecode(field[9:]))
        elif field.startswith(b'HEAD '):
            raw_head = field[5:]
            if re.fullmatch(OID_HEX.encode(), raw_head) is not None:
                head = raw_head.decode('ascii')
        elif field == b'detached':
            detached = True
    if location is None or not location.is_absolute():
        raise PubanonError('Git returned malformed worktree registrations')
    return location, head, detached


def _publication_source(record: PublicationRecord) -> str | None:
    """Return the exact pinned source or a derived publication's source input."""
    if record.pinned is not None:
        return record.pinned.commit
    return record.derivation.original_source if record.derivation is not None else None


def _delete_source(
    project: ProjectConfig, record: PublicationRecord
) -> _source.SourceBookmark | None:
    """Preflight the owned source ref, accepting only verified relocation."""
    bookmark = record.source_bookmark
    if bookmark is None:
        return None
    source_commit = _publication_source(record)
    if source_commit is None:
        raise PubanonError('source bookmark has no recorded source commit')
    common = bookmark.git_common_dir
    result = run_git(
        ['--git-dir', str(common), 'rev-parse', '--path-format=absolute', '--git-common-dir'],
        cwd=None,
        check=False,
    )
    if (
        not common.is_dir()
        or common.resolve() != common
        or result.returncode != 0
        or result.stdout != os.fsencode(common.as_posix()) + b'\n'
    ):
        recovery = _source.source_recovery(
            bookmark, source_commit, relocated_source=project.source_path
        )
        if recovery.status != 'protected' or recovery.source_path is None:
            raise PubanonError(
                'source repository is unavailable for deleting',
                fact=escape(str(common)),
                recovery=recovery.reason,
            )
        bookmark = _source.describe_bookmark(recovery.source_path, project.name, record.commit)
    prefix = ['--git-dir', str(bookmark.git_common_dir)]
    symbolic = run_git(
        [*prefix, 'symbolic-ref', '--quiet', bookmark.refname], cwd=None, check=False
    )
    if symbolic.returncode != 1:
        raise PubanonError(
            'source bookmark is not a direct ref', fact=escape(str(bookmark.git_common_dir))
        )
    refs = run_git(
        [*prefix, 'for-each-ref', '--format=%(refname) %(objectname)', bookmark.refname], cwd=None
    ).stdout.splitlines()
    for line in refs:
        name, separator, target = line.partition(b' ')
        if name == bookmark.refname.encode() and (
            not separator or target != source_commit.encode()
        ):
            raise PubanonError(
                'source bookmark changed to another commit',
                fact=escape(str(bookmark.git_common_dir)),
            )
    return bookmark


def _registered_worktrees(source: Path) -> dict[Path, tuple[str | None, bool]]:
    """Read registered worktree locations, HEADs and detached state."""
    result = run_git(['worktree', 'list', '--porcelain', '-z'], cwd=source, check=False)
    if result.returncode != 0:
        raise PubanonError(
            'cannot inspect source worktree registrations', fact=git_failure_fact(result)
        )
    if result.stdout and not result.stdout.endswith(b'\0'):
        raise PubanonError('Git returned malformed worktree registrations')
    registered: dict[Path, tuple[str | None, bool]] = {}
    for block in result.stdout.split(b'\0\0'):
        if not block:
            continue
        location, head, detached = _parse_worktree_registration(block)
        if location in registered:
            raise PubanonError('Git returned malformed worktree registrations')
        registered[location] = head, detached
    return registered


def _rev_list(store: Path, roots: set[str], *, parents: bool) -> list[str]:
    """Walk only explicitly supplied publication roots."""
    if not roots:
        return []
    args = ['rev-list']
    if parents:
        args.extend(('--parents', '--timestamp'))
    args.extend(sorted(roots))
    result = run_git(['--git-dir', str(store.absolute()), *args], cwd=store, check=False)
    if result.returncode != 0:
        raise PubanonError('cannot read local publication history', fact=git_failure_fact(result))
    try:
        return result.stdout.decode('ascii').splitlines()
    except UnicodeDecodeError as exc:
        raise PubanonError('Git returned invalid publication history') from exc


def _publication_graph(store: Path, roots: set[str]) -> dict[str, tuple[int, tuple[str, ...]]]:
    """Read timestamps and parents for the known publication graph."""
    graph: dict[str, tuple[int, tuple[str, ...]]] = {}
    for line in _rev_list(store, roots, parents=True):
        timestamp, oid, *parents = line.split()
        if re.fullmatch(OID_HEX, oid) is None or any(
            re.fullmatch(OID_HEX, parent) is None for parent in parents
        ):
            raise PubanonError('Git returned invalid publication history')
        try:
            graph[oid] = int(timestamp), tuple(parents)
        except ValueError as exc:
            raise PubanonError('Git returned invalid publication history') from exc
    return graph


def _release_archive(paths: Paths, project: str, commit: str) -> None:
    """Remove this archive while retaining other listed ancestors."""
    if _candidate.publication_archive(paths, project, commit):
        graph = _publication_graph(_candidate.candidate_store_path(paths, project), {commit})
        deleted = _records.deleted_publications(paths, project)
        pending = list(graph.get(commit, (0, ()))[1])
        visited: set[str] = set()
        while pending:
            parent = pending.pop()
            if parent in visited:
                continue
            visited.add(parent)
            if parent in deleted:
                pending.extend(graph.get(parent, (0, ()))[1])
            else:
                _archive_publication(paths, project, parent)
        store = _candidate.candidate_store_path(paths, project)
        run_git(
            ['--git-dir', str(store.absolute()), 'update-ref', '--no-deref', '--stdin'],
            cwd=store,
            input_bytes=f'delete {_records.publication_ref(commit)} {commit}\n'.encode(),
        )


def _retained_dependencies(paths: Paths, project: str) -> tuple[set[str], set[str]]:
    """Collect frozen publication IDs and owned source locators needed for recovery."""
    commits: set[str] = set()
    bookmarks: set[str] = set()
    active = _records.load_candidate(paths, project)
    progress = _records.load_rewrite(paths, project)
    replayed: tuple[_records.ReplayEntry, ...] = ()
    if active is not None:
        commits.add(active.commit)
        if active.source_bookmark is not None:
            bookmarks.add(active.source_bookmark.refname)
        replayed = active.replayed
        if active.replaces is not None:
            commits.update((active.replaces, *active.prestate_tags.values()))
            commits.update(oid for oid in (active.prestate_main, active.tip) if oid is not None)
            if active.replace_scope == 'all' and active.prestate_main is not None:
                commits.update(
                    _rev_list(
                        _candidate.candidate_store_path(paths, project),
                        {active.prestate_main},
                        parents=False,
                    )
                )
            elif active.replace_scope == 'one' and active.tip is None and progress is None:
                commits.update(
                    _candidate.replacement_descendants(
                        _candidate.candidate_store_path(paths, project),
                        active.replaces,
                        active.prestate_main,
                    )
                )
    if progress is not None:
        commits.update(
            (progress.correction, progress.replaces, progress.prestate_main, *progress.descendants)
        )
        replayed += progress.replayed
    for entry in replayed:
        commits.update((entry.old, entry.new))
        bookmarks.update(
            bookmark.refname
            for bookmark in (entry.original_bookmark, entry.source_bookmark)
            if bookmark is not None
        )
    return commits, bookmarks


def _delete_preflight(
    paths: Paths, project: ProjectConfig, entry: PublicationEntry
) -> _source.SourceBookmark | None:
    """Refuse stale records, candidate dependencies, and conflicting owned refs."""
    record = entry.record
    if _records.load_publication(paths, project.name, entry.commit) != record:
        raise PubanonError('published snapshot record changed before deleting')
    commits, bookmarks = _retained_dependencies(paths, project.name)
    if entry.commit in commits or (
        record is not None
        and record.source_bookmark is not None
        and record.source_bookmark.refname in bookmarks
    ):
        raise PubanonError(
            'local recovery evidence is required by an active snapshot or rewrite',
            recovery='Resolve the prepared snapshot before deleting this published snapshot.',
        )
    _candidate.publication_archive(paths, project.name, entry.commit)
    return _delete_source(project, record) if record is not None else None


def _worktree_guidance(
    paths: Paths,
    project: ProjectConfig,
    record: PublicationRecord,
    recovery: _source.SourceRecovery,
    source_commit: str,
) -> tuple[str, ...]:
    """Describe a verified source's worktree occupancy and runnable commands."""
    source = recovery.source_path
    if source is None:
        return ()
    destination = (
        paths.data_dir / 'projects' / project.name / 'source-worktrees' / record.commit
    ).absolute()
    lines = [
        (
            f'{"Source input" if record.derivation else "Source"}: '
            f'{recovery.status} at {escape(str(source))}'
        ),
    ]
    if recovery.reason:
        lines.append(f'Source protection: {escape(recovery.reason)}')
    registered = _registered_worktrees(source)
    current = registered.get(destination)
    try:
        occupied = destination.exists() or destination.is_symlink()
        parent_conflict = destination.parent.is_symlink() or (
            destination.parent.exists() and not destination.parent.is_dir()
        )
    except (OSError, RuntimeError) as exc:
        raise PubanonError('cannot inspect suggested source worktree path') from exc
    if parent_conflict:
        lines.append(
            f'Source worktree parent is occupied or conflicting: {escape(str(destination.parent))}'
        )
        return tuple(lines)
    if current is not None and occupied and current == (source_commit, True):
        lines.append(f'Matching detached source worktree: {escape(str(destination))}')
    elif current is not None or occupied:
        lines.append(
            f'Source worktree path is occupied or conflicting: {escape(str(destination))}'
        )
        return tuple(lines)
    else:
        command = shell_command(
            'git',
            '-C',
            str(source),
            'worktree',
            'add',
            '--detach',
            str(destination),
            source_commit,
        )
        if command is None:
            lines.append('Source path cannot be displayed as a safe runnable shell command.')
            return tuple(lines)
        lines.append(command)
    removal = shell_command('git', '-C', str(source), 'worktree', 'remove', str(destination))
    if removal is None:
        lines.append('Source path cannot be displayed as a safe runnable removal command.')
    else:
        lines.append(removal)
        lines.append('Removing this worktree leaves the main checkout and branches unchanged.')
    return tuple(lines)


def delete_guidance(
    paths: Paths, project: ProjectConfig, entry: PublicationEntry
) -> tuple[str, ...]:
    """Preflight cleanup under the project lock and describe its exact scope."""
    _delete_preflight(paths, project, entry)
    return (
        display_field('Snapshot', entry.commit[:12]),
        display_field('Version', escape(', '.join(entry.tags) or 'untagged')),
        'Delete this local snapshot entry, review directory, and source recovery information.',
        'GitHub and source working directories are unchanged.',
    )


def delete_publication(paths: Paths, project: ProjectConfig, entry: PublicationEntry) -> None:
    """Release owned preservation under the project lock with retry information.

    The caller obtains confirmation after `delete_guidance`. Ownership is
    checked again here and at each ref deletion. Recorded publications retain
    their recovery record until last; recordless entries save the deletion
    before releasing their archive. A partial failure reports the retry
    command.
    """
    bookmark = _delete_preflight(paths, project, entry)
    record = entry.record
    if record is None:
        completed: list[str] = []
        remaining = ['review directory', 'local deletion', 'archive bookmark']
        try:
            _candidate.clean_inspect(paths, project.name, commit=entry.commit)
            completed.append('review directory removed or absent')
            remaining.pop(0)
            _records.mark_publication_deleted(paths, project.name, entry.commit)
            completed.append('local deletion saved')
            remaining.pop(0)
            _release_archive(paths, project.name, entry.commit)
        except (PubanonError, OSError, KeyboardInterrupt) as exc:
            error_type = (
                OperationCancelled
                if isinstance(exc, (KeyboardInterrupt, OperationCancelled))
                else PubanonError
            )
            retry = shell_command('pubanon', 'delete', entry.commit, f'--project={project.name}')
            raise error_type(
                'local preservation cleanup is incomplete',
                fact=(
                    f'Completed: {", ".join(completed) or "none"}. '
                    f'Remaining: {", ".join(remaining)}. {escape(str(exc))}'
                ),
                recovery=retry
                or f'Select project {escape(project.name)}, then retry delete for this snapshot.',
            ) from exc
        return
    if bookmark is not None and bookmark != record.source_bookmark:
        record = _records.relocate_publication_source(paths, project.name, record, bookmark)
    source_commit = _publication_source(record)
    if bookmark is not None and source_commit is None:
        raise PubanonError('source bookmark has no recorded source commit')
    completed = []
    remaining = ['source bookmark', 'archive bookmark', 'private recovery record']
    path = _records.publication_path(paths, project.name, entry.commit)
    try:
        _candidate.clean_inspect(paths, project.name, commit=entry.commit)
        if bookmark is not None and source_commit is not None:
            _source.remove_bookmark(bookmark, source_commit)
        completed.append(
            'source bookmark released' if bookmark is not None else 'no source bookmark recorded'
        )
        remaining.pop(0)
        _release_archive(paths, project.name, entry.commit)
        completed.append('archive bookmark released')
        remaining.pop(0)
        _records.mark_publication_deleted(paths, project.name, entry.commit)
        _filesystem.durable_unlink(path)
        completed.append('private recovery record removed')
    except (PubanonError, OSError, KeyboardInterrupt) as exc:
        error_type = (
            OperationCancelled
            if isinstance(exc, (KeyboardInterrupt, OperationCancelled))
            else PubanonError
        )
        if not path.exists():
            if 'private recovery record removed' not in completed:
                completed.append('private recovery record removed')
            raise error_type(
                'local preservation was removed; directory durability could not be confirmed',
                fact=f'Completed: {", ".join(completed)}. {escape(str(exc))}',
                recovery=(
                    'Check filesystem health. No further bookmark cleanup is needed; '
                    'an archived snapshot may no longer appear in local history.'
                ),
            ) from exc
        retry = shell_command('pubanon', 'delete', entry.commit, f'--project={project.name}')
        raise error_type(
            'local preservation cleanup is incomplete',
            fact=(
                f'Completed: {", ".join(completed) or "none"}. '
                f'Remaining: {", ".join(remaining)}. {escape(str(exc))}'
            ),
            recovery=retry
            or f'Select project {escape(project.name)}, then retry delete for this snapshot.',
        ) from exc


def inspect_publication(
    paths: Paths, project: str, entry: PublicationEntry, *, refresh: bool = False
) -> Path:
    """Materialize the selected saved commit without current candidate gates.

    A private record, when present, supplies the exact expected tree and
    content digest. Recordless publications can be inspected from their Git
    commit. Ordinary reuse verifies the commit-keyed copy and refuses local
    edits; `refresh` replaces the selected copy. Missing or conflicting saved
    objects are refused before replacement.

    The caller must hold the project lock for the complete operation.

    Returns:
        Path to the selected commit's read-only review directory.
    """
    if re.fullmatch(OID_HEX, entry.commit) is None:
        raise PubanonError('invalid published snapshot commit')
    store = _candidate.candidate_store_path(paths, project)
    if not store.exists():
        raise PubanonError(
            'published commit is unavailable', fact=entry.commit, recovery=_PUBLICATION_REPAIR
        )
    result = run_git(
        ['--git-dir', str(store.absolute()), 'cat-file', 'commit', entry.commit],
        cwd=store,
        check=False,
    )
    if result.returncode != 0:
        raise PubanonError(
            'published commit is unavailable', fact=entry.commit, recovery=_PUBLICATION_REPAIR
        )
    first_line = result.stdout.partition(b'\n')[0]
    if (
        not first_line.startswith(b'tree ')
        or re.fullmatch(OID_HEX.encode(), first_line[5:]) is None
    ):
        raise PubanonError('published commit has an invalid tree')
    if entry.record is not None and first_line[5:].decode('ascii') != entry.record.tree:
        raise PubanonError('published tree conflicts with its record')
    snapshot = _source.read_snapshot(store, entry.commit, git_dir=store)
    if (
        entry.record is not None
        and _source.snapshot_digest(snapshot) != entry.record.snapshot_digest
    ):
        raise PubanonError('published files conflict with their record')
    try:
        return _candidate.materialize_snapshot(
            paths, project, entry.commit, snapshot, refresh=refresh
        )
    except PubanonError as error:
        if error.recovery == 'pubanon inspect --refresh':
            error.recovery = f'pubanon inspect --published {entry.commit} --refresh'
        raise


def preserve_previous_tip(paths: Paths, project: str, candidate: CandidateRecord) -> None:
    """Retain the complete old history, including recordless commits, before transport."""
    if candidate.replaces is not None and candidate.prestate_main is not None:
        _archive_publication(paths, project, candidate.prestate_main)


def publications(
    paths: Paths, project: str, *, include_deleted: bool = False
) -> tuple[PublicationEntry, ...]:
    """List the union of confirmed records and operational published history.

    Candidate and repair references are never publication roots. A missing
    private record object remains listed with an unknown Git commit time.
    `include_deleted` exposes underlying history for publication operations,
    without restoring locally deleted entries to the snapshot list.

    Descendants precede ancestors. Independent ready entries use confirmation
    time, falling back to commit time, with deterministic object-ID
    tie-breaking. Invalid records or archive ownership fail the operation
    without a partial result.
    """
    records = {record.commit: record for record in _records.list_publications(paths, project)}
    main, current_tags = _candidate.published_refs(paths, project)
    store = _candidate.candidate_store_path(paths, project)
    ref_roots = set(current_tags.values())
    if main is not None:
        ref_roots.add(main)
    current = set(_rev_list(store, {main} if main else set(), parents=False))
    ref_roots.update(_archive_roots(paths, project))
    reachable = set(_rev_list(store, ref_roots, parents=False))
    available_records = {
        oid
        for oid in records
        if store.exists()
        and run_git(
            ['--git-dir', str(store.absolute()), 'cat-file', '-e', f'{oid}^{{commit}}'],
            cwd=store,
            check=False,
        ).returncode
        == 0
    }
    graph = _publication_graph(store, ref_roots | available_records)
    deleted: set[str] = set() if include_deleted else _records.deleted_publications(paths, project)
    known = (reachable - deleted) | records.keys()
    tags: dict[str, set[str]] = {oid: set() for oid in known}
    for tag, oid in current_tags.items():
        if _VERSION_TAG.fullmatch(tag) is not None and oid in tags:
            tags[oid].add(tag)
    for oid, record in records.items():
        if record.tag_intent is not None:
            tags[oid].add(record.tag_intent)
    entries = {
        oid: PublicationEntry(
            oid,
            tuple(sorted(tags[oid])),
            datetime.fromtimestamp(graph[oid][0], UTC) if oid in graph else None,
            records.get(oid),
            oid not in current,
        )
        for oid in known
    }
    return _ordered_entries(entries, graph)


def record_publication(
    paths: Paths, project: ProjectConfig, candidate: CandidateRecord
) -> PublicationRecord:
    """Save tip proof first, then every frozen publication before local ref finalization.

    The caller confirms the exact transaction or verifies durable prior proof.
    Existing records retain their first confirmation time and must match all
    provenance fields. Independent archive and record writes are retryable.
    The caller must hold the project lock for the complete operation.

    Returns:
        The record for `candidate.commit`, which can differ from the replayed
        transaction tip whose proof is saved first.
    """
    _candidate.verify_publication_objects(paths, project.name, candidate)
    commits = (candidate.commit, *(entry.new for entry in candidate.replayed))
    records: dict[str, PublicationRecord] = {}
    for commit in commits:
        existing = _records.load_publication(paths, project.name, commit)
        record = _candidate.publication_record(
            paths,
            project.name,
            candidate,
            commit,
            existing.confirmed_at if existing is not None else datetime.now(UTC),
        )
        if existing is not None and existing != record:
            raise PubanonError(
                'published snapshot record conflicts with snapshot provenance', fact=commit
            )
        _candidate.publication_archive(paths, project.name, commit)
        records[commit] = record
    tip = _records.candidate_tip(candidate)
    _archive_publication(paths, project.name, tip)
    _records.save_publication(paths, project.name, records[tip])
    for commit in commits:
        if commit != tip:
            _records.save_publication(paths, project.name, records[commit])
            _archive_publication(paths, project.name, commit)
    return records[candidate.commit]


def select_publication(
    paths: Paths, project: str, selector: str, *, include_deleted: bool = False
) -> PublicationEntry:
    """Resolve only known publication tags, IDs, or recorded main.

    `latest` selects recorded current main. Other selectors are supported
    release labels or hexadecimal commit prefixes. An operational tag target
    takes precedence over unrelated retained records carrying the same label.

    Raises:
        PubanonError: The selector is absent or ambiguous in local history.
    """
    entries = publications(paths, project, include_deleted=include_deleted)
    if selector == 'latest':
        main, _ = _candidate.published_refs(paths, project)
        if main is None:
            alternatives = ', '.join(
                entry.tags[0] if entry.tags else entry.commit[:12] for entry in entries
            )
            raise PubanonError(
                'latest published snapshot is unavailable without recorded main',
                recovery=f'Choose an explicit listed selector: {alternatives}'
                if alternatives
                else 'No published snapshots are recorded.',
            )
        match = next((entry for entry in entries if entry.commit == main), None)
        if match is not None:
            return match
        raise PubanonError(
            'latest published snapshot was deleted locally', recovery='pubanon list'
        )
    if _VERSION_TAG.fullmatch(selector) is not None:
        matches = [entry for entry in entries if selector in entry.tags]
        _, current_tags = _candidate.published_refs(paths, project)
        current = current_tags.get(selector)
        if current is not None:
            matches = [entry for entry in matches if entry.commit == current]
    elif _OID_PREFIX.fullmatch(selector) is not None:
        matches = [entry for entry in entries if entry.commit.startswith(selector.lower())]
    else:
        matches = []
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise PubanonError(
            'ambiguous published snapshot selector',
            fact=', '.join(
                f'{entry.commit} ({", ".join(entry.tags) or "untagged"})' for entry in matches
            ),
        )
    raise PubanonError('unknown published snapshot selector', fact=escape(selector))


def source_cleanup_guidance(project: ProjectConfig, entry: PublicationEntry) -> tuple[str, ...]:
    """Describe existing detached source worktrees without suggesting creation."""
    record = entry.record
    if record is None or record.source_bookmark is None:
        return ()
    source_commit = _publication_source(record)
    if source_commit is None:
        return ()
    recovery = _source.source_recovery(
        record.source_bookmark, source_commit, relocated_source=project.source_path
    )
    if recovery.source_path is None:
        return ()
    lines: list[str] = []
    # Git lists the primary worktree first; only linked worktrees can be removed.
    linked = list(_registered_worktrees(recovery.source_path).items())[1:]
    for location, state in linked:
        if state != (source_commit, True) or not location.is_dir():
            continue
        label = 'source input' if record.derivation is not None else 'source'
        lines.append(
            f'Preserved {label} worktree (can still retain objects): {escape(str(location))}'
        )
        command = shell_command(
            'git', '-C', str(recovery.source_path), 'worktree', 'remove', str(location)
        )
        lines.append(
            command or 'Source path cannot be displayed as a safe runnable removal command.'
        )
    return tuple(lines)


def source_guidance(
    paths: Paths, project: ProjectConfig, entry: PublicationEntry
) -> tuple[str, ...]:
    """Describe complete-source recovery and user-managed worktree commands."""
    record = entry.record
    if record is None:
        return ('Source: not recorded; the original source location cannot be verified.',)
    lines: tuple[str, ...] = ()
    if record.derivation is not None:
        derivation = record.derivation
        lines = (
            display_field('Original snapshot', derivation.original),
            display_field('Correction snapshot', derivation.correction),
            display_field('Source input', derivation.original_source or 'unknown'),
            'The published files may differ from this source input after replaying changes.',
        )
    source_commit = _publication_source(record)
    if source_commit is None:
        return (
            *lines,
            'Source recovery unavailable: original source input is unknown.',
        )
    if record.source_bookmark is None:
        return (
            *lines,
            'Source recovery unavailable: source input location was not preserved.',
        )
    recovery = _source.source_recovery(
        record.source_bookmark, source_commit, relocated_source=project.source_path
    )
    if recovery.status == 'unavailable' or recovery.source_path is None:
        return (
            *lines,
            f'Source unavailable: {escape(recovery.reason or "source repository is missing")}',
        )
    return (
        *lines,
        *_worktree_guidance(paths, project, record, recovery, source_commit),
    )
