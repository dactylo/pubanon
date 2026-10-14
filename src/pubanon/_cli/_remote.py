"""Adapt publication, repository, stage, and watch commands."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from pubanon import (
    _config,
    _history_output,
)
from pubanon._cli import (
    _context,
)
from pubanon._cli import (
    _releases as cli_releases,
)
from pubanon._errors import ObservationTimeoutError, OperationCanceled, PubanonError
from pubanon._remote import _host, _lifecycle, _publish, _releases, _repair, _stage, _watch
from pubanon._snapshots import _audit, _candidate, _records
from pubanon._terminal import (
    detail_fields,
    escape,
    field,
    next_step,
    output,
    style,
)

if TYPE_CHECKING:
    import argparse


def _report_resolved_page(
    ctx: _context.ProjectContext, record: _candidate.CandidateRecord
) -> None:
    """Report read-only page status without changing a resolved Git outcome."""
    if not _config.release_pages_enabled(ctx.project):
        return
    tags = _releases.page_tags(record)
    for tag in tags:
        try:
            page = _releases.page_status(ctx.paths, ctx.global_config, ctx.project, tag)
        except (PubanonError, OSError, ObservationTimeoutError) as exc:
            if isinstance(exc, OperationCanceled):
                raise
            output(
                f'warning: could not look up the release page for {escape(tag)}: '
                f'{escape(str(exc))}',
                kind='warning',
            )
        else:
            output(
                field(
                    'Release page',
                    escape(page.url) if page else f'no page exists for {escape(tag)}',
                )
            )
            if page is None:
                output(_releases.create_command(ctx.project.name, tag))


def _report_store_repair(
    result: _repair.RepairResult, ctx: _context.ProjectContext, *, changed: bool
) -> None:
    """Report local restoration and findings without acknowledgment."""
    output(
        (
            'Rebuilt the saved published history from verified host refs.'
            if changed
            else 'Local publication refs already match verified host refs; '
            'no replacement was needed.'
        ),
        kind='success',
    )
    print('The host repository and source repository were not changed.')

    details = _audit.history_details(
        result.audit,
        ctx.global_config,
        ctx.project,
        _config.derived_private_terms(ctx.paths, ctx.project, os.environ),
    )
    for detail in details:
        label = _history_output.HISTORY_LABELS[detail.rule_class]
        print(f'{style(label + ":")} {escape(detail.description)}')
    if result.audit.findings:
        output('Repair did not acknowledge these findings.', kind='warning')


def _verify_repo_settings(
    ctx: _context.ProjectContext, *, repaired: bool, review_findings: bool = False
) -> str:
    """Check declared host settings with truthful local-restoration context."""
    if ctx.project.settings is None:
        raise PubanonError('repository settings are missing')
    failure = (
        'saved published history was rebuilt; host settings verification failed'
        if repaired
        else 'host settings do not match the declared settings'
    )
    recovery = 'pubanon repo verify' if repaired else None
    if review_findings:
        recovery = 'pubanon repo verify; then run `pubanon audit --remote` and review the findings'
    try:
        facts = _host.repository_settings(ctx.project.owner, ctx.project.repo)
    except PubanonError as exc:
        if repaired or review_findings:
            raise PubanonError(failure, fact=exc.render(), recovery=recovery) from exc
        raise
    mismatches = _lifecycle.settings_mismatches(ctx.project.settings, facts)
    if mismatches:
        raise PubanonError(
            failure,
            fact='; '.join(escape(item) for item in mismatches),
            recovery=recovery,
        )
    return 'Repository settings match your configuration.'


def package_watch(args: argparse.Namespace) -> None:
    """Wait for one selected package release."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        _watch.watch_package(
            paths, ctx.global_config, ctx.project, version=args.version, timeout=args.timeout
        )


def publish_context(
    ctx: _context.ProjectContext,
    record: _candidate.CandidateRecord,
    *,
    yes: bool,
    release_page: _releases.PageRequest | None = None,
) -> _publish.PublishContext:
    """Build publication facts with the CLI confirmation mode.

    Args:
        ctx: Loaded project context under its project lock.
        record: Retained candidate selected for publication.
        yes: Skip confirmation and require unattended-safe publication.
        release_page: Invocation overrides, or None for configured defaults.

    Returns:
        Publication context retaining the supplied page bytes and paths.
    """
    return _publish.PublishContext(
        ctx.paths,
        ctx.global_config,
        ctx.project,
        record,
        yes,
        release_page if release_page is not None else _releases.PageRequest(),
    )


def command_publish(args: argparse.Namespace) -> None:
    """Publish or resolve prepared history."""
    notes = (
        cli_releases.read_notes_file(args.release_notes_file)
        if args.release_notes_file is not None
        else None
    )
    page = _releases.PageRequest(
        skip=args.no_release_page,
        notes=notes,
        notes_file=(
            Path(args.release_notes_file).absolute()
            if args.release_notes_file is not None
            else None
        ),
        requested=args.release_page,
    )
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        record = _records.load_candidate(paths, name)
        if record is None:
            raise PubanonError('no prepared snapshot is retained', recovery='pubanon prepare')
        publication_context = publish_context(
            ctx,
            record,
            yes=args.yes,
            release_page=page,
        )
        if args.resolve:
            outcome = _publish.resolve(publication_context)
            if outcome == 'published':
                output(
                    'The snapshot was published and verified on the host repository.',
                    kind='success',
                )
                print('Local snapshot state was finalized.')
                _report_resolved_page(ctx, record)
                _publish.report_source_preservation(
                    paths, name, record.commit, source_tags=ctx.project.source_tags
                )
                print()
                next_action = _publish.publication_next_action(ctx.project, record)
            else:
                output(
                    'The repository still points to the previous published snapshot.',
                    kind='warning',
                )
                print('Your snapshot is available to publish.')
                print()
                retained = _records.load_candidate(paths, name)
                next_action = _stage.candidate_next_action(
                    paths, ctx.global_config, ctx.project, retained
                )
            if record.replaces is None:
                print(f'{style("Details:")} snapshot commit {record.commit}')
            else:
                print(
                    f'{style("Details:")} correction {record.commit}; '
                    f'publication tip {_records.candidate_tip(record)}'
                )
            next_step(next_action)
        else:
            _publish.publish(publication_context)


def repo_create(args: argparse.Namespace) -> None:
    """Create and verify the configured host repository."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        _lifecycle.repo_create(
            paths,
            ctx.global_config,
            ctx.project,
            yes=args.yes,
        )


def repo_restore(args: argparse.Namespace) -> None:
    """Restore GitHub or the local saved publication history."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        if args.to == 'remote':
            _publish.sync(_publish.SyncContext(paths, ctx.global_config, ctx.project, args.yes))
            return

        _repair.check_store_repair_candidate(paths, name)
        destination = _host.verify_destination(ctx.paths, ctx.global_config, ctx.project)
        remote = _host.ls_remote(destination, paths, name)
        _context.refuse_unexpected_branches(remote)
        local_main, local_tags = _candidate.published_refs(ctx.paths, ctx.project.name)
        local_refs = remote._replace(main=local_main, tags=local_tags, branches=())
        if remote.main is None:
            raise PubanonError(
                'host main is absent; the saved published history was not changed',
                recovery=(
                    'restore the host main branch, then rerun pubanon repo restore --to local'
                ),
            )
        repaired = _repair.rebuild_store(
            paths, (ctx.global_config, ctx.project), destination, yes=args.yes
        )
        repair_changed = repaired.refs != local_refs
        local_refs = remote = repaired.refs
        _report_store_repair(repaired, ctx, changed=repair_changed)
        settings = _verify_repo_settings(
            ctx, repaired=repair_changed, review_findings=bool(repaired.audit.findings)
        )
        output(settings, kind='success')
        detail_fields(
            [
                ('Repository', f'exists; visibility={destination.visibility}'),
                ('Expected refs', escape(_host.remote_refs_text(local_refs))),
                ('Host refs', escape(_host.remote_refs_text(remote))),
            ]
        )
        if repaired.audit.findings:
            next_step('pubanon audit --remote')


def repo_reveal(args: argparse.Namespace) -> None:
    """Reveal or reverify the configured host repository."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        _lifecycle.reveal(paths, ctx.global_config, ctx.project)


def repo_watch(args: argparse.Namespace) -> None:
    """Wait for required CI checks on published main or an exact tag."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        _watch.watch_ci(
            paths,
            ctx.global_config,
            ctx.project,
            staged=False,
            version=args.version,
            timeout=args.timeout,
        )


def stage_delete(args: argparse.Namespace) -> None:
    """Delete the configured private staging repository under the project lock."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        _stage.stage_delete(paths, ctx.global_config, ctx.project, yes=args.yes)


def stage_forget(args: argparse.Namespace) -> None:
    """Forget local staging tracking under the project lock."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        _stage.stage_forget(paths, ctx.project, yes=args.yes)


def stage_push(args: argparse.Namespace) -> None:
    """Push one retained snapshot to the staging repository."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        _stage.stage_push(
            paths,
            ctx.global_config,
            ctx.project,
            yes=args.yes,
            ci=_stage.StageCiOptions(full_tests=args.full_tests, hosted=args.hosted),
        )


def stage_repair(args: argparse.Namespace) -> None:
    """Repair staging tracking under the project lock."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        _stage.stage_repair(paths, ctx.global_config, ctx.project, yes=args.yes)


def stage_status(args: argparse.Namespace) -> None:
    """Report staging facts, tracking, and exact snapshot checks."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        _stage.stage_status(paths, ctx.global_config, ctx.project)


def stage_watch(args: argparse.Namespace) -> None:
    """Wait for required CI checks on the recorded staged snapshot."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        _watch.watch_ci(
            paths,
            ctx.global_config,
            ctx.project,
            staged=True,
            version=None,
            timeout=args.timeout,
        )


def verify_repo(args: argparse.Namespace) -> None:
    """Compare host repository facts with the local publication expectation."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)

    def _verify() -> None:
        ctx = _context.load_context(paths, name)

        _context.require_advisory_store(paths, name)
        destination = _host.verify_destination(ctx.paths, ctx.global_config, ctx.project)
        remote = _host.ls_remote(destination, paths, name)
        _context.refuse_unexpected_branches(remote)
        local_main, local_tags = _candidate.published_refs(ctx.paths, ctx.project.name)
        local_refs = remote._replace(main=local_main, tags=local_tags, branches=())
        if local_main is not None and remote.main is None and not remote.tags:
            record = _records.load_candidate(ctx.paths, ctx.project.name)
            correcting = record is not None and record.replaces is not None
            raise PubanonError(
                'the final repository is empty',
                fact=_publish.empty_destination_fact(destination.owner, destination.repo),
                recovery='pubanon repo restore --to remote'
                + (f'\n{_publish.CORRECTION_RESTORE_NOTE}' if correcting else ''),
            )
        if (remote.main, remote.tags) != (local_main, local_tags):
            raise PubanonError(
                'Saved snapshot history differs from GitHub.',
                expected=escape(_host.remote_refs_text(local_refs)),
                actual=escape(_host.remote_refs_text(remote)),
                recovery=(
                    'If local saved publication history is correct, review '
                    'pubanon repo restore --to remote. If GitHub history is correct, '
                    'review pubanon repo restore --to local. '
                    'For outside changes, follow the guide outside-fix procedure '
                    '(docs/guide.md#accepting-an-outside-fix).'
                ),
            )
        settings = _verify_repo_settings(ctx, repaired=False)
        output(
            f'Final repository {escape(destination.owner)}/{escape(destination.repo)} '
            f'({escape(destination.visibility)}) matches saved published history.',
            kind='success',
        )
        output(settings, kind='success')
        detail_fields(
            [
                ('Repository', f'exists; visibility={destination.visibility}'),
                ('Expected refs', escape(_host.remote_refs_text(local_refs))),
                ('Host refs', escape(_host.remote_refs_text(remote))),
            ]
        )

    with _config.project_lock(paths, name):
        _verify()
