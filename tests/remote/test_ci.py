import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from pubanon._errors import ObservationTimeoutError, OperationCanceled, PubanonError
from pubanon._remote import _ci
from pubanon._remote._host import CheckVerdict, WorkflowRun
from tests._git import git_env

_COMMIT = 'a' * 40


def _bare_snapshot(
    tmp_path: Path, make_repo: Callable[..., Path], workflow: str, *, name: str
) -> tuple[Path, str]:
    source = make_repo(name, files={'.github/workflows/ci.yml': workflow})
    store = tmp_path / f'{name}.git'
    subprocess.run(['git', 'clone', '--bare', '-q', source, store], check=True, env=git_env())
    result = subprocess.run(
        ['git', '-C', source, 'rev-parse', 'HEAD'],
        check=True,
        capture_output=True,
        text=True,
        env=git_env(),
    )
    return store, result.stdout.strip()


def _dispatch() -> dict[str, object]:
    return {
        'args': [
            'api',
            '--method',
            'POST',
            '--header',
            'X-GitHub-Api-Version: 2026-03-10',
            'repos/owner/repo/actions/workflows/ci.yml/dispatches',
            '-f',
            'ref=main',
            '-F',
            'inputs[hosted]=true',
            '-F',
            'inputs[full_tests]=false',
        ],
        'stdout': json.dumps({'workflow_run_id': 9}),
    }


def _display_run(*, conclusion: str = 'success') -> WorkflowRun:
    return WorkflowRun(
        9,
        9,
        1,
        '.github/workflows/ci.yml',
        'workflow_dispatch',
        'main',
        _COMMIT,
        'completed',
        conclusion,
        'CI\x1b[31m / hosted',
        'https://example.invalid/\nrun',
    )


def _evidence(required: tuple[str, ...] = ('quality',)) -> _ci.RunEvidence:
    return _ci.run_evidence(
        ('owner', 'repo'), _ci.RunQuery('ci.yml', _COMMIT, 'workflow_dispatch'), required=required
    )


def _jobs(number: int, *results: tuple[str, str | None], attempt: int = 1) -> dict[str, object]:
    jobs = [
        {
            'run_id': number,
            'run_attempt': attempt,
            'name': name,
            'status': 'completed',
            'conclusion': conclusion,
        }
        for name, conclusion in results
    ]
    return {
        'args': [
            'api',
            f'repos/owner/repo/actions/runs/{number}/attempts/{attempt}/jobs?per_page=100&page=1',
        ],
        'stdout': json.dumps({'total_count': len(jobs), 'jobs': jobs}),
    }


def _listing(
    runs: list[dict[str, object]],
    *,
    page: int = 1,
    total: int | None = None,
    event: str = 'workflow_dispatch',
) -> dict[str, object]:
    return {
        'args': [
            'api',
            (
                f'repos/owner/repo/actions/workflows/ci.yml/runs?head_sha={_COMMIT}'
                f'&event={event}&branch=main&per_page=100&page={page}'
            ),
        ],
        'stdout': json.dumps(
            {'total_count': len(runs) if total is None else total, 'workflow_runs': runs}
        ),
        'once': True,
    }


def _read_run(run: dict[str, object]) -> dict[str, object]:
    return {'args': ['api', 'repos/owner/repo/actions/runs/9'], 'stdout': json.dumps(run)}


def _recent(*runs: dict[str, object]) -> dict[str, object]:
    return {
        'args': [
            'api',
            (
                'repos/owner/repo/actions/workflows/ci.yml/runs'
                '?event=workflow_dispatch&branch=main&per_page=100'
            ),
        ],
        'stdout': json.dumps({'total_count': len(runs), 'workflow_runs': list(runs)}),
    }


def _registered() -> dict[str, object]:
    return {
        'args': ['api', 'repos/owner/repo/actions/workflows/ci.yml'],
        'stdout': json.dumps({'path': '.github/workflows/ci.yml', 'state': 'active'}),
    }


def _run(
    number: int,
    *,
    status: str = 'completed',
    conclusion: str | None = 'success',
    attempt: int = 1,
) -> dict[str, object]:
    return {
        'id': number,
        'run_number': number,
        'run_attempt': attempt,
        'path': '.github/workflows/ci.yml',
        'event': 'workflow_dispatch',
        'head_branch': 'main',
        'head_sha': _COMMIT,
        'status': status,
        'conclusion': conclusion,
        'display_title': 'CI / hosted',
        'html_url': f'https://example.invalid/runs/{number}',
    }


def _script(path: Path, *responses: dict[str, object]) -> None:
    path.write_text(json.dumps(responses))


def _variable(value: str) -> dict[str, object]:
    return {
        'args': ['api', 'repos/owner/repo/actions/variables/CI_RUN_ON_PUSH'],
        'stdout': json.dumps({'name': 'CI_RUN_ON_PUSH', 'value': value}),
    }


def test_disappearing_latest_run_is_pending(fake_gh: Path) -> None:
    _script(fake_gh, _listing([_run(1)]), _jobs(1, ('quality', 'success')), _listing([]))
    assert _evidence().state == 'pending'


def test_ensure_run_on_push_variable_creates_and_verifies(fake_gh: Path, tmp_path: Path) -> None:
    plan = _ci.StageCiPlan(
        workflow='ci.yml',
        hosted=False,
        full_tests=False,
        create_variable=True,
        cancels=(),
        replaces=None,
    )
    _script(
        fake_gh,
        {
            'args': [
                'api',
                '--method',
                'POST',
                'repos/owner/repo/actions/variables',
                '-f',
                'name=CI_RUN_ON_PUSH',
                '-f',
                'value=false',
            ],
        },
        _variable('FALSE'),
    )
    _ci.ensure_run_on_push_variable(('owner', 'repo'), plan)
    assert len((tmp_path / 'fake-gh-calls.jsonl').read_text().splitlines()) == 2

    _script(
        fake_gh,
        {
            'args': [
                'api',
                '--method',
                'POST',
                'repos/owner/repo/actions/variables',
                '-f',
                'name=CI_RUN_ON_PUSH',
                '-f',
                'value=false',
            ],
        },
        _variable('true'),
    )
    with pytest.raises(PubanonError, match='CI_RUN_ON_PUSH') as error:
        _ci.ensure_run_on_push_variable(('owner', 'repo'), plan)
    assert error.value.fact is not None
    assert 'created' in error.value.fact
    assert 'Nothing was pushed' in error.value.fact
    assert error.value.recovery is not None
    assert len((tmp_path / 'fake-gh-calls.jsonl').read_text().splitlines()) == 4


def test_ensure_run_on_push_variable_is_noop_when_false_is_present(
    tmp_path: Path,
) -> None:
    plan = _ci.StageCiPlan(
        workflow='ci.yml',
        hosted=False,
        full_tests=False,
        create_variable=False,
        cancels=(),
        replaces=None,
    )
    _ci.ensure_run_on_push_variable(('owner', 'repo'), plan)
    assert not (tmp_path / 'fake-gh-calls.jsonl').exists()


def test_ensure_run_on_push_variable_reports_created_when_readback_fails(
    fake_gh: Path, tmp_path: Path
) -> None:
    plan = _ci.StageCiPlan(
        workflow='ci.yml',
        hosted=False,
        full_tests=False,
        create_variable=True,
        cancels=(),
        replaces=None,
    )
    _script(
        fake_gh,
        {
            'args': [
                'api',
                '--method',
                'POST',
                'repos/owner/repo/actions/variables',
                '-f',
                'name=CI_RUN_ON_PUSH',
                '-f',
                'value=false',
            ],
        },
        {
            'args': ['api', 'repos/owner/repo/actions/variables/CI_RUN_ON_PUSH'],
            'returncode': 1,
            'stderr': 'HTTP 403',
        },
    )
    with pytest.raises(PubanonError, match='CI_RUN_ON_PUSH') as error:
        _ci.ensure_run_on_push_variable(('owner', 'repo'), plan)
    assert error.value.fact is not None
    assert 'created' in error.value.fact
    assert 'Nothing was pushed' in error.value.fact
    assert 'GitHub denied access to Actions variables' in error.value.fact
    assert error.value.recovery is not None
    assert 'pubanon stage push' in error.value.recovery
    assert len((tmp_path / 'fake-gh-calls.jsonl').read_text().splitlines()) == 2


def test_ensure_run_on_push_variable_reports_created_when_readback_is_canceled(
    fake_gh: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan = _ci.StageCiPlan(
        workflow='ci.yml',
        hosted=False,
        full_tests=False,
        create_variable=True,
        cancels=(),
        replaces=None,
    )
    _script(
        fake_gh,
        {
            'args': [
                'api',
                '--method',
                'POST',
                'repos/owner/repo/actions/variables',
                '-f',
                'name=CI_RUN_ON_PUSH',
                '-f',
                'value=false',
            ],
        },
    )

    def canceled(_coordinates: tuple[str, str], _name: str) -> str | None:
        raise OperationCanceled('Canceled.')

    monkeypatch.setattr(_ci, 'repository_variable', canceled)
    with pytest.raises(OperationCanceled, match='Canceled after creating CI_RUN_ON_PUSH') as error:
        _ci.ensure_run_on_push_variable(('owner', 'repo'), plan)
    assert error.value.fact == 'The variable was created. Nothing was pushed.'
    assert len((tmp_path / 'fake-gh-calls.jsonl').read_text().splitlines()) == 1


def test_evidence_facts_names_run_and_failing_checks() -> None:
    evidence = _ci.RunEvidence(
        'unacceptable',
        _display_run(),
        (
            CheckVerdict('quality', 'success', 'success'),
            CheckVerdict('hosted\nvalidation', 'unacceptable', 'skipped'),
            CheckVerdict('custom', 'unacceptable', 'bad\x1bresult'),
        ),
        'unused detail',
    )
    assert _ci.evidence_facts(evidence) == (
        r'#9 CI\x1b[31m / hosted (https://example.invalid/\x0arun); '
        r'hosted\x0avalidation: failed (skipped), custom: failed (bad\x1bresult)'
    )
    assert _ci.evidence_facts(evidence._replace(checks=(), detail='run\ncancelled')).endswith(
        r'; run\x0acancelled'
    )
    assert _ci.evidence_facts(evidence._replace(checks=(), detail='')).endswith('run)')
    assert _ci.evidence_facts(evidence._replace(run=None)) == 'no requested run'


def test_evidence_is_missing_without_runs(fake_gh: Path) -> None:
    _script(fake_gh, _listing([]))
    evidence = _evidence()
    assert evidence.state == 'missing'
    assert evidence.run is None


def test_evidence_succeeds_when_required_jobs_pass(fake_gh: Path) -> None:
    runs = [_run(1)]
    _script(
        fake_gh,
        _listing(runs),
        _jobs(1, ('package', 'success'), ('quality', 'success'), ('optional', 'failure')),
        _listing(runs),
    )
    evidence = _evidence(('quality', 'package'))
    assert evidence.state == 'success'
    assert [(check.name, check.state) for check in evidence.checks] == [
        ('quality', 'success'),
        ('package', 'success'),
    ]


def test_final_repository_evidence_queries_push_runs_on_main(fake_gh: Path) -> None:
    runs = [_run(1) | {'event': 'push'}]
    _script(
        fake_gh,
        _listing(runs, event='push'),
        _jobs(1, ('quality', 'success')),
        _listing(runs, event='push'),
    )
    evidence = _ci.run_evidence(
        ('owner', 'repo'), _ci.RunQuery('ci.yml', _COMMIT, 'push'), required=('quality',)
    )
    assert evidence.state == 'success'


def test_newer_run_between_listings_is_pending(fake_gh: Path) -> None:
    _script(
        fake_gh,
        _listing([_run(1)]),
        _jobs(1, ('quality', 'success')),
        _listing([_run(1), _run(2)]),
    )
    evidence = _evidence()
    assert evidence.state == 'pending'
    assert evidence.detail == 'a newer run or attempt appeared'


def test_plan_stage_ci_accepts_variable_in_any_case(fake_gh: Path) -> None:
    _script(fake_gh, _variable('FALSE'), _recent())
    plan = _ci.plan_stage_ci(('owner', 'repo'), 'ci.yml', _COMMIT, hosted=False, full_tests=True)
    assert plan.create_variable is False


def test_plan_stage_ci_previews_missing_variable(fake_gh: Path) -> None:
    _script(
        fake_gh,
        {
            'args': ['api', 'repos/owner/repo/actions/variables/CI_RUN_ON_PUSH'],
            'returncode': 1,
            'stderr': 'HTTP 404',
        },
        _recent(),
    )
    plan = _ci.plan_stage_ci(('owner', 'repo'), 'ci.yml', _COMMIT, hosted=False, full_tests=False)
    assert plan.create_variable is True
    assert plan.cancels == ()
    assert plan.replaces is None


def test_plan_stage_ci_refuses_conflicting_variable(fake_gh: Path, tmp_path: Path) -> None:
    _script(fake_gh, _variable('true\nunsafe'))
    with pytest.raises(PubanonError, match='unexpected value') as error:
        _ci.plan_stage_ci(('owner', 'repo'), 'ci.yml', _COMMIT, hosted=False, full_tests=False)
    assert error.value.fact == r'true\x0aunsafe'
    assert len((tmp_path / 'fake-gh-calls.jsonl').read_text().splitlines()) == 1


def test_plan_stage_ci_replaces_only_latest_success_for_commit(fake_gh: Path) -> None:
    runs = (_run(6), _run(8, conclusion='failure'), _run(9))
    _script(fake_gh, _variable('false'), _recent(*runs))
    plan = _ci.plan_stage_ci(('owner', 'repo'), 'ci.yml', _COMMIT, hosted=False, full_tests=False)
    assert plan.replaces is not None
    assert plan.replaces.number == 9


def test_plan_stage_ci_reports_variable_cancellations_and_replaced_evidence(fake_gh: Path) -> None:
    runs = (
        _run(7, status='in_progress', conclusion=None),
        _run(6),
        _run(8, conclusion='failure'),
        _run(5) | {'head_sha': 'b' * 40},
    )
    _script(fake_gh, _variable('false'), _recent(*runs))
    plan = _ci.plan_stage_ci(('owner', 'repo'), 'ci.yml', _COMMIT, hosted=True, full_tests=False)
    assert (plan.workflow, plan.hosted, plan.full_tests, plan.create_variable) == (
        'ci.yml',
        True,
        False,
        False,
    )
    assert tuple(run.number for run in plan.cancels) == (7,)
    assert plan.replaces is None


def test_require_ci_contract_reads_snapshot_workflow(
    tmp_path: Path, make_repo: Callable[..., Path]
) -> None:
    valid = (
        'on:\n  workflow_dispatch:\n    inputs:\n'
        '      hosted: {type: boolean}\n      full_tests: {type: boolean}\n'
    )
    store, commit = _bare_snapshot(tmp_path, make_repo, valid, name='valid')
    _ci.require_ci_contract(store, commit, 'ci.yml')
    with pytest.raises(PubanonError, match='prepared snapshot has no') as missing:
        _ci.require_ci_contract(store, commit, 'other.yml')
    assert missing.value.recovery == (
        'add the workflow or correct ci_workflow, then prepare a new snapshot'
    )

    invalid_store, invalid_commit = _bare_snapshot(
        tmp_path,
        make_repo,
        'on:\n  workflow_dispatch:\n    inputs:\n      hosted: {type: boolean}\n',
        name='invalid',
    )
    with pytest.raises(PubanonError, match='cannot receive Pubanon CI requests') as invalid:
        _ci.require_ci_contract(invalid_store, invalid_commit, 'ci.yml')
    assert invalid.value.fact == 'does not declare the boolean input full_tests'


def test_rerun_attempt_reads_jobs_for_current_attempt(fake_gh: Path) -> None:
    runs = [_run(1, attempt=3)]
    _script(fake_gh, _listing(runs), _jobs(1, ('quality', 'success'), attempt=3), _listing(runs))
    assert _evidence().state == 'success'


def test_start_stage_ci_bounds_stalled_registration_request(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    now = [0.0]
    timeouts: list[float] = []
    monkeypatch.setattr(_ci, '_monotonic', lambda: now[0], raising=False)

    def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(_ci, '_sleep', sleep, raising=False)

    def stalled(_coordinates: tuple[str, str], _workflow: str, *, timeout: float) -> bool:
        timeouts.append(timeout)
        assert timeout <= 60 - now[0]
        now[0] += timeout
        raise ObservationTimeoutError

    monkeypatch.setattr(_ci, 'workflow_registered', stalled)
    with pytest.raises(PubanonError, match='not registered'):
        _ci.start_stage_ci(('owner', 'repo'), 'ci.yml', _COMMIT, hosted=False, full_tests=False)
    assert timeouts == [60]
    assert now[0] <= 60
    assert not (tmp_path / 'fake-gh-calls.jsonl').exists()


def test_start_stage_ci_never_dispatches_when_registration_times_out(
    fake_gh: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    monkeypatch.setattr(_ci, '_monotonic', lambda: now[0], raising=False)

    def sleep(seconds: float) -> None:
        now[0] += seconds

    monkeypatch.setattr(_ci, '_sleep', sleep, raising=False)
    _script(fake_gh, {'args': _registered()['args'], 'returncode': 1, 'stderr': 'HTTP 404'})
    with pytest.raises(PubanonError, match='not registered') as error:
        _ci.start_stage_ci(('owner', 'repo'), 'ci.yml', _COMMIT, hosted=False, full_tests=False)
    assert error.value.fact == 'The snapshot is staged. CI did not start.'
    calls = (tmp_path / 'fake-gh-calls.jsonl').read_text().splitlines()
    assert len(calls) == 12
    assert now[0] == 60


def test_start_stage_ci_rejects_registration_reply_after_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [0.0]
    monkeypatch.setattr(_ci, '_monotonic', lambda: now[0], raising=False)

    def late_registration(
        _coordinates: tuple[str, str], _workflow: str, *, timeout: float
    ) -> bool:
        now[0] += timeout + 1
        return True

    monkeypatch.setattr(_ci, 'workflow_registered', late_registration)
    with pytest.raises(PubanonError, match='not registered'):
        _ci.start_stage_ci(('owner', 'repo'), 'ci.yml', _COMMIT, hosted=False, full_tests=False)
    assert now[0] == 61


@pytest.mark.parametrize(
    ('observed', 'displayed'),
    [('b' * 40, 'b' * 40), ('b\n\x1b[31m', r'b\x0a\x1b[31m')],
)
def test_start_stage_ci_reports_moved_main(fake_gh: Path, observed: str, displayed: str) -> None:
    _script(fake_gh, _registered(), _dispatch(), _read_run(_run(9) | {'head_sha': observed}))
    with pytest.raises(PubanonError, match='main moved after the push') as error:
        _ci.start_stage_ci(('owner', 'repo'), 'ci.yml', _COMMIT, hosted=True, full_tests=False)
    assert error.value.expected == _COMMIT
    assert error.value.actual == displayed
    assert observed not in error.value.render() or observed == displayed


def test_start_stage_ci_reports_read_failure_after_dispatch(fake_gh: Path) -> None:
    _script(
        fake_gh,
        _registered(),
        _dispatch(),
        {
            'args': ['api', 'repos/owner/repo/actions/runs/9'],
            'returncode': 1,
            'stderr': 'HTTP 503',
        },
    )
    with pytest.raises(PubanonError, match='run could not be verified') as error:
        _ci.start_stage_ci(('owner', 'repo'), 'ci.yml', _COMMIT, hosted=True, full_tests=False)
    assert error.value.fact == 'The snapshot is staged.'


@pytest.mark.parametrize('uncertain', [False, True])
def test_start_stage_ci_reports_refused_and_uncertain_dispatch(
    fake_gh: Path, tmp_path: Path, uncertain: bool
) -> None:
    response = _dispatch() | {
        'returncode': 1,
        'stderr': 'HTTP 503: host\nmessage' if uncertain else 'HTTP 422: host\nmessage',
    }
    _script(fake_gh, _registered(), response)
    pattern = 'CI may have started' if uncertain else 'GitHub refused'
    with pytest.raises(PubanonError, match=pattern) as error:
        _ci.start_stage_ci(('owner', 'repo'), 'ci.yml', _COMMIT, hosted=True, full_tests=False)
    assert error.value.fact is not None
    assert 'The snapshot is staged.' in error.value.fact
    if uncertain:
        assert error.value.fact == 'The snapshot is staged. GitHub did not confirm the CI request.'
    else:
        assert r'host\x0amessage' in error.value.fact
    calls = [
        json.loads(line)['args']
        for line in (tmp_path / 'fake-gh-calls.jsonl').read_text().splitlines()
    ]
    assert calls.count(_dispatch()['args']) == 1


@pytest.mark.parametrize(
    'change',
    [
        {'path': '.github/workflows/other.yml'},
        {'event': 'push'},
        {'head_branch': 'other'},
    ],
)
def test_start_stage_ci_rejects_wrong_run_identity(
    fake_gh: Path, change: dict[str, object]
) -> None:
    _script(fake_gh, _registered(), _dispatch(), _read_run(_run(9) | change))
    with pytest.raises(PubanonError, match='run could not be verified') as error:
        _ci.start_stage_ci(('owner', 'repo'), 'ci.yml', _COMMIT, hosted=True, full_tests=False)
    assert error.value.fact == 'The snapshot is staged.'


@pytest.mark.parametrize(
    'change',
    [
        {'run_attempt': 2},
        {'status': 'in_progress'},
        {'conclusion': 'failure'},
        {'id': 2},
    ],
)
def test_attempt_change_between_listings_is_pending(
    fake_gh: Path, change: dict[str, object]
) -> None:
    _script(
        fake_gh,
        _listing([_run(1)]),
        _jobs(1, ('quality', 'success')),
        _listing([_run(1) | change]),
    )
    assert _evidence().state == 'pending'


@pytest.mark.parametrize('staged', [False, True])
def test_cancelled_latest_run_recovery_requests_new_run(staged: bool) -> None:
    evidence = _ci.RunEvidence(
        'unacceptable', _display_run(conclusion='cancelled'), (), 'run cancelled'
    )
    recovery = _ci.evidence_recovery(evidence, staged=staged, retry='retry publication')
    assert 'cancelled' in recovery
    assert ('pubanon stage push' if staged else 'rerun the workflow on GitHub') in recovery
    if staged:
        assert 'superseded' in recovery
    assert recovery.endswith('then retry publication')


@pytest.mark.parametrize('expire', [False, True])
def test_evidence_asks_the_budget_before_each_request(
    fake_gh: Path, tmp_path: Path, expire: bool
) -> None:
    runs = [_run(1)]
    _script(fake_gh, _listing(runs), _jobs(1, ('quality', 'success')), _listing(runs))
    calls: list[int] = []

    def remaining() -> float:
        calls.append(len(calls) + 1)
        if expire and len(calls) == 2:
            raise ObservationTimeoutError
        return 5.0

    if expire:
        with pytest.raises(ObservationTimeoutError):
            _ci.run_evidence(
                ('owner', 'repo'),
                _ci.RunQuery('ci.yml', _COMMIT, 'workflow_dispatch'),
                required=('quality',),
                remaining=remaining,
            )
    else:
        assert (
            _ci.run_evidence(
                ('owner', 'repo'),
                _ci.RunQuery('ci.yml', _COMMIT, 'workflow_dispatch'),
                required=('quality',),
                remaining=remaining,
            ).state
            == 'success'
        )
    assert len(calls) == (2 if expire else 3)
    recorded = (tmp_path / 'fake-gh-calls.jsonl').read_text().splitlines()
    assert len(recorded) == (1 if expire else 3)


@pytest.mark.parametrize('status', ['queued', 'in_progress'])
def test_evidence_is_pending_while_latest_run_is_incomplete(fake_gh: Path, status: str) -> None:
    _script(fake_gh, _listing([_run(1), _run(2, status=status, conclusion=None)]))
    evidence = _evidence(('quality', 'package'))
    assert evidence.state == 'pending'
    assert evidence.run is not None
    assert evidence.run.number == 2
    assert [(check.name, check.state) for check in evidence.checks] == [
        ('quality', 'pending'),
        ('package', 'pending'),
    ]


@pytest.mark.parametrize('second', [False, True])
def test_evidence_propagates_failed_or_malformed_host_reads(fake_gh: Path, second: bool) -> None:
    responses = [_listing([_run(1)]), _jobs(1, ('quality', 'success'))] if second else []
    _script(fake_gh, *responses, _listing([]) | {'stdout': 'not json'})
    with pytest.raises(PubanonError, match='invalid workflow run response'):
        _evidence()


@pytest.mark.parametrize('conclusion', ['skipped', 'failure', 'cancelled', 'timed_out', None])
def test_evidence_rejects_skipped_or_absent_required_job(
    fake_gh: Path, conclusion: str | None
) -> None:
    runs = [_run(1)]
    _script(fake_gh, _listing(runs), _jobs(1, ('quality', conclusion)), _listing(runs))
    evidence = _evidence(('quality', 'package', 'Quality'))
    assert evidence.state == 'unacceptable'
    assert evidence.checks == (
        CheckVerdict('quality', 'unacceptable', conclusion or 'null'),
        CheckVerdict('package', 'unacceptable', 'not run'),
        CheckVerdict('Quality', 'unacceptable', 'not run'),
    )


@pytest.mark.parametrize('conclusion', ['success', 'failure'])
def test_evidence_reports_duplicate_job_names_as_ambiguous(fake_gh: Path, conclusion: str) -> None:
    runs = [_run(1, conclusion=conclusion)]
    _script(
        fake_gh,
        _listing(runs),
        _jobs(1, ('quality', 'success'), ('quality', 'success')),
        _listing(runs),
    )
    evidence = _evidence()
    assert evidence.state == ('ambiguous' if conclusion == 'success' else 'unacceptable')
    assert evidence.checks == (CheckVerdict('quality', 'ambiguous', '2 jobs'),)


@pytest.mark.parametrize('conclusion', ['success', 'failure', 'cancelled', 'skipped', None])
def test_evidence_requires_run_conclusion_without_required_checks(
    fake_gh: Path, conclusion: str | None
) -> None:
    runs = [_run(1, conclusion=conclusion)]
    _script(fake_gh, _listing(runs), _jobs(1), _listing(runs))
    evidence = _evidence(())
    assert evidence.state == ('success' if conclusion == 'success' else 'unacceptable')
    assert evidence.checks == ()
    assert evidence.detail == ('' if conclusion == 'success' else f'run {conclusion or "null"}')


@pytest.mark.parametrize('latest', ['success', 'failure'])
def test_latest_run_wins_over_older_results(fake_gh: Path, latest: str) -> None:
    older = 'failure' if latest == 'success' else 'success'
    runs = [_run(2, conclusion=latest), _run(1, conclusion=older, attempt=99)]
    _script(fake_gh, _listing(runs), _jobs(2, ('quality', latest)), _listing(runs))
    evidence = _evidence()
    assert evidence.state == ('success' if latest == 'success' else 'unacceptable')
    assert evidence.run is not None
    assert evidence.run.number == 2


@pytest.mark.parametrize('second', [False, True])
def test_listing_change_between_pages_is_pending(fake_gh: Path, second: bool) -> None:
    unstable = [
        _listing([_run(n) for n in range(1, 101)], total=101),
        _listing([_run(101)], page=2, total=102),
    ]
    responses = [_listing([_run(1)]), _jobs(1, ('quality', 'success'))] if second else []
    _script(fake_gh, *responses, *unstable)
    evidence = _evidence()
    assert evidence.state == 'pending'
    assert evidence.detail == (
        'a newer run or attempt appeared' if second else 'runs changed while being read'
    )


@pytest.mark.parametrize(
    ('state', 'staged', 'phrase'),
    [
        ('missing', True, 'pubanon stage push'),
        ('missing', False, 'push CI run'),
        ('pending', True, 'wait for the run'),
        ('ambiguous', True, 'duplicate runs or job names'),
        ('unacceptable', False, r'https://example.invalid/\x0arun'),
    ],
)
def test_recovery_explains_remaining_states(state: str, staged: bool, phrase: str) -> None:
    evidence = _ci.RunEvidence(state, None if state == 'missing' else _display_run(), (), '')
    recovery = _ci.evidence_recovery(evidence, staged=staged, retry='retry status')
    assert phrase in recovery
    assert recovery.endswith('then retry status')


@pytest.mark.parametrize('staged', [False, True])
def test_skipped_required_job_recovery_names_job(staged: bool) -> None:
    evidence = _ci.RunEvidence(
        'unacceptable',
        _display_run(),
        (
            CheckVerdict('hosted\nvalidation', 'unacceptable', 'skipped'),
            CheckVerdict('package', 'unacceptable', 'not run'),
        ),
        '',
    )
    recovery = _ci.evidence_recovery(evidence, staged=staged, retry='retry publication')
    assert r'hosted\x0avalidation' in recovery
    assert 'package' in recovery
    assert 'did not execute' in recovery
    assert ('pubanon stage push --hosted' in recovery) == staged
    assert recovery.endswith('then retry publication')


def test_start_stage_ci_waits_for_registration_then_dispatches_once(
    fake_gh: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = [0.0]
    sleeps: list[float] = []
    monkeypatch.setattr(_ci, '_monotonic', lambda: now[0], raising=False)

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(_ci, '_sleep', sleep, raising=False)
    _script(
        fake_gh,
        {'args': _registered()['args'], 'returncode': 1, 'stderr': 'HTTP 404', 'once': True},
        _registered(),
        _dispatch(),
        _read_run(_run(9)),
    )
    run = _ci.start_stage_ci(('owner', 'repo'), 'ci.yml', _COMMIT, hosted=True, full_tests=False)
    assert run.identifier == 9
    assert sleeps == [5]
    calls = [
        json.loads(line)['args']
        for line in (tmp_path / 'fake-gh-calls.jsonl').read_text().splitlines()
    ]
    assert calls.count(_dispatch()['args']) == 1


@pytest.mark.parametrize('second', [False, True])
def test_tied_run_numbers_are_ambiguous(fake_gh: Path, second: bool) -> None:
    responses = [_listing([_run(1)]), _jobs(1, ('quality', 'success'))] if second else []
    _script(fake_gh, *responses, _listing([_run(1), _run(1) | {'id': 2}]))
    assert _evidence().state == ('pending' if second else 'ambiguous')
