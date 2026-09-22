"""Verify and execute publication transactions."""

# pyright: reportPrivateUsage=false

import os
import re
from contextlib import suppress
from dataclasses import dataclass, replace
from pathlib import Path
from typing import NamedTuple

from pubanon._config import (
    GlobalConfig,
    Paths,
    ProjectConfig,
    assemble_rules,
    derived_private_terms,
    save_project,
)
from pubanon._errors import OperationCancelled, PubanonError, PublicationCancelled
from pubanon._remote import _host, _stage
from pubanon._remote._host import (
    Destination,
    RemoteRefs,
    fetch,
    ls_remote,
    push,
    remote_refs_text,
    verify_destination,
    verify_recorded_destination,
)
from pubanon._run import OID_HEX, git_failure_fact, run_git
from pubanon._snapshots import _audit, _candidate, _history, _records, _source
from pubanon._snapshots._candidate import (
    CANDIDATE_REF,
    STATE_RETAINED,
    STATE_UNKNOWN_OUTCOME,
    CandidateRecord,
    load_candidate,
    published_refs,
    save_candidate,
    store_path,
    verify_retained_candidate,
)
from pubanon._snapshots._source import check_source_safety
from pubanon._terminal import (
    confirm,
    escape,
    field,
    heading,
    next_step,
    output,
    shell_command,
    style,
)


@dataclass(frozen=True)
class PublishContext:
    """Bundle the facts and interaction mode for one publication.

    Positional constructor inputs provide the derived paths, active global
    configuration, selected project, retained candidate, and interaction mode.
    """

    _paths: Paths
    _global_config: GlobalConfig
    _project: ProjectConfig
    _record: CandidateRecord

    _yes: bool
    _unattended: bool
    _verbose: bool = False


class RepairResult(NamedTuple):
    """Record restored local refs and their current-policy history audit.

    Attributes:
        refs: Actual installed main and exact tag set, freshly matched to the
            complete host observation before return. No extra branches exist.
        audit: Current content findings over all restored history. Structural
            checks passed; findings do not imply exposure acknowledgment or
            prove historical provenance.
    """

    refs: RemoteRefs
    audit: _audit.HistoryAudit


@dataclass(frozen=True)
class SyncContext:
    """Bundle the facts and interaction mode for one host catch-up.

    Positional constructor inputs provide the derived paths, active global
    configuration, selected project, and interaction mode.
    """

    _paths: Paths
    _global_config: GlobalConfig
    _project: ProjectConfig

    _yes: bool
    _unattended: bool


@dataclass(frozen=True)
class _Verified:
    """Bind a verified destination to its fresh remote references."""

    _destination: Destination
    _remote: RemoteRefs


def _acknowledge_exposure(
    paths: Paths,
    configs: tuple[GlobalConfig, ProjectConfig],
    audit: _audit.HistoryAudit,
    *,
    unattended: bool,
) -> ProjectConfig:
    """Record an informed decision for current historical exposures."""
    g, p = configs
    digest = _audit.exposure_digest(audit.findings)
    if not digest or digest == p.acknowledged_exposure:
        return p
    if unattended:
        raise PubanonError(
            '--yes cannot acknowledge historical exposure',
            recovery=(
                'run pubanon status --audit to review the findings, then rerun without --yes'
            ),
        )

    details = _audit.history_details(audit, g, p, derived_private_terms(paths, p, os.environ))
    heading('Historical exposure review', separate=True)
    for label, value in details.items():
        output(f'{style(escape(label) + ":")} {escape(value)}')
    output('Locations show the first match for each finding; more matches may exist.')
    output(
        'Acknowledgment accepts these existing identity-critical historical findings. '
        'It does not remove published content or permit these terms in new snapshot content.',
        kind='warning',
    )
    if not confirm('Continue despite this earlier exposure?'):
        raise PubanonError('historical exposure was not acknowledged')
    updated = replace(p, acknowledged_exposure=digest)
    save_project(paths, updated)
    return updated


def _audit_sync(
    ctx: SyncContext,
    local_main: str,
    local_tags: dict[str, str],
    remote: RemoteRefs,
) -> None:
    """Audit the sent range and require historical exposure acknowledgment."""
    full_commits = _audit.published_commits(ctx._paths, ctx._project.name, new=local_main)
    focused_commits = frozenset(
        _audit.published_commits(ctx._paths, ctx._project.name, new=local_main, old=remote.main)
    )
    missing_tags = {tag: local_tags[tag] for tag in local_tags if tag not in remote.tags}
    rules = assemble_rules(
        ctx._global_config,
        ctx._project,
        derived_private_terms(ctx._paths, ctx._project, os.environ),
    )
    full_audit, range_audit = _audit.audit_history_pair(
        ctx._paths,
        ctx._project.name,
        full_commits,
        local_tags,
        focused_commits,
        missing_tags,
        rules,
        ctx._global_config,
        tip=local_main,
    )

    if full_audit.identity_problems:
        raise PubanonError(
            'published history has identity problems',
            fact=escape(', '.join(full_audit.identity_problems)),
            recovery='pubanon status --audit',
        )
    range_details = _audit.history_details(
        range_audit,
        ctx._global_config,
        ctx._project,
        derived_private_terms(ctx._paths, ctx._project, os.environ),
    )
    for label, detail in range_details.items():
        if label.startswith('policy mismatch'):
            output(f'policy mismatch: {escape(detail)}', kind='warning')
    _acknowledge_exposure(
        ctx._paths, (ctx._global_config, ctx._project), full_audit, unattended=ctx._unattended
    )


def _classify_ref_drift(
    ctx: PublishContext,
    selector_name: str,
    selector_oid: str,
) -> tuple[str, str | None] | None:
    """Classify movement of one retained source ref."""
    current = run_git(
        ['rev-parse', '--verify', selector_name], cwd=ctx._project.source_path, check=False
    )
    if current.returncode != 0:
        return 'deleted', None
    if current.stdout == f'{selector_oid}\n'.encode():
        return None
    resolved = run_git(
        ['rev-parse', '--verify', f'{selector_name}^{{commit}}'],
        cwd=ctx._project.source_path,
        check=False,
    )
    if (
        resolved.returncode != 0
        or re.fullmatch(rb'(?:' + OID_HEX.encode() + rb')\n', resolved.stdout) is None
    ):
        raise PubanonError('source ref no longer resolves to a commit')
    current_commit = resolved.stdout[:-1].decode('ascii')
    if current_commit == ctx._record.pinned.commit:
        return 'changed ref object with unchanged commit', current_commit
    advanced = run_git(
        [
            'merge-base',
            '--is-ancestor',
            ctx._record.pinned.commit,
            current_commit,
        ],
        cwd=ctx._project.source_path,
        check=False,
    )
    if advanced.returncode == 0:
        return 'advanced', current_commit
    if advanced.returncode != 1:
        raise PubanonError('cannot classify source ref drift')
    rewound = run_git(
        [
            'merge-base',
            '--is-ancestor',
            current_commit,
            ctx._record.pinned.commit,
        ],
        cwd=ctx._project.source_path,
        check=False,
    )
    if rewound.returncode == 0:
        return 'rewound', current_commit
    if rewound.returncode == 1:
        return 'diverged', current_commit
    raise PubanonError('cannot classify source ref drift')


def _clear_repair_refs(store: Path) -> None:
    """Delete only refs in the private repair namespace."""
    output = run_git(
        [
            '--git-dir',
            str(store.absolute()),
            'for-each-ref',
            '--format=%(refname)',
            'refs/pubanon/repair/',
        ],
        cwd=store,
    ).stdout
    if output:
        commands = b''.join(b'delete ' + ref + b'\n' for ref in output.splitlines())
        run_git(
            ['--git-dir', str(store.absolute()), 'update-ref', '--no-deref', '--stdin'],
            cwd=store,
            input_bytes=commands,
        )


def _confirm_repair(before: RemoteRefs, fetched: RemoteRefs, *, yes: bool) -> None:
    """Review every changed local ref before authorizing host-directed replacement."""
    old_refs = {'refs/heads/main': before.main} | {
        f'refs/tags/{name}': oid for name, oid in before.tags.items()
    }
    new_refs = {'refs/heads/main': fetched.main} | {
        f'refs/tags/{name}': oid for name, oid in fetched.tags.items()
    }
    changes = {
        ref: f'{old_refs.get(ref) or "absent"} -> {new_refs.get(ref) or "absent"}'
        for ref in sorted(old_refs.keys() | new_refs.keys())
        if old_refs.get(ref) != new_refs.get(ref)
    }
    heading('Replace local publication history with verified GitHub history:')
    for label, value in changes.items():
        output(f'{style(escape(label) + ":")} {escape(value)}')
    output('The host repository and source repository will not change.')
    if not yes and not confirm('Replace local publication refs with verified GitHub refs?'):
        raise PubanonError('repair declined; local refs were not replaced')


def _destination_from_record(record: CandidateRecord) -> Destination:
    """Rebuild the recorded destination through the host URL seam."""
    destination = record.destination
    return Destination(
        adapter=destination.adapter,
        owner=destination.owner,
        repo=destination.repo,
        url=_host.URL_TEMPLATE.format(owner=destination.owner, repo=destination.repo),
        account=destination.account,
        visibility=destination.visibility,
    )


def _expected_pushed(record: CandidateRecord) -> RemoteRefs:
    """Return the exact main and tag state one push should create."""
    return RemoteRefs(*_records.candidate_poststate(record))


def _changed_tags(record: CandidateRecord) -> tuple[str, ...]:
    """Identify only tags whose exact values change in this transaction."""
    after = _expected_pushed(record).tags
    return tuple(
        tag
        for tag in sorted(record.prestate_tags.keys() | after.keys())
        if record.prestate_tags.get(tag) != after.get(tag)
    )


def _install_repair_refs(
    store: Path,
    before: RemoteRefs,
    fetched: RemoteRefs,
) -> None:
    """Replace managed refs atomically only if their original values remain."""
    old = {f'refs/tags/{name}': oid for name, oid in before.tags.items()}
    if before.main is not None:
        old['refs/heads/main'] = before.main
    new = {f'refs/tags/{name}': oid for name, oid in fetched.tags.items()}
    if fetched.main is None:
        raise PubanonError('fetched repair main is absent')
    new['refs/heads/main'] = fetched.main
    commands = ['start']
    for ref, oid in new.items():
        if ref in old:
            commands.append(f'update {ref} {oid} {old[ref]}')
        else:
            commands.append(f'create {ref} {oid}')
    commands.extend(f'delete {ref} {oid}' for ref, oid in old.items() if ref not in new)
    commands.extend(('prepare', 'commit', ''))
    run_git(
        ['--git-dir', str(store.absolute()), 'update-ref', '--no-deref', '--stdin'],
        cwd=store,
        input_bytes='\n'.join(commands).encode('utf-8', 'surrogateescape'),
    )


def _local_ref_command(ref: str, old: str | None, new: str | None, zero: str) -> str:
    """Encode a guarded create, update, deletion, or unchanged-value check."""
    if old == new:
        return f'verify {ref} {old or zero}'
    if old is None:
        return f'create {ref} {new}'
    if new is None:
        return f'delete {ref} {old}'
    return f'update {ref} {new} {old}'


def _finalize(ctx: PublishContext, remote: RemoteRefs) -> None:
    """Apply a guarded local transaction and remove candidate metadata last."""
    del remote
    before = RemoteRefs(ctx._record.prestate_main, ctx._record.prestate_tags)
    after = _expected_pushed(ctx._record)
    actual = RemoteRefs(*published_refs(ctx._paths, ctx._project.name))
    if actual not in (before, after):
        raise PubanonError('local published refs conflict with publication prestate and poststate')
    store = store_path(ctx._paths, ctx._project.name)
    commands = ['start']
    refs = [('refs/heads/main', actual.main, after.main)]
    refs.extend(
        (f'refs/tags/{tag}', actual.tags.get(tag), after.tags.get(tag))
        for tag in _changed_tags(ctx._record)
    )
    commands.extend(
        _local_ref_command(ref, old, new, '0' * len(ctx._record.commit)) for ref, old, new in refs
    )
    anchor = run_git(
        ['--git-dir', str(store.absolute()), 'show-ref', '--verify', '--hash', CANDIDATE_REF],
        cwd=store,
        check=False,
    )
    if anchor.returncode == 0:
        if anchor.stdout != f'{after.main}\n'.encode():
            raise PubanonError('local snapshot ref conflicts with published snapshot tip')
        commands.append(f'delete {CANDIDATE_REF} {after.main}')
    elif actual != after:
        raise PubanonError('local snapshot ref is missing before finalization')
    else:
        commands.append(f'verify {CANDIDATE_REF} {"0" * len(ctx._record.commit)}')
    commands.extend(('prepare', 'commit', ''))
    result = run_git(
        ['--git-dir', str(store.absolute()), 'update-ref', '--no-deref', '--stdin'],
        cwd=store,
        input_bytes='\n'.join(commands).encode(),
        check=False,
    )
    if result.returncode != 0:
        raise PubanonError('cannot record published refs locally', fact=git_failure_fact(result))
    try:
        _candidate.record_path(ctx._paths, ctx._project.name).unlink(missing_ok=True)
    except (OSError, RuntimeError) as exc:
        raise PubanonError('cannot remove published snapshot record') from exc


def _managed_outcome(record: CandidateRecord, remote: RemoteRefs) -> str:
    """Classify the managed projection of one authoritative remote read."""
    after = _expected_pushed(record)
    changed = _changed_tags(record)
    if remote.main == after.main and all(
        remote.tags.get(tag) == after.tags.get(tag) for tag in changed
    ):
        return 'pushed'
    if remote.main == record.prestate_main and all(
        remote.tags.get(tag) == record.prestate_tags.get(tag) for tag in changed
    ):
        return 'not-pushed'
    return 'indeterminate'


def _preserve_replayed_sources(ctx: PublishContext) -> PublishContext:
    """Persist each derived publication's independent frozen locator before creation."""
    for index, entry in enumerate(ctx._record.replayed):
        if entry.original_source is None or entry.original_bookmark is None:
            continue
        bookmark = entry.source_bookmark
        if bookmark is None:
            bookmark = replace(
                entry.original_bookmark,
                refname=_source.bookmark_ref(ctx._project.name, entry.new),
            )
            replayed = list(ctx._record.replayed)
            replayed[index] = replace(entry, source_bookmark=bookmark)
            ctx = replace(ctx, _record=replace(ctx._record, replayed=tuple(replayed)))
            save_candidate(ctx._paths, ctx._project.name, ctx._record)
        _source.ensure_bookmark(bookmark, entry.original_source)
    return ctx


def _preserve_source(ctx: PublishContext) -> PublishContext:
    """Persist bookmark intent before creating the source reference."""
    try:
        bookmark = ctx._record.source_bookmark
        if bookmark is None:
            bookmark = _source.describe_bookmark(
                ctx._project.source_path, ctx._project.name, ctx._record.commit
            )
            ctx = replace(ctx, _record=replace(ctx._record, source_bookmark=bookmark))
            save_candidate(ctx._paths, ctx._project.name, ctx._record)
        _source.ensure_bookmark(bookmark, ctx._record.pinned.commit)
    except (OSError, PubanonError) as exc:
        if isinstance(exc, OperationCancelled):
            raise
        raise PubanonError(
            'source preservation is incomplete',
            fact=str(exc),
            recovery='Restore source access or correct its registered location, then retry '
            'pubanon publish. Snapshot evidence and any provisional bookmark are retained.',
        ) from exc
    return ctx


def _publication_cancelled(
    error: KeyboardInterrupt | OperationCancelled,
    *,
    fact: str,
    recovery: str,
) -> OperationCancelled:
    """Preserve cancellation details while adding publication lifecycle facts."""
    cancelled = (
        error
        if isinstance(error, OperationCancelled)
        else OperationCancelled('operation cancelled')
    )
    cancelled.fact = f'{fact}\n{cancelled.fact}' if cancelled.fact else fact
    cancelled.recovery = recovery
    return cancelled


def _refspecs(record: CandidateRecord) -> list[str]:
    """Build exact-OID publication refspecs."""
    after = _expected_pushed(record)
    refspecs = [f'{after.main}:refs/heads/main']
    refspecs.extend(f'{after.tags.get(tag, "")}:refs/tags/{tag}' for tag in _changed_tags(record))
    return refspecs


def _push_args(record: CandidateRecord) -> list[str]:
    """Build the closed atomic push argument list."""
    expected_main = record.prestate_main or ''
    args = [
        '--atomic',
        f'--force-with-lease=refs/heads/main:{expected_main}',
    ]
    args.extend(
        f'--force-with-lease=refs/tags/{tag}:{record.prestate_tags.get(tag, "")}'
        for tag in _changed_tags(record)
    )
    args.append('--no-follow-tags')
    args.extend(_refspecs(record))
    return args


def _refuse_sync_branches(remote: RemoteRefs) -> None:
    """Refuse destination branches outside the publication universe."""
    if remote.branches:
        raise PubanonError(
            'destination contains an unexpected branch',
            expected=escape(remote_refs_text(RemoteRefs(remote.main, remote.tags, ()))),
            actual=escape(remote_refs_text(remote)),
            recovery='remove every destination branch except main, then rerun',
        )


def _repair_refs(store: Path) -> RemoteRefs:
    """Read and validate fetched main and direct tags in the repair namespace."""
    output = run_git(
        [
            '--git-dir',
            str(store.absolute()),
            'for-each-ref',
            '--format=%(objectname) %(refname)',
            'refs/pubanon/repair/',
        ],
        cwd=store,
    ).stdout
    main = None
    tags: dict[str, str] = {}
    for line in output.splitlines():
        match = re.fullmatch(
            rb'(' + OID_HEX.encode() + rb') refs/pubanon/repair/(main|tags/(.+))', line
        )
        if match is None:
            raise PubanonError('Git returned malformed repair refs')
        oid = match[1].decode('ascii')
        if match[2] == b'main':
            if main is not None:
                raise PubanonError('Git returned duplicate repair main')
            main = oid
        else:
            try:
                name = match[3].decode('utf-8')
            except UnicodeDecodeError as exc:
                raise PubanonError('Git returned malformed repair tag text') from exc
            if name in tags:
                raise PubanonError('Git returned duplicate repair tag')
            tags[name] = oid
    if main is None:
        raise PubanonError('fetched repair main is absent')
    return RemoteRefs(main, tags)


def _replacement_review(ctx: PublishContext, verified: _Verified) -> str:
    """Summarize the replacement scope before its single approval question."""
    record = ctx._record
    if record.replaces is None or record.prestate_main is None:
        raise PubanonError('replacement snapshot is incomplete')
    _, after_tags = _records.candidate_poststate(record)
    output('Replace published snapshot', kind='heading')
    output(
        field(
            'Repository',
            f'{escape(verified._destination.owner)}/{escape(verified._destination.repo)}',
        )
    )
    output(field('Visibility', verified._destination.visibility.upper(), kind='warning'))
    tags = sorted(tag for tag, oid in record.prestate_tags.items() if oid == record.replaces)
    output(
        field(
            'Version',
            escape(
                ', '.join(tags)
                if record.replace_scope != 'all' and tags
                else record.tag_intent or 'untagged'
            ),
        )
    )
    if record.replace_scope == 'all':
        count = len(
            _audit.published_commits(ctx._paths, ctx._project.name, new=record.prestate_main)
        )
        output(
            field(
                'Scope',
                f'all published history ({count} snapshots) -> one snapshot',
                kind='warning',
            )
        )
    width = 40 if ctx._verbose else 12
    output(field('Commit', f'{record.replaces[:width]} -> {record.commit[:width]}'))
    updated = sorted(
        tag for tag, oid in after_tags.items() if record.prestate_tags.get(tag) != oid
    )
    output(field('Updates', escape(', '.join(['main', *updated]))))
    removed = sorted(record.prestate_tags.keys() - after_tags.keys())
    if removed:
        output(field('Removed tags', escape(', '.join(removed)), kind='warning'))
    if record.replayed:
        output(field('Later snapshots', f'{len(record.replayed)} replayed'))
    if ctx._verbose:
        for entry in record.replayed:
            output(field('Replayed commit', f'{entry.old} -> {entry.new}'))
    output()
    output(
        'Existing clones, GitHub Releases, and uploaded packages are unchanged.', kind='warning'
    )
    if verified._destination.visibility == 'public':
        output('Public forks and caches may also retain the old commits.', kind='warning')
    return 'Publish this replacement?'


def _publication_review_question(ctx: PublishContext, verified: _Verified) -> str:
    """Present the selected publication operation and return its confirmation."""
    if ctx._record.replaces is None or ctx._verbose:
        output('Publish prepared snapshot', kind='heading')
        output(
            _candidate.publication_report(
                ctx._paths, ctx._record, ctx._global_config, ctx._project
            )
        )
        output(
            '  '
            + field(
                'Verified destination',
                f'{escape(verified._destination.owner)}/{escape(verified._destination.repo)}',
            )
        )
        output('  ' + field('Verified visibility', escape(verified._destination.visibility)))
        if ctx._record.source_bookmark is not None:
            output('  A private source bookmark protects the original source revision.')
            output('  Source branches, tags, HEAD, index, and files are unchanged.')
        output('  Content, identity, and destination rechecked.', kind='success')
    if ctx._record.replaces is None:
        if verified._destination.visibility == 'public':
            output('This publishes the prepared snapshot to a public repository.', kind='warning')
        else:
            output('This publishes the prepared snapshot to a private repository.')
        target = f'{escape(verified._destination.owner)}/{escape(verified._destination.repo)}'
        what = f'main at {ctx._record.commit[:7]}'
        if ctx._record.tag_intent is not None:
            what += f' and tag {escape(ctx._record.tag_intent)}'
        question = f'Publish {what} to {target} ({verified._destination.visibility})?'
    else:
        question = _replacement_review(ctx, verified)
    return question


def _review_publication(ctx: PublishContext, verified: _Verified) -> None:
    """Verify and review one candidate before attempting a push."""
    main, tags = published_refs(ctx._paths, ctx._project.name)
    if main is not None:
        commits = _audit.published_commits(ctx._paths, ctx._project.name, new=main)
        rules = assemble_rules(
            ctx._global_config,
            ctx._project,
            derived_private_terms(ctx._paths, ctx._project, os.environ),
        )
        audit = _audit.audit_history(
            ctx._paths, ctx._project.name, commits, tags, rules, ctx._global_config, tip=main
        )

        # Replacement identity is proved over its complete poststate before host work.
        if audit.identity_problems and ctx._record.replaces is None:
            raise PubanonError(
                'published history has identity problems',
                fact=escape(', '.join(audit.identity_problems)),
                recovery='pubanon status --audit',
            )
        _acknowledge_exposure(
            ctx._paths, (ctx._global_config, ctx._project), audit, unattended=ctx._unattended
        )
    stage = _stage.load_stage(ctx._paths, ctx._project.name)
    if stage is not None:
        tip = _records.candidate_tip(ctx._record)
        if stage.commit != tip:
            raise PubanonError(
                'staging evidence does not cover the retained snapshot',
                expected=escape(tip),
                actual=escape(stage.commit),
                recovery='pubanon stage push, or pubanon stage clean',
            )
        output('The staged snapshot matches the prepared snapshot.', kind='success')
    question = _publication_review_question(ctx, verified)
    inspection = _candidate.materialize_inspect(ctx._paths, ctx._project.name, ctx._record)
    if ctx._verbose:
        output(field('Snapshot directory', escape(str(inspection))))
    if not ctx._yes and not confirm(question):
        raise PubanonError(
            'publication declined; prepared snapshot, its snapshot directory, and provisional '
            'source bookmark retained',
            fact=f'Inspection copy: {escape(str(inspection))}',
            recovery='pubanon inspect, then pubanon publish when ready',
        )


def _save_publication_state(
    ctx: PublishContext, record: CandidateRecord, *, cancellation_fact: str
) -> None:
    """Preserve known host facts if interrupted around an atomic record write."""
    try:
        save_candidate(ctx._paths, ctx._project.name, record)
    except (OperationCancelled, KeyboardInterrupt) as error:
        current = load_candidate(ctx._paths, ctx._project.name)
        recovery = (
            'pubanon publish --resolve'
            if current is not None and current.state == STATE_UNKNOWN_OUTCOME
            else 'pubanon publish'
        )
        raise _publication_cancelled(error, fact=cancellation_fact, recovery=recovery) from None


def _sync_args(
    local_main: str,
    local_tags: dict[str, str],
    remote: RemoteRefs,
) -> list[str]:
    """Build exact synchronization leases and refspecs."""
    missing_tags = {tag: local_tags[tag] for tag in local_tags if tag not in remote.tags}
    args = ['--atomic', f'--force-with-lease=refs/heads/main:{remote.main or ""}']
    args.extend(f'--force-with-lease=refs/tags/{tag}:' for tag in sorted(missing_tags))
    args.append('--no-follow-tags')
    args.append(f'{local_main}:refs/heads/main')
    args.extend(f'{missing_tags[tag]}:refs/tags/{tag}' for tag in sorted(missing_tags))
    return args


def _unrelated_drift(record: CandidateRecord, remote: RemoteRefs) -> str | None:
    """Render remote facts outside the managed projection."""
    unexpected_tags = {
        tag: oid
        for tag, oid in remote.tags.items()
        if tag not in _changed_tags(record) and record.prestate_tags.get(tag) != oid
    }
    missing_tags = {
        tag: oid
        for tag, oid in record.prestate_tags.items()
        if tag not in _changed_tags(record) and tag not in remote.tags
    }
    parts: list[str] = []
    if remote.branches:
        parts.append('branches: ' + ', '.join(f'{name}={oid}' for name, oid in remote.branches))
    if unexpected_tags:
        parts.append(
            'unexpected tags: '
            + ', '.join(f'{tag}={oid}' for tag, oid in sorted(unexpected_tags.items()))
        )
    if missing_tags:
        parts.append('missing tags: ' + ', '.join(sorted(missing_tags)))
    return '; '.join(parts) or None


def _settle(
    ctx: PublishContext,
    remote: RemoteRefs,
    push_error: PubanonError | None,
) -> str:
    """Apply one classified managed outcome to local candidate state."""
    outcome = _managed_outcome(ctx._record, remote)
    drift = _unrelated_drift(ctx._record, remote)
    if outcome != 'pushed' and _candidate.confirmed_publication(
        ctx._paths, ctx._project.name, ctx._record
    ):
        outcome = 'pushed'
        drift = (
            'current host refs differ from the confirmed published snapshot: '
            + remote_refs_text(remote)
        )
    escaped_drift = None if drift is None else escape(drift)
    if outcome == 'pushed':
        if ctx._record.source_bookmark is None:
            raise PubanonError('confirmed snapshot source bookmark is missing')
        try:
            _history.record_publication(ctx._paths, ctx._project, ctx._record)
            _finalize(ctx, remote)
        except (OSError, PubanonError, KeyboardInterrupt) as error:
            failure = (
                error
                if isinstance(error, PubanonError)
                else OperationCancelled('operation cancelled')
                if isinstance(error, KeyboardInterrupt)
                else PubanonError(str(error))
            )
            landed = (
                f'Published and verified commit {escape(ctx._record.commit)} '
                'on the host repository.'
            )
            failure.fact = f'{landed}\n{failure.fact}' if failure.fact else landed
            if (
                isinstance(failure, OperationCancelled)
                and not _candidate.record_path(ctx._paths, ctx._project.name).exists()
            ):
                failure.args = (
                    'Publication and local finalization completed before cancellation.',
                )
                failure.recovery = 'pubanon status --remote; do not publish a new snapshot'
                raise failure from None
            failure.args = (f'Local finalization is incomplete: {failure}',)
            failure.recovery = (
                'Do not publish a new snapshot to resolve this error. '
                'Run pubanon publish --resolve to reconcile local state.'
            )
            raise failure from None
        if escaped_drift is not None:
            raise PubanonError(
                'snapshot landed and must not be retried; '
                'unrelated remote drift blocks later publication until resolved',
                fact=escaped_drift,
                recovery='pubanon repo verify',
            )
        return outcome
    if outcome == 'not-pushed':
        _save_publication_state(
            ctx,
            replace(ctx._record, state=STATE_RETAINED),
            cancellation_fact=(
                'The repository still points to the previous published snapshot. '
                'The prepared snapshot is available to publish.'
            ),
        )
        if escaped_drift is not None:
            output(f'unrelated remote drift: {escaped_drift}', kind='warning')
        return outcome
    managed_tags = _changed_tags(ctx._record)
    pushed = _expected_pushed(ctx._record)
    pushed_tags = {tag: pushed.tags[tag] for tag in managed_tags if tag in pushed.tags}
    actual_tags = {tag: remote.tags[tag] for tag in managed_tags if tag in remote.tags}
    raise PubanonError(
        'publication outcome is indeterminate',
        expected=(
            'prestate: '
            + escape(
                remote_refs_text(
                    RemoteRefs(
                        ctx._record.prestate_main,
                        {
                            tag: ctx._record.prestate_tags[tag]
                            for tag in managed_tags
                            if tag in ctx._record.prestate_tags
                        },
                    )
                )
            )
            + '; pushed: '
            + escape(remote_refs_text(RemoteRefs(pushed.main, pushed_tags)))
        ),
        actual=escape(remote_refs_text(RemoteRefs(remote.main, actual_tags))),
        fact=(None if escaped_drift is None else f'unrelated remote drift: {escaped_drift}'),
        recovery=(
            'pubanon publish --resolve once the remote settles, or pubanon discard '
            '--force to abandon the evidence; force discard abandons the ability to '
            'determine whether the push landed'
        ),
    ) from push_error


def _push_candidate(ctx: PublishContext, verified: _Verified) -> None:
    """Push one verified candidate with unknown-outcome protection."""
    unknown = replace(ctx._record, state=STATE_UNKNOWN_OUTCOME)
    try:
        _save_publication_state(
            ctx, unknown, cancellation_fact='No push was attempted; prepared snapshot is retained.'
        )
    except (OSError, PubanonError) as error:
        if isinstance(error, OperationCancelled):
            raise
        current = load_candidate(ctx._paths, ctx._project.name)
        retry = (
            'pubanon publish --resolve'
            if current is not None and current.state == STATE_UNKNOWN_OUTCOME
            else 'pubanon publish'
        )
        raise PubanonError(
            'Nothing was published; snapshot state could not be saved',
            fact=f'Source repository: {escape(str(ctx._project.source_path))}\n{error}',
            recovery=f'Restore private state write access, then run {retry}. '
            'The prepared snapshot and provisional source bookmark are retained.',
        ) from error
    ctx = replace(ctx, _record=unknown)
    push_error: PubanonError | None = None
    try:
        push(
            verified._destination,
            store_path(ctx._paths, ctx._project.name),
            _push_args(ctx._record),
        )
    except (OperationCancelled, KeyboardInterrupt) as error:
        raise _publication_cancelled(
            error,
            fact='publication outcome is unknown; recovery evidence is retained',
            recovery='pubanon publish --resolve',
        ) from None
    except PubanonError as error:
        push_error = error
    try:
        remote = ls_remote(verified._destination, ctx._paths, ctx._project.name)
    except (OperationCancelled, KeyboardInterrupt) as error:
        raise _publication_cancelled(
            error,
            fact='publication outcome is unknown; recovery evidence is retained',
            recovery='pubanon publish --resolve',
        ) from None
    except PubanonError as read_error:
        facts = [read_error.render()]
        if push_error is not None:
            facts.insert(0, push_error.render())
        raise PubanonError(
            'publication outcome is unknown',
            fact='; '.join(facts),
            recovery='pubanon publish --resolve',
        ) from read_error
    if _settle(ctx, remote, push_error) == 'not-pushed':
        if push_error is not None:
            raise push_error
        raise PubanonError(
            'The repository still points to the previous published snapshot; '
            'the prepared snapshot is available',
            recovery='rerun pubanon publish',
        )


def _verify_remote(record: CandidateRecord, remote: RemoteRefs) -> None:
    """Require the closed remote universe to equal recorded prestate."""
    if (
        record.replaces is None
        and record.tag_intent is not None
        and record.tag_intent in remote.tags
    ):
        raise PubanonError('intended tag already exists on the destination')
    expected = RemoteRefs(record.prestate_main, record.prestate_tags)
    if remote != expected:
        raise PubanonError(
            'remote prestate does not match the snapshot record',
            expected=escape(remote_refs_text(expected)),
            actual=escape(remote_refs_text(remote)),
            recovery='pubanon status --remote, then see "Accepting an outside fix" in the '
            'operator guide',
        )


def _verify_source(ctx: PublishContext) -> None:
    """Re-prove source safety, pinned existence, and selector intent."""
    check_source_safety(ctx._project.source_path)
    result = run_git(
        ['rev-parse', '--verify', f'{ctx._record.pinned.commit}^{{commit}}'],
        cwd=ctx._project.source_path,
        check=False,
    )
    if result.returncode != 0 or result.stdout != f'{ctx._record.pinned.commit}\n'.encode():
        raise PubanonError('pinned source commit no longer exists')
    if ctx._record.pinned.selector_kind == 'explicit':
        return
    selector_name = ctx._record.pinned.selector_name
    selector_oid = ctx._record.pinned.selector_oid
    if selector_name is None or selector_oid is None:
        raise PubanonError('snapshot selector intent is invalid')
    drift = _classify_ref_drift(ctx, selector_name, selector_oid)
    if drift is None:
        return
    if ctx._unattended:
        raise PubanonError(
            '--yes cannot approve publishing after the selected source changed',
            recovery='rerun pubanon publish in an interactive terminal without --yes',
        )
    message = (
        'Warning: source tag has changed; it still selects the prepared source commit.'
        if drift[0] == 'changed ref object with unchanged commit'
        else 'Warning: source has changed since snapshot was prepared.'
    )
    output(message, kind='warning')


def _verify_for_publish(ctx: PublishContext) -> _Verified:
    """Re-prove the closed publication verification list in order."""
    if _records.rewrite_path(ctx._paths, ctx._project.name).exists():
        raise PubanonError(
            'rewrite completion is pending',
            recovery=shell_command(
                'pubanon', 'prepare', f'--project={ctx._project.name}', '--continue'
            )
            or (
                f'Choose project {escape(ctx._project.name)} from its registered directory, '
                'then rerun prepare --continue.'
            ),
        )
    verify_retained_candidate(
        ctx._paths, ctx._global_config, ctx._project, ctx._record, env=os.environ
    )
    _verify_source(ctx)
    destination = verify_destination(ctx._paths, ctx._global_config, ctx._project)
    recorded = _destination_from_record(ctx._record)
    if destination != recorded:
        raise PubanonError('verified destination does not match the snapshot record')
    remote = ls_remote(destination, ctx._paths, ctx._project.name)
    _verify_remote(ctx._record, remote)
    return _Verified(destination, remote)


def _verify_sync_prefix(
    ctx: SyncContext,
    local_main: str,
    local_tags: dict[str, str],
    remote: RemoteRefs,
) -> None:
    """Prove the host is empty or a strict prefix of local history."""
    store = store_path(ctx._paths, ctx._project.name)
    if remote.main is not None:
        exists = run_git(
            ['--git-dir', str(store.absolute()), 'cat-file', '-e', f'{remote.main}^{{commit}}'],
            cwd=store,
            check=False,
        )
        if exists.returncode != 0:
            raise PubanonError(
                'host main diverges from local published history',
                expected=escape(local_main),
                actual=escape(remote.main),
                recovery='pubanon repo verify',
            )
        ancestry = run_git(
            [
                '--git-dir',
                str(store.absolute()),
                'merge-base',
                '--is-ancestor',
                remote.main,
                local_main,
            ],
            cwd=store,
            check=False,
        )
        if ancestry.returncode == 1:
            raise PubanonError(
                'host main diverges from local published history',
                expected=escape(local_main),
                actual=escape(remote.main),
                recovery='pubanon repo verify',
            )
        if ancestry.returncode != 0:
            raise PubanonError(
                'cannot classify host main ancestry',
                fact=git_failure_fact(ancestry),
                expected=escape(local_main),
                actual=escape(remote.main),
                recovery='pubanon repo verify',
            )
    for tag, remote_oid in sorted(remote.tags.items()):
        local_oid = local_tags.get(tag)
        if local_oid != remote_oid:
            raise PubanonError(
                'remote tag does not match local published history',
                fact=f'tag: {escape(tag)}',
                expected=escape(local_oid or 'absent'),
                actual=escape(remote_oid),
                recovery='pubanon repo verify',
            )


def check_store_repair_candidate(paths: Paths, project: str) -> None:
    """Refuse store repair while any candidate is retained.

    Args:
        paths: Derived Pubanon paths.
        project: Registered project name.

    Raises:
        PubanonError: A retained candidate requires recovery before repair.
    """
    record = load_candidate(paths, project)
    if record is None:
        return
    if record.state == STATE_UNKNOWN_OUTCOME:
        raise PubanonError(
            'cannot repair the store while a prepared snapshot is retained',
            recovery='pubanon publish --resolve',
        )
    raise PubanonError(
        'cannot repair the store while a prepared snapshot is retained; '
        'a later pubanon prepare rebuilds the discarded snapshot',
        recovery='pubanon discard, then pubanon repo verify --repair-store',
    )


def publication_next_action(project: ProjectConfig, record: CandidateRecord | None = None) -> str:
    """Choose the next configured observation or lifecycle step after publication.

    A replacement record must contain its completed poststate. Return an empty
    string when no configured next step exists.
    """
    if project.required_checks:
        return 'pubanon watch ci'
    version = ''
    if record is not None and record.replaces is not None:
        tip, tags = _records.candidate_poststate(record)
        current = tuple(
            parsed
            for tag, oid in tags.items()
            if oid == tip and (parsed := _candidate.version_from_tag(tag)) is not None
        )
        if current:
            selected = max(current, key=lambda item: tuple(int(part) for part in item.split('.')))
            version = f' --version {selected}'
    if project.package is not None:
        return f'pubanon watch package{version}'
    if project.visibility == 'private':
        return 'pubanon repo reveal'
    return ''


def rebuild_store(
    paths: Paths,
    configs: tuple[GlobalConfig, ProjectConfig],
    dest: Destination,
    *,
    yes: bool,
) -> RepairResult:
    """Validate host history and atomically replace advisory main and tags.

    The caller holds the project lock and verifies the destination. Fetched
    objects are audited in private temporary refs before installation. Content
    findings may be restored locally without acknowledgment; invalid structure
    is refused. Host advancement needs no ancestry relationship with local
    refs.

    Args:
        paths: Derived Pubanon paths.
        configs: Current global configuration and project whose local refs will
            be replaced.
        dest: Destination already verified by the host adapter.

        yes: Preauthorize ordinary replacement confirmation only.

    Returns:
        Actual installed refs matched to a fresh complete host observation,
        together with current-policy findings over their entire history.

    Raises:
        PubanonError: Candidate recovery is required, imported history is
            invalid, refs race, or a read or write fails. Failures after atomic
            installation explicitly state that local refs may have changed;
            no rollback or provenance guarantee is provided.
    """
    g, p = configs
    check_store_repair_candidate(paths, p.name)
    before = RemoteRefs(*published_refs(paths, p.name))
    store = store_path(paths, p.name)
    try:
        _clear_repair_refs(store)
        fetch(
            dest,
            store,
            [
                '+refs/heads/main:refs/pubanon/repair/main',
                '+refs/tags/*:refs/pubanon/repair/tags/*',
            ],
        )
        fetched = _repair_refs(store)
        if fetched.main is None:
            raise PubanonError('fetched repair main is absent')
        rules = assemble_rules(g, p, derived_private_terms(paths, p, os.environ))
        commits = _audit.published_commits(paths, p.name, new=fetched.main)
        audit = _audit.audit_history(
            paths, p.name, commits, fetched.tags, rules, g, tip=fetched.main
        )

        if audit.identity_problems:
            raise PubanonError(
                'host history has invalid publication structure; managed refs were not replaced',
                fact=escape(', '.join(audit.identity_problems)),
            )
        if fetched != ls_remote(dest, paths, p.name):
            raise PubanonError(
                'host refs changed or contain unexpected branches; managed refs were not replaced',
                recovery='pubanon repo verify --repair-store',
            )
        if before != fetched:
            _confirm_repair(before, fetched, yes=yes)
            if fetched != ls_remote(dest, paths, p.name):
                raise PubanonError(
                    'host refs changed during consent; managed refs were not replaced',
                    recovery='pubanon repo verify --repair-store',
                )
            _install_repair_refs(store, before, fetched)
        try:
            actual = RemoteRefs(*published_refs(paths, p.name))
            observed = ls_remote(dest, paths, p.name)
        except PubanonError as exc:
            raise type(exc)(
                'repair could not verify its result; local refs may have changed',
                fact=exc.render(),
                recovery='pubanon repo verify --repair-store',
            ) from exc
        if actual != fetched or actual != observed:
            raise PubanonError(
                'installed refs no longer match verified host refs; local refs may have changed',
                recovery='pubanon repo verify --repair-store',
            )
        return RepairResult(actual, audit)
    finally:
        # Best-effort cleanup must not hide the primary repair outcome.
        with suppress(PubanonError):
            _clear_repair_refs(store)


def report_source_preservation(paths: Paths, project: str, commit: str) -> None:
    """Report recorded source preservation and current local availability.

    `commit` must be a complete publication object ID. Missing provenance or
    unavailable local recovery produces warning output; successful recovery is
    not required for this report.
    """
    record = _records.load_publication(paths, project, commit)
    bookmark = None if record is None else record.source_bookmark
    source = None
    if record is not None:
        source = (
            record.pinned.commit
            if record.pinned is not None
            else record.derivation.original_source
            if record.derivation is not None
            else None
        )
    if record is None or bookmark is None or source is None:
        output(
            'Original source recovery is unavailable; no verified source bookmark was recorded.',
            kind='warning',
        )
    else:
        recovery = _source.source_recovery(bookmark, source)
        if recovery.status != 'protected':
            output(
                f'Source recovery: {recovery.status}; {escape(recovery.reason or "")}',
                kind='warning',
            )
        else:
            output('Pubanon left a private bookmark on this revision in your source repository.')
    output('Source branches, tags, HEAD, index, and files were not changed.')


def _report_publication(ctx: PublishContext) -> None:
    """Describe completed publication and private preservation."""
    heading('Published snapshot')
    output('Publication verified', kind='success')
    recorded = ctx._record.destination
    output('  ' + field('Destination', f'GitHub {escape(recorded.owner)}/{escape(recorded.repo)}'))
    output('  ' + field('Visibility at publication', escape(recorded.visibility.upper())))
    publication = _records.load_publication(ctx._paths, ctx._project.name, ctx._record.commit)
    if publication is not None:
        confirmed = publication.confirmed_at.astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')
        output('  ' + field('Published', confirmed))
    if ctx._record.replaces is None:
        if ctx._record.tag_intent is not None:
            output('  ' + field('Version', escape(ctx._record.tag_intent)))
        output('  ' + field('Snapshot commit', ctx._record.commit))
    else:
        corrected = sorted(
            tag for tag, oid in ctx._record.prestate_tags.items() if oid == ctx._record.replaces
        )
        label = ', '.join(escape(tag) for tag in corrected) or ctx._record.replaces[:12]
        output('  ' + field('Corrected snapshot', f'{label} ({ctx._record.replaces[:12]})'))
        tip = _records.candidate_tip(ctx._record)
        if tip != ctx._record.commit:
            output('  ' + field('Correction commit', ctx._record.commit))
        if ctx._record.replayed:
            output('  ' + field('Replayed snapshots', str(len(ctx._record.replayed))))
        moved = ', '.join(escape(tag) for tag in _changed_tags(ctx._record)) or 'none'
        output('  ' + field('Moved tags', moved))
        output('  ' + field('Snapshot commit', tip))
    report_source_preservation(ctx._paths, ctx._project.name, ctx._record.commit)
    output()
    next_step(publication_next_action(ctx._project, ctx._record))


def publish(ctx: PublishContext) -> None:
    """Verify, confirm, and publish one retained candidate.

    Atomically push the exact candidate main and optional tag refs, then read
    the host to classify the outcome. Confirmed publication updates local
    published refs and removes the candidate record, preserving inspection
    copies.
    An unclassifiable outcome retains evidence for `resolve`.

    The caller must hold the project lock for the complete operation.
    Cancellation before the push retains the prepared candidate and can retain
    provisional source bookmarks; cancellation is not a rollback of all local
    preparation. Cancellation during or after publication can require outcome
    resolution or status inspection.

    Args:
        ctx: Publication facts and selected confirmation mode.

    Raises:
        OperationCancelled: Publication is cancelled with the recovery state
            described above.
        PubanonError: Verification, confirmation, push, or finalization fails.
            Publication may already have landed; use the reported recovery
            action before retrying.
    """
    if ctx._record.state == STATE_UNKNOWN_OUTCOME:
        raise PubanonError(
            'snapshot has an unknown publish outcome',
            recovery='pubanon publish --resolve',
        )
    try:
        verified = _verify_for_publish(ctx)
        ctx = _preserve_source(ctx)
        ctx = _preserve_replayed_sources(ctx)
        _history.preserve_previous_tip(ctx._paths, ctx._project.name, ctx._record)
        _review_publication(ctx, verified)
    except (OperationCancelled, KeyboardInterrupt) as error:
        raise PublicationCancelled(
            'Publication cancelled. Nothing was published.',
            fact='The prepared snapshot is still available.',
            recovery='pubanon publish',
        ) from error
    except (OSError, PubanonError) as error:
        failure = error if isinstance(error, PubanonError) else PubanonError(str(error))
        retained = (
            'Nothing was published. No push was attempted; snapshot evidence and any '
            'provisional source bookmark are retained. '
            f'Source repository: {escape(str(ctx._project.source_path))}'
        )
        failure.fact = f'{retained}\n{failure.fact}' if failure.fact else retained
        if failure.recovery is None:
            failure.recovery = 'Correct the reported problem, then retry pubanon publish.'
        raise failure from None
    _push_candidate(ctx, verified)
    try:
        _report_publication(ctx)
    except (OperationCancelled, KeyboardInterrupt) as error:
        raise _publication_cancelled(
            error,
            fact='Publication and local finalization completed before cancellation.',
            recovery='pubanon status --remote; do not publish a new snapshot',
        ) from None


def resolve(ctx: PublishContext) -> str:
    """Resolve one candidate whose remote push outcome is unknown.

    Read the recorded destination without pushing. Confirmed publication
    finalizes local published refs and removes the candidate record, preserving
    inspection copies. A confirmed unsent outcome returns the candidate to
    retained state.

    The caller must hold the project lock for the complete operation.
    Cancellation attempts no new push but may occur after local finalization;
    follow the reported recovery action before retrying.

    Args:
        ctx: Publication facts loaded from the retained candidate. Recorded
            destination facts govern resolution even if configuration changed.

    Returns:
        `published` after local finalization, or `not-sent` after restoring
        retained state.

    Raises:
        OperationCancelled: Outcome resolution is cancelled; local finalization
            may already be complete.
        PubanonError: The candidate is not unresolved, destination verification
            or observation fails, the outcome is indeterminate, or local
            finalization fails. Confirmed publication with unrelated remote
            drift is finalized before a refusal explains that it must not be
            retried.
    """
    if ctx._record.state != STATE_UNKNOWN_OUTCOME:
        raise PubanonError('snapshot does not have an unknown publish outcome')
    try:
        recorded = _destination_from_record(ctx._record)
        destination = verify_recorded_destination(recorded)
        remote = ls_remote(destination, ctx._paths, ctx._project.name)
    except (OperationCancelled, KeyboardInterrupt) as error:
        raise _publication_cancelled(
            error,
            fact='publication outcome is unknown; recovery evidence is retained',
            recovery='pubanon publish --resolve',
        ) from None
    outcome = _settle(ctx, remote, None)
    return 'published' if outcome == 'pushed' else 'not-sent'


def sync(ctx: SyncContext) -> None:
    """Restore an empty or strictly-behind host from published history.

    Return without pushing when the host already matches. Otherwise audit
    retained history, obtain any required historical-exposure acknowledgment,
    and atomically push missing managed refs. Local published refs and any
    retained candidate are preserved; exposure acknowledgment may update
    project configuration.

    The caller must hold the project lock for the complete operation.

    Args:
        ctx: Local publication facts and selected confirmation mode.

    Raises:
        PubanonError: Candidate recovery is required, history or host
            verification fails, confirmation is refused, or restoration cannot
            be verified. A failed post-push verification may follow a
            successful host update.
    """
    record = load_candidate(ctx._paths, ctx._project.name)
    if record is not None and record.state == STATE_UNKNOWN_OUTCOME:
        raise PubanonError(
            'snapshot has an unknown publish outcome',
            recovery='pubanon publish --resolve',
        )
    local_main, local_tags = published_refs(ctx._paths, ctx._project.name)
    if local_main is None:
        raise PubanonError(
            'local published history has no main branch',
            recovery='pubanon repo verify --repair-store',
        )
    destination = verify_destination(ctx._paths, ctx._global_config, ctx._project)
    remote = ls_remote(destination, ctx._paths, ctx._project.name)
    _refuse_sync_branches(remote)
    expected = RemoteRefs(local_main, local_tags, ())
    if remote == expected:
        output('Host repository already matches the published history.', kind='success')
        output('No source, local, or host state was changed.')
        output()
        return
    _verify_sync_prefix(ctx, local_main, local_tags, remote)

    args = _sync_args(local_main, local_tags, remote)
    _audit_sync(ctx, local_main, local_tags, remote)
    heading('Synchronization preview:')
    commits = _audit.published_commits(
        ctx._paths, ctx._project.name, new=local_main, old=remote.main
    )
    metadata: dict[str, str] = {}
    store = store_path(ctx._paths, ctx._project.name)
    for commit in commits:
        metadata[f'Commit {commit}'] = (
            run_git(
                [
                    '--git-dir',
                    str(store.absolute()),
                    'show',
                    '-s',
                    '--format=Tree: %T%nAuthor: %an <%ae>%nCommitter: %cn <%ce>%nMessage:%n%B',
                    commit,
                ],
                cwd=store,
            )
            .stdout.decode(errors='surrogateescape')
            .rstrip('\n')
        )
    heading('Synchronization facts', separate=True)
    for label, value in {
        'Verified destination': f'{destination.owner}/{destination.repo}',
        'Verified visibility': destination.visibility,
        'Sent range': f'{remote.main or "empty"}..{local_main}',
        'Sent commit count': str(len(commits)),
        **metadata,
    }.items():
        output(f'{style(escape(label) + ":")} {escape(value)}')
    output('  Published history content and identity checks: rechecked now.', kind='success')
    output(
        'This makes the synchronized history publicly accessible.'
        if destination.visibility == 'public'
        else 'This sends the synchronized history to the private host repository.',
        kind='warning' if destination.visibility == 'public' else 'plain',
    )
    for arg in args:
        output(f'  {escape(arg)}')
    if not ctx._yes and not confirm(
        f'Push these references to {escape(destination.owner)}/{escape(destination.repo)}?'
    ):
        raise PubanonError('synchronization declined')
    push(destination, store_path(ctx._paths, ctx._project.name), args)
    readback = ls_remote(destination, ctx._paths, ctx._project.name)
    if readback != expected:
        raise PubanonError(
            'host does not match published history after synchronization',
            expected=escape(remote_refs_text(expected)),
            actual=escape(remote_refs_text(readback)),
            recovery='rerun pubanon publish --sync',
        )
    output('Restored and verified the host repository from local history.', kind='success')
    output('Local published history and any retained snapshot were not changed.')
    output('The source repository was not changed.')
    output()
    heading('Details:')
    for arg in args:
        output(f'  {escape(arg)}')
    output()
