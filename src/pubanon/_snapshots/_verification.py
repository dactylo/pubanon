"""Prove retained candidates against their objects and current gates."""

import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from pubanon._config import (
    GlobalConfig,
    Paths,
    ProjectConfig,
    check_publication_state_paths,
    derived_private_terms,
    fingerprint,
    gate_details,
)
from pubanon._errors import PubanonError
from pubanon._run import git_failure_fact, run_git
from pubanon._snapshots import _audit, _candidate, _inspection, _records, _store
from pubanon._snapshots._records import (
    CandidateRecord,
    load_candidate,
)
from pubanon._snapshots._source import (
    Snapshot,
    read_snapshot,
    snapshot_digest,
)
from pubanon._terminal import (
    escape,
)


@dataclass(frozen=True)
class ObjectProof:
    """Retain object evidence for one uninterrupted, locked review step.

    Obtain instances from `prove_objects`. Re-prove after record changes or
    interactive prompts, and on every later command. This evidence does not
    establish current policy, replacement structure, or host acceptance.

    Attributes:
        record: Exact retained candidate record proven against Git objects.
        snapshot: Correction snapshot read from `record.commit`.
        identity: Verified `name <email>` bytes from correction metadata.
    """

    record: CandidateRecord
    snapshot: Snapshot
    identity: bytes


def _candidate_gate_units(
    global_config: GlobalConfig,
    record: CandidateRecord,
    snapshot: Snapshot,
) -> list[tuple[str, bytes, str]]:
    """Return every retained-candidate unit subject to current gates."""
    units = [(entry.path, entry.path.encode(), 'filename') for entry in snapshot]
    units.extend((entry.path, entry.data, 'blob') for entry in snapshot)
    units.append(('commit message', record.message.encode(), 'message'))
    identity = f'{global_config.anon_name} <{global_config.anon_email}>'.encode()
    units.append(('publication identity', identity, 'header identity value'))
    if record.tag_intent is not None:
        units.append(('tag intent', record.tag_intent.encode(), 'tag name'))
    return units


def _correction_parents(store: Path, record: CandidateRecord) -> tuple[str, ...]:
    """Return the exact correction parents implied by its frozen scope."""
    if record.replaces is None:
        return () if record.prestate_main is None else (record.prestate_main,)
    if record.replace_scope == 'all':
        if record.replaces != record.prestate_main:
            raise PubanonError('replacement all must select the published tip')
        return ()
    if record.replace_scope != 'one':
        raise PubanonError('replacement scope is invalid')
    return _candidate.commit_fields(store, record.replaces)[1]


def _verify_commit_metadata(
    record: CandidateRecord,
    raw: bytes,
    recovery: str,
    parents: tuple[str, ...] | None = None,
) -> bytes:
    """Require exact retained commit headers and return their shared identity."""
    headers, separator, message = raw.partition(b'\n\n')
    expected_headers = [f'tree {record.tree}'.encode()]
    if parents is None:
        parents = () if record.prestate_main is None else (record.prestate_main,)
    expected_headers.extend(f'parent {parent}'.encode() for parent in parents)
    lines = headers.split(b'\n')
    if (
        not separator
        or len(lines) != len(expected_headers) + 2
        or lines[: len(expected_headers)] != expected_headers
    ):
        raise PubanonError(
            'snapshot commit metadata does not match the record',
            recovery=recovery,
        )
    identities: list[bytes] = []
    for label, line in zip((b'author', b'committer'), lines[-2:], strict=True):
        match = re.fullmatch(
            label + rb' (?P<identity>[^<>\r\n]+ <[^<>\r\n]+>) [0-9]+ \+0000',
            line,
        )
        if match is None:
            raise PubanonError(
                'snapshot commit metadata does not match the record',
                recovery=recovery,
            )
        identities.append(match.group('identity'))
    if identities[0] != identities[1]:
        raise PubanonError(
            'snapshot commit metadata does not match the record',
            recovery=recovery,
        )
    if message != record.message.encode() + b'\n':
        raise PubanonError(
            'snapshot commit metadata does not match the record',
            recovery=recovery,
        )
    return identities[0]


def _verify_replacement_content(  # noqa: PLR0913
    paths: Paths,
    global_config: GlobalConfig,
    project: ProjectConfig,
    record: CandidateRecord,
    derived: tuple[str, ...],
    *,
    recovery: str,
) -> Snapshot:
    """Gate complete entries introduced by replay."""
    tip, _tags = _records.candidate_poststate(record)
    # Historical content findings belong to the existing exposure acknowledgment
    # flow. Only complete changed entries are new publication content here.
    store = _store.candidate_store_path(paths, project.name)
    last_new: Snapshot | None = None
    for entry in record.replayed:
        old = read_snapshot(store, entry.old, git_dir=store)
        new = read_snapshot(store, entry.new, git_dir=store)
        _inspection.validate_tree_paths(new)
        check_publication_state_paths(paths, project, [item.path for item in new])
        changed = _audit.changed_entries(old, new)
        units = [(item.path, item.path.encode(), 'filename') for item in changed]
        units.extend((item.path, item.data, 'blob') for item in changed)
        details = gate_details(units, global_config, project, derived)
        if details:
            raise PubanonError(
                'the replayed snapshot did not pass current content checks',
                fact=escape(f'{entry.old[:12]}: ' + '; '.join(details.values())),
                recovery=recovery,
            )
        last_new = new
    if last_new is not None and record.replayed[-1].new == tip:
        return last_new
    return read_snapshot(store, tip, git_dir=store)


def _verify_replayed_commits(store: Path, record: CandidateRecord, identity: bytes) -> str:
    """Prove each replay's parent, message, author date, and anonymous metadata."""
    queries = tuple(
        dict.fromkeys(
            oid.encode('ascii') for entry in record.replayed for oid in (entry.old, entry.new)
        )
    )
    objects = _audit.read_objects(store, queries)
    previous = record.commit
    for entry in record.replayed:
        old = objects[entry.old.encode('ascii')]
        new = objects[entry.new.encode('ascii')]
        if old is None or new is None or old[0] != b'commit' or new[0] != b'commit':
            raise PubanonError('replayed commit is unavailable')
        _old_tree, _old_parents, old_date, old_message = _candidate.parse_commit_fields(old[1])
        raw = new[1]
        new_tree, _parents, new_date, new_message = _candidate.parse_commit_fields(raw)
        if new_date != old_date or new_message != old_message:
            raise PubanonError('replayed commit changed the original author date or message')
        try:
            message = old_message.removesuffix(b'\n').decode('utf-8')
        except UnicodeDecodeError as exc:
            raise PubanonError('replayed commit message is invalid') from exc
        replay_record = replace(record, commit=entry.new, tree=new_tree, message=message)
        if _verify_commit_metadata(replay_record, raw, 'pubanon discard', (previous,)) != identity:
            raise PubanonError('replayed commit changed the anonymous identity')
        previous = entry.new
    return previous


def _verify_replacement_objects(
    paths: Paths,
    project: str,
    record: CandidateRecord,
    identity: bytes,
) -> None:
    """Verify the full frozen replacement independently of mutable configuration."""
    if record.replaces is None:
        return
    tip, tags = _records.candidate_poststate(record)
    store = _store.candidate_store_path(paths, project)
    descendants = _candidate.replacement_descendants(store, record.replaces, record.prestate_main)
    expected = descendants if record.replace_scope == 'one' else ()
    if tuple(entry.old for entry in record.replayed) != expected:
        raise PubanonError('replacement replay does not contain the complete descendant chain')
    previous = _verify_replayed_commits(store, record, identity)
    if (
        record.replace_scope == 'one'
        and record.replaces not in record.prestate_tags.values()
        and record.tag_intent in record.prestate_tags
    ):
        raise PubanonError('intended tag is already published')
    expected_tags = _records.replacement_poststate_tags(record, record.replayed)
    if tags != expected_tags or tip != previous:
        raise PubanonError('replacement poststate does not match the complete replay')
    audit = _audit.audit_history(
        paths,
        project,
        _audit.published_commits(paths, project, new=tip),
        tags,
        {},
        identity.decode('utf-8'),
        tip=tip,
    )
    if audit.identity_problems:
        raise PubanonError(
            'replacement history has identity problems',
            fact=escape('; '.join(audit.identity_problems)),
        )


def prove_objects(
    paths: Paths,
    project: str,
    record: CandidateRecord,
    *,
    recovery: str = 'pubanon discard, then pubanon prepare',
    anchor: str | None = None,
) -> ObjectProof:
    """Prove one stable retained record against its immutable Git objects.

    The caller must hold the project lock for this proof and its consumers.
    Reuse is limited to one review step; prompts or record changes require a
    fresh proof. Current policy, replacement structure, and host acceptance
    remain separate checks.

    Args:
        paths: Derived pubanon paths.
        project: Registered project name.
        record: Exact saved candidate to verify.
        recovery: Recovery advice attached to verification refusals.
        anchor: Expected candidate ref for a partial replay, or `None` to
            derive the anchor from the record.

    Returns:
        The verified record, correction snapshot, and commit identity.

    Raises:
        PubanonError: The record changed or retained objects fail verification.
    """
    if load_candidate(paths, project) != record:
        raise PubanonError('snapshot record changed before verification', recovery=recovery)
    store = _store.candidate_store_path(paths, project)
    candidate_ref = run_git(
        ['--git-dir', str(store.absolute()), 'rev-parse', '--verify', _candidate.CANDIDATE_REF],
        cwd=store,
        check=False,
    )
    if anchor is None:
        anchor = record.commit if record.tip is None else _records.candidate_tip(record)
    if candidate_ref.returncode != 0 or candidate_ref.stdout != f'{anchor}\n'.encode():
        raise PubanonError(
            'saved snapshot commit could not be verified',
            fact=git_failure_fact(candidate_ref) if candidate_ref.returncode != 0 else None,
            recovery=recovery,
        )
    result = run_git(
        ['--git-dir', str(store.absolute()), 'cat-file', 'commit', record.commit],
        cwd=store,
        check=False,
    )
    if result.returncode != 0:
        raise PubanonError(
            'snapshot commit cannot be read', fact=git_failure_fact(result), recovery=recovery
        )
    verified_identity = _verify_commit_metadata(
        record, result.stdout, recovery, _correction_parents(store, record)
    )
    snapshot = read_snapshot(store, record.commit, git_dir=store)
    if snapshot_digest(snapshot) != record.snapshot_digest:
        raise PubanonError(
            'saved snapshot files do not match what was prepared', recovery=recovery
        )
    if load_candidate(paths, project) != record:
        raise PubanonError('snapshot record changed during verification', recovery=recovery)
    return ObjectProof(record, snapshot, verified_identity)


def materialize_inspect(
    paths: Paths,
    project: str,
    proof: ObjectProof,
    *,
    refresh: bool = False,
) -> Path:
    """Create or reuse the verified resulting tip's read-only inspection copy.

    Existing copies must match the saved snapshot on ordinary reuse. Refresh
    explicitly replaces the selected copy. The copy is not publication
    authority.

    The caller must hold the project lock for the complete operation.

    Args:
        paths: Derived pubanon paths.
        project: Registered project name.
        proof: Object evidence obtained for this review step.

        refresh: Replace the selected copy, including any local changes.

    Returns:
        Path to the candidate-specific inspection directory.

    Raises:
        PubanonError: Exact materialization fails.
    """
    record = proof.record
    snapshot = proof.snapshot
    _verify_replacement_objects(paths, project, record, proof.identity)
    commit = _records.candidate_tip(record)
    if commit != record.commit:
        store = _store.candidate_store_path(paths, project)
        snapshot = read_snapshot(store, commit, git_dir=store)
    return _inspection.materialize_snapshot(paths, project, commit, snapshot, refresh=refresh)


def select_rewritten_commit(record: CandidateRecord, selector: str | None) -> str:
    """Resolve a rewritten commit or moved tag from one candidate record.

    Args:
        record: Candidate containing the rewritten commits and moved tags.
        selector: `None` for the candidate tip, or an exact moved tag or unique
            case-insensitive object-ID prefix for a rewritten commit.

    Raises:
        PubanonError: The candidate is not a replacement, the selector is
            unknown or ambiguous, or a tag points to untouched history.
    """
    if record.replaces is None:
        raise PubanonError('no replaced snapshot is retained')
    rewritten = (record.commit, *(entry.new for entry in record.replayed))
    if selector is None:
        selected = _records.candidate_tip(record)
    elif record.poststate_tags is not None and selector in record.poststate_tags:
        selected = record.poststate_tags[selector]
    else:
        matches = (
            [oid for oid in rewritten if oid.startswith(selector.lower())] if selector else []
        )
        if len(matches) != 1:
            raise PubanonError('rewritten selector is unknown or ambiguous', fact=escape(selector))
        selected = matches[0]
    if selected not in rewritten:
        raise PubanonError(
            'selector does not name a replaced snapshot', fact=escape(selector or '')
        )
    return selected


def inspect_rewritten(
    paths: Paths,
    project: str,
    proof: ObjectProof,
    selector: str | None,
    *,
    refresh: bool = False,
) -> Path:
    """Materialize a verified corrected or replayed commit selected by tag or OID.

    The caller must hold the project lock for the complete operation.

    Args:
        paths: Paths derived for this command.
        project: Registered project name.
        proof: Object evidence for the candidate containing rewritten commits.
        selector: `None` for the candidate tip; other selectors follow
            `select_rewritten_commit`.
        refresh: Replace the selected copy instead of refusing local edits.

    Returns:
        Path to the selected commit's read-only inspection directory.

    Raises:
        PubanonError: Retained replacement evidence, selection, or exact
            materialization fails.
    """
    record = proof.record
    snapshot = proof.snapshot
    try:
        _verify_replacement_objects(paths, project, record, proof.identity)
    except UnicodeDecodeError as exc:
        raise PubanonError(
            'retained replacement commit identity is not valid UTF-8',
            fact=record.commit,
            recovery='pubanon discard, then pubanon prepare',
        ) from exc
    selected = select_rewritten_commit(record, selector)
    if selected != record.commit:
        store = _store.candidate_store_path(paths, project)
        snapshot = read_snapshot(store, selected, git_dir=store)
    try:
        return _inspection.materialize_snapshot(
            paths, project, selected, snapshot, refresh=refresh
        )
    except PubanonError as exc:
        if exc.recovery != 'pubanon inspect --refresh':
            raise
        raise PubanonError(
            str(exc), recovery=f'pubanon inspect --rewritten {selected} --refresh'
        ) from exc


def verify_publication_objects(paths: Paths, project: str, record: CandidateRecord) -> None:
    """Verify exact correction objects and the complete replacement poststate.

    This works after operational candidate refs have been removed and does not
    consult mutable configuration or the source repository.

    Raises:
        PubanonError: Required correction or replay objects are unavailable or
            conflict with retained evidence.
    """
    store = _store.candidate_store_path(paths, project)
    result = run_git(
        ['--git-dir', str(store.absolute()), 'cat-file', 'commit', record.commit],
        cwd=store,
        check=False,
    )
    if result.returncode != 0:
        raise PubanonError('confirmed publication commit is unavailable', fact=record.commit)
    identity = _verify_commit_metadata(
        record, result.stdout, 'pubanon publish --resolve', _correction_parents(store, record)
    )
    _verify_replacement_objects(paths, project, record, identity)
    if (
        snapshot_digest(read_snapshot(store, record.commit, git_dir=store))
        != record.snapshot_digest
    ):
        raise PubanonError('confirmed publication content conflicts with snapshot')


def confirmed_publication(paths: Paths, project: str, record: CandidateRecord) -> bool:
    """Validate durable proof that a candidate was previously confirmed.

    A strict matching publication record or an owned direct archive ref is
    sufficient. Conflicting provenance or refs raise `PubanonError`; absent
    evidence returns false. This does not observe the host's current state.
    """
    if record.replaces is not None and record.tip is None:
        if _records.load_publication(
            paths, project, record.commit
        ) is not None or _candidate.publication_archive(paths, project, record.commit):
            raise PubanonError('the incomplete snapshot conflicts with saved published history')
        return False
    tip = _records.candidate_tip(record)
    published = _records.load_publication(paths, project, tip)
    archive = _candidate.publication_archive(paths, project, tip)
    if published is None and not archive:
        return False
    verify_publication_objects(paths, project, record)
    for commit in (record.commit, *(entry.new for entry in record.replayed)):
        existing = _records.load_publication(paths, project, commit)
        if existing is not None and existing != _candidate.publication_record(
            paths, project, record, commit, existing.confirmed_at
        ):
            raise PubanonError(
                'publication record conflicts with snapshot provenance', fact=commit
            )
        _candidate.publication_archive(paths, project, commit)
    return True


def verify_retained_candidate(  # noqa: PLR0913
    paths: Paths,
    global_config: GlobalConfig,
    project: ProjectConfig,
    record: CandidateRecord,
    *,
    env: Mapping[str, str],
    recovery: str = 'pubanon discard, then pubanon prepare',
) -> Snapshot:
    """Re-prove one retained candidate against current content gates.

    Replacement verification proves all required history identity metadata
    while applying content gates to changed replay content. Unchanged
    historical disclosure is allowed.

    Args:
        paths: Derived pubanon paths.
        global_config: Active global configuration.
        project: Active project configuration.
        record: Candidate record loaded by the current command.

        env: Current command environment for dynamic private-path terms.
        recovery: Advice for invalid retained objects or failed current gates.

    Returns:
        The verified resulting tip snapshot. The record's tree, digest, and
        pinned source continue to describe the correction snapshot.

    Raises:
        PubanonError: The record, candidate objects, or current gates fail.
    """
    proof = prove_objects(paths, project.name, record, recovery=recovery)
    snapshot, identity = proof.snapshot, proof.identity
    _verify_replacement_objects(paths, project.name, record, identity)
    _candidate.require_known_outcome(record)
    if fingerprint(global_config, project) != record.fingerprint:
        raise PubanonError(
            'the configuration changed after the snapshot was prepared',
            recovery=recovery,
        )
    current_identity = f'{global_config.anon_name} <{global_config.anon_email}>'.encode()
    # _verify_replacement_objects audited history with this verified identity.
    if identity != current_identity:
        raise PubanonError(
            'snapshot commit metadata does not match the record',
            recovery=recovery,
        )

    check_publication_state_paths(paths, project, [entry.path for entry in snapshot])
    derived = derived_private_terms(paths, project, env)
    details = gate_details(
        _candidate_gate_units(global_config, record, snapshot), global_config, project, derived
    )
    if details:
        raise PubanonError(
            'the snapshot did not pass current content checks',
            fact=escape('; '.join(details.values())),
            recovery=recovery,
        )
    if record.replaces is not None:
        return _verify_replacement_content(
            paths, global_config, project, record, derived, recovery=recovery
        )
    return snapshot


def verify_rewrite_progress(
    paths: Paths, project: str, record: CandidateRecord, progress: _records.RewriteRecord
) -> str:
    """Verify a frozen partial replay and return its current retained anchor.

    An anchor one unsaved step ahead is verified against the next old commit;
    the caller may reset it to the saved base with an expected-old ref update
    and replay that step. Missing anchors are restored from verified progress.

    The caller must hold the project lock for the complete operation.

    Raises:
        PubanonError: Saved progress conflicts, descendant proof is incomplete,
            replay metadata is invalid, or the retained anchor is unrelated.
    """
    if (
        record.tip is not None
        or record.replaces != progress.replaces
        or record.commit != progress.correction
        or record.prestate_main != progress.prestate_main
        or _records.load_rewrite(paths, project) != progress
    ):
        raise PubanonError('rewrite record conflicts with retained snapshot')
    store = _store.candidate_store_path(paths, project)
    expected = _candidate.replacement_descendants(store, progress.replaces, progress.prestate_main)
    if (
        progress.descendants != expected
        or tuple(entry.old for entry in progress.replayed) != expected[: len(progress.replayed)]
    ):
        raise PubanonError('rewrite descendants do not match frozen publication history')
    raw = _candidate.git_result(
        ['cat-file', 'commit', record.commit], store=store, failure='correction is unavailable'
    )
    identity = _verify_commit_metadata(
        record, raw, 'pubanon discard', _correction_parents(store, record)
    )
    previous = _verify_replayed_commits(
        store, replace(record, replayed=progress.replayed), identity
    )
    anchor = _candidate.read_ref(store, _candidate.CANDIDATE_REF)
    if anchor is None:
        _candidate.git_result(
            ['update-ref', '--no-deref', _candidate.CANDIDATE_REF, previous, '0' * len(previous)],
            store=store,
            failure='cannot restore rewrite anchor',
        )
        anchor = previous
    elif anchor != previous:
        if len(progress.replayed) == len(expected):
            raise PubanonError('rewrite anchor differs from completed progress')
        pending = _records.ReplayEntry(expected[len(progress.replayed)], anchor)
        _verify_replayed_commits(
            store, replace(record, replayed=(*progress.replayed, pending)), identity
        )
    prove_objects(paths, project, record, recovery='pubanon discard', anchor=anchor)
    return anchor
