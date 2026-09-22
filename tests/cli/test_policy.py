import json
import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from pubanon import (
    _cli,
    _config,
    _policy,
)
from pubanon._cli._context import ProjectContext
from pubanon._cli._selection import _policy_decision, apply_policy_offers
from pubanon._errors import OperationCancelled, PubanonError
from pubanon._snapshots import _source
from pubanon._terminal import escape
from tests._permissions import assert_private_mode
from tests._setup import configure


def test_adjust_reoffers_unmatched_remainder(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'pkg/a.py': 'a', 'pkg/b.py': 'b'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    project = replace(project, allow=())
    ctx = ProjectContext(paths, global_config, project)
    pinned = _source.pin(source, 'HEAD')
    snapshot = _source.read_snapshot(source, pinned.commit)
    answers = iter(['customize', 'pkg/a.py', 'include'])  # narrow to one file, then accept rest

    def _answer(_prompt: str) -> str:
        return next(answers)

    monkeypatch.setattr('builtins.input', _answer)
    updated = apply_policy_offers(ctx, snapshot, persist=False, yes=False)
    membership = _policy.evaluate_membership(
        ['pkg/a.py', 'pkg/b.py'], updated.project.allow, updated.project.deny
    )
    assert membership.unmatched == []


def test_first_policy_pass_owns_new_credential_like_paths(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n', 'sub/KEY.PEM': 'k'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    ctx = ProjectContext(paths, global_config, project)
    pinned = _source.pin(source, 'HEAD')
    snapshot = _source.read_snapshot(source, pinned.commit)
    answers = iter(['exclude'])

    def _answer(_prompt: str) -> str:
        return next(answers)

    monkeypatch.setattr('builtins.input', _answer)
    updated = apply_policy_offers(ctx, snapshot, persist=False, yes=False)
    membership = _policy.evaluate_membership(
        [entry.path for entry in snapshot], updated.project.allow, updated.project.deny
    )
    assert _policy.secret_like(membership.allowed, updated.project.secret_exceptions) == []


def test_guided_policy_exposes_nested_internal_groups(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(
        files={
            'docs/guide.md': 'guide\n',
            'docs/internal/notes.md': 'internal\n',
            'docs/superpowers/design.md': 'design\n',
            '.agents/instructions.md': 'agent instructions\n',
        }
    )
    paths, _global_config, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    group_decisions = iter(['e', 'i', 'e', 'i'])

    selection_actions = iter(['r', 'c'])

    def _answer(prompt: str) -> str:
        if prompt == 'Use this selection? [c]ontinue / [r]eview / [b]ack / [q]uit: ':
            return next(selection_actions)
        if prompt == (
            'Include these files? [i]nclude / [e]xclude / [c]ustomize / [b]ack / [q]uit: '
        ):
            return next(group_decisions)
        if prompt == 'Save this configuration? [s]ave / [e]dit / [c]ancel: ':
            return 's'
        if prompt in {
            'GitHub owner [anonymous-account]: ',
            'GitHub repository [demo]: ',
            'Repository visibility (private/public) [private]: ',
            'Required CI checks (comma separated, blank for none): ',
            'Repository description (blank for none): ',
            'Enable issues? [Y/n] ',
            'Enable wiki? [y/N] ',
            'Enable projects? [y/N] ',
        }:
            return ''
        pytest.fail(f'unexpected init prompt: {prompt}')

    monkeypatch.setattr('builtins.input', _answer)

    assert _cli.main(['init', '--project', 'demo']) == 0

    output = capsys.readouterr().out
    assert 'docs/internal/' in output
    assert 'docs/superpowers/' in output
    assert '.agents/' in output
    project = _config.load_project(paths, 'demo')
    membership = _policy.evaluate_membership(
        [
            'docs/guide.md',
            'docs/internal/notes.md',
            'docs/superpowers/design.md',
            '.agents/instructions.md',
        ],
        project.allow,
        project.deny,
    )
    assert membership.allowed == ['docs/guide.md', 'docs/superpowers/design.md']
    assert membership.denied == ['docs/internal/notes.md', '.agents/instructions.md']
    assert membership.unmatched == []


def test_new_path_batch_json_preserves_controls_and_terminal_output_escapes_them(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    raw_path = 'line\n\x1bbreak.txt' if os.name != 'nt' else 'line\u2028break.txt'
    source = make_repo(files={raw_path: 'public\n'})
    unsafe_root = tmp_path / ('data\\\x1broot' if os.name != 'nt' else 'data\u2028root')
    paths, _global_config, project = configure(unsafe_root, monkeypatch, source)
    assert project is not None
    monkeypatch.chdir(source)

    assert _cli.main(['prepare', '--acknowledge-new-paths']) == 0

    batch_path = paths.data_dir / 'projects' / project.name / 'new-paths.json'
    encoded = batch_path.read_text(encoding='utf-8')
    assert json.loads(encoded) == [raw_path]
    if os.name != 'nt':
        assert raw_path not in encoded
        assert r'line\n\u001bbreak.txt' in encoded
    assert_private_mode(batch_path, 0o600)
    output = capsys.readouterr().out
    assert raw_path not in output
    assert escape(raw_path) in output
    assert str(batch_path) not in output
    assert escape(str(batch_path)) in output


def test_policy_decision_accepts_single_key_and_renders_matches(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompts: list[str] = []

    def _answer(prompt: str) -> str:
        prompts.append(prompt)
        return 'i'

    monkeypatch.setattr('builtins.input', _answer)

    assert _policy_decision('src/**', ['src/a.py', 'src/b.py']) == (
        'publish',
        'src/**',
    )
    assert capsys.readouterr().out == (
        'Files to include or exclude:\n  src/a.py\n  src/b.py\n'
        'Selection pattern: src/** (also applies to future matching files).\n'
    )
    assert prompts == ['Include these files? [i]nclude / [e]xclude / [c]ustomize / [q]uit: ']


def test_policy_decision_quit_refuses_run(monkeypatch: pytest.MonkeyPatch) -> None:
    def _quit_decision(_prompt: str) -> str:
        return 'q'

    monkeypatch.setattr('builtins.input', _quit_decision)

    with pytest.raises(OperationCancelled, match='operation cancelled') as error:
        _policy_decision('**', ['README.md'])
    assert error.value.fact == 'nothing was saved'


def test_policy_offers_explain_selection_rules_once_by_default(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'KEY.PEM': 'key'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    ctx = ProjectContext(paths, global_config, project)
    pinned = _source.pin(source, 'HEAD')
    snapshot = _source.read_snapshot(source, pinned.commit)

    def _exclude_decision(_prompt: str) -> str:
        return 'e'

    monkeypatch.setattr('builtins.input', _exclude_decision)

    updated = apply_policy_offers(
        ctx,
        snapshot,
        persist=False,
        yes=False,
    )

    assert updated.project.deny == ('KEY.PEM',)
    assert (
        capsys.readouterr().out.count(
            'These decisions configure selection rules. Nothing is published now.\n'
        )
        == 1
    )


def test_policy_offers_yes_still_refuses_credential_warning_without_input(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'KEY.PEM': 'key'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    ctx = ProjectContext(paths, global_config, project)
    pinned = _source.pin(source, 'HEAD')
    snapshot = _source.read_snapshot(source, pinned.commit)

    def _unexpected_input(_prompt: str) -> str:
        pytest.fail('--yes requested a credential-like path decision')

    monkeypatch.setattr('builtins.input', _unexpected_input)

    with pytest.raises(PubanonError, match='--yes cannot answer credential-like path warnings'):
        apply_policy_offers(ctx, snapshot, persist=False, yes=True)
