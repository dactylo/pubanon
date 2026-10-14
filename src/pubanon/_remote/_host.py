"""Verify GitHub destinations and read their published references."""

import json
import os
import re
import subprocess
import time
import urllib.parse
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
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
from pubanon._snapshots._store import store_path
from pubanon._terminal import escape, escape_lines

_ACTIONS_API_VERSION = '2026-03-10'
_CHECK_RUN_PAGE_SIZE = 100
_CREATED_PATTERN = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z')
_CREDENTIAL_CONFIG = (
    '-c',
    'credential.helper=',
    '-c',
    'credential.helper=!gh auth git-credential',
)
_DISCLOSURE_PAGE_SIZE = 100
_DISPATCH_TIMEOUT = 60
_GH: str = 'gh'
_GH_HOST_ENV: dict[str, str] = {'GH_HOST': 'github.com'}
_OID_PATTERN = re.compile(OID_HEX)
_RELEASE_PAGE_SIZE = 100
_REVEAL_COMMAND = 'pubanon repo reveal'
_REPOSITORY_QUERY = '{owner: .owner.login, repo: .name, visibility: .visibility}'
_REPOSITORY_SETTINGS_QUERY = (
    '{description: .description, default_branch: .default_branch, '
    'issues: .has_issues, wiki: .has_wiki, projects: .has_projects, '
    'pull_requests: .has_pull_requests, discussions: .has_discussions}'
)
_RUN_PAGE_SIZE = 100
_SERVER_ERROR_PATTERN = re.compile(rb'\bHTTP 50[0234]\b')
_SERVER_ERROR_PAUSES = (2, 4)
_URL_TEMPLATE = 'https://github.com/{owner}/{repo}.git'
_monotonic = time.monotonic
_sleep = time.sleep

type CheckState = Literal['missing', 'ambiguous', 'pending', 'success', 'unacceptable']

type _CheckRun = tuple[str, str, str, str | None]
type LogAbsence = Literal['api-404', 'storage-404']


class ActionsJob(NamedTuple):
    """Hold one workflow job from any attempt, classified by whether it ran.

    Attributes:
        identifier: Positive job identifier used to derive API paths.
        run: Positive identifier of the job's run.
        attempt: Positive attempt number that created the job.
        name: Host-provided job name, requiring escaping for display.
        runner_id: Positive runner identifier, or None when the job never
            started.
        runner: Runner name, group, and labels, newline-joined, checked for
            terms for every job.
    """

    identifier: int
    run: int
    attempt: int
    name: str
    runner_id: int | None
    runner: bytes

    @property
    def ran(self) -> bool:
        """Whether GitHub assigned the job a runner."""
        return self.runner_id is not None


class ActionsRun(NamedTuple):
    """Hold one workflow run's identity, state, and checked details.

    Attributes:
        identifier: Positive run identifier used to derive API paths.
        attempt: Positive current attempt number.
        status: Current execution status.
        workflow: Host-provided workflow name, or the workflow path when the
            name is null; requires escaping for display.
        event: Host-provided triggering event, requiring escaping.
        commit: Full commit ID the run tested.
        created_at: Creation time as `YYYY-MM-DDTHH:MM:SSZ`.
        details: Workflow name, display title, event, branch, path, head commit
            message, author and committer names and emails, and actor and
            triggering actor logins, newline-joined, checked for terms.
    """

    identifier: int
    attempt: int
    status: str
    workflow: str
    event: str
    commit: str
    created_at: str
    details: bytes


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
    state: CheckState
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
        identifier: GitHub's numeric release identifier.
        immutable: Whether GitHub has made the page immutable.
        created_at: Creation time as GitHub's UTC timestamp.
        published_at: Publication time, or None for a draft.
    """

    tag: str
    title: str
    notes: str
    url: str
    draft: bool
    prerelease: bool
    identifier: int = 0
    immutable: bool = False
    created_at: str = ''
    published_at: str | None = None


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


class DispatchRejectedError(PubanonError):
    """Report a refused dispatch for which no workflow run was started."""


class DispatchUncertainError(PubanonError):
    """Report an unknown dispatch outcome that must not be retried automatically."""


class ImmutableReleases(NamedTuple):
    """Hold a repository's immutable-release setting.

    Attributes:
        enabled: Whether releases become immutable when published.
        enforced_by_owner: Whether the repository owner enforces the setting.
    """

    enabled: bool
    enforced_by_owner: bool


class RemoteRefs(NamedTuple):
    """Hold the complete pubanon remote-reference universe.

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


class RunListingChangedError(PubanonError):
    """Report a workflow-run listing that changed during paginated observation."""


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


class WorkflowJob(NamedTuple):
    """Hold one job from the requested workflow-run attempt.

    Attributes:
        name: Host-provided job name, requiring escaping for display.
        status: Current job execution status.
        conclusion: Final result, or `None` before completion.
    """

    name: str
    status: str
    conclusion: str | None


class WorkflowRun(NamedTuple):
    """Hold validated workflow-run facts without trusting host-provided URLs.

    Attributes:
        identifier: Positive run identifier used to derive API paths.
        number: Positive workflow run number used to order runs.
        attempt: Positive current attempt number.
        path: Workflow path within the repository.
        event: Event that triggered the run.
        branch: Branch selected by the triggering event.
        commit: Commit tested by the run.
        status: Current execution status.
        conclusion: Final result, or `None` before completion.
        title: Host-provided display title, requiring escaping for display.
        url: Host-provided web address for escaped display only, never API
            requests.
    """

    identifier: int
    number: int
    attempt: int
    path: str
    event: str
    branch: str
    commit: str
    status: str
    conclusion: str | None
    title: str
    url: str


def _gh_error(returncode: int, stdout: bytes, stderr: bytes) -> PubanonError:
    """Return a GitHub CLI error headed by its first output line, one fact line per line."""
    lines = [
        *escape_lines(stderr.decode(errors='replace')),
        *escape_lines(stdout.decode(errors='replace')),
    ]
    if not lines:
        return PubanonError('GitHub CLI request failed', fact=f'exit status {returncode}')
    headline, *rest = lines
    return PubanonError(headline, fact='\n'.join((f'exit status {returncode}', *rest)))


def _gh_once(
    args: Sequence[str], *, timeout: float | None, input_bytes: bytes | None
) -> tuple[int, bytes, bytes]:
    """Run one captured GitHub CLI process."""
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
        return completed.returncode, completed.stdout, completed.stderr
    raise ObservationTimeoutError


@overload
def _gh(
    args: Sequence[str],
    *,
    check: Literal[True] = True,
    timeout: float | None = None,
    input_bytes: bytes | None = None,
    read: bool = False,
) -> bytes: ...


@overload
def _gh(
    args: Sequence[str],
    *,
    check: Literal[False],
    timeout: float | None = None,
    input_bytes: bytes | None = None,
    read: bool = False,
) -> tuple[int, bytes, bytes]: ...


def _gh(
    args: Sequence[str],
    *,
    check: bool = True,
    timeout: float | None = None,
    input_bytes: bytes | None = None,
    read: bool = False,
) -> bytes | tuple[int, bytes, bytes]:
    """Run one captured GitHub CLI command, repeating a read after a server error."""
    deadline = _monotonic() + timeout if read and timeout is not None else None
    remaining = timeout
    pauses = iter(_SERVER_ERROR_PAUSES if read else ())
    while True:
        returncode, stdout, stderr = _gh_once(args, timeout=remaining, input_bytes=input_bytes)
        if returncode == 0 or _SERVER_ERROR_PATTERN.search(stderr) is None:
            break
        pause = next(pauses, None)
        if pause is None or (deadline is not None and _monotonic() + pause >= deadline):
            break
        _sleep(pause)
        if deadline is not None:
            remaining = deadline - _monotonic()
            if remaining <= 0:
                break
    if not check:
        return returncode, stdout, stderr
    if returncode != 0:
        raise _gh_error(returncode, stdout, stderr)
    return stdout


def _object_field(mapping: Mapping[str, object], key: str, label: str) -> dict[str, object] | None:
    """Return a nested JSON object that may be null or absent."""
    value = mapping.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PubanonError(f'invalid {label} response')
    return cast('dict[str, object]', value)


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


def _release_entry(raw: object) -> GitHubRelease:
    """Validate one GitHub release object."""
    if not isinstance(raw, dict):
        raise PubanonError('invalid release response')
    release = cast('dict[str, object]', raw)
    identifier = release.get('id')
    tag = release.get('tag_name')
    title = release.get('name')
    notes = release.get('body')
    url = release.get('html_url')
    draft = release.get('draft')
    prerelease = release.get('prerelease')
    immutable = release.get('immutable')
    created = release.get('created_at')
    published = release.get('published_at')
    if (
        type(identifier) is not int
        or identifier <= 0
        or not isinstance(tag, str)
        or not tag
        or 'name' not in release
        or (title is not None and not isinstance(title, str))
        or (notes is not None and not isinstance(notes, str))
        or not isinstance(url, str)
        or not url
        or type(draft) is not bool
        or type(prerelease) is not bool
        or type(immutable) is not bool
        or not isinstance(created, str)
        or not created
        or 'published_at' not in release
        or (published is not None and not isinstance(published, str))
    ):
        raise PubanonError('invalid release response')
    return GitHubRelease(
        tag,
        title or '',
        notes or '',
        url,
        draft,
        prerelease,
        identifier=identifier,
        immutable=immutable,
        created_at=created,
        published_at=published,
    )


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


def _setting_value(value: str | bool) -> str:
    """Render one repository setting for comparison."""
    return str(value).lower() if isinstance(value, bool) else value


def _text_fields(mapping: Mapping[str, object], keys: Sequence[str], label: str) -> list[str]:
    """Return the present string values of fields that may be null."""
    values: list[str] = []
    for key in keys:
        value = mapping.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise PubanonError(f'invalid {label} response')
        values.append(value)
    return values


def _actions_job(raw: object, run_id: int, *, command: str) -> ActionsJob:
    """Validate one job of a run and classify whether it ran."""
    if not isinstance(raw, dict):
        raise PubanonError('invalid workflow job response')
    job = cast('dict[str, object]', raw)
    identifier = job.get('id')
    attempt = job.get('run_attempt')
    name = job.get('name')
    labels = job.get('labels')
    steps = job.get('steps')
    if (
        type(identifier) is not int
        or identifier <= 0
        or type(job.get('run_id')) is not int
        or job.get('run_id') != run_id
        or type(attempt) is not int
        or attempt <= 0
        or not isinstance(name, str)
        or job.get('status') != 'completed'
        or not {'conclusion', 'runner_id', 'runner_name', 'runner_group_name'} <= job.keys()
        or (job.get('runner_id') is not None and type(job.get('runner_id')) is not int)
        or not isinstance(labels, list)
        or (steps is not None and not isinstance(steps, list))
    ):
        raise PubanonError('invalid workflow job response')
    runner = _text_fields(job, ('runner_name', 'runner_group_name'), 'workflow job')
    _text_fields(job, ('conclusion',), 'workflow job')
    texts = [label for label in cast('list[object]', labels) if isinstance(label, str)]
    if len(texts) != len(cast('list[object]', labels)):
        raise PubanonError('invalid workflow job response')
    runner_id = job['runner_id']
    details = '\n'.join((*runner, *texts)).encode()
    if type(runner_id) is int and runner_id > 0:
        return ActionsJob(identifier, run_id, attempt, name, runner_id, details)
    if (
        runner_id is None
        and job['runner_name'] is None
        and steps == []
        and job['conclusion'] in {'skipped', 'cancelled'}
    ):
        return ActionsJob(identifier, run_id, attempt, name, None, details)
    raise PubanonError(
        'GitHub returned a workflow job pubanon cannot classify',
        fact=f'job {identifier}',
        recovery=f'rerun {command}',
    )


def _actions_run(raw: object) -> ActionsRun:
    """Validate one listed workflow run and gather the details reveal checks."""
    if not isinstance(raw, dict):
        raise PubanonError('invalid workflow run response')
    run = cast('dict[str, object]', raw)
    identifier = run.get('id')
    attempt = run.get('run_attempt')
    required = [
        run.get(key)
        for key in ('status', 'event', 'path', 'head_sha', 'created_at', 'display_title')
    ]
    if (
        type(identifier) is not int
        or identifier <= 0
        or type(attempt) is not int
        or attempt <= 0
        or not all(isinstance(value, str) for value in required)
        or 'head_branch' not in run
    ):
        raise PubanonError('invalid workflow run response')
    status, event, path, commit, created, title = cast('list[str]', required)
    if _OID_PATTERN.fullmatch(commit) is None or _CREATED_PATTERN.fullmatch(created) is None:
        raise PubanonError('invalid workflow run response')
    name = _text_fields(run, ('name',), 'workflow run')
    details = [*name, title, event, *_text_fields(run, ('head_branch',), 'workflow run'), path]
    head = _object_field(run, 'head_commit', 'workflow run')
    if head is not None:
        details.extend(_text_fields(head, ('message',), 'workflow run'))
        for role in ('author', 'committer'):
            person = _object_field(head, role, 'workflow run')
            if person is not None:
                details.extend(_text_fields(person, ('name', 'email'), 'workflow run'))
    for role in ('actor', 'triggering_actor'):
        actor = _object_field(run, role, 'workflow run')
        if actor is not None:
            details.extend(_text_fields(actor, ('login',), 'workflow run'))
    return ActionsRun(
        identifier,
        attempt,
        status,
        name[0] if name else path,
        event,
        commit,
        created,
        '\n'.join(details).encode(),
    )


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Build a JSON object while refusing duplicate keys."""
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _actions_payload(output: bytes, label: str) -> dict[str, object]:
    """Decode one Actions object while rejecting duplicate JSON keys."""
    try:
        parsed: object = json.loads(output, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError) as exc:
        raise PubanonError(f'invalid {label} response') from exc
    if not isinstance(parsed, dict):
        raise PubanonError(f'invalid {label} response')
    return cast('dict[str, object]', parsed)


def _actions_page(output: bytes, collection: str, label: str) -> tuple[int, list[object]]:
    """Read a nonnegative listing total and its entries from an Actions response."""
    payload = _actions_payload(output, label)
    total = payload.get('total_count')
    entries = payload.get(collection)
    if type(total) is not int or total < 0 or not isinstance(entries, list):
        raise PubanonError(f'invalid {label} response')
    return total, cast('list[object]', entries)


def _all_pages(path: str, collection: str, label: str, *, command: str) -> list[object]:
    """Read every page of one Actions listing whose total must not change."""
    entries: list[object] = []
    expected_total: int | None = None
    page = 1
    while True:
        output = _gh(['api', f'{path}per_page={_RUN_PAGE_SIZE}&page={page}'], read=True)
        total, page_entries = _actions_page(output, collection, label)
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise PubanonError(
                f'{label} listing changed while being read',
                recovery=f'rerun {command}',
            )
        entries.extend(page_entries)
        if len(entries) > expected_total:
            raise PubanonError(f'invalid {label} response')
        if len(entries) == expected_total:
            return entries
        if len(page_entries) < _RUN_PAGE_SIZE:
            raise PubanonError(f'invalid {label} response')
        page += 1


def _check_runs_payload(output: bytes) -> tuple[int, list[_CheckRun]]:
    """Strictly parse one GitHub check-runs response."""
    try:
        parsed = json.loads(output, object_pairs_hook=_unique_object)
    except (UnicodeDecodeError, ValueError) as exc:
        raise PubanonError('GitHub returned check results pubanon could not read') from exc
    if not isinstance(parsed, dict):
        raise PubanonError('GitHub returned check results pubanon could not read')
    raw_payload = cast('dict[object, object]', parsed)
    if set(raw_payload) != {'total_count', 'check_runs'}:
        raise PubanonError('GitHub returned check results pubanon could not read')
    payload = cast('dict[str, object]', raw_payload)
    total_count = payload['total_count']
    raw_runs = payload['check_runs']
    if type(total_count) is not int or total_count < 0 or not isinstance(raw_runs, list):
        raise PubanonError('GitHub returned check results pubanon could not read')
    runs: list[_CheckRun] = []
    for raw_run in cast('list[object]', raw_runs):
        if not isinstance(raw_run, dict):
            raise PubanonError('GitHub returned check results pubanon could not read')
        run = cast('dict[object, object]', raw_run)
        if not {'name', 'head_sha', 'status', 'conclusion'} <= set(run):
            raise PubanonError('GitHub returned check results pubanon could not read')
        name = run['name']
        head_sha = run['head_sha']
        status = run['status']
        conclusion = run['conclusion']
        if (
            not isinstance(name, str)
            or not isinstance(head_sha, str)
            or not isinstance(status, str)
        ):
            raise PubanonError('GitHub returned check results pubanon could not read')
        if conclusion is not None and not isinstance(conclusion, str):
            raise PubanonError('GitHub returned check results pubanon could not read')
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


def _http_status(stdout: bytes, stderr: bytes) -> int | None:
    """Read an HTTP status from a strict JSON body or the GitHub CLI diagnostic."""
    try:
        payload = _actions_payload(stdout, 'HTTP error')
    except PubanonError:
        payload = {}
    status = payload.get('status')
    if type(status) is int and re.fullmatch(r'[1-5][0-9]{2}', str(status)):
        return status
    if isinstance(status, str) and re.fullmatch(r'[1-5][0-9]{2}', status):
        return int(status)
    match = re.search(rb'HTTP (\d{3})', stderr)
    return int(match[1]) if match is not None else None


def _log_absence(stdout: bytes, stderr: bytes) -> LogAbsence | None:
    """Classify GitHub's two not-found answers to a job log request."""
    try:
        payload = _actions_payload(stdout, 'job log')
    except PubanonError:
        if b'<Code>BlobNotFound</Code>' in stdout and re.fullmatch(rb'gh: HTTP 404\s*', stderr):
            return 'storage-404'
        return None
    if (
        payload.get('message') == 'Not Found'
        and payload.get('status') in ('404', 404)
        and re.fullmatch(rb'gh: Not Found \(HTTP 404\)\s*', stderr)
    ):
        return 'api-404'
    return None


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
        output = _gh(
            ['api', f'repos/{owner}/{repo}/actions/{resource}?per_page=100&page={page}'], read=True
        )
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
    return tuple(_release_entry(raw) for raw in cast('list[object]', parsed))


def _repository_not_found(stdout: bytes, stderr: bytes) -> bool:
    """Recognize GitHub's missing-or-inaccessible repository response."""
    return _http_status(stdout, stderr) == HTTPStatus.NOT_FOUND


def _workflow_job(raw: object, run_id: int, attempt: int) -> WorkflowJob:
    """Validate one job's fields and ownership by the requested run attempt."""
    if not isinstance(raw, dict):
        raise PubanonError('invalid workflow job response')
    job = cast('dict[str, object]', raw)
    name = job.get('name')
    status = job.get('status')
    conclusion = job.get('conclusion')
    if (
        not isinstance(name, str)
        or not isinstance(status, str)
        or 'conclusion' not in job
        or (conclusion is not None and not isinstance(conclusion, str))
        or type(job.get('run_id')) is not int
        or job['run_id'] != run_id
        or type(job.get('run_attempt')) is not int
        or job['run_attempt'] != attempt
    ):
        raise PubanonError('invalid workflow job response')
    return WorkflowJob(name, status, conclusion)


def _workflow_run(raw: object) -> WorkflowRun:
    """Validate required workflow-run fields and positive nonboolean identifiers."""
    if not isinstance(raw, dict):
        raise PubanonError('invalid workflow run response')
    run = cast('dict[str, object]', raw)
    for key in ('id', 'run_number', 'run_attempt'):
        value = run.get(key)
        if type(value) is not int or value <= 0:
            raise PubanonError('invalid workflow run response')
    for key in ('path', 'event', 'head_branch', 'head_sha', 'status', 'display_title', 'html_url'):
        if not isinstance(run.get(key), str):
            raise PubanonError('invalid workflow run response')
    conclusion = run.get('conclusion')
    if 'conclusion' not in run or (conclusion is not None and not isinstance(conclusion, str)):
        raise PubanonError('invalid workflow run response')
    return WorkflowRun(
        cast('int', run['id']),
        cast('int', run['run_number']),
        cast('int', run['run_attempt']),
        cast('str', run['path']),
        cast('str', run['event']),
        cast('str', run['head_branch']),
        cast('str', run['head_sha']),
        cast('str', run['status']),
        conclusion,
        cast('str', run['display_title']),
        cast('str', run['html_url']),
    )


def actions_cache_count(owner: str, repo: str) -> int:
    """Count a repository's Actions caches.

    Args:
        owner: Configured repository owner.
        repo: Configured repository name.

    Returns:
        GitHub's total number of caches.

    Raises:
        PubanonError: The read fails or the answer is invalid; never zero by
            default.
    """
    output = _gh(['api', f'repos/{owner}/{repo}/actions/caches?per_page=1'], read=True)
    total, entries = _actions_page(output, 'actions_caches', 'Actions cache')
    if len(entries) > min(total, 1):
        raise PubanonError('invalid Actions cache response')
    return total


def actions_jobs(
    owner: str, repo: str, run_id: int, *, command: str = _REVEAL_COMMAND
) -> tuple[ActionsJob, ...]:
    """Read every job of every attempt of one workflow run.

    Args:
        owner: Configured repository owner.
        repo: Configured repository name.
        run_id: Validated run identifier.
        command: Command that reruns the check, named in recovery text.

    Returns:
        Jobs in GitHub's order, each classified as ran or never started.

    Raises:
        PubanonError: A read fails, the listing changes, or a job is invalid or
            cannot be classified.
    """
    jobs: list[ActionsJob] = []
    for entry in _all_pages(
        f'repos/{owner}/{repo}/actions/runs/{run_id}/jobs?filter=all&',
        'jobs',
        'workflow job',
        command=command,
    ):
        job = _actions_job(entry, run_id, command=command)
        if any(seen.identifier == job.identifier for seen in jobs):
            raise PubanonError('invalid workflow job response')
        jobs.append(job)
    return tuple(jobs)


def actions_runs(
    owner: str, repo: str, *, command: str = _REVEAL_COMMAND
) -> tuple[ActionsRun, ...]:
    """Read every workflow run of a repository with the details reveal checks.

    Args:
        owner: Configured repository owner.
        repo: Configured repository name.
        command: Command that reruns the check, named in recovery text.

    Returns:
        Runs in GitHub's order.

    Raises:
        PubanonError: A read fails, the listing changes, or a run is invalid.
    """
    runs: list[ActionsRun] = []
    for entry in _all_pages(
        f'repos/{owner}/{repo}/actions/runs?',
        'workflow_runs',
        'workflow run',
        command=command,
    ):
        run = _actions_run(entry)
        if any(seen.identifier == run.identifier for seen in runs):
            raise PubanonError('invalid workflow run response')
        runs.append(run)
    return tuple(runs)


def blob_url(owner: str, repo: str, ref: str, path: str) -> str:
    """Build a GitHub browser URL for a file at an exact ref.

    Args:
        owner: Final repository owner.
        repo: Final repository name.
        ref: Exact tag or other immutable ref.
        path: Repository-relative POSIX path to the file.
    """
    coordinates = '/'.join(urllib.parse.quote(part, safe='') for part in (owner, repo))
    encoded_ref = urllib.parse.quote(ref, safe='')
    encoded_path = '/'.join(urllib.parse.quote(part, safe='') for part in path.split('/'))
    return f'https://github.com/{coordinates}/blob/{encoded_ref}/{encoded_path}'


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


def convert_release(dest: Destination, release_id: int, title: str) -> None:
    """Edit one release page without changing it, so GitHub makes it immutable.

    Args:
        dest: Verified final repository.
        release_id: Identifier of the page to convert.
        title: The page's current, nonempty title, sent unchanged.

    Raises:
        PubanonError: The edit fails; its outcome is unknown.
    """
    _gh(
        [
            'api',
            '-X',
            'PATCH',
            f'repos/{dest.owner}/{dest.repo}/releases/{release_id}',
            '--input',
            '-',
        ],
        input_bytes=json.dumps({'name': title}).encode(),
    )


def create_repository_variable(coordinates: tuple[str, str], name: str, value: str) -> None:
    """Create an Actions variable with a single host request.

    Args:
        coordinates: Verified repository owner and name.
        name: Actions variable name.
        value: Literal variable value passed structurally to GitHub.

    Raises:
        PubanonError: Creation failed; the outcome may be unknown.
    """
    owner, repo = coordinates
    _gh(
        [
            'api',
            '--method',
            'POST',
            f'repos/{owner}/{repo}/actions/variables',
            '-f',
            f'name={name}',
            '-f',
            f'value={value}',
        ]
    )


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
    status = _http_status(stdout, stderr)
    if status == HTTPStatus.FORBIDDEN:
        raise PubanonError(
            'staging deletion is not authorized',
            fact='HTTP 403: GitHub refused authorization',
            recovery=(
                'review the active account and repository permissions; a missing delete_repo '
                'scope is one possible cause. You may delete the staging repository manually '
                'and use pubanon stage forget to remove local tracking'
            ),
        )
    if status == HTTPStatus.NOT_FOUND:
        raise PubanonError(
            'repository is absent or inaccessible',
            recovery='verify repository access and state, then retry `pubanon stage delete`',
        )
    raise _gh_error(returncode, stdout, stderr)


def delete_run_logs(owner: str, repo: str, run_id: int) -> None:
    """Ask GitHub to delete the logs of every attempt of one run.

    Args:
        owner: Configured repository owner.
        repo: Configured repository name.
        run_id: Validated run identifier.

    Raises:
        PubanonError: GitHub did not confirm the deletion; the outcome is
            unknown. The diagnostic names only the run and status.
    """
    returncode, stdout, stderr = _gh(
        ['api', '-X', 'DELETE', f'repos/{owner}/{repo}/actions/runs/{run_id}/logs'], check=False
    )
    if returncode:
        status = _http_status(stdout, stderr)
        raise PubanonError(
            'GitHub did not confirm deleting the logs',
            fact=f'run {run_id}: '
            + (f'HTTP {status}' if status is not None else f'exit status {returncode}'),
        )


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


def dispatch_workflow(
    coordinates: tuple[str, str],
    workflow: str,
    *,
    ref: str,
    inputs: Mapping[str, bool],
) -> int:
    """Dispatch once and classify whether a failed request could have started CI.

    Args:
        coordinates: Verified repository owner and name.
        workflow: Configured workflow filename under `.github/workflows/`.
        ref: Branch or tag to dispatch.
        inputs: Both `hosted` and `full_tests` boolean inputs, sent in mapping
            order.

    Returns:
        The positive workflow-run identifier returned by GitHub.

    Raises:
        DispatchRejectedError: The CLI could not start or GitHub definitively
            refused.
        DispatchUncertainError: CI may have started; never retry automatically.
    """
    owner, repo = coordinates
    args = [
        'api',
        '--method',
        'POST',
        '--header',
        f'X-GitHub-Api-Version: {_ACTIONS_API_VERSION}',
        f'repos/{owner}/{repo}/actions/workflows/{workflow}/dispatches',
        '-f',
        f'ref={ref}',
    ]
    for name, value in inputs.items():
        args.extend(['-F', f'inputs[{name}]={str(value).lower()}'])
    try:
        returncode, stdout, stderr = _gh(args, check=False, timeout=_DISPATCH_TIMEOUT)
    except ObservationTimeoutError as exc:
        raise DispatchUncertainError('workflow dispatch timed out; CI may have started') from exc
    except OperationCanceled:
        raise
    except PubanonError as exc:
        raise DispatchRejectedError(str(exc)) from exc
    if returncode:
        status = _http_status(stdout, stderr)
        error_type = (
            DispatchRejectedError
            if status is not None
            and HTTPStatus.BAD_REQUEST <= status < HTTPStatus.INTERNAL_SERVER_ERROR
            and status not in {HTTPStatus.REQUEST_TIMEOUT, HTTPStatus.TOO_MANY_REQUESTS}
            else DispatchUncertainError
        )
        error = _gh_error(returncode, stdout, stderr)
        raise error_type(str(error), fact=error.fact)
    try:
        payload = _actions_payload(stdout, 'workflow dispatch')
    except PubanonError as exc:
        raise DispatchUncertainError(
            'invalid workflow dispatch response; CI may have started'
        ) from exc
    run_id = payload.get('workflow_run_id')
    if type(run_id) is not int or run_id <= 0:
        raise DispatchUncertainError('invalid workflow dispatch response; CI may have started')
    return run_id


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


def enable_immutable_releases(owner: str, repo: str) -> None:
    """Turn on immutable releases for one repository.

    Args:
        owner: Configured repository owner.
        repo: Configured repository name.

    Raises:
        PubanonError: The request fails; its outcome is unknown.
    """
    _gh(['api', '-X', 'PUT', f'repos/{owner}/{repo}/immutable-releases'])


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


def immutable_releases(owner: str, repo: str) -> ImmutableReleases:
    """Read a repository's immutable-release setting.

    GitHub documents 404 for a disabled setting but answered 200 with `enabled:
    false` in testing; both mean off.

    Args:
        owner: Configured repository owner.
        repo: Configured repository name.

    Returns:
        Whether the setting is on and whether the owner enforces it.

    Raises:
        PubanonError: The read fails otherwise or the answer is invalid.
    """
    returncode, stdout, stderr = _gh(
        ['api', f'repos/{owner}/{repo}/immutable-releases'],
        check=False,
        read=True,
    )
    if returncode:
        if _http_status(stdout, stderr) == HTTPStatus.NOT_FOUND:
            return ImmutableReleases(enabled=False, enforced_by_owner=False)
        raise _gh_error(returncode, stdout, stderr)
    payload = _actions_payload(stdout, 'immutable releases')
    enabled = payload.get('enabled')
    enforced = payload.get('enforced_by_owner')
    if (
        set(payload) != {'enabled', 'enforced_by_owner'}
        or type(enabled) is not bool
        or type(enforced) is not bool
    ):
        raise PubanonError('invalid immutable releases response')
    return ImmutableReleases(enabled, enforced)


def job_log(
    owner: str, repo: str, job_id: int, *, command: str = _REVEAL_COMMAND
) -> bytes | LogAbsence:
    """Read one job's complete log without ever quoting GitHub's response.

    GitHub answers with a redirect to a storage service. Terminal escape
    sequences stay in the returned bytes.

    Args:
        owner: Configured repository owner.
        repo: Configured repository name.
        job_id: Validated job identifier.
        command: Command that reruns the check, named in recovery text.

    Returns:
        The log; `api-404` when GitHub's API answers not found, as for a
        deleted log or an unknown job; or `storage-404` when the storage
        service has no log, as for a job that never started.

    Raises:
        PubanonError: GitHub CLI is older than 2.97.0, or the request failed in
            any other way. Diagnostics name only the job and status.
    """
    returncode, stdout, stderr = _gh(
        ['api', '--allow-escape-sequences', f'repos/{owner}/{repo}/actions/jobs/{job_id}/logs'],
        check=False,
        read=True,
    )
    if returncode == 0:
        return stdout
    if b'unknown flag: --allow-escape-sequences' in stderr:
        raise PubanonError(
            'GitHub CLI is too old to read Actions logs',
            recovery=f'upgrade GitHub CLI to 2.97.0 or later, then rerun {command}',
        )
    absence = _log_absence(stdout, stderr)
    if absence is not None:
        return absence
    status = _http_status(stdout, stderr)
    raise PubanonError(
        'cannot check Actions logs',
        fact=f'job {job_id}: '
        + (f'HTTP {status}' if status is not None else f'exit status {returncode}'),
        recovery=f'rerun {command}',
    )


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


def read_actions_job(owner: str, repo: str, job_id: int, run_id: int) -> ActionsJob:
    """Read one job and confirm it belongs to the expected run.

    Args:
        owner: Configured repository owner.
        repo: Configured repository name.
        job_id: Validated job identifier.
        run_id: Run the job must belong to.

    Returns:
        The job, classified as ran or never started.

    Raises:
        PubanonError: The read fails or returns another job or run.
    """
    output = _gh(['api', f'repos/{owner}/{repo}/actions/jobs/{job_id}'], read=True)
    job = _actions_job(_actions_payload(output, 'workflow job'), run_id, command=_REVEAL_COMMAND)
    if job.identifier != job_id:
        raise PubanonError('invalid workflow job response')
    return job


def read_release_by_id(dest: Destination, release_id: int) -> GitHubRelease:
    """Read one release page by identifier.

    Args:
        dest: Verified final repository.
        release_id: Identifier of the page.

    Returns:
        The page as GitHub stores it now.

    Raises:
        PubanonError: The read fails or returns another page.
    """
    output = _gh(['api', f'repos/{dest.owner}/{dest.repo}/releases/{release_id}'], read=True)
    release = _release_entry(_actions_payload(output, 'release'))
    if release.identifier != release_id:
        raise PubanonError('invalid release response')
    return release


def remaining_timeout(timeout: float, started: float, now: float) -> float:
    """Return the positive remainder of one supplied timeout budget.

    Args:
        timeout: Total permitted duration in seconds.
        started: Monotonic time when the budget began.
        now: Current reading from the caller's monotonic clock.

    Raises:
        ObservationTimeoutError: The budget has expired.
    """
    remaining = timeout - (now - started)
    if remaining <= 0:
        raise ObservationTimeoutError
    return remaining


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


def repository_url(owner: str, repo: str) -> str:
    """Format the Git transport URL for validated repository coordinates.

    Args:
        owner: Repository owner already validated by the caller.
        repo: Main or staging repository name already validated by the caller.
    """
    return _URL_TEMPLATE.format(owner=owner, repo=repo)


def repository_variable(
    coordinates: tuple[str, str], name: str, *, timeout: float | None = None
) -> str | None:
    """Read an Actions variable, distinguishing absence from denied access.

    Args:
        coordinates: Verified repository owner and name.
        name: Actions variable name to read.
        timeout: Optional maximum read duration in seconds.

    Returns:
        The literal value, or `None` when GitHub returns HTTP 404.

    Raises:
        ObservationTimeoutError: `timeout` expires before GitHub answers.
        PubanonError: Access is denied, the read fails, or the response is
            malformed.
    """
    owner, repo = coordinates
    returncode, stdout, stderr = _gh(
        ['api', f'repos/{owner}/{repo}/actions/variables/{name}'],
        check=False,
        timeout=timeout,
        read=True,
    )
    if returncode:
        status = _http_status(stdout, stderr)
        if status == HTTPStatus.NOT_FOUND:
            return None
        if status == HTTPStatus.FORBIDDEN:
            raise PubanonError(
                'GitHub denied access to Actions variables',
                recovery='grant the GitHub login access to Actions variables, then retry',
            )
        raise _gh_error(returncode, stdout, stderr)
    payload = _actions_payload(stdout, 'repository variable')
    value = payload.get('value')
    if payload.get('name') != name or not isinstance(value, str):
        raise PubanonError('invalid repository variable response')
    return value


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
        ['api', f'repos/{owner}/{repo}', '--jq', _REPOSITORY_QUERY],
        check=False,
        read=True,
    )
    if returncode == 0:
        _parse_repository_facts(stdout)
        return True
    if _repository_not_found(stdout, stderr):
        return False
    raise _gh_error(returncode, stdout, stderr)


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


def settings_differences(
    expected: Settings, actual: SettingsFacts, *, skip_default_branch: bool = False
) -> list[str]:
    """Compare declared settings with observed repository facts.

    Args:
        expected: Settings declared in the project configuration.
        actual: Settings observed from the host repository.
        skip_default_branch: Omit the branch created by a later push.

    Returns:
        One ordered diagnostic for each mismatched setting.
    """
    fields = (
        ('description', expected.description, actual.description),
        ('default_branch', expected.default_branch, actual.default_branch),
        ('issues', expected.issues, actual.issues),
        ('wiki', expected.wiki, actual.wiki),
        ('projects', expected.projects, actual.projects),
        ('pull_requests', expected.pull_requests, actual.pull_requests),
        ('discussions', expected.discussions, actual.discussions),
    )
    return [
        f'{name}: expected {_setting_value(wanted)}; actual {_setting_value(observed)}'
        for name, wanted, observed in fields
        if wanted != observed and (name != 'default_branch' or not skip_default_branch)
    ]


def _authenticated_account(*, timeout: float | None = None) -> str:
    """Read the authenticated GitHub account."""
    output = _gh(['api', 'user', '--jq', '.login'], timeout=timeout, read=True)
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
        ['api', f'repos/{owner}/{repo}', '--jq', _REPOSITORY_SETTINGS_QUERY],
        timeout=timeout,
        read=True,
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
    differences = settings_differences(settings, actual, skip_default_branch=True)
    if differences:
        raise PubanonError(
            'repository was created but its settings do not match',
            fact='; '.join(escape(item) for item in differences),
            recovery=settings_recovery,
        )


def _read_release_page(
    dest: Destination, *, page: int, per_page: int, deadline: float | None = None
) -> tuple[GitHubRelease, ...]:
    """Read one explicit GitHub release-list page."""
    timeout = None if deadline is None else deadline - _monotonic()
    if timeout is not None and timeout <= 0:
        raise ObservationTimeoutError
    output = _gh(
        [
            'api',
            '--method',
            'GET',
            f'repos/{dest.owner}/{dest.repo}/releases?per_page={per_page}&page={page}',
        ],
        timeout=timeout,
        read=True,
    )
    return _release_page(output)


def _read_workflow_runs_page(
    coordinates: tuple[str, str],
    workflow: str,
    query: list[tuple[str, str]],
    *,
    timeout: float | None = None,
) -> tuple[int, tuple[WorkflowRun, ...]]:
    """Read one strict run listing, treating an unregistered workflow as empty."""
    owner, repo = coordinates
    returncode, stdout, stderr = _gh(
        [
            'api',
            f'repos/{owner}/{repo}/actions/workflows/{workflow}/runs?'
            + urllib.parse.urlencode(query),
        ],
        check=False,
        timeout=timeout,
        read=True,
    )
    if returncode:
        if _http_status(stdout, stderr) == HTTPStatus.NOT_FOUND:
            return 0, ()
        raise _gh_error(returncode, stdout, stderr)
    total, entries = _actions_page(stdout, 'workflow_runs', 'workflow run')
    return total, tuple(_workflow_run(entry) for entry in entries)


def all_releases(dest: Destination, *, deadline: float | None = None) -> tuple[GitHubRelease, ...]:
    """Read the complete release listing in GitHub's order.

    Args:
        dest: Previously verified GitHub destination.
        deadline: Monotonic deadline for the complete listing, if bounded.

    Returns:
        All release pages in GitHub's listing order.

    Raises:
        PubanonError: A host request fails or a response is invalid.
        ObservationTimeoutError: The deadline or a host request expires.
    """
    releases: list[GitHubRelease] = []
    page = 1
    while True:
        entries = _read_release_page(
            dest, page=page, per_page=_RELEASE_PAGE_SIZE, deadline=deadline
        )
        if len(entries) > _RELEASE_PAGE_SIZE:
            raise PubanonError('invalid release response')
        releases.extend(entries)
        if len(entries) < _RELEASE_PAGE_SIZE:
            return tuple(releases)
        page += 1


def create_release(
    dest: Destination, tag: str, *, title: str, notes: bytes, mark_latest: bool = True
) -> None:
    """Create a release page for an existing tag in a verified repository.

    Args:
        dest: Previously verified GitHub destination.
        tag: Existing tag for the new release page.
        title: Exact title prepared by the caller.
        notes: Exact UTF-8 notes prepared by the caller.
        mark_latest: Let GitHub choose the latest page when true; otherwise
            explicitly prevent this page from becoming latest.

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
            *([] if mark_latest else ['--latest=false']),
        ],
        input_bytes=notes,
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


def list_run_jobs(
    coordinates: tuple[str, str],
    run_id: int,
    attempt: int,
    *,
    remaining: Callable[[], float | None] | None = None,
) -> tuple[WorkflowJob, ...]:
    """Read every job page for one fixed workflow-run attempt.

    Args:
        coordinates: Verified repository owner and name.
        run_id: Validated positive run identifier.
        attempt: Validated positive attempt number.
        remaining: Shared budget callback, called once before each request,
            returning remaining seconds or `None` for no deadline. Its errors
            propagate.

    Returns:
        Jobs in host order, including duplicate names for verdict evaluation.

    Raises:
        PubanonError: A host read, job identity, or listing consistency check
            failed.
        ObservationTimeoutError: The caller's budget or a host request expired.
    """
    owner, repo = coordinates
    jobs: list[WorkflowJob] = []
    expected_total: int | None = None
    page = 1
    while True:
        output = _gh(
            [
                'api',
                (
                    f'repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs'
                    f'?per_page={_RUN_PAGE_SIZE}&page={page}'
                ),
            ],
            timeout=remaining() if remaining is not None else None,
            read=True,
        )
        total, entries = _actions_page(output, 'jobs', 'workflow job')
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise PubanonError('invalid workflow job response')
        jobs.extend(_workflow_job(entry, run_id, attempt) for entry in entries)
        if len(jobs) > expected_total:
            raise PubanonError('invalid workflow job response')
        if len(jobs) == expected_total:
            return tuple(jobs)
        if len(entries) < _RUN_PAGE_SIZE:
            raise PubanonError('invalid workflow job response')
        page += 1


def list_workflow_runs(
    coordinates: tuple[str, str],
    workflow: str,
    commit: str,
    *,
    event: str,
    remaining: Callable[[], float | None] | None = None,
) -> tuple[WorkflowRun, ...]:
    """Read all matching main-branch runs and detect changes during pagination.

    Args:
        coordinates: Verified repository owner and name.
        workflow: Configured workflow filename under `.github/workflows/`.
        commit: Exact commit the runs must test.
        event: Required trigger event.
        remaining: Shared budget callback, called once before each request,
            returning remaining seconds or `None` for no deadline. Its errors
            propagate.

    Returns:
        Validated matching runs in host order, or an empty tuple for HTTP 404.

    Raises:
        RunListingChangedError: Counts, duplicate IDs, or short pages show a
            changing listing.
        PubanonError: A host read failed or a run does not match the query.
        ObservationTimeoutError: The caller's budget or a host request expired.
    """
    runs: list[WorkflowRun] = []
    seen: set[int] = set()
    expected_total: int | None = None
    page = 1
    while True:
        total, entries = _read_workflow_runs_page(
            coordinates,
            workflow,
            [
                ('head_sha', commit),
                ('event', event),
                ('branch', 'main'),
                ('per_page', str(_RUN_PAGE_SIZE)),
                ('page', str(page)),
            ],
            timeout=remaining() if remaining is not None else None,
        )
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise RunListingChangedError('workflow run listing changed between pages')
        for run in entries:
            if (run.commit, run.event, run.branch, run.path) != (
                commit,
                event,
                'main',
                f'.github/workflows/{workflow}',
            ):
                raise PubanonError('invalid workflow run response')
            if run.identifier in seen:
                raise RunListingChangedError('workflow run listing contains duplicate identifiers')
            seen.add(run.identifier)
            runs.append(run)
        if len(runs) > expected_total:
            raise RunListingChangedError('workflow run listing exceeds its reported total')
        if len(runs) == expected_total:
            return tuple(runs)
        if len(entries) < _RUN_PAGE_SIZE:
            raise RunListingChangedError('workflow run listing ended before its reported total')
        page += 1


def read_workflow_run(
    coordinates: tuple[str, str],
    run_id: int,
    *,
    timeout: float | None = None,
) -> WorkflowRun:
    """Read a run through its verified repository and confirm its identity.

    Args:
        coordinates: Verified repository owner and name.
        run_id: Validated positive identifier from a host response.
        timeout: Request deadline in seconds, or `None` for no deadline.

    Raises:
        PubanonError: The read failed or the returned fields or identity are
            invalid.
        ObservationTimeoutError: The host request expired.
    """
    owner, repo = coordinates
    output = _gh(
        ['api', f'repos/{owner}/{repo}/actions/runs/{run_id}'], timeout=timeout, read=True
    )
    run = _workflow_run(_actions_payload(output, 'workflow run'))
    if run.identifier != run_id:
        raise PubanonError('invalid workflow run response')
    return run


def read_release(
    dest: Destination, tag: str, *, deadline: float | None = None
) -> GitHubRelease | None:
    """Find an exact release tag after successfully exhausting the listing.

    Args:
        dest: Previously verified GitHub destination.
        tag: Exact tag to find, without latest-release fallback.
        deadline: Monotonic deadline for the complete lookup, if bounded.

    Returns:
        The matching page, or `None` after a complete successful lookup.

    Raises:
        PubanonError: A host request fails or a response is invalid.
        ObservationTimeoutError: The deadline or a host request expires.
    """
    page = 1
    while True:
        entries = _read_release_page(
            dest, page=page, per_page=_RELEASE_PAGE_SIZE, deadline=deadline
        )
        if len(entries) > _RELEASE_PAGE_SIZE:
            raise PubanonError('invalid release response')
        for release in entries:
            if release.tag == tag:
                return release
        if len(entries) < _RELEASE_PAGE_SIZE:
            return None
        page += 1


def recent_workflow_runs(
    coordinates: tuple[str, str],
    workflow: str,
    *,
    event: str,
) -> tuple[WorkflowRun, ...]:
    """Read only the first page of recent main-branch runs for a staging preview.

    Args:
        coordinates: Verified repository owner and name.
        workflow: Configured workflow filename under `.github/workflows/`.
        event: Trigger event to query.

    Returns:
        Up to one host page in host order, or an empty tuple for HTTP 404.

    Raises:
        PubanonError: The read failed or the response contains malformed runs.
    """
    _, runs = _read_workflow_runs_page(
        coordinates,
        workflow,
        [('event', event), ('branch', 'main'), ('per_page', str(_RUN_PAGE_SIZE))],
    )
    return runs


def sync_repository_description(dest: Destination, description: str) -> bool:
    """Apply a configured description to an existing repository and verify it.

    Args:
        dest: Verified destination whose description may be changed.
        description: Content-checked description; an empty string clears it.

    Returns:
        Whether the description needed an update.

    Raises:
        PubanonError: The description could not be read, updated, or verified.
            A failed update or verification may leave the description changed.
    """
    if repository_settings(dest.owner, dest.repo).description == description:
        return False
    _gh(
        [
            'api',
            '-X',
            'PATCH',
            f'repos/{dest.owner}/{dest.repo}',
            '-f',
            f'description={description}',
        ]
    )
    actual = repository_settings(dest.owner, dest.repo).description
    if actual != description:
        raise PubanonError(
            'repository description did not match after update',
            expected=escape(description),
            actual=escape(actual),
        )
    return True


def workflow_registered(
    coordinates: tuple[str, str],
    workflow: str,
    *,
    timeout: float | None = None,
) -> bool:
    """Check whether GitHub has registered and enabled the configured workflow.

    Args:
        coordinates: Verified repository owner and name.
        workflow: Configured workflow filename under `.github/workflows/`.
        timeout: Request deadline in seconds, or `None` for no deadline.

    Returns:
        `True` for an active workflow, or `False` for HTTP 404.

    Raises:
        PubanonError: The workflow is disabled, the read fails, or its path is
            invalid.
        ObservationTimeoutError: The host request expired.
    """
    owner, repo = coordinates
    returncode, stdout, stderr = _gh(
        ['api', f'repos/{owner}/{repo}/actions/workflows/{workflow}'],
        check=False,
        timeout=timeout,
        read=True,
    )
    if returncode:
        if _http_status(stdout, stderr) == HTTPStatus.NOT_FOUND:
            return False
        raise _gh_error(returncode, stdout, stderr)
    payload = _actions_payload(stdout, 'workflow')
    if payload.get('path') != f'.github/workflows/{workflow}':
        raise PubanonError('invalid workflow response')
    if payload.get('state') != 'active':
        raise PubanonError(
            'the CI workflow is disabled in the staging repository',
            recovery='enable the workflow on GitHub, then rerun pubanon stage push',
        )
    return True


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
    output = _gh(args, timeout=timeout, read=True)
    total_count, runs = _check_runs_payload(output)
    if total_count == 0:
        if runs:
            raise PubanonError('GitHub returned check results pubanon could not read')
        return CheckVerdict(name, 'missing', '')
    if total_count > _CHECK_RUN_PAGE_SIZE and len(runs) == _CHECK_RUN_PAGE_SIZE:
        return CheckVerdict(name, 'ambiguous', f'{total_count} check runs')
    if len(runs) != total_count:
        raise PubanonError('GitHub returned check results pubanon could not read')
    if any(run_name != name or head_sha != commit for run_name, head_sha, _, _ in runs):
        raise PubanonError('GitHub returned check results pubanon could not read')
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
        owner_type = _gh(['api', f'users/{dest.owner}', '--jq', '.type'], read=True)
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
        paths: Derived pubanon configuration and data paths.
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
    returncode, stdout, stderr = _gh(args, check=False, timeout=timeout, read=True)
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
    error = _gh_error(returncode, stdout, stderr)
    raise PubanonError(
        f'Could not check GitHub repository {target}',
        fact=f'{error}\n{error.fact}',
        recovery='Check GitHub access and connectivity, then retry.',
    )


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
            expected.account,
            authority=authority,
            timeout=remaining_timeout(timeout, started, _monotonic()),
        )
        owner, repo, visibility = repository_facts(
            expected.owner,
            expected.repo,
            timeout=remaining_timeout(timeout, started, _monotonic()),
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
        paths: Derived pubanon configuration and data paths.
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
        expected,
        authority='configuration',
        timeout=remaining_timeout(timeout, started, _monotonic()),
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
