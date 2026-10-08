"""Build and validate command-line arguments."""

from __future__ import annotations

import argparse
import os
import sys
from contextlib import contextmanager
from typing import TYPE_CHECKING, NoReturn, Protocol, cast, override

from pubanon._remote import _watch
from pubanon._terminal import (
    color_enabled,
    style,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Generator


_COMMAND_GROUPS: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = (
    (
        'publish',
        (
            ('prepare', 'create a snapshot from committed files'),
            ('inspect', 'show snapshot details and where its review copy is'),
            ('publish', 'send the prepared snapshot to the final repository'),
            ('discard', 'drop the prepared snapshot and its review copies'),
            ('status', "show the project's current state"),
            ('run', 'run a declared release workflow'),
        ),
    ),
    (
        'local snapshots',
        (
            ('list', 'list saved snapshots'),
            ('show', 'show a snapshot and how to recover its source'),
            ('delete', 'delete a saved snapshot and its review copies'),
            ('clean', 'delete superseded snapshots and all review copies'),
            ('audit', 'check published history against current privacy rules'),
        ),
    ),
    (
        'remote repositories',
        (
            ('stage', 'manage the private staging repository'),
            ('repo', 'create, reveal, verify, or restore the final repository'),
            ('release', 'manage GitHub release pages in the final repository'),
            ('package', 'observe package publication'),
        ),
    ),
    (
        'setup',
        (
            ('setup', 'create, verify, or edit global configuration'),
            ('init', 'register a project and choose its files and destination'),
            ('forget', "remove a project's local pubanon data"),
            ('doctor', 'check configuration, tools, and GitHub login'),
        ),
    ),
)
_COMMAND_HELP = {name: text for _title, commands in _COMMAND_GROUPS for name, text in commands}


@contextmanager
def _help_color_context() -> Generator[None]:
    """Keep argparse's color override active through lazy help rendering."""
    previous = os.environ.get('PYTHON_COLORS')
    os.environ['PYTHON_COLORS'] = '1' if color_enabled() else '0'
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop('PYTHON_COLORS', None)
        else:
            os.environ['PYTHON_COLORS'] = previous


class _ArgumentParser(argparse.ArgumentParser):
    """Group command options and style parser errors."""

    def _add_common_options(self, *, root: bool = False, project: bool = True) -> None:
        """Register shared options without nested defaults hiding parent selections."""
        self._optionals.title = 'common options'
        if project:
            self.add_argument(
                '-p',
                '--project',
                metavar='NAME',
                default=None if root else argparse.SUPPRESS,
                help='registered project name',
            )
        self.add_argument(
            '-v',
            '--verbose',
            action='store_true',
            default=False if root else argparse.SUPPRESS,
            help='show full identifiers, paths, and supporting details',
        )
        self.add_argument(
            '--color',
            choices=('auto', 'always', 'never'),
            default='auto' if root else argparse.SUPPRESS,
            help='control terminal color (default: auto)',
        )
        if root:
            self.add_argument(
                '--version',
                dest='root_version',
                action='store_true',
                help='show the version and exit',
            )
        else:
            # Route subsequent options into the command's own native argument group.
            command = self.prog.removeprefix('pubanon ')
            self._optionals = self.add_argument_group(f'{command} options')

    def _resolved_option(self, option: str) -> argparse.Action | None:
        """Resolve an option using this parser's accepted long-option prefixes."""
        for action in self._actions:
            if option in action.option_strings:
                return action
        if not self.allow_abbrev or not option.startswith('--'):
            return None
        matches = [
            action
            for action in self._actions
            for name in action.option_strings
            if name.startswith('--') and name.startswith(option)
        ]
        return matches[0] if len(matches) == 1 else None

    @override
    def error(self, message: str) -> NoReturn:
        self.print_usage(sys.stderr)
        rendered = f'{self.prog}: error: {message}'
        self.exit(2, style(rendered, 'error', stream=sys.stderr) + '\n')

    @override
    @_help_color_context()
    def format_help(self, formatter: argparse.HelpFormatter | None = None) -> str:
        if sys.version_info >= (3, 15):
            return super().format_help(formatter=formatter)
        return super().format_help()

    @override
    @_help_color_context()
    def format_usage(self, formatter: argparse.HelpFormatter | None = None) -> str:
        if sys.version_info >= (3, 15):
            return super().format_usage(formatter=formatter)
        return super().format_usage()

    def selected_color_mode(self, arguments: list[str]) -> str:
        """Find the last color option accepted at its parser level."""
        mode = 'auto'
        parser = self
        index = 0
        while index < len(arguments):
            argument = arguments[index]
            if argument == '--':
                break
            if argument.startswith('-') and argument != '-':
                option, equals, explicit = argument.partition('=')
                action = parser._resolved_option(option)
                if action is None:
                    index += 1
                    continue
                if action.dest == 'color':
                    value = (
                        explicit
                        if equals
                        else arguments[index + 1]
                        if index + 1 < len(arguments)
                        else None
                    )
                    if value in {'auto', 'always', 'never'}:
                        mode = value
                if not equals and action.nargs is None:
                    index += 1
                index += 1
                continue
            for action in parser._actions:
                choices = action.choices
                if not action.option_strings and isinstance(choices, dict) and argument in choices:
                    parser = cast('_ArgumentParser', choices[argument])
                    break
            index += 1
        return mode


class _Commands(Protocol):
    """Describe the subparser operation used while building the CLI."""

    @property
    def add_parser(self) -> Callable[..., argparse.ArgumentParser]:
        """Return the subparser creation operation."""
        ...


class _RootParser(_ArgumentParser):
    """Group top-level commands using argparse's action rendering."""

    @override
    @_help_color_context()
    def format_help(self, formatter: argparse.HelpFormatter | None = None) -> str:
        if formatter is None:
            formatter = self._get_formatter()
        formatter.add_usage(self.usage, self._actions, self._mutually_exclusive_groups)
        formatter.add_text(self.description)
        for title, commands in _COMMAND_GROUPS:
            formatter.start_section(title)
            formatter.add_arguments(
                [argparse.Action([], name, help=text) for name, text in commands]
            )
            formatter.end_section()
        formatter.start_section('common options')
        formatter.add_arguments(self._get_optional_actions())
        formatter.end_section()
        formatter.add_text(self.epilog)
        return formatter.format_help()


def _add_command_parser(
    commands: _Commands,
    name: str,
    help_text: str,
    *,
    description: str | None = None,
    project: bool = True,
) -> argparse.ArgumentParser:
    """Add a command parser with common options registered first."""
    parser = cast(
        '_ArgumentParser',
        commands.add_parser(
            name,
            help=help_text,
            description=help_text if description is None else description,
            formatter_class=argparse.RawDescriptionHelpFormatter,
        ),
    )
    parser._add_common_options(project=project)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    return parser


def _add_init_parser(commands: _Commands) -> None:
    """Add project initialization options."""
    init = _add_command_parser(
        commands,
        'init',
        _COMMAND_HELP['init'],
    )
    init.add_argument(
        '--edit',
        action='store_true',
        help=(
            'review and change saved visibility, repository settings, route, '
            'CI workflow and required checks, release pages, and source tags'
        ),
    )
    init.add_argument(
        '--relocate', action='store_true', help='move the registered project to the current source'
    )
    init.add_argument(
        '--self-publication',
        action='store_true',
        help=(
            'publish pubanon itself without blocking its own name; '
            'also enables it for an existing project'
        ),
    )
    init.add_argument(
        '--publication-route',
        choices=['direct', 'staged'],
        help='choose direct upload or private staging before final publication',
    )
    source_tags = init.add_mutually_exclusive_group()
    source_tags.add_argument(
        '--source-tags',
        dest='source_tags',
        action='store_true',
        help='create local source tags for published versions',
    )
    source_tags.add_argument(
        '--no-source-tags',
        dest='source_tags',
        action='store_false',
        help='disable local source tags for published versions',
    )
    init.set_defaults(source_tags=None)
    init.add_argument(
        '--yes',
        action='store_true',
        help=(
            'verify existing configuration without prompts; '
            'relocation requires interactive approval'
        ),
    )


def _add_inspect_parser(commands: _Commands) -> None:
    """Add explicit inspection selection and refresh options."""
    inspect = _add_command_parser(
        commands,
        'inspect',
        _COMMAND_HELP['inspect'],
    )
    inspect.add_argument(
        '--published',
        nargs='?',
        const='latest',
        metavar='SELECTOR',
        help='inspect a published snapshot (default: latest), release tag, or commit prefix',
    )
    inspect.add_argument(
        '--rewritten',
        metavar='SELECTOR',
        help='inspect a replacement snapshot by moved tag or new commit prefix',
    )
    inspect.add_argument(
        '--refresh',
        action='store_true',
        help='restore the selected review copy from the saved snapshot',
    )


def _add_list_parser(commands: _Commands) -> None:
    """Add commands for locally known snapshots."""
    _add_command_parser(commands, 'list', _COMMAND_HELP['list'])
    show = _add_command_parser(commands, 'show', _COMMAND_HELP['show'])
    show.add_argument(
        'selector', metavar='SELECTOR', help='prepared, latest, release tag, or commit prefix'
    )
    deletion = _add_command_parser(commands, 'delete', _COMMAND_HELP['delete'])
    deletion.add_argument('selector', metavar='SELECTOR', help='latest, tag, or commit prefix')
    deletion.add_argument('--yes', action='store_true', help='confirm selected local deletion')
    cleanup = _add_command_parser(commands, 'clean', _COMMAND_HELP['clean'])
    cleanup.add_argument('--yes', action='store_true', help='remove without confirmation')


def _add_prepare_parser(commands: _Commands) -> None:
    """Add preparation and saved-rewrite continuation options."""
    prepare = _add_command_parser(commands, 'prepare', _COMMAND_HELP['prepare'])
    prepare.add_argument(
        '--source',
        default=None,
        metavar='REV',
        help='committed revision to prepare (working changes are excluded) (default: HEAD)',
    )
    prepare.add_argument(
        '--replace',
        nargs='?',
        const='latest',
        metavar='SELECTOR',
        help='correct a published release (default: latest); use all to start over',
    )
    prepare.add_argument(
        '--continue',
        dest='continue_rewrite',
        action='store_true',
        help='resume the saved publication rewrite',
    )
    prepare.add_argument(
        '--version',
        help='release version X.Y.Z (default: selected committed pyproject.toml version)',
    )
    prepare.add_argument('--message', metavar='MSG', help='publication commit message')
    prepare.add_argument(
        '--acknowledge-new-paths',
        action='store_true',
        help='approve all newly included files after reviewing the list',
    )
    prepare.add_argument(
        '--yes',
        action='store_true',
        help=(
            'prepare without ordinary confirmations; cannot approve file choices '
            'or newly included files'
        ),
    )


def _add_publish_parser(commands: _Commands) -> None:
    """Add publication options and mutually exclusive page overrides."""
    publish = _add_command_parser(commands, 'publish', _COMMAND_HELP['publish'])
    page = publish.add_mutually_exclusive_group()
    page.add_argument(
        '--release-page',
        action='store_true',
        help='create a release page for this publication even when release pages are off',
    )
    page.add_argument(
        '--no-release-page',
        action='store_true',
        help='publish without creating or looking up a release page',
    )
    page.add_argument(
        '--release-notes-file',
        metavar='FILE',
        help='use this UTF-8 Markdown file as the release page notes',
    )
    publish.add_argument(
        '--resolve', action='store_true', help='check whether an interrupted publication completed'
    )
    publish.add_argument(
        '--yes',
        action='store_true',
        help=(
            'publish without the ordinary confirmation; refuses if source changes '
            'or sensitive terms in earlier snapshots need your decision'
        ),
    )


def _add_run_parser(commands: _Commands) -> None:
    """Add the declared-workflow parser."""
    run = _add_command_parser(commands, 'run', _COMMAND_HELP['run'])
    run.add_argument('name', help='configured workflow name')
    run.add_argument('--version', help='release version in X.Y.Z form')
    page = run.add_mutually_exclusive_group()
    page.add_argument(
        '--release-page',
        action='store_true',
        help='create a release page for this publication even when release pages are off',
    )
    page.add_argument(
        '--no-release-page',
        action='store_true',
        help='publish without creating or looking up a release page',
    )
    page.add_argument(
        '--release-notes-file',
        metavar='FILE',
        help='use this UTF-8 Markdown file as the release page notes',
    )
    run.add_argument(
        '--acknowledge-new-paths',
        action='store_true',
        help='approve all newly included files after reviewing the list',
    )
    run.add_argument(
        '--yes',
        action='store_true',
        help=(
            'skip ordinary confirmations; cannot approve file choices, source changes, '
            'first staging creation, earlier privacy findings, or exposing Actions logs'
        ),
    )


def _positive_release_limit(value: str) -> int:
    """Parse a positive release-list count."""
    try:
        limit = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError('--limit must be a positive integer') from exc
    if limit <= 0:
        raise argparse.ArgumentTypeError('--limit must be a positive integer')
    return limit


def _add_release_parser(commands: _Commands) -> None:
    """Add explicit release-page creation, editing, and observation."""
    release = _add_command_parser(
        commands,
        'release',
        _COMMAND_HELP['release'],
    )
    subcommands = release.add_subparsers(dest='release_command', required=True)
    create = _add_command_parser(
        subcommands, 'create', 'create a page for an existing published tag'
    )
    create.add_argument('tag', metavar='TAG', help='existing vX.Y.Z publication tag')
    create.add_argument('--title', help='page title (default: release_page title, normally TAG)')
    create.add_argument(
        '--notes-file',
        metavar='FILE',
        help='UTF-8 Markdown notes file (default: release_page notes)',
    )
    create.add_argument('--yes', action='store_true', help='skip the confirmation prompt')
    edit = _add_command_parser(subcommands, 'edit', 'edit an existing page title or notes')
    edit.add_argument('tag', metavar='TAG', help='existing vX.Y.Z publication tag')
    edit.add_argument('--title', help='replacement page title')
    edit.add_argument('--notes-file', metavar='FILE', help='replacement UTF-8 Markdown notes')
    edit.add_argument('--yes', action='store_true', help='skip the confirmation prompt')
    listing = _add_command_parser(subcommands, 'list', 'list pages in the final repository')
    listing.add_argument(
        '--limit',
        type=_positive_release_limit,
        default=30,
        metavar='COUNT',
        help='maximum pages to show (default: 30)',
    )
    show = _add_command_parser(subcommands, 'show', 'show a page and its full notes')
    show.add_argument('tag', metavar='TAG', help='exact release tag')


def _positive_timeout(value: str) -> int:
    """Parse one positive timeout in seconds."""
    message = '--timeout must be a positive integer number of seconds'
    try:
        timeout = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(message) from exc
    if timeout <= 0:
        raise argparse.ArgumentTypeError(message)
    return timeout


def _add_watch_timeout(parser: argparse.ArgumentParser) -> None:
    """Add the shared wait limit to one observation command."""
    parser.add_argument(
        '--timeout',
        type=_positive_timeout,
        default=_watch.DEFAULT_TIMEOUT,
        help=f'maximum wait in seconds (default: {_watch.DEFAULT_TIMEOUT})',
    )


def _add_package_parser(commands: _Commands) -> None:
    """Add package observation under its target group."""
    package = _add_command_parser(
        commands,
        'package',
        _COMMAND_HELP['package'],
    )
    package_commands = package.add_subparsers(dest='package_command', required=True)
    package_watch = _add_command_parser(
        package_commands,
        'watch',
        'wait for the numerically greatest published version or an exact published version',
    )
    package_watch.add_argument('--version', help='exact published package version to select')
    _add_watch_timeout(package_watch)


def _add_repo_watch_parser(commands: _Commands) -> None:
    """Add checks on published main or an exact published tag."""
    repo_watch = _add_command_parser(
        commands,
        'watch',
        'wait for required checks on recorded published main or an exact published tag',
    )
    repo_watch.add_argument('--version', help='exact published release version to select')
    _add_watch_timeout(repo_watch)


def _add_stage_observation_parsers(commands: _Commands) -> None:
    """Add staging status and check observation in command order."""
    _add_command_parser(commands, 'status', 'show staging facts, tracking, and snapshot checks')
    _add_watch_timeout(
        _add_command_parser(commands, 'watch', 'wait for checks on the recorded staged snapshot')
    )


def build_parser() -> _RootParser:
    """Build the argument parser."""
    parser = _RootParser(
        prog='pubanon',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=(
            'Initial workflow: setup -> init\n'
            'Staged route: prepare -> inspect -> stage push -> stage watch -> publish\n'
            'Direct route: prepare -> inspect -> publish'
        ),
    )
    parser._add_common_options(root=True)  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
    commands = parser.add_subparsers(
        dest='command', metavar='COMMAND', parser_class=_ArgumentParser
    )
    setup = _add_command_parser(
        commands,
        'setup',
        _COMMAND_HELP['setup'],
        project=False,
    )
    setup.add_argument(
        '--edit',
        action='store_true',
        help=(
            'review and correct saved identity, terms, expected account, route, '
            'and ignored files or directories'
        ),
    )
    _add_command_parser(
        commands,
        'doctor',
        _COMMAND_HELP['doctor'],
        description='',
        project=False,
    )

    _add_init_parser(commands)
    forget = _add_command_parser(commands, 'forget', _COMMAND_HELP['forget'])
    forget.add_argument(
        '--force',
        action='store_true',
        help='abandon unreadable or unresolved publication evidence',
    )
    forget.add_argument('--yes', action='store_true', help='remove without confirmation')

    status = _add_command_parser(commands, 'status', _COMMAND_HELP['status'])
    status.add_argument(
        '--remote', action='store_true', help='compare the recorded publication with GitHub'
    )
    audit = _add_command_parser(commands, 'audit', _COMMAND_HELP['audit'])
    audit.add_argument(
        '--remote', action='store_true', help='verify the recorded history against GitHub'
    )

    _add_prepare_parser(commands)

    _add_inspect_parser(commands)
    _add_list_parser(commands)

    _add_publish_parser(commands)

    discard = _add_command_parser(commands, 'discard', _COMMAND_HELP['discard'])
    discard.add_argument(
        '--force',
        action='store_true',
        help='abandon recovery information needed to resolve an interrupted publication',
    )

    _add_run_parser(commands)
    _add_release_parser(commands)

    repo = _add_command_parser(
        commands,
        'repo',
        _COMMAND_HELP['repo'],
    )
    repo_commands = repo.add_subparsers(dest='repo_command', required=True)
    create = _add_command_parser(
        repo_commands, 'create', 'create or check an empty final repository using saved settings'
    )
    create.add_argument(
        '--yes',
        action='store_true',
        help='create the repository without asking for confirmation',
    )
    _add_command_parser(repo_commands, 'reveal', 'make the final repository public')
    _add_command_parser(
        repo_commands, 'verify', 'compare saved published history with the final repository'
    )
    restore = _add_command_parser(
        repo_commands, 'restore', 'restore remote or local saved publication history'
    )
    restore.add_argument(
        '--to',
        choices=('remote', 'local'),
        required=True,
        help=(
            'restore the remote repository from saved history or local saved history '
            'from the remote'
        ),
    )
    restore.add_argument(
        '--yes',
        action='store_true',
        help='restore the selected destination without ordinary confirmation',
    )
    _add_repo_watch_parser(repo_commands)

    stage = _add_command_parser(
        commands,
        'stage',
        _COMMAND_HELP['stage'],
        description='push, check, repair, forget, or delete private staging',
    )
    stage_commands = stage.add_subparsers(dest='stage_command', required=True)
    stage_push = _add_command_parser(stage_commands, 'push', 'push the prepared snapshot')
    stage_push.add_argument(
        '--yes',
        action='store_true',
        help='approve the staged push; cannot approve first-use repository creation',
    )
    stage_push.add_argument(
        '--full-tests',
        action='store_true',
        help='run all tests on local runners (workflow input full_tests)',
    )
    stage_push.add_argument(
        '--hosted',
        action='store_true',
        help='run CI on GitHub-hosted runners (workflow input hosted)',
    )
    _add_stage_observation_parsers(stage_commands)
    stage_repair = _add_command_parser(stage_commands, 'repair', 'repair staging tracking')
    stage_repair.add_argument('--yes', action='store_true', help='approve the previewed repair')
    stage_forget = _add_command_parser(
        stage_commands, 'forget', 'remove local staged-candidate tracking'
    )
    stage_forget.add_argument('--yes', action='store_true', help='forget without confirmation')
    stage_delete = _add_command_parser(
        stage_commands, 'delete', 'delete the configured private staging repository'
    )
    stage_delete.add_argument(
        '--yes',
        action='store_true',
        help='delete without confirmation',
    )
    stage_commands.metavar = '{' + ','.join(stage_commands.choices) + '}'
    _add_package_parser(commands)
    return parser


def validate_options(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject conflicting modes before dispatch."""
    if args.command is None:
        parser.error('the following arguments are required: COMMAND')
    if args.command in {'setup', 'doctor'} and args.project is not None:
        parser.error('--project cannot be used with a global command')
    if (
        args.command == 'init'
        and args.edit
        and (
            args.relocate
            or args.self_publication
            or args.publication_route is not None
            or args.source_tags is not None
            or args.yes
        )
    ):
        parser.error('--edit cannot be combined with other init options')
    if (
        args.command == 'publish'
        and args.resolve
        and (args.release_page or args.no_release_page or args.release_notes_file is not None)
    ):
        parser.error('--resolve cannot be combined with release page options')
    if args.command == 'release':
        if args.release_command in {'create', 'edit'} and args.title == '':
            parser.error('--title must not be empty')
        if args.release_command == 'edit' and args.title is None and args.notes_file is None:
            parser.error('release edit requires --title or --notes-file')
    if (
        args.command == 'prepare'
        and args.continue_rewrite
        and (
            args.replace is not None
            or args.source is not None
            or args.version is not None
            or args.message is not None
            or args.acknowledge_new_paths
        )
    ):
        parser.error('--continue cannot be combined with construction options')
