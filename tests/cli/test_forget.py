import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Generator
from contextlib import ExitStack, contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from pubanon import _cli, _config
from pubanon._cli import _forget
from pubanon._snapshots import _records, _verification
from pubanon._terminal import display_path, escape
from tests._cli import gh_calls, prepare, record_publication
from tests._git import git_env
from tests._setup import configure


@contextmanager
def _held_lock(name: str) -> Generator[None]:
    script = (
        'import os, sys\n'
        'from pubanon import _config\n'
        'with _config.project_lock(_config.derive_paths(os.environ), sys.argv[1]):\n'
        ' print("locked", flush=True)\n'
        ' sys.stdin.readline()\n'
    )
    with subprocess.Popen(
        [sys.executable, '-c', script, name],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        env=dict(os.environ),
    ) as process:
        assert process.stdout is not None
        assert process.stdin is not None
        assert process.stdout.readline().strip() == 'locked'
        try:
            yield
        finally:
            process.stdin.write('\n')
            process.stdin.flush()
            assert process.wait(timeout=10) == 0


def test_forget_absent_project_does_not_create_project_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    assert _cli.main(['forget', '--project', 'demo', '--yes']) == 0
    assert not paths.data_dir.exists()


@pytest.mark.parametrize('yes', [False, True])
def test_forget_confirmation(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    yes: bool,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo()
    paths, _, _ = configure(tmp_path, monkeypatch, source)
    configuration = paths.config_dir / 'projects' / 'demo.toml'
    before = configuration.read_bytes(), paths.registry.read_bytes()
    prompts: list[str] = []

    def _decline(prompt: str) -> str:
        prompts.append(prompt)
        return 'n'

    monkeypatch.setattr('builtins.input', _decline)
    assert _cli.main(['forget', '--project', 'demo', *(['--yes'] if yes else [])]) == (
        0 if yes else 130
    )
    assert bool(prompts) is not yes
    if not yes:
        captured = capsys.readouterr()
        assert 'Canceled. Nothing was removed.' in captured.err
        assert f'Registry entry: demo (in {display_path(paths.registry)})' in captured.out
        assert f'Configuration: {display_path(configuration)}' in captured.out
        assert (configuration.read_bytes(), paths.registry.read_bytes()) == before


@pytest.mark.parametrize(
    'case',
    [
        (state, force)
        for state in ('unknown', 'candidate.toml', 'rewrite.toml', 'worktree')
        for force in (False, True)
    ],
)
def test_forget_checks_recovery_before_changes(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[str, bool],
) -> None:
    state, force = case
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    directory = paths.data_dir / 'projects' / project.name
    if state == 'unknown':
        _records.save_candidate(
            paths, project.name, replace(record, state=_records.STATE_UNKNOWN_OUTCOME)
        )
    elif state == 'worktree':
        (directory / 'source-worktrees' / 'retained').mkdir(parents=True)
    else:
        (directory / state).write_text('broken = [')
    configuration = paths.config_dir / 'projects' / 'demo.toml'
    before = configuration.read_bytes(), paths.registry.read_bytes()
    capsys.readouterr()
    expected = 0 if force and state != 'worktree' else 1
    assert (
        _cli.main(['forget', '--project', 'demo', '--yes', *(['--force'] if force else [])])
        == expected
    )
    if expected:
        error = capsys.readouterr().err
        assert (configuration.read_bytes(), paths.registry.read_bytes()) == before
        if state == 'unknown':
            assert 'pubanon publish --project=demo --resolve' in error
        elif state == 'worktree':
            worktree = directory / 'source-worktrees' / 'retained'
            assert display_path(worktree) in error
            command = next(
                line.removeprefix('Next: ')
                for line in error.splitlines()
                if line.startswith('Next: ')
            )
            assert shlex.split(command) == [
                'git',
                '-C',
                str(source),
                'worktree',
                'remove',
                str(worktree),
            ]
        else:
            assert escape((directory / state).as_posix()) in error
            assert 'invalid' in error


def test_forget_keeps_a_lock_acquired_at_completion(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch, make_repo())
    original_output = _forget.output
    with ExitStack() as held:

        def _handoff(text: str, **_kwargs: object) -> None:
            if text == 'Forgot the local project data.':
                held.enter_context(_held_lock('demo'))
            original_output(text)

        monkeypatch.setattr(_forget, 'output', _handoff)
        assert _cli.main(['forget', '--project', 'demo', '--yes']) == 0
        assert (paths.data_dir / 'projects' / 'demo' / 'project.lock').exists()
        assert _cli.main(['status', '--project', 'demo']) == 1
        assert 'another pubanon command is running for this project' in capsys.readouterr().err


def test_forget_refuses_held_lock(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch, make_repo())
    with _held_lock('demo'):
        assert _cli.main(['forget', '--project', 'demo', '--yes']) == 1
    assert 'another pubanon command is running for this project' in capsys.readouterr().err
    assert (paths.config_dir / 'projects' / 'demo.toml').exists()


def test_forget_removes_local_state_keeps_source_and_lock(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    monkeypatch.setenv('GIT_DIR', str(tmp_path / 'inherited.git'))
    subprocess.run(['git', '-C', source, 'tag', 'pubanon/v1.0.0'], check=True, env=git_env())
    directory = paths.data_dir / 'projects' / project.name
    lock = directory / 'project.lock'
    identity = lock.stat().st_ino
    outside = tmp_path / 'outside'
    outside.mkdir()
    (outside / 'kept').touch()
    (directory / 'link').symlink_to(outside, target_is_directory=True)
    (directory / 'draft.json').write_text('{}')
    inspection = _verification.materialize_inspect(
        paths, project.name, _verification.prove_objects(paths, project.name, record)
    )
    assert inspection.exists()
    record_publication(paths, project, record)
    refs = subprocess.check_output(['git', '-C', source, 'show-ref'], env=git_env())
    capsys.readouterr()
    monkeypatch.chdir(source)

    assert _cli.main(['forget', '--yes']) == 0

    assert list(directory.iterdir()) == [lock]
    assert lock.stat().st_ino == identity
    assert (outside / 'kept').exists()
    assert _config.registry_lookup(paths, source) is None
    assert not (paths.config_dir / 'projects' / 'demo.toml').exists()
    assert subprocess.check_output(['git', '-C', source, 'show-ref'], env=git_env()) == refs
    assert gh_calls(tmp_path) == []
    rendered = capsys.readouterr().out
    for expected in (
        'Project: demo',
        display_path(source),
        display_path(inspection.parent),
        'anonymous-account/public-repo',
        'pubanon init',
        'pubanon repo restore --to local',
    ):
        assert expected in rendered
    assert _cli.main(['status', '--project', 'demo']) == 1
    assert 'configuration' in capsys.readouterr().err
    with _held_lock('demo'):
        assert _cli.main(['status', '--project', 'demo']) == 1
        assert 'another pubanon command is running for this project' in capsys.readouterr().err
    answers = iter(['c', '', '', '', '', '', '', '', '', 's'])

    def _answer(prompt: str) -> str:
        if prompt.startswith('Publication route (staged/direct) [') or prompt in {
            'Create local tags marking the source of each publication? [y/N] ',
            'Create a release page for each published version? [y/N] ',
            'Enable pull requests? [y/N] ',
            'Enable discussions? [y/N] ',
        }:
            return ''
        return next(answers)

    monkeypatch.setattr('builtins.input', _answer)
    assert _cli.main(['init', '--project', 'demo']) == 0
    assert _config.registry_lookup(paths, source) == 'demo'
    assert lock.stat().st_ino == identity


@pytest.mark.parametrize('removed', [0, 1, 2, 3])
def test_forget_resumes_partial_removal(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    removed: int,
) -> None:
    source = make_repo()
    paths, _, _ = configure(tmp_path, monkeypatch, source)
    directory = _config.create_data_dirs(paths, 'demo')
    (directory / 'remaining').touch()
    configuration = paths.config_dir / 'projects' / 'demo.toml'
    if removed >= 1:
        paths.registry.write_text('{}')
    if removed >= 2:
        configuration.unlink()
    if removed >= 3:
        (directory / 'remaining').unlink()
        (directory / 'project.lock').touch()
    assert _cli.main(['forget', '--project', 'demo', '--yes']) == 0
    assert list(directory.iterdir()) == [directory / 'project.lock']
    assert json.loads(paths.registry.read_text()) == {}
    assert not configuration.exists()
    rendered = capsys.readouterr().out
    if removed == 1:
        assert f'Source: {display_path(source)}' in rendered
    if removed == 2:
        assert 'Final repository: unknown' in rendered
        assert 'Staging repository: unknown' in rendered
    if removed == 3:
        assert 'Nothing to remove' in rendered


@pytest.mark.parametrize('name', ['../other', ''])
def test_forget_invalid_name_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], name: str
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    assert _cli.main(['forget', '--project', name, '--yes']) == 1
    assert 'invalid project name' in capsys.readouterr().err
    assert not paths.data_dir.exists()


def test_forget_unreadable_configuration_keeps_registered_source_in_preview(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo()
    paths, _, _ = configure(tmp_path, monkeypatch, source)
    (paths.config_dir / 'projects' / 'demo.toml').write_text('broken = [')
    assert _cli.main(['forget', '--project', 'demo', '--yes']) == 0
    rendered = capsys.readouterr().out
    assert display_path(source) in rendered
    assert 'Final repository: unknown' in rendered
    assert 'Staging repository: unknown' in rendered
