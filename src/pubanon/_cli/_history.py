"""Present and remove local snapshot history."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from pubanon import (
    _config,
    _run,
)
from pubanon._cli import _context
from pubanon._errors import PubanonError
from pubanon._snapshots import _candidate, _history, _records, _source
from pubanon._terminal import (
    confirm,
    escape,
    heading,
    next_step,
    output,
    style,
)
from pubanon._terminal import (
    field as terminal_field,
)

if TYPE_CHECKING:
    import argparse


def _delete_publication(ctx: _context.ProjectContext, args: argparse.Namespace) -> None:
    """Confirm and delete the selected local publication."""
    entry = _history.select_publication(
        ctx.paths, ctx.project.name, args.selector, include_deleted=True
    )
    for line in _history.delete_guidance(ctx.paths, ctx.project, entry):
        label, separator, value = line.partition(':')
        print(f'{style(label + separator)}{value}' if separator else line)
    cleanup = _history.source_cleanup_guidance(ctx.project, entry)
    label = ', '.join(entry.tags) or entry.commit[:12]
    if not args.yes and not confirm(f'Delete local snapshot {label}?'):
        output('Nothing was deleted.')
        return
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
    source_commit = (
        entry.record.pinned.commit
        if entry.record.pinned is not None
        else entry.record.derivation.original_source
        if entry.record.derivation is not None
        else None
    )
    if source_commit is None:
        return 'unavailable'
    return _source.source_recovery(
        entry.record.source_bookmark,
        source_commit,
        relocated_source=project.source_path,
    ).status


def _history_entry(
    ctx: _context.ProjectContext,
    entry: _history.PublicationEntry,
    candidate: _candidate.CandidateRecord | None,
    *,
    verbose: bool,
) -> tuple[tuple[str, str, str, str], list[str]]:
    """Describe one published row and its optional provenance."""
    pending = (
        candidate is not None
        and entry.commit in {candidate.commit, *(item.new for item in candidate.replayed)}
        and candidate.state == _candidate.STATE_UNKNOWN_OUTCOME
    )
    snapshot = entry.commit if verbose else entry.commit[:12]
    state = 'published' if pending else 'superseded' if entry.replaced else 'published'
    published = (
        entry.record.confirmed_at.astimezone().isoformat(sep=' ', timespec='seconds')
        if entry.record is not None
        else '-'
    )
    details: list[str] = []
    if pending:
        details.append(terminal_field('Local recording', 'incomplete', kind='warning'))
    if verbose:
        details.extend(['', terminal_field('Snapshot', snapshot)])
        details.append(terminal_field('Source', _history_source_status(ctx.project, entry)))
        if entry.record is not None and entry.record.pinned is not None:
            details.append(terminal_field('Source commit', entry.record.pinned.commit))
        elif entry.record is not None and entry.record.derivation is not None:
            derivation = entry.record.derivation
            details.extend(
                [
                    terminal_field('Original snapshot', derivation.original),
                    terminal_field('Correction snapshot', derivation.correction),
                    terminal_field('Source input', derivation.original_source or 'unavailable'),
                ]
            )
    return (snapshot, escape(', '.join(entry.tags) or 'untagged'), state, published), details


def _pending_history(
    ctx: _context.ProjectContext,
    record: _candidate.CandidateRecord,
    entries: tuple[_history.PublicationEntry, ...],
    *,
    verbose: bool,
) -> tuple[list[tuple[str, str, str, str]], list[str], str]:
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
    details: list[str] = []
    for commit, intended, source, source_label in snapshots:
        if commit in published:
            continue
        version = (
            ', '.join(tag for tag, oid in tags.items() if oid == commit) or intended or 'untagged'
        )
        snapshot = commit if verbose else commit[:12]
        rows.append((snapshot, escape(version), state, '-'))
        if confirmed:
            details.append(terminal_field('Local recording', 'incomplete', kind='warning'))
        if verbose:
            details.extend(
                [
                    '',
                    terminal_field('Snapshot', snapshot),
                    terminal_field('Operation', 'replacement' if record.replaces else 'new'),
                ]
            )
            if source is not None:
                details.append(terminal_field(source_label, source))
    if unknown:
        if not confirmed:
            details.append('Pubanon could not confirm whether publication completed.')
        return rows, details, 'pubanon publish --resolve'
    if incomplete:
        details.append('Preparation stopped before the replacement was ready.')
        return rows, details, 'pubanon prepare --continue'
    return rows, details, 'pubanon inspect' if rows else ''


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


def _show_history_table(rows: list[tuple[str, str, str, str]], details: list[str]) -> None:
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
    recording_note = terminal_field('Local recording', 'incomplete', kind='warning')
    if recording_note in details:
        print(recording_note)
    for detail in details:
        if detail != recording_note:
            print(detail)


def _show_publication(
    ctx: _context.ProjectContext, entry: _history.PublicationEntry, *, verbose: bool = False
) -> None:
    """Show recorded provenance, bounded Git changes, and source recovery advice."""
    heading('Superseded snapshot' if entry.replaced else 'Published snapshot')
    print(terminal_field('Snapshot commit', entry.commit if verbose else entry.commit[:12]))
    version = ', '.join(entry.tags) or 'untagged'
    print(f'{style("Version:")} {escape(version)}')
    record = entry.record
    if record is not None:
        destination = record.destination
        print(
            terminal_field(
                'Destination', f'{escape(destination.owner)}/{escape(destination.repo)}'
            )
        )
        print(terminal_field('Visibility at publication', destination.visibility.upper()))
        confirmed_at = record.confirmed_at.astimezone().isoformat(sep=' ', timespec='seconds')
        print(terminal_field('Published', confirmed_at))
        if verbose and record.pinned is not None:
            print(terminal_field('Source commit', record.pinned.commit))
    elif entry.commit_time is not None:
        commit_time = entry.commit_time.astimezone().isoformat(sep=' ', timespec='seconds')
        print(f'{style("Commit time:")} {commit_time}')
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
        changes = summary.stdout.decode(errors='surrogateescape').strip() or 'No file changes'
        for line in changes.split('\n'):
            print(escape(line))
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
        print(terminal_field('Local store', escape(str(store))))
    if record and record.source_bookmark and source_lines[0].startswith('Source unavailable:'):
        print(
            f'{style("Recorded source repository:")} '
            f'{escape(str(record.source_bookmark.source_path))}'
        )
    for line in source_lines:
        if line.startswith('git '):
            label = 'Remove source worktree:' if ' worktree remove ' in line else 'View source:'
            print(f'{style(label)} {style(line, "choice")}')
        else:
            label, separator, value = line.partition(':')
            print(f'{style(label + separator)}{value}' if separator else line)
    print()
    next_step(
        f'pubanon inspect --published {entry.commit if verbose else entry.commit[:12]}'
        if available
        else 'pubanon repo verify --repair-store'
    )


def command_clean(args: argparse.Namespace) -> None:
    """Preview and delete superseded local snapshots under the project lock."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        entries = tuple(entry for entry in _history.publications(paths, name) if entry.replaced)
        if not entries:
            output('No superseded snapshots to clean.')
            return
        for entry in entries:
            _history.delete_guidance(paths, ctx.project, entry)
        heading('Superseded snapshots to remove')
        rows = [_history_entry(ctx, entry, None, verbose=args.verbose)[0] for entry in entries]
        _show_history_table(rows, [])
        print(
            'The listed snapshots, review directories, and source recovery information '
            'will be removed.'
        )
        print('Current published snapshots and prepared work are kept.')
        question = (
            'Delete this superseded local snapshot?'
            if len(entries) == 1
            else f'Delete these {len(entries)} superseded local snapshots?'
        )
        if not args.yes and not confirm(question):
            output('Nothing was deleted.')
            return
        for entry in entries:
            _history.delete_publication(paths, ctx.project, entry)
            output(f'Deleted local snapshot {entry.commit[:12]}.', kind='success')
        output('Superseded snapshot cleanup complete.', kind='success')


def command_delete(args: argparse.Namespace) -> None:
    """Confirm and delete one selected local snapshot."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        candidate = _selected_prepared(paths, name, args.selector)
        if candidate is not None:
            if candidate.state == _candidate.STATE_UNKNOWN_OUTCOME:
                raise PubanonError(
                    'Pubanon could not confirm whether the last publication completed',
                    recovery='pubanon publish --resolve',
                )
            tip = candidate.tip or candidate.commit
            print(terminal_field('Prepared snapshot', tip))
            print('The saved candidate and its review directories will be removed.')
            if not args.yes and not confirm(f'Delete local snapshot {tip[:12]}?'):
                output('Nothing was deleted.')
                return
            reviewed = dict.fromkeys(
                (candidate.commit, *(entry.new for entry in candidate.replayed), tip)
            )
            for commit in reviewed:
                _candidate.clean_inspect(paths, name, commit=commit)
            _candidate.discard(paths, name, force=False)
            output('Deleted the prepared snapshot and its review directories.', kind='success')
            return
        _delete_publication(ctx, args)


def command_discard(args: argparse.Namespace) -> None:
    """Discard one retained snapshot and report preserved snapshot directories."""
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
        heading('Preserved snapshot directories', separate=True)
        for copy in copies:
            print(f'  {escape(str(copy))}')
        next_step(_context.project_command(name, 'inspect', f'--project={name}', '--clean'))


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
        print(style('Snapshots', 'heading'))
        rows, details, action = (
            _pending_history(ctx, candidate, entries, verbose=args.verbose)
            if candidate
            else ([], [], '')
        )
        for entry in entries:
            row, provenance = _history_entry(ctx, entry, candidate, verbose=args.verbose)
            rows.append(row)
            details.extend(provenance)
        _show_history_table(rows, details)
        if args.verbose:
            print(terminal_field('Source repository', escape(str(ctx.project.source_path))))
            print(
                terminal_field(
                    'Local store', escape(str(_candidate.candidate_store_path(paths, name)))
                )
            )
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
        _show_publication(
            ctx, _history.select_publication(paths, name, args.selector), verbose=args.verbose
        )
