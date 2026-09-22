"""Resume frozen publication-history replay without changing published refs."""

import re
import shutil
import time
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Literal

from pubanon._config import GlobalConfig, Paths, ProjectConfig, fingerprint
from pubanon._errors import PubanonError
from pubanon._run import OID_HEX, run_git
from pubanon._snapshots import _candidate, _conflicts, _history, _records
from pubanon._snapshots._records import (
    CandidateRecord,
    ReplayEntry,
    Resolution,
    RewritePause,
    RewriteRecord,
)


def _commit_replay(store: Path, g: GlobalConfig, old: str, base: str, tree: str) -> str:
    """Create a commit even when replay produced no file changes."""
    raw = run_git(
        ['--git-dir', str(store.absolute()), 'cat-file', 'commit', old], cwd=store
    ).stdout
    headers, _, message = raw.partition(b'\n\n')
    author = next(line for line in headers.splitlines() if line.startswith(b'author '))
    date = b' '.join(author.rsplit(b' ', 2)[-2:]).decode('ascii')
    env = {
        'GIT_AUTHOR_NAME': g.anon_name,
        'GIT_AUTHOR_EMAIL': g.anon_email,
        'GIT_AUTHOR_DATE': date,
        'GIT_COMMITTER_NAME': g.anon_name,
        'GIT_COMMITTER_EMAIL': g.anon_email,
        'GIT_COMMITTER_DATE': f'{int(time.time())} +0000',
    }
    result = run_git(
        ['--git-dir', str(store.absolute()), 'commit-tree', tree, '-p', base],
        cwd=store,
        input_bytes=message,
        env_extra=env,
    )
    commit = result.stdout.strip().decode('ascii')
    if re.fullmatch(OID_HEX, commit) is None:
        raise PubanonError('Git returned an invalid replay commit ID')
    return commit


def _complete(record: CandidateRecord, progress: RewriteRecord) -> CandidateRecord:
    """Derive the final candidate only from frozen tags and the exact saved mapping."""
    mapping = {progress.replaces: progress.correction}
    mapping.update((entry.old, entry.new) for entry in progress.replayed)
    tags = {tag: mapping.get(oid, oid) for tag, oid in record.prestate_tags.items()}
    if record.tag_intent is not None and progress.replaces not in record.prestate_tags.values():
        tags[record.tag_intent] = record.commit
    return replace(
        record,
        tip=progress.replayed[-1].new if progress.replayed else record.commit,
        poststate_tags=tags,
        replayed=progress.replayed,
    )


def _merge(store: Path, progress: RewriteRecord) -> _conflicts.MergeResult:
    """Merge exactly the next frozen descendant onto the saved new base."""
    index = len(progress.replayed)
    old = progress.descendants[index]
    parent = progress.replaces if index == 0 else progress.descendants[index - 1]
    base = progress.replayed[-1].new if progress.replayed else progress.correction
    return _conflicts.parse_merge_result(
        run_git(
            [
                '--git-dir',
                str(store.absolute()),
                'merge-tree',
                '--write-tree',
                '--merge-base',
                parent,
                '-z',
                '--messages',
                base,
                old,
            ],
            cwd=store,
            check=False,
        )
    )


def _read_ref(store: Path, ref: str) -> str | None:
    """Read one optional direct retention ref."""
    result = run_git(
        ['--git-dir', str(store.absolute()), 'for-each-ref', '--format=%(objectname)', ref],
        cwd=store,
    )
    return result.stdout.strip().decode('ascii') or None


def _clear_pause(paths: Paths, project: str, store: Path) -> None:
    """Remove completed pause artifacts while keeping replay evidence for retry."""
    directory = _records.rewrite_directory(paths, project)
    try:
        if directory.is_symlink() or directory.is_junction():
            raise PubanonError('cannot clean rewrite directory through a symlink')
        if directory.exists():
            shutil.rmtree(directory)
    except OSError as exc:
        raise PubanonError('cannot clean completed rewrite export') from exc
    old = _read_ref(store, _candidate.REWRITE_MERGED_REF)
    if old is not None:
        run_git(
            [
                '--git-dir',
                str(store.absolute()),
                'update-ref',
                '--no-deref',
                '-d',
                _candidate.REWRITE_MERGED_REF,
                old,
            ],
            cwd=store,
        )


def _source_entry(
    paths: Paths, project: str, old: str, new: str, resolutions: tuple[Resolution, ...]
) -> ReplayEntry:
    """Capture available old provenance without consulting current source settings."""
    publication = _records.load_publication(paths, project, old)
    source = None
    bookmark = None
    if publication is not None:
        if publication.pinned is not None:
            source = publication.pinned.commit
        elif publication.derivation is not None:
            source = publication.derivation.original_source
        bookmark = publication.source_bookmark
    return ReplayEntry(old, new, source, bookmark, resolutions=resolutions)


def _update_ref(store: Path, ref: str, new: str, old: str | None) -> None:
    """Install a retention ref only if its observed old value remains unchanged."""
    run_git(
        [
            '--git-dir',
            str(store.absolute()),
            'update-ref',
            '--no-deref',
            ref,
            new,
            old or '0' * len(new),
        ],
        cwd=store,
    )


def _retain_pause(store: Path, progress: RewriteRecord) -> None:
    """Restore the exact pending tree if its ref was not installed before interruption."""
    pause = progress.pause
    if pause is None:
        raise PubanonError('rewrite pause is missing')
    current = _read_ref(store, _candidate.REWRITE_MERGED_REF)
    if current is not None and current != pause.tree:
        raise PubanonError('rewrite merged-tree ref conflicts with saved pause')
    if current is None:
        if (
            run_git(
                ['--git-dir', str(store.absolute()), 'cat-file', '-e', pause.tree],
                cwd=store,
                check=False,
            ).returncode
            != 0
        ):
            merged = _merge(store, progress)
            if (
                not merged.conflicted
                or merged.tree != pause.tree
                or _conflicts.expand_conflicts(store, merged) != pause.conflicts
            ):
                raise PubanonError('saved rewrite pause cannot be reconstructed')
        _update_ref(store, _candidate.REWRITE_MERGED_REF, pause.tree, None)
    merged = _conflicts.MergeResult(pause.tree, conflicted=True, conflicts=pause.conflicts)
    if _conflicts.expand_conflicts(store, merged) != pause.conflicts:
        raise PubanonError('saved rewrite decision paths changed')


def _replay_loop(
    paths: Paths, g: GlobalConfig, p: ProjectConfig, progress: RewriteRecord
) -> RewriteRecord:
    """Append durable completed steps until the chain finishes or exports a pause."""
    store = _candidate.candidate_store_path(paths, p.name)
    while len(progress.replayed) < len(progress.descendants):
        resolutions: tuple[Resolution, ...] = ()
        tree: str | None = None
        if progress.phase == 'replaying':
            _clear_pause(paths, p.name, store)
            merged = _merge(store, progress)
            tree = merged.tree
            if merged.conflicted:
                pause = RewritePause(
                    progress.descendants[len(progress.replayed)],
                    tree,
                    _conflicts.expand_conflicts(store, merged),
                )
                progress = replace(progress, phase='exporting', pause=pause)
                _records.save_rewrite(paths, p.name, progress)
        if progress.phase in {'exporting', 'awaiting'}:
            _retain_pause(store, progress)
            if progress.pause is None:
                raise PubanonError('rewrite pause is missing')
            directory = _records.rewrite_directory(paths, p.name)
            if progress.phase == 'exporting':
                _conflicts.export_conflicts(store, directory, progress.pause)
                _records.save_rewrite(paths, p.name, replace(progress, phase='awaiting'))
                return progress
            tree, resolutions = _conflicts.read_resolutions(store, directory, progress.pause)
        if tree is None:
            raise PubanonError('rewrite phase did not produce a tree')
        old = progress.descendants[len(progress.replayed)]
        base = progress.replayed[-1].new if progress.replayed else progress.correction
        new = _commit_replay(store, g, old, base, tree)
        entry = _source_entry(paths, p.name, old, new, resolutions)
        _update_ref(store, _candidate.CANDIDATE_REF, new, base)
        progress = replace(
            progress, replayed=(*progress.replayed, entry), phase='replaying', pause=None
        )
        _records.save_rewrite(paths, p.name, progress)
        _clear_pause(paths, p.name, store)
    return progress


def continue_rewrite(
    paths: Paths, g: GlobalConfig, p: ProjectConfig, *, env: Mapping[str, str]
) -> CandidateRecord:
    """Resume retained replacement progress, returning a complete candidate or explicit pause.

    The caller holds the project lock. An incomplete returned candidate has a
    durable awaiting-resolution record and export. Calling while awaiting
    validates user decisions; calling while exporting only restores the export.
    No source lookup or host operation is performed.
    """
    record = _records.load_candidate(paths, p.name)
    progress = _records.load_rewrite(paths, p.name)
    if record is None or record.replaces is None:
        raise PubanonError('no rewrite is in progress')
    if fingerprint(g, p) != record.fingerprint:
        raise PubanonError(
            'publication configuration changed after prepare',
            recovery='pubanon discard, then pubanon prepare',
        )
    store = _candidate.candidate_store_path(paths, p.name)
    if record.tip is not None:
        if progress is None:
            raise PubanonError('no rewrite is in progress')
        if (
            progress.correction != record.commit
            or progress.replaces != record.replaces
            or progress.prestate_main != record.prestate_main
            or progress.replayed != record.replayed
            or progress.descendants != tuple(entry.old for entry in record.replayed)
        ):
            raise PubanonError('rewrite record conflicts with completed snapshot')
        _candidate.verify_retained_candidate(paths, g, p, record, env=env)
        _clear_pause(paths, p.name, store)
        _records.rewrite_path(paths, p.name).unlink()
        return record
    if progress is None:
        if record.prestate_main is None:
            raise PubanonError('rewrite publication base is missing')
        descendants = _candidate.replacement_descendants(
            store, record.replaces, record.prestate_main
        )
        progress = RewriteRecord(
            record.commit, record.replaces, record.prestate_main, descendants, 'replaying'
        )
        _records.save_rewrite(paths, p.name, progress)
    anchor = _candidate.verify_rewrite_progress(paths, p.name, record, progress)
    base = progress.replayed[-1].new if progress.replayed else record.commit
    if anchor != base:
        _update_ref(store, _candidate.CANDIDATE_REF, base, anchor)
    progress = _replay_loop(paths, g, p, progress)
    if progress.phase == 'exporting':
        return record
    _clear_pause(paths, p.name, store)
    complete = _complete(record, progress)
    _records.save_candidate(paths, p.name, complete)
    _candidate.verify_retained_candidate(paths, g, p, complete, env=env)
    _records.rewrite_path(paths, p.name).unlink()
    return complete


def resolve_replacement(
    paths: Paths, project: str, selector: str
) -> tuple[str, Literal['one', 'all']]:
    """Resolve the local history grammar and require current main ancestry.

    `all` selects current main and requests a fresh root; every other selector
    preserves the exact selected publication for a one-publication correction.
    """
    main, _ = _candidate.published_refs(paths, project)
    if main is None:
        raise PubanonError('nothing to replace; this would be the first publication')
    selected = (
        main
        if selector == 'all'
        else _history.select_publication(paths, project, selector, include_deleted=True).commit
    )
    _candidate.replacement_descendants(
        _candidate.candidate_store_path(paths, project), selected, main
    )
    return selected, 'all' if selector == 'all' else 'one'
