"""Audit locally recorded published history and optional host refs."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from pubanon import _config
from pubanon._cli import _context
from pubanon._errors import PubanonError
from pubanon._gate import GateHit, RuleClass
from pubanon._remote import _host
from pubanon._snapshots import _audit, _candidate
from pubanon._terminal import color_enabled, escape, style, verbose_enabled

if TYPE_CHECKING:
    import argparse
    from collections.abc import Collection, Sequence


def _finding_lines(evidence: _audit.HistoryEvidence, origins: tuple[str, ...]) -> list[str]:
    """Render one escaped finding with its location before its supporting evidence."""
    finding = evidence.finding
    location = evidence.label
    if finding.unit.startswith('path:') and (
        evidence.label == 'filename' or evidence.excerpt is None
    ):
        location = f'{finding.unit.removeprefix("path:")} ({location})'
    elif finding.unit.startswith('tag:'):
        location = f'tag {finding.unit.removeprefix("tag:")}'
    sources = '; '.join(label.replace(' entry ', ', item ') for label in origins)
    lines = [
        '  ' + style(escape(location), 'label'),
        f'    Rule: {escape(sources)}',
    ]
    excerpt = evidence.excerpt
    span = evidence.excerpt_span if color_enabled() else None
    if excerpt is None or span is None:
        lines.append('    Matched: ' + style(escape(finding.term), 'warning'))
    if excerpt is not None:
        if span is None:
            text = escape(excerpt)
        else:
            start, end = span
            text = (
                escape(excerpt[:start])
                + style(escape(excerpt[start:end]), 'warning')
                + escape(excerpt[end:])
            )
        lines.append(f'    Text: {text}')
    return lines


def _snapshot_heading(commit: str | None, tags: dict[str, str]) -> str:
    """Label the witness snapshot with its published tags and commit ID."""
    identity = 'unknown'
    if commit is not None:
        identity = commit if verbose_enabled() else commit[:12]
        versions = ', '.join(name for name, target in tags.items() if target == commit)
        if versions:
            identity = f'{versions} ({identity})'
    return style(f'Published snapshot: {escape(identity)}', 'label')


def _finding_groups(
    result: _audit.HistoryAudit,
    ctx: _context.ProjectContext,
    derived: Collection[str],
    commits: Sequence[str],
    tags: dict[str, str],
) -> list[str]:
    """Group each warning category by witness snapshot, newest first."""
    lines: list[str] = []
    for rule_class, heading in (
        (RuleClass.IDENTITY_CRITICAL, 'historical exposure'),
        (RuleClass.HYGIENE, 'policy mismatch'),
    ):
        groups: dict[str | None, list[_audit.HistoryEvidence]] = {}
        for item in result.evidence:
            if item.finding.rule_class is rule_class:
                groups.setdefault(item.commit, []).append(item)
        if not groups:
            continue
        lines.extend(('', style(style(f'Warning: {heading}', 'warning'), 'label')))
        for commit in (*commits, None):
            if commit not in groups:
                continue
            lines.extend(('', _snapshot_heading(commit, tags)))
            for item in groups[commit]:
                origins = _config.rule_labels(
                    GateHit(*item.finding), ctx.global_config, ctx.project, derived
                )
                lines.extend(('', *_finding_lines(item, origins)))
    if lines:
        lines.append('')
    return lines


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
        ctx.paths, ctx.project.name, commits, tags, rules, ctx.global_config, tip=main
    )
    label = 'audit: local copy of published history'
    if verified_against_host:
        label += '; verified against GitHub'
    lines = [label, *_finding_groups(result, ctx, derived, commits, tags)]
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
    if has_exposure:
        lines.append(
            'warning: if this history has been public, deleting or rewriting it does not erase '
            'copies, forks, or caches.'
        )
    return lines


def command_audit(args: argparse.Namespace) -> None:
    """Audit recorded published history, verifying host refs when requested."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        record = _candidate.load_candidate(paths, name)
        if getattr(record, 'state', None) == _candidate.STATE_UNKNOWN_OUTCOME:
            raise PubanonError(
                'the outcome of the last publication is unknown',
                recovery='pubanon publish --resolve',
            )
        if args.remote:
            _context.require_advisory_store(paths, name)
        main, tags = _context.local_refs(ctx)
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
                    'in the operator guide if GitHub changed outside Pubanon',
                )
        print('\n'.join(_audit_report(ctx, main, tags, verified_against_host=args.remote)))
