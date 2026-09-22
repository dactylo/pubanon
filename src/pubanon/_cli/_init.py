"""Guide project initialization and resume saved answers."""

from __future__ import annotations

import csv
import io
import os
import shlex
import tomllib
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pubanon import (
    _config,
    _policy,
)
from pubanon._errors import OperationCancelled, PubanonError
from pubanon._remote import _lifecycle, _workflow_checks
from pubanon._snapshots import _source
from pubanon._terminal import (
    ask,
    confirm,
    diagnostic,
    escape,
    heading,
    next_step,
    output,
    style,
)

if TYPE_CHECKING:
    import argparse


from pubanon._cli import _context, _init_draft, _selection


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
        'Edit:', choices=['destination', 'visibility', 'policy', 'checks', 'settings', 'back']
    )
    steps = {
        'destination': ('owner', 'repo'),
        'visibility': ('visibility',),
        'policy': ('selection',),
        'checks': ('checks',),
        'settings': ('description', 'issues', 'wiki', 'projects'),
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
        raise OperationCancelled('operation cancelled')
    if choice == 'back':
        # Before the group review, Back returns to the preceding filename warning.
        if draft.flagged:
            return replace(draft, flagged=draft.flagged[:-1])
        output('There is no earlier file choice.', kind='note')
        return draft
    return replace(draft, flagged=(*draft.flagged, (remaining[0], choice)))


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


def _load_init_draft(ctx: _context.ProjectContext, commit: str) -> _init_draft.Draft:
    """Offer a saved draft without silently discarding or finalizing it."""
    draft = _init_draft.load(ctx.paths, ctx.project.name)
    if draft is not None:
        heading('Saved setup draft', separate=True)
        print(f'{style("Source:")} {escape(str(draft.source))}')
        same_source = _config.same_source(draft.source, ctx.project.source_path)
        choices = ['resume', 'start over', 'quit'] if same_source else ['start over', 'quit']
        if not same_source:
            print('This draft belongs to another source repository.')
        choice = ask('Saved setup:', choices=choices)
        if choice == 'quit':
            raise OperationCancelled(
                'operation cancelled', fact='existing setup draft was retained'
            )
        if choice == 'start over':
            draft = None
    if draft is None:
        draft = _init_draft.Draft(ctx.project.source_path, commit, ctx.project.self_publication)
    elif draft.commit != commit:
        print(
            'The source changed. Review the updated selection; your earlier choices are retained.'
        )
        pending = ('selection', *(step for step in draft.pending if step != 'selection'))
        draft = replace(draft, commit=commit, phase='flagged', pending=pending)
    if ctx.project.self_publication:
        draft = replace(draft, self_publication=True)
    return _checkpoint_init(ctx, draft)


def _manual_required_checks(default: tuple[str, ...]) -> tuple[str, ...]:
    """Read check names with CSV quoting so commas inside names are preserved."""
    output('Quote names containing commas, for example "tests (linux, 3.13)".', kind='note')
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


def _project_review(
    project: _config.ProjectConfig,
    snapshot: _source.Snapshot,
) -> None:
    """Display the complete project snapshot before its save decision."""
    membership = _policy.evaluate_membership(
        [entry.path for entry in snapshot],
        project.allow,
        project.deny,
    )
    settings = project.settings
    if settings is None:
        raise PubanonError('repository settings are missing')
    checks = _check_names_csv(project.required_checks) if project.required_checks else 'none'
    values = {
        'Source': str(project.source_path),
        'Destination': f'{project.owner}/{project.repo}',
        'Visibility': project.visibility,
        'Selection scope': (
            f'{len(membership.allowed)} included; '
            f'{len(membership.denied) + len(membership.reserved)} excluded'
        ),
        'Required checks': checks,
        'Description': settings.description or '(empty)',
        'Issues': 'yes' if settings.issues else 'no',
        'Wiki': 'yes' if settings.wiki else 'no',
        'Projects': 'yes' if settings.projects else 'no',
    }
    heading('Project configuration to save', separate=True)
    for label, value in values.items():
        print(f'{style(escape(label) + ":")} {escape(value)}')
    output('Publication uses the main branch.')
    if not project.required_checks:
        output('No CI checks configured; CI waiting is optional.', kind='warning')


def _replace_project_context(
    ctx: _context.ProjectContext,
    project: _config.ProjectConfig,
) -> _context.ProjectContext:
    """Return one project context for the replacement values."""
    return _context.ProjectContext(ctx.paths, ctx.global_config, project)


def _project_from_draft(
    ctx: _context.ProjectContext, snapshot: _source.Snapshot, draft: _init_draft.Draft
) -> _context.ProjectContext:
    """Derive a proposed configuration from saved choices and the current tree."""
    paths = [entry.path for entry in snapshot]
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
    ):
        settings = _config.Settings(
            draft.description, 'main', draft.issues, draft.wiki, draft.projects
        )
    project = replace(
        ctx.project,
        allow=tuple(allow),
        deny=tuple(deny),
        secret_exceptions=tuple(exceptions),
        owner=draft.owner if draft.owner is not None else ctx.project.owner,
        repo=draft.repo if draft.repo is not None else ctx.project.repo,
        visibility=draft.visibility or 'private',
        required_checks=draft.required_checks or (),
        settings=settings,
        self_publication=draft.self_publication,
    )
    return _replace_project_context(ctx, project)


def _draft_group_choice(
    ctx: _context.ProjectContext, snapshot: _source.Snapshot, draft: _init_draft.Draft
) -> _init_draft.Draft:
    """Resolve one unmatched group and retain its decision for resume or Back."""
    proposed = _project_from_draft(ctx, snapshot, draft)
    paths = [entry.path for entry in snapshot]
    flagged = set(_policy.secret_like(paths, ()))
    ordinary = [path for path in paths if path not in flagged]
    unmatched = _policy.evaluate_membership(
        ordinary, proposed.project.allow, proposed.project.deny
    ).unmatched
    if not unmatched:
        return replace(draft, phase='preview')
    pattern, admitted = _policy.narrowest_offer(unmatched, guided=True)[0]
    print(style('Files to include or exclude:'))
    for path in admitted:
        print(f'  {escape(path)}')
    print(f'Selection pattern: {escape(pattern)} (also applies to future matching files).')
    choice = ask(
        'Include these files?', choices=['include', 'exclude', 'customize', 'back', 'quit']
    )
    if choice == 'quit':
        raise OperationCancelled('operation cancelled')
    if choice == 'back':
        return _back_init_selection(draft)
    if choice == 'customize':
        pattern = ask('Replacement pattern')
        choice = 'include'
    return replace(draft, groups=(*draft.groups, (pattern, choice)))


def _draft_review(
    ctx: _context.ProjectContext, snapshot: _source.Snapshot, draft: _init_draft.Draft
) -> _init_draft.Draft:
    """Require an explicit final save even after resuming a complete draft."""
    proposed = _project_from_draft(ctx, snapshot, draft)
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
    _project_review(proposed.project, snapshot)
    action = ask('Save this configuration?', choices=['save', 'edit', 'cancel'])
    if action == 'cancel':
        raise OperationCancelled('project initialization cancelled')
    if action == 'edit':
        return _checkpoint_init(ctx, _draft_edit(draft))
    # Keep the on-disk draft at review until configuration installation succeeds.
    return replace(draft, pending=())


def _draft_selection(
    ctx: _context.ProjectContext,
    snapshot: _source.Snapshot,
    draft: _init_draft.Draft,
    *,
    verbose: bool,
) -> _init_draft.Draft:
    """Review a resumable selection and allow correction before continuing."""
    print('Choose the files to publish. Nothing is published now.')
    paths = [entry.path for entry in snapshot]
    heading('Files needing your decision:', separate=True)
    if not _policy.secret_like(paths, ()):
        print('  None.')
    while True:
        if draft.phase == 'flagged':
            draft = _checkpoint_init(ctx, _draft_flagged_choice(paths, draft))
        elif draft.phase == 'groups':
            draft = _checkpoint_init(ctx, _draft_group_choice(ctx, snapshot, draft))
        else:
            _selection.show_selection(
                _project_from_draft(ctx, snapshot, draft), paths, verbose=verbose
            )
            choice = ask('Use this selection?', choices=['continue', 'review', 'back', 'quit'])
            if choice == 'quit':
                raise OperationCancelled('operation cancelled')
            if choice == 'continue':
                return _checkpoint_init(ctx, replace(draft, pending=draft.pending[1:]))
            if choice == 'back':
                draft = _back_init_selection(draft)
            else:
                print('Review each group below. Customize lets you edit its filename pattern.')
                draft = replace(draft, reviewing=True, groups=(), phase='groups')
            draft = _checkpoint_init(ctx, draft)


def _report_existing_project_init(
    project: _config.ProjectConfig,
    project_file: Path,
    *,
    relocated: bool,
) -> None:
    """Report the state changes made by initialization of an existing project."""
    if relocated:
        output(
            f'Relocated project {escape(project.name)} to the current source repository.',
            kind='success',
        )
        print('The project configuration and source registration were updated.')
    else:
        output(
            f'Project {escape(project.name)} is already configured and registered.',
            kind='success',
        )
        print('Only the source registration was refreshed.')
    print()
    print(f'{style("Details:")} project configuration {escape(str(project_file))}')
    next_step('pubanon status')


def _review_relocation(project: _config.ProjectConfig, source: Path, *, yes: bool) -> None:
    """Require distinguishable source paths before approving relocation."""
    heading('Source relocation')
    for label, value in {
        'Old source': str(project.source_path),
        'New source': str(source),
    }.items():
        print(f'{style(escape(label) + ":")} {escape(value)}')
    if yes:
        raise PubanonError('--yes cannot answer a source relocation decision')
    output(
        'Retained snapshots must be prepared again; snapshot history is unchanged.',
        kind='warning',
    )
    if not confirm('Move this project to the new source path?'):
        raise PubanonError('source relocation declined')


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


def _required_checks_prompt(
    *,
    default: tuple[str, ...] = (),
    snapshot: _source.Snapshot = (),
) -> tuple[str, ...]:
    """Offer workflow check names, preserving a manual CSV entry option."""
    discovered = _workflow_checks.discover(snapshot)
    if discovered.notes:
        heading('Checks Pubanon could not list automatically', separate=True)
        for note in discovered.notes:
            output(escape(note), kind='warning')
        output(
            'This affects automatic name discovery, not which jobs GitHub runs. '
            'Enter any missing check names manually.',
            kind='note',
        )
    if not discovered.names:
        return _manual_required_checks(default)
    heading('GitHub checks found in selected workflow files', separate=True)
    print('Choose which results Pubanon waits for. This does not change which jobs GitHub runs.')
    for number, name in enumerate(discovered.names, start=1):
        print(f'  {number}. {escape(name)}')
    choices = ['all listed', 'select', 'none', 'manual']
    if default:
        print(f'\n{style("Current requirements:")} {escape(_check_names_csv(default))}')
        choices.append('keep current')
    while True:
        action = ask('Require checks:', choices=choices)
        if action == 'select':
            selected = _select_check_numbers(discovered.names)
            if selected is None:
                continue
            return selected
        if action == 'manual':
            return _manual_required_checks(default)
        return {'all listed': discovered.names, 'none': (), 'keep current': default}[action]


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
            draft.owner or ctx.global_config.accounts['github']
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
    elif step == 'checks':
        draft = replace(
            draft,
            required_checks=_required_checks_prompt(
                default=draft.required_checks or (),
                snapshot=_selection.sanitized_snapshot(
                    _project_from_draft(ctx, snapshot, draft), snapshot
                ),
            ),
        )
    elif step == 'description':
        draft = replace(draft, description=_draft_description(draft))
        print('Publication uses the main branch.')
    else:
        default = getattr(draft, step)
        enabled = confirm(
            f'Enable {step}?', default=(step == 'issues') if default is None else default
        )
        draft = replace(draft, **{step: enabled})
    return _checkpoint_init(ctx, replace(draft, pending=draft.pending[1:]))


def _initial_project(ctx: _context.ProjectContext, *, verbose: bool) -> _config.ProjectConfig:
    """Resume or collect a project draft without installing it before final save."""
    source = ctx.project.source_path
    _source.check_source_safety(source)
    pinned = _source.pin(source, 'HEAD')
    snapshot = _source.read_snapshot(source, pinned.commit)
    heading('Project source')
    print(f'{style("Source repository root:")} {escape(str(source))}')
    draft = _load_init_draft(ctx, pinned.commit)
    resume = (
        'pubanon init'
        if ctx.project.name == source.name
        else shlex.join(['pubanon', 'init', f'--project={ctx.project.name}'])
    )
    output(f'Setup progress saved. Rerun {escape(resume)} to resume.', kind='note')
    try:
        while draft.pending:
            step = draft.pending[0]
            if step == 'selection':
                draft = _draft_selection(ctx, snapshot, draft, verbose=verbose)
            elif step == 'review':
                draft = _draft_review(ctx, snapshot, draft)
            else:
                draft = _draft_field(ctx, snapshot, draft)
    except (OperationCancelled, PubanonError) as error:
        error.fact = 'project configuration was not saved; setup draft was saved'
        error.recovery = f'rerun {escape(resume)} to resume'
        raise
    return _project_from_draft(ctx, snapshot, draft).project


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
    if args.yes and not project_file.exists():
        raise PubanonError(
            '--yes cannot answer policy proposals',
            recovery=(f'author {escape(str(project_file))} explicitly, then rerun pubanon init'),
        )
    with _config.project_lock(paths, name):
        if project_file.exists():
            project = _config.load_project(paths, name)
            relocated = False
            if not _config.same_source(project.source_path, source):
                if not args.relocate:
                    raise PubanonError(
                        'configured source path differs from the current source',
                        recovery='rerun pubanon init --relocate',
                    )
                _review_relocation(project, source, yes=args.yes)

                _source.check_source_safety(source)
                project = replace(project, source_path=source)
                _config.install_project(paths, project)
                relocated = True
            else:
                _config.registry_set(paths, source, name)
            _report_existing_project_init(project, project_file, relocated=relocated)
            return
        preliminary = _config.ProjectConfig(
            name=name,
            source_path=source,
            host='github',
            owner=global_config.accounts['github'],
            repo=name,
            visibility='private',
            allow=(),
            deny=(),
            secret_exceptions=(),
            extra_terms=(),
            hygiene_terms=(),
            self_publication=args.self_publication,
            required_checks=(),
        )
        project = _initial_project(
            _context.ProjectContext(paths, global_config, preliminary), verbose=args.verbose
        )
        _config.install_project(paths, project)
        _init_draft.remove(paths, name)

    output(f'Configured and registered project {escape(name)}.', kind='success')
    print()
    print(f'{style("Details:")} project configuration {escape(str(project_file))}')
    next_step('pubanon status')
