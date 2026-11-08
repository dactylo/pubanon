"""Present and apply shared file-selection decisions."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

from pubanon import (
    _config,
    _filesystem,
    _policy,
)
from pubanon._errors import OperationCanceled, PubanonError
from pubanon._terminal import (
    ask,
    confirm,
    detail,
    display_path,
    escape,
    heading,
    output,
    style,
    verbose_enabled,
)

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable
    from pathlib import Path

    from pubanon._snapshots import _source


from pubanon._cli import _context

_UNDECIDED_SHOWN = 5


def _confirm_effect(pattern: str, action: str, membership: _policy.Membership) -> bool:
    """Report what a group choice changes, refusing no-ops and unconfirmed exclusions."""
    decided = _policy.evaluate_membership(membership.unmatched, (pattern,), ()).allowed
    removed = (
        _policy.evaluate_membership(membership.allowed, (), (pattern,)).denied
        if action == 'deny'
        else []
    )
    if not decided and not removed:
        output(f'No undecided files match {escape(pattern)}. Nothing changed.', kind='warning')
        return False
    if removed:
        output('This pattern also matches files that are already included:', kind='warning')
        for path in removed:
            print(f'  {escape(path)}')
        if not confirm('Exclude them too?', default=False):
            output('Nothing changed.', kind='note')
            return False
    count = len(decided) + len(removed)
    verb = 'Included' if action == 'publish' else 'Excluded'
    output(f'{verb} {count} {"file" if count == 1 else "files"}.')
    return True


def _policy_review_notice(membership: _policy.Membership, exceptions: tuple[str, ...]) -> None:
    """Explain selection rules only when a selection decision is needed."""
    if membership.unmatched or _policy.secret_like(membership.allowed, exceptions):
        print('These decisions configure selection rules. Nothing is published now.')


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


def _save_new_path_list(
    ctx: _context.ProjectContext,
    new_paths: list[str],
    list_name: str = 'new-paths',
) -> Path:
    """Write a complete path batch with owner-only POSIX permissions and return its path."""
    path = _config.create_data_dirs(ctx.paths, ctx.project.name) / f'{list_name}.json'
    try:
        _filesystem.write_owner_only(
            path, (json.dumps(new_paths, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
        )
    except (OSError, UnicodeError) as exc:
        raise PubanonError('cannot write complete path list') from exc
    return path


def _undecided_fact(paths: list[str]) -> str:
    """Name the first undecided paths and count the rest."""
    shown = ', '.join(escape(path) for path in paths[:_UNDECIDED_SHOWN])
    rest = len(paths) - _UNDECIDED_SHOWN
    more = f', and {rest} more' if rest > 0 else ''
    noun = 'file' if len(paths) == 1 else 'files'
    return f'{len(paths)} undecided {noun}: {shown}{more}'


def ask_credential_file(path: str, *, back: bool = False) -> str:
    """Warn about one credential-like filename and ask whether to publish it.

    Args:
        path: Source-relative path whose name looks credential-like.
        back: Offer a return to the previous choice.

    Returns:
        `include`, `exclude`, `quit`, or `back` when offered.

    Raises:
        PubanonError: Input ended or an interactive terminal is unavailable.
        OperationCanceled: The prompt is interrupted.
    """
    output(
        'This filename can contain credentials. A name match is a warning, not proof of a secret.',
        kind='warning',
    )
    print(f'\n{style("Credential-like file:")} {escape(path)}')
    choices = ['include', 'exclude', *(['back'] if back else []), 'quit']
    return ask('Include it?', choices=choices)


def _policy_decision(
    pattern: str, admitted: list[str], membership: _policy.Membership, *, back: bool = False
) -> tuple[str, str]:
    """Read a group choice that changes the selection, optionally undoing the previous one."""
    while True:
        print(style('Undecided files:'))
        for path in admitted:
            print(f'  {escape(path)}')
        print(f'Selection pattern: {escape(pattern)} (also applies to future matching files).')
        choices = ['include', 'exclude', 'customize', *(['back'] if back else []), 'quit']
        decision = ask('Include these files?', choices=choices)
        if decision == 'back':
            return 'back', pattern
        if decision == 'quit':
            raise OperationCanceled('Canceled. Nothing was saved.')
        selected = pattern
        if decision == 'customize':
            selected = ask('Custom pattern')
            decision = ask('Include or exclude matching files?', choices=['include', 'exclude'])
        action = 'publish' if decision == 'include' else 'deny'
        if _confirm_effect(selected, action, membership):
            return action, selected


def _apply_policy_offers(
    ctx: _context.ProjectContext,
    snapshot: _source.Snapshot,
    *,
    yes: bool,
) -> _context.ProjectContext:
    """Resolve unmatched and credential-like path decisions."""
    project = ctx.project
    paths = [entry.path for entry in snapshot]
    global_deny = _config.global_deny(ctx.global_config)
    membership = _policy.evaluate_membership(
        paths, project.allow, project.deny, global_deny=global_deny
    )
    _policy_review_notice(membership, project.secret_exceptions)
    if membership.unmatched and yes:
        raise PubanonError(
            '--yes cannot answer policy proposals',
            fact=_undecided_fact(membership.unmatched),
            recovery='rerun without --yes to decide them',
        )
    allow = list(project.allow)
    deny = list(project.deny)
    exceptions = list(project.secret_exceptions)
    while membership.unmatched:
        pattern, admitted = _policy.narrowest_offer(membership.unmatched, guided=False)[0]
        action, selected = _policy_decision(pattern, admitted, membership)
        (allow if action == 'publish' else deny).append(selected)
        membership = _policy.evaluate_membership(
            paths, tuple(allow), deny, global_deny=global_deny
        )
    project = replace(project, allow=tuple(allow), deny=tuple(deny))
    allowed = membership.allowed
    for path in _policy.secret_like(allowed, exceptions):
        if yes:
            raise PubanonError(
                '--yes cannot answer credential-like path warnings',
                fact=f'path: {escape(path)}',
                recovery='rerun without --yes to decide it',
            )
        decision = ask_credential_file(path)
        if decision == 'include':
            exceptions.append(path)
        elif decision == 'exclude':
            deny.append(_policy.literal_pattern(path))
        else:
            raise OperationCanceled('Canceled. Nothing was saved.')
    project = replace(project, deny=tuple(deny), secret_exceptions=tuple(exceptions))
    membership = _policy.evaluate_membership(
        paths, project.allow, project.deny, global_deny=global_deny
    )
    if membership.unmatched:
        raise PubanonError('project policy still has unmatched paths')
    return _context.ProjectContext(ctx.paths, ctx.global_config, project)


def ask_next_group(
    paths: Iterable[str],
    allow: Collection[str],
    deny: Collection[str],
    *,
    global_deny: Collection[str],
) -> tuple[str, str] | None:
    """Ask about the narrowest undecided group, offering Back.

    Args:
        paths: Paths eligible for a group decision.
        allow: Include patterns decided so far.
        deny: Exclude patterns decided so far.
        global_deny: Patterns every project excludes.

    Returns:
        `None` when no path is undecided; otherwise the `_policy_decision`
        result, whose action is `publish`, `deny`, or `back`.

    Raises:
        PubanonError: Input ended or an interactive terminal is unavailable.
        OperationCanceled: The selection is canceled or interrupted.
    """
    membership = _policy.evaluate_membership(paths, allow, deny, global_deny=global_deny)
    if not membership.unmatched:
        return None
    pattern, admitted = _policy.narrowest_offer(membership.unmatched, guided=True)[0]
    return _policy_decision(pattern, admitted, membership, back=True)


def _review_groups(ctx: _context.ProjectContext, paths: list[str]) -> _context.ProjectContext:
    """Reconsider the selected tree's groups without changing saved configuration."""
    global_deny = _config.global_deny(ctx.global_config)
    eligible = _policy.evaluate_membership(paths, ('**',), (), global_deny=global_deny).allowed
    project = replace(ctx.project, allow=(), deny=())
    previous: list[_config.ProjectConfig] = []
    while (
        decision := ask_next_group(eligible, project.allow, project.deny, global_deny=global_deny)
    ) is not None:
        action, selected = decision
        if action == 'back':
            if previous:
                project = previous.pop()
            else:
                output('There is no earlier file choice.', kind='note')
            continue
        previous.append(project)
        project = (
            replace(project, allow=(*project.allow, selected))
            if action == 'publish'
            else replace(project, deny=(*project.deny, selected))
        )
    return _context.ProjectContext(ctx.paths, ctx.global_config, project)


def sanitized_snapshot(
    ctx: _context.ProjectContext, snapshot: _source.Snapshot
) -> _source.Snapshot:
    """Retain only paths explicitly allowed after deny precedence.

    Raises:
        PubanonError: Selected source paths remain undecided.
    """
    membership = _policy.evaluate_membership(
        [entry.path for entry in snapshot],
        ctx.project.allow,
        ctx.project.deny,
        global_deny=_config.global_deny(ctx.global_config),
    )
    if membership.unmatched:
        raise PubanonError('project policy has unmatched paths')
    allowed = set(membership.allowed)
    return tuple(entry for entry in snapshot if entry.path in allowed)


def show_new_paths(
    ctx: _context.ProjectContext, new_paths: list[str], *, show: bool = True
) -> None:
    """Save every new path and optionally list paths for a later release review.

    Raises:
        PubanonError: The complete private path report cannot be saved.
    """
    saved = _save_new_path_list(ctx, new_paths)
    if show:
        heading('Files not in the last release', separate=True)
        for path in new_paths:
            print(f'  {escape(path)}')
        print()
    detail(f'Saved list: {display_path(saved)}')


def show_selection(ctx: _context.ProjectContext, paths: list[str]) -> None:
    """Display and save the exact proposed selection with exclusion reasons.

    Raises:
        PubanonError: Paths remain undecided or the complete private selection
            report cannot be saved.
    """
    membership = _policy.evaluate_membership(
        paths,
        ctx.project.allow,
        ctx.project.deny,
        global_deny=_config.global_deny(ctx.global_config),
    )
    if membership.unmatched:
        raise PubanonError('project policy still has unmatched paths')
    _print_grouped_files(membership.allowed)
    heading('Excluded files:', separate=True)
    for path in membership.reserved:
        print(f'  {escape(path)} - pubanon configuration (always excluded)')
    globally_denied = set(
        _policy.evaluate_membership(
            paths, (), (), global_deny=_config.global_deny(ctx.global_config)
        ).denied
    )
    for path in membership.denied:
        reason = ' - globally ignored' if path in globally_denied else ''
        print(f'  {escape(path)}{reason}')
    if membership.reserved or membership.denied:
        print()
        output('  Exclusions override included directories.', kind='note')
    else:
        print('  None.')
    if verbose_enabled():
        _print_policy_offers(
            [
                (
                    pattern,
                    _policy.evaluate_membership(
                        paths,
                        (pattern,),
                        ctx.project.deny,
                        global_deny=_config.global_deny(ctx.global_config),
                    ).allowed,
                )
                for pattern in ctx.project.allow
            ]
        )
        heading('Exclude rules:', separate=True)
        print('Deny rules take precedence over allow rules.')
        for directory in ctx.global_config.ignored_directories:
            print(f'  {escape(directory)}/ - globally ignored directory')
        for file in ctx.global_config.ignored_files:
            print(f'  {escape(file)} - globally ignored file')
        for pattern in ctx.project.deny:
            print(f'  {escape(pattern)}')
        if not ctx.project.deny and not _config.global_deny(ctx.global_config):
            print('  None.')
    complete_list = _save_new_path_list(ctx, membership.allowed, 'proposed-selection')
    detail(f'Publish list saved to: {display_path(complete_list)}')


def review_selection(
    ctx: _context.ProjectContext, snapshot: _source.Snapshot, *, yes: bool
) -> _context.ProjectContext:
    """Accept or revise file choices before saving the project's selection.

    Unattended preparation keeps the existing policy-proposal refusals. An
    interactive cancellation leaves the saved project configuration unchanged.

    Raises:
        PubanonError: Unattended selection requires new decisions, selection
            reporting or saving fails, or interactive input is unavailable or
            ends.
        OperationCanceled: The review is canceled or interrupted.
    """
    original = ctx.project
    ctx = _apply_policy_offers(ctx, snapshot, yes=yes)
    if not yes:
        paths = [entry.path for entry in snapshot]
        while True:
            show_selection(ctx, paths)
            choice = ask('Use this selection?', choices=['continue', 'review', 'quit'])
            if choice == 'quit':
                raise OperationCanceled('Canceled. Nothing was saved.')
            if choice == 'continue':
                break
            print('Review each group below. Customize takes a pattern to include or exclude.')
            ctx = _apply_policy_offers(_review_groups(ctx, paths), snapshot, yes=False)
    if ctx.project != original:
        _config.save_project(ctx.paths, ctx.project)
    return ctx
