import os
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import overload

import pytest

from pubanon import _config

Script = Callable[[Sequence[str | tuple[str, str]]], None]


def make_global_config() -> _config.GlobalConfig:
    return _config.GlobalConfig(
        anon_name='Anonymous Publisher',
        anon_email='anonymous@example.invalid',
        accounts={'github': 'anonymous-account'},
        sensitive_terms=('Private Person',),
    )


def make_paths(root: Path) -> _config.Paths:
    return _config.derive_paths(
        {'XDG_CONFIG_HOME': str(root / 'config'), 'XDG_DATA_HOME': str(root / 'data')}
    )


def make_project_config(source_path: Path) -> _config.ProjectConfig:
    return _config.ProjectConfig(
        name='demo',
        source_path=source_path,
        host='github',
        owner='anonymous-account',
        repo='public-repo',
        visibility='private',
        allow=('**',),
        deny=(),
        secret_exceptions=(),
        extra_terms=(),
        hygiene_terms=(),
        self_publication=True,
        required_checks=(),
    )


@overload
def configure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: None = None,
    *,
    project: str = 'demo',
) -> tuple[_config.Paths, _config.GlobalConfig, None]: ...


@overload
def configure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: Path,
    *,
    project: str = 'demo',
) -> tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig]: ...


def configure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source: Path | None = None,
    *,
    project: str = 'demo',
) -> tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig | None]:
    config_root = tmp_path / 'config'
    data_root = tmp_path / 'data'
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(config_root))
    monkeypatch.setenv('XDG_DATA_HOME', str(data_root))
    paths = _config.derive_paths(os.environ)
    paths.config_dir.mkdir(parents=True)
    paths.global_config.write_text(
        'anon_name = "Anonymous Publisher"\n'
        'anon_email = "anonymous@example.invalid"\n'
        'sensitive_terms = ["Private Person"]\n'
        'default_publication_route = "staged"\n'
        '[accounts]\n'
        'github = "anonymous-account"\n'
    )
    global_config = _config.load_global(paths)
    if source is None:
        return paths, global_config, None
    project_config = replace(
        make_project_config(source.resolve()),
        name=project,
        publication_route='direct',
        release_page=_config.ReleasePageSettings(enabled=False),
        settings=_config.Settings(
            '',
            'main',
            issues=True,
            wiki=False,
            projects=False,
            pull_requests=False,
            discussions=False,
        ),
    )
    _config.save_project(paths, project_config)
    _config.registry_set(paths, source, project)
    return paths, global_config, project_config
