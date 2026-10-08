"""Render retained candidate reports."""

import os
from collections.abc import Sequence
from contextlib import suppress
from pathlib import Path

from pubanon import _filesystem, _policy
from pubanon._config import (
    GlobalConfig,
    Paths,
    ProjectConfig,
    create_data_dirs,
    fingerprint,
)
from pubanon._errors import PubanonError
from pubanon._snapshots import _audit, _candidate, _records, _store, _verification
from pubanon._snapshots._records import (
    STATE_UNKNOWN_OUTCOME,
    CandidateRecord,
)
from pubanon._terminal import (
    display_path,
    escape,
    render_fields,
    shell_command,
    style,
    verbose_enabled,
)

_REWRITE_REPORT_FILE_LIMIT = 20


def _replacement_scope_rows(
    paths: Paths, project: str, record: CandidateRecord
) -> list[tuple[str, str]]:
    """Describe the replacement extent and tags absent from its completed poststate."""
    if record.replace_scope == 'all':
        if record.prestate_main is None:
            raise PubanonError('replacement snapshot is incomplete')
        count = len(_audit.published_commits(paths, project, new=record.prestate_main))
        scope = f'all published history ({count} snapshots) -> one snapshot'
    else:
        scope = 'one published snapshot and its replayed descendants'
    rows = [('Scope', scope)]
    if record.poststate_tags is not None:
        removed = sorted(record.prestate_tags.keys() - record.poststate_tags.keys())
        if removed:
            rows.append(('Removed tags', style(escape(', '.join(removed)), 'warning')))
    return rows


def _rewritten_change_report(
    paths: Paths, project: str, record: CandidateRecord, *, complete: bool = False
) -> str:
    """Describe each replacement against its own original release."""
    if record.replaces is None:
        return ''
    store = _store.candidate_store_path(paths, project)
    pairs: list[tuple[str, str, tuple[_records.Resolution, ...]]] = [
        (record.replaces, record.commit, ())
    ]
    pairs.extend((entry.old, entry.new, entry.resolutions) for entry in record.replayed)
    lines = ['Replaced snapshots:' if complete else style('Replaced snapshots', 'heading')]
    for old, new, resolutions in pairs:
        lines.append(f'  {old if complete else old[:12]} -> {new if complete else new[:12]}')
        changed = (
            _candidate.git_result(
                ['diff', '--name-status', '--no-renames', old, new],
                store=store,
                failure='cannot describe rewritten release changes',
            )
            .decode(errors='surrogateescape')
            .splitlines()
        )
        shown = changed if complete else changed[:_REWRITE_REPORT_FILE_LIMIT]
        # Git quotes tabs within filenames; literal tabs separate report columns.
        lines.extend('    ' + escape(line.replace('\t', '  ')) for line in shown)
        if not complete and len(changed) > _REWRITE_REPORT_FILE_LIMIT:
            lines.append(
                f'    ... and {len(changed) - _REWRITE_REPORT_FILE_LIMIT} more file changes'
            )
        if not changed:
            lines.append('    No file changes relative to the original release.')
        lines.extend(
            f'    Manual resolution: {escape(", ".join(resolution.paths))}'
            for resolution in resolutions
        )
    before = record.prestate_tags
    after = record.poststate_tags or {}
    moved = [tag for tag in sorted(after) if tag in before and before[tag] != after[tag]]
    if moved:
        lines.append('')
        lines.append('Tags moved:' if complete else style('Tags moved', 'heading'))
        lines.extend(
            f'  {escape(tag)}: {before[tag] if complete else before[tag][:12]} -> '
            f'{after[tag] if complete else after[tag][:12]}'
            for tag in moved
        )
    return '\n\n' + '\n'.join(lines)


def _candidate_change_summary(
    paths: Paths, project: str, record: CandidateRecord
) -> tuple[str, Path]:
    """Bound terminal diff statistics and retain a complete private file report."""
    store = _store.candidate_store_path(paths, project)
    args = ['diff-tree', '--no-commit-id', '-r', '--root', '--no-renames']
    tip = record.commit if record.tip is None else _records.candidate_tip(record)
    revisions = [tip] if record.prestate_main is None else [record.prestate_main, tip]
    summary = (
        _candidate.git_result(
            [*args, '--stat', '--stat-count=20', *revisions],
            store=store,
            failure='cannot render snapshot changes',
        )
        .decode(errors='surrogateescape')
        .rstrip('\n')
    )
    complete = _candidate.git_result(
        [*args, '--numstat', *revisions],
        store=store,
        failure='cannot read complete snapshot file report',
    ).decode(errors='surrogateescape')
    rewritten = _rewritten_change_report(paths, project, record, complete=True)
    path = create_data_dirs(paths, project) / f'candidate-files-{record.commit}.txt'
    descriptor = None
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        _filesystem.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, 'w', encoding='utf-8') as handle:
            descriptor = None
            handle.write('Added lines, removed lines, path (binary files use -):\n')
            for line in complete.split('\n'):
                if line:
                    handle.write(escape(line.replace('\t', '  ')) + '\n')
            if rewritten:
                handle.write(rewritten + '\n')
    except (OSError, UnicodeError) as exc:
        if descriptor is not None:
            with suppress(OSError):
                os.close(descriptor)
        raise PubanonError('cannot write complete snapshot file report') from exc
    heading = (
        'first release' if record.prestate_main is None else 'since recorded publication base'
    )
    lines = '\n'.join(
        f'  {escape(line.removeprefix(" "))}'
        for line in (summary or '0 files changed').split('\n')
    )
    return f'{heading}\n{lines}', path


def tree_paths(paths: Paths, project: str, commit: str) -> list[str]:
    """Return the paths of one store commit without reading blob bodies.

    Args:
        paths: Derived pubanon configuration and data paths.
        project: Registered project name.
        commit: Commit or tree object ID in the advisory store.

    Returns:
        Snapshot-relative posix paths in Git order.

    Raises:
        PubanonError: The store read fails or returns malformed paths.
    """
    output = _candidate.git_result(
        ['ls-tree', '-r', '-z', '--name-only', '--full-tree', commit],
        store=_store.candidate_store_path(paths, project),
        failure='cannot read published paths',
    )
    records = output.split(b'\0')
    if records[-1:] != [b'']:
        raise PubanonError('cannot read published paths')
    try:
        return [record.decode('utf-8') for record in records[:-1]]
    except UnicodeDecodeError as exc:
        raise PubanonError('cannot read published paths') from exc


def credential_warning(paths: Paths, project: str, record: CandidateRecord) -> str:
    """Describe credential-like files included in the prepared publication history."""
    commits = (record.commit, *(entry.new for entry in record.replayed))
    secrets = sorted(
        {
            path
            for commit in commits
            for path in _policy.secret_like(tree_paths(paths, project, commit), ())
        }
    )
    if not secrets:
        return ''
    count = len(secrets)
    return style(
        f'warning: Snapshot includes {count} credential-like file{"s" if count != 1 else ""}: '
        + ', '.join(escape(path) for path in secrets)
        + '.',
        'warning',
    )


def _candidate_report_details(
    paths: Paths,
    proof: _verification.ObjectProof,
    p: ProjectConfig,
    *,
    extra_rows: Sequence[tuple[str, str]] = (),
) -> str:
    """Render common factual details from verified object evidence."""
    record = proof.record
    snapshot = proof.snapshot
    identity = escape(proof.identity.decode(errors='surrogateescape'))
    summary, file_list = _candidate_change_summary(paths, p.name, record)
    tip = record.tip if record.tip is not None else record.commit
    tag = 'untagged' if record.tag_intent is None else escape(record.tag_intent)
    if record.state == STATE_UNKNOWN_OUTCOME:
        state = 'publication outcome unknown'
    elif record.replaces is not None and record.tip is None:
        state = 'preparation incomplete'
    else:
        state = 'prepared'
    multi_snapshot = record.replaces is not None and tip != record.commit
    title = f'Prepared snapshots ({state})' if multi_snapshot else f'Snapshot {tag} ({state})'
    rows = [
        (
            'Destination',
            (
                f'{escape(record.destination.owner)}/{escape(record.destination.repo)} '
                f'({escape(record.destination.visibility)})'
            ),
        ),
        ('Identity', identity),
    ]
    if multi_snapshot:
        rows.append(('Correction version', tag))
        final_tags = sorted(
            tag for tag, commit in (record.poststate_tags or {}).items() if commit == tip
        )
        rows.append(('Final version', ', '.join(escape(tag) for tag in final_tags) or 'untagged'))
    message_label = 'Correction message' if multi_snapshot else 'Message'
    message_lines = record.message.rstrip('\n').split('\n')
    message = (
        escape(message_lines[0])
        if len(message_lines) == 1
        else ('\n' + '\n'.join('  ' + escape(line) for line in message_lines))
    )
    rows.append((message_label, message))
    summary_heading, _, rest = summary.partition('\n')
    if record.prestate_main is None:
        summary_heading += f', {len(snapshot)} file{"s" if len(snapshot) != 1 else ""}'
    rows.append(
        ('Final snapshot changes' if multi_snapshot else 'Changes', summary_heading + '\n' + rest)
    )
    if record.replaces is not None:
        width = 40 if verbose_enabled() else 12
        rows.append(('Replaces', record.replaces[:width]))
        rows.extend(_replacement_scope_rows(paths, p.name, record))
        if multi_snapshot:
            rows.extend(
                [
                    ('Correction snapshot ID', record.commit[:width]),
                    ('Final snapshot ID', tip[:width]),
                ]
            )
    rows.extend(extra_rows)
    rows.append(('File list', display_path(file_list)))
    if verbose_enabled():
        rows.extend(
            [
                ('Operation', 'replacement' if record.replaces is not None else 'new'),
                (
                    'Correction source commit' if multi_snapshot else 'Source commit',
                    record.pinned.commit,
                ),
                ('Snapshot commit', tip),
                ('Snapshot tree', record.tree),
            ]
        )
    lines = [style(title, 'heading'), render_fields(rows, align_continuations=False)]
    warning = credential_warning(paths, p.name, record)
    if warning:
        lines.append(warning)
    return '\n'.join(lines)


def inspect_report(
    paths: Paths,
    proof: _verification.ObjectProof,
    g: GlobalConfig,
    p: ProjectConfig,
) -> str:
    """Render the complete candidate inspection report.

    Compare the proven snapshot with the recorded publication prestate.
    Current policy and live host checks are not rerun.
    The returned terminal report bounds its displayed file list and writes a
    separate complete private file report. The caller must hold the project
    lock for the complete operation.

    Args:
        paths: Derived pubanon paths.
        proof: Object evidence obtained for this review step.
        g: Active global configuration.
        p: Active project configuration.

    Returns:
        Publication identity, diff, exception, and gate facts.

    Raises:
        PubanonError: The candidate diff and complete private file report
            cannot be produced.
    """
    record = proof.record
    configuration_unchanged = fingerprint(g, p) == record.fingerprint
    if configuration_unchanged:
        configuration = 'unchanged since prepare'
    elif record.state == STATE_UNKNOWN_OUTCOME:
        configuration = (
            'changed since prepare; checks have not been rerun with these settings; '
            'resolve the publication outcome first'
        )
    else:
        configuration = (
            'changed since prepare; checks have not been rerun with these settings; '
            'snapshot cannot publish'
        )
    configuration_kind = 'success' if configuration_unchanged else 'warning'
    checks = 'no sensitive terms found when prepared; snapshot verified; '
    checks += style('GitHub not rechecked', 'warning')
    rows = [('Checks', checks)]
    if verbose_enabled() or not configuration_unchanged:
        rows.append(('Publication configuration', style(configuration, configuration_kind)))
    details = _candidate_report_details(paths, proof, p, extra_rows=rows)
    return details + _rewritten_change_report(paths, p.name, record)


def publication_report(
    paths: Paths,
    proof: _verification.ObjectProof,
    p: ProjectConfig,
) -> str:
    """Render retained candidate facts for a publication preview.

    Use verified object evidence to report retained metadata, file changes, and
    credential exceptions. The caller supplies current gate and live-host
    verification facts; this report does not perform or claim those checks.
    The returned terminal report bounds its displayed file list and writes a
    separate complete private file report. The caller must hold the project
    lock for the complete operation.

    Args:
        paths: Derived pubanon paths.
        proof: Object evidence obtained for this review step.
        p: Active project configuration.

    Returns:
        The shared factual report without inspection-only instructions.

    Raises:
        PubanonError: The candidate diff or complete file report cannot be
            produced.
    """
    return _candidate_report_details(paths, proof, p)


def reprepare_advice(record: CandidateRecord, *, project: str | None = None) -> str:
    """Describe how to rebuild the selected source revision and release tag."""
    selection = [f'--project={project}'] if project is not None else []
    prepare = ['pubanon', 'prepare', *selection]
    if record.replaces is not None:
        selector = 'all' if record.replace_scope == 'all' else record.replaces
        prepare.extend(('--replace', selector))
    prepare.extend(('--source', record.pinned.commit))
    if record.tag_intent is not None:
        prepare.extend(('--version', record.tag_intent[1:]))
    discard_command = shell_command('pubanon', 'discard', *selection)
    prepare_command = shell_command(*prepare)
    if discard_command is None or prepare_command is None:
        return (
            f'Select project {escape(project or "")}, then rerun discard and prepare '
            'for this snapshot.'
        )
    return f'{discard_command} && {prepare_command}'


def inspect_next_action(record: CandidateRecord, g: GlobalConfig, p: ProjectConfig) -> str:
    """Return the next inspection action, prioritizing an unknown publish outcome."""
    if record.state == STATE_UNKNOWN_OUTCOME:
        return 'pubanon publish --resolve'
    if fingerprint(g, p) == record.fingerprint:
        return 'pubanon publish'
    return reprepare_advice(record, project=p.name)
