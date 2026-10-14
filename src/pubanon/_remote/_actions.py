"""Check GitHub Actions run details, runners, and job logs before reveal."""

import re
from bisect import bisect_left, bisect_right
from collections.abc import Collection, Iterable, Mapping, Sequence
from typing import NamedTuple

from pubanon import _history_output
from pubanon._config import GlobalConfig, ProjectConfig
from pubanon._errors import OperationCanceled, PubanonError
from pubanon._gate import GateHit, RuleClass, first_match, gate_unit
from pubanon._remote import _host
from pubanon._remote._host import ActionsJob, ActionsRun
from pubanon._terminal import (
    ask,
    confirm,
    detail_fields,
    escape,
    fields,
    output,
    style,
    verbose_enabled,
)

_ESCAPE_SEQUENCE = re.compile(
    rb'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)'
    rb'|[PX^_][^\x1b]*\x1b\\|[ -/]*[0-~])'
)
_HEADLINES = {
    RuleClass.IDENTITY_CRITICAL: 'Sensitive terms in Actions logs',
    RuleClass.HYGIENE: 'Policy terms in Actions logs',
}
_PAIR_SIZE = 2
_REVEAL_COMMAND = 'pubanon repo reveal'


class ActionsFinding(NamedTuple):
    """Locate one configured term in Actions content.

    Attributes:
        run: Identifier of the run whose content matched.
        location: Unescaped row label: `run details`, `job NAME`, or `runner
            for job NAME`, with the attempt when a run has several.
        job: Identifier of the job whose log matched, or None for run and
            runner details.
        hit: Matched term and rule class; `hit.unit` is unique per location.
        lines: One-based log lines containing the term, gathered only in
            verbose mode.
    """

    run: int
    location: str
    job: int | None
    hit: GateHit
    lines: tuple[int, ...]


class ActionsReview(NamedTuple):
    """Hold one complete check of a repository's Actions content.

    Attributes:
        coordinates: Configured repository owner and name.
        runs: Completed runs in GitHub's order.
        jobs: Jobs from every attempt of every run.
        read: Identifiers of jobs whose logs were read.
        gone: Number of jobs that ran whose logs are gone.
        findings: Matches in the order they were found.
        command: Command that reruns the check, named in recovery and hint
            text.
    """

    coordinates: tuple[str, str]
    runs: tuple[ActionsRun, ...]
    jobs: tuple[ActionsJob, ...]
    read: frozenset[int]
    gone: int
    findings: tuple[ActionsFinding, ...]
    command: str = _REVEAL_COMMAND


def _checked(review: ActionsReview) -> int:
    """Count jobs that ran and whose logs were read."""
    return sum(1 for job in review.jobs if job.ran and job.identifier in review.read)


def _counted(count: int, noun: str) -> str:
    """Return a count with a regular plural noun."""
    return f'{count} {noun}' if count == 1 else f'{count} {noun}s'


def _job_label(prefix: str, job: ActionsJob, attempts: Mapping[int, int]) -> str:
    """Name one job row, adding the attempt when its run has several."""
    if attempts[job.run] > 1:
        return f'{prefix}{job.name} (attempt {job.attempt})'
    return f'{prefix}{job.name}'


def _job_state(jobs: Iterable[ActionsJob]) -> list[int]:
    """Return the sorted identifiers of a snapshot's jobs."""
    return sorted(job.identifier for job in jobs)


def _jobs(
    coordinates: tuple[str, str], runs: Iterable[ActionsRun], command: str
) -> tuple[ActionsJob, ...]:
    """Read the jobs of every attempt of every run."""
    owner, repo = coordinates
    return tuple(
        job
        for run in runs
        for job in _host.actions_jobs(owner, repo, run.identifier, command=command)
    )


def _log_error(job: ActionsJob, reason: str, command: str) -> PubanonError:
    """Describe one unusable job log without quoting GitHub's response."""
    return PubanonError(
        'cannot check Actions logs',
        fact=f'job {job.identifier}: {reason}',
        recovery=f'rerun {command}',
    )


def _outcomes(runs: Sequence[int], sent: Sequence[int], verified: Sequence[int]) -> str:
    """List runs with deleted logs, uncertain outcomes, and untouched logs."""
    groups = (
        ('Deleted', [run for run in runs if run in verified]),
        ('Outcome unknown', [run for run in sent if run not in verified]),
        ('Not reached', [run for run in runs if run not in sent]),
    )
    return '\n'.join(
        f'{label}: ' + ', '.join(f'run {run}' for run in group) for label, group in groups if group
    )


def _plain_chunks(data: bytes) -> Iterable[tuple[int, bytes]]:
    """Yield nonempty content spans and their offsets with terminal escapes removed."""
    start = 0
    for match in _ESCAPE_SEQUENCE.finditer(data):
        if match.start() > start:
            yield start, data[start : match.start()]
        start = match.end()
    if start < len(data):
        yield start, data[start:]


def _log_lines(log: bytes, hit: GateHit) -> tuple[int, ...]:
    """Return original log lines contributing raw or escape-stripped term matches."""
    rules = {hit.rule_class: (hit.term,)}
    lines = {
        number
        for number, line in enumerate(log.split(b'\n'), start=1)
        if gate_unit(hit.unit, line, rules)
    }
    chunks = list(_plain_chunks(log))
    stripped = b''.join(chunk for _source, chunk in chunks)
    if stripped == log:
        return tuple(sorted(lines))
    newlines = [match.start() for match in re.finditer(b'\n', log)]
    ends: list[int] = []
    length = 0
    for _source, chunk in chunks:
        length += len(chunk)
        ends.append(length)
    offset = 0
    for line in stripped.split(b'\n'):
        cursor = 0
        while (span := first_match(line[cursor:], hit.term)) is not None:
            start, end = offset + cursor + span[0], offset + cursor + span[1]
            if start == end:
                break
            # Only retained bytes contribute lines; discarded control payloads do not.
            for index in range(bisect_right(ends, start), bisect_left(ends, end) + 1):
                source, chunk = chunks[index]
                original = source + max(0, start - (ends[index] - len(chunk)))
                lines.add(bisect_left(newlines, original) + 1)
            cursor += span[0] + 1
        offset += len(line) + 1
    return tuple(sorted(lines))


def _matches(unit: str, data: bytes, rules: Mapping[RuleClass, Iterable[str]]) -> list[GateHit]:
    """Match content with and without terminal escape sequences, once per term."""
    hits = gate_unit(unit, data, rules)
    stripped = b''.join(chunk for _source, chunk in _plain_chunks(data))
    if stripped != data:
        hits.extend(gate_unit(unit, stripped, rules))
    unique: dict[tuple[RuleClass, str], GateHit] = {}
    for hit in hits:
        unique.setdefault((hit.rule_class, hit.term), hit)
    return list(unique.values())


def _read_log(coordinates: tuple[str, str], job: ActionsJob, command: str) -> bytes | None:
    """Return a job's log, or None when the job never started or its log is gone."""
    owner, repo = coordinates
    log = _host.job_log(owner, repo, job.identifier, command=command)
    if isinstance(log, bytes):
        if log or not job.ran:
            return log or None
        raise _log_error(job, 'GitHub returned an empty log', command)
    if not job.ran:
        return None
    if log == 'storage-404':
        raise _log_error(job, 'its log is not available yet', command)
    try:
        current = _host.read_actions_job(owner, repo, job.identifier, job.run)
    except PubanonError:
        current = None
    if current != job:
        raise _log_error(job, 'GitHub did not confirm that its log is gone', command)
    return None


def _require_gone(coordinates: tuple[str, str], job: ActionsJob, command: str) -> None:
    """Reject a previously read job log that GitHub still serves."""
    if _read_log(coordinates, job, command) is not None:
        raise PubanonError('a deleted log is still available', fact=f'job {job.identifier}')


def _delete(review: ActionsReview, runs: Sequence[int], account: str) -> None:
    """Delete run logs and confirm that every previously read log is gone."""
    owner, repo = review.coordinates
    sent: list[int] = []
    verified: list[int] = []
    try:
        for run in runs:
            _host.require_authenticated_account(account, authority='configuration')
            sent.append(run)
            _host.delete_run_logs(owner, repo, run)
        for run in runs:
            for job in review.jobs:
                if job.run != run or job.identifier not in review.read:
                    continue
                _require_gone(review.coordinates, job, review.command)
            _host.require_authenticated_account(account, authority='configuration')
            verified.append(run)
    except KeyboardInterrupt:
        raise OperationCanceled(
            'Canceled while deleting Actions logs.',
            fact=_outcomes(runs, sent, verified),
            recovery=f'rerun {review.command}',
        ) from None
    except PubanonError as exc:
        facts = [str(exc)]
        if exc.fact:
            facts.append(exc.fact)
        facts.append(_outcomes(runs, sent, verified))
        raise PubanonError(
            'deleting Actions logs did not finish',
            fact='\n'.join(facts),
            recovery=f'rerun {review.command}',
        ) from None
    output(f'Deleted the logs of {_counted(len(runs), "run")}.', kind='success')


def _row_details(
    coordinates: tuple[str, str], run: int, job: int, lines: Collection[int]
) -> list[str]:
    """Link one log row to its job and list the matching log lines."""
    owner, repo = coordinates
    details = [escape(f'https://github.com/{owner}/{repo}/actions/runs/{run}/job/{job}')]
    if lines:
        details.append('Log lines: ' + ', '.join(str(line) for line in sorted(lines)))
    return details


def _run_state(runs: Iterable[ActionsRun]) -> list[tuple[int, int, str]]:
    """Return each run's identifier, attempt, and status in identifier order."""
    return sorted((run.identifier, run.attempt, run.status) for run in runs)


def _series(parts: Sequence[str]) -> str:
    """Join list items, adding a final comma when an item already contains `and`."""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == _PAIR_SIZE and not any(' and ' in part for part in parts):
        return f'{parts[0]} and {parts[1]}'
    return ', '.join(parts[:-1]) + f', and {parts[-1]}'


def cancel_message(deleted: Sequence[int]) -> str:
    """Return the cancellation message, naming any logs already deleted.

    Args:
        deleted: Runs whose logs this reveal deleted.
    """
    if not deleted:
        return 'Canceled. Nothing was changed.'
    return (
        f'Canceled. Deleted the logs of {_counted(len(deleted), "run")}; nothing else was changed.'
    )


def _deletion_chosen(review: ActionsReview, runs: Sequence[int], *, cancelable: bool) -> bool:
    """Ask whether to delete the matching runs' logs; True only after confirmation."""
    choices = ['delete', 'keep', 'cancel'] if cancelable else ['delete', 'keep']
    names = {run.identifier: run.workflow for run in review.runs}
    listing = '; '.join(f'{run}, {escape(names[run])}' for run in runs)
    count = _counted(len(runs), 'run')
    try:
        choice = ask('What should happen to these logs?', choices=choices)
        if choice == 'delete' and any(item.job is None for item in review.findings):
            output('Run and runner details stay; deleting logs cannot remove them.')
        confirmed = choice == 'delete' and confirm(
            f'Delete the logs of {count} ({listing})? This cannot be undone.'
        )
    except OperationCanceled:
        raise
    except PubanonError:
        if cancelable:
            raise
        output(
            'warning: could not ask about deleting Actions logs; nothing was deleted.',
            kind='warning',
        )
        return False
    if cancelable and not confirmed and choice != 'keep':
        raise OperationCanceled(cancel_message(()))
    return confirmed


def clean_sentence(review: ActionsReview) -> str:
    """Summarize a check without findings in one sentence for audit.

    Args:
        review: Completed Actions check without findings.
    """
    if not review.runs:
        return 'No Actions runs to check.'
    text = (
        f'No configured sensitive terms were found in the Actions logs: {_checked(review)} checked'
    )
    if review.gone:
        text += f', {review.gone} no longer available'
    return f'{text}.'


def deletion_note(deleted: Sequence[int]) -> str:
    """Name logs this reveal deleted, for the details of a later error.

    Args:
        deleted: Runs whose logs this reveal deleted; must not be empty.
    """
    return f'Deleted the logs of {_counted(len(deleted), "run")}.'


def require_unchanged(review: ActionsReview) -> None:
    """Refuse when runs, attempts, statuses, or jobs differ from a review.

    Separate requests cannot give an atomic view: a run that starts after this
    read is not checked.

    Args:
        review: Completed Actions check to compare with.

    Raises:
        PubanonError: GitHub's runs or jobs differ, or a read fails.
    """
    owner, repo = review.coordinates
    runs = _host.actions_runs(owner, repo, command=review.command)
    if _run_state(runs) == _run_state(review.runs) and _job_state(
        _jobs(review.coordinates, runs, review.command)
    ) == _job_state(review.jobs):
        return
    raise PubanonError('Actions runs changed during the check', recovery=f'rerun {review.command}')


def check(
    coordinates: tuple[str, str],
    account: str,
    rules: Mapping[RuleClass, Iterable[str]],
    *,
    command: str = _REVEAL_COMMAND,
) -> ActionsReview:
    """Read and match every run's details, every job's runner, and every job log.

    Logs stay in memory and are never printed or saved. A job that ran must
    yield a log, or a log that GitHub's API reports missing while the job
    itself still reads back unchanged under the configured account.

    Args:
        coordinates: Configured repository owner and name.
        account: Configured account, checked again after any gone log.
        rules: Content rules from `assemble_rules`.
        command: Command that reruns the check, named in recovery text.

    Returns:
        The complete review, confirmed unchanged by a second read.

    Raises:
        PubanonError: A run is unfinished, a read fails or is invalid, a log
            cannot be confirmed, the account changed, or the runs changed.
    """
    owner, repo = coordinates
    runs = _host.actions_runs(owner, repo, command=command)
    if any(run.status != 'completed' for run in runs):
        raise PubanonError(
            'workflow runs are still running',
            recovery=f'wait for them to finish, then rerun {command}',
        )
    jobs = _jobs(coordinates, runs, command)
    attempts = {run.identifier: run.attempt for run in runs}
    findings = [
        ActionsFinding(run.identifier, 'run details', None, hit, ())
        for run in runs
        for hit in _matches(f'run {run.identifier} details', run.details, rules)
    ]
    if jobs:
        output(
            f'Checking Actions logs: {_counted(len(runs), "run")}, {_counted(len(jobs), "job")}.'
        )
    read: set[int] = set()
    gone = 0
    for job in jobs:
        findings.extend(
            ActionsFinding(job.run, _job_label('runner for job ', job, attempts), None, hit, ())
            for hit in _matches(f'job {job.identifier} runner', job.runner, rules)
        )
        log = _read_log(coordinates, job, command)
        if log is None:
            if job.ran:
                gone += 1
            continue
        read.add(job.identifier)
        findings.extend(
            ActionsFinding(
                job.run,
                _job_label('job ', job, attempts),
                job.identifier,
                hit,
                _log_lines(log, hit) if verbose_enabled() else (),
            )
            for hit in _matches(f'job {job.identifier} log', log, rules)
        )
    if gone:
        _host.require_authenticated_account(account, authority='configuration')
    review = ActionsReview(
        coordinates, runs, jobs, frozenset(read), gone, tuple(findings), command
    )
    require_unchanged(review)
    return review


def row(review: ActionsReview) -> str:
    """Summarize a check for the verification table; counts cover jobs that ran.

    Args:
        review: Completed Actions check.
    """
    checked = _checked(review)
    if not checked and not review.gone:
        counts = 'none'
    else:
        counts = f'{checked} checked'
        if review.gone:
            counts += f', {review.gone} no longer available'
    if review.findings:
        return f'{counts}; findings listed below'
    return counts if counts == 'none' else f'{counts}; no sensitive terms'


def show_unchecked(
    coordinates: tuple[str, str],
    runs: Sequence[int],
    *,
    artifacts: int,
    caches: int,
) -> None:
    """Name the Actions content reveal cannot check, with links to review it.

    Args:
        coordinates: Configured repository owner and name.
        runs: Workflow run identifiers, linked only in verbose mode.
        artifacts: Number of artifacts whose contents stay unchecked.
        caches: Number of Actions caches whose contents stay unchecked.
    """
    owner, repo = coordinates
    contents = [
        _counted(count, noun)
        for count, noun in ((artifacts, 'artifact'), (caches, 'Actions cache'))
        if count
    ]
    parts = [f'the contents of {" and ".join(contents)}'] if contents else []
    if runs:
        parts.append('job summaries and annotations')
    parts.append('earlier pushes on the Activity page')
    output(f'{style("Not checked:")} {_series(parts)}.')
    fields(
        [
            ('Actions', f'https://github.com/{owner}/{repo}/actions'),
            ('Activity', f'https://github.com/{owner}/{repo}/activity'),
        ],
        indent=2,
    )
    detail_fields(
        [(f'Run {run}', f'https://github.com/{owner}/{repo}/actions/runs/{run}') for run in runs],
        indent=2,
    )


def summary(
    review: ActionsReview,
    configs: tuple[GlobalConfig, ProjectConfig],
    derived: Collection[str],
) -> list[str]:
    """Summarize findings by rule class and run, oldest run first.

    Rows show configured terms, never log text. Verbose mode links each log row
    to its job, lists its log lines, and shows every row.

    Args:
        review: Completed Actions check.
        configs: Global and project rules used to label each finding's origin.
        derived: Private terms derived from the active environment.

    Returns:
        Escaped, styled display lines, or an empty list without findings.
    """
    verbose = verbose_enabled()
    lines: list[str] = []
    for rule_class, headline in _HEADLINES.items():
        selected = [item for item in review.findings if item.hit.rule_class is rule_class]
        if not selected:
            continue
        groups: list[_history_output.SummaryGroup] = []
        for run in sorted(review.runs, key=lambda item: (item.created_at, item.identifier)):
            rows: dict[str, tuple[str, list[str], int | None, set[int]]] = {}
            for item in selected:
                if item.run == run.identifier:
                    entry = rows.setdefault(item.hit.unit, (item.location, [], item.job, set()))
                    entry[1].append(item.hit.term)
                    entry[3].update(item.lines)
            if not rows:
                continue
            heading = (
                f'Run {run.identifier}, {escape(run.workflow)} on {escape(run.event)}, '
                f'commit {run.commit[:12]}, {run.created_at[:10]}:'
            )
            groups.append(
                (
                    heading,
                    [
                        (
                            escape(location),
                            terms,
                            _row_details(review.coordinates, run.identifier, job, numbers)
                            if verbose and job is not None
                            else (),
                        )
                        for location, terms, job, numbers in rows.values()
                    ],
                )
            )
        counts = (
            len({item.hit.term for item in selected}),
            len({item.hit.unit for item in selected}),
        )
        lines.extend(_history_output.summary_block(headline, counts, groups, unlimited=verbose))
        rules = _history_output.rules_line((item.hit for item in selected), configs, derived)
        lines.append('  ' + escape(rules))
    if lines and not verbose:
        lines.append(f'  {review.command} --verbose links each row to its job and log lines.')
    return lines


def _present(
    review: ActionsReview,
    configs: tuple[GlobalConfig, ProjectConfig],
    derived: Collection[str],
    *,
    already_public: bool,
) -> tuple[int, ...]:
    """Show findings and what publishing them means; return runs with matching logs."""
    for line in summary(review, configs, derived):
        output(line)
    log_runs = tuple(dict.fromkeys(item.run for item in review.findings if item.job is not None))
    if not log_runs:
        output(
            'These run details are already public.'
            if already_public
            else 'Making the repository public publishes these run details.'
        )
        output('Deleting logs cannot remove them; the guide explains deleting a run on GitHub.')
        return ()
    output(
        'These logs are already public. Deleting them stops GitHub from serving them; copies '
        'made meanwhile remain.'
        if already_public
        else "Making the repository public publishes these logs. Deleting a run's logs removes "
        'them for every job in that run; the run and its results stay.'
    )
    return log_runs


def decide(
    review: ActionsReview,
    configs: tuple[GlobalConfig, ProjectConfig],
    derived: Collection[str],
    account: str,
    *,
    already_public: bool,
) -> tuple[int, ...]:
    """Show Actions findings, then keep, delete, or cancel.

    Deleting needs a second confirmation and is verified before returning.

    Args:
        review: Completed Actions check.
        configs: Global and project rules used to label each finding's origin.
        derived: Private terms derived from the active environment.
        account: Configured account, checked before deleting and after gone
            logs.
        already_public: Whether GitHub already shows the repository as public.

    Returns:
        Runs whose logs were deleted and confirmed gone; empty otherwise.

    Raises:
        OperationCanceled: The user canceled before changing anything, or
            interrupted deletion and the error lists each run's outcome.
        PubanonError: Deletion did not finish; the error lists each run's
            outcome.
    """
    if not review.findings:
        return ()
    log_runs = _present(review, configs, derived, already_public=already_public)
    if not log_runs:
        if ask('What should happen to these run details?', choices=['keep', 'cancel']) == 'cancel':
            raise OperationCanceled(cancel_message(()))
        return ()
    if not _deletion_chosen(review, log_runs, cancelable=True):
        return ()
    _delete(review, log_runs, account)
    return log_runs


def offer_log_deletion(
    review: ActionsReview,
    configs: tuple[GlobalConfig, ProjectConfig],
    derived: Collection[str],
    account: str,
    *,
    already_public: bool,
) -> tuple[int, ...]:
    """Show Actions findings, then delete matching logs or keep them.

    For checks outside reveal, where nothing follows that canceling would
    prevent. Findings only in run or runner details ask nothing. Declining the
    second confirmation keeps the logs, and a terminal that cannot answer
    deletes nothing and prints a warning.

    Args:
        review: Completed Actions check.
        configs: Global and project rules used to label each finding's origin.
        derived: Private terms derived from the active environment.
        account: Configured account, checked before deleting and after gone
            logs.
        already_public: Whether GitHub already shows the repository as public.

    Returns:
        Runs whose logs were deleted and confirmed gone; empty otherwise.

    Raises:
        OperationCanceled: The user pressed Ctrl-C, or interrupted deletion and
            the error lists each run's outcome.
        PubanonError: Deletion did not finish; the error lists each run's
            outcome.
    """
    if not review.findings:
        return ()
    log_runs = _present(review, configs, derived, already_public=already_public)
    if not log_runs or not _deletion_chosen(review, log_runs, cancelable=False):
        return ()
    _delete(review, log_runs, account)
    return log_runs
