"""Present and remove local snapshot history."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from pubanon import (
    _config,
    _run,
)
from pubanon._cli import _context
from pubanon._errors import OperationCanceled, PubanonError
from pubanon._snapshots import _candidate, _history, _records, _source
from pubanon._terminal import (
    confirm,
    display_path,
    display_time,
    escape,
    fields,
    heading,
    next_step,
    output,
    style,
)

if TYPE_CHECKING:
    import argparse
    from collections.abc import Sequence


def _clean_summary(*, snapshots: bool, copies: bool) -> str:
    """Name only the local objects in one cleanup preview."""
    if snapshots and copies:
        return (
            'The listed snapshots, review copies, and source recovery information will be removed.'
        )
    if snapshots:
        return 'The listed snapshots and their source recovery information will be removed.'
    return 'The listed review copies will be removed.'


def _delete_publication(ctx: _context.ProjectContext, args: argparse.Namespace) -> None:
    """Confirm and delete the selected local publication."""
    entry = _history.select_publication(
        ctx.paths, ctx.project.name, args.selector, include_deleted=True
    )
    lines = _history.delete_guidance(ctx.paths, ctx.project, entry)
    fields([(label, value) for label, _, value in (line.partition(': ') for line in lines[:2])])
    for line in lines[2:]:
        print(line)
    cleanup = _history.source_cleanup_guidance(ctx.project, entry)
    label = ', '.join(entry.tags) or entry.commit[:12]
    if not args.yes and not confirm(f'Delete local snapshot {label}?'):
        raise OperationCanceled('Canceled. Nothing was deleted.')
    _history.delete_publication(ctx.paths, ctx.project, entry)
    output('Deleted the local snapshot and recovery information.', kind='success')
    for line in cleanup:
        print(line)


def _history_source_status(
    project: _config.ProjectConfig, entry: _history.PublicationEntry
) -> str:
    """Summarize source recovery without listing paths or commands."""
    if entry.record is None:
        return 'not recorded'
    if entry.record.source_bookmark is None:
        return 'unavailable'
    source_commit = _records.publication_source(entry.record)
    if source_commit is None:
        return 'unavailable'
    return _source.source_recovery(
        entry.record.source_bookmark,
        source_commit,
        relocated_source=project.source_path,
    ).status


def _matching_source_details(match: _source.SourceMatches | None) -> list[tuple[str, str]]:
    """Describe the latest exact content match and the number found."""
    if match is None:
        return [
            ('Latest match', 'unavailable'),
            ('Number of matches', 'unavailable'),
        ]
    return [
        (f'Latest match on {escape(match.branch)}', match.latest or 'none'),
        ('Number of matches', str(match.count)),
    ]


def _history_entry(
    ctx: _context.ProjectContext,
    entry: _history.PublicationEntry,
    candidate: _candidate.CandidateRecord | None,
    *,
    verbose: bool,
    match: _source.SourceMatches | None = None,
) -> tuple[tuple[str, str, str, str], list[str | tuple[str, str]]]:
    """Describe one published row and its optional provenance."""
    pending = (
        candidate is not None
        and entry.commit in {candidate.commit, *(item.new for item in candidate.replayed)}
        and candidate.state == _candidate.STATE_UNKNOWN_OUTCOME
    )
    snapshot = entry.commit if verbose else entry.commit[:12]
    state = 'published' if pending else 'superseded' if entry.replaced else 'published'
    published = display_time(entry.record.confirmed_at) if entry.record is not None else '-'
    details: list[str | tuple[str, str]] = []
    if pending:
        details.append(('Local recording', style('incomplete', 'warning')))
    if verbose:
        details.extend(['', ('Snapshot', snapshot)])
        details.append(('Source', _history_source_status(ctx.project, entry)))
        if entry.record is not None and entry.record.pinned is not None:
            details.append(('Original source', entry.record.pinned.commit))
            details.extend(_matching_source_details(match))
        elif entry.record is not None and entry.record.derivation is not None:
            derivation = entry.record.derivation
            details.extend(
                [
                    ('Original snapshot', derivation.original),
                    ('Correction snapshot', derivation.correction),
                    ('Source input', derivation.original_source or 'unavailable'),
                ]
            )
            details.extend(_matching_source_details(match))
    return (snapshot, escape(', '.join(entry.tags) or 'untagged'), state, published), details


def _pending_history(
    ctx: _context.ProjectContext,
    record: _candidate.CandidateRecord,
    entries: tuple[_history.PublicationEntry, ...],
    *,
    verbose: bool,
) -> tuple[list[tuple[str, str, str, str]], list[str | tuple[str, str]], str]:
    """List each retained snapshot under its own identity and return its recovery action."""
    progress = _records.load_rewrite(ctx.paths, ctx.project.name)
    incomplete = record.replaces is not None and (record.tip is None or progress is not None)
    unknown = record.state == _candidate.STATE_UNKNOWN_OUTCOME
    confirmed = unknown and _candidate.confirmed_publication(ctx.paths, ctx.project.name, record)
    state = (
        'published'
        if confirmed
        else 'unresolved'
        if unknown
        else 'incomplete'
        if incomplete
        else 'prepared'
    )
    replayed = record.replayed or (progress.replayed if progress is not None else ())
    rewritten = {record.replaces: record.commit, **{entry.old: entry.new for entry in replayed}}
    tags = (
        record.poststate_tags
        if record.poststate_tags is not None
        else {tag: rewritten.get(commit, commit) for tag, commit in record.prestate_tags.items()}
    )
    snapshots: list[tuple[str, str | None, str | None, str]] = [
        (record.commit, record.tag_intent, record.pinned.commit, 'Source commit')
    ]
    snapshots.extend(
        (entry.new, None, entry.original_source, 'Source input') for entry in replayed
    )
    published = {entry.commit for entry in entries}
    rows: list[tuple[str, str, str, str]] = []
    details: list[str | tuple[str, str]] = []
    for commit, intended, source, source_label in snapshots:
        if commit in published:
            continue
        version = (
            ', '.join(tag for tag, oid in tags.items() if oid == commit) or intended or 'untagged'
        )
        snapshot = commit if verbose else commit[:12]
        rows.append((snapshot, escape(version), state, '-'))
        if confirmed:
            details.append(('Local recording', style('incomplete', 'warning')))
        if verbose:
            details.extend(
                [
                    '',
                    ('Snapshot', snapshot),
                    ('Operation', 'replacement' if record.replaces else 'new'),
                ]
            )
            if source is not None:
                details.append((source_label, source))
    if unknown:
        if not confirmed:
            details.append('Pubanon could not confirm whether publication completed.')
        return rows, details, 'pubanon publish --resolve'
    if incomplete:
        details.append('Preparation stopped before the replacement was ready.')
        return rows, details, 'pubanon prepare --continue'
    return rows, details, 'pubanon inspect' if rows else ''


def _refresh_source_matches(
    ctx: _context.ProjectContext,
    entries: tuple[_history.PublicationEntry, ...],
    *,
    verbose: bool,
) -> dict[str, _source.SourceMatches]:
    """Read source history once for display and refresh opted-in source tags."""
    if not verbose and not ctx.project.source_tags:
        return {}
    try:
        matches = _history.source_matches(ctx.project, entries)
        for warning in _history.refresh_source_tags(ctx.project, entries, matches=matches):
            output(escape(warning), kind='warning')
    except OperationCanceled:
        raise
    except (OSError, PubanonError) as exc:
        output(f'Source matching unavailable: {escape(str(exc))}', kind='warning')
        return {}
    else:
        return matches


def _selected_prepared(
    paths: _config.Paths, name: str, selector: str
) -> _candidate.CandidateRecord | None:
    """Select a prepared candidate by its explicit name or unambiguous commit prefix."""
    candidate = _candidate.load_candidate(paths, name)
    if candidate is None:
        return None
    if selector == 'prepared':
        return candidate
    tip = candidate.tip or candidate.commit
    if (
        selector
        and all(character in '0123456789abcdefABCDEF' for character in selector)
        and (tip.startswith(selector.lower()) or candidate.commit.startswith(selector.lower()))
        and not any(
            entry.commit.startswith(selector.lower())
            for entry in _history.publications(paths, name)
        )
    ):
        return candidate
    return None


def _show_history_table(
    rows: list[tuple[str, str, str, str]], details: list[str | tuple[str, str]]
) -> None:
    """Align snapshot rows and append their recording and provenance details."""
    headers = ('ID', 'Version', 'State', 'Published')
    widths = [max(len(row[index]) for row in [headers, *rows]) for index in range(3)]
    print(
        style(
            '  '.join(value.ljust(width) for value, width in zip(headers, widths, strict=False))
            + '  Published'
        )
    )
    for snapshot, version, state, published in rows:
        state_kind = (
            'success'
            if state == 'published'
            else 'note'
            if state in {'prepared', 'superseded'}
            else 'warning'
        )
        print(
            f'{snapshot.ljust(widths[0])}  {version.ljust(widths[1])}  '
            f'{style(state.ljust(widths[2]), state_kind)}  {published}'
        )
    recording_note = ('Local recording', style('incomplete', 'warning'))
    if recording_note in details:
        fields([recording_note])
    detail_rows: list[tuple[str, str]] = []
    for detail in details:
        if detail == recording_note:
            continue
        if isinstance(detail, tuple):
            detail_rows.append(detail)
        else:
            fields(detail_rows)
            detail_rows.clear()
            print(detail)
    fields(detail_rows)


def _show_source_lines(lines: Sequence[str], rows: list[tuple[str, str]]) -> None:
    """Align source recovery fields while preserving complete runnable commands."""
    for line in lines:
        if line.startswith('git '):
            label = 'Remove source worktree' if ' worktree remove ' in line else 'View source'
            rows.append((label, style(line, 'choice')))
        else:
            label, separator, value = line.partition(':')
            if separator:
                rows.append((label, value.lstrip()))
            else:
                fields(rows)
                rows = []
                print(line)
    fields(rows)


def _show_publication(
    ctx: _context.ProjectContext,
    entry: _history.PublicationEntry,
    *,
    verbose: bool = False,
    match: _source.SourceMatches | None = None,
) -> None:
    """Show recorded provenance, bounded Git changes, and source recovery advice."""
    heading('Superseded snapshot' if entry.replaced else 'Published snapshot')
    rows = [
        ('Snapshot commit', entry.commit if verbose else entry.commit[:12]),
        ('Version', escape(', '.join(entry.tags) or 'untagged')),
    ]
    record = entry.record
    if record is not None:
        destination = record.destination
        rows.extend(
            [
                ('Destination', f'{escape(destination.owner)}/{escape(destination.repo)}'),
                ('Visibility at publication', destination.visibility),
                ('Published', display_time(record.confirmed_at)),
            ]
        )
        if verbose and record.pinned is not None:
            rows.append(('Original source', record.pinned.commit))
    elif entry.commit_time is not None:
        rows.append(('Commit time', display_time(entry.commit_time)))
    fields(rows)
    store = _candidate.candidate_store_path(ctx.paths, ctx.project.name)
    summary = (
        _run.run_git(
            [
                'diff-tree',
                '--no-commit-id',
                '-r',
                '--root',
                '--no-renames',
                '--stat',
                '--stat-count=20',
                entry.commit,
            ],
            cwd=store,
            check=False,
        )
        if store.exists()
        else None
    )
    available = summary is not None and summary.returncode == 0
    if available and summary is not None:
        heading('Changes', separate=True)
        changes = summary.stdout.decode(errors='surrogateescape').rstrip() or ' No file changes'
        for line in changes.split('\n'):
            print('  ' + escape(line.removeprefix(' ')))
    else:
        output(
            'Change summary and published inspection unavailable: local Git objects are missing',
            kind='warning',
        )
        output(
            'Restore the local store from a backup, or recover it from GitHub '
            'if the snapshot is still there.'
        )
    source_lines = (
        _history.source_guidance(ctx.paths, ctx.project, entry)
        if verbose
        else [f'Source: {_history_source_status(ctx.project, entry)}']
    )
    if verbose:
        fields(
            [
                (f'Latest match on {escape(match.branch)}', match.latest or 'none')
                if match is not None
                else ('Latest match', 'unavailable'),
                ('Number of matches', str(match.count) if match is not None else 'unavailable'),
                ('Local store', display_path(store)),
            ]
        )
        print()
    source_rows: list[tuple[str, str]] = []
    if record and record.source_bookmark and source_lines[0].startswith('Source unavailable:'):
        source_rows.append(
            ('Recorded source repository', display_path(record.source_bookmark.source_path))
        )
    _show_source_lines(source_lines, source_rows)
    print()
    next_step(
        f'pubanon inspect --published {entry.commit if verbose else entry.commit[:12]}'
        if available
        else 'pubanon repo restore --to local'
    )


def command_clean(args: argparse.Namespace) -> None:
    """Preview and remove superseded snapshots and review copies."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        entries = tuple(entry for entry in _history.publications(paths, name) if entry.replaced)
        copies = _candidate.inspection_paths(paths, name)
        if not entries and not copies:
            output('Nothing to remove.')
            return
        for entry in entries:
            _history.delete_guidance(paths, ctx.project, entry)
        if entries:
            heading('Superseded snapshots to remove')
            rows = [_history_entry(ctx, entry, None, verbose=args.verbose)[0] for entry in entries]
            _show_history_table(rows, [])
        if copies:
            heading('Review copies to remove', separate=bool(entries))
            for copy in copies:
                print(f'  {display_path(copy)}')
        print(_clean_summary(snapshots=bool(entries), copies=bool(copies)))
        print('Current published snapshots and prepared work are kept.')
        if entries:
            question = (
                'Delete this superseded local snapshot?'
                if len(entries) == 1
                else f'Delete these {len(entries)} superseded local snapshots?'
            )
        else:
            question = (
                'Remove this review copy?' if len(copies) == 1 else 'Remove these review copies?'
            )
        if not args.yes and not confirm(question):
            raise OperationCanceled('Canceled. Nothing was deleted.')
        for entry in entries:
            _history.delete_publication(paths, ctx.project, entry)
            output(f'Deleted local snapshot {entry.commit[:12]}.', kind='success')
        _candidate.clean_inspect(paths, name)
        output('Cleanup complete.', kind='success')


def command_delete(args: argparse.Namespace) -> None:
    """Confirm and delete one selected local snapshot."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        if args.selector == 'prepared':
            raise PubanonError(
                'Prepared snapshots are discarded with pubanon discard',
                recovery='pubanon discard',
            )
        ctx = _context.load_context(paths, name)
        _delete_publication(ctx, args)


def command_discard(args: argparse.Namespace) -> None:
    """Discard one retained snapshot and its review copies."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        removed = _candidate.discard(paths, name, force=args.force)
        copies = _candidate.inspection_paths(paths, name)
    if removed:
        output('Discarded the prepared snapshot.', kind='success')
    else:
        output('No prepared snapshot was found.', kind='warning')
    if copies:
        heading('Remaining review copies', separate=True)
        for copy in copies:
            print(f'  {display_path(copy)}')
        next_step(_context.project_command(name, 'clean', f'--project={name}'))


def command_list(args: argparse.Namespace) -> None:
    """List locally known snapshots."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        entries = _history.publications(paths, name)
        candidate = _candidate.load_candidate(paths, name)
        if not entries and candidate is None:
            output('No recorded snapshot history.')
            print()
            next_step('pubanon prepare')
            return
        matches = _refresh_source_matches(ctx, entries, verbose=args.verbose)
        print(style('Snapshots', 'heading'))
        rows, details, action = (
            _pending_history(ctx, candidate, entries, verbose=args.verbose)
            if candidate
            else ([], [], '')
        )
        for entry in entries:
            row, provenance = _history_entry(
                ctx, entry, candidate, verbose=args.verbose, match=matches.get(entry.commit)
            )
            rows.append(row)
            details.extend(provenance)
        if args.verbose:
            details.extend(
                [
                    ('Source repository', display_path(ctx.project.source_path)),
                    ('Local store', display_path(_candidate.candidate_store_path(paths, name))),
                ]
            )
        _show_history_table(rows, details)
        if not action:
            main, _tags = _context.local_refs(ctx)
            selector = (
                'latest'
                if any(entry.commit == main for entry in entries)
                else entries[0].commit[:12]
            )
            action = _context.project_command(name, 'show', f'--project={name}', selector)
        print()
        next_step(action)


def command_show(args: argparse.Namespace) -> None:
    """Show one selected prepared or published snapshot."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        candidate = _selected_prepared(paths, name, args.selector)
        if candidate is not None:
            heading('Prepared snapshot')
            print(
                _candidate.inspect_report(
                    paths, candidate, ctx.global_config, ctx.project, verbose=args.verbose
                )
            )
            print()
            next_step('pubanon inspect')
            return
        entries = _history.publications(paths, name)
        entry = _history.select_publication(paths, name, args.selector, entries=entries)
        if not ctx.project.source_tags:
            entries = (entry,)
        matches = _refresh_source_matches(ctx, entries, verbose=args.verbose)
        _show_publication(ctx, entry, verbose=args.verbose, match=matches.get(entry.commit))
