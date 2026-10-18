"""Build and retain one publication candidate."""

# pyright: reportPrivateUsage=false

import os
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Literal

from pubanon._config import (
    GlobalConfig,
    Paths,
    ProjectConfig,
    assemble_rules,
    commit_message_template,
    derived_private_terms,
    fingerprint,
    identity_refusal,
    identity_rule_sources,
)
from pubanon._errors import PubanonError
from pubanon._gate import gate_unit
from pubanon._run import OID_HEX, git_failure_fact, run_git
from pubanon._snapshots import _inspection, _records, _store
from pubanon._snapshots._records import (
    STATE_RETAINED,
    STATE_UNKNOWN_OUTCOME,
    CandidateRecord,
    RecordedDestination,
    save_candidate,
)
from pubanon._snapshots._source import (
    PinnedSource,
    Snapshot,
    read_snapshot,
    snapshot_digest,
)
from pubanon._terminal import (
    output,
)

CANDIDATE_REF = 'refs/pubanon/candidate'
REWRITE_MERGED_REF = 'refs/pubanon/rewrite/merged'
_OID_PATTERN = re.compile(OID_HEX)
_VERSION_PATTERN = re.compile(r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)')


@dataclass(frozen=True)
class CandidateInputs:
    """Group the source facts used to build one candidate.

    The first four positional constructor inputs provide the exact source
    snapshot, pinned selector, optional release version, and optional commit
    message. The version is omitted or has strict `X.Y.Z` form. Callers select
    the source and apply all path and blob gates before construction; these
    inputs do not establish complete host or publication acceptance.

    Replacement selection and scope are supplied together or both omitted.
    Omitting `prestate` makes construction read the current advisory-store main
    and tags.

    Attributes:
        prestate: Optional published main and tags captured before
            construction.
        replaces: Resolved old publication commit to correct.
        replace_scope: Correct one publication or replace all history.
    """

    _snapshot: Snapshot
    _pinned: PinnedSource
    _version: str | None
    _message: str | None
    prestate: tuple[str | None, dict[str, str]] | None = None
    replaces: str | None = None
    replace_scope: Literal['one', 'all'] | None = None


@dataclass(frozen=True)
class _BuildMetadata:
    """Hold resolved construction metadata without consulting saved provenance."""

    _parents: tuple[str, ...]
    _descendants: tuple[str, ...]
    _old_tree: str | None
    _old_message: str | None
    _tags: tuple[str, ...]
    _version: str | None
    _message: str | None


def _complete_unreplayed(record: CandidateRecord, metadata: _BuildMetadata) -> CandidateRecord:
    """Complete all-history and latest-publication corrections without replay."""
    if record.replaces is None or (record.replace_scope != 'all' and metadata._descendants):
        return record
    tags = dict(record.prestate_tags) if record.replace_scope == 'one' else {}
    for tag in metadata._tags:
        tags[tag] = record.commit
    if record.tag_intent is not None:
        tags[record.tag_intent] = record.commit
    return replace(record, tip=record.commit, poststate_tags=tags)


def _oid(output: bytes, failure: str) -> str:
    """Parse one object ID without exposing malformed command output."""
    if not output.endswith(b'\n') or b'\n' in output[:-1]:
        raise PubanonError(failure)
    try:
        oid = output[:-1].decode('ascii')
    except UnicodeDecodeError as exc:
        raise PubanonError(failure) from exc
    if _OID_PATTERN.fullmatch(oid) is None:
        raise PubanonError(failure)
    return oid


def _oids(output: bytes, count: int, failure: str) -> list[str]:
    """Parse an exact number of object ID response lines."""
    lines = output.splitlines(keepends=True)
    if len(lines) != count:
        raise PubanonError(failure)
    return [_oid(line, failure) for line in lines]


def _replacement_parent_graph(store: Path, main: str) -> dict[str, tuple[str, ...]]:
    """Read the parent graph once without requiring all earlier history to be linear."""
    result = run_git(
        ['--git-dir', str(store.absolute()), 'rev-list', '--parents', main],
        cwd=store,
        check=False,
    )
    if result.returncode != 0:
        raise PubanonError('cannot read local publication history', fact=git_failure_fact(result))
    graph: dict[str, tuple[str, ...]] = {}
    for line in result.stdout.splitlines():
        fields = line.split()
        if not fields or any(re.fullmatch(OID_HEX.encode(), field) is None for field in fields):
            raise PubanonError('Git returned invalid publication history')
        oid, *parents = (field.decode('ascii') for field in fields)
        if oid in graph:
            raise PubanonError('Git returned invalid publication history')
        graph[oid] = tuple(parents)
    return graph


def git_result(
    args: Sequence[str],
    *,
    store: Path,
    failure: str,
    input_bytes: bytes | None = None,
    env_extra: Mapping[str, str] | None = None,
) -> bytes:
    """Run candidate plumbing and retain escaped failure diagnostics.

    Args:
        args: Git arguments after the candidate store selection.
        store: Bare candidate store for the command.
        failure: Public failure summary for an unsuccessful command.
        input_bytes: Optional standard input passed to Git.
        env_extra: Additional environment entries passed to Git.

    Returns:
        Standard output bytes from a successful command.

    Raises:
        PubanonError: Git reports an unsuccessful command.
    """
    result = run_git(
        ['--git-dir', str(store.absolute()), *args],
        cwd=store,
        input_bytes=input_bytes,
        env_extra=env_extra,
        check=False,
    )
    if result.returncode != 0:
        raise PubanonError(failure, fact=git_failure_fact(result))
    return result.stdout


def _read_tags(store: Path) -> dict[str, str]:
    """Read the complete advisory store tag set."""
    output = git_result(
        ['for-each-ref', '--format=%(refname:strip=2) %(objectname)', 'refs/tags'],
        store=store,
        failure='cannot read snapshot store tags',
    )
    tags: dict[str, str] = {}
    for line in output.splitlines():
        raw_tag, separator, raw_oid = line.rpartition(b' ')
        try:
            tag = raw_tag.decode('utf-8')
            oid = raw_oid.decode('ascii')
        except UnicodeDecodeError as exc:
            raise PubanonError('Git returned invalid snapshot store tags') from exc
        if not separator or not tag or tag in tags or _OID_PATTERN.fullmatch(oid) is None:
            raise PubanonError('Git returned invalid snapshot store tags')
        tags[tag] = oid
    return tags


def _write_blobs(store: Path, snapshot: Snapshot) -> list[str]:
    """Write all candidate blobs through one import stream."""
    if not snapshot:
        return []
    stream = bytearray(b'feature get-mark\nfeature done\n')
    for mark, entry in enumerate(snapshot, start=1):
        stream.extend(f'blob\nmark :{mark}\ndata {len(entry.data)}\n'.encode())
        stream.extend(entry.data)
        stream.extend(f'\nget-mark :{mark}\n'.encode())
    stream.extend(b'done\n')
    output = git_result(
        ['fast-import', '--quiet'],
        store=store,
        failure='cannot write snapshot blob',
        input_bytes=bytes(stream),
    )
    return _oids(output, len(snapshot), 'Git returned an invalid snapshot blob ID')


def _write_trees(store: Path, contents: dict[str, list[tuple[bytes, bytes]]]) -> str:
    """Write candidate trees in one batch per directory depth."""
    root_oid: str | None = None
    depths = sorted(
        {directory.count('/') + bool(directory) for directory in contents},
        reverse=True,
    )
    for depth in depths:
        directories = sorted(
            (
                directory
                for directory in contents
                if directory.count('/') + bool(directory) == depth
            ),
            key=lambda item: item.encode('utf-8'),
        )
        tree_input = bytearray()
        for directory in directories:
            tree_input.extend(b''.join(record for _name, record in sorted(contents[directory])))
            tree_input.extend(b'\0')
        tree_oids = _oids(
            git_result(
                ['mktree', '-z', '--batch'],
                store=store,
                failure='cannot write snapshot tree',
                input_bytes=bytes(tree_input),
            ),
            len(directories),
            'Git returned an invalid snapshot tree ID',
        )
        for directory, tree_oid in zip(directories, tree_oids, strict=True):
            if directory:
                parent, _, name = directory.rpartition('/')
                raw_name = name.encode('utf-8')
                contents[parent].append(
                    (
                        raw_name,
                        b'040000 tree ' + tree_oid.encode() + b'\t' + raw_name + b'\0',
                    )
                )
            else:
                root_oid = tree_oid
    if root_oid is None:
        raise PubanonError('cannot write snapshot root tree')
    return root_oid


def _write_tree(store: Path, snapshot: Snapshot) -> str:
    """Write snapshot blobs and deterministic bottom-up trees."""
    sorted_snapshot = tuple(sorted(snapshot, key=lambda item: item.path.encode('utf-8')))
    _inspection.validate_tree_paths(sorted_snapshot)
    contents: dict[str, list[tuple[bytes, bytes]]] = {'': []}
    blob_oids = _write_blobs(store, sorted_snapshot)
    for entry, oid in zip(sorted_snapshot, blob_oids, strict=True):
        parent = PurePosixPath(entry.path).parent
        while str(parent) != '.':
            contents.setdefault(parent.as_posix(), [])
            parent = parent.parent
        directory, _, name = entry.path.rpartition('/')
        raw_name = name.encode('utf-8')
        contents[directory].append(
            (raw_name, f'{entry.mode:o} blob {oid}\t'.encode() + raw_name + b'\0')
        )
    return _write_trees(store, contents)


def outcome_unknown(record: CandidateRecord | None) -> bool:
    """Return whether a retained candidate needs publication outcome resolution."""
    return record is not None and record.state == STATE_UNKNOWN_OUTCOME


def parse_commit_fields(raw: bytes) -> tuple[str, tuple[str, ...], bytes, bytes]:
    """Parse immutable tree, parents, author date, and message from one commit.

    Args:
        raw: Unmodified commit object bytes.

    Returns:
        The tree ID, ordered parent IDs, author timestamp and timezone bytes,
        and exact commit message bytes.

    Raises:
        PubanonError: Required metadata is malformed or missing.
    """
    headers, separator, message = raw.partition(b'\n\n')
    lines = headers.split(b'\n')
    trees = [line[5:] for line in lines if line.startswith(b'tree ')]
    authors = [line.rsplit(b' ', 2)[-2:] for line in lines if line.startswith(b'author ')]
    raw_parents = [line[7:] for line in lines if line.startswith(b'parent ')]
    if not separator or len(trees) != 1 or len(authors) != 1:
        raise PubanonError('replacement commit metadata is invalid')
    try:
        tree = trees[0].decode('ascii')
        parents = tuple(parent.decode('ascii') for parent in raw_parents)
    except UnicodeDecodeError as exc:
        raise PubanonError('replacement commit metadata is invalid') from exc
    if any(_OID_PATTERN.fullmatch(oid) is None for oid in (tree, *parents)):
        raise PubanonError('replacement commit metadata is invalid')
    return tree, parents, b' '.join(authors[0]), message


def commit_fields(store: Path, commit: str) -> tuple[str, tuple[str, ...], bytes, bytes]:
    """Read immutable tree, parents, author date, and message from one commit.

    Args:
        store: Bare candidate store containing the commit.
        commit: Commit object ID to read.

    Returns:
        The tree ID, ordered parent IDs, author timestamp and timezone bytes,
        and exact commit message bytes.
    """
    return parse_commit_fields(
        git_result(
            ['cat-file', 'commit', commit],
            store=store,
            failure='replacement commit is unavailable',
        )
    )


def publication_archive(paths: Paths, project: str, commit: str) -> bool:
    """Verify an owned direct archive ref or report its absence.

    Conflicting or symbolic references are refused; similarly prefixed child
    references are not evidence for the exact publication.
    """
    store = _store.candidate_store_path(paths, project)
    if not store.exists():
        return False
    ref = _records.publication_ref(commit)
    symbolic = run_git(
        ['--git-dir', str(store.absolute()), 'symbolic-ref', '--quiet', ref],
        cwd=store,
        check=False,
    )
    if symbolic.returncode != 1:
        raise PubanonError('publication archive reference is not a direct ref', fact=ref)
    refs = run_git(
        [
            '--git-dir',
            str(store.absolute()),
            'for-each-ref',
            '--format=%(refname) %(objectname)',
            ref,
        ],
        cwd=store,
    ).stdout.splitlines()
    for line in refs:
        name, separator, target = line.partition(b' ')
        if name != ref.encode():
            continue
        if not separator or target != commit.encode():
            raise PubanonError('publication archive reference conflicts with snapshot', fact=ref)
        return True
    return False


def publication_record(
    paths: Paths, project: str, candidate: CandidateRecord, commit: str, confirmed_at: datetime
) -> _records.PublicationRecord:
    """Derive one confirmation record from frozen candidate facts and objects.

    `commit` must be the correction or one replay entry. A replay record
    derives its tree, message, digest, provenance, and matching strict release
    tag from the retained poststate and omits pinned-source data. The result is
    not persisted.
    """
    entry = next((entry for entry in candidate.replayed if entry.new == commit), None)
    if commit != candidate.commit and entry is None:
        raise PubanonError('publication is outside the snapshot transaction')
    tree = candidate.tree
    digest = candidate.snapshot_digest
    message = candidate.message
    tag = candidate.tag_intent
    if entry is not None:
        store = _store.candidate_store_path(paths, project)
        raw = run_git(
            ['--git-dir', str(store.absolute()), 'cat-file', 'commit', commit], cwd=store
        ).stdout
        tree = raw.splitlines()[0].removeprefix(b'tree ').decode('ascii')
        message = raw.partition(b'\n\n')[2].decode('utf-8')
        digest = snapshot_digest(read_snapshot(store, commit, git_dir=store))
        tags = _records.candidate_poststate(candidate)[1]
        tag = next(
            (
                name
                for name in sorted(tags)
                if tags[name] == commit
                and name.startswith('v')
                and _VERSION_PATTERN.fullmatch(name[1:]) is not None
            ),
            None,
        )
    return _records.PublicationRecord(
        commit=commit,
        tree=tree,
        snapshot_digest=digest,
        fingerprint=candidate.fingerprint,
        message=message,
        tag_intent=tag,
        pinned=candidate.pinned if entry is None else None,
        destination=candidate.destination,
        confirmed_at=confirmed_at,
        source_bookmark=candidate.source_bookmark if entry is None else entry.source_bookmark,
        replaces=candidate.replaces if entry is None else None,
        replace_scope=candidate.replace_scope if entry is None else None,
        derivation=None
        if entry is None
        else _records.Derivation(
            entry.old, entry.original_source, candidate.commit, entry.resolutions
        ),
    )


def read_ref(store: Path, ref: str) -> str | None:
    """Read the OID of one exact advisory-store ref, if present.

    Args:
        store: Snapshot store containing the ref.
        ref: Fully qualified ref name to read.

    Returns:
        The ref's OID, or `None` when the exact ref is absent.

    Raises:
        PubanonError: Git cannot read the store or returns malformed ref data.
    """
    output = git_result(
        ['for-each-ref', '--format=%(refname) %(objectname)', ref],
        store=store,
        failure='cannot read snapshot store ref',
    )
    exact = None
    for line in output.splitlines(keepends=True):
        name, separator, raw_oid = line.partition(b' ')
        if not name or not separator:
            raise PubanonError('Git returned an invalid snapshot store ref')
        oid = _oid(raw_oid, 'Git returned an invalid snapshot store ref')
        if name == ref.encode():
            if exact is not None:
                raise PubanonError('Git returned an invalid snapshot store ref')
            exact = oid
    return exact


def published_refs(paths: Paths, project: str) -> tuple[str | None, dict[str, str]]:
    """Read the published main and tags from one advisory store.

    Args:
        paths: Derived pubanon configuration and data paths.
        project: Registered project name.

    Returns:
        The optional main tip and complete tag map.

    Raises:
        PubanonError: The project name or stored references are invalid.
    """
    store = _store.candidate_store_path(paths, project)
    if not store.exists():
        return None, {}
    return read_ref(store, 'refs/heads/main'), _read_tags(store)


def regular_file_bytes(paths: Paths, project: str, commit: str, path: str) -> bytes | None:
    """Read an exact regular file from a store commit.

    Args:
        paths: Derived pubanon configuration and data paths.
        project: Registered project name.
        commit: Published commit object ID in the advisory store.
        path: Repository-relative POSIX path to read.

    Returns:
        The file's bytes, or None when the path is not a regular file there.

    Raises:
        PubanonError: The store read fails or returns malformed tree data.
    """
    store = _store.candidate_store_path(paths, project)
    output = git_result(
        ['ls-tree', '-z', '--full-tree', commit, '--', path],
        store=store,
        failure='cannot read published file',
    )
    if not output:
        return None
    if not output.endswith(b'\0') or b'\0' in output[:-1]:
        raise PubanonError('cannot read published file')
    metadata, separator, actual_path = output[:-1].partition(b'\t')
    try:
        mode, kind, oid = metadata.split(b' ')
    except ValueError as exc:
        raise PubanonError('cannot read published file') from exc
    if (
        not separator
        or len(oid) not in {40, 64}
        or any(character not in b'0123456789abcdef' for character in oid)
    ):
        raise PubanonError('cannot read published file')
    if not (
        actual_path == path.encode('utf-8') and mode in {b'100644', b'100755'} and kind == b'blob'
    ):
        return None
    return git_result(
        ['cat-file', 'blob', oid.decode('ascii')],
        store=store,
        failure='cannot read published file',
    )


def replacement_descendants(store: Path, selected: str, main: str | None) -> tuple[str, ...]:
    """Prove linear main ancestry and return descendants after the selected commit.

    Args:
        store: Bare publication store holding the selected history.
        selected: Published commit whose descendants will be replayed.
        main: Published main tip, or `None` when nothing has been published.

    Returns:
        Descendants in oldest-first order, excluding `selected`; empty when
        `selected` equals `main`.

    Raises:
        PubanonError: Main is missing, history is nonlinear, or `selected` is
            outside main's ancestry.
    """
    if main is None:
        raise PubanonError('nothing to replace; this would be the first publication')
    graph = _replacement_parent_graph(store, main)
    chain: list[str] = []
    seen: set[str] = set()
    current = main
    while True:
        if current in seen or current not in graph:
            raise PubanonError('Git returned invalid publication history')
        seen.add(current)
        parents = graph[current]
        if len(parents) > 1:
            raise PubanonError('replacement history is not linear')
        if current == selected:
            return tuple(reversed(chain))
        chain.append(current)
        if not parents:
            raise PubanonError('selected replacement is outside published main history')
        current = parents[0]


def require_known_outcome(record: CandidateRecord | None) -> None:
    """Refuse a candidate that needs publication outcome resolution.

    Raises:
        PubanonError: The previous publication outcome is unknown; resolve it
            before starting another operation.
    """
    if outcome_unknown(record):
        raise PubanonError(
            'the outcome of the last publication is unknown',
            recovery='pubanon publish --resolve',
        )


def tag_intent_for(version: str | None) -> str | None:
    """Return the validated tag intent for one optional version.

    Args:
        version: Optional `X.Y.Z` release version.

    Returns:
        `vX.Y.Z`, or `None` for an untagged candidate.

    Raises:
        PubanonError: The version is not `X.Y.Z`.
    """
    if version is None:
        return None
    if _VERSION_PATTERN.fullmatch(version) is None:
        raise PubanonError('version must have X.Y.Z form')
    return f'v{version}'


def _commit_message(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
    version: str | None,
    message: str | None,
) -> tuple[str | None, str]:
    """Validate tag intent and gate the controlled commit message."""
    tag_intent = tag_intent_for(version)
    controlled = message
    if controlled is None:
        template = commit_message_template(g, p, tagged=tag_intent is not None)
        fields = {} if tag_intent is None else {'tag': tag_intent, 'version': version}
        controlled = template.format_map(fields).rstrip('\r\n')
    if not controlled:
        raise PubanonError('commit message must not be empty')
    try:
        controlled_bytes = controlled.encode()
    except UnicodeEncodeError as exc:
        raise PubanonError('the snapshot commit message must be valid UTF-8 text') from exc
    derived = derived_private_terms(paths, p, os.environ)
    rules = assemble_rules(g, p, derived)

    identity = f'{g.anon_name} <{g.anon_email}>'
    identity_hits = gate_unit('publication identity', identity.encode(), rules)
    if identity_hits:
        raise identity_refusal(identity_rule_sources(identity_hits, g, p, derived))
    if tag_intent is not None and gate_unit('tag intent', tag_intent.encode(), rules):
        raise PubanonError('the release tag did not pass content checks')
    if gate_unit('commit message', controlled_bytes, rules):
        raise PubanonError('the snapshot commit message did not pass content checks')
    return tag_intent, controlled


def version_from_tag(tag: str) -> str | None:
    """Return the version from one strict `vX.Y.Z` release tag.

    Args:
        tag: Candidate Git tag name.

    Returns:
        The `X.Y.Z` version, or `None` for a nonrelease tag.
    """
    if not tag.startswith('v'):
        return None
    version = tag[1:]
    return version if _VERSION_PATTERN.fullmatch(version) is not None else None


def replacement_version(version: str | None, tags: Sequence[str]) -> str | None:
    """Resolve correction version policy from every tag on the selected release.

    Args:
        version: Explicit release version, if supplied.
        tags: Tags moving with the corrected publication.

    Returns:
        The explicit or unambiguous inferred release version.

    Raises:
        PubanonError: An explicit version does not name a moved tag.
    """
    if version is None and len(tags) == 1:
        return version_from_tag(tags[0])
    if version is not None and tags and tag_intent_for(version) not in tags:
        raise PubanonError(
            'replacement version must match a moved tag',
            recovery='commit the version correction or publish a new version',
        )
    return version


def _build_metadata(
    store: Path,
    inputs: CandidateInputs,
    parent: str | None,
    prestate_tags: dict[str, str],
) -> _BuildMetadata:
    """Resolve parents and message from frozen Git objects and release selections."""
    if (inputs.replaces is None) != (inputs.replace_scope is None):
        raise PubanonError('replacement selection and scope must be supplied together')
    if inputs.replaces is None:
        parents = () if parent is None else (parent,)
        return _BuildMetadata(parents, (), None, None, (), inputs._version, inputs._message)
    descendants = replacement_descendants(store, inputs.replaces, parent)
    old_tree, old_parents, _date, raw_message = commit_fields(store, inputs.replaces)
    if inputs.replace_scope == 'all' and inputs.replaces != parent:
        raise PubanonError('replacement all must select the published tip')
    parents = old_parents if inputs.replace_scope == 'one' else ()
    try:
        old_message = raw_message.removesuffix(b'\n').decode('utf-8')
    except UnicodeDecodeError as exc:
        raise PubanonError('replacement commit message is invalid') from exc
    tags = tuple(
        tag
        for tag, oid in prestate_tags.items()
        if oid == inputs.replaces and inputs.replace_scope == 'one'
    )
    version = replacement_version(inputs._version, tags)
    return _BuildMetadata(
        parents,
        descendants,
        old_tree,
        old_message,
        tags,
        version,
        old_message if inputs._message is None else inputs._message,
    )


def build_candidate(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
    inputs: CandidateInputs,
) -> CandidateRecord:
    """Build and retain one publication candidate.

    The caller must hold the project lock for the complete operation and must
    first select the source and apply source path and blob gates. Construction
    gates identity, commit message, and tag intent; it does not establish
    complete source, content, host, or publication acceptance. Replacement
    selection and scope must be paired. Earlier replacements produce an
    incomplete candidate that requires replay.

    Args:
        paths: Derived pubanon configuration and data paths.
        g: Global publication identity configuration.
        p: Project publication configuration.
        inputs: Exact source facts and optional release selections.

    Returns:
        The retained candidate record.

    Raises:
        PubanonError: A candidate exists or construction fails closed.
    """
    if _records.candidate_path(paths, p.name).exists():
        raise PubanonError('a snapshot is already prepared')
    store = _store.store_path(paths, p.name)
    if inputs.prestate is None:
        parent = read_ref(store, 'refs/heads/main')
        prestate_tags = _read_tags(store)
    else:
        parent, prestate_tags = inputs.prestate
    metadata = _build_metadata(store, inputs, parent, prestate_tags)
    tag_intent, controlled_message = _commit_message(
        paths, g, p, metadata._version, metadata._message
    )
    if len(metadata._tags) > 1:
        tag_intent = None
    if inputs.replace_scope == 'one' and not metadata._tags and tag_intent in prestate_tags:
        raise PubanonError('intended tag is already published')
    tree = _write_tree(store, inputs._snapshot)
    if tree == metadata._old_tree and controlled_message == metadata._old_message:
        output('Replacement has unchanged files and commit message.', kind='warning')
    epoch = int(time.time())
    date = f'{epoch} +0000'
    env = {
        'GIT_AUTHOR_DATE': date,
        'GIT_AUTHOR_EMAIL': g.anon_email,
        'GIT_AUTHOR_NAME': g.anon_name,
        'GIT_COMMITTER_DATE': date,
        'GIT_COMMITTER_EMAIL': g.anon_email,
        'GIT_COMMITTER_NAME': g.anon_name,
    }
    args = ['commit-tree', tree]
    for correction_parent in metadata._parents:
        args.extend(['-p', correction_parent])
    commit = _oid(
        git_result(
            args,
            store=store,
            failure='cannot write snapshot commit',
            input_bytes=controlled_message.encode() + b'\n',
            env_extra=env,
        ),
        'Git returned an invalid snapshot commit ID',
    )
    if commit == inputs.replaces:
        # Equal objects in the same second still need a distinct correction ID.
        env['GIT_AUTHOR_DATE'] = env['GIT_COMMITTER_DATE'] = f'{epoch + 1} +0000'
        commit = _oid(
            git_result(
                args,
                store=store,
                failure='cannot write snapshot commit',
                input_bytes=controlled_message.encode() + b'\n',
                env_extra=env,
            ),
            'Git returned an invalid snapshot commit ID',
        )
    git_result(
        ['update-ref', '--no-deref', CANDIDATE_REF, commit],
        store=store,
        failure='cannot anchor snapshot commit',
    )
    record = CandidateRecord(
        commit=commit,
        tree=tree,
        snapshot_digest=snapshot_digest(inputs._snapshot),
        tag_intent=tag_intent,
        fingerprint=fingerprint(g, p),
        pinned=inputs._pinned,
        destination=RecordedDestination(
            p.host,
            p.owner,
            p.repo,
            p.visibility,
            g.accounts[p.host],
        ),
        prestate_main=parent,
        prestate_tags=prestate_tags,
        state=STATE_RETAINED,
        message=controlled_message,
        replaces=inputs.replaces,
        replace_scope=inputs.replace_scope,
    )
    record = _complete_unreplayed(record, metadata)
    save_candidate(paths, p.name, record)
    return record
