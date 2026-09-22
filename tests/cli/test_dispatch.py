import argparse
import io
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest

from pubanon import (
    _cli,
    _config,
)
from pubanon._cli import _arguments, _init, _inspect, _prepare, _setup, _workflow
from pubanon._cli import _history as cli_history
from pubanon._cli import _remote as cli_remote
from pubanon._cli import _status as cli_status
from pubanon._cli._arguments import build_parser
from pubanon._errors import PubanonError
from pubanon._remote import _host, _watch
from pubanon._snapshots import _candidate
from pubanon._terminal import ask, confirm, next_step
from tests._git import git_env
from tests._setup import configure


def test_abbreviated_color_after_separator_does_not_change_help_policy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _cli.main(['show', '--', '--colo=always']) == 1
    assert '\x1b[' not in capsys.readouterr().err


@pytest.mark.parametrize('command', [[], ['status'], ['show'], ['repo', 'verify']])
@pytest.mark.parametrize('option', ['--colo=always', '--colo'])
def test_abbreviated_color_applies_to_help_at_every_command_level(
    command: list[str], option: str, capsys: pytest.CaptureFixture[str]
) -> None:
    color = [option] if '=' in option else [option, 'always']
    assert _cli.main([*command, *color, '--help']) == 0
    assert '\x1b[' in capsys.readouterr().out


@pytest.mark.parametrize('command', [[], ['status'], ['repo', 'verify']])
def test_abbreviated_never_disables_color_on_help(
    command: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli.main([*command, '--colo=never', '--help']) == 0
    assert '\x1b[' not in capsys.readouterr().out


def test_ambiguous_color_prefix_is_left_to_argparse(capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli.main(['prepare', '--co=always', '--help']) == 2
    rendered = capsys.readouterr()
    assert 'ambiguous option' in rendered.err
    assert '\x1b[' not in rendered.err


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
    assert 'cancelled' in captured.err
    assert 'Traceback' not in captured.out + captured.err


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
    store = _candidate.store_path(paths, project.name)
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
    assert ('\x1b[' in capsys.readouterr().out) == colored


def test_color_operand_after_separator_does_not_change_help_policy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _cli.main(['show', '--', '--color=always']) == 1
    assert '\x1b[' not in capsys.readouterr().err


@pytest.mark.parametrize('command', [[], ['status'], ['show'], ['repo', 'verify']])
@pytest.mark.parametrize('mode', ['always', 'never'])
def test_color_option_applies_to_help_at_every_command_level(
    command: list[str], mode: str, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _cli.main([*command, '--color', mode, '--help']) == 0
    rendered = capsys.readouterr().out
    assert ('\x1b[' in rendered) == (mode == 'always')
    assert '--color {auto,always,never}' in rendered
    assert '--verbose' in rendered


@pytest.mark.parametrize('mode', ['always', 'never'])
def test_color_option_controls_help_on_a_terminal(
    mode: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    monkeypatch.delenv('NO_COLOR', raising=False)
    assert _cli.main(['--color', mode, '--help']) == 0
    assert ('\x1b[' in capsys.readouterr().out) == (mode == 'always')


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
        (['inspect', '--project', 'demo', '--clean'], _inspect, 'command_inspect'),
        (['inspect', '--published'], _inspect, 'command_inspect'),
        (['list', '--project', 'demo'], cli_history, 'command_list'),
        (['show', 'latest', '--project', 'demo'], cli_history, 'command_show'),
        (['delete', 'latest', '--project', 'demo', '--yes'], cli_history, 'command_delete'),
        (['clean', '--project', 'demo', '--yes'], cli_history, 'command_clean'),
        (['publish', '--project', 'demo', '--resolve', '--yes'], cli_remote, 'command_publish'),
        (['discard', '--project', 'demo', '--force'], cli_history, 'command_discard'),
        (['release', '1.2.3', '--project', 'demo', '--yes'], _workflow, 'command_release'),
        (['repo', 'verify', '--project', 'demo', '--repair-store'], cli_remote, 'verify_repo'),
        (['stage', 'push', '--project', 'demo', '--yes'], cli_remote, 'stage_push'),
        (['stage', 'clean', '--project', 'demo', '--yes'], cli_remote, 'stage_clean'),
        (['watch', 'ci', '--project', 'demo'], cli_remote, 'watch_ci'),
        (['watch', 'package', '--project', 'demo'], cli_remote, 'watch_package'),
    ],
)
def test_command_surface_dispatches(
    argv: list[str], handler_module: ModuleType, handler_name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[tuple[object, ...]] = []

    def _handler(*args: object) -> None:
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

    assert 'project has an active operation' in capsys.readouterr().err


@pytest.mark.parametrize(
    ('argv', 'expected'),
    [
        ([], ('create, reveal, or verify', 'show current project status')),
        (['setup'], ('--edit', 'correct saved identity, terms, or expected account')),
        (
            ['init'],
            (
                'publication uses',
                'main',
                '--relocate',
                '--yes',
                'verify existing configuration without prompts',
                'relocation requires interactive approval',
            ),
        ),
        (['inspect'], ('--clean', 'all project review directories', '--refresh', '--published')),
        (['list'], ('--project', 'locally known snapshots')),
        (['show'], ('SELECTOR', 'prepared', 'latest', 'release tag')),
        (['delete'], ('SELECTOR', 'prepared', 'selected local deletion')),
        (
            ['publish'],
            (
                '--resolve',
                'interrupted publication',
                '--sync',
                'saved snapshot history',
                '--yes',
            ),
        ),
        (['discard'], ('--force', 'abandon', 'resolve')),
        (['status'], ('--audit', 'current privacy rules', '--remote', 'with GitHub')),
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
        (['release'], ('version', '--yes', 'cannot')),
        (['run'], ('name', '--version', '--acknowledge-new-paths', '--yes', 'cannot')),
        (['repo', 'create'], ('--private', '--public', '--yes', 'without asking')),
        (['repo'], ('create, reveal, or verify',)),
        (
            ['repo', 'verify'],
            ('--repair-store', 'saved snapshot history', '--yes', 'without confirmation'),
        ),
        (['stage'], ('push a snapshot, check its status, or delete',)),
        (['stage', 'push'], ('--yes', 'cannot')),
        (['stage', 'clean'], ('--yes', 'cannot', 'evidence')),
        (['watch'], ('observe CI and package publication',)),
        (['watch', 'ci'], ('--staged', '--version', '--timeout', 'seconds', '1800')),
        (['watch', 'package'], ('--version', '--timeout', 'seconds', '1800')),
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


def test_explicit_color_applies_to_usage_errors(capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli.main(['--color', 'always', 'unknown-command']) == 2
    assert '\x1b[' in capsys.readouterr().err


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


@pytest.mark.parametrize('arguments', [['--help'], ['init', '--help']])
def test_help_styles_sections_on_terminal(
    arguments: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    assert _cli.main(arguments) == 0
    text = capsys.readouterr().out
    assert '\x1b[1;36moptions\x1b[0m:' in text
    if arguments == ['--help']:
        assert '\x1b[1;36msnapshots\x1b[0m:' in text
    assert '--help' in text


@pytest.mark.parametrize(
    'argv',
    [
        ['show', 'latest', 'extra'],
        ['inspect', '--published', '--clean'],
        ['inspect', '--published', 'latest', '--clean'],
        ['inspect', '--refresh', '--clean'],
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


@pytest.mark.parametrize('selector', [None, 'v1.2.3'])
def test_inspect_published_parser_permits_refresh(selector: str | None) -> None:
    argv = ['inspect', '--published', *([selector] if selector else []), '--refresh']
    args = build_parser().parse_args(argv)
    assert args.published == (selector or 'latest')
    assert args.refresh is True


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

    def _refuse() -> None:
        raise PubanonError('safe refusal', recovery='fix it')

    monkeypatch.setattr(_setup, 'setup', _refuse)
    monkeypatch.setattr('pubanon._run.check_floors', lambda: None)

    assert _cli.main(['setup']) == 1
    assert 'error: safe refusal\n  recovery: fix it' in capsys.readouterr().err


def test_parser_construction_error_is_reported(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def _broken_parser() -> argparse.ArgumentParser:
        raise PubanonError('parser setup failed')

    monkeypatch.setattr(_arguments, 'build_parser', _broken_parser)
    assert _cli.main(['--help']) == 1
    assert 'parser setup failed' in capsys.readouterr().err


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


def test_prepare_rewrite_continue_reaches_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[bool] = []

    def _handler(args: argparse.Namespace) -> None:
        seen.append(args.continue_rewrite)

    monkeypatch.setattr(_prepare, 'command_prepare', _handler)
    assert _cli.main(['prepare', '--project', 'demo', '--continue', '--yes']) == 0
    assert seen == [True]


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


def test_prepare_without_replacement_keeps_new_publication_mode() -> None:
    assert build_parser().parse_args(['prepare']).replace is None


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


def test_publish_resolve_and_sync_are_mutually_exclusive(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _cli.main(['publish', '--resolve', '--sync']) == 2
    assert 'not allowed with argument' in capsys.readouterr().err


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
    _candidate.store_path(paths, project.name)
    _config.save_project(
        paths,
        replace(project, repo=f'public-{term}', extra_terms=(term,)),
    )

    assert _cli.main(argv) == 1

    rendered = capsys.readouterr().err
    assert 'repository repo failed content gates' in rendered
    assert term in rendered
    assert not (tmp_path / 'fake-gh-calls.jsonl').exists()


@pytest.mark.parametrize(
    'argv',
    [
        ['status', '--project', 'demo', '--remote'],
        ['status', '--project', 'demo', '--audit', '--remote'],
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
    store = _candidate.candidate_store_path(paths, project.name)
    assert not store.exists()

    assert _cli.main(argv) == 1

    error = capsys.readouterr().err
    assert 'local advisory store is absent' in error
    assert 'pubanon repo verify --project=demo --repair-store' in error
    assert not store.exists()


def test_repo_publicize_is_an_unknown_command(capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli.main(['repo', 'publicize']) == 2
    assert "invalid choice: 'publicize'" in capsys.readouterr().err


def test_repo_reveal_has_no_yes_option(capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli.main(['repo', 'reveal', '--project', 'demo', '--yes']) == 2
    assert 'unrecognized arguments: --yes' in capsys.readouterr().err


def test_repo_reveal_help_exits_successfully() -> None:
    assert _cli.main(['repo', 'reveal', '--help']) == 0


def test_repo_verify_yes_requires_repair_store(capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli.main(['repo', 'verify', '--yes']) == 2
    assert '--yes requires --repair-store' in capsys.readouterr().err


def test_root_help_groups_every_command_once(capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli.main(['--help']) == 0
    text = capsys.readouterr().out
    assert (
        'Initial workflow: setup -> init\nTypical workflow: prepare -> inspect -> publish\n'
    ) in text
    groups = {
        'publish': ('prepare', 'inspect', 'publish', 'status', 'discard'),
        'snapshots': ('list', 'show', 'delete', 'clean'),
        'setup and repositories': ('setup', 'init', 'repo', 'doctor'),
        'automation and verification': ('release', 'run', 'stage', 'watch'),
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
    assert 'options:\n  -h, --help' in text


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


def test_setup_edit_parser_sets_edit_flag() -> None:
    args = build_parser().parse_args(['setup', '--edit'])

    assert args.command == 'setup'
    assert args.edit is True


@pytest.mark.parametrize(
    ('argv', 'stage_command', 'yes'),
    [
        (['stage', 'push', '--project', 'demo', '--yes'], 'push', True),
        (['stage', 'status', '--project', 'demo'], 'status', None),
        (['stage', 'clean', '--project', 'demo', '--yes'], 'clean', True),
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


def test_staged_watch_refuses_release_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert _cli.main(['watch', 'ci', '--staged', '--version', '1.0.0']) == 1
    assert 'the staging repository holds snapshots, not releases' in capsys.readouterr().err


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
            ['watch', 'ci'],
            {'staged': False, 'version': None, 'timeout': _watch.DEFAULT_TIMEOUT},
        ),
        (
            ['watch', 'ci', '--staged', '--timeout', '45'],
            {'staged': True, 'version': None, 'timeout': 45},
        ),
        (
            ['watch', 'ci', '--version', '1.2.3', '--timeout', '30'],
            {'staged': False, 'version': '1.2.3', 'timeout': 30},
        ),
        (
            ['watch', 'package'],
            {'version': None, 'timeout': _watch.DEFAULT_TIMEOUT},
        ),
        (
            ['watch', 'package', '--version', '2.0.0', '--timeout', '60'],
            {'version': '2.0.0', 'timeout': 60},
        ),
    ],
)
def test_watch_parser_exposes_planned_options(
    argv: list[str],
    expected: dict[str, object],
) -> None:
    args = build_parser().parse_args([*argv, '--project', 'demo'])

    assert args.watch_command == argv[1]
    assert args.project == 'demo'
    assert {name: getattr(args, name) for name in expected} == expected


@pytest.mark.parametrize('timeout', ['not-an-integer', '0', '-5'])
def test_watch_timeout_refuses_non_positive_integer(
    timeout: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _cli.main(['watch', 'ci', '--timeout', timeout]) == 2
    assert '--timeout must be a positive integer number of seconds' in capsys.readouterr().err
