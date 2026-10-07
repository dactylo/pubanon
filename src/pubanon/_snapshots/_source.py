"""Pin and read safe source repository snapshots."""

import hashlib
import os
import re
import unicodedata
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path, PurePath
from typing import Literal, NamedTuple
from urllib.parse import unquote_to_bytes

from pubanon._config import require_project_name
from pubanon._errors import PubanonError
from pubanon._run import OID_HEX, GitResult, git_failure_fact, run_git
from pubanon._terminal import escape, path_text

_BATCH_HEADER_FIELD_COUNT = 3
_BOOKMARK_REF = re.compile(rf'refs/pubanon/publications/([^/]+)/({OID_HEX})')
_BOOKMARK_SAFE = frozenset(b'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_')
_GITLINK_MODE = b'160000'
_SYMLINK_MODE = 0o120000
_TREE_METADATA_FIELD_COUNT = 3
_TREE_MODES = frozenset({b'100644', b'100755', b'120000'})

type Snapshot = tuple[Entry, ...]
type _TreeEntry = tuple[bytes, str, int, bytes]


class Entry(NamedTuple):
    """Retain one exact path, mode, object kind, and byte payload.

    Attributes:
        path: Validated snapshot-relative posix path.
        mode: Git tree mode preserved for the path.
        kind: Object role assigned to the path: blob or symbolic link.
        data: Exact blob content or symbolic-link target.
    """

    path: str
    mode: int
    kind: str
    data: bytes


class PinnedSource(NamedTuple):
    """Record an immutable commit and the selector intent that chose it.

    Attributes:
        commit: Immutable source commit selected for the snapshot.
        selector_kind: `ref` for a named ref or `explicit` for another revision
            expression.
        selector_name: Full ref name retained for drift checks, or `None` for
            an explicit revision.
        selector_oid: Direct ref object ID observed when pinned, or `None` for
            an explicit revision. An annotated tag's direct ID may differ from
            `commit`.
    """

    commit: str
    selector_kind: str
    selector_name: str | None
    selector_oid: str | None


@dataclass(frozen=True)
class SourceBookmark:
    """Record bookmark intent and location in an original Git repository.

    An instance does not prove that the ref exists or protects a commit.

    Attributes:
        source_path: Recorded source checkout path.
        git_common_dir: Common Git directory shared by linked worktrees.
        refname: Full private reference that protects the source commit.
    """

    source_path: Path
    git_common_dir: Path
    refname: str


@dataclass(frozen=True)
class SourceMatches:
    """Describe identical source contents in the current checkout's history.

    Attributes:
        branch: Current branch name, or `HEAD` for a detached checkout.
        latest: First matching commit in Git's topological history order.
        count: Number of matching commits reachable from HEAD.
        original_reachable: Whether the recorded source itself is reachable.
    """

    branch: str
    latest: str | None
    count: int
    original_reachable: bool


@dataclass(frozen=True)
class SourceRecovery:
    """Describe whether an exact source commit remains available.

    Attributes:
        status: Whether the commit is protected, merely available, or missing.
        source_path: Verified Git execution path when the commit is available.
        reason: Why protection or recovery is incomplete, if applicable.
    """

    status: Literal['protected', 'available', 'unavailable']
    source_path: Path | None
    reason: str | None


class WorkingChanges(NamedTuple):
    """Count working changes omitted from a committed source snapshot.

    Attributes:
        staged: Entries changed in the index relative to HEAD.
        unstaged: Tracked entries changed in the worktree relative to the
            index.
        untracked: Individual untracked files. Ignored files are excluded.
    """

    staged: int
    unstaged: int
    untracked: int


def _bookmark_git(
    bookmark: SourceBookmark, args: list[str], *, input_bytes: bytes | None = None
) -> GitResult:
    """Run Git against the recorded common repository, even without its checkout."""
    return run_git(
        ['--git-dir', str(bookmark.git_common_dir), *args],
        cwd=None,
        input_bytes=input_bytes,
        check=False,
    )


def _git_path(root: Path, name: str) -> Path:
    """Resolve one repository administrative path without displaying it."""
    output = run_git(['rev-parse', '--path-format=absolute', '--git-path', name], cwd=root).stdout
    if not output.endswith(b'\n') or b'\n' in output[:-1] or b'\0' in output:
        raise PubanonError('Git returned an ambiguous administrative path')
    raw_path = output[:-1]
    if not raw_path:
        raise PubanonError('Git returned an empty administrative path')
    return Path(os.fsdecode(raw_path))


def _matches_git_path_output(output: bytes, expected: PurePath) -> bool:
    """Compare one Git path line with a path using Git's slash separators."""
    return (
        output.endswith(b'\n')
        and b'\n' not in output[:-1]
        and b'\0' not in output
        and output[:-1] == os.fsencode(expected.as_posix())
    )


def _parse_oid(output: bytes) -> str:
    """Parse one unambiguous object ID line."""
    if not output.endswith(b'\n') or b'\n' in output[:-1]:
        raise PubanonError('Git returned ambiguous object identity output')
    raw_oid = output[:-1]
    if re.fullmatch(OID_HEX.encode(), raw_oid) is None:
        raise PubanonError('Git returned malformed object identity output')
    return raw_oid.decode('ascii')


def _bookmark_target(bookmark: SourceBookmark) -> str | None:
    """Read a direct bookmark without following a symbolic reference."""
    symbolic = _bookmark_git(bookmark, ['symbolic-ref', '-q', bookmark.refname])
    if symbolic.returncode == 0:
        raise PubanonError('source bookmark is symbolic and cannot be changed')
    if symbolic.returncode != 1:
        raise PubanonError('cannot inspect source bookmark', fact=git_failure_fact(symbolic))
    current = _bookmark_git(
        bookmark, ['for-each-ref', '--format=%(refname) %(objectname)', bookmark.refname]
    )
    if current.returncode != 0:
        raise PubanonError('cannot inspect source bookmark', fact=git_failure_fact(current))
    if not current.stdout:
        return None
    prefix = bookmark.refname.encode() + b' '
    if not current.stdout.startswith(prefix):
        raise PubanonError('Git returned an unexpected source bookmark')
    return _parse_oid(current.stdout[len(prefix) :])


def _read_blobs(
    root: Path, entries: list[_TreeEntry], *, git_dir: Path | None = None
) -> list[bytes]:
    """Read exact blob bytes through one `cat-file --batch` process."""
    if not entries:
        return []
    selector = ['--git-dir', str(git_dir.absolute())] if git_dir is not None else []
    result = run_git(
        [*selector, 'cat-file', '--batch'],
        cwd=root,
        input_bytes=b''.join(oid + b'\n' for _, _, _, oid in entries),
        check=False,
    )
    if result.returncode != 0:
        raise PubanonError('source object read failed', fact=git_failure_fact(result))
    output = result.stdout
    blobs: list[bytes] = []
    offset = 0
    for _raw_path, path, _mode, expected_oid in entries:
        header_end = output.find(b'\n', offset)
        if header_end < 0:
            raise PubanonError('Git returned an unterminated object header')
        header = output[offset:header_end].split(b' ')
        if len(header) != _BATCH_HEADER_FIELD_COUNT:
            raise PubanonError(
                'source object could not be read',
                fact=f'entry: {escape(path)}',
            )
        actual_oid, object_type, raw_size = header
        if actual_oid != expected_oid or object_type != b'blob':
            raise PubanonError('Git returned an ambiguous object response')
        if re.fullmatch(rb'0|[1-9][0-9]*', raw_size) is None:
            raise PubanonError('Git returned a malformed object size')
        size = int(raw_size)
        data_start = header_end + 1
        data_end = data_start + size
        if data_end >= len(output) or output[data_end : data_end + 1] != b'\n':
            raise PubanonError('Git returned a truncated object')
        blobs.append(output[data_start:data_end])
        offset = data_end + 1
    if offset != len(output):
        raise PubanonError('Git returned trailing object data')
    return blobs


def _source_commit_exists(bookmark: SourceBookmark, source_commit: str) -> bool:
    """Check the exact object type without resolving a revision expression."""
    result = _bookmark_git(bookmark, ['cat-file', '-t', source_commit])
    return result.returncode == 0 and result.stdout == b'commit\n'


def bookmark_ref(project: str, publication: str) -> str:
    """Return a collision-free Git ref for a project and publication.

    Raises:
        PubanonError: The project name is invalid or `publication` is not a
            full lowercase supported object ID.
    """
    require_project_name(project)
    if re.fullmatch(OID_HEX, publication) is None:
        raise PubanonError('invalid publication object ID')
    try:
        encoded = ''.join(
            chr(value) if value in _BOOKMARK_SAFE else f'%{value:02X}'
            for value in project.encode('utf-8')
        )
    except UnicodeError as exc:
        raise PubanonError('invalid project name') from exc
    return f'refs/pubanon/publications/{encoded}/{publication}'


def _bookmark_parts(bookmark: SourceBookmark) -> tuple[str, str]:
    """Decode only the canonical project component of an owned bookmark."""
    match = _BOOKMARK_REF.fullmatch(bookmark.refname)
    if match is None:
        raise PubanonError('invalid source bookmark')
    try:
        project = unquote_to_bytes(match[1]).decode('utf-8')
        if bookmark_ref(project, match[2]) != bookmark.refname:
            raise PubanonError('invalid source bookmark')
    except UnicodeError as exc:
        raise PubanonError('invalid source bookmark') from exc
    return project, match[2]


def _check_bookmark(bookmark: SourceBookmark, source_commit: str) -> None:
    """Require exact record shapes and the original common repository."""
    _bookmark_parts(bookmark)
    if (
        re.fullmatch(OID_HEX, source_commit) is None
        or not bookmark.source_path.is_absolute()
        or not bookmark.git_common_dir.is_absolute()
    ):
        raise PubanonError('invalid source bookmark')
    try:
        common = bookmark.git_common_dir.resolve(strict=True)
    except OSError as exc:
        raise PubanonError('recorded source repository is unavailable') from exc
    if not common.is_dir() or common != bookmark.git_common_dir:
        raise PubanonError('recorded source repository is unavailable')
    result = _bookmark_git(bookmark, ['rev-parse', '--path-format=absolute', '--git-common-dir'])
    if result.returncode != 0 or not _matches_git_path_output(result.stdout, common):
        raise PubanonError('recorded source repository is unavailable')


def check_bookmark_release(bookmark: SourceBookmark, source_commit: str) -> bool:
    """Verify that an owned bookmark can be released at its recorded commit.

    An absent ref is safe to treat as an already completed release.

    Args:
        bookmark: Recorded location and name of the owned source ref.
        source_commit: Exact commit expected at the ref.

    Returns:
        Whether the bookmark still exists at `source_commit`.

    Raises:
        PubanonError: The repository or bookmark is invalid, the ref is
            symbolic or points elsewhere, or its target cannot be inspected.
    """
    _check_bookmark(bookmark, source_commit)
    current = _bookmark_target(bookmark)
    if current is None:
        return False
    if current != source_commit:
        raise PubanonError('source bookmark changed to another commit')
    return True


def check_source_safety(root: Path) -> None:
    """Require a source repository suitable for isolated object reads.

    Refuse configured remotes, an alternates file, replacement refs, a grafts
    file, or shallow history. The source repository is not modified.

    Args:
        root: Source repository root.

    Raises:
        PubanonError: A source safety precondition is not satisfied or cannot
            be established.
    """
    remotes = run_git(['remote'], cwd=root).stdout
    if remotes:
        raise PubanonError(
            'source repository is not safe to read',
            fact='a Git remote is configured',
            recovery='remove every configured remote and rerun',
        )

    alternates = _git_path(root, 'objects/info/alternates')
    if alternates.is_file():
        raise PubanonError(
            'source repository is not safe to read',
            fact='an alternate object store is configured',
            recovery=(
                'run git repack -a while alternates still exist; then temporarily rename '
                f'{escape(path_text(alternates))} and verify all branch/tag tips and '
                'reachable objects with git fsck --full before removing the saved file. '
                'Restore it on any failure; see '
                'docs/guide.md#disconnecting-an-alternate-object-store'
            ),
        )

    replacements = run_git(
        ['for-each-ref', '--format=%(refname)', 'refs/replace'], cwd=root
    ).stdout
    if replacements:
        raise PubanonError(
            'source repository is not safe to read',
            fact='a replacement ref is configured',
            recovery='remove every refs/replace entry and rerun',
        )

    grafts = _git_path(root, 'info/grafts')
    if grafts.is_file():
        raise PubanonError(
            'source repository is not safe to read',
            fact='a grafts file is present',
            recovery='remove the grafts file and rerun',
        )

    shallow = run_git(['rev-parse', '--is-shallow-repository'], cwd=root).stdout
    if shallow not in {b'false\n', b'true\n'}:
        raise PubanonError('Git returned an ambiguous shallow-repository status')
    if shallow == b'true\n':
        raise PubanonError(
            'source repository is not safe to read',
            fact='the source repository is shallow',
            recovery='use a complete source repository and rerun',
        )


def decode_tree_path(raw_path: bytes) -> str:
    """Decode and validate one Git tree path.

    Args:
        raw_path: Raw path bytes returned by Git.

    Returns:
        The decoded and validated tree path.

    Raises:
        PubanonError: `raw_path` is not UTF-8 or is not a relative,
            slash-separated path with no empty, `.` or `..` components.
    """
    try:
        path = raw_path.decode('utf-8')
    except UnicodeDecodeError as exc:
        display_path = escape(raw_path.decode('utf-8', errors='backslashreplace'))
        raise PubanonError(
            'source tree contains a non-UTF-8 path',
            fact=f'entry: {display_path}',
        ) from exc
    if raw_path.startswith(b'/') or any(
        component in {b'', b'.', b'..'} for component in raw_path.split(b'/')
    ):
        raise PubanonError(
            'Git returned a malformed tree path',
            fact=f'entry: {escape(path)}',
        )
    return path


def ensure_bookmark(bookmark: SourceBookmark, source_commit: str) -> None:
    """Idempotently create or verify the owned bookmark at one exact commit.

    Raises:
        PubanonError: The original repository or commit is invalid or missing,
            or the ref is symbolic, conflicting, or cannot be updated.
    """
    _check_bookmark(bookmark, source_commit)
    check_source_safety(bookmark.git_common_dir)
    if not _source_commit_exists(bookmark, source_commit):
        raise PubanonError('source commit is unavailable for bookmarking')
    current = _bookmark_target(bookmark)
    if current is not None and current != source_commit:
        raise PubanonError('source bookmark changed to another commit')
    operation = 'create' if current is None else 'verify'
    command = f'{operation} {bookmark.refname} {source_commit}\n'.encode()
    result = _bookmark_git(bookmark, ['update-ref', '--no-deref', '--stdin'], input_bytes=command)
    if result.returncode != 0:
        raise PubanonError('cannot preserve source bookmark', fact=git_failure_fact(result))


def matching_commits(root: Path, sources: Collection[str]) -> dict[str, SourceMatches]:
    """Find exact source-tree matches with one walk of the current history.

    No refs or files change.

    Returns:
        Matches keyed by source commit, omitting missing original source
        objects. A present source with no matching history has a zero count.

    Raises:
        PubanonError: The current repository history cannot be read.
    """
    if not sources:
        return {}
    history = run_git(['log', '--topo-order', '--format=%H %T', 'HEAD', '--'], cwd=root)
    branch_result = run_git(['symbolic-ref', '--quiet', '--short', 'HEAD'], cwd=root, check=False)
    branch = branch_result.stdout.decode(errors='surrogateescape').strip() or 'HEAD'
    trees: dict[str, list[str]] = {}
    reachable: dict[str, str] = {}
    for line in history.stdout.decode('ascii').splitlines():
        commit, tree = line.split()
        trees.setdefault(tree, []).append(commit)
        reachable[commit] = tree
    matches: dict[str, SourceMatches] = {}
    for source in dict.fromkeys(sources):
        tree = reachable.get(source)
        if tree is None:
            result = run_git(
                ['rev-parse', '--verify', f'{source}^{{tree}}'], cwd=root, check=False
            )
            if result.returncode != 0:
                continue
            tree = _parse_oid(result.stdout)
        commits = trees.get(tree, [])
        matches[source] = SourceMatches(
            branch, commits[0] if commits else None, len(commits), source in reachable
        )
    return matches


def path_portability_keys(path: str) -> list[str]:
    """Return NFC-casefolded portability keys for each path component."""
    return [unicodedata.normalize('NFC', part).casefold() for part in path.split('/')]


def _symlink_problem(path: str, target: bytes, symlink_paths: frozenset[str]) -> str | None:
    """Classify a structurally unsafe symlink target."""
    if not target or b'\0' in target:
        return 'invalid'
    if target.startswith(b'/'):
        return 'absolute'
    resolved = path_portability_keys(path)[:-1]
    for component in path_portability_keys(target.decode('utf-8', errors='surrogateescape')):
        if component in {'', '.'}:
            continue
        if component == '..':
            if not resolved:
                return 'escaping'
            resolved.pop()
        else:
            resolved.append(component)
            # A later '..' must not hide traversal through an aliased symlink.
            if '/'.join(resolved) in symlink_paths:
                return 'unresolvable'
    return None


def pin(root: Path, rev: str) -> PinnedSource:
    """Resolve a source selector to one immutable commit.

    Args:
        root: Source repository root.
        rev: Named ref or any Git revision that resolves to a commit.

    Returns:
        The pinned commit and selector intent.

    Raises:
        PubanonError: The selector is invalid or Git returns ambiguous output.
    """
    symbolic = run_git(
        ['rev-parse', '--symbolic-full-name', '--verify', rev], cwd=root, check=False
    )
    selector_name: str | None = None
    if symbolic.returncode == 0 and symbolic.stdout:
        if not symbolic.stdout.endswith(b'\n') or b'\n' in symbolic.stdout[:-1]:
            raise PubanonError('Git returned an ambiguous ref selector')
        try:
            candidate_name = symbolic.stdout[:-1].decode('utf-8')
        except UnicodeDecodeError as exc:
            raise PubanonError('Git returned a non-UTF-8 ref name') from exc
        if candidate_name.startswith('refs/'):
            selector_name = candidate_name

    if selector_name is not None:
        selector_result = run_git(['rev-parse', '--verify', selector_name], cwd=root, check=False)
        if selector_result.returncode != 0:
            raise PubanonError(
                f'Could not read source reference {escape(selector_name)}',
                fact=git_failure_fact(selector_result),
                recovery=(
                    'Check that the selected branch or tag exists and points to an available '
                    'commit, then retry preparation.'
                ),
            )
        commit_result = run_git(
            ['rev-parse', '--verify', f'{selector_name}^{{commit}}'], cwd=root, check=False
        )
        if commit_result.returncode != 0:
            raise PubanonError(
                f'Could not read a commit from source reference {escape(selector_name)}',
                fact=git_failure_fact(commit_result),
                recovery=(
                    'Check that the selected branch or tag exists and points to an available '
                    'commit, then retry preparation.'
                ),
            )
        return PinnedSource(
            _parse_oid(commit_result.stdout),
            'ref',
            selector_name,
            _parse_oid(selector_result.stdout),
        )

    commit_result = run_git(['rev-parse', '--verify', f'{rev}^{{commit}}'], cwd=root, check=False)
    if commit_result.returncode != 0:
        raise PubanonError(
            f"cannot resolve source selector '{escape(rev)}'",
            fact=git_failure_fact(commit_result),
            recovery='Select an existing branch, tag, or commit and retry preparation.',
        )
    return PinnedSource(_parse_oid(commit_result.stdout), 'explicit', None, None)


def remove_bookmark(bookmark: SourceBookmark, source_commit: str) -> None:
    """Release only the owned bookmark at its expected commit.

    An absent ref is successful retry completion.

    Raises:
        PubanonError: The original repository is invalid, the ref is symbolic
            or points elsewhere, or the checked deletion fails.
    """
    if not check_bookmark_release(bookmark, source_commit):
        return
    command = f'delete {bookmark.refname} {source_commit}\n'.encode()
    result = _bookmark_git(bookmark, ['update-ref', '--no-deref', '--stdin'], input_bytes=command)
    if result.returncode != 0:
        raise PubanonError('cannot release source bookmark', fact=git_failure_fact(result))


def repository_root(selected: Path) -> Path:
    """Return the canonical top-level directory for one Git worktree.

    Args:
        selected: Repository directory selected by the operator.

    Returns:
        The existing canonical top-level worktree directory.

    Raises:
        PubanonError: Git cannot identify one unambiguous canonical worktree
            root.
    """
    result = run_git(['rev-parse', '--show-toplevel'], cwd=selected, check=False)
    output = result.stdout
    if result.returncode != 0:
        raise PubanonError(
            'cannot identify one canonical Git worktree root', fact=git_failure_fact(result)
        )
    if not output.endswith(b'\n') or b'\n' in output[:-1] or b'\0' in output:
        raise PubanonError('cannot identify one canonical Git worktree root')
    raw_root = output[:-1]
    root = Path(os.fsdecode(raw_root))
    if not raw_root or not root.is_absolute():
        raise PubanonError('cannot identify one canonical Git worktree root')
    try:
        canonical = root.resolve(strict=True)
    except OSError as exc:
        raise PubanonError('cannot identify one canonical Git worktree root') from exc
    if not canonical.is_dir():
        raise PubanonError('cannot identify one canonical Git worktree root')
    return canonical


def _verified_execution_path(
    bookmark: SourceBookmark, relocated_source: Path | None, *, protected: bool
) -> Path:
    """Choose an existing checkout only when it shares the recorded repository."""
    execution_path = bookmark.git_common_dir
    for candidate in (bookmark.source_path, relocated_source if protected else None):
        if candidate is None:
            continue
        try:
            root = repository_root(candidate)
            output = run_git(
                ['rev-parse', '--path-format=absolute', '--git-common-dir'], cwd=root
            ).stdout
        except PubanonError:
            continue
        if _matches_git_path_output(output, bookmark.git_common_dir):
            execution_path = root
    return execution_path


def describe_bookmark(source: Path, project: str, publication: str) -> SourceBookmark:
    """Describe a publication's private ref after source-safety checks.

    The function returns canonical locator information without creating the
    ref.

    Raises:
        PubanonError: The source is unsafe or unavailable, or the project or
            publication identifier is invalid.
    """
    refname = bookmark_ref(project, publication)
    root = repository_root(source)
    check_source_safety(root)
    output = run_git(['rev-parse', '--path-format=absolute', '--git-common-dir'], cwd=root).stdout
    if not output.endswith(b'\n') or b'\n' in output[:-1] or b'\0' in output:
        raise PubanonError('Git returned an ambiguous common directory')
    common = Path(os.fsdecode(output[:-1]))
    if not common.is_absolute():
        raise PubanonError('Git returned an ambiguous common directory')
    try:
        common = common.resolve(strict=True)
    except OSError as exc:
        raise PubanonError('Git returned an unavailable common directory') from exc
    return SourceBookmark(root, common, refname)


def _relocated_recovery(
    bookmark: SourceBookmark, source_commit: str, relocated_source: Path | None
) -> SourceRecovery | None:
    """Accept a moved repository only with the exact owned ref and commit."""
    if relocated_source is None:
        return None
    try:
        project, publication = _bookmark_parts(bookmark)
        relocated = describe_bookmark(relocated_source, project, publication)
        if _bookmark_target(relocated) != source_commit or not _source_commit_exists(
            relocated, source_commit
        ):
            return None
    except PubanonError:
        return None
    return SourceRecovery('protected', relocated.source_path, None)


def snapshot_digest(snapshot: Iterable[Entry]) -> str:
    """Hash length-framed snapshot records in path-byte order.

    Args:
        snapshot: Exact source entries to identify.

    Returns:
        The lowercase SHA-256 hexadecimal digest.
    """
    digest = hashlib.sha256()
    for entry in sorted(snapshot, key=lambda item: item.path.encode('utf-8')):
        fields = (
            entry.path.encode('utf-8'),
            format(entry.mode, 'o').encode('ascii'),
            entry.kind.encode('ascii'),
            entry.data,
        )
        for field in fields:
            digest.update(len(field).to_bytes(8, 'big'))
            digest.update(field)
    return digest.hexdigest()


def source_recovery(
    bookmark: SourceBookmark, source_commit: str, *, relocated_source: Path | None = None
) -> SourceRecovery:
    """Report exact source availability without changing its repository.

    Operational unavailability is represented by the returned status and
    reason. Relocation is accepted only after verifying repository identity or
    the exact owned ref and commit; a matching commit alone is insufficient.
    """
    try:
        _check_bookmark(bookmark, source_commit)
        exists = _source_commit_exists(bookmark, source_commit)
    except PubanonError as exc:
        if relocated := _relocated_recovery(bookmark, source_commit, relocated_source):
            return relocated
        return SourceRecovery('unavailable', None, str(exc))
    if not exists:
        if relocated := _relocated_recovery(bookmark, source_commit, relocated_source):
            return relocated
        return SourceRecovery('unavailable', None, 'original source commit is missing')
    try:
        current = _bookmark_target(bookmark)
    except PubanonError as exc:
        current = None
        reason = str(exc)
    else:
        reason = 'source bookmark changed' if current is not None else 'source bookmark is missing'

    execution_path = _verified_execution_path(
        bookmark, relocated_source, protected=current == source_commit
    )
    if current == source_commit:
        return SourceRecovery('protected', execution_path, None)
    return SourceRecovery('available', execution_path, reason)


def tree_mode(raw_mode: bytes, path: str) -> int:
    """Parse one Git tree mode under the supported source grammar.

    Args:
        raw_mode: Raw mode bytes returned by Git.
        path: Decoded tree path named in validation failures.

    Returns:
        The parsed tree mode.

    Raises:
        PubanonError: `raw_mode` is not exactly `100644`, `100755`, or
            `120000`; gitlinks and all other modes are refused.
    """
    if raw_mode == _GITLINK_MODE:
        raise PubanonError(
            'source tree contains an unsupported gitlink',
            fact=f'gitlink: {escape(path)}',
            recovery='select a commit without the submodule or vendor its content and rerun',
        )
    if raw_mode not in _TREE_MODES:
        raise PubanonError(
            'source tree contains an unsupported mode',
            fact=f'entry: {escape(path)}',
        )
    return int(raw_mode, 8)


def _parse_tree_entry(record: bytes) -> _TreeEntry:
    """Parse and validate one recursive tree entry."""
    metadata, separator, raw_path = record.partition(b'\t')
    fields = metadata.split(b' ')
    if separator != b'\t' or len(fields) != _TREE_METADATA_FIELD_COUNT or not raw_path:
        raise PubanonError('Git returned a malformed tree entry')
    raw_mode, raw_object_type, oid = fields
    path = decode_tree_path(raw_path)
    mode = tree_mode(raw_mode, path)
    if raw_object_type != b'blob':
        raise PubanonError(
            'source tree contains an unsupported object kind',
            fact=f'entry: {escape(path)}',
        )
    if re.fullmatch(OID_HEX.encode(), oid) is None:
        raise PubanonError('Git returned a malformed tree object ID')
    return raw_path, path, mode, oid


def _parse_tree(output: bytes) -> list[_TreeEntry]:
    """Parse exact recursive `ls-tree -z` output."""
    records = output.split(b'\0')
    if records[-1:] != [b'']:
        raise PubanonError('Git returned an unterminated tree entry')
    entries = [_parse_tree_entry(record) for record in records[:-1]]
    if len({raw_path for raw_path, _, _, _ in entries}) != len(entries):
        raise PubanonError('Git returned duplicate tree entries')
    entries.sort(key=lambda entry: entry[0])
    return entries


def update_source_tag(root: Path, label: str, target: str, known_sources: Collection[str]) -> None:
    """Create or refresh a local `pubanon/` tag for a recorded publication.

    Existing lightweight tags may move only from recorded source contents.
    Annotated tags and tags pointing elsewhere are left unchanged.

    Args:
        root: Registered source repository.
        label: Publication label used for the managed tag.
        target: Source commit to tag.
        known_sources: Recorded source commits whose trees permit moving an
            existing lightweight tag.

    Raises:
        PubanonError: A conflicting tag exists or Git cannot update the ref.
    """
    ref = f'refs/tags/pubanon/{label}'
    current = run_git(['show-ref', '--verify', '--hash', ref], cwd=root, check=False)
    old = _parse_oid(current.stdout) if current.returncode == 0 else None
    if old == target:
        return
    if old is not None:
        kind = run_git(['cat-file', '-t', old], cwd=root).stdout
        if kind != b'commit\n':
            raise PubanonError(f'existing tag pubanon/{label} is annotated and was left unchanged')
        old_tree = run_git(['rev-parse', '--verify', f'{old}^{{tree}}'], cwd=root, check=False)
        allowed = any(
            (
                tree := run_git(
                    ['rev-parse', '--verify', f'{source}^{{tree}}'], cwd=root, check=False
                )
            ).returncode
            == 0
            and tree.stdout == old_tree.stdout
            for source in known_sources
        )
        if not allowed:
            raise PubanonError(
                f'local source tag pubanon/{label} already points to unrelated contents'
            )
    run_git(['update-ref', '--no-deref', ref, target, old or '0' * len(target)], cwd=root)


def validate_symlink_graph(snapshot: Collection[Entry]) -> None:
    """Refuse structurally unsafe symlink targets in one ordered snapshot.

    Args:
        snapshot: Exact source entries in path-byte order.

    Raises:
        PubanonError: A symlink target is invalid, absolute, escaping, or
            recursively unresolvable.
    """
    symlink_paths = frozenset(
        '/'.join(path_portability_keys(entry.path))
        for entry in snapshot
        if entry.mode == _SYMLINK_MODE
    )
    for entry in snapshot:
        if entry.kind != 'symlink':
            continue
        problem = _symlink_problem(entry.path, entry.data, symlink_paths)
        if problem is not None:
            raise PubanonError(
                f'source tree contains an {problem} symlink target',
                fact=f'symlink: {escape(entry.path)}',
                recovery='replace the unsafe symlink and rerun',
            )


def read_snapshot(root: Path, commit: str, *, git_dir: Path | None = None) -> Snapshot:
    """Read exact entries from one immutable commit or tree.

    Args:
        root: Source repository root or store working directory.
        commit: Immutable commit or tree object ID.
        git_dir: Explicit Git directory for store reads; omit for source
            worktrees.

    Returns:
        The immutable source snapshot in path-byte order.

    Raises:
        PubanonError: The tree or an object is malformed or unsupported.
    """
    selector = ['--git-dir', str(git_dir.absolute())] if git_dir is not None else []
    result = run_git(
        [*selector, 'ls-tree', '-r', '-z', '--full-tree', commit], cwd=root, check=False
    )
    if result.returncode != 0:
        raise PubanonError('source tree read failed', fact=git_failure_fact(result))
    tree_entries = _parse_tree(result.stdout)
    blobs = _read_blobs(root, tree_entries, git_dir=git_dir)
    snapshot: list[Entry] = []
    for (_raw_path, path, mode, _oid), data in zip(tree_entries, blobs, strict=True):
        kind = 'symlink' if mode == _SYMLINK_MODE else 'blob'
        snapshot.append(Entry(path, mode, kind, data))
    validate_symlink_graph(snapshot)
    return tuple(snapshot)


def working_changes(root: Path) -> WorkingChanges:
    """Count omitted worktree and index changes without refreshing the index.

    A path changed in both the index and worktree contributes to both counts.
    Rename sources are consumed as part of their destination record.
    """
    output = run_git(
        ['status', '--porcelain=v1', '-z', '--untracked-files=all'],
        cwd=root,
        env_extra={'GIT_OPTIONAL_LOCKS': '0'},
    ).stdout
    staged = unstaged = untracked = 0
    records = iter(output.split(b'\0'))
    for record in records:
        if not record:
            continue
        index, worktree = record[:2]
        if record[:2] == b'??':
            untracked += 1
        else:
            staged += index != ord(' ')
            unstaged += worktree != ord(' ')
            if index in b'RC' or worktree in b'RC':
                next(records)
    return WorkingChanges(staged, unstaged, untracked)
