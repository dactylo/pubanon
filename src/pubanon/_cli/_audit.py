"""Audit locally recorded published history and optional host refs."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from pubanon import _config, _history_output
from pubanon._cli import _context
from pubanon._errors import PubanonError
from pubanon._gate import RuleClass
from pubanon._remote import _actions, _host
from pubanon._snapshots import _audit, _candidate, _records
from pubanon._terminal import escape, output

if TYPE_CHECKING:
    import argparse

_AUDIT_COMMAND = 'pubanon audit --remote'


def _audit_actions(ctx: _context.ProjectContext, destination: _host.Destination) -> None:
    """Check the final repository's Actions content and offer to delete matching logs."""
    g, p = ctx.global_config, ctx.project
    account = g.accounts[p.host]
    derived = _config.derived_private_terms(ctx.paths, p, os.environ)
    review = _actions.check(
        (p.owner, p.repo), account, _config.assemble_rules(g, p, derived), command=_AUDIT_COMMAND
    )
    if not review.findings:
        output(_actions.clean_sentence(review))
    _actions.offer_log_deletion(
        review, (g, p), derived, account, already_public=destination.visibility == 'public'
    )
    inventory = _host.disclosure(p.owner, p.repo)
    _actions.show_unchecked(
        (p.owner, p.repo),
        [entry.identifier for entry in inventory.runs],
        artifacts=len(inventory.artifacts),
        caches=_host.actions_cache_count(p.owner, p.repo),
    )


def _audit_report(
    ctx: _context.ProjectContext,
    main: str | None,
    tags: dict[str, str],
    *,
    verified_against_host: bool,
) -> list[str]:
    """Return current-policy findings over the local published history."""
    if main is None:
        return ['No recorded snapshot history to check.']
    commits = _audit.published_commits(ctx.paths, ctx.project.name, new=main)
    if not commits:
        return ['No recorded snapshot history to check.']
    derived = _config.derived_private_terms(ctx.paths, ctx.project, os.environ)
    rules = _config.assemble_rules(ctx.global_config, ctx.project, derived)
    result = _audit.audit_history(
        ctx.paths,
        ctx.project.name,
        commits,
        tags,
        rules,
        f'{ctx.global_config.anon_name} <{ctx.global_config.anon_email}>',
        tip=main,
    )
    label = 'audit: local copy of published history'
    if verified_against_host:
        label += '; verified against GitHub'
    lines = [
        label,
        *_history_output.finding_groups(
            result, (ctx.global_config, ctx.project), derived, commits, tags
        ),
    ]
    has_exposure = any(
        finding.rule_class is RuleClass.IDENTITY_CRITICAL for finding in result.findings
    )
    lines.extend(f'identity problem: {escape(problem)}' for problem in result.identity_problems)
    if result.identity_problems:
        lines.append('Content auditing could not be completed reliably.')
    elif not has_exposure:
        lines.append('No configured sensitive terms were found in the checked history.')
    elif _audit.exposure_digest(result.findings) == ctx.project.acknowledged_exposure:
        lines.append('Historical exposure acknowledged.')
    else:
        lines.append(
            'Historical exposure unacknowledged; publish requires interactive acknowledgment.'
        )
    if result.findings and not result.identity_problems and ctx.project.visibility == 'private':
        lines.append(
            'pubanon repo reveal lists these findings and asks before making them public.'
        )
    if has_exposure:
        lines.append(
            'warning: if this history has been public, deleting or rewriting it does not erase '
            'copies, forks, or caches.'
        )
    return lines


def command_audit(args: argparse.Namespace) -> None:
    """Audit published history; with --remote, verify host refs and check Actions logs."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        record = _records.load_candidate(paths, name)
        _candidate.require_known_outcome(record)
        if args.remote:
            _context.require_advisory_store(paths, name)
        main, tags = _candidate.published_refs(ctx.paths, ctx.project.name)
        destination = None
        if args.remote:
            destination = _host.verify_destination(ctx.paths, ctx.global_config, ctx.project)
            remote = _host.ls_remote(destination, paths, name)
            _context.refuse_unexpected_branches(remote)
            if (remote.main, remote.tags) != (main, tags):
                raise PubanonError(
                    'GitHub branches and tags do not match saved published history',
                    expected=escape(_host.remote_refs_text(_host.RemoteRefs(main, tags, ()))),
                    actual=escape(_host.remote_refs_text(remote)),
                    recovery='pubanon repo verify; use pubanon repo restore --to local '
                    'only if GitHub is authoritative, or see "Accepting an outside fix" '
                    'in the operator guide if GitHub changed outside pubanon',
                )
        print('\n'.join(_audit_report(ctx, main, tags, verified_against_host=args.remote)))
        if destination is not None:
            _audit_actions(ctx, destination)
