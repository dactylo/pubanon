"""Discard a retained candidate and its rewrite state."""

from pubanon import _filesystem
from pubanon._config import (
    Paths,
)
from pubanon._errors import PubanonError
from pubanon._run import git_failure_fact, run_git
from pubanon._snapshots import _candidate, _inspection, _records, _store, _verification
from pubanon._snapshots._records import (
    STATE_UNKNOWN_OUTCOME,
    CandidateRecord,
    load_candidate,
)
from pubanon._snapshots._source import (
    remove_bookmark,
)
from pubanon._terminal import (
    escape,
    output,
    path_text,
)


def _discard_rewrite(
    paths: Paths, project: str, entries: tuple[_records.ReplayEntry, ...]
) -> bool:
    """Release unfinished replay artifacts, retaining records on cleanup failure."""
    directory = _records.rewrite_directory(paths, project)
    present = directory.exists() or _records.rewrite_path(paths, project).exists()
    if directory.is_symlink():
        raise PubanonError('cannot discard rewrite directory through a symlink')
    try:
        _filesystem.remove_tree(directory)
    except OSError as exc:
        raise PubanonError('cannot discard rewrite directory; snapshot retained') from exc
    for entry in entries:
        if entry.source_bookmark is not None and entry.original_source is not None:
            remove_bookmark(entry.source_bookmark, entry.original_source)
    store = _store.candidate_store_path(paths, project)
    if (
        store.exists()
        and run_git(
            [
                '--git-dir',
                str(store.absolute()),
                'show-ref',
                '--verify',
                '--quiet',
                _candidate.REWRITE_MERGED_REF,
            ],
            cwd=store,
            check=False,
        ).returncode
        == 0
    ):
        old = _candidate.read_ref(store, _candidate.REWRITE_MERGED_REF)
        if old is not None:
            _candidate.git_result(
                ['update-ref', '--no-deref', '-d', _candidate.REWRITE_MERGED_REF, old],
                store=store,
                failure='cannot discard rewrite ref',
            )
            present = True
    return present


def _discard_source_bookmark(record: CandidateRecord | None) -> None:
    """Release the candidate bookmark after rewrite directory cleanup."""
    if record is not None and record.source_bookmark is not None:
        try:
            remove_bookmark(record.source_bookmark, record.pinned.commit)
        except PubanonError as exc:
            raise PubanonError(
                'cannot discard source bookmark; snapshot retained',
                fact=f'{escape(path_text(record.source_bookmark.source_path))}: {exc.render()}',
                recovery='Restore access to the recorded source repository or correct '
                'the conflicting bookmark, then retry discard.',
            ) from exc
        output(
            "Removed the snapshot's private source bookmark. "
            'Branches, tags, and files were not changed.'
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
            fact=f'{escape(path_text(_records.candidate_path(paths, project)))}: {exc.render()}',
        ) from exc
    try:
        _records.load_rewrite(paths, project)
    except PubanonError as exc:
        raise PubanonError(
            str(exc),
            fact=f'{escape(path_text(_records.rewrite_path(paths, project)))}: {exc.render()}',
        ) from exc
    _candidate.require_known_outcome(record)


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


def _discard_preflight(paths: Paths, project: str, *, force: bool) -> CandidateRecord | None:
    """Refuse confirmed or protected candidates before any cleanup."""
    record = _candidate_for_discard(paths, project, force=force)
    if record is not None:
        try:
            confirmed = _verification.confirmed_publication(paths, project, record)
        except PubanonError:
            store = _store.candidate_store_path(paths, project)
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
        if _records.load_publication(
            paths, project, entry.new
        ) is not None or _candidate.publication_archive(paths, project, entry.new):
            raise PubanonError(
                'confirmed publication cannot be discarded', recovery='pubanon publish --resolve'
            )
    return entries


def discard(paths: Paths, project: str, *, force: bool) -> bool:
    """Discard one candidate and its review copies.

    The caller must hold the project lock for the complete operation.

    Args:
        paths: Derived pubanon paths.
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
        _inspection.clean_inspect(paths, project, commit=commit)
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
    store = _store.candidate_store_path(paths, project)
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
                    _candidate.CANDIDATE_REF,
                ],
                cwd=store,
                check=False,
            ).returncode
            == 0
        )
        old = _candidate.read_ref(store, _candidate.CANDIDATE_REF) if ref_present else None
        args = ['update-ref', '--no-deref', '-d', _candidate.CANDIDATE_REF]
        if old is not None:
            args.append(old)
        result = run_git(['--git-dir', str(store.absolute()), *args], cwd=store, check=False)
        if result.returncode != 0:
            if not force:
                raise PubanonError('cannot discard snapshot ref', fact=git_failure_fact(result))
            leftovers.append(f'saved snapshot reference: {git_failure_fact(result)}')
    candidate_record = _records.candidate_path(paths, project)
    record_present = candidate_record.exists()
    try:
        _records.rewrite_path(paths, project).unlink(missing_ok=True)
        candidate_record.unlink(missing_ok=True)
    except OSError as exc:
        raise PubanonError('cannot discard snapshot record') from exc
    if leftovers:
        raise PubanonError(
            'snapshot record discarded; local cleanup is incomplete',
            fact=', '.join(leftovers),
            recovery='pubanon repo restore --to local',
        )
    return ref_present or record_present or rewrite_present
