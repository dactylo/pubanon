"""Persist resumable guided-init drafts."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pubanon import _filesystem
from pubanon._config import PUBLICATION_ROUTES, Paths, create_data_dirs
from pubanon._errors import PubanonError

_ACTIONS = frozenset({'exclude', 'include'})
_DEFAULT_PENDING = (
    'selection',
    'owner',
    'repo',
    'visibility',
    'description',
    'issues',
    'pull_requests',
    'discussions',
    'projects',
    'wiki',
    'publication_route',
    'checks',
    'release_pages',
    'source_tags',
    'review',
)
_DRAFT_KEYS = frozenset(
    {
        'commit',
        'description',
        'flagged',
        'groups',
        'issues',
        'owner',
        'pending',
        'phase',
        'publication_route',
        'projects',
        'pull_requests',
        'release_page_drafts',
        'release_page_sections',
        'release_pages',
        'discussions',
        'repo',
        'required_checks',
        'reviewing',
        'self_publication',
        'source_tags',
        'source',
        'version',
        'visibility',
        'wiki',
    }
)
_OPTIONAL_DRAFT_KEYS = frozenset({'release_page_drafts', 'release_page_sections'})
_PAIR_LENGTH = 2
_PHASES = frozenset({'flagged', 'groups', 'preview'})
_STEPS = frozenset(_DEFAULT_PENDING)
_VERSION = 1


@dataclass(frozen=True)
class Draft:
    """Hold the durable state of one guided-init session.

    Persisted drafts have at least one pending step, end with `review`, and use
    the `flagged`, `groups`, or `preview` phase. An empty `pending` tuple is a
    transient completed state and is not persisted.

    Attributes:
        source: Source repository selected for the project.
        commit: Current saved source selection. Resume may replace it when the
            selected source changes.
        self_publication: Whether the source product is pubanon itself.
        pending: Init choices that remain unanswered.
        phase: Current file-selection phase.
        reviewing: Whether the operator is reviewing completed choices.
        flagged: Per-path include or exclude choices.
        groups: Group pattern choices in the order they were made.
        owner: Selected repository owner, when answered.
        repo: Selected repository name, when answered.
        visibility: Selected repository visibility, when answered.
        publication_route: Selected publication route, when answered.
        source_tags: Whether source version tags are selected.
        required_checks: Selected required checks, when answered.
        description: Selected repository description, when answered.
        issues: Selected issue-tracker setting, when answered.
        wiki: Selected wiki setting, when answered.
        projects: Selected projects setting, when answered.
        pull_requests: Selected pull requests setting, when answered.
        discussions: Selected discussions setting, when answered.
        release_pages: Whether tagged publications create release pages.
        release_page_drafts: Whether those pages are created as drafts.
        release_page_sections: Whether default notes include changelog
            sections, when answered.
    """

    source: Path
    commit: str
    self_publication: bool
    pending: tuple[str, ...] = _DEFAULT_PENDING
    phase: str = 'flagged'
    reviewing: bool = False
    flagged: tuple[tuple[str, str], ...] = ()
    groups: tuple[tuple[str, str], ...] = ()
    owner: str | None = None
    repo: str | None = None
    visibility: str | None = None
    publication_route: str | None = None
    source_tags: bool = False
    required_checks: tuple[str, ...] | None = None
    description: str | None = None
    issues: bool | None = None
    wiki: bool | None = None
    projects: bool | None = None
    pull_requests: bool | None = None
    discussions: bool | None = None
    release_pages: bool = False
    release_page_drafts: bool = False
    release_page_sections: bool | None = None


class InvalidDraftError(PubanonError):
    """Indicate that a saved setup draft cannot be resumed."""


def _boolean(data: dict[str, object], key: str) -> bool:
    """Return one required JSON boolean."""
    value = data[key]
    if not isinstance(value, bool):
        raise InvalidDraftError('invalid init draft')
    return value


def _optional_boolean(data: dict[str, object], key: str) -> bool | None:
    """Return one optional JSON boolean."""
    value = data[key]
    if value is None:
        return None
    if not isinstance(value, bool):
        raise InvalidDraftError('invalid init draft')
    return value


def _optional_string(data: dict[str, object], key: str) -> str | None:
    """Return one optional JSON string."""
    value = data[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidDraftError('invalid init draft')
    return value


def _pairs(data: dict[str, object], key: str) -> tuple[tuple[str, str], ...]:
    """Return one required JSON list of string pairs."""
    value = data[key]
    if not isinstance(value, list):
        raise InvalidDraftError('invalid init draft')
    pairs: list[tuple[str, str]] = []
    for pair in cast('list[object]', value):
        if not isinstance(pair, list):
            raise InvalidDraftError('invalid init draft')
        items = cast('list[object]', pair)
        if len(items) != _PAIR_LENGTH or not all(isinstance(item, str) for item in items):
            raise InvalidDraftError('invalid init draft')
        first, second = cast('list[str]', items)
        pairs.append((first, second))
    return tuple(pairs)


def _path(paths: Paths, name: str) -> Path:
    """Return the durable init-draft path for one project."""
    return paths.data_dir / 'projects' / name / 'init-draft.json'


def _payload(draft: Draft) -> dict[str, object]:
    """Return one draft as versioned JSON data."""
    return {
        'version': _VERSION,
        'source': str(draft.source),
        'commit': draft.commit,
        'self_publication': draft.self_publication,
        'source_tags': draft.source_tags,
        'release_pages': draft.release_pages,
        'release_page_drafts': draft.release_page_drafts,
        'release_page_sections': draft.release_page_sections,
        'pending': list(draft.pending),
        'phase': draft.phase,
        'reviewing': draft.reviewing,
        'flagged': [list(pair) for pair in draft.flagged],
        'groups': [list(pair) for pair in draft.groups],
        'owner': draft.owner,
        'repo': draft.repo,
        'visibility': draft.visibility,
        'publication_route': draft.publication_route,
        'required_checks': (
            None if draft.required_checks is None else list(draft.required_checks)
        ),
        'description': draft.description,
        'issues': draft.issues,
        'wiki': draft.wiki,
        'projects': draft.projects,
        'pull_requests': draft.pull_requests,
        'discussions': draft.discussions,
    }


def _string(data: dict[str, object], key: str) -> str:
    """Return one required JSON string."""
    value = data[key]
    if not isinstance(value, str):
        raise InvalidDraftError('invalid init draft')
    return value


def _strings(data: dict[str, object], key: str) -> tuple[str, ...]:
    """Return one required JSON string list."""
    value = data[key]
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in cast('list[object]', value)
    ):
        raise InvalidDraftError('invalid init draft')
    return tuple(cast('list[str]', value))


def _optional_strings(data: dict[str, object], key: str) -> tuple[str, ...] | None:
    """Return one optional JSON string list."""
    if data[key] is None:
        return None
    return _strings(data, key)


def _draft(data: object) -> Draft:
    """Validate JSON data and return its init draft."""
    if not isinstance(data, dict):
        raise InvalidDraftError('invalid init draft')
    values = cast('dict[str, object]', data)
    if type(values.get('version')) is not int:
        raise InvalidDraftError('invalid init draft')
    if values['version'] != _VERSION:
        raise InvalidDraftError('invalid init draft version')
    if not _DRAFT_KEYS - _OPTIONAL_DRAFT_KEYS <= values.keys() <= _DRAFT_KEYS:
        raise InvalidDraftError('invalid init draft')
    pending = _strings(values, 'pending')
    phase = _string(values, 'phase')
    flagged = _pairs(values, 'flagged')
    groups = _pairs(values, 'groups')
    publication_route = _optional_string(values, 'publication_route')
    source_tags = _boolean(values, 'source_tags')
    release_pages = _boolean(values, 'release_pages')
    release_page_drafts = (
        _boolean(values, 'release_page_drafts') if 'release_page_drafts' in values else False
    )
    if (
        not pending
        or pending[-1] != 'review'
        or any(step not in _STEPS for step in pending)
        or phase not in _PHASES
        or any(action not in _ACTIONS for _pattern, action in (*flagged, *groups))
        or (publication_route is None and 'publication_route' not in pending)
        or (publication_route is not None and publication_route not in PUBLICATION_ROUTES)
        or (release_page_drafts and not release_pages)
    ):
        raise InvalidDraftError('invalid init draft')
    return Draft(
        source=Path(_string(values, 'source')),
        commit=_string(values, 'commit'),
        self_publication=_boolean(values, 'self_publication'),
        pending=pending,
        phase=phase,
        reviewing=_boolean(values, 'reviewing'),
        flagged=flagged,
        groups=groups,
        owner=_optional_string(values, 'owner'),
        repo=_optional_string(values, 'repo'),
        visibility=_optional_string(values, 'visibility'),
        publication_route=publication_route,
        source_tags=source_tags,
        required_checks=_optional_strings(values, 'required_checks'),
        description=_optional_string(values, 'description'),
        issues=_optional_boolean(values, 'issues'),
        wiki=_optional_boolean(values, 'wiki'),
        projects=_optional_boolean(values, 'projects'),
        pull_requests=_optional_boolean(values, 'pull_requests'),
        discussions=_optional_boolean(values, 'discussions'),
        release_pages=release_pages,
        release_page_drafts=release_page_drafts,
        release_page_sections=(
            _optional_boolean(values, 'release_page_sections')
            if 'release_page_sections' in values
            else None
        ),
    )


def load(paths: Paths, name: str) -> Draft | None:
    """Load one project init draft when it exists.

    Args:
        paths: Derived pubanon configuration and data paths.
        name: Project whose draft should be loaded.

    Returns:
        The validated draft, or `None` when no draft exists.

    Raises:
        InvalidDraftError: The draft contains invalid data.
        PubanonError: The draft cannot be read.
    """
    try:
        content = _path(paths, name).read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise PubanonError('cannot load init draft') from exc
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise InvalidDraftError('invalid init draft') from exc
    return _draft(data)


def remove(paths: Paths, name: str) -> None:
    """Remove one project init draft when it exists.

    Args:
        paths: Derived pubanon configuration and data paths.
        name: Project whose draft should be removed.

    Raises:
        PubanonError: The draft cannot be removed.
    """
    try:
        _path(paths, name).unlink(missing_ok=True)
    except OSError as exc:
        raise PubanonError('cannot remove init draft') from exc


def save(paths: Paths, name: str, draft: Draft) -> None:
    """Atomically save one project init draft with owner-only POSIX permissions.

    Args:
        paths: Derived pubanon configuration and data paths.
        name: Project whose draft should be saved.
        draft: Complete guided-init state to persist.

    Raises:
        PubanonError: The draft is invalid or cannot be saved. A failure after
            replacement reports that it may have been saved.
    """
    payload = _payload(draft)
    _draft(payload)
    try:
        content = (json.dumps(payload, ensure_ascii=False, indent=2) + '\n').encode('utf-8')
    except (TypeError, ValueError, UnicodeError) as exc:
        raise PubanonError('invalid init draft') from exc

    create_data_dirs(paths, name)
    destination = _path(paths, name)
    try:
        _filesystem.write_private(destination, content)
    except _filesystem.DurableWriteError as exc:
        if exc.interrupted and not exc.replacement_attempted:
            raise KeyboardInterrupt from exc.__cause__
        message = (
            'init draft may have been saved'
            if exc.replaced or (exc.interrupted and exc.replacement_attempted)
            else 'cannot save init draft'
        )
        raise PubanonError(message) from exc.__cause__
