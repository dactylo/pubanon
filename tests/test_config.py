import json
import os
import subprocess
import sys
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Never, cast

import pytest

from pubanon import _config, _filesystem, _locking
from pubanon._errors import PubanonError
from pubanon._gate import GateHit, RuleClass
from pubanon._terminal import escape
from pubanon._toml import toml_string
from tests._permissions import assert_private_mode


def _global_config(accounts: dict[str, str] | None = None) -> _config.GlobalConfig:
    return _config.GlobalConfig(
        anon_name='Public Name',
        anon_email='public@example.invalid',
        accounts={'github': 'anonymous-account'} if accounts is None else accounts,
        sensitive_terms=('Real Name', 'real@example.test'),
    )


def _identity_configs() -> tuple[_config.GlobalConfig, _config.ProjectConfig]:
    g = _config.GlobalConfig(
        anon_name='Pat Example',
        anon_email='pat@example.com',
        accounts={'github': 'pat-example'},
        sensitive_terms=('global-term', 'shared-term'),
    )
    p = _config.ProjectConfig(
        name='demo',
        source_path=Path('/source'),
        host='github',
        owner='pat-example',
        repo='demo',
        visibility='private',
        allow=('**',),
        deny=(),
        secret_exceptions=(),
        extra_terms=('project-term', 'shared-term'),
        hygiene_terms=('draft',),
        self_publication=False,
        required_checks=(),
    )
    return g, p


def _identity_hit(term: str, rule_class: RuleClass = RuleClass.IDENTITY_CRITICAL) -> GateHit:
    return GateHit('publication identity', rule_class, term)


def _project_config(
    tmp_path: Path,
    *,
    source_path: Path | None = None,
    self_publication: bool = False,
) -> _config.ProjectConfig:
    return _config.ProjectConfig(
        name='demo',
        source_path=tmp_path / 'source' if source_path is None else source_path,
        host='github',
        owner='anonymous-account',
        repo='public-repo',
        visibility='private',
        allow=('src/**',),
        deny=('notes/**',),
        secret_exceptions=('tests/fixture.key',),
        extra_terms=('private-product',),
        hygiene_terms=('internal-marker',),
        self_publication=self_publication,
        required_checks=('test',),
        settings=_config.Settings(
            description='Published project',
            default_branch='main',
            issues=True,
            wiki=False,
            projects=True,
        ),
    )


def _project_toml(source_path: Path, *, settings: bool = True) -> str:
    return (
        'name = "demo"\n'
        f'source_path = {toml_string(str(source_path))}\n'
        'host = "github"\n'
        'owner = "anonymous-account"\n'
        'repo = "public-repo"\n'
        'visibility = "private"\n'
        'allow = ["src/**"]\n'
        'deny = ["notes/**"]\n'
        'secret_exceptions = ["tests/fixture.key"]\n'
        'extra_terms = ["private-product"]\n'
        'hygiene_terms = ["internal-marker"]\n'
        'self_publication = false\n'
        'required_checks = ["test"]\n'
        + (
            'settings = { description = "Published project", default_branch = "main", '
            'issues = true, wiki = false, projects = true }\n'
            if settings
            else ''
        )
    )


def _write_global_config(
    tmp_path: Path,
    *,
    anon_email: str = 'public@example.invalid',
    anon_name: str = 'Public Name',
    sensitive_terms: list[str] | None = None,
) -> _config.Paths:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    paths.config_dir.mkdir(parents=True)
    terms = ['secret'] if sensitive_terms is None else sensitive_terms
    rendered_terms = ', '.join(toml_string(term) for term in terms)
    paths.global_config.write_text(
        f'anon_name = {toml_string(anon_name)}\n'
        f'anon_email = {toml_string(anon_email)}\n'
        f'sensitive_terms = [{rendered_terms}]\n'
        '[accounts]\n'
        'github = "anonymous-account"\n'
    )
    return paths


def _write_project_config(tmp_path: Path, **overrides: list[str]) -> _config.Paths:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project_file = paths.config_dir / 'projects' / 'proj.toml'
    project_file.parent.mkdir(parents=True, exist_ok=True)
    config = _project_toml(tmp_path / 'source').replace('name = "demo"', 'name = "proj"')
    for key, terms in overrides.items():
        rendered_terms = ', '.join(f'"{term}"' for term in terms)
        config = config.replace(
            f'{key} = ["private-product"]',
            f'{key} = [{rendered_terms}]',
        ).replace(
            f'{key} = ["internal-marker"]',
            f'{key} = [{rendered_terms}]',
        )
    project_file.write_text(config)
    return paths


def test_assemble_rules_blocks_branding_and_all_configured_terms_by_default(
    tmp_path: Path,
) -> None:

    assert _config.assemble_rules(_global_config(), _project_config(tmp_path), ('/private',)) == {
        RuleClass.IDENTITY_CRITICAL: (
            'Real Name',
            'real@example.test',
            'private-product',
            '/private',
        ),
        RuleClass.HYGIENE: ('pubanon', 'internal-marker'),
    }


def test_assemble_rules_self_publication_lifts_only_builtin_branding(tmp_path: Path) -> None:

    project = _project_config(tmp_path, self_publication=True)

    assert _config.assemble_rules(_global_config(), project, ('/private',)) == {
        RuleClass.IDENTITY_CRITICAL: (
            'Real Name',
            'real@example.test',
            'private-product',
            '/private',
        ),
        RuleClass.HYGIENE: ('internal-marker',),
    }


@pytest.mark.parametrize('role', ['global', 'project'])
@pytest.mark.parametrize('failure', ['missing', 'permission denied', 'malformed TOML'])
def test_config_failure_identifies_file_role_and_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, role: str, failure: str
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path / 'private-home')})
    location = paths.global_config if role == 'global' else paths.config_dir / 'projects/demo.toml'
    location.parent.mkdir(parents=True)
    if failure == 'permission denied':

        def _deny(_path: Path, *_args: object, **_kwargs: object) -> Never:
            raise PermissionError('private-parser-canary')

        monkeypatch.setattr(Path, 'read_text', _deny)
    elif failure == 'malformed TOML':
        location.write_text('anon_name = "private-parser-canary"\nbroken = ?\n')

    def _load() -> None:
        if role == 'global':
            _config.load_global(paths)
        else:
            _config.load_project(paths, 'demo')

    with pytest.raises(PubanonError) as error:
        _load()
    rendered = error.value.render()
    assert escape(str(location)) in rendered
    assert f'{role} configuration' in rendered
    assert failure in rendered
    assert 'private-parser-canary' not in rendered
    if failure == 'malformed TOML':
        assert 'line 2, column 10' in rendered


def test_configuration_loads_use_explicit_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    source = tmp_path / 'source'
    source.mkdir()
    project = replace(
        _project_config(tmp_path, source_path=source),
        extra_terms=('Jos\u00e9 project term',),
    )
    paths.config_dir.mkdir(parents=True)
    paths.global_config.write_text(
        'anon_name = "Public Name"\n'
        'anon_email = "public@example.invalid"\n'
        'sensitive_terms = ["Jos\u00e9"]\n'
        '[accounts]\n'
        'github = "anonymous-account"\n',
        encoding='utf-8',
    )
    _config.save_project(paths, project)
    _config.registry_set(paths, source, project.name)
    original_read_text = Path.read_text
    calls: list[tuple[Path, str | None]] = []

    def _tracked_read_text(
        self: Path,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> str:
        calls.append((self, encoding))
        return original_read_text(self, encoding=encoding, errors=errors, newline=newline)

    monkeypatch.setattr(Path, 'read_text', _tracked_read_text)

    global_config = _config.load_global(paths)
    assert _config.load_project(paths, project.name) == project
    assert _config.registry_lookup(paths, source) == project.name
    assert global_config.sensitive_terms == ('Jos\u00e9',)
    assert project.extra_terms == ('Jos\u00e9 project term',)
    assert calls == [
        (paths.global_config, 'utf-8'),
        (paths.config_dir / 'projects' / 'demo.toml', 'utf-8'),
        (paths.registry, 'utf-8'),
    ]


@pytest.mark.parametrize('name', ['', '.', '..', 'a/b', '/abs'])
def test_create_data_dirs_refuses_invalid_project_name(tmp_path: Path, name: str) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path / 'home')})

    with pytest.raises(PubanonError, match='invalid project name'):
        _config.create_data_dirs(paths, name)

    assert not paths.data_dir.parent.exists()


@pytest.mark.parametrize('name', [None, 'proj'])
def test_create_data_dirs_restricts_every_managed_level(tmp_path: Path, name: str | None) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path / 'home')})
    outside_parent = paths.data_dir.parent
    levels = [paths.data_dir]
    if name is not None:
        levels.extend((paths.data_dir / 'projects', paths.data_dir / 'projects' / name))
    levels[-1].mkdir(parents=True)
    outside_parent.chmod(0o755)
    outside_mode = outside_parent.stat().st_mode
    for level in levels:
        level.chmod(0o755)

    deepest = _config.create_data_dirs(paths, name)

    assert deepest == levels[-1]
    assert outside_parent.stat().st_mode == outside_mode
    for level in levels:
        assert_private_mode(level, 0o700)


def test_derive_paths_refuses_relative_xdg_config_home(tmp_path: Path) -> None:

    with pytest.raises(PubanonError, match=r'XDG_CONFIG_HOME.*absolute'):
        _config.derive_paths(
            {
                'HOME': str(tmp_path / 'home'),
                'XDG_CONFIG_HOME': 'relative',
                'XDG_DATA_HOME': str(tmp_path / 'data'),
            }
        )


def test_derive_paths_refuses_relative_xdg_data_home(tmp_path: Path) -> None:

    with pytest.raises(PubanonError, match=r'XDG_DATA_HOME.*absolute'):
        _config.derive_paths(
            {
                'HOME': str(tmp_path / 'home'),
                'XDG_CONFIG_HOME': str(tmp_path / 'config'),
                'XDG_DATA_HOME': 'relative',
            }
        )


def test_derive_paths_treats_empty_environment_values_as_absent() -> None:
    home = Path('~').expanduser()

    paths = _config.derive_paths({'HOME': '', 'XDG_CONFIG_HOME': '', 'XDG_DATA_HOME': ''})

    assert paths == _config.Paths(
        home / '.config' / 'pubanon',
        home / '.local' / 'share' / 'pubanon',
        home / '.config' / 'pubanon' / 'config.toml',
        home / '.local' / 'share' / 'pubanon' / 'registry.json',
    )
    assert all(path.is_absolute() for path in paths)


def test_derive_paths_uses_xdg_fallbacks(tmp_path: Path) -> None:

    home = tmp_path / 'home'

    assert _config.derive_paths({'HOME': str(home)}) == _config.Paths(
        home / '.config' / 'pubanon',
        home / '.local' / 'share' / 'pubanon',
        home / '.config' / 'pubanon' / 'config.toml',
        home / '.local' / 'share' / 'pubanon' / 'registry.json',
    )


def test_derived_private_terms_include_known_private_paths(tmp_path: Path) -> None:

    home = tmp_path / 'home'
    config_root = tmp_path / 'private-config'
    data_root = tmp_path / 'private-data'
    source = tmp_path / 'private-source'
    paths = _config.derive_paths(
        {
            'HOME': str(home),
            'XDG_CONFIG_HOME': str(config_root),
            'XDG_DATA_HOME': str(data_root),
        }
    )
    project = _project_config(tmp_path, source_path=source)

    machine_terms = _config.machine_private_terms(paths, {'HOME': str(home)})

    assert machine_terms == (str(home), str(paths.config_dir), str(paths.data_dir))
    assert _config.derived_private_terms(paths, project, {'HOME': str(home)}) == (
        *machine_terms,
        str(source),
    )


def test_derived_private_terms_resolution_failure_is_path_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    private_source = tmp_path / 'private-source'
    paths = _config.derive_paths({'HOME': str(tmp_path / 'home')})
    project = _project_config(tmp_path, source_path=private_source)

    def _fail_resolve(path: Path, *_args: object, **_kwargs: object) -> Never:
        raise RuntimeError(f'cannot resolve {path}')

    monkeypatch.setattr(Path, 'resolve', _fail_resolve)

    with pytest.raises(PubanonError) as exc_info:
        _config.derived_private_terms(paths, project, {'HOME': str(tmp_path / 'home')})

    assert escape(str(private_source)) not in exc_info.value.render()


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('anon_name', 'Different Public Name'),
        ('anon_email', 'different@example.invalid'),
        ('accounts', {'github': 'different-account'}),
        ('sensitive_terms', ('Different Real Name',)),
    ],
)
def test_fingerprint_changes_with_global_governing_field(
    tmp_path: Path, field: str, value: object
) -> None:

    original = _global_config()

    assert _config.fingerprint(original, _project_config(tmp_path)) != _config.fingerprint(
        replace(original, **{field: value}), _project_config(tmp_path)
    )


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('source_path', Path('/different/source')),
        ('host', 'future'),
        ('owner', 'different-owner'),
        ('repo', 'different-repo'),
        ('visibility', 'public'),
        ('allow', ('different/**',)),
        ('deny', ('different/**',)),
        ('secret_exceptions', ('different.key',)),
        ('extra_terms', ('different-private',)),
        ('hygiene_terms', ('different-marker',)),
        ('self_publication', True),
        ('required_checks', ('different-check',)),
    ],
)
def test_fingerprint_changes_with_project_governing_field(
    tmp_path: Path, field: str, value: object
) -> None:

    project = _project_config(tmp_path)

    assert _config.fingerprint(_global_config(), project) != _config.fingerprint(
        _global_config(), replace(project, **{field: value})
    )


def test_fingerprint_changes_with_reserved_deny_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:

    original = _config.fingerprint(_global_config(), _project_config(tmp_path))
    monkeypatch.setattr(_config, 'RESERVED_DENY', frozenset({'.pubanon.toml', '.future'}))

    assert _config.fingerprint(_global_config(), _project_config(tmp_path)) != original


def test_fingerprint_excludes_new_non_governing_project_fields(tmp_path: Path) -> None:
    project = _project_config(tmp_path)
    changed = replace(
        project,
        _stage_repo='custom-stage',
        settings=_config.Settings(
            description='Published project',
            default_branch='main',
            issues=True,
            wiki=False,
            projects=True,
        ),
        acknowledged_exposure='a' * 64,
        package=_config.PackageObservation(
            index='pypi',
            name='public-package',
            release_checks=('test',),
        ),
        workflows={
            'release': ('prepare', 'inspect', 'publish', 'watch package'),
        },
    )

    assert (
        _config.fingerprint(_global_config(), project).encode()
        == _config.fingerprint(_global_config(), changed).encode()
    )


def test_fingerprint_excludes_non_governing_project_name(tmp_path: Path) -> None:

    project = _project_config(tmp_path)

    assert _config.fingerprint(_global_config(), project) == _config.fingerprint(
        _global_config(), replace(project, name='renamed-registration')
    )


def test_fingerprint_is_stable_across_mapping_key_order(tmp_path: Path) -> None:

    first = _global_config({'github': 'anonymous-account', 'future': 'future-account'})
    second = _global_config({'future': 'future-account', 'github': 'anonymous-account'})
    project = _project_config(tmp_path)

    assert _config.fingerprint(first, project) == _config.fingerprint(second, project)


def test_fingerprint_matches_exact_projection_serialization(tmp_path: Path) -> None:

    project = _project_config(tmp_path, source_path=Path('/private/source'))

    # The serialized source path uses each platform's native separators.
    expected = (
        '2d37aeee070634a71cd340e093450b4b6b8e152d367926def6bf37d696fdf861'
        if os.name == 'nt'
        else 'e49eedd085a79528a4a32943fd046beea3979ace0abcfb48adbfa7b56ae0bfa3'
    )
    assert _config.fingerprint(_global_config(), project) == expected


def test_gate_host_coordinates_reports_current_derived_terms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    term = 'derived-private-coordinate'
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    global_config = replace(_global_config(), sensitive_terms=())
    project = replace(
        _project_config(tmp_path, self_publication=True),
        repo=f'public-{term}',
        extra_terms=(),
        hygiene_terms=(),
    )

    def _derived_terms(
        _paths: _config.Paths,
        _project: _config.ProjectConfig,
        _env: object,
    ) -> tuple[str, ...]:
        return (term,)

    monkeypatch.setattr(_config, 'derived_private_terms', _derived_terms)

    with pytest.raises(PubanonError, match='repository repo failed content gates') as exc_info:
        _config.gate_host_coordinates(
            paths, global_config, project, coordinates=(project.owner, project.repo)
        )

    assert term in exc_info.value.render()


def test_global_from_mapping_refuses_unknown_account_adapter() -> None:
    data: dict[str, object] = {
        'anon_name': 'Public Name',
        'anon_email': 'public@example.invalid',
        'sensitive_terms': ['private-term'],
        'accounts': {'github': 'public-account', 'other': 'private-account'},
    }

    with pytest.raises(PubanonError, match='unknown account adapter key: other'):
        _config.global_from_mapping(data)


def test_held_project_lock_does_not_render_sensitive_project_name(tmp_path: Path) -> None:
    name = 'private-project-canary'
    paths = _write_global_config(tmp_path, sensitive_terms=[name])

    with (
        _config.project_lock(paths, name),
        pytest.raises(PubanonError) as exc_info,
        _config.project_lock(paths, name),
    ):
        pytest.fail('conflicting project lock unexpectedly acquired')

    assert name not in exc_info.value.render()
    assert exc_info.value.recovery == 'retry after the active operation finishes'


def test_held_project_lock_filesystem_failure_is_path_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path / 'private-home')})
    private_lock = paths.data_dir / 'projects' / 'demo' / 'project.lock'

    def _fail_open(path: Path, *_args: object, **_kwargs: object) -> Never:
        raise OSError(f'cannot open {path}')

    monkeypatch.setattr(Path, 'open', _fail_open)

    with pytest.raises(PubanonError) as exc_info, _config.project_lock(paths, 'demo'):
        pytest.fail('project lock unexpectedly acquired')

    assert escape(str(private_lock)) not in exc_info.value.render()


def test_held_project_lock_refuses_with_exact_recovery(tmp_path: Path) -> None:

    paths = _config.derive_paths({'HOME': str(tmp_path / 'home')})

    with (
        _config.project_lock(paths, 'demo'),
        pytest.raises(PubanonError) as exc_info,
        _config.project_lock(paths, 'demo'),
    ):
        pytest.fail('conflicting project lock unexpectedly acquired')

    assert exc_info.value.recovery == 'retry after the active operation finishes'
    assert 'retry after the active operation finishes' in exc_info.value.render()
    with _config.project_lock(paths, 'demo'):
        pass


def test_identity_refusal_names_sources_and_joined_recovery() -> None:
    error = _config.identity_refusal(('sensitive_terms', 'private path'))
    assert str(error) == 'publication identity matches an active disclosure rule'
    assert error.fact == 'rule source: sensitive_terms, private path'
    assert error.recovery == (
        'remove the value from sensitive_terms or choose another publication identity; '
        'choose a publication identity that does not contain a private path'
    )


@pytest.mark.parametrize(
    ('hits', 'expected'),
    [
        ([_identity_hit('global-term')], ('sensitive_terms',)),
        ([_identity_hit('project-term')], ('extra_terms',)),
        ([_identity_hit('/home/private')], ('private path',)),
        ([_identity_hit('draft', RuleClass.HYGIENE)], ('hygiene term',)),
        (
            [
                _identity_hit('draft', RuleClass.HYGIENE),
                _identity_hit('/home/private'),
                _identity_hit('global-term'),
                _identity_hit('global-term'),
            ],
            ('sensitive_terms', 'private path', 'hygiene term'),
        ),
        ([_identity_hit('shared-term')], ('sensitive_terms', 'extra_terms')),
    ],
)
def test_identity_rule_sources_names_each_source_once_in_order(
    hits: list[GateHit], expected: tuple[str, ...]
) -> None:
    g, p = _identity_configs()
    assert _config.identity_rule_sources(hits, g, p, ('/home/private',)) == expected


def test_identity_rule_sources_without_project_uses_global_and_derived_terms() -> None:
    g, _p = _identity_configs()
    hits = [_identity_hit('/home/private'), _identity_hit('global-term')]
    assert _config.identity_rule_sources(hits, g, None, ('/home/private',)) == (
        'sensitive_terms',
        'private path',
    )


def test_install_project_retains_reservation_after_save_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path / 'home')})
    source = tmp_path / 'private-source'
    source.mkdir()
    project = _project_config(tmp_path, source_path=source.resolve())
    other = replace(project, name='other-project')
    real_save_project = _config.save_project
    attempts = 0

    def _fail_once(actual_paths: _config.Paths, actual_project: _config.ProjectConfig) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PubanonError('cannot save project configuration')
        real_save_project(actual_paths, actual_project)

    monkeypatch.setattr(_config, 'save_project', _fail_once)

    with pytest.raises(PubanonError, match='cannot save project configuration'):
        _config.install_project(paths, project)

    assert _config.registry_lookup(paths, source) == project.name
    assert not (paths.config_dir / 'projects' / 'demo.toml').exists()
    with pytest.raises(PubanonError, match='another project'):
        _config.install_project(paths, other)

    _config.install_project(paths, project)

    assert attempts == 2
    assert _config.registry_lookup(paths, source) == project.name
    assert _config.load_project(paths, project.name) == project


def test_install_project_serializes_source_ownership_before_config_visibility(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path / 'home')})
    source = tmp_path / 'private-source'
    source.mkdir()
    first = replace(_project_config(tmp_path, source_path=source.resolve()), name='first-project')
    second = replace(first, name='second-project')
    save_started = threading.Event()
    release_save = threading.Event()
    real_save_project = _config.save_project

    def _save_while_registry_is_reserved(
        actual_paths: _config.Paths, project: _config.ProjectConfig
    ) -> None:
        assert json.loads(paths.registry.read_text(encoding='utf-8')) == {
            str(source.resolve()): 'first-project'
        }
        save_started.set()
        assert release_save.wait(timeout=5), 'test did not release the blocked project save'
        real_save_project(actual_paths, project)

    monkeypatch.setattr(_config, 'save_project', _save_while_registry_is_reserved)
    with ThreadPoolExecutor(max_workers=1) as executor:
        owner = executor.submit(_config.install_project, paths, first)
        try:
            assert save_started.wait(timeout=5), (
                'first installation did not reach its save boundary'
            )
            with pytest.raises(PubanonError, match='project registry is active'):
                _config.install_project(paths, second)
            assert not (paths.config_dir / 'projects' / 'second-project.toml').exists()
        finally:
            release_save.set()
        owner.result(timeout=5)

    assert _config.registry_lookup(paths, source) == 'first-project'
    assert _config.load_project(paths, 'first-project') == first
    assert list((paths.config_dir / 'projects').glob('*.toml')) == [
        paths.config_dir / 'projects' / 'first-project.toml'
    ]


def test_load_global_accepts_ordinary_identity(tmp_path: Path) -> None:
    paths = _write_global_config(tmp_path, anon_name="Ana O'Reilly-Smith", anon_email='a@b.c')
    assert _config.load_global(paths).anon_name == "Ana O'Reilly-Smith"


def test_load_global_reads_exact_schema(tmp_path: Path) -> None:

    paths = _config.derive_paths({'HOME': str(tmp_path)})
    paths.config_dir.mkdir(parents=True)
    paths.global_config.write_text(
        'anon_name = "Public Name"\n'
        'anon_email = "public@example.invalid"\n'
        'sensitive_terms = ["Real Name", "real@example.test"]\n'
        '[accounts]\n'
        'github = "anonymous-account"\n'
    )

    assert _config.load_global(paths) == _config.GlobalConfig(
        anon_name='Public Name',
        anon_email='public@example.invalid',
        accounts={'github': 'anonymous-account'},
        sensitive_terms=('Real Name', 'real@example.test'),
    )


def test_load_global_refuses_blank_sensitive_term(tmp_path: Path) -> None:
    paths = _write_global_config(tmp_path, sensitive_terms=['secret', '  '])
    with pytest.raises(PubanonError, match='sensitive_terms'):
        _config.load_global(paths)


def test_load_global_refuses_empty_sensitive_terms(tmp_path: Path) -> None:
    paths = _write_global_config(tmp_path, sensitive_terms=[])
    with pytest.raises(PubanonError, match='sensitive_terms'):
        _config.load_global(paths)


def test_load_global_refuses_invalid_utf8_with_usable_path(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path / 'private-home')})
    paths.config_dir.mkdir(parents=True)
    paths.global_config.write_bytes(b'anon_name = "invalid\xff"\n')

    with pytest.raises(PubanonError) as error:
        _config.load_global(paths)

    assert 'invalid UTF-8' in error.value.render()
    assert escape(str(paths.global_config)) in error.value.render()


@pytest.mark.parametrize(
    ('content', 'match'),
    [
        ('anon_name = [\n', 'invalid'),
        (
            (
                'anon_name = "Public Name"\nanon_email = "public@example.invalid"\n'
                'sensitive_terms = []\n[accounts]\n'
            ),
            'github',
        ),
    ],
)
def test_load_global_refuses_malformed_or_incomplete_schema(
    tmp_path: Path, content: str, match: str
) -> None:

    paths = _config.derive_paths({'HOME': str(tmp_path)})
    paths.config_dir.mkdir(parents=True)
    paths.global_config.write_text(content)

    with pytest.raises(PubanonError, match=match):
        _config.load_global(paths)


def test_load_global_refuses_unknown_adapter(tmp_path: Path) -> None:

    paths = _config.derive_paths({'HOME': str(tmp_path)})
    paths.config_dir.mkdir(parents=True)
    paths.global_config.write_text(
        'anon_name = "Public Name"\n'
        'anon_email = "public@example.invalid"\n'
        'sensitive_terms = []\n'
        '[accounts]\n'
        'github = "anonymous-account"\n'
        'gitlab = "wrong-adapter"\n'
    )

    with pytest.raises(PubanonError, match='gitlab'):
        _config.load_global(paths)


def test_load_global_refuses_unknown_key_and_names_it(tmp_path: Path) -> None:

    paths = _config.derive_paths({'HOME': str(tmp_path)})
    paths.config_dir.mkdir(parents=True)
    paths.global_config.write_text(
        'anon_name = "Public Name"\n'
        'anon_email = "public@example.invalid"\n'
        'sensitive_terms = []\n'
        'unexpected = true\n'
        '[accounts]\n'
        'github = "anonymous-account"\n'
    )

    with pytest.raises(PubanonError, match='unexpected'):
        _config.load_global(paths)


@pytest.mark.parametrize(
    'value',
    ['', ' name', 'name ', 'a<b', 'a>b', 'line\rbreak', 'tab\there', '\u00a0lead'],
)
def test_load_global_refuses_unstable_identity(tmp_path: Path, value: str) -> None:
    paths = _write_global_config(tmp_path, anon_name=value)
    with pytest.raises(PubanonError) as excinfo:
        _config.load_global(paths)
    message = str(excinfo.value).removeprefix('anon_name ')
    assert not value or value not in message


def test_load_global_refuses_wrong_value_type(tmp_path: Path) -> None:

    paths = _config.derive_paths({'HOME': str(tmp_path)})
    paths.config_dir.mkdir(parents=True)
    paths.global_config.write_text(
        'anon_name = 3\n'
        'anon_email = "public@example.invalid"\n'
        'sensitive_terms = []\n'
        '[accounts]\n'
        'github = "anonymous-account"\n'
    )

    with pytest.raises(PubanonError, match='anon_name'):
        _config.load_global(paths)


def test_load_project_defaults_other_optional_fields(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    project_file.parent.mkdir(parents=True)
    project_file.write_text(_project_toml(tmp_path / 'source'))

    project = _config.load_project(paths, 'demo')

    assert project._stage_repo is None
    assert project.acknowledged_exposure == ''
    assert project.package is None
    assert project.workflows == {}


def test_load_project_reads_exact_schema(tmp_path: Path) -> None:

    source = tmp_path / 'source'
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    project_file.parent.mkdir(parents=True)
    project_file.write_text(
        f'name = "demo"\n'
        f'source_path = {toml_string(str(source))}\n'
        'host = "github"\n'
        'owner = "anonymous-account"\n'
        'repo = "public-repo"\n'
        'visibility = "private"\n'
        'allow = ["src/**", "README.md"]\n'
        'deny = ["notes/**"]\n'
        'secret_exceptions = ["tests/fixture.key"]\n'
        'extra_terms = ["private-product"]\n'
        'hygiene_terms = ["internal-marker"]\n'
        'self_publication = false\n'
        'required_checks = ["test"]\n'
        'settings = { description = "Published project", default_branch = "main", '
        'issues = true, wiki = false, projects = true }\n'
    )

    assert _config.load_project(paths, 'demo') == _config.ProjectConfig(
        name='demo',
        source_path=source,
        host='github',
        owner='anonymous-account',
        repo='public-repo',
        visibility='private',
        allow=('src/**', 'README.md'),
        deny=('notes/**',),
        secret_exceptions=('tests/fixture.key',),
        extra_terms=('private-product',),
        hygiene_terms=('internal-marker',),
        self_publication=False,
        required_checks=('test',),
        settings=_config.Settings(
            description='Published project',
            default_branch='main',
            issues=True,
            wiki=False,
            projects=True,
        ),
    )


def test_load_project_refuses_acknowledged_exposure_with_invalid_digest(
    tmp_path: Path,
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    project_file.parent.mkdir(parents=True)
    project_file.write_text(_project_toml(tmp_path / 'source') + 'acknowledged_exposure = "xyz"\n')

    with pytest.raises(
        PubanonError,
        match='acknowledged_exposure must be empty or 64 lowercase hex characters',
    ):
        _config.load_project(paths, 'demo')


def test_load_project_refuses_blank_extra_and_hygiene_terms(tmp_path: Path) -> None:
    for key in ('extra_terms', 'hygiene_terms'):
        paths = _write_project_config(tmp_path, **{key: ['ok', '']})
        with pytest.raises(PubanonError, match=key):
            _config.load_project(paths, 'proj')


def test_load_project_refuses_empty_package_name(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    project_file.parent.mkdir(parents=True)
    project_file.write_text(
        _project_toml(tmp_path / 'source') + '[package]\n'
        'index = "pypi"\n'
        'name = ""\n'
        'release_checks = ["test"]\n'
    )

    with pytest.raises(PubanonError, match='name must be nonempty'):
        _config.load_project(paths, 'demo')


def test_load_project_refuses_empty_package_release_checks(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    project_file.parent.mkdir(parents=True)
    project_file.write_text(
        _project_toml(tmp_path / 'source') + '[package]\n'
        'index = "pypi"\n'
        'name = "public-package"\n'
        'release_checks = []\n'
    )

    with pytest.raises(PubanonError, match='release_checks must contain at least one'):
        _config.load_project(paths, 'demo')


def test_load_project_refuses_empty_workflow_name(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    project_file.parent.mkdir(parents=True)
    project_file.write_text(
        _project_toml(tmp_path / 'source') + '[workflow.""]\nstages = ["prepare"]\n'
    )

    with pytest.raises(PubanonError, match='workflow name must be nonempty'):
        _config.load_project(paths, 'demo')


@pytest.mark.parametrize(
    ('stages', 'problem'),
    [
        ('["prepare", "future"]', 'future'),
        ('["prepare", "prepare"]', 'prepare'),
        ('["inspect", "prepare"]', 'prepare'),
    ],
)
def test_load_project_refuses_invalid_workflow_stage_and_names_entry(
    tmp_path: Path, stages: str, problem: str
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    project_file.parent.mkdir(parents=True)
    project_file.write_text(
        _project_toml(tmp_path / 'source') + f'[workflow.release]\nstages = {stages}\n'
    )

    with pytest.raises(PubanonError, match=rf'workflow release.*{problem}'):
        _config.load_project(paths, 'demo')


def test_load_project_refuses_missing_settings_key(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    project_file.parent.mkdir(parents=True)
    project_file.write_text(
        _project_toml(tmp_path / 'source', settings=False) + '[settings]\n'
        'description = "Published project"\n'
        'default_branch = "main"\n'
        'issues = true\n'
        'wiki = false\n'
    )

    with pytest.raises(PubanonError, match='missing settings key: projects'):
        _config.load_project(paths, 'demo')


def test_load_project_refuses_missing_settings_table(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    project_file.parent.mkdir(parents=True)
    project_file.write_text(_project_toml(tmp_path / 'source', settings=False))

    with pytest.raises(PubanonError, match='missing project configuration key: settings'):
        _config.load_project(paths, 'demo')


def test_load_project_refuses_relative_source_path(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    project_file.parent.mkdir(parents=True)
    project_file.write_text(
        'name = "demo"\nsource_path = "relative/source"\nhost = "github"\n'
        'owner = "account"\nrepo = "repo"\nvisibility = "public"\n'
        'allow = []\ndeny = []\nsecret_exceptions = []\nextra_terms = []\n'
        'hygiene_terms = []\nself_publication = false\nrequired_checks = []\n'
        'settings = { description = "Published project", default_branch = "main", '
        'issues = true, wiki = false, projects = true }\n'
    )

    with pytest.raises(PubanonError, match='source_path'):
        _config.load_project(paths, 'demo')


def test_load_project_refuses_stage_repo_containing_slash(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    project_file.parent.mkdir(parents=True)
    project_file.write_text(_project_toml(tmp_path / 'source') + 'stage_repo = "nested/stage"\n')

    with pytest.raises(PubanonError, match=r'stage_repo.*host coordinate segment'):
        _config.load_project(paths, 'demo')


def test_load_project_refuses_stage_repo_equal_to_repo(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    project_file.parent.mkdir(parents=True)
    project_file.write_text(_project_toml(tmp_path / 'source') + 'stage_repo = "public-repo"\n')

    with pytest.raises(PubanonError, match='stage_repo must differ from repo'):
        _config.load_project(paths, 'demo')


def test_load_project_refuses_stored_name_mismatch(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project = _project_config(tmp_path)
    _config.save_project(paths, project)
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    project_file.rename(project_file.with_name('requested.toml'))

    with pytest.raises(PubanonError, match='name'):
        _config.load_project(paths, 'requested')


def test_load_project_refuses_unknown_key_and_names_it(tmp_path: Path) -> None:

    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    project_file.parent.mkdir(parents=True)
    project_file.write_text(
        f'name = "demo"\n'
        f'source_path = {toml_string(str(tmp_path / "source"))}\n'
        'host = "github"\nowner = "account"\nrepo = "repo"\nvisibility = "public"\n'
        'allow = []\ndeny = []\nsecret_exceptions = []\nextra_terms = []\n'
        'hygiene_terms = []\nself_publication = false\nrequired_checks = []\n'
        'surprise = "no"\n'
    )

    with pytest.raises(PubanonError, match='surprise'):
        _config.load_project(paths, 'demo')


def test_load_project_refuses_unknown_settings_key_and_names_it(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    project_file.parent.mkdir(parents=True)
    project_file.write_text(
        _project_toml(tmp_path / 'source', settings=False) + '[settings]\n'
        'description = "Published project"\n'
        'default_branch = "main"\n'
        'issues = true\n'
        'wiki = false\n'
        'projects = true\n'
        'surprise = true\n'
    )

    with pytest.raises(PubanonError, match='unknown settings key: surprise'):
        _config.load_project(paths, 'demo')


@pytest.mark.parametrize('field', ['owner', 'repo', 'stage_repo'])
@pytest.mark.parametrize(
    'value',
    [
        pytest.param('', id='empty'),
        pytest.param('two/segments', id='slash'),
        pytest.param('repo?query', id='query'),
        pytest.param('repo#fragment', id='fragment'),
        pytest.param('repo\x00control', id='nul-control'),
        pytest.param('repo\x7fcontrol', id='del-control'),
        pytest.param('repo\ud800surrogate', id='surrogate'),
        pytest.param(' leading', id='leading-whitespace'),
        pytest.param('trailing ', id='trailing-whitespace'),
    ],
)
def test_load_project_refuses_unsafe_host_segments(
    field: str,
    value: str,
    tmp_path: Path,
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    project_file.parent.mkdir(parents=True)
    text = _project_toml(tmp_path / 'source')
    if '\ud800' in value:
        rendered_value = '"repo\\uD800surrogate"'
        error = 'invalid project configuration'
    else:
        rendered_value = toml_string(value)
        error = f'{field}.*host coordinate segment'
    if field == 'stage_repo':
        text += f'stage_repo = {rendered_value}\n'
    else:
        original = 'anonymous-account' if field == 'owner' else 'public-repo'
        text = text.replace(
            f'{field} = {toml_string(original)}',
            f'{field} = {rendered_value}',
        )
    project_file.write_text(text, encoding='utf-8')

    with pytest.raises(PubanonError, match=error):
        _config.load_project(paths, 'demo')


@pytest.mark.parametrize(
    ('field', 'value'),
    [('host', 'gitlab'), ('visibility', 'unlisted')],
)
def test_load_project_refuses_unsupported_enum_value(
    tmp_path: Path, field: str, value: str
) -> None:

    project = {
        'name': 'demo',
        'source_path': str(tmp_path / 'source'),
        'host': 'github',
        'owner': 'account',
        'repo': 'repo',
        'visibility': 'public',
    }
    project[field] = value
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    project_file.parent.mkdir(parents=True)
    project_file.write_text(
        ''.join(f'{key} = {toml_string(item)}\n' for key, item in project.items())
        + 'allow = []\ndeny = []\nsecret_exceptions = []\nextra_terms = []\n'
        'hygiene_terms = []\nself_publication = false\nrequired_checks = []\n'
        'settings = { description = "Published project", default_branch = "main", '
        'issues = true, wiki = false, projects = true }\n'
    )

    with pytest.raises(PubanonError, match=field):
        _config.load_project(paths, 'demo')


def test_load_project_refuses_unsupported_package_index(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    project_file.parent.mkdir(parents=True)
    project_file.write_text(
        _project_toml(tmp_path / 'source') + '[package]\n'
        'index = "other"\n'
        'name = "public-package"\n'
        'release_checks = ["test"]\n'
    )

    with pytest.raises(PubanonError, match=r'index.*pypi'):
        _config.load_project(paths, 'demo')


def test_project_dirs_are_owner_only_under_permissive_umask(tmp_path: Path) -> None:
    previous = os.umask(0o022)
    try:
        paths = _config.derive_paths({'HOME': str(tmp_path / 'fresh')})
        with _config.project_lock(paths, 'proj'):
            pass
        for level in (
            paths.data_dir,
            paths.data_dir / 'projects',
            paths.data_dir / 'projects' / 'proj',
        ):
            assert_private_mode(level, 0o700)
    finally:
        os.umask(previous)


@pytest.mark.parametrize('field', ['owner', 'repo', 'stage_repo'])
def test_project_host_segments_round_trip(field: str, tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    python_field = '_stage_repo' if field == 'stage_repo' else field
    project = replace(_project_config(tmp_path), **{python_field: f'valid-{field}'})

    _config.save_project(paths, project)

    assert _config.load_project(paths, project.name) == project


def test_project_lock_emits_no_encoding_warning(tmp_path: Path) -> None:
    env = dict(os.environ)
    env['PYTHONPATH'] = str(Path(__file__).parents[1] / 'src')
    script = (
        'from pubanon import _config, _filesystem, _locking\n'
        f'paths = _config.derive_paths({{"HOME": {str(tmp_path)!r}}})\n'
        'with _config.project_lock(paths, "proj"):\n'
        '    pass\n'
    )
    completed = subprocess.run(
        [
            sys.executable,
            '-X',
            'warn_default_encoding',
            '-W',
            'error::EncodingWarning',
            '-c',
            script,
        ],
        capture_output=True,
        check=False,
        env=env,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr


@pytest.mark.parametrize('name', ['publications', 'source-worktrees', 'rewrite', 'rewrite.toml'])
@pytest.mark.parametrize('existing', [False, True])
@pytest.mark.parametrize('source_alias', [False, True])
def test_publication_state_paths_guard_native_case_aliases(
    tmp_path: Path, name: str, *, existing: bool, source_alias: bool
) -> None:
    source = tmp_path / 'Source'
    state = source / 'State'
    state.mkdir(parents=True)
    alias = tmp_path / 'sOURCE'
    if not alias.exists() or not alias.samefile(source):
        pytest.skip('filesystem does not provide case aliases')
    private = state / 'pubanon' / 'projects' / 'demo' / name
    if name != 'rewrite.toml':
        private /= 'private.txt'
    if existing:
        private.parent.mkdir(parents=True)
        private.write_text('private fixture', encoding='utf-8')
    project = _project_config(tmp_path, source_path=alias if source_alias else source)
    paths = _config.derive_paths(
        {'XDG_CONFIG_HOME': str(tmp_path / 'config'), 'XDG_DATA_HOME': str(source / 'sTATE')}
    )

    for selected in (
        private.relative_to(source).as_posix(),
        private.relative_to(source).as_posix().replace('State/', 'sTATE/', 1),
    ):
        with pytest.raises(
            PubanonError, match='selected snapshot includes private publication state'
        ):
            _config.check_publication_state_paths(paths, project, [selected])


@pytest.mark.parametrize('directory', ['publications', 'source-worktrees', 'rewrite'])
def test_publication_state_paths_guard_only_configured_subtree(
    tmp_path: Path, directory: str
) -> None:
    project = _project_config(tmp_path, source_path=tmp_path)
    paths = _config.derive_paths(
        {'XDG_CONFIG_HOME': str(tmp_path / 'config'), 'XDG_DATA_HOME': str(tmp_path / 'state')}
    )
    selected = f'state/pubanon/projects/demo/{directory}/private.txt'
    with pytest.raises(PubanonError, match='publication state') as error:
        _config.check_publication_state_paths(paths, project, [selected])
    assert escape(str(tmp_path / selected)) in error.value.render()
    _config.check_publication_state_paths(
        paths,
        project,
        [f'{directory}/ordinary.txt', selected.replace(directory, directory + '-other')],
    )


def test_publication_state_paths_reports_inaccessible_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project = _project_config(tmp_path, source_path=tmp_path)
    private = paths.data_dir / 'projects' / 'demo' / 'publications'
    original_stat = Path.stat

    def _fail_private_stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        if self == private:
            raise PermissionError('injected inaccessible path')
        return original_stat(self, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(Path, 'stat', _fail_private_stat)

    with pytest.raises(PubanonError, match='cannot inspect publication state'):
        _config.check_publication_state_paths(paths, project, ['README.md'])


def test_registry_directory_sync_failure_reports_possible_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path / 'home')})
    source = tmp_path / 'source'
    source.mkdir()

    def _fail_directory_sync(_directory: Path) -> Never:
        raise OSError('injected directory sync failure')

    monkeypatch.setattr(_filesystem, 'sync_directory', _fail_directory_sync)

    with pytest.raises(PubanonError, match='registry may have been saved'):
        _config.registry_set(paths, source, 'demo')

    assert _config.registry_lookup(paths, source) == 'demo'
    assert list(paths.registry.parent.glob('.registry-*.tmp')) == []


def test_registry_file_sync_failure_preserves_prior_registry_and_cleans_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path / 'home')})
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    first.mkdir()
    second.mkdir()
    _config.registry_set(paths, first, 'first-project')
    original = paths.registry.read_bytes()

    def _fail_sync(_descriptor: int) -> Never:
        raise OSError('injected file sync failure')

    monkeypatch.setattr(os, 'fsync', _fail_sync)

    with pytest.raises(PubanonError, match='cannot update project registry'):
        _config.registry_set(paths, second, 'second-project')

    assert paths.registry.read_bytes() == original
    assert _config.registry_lookup(paths, first) == 'first-project'
    assert list(paths.registry.parent.glob('.registry-*.tmp')) == []


def test_registry_lookup_existing_registry_uses_lock(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path / 'home')})
    source = tmp_path / 'source'
    source.mkdir()
    _config.registry_set(paths, source, 'demo')
    lock_path = paths.registry.with_name('registry.lock')

    with lock_path.open('ab') as lock_file:
        _locking.acquire(lock_file.fileno())
        with pytest.raises(PubanonError, match='project registry is active') as exc_info:
            _config.registry_lookup(paths, source)

    assert exc_info.value.recovery == 'retry after the active operation finishes'
    assert_private_mode(paths.registry, 0o600)
    assert_private_mode(lock_path, 0o600)


def test_registry_lookup_keeps_distinct_native_case_sensitive_roots(tmp_path: Path) -> None:
    source = tmp_path / 'Source'
    source.mkdir()
    other = tmp_path / 'source'
    if other.exists():
        pytest.skip('filesystem does not provide distinct case-sensitive paths')
    other.mkdir()
    paths = _config.derive_paths({'HOME': str(tmp_path / 'home')})
    _config.registry_set(paths, source, 'demo')

    assert _config.registry_lookup(paths, other / 'missing') is None


def test_registry_lookup_rejects_corrupt_existing_registry(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path / 'home')})
    paths.data_dir.mkdir(parents=True)
    paths.registry.write_text('{', encoding='utf-8')
    lock_path = paths.registry.with_name('registry.lock')

    with pytest.raises(PubanonError, match='invalid project registry') as exc_info:
        _config.registry_lookup(paths, tmp_path)

    assert escape(str(paths.registry)) in exc_info.value.render()
    assert '--project NAME' in exc_info.value.render()
    assert lock_path.is_file()
    assert_private_mode(lock_path, 0o600)


def test_registry_lookup_rejects_unresolvable_registered_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path / 'home')})
    source = tmp_path / 'source'
    nested = source / 'nested'
    nested.mkdir(parents=True)
    _config.create_data_dirs(paths, None)
    paths.registry.write_text(json.dumps({str(source): 'demo'}), encoding='utf-8')
    original_resolve = Path.resolve

    def _fail_registered_source(self: Path, strict: bool = False) -> Path:
        if self == source:
            raise OSError('cannot resolve registered source')
        return original_resolve(self, strict=strict)

    monkeypatch.setattr(Path, 'resolve', _fail_registered_source)

    with pytest.raises(PubanonError, match='invalid project registry'):
        _config.registry_lookup(paths, nested)


def test_registry_lookup_resolves_aliases_before_selecting_most_specific(
    tmp_path: Path,
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path / 'home')})
    source = tmp_path / 'source'
    nested = source / 'nested'
    working_directory = nested / 'working'
    working_directory.mkdir(parents=True)
    source_alias = tmp_path / 'source-alias'
    nested_alias = tmp_path / 'nested-alias'
    source_alias.symlink_to(source, target_is_directory=True)
    nested_alias.symlink_to(nested, target_is_directory=True)
    _config.create_data_dirs(paths, None)
    paths.registry.write_text(
        json.dumps(
            {
                str(source_alias): 'outer-project',
                str(nested_alias): 'nested-project',
            }
        ),
        encoding='utf-8',
    )

    assert _config.registry_lookup(paths, working_directory) == 'nested-project'


def test_registry_lookup_selects_most_specific_native_case_alias(tmp_path: Path) -> None:
    source = tmp_path / 'Source'
    nested = source / 'Nested'
    nested.mkdir(parents=True)
    alias = tmp_path / 'sOURCE'
    if not alias.exists() or not alias.samefile(source):
        pytest.skip('filesystem does not provide case aliases')
    paths = _config.derive_paths({'HOME': str(tmp_path / 'home')})
    _config.create_data_dirs(paths, None)
    paths.registry.write_text(
        json.dumps({str(source): 'outer', str(alias / 'nESTED'): 'inner'}), encoding='utf-8'
    )

    assert _config.registry_lookup(paths, nested / 'missing' / 'child') == 'inner'


def test_registry_lookup_without_state_is_read_only(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path / 'home')})
    lock_path = paths.registry.with_name('registry.lock')

    assert _config.registry_lookup(paths, tmp_path) is None
    assert not paths.data_dir.exists()
    assert not paths.registry.exists()
    assert not lock_path.exists()


def test_registry_save_syncs_file_replaces_and_syncs_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path / 'home')})
    source = tmp_path / 'source'
    source.mkdir()
    events: list[str] = []
    original_fsync = os.fsync
    original_replace = _filesystem.replace
    original_sync_directory = _filesystem.sync_directory

    def _track_fsync(descriptor: int) -> None:
        events.append('file sync')
        original_fsync(descriptor)

    def _track_replace(temporary: Path, target: Path) -> None:
        assert target == paths.registry
        assert temporary.parent == target.parent
        assert_private_mode(temporary, 0o600)
        events.append('replace')
        original_replace(temporary, target)

    def _track_directory(directory: Path) -> None:
        assert directory == paths.registry.parent
        events.append('directory sync')
        original_sync_directory(directory)

    monkeypatch.setattr(os, 'fsync', _track_fsync)
    monkeypatch.setattr(_filesystem, 'replace', _track_replace)
    monkeypatch.setattr(_filesystem, 'sync_directory', _track_directory)

    _config.registry_set(paths, source, 'demo')

    assert events == [
        'file sync',
        'replace',
        'directory sync',
        *([] if os.name == 'nt' else ['file sync']),
    ]
    assert _config.registry_lookup(paths, source) == 'demo'
    assert list(paths.registry.parent.glob('.registry-*.tmp')) == []


def test_registry_set_filesystem_failure_identifies_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path / 'private-home')})
    source = tmp_path / 'private-source'
    source.mkdir()

    def _fail_replace(_source: Path, target: Path) -> Never:
        assert target == paths.registry
        raise OSError(f'cannot replace {target}')

    monkeypatch.setattr(_filesystem, 'replace', _fail_replace)

    with pytest.raises(PubanonError) as exc_info:
        _config.registry_set(paths, source, 'demo')

    assert escape(str(paths.registry)) in exc_info.value.render()


def test_registry_set_refuses_conflicting_owner_hidden_by_same_project_alias(
    tmp_path: Path,
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path / 'home')})
    source = tmp_path / 'source'
    source.mkdir()
    first_alias = tmp_path / 'first-alias'
    conflicting_alias = tmp_path / 'conflicting-alias'
    first_alias.symlink_to(source, target_is_directory=True)
    conflicting_alias.symlink_to(source, target_is_directory=True)
    _config.create_data_dirs(paths, None)
    paths.registry.write_text(
        json.dumps(
            {
                str(first_alias): 'demo',
                str(conflicting_alias): 'other-project',
            }
        ),
        encoding='utf-8',
    )

    with pytest.raises(PubanonError, match='another project'):
        _config.registry_set(paths, source, 'demo')


def test_registry_set_refuses_source_owned_by_another_project(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path / 'home')})
    source = tmp_path / 'private-source'
    source.mkdir()
    _config.registry_set(paths, source, 'first-project')

    with pytest.raises(PubanonError, match='another project') as exc_info:
        _config.registry_set(paths, source, 'second-project')

    assert escape(str(source)) not in exc_info.value.render()
    assert _config.registry_lookup(paths, source) == 'first-project'


def test_registry_set_rejects_case_variant_duplicate(tmp_path: Path) -> None:
    source = tmp_path / 'Repo'
    source.mkdir()
    config_dir = tmp_path / 'config' / 'pubanon'
    data_dir = tmp_path / 'data' / 'pubanon'
    paths = _config.Paths(
        config_dir, data_dir, config_dir / 'config.toml', data_dir / 'registry.json'
    )
    data_dir.mkdir(parents=True)
    _config.registry_set(paths, source, 'proj')
    variant = Path(str(source).replace('Repo', 'REPO'))
    if not variant.exists():  # case-sensitive filesystem: samefile boundary not reachable
        pytest.skip('requires a case-insensitive filesystem')
    with pytest.raises(PubanonError, match='another project'):
        _config.registry_set(paths, variant, 'other')


def test_registry_set_replaces_prior_source_for_same_project(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path / 'home')})
    original = tmp_path / 'original-source'
    relocated = tmp_path / 'relocated-source'
    original.mkdir()
    relocated.mkdir()
    _config.registry_set(paths, original, 'demo')

    _config.registry_set(paths, relocated, 'demo')

    assert _config.registry_lookup(paths, original) is None
    assert _config.registry_lookup(paths, relocated) == 'demo'


def test_registry_set_replaces_same_source_aliases_with_canonical_path(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path / 'home')})
    source = tmp_path / 'source'
    source.mkdir()
    first_alias = tmp_path / 'first-alias'
    second_alias = tmp_path / 'second-alias'
    first_alias.symlink_to(source, target_is_directory=True)
    second_alias.symlink_to(source, target_is_directory=True)
    _config.create_data_dirs(paths, None)
    paths.registry.write_text(
        json.dumps({str(first_alias): 'demo', str(second_alias): 'demo'}),
        encoding='utf-8',
    )

    _config.registry_set(paths, source.resolve(), 'demo')

    assert json.loads(paths.registry.read_text(encoding='utf-8')) == {
        str(source.resolve()): 'demo'
    }


def test_registry_survives_interleaved_updates_and_resolves_nested_cwd(tmp_path: Path) -> None:

    paths = _config.derive_paths({'HOME': str(tmp_path / 'home')})
    first = tmp_path / 'first'
    second = tmp_path / 'second'
    (first / 'nested').mkdir(parents=True)
    (second / 'nested').mkdir(parents=True)

    _config.registry_set(paths, first, 'first-project')
    _config.registry_set(paths, second, 'second-project')
    _config.registry_set(paths, first, 'first-project')

    assert _config.registry_lookup(paths, first / 'nested') == 'first-project'
    assert _config.registry_lookup(paths, second / 'nested') == 'second-project'
    assert _config.registry_lookup(paths, tmp_path / 'outside') is None
    assert_private_mode(paths.registry, 0o600)
    assert_private_mode(paths.registry.parent, 0o700)


def test_registry_write_uses_explicit_utf8(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    source = tmp_path / 'source-\N{SNOWMAN}'
    source.mkdir()
    _config.registry_set(paths, source, 'demo')
    assert json.loads(paths.registry.read_bytes().decode('utf-8')) == {
        str(source.resolve()): 'demo'
    }


def test_repository_name_gate_explains_self_publication(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project = replace(_project_config(tmp_path, self_publication=False), repo='pubanon')
    with pytest.raises(PubanonError) as failure:
        _config.gate_host_coordinates(
            paths, _global_config(), project, coordinates=(project.owner, project.repo)
        )
    rendered = failure.value.render()
    assert 'built-in rule flags the word pubanon' in rendered
    assert 'pubanon init --self-publication' in rendered


@pytest.mark.skipif(os.name == 'nt', reason='Windows project names cannot use path aliases')
@pytest.mark.parametrize('name', ['demo.', 'demo '])
def test_require_project_name_preserves_posix_alias_spellings(name: str) -> None:
    assert _config.require_project_name(name) == name


@pytest.mark.parametrize('name', ['', '.', '..', 'a/b', '/abs'])
def test_require_project_name_refuses_unsafe_names(name: str) -> None:
    with pytest.raises(PubanonError, match='invalid project name'):
        _config.require_project_name(name)


def test_require_project_name_returns_valid_name() -> None:
    assert _config.require_project_name('proj') == 'proj'


def test_rewrite_record_guard_uses_resolved_configured_location(tmp_path: Path) -> None:
    project = _project_config(tmp_path, source_path=tmp_path)
    paths = _config.derive_paths(
        {'XDG_CONFIG_HOME': str(tmp_path / 'config'), 'XDG_DATA_HOME': str(tmp_path / 'state')}
    )
    with pytest.raises(PubanonError, match='publication state'):
        _config.check_publication_state_paths(
            paths, project, ['state/pubanon/projects/demo/rewrite.toml']
        )
    _config.check_publication_state_paths(
        paths, project, ['rewrite/file', 'rewrite.toml', 'resolutions.toml']
    )


def test_rule_labels_include_all_sources_and_human_entry_numbers() -> None:
    g, p = _identity_configs()
    hit = GateHit('unit', RuleClass.IDENTITY_CRITICAL, 'shared-term')
    assert _config.rule_labels(hit, g, p, ('other', 'shared-term')) == (
        'global sensitive_terms entry 2',
        'project extra_terms entry 2',
        'derived private path entry 2',
    )
    assert _config.rule_labels(GateHit('unit', RuleClass.HYGIENE, 'draft'), g, p, ()) == (
        'project hygiene_terms entry 1',
    )
    assert _config.rule_labels(GateHit('unit', RuleClass.HYGIENE, 'pubanon'), g, p, ()) == (
        'built-in hygiene rule',
    )


@pytest.mark.skipif(os.name == 'nt', reason='Windows commits through write-through replacement')
def test_save_global_directory_fsync_failure_reports_possible_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    config = _global_config()
    _config.save_global(paths, config, None)
    original = paths.global_config.read_bytes()
    updated = replace(config, anon_name='Updated Name')
    fsync_calls = 0
    original_fsync = os.fsync

    def _fail_directory_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError('injected directory fsync failure')
        original_fsync(descriptor)

    monkeypatch.setattr(os, 'fsync', _fail_directory_fsync)

    with pytest.raises(PubanonError) as exc_info:
        _config.save_global(paths, updated, original)

    assert exc_info.value.render() == (
        'error: global configuration may have been saved\nrecovery: rerun pubanon setup'
    )
    assert fsync_calls == 2
    assert _config.load_global(paths) == updated


def test_save_global_file_fsync_failure_preserves_prior_bytes_and_other_temps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    config = _global_config()
    _config.save_global(paths, config, None)
    original = paths.global_config.read_bytes()
    unrelated = paths.config_dir / '.global-unrelated.tmp'
    unrelated.write_bytes(b'keep')

    def _fail_fsync(_descriptor: int) -> Never:
        raise OSError('injected file fsync failure')

    monkeypatch.setattr(os, 'fsync', _fail_fsync)

    with pytest.raises(PubanonError, match='cannot save global configuration'):
        _config.save_global(
            paths,
            replace(config, anon_name='Updated Name'),
            original,
        )

    assert paths.global_config.read_bytes() == original
    assert unrelated.read_bytes() == b'keep'
    assert list(paths.config_dir.glob('.global-*.tmp')) == [unrelated]


def test_save_global_keyboard_interrupt_after_replace_reports_possible_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    config = _global_config()
    _config.save_global(paths, config, None)
    original = paths.global_config.read_bytes()
    updated = replace(config, anon_name='Updated Name')

    def _cancel_directory_sync(_path: Path) -> Never:
        raise KeyboardInterrupt

    def _save_after_replace() -> None:
        try:
            _config.save_global(paths, updated, original)
        except KeyboardInterrupt as exc:
            raise AssertionError(
                'post-replace cancellation did not report the possible save'
            ) from exc

    monkeypatch.setattr(_config, '_sync_directory', _cancel_directory_sync)

    with pytest.raises(PubanonError) as exc_info:
        _save_after_replace()

    assert exc_info.value.render() == (
        'error: global configuration may have been saved\nrecovery: rerun pubanon setup'
    )
    assert _config.load_global(paths) == updated
    assert list(paths.config_dir.glob('.global-*.tmp')) == []


def test_save_global_keyboard_interrupt_before_replace_cleans_owned_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    config = _global_config()
    _config.save_global(paths, config, None)
    original = paths.global_config.read_bytes()

    def _cancel_snapshot(_path: Path, _expected: bytes | None) -> Never:
        raise KeyboardInterrupt

    monkeypatch.setattr(_config, '_require_global_snapshot', _cancel_snapshot)

    with pytest.raises(KeyboardInterrupt):
        _config.save_global(
            paths,
            replace(config, anon_name='Updated Name'),
            original,
        )

    assert paths.global_config.read_bytes() == original
    assert list(paths.config_dir.glob('.global-*.tmp')) == []


def test_save_global_keyboard_interrupt_during_temp_write_cleans_owned_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    config = _global_config()
    _config.save_global(paths, config, None)
    original = paths.global_config.read_bytes()

    def _cancel_fsync(_descriptor: int) -> Never:
        raise KeyboardInterrupt

    monkeypatch.setattr(os, 'fsync', _cancel_fsync)

    with pytest.raises(KeyboardInterrupt):
        _config.save_global(
            paths,
            replace(config, anon_name='Updated Name'),
            original,
        )

    assert paths.global_config.read_bytes() == original
    assert list(paths.config_dir.glob('.global-*.tmp')) == []


def test_save_global_refuses_changed_original_and_preserves_external_edit(
    tmp_path: Path,
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    original = _global_config()
    _config.save_global(paths, original, None)
    snapshot = paths.global_config.read_bytes()
    external = snapshot.replace(b'Public Name', b'External Name')
    paths.global_config.write_bytes(external)

    with pytest.raises(PubanonError, match='global configuration changed during setup'):
        _config.save_global(
            paths,
            replace(original, anon_name='Updated Name'),
            snapshot,
        )

    assert paths.global_config.read_bytes() == external


def test_save_global_refuses_invalid_config_before_filesystem_change(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    invalid = replace(_global_config(), sensitive_terms=())

    with pytest.raises(PubanonError, match='sensitive_terms'):
        _config.save_global(paths, invalid, None)

    assert not paths.config_dir.exists()


def test_save_global_replace_failure_preserves_prior_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    config = _global_config()
    _config.save_global(paths, config, None)
    original = paths.global_config.read_bytes()

    def _fail_replace(_path: Path, _target: Path) -> Never:
        raise OSError('injected replace failure')

    monkeypatch.setattr(_filesystem, 'replace', _fail_replace)

    with pytest.raises(PubanonError, match='cannot save global configuration'):
        _config.save_global(
            paths,
            replace(config, anon_name='Updated Name'),
            original,
        )

    assert paths.global_config.read_bytes() == original
    assert list(paths.config_dir.glob('.global-*.tmp')) == []


def test_save_global_round_trips_escaped_unicode_with_owner_only_permissions(
    tmp_path: Path,
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    config = _config.GlobalConfig(
        anon_name='Public "Name" ☃',
        anon_email=r'public\\alias@example.invalid',
        accounts={'github': r'public\\account'},
        sensitive_terms=('private "term"', r'private\\term', '私密'),
    )

    _config.save_global(paths, config, None)

    assert _config.load_global(paths) == config
    assert_private_mode(paths.config_dir, 0o700)
    assert_private_mode(paths.global_config, 0o600)


def test_save_project_close_failure_is_primary_after_successful_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project = _project_config(tmp_path)
    _config.save_project(paths, project)
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    original = project_file.read_bytes()
    original_fdopen = os.fdopen

    class _FailingCloseHandle:
        def __init__(self, descriptor: int, mode: str) -> None:
            self._handle = original_fdopen(descriptor, mode)

        def close(self) -> Never:
            self._handle.close()
            raise OSError('injected close failure')

        def fileno(self) -> int:
            return self._handle.fileno()

        def flush(self) -> None:
            self._handle.flush()

        def write(self, content: bytes) -> int:
            return self._handle.write(content)

    monkeypatch.setattr(os, 'fdopen', _FailingCloseHandle)

    with pytest.raises(PubanonError, match='cannot save project configuration') as exc_info:
        _config.save_project(paths, replace(project, visibility='public'))

    assert isinstance(exc_info.value.__cause__, OSError)
    assert str(exc_info.value.__cause__) == 'injected close failure'
    assert project_file.read_bytes() == original
    assert not project_file.with_suffix('.toml.tmp').exists()


@pytest.mark.skipif(os.name == 'nt', reason='Windows commits through write-through replacement')
def test_save_project_directory_fsync_failure_occurs_after_replace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project = _project_config(tmp_path)
    _config.save_project(paths, project)
    updated = replace(project, visibility='public')
    fsync_calls = 0
    original_fsync = os.fsync

    def _fail_directory_fsync(descriptor: int) -> None:
        nonlocal fsync_calls
        fsync_calls += 1
        if fsync_calls == 2:
            raise OSError('injected directory fsync failure')
        original_fsync(descriptor)

    monkeypatch.setattr(os, 'fsync', _fail_directory_fsync)

    with pytest.raises(PubanonError, match='cannot save project configuration'):
        _config.save_project(paths, updated)

    assert fsync_calls == 2
    assert _config.load_project(paths, project.name) == updated


def test_save_project_file_fsync_failure_preserves_prior_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project = _project_config(tmp_path)
    _config.save_project(paths, project)
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    original = project_file.read_bytes()

    def _fail_fsync(_descriptor: int) -> Never:
        raise OSError('injected file fsync failure')

    monkeypatch.setattr(os, 'fsync', _fail_fsync)

    with pytest.raises(PubanonError, match='cannot save project configuration'):
        _config.save_project(paths, replace(project, visibility='public'))

    assert project_file.read_bytes() == original
    assert not project_file.with_suffix('.toml.tmp').exists()


def test_save_project_filesystem_failure_is_path_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path / 'private-home')})
    project = _project_config(tmp_path)
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    _config.save_project(paths, project)
    original = project_file.read_bytes()
    updated = replace(project, visibility='public')

    def _fail_open(_path: Path, _flags: int, _mode: int = 0o777) -> Never:
        raise OSError(f'cannot open {project_file}')

    def _fail_cleanup(_path: Path, *, missing_ok: bool = False) -> Never:
        del missing_ok
        raise OSError(f'cannot remove {project_file}')

    monkeypatch.setattr(os, 'open', _fail_open)
    monkeypatch.setattr(Path, 'unlink', _fail_cleanup)

    with pytest.raises(PubanonError) as exc_info:
        _config.save_project(paths, updated)

    assert escape(str(project_file)) not in exc_info.value.render()
    assert isinstance(exc_info.value.__cause__, OSError)
    assert str(exc_info.value.__cause__) == f'cannot open {project_file}'
    assert project_file.read_bytes() == original


@pytest.mark.parametrize('field', ['extra_terms', 'hygiene_terms'])
@pytest.mark.parametrize('term', ['', ' ', '\t\n'])
def test_save_project_refuses_blank_terms_without_changing_file(
    tmp_path: Path,
    field: str,
    term: str,
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project = _project_config(tmp_path)
    _config.save_project(paths, project)
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    original = project_file.read_bytes()

    with pytest.raises(PubanonError, match=rf'{field} must not contain blank terms'):
        _config.save_project(paths, replace(project, **{field: (term,)}))

    assert project_file.read_bytes() == original


@pytest.mark.parametrize(
    ('field', 'value'),
    [
        ('host', 'gitlab'),
        ('allow', cast('tuple[str, ...]', ['src/**'])),
        ('self_publication', cast('bool', 1)),
    ],
)
def test_save_project_refuses_invalid_schema(tmp_path: Path, field: str, value: object) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project = _project_config(tmp_path)
    _config.save_project(paths, project)
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    original = project_file.read_bytes()
    project_file.parent.chmod(0o755)
    original_mode = project_file.parent.stat().st_mode
    invalid = replace(project, **{field: value})

    with pytest.raises(PubanonError, match=field):
        _config.save_project(paths, invalid)

    assert project_file.read_bytes() == original
    assert project_file.parent.stat().st_mode == original_mode


def test_save_project_refuses_missing_settings(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})

    with pytest.raises(PubanonError, match='settings must be configured'):
        _config.save_project(paths, replace(_project_config(tmp_path), settings=None))

    assert not (paths.config_dir / 'projects' / 'demo.toml').exists()


def test_save_project_refuses_noncanonical_source_path(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project = _project_config(tmp_path, source_path=tmp_path / 'source' / '..')

    with pytest.raises(PubanonError, match='source_path'):
        _config.save_project(paths, project)

    assert not (paths.config_dir / 'projects' / 'demo.toml').exists()


def test_save_project_refuses_surrogate_before_changing_file(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project = _project_config(tmp_path)
    _config.save_project(paths, project)
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    original = project_file.read_bytes()
    project_file.parent.chmod(0o755)
    original_mode = project_file.parent.stat().st_mode
    invalid = replace(project, extra_terms=('invalid\ud800term',))

    with pytest.raises(PubanonError, match='invalid project configuration text'):
        _config.save_project(paths, invalid)

    assert project_file.read_bytes() == original
    assert project_file.parent.stat().st_mode == original_mode


@pytest.mark.parametrize('field', ['owner', 'repo', 'stage_repo'])
@pytest.mark.parametrize(
    'value',
    [
        pytest.param('', id='empty'),
        pytest.param('two/segments', id='slash'),
        pytest.param('repo?query', id='query'),
        pytest.param('repo#fragment', id='fragment'),
        pytest.param('repo\x00control', id='nul-control'),
        pytest.param('repo\x7fcontrol', id='del-control'),
        pytest.param('repo\ud800surrogate', id='surrogate'),
        pytest.param(' leading', id='leading-whitespace'),
        pytest.param('trailing ', id='trailing-whitespace'),
    ],
)
def test_save_project_refuses_unsafe_host_segments(
    field: str,
    value: str,
    tmp_path: Path,
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    python_field = '_stage_repo' if field == 'stage_repo' else field
    project = replace(_project_config(tmp_path), **{python_field: value})

    with pytest.raises(PubanonError, match=f'{field}.*host coordinate segment'):
        _config.save_project(paths, project)

    assert not (paths.config_dir / 'projects' / 'demo.toml').exists()


def test_save_project_replace_failure_preserves_prior_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project = _project_config(tmp_path)
    _config.save_project(paths, project)
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    original = project_file.read_bytes()

    def _fail_replace(_path: Path, _target: Path) -> Never:
        raise OSError('injected replace failure')

    monkeypatch.setattr(_filesystem, 'replace', _fail_replace)

    with pytest.raises(PubanonError, match='cannot save project configuration'):
        _config.save_project(paths, replace(project, visibility='public'))

    assert project_file.read_bytes() == original
    assert not project_file.with_suffix('.toml.tmp').exists()


def test_save_project_round_trips_new_optional_fields(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project = replace(
        _project_config(tmp_path),
        _stage_repo='custom-stage',
        settings=_config.Settings(
            description='Published project',
            default_branch='main',
            issues=True,
            wiki=False,
            projects=True,
        ),
        acknowledged_exposure='b' * 64,
        package=_config.PackageObservation(
            index='pypi',
            name='public-package',
            release_checks=('test', 'build'),
        ),
        workflows={
            'staged': ('prepare', 'stage push', 'watch ci --staged'),
            'release': ('inspect', 'publish', 'repo reveal', 'watch package'),
        },
    )

    _config.save_project(paths, project)

    assert _config.load_project(paths, project.name) == project
    saved = tomllib.loads(
        (paths.config_dir / 'projects' / 'demo.toml').read_text(encoding='utf-8')
    )
    assert saved['package'] == {
        'index': 'pypi',
        'name': 'public-package',
        'release_checks': ['test', 'build'],
    }
    assert saved['workflow'] == {
        'staged': {'stages': ['prepare', 'stage push', 'watch ci --staged']},
        'release': {'stages': ['inspect', 'publish', 'repo reveal', 'watch package']},
    }


def test_save_project_round_trips_non_bmp_and_del(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project = replace(
        _project_config(tmp_path),
        repo='public-\U0001f680',
        extra_terms=('public\x7fterm', 'public-\U0001f680-array'),
    )

    _config.save_project(paths, project)

    assert _config.load_project(paths, project.name) == project


@pytest.mark.parametrize('field', ['extra_terms', 'hygiene_terms'])
@pytest.mark.parametrize('terms', [(), ('  first term  ', 'second term ')])
def test_save_project_round_trips_term_bytes_and_order(
    tmp_path: Path,
    field: str,
    terms: tuple[str, ...],
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project = replace(_project_config(tmp_path), **{field: terms})

    _config.save_project(paths, project)

    assert getattr(_config.load_project(paths, project.name), field) == terms


def test_save_project_round_trips_with_owner_only_permissions(tmp_path: Path) -> None:

    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project = _config.ProjectConfig(
        name='demo',
        source_path=tmp_path / 'source with space',
        host='github',
        owner='anonymous-account',
        repo='public-repo',
        visibility='private',
        allow=('src/**', 'quote"file'),
        deny=('notes/**',),
        secret_exceptions=(),
        extra_terms=('private-product',),
        hygiene_terms=('internal-marker',),
        self_publication=False,
        required_checks=('test',),
        settings=_config.Settings(
            description='Public repository',
            default_branch='main',
            issues=True,
            wiki=False,
            projects=True,
        ),
    )

    _config.save_project(paths, project)

    project_file = paths.config_dir / 'projects' / 'demo.toml'
    assert _config.load_project(paths, 'demo') == project
    assert_private_mode(project_file, 0o600)
    assert_private_mode(project_file.parent, 0o700)


def test_save_project_uses_owner_only_temp_and_durable_replace_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project = _project_config(tmp_path)
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    temporary = project_file.with_suffix('.toml.tmp')
    events: list[str] = []
    descriptors: dict[int, str] = {}
    original_fchmod = _filesystem.fchmod
    original_fsync = os.fsync
    original_open = os.open
    original_replace = _filesystem.replace

    def _tracked_open(path: Path, flags: int, mode: int = 0o777) -> int:
        descriptor = original_open(path, flags, mode)
        label = 'temporary' if Path(path) == temporary else 'directory'
        descriptors[descriptor] = label
        events.append(f'open {label} {flags} {mode:o}')
        return descriptor

    def _tracked_fchmod(descriptor: int, mode: int) -> None:
        events.append(f'fchmod {descriptors[descriptor]} {mode:o}')
        original_fchmod(descriptor, mode)

    def _tracked_fsync(descriptor: int) -> None:
        events.append(f'fsync {descriptors[descriptor]}')
        original_fsync(descriptor)

    def _tracked_replace(path: Path, target: Path) -> None:
        assert path == temporary
        assert target == project_file
        assert_private_mode(path, 0o600)
        events.append('replace')
        original_replace(path, target)

    monkeypatch.setattr(os, 'open', _tracked_open)
    monkeypatch.setattr(_filesystem, 'fchmod', _tracked_fchmod)
    monkeypatch.setattr(os, 'fsync', _tracked_fsync)
    monkeypatch.setattr(_filesystem, 'replace', _tracked_replace)

    _config.save_project(paths, project)

    assert events == [
        f'open temporary {os.O_WRONLY | os.O_CREAT | os.O_TRUNC} 600',
        'fchmod temporary 600',
        'fsync temporary',
        'replace',
        *([] if os.name == 'nt' else [f'open directory {os.O_RDONLY} 777', 'fsync directory']),
    ]
    assert _config.load_project(paths, project.name) == project


def test_save_project_write_and_close_failures_preserve_operation_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project = _project_config(tmp_path)
    _config.save_project(paths, project)
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    original = project_file.read_bytes()
    original_fdopen = os.fdopen

    class _FailingWriteHandle:
        def __init__(self, descriptor: int, mode: str) -> None:
            self._handle = original_fdopen(descriptor, mode)

        def close(self) -> Never:
            self._handle.close()
            raise OSError('injected close failure')

        def fileno(self) -> int:
            return self._handle.fileno()

        def flush(self) -> None:
            self._handle.flush()

        def write(self, _content: bytes) -> Never:
            raise OSError('injected write failure')

    monkeypatch.setattr(os, 'fdopen', _FailingWriteHandle)

    with pytest.raises(PubanonError, match='cannot save project configuration') as exc_info:
        _config.save_project(paths, replace(project, visibility='public'))

    assert isinstance(exc_info.value.__cause__, OSError)
    assert str(exc_info.value.__cause__) == 'injected write failure'
    assert project_file.read_bytes() == original
    assert not project_file.with_suffix('.toml.tmp').exists()


def test_settings_load_refuses_non_main_default_branch(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    project_file.parent.mkdir(parents=True)
    project_file.write_text(
        _project_toml(tmp_path / 'source', settings=False) + '[settings]\n'
        'description = "Published project"\n'
        'default_branch = "trunk"\n'
        'issues = true\n'
        'wiki = false\n'
        'projects = true\n'
    )

    with pytest.raises(
        PubanonError,
        match=r'settings\.default_branch must be main',
    ) as exc_info:
        _config.load_project(paths, 'demo')

    assert exc_info.value.recovery == (
        f'set settings.default_branch = "main" in {escape(str(project_file))}'
    )


def test_setup_lock_is_exclusive_and_owner_only(tmp_path: Path) -> None:
    paths = _config.derive_paths({'HOME': str(tmp_path)})

    with (
        _config.setup_lock(paths),
        pytest.raises(PubanonError, match='global setup is active'),
        _config.setup_lock(paths),
    ):
        pytest.fail('conflicting setup lock unexpectedly acquired')

    lock_path = paths.config_dir / 'setup.lock'
    assert_private_mode(paths.config_dir, 0o700)
    assert_private_mode(lock_path, 0o600)


@pytest.mark.parametrize(
    ('override', 'expected'),
    [(None, 'public-repo-stage'), ('custom-stage', 'custom-stage')],
)
def test_stage_repository_uses_override_or_repo_default(
    tmp_path: Path, override: str | None, expected: str
) -> None:
    project = replace(_project_config(tmp_path), _stage_repo=override)

    assert _config.stage_repository(project) == expected


@pytest.mark.parametrize('value', ['repo', 'two words', 'caf\u00e9'])
def test_validate_host_segment_accepts_valid_single_segments(value: str) -> None:
    assert _config.validate_host_segment('repo', value) == value


@pytest.mark.parametrize(
    'value',
    [
        pytest.param('', id='empty'),
        pytest.param('two/segments', id='slash'),
        pytest.param('repo?query', id='query'),
        pytest.param('repo#fragment', id='fragment'),
        pytest.param('repo\x00control', id='nul-control'),
        pytest.param('repo\x7fcontrol', id='del-control'),
        pytest.param('repo\ud800surrogate', id='surrogate'),
        pytest.param(' leading', id='leading-whitespace'),
        pytest.param('trailing ', id='trailing-whitespace'),
    ],
)
def test_validate_host_segment_refuses_unsafe_values(value: str) -> None:
    with pytest.raises(PubanonError, match='host coordinate segment'):
        _config.validate_host_segment('repo', value)


@pytest.mark.parametrize('value', ['', ' Alias', 'A<lias', 'Ali\x00as'])
def test_validate_identity_rejects_unstable_values(value: str) -> None:
    with pytest.raises(PubanonError, match='anon_name must be a nonempty value'):
        _config.validate_identity('anon_name', value)


def test_validate_identity_returns_stable_value() -> None:
    assert _config.validate_identity('anon_name', 'Alias') == 'Alias'


def test_validate_term_rejects_blank() -> None:
    with pytest.raises(PubanonError, match='must not be blank'):
        _config.validate_term('  ')


@pytest.mark.parametrize('name', ['demo', '.demo', ' demo', 'demo.name'])
def test_windows_project_name_accepts_non_alias(name: str) -> None:
    _config._validate_windows_project_name(name)


@pytest.mark.parametrize('name', ['demo.', 'demo ', 'demo..', 'demo. '])
def test_windows_project_name_refuses_terminal_alias(name: str) -> None:
    with pytest.raises(PubanonError, match='invalid project name'):
        _config._validate_windows_project_name(name)
