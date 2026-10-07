"""Request staging CI and judge the latest requested run."""

import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import NamedTuple

from pubanon._errors import ObservationTimeoutError, OperationCanceled, PubanonError
from pubanon._remote import _host
from pubanon._remote._host import (
    CheckState,
    CheckVerdict,
    DispatchRejectedError,
    DispatchUncertainError,
    RunListingChangedError,
    WorkflowJob,
    WorkflowRun,
)
from pubanon._remote._workflow_checks import dispatch_contract_problem
from pubanon._run import run_git
from pubanon._terminal import display_check_result, escape

RUN_ON_PUSH_VARIABLE = 'CI_RUN_ON_PUSH'
_REGISTRATION_POLL_SECONDS = 5
_REGISTRATION_SECONDS = 60
_monotonic = time.monotonic
_sleep = time.sleep


class RunEvidence(NamedTuple):
    """Describe the latest requested run and its required-job verdicts.

    Attributes:
        state: `success`, `pending`, `missing`, `unacceptable`, or `ambiguous`.
            Only `success` permits publication.
        run: Latest run identified during observation, or `None` when no unique
            latest run was identified. Pending evidence may retain the run
            observed before a concurrent change.
        checks: Required-job verdicts in configured order, empty when no jobs
            were evaluated except for an incomplete run's pending checks.
        detail: Short reason for the overall verdict, empty on success.
            Host-provided content must be escaped before display.
    """

    state: CheckState
    run: WorkflowRun | None
    checks: tuple[CheckVerdict, ...]
    detail: str


class RunQuery(NamedTuple):
    """Select workflow evidence for one commit and trigger on main.

    Attributes:
        workflow: Configured workflow filename under `.github/workflows/`.
        commit: Exact commit the workflow run must test.
        event: Required trigger, `workflow_dispatch` for staged evidence or
            `push` for final-repository evidence.
    """

    workflow: str
    commit: str
    event: str


class StageCiPlan(NamedTuple):
    """Describe the requested run and its advisory staging effects.

    Attributes:
        workflow: Configured workflow filename.
        hosted: Whether to request hosted runners.
        full_tests: Whether to request the full local test selection.
        create_variable: Whether the push switch must be created.
        cancels: Incomplete recent requested runs on main.
        replaces: Highest-numbered successful requested run for this commit.
    """

    workflow: str
    hosted: bool
    full_tests: bool
    create_variable: bool
    cancels: tuple[WorkflowRun, ...]
    replaces: WorkflowRun | None


def _job_verdict(name: str, jobs: tuple[WorkflowJob, ...]) -> CheckVerdict:
    """Judge an exact required name against jobs from one run attempt."""
    matches = [job for job in jobs if job.name == name]
    if not matches:
        return CheckVerdict(name, 'unacceptable', 'not run')
    if len(matches) > 1:
        return CheckVerdict(name, 'ambiguous', f'{len(matches)} jobs')
    conclusion = matches[0].conclusion
    state = 'success' if conclusion == 'success' else 'unacceptable'
    return CheckVerdict(name, state, conclusion or 'null')


def _completed_evidence(
    run: WorkflowRun, jobs: tuple[WorkflowJob, ...], required: Sequence[str]
) -> RunEvidence:
    """Combine the run conclusion with exact required-job verdicts."""
    checks = tuple(_job_verdict(name, jobs) for name in required)
    if run.conclusion != 'success':
        return RunEvidence('unacceptable', run, checks, f'run {run.conclusion or "null"}')
    if any(check.state == 'ambiguous' for check in checks):
        state = 'ambiguous'
    elif any(check.state != 'success' for check in checks):
        state = 'unacceptable'
    else:
        state = 'success'
    return RunEvidence(state, run, checks, '')


def _latest(runs: tuple[WorkflowRun, ...]) -> WorkflowRun | None:
    """Return the highest numbered run only when its number is unique."""
    if not runs:
        return None
    latest = max(runs, key=lambda run: run.number)
    if sum(run.number == latest.number for run in runs) != 1:
        return None
    return latest


def _same_attempt(first: WorkflowRun, second: WorkflowRun | None) -> bool:
    """Compare the fields that must remain current across job observation."""
    return second is not None and (
        first.identifier,
        first.attempt,
        first.status,
        first.conclusion,
    ) == (second.identifier, second.attempt, second.status, second.conclusion)


def _wait_for_registration(coordinates: tuple[str, str], workflow: str) -> None:
    """Poll workflow registration within one shared deadline."""
    deadline = _monotonic() + _REGISTRATION_SECONDS
    while (remaining := deadline - _monotonic()) > 0:
        try:
            if (
                _host.workflow_registered(coordinates, workflow, timeout=remaining)
                and _monotonic() <= deadline
            ):
                break
        except ObservationTimeoutError:
            pass
        remaining = deadline - _monotonic()
        if remaining > 0:
            _sleep(min(_REGISTRATION_POLL_SECONDS, remaining))
    else:
        raise PubanonError(
            'GitHub has not registered the CI workflow yet',
            fact='The snapshot is staged. CI did not start.',
            recovery='wait a minute, then rerun pubanon stage push',
        )


def ensure_run_on_push_variable(coordinates: tuple[str, str], plan: StageCiPlan) -> None:
    """Create the staging push switch when planned and verify its host value.

    Args:
        coordinates: Verified staging repository owner and name.
        plan: Advisory plan made before confirmation.

    Raises:
        PubanonError: Creation or read-back fails, or the value is not false.
    """
    if not plan.create_variable:
        return
    _host.create_repository_variable(coordinates, RUN_ON_PUSH_VARIABLE, 'false')
    recovery = 'verify CI_RUN_ON_PUSH and GitHub access, then rerun pubanon stage push'
    try:
        value = _host.repository_variable(coordinates, RUN_ON_PUSH_VARIABLE)
    except OperationCanceled as exc:
        raise OperationCanceled(
            'Canceled after creating CI_RUN_ON_PUSH.',
            fact='The variable was created. Nothing was pushed.',
            recovery=recovery,
        ) from exc
    except (PubanonError, ObservationTimeoutError) as exc:
        reason = (
            'GitHub timed out while reading CI_RUN_ON_PUSH'
            if isinstance(exc, ObservationTimeoutError)
            else str(exc)
        )
        raise PubanonError(
            'CI_RUN_ON_PUSH was created, but its value could not be verified',
            fact=f'The variable was created. Nothing was pushed. {reason}',
            recovery=recovery,
        ) from exc
    if value is None or value.casefold() != 'false':
        raise PubanonError(
            'the staging repository variable CI_RUN_ON_PUSH is not false after creation',
            fact='The variable was created. Nothing was pushed. Its value could not be verified.'
            if value is None
            else f'The variable was created. Nothing was pushed. GitHub reported {escape(value)}.',
            recovery=recovery,
        )


def evidence_facts(evidence: RunEvidence) -> str:
    """Render escaped run identity and unsuccessful checks for refusal facts.

    Returns:
        `no requested run` when no run was identified; otherwise the run
        number, title, URL, and failing checks in configured order. A nonempty
        detail appears only when there are no unsuccessful checks.
    """
    if evidence.run is None:
        return 'no requested run'
    run = evidence.run
    fact = f'#{run.number} {escape(run.title)} ({escape(run.url)})'
    checks = [
        f'{escape(check.name)}: {display_check_result(check)}'
        for check in evidence.checks
        if check.state != 'success'
    ]
    detail = ', '.join(checks) if checks else escape(evidence.detail)
    return f'{fact}; {detail}' if detail else fact


def evidence_recovery(evidence: RunEvidence, *, staged: bool, retry: str) -> str:
    """Explain how to recover from refused workflow evidence.

    Args:
        evidence: Latest observed run verdict.
        staged: Whether a staging dispatch can request replacement evidence.
        retry: Caller-provided next command or action, already safe for
            display.

    Returns:
        One recovery sentence ending in `then {retry}`, or an empty string for
        successful evidence. Host-provided names and URLs are escaped.
    """
    unexecuted = [
        escape(check.name) for check in evidence.checks if check.detail in {'not run', 'skipped'}
    ]
    if evidence.state == 'success':
        return ''
    if evidence.state == 'missing':
        action = (
            'request a run with pubanon stage push'
            if staged
            else 'wait for the push CI run to appear'
        )
    elif evidence.state == 'pending':
        action = 'wait for the run to complete'
    elif evidence.run is not None and evidence.run.conclusion == 'cancelled':
        action = (
            'the latest run was cancelled, possibly superseded; '
            'request a new run with pubanon stage push'
            if staged
            else 'the latest run was cancelled; rerun the workflow on GitHub'
        )
    elif evidence.state == 'ambiguous':
        action = 'resolve the duplicate runs or job names'
    elif unexecuted:
        action = f'the latest run did not execute {", ".join(unexecuted)}; '
        action += (
            'request a run that executes them, for example pubanon stage push --hosted'
            if staged
            else 'rerun the workflow on GitHub so it executes them'
        )
    else:
        url = f' ({escape(evidence.run.url)})' if evidence.run is not None else ''
        action = f'inspect the failed run{url} and fix or rerun it'
    return f'{action}, then {retry}'


def plan_stage_ci(
    coordinates: tuple[str, str], workflow: str, commit: str, *, hosted: bool, full_tests: bool
) -> StageCiPlan:
    """Preview variable creation, cancellations, and replaced run evidence.

    Args:
        coordinates: Verified staging repository owner and name.
        workflow: Configured workflow filename.
        commit: Prepared snapshot commit.
        hosted: Whether the requested run uses hosted runners.
        full_tests: Whether the requested run includes full local tests.

    Returns:
        One-page advisory preview; it never determines publication acceptance.

    Raises:
        PubanonError: The push switch conflicts or host reads fail.
    """
    value = _host.repository_variable(coordinates, RUN_ON_PUSH_VARIABLE)
    if value is not None and value.casefold() != 'false':
        raise PubanonError(
            'the staging repository variable CI_RUN_ON_PUSH has an unexpected value',
            fact=escape(value),
            recovery=(
                'set CI_RUN_ON_PUSH to false or delete it on GitHub, then rerun pubanon stage push'
            ),
        )
    runs = _host.recent_workflow_runs(coordinates, workflow, event='workflow_dispatch')
    cancels = tuple(run for run in runs if run.status != 'completed')
    latest = max(
        (run for run in runs if run.commit == commit), key=lambda run: run.number, default=None
    )
    replaces = (
        latest
        if latest is not None and latest.status == 'completed' and latest.conclusion == 'success'
        else None
    )
    return StageCiPlan(workflow, hosted, full_tests, value is None, cancels, replaces)


def require_ci_contract(store: Path, commit: str, workflow: str) -> None:
    """Require the prepared workflow blob to declare both boolean dispatch inputs.

    Args:
        store: Bare snapshot store containing the selected commit.
        commit: Exact prepared snapshot commit.
        workflow: Configured workflow filename.

    Raises:
        PubanonError: The blob is absent or has an unusable dispatch contract.
    """
    path = f'.github/workflows/{workflow}'
    result = run_git(
        ['--git-dir', str(store.absolute()), 'cat-file', 'blob', f'{commit}:{path}'],
        cwd=store,
        check=False,
    )
    if result.returncode:
        raise PubanonError(
            f'the prepared snapshot has no {path}',
            recovery='add the workflow or correct ci_workflow, then prepare a new snapshot',
        )
    problem = dispatch_contract_problem(result.stdout)
    if problem is not None:
        raise PubanonError(
            f'{path} cannot receive pubanon CI requests',
            fact=problem,
            recovery=(
                'declare the boolean workflow_dispatch inputs hosted and full_tests, '
                'then prepare a new snapshot'
            ),
        )


def run_evidence(
    coordinates: tuple[str, str],
    query: RunQuery,
    *,
    required: Sequence[str],
    remaining: Callable[[], float | None] | None = None,
) -> RunEvidence:
    """Judge the latest matching run and confirm its attempt stayed current.

    Args:
        coordinates: Verified repository owner and name.
        query: Workflow, exact commit, and event to query on branch main.
        required: Exact job names, evaluated in configured order. With no
            required names, the run conclusion alone decides acceptance.
        remaining: Shared deadline callback forwarded to every listing. The
            host evaluates it immediately before each page request, using its
            result as the timeout. `None` means no deadline; callback errors
            propagate without issuing another request.

    Returns:
        Fresh evidence without caching. A changing run listing is pending;
        a successful result confirms only this observation, not future runs.

    Raises:
        PubanonError: A host request fails or its response is invalid.
        ObservationTimeoutError: The shared budget or a host request expires.
    """
    try:
        runs = _host.list_workflow_runs(
            coordinates, query.workflow, query.commit, event=query.event, remaining=remaining
        )
    except RunListingChangedError:
        return RunEvidence('pending', None, (), 'runs changed while being read')
    if not runs:
        return RunEvidence('missing', None, (), '')
    latest = _latest(runs)
    if latest is None:
        return RunEvidence('ambiguous', None, (), 'duplicate run numbers')
    if latest.status != 'completed':
        checks = tuple(CheckVerdict(name, 'pending', latest.status) for name in required)
        return RunEvidence('pending', latest, checks, f'run {latest.status}')
    jobs = _host.list_run_jobs(coordinates, latest.identifier, latest.attempt, remaining=remaining)
    try:
        confirmed = _latest(
            _host.list_workflow_runs(
                coordinates, query.workflow, query.commit, event=query.event, remaining=remaining
            )
        )
    except RunListingChangedError:
        confirmed = None
    if not _same_attempt(latest, confirmed):
        return RunEvidence('pending', latest, (), 'a newer run or attempt appeared')
    return _completed_evidence(latest, jobs, required)


def start_stage_ci(
    coordinates: tuple[str, str], workflow: str, commit: str, *, hosted: bool, full_tests: bool
) -> WorkflowRun:
    """Wait for workflow registration, dispatch once, and verify the returned run.

    Args:
        coordinates: Verified staging repository owner and name.
        workflow: Configured workflow filename.
        commit: Prepared snapshot commit expected in the returned run.
        hosted: Whether to request hosted runners.
        full_tests: Whether to request full local tests.

    Returns:
        Verified run for the staged commit.

    Raises:
        PubanonError: Registration, dispatch, or run identity cannot be
            confirmed.
    """
    _wait_for_registration(coordinates, workflow)
    try:
        run_id = _host.dispatch_workflow(
            coordinates, workflow, ref='main', inputs={'hosted': hosted, 'full_tests': full_tests}
        )
    except DispatchRejectedError as exc:
        raise PubanonError(
            'GitHub refused the CI request',
            fact=f'The snapshot is staged. No CI run started. {exc}',
            recovery='correct the reported problem, then rerun pubanon stage push',
        ) from exc
    except DispatchUncertainError as exc:
        raise PubanonError(
            'CI may have started',
            fact='The snapshot is staged. GitHub did not confirm the CI request.',
            recovery=(
                'run pubanon stage status to see runs for this commit; '
                'rerun pubanon stage push to request another run'
            ),
        ) from exc
    try:
        run = _host.read_workflow_run(coordinates, run_id)
    except OperationCanceled:
        raise
    except (PubanonError, ObservationTimeoutError) as exc:
        raise PubanonError(
            'CI started, but its run could not be verified',
            fact='The snapshot is staged.',
            recovery='pubanon stage status',
        ) from exc
    if (
        run.path != f'.github/workflows/{workflow}'
        or run.event != 'workflow_dispatch'
        or run.branch != 'main'
    ):
        raise PubanonError(
            'CI started, but its run could not be verified',
            fact='The snapshot is staged.',
            recovery='pubanon stage status',
        )
    if run.commit != commit:
        raise PubanonError(
            'main moved after the push; the requested run tests another commit',
            expected=commit,
            actual=escape(run.commit),
            fact='That run does not count for the staged snapshot.',
            recovery='run pubanon stage status, then pubanon stage push',
        )
    return run
