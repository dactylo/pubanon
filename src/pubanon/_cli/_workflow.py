"""Compose release and declared workflow commands."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING

from pubanon import (
    _config,
)
from pubanon._cli import (
    _context,
    _inspect,
    _prepare,
)
from pubanon._cli import _releases as cli_releases
from pubanon._cli import _remote as cli_remote
from pubanon._errors import OperationCanceled, PubanonError
from pubanon._remote import _host, _lifecycle, _publish, _releases, _stage, _watch
from pubanon._snapshots import _candidate, _records, _reports, _verification
from pubanon._terminal import (
    escape,
    guidance,
    guidance_context,
    heading,
    next_step,
    output,
    project_command,
)

if TYPE_CHECKING:
    import argparse

_CANDIDATE_BOUND_STAGES = frozenset({'inspect', 'prepare', 'publish', 'stage push', 'stage watch'})


@dataclass(frozen=True)
class _RunPlan:
    """Retain one verified declared-workflow invocation."""

    ctx: _context.ProjectContext
    stages: tuple[str, ...]
    remaining_stages: tuple[str, ...]
    record: _records.CandidateRecord | None
    intended: str | None
    published_ctx: _context.ProjectContext | None
    stage_creation_needed: bool
    release_page: _releases.PageRequest = field(default_factory=_releases.PageRequest)


@dataclass
class _RunState:
    """Track one in-process declared-workflow execution."""

    ctx: _context.ProjectContext
    record: _records.CandidateRecord | None
    published_completed: bool
    release_page: _releases.PageRequest = field(default_factory=_releases.PageRequest)


def _published_run_context(
    ctx: _context.ProjectContext,
    stages: tuple[str, ...],
    intended: str | None,
) -> _context.ProjectContext | None:
    """Return the verified host context when the declared publication completed."""
    if 'publish' not in stages or intended is None:
        return None
    private_error: PubanonError | None = None
    try:
        destination = _host.verify_destination(ctx.paths, ctx.global_config, ctx.project)
    except PubanonError as exc:
        publish_index = stages.index('publish')
        reveal_follows = 'repo reveal' in stages[publish_index + 1 :]
        if not reveal_follows or ctx.project.visibility != 'private':
            raise
        private_error = exc
        ctx = replace(ctx, project=replace(ctx.project, visibility='public'))
        try:
            destination = _host.verify_destination(ctx.paths, ctx.global_config, ctx.project)
        except PubanonError:
            raise private_error from None
    remote = _host.ls_remote(destination, ctx.paths, ctx.project.name)
    _local_main, local_tags = _candidate.published_refs(ctx.paths, ctx.project.name)
    remote_commit = remote.tags.get(intended)
    local_commit = local_tags.get(intended)
    if remote_commit is None and local_commit is None:
        if private_error is not None:
            raise private_error from None
        return None
    if remote_commit is not None and remote_commit == local_commit:
        return ctx
    raise PubanonError(
        'published version state is ambiguous',
        expected=(f'local {escape(intended)}: {escape(local_commit or "absent")}'),
        actual=(f'host {escape(intended)}: {escape(remote_commit or "absent")}'),
        recovery='pubanon repo verify',
    )


def _resolve_run_plan(
    args: argparse.Namespace, ctx: _context.ProjectContext, page: _releases.PageRequest
) -> _RunPlan:
    """Validate and bind one declared workflow invocation."""
    record = _records.load_candidate(ctx.paths, ctx.project.name)
    _candidate.require_known_outcome(record)
    _context.refuse_retained_rewrite(ctx.paths, ctx.project.name, record)
    stages = ctx.project.workflows.get(args.name)
    if stages is None:
        declared = ', '.join(sorted(ctx.project.workflows)) or 'none'
        raise PubanonError(
            f'workflow is not declared: {escape(args.name)}',
            fact=f'declared workflows: {escape(declared)}',
        )
    if 'publish' not in stages and (page.skip or page.requested or page.notes is not None):
        raise PubanonError('release page options require a workflow with a publish stage')
    if 'publish' in stages and args.version is None and record is None:
        raise PubanonError(
            'a prepared snapshot or --version is required for a workflow containing publish',
            recovery='run pubanon prepare first, or use --version to select a release',
        )
    intended = _candidate.tag_intent_for(args.version)
    if args.version is None and record is not None:
        intended = record.tag_intent
    if (
        record is not None
        and _CANDIDATE_BOUND_STAGES.intersection(stages)
        and record.tag_intent != intended
    ):
        raise PubanonError(
            'the prepared snapshot has a different release tag',
            expected=escape(intended or 'none'),
            actual=escape(record.tag_intent or 'none'),
            recovery='to use the prepared snapshot: pubanon inspect and follow its next step; '
            'to replace it: pubanon discard, then pubanon prepare',
        )
    published_ctx = _published_run_context(ctx, stages, intended)
    publish_index = stages.index('publish') if 'publish' in stages else -1
    remaining = stages[publish_index + 1 :] if published_ctx is not None else stages
    stage_creation_needed = False
    if 'stage push' in remaining:
        stage_creation_needed = not _stage.staging_repository_visible(
            ctx.paths, ctx.global_config, ctx.project
        )
    return _RunPlan(
        ctx,
        stages,
        remaining,
        record,
        intended,
        published_ctx,
        stage_creation_needed,
        page,
    )


def _run_inspect_stage(state: _RunState, *, publishing: bool) -> None:
    """Materialize and report one retained snapshot for workflow inspection."""
    if state.record is None:
        raise PubanonError('no prepared snapshot is retained', recovery='pubanon prepare')
    ctx = state.ctx
    proof = _verification.prove_objects(ctx.paths, ctx.project.name, state.record)
    _inspect.inspection_copy(ctx, proof, workflow=True)
    print('Inspection does not pause this workflow for file review.')
    if not publishing:
        print(_reports.publication_report(ctx.paths, proof, ctx.project))


def _run_prepare_stage(args: argparse.Namespace, state: _RunState) -> None:
    """Execute or resume one declared prepare stage."""
    if state.record is None:
        state.record = _prepare.prepare_locked(
            state.ctx,
            _prepare.PrepareOptions(
                source='HEAD',
                version=args.version,
                message=None,
                acknowledge_new_paths=args.acknowledge_new_paths,
                yes=args.yes,
            ),
        )
        state.ctx = _context.load_context(state.ctx.paths, state.ctx.project.name)
        return
    _verification.verify_retained_candidate(
        state.ctx.paths,
        state.ctx.global_config,
        state.ctx.project,
        state.record,
        env=os.environ,
    )


def _run_publish_stage(args: argparse.Namespace, state: _RunState) -> None:
    """Verify the invocation tag and invoke the publication primitive."""
    if state.record is None:
        raise PubanonError('no prepared snapshot is retained', recovery='pubanon prepare')
    intended = _candidate.tag_intent_for(args.version)
    if state.record.tag_intent != intended:
        raise PubanonError(
            'the prepared snapshot has a different release tag',
            expected=escape(intended or 'none'),
            actual=escape(state.record.tag_intent or 'none'),
            recovery='to use the prepared snapshot: pubanon inspect and follow its next step; '
            'to replace it: pubanon discard, then pubanon prepare',
        )
    _publish.publish(
        cli_remote.publish_context(
            state.ctx,
            state.record,
            yes=args.yes,
            release_page=state.release_page,
        )
    )
    state.published_completed = True


def _run_reveal_stage(args: argparse.Namespace, state: _RunState) -> None:
    """Invoke reveal without answering its final question."""
    if args.yes:
        raise PubanonError(
            '--yes cannot make the repository public',
            recovery='run pubanon repo reveal interactively',
        )
    _lifecycle.reveal(state.ctx.paths, state.ctx.global_config, state.ctx.project)
    state.ctx = _context.load_context(state.ctx.paths, state.ctx.project.name)


def _run_stage_push_stage(args: argparse.Namespace, plan: _RunPlan, state: _RunState) -> None:
    """Recheck staging visibility and invoke the staging primitive."""
    visible = _stage.staging_repository_visible(
        state.ctx.paths, state.ctx.global_config, state.ctx.project
    )
    if (not visible) != plan.stage_creation_needed:
        raise PubanonError(
            'staging repository visibility changed after preview',
            recovery='rerun the workflow for a current preview',
        )
    _stage.stage_push(state.ctx.paths, state.ctx.global_config, state.ctx.project, yes=args.yes)


def _run_wants_page(plan: _RunPlan) -> bool:
    """Select page work for a tagged publishing workflow and its overrides."""
    page = plan.release_page
    return (
        'publish' in plan.stages
        and plan.intended is not None
        and not page.skip
        and (
            page.requested
            or page.notes is not None
            or _config.release_pages_enabled(plan.ctx.project)
        )
    )


def _resume_run_page(args: argparse.Namespace, plan: _RunPlan) -> None:
    """Create a missing page with a fresh preview under the workflow lock."""
    ctx = plan.published_ctx
    if ctx is None or not _run_wants_page(plan):
        return
    tag = plan.intended
    if tag is None:
        raise ValueError('release page resume requires a publication tag')
    page = plan.release_page
    if page.notes is None and _releases.page_status(
        ctx.paths, ctx.global_config, ctx.project, tag
    ):
        return
    cli_releases.create_page(
        ctx,
        _releases.ReleaseRequest('create', tag, None, page.notes, page.notes_file),
        yes=args.yes,
    )


def _run_mutations(plan: _RunPlan) -> list[str]:
    """Return every external mutation in the remaining declared stages."""
    mutations: list[str] = []
    draft = 'draft ' if _config.draft_release_pages(plan.ctx.project) else ''
    page_change = f'create {draft}release page {plan.intended} if none exists'
    if plan.published_ctx is not None and _run_wants_page(plan):
        mutations.append(page_change)
    for stage in plan.remaining_stages:
        if stage == 'stage push':
            if plan.stage_creation_needed:
                mutations.append(
                    'create staging repository '
                    f'{plan.ctx.project.owner}/{_config.stage_repository(plan.ctx.project)} '
                    '(first use)'
                )
            mutations.append('replace the files in the private staging repository')
        elif stage == 'publish':
            mutations.append(
                f'publish to {plan.ctx.project.owner}/{plan.ctx.project.repo} on main'
            )
            if plan.intended is not None:
                mutations.append(f'create release tag {plan.intended}')
            if _run_wants_page(plan):
                mutations.append(page_change)
        elif stage == 'repo reveal':
            mutations.append('make the final repository public')
    return mutations


def _run_watch_ci_stage(args: argparse.Namespace, state: _RunState, *, staged: bool) -> None:
    """Invoke one exact-target CI watch stage."""
    _watch.watch_ci(
        state.ctx.paths,
        state.ctx.global_config,
        state.ctx.project,
        staged=staged,
        version=None if staged else args.version,
        timeout=_watch.DEFAULT_TIMEOUT,
    )


def _run_watch_package_stage(args: argparse.Namespace, state: _RunState) -> None:
    """Invoke one version-bound package watch stage."""
    _watch.watch_package(
        state.ctx.paths,
        state.ctx.global_config,
        state.ctx.project,
        version=args.version,
        timeout=_watch.DEFAULT_TIMEOUT,
    )


def _execute_run_stage(
    args: argparse.Namespace,
    plan: _RunPlan,
    state: _RunState,
    stage: str,
) -> None:
    """Dispatch one declared stage to its existing primitive boundary."""
    if stage == 'prepare':
        _run_prepare_stage(args, state)
    elif stage == 'inspect':
        _run_inspect_stage(state, publishing='publish' in plan.remaining_stages)
    elif stage == 'stage push':
        _run_stage_push_stage(args, plan, state)
    elif stage == 'stage watch':
        _run_watch_ci_stage(args, state, staged=True)
    elif stage == 'publish':
        _run_publish_stage(args, state)
    elif stage == 'repo watch':
        _run_watch_ci_stage(args, state, staged=False)
    elif stage == 'repo reveal':
        _run_reveal_stage(args, state)
    elif stage == 'package watch':
        _run_watch_package_stage(args, state)
    else:
        raise PubanonError(f'workflow stage is not implemented: {stage}')


def _show_run_preview(name: str, plan: _RunPlan) -> None:
    """Print one declared workflow's resolved stages and mutations."""
    stages = ', '.join(plan.stages)
    output(
        f'Workflow {escape(name)} ({escape(plan.ctx.project.publication_route)} route): '
        f'{escape(stages)}'
    )
    mutations = _run_mutations(plan)
    if mutations:
        heading('Changes to GitHub', separate=True)
        for mutation in mutations:
            print(f'  {escape(mutation)}')


def _stepwise_commands(
    args: argparse.Namespace,
    state: _RunState,
    stages: tuple[str, ...],
) -> str:
    """Render exact individual recovery commands after publication."""
    commands: list[str] = []
    name = state.ctx.project.name
    version_args = ('--version', args.version) if args.version is not None else ()
    for stage in stages:
        if stage in {'repo watch', 'package watch'}:
            command = project_command(name, *stage.split(), f'--project={name}', *version_args)
        else:
            command = project_command(name, *stage.split(), f'--project={name}')
        commands.append(f'  {command}')
    return 'finish stepwise:\n' + '\n'.join(commands)


def _workflow_recovery(recovery: str | None, stepwise: str) -> str:
    """Keep stage advice and list only follow-up commands it does not already name."""
    if recovery is None:
        return stepwise
    recovery = guidance(recovery)
    remaining = [
        line.strip()
        for line in guidance(stepwise).splitlines()[1:]
        if line.strip() not in recovery
    ]
    return recovery + ('\nThen finish stepwise:\n' + '\n'.join(remaining) if remaining else '')


def _execute_run(args: argparse.Namespace, plan: _RunPlan) -> None:
    """Execute every remaining stage with post-publication recovery."""
    state = _RunState(plan.ctx, plan.record, plan.published_ctx is not None, plan.release_page)
    for index, stage in enumerate(plan.remaining_stages):
        try:
            if stage not in {'publish', 'repo reveal'} and (stage != 'stage push' or args.yes):
                heading(stage.capitalize(), separate=True)
            _execute_run_stage(args, plan, state, stage)
        except (PubanonError, KeyboardInterrupt) as error:
            exc = error if isinstance(error, PubanonError) else OperationCanceled('Canceled.')
            recovery_index = index
            if not state.published_completed and stage == 'publish':
                if _records.load_candidate(state.ctx.paths, state.ctx.project.name) is not None:
                    raise exc from None
                try:
                    state.published_completed = (
                        _published_run_context(state.ctx, plan.stages, plan.intended) is not None
                    )
                except (PubanonError, KeyboardInterrupt):
                    raise exc from None
                if state.published_completed:
                    recovery_index += 1
            if not state.published_completed:
                raise exc from None
            fact = exc.fact
            completed = 'The publication itself completed.'
            exc.fact = f'{fact}\n{completed}' if fact else completed
            stepwise = _stepwise_commands(args, state, plan.remaining_stages[recovery_index:])
            exc.recovery = _workflow_recovery(exc.recovery, stepwise)
            raise exc from None


def command_run(args: argparse.Namespace) -> None:
    """Execute one declared workflow while holding the project lock.

    Raises:
        PubanonError: Configuration, workflow selection, or a constituent
            workflow stage fails.
        OperationCanceled: An interactive decision is declined or the operation
            is canceled.
    """
    page = cli_releases.page_request(args)
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        plan = _resolve_run_plan(args, _context.load_context(paths, name), page)
        if args.version is None and 'publish' in plan.stages and plan.intended is not None:
            args.version = plan.intended.removeprefix('v')
        _show_run_preview(args.name, plan)
        _resume_run_page(args, plan)
        if plan.published_ctx is not None and not plan.remaining_stages:
            output(f'Nothing to do: {escape(plan.intended or "untagged")} is already published.')
            return
        if args.yes and plan.stage_creation_needed:
            raise PubanonError(
                '--yes cannot answer first-use staging repository creation',
                recovery='run interactively: pubanon stage push',
            )
        if args.yes:
            output('--yes: stages continue without asking.', kind='note')
        with guidance_context(workflow=True):
            _execute_run(args, plan)
        output(f'Workflow {escape(args.name)} completed.', kind='success')
        if _records.load_candidate(paths, name) is not None:
            print('The snapshot is prepared but has not been published.')
            next_step('pubanon inspect')
        elif 'repo reveal' not in plan.stages and (
            following := _watch.next_after_publication(
                plan.ctx.project,
                version=args.version,
                ci_observed='repo watch' in plan.stages,
                package_observed='package watch' in plan.stages,
            )
        ):
            next_step(following)
