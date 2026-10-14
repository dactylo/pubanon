import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from pubanon._config import GlobalConfig, ProjectConfig
from pubanon._errors import OperationCanceled, PubanonError
from pubanon._gate import RuleClass
from pubanon._remote import _actions, _host
from pubanon._remote._host import ActionsJob, ActionsRun, LogAbsence
from pubanon._terminal import color_context, escape, verbose_context
from tests._setup import configure

_ACCOUNT = 'anonymous-account'
_AUDIT_MENU = 'What should happen to these logs? [d]elete / [k]eep: '
_COORDINATES = ('anonymous-account', 'public-repo')
_DETAILS_MENU = 'What should happen to these run details? [k]eep / [c]ancel: '
_MENU = 'What should happen to these logs? [d]elete / [k]eep / [c]ancel: '
_RULES = {RuleClass.IDENTITY_CRITICAL: ('term-a',), RuleClass.HYGIENE: ('style-term',)}
_RUN = 40000000001
_SECOND = 40000000002
_THIRD = 40000000003
_Script = Callable[[Sequence[str | tuple[str, str]]], None]


class _Host:
    def __init__(self) -> None:
        self.account = _ACCOUNT
        self.account_checks = 0
        self.commands: list[str] = []
        self.delete_errors: dict[int, BaseException] = {}
        self.deleted: list[int] = []
        self.jobs: dict[int, tuple[ActionsJob, ...]] = {}
        self.kept: set[int] = set()
        self.log_calls: list[int] = []
        self.log_errors: dict[int, PubanonError] = {}
        self.logs: dict[int, bytes | LogAbsence] = {}
        self.reads: dict[int, ActionsJob | BaseException] = {}
        self.runs: list[tuple[ActionsRun, ...]] = [()]
        self.switch_after_delete = False

    def actions_jobs(
        self, _owner: str, _repo: str, run_id: int, *, command: str
    ) -> tuple[ActionsJob, ...]:
        self.commands.append(command)
        return self.jobs.get(run_id, ())

    def actions_runs(self, _owner: str, _repo: str, *, command: str) -> tuple[ActionsRun, ...]:
        self.commands.append(command)
        return self.runs.pop(0) if len(self.runs) > 1 else self.runs[0]

    def delete_run_logs(self, _owner: str, _repo: str, run_id: int) -> None:
        error = self.delete_errors.get(run_id)
        if error is not None:
            raise error
        self.deleted.append(run_id)
        if run_id not in self.kept:
            for job in self.jobs[run_id]:
                self.logs[job.identifier] = 'api-404'
        if self.switch_after_delete:
            self.account = 'other-account'

    def job_log(self, _owner: str, _repo: str, job_id: int, *, command: str) -> bytes | LogAbsence:
        self.commands.append(command)
        self.log_calls.append(job_id)
        error = self.log_errors.get(job_id)
        if error is not None:
            raise error
        return self.logs[job_id]

    def read_actions_job(self, _owner: str, _repo: str, job_id: int, run_id: int) -> ActionsJob:
        override = self.reads.get(job_id)
        if isinstance(override, BaseException):
            raise override
        if override is not None:
            return override
        return next(job for job in self.jobs[run_id] if job.identifier == job_id)

    def require_authenticated_account(self, expected: str, *, authority: str) -> str:
        self.account_checks += 1
        if self.account != expected:
            raise PubanonError(f'authenticated account does not match {authority}')
        return expected


def _configs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[GlobalConfig, ProjectConfig]:
    _paths, global_config, project = configure(tmp_path, monkeypatch, tmp_path)
    assert project is not None
    return (
        replace(global_config, sensitive_terms=('term-a',)),
        replace(project, hygiene_terms=('style-term',)),
    )


@pytest.fixture
def configs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[GlobalConfig, ProjectConfig]:
    return _configs(tmp_path, monkeypatch)


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch) -> _Host:
    fake = _Host()
    for name in (
        'actions_jobs',
        'actions_runs',
        'delete_run_logs',
        'job_log',
        'read_actions_job',
        'require_authenticated_account',
    ):
        monkeypatch.setattr(_host, name, getattr(fake, name))
    return fake


def _job(
    identifier: int,
    *,
    run: int = _RUN,
    attempt: int = 1,
    name: str = 'lint',
    ran: bool = True,
) -> ActionsJob:
    runner = b'GitHub Actions 5\nGitHub Actions\nubuntu-latest' if ran else b''
    return ActionsJob(identifier, run, attempt, name, 5 if ran else None, runner)


def _run(
    identifier: int = _RUN,
    *,
    attempt: int = 1,
    status: str = 'completed',
    created: str = '2026-09-22T10:00:00Z',
    details: bytes = b'CI\nRelease',
) -> ActionsRun:
    return ActionsRun(identifier, attempt, status, 'CI', 'push', 'a' * 40, created, details)


def _log_review(host: _Host) -> _actions.ActionsReview:
    host.runs = [(_run(attempt=2), _run(_SECOND, created='2026-09-23T10:00:00Z'))]
    host.jobs = {_RUN: (_job(1), _job(2, attempt=2)), _SECOND: (_job(3, run=_SECOND),)}
    host.logs = {1: b'term-a\n', 2: b'ok\n', 3: b'ok\n'}
    return _actions.check(_COORDINATES, _ACCOUNT, _RULES)


def test_cancel_message_names_deleted_logs() -> None:
    assert _actions.cancel_message(()) == 'Canceled. Nothing was changed.'
    rendered = _actions.cancel_message((1,))
    assert 'Canceled' in rendered
    assert 'Deleted the logs of 1 run' in rendered
    assert 'nothing else was changed' in rendered
    rendered = _actions.cancel_message((1, 2))
    assert 'Canceled' in rendered
    assert 'Deleted the logs of 2 runs' in rendered
    assert 'nothing else was changed' in rendered
    assert _actions.deletion_note((1, 2)) == 'Deleted the logs of 2 runs.'


def test_check_accepts_missing_logs_for_jobs_that_never_started(host: _Host) -> None:
    host.runs = [(_run(),)]
    host.jobs = {_RUN: (_job(1, ran=False), _job(2, ran=False), _job(3, ran=False))}
    host.logs = {1: 'storage-404', 2: 'api-404', 3: b'style-term\n'}

    review = _actions.check(_COORDINATES, _ACCOUNT, _RULES)

    assert (review.read, review.gone) == (frozenset({3}), 0)
    assert [item.hit.term for item in review.findings] == ['style-term']
    assert _actions.row(review) == 'none; findings listed below'
    assert host.account_checks == 0


def test_check_counts_a_gone_log_after_a_matching_job_read_and_account_check(
    host: _Host,
) -> None:
    host.runs = [(_run(),)]
    host.jobs = {_RUN: (_job(1), _job(2))}
    host.logs = {1: b'ok\n', 2: 'api-404'}

    review = _actions.check(_COORDINATES, _ACCOUNT, _RULES)

    assert (review.read, review.gone) == (frozenset({1}), 1)
    assert _actions.row(review) == '1 checked, 1 no longer available; no sensitive terms'
    assert host.account_checks == 1


def test_check_matches_logs_details_and_runners_including_split_terms(host: _Host) -> None:
    host.runs = [(_run(details=b'CI\nterm-a release'),)]
    host.jobs = {_RUN: (ActionsJob(1, _RUN, 1, 'lint', 5, b'style-term-runner'),)}
    host.logs = {1: b'ok\nte\x1b[31mrm-a\x1b[0m\n'}

    review = _actions.check(_COORDINATES, _ACCOUNT, _RULES)

    assert [(item.location, item.job, item.hit.term) for item in review.findings] == [
        ('run details', None, 'term-a'),
        ('runner for job lint', None, 'style-term'),
        ('job lint', 1, 'term-a'),
    ]
    assert _actions.row(review) == '1 checked; findings listed below'


def test_check_matches_runner_details_of_jobs_that_never_started(host: _Host) -> None:
    host.runs = [(_run(),)]
    host.jobs = {_RUN: (ActionsJob(1, _RUN, 1, 'lint', None, b'style-term-runner'),)}
    host.logs = {1: 'storage-404'}

    review = _actions.check(_COORDINATES, _ACCOUNT, _RULES)

    assert [(item.location, item.job, item.hit.term) for item in review.findings] == [
        ('runner for job lint', None, 'style-term'),
    ]


def test_check_passes_its_command_to_every_actions_read(host: _Host) -> None:
    host.runs = [(_run(),)]
    host.jobs = {_RUN: (_job(1),)}
    host.logs = {1: b'ok\n'}

    _actions.check(_COORDINATES, _ACCOUNT, _RULES, command='pubanon audit --remote')

    assert host.commands
    assert set(host.commands) == {'pubanon audit --remote'}


def test_check_reads_every_log_and_reports_a_clean_row(
    host: _Host, capsys: pytest.CaptureFixture[str]
) -> None:
    host.runs = [(_run(),)]
    host.jobs = {_RUN: (_job(1), _job(2, name='test'))}
    host.logs = {1: b'lint ok\n', 2: b'tests ok\n'}

    review = _actions.check(_COORDINATES, _ACCOUNT, _RULES)

    assert review.findings == ()
    assert review.read == frozenset({1, 2})
    assert _actions.row(review) == '2 checked; no sensitive terms'
    assert capsys.readouterr().out == 'Checking Actions logs: 1 run, 2 jobs.\n'
    assert host.account_checks == 0


@pytest.mark.parametrize('failure', ['running', 'empty log', 'changed'])
def test_check_recovery_names_the_command_that_reruns_it(host: _Host, failure: str) -> None:
    host.runs = [(_run(status='in_progress' if failure == 'running' else 'completed'),)]
    if failure == 'changed':
        host.runs.append((_run(attempt=2),))
    host.jobs = {_RUN: (_job(1),)}
    host.logs = {1: b'' if failure == 'empty log' else b'ok\n'}

    with pytest.raises(PubanonError) as error:
        _actions.check(_COORDINATES, _ACCOUNT, _RULES, command='pubanon audit --remote')

    assert (error.value.recovery or '').endswith('rerun pubanon audit --remote')


@pytest.mark.parametrize(
    'sequence',
    [
        pytest.param(b'\x1b(B\x1b[m', id='xterm-reset'),
        pytest.param(b'\x1b$(B', id='multiple-intermediates'),
        pytest.param(b'\x1b[31m', id='csi'),
        pytest.param(b'\x1b]0;hidden\x07', id='osc-bel'),
        pytest.param(b'\x1b]0;hidden\x1b\\', id='osc-st'),
        pytest.param(b'\x1bPhidden\x1b\\', id='dcs'),
        pytest.param(b'\x1bXhidden\x1b\\', id='sos'),
        pytest.param(b'\x1b^hidden\x1b\\', id='pm'),
        pytest.param(b'\x1b_hidden\x1b\\', id='apc'),
    ],
)
def test_check_matches_terms_split_by_terminal_sequences(
    configs: tuple[GlobalConfig, ProjectConfig],
    host: _Host,
    capsys: pytest.CaptureFixture[str],
    sequence: bytes,
) -> None:
    host.runs = [(_run(),)]
    host.jobs = {_RUN: (_job(1),)}
    host.logs = {1: b'private log prefix\nte' + sequence + b'rm-a private log suffix\n'}

    with verbose_context(enabled=True), color_context('never'):
        review = _actions.check(_COORDINATES, _ACCOUNT, _RULES)
        summary = _actions.summary(review, configs, ())

    assert [(item.job, item.hit.term, item.lines) for item in review.findings] == [
        (1, 'term-a', (2,)),
    ]
    assert '      Log lines: 2' in summary
    rendered = '\n'.join(summary) + capsys.readouterr().out
    assert 'private log' not in rendered
    assert 'hidden' not in rendered
    assert '\x1b' not in rendered


@pytest.mark.parametrize('status', ['requested', 'queued', 'pending', 'waiting', 'in_progress'])
def test_check_refuses_unfinished_runs_before_reading_logs(host: _Host, status: str) -> None:
    host.runs = [(_run(status=status),)]
    host.jobs = {_RUN: (_job(1),)}

    with pytest.raises(PubanonError, match='workflow runs are still running') as error:
        _actions.check(_COORDINATES, _ACCOUNT, _RULES)

    assert error.value.recovery is not None
    assert 'wait for them to finish' in error.value.recovery
    assert 'pubanon repo reveal' in error.value.recovery
    assert host.log_calls == []


@pytest.mark.parametrize(
    'failure', ['read fails', 'different job', 'different runner', 'account changed']
)
def test_check_refuses_an_api_404_without_evidence_that_the_log_is_gone(
    host: _Host, failure: str
) -> None:
    host.runs = [(_run(),)]
    host.jobs = {_RUN: (_job(1),)}
    host.logs = {1: 'api-404'}
    if failure == 'read fails':
        host.reads[1] = PubanonError('invalid workflow job response')
    elif failure == 'different job':
        host.reads[1] = _job(1, attempt=2)
    elif failure == 'different runner':
        host.reads[1] = _job(1)._replace(runner_id=6)
    else:
        host.account = 'other-account'

    with pytest.raises(PubanonError) as error:
        _actions.check(_COORDINATES, _ACCOUNT, _RULES)

    if failure == 'account changed':
        assert str(error.value) == 'authenticated account does not match configuration'
    else:
        assert str(error.value) == 'cannot check Actions logs'
        assert error.value.fact == 'job 1: GitHub did not confirm that its log is gone'


def test_check_reports_none_without_jobs(host: _Host, capsys: pytest.CaptureFixture[str]) -> None:
    review = _actions.check(_COORDINATES, _ACCOUNT, _RULES)

    assert _actions.row(review) == 'none'
    assert capsys.readouterr().out == ''
    assert host.log_calls == []


@pytest.mark.parametrize(
    ('log', 'reason'),
    [(b'', 'GitHub returned an empty log'), ('storage-404', 'its log is not available yet')],
)
def test_check_requires_a_log_from_every_job_that_ran(
    host: _Host, log: bytes | LogAbsence, reason: str
) -> None:
    host.runs = [(_run(),)]
    host.jobs = {_RUN: (_job(1),)}
    host.logs = {1: log}

    with pytest.raises(PubanonError, match='cannot check Actions logs') as error:
        _actions.check(_COORDINATES, _ACCOUNT, _RULES)

    assert error.value.fact == f'job 1: {reason}'
    assert error.value.recovery == 'rerun pubanon repo reveal'


def test_check_stops_when_runs_change_while_reading(host: _Host) -> None:
    host.runs = [(_run(),), (_run(), _run(40000000002))]
    host.jobs = {_RUN: (_job(1),)}
    host.logs = {1: b'ok\n'}

    with pytest.raises(PubanonError, match='Actions runs changed during the check') as error:
        _actions.check(_COORDINATES, _ACCOUNT, _RULES)

    assert error.value.recovery == 'rerun pubanon repo reveal'


def test_check_tracks_lines_of_overlapping_stripped_matches(host: _Host) -> None:
    host.runs = [(_run(),)]
    host.jobs = {_RUN: (_job(1),)}
    host.logs = {1: b'aa\x1bPignored\n\x1b\\a\x1bPignored\n\x1b\\a\n'}

    with verbose_context(enabled=True):
        review = _actions.check(_COORDINATES, _ACCOUNT, {RuleClass.IDENTITY_CRITICAL: ('aaa',)})

    assert [(item.hit.term, item.lines) for item in review.findings] == [('aaa', (1, 2, 3))]


def test_check_tracks_original_lines_across_multiline_control_strings(
    configs: tuple[GlobalConfig, ProjectConfig],
    host: _Host,
    capsys: pytest.CaptureFixture[str],
) -> None:
    host.runs = [(_run(),)]
    host.jobs = {_RUN: (_job(1),)}
    host.logs = {
        1: (
            b'private log prefix\n'
            b'before\x1bPignored\n'
            b'ignored\x1b\\term-a\n'
            b'te\x1b]ignored\n'
            b'ignored\n'
            b'style-term\x1b\\rm-a\n'
            b'term-a\n'
        )
    }

    with verbose_context(enabled=True), color_context('never'):
        review = _actions.check(_COORDINATES, _ACCOUNT, _RULES)
        summary = _actions.summary(review, configs, ())

    assert [(item.hit.term, item.lines) for item in review.findings] == [
        ('term-a', (3, 4, 6, 7)),
        ('style-term', (6,)),
    ]
    assert '      Log lines: 3, 4, 6, 7' in summary
    assert '      Log lines: 6' in summary
    rendered = '\n'.join(summary) + capsys.readouterr().out
    assert 'private log' not in rendered
    assert 'ignored' not in rendered
    assert '\x1b' not in rendered


def test_clean_sentence_counts_checked_and_unavailable_logs(host: _Host) -> None:
    assert (
        _actions.clean_sentence(_actions.check(_COORDINATES, _ACCOUNT, _RULES))
        == 'No Actions runs to check.'
    )
    host.runs = [(_run(),)]
    host.jobs = {_RUN: (_job(1), _job(2), _job(3, ran=False))}
    host.logs = {1: b'ok\n', 2: 'api-404', 3: 'storage-404'}

    review = _actions.check(_COORDINATES, _ACCOUNT, _RULES)

    rendered = _actions.clean_sentence(review)
    assert 'No configured sensitive terms were found' in rendered
    assert 'Actions logs' in rendered
    assert re.search(r'\b1 checked\b', rendered)
    assert re.search(r'\b1 no longer available\b', rendered)


def test_decide_cancel_changes_nothing(
    configs: tuple[GlobalConfig, ProjectConfig], host: _Host, answers: _Script
) -> None:
    review = _log_review(host)
    answers([(_MENU, 'c')])

    with pytest.raises(OperationCanceled, match=r'^Canceled\. Nothing was changed\.$'):
        _actions.decide(review, configs, (), _ACCOUNT, already_public=False)

    assert host.deleted == []


def test_decide_fails_when_the_terminal_cannot_answer(
    configs: tuple[GlobalConfig, ProjectConfig], host: _Host, monkeypatch: pytest.MonkeyPatch
) -> None:
    review = _log_review(host)

    def _ended(_prompt: object = '') -> str:
        raise EOFError

    monkeypatch.setattr('builtins.input', _ended)

    with pytest.raises(PubanonError) as error:
        _actions.decide(review, configs, (), _ACCOUNT, already_public=False)

    assert not isinstance(error.value, OperationCanceled)
    assert host.deleted == []


def test_decide_offers_keep_or_cancel_when_only_details_match(
    configs: tuple[GlobalConfig, ProjectConfig],
    host: _Host,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    host.runs = [(_run(details=b'CI\nterm-a'),)]
    host.jobs = {_RUN: (_job(1),)}
    host.logs = {1: b'ok\n'}
    review = _actions.check(_COORDINATES, _ACCOUNT, _RULES)
    answers([(_DETAILS_MENU, 'k')])

    assert _actions.decide(review, configs, (), _ACCOUNT, already_public=False) == ()
    out = capsys.readouterr().out
    assert 'public publishes these run details' in out
    assert 'Deleting logs cannot remove them' in out
    assert 'deleting a run on GitHub' in out


def test_decide_says_details_remain_when_deleting_logs(
    configs: tuple[GlobalConfig, ProjectConfig],
    host: _Host,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    host.runs = [(_run(details=b'CI\nterm-a'),)]
    host.jobs = {_RUN: (_job(1),)}
    host.logs = {1: b'term-a\n'}
    review = _actions.check(_COORDINATES, _ACCOUNT, _RULES)
    answers(
        [
            (_MENU, 'd'),
            (f'Delete the logs of 1 run ({_RUN}, CI)? This cannot be undone. [y/n] ', 'y'),
        ]
    )

    _actions.decide(review, configs, (), _ACCOUNT, already_public=False)

    rendered = capsys.readouterr().out
    assert 'Run and runner details stay' in rendered
    assert 'deleting logs cannot remove them' in rendered


def test_decide_without_findings_asks_nothing(
    configs: tuple[GlobalConfig, ProjectConfig], host: _Host
) -> None:
    review = _actions.check(_COORDINATES, _ACCOUNT, _RULES)

    assert _actions.decide(review, configs, (), _ACCOUNT, already_public=False) == ()
    assert host.deleted == []


def test_offer_log_deletion_asks_nothing_when_only_details_match(
    configs: tuple[GlobalConfig, ProjectConfig],
    host: _Host,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    host.runs = [(_run(details=b'CI\nterm-a'),)]
    host.jobs = {_RUN: (_job(1),)}
    host.logs = {1: b'ok\n'}
    review = _actions.check(_COORDINATES, _ACCOUNT, _RULES)
    answers([])

    assert _actions.offer_log_deletion(review, configs, (), _ACCOUNT, already_public=True) == ()
    assert 'These run details are already public.' in capsys.readouterr().out


def test_offer_log_deletion_deletes_after_confirmation(
    configs: tuple[GlobalConfig, ProjectConfig],
    host: _Host,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    review = _log_review(host)
    answers(
        [
            (_AUDIT_MENU, 'd'),
            (f'Delete the logs of 1 run ({_RUN}, CI)? This cannot be undone. [y/n] ', 'y'),
        ]
    )

    result = _actions.offer_log_deletion(review, configs, (), _ACCOUNT, already_public=True)

    assert result == (_RUN,)
    assert host.deleted == [_RUN]
    out = capsys.readouterr().out
    assert 'These logs are already public.' in out
    assert 'Deleted the logs of 1 run.' in out


@pytest.mark.parametrize('answer', ['keep', 'decline'])
def test_offer_log_deletion_keeps_logs_on_keep_or_a_declined_confirmation(
    configs: tuple[GlobalConfig, ProjectConfig], host: _Host, answers: _Script, answer: str
) -> None:
    review = _log_review(host)
    if answer == 'keep':
        answers([(_AUDIT_MENU, 'k')])
    else:
        answers(
            [
                (_AUDIT_MENU, 'd'),
                (f'Delete the logs of 1 run ({_RUN}, CI)? This cannot be undone. [y/n] ', 'n'),
            ]
        )

    result = _actions.offer_log_deletion(review, configs, (), _ACCOUNT, already_public=True)

    assert result == ()
    assert host.deleted == []


def test_offer_log_deletion_reports_an_unfinished_deletion_with_its_command(
    configs: tuple[GlobalConfig, ProjectConfig], host: _Host, answers: _Script
) -> None:
    host.runs = [(_run(),)]
    host.jobs = {_RUN: (_job(1),)}
    host.logs = {1: b'term-a\n'}
    host.delete_errors = {_RUN: PubanonError('HTTP 500')}
    review = _actions.check(_COORDINATES, _ACCOUNT, _RULES, command='pubanon audit --remote')
    answers(
        [
            (_AUDIT_MENU, 'd'),
            (f'Delete the logs of 1 run ({_RUN}, CI)? This cannot be undone. [y/n] ', 'y'),
        ]
    )

    with pytest.raises(PubanonError, match='deleting Actions logs did not finish') as error:
        _actions.offer_log_deletion(review, configs, (), _ACCOUNT, already_public=True)

    assert error.value.recovery == 'rerun pubanon audit --remote'


def test_offer_log_deletion_warns_when_the_terminal_cannot_answer(
    configs: tuple[GlobalConfig, ProjectConfig],
    host: _Host,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    review = _log_review(host)

    def _ended(_prompt: object = '') -> str:
        raise EOFError

    monkeypatch.setattr('builtins.input', _ended)

    with color_context('never'):
        result = _actions.offer_log_deletion(review, configs, (), _ACCOUNT, already_public=True)

    assert result == ()
    assert host.deleted == []
    rendered = capsys.readouterr().out
    assert 'warning: could not ask about deleting Actions logs' in rendered
    assert 'nothing was deleted' in rendered


@pytest.mark.parametrize('change', ['attempt', 'job'])
def test_require_unchanged_detects_new_attempts_and_jobs(host: _Host, change: str) -> None:
    host.runs = [(_run(),)]
    host.jobs = {_RUN: (_job(1),)}
    host.logs = {1: b'ok\n'}
    review = _actions.check(_COORDINATES, _ACCOUNT, _RULES)
    if change == 'attempt':
        host.runs = [(_run(attempt=2),)]
    else:
        host.jobs = {_RUN: (_job(1), _job(2))}

    with pytest.raises(PubanonError, match='Actions runs changed during the check') as error:
        _actions.require_unchanged(review)

    assert error.value.recovery == 'rerun pubanon repo reveal'


def test_show_unchecked_lists_run_links_only_in_verbose_mode(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with color_context('never'), verbose_context(enabled=True):
        _actions.show_unchecked(_COORDINATES, (7,), artifacts=0, caches=0)

    assert '  Run 7: https://github.com/anonymous-account/public-repo/actions/runs/7' in (
        capsys.readouterr().out.splitlines()
    )


@pytest.mark.parametrize(
    ('runs', 'artifacts', 'caches', 'statement'),
    [
        (
            (1,),
            6,
            2,
            (
                'the contents of 6 artifacts and 2 Actions caches, job summaries and annotations, '
                'and earlier pushes on the Activity page.'
            ),
        ),
        (
            (1,),
            1,
            0,
            (
                'the contents of 1 artifact, job summaries and annotations, and earlier pushes on '
                'the Activity page.'
            ),
        ),
        ((1,), 0, 0, 'job summaries and annotations, and earlier pushes on the Activity page.'),
        ((), 2, 0, 'the contents of 2 artifacts and earlier pushes on the Activity page.'),
        ((), 0, 0, 'earlier pushes on the Activity page.'),
    ],
)
def test_show_unchecked_names_each_part_by_its_count(
    capsys: pytest.CaptureFixture[str],
    runs: tuple[int, ...],
    artifacts: int,
    caches: int,
    statement: str,
) -> None:
    with color_context('never'):
        _actions.show_unchecked(_COORDINATES, runs, artifacts=artifacts, caches=caches)

    assert capsys.readouterr().out.splitlines() == [
        f'Not checked: {statement}',
        '  Actions:  https://github.com/anonymous-account/public-repo/actions',
        '  Activity: https://github.com/anonymous-account/public-repo/activity',
    ]


@pytest.mark.parametrize('already_public', [False, True])
def test_decide_keep_returns_no_deletions(
    configs: tuple[GlobalConfig, ProjectConfig],
    host: _Host,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
    already_public: bool,
) -> None:
    review = _log_review(host)
    answers([(_MENU, 'k')])

    result = _actions.decide(review, configs, (), _ACCOUNT, already_public=already_public)

    assert result == ()
    assert host.deleted == []
    out = capsys.readouterr().out
    if already_public:
        assert 'These logs are already public' in out
        assert 'Deleting them stops GitHub from serving them' in out
        assert 'copies made meanwhile remain' in out
    else:
        assert 'public publishes these logs' in out
        assert "Deleting a run's logs" in out
        assert 'every job in that run' in out
        assert 'run and its results stay' in out


@pytest.mark.parametrize('confirmed', [False, True])
def test_decide_delete_removes_and_verifies_logs_of_every_attempt(
    configs: tuple[GlobalConfig, ProjectConfig],
    host: _Host,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
    confirmed: bool,
) -> None:
    review = _log_review(host)
    answers(
        [
            (_MENU, 'd'),
            (
                f'Delete the logs of 1 run ({_RUN}, CI)? This cannot be undone. [y/n] ',
                'y' if confirmed else 'n',
            ),
        ]
    )
    if not confirmed:
        with pytest.raises(OperationCanceled, match=r'^Canceled\. Nothing was changed\.$'):
            _actions.decide(review, configs, (), _ACCOUNT, already_public=False)
        assert host.deleted == []
        return

    assert _actions.decide(review, configs, (), _ACCOUNT, already_public=False) == (_RUN,)
    assert host.deleted == [_RUN]
    assert host.log_calls[-2:] == [1, 2]
    assert host.account_checks == 2
    assert 'Deleted the logs of 1 run.' in capsys.readouterr().out


@pytest.mark.parametrize(
    'scenario', [('delete', False), ('delete', True), ('read', False), ('read', True)]
)
def test_delete_preserves_safe_status_facts_without_log_text(
    configs: tuple[GlobalConfig, ProjectConfig],
    host: _Host,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
    scenario: tuple[str, bool],
) -> None:
    failure, verbose = scenario
    private_text = 'private log content must stay hidden'
    host.runs = [(_run(),)]
    host.jobs = {_RUN: (_job(1),)}
    host.logs = {1: f'term-a\n{private_text}\n'.encode()}
    with verbose_context(enabled=verbose):
        review = _actions.check(_COORDINATES, _ACCOUNT, _RULES)
    if failure == 'delete':
        message = 'GitHub did not confirm deleting the logs'
        detail = f'run {_RUN}: HTTP 403'
        host.delete_errors[_RUN] = PubanonError(message, fact=detail)
    else:
        message = 'cannot check Actions logs'
        detail = 'job 1: HTTP 503'
        host.log_errors[1] = PubanonError(message, fact=detail)
    answers(
        [
            (_MENU, 'd'),
            (f'Delete the logs of 1 run ({_RUN}, CI)? This cannot be undone. [y/n] ', 'y'),
        ]
    )

    with pytest.raises(PubanonError) as error, verbose_context(enabled=verbose):
        _actions.decide(review, configs, (), _ACCOUNT, already_public=False)

    assert error.value.fact == f'{message}\n{detail}\nOutcome unknown: run {_RUN}'
    assert private_text not in error.value.render()
    assert private_text not in capsys.readouterr().out


@pytest.mark.parametrize(
    ('failure', 'expected'),
    [
        (
            'request failed',
            (
                'deleting Actions logs did not finish',
                (
                    'GitHub did not confirm deleting the logs\n'
                    f'Outcome unknown: run {_RUN}, run {_SECOND}\nNot reached: run {_THIRD}'
                ),
            ),
        ),
        (
            'still served',
            (
                'deleting Actions logs did not finish',
                (
                    f'a deleted log is still available\njob 2\nDeleted: run {_RUN}\n'
                    f'Outcome unknown: run {_SECOND}, run {_THIRD}'
                ),
            ),
        ),
        (
            'interrupted',
            (
                'Canceled while deleting Actions logs.',
                f'Outcome unknown: run {_RUN}, run {_SECOND}\nNot reached: run {_THIRD}',
            ),
        ),
        (
            'account changed',
            (
                'deleting Actions logs did not finish',
                (
                    'authenticated account does not match configuration\n'
                    f'Not reached: run {_RUN}, run {_SECOND}, run {_THIRD}'
                ),
            ),
        ),
        (
            'account switched',
            (
                'deleting Actions logs did not finish',
                (
                    'authenticated account does not match configuration\n'
                    f'Outcome unknown: run {_RUN}\nNot reached: run {_SECOND}, run {_THIRD}'
                ),
            ),
        ),
    ],
)
def test_delete_reports_confirmed_unknown_and_unreached_runs(
    configs: tuple[GlobalConfig, ProjectConfig],
    host: _Host,
    answers: _Script,
    failure: str,
    expected: tuple[str, str],
) -> None:
    host.runs = [
        (
            _run(),
            _run(_SECOND, created='2026-09-23T10:00:00Z'),
            _run(_THIRD, created='2026-09-24T10:00:00Z'),
        )
    ]
    host.jobs = {
        _RUN: (_job(1),),
        _SECOND: (_job(2, run=_SECOND),),
        _THIRD: (_job(3, run=_THIRD),),
    }
    host.logs = {1: b'term-a\n', 2: b'term-a\n', 3: b'term-a\n'}
    review = _actions.check(_COORDINATES, _ACCOUNT, _RULES)
    if failure == 'request failed':
        host.delete_errors[_SECOND] = PubanonError('GitHub did not confirm deleting the logs')
    elif failure == 'still served':
        host.kept.add(_SECOND)
    elif failure == 'interrupted':
        host.delete_errors[_SECOND] = KeyboardInterrupt()
    elif failure == 'account switched':
        host.switch_after_delete = True
    else:
        host.account = 'other-account'
    answers(
        [
            (_MENU, 'd'),
            (
                (
                    f'Delete the logs of 3 runs ({_RUN}, CI; {_SECOND}, CI; {_THIRD}, CI)? '
                    'This cannot be undone. [y/n] '
                ),
                'y',
            ),
        ]
    )

    with pytest.raises(PubanonError) as error:
        _actions.decide(review, configs, (), _ACCOUNT, already_public=False)

    assert isinstance(error.value, OperationCanceled) is (failure == 'interrupted')
    assert (str(error.value), error.value.fact) == expected
    assert error.value.recovery == 'rerun pubanon repo reveal'


def test_summary_escapes_names_from_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: _Host
) -> None:
    configs = _configs(tmp_path, monkeypatch)
    workflow, event, job = 'CI\x1b[2J', 'push\x07', 'lint\x1b]0;x\x07'
    host.runs = [
        (ActionsRun(_RUN, 1, 'completed', workflow, event, 'a' * 40, '2026-09-22T10:00:00Z', b''),)
    ]
    host.jobs = {_RUN: (ActionsJob(1, _RUN, 1, job, 5, b''),)}
    host.logs = {1: b'term-a\n'}

    with color_context('never'):
        lines = _actions.summary(_actions.check(_COORDINATES, _ACCOUNT, _RULES), configs, ())

    assert not any('\x1b' in line or '\x07' in line for line in lines)
    heading = (
        f'  Run {_RUN}, {escape(workflow)} on {escape(event)}, commit aaaaaaaaaaaa, 2026-09-22:'
    )
    assert heading in lines
    assert f'    job {escape(job)}  term-a' in lines


def test_summary_groups_by_run_oldest_first_and_labels_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: _Host
) -> None:
    configs = _configs(tmp_path, monkeypatch)
    older = _run(attempt=2, created='2026-09-21T10:00:00Z')
    newer = _run(40000000002, details=b'CI\nterm-a')
    host.runs = [(newer, older)]
    host.jobs = {
        older.identifier: (_job(1), _job(2, attempt=2)),
        newer.identifier: (_job(3, run=newer.identifier),),
    }
    host.logs = {1: b'term-a\n', 2: b'term-a\nstyle-term\n', 3: b'ok\n'}
    review = _actions.check(_COORDINATES, _ACCOUNT, _RULES)

    with color_context('never'):
        lines = _actions.summary(review, configs, ())

    assert lines == [
        'Sensitive terms in Actions logs: 1 term in 3 locations',
        f'  Run {_RUN}, CI on push, commit aaaaaaaaaaaa, 2026-09-21:',
        '    job lint (attempt 1)  term-a',
        '    job lint (attempt 2)  term-a',
        '  Run 40000000002, CI on push, commit aaaaaaaaaaaa, 2026-09-22:',
        '    run details           term-a',
        '  Rules: global sensitive_terms entry 1',
        'Policy terms in Actions logs: 1 term in 1 location',
        f'  Run {_RUN}, CI on push, commit aaaaaaaaaaaa, 2026-09-21:',
        '    job lint (attempt 2)  style-term',
        '  Rules: project hygiene_terms entry 1',
        '  pubanon repo reveal --verbose links each row to its job and log lines.',
    ]


def test_summary_hint_names_the_command_that_reruns_the_check(
    configs: tuple[GlobalConfig, ProjectConfig], host: _Host
) -> None:
    host.runs = [(_run(),)]
    host.jobs = {_RUN: (_job(1),)}
    host.logs = {1: b'term-a\n'}
    review = _actions.check(_COORDINATES, _ACCOUNT, _RULES, command='pubanon audit --remote')

    with color_context('never'):
        lines = _actions.summary(review, configs, ())

    assert 'pubanon audit --remote --verbose' in lines[-1]


def test_summary_in_verbose_mode_links_jobs_lists_lines_and_shows_every_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, host: _Host
) -> None:
    configs = _configs(tmp_path, monkeypatch)
    host.runs = [(_run(),)]
    host.jobs = {_RUN: tuple(_job(index, name=f'job-{index:02}') for index in range(1, 23))}
    host.logs = dict.fromkeys(range(1, 23), b'ok\nterm-a\nte\x1b[1mrm-a\n')

    with verbose_context(enabled=True), color_context('never'):
        lines = _actions.summary(_actions.check(_COORDINATES, _ACCOUNT, _RULES), configs, ())

    index = lines.index('    job job-01  term-a')
    assert lines[index + 1 : index + 3] == [
        f'      https://github.com/anonymous-account/public-repo/actions/runs/{_RUN}/job/1',
        '      Log lines: 2, 3',
    ]
    assert '    job job-22  term-a' in lines
    assert not any(line.startswith('  and ') for line in lines)
    assert not any('--verbose' in line for line in lines)
