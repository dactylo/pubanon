"""Compose release and declared workflow commands."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from pubanon import (
    _config,
)
from pubanon._cli import (
    _context,
    _inspect,
    _prepare,
)
from pubanon._cli import _remote as cli_remote
from pubanon._errors import OperationCancelled, PubanonError
from pubanon._remote import _host, _lifecycle, _publish, _stage, _watch
from pubanon._snapshots import _candidate
from pubanon._terminal import (
    confirm,
    escape,
    guidance_context,
    heading,
    next_step,
    output,
)

if TYPE_CHECKING:
    import argparse

_CANDIDATE_BOUND_STAGES = frozenset(
    {'inspect', 'prepare', 'publish', 'stage push', 'watch ci --staged'}
)


@dataclass(frozen=True)
class _RunPlan:
    """Retain one verified declared-workflow invocation."""

    _ctx: _context.ProjectContext
    _stages: tuple[str, ...]
    _remaining_stages: tuple[str, ...]
    _record: _candidate.CandidateRecord | None
    _intended: str | None
    _already_published: bool
    _stage_creation_needed: bool


@dataclass
class _RunState:
    """Track one in-process declared-workflow execution."""

    _ctx: _context.ProjectContext
    _record: _candidate.CandidateRecord | None
    _published_completed: bool


def _published_run_version(
    ctx: _context.ProjectContext,
    stages: tuple[str, ...],
    intended: str | None,
) -> bool:
    """Classify whether one declared publication already completed."""
    if 'publish' not in stages:
        return False
    if intended is None:
        raise PubanonError('workflow containing publish requires --version')
    host = _host
    private_error: PubanonError | None = None
    try:
        destination = host.verify_destination(ctx.paths, ctx.global_config, ctx.project)
    except PubanonError as exc:
        publish_index = stages.index('publish')
        reveal_follows = 'repo reveal' in stages[publish_index + 1 :]
        if not reveal_follows or ctx.project.visibility != 'private':
            raise
        private_error = exc
        try:
            destination = host.verify_destination(
                ctx.paths, ctx.global_config, replace(ctx.project, visibility='public')
            )
        except PubanonError:
            raise private_error from None
    remote = host.ls_remote(destination, ctx.paths, ctx.project.name)
    _local_main, local_tags = _candidate.published_refs(ctx.paths, ctx.project.name)
    remote_commit = remote.tags.get(intended)
    local_commit = local_tags.get(intended)
    if remote_commit is None and local_commit is None:
        if private_error is not None:
            raise private_error from None
        return False
    if remote_commit is not None and remote_commit == local_commit:
        return True
    raise PubanonError(
        'published version state is ambiguous',
        expected=(f'local {escape(intended)}: {escape(local_commit or "absent")}'),
        actual=(f'host {escape(intended)}: {escape(remote_commit or "absent")}'),
        recovery='pubanon repo verify',
    )


def _resolve_run_plan(args: argparse.Namespace, ctx: _context.ProjectContext) -> _RunPlan:
    """Validate and bind one declared workflow invocation."""
    record = _candidate.load_candidate(ctx.paths, ctx.project.name)
    if record is not None and record.state == _candidate.STATE_UNKNOWN_OUTCOME:
        raise PubanonError(
            'snapshot has an unknown publish outcome',
            recovery='pubanon publish --resolve',
        )
    _context.refuse_retained_rewrite(ctx.paths, ctx.project.name, record)
    stages = ctx.project.workflows.get(args.name)
    if stages is None:
        declared = ', '.join(sorted(ctx.project.workflows)) or 'none'
        raise PubanonError(
            f'workflow is not declared: {escape(args.name)}',
            fact=f'declared workflows: {escape(declared)}',
        )
    if 'publish' in stages and args.version is None:
        raise PubanonError('workflow containing publish requires --version')
    intended = _candidate.tag_intent_for(args.version)
    if args.version is None and record is not None and 'publish' not in stages:
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
            recovery='to use the prepared snapshot: pubanon inspect, then pubanon publish; '
            'to replace it: pubanon discard, then pubanon prepare',
        )
    already_published = _published_run_version(ctx, stages, intended)
    publish_index = stages.index('publish') if 'publish' in stages else -1
    remaining = stages[publish_index + 1 :] if already_published else stages
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
        already_published,
        stage_creation_needed,
    )


def _run_mutations(plan: _RunPlan) -> list[str]:
    """Return every external mutation in the remaining declared stages."""
    mutations: list[str] = []
    for stage in plan._remaining_stages:
        if stage == 'stage push':
            if plan._stage_creation_needed:
                mutations.append(
                    'create staging repository '
                    f'{plan._ctx.project.owner}/{_config.stage_repository(plan._ctx.project)} '
                    '(first use)'
                )
            mutations.append('replace the files in the private staging repository')
        elif stage == 'publish':
            mutations.append(
                f'publish to {plan._ctx.project.owner}/{plan._ctx.project.repo} on main'
            )
            if plan._intended is not None:
                mutations.append(f'create release tag {plan._intended}')
        elif stage == 'repo reveal':
            mutations.append('make the publication repository public')
    return mutations


def _run_prepare_stage(args: argparse.Namespace, state: _RunState) -> None:
    """Execute or resume one declared prepare stage."""
    if state._record is None:
        state._record = _prepare.prepare_locked(
            state._ctx,
            _prepare.PrepareOptions(
                source='HEAD',
                version=args.version,
                message=None,
                acknowledge_new_paths=args.acknowledge_new_paths,
                yes=args.yes,
            ),
        )
        state._ctx = _context.load_context(state._ctx.paths, state._ctx.project.name)
        return
    _candidate.verify_retained_candidate(
        state._ctx.paths,
        state._ctx.global_config,
        state._ctx.project,
        state._record,
        env=os.environ,
    )


def _run_publish_stage(args: argparse.Namespace, state: _RunState) -> None:
    """Verify the invocation tag and invoke the publication primitive."""
    if state._record is None:
        raise PubanonError('no prepared snapshot is retained', recovery='pubanon prepare')
    intended = _candidate.tag_intent_for(args.version)
    if state._record.tag_intent != intended:
        raise PubanonError(
            'the prepared snapshot has a different release tag',
            expected=escape(intended or 'none'),
            actual=escape(state._record.tag_intent or 'none'),
            recovery='to use the prepared snapshot: pubanon inspect, then pubanon publish; '
            'to replace it: pubanon discard, then pubanon prepare',
        )
    _publish.publish(
        cli_remote.publish_context(
            state._ctx,
            state._record,
            yes=args.yes,
            unattended=args.yes,
        )
    )
    state._published_completed = True


def _run_reveal_stage(args: argparse.Namespace, state: _RunState) -> None:
    """Invoke reveal without covering its disclosure acknowledgment."""
    if args.yes:
        raise PubanonError(
            '--yes cannot answer the repository disclosure acknowledgment',
            recovery='run pubanon repo reveal interactively',
        )
    _lifecycle.reveal(state._ctx.paths, state._ctx.global_config, state._ctx.project)
    state._ctx = _context.load_context(state._ctx.paths, state._ctx.project.name)


def _run_stage_push_stage(args: argparse.Namespace, plan: _RunPlan, state: _RunState) -> None:
    """Recheck staging visibility and invoke the staging primitive."""
    visible = _stage.staging_repository_visible(
        state._ctx.paths, state._ctx.global_config, state._ctx.project
    )
    if (not visible) != plan._stage_creation_needed:
        raise PubanonError(
            'staging repository visibility changed after preview',
            recovery='rerun the workflow for a current preview',
        )
    _stage.stage_push(state._ctx.paths, state._ctx.global_config, state._ctx.project, yes=args.yes)


def _run_watch_ci_stage(args: argparse.Namespace, state: _RunState, *, staged: bool) -> None:
    """Invoke one exact-target CI watch stage."""
    watch_module = _watch
    watch_module.watch_ci(
        state._ctx.paths,
        state._ctx.global_config,
        state._ctx.project,
        staged=staged,
        version=None if staged else args.version,
        timeout=watch_module.DEFAULT_TIMEOUT,
    )


def _run_watch_package_stage(args: argparse.Namespace, state: _RunState) -> None:
    """Invoke one version-bound package watch stage."""
    watch_module = _watch
    watch_module.watch_package(
        state._ctx.paths,
        state._ctx.global_config,
        state._ctx.project,
        version=args.version,
        timeout=watch_module.DEFAULT_TIMEOUT,
    )


def _show_run_preview(name: str, plan: _RunPlan) -> None:
    """Print one declared workflow's resolved stages and mutations."""
    publish_index = plan._stages.index('publish') if 'publish' in plan._stages else -1
    heading(f'workflow {escape(name)} stages:')
    for index, stage in enumerate(plan._stages):
        if plan._already_published and index <= publish_index:
            print(f'  {stage} (already published)')
        elif stage == 'prepare' and plan._record is not None:
            print('  prepare (resume retained snapshot)')
        else:
            print(f'  {stage}')
    mutations = _run_mutations(plan)
    if mutations:
        heading('Changes to GitHub:', separate=True)
        for mutation in mutations:
            print(f'  {escape(mutation)}')


def _stepwise_commands(
    args: argparse.Namespace,
    state: _RunState,
    stages: tuple[str, ...],
) -> str:
    """Render exact individual recovery commands after publication."""
    commands: list[str] = []
    name = state._ctx.project.name
    for stage in stages:
        if stage == 'watch ci':
            command = _context.project_command(
                name, 'watch', 'ci', f'--project={name}', '--version', args.version
            )
        elif stage == 'repo reveal':
            command = _context.project_command(name, 'repo', 'reveal', f'--project={name}')
        elif stage == 'watch package':
            command = _context.project_command(
                name, 'watch', 'package', f'--project={name}', '--version', args.version
            )
        else:
            command = _context.project_command(name, *stage.split(), f'--project={name}')
        commands.append(f'  {command}')
    return 'finish stepwise:\n' + '\n'.join(commands)


def _watch_version_option(args: argparse.Namespace) -> str:
    """Preserve a workflow's selected release in follow-up observations."""
    return f' --version {shlex.quote(args.version)}' if args.version else ''


def _workflow_candidate_report(
    ctx: _context.ProjectContext, record: _candidate.CandidateRecord
) -> str:
    """Compose an unmasked snapshot publication report."""
    return _candidate.publication_report(ctx.paths, record, ctx.global_config, ctx.project)


def _run_inspect_stage(state: _RunState, *, publishing: bool) -> None:
    """Materialize and report one retained snapshot for workflow inspection."""
    if state._record is None:
        raise PubanonError('no prepared snapshot is retained', recovery='pubanon prepare')
    _inspect.inspection_copy(state._ctx, state._record, workflow=True)
    print('Inspection does not pause this workflow for file review.')
    if not publishing:
        print(_workflow_candidate_report(state._ctx, state._record))


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
        _run_inspect_stage(state, publishing='publish' in plan._remaining_stages)
    elif stage == 'stage push':
        _run_stage_push_stage(args, plan, state)
    elif stage == 'watch ci --staged':
        _run_watch_ci_stage(args, state, staged=True)
    elif stage == 'publish':
        _run_publish_stage(args, state)
    elif stage == 'watch ci':
        _run_watch_ci_stage(args, state, staged=False)
    elif stage == 'repo reveal':
        _run_reveal_stage(args, state)
    elif stage == 'watch package':
        _run_watch_package_stage(args, state)
    else:
        raise PubanonError(f'workflow stage is not implemented: {stage}')


def _execute_run(args: argparse.Namespace, plan: _RunPlan) -> None:
    """Execute every remaining stage with post-publication recovery."""
    state = _RunState(plan._ctx, plan._record, plan._already_published)
    for index, stage in enumerate(plan._remaining_stages):
        try:
            _execute_run_stage(args, plan, state, stage)
        except (PubanonError, KeyboardInterrupt) as error:
            exc = (
                error
                if isinstance(error, PubanonError)
                else OperationCancelled('operation cancelled')
            )
            recovery_index = index
            if not state._published_completed and stage == 'publish':
                if (
                    _candidate.load_candidate(state._ctx.paths, state._ctx.project.name)
                    is not None
                ):
                    raise exc from None
                try:
                    state._published_completed = _published_run_version(
                        state._ctx,
                        plan._stages,
                        plan._intended,
                    )
                except (PubanonError, KeyboardInterrupt):
                    raise exc from None
                if state._published_completed:
                    recovery_index += 1
            if not state._published_completed:
                raise exc from None
            fact = exc.fact
            if exc.recovery is not None:
                stage_recovery = f'stage recovery: {exc.recovery}'
                fact = stage_recovery if fact is None else f'{fact}\n{stage_recovery}'
            completed = 'publication already completed before this stage stopped'
            exc.fact = f'{fact}\n{completed}' if fact else completed
            exc.recovery = _stepwise_commands(args, state, plan._remaining_stages[recovery_index:])
            raise exc from None


def command_release(args: argparse.Namespace) -> None:
    """Compose prepare, inspect summary, and publish primitives."""
    publish_module = _publish
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        ctx = _context.load_context(paths, name)
        intended = _candidate.tag_intent_for(args.version)
        if intended is None:
            raise PubanonError('version must have X.Y.Z form')
        record = _candidate.load_candidate(paths, name)
        if record is not None and record.state == _candidate.STATE_UNKNOWN_OUTCOME:
            raise PubanonError(
                'snapshot has an unknown publish outcome',
                recovery='pubanon publish --resolve',
            )
        _context.refuse_retained_rewrite(paths, name, record)
        if record is not None and record.tag_intent != intended:
            raise PubanonError(
                'the prepared snapshot has a different release tag',
                expected=escape(intended),
                actual=escape(record.tag_intent or 'none'),
                recovery='to use the prepared snapshot: pubanon inspect, then pubanon publish; '
                'to replace it: pubanon discard, then pubanon prepare',
            )
        heading('release stages:')
        print('  prepare snapshot' if record is None else '  resume retained snapshot')
        print('  inspect snapshot summary')
        print('  verify and publish the main branch')
        print(f'  publish release tag {escape(intended)}')
        if record is None:
            options = _prepare.PrepareOptions(
                source='HEAD',
                version=args.version,
                message=None,
                acknowledge_new_paths=False,
                yes=args.yes,
            )
            plan = _prepare.plan_prepare_locked(ctx, options)
            first_release = plan.published[0] is None
            if args.yes and plan.new_paths and not first_release:
                raise PubanonError(
                    '--yes cannot acknowledge new paths',
                    recovery=(
                        f'run pubanon prepare --version {args.version} --acknowledge-new-paths, '
                        f'then pubanon release {args.version} --yes'
                    ),
                )
            if (
                plan.new_paths
                and not first_release
                and not confirm(
                    f'Include these {len(plan.new_paths)} files that were not in the last release?'
                )
            ):
                raise PubanonError('new paths were not acknowledged')
            record = _prepare.build_candidate(plan, options)
            ctx = _context.load_context(paths, name)
        if args.yes:
            output(
                'Automatic continuation: publication will follow the verified snapshot preview.',
                kind='warning',
            )
        _inspect.inspection_copy(ctx, record, workflow=True)
        print('Inspection does not pause this workflow for file review.')
        publish_module.publish(
            cli_remote.publish_context(
                ctx,
                record,
                yes=args.yes,
                unattended=args.yes,
            )
        )


def command_run(args: argparse.Namespace) -> None:
    """Execute one declared workflow while holding the project lock."""
    paths = _config.derive_paths(os.environ)
    name = _context.project_name(paths, args.project)
    with _config.project_lock(paths, name):
        plan = _resolve_run_plan(args, _context.load_context(paths, name))
        _show_run_preview(args.name, plan)
        if plan._already_published and not plan._remaining_stages:
            output(f'version {args.version} is already published', kind='success')
            print('No stage or mutation ran.')
            print()
            return
        if args.yes and plan._stage_creation_needed:
            raise PubanonError(
                '--yes cannot answer first-use staging repository creation',
                recovery='run interactively: pubanon stage push',
            )
        if args.yes:
            output(
                'Automatic continuation: selected stages proceed without ordinary prompts.',
                kind='warning',
            )
        with guidance_context(workflow=True):
            _execute_run(args, plan)
        output(f'Workflow {escape(args.name)} completed.', kind='success')
        if _candidate.load_candidate(paths, name) is not None:
            print('The snapshot is prepared but has not been published.')
            next_step('pubanon inspect')
        elif (
            plan._ctx.project.required_checks
            and 'watch ci' not in plan._stages
            and 'repo reveal' not in plan._stages
        ):
            next_step(f'pubanon watch ci{_watch_version_option(args)}')
        elif (
            plan._ctx.project.package is not None
            and 'watch package' not in plan._stages
            and 'repo reveal' not in plan._stages
        ):
            next_step(f'pubanon watch package{_watch_version_option(args)}')
        elif 'repo reveal' not in plan._stages and plan._ctx.project.visibility == 'private':
            next_step('pubanon repo reveal')
