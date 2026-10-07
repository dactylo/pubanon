import argparse
import importlib.metadata
import io
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest

from pubanon import (
    _cli,
    _config,
    _run,
    _terminal,
)
from pubanon._cli import _arguments, _forget, _init, _inspect, _prepare, _releases, _setup
from pubanon._cli import _history as cli_history
from pubanon._cli import _remote as cli_remote
from pubanon._cli import _status as cli_status
from pubanon._cli._arguments import build_parser
from pubanon._cli._downstream import _ClosableTextOutput
from pubanon._errors import PubanonError
from pubanon._remote import _host, _watch
from pubanon._snapshots import _store
from pubanon._terminal import ask, confirm, next_step
from tests._git import git_env
from tests._process import process_transcript, run_closing_after_first_line, run_with_closed_output
from tests._setup import configure

_REFUSE_AFTER_OUTPUT = (
    'from pubanon import _cli\n'
    'from pubanon._errors import PubanonError\n'
    'def _command(_args):\n'
    '    print("partial output")\n'
    '    raise PubanonError("refused")\n'
    '_cli._dispatch = _command\n'
    'raise SystemExit(_cli.main(["setup"]))\n'
)


def _help_entries(
    parser: argparse.ArgumentParser, prefix: str
) -> Iterator[tuple[str, str | None]]:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            subparsers = cast('argparse._SubParsersAction[argparse.ArgumentParser]', action)
            visible = [choice.dest for choice in subparsers._choices_actions]
            for choice in subparsers._choices_actions:
                yield f'{prefix} {choice.dest}', choice.help
            for name in visible:
                yield from _help_entries(subparsers.choices[name], f'{prefix} {name}')
        elif action.help is not argparse.SUPPRESS:
            label = '/'.join(action.option_strings) or action.dest
            yield f'{prefix} {label}', action.help


def _starts_in_lowercase_style(text: str) -> bool:
    first = text.split(maxsplit=1)[0]
    is_acronym = first.upper() == first and any(character.isalpha() for character in first)
    return text[0].islower() or is_acronym


def test_abbreviated_color_after_separator_does_not_change_help_policy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _cli.main(['show', '--', '--colo=always']) == 1
    assert '\x1b[' not in capsys.readouterr().err


def test_all_command_help_uses_the_same_common_option_order() -> None:
    pending: list[argparse.ArgumentParser] = [build_parser()]
    with _terminal.color_context('never'):
        while pending:
            parser = pending.pop()
            text = parser.format_help()
            sections = re.findall(r'^([\w ]+ options):$', text, re.MULTILINE)
            assert sections, parser.prog
            assert sections[0] == 'common options', parser.prog
            assert '\noptions:\n' not in text, parser.prog
            common = text.split('common options:\n', 1)[1].split('\n\n', 1)[0]
            flags = re.findall(r'^  ((?:-\w, )?--[a-z-]+)\b', common, re.MULTILINE)
            expected = ['-h, --help']
            if parser.prog not in {'pubanon setup', 'pubanon doctor'}:
                expected.append('-p, --project')
            expected.extend(('-v, --verbose', '--color'))
            if parser.prog == 'pubanon':
                expected.append('--version')
            assert flags == expected, parser.prog
            for action in parser._actions:
                if isinstance(action, argparse._SubParsersAction):
                    subcommands = cast(
                        'argparse._SubParsersAction[argparse.ArgumentParser]', action
                    )
                    pending.extend(subcommands.choices.values())


def test_ambiguous_color_prefix_is_left_to_argparse(capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli.main(['prepare', '--co=always', '--help']) == 2
    rendered = capsys.readouterr()
    assert 'ambiguous option' in rendered.err
    assert '\x1b[' not in rendered.err


def test_cli_keeps_stringio_streams_unmodified(monkeypatch: pytest.MonkeyPatch) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', stdout)
    monkeypatch.setattr(sys, 'stderr', stderr)

    def _command(_args: argparse.Namespace) -> None:
        print('caf\xe9')
        raise PubanonError('caf\xe9 diagnostic')

    monkeypatch.setattr(_cli, '_dispatch', _command)
    assert _cli.main(['setup']) == 1
    assert sys.stdout is stdout
    assert sys.stderr is stderr
    assert stdout.getvalue() == 'caf\xe9\n'
    assert 'caf\xe9 diagnostic' in stderr.getvalue()


def test_cli_restores_supplied_stream_error_policies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout_bytes = io.BytesIO()
    stderr_bytes = io.BytesIO()
    stdout = io.TextIOWrapper(stdout_bytes, encoding='ascii', errors='strict', newline='\n')
    stderr = io.TextIOWrapper(stderr_bytes, encoding='ascii', errors='strict', newline='\n')
    monkeypatch.setattr(sys, 'stdout', stdout)
    monkeypatch.setattr(sys, 'stderr', stderr)

    def _command(_args: argparse.Namespace) -> None:
        print('caf\xe9')
        raise PubanonError('caf\xe9 diagnostic')

    monkeypatch.setattr(_cli, '_dispatch', _command)
    assert _cli.main(['setup']) == 1
    assert sys.stdout is stdout
    assert sys.stderr is stderr
    assert stdout.encoding == stderr.encoding == 'ascii'
    assert stdout.errors == stderr.errors == 'strict'
    stdout.flush()
    stderr.flush()
    assert stdout_bytes.getvalue() == b'caf\\xe9\n'
    assert b'caf\\xe9 diagnostic' in stderr_bytes.getvalue()


@pytest.mark.parametrize('argv', [['--help'], ['--version']], ids=['help', 'version'])
def test_closed_stdout_before_output_is_quiet(argv: list[str]) -> None:
    result = run_with_closed_output(
        ['-m', 'pubanon', *argv], env={**os.environ, 'PYTHON_COLORS': '0'}
    )
    assert (result.returncode, result.stderr) == (0, b''), process_transcript(result)


def test_closed_stdout_keeps_a_refusal_status() -> None:
    result = run_with_closed_output(['-c', _REFUSE_AFTER_OUTPUT])
    assert result.returncode == 1, process_transcript(result)
    assert result.stderr.splitlines() == [b'error: refused'], process_transcript(result)


def test_closed_stdout_keeps_the_status_result(
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
) -> None:
    _paths, _global_config, project = configured_project
    result = run_with_closed_output(
        ['-m', 'pubanon', 'status', '--project', project.name], env=git_env()
    )
    assert (result.returncode, result.stderr) == (0, b''), process_transcript(result)


def test_color_operand_after_separator_does_not_change_help_policy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _cli.main(['show', '--', '--color=always']) == 1
    assert '\x1b[' not in capsys.readouterr().err


def test_commands_see_the_terminal_and_error_policy_through_the_stdout_wrapper(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    observed: list[object] = []

    def _command(_args: argparse.Namespace) -> None:
        observed.extend((sys.stdout, sys.stdout.errors, _terminal.color_enabled()))
        print('caf\xe9')

    original = sys.stdout
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.setattr(_cli, '_dispatch', _command)
    assert _cli.main(['setup']) == 0
    stream, errors, color = observed
    assert isinstance(stream, _ClosableTextOutput)
    assert (errors, color) == ('backslashreplace', True)
    assert sys.stdout is original
    assert capsys.readouterr().out == 'caf\xe9\n'


def test_create_help_describes_optional_page_defaults(capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli.main(['release', 'create', '--help']) == 0
    help_text = capsys.readouterr().out
    normalized_help = ' '.join(help_text.split())
    assert '[--notes-file FILE]' in help_text
    assert 'UTF-8 Markdown notes file (default: release_page notes)' in normalized_help
    assert 'page title (default: release_page title, normally TAG)' in normalized_help


def test_every_visible_option_and_command_has_lowercase_help() -> None:
    problems = [
        f'{name}: {text!r}'
        for name, text in _help_entries(build_parser(), 'pubanon')
        if not text or not _starts_in_lowercase_style(text) or text.endswith('.')
    ]
    assert problems == []


@pytest.mark.parametrize(
    'option',
    [
        ['--yes'],
        ['--relocate'],
        ['--self-publication'],
        ['--source-tags'],
        ['--no-source-tags'],
        ['--publication-route', 'direct'],
    ],
)
def test_init_edit_rejects_other_init_options(
    option: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _unexpected(_args: argparse.Namespace) -> None:
        pytest.fail('invalid init dispatched')

    monkeypatch.setattr(_cli, '_dispatch', _unexpected)
    assert _cli.main(['init', '--edit', *option]) == 2
    assert '--edit cannot be combined with other init options' in capsys.readouterr().err


def test_init_parser_accepts_mutually_exclusive_source_tags_flags() -> None:
    parser = build_parser()
    assert parser.parse_args(['init']).source_tags is None
    assert parser.parse_args(['init', '--source-tags']).source_tags is True
    assert parser.parse_args(['init', '--no-source-tags']).source_tags is False
    with pytest.raises(SystemExit, match='2'):
        parser.parse_args(['init', '--source-tags', '--no-source-tags'])


def test_init_parser_accepts_only_publication_routes() -> None:
    parser = build_parser()
    assert (
        parser.parse_args(['init', '--publication-route', 'direct']).publication_route == 'direct'
    )
    assert (
        parser.parse_args(['init', '--publication-route', 'staged']).publication_route == 'staged'
    )
    with pytest.raises(SystemExit, match='2'):
        parser.parse_args(['init', '--publication-route', 'unknown'])


def test_main_accepts_one_pass_help_argv(capsys: pytest.CaptureFixture[str]) -> None:
    argv = (argument for argument in ('--help',))

    assert _cli.main(argv) == 0
    output = capsys.readouterr()
    assert 'usage: pubanon' in output.out
    assert output.err == ''


def test_main_accepts_tuple_help_argv(capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli.main(('--help',)) == 0
    output = capsys.readouterr()
    assert 'usage: pubanon' in output.out
    assert output.err == ''


def test_main_returns_usage_and_refusal_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _unexpected_floors() -> None:
        pytest.fail('usage failure checked repository runtime floors')

    monkeypatch.setattr('pubanon._run.check_floors', _unexpected_floors)

    assert _cli.main(['not-a-command']) == 2

    def _refuse(*, edit: bool = False) -> None:
        assert not edit
        raise PubanonError('safe refusal', recovery='fix it')

    monkeypatch.setattr(_setup, 'setup', _refuse)
    monkeypatch.setattr('pubanon._run.check_floors', lambda: None)

    assert _cli.main(['setup']) == 1
    assert 'error: safe refusal\n\nNext: fix it' in capsys.readouterr().err


def test_parser_construction_error_is_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _broken_parser() -> argparse.ArgumentParser:
        raise PubanonError('parser setup failed')

    monkeypatch.setattr(_arguments, 'build_parser', _broken_parser)
    assert _cli.main(['--help']) == 1
    assert 'parser setup failed' in capsys.readouterr().err


def test_prepare_rewrite_continue_reaches_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []

    def _handler(args: argparse.Namespace) -> None:
        seen.append(args.continue_rewrite)

    monkeypatch.setattr(_prepare, 'command_prepare', _handler)
    assert _cli.main(['prepare', '--project', 'demo', '--continue', '--yes']) == 0
    assert seen == [True]


def test_prepare_without_replacement_keeps_new_publication_mode() -> None:
    assert build_parser().parse_args(['prepare']).replace is None


def test_project_command_guidance_matches_registered_commands() -> None:
    pending: list[argparse.ArgumentParser] = [build_parser()]
    commands: set[str] = set()
    while pending:
        parser = pending.pop()
        groups = [
            cast('argparse._SubParsersAction[argparse.ArgumentParser]', action)
            for action in parser._actions
            if isinstance(action, argparse._SubParsersAction)
        ]
        if groups:
            for group in groups:
                pending.extend(group.choices.values())
        elif any('--project' in action.option_strings for action in parser._actions):
            commands.add(parser.prog.removeprefix('pubanon '))
    assert commands == set(_terminal.PROJECT_COMMANDS)
    with _terminal.guidance_context(project='chosen'):
        for command in commands:
            assert _terminal.guidance(f'pubanon {command}') == (
                f'pubanon {command} --project=chosen'
            )


def test_publish_help_lists_page_options(capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli.main(['publish', '--help']) == 0
    out = ' '.join(capsys.readouterr().out.split())
    assert '--no-release-page' in out
    assert '--release-notes-file FILE' in out
    assert 'publish without creating or looking up a release page' in out


def test_reader_closing_after_the_first_help_line_is_quiet() -> None:
    result = run_closing_after_first_line(
        ['-m', 'pubanon', '--help'], env={**os.environ, 'PYTHON_COLORS': '0'}
    )
    assert (result.returncode, result.stderr) == (0, b''), process_transcript(result)


def test_release_parser_accepts_explicit_tag_and_positive_limit() -> None:
    parser = build_parser()
    create = parser.parse_args(['release', 'create', 'v1.2.3'])
    assert create.notes_file is None
    assert create.title is None
    assert parser.parse_args(['release', 'show', 'v1.2.3']).tag == 'v1.2.3'
    assert parser.parse_args(['release', 'list']).limit == 30
    assert parser.parse_args(['release', 'list', '--limit', '2']).limit == 2


def test_repo_restore_requires_destination(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _cli.main(['repo', 'restore']) == 2
    assert 'required: --to' in capsys.readouterr().err


def test_repo_reveal_has_no_yes_option(capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli.main(['repo', 'reveal', '--project', 'demo', '--yes']) == 2
    assert 'unrecognized arguments: --yes' in capsys.readouterr().err


def test_repo_reveal_help_exits_successfully() -> None:
    assert _cli.main(['repo', 'reveal', '--help']) == 0


def test_root_help_describes_version_as_the_standard_action(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _cli.main(['--help']) == 0
    text = capsys.readouterr().out
    assert re.search(r'^  --version +show the version and exit$', text, re.MULTILINE), text


def test_root_help_groups_every_command_once(capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli.main(['--help']) == 0
    text = capsys.readouterr().out
    assert 'Initial workflow: setup -> init\n' in text
    assert ('Staged route: prepare -> inspect -> stage push -> stage watch -> publish\n') in text
    assert 'Direct route: prepare -> inspect -> publish\n' in text
    groups = {
        'publish': ('prepare', 'inspect', 'publish', 'discard', 'status', 'run'),
        'local snapshots': ('list', 'show', 'delete', 'clean', 'audit'),
        'remote repositories': ('stage', 'repo', 'release', 'package'),
        'setup': ('setup', 'init', 'forget', 'doctor'),
    }
    previous = 0
    for title, commands in groups.items():
        start = text.index(f'\n{title}:\n')
        assert start > previous
        previous = start
        section = text[start:].split('\n\n', 1)[0]
        for command in commands:
            assert any(line.split()[:1] == [command] for line in section.splitlines()[1:])
            assert sum(line.startswith(f'  {command} ') for line in text.splitlines()) == 1
    assert 'common options:\n  -h, --help' in text


def test_root_version_reports_missing_distribution_metadata(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _missing(_name: str) -> str:
        raise importlib.metadata.PackageNotFoundError('pubanon')

    monkeypatch.setattr(importlib.metadata, 'version', _missing)
    assert _cli.main(['--version']) == 1
    assert 'installed distribution metadata' in capsys.readouterr().err


def test_run_page_options_are_mutually_exclusive(capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli.main(['run', 'ship', '--no-release-page', '--release-notes-file', 'notes.md']) == 2
    assert 'not allowed with argument' in capsys.readouterr().err


def test_run_parser_exposes_declared_workflow_options() -> None:
    args = build_parser().parse_args(
        [
            'run',
            'ship',
            '--project',
            'demo',
            '--version',
            '1.2.3',
            '--acknowledge-new-paths',
            '--yes',
        ]
    )

    assert args.command == 'run'
    assert args.name == 'ship'
    assert args.project == 'demo'
    assert args.version == '1.2.3'
    assert args.acknowledge_new_paths is True
    assert args.yes is True


def test_setup_edit_parser_sets_edit_flag() -> None:
    args = build_parser().parse_args(['setup', '--edit'])

    assert args.command == 'setup'
    assert args.edit is True


def test_stage_metavar_matches_registered_subcommands() -> None:
    parser = build_parser()
    commands = next(
        cast('argparse._SubParsersAction[argparse.ArgumentParser]', action)
        for action in parser._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    stage = commands.choices['stage']
    subcommands = next(
        cast('argparse._SubParsersAction[argparse.ArgumentParser]', action)
        for action in stage._actions
        if isinstance(action, argparse._SubParsersAction)
    )
    expected = '{' + ','.join(subcommands.choices) + '}'
    assert subcommands.metavar == expected
    assert expected in stage.format_usage()


@pytest.mark.parametrize(
    'argv', [['--help'], ['--version'], [], ['setup']], ids=['help', 'version', 'usage', 'command']
)
def test_unavailable_stdout_is_an_error(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _command(_args: argparse.Namespace) -> None:
        print('partial output')

    monkeypatch.setattr(_cli, '_dispatch', _command)
    # Restore stdout before capsys finalizes, so its capture stream is not reinstated.
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(sys, 'stdout', None)
        status = _cli.main(argv)

    assert (status, capsys.readouterr().err) == (1, 'error: standard output is unavailable\n')


@pytest.mark.parametrize('mode', ['always', 'never'])
def test_explicit_color_applies_to_usage_errors(
    mode: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_color = '0' if mode == 'always' else '1'
    monkeypatch.setenv('PYTHON_COLORS', original_color)
    assert _cli.main(['--color', mode, 'unknown-command']) == 2
    assert os.environ['PYTHON_COLORS'] == original_color
    rendered = capsys.readouterr().err
    assert ('\x1b[' in rendered) == (mode == 'always')
    assert ('\x1b[' in rendered.splitlines()[0]) == (
        mode == 'always' and sys.version_info >= (3, 14)
    )


@pytest.mark.parametrize('arguments', [['status', '--verbose'], ['status'], ['setup']])
def test_main_scopes_verbose_detail(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    def _command(_args: argparse.Namespace) -> None:
        _terminal.detail('diagnostic detail')

    monkeypatch.setattr(_cli, '_dispatch', _command)
    assert _cli.main(arguments) == 0
    assert capsys.readouterr().out == ('diagnostic detail\n' if '--verbose' in arguments else '')
    assert not _terminal.verbose_enabled()


@pytest.mark.parametrize('command', [[], ['status'], ['show'], ['repo', 'verify']])
@pytest.mark.parametrize('option', ['--colo=always', '--colo'])
def test_abbreviated_color_applies_to_help_at_every_command_level(
    command: list[str], option: str, capsys: pytest.CaptureFixture[str]
) -> None:
    color = [option] if '=' in option else [option, 'always']
    assert _cli.main([*command, *color, '--help']) == 0
    assert ('\x1b[' in capsys.readouterr().out) == (sys.version_info >= (3, 14))


@pytest.mark.parametrize('command', [[], ['status'], ['repo', 'verify']])
def test_abbreviated_never_disables_color_on_help(
    command: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli.main([*command, '--colo=never', '--help']) == 0
    assert '\x1b[' not in capsys.readouterr().out


@pytest.mark.parametrize(
    ('encoding', 'expected'),
    [
        ('ascii:strict', b'caf\\xe9'),
        ('utf-8:strict', 'caf\xe9'.encode()),
    ],
)
def test_cli_child_displays_unicode_on_both_output_streams(encoding: str, expected: bytes) -> None:
    script = (
        'from pubanon import _cli\n'
        'from pubanon._errors import PubanonError\n'
        'def command(_args):\n'
        '    print("caf\\u00e9")\n'
        '    raise PubanonError("caf\\u00e9 diagnostic")\n'
        '_cli._dispatch = command\n'
        'raise SystemExit(_cli.main(["setup"]))\n'
    )
    result = subprocess.run(
        [sys.executable, '-c', script],
        capture_output=True,
        check=False,
        timeout=10,
        env={
            **os.environ,
            'PYTHONIOENCODING': encoding,
            'PYTHONPATH': str(Path(__file__).parents[2] / 'src'),
        },
    )

    assert result.returncode == 1
    assert expected in result.stdout
    assert expected in result.stderr
    assert b'UnicodeEncodeError' not in result.stderr


@pytest.mark.parametrize('reader', ['visible', 'optional', 'confirmation', 'entry'])
def test_cli_interrupt_reports_cancellation_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reader: str,
) -> None:
    def _interrupted(_prompt: str) -> str:
        raise KeyboardInterrupt

    def _command(_args: argparse.Namespace) -> None:
        if reader == 'visible':
            ask('Name')
        elif reader == 'optional':
            ask('Description', default='')
        elif reader == 'confirmation':
            confirm('Continue?')
        else:
            raise KeyboardInterrupt

    monkeypatch.setattr('builtins.input', _interrupted)
    monkeypatch.setattr(_cli, '_dispatch', _command)
    assert _cli.main(['setup']) == 130
    captured = capsys.readouterr()
    assert 'Canceled.' in captured.err
    assert 'Traceback' not in captured.out + captured.err


@pytest.mark.parametrize(
    'case',
    [
        (['status', '--project', 'demo', '--remote'], 0),
        (['repo', 'verify', '--project', 'demo'], 1),
    ],
)
def test_cli_ref_diagnostics_preserve_effective_terms(
    case: tuple[list[str], int],
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    argv, expected_status = case
    term = 'private-ref-name'
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    project = replace(project, extra_terms=(term,))
    _config.save_project(paths, project)
    commit = (
        subprocess.run(
            ['git', '-C', source, 'rev-parse', 'HEAD'],
            check=True,
            capture_output=True,
            env=git_env(),
        )
        .stdout.decode()
        .strip()
    )
    store = _store.store_path(paths, project.name)
    subprocess.run(
        [
            'git',
            '-C',
            store,
            'fetch',
            '-q',
            str(source),
            f'{commit}:refs/tags/{term}-local',
        ],
        check=True,
        env=git_env(),
    )
    for tag in ('unchanged', f'{term}-changed'):
        subprocess.run(
            ['git', '-C', store, 'update-ref', f'refs/tags/{tag}', commit],
            check=True,
            env=git_env(),
        )
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account=global_config.accounts['github'],
        visibility=project.visibility,
    )

    def _verified_destination(
        _paths: _config.Paths,
        _global: _config.GlobalConfig,
        _project: _config.ProjectConfig,
    ) -> _host.Destination:
        return destination

    def _remote_refs(
        _destination: _host.Destination,
        _paths: _config.Paths,
        _project: str,
    ) -> _host.RemoteRefs:
        return _host.RemoteRefs(
            None,
            {'unchanged': commit, f'{term}-remote': commit, f'{term}-changed': '1' * 40},
        )

    monkeypatch.setattr(_host, 'verify_destination', _verified_destination)
    monkeypatch.setattr(_host, 'ls_remote', _remote_refs)

    assert _cli.main(argv) == expected_status
    captured = capsys.readouterr()
    rendered = captured.out + captured.err
    assert term in rendered
    if argv[0] == 'status':
        diagnostic = next(line for line in captured.out.splitlines() if line.startswith('Tags '))
        assert diagnostic == (f'Tags that differ: {term}-changed, {term}-local, {term}-remote')


@pytest.mark.parametrize(
    ('argv', 'colored'),
    [
        (['--color=never', 'status', '--colo=always', '--help'], True),
        (['--colo=always', 'status', '--color', 'never', '--help'], False),
        (['--co=always', 'prepare', '--color=never', '--help'], False),
        (['--project', 'repo', '--colo=always', 'status', '--help'], True),
    ],
)
def test_color_abbreviation_respects_repetition_and_parser_scope(
    argv: list[str], colored: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli.main(argv) == 0
    assert ('\x1b[' in capsys.readouterr().out) == (colored and sys.version_info >= (3, 14))


@pytest.mark.parametrize('command', [[], ['status'], ['show'], ['repo', 'verify']])
@pytest.mark.parametrize('mode', ['always', 'never'])
def test_color_option_applies_to_help_at_every_command_level(
    command: list[str],
    mode: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_color = '0' if mode == 'always' else '1'
    monkeypatch.setenv('PYTHON_COLORS', original_color)
    assert _cli.main([*command, '--color', mode, '--help']) == 0
    assert os.environ['PYTHON_COLORS'] == original_color
    rendered = capsys.readouterr().out
    assert ('\x1b[' in rendered) == (mode == 'always' and sys.version_info >= (3, 14))
    plain = re.sub(r'\x1b\[[0-9;]*m', '', rendered)
    assert '--color {auto,always,never}' in plain
    assert '--verbose' in plain
    if sys.version_info >= (3, 14):
        assert bool(re.search(r'\x1b\[[0-9;]+m--color\b', rendered)) == (mode == 'always')


@pytest.mark.parametrize('mode', ['always', 'never'])
def test_color_option_controls_help_on_a_terminal(
    mode: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    monkeypatch.delenv('NO_COLOR', raising=False)
    assert _cli.main(['--color', mode, '--help']) == 0
    assert ('\x1b[' in capsys.readouterr().out) == (
        mode == 'always' and sys.version_info >= (3, 14)
    )


@pytest.mark.parametrize(
    ('command', 'expected'),
    [
        (
            ['prepare'],
            [
                '--source',
                '--replace',
                '--continue',
                '--version',
                '--message',
                '--acknowledge-new-paths',
                '--yes',
            ],
        ),
        (['release', 'create'], ['--title', '--notes-file', '--yes']),
        (['release', 'edit'], ['--title', '--notes-file', '--yes']),
        (['forget'], ['--force', '--yes']),
    ],
)
def test_command_help_groups_options_in_workflow_order(
    command: list[str], expected: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli.main([*command, '--color', 'never', '--help']) == 0
    text = capsys.readouterr().out
    heading = f'{" ".join(command)} options:\n'
    assert heading in text
    assert text.index('common options:\n') < text.index(heading)
    section = text.split(heading, 1)[1].split('\n\n', 1)[0]
    assert re.findall(r'^  (--[a-z-]+)\b', section, re.MULTILINE) == expected


@pytest.mark.parametrize(
    ('argv', 'handler_module', 'handler_name'),
    [
        (['setup'], _setup, 'setup'),
        (['doctor'], _setup, 'doctor'),
        (['init', '--project', 'demo', '--relocate'], _init, 'command_init'),
        (['status', '--project', 'demo', '--remote'], cli_status, 'command_status'),
        (
            [
                'prepare',
                '--project',
                'demo',
                '--version',
                '1.2.3',
                '--source',
                'HEAD',
                '--message',
                'release',
                '--acknowledge-new-paths',
                '--yes',
            ],
            _prepare,
            'command_prepare',
        ),
        (['inspect', '--published'], _inspect, 'command_inspect'),
        (['list', '--project', 'demo'], cli_history, 'command_list'),
        (['show', 'latest', '--project', 'demo'], cli_history, 'command_show'),
        (['delete', 'latest', '--project', 'demo', '--yes'], cli_history, 'command_delete'),
        (['clean', '--project', 'demo', '--yes'], cli_history, 'command_clean'),
        (['forget', '--project', 'demo', '--yes', '--force'], _forget, 'command_forget'),
        (['publish', '--project', 'demo', '--resolve', '--yes'], cli_remote, 'command_publish'),
        (['release', 'create', 'v1.2.3', '--notes-file', 'notes.md'], _releases, 'command_create'),
        (['release', 'edit', 'v1.2.3', '--title', 'New'], _releases, 'command_edit'),
        (['release', 'list'], _releases, 'command_list'),
        (['release', 'show', 'v1.2.3'], _releases, 'command_show'),
        (['discard', '--project', 'demo', '--force'], cli_history, 'command_discard'),
        (['repo', 'restore', '--project', 'demo', '--to', 'local'], cli_remote, 'repo_restore'),
        (['repo', 'restore', '--project', 'demo', '--to', 'remote'], cli_remote, 'repo_restore'),
        (['stage', 'push', '--project', 'demo', '--yes'], cli_remote, 'stage_push'),
        (['stage', 'delete', '--project', 'demo', '--yes'], cli_remote, 'stage_delete'),
        (['stage', 'forget', '--project', 'demo', '--yes'], cli_remote, 'stage_forget'),
        (['stage', 'watch', '--project', 'demo'], cli_remote, 'stage_watch'),
        (['repo', 'watch', '--project', 'demo'], cli_remote, 'repo_watch'),
        (['package', 'watch', '--project', 'demo'], cli_remote, 'package_watch'),
    ],
)
def test_command_surface_dispatches(
    argv: list[str], handler_module: ModuleType, handler_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[object, ...]] = []

    def _handler(*args: object, **_kwargs: object) -> None:
        seen.append(args)

    monkeypatch.setattr(handler_module, handler_name, _handler)

    assert _cli.main(argv) == 0
    assert len(seen) == 1


@pytest.mark.parametrize(
    'argv',
    [
        ['status', '--project', 'demo'],
        ['stage', 'status', '--project', 'demo'],
        ['repo', 'verify', '--project', 'demo'],
    ],
)
def test_configuration_readers_refuse_while_initialization_holds_project_lock(
    argv: list[str],
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _global_config, project = configured_project
    assert project is not None

    def _unexpected_load(_paths: _config.Paths, _name: str) -> _config.ProjectConfig:
        pytest.fail('configuration reader loaded a project during initialization')

    monkeypatch.setattr(_config, 'load_project', _unexpected_load)

    with _config.project_lock(paths, project.name):
        assert _cli.main(argv) == 1

    assert 'another pubanon command is running for this project' in capsys.readouterr().err


@pytest.mark.parametrize(
    ('argv', 'expected'),
    [
        (
            [],
            (
                'create, reveal, verify, or restore',
                "project's current state",
                'review copy',
                'saved snapshots',
                'release workflow',
                'GitHub login',
            ),
        ),
        (
            ['setup'],
            ('--edit', 'saved identity', 'terms', 'expected account', 'route', 'ignored files'),
        ),
        (
            ['init'],
            (
                'register a project',
                'destination',
                '--relocate',
                'also enables it for an existing project',
                '--yes',
                'verify existing configuration without prompts',
                'relocation requires interactive approval',
            ),
        ),
        (['inspect'], ('--refresh', '--published', 'review copy')),
        (['list'], ('--project', 'saved snapshots')),
        (['show'], ('SELECTOR', 'prepared', 'latest', 'release tag')),
        (['release'], ('GitHub release pages', 'create', 'edit', 'list', 'show')),
        (['release', 'create'], ('TAG', '--notes-file', '--title', '--yes')),
        (['release', 'edit'], ('TAG', '--notes-file', '--title', '--yes')),
        (['release', 'list'], ('--limit', 'default: 30')),
        (['release', 'show'], ('TAG', 'full notes')),
        (['delete'], ('SELECTOR', 'latest', 'selected local deletion')),
        (
            ['publish'],
            (
                '--resolve',
                'interrupted publication',
                '--yes',
            ),
        ),
        (['discard'], ('--force', 'abandon', 'resolve')),
        (['status'], ('--remote', 'with GitHub')),
        (
            ['audit'],
            (
                'published history',
                'current privacy rules',
                '--remote',
                'against GitHub',
            ),
        ),
        (
            ['prepare'],
            (
                '--source',
                'default: HEAD',
                '--message',
                '--acknowledge-new-paths',
                '--yes',
                'cannot',
            ),
        ),
        (['run'], ('name', '--version', '--acknowledge-new-paths', '--yes', 'cannot')),
        (
            ['repo', 'create'],
            ('create the final repository using the saved visibility', '--yes', 'without asking'),
        ),
        (['repo'], ('create, reveal, verify, or restore',)),
        (['repo', 'reveal'], ('make the final repository public',)),
        (
            ['repo', 'verify'],
            ('compare saved published history with the final repository',),
        ),
        (['repo', 'restore'], ('--to', 'remote', 'local', '--yes')),
        (['stage'], ('push, check, repair, forget, or delete private staging',)),
        (['stage', 'push'], ('--yes', 'cannot')),
        (['stage', 'forget'], ('--yes', 'forget without confirmation')),
        (['stage', 'delete'], ('--yes', 'delete without confirmation')),
        (['stage', 'watch'], ('recorded staged snapshot', '--timeout', 'seconds', '1800')),
        (
            ['repo', 'watch'],
            ('recorded published main', '--version', '--timeout', 'seconds', '1800'),
        ),
        (['package'], ('observe package publication',)),
        (
            ['package', 'watch'],
            ('numerically greatest', '--version', '--timeout', 'seconds', '1800'),
        ),
    ],
)
def test_consequential_help_describes_behavior(
    argv: list[str],
    expected: tuple[str, ...],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _cli.main([*argv, '--help']) == 0

    output = ' '.join(capsys.readouterr().out.split())
    for phrase in expected:
        assert phrase in output


@pytest.mark.parametrize(
    'argv',
    [
        ['-p', 'demo', 'show', 'latest'],
        ['show', 'latest', '-p', 'demo'],
        ['--project', 'other', 'show', 'latest', '--project', 'demo'],
    ],
)
def test_global_project_selection_reaches_show_handler(
    argv: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []

    def _handler(args: argparse.Namespace) -> None:
        seen.append(args.project)

    monkeypatch.setattr(cli_history, 'command_show', _handler)
    assert _cli.main(argv) == 0
    assert seen == ['demo']


@pytest.mark.parametrize(
    'argv',
    [
        ['show', 'latest', 'extra'],
    ],
)
def test_history_and_inspect_usage_errors_precede_side_effects(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _unexpected_dispatch(_args: argparse.Namespace) -> None:
        pytest.fail('invalid command dispatched')

    monkeypatch.setattr(_cli, '_dispatch', _unexpected_dispatch)
    assert _cli.main(argv) == 2
    assert 'error:' in capsys.readouterr().err


@pytest.mark.parametrize(
    'argv',
    [
        ['release'],
        ['release', 'create', 'v1.2.3', '--notes-file', 'notes', '--title', ''],
        ['release', 'edit', 'v1.2.3'],
        ['release', 'edit', 'v1.2.3', '--title', ''],
        ['release', 'list', '--limit', '0'],
        ['release', 'list', '--limit', 'invalid'],
        ['release', 'show'],
    ],
)
def test_release_usage_errors_precede_dispatch(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _unexpected_dispatch(_args: argparse.Namespace) -> None:
        pytest.fail('invalid release dispatched')

    monkeypatch.setattr(_cli, '_dispatch', _unexpected_dispatch)
    assert _cli.main(argv) == 2
    assert 'error:' in capsys.readouterr().err


@pytest.mark.parametrize('selector', [None, 'v1.2.3'])
def test_inspect_published_parser_permits_refresh(selector: str | None) -> None:
    argv = ['inspect', '--published', *([selector] if selector else []), '--refresh']
    args = build_parser().parse_args(argv)
    assert args.published == (selector or 'latest')
    assert args.refresh is True


@pytest.mark.parametrize('selector', [None, 'latest', 'v1.2.3', 'abcdef', 'all'])
def test_prepare_replacement_selector_reaches_handler(
    selector: str | None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[str | None, bool, str]] = []

    def _handler(args: argparse.Namespace) -> None:
        seen.append((args.replace, args.continue_rewrite, args.project))

    monkeypatch.setattr(_prepare, 'command_prepare', _handler)
    arguments = ['prepare', '--project', 'demo', '--replace']
    if selector is not None:
        arguments.append(selector)
    assert _cli.main(arguments) == 0
    assert seen == [(selector or 'latest', False, 'demo')]


@pytest.mark.parametrize(
    'options',
    [
        ['--replace'],
        ['--replace', 'latest'],
        ['--source', 'HEAD'],
        ['--version', '1.0.0'],
        ['--message', 'correction'],
        ['--acknowledge-new-paths'],
    ],
)
def test_prepare_rewrite_continue_rejects_construction_options(
    options: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _forbidden(_args: argparse.Namespace) -> None:
        pytest.fail('conflicting prepare options dispatched')

    monkeypatch.setattr(_cli, '_dispatch', _forbidden)
    assert _cli.main(['prepare', '--continue', *options]) == 2
    assert '--continue cannot be combined' in capsys.readouterr().err


@pytest.mark.parametrize('location', ['same', 'different', 'outside', 'unavailable'])
def test_project_guidance_omits_only_the_project_selected_by_the_working_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    location: str,
) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    paths, _global, _project = configure(tmp_path, monkeypatch, source)
    other = tmp_path / 'other'
    other.mkdir()
    _config.registry_set(paths, other, 'other')
    monkeypatch.chdir(
        source if location == 'same' else other if location == 'different' else tmp_path
    )

    def _status(_args: argparse.Namespace) -> None:
        next_step('pubanon inspect')
        next_step('pubanon show --project demo latest')

    def _no_floors() -> None:
        pass

    def _unavailable(_paths: _config.Paths, _cwd: Path) -> str | None:
        raise PubanonError('registry is unavailable')

    monkeypatch.setattr(cli_status, 'command_status', _status)
    monkeypatch.setattr(_cli._run, 'check_floors', _no_floors)
    if location == 'unavailable':
        monkeypatch.setattr(_config, 'registry_lookup', _unavailable)
    assert _cli.main(['status', '--project', 'demo']) == 0
    inspect_qualifier = '' if location == 'same' else ' --project=demo'
    show_qualifier = '' if location == 'same' else ' --project demo'
    assert capsys.readouterr().out == (
        f'Next: pubanon inspect{inspect_qualifier}\nNext: pubanon show{show_qualifier} latest\n'
    )


@pytest.mark.parametrize(
    'argv',
    [
        ['status', '--project', 'demo', '--remote'],
        ['repo', 'verify', '--project', 'demo'],
    ],
)
def test_remote_read_commands_gate_current_coordinates_before_host_access(
    argv: list[str],
    tmp_path: Path,
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    capsys: pytest.CaptureFixture[str],
) -> None:
    term = 'private-coordinate'
    paths, _global_config, project = configured_project
    assert project is not None
    _store.store_path(paths, project.name)
    _config.save_project(
        paths,
        replace(project, repo=f'public-{term}', extra_terms=(term,)),
    )

    assert _cli.main(argv) == 1

    rendered = capsys.readouterr().err
    assert 'repository name did not pass content checks' in rendered
    assert term in rendered
    assert not (tmp_path / 'fake-gh-calls.jsonl').exists()


@pytest.mark.parametrize(
    'argv',
    [
        ['status', '--project', 'demo', '--remote'],
        ['audit', '--project', 'demo', '--remote'],
        ['repo', 'verify', '--project', 'demo'],
    ],
)
def test_remote_read_only_commands_refuse_missing_store_without_creating_it(
    argv: list[str],
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    remote = tmp_path / 'host.git'
    subprocess.run(['git', 'init', '--bare', '-q', remote], check=True, env=git_env())
    destination = _host.Destination(
        adapter='github',
        owner=project.owner,
        repo=project.repo,
        url=remote.as_uri(),
        account=global_config.accounts['github'],
        visibility=project.visibility,
    )

    def _verified_destination(
        _paths: _config.Paths,
        _global: _config.GlobalConfig,
        _project: _config.ProjectConfig,
    ) -> _host.Destination:
        return destination

    monkeypatch.setattr(_host, 'verify_destination', _verified_destination)
    store = _store.candidate_store_path(paths, project.name)
    assert not store.exists()

    assert _cli.main(argv) == 1

    error = capsys.readouterr().err
    assert 'no saved published history was found' in error
    assert 'pubanon repo restore --project=demo --to local' in error
    assert not store.exists()


@pytest.mark.parametrize('trailing_command', [[], ['doctor']])
def test_root_version_uses_distribution_metadata_without_runtime_checks(
    trailing_command: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def _metadata(name: str) -> str:
        assert name == 'pubanon'
        return '9.8.7'

    def _unexpected() -> None:
        pytest.fail('version checked runtime tools')

    monkeypatch.setattr(importlib.metadata, 'version', _metadata)
    monkeypatch.setattr(_run, 'check_floors', _unexpected)
    assert _cli.main(['--version', *trailing_command]) == 0
    assert capsys.readouterr().out == 'pubanon 9.8.7\n'


@pytest.mark.parametrize('command', ['setup', 'doctor'])
@pytest.mark.parametrize('root_position', [False, True])
def test_global_commands_refuse_explicit_project(
    command: str, root_position: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ['--project', 'demo', command] if root_position else [command, '--project', 'demo']
    assert _cli.main(argv) == 2
    assert '--project' in capsys.readouterr().err


@pytest.mark.parametrize(
    ('argv', 'handler_module', 'handler_name'),
    [
        (['init', '--project', 'demo', '--yes'], _init, 'command_init'),
        (['repo', 'verify', '--project', 'demo'], cli_remote, 'verify_repo'),
    ],
)
def test_runtime_floor_refusal_prevents_repository_handler(
    argv: list[str],
    handler_module: ModuleType,
    handler_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _refuse_floors() -> None:
        raise PubanonError('runtime floor refusal')

    def _unexpected_handler(_args: argparse.Namespace) -> None:
        pytest.fail('repository handler ran below the runtime floor')

    monkeypatch.setattr('pubanon._run.check_floors', _refuse_floors)
    monkeypatch.setattr(handler_module, handler_name, _unexpected_handler)

    assert _cli.main(argv) == 1


@pytest.mark.parametrize(
    ('argv', 'stage_command', 'yes'),
    [
        (['stage', 'push', '--project', 'demo', '--yes'], 'push', True),
        (['stage', 'status', '--project', 'demo'], 'status', None),
        (['stage', 'delete', '--project', 'demo', '--yes'], 'delete', True),
        (['stage', 'forget', '--project', 'demo', '--yes'], 'forget', True),
        (['stage', 'repair', '--project', 'demo', '--yes'], 'repair', True),
    ],
)
def test_stage_parser_exposes_planned_subcommands(
    argv: list[str],
    stage_command: str,
    yes: bool | None,
) -> None:
    args = build_parser().parse_args(argv)

    assert args.stage_command == stage_command
    assert args.project == 'demo'
    assert getattr(args, 'yes', None) is yes


def test_unregistered_directory_names_the_problem_and_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)

    assert _cli.main(['status']) == 1

    error = capsys.readouterr().err
    assert 'error: this directory is not a registered project' in error
    assert 'pubanon init or pass --project' in error


@pytest.mark.parametrize(
    'argv',
    [
        ['--verbose', 'show', 'latest'],
        ['show', '--verbose', 'latest'],
        ['show', 'latest', '-v'],
    ],
)
def test_verbose_option_survives_nested_command_defaults(argv: list[str]) -> None:
    assert build_parser().parse_args(argv).verbose is True


@pytest.mark.parametrize(
    ('argv', 'expected'),
    [
        (
            ['stage', 'watch', '--timeout', '45'],
            {'timeout': 45},
        ),
        (
            ['repo', 'watch'],
            {'version': None, 'timeout': _watch.DEFAULT_TIMEOUT},
        ),
        (
            ['repo', 'watch', '--version', '1.2.3', '--timeout', '30'],
            {'version': '1.2.3', 'timeout': 30},
        ),
        (
            ['package', 'watch'],
            {'version': None, 'timeout': _watch.DEFAULT_TIMEOUT},
        ),
        (
            ['package', 'watch', '--version', '2.0.0', '--timeout', '60'],
            {'version': '2.0.0', 'timeout': 60},
        ),
    ],
)
def test_watch_parser_exposes_planned_options(
    argv: list[str],
    expected: dict[str, object],
) -> None:
    args = build_parser().parse_args([*argv, '--project', 'demo'])

    assert getattr(args, f'{argv[0]}_command') == argv[1]
    assert args.project == 'demo'
    assert {name: getattr(args, name) for name in expected} == expected


@pytest.mark.parametrize('timeout', ['not-an-integer', '0', '-5'])
def test_watch_timeout_refuses_non_positive_integer(
    timeout: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _cli.main(['repo', 'watch', '--timeout', timeout]) == 2
    assert '--timeout must be a positive integer number of seconds' in capsys.readouterr().err


@pytest.mark.parametrize(
    'argv',
    [
        ['publish', '--no-release-page', '--release-notes-file', 'notes'],
        ['publish', '--release-page', '--no-release-page'],
        ['publish', '--release-page', '--release-notes-file', 'notes'],
        ['publish', '--resolve', '--release-page'],
        ['publish', '--resolve', '--no-release-page'],
        ['publish', '--resolve', '--release-notes-file', 'notes'],
    ],
)
def test_publish_page_usage_errors_precede_dispatch(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _unexpected(_args: argparse.Namespace) -> None:
        pytest.fail('invalid publish dispatched')

    monkeypatch.setattr(_cli, '_dispatch', _unexpected)
    assert _cli.main(argv) == 2
    assert 'error:' in capsys.readouterr().err


@pytest.mark.parametrize(
    'argv',
    [
        ['run', 'ship', '--release-page', '--no-release-page'],
        ['run', 'ship', '--release-page', '--release-notes-file', 'notes'],
    ],
)
def test_run_page_usage_errors_precede_dispatch(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _unexpected(_args: argparse.Namespace) -> None:
        pytest.fail('invalid run dispatched')

    monkeypatch.setattr(_cli, '_dispatch', _unexpected)
    assert _cli.main(argv) == 2
    assert 'error:' in capsys.readouterr().err
