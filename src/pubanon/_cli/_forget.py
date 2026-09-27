"""Remove one project's local registration and data."""

import argparse
import os
from pathlib import Path

from pubanon import _config, _filesystem
from pubanon._cli import _context
from pubanon._errors import OperationCanceled, PubanonError
from pubanon._snapshots import _candidate
from pubanon._terminal import confirm, display_path, escape, field, heading, output, shell_command


def _check_worktrees(directory: Path, source: str) -> None:
    """Keep source worktrees registered until Git removes them."""
    worktrees = directory / 'source-worktrees'
    if not worktrees.exists() or worktrees.is_symlink() or worktrees.is_junction():
        return
    entries = sorted(worktrees.iterdir())
    if not entries:
        return
    commands = [
        (
            shell_command('git', '-C', source, 'worktree', 'remove', str(entry))
            if source != 'unknown'
            else None
        )
        or f'Use Git in the source repository to remove worktree {display_path(entry)}.'
        for entry in entries
    ]
    raise PubanonError(
        'source worktrees must be removed before forgetting the project',
        fact='\n'.join(display_path(entry) for entry in entries),
        recovery='\n'.join(commands),
    )


def _entries(directory: Path) -> list[Path]:
    """List project data while preserving its permanent locking infrastructure."""
    if not directory.exists():
        return []
    return sorted(entry for entry in directory.iterdir() if entry.name != 'project.lock')


def _remove_entries(entries: list[Path]) -> None:
    """Remove data entries without following symlinks or directory junctions."""
    for entry in entries:
        if entry.is_symlink():
            entry.unlink()
        elif entry.is_junction():
            entry.rmdir()
        elif entry.is_dir():
            _filesystem.remove_tree(entry)
        else:
            entry.unlink()


def command_forget(args: argparse.Namespace) -> None:
    """Preview and remove local project data while retaining its operation lock."""
    paths = _config.derive_paths(os.environ)
    selected = args.project if args.project is not None else _context.project_name(paths, None)
    name = _config.require_project_name(selected)
    directory = paths.data_dir / 'projects' / name
    configuration = paths.config_dir / 'projects' / f'{name}.toml'
    try:
        if (
            _config.registry_source(paths, name) is None
            and not configuration.exists()
            and not configuration.is_symlink()
            and not _entries(directory)
        ):
            output('Nothing to remove.')
            return
        with _config.project_lock(paths, name):
            registered_source = _config.registry_source(paths, name)
            source = registered_source
            entries = _entries(directory)
            if (
                source is None
                and not configuration.exists()
                and not configuration.is_symlink()
                and not entries
            ):
                output('Nothing to remove.')
                return
            _candidate.check_recovery_state(paths, name, force=args.force)
            try:
                project = _config.load_project(paths, name)
            except PubanonError:
                project = None
            source = source or (str(project.source_path) if project else 'unknown')
            _check_worktrees(directory, source)
            heading('Local project data to remove')
            print(field('Project', escape(name)))
            print(field('Source', display_path(Path(source)) if source != 'unknown' else source))
            if registered_source is not None:
                print(
                    field('Registry entry', f'{escape(name)} (in {display_path(paths.registry)})')
                )
            if configuration.exists() or configuration.is_symlink():
                print(field('Configuration', display_path(configuration)))
            for entry in entries:
                print(field('Data', display_path(entry)))
            print(
                field(
                    'Final repository',
                    escape(f'{project.owner}/{project.repo}') if project else 'unknown',
                )
            )
            print(
                field(
                    'Staging repository',
                    escape(f'{project.owner}/{_config.stage_repository(project)}')
                    if project
                    else 'unknown',
                )
            )
            print('The source repository and repositories on GitHub are kept.')
            if not args.yes and not confirm('Forget this project on this machine?'):
                raise OperationCanceled('Canceled. Nothing was removed.')
            _config.registry_remove(paths, name)
            configuration.unlink(missing_ok=True)
            _remove_entries(entries)
    except (OSError, RuntimeError) as exc:
        raise PubanonError(
            'cannot finish removing local project data',
            fact=escape(str(exc)),
            recovery=_context.project_command(name, 'forget', f'--project={name}'),
        ) from exc
    output('Forgot the local project data.', kind='success')
    print('Run pubanon init in the source to register the project again.')
    print('Run pubanon repo restore --to local to rebuild local published history from the host.')
