import os
import re
import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Unpack
from unittest.mock import patch

import pytest

from pubanon import _filesystem
from pubanon._cli import _arguments
from pubanon._config import GlobalConfig, Paths, ProjectConfig, fingerprint
from pubanon._errors import PubanonError
from pubanon._run import GitResult, run_git
from pubanon._snapshots import (
    _candidate,
    _inspection,
    _records,
    _source,
    _store,
    _verification,
)
from pubanon._snapshots._candidate import _write_tree
from pubanon._snapshots._source import (
    Entry,
    PinnedSource,
    Snapshot,
    read_snapshot,
    snapshot_digest,
)
from tests._git import RunGitOptions, git_bytes, oracle_tree
from tests._permissions import assert_private_mode
from tests._setup import make_paths
from tests.snapshots.test_candidate import (
    build,
    configs,
    pinned,
    replacement_history,
    source_snapshot,
)
from tests.snapshots.test_inspection import dirty_inspection


def _replace_candidate_commit(
    paths: Paths,
    project: ProjectConfig,
    record: _candidate.CandidateRecord,
    raw: bytes,
) -> _candidate.CandidateRecord:
    store = _store.candidate_store_path(paths, project.name)
    commit = git_bytes(
        store,
        'hash-object',
        '--literally',
        '-t',
        'commit',
        '-w',
        '--stdin',
        input_bytes=raw,
    )
    commit_text = commit.strip().decode('ascii')
    git_bytes(store, 'update-ref', _candidate.CANDIDATE_REF, commit_text)
    changed = replace(record, commit=commit_text)
    _candidate.save_candidate(paths, project.name, changed)
    return changed


def _reprepare_record() -> _records.CandidateRecord:
    return _records.CandidateRecord(
        commit='a' * 40,
        tree='b' * 40,
        snapshot_digest='c' * 64,
        tag_intent=None,
        fingerprint='d' * 64,
        pinned=PinnedSource('e' * 40, 'explicit', None, None),
        destination=_records.RecordedDestination('github', 'owner', 'repo', 'private', 'account'),
        prestate_main=None,
        prestate_tags={},
        state=_candidate.STATE_RETAINED,
        message='release',
    )


def _retain_alias_symlink_candidate(
    tmp_path: Path,
) -> tuple[Paths, ProjectConfig, _candidate.CandidateRecord]:
    paths, project, record = build(tmp_path)
    store = _store.candidate_store_path(paths, project.name)
    snapshot = (
        Entry('A', 0o120000, 'symlink', b'.'),
        Entry('B', 0o120000, 'symlink', b'a/../sibling'),
    )
    tree = oracle_tree(store, snapshot)
    raw = git_bytes(store, 'cat-file', 'commit', record.commit)
    raw = raw.replace(f'tree {record.tree}\n'.encode(), f'tree {tree}\n'.encode(), 1)
    changed = replace(record, tree=tree, snapshot_digest=snapshot_digest(snapshot))
    changed = _replace_candidate_commit(paths, project, changed, raw)
    return paths, project, changed


def complete_replacement(
    tmp_path: Path,
    *,
    changed: Snapshot | None = None,
    stage: bool = False,
    selected: int = 0,
    tagged: bool = False,
) -> tuple[Paths, GlobalConfig, ProjectConfig, _records.CandidateRecord]:
    paths, g, project, commits = replacement_history(tmp_path, stage=stage)
    store = _store.store_path(paths, project.name)
    if tagged:
        for index, old in enumerate(commits):
            git_bytes(store, 'update-ref', f'refs/tags/v{index}.0.0', old)
    record = _candidate.build_candidate(
        paths,
        g,
        project,
        _candidate.CandidateInputs(
            source_snapshot(),
            pinned(),
            None,
            None,
            replaces=commits[selected],
            replace_scope='one',
        ),
    )
    replayed: list[_records.ReplayEntry] = []
    previous = record.commit
    for old in commits[selected + 1 :]:
        raw = git_bytes(store, 'cat-file', 'commit', old)
        headers, _, message = raw.partition(b'\n\n')
        old_parent = git_bytes(store, 'show', '-s', '--format=%P', old).strip()
        headers = headers.replace(b'parent ' + old_parent, b'parent ' + previous.encode())
        if changed is not None:
            tree = _write_tree(store, changed)
            old_tree = git_bytes(store, 'show', '-s', '--format=%T', old).strip()
            headers = headers.replace(b'tree ' + old_tree, b'tree ' + tree.encode())
        result = run_git(
            ['hash-object', '--literally', '-t', 'commit', '-w', '--stdin'],
            cwd=store,
            input_bytes=headers + b'\n\n' + message,
        )
        new = result.stdout.strip().decode()
        replayed.append(_records.ReplayEntry(old=old, new=new))
        previous = new
    tags = {}
    if tagged:
        rewritten = (record.commit, *(entry.new for entry in replayed))
        tags = {
            f'v{index}.0.0': commits[index] if index < selected else rewritten[index - selected]
            for index in range(len(commits))
        }
    record = replace(record, replayed=tuple(replayed), tip=previous, poststate_tags=tags)
    _candidate.save_candidate(paths, project.name, record)
    git_bytes(store, 'update-ref', _candidate.CANDIDATE_REF, previous)
    return paths, g, project, record


@dataclass(frozen=True)
class _RetainedVerification:
    calls: list[list[str]]
    global_config: GlobalConfig
    paths: Paths
    project: ProjectConfig
    record: _candidate.CandidateRecord
    snapshot: Snapshot


@pytest.fixture
def retained_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _RetainedVerification:
    paths = make_paths(tmp_path)
    global_config, project = configs(tmp_path)
    snapshot = source_snapshot()
    record = _candidate.build_candidate(
        paths, global_config, project, _candidate.CandidateInputs(snapshot, pinned(), None, None)
    )
    calls: list[list[str]] = []
    real_run_git = _verification.run_git

    def _record_run_git(args: Sequence[str], **kwargs: Unpack[RunGitOptions]) -> GitResult:
        calls.append(list(args))
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(_verification, 'run_git', _record_run_git)
    return _RetainedVerification(calls, global_config, paths, project, record, snapshot)


@pytest.mark.parametrize('command', ['rev-parse', 'cat-file'])
def test_candidate_verification_reports_child_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    paths, project, record = build(tmp_path)
    global_config, _ = configs(tmp_path)
    real_git = _verification.run_git

    def _fail(args: Sequence[str], **kwargs: Unpack[RunGitOptions]) -> GitResult:
        if args[2] == command:
            return GitResult(7, b'', b'diagnostic\x1b\\path')
        return real_git(args, **kwargs)

    monkeypatch.setattr(_verification, 'run_git', _fail)
    with pytest.raises(PubanonError) as error:
        _verification.verify_retained_candidate(paths, global_config, project, record, env={})
    assert error.value.fact == r'exit status 7: diagnostic\x1b\\path'
    assert error.value.recovery == 'pubanon discard, then pubanon prepare'
    assert _records.load_candidate(paths, project.name) == record


@pytest.mark.slow
def test_frozen_publication_verification_rejects_substituted_tag_mapping(tmp_path: Path) -> None:
    paths, _g, project, record = complete_replacement(tmp_path)
    changed = replace(record, poststate_tags={'v1.0.0': record.commit})
    with pytest.raises(PubanonError, match='poststate'):
        _verification.verify_publication_objects(paths, project.name, changed)


def test_incomplete_replacement_conflicts_with_archive_only(tmp_path: Path) -> None:
    paths, _global_config, project, record = complete_replacement(tmp_path)
    store = _store.store_path(paths, project.name)
    assert _records.load_publication(paths, project.name, record.commit) is None
    git_bytes(store, 'update-ref', _records.publication_ref(record.commit), record.commit)

    with pytest.raises(
        PubanonError, match='the incomplete snapshot conflicts with saved published history'
    ):
        _verification.confirmed_publication(paths, project.name, replace(record, tip=None))


def test_inspect_materializes_exact_files_modes_and_symlink_without_authority(
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    global_config, project_config = configs(tmp_path)
    snapshot = (
        Entry('README.md', 0o100644, 'blob', b'public\x00bytes\n'),
        Entry('bin/tool', 0o100755, 'blob', b'#!/bin/sh\n'),
        Entry('readme-link', 0o120000, 'symlink', b'README.md'),
    )
    record = _candidate.build_candidate(
        paths,
        global_config,
        project_config,
        _candidate.CandidateInputs(snapshot, pinned(), None, None),
    )

    inspect = _verification.materialize_inspect(
        paths, project_config.name, _verification.prove_objects(paths, project_config.name, record)
    )

    assert (inspect / 'README.md').read_bytes() == b'public\x00bytes\n'
    assert_private_mode(inspect, 0o500)
    assert_private_mode(inspect / 'bin', 0o500)
    assert_private_mode(inspect / 'README.md', 0o400)
    assert_private_mode(inspect / 'bin/tool', 0o500)
    assert os.fsencode((inspect / 'readme-link').readlink()) == b'README.md'
    assert not (inspect / '.git').exists()
    assert (
        _verification.materialize_inspect(
            paths,
            project_config.name,
            _verification.prove_objects(paths, project_config.name, record),
        )
        == inspect
    )
    assert (inspect / 'README.md').read_bytes() == b'public\x00bytes\n'
    store = _store.store_path(paths, project_config.name)
    assert (
        git_bytes(store, 'rev-parse', f'{record.commit}^{{tree}}').strip().decode() == record.tree
    )


def test_inspect_refuses_alias_symlink_before_replacing_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths, project, record = _retain_alias_symlink_candidate(tmp_path)
    marker = dirty_inspection(paths, project.name)

    def _refuse_materialization(*_args: object, **_kwargs: object) -> None:
        pytest.fail('unsafe retained graph reached symlink materialization')

    monkeypatch.setattr(Path, 'symlink_to', _refuse_materialization)

    with pytest.raises(PubanonError, match='unresolvable symlink target'):
        _verification.materialize_inspect(
            paths, project.name, _verification.prove_objects(paths, project.name, record)
        )

    assert marker.read_text(encoding='utf-8') == 'keep this edit\n'
    assert sorted(path.name for path in marker.parent.iterdir()) == ['edited.txt']


@pytest.mark.parametrize('part', ['name', 'email'])
def test_inspect_refuses_bare_cr_in_shared_identity_before_cleaning(
    part: str,
    tmp_path: Path,
) -> None:
    paths, project, record = build(tmp_path)
    marker = dirty_inspection(paths, project.name)
    store = _store.candidate_store_path(paths, project.name)
    raw = git_bytes(store, 'cat-file', 'commit', record.commit)
    records = raw.split(b'\n')
    if part == 'name':
        old, new = b'Anonymous Publisher', b'Anonymous\r Publisher'
    else:
        old, new = b'anonymous@example.invalid', b'anonymous\r@example.invalid'
    for field in (b'author ', b'committer '):
        index = next(index for index, value in enumerate(records) if value.startswith(field))
        records[index] = records[index].replace(old, new, 1)
    changed = _replace_candidate_commit(paths, project, record, b'\n'.join(records))

    with pytest.raises(
        PubanonError,
        match='snapshot commit metadata does not match the record',
    ) as exc_info:
        _verification.materialize_inspect(
            paths, project.name, _verification.prove_objects(paths, project.name, changed)
        )

    assert exc_info.value.recovery == 'pubanon discard, then pubanon prepare'
    assert marker.read_text(encoding='utf-8') == 'keep this edit\n'


def test_inspect_refuses_candidate_ref_tamper_before_cleaning(tmp_path: Path) -> None:
    paths, project, record = build(tmp_path)
    marker = dirty_inspection(paths, project.name)
    git_bytes(
        _store.candidate_store_path(paths, project.name),
        'update-ref',
        '-d',
        _candidate.CANDIDATE_REF,
    )

    with pytest.raises(PubanonError, match='saved snapshot commit could not be verified'):
        _verification.materialize_inspect(
            paths, project.name, _verification.prove_objects(paths, project.name, record)
        )

    assert marker.read_text(encoding='utf-8') == 'keep this edit\n'


@pytest.mark.parametrize(
    'tamper',
    ['additional-header', 'identity', 'timestamp', 'offset'],
)
def test_inspect_refuses_commit_header_tamper_before_cleaning(
    tamper: str,
    tmp_path: Path,
) -> None:
    paths, project, record = build(tmp_path)
    marker = dirty_inspection(paths, project.name)
    store = _store.candidate_store_path(paths, project.name)
    raw = git_bytes(store, 'cat-file', 'commit', record.commit)
    if tamper == 'additional-header':
        raw = raw.replace(b'\nauthor ', b'\ngpgsig forged-signature\nauthor ', 1)
    elif tamper == 'identity':
        raw = raw.replace(
            b'committer Anonymous Publisher <anonymous@example.invalid>',
            b'committer Different Publisher <different@example.invalid>',
            1,
        )
    elif tamper == 'timestamp':
        raw = re.sub(rb'(?m)^(author .+ )\d+( \+0000)$', rb'\1not-a-time\2', raw, count=1)
    else:
        raw = raw.replace(b' +0000\n', b' -0700\n', 1)
    changed = _replace_candidate_commit(paths, project, record, raw)

    with pytest.raises(PubanonError, match='snapshot commit metadata does not match the record'):
        _verification.materialize_inspect(
            paths, project.name, _verification.prove_objects(paths, project.name, changed)
        )

    assert marker.read_text(encoding='utf-8') == 'keep this edit\n'


def test_inspect_refuses_commit_tree_tamper_before_cleaning(tmp_path: Path) -> None:
    paths, project, record = build(tmp_path)
    marker = dirty_inspection(paths, project.name)
    store = _store.candidate_store_path(paths, project.name)
    other_tree = oracle_tree(
        store,
        (Entry('replacement.txt', 0o100644, 'blob', b'replacement\n'),),
    )
    changed = replace(record, tree=other_tree)
    _candidate.save_candidate(paths, project.name, changed)

    with pytest.raises(PubanonError, match='snapshot commit metadata does not match the record'):
        _verification.materialize_inspect(
            paths, project.name, _verification.prove_objects(paths, project.name, changed)
        )

    assert marker.read_text(encoding='utf-8') == 'keep this edit\n'


@pytest.mark.parametrize('field', ['author', 'committer'])
def test_inspect_refuses_crlf_identity_header_before_cleaning(
    field: str,
    tmp_path: Path,
) -> None:
    paths, project, record = build(tmp_path)
    marker = dirty_inspection(paths, project.name)
    store = _store.candidate_store_path(paths, project.name)
    raw = git_bytes(store, 'cat-file', 'commit', record.commit)
    records = raw.split(b'\n')
    index = next(
        index for index, value in enumerate(records) if value.startswith(f'{field} '.encode())
    )
    records[index] += b'\r'
    changed = _replace_candidate_commit(paths, project, record, b'\n'.join(records))

    with pytest.raises(
        PubanonError,
        match='snapshot commit metadata does not match the record',
    ) as exc_info:
        _verification.materialize_inspect(
            paths, project.name, _verification.prove_objects(paths, project.name, changed)
        )

    assert exc_info.value.recovery == 'pubanon discard, then pubanon prepare'
    assert marker.read_text(encoding='utf-8') == 'keep this edit\n'


def test_inspect_refuses_invalid_tag_intent_before_cleaning(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    global_config, project = configs(tmp_path)
    record = _candidate.build_candidate(
        paths,
        global_config,
        project,
        _candidate.CandidateInputs(source_snapshot(), pinned(), '1.2.3', None),
    )
    marker = dirty_inspection(paths, project.name)
    candidate_path = _records.candidate_path(paths, project.name)
    candidate_path.write_text(
        candidate_path.read_text(encoding='utf-8').replace('v1.2.3', 'v01.2.3'),
        encoding='utf-8',
    )
    invalid = replace(record, tag_intent='v01.2.3', message='release: v01.2.3')

    with pytest.raises(PubanonError, match='invalid snapshot record'):
        _verification.materialize_inspect(
            paths, project.name, _verification.prove_objects(paths, project.name, invalid)
        )

    assert marker.read_text(encoding='utf-8') == 'keep this edit\n'


def test_inspect_refuses_physical_alias_symlink_escape(tmp_path: Path) -> None:
    probe = tmp_path / 'probe'
    probe.mkdir()
    (probe / 'A').symlink_to('.', target_is_directory=True)
    if not (probe / 'a').exists():
        pytest.skip('requires a case-insensitive filesystem')
    sibling = tmp_path / 'sibling'
    sibling.write_bytes(b'synthetic sibling sentinel\n')
    (probe / 'B').symlink_to(Path('a/../sibling'))
    try:
        resolved = (probe / 'B').read_bytes()
    except OSError:
        pytest.skip('filesystem does not resolve parent traversal through this symlink alias')
    assert resolved == sibling.read_bytes()

    paths, project, record = _retain_alias_symlink_candidate(tmp_path)
    marker = dirty_inspection(paths, project.name)
    inspection_sibling = marker.parent.parent / 'sibling'
    inspection_sibling.write_bytes(sibling.read_bytes())

    with pytest.raises(PubanonError, match='unresolvable symlink target'):
        _verification.materialize_inspect(
            paths, project.name, _verification.prove_objects(paths, project.name, record)
        )

    assert marker.read_text(encoding='utf-8') == 'keep this edit\n'
    assert not (marker.parent / 'B').is_symlink()
    assert inspection_sibling.read_bytes() == b'synthetic sibling sentinel\n'


def test_inspect_refuses_record_change_during_proof_before_cleaning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, project, record = build(tmp_path)
    marker = dirty_inspection(paths, project.name)
    original_read_snapshot = _verification.read_snapshot

    def _change_record(store: Path, commit: str, *, git_dir: Path | None = None) -> Snapshot:
        snapshot = original_read_snapshot(store, commit, git_dir=git_dir)
        _candidate.save_candidate(
            paths,
            project.name,
            replace(record, message='changed during proof'),
        )
        return snapshot

    monkeypatch.setattr(_verification, 'read_snapshot', _change_record)

    with pytest.raises(PubanonError, match='snapshot record changed during verification'):
        _verification.materialize_inspect(
            paths, project.name, _verification.prove_objects(paths, project.name, record)
        )

    assert marker.read_text(encoding='utf-8') == 'keep this edit\n'


@pytest.mark.parametrize('field', ['parent', 'message'])
def test_inspect_refuses_recorded_commit_fact_tamper_before_cleaning(
    field: str,
    tmp_path: Path,
) -> None:
    paths, project, record = build(tmp_path)
    marker = dirty_inspection(paths, project.name)
    changed = (
        replace(record, prestate_main='f' * 40)
        if field == 'parent'
        else replace(record, message='changed message')
    )
    _candidate.save_candidate(paths, project.name, changed)

    with pytest.raises(PubanonError, match='snapshot commit metadata does not match the record'):
        _verification.materialize_inspect(
            paths, project.name, _verification.prove_objects(paths, project.name, changed)
        )

    assert marker.read_text(encoding='utf-8') == 'keep this edit\n'


def test_inspect_refuses_snapshot_digest_tamper_before_cleaning(tmp_path: Path) -> None:
    paths, project, record = build(tmp_path)
    marker = dirty_inspection(paths, project.name)
    changed = replace(record, snapshot_digest='0' * 64)
    _candidate.save_candidate(paths, project.name, changed)

    with pytest.raises(PubanonError, match='saved snapshot files do not match what was prepared'):
        _verification.materialize_inspect(
            paths, project.name, _verification.prove_objects(paths, project.name, changed)
        )

    assert marker.read_text(encoding='utf-8') == 'keep this edit\n'


@pytest.mark.parametrize('refresh', [False, True])
def test_inspect_verifies_retained_objects_before_reusing_or_refreshing_copy(
    tmp_path: Path, refresh: bool
) -> None:
    paths, project, record = build(tmp_path)
    inspection = _verification.materialize_inspect(
        paths, project.name, _verification.prove_objects(paths, project.name, record)
    )
    _filesystem.chmod(inspection, 0o700)
    (inspection / 'notes').write_text('keep notes')
    git_bytes(_store.store_path(paths, project.name), 'update-ref', '-d', _candidate.CANDIDATE_REF)
    with pytest.raises(PubanonError, match='saved snapshot commit could not be verified'):
        _verification.materialize_inspect(
            paths,
            project.name,
            _verification.prove_objects(paths, project.name, record),
            refresh=refresh,
        )
    assert (inspection / 'notes').read_text() == 'keep notes'


@pytest.mark.parametrize('directory', ['publications', 'source-worktrees'])
def test_publication_state_in_committed_retained_snapshot_is_refused(
    tmp_path: Path, make_repo: Callable[..., Path], directory: str
) -> None:
    selected = f'state/pubanon/projects/example/{directory}/private.txt'
    source = make_repo(files={selected: 'private state', 'publications/ordinary.txt': 'ordinary'})
    paths = make_paths(source)
    paths = paths._replace(data_dir=source / 'state' / 'pubanon')
    global_config, project = configs(tmp_path)
    project = replace(project, source_path=source.resolve(), allow=('**',))
    pinned = _source.pin(source, 'HEAD')
    snapshot = read_snapshot(source, pinned.commit)
    record = _candidate.build_candidate(
        paths, global_config, project, _candidate.CandidateInputs(snapshot, pinned, None, None)
    )
    with pytest.raises(PubanonError, match='publication state'):
        _verification.verify_retained_candidate(paths, global_config, project, record, env={})


def test_replacement_audits_untouched_earlier_commit_headers(tmp_path: Path) -> None:
    paths, g, project, commits = replacement_history(tmp_path)
    store = _store.store_path(paths, project.name)
    raw = git_bytes(store, 'cat-file', 'commit', commits[0])
    raw = raw.replace(b'\nauthor ', b'\ngpgsig invalid-signature\nauthor ')
    signed = run_git(
        ['hash-object', '--literally', '-t', 'commit', '-w', '--stdin'], cwd=store, input_bytes=raw
    ).stdout.strip()
    raw = git_bytes(store, 'cat-file', 'commit', commits[1])
    raw = raw.replace(b'parent ' + commits[0].encode(), b'parent ' + signed)
    selected = (
        run_git(
            ['hash-object', '--literally', '-t', 'commit', '-w', '--stdin'],
            cwd=store,
            input_bytes=raw,
        )
        .stdout.strip()
        .decode()
    )
    git_bytes(store, 'update-ref', 'refs/heads/main', selected)
    record = _candidate.build_candidate(
        paths,
        g,
        project,
        _candidate.CandidateInputs(
            source_snapshot(),
            pinned(),
            None,
            None,
            replaces=selected,
            replace_scope='one',
        ),
    )
    with pytest.raises(PubanonError, match='history has identity problems'):
        _verification.verify_retained_candidate(paths, g, project, record, env={})


@pytest.mark.parametrize('name', ['my project', "it's mine", r'a\b', '-leading'])
def test_reprepare_advice_selects_exact_project(name: str) -> None:
    record = _reprepare_record()
    advice = _verification.reprepare_advice(record, project=name)
    for command in advice.split(' && '):
        argv = shlex.split(command)
        assert _arguments.build_parser().parse_args(argv[1:]).project == name


@pytest.mark.slow
def test_replacement_checks_reserved_paths_in_every_replayed_tree(tmp_path: Path) -> None:
    relative = 'pubanon/projects/example/publications/private.txt'
    paths, g, project, record = complete_replacement(
        tmp_path,
        changed=(Entry(relative, 0o100644, 'blob', b'public bytes'),),
    )
    # Put the existing data directory under the configured source root without
    # changing any frozen objects or introducing private bytes into correction.
    project = replace(project, source_path=paths.data_dir.parent)
    actual = paths.data_dir.name + '/projects/example/publications/private.txt'
    assert actual == relative
    record = replace(record, fingerprint=fingerprint(g, project))
    _candidate.save_candidate(paths, project.name, record)
    with pytest.raises(PubanonError, match='publication state'):
        _verification.verify_retained_candidate(paths, g, project, record, env={})


@pytest.mark.slow
def test_replacement_gates_complete_changed_path_names(tmp_path: Path) -> None:
    paths, g, project, record = complete_replacement(
        tmp_path,
        changed=(Entry('old-name.txt', 0o100644, 'blob', b'public bytes'),),
    )
    g = replace(g, sensitive_terms=('old-name',))
    record = replace(record, fingerprint=fingerprint(g, project))
    _candidate.save_candidate(paths, project.name, record)
    with pytest.raises(
        PubanonError, match='the replayed snapshot did not pass current content checks'
    ) as error:
        _verification.verify_retained_candidate(paths, g, project, record, env={})
    assert 'old-name.txt' in str(error.value.fact)


@pytest.mark.slow
@pytest.mark.parametrize('resolved', [False, True])
@pytest.mark.parametrize('filename', ['new.txt', 'file.txt'])
def test_replacement_gates_full_changed_blob_even_when_term_occurred_elsewhere(
    tmp_path: Path,
    resolved: bool,
    filename: str,
) -> None:
    # On file.txt, the private first line is unchanged from the first old
    # release. A patch-lines-only scan would miss it in this changed blob.
    changed = (Entry(filename, 0o100644, 'blob', b'old 1\nnew public line'),)
    paths, g, project, record = complete_replacement(tmp_path, changed=changed)
    g = replace(g, sensitive_terms=('old',))
    if resolved:
        entries = tuple(
            replace(entry, resolutions=(_records.Resolution(paths=(filename,), action='keep'),))
            for entry in record.replayed
        )
        record = replace(record, replayed=entries)
    record = replace(record, fingerprint=fingerprint(g, project))
    _candidate.save_candidate(paths, project.name, record)
    with pytest.raises(
        PubanonError, match='the replayed snapshot did not pass current content checks'
    ) as error:
        _verification.verify_retained_candidate(paths, g, project, record, env={})
    assert filename in str(error.value.fact)
    assert record.replayed[0].old[:12] in str(error.value.fact)


@pytest.mark.slow
def test_replacement_inspection_defaults_to_tip(
    tmp_path: Path,
) -> None:
    paths, _g, project, record = complete_replacement(tmp_path)
    tip = _verification.materialize_inspect(
        paths, project.name, _verification.prove_objects(paths, project.name, record)
    )
    assert tip.name == record.tip
    assert (tip / 'file.txt').read_bytes() == b'old 2'


@pytest.mark.slow
def test_replacement_inspection_rejects_untouched_earlier_tag(tmp_path: Path) -> None:
    paths, _g, project, record = complete_replacement(tmp_path, selected=1, tagged=True)
    selected = _verification.inspect_rewritten(
        paths, project.name, _verification.prove_objects(paths, project.name, record), 'v1.0.0'
    )
    replayed = _verification.inspect_rewritten(
        paths, project.name, _verification.prove_objects(paths, project.name, record), 'v2.0.0'
    )
    assert selected.name == record.commit
    assert replayed.name == record.tip
    copies = _inspection.inspection_paths(paths, project.name)
    refs = _candidate.published_refs(paths, project.name)
    with pytest.raises(PubanonError) as error:
        _verification.inspect_rewritten(
            paths, project.name, _verification.prove_objects(paths, project.name, record), 'v0.0.0'
        )
    assert error.value.fact == 'v0.0.0'
    assert _inspection.inspection_paths(paths, project.name) == copies
    assert _candidate.published_refs(paths, project.name) == refs
    assert _records.load_candidate(paths, project.name) == record


@pytest.mark.slow
def test_replacement_inspection_selects_only_rewritten_commits(tmp_path: Path) -> None:
    paths, _g, project, record = complete_replacement(tmp_path)
    assert record.replaces is not None
    correction = _verification.inspect_rewritten(
        paths,
        project.name,
        _verification.prove_objects(paths, project.name, record),
        record.commit[:12],
    )
    replay = _verification.inspect_rewritten(
        paths,
        project.name,
        _verification.prove_objects(paths, project.name, record),
        record.replayed[0].new,
    )
    assert _verification.select_rewritten_commit(record, record.commit.upper()) == record.commit
    assert correction.name == record.commit
    assert replay.name == record.replayed[0].new
    assert (correction / 'README.md').read_bytes() == b'public\n'
    assert (replay / 'file.txt').read_bytes() == b'old 1'
    _filesystem.chmod(correction, 0o700)
    _filesystem.chmod(replay, 0o700)
    (correction / 'notes').write_text('discard on refresh')
    (replay / 'notes').write_text('preserve other copy')
    with pytest.raises(PubanonError) as error:
        _verification.inspect_rewritten(
            paths,
            project.name,
            _verification.prove_objects(paths, project.name, record),
            record.replayed[0].new,
        )
    assert error.value.recovery == (
        f'pubanon inspect --rewritten {record.replayed[0].new} --refresh'
    )
    assert (
        _verification.inspect_rewritten(
            paths,
            project.name,
            _verification.prove_objects(paths, project.name, record),
            record.commit,
            refresh=True,
        )
        == correction
    )
    assert not (correction / 'notes').exists()
    assert (replay / 'notes').read_text() == 'preserve other copy'
    with pytest.raises(PubanonError, match='rewritten'):
        _verification.inspect_rewritten(
            paths,
            project.name,
            _verification.prove_objects(paths, project.name, record),
            record.replaces[:12],
        )


@pytest.mark.slow
def test_replacement_publication_proof_needs_neither_ref_nor_current_configuration(
    tmp_path: Path,
) -> None:
    paths, _g, project, record = complete_replacement(tmp_path)
    store = _store.store_path(paths, project.name)
    git_bytes(store, 'update-ref', '-d', _candidate.CANDIDATE_REF)
    _records.candidate_path(paths, project.name).unlink()
    _verification.verify_publication_objects(paths, project.name, record)


@pytest.mark.slow
def test_replacement_rejects_missing_untagged_descendant(tmp_path: Path) -> None:
    paths, g, project, record = complete_replacement(tmp_path)
    store = _store.store_path(paths, project.name)
    last = record.replayed[-1]
    raw = git_bytes(store, 'cat-file', 'commit', last.new)
    raw = raw.replace(
        f'parent {record.replayed[0].new}'.encode(), f'parent {record.commit}'.encode()
    )
    new = (
        run_git(
            ['hash-object', '-t', 'commit', '-w', '--stdin'],
            cwd=store,
            input_bytes=raw,
        )
        .stdout.strip()
        .decode()
    )
    # The remaining old/new pair has the expected message/date and a valid
    # correction -> tip chain. Only complete old-sequence proof catches the gap.
    record = replace(record, tip=new, replayed=(replace(last, new=new),))
    _candidate.save_candidate(paths, project.name, record)
    git_bytes(store, 'update-ref', _candidate.CANDIDATE_REF, new)
    with pytest.raises(PubanonError, match='complete descendant chain'):
        _verification.verify_retained_candidate(paths, g, project, record, env={})


@pytest.mark.slow
@pytest.mark.parametrize('anchor', ['correction', 'old-tip', 'intermediate'])
def test_replacement_rejects_stale_or_substituted_candidate_anchor(
    tmp_path: Path,
    anchor: str,
) -> None:
    paths, g, project, record = complete_replacement(tmp_path)
    store = _store.store_path(paths, project.name)
    anchors = {
        'correction': record.commit,
        'old-tip': record.replayed[-1].old,
        'intermediate': record.replayed[0].new,
    }
    git_bytes(store, 'update-ref', _candidate.CANDIDATE_REF, anchors[anchor])
    with pytest.raises(PubanonError, match='saved snapshot commit'):
        _verification.verify_retained_candidate(paths, g, project, record, env={})
    with pytest.raises(PubanonError, match='saved snapshot commit'):
        _verification.materialize_inspect(
            paths, project.name, _verification.prove_objects(paths, project.name, record)
        )


@pytest.mark.parametrize('mutation', ['parent', 'message', 'author-date', 'identity', 'signature'])
def test_replacement_rejects_substituted_replay_metadata(tmp_path: Path, mutation: str) -> None:
    paths, g, project, record = complete_replacement(tmp_path)
    store = _store.store_path(paths, project.name)
    last = record.replayed[-1]
    raw = git_bytes(store, 'cat-file', 'commit', last.new)
    if mutation == 'parent':
        raw = raw.replace(
            f'parent {record.replayed[0].new}'.encode(), f'parent {record.commit}'.encode()
        )
    elif mutation == 'message':
        raw = raw.replace(b'\n\nrelease 2\n', b'\n\nsubstituted message\n')
    elif mutation == 'author-date':
        raw = re.sub(rb'(author [^\n]+>) [0-9]+', rb'\1 1234567890', raw)
    elif mutation == 'identity':
        raw = raw.replace(b'Anonymous Publisher', b'Other Publisher')
    else:
        raw = raw.replace(b'\nauthor ', b'\ngpgsig forged\nauthor ')
    result = run_git(
        ['hash-object', '--literally', '-t', 'commit', '-w', '--stdin'], cwd=store, input_bytes=raw
    )
    new = result.stdout.strip().decode()
    assert new != last.new
    record = replace(record, tip=new, replayed=(*record.replayed[:-1], replace(last, new=new)))
    _candidate.save_candidate(paths, project.name, record)
    git_bytes(store, 'update-ref', _candidate.CANDIDATE_REF, new)
    with pytest.raises(
        PubanonError, match=r'metadata|original author date or message|anonymous identity'
    ):
        _verification.verify_retained_candidate(paths, g, project, record, env={})
    with pytest.raises(PubanonError):
        _verification.verify_publication_objects(paths, project.name, record)


@pytest.mark.slow
def test_replacement_verification_returns_tip_and_allows_unchanged_historical_exposure(
    tmp_path: Path,
) -> None:
    paths, g, project, record = complete_replacement(tmp_path)
    g = replace(g, sensitive_terms=('old',))
    record = replace(record, fingerprint=fingerprint(g, project))
    _candidate.save_candidate(paths, project.name, record)
    with patch.object(_verification, 'read_snapshot', wraps=_verification.read_snapshot) as reads:
        snapshot = _verification.verify_retained_candidate(paths, g, project, record, env={})
    assert snapshot == (Entry('file.txt', 0o100644, 'blob', b'old 2'),)
    assert record.snapshot_digest == snapshot_digest(source_snapshot())
    read_commits = [call.args[1] for call in reads.call_args_list]
    assert read_commits.count(record.commit) == 1
    for entry in record.replayed:
        assert read_commits.count(entry.old) == 1
        assert read_commits.count(entry.new) == 1
    assert len(read_commits) == 1 + 2 * len(record.replayed)


@pytest.mark.parametrize('side', ['old', 'new'])
@pytest.mark.parametrize('object_kind', ['missing', 'tree'])
def test_replay_metadata_requires_available_commit_objects(
    tmp_path: Path, side: str, object_kind: str
) -> None:
    paths, g, project, record = complete_replacement(tmp_path)
    entry = record.replayed[0]
    oid = 'f' * 40 if object_kind == 'missing' else record.tree
    invalid = replace(entry, old=oid) if side == 'old' else replace(entry, new=oid)
    record = replace(record, replayed=(invalid, *record.replayed[1:]))
    with pytest.raises(PubanonError, match=r'commit.*unavailable'):
        _verification._verify_replayed_commits(
            _store.candidate_store_path(paths, project.name),
            record,
            f'{g.anon_name} <{g.anon_email}>'.encode(),
        )


@pytest.mark.slow
def test_replay_metadata_verification_uses_bounded_processes(tmp_path: Path) -> None:
    paths, g, project, record = complete_replacement(tmp_path)
    store = _store.candidate_store_path(paths, project.name)
    identity = f'{g.anon_name} <{g.anon_email}>'.encode()
    counts: list[int] = []
    for size in (0, 1, len(record.replayed)):
        selected = replace(record, replayed=record.replayed[:size])
        with patch('subprocess.Popen', wraps=subprocess.Popen) as processes:
            tip = _verification._verify_replayed_commits(store, selected, identity)
        assert tip == (selected.replayed[-1].new if size else record.commit)
        counts.append(processes.call_count)
    assert counts == [0, 1, 1]


def test_reprepare_advice_shows_unrenderable_project() -> None:
    record = _reprepare_record()
    advice = _verification.reprepare_advice(record, project='line\nbreak')
    assert 'line\\x0abreak' in advice
    assert advice.startswith('Choose project line\\x0abreak from its registered directory')
    assert '&&' not in advice


def test_retained_verification_reads_commit_metadata_once(
    retained_verification: _RetainedVerification,
) -> None:
    case = retained_verification

    _verification.verify_retained_candidate(
        case.paths, case.global_config, case.project, case.record, env=os.environ
    )

    store = _store.candidate_store_path(case.paths, case.project.name)
    metadata_read = ['--git-dir', str(store.absolute()), 'cat-file', 'commit', case.record.commit]
    assert [args for args in case.calls if args == metadata_read] == [metadata_read]


def test_rewritten_inspection_explains_invalid_utf8_retained_identity(tmp_path: Path) -> None:
    paths, _g, project, record = complete_replacement(tmp_path, selected=2)
    store = _store.candidate_store_path(paths, project.name)
    raw = git_bytes(store, 'cat-file', 'commit', record.commit)
    malformed = raw.replace(b'Anonymous Publisher', b'Anonymous \xffPublisher')
    assert malformed != raw
    commit = (
        git_bytes(
            store,
            'hash-object',
            '--literally',
            '-t',
            'commit',
            '-w',
            '--stdin',
            input_bytes=malformed,
        )
        .strip()
        .decode('ascii')
    )
    record = replace(record, commit=commit, tip=commit)
    _candidate.save_candidate(paths, project.name, record)
    git_bytes(store, 'update-ref', _candidate.CANDIDATE_REF, commit)

    with pytest.raises(PubanonError, match=r'retained replacement.*UTF-8') as error:
        _verification.inspect_rewritten(
            paths, project.name, _verification.prove_objects(paths, project.name, record), commit
        )

    assert error.value.recovery == 'pubanon discard, then pubanon prepare'
    assert _inspection.inspection_paths(paths, project.name) == ()
    assert _records.load_candidate(paths, project.name) == record
    assert b'\xff' in git_bytes(store, 'cat-file', 'commit', commit)


@pytest.mark.slow
def test_rewritten_inspection_verifies_before_reusing_editable_copy(tmp_path: Path) -> None:
    paths, _g, project, record = complete_replacement(tmp_path)
    inspection = _verification.inspect_rewritten(
        paths,
        project.name,
        _verification.prove_objects(paths, project.name, record),
        record.commit,
    )
    _filesystem.chmod(inspection, 0o700)
    (inspection / 'notes').write_text('keep')
    git_bytes(_store.store_path(paths, project.name), 'update-ref', '-d', _candidate.CANDIDATE_REF)
    with pytest.raises(PubanonError):
        _verification.inspect_rewritten(
            paths,
            project.name,
            _verification.prove_objects(paths, project.name, record),
            record.commit,
            refresh=True,
        )
    assert (inspection / 'notes').read_text() == 'keep'


def test_stale_replacement_advice_keeps_the_correction(tmp_path: Path) -> None:
    paths, g, project, record = complete_replacement(tmp_path)
    changed = replace(project, extra_terms=('Changed term',))

    with pytest.raises(
        PubanonError, match='the configuration changed after the snapshot was prepared'
    ) as exc_info:
        _verification.verify_retained_candidate(paths, g, changed, record, env={})

    recovery = exc_info.value.recovery or ''
    assert f'--replace {record.replaces}' in recovery
    assert f'--source {record.pinned.commit}' in recovery
    assert ' && ' in recovery


def test_verify_retained_candidate_refuses_intact_utf8_term_in_mixed_blob(
    tmp_path: Path,
) -> None:
    term = 'Privat\u00e9Alias'
    snapshot = (Entry('mixed.bin', 0o100644, 'blob', b'\xffPrivat\xc3\xa9Alias\n'),)
    paths = make_paths(tmp_path)
    global_config, project = configs(tmp_path)
    record = _candidate.build_candidate(
        paths, global_config, project, _candidate.CandidateInputs(snapshot, pinned(), None, None)
    )
    current_project = replace(project, extra_terms=(term,))
    current_record = replace(record, fingerprint=fingerprint(global_config, current_project))
    _candidate.save_candidate(paths, project.name, current_record)

    with pytest.raises(
        PubanonError, match='the snapshot did not pass current content checks'
    ) as exc_info:
        _verification.verify_retained_candidate(
            paths, global_config, current_project, current_record, env=os.environ
        )

    assert exc_info.value.recovery == 'pubanon discard, then pubanon prepare'

    assert 'project extra_terms entry 1' in (exc_info.value.fact or '')
    assert f'blob bytes 1:{1 + len(term.encode())}' in (exc_info.value.fact or '')
    assert term in exc_info.value.render()
    assert term in (exc_info.value.fact or '')


def test_verify_retained_candidate_reloads_exact_record(tmp_path: Path) -> None:
    paths = make_paths(tmp_path)
    global_config, project = configs(tmp_path)
    record = _candidate.build_candidate(
        paths,
        global_config,
        project,
        _candidate.CandidateInputs(source_snapshot(), pinned(), None, None),
    )
    _candidate.save_candidate(paths, project.name, replace(record, message='changed record'))

    with pytest.raises(PubanonError, match='snapshot record changed') as exc_info:
        _verification.verify_retained_candidate(
            paths, global_config, project, record, env=os.environ
        )

    assert exc_info.value.recovery == 'pubanon discard, then pubanon prepare'


def test_verify_retained_candidate_requires_current_commit_identity(
    retained_verification: _RetainedVerification,
) -> None:
    case = retained_verification
    current_config = replace(
        case.global_config,
        anon_name='Changed Publisher',
        anon_email='changed@example.invalid',
    )
    current_record = replace(
        case.record,
        fingerprint=fingerprint(current_config, case.project),
    )
    _candidate.save_candidate(case.paths, case.project.name, current_record)

    with pytest.raises(
        PubanonError,
        match='snapshot commit metadata does not match the record',
    ) as exc_info:
        _verification.verify_retained_candidate(
            case.paths, current_config, case.project, current_record, env=os.environ
        )

    assert exc_info.value.recovery == 'pubanon discard, then pubanon prepare'


def test_verify_retained_candidate_returns_exact_snapshot(
    retained_verification: _RetainedVerification,
) -> None:
    case = retained_verification
    assert (
        _verification.verify_retained_candidate(
            case.paths, case.global_config, case.project, case.record, env=os.environ
        )
        == case.snapshot
    )


@pytest.mark.parametrize(
    ('snapshot', 'version', 'message', 'term'),
    [
        (
            (Entry('classified/README.md', 0o100644, 'blob', b'public\n'),),
            None,
            None,
            'classified',
        ),
        ((Entry('README.md', 0o100644, 'blob', b'classified\n'),), None, None, 'classified'),
        ((source_snapshot()[0],), None, 'classified release', 'classified'),
        ((source_snapshot()[0],), None, None, 'anonymous@example.invalid'),
        ((source_snapshot()[0],), '1.2.3', 'public release', 'v1.2.3'),
    ],
    ids=('path', 'data', 'message', 'identity', 'tag'),
)
def test_verify_retained_candidate_runs_every_current_gate(
    snapshot: tuple[Entry, ...],
    version: str | None,
    message: str | None,
    term: str,
    tmp_path: Path,
) -> None:
    paths = make_paths(tmp_path)
    global_config, project = configs(tmp_path)
    record = _candidate.build_candidate(
        paths,
        global_config,
        project,
        _candidate.CandidateInputs(snapshot, pinned(), version, message),
    )
    current_config = replace(global_config, sensitive_terms=(term,))
    current_record = replace(record, fingerprint=fingerprint(current_config, project))
    _candidate.save_candidate(paths, project.name, current_record)

    with pytest.raises(
        PubanonError, match='the snapshot did not pass current content checks'
    ) as exc_info:
        _verification.verify_retained_candidate(
            paths, current_config, project, current_record, env=os.environ
        )

    assert exc_info.value.recovery == 'pubanon discard, then pubanon prepare'
