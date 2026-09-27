import io
import os
import shlex
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest

from pubanon._cli import _arguments

if os.name != 'nt':
    import pty
    import select
    import signal
    import termios

from pubanon import _terminal
from pubanon._errors import OperationCanceled, PubanonError
from pubanon._remote._host import CheckVerdict

_Script = Callable[[Sequence[tuple[str, str]]], None]


def test_ask_accepts_one_pass_choices(answers: _Script) -> None:
    answers([('Continue? [y]es / [n]o: ', 'y')])

    assert _terminal.ask('Continue?', choices=(choice for choice in ('yes', 'no'))) == 'yes'


def test_ask_blank_takes_default(answers: _Script) -> None:
    answers([('GitHub owner [acct]: ', '')])
    assert _terminal.ask('GitHub owner', default='acct') == 'acct'


def test_ask_choices_add_colon_to_plain_question(answers: _Script) -> None:
    answers([('Terms: [a]dd / [r]eplace / re[m]ove / [d]one: ', 'REMOVE')])
    assert _terminal.ask('Terms', choices=['add', 'replace', 're[m]ove', 'done']) == 'remove'


def test_ask_choices_blank_takes_default(answers: _Script) -> None:
    answers([('Description: [k]eep / [r]eplace / [c]lear [keep]: ', '')])
    assert (
        _terminal.ask('Description', choices=['keep', 'replace', 'clear'], default='keep')
        == 'keep'
    )


def test_ask_choices_honor_marked_letter_and_default(answers: _Script) -> None:
    answers([('Terms: [a]dd / re[m]ove / [d]one [done]: ', 'm')])
    assert _terminal.ask('Terms:', choices=['add', 're[m]ove', 'done'], default='done') == 'remove'


def test_ask_choices_reask_on_invalid_answer(
    answers: _Script, capsys: pytest.CaptureFixture[str]
) -> None:
    prompt = 'Save? [s]ave / [e]dit / [c]ancel: '
    answers([(prompt, 'x'), (prompt, ''), (prompt, 'c')])
    assert _terminal.ask('Save?', choices=['save', 'edit', 'cancel']) == 'cancel'
    assert capsys.readouterr().out == 'Choose save, edit, or cancel.\n' * 2


def test_ask_empty_default_allows_blank(answers: _Script) -> None:
    answers([('Repository description: ', '')])
    assert _terminal.ask('Repository description', default='') == ''


def test_ask_escapes_displayed_default(answers: _Script) -> None:
    answers([('Name [a\\x1b[31mb\\\\c]: ', '')])
    assert _terminal.ask('Name', default='a\x1b[31mb\\c') == 'a\x1b[31mb\\c'


def test_ask_reasks_beyond_three_blank_answers(
    answers: _Script, capsys: pytest.CaptureFixture[str]
) -> None:
    answers([('project: ', '')] * 4 + [('project: ', 'demo')])
    assert _terminal.ask('project') == 'demo'
    assert capsys.readouterr().out == 'A value is required.\n' * 4


def test_ask_reasks_on_blank_without_default(
    answers: _Script, capsys: pytest.CaptureFixture[str]
) -> None:
    answers([('Publication name: ', ''), ('Publication name: ', 'Alias')])
    assert _terminal.ask('Publication name') == 'Alias'
    assert capsys.readouterr().out == 'A value is required.\n'


def test_ask_rejects_ambiguous_abbreviations() -> None:
    with pytest.raises(ValueError, match='ambiguous choice letters'):
        _terminal.ask('Action', choices=['replace', 'remove'])


def test_ask_returns_free_text_with_whitespace_intact(answers: _Script) -> None:
    answers([('Publication name: ', ' Some Alias ')])
    assert _terminal.ask('Publication name') == ' Some Alias '


def test_ask_two_choices_render_retry_hint(
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    prompt = 'Whitespace: [k]eep / [r]e-enter: '
    answers([(prompt, 'x'), (prompt, 'keep')])
    assert _terminal.ask('Whitespace:', choices=['keep', 're-enter']) == 'keep'
    assert capsys.readouterr().out == 'Choose keep or re-enter.\n'


def test_cancellation_is_distinct_from_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    rendered = _terminal.diagnostic(OperationCanceled('Canceled.', fact='not saved'))
    assert rendered == '\x1b[33mCanceled.\x1b[0m\n  not saved'


def test_color_context_rejects_unknown_mode() -> None:
    with (
        pytest.raises(ValueError, match='invalid color mode'),
        _terminal.color_context('sometimes'),
    ):
        pass


def test_color_context_restores_nested_mode_after_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('NO_COLOR', '')
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: False)
    with _terminal.color_context('always'):
        assert _terminal.style('Label') == '\x1b[1mLabel\x1b[0m'
        with pytest.raises(RuntimeError, match='failed'), _terminal.color_context('never'):
            raise RuntimeError('failed')
        assert _terminal.style('Label') == '\x1b[1mLabel\x1b[0m'
    assert _terminal.style('Label') == 'Label'


def test_confirm_reasks_on_blank_without_default(
    answers: _Script, capsys: pytest.CaptureFixture[str]
) -> None:
    answers(
        [
            ('Publish? [y/n] ', ''),
            ('Publish? [y/n] ', 'maybe'),
            ('Publish? [y/n] ', 'later'),
            ('Publish? [y/n] ', 'n'),
        ]
    )
    assert _terminal.confirm('Publish?') is False
    assert capsys.readouterr().out == 'Enter y or n.\n' * 3


def test_confirm_strips_surrounding_whitespace(answers: _Script) -> None:
    answers([('Publish? [y/n] ', ' yes ')])
    assert _terminal.confirm('Publish?') is True


def test_confirmation_resets_color_before_answer(
    monkeypatch: pytest.MonkeyPatch, answers: _Script
) -> None:
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    answers([('\x1b[1mPublish?\x1b[0m \x1b[36m[y/N]\x1b[0m ', '')])
    assert _terminal.confirm('Publish?', default=False) is False


def test_diagnostic_uses_destination_and_keeps_details(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(sys.stderr, 'isatty', lambda: False)
    error = PubanonError('account mismatch', expected='configured', actual='authenticated')
    assert _terminal.diagnostic(error, stream=sys.stderr) == (
        'error: account mismatch\n  Expected: configured\n  Found: authenticated'
    )
    assert _terminal.diagnostic(error).startswith('\x1b[1;31merror: account mismatch\x1b[0m')


def test_display_path_abbreviates_home_and_escapes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    home = tmp_path / 'home'
    expected = r'~\\report\x1b.txt' if os.name == 'nt' else r'~/report\x1b.txt'
    assert _terminal.display_path(home / 'report\x1b.txt') == expected
    outside = tmp_path / 'elsewhere'
    assert _terminal.display_path(outside) == _terminal.escape(str(outside))
    monkeypatch.chdir(tmp_path)
    assert _terminal.display_path(Path('elsewhere')) == _terminal.escape(str(outside))
    command = _terminal.shell_command('cat', str(home / 'report.txt'))
    assert command is not None
    assert shlex.split(command) == ['cat', str(home / 'report.txt')]


def test_display_time_uses_local_seconds_and_zone(monkeypatch: pytest.MonkeyPatch) -> None:
    if sys.platform == 'win32':
        pytest.skip('requires process timezone control')
    try:
        with monkeypatch.context() as context:
            context.setenv('TZ', 'EST5')
            time.tzset()
            assert _terminal.display_time(datetime(2026, 9, 26, 20, 11, 44, tzinfo=UTC)) == (
                '2026-09-26 15:11:44 EST'
            )
    finally:
        time.tzset()


def test_escape_renders_controls_and_backslashes_visibly() -> None:
    assert _terminal.escape('a\x1b[31mb\\c') == 'a\\x1b[31mb\\\\c'


def test_field_styles_label_and_optional_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv('NO_COLOR', '')
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: False)
    with _terminal.color_context('always'):
        assert _terminal.field('Status', 'ready', kind='success') == (
            '\x1b[1mStatus:\x1b[0m \x1b[32mready\x1b[0m'
        )
    assert _terminal.field('Status', 'ready', kind='success') == 'Status: ready'


def test_fields_align_values_and_style_only_labels(capsys: pytest.CaptureFixture[str]) -> None:
    with _terminal.color_context('always'):
        _terminal.fields([('A', 'one'), ('Long', 'two')], indent=2)
        _terminal.fields([])
    assert capsys.readouterr().out == ('  \x1b[1mA:\x1b[0m    one\n  \x1b[1mLong:\x1b[0m two\n')


def test_guidance_keeps_project_and_existing_command_options() -> None:
    with _terminal.guidance_context(project='other'):
        assert _terminal.guidance('run `pubanon publish --resolve`.') == (
            'run `pubanon publish --project=other --resolve`.'
        )
        assert _terminal.guidance('pubanon stage forget') == (
            'pubanon stage forget --project=other'
        )
        assert _terminal.guidance('pubanon stage delete --yes') == (
            'pubanon stage delete --project=other --yes'
        )
        assert _terminal.guidance('pubanon stage repair or pubanon stage forget') == (
            'pubanon stage repair --project=other or pubanon stage forget --project=other'
        )
        assert _terminal.guidance('pubanon repo watch --project demo --version 1.0.0') == (
            'pubanon repo watch --project demo --version 1.0.0'
        )
        assert _terminal.guidance('pubanon stage watch --timeout 45') == (
            'pubanon stage watch --project=other --timeout 45'
        )
        assert _terminal.guidance('pubanon package watch --version 1.0.0') == (
            'pubanon package watch --project=other --version 1.0.0'
        )
        assert _terminal.guidance('pubanon audit --remote') == (
            'pubanon audit --project=other --remote'
        )
        assert _terminal.guidance('pubanon release show v1.2.3') == (
            'pubanon release show --project=other v1.2.3'
        )
        assert _terminal.guidance('pubanon setup --edit') == 'pubanon setup --edit'
        assert (
            _terminal.guidance('Read /tmp/pubanon inspect/notes')
            == 'Read /tmp/pubanon inspect/notes'
        )
    assert _terminal.guidance('pubanon inspect') == 'pubanon inspect'


def test_guidance_omits_project_selected_by_current_directory() -> None:
    with _terminal.guidance_context(project='demo', implicit_project='demo'):
        assert _terminal.guidance('run `pubanon inspect`.') == 'run `pubanon inspect`.'
        assert _terminal.guidance('pubanon stage forget --project demo') == (
            'pubanon stage forget'
        )
        assert _terminal.guidance('pubanon stage delete --project=demo --yes') == (
            'pubanon stage delete --yes'
        )
        assert _terminal.guidance('run `pubanon publish --project demo --resolve`.') == (
            'run `pubanon publish --resolve`.'
        )
        assert _terminal.guidance('run `pubanon publish --project other --resolve`.') == (
            'run `pubanon publish --project other --resolve`.'
        )
        assert _terminal.guidance('run `pubanon show --project demo latest`.') == (
            'run `pubanon show latest`.'
        )
        assert _terminal.guidance('run `pubanon delete --project demo latest`.') == (
            'run `pubanon delete latest`.'
        )
        assert _terminal.guidance('pubanon audit --project=demo --remote') == (
            'pubanon audit --remote'
        )
    assert _terminal.guidance('pubanon inspect') == 'pubanon inspect'


def test_guidance_omits_shell_quoted_implicit_project() -> None:
    with _terminal.guidance_context(project='my project', implicit_project='my project'):
        assert _terminal.guidance("run `pubanon inspect --project 'my project'`.") == (
            'run `pubanon inspect`.'
        )


def test_guidance_preserves_different_explicit_project_in_nested_workflow() -> None:
    with (
        _terminal.guidance_context(project='demo', implicit_project='demo'),
        _terminal.guidance_context(project='other', workflow=True),
    ):
        assert _terminal.guidance('run `pubanon publish --resolve`.') == (
            'run `pubanon publish --project=other --resolve`.'
        )
        assert _terminal.guidance('run `pubanon publish --project other`.') == (
            'run `pubanon publish --project other`.'
        )
        assert _terminal.guidance('run `pubanon show latest`.') == (
            'run `pubanon show --project=other latest`.'
        )


def test_guidance_qualifies_forget_recovery() -> None:
    with _terminal.guidance_context(project='other'):
        assert _terminal.guidance('pubanon forget --yes') == 'pubanon forget --project=other --yes'


def test_guidance_shows_unrenderable_project_without_escaped_command() -> None:
    with _terminal.guidance_context(project='line\nbreak'):
        result = _terminal.guidance('run `pubanon clean`.')
    assert 'line\\x0abreak' in result
    assert 'rerun the clean action' in result
    assert '--project=' not in result


def test_guidance_stage_recovery_quotes_project_and_uses_safe_fallback() -> None:
    with _terminal.guidance_context(project='my project'):
        command = _terminal.guidance('pubanon stage forget')
    assert shlex.split(command) == ['pubanon', 'stage', 'forget', '--project=my project']

    with _terminal.guidance_context(project='line\nbreak'):
        advice = _terminal.guidance('pubanon stage delete')
    assert advice == (
        'Choose project line\\x0abreak from its registered directory, '
        'then rerun the stage delete action.'
    )


def test_guidance_uses_implicit_project_without_explicit_selection() -> None:
    with _terminal.guidance_context(implicit_project='demo'):
        assert _terminal.guidance('run `pubanon show --project demo latest`.') == (
            'run `pubanon show latest`.'
        )
        assert _terminal.guidance('run `pubanon inspect`.') == 'run `pubanon inspect`.'


def test_heading_starts_output_and_separates_later_sections(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    _terminal.heading('Included files')
    print('A file')
    _terminal.heading('Excluded files', separate=True)
    assert capsys.readouterr().out == (
        '\x1b[1;36mIncluded files\x1b[0m\nA file\n\n\x1b[1;36mExcluded files\x1b[0m\n'
    )


def test_interrupt_cancels(monkeypatch: pytest.MonkeyPatch) -> None:
    def _interrupted(_prompt: str) -> str:
        raise KeyboardInterrupt

    monkeypatch.setattr('builtins.input', _interrupted)
    with pytest.raises(OperationCanceled, match=r'Canceled\.'):
        _terminal.ask('Name')


@pytest.mark.parametrize(
    ('kind', 'message', 'headline', 'code'),
    [
        ('warning', 'Canceled. Nothing was published.', 'Canceled. Nothing was published.', '33'),
        ('error', 'outcome is unknown', 'error: outcome is unknown', '1;31'),
        ('success', 'Publication completed.', 'Publication completed.', '32'),
    ],
)
def test_interruption_presentation_is_independent_of_status(
    kind: Literal['warning', 'error', 'success'],
    message: str,
    headline: str,
    code: str,
) -> None:
    error = OperationCanceled(message, fact='retained fact', recovery='pubanon status')
    error.diagnostic_kind = kind
    with _terminal.color_context('always'):
        assert _terminal.diagnostic(error) == (
            f'\x1b[{code}m{headline}\x1b[0m\n  retained fact\n\n'
            '\x1b[1mNext:\x1b[0m \x1b[36mpubanon status\x1b[0m'
        )
        assert error.render() == f'{headline}\n  retained fact\n\nNext: pubanon status'


@pytest.mark.parametrize(
    ('seconds', 'expected'), [(60, '1 minute'), (1800, '30 minutes'), (90, '90 seconds')]
)
def test_display_duration_uses_whole_minutes(seconds: int, expected: str) -> None:
    assert _terminal.display_duration(seconds) == expected


@pytest.mark.parametrize(
    ('state', 'detail', 'expected'),
    [
        ('missing', '', 'not found'),
        ('ambiguous', '101 check runs', 'could not be determined'),
        ('pending', 'queued', 'pending'),
        ('success', 'success', 'passed'),
        ('unacceptable', 'failure', 'failed'),
        ('unacceptable', 'neutral', 'failed (neutral)'),
        ('unacceptable', 'skipped', 'failed (skipped)'),
        (
            'unacceptable',
            'action_required, cancelled, failure, timed_out',
            'failed (needs action, canceled, timed out)',
        ),
        ('unacceptable', 'unknown\x1b', 'failed (unknown\\x1b)'),
    ],
)
def test_display_check_result_preserves_nonpassing_verdicts(
    state: str, detail: str, expected: str
) -> None:
    assert _terminal.display_check_result(CheckVerdict('build', state, detail)) == expected


@pytest.mark.parametrize(
    ('error', 'expected'),
    [
        (PubanonError('refused'), 'error: refused'),
        (PubanonError('refused', fact='first\nsecond'), 'error: refused\n  first\n  second'),
        (
            PubanonError('refused', expected='one', actual='two'),
            'error: refused\n  Expected: one\n  Found: two',
        ),
        (
            PubanonError('refused', recovery='retry with\npubanon status'),
            'error: refused\n\nNext: retry with\n      pubanon status',
        ),
        (
            OperationCanceled('Canceled.', fact='kept', recovery='pubanon status'),
            'Canceled.\n  kept\n\nNext: pubanon status',
        ),
    ],
)
def test_diagnostic_layout(error: PubanonError, expected: str) -> None:
    with _terminal.color_context('never'):
        assert _terminal.diagnostic(error) == expected


@pytest.mark.parametrize(('reply', 'expected'), [('s', 'save'), ('SAVE', 'save'), (' e ', 'edit')])
def test_ask_choices_accept_letter_or_word(answers: _Script, reply: str, expected: str) -> None:
    answers([('Save? [s]ave / [e]dit / [c]ancel: ', reply)])
    assert _terminal.ask('Save?', choices=['save', 'edit', 'cancel']) == expected


@pytest.mark.parametrize(
    ('mode', 'terminal', 'no_color', 'expected'),
    [
        ('auto', True, False, '\x1b[1mLabel\x1b[0m'),
        ('auto', False, False, 'Label'),
        ('auto', True, True, 'Label'),
        ('always', False, True, '\x1b[1mLabel\x1b[0m'),
        ('never', True, False, 'Label'),
    ],
)
def test_color_context_controls_terminal_style(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    terminal: bool,
    no_color: bool,
    expected: str,
) -> None:
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: terminal)
    if no_color:
        monkeypatch.setenv('NO_COLOR', '')
    else:
        monkeypatch.delenv('NO_COLOR', raising=False)
    with _terminal.color_context(mode):
        assert _terminal.style('Label') == expected


@pytest.mark.parametrize(
    ('default', 'prompt', 'reply', 'expected'),
    [
        (None, 'Publish? [y/n] ', 'y', True),
        (None, 'Publish? [y/n] ', 'NO', False),
        (True, 'Publish? [Y/n] ', '', True),
        (False, 'Publish? [y/N] ', '', False),
    ],
)
def test_confirm_renders_default_and_accepts_answers(
    answers: _Script, default: bool | None, prompt: str, reply: str, expected: bool
) -> None:
    answers([(prompt, reply)])
    assert _terminal.confirm('Publish?', default=default) is expected


@pytest.mark.parametrize('failure', [EOFError(), OSError('closed')])
def test_end_of_input_requires_interactive_terminal(
    monkeypatch: pytest.MonkeyPatch, failure: Exception
) -> None:
    def _unavailable(_prompt: str) -> str:
        raise failure

    monkeypatch.setattr('builtins.input', _unavailable)
    with pytest.raises(PubanonError, match='this step needs an interactive terminal') as error:
        _terminal.confirm('Publish?')
    assert error.value.recovery == 'rerun in an interactive terminal'


@pytest.mark.parametrize(
    ('text', 'expected'),
    [
        ('\x00\x1b\x7f\x85', r'\x00\x1b\x7f\x85'),
        ('\u200b\u202e\U000e0001', r'\u200b\u202e\U000e0001'),
        ('\u2028\u2029', r'\u2028\u2029'),
        ('\ud800\udcff', r'\ud800\udcff'),
        ('café \U0001f680', 'café \U0001f680'),
        ('Secret\n\r\t\x1b[31m\\name\u202e', r'Secret\x0a\x0d\x09\x1b[31m\\name\u202e'),
    ],
)
def test_escape_preserves_spelling_and_renders_unicode_controls(text: str, expected: str) -> None:
    assert _terminal.escape(text) == expected


@pytest.mark.parametrize(
    ('keys', 'expected'),
    [
        (b'a' * 60 + b'\x7f' * 45 + b'Z\n', b'a' * 15 + b'Z'),
        (b'a' * 60 + b'\x1b[D' * 35 + b'\x7f' * 5 + b'Z\n', b'a' * 20 + b'Z' + b'a' * 35),
    ],
)
def test_free_text_edits_across_wrapped_lines(keys: bytes, expected: bytes) -> None:
    if os.name == 'nt':
        pytest.skip('requires a POSIX pseudoterminal')
    master, slave = pty.openpty()
    termios.tcsetwinsize(slave, (24, 40))
    script = (
        'from pubanon._terminal import ask\n'
        'print("RESULT", repr(ask("Description")), flush=True)\n'
    )
    try:
        with subprocess.Popen(
            [sys.executable, '-c', script],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            env={
                **os.environ,
                'TERM': 'xterm',
                'PYTHONPATH': str(Path(_terminal.__file__).parents[1]),
            },
        ) as process:
            try:
                captured = b''
                deadline = time.monotonic() + 5
                while b'Description' not in captured:
                    assert time.monotonic() < deadline, captured
                    if select.select([master], [], [], 0.1)[0]:
                        captured += os.read(master, 4096)
                # Move left and erase past the wrap, then insert into the earlier row.
                os.write(master, keys)
                while b'RESULT' not in captured:
                    assert time.monotonic() < deadline, captured
                    if select.select([master], [], [], 0.1)[0]:
                        captured += os.read(master, 4096)
                process.wait(timeout=5)
                assert b"RESULT '" + expected + b"'" in captured
                assert process.returncode == 0
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=5)
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.skipif(os.name == 'nt', reason='readline prompt markers are POSIX-only')
def test_free_text_prompt_marks_color_as_zero_width(
    monkeypatch: pytest.MonkeyPatch,
    answers: _Script,
) -> None:
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    answers([('\x01\x1b[1m\x02Name\x01\x1b[0m\x02: ', 'value')])
    assert _terminal.ask('Name') == 'value'


@pytest.mark.parametrize('name', ['my project', "it's mine", r'a\b', '-leading'])
def test_guidance_preserves_explicit_quoted_project(name: str) -> None:
    command = _terminal.shell_command('pubanon', 'clean', f'--project={name}')
    assert command is not None
    with _terminal.guidance_context(project='other'):
        assert _terminal.guidance(command) == command
    with _terminal.guidance_context(project='demo', implicit_project='demo'):
        assert _terminal.guidance(command) == command
    with _terminal.guidance_context(project=name, implicit_project=name):
        result = _terminal.guidance(command)
    assert shlex.split(result) == ['pubanon', 'clean']


@pytest.mark.parametrize(
    ('command', 'action', 'mode'),
    [
        ('pubanon publish --resolve', 'publish', '--resolve'),
        ('pubanon prepare --continue', 'prepare', '--continue'),
    ],
)
def test_guidance_retains_recovery_mode_for_unrenderable_project(
    command: str, action: str, mode: str
) -> None:
    with _terminal.guidance_context(project='line\nbreak'):
        result = _terminal.guidance(command)
    assert result == (
        'Choose project line\\x0abreak from its registered directory, '
        f'then rerun the {action} action with {mode}.'
    )
    assert '--project=' not in result


@pytest.mark.parametrize(
    ('expression', 'keys', 'expected'),
    [
        ("ask('Action?', choices=['continue', 'quit'])", b'c', b'RESULT continue'),
        ("ask('Action?', choices=['continue', 'quit'])", b'Q', b'RESULT quit'),
        ("confirm('Proceed?')", b'y', b'RESULT True'),
        ("ask('Action?', choices=['continue', 'quit'])", None, b'Canceled.'),
        ("ask('Name')", b'example', b'RESULT example'),
        ("confirm('Proceed?', default=False)", b'\r', b'RESULT False'),
        (
            "ask('Action?', choices=['continue', 'quit'])",
            b'\x04',
            b'this step needs an interactive terminal',
        ),
    ],
)
def test_menu_accepts_key_without_enter_and_restores_terminal(
    expression: str,
    keys: bytes | None,
    expected: bytes,
) -> None:
    if os.name == 'nt':
        pytest.skip('requires a POSIX pseudoterminal')
    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)
    script = (
        # Background CI launchers may ignore SIGINT; emulate a foreground terminal.
        'import signal\n'
        'signal.signal(signal.SIGINT, signal.default_int_handler)\n'
        'from pubanon._terminal import ask, confirm\n'
        'from pubanon._errors import PubanonError\n'
        'try:\n'
        f'    print("RESULT", {expression})\n'
        'except PubanonError as error:\n'
        '    print(str(error))\n'
    )
    try:
        with subprocess.Popen(
            [sys.executable, '-c', script],
            stdin=slave,
            env={**os.environ, 'PYTHONPATH': str(Path(_terminal.__file__).parents[1])},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            try:
                assert process.stdout is not None
                assert select.select([process.stdout], [], [], 5)[0], 'prompt was not displayed'
                prompt = os.read(process.stdout.fileno(), 4096)
                assert any(label in prompt for label in (b'Action?', b'Proceed?', b'Name'))
                if keys is None:
                    process.send_signal(signal.SIGINT)
                else:
                    os.write(master, keys)
                if expression == "ask('Name')":
                    assert not select.select([process.stdout], [], [], 0.1)[0]
                    os.write(master, b'\n')
                output, errors = process.communicate(timeout=5)
                assert process.returncode == 0, errors
                assert expected in output
                restored = termios.tcgetattr(slave)
                # macOS sets PENDIN when restoring settings; it is transient queue state.
                restored[3] &= ~termios.PENDIN
                original[3] &= ~termios.PENDIN
                assert restored == original
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate()
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize(
    ('second', 'fresh', 'expected'),
    [
        ("confirm('Second?', default=False)", b'y', b'SECOND True'),
        ("ask('Name')", b'Alice\n', b'SECOND Alice'),
    ],
)
def test_menu_queued_line_does_not_answer_next_prompt(
    second: str, fresh: bytes, expected: bytes
) -> None:
    if os.name == 'nt':
        pytest.skip('requires a POSIX pseudoterminal')
    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)
    script = (
        'from pubanon._terminal import ask, confirm\n'
        'print("FIRST", confirm("First?"), flush=True)\n'
        f'print("SECOND", {second}, flush=True)\n'
    )
    try:
        with subprocess.Popen(
            [sys.executable, '-c', script],
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={**os.environ, 'PYTHONPATH': str(Path(_terminal.__file__).parents[1])},
        ) as process:
            try:
                assert process.stdout is not None
                assert select.select([process.stdout], [], [], 5)[0], (
                    'first prompt was not displayed'
                )
                prompt = os.read(process.stdout.fileno(), 4096)
                assert b'First?' in prompt
                os.write(master, b'yes\n')
                output = b''
                deadline = time.monotonic() + 5
                while b'Second?' not in output and b'Name' not in output:
                    assert time.monotonic() < deadline, output
                    if select.select([process.stdout], [], [], 0.1)[0]:
                        output += os.read(process.stdout.fileno(), 4096)
                assert b'FIRST True' in output
                assert b'SECOND ' not in output
                assert not select.select([process.stdout], [], [], 0.1)[0]
                os.write(master, fresh)
                remaining, errors = process.communicate(timeout=5)
                assert process.returncode == 0, errors
                assert expected in output + remaining
                restored = termios.tcgetattr(slave)
                restored[3] &= ~termios.PENDIN
                original[3] &= ~termios.PENDIN
                assert restored == original
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=5)
    finally:
        os.close(master)
        os.close(slave)


def test_never_mode_removes_bold_from_all_terminal_helpers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    with _terminal.color_context('never'):
        assert _terminal.style('Label', 'label') == 'Label'
        assert _terminal.style('Heading', 'heading') == 'Heading'
        assert _terminal.diagnostic(PubanonError('failed')) == 'error: failed'


@pytest.mark.parametrize('implicit', [None, 'demo'])
def test_next_step_qualifies_each_command_in_rebuild_sequence(
    capsys: pytest.CaptureFixture[str], implicit: str | None
) -> None:
    with (
        _terminal.color_context('always'),
        _terminal.guidance_context(project='demo', implicit_project=implicit),
    ):
        _terminal.next_step('pubanon discard && pubanon prepare --source abc123')
    project = '' if implicit else ' --project=demo'
    assert capsys.readouterr().out == (
        f'\x1b[1mNext:\x1b[0m \x1b[36mpubanon discard{project} && '
        f'pubanon prepare{project} --source abc123\x1b[0m\n'
    )


@pytest.mark.parametrize('terminal', [True, False])
def test_no_color_preserves_readable_sections(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], terminal: bool
) -> None:
    monkeypatch.setenv('NO_COLOR', '')
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: terminal)
    _terminal.heading('Included files')
    assert capsys.readouterr().out == 'Included files\n'


@pytest.mark.parametrize(
    ('terminal', 'no_color', 'expected'),
    [
        (True, False, '\x1b[2mNote.\x1b[0m\n'),
        (False, False, 'Note.\n'),
        (True, True, 'Note.\n'),
    ],
)
def test_note_style_respects_terminal_and_no_color(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    terminal: bool,
    no_color: bool,
    expected: str,
) -> None:
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: terminal)
    if no_color:
        monkeypatch.setenv('NO_COLOR', '')
    else:
        monkeypatch.delenv('NO_COLOR', raising=False)
    _terminal.output('Note.', kind='note')
    assert capsys.readouterr().out == expected


def test_output_encoding_context_restores_after_unexpected_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stream = io.TextIOWrapper(io.BytesIO(), encoding='ascii', errors='strict')
    monkeypatch.setattr(sys, 'stdout', stream)
    with pytest.raises(RuntimeError, match='failed'), _terminal.output_encoding_context():
        raise RuntimeError('failed')
    assert stream.encoding == 'ascii'
    assert stream.errors == 'strict'


@pytest.mark.parametrize('reader', [_terminal.ask, _terminal.confirm])
def test_piped_stdin_never_reaches_a_question(
    monkeypatch: pytest.MonkeyPatch,
    reader: Callable[[str], str | bool],
) -> None:
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    # The autouse guard would fail the test if input() were called; the reader must not call it.
    with pytest.raises(PubanonError, match='this step needs an interactive terminal') as error:
        reader('Name')
    assert error.value.recovery == 'rerun in an interactive terminal'


def test_redirected_output_has_no_ansi(capsys: pytest.CaptureFixture[str]) -> None:
    _terminal.output('Saved.', kind='success')
    assert capsys.readouterr().out == 'Saved.\n'


def test_render_fields_returns_text_without_printing(capsys: pytest.CaptureFixture[str]) -> None:
    with _terminal.color_context('never'):
        assert _terminal.render_fields([('A', 'one'), ('Long', 'two')], indent=2) == (
            '  A:    one\n  Long: two'
        )
        assert _terminal.render_fields([]) == ''
    assert capsys.readouterr().out == ''


def test_shell_command_omits_unrepresentable_control() -> None:
    assert _terminal.shell_command('pubanon', 'inspect', '--project=a\nb') is None


def test_shell_command_preserves_windows_paths_and_spaces() -> None:
    args = (
        'git',
        '-C',
        r"C:\source's tree",
        'worktree',
        'add',
        '--detach',
        r'C:\private state\source-worktrees\commit',
        'a' * 40,
    )
    command = _terminal.shell_command(*args)
    assert command is not None
    assert shlex.split(command) == list(args)


@pytest.mark.parametrize('name', ['my project', "it's mine", r'a\b', '-leading'])
def test_shell_command_selects_exact_project(name: str) -> None:
    command = _terminal.shell_command('pubanon', 'clean', f'--project={name}')
    assert command is not None
    argv = shlex.split(command)
    assert argv == ['pubanon', 'clean', f'--project={name}']
    parsed = _arguments.build_parser().parse_args(argv[1:])
    assert parsed.project == name
    assert parsed.command == 'clean'


def test_terminal_prompt_styles_question_and_choices(
    monkeypatch: pytest.MonkeyPatch, answers: _Script
) -> None:
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    answers([('\x1b[1mSave?\x1b[0m \x1b[36m[s]ave / [c]ancel\x1b[0m: ', 's')])
    assert _terminal.ask('Save?', choices=['save', 'cancel']) == 'save'


def test_verbose_details_are_scoped_and_restore_after_failure(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert not _terminal.verbose_enabled()
    _terminal.detail('hidden')
    _terminal.detail_fields([('Hidden', 'value')])
    assert capsys.readouterr().out == ''

    def _fail_with_details() -> None:
        with _terminal.verbose_context(enabled=True):
            assert _terminal.verbose_enabled()
            _terminal.detail('visible')
            _terminal.detail_fields([('Short', 'one'), ('Long label', 'two')], indent=2)
            with _terminal.verbose_context(enabled=False):
                _terminal.detail('nested hidden')
            _terminal.detail()
            raise RuntimeError

    with pytest.raises(RuntimeError):
        _fail_with_details()
    assert not _terminal.verbose_enabled()
    assert capsys.readouterr().out == 'visible\n  Short:      one\n  Long label: two\n\n'


def test_windows_line_prompt_keeps_ansi_sequences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = '\x1b[1mName\x1b[0m: '

    def _reply(actual: str) -> str:
        assert actual == prompt
        return 'Alice'

    monkeypatch.setattr(_terminal.os, 'name', 'nt')
    monkeypatch.setattr('builtins.input', _reply)
    assert _terminal._read(prompt) == 'Alice'


@pytest.mark.parametrize(
    ('stale', 'queued'),
    [
        (['n'], ['\r', 'x']),
        (['\x00', 'H'], ['\xe0', 'P', 'x']),
    ],
)
def test_windows_menu_discards_stale_and_accepted_queued_keys(
    stale: list[str], queued: list[str]
) -> None:
    remaining = iter([*stale, 'y', *queued])
    available = iter([*[True] * len(stale), False, *[True] * len(queued), False])
    assert (
        _terminal._read_key_windows('Proceed? ', lambda: next(remaining), lambda: next(available))
        == 'y'
    )
    assert list(remaining) == []


@pytest.mark.parametrize(
    ('characters', 'expected'),
    [
        (['Y'], 'Y'),
        (['\r'], ''),
        (['\x00', 'y'], '\x00'),
        (['\xe0', 'n'], '\x00'),
    ],
)
def test_windows_menu_reads_one_console_key(
    characters: list[str], expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    remaining = iter(characters)
    assert (
        _terminal._read_key_windows('Proceed? ', lambda: next(remaining), lambda: False)
        == expected
    )
    assert list(remaining) == []
    assert capsys.readouterr().out.startswith('Proceed? ')


@pytest.mark.parametrize('character', ['\x04', '\x1a'])
def test_windows_menu_treats_eof_keys_as_unavailable(character: str) -> None:
    with pytest.raises(EOFError):
        _terminal._read_key_windows('Proceed? ', lambda: character, lambda: False)


def test_windows_menu_turns_ctrl_c_into_interrupt() -> None:
    with pytest.raises(KeyboardInterrupt):
        _terminal._read_key_windows('Proceed? ', lambda: '\x03', lambda: False)


def test_workflow_suppresses_child_next_steps(capsys: pytest.CaptureFixture[str]) -> None:
    with _terminal.guidance_context(workflow=True):
        _terminal.next_step('pubanon publish')
    assert capsys.readouterr().out == ''
    _terminal.next_step('pubanon inspect')
    assert capsys.readouterr().out == 'Next: pubanon inspect\n'
