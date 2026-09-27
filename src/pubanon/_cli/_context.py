"""Resolve project configuration and local-state prerequisites."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pubanon import (
    _config,
)
from pubanon._errors import PubanonError
from pubanon._remote import _host
from pubanon._snapshots import _candidate, _records
from pubanon._terminal import (
    escape,
    shell_command,
)


@dataclass(frozen=True)
class ProjectContext:
    """Bundle one resolved project's immutable local configuration.

    Attributes:
        paths: Locations of the local configuration and data roots.
        global_config: Saved global identity and policy configuration.
        project: Saved or proposed configuration for the selected project.
    """

    paths: _config.Paths
    global_config: _config.GlobalConfig
    project: _config.ProjectConfig


def load_context(paths: _config.Paths, name: str) -> ProjectContext:
    """Load one resolved project configuration."""
    global_config = _config.load_global(paths)
    project = _config.load_project(paths, name)
    return ProjectContext(paths, global_config, project)


def local_refs(ctx: ProjectContext) -> tuple[str | None, dict[str, str]]:
    """Read main and tags from the advisory store without creating it."""
    return _candidate.published_refs(ctx.paths, ctx.project.name)


def project_command(name: str, *args: str) -> str:
    """Render a project command or direct recovery to the selected project."""
    command = shell_command('pubanon', *args)
    if command is None:
        action = (
            ' '.join(args[:2])
            if args[0] in {'repo', 'stage', 'package', 'review', 'release'}
            else args[0]
        )
        modes = ' '.join(arg for arg in args if arg in {'--continue', '--resolve'})
        suffix = f' {modes}' if modes else ''
        return (
            f'Choose project {escape(name)} from its registered directory, '
            f'then rerun {action}{suffix}.'
        )
    return command


def project_name(paths: _config.Paths, selected: str | None) -> str:
    """Resolve an explicit or registered project name."""
    name = selected or _config.registry_lookup(paths, Path.cwd())
    if name is None:
        raise PubanonError(
            'this directory is not a registered project',
            recovery='run pubanon init or pass --project',
        )
    return _config.require_project_name(name)


def refuse_retained_rewrite(
    paths: _config.Paths, project: str, record: _candidate.CandidateRecord | None
) -> None:
    """Keep automatic workflows from acting on a retained history replacement."""
    rewriting = _records.load_rewrite(paths, project) is not None
    if not rewriting and (record is None or record.replaces is None):
        return
    if rewriting or (record is not None and record.tip is None):
        raise PubanonError(
            'a publication rewrite is incomplete',
            recovery='run `pubanon prepare --continue` to finish the rewrite',
        )
    raise PubanonError(
        'a publication rewrite is ready for review',
        recovery='run `pubanon inspect`, then follow its next step',
    )


def refuse_unexpected_branches(refs: _host.RemoteRefs) -> None:
    """Refuse branch drift at one CLI policy boundary."""
    if refs.branches:
        raise PubanonError(
            'destination contains an unexpected branch',
            expected=escape(_host.remote_refs_text(refs._replace(branches=()))),
            actual=escape(_host.remote_refs_text(refs)),
            recovery='remove every destination branch except main, then rerun',
        )


def require_advisory_store(paths: _config.Paths, project: str) -> None:
    """Refuse a read-only remote comparison without local published state."""
    store = _candidate.candidate_store_path(paths, project)
    if not store.is_dir():
        raise PubanonError(
            'no saved published history was found',
            recovery='pubanon repo restore --to local',
        )
