import re
import subprocess
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import NotRequired, TypedDict, Unpack
from unittest.mock import patch

import pytest

from pubanon._config import GlobalConfig, Paths
from pubanon._errors import PubanonError
from pubanon._gate import RuleClass, gate_unit
from pubanon._run import GitResult, run_git
from pubanon._snapshots import _audit, _source
from pubanon._snapshots._source import Entry, Snapshot, read_snapshot
from pubanon._snapshots._store import store_path
from tests._git import oracle_tree

_ANON_EMAIL = 'anonymous@example.invalid'
_ANON_NAME = 'Anonymous Publisher'
_PROJECT = 'example'


class _CommitOptions(TypedDict, total=False):
    author_email: str
    author_name: str
    committer_offset: str
    parent: str | None
    path: str


class _RunGitOptions(TypedDict):
    check: NotRequired[bool]
    cwd: Path | None
    env_extra: NotRequired[Mapping[str, str] | None]
    input_bytes: NotRequired[bytes | None]


class _SnapshotCommitOptions(TypedDict, total=False):
    author_email: str
    author_name: str
    committer_email: str
    committer_name: str
    committer_offset: str
    parent: str | None


def _audit_history(
    paths: Paths,
    commits: tuple[str, ...],
    *,
    tip: str,
    tags: dict[str, str] | None = None,
    rules: dict[RuleClass, tuple[str, ...]] | None = None,
) -> _audit.HistoryAudit:
    return _audit.audit_history(
        paths, _PROJECT, commits, tags or {}, rules or {}, f'{_ANON_NAME} <{_ANON_EMAIL}>', tip=tip
    )


def _commit_object(paths: Paths, commit: str) -> tuple[bytes, bytes]:
    store = store_path(paths, _PROJECT)
    raw = run_git(['cat-file', 'commit', commit], cwd=store).stdout
    headers, separator, message = raw.partition(b'\n\n')
    assert separator
    return headers, message


def _global_config() -> GlobalConfig:
    return GlobalConfig(
        anon_name=_ANON_NAME,
        anon_email=_ANON_EMAIL,
        accounts={'github': 'release-account'},
        sensitive_terms=(),
    )


def _oid(output: bytes) -> str:
    return output.decode('ascii').strip()


def _paths(tmp_path: Path) -> Paths:
    config_dir = tmp_path / 'config' / 'pubanon'
    data_dir = tmp_path / 'data' / 'pubanon'
    return Paths(config_dir, data_dir, config_dir / 'config.toml', data_dir / 'registry.json')


def _slow_audit_history(
    paths: Paths,
    commits: Sequence[str],
    tags: Mapping[str, str],
    rules: Mapping[RuleClass, Sequence[str]],
    *,
    tip: str,
) -> tuple[tuple[_audit.AuditFinding, ...], tuple[str, ...]]:
    store = store_path(paths, _PROJECT)
    findings: set[_audit.AuditFinding] = set()
    identity_problems: list[str] = []
    global_config = _global_config()

    for oid in commits:
        raw = run_git(['cat-file', 'commit', oid], cwd=store).stdout
        headers, separator, message = raw.partition(b'\n\n')
        if not separator:
            raise PubanonError('Git returned a malformed published commit')
        findings.update(
            _audit.AuditFinding(*hit) for hit in gate_unit(f'{oid[:12]}:message', message, rules)
        )
        header_lines = headers.splitlines()
        identity = (
            re.escape(f'{global_config.anon_name} <{global_config.anon_email}>'.encode())
            + rb' [0-9]+ \+0000'
        )
        for field in (b'author', b'committer'):
            expected = field + b' ' + identity
            matches = [line for line in header_lines if line.startswith(field + b' ')]
            if len(matches) != 1 or re.fullmatch(expected, matches[0]) is None:
                identity_problems.append(f'{oid[:12]}: invalid {field.decode()} identity')
        if any(line.startswith(b'gpgsig') for line in header_lines):
            identity_problems.append(f'{oid[:12]}: commit is signed')
        for entry in read_snapshot(store, oid):
            unit = f'path:{entry.path}'
            findings.update(
                _audit.AuditFinding(*hit) for hit in gate_unit(unit, entry.path.encode(), rules)
            )
            findings.update(
                _audit.AuditFinding(*hit) for hit in gate_unit(unit, entry.data, rules)
            )
    for name, oid in tags.items():
        findings.update(
            _audit.AuditFinding(*hit) for hit in gate_unit(f'tag:{name}', name.encode(), rules)
        )
        object_type = run_git(['cat-file', '-t', oid], cwd=store, check=False)
        if object_type.returncode != 0 or object_type.stdout != b'commit\n':
            identity_problems.append(f'tag:{name}: tag does not point directly to a commit')
            continue
        ancestry = run_git(['merge-base', '--is-ancestor', oid, tip], cwd=store, check=False)
        if ancestry.returncode != 0:
            identity_problems.append(f'tag:{name}: tag target is outside main history')
    ordered_findings = tuple(
        sorted(findings, key=lambda finding: (finding.unit, finding.rule_class.name, finding.term))
    )
    return ordered_findings, tuple(identity_problems)


def _summary(
    audit: _audit.HistoryAudit,
) -> tuple[tuple[_audit.AuditFinding, ...], tuple[str, ...]]:
    return audit.findings, audit.identity_problems


def _write_commit(
    paths: Paths,
    *,
    data: bytes,
    message: bytes,
    **options: Unpack[_CommitOptions],
) -> str:
    store = store_path(paths, _PROJECT)

    author_email = options.get('author_email', _ANON_EMAIL)
    author_name = options.get('author_name', _ANON_NAME)
    committer_offset = options.get('committer_offset', '+0000')
    parent = options.get('parent')
    path = options.get('path', 'history.txt')
    blob = _oid(run_git(['hash-object', '-w', '--stdin'], cwd=store, input_bytes=data).stdout)
    tree = _oid(
        run_git(
            ['mktree', '-z'], cwd=store, input_bytes=f'100644 blob {blob}\t{path}\0'.encode()
        ).stdout
    )
    env = {
        'GIT_AUTHOR_NAME': author_name,
        'GIT_AUTHOR_EMAIL': author_email,
        'GIT_AUTHOR_DATE': '1700000000 +0000',
        'GIT_COMMITTER_NAME': _ANON_NAME,
        'GIT_COMMITTER_EMAIL': _ANON_EMAIL,
        'GIT_COMMITTER_DATE': f'1700000000 {committer_offset}',
    }
    args = ['commit-tree', tree]
    if parent is not None:
        args.extend(('-p', parent))
    return _oid(run_git(args, cwd=store, input_bytes=message, env_extra=env).stdout)


def _write_literal_commit(paths: Paths, headers: bytes, message: bytes) -> str:
    store = store_path(paths, _PROJECT)
    return _oid(
        run_git(
            ['hash-object', '--literally', '-t', 'commit', '-w', '--stdin'],
            cwd=store,
            input_bytes=headers + b'\n\n' + message,
        ).stdout
    )


def _with_gpgsig(paths: Paths, commit: str) -> str:
    headers, message = _commit_object(paths, commit)
    signed = (
        headers + b'\ngpgsig -----BEGIN PGP SIGNATURE-----\n fake\n -----END PGP SIGNATURE-----'
    )
    return _write_literal_commit(paths, signed, message)


def _write_merge_commit(paths: Paths, first_parent: str, second_parent: str) -> str:
    store = store_path(paths, _PROJECT)
    tree = oracle_tree(store, ())
    return _oid(
        run_git(
            ['commit-tree', tree, '-p', first_parent, '-p', second_parent],
            cwd=store,
            input_bytes=b'merge\n',
            env_extra={
                'GIT_AUTHOR_NAME': _ANON_NAME,
                'GIT_AUTHOR_EMAIL': _ANON_EMAIL,
                'GIT_AUTHOR_DATE': '1700000000 +0000',
                'GIT_COMMITTER_NAME': _ANON_NAME,
                'GIT_COMMITTER_EMAIL': _ANON_EMAIL,
                'GIT_COMMITTER_DATE': '1700000000 +0000',
            },
        ).stdout
    )


def _write_snapshot_commit(
    paths: Paths,
    snapshot: Snapshot,
    message: bytes,
    **options: Unpack[_SnapshotCommitOptions],
) -> str:
    store = store_path(paths, _PROJECT)
    tree = oracle_tree(store, snapshot)
    env = {
        'GIT_AUTHOR_NAME': options.get('author_name', _ANON_NAME),
        'GIT_AUTHOR_EMAIL': options.get('author_email', _ANON_EMAIL),
        'GIT_AUTHOR_DATE': '1700000000 +0000',
        'GIT_COMMITTER_NAME': options.get('committer_name', _ANON_NAME),
        'GIT_COMMITTER_EMAIL': options.get('committer_email', _ANON_EMAIL),
        'GIT_COMMITTER_DATE': f'1700000000 {options.get("committer_offset", "+0000")}',
    }
    args = ['commit-tree', tree]
    if (parent := options.get('parent')) is not None:
        args.extend(('-p', parent))
    return _oid(run_git(args, cwd=store, input_bytes=message, env_extra=env).stdout)


def test_changed_entries_preserves_whole_entries_for_new_changed_and_mode_only_paths() -> None:
    old = (
        Entry('same', 0o100644, 'blob', b'unchanged'),
        Entry('mode', 0o100644, 'blob', b'whole old blob'),
        Entry('edit', 0o100644, 'blob', b'old line\n'),
        Entry('deleted', 0o100644, 'blob', b'removed'),
    )
    new = (
        old[0],
        Entry('mode', 0o100755, 'blob', old[1].data),
        Entry('edit', 0o100644, 'blob', b'old line\nadded line\n'),
        Entry('renamed', 0o100644, 'blob', old[3].data),
    )
    assert _audit.changed_entries(old, new) == new[1:]


def test_clean_two_commit_history_has_no_audit_problems(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    first = _write_commit(paths, data=b'public\n', message=b'first\n')
    second = _write_commit(
        paths,
        data=b'public update\n',
        message=b'second\n',
        parent=first,
    )

    result = _audit_history(paths, (second, first), tip=second)

    assert _summary(result) == ((), ())


def test_commit_header_parser_classifies_each_structural_failure() -> None:
    commit = 'a' * 40
    tree = b'tree ' + b'b' * 40
    author = b'author Anonymous Publisher <anonymous@example.invalid> 1700000000 +0000'
    committer = b'committer Anonymous Publisher <anonymous@example.invalid> 1700000000 +0000'
    identity = re.escape(f'{_ANON_NAME} <{_ANON_EMAIL}>'.encode()) + rb' [0-9]+ \+0000'
    cases = (
        (
            b'\n'.join((tree, tree, author, committer)),
            (f'{commit[:12]}: duplicate tree header',),
        ),
        (
            b'\n'.join((tree, author, author, committer)),
            (f'{commit[:12]}: duplicate author header',),
        ),
        (
            b'\n'.join((tree, author, committer, committer)),
            (f'{commit[:12]}: duplicate committer header',),
        ),
        (
            b'\n'.join((author, committer)),
            (f'{commit[:12]}: missing tree header',),
        ),
        (
            b'\n'.join((tree, committer)),
            (f'{commit[:12]}: missing author header',),
        ),
        (
            b'\n'.join((tree, author)),
            (f'{commit[:12]}: missing committer header',),
        ),
        (
            b'\n'.join((b'tree not-an-object-id', author, committer)),
            (f'{commit[:12]}: invalid tree object ID',),
        ),
        (
            b'\n'.join((tree, b'parent not-an-object-id', author, committer)),
            (f'{commit[:12]}: invalid parent object ID',),
        ),
        (
            b'\n'.join((b' orphan-private-value', tree, author, committer)),
            (f'{commit[:12]}: orphan commit header continuation',),
        ),
        (
            b'\n'.join((tree, b'malformed-private-value', author, committer)),
            (f'{commit[:12]}: malformed commit header',),
        ),
    )

    for malformed, expected in cases:
        parsed = _audit._parse_commit_headers(commit, malformed, identity)

        assert parsed.problems == expected
        assert 'private-value' not in '\n'.join(parsed.problems)


def test_exposure_digest_changes_when_identity_finding_is_added() -> None:
    first = _audit.AuditFinding('path:a', RuleClass.IDENTITY_CRITICAL, 'canaryx')
    second = _audit.AuditFinding('path:b', RuleClass.IDENTITY_CRITICAL, 'canaryx')

    assert _audit.exposure_digest((first,)) != _audit.exposure_digest((first, second))


def test_exposure_digest_ignores_hygiene_findings() -> None:
    identity = _audit.AuditFinding('path:a', RuleClass.IDENTITY_CRITICAL, 'canaryx')
    hygiene = _audit.AuditFinding('path:b', RuleClass.HYGIENE, 'canaryx')

    assert _audit.exposure_digest((identity, hygiene)) == _audit.exposure_digest((identity,))


def test_exposure_digest_is_empty_without_identity_findings() -> None:
    hygiene = _audit.AuditFinding('path:a', RuleClass.HYGIENE, 'canaryx')

    assert _audit.exposure_digest(()) == ''
    assert _audit.exposure_digest((hygiene,)) == ''


def test_exposure_digest_is_stable_across_finding_order() -> None:
    first = _audit.AuditFinding('path:a', RuleClass.IDENTITY_CRITICAL, 'canaryx')
    second = _audit.AuditFinding('path:b', RuleClass.IDENTITY_CRITICAL, 'canaryx')

    assert _audit.exposure_digest((first, second)) == _audit.exposure_digest((second, first))


def test_exposure_digest_uses_normative_serialization() -> None:
    finding = _audit.AuditFinding('path:a', RuleClass.IDENTITY_CRITICAL, 'canaryx')

    assert (
        _audit.exposure_digest((finding,))
        == '6e343077a5a70cb1f4acf79cc47dea5d8883ba270a5587fb096c40c5e1056a5c'
    )


@pytest.mark.parametrize(
    ('mutation', 'problem'),
    [
        ('missing', 'missing tree header'),
        ('invalid', 'invalid tree object ID'),
    ],
)
def test_history_audit_classifies_literal_tree_failure_before_diff(
    mutation: str,
    problem: str,
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    original = _write_snapshot_commit(paths, (), b'release\n')
    headers, message = _commit_object(paths, original)
    records = headers.split(b'\n')
    tree_index = next(index for index, record in enumerate(records) if record.startswith(b'tree '))
    if mutation == 'missing':
        records.pop(tree_index)
    else:
        records[tree_index] = b'tree private-tree-value'
    commit = _write_literal_commit(paths, b'\n'.join(records), message)

    result = _audit_history(paths, (commit,), tip=commit)

    assert _summary(result) == (
        (),
        (f'{commit[:12]}: {problem}',),
    )
    assert 'private-tree-value' not in '\n'.join(result.identity_problems)


def test_history_audit_classifies_tags_by_validated_commit_membership(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    root = _write_commit(paths, data=b'root\n', message=b'root\n')
    middle = _write_commit(paths, data=b'middle\n', message=b'middle\n', parent=root)
    tip = _write_commit(paths, data=b'tip\n', message=b'tip\n', parent=middle)
    outside = _write_commit(paths, data=b'outside\n', message=b'outside\n')
    store = store_path(paths, _PROJECT)
    blob = _oid(run_git(['hash-object', '-w', '--stdin'], cwd=store, input_bytes=b'blob\n').stdout)
    tag = _oid(
        run_git(
            ['mktag'],
            cwd=store,
            input_bytes=(
                f'object {tip}\n'
                'type commit\n'
                'tag annotated\n'
                f'tagger {_ANON_NAME} <{_ANON_EMAIL}> 1 +0000\n\n'
                'release\n'
            ).encode(),
        ).stdout
    )
    calls: list[tuple[str, ...]] = []
    real_run_git = _audit.run_git

    def _record_run_git(args: Sequence[str], **kwargs: Unpack[_RunGitOptions]) -> GitResult:
        calls.append(tuple(args))
        return real_run_git(args, **kwargs)

    monkeypatch.setattr(_audit, 'run_git', _record_run_git)

    result = _audit_history(
        paths,
        (tip, middle, root),
        tip=tip,
        tags={
            'root': root,
            'middle': middle,
            'tip': tip,
            'outside': outside,
            'absent': 'f' * 40,
            'blob': blob,
            'annotated': tag,
        },
    )

    assert result.identity_problems == (
        'tag:outside: tag target is outside main history',
        'tag:absent: tag does not point directly to a commit',
        'tag:blob: tag does not point directly to a commit',
        'tag:annotated: tag does not point directly to a commit',
    )
    assert not any(call[2:3] == ('merge-base',) for call in calls)


def test_history_audit_deduplicates_blob_findings_across_commits(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    first = _write_commit(paths, data=b'canaryx orchidx\n', message=b'first\n')
    second = _write_commit(paths, data=b'new orchidx canaryx\n', message=b'second\n', parent=first)
    third = _write_snapshot_commit(paths, (), b'deleted\n', parent=second)
    rules = {RuleClass.IDENTITY_CRITICAL: ('canaryx', 'orchidx')}
    full, focused = _audit.audit_history_pair(
        paths,
        _PROJECT,
        (third, second, first),
        {},
        (second, third),
        {},
        rules,
        f'{_ANON_NAME} <{_ANON_EMAIL}>',
        tip=third,
    )
    expected = tuple(
        _audit.AuditFinding('path:history.txt', RuleClass.IDENTITY_CRITICAL, term)
        for term in ('canaryx', 'orchidx')
    )
    assert full.findings == focused.findings == expected
    for result, witness in ((full, first), (focused, second)):
        assert tuple(item.finding for item in result.evidence) == expected
        for item in result.evidence:
            assert item.commit == witness
            data = run_git(
                ['show', f'{item.commit}:history.txt'], cwd=store_path(paths, _PROJECT)
            ).stdout
            location = re.fullmatch(r'blob bytes ([0-9]+):([0-9]+)', item.location)
            assert location is not None
            start, end = map(int, location.groups())
            assert data[start:end] == item.finding.term.encode()
    assert _audit.exposure_digest(full.findings) == _audit.exposure_digest(focused.findings)


def test_history_audit_finds_intact_utf8_term_in_mixed_blob(tmp_path: Path) -> None:
    term = 'Privat\u00e9Alias'
    paths = _paths(tmp_path)
    commit = _write_commit(
        paths,
        data=b'\xffPrivat\xc3\xa9Alias\n',
        message=b'release\n',
    )

    result = _audit_history(
        paths,
        (commit,),
        tip=commit,
        rules={RuleClass.IDENTITY_CRITICAL: (term,)},
    )

    assert result.findings == (
        _audit.AuditFinding('path:history.txt', RuleClass.IDENTITY_CRITICAL, term),
    )


def test_history_audit_gates_complete_non_structural_header_values(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    root = _write_snapshot_commit(paths, (), b'root\n')
    original = _write_snapshot_commit(paths, (), b'release\n', parent=root)
    headers, message = _commit_object(paths, original)
    records = headers.split(b'\n')
    tree = records[0].removeprefix(b'tree ').decode('ascii')
    records[1:1] = [
        b'encoding hygiene-encoding',
        b'x-extension private-extension',
        b' continuation-hygiene',
        b'mergetag public tag',
        b' private-mergetag',
    ]
    commit = _write_literal_commit(paths, b'\n'.join(records), message)
    rules = {
        RuleClass.HYGIENE: (
            'author',
            'continuation-hygiene',
            'hygiene-encoding',
            'parent',
            'tree',
        ),
        RuleClass.IDENTITY_CRITICAL: (
            '+0000',
            '1700000000',
            'private-extension',
            'private-mergetag',
            root,
            tree,
        ),
    }

    full, focused = _audit.audit_history_pair(
        paths,
        _PROJECT,
        (commit, root),
        {},
        (root,),
        {},
        rules,
        f'{_ANON_NAME} <{_ANON_EMAIL}>',
        tip=commit,
    )

    assert _summary(full) == (
        (
            _audit.AuditFinding(
                f'{commit[:12]}:header:encoding',
                RuleClass.HYGIENE,
                'hygiene-encoding',
            ),
            _audit.AuditFinding(
                f'{commit[:12]}:header:mergetag',
                RuleClass.IDENTITY_CRITICAL,
                'private-mergetag',
            ),
            _audit.AuditFinding(
                f'{commit[:12]}:header:x-extension',
                RuleClass.HYGIENE,
                'continuation-hygiene',
            ),
            _audit.AuditFinding(
                f'{commit[:12]}:header:x-extension',
                RuleClass.IDENTITY_CRITICAL,
                'private-extension',
            ),
        ),
        (),
    )
    assert _summary(focused) == ((), ())


@pytest.mark.parametrize(
    ('field', 'problem'),
    [
        (b'author', 'invalid author identity'),
        (b'committer', 'invalid committer identity'),
    ],
)
def test_history_audit_keeps_carriage_return_in_identity_value(
    field: bytes,
    problem: str,
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    original = _write_snapshot_commit(paths, (), b'release\n')
    headers, message = _commit_object(paths, original)
    records = headers.split(b'\n')
    index = next(index for index, record in enumerate(records) if record.startswith(field + b' '))
    records[index] += b'\rprivate-header-value'
    commit = _write_literal_commit(paths, b'\n'.join(records), message)

    result = _audit_history(paths, (commit,), tip=commit)

    assert result.identity_problems == (f'{commit[:12]}: {problem}',)
    assert 'private-header-value' not in '\n'.join(result.identity_problems)


def test_history_audit_labels_hygiene_term_in_commit_message(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    commit = _write_commit(
        paths,
        data=b'public\n',
        message=b'canaryx release\n',
    )

    result = _audit_history(
        paths,
        (commit,),
        tip=commit,
        rules={RuleClass.HYGIENE: ('canaryx',)},
    )

    assert result.findings == (
        _audit.AuditFinding(f'{commit[:12]}:message', RuleClass.HYGIENE, 'canaryx'),
    )


def test_history_audit_labels_sensitive_path_and_direct_tag(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    commit = _write_commit(
        paths,
        data=b'public\n',
        message=b'release\n',
        path='private-path.txt',
    )

    result = _audit_history(
        paths,
        (commit,),
        tip=commit,
        tags={'private-tag': commit},
        rules={RuleClass.IDENTITY_CRITICAL: ('private-path', 'private-tag')},
    )

    assert _summary(result) == (
        (
            _audit.AuditFinding(
                'path:private-path.txt',
                RuleClass.IDENTITY_CRITICAL,
                'private-path',
            ),
            _audit.AuditFinding(
                'tag:private-tag',
                RuleClass.IDENTITY_CRITICAL,
                'private-tag',
            ),
        ),
        (),
    )


def test_history_audit_reports_every_unsupported_topology(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    root = _write_snapshot_commit(
        paths,
        (Entry('history.txt', 0o100644, 'blob', b'root\n'),),
        b'root\n',
    )
    child = _write_snapshot_commit(
        paths,
        (Entry('history.txt', 0o100644, 'blob', b'child\n'),),
        b'child\n',
        parent=root,
    )
    disconnected = _write_snapshot_commit(
        paths,
        (Entry('history.txt', 0o100644, 'blob', b'disconnected\n'),),
        b'disconnected\n',
    )
    wrong_parent = _write_snapshot_commit(
        paths,
        (Entry('history.txt', 0o100644, 'blob', b'wrong\n'),),
        b'wrong\n',
        parent=disconnected,
    )
    merge = _write_merge_commit(paths, root, child)

    assert _audit_history(paths, (root,), tip=root).identity_problems == ()
    assert _audit_history(paths, (child, root), tip=child).identity_problems == ()

    cases = (
        (
            (child, root),
            root,
            ('published history tip does not match declared tip',),
        ),
        (
            (disconnected, root),
            disconnected,
            (f'{disconnected[:12]}: commit does not have exactly one parent',),
        ),
        (
            (root, child),
            root,
            (
                f'{root[:12]}: commit does not have exactly one parent',
                f'{child[:12]}: oldest commit has a parent',
            ),
        ),
        (
            (root, root),
            root,
            ('published history contains duplicate commits',),
        ),
        (
            (wrong_parent, root),
            wrong_parent,
            (f'{wrong_parent[:12]}: commit parent does not match history',),
        ),
        (
            (merge, child, root),
            merge,
            (f'{merge[:12]}: commit does not have exactly one parent',),
        ),
    )

    for commits, tip, expected in cases:
        result = _audit_history(paths, commits, tip=tip)

        assert result.findings == ()
        assert result.identity_problems == expected


@pytest.mark.parametrize('term', [_ANON_NAME, _ANON_EMAIL])
@pytest.mark.parametrize('rule_class', [RuleClass.IDENTITY_CRITICAL, RuleClass.HYGIENE])
def test_history_audit_reports_newly_sensitive_anonymous_identity(
    tmp_path: Path,
    term: str,
    rule_class: RuleClass,
) -> None:
    paths = _paths(tmp_path)
    first = _write_snapshot_commit(paths, (), b'first\n')
    second = _write_snapshot_commit(paths, (), b'second\n', parent=first)
    commits = (second, first)
    assert _summary(_audit_history(paths, commits, tip=second)) == ((), ())
    unrelated_rules = {rule_class: ('unrelated-private-term',)}
    assert _summary(_audit_history(paths, commits, tip=second, rules=unrelated_rules)) == ((), ())

    full, focused = _audit.audit_history_pair(
        paths,
        _PROJECT,
        commits,
        {},
        (first,),
        {},
        {rule_class: (term, term)},
        f'{_ANON_NAME} <{_ANON_EMAIL}>',
        tip=second,
    )

    expected = tuple(
        _audit.AuditFinding(f'{oid[:12]}:header:{field}', rule_class, term)
        for oid in sorted(commits, key=lambda oid: oid[:12])
        for field in ('author', 'committer')
    )
    assert _summary(full) == (expected, ())
    assert _summary(focused) == (
        tuple(finding for finding in expected if finding.unit.startswith(first[:12])),
        (),
    )


def test_history_audit_reports_non_utc_committer(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    commit = _write_commit(
        paths,
        data=b'public\n',
        message=b'release\n',
        committer_offset='+0100',
    )

    result = _audit_history(paths, (commit,), tip=commit)

    assert len(result.identity_problems) == 1
    assert result.identity_problems[0].startswith(f'{commit[:12]}: ')


def test_history_audit_reports_only_invalid_author(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    commit = _write_commit(
        paths,
        author_name='Private Author',
        data=b'public\n',
        message=b'release\n',
    )

    result = _audit_history(paths, (commit,), tip=commit)

    assert _summary(result) == (
        (),
        (f'{commit[:12]}: invalid author identity',),
    )


def test_history_audit_reports_signed_commit(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    unsigned = _write_commit(paths, data=b'public\n', message=b'release\n')
    signed = _with_gpgsig(paths, unsigned)

    result = _audit_history(
        paths,
        (signed,),
        tip=signed,
        rules={RuleClass.IDENTITY_CRITICAL: ('fake', 'PGP SIGNATURE')},
    )

    assert _summary(result) == (
        (),
        (f'{signed[:12]}: commit is signed',),
    )


@pytest.mark.slow
def test_history_batch_matches_oracle_for_unchanged_and_shared_blobs(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    empty = _write_snapshot_commit(paths, (), b'empty root\n')
    shared = (
        Entry('copy-one.txt', 0o100644, 'blob', b'shared-secret\n'),
        Entry('nested/copy-two.txt', 0o100644, 'blob', b'shared-secret\n'),
    )
    stable = (
        Entry('binary.bin', 0o100644, 'blob', b'public\x00bytes\xff'),
        *shared,
        Entry('link', 0o120000, 'symlink', b'nested/private-path.txt'),
        Entry('mode.txt', 0o100644, 'blob', b'run me\n'),
        Entry('nested/private-path.txt', 0o100644, 'blob', b'inherited-secret\n'),
    )
    added = _write_snapshot_commit(paths, stable, b'add files\n', parent=empty)
    changed_snapshot = (
        Entry('binary.bin', 0o100644, 'blob', b'\x00removed-secret\xff'),
        *shared,
        Entry('link', 0o120000, 'symlink', b'mode.txt'),
        Entry('mode.txt', 0o100755, 'blob', b'run me\n'),
        Entry('nested/private-path.txt', 0o100644, 'blob', b'inherited-secret\n'),
        Entry('new.txt', 0o100644, 'blob', b'new\n'),
    )
    changed = _write_snapshot_commit(
        paths,
        changed_snapshot,
        b'hygiene-message\n',
        author_name='Private Author',
        parent=added,
    )
    deleted_snapshot = (
        *(
            entry
            for entry in changed_snapshot
            if entry.path not in {'binary.bin', 'link', 'mode.txt'}
        ),
        # Removing link makes this new target safe, even though link used to target mode.txt.
        Entry('mode.txt', 0o120000, 'symlink', b'link'),
    )
    deleted = _write_snapshot_commit(paths, deleted_snapshot, b'delete binary\n', parent=changed)
    readded_snapshot = (
        Entry('binary.bin', 0o100644, 'blob', b'public again\n'),
        Entry('link', 0o120000, 'symlink', b'mode.txt'),
        # Replacing the symlink with a file makes the reintroduced link safe.
        Entry('mode.txt', 0o100644, 'blob', b'run me\n'),
        *(entry for entry in deleted_snapshot if entry.path != 'mode.txt'),
    )
    readded = _write_snapshot_commit(
        paths,
        readded_snapshot,
        b're-add binary\n',
        parent=deleted,
    )
    unchanged = _write_snapshot_commit(
        paths,
        readded_snapshot,
        b'focused-message\n',
        committer_name='Private Committer',
        parent=readded,
    )
    commits = (unchanged, readded, deleted, changed, added, empty)
    focused_commits = (unchanged,)
    tags = {'private-tag': added, 'private-focus-tag': unchanged}
    focused_tags = {'private-focus-tag': unchanged}
    rules = {
        RuleClass.HYGIENE: ('focused-message', 'hygiene-message'),
        RuleClass.IDENTITY_CRITICAL: (
            'inherited-secret',
            'private-path',
            'private-tag',
            'removed-secret',
            'shared-secret',
        ),
    }
    expected_full = _slow_audit_history(paths, commits, tags, rules, tip=unchanged)
    expected_focused = _slow_audit_history(
        paths,
        focused_commits,
        focused_tags,
        rules,
        tip=unchanged,
    )

    full, focused = _audit.audit_history_pair(
        paths,
        _PROJECT,
        commits,
        tags,
        focused_commits,
        focused_tags,
        rules,
        f'{_ANON_NAME} <{_ANON_EMAIL}>',
        tip=unchanged,
    )

    assert _summary(full) == expected_full
    assert _summary(focused) == expected_focused
    assert (
        _audit.AuditFinding(
            'path:nested/private-path.txt',
            RuleClass.IDENTITY_CRITICAL,
            'inherited-secret',
        )
        in focused.findings
    )
    assert all(finding.term != 'removed-secret' for finding in focused.findings)
    assert tuple(
        finding.unit for finding in focused.findings if finding.term == 'shared-secret'
    ) == ('path:copy-one.txt', 'path:nested/copy-two.txt')


@pytest.mark.slow
def test_history_batch_process_count_does_not_scale_per_commit_or_blob(tmp_path: Path) -> None:
    counts: list[int] = []
    rules = {RuleClass.IDENTITY_CRITICAL: ('payload-canary', 'removed-canary')}
    # Vary history length and blobs per tree separately at the same directory depth.
    for commit_count, blob_count in ((2, 2), (16, 2), (2, 16)):
        paths = _paths(tmp_path / f'{commit_count}-{blob_count}')
        commits: list[str] = []
        for revision in range(commit_count):
            snapshot = tuple(
                Entry(
                    f'file-{index}.txt',
                    0o100644,
                    'blob',
                    f'payload-canary {revision}:{index}\n'.encode(),
                )
                for index in range(blob_count)
            )
            if revision == 0:
                snapshot += (Entry('removed.txt', 0o100644, 'blob', b'removed-canary\n'),)
            commits.insert(
                0,
                _write_snapshot_commit(
                    paths,
                    snapshot,
                    f'revision {revision}\n'.encode(),
                    parent=commits[0] if commits else None,
                ),
            )
        tip = commits[0]
        tags = {'v2.0.0': tip}
        expected_full = _slow_audit_history(paths, commits, tags, rules, tip=tip)
        expected_focused = _slow_audit_history(paths, (tip,), tags, rules, tip=tip)

        # Count the operation across module boundaries without fixing Git command syntax.
        with patch('subprocess.Popen', wraps=subprocess.Popen) as processes:
            full, focused = _audit.audit_history_pair(
                paths,
                _PROJECT,
                commits,
                tags,
                (tip,),
                tags,
                rules,
                f'{_ANON_NAME} <{_ANON_EMAIL}>',
                tip=tip,
            )
        counts.append(processes.call_count)

        assert _summary(full) == expected_full
        assert _summary(focused) == expected_focused
        removed = _audit.AuditFinding(
            'path:removed.txt', RuleClass.IDENTITY_CRITICAL, 'removed-canary'
        )
        assert removed in full.findings
        assert removed not in focused.findings
        assert len(focused.findings) == blob_count

    # Permit a constant extra batch, but reject a child process for each object.
    assert counts[0] <= 6
    assert max(counts[1:]) <= counts[0] + 2


def test_history_batch_refuses_gitlink_from_raw_diff(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    root = _write_snapshot_commit(paths, (), b'empty root\n')
    store = store_path(paths, _PROJECT)
    tree = _oid(
        run_git(
            ['mktree', '-z'], cwd=store, input_bytes=f'160000 commit {root}\tsubmodule\0'.encode()
        ).stdout
    )
    gitlink = _oid(
        run_git(
            ['commit-tree', tree, '-p', root],
            cwd=store,
            input_bytes=b'gitlink\n',
            env_extra={
                'GIT_AUTHOR_NAME': _ANON_NAME,
                'GIT_AUTHOR_EMAIL': _ANON_EMAIL,
                'GIT_AUTHOR_DATE': '1700000000 +0000',
                'GIT_COMMITTER_NAME': _ANON_NAME,
                'GIT_COMMITTER_EMAIL': _ANON_EMAIL,
                'GIT_COMMITTER_DATE': '1700000000 +0000',
            },
        ).stdout
    )

    with pytest.raises(PubanonError, match='unsupported gitlink') as exc_info:
        _audit_history(paths, (gitlink, root), tip=gitlink)

    assert exc_info.value.recovery is not None
    assert 'commit without the submodule' in exc_info.value.recovery
    assert 'vendor its content' in exc_info.value.recovery


@pytest.mark.parametrize(
    ('mutation', 'error'),
    [
        ('bad-metadata', 'Git returned a malformed changed entry'),
        ('bad-oid', 'Git returned a mismatched published commit boundary'),
        ('duplicate', 'Git returned duplicate changed entries'),
        ('missing', 'Git returned an unterminated published history diff'),
        ('missing-path', 'Git returned a changed entry without a path'),
        ('nonzero', 'published history diff read failed'),
        ('trailing', 'Git returned trailing published history diff data'),
    ],
)
def test_history_batch_refuses_malformed_diff_stream(
    mutation: str,
    error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    commit = _write_snapshot_commit(
        paths,
        (Entry('file.txt', 0o100644, 'blob', b'data\n'),),
        b'commit\n',
    )
    real_run_git = _audit.run_git

    def _malformed_run_git(args: Sequence[str], **kwargs: Unpack[_RunGitOptions]) -> GitResult:
        result = real_run_git(args, **kwargs)
        if args[2:4] != ['diff-tree', '--stdin']:
            return result
        parts = result.stdout.split(b'\0')
        if mutation == 'bad-metadata':
            parts[1] = b':bad'
            output = b'\0'.join(parts)
        elif mutation == 'bad-oid':
            output = b'x' + result.stdout[1:]
        elif mutation == 'duplicate':
            output = b'\0'.join((*parts[:-1], parts[1], parts[2], b''))
        elif mutation == 'missing':
            output = b''
        elif mutation == 'missing-path':
            output = b'\0'.join((parts[0], parts[1], b''))
        elif mutation == 'nonzero':
            return GitResult(1, result.stdout, b'private-diff-canary\x1b\\path')
        else:
            output = result.stdout + b'trailing\0'
        return GitResult(0, output, b'')

    monkeypatch.setattr(_audit, 'run_git', _malformed_run_git)

    with pytest.raises(PubanonError, match=error) as exc_info:
        _audit_history(paths, (commit,), tip=commit)

    assert (
        mutation != 'nonzero'
        or exc_info.value.fact == r'exit status 1: private-diff-canary\x1b\\path'
    )


@pytest.mark.parametrize(
    ('mutation', 'error'),
    [
        ('bad-header', 'Git returned a malformed published object header'),
        ('bad-oid', 'Git returned a mismatched published object'),
        ('bad-size', 'Git returned a malformed published object size'),
        ('missing', 'Git returned an unterminated published object header'),
        ('nonzero', 'published object read failed'),
        ('trailing', 'Git returned trailing published object data'),
        ('truncated', 'Git returned a truncated published object'),
        ('wrong-type', 'published commit object could not be read'),
    ],
)
def test_history_batch_refuses_malformed_object_stream(
    mutation: str,
    error: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    commit = _write_snapshot_commit(
        paths,
        (Entry('file.txt', 0o100644, 'blob', b'data\n'),),
        b'commit\n',
    )
    real_run_git = _audit.run_git

    def _malformed_run_git(args: Sequence[str], **kwargs: Unpack[_RunGitOptions]) -> GitResult:
        result = real_run_git(args, **kwargs)
        if args[2:] != ['cat-file', '--batch']:
            return result
        header_end = result.stdout.index(b'\n')
        header = result.stdout[:header_end]
        if mutation == 'bad-header':
            output = header.replace(b' ', b'', 1) + result.stdout[header_end:]
        elif mutation == 'bad-oid':
            output = b'x' + result.stdout[1:]
        elif mutation == 'bad-size':
            fields = header.split(b' ')
            output = b' '.join((*fields[:2], b'x')) + result.stdout[header_end:]
        elif mutation == 'missing':
            output = b''
        elif mutation == 'nonzero':
            return GitResult(1, result.stdout, b'private-object-canary\x1b\\path')
        elif mutation == 'trailing':
            output = result.stdout + b'trailing'
        elif mutation == 'truncated':
            output = result.stdout[:-1]
        else:
            output = header.replace(b' commit ', b' blob ', 1) + result.stdout[header_end:]
        return GitResult(0, output, b'')

    monkeypatch.setattr(_audit, 'run_git', _malformed_run_git)

    with pytest.raises(PubanonError, match=error) as exc_info:
        _audit_history(paths, (commit,), tip=commit)

    assert (
        mutation != 'nonzero'
        or exc_info.value.fact == r'exit status 1: private-object-canary\x1b\\path'
    )


@pytest.mark.parametrize(
    ('previous', 'changed', 'problem', 'path'),
    [
        (
            (Entry('link', 0o120000, 'symlink', b'target.txt'),),
            (Entry('link', 0o120000, 'symlink', b'/target.txt'),),
            'absolute',
            'link',
        ),
        (
            (),
            (Entry('link', 0o120000, 'symlink', b'/target.txt'),),
            'absolute',
            'link',
        ),
        (
            (Entry('link', 0o100644, 'blob', b'/target.txt'),),
            (Entry('link', 0o120000, 'symlink', b'/target.txt'),),
            'absolute',
            'link',
        ),
        (
            (Entry('a-link', 0o120000, 'symlink', b'z-link'),),
            (
                Entry('a-link', 0o120000, 'symlink', b'z-link'),
                Entry('z-link', 0o120000, 'symlink', b'target.txt'),
            ),
            'unresolvable',
            'a-link',
        ),
        (
            (Entry('z-link', 0o120000, 'symlink', b'target.txt'),),
            (
                Entry('a-link', 0o120000, 'symlink', b'/target.txt'),
                Entry('z-link', 0o120000, 'symlink', b'/target.txt'),
            ),
            'absolute',
            'a-link',
        ),
    ],
    ids=('retarget', 'introduction', 'mode-transition', 'unchanged-link', 'path-order'),
)
def test_history_batch_refuses_unsafe_symlink_from_reconstructed_history(
    tmp_path: Path,
    previous: Snapshot,
    changed: Snapshot,
    problem: str,
    path: str,
) -> None:
    paths = _paths(tmp_path)
    safe = _write_snapshot_commit(
        paths,
        (
            *previous,
            Entry('target.txt', 0o100644, 'blob', b'public\n'),
        ),
        b'safe link\n',
    )
    unsafe = _write_snapshot_commit(
        paths,
        (
            *changed,
            Entry('target.txt', 0o100644, 'blob', b'public\n'),
        ),
        b'unsafe link\n',
        parent=safe,
    )

    with pytest.raises(PubanonError, match=f'{problem} symlink target') as exc_info:
        _audit_history(paths, (unsafe, safe), tip=unsafe)

    assert exc_info.value.fact == f'symlink: {path}'
    assert exc_info.value.recovery == 'replace the unsafe symlink and rerun'


@pytest.mark.parametrize(
    ('raw_path', 'raw_mode', 'grammar'),
    [
        (b'bad-\xff.txt', b'100644', 'path'),
        (b'../private.txt', b'100644', 'path'),
        (b'vendor', b'160000', 'mode'),
        (b'file.txt', b'100600', 'mode'),
    ],
)
def test_history_changes_share_source_tree_grammar(
    raw_path: bytes,
    raw_mode: bytes,
    grammar: str,
) -> None:

    if grammar == 'path':
        with pytest.raises(PubanonError) as source_error:
            _source.decode_tree_path(raw_path)
    else:
        with pytest.raises(PubanonError) as source_error:
            _source.tree_mode(raw_mode, raw_path.decode())
    oid = b'a' * 40
    metadata = b':000000 ' + raw_mode + b' ' + (b'0' * 40) + b' ' + oid + b' A'

    with pytest.raises(PubanonError) as history_error:
        _audit._parse_change(metadata, raw_path, 40)

    assert history_error.value.render() == source_error.value.render()


@pytest.mark.parametrize(
    ('label', 'data', 'term', 'description'),
    [
        ('blob', b'first\r\nblocked context\r\n', 'blocked', 'notes.txt:2; text blocked context'),
        (
            'message',
            b'subject\n\nblocked detail\n',
            'blocked',
            'commit message line 3; text blocked detail',
        ),
        ('filename', b'blocked.txt', 'blocked', 'filename; text blocked.txt'),
        ('tag name', b'blocked-tag', 'blocked', 'tag name; text blocked-tag'),
        ('header x-note value', b'blocked', 'blocked', 'header x-note value; text blocked'),
        ('blob', b'\x00blocked', 'blocked', 'blob bytes 1:8'),
        ('blob', b'\xffblocked', 'blocked', 'blob bytes 1:8'),
        (
            'blob',
            'first\ncafe\u0301 context'.encode(),
            'caf\u00e9',
            'notes.txt:2; text cafe\u0301 context',
        ),
        ('blob', b'first\nblocked\x1b[31m', 'blocked', 'notes.txt:2; text blocked\x1b[31m'),
        (
            'blob',
            b'x' * 100 + b'blocked' + b'y' * 100,
            'blocked',
            'notes.txt:1; text ...' + 'x' * 60 + 'blocked' + 'y' * 60 + '...',
        ),
    ],
)
def test_history_evidence_describes_text_and_retains_binary_offsets(
    label: str, data: bytes, term: str, description: str
) -> None:
    findings: dict[_audit.AuditFinding, _audit.HistoryEvidence] = {}

    _audit._add_findings(
        findings, 'path:notes.txt', data, {RuleClass.IDENTITY_CRITICAL: (term,)}, ('a' * 40, label)
    )

    assert len(findings) == 1
    assert next(iter(findings.values())).description == description


def test_history_evidence_identifies_filename_blob_message_header_and_tag_bytes(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    path = 'path-canary-shared-canary.txt'
    original = _write_commit(
        paths, path=path, data=b'blob-canary shared-canary\n', message=b'message-canary\n'
    )
    headers, message = _commit_object(paths, original)
    commit = _write_literal_commit(paths, headers + b'\nx-note header-canary', message)
    terms = (
        'path-canary',
        'shared-canary',
        'blob-canary',
        'message-canary',
        'header-canary',
        'tag-canary',
        _ANON_NAME,
    )
    full, focused = _audit.audit_history_pair(
        paths,
        _PROJECT,
        (commit,),
        {'tag-canary': commit},
        (commit,),
        {'tag-canary': commit},
        {RuleClass.IDENTITY_CRITICAL: terms},
        f'{_ANON_NAME} <{_ANON_EMAIL}>',
        tip=commit,
    )
    assert full == focused
    assert len(full.evidence) == len(full.findings) == 8
    for item in full.evidence:
        assert item.commit == commit
        assert item.commit is not None
        unit, offsets = item.location.rsplit(' bytes ', 1)
        start, end = map(int, offsets.split(':'))
        actual_headers, actual_message = _commit_object(paths, item.commit)
        if unit == 'filename':
            snapshot = read_snapshot(store_path(paths, _PROJECT), item.commit)
            data = next(entry.path.encode() for entry in snapshot if entry.path == path)
        elif unit == 'blob':
            data = run_git(
                ['show', f'{item.commit}:{path}'], cwd=store_path(paths, _PROJECT)
            ).stdout
        elif unit == 'message':
            data = actual_message
        elif unit == 'tag name':
            data = item.finding.unit.removeprefix('tag:').encode()
        else:
            field = unit.removeprefix('header ').removesuffix(' value').encode()
            data = next(
                record.removeprefix(field + b' ')
                for record in actual_headers.split(b'\n')
                if record.startswith(field + b' ')
            )
        assert data[start:end] == item.finding.term.encode()
        if item.finding.term == 'shared-canary':
            assert unit == 'filename'


def test_identity_only_audit_reads_only_required_object_bodies(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    store = store_path(paths, _PROJECT)
    snapshot = (
        Entry('file.txt', 0o100644, 'blob', b'ordinary content'),
        Entry('executable', 0o100755, 'blob', b'executable content'),
        Entry('shared.txt', 0o100644, 'blob', b'file.txt'),
        Entry('link', 0o120000, 'symlink', b'file.txt'),
    )
    commit = _write_snapshot_commit(paths, snapshot, b'commit\n')
    tag = _oid(
        run_git(
            ['hash-object', '-w', '-t', 'tag', '--stdin'],
            cwd=store,
            input_bytes=(
                f'object {commit}\ntype commit\ntag v1\n'
                f'tagger {_ANON_NAME} <{_ANON_EMAIL}> 1700000000 +0000\n\nannotation\n'
            ).encode(),
        ).stdout
    )
    blobs = tuple(
        run_git(['hash-object', '--stdin'], cwd=store, input_bytes=entry.data).stdout.strip()
        for entry in snapshot
    )
    with (
        patch.object(_audit, 'read_objects', wraps=_audit.read_objects) as reads,
        patch.object(_audit, 'run_git', wraps=_audit.run_git) as commands,
    ):
        result = _audit_history(paths, (commit,), tip=commit, tags={'v1': tag})
    requested = [oid for call in reads.call_args_list for oid in call.args[1]]
    checked = [
        call.kwargs['input_bytes'].splitlines()
        for call in commands.call_args_list
        if call.args[0][2:] == ['cat-file', '--batch-check']
    ]

    assert result.findings == ()
    assert result.identity_problems == ('tag:v1: tag does not point directly to a commit',)
    assert requested == [commit.encode(), tag.encode(), blobs[-1]]
    assert blobs[0] not in requested
    assert blobs[1] not in requested
    assert len(checked) == 1
    assert set(checked[0]) == set(blobs[:2])
    assert len(checked[0]) == 2


@pytest.mark.parametrize(
    ('mutation', 'error'),
    [
        ('missing-object', 'published blob object could not be read'),
        ('wrong-type', 'published blob object could not be read'),
        ('bad-header', 'malformed published object header'),
        ('bad-oid', 'mismatched published object'),
        ('bad-type', 'unsupported published object type'),
        ('bad-size', 'malformed published object size'),
        ('unterminated', 'unterminated published object header'),
        ('trailing', 'trailing published object data'),
        ('nonzero', 'published object read failed'),
    ],
)
def test_identity_only_audit_validates_blob_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mutation: str, error: str
) -> None:
    paths = _paths(tmp_path)
    commit = _write_commit(paths, data=b'ordinary content', message=b'commit\n')
    real_run_git = _audit.run_git

    def _malformed_run_git(args: Sequence[str], **kwargs: Unpack[_RunGitOptions]) -> GitResult:
        result = real_run_git(args, **kwargs)
        if args[2:] != ['cat-file', '--batch-check']:
            return result
        oid, object_type, size = result.stdout.rstrip(b'\n').split(b' ')
        if mutation == 'nonzero':
            return GitResult(1, b'', b'object read failed')
        output = {
            'missing-object': oid + b' missing\n',
            'wrong-type': b' '.join((oid, b'tree', size)) + b'\n',
            'bad-header': oid + b'\n',
            'bad-oid': b'x' + result.stdout[1:],
            'bad-type': b' '.join((oid, b'unknown', size)) + b'\n',
            'bad-size': b' '.join((oid, object_type, b'-1')) + b'\n',
            'unterminated': result.stdout[:-1],
            'trailing': result.stdout + b'extra\n',
        }[mutation]
        return GitResult(0, output, b'')

    monkeypatch.setattr(_audit, 'run_git', _malformed_run_git)
    with pytest.raises(PubanonError, match=error):
        _audit_history(paths, (commit,), tip=commit)


def test_published_commits_with_old_returns_only_newer_commits(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    first = _write_commit(paths, data=b'first\n', message=b'first\n')
    second = _write_commit(
        paths,
        data=b'second\n',
        message=b'second\n',
        parent=first,
    )

    assert _audit.published_commits(paths, _PROJECT, new=second, old=first) == (second,)


def test_read_objects_refuses_damaged_store_inside_ancestor_repository(
    make_repo: Callable[..., Path],
) -> None:
    ancestor = make_repo()
    store = ancestor / 'store.git'
    store.mkdir()
    commit = run_git(['rev-parse', 'HEAD'], cwd=ancestor).stdout.strip()

    with pytest.raises(PubanonError, match='published object read failed') as error:
        _audit.read_objects(store, [commit])

    assert 'not a git repository' in error.value.render()
