import os
import stat
import tempfile
import tomllib
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pubanon import _filesystem, _windows_fs
from pubanon._errors import PubanonError
from pubanon._run import GitResult, run_git
from pubanon._snapshots import _conflicts
from pubanon._snapshots._conflicts import (
    MergeResult,
    expand_conflicts,
    export_conflicts,
    parse_merge_result,
    read_resolutions,
)
from pubanon._snapshots._records import ConflictStage, RewritePause
from pubanon._snapshots._source import read_snapshot
from pubanon._toml import toml_string
from tests._git import git_env


def _decide(directory: Path, actions: dict[str, str]) -> None:
    manifest = directory / 'resolutions.toml'
    text = manifest.read_text()
    # Each repeated path gets the same declaration.
    for path, action in actions.items():
        text = text.replace(
            f'path = {toml_string(path)}\naction = "unresolved"',
            f'path = {toml_string(path)}\n{action}',
        )
    manifest.write_text(text)


def _files(store: Path, tree: str) -> dict[str, tuple[int, bytes]]:
    return {entry.path: (entry.mode, entry.data) for entry in read_snapshot(store, tree)}


def _tree(store: Path, files: Mapping[str, bytes | tuple[int, bytes]]) -> str:
    with tempfile.TemporaryDirectory() as temporary:
        env = {'GIT_INDEX_FILE': str(Path(temporary) / 'index')}
        run_git(['read-tree', '--empty'], cwd=store, env_extra=env)
        entries = b''
        for path, value in files.items():
            mode, data = value if isinstance(value, tuple) else (0o100644, value)
            oid = run_git(
                ['hash-object', '-w', '--stdin'], cwd=store, input_bytes=data
            ).stdout.strip()
            entries += f'{mode:o} '.encode() + oid + b'\t' + os.fsencode(path) + b'\0'
        run_git(
            ['update-index', '-z', '--index-info'], cwd=store, env_extra=env, input_bytes=entries
        )
        return run_git(['write-tree'], cwd=store, env_extra=env).stdout.decode().strip()


def _merge(
    tmp_path: Path,
    base: Mapping[str, bytes | tuple[int, bytes]],
    ours: Mapping[str, bytes | tuple[int, bytes]],
    theirs: Mapping[str, bytes | tuple[int, bytes]],
) -> tuple[Path, RewritePause]:
    store = tmp_path / 'store.git'
    run_git(['init', '--bare', str(store)], cwd=None, env_extra=git_env())
    commits: list[str] = []
    for files in (base, ours, theirs):
        tree = _tree(store, files)
        parent = ['-p', commits[0]] if commits else []
        commits.append(
            run_git(
                ['commit-tree', tree, *parent],
                cwd=store,
                input_bytes=b'fixture\n',
                env_extra=git_env(),
            )
            .stdout.decode()
            .strip()
        )
    result = parse_merge_result(
        run_git(
            [
                'merge-tree',
                '--write-tree',
                '--merge-base',
                commits[0],
                '-z',
                '--messages',
                *commits[1:],
            ],
            cwd=store,
            check=False,
        )
    )
    assert result.conflicted
    return store, RewritePause(commits[-1], result.tree, expand_conflicts(store, result))


def test_binary_groups_require_consistent_decisions_and_regular_versions(tmp_path: Path) -> None:
    store, pause = _merge(tmp_path, {'f': b'\0base'}, {'f': b'\0ours'}, {'f': b'\0theirs'})
    assert len(pause.conflicts) == 2
    assert all(group.paths == ('f',) for group in pause.conflicts)
    directory = tmp_path / 'rewrite'
    export_conflicts(store, directory, pause)
    manifest = directory / 'resolutions.toml'
    manifest.write_text(
        manifest.read_text()
        .replace('action = "unresolved"', 'action = "keep"', 1)
        .replace('action = "unresolved"', 'action = "delete"')
    )
    with pytest.raises(PubanonError, match='inconsistent'):
        read_resolutions(store, directory, pause)
    export_conflicts(store, directory, pause)
    _decide(directory, {'f': 'action = "keep"'})
    (directory / 'tree/f').write_bytes(b'\0<<<<<<< chosen\xff')
    tree, decisions = read_resolutions(store, directory, pause)
    assert _files(store, tree) == {'f': (0o100644, b'\0<<<<<<< chosen\xff')}
    assert len(decisions) == 1
    assert sorted(path.name for path in (directory / 'versions').iterdir()) == ['0000']
    assert (directory / 'versions/0000/base').read_bytes() == b'\0base'


@pytest.mark.skipif(os.name == 'nt', reason='POSIX directory traversal')
def test_conflicts_accept_trusted_ancestor_symlink(tmp_path: Path) -> None:
    store, pause = _merge(tmp_path, {'f': b'base'}, {'f': b'ours'}, {'f': b'theirs'})
    trusted = tmp_path / 'trusted'
    trusted.mkdir()
    alias = tmp_path / 'alias'
    alias.symlink_to(trusted, target_is_directory=True)
    directory = alias / 'rewrite'
    export_conflicts(store, directory, pause)
    (directory / 'tree/f').write_bytes(b'resolved\n')
    _decide(directory, {'f': 'action = "keep"'})
    tree, _ = read_resolutions(store, directory, pause)
    assert _files(store, tree) == {'f': (0o100644, b'resolved\n')}


def test_directory_file_replacement_requires_explicit_child_decision(tmp_path: Path) -> None:
    store, pause = _merge(tmp_path, {}, {'d': b'file\n'}, {'d/child': (0o100755, b'child\n')})
    group = pause.conflicts[0]
    assert 'd' in group.paths
    assert 'd' not in group.decision_paths
    assert 'd/child' in group.decision_paths
    relocated = next(path for path in group.decision_paths if path != 'd/child')
    directory = tmp_path / 'rewrite'
    export_conflicts(store, directory, pause)
    _decide(directory, {relocated: 'rename = "d"', 'd/child': 'action = "delete"'})
    (directory / 'tree/d/child').unlink()
    (directory / 'tree/d').rmdir()
    (directory / 'tree/d').write_bytes(b'replacing directory\n')
    tree, _ = read_resolutions(store, directory, pause)
    assert _files(store, tree) == {'d': (0o100644, b'replacing directory\n')}


@pytest.mark.parametrize('action', ['keep', 'delete', 'rename'])
def test_directory_split_expands_saved_leaves(tmp_path: Path, action: str) -> None:
    store, pause = _merge(
        tmp_path,
        {'old/a': b'a\n', 'old/b': b'b\n'},
        {'one/a': b'a\n', 'two/b': b'b\n'},
        {'old/a': b'a\n', 'old/b': b'b\n', 'old/c': b'c\n'},
    )
    assert pause.conflicts[0].paths == ('old',)
    assert pause.conflicts[0].stages == ()
    assert pause.conflicts[0].decision_paths == ('old/c',)
    directory = tmp_path / 'rewrite'
    export_conflicts(store, directory, pause)
    declaration = 'rename = "one/c"' if action == 'rename' else f'action = "{action}"'
    _decide(directory, {'old/c': declaration})
    if action == 'rename':
        (directory / 'tree/old/c').rename(directory / 'tree/one/c')
    tree, _ = read_resolutions(store, directory, pause)
    expected = {'one/a': (0o100644, b'a\n'), 'two/b': (0o100644, b'b\n')}
    if action != 'delete':
        expected['one/c' if action == 'rename' else 'old/c'] = (0o100644, b'c\n')
    assert _files(store, tree) == expected
    invalid = replace(
        pause, conflicts=(replace(pause.conflicts[0], decision_paths=('invented',)),)
    )
    with pytest.raises(PubanonError, match='decision'):
        read_resolutions(store, directory, invalid)


def test_empty_frozen_decisions_and_unrepresentable_conflict_are_refused(tmp_path: Path) -> None:
    store, pause = _merge(tmp_path, {'f': b'base'}, {'f': b'ours'}, {'f': b'theirs'})
    with pytest.raises(PubanonError, match='decision'):
        expand_conflicts(store, MergeResult(pause.tree, conflicted=True, conflicts=()))
    invalid = replace(pause, conflicts=(replace(pause.conflicts[0], decision_paths=()),))
    with pytest.raises(PubanonError, match='decision'):
        export_conflicts(store, tmp_path / 'rewrite', invalid)
    assert not (tmp_path / 'rewrite').exists()


def test_export_installs_flushed_manifest_through_durable_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, pause = _merge(tmp_path, {'f': b'base'}, {'f': b'ours'}, {'f': b'theirs'})
    directory = tmp_path / 'rewrite'
    flushed: list[int] = []
    installed: list[Path] = []
    fsync = os.fsync

    def _record_flush(descriptor: int) -> None:
        flushed.append(os.fstat(descriptor).st_ino)
        fsync(descriptor)

    def _install(source: Path, destination: Path) -> None:
        assert source != destination
        assert source.stat().st_ino in flushed
        assert tomllib.loads(source.read_text())['tree'] == pause.tree
        assert destination == directory / 'resolutions.toml'
        source.replace(destination)
        installed.append(destination)

    monkeypatch.setattr(os, 'fsync', _record_flush)
    monkeypatch.setattr(_filesystem, 'replace', _install)
    export_conflicts(store, directory, pause)
    assert installed == [directory / 'resolutions.toml']
    assert not (directory / 'resolutions.toml.tmp').exists()


@pytest.mark.parametrize(
    'change',
    [
        'path',
        'type',
        'stage',
        'extra',
        'unknown',
        'missing',
        'occupied',
        'duplicate',
        'traversal',
        'parent_symlink',
    ],
)
def test_invalid_resolution_preserves_store_and_export(tmp_path: Path, change: str) -> None:
    store, pause = _merge(
        tmp_path,
        {'f': b'base\n', 'g': b'base\n', 'stable': b'stable'},
        {'f': b'ours\n', 'g': b'ours\n', 'stable': b'stable'},
        {'f': b'theirs\n', 'g': b'theirs\n', 'stable': b'stable'},
    )
    directory = tmp_path / 'rewrite'
    export_conflicts(store, directory, pause)
    (directory / 'tree/f').write_bytes(b'resolved')
    (directory / 'tree/g').write_bytes(b'resolved')
    actions = {'f': 'action = "keep"', 'g': 'action = "keep"'}
    action_overrides = {
        'occupied': 'rename = "stable"',
        'duplicate': 'rename = "g"',
        'traversal': 'rename = "../escape"',
        'unknown': 'action = "guess"',
    }
    actions['f'] = action_overrides.get(change, actions['f'])
    if change == 'parent_symlink':
        outside = tmp_path / 'outside'
        outside.mkdir()
        (outside / 'chosen').write_bytes(b'outside')
        (directory / 'tree/parent').symlink_to(outside, target_is_directory=True)
        actions['f'] = 'rename = "parent/chosen"'
    _decide(directory, actions)
    manifest = directory / 'resolutions.toml'
    text = manifest.read_text()
    if change == 'path':
        text = text.replace('decision_paths = ["f"]', 'decision_paths = ["invented"]')
    elif change == 'type':
        text = text.replace('type = "CONFLICT (contents)"', 'type = "invented"')
    elif change == 'stage':
        text = text.replace('mode = 33188', 'mode = 33261')
    elif change == 'extra':
        text += '\n[[conflicts.decisions]]\npath = "extra"\naction = "delete"\n'
    elif change == 'missing':
        (directory / 'tree/f').unlink()
    manifest.write_text(text)
    before = run_git(['show-ref'], cwd=store, check=False)
    with pytest.raises(PubanonError):
        read_resolutions(store, directory, pause)
    assert manifest.read_text() == text
    assert run_git(['show-ref'], cwd=store, check=False) == before
    assert not (store / 'index').exists()
    if change == 'parent_symlink':
        assert (tmp_path / 'outside/chosen').read_bytes() == b'outside'


def test_manifest_refuses_invented_metadata_types(tmp_path: Path) -> None:
    store, pause = _merge(tmp_path, {'f': b'base'}, {'f': b'ours'}, {'f': b'theirs'})
    directory = tmp_path / 'rewrite'
    export_conflicts(store, directory, pause)
    _decide(directory, {'f': 'action = "delete"'})
    manifest = directory / 'resolutions.toml'
    manifest.write_text(manifest.read_text().replace('mode = 33188', 'mode = 33188.0'))
    with pytest.raises(PubanonError, match='metadata'):
        read_resolutions(store, directory, pause)


def test_mixed_merge_preserves_clean_auto_merge_and_reports_ignored_edit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store, pause = _merge(
        tmp_path,
        {'bad': b'base\n', 'good': b'1\n2\n3\n4\n5\n'},
        {'bad': b'ours\n', 'good': b'one\n2\n3\n4\n5\n'},
        {'bad': b'theirs\n', 'good': b'1\n2\n3\n4\nfive\n'},
    )
    assert {path for group in pause.conflicts for path in group.decision_paths} == {'bad'}
    directory = tmp_path / 'rewrite'
    export_conflicts(store, directory, pause)
    _decide(directory, {'bad': 'action = "delete"'})
    (directory / 'tree/good').write_bytes(b'ignored')
    tree, _ = read_resolutions(store, directory, pause)
    assert _files(store, tree) == {'good': (0o100644, b'one\n2\n3\n4\nfive\n')}
    assert 'Ignoring undeclared conflict export edit: good' in capsys.readouterr().out


@pytest.mark.parametrize('action', ['keep', 'delete'])
def test_modify_delete_requires_explicit_deletion(tmp_path: Path, action: str) -> None:
    store, pause = _merge(tmp_path, {'f': b'base\n'}, {}, {'f': b'changed\n'})
    directory = tmp_path / 'rewrite'
    export_conflicts(store, directory, pause)
    _decide(directory, {'f': f'action = "{action}"'})
    (directory / 'tree/f').unlink()
    if action == 'keep':
        with pytest.raises(PubanonError, match='existing'):
            read_resolutions(store, directory, pause)
    else:
        tree, _ = read_resolutions(store, directory, pause)
        assert _files(store, tree) == {}


def test_names_modes_and_repository_manifest_are_literal(tmp_path: Path) -> None:
    paths = ['space file', "quote'file"]
    if os.name != 'nt':
        paths.extend(['quote"file', 'line\nfile', 'tab\tfile'])
    base: dict[str, tuple[int, bytes]] = dict.fromkeys(paths, (0o100755, b'base\n'))
    ours: dict[str, tuple[int, bytes]] = dict.fromkeys(paths, (0o100755, b'ours\n'))
    theirs: dict[str, tuple[int, bytes]] = dict.fromkeys(paths, (0o100755, b'theirs\n'))
    for files in (base, ours, theirs):
        files['resolutions.toml'] = (0o100644, b'ordinary\n')
    store, pause = _merge(tmp_path, base, ours, theirs)
    directory = tmp_path / 'rewrite'
    export_conflicts(store, directory, pause)
    assert (directory / 'tree/resolutions.toml').read_bytes() == b'ordinary\n'
    _decide(directory, dict.fromkeys(paths, 'action = "keep"'))
    for path in paths:
        (directory / 'tree' / path).write_bytes(b'literal\r\n\x80')
    tree, _ = read_resolutions(store, directory, pause)
    assert _files(store, tree) == {
        **dict.fromkeys(paths, (0o100755, b'literal\r\n\x80')),
        'resolutions.toml': (0o100644, b'ordinary\n'),
    }


def test_open_directory_closes_descriptors_on_descent_failure(tmp_path: Path) -> None:
    if os.name == 'nt':
        pytest.skip('POSIX directory traversal')
    child = tmp_path / 'child'
    child.mkdir()
    descriptors = [os.open(path, os.O_RDONLY | os.O_DIRECTORY) for path in (tmp_path, child)]
    with (
        patch.object(os, 'open', side_effect=[*descriptors, OSError('descent failed')]),
        pytest.raises(OSError, match='descent failed'),
    ):
        _conflicts._open_directory(child / 'missing', trusted_parent=tmp_path)
    for descriptor in descriptors:
        with pytest.raises(OSError, match='Bad file descriptor'):
            os.fstat(descriptor)


@pytest.mark.skipif(os.name == 'nt', reason='POSIX directory traversal')
@pytest.mark.parametrize('path', ['outside', 'owned/../outside'])
def test_open_directory_rejects_paths_outside_anchor(tmp_path: Path, path: str) -> None:
    with (
        patch.object(os, 'open') as opened,
        pytest.raises(ValueError, match=r'subpath|outside its trusted parent'),
    ):
        _conflicts._open_directory(tmp_path / path, trusted_parent=tmp_path / 'owned')
    opened.assert_not_called()


def test_parser_attaches_unnamed_stage_and_rejects_unsupported_mode() -> None:
    oid = b'a' * 40
    raw = oid + b'\0' + b'100644 ' + oid + b' 2\tf\0\0'
    result = parse_merge_result(GitResult(1, raw, b''))
    assert result.conflicts[0].conflict_type == 'contents'
    with pytest.raises(PubanonError, match='gitlink'):
        parse_merge_result(GitResult(1, raw.replace(b'100644', b'160000'), b''))


def test_parser_keeps_grouped_stages_and_ignores_success_messages() -> None:
    path = 'line file' if os.name == 'nt' else 'line\nfile'
    oid = b'a' * 40
    raw = oid + b'\0' + b'100644 ' + oid + b' 2\t' + path.encode() + b'\0\0'
    raw += b'1\0good\0Auto-merging\0merged\0'
    raw += b'2\0' + path.encode() + b'\0missing\0CONFLICT (rename)\0diagnostic\0'
    result = parse_merge_result(GitResult(1, raw, b''))
    assert len(result.conflicts) == 1
    assert result.conflicts[0].paths == (path, 'missing')
    assert result.conflicts[0].stages[0].path == path


@pytest.mark.parametrize(
    'payload',
    [
        b'',
        b'a' * 40,
        b'a' * 40 + b'\0',
        b'a' * 40 + b'\0' + b'100644 ' + b'b' * 40 + b' 4\tf\0\0',
        b'a' * 40 + b'\0\0' + b'2\0f\0CONFLICT\0message\0',
    ],
)
def test_parser_rejects_malformed_output(payload: bytes) -> None:
    with pytest.raises(PubanonError):
        parse_merge_result(GitResult(1, payload, b''))


def test_parser_reports_failed_command_with_bounded_output() -> None:
    with pytest.raises(PubanonError) as error:
        parse_merge_result(GitResult(128, b'x' * 10000, b'\x1b' * 10000))
    assert len(error.value.render()) < 5000


def test_parser_retains_empty_conflict_result() -> None:
    result = parse_merge_result(GitResult(1, b'a' * 40 + b'\0\0', b''))
    assert result.conflicted
    assert result.conflicts == ()


def test_prefix_stage_paths_use_distinct_numbered_slots(tmp_path: Path) -> None:
    store, pause = _merge(
        tmp_path,
        {'f': b'original\n'},
        {'f/base': (0o100755, b'original\n')},
        {'other': b'original\n'},
    )
    group = pause.conflicts[0]
    oid = (
        run_git(['hash-object', '-w', '--stdin'], cwd=store, input_bytes=b'original\n')
        .stdout.decode()
        .strip()
    )
    # Explicit synthetic export record exercises exact-stage-plus-descendants semantics.
    group = replace(group, stages=(*group.stages, ConflictStage('f', 0o100644, oid, 1)))
    pause = replace(
        pause,
        conflicts=expand_conflicts(
            store, MergeResult(pause.tree, conflicted=True, conflicts=(group,))
        ),
    )
    directory = tmp_path / 'rewrite'
    export_conflicts(store, directory, pause)
    manifest = tomllib.loads((directory / 'resolutions.toml').read_text())
    versions = {item['path']: item for item in manifest['conflicts'][0]['versions']}
    assert versions['f']['file'] == 'versions/0000/base'
    assert versions['f/base']['file'] == 'versions/0001/ours'
    assert versions['other']['file'] == 'versions/0002/theirs'
    _decide(
        directory,
        {'f': 'action = "delete"', 'f/base': 'action = "keep"', 'other': 'action = "delete"'},
    )
    tree, _ = read_resolutions(store, directory, pause)
    assert _files(store, tree) == {'f/base': (0o100755, b'original\n')}


def test_reexport_restores_partial_copy_and_preserves_awaiting_edits(tmp_path: Path) -> None:
    store, pause = _merge(tmp_path, {'f': b'base'}, {'f': b'ours'}, {'f': b'theirs'})
    directory = tmp_path / 'rewrite'
    directory.mkdir()
    (directory / 'tree').mkdir()
    (directory / 'tree/extra').write_bytes(b'partial')
    export_conflicts(store, directory, pause)
    assert not (directory / 'tree/extra').exists()
    manifest = (directory / 'resolutions.toml').read_bytes()
    assert tomllib.loads(manifest.decode())['tree'] == pause.tree
    (directory / 'versions/0000/ours').write_bytes(b'not an editable input')
    _decide(directory, {'f': 'action = "keep"'})
    (directory / 'tree/f').write_bytes(b'resolved')
    tree, _ = read_resolutions(store, directory, pause)
    assert _files(store, tree) == {'f': (0o100644, b'resolved')}
    assert (directory / 'tree/f').read_bytes() == b'resolved'
    export_conflicts(store, directory, pause)
    assert (directory / 'resolutions.toml').read_bytes() == manifest
    assert (directory / 'versions/0000/ours').read_bytes() == b'ours'


def test_rename_group_keeps_missing_original_and_reads_declared_destination(
    tmp_path: Path,
) -> None:
    store, pause = _merge(
        tmp_path, {'old': b'original\n'}, {'left': b'original\n'}, {'right': b'original\n'}
    )
    assert set(pause.conflicts[0].paths) == {'old', 'left', 'right'}
    assert set(pause.conflicts[0].decision_paths) == {'old', 'left', 'right'}
    directory = tmp_path / 'rewrite'
    export_conflicts(store, directory, pause)
    _decide(
        directory,
        {'old': 'action = "delete"', 'left': 'rename = "chosen"', 'right': 'action = "delete"'},
    )
    (directory / 'tree/chosen').write_bytes(b'edited\r\n')
    tree, _ = read_resolutions(store, directory, pause)
    assert _files(store, tree) == {'chosen': (0o100644, b'edited\r\n')}


@pytest.mark.parametrize('target', ['tree', 'resolutions.toml', 'root'])
def test_resolution_does_not_follow_replaced_control_roots(tmp_path: Path, target: str) -> None:
    store, pause = _merge(tmp_path, {'f': b'base'}, {'f': b'ours'}, {'f': b'theirs'})
    directory = tmp_path / 'rewrite'
    export_conflicts(store, directory, pause)
    _decide(directory, {'f': 'action = "delete"'})
    selected = directory if target == 'root' else directory / target
    outside = tmp_path / 'outside'
    selected.rename(outside)
    sentinel = outside / 'sentinel' if outside.is_dir() else outside
    before = sentinel.read_bytes() if sentinel.exists() else b'outside'
    sentinel.write_bytes(before)
    selected.symlink_to(outside, target_is_directory=outside.is_dir())
    with pytest.raises(PubanonError):
        read_resolutions(store, directory, pause)
    assert sentinel.read_bytes() == before
    if target == 'root':
        with pytest.raises(PubanonError):
            export_conflicts(store, directory, pause)
        assert sentinel.read_bytes() == before


def test_symlink_versions_and_resolution_preserve_target_bytes(tmp_path: Path) -> None:
    store, pause = _merge(
        tmp_path,
        {'link': (0o120000, b'base')},
        {'link': (0o120000, b'ours')},
        {'link': (0o120000, b'theirs')},
    )
    directory = tmp_path / 'rewrite'
    export_conflicts(store, directory, pause)
    assert not (directory / 'versions/0000/ours').is_symlink()
    assert (directory / 'versions/0000/ours').read_bytes() == b'ours'
    _decide(directory, {'link': 'action = "keep"'})
    (directory / 'tree/link').unlink()
    target = b'unknown-target' if os.name == 'nt' else b'unknown-\xff'
    (directory / 'tree/link').symlink_to(os.fsdecode(target))
    tree, _ = read_resolutions(store, directory, pause)
    assert _files(store, tree) == {'link': (0o120000, target)}
    (directory / 'tree/link').unlink()
    (directory / 'tree/link').symlink_to('../../outside')
    with pytest.raises(PubanonError, match='escaping'):
        read_resolutions(store, directory, pause)


@pytest.mark.slow
def test_text_resolution_is_explicit_and_preserves_literal_bytes(tmp_path: Path) -> None:
    store, pause = _merge(
        tmp_path,
        {'f': b'base\n', 'stable': b'stable\n'},
        {'f': b'ours\n', 'stable': b'stable\n'},
        {'f': b'theirs\n', 'stable': b'stable\n'},
    )
    directory = tmp_path / 'rewrite'
    export_conflicts(store, directory, pause)
    with pytest.raises(PubanonError, match='unresolved'):
        read_resolutions(store, directory, pause)
    _decide(directory, {'f': 'action = "keep"'})
    with pytest.raises(PubanonError, match='markers'):
        read_resolutions(store, directory, pause)
    (directory / 'tree/f').write_bytes(b'resolved\r\n\x80\n')
    (directory / 'tree/stable').write_bytes(b'undeclared edit')
    (directory / 'tree/extra').write_bytes(b'undeclared addition')
    tree, decisions = read_resolutions(store, directory, pause)
    assert _files(store, tree) == {
        'f': (0o100644, b'resolved\r\n\x80\n'),
        'stable': (0o100644, b'stable\n'),
    }
    assert [(item.paths, item.action) for item in decisions] == [(('f',), 'keep')]


@pytest.mark.parametrize(
    'destination',
    [
        '..\\outside',
        'C:outside',
        'name:stream',
        'NUL',
        'file.',
        'directory /file',
        '\\rooted',
        'line\nfile',
    ],
)
def test_windows_rename_refuses_paths_reinterpreted_by_native_filesystem(
    monkeypatch: pytest.MonkeyPatch, destination: str
) -> None:
    monkeypatch.setattr(_conflicts, 'sys', SimpleNamespace(platform='win32'))
    with pytest.raises(PubanonError):
        _conflicts._decision({'path': 'f', 'rename': destination}, 'f')


@pytest.mark.parametrize('action', ['keep', 'rename'])
def test_windows_resolution_preserves_frozen_executable_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
) -> None:
    store, pause = _merge(
        tmp_path,
        {'f': (0o100755, b'base'), 'stable': (0o100755, b'stable')},
        {'f': (0o100755, b'ours'), 'stable': (0o100755, b'stable')},
        {'f': (0o100755, b'theirs'), 'stable': (0o100755, b'stable')},
    )
    directory = tmp_path / 'rewrite'
    export_conflicts(store, directory, pause)
    chosen = 'chosen' if action == 'rename' else 'f'
    _decide(directory, {'f': 'rename = "chosen"' if action == 'rename' else 'action = "keep"'})
    (directory / 'tree' / chosen).write_bytes(b'resolved')
    (directory / 'tree' / chosen).chmod(0o600)
    (directory / 'tree/stable').chmod(0o600)
    reads: list[Path] = []

    def _read_entry(path: Path) -> tuple[int, bytes]:
        reads.append(path)
        return stat.S_IFREG, path.read_bytes()

    def _check_directory(_path: Path) -> None:
        pass

    monkeypatch.setattr(_conflicts, 'sys', SimpleNamespace(platform='win32'), raising=False)
    monkeypatch.setattr(_windows_fs, 'check_directory', _check_directory)
    monkeypatch.setattr(_windows_fs, 'read_entry', _read_entry)
    tree, _ = read_resolutions(store, directory, pause)
    assert _files(store, tree) == {
        chosen: (0o100755, b'resolved'),
        'stable': (0o100755, b'stable'),
    }
    assert directory / 'tree' / chosen in reads
    assert 'stable' not in capsys.readouterr().out


def test_windows_resolution_recovers_executable_mode_from_missing_leaf_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, pause = _merge(
        tmp_path,
        {'old': (0o100755, b'original\n')},
        {'left': (0o100755, b'original\n')},
        {'right': (0o100755, b'original\n')},
    )
    assert 'old' not in _files(store, pause.tree)
    directory = tmp_path / 'rewrite'
    export_conflicts(store, directory, pause)
    _decide(
        directory,
        {'old': 'action = "keep"', 'left': 'action = "delete"', 'right': 'action = "delete"'},
    )
    (directory / 'tree/old').write_bytes(b'restored')

    def _check_directory(_path: Path) -> None:
        pass

    def _read_entry(path: Path) -> tuple[int, bytes]:
        return stat.S_IFREG, path.read_bytes()

    monkeypatch.setattr(_conflicts, 'sys', SimpleNamespace(platform='win32'), raising=False)
    monkeypatch.setattr(_windows_fs, 'check_directory', _check_directory)
    monkeypatch.setattr(_windows_fs, 'read_entry', _read_entry)
    tree, _ = read_resolutions(store, directory, pause)
    assert _files(store, tree) == {'old': (0o100755, b'restored')}


@pytest.mark.parametrize('target', [b'..\\outside', b'C:outside', b'name:stream', b'file.'])
def test_windows_symlink_resolution_refuses_reinterpreted_targets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: bytes
) -> None:
    def _read_entry(_path: Path) -> tuple[int, bytes]:
        return stat.S_IFLNK, target

    monkeypatch.setattr(_conflicts, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(_windows_fs, 'read_entry', _read_entry)
    with pytest.raises(PubanonError):
        _conflicts._read_entry(tmp_path, 'link', trusted_parent=tmp_path.parent)


def test_windows_undeclared_scan_does_not_descend_reparse_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tree = tmp_path / 'tree'
    (tree / 'junction').mkdir(parents=True)
    (tree / 'junction/private').write_bytes(b'outside')
    reads: list[Path] = []

    def _check_directory(path: Path) -> None:
        if path.name == 'junction':
            raise OSError('reparse point')

    def _read_entry(path: Path) -> tuple[int, bytes]:
        reads.append(path)
        raise OSError('reparse point')

    monkeypatch.setattr(_conflicts, 'sys', SimpleNamespace(platform='win32'))
    monkeypatch.setattr(_windows_fs, 'check_directory', _check_directory)
    monkeypatch.setattr(_windows_fs, 'read_entry', _read_entry)
    _conflicts._report_undeclared(tmp_path, (), set())
    assert reads == [tree / 'junction']
    assert capsys.readouterr().out == 'Ignoring undeclared conflict export edit: junction\n'
