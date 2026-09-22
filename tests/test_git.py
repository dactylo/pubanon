import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from tests._git import git_env


def _write_executable(path: Path, marker_variable: str, *, command: bool = False) -> Path:
    if os.name == 'nt' and command:
        path = path.with_suffix('.cmd')
        path.write_text(f'@echo off\n type nul > "%{marker_variable}%"\n')
    else:
        path.write_text(f'#!/bin/sh\n: > "${marker_variable}"\n', newline='\n')
        path.chmod(0o755)
    return path


def test_fixture_git_disables_hooks_signing_and_templates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hook_marker = tmp_path / 'hook-ran'
    signing_marker = tmp_path / 'signing-ran'
    hooks = tmp_path / 'hooks'
    hooks.mkdir()
    _write_executable(hooks / 'pre-commit', 'HOOK_MARKER')
    signing_program = _write_executable(tmp_path / 'signer', 'SIGNING_MARKER', command=True)
    template = tmp_path / 'template'
    (template / 'info').mkdir(parents=True)
    (template / 'info' / 'exclude').write_text('ambient template content\n')
    global_config = tmp_path / 'global.gitconfig'
    global_config.write_text(
        '[core]\n'
        f'\thooksPath = "{hooks.as_posix()}"\n'
        '[commit]\n'
        '\tgpgSign = true\n'
        '[gpg]\n'
        f'\tprogram = "{signing_program.as_posix()}"\n'
        '[init]\n'
        f'\ttemplateDir = "{template.as_posix()}"\n'
    )
    monkeypatch.setenv('HOOK_MARKER', str(hook_marker))
    monkeypatch.setenv('SIGNING_MARKER', str(signing_marker))
    configured_env = git_env({'GIT_CONFIG_GLOBAL': str(global_config)})
    repo = tmp_path / 'repo'

    subprocess.run(['git', 'init', '-q', repo], check=True, env=configured_env)
    (repo / 'tracked.txt').write_text('fixture content\n')
    subprocess.run(['git', '-C', repo, 'add', 'tracked.txt'], check=True, env=configured_env)
    completed = subprocess.run(
        [
            'git',
            '-C',
            repo,
            '-c',
            'user.name=Test User',
            '-c',
            'user.email=test@example.invalid',
            'commit',
            '-qm',
            'fixture commit',
        ],
        check=False,
        env=configured_env,
    )

    assert completed.returncode == 0
    assert (
        subprocess.run(
            ['git', '-C', repo, 'show', 'HEAD:tracked.txt'],
            check=True,
            env=configured_env,
            stdout=subprocess.PIPE,
            text=True,
        ).stdout
        == 'fixture content\n'
    )
    assert not hook_marker.exists()
    assert not signing_marker.exists()
    assert not (repo / '.git' / 'info' / 'exclude').exists()


def test_fixture_git_ignores_ambient_repository_configuration(
    make_repo: Callable[..., Path],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    decoy = make_repo('decoy', files={'marker.txt': 'decoy\n'})
    decoy_head = subprocess.run(
        ['git', '-C', decoy, 'rev-parse', 'HEAD'],
        check=True,
        env=git_env(),
        stdout=subprocess.PIPE,
        text=True,
    ).stdout
    home = tmp_path / 'home'
    home.mkdir()
    global_config = home / '.gitconfig'
    global_config.write_text('[init]\n\tdefaultBranch = ambient\n')
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('GIT_CONFIG_COUNT', '1')
    monkeypatch.setenv('GIT_CONFIG_KEY_0', 'core.abbrev')
    monkeypatch.setenv('GIT_CONFIG_VALUE_0', '5')
    monkeypatch.setenv('GIT_CONFIG_GLOBAL', str(global_config))
    monkeypatch.setenv('GIT_DIR', str(decoy / '.git'))
    monkeypatch.setenv('GIT_INDEX_FILE', str(tmp_path / 'decoy.index'))

    selected = make_repo('selected', files={'marker.txt': 'selected\n'})
    selected_initial = subprocess.run(
        ['git', '-C', selected, 'rev-parse', 'HEAD'],
        check=True,
        env=git_env(),
        stdout=subprocess.PIPE,
        text=True,
    ).stdout
    (selected / 'later.txt').write_text('later fixture content\n')
    subprocess.run(['git', '-C', selected, 'add', 'later.txt'], check=True, env=git_env())
    subprocess.run(
        [
            'git',
            '-C',
            selected,
            '-c',
            'user.name=Test User',
            '-c',
            'user.email=test@example.invalid',
            'commit',
            '-qm',
            'later fixture commit',
        ],
        check=True,
        env=git_env(),
    )
    content = subprocess.run(
        ['git', '-C', selected, 'show', 'HEAD:later.txt'],
        check=True,
        env=git_env(),
        stdout=subprocess.PIPE,
        text=True,
    ).stdout
    selected_head = subprocess.run(
        ['git', '-C', selected, 'rev-parse', 'HEAD'],
        check=True,
        env=git_env(),
        stdout=subprocess.PIPE,
        text=True,
    ).stdout
    current_decoy_head = subprocess.run(
        ['git', '-C', decoy, 'rev-parse', 'HEAD'],
        check=True,
        env=git_env(),
        stdout=subprocess.PIPE,
        text=True,
    ).stdout
    branch = subprocess.run(
        ['git', '-C', selected, 'symbolic-ref', '--short', 'HEAD'],
        check=True,
        env=git_env(),
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.strip()

    assert content == 'later fixture content\n'
    assert branch != 'ambient'
    assert selected_head != selected_initial
    assert current_decoy_head == decoy_head
    assert not (tmp_path / 'decoy.index').exists()
    assert os.environ['GIT_DIR'] == str(decoy / '.git')


def test_fixture_git_preserves_explicit_identity_and_dates(tmp_path: Path) -> None:
    repo = tmp_path / 'repo'
    identity_and_dates = {
        'GIT_AUTHOR_DATE': '2001-02-03T04:05:06+00:00',
        'GIT_AUTHOR_EMAIL': 'author@example.invalid',
        'GIT_AUTHOR_NAME': 'Fixture Author',
        'GIT_COMMITTER_DATE': '2002-03-04T05:06:07+00:00',
        'GIT_COMMITTER_EMAIL': 'committer@example.invalid',
        'GIT_COMMITTER_NAME': 'Fixture Committer',
    }
    subprocess.run(['git', 'init', '-q', repo], check=True, env=git_env())
    (repo / 'tracked.txt').write_text('fixture content\n')
    subprocess.run(['git', '-C', repo, 'add', 'tracked.txt'], check=True, env=git_env())
    subprocess.run(
        ['git', '-C', repo, 'commit', '-qm', 'fixture commit'],
        check=True,
        env=git_env(identity_and_dates),
    )

    metadata = subprocess.run(
        ['git', '-C', repo, 'show', '-s', '--format=%an%n%ae%n%aI%n%cn%n%ce%n%cI'],
        check=True,
        env=git_env(),
        stdout=subprocess.PIPE,
        text=True,
    ).stdout.splitlines()

    assert metadata == [
        'Fixture Author',
        'author@example.invalid',
        '2001-02-03T04:05:06Z',
        'Fixture Committer',
        'committer@example.invalid',
        '2002-03-04T05:06:07Z',
    ]


def test_fixture_git_supplies_identity_without_user_configuration(tmp_path: Path) -> None:
    repo = tmp_path / 'repo'
    environment = git_env()
    subprocess.run(['git', 'init', '-q', repo], check=True, env=environment)
    subprocess.run(
        [
            'git',
            '-C',
            repo,
            '-c',
            'user.useConfigOnly=true',
            'commit',
            '--allow-empty',
            '-qm',
            'fixture commit',
        ],
        check=True,
        capture_output=True,
        env=environment,
    )
    metadata = subprocess.run(
        ['git', '-C', repo, 'show', '-s', '--format=%an%n%ae%n%cn%n%ce'],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    ).stdout.splitlines()
    assert metadata == [
        'Test User',
        'test@example.invalid',
        'Test User',
        'test@example.invalid',
    ]
