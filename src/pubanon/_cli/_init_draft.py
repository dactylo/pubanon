"""Persist resumable guided-init drafts."""

import json
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from pubanon import _filesystem
from pubanon._config import Paths, create_data_dirs
from pubanon._errors import PubanonError

_ACTIONS = frozenset({'exclude', 'include'})
_DEFAULT_PENDING = (
    'selection',
    'owner',
    'repo',
    'visibility',
    'checks',
    'description',
    'issues',
    'wiki',
    'projects',
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
        'projects',
        'repo',
        'required_checks',
        'reviewing',
        'self_publication',
        'source',
        'version',
        'visibility',
        'wiki',
    }
)
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
        self_publication: Whether the source product is Pubanon itself.
        pending: Init choices that remain unanswered.
        phase: Current file-selection phase.
        reviewing: Whether the operator is reviewing completed choices.
        flagged: Per-path include or exclude choices.
        groups: Group pattern choices in the order they were made.
        owner: Selected repository owner, when answered.
        repo: Selected repository name, when answered.
        visibility: Selected repository visibility, when answered.
        required_checks: Selected required checks, when answered.
        description: Selected repository description, when answered.
        issues: Selected issue-tracker setting, when answered.
        wiki: Selected wiki setting, when answered.
        projects: Selected projects setting, when answered.
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
    required_checks: tuple[str, ...] | None = None
    description: str | None = None
    issues: bool | None = None
    wiki: bool | None = None
    projects: bool | None = None


def _boolean(data: dict[str, object], key: str) -> bool:
    """Return one required JSON boolean."""
    value = data[key]
    if not isinstance(value, bool):
        raise PubanonError('invalid init draft')
    return value


def _optional_boolean(data: dict[str, object], key: str) -> bool | None:
    """Return one optional JSON boolean."""
    value = data[key]
    if value is None:
        return None
    if not isinstance(value, bool):
        raise PubanonError('invalid init draft')
    return value


def _optional_string(data: dict[str, object], key: str) -> str | None:
    """Return one optional JSON string."""
    value = data[key]
    if value is None:
        return None
    if not isinstance(value, str):
        raise PubanonError('invalid init draft')
    return value


def _pairs(data: dict[str, object], key: str) -> tuple[tuple[str, str], ...]:
    """Return one required JSON list of string pairs."""
    value = data[key]
    if not isinstance(value, list):
        raise PubanonError('invalid init draft')
    pairs: list[tuple[str, str]] = []
    for pair in cast('list[object]', value):
        if not isinstance(pair, list):
            raise PubanonError('invalid init draft')
        items = cast('list[object]', pair)
        if len(items) != _PAIR_LENGTH or not all(isinstance(item, str) for item in items):
            raise PubanonError('invalid init draft')
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
        'pending': list(draft.pending),
        'phase': draft.phase,
        'reviewing': draft.reviewing,
        'flagged': [list(pair) for pair in draft.flagged],
        'groups': [list(pair) for pair in draft.groups],
        'owner': draft.owner,
        'repo': draft.repo,
        'visibility': draft.visibility,
        'required_checks': (
            None if draft.required_checks is None else list(draft.required_checks)
        ),
        'description': draft.description,
        'issues': draft.issues,
        'wiki': draft.wiki,
        'projects': draft.projects,
    }


def _string(data: dict[str, object], key: str) -> str:
    """Return one required JSON string."""
    value = data[key]
    if not isinstance(value, str):
        raise PubanonError('invalid init draft')
    return value


def _strings(data: dict[str, object], key: str) -> tuple[str, ...]:
    """Return one required JSON string list."""
    value = data[key]
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in cast('list[object]', value)
    ):
        raise PubanonError('invalid init draft')
    return tuple(cast('list[str]', value))


def _optional_strings(data: dict[str, object], key: str) -> tuple[str, ...] | None:
    """Return one optional JSON string list."""
    if data[key] is None:
        return None
    return _strings(data, key)


def _draft(data: object) -> Draft:
    """Validate JSON data and return its init draft."""
    if not isinstance(data, dict):
        raise PubanonError('invalid init draft')
    values = cast('dict[str, object]', data)
    if values.keys() != _DRAFT_KEYS or type(values['version']) is not int:
        raise PubanonError('invalid init draft')
    if values['version'] != _VERSION:
        raise PubanonError('invalid init draft version')
    pending = _strings(values, 'pending')
    phase = _string(values, 'phase')
    flagged = _pairs(values, 'flagged')
    groups = _pairs(values, 'groups')
    if (
        not pending
        or pending[-1] != 'review'
        or any(step not in _STEPS for step in pending)
        or phase not in _PHASES
        or any(action not in _ACTIONS for _pattern, action in (*flagged, *groups))
    ):
        raise PubanonError('invalid init draft')
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
        required_checks=_optional_strings(values, 'required_checks'),
        description=_optional_string(values, 'description'),
        issues=_optional_boolean(values, 'issues'),
        wiki=_optional_boolean(values, 'wiki'),
        projects=_optional_boolean(values, 'projects'),
    )


def load(paths: Paths, name: str) -> Draft | None:
    """Load one project init draft when it exists.

    Args:
        paths: Derived Pubanon configuration and data paths.
        name: Project whose draft should be loaded.

    Returns:
        The validated draft, or `None` when no draft exists.

    Raises:
        PubanonError: The draft cannot be read or contains invalid data.
    """
    try:
        content = _path(paths, name).read_text(encoding='utf-8')
    except FileNotFoundError:
        return None
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise PubanonError('cannot load init draft') from exc
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise PubanonError('invalid init draft') from exc
    return _draft(data)


def remove(paths: Paths, name: str) -> None:
    """Remove one project init draft when it exists.

    Args:
        paths: Derived Pubanon configuration and data paths.
        name: Project whose draft should be removed.

    Raises:
        PubanonError: The draft cannot be removed.
    """
    try:
        _path(paths, name).unlink(missing_ok=True)
    except (OSError, RuntimeError) as exc:
        raise PubanonError('cannot remove init draft') from exc


def save(paths: Paths, name: str, draft: Draft) -> None:
    """Atomically save one project init draft with owner-only POSIX permissions.

    Args:
        paths: Derived Pubanon configuration and data paths.
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

    directory = create_data_dirs(paths, name)
    destination = _path(paths, name)
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix='.init-draft-',
            suffix='.tmp',
            dir=directory,
        )
        temporary = Path(temporary_name)
        _filesystem.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, 'wb')
        descriptor = None
        try:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        except (OSError, RuntimeError):
            with suppress(OSError, RuntimeError):
                handle.close()
            raise
        handle.close()
        _filesystem.replace(temporary, destination)
    except (OSError, RuntimeError) as exc:
        if descriptor is not None:
            with suppress(OSError, RuntimeError):
                os.close(descriptor)
        if temporary is not None:
            with suppress(OSError, RuntimeError):
                temporary.unlink(missing_ok=True)
        raise PubanonError('cannot save init draft') from exc
    try:
        _filesystem.sync_directory(directory)
    except (OSError, RuntimeError, KeyboardInterrupt) as exc:
        raise PubanonError('init draft may have been saved') from exc
