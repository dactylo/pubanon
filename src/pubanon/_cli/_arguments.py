"""Build and validate command-line arguments."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import argparse
import sys
from typing import TYPE_CHECKING, NoReturn, Protocol, cast, override

from pubanon._remote import _watch
from pubanon._terminal import (
    style,
)

if TYPE_CHECKING:
    from collections.abc import Callable


_COMMAND_HELP_GROUPS = (
    ('publish', ('prepare', 'inspect', 'publish', 'status', 'discard')),
    ('snapshots', ('list', 'show', 'delete', 'clean')),
    ('setup and repositories', ('setup', 'init', 'repo', 'doctor')),
    ('automation and verification', ('release', 'run', 'stage', 'watch')),
)


class _ArgumentParser(argparse.ArgumentParser):
    """Style command-line parser errors."""

    def _add_presentation_options(self, *, root: bool = False) -> None:
        """Register shared options without nested defaults hiding parent selections."""
        self.color = False
        self.add_argument(
            '-p',
            '--project',
            metavar='NAME',
            default=None if root else argparse.SUPPRESS,
            help='registered project name',
        )
        self.add_argument(
            '--color',
            choices=('auto', 'always', 'never'),
            default='auto' if root else argparse.SUPPRESS,
            help='control terminal color (default: auto)',
        )
        self.add_argument(
            '-v',
            '--verbose',
            action='store_true',
            default=False if root else argparse.SUPPRESS,
            help='show full identifiers, paths, and supporting details',
        )
        for action in self._actions:
            choices = action.choices
            if isinstance(choices, dict):
                for child in cast('dict[str, _ArgumentParser]', choices).values():
                    child._add_presentation_options()

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


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Style argparse section headings."""

    @override
    def start_section(self, heading: str | None) -> None:
        super().start_section(style(heading, 'heading') if heading is not None else None)

    def _add_command_groups(self, action: argparse.Action) -> None:
        """Render registered command descriptions in workflow groups."""
        commands = {item.dest: item for item in self._iter_indented_subactions(action)}
        for title, names in _COMMAND_HELP_GROUPS:
            self.start_section(title)
            self.add_arguments([commands[name] for name in names])
            self.end_section()


class _RootParser(_ArgumentParser):
    """Group top-level commands using argparse's action rendering."""

    @override
    def format_help(self) -> str:
        formatter = cast('_HelpFormatter', self._get_formatter())
        formatter.add_usage(self.usage, self._actions, self._mutually_exclusive_groups)
        formatter.add_text(self.description)
        for action in self._get_positional_actions():
            formatter._add_command_groups(action)
        formatter.start_section('options')
        formatter.add_arguments(self._get_optional_actions())
        formatter.end_section()
        formatter.add_text(self.epilog)
        return formatter.format_help()


def _add_project_parser(
    commands: _Commands,
    name: str,
    help_text: str,
) -> argparse.ArgumentParser:
    """Add one project command parser."""
    return commands.add_parser(
        name,
        help=help_text,
        description=help_text,
        formatter_class=_HelpFormatter,
    )


def _add_init_parser(commands: _Commands) -> None:
    """Add project initialization options."""
    init = _add_project_parser(
        commands,
        'init',
        'choose files and a GitHub destination; publication uses main',
    )
    init.add_argument(
        '--relocate', action='store_true', help='move the registered project to the current source'
    )
    init.add_argument(
        '--self-publication',
        action='store_true',
        help='publish Pubanon itself without blocking its own name',
    )
    init.add_argument(
        '--yes',
        action='store_true',
        help=(
            'verify existing configuration without prompts; '
            'relocation requires interactive approval'
        ),
    )


def _add_inspect_parser(commands: _Commands) -> None:
    """Add explicit inspection refresh and cleanup options."""
    inspect = _add_project_parser(
        commands,
        'inspect',
        'review a snapshot, published files, or preserved snapshot directories',
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
    inspect_mode = inspect.add_mutually_exclusive_group()
    inspect_mode.add_argument(
        '--clean',
        action='store_true',
        help='remove all project review directories',
    )
    inspect_mode.add_argument(
        '--refresh',
        action='store_true',
        help='restore the selected review directory from the saved snapshot',
    )


def _add_list_parser(commands: _Commands) -> None:
    """Add commands for locally known snapshots."""
    _add_project_parser(commands, 'list', 'list locally known snapshots')
    show = _add_project_parser(commands, 'show', 'show a snapshot and its source recovery')
    show.add_argument(
        'selector', metavar='SELECTOR', help='prepared, latest, release tag, or commit prefix'
    )
    deletion = _add_project_parser(
        commands, 'delete', 'delete one local snapshot, its review directory, and recovery'
    )
    deletion.add_argument(
        'selector', metavar='SELECTOR', help='prepared, latest, tag, or commit prefix'
    )
    deletion.add_argument('--yes', action='store_true', help='confirm selected local deletion')
    cleanup = _add_project_parser(commands, 'clean', 'remove superseded local snapshots')
    cleanup.add_argument('--yes', action='store_true', help='confirm superseded snapshot cleanup')


def _add_prepare_parser(commands: _Commands) -> None:
    """Add preparation and saved-rewrite continuation options."""
    prepare = _add_project_parser(commands, 'prepare', 'prepare a snapshot from committed files')
    prepare.add_argument(
        '--version',
        help='release version X.Y.Z (default: selected committed pyproject.toml version)',
    )
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


def _add_run_parser(commands: _Commands) -> None:
    """Add the declared-workflow parser."""
    run = _add_project_parser(commands, 'run', 'run one declared workflow')
    run.add_argument('name', help='configured workflow name')
    run.add_argument('--version', help='release version in X.Y.Z form')
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


def _add_watch_parsers(commands: _Commands) -> None:
    """Add the grouped observation command parsers."""
    watch = commands.add_parser(
        'watch',
        help='observe CI and package publication',
        description='observe CI and package publication',
        formatter_class=_HelpFormatter,
    )
    watch_commands = watch.add_subparsers(dest='watch_command', required=True)
    watch_ci = _add_project_parser(watch_commands, 'ci', 'wait for required CI checks')
    watch_ci.add_argument(
        '--staged', action='store_true', help='watch checks for the exact staged snapshot'
    )
    watch_ci.add_argument('--version', help='published release version to select')
    watch_ci.add_argument(
        '--timeout',
        type=_positive_timeout,
        default=_watch.DEFAULT_TIMEOUT,
        help=f'maximum wait in seconds (default: {_watch.DEFAULT_TIMEOUT})',
    )
    watch_package = _add_project_parser(
        watch_commands,
        'package',
        'wait for package publication',
    )
    watch_package.add_argument('--version', help='published package version to select')
    watch_package.add_argument(
        '--timeout',
        type=_positive_timeout,
        default=_watch.DEFAULT_TIMEOUT,
        help=f'maximum wait in seconds (default: {_watch.DEFAULT_TIMEOUT})',
    )


def build_parser() -> _RootParser:
    """Build the argument parser."""
    parser = _RootParser(
        prog='pubanon',
        formatter_class=_HelpFormatter,
        description=(
            'Initial workflow: setup -> init\nTypical workflow: prepare -> inspect -> publish'
        ),
    )
    commands = parser.add_subparsers(
        dest='command', required=True, metavar='COMMAND', parser_class=_ArgumentParser
    )
    setup = commands.add_parser(
        'setup',
        help='create, verify, or edit global configuration',
        description='create, verify, or edit global configuration',
        formatter_class=_HelpFormatter,
    )
    setup.add_argument(
        '--edit',
        action='store_true',
        help='review and correct saved identity, terms, or expected account',
    )
    commands.add_parser(
        'doctor',
        help='check configuration, Python, Git, and GitHub authentication',
        formatter_class=_HelpFormatter,
    )

    _add_init_parser(commands)

    status = _add_project_parser(commands, 'status', 'show current project status')
    status.add_argument(
        '--audit', action='store_true', help='audit published history under current privacy rules'
    )
    status.add_argument(
        '--remote', action='store_true', help='compare the recorded publication with GitHub'
    )

    _add_prepare_parser(commands)

    _add_inspect_parser(commands)
    _add_list_parser(commands)

    publish = _add_project_parser(commands, 'publish', 'publish the prepared snapshot')
    publish_mode = publish.add_mutually_exclusive_group()
    publish_mode.add_argument(
        '--resolve', action='store_true', help='check whether an interrupted publication completed'
    )
    publish_mode.add_argument(
        '--sync',
        action='store_true',
        help='restore saved snapshot history to an empty or older destination',
    )
    publish.add_argument(
        '--yes',
        action='store_true',
        help=(
            'publish without the ordinary confirmation; refuses if source changes '
            'or sensitive terms in earlier snapshots need your decision'
        ),
    )

    discard = _add_project_parser(
        commands, 'discard', 'discard prepared work and keep its review directories'
    )
    discard.add_argument(
        '--force',
        action='store_true',
        help='abandon recovery information needed to resolve an interrupted publication',
    )

    release = _add_project_parser(commands, 'release', 'prepare, inspect, and publish')
    release.add_argument('version', help='release version in X.Y.Z form')
    release.add_argument(
        '--yes',
        action='store_true',
        help=(
            'skip publication confirmation; cannot approve file choices, source changes, '
            'or earlier privacy findings'
        ),
    )

    _add_run_parser(commands)

    repo = commands.add_parser(
        'repo',
        help='create, reveal, or verify the GitHub repository',
        description='create, reveal, or verify the GitHub repository',
        formatter_class=_HelpFormatter,
    )
    repo_commands = repo.add_subparsers(dest='repo_command', required=True)
    create = _add_project_parser(
        repo_commands, 'create', 'create the GitHub repository using the saved visibility'
    )
    visibility = create.add_mutually_exclusive_group()
    visibility.add_argument(
        '--private',
        dest='visibility',
        action='store_const',
        const='private',
        help='require private visibility; must match the saved setting',
    )
    visibility.add_argument(
        '--public',
        dest='visibility',
        action='store_const',
        const='public',
        help='require public visibility; must match the saved setting',
    )
    create.add_argument(
        '--yes',
        action='store_true',
        help='create the repository without asking for confirmation',
    )
    _add_project_parser(repo_commands, 'reveal', 'make the GitHub repository public')
    verify = repo_commands.add_parser(
        'verify',
        help='compare saved snapshot history with GitHub',
        formatter_class=_HelpFormatter,
    )
    verify.add_argument(
        '--repair-store',
        action='store_true',
        help='replace saved snapshot history with verified GitHub history',
    )
    verify.add_argument(
        '--yes',
        action='store_true',
        help='replace saved snapshot history with --repair-store without confirmation',
    )

    stage = commands.add_parser(
        'stage',
        help='manage the private staging repository',
        description='push a snapshot, check its status, or delete the private staging repository',
        formatter_class=_HelpFormatter,
    )
    stage_commands = stage.add_subparsers(dest='stage_command', required=True)
    stage_push = _add_project_parser(stage_commands, 'push', 'push the prepared snapshot')
    stage_push.add_argument(
        '--yes',
        action='store_true',
        help='approve the staged push; cannot approve first-use repository creation',
    )
    _add_project_parser(stage_commands, 'status', 'show required checks for the staged commit')
    stage_clean = _add_project_parser(
        stage_commands,
        'clean',
        'delete the selected staging repository',
    )
    stage_clean.add_argument(
        '--yes',
        action='store_true',
        help=(
            'delete without confirmation; cannot approve abandoning evidence '
            'of an unverified deletion'
        ),
    )
    _add_watch_parsers(commands)
    parser._add_presentation_options(root=True)
    return parser


def validate_options(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """Reject conflicting modes before dispatch."""
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
    if args.command == 'inspect' and args.clean and args.published is not None:
        parser.error('--clean cannot be combined with --published')
    if (
        args.command == 'repo'
        and args.repo_command == 'verify'
        and args.yes
        and not args.repair_store
    ):
        parser.error('--yes requires --repair-store')
