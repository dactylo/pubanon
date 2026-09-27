import builtins
import os
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import cast

import pytest

from pubanon import _config, _terminal
from tests._git import git_env
from tests._setup import configure

_Script = Callable[[Sequence[str | tuple[str, str]]], None]

_FAKE_GH_SCRIPT = """#!/usr/bin/env python
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", newline="\\n")
sys.stderr.reconfigure(encoding="utf-8", newline="\\n")

args = sys.argv[1:]
stdin = sys.stdin.read() if args[:2] == ["auth", "git-credential"] else ""
calls_path = Path(os.environ["PUBANON_FAKE_GH_CALLS"])
call = {"args": args, "stdin": stdin}
if os.environ.get("PUBANON_FAKE_GH_RECORD_HOST") == "1":
    call["gh_host"] = os.environ.get("GH_HOST")
    call["gh_config_dir"] = os.environ.get("GH_CONFIG_DIR")
with calls_path.open("a") as calls:
    calls.write(json.dumps(call) + "\\n")
responses_path = Path(os.environ["PUBANON_FAKE_GH_RESPONSES"])
responses = json.loads(responses_path.read_text())
for index, response in enumerate(responses):
    if response["args"] == args:
        if response.get("once") is True:
            responses.pop(index)
            responses_path.write_text(json.dumps(responses))
        sys.stdout.write(response.get("stdout", ""))
        sys.stderr.write(response.get("stderr", ""))
        raise SystemExit(response.get("returncode", 0))
sys.stderr.write("unexpected fake gh invocation: " + json.dumps(args) + "\\n")
raise SystemExit(127)
"""


@pytest.fixture(autouse=True)
def _isolate_user_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('USERPROFILE', str(tmp_path / 'home'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.setenv('XDG_DATA_HOME', str(tmp_path / 'data'))


@pytest.fixture(autouse=True)
def _refuse_unscripted_input(monkeypatch: pytest.MonkeyPatch) -> None:
    def _refuse(prompt: object = '') -> str:
        pytest.fail(f'unscripted prompt: {prompt!r}')

    def _scripted_key(prompt: str) -> str:
        return builtins.input(prompt)

    monkeypatch.setattr('builtins.input', _refuse)
    monkeypatch.setattr(_terminal, '_read_key', _scripted_key)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)


@pytest.fixture
def answers(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Script]:
    remaining: list[str | tuple[str, str]] = []

    def _reply(prompt: object = '') -> str:
        assert remaining, f'unexpected prompt: {prompt!r}'
        reply = remaining[0]
        if isinstance(reply, str):
            response = reply
        else:
            expected, response = reply
            assert prompt == expected, f'expected prompt {expected!r}, got {prompt!r}'
        del remaining[0]
        return response

    def _script(replies: Sequence[str | tuple[str, str]]) -> None:
        remaining.extend(replies)
        monkeypatch.setattr('builtins.input', _reply)

    yield _script
    assert remaining == [], f'unconsumed answers: {remaining!r}'


@pytest.fixture(autouse=True)
def fake_gh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / 'fake-bin'
    bin_dir.mkdir()
    executable = bin_dir / 'gh'
    executable.write_text(_FAKE_GH_SCRIPT, encoding='utf-8', newline='\n')
    executable.chmod(0o755)
    if os.name == 'nt':
        (bin_dir / 'gh.cmd').write_text(
            f'@echo off\n"{sys.executable}" "{executable}" %*\nexit /b %errorlevel%\n'
        )

        original_popen = cast(
            'Callable[..., subprocess.Popen[bytes] | subprocess.Popen[str]]', subprocess.Popen
        )

        def _launch(
            *args: object, **kwargs: object
        ) -> subprocess.Popen[bytes] | subprocess.Popen[str]:
            command = args[0] if args else kwargs.get('args')
            if isinstance(command, (list, tuple)):
                parts = cast('Sequence[str]', command)
                if parts and parts[0] == 'gh':
                    replacement = [sys.executable, str(executable), *parts[1:]]
                    args = (replacement, *args[1:]) if args else args
                    if not args:
                        kwargs['args'] = replacement
            return original_popen(*args, **kwargs)

        monkeypatch.setattr(subprocess, 'Popen', _launch)
    responses = tmp_path / 'fake-gh-responses.json'
    responses.write_text('[]\n')
    monkeypatch.setenv('PUBANON_FAKE_GH_CALLS', str(tmp_path / 'fake-gh-calls.jsonl'))
    monkeypatch.setenv('PUBANON_FAKE_GH_RESPONSES', str(responses))
    monkeypatch.setenv('PATH', f'{bin_dir}{os.pathsep}{os.environ["PATH"]}')
    return responses


@pytest.fixture
def make_repo(tmp_path: Path) -> Callable[..., Path]:
    def _make(
        name: str = 'repo',
        *,
        files: Mapping[str, bytes | str] | None = None,
        symlinks: Mapping[str, str] | None = None,
    ) -> Path:
        repo = tmp_path / name
        repo.mkdir()
        env = git_env()
        subprocess.run(['git', 'init', '-q', repo], check=True, env=env)
        for relative, data in (files or {}).items():
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(data, bytes):
                path.write_bytes(data)
            else:
                path.write_text(data, encoding='utf-8', newline='\n')
        for relative, target in (symlinks or {}).items():
            path = repo / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(target)
        subprocess.run(['git', '-C', repo, 'add', '-A'], check=True, env=env)
        subprocess.run(
            [
                'git',
                '-C',
                repo,
                '-c',
                'user.name=Test User',
                '-c',
                'user.email=test@example.invalid',
                'commit',
                '--allow-empty',
                '-qm',
                'initial',
                '-m',
                'Create repository history for behavior tests.',
            ],
            check=True,
            env=env,
        )
        return repo

    return _make


@pytest.fixture
def configured_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    make_repo: Callable[..., Path],
) -> tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig]:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    return paths, global_config, project
