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
from pubanon._remote import _releases, _stage
from pubanon._snapshots import _history, _inspection, _records, _reports, _verification
from pubanon._terminal import (
    display_path,
    escape,
    fields,
    guidance,
    heading,
    next_step,
    output,
    style,
    verbose_enabled,
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
    snapshot: str | None = None,
) -> str:
    """Describe verified read-only snapshot files and optional review directories."""
    lines: list[str] = []
    if snapshot is not None:
        lines.extend(['  ' + terminal_field('Snapshot', snapshot), ''])
    lines.append(terminal_field('Review copy', display_path(selected)))
    if verbose_enabled():
        older = [copy for copy in copies if copy != selected]
        if older:
            lines.extend(['', style('Directories from earlier snapshots', 'heading')])
            lines.extend(f'  {display_path(copy)}' for copy in older)
        lines.append(guidance('Remove review copies with `pubanon clean`.'))
    return '\n'.join(lines)


def _inspect_published(ctx: _context.ProjectContext, selector: str, *, refresh: bool) -> None:
    """Inspect the verified read-only files of one saved publication."""
    entry = _history.select_publication(ctx.paths, ctx.project.name, selector)
    selected = _inspection.inspection_path(ctx.paths, ctx.project.name, entry.commit)
    copies = _inspection.inspection_paths(ctx.paths, ctx.project.name)
    if refresh:
        print(f'{style("Replacing review copy:")} {display_path(selected)}')
        output('All current contents of this directory will be replaced.', kind='warning')
        sys.stdout.flush()
    inspection = _history.inspect_publication(ctx.paths, ctx.project.name, entry, refresh=refresh)
    identity = escape(', '.join(entry.tags) or 'untagged')
    snapshot_id = entry.commit if verbose_enabled() else entry.commit[:12]
    heading('Published snapshot', separate=refresh)
    fields([('Snapshot', snapshot_id), ('Version', identity)])
    print('\n' + _inspection_details(inspection, copies))
    print()
    next_step(
        _context.project_command(
            ctx.project.name,
            'show',
            f'--project={ctx.project.name}',
            entry.commit if verbose_enabled() else entry.commit[:12],
        )
    )


def _inspect_rewritten_copy(
    ctx: _context.ProjectContext,
    proof: _verification.ObjectProof,
    selector: str,
    *,
    refresh: bool,
) -> Path:
    """Show the exact copy targeted by refresh before materializing it."""
    record = proof.record
    commit = _verification.select_rewritten_commit(record, selector)
    selected_path = _inspection.inspection_path(ctx.paths, ctx.project.name, commit)
    copies = _inspection.inspection_paths(ctx.paths, ctx.project.name)
    if refresh:
        print(terminal_field('Replacing review copy', display_path(selected_path)))
        output('All current contents of this directory will be replaced.', kind='warning')
        sys.stdout.flush()
    selected = _verification.inspect_rewritten(
        ctx.paths, ctx.project.name, proof, selector, refresh=refresh
    )
    print(_reports.inspect_report(ctx.paths, proof, ctx.global_config, ctx.project))
    print(
        '\n'
        + _inspection_details(
            selected,
            copies,
            snapshot=commit if verbose_enabled() else commit[:12],
        )
    )
    print()
    next_step(_stage.candidate_next_action(ctx.paths, ctx.global_config, ctx.project, record))

    return selected


def _inspection_listing(paths: _config.Paths, name: str) -> list[str]:
    """Describe preserved snapshot directories independently of snapshot state."""
    copies = _inspection.inspection_paths(paths, name)
    if not copies:
        return []
    return [
        f'{style("Preserved snapshot directories:")} {len(copies)}',
        *(f'  {display_path(copy)}' for copy in copies),
        'Open these directories in your editor to review their files.',
        guidance('Remove review copies with `pubanon clean`.'),
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
    proof: _verification.ObjectProof,
    *,
    refresh: bool = False,
    workflow: bool = False,
) -> Path:
    """Materialize a selected copy and describe its deliberate retention lifecycle.

    Args:
        ctx: Project context for the retained snapshot.
        proof: Object evidence obtained for this review step.
        refresh: Replace all contents of an existing inspection directory.
        workflow: Omit the candidate report and next action handled by the
            workflow.

    Returns:
        The inspection directory, retained until explicitly cleaned or
        refreshed.
    """
    record = proof.record
    selected = _inspection.inspection_path(
        ctx.paths, ctx.project.name, _records.candidate_tip(record)
    )
    copies = _inspection.inspection_paths(ctx.paths, ctx.project.name)
    if refresh:
        print(f'{style("Replacing review copy:")} {display_path(selected)}')
        print(
            style('All current contents of this directory will be replaced.', 'warning'),
            flush=True,
        )
    inspection = _verification.materialize_inspect(
        ctx.paths, ctx.project.name, proof, refresh=refresh
    )
    details = _inspection_details(inspection, copies)
    if not workflow:
        print(_reports.inspect_report(ctx.paths, proof, ctx.global_config, ctx.project))
        _releases.show_planned_page(ctx.paths, ctx.project, record)
    print('\n' + details)
    print()
    if not workflow:
        next_step(_stage.candidate_next_action(ctx.paths, ctx.global_config, ctx.project, record))
    return inspection


def command_inspect(args: argparse.Namespace) -> None:
    """Materialize or report one retained snapshot inspection."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    if args.rewritten is not None and args.published is not None:
        raise PubanonError('--rewritten cannot be combined with --published')
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        if args.published is not None:
            _inspect_published(ctx, args.published, refresh=args.refresh)
            return
        record = _records.load_candidate(paths, name)
        if record is None:
            _inspect_without_candidate(
                paths, name, refresh=args.refresh or args.rewritten is not None
            )
            return
        proof = _verification.prove_objects(paths, name, record)
        if args.rewritten is not None:
            _inspect_rewritten_copy(ctx, proof, args.rewritten, refresh=args.refresh)
            return
        inspection_copy(ctx, proof, refresh=args.refresh)
