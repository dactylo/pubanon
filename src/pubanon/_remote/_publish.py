"""Verify and execute publication transactions."""

# pyright: reportPrivateUsage=false

import os
import re
from dataclasses import dataclass, replace
from dataclasses import field as dataclass_field
from typing import Literal

from pubanon import _history_output
from pubanon._config import (
    GlobalConfig,
    Paths,
    ProjectConfig,
    assemble_rules,
    derived_private_terms,
    save_project,
)
from pubanon._errors import OperationCanceled, PubanonError, PublicationCanceled
from pubanon._gate import RuleClass
from pubanon._remote import _host, _lifecycle, _releases, _stage, _watch
from pubanon._remote._ci import (
    RUN_ON_PUSH_VARIABLE,
    RunQuery,
    evidence_facts,
    evidence_recovery,
    run_evidence,
)
from pubanon._remote._host import (
    Destination,
    RemoteRefs,
)
from pubanon._run import OID_HEX, git_failure_fact, run_git
from pubanon._snapshots import (
    _audit,
    _candidate,
    _history,
    _records,
    _reports,
    _source,
    _verification,
)
from pubanon._snapshots._candidate import (
    CANDIDATE_REF,
    STATE_RETAINED,
    STATE_UNKNOWN_OUTCOME,
    CandidateRecord,
    published_refs,
    save_candidate,
)
from pubanon._snapshots._records import load_candidate
from pubanon._snapshots._source import check_source_safety
from pubanon._snapshots._store import store_path
from pubanon._snapshots._verification import verify_retained_candidate
from pubanon._terminal import (
    confirm,
    detail,
    detail_fields,
    display_check_result,
    display_path,
    display_time,
    escape,
    field,
    fields,
    heading,
    next_step,
    output,
    path_text,
    project_fallback,
    shell_command,
    style,
    verbose_enabled,
)

CORRECTION_RESTORE_NOTE = (
    'Restoring briefly puts back the history you are correcting. To remove it afterward, '
    'see "Recreating the final repository" in the operator guide.'
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
    _release_page: _releases.PageRequest = dataclass_field(default_factory=_releases.PageRequest)


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


@dataclass(frozen=True)
class _Verified:
    """Bind a verified destination to its fresh remote references."""

    _destination: Destination
    _remote: RemoteRefs


def _acknowledge_exposure(
    paths: Paths,
    configs: tuple[GlobalConfig, ProjectConfig],
    audit: _audit.HistoryAudit,
    history: tuple[tuple[str, ...], dict[str, str]],
    *,
    unattended: bool,
) -> ProjectConfig:
    """Record an informed decision for current historical exposures."""
    _g, p = configs
    digest = _audit.exposure_digest(audit.findings)
    if not digest or digest == p.acknowledged_exposure:
        return p
    if unattended:
        raise PubanonError(
            '--yes cannot acknowledge historical exposure',
            recovery=('run pubanon audit to review the findings, then rerun without --yes'),
        )

    commits, tags = history
    lines = _history_output.finding_groups(
        audit,
        configs,
        derived_private_terms(paths, p, os.environ),
        commits,
        tags,
    )
    heading('Historical exposure review', separate=True)
    for line in lines:
        output(line)
    output('Locations show the first match for each finding; more matches may exist.')
    output(
        'Acknowledgment accepts these existing identity-critical historical findings. '
        'It does not remove published content or permit these terms in new snapshot content.',
        kind='warning',
    )
    if not confirm('Continue despite this earlier exposure?'):
        raise OperationCanceled('Canceled. Nothing was published.')
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
    derived = derived_private_terms(ctx._paths, ctx._project, os.environ)
    rules = assemble_rules(
        ctx._global_config,
        ctx._project,
        derived,
    )
    full_audit, range_audit = _audit.audit_history_pair(
        ctx._paths,
        ctx._project.name,
        full_commits,
        local_tags,
        focused_commits,
        missing_tags,
        rules,
        f'{ctx._global_config.anon_name} <{ctx._global_config.anon_email}>',
        tip=local_main,
    )

    if full_audit.identity_problems:
        raise PubanonError(
            'published history has identity problems',
            fact=escape(', '.join(full_audit.identity_problems)),
            recovery='pubanon audit',
        )
    range_details = _audit.history_details(
        range_audit,
        ctx._global_config,
        ctx._project,
        derived,
        short_commits=not verbose_enabled(),
    )
    for item in range_details:
        if item.rule_class is RuleClass.HYGIENE:
            label = _history_output.HISTORY_LABELS[item.rule_class]
            output(f'{label}: {escape(item.description)}', kind='warning')
    _acknowledge_exposure(
        ctx._paths,
        (ctx._global_config, ctx._project),
        full_audit,
        (full_commits, local_tags),
        unattended=ctx._yes,
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


def _destination_from_record(record: CandidateRecord) -> Destination:
    """Rebuild the recorded destination through the host URL seam."""
    destination = record.destination
    return Destination(
        adapter=destination.adapter,
        owner=destination.owner,
        repo=destination.repo,
        url=_host.repository_url(destination.owner, destination.repo),
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


def _local_ref_command(ref: str, old: str | None, new: str | None, zero: str) -> str:
    """Encode a guarded create, update, deletion, or unchanged-value check."""
    if old == new:
        return f'verify {ref} {old or zero}'
    if old is None:
        return f'create {ref} {new}'
    if new is None:
        return f'delete {ref} {old}'
    return f'update {ref} {new} {old}'


def _finalize(ctx: PublishContext) -> None:
    """Apply a guarded local transaction and remove candidate metadata last."""
    before = RemoteRefs(ctx._record.prestate_main, ctx._record.prestate_tags)
    after = _expected_pushed(ctx._record)
    actual = RemoteRefs(*published_refs(ctx._paths, ctx._project.name))
    if actual not in (before, after):
        raise PubanonError(
            'saved published history matches neither the state before this publication '
            'nor the state after it'
        )
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
        _records.candidate_path(ctx._paths, ctx._project.name).unlink(missing_ok=True)
    except OSError as exc:
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
        if isinstance(exc, OperationCanceled):
            raise
        raise PubanonError(
            'source preservation is incomplete',
            fact=str(exc),
            recovery='Restore source access or correct its registered location, then retry '
            'pubanon publish. Snapshot recovery information and any source bookmark are retained.',
        ) from exc
    return ctx


def _publication_cancelled(
    error: KeyboardInterrupt | OperationCanceled,
    *,
    headline: str,
    diagnostic_kind: Literal['warning', 'error', 'success'],
    fact: str,
    recovery: str,
) -> OperationCanceled:
    """Preserve cancellation details while adding publication lifecycle facts."""
    cancelled = error if isinstance(error, OperationCanceled) else OperationCanceled(headline)
    cancelled.args = (headline,)
    cancelled.diagnostic_kind = diagnostic_kind
    cancelled.fact = f'{fact}\n{cancelled.fact}' if cancelled.fact else fact
    cancelled.recovery = recovery
    return cancelled


def _refresh_source_tags(ctx: PublishContext) -> None:
    """Refresh opted-in local tags after finalization without changing the host outcome."""
    if not ctx._project.source_tags:
        return
    try:
        warnings = _history.refresh_source_tags(
            ctx._project, _history.publications(ctx._paths, ctx._project.name)
        )
    except (OSError, PubanonError) as error:
        if isinstance(error, OperationCanceled):
            raise
        warnings = (f'source tags could not be refreshed: {error}',)
    for warning in warnings:
        output(escape(warning), kind='warning')


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
            expected=escape(_host.remote_refs_text(RemoteRefs(remote.main, remote.tags, ()))),
            actual=escape(_host.remote_refs_text(remote)),
            recovery='remove every destination branch except main, then rerun',
        )


def _replacement_review(ctx: PublishContext, verified: _Verified, page: _releases.PagePlan) -> str:
    """Summarize the replacement scope before its single approval question."""
    record = ctx._record
    if record.replaces is None or record.prestate_main is None:
        raise PubanonError('replacement snapshot is incomplete')
    _, after_tags = _records.candidate_poststate(record)
    output('Replace published snapshot', kind='heading')
    rows = [
        (
            'Repository',
            f'{escape(verified._destination.owner)}/{escape(verified._destination.repo)}',
        ),
        ('Visibility', escape(verified._destination.visibility)),
    ]
    tags = sorted(tag for tag, oid in record.prestate_tags.items() if oid == record.replaces)
    rows.append(
        (
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
        rows.append(
            (
                'Scope',
                style(f'all published history ({count} snapshots) -> one snapshot', 'warning'),
            )
        )
    else:
        rows.append(('Scope', 'one published snapshot and its replayed descendants'))
    width = 40 if verbose_enabled() else 12
    rows.append(('Commit', f'{record.replaces[:width]} -> {record.commit[:width]}'))
    updated = sorted(
        tag for tag, oid in after_tags.items() if record.prestate_tags.get(tag) != oid
    )
    rows.append(('Updates', escape(', '.join(['main', *updated]))))
    moved = [tag for tag in updated if tag in record.prestate_tags]
    if moved:
        rows.append(('Moved tags', escape(', '.join(moved))))
    removed = sorted(record.prestate_tags.keys() - after_tags.keys())
    if removed:
        rows.append(('Removed tags', style(escape(', '.join(removed)), 'warning')))
    if record.replayed:
        rows.append(('Later snapshots', f'{len(record.replayed)} replayed'))
    rows.extend(
        ('Replayed commit', f'{entry.old[:width]} -> {entry.new[:width]}')
        for entry in record.replayed
    )
    fields(rows)
    _releases.show_page_plan(page)
    output()
    output(
        'warning: This changes branches and tags; old commits and GitHub activity, '
        'Actions logs, and artifacts '
        'may remain.',
        kind='warning',
    )
    output(
        'warning: Existing clones may retain old history; GitHub Releases and uploaded '
        'packages are unchanged.',
        kind='warning',
    )
    if verified._destination.visibility == 'public':
        output('warning: Public forks and caches may also retain the old commits.', kind='warning')
    suffix = ' and create its release page' if page.action == 'create' else ''
    return f'Publish this replacement{suffix}?'


def _publication_review_question(
    ctx: PublishContext,
    verified: _Verified,
    page: _releases.PagePlan,
    proof: _verification.ObjectProof,
) -> str:
    """Present the selected publication operation and return its confirmation."""
    report = _reports.publication_report(ctx._paths, proof, ctx._project)
    if ctx._record.replaces is not None:
        question = _replacement_review(ctx, verified, page)
    else:
        destination = verified._destination
        target = f'{escape(destination.owner)}/{escape(destination.repo)}'
        what = escape(ctx._record.tag_intent or 'untagged snapshot')
        heading(f'Publish {what}')
        count = len(_reports.tree_paths(ctx._paths, ctx._project.name, ctx._record.commit))
        changes = 'first release' if ctx._record.prestate_main is None else 'since last release'
        fields(
            [
                ('Destination', f'{target} ({escape(destination.visibility)})'),
                ('Changes', f'{changes}, {count} file{"s" if count != 1 else ""}'),
                ('Checked', 'content, identity, and destination'),
            ]
        )
        _releases.show_page_plan(page)
        suffix = ' and create its release page' if page.action == 'create' else ''
        question = f'Publish {what} to {target}{suffix}?'
    if ctx._project.settings is not None:
        output(field('Description', escape(ctx._project.settings.description) or '(empty)'))
    if verbose_enabled():
        output(report)
    else:
        warning = _reports.credential_warning(ctx._paths, ctx._project.name, ctx._record)
        if warning:
            output(warning)
    if ctx._record.prestate_main is None and ctx._project.publication_route == 'direct':
        output(
            'warning: GitHub keeps a record of this upload even if you later replace '
            'published history.',
            kind='warning',
        )
    return question


def _require_final_push_ci(destination: Destination) -> None:
    """Refuse a final repository that disables push-triggered CI."""
    value = _host.repository_variable((destination.owner, destination.repo), RUN_ON_PUSH_VARIABLE)
    if value is not None and value.casefold() == 'false':
        raise PubanonError(
            'the final repository turns off push CI',
            fact='CI_RUN_ON_PUSH is false in the final repository.',
            recovery='delete CI_RUN_ON_PUSH from the final repository on GitHub, '
            'then rerun pubanon publish',
        )


def _require_stage_ci(ctx: PublishContext, tip: str) -> None:
    """Require fresh successful workflow evidence for the verified staging tip."""
    destination, stage = _stage.verify_staged_target(ctx._paths, ctx._global_config, ctx._project)
    if stage.commit != tip:
        raise PubanonError(
            'staging evidence does not cover the retained snapshot',
            expected=escape(tip),
            actual=escape(stage.commit),
            recovery='pubanon stage push',
        )
    workflow = ctx._project.ci_workflow
    if workflow is None:
        return
    evidence = run_evidence(
        (destination.owner, destination.repo),
        RunQuery(workflow, tip, 'workflow_dispatch'),
        required=ctx._project.required_checks,
    )
    if evidence.state != 'success':
        raise PubanonError(
            'the latest staging CI run is not acceptable',
            fact=evidence_facts(evidence),
            recovery=evidence_recovery(evidence, staged=True, retry='pubanon stage watch'),
        )
    detail(field('Verified staged commit', escape(stage.commit)))
    detail(field('Accepted CI run', evidence_facts(evidence)))


def _review_publication(
    ctx: PublishContext, verified: _Verified, page: _releases.PagePlan
) -> None:
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
            ctx._paths,
            ctx._project.name,
            commits,
            tags,
            rules,
            f'{ctx._global_config.anon_name} <{ctx._global_config.anon_email}>',
            tip=main,
        )

        # Replacement identity is proved over its complete poststate before host work.
        if audit.identity_problems and ctx._record.replaces is None:
            raise PubanonError(
                'published history has identity problems',
                fact=escape(', '.join(audit.identity_problems)),
                recovery='pubanon audit',
            )
        _acknowledge_exposure(
            ctx._paths,
            (ctx._global_config, ctx._project),
            audit,
            (commits, tags),
            unattended=ctx._yes,
        )
    # Re-prove objects: the record was rewritten and prompts ran since _verify_for_publish.
    proof = _verification.prove_objects(ctx._paths, ctx._project.name, ctx._record)
    question = _publication_review_question(ctx, verified, page, proof)
    inspection = _verification.materialize_inspect(ctx._paths, ctx._project.name, proof)
    if verbose_enabled():
        output(field('Review copy', display_path(inspection)))
    if not ctx._yes and not confirm(question):
        raise PublicationCanceled(
            'Canceled. Nothing was published.',
            fact='The prepared snapshot is kept.',
            recovery='pubanon publish',
        )


def _save_publication_state(
    ctx: PublishContext, record: CandidateRecord, *, cancellation_fact: str
) -> None:
    """Preserve known host facts if interrupted around an atomic record write."""
    try:
        save_candidate(ctx._paths, ctx._project.name, record)
    except (OperationCanceled, KeyboardInterrupt) as error:
        current = load_candidate(ctx._paths, ctx._project.name)
        recovery = (
            'pubanon publish --resolve'
            if current is not None and current.state == STATE_UNKNOWN_OUTCOME
            else 'pubanon publish'
        )
        raise _publication_cancelled(
            error,
            headline='Canceled. Nothing was published.',
            diagnostic_kind='warning',
            fact=cancellation_fact,
            recovery=recovery,
        ) from None


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


def _sync_description(ctx: PublishContext, destination: Destination) -> None:
    """Apply the reviewed description while retaining retryable snapshot state."""
    settings = ctx._project.settings
    if settings is None:
        raise PubanonError('repository settings are missing')
    fact = (
        'No code was pushed. The repository description may have changed. '
        'The prepared snapshot is kept.'
    )
    try:
        changed = _host.sync_repository_description(destination, settings.description)
    except (OperationCanceled, KeyboardInterrupt) as error:
        raise _publication_cancelled(
            error,
            headline='Canceled while updating the repository description.',
            diagnostic_kind='warning',
            fact=fact,
            recovery='pubanon publish',
        ) from None
    except (OSError, PubanonError) as error:
        reason = error.render() if isinstance(error, PubanonError) else str(error)
        raise PubanonError(
            'repository description could not be synchronized',
            fact=f'{fact}\n{reason}',
            recovery='Correct the reported problem, then rerun pubanon publish.',
        ) from error
    if changed:
        output('Updated repository description.')


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
    if outcome != 'pushed' and _verification.confirmed_publication(
        ctx._paths, ctx._project.name, ctx._record
    ):
        outcome = 'pushed'
        drift = (
            'current host refs differ from the confirmed published snapshot: '
            + _host.remote_refs_text(remote)
        )
    escaped_drift = None if drift is None else escape(drift)
    if outcome == 'pushed':
        if ctx._record.source_bookmark is None:
            raise PubanonError('confirmed snapshot source bookmark is missing')
        try:
            _history.record_publication(ctx._paths, ctx._project, ctx._record)
            _finalize(ctx)
            _refresh_source_tags(ctx)
        except (OSError, PubanonError, KeyboardInterrupt) as error:
            failure = (
                error
                if isinstance(error, PubanonError)
                else OperationCanceled('Local finalization is incomplete.')
                if isinstance(error, KeyboardInterrupt)
                else PubanonError(str(error))
            )
            landed = (
                f'Published and verified commit {escape(ctx._record.commit)} '
                'on the host repository.'
            )
            failure.fact = f'{landed}\n{failure.fact}' if failure.fact else landed
            if (
                isinstance(failure, OperationCanceled)
                and not _records.candidate_path(ctx._paths, ctx._project.name).exists()
            ):
                failure.diagnostic_kind = 'success'
                failure.args = ('Publication and local finalization completed.',)
                failure.recovery = 'pubanon status --remote; do not publish a new snapshot'
                raise failure from None
            if isinstance(failure, OperationCanceled):
                failure.diagnostic_kind = 'error'
            failure.args = (
                'Local finalization is incomplete.'
                if isinstance(failure, OperationCanceled)
                else f'Local finalization is incomplete: {failure}',
            )
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
            'before: '
            + escape(
                _host.remote_refs_text(
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
            + '; after push: '
            + escape(_host.remote_refs_text(RemoteRefs(pushed.main, pushed_tags)))
        ),
        actual=escape(_host.remote_refs_text(RemoteRefs(remote.main, actual_tags))),
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
        if isinstance(error, OperationCanceled):
            raise
        current = load_candidate(ctx._paths, ctx._project.name)
        retry = (
            'pubanon publish --resolve'
            if current is not None and current.state == STATE_UNKNOWN_OUTCOME
            else 'pubanon publish'
        )
        raise PubanonError(
            'Nothing was published; snapshot state could not be saved',
            fact=f'Source repository: {escape(path_text(ctx._project.source_path))}\n{error}',
            recovery=f'Restore private state write access, then run {retry}. '
            'The prepared snapshot and source bookmark are retained.',
        ) from error
    ctx = replace(ctx, _record=unknown)
    push_error: PubanonError | None = None
    try:
        _host.push(
            verified._destination,
            store_path(ctx._paths, ctx._project.name),
            _push_args(ctx._record),
        )
    except (OperationCanceled, KeyboardInterrupt) as error:
        raise _publication_cancelled(
            error,
            headline='the outcome of the last publication is unknown',
            diagnostic_kind='error',
            fact='Recovery information is retained.',
            recovery='pubanon publish --resolve',
        ) from None
    except PubanonError as error:
        push_error = error
    try:
        remote = _host.ls_remote(verified._destination, ctx._paths, ctx._project.name)
    except (OperationCanceled, KeyboardInterrupt) as error:
        raise _publication_cancelled(
            error,
            headline='the outcome of the last publication is unknown',
            diagnostic_kind='error',
            fact='Recovery information is retained.',
            recovery='pubanon publish --resolve',
        ) from None
    except PubanonError as read_error:
        facts = [read_error.render()]
        if push_error is not None:
            facts.insert(0, push_error.render())
        raise PubanonError(
            'the outcome of the last publication is unknown',
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


def _verify_publication_route(ctx: PublishContext) -> None:
    """Require the exact private staged candidate and every configured CI check."""
    if ctx._project.publication_route == 'direct':
        return
    tip = _records.candidate_tip(ctx._record)
    stage = _stage.load_stage(ctx._paths, ctx._project.name)
    if stage is not None and stage.commit != tip:
        raise PubanonError(
            'staging evidence does not cover the retained snapshot',
            expected=escape(tip),
            actual=escape(stage.commit),
            recovery='pubanon stage push',
        )
    if ctx._project.ci_workflow:
        _require_stage_ci(ctx, tip)
        return
    destination, stage = _stage.verify_staged_target(ctx._paths, ctx._global_config, ctx._project)
    verdicts = _watch.ci_verdicts(destination, tip, ctx._project.required_checks)
    unresolved = [verdict for verdict in verdicts if verdict.state != 'success']
    if unresolved:
        raise PubanonError(
            'required staging checks are not acceptable',
            fact=', '.join(
                f'{escape(verdict.name)}: {display_check_result(verdict)}'
                for verdict in unresolved
            ),
            recovery=_host.check_recovery(unresolved, retry='pubanon stage watch'),
        )
    detail(field('Verified staged commit', escape(stage.commit)))
    if verdicts:
        detail('Required staging checks passed.')
        for verdict in verdicts:
            detail(field(escape(verdict.name), display_check_result(verdict)))
    else:
        output('No required staging checks configured.', kind='note')


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
    if ctx._yes:
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


def empty_destination_fact(owner: str, repo: str) -> str:
    """Explain an empty final repository that already has saved published history."""
    return (
        f'{escape(f"{owner}/{repo}")} has no branches or tags, but pubanon has already '
        'published to it. This happens when the repository is deleted and recreated.'
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
    if remote != expected and remote == RemoteRefs(None, {}):
        destination = record.destination
        if record.replaces is None:
            raise PubanonError(
                'the final repository is empty',
                fact=empty_destination_fact(destination.owner, destination.repo),
                recovery='pubanon repo restore --to remote, then rerun pubanon publish',
            )
        raise PubanonError(
            'the final repository is empty',
            fact='This snapshot corrects published history that '
            f'{escape(f"{destination.owner}/{destination.repo}")} no longer has. This happens '
            'when the repository is deleted and recreated before the correction is published.',
            recovery='pubanon repo restore --to remote, then rerun pubanon publish\n'
            + CORRECTION_RESTORE_NOTE,
        )
    if remote != expected:
        raise PubanonError(
            'the final repository does not match what this snapshot expects',
            expected=escape(_host.remote_refs_text(expected)),
            actual=escape(_host.remote_refs_text(remote)),
            recovery='pubanon status --remote, then see "Accepting an outside fix" in the '
            'operator guide',
        )


def _verify_for_publish(ctx: PublishContext) -> _Verified:
    """Re-prove the closed publication verification list in order."""
    if _records.rewrite_path(ctx._paths, ctx._project.name).exists():
        raise PubanonError(
            'rewrite completion is pending',
            recovery=shell_command(
                'pubanon', 'prepare', f'--project={ctx._project.name}', '--continue'
            )
            or project_fallback(ctx._project.name, 'prepare', '--continue'),
        )
    verify_retained_candidate(
        ctx._paths, ctx._global_config, ctx._project, ctx._record, env=os.environ
    )
    _lifecycle.gate_host_fields(ctx._paths, ctx._global_config, ctx._project)
    _verify_source(ctx)
    destination = _host.verify_destination(ctx._paths, ctx._global_config, ctx._project)
    if ctx._project.ci_workflow:
        _require_final_push_ci(destination)
    recorded = _destination_from_record(ctx._record)
    if destination != recorded:
        raise PubanonError('verified destination does not match the snapshot record')
    remote = _host.ls_remote(destination, ctx._paths, ctx._project.name)
    _verify_remote(ctx._record, remote)
    return _Verified(destination, remote)


def publication_next_action(project: ProjectConfig, record: CandidateRecord | None = None) -> str:
    """Choose the next configured observation or lifecycle step after publication.

    A replacement record must contain its completed poststate. Return an empty
    string when no configured next step exists.
    """
    if project.required_checks or project.ci_workflow:
        return 'pubanon repo watch'
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
        return f'pubanon package watch{version}'
    if project.visibility == 'private':
        return 'pubanon repo reveal'
    return ''


def report_source_preservation(
    paths: Paths, project: str, commit: str, *, source_tags: bool = False
) -> None:
    """Report recorded source preservation and current local availability.

    `commit` must be a complete publication object ID. Missing provenance or
    unavailable local recovery produces warning output; successful recovery is
    not required for this report.
    """
    record = _records.load_publication(paths, project, commit)
    bookmark = None if record is None else record.source_bookmark
    source = None
    if record is not None:
        source = _records.publication_source(record)
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
            output('A private bookmark in your source repository keeps the original commit.')
            detail_fields([('Source bookmark', escape(bookmark.refname))])
    if source_tags:
        detail('Source branches, HEAD, index, and files were not changed.')
    else:
        detail('Source branches, tags, HEAD, index, and files were not changed.')


def _report_publication(ctx: PublishContext) -> None:
    """Describe completed publication and private preservation."""
    recorded = ctx._record.destination
    what = escape(ctx._record.tag_intent or 'untagged snapshot')
    output(
        f'Published {what} to {escape(recorded.owner)}/{escape(recorded.repo)} '
        f'({escape(recorded.visibility)}).',
        kind='success',
    )
    publication = _records.load_publication(ctx._paths, ctx._project.name, ctx._record.commit)
    if publication is not None:
        detail_fields([('Published', display_time(publication.confirmed_at))])
    if ctx._record.replaces is None:
        detail_fields([('Snapshot commit', ctx._record.commit)])
    else:
        corrected = sorted(
            tag for tag, oid in ctx._record.prestate_tags.items() if oid == ctx._record.replaces
        )
        label = ', '.join(escape(tag) for tag in corrected) or ctx._record.replaces[:12]
        rows = [('Corrected snapshot', f'{label} ({ctx._record.replaces[:12]})')]
        tip = _records.candidate_tip(ctx._record)
        if tip != ctx._record.commit:
            rows.append(
                (
                    'Correction commit',
                    ctx._record.commit if verbose_enabled() else ctx._record.commit[:12],
                )
            )
        if ctx._record.replayed:
            rows.append(('Replayed snapshots', str(len(ctx._record.replayed))))
        moved = ', '.join(escape(tag) for tag in _changed_tags(ctx._record)) or 'none'
        rows.append(('Moved tags', moved))
        rows.append(('Snapshot commit', tip if verbose_enabled() else tip[:12]))
        fields(rows)
    report_source_preservation(
        ctx._paths, ctx._project.name, ctx._record.commit, source_tags=ctx._project.source_tags
    )


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
        OperationCanceled: Publication is cancelled with the recovery state
            described above.
        PubanonError: Verification, confirmation, push, or finalization fails.
            Publication may already have landed; use the reported recovery
            action before retrying.
    """
    _candidate.require_known_outcome(ctx._record)
    try:
        verified = _verify_for_publish(ctx)
        page = _releases.plan_publication_page(
            ctx._paths, ctx._global_config, ctx._project, ctx._record, ctx._release_page
        )
        _verify_publication_route(ctx)
        ctx = _preserve_source(ctx)
        ctx = _preserve_replayed_sources(ctx)
        _history.preserve_previous_tip(ctx._paths, ctx._project.name, ctx._record)
        _review_publication(ctx, verified, page)
        if ctx._project.ci_workflow and ctx._project.publication_route != 'direct':
            _require_stage_ci(ctx, _records.candidate_tip(ctx._record))
    except (OperationCanceled, KeyboardInterrupt) as error:
        raise PublicationCanceled(
            'Canceled. Nothing was published.',
            fact='The prepared snapshot is kept.',
            recovery='pubanon publish',
        ) from error
    except (OSError, PubanonError) as error:
        failure = error if isinstance(error, PubanonError) else PubanonError(str(error))
        retained = (
            'Nothing was published. No push was attempted; snapshot recovery information and any '
            'source bookmark are retained. '
            f'Source repository: {escape(path_text(ctx._project.source_path))}'
        )
        failure.fact = f'{retained}\n{failure.fact}' if failure.fact else retained
        if failure.recovery is None:
            failure.recovery = 'Correct the reported problem, then retry pubanon publish.'
        raise failure from None
    _sync_description(ctx, verified._destination)
    _push_candidate(ctx, verified)
    try:
        _report_publication(ctx)
    except (OperationCanceled, KeyboardInterrupt) as error:
        raise _publication_cancelled(
            error,
            headline='Publication and local finalization completed.',
            diagnostic_kind='success',
            fact=(
                f'Published and verified commit {escape(ctx._record.commit)} '
                'on the host repository.'
            ),
            recovery='pubanon status --remote; do not publish a new snapshot',
        ) from None

    outcome = _releases.complete_publication_page(
        ctx._paths, ctx._global_config, ctx._project, page
    )
    if outcome.url is not None:
        label = 'Release page already exists' if outcome.kind == 'existing' else 'Release page'
        output(field(label, escape(outcome.url)))
    else:
        _releases.show_page_plan(page, published=True)
    output()
    next_step(publication_next_action(ctx._project, ctx._record))


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
        OperationCanceled: Outcome resolution is cancelled; local finalization
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
        destination = _host.verify_recorded_destination(recorded)
        remote = _host.ls_remote(destination, ctx._paths, ctx._project.name)
    except (OperationCanceled, KeyboardInterrupt) as error:
        raise _publication_cancelled(
            error,
            headline='the outcome of the last publication is unknown',
            diagnostic_kind='error',
            fact='Recovery information is retained.',
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
    _candidate.require_known_outcome(record)
    local_main, local_tags = published_refs(ctx._paths, ctx._project.name)
    if local_main is None:
        raise PubanonError(
            'local published history has no main branch',
            recovery='pubanon repo restore --to local',
        )
    destination = _host.verify_destination(ctx._paths, ctx._global_config, ctx._project)
    remote = _host.ls_remote(destination, ctx._paths, ctx._project.name)
    _refuse_sync_branches(remote)
    expected = RemoteRefs(local_main, local_tags, ())
    if remote == expected:
        output('The final repository already matches saved published history.', kind='success')
        output()
        return
    _verify_sync_prefix(ctx, local_main, local_tags, remote)

    args = _sync_args(local_main, local_tags, remote)
    _audit_sync(ctx, local_main, local_tags, remote)
    heading(
        f'Restore {escape(destination.owner)}/{escape(destination.repo)} '
        'from saved published history'
    )
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
    fields(
        [
            ('Visibility', destination.visibility),
            ('Snapshots', str(len(commits))),
            (
                'Branch main',
                f'{remote.main[:12] if remote.main else "empty"} -> {local_main[:12]}',
            ),
            (
                'Tags to restore',
                escape(', '.join(sorted(local_tags.keys() - remote.tags.keys())) or 'none'),
            ),
            ('Checked', 'published history content and identity'),
        ]
    )
    detail_fields(
        [
            ('Sent range', f'{remote.main or "empty"}..{local_main}'),
            *((escape(label), escape(value)) for label, value in metadata.items()),
        ]
    )
    output(
        'warning: this makes the restored history publicly accessible.'
        if destination.visibility == 'public'
        else 'This sends the restored history to the private final repository.',
        kind='warning' if destination.visibility == 'public' else 'plain',
    )
    for arg in args:
        detail(f'  {escape(arg)}')
    if not ctx._yes and not confirm(
        'Restore these branches and tags to '
        f'{escape(destination.owner)}/{escape(destination.repo)}?'
    ):
        raise OperationCanceled('Canceled. Nothing was pushed.')
    _host.push(destination, store_path(ctx._paths, ctx._project.name), args)
    readback = _host.ls_remote(destination, ctx._paths, ctx._project.name)
    if readback != expected:
        raise PubanonError(
            'the final repository does not match saved published history after restoration',
            expected=escape(_host.remote_refs_text(expected)),
            actual=escape(_host.remote_refs_text(readback)),
            recovery='rerun pubanon repo restore --to remote',
        )
    output(
        f'Restored {escape(destination.owner)}/{escape(destination.repo)} '
        'from saved published history.',
        kind='success',
    )
    output('Local published history and any retained snapshot were not changed.')
    output('The source repository was not changed.', kind='note')
    output()
