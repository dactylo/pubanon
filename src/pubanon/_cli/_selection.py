"""Present and apply shared file-selection decisions."""

from __future__ import annotations

import json
import sys
from dataclasses import replace
from typing import TYPE_CHECKING

from pubanon import (
    _config,
    _filesystem,
    _policy,
)
from pubanon._errors import OperationCancelled, PubanonError
from pubanon._terminal import (
    ask,
    escape,
    heading,
    output,
    style,
)

if TYPE_CHECKING:
    from pathlib import Path

    from pubanon._snapshots import _source


from pubanon._cli import _context


def _policy_decision(pattern: str, admitted: list[str]) -> tuple[str, str]:
    """Read one include, exclude, customize, or quit policy decision."""
    print(style('Files to include or exclude:'))
    for path in admitted:
        print(f'  {escape(path)}')
    print(f'Selection pattern: {escape(pattern)} (also applies to future matching files).')
    decision = ask('Include these files?', choices=['include', 'exclude', 'customize', 'quit'])
    if decision == 'customize':
        return 'publish', ask('Replacement pattern')
    if decision == 'include':
        return 'publish', pattern
    if decision == 'exclude':
        return 'deny', pattern
    raise OperationCancelled('operation cancelled', fact='nothing was saved')


def _print_grouped_files(paths: list[str]) -> None:
    """Show every included path once, grouped under directory counts."""
    heading('Files to publish:')
    if not paths:
        print('  None.')
        return
    groups: dict[str, list[str]] = {}
    for path in paths:
        directory, separator, _rest = path.partition('/')
        groups.setdefault(directory + separator, []).append(path)
    for group, members in sorted(groups.items()):
        if group.endswith('/'):
            count = len(members)
            unit = 'file' if count == 1 else 'files'
            print(f'  {style(escape(group) + f" - {count} {unit}")}')
            for path in members:
                print(f'    {escape(path)}')
        else:
            print(f'  {escape(group)}')


def _print_policy_offers(offers: list[tuple[str, list[str]]]) -> None:
    """Explain the proposed include patterns with bounded examples."""
    heading('Proposed allow rules:', separate=True)
    print('Examples are the first two matching files in path order.')
    if not offers:
        print('  None.')
    for pattern, admitted in offers:
        count = len(admitted)
        unit = 'file' if count == 1 else 'files'
        print(f'  {style(escape(pattern) + ":")} {count} {unit}')
        for path in sorted(admitted)[:2]:
            print(f'    {style("sample:")} {escape(path)}')


def apply_policy_offers(
    ctx: _context.ProjectContext,
    snapshot: _source.Snapshot,
    *,
    persist: bool = True,
    yes: bool,
) -> _context.ProjectContext:
    """Interactively resolve unmatched and credential-like path decisions.

    Args:
        ctx: Project configuration to refine.
        snapshot: Source entries whose selection needs review.
        persist: Save changed project configuration when true.
        yes: Refuse proposals that require interactive answers.

    Returns:
        A context containing the resulting selection rules.
    """
    sys.stdout.write('These decisions configure selection rules. Nothing is published now.\n')
    project = ctx.project
    paths = [entry.path for entry in snapshot]
    membership = _policy.evaluate_membership(paths, project.allow, project.deny)
    if membership.unmatched and yes:
        raise PubanonError('--yes cannot answer policy proposals')
    allow = list(project.allow)
    deny = list(project.deny)
    exceptions = list(project.secret_exceptions)
    membership = _policy.evaluate_membership(paths, tuple(allow), tuple(deny))
    while membership.unmatched:
        for pattern, admitted in _policy.narrowest_offer(
            membership.unmatched,
            guided=False,
        ):
            action, selected = _policy_decision(pattern, admitted)
            (allow if action == 'publish' else deny).append(selected)
        membership = _policy.evaluate_membership(paths, tuple(allow), tuple(deny))
    project = replace(project, allow=tuple(allow), deny=tuple(deny))
    allowed = membership.allowed
    for path in _policy.secret_like(allowed, exceptions):
        if yes:
            raise PubanonError('--yes cannot answer credential-like path warnings')
        print(f'{style("Credential-like file:")} {escape(path)}')
        decision = ask('Include it?', choices=['include', 'exclude', 'quit'])
        if decision == 'include':
            exceptions.append(path)
        elif decision == 'exclude':
            deny.append(_policy.literal_pattern(path))
        else:
            raise OperationCancelled('operation cancelled', fact='nothing was saved')
    project = replace(project, deny=tuple(deny), secret_exceptions=tuple(exceptions))
    if persist and project != ctx.project:
        _config.save_project(ctx.paths, project)
    membership = _policy.evaluate_membership(paths, project.allow, project.deny)
    if membership.unmatched:
        raise PubanonError('project policy still has unmatched paths')
    return _context.ProjectContext(ctx.paths, ctx.global_config, project)


def _save_new_path_list(
    ctx: _context.ProjectContext,
    new_paths: list[str],
    list_name: str = 'new-paths',
) -> Path:
    """Write a complete path batch with owner-only POSIX permissions and return its path."""
    path = _config.create_data_dirs(ctx.paths, ctx.project.name) / f'{list_name}.json'
    try:
        path.write_text(
            json.dumps(new_paths, ensure_ascii=False, indent=2) + '\n',
            encoding='utf-8',
        )
        _filesystem.chmod(path, 0o600)
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise PubanonError('cannot write complete path list') from exc
    return path


def sanitized_snapshot(
    ctx: _context.ProjectContext, snapshot: _source.Snapshot
) -> _source.Snapshot:
    """Retain only paths explicitly allowed after deny precedence."""
    membership = _policy.evaluate_membership(
        [entry.path for entry in snapshot], ctx.project.allow, ctx.project.deny
    )
    if membership.unmatched:
        raise PubanonError('project policy has unmatched paths')
    allowed = set(membership.allowed)
    return tuple(entry for entry in snapshot if entry.path in allowed)


def show_new_paths(ctx: _context.ProjectContext, new_paths: list[str]) -> None:
    """List every path absent from the last release, then name the saved copy."""
    heading('Files not in the last release:', separate=True)
    for path in new_paths:
        print(f'  {escape(path)}')
    print()
    print(f'{style("Saved list:")} {escape(str(_save_new_path_list(ctx, new_paths)))}')


def show_selection(ctx: _context.ProjectContext, paths: list[str], *, verbose: bool) -> None:
    """Display and save the exact proposed selection with exclusion reasons."""
    membership = _policy.evaluate_membership(paths, ctx.project.allow, ctx.project.deny)
    if membership.unmatched:
        raise PubanonError('project policy still has unmatched paths')
    _print_grouped_files(membership.allowed)
    heading('Excluded files:', separate=True)
    for path in membership.reserved:
        print(f'  {escape(path)} - Pubanon configuration (always excluded)')
    for path in membership.denied:
        print(f'  {escape(path)}')
    if membership.reserved or membership.denied:
        print()
        output('  Exclusions override included directories.', kind='note')
    else:
        print('  None.')
    if verbose:
        _print_policy_offers(
            [
                (
                    pattern,
                    _policy.evaluate_membership(paths, (pattern,), ctx.project.deny).allowed,
                )
                for pattern in ctx.project.allow
            ]
        )
        heading('Exclude rules:', separate=True)
        print('Deny rules take precedence over allow rules.')
        for pattern in ctx.project.deny:
            print(f'  {escape(pattern)}')
        if not ctx.project.deny:
            print('  None.')
    complete_list = _save_new_path_list(ctx, membership.allowed, 'proposed-selection')
    print(f'\n{style("Publish list saved to:")} {escape(str(complete_list))}\n')
