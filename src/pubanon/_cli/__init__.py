"""Provide the Pubanon command-line entry point."""

from __future__ import annotations

import os
import sys
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING

from pubanon import (
    _config,
    _run,
)
from pubanon._cli import (
    _arguments,
    _init,
    _inspect,
    _prepare,
    _setup,
    _status,
    _workflow,
)
from pubanon._cli import _history as cli_history
from pubanon._cli import _remote as cli_remote
from pubanon._errors import OperationCancelled, PubanonError
from pubanon._terminal import (
    color_context,
    diagnostic,
    guidance,
    guidance_context,
    output_encoding_context,
)

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable, Iterable, Sequence


def _dispatch(args: argparse.Namespace) -> None:
    """Dispatch one parsed command."""

    def _dispatch_doctor(_args: argparse.Namespace) -> None:
        _setup.doctor()

    def _dispatch_setup(args: argparse.Namespace) -> None:
        if args.edit:
            _setup.setup(edit=True)
        else:
            _setup.setup()

    handlers: dict[str, Callable[[argparse.Namespace], None]] = {
        'clean': cli_history.command_clean,
        'delete': cli_history.command_delete,
        'discard': cli_history.command_discard,
        'doctor': _dispatch_doctor,
        'list': cli_history.command_list,
        'init': _init.command_init,
        'inspect': _inspect.command_inspect,
        'prepare': _prepare.command_prepare,
        'publish': cli_remote.command_publish,
        'release': _workflow.command_release,
        'run': _workflow.command_run,
        'setup': _dispatch_setup,
        'show': cli_history.command_show,
        'status': _status.command_status,
    }
    if args.command == 'repo':
        handler = {
            'create': cli_remote.repo_create,
            'reveal': cli_remote.repo_reveal,
            'verify': cli_remote.verify_repo,
        }.get(args.repo_command)
    elif args.command == 'stage':
        handler = {
            'clean': cli_remote.stage_clean,
            'push': cli_remote.stage_push,
            'status': cli_remote.stage_status,
        }.get(args.stage_command)
    elif args.command == 'watch':
        handler = {'ci': cli_remote.watch_ci, 'package': cli_remote.watch_package}.get(
            args.watch_command
        )
    else:
        handler = handlers.get(args.command)
    if handler is None:
        raise PubanonError('unsupported command')
    if args.command != 'setup':
        _run.check_floors()
    handler(args)


def _implicit_guidance_project(args: argparse.Namespace) -> str | None:
    """Omit redundant project advice when registry lookup is available."""
    if args.command in {'setup', 'doctor'}:
        return None
    try:
        paths = _config.derive_paths(os.environ)
        if not paths.registry.exists():
            return None
        return _config.registry_lookup(paths, Path.cwd())
    except PubanonError:
        # Advice must not add a registry dependency to explicit project selection.
        return None


def _report_error(error: PubanonError) -> None:
    """Report a refusal with its diagnostic values."""
    print(diagnostic(error, stream=sys.stderr), file=sys.stderr)


def _main(argv: Sequence[str] | None = None) -> int:
    """Parse and dispatch one command, translating failures to process exit statuses."""
    with ExitStack() as contexts:
        try:
            parser = _arguments.build_parser()
            arguments = list(sys.argv[1:] if argv is None else argv)
            contexts.enter_context(color_context(parser.selected_color_mode(arguments)))
            args = parser.parse_args(arguments)
            _arguments.validate_options(parser, args)
            try:
                selected = getattr(args, 'project', None)
                implicit = _implicit_guidance_project(args)
                with guidance_context(
                    project=selected,
                    implicit_project=implicit if selected in {None, implicit} else None,
                ):
                    try:
                        _dispatch(args)
                    except PubanonError as error:
                        if error.recovery is not None:
                            error.recovery = guidance(error.recovery)
                        raise
            except OperationCancelled:
                raise
            except PubanonError as error:
                _report_error(error)
                return 1
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 2
        except OperationCancelled as error:
            print(diagnostic(error, stream=sys.stderr), file=sys.stderr)
            return 130
        except KeyboardInterrupt:
            error = OperationCancelled('operation cancelled')
            print(diagnostic(error, stream=sys.stderr), file=sys.stderr)
            return 130
        except PubanonError as error:
            print(diagnostic(error, stream=sys.stderr), file=sys.stderr)
            return 1
    return 0


def main(argv: Iterable[str] | None = None) -> int:
    """Run one command with consistent color policy, including help and errors.

    Args:
        argv: Arguments excluding the executable, or None for process
            arguments.

    Returns:
        Zero for success, one for refusal, two for usage errors, or 130 for
        interruption.
    """
    arguments = list(sys.argv[1:] if argv is None else argv)
    with output_encoding_context():
        return _main(arguments)
