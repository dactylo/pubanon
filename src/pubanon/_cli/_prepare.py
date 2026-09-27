"""Plan and prepare retained snapshots."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, cast

from pubanon import (
    _config,
    _run,
)
from pubanon._cli import _context, _selection, _status
from pubanon._errors import OperationCanceled, PubanonError
from pubanon._gate import first_match, gate_unit
from pubanon._snapshots import _candidate, _records, _rewrite, _source
from pubanon._terminal import (
    confirm,
    detail,
    detail_fields,
    escape,
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
    from collections.abc import Mapping

    from pubanon._gate import RuleClass


@dataclass(frozen=True)
class PrepareOptions:
    """Hold immutable operator selections for one prepare invocation.

    Attributes:
        source: Source revision to pin before reading committed files.
        version: Requested release version, or None for automatic detection.
        message: Commit-message override, or None for the generated message.
        acknowledge_new_paths: Whether inclusion of new paths is acknowledged.
        yes: Whether preparation must proceed without interactive confirmation.
        replacement: Published-snapshot selector to replace, or None for a new
            snapshot.
    """

    source: str
    version: str | None
    message: str | None
    acknowledge_new_paths: bool
    yes: bool
    replacement: str | None = None


@dataclass(frozen=True)
class PreparePlan:
    """Retain verified prepare inputs while the caller holds the project lock.

    Attributes:
        ctx: Project context after applying accepted selection-policy offers.
        snapshot: Sanitized source entries selected for the candidate.
        pinned: Pinned source revision supplying the snapshot.
        new_paths: Selected paths absent from the previous publication.
        published: Previous published main commit and release-tag mapping.
        version: Resolved release version, or None for an untagged snapshot.
        replaces: Published commit being replaced, or None for a new snapshot.
        replace_scope: Replacement extent, or None for a new snapshot.
    """

    ctx: _context.ProjectContext
    snapshot: _source.Snapshot
    pinned: _source.PinnedSource
    new_paths: list[str]
    published: tuple[str | None, dict[str, str]]
    version: str | None
    replaces: str | None = None
    replace_scope: Literal['one', 'all'] | None = None


def _continue_rewrite(ctx: _context.ProjectContext) -> _candidate.CandidateRecord:
    """Resume saved inputs and retain exact replacement advice on invalidation."""
    try:
        return _rewrite.continue_rewrite(ctx.paths, ctx.global_config, ctx.project, env=os.environ)
    except PubanonError as error:
        if error.recovery == 'pubanon discard, then pubanon prepare':
            record = _candidate.load_candidate(ctx.paths, ctx.project.name)
            if record is not None and record.replaces is not None:
                error.recovery = _candidate.reprepare_advice(record, project=ctx.project.name)
        raise


def _gate_excerpt(data: bytes, start: int, end: int) -> str:
    """Return bounded, escaped source context around the matching text."""
    line_start = data.rfind(b'\n', 0, start) + 1
    line_end = data.find(b'\n', end)
    if line_end < 0:
        line_end = len(data)
    left = max(line_start, start - 60)
    right = min(line_end, end + 60)
    excerpt = escape(data[left:right].rstrip(b'\r').decode('utf-8', errors='replace'))
    if left > line_start:
        excerpt = '...' + excerpt
    if right < line_end:
        excerpt += '...'
    return excerpt


def _file_gate_findings(entry: _source.Entry, rules: Mapping[RuleClass, tuple[str, ...]]) -> str:
    """Show blocked filename text and committed source excerpts for one included file."""
    findings: list[str] = []
    filename_terms = dict.fromkeys(
        hit.term for hit in gate_unit(entry.path, entry.path.encode(), rules)
    )
    if filename_terms:
        findings.append(
            f'    {style(escape(entry.path), stream=sys.stderr)} (filename)\n'
            f'      Blocked text: {escape(", ".join(filename_terms))}'
        )
    lines: dict[int, list[tuple[int, int, str]]] = {}
    for hit in gate_unit(entry.path, entry.data, rules):
        span = first_match(entry.data, hit.term)
        if span is None:
            raise PubanonError('gate match location could not be determined')
        start, end = span
        line = entry.data[:start].count(b'\n') + 1
        lines.setdefault(line, []).append((start, end, hit.term))
    for line, matches in sorted(lines.items()):
        excerpt = _gate_excerpt(
            entry.data,
            min(start for start, _end, _term in matches),
            max(end for _start, end, _term in matches),
        )
        terms = dict.fromkeys(term for _start, _end, term in matches)
        findings.append(
            f'    {style(f"{escape(entry.path)}:{line}", stream=sys.stderr)}\n'
            f'      {excerpt}\n'
            f'      Blocked text: {escape(", ".join(terms))}'
        )
    return '\n\n'.join(findings)


def _new_paths(
    ctx: _context.ProjectContext,
    snapshot: _source.Snapshot,
    main: str | None,
) -> list[str]:
    """Return sanitized paths absent from the last local published state."""
    if main is None:
        return [entry.path for entry in snapshot]
    old_paths = set(_candidate.tree_paths(ctx.paths, ctx.project.name, main))
    return [entry.path for entry in snapshot if entry.path not in old_paths]


def _publication_version(snapshot: _source.Snapshot, requested: str | None) -> str | None:
    """Resolve the release version from selected committed package metadata."""
    entry = next(
        (item for item in snapshot if item.path == 'pyproject.toml' and item.kind == 'blob'),
        None,
    )
    if entry is None:
        return requested
    try:
        document = tomllib.loads(entry.data.decode('utf-8'))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise PubanonError(
            'cannot read the version from committed pyproject.toml',
            recovery='fix and commit pyproject.toml, then prepare again',
        ) from error
    project = document.get('project')
    if not isinstance(project, dict) or 'version' not in project:
        return requested
    version = cast('dict[str, object]', project)['version']
    if not isinstance(version, str):
        raise PubanonError(
            'committed pyproject.toml project.version must be a string in X.Y.Z form'
        )
    try:
        _candidate.tag_intent_for(version)
    except PubanonError as error:
        raise PubanonError(
            'committed pyproject.toml project.version must have X.Y.Z form',
            actual=escape(version),
        ) from error
    if requested is not None and requested != version:
        raise PubanonError(
            'release version does not match committed pyproject.toml',
            expected=escape(version),
            actual=escape(requested),
            recovery=(
                'omit --version to use the committed package version, or update and commit '
                'project.version before preparing that release'
            ),
        )
    return version


def _require_no_prepared_candidate(ctx: _context.ProjectContext) -> None:
    """Keep the saved snapshot available and explain how to continue."""
    record = _candidate.load_candidate(ctx.paths, ctx.project.name)
    selection = f'--project={ctx.project.name}'
    if record is None:
        if _records.load_rewrite(ctx.paths, ctx.project.name) is not None:
            raise PubanonError(
                'a rewrite is already retained',
                recovery=(
                    f'{_context.project_command(ctx.project.name, "discard", selection)}, then '
                    f'{_context.project_command(ctx.project.name, "prepare", selection)}'
                ),
            )
        return
    if record.state == _candidate.STATE_UNKNOWN_OUTCOME:
        raise PubanonError(
            'Pubanon could not confirm whether the prepared snapshot was published',
            recovery=_context.project_command(ctx.project.name, 'publish', selection, '--resolve'),
        )
    if record.replaces is not None and record.tip is None:
        raise PubanonError(
            'a rewrite is already retained',
            recovery=(
                f'{_context.project_command(ctx.project.name, "prepare", selection, "--continue")}'
                '; '
                'to replace it: '
                f'{_context.project_command(ctx.project.name, "discard", selection)}, '
                f'then {_context.project_command(ctx.project.name, "prepare", selection)}'
            ),
        )
    version = f'for {escape(record.tag_intent)}' if record.tag_intent else 'without a version tag'
    raise PubanonError(
        f'a snapshot is already prepared {version}',
        recovery=(
            f'to use it: {_context.project_command(ctx.project.name, "inspect", selection)}, then '
            f'{_context.project_command(ctx.project.name, "publish", selection)}; '
            'to replace it: '
            f'{_context.project_command(ctx.project.name, "discard", selection)}, '
            f'then {_context.project_command(ctx.project.name, "prepare", selection)}'
        ),
    )


def _run_gates(ctx: _context.ProjectContext, snapshot: _source.Snapshot) -> None:
    """Run active gates over every retained path and byte payload."""
    _config.check_publication_state_paths(
        ctx.paths, ctx.project, [entry.path for entry in snapshot]
    )
    derived = _config.derived_private_terms(ctx.paths, ctx.project, os.environ)
    rules = _config.assemble_rules(ctx.global_config, ctx.project, derived)
    groups = [details for entry in snapshot if (details := _file_gate_findings(entry, rules))]
    if groups:
        count = len(groups)
        subject = (
            '1 file selected for publication needs'
            if count == 1
            else (f'{count} files selected for publication need')
        )
        raise PubanonError(
            'cannot prepare: blocked text in included files',
            fact=(
                f'{subject} changes:\n\n'
                + '\n\n'.join(groups)
                + '\n\n  Locations refer to the selected commit. '
                'The first match for each blocked term is shown.'
            ),
            recovery=(
                'Edit these files to remove the blocked text, or exclude them from publication.\n'
                '  Commit source edits before rerunning pubanon prepare.'
            ),
        )


def _show_source_selection(
    ctx: _context.ProjectContext, options: PrepareOptions, pinned: _source.PinnedSource
) -> None:
    """Identify the committed selection and count omitted working changes."""
    changes = _source.working_changes(ctx.project.source_path)
    selector = (pinned.selector_name or options.source).removeprefix('refs/heads/')
    commit = pinned.commit if verbose_enabled() else pinned.commit[:12]
    omitted = ', '.join(
        f'{count} {label} {"change" if count == 1 else "changes"}'
        for label, count in (
            ('staged', changes.staged),
            ('unstaged', changes.unstaged),
            ('untracked', changes.untracked),
        )
        if count
    )
    suffix = f' (left out: {omitted})' if omitted else ''
    print(terminal_field('Source', f'{escape(selector)} at {commit}{suffix}'))
    rows = [('Selected source', escape(options.source))]
    if pinned.selector_name is not None:
        rows.append(('Selected source ref', escape(pinned.selector_name)))
    detail_fields(rows)
    if options.source != 'HEAD':
        current = _run.run_git(
            ['rev-parse', '--verify', 'HEAD^{commit}'], cwd=ctx.project.source_path, check=False
        )
        if current.returncode != 0:
            detail('Using the selected revision; current HEAD is unavailable.')
        elif current.stdout != f'{pinned.commit}\n'.encode():
            detail(
                'Selected revision differs from current HEAD; only the selected commit is used.'
            )
        else:
            detail('The selected revision currently matches HEAD.')
    detail('Only committed files are selected. Run `git status` for working-change details.')


def build_candidate(plan: PreparePlan, options: PrepareOptions) -> _candidate.CandidateRecord:
    """Build a snapshot from an unchanged plan while holding its project lock."""
    record = _candidate.build_candidate(
        plan.ctx.paths,
        plan.ctx.global_config,
        plan.ctx.project,
        _candidate.CandidateInputs(
            plan.snapshot,
            plan.pinned,
            plan.version,
            options.message,
            prestate=plan.published,
            replaces=plan.replaces,
            replace_scope=plan.replace_scope,
        ),
    )
    if record.replaces is not None:
        return record
    tag = record.tag_intent or 'untagged'
    count = len(plan.snapshot)
    print()
    output(
        f'Prepared snapshot {escape(tag)} with {count} file{"s" if count != 1 else ""}.',
        kind='success',
    )
    detail_fields([('Operation', 'new')])
    return record


def plan_prepare_locked(
    ctx: _context.ProjectContext,
    options: PrepareOptions,
) -> PreparePlan:
    """Verify and retain snapshot inputs while holding the project lock."""
    _config.gate_host_coordinates(
        ctx.paths,
        ctx.global_config,
        ctx.project,
        coordinates=(ctx.project.owner, ctx.project.repo),
    )
    _config.gate_host_coordinates(
        ctx.paths,
        ctx.global_config,
        ctx.project,
        coordinates=(ctx.project.owner, _config.stage_repository(ctx.project)),
    )
    _require_no_prepared_candidate(ctx)
    replaces, scope = (
        _rewrite.resolve_replacement(ctx.paths, ctx.project.name, options.replacement)
        if options.replacement is not None
        else (None, None)
    )
    tag_intent = _candidate.tag_intent_for(options.version)
    published = _candidate.published_refs(ctx.paths, ctx.project.name)
    moved_tags = tuple(
        tag for tag, oid in published[1].items() if oid == replaces and scope == 'one'
    )
    if tag_intent is not None and tag_intent in published[1] and scope != 'all' and not moved_tags:
        raise PubanonError(
            'intended tag is already published',
            recovery=(
                'choose a new version; to correct the snapshot that carries this tag: '
                + _context.project_command(
                    ctx.project.name,
                    'prepare',
                    f'--project={ctx.project.name}',
                    '--replace',
                    tag_intent,
                )
            ),
        )
    _source.check_source_safety(ctx.project.source_path)
    pinned = _source.pin(ctx.project.source_path, options.source)
    _show_source_selection(ctx, options, pinned)
    snapshot = _source.read_snapshot(ctx.project.source_path, pinned.commit)
    ctx = _selection.review_selection(ctx, snapshot, yes=options.yes)
    sanitized = _selection.sanitized_snapshot(ctx, snapshot)
    try:
        version = _candidate.replacement_version(
            _publication_version(sanitized, options.version), moved_tags
        )
    except PubanonError as error:
        if str(error) != 'replacement version must match a moved tag':
            raise
        raise PubanonError(
            str(error),
            recovery='commit the version correction or publish a new version',
        ) from None
    inferred_tag = _candidate.tag_intent_for(version)
    if (
        inferred_tag is not None
        and inferred_tag in published[1]
        and scope != 'all'
        and not moved_tags
    ):
        raise PubanonError(
            'intended tag is already published',
            fact=f'tag: {escape(inferred_tag)}',
            recovery=(
                'update and commit the package version before preparing a new release; '
                'to correct the snapshot that carries this tag: '
                + _context.project_command(
                    ctx.project.name,
                    'prepare',
                    f'--project={ctx.project.name}',
                    '--replace',
                    inferred_tag,
                )
            ),
        )
    new_paths = _new_paths(ctx, sanitized, published[0])
    if new_paths:
        _selection.show_new_paths(ctx, new_paths, show=published[0] is not None)
    _run_gates(ctx, sanitized)
    return PreparePlan(ctx, sanitized, pinned, new_paths, published, version, replaces, scope)


def prepare_locked(
    ctx: _context.ProjectContext,
    options: PrepareOptions,
) -> _candidate.CandidateRecord:
    """Prepare one snapshot while the caller holds the project lock."""
    plan = plan_prepare_locked(ctx, options)
    first_release = plan.published[0] is None
    if plan.new_paths and not first_release and not options.acknowledge_new_paths:
        if options.yes:
            raise PubanonError(
                '--yes cannot acknowledge new paths',
                recovery=(
                    'run interactive pubanon prepare, or rerun with '
                    '--acknowledge-new-paths after reviewing the complete list'
                ),
            )
        if not confirm(
            f'Include these {len(plan.new_paths)} files that were not in the last release?'
        ):
            raise OperationCanceled('Canceled. Nothing was created.')
    return build_candidate(plan, options)


def command_prepare(args: argparse.Namespace) -> None:
    """Prepare one retained snapshot without host contact."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        if args.continue_rewrite:
            record = _continue_rewrite(ctx)
        else:
            record = prepare_locked(
                ctx,
                PrepareOptions(
                    args.source or 'HEAD',
                    args.version,
                    args.message,
                    args.acknowledge_new_paths,
                    args.yes,
                    args.replace,
                ),
            )
            if record.replaces is not None and record.tip is None:
                ctx = _context.load_context(paths, name)
                record = _continue_rewrite(ctx)
            elif record.replaces is not None:
                ctx = _context.load_context(paths, name)
                _candidate.verify_retained_candidate(
                    paths, ctx.global_config, ctx.project, record, env=os.environ
                )
        if record.replaces is not None:
            heading(
                'Prepared snapshot'
                if record.tip is not None
                else 'Snapshot preparation incomplete'
            )
            print(terminal_field('Version', escape(record.tag_intent or 'untagged')))
            detail_fields([('Operation', 'replacement')])
            print('\n'.join(_status.rewrite_status(paths, name, record, verbose=args.verbose)))
            if record.tip is None:
                next_step(
                    _context.project_command(name, 'prepare', f'--project={name}', '--continue')
                )
                return
    next_step('pubanon inspect')
