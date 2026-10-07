import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

from pubanon import _filesystem
from pubanon._config import Paths
from pubanon._errors import PubanonError
from pubanon._snapshots import (
    _inspection,
    _records,
    _verification,
)
from pubanon._snapshots._source import (
    Entry,
    Snapshot,
)
from tests._permissions import assert_private_mode
from tests.snapshots.test_candidate import (
    _build,
    _paths,
    _snapshot,
)


def _dirty_inspection(paths: Paths, project: str) -> Path:
    inspect = paths.data_dir / 'projects' / project / 'inspections' / ('a' * 40)
    inspect.mkdir(parents=True)
    marker = inspect / 'edited.txt'
    marker.write_text('keep this edit\n', encoding='utf-8')
    return marker


def test_clean_inspect_refuses_a_symlink_root(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    inspect = paths.data_dir / 'projects' / 'example' / 'inspections' / ('a' * 40)
    inspect.parent.mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'sentinel'
    sentinel.write_bytes(b'preserve')
    inspect.symlink_to(outside, target_is_directory=True)
    _filesystem.chmod(outside, 0o555)

    try:
        with pytest.raises(PubanonError, match='cannot clean review copy'):
            _inspection.clean_inspect(paths, 'example')
        assert inspect.is_symlink()
        assert sentinel.read_bytes() == b'preserve'
        assert_private_mode(outside, 0o555)
    finally:
        _filesystem.chmod(outside, 0o700)


@pytest.mark.parametrize('directory_mode', [0o000, 0o444, 0o555, 0o755])
def test_clean_inspect_removes_restricted_directories_without_changing_link_targets(
    tmp_path: Path,
    directory_mode: int,
) -> None:
    paths = _paths(tmp_path)
    inspect = paths.data_dir / 'projects' / 'example' / 'inspections' / ('a' * 40)
    nested = inspect / 'build' / 'nested'
    nested.mkdir(parents=True)
    (nested / 'artifact').write_bytes(b'build output')
    _filesystem.chmod(nested / 'artifact', 0o444)
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'sentinel'
    sentinel.write_bytes(b'preserve')
    _filesystem.chmod(sentinel, 0o444)
    _filesystem.chmod(outside, 0o555)
    (nested / 'directory-link').symlink_to(outside, target_is_directory=True)
    (nested / 'file-link').symlink_to(sentinel)
    restricted = (inspect, inspect / 'build', nested)
    for directory in reversed(restricted):
        _filesystem.chmod(directory, directory_mode)

    try:
        assert _inspection.clean_inspect(paths, 'example')
        assert not inspect.exists()
        assert sentinel.read_bytes() == b'preserve'
        assert_private_mode(sentinel, 0o444)
        assert_private_mode(outside, 0o555)
        assert not _inspection.clean_inspect(paths, 'example')
    finally:
        for directory in restricted:
            if directory.exists():
                _filesystem.chmod(directory, 0o700)
        _filesystem.chmod(outside, 0o700)


def test_inspect_clean_removes_known_copies_and_preserves_other_state(tmp_path: Path) -> None:
    paths, project, record = _build(tmp_path)
    previous_marker = _dirty_inspection(paths, project.name)
    selected = _verification.materialize_inspect(
        paths, project.name, _verification.prove_objects(paths, project.name, record)
    )
    unrelated = selected.parent / 'notes'
    unrelated.mkdir()
    (unrelated / 'keep').write_text('unrelated')
    assert _inspection.clean_inspect(paths, project.name)
    assert not selected.exists()
    assert not previous_marker.parent.exists()
    assert (unrelated / 'keep').read_text() == 'unrelated'
    assert _records.load_candidate(paths, project.name) == record
    assert not _inspection.clean_inspect(paths, project.name)


def test_inspect_creates_regular_files_exclusively(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    project = 'example'
    commit = 'b' * 40
    snapshot = _snapshot()
    inspect = paths.data_dir / 'projects' / project / 'inspections' / commit
    original_open = cast('Callable[..., object]', Path.open)
    modes: list[str] = []

    def _tracked_open(self: Path, *args: object, **kwargs: object) -> object:
        mode = args[0] if args else kwargs.get('mode', 'r')
        assert isinstance(mode, str)
        if self.is_relative_to(inspect):
            modes.append(mode)
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', _tracked_open)

    _inspection.materialize_snapshot(paths, project, commit, snapshot)

    assert modes == ['xb', 'xb']


@pytest.mark.parametrize('symlink_parent', [False, True])
def test_inspect_refuses_symlink_copy_or_parent_without_touching_target(
    tmp_path: Path, symlink_parent: bool
) -> None:
    paths = _paths(tmp_path)
    project = 'example'
    commit = 'b' * 40
    snapshot = _snapshot()
    root = paths.data_dir / 'projects' / project / 'inspections'
    root.parent.mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'notes').write_text('keep outside notes')
    if symlink_parent:
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir()
        (root / commit).symlink_to(outside, target_is_directory=True)
    with pytest.raises(PubanonError, match='symlink'):
        _inspection.materialize_snapshot(paths, project, commit, snapshot)
    with pytest.raises(PubanonError, match=r'symlink|cannot clean review copy'):
        _inspection.clean_inspect(paths, project)
    assert (outside / 'notes').read_text() == 'keep outside notes'


def test_inspect_refuses_unexpected_filesystem_directory_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    project = 'example'
    commit = 'b' * 40
    snapshot = (
        Entry('A/first.txt', 0o100644, 'blob', b'first\n'),
        Entry('B/second.txt', 0o100644, 'blob', b'second\n'),
    )
    inspect = paths.data_dir / 'projects' / project / 'inspections' / commit
    original_mkdir = Path.mkdir

    def _case_insensitive_mkdir(
        self: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if self == inspect / 'B':
            raise FileExistsError
        original_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, 'mkdir', _case_insensitive_mkdir)

    with pytest.raises(PubanonError, match='paths collide in the review copy'):
        _inspection.materialize_snapshot(paths, project, commit, snapshot)
    assert not inspect.exists()
    monkeypatch.setattr(Path, 'mkdir', original_mkdir)
    assert _inspection.materialize_snapshot(paths, project, commit, snapshot) == inspect
    assert (inspect / 'B/second.txt').read_bytes() == b'second\n'


@pytest.mark.parametrize('change', ['content', 'extra', 'missing', 'executable', 'symlink'])
def test_inspect_reuse_refuses_changed_copy_until_explicit_refresh(
    tmp_path: Path, change: str
) -> None:
    paths = _paths(tmp_path)
    project = 'example'
    commit = 'b' * 40
    snapshot = (*_snapshot(), Entry('link', 0o120000, 'symlink', b'README.md'))
    inspect = _inspection.materialize_snapshot(paths, project, commit, snapshot)
    _filesystem.chmod(inspect, 0o700)
    if change == 'content':
        _filesystem.chmod(inspect / 'README.md', 0o600)
        (inspect / 'README.md').write_bytes(b'changed')
    elif change == 'extra':
        (inspect / 'extra.txt').write_bytes(b'extra')
    elif change == 'missing':
        _filesystem.chmod(inspect / 'README.md', 0o600)
        (inspect / 'README.md').unlink()
    elif change == 'executable':
        _filesystem.chmod(inspect / 'bin/tool', 0o400)
        if os.name == 'nt':
            assert _inspection.materialize_snapshot(paths, project, commit, snapshot) == inspect
            return
    else:
        (inspect / 'link').unlink()
        (inspect / 'link').symlink_to('bin/tool')

    with pytest.raises(PubanonError) as error:
        _inspection.materialize_snapshot(paths, project, commit, snapshot)
    assert error.value.recovery == 'pubanon inspect --refresh'
    assert (
        _inspection.materialize_snapshot(
            paths,
            project,
            commit,
            snapshot,
            refresh=True,
        )
        == inspect
    )
    assert_private_mode(inspect, 0o500)
    assert (inspect / 'README.md').read_bytes() == b'public\n'
    assert_private_mode(inspect / 'bin/tool', 0o500)
    assert inspect.joinpath('link').readlink() == Path('README.md')


def test_inspect_reuse_refuses_symlink_directory_without_touching_external_tree(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    project = 'example'
    commit = 'b' * 40
    snapshot = _snapshot()
    inspect = _inspection.materialize_snapshot(paths, project, commit, snapshot)
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'tool'
    sentinel.write_bytes(b'outside')
    _filesystem.chmod(inspect, 0o700)
    _filesystem.chmod(inspect / 'bin', 0o700)
    _filesystem.chmod(inspect / 'bin/tool', 0o700)
    (inspect / 'bin/tool').unlink()
    (inspect / 'bin').rmdir()
    (inspect / 'bin').symlink_to(outside, target_is_directory=True)

    with pytest.raises(PubanonError) as error:
        _inspection.materialize_snapshot(paths, project, commit, snapshot)
    assert error.value.recovery == 'pubanon inspect --refresh'
    assert sentinel.read_bytes() == b'outside'
    _inspection.materialize_snapshot(paths, project, commit, snapshot, refresh=True)
    assert sentinel.read_bytes() == b'outside'
    assert (inspect / 'bin/tool').read_bytes() == b'#!/bin/sh\n'


@pytest.mark.parametrize('operation', ['read', 'lock'])
def test_inspect_reuse_reports_access_failures_separately_from_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    paths = _paths(tmp_path)
    project = 'example'
    commit = 'b' * 40
    snapshot = _snapshot()
    inspect = _inspection.materialize_snapshot(paths, project, commit, snapshot)
    if operation == 'read':
        original_read = Path.read_bytes

        def _fail_read(path: Path) -> bytes:
            if path == inspect / 'README.md':
                raise PermissionError('cannot read')
            return original_read(path)

        if os.name == 'nt':
            original_entry_read = _inspection._windows_fs.read_entry

            def _fail_entry_read(path: Path) -> tuple[int, bytes]:
                if path == inspect / 'README.md':
                    raise PermissionError('cannot read')
                return original_entry_read(path)

            monkeypatch.setattr(_inspection._windows_fs, 'read_entry', _fail_entry_read)
        else:
            monkeypatch.setattr(Path, 'read_bytes', _fail_read)
        expected = 'cannot verify review directory'
    else:
        original_chmod = _filesystem.chmod

        def _fail_lock(path: Path, mode: int, *, follow_symlinks: bool = True) -> None:
            if path == inspect / 'README.md':
                raise PermissionError('cannot chmod')
            original_chmod(path, mode, follow_symlinks=follow_symlinks)

        monkeypatch.setattr(_filesystem, 'chmod', _fail_lock)
        expected = 'cannot lock review directory'

    with pytest.raises(PubanonError, match=expected) as error:
        _inspection.materialize_snapshot(paths, project, commit, snapshot)
    assert error.value.recovery is None


def test_inspect_reuse_verifies_existing_editable_copy_then_locks_it(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    project = 'example'
    commit = 'b' * 40
    snapshot = _snapshot()
    inspect = _inspection.materialize_snapshot(paths, project, commit, snapshot)
    _filesystem.chmod(inspect, 0o700)
    _filesystem.chmod(inspect / 'bin', 0o700)
    _filesystem.chmod(inspect / 'README.md', 0o600)
    _filesystem.chmod(inspect / 'bin/tool', 0o700)

    assert _inspection.materialize_snapshot(paths, project, commit, snapshot) == inspect
    assert_private_mode(inspect, 0o500)
    assert_private_mode(inspect / 'bin', 0o500)
    assert_private_mode(inspect / 'README.md', 0o400)
    assert_private_mode(inspect / 'bin/tool', 0o500)


def test_inspection_path_validates_components_without_creating_directories(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    commit = 'a' * 40

    inspection = _inspection.inspection_path(paths, 'example', commit)

    assert inspection == paths.data_dir / 'projects' / 'example' / 'inspections' / commit
    assert not paths.data_dir.exists()
    with pytest.raises(PubanonError, match='invalid project name'):
        _inspection.inspection_path(paths, '../outside', commit)
    with pytest.raises(PubanonError, match='invalid review copy commit'):
        _inspection.inspection_path(paths, 'example', '../outside')
    assert not paths.data_dir.exists()


def test_materialization_explains_missing_windows_symlink_privilege(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failure = OSError('privilege not held')
    monkeypatch.setattr(failure, 'winerror', 1314, raising=False)

    def _symlink_to(_self: Path, _target: str, target_is_directory: bool = False) -> None:
        del target_is_directory
        raise failure

    monkeypatch.setattr(Path, 'symlink_to', _symlink_to)
    directory = tmp_path / 'snapshot'
    with pytest.raises(PubanonError, match='Windows cannot create snapshot symlinks') as error:
        _inspection.materialize_directory(
            directory, (Entry('link', 0o120000, 'symlink', b'missing'),)
        )
    assert error.value.recovery is not None
    assert 'Developer Mode' in error.value.recovery
    assert not directory.exists()


def test_materialization_preserves_forward_directory_symlink(tmp_path: Path) -> None:
    directory = tmp_path / 'snapshot'
    snapshot = (
        Entry('link', 0o120000, 'symlink', b'nested'),
        Entry('nested/file', 0o100644, 'blob', b'content'),
    )
    _inspection.materialize_directory(directory, snapshot)
    assert (directory / 'link').is_symlink()
    assert (directory / 'link').is_dir()
    assert (directory / 'link/file').read_bytes() == b'content'
    assert (directory / 'link').readlink() == Path('nested')


def test_materialization_protects_each_directory_before_writing_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = tmp_path / 'snapshot'
    original_open = cast('Callable[..., object]', Path.open)

    def _checked_open(self: Path, *args: object, **kwargs: object) -> object:
        if self.is_relative_to(directory):
            parent = self.parent
            while parent.is_relative_to(directory):
                assert_private_mode(parent, 0o700)
                parent = parent.parent
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, 'open', _checked_open)
    _inspection.materialize_directory(
        directory, (Entry('nested/deep/file', 0o100644, 'blob', b'content'),)
    )
    assert (directory / 'nested/deep/file').read_bytes() == b'content'


def test_materialize_directory_is_exclusive_private_and_cleans_partial_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / 'resolution'
    _inspection.materialize_directory(target, _snapshot())
    assert_private_mode(target, 0o700)
    with pytest.raises(PubanonError):
        _inspection.materialize_directory(target, _snapshot())
    assert target.exists()
    broken = tmp_path / 'broken'

    def _fail(_directory: Path, _snapshot: Snapshot) -> None:
        raise OSError('write failed')

    monkeypatch.setattr(_inspection, '_materialize_inspection_files', _fail)
    with pytest.raises(PubanonError):
        _inspection.materialize_directory(broken, _snapshot())
    assert not broken.exists()


@pytest.mark.parametrize(
    'snapshot',
    [
        (Entry('escape', 0o120000, 'symlink', b'../outside'),),
        (Entry('bad', 0o100600, 'blob', b'bytes'),),
        (Entry('A', 0o100644, 'blob', b'a'), Entry('a', 0o100644, 'blob', b'b')),
    ],
)
def test_materialize_directory_validates_before_creation(
    tmp_path: Path, snapshot: Snapshot
) -> None:
    target = tmp_path / 'invalid'
    with pytest.raises(PubanonError):
        _inspection.materialize_directory(target, snapshot)
    assert not target.exists()


@pytest.mark.skipif(os.name != 'nt', reason='requires native Windows directory junctions')
@pytest.mark.parametrize('junction_root', [False, True])
def test_windows_inspection_cleanup_never_traverses_junctions(
    tmp_path: Path, junction_root: bool
) -> None:
    inspection = tmp_path / 'inspection'
    outside = tmp_path / 'outside'
    outside.mkdir()
    sentinel = outside / 'sentinel'
    sentinel.write_bytes(b'preserve')
    _filesystem.chmod(outside, 0o500)
    if junction_root:
        junction = inspection
    else:
        inspection.mkdir()
        junction = inspection / 'junction'
    subprocess.run(
        ['cmd.exe', '/c', 'mklink', '/J', str(junction), str(outside)],
        check=True,
        capture_output=True,
    )
    try:
        if junction_root:
            with pytest.raises(PubanonError, match='cannot clean review copy'):
                _inspection._clean_inspection_path(inspection)
            assert junction.is_junction()
        else:
            assert _inspection._clean_inspection_path(inspection)
            assert not inspection.exists()
        assert sentinel.read_bytes() == b'preserve'
        assert_private_mode(outside, 0o500)
    finally:
        _filesystem.chmod(outside, 0o700)


@pytest.mark.parametrize('saved_mode', [0o100644, 0o100755])
def test_windows_inspection_verification_preserves_saved_git_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, saved_mode: int
) -> None:
    (tmp_path / 'other-directory').mkdir()
    target = tmp_path / 'file'
    target.write_bytes(b'content')
    _filesystem.chmod(target, 0o600)
    with os.scandir(tmp_path) as entries:
        child = next(entry for entry in entries if entry.name == target.name)
    saved = Entry('file', saved_mode, 'blob', b'content')
    reads: list[Path] = []

    def _read_entry(path: Path) -> tuple[int, bytes]:
        reads.append(path)
        return stat.S_IFREG, path.read_bytes()

    monkeypatch.setattr(_inspection, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(_inspection._windows_fs, 'read_entry', _read_entry)
    _inspection._verify_inspection_entry(child, saved)
    target.write_bytes(b'changed')
    with pytest.raises(PubanonError, match='saved snapshot'):
        _inspection._verify_inspection_entry(child, saved)
    assert reads == [target, target]


@pytest.mark.parametrize(
    'entry',
    [
        Entry('C:escape', 0o100644, 'blob', b'content'),
        Entry(r'nested\escape', 0o100644, 'blob', b'content'),
        Entry('trailing.', 0o100644, 'blob', b'content'),
        Entry('CON', 0o100644, 'blob', b'content'),
        Entry('link', 0o120000, 'symlink', b'C:escape'),
        Entry('link', 0o120000, 'symlink', rb'..\escape'),
        Entry('link', 0o120000, 'symlink', b'target-\xff'),
    ],
)
def test_windows_materialization_refuses_native_path_aliases_before_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, entry: Entry
) -> None:
    target = tmp_path / 'snapshot'

    def _windows_decode(raw: bytes) -> str:
        return raw.decode('utf-8', errors='surrogatepass')

    monkeypatch.setattr(_inspection, 'os', SimpleNamespace(name='nt', fsdecode=_windows_decode))
    with pytest.raises(PubanonError, match='cannot represent snapshot'):
        _inspection.materialize_directory(target, (entry,))
    assert not target.exists()
