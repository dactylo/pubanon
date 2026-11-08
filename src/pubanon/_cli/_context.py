"""Resolve project configuration and local-state prerequisites."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pubanon import (
    _config,
)
from pubanon._errors import PubanonError
from pubanon._snapshots import _records, _store


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
    """Load one resolved project configuration.

    Raises:
        PubanonError: Global or project configuration is missing, unreadable,
            or invalid.
    """
    global_config = _config.load_global(paths)
    project = _config.load_project(paths, name)
    return ProjectContext(paths, global_config, project)


def project_name(paths: _config.Paths, selected: str | None) -> str:
    """Resolve an explicit or registered project name.

    Raises:
        PubanonError: The registry cannot be read, no project is registered, or
            the selected name is invalid.
    """
    name = selected or _config.registry_lookup(paths, Path.cwd())
    if name is None:
        raise PubanonError(
            'this directory is not a registered project',
            recovery='run pubanon init or pass --project',
        )
    return _config.require_project_name(name)


def refuse_retained_rewrite(
    paths: _config.Paths, project: str, record: _records.CandidateRecord | None
) -> None:
    """Keep automatic workflows from acting on a retained history replacement.

    Raises:
        PubanonError: Rewrite evidence is unreadable, or a retained history
            replacement requires explicit review or continuation.
    """
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


def require_advisory_store(paths: _config.Paths, project: str) -> None:
    """Refuse a read-only remote comparison without local published state.

    Raises:
        PubanonError: The project name is invalid or its local publication
            store is absent.
    """
    store = _store.candidate_store_path(paths, project)
    if not store.is_dir():
        raise PubanonError(
            'no saved published history was found',
            recovery=(
                'if this project has never been published, run pubanon prepare first; '
                'otherwise rebuild the history with pubanon repo restore --to local'
            ),
        )
