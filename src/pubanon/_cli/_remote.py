"""Adapt publication, repository, stage, and watch commands."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from pubanon import (
    _config,
)
from pubanon._cli import (
    _context,
)
from pubanon._errors import PubanonError
from pubanon._remote import _host, _lifecycle, _publish, _stage, _watch
from pubanon._snapshots import _audit, _candidate, _records
from pubanon._terminal import (
    escape,
    heading,
    next_step,
    output,
    style,
)

if TYPE_CHECKING:
    import argparse


def _report_store_repair(
    result: _publish.RepairResult, ctx: _context.ProjectContext, *, changed: bool
) -> None:
    """Report local restoration and findings without acknowledgment."""
    output(
        (
            'Rebuilt the local advisory store from verified host refs.'
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
    for label, detail in details.items():
        print(f'{style(label.rsplit(" ", 1)[0] + ":")} {escape(detail)}')
    if result.audit.findings:
        output('Repair did not acknowledge these findings.', kind='warning')


def publish_context(
    ctx: _context.ProjectContext,
    record: _candidate.CandidateRecord,
    *,
    yes: bool,
    unattended: bool,
    verbose: bool = False,
) -> _publish.PublishContext:
    """Build a publication context from concrete typed collaborators."""
    publish_module = _publish

    return publish_module.PublishContext(
        ctx.paths, ctx.global_config, ctx.project, record, yes, unattended, verbose
    )


def _verify_repo_settings(
    ctx: _context.ProjectContext, *, repaired: bool, review_findings: bool = False
) -> str:
    """Check declared host settings with truthful local-restoration context."""
    if ctx.project.settings is None:
        raise PubanonError('repository settings are missing')
    failure = (
        'local advisory store was rebuilt; host settings verification failed'
        if repaired
        else 'host settings do not match the declared settings'
    )
    recovery = 'pubanon repo verify' if repaired else None
    if review_findings:
        recovery = (
            'pubanon repo verify; then run `pubanon status --audit --remote` '
            'and review the findings'
        )
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
    return 'Settings: match'


def command_publish(args: argparse.Namespace) -> None:
    """Publish, resolve, or synchronize published history."""
    publish_module = _publish
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        if args.sync:
            publish_module.sync(
                publish_module.SyncContext(
                    paths, ctx.global_config, ctx.project, args.yes, args.yes
                )
            )
            return
        record = _candidate.load_candidate(paths, name)
        if record is None:
            raise PubanonError('no prepared snapshot is retained', recovery='pubanon prepare')
        publication_context = publish_context(
            ctx,
            record,
            yes=args.yes,
            unattended=args.yes,
            verbose=args.verbose,
        )
        if args.resolve:
            outcome = publish_module.resolve(publication_context)
            if outcome == 'published':
                output(
                    'The snapshot was published and verified on the host repository.',
                    kind='success',
                )
                print('Local snapshot state was finalized.')
                publish_module.report_source_preservation(paths, name, record.commit)
                print()
                next_action = publish_module.publication_next_action(ctx.project, record)
            else:
                output(
                    'The repository still points to the previous published snapshot.',
                    kind='warning',
                )
                print('Your snapshot is available to publish.')
                print()
                next_action = 'pubanon publish'
            if record.replaces is None:
                print(f'{style("Details:")} snapshot commit {record.commit}')
            else:
                print(
                    f'{style("Details:")} correction {record.commit}; '
                    f'publication tip {_records.candidate_tip(record)}'
                )
            next_step(next_action)
        else:
            publish_module.publish(publication_context)


def repo_create(args: argparse.Namespace) -> None:
    """Create and verify the configured host repository."""
    lifecycle = _lifecycle
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        lifecycle.repo_create(
            paths,
            ctx.global_config,
            ctx.project,
            visibility=args.visibility or ctx.project.visibility,
            yes=args.yes,
        )


def repo_reveal(args: argparse.Namespace) -> None:
    """Reveal or reverify the configured host repository."""
    lifecycle = _lifecycle
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        lifecycle.reveal(paths, ctx.global_config, ctx.project)


def stage_clean(args: argparse.Namespace) -> None:
    """Clean the recorded staging target or the configured fallback."""
    stage_module = _stage
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        stage_module.stage_clean(paths, ctx.global_config, ctx.project, yes=args.yes)


def stage_push(args: argparse.Namespace) -> None:
    """Push one retained snapshot to the staging repository."""
    stage_module = _stage
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        stage_module.stage_push(paths, ctx.global_config, ctx.project, yes=args.yes)


def stage_status(args: argparse.Namespace) -> None:
    """Report required checks for the recorded staged commit."""
    stage_module = _stage
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        stage_module.stage_status(paths, ctx.global_config, ctx.project)


def verify_repo(args: argparse.Namespace) -> None:
    """Compare host repository facts with the local publication expectation."""
    host = _host
    publish_module = _publish
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)

    def _verify() -> None:
        ctx = _context.load_context(paths, name)

        repair_changed = False
        repair_findings: tuple[_audit.AuditFinding, ...] = ()
        if args.repair_store:
            publish_module.check_store_repair_candidate(paths, name)
        else:
            _context.require_advisory_store(paths, name)
        destination = host.verify_destination(ctx.paths, ctx.global_config, ctx.project)
        remote = host.ls_remote(destination, paths, name)
        _context.refuse_unexpected_branches(remote)
        local_main, local_tags = _context.local_refs(ctx)
        local_refs = remote._replace(main=local_main, tags=local_tags, branches=())
        if args.repair_store:
            if remote.main is None:
                raise PubanonError(
                    'host main is absent; the local advisory store was not changed',
                    recovery=(
                        'restore the host main branch, then rerun '
                        'pubanon repo verify --repair-store'
                    ),
                )
            repaired = publish_module.rebuild_store(
                paths, (ctx.global_config, ctx.project), destination, yes=args.yes
            )
            repair_changed = repaired.refs != local_refs
            local_refs = remote = repaired.refs
            repair_findings = repaired.audit.findings
            _report_store_repair(repaired, ctx, changed=repair_changed)
        elif (remote.main, remote.tags) != (local_main, local_tags):
            raise PubanonError(
                'Saved snapshot history differs from GitHub.',
                expected=escape(host.remote_refs_text(local_refs)),
                actual=escape(host.remote_refs_text(remote)),
                recovery=(
                    'If local snapshot history is correct, review pubanon publish --sync. '
                    'If GitHub history is correct, review pubanon repo verify --repair-store. '
                    'For outside changes, follow the guide outside-fix procedure '
                    '(docs/guide.md#accepting-an-outside-fix).'
                ),
            )
        details = [
            f'Repository: exists; visibility={destination.visibility}',
            f'Expected refs: {escape(host.remote_refs_text(local_refs))}',
            f'Host refs: {escape(host.remote_refs_text(remote))}',
        ]
        details.append(
            _verify_repo_settings(
                ctx, repaired=repair_changed, review_findings=bool(repair_findings)
            )
        )
        if not args.repair_store:
            output('Host repository matches the expected snapshot state.', kind='success')
            print()
        heading('Details:')
        for detail in details:
            print(f'  {detail}')
        print()
        if repair_findings:
            next_step('pubanon status --audit --remote')

    with _config.project_lock(paths, name):
        _verify()


def watch_ci(args: argparse.Namespace) -> None:
    """Wait for required CI checks on one selected commit."""
    if args.staged and args.version is not None:
        raise PubanonError('the staging repository holds snapshots, not releases')
    watch_module = _watch
    timeout = args.timeout if args.timeout is not None else watch_module.DEFAULT_TIMEOUT
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        watch_module.watch_ci(
            paths,
            ctx.global_config,
            ctx.project,
            staged=args.staged,
            version=args.version,
            timeout=timeout,
        )


def watch_package(args: argparse.Namespace) -> None:
    """Wait for one selected package release."""
    watch_module = _watch
    timeout = args.timeout if args.timeout is not None else watch_module.DEFAULT_TIMEOUT
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        watch_module.watch_package(
            paths, ctx.global_config, ctx.project, version=args.version, timeout=timeout
        )
