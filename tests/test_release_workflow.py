import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import cast

import pytest

from pubanon._remote import _workflow_checks
from tests._cli import gh_calls
from tests._git import git_env

_LISTING_ARGS = ['api', '--paginate', '--slurp', 'repos/owner/repo/releases?per_page=100']
_OWN = Path(__file__).parents[1] / '.github/workflows'
_POSIX_ONLY = pytest.mark.skipif(
    os.name == 'nt', reason='release workflow steps run on Ubuntu runners'
)
_PUBLISHED_URL = 'https://github.example/owner/repo/releases/tag/v0.6.0'
_STARTER = Path(__file__).parents[1] / 'docs/examples'
_START_ARGS = ['workflow', 'run', 'release.yml', '--repo', 'owner/repo', '--ref', 'v0.6.0']


def _ci_query(commit: str) -> list[str]:
    return [
        'api',
        '--method',
        'GET',
        'repos/owner/repo/actions/workflows/ci.yml/runs',
        '-f',
        f'head_sha={commit}',
        '-f',
        'branch=main',
        '-f',
        'event=push',
        '-f',
        'per_page=1',
    ]


def _load(path: Path) -> Mapping[str, object]:
    root, problem = _workflow_checks._load_workflow(path.read_bytes())
    assert problem is None
    assert root is not None
    return cast('Mapping[str, object]', root)


def _jobs(path: Path) -> Mapping[str, Mapping[str, object]]:
    return cast('Mapping[str, Mapping[str, object]]', _load(path)['jobs'])


def _patch_args(identifier: int, latest: str) -> list[str]:
    return [
        'api',
        '--method',
        'PATCH',
        f'repos/owner/repo/releases/{identifier}',
        '-F',
        'draft=false',
        '-f',
        f'make_latest={latest}',
    ]


def _release(
    identifier: int, tag: str, *, draft: bool = False, prerelease: bool = False
) -> dict[str, object]:
    return {
        'id': identifier,
        'tag_name': tag,
        'draft': draft,
        'prerelease': prerelease,
        'html_url': f'https://github.example/owner/repo/releases/{identifier}',
    }


def _repository(
    make_repo: Callable[..., Path], version: str = '0.6.0', *, tagged: bool
) -> tuple[Path, str]:
    pyproject = f'[project]\nname = "example"\nversion = "{version}"\n'
    repo = make_repo(files={'pyproject.toml': pyproject})
    commit = subprocess.run(
        ['git', '-C', str(repo), 'rev-parse', 'HEAD'],
        capture_output=True,
        text=True,
        check=True,
        env=git_env(),
    ).stdout.strip()
    if tagged:
        subprocess.run(['git', '-C', str(repo), 'tag', f'v{version}'], check=True, env=git_env())
    return repo, commit


def _step(path: Path, job: str, step_id: str) -> Mapping[str, object]:
    steps = cast('list[Mapping[str, object]]', _jobs(path)[job]['steps'])
    return next(step for step in steps if step.get('id') == step_id)


def _run_step(
    tmp_path: Path, workflow: str, job: str, env: Mapping[str, str], *, cwd: Path
) -> subprocess.CompletedProcess[str]:
    step = _step(_STARTER / workflow, job, job)
    assert step['shell'] == 'python'
    script = tmp_path / f'{job}.py'
    script.write_text(str(step['run']), encoding='utf-8')
    return subprocess.run(
        [sys.executable, str(script)],
        cwd=cwd,
        env={
            **git_env(),
            'GH_TOKEN': 'token',
            'GITHUB_OUTPUT': str(tmp_path / 'outputs.txt'),
            'GITHUB_REPOSITORY': 'owner/repo',
            'GITHUB_STEP_SUMMARY': str(tmp_path / 'summary.md'),
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )


def _publish_page(
    tmp_path: Path,
    fake_gh: Path,
    releases: list[dict[str, object]],
    patch: list[str] | None = None,
) -> str:
    responses: list[dict[str, object]] = [
        {'args': _LISTING_ARGS, 'stdout': json.dumps([releases])}
    ]
    if patch is not None:
        responses.append({'args': patch, 'stdout': json.dumps({'html_url': _PUBLISHED_URL})})
    fake_gh.write_text(json.dumps(responses), encoding='utf-8')
    result = _run_step(tmp_path, 'release.yml', 'page', {'TAG': 'v0.6.0'}, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    return (tmp_path / 'summary.md').read_text(encoding='utf-8')


def _start(tmp_path: Path, repo: Path, commit: str) -> subprocess.CompletedProcess[str]:
    env = {'RELEASE_COMMIT': commit, 'RELEASE_WORKFLOW_FILE': 'release.yml'}
    return _run_step(tmp_path, 'release-start.yml', 'start', env, cwd=repo)


def _verify(tmp_path: Path, repo: Path, commit: str) -> subprocess.CompletedProcess[str]:
    env = {'CI_WORKFLOW_FILE': 'ci.yml', 'DEFAULT_BRANCH': 'main', 'GITHUB_SHA': commit}
    return _run_step(tmp_path, 'release.yml', 'verify', env, cwd=repo)


@_POSIX_ONLY
def test_page_step_leaves_a_published_page_alone(tmp_path: Path, fake_gh: Path) -> None:
    # A stray draft for the same tag is not published over the published page.
    summary = _publish_page(
        tmp_path, fake_gh, [_release(1, 'v0.6.0'), _release(2, 'v0.6.0', draft=True)]
    )

    assert gh_calls(tmp_path) == [_LISTING_ARGS]
    assert summary == (
        'The release page for v0.6.0 is already published: '
        'https://github.example/owner/repo/releases/1\n'
    )


@_POSIX_ONLY
def test_page_step_publishes_a_late_draft_when_run_again(tmp_path: Path, fake_gh: Path) -> None:
    first = _publish_page(tmp_path, fake_gh, [_release(2, 'v0.5.0')])

    assert first == (
        'No release page for v0.6.0. If pubanon creates a draft for it later, '
        'rerun this workflow to publish it.\n'
    )
    assert gh_calls(tmp_path) == [_LISTING_ARGS]

    second = _publish_page(
        tmp_path,
        fake_gh,
        [_release(1, 'v0.6.0', draft=True), _release(2, 'v0.5.0')],
        _patch_args(1, 'true'),
    )

    assert second.endswith(f'Published the release page for v0.6.0: {_PUBLISHED_URL}\n')
    assert gh_calls(tmp_path)[-1] == _patch_args(1, 'true')


@_POSIX_ONLY
@pytest.mark.parametrize(
    ('others', 'prerelease', 'latest'),
    [
        ([], False, 'true'),
        ([_release(2, 'v0.5.0')], False, 'true'),
        ([_release(2, 'v0.10.0')], False, 'false'),
        ([_release(2, 'v1.0.0', draft=True)], False, 'true'),
        ([_release(2, 'v1.0.0', prerelease=True)], False, 'true'),
        ([_release(2, 'nightly')], False, 'true'),
        ([], True, 'false'),
    ],
)
def test_page_step_publishes_the_draft_by_the_latest_rule(
    tmp_path: Path,
    fake_gh: Path,
    others: list[dict[str, object]],
    *,
    prerelease: bool,
    latest: str,
) -> None:
    draft = _release(1, 'v0.6.0', draft=True, prerelease=prerelease)

    summary = _publish_page(tmp_path, fake_gh, [draft, *others], _patch_args(1, latest))

    assert gh_calls(tmp_path) == [_LISTING_ARGS, _patch_args(1, latest)]
    assert summary == f'Published the release page for v0.6.0: {_PUBLISHED_URL}\n'


@pytest.mark.parametrize('directory', [_STARTER, _OWN])
def test_release_checks_come_only_from_dispatched_runs(directory: Path) -> None:
    # A run dispatched on a tag reports its checks on the tagged commit, where package
    # observation looks. CI runs that publish nothing start only release-start.yml, and
    # a skipped job there must not share a name with a release check.
    release_names = {job['name'] for job in _jobs(directory / 'release.yml').values()}
    start_names = {job['name'] for job in _jobs(directory / 'release-start.yml').values()}

    assert _load(directory / 'release.yml')['on'] == {'workflow_dispatch': None}
    assert 'Publish to PyPI' in release_names
    assert release_names.isdisjoint(start_names)


@pytest.mark.parametrize(
    ('workflow', 'job'),
    [('release-start.yml', 'start'), ('release.yml', 'verify'), ('release.yml', 'page')],
)
def test_shared_steps_are_identical_in_the_starter_and_pubanon_workflows(
    workflow: str, job: str
) -> None:
    assert _step(_OWN / workflow, job, job) == _step(_STARTER / workflow, job, job)


@_POSIX_ONLY
def test_start_step_starts_the_release_workflow_on_a_tagged_version(
    tmp_path: Path, make_repo: Callable[..., Path], fake_gh: Path
) -> None:
    repo, commit = _repository(make_repo, tagged=True)
    fake_gh.write_text(json.dumps([{'args': _START_ARGS}]), encoding='utf-8')

    result = _start(tmp_path, repo, commit)

    assert result.returncode == 0, result.stderr
    assert gh_calls(tmp_path) == [_START_ARGS]
    assert (tmp_path / 'summary.md').read_text(encoding='utf-8') == (
        'Started the release workflow for v0.6.0.\n'
    )


@_POSIX_ONLY
@pytest.mark.parametrize(
    ('version', 'tagged', 'summary'),
    [
        ('0.6.0', False, 'No v0.6.0 tag at this commit; nothing to publish.\n'),
        (
            '0.6.0rc1',
            True,
            'Version 0.6.0rc1 in pyproject.toml is not in X.Y.Z form; nothing to publish.\n',
        ),
        (
            '01.2.3',
            True,
            'Version 01.2.3 in pyproject.toml is not in X.Y.Z form; nothing to publish.\n',
        ),
    ],
)
def test_start_step_starts_nothing_without_a_strict_version_tag(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    *,
    version: str,
    tagged: bool,
    summary: str,
) -> None:
    repo, commit = _repository(make_repo, version, tagged=tagged)

    result = _start(tmp_path, repo, commit)

    assert result.returncode == 0, result.stderr
    assert gh_calls(tmp_path) == []
    assert (tmp_path / 'summary.md').read_text(encoding='utf-8') == summary


@_POSIX_ONLY
def test_verify_step_continues_for_a_tagged_commit_with_passing_ci(
    tmp_path: Path, make_repo: Callable[..., Path], fake_gh: Path
) -> None:
    repo, commit = _repository(make_repo, tagged=True)
    run = {
        'id': 7,
        'status': 'completed',
        'conclusion': 'success',
        'html_url': 'https://github.example/runs/7',
    }
    fake_gh.write_text(
        json.dumps([{'args': _ci_query(commit), 'stdout': json.dumps({'workflow_runs': [run]})}]),
        encoding='utf-8',
    )

    result = _verify(tmp_path, repo, commit)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'outputs.txt').read_text(encoding='utf-8') == (
        'tag=v0.6.0\nname=example\nrun-id=7\n'
    )
    assert (tmp_path / 'summary.md').read_text(encoding='utf-8') == (
        f'Publishing v0.6.0 from `{commit}`. [CI run](https://github.example/runs/7).\n'
    )
    assert gh_calls(tmp_path) == [_ci_query(commit)]


@_POSIX_ONLY
@pytest.mark.parametrize(
    ('runs', 'message'),
    [
        ([], 'No push CI run exists for this release commit.'),
        (
            [
                {
                    'id': 7,
                    'status': 'completed',
                    'conclusion': 'failure',
                    'html_url': 'https://github.example/runs/7',
                }
            ],
            'CI must pass before publishing: https://github.example/runs/7',
        ),
    ],
)
def test_verify_step_refuses_missing_or_failed_ci(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    fake_gh: Path,
    runs: list[dict[str, object]],
    message: str,
) -> None:
    repo, commit = _repository(make_repo, tagged=True)
    fake_gh.write_text(
        json.dumps([{'args': _ci_query(commit), 'stdout': json.dumps({'workflow_runs': runs})}]),
        encoding='utf-8',
    )

    result = _verify(tmp_path, repo, commit)

    assert result.returncode != 0
    assert message in result.stderr
    assert not (tmp_path / 'outputs.txt').exists()
    assert gh_calls(tmp_path) == [_ci_query(commit)]


@_POSIX_ONLY
@pytest.mark.parametrize(
    ('version', 'tagged', 'message'),
    [
        ('0.6.0', False, 'v0.6.0 must point to the selected commit before publishing.'),
        ('0.6.0rc1', True, 'Version 0.6.0rc1 in pyproject.toml must have X.Y.Z form to publish.'),
        ('01.2.3', True, 'Version 01.2.3 in pyproject.toml must have X.Y.Z form to publish.'),
    ],
)
def test_verify_step_refuses_a_commit_without_a_strict_version_tag(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    *,
    version: str,
    tagged: bool,
    message: str,
) -> None:
    repo, commit = _repository(make_repo, version, tagged=tagged)

    result = _verify(tmp_path, repo, commit)

    assert result.returncode != 0
    assert message in result.stderr
    assert not (tmp_path / 'outputs.txt').exists()
    assert gh_calls(tmp_path) == []
