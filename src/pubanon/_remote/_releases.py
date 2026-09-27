"""Gate release metadata and verify existing publication tags around writes."""

import os
from dataclasses import dataclass
from typing import Literal

from pubanon import _config, _gate
from pubanon._errors import OperationCanceled, PubanonError
from pubanon._remote import _host
from pubanon._snapshots import _candidate, _records
from pubanon._terminal import escape, shell_command


@dataclass(frozen=True)
class ReleaseChange:
    """Retain the exact gated release change for confirmation and execution.

    Instances returned by `prepare_change` are applied under the same project
    lock and configuration. Notes retain the checked UTF-8 bytes.

    Attributes:
        operation: Whether to create or edit the page.
        destination: Verified configured final repository.
        tag: Existing strict publication tag.
        commit: Locally published commit matched by the host tag.
        title: Complete resulting title.
        notes: Complete resulting UTF-8 notes.
        previous: Previewed page for edit, or `None` for create.
    """

    operation: Literal['create', 'edit']
    destination: _host.Destination
    tag: str
    commit: str
    title: str
    notes: bytes
    previous: _host.GitHubRelease | None


@dataclass(frozen=True)
class ReleaseRequest:
    """Group the requested operation and supplied release metadata.

    Attributes:
        operation: Explicit create or edit, never an upsert.
        tag: Existing strict `vX.Y.Z` publication tag.
        title: Nonempty supplied title, or `None` to retain an edit title or
            default a new page's title to its tag.
        notes: UTF-8 bytes, required for create. `None` retains edit notes;
            empty bytes explicitly clear them.
    """

    operation: Literal['create', 'edit']
    tag: str
    title: str | None
    notes: bytes | None


def _command(project: str, *args: str) -> str:
    """Render project-qualified recovery without unsafe terminal characters."""
    command = shell_command('pubanon', *args, f'--project={project}')
    if command is not None:
        return command
    return (
        f'Choose project {escape(project)} from its registered directory, '
        f'then run pubanon {escape(" ".join(args))}.'
    )


def _gate_metadata(
    paths: _config.Paths,
    g: _config.GlobalConfig,
    p: _config.ProjectConfig,
    change: ReleaseChange,
) -> None:
    """Refuse either rule class in the entire resulting release metadata."""
    rules = _config.assemble_rules(g, p, _config.derived_private_terms(paths, p, os.environ))
    findings: list[str] = []
    for name, data in (
        ('tag', change.tag.encode()),
        ('title', change.title.encode()),
        ('notes', change.notes),
    ):
        for hit in _gate.gate_unit(name, data, rules):
            span = _gate.first_match(data, hit.term)
            location = name
            context = ''
            if span is not None:
                start, end = span
                if name == 'notes':
                    location += f' line {data[:start].count(b"\n") + 1}'
                line_start = data.rfind(b'\n', 0, start) + 1
                line_end = data.find(b'\n', end)
                excerpt = data[line_start : None if line_end < 0 else line_end]
                context = f': {escape(excerpt.decode("utf-8"))}'
            findings.append(
                f'{location}: {hit.rule_class.name.lower()} match {escape(hit.term)}{context}'
            )
    if findings:
        raise PubanonError('release content is blocked', fact='\n'.join(findings))


def _require_remote_tag(
    paths: _config.Paths,
    p: _config.ProjectConfig,
    dest: _host.Destination,
    tag: str,
    commit: str,
) -> None:
    """Require the selected host tag to match the locally published commit."""
    remote = _host.ls_remote(dest, paths, p.name)
    actual = remote.tags.get(tag)
    if actual != commit:
        raise PubanonError(
            'release tag does not match the published commit on GitHub',
            expected=f'{escape(tag)} -> {escape(commit)}',
            actual=escape(actual) if actual is not None else 'tag is absent',
        )


def _verify_publication(
    paths: _config.Paths,
    g: _config.GlobalConfig,
    p: _config.ProjectConfig,
    tag: str,
) -> tuple[_host.Destination, str]:
    """Resolve published refs without treating a prepared candidate as proof."""
    candidate = _records.load_candidate(paths, p.name)
    if candidate is not None and candidate.state == _records.STATE_UNKNOWN_OUTCOME:
        raise PubanonError(
            'the outcome of the last publication is unknown',
            recovery=_command(p.name, 'publish', '--resolve'),
        )
    if not _candidate.candidate_store_path(paths, p.name).is_dir():
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
    _require_remote_tag(paths, p, dest, tag, commit)
    return dest, commit


def _verify_result(
    paths: _config.Paths,
    p: _config.ProjectConfig,
    change: ReleaseChange,
) -> _host.GitHubRelease:
    """Independently verify the resulting page and its existing tag."""
    _require_remote_tag(paths, p, change.destination, change.tag, change.commit)
    result = _host.read_release(change.destination, change.tag)
    draft = change.previous.draft if change.previous is not None else False
    prerelease = change.previous.prerelease if change.previous is not None else False
    if (
        result is None
        or result.tag != change.tag
        or result.title != change.title
        or result.notes.encode() != change.notes
        or result.draft != draft
        or result.prerelease != prerelease
    ):
        raise PubanonError('release read-back does not match the requested result')
    return result


def apply_change(
    paths: _config.Paths,
    g: _config.GlobalConfig,
    p: _config.ProjectConfig,
    change: ReleaseChange,
) -> _host.GitHubRelease:
    """Apply retained metadata and return only an independently verified page.

    Args:
        paths: Local configuration and data locations.
        g: The global configuration used during preparation.
        p: The project configuration used during preparation.
        change: Gated facts returned by `prepare_change`. The caller holds the
            same project lock across preparation, confirmation, and this call.

    Raises:
        PubanonError: Publication or page facts changed before writing, or the
            mutation outcome cannot be verified. A write is never retried.
        OperationCanceled: Interrupted mutation may have changed the page.
    """
    destination, commit = _verify_publication(paths, g, p, change.tag)
    if destination != change.destination or commit != change.commit:
        raise PubanonError('release destination or tag changed since preview')
    current = _host.read_release(destination, change.tag)
    if current != change.previous:
        raise PubanonError('release page changed since preview; prepare the change again')
    try:
        write = _host.create_release if change.operation == 'create' else _host.edit_release
        write(destination, change.tag, title=change.title, notes=change.notes)
        result = _verify_result(paths, p, change)
    except (PubanonError, OSError, KeyboardInterrupt) as exc:
        error_type = (
            OperationCanceled
            if isinstance(exc, (OperationCanceled, KeyboardInterrupt))
            else PubanonError
        )
        detail = exc.render() if isinstance(exc, PubanonError) else str(exc)
        raise error_type(
            'release outcome is uncertain; the page may have changed',
            fact=escape(detail),
            recovery=_command(p.name, 'release', 'show', change.tag),
        ) from exc
    return result


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
    if operation == 'create' and notes is None:
        raise PubanonError('release creation requires notes')
    if operation == 'edit' and title is None and notes is None:
        raise PubanonError('release edit requires a title or notes')
    if title == '':
        raise PubanonError('release title must not be empty')
    destination, commit = _verify_publication(paths, g, p, tag)
    previous = _host.read_release(destination, tag)
    if operation == 'create' and previous is not None:
        raise PubanonError(
            'release page already exists',
            recovery=_command(p.name, 'release', 'show', tag),
        )
    if operation == 'edit' and previous is None:
        raise PubanonError('release page does not exist', fact=escape(tag))
    try:
        final_title = title if title is not None else previous.title if previous else tag
        final_notes = notes if notes is not None else previous.notes.encode() if previous else b''
        final_title.encode('utf-8')
        final_notes.decode('utf-8')
    except UnicodeError as exc:
        raise PubanonError('release title and notes must be valid UTF-8') from exc
    change = ReleaseChange(operation, destination, tag, commit, final_title, final_notes, previous)
    _gate_metadata(paths, g, p, change)
    return change
