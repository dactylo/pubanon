"""Gate release metadata and verify existing publication tags around writes."""

import os
import string
import time
from collections.abc import Collection, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from pubanon import _config, _gate
from pubanon._errors import ObservationTimeoutError, OperationCanceled, PubanonError
from pubanon._remote import _host
from pubanon._snapshots import _candidate, _records, _store
from pubanon._terminal import display_path, escape, field, output, project_fallback, shell_command

_DRAFT_GUIDANCE = (
    'It appears when your release workflow publishes it after the package upload. '
    'If it is still a draft after that workflow finishes, rerun the workflow or publish the '
    'page on GitHub.'
)
_EXCERPT_LIMIT = 120
_PAIR_SIZE = 2
_VERIFY_FIRST_WAIT = 0.5
_VERIFY_SECONDS = 10.0
_monotonic = time.monotonic
_sleep = time.sleep


@dataclass(frozen=True)
class _PublishedTag:
    """Pair the selected publication tag with its recorded commit."""

    tag: str
    commit: str


@dataclass(frozen=True)
class _PlannedPage:
    """Hold rendered page content and its source for a publication preview."""

    title: str
    notes: bytes
    notes_file: Path | None
    changelog_missing: bool


@dataclass(frozen=True)
class ReleaseChange:
    """Retain the exact gated release change for confirmation and execution.

    Instances returned by `prepare_change` are applied under the same project
    lock and configuration. Notes retain the checked UTF-8 bytes.

    Attributes:
        operation: Whether to create, edit, or reuse a matching page.
        destination: Verified configured final repository.
        tag: Existing strict publication tag.
        commit: Locally published commit matched by the host tag.
        title: Complete resulting title.
        notes: Complete resulting UTF-8 notes.
        previous: Previewed page for edit or a matching existing page;
            `None` for a new creation.
        notes_file: Supplied notes path retained for creation recovery.
        changelog_missing: Whether default notes lack a changelog link.
        title_override: Explicit supplied title retained for creation recovery.
        draft: Whether a creation requests a draft page. A matching published
            page then also counts as the same page, because the release
            workflow may already have published it.
    """

    operation: Literal['create', 'edit', 'existing']
    destination: _host.Destination
    tag: str
    commit: str
    title: str
    notes: bytes
    previous: _host.GitHubRelease | None
    notes_file: Path | None = None
    changelog_missing: bool = False
    title_override: str | None = None
    draft: bool = False


@dataclass(frozen=True)
class PageOutcome:
    """Describe completed page work after verified Git publication.

    Attributes:
        kind: No page work, a new creation, a matching page that appeared, or
            an existing page retained without modification.
        url: Verified hosted page URL, or None when no page work was planned.
        draft: Whether the page read back from GitHub is still a draft.
    """

    kind: Literal['none', 'created', 'existing', 'kept']
    url: str | None
    draft: bool = False


@dataclass(frozen=True)
class PagePlan:
    """Retain the page action approved alongside a publication.

    Attributes:
        action: No page work, retain an existing page, or create a page.
        reason: Why this action was selected.
        tag: Unique publication tag, when one is available.
        candidates: Ambiguous strict version tags at the correction commit.
        existing: Existing page retained without modification.
        change: Retained request content for creation or a matching keep;
            its notes path remains available for recovery.
        project: Registered project for qualified recovery commands.
        waiting: Lower strict version tags whose pages are still drafts, in
            version order; only creation plans list them.
    """

    action: Literal['none', 'keep', 'create']
    reason: Literal['untagged', 'ambiguous', 'skipped', 'disabled', 'exists', 'matches', 'new']
    tag: str | None = None
    candidates: tuple[str, ...] = ()
    existing: _host.GitHubRelease | None = None
    change: ReleaseChange | None = None
    project: str = ''
    waiting: tuple[str, ...] = ()


@dataclass(frozen=True)
class PageRequest:
    """Hold invocation overrides for publication page planning.

    Attributes:
        skip: Suppress all page work, including lookup.
        notes: Exact UTF-8 notes bytes; None uses configured defaults.
        notes_file: Source path for supplied bytes, retained for recovery.
        requested: Create a page even when release pages are off for the
            project.
    """

    skip: bool = False
    notes: bytes | None = None
    notes_file: Path | None = None
    requested: bool = False


class ReleaseOutcomeUnknownError(PubanonError):
    """Report an attempted page write whose result could not be verified.

    Diagnostic fields and recovery follow `PubanonError`. Callers must not
    automatically repeat the write.
    """


class _PageConflictError(PubanonError):
    """Identify a different existing page whose fix is to inspect or edit it."""


class _ReleaseMismatchError(PubanonError):
    """Identify an already escaped read-back difference report."""


@dataclass(frozen=True)
class ReleaseRequest:
    """Group the requested operation and supplied release metadata.

    Attributes:
        operation: Explicit create or edit, never an upsert.
        tag: Existing strict `vX.Y.Z` publication tag.
        title: Nonempty supplied title, or `None` to retain an edit title or
            default a new page's title to its tag.
        notes: Supplied UTF-8 bytes, or `None` for creation defaults or to
            retain edit notes; empty bytes explicitly clear them.
        notes_file: Source path for supplied notes bytes, when applicable.
    """

    operation: Literal['create', 'edit']
    tag: str
    title: str | None
    notes: bytes | None
    notes_file: Path | None = None


@dataclass(frozen=True)
class ReleaseResult:
    """Return an independently verified page and whether a write was attempted.

    Attributes:
        page: Exact matching release page observed on the host.
        written: False when a matching page already satisfied creation.
    """

    page: _host.GitHubRelease
    written: bool


def _command(project: str, *args: str) -> str:
    """Render project-qualified recovery without unsafe terminal characters."""
    command = shell_command('pubanon', *args, f'--project={project}')
    if command is not None:
        return command
    return project_fallback(project, ' '.join(args), None)


def _differences(page: _host.GitHubRelease | None, change: ReleaseChange) -> tuple[str, ...]:
    """Name each requested field that differs; a requested draft may already be published."""
    if page is None:
        return ('page',)
    previous = change.previous
    expected = (
        change.tag,
        change.title,
        change.notes,
        previous.draft if previous is not None else False,
        previous.prerelease if previous is not None else False,
    )
    actual = (page.tag, page.title, page.notes.encode(), page.draft, page.prerelease)
    names = ('tag', 'title', 'notes', 'draft', 'prerelease')
    either_status = previous is None and change.draft
    return tuple(
        name
        for name, want, got in zip(names, expected, actual, strict=True)
        if want != got and not (name == 'draft' and either_status)
    )


def _excerpt(value: object) -> str:
    """Bound an escaped diagnostic excerpt to at most 120 characters."""
    rendered = escape(str(value))
    return rendered if len(rendered) <= _EXCERPT_LIMIT else rendered[: _EXCERPT_LIMIT - 3] + '...'


def _difference_report(page: _host.GitHubRelease | None, change: ReleaseChange) -> str:
    """Describe each mismatched field, locating the first differing notes line."""
    if page is None:
        return 'page: expected present; observed absent'
    lines: list[str] = []
    for name in _differences(page, change):
        if name in {'draft', 'prerelease'}:
            expected = getattr(change.previous, name) if change.previous is not None else False
        else:
            expected = getattr(change, name)
        actual = getattr(page, name)
        label = name
        if name == 'notes':
            wanted = change.notes.decode().split('\n')
            observed = page.notes.split('\n')
            index = next(
                i
                for i in range(max(len(wanted), len(observed)))
                if (wanted[i] if i < len(wanted) else None)
                != (observed[i] if i < len(observed) else None)
            )
            label = f'notes line {index + 1}'
            expected = wanted[index] if index < len(wanted) else '<end of notes>'
            actual = observed[index] if index < len(observed) else '<end of notes>'
        lines.append(f"{label}: expected '{_excerpt(expected)}'; observed '{_excerpt(actual)}'")
    return '\n'.join(lines)


def _conflict_error(
    p: _config.ProjectConfig, page: _host.GitHubRelease, change: ReleaseChange
) -> _PageConflictError:
    """Describe a different existing page and point to inspection or editing."""
    return _PageConflictError(
        'release page already exists with different content or status',
        fact=_difference_report(page, change),
        recovery=(
            _command(p.name, 'release', 'show', change.tag)
            + '\n'
            + _command(p.name, 'release', 'edit', change.tag)
        ),
    )


def _not_yet_visible(page: _host.GitHubRelease | None, change: ReleaseChange) -> bool:
    """Recognize only absent creations or an unchanged previous edit page."""
    return page is None if change.operation == 'create' else page == change.previous


def _page_option(request: PageRequest) -> str | None:
    """Name the invocation option that requires a single publication tag."""
    if request.notes is not None:
        return '--release-notes-file'
    if request.requested:
        return '--release-page'
    return None


def _require_remote_tag(
    paths: _config.Paths,
    p: _config.ProjectConfig,
    dest: _host.Destination,
    publication: _PublishedTag,
    *,
    deadline: float | None = None,
) -> None:
    """Require the selected host tag to match the locally published commit."""
    timeout = None if deadline is None else deadline - _monotonic()
    if timeout is not None and timeout <= 0:
        raise ObservationTimeoutError
    remote = _host.ls_remote(dest, paths, p.name, timeout=timeout)
    actual = remote.tags.get(publication.tag)
    if actual != publication.commit:
        raise PubanonError(
            'release tag does not match the published commit on GitHub',
            expected=f'{escape(publication.tag)} -> {escape(publication.commit)}',
            actual=escape(actual) if actual is not None else 'tag is absent',
        )


def _show_creation(
    change: ReleaseChange | _PlannedPage, *, draft: bool, label: str = 'Release page'
) -> None:
    """Show the exact creation title, source, and full escaped notes."""
    action = 'create draft' if draft else 'create'
    row = f'{action} "{escape(change.title)}"'
    if change.notes_file is not None:
        row += f' with notes from {display_path(change.notes_file)}'
    elif change.changelog_missing:
        row += ' with empty notes (no CHANGELOG.md; set release_page.changelog)'
    output(field(label, row))
    output(field('Notes', ''))
    for line in change.notes.decode('utf-8').split('\n'):
        output(escape(line))


def _verify_publication(
    paths: _config.Paths,
    g: _config.GlobalConfig,
    p: _config.ProjectConfig,
    tag: str,
) -> tuple[_host.Destination, str]:
    """Resolve published refs without treating a prepared candidate as proof."""
    candidate = _records.load_candidate(paths, p.name)
    _candidate.require_known_outcome(candidate)
    if not _store.candidate_store_path(paths, p.name).is_dir():
        raise PubanonError(
            'no saved published history was found',
            recovery=_command(p.name, 'repo', 'restore', '--to', 'local'),
        )
    main, tags = _candidate.published_refs(paths, p.name)
    if main is None:
        raise PubanonError(
            'local published history has no main branch',
            recovery=_command(p.name, 'repo', 'restore', '--to', 'local'),
        )
    commit = tags.get(tag)
    if commit is None:
        raise PubanonError(
            'release tag is absent from local published refs',
            fact=escape(tag),
            recovery=_command(p.name, 'repo', 'restore', '--to', 'local'),
        )
    dest = _host.verify_destination(paths, g, p)
    _require_remote_tag(paths, p, dest, _PublishedTag(tag, commit))
    return dest, commit


def _keep_publication_page(
    paths: _config.Paths,
    g: _config.GlobalConfig,
    p: _config.ProjectConfig,
    tag: str,
) -> PageOutcome:
    """Recheck a kept page without recreating unpreviewed content."""
    destination, _commit = _verify_publication(paths, g, p, tag)
    page = next((page for page in _host.all_releases(destination) if page.tag == tag), None)
    if page is None:
        raise PubanonError('the previously existing release page disappeared')
    return PageOutcome('kept', page.url, page.draft)


def _verify_result(
    paths: _config.Paths,
    p: _config.ProjectConfig,
    change: ReleaseChange,
) -> _host.GitHubRelease:
    """Recheck invisible writes within a shared request and wait budget."""
    deadline = _monotonic() + _VERIFY_SECONDS
    _require_remote_tag(
        paths, p, change.destination, _PublishedTag(change.tag, change.commit), deadline=deadline
    )
    wait = _VERIFY_FIRST_WAIT
    while _monotonic() < deadline:
        page = _host.read_release(change.destination, change.tag, deadline=deadline)
        if page is not None and not _differences(page, change):
            return page
        if not _not_yet_visible(page, change):
            raise _ReleaseMismatchError(
                'release page read-back does not match the requested result',
                fact=_difference_report(page, change),
            )
        remaining = deadline - _monotonic()
        if remaining <= 0:
            break
        _sleep(min(wait, remaining))
        wait *= 2
    raise PubanonError('the release page was not visible within 10 seconds')


def _version_key(tag: str) -> tuple[int, ...] | None:
    """Return a strict `vX.Y.Z` tag's numeric version for ordering, or None."""
    version = _candidate.version_from_tag(tag)
    return None if version is None else tuple(int(part) for part in version.split('.'))


def _creation_scan(
    p: _config.ProjectConfig, change: ReleaseChange
) -> tuple[_host.GitHubRelease | None, bool]:
    """Scan once for a matching page and higher published numeric versions."""
    pages = _host.all_releases(change.destination)
    current = next((page for page in pages if page.tag == change.tag), None)
    if current is not None and _differences(current, change):
        raise _conflict_error(p, current, change)
    wanted = _version_key(change.tag)
    if wanted is None:
        raise PubanonError('release page requires a strict vX.Y.Z tag')
    higher = any(
        key > wanted
        for page in pages
        if not page.draft and not page.prerelease and (key := _version_key(page.tag)) is not None
    )
    return current, not higher


def _waiting_drafts(pages: tuple[_host.GitHubRelease, ...], tag: str) -> tuple[str, ...]:
    """Return lower strict-version tags whose pages are still drafts, oldest first."""
    current = _version_key(tag)
    if current is None:
        return ()
    waiting = {
        page.tag: key
        for page in pages
        if page.draft and (key := _version_key(page.tag)) is not None and key < current
    }
    return tuple(sorted(waiting, key=waiting.__getitem__))


def _waiting_warning(tags: tuple[str, ...]) -> str:
    """Name earlier versions whose release pages are still drafts."""
    names = escape(
        ' and '.join(tags)
        if len(tags) <= _PAIR_SIZE
        else ', '.join(tags[:-1]) + f', and {tags[-1]}'
    )
    if len(tags) == 1:
        return (
            f'warning: The release page for {names} is still a draft. '
            'Rerun its release workflow, or publish the page on GitHub.'
        )
    return (
        f'warning: The release pages for {names} are still drafts. '
        'Rerun their release workflows, or publish the pages on GitHub.'
    )


def create_command(
    project: str, tag: str, *, title: str | None = None, notes_file: Path | None = None
) -> str:
    """Render project-qualified creation recovery with explicit overrides.

    Args:
        project: Registered project name.
        tag: Existing publication tag for the page.
        title: Explicit title override, never a configured template result.
        notes_file: Supplied notes file to reuse, rendered as an absolute path.

    Returns:
        Shell-safe recovery command, or escaped manual guidance.
    """
    args = ['release', 'create', tag]
    if title is not None:
        args.extend(('--title', title))
    if notes_file is not None:
        args.extend(('--notes-file', str(notes_file.absolute())))
    return _command(project, *args)


def _render_page(
    paths: _config.Paths,
    p: _config.ProjectConfig,
    tag: str,
    commit: str,
    *,
    request: ReleaseRequest | None = None,
) -> _PlannedPage:
    """Render release page metadata from settings and the published tree."""
    version = _candidate.version_from_tag(tag)
    if version is None:
        raise PubanonError('release page requires a strict vX.Y.Z tag')
    settings = p.release_page or _config.ReleasePageSettings()
    title = request.title if request is not None else None
    notes = request.notes if request is not None else None
    template = settings.notes
    needs_changelog = notes is None and (
        template is None
        or any(
            field == 'changelog_url'
            for _text, field, _spec, _conversion in string.Formatter().parse(template)
        )
    )
    changelog_url = ''
    changelog_missing = False
    if needs_changelog:
        path = settings.changelog or 'CHANGELOG.md'
        if _candidate.regular_file_at(paths, p.name, commit, path):
            changelog_url = _host.blob_url(p.owner, p.repo, tag, path)
        elif settings.changelog is not None:
            raise PubanonError(
                f'changelog {escape(path)} is not a file in the published snapshot',
                fact='release_page.changelog names this file, and the notes link to it.',
                recovery='point release_page.changelog at a file in the published snapshot, '
                'or remove the setting',
            )
        elif template is not None:
            raise PubanonError(
                'release_page.notes uses {changelog_url}, but CHANGELOG.md is not in the '
                'published snapshot',
                recovery="set release_page.changelog to your changelog's path, "
                'or remove {changelog_url} from release_page.notes',
            )
        else:
            changelog_missing = True

    rendered_title = (
        title
        if title is not None
        else (settings.title or '{tag}').format_map({'tag': tag, 'version': version})
    )
    if not rendered_title.strip() or '\r' in rendered_title or '\n' in rendered_title:
        raise PubanonError('release page title must be one nonempty line')

    if notes is not None:
        rendered_notes = notes
    elif template is None:
        rendered_notes = (
            f'See the [changelog]({changelog_url}).\n'.encode() if changelog_url else b''
        )
    else:
        content = template.format_map(
            {'tag': tag, 'version': version, 'changelog_url': changelog_url}
        ).rstrip('\r\n')
        rendered_notes = (content + '\n').encode('utf-8') if content else b''
    return _PlannedPage(
        rendered_title,
        rendered_notes,
        request.notes_file if request is not None else None,
        changelog_missing,
    )


def apply_change(
    paths: _config.Paths,
    g: _config.GlobalConfig,
    p: _config.ProjectConfig,
    change: ReleaseChange,
) -> ReleaseResult:
    """Apply retained metadata and return only an independently verified page.

    Args:
        paths: Local configuration and data locations.
        g: The global configuration used during preparation.
        p: The project configuration used during preparation.
        change: Gated facts returned by `prepare_change`. The caller holds the
            same project lock across preparation, confirmation, and this call.

    Returns:
        Matching page and whether a write was attempted.

    Raises:
        PubanonError: Publication or page facts changed before writing.
        ReleaseOutcomeUnknownError: An attempted write could not be verified.
            A write is never retried.
        OperationCanceled: Interrupted mutation may have changed the page.
        ValueError: An existing-page result was incorrectly passed for writing.
    """
    if change.operation == 'existing':
        raise ValueError('a matching release page has no change to apply')
    destination, commit = _verify_publication(paths, g, p, change.tag)
    if destination != change.destination or commit != change.commit:
        raise PubanonError('release destination or tag changed since preview')
    mark_latest = True
    if change.operation == 'create':
        current, mark_latest = _creation_scan(p, change)
        if current is not None:
            return ReleaseResult(current, written=False)
    elif _host.read_release(destination, change.tag) != change.previous:
        raise PubanonError('release page changed since preview; prepare the change again')
    recovery = (
        create_command(
            p.name, change.tag, title=change.title_override, notes_file=change.notes_file
        )
        if change.operation == 'create'
        else _command(p.name, 'release', 'show', change.tag)
    )
    try:
        if change.operation == 'create':
            _host.create_release(
                destination,
                change.tag,
                title=change.title,
                notes=change.notes,
                mode='draft' if change.draft else ('latest' if mark_latest else 'not-latest'),
            )
        else:
            _host.edit_release(destination, change.tag, title=change.title, notes=change.notes)
        result = _verify_result(paths, p, change)
    except (PubanonError, ObservationTimeoutError, OSError, KeyboardInterrupt) as exc:
        error_type = (
            OperationCanceled
            if isinstance(exc, (OperationCanceled, KeyboardInterrupt))
            else ReleaseOutcomeUnknownError
        )
        detail = exc.render() if isinstance(exc, PubanonError) else str(exc)
        if isinstance(exc, ObservationTimeoutError):
            detail = 'release page observation timed out'
        raise error_type(
            'could not verify the release page; the page may have changed',
            fact=detail if isinstance(exc, _ReleaseMismatchError) else escape(detail),
            recovery=recovery,
        ) from exc
    return ReleaseResult(result, written=True)


def complete_publication_page(
    paths: _config.Paths,
    g: _config.GlobalConfig,
    p: _config.ProjectConfig,
    plan: PagePlan,
) -> PageOutcome:
    """Complete approved page work after Git publication has been finalized.

    Args:
        paths: Local configuration and published history locations.
        g: Global configuration retained from the publication preview.
        p: Project configuration retained under the same project lock.
        plan: Approved page action and exact gated creation bytes. The caller
            has already verified and finalized the Git publication.

    Returns:
        Verified page URL and whether creation, convergence, or retention
        completed; inactive plans return without host access.

    Raises:
        PubanonError: Page completion failed after code publication. Recovery
            creates or finds the page without repeating the Git publication.
        OperationCanceled: Interrupted page work after code publication.
    """
    if plan.action == 'none':
        return PageOutcome('none', None)
    if plan.tag is None:
        raise ValueError('active page plan has no tag')
    try:
        if plan.action == 'keep':
            outcome = _keep_publication_page(paths, g, p, plan.tag)
            return replace(outcome, kind='existing') if plan.reason == 'matches' else outcome
        if plan.change is None:
            raise ValueError('creation page plan has no retained change')
        result = apply_change(paths, g, p, plan.change)
        return PageOutcome(
            'created' if result.written else 'existing', result.page.url, result.page.draft
        )
    except (PubanonError, OSError, ObservationTimeoutError, KeyboardInterrupt) as exc:
        interrupted = isinstance(exc, (OperationCanceled, KeyboardInterrupt))
        uncertain = isinstance(exc, (ReleaseOutcomeUnknownError, OperationCanceled))
        message = 'may not have been created' if uncertain else 'was not created'
        detail = exc.render() if isinstance(exc, PubanonError) else escape(str(exc))
        if isinstance(exc, ObservationTimeoutError):
            detail = 'release page observation timed out'
        recovery = create_command(
            p.name,
            plan.tag,
            notes_file=plan.change.notes_file if plan.change is not None else None,
        )
        if isinstance(exc, _PageConflictError):
            # Creating again would refuse the same page; inspection or editing fixes it.
            detail = f'{exc}\n{exc.fact}' if exc.fact else str(exc)
            recovery = exc.recovery
        error_type = OperationCanceled if interrupted else PubanonError
        raise error_type(
            f'the release page {message}',
            fact='The code was published and local publication state was finalized.\n' + detail,
            recovery=recovery,
        ) from exc


def page_status(
    paths: _config.Paths,
    g: _config.GlobalConfig,
    p: _config.ProjectConfig,
    tag: str,
) -> _host.GitHubRelease | None:
    """Read a page after verifying its locally published tag on the host.

    Args:
        paths: Local configuration and published history locations.
        g: Current global identity and content rules.
        p: Selected project's final repository configuration.
        tag: Published version tag to look up without any write.

    Returns:
        Hosted page, or None only after a successful absent-page lookup.

    Raises:
        PubanonError: Publication verification or page lookup failed.
        ObservationTimeoutError: A host observation timed out.
    """
    destination, _commit = _verify_publication(paths, g, p, tag)
    return _host.read_release(destination, tag)


def page_tags(record: _records.CandidateRecord) -> tuple[str, ...]:
    """Return the explicit tag or sorted strict tags at the candidate commit.

    Args:
        record: Complete retained publication, including replacement poststate.

    Returns:
        Explicit tag alone, or all strict version tags pointing at the
        correction commit; replayed descendant tags are excluded.
    """
    if record.tag_intent is not None:
        return (record.tag_intent,)
    return tuple(
        sorted(
            tag
            for tag, commit in _records.candidate_poststate(record)[1].items()
            if commit == record.commit and _candidate.version_from_tag(tag) is not None
        )
    )


def _inactive_page_plan(
    p: _config.ProjectConfig, record: _records.CandidateRecord, request: PageRequest
) -> PagePlan | None:
    """Choose local-only outcomes before any lookup or rendering."""
    tags = page_tags(record)
    option = _page_option(request)
    if not tags:
        if option is not None:
            raise PubanonError(f'{option} requires a tagged publication')
        return PagePlan('none', 'untagged', project=p.name)
    if len(tags) > 1:
        if option is not None:
            raise PubanonError(
                f'{option} requires one unambiguous publication tag',
                recovery='\n'.join(
                    create_command(p.name, tag, notes_file=request.notes_file) for tag in tags
                ),
            )
        return PagePlan('none', 'ambiguous', candidates=tags, project=p.name)
    tag = next(iter(tags))
    if request.skip:
        return PagePlan('none', 'skipped', tag=tag, project=p.name)
    if request.notes is None and not request.requested and not _config.release_pages_enabled(p):
        return PagePlan('none', 'disabled', tag=tag, project=p.name)
    return None


def page_url_text(url: str, *, draft: bool) -> str:
    """Render a page URL for display, marking a page that is still a draft.

    Args:
        url: Page URL as GitHub reported it.
        draft: Whether the observed page is a draft.

    Returns:
        The escaped URL, followed by ` (draft)` for a draft.
    """
    return escape(url) + (' (draft)' if draft else '')


def release_metadata_findings(
    rules: Mapping[_gate.RuleClass, Collection[str]],
    *,
    tag: str,
    title: str,
    notes: bytes,
) -> list[str]:
    """Return rendered findings for release metadata under the current rules.

    Args:
        rules: Current content rules, including derived private terms.
        tag: Release tag to scan in full.
        title: Release title to scan in full.
        notes: UTF-8 release notes to scan in full.

    Returns:
        Finding lines in tag, title, then notes order; empty when clean.
    """
    findings: list[str] = []
    for name, data in (
        ('tag', tag.encode()),
        ('title', title.encode()),
        ('notes', notes),
    ):
        for hit in _gate.gate_unit(name, data, rules):
            span = _gate.first_match(data, hit.term)
            if span is None:
                raise PubanonError('gate match location could not be determined')
            start, end = span
            location = name
            if name == 'notes':
                location += f' line {data[:start].count(b"\n") + 1}'
            excerpt, _span = _gate.match_excerpt(data, start, end)
            context = f': {escape(excerpt)}'
            findings.append(
                f'{location}: {hit.rule_class.name.lower()} match {escape(hit.term)}{context}'
            )
    return findings


def _gate_metadata(
    paths: _config.Paths,
    g: _config.GlobalConfig,
    p: _config.ProjectConfig,
    tag: str,
    page: _PlannedPage,
) -> None:
    """Refuse either rule class in the entire resulting release metadata."""
    rules = _config.assemble_rules(g, p, _config.derived_private_terms(paths, p, os.environ))
    findings = release_metadata_findings(rules, tag=tag, title=page.title, notes=page.notes)
    if findings:
        raise PubanonError('release content is blocked', fact='\n'.join(findings))


def plan_publication_page(
    paths: _config.Paths,
    g: _config.GlobalConfig,
    p: _config.ProjectConfig,
    record: _records.CandidateRecord,
    request: PageRequest,
) -> PagePlan:
    """Choose, render, and gate page work before a publication push.

    Args:
        paths: Local configuration and candidate store locations.
        g: Current global identity and content rules.
        p: Selected project configuration held under its project lock.
        record: Retained candidate whose recorded destination the caller has
            already matched to the verified configured destination.
        request: Invocation overrides with notes read exactly once by the
            caller.

    Returns:
        Preview facts and exact gated bytes, without any host mutation.

    Raises:
        PubanonError: Page lookup, rendering, matching, or content rules
            prevent publication, or supplied notes have no unambiguous version
            tag.
    """
    inactive = _inactive_page_plan(p, record, request)
    if inactive is not None:
        return inactive
    tag = page_tags(record)[0]
    recorded = record.destination
    destination = _host.Destination(
        recorded.adapter,
        recorded.owner,
        recorded.repo,
        _host.repository_url(recorded.owner, recorded.repo),
        recorded.account,
        recorded.visibility,
    )
    try:
        pages = _host.all_releases(destination)
    except (PubanonError, OSError, ObservationTimeoutError) as exc:
        if isinstance(exc, OperationCanceled):
            raise
        raise PubanonError(
            'could not look up the release page',
            fact=escape(str(exc)),
            recovery=(
                'to publish now without the page, rerun with --no-release-page\n'
                f'add the page later: {create_command(p.name, tag)}'
            ),
        ) from exc
    previous = next((page for page in pages if page.tag == tag), None)
    if previous is not None and request.notes is None:
        return PagePlan('keep', 'exists', tag=tag, existing=previous, project=p.name)
    rendered = _render_page(
        paths,
        p,
        tag,
        record.commit,
        request=ReleaseRequest('create', tag, None, request.notes, request.notes_file),
    )
    change = ReleaseChange(
        'create',
        destination,
        tag,
        record.commit,
        rendered.title,
        rendered.notes,
        None,
        notes_file=rendered.notes_file,
        changelog_missing=rendered.changelog_missing,
        draft=_config.draft_release_pages(p),
    )
    if previous is not None:
        if _differences(previous, change):
            raise _conflict_error(p, previous, change)
        return PagePlan(
            'keep', 'matches', tag=tag, existing=previous, change=change, project=p.name
        )
    _gate_metadata(paths, g, p, tag, rendered)
    return PagePlan(
        'create',
        'new',
        tag=tag,
        change=change,
        project=p.name,
        waiting=_waiting_drafts(pages, tag),
    )


def prepare_change(
    paths: _config.Paths,
    g: _config.GlobalConfig,
    p: _config.ProjectConfig,
    request: ReleaseRequest,
) -> ReleaseChange:
    """Verify publication and gate the complete resulting release metadata.

    Args:
        paths: Local configuration and data locations.
        g: Current global identity and content rules.
        p: Selected project's final destination and rules.
        request: Operation and supplied title/notes for the selected tag.

    Returns:
        Exact checked metadata and preview facts. The caller must hold the
        project lock through confirmation and `apply_change`.

    Raises:
        PubanonError: Inputs, publication evidence, host facts, or content
            rules prevent the requested write. No mutation is attempted.
    """
    operation, tag, title, notes = request.operation, request.tag, request.title, request.notes
    if _candidate.version_from_tag(tag) is None:
        raise PubanonError('release tag must have vX.Y.Z form')
    if operation == 'edit' and title is None and notes is None:
        raise PubanonError('release edit requires a title or notes')
    if title == '':
        raise PubanonError('release title must not be empty')
    destination, commit = _verify_publication(paths, g, p, tag)
    planned = (
        _render_page(paths, p, tag, commit, request=request) if operation == 'create' else None
    )
    previous = _host.read_release(destination, tag)
    if operation == 'edit' and previous is None:
        raise PubanonError('release page does not exist', fact=escape(tag))
    try:
        if planned is not None:
            final_title, final_notes = planned.title, planned.notes
        else:
            if previous is None:
                raise ValueError('edit release change has no page')
            final_title = title if title is not None else previous.title
            final_notes = notes if notes is not None else previous.notes.encode()
        final_title.encode('utf-8')
        final_notes.decode('utf-8')
    except UnicodeError as exc:
        raise PubanonError('release title and notes must be valid UTF-8') from exc
    change = ReleaseChange(
        operation,
        destination,
        tag,
        commit,
        final_title,
        final_notes,
        previous if operation == 'edit' else None,
        notes_file=request.notes_file,
        changelog_missing=planned.changelog_missing if planned is not None else False,
        title_override=request.title,
        draft=operation == 'create' and _config.draft_release_pages(p),
    )
    if operation == 'create' and previous is not None:
        if _differences(previous, change):
            raise _conflict_error(p, previous, change)
        return replace(change, operation='existing', previous=previous)
    _gate_metadata(
        paths,
        g,
        p,
        tag,
        _PlannedPage(final_title, final_notes, request.notes_file, changelog_missing=False),
    )
    return change


def show_draft_guidance(p: _config.ProjectConfig, *, draft: bool) -> None:
    """Explain how a reported draft becomes public, when the project creates drafts.

    Another project's draft is not its release workflow's to publish, so
    nothing is shown unless `release_page.draft` is true.

    Args:
        p: Project whose `release_page.draft` setting applies.
        draft: Whether the reported page is still a draft.
    """
    if draft and _config.draft_release_pages(p):
        output(_DRAFT_GUIDANCE)


def show_page_outcome(p: _config.ProjectConfig, outcome: PageOutcome) -> None:
    """Report the page that completed publication observed on GitHub.

    Args:
        p: Project whose `release_page.draft` setting applies.
        outcome: Completed page work with a verified URL.

    Raises:
        ValueError: The outcome has no page URL.
    """
    if outcome.url is None:
        raise ValueError('page outcome has no URL')
    label = 'Release page already exists' if outcome.kind == 'existing' else 'Release page'
    output(field(label, page_url_text(outcome.url, draft=outcome.draft)))
    show_draft_guidance(p, draft=outcome.draft)


def show_page_plan(plan: PagePlan, *, published: bool = False) -> None:
    """Show the selected page action and full escaped creation notes.

    Args:
        plan: Retained preview facts; untagged plans produce no output.
        published: Whether the code publication has finished, so a page-less
            outcome shows how to add the page later.
    """
    if plan.reason == 'untagged':
        return
    if published and plan.reason in {'disabled', 'skipped'} and plan.tag is not None:
        output(field('Release page', 'not created'))
        output(f'Add one later with: {create_command(plan.project, plan.tag)}')
        return
    if plan.reason == 'ambiguous':
        output(
            field(
                'Release page', f'not created: {escape(" and ".join(plan.candidates))} point here'
            )
        )
        for tag in plan.candidates:
            output(create_command(plan.project, tag))
    elif plan.reason == 'disabled':
        output(field('Release page', 'not created; release pages are off for this project'))
    elif plan.reason == 'skipped':
        output(field('Release page', 'skipped for this publication'))
    elif plan.existing is not None:
        status = 'draft' if plan.existing.draft else 'published'
        output(field('Release page', f'keep existing page ({status}) {escape(plan.existing.url)}'))
    elif plan.change is not None:
        _show_creation(plan.change, draft=plan.change.draft)
        if plan.waiting:
            output(_waiting_warning(plan.waiting), kind='warning')


def show_planned_page(
    paths: _config.Paths, p: _config.ProjectConfig, record: _records.CandidateRecord
) -> None:
    """Show a local page preview for a retained candidate without host reads.

    Args:
        paths: Local configuration and retained snapshot locations.
        p: Current project settings used for the tentative preview.
        record: Prepared candidate whose publication tags select the page.
    """
    tags = page_tags(record)
    if not tags:
        return
    label = 'Release page (planned)'
    if len(tags) > 1:
        output(field(label, f'not created: {escape(" and ".join(tags))} point here'))
        for tag in tags:
            output(create_command(p.name, tag))
        return
    if not _config.release_pages_enabled(p):
        output(field(label, 'not created; release pages are off for this project'))
        return
    try:
        planned = _render_page(paths, p, next(iter(tags)), record.commit)
    except PubanonError as exc:
        output(field(label, f'cannot be planned: {escape(str(exc))}'))
        return
    _show_creation(planned, draft=_config.draft_release_pages(p), label=label)
