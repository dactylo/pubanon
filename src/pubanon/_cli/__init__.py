"""Provide the pubanon command-line entry point."""

from __future__ import annotations

import os
import sys
from contextlib import ExitStack
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING

from pubanon import (
    _config,
    _run,
)
from pubanon._cli import (
    _arguments,
    _audit,
    _forget,
    _init,
    _inspect,
    _prepare,
    _releases,
    _setup,
    _status,
    _workflow,
)
from pubanon._cli import _history as cli_history
from pubanon._cli import _remote as cli_remote
from pubanon._cli._downstream import closable_stdout
from pubanon._errors import OperationCanceled, PubanonError
from pubanon._terminal import (
    color_context,
    diagnostic,
    guidance,
    guidance_context,
    output_encoding_context,
    verbose_context,
)

if TYPE_CHECKING:
    import argparse
    from collections.abc import Callable, Iterable, Sequence


def _group_handler(args: argparse.Namespace) -> Callable[[argparse.Namespace], None] | None:
    """Select one nested command handler."""
    if args.command == 'repo':
        return {
            'create': cli_remote.repo_create,
            'reveal': cli_remote.repo_reveal,
            'restore': cli_remote.repo_restore,
            'verify': cli_remote.verify_repo,
            'watch': cli_remote.repo_watch,
        }.get(args.repo_command)
    if args.command == 'stage':
        return {
            'delete': cli_remote.stage_delete,
            'forget': cli_remote.stage_forget,
            'push': cli_remote.stage_push,
            'repair': cli_remote.stage_repair,
            'status': cli_remote.stage_status,
            'watch': cli_remote.stage_watch,
        }.get(args.stage_command)
    if args.command == 'package':
        return {'watch': cli_remote.package_watch}.get(args.package_command)
    if args.command == 'release':
        return {
            'create': _releases.command_create,
            'edit': _releases.command_edit,
            'list': _releases.command_list,
            'show': _releases.command_show,
        }.get(args.release_command)
    return None


def _dispatch(args: argparse.Namespace) -> None:
    """Dispatch one parsed command."""

    def _dispatch_doctor(_args: argparse.Namespace) -> None:
        _setup.doctor()

    def _dispatch_setup(args: argparse.Namespace) -> None:
        _setup.setup(edit=args.edit)

    handlers: dict[str, Callable[[argparse.Namespace], None]] = {
        'audit': _audit.command_audit,
        'clean': cli_history.command_clean,
        'delete': cli_history.command_delete,
        'discard': cli_history.command_discard,
        'doctor': _dispatch_doctor,
        'forget': _forget.command_forget,
        'list': cli_history.command_list,
        'init': _init.command_init,
        'inspect': _inspect.command_inspect,
        'prepare': _prepare.command_prepare,
        'publish': cli_remote.command_publish,
        'run': _workflow.command_run,
        'setup': _dispatch_setup,
        'show': cli_history.command_show,
        'status': _status.command_status,
    }
    handler = (
        _group_handler(args)
        if args.command in {'repo', 'stage', 'package', 'release'}
        else handlers.get(args.command)
    )
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


def _print_installed_version() -> None:
    """Print the installed distribution version without loading project state."""
    try:
        installed_version = metadata.version('pubanon')
    except metadata.PackageNotFoundError as exc:
        raise PubanonError('installed distribution metadata for pubanon is unavailable') from exc
    print(f'pubanon {installed_version}')


def _main(argv: Sequence[str] | None = None) -> int:
    """Parse and dispatch one command, translating failures to process exit statuses."""
    with ExitStack() as contexts:
        try:
            parser = _arguments.build_parser()
            arguments = list(sys.argv[1:] if argv is None else argv)
            contexts.enter_context(color_context(parser.selected_color_mode(arguments)))
            args = parser.parse_args(arguments)
            contexts.enter_context(verbose_context(getattr(args, 'verbose', False)))
            if args.root_version:
                _print_installed_version()
            else:
                _arguments.validate_options(parser, args)
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
        except SystemExit as exc:
            return exc.code if isinstance(exc.code, int) else 2
        except OperationCanceled as error:
            print(diagnostic(error, stream=sys.stderr), file=sys.stderr)
            return 130
        except KeyboardInterrupt:
            error = OperationCanceled('Canceled.')
            print(diagnostic(error, stream=sys.stderr), file=sys.stderr)
            return 130
        except PubanonError as error:
            print(diagnostic(error, stream=sys.stderr), file=sys.stderr)
            return 1
    return 0


def _report_error(error: PubanonError) -> None:
    """Report a refusal with its diagnostic values."""
    print(diagnostic(error, stream=sys.stderr), file=sys.stderr)


def main(argv: Iterable[str] | None = None) -> int:
    """Run one command with consistent color policy, including help and errors.

    If a reader closes standard output early, the remaining output is discarded
    and the exit status reflects the work completed before the closure.
    Without a standard output stream at all, the command is refused before any
    other work.

    Args:
        argv: Arguments excluding the executable, or None for process
            arguments.

    Returns:
        Zero for success, one for refusal, two for usage errors, or 130 for
        interruption.
    """
    if sys.stdout is None:
        # Started with standard output closed: nothing can be delivered, and
        # the closed-stdout wrapper cannot wrap a missing stream.
        _report_error(PubanonError('standard output is unavailable'))
        return 1
    arguments = list(sys.argv[1:] if argv is None else argv)
    # Restoring the error policy flushes the real stream, so the wrapper must
    # first silence a closed descriptor: keep it inside the encoding context.
    with output_encoding_context(), closable_stdout():
        return _main(arguments)
