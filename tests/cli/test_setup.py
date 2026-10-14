import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from pubanon import (
    _cli,
    _config,
)
from pubanon._cli import _setup as cli_setup
from pubanon._errors import PubanonError
from pubanon._remote import _host
from tests._cli import gh_calls
from tests._permissions import assert_private_mode
from tests._setup import configure

_Script = Callable[[Sequence[str | tuple[str, str]]], None]


@pytest.mark.parametrize('failure', [EOFError(), KeyboardInterrupt()])
def test_setup_cancellation_preserves_original_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: BaseException,
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    paths.global_config.unlink()

    def _cancel(_prompt: str) -> str:
        raise failure

    monkeypatch.setattr('builtins.input', _cancel)
    assert _cli.main(['setup']) == (130 if isinstance(failure, KeyboardInterrupt) else 1)
    assert not paths.global_config.exists()
    expected = (
        'Canceled.'
        if isinstance(failure, KeyboardInterrupt)
        else 'this step needs an interactive terminal'
    )
    assert expected in capsys.readouterr().err


@pytest.mark.parametrize('failure', [EOFError(), OSError(), KeyboardInterrupt()])
def test_setup_cancels_at_save_menu_without_saving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: BaseException,
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    original = paths.global_config.read_bytes()
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    prompts = ['Save this configuration? [s]ave / [e]dit / [c]ancel: ']

    def _cancel(prompt: str) -> str:
        assert prompts, f'unexpected prompt: {prompt}'
        assert prompt == prompts.pop(0)
        raise failure

    monkeypatch.setattr('builtins.input', _cancel)

    assert _cli.main(['setup', '--edit']) == (130 if isinstance(failure, KeyboardInterrupt) else 1)
    assert prompts == []
    assert paths.global_config.read_bytes() == original
    output = capsys.readouterr()
    assert (
        'Canceled.'
        if isinstance(failure, KeyboardInterrupt)
        else 'this step needs an interactive terminal'
    ) in output.err
    if isinstance(failure, KeyboardInterrupt):
        assert 'GitHub authentication remains in effect' in output.err
    else:
        assert 'rerun in an interactive terminal' in output.err
    assert 'Traceback' not in output.out + output.err


@pytest.mark.parametrize('failure', [EOFError(), KeyboardInterrupt()])
def test_setup_cancels_during_confirmation_without_saving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_gh: Path,
    capsys: pytest.CaptureFixture[str],
    failure: BaseException,
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    paths.global_config.unlink()
    fake_gh.write_text(
        json.dumps([{'args': ['api', 'user', '--jq', '.login'], 'stdout': 'public-account\n'}])
    )
    prompts = iter(
        [
            ('Publication name: ', 'Public Publisher'),
            ('Publication email: ', 'public@example.invalid'),
            ('Sensitive term: ', 'private-canary'),
            ('Sensitive term: ', ''),
            ('Default publication route (staged/direct) [staged]: ', ''),
        ]
    )

    def _cancel(prompt: str) -> str:
        try:
            expected, response = next(prompts)
        except StopIteration:
            assert prompt == 'Use this GitHub account? [Y/n] '
            raise failure from None
        assert prompt == expected
        return response

    def _no_remaining_prompts() -> None:
        try:
            remaining = next(prompts)
        except StopIteration:
            return
        pytest.fail(f'unconsumed prompt response: {remaining!r}')

    monkeypatch.setattr('builtins.input', _cancel)
    assert _cli.main(['setup']) == (130 if isinstance(failure, KeyboardInterrupt) else 1)
    _no_remaining_prompts()
    assert not paths.global_config.exists()
    assert (
        'Canceled.'
        if isinstance(failure, KeyboardInterrupt)
        else 'this step needs an interactive terminal'
    ) in capsys.readouterr().err


def test_setup_cancels_during_runtime_floor_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    original = paths.global_config.read_bytes()

    def _interrupted() -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr('pubanon._run.check_floors', _interrupted)
    assert _cli.main(['setup']) == 130
    assert paths.global_config.read_bytes() == original
    output = capsys.readouterr()
    assert 'Canceled.' in output.err
    assert 'rerun pubanon setup' in output.err
    assert 'Traceback' not in output.out + output.err


def test_setup_checks_runtime_floors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    configure(tmp_path, monkeypatch)
    seen: list[str] = []

    def _floors() -> None:
        seen.append('floors')

    def _setup(_paths: _config.Paths, *, edit: bool = False) -> None:
        assert not edit
        seen.append('setup')

    monkeypatch.setattr('pubanon._run.check_floors', _floors)
    monkeypatch.setattr(cli_setup, '_setup_locked', _setup)

    assert _cli.main(['setup']) == 0
    assert seen == ['floors', 'setup']


def test_setup_complete_rerun_preserves_bytes_and_repairs_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_gh: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    original = paths.global_config.read_bytes()
    if os.name == 'nt':
        for path in (paths.global_config, paths.config_dir):
            subprocess.run(
                ['icacls', str(path), '/grant', '*S-1-1-0:(R)'],
                check=True,
                capture_output=True,
            )
    else:
        paths.global_config.chmod(0o644)
        paths.config_dir.chmod(0o755)
    fake_gh.write_text(
        json.dumps([{'args': ['api', 'user', '--jq', '.login'], 'stdout': 'anonymous-account\n'}])
    )
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    assert _cli.main(['setup']) == 0
    assert paths.global_config.read_bytes() == original
    assert_private_mode(paths.global_config, 0o600)
    assert_private_mode(paths.config_dir, 0o700)
    output = capsys.readouterr().out
    assert 'Publication identity:' in output
    assert 'Anonymous Publisher <anonymous@example.invalid>' in output
    assert 'pubanon setup --edit' in output


def test_setup_creates_complete_owner_only_global_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_gh: Path,
    answers: _Script,
) -> None:
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
    monkeypatch.delenv('XDG_DATA_HOME', raising=False)
    fake_gh.write_text(
        json.dumps([{'args': ['api', 'user', '--jq', '.login'], 'stdout': 'pat-example\n'}])
    )
    answers(
        [
            ('Publication name: ', 'Pat Example'),
            ('Publication email: ', 'pat@example.com'),
            ('Sensitive term: ', 'private-canary'),
            ('Sensitive term: ', ''),
            ('Default publication route (staged/direct) [staged]: ', ''),
            ('Use this GitHub account? [Y/n] ', ''),
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
        ]
    )

    assert _cli.main(['setup']) == 0

    paths = _config.derive_paths({'HOME': str(tmp_path)})
    assert paths.global_config.exists()
    assert_private_mode(paths.global_config, 0o600)
    assert_private_mode(paths.config_dir, 0o700)
    config = _config.load_global(paths)
    assert config.anon_name == 'Pat Example'
    assert config.anon_email == 'pat@example.com'
    assert config.sensitive_terms == ('private-canary',)
    assert config.accounts == {'github': 'pat-example'}
    assert config.default_publication_route == 'staged'
    output = capsys.readouterr().out
    assert output
    assert not output.startswith('\n')
    assert 'Saved your configuration.' in output
    assert 'pubanon init' in output
    assert 'Identity:' in output
    assert 'Pat Example <pat@example.com>' in output
    assert 'GitHub account: pat-example' in output
    assert 'Default route:' in output
    assert 'staged (new projects only)' in output
    assert 'Sensitive terms:' in output
    assert '  1. private-canary' in output
    assert 'private-canary' in output
    assert str(paths.global_config) not in output
    assert 'Nothing was changed' not in output


def test_setup_edit_account_mismatch_can_be_corrected_before_readiness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_gh: Path,
    answers: _Script,
) -> None:
    paths, original, _ = configure(tmp_path, monkeypatch)
    fake_gh.write_text(
        json.dumps([{'args': ['api', 'user', '--jq', '.login'], 'stdout': 'correct-account\n'}])
    )
    answers(
        [
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 'e'),
            'a',
            ('Use this GitHub account? [Y/n] ', ''),
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
        ]
    )

    assert _cli.main(['setup', '--edit']) == 0

    config = _config.load_global(paths)
    assert config.anon_name == original.anon_name
    assert config.anon_email == original.anon_email
    assert config.sensitive_terms == original.sensitive_terms
    assert config.accounts == {'github': 'correct-account'}


def test_setup_edit_changes_default_without_changing_existing_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_gh: Path,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    paths, _, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    _config.save_project(paths, replace(project, publication_route='staged'))
    fake_gh.write_text(
        json.dumps([{'args': ['api', 'user', '--jq', '.login'], 'stdout': 'anonymous-account\n'}])
    )
    answers(
        [
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 'e'),
            'r',
            ('Default publication route (staged/direct) [staged]: ', 'direct'),
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
        ]
    )

    assert _cli.main(['setup', '--edit']) == 0
    assert _config.load_global(paths).default_publication_route == 'direct'
    assert _config.load_project(paths, project.name).publication_route == 'staged'
    rendered = capsys.readouterr().out
    assert 'new projects only' in rendered


def test_setup_edit_completed_config_preserves_unedited_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_gh: Path,
    answers: _Script,
) -> None:
    paths, original, _ = configure(tmp_path, monkeypatch)
    fake_gh.write_text(
        json.dumps([{'args': ['api', 'user', '--jq', '.login'], 'stdout': 'anonymous-account\n'}])
    )
    answers(
        [
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 'e'),
            't',
            ('Terms: [a]dd / [r]eplace / re[m]ove / [d]one: ', 'a'),
            ('Sensitive term: ', 'additional-canary'),
            ('Terms: [a]dd / [r]eplace / re[m]ove / [d]one: ', 'd'),
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
        ]
    )

    assert _cli.main(['setup', '--edit']) == 0
    output = capsys.readouterr().out
    assert output
    assert not output.startswith('\n')

    config = _config.load_global(paths)
    assert config.anon_name == original.anon_name
    assert config.anon_email == original.anon_email
    assert config.accounts == original.accounts
    assert config.sensitive_terms == ('Private Person', 'additional-canary')


def test_setup_edit_corrects_identity_before_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_gh: Path,
    capsys: pytest.CaptureFixture[str],
    answers: _Script,
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    fake_gh.write_text(
        json.dumps([{'args': ['api', 'user', '--jq', '.login'], 'stdout': 'anonymous-account\n'}])
    )
    answers(
        [
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 'e'),
            'i',
            ('Publication name [Anonymous Publisher]: ', 'Correct Publisher'),
            ('Publication email [anonymous@example.invalid]: ', 'correct@example.invalid'),
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
        ]
    )

    assert _cli.main(['setup', '--edit']) == 0

    config = _config.load_global(paths)
    assert config.anon_name == 'Correct Publisher'
    assert config.anon_email == 'correct@example.invalid'
    assert config.sensitive_terms == ('Private Person',)
    assert config.accounts == {'github': 'anonymous-account'}
    output = capsys.readouterr().out
    assert 'Anonymous Publisher <anonymous@example.invalid>' in output
    assert 'Correct Publisher <correct@example.invalid>' in output
    assert 'every project using this configuration' in output
    assert 'No history will be rewritten' in output


def test_setup_edit_preserves_commit_messages_when_route_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_gh: Path,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    paths.global_config.write_text(
        paths.global_config.read_text()
        + '\n[commit_message]\n'
        + 'tagged = """Release {tag}\n\nBody"""\n'
        + 'untagged = "Update published files"\n'
    )
    fake_gh.write_text(
        json.dumps([{'args': ['api', 'user', '--jq', '.login'], 'stdout': 'anonymous-account\n'}])
    )
    answers(
        [
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 'e'),
            'r',
            ('Default publication route (staged/direct) [staged]: ', 'direct'),
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
        ]
    )

    assert _cli.main(['setup', '--edit']) == 0
    saved = _config.load_global(paths)
    assert saved.default_publication_route == 'direct'
    assert saved.commit_message == _config.CommitMessageSettings(
        tagged='Release {tag}\n\nBody', untagged='Update published files'
    )
    assert 'Commit messages:' in capsys.readouterr().out


def test_setup_edit_save_preserves_direct_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_gh: Path,
    answers: _Script,
) -> None:
    paths, original, _ = configure(tmp_path, monkeypatch)
    _config.save_global(
        paths,
        replace(original, default_publication_route='direct'),
        paths.global_config.read_bytes(),
    )
    fake_gh.write_text(
        json.dumps([{'args': ['api', 'user', '--jq', '.login'], 'stdout': 'anonymous-account\n'}])
    )
    answers([('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's')])

    assert _cli.main(['setup', '--edit']) == 0
    assert _config.load_global(paths).default_publication_route == 'direct'


def test_setup_edits_global_ignored_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, original, _ = configure(tmp_path, monkeypatch)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(_host, 'authenticated_account', lambda: 'anonymous-account')
    answers = iter(
        [
            'e',
            'd',
            'a',
            'docs/private/',
            'a',
            'notes/confidential',
            'd',
            's',
        ]
    )

    def answer(_prompt: str) -> str:
        return next(answers)

    monkeypatch.setattr('builtins.input', answer)
    assert _cli.main(['setup', '--edit']) == 0
    saved = _config.load_global(paths)
    assert saved == replace(original, ignored_directories=('docs/private', 'notes/confidential'))
    assert 'Excluded directories:' in capsys.readouterr().out


def test_setup_edits_global_ignored_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, original, _ = configure(tmp_path, monkeypatch)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(_host, 'authenticated_account', lambda: 'anonymous-account')
    answers = iter(
        [
            'e',
            'f',
            'a',
            'LOCAL.md',
            'a',
            'notes/private.txt',
            'd',
            's',
        ]
    )

    def answer(_prompt: str) -> str:
        return next(answers)

    monkeypatch.setattr('builtins.input', answer)
    assert _cli.main(['setup', '--edit']) == 0
    saved = _config.load_global(paths)
    assert saved == replace(original, ignored_files=('LOCAL.md', 'notes/private.txt'))
    assert 'Excluded files:' in capsys.readouterr().out


def test_setup_interrupt_after_save_retains_configuration_and_facts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_gh: Path,
    capsys: pytest.CaptureFixture[str],
    answers: _Script,
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    paths.global_config.unlink()
    fake_gh.write_text(
        json.dumps([{'args': ['api', 'user', '--jq', '.login'], 'stdout': 'public-account\n'}])
    )
    answers(
        [
            ('Publication name: ', 'Public Publisher'),
            ('Publication email: ', 'new@example.com'),
            ('Sensitive term: ', 'private-canary'),
            ('Sensitive term: ', ''),
            ('Default publication route (staged/direct) [staged]: ', ''),
            ('Use this GitHub account? [Y/n] ', ''),
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
        ]
    )
    save = _config.save_global

    def _interrupt_after_save(
        paths: _config.Paths,
        config: _config.GlobalConfig,
        expected_bytes: bytes | None,
    ) -> None:
        save(paths, config, expected_bytes)

        def _interrupted() -> str:
            raise KeyboardInterrupt

        monkeypatch.setattr(_host, 'authenticated_account', _interrupted)

    monkeypatch.setattr(_config, 'save_global', _interrupt_after_save)
    assert _cli.main(['setup']) == 130
    assert _config.load_global(paths).anon_email == 'new@example.com'
    captured = capsys.readouterr()
    assert 'configuration was saved' in captured.err
    assert 'saved configuration was retained' in captured.err
    assert 'rerun pubanon setup' in captured.err
    assert 'ready' not in captured.out


def test_setup_missing_configuration_requires_interactive_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    paths.global_config.unlink()
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    assert _cli.main(['setup']) == 1
    assert not paths.global_config.exists()
    assert 'terminal' in capsys.readouterr().err


def test_setup_post_save_readiness_failure_retains_valid_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_gh: Path,
    capsys: pytest.CaptureFixture[str],
    answers: _Script,
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    paths.global_config.unlink()
    fake_gh.write_text(
        json.dumps(
            [
                {
                    'args': ['api', 'user', '--jq', '.login'],
                    'stdout': 'public-account\n',
                    'once': True,
                },
                {
                    'args': ['api', 'user', '--jq', '.login'],
                    'returncode': 1,
                    'stderr': 'ghp_PRIVATE_CREDENTIAL',
                },
            ]
        )
    )
    answers(
        [
            ('Publication name: ', 'Public Publisher'),
            ('Publication email: ', 'new@example.com'),
            ('Sensitive term: ', 'private-canary'),
            ('Sensitive term: ', ''),
            ('Default publication route (staged/direct) [staged]: ', ''),
            ('Use this GitHub account? [Y/n] ', ''),
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
        ]
    )
    assert _cli.main(['setup']) == 1
    assert _config.load_global(paths).anon_email == 'new@example.com'
    output = capsys.readouterr()
    assert 'configuration was saved' in output.err
    assert 'rerun pubanon setup' in output.err
    assert 'ghp_PRIVATE_CREDENTIAL' in output.err
    assert 'ready' not in output.out


def test_setup_preserves_nonblank_term_whitespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    answers: _Script,
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    paths.global_config.unlink()
    Path(os.environ['PUBANON_FAKE_GH_RESPONSES']).write_text(
        json.dumps([{'args': ['api', 'user', '--jq', '.login'], 'stdout': 'public-account\n'}])
    )
    answers(
        [
            ('Publication name: ', 'Public Publisher'),
            ('Publication email: ', 'public@example.invalid'),
            ('Sensitive term: ', ' spaced-canary '),
            ('Sensitive term: ', ''),
            ('Default publication route (staged/direct) [staged]: ', ''),
            ('Use this GitHub account? [Y/n] ', ''),
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
        ]
    )

    assert _cli.main(['setup']) == 0
    assert _config.load_global(paths).sensitive_terms == (' spaced-canary ',)


def test_setup_refuses_configured_account_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_gh: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    original = paths.global_config.read_bytes()
    fake_gh.write_text(
        json.dumps([{'args': ['api', 'user', '--jq', '.login'], 'stdout': 'other-account\n'}])
    )
    assert _cli.main(['setup']) == 1
    assert paths.global_config.read_bytes() == original
    err = capsys.readouterr().err
    assert 'Expected: anonymous-account' in err
    assert 'Found: other-account' in err


def test_setup_refuses_existing_identity_collision_with_machine_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, config, _ = configure(tmp_path, monkeypatch)
    _config.save_global(
        paths, replace(config, anon_name=str(tmp_path / 'home')), paths.global_config.read_bytes()
    )
    original = paths.global_config.read_bytes()
    assert _cli.main(['setup']) == 1
    assert paths.global_config.read_bytes() == original
    output = capsys.readouterr()
    assert 'private path' in output.err
    assert str(tmp_path) not in output.out + output.err


def test_setup_retries_colliding_new_identity_without_removing_terms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_gh: Path,
    capsys: pytest.CaptureFixture[str],
    answers: _Script,
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    paths.global_config.unlink()
    fake_gh.write_text(
        json.dumps([{'args': ['api', 'user', '--jq', '.login'], 'stdout': 'public-account\n'}])
    )
    answers(
        [
            ('Publication name: ', 'Private Person'),
            ('Publication email: ', 'public@example.com'),
            ('Sensitive term: ', 'Private Person'),
            ('Sensitive term: ', ''),
            ('Default publication route (staged/direct) [staged]: ', ''),
            ('Use this GitHub account? [Y/n] ', ''),
            'i',
            ('Publication name [Private Person]: ', 'Public Publisher'),
            ('Publication email [public@example.com]: ', 'public@example.com'),
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
        ]
    )
    assert _cli.main(['setup']) == 0
    config = _config.load_global(paths)
    assert config.anon_name == 'Public Publisher'
    assert config.sensitive_terms == ('Private Person',)
    output = capsys.readouterr()
    assert 'sensitive_terms' in output.out
    assert 'Private Person' in output.out + output.err


def test_setup_retries_invalid_new_fields_and_terms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_gh: Path,
    capsys: pytest.CaptureFixture[str],
    answers: _Script,
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    paths.global_config.unlink()
    fake_gh.write_text(
        json.dumps([{'args': ['api', 'user', '--jq', '.login'], 'stdout': 'public-account\n'}])
    )
    answers(
        [
            ('Publication name: ', 'bad<private-canary>'),
            ('Publication name: ', 'Public Publisher'),
            ('Publication email: ', 'public@example.com'),
            ('Sensitive term: ', '   '),
            ('Sensitive term: ', 'private-canary'),
            ('Sensitive term: ', ''),
            ('Default publication route (staged/direct) [staged]: ', ''),
            ('Use this GitHub account? [Y/n] ', ''),
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
        ]
    )
    assert _cli.main(['setup']) == 0
    config = _config.load_global(paths)
    assert config.anon_name == 'Public Publisher'
    assert config.sensitive_terms == ('private-canary',)
    output = capsys.readouterr()
    assert 'anon_name must be a nonempty value' in output.out
    assert 'private-canary' in output.out + output.err


@pytest.mark.parametrize(
    ('settings', 'summary'),
    [
        (None, 'built-in (set [commit_message] in config.toml to change)'),
        (
            _config.CommitMessageSettings(tagged='Release {tag}\n\nBody'),
            'tagged "Release {tag}"..., untagged built-in',
        ),
        (
            _config.CommitMessageSettings(tagged='Release \x1b{tag}'),
            r'tagged "Release \x1b{tag}", untagged built-in',
        ),
    ],
)
def test_setup_review_summarizes_commit_messages_safely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    settings: _config.CommitMessageSettings | None,
    summary: str,
) -> None:
    _, original, _ = configure(tmp_path, monkeypatch)
    cli_setup._setup_review(replace(original, commit_message=settings), original)

    output = capsys.readouterr().out
    row = next(line for line in output.splitlines() if line.startswith('Commit messages:'))
    assert row.partition(':')[2].strip() == summary
    assert '\x1b' not in output


@pytest.mark.parametrize(
    ('table', 'expected'),
    [
        (
            'tagged = "{project}"',
            'commit_message.tagged in the global configuration',
        ),
        (
            'private_text = "x"',
            'unknown commit_message key in the global configuration',
        ),
    ],
)
def test_setup_edit_reports_commit_message_errors_without_overwriting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    table: str,
    expected: str,
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    paths.global_config.write_text(
        paths.global_config.read_text() + f'\n[commit_message]\n{table}\n'
    )
    original = paths.global_config.read_bytes()

    assert _cli.main(['setup', '--edit']) == 1
    assert paths.global_config.read_bytes() == original
    output = capsys.readouterr()
    assert expected in output.err
    assert 'private_text' not in output.out + output.err
    if table.startswith('tagged'):
        assert 'Supported fields: tag, version' in output.err
    else:
        assert 'Supported fields:' not in output.err


@pytest.mark.parametrize('action', ['s', 'c'])
def test_setup_incomplete_account_requires_save_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answers: _Script,
    action: str,
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    paths.global_config.write_text(
        'anon_name = "Anonymous Publisher"\n'
        'anon_email = "anonymous@example.invalid"\n'
        'sensitive_terms = ["Private Person"]\n'
        'default_publication_route = "staged"\n'
        '\n[accounts]\n'
        'github = ""\n'
    )
    original = paths.global_config.read_bytes()
    Path(os.environ['PUBANON_FAKE_GH_RESPONSES']).write_text(
        json.dumps([{'args': ['api', 'user', '--jq', '.login'], 'stdout': 'public-account\n'}])
    )
    answers(
        [
            ('Use this GitHub account? [Y/n] ', ''),
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', action),
        ]
    )

    assert _cli.main(['setup']) == (0 if action == 's' else 130)
    output = capsys.readouterr()
    assert 'Configuration to save' in output.out
    if action == 's':
        assert _config.load_global(paths).accounts == {'github': 'public-account'}
        assert paths.global_config.read_bytes() != original
        assert 'Saved your configuration.' in output.out
    else:
        assert paths.global_config.read_bytes() == original
        assert 'ready' not in output.out


@pytest.mark.parametrize('action', ['retry', 'login', 'cancel'])
def test_setup_failed_authentication_offers_explicit_actions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    action: str,
    answers: _Script,
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    original = paths.global_config.read_bytes()
    Path(os.environ['PUBANON_FAKE_GH_RESPONSES']).write_text(
        json.dumps(
            [
                {
                    'args': ['api', 'user', '--jq', '.login'],
                    'returncode': 1,
                    'stderr': 'ghp_PRIVATE_CREDENTIAL',
                    'once': True,
                },
                {'args': ['api', 'user', '--jq', '.login'], 'stdout': 'anonymous-account\n'},
            ]
        )
    )
    answers([('Authentication: [r]etry / [l]ogin / [c]ancel: ', action[0])])
    logins: list[bool] = []
    before_login: list[str] = []

    def _login() -> None:
        before_login.append(capsys.readouterr().out)
        logins.append(True)

    monkeypatch.setattr(_host, 'login', _login)
    assert _cli.main(['setup']) == (130 if action == 'cancel' else 0)
    assert paths.global_config.read_bytes() == original
    output = capsys.readouterr()
    combined = ''.join(before_login) + output.out + output.err
    assert 'ghp_PRIVATE_CREDENTIAL' in combined
    assert 'could not be verified' in combined
    assert logins == ([True] if action == 'login' else [])
    if action == 'login':
        assert 'Handing this terminal to gh auth login.' in before_login[0]
        assert 'Completed authentication stays in effect' in before_login[0]
    assert len(gh_calls(tmp_path)) >= (1 if action == 'cancel' else 2)


@pytest.mark.parametrize('login_result', ['failed', 'still-unverified', 'wrong-account'])
def test_setup_login_never_saves_without_verified_matching_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    login_result: str,
    answers: _Script,
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    original = paths.global_config.read_bytes()
    responses = [
        {'args': ['api', 'user', '--jq', '.login'], 'returncode': 1, 'once': True},
        {'args': ['api', 'user', '--jq', '.login'], 'returncode': 1}
        if login_result == 'still-unverified'
        else {'args': ['api', 'user', '--jq', '.login'], 'stdout': 'another-account\n'},
    ]
    Path(os.environ['PUBANON_FAKE_GH_RESPONSES']).write_text(json.dumps(responses))
    answers(
        [
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
            ('Authentication: [r]etry / [l]ogin / [c]ancel: ', 'l'),
            *(
                [('Authentication: [r]etry / [l]ogin / [c]ancel: ', 'c')]
                if login_result == 'still-unverified'
                else []
            ),
            *(
                [('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 'c')]
                if login_result == 'wrong-account'
                else []
            ),
        ]
    )
    logins: list[bool] = []

    def _login() -> None:
        logins.append(True)
        if login_result == 'failed':
            raise PubanonError('GitHub login did not complete')

    monkeypatch.setattr(_host, 'login', _login)
    assert _cli.main(['setup', '--edit']) == (
        130 if login_result in {'still-unverified', 'wrong-account'} else 1
    )
    assert paths.global_config.read_bytes() == original
    assert logins == [True]
    output = capsys.readouterr()
    assert 'ready' not in output.out
    if login_result in {'still-unverified', 'wrong-account'}:
        assert len(gh_calls(tmp_path)) == 2


@pytest.mark.parametrize(
    ('content', 'diagnostic'),
    [
        ('anon_name = "private-canary', 'malformed'),
        ('"private-canary" = "secret"', 'unknown global configuration key'),
        (
            (
                'anon_name = "bad<private-canary>"\nanon_email = "public@example.com"\n'
                'sensitive_terms = ["private-canary"]\n'
                'default_publication_route = "staged"\n[accounts]\ngithub = "account"'
            ),
            'anon_name',
        ),
    ],
)
def test_setup_refuses_invalid_existing_configuration_without_overwriting_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    content: str,
    diagnostic: str,
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    paths.global_config.write_text(content)
    assert _cli.main(['setup']) == 1
    assert paths.global_config.read_text() == content
    output = capsys.readouterr()
    assert diagnostic in output.err


def test_setup_save_decline_leaves_original_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_gh: Path,
    answers: _Script,
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    original = paths.global_config.read_bytes()
    fake_gh.write_text(
        json.dumps([{'args': ['api', 'user', '--jq', '.login'], 'stdout': 'other-account\n'}])
    )
    answers(
        [
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 'e'),
            'a',
            ('Use this GitHub account? [Y/n] ', ''),
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 'c'),
        ]
    )
    assert _cli.main(['setup', '--edit']) == 130
    assert paths.global_config.read_bytes() == original


def test_setup_saved_account_mismatch_preserves_exact_review_details(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths, config, _ = configure(tmp_path, monkeypatch)

    def _account() -> str:
        return 'private-account-canary\x1b\\path'

    monkeypatch.setattr(_host, 'authenticated_account', _account)
    with pytest.raises(PubanonError) as raised:
        cli_setup._setup_verify_saved(paths, config)
    assert raised.value.expected == config.accounts['github']
    assert raised.value.actual == r'private-account-canary\x1b\\path'
    assert 'configuration was saved' in raised.value.render()
    assert 'private-account-canary' in raised.value.render()


def test_setup_shows_terms_while_editing_and_before_saving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_gh: Path,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    paths.global_config.unlink()
    fake_gh.write_text(
        json.dumps([{'args': ['api', 'user', '--jq', '.login'], 'stdout': 'public-account\n'}])
    )
    answers(
        [
            ('Publication name: ', 'Public Publisher'),
            ('Publication email: ', 'public@example.invalid'),
            ('Sensitive term: ', 'first-canary'),
            ('Sensitive term: ', ''),
            ('Default publication route (staged/direct) [staged]: ', ''),
            ('Use this GitHub account? [Y/n] ', ''),
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 'e'),
            't',
            ('Terms: [a]dd / [r]eplace / re[m]ove / [d]one: ', 'd'),
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
        ]
    )
    assert _cli.main(['setup']) == 0
    out = capsys.readouterr().out
    assert '  1. first-canary' in out
    assert 'Sensitive terms:' in out
    assert _config.load_global(paths).sensitive_terms == ('first-canary',)


@pytest.mark.parametrize('action', ['add', 'replace'])
def test_setup_term_edit_retries_blank_before_valid_value(
    action: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_gh: Path,
    answers: _Script,
) -> None:
    paths, config, _ = configure(tmp_path, monkeypatch)
    _config.save_global(paths, config, paths.global_config.read_bytes())
    fake_gh.write_text(
        json.dumps([{'args': ['api', 'user', '--jq', '.login'], 'stdout': 'anonymous-account\n'}])
    )
    selection = (
        [('Terms: [a]dd / [r]eplace / re[m]ove / [d]one: ', 'a')]
        if action == 'add'
        else [
            ('Terms: [a]dd / [r]eplace / re[m]ove / [d]one: ', 'r'),
            ('Term number: ', '1'),
        ]
    )
    answers(
        [
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 'e'),
            't',
            *selection,
            ('Sensitive term: ', ''),
            ('Sensitive term: ', 'replacement-canary'),
            ('Terms: [a]dd / [r]eplace / re[m]ove / [d]one: ', 'd'),
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
        ]
    )

    assert _cli.main(['setup', '--edit']) == 0
    expected = (
        ('Private Person', 'replacement-canary') if action == 'add' else ('replacement-canary',)
    )
    assert _config.load_global(paths).sensitive_terms == expected


def test_setup_term_review_keeps_ids_after_earlier_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_gh: Path,
    answers: _Script,
) -> None:
    paths, config, _ = configure(tmp_path, monkeypatch)
    _config.save_global(
        paths,
        replace(config, sensitive_terms=('first-canary', 'second-canary', 'third-canary')),
        paths.global_config.read_bytes(),
    )
    fake_gh.write_text(
        json.dumps([{'args': ['api', 'user', '--jq', '.login'], 'stdout': 'anonymous-account\n'}])
    )
    answers(
        [
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 'e'),
            't',
            ('Terms: [a]dd / [r]eplace / re[m]ove / [d]one: ', 'm'),
            ('Term number: ', '1'),
            ('Terms: [a]dd / [r]eplace / re[m]ove / [d]one: ', 'a'),
            ('Sensitive term: ', 'fourth-canary'),
            ('Terms: [a]dd / [r]eplace / re[m]ove / [d]one: ', 'm'),
            ('Term number: ', '2'),
            ('Terms: [a]dd / [r]eplace / re[m]ove / [d]one: ', 'd'),
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
        ]
    )

    assert _cli.main(['setup', '--edit']) == 0
    assert _config.load_global(paths).sensitive_terms == ('third-canary', 'fourth-canary')


def test_setup_term_review_lists_terms_by_number(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_gh: Path,
    capsys: pytest.CaptureFixture[str],
    answers: _Script,
) -> None:
    paths, config, _ = configure(tmp_path, monkeypatch)
    _config.save_global(
        paths,
        replace(config, sensitive_terms=('first-canary', 'second-canary')),
        paths.global_config.read_bytes(),
    )
    fake_gh.write_text(
        json.dumps([{'args': ['api', 'user', '--jq', '.login'], 'stdout': 'anonymous-account\n'}])
    )
    answers(
        [
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 'e'),
            't',
            ('Terms: [a]dd / [r]eplace / re[m]ove / [d]one: ', 'r'),
            ('Term number: ', '1'),
            ('Sensitive term: ', 'replacement-canary'),
            ('Terms: [a]dd / [r]eplace / re[m]ove / [d]one: ', 'm'),
            ('Term number: ', '2'),
            ('Terms: [a]dd / [r]eplace / re[m]ove / [d]one: ', 'd'),
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
        ]
    )

    assert _cli.main(['setup', '--edit']) == 0
    assert _config.load_global(paths).sensitive_terms == ('replacement-canary',)
    output = capsys.readouterr().out
    assert '  1. first-canary' in output
    assert '  2. second-canary' in output
    for term in ('first-canary', 'second-canary', 'replacement-canary'):
        assert term in output


def test_setup_writes_nothing_until_save(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_gh: Path,
    answers: _Script,
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    paths.global_config.unlink()
    fake_gh.write_text(
        json.dumps([{'args': ['api', 'user', '--jq', '.login'], 'stdout': 'public-account\n'}])
    )
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    answers(
        [
            ('Publication name: ', 'Public Publisher'),
            ('Publication email: ', 'public@example.invalid'),
            ('Sensitive term: ', 'first-canary'),
            ('Sensitive term: ', ''),
            ('Default publication route (staged/direct) [staged]: ', ''),
            ('Use this GitHub account? [Y/n] ', ''),
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 'c'),
        ]
    )
    assert _cli.main(['setup']) == 130
    assert not paths.global_config.exists()
