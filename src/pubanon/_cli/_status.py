"""Report snapshot and publication status."""

from __future__ import annotations

import os
import tomllib
from typing import TYPE_CHECKING, cast

from pubanon import (
    _config,
    _policy,
    _run,
)
from pubanon._cli import _context
from pubanon._errors import ObservationTimeoutError, PubanonError
from pubanon._remote import _host
from pubanon._snapshots import _candidate, _records
from pubanon._terminal import (
    display_path,
    display_time,
    escape,
    fields,
    heading,
    next_step,
    style,
)
from pubanon._terminal import (
    field as terminal_field,
)

if TYPE_CHECKING:
    import argparse


def _head_has_static_version(ctx: _context.ProjectContext) -> bool:
    """Check whether preparing HEAD can infer its selected package version."""
    membership = _policy.evaluate_membership(
        ['pyproject.toml'],
        ctx.project.allow,
        ctx.project.deny,
        global_deny=_config.global_deny(ctx.global_config),
    )
    if not membership.allowed:
        return False
    try:
        result = _run.run_git(
            ['show', 'HEAD:pyproject.toml'],
            cwd=ctx.project.source_path,
            check=False,
            timeout=5.0,
        )
        if result.returncode != 0:
            return False
        document = tomllib.loads(result.stdout.decode('utf-8'))
        project = document.get('project')
        if not isinstance(project, dict):
            return False
        version = cast('dict[str, object]', project).get('version')
        if not isinstance(version, str):
            return False
        _candidate.tag_intent_for(version)
    except (ObservationTimeoutError, PubanonError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return False
    return True


def _ordinary_reprepare_advice(
    ctx: _context.ProjectContext, record: _candidate.CandidateRecord
) -> str:
    """Select current HEAD and retain only a version that HEAD cannot infer."""
    suffix = (
        f' --version {record.tag_intent[1:]}'
        if record.tag_intent is not None and not _head_has_static_version(ctx)
        else ''
    )
    return f'pubanon discard && pubanon prepare{suffix}'


def _publication_status(
    ctx: _context.ProjectContext,
    main: str | None,
    tags: dict[str, str],
    verified: _host.Destination | None,
    *,
    verbose: bool = False,
) -> list[tuple[str, str]]:
    """Summarize publication while retaining differing configuration and observed facts."""
    if main is None:
        return [('Published', 'none')]
    record = _records.load_publication(ctx.paths, ctx.project.name, main)
    version = next((tag for tag, commit in tags.items() if commit == main), None)
    label = escape((record.tag_intent if record else None) or version or 'untagged')
    rows: list[tuple[str, str]] = []
    if record is not None:
        destination = record.destination
        rows.append(
            (
                'Published',
                (
                    f'{label} to {escape(destination.owner)}/{escape(destination.repo)} '
                    f'({destination.visibility}), {display_time(record.confirmed_at)}'
                ),
            )
        )
        same_destination = (destination.adapter, destination.owner, destination.repo) == (
            ctx.project.host,
            ctx.project.owner,
            ctx.project.repo,
        )
        if same_destination:
            if destination.visibility != ctx.project.visibility:
                rows.append(('Configured visibility', ctx.project.visibility))
            if verified is not None:
                rows.append(('Current visibility', verified.visibility))
        else:
            rows.append(
                (
                    'Configured destination',
                    (
                        f'{escape(ctx.project.owner)}/{escape(ctx.project.repo)} '
                        f'({ctx.project.visibility})'
                    ),
                )
            )
    else:
        rows.extend([('Published', label), ('Source', 'not recorded')])
    if verbose:
        rows.append(('Snapshot commit', main))
    return rows


def _replacement_summary(record: _candidate.CandidateRecord) -> str:
    """Explain which published history the replacement changes."""
    if record.replace_scope == 'all':
        return 'Replace all published history and tags with one new initial publication.'
    if record.replaces == record.prestate_main:
        return 'Replace the latest publication; preserve earlier history.'
    return (
        'Replace the selected publication and rebuild later publications; '
        'preserve earlier history.'
    )


def _snapshot_provenance(
    ctx: _context.ProjectContext, record: _candidate.CandidateRecord
) -> list[str]:
    """Render full identifiers and source coordinates for verbose snapshot status."""
    lines = [
        terminal_field('Operation', 'replacement' if record.replaces is not None else 'new'),
        terminal_field('Snapshot commit', record.commit),
        terminal_field('Source commit', record.pinned.commit),
        terminal_field('Source repository', display_path(ctx.project.source_path)),
        terminal_field(
            'Local store',
            display_path(_candidate.candidate_store_path(ctx.paths, ctx.project.name)),
        ),
        terminal_field(
            'Source selector', escape(record.pinned.selector_name or record.pinned.commit)
        ),
    ]
    if record.tip is not None and record.tip != record.commit:
        lines.append(terminal_field('Final snapshot ID', record.tip))
    return lines


def rewrite_status(
    paths: _config.Paths, name: str, record: _candidate.CandidateRecord, *, verbose: bool = False
) -> list[str]:
    """Summarize replacement preparation and actionable incomplete progress."""
    progress = _records.load_rewrite(paths, name)
    label = ', '.join(tag for tag, oid in record.prestate_tags.items() if oid == record.replaces)
    selected = label or (record.replaces or record.commit)[:12]
    scope = (
        'all published snapshots'
        if record.replace_scope == 'all'
        else f'published snapshot for {escape(selected)}'
    )
    details = [terminal_field('Replaces', scope), _replacement_summary(record)]
    count = len(progress.replayed) if progress is not None else len(record.replayed)
    if count:
        details.append(terminal_field('Later snapshots rebuilt', str(count)))
    if record.tip is not None and progress is None:
        if verbose:
            details.append(terminal_field('Resulting tip', record.tip))
        return details
    details.append(terminal_field('Preparation', 'incomplete', kind='warning'))
    if record.tip is not None:
        details.append('Replacement preparation completed; local cleanup is pending.')
    elif progress is None:
        details.append('Replacement initialization incomplete; saved snapshot is available.')
    elif progress.phase == 'replaying':
        details.append('Preparation interrupted while rebuilding later snapshots.')
    elif progress.phase == 'exporting':
        details.append('Conflict export incomplete; continuation restores the export.')
    else:
        directory = _records.rewrite_directory(paths, name)
        decisions = (
            len(
                {path for conflict in progress.pause.conflicts for path in conflict.decision_paths}
            )
            if progress.pause
            else 0
        )
        details.append(terminal_field('Path decisions awaiting resolution', str(decisions)))
        details.append(terminal_field('Resolution directory', display_path(directory)))
        details.append(
            terminal_field('Resolution manifest', display_path(directory / 'resolutions.toml'))
        )
    if progress is not None and progress.pause is not None:
        old = progress.pause.old
        tags = sorted(tag for tag, oid in record.prestate_tags.items() if oid == old)
        release = escape(', '.join(tags)) if tags else old[:12]
        details.append(terminal_field('Rebuilding snapshot', release))
        details.extend(
            f'  {escape(conflict.conflict_type)}: {escape(conflict.message)}'
            for conflict in progress.pause.conflicts
        )
    if verbose and record.tip is not None:
        details.append(terminal_field('Resulting tip', record.tip))
    return details


def _status_candidate(
    ctx: _context.ProjectContext,
    record: _candidate.CandidateRecord | None,
    *,
    verbose: bool = False,
) -> tuple[list[str], list[str], bool, bool]:
    """Report snapshot recovery before considering publication or remote status."""
    paths, name = ctx.paths, ctx.project.name
    details: list[str] = []
    causes: list[str] = []
    source_changed = False
    source_available = False
    if record is None:
        pass
    elif record.state == _candidate.STATE_UNKNOWN_OUTCOME and _candidate.confirmed_publication(
        paths, name, record
    ):
        details.append(style('Snapshot published; local recording needs completion.', 'warning'))
    else:
        incomplete = record.replaces is not None and (
            record.tip is None or _records.load_rewrite(paths, name) is not None
        )
        title = (
            'Snapshot with unknown publish outcome'
            if record.state == _candidate.STATE_UNKNOWN_OUTCOME
            else 'Snapshot preparation incomplete'
            if incomplete
            else 'Prepared snapshot'
        )
        details.extend([style(title, 'warning')] if title != 'Prepared snapshot' else [])
        if record.replaces is not None and record.state != _candidate.STATE_UNKNOWN_OUTCOME:
            details.extend(rewrite_status(paths, name, record, verbose=verbose))
        if verbose:
            details.extend(_snapshot_provenance(ctx, record))
        if _config.fingerprint(ctx.global_config, ctx.project) != record.fingerprint:
            causes.append('Pubanon settings have changed since this snapshot was prepared.')
        if record.pinned.selector_kind == 'ref' and record.pinned.selector_name is not None:
            current = _run.run_git(
                ['rev-parse', '--verify', record.pinned.selector_name],
                cwd=ctx.project.source_path,
                check=False,
            )
            expected_selector = f'{record.pinned.selector_oid}\n'.encode()
            if current.returncode != 0 or current.stdout != expected_selector:
                source_changed = True
        exists = _run.run_git(
            ['cat-file', '-e', f'{record.pinned.commit}^{{commit}}'],
            cwd=ctx.project.source_path,
            check=False,
        )
        source_available = exists.returncode == 0
        if not source_available:
            causes.append(
                'The source commit used to prepare this snapshot is no longer available.'
            )
        if source_changed or verbose:
            details.append(
                terminal_field(
                    'Source revision changed since preparation',
                    'yes' if source_changed else 'no',
                    kind='warning' if source_changed else 'plain',
                )
            )
        details.extend(causes)
    return details, causes, source_changed, source_available


def command_status(args: argparse.Namespace) -> None:
    """Report local snapshot, invalidation, and optional remote state."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        record = _candidate.load_candidate(paths, name)
        details, causes, source_changed, source_available = _status_candidate(
            ctx, record, verbose=args.verbose
        )
        if args.remote:
            _context.require_advisory_store(paths, name)
        main, tags = _context.local_refs(ctx)
        verified_against_host = False
        destination = None
        unresolved = getattr(record, 'state', None) == _candidate.STATE_UNKNOWN_OUTCOME
        comparison: str | None = None
        if args.remote:
            destination = _host.verify_destination(ctx.paths, ctx.global_config, ctx.project)
            remote = _host.ls_remote(destination, paths, name)
            _context.refuse_unexpected_branches(remote)
            comparison = 'match' if (remote.main, remote.tags) == (main, tags) else 'mismatch'
            details.append(
                'GitHub matches the recorded published snapshot.'
                if comparison == 'match'
                else 'GitHub differs from the recorded published snapshot.'
            )
            differing_tags = [
                tag
                for tag in sorted(tags.keys() | remote.tags.keys())
                if tags.get(tag) != remote.tags.get(tag)
            ]
            if differing_tags:
                details.append(f'{style("Tags that differ:")} {escape(", ".join(differing_tags))}')
            verified_against_host = comparison == 'match'
        published = _publication_status(
            ctx, main, tags, destination if verified_against_host else None, verbose=args.verbose
        )
        action_index = (
            unresolved,
            comparison == 'mismatch',
            record is None,
            bool(causes),
            source_changed,
            True,
        ).index(True)
        next_actions = (
            'pubanon publish --resolve',
            'pubanon repo verify',
            'pubanon prepare',
            _candidate.reprepare_advice(record, project=name)
            if source_available and record is not None and record.replaces is not None
            else 'pubanon discard && pubanon prepare',
            'pubanon inspect',
            'pubanon inspect',
        )
        action = next_actions[action_index]
        if (
            action == 'pubanon discard && pubanon prepare'
            and record is not None
            and record.replaces is None
        ):
            action = _ordinary_reprepare_advice(ctx, record)
        if (
            record is not None
            and record.replaces is not None
            and not unresolved
            and comparison != 'mismatch'
            and not causes
        ):
            if record.tip is None or _records.load_rewrite(paths, name) is not None:
                action = _context.project_command(
                    name, 'prepare', f'--project={name}', '--continue'
                )
            else:
                action = 'pubanon inspect'
        copies = _candidate.inspection_paths(paths, name)
        heading(f'Project {escape(name)} ({escape(ctx.project.publication_route)} route)')
        fields(
            [
                (
                    'Prepared',
                    escape(record.tag_intent or 'untagged') if record is not None else 'none',
                ),
                *published,
                ('Review copies', str(len(copies))),
            ]
        )
        if details:
            print('\n'.join(details))
        if args.verbose and copies:
            for copy in copies:
                print(terminal_field('Review copy', display_path(copy)))
        print()
        next_step(action)
