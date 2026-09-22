import os
from pathlib import Path

import pytest

from pubanon import _config


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
        '[accounts]\n'
        'github = "anonymous-account"\n'
    )
    global_config = _config.load_global(paths)
    if source is None:
        return paths, global_config, None
    project_config = _config.ProjectConfig(
        name=project,
        source_path=source.resolve(),
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
        settings=_config.Settings('', 'main', issues=True, wiki=False, projects=False),
    )
    _config.save_project(paths, project_config)
    _config.registry_set(paths, source, project)
    return paths, global_config, project_config
