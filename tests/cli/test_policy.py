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
from pubanon._cli import _selection
from pubanon._cli._context import ProjectContext
from pubanon._cli._selection import (
    _apply_policy_offers,
    policy_decision,
    sanitized_snapshot,
    show_selection,
)
from pubanon._errors import OperationCanceled, PubanonError
from pubanon._snapshots import _source
from pubanon._terminal import escape, verbose_context
from tests._permissions import assert_private_mode
from tests._setup import configure


def test_adjust_reoffers_unmatched_remainder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    project = replace(project, allow=())
    ctx = ProjectContext(paths, global_config, project)
    snapshot = (
        _source.Entry('pkg/a.py', 0o100644, 'blob', b'a'),
        _source.Entry('pkg/b.py', 0o100644, 'blob', b'b'),
    )
    answers = iter(['c', 'pkg/a.py', 'i'])  # narrow to one file, then accept rest

    def _answer(_prompt: str) -> str:
        return next(answers)

    monkeypatch.setattr('builtins.input', _answer)
    updated = _apply_policy_offers(ctx, snapshot, persist=False, yes=False)
    membership = _policy.evaluate_membership(
        ['pkg/a.py', 'pkg/b.py'], updated.project.allow, updated.project.deny
    )
    assert membership.unmatched == []


def test_first_policy_pass_owns_new_credential_like_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    ctx = ProjectContext(paths, global_config, project)
    snapshot = (
        _source.Entry('README.md', 0o100644, 'blob', b'public\n'),
        _source.Entry('sub/KEY.PEM', 0o100644, 'blob', b'k'),
    )
    answers = iter(['e'])

    def _answer(_prompt: str) -> str:
        return next(answers)

    monkeypatch.setattr('builtins.input', _answer)
    updated = _apply_policy_offers(ctx, snapshot, persist=False, yes=False)
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
            'docs/private/notes.md': 'internal\n',
            'docs/worknotes/design.md': 'design\n',
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
            'Publication route (staged/direct) [staged]: ',
            'Create local tags marking the source of each publication? [y/N] ',
            'Required CI checks (comma separated, blank for none): ',
            'Create a release page for each published version? [y/N] ',
            'Repository description (blank for none): ',
            'Enable issues? [Y/n] ',
            'Enable wiki? [y/N] ',
            'Enable projects? [y/N] ',
            'Enable pull requests? [y/N] ',
            'Enable discussions? [y/N] ',
        }:
            return ''
        pytest.fail(f'unexpected init prompt: {prompt}')

    monkeypatch.setattr('builtins.input', _answer)

    assert _cli.main(['init', '--project', 'demo']) == 0

    output = capsys.readouterr().out
    assert 'docs/private/' in output
    assert 'docs/worknotes/' in output
    assert '.agents/' in output
    project = _config.load_project(paths, 'demo')
    membership = _policy.evaluate_membership(
        [
            'docs/guide.md',
            'docs/private/notes.md',
            'docs/worknotes/design.md',
            '.agents/instructions.md',
        ],
        project.allow,
        project.deny,
    )
    assert membership.allowed == ['docs/guide.md', 'docs/worknotes/design.md']
    assert membership.denied == ['docs/private/notes.md', '.agents/instructions.md']
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
    paths, global_config, project = configure(unsafe_root, monkeypatch, source)
    assert project is not None
    monkeypatch.chdir(source)

    assert _cli.main(['prepare', '--acknowledge-new-paths', '--yes', '--verbose']) == 0

    batch_path = paths.data_dir / 'projects' / project.name / 'new-paths.json'
    encoded = batch_path.read_text(encoding='utf-8')
    assert json.loads(encoded) == [raw_path]
    if os.name != 'nt':
        assert raw_path not in encoded
        assert r'line\n\u001bbreak.txt' in encoded
    assert_private_mode(batch_path, 0o600)
    output = capsys.readouterr().out
    assert raw_path not in output
    assert escape(raw_path) not in output
    assert str(batch_path) not in output
    assert escape((batch_path).as_posix()) in output

    _selection.show_new_paths(ProjectContext(paths, global_config, project), [raw_path])
    later_release = capsys.readouterr().out
    assert raw_path not in later_release
    assert escape(raw_path) in later_release


def test_policy_decision_accepts_single_key_and_renders_matches(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompts: list[str] = []

    def _answer(prompt: str) -> str:
        prompts.append(prompt)
        return 'i'

    monkeypatch.setattr('builtins.input', _answer)

    assert policy_decision('src/**', ['src/a.py', 'src/b.py']) == (
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

    with pytest.raises(OperationCanceled, match=r'Canceled\.') as error:
        policy_decision('**', ['README.md'])
    assert 'Nothing was saved.' in str(error.value)


def test_policy_offers_explain_selection_rules_once_by_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    ctx = ProjectContext(paths, global_config, project)
    snapshot = (_source.Entry('KEY.PEM', 0o100644, 'blob', b'key'),)

    def _exclude_decision(_prompt: str) -> str:
        return 'e'

    monkeypatch.setattr('builtins.input', _exclude_decision)

    updated = _apply_policy_offers(
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
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    ctx = ProjectContext(paths, global_config, project)
    snapshot = (_source.Entry('KEY.PEM', 0o100644, 'blob', b'key'),)

    def _unexpected_input(_prompt: str) -> str:
        pytest.fail('--yes requested a credential-like path decision')

    monkeypatch.setattr('builtins.input', _unexpected_input)

    with pytest.raises(PubanonError, match='--yes cannot answer credential-like path warnings'):
        _apply_policy_offers(ctx, snapshot, persist=False, yes=True)


@pytest.mark.parametrize('decision', ['continue', 'quit'])
def test_selection_review_preserves_saved_choices(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    paths, config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    project = replace(project, allow=('README.md', 'future/**'), deny=('notes/**',))
    _config.save_project(paths, project)
    ctx = ProjectContext(paths, config, project)
    snapshot = (
        _source.Entry('README.md', 0o100644, 'blob', b'public'),
        _source.Entry('notes/draft.md', 0o100644, 'blob', b'draft'),
    )
    replies = iter([decision[0]])

    def _answer(_prompt: str) -> str:
        return next(replies)

    monkeypatch.setattr('builtins.input', _answer)
    if decision == 'quit':
        with pytest.raises(OperationCanceled):
            _selection.review_selection(ctx, snapshot, yes=False)
    else:
        assert _selection.review_selection(ctx, snapshot, yes=False).project == project
    assert _config.load_project(paths, project.name) == project
    assert next(replies, None) is None


@pytest.mark.parametrize('decision', ['continue', 'quit'])
def test_selection_review_applies_edited_groups_only_on_acceptance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    decision: str,
) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    paths, config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    config = replace(config, ignored_directories=('private',))
    ctx = ProjectContext(paths, config, project)
    snapshot = tuple(
        _source.Entry(path, 0o100644, 'blob', b'content')
        for path in ('README.md', 'notes/draft.md', 'private/key.pem')
    )
    replies = iter(['r', 'e', 'b', 'c', 'README.md', 'e', decision[0]])

    def _answer(_prompt: str) -> str:
        return next(replies)

    monkeypatch.setattr('builtins.input', _answer)
    if decision == 'quit':
        with pytest.raises(OperationCanceled):
            _selection.review_selection(ctx, snapshot, yes=False)
        assert _config.load_project(paths, project.name) == project
    else:
        updated = _selection.review_selection(ctx, snapshot, yes=False)
        assert [entry.path for entry in sanitized_snapshot(updated, snapshot)] == ['README.md']
        assert _config.load_project(paths, project.name) == updated.project
        assert updated.project.settings == project.settings
        assert updated.project.required_checks == project.required_checks
    assert next(replies, None) is None


@pytest.mark.parametrize('allow', [('**',), ('README.md',)])
def test_global_directory_exclusions_override_includes_and_skip_proposals(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    allow: tuple[str, ...],
) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    global_config = replace(
        global_config, ignored_directories=('DOCS/Private', 'Notes/CONFIDENTIAL')
    )
    project = replace(project, allow=(*allow, 'docs/private-copy/**'))
    ctx = ProjectContext(paths, global_config, project)
    snapshot = (
        _source.Entry('README.md', 0o100644, 'blob', b'public'),
        _source.Entry('docs/private-copy/guide.md', 0o100644, 'blob', b'public'),
        _source.Entry('docs/private/private.key', 0o100644, 'blob', b'private'),
        _source.Entry('notes/confidential/nested/notes.md', 0o100644, 'blob', b'private'),
    )
    updated = _apply_policy_offers(ctx, snapshot, persist=False, yes=True)
    assert [entry.path for entry in sanitized_snapshot(updated, snapshot)] == [
        'README.md',
        'docs/private-copy/guide.md',
    ]
    assert updated.project.deny == ()
    with verbose_context(enabled=True):
        show_selection(updated, [entry.path for entry in snapshot])
    assert 'globally ignored directory' in capsys.readouterr().out
