"""Adapt release-page commands to checked policy and terminal output."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pubanon import _config
from pubanon._cli import _context
from pubanon._errors import OperationCanceled, PubanonError
from pubanon._remote import _host, _releases
from pubanon._terminal import confirm, escape, fields, heading, output, verbose_enabled
from pubanon._terminal import field as terminal_field

if TYPE_CHECKING:
    import argparse


def _notes(path: str | None) -> bytes | None:
    """Read supplied UTF-8 notes exactly once without normalizing newlines."""
    if path is None:
        return None
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise PubanonError('cannot read release notes file', fact=escape(str(exc))) from exc
    try:
        data.decode('utf-8')
    except UnicodeError as exc:
        raise PubanonError('release notes file must be valid UTF-8') from exc
    return data


def _print_notes(notes: str) -> None:
    """Print every notes line while visibly escaping control characters."""
    print(terminal_field('Notes', ''))
    for line in notes.split('\n'):
        print(escape(line))


def _status(page: _host.GitHubRelease) -> str:
    """Describe hosted release flags without changing them."""
    return ', '.join(
        ('draft' if page.draft else 'published', 'prerelease' if page.prerelease else 'release')
    )


def _preview(change: _releases.ReleaseChange) -> None:
    """Show all checked release facts before a possible host mutation."""
    heading(f'Release page {change.operation}')
    destination = change.destination
    fields(
        [
            ('Repository', escape(f'{destination.owner}/{destination.repo}')),
            ('Visibility', escape(destination.visibility)),
            ('Tag', escape(change.tag)),
            (
                'Published commit',
                escape(change.commit if verbose_enabled() else change.commit[:12]),
            ),
            ('Title', escape(change.title)),
        ]
    )
    _print_notes(change.notes.decode('utf-8'))
    if change.operation == 'create':
        output('Confirmation creates a published, non-prerelease release page.')
    else:
        previous = change.previous
        if previous is not None:
            print(terminal_field('Existing status', _status(previous)))
        output('Confirmation updates this page title and notes.')


def _write(args: argparse.Namespace, operation: Literal['create', 'edit']) -> None:
    """Keep the project lock through preparation, confirmation, and read-back."""
    notes = _notes(args.notes_file)
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        request = _releases.ReleaseRequest(operation, args.tag, args.title, notes)
        change = _releases.prepare_change(ctx.paths, ctx.global_config, ctx.project, request)
        _preview(change)
        if not args.yes and not confirm(f'{operation.capitalize()} this release page?'):
            raise OperationCanceled('Canceled. Nothing was changed.')
        result = _releases.apply_change(ctx.paths, ctx.global_config, ctx.project, change)
    past = 'created' if operation == 'create' else 'edited'
    output(f'Release page {past}: {escape(result.url)}', kind='success')


def command_create(args: argparse.Namespace) -> None:
    """Create a page for an already published tag after confirmation."""
    _write(args, 'create')


def command_edit(args: argparse.Namespace) -> None:
    """Edit a page's title or notes after confirmation."""
    _write(args, 'edit')


def command_list(args: argparse.Namespace) -> None:
    """List a bounded number of pages in the verified final repository."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        dest = _host.verify_destination(ctx.paths, ctx.global_config, ctx.project)
        pages = _host.list_releases(dest, limit=args.limit)
    heading('GitHub release pages')
    fields([('Repository', escape(f'{dest.owner}/{dest.repo}')), ('Limit', str(args.limit))])
    if not pages:
        output('No release pages found.')
        return
    for page in pages:
        print(f'{escape(page.tag)}  {escape(page.title)}  [{_status(page)}]')


def command_show(args: argparse.Namespace) -> None:
    """Show an explicit page and its complete hosted notes."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        dest = _host.verify_destination(ctx.paths, ctx.global_config, ctx.project)
        page = _host.read_release(dest, args.tag)
    if page is None:
        raise PubanonError('release page does not exist', fact=escape(args.tag))
    heading('GitHub release page')
    fields(
        [
            ('Repository', escape(f'{dest.owner}/{dest.repo}')),
            ('Tag', escape(page.tag)),
            ('URL', escape(page.url)),
            ('Status', _status(page)),
            ('Title', escape(page.title)),
        ]
    )
    _print_notes(page.notes)
