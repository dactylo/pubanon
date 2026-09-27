import pytest

from pubanon._remote import _workflow_checks
from pubanon._snapshots._source import Entry


def _workflow(data: str, path: str = '.github/workflows/checks.yml') -> Entry:
    return Entry(path, 0o100644, 'blob', data.encode())


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


@pytest.mark.parametrize(
    'trigger', ['on: push', 'on: [push, workflow_dispatch]', 'on:\n  push:\n    branches: ["**"]']
)
def test_discover_recognizes_unfiltered_branch_pushes(trigger: str) -> None:
    result = _workflow_checks.discover(
        (_workflow(f'{trigger}\njobs:\n  quality:\n    runs-on: linux\n'),)
    )

    assert result.names == ('quality',)
    assert result.cautions == ()


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
