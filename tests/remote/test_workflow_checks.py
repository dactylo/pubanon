from pathlib import Path
from typing import TYPE_CHECKING, cast

import pytest

from pubanon._remote import _workflow_checks
from pubanon._snapshots._source import Entry

if TYPE_CHECKING:
    from collections.abc import Mapping

_DISPATCH_INPUTS = """\
  workflow_dispatch:
    inputs:
      hosted:
        type: boolean
      full_tests:
        type: boolean
"""
_DYNAMIC_PUSH = """\
on: push
jobs:
  build:
    strategy:
      matrix:
        os: ${{ fromJSON(vars.OSES) }}
    runs-on: linux
"""
_EXAMPLES = Path(__file__).parents[2] / 'docs/examples'
_JOB = 'jobs:\n  test:\n    runs-on: linux\n'
_PLAIN_LINT = 'on: push\njobs:\n  lint:\n    runs-on: linux\n'
_PLAIN_TEST = 'on: push\njobs:\n  test:\n    runs-on: linux\n'
_STARTER = Path(__file__).parents[2] / 'docs/examples/ci.yml'


def _requestable(push: str = '  push:\n', jobs: str = _JOB) -> str:
    return f'on:\n{push}{_DISPATCH_INPUTS}{jobs}'


def _workflow(data: str, path: str = '.github/workflows/checks.yml') -> Entry:
    return Entry(path, 0o100644, 'blob', data.encode())


def test_detect_checks_the_trigger_even_when_no_job_resolves() -> None:
    jobs = (
        'jobs:\n  test:\n    strategy:\n      matrix:\n'
        '        os: ${{ fromJSON(vars.OSES) }}\n    runs-on: linux\n'
    )
    snapshot = (
        _workflow(
            _requestable(push='  push:\n    paths: ["src/**"]\n', jobs=jobs),
            '.github/workflows/ci.yml',
        ),
    )

    assert _workflow_checks.discover(snapshot).cautions == ()
    detection = _workflow_checks.detect(snapshot)
    assert detection.requestable == ('ci.yml',)
    assert detection.connectable == ()


def test_detect_classifies_requestable_and_connectable_workflows() -> None:
    missing = 'does not declare workflow_dispatch inputs'
    detection = _workflow_checks.detect(
        (
            _workflow(_requestable(), '.github/workflows/ci.yml'),
            _workflow(_requestable(push=''), '.github/workflows/manual.yml'),
            _workflow('on:\n  workflow_dispatch:\njobs: {}\n', '.github/workflows/partial.yml'),
            _workflow(
                _requestable(push='  push:\n    paths: ["src/**"]\n'),
                '.github/workflows/paths.yml',
            ),
            _workflow(_PLAIN_LINT, '.github/workflows/plain.yml'),
        )
    )

    assert detection.requestable == ('ci.yml', 'manual.yml', 'paths.yml')
    assert detection.connectable == ('ci.yml',)
    assert set(detection.problems) == {'manual.yml', 'partial.yml', 'paths.yml', 'plain.yml'}
    assert 'workflow starts manually' in detection.problems['manual.yml']
    assert 'workflow filters changed paths' in detection.problems['paths.yml']
    assert missing in detection.problems['partial.yml']
    assert missing in detection.problems['plain.yml']
    assert detection.notes == (
        f'.github/workflows/manual.yml: {detection.problems["manual.yml"]}',
        f'.github/workflows/partial.yml: {detection.problems["partial.yml"]}',
        f'.github/workflows/paths.yml: {detection.problems["paths.yml"]}',
    )


def test_detect_reads_only_direct_workflow_blobs() -> None:
    data = _requestable().encode()
    detection = _workflow_checks.detect(
        (
            Entry('workflow.yml', 0o100644, 'blob', data),
            Entry('.github/workflows/nested/ci.yml', 0o100644, 'blob', data),
            Entry('.github/workflows/ci.txt', 0o100644, 'blob', data),
            Entry('.github/workflows/link.yml', 0o120000, 'symlink', data),
        )
    )

    assert detection == _workflow_checks.WorkflowDetection((), (), {}, ())


def test_detect_refuses_a_file_name_ci_workflow_cannot_use() -> None:
    detection = _workflow_checks.detect(
        (_workflow(_requestable(), '.github/workflows/.hidden.yml'),)
    )

    assert detection.requestable == ('.hidden.yml',)
    assert detection.connectable == ()
    assert detection.problems == {'.hidden.yml': 'has a file name that ci_workflow cannot use'}


def test_discover_applies_simple_exclude_and_include_entries() -> None:
    result = _workflow_checks.discover(
        (
            _workflow(
                """\
jobs:
  test:
    strategy:
      matrix:
        os: [ubuntu, macos]
        python: ["3.13", "3.14"]
        exclude:
          - os: macos
            python: "3.14"
        include:
          - os: windows
            python: "3.13"
          - os: ubuntu
            python: "3.13"
            experimental: true
"""
            ),
        )
    )

    assert result.names == (
        'test (ubuntu, 3.13, true)',
        'test (ubuntu, 3.14)',
        'test (macos, 3.13)',
        'test (windows, 3.13)',
    )
    assert result.notes == ()


def test_discover_deduplicates_names_and_reports_the_duplicate() -> None:
    result = _workflow_checks.discover(
        (
            _workflow(
                """\
jobs:
  first:
    name: Shared
    runs-on: ubuntu-latest
  second:
    name: Shared
    runs-on: macos-latest
"""
            ),
        )
    )

    assert result.names == ('Shared',)
    assert len(result.notes) == 1
    assert 'checks.yml' in result.notes[0]
    assert 'second' in result.notes[0]
    assert 'Shared' in result.notes[0]


def test_discover_does_not_add_matrix_suffix_to_explicit_name_equal_to_job_id() -> None:
    result = _workflow_checks.discover(
        (
            _workflow(
                """\
jobs:
  test:
    name: test
    strategy:
      matrix:
        os: [ubuntu, macos]
    runs-on: ${{ matrix.os }}
"""
            ),
        )
    )

    assert result.names == ('test',)
    assert len(result.notes) == 1
    assert 'duplicate discovered check name' in result.notes[0]


def test_discover_identifies_conditions_on_each_matrix_check() -> None:
    result = _workflow_checks.discover(
        (
            _workflow("""\
on: push
jobs:
  tests:
    if: inputs.run_tests
    strategy:
      matrix:
        os: [linux, windows]
"""),
        )
    )

    assert result.names == ('tests (linux)', 'tests (windows)')
    assert tuple(caution.name for caution in result.cautions) == result.names
    assert all(
        any('condition' in message for message in caution.reasons) for caution in result.cautions
    )
    assert all(
        caution.reasons == ('job runs only when its if: condition holds',)
        for caution in result.cautions
    )


def test_discover_ignores_other_paths_and_non_blob_entries() -> None:
    workflow = b'jobs:\n  build:\n    runs-on: ubuntu-latest\n'
    result = _workflow_checks.discover(
        (
            Entry('workflow.yml', 0o100644, 'blob', workflow),
            Entry('.github/workflows/nested/checks.yml', 0o100644, 'blob', workflow),
            Entry('.github/workflows/checks.txt', 0o100644, 'blob', workflow),
            Entry('.github/workflows/link.yml', 0o120000, 'symlink', workflow),
        )
    )

    assert result == _workflow_checks.Discovery((), ())


def test_discover_preserves_include_only_matrix_order() -> None:
    workflow = _workflow(
        """\
jobs:
  quality:
    runs-on: ubuntu-latest
  tests:
    strategy:
      matrix:
        include:
          - os: windows-2022
            python-version: "3.13"
          - os: ubuntu-24.04
            python-version: "3.14"
          - os: macos-15
            python-version: "3.13"
"""
    )

    result = _workflow_checks.discover((workflow,))

    assert result.names == (
        'quality',
        'tests (windows-2022, 3.13)',
        'tests (ubuntu-24.04, 3.14)',
        'tests (macos-15, 3.13)',
    )
    assert result.notes == ()


def test_discover_rejects_expressions_inside_literal_matrix_values() -> None:
    result = _workflow_checks.discover(
        (
            _workflow(
                """\
jobs:
  dynamic-axis:
    strategy:
      matrix:
        os: [ubuntu, "${{ inputs.os }}"]
    runs-on: ${{ matrix.os }}
  dynamic-include:
    strategy:
      matrix:
        include:
          - os: "${{ inputs.os }}"
    runs-on: ${{ matrix.os }}
"""
            ),
        )
    )

    assert result.names == ()
    assert len(result.notes) == 2
    assert all('checks.yml' in note and 'expression' in note for note in result.notes)


def test_discover_reports_jobs_it_cannot_resolve() -> None:
    result = _workflow_checks.discover(
        (
            _workflow(
                """\
jobs:
  expression:
    name: Test ${{ fromJSON(needs.setup.outputs.matrix) }}
    runs-on: ubuntu-latest
  dynamic:
    strategy:
      matrix: ${{ fromJSON(needs.setup.outputs.matrix) }}
    runs-on: ubuntu-latest
  objects:
    strategy:
      matrix:
        target:
          - os: ubuntu
    runs-on: ubuntu-latest
  called:
    uses: owner/repository/.github/workflows/shared.yml@main
"""
            ),
            _workflow('jobs: [', '.github/workflows/broken.yaml'),
        )
    )

    assert result.names == ()
    assert len(result.notes) == 5
    assert any('checks.yml' in note and 'expression' in note for note in result.notes)
    assert any('checks.yml' in note and 'dynamic' in note for note in result.notes)
    assert any('checks.yml' in note and 'objects' in note for note in result.notes)
    assert any('checks.yml' in note and 'called' in note for note in result.notes)
    assert any('broken.yaml' in note and 'could not be parsed' in note for note in result.notes)


def test_discover_uses_static_and_matrix_expanded_explicit_names() -> None:
    result = _workflow_checks.discover(
        (
            _workflow(
                """\
jobs:
  lint:
    name: Lint source
    runs-on: ubuntu-latest
  test:
    name: Test ${{ matrix.python }} on ${{matrix.os}}
    strategy:
      matrix:
        python: ["3.13", "3.14"]
        os: [ubuntu-latest]
    runs-on: ${{ matrix.os }}
"""
            ),
        )
    )

    assert result.names == (
        'Lint source',
        'Test 3.13 on ubuntu-latest',
        'Test 3.14 on ubuntu-latest',
    )
    assert result.notes == ()


def test_discover_workflow_reads_only_the_named_workflow() -> None:
    snapshot = (
        _workflow('jobs:\n  quality:\n    runs-on: linux\n', '.github/workflows/ci.yml'),
        _workflow('jobs:\n  publish:\n    runs-on: linux\n', '.github/workflows/release.yml'),
    )

    assert _workflow_checks.discover_workflow(snapshot, 'ci.yml').names == ('quality',)
    assert _workflow_checks.discover_workflow(
        snapshot, 'absent.yml'
    ) == _workflow_checks.Discovery((), ())


def test_dispatch_contract_accepts_pubanon_workflow() -> None:
    workflow = Path(__file__).parents[2] / '.github/workflows/ci.yml'

    assert _workflow_checks.dispatch_contract_problem(workflow.read_bytes()) is None


@pytest.mark.parametrize(
    ('data', 'problem'),
    [
        ('jobs: {}', 'does not declare workflow_dispatch inputs'),
        ('on: [push, workflow_dispatch]', 'does not declare workflow_dispatch inputs'),
        ('on:\n  workflow_dispatch:', 'does not declare workflow_dispatch inputs'),
        (
            'on:\n  workflow_dispatch:\n    inputs:\n      hosted:\n        type: boolean',
            'does not declare the boolean input full_tests',
        ),
        (
            """\
on:
  workflow_dispatch:
    inputs:
      hosted:
        type: string
      full_tests:
        type: boolean""",
            'does not declare the boolean input hosted',
        ),
        ('on: [', 'could not be parsed as YAML 1.2'),
        ('- push', 'does not contain a workflow mapping'),
    ],
)
def test_dispatch_contract_reports_missing_declarations(data: str, problem: str) -> None:
    assert _workflow_checks.dispatch_contract_problem(data.encode()) == problem


@pytest.mark.parametrize(
    ('trigger', 'reason'),
    [
        ('on: workflow_dispatch', 'manually'),
        ('on: [release, workflow_dispatch]', 'branch pushes'),
        ('on:\n  push:\n    tags: ["v*"]', 'tags'),
        ('on:\n  push:\n    branches: [release]', 'branches'),
        ('on:\n  push:\n    paths: ["src/**"]', 'paths'),
        ('on:\n  push:\n    paths-ignore: ["docs/**"]', 'paths'),
        ('on:\n  push:\n    branches-ignore: [main]', 'branches'),
        ('', 'trigger'),
    ],
)
def test_discover_explains_when_a_workflow_may_not_run_on_a_branch_push(
    trigger: str,
    reason: str,
) -> None:
    result = _workflow_checks.discover(
        (_workflow(f'{trigger}\njobs:\n  verify:\n    runs-on: linux\n'),)
    )

    assert result.names == ('verify',)
    (caution,) = result.cautions
    assert caution.name == 'verify'
    assert caution.path == '.github/workflows/checks.yml'
    assert any(reason in message for message in caution.reasons)


@pytest.mark.parametrize(
    'trigger', ['on: push', 'on: [push, workflow_dispatch]', 'on:\n  push:\n    branches: ["**"]']
)
def test_discover_recognizes_unfiltered_branch_pushes(trigger: str) -> None:
    result = _workflow_checks.discover(
        (_workflow(f'{trigger}\njobs:\n  quality:\n    runs-on: linux\n'),)
    )

    assert result.names == ('quality',)
    assert result.cautions == ()


@pytest.mark.parametrize(
    ('push', 'connectable'),
    [
        ('  push:\n', True),
        ('  push:\n    branches: ["**"]\n', True),
        ('  push:\n    branches: [main]\n', False),
        ('  push:\n    tags: ["v*"]\n', False),
        ('  push:\n    paths-ignore: ["docs/**"]\n', False),
        ('', False),
    ],
)
def test_detect_connects_only_workflows_that_start_on_every_branch_push(
    push: str,
    connectable: bool,
) -> None:
    detection = _workflow_checks.detect(
        (_workflow(_requestable(push=push), '.github/workflows/ci.yml'),)
    )

    assert detection.requestable == ('ci.yml',)
    assert (detection.connectable == ('ci.yml',)) is connectable


@pytest.mark.parametrize(
    ('trigger', 'noted'),
    [
        ('on: workflow_dispatch', True),
        ('on: [push, workflow_dispatch]', True),
        ('on:\n  workflow_dispatch:', True),
        ('on: push', False),
    ],
)
def test_detect_notes_dispatch_triggers_without_inputs(trigger: str, noted: bool) -> None:
    detection = _workflow_checks.detect((_workflow(f'{trigger}\njobs: {{}}\n'),))

    assert detection.requestable == ()
    assert detection.problems == {'checks.yml': 'does not declare workflow_dispatch inputs'}
    expected = ('.github/workflows/checks.yml: does not declare workflow_dispatch inputs',)
    assert detection.notes == (expected if noted else ())


@pytest.mark.parametrize(
    ('files', 'checks', 'expected'),
    [
        ({'ci.yml': _requestable()}, ('test',), True),
        ({'ci.yml': _requestable(push='')}, ('test',), True),
        ({'ci.yml': _requestable()}, (), False),
        ({'ci.yml': _requestable(), 'plain.yml': _PLAIN_LINT}, ('lint',), False),
        ({'ci.yml': _requestable(), 'plain.yml': _PLAIN_TEST}, ('test',), False),
        ({'ci.yml': _requestable(), 'plain.yml': _DYNAMIC_PUSH}, ('test',), False),
        ({'ci.yml': _requestable()}, ('missing',), False),
        ({'plain.yml': _PLAIN_TEST}, ('test',), False),
    ],
)
def test_only_requestable_checks_requires_evidence(
    files: dict[str, str],
    checks: tuple[str, ...],
    expected: bool,
) -> None:
    snapshot = tuple(_workflow(data, f'.github/workflows/{name}') for name, data in files.items())

    assert _workflow_checks.only_requestable_checks(snapshot, checks) is expected


def test_starter_release_workflows_do_not_run_on_branch_pushes() -> None:
    snapshot = tuple(
        Entry(f'.github/workflows/{name}', 0o100644, 'blob', (_EXAMPLES / name).read_bytes())
        for name in ('release-start.yml', 'release.yml')
    )
    discovered = _workflow_checks.discover(snapshot)

    assert discovered.names == (
        'Start release',
        'Verify release',
        'Build distributions',
        'Publish to PyPI',
        'Publish release page',
    )
    assert discovered.notes == ()
    # init flags these checks instead of selecting them as required by default.
    assert {caution.name for caution in discovered.cautions} == set(discovered.names)


def test_starter_workflow_is_connectable_and_lists_every_job() -> None:
    data = _STARTER.read_bytes()
    snapshot = (Entry('.github/workflows/ci.yml', 0o100644, 'blob', data),)
    discovered = _workflow_checks.discover(snapshot)

    assert _workflow_checks.dispatch_contract_problem(data) is None
    assert _workflow_checks.detect(snapshot).connectable == ('ci.yml',)
    assert discovered.names == (
        'test (ubuntu-24.04)',
        'test (macos-15)',
        'test (windows-2022)',
        'hosted-validation',
    )
    assert discovered.notes == ()


def test_starter_workflow_skips_push_runs_and_fails_until_customized() -> None:
    root, problem = _workflow_checks._load_workflow(_STARTER.read_bytes())
    assert problem is None
    assert root is not None
    jobs = cast('Mapping[str, Mapping[str, object]]', root['jobs'])

    assert set(jobs) == {'test', 'hosted-validation'}
    assert all("vars.CI_RUN_ON_PUSH != 'false'" in str(job['if']) for job in jobs.values())
    steps = cast('list[Mapping[str, object]]', jobs['test']['steps'])
    assert str(steps[-1]['run']).rstrip().endswith('exit 1')
