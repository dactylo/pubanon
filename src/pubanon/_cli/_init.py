"""Guide project initialization and resume saved answers."""

from __future__ import annotations

import csv
import io
import os
import shlex
import sys
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pubanon import (
    _config,
    _policy,
)
from pubanon._config import stage_repository
from pubanon._errors import OperationCanceled, PubanonError
from pubanon._remote import _lifecycle, _workflow_checks
from pubanon._snapshots import _candidate, _source
from pubanon._terminal import (
    ask,
    confirm,
    detail,
    diagnostic,
    display_path,
    escape,
    fields,
    heading,
    next_step,
    output,
    path_text,
    style,
)

if TYPE_CHECKING:
    import argparse


from pubanon._cli import _context, _init_draft, _selection

_RELEASE_PAGE_SUMMARY = {'draft': 'yes, as drafts', 'no': 'no', 'yes': 'yes'}
_UNSELECTED_SHOWN = 5


def _back_init_selection(draft: _init_draft.Draft) -> _init_draft.Draft:
    """Undo the previous selection decision without discarding other setup fields."""
    if draft.groups:
        return replace(draft, groups=draft.groups[:-1], phase='groups')
    if draft.flagged:
        return replace(draft, flagged=draft.flagged[:-1], phase='flagged')
    output('There is no earlier file choice.', kind='note')
    return draft


def _check_names_csv(names: tuple[str, ...]) -> str:
    """Render check names as one unambiguous editable CSV record."""
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator='\n').writerow(names)
    return buffer.getvalue().rstrip('\n')


def _checkpoint_init(ctx: _context.ProjectContext, draft: _init_draft.Draft) -> _init_draft.Draft:
    """Persist accepted answers before advancing to the next question."""
    _init_draft.save(ctx.paths, ctx.project.name, draft)
    return draft


def _choose_saved_draft(
    ctx: _context.ProjectContext, draft: _init_draft.Draft, resume: str
) -> str:
    """Offer only choices valid for a saved draft's source."""
    heading('Saved setup draft', separate=True)
    print(f'{style("Source:")} {escape(path_text(draft.source))}')
    same_source = _config.same_source(draft.source, ctx.project.source_path)
    choices = ['resume', 'start over', 'quit'] if same_source else ['start over', 'quit']
    if not same_source:
        print('This draft belongs to another source repository.')
    try:
        choice = ask('Saved setup:', choices=choices)
    except OperationCanceled:
        choice = 'quit'
    if choice == 'quit':
        if same_source:
            output(f'Setup progress saved. Rerun {escape(resume)} to resume.', kind='note')
            recovery = f'rerun {escape(resume)} to resume'
        else:
            recovery = (
                f'rerun {escape(resume)} from the draft source shown above, '
                'or rerun here and choose start over'
            )
        raise OperationCanceled(
            'Canceled. The setup draft is kept.',
            fact='project configuration was not saved',
            recovery=recovery,
        ) from None
    return choice


def _description_prompt(current: str | None, source: Path | None = None) -> str:
    """Suggest local project metadata only when no description has been saved."""
    label = 'Current description:'
    if current is None and source is not None:
        try:
            document = tomllib.loads((source / 'pyproject.toml').read_text(encoding='utf-8'))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            document = {}
        project = document.get('project')
        if isinstance(project, dict):
            description = cast('dict[str, object]', project).get('description')
            if isinstance(description, str):
                current = description
                label = 'Suggested description (pyproject.toml):'
    if current:
        print(f'{style(label)} {escape(current)}')
        action = ask('Description:', choices=['keep', 'replace', 'clear'], default='keep')
        if action == 'keep':
            return current
        if action == 'clear':
            return ''
    return ask('Repository description (blank for none)', default='')


def _draft_description(draft: _init_draft.Draft) -> str:
    """Edit a saved description or suggest one from local project metadata."""
    return _description_prompt(draft.description, draft.source)


def _draft_edit(draft: _init_draft.Draft) -> _init_draft.Draft:
    """Queue the selected settings section while retaining all existing answers."""
    action = ask(
        'Edit:',
        choices=[
            'policy',
            'destination',
            'visibility',
            'settings',
            'route',
            'checks',
            're[l]ease pages',
            'tags',
            'back',
        ],
    )
    steps = {
        'policy': ('selection',),
        'destination': ('owner', 'repo'),
        'visibility': ('visibility',),
        'settings': ('description', 'issues', 'pull_requests', 'discussions', 'projects', 'wiki'),
        'route': ('publication_route',),
        'checks': ('checks',),
        'release pages': ('release_pages',),
        'tags': ('source_tags',),
        'back': (),
    }[action]
    return replace(draft, pending=(*steps, 'review'), phase='preview')


def _draft_flagged_choice(paths: list[str], draft: _init_draft.Draft) -> _init_draft.Draft:
    """Resolve the next filename warning, with a way back to the previous choice."""
    answered = {path for path, _action in draft.flagged}
    remaining = [path for path in _policy.secret_like(paths, ()) if path not in answered]
    if not remaining:
        return replace(draft, phase='groups' if draft.reviewing else 'preview')
    output(
        'These filenames can contain credentials. '
        'A name match is a warning, not proof of a secret.',
        kind='warning',
    )
    print(f'\n{style("File:")} {escape(remaining[0])}')
    choice = ask('Include it?', choices=['include', 'exclude', 'back', 'quit'])
    if choice == 'quit':
        raise OperationCanceled('Canceled.')
    if choice == 'back':
        # Before the group review, Back returns to the preceding filename warning.
        if draft.flagged:
            return replace(draft, flagged=draft.flagged[:-1])
        output('There is no earlier file choice.', kind='note')
        return draft
    return replace(draft, flagged=(*draft.flagged, (remaining[0], choice)))


def _feature_prompt(step: str, default: bool | None) -> bool:
    """Ask whether one repository feature is enabled, starting from its saved value."""
    if step == 'pull_requests':
        output('pubanon publishes from the private source and does not merge on GitHub.')
    label = step.replace('_', ' ')
    return confirm(f'Enable {label}?', default=(step == 'issues') if default is None else default)


def _host_segment_prompt(field: str, prompt: str, *, default: str) -> str:
    """Read one host coordinate, retrying only its invalid value."""
    while True:
        value = ask(prompt, default=default)
        try:
            return _config.validate_host_segment(field, value)
        except PubanonError as error:
            print(diagnostic(error))


def _init_source(paths: _config.Paths) -> tuple[Path, str | None]:
    """Resolve the canonical source repository and its registration."""
    source = _source.repository_root(Path.cwd())
    return source, _config.registry_lookup(paths, source)


def _manual_required_checks(default: tuple[str, ...]) -> tuple[str, ...]:
    """Read check names with CSV quoting so commas inside names are preserved."""
    output('Quote names containing commas, for example "test (linux, 3.13)".', kind='note')
    while True:
        raw_checks = ask(
            'Required CI checks (comma separated, blank for none)',
            default=_check_names_csv(default),
        )
        try:
            fields = next(csv.reader([raw_checks], skipinitialspace=True, strict=True))
        except csv.Error:
            output(
                'Invalid quoting. Put each name containing commas in double quotes.',
                kind='warning',
            )
            continue
        return tuple(
            dict.fromkeys(
                item.strip()
                for item in fields
                if item.strip() and item.strip().casefold() != 'none'
            )
        )


def _publication_route_prompt(*, default: str) -> str:
    """Read an explicit route while showing the global default."""
    print('Staged: try snapshots privately before final publication.')
    print('Direct: upload straight to the final repository.')
    while True:
        route = ask('Publication route (staged/direct)', default=default).casefold()
        if route in _config.PUBLICATION_ROUTES:
            return route
        output('Choose staged or direct.', kind='warning')


def _recover_invalid_draft(ctx: _context.ProjectContext, resume: str) -> None:
    """Offer to discard an unusable saved draft or retain it on cancellation."""
    heading('Saved setup draft', separate=True)
    print('This saved setup draft cannot be resumed.')
    try:
        choice = ask('Saved setup:', choices=['start over', 'quit'])
    except OperationCanceled:
        choice = 'quit'
    if choice == 'quit':
        raise OperationCanceled(
            'Canceled. The setup draft is kept.',
            fact='project configuration was not saved',
            recovery=f'rerun {escape(resume)} and choose start over',
        ) from None
    _init_draft.remove(ctx.paths, ctx.project.name)


def _read_init_draft(ctx: _context.ProjectContext, resume: str) -> _init_draft.Draft | None:
    """Load a saved draft, offering recovery when its contents are unusable."""
    try:
        return _init_draft.load(ctx.paths, ctx.project.name)
    except _init_draft.InvalidDraftError:
        _recover_invalid_draft(ctx, resume)
        return None


def _load_init_draft(
    ctx: _context.ProjectContext,
    commit: str,
    route: str | None,
    source_tags: bool | None,
    resume: str,
) -> _init_draft.Draft:
    """Offer a saved draft without silently discarding or finalizing it."""
    draft = _read_init_draft(ctx, resume)
    if draft is not None and _choose_saved_draft(ctx, draft, resume) == 'start over':
        draft = None
    if draft is None:
        draft = _init_draft.Draft(
            ctx.project.source_path, commit, ctx.project.self_publication, source_tags=True
        )
    elif draft.commit != commit:
        print(
            'The source changed. Review the updated selection; your earlier choices are retained.'
        )
        pending = ('selection', *(step for step in draft.pending if step != 'selection'))
        draft = replace(draft, commit=commit, phase='flagged', pending=pending)
    if ctx.project.self_publication:
        draft = replace(draft, self_publication=True)
    if route is not None:
        draft = replace(
            draft,
            publication_route=route,
            pending=tuple(step for step in draft.pending if step != 'publication_route'),
        )
    if source_tags is not None:
        draft = replace(
            draft,
            source_tags=source_tags,
            pending=tuple(step for step in draft.pending if step != 'source_tags'),
        )
    return _checkpoint_init(ctx, draft)


def _release_pages_answer(project: _config.ProjectConfig) -> str:
    """Return the init answer that matches a project's saved release page settings."""
    if not _config.release_pages_enabled(project):
        return 'no'
    return 'draft' if _config.draft_release_pages(project) else 'yes'


def _edited_release_pages(project: _config.ProjectConfig, answer: str) -> _config.ProjectConfig:
    """Save a release page answer, keeping the saved table when the answer is unchanged."""
    if answer == _release_pages_answer(project):
        return project
    return replace(
        project,
        release_page=replace(
            project.release_page or _config.ReleasePageSettings(),
            enabled=answer != 'no',
            draft=True if answer == 'draft' else None,
        ),
    )


def _project_review(
    project: _config.ProjectConfig,
    snapshot: _source.Snapshot,
    global_config: _config.GlobalConfig,
) -> None:
    """Display the complete project snapshot before its save decision."""
    membership = _policy.evaluate_membership(
        [entry.path for entry in snapshot],
        project.allow,
        project.deny,
        global_deny=_config.global_deny(global_config),
    )
    settings = project.settings
    if settings is None:
        raise PubanonError('repository settings are missing')
    checks = '\n'.join(escape(name) for name in project.required_checks) or 'none'
    included_count = len(membership.allowed)
    excluded_count = len(membership.denied) + len(membership.reserved)
    values = [
        ('Source', display_path(project.source_path)),
        ('Files', f'{included_count} included, {excluded_count} excluded'),
        (
            'Final repository',
            f'{escape(project.owner)}/{escape(project.repo)} ({escape(project.visibility)})',
        ),
        ('Description', escape(settings.description or '(empty)')),
        ('Features', _lifecycle.feature_summary(settings)),
        ('Route', escape(project.publication_route)),
    ]
    if project.publication_route == 'staged':
        values.append(
            (
                'Staging repository',
                f'{escape(project.owner)}/{escape(stage_repository(project))} (private)',
            )
        )
    values.extend(
        [
            ('CI workflow', escape(project.ci_workflow or 'none')),
            ('Required checks', checks),
            ('Release pages', _RELEASE_PAGE_SUMMARY[_release_pages_answer(project)]),
            ('Source tags', 'yes' if project.source_tags else 'no'),
        ]
    )
    heading('Project configuration to save', separate=True)
    fields(values)
    if (
        project.publication_route == 'staged'
        and not project.required_checks
        and project.ci_workflow is None
    ):
        output('No CI checks configured; CI waiting is optional.', kind='warning')


def _release_pages_prompt(*, route: str, ci_configured: bool, default: str) -> str:
    """Explain when release pages are created, then ask whether and how to create them."""
    after_checks = route == 'staged' and ci_configured
    print('\nRelease pages announce published versions.')
    print('Creating one can start release workflows, such as package uploads.')
    print(
        'Each page is created when you publish, after staging checks pass.'
        if after_checks
        else 'Each page is created right after publishing, before CI runs.'
    )
    print('Answer draft if your release workflow publishes the page after uploading the package.')
    if not after_checks:
        print('Otherwise, to wait for CI, answer no and run pubanon release create later.')
    return ask(
        'Create a release page for each published version?',
        choices=['yes', 'draft', 'no'],
        default=default,
    )


def _replace_project_context(
    ctx: _context.ProjectContext,
    project: _config.ProjectConfig,
) -> _context.ProjectContext:
    """Return one project context for the replacement values."""
    return _context.ProjectContext(ctx.paths, ctx.global_config, project)


def _connect_workflow(
    proposed: _context.ProjectContext, snapshot: _source.Snapshot
) -> _context.ProjectContext:
    """Connect the single connectable workflow of a staged guided-init project."""
    project = proposed.project
    workflow = None
    if project.publication_route == 'staged':
        connectable = _workflow_checks.detect(
            _selection.sanitized_snapshot(proposed, snapshot)
        ).connectable
        if len(connectable) == 1:
            workflow = connectable[0]
    return _replace_project_context(proposed, replace(project, ci_workflow=workflow))


def _project_from_draft(
    ctx: _context.ProjectContext, snapshot: _source.Snapshot, draft: _init_draft.Draft
) -> _context.ProjectContext:
    """Derive a proposed configuration from saved choices and the current tree."""
    paths = _policy.evaluate_membership(
        [entry.path for entry in snapshot],
        ('**',),
        (),
        global_deny=_config.global_deny(ctx.global_config),
    ).allowed
    flagged = set(_policy.secret_like(paths, ()))
    ordinary = [
        path for path in paths if path not in flagged and not _policy.is_reserved_path(path)
    ]
    allow = (
        [pattern for pattern, action in draft.groups if action == 'include']
        if draft.reviewing
        else [pattern for pattern, _paths in _policy.narrowest_offer(ordinary, guided=True)]
    )
    deny = [pattern for pattern, action in draft.groups if action == 'exclude']
    exceptions: list[str] = []
    for path, action in draft.flagged:
        if action == 'include':
            if path not in _policy.evaluate_membership([path], allow, deny).allowed:
                allow.append(_policy.literal_pattern(path))
            exceptions.append(path)
        else:
            deny.append(_policy.literal_pattern(path))
    settings = None
    if (
        draft.description is not None
        and draft.issues is not None
        and draft.wiki is not None
        and draft.projects is not None
        and draft.pull_requests is not None
        and draft.discussions is not None
    ):
        settings = _config.Settings(
            draft.description,
            'main',
            draft.issues,
            draft.wiki,
            draft.projects,
            draft.pull_requests,
            draft.discussions,
        )
    project = replace(
        ctx.project,
        allow=tuple(allow),
        deny=tuple(deny),
        secret_exceptions=tuple(exceptions),
        owner=draft.owner if draft.owner is not None else ctx.project.owner,
        repo=draft.repo if draft.repo is not None else ctx.project.repo,
        visibility=draft.visibility or 'private',
        publication_route=draft.publication_route or ctx.project.publication_route,
        required_checks=draft.required_checks or (),
        source_tags=draft.source_tags,
        release_page=replace(
            ctx.project.release_page or _config.ReleasePageSettings(),
            enabled=draft.release_pages,
            draft=True if draft.release_page_drafts else None,
        ),
        settings=settings,
        self_publication=draft.self_publication,
    )
    return _replace_project_context(ctx, project)


def _draft_group_choice(
    ctx: _context.ProjectContext, snapshot: _source.Snapshot, draft: _init_draft.Draft
) -> _init_draft.Draft:
    """Resolve one unmatched group and retain its decision for resume or Back."""
    proposed = _project_from_draft(ctx, snapshot, draft)
    paths = _policy.evaluate_membership(
        [entry.path for entry in snapshot],
        ('**',),
        (),
        global_deny=_config.global_deny(ctx.global_config),
    ).allowed
    flagged = set(_policy.secret_like(paths, ()))
    ordinary = [path for path in paths if path not in flagged]
    membership = _policy.evaluate_membership(
        ordinary,
        proposed.project.allow,
        proposed.project.deny,
        global_deny=_config.global_deny(ctx.global_config),
    )
    if not membership.unmatched:
        return replace(draft, phase='preview')
    pattern, admitted = _policy.narrowest_offer(membership.unmatched, guided=True)[0]
    action, pattern = _selection.policy_decision(pattern, admitted, membership, back=True)
    if action == 'back':
        return _back_init_selection(draft)
    choice = 'include' if action == 'publish' else 'exclude'
    return replace(draft, groups=(*draft.groups, (pattern, choice)))


def _draft_review(
    ctx: _context.ProjectContext, snapshot: _source.Snapshot, draft: _init_draft.Draft
) -> _init_draft.Draft:
    """Require an explicit final save even after resuming a complete draft."""
    if draft.publication_route is None:
        raise PubanonError('choose a publication route before saving')
    proposed = _connect_workflow(_project_from_draft(ctx, snapshot, draft), snapshot)
    try:
        _config.gate_host_coordinates(
            ctx.paths,
            ctx.global_config,
            proposed.project,
            coordinates=(proposed.project.owner, proposed.project.repo),
        )
        _lifecycle.gate_host_fields(ctx.paths, ctx.global_config, proposed.project)
    except PubanonError as error:
        print(diagnostic(error))
        output('Edit the field named in the error before saving.', kind='warning')
        return _checkpoint_init(ctx, _draft_edit(draft))
    _project_review(proposed.project, snapshot, ctx.global_config)
    action = ask('Save this configuration?', choices=['save', 'edit', 'cancel'])
    if action == 'cancel':
        raise OperationCanceled('Canceled. Nothing was saved.')
    if action == 'edit':
        return _checkpoint_init(ctx, _draft_edit(draft))
    # Keep the on-disk draft at review until configuration installation succeeds.
    return replace(draft, pending=())


def _draft_selection(
    ctx: _context.ProjectContext,
    snapshot: _source.Snapshot,
    draft: _init_draft.Draft,
) -> _init_draft.Draft:
    """Review a resumable selection and allow correction before continuing."""
    print('Choose the files to publish. Nothing is published now.')
    paths = [entry.path for entry in snapshot]
    decision_paths = _policy.evaluate_membership(
        paths, ('**',), (), global_deny=_config.global_deny(ctx.global_config)
    ).allowed
    heading('Files needing your decision:', separate=True)
    if not _policy.secret_like(decision_paths, ()):
        print('  None.')
    while True:
        if draft.phase == 'flagged':
            draft = _checkpoint_init(ctx, _draft_flagged_choice(decision_paths, draft))
        elif draft.phase == 'groups':
            draft = _checkpoint_init(ctx, _draft_group_choice(ctx, snapshot, draft))
        else:
            _selection.show_selection(_project_from_draft(ctx, snapshot, draft), paths)
            choice = ask('Use this selection?', choices=['continue', 'review', 'back', 'quit'])
            if choice == 'quit':
                raise OperationCanceled('Canceled.')
            if choice == 'continue':
                return _checkpoint_init(ctx, replace(draft, pending=draft.pending[1:]))
            if choice == 'back':
                draft = _back_init_selection(draft)
            else:
                print('Review each group below. Customize takes a pattern to include or exclude.')
                draft = replace(draft, reviewing=True, groups=(), phase='groups')
            draft = _checkpoint_init(ctx, draft)


def _report_existing_project_init(
    paths: _config.Paths,
    project: _config.ProjectConfig,
    *,
    relocated: bool,
    project_changed: bool,
    self_publication_enabled: bool,
) -> None:
    """Report the state changes made by initialization of an existing project."""
    heading(f'Project {escape(project.name)}')
    fields(
        [
            ('Route', escape(project.publication_route)),
            ('Release pages', _RELEASE_PAGE_SUMMARY[_release_pages_answer(project)]),
            ('Source tags', 'yes' if project.source_tags else 'no'),
        ]
    )
    project_file = paths.config_dir / 'projects' / f'{project.name}.toml'
    detail(f'Project configuration: {escape(path_text(project_file))}')
    if relocated:
        output(
            f'Relocated project {escape(project.name)} to the current source repository.',
            kind='success',
        )
    else:
        output(
            f'Updated project {escape(project.name)} and its source registration.'
            if project_changed
            else f'Refreshed source registration for project {escape(project.name)}.',
            kind='success',
        )
    if self_publication_enabled:
        output('Enabled self_publication.', kind='success')
    print('To change settings, run `pubanon init --edit`.')
    try:
        known_existing = _candidate.published_refs(paths, project.name)[0] is not None
    except PubanonError:
        known_existing = False
    next_step('pubanon prepare' if known_existing else 'pubanon repo create')


def _report_unselected_paths(paths: list[str]) -> None:
    """Explain how to select new files before editing the CI workflow and checks."""
    output(
        'Checks cannot be edited yet: these committed files are not in the saved selection.',
        kind='warning',
    )
    for path in paths[:_UNSELECTED_SHOWN]:
        print(f'  {escape(path)}')
    if len(paths) > _UNSELECTED_SHOWN:
        print(f'  and {len(paths) - _UNSELECTED_SHOWN} more')
    output(
        'Save or cancel this session first. Then run pubanon prepare, after pubanon discard '
        'if a snapshot is prepared, choose which new files to publish, and rerun '
        'pubanon init --edit.',
        kind='note',
    )


def _review_relocation(project: _config.ProjectConfig, source: Path, *, yes: bool) -> None:
    """Require distinguishable source paths before approving relocation."""
    heading('Source relocation')
    for label, value in {
        'Old source': path_text(project.source_path),
        'New source': path_text(source),
    }.items():
        print(f'{style(escape(label) + ":")} {escape(value)}')
    if yes:
        raise PubanonError('--yes cannot answer a source relocation decision')
    output(
        'Retained snapshots must be prepared again; snapshot history is unchanged.',
        kind='warning',
    )
    if not confirm('Move this project to the new source path?'):
        raise OperationCanceled('Canceled. Nothing was changed.')


def _select_check_numbers(names: tuple[str, ...]) -> tuple[str, ...] | None:
    """Select numbered checks, or return None to go back to the check menu."""
    while True:
        selected = ask('Check numbers (spaces or commas, for example 1 3; back to return)')
        if selected.strip().casefold() == 'back':
            return None
        try:
            indexes = [int(value) for value in selected.replace(',', ' ').split()]
        except ValueError:
            indexes = []
        if indexes and all(1 <= index <= len(names) for index in indexes):
            return tuple(dict.fromkeys(names[index - 1] for index in indexes))
        output(f'Choose numbers from 1 to {len(names)}.', kind='warning')


def _select_workflow_number(names: tuple[str, ...]) -> str:
    """Read the number of one listed workflow."""
    while True:
        answer = ask('Workflow number')
        try:
            index = int(answer)
        except ValueError:
            index = 0
        if 1 <= index <= len(names):
            return names[index - 1]
        output(f'Choose a number from 1 to {len(names)}.', kind='warning')


def _ci_workflow_prompt(
    project: _config.ProjectConfig, detection: _workflow_checks.WorkflowDetection
) -> str | None:
    """Choose the connected CI workflow, starting from the saved value."""
    current = project.ci_workflow
    connectable = detection.connectable
    if current is None and not connectable:
        return None
    heading('CI workflow', separate=True)
    print(
        'pubanon requests runs of the connected workflow from stage push '
        'and judges its push runs after publishing.'
    )
    print(f'{style("Current:")} {escape(current or "none")}')
    if current is not None and current not in connectable:
        reason = detection.problems.get(current, 'is not in the selected files')
        output(f'{escape(current)} {escape(reason)}.', kind='warning')
    for number, name in enumerate(connectable, start=1):
        print(f'  {number}. {escape(name)}')
    choices = ['keep'] if current is not None else []
    if connectable:
        choices.append('connect')
    choices.append('none')
    if current is not None:
        suggested = 'keep'
    elif project.publication_route == 'staged' and len(connectable) == 1:
        suggested = 'connect'
    else:
        suggested = 'none'
    action = ask('CI workflow:', choices=choices, default=suggested)
    if action == 'keep':
        return current
    if action == 'none':
        return None
    return connectable[0] if len(connectable) == 1 else _select_workflow_number(connectable)


def _settings_prompt(current: _config.Settings) -> _config.Settings:
    """Edit the repository description and features, starting from saved values."""
    return replace(
        current,
        description=_description_prompt(current.description),
        issues=_feature_prompt('issues', current.issues),
        pull_requests=_feature_prompt('pull_requests', current.pull_requests),
        discussions=_feature_prompt('discussions', current.discussions),
        projects=_feature_prompt('projects', current.projects),
        wiki=_feature_prompt('wiki', current.wiki),
    )


def _show_discovered_checks(
    discovered: _workflow_checks.Discovery, *, connected: str | None
) -> None:
    """List check names and explain what requiring a flagged check means."""
    source = escape(connected) if connected is not None else 'selected workflow files'
    heading(f'GitHub checks found in {source}', separate=True)
    print('Choose which results pubanon waits for. This does not change which jobs GitHub runs.')
    for number, name in enumerate(discovered.names, start=1):
        print(f'  {number}. {escape(name)}')
        for caution in discovered.cautions:
            if caution.name == name:
                print(f'     {escape(caution.path)}: {escape("; ".join(caution.reasons))}')
    if not discovered.cautions:
        return
    if connected is not None:
        print('\npubanon accepts a run only if it executed every required job.')
        print('A quick run that skips a hosted-only job cannot satisfy it; a hosted run can.')
    else:
        print('\nA required check that never runs for a push cannot pass.')
        print('Leave out jobs that run only for tags, releases, or schedules.')


def _required_checks_prompt(
    *,
    default: tuple[str, ...] = (),
    snapshot: _source.Snapshot = (),
    connected: str | None = None,
) -> tuple[str, ...]:
    """Offer workflow check names, limited to a connected workflow's jobs."""
    discovered = (
        _workflow_checks.discover(snapshot)
        if connected is None
        else _workflow_checks.discover_workflow(snapshot, connected)
    )
    if discovered.notes:
        heading('Checks pubanon could not list automatically', separate=True)
        for note in discovered.notes:
            output(escape(note), kind='warning')
        output(
            'This affects automatic name discovery, not which jobs GitHub runs. '
            'Enter any missing check names manually.',
            kind='note',
        )
    if not discovered.names:
        return _manual_required_checks(default)
    _show_discovered_checks(discovered, connected=connected)
    choices = ['all listed', 'select', 'none', 'manual']
    suggested = None
    if default:
        print(f'\n{style("Current requirements:")} {escape(_check_names_csv(default))}')
        choices.append('keep current')
        suggested = 'keep current'
    elif not discovered.notes and (connected is not None or not discovered.cautions):
        suggested = 'all listed'
    while True:
        action = ask('Require checks:', choices=choices, default=suggested)
        if action == 'select':
            selected = _select_check_numbers(discovered.names)
            if selected is None:
                continue
            return selected
        if action == 'manual':
            return _manual_required_checks(default)
        return {'all listed': discovered.names, 'none': (), 'keep current': default}[action]


def _show_workflow_connection(project: _config.ProjectConfig, snapshot: _source.Snapshot) -> None:
    """Explain the connected CI workflow, or why guided init connected none."""
    if project.ci_workflow is not None:
        heading('CI workflow', separate=True)
        print(
            f'pubanon will request runs of {escape(project.ci_workflow)}: it declares the '
            'hosted and full_tests inputs and runs on every branch push.'
        )
        return
    if project.publication_route != 'staged':
        return
    detection = _workflow_checks.detect(snapshot)
    if len(detection.connectable) > 1:
        names = ', '.join(escape(name) for name in detection.connectable)
        output(
            f'Several workflows can be connected: {names}. Choose one with pubanon init --edit.',
            kind='note',
        )
        return
    for note in detection.notes:
        output(escape(note), kind='note')
    if detection.notes:
        output(
            'To let pubanon request CI runs, start from the starter workflow described '
            'under "Request CI from stage push" in the pubanon guide.',
            kind='note',
        )


def _source_tags_prompt(*, default: bool) -> bool:
    """Ask whether to mark the source commit of each publication with a local tag."""
    return confirm('Create local tags marking the source of each publication?', default=default)


def _unselected_paths(ctx: _context.ProjectContext, snapshot: _source.Snapshot) -> list[str]:
    """Return committed paths that the saved selection neither includes nor excludes."""
    return _policy.evaluate_membership(
        [entry.path for entry in snapshot],
        ctx.project.allow,
        ctx.project.deny,
        global_deny=_config.global_deny(ctx.global_config),
    ).unmatched


def _edit_checks_section(
    ctx: _context.ProjectContext,
    snapshot: _source.Snapshot,
    project: _config.ProjectConfig,
) -> _config.ProjectConfig:
    """Edit a connected workflow and its required checks when selection is complete."""
    current = _replace_project_context(ctx, project)
    if unselected := _unselected_paths(current, snapshot):
        _report_unselected_paths(unselected)
        return project
    selected = _selection.sanitized_snapshot(current, snapshot)
    workflow = _ci_workflow_prompt(project, _workflow_checks.detect(selected))
    if workflow != project.ci_workflow:
        output('Changing the CI workflow invalidates a prepared snapshot.', kind='warning')
    checks = _required_checks_prompt(
        default=project.required_checks, snapshot=selected, connected=workflow
    )
    if checks != project.required_checks:
        output('Changing required checks invalidates a prepared snapshot.', kind='warning')
    return replace(project, ci_workflow=workflow, required_checks=checks)


def _visibility_prompt(*, default: str) -> str:
    """Read a supported visibility without discarding prior answers."""
    while True:
        visibility = ask('Repository visibility (private/public)', default=default).casefold()
        if visibility in _config.VISIBILITIES:
            return visibility
        output('Expected visibility must be private or public.', kind='warning')


def _draft_field(
    ctx: _context.ProjectContext, snapshot: _source.Snapshot, draft: _init_draft.Draft
) -> _init_draft.Draft:
    """Read one settings field and advance only after storing its accepted answer."""
    step = draft.pending[0]
    if step in {'owner', 'repo'}:
        default = (
            draft.owner or ctx.global_config.accounts[ctx.project.host]
            if step == 'owner'
            else draft.repo or ctx.project.name
        )
        label = 'GitHub owner' if step == 'owner' else 'GitHub repository'
        value = _host_segment_prompt(step, label, default=default)
        draft = replace(draft, **{step: value})
    elif step == 'visibility':
        draft = replace(
            draft, visibility=_visibility_prompt(default=draft.visibility or 'private')
        )
    elif step == 'publication_route':
        draft = replace(
            draft,
            publication_route=_publication_route_prompt(
                default=draft.publication_route or ctx.global_config.default_publication_route
            ),
        )
    elif step == 'checks':
        proposed = _connect_workflow(_project_from_draft(ctx, snapshot, draft), snapshot)
        selected = _selection.sanitized_snapshot(proposed, snapshot)
        _show_workflow_connection(proposed.project, selected)
        draft = replace(
            draft,
            required_checks=_required_checks_prompt(
                default=draft.required_checks or (),
                snapshot=selected,
                connected=proposed.project.ci_workflow,
            ),
        )
    elif step == 'description':
        draft = replace(draft, description=_draft_description(draft))
    elif step == 'source_tags':
        draft = replace(
            draft,
            source_tags=_source_tags_prompt(default=draft.source_tags),
        )
    elif step == 'release_pages':
        proposed = _connect_workflow(_project_from_draft(ctx, snapshot, draft), snapshot)
        answer = _release_pages_prompt(
            route=proposed.project.publication_route,
            ci_configured=bool(proposed.project.required_checks or proposed.project.ci_workflow),
            default=_release_pages_answer(proposed.project),
        )
        draft = replace(draft, release_pages=answer != 'no', release_page_drafts=answer == 'draft')
    else:
        draft = replace(draft, **{step: _feature_prompt(step, getattr(draft, step))})
    return _checkpoint_init(ctx, replace(draft, pending=draft.pending[1:]))


def _edit_project_section(
    ctx: _context.ProjectContext,
    snapshot: _source.Snapshot,
    project: _config.ProjectConfig,
) -> _config.ProjectConfig:
    """Change one selected settings section while keeping every other saved value."""
    action = ask(
        'Edit:',
        choices=['visibility', 'settings', 'route', 'checks', 're[l]ease pages', 'tags', 'back'],
    )
    if action == 'visibility':
        visibility = _visibility_prompt(default=project.visibility)
        if visibility != project.visibility:
            output(
                'Changing the expected visibility invalidates a prepared snapshot.',
                kind='warning',
            )
        project = replace(project, visibility=visibility)
    elif action == 'route':
        project = replace(
            project,
            publication_route=_publication_route_prompt(default=project.publication_route),
        )
    elif action == 'tags':
        project = replace(project, source_tags=_source_tags_prompt(default=project.source_tags))
    elif action == 'release pages':
        answer = _release_pages_prompt(
            route=project.publication_route,
            ci_configured=bool(project.required_checks or project.ci_workflow),
            default=_release_pages_answer(project),
        )
        project = _edited_release_pages(project, answer)
    elif action == 'checks':
        project = _edit_checks_section(ctx, snapshot, project)
    elif action == 'settings':
        if project.settings is None:
            raise PubanonError('repository settings are missing')
        project = replace(project, settings=_settings_prompt(project.settings))
    return project


def _edit_existing_project(ctx: _context.ProjectContext) -> None:
    """Review, change, and save the settings of an existing project."""
    if not sys.stdin.isatty():
        raise PubanonError(
            'init --edit requires an interactive terminal',
            recovery='rerun pubanon init --edit in an interactive terminal',
        )
    source = ctx.project.source_path
    _source.check_source_safety(source)
    snapshot = _source.read_snapshot(source, _source.pin(source, 'HEAD').commit)
    original = ctx.project
    project = original
    project_file = ctx.paths.config_dir / 'projects' / f'{project.name}.toml'
    output('To change the selected files, run pubanon prepare and choose review.', kind='note')
    while True:
        _project_review(project, snapshot, ctx.global_config)
        action = ask('Save this configuration?', choices=['save', 'edit', 'cancel'])
        if action == 'cancel':
            raise OperationCanceled('Canceled. Nothing was saved.')
        if action == 'save':
            if project == original:
                output('No changes to save.', kind='note')
                return
            try:
                _lifecycle.gate_host_fields(ctx.paths, ctx.global_config, project)
            except PubanonError as error:
                print(diagnostic(error))
                output('Edit the field named in the error before saving.', kind='warning')
                output(
                    'For fields not listed in Edit, cancel and edit the project file: '
                    f'{escape(path_text(project_file))}',
                    kind='warning',
                )
                continue
            break
        project = _edit_project_section(ctx, snapshot, project)
    _config.save_project(ctx.paths, project)
    output(f'Saved project {escape(project.name)}.', kind='success')
    detail(f'Project configuration: {escape(path_text(project_file))}')


def _initial_project(
    ctx: _context.ProjectContext,
    *,
    route: str | None,
    source_tags: bool | None,
) -> _config.ProjectConfig:
    """Resume or collect a project draft without installing it before final save."""
    source = ctx.project.source_path
    _source.check_source_safety(source)
    pinned = _source.pin(source, 'HEAD')
    snapshot = _source.read_snapshot(source, pinned.commit)
    heading('Project source')
    print(f'{style("Source repository root:")} {escape(path_text(source))}')
    resume = (
        'pubanon init'
        if ctx.project.name == source.name
        else shlex.join(['pubanon', 'init', f'--project={ctx.project.name}'])
    )
    draft = _load_init_draft(ctx, pinned.commit, route, source_tags, resume)
    try:
        while draft.pending:
            step = draft.pending[0]
            if step == 'selection':
                draft = _draft_selection(ctx, snapshot, draft)
            elif step == 'review':
                draft = _draft_review(ctx, snapshot, draft)
            else:
                draft = _draft_field(ctx, snapshot, draft)
    except (OperationCanceled, PubanonError) as error:
        if isinstance(error, OperationCanceled):
            output(f'Setup progress saved. Rerun {escape(resume)} to resume.', kind='note')
        error.fact = 'project configuration was not saved; setup draft was saved'
        error.recovery = f'rerun {escape(resume)} to resume'
        raise
    return _connect_workflow(_project_from_draft(ctx, snapshot, draft), snapshot).project


def command_init(args: argparse.Namespace) -> None:
    """Initialize, rebuild, or relocate one project registration."""
    paths = _config.derive_paths(os.environ)
    global_config = _config.load_global(paths)
    source, registered = _init_source(paths)
    if args.project is not None and registered is not None and args.project != registered:
        raise PubanonError(
            'source is already registered to another project',
            fact=f'registered project: {escape(registered)}',
        )
    name = args.project or registered or source.name
    _config.require_project_name(name)
    project_file = paths.config_dir / 'projects' / f'{name}.toml'
    if args.edit and not project_file.exists():
        raise PubanonError('no saved project to edit', recovery='pubanon init')
    if args.yes and not project_file.exists():
        raise PubanonError(
            '--yes cannot answer policy proposals',
            recovery=(
                f'author {escape(path_text(project_file))} explicitly, then rerun pubanon init'
            ),
        )
    with _config.project_lock(paths, name):
        if project_file.exists():
            project = _config.load_project(paths, name)
            if args.edit:
                if not _config.same_source(project.source_path, source):
                    raise PubanonError(
                        'configured source path differs from the current source',
                        recovery='rerun pubanon init --relocate, then pubanon init --edit',
                    )
                _edit_existing_project(_context.ProjectContext(paths, global_config, project))
                return
            relocated = False
            route_changed = args.publication_route is not None and (
                args.publication_route != project.publication_route
            )
            source_tags_changed = (
                args.source_tags is not None and args.source_tags != project.source_tags
            )
            self_publication_changed = args.self_publication and not project.self_publication
            if not _config.same_source(project.source_path, source):
                if not args.relocate:
                    raise PubanonError(
                        'configured source path differs from the current source',
                        recovery='rerun pubanon init --relocate',
                    )
                _review_relocation(project, source, yes=args.yes)

                _source.check_source_safety(source)
                project = replace(
                    project,
                    source_path=source,
                    publication_route=args.publication_route or project.publication_route,
                    source_tags=(
                        project.source_tags if args.source_tags is None else args.source_tags
                    ),
                    self_publication=project.self_publication or self_publication_changed,
                )
                _config.install_project(paths, project)
                relocated = True
            else:
                if route_changed or source_tags_changed or self_publication_changed:
                    project = replace(
                        project,
                        publication_route=args.publication_route or project.publication_route,
                        source_tags=(
                            project.source_tags if args.source_tags is None else args.source_tags
                        ),
                        self_publication=project.self_publication or self_publication_changed,
                    )
                    _config.save_project(paths, project)
                _config.registry_set(paths, source, name)
            _report_existing_project_init(
                paths,
                project,
                relocated=relocated,
                project_changed=route_changed or source_tags_changed or self_publication_changed,
                self_publication_enabled=self_publication_changed,
            )
            return
        preliminary = _config.ProjectConfig(
            name=name,
            source_path=source,
            host=_config.DEFAULT_HOST,
            owner=global_config.accounts[_config.DEFAULT_HOST],
            repo=name,
            visibility='private',
            allow=(),
            deny=(),
            secret_exceptions=(),
            extra_terms=(),
            hygiene_terms=(),
            self_publication=args.self_publication,
            required_checks=(),
            publication_route=global_config.default_publication_route,
        )
        project = _initial_project(
            _context.ProjectContext(paths, global_config, preliminary),
            route=args.publication_route,
            source_tags=args.source_tags,
        )
        _config.install_project(paths, project)
        _init_draft.remove(paths, name)

    output(f'Saved project {escape(name)}.', kind='success')
    detail(f'Project configuration: {escape(path_text(project_file))}')
    next_step('pubanon repo create')
