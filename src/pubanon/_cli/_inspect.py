"""Materialize and present snapshot inspections."""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING

from pubanon import (
    _config,
)
from pubanon._cli import _context
from pubanon._errors import PubanonError
from pubanon._snapshots import _candidate, _history, _records
from pubanon._terminal import (
    escape,
    guidance,
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
    from pathlib import Path


def _inspection_details(
    selected: Path,
    copies: tuple[Path, ...],
    *,
    verbose: bool,
    snapshot: str | None = None,
) -> str:
    """Describe verified read-only snapshot files and optional review directories."""
    lines: list[str] = []
    if snapshot is not None:
        lines.extend(['  ' + terminal_field('Snapshot', snapshot), ''])
    lines.extend([style('Snapshot directory', 'heading'), f'  {escape(str(selected))}'])
    if verbose:
        older = [copy for copy in copies if copy != selected]
        if older:
            lines.extend(['', style('Directories from earlier snapshots', 'heading')])
            lines.extend(f'  {escape(str(copy))}' for copy in older)
        lines.append(guidance('Remove review directories with `pubanon inspect --clean`.'))
    return '\n'.join(lines)


def _inspect_rewritten_copy(
    ctx: _context.ProjectContext,
    record: _candidate.CandidateRecord,
    selector: str,
    *,
    refresh: bool,
    verbose: bool = False,
) -> None:
    """Show the exact copy targeted by refresh before materializing it."""
    commit = _candidate.select_rewritten_commit(record, selector)
    selected_path = ctx.paths.data_dir / 'projects' / ctx.project.name / 'inspections' / commit
    copies = _candidate.inspection_paths(ctx.paths, ctx.project.name)
    if refresh:
        print(terminal_field('Replacing snapshot directory', escape(str(selected_path))))
        output('All current contents of this directory will be replaced.', kind='warning')
        sys.stdout.flush()
    selected = _candidate.inspect_rewritten(
        ctx.paths, ctx.project.name, record, selector, refresh=refresh
    )
    print(
        _candidate.inspect_report(
            ctx.paths, record, ctx.global_config, ctx.project, verbose=verbose
        )
    )
    print(
        '\n'
        + _inspection_details(
            selected,
            copies,
            verbose=verbose,
            snapshot=commit if verbose else commit[:12],
        )
    )
    print()
    next_step(_candidate.inspect_next_action(record, ctx.global_config, ctx.project))


def _inspection_listing(paths: _config.Paths, name: str) -> list[str]:
    """Describe preserved snapshot directories independently of snapshot state."""
    copies = _candidate.inspection_paths(paths, name)
    if not copies:
        return []
    return [
        f'{style("Preserved snapshot directories:")} {len(copies)}',
        *(f'  {escape(str(copy))}' for copy in copies),
        'Open these directories in your editor to review their files.',
        guidance('Remove review directories with `pubanon inspect --clean`.'),
    ]


def _inspect_without_candidate(paths: _config.Paths, name: str, *, refresh: bool) -> None:
    """Report preserved copies or explain why materialization cannot proceed."""
    copies = _inspection_listing(paths, name)
    if copies and not refresh:
        output('No prepared snapshot is available.')
        for line in copies:
            output(line)
        return
    raise PubanonError('no prepared snapshot is retained', recovery='pubanon prepare')


def inspection_copy(
    ctx: _context.ProjectContext,
    record: _candidate.CandidateRecord,
    *,
    refresh: bool = False,
    workflow: bool = False,
    verbose: bool = False,
) -> Path:
    """Materialize a selected copy and describe its deliberate retention lifecycle.

    Args:
        ctx: Project context for the retained snapshot.
        record: Candidate to materialize under its snapshot identity.
        refresh: Replace all contents of an existing inspection directory.
        workflow: Omit the candidate report and next action handled by the
            workflow.
        verbose: Include directories preserved from earlier snapshots.

    Returns:
        The inspection directory, retained until explicitly cleaned or
        refreshed.
    """
    selected = (
        ctx.paths.data_dir
        / 'projects'
        / ctx.project.name
        / 'inspections'
        / _records.candidate_tip(record)
    )
    copies = _candidate.inspection_paths(ctx.paths, ctx.project.name)
    if refresh:
        print(f'{style("Replacing snapshot directory:")} {escape(str(selected))}')
        print(
            style('All current contents of this directory will be replaced.', 'warning'),
            flush=True,
        )
    inspection = _candidate.materialize_inspect(
        ctx.paths, ctx.project.name, record, refresh=refresh
    )
    details = _inspection_details(inspection, copies, verbose=verbose)
    if not workflow:
        print(
            _candidate.inspect_report(
                ctx.paths, record, ctx.global_config, ctx.project, verbose=verbose
            )
        )
    print('\n' + details)
    print()
    if not workflow:
        next_step(_candidate.inspect_next_action(record, ctx.global_config, ctx.project))
    return inspection


def _inspect_published(
    ctx: _context.ProjectContext, selector: str, *, refresh: bool, verbose: bool = False
) -> None:
    """Inspect the verified read-only files of one saved publication."""
    entry = _history.select_publication(ctx.paths, ctx.project.name, selector)
    selected = ctx.paths.data_dir / 'projects' / ctx.project.name / 'inspections' / entry.commit
    copies = _candidate.inspection_paths(ctx.paths, ctx.project.name)
    if refresh:
        print(f'{style("Replacing snapshot directory:")} {escape(str(selected))}')
        output('All current contents of this directory will be replaced.', kind='warning')
        sys.stdout.flush()
    inspection = _history.inspect_publication(ctx.paths, ctx.project.name, entry, refresh=refresh)
    identity = escape(', '.join(entry.tags) or 'untagged')
    snapshot_id = entry.commit if verbose else entry.commit[:12]
    heading('Published snapshot', separate=refresh)
    print(terminal_field('Snapshot', snapshot_id))
    print(terminal_field('Version', identity))
    print('\n' + _inspection_details(inspection, copies, verbose=verbose))
    print()
    next_step(
        _context.project_command(
            ctx.project.name,
            'show',
            f'--project={ctx.project.name}',
            entry.commit if verbose else entry.commit[:12],
        )
    )


def command_inspect(args: argparse.Namespace) -> None:
    """Materialize, report, or clean one retained snapshot inspection."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    if args.rewritten is not None and (args.published is not None or args.clean):
        raise PubanonError('--rewritten cannot be combined with --published or --clean')
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        if args.published is not None:
            _inspect_published(ctx, args.published, refresh=args.refresh, verbose=args.verbose)
            return
        if args.clean:
            copies = _candidate.inspection_paths(paths, name)
            if copies:
                heading('Removing snapshot directories')
                for copy in copies:
                    print(f'  {escape(str(copy))}')
                output(
                    'All listed directories and their contents will be removed.', kind='warning'
                )
                sys.stdout.flush()
            removed = _candidate.clean_inspect(paths, name)
            if removed:
                output('Removed the listed snapshot directories.', kind='success')
            else:
                output('No snapshot directories to remove.', kind='note')
            if _candidate.record_path(paths, name).exists():
                print('The prepared snapshot is still available.')
            return
        record = _candidate.load_candidate(paths, name)
        if record is None:
            _inspect_without_candidate(
                paths, name, refresh=args.refresh or args.rewritten is not None
            )
            return
        if args.rewritten is not None:
            _inspect_rewritten_copy(
                ctx, record, args.rewritten, refresh=args.refresh, verbose=args.verbose
            )
            return
        inspection_copy(ctx, record, refresh=args.refresh, verbose=args.verbose)


def inspection_status(paths: _config.Paths, name: str) -> list[str]:
    """Summarize preserved copies and point to their detailed listing."""
    copies = _candidate.inspection_paths(paths, name)
    if not copies:
        return []
    command = style(guidance('pubanon inspect --verbose'), 'choice')
    return [
        terminal_field('Snapshot directories', str(len(copies))),
        terminal_field('View directories', command),
    ]
