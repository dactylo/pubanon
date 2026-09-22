"""Locate and initialize project advisory bare stores."""

from pathlib import Path

from pubanon._config import Paths, create_data_dirs, require_project_name
from pubanon._errors import PubanonError
from pubanon._run import git_failure_fact, run_git


def candidate_store_path(paths: Paths, project: str) -> Path:
    """Return one project's advisory bare-store path without creating it.

    Args:
        paths: Derived Pubanon configuration and data paths.
        project: Registered project name.

    Returns:
        Path to the project's advisory bare Git repository.

    Raises:
        PubanonError: The project name is invalid.
    """
    require_project_name(project)
    return paths.data_dir / 'projects' / project / 'store.git'


def store_path(paths: Paths, project: str) -> Path:
    """Create and return one project's advisory bare store.

    Args:
        paths: Derived Pubanon configuration and data paths.
        project: Registered project name.

    Returns:
        Path to the initialized bare Git repository.

    Raises:
        PubanonError: The store cannot be initialized.
    """
    store = candidate_store_path(paths, project)
    if store.exists():
        return store
    create_data_dirs(paths, project)
    result = run_git(['init', '--bare', str(store)], cwd=None, check=False)
    if result.returncode != 0:
        raise PubanonError('cannot initialize snapshot store', fact=git_failure_fact(result))
    return store
