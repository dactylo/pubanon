"""Adapt release-page commands to checked policy and terminal output."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pubanon import _config
from pubanon._cli import _context
from pubanon._errors import OperationCanceled, PubanonError
from pubanon._remote import _host, _releases
from pubanon._terminal import (
    confirm,
    display_commit,
    escape,
    fields,
    heading,
    output,
)
from pubanon._terminal import field as terminal_field

if TYPE_CHECKING:
    import argparse


def _changed_fields(change: _releases.ReleaseChange) -> list[str]:
    """Name the page fields that confirming an edit changes."""
    previous = change.previous
    if previous is None:
        return ['title', 'notes']
    return [
        name
        for name, differs in (
            ('title', change.title != previous.title),
            ('notes', change.notes != previous.notes.encode('utf-8')),
        )
        if differs
    ]


def _print_notes(notes: str, *, label: str = 'Notes', source: str = '') -> None:
    """Print every notes line while visibly escaping control characters."""
    print(terminal_field(label, source))
    for line in notes.split('\n'):
        print(escape(line))


def _read_notes_file(path: str) -> bytes:
    """Read supplied UTF-8 notes exactly once without normalizing newlines."""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise PubanonError('cannot read release notes file', fact=escape(str(exc))) from exc
    try:
        data.decode('utf-8')
    except UnicodeError as exc:
        raise PubanonError('release notes file must be valid UTF-8') from exc
    return data


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
                escape(display_commit(change.commit)),
            ),
            ('Title', escape(change.title)),
        ]
    )
    source = _releases.notes_source(change.origin, change.notes_file)
    if change.operation == 'edit' and change.origin is not None and change.previous is not None:
        _print_notes(change.previous.notes, label='Current notes')
        _print_notes(change.notes.decode('utf-8'), label='New notes', source=source)
    else:
        _print_notes(change.notes.decode('utf-8'), source=source)
    _releases.show_notes_warning(change.origin)
    if change.origin is not None and change.origin.kind == 'empty':
        output(
            'No CHANGELOG.md in the published snapshot; set release_page.changelog to link one.'
        )
    if change.operation == 'create':
        output(
            'Confirmation creates a draft, non-prerelease release page. It stays hidden until '
            'your release workflow, or you, publish it.'
            if change.draft
            else 'Confirmation creates a published, non-prerelease release page.'
        )
    else:
        previous = change.previous
        if previous is not None:
            print(terminal_field('Existing status', _status(previous)))
        output(f"Confirmation updates this page's {' and '.join(_changed_fields(change))}.")


def command_list(args: argparse.Namespace) -> None:
    """List a bounded number of pages in the verified final repository.

    Raises:
        PubanonError: Configuration, destination verification, or the hosted
            listing fails.
    """
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
    """Show an explicit page and its complete hosted notes.

    Raises:
        PubanonError: Configuration or destination verification fails, or the
            exact page is unavailable.
    """
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


def create_page(
    ctx: _context.ProjectContext, request: _releases.ReleaseRequest, *, yes: bool
) -> None:
    """Create or reuse the requested page while the caller holds its project lock.

    Args:
        ctx: Loaded project context under the caller's project lock.
        request: Tag and optional creation overrides.
        yes: Skip confirmation for a new page.

    Raises:
        OperationCanceled: The operator declines creation.
        PubanonError: Publication, policy, or host facts prevent creation.
    """
    change = _releases.prepare_change(ctx.paths, ctx.global_config, ctx.project, request)
    if change.operation == 'existing':
        page = change.previous
        if page is None:
            raise ValueError('existing release change has no page')
        output(
            f'Release page already exists: {_releases.page_url_text(page.url, draft=page.draft)}',
            kind='success',
        )
        _releases.show_draft_guidance(ctx.project, draft=page.draft)
        return
    _preview(change)
    if not yes and not confirm('Create this release page?'):
        raise OperationCanceled('Canceled. Nothing was changed.')
    result = _releases.apply_change(ctx.paths, ctx.global_config, ctx.project, change)
    message = 'created' if result.written else 'already exists'
    url = _releases.page_url_text(result.page.url, draft=result.page.draft)
    output(f'Release page {message}: {url}', kind='success')
    _releases.show_draft_guidance(ctx.project, draft=result.page.draft)


def _write(args: argparse.Namespace, operation: Literal['create', 'edit']) -> None:
    """Keep the project lock through preparation, confirmation, and read-back."""
    notes = _read_notes_file(args.notes_file) if args.notes_file is not None else None
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        request = _releases.ReleaseRequest(
            operation,
            args.tag,
            args.title,
            notes,
            Path(args.notes_file).absolute() if args.notes_file is not None else None,
            refresh_notes=operation == 'edit' and args.refresh_notes,
        )
        if operation == 'create':
            create_page(ctx, request, yes=args.yes)
            return
        change = _releases.prepare_change(ctx.paths, ctx.global_config, ctx.project, request)
        if not _changed_fields(change):
            _releases.show_notes_warning(change.origin)
            output('Nothing to change: the release page already has these notes and this title.')
            return
        _preview(change)
        if not args.yes and not confirm(f'{operation.capitalize()} this release page?'):
            raise OperationCanceled('Canceled. Nothing was changed.')
        result = _releases.apply_change(ctx.paths, ctx.global_config, ctx.project, change)
    output(f'Release page edited: {escape(result.page.url)}', kind='success')


def command_create(args: argparse.Namespace) -> None:
    """Create a page for an already published tag after confirmation.

    Raises:
        PubanonError: Publication or metadata checks fail, or the page write
            cannot be verified.
        OperationCanceled: An interactive decision is declined or the operation
            is canceled.
    """
    _write(args, 'create')


def command_edit(args: argparse.Namespace) -> None:
    """Edit a page's title or notes after confirmation.

    Raises:
        PubanonError: Publication or metadata checks fail, or the page write
            cannot be verified.
        OperationCanceled: An interactive decision is declined or the operation
            is canceled.
    """
    _write(args, 'edit')


def page_request(args: argparse.Namespace) -> _releases.PageRequest:
    """Build the release-page request from the publish and run page options.

    Raises:
        PubanonError: The notes file cannot be read or is not UTF-8.
    """
    notes_file = args.release_notes_file
    return _releases.PageRequest(
        skip=args.no_release_page,
        notes=_read_notes_file(notes_file) if notes_file is not None else None,
        notes_file=Path(notes_file).absolute() if notes_file is not None else None,
        requested=args.release_page,
    )
