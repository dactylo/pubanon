"""Persist private candidate and confirmed published snapshot records."""

import os
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

from pubanon import _filesystem
from pubanon._config import Paths, create_data_dirs, require_project_name, validate_host_segment
from pubanon._errors import PubanonError
from pubanon._run import OID_HEX
from pubanon._snapshots._source import (
    PinnedSource,
    SourceBookmark,
    bookmark_ref,
    path_portability_keys,
)
from pubanon._terminal import escape, path_text
from pubanon._toml import toml_string

STATE_RETAINED = 'retained'
STATE_UNKNOWN_OUTCOME = 'unknown-outcome'

_BOOKMARK_KEYS = frozenset({'source_path', 'git_common_dir', 'refname'})
_CANDIDATE_KEYS = frozenset(
    {
        'commit',
        'tree',
        'snapshot_digest',
        'fingerprint',
        'state',
        'message',
        'tag_intent',
        'prestate_main',
        'pinned',
        'destination',
        'prestate_tags',
        'source_bookmark',
        'replaces',
        'replace_scope',
        'tip',
        'poststate_tags',
        'replayed',
    }
)
_CANDIDATE_REQUIRED = _CANDIDATE_KEYS - {
    'tag_intent',
    'prestate_main',
    'source_bookmark',
    'replaces',
    'replace_scope',
    'tip',
    'poststate_tags',
    'replayed',
}
_DESTINATION_KEYS = frozenset({'account', 'adapter', 'owner', 'repo', 'visibility'})
_OID_PATTERN = re.compile(OID_HEX)
_PUBLICATION_KEYS = frozenset(
    {
        'schema_version',
        'commit',
        'tree',
        'snapshot_digest',
        'fingerprint',
        'message',
        'tag_intent',
        'confirmed_at',
        'pinned',
        'destination',
        'source_bookmark',
        'replaces',
        'replace_scope',
        'derivation',
    }
)
_PUBLICATION_REQUIRED = _PUBLICATION_KEYS - {
    'tag_intent',
    'source_bookmark',
    'replaces',
    'replace_scope',
    'pinned',
    'derivation',
}
_REWRITE_KEYS = frozenset(
    {'correction', 'replaces', 'prestate_main', 'descendants', 'phase', 'replayed', 'pause'}
)
_VALID_MODES = frozenset({0o100644, 0o100755, 0o120000})
_VALID_STATES = frozenset({STATE_RETAINED, STATE_UNKNOWN_OUTCOME})
_VERSION_PATTERN = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')
type _TOML_VALUE = (
    str | int | datetime | tuple[str, ...] | dict[str, _TOML_VALUE] | list[dict[str, _TOML_VALUE]]
)


@dataclass(frozen=True)
class ConflictStage:
    """Retain an unmerged file's path, Git mode, object ID, and index stage.

    Stages `1`, `2`, and `3` denote base, ours, and theirs, respectively.
    """

    path: str
    mode: int
    oid: str
    stage: Literal[1, 2, 3]


@dataclass(frozen=True)
class ConflictEntry:
    """Preserve Git diagnostics separately from frozen actionable leaf paths.

    `decision_paths` may be empty in parsed merge output, but must be expanded
    before a conflict is saved in a durable rewrite pause.
    """

    paths: tuple[str, ...]
    conflict_type: str
    message: str
    stages: tuple[ConflictStage, ...]
    decision_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecordedDestination:
    """Retain the configured host destination facts.

    Attributes:
        adapter: Host adapter selected for publication.
        owner: Configured host namespace.
        repo: Configured repository name.
        visibility: Required destination visibility.
        account: Expected publishing account.
    """

    adapter: str
    owner: str
    repo: str
    visibility: str
    account: str


@dataclass(frozen=True)
class Resolution:
    """Record one completed decision for a single actionable conflict path.

    `new_path` is required only for a rename. The singleton `paths` tuple
    retains the conflict protocol's representation without ambiguous groups.
    """

    paths: tuple[str, ...]
    action: Literal['keep', 'delete', 'rename']
    new_path: str | None = None


@dataclass(frozen=True)
class Derivation:
    """Describe a replayed publication without claiming its tree is a source snapshot.

    `original_source` is optional input provenance. `correction` identifies the
    base correction, and `resolutions` retains completed manual decisions.
    """

    original: str
    original_source: str | None
    correction: str
    resolutions: tuple[Resolution, ...] = ()


@dataclass(frozen=True)
class PublicationRecord:
    """Retain immutable provenance of one confirmed publication.

    Attributes:
        commit: Published sanitized commit ID.
        tree: Published sanitized tree ID.
        snapshot_digest: Digest of published content.
        fingerprint: Publication configuration fingerprint.
        message: Controlled published commit message.
        tag_intent: Optional published release tag.
        pinned: Original source commit and selector; absent for derived
            records.
        destination: Frozen host destination.
        confirmed_at: First confirmed publication time in UTC.
        source_bookmark: Required source preservation locator for pinned
            publications; optional for derived publications.
        replaces: Optional old publication selected for replacement.
        replace_scope: Replace one publication or all old history.
        derivation: Replay provenance, present exactly when pinned is absent.
    """

    commit: str
    tree: str
    snapshot_digest: str
    fingerprint: str
    message: str
    tag_intent: str | None
    pinned: PinnedSource | None
    destination: RecordedDestination
    confirmed_at: datetime
    source_bookmark: SourceBookmark | None
    replaces: str | None = None
    replace_scope: Literal['one', 'all'] | None = None
    derivation: Derivation | None = None


@dataclass(frozen=True)
class ReplayEntry:
    """Retain one old-to-new publication mapping and its resolution provenance.

    `original_bookmark` belongs to `old`; `source_bookmark` independently
    preserves the same known original source for `new`. Neither is invented
    when the original source is unknown.
    """

    old: str
    new: str
    original_source: str | None = None
    original_bookmark: SourceBookmark | None = None
    source_bookmark: SourceBookmark | None = None
    resolutions: tuple[Resolution, ...] = ()


@dataclass(frozen=True)
class CandidateRecord:
    """Retain every fact needed to verify and publish one candidate.

    Attributes:
        commit: Retained sanitized commit ID.
        tree: Retained sanitized tree ID.
        snapshot_digest: Digest of candidate content.
        tag_intent: Optional release tag.
        fingerprint: Publication configuration fingerprint.
        pinned: Original source commit and selector.
        destination: Frozen host destination.
        prestate_main: Main tip observed before construction.
        prestate_tags: Tag map observed before construction.
        state: Retained publication outcome state.
        message: Controlled commit message.
        source_bookmark: Optional source preservation locator.
        replaces: Optional old publication selected for replacement.
        replace_scope: Replace one publication or all old history.
        tip: Complete replacement main tip; absent while replay is incomplete.
        poststate_tags: Complete replacement tags, including an explicit empty
            map.
        replayed: Ordered completed replay provenance.
    """

    commit: str
    tree: str
    snapshot_digest: str
    tag_intent: str | None
    fingerprint: str
    pinned: PinnedSource
    destination: RecordedDestination
    prestate_main: str | None
    prestate_tags: dict[str, str]
    state: str
    message: str
    source_bookmark: SourceBookmark | None = None
    replaces: str | None = None
    replace_scope: Literal['one', 'all'] | None = None
    tip: str | None = None
    poststate_tags: dict[str, str] | None = None
    replayed: tuple[ReplayEntry, ...] = ()


@dataclass(frozen=True)
class RewritePause:
    """Retain the next old commit, pending merged tree, and expanded conflicts."""

    old: str
    tree: str
    conflicts: tuple[ConflictEntry, ...]


@dataclass(frozen=True)
class RewriteRecord:
    """Retain frozen replay inputs and progress independently of publication outcome.

    `correction` is the new commit replacing `replaces`; `prestate_main` is the
    old publication tip. `descendants` lists the old commits after `replaces`
    in oldest-first order through `prestate_main`. It is empty when `replaces`
    is already the old tip.

    `replayed` is a completed prefix of `descendants`. Only the exporting and
    awaiting phases carry a pause, for the next unprocessed descendant.
    """

    correction: str
    replaces: str
    prestate_main: str
    descendants: tuple[str, ...]
    phase: Literal['replaying', 'exporting', 'awaiting']
    replayed: tuple[ReplayEntry, ...] = ()
    pause: RewritePause | None = None


def _array(data: Mapping[str, object], key: str, error: str) -> list[object]:
    """Read a required TOML array without accepting a scalar or table."""
    value = data.get(key)
    if not isinstance(value, list):
        raise PubanonError(error)
    return cast('list[object]', value)


def _bookmark_data(bookmark: SourceBookmark) -> dict[str, _TOML_VALUE]:
    """Represent a source locator using its exact established field names."""
    return {
        'source_path': str(bookmark.source_path),
        'git_common_dir': str(bookmark.git_common_dir),
        'refname': bookmark.refname,
    }


def _conflict_data(conflict: ConflictEntry) -> dict[str, _TOML_VALUE]:
    """Represent frozen diagnostics and their independently expanded file decisions."""
    return {
        'paths': conflict.paths,
        'conflict_type': conflict.conflict_type,
        'message': conflict.message,
        'decision_paths': conflict.decision_paths,
        'stages': [
            {'path': stage.path, 'mode': stage.mode, 'oid': stage.oid, 'stage': stage.stage}
            for stage in conflict.stages
        ],
    }


def _integer(data: Mapping[str, object], key: str, error: str) -> int:
    """Read a required TOML integer, excluding booleans."""
    value = data.get(key)
    if type(value) is not int:
        raise PubanonError(error)
    return value


def _keys(data: Mapping[str, object], required: set[str], optional: set[str], error: str) -> None:
    """Reject missing or unknown fields at one table boundary."""
    if required - data.keys() or data.keys() - required - optional:
        raise PubanonError(error)


def _matches_suffix(name: str, suffix: str) -> bool:
    """Match a publication inventory suffix with platform path semantics."""
    if os.name == 'nt':
        return name.casefold().endswith(suffix.casefold())
    return name.endswith(suffix)


def _optional_string(data: Mapping[str, object], key: str, error: str) -> str | None:
    """Read an optional TOML string."""
    value = data.get(key)
    if not isinstance(value, str | None):
        raise PubanonError(error)
    return value


def _publication_inventory(directory: Path, suffix: str, error: str) -> tuple[Path, ...]:
    """Enumerate one complete publication inventory or refuse an unreadable directory."""
    try:
        entries = os.scandir(directory)
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise PubanonError(error) from exc
    try:
        with entries:
            return tuple(
                Path(entry.path) for entry in entries if _matches_suffix(entry.name, suffix)
            )
    except OSError as exc:
        raise PubanonError(error) from exc


def _read(path: Path, error: str, *, fact: str | None = None) -> dict[str, object] | None:
    """Load a strict UTF-8 TOML document without creating directories."""
    try:
        if not path.exists():
            return None
        return tomllib.loads(path.read_text(encoding='utf-8'))
    # tomllib.loads raises RecursionError for deeply nested records.
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        raise PubanonError(error, fact=fact) from exc


def _resolution_data(resolution: Resolution) -> dict[str, _TOML_VALUE]:
    """Represent a completed resolution without optional null fields."""
    data: dict[str, _TOML_VALUE] = {'paths': resolution.paths, 'action': resolution.action}
    if resolution.new_path is not None:
        data['new_path'] = resolution.new_path
    return data


def _replay_data(entry: ReplayEntry) -> dict[str, _TOML_VALUE]:
    """Represent replay provenance including distinct bookmark ownership."""
    data: dict[str, _TOML_VALUE] = {'old': entry.old, 'new': entry.new}
    if entry.original_source is not None:
        data['original_source'] = entry.original_source
    if entry.original_bookmark is not None:
        data['original_bookmark'] = _bookmark_data(entry.original_bookmark)
    if entry.source_bookmark is not None:
        data['source_bookmark'] = _bookmark_data(entry.source_bookmark)
    data['resolutions'] = [_resolution_data(item) for item in entry.resolutions]
    return data


def _scope(data: Mapping[str, object], error: str) -> Literal['one', 'all'] | None:
    """Read an optional supported replacement scope."""
    scope = _optional_string(data, 'replace_scope', error)
    if scope not in {None, 'one', 'all'}:
        raise PubanonError(error)
    return cast("Literal['one', 'all'] | None", scope)


def _shared_data(record: CandidateRecord | PublicationRecord) -> dict[str, _TOML_VALUE]:
    """Represent shared provenance in the existing serialized table order."""
    data: dict[str, _TOML_VALUE] = {}
    if record.pinned is not None:
        pinned: dict[str, _TOML_VALUE] = {
            'commit': record.pinned.commit,
            'selector_kind': record.pinned.selector_kind,
        }
        if record.pinned.selector_name is not None:
            pinned['selector_name'] = record.pinned.selector_name
        if record.pinned.selector_oid is not None:
            pinned['selector_oid'] = record.pinned.selector_oid
        data['pinned'] = pinned
    data['destination'] = {
        'adapter': record.destination.adapter,
        'owner': record.destination.owner,
        'repo': record.destination.repo,
        'visibility': record.destination.visibility,
        'account': record.destination.account,
    }
    if record.source_bookmark is not None:
        data['source_bookmark'] = _bookmark_data(record.source_bookmark)
    return data


def _string(data: Mapping[str, object], key: str, error: str) -> str:
    """Read a required TOML string."""
    value = data.get(key)
    if not isinstance(value, str):
        raise PubanonError(error)
    return value


def _strings(data: Mapping[str, object], key: str, error: str) -> tuple[str, ...]:
    """Read a strict array of TOML strings."""
    values = _array(data, key, error)
    if any(not isinstance(value, str) for value in values):
        raise PubanonError(error)
    return cast('tuple[str, ...]', tuple(values))


def _table(data: Mapping[str, object], key: str, error: str) -> dict[str, object]:
    """Read a required TOML table."""
    value = data.get(key)
    if not isinstance(value, dict):
        raise PubanonError(error)
    return cast('dict[str, object]', value)


def _destination(data: Mapping[str, object], error: str) -> RecordedDestination:
    """Read exact destination facts."""
    table = _table(data, 'destination', error)
    if table.keys() != _DESTINATION_KEYS:
        raise PubanonError(error)
    return RecordedDestination(
        *(
            _string(table, key, error)
            for key in ('adapter', 'owner', 'repo', 'visibility', 'account')
        )
    )


def _pinned(data: Mapping[str, object], error: str) -> PinnedSource:
    """Read exact source selector evidence."""
    table = _table(data, 'pinned', error)
    kind = _string(table, 'selector_kind', error)
    expected = (
        {'commit', 'selector_kind', 'selector_name', 'selector_oid'}
        if kind == 'ref'
        else {'commit', 'selector_kind'}
    )
    if table.keys() != expected:
        raise PubanonError(error)
    return PinnedSource(
        _string(table, 'commit', error),
        kind,
        _optional_string(table, 'selector_name', error),
        _optional_string(table, 'selector_oid', error),
    )


def _tables(data: Mapping[str, object], key: str, error: str) -> tuple[dict[str, object], ...]:
    """Read strict table entries from one required TOML array."""
    result: list[dict[str, object]] = []
    for item in _array(data, key, error):
        if not isinstance(item, dict):
            raise PubanonError(error)
        result.append(cast('dict[str, object]', item))
    return tuple(result)


def _conflicts(data: Mapping[str, object], error: str) -> tuple[ConflictEntry, ...]:
    """Read strict conflict groups and stage arrays."""
    conflicts: list[ConflictEntry] = []
    for table in _tables(data, 'conflicts', error):
        _keys(
            table, {'paths', 'conflict_type', 'message', 'decision_paths', 'stages'}, set(), error
        )
        stages: list[ConflictStage] = []
        for stage in _tables(table, 'stages', error):
            _keys(stage, {'path', 'mode', 'oid', 'stage'}, set(), error)
            stages.append(
                ConflictStage(
                    _string(stage, 'path', error),
                    _integer(stage, 'mode', error),
                    _string(stage, 'oid', error),
                    cast('Literal[1, 2, 3]', _integer(stage, 'stage', error)),
                )
            )
        conflicts.append(
            ConflictEntry(
                _strings(table, 'paths', error),
                _string(table, 'conflict_type', error),
                _string(table, 'message', error),
                tuple(stages),
                _strings(table, 'decision_paths', error),
            )
        )
    return tuple(conflicts)


def _resolutions(data: Mapping[str, object], error: str) -> tuple[Resolution, ...]:
    """Read exact completed resolution tables."""
    resolutions: list[Resolution] = []
    for table in _tables(data, 'resolutions', error):
        _keys(table, {'paths', 'action'}, {'new_path'}, error)
        action = _string(table, 'action', error)
        if action not in {'keep', 'delete', 'rename'}:
            raise PubanonError(error)
        resolutions.append(
            Resolution(
                _strings(table, 'paths', error),
                cast("Literal['keep', 'delete', 'rename']", action),
                _optional_string(table, 'new_path', error),
            )
        )
    return tuple(resolutions)


def _derivation(data: Mapping[str, object], error: str) -> Derivation | None:
    """Read optional derivation provenance without manufacturing source facts."""
    if 'derivation' not in data:
        return None
    table = _table(data, 'derivation', error)
    _keys(table, {'original', 'correction', 'resolutions'}, {'original_source'}, error)
    return Derivation(
        _string(table, 'original', error),
        _optional_string(table, 'original_source', error),
        _string(table, 'correction', error),
        _resolutions(table, error),
    )


def _toml(value: str, error: str) -> str:
    """Serialize one supported TOML string."""
    try:
        return toml_string(value)
    except ValueError as exc:
        raise PubanonError(error) from exc


def _toml_lines(data: dict[str, _TOML_VALUE], error: str, prefix: str = '') -> list[str]:
    """Serialize ordered scalar fields followed by nested tables and table arrays."""
    lines: list[str] = []
    tables: list[str] = []
    for key, value in data.items():
        name = key if re.fullmatch(r'[A-Za-z0-9_-]+', key) else _toml(key, error)
        if prefix in {'prestate_tags', 'poststate_tags'}:
            name = _toml(key, error)
        path = f'{prefix}.{name}' if prefix else name
        if isinstance(value, dict):
            tables.extend(('', f'[{path}]', *_toml_lines(value, error, path)))
        elif isinstance(value, list) and value:
            for entry in value:
                tables.extend(('', f'[[{path}]]', *_toml_lines(entry, error, path)))
        else:
            if isinstance(value, str):
                rendered = _toml(value, error)
            elif isinstance(value, datetime):
                rendered = value.isoformat().replace('+00:00', 'Z')
            elif isinstance(value, tuple):
                rendered = '[' + ', '.join(_toml(item, error) for item in value) + ']'
            elif isinstance(value, list):
                rendered = '[]'
            else:
                rendered = str(value)
            lines.append(f'{name} = {rendered}')
    return lines + tables


def _candidate_text(record: CandidateRecord) -> str:
    """Serialize the prepared snapshot record."""
    data: dict[str, _TOML_VALUE] = {
        'commit': record.commit,
        'tree': record.tree,
        'snapshot_digest': record.snapshot_digest,
        'fingerprint': record.fingerprint,
        'state': record.state,
        'message': record.message,
    }
    if record.tag_intent is not None:
        data['tag_intent'] = record.tag_intent
    if record.prestate_main is not None:
        data['prestate_main'] = record.prestate_main
    if record.replaces is not None:
        data['replaces'] = record.replaces
        data['replace_scope'] = cast('str', record.replace_scope)
    if record.tip is not None:
        data['tip'] = record.tip
        data['replayed'] = [_replay_data(entry) for entry in record.replayed]
    data.update(_shared_data(record))
    data['prestate_tags'] = dict(sorted(record.prestate_tags.items()))
    if record.poststate_tags is not None:
        data['poststate_tags'] = dict(sorted(record.poststate_tags.items()))
    return '\n'.join(_toml_lines(data, 'invalid snapshot record')) + '\n'


def _publication_text(record: PublicationRecord) -> str:
    """Serialize a confirmed publication without candidate state."""
    data: dict[str, _TOML_VALUE] = {
        'schema_version': 1,
        'commit': record.commit,
        'tree': record.tree,
        'snapshot_digest': record.snapshot_digest,
        'fingerprint': record.fingerprint,
        'message': record.message,
        'confirmed_at': record.confirmed_at,
    }
    if record.tag_intent is not None:
        data['tag_intent'] = record.tag_intent
    if record.replaces is not None:
        data['replaces'] = record.replaces
        data['replace_scope'] = cast('str', record.replace_scope)
    data.update(_shared_data(record))
    if record.derivation is not None:
        derivation = record.derivation
        derived: dict[str, _TOML_VALUE] = {
            'original': derivation.original,
            'correction': derivation.correction,
            'resolutions': [_resolution_data(item) for item in derivation.resolutions],
        }
        if derivation.original_source is not None:
            derived['original_source'] = derivation.original_source
        data['derivation'] = derived
    return '\n'.join(_toml_lines(data, 'invalid published snapshot record')) + '\n'


def _validate_bookmark(
    bookmark: SourceBookmark | None, project: str, commit: str, error: str
) -> None:
    """Require absolute source paths and this record's private reference."""
    if bookmark is None:
        return
    if (
        not bookmark.source_path.is_absolute()
        or not bookmark.git_common_dir.is_absolute()
        or bookmark.refname != bookmark_ref(project, commit)
    ):
        raise PubanonError(error)


def _bookmark(
    data: Mapping[str, object],
    project: str,
    commit: str,
    error: str,
    key: str = 'source_bookmark',
) -> SourceBookmark | None:
    """Read an optional exact source preservation locator."""
    if key not in data:
        return None
    table = _table(data, key, error)
    if table.keys() != _BOOKMARK_KEYS:
        raise PubanonError(error)
    bookmark = SourceBookmark(
        Path(_string(table, 'source_path', error)),
        Path(_string(table, 'git_common_dir', error)),
        _string(table, 'refname', error),
    )
    _validate_bookmark(bookmark, project, commit, error)
    return bookmark


def _replayed(data: Mapping[str, object], project: str, error: str) -> tuple[ReplayEntry, ...]:
    """Read exact replay mappings and their nested source locators."""
    entries: list[ReplayEntry] = []
    for table in _tables(data, 'replayed', error):
        _keys(
            table,
            {'old', 'new', 'resolutions'},
            {'original_source', 'original_bookmark', 'source_bookmark'},
            error,
        )
        old, new = _string(table, 'old', error), _string(table, 'new', error)
        entries.append(
            ReplayEntry(
                old,
                new,
                _optional_string(table, 'original_source', error),
                _bookmark(table, project, old, error, 'original_bookmark'),
                _bookmark(table, project, new, error),
                _resolutions(table, error),
            )
        )
    return tuple(entries)


def _validate_path(path: str, error: str) -> None:
    """Reject traversal and repository metadata paths in durable file decisions."""
    if (
        not path
        or '\0' in path
        or path.startswith('/')
        or any(part in {'', '.', '..'} for part in path.split('/'))
        or '.git' in path_portability_keys(path)
    ):
        raise PubanonError(error)


def _validate_conflict(conflict: ConflictEntry, error: str) -> None:
    """Require safe diagnostic paths, frozen decisions, and valid unmerged stages."""
    if (
        not conflict.paths
        or not conflict.conflict_type
        or not conflict.message
        or not conflict.decision_paths
        or len(set(conflict.decision_paths)) != len(conflict.decision_paths)
    ):
        raise PubanonError(error)
    for path in (*conflict.paths, *conflict.decision_paths):
        _validate_path(path, error)
    seen: set[tuple[str, int]] = set()
    for stage in conflict.stages:
        _validate_path(stage.path, error)
        key = stage.path, stage.stage
        if (
            type(stage.mode) is not int
            or stage.mode not in _VALID_MODES
            or type(stage.stage) is not int
            or stage.stage not in {1, 2, 3}
            or _OID_PATTERN.fullmatch(stage.oid) is None
            or key in seen
        ):
            raise PubanonError(error)
        seen.add(key)


def _validate_pinned(pinned: PinnedSource | None, error: str) -> None:
    """Require a valid source OID and the exact supported selector form."""
    if pinned is None:
        raise PubanonError(error)
    selector_valid = (
        pinned.selector_kind == 'ref'
        and pinned.selector_name is not None
        and pinned.selector_name.startswith('refs/')
        and pinned.selector_oid is not None
        and _OID_PATTERN.fullmatch(pinned.selector_oid) is not None
    ) or (
        pinned.selector_kind == 'explicit'
        and pinned.selector_name is None
        and pinned.selector_oid is None
    )
    if not selector_valid or _OID_PATTERN.fullmatch(pinned.commit) is None:
        raise PubanonError(error)


def _validate_replacement(
    replaces: str | None, scope: str | None, commit: str, error: str
) -> None:
    """Require the paired replacement selector and supported scope."""
    if replaces is None:
        if scope is not None:
            raise PubanonError(error)
    elif (
        _OID_PATTERN.fullmatch(replaces) is None
        or replaces == commit
        or scope not in {'one', 'all'}
    ):
        raise PubanonError(error)


def _validate_common(
    record: CandidateRecord | PublicationRecord, project: str, error: str
) -> None:
    """Validate shared provenance and destination fields."""
    try:
        validate_host_segment('owner', record.destination.owner)
        validate_host_segment('repo', record.destination.repo)
    except PubanonError as exc:
        raise PubanonError(error) from exc
    if record.pinned is not None:
        _validate_pinned(record.pinned, error)
    _validate_replacement(record.replaces, record.replace_scope, record.commit, error)
    invalid = (
        any(_OID_PATTERN.fullmatch(oid) is None for oid in (record.commit, record.tree))
        or any(
            re.fullmatch(r'[0-9a-f]{64}', digest) is None
            for digest in (record.snapshot_digest, record.fingerprint)
        )
        or (
            record.tag_intent is not None
            and (
                not record.tag_intent.startswith('v')
                or _VERSION_PATTERN.fullmatch(record.tag_intent[1:]) is None
            )
        )
        or not record.message
        or '\0' in record.message
        or not all(
            isinstance(value, str) and value for value in record.destination.__dict__.values()
        )
    )
    if invalid:
        raise PubanonError(error)
    _validate_bookmark(record.source_bookmark, project, record.commit, error)


def _validate_resolutions(resolutions: tuple[Resolution, ...], error: str) -> None:
    """Require unambiguous completed actions for individual safe paths."""
    seen: dict[str, Resolution] = {}
    for resolution in resolutions:
        if len(resolution.paths) != 1 or resolution.action not in {'keep', 'delete', 'rename'}:
            raise PubanonError(error)
        path = resolution.paths[0]
        _validate_path(path, error)
        if resolution.action == 'rename':
            if resolution.new_path is None or resolution.new_path == path:
                raise PubanonError(error)
            _validate_path(resolution.new_path, error)
        elif resolution.new_path is not None:
            raise PubanonError(error)
        if path in seen and seen[path] != resolution:
            raise PubanonError(error)
        seen[path] = resolution


def _validate_publication(record: PublicationRecord, project: str) -> None:
    """Validate a confirmed record's provenance form and UTC timestamp."""
    error = 'invalid published snapshot record'
    _validate_common(record, project, error)
    if record.confirmed_at.tzinfo is None or record.confirmed_at.utcoffset() != timedelta(0):
        raise PubanonError(error)
    if (record.pinned is None) == (record.derivation is None):
        raise PubanonError(error)
    if record.pinned is not None and record.source_bookmark is None:
        raise PubanonError(error)
    if record.derivation is not None:
        derivation = record.derivation
        if (
            record.replaces is not None
            or any(
                oid is not None and _OID_PATTERN.fullmatch(oid) is None
                for oid in (derivation.original, derivation.correction, derivation.original_source)
            )
            or record.commit in {derivation.original, derivation.correction}
        ):
            raise PubanonError(error)
        if derivation.original_source is None and record.source_bookmark is not None:
            raise PubanonError(error)
        _validate_resolutions(derivation.resolutions, error)


def _validate_replayed(entries: tuple[ReplayEntry, ...], project: str, error: str) -> None:
    """Require unique valid mappings and independently owned bookmark locators."""
    if len({entry.old for entry in entries}) != len(entries) or len(
        {entry.new for entry in entries}
    ) != len(entries):
        raise PubanonError(error)
    for entry in entries:
        for oid in (entry.old, entry.new, entry.original_source):
            if oid is not None and _OID_PATTERN.fullmatch(oid) is None:
                raise PubanonError(error)
        if entry.old == entry.new or (
            entry.original_source is None
            and (entry.original_bookmark is not None or entry.source_bookmark is not None)
        ):
            raise PubanonError(error)
        _validate_bookmark(entry.original_bookmark, project, entry.old, error)
        _validate_bookmark(entry.source_bookmark, project, entry.new, error)
        _validate_resolutions(entry.resolutions, error)


def _validate_rewrite(record: RewriteRecord, project: str) -> None:
    """Validate frozen replay order and phase/pause relationships without Git."""
    error = 'invalid rewrite record'
    if any(
        _OID_PATTERN.fullmatch(oid) is None
        for oid in (record.correction, record.replaces, record.prestate_main, *record.descendants)
    ):
        raise PubanonError(error)
    if (
        record.correction == record.replaces
        or record.replaces in record.descendants
        or record.correction in record.descendants
        or len(set(record.descendants)) != len(record.descendants)
        or record.prestate_main
        != (record.descendants[-1] if record.descendants else record.replaces)
        or record.phase not in {'replaying', 'exporting', 'awaiting'}
    ):
        raise PubanonError(error)
    _validate_replayed(record.replayed, project, error)
    if tuple(entry.old for entry in record.replayed) != record.descendants[
        : len(record.replayed)
    ] or any(entry.new == record.correction for entry in record.replayed):
        raise PubanonError(error)
    if record.phase == 'replaying':
        if record.pause is not None:
            raise PubanonError(error)
        return
    pause = record.pause
    if (
        pause is None
        or len(record.replayed) >= len(record.descendants)
        or pause.old != record.descendants[len(record.replayed)]
        or _OID_PATTERN.fullmatch(pause.tree) is None
        or not pause.conflicts
    ):
        raise PubanonError(error)
    for conflict in pause.conflicts:
        _validate_conflict(conflict, error)


def _validate_tags(tags: dict[str, str], error: str) -> None:
    """Require supported retained tag names and full object IDs."""
    if any(
        not tag
        or re.search(r'[\x00-\x20\x7f~^:?*\[\\]', tag) is not None
        or _OID_PATTERN.fullmatch(oid) is None
        for tag, oid in tags.items()
    ):
        raise PubanonError(error)


def _write(
    path: Path, content: str, error: str, saved_error: str, *, fact: str | None = None
) -> None:
    """Durably replace one private record through a same-directory temporary."""
    try:
        _filesystem.write_private(path, content.encode('utf-8'))
    except UnicodeError as exc:
        raise PubanonError(error, fact=fact) from exc
    except _filesystem.DurableWriteError as exc:
        if exc.interrupted and not exc.replacement_attempted:
            raise KeyboardInterrupt from exc.__cause__
        message = (
            saved_error
            if exc.replaced or (exc.interrupted and exc.replacement_attempted)
            else error
        )
        raise PubanonError(message, fact=fact) from exc.__cause__


def candidate_tip(record: CandidateRecord) -> str:
    """Return the publishable tip, refusing an incomplete replacement.

    Raises:
        PubanonError: A replacement lacks its complete retained poststate.
    """
    if record.replaces is None:
        return record.commit
    if record.tip is None or record.poststate_tags is None:
        raise PubanonError(
            'replacement snapshot is incomplete',
            recovery='run pubanon prepare --continue or pubanon discard',
        )
    return record.tip


def candidate_poststate(record: CandidateRecord) -> tuple[str, dict[str, str]]:
    """Return the publishable main OID and an independently owned complete tag map.

    Raises:
        PubanonError: A replacement lacks its complete retained poststate.
    """
    tip = candidate_tip(record)
    if record.replaces is not None:
        return tip, dict(cast('dict[str, str]', record.poststate_tags))
    tags = dict(record.prestate_tags)
    if record.tag_intent is not None:
        tags[record.tag_intent] = record.commit
    return tip, tags


def project_dir(paths: Paths, project: str) -> Path:
    """Resolve a validated project data directory.

    Raises:
        PubanonError: The project name is invalid.
    """
    require_project_name(project)
    return paths.data_dir / 'projects' / project


def candidate_path(paths: Paths, project: str) -> Path:
    """Return the active snapshot record path."""
    return project_dir(paths, project) / 'candidate.toml'


def deleted_publications(paths: Paths, project: str) -> set[str]:
    """Read local deletions that must not be rediscovered through Git history."""
    directory = project_dir(paths, project) / 'publications'
    return {
        path.stem
        for path in _publication_inventory(
            directory, '.deleted', 'cannot list local snapshot deletions'
        )
        if _OID_PATTERN.fullmatch(path.stem) is not None
    }


def publication_path(paths: Paths, project: str, commit: str) -> Path:
    """Return a confirmed record path keyed by its full commit ID."""
    path = project_dir(paths, project) / 'publications' / f'{commit}.toml'
    if _OID_PATTERN.fullmatch(commit) is None:
        raise PubanonError('invalid published snapshot commit ID', fact=escape(path_text(path)))
    return path


def load_publication(paths: Paths, project: str, commit: str) -> PublicationRecord | None:
    """Load a confirmed record keyed by its full commit ID.

    Returns:
        The validated record, or `None` when it is absent.

    Raises:
        PubanonError: The record has malformed or unsupported schema or
            provenance, or its commit does not match its filename.
    """
    error = 'invalid published snapshot record'
    path = publication_path(paths, project, commit)
    data = _read(path, error, fact=escape(path_text(path)))
    if data is None:
        return None
    version = data.get('schema_version')
    if type(version) is not int or version != 1:
        raise PubanonError('unsupported published snapshot record schema version', fact=commit)
    if data.keys() - _PUBLICATION_KEYS or _PUBLICATION_REQUIRED - data.keys():
        raise PubanonError(error, fact=commit)
    confirmed_at = data.get('confirmed_at')
    if not isinstance(confirmed_at, datetime):
        raise PubanonError(error, fact=commit)
    record = PublicationRecord(
        commit=_string(data, 'commit', error),
        tree=_string(data, 'tree', error),
        snapshot_digest=_string(data, 'snapshot_digest', error),
        fingerprint=_string(data, 'fingerprint', error),
        message=_string(data, 'message', error),
        tag_intent=_optional_string(data, 'tag_intent', error),
        pinned=_pinned(data, error) if 'pinned' in data else None,
        destination=_destination(data, error),
        confirmed_at=confirmed_at,
        source_bookmark=_bookmark(data, project, commit, error),
        replaces=_optional_string(data, 'replaces', error),
        replace_scope=_scope(data, error),
        derivation=_derivation(data, error),
    )
    _validate_publication(record, project)
    record = replace(record, confirmed_at=record.confirmed_at.astimezone(UTC))
    if record.commit != commit:
        raise PubanonError('published snapshot record filename does not match commit', fact=commit)
    return record


def list_publications(paths: Paths, project: str) -> tuple[PublicationRecord, ...]:
    """List every valid confirmed record without treating temporary files as entries.

    Raises:
        PubanonError: An entry is invalid or disappears during the listing. No
            partial result is returned.
    """
    directory = project_dir(paths, project) / 'publications'
    paths_found = sorted(
        _publication_inventory(directory, '.toml', 'cannot list published snapshot records')
    )
    records: list[PublicationRecord] = []
    for path in paths_found:
        record = load_publication(paths, project, path.stem)
        if record is None:
            raise PubanonError(
                'published snapshot record disappeared while listing', fact=path.name
            )
        records.append(record)
    return tuple(records)


def mark_publication_deleted(paths: Paths, project: str, commit: str) -> None:
    """Persist a deletion without retaining Git objects or source provenance.

    The caller must hold the project lock for the complete operation.
    """
    path = publication_path(paths, project, commit).with_suffix('.deleted')
    try:
        create_data_dirs(paths, project)
        path.parent.mkdir(mode=0o700, exist_ok=True)
    except OSError as exc:
        raise PubanonError('cannot save local snapshot deletion', fact=commit) from exc
    _write(
        path,
        '',
        'cannot save local snapshot deletion',
        'local snapshot deletion may have been saved',
        fact=commit,
    )


def publication_ref(commit: str) -> str:
    """Return the publication-store reference protecting one commit."""
    if _OID_PATTERN.fullmatch(commit) is None:
        raise PubanonError('invalid published snapshot commit ID')
    return f'refs/pubanon/published/{commit}'


def publication_source(record: PublicationRecord) -> str | None:
    """Return pinned source provenance or the original input of a derivation.

    Returns:
        The recorded source commit, or `None` when no source input is known.
    """
    if record.pinned is not None:
        return record.pinned.commit
    return record.derivation.original_source if record.derivation is not None else None


def relocate_publication_source(
    paths: Paths, project: str, expected: PublicationRecord, bookmark: SourceBookmark
) -> PublicationRecord:
    """Durably replace only a verified source locator before local deletion.

    Callers hold the project lock and verify the exact bookmark and source OID
    at the relocated repository. All provenance and confirmation time remain
    unchanged; a changed or missing prior record refuses the replacement.
    """
    if expected.source_bookmark is None or bookmark.refname != expected.source_bookmark.refname:
        raise PubanonError('relocated source bookmark conflicts with publication')
    record = replace(expected, source_bookmark=bookmark)
    _validate_publication(record, project)
    if load_publication(paths, project, expected.commit) != expected:
        raise PubanonError('published snapshot record changed before source relocation')
    path = publication_path(paths, project, expected.commit)
    _write(
        path,
        _publication_text(record),
        'cannot save relocated publication source',
        'relocated publication source may have been saved',
        fact=escape(path_text(path)),
    )
    return record


def replacement_poststate_tags(
    record: CandidateRecord, replayed: Sequence[ReplayEntry]
) -> dict[str, str]:
    """Derive resulting tags from a replacement and its completed replay entries.

    Args:
        record: Replacement candidate supplying the frozen tag map and intent.
        replayed: Completed old-to-new mappings, including partial rewrite
            progress.

    Returns:
        A new tag mapping that preserves unaffected tags for a single
        replacement, or contains only the intended tag when replacing all
        history.
    """
    if record.replace_scope == 'all':
        return {} if record.tag_intent is None else {record.tag_intent: record.commit}
    mapping = {record.replaces: record.commit}
    mapping.update((entry.old, entry.new) for entry in replayed)
    tags = {tag: mapping.get(oid, oid) for tag, oid in record.prestate_tags.items()}
    if record.replaces not in record.prestate_tags.values() and record.tag_intent is not None:
        tags[record.tag_intent] = record.commit
    return tags


def _validate_candidate(record: CandidateRecord, project: str) -> None:
    """Validate complete or interrupted candidate state without consulting Git."""
    error = 'invalid snapshot record'
    _validate_common(record, project, error)
    _validate_pinned(record.pinned, error)
    if record.state not in _VALID_STATES:
        raise PubanonError(error)
    if record.prestate_main is not None and _OID_PATTERN.fullmatch(record.prestate_main) is None:
        raise PubanonError(error)
    _validate_tags(record.prestate_tags, error)
    if record.replaces is None:
        if record.tip is not None or record.poststate_tags is not None or record.replayed:
            raise PubanonError(error)
        return
    if record.prestate_main is None:
        raise PubanonError(error)
    if record.replace_scope == 'all' and record.replaces != record.prestate_main:
        raise PubanonError(error)
    if record.tip is None and record.poststate_tags is None and not record.replayed:
        return
    if record.tip is None or record.poststate_tags is None:
        raise PubanonError(error)
    _validate_replayed(record.replayed, project, error)
    _validate_tags(record.poststate_tags, error)
    if (
        record.tip != (record.replayed[-1].new if record.replayed else record.commit)
        or record.poststate_tags != replacement_poststate_tags(record, record.replayed)
        or (record.replace_scope == 'all' and record.replayed)
        or any(
            entry.old == record.replaces or entry.new == record.commit for entry in record.replayed
        )
    ):
        raise PubanonError(error)


def load_candidate(paths: Paths, project: str) -> CandidateRecord | None:
    """Load one strict snapshot record if present.

    Returns:
        The validated record, or `None` when it is absent.

    Raises:
        PubanonError: The stored schema or candidate state is invalid.
    """
    error = 'invalid snapshot record'
    data = _read(candidate_path(paths, project), error)
    if data is None:
        return None
    if data.keys() - _CANDIDATE_KEYS or _CANDIDATE_REQUIRED - data.keys():
        raise PubanonError(error)
    tags = _table(data, 'prestate_tags', error)
    complete_keys = {'tip', 'poststate_tags', 'replayed'}
    if complete_keys & data.keys() and not complete_keys <= data.keys():
        raise PubanonError(error)
    poststate = _table(data, 'poststate_tags', error) if 'poststate_tags' in data else None
    record = CandidateRecord(
        commit=_string(data, 'commit', error),
        tree=_string(data, 'tree', error),
        snapshot_digest=_string(data, 'snapshot_digest', error),
        tag_intent=_optional_string(data, 'tag_intent', error),
        fingerprint=_string(data, 'fingerprint', error),
        pinned=_pinned(data, error),
        destination=_destination(data, error),
        prestate_main=_optional_string(data, 'prestate_main', error),
        prestate_tags={tag: _string(tags, tag, error) for tag in tags},
        state=_string(data, 'state', error),
        message=_string(data, 'message', error),
        source_bookmark=_bookmark(data, project, _string(data, 'commit', error), error),
        replaces=_optional_string(data, 'replaces', error),
        replace_scope=_scope(data, error),
        tip=_optional_string(data, 'tip', error),
        poststate_tags=None
        if poststate is None
        else {tag: _string(poststate, tag, error) for tag in poststate},
        replayed=_replayed(data, project, error) if 'replayed' in data else (),
    )
    _validate_candidate(record, project)
    return record


def rewrite_directory(paths: Paths, project: str) -> Path:
    """Return the private conflict export directory without creating it."""
    return project_dir(paths, project) / 'rewrite'


def rewrite_path(paths: Paths, project: str) -> Path:
    """Return the private durable rewrite record path."""
    return project_dir(paths, project) / 'rewrite.toml'


def load_rewrite(paths: Paths, project: str) -> RewriteRecord | None:
    """Load strict frozen rewrite progress if present.

    Returns:
        The validated progress, or `None` when it is absent.

    Raises:
        PubanonError: Progress is invalid, including inconsistent phase and
            pause relationships.
    """
    error = 'invalid rewrite record'
    data = _read(rewrite_path(paths, project), error)
    if data is None:
        return None
    _keys(data, set(_REWRITE_KEYS - {'pause'}), {'pause'}, error)
    pause = None
    if 'pause' in data:
        table = _table(data, 'pause', error)
        _keys(table, {'old', 'tree', 'conflicts'}, set(), error)
        pause = RewritePause(
            _string(table, 'old', error), _string(table, 'tree', error), _conflicts(table, error)
        )
    phase = _string(data, 'phase', error)
    if phase not in {'replaying', 'exporting', 'awaiting'}:
        raise PubanonError(error)
    record = RewriteRecord(
        _string(data, 'correction', error),
        _string(data, 'replaces', error),
        _string(data, 'prestate_main', error),
        _strings(data, 'descendants', error),
        cast("Literal['replaying', 'exporting', 'awaiting']", phase),
        _replayed(data, project, error),
        pause,
    )
    _validate_rewrite(record, project)
    return record


def save_candidate(paths: Paths, project: str, record: CandidateRecord) -> None:
    """Save a validated snapshot record with owner-only POSIX permissions.

    The caller must hold the project lock for the complete operation.

    Raises:
        PubanonError: The candidate state is invalid or cannot be persisted.
    """
    _validate_candidate(record, project)
    content = _candidate_text(record)
    create_data_dirs(paths, project)
    _write(
        candidate_path(paths, project),
        content,
        'cannot save snapshot record',
        'snapshot record may have been saved',
    )


def save_publication(paths: Paths, project: str, record: PublicationRecord) -> None:
    """Durably save or verify one immutable confirmed record.

    The caller must hold the project lock for the complete operation. An
    idempotent retry preserves the existing record's first `confirmed_at`
    value. Any other provenance difference is refused.

    Raises:
        PubanonError: The record is invalid, conflicts with confirmed
            provenance, or cannot be persisted.
    """
    _validate_publication(record, project)
    path = publication_path(paths, project, record.commit)
    existing = load_publication(paths, project, record.commit)
    if existing is not None:
        if existing != replace(record, confirmed_at=existing.confirmed_at):
            raise PubanonError(
                'published snapshot record conflicts with confirmed provenance', fact=record.commit
            )
        return
    content = _publication_text(record)
    create_data_dirs(paths, project)
    try:
        path.parent.mkdir(mode=0o700, exist_ok=True)
        _filesystem.chmod(path.parent, 0o700)
    except OSError as exc:
        raise PubanonError('cannot create published snapshot record directory') from exc
    _write(
        path,
        content,
        'cannot save published snapshot record',
        'published snapshot record may have been saved',
        fact=escape(path_text(path)),
    )


def save_rewrite(paths: Paths, project: str, record: RewriteRecord) -> None:
    """Atomically save validated rewrite progress with owner-only POSIX permissions.

    The caller must hold the project lock for the complete operation.

    Raises:
        PubanonError: Progress is invalid or cannot be persisted.
    """
    _validate_rewrite(record, project)
    data: dict[str, _TOML_VALUE] = {
        'correction': record.correction,
        'replaces': record.replaces,
        'prestate_main': record.prestate_main,
        'descendants': record.descendants,
        'phase': record.phase,
        'replayed': [_replay_data(entry) for entry in record.replayed],
    }
    if record.pause is not None:
        data['pause'] = {
            'old': record.pause.old,
            'tree': record.pause.tree,
            'conflicts': [_conflict_data(item) for item in record.pause.conflicts],
        }
    content = '\n'.join(_toml_lines(data, 'invalid rewrite record')) + '\n'
    create_data_dirs(paths, project)
    _write(
        rewrite_path(paths, project),
        content,
        'cannot save rewrite record',
        'rewrite record may have been saved',
    )
