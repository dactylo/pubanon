"""Verify GitHub destinations and read their published references."""

import json
import os
import re
import subprocess
import time
import urllib.parse
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NamedTuple, cast, overload

from pubanon._config import (
    GlobalConfig,
    Paths,
    ProjectConfig,
    Settings,
    gate_host_coordinates,
    validate_host_segment,
)
from pubanon._errors import ObservationTimeoutError, OperationCanceled, PubanonError
from pubanon._run import OID_HEX, GitResult, run_credential_child
from pubanon._snapshots._candidate import store_path
from pubanon._terminal import escape

_CHECK_RUN_PAGE_SIZE = 100
_CREDENTIAL_CONFIG = (
    '-c',
    'credential.helper=',
    '-c',
    'credential.helper=!gh auth git-credential',
)
_DISCLOSURE_PAGE_SIZE = 100
_GH: str = 'gh'
_GH_HOST_ENV: dict[str, str] = {'GH_HOST': 'github.com'}
_OID_PATTERN = re.compile(OID_HEX)
_RELEASE_PAGE_SIZE = 100
_REPOSITORY_QUERY = '{owner: .owner.login, repo: .name, visibility: .visibility}'
_REPOSITORY_SETTINGS_QUERY = (
    '{description: .description, default_branch: .default_branch, '
    'issues: .has_issues, wiki: .has_wiki, projects: .has_projects, '
    'pull_requests: .has_pull_requests, discussions: .has_discussions}'
)
_URL_TEMPLATE = 'https://github.com/{owner}/{repo}.git'
_monotonic = time.monotonic

type _CheckRun = tuple[str, str, str, str | None]


class CheckVerdict(NamedTuple):
    """Classify one required GitHub check.

    Attributes:
        name: Required check name supplied by project policy.
        state: `missing` when no run exists, `ambiguous` when the response
            cannot cover all runs, `pending` before all runs complete,
            `success` when all runs succeed, or `unacceptable` when any
            completed run has another conclusion.
        detail: Empty for a missing run; otherwise the run count, current
            status, or completed conclusion.
    """

    name: str
    state: str
    detail: str


@dataclass(frozen=True)
class Destination:
    """Bind host destination values to a derived Git transport URL.

    Attributes:
        adapter: Host adapter used for destination operations.
        owner: Selected host namespace for the repository.
        repo: Expected host repository name.
        url: Derived Git transport URL for the repository.
        account: Authenticated host account expected for mutations.
        visibility: Repository visibility expected by the host operation.
    """

    adapter: str
    owner: str
    repo: str
    url: str
    account: str
    visibility: str


@dataclass(frozen=True)
class GitHubRelease:
    """Hold the content and status of one GitHub release page.

    Attributes:
        tag: Exact tag associated with the release.
        title: Release page title.
        notes: Complete Markdown release notes.
        url: Browser URL of the release page.
        draft: Whether the page is a draft.
        prerelease: Whether the page is marked as a prerelease.
    """

    tag: str
    title: str
    notes: str
    url: str
    draft: bool
    prerelease: bool


class DisclosureEntry(NamedTuple):
    """Hold one stable Actions inventory entry.

    Attributes:
        identifier: Numeric identifier returned by GitHub.
        name: Display name returned by GitHub.
    """

    identifier: int
    name: str


class Disclosure(NamedTuple):
    """Hold complete workflow-run and artifact inventories.

    Attributes:
        runs: Stable inventory of workflow-run identifiers and names.
        artifacts: Stable inventory of artifact identifiers and names.
    """

    runs: tuple[DisclosureEntry, ...]
    artifacts: tuple[DisclosureEntry, ...]


class RemoteRefs(NamedTuple):
    """Hold the complete Pubanon remote-reference universe.

    Attributes:
        main: Direct object ID of the remote main branch, if present.
        tags: Direct object IDs keyed by remote tag name.
        branches: Non-main remote branch names paired with direct object IDs.
    """

    main: str | None
    tags: dict[str, str]
    branches: tuple[tuple[str, str], ...] = ()


class RepositoryCreationUncertainError(PubanonError):
    """Report that repository creation could not be confirmed.

    Failures from the GitHub CLI creation invocation use this error, including
    failures to launch that invocation. The error does not prove that GitHub
    received the POST. Earlier owner-lookup failures remain `PubanonError`.
    """


class SettingsFacts(NamedTuple):
    """Hold repository settings read from GitHub.

    Attributes:
        description: Current host repository description.
        default_branch: Current host default-branch name.
        issues: Whether the host issue tracker is enabled.
        wiki: Whether the host wiki is enabled.
        projects: Whether host projects are enabled.
        pull_requests: Whether host pull requests are enabled.
        discussions: Whether host discussions are enabled.
    """

    description: str
    default_branch: str
    issues: bool
    wiki: bool
    projects: bool
    pull_requests: bool
    discussions: bool


def _gh_error(returncode: int, stdout: bytes, stderr: bytes) -> PubanonError:
    """Return one escaped GitHub CLI error."""
    output = stdout.decode(errors='replace') + stderr.decode(errors='replace')
    return PubanonError(
        escape(output.strip()) or 'GitHub CLI request failed',
        fact=f'exit status {returncode}',
    )


@overload
def _gh(
    args: Sequence[str],
    *,
    check: Literal[True] = True,
    timeout: float | None = None,
    input_bytes: bytes | None = None,
) -> bytes: ...


@overload
def _gh(
    args: Sequence[str],
    *,
    check: Literal[False],
    timeout: float | None = None,
    input_bytes: bytes | None = None,
) -> tuple[int, bytes, bytes]: ...


def _gh(
    args: Sequence[str],
    *,
    check: bool = True,
    timeout: float | None = None,
    input_bytes: bytes | None = None,
) -> bytes | tuple[int, bytes, bytes]:
    """Run one captured GitHub CLI command with escaped failures."""
    try:
        completed = subprocess.run(  # noqa: S603
            [_GH, *args],
            check=False,
            capture_output=True,
            env=os.environ | _GH_HOST_ENV,
            timeout=timeout,
            input=input_bytes,
        )
    except subprocess.TimeoutExpired:
        pass
    except OSError as exc:
        raise PubanonError('cannot run GitHub CLI') from exc
    else:
        if not check:
            return completed.returncode, completed.stdout, completed.stderr
        if completed.returncode != 0:
            raise _gh_error(completed.returncode, completed.stdout, completed.stderr)
        return completed.stdout
    raise ObservationTimeoutError


def _apply_created_settings(dest: Destination, settings: Settings, settings_recovery: str) -> None:
    """Apply remaining settings after creation and verify all declared creation fields."""
    update_args = [
        'api',
        '-X',
        'PATCH',
        f'repos/{dest.owner}/{dest.repo}',
        '-F',
        f'has_pull_requests={str(settings.pull_requests).lower()}',
    ]
    if dest.owner != dest.account:
        update_args.extend(['-F', f'has_discussions={str(settings.discussions).lower()}'])
    try:
        _gh(update_args)
        actual = repository_settings(dest.owner, dest.repo)
    except OperationCanceled:
        raise
    except PubanonError as exc:
        raise PubanonError(
            'repository was created, but its settings could not be verified',
            fact=exc.render(),
            recovery=settings_recovery,
        ) from exc
    differences: list[str] = []
    fields = (
        ('description', settings.description, actual.description),
        ('issues', settings.issues, actual.issues),
        ('wiki', settings.wiki, actual.wiki),
        ('projects', settings.projects, actual.projects),
        ('pull_requests', settings.pull_requests, actual.pull_requests),
        ('discussions', settings.discussions, actual.discussions),
    )
    for name, declared, observed in fields:
        if declared != observed:
            declared_text = str(declared).lower() if isinstance(declared, bool) else declared
            observed_text = str(observed).lower() if isinstance(observed, bool) else observed
            differences.append(f'{name}: declared {declared_text}, observed {observed_text}')
    if differences:
        raise PubanonError(
            'repository was created but its settings do not match',
            fact='; '.join(escape(item) for item in differences),
            recovery=settings_recovery,
        )


def _authenticated_account(*, timeout: float | None = None) -> str:
    """Read the authenticated GitHub account."""
    output = _gh(['api', 'user', '--jq', '.login'], timeout=timeout)
    if not output.endswith(b'\n') or b'\n' in output[:-1]:
        raise PubanonError('invalid authenticated account response')
    try:
        account = output[:-1].decode()
    except UnicodeDecodeError as exc:
        raise PubanonError('invalid authenticated account response') from exc
    if not account:
        raise PubanonError('invalid authenticated account response')
    return account


def _ls_remote_output(
    dest: Destination,
    *,
    store: Path | None = None,
    timeout: float | None = None,
) -> bytes:
    """Read destination heads and tags with the credential-bearing transport."""
    selector = ['--git-dir', str(store.absolute())] if store is not None else []
    args = [
        *selector,
        *_CREDENTIAL_CONFIG,
        'ls-remote',
        dest.url,
        'refs/heads/*',
        'refs/tags/*',
    ]
    result = run_credential_child(
        args,
        cwd=store,
        env_extra=_GH_HOST_ENV,
        operation='ls-remote',
        timeout=timeout,
    )
    return result.stdout


def _parse_ref_line(line: str) -> tuple[str, str]:
    """Parse one strict `ls-remote` output line."""
    try:
        oid, ref = line.split('\t')
    except ValueError as exc:
        raise PubanonError('invalid remote reference response') from exc
    if _OID_PATTERN.fullmatch(oid) is None:
        raise PubanonError('invalid remote reference response')
    return oid, ref


def _parse_ref_lines(output: bytes) -> list[str]:
    """Decode one complete `ls-remote` response into lines."""
    if not output:
        return []
    if not output.endswith(b'\n'):
        raise PubanonError('invalid remote reference response')
    try:
        return output[:-1].decode('ascii').split('\n')
    except UnicodeDecodeError as exc:
        raise PubanonError('invalid remote reference response') from exc


def _record_direct_ref(refs: dict[str, str], name: str, oid: str) -> None:
    """Record one nonempty, unique direct reference."""
    if not name or name in refs:
        raise PubanonError('invalid remote reference response')
    refs[name] = oid


def _remaining_timeout(timeout: float, started: float) -> float:
    """Return the positive remainder of one supplied timeout budget."""
    remaining = timeout - (_monotonic() - started)
    if remaining <= 0:
        raise ObservationTimeoutError
    return remaining


def _remote_refs(output: bytes) -> RemoteRefs:
    """Parse the selected heads and direct tags into remote facts."""
    lines = _parse_ref_lines(output)
    if not lines:
        return RemoteRefs(None, {})
    main: str | None = None
    branches: dict[str, str] = {}
    tags: dict[str, str] = {}
    for line in lines:
        oid, ref = _parse_ref_line(line)
        if ref == 'refs/heads/main':
            if main is not None:
                raise PubanonError('invalid remote reference response')
            main = oid
        elif ref.startswith('refs/heads/'):
            branch = ref.removeprefix('refs/heads/')
            _record_direct_ref(branches, branch, oid)
        elif ref.startswith('refs/tags/') and ref.endswith('^{}'):
            peeled = ref.removeprefix('refs/tags/').removesuffix('^{}')
            if not peeled:
                raise PubanonError('invalid remote reference response')
        elif ref.startswith('refs/tags/'):
            tag = ref.removeprefix('refs/tags/')
            _record_direct_ref(tags, tag, oid)
    return RemoteRefs(main, tags, tuple(sorted(branches.items())))


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object while refusing duplicate keys."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _check_runs_payload(output: bytes) -> tuple[int, list[_CheckRun]]:
    """Strictly parse one GitHub check-runs response."""
    try:
        parsed = json.loads(output, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError) as exc:
        raise PubanonError('GitHub returned check results Pubanon could not read') from exc
    if not isinstance(parsed, dict):
        raise PubanonError('GitHub returned check results Pubanon could not read')
    raw_payload = cast('dict[object, object]', parsed)
    if set(raw_payload) != {'total_count', 'check_runs'}:
        raise PubanonError('GitHub returned check results Pubanon could not read')
    payload = cast('dict[str, object]', raw_payload)
    total_count = payload['total_count']
    raw_runs = payload['check_runs']
    if type(total_count) is not int or total_count < 0 or not isinstance(raw_runs, list):
        raise PubanonError('GitHub returned check results Pubanon could not read')
    runs: list[_CheckRun] = []
    for raw_run in cast('list[object]', raw_runs):
        if not isinstance(raw_run, dict):
            raise PubanonError('GitHub returned check results Pubanon could not read')
        run = cast('dict[object, object]', raw_run)
        if not {'name', 'head_sha', 'status', 'conclusion'} <= set(run):
            raise PubanonError('GitHub returned check results Pubanon could not read')
        name = run['name']
        head_sha = run['head_sha']
        status = run['status']
        conclusion = run['conclusion']
        if (
            not isinstance(name, str)
            or not isinstance(head_sha, str)
            or not isinstance(status, str)
        ):
            raise PubanonError('GitHub returned check results Pubanon could not read')
        if conclusion is not None and not isinstance(conclusion, str):
            raise PubanonError('GitHub returned check results Pubanon could not read')
        runs.append((name, head_sha, status, conclusion))
    return total_count, runs


def _disclosure_page(
    output: bytes,
    collection_key: str,
) -> tuple[int, tuple[tuple[int, str], ...]]:
    """Strictly parse one workflow-run or artifact inventory page."""
    try:
        parsed = json.loads(output, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError) as exc:
        raise PubanonError('invalid disclosure response') from exc
    if not isinstance(parsed, dict):
        raise PubanonError('invalid disclosure response')
    raw_payload = cast('dict[object, object]', parsed)
    if set(raw_payload) != {'total_count', collection_key}:
        raise PubanonError('invalid disclosure response')
    payload = cast('dict[str, object]', raw_payload)
    total_count = payload['total_count']
    raw_entries = payload[collection_key]
    if type(total_count) is not int or total_count < 0 or not isinstance(raw_entries, list):
        raise PubanonError('invalid disclosure response')
    entries: list[tuple[int, str]] = []
    for raw_entry in cast('list[object]', raw_entries):
        if not isinstance(raw_entry, dict):
            raise PubanonError('invalid disclosure response')
        entry = cast('dict[object, object]', raw_entry)
        entry_id = entry.get('id')
        name = entry.get('name')
        if type(entry_id) is not int or not isinstance(name, str):
            raise PubanonError('invalid disclosure response')
        entries.append((entry_id, name))
    return total_count, tuple(entries)


def _parse_repository_facts(output: bytes) -> tuple[str, str, str]:
    """Parse strict projected repository facts."""
    try:
        parsed = json.loads(output, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError) as exc:
        raise PubanonError('invalid repository response') from exc
    if not isinstance(parsed, dict):
        raise PubanonError('invalid repository response')
    raw_payload = cast('dict[object, object]', parsed)
    if set(raw_payload) != {'owner', 'repo', 'visibility'}:
        raise PubanonError('invalid repository response')
    payload = cast('dict[str, object]', raw_payload)
    if not all(isinstance(payload[key], str) and payload[key] for key in payload):
        raise PubanonError('invalid repository response')
    return cast('tuple[str, str, str]', (payload['owner'], payload['repo'], payload['visibility']))


def _read_disclosure_page(
    owner: str,
    repo: str,
    resource: str,
    collection_key: str,
    page: int,
) -> tuple[int, tuple[tuple[int, str], ...]]:
    """Read one GitHub Actions page with repository and inventory context."""
    label = 'workflow runs' if resource == 'runs' else 'artifacts'
    target = f'{escape(owner)}/{escape(repo)}'
    try:
        output = _gh(['api', f'repos/{owner}/{repo}/actions/{resource}?per_page=100&page={page}'])
    except OperationCanceled:
        raise
    except PubanonError as exc:
        raise PubanonError(
            f'Could not check GitHub Actions {label} for {target}',
            fact=f'The GitHub Actions inventory is unknown. {exc}',
            recovery='Check GitHub access, then retry the disclosure check.',
        ) from exc
    try:
        return _disclosure_page(output, collection_key)
    except PubanonError as exc:
        raise PubanonError(
            f'GitHub returned an invalid disclosure response while checking {label} for {target}',
            fact='The GitHub Actions inventory is unknown.',
            recovery=(
                'Retry the disclosure check; if this persists, investigate the '
                'GitHub API response.'
            ),
        ) from exc


def _disclosure_pass(
    owner: str,
    repo: str,
    resource: str,
    collection_key: str,
) -> dict[int, str]:
    """Read one complete paginated disclosure inventory."""
    entries: dict[int, str] = {}
    expected_total: int | None = None
    page = 1
    label = 'workflow runs' if resource == 'runs' else 'artifacts'
    target = f'{escape(owner)}/{escape(repo)}'
    invalid_response = PubanonError(
        f'GitHub returned an invalid disclosure response while checking {label} for {target}',
        fact='The GitHub Actions inventory is unknown.',
        recovery=(
            'Retry the disclosure check; if this persists, investigate the GitHub API response.'
        ),
    )
    while True:
        total_count, page_entries = _read_disclosure_page(
            owner, repo, resource, collection_key, page
        )
        if expected_total is None:
            expected_total = total_count
        elif total_count != expected_total:
            raise invalid_response
        for entry_id, name in page_entries:
            if entry_id in entries:
                raise invalid_response
            entries[entry_id] = name
        if len(entries) > expected_total:
            raise invalid_response
        if len(entries) == expected_total:
            return entries
        if len(page_entries) < _DISCLOSURE_PAGE_SIZE:
            raise invalid_response
        page += 1


def _disclosure_entries(
    owner: str,
    repo: str,
    resource: str,
    collection_key: str,
) -> tuple[DisclosureEntry, ...]:
    """Read and stabilize one complete paginated disclosure inventory."""
    first = _disclosure_pass(owner, repo, resource, collection_key)
    second = _disclosure_pass(owner, repo, resource, collection_key)
    if first != second:
        raise PubanonError(
            f'unstable disclosure response for GitHub Actions '
            f'{"workflow runs" if resource == "runs" else "artifacts"} '
            f'in {escape(owner)}/{escape(repo)}',
            fact='The GitHub Actions inventory changed while it was being checked.',
            recovery='stop workflow or artifact changes, then rerun',
        )
    return tuple(DisclosureEntry(entry_id, name) for entry_id, name in sorted(first.items()))


def _release_page(output: bytes) -> tuple[GitHubRelease, ...]:
    """Parse one successful GitHub release-list response."""
    try:
        parsed = json.loads(output, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError) as exc:
        raise PubanonError('invalid release response') from exc
    if not isinstance(parsed, list):
        raise PubanonError('invalid release response')
    releases: list[GitHubRelease] = []
    for raw_release in cast('list[object]', parsed):
        if not isinstance(raw_release, dict):
            raise PubanonError('invalid release response')
        release = cast('dict[str, object]', raw_release)
        tag = release.get('tag_name')
        title = release.get('name')
        notes = release.get('body')
        url = release.get('html_url')
        draft = release.get('draft')
        prerelease = release.get('prerelease')
        if (
            not isinstance(tag, str)
            or not tag
            or 'name' not in release
            or (title is not None and not isinstance(title, str))
            or (notes is not None and not isinstance(notes, str))
            or not isinstance(url, str)
            or not url
            or type(draft) is not bool
            or type(prerelease) is not bool
        ):
            raise PubanonError('invalid release response')
        releases.append(GitHubRelease(tag, title or '', notes or '', url, draft, prerelease))
    return tuple(releases)


def _read_release_page(
    dest: Destination, *, page: int, per_page: int
) -> tuple[GitHubRelease, ...]:
    """Read one explicit GitHub release-list page."""
    output = _gh(
        [
            'api',
            '--method',
            'GET',
            f'repos/{dest.owner}/{dest.repo}/releases?per_page={per_page}&page={page}',
        ]
    )
    return _release_page(output)


def _repository_not_found(stdout: bytes, stderr: bytes) -> bool:
    """Recognize GitHub's missing-or-inaccessible repository response."""
    try:
        parsed = json.loads(stdout, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError):
        parsed = None
    if isinstance(parsed, dict):
        payload = cast('dict[object, object]', parsed)
        if payload.get('status') == '404':
            return True
    return b'HTTP 404' in stderr


def authenticated_account(*, timeout: float | None = None) -> str:
    """Probe the authenticated GitHub account with escaped failure diagnostics.

    Args:
        timeout: Optional maximum probe duration in seconds.

    Returns:
        The exact authenticated GitHub account.

    Raises:
        PubanonError: The authenticated account cannot be verified.
    """
    try:
        account = _authenticated_account(timeout=timeout)
    except OperationCanceled:
        raise
    except ObservationTimeoutError:
        pass
    except PubanonError as exc:
        raise PubanonError(
            'GitHub authentication could not be verified',
            fact=f'{exc}; {exc.fact}' if exc.fact else str(exc),
            expected=exc.expected,
            actual=exc.actual,
            recovery=exc.recovery,
        ) from None
    else:
        return account
    raise PubanonError('GitHub authentication could not be verified')


def check_recovery(verdicts: Iterable[CheckVerdict], *, retry: str) -> str:
    """Choose recovery from the most actionable observed check state.

    Args:
        verdicts: Required-check observations to classify.
        retry: Caller-specific action to take after resolving the state.

    Returns:
        State-specific recovery followed by the caller's retry action.
    """
    states = {verdict.state for verdict in verdicts}
    if 'unacceptable' in states:
        action = 'inspect the failed check and fix or rerun its workflow'
    elif 'ambiguous' in states:
        action = 'resolve the multiple matching check runs'
    elif 'missing' in states:
        action = 'check the configured name and workflow trigger'
    else:
        action = 'wait for the check'
    return f'{action}, then {retry}'


def check_result(
    coordinates: tuple[str, str],
    commit: str,
    name: str,
    *,
    timeout: float | None = None,
) -> CheckVerdict:
    """Require all latest runs of a named check on one commit to succeed.

    Args:
        coordinates: Exact GitHub repository owner and name.
        commit: Exact commit object ID expected in the response.
        name: Required check name.

        timeout: Optional maximum host-read duration in seconds.

    Returns:
        The fail-closed check verdict.

    Raises:
        PubanonError: The GitHub response is invalid or cannot be read.
        ObservationTimeoutError: The supplied host-read timeout expires.
    """
    owner, repo = coordinates
    query = urllib.parse.urlencode(
        {'check_name': name, 'filter': 'latest', 'per_page': str(_CHECK_RUN_PAGE_SIZE)}
    )
    args = ['api', f'repos/{owner}/{repo}/commits/{commit}/check-runs?{query}']
    output = _gh(args, timeout=timeout)
    total_count, runs = _check_runs_payload(output)
    if total_count == 0:
        if runs:
            raise PubanonError('GitHub returned check results Pubanon could not read')
        return CheckVerdict(name, 'missing', '')
    if total_count > _CHECK_RUN_PAGE_SIZE and len(runs) == _CHECK_RUN_PAGE_SIZE:
        return CheckVerdict(name, 'ambiguous', f'{total_count} check runs')
    if len(runs) != total_count:
        raise PubanonError('GitHub returned check results Pubanon could not read')
    if any(run_name != name or head_sha != commit for run_name, head_sha, _, _ in runs):
        raise PubanonError('GitHub returned check results Pubanon could not read')
    failures = [
        conclusion or 'null'
        for _, _, status, conclusion in runs
        if status == 'completed' and conclusion != 'success'
    ]
    if failures:
        return CheckVerdict(name, 'unacceptable', ', '.join(sorted(set(failures))))
    pending = [status for _, _, status, _ in runs if status != 'completed']
    if pending:
        return CheckVerdict(name, 'pending', ', '.join(sorted(set(pending))))
    return CheckVerdict(name, 'success', 'success')


def create_release(dest: Destination, tag: str, *, title: str, notes: bytes) -> None:
    """Create a release page for an existing tag in a verified repository.

    Args:
        dest: Previously verified GitHub destination.
        tag: Existing tag for the new release page.
        title: Exact title prepared by the caller.
        notes: Exact UTF-8 notes prepared by the caller.

    Raises:
        PubanonError: GitHub CLI cannot complete the mutation.
    """
    _gh(
        [
            'release',
            'create',
            tag,
            '--repo',
            f'{dest.owner}/{dest.repo}',
            '--verify-tag',
            f'--title={title}',
            '--notes-file',
            '-',
        ],
        input_bytes=notes,
    )


def create_repository(
    dest: Destination,
    settings: Settings | None,
    *,
    settings_recovery: str = "check the settings in the repository's GitHub settings",
) -> None:
    """Create one empty repository at a verified destination.

    Args:
        dest: Destination coordinates and explicit visibility.
        settings: Optional declared settings to apply and verify. The default
            branch is not set or compared here.
        settings_recovery: Caller guidance after settings cannot be verified or
            do not match following successful creation.

    Raises:
        PubanonError: The owner lookup fails, the owner is unsupported, or
            settings cannot be verified or do not match after creation.
        RepositoryCreationUncertainError: The GitHub CLI creation invocation
            fails, without establishing whether the repository was created.
    """
    args = ['api', '-X', 'POST']
    if dest.owner == dest.account:
        args.append('user/repos')
    else:
        owner_type = _gh(['api', f'users/{dest.owner}', '--jq', '.type'])
        if owner_type != b'Organization\n':
            raise PubanonError('account cannot create under the configured owner')
        args.append(f'orgs/{dest.owner}/repos')
    args.extend(
        [
            '-f',
            f'name={dest.repo}',
            '-F',
            f'private={str(dest.visibility == "private").lower()}',
        ]
    )
    if dest.owner != dest.account:
        args.extend(['-f', f'visibility={dest.visibility}'])
    if settings is not None:
        args.extend(
            [
                '-f',
                f'description={settings.description}',
                '-F',
                f'has_issues={str(settings.issues).lower()}',
                '-F',
                f'has_wiki={str(settings.wiki).lower()}',
                '-F',
                f'has_projects={str(settings.projects).lower()}',
            ]
        )
        if dest.owner == dest.account:
            args.extend(['-F', f'has_discussions={str(settings.discussions).lower()}'])
    try:
        _gh(args)
    except OperationCanceled:
        raise
    except PubanonError as exc:
        raise RepositoryCreationUncertainError(
            str(exc),
            fact=exc.fact,
            expected=exc.expected,
            actual=exc.actual,
            recovery=exc.recovery,
        ) from exc

    if settings is None:
        return
    _apply_created_settings(dest, settings, settings_recovery)


def delete_repository(owner: str, repo: str) -> None:
    """Delete one repository with classified authorization failures.

    Args:
        owner: GitHub repository owner.
        repo: GitHub repository name.

    Raises:
        PubanonError: Deletion is unauthorized, redundant, or fails.
    """
    returncode, stdout, stderr = _gh(['api', '-X', 'DELETE', f'repos/{owner}/{repo}'], check=False)
    if returncode == 0:
        return
    try:
        parsed = json.loads(stdout, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError):
        parsed = None
    status = (
        cast('dict[object, object]', parsed).get('status') if isinstance(parsed, dict) else None
    )
    if status == '403':
        raise PubanonError(
            'staging deletion is not authorized',
            fact='HTTP 403: GitHub refused authorization',
            recovery=(
                'review the active account and repository permissions; a missing delete_repo '
                'scope is one possible cause. You may delete the staging repository manually '
                'and use pubanon stage forget to remove local tracking'
            ),
        )
    if status == '404':
        raise PubanonError(
            'repository is absent or inaccessible',
            recovery='verify repository access and state, then retry `pubanon stage delete`',
        )
    raise _gh_error(returncode, stdout, stderr)


def disclosure(owner: str, repo: str) -> Disclosure:
    """Read complete GitHub Actions disclosure inventories.

    Args:
        owner: GitHub repository owner.
        repo: GitHub repository name.

    Returns:
        Every workflow run and artifact reported by GitHub.

    Raises:
        PubanonError: A complete inventory cannot be read or validated.
    """
    runs = _disclosure_entries(owner, repo, 'runs', 'workflow_runs')
    artifacts = _disclosure_entries(owner, repo, 'artifacts', 'artifacts')
    return Disclosure(runs, artifacts)


def edit_release(dest: Destination, tag: str, *, title: str, notes: bytes) -> None:
    """Replace the title and notes of one existing GitHub release page.

    Args:
        dest: Previously verified GitHub destination.
        tag: Existing release tag to edit.
        title: Exact resulting title prepared by the caller.
        notes: Exact resulting UTF-8 notes prepared by the caller.

    Raises:
        PubanonError: GitHub CLI cannot complete the mutation.
    """
    _gh(
        [
            'release',
            'edit',
            tag,
            '--repo',
            f'{dest.owner}/{dest.repo}',
            '--verify-tag',
            f'--title={title}',
            '--notes-file',
            '-',
        ],
        input_bytes=notes,
    )


def fetch(dest: Destination, cwd: Path, refspecs: Iterable[str]) -> GitResult:
    """Fetch managed refs from a verified destination.

    Reference updates are atomic. Pruning removes destination refs covered by
    the supplied refspecs when their source refs have disappeared.

    Args:
        dest: Verified destination carrying the derived transport URL.
        cwd: Bare Git repository receiving the fetched objects and refs.
        refspecs: Exact source-to-destination refspecs to fetch.

    Returns:
        The successful credential-bearing Git result.

    Raises:
        PubanonError: The credential-bearing transport fails.
    """
    return run_credential_child(
        [
            '--git-dir',
            str(cwd.absolute()),
            *_CREDENTIAL_CONFIG,
            'fetch',
            '--no-tags',
            '--atomic',
            '--prune',
            dest.url,
            *refspecs,
        ],
        cwd=cwd,
        env_extra=_GH_HOST_ENV,
        operation='fetch',
    )


def list_releases(dest: Destination, *, limit: int) -> tuple[GitHubRelease, ...]:
    """Read up to `limit` release pages from a verified destination.

    Args:
        dest: Previously verified GitHub destination.
        limit: Positive maximum number of pages to return.

    Returns:
        Release pages in GitHub's listing order.

    Raises:
        ValueError: The limit is not positive.
        PubanonError: A host request fails or a response is invalid.
    """
    if limit < 1:
        raise ValueError('release limit must be positive')
    releases: list[GitHubRelease] = []
    page = 1
    per_page = min(_RELEASE_PAGE_SIZE, limit)
    while len(releases) < limit:
        entries = _read_release_page(dest, page=page, per_page=per_page)
        if len(entries) > per_page:
            raise PubanonError('invalid release response')
        releases.extend(entries)
        if len(entries) < per_page:
            break
        page += 1
    return tuple(releases[:limit])


def login() -> None:
    """Hand GitHub authentication to an inherited operator terminal.

    Raises:
        OperationCanceled: The operator interrupts login.
        PubanonError: The GitHub CLI cannot start or does not complete login.
    """
    interrupted = False
    try:
        completed = subprocess.run(  # noqa: S603
            [
                _GH,
                'auth',
                'login',
                '--hostname',
                'github.com',
                '--git-protocol',
                'https',
                '--web',
            ],
            check=False,
            env=os.environ | _GH_HOST_ENV,
        )
    except KeyboardInterrupt:
        interrupted = True
    except OSError:
        pass
    else:
        if completed.returncode == 0:
            return
    if interrupted:
        raise OperationCanceled('Canceled.')
    raise PubanonError('GitHub login did not complete')


def ls_remote(
    dest: Destination,
    paths: Paths,
    project: str,
    *,
    timeout: float | None = None,
) -> RemoteRefs:
    """Read the destination heads and complete direct-tag set.

    Initializes the project's local advisory store if it is absent.

    Args:
        dest: Verified destination carrying the derived transport URL.
        paths: Derived Pubanon configuration and data paths.
        project: Registered project name.

        timeout: Optional maximum transport-read duration in seconds.

    Returns:
        The optional main tip, complete direct-tag map, and sorted non-main
        branch name and object ID pairs.

    Raises:
        PubanonError: Local store initialization, transport, or reference
            parsing fails.
        ObservationTimeoutError: The supplied transport timeout expires.
    """
    store = store_path(paths, project)
    return _remote_refs(_ls_remote_output(dest, store=store, timeout=timeout))


def push(dest: Destination, cwd: Path, args: Iterable[str]) -> GitResult:
    """Push exact ref arguments to a verified destination.

    Args:
        dest: Verified destination carrying the derived transport URL.
        cwd: Bare Git repository containing the objects to push.
        args: Push options and exact refspecs after the destination.

    Returns:
        The successful credential-bearing Git result.

    Raises:
        PubanonError: The credential-bearing transport fails.
    """
    return run_credential_child(
        ['--git-dir', str(cwd.absolute()), *_CREDENTIAL_CONFIG, 'push', dest.url, *args],
        cwd=cwd,
        env_extra=_GH_HOST_ENV,
        operation='push',
    )


def read_release(dest: Destination, tag: str) -> GitHubRelease | None:
    """Find an exact release tag after successfully exhausting the listing.

    Args:
        dest: Previously verified GitHub destination.
        tag: Exact tag to find, without latest-release fallback.

    Returns:
        The matching page, or `None` after a complete successful lookup.

    Raises:
        PubanonError: A host request fails or a response is invalid.
    """
    page = 1
    while True:
        entries = _read_release_page(dest, page=page, per_page=_RELEASE_PAGE_SIZE)
        if len(entries) > _RELEASE_PAGE_SIZE:
            raise PubanonError('invalid release response')
        for release in entries:
            if release.tag == tag:
                return release
        if len(entries) < _RELEASE_PAGE_SIZE:
            return None
        page += 1


def read_remote_refs(dest: Destination, *, timeout: float | None = None) -> RemoteRefs:
    """Read destination references without creating an advisory store.

    Args:
        dest: Verified destination carrying the derived transport URL.
        timeout: Optional maximum transport-read duration in seconds.

    Returns:
        The optional main tip, direct tags, and sorted non-main branches.

    Raises:
        PubanonError: Transport or reference parsing fails.
        ObservationTimeoutError: The supplied transport timeout expires.
    """
    return _remote_refs(_ls_remote_output(dest, timeout=timeout))


def remote_refs_text(refs: RemoteRefs) -> str:
    """Render one complete remote-reference universe as raw text.

    Args:
        refs: Complete remote main, tag, and non-main branch facts.

    Returns:
        Deterministic one-line reference comparison text.
    """
    tags = ', '.join(f'{name}={oid}' for name, oid in sorted(refs.tags.items())) or 'none'
    branches = ', '.join(f'{name}={oid}' for name, oid in sorted(refs.branches)) or 'none'
    return f'main={refs.main or "absent"}; tags={tags}; branches={branches}'


def repository_facts(
    owner: str,
    repo: str,
    *,
    timeout: float | None = None,
    missing_recovery: str | None = None,
) -> tuple[str, str, str]:
    """Read GitHub repository facts for explicit coordinates.

    Args:
        owner: GitHub repository owner.
        repo: GitHub repository name.

        timeout: Optional maximum host-read duration in seconds.
        missing_recovery: Recovery advice for an inaccessible staging or other
            alternate repository; defaults to configured repository creation.

    Returns:
        The canonical owner, repository name, and visibility.

    Raises:
        PubanonError: The GitHub response is invalid or cannot be read.
        ObservationTimeoutError: The supplied host-read timeout expires.
    """
    args = ['api', f'repos/{owner}/{repo}', '--jq', _REPOSITORY_QUERY]
    returncode, stdout, stderr = _gh(args, check=False, timeout=timeout)
    if returncode == 0:
        return _parse_repository_facts(stdout)
    target = f'{escape(owner)}/{escape(repo)}'
    if _repository_not_found(stdout, stderr):
        raise PubanonError(
            f'Cannot access GitHub repository {target}',
            fact=(
                'GitHub returned 404: the repository does not exist or this account lacks access.'
            ),
            recovery=missing_recovery
            or (
                'Check the repository name and GitHub permissions. '
                'If it has not been created, run `pubanon repo create`, then retry.'
            ),
        )
    raise PubanonError(
        f'Could not check GitHub repository {target}',
        fact=str(_gh_error(returncode, stdout, stderr)),
        recovery='Check GitHub access and connectivity, then retry.',
    )


def repository_settings(
    owner: str,
    repo: str,
    *,
    timeout: float | None = None,
) -> SettingsFacts:
    """Read supported GitHub repository settings.

    Args:
        owner: GitHub repository owner.
        repo: GitHub repository name.
        timeout: Optional maximum host-read duration in seconds.

    Returns:
        The strict repository settings facts.

    Raises:
        PubanonError: The GitHub response is invalid or cannot be read.
        ObservationTimeoutError: The supplied host-read timeout expires.
    """
    output = _gh(
        ['api', f'repos/{owner}/{repo}', '--jq', _REPOSITORY_SETTINGS_QUERY], timeout=timeout
    )
    try:
        parsed = json.loads(output, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError) as exc:
        raise PubanonError('invalid repository settings response') from exc
    if not isinstance(parsed, dict):
        raise PubanonError('invalid repository settings response')
    raw_payload = cast('dict[object, object]', parsed)
    expected = {
        'description',
        'default_branch',
        'issues',
        'wiki',
        'projects',
        'pull_requests',
        'discussions',
    }
    if set(raw_payload) != expected:
        raise PubanonError('invalid repository settings response')
    payload = cast('dict[str, object]', raw_payload)
    description = payload['description']
    default_branch = payload['default_branch']
    issues = payload['issues']
    wiki = payload['wiki']
    projects = payload['projects']
    pull_requests = payload['pull_requests']
    discussions = payload['discussions']
    if description is not None and not isinstance(description, str):
        raise PubanonError('invalid repository settings response')
    if not isinstance(default_branch, str):
        raise PubanonError('invalid repository settings response')
    if (
        not isinstance(issues, bool)
        or not isinstance(wiki, bool)
        or not isinstance(projects, bool)
        or not isinstance(pull_requests, bool)
        or not isinstance(discussions, bool)
    ):
        raise PubanonError('invalid repository settings response')
    return SettingsFacts(
        description or '', default_branch, issues, wiki, projects, pull_requests, discussions
    )


def repository_url(owner: str, repo: str) -> str:
    """Format the Git transport URL for validated repository coordinates.

    Args:
        owner: Repository owner already validated by the caller.
        repo: Main or staging repository name already validated by the caller.
    """
    return _URL_TEMPLATE.format(owner=owner, repo=repo)


def repository_visible(owner: str, repo: str) -> bool:
    """Report whether a repository is visible to this GitHub request.

    Args:
        owner: GitHub repository owner.
        repo: GitHub repository name.

    Returns:
        `True` for a successful read and `False` for a classified HTTP 404.

    Raises:
        PubanonError: The response is invalid or the result is neither success
            nor a classified HTTP 404.
    """
    returncode, stdout, stderr = _gh(
        ['api', f'repos/{owner}/{repo}', '--jq', _REPOSITORY_QUERY], check=False
    )
    if returncode == 0:
        _parse_repository_facts(stdout)
        return True
    if _repository_not_found(stdout, stderr):
        return False
    raise _gh_error(returncode, stdout, stderr)


def require_authenticated_account(
    expected: str,
    *,
    authority: str,
    timeout: float | None = None,
) -> str:
    """Require the active account to match one named authority.

    Args:
        expected: Exact account required by the authority.

        authority: Name of the facts that require the account.
        timeout: Optional maximum host-read duration in seconds.

    Returns:
        The exact authenticated account after equality is proven.

    Raises:
        PubanonError: The authenticated-account response is unreadable or
            invalid, or the account does not match the authority.
        ObservationTimeoutError: The supplied host-read timeout expires.
    """
    actual = _authenticated_account(timeout=timeout)
    if actual != expected:
        raise PubanonError(
            f'authenticated account does not match {authority}',
            fact='authenticated account',
            expected=escape(expected),
            actual=escape(actual),
        )
    return actual


def _verify_destination(
    expected: Destination,
    *,
    authority: str,
    timeout: float | None = None,
) -> Destination:
    """Verify one destination against its named authority."""
    if timeout is None:
        require_authenticated_account(expected.account, authority=authority)
        owner, repo, visibility = repository_facts(expected.owner, expected.repo)
    else:
        started = _monotonic()
        require_authenticated_account(
            expected.account, authority=authority, timeout=_remaining_timeout(timeout, started)
        )
        owner, repo, visibility = repository_facts(
            expected.owner, expected.repo, timeout=_remaining_timeout(timeout, started)
        )
    for fact, wanted, actual in (
        ('repository owner', expected.owner, owner),
        ('repository name', expected.repo, repo),
    ):
        if actual != wanted:
            raise PubanonError(
                f'{fact} does not match {authority}',
                fact=fact,
                expected=escape(wanted),
                actual=escape(actual),
            )
    if visibility != expected.visibility:
        raise PubanonError(
            f'repository visibility does not match {authority}',
            fact='repository visibility',
            expected=escape(expected.visibility),
            actual=escape(visibility),
        )
    return expected


def set_default_branch(
    owner: str,
    repo: str,
    branch: str,
    *,
    timeout: float | None = None,
) -> None:
    """Request one GitHub repository default-branch update.

    Args:
        owner: GitHub repository owner.
        repo: GitHub repository name.
        branch: Requested default branch.
        timeout: Optional maximum request duration in seconds.

    Raises:
        PubanonError: The GitHub request fails.
        ObservationTimeoutError: The supplied request timeout expires.
    """
    _gh(
        [
            'api',
            '-X',
            'PATCH',
            f'repos/{owner}/{repo}',
            '-f',
            f'default_branch={branch}',
        ],
        timeout=timeout,
    )


def set_visibility(owner: str, repo: str, visibility: str) -> None:
    """Set one repository's visibility.

    Args:
        owner: GitHub repository owner.
        repo: GitHub repository name.
        visibility: Explicit target visibility.

    Raises:
        PubanonError: The mutation fails.
    """
    _gh(
        [
            'api',
            '-X',
            'PATCH',
            f'repos/{owner}/{repo}',
            '-f',
            f'visibility={visibility}',
        ]
    )


def verify_destination(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
    *,
    timeout: float | None = None,
) -> Destination:
    """Verify and bind one configured host destination.

    Validate and content-gate the configured coordinates before host contact,
    then require exact account, owner, repository, and visibility matches.

    Args:
        paths: Derived Pubanon configuration and data paths.
        g: Global configuration containing the expected host account.
        p: Project configuration containing the repository coordinates.

        timeout: Optional shared verification budget in seconds.

    Returns:
        The configured destination with its derived transport URL.

    Raises:
        PubanonError: Local coordinate validation or content gates fail, or
            host metadata cannot be read or does not match configuration.
        ObservationTimeoutError: The supplied verification budget expires.
    """
    started = 0.0
    if timeout is not None:
        started = _monotonic()
    gate_host_coordinates(paths, g, p, coordinates=(p.owner, p.repo))
    expected_account = g.accounts[p.host]
    expected = Destination(
        adapter=p.host,
        owner=p.owner,
        repo=p.repo,
        url=repository_url(p.owner, p.repo),
        account=expected_account,
        visibility=p.visibility,
    )
    if timeout is None:
        return _verify_destination(expected, authority='configuration')
    return _verify_destination(
        expected, authority='configuration', timeout=_remaining_timeout(timeout, started)
    )


def verify_recorded_destination(
    destination: Destination,
) -> Destination:
    """Verify destination facts retained for outcome resolution.

    Args:
        destination: Recorded destination reconstructed by the publisher.

    Returns:
        The verified recorded destination.

    Raises:
        PubanonError: The adapter or recorded coordinate segments are unsafe,
            host metadata cannot be read, or metadata does not match the
            snapshot record.
    """
    if destination.adapter != 'github':
        raise PubanonError('unsupported recorded destination adapter')
    validate_host_segment('owner', destination.owner)
    validate_host_segment('repo', destination.repo)

    return _verify_destination(destination, authority='snapshot record')
