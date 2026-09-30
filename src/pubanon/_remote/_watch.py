"""Observe exact CI and package-release targets without mutation."""

import time
from collections.abc import Iterable, Sequence
from typing import NamedTuple

from pubanon._config import (
    GlobalConfig,
    PackageObservation,
    Paths,
    ProjectConfig,
    stage_repository,
)
from pubanon._errors import ObservationTimeoutError, PubanonError
from pubanon._remote._ci import (
    RunEvidence,
    RunQuery,
    evidence_facts,
    evidence_recovery,
    run_evidence,
)
from pubanon._remote._host import (
    CheckVerdict,
    Destination,
    RemoteRefs,
    check_recovery,
    check_result,
    ls_remote,
    remote_refs_text,
    verify_destination,
)
from pubanon._remote._package import PackageRelease, release_state
from pubanon._remote._stage import candidate_next_action, load_stage, verify_staged_target
from pubanon._snapshots._candidate import (
    load_candidate,
    published_refs,
    tag_intent_for,
    version_from_tag,
)
from pubanon._terminal import (
    detail,
    detail_fields,
    display_check_result,
    display_duration,
    escape,
    field,
    fields,
    heading,
    next_step,
    output,
)

DEFAULT_TIMEOUT = 1800
_POLL_SECONDS = 15
_PROGRESS_SECONDS = 30
_monotonic = time.monotonic
_sleep = time.sleep


class _RunObservation(NamedTuple):
    """Carry one watch's shared deadline and recovery context."""

    deadline: float
    retry: str
    staged: bool


def _check_facts(verdicts: Sequence[CheckVerdict]) -> str:
    """Render exact check states for a refusal fact."""
    return ', '.join(
        f'{escape(verdict.name)}: {display_check_result(verdict)}' for verdict in verdicts
    )


def _check_status(verdict: CheckVerdict) -> str:
    """Render one check state for human observation."""
    kind = 'success' if verdict.state == 'success' else 'warning'
    return field(escape(verdict.name), display_check_result(verdict), kind=kind)


def _pause_or_expire(deadline: float, error: PubanonError) -> None:
    """Pause one polling interval or raise when the shared deadline expires."""
    remaining = deadline - _monotonic()
    if remaining <= 0:
        raise error
    _sleep(min(_POLL_SECONDS, remaining))
    if _monotonic() >= deadline:
        raise error


def _published_next_action(
    p: ProjectConfig, *, package_complete: bool = False, version: str | None = None
) -> str:
    """Choose the next configured step after a published observation."""
    if not package_complete and p.package is not None:
        selected = f' --version {escape(version)}' if version is not None else ''
        return f'pubanon package watch{selected}'
    if p.visibility == 'private':
        return 'pubanon repo reveal'
    return ''


def _report_check_changes(
    current: tuple[CheckVerdict, ...], previous: tuple[CheckVerdict, ...] | None
) -> None:
    """Show each changed check state once, with diagnostic detail in verbose mode."""
    for index, verdict in enumerate(current):
        if previous is None or verdict.state != previous[index].state:
            output(_check_status(verdict))
        if previous is None or verdict != previous[index]:
            detail(field('Check detail', escape(verdict.detail)))


def _report_ci(next_action: str) -> None:
    """Report a conclusive successful CI observation."""
    output('Required checks passed.', kind='success')
    output()
    if next_action == 'pubanon repo reveal':
        output('Revealing the repository makes its history public.')
    next_step(next_action)


def _report_ci_start(
    target: tuple[str, str],
    selection: str,
    commit: str | None,
    checks: str,
    timeout: int,
) -> None:
    """Report the complete selected CI observation before host reads."""
    owner, repo = target
    selected = f' ({escape(commit[:12])})' if commit is not None else ''
    output(
        f'Waiting for checks on {escape(selection)}{selected} in {escape(owner)}/{escape(repo)}'
    )
    if commit is not None:
        detail_fields([('Selected commit', escape(commit))])
    fields(
        [
            ('Checks', checks),
            (
                'Timeout',
                (
                    f'{display_duration(timeout)}. Press Ctrl-C to stop waiting; '
                    'rerun the command to continue.'
                ),
            ),
        ]
    )


def _report_package(
    release: PackageRelease,
    next_action: str,
) -> None:
    """Report a conclusive successful package observation."""
    output(f'Package release v{escape(release.version)} is published.', kind='success')
    heading('Published files:', separate=True)
    for filename in release.files:
        output(f'  {escape(filename)}')
    output()
    if next_action == 'pubanon repo reveal':
        output('Revealing the repository makes its history public.')
    next_step(next_action)


def _report_package_start(
    target: tuple[str, str],
    selection: str,
    release: tuple[str, str],
    package: PackageObservation,
    timeout: int,
) -> None:
    """Report the complete selected package observation before host reads."""
    owner, repo = target
    version, commit = release
    output(
        f'Waiting for package {escape(package.name)} v{escape(version)} '
        f'({escape(commit[:12])}) in {escape(owner)}/{escape(repo)}'
    )
    detail_fields([('Selection', selection), ('Selected commit', escape(commit))])
    rendered = ', '.join(escape(name) for name in package.release_checks)
    fields(
        [
            ('Checks', rendered),
            (
                'Timeout',
                (
                    f'{display_duration(timeout)}. Press Ctrl-C to stop waiting; '
                    'rerun the command to continue.'
                ),
            ),
        ]
    )


def _select_ci_commit(paths: Paths, project: str, version: str | None) -> str:
    """Select the exact published commit for a CI observation."""
    main, tags = published_refs(paths, project)
    tag = tag_intent_for(version)
    if tag is None:
        if main is None:
            raise PubanonError('no published commit exists', recovery='pubanon publish')
        return main
    try:
        return tags[tag]
    except KeyError as exc:
        raise PubanonError(
            'the release selected by --version is not published',
            fact=tag,
            recovery='choose a published --version or omit --version',
        ) from exc


def _semantic_tag(tag: str) -> tuple[int, int, int] | None:
    """Return one strict release tag's numeric comparison key."""
    version = version_from_tag(tag)
    if version is None:
        return None
    major, minor, patch = version.split('.')
    return int(major), int(minor), int(patch)


def _timeout_error(stage: str, *, retry: str = 'rerun the watch') -> PubanonError:
    """Translate an observation timeout into one safe pending outcome."""
    return PubanonError(
        'observation timed out',
        fact=f'{escape(stage)}: pending',
        recovery=retry,
    )


def _remaining(deadline: float, stage: str, *, retry: str = 'rerun the watch') -> float:
    """Return positive remaining time or raise the exact pending stage."""
    remaining = deadline - _monotonic()
    if remaining <= 0:
        raise _timeout_error(stage, retry=retry)
    return remaining


def _verified_published_destination(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
    deadline: float,
    *,
    retry: str = 'rerun the watch',
) -> Destination:
    """Verify complete host refs against the local published-state copy."""
    local_main, local_tags = published_refs(paths, p.name)
    detail(field('Phase', 'verifying account and repository'))
    try:
        destination = verify_destination(
            paths,
            g,
            p,
            timeout=_remaining(deadline, 'account/repository verification', retry=retry),
        )
    except ObservationTimeoutError as exc:
        raise _timeout_error('account/repository verification', retry=retry) from exc
    expected = RemoteRefs(local_main, local_tags, ())
    detail(field('Phase', 'reading published refs'))
    try:
        remote = ls_remote(
            destination, paths, p.name, timeout=_remaining(deadline, 'refs', retry=retry)
        )
    except ObservationTimeoutError as exc:
        raise _timeout_error('refs', retry=retry) from exc
    if remote != expected:
        raise PubanonError(
            'host refs do not match local published history',
            expected=escape(remote_refs_text(expected)),
            actual=escape(remote_refs_text(remote)),
            recovery=(
                'remove every destination branch except main, then rerun'
                if remote.branches
                else 'pubanon repo restore --to local'
            ),
        )
    return destination


def _wait_for_checks(
    dest: Destination,
    commit: str,
    checks: Sequence[str],
    deadline: float,
    *,
    retry: str = 'rerun the watch',
) -> list[CheckVerdict]:
    """Wait for every exact required check within one shared deadline."""
    previous: tuple[CheckVerdict, ...] | None = None
    started = _monotonic()
    last_elapsed = started
    while True:
        verdicts: list[CheckVerdict] = []
        for name in checks:
            try:
                result = check_result(
                    (dest.owner, dest.repo),
                    commit,
                    name,
                    timeout=_remaining(deadline, name, retry=retry),
                )
            except ObservationTimeoutError as exc:
                raise _timeout_error(name, retry=retry) from exc
            if result.state not in {'missing', 'pending', 'success'}:
                raise PubanonError(
                    'required checks are not acceptable',
                    fact=_check_facts((result,)),
                    recovery=check_recovery((result,), retry=retry),
                )
            verdicts.append(result)
        current = tuple(verdicts)
        if current != previous:
            _report_check_changes(current, previous)
            previous = current
        unresolved = [verdict for verdict in verdicts if verdict.state != 'success']
        if not unresolved:
            return verdicts
        now = _monotonic()
        if now - last_elapsed >= _PROGRESS_SECONDS:
            elapsed = int(now - started)
            names = ', '.join(escape(verdict.name) for verdict in unresolved)
            output(f'Elapsed: {elapsed} seconds. Still waiting for {names}.', kind='warning')
            last_elapsed = now
        _pause_or_expire(
            deadline,
            PubanonError(
                'required checks are pending',
                fact=_check_facts(unresolved),
                recovery=check_recovery(unresolved, retry=retry),
            ),
        )


def _wait_for_run(
    dest: Destination, query: RunQuery, checks: Sequence[str], observation: _RunObservation
) -> RunEvidence:
    """Follow the latest run until conclusive evidence or the shared deadline."""
    deadline, retry, staged = observation
    previous: tuple[int | None, str] | None = None
    shown: set[int] = set()
    started = _monotonic()
    last_elapsed = started
    while True:
        try:
            evidence = run_evidence(
                (dest.owner, dest.repo),
                query,
                required=checks,
                remaining=lambda: _remaining(deadline, 'CI run', retry=retry),
            )
        except ObservationTimeoutError as exc:
            raise _timeout_error('CI run', retry=retry) from exc
        _remaining(deadline, 'CI run', retry=retry)
        run = evidence.run
        current = (run.number if run is not None else None, evidence.state)
        if run is not None and run.identifier not in shown:
            output(field('CI run', f'#{run.number} {escape(run.title)}'))
            output(field('Run URL', escape(run.url)))
            shown.add(run.identifier)
        if current != previous:
            output(_check_status(CheckVerdict('CI verdict', evidence.state, evidence.detail)))
            previous = current
        if evidence.state == 'success':
            return evidence
        if evidence.state not in {'missing', 'pending'}:
            raise PubanonError(
                'the latest CI run is not acceptable',
                fact=evidence_facts(evidence),
                recovery=evidence_recovery(evidence, staged=staged, retry=retry),
            )
        now = _monotonic()
        if now - last_elapsed >= _PROGRESS_SECONDS:
            output(
                f'Elapsed: {int(now - started)} seconds. Still waiting for CI run.', kind='warning'
            )
            last_elapsed = now
        _pause_or_expire(deadline, _timeout_error('CI run', retry=retry))


def ci_verdicts(
    dest: Destination,
    commit: str,
    checks: Iterable[str],
) -> list[CheckVerdict]:
    """Read each configured CI check once for an exact commit.

    Args:
        dest: Verified host destination to query.
        commit: Exact commit object ID to observe.
        checks: Configured check names in display order.

    Returns:
        One current verdict per configured check.

    Raises:
        PubanonError: A host response is invalid or cannot be read.
    """
    return [check_result((dest.owner, dest.repo), commit, name) for name in checks]


def select_package_release(
    paths: Paths,
    project: str,
    version: str | None,
) -> tuple[str, str]:
    """Select an exact published release version and its own tag commit.

    Args:
        paths: Derived Pubanon configuration and data paths.
        project: Registered project name.
        version: Optional exact `X.Y.Z` published release. When omitted,
            selects the numerically greatest strict `X.Y.Z` release tag,
            independently of commit recency and the published main branch.

    Returns:
        The selected version and exact tag commit object ID.

    Raises:
        PubanonError: The requested or default published release is absent, or
            the version is malformed.
    """
    _main, tags = published_refs(paths, project)
    if version is not None:
        tag = tag_intent_for(version)
        if tag is None:
            raise PubanonError('version must have X.Y.Z form')
        try:
            return version, tags[tag]
        except KeyError as exc:
            raise PubanonError(
                'the release selected by --version is not published',
                fact=tag,
                recovery='choose a published --version or omit --version',
            ) from exc

    releases = [
        (key, release_tag, commit)
        for release_tag, commit in tags.items()
        if (key := _semantic_tag(release_tag)) is not None
    ]
    if not releases:
        raise PubanonError(
            'no published release tag exists',
            recovery='publish a versioned snapshot before observing a package release',
        )
    _key, selected_tag, commit = max(releases, key=lambda release: release[0])
    return selected_tag[1:], commit


def watch_ci(  # noqa: PLR0913
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
    *,
    staged: bool,
    version: str | None,
    timeout: int,
) -> None:
    """Wait for required CI checks on one exact selected commit.

    Poll missing or pending checks within the shared timeout. Failed or
    ambiguous checks refuse immediately. Integrated projects follow the latest
    workflow run, including when no required check names are configured.

    Args:
        paths: Derived Pubanon configuration and data paths.
        g: Global publication identity configuration.
        p: Project publication configuration.

        staged: Whether to observe the exact recorded staged candidate.
        version: Optional exact `X.Y.Z` published release. When omitted and not
            staged, select the published main commit. Must be `None` when
            staged.
        timeout: Shared selection, verification, and polling budget in seconds.

    Raises:
        PubanonError: Selection, configuration, staging, or host facts are
            unsafe, a check is unacceptable, or checks remain unresolved
            through the deadline.
    """
    if staged and version is not None:
        raise PubanonError('the staging repository holds prepared snapshots, not releases')
    if not (p.required_checks or p.ci_workflow):
        raise PubanonError(
            'no required checks are configured',
            recovery='add required_checks to the project configuration',
        )
    retry = (
        'rerun `pubanon stage watch`'
        if staged
        else f'rerun `pubanon repo watch --version {escape(version)}`'
        if version is not None
        else 'rerun the watch'
    )
    deadline = _monotonic() + timeout
    checks = (
        ', '.join(escape(name) for name in p.required_checks)
        if p.required_checks
        else f'latest run of {escape(p.ci_workflow or "")}'
    )
    if staged:
        recorded = load_stage(paths, p.name)
        selected_commit = recorded.commit if recorded is not None else None
        _report_ci_start(
            (p.owner, stage_repository(p)),
            'recorded staged snapshot',
            selected_commit,
            checks,
            timeout,
        )
        detail(field('Phase', 'verifying the staged target'))
        try:
            dest, record = verify_staged_target(
                paths,
                g,
                p,
                timeout=_remaining(deadline, 'account/repository verification', retry=retry),
            )
        except ObservationTimeoutError as exc:
            raise _timeout_error(
                exc.stage or 'account/repository verification', retry=retry
            ) from exc
        commit = record.commit
    else:
        commit = _select_ci_commit(paths, p.name, version)
        selection = (
            'recorded published main' if version is None else f'exact tag v{escape(version)}'
        )
        _report_ci_start((p.owner, p.repo), selection, commit, checks, timeout)
        dest = _verified_published_destination(paths, g, p, deadline, retry=retry)
    detail(field('Phase', 'reading CI checks'))
    if p.ci_workflow:
        _wait_for_run(
            dest,
            RunQuery(p.ci_workflow, commit, 'workflow_dispatch' if staged else 'push'),
            p.required_checks,
            _RunObservation(deadline, retry, staged),
        )
    else:
        _wait_for_checks(dest, commit, p.required_checks, deadline, retry=retry)
    next_action = (
        candidate_next_action(paths, g, p, load_candidate(paths, p.name), checked_commit=commit)
        if staged
        else _published_next_action(p, version=version)
    )
    _report_ci(next_action)


def watch_package(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
    *,
    version: str | None,
    timeout: int,
) -> None:
    """Wait for release checks and one exact package-index release.

    Missing release metadata and metadata without distribution files are both
    pending states and continue polling.

    Args:
        paths: Derived Pubanon configuration and data paths.
        g: Global publication identity configuration.
        p: Project publication configuration.

        version: Optional exact `X.Y.Z` published release. When omitted,
            selects the numerically greatest strict `X.Y.Z` release tag,
            independently of commit recency and the published main branch.
        timeout: One deadline in seconds covering release selection,
            destination and reference verification, release checks, and every
            complete package fetch. Process scheduling and cleanup can add
            overhead.

    Raises:
        PubanonError: Package observation is disabled, selection or host facts
            are unsafe, a release check fails, or observation remains pending
            through the deadline.
    """
    package = p.package
    if package is None:
        raise PubanonError(
            'package observation is not enabled in [package] configuration',
            recovery='add a [package] table to the project configuration',
        )
    deadline = _monotonic() + timeout
    selected_version, commit = select_package_release(paths, p.name, version)
    retry = (
        f'rerun `pubanon package watch --version {escape(selected_version)}`'
        if version is not None
        else 'rerun the watch'
    )
    selection = (
        'numerically greatest published X.Y.Z tag'
        if version is None
        else f'exact tag v{escape(version)}'
    )
    _report_package_start(
        (p.owner, p.repo), selection, (selected_version, commit), package, timeout
    )
    dest = _verified_published_destination(paths, g, p, deadline, retry=retry)
    detail(field('Phase', 'reading package release checks'))
    _wait_for_checks(dest, commit, package.release_checks, deadline, retry=retry)
    detail(field('Phase', 'reading the package index'))
    previous: str | None = None
    started = _monotonic()
    last_elapsed = started
    while True:
        try:
            release = release_state(
                package.index,
                package.name,
                selected_version,
                timeout=_remaining(deadline, 'package release', retry=retry),
            )
        except ObservationTimeoutError as exc:
            raise _timeout_error('package release', retry=retry) from exc
        _remaining(deadline, 'package release', retry=retry)
        if release is not None and release.files:
            _report_package(release, _published_next_action(p, package_complete=True))
            return
        state = 'absent' if release is None else 'metadata found; no distribution files'
        if state != previous:
            output('  ' + field('Package', state, kind='warning'))
            previous = state
        now = _monotonic()
        if now - last_elapsed >= _PROGRESS_SECONDS:
            elapsed = int(now - started)
            output(
                f'Elapsed: {elapsed} seconds. Still waiting for package files.',
                kind='warning',
            )
            last_elapsed = now
        fact = f'{escape(package.name)} {escape(selected_version)} ' + (
            'is not yet published' if release is None else 'has metadata but no distribution files'
        )
        recovery = (
            f'wait for the package release, then {retry}'
            if release is None
            else f'wait for distribution files, then {retry}'
        )
        _pause_or_expire(
            deadline,
            PubanonError(
                'package release is pending',
                fact=fact,
                recovery=recovery,
            ),
        )
