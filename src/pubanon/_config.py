"""Load Pubanon configuration and coordinate local project state."""

# pyright: reportPrivateUsage=false

import hashlib
import json
import os
import re
import tempfile
import tomllib
import unicodedata
from collections.abc import Collection, Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, NamedTuple, cast

from pubanon import _filesystem, _locking
from pubanon._errors import PubanonError
from pubanon._gate import GateHit, RuleClass, first_match, gate_unit
from pubanon._policy import RESERVED_DENY, literal_pattern
from pubanon._terminal import escape
from pubanon._toml import toml_array, toml_string

PUBLICATION_ROUTES = frozenset({'direct', 'staged'})
VISIBILITIES = frozenset({'private', 'public'})

_ADAPTER_KEYS = frozenset({'github'})
_EXPOSURE_DIGEST_LENGTH = 64
_GLOBAL_KEYS = frozenset(
    {'accounts', 'anon_email', 'anon_name', 'default_publication_route', 'sensitive_terms'}
)
_HOSTS = frozenset({'github'})
_IDENTITY_SOURCE_RECOVERY: Mapping[str, str] = {
    'sensitive_terms': (
        'remove the value from sensitive_terms or choose another publication identity'
    ),
    'extra_terms': (
        "remove the value from the project's extra_terms or choose another publication identity"
    ),
    'private path': 'choose a publication identity that does not contain a private path',
    'hygiene term': (
        'choose another publication identity, or set self_publication if the term is '
        'the product name'
    ),
}
_PACKAGE_INDEXES = frozenset({'pypi'})
_PACKAGE_KEYS = frozenset({'index', 'name', 'release_checks'})
_PROJECT_KEYS = frozenset(
    {
        'allow',
        'deny',
        'extra_terms',
        'host',
        'hygiene_terms',
        'name',
        'owner',
        'publication_route',
        'repo',
        'required_checks',
        'secret_exceptions',
        'self_publication',
        'settings',
        'source_path',
        'visibility',
    }
)
_PROJECT_OPTIONAL_KEYS = frozenset(
    {'acknowledged_exposure', 'package', 'source_tags', 'stage_repo', 'workflow'}
)
_SETTINGS_KEYS = frozenset(
    {'default_branch', 'description', 'discussions', 'issues', 'projects', 'pull_requests', 'wiki'}
)
_WORKFLOW_KEYS = frozenset({'stages'})
_WORKFLOW_STAGES = (
    'prepare',
    'inspect',
    'stage push',
    'stage watch',
    'publish',
    'repo watch',
    'repo reveal',
    'package watch',
)


@dataclass(frozen=True)
class GlobalConfig:
    """Hold the global publication identity and private terms.

    The publication identity may be an alias or a real name. Published-history
    audits require every commit to match the current name and email.

    Attributes:
        anon_name: Author and committer name used for publication commits.
        anon_email: Author and committer email used for publication commits.
        accounts: Expected authenticated account for each host adapter.
        sensitive_terms: Globally configured terms that must remain private.
        default_publication_route: Route selected for newly initialized
            projects.
        ignored_files: Literal basenames at any depth or source-relative file
            paths, matched case-insensitively.
        ignored_directories: Literal source-relative directories excluded in
            every project, matched case-insensitively.
    """

    anon_name: str
    anon_email: str
    accounts: Mapping[str, str]
    sensitive_terms: tuple[str, ...]
    default_publication_route: str = 'staged'
    ignored_directories: tuple[str, ...] = ()
    ignored_files: tuple[str, ...] = ()


@dataclass(frozen=True)
class PackageObservation:
    """Hold the declared package observation target.

    Attributes:
        index: Package index adapter name.
        name: Package name at the selected index.
        release_checks: Checks required for an observed release.
    """

    index: str
    name: str
    release_checks: tuple[str, ...]


class Paths(NamedTuple):
    """Hold the derived Pubanon configuration and data paths.

    Attributes:
        config_dir: Root directory for Pubanon configuration.
        data_dir: Root directory for Pubanon operational state.
        global_config: Global configuration file location.
        registry: Source-to-project registry file location.
    """

    config_dir: Path
    data_dir: Path
    global_config: Path
    registry: Path


@dataclass(frozen=True)
class Settings:
    """Hold the declared repository settings summary.

    Attributes:
        description: Intended host repository description.
        default_branch: Intended host default-branch name.
        issues: Whether the host issue tracker should be enabled.
        wiki: Whether the host wiki should be enabled.
        projects: Whether host projects should be enabled.
        pull_requests: Whether host pull requests should be enabled.
        discussions: Whether host discussions should be enabled.
    """

    description: str
    default_branch: str
    issues: bool
    wiki: bool
    projects: bool
    pull_requests: bool
    discussions: bool


@dataclass(frozen=True)
class ProjectConfig:
    """Hold one project's publication intent and policy.

    Attributes:
        name: Registered project identifier.
        source_path: Canonical local source repository path.
        host: Adapter used for host operations.
        owner: Host namespace that owns the publication repository.
        repo: Final publication repository name.
        visibility: Required visibility of the publication repository.
        allow: Path patterns admitted to publication.
        deny: Path patterns excluded from publication.
        secret_exceptions: Credential-like paths explicitly authorized for
            publication.
        extra_terms: Project-specific terms that must remain private.
        hygiene_terms: Project-specific terms reported as policy mismatches.
        self_publication: Whether to omit the default Pubanon hygiene term.
        required_checks: Host checks required when validating staged or
            published commits.
        settings: Intended host repository settings. `None` is used only while
            assembling an unsaved initialization draft.
        acknowledged_exposure: Digest of accepted identity-critical history
            findings.
        package: Optional package release to observe after publication.
        workflows: Named sequences of publication stages.
        publication_route: Chosen route to the final publication repository.
        source_tags: Whether to mark published versions in the source
            repository.
    """

    name: str
    source_path: Path
    host: str
    owner: str
    repo: str
    visibility: str
    allow: tuple[str, ...]
    deny: tuple[str, ...]
    secret_exceptions: tuple[str, ...]
    extra_terms: tuple[str, ...]
    hygiene_terms: tuple[str, ...]
    self_publication: bool
    required_checks: tuple[str, ...]
    _stage_repo: str | None = None
    settings: Settings | None = None
    acknowledged_exposure: str = ''
    package: PackageObservation | None = None
    workflows: Mapping[str, tuple[str, ...]] = field(default_factory=dict[str, tuple[str, ...]])
    publication_route: str = 'staged'
    source_tags: bool = False


def _close_lock_quietly(lock_file: IO[bytes]) -> None:
    """Close a lock file without replacing an earlier failure."""
    with suppress(OSError, RuntimeError):
        lock_file.close()


def _acquire_lock(lock_file: IO[bytes], message: str) -> None:
    """Acquire one nonblocking lock or raise a path-free refusal."""
    try:
        _locking.acquire(lock_file.fileno())
    except BlockingIOError as exc:
        _close_lock_quietly(lock_file)
        raise PubanonError(
            message,
            recovery='retry after the active operation finishes',
        ) from exc
    except (OSError, RuntimeError) as exc:
        _close_lock_quietly(lock_file)
        raise PubanonError('cannot access operation lock') from exc


def _existing_ancestor(path: Path) -> tuple[Path, tuple[str, ...]]:
    """Split a resolved path into its existing ancestor and missing lexical suffix."""
    missing: list[str] = []
    while True:
        try:
            path.stat()
        except FileNotFoundError:
            missing.append(path.name)
            path = path.parent
        else:
            return path, tuple(reversed(missing))


def _home_path(env: Mapping[str, str]) -> Path:
    """Return the absolute operator home without exposing resolution details."""
    try:
        home = Path(env.get('HOME') or '~').expanduser()
    except (OSError, RuntimeError) as exc:
        raise PubanonError('cannot resolve home directory') from exc
    if not home.is_absolute():
        raise PubanonError('HOME must be absolute')
    return home


def _is_within(path: Path, directory: Path, message: str) -> bool:
    """Compare resolved paths by ancestor identity and any missing lexical suffix."""
    try:
        root, root_suffix = _existing_ancestor(directory)
        existing, suffix = _existing_ancestor(path)
        for ancestor in (existing, *existing.parents):
            if ancestor.samefile(root):
                relative = (*existing.relative_to(ancestor).parts, *suffix)
                return relative[: len(root_suffix)] == root_suffix
    except (OSError, RuntimeError) as exc:
        raise PubanonError(message) from exc
    return False


def _load_registry(path: Path) -> dict[str, str]:
    """Load the source-to-project registry."""
    diagnostic = {
        'fact': f'registry file: {escape(str(path))}',
        'recovery': (
            'restore a known backup or reconstruct the registry from known project '
            'configuration; pass --project NAME to use an existing project'
        ),
    }
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, RuntimeError, UnicodeError, json.JSONDecodeError) as exc:
        raise PubanonError('invalid project registry', **diagnostic) from exc
    if not isinstance(data, dict):
        raise PubanonError('invalid project registry', **diagnostic)
    registry: dict[str, str] = {}
    for source, name in cast('dict[object, object]', data).items():
        if not isinstance(source, str) or not isinstance(name, str):
            raise PubanonError('invalid project registry', **diagnostic)
        registry[source] = name
    return registry


def _open_lock(path: Path) -> IO[bytes]:
    """Open a lock with owner-only POSIX permissions without exposing its private path."""
    lock_file: IO[bytes] | None = None
    try:
        try:
            path.parent.mkdir(mode=0o700)
        except FileExistsError:
            pass
        else:
            _filesystem.chmod(path.parent, 0o700)
        lock_file = path.open('ab')
        _filesystem.fchmod(lock_file.fileno(), 0o600)
    except (OSError, RuntimeError) as exc:
        if lock_file is not None:
            _close_lock_quietly(lock_file)
        raise PubanonError('cannot access operation lock') from exc
    return lock_file


def _release_lock(lock_file: IO[bytes]) -> None:
    """Release one lock and translate ordinary cleanup failures."""
    try:
        _locking.release(lock_file.fileno())
    except (OSError, RuntimeError) as exc:
        _close_lock_quietly(lock_file)
        raise PubanonError('cannot release operation lock') from exc
    try:
        lock_file.close()
    except (OSError, RuntimeError) as exc:
        raise PubanonError('cannot release operation lock') from exc


@contextmanager
def _exclusive_lock(path: Path, message: str) -> Generator[None]:
    """Hold one nonblocking lock for a cooperating Pubanon operation."""
    lock_file = _open_lock(path)
    _acquire_lock(lock_file, message)
    try:
        yield
    finally:
        _release_lock(lock_file)


def _require_boolean(data: Mapping[str, object], key: str) -> bool:
    """Return one required boolean configuration value."""
    value = data[key]
    if not isinstance(value, bool):
        raise PubanonError(f'{key} must be a boolean')
    return value


def _require_exact_keys(data: Mapping[str, object], expected: frozenset[str], scope: str) -> None:
    """Refuse missing and unknown configuration keys."""
    unknown = sorted(data.keys() - expected)
    if unknown:
        raise PubanonError(f'unknown {scope} key: {", ".join(unknown)}')
    missing = sorted(expected - data.keys())
    if missing:
        raise PubanonError(f'missing {scope} key: {", ".join(missing)}')


def _require_global_snapshot(path: Path, expected: bytes | None) -> None:
    """Require the global configuration bytes observed before setup."""
    try:
        current = path.read_bytes()
    except FileNotFoundError:
        current = None
    if current != expected:
        raise PubanonError(
            'global configuration changed during setup',
            recovery='rerun pubanon setup',
        )


def _require_string(data: Mapping[str, object], key: str) -> str:
    """Return one required string configuration value."""
    value = data[key]
    if not isinstance(value, str):
        raise PubanonError(f'{key} must be a string')
    return value


def _require_absolute_path(data: Mapping[str, object], key: str) -> Path:
    """Return one required absolute path."""
    path = Path(_require_string(data, key))
    if not path.is_absolute():
        raise PubanonError(f'{key} must be absolute')
    return path


def _require_choice(data: Mapping[str, object], key: str, choices: frozenset[str]) -> str:
    """Return one required string selected from a closed set."""
    value = _require_string(data, key)
    if value not in choices:
        raise PubanonError(f'{key} must be one of {", ".join(sorted(choices))}')
    return value


def _require_nonempty_string(data: Mapping[str, object], key: str) -> str:
    """Return one required nonempty string configuration value."""
    value = _require_string(data, key)
    if not value:
        raise PubanonError(f'{key} must be nonempty')
    return value


def _require_strings(data: Mapping[str, object], key: str) -> tuple[str, ...]:
    """Return one required list of string configuration values."""
    value = data[key]
    if not isinstance(value, list):
        raise PubanonError(f'{key} must be a list of strings')
    strings: list[str] = []
    for item in cast('list[object]', value):
        if not isinstance(item, str):
            raise PubanonError(f'{key} must be a list of strings')
        strings.append(item)
    return tuple(strings)


def _package_from_mapping(data: Mapping[str, object]) -> PackageObservation:
    """Validate and return one package observation target."""
    _require_exact_keys(data, _PACKAGE_KEYS, 'package')
    release_checks = _require_strings(data, 'release_checks')
    if not release_checks:
        raise PubanonError('release_checks must contain at least one check')
    return PackageObservation(
        index=_require_choice(data, 'index', _PACKAGE_INDEXES),
        name=_require_nonempty_string(data, 'name'),
        release_checks=release_checks,
    )


def _require_table(data: Mapping[str, object], key: str) -> dict[str, object]:
    """Return one required TOML table."""
    value = data[key]
    if not isinstance(value, dict):
        raise PubanonError(f'{key} must be a table')
    table: dict[str, object] = {}
    for table_key, item in cast('dict[object, object]', value).items():
        if not isinstance(table_key, str):
            raise PubanonError(f'{key} must be a table')
        table[table_key] = item
    return table


def _require_tuple_strings(data: Mapping[str, object], key: str) -> tuple[str, ...]:
    """Return one required tuple of string configuration values."""
    value = data[key]
    if not isinstance(value, tuple):
        raise PubanonError(f'{key} must be a tuple of strings')
    items = cast('tuple[object, ...]', value)
    if not all(isinstance(item, str) for item in items):
        raise PubanonError(f'{key} must be a tuple of strings')
    return cast('tuple[str, ...]', items)


def _resolve_path(path: Path, message: str) -> Path:
    """Resolve a path while replacing private filesystem diagnostics."""
    try:
        return path.resolve()
    except (OSError, RuntimeError) as exc:
        raise PubanonError(message) from exc


def _settings_from_mapping(data: Mapping[str, object]) -> Settings:
    """Validate and return one repository settings summary."""
    _require_exact_keys(data, _SETTINGS_KEYS, 'settings')
    settings = Settings(
        description=_require_string(data, 'description'),
        default_branch=_require_nonempty_string(data, 'default_branch'),
        issues=_require_boolean(data, 'issues'),
        wiki=_require_boolean(data, 'wiki'),
        projects=_require_boolean(data, 'projects'),
        pull_requests=_require_boolean(data, 'pull_requests'),
        discussions=_require_boolean(data, 'discussions'),
    )
    if settings.default_branch != 'main':
        raise PubanonError('settings.default_branch must be main')
    return settings


def _sync_directory(path: Path) -> None:
    """Complete the platform-specific commit for a prior file replacement."""
    _filesystem.sync_directory(path)


def _toml_array(values: tuple[str, ...]) -> str:
    """Serialize project strings as one TOML array."""
    try:
        return toml_array(values)
    except ValueError as exc:
        raise PubanonError('invalid project configuration text') from exc


def _toml_string(value: str) -> str:
    """Serialize one project string as TOML syntax."""
    try:
        return toml_string(value)
    except ValueError as exc:
        raise PubanonError('invalid project configuration text') from exc


def _validate_acknowledged_exposure(value: object) -> str:
    """Validate and return one exposure acknowledgment digest."""
    if not isinstance(value, str) or (
        value
        and (
            len(value) != _EXPOSURE_DIGEST_LENGTH
            or any(character not in '0123456789abcdef' for character in value)
        )
    ):
        raise PubanonError('acknowledged_exposure must be empty or 64 lowercase hex characters')
    return value


def _validate_package(value: object) -> PackageObservation | None:
    """Validate and return an optional in-memory package observation target."""
    if value is None:
        return None
    if not isinstance(value, PackageObservation):
        raise PubanonError('package must be PackageObservation or None')
    return _package_from_mapping(
        {
            'index': value.index,
            'name': value.name,
            'release_checks': list(value.release_checks),
        }
    )


def _validate_windows_project_name(name: str) -> None:
    """Refuse project names that alias another Windows path component."""
    if name.endswith(('.', ' ')):
        raise PubanonError('invalid project name')


def _workflows_from_mapping(
    data: Mapping[str, object],
) -> dict[str, tuple[str, ...]]:
    """Validate and return workflow stage declarations."""
    workflows: dict[str, tuple[str, ...]] = {}
    for name in data:
        if not name:
            raise PubanonError('workflow name must be nonempty')
        workflow = _require_table(data, name)
        _require_exact_keys(workflow, _WORKFLOW_KEYS, f'workflow {name}')
        stages = _require_strings(workflow, 'stages')
        previous_index = -1
        seen: set[str] = set()
        for stage in stages:
            if stage not in _WORKFLOW_STAGES:
                raise PubanonError(f'workflow {name} contains unknown stage: {stage}')
            if stage in seen:
                raise PubanonError(f'workflow {name} contains duplicate stage: {stage}')
            stage_index = _WORKFLOW_STAGES.index(stage)
            if stage_index < previous_index:
                raise PubanonError(f'workflow {name} stage is out of order: {stage}')
            previous_index = stage_index
            seen.add(stage)
        workflows[name] = stages
    return workflows


def _validate_workflows(value: object) -> Mapping[str, tuple[str, ...]]:
    """Validate and return in-memory workflow stage declarations."""
    if not isinstance(value, Mapping):
        raise PubanonError('workflows must be a mapping')
    tables: dict[str, object] = {}
    for name, stages in cast('Mapping[object, object]', value).items():
        if not isinstance(name, str):
            raise PubanonError('workflow names must be strings')
        if not isinstance(stages, tuple):
            raise PubanonError(f'workflow {name} stages must be a tuple of strings')
        stage_items = cast('tuple[object, ...]', stages)
        if not all(isinstance(stage, str) for stage in stage_items):
            raise PubanonError(f'workflow {name} stages must be a tuple of strings')
        typed_stages = cast('tuple[str, ...]', stage_items)
        tables[name] = {'stages': list(typed_stages)}
    return _workflows_from_mapping(tables)


def _write_global_temporary(directory: Path, payload: bytes) -> Path:
    """Write and sync a unique global configuration temporary with owner-only POSIX permissions."""
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix='.global-',
            suffix='.tmp',
            dir=directory,
        )
        temporary = Path(temporary_name)
        _filesystem.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, 'wb')
        descriptor = None
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        except (OSError, RuntimeError, UnicodeError, KeyboardInterrupt):
            with suppress(OSError, RuntimeError):
                handle.close()
            raise
        handle.close()
    except (OSError, RuntimeError, UnicodeError, KeyboardInterrupt):
        if descriptor is not None:
            with suppress(OSError, RuntimeError):
                os.close(descriptor)
        if temporary is not None:
            with suppress(OSError, RuntimeError):
                temporary.unlink(missing_ok=True)
        raise
    return temporary


def _write_registry(paths: Paths, registry: dict[str, str]) -> None:
    """Durably replace registry contents while its lock is held."""
    payload = (json.dumps(registry, separators=(',', ':')) + '\n').encode('utf-8')
    descriptor: int | None = None
    temporary: Path | None = None
    replacement_attempted = False
    try:
        try:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix='.registry-', suffix='.tmp', dir=paths.registry.parent
            )
            temporary = Path(temporary_name)
            _filesystem.fchmod(descriptor, 0o600)
            handle = os.fdopen(descriptor, 'wb')
            descriptor = None
            try:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            except (OSError, RuntimeError, KeyboardInterrupt):
                with suppress(OSError, RuntimeError):
                    handle.close()
                raise
            handle.close()
            replacement_attempted = True
            _filesystem.replace(temporary, paths.registry)
        except (OSError, RuntimeError) as exc:
            raise PubanonError(
                'cannot update project registry',
                fact=f'registry file: {escape(str(paths.registry))}',
            ) from exc
        except KeyboardInterrupt as exc:
            raise PubanonError(
                (
                    'project registry may have been saved'
                    if replacement_attempted
                    else 'cannot update project registry'
                ),
                fact=f'registry file: {escape(str(paths.registry))}',
            ) from exc
    finally:
        if descriptor is not None:
            with suppress(OSError, RuntimeError):
                os.close(descriptor)
        if temporary is not None:
            with suppress(OSError, RuntimeError):
                temporary.unlink(missing_ok=True)
    try:
        _sync_directory(paths.registry.parent)
    except (OSError, RuntimeError, KeyboardInterrupt) as exc:
        raise PubanonError(
            'project registry may have been saved',
            fact=f'registry file: {escape(str(paths.registry))}',
        ) from exc


def assemble_rules(
    g: GlobalConfig, p: ProjectConfig, derived: Iterable[str]
) -> dict[RuleClass, tuple[str, ...]]:
    """Assemble identity-critical and hygiene content rules.

    Args:
        g: Global configuration.
        p: Project configuration.
        derived: Private path terms derived for this command.

    Returns:
        Active terms grouped by security class.
    """
    hygiene = p.hygiene_terms if p.self_publication else ('pubanon', *p.hygiene_terms)
    return {
        RuleClass.IDENTITY_CRITICAL: (*g.sensitive_terms, *p.extra_terms, *derived),
        RuleClass.HYGIENE: hygiene,
    }


def check_publication_state_paths(
    paths: Paths, project: ProjectConfig, snapshot_paths: Collection[str]
) -> None:
    """Refuse selected files under this project's private publication state.

    Paths outside the configured source root impose no selection restriction.
    Ordinary directories sharing a state-directory name remain publishable.
    """
    source = _resolve_path(project.source_path, 'cannot resolve project source')
    project_state = paths.data_dir / 'projects' / project.name
    for name in ('publications', 'source-worktrees', 'rewrite', 'rewrite.toml'):
        directory = _resolve_path(project_state / name, 'cannot resolve publication state')
        if not _is_within(directory, source, 'cannot inspect publication state'):
            continue
        for selected in snapshot_paths:
            selected_path = _resolve_path(
                source / selected, 'cannot resolve selected snapshot path'
            )
            if _is_within(selected_path, directory, 'cannot inspect selected snapshot path'):
                raise PubanonError(
                    'selected snapshot includes private publication state',
                    fact=escape(str(source / selected)),
                    recovery=f'Exclude {escape(str(directory))} from selection or relocate '
                    'private state outside the source repository.',
                )


def configuration_error(path: Path, role: str, cause: Exception) -> PubanonError:
    """Describe a configuration read failure without echoing private parser text.

    Args:
        path: Actual operator-local file location, shown with controls escaped.
        role: File role, such as global or project.
        cause: Failure from reading or parsing that file; raw text stays
            private.

    Returns:
        A safe category and usable recovery location, with TOML coordinates
        when the interpreter supplies them.
    """
    category = 'unreadable'
    if isinstance(cause, FileNotFoundError):
        category = 'missing'
    elif isinstance(cause, PermissionError):
        category = 'permission denied'
    elif isinstance(cause, tomllib.TOMLDecodeError):
        category = 'malformed TOML'
        # Python 3.13 exposes coordinates only in the standard numeric suffix.
        if position := re.search(r'\(at line (\d+), column (\d+)\)$', str(cause)):
            category += f' at line {position[1]}, column {position[2]}'
    elif isinstance(cause, UnicodeError):
        category = 'invalid UTF-8'
    return PubanonError(
        f'invalid {role} configuration: {category}',
        recovery=f'review {role} configuration at {escape(str(path))}',
    )


def _load_toml(path: Path, role: str) -> dict[str, object]:
    """Load TOML with a usable file location and a safe failure category."""
    try:
        return tomllib.loads(path.read_text(encoding='utf-8'))
    except (OSError, RuntimeError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise configuration_error(path, role, exc) from exc


def derive_paths(env: Mapping[str, str]) -> Paths:
    """Derive Pubanon paths from one environment snapshot.

    Args:
        env: Environment variables captured for the command. Nonempty
            `XDG_CONFIG_HOME` and `XDG_DATA_HOME` select their respective
            roots. Otherwise use `.config` and `.local/share` under `HOME`,
            falling back to the current user's home when `HOME` is empty or
            absent.

    Returns:
        Configuration and data paths beneath each root's `pubanon` directory.
        No directories or files are created.

    Raises:
        PubanonError: The operator home cannot be resolved or is not absolute,
            or an explicitly configured XDG root is not absolute.
    """
    home = _home_path(env)
    config_override = env.get('XDG_CONFIG_HOME')
    config_root = Path(config_override) if config_override else home / '.config'
    if config_override and not config_root.is_absolute():
        raise PubanonError('XDG_CONFIG_HOME must be absolute')
    data_override = env.get('XDG_DATA_HOME')
    data_root = Path(data_override) if data_override else home / '.local' / 'share'
    if data_override and not data_root.is_absolute():
        raise PubanonError('XDG_DATA_HOME must be absolute')
    config_dir = config_root / 'pubanon'
    data_dir = data_root / 'pubanon'
    return Paths(config_dir, data_dir, config_dir / 'config.toml', data_dir / 'registry.json')


def fingerprint(g: GlobalConfig, p: ProjectConfig) -> str:
    """Hash the complete publication-governing configuration projection.

    Args:
        g: Global configuration.
        p: Project configuration.

    Returns:
        Lowercase SHA-256 hexadecimal digest.
    """
    projection: dict[str, object] = {
        'ignored_directories': g.ignored_directories,
        'ignored_files': g.ignored_files,
        'accounts': dict(g.accounts),
        'allow': p.allow,
        'anon_email': g.anon_email,
        'anon_name': g.anon_name,
        'deny': p.deny,
        'extra_terms': p.extra_terms,
        'host': p.host,
        'hygiene_terms': p.hygiene_terms,
        'owner': p.owner,
        'repo': p.repo,
        'required_checks': p.required_checks,
        'reserved_deny': sorted(RESERVED_DENY),
        'secret_exceptions': p.secret_exceptions,
        'self_publication': p.self_publication,
        'sensitive_terms': g.sensitive_terms,
        'source_path': str(p.source_path),
        'visibility': p.visibility,
    }
    if g.ignored_directories or g.ignored_files:
        # Rebuild snapshots prepared with the previous case-sensitive exclusions.
        projection['global_deny_casefold'] = True
    serialized = json.dumps(projection, sort_keys=True, separators=(',', ':'))
    return hashlib.sha256(serialized.encode()).hexdigest()


def global_deny(g: GlobalConfig) -> tuple[str, ...]:
    """Return literal file and directory exclusion patterns for every project."""
    return tuple(
        pattern
        for directory in g.ignored_directories
        for pattern in (literal_pattern(directory), f'{literal_pattern(directory)}/**')
    ) + tuple(
        pattern
        for file in g.ignored_files
        for pattern in (
            (literal_pattern(file), f'**/{literal_pattern(file)}')
            if '/' not in file
            else (literal_pattern(file),)
        )
    )


def identity_refusal(sources: Sequence[str]) -> PubanonError:
    """Build the refusal for a publication identity that matches active rules.

    Args:
        sources: Rule sources from `identity_rule_sources`, in its order.

    Returns:
        An error naming the sources and the recovery for each, never the
        matched term.
    """
    message = 'publication identity matches an active disclosure rule'
    fact = escape('rule source: ' + ', '.join(sources))
    recovery = escape('; '.join(_IDENTITY_SOURCE_RECOVERY[source] for source in sources))
    return PubanonError(
        message,
        fact=fact,
        recovery=recovery,
    )


def identity_rule_sources(
    hits: Iterable[GateHit],
    g: GlobalConfig,
    p: ProjectConfig | None,
    derived: Collection[str],
) -> tuple[str, ...]:
    """Name the configured rule sources behind publication-identity hits.

    Args:
        hits: Gate hits for the identity unit.
        g: Global configuration.
        p: Project configuration, or None before a project is selected.
        derived: Private path terms active for this command.

    Returns:
        Each matched source at most once, ordered `sensitive_terms`,
        `extra_terms`, `private path`, `hygiene term`. One term listed in
        several sources names every source it appears in.
    """
    extra_terms = () if p is None else p.extra_terms
    found: set[str] = set()
    for hit in hits:
        if hit.rule_class is RuleClass.HYGIENE:
            found.add('hygiene term')
            continue
        if hit.term in g.sensitive_terms:
            found.add('sensitive_terms')
        if hit.term in extra_terms:
            found.add('extra_terms')
        if hit.term in derived:
            found.add('private path')
    return tuple(source for source in _IDENTITY_SOURCE_RECOVERY if source in found)


def machine_private_terms(paths: Paths, env: Mapping[str, str]) -> tuple[str, ...]:
    """Return private paths known without selecting a project.

    Args:
        paths: Paths derived for this command.
        env: Environment variables captured for the command.

    Returns:
        Home, configuration root, and data root strings.

    Raises:
        PubanonError: The operator home cannot be resolved safely.
    """
    return (str(_home_path(env)), str(paths.config_dir), str(paths.data_dir))


def derived_private_terms(
    paths: Paths, p: ProjectConfig, env: Mapping[str, str]
) -> tuple[str, ...]:
    """Return private filesystem paths that content gates must block.

    Args:
        paths: Paths derived for this command.
        p: Project configuration.
        env: Environment variables captured for the command.

    Returns:
        Home, configuration, data, and canonical source paths.

    Raises:
        PubanonError: The operator home or project source cannot be resolved
            safely.
    """
    return (
        *machine_private_terms(paths, env),
        str(_resolve_path(p.source_path, 'cannot resolve project source')),
    )


def require_project_name(name: str) -> str:
    """Return one path-safe project name.

    Args:
        name: Requested project name.

    Returns:
        The validated name.

    Raises:
        PubanonError: The name is empty or unsafe as a path component.
    """
    if not name or name in {'.', '..'} or Path(name).name != name:
        raise PubanonError('invalid project name')
    if os.name == 'nt':
        _validate_windows_project_name(name)
    return name


def create_data_dirs(paths: Paths, name: str | None) -> Path:
    """Create Pubanon data directories for one optional project.

    POSIX mode bits restrict the directories to owner-only access.

    Args:
        paths: Paths derived for this command.
        name: Project name, or `None` for the data root only.

    Returns:
        The deepest created directory.

    Raises:
        PubanonError: A directory cannot be created or restricted.
    """
    if name is not None:
        require_project_name(name)
    levels = [paths.data_dir]
    if name is not None:
        levels.extend((paths.data_dir / 'projects', paths.data_dir / 'projects' / name))
    try:
        paths.data_dir.parent.mkdir(parents=True, exist_ok=True)
        for level in levels:
            level.mkdir(mode=0o700, exist_ok=True)
            _filesystem.chmod(level, 0o700)
    except (OSError, RuntimeError) as exc:
        raise PubanonError('cannot create Pubanon data directories') from exc
    return levels[-1]


@contextmanager
def project_lock(paths: Paths, name: str) -> Generator[None]:
    """Hold the nonblocking lock for one project's mutating operation.

    Args:
        paths: Paths derived for this command.
        name: Project name.

    Yields:
        Control while the project lock is held.

    Raises:
        PubanonError: Project data directories cannot be created or restricted,
            or operation lock access, acquisition, or release fails.
    """
    create_data_dirs(paths, name)
    lock_path = paths.data_dir / 'projects' / name / 'project.lock'
    with _exclusive_lock(lock_path, 'another Pubanon command is running for this project'):
        yield


def registry_lookup(paths: Paths, cwd: Path) -> str | None:
    """Resolve the most specific registered source containing a working directory.

    Args:
        paths: Paths derived for this command.
        cwd: Working directory to resolve.

    Returns:
        Registered project name, or `None` outside registered sources.

    Raises:
        PubanonError: Data-directory setup, registry-lock access, registry
            decoding, or working-directory resolution fails; registered-source
            resolution failure is reported as invalid registry state.
    """
    if not paths.registry.exists() and not paths.data_dir.exists():
        return None
    create_data_dirs(paths, None)
    with _exclusive_lock(paths.registry.with_name('registry.lock'), 'project registry is active'):
        registry = _load_registry(paths.registry)
    resolved = _resolve_path(cwd, 'cannot resolve working directory')
    matches = [
        (resolved_source, name)
        for source, name in registry.items()
        if _is_within(
            resolved,
            resolved_source := _resolve_path(Path(source), 'invalid project registry'),
            'cannot inspect project registry paths',
        )
    ]
    if not matches:
        return None
    return max(matches, key=lambda item: len(item[0].parts))[1]


def registry_remove(paths: Paths, name: str) -> bool:
    """Remove every registration for a project under the registry lock.

    Returns:
        Whether any entry was removed.
    """
    create_data_dirs(paths, None)
    with _exclusive_lock(paths.registry.with_name('registry.lock'), 'project registry is active'):
        registry = _load_registry(paths.registry)
        remaining = {source: project for source, project in registry.items() if project != name}
        if remaining == registry:
            return False
        _write_registry(paths, remaining)
        return True


def registry_source(paths: Paths, name: str) -> str | None:
    """Read a project's registered canonical source under the registry lock.

    Returns:
        The registered source, or `None` when no entry maps to the name.
    """
    if not paths.registry.exists() and not paths.data_dir.exists():
        return None
    create_data_dirs(paths, None)
    with _exclusive_lock(paths.registry.with_name('registry.lock'), 'project registry is active'):
        registry = _load_registry(paths.registry)
    return next((source for source, project in registry.items() if project == name), None)


def rule_labels(
    hit: GateHit, g: GlobalConfig, p: ProjectConfig | None, derived: Collection[str]
) -> tuple[str, ...]:
    """Name all active origins of a hit without exposing terms or paths.

    Args:
        hit: Gate match whose origins are labeled.
        g: Global configuration supplying sensitive terms.
        p: Project configuration, or `None` when no project is selected.
        derived: Private path terms derived for this project.

    Returns:
        Source labels with one-based entry numbers in global, project, derived,
        then hygiene order. Labels use the supplied configuration snapshot; the
        built-in product rule has no configuration entry.
    """
    sources = (
        ('global sensitive_terms', g.sensitive_terms),
        ('project extra_terms', () if p is None else p.extra_terms),
        ('derived private path', derived),
    )
    if hit.rule_class is RuleClass.IDENTITY_CRITICAL:
        return tuple(
            f'{label} entry {index}'
            for label, terms in sources
            for index, term in enumerate(terms, 1)
            if term == hit.term
        )
    labels: list[str] = []
    if p is not None:
        if not p.self_publication and hit.term == 'pubanon':
            labels.append('built-in hygiene rule')
        labels.extend(
            f'project hygiene_terms entry {index}'
            for index, term in enumerate(p.hygiene_terms, 1)
            if term == hit.term
        )
    return tuple(labels)


def gate_details(
    units: Iterable[tuple[str, bytes, str]],
    g: GlobalConfig,
    p: ProjectConfig,
    derived: Collection[str],
) -> dict[str, str]:
    """Collect literal first-match diagnostics for explicit local gate review.

    Args:
        units: Unit name, raw bytes, and byte-location label for each scanned
            unit.
        g: Current global rule configuration.
        p: Current project rule configuration.
        derived: Active private path terms in their established order.

    Returns:
        Details name the term, the unit, and the byte range so the operator can
        find the match.
    """
    rules = assemble_rules(g, p, derived)
    details: dict[str, str] = {}
    for name, data, location in units:
        for hit in gate_unit(name, data, rules):
            span = first_match(data, hit.term)
            if span is None:
                raise PubanonError('gate match location could not be determined')
            start, end = span
            labels = ', '.join(rule_labels(hit, g, p, derived))
            details[f'Finding {len(details) + 1}'] = (
                f'{labels}; unit {name}; {location} bytes {start}:{end}; term {hit.term}'
            )
    return details


def same_source(a: Path, b: Path) -> bool:
    """Return whether two paths name one filesystem source.

    Args:
        a: First absolute path.
        b: Second absolute path.

    Returns:
        Whether the paths are equal or both exist and share filesystem
        identity.
    """
    if a == b:
        return True
    try:
        return a.exists() and b.exists() and a.samefile(b)
    except (OSError, RuntimeError):
        return False


def _set_registry_entry(paths: Paths, canonical_source: str, name: str) -> None:
    """Set one registry entry while the caller holds the registry lock."""
    registry = _load_registry(paths.registry)
    for registered_source, registered_name in registry.items():
        if (
            same_source(Path(registered_source), Path(canonical_source))
            and registered_name != name
        ):
            raise PubanonError('source is already registered to another project')
    registry = {
        registered_source: registered_name
        for registered_source, registered_name in registry.items()
        if registered_name != name
    }
    registry[canonical_source] = name
    _write_registry(paths, registry)


def registry_set(paths: Paths, source: Path, name: str) -> None:
    """Register a canonical source path under the registry lock.

    Args:
        paths: Paths derived for this command.
        source: Source repository path.
        name: Project name.

    Raises:
        PubanonError: The source cannot be registered.
    """
    lock_path = paths.registry.with_name('registry.lock')
    canonical_source = str(_resolve_path(source, 'cannot resolve registry source'))
    create_data_dirs(paths, None)
    with _exclusive_lock(lock_path, 'project registry is active'):
        _set_registry_entry(paths, canonical_source, name)


@contextmanager
def setup_lock(paths: Paths) -> Generator[None]:
    """Hold the nonblocking lock for one guided setup operation.

    Args:
        paths: Paths derived for this command.

    Yields:
        Control while the setup lock is held.

    Raises:
        PubanonError: Lock access, acquisition, or release fails.
    """
    try:
        paths.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        _filesystem.chmod(paths.config_dir, 0o700)
    except (OSError, RuntimeError) as exc:
        raise PubanonError('cannot access operation lock') from exc
    with _exclusive_lock(paths.config_dir / 'setup.lock', 'global setup is active'):
        yield


def stage_repository(p: ProjectConfig) -> str:
    """Return the configured or default staging repository name.

    Args:
        p: Project configuration.

    Returns:
        Staging repository name.
    """
    return p._stage_repo or f'{p.repo}-stage'


def validate_host_segment(field: str, value: object) -> str:
    """Validate and return one host coordinate segment.

    Args:
        field: Configuration field named in validation failures.
        value: Candidate host coordinate segment.

    Returns:
        The validated host coordinate segment.

    Raises:
        PubanonError: `value` is not a safe nonempty host coordinate segment.
    """
    labels = {
        'owner': 'GitHub owner',
        'repo': 'repository name',
        'stage_repo': 'staging repository name',
    }
    label = labels.get(field, field.replace('_', ' '))
    if not isinstance(value, str):
        raise PubanonError(f'invalid {label}')
    invalid = (
        not value
        or value != value.strip()
        or any(character in '/?#' for character in value)
        or any(
            (character.isascii() and unicodedata.category(character) == 'Cc')
            or unicodedata.category(character) == 'Cs'
            for character in value
        )
    )
    if invalid:
        raise PubanonError(f'invalid {label}')
    return value


def _validate_stage_repo(value: object, repo: str) -> str | None:
    """Validate and return an optional staging repository name."""
    if value is None:
        return None
    stage_repo = validate_host_segment('stage_repo', value)
    if stage_repo == repo:
        raise PubanonError('stage_repo must differ from repo')
    return stage_repo


def gate_host_coordinates(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
    *,
    coordinates: tuple[str, str],
) -> None:
    """Gate locally selected host coordinates before any host contact.

    Args:
        paths: Derived Pubanon configuration and data paths.
        g: Global publication identity configuration.
        p: Project publication configuration.

        coordinates: The `(owner, repo)` pair to gate, which may differ from
            the project's final publication destination.

    Raises:
        PubanonError: A coordinate segment has invalid syntax or matches an
            active configured or derived content-gate term. Both failures occur
            locally before any host contact.
    """
    owner, repo = coordinates
    fields = (
        ('owner', validate_host_segment('owner', owner)),
        ('repo', validate_host_segment('repo', repo)),
    )
    rules = assemble_rules(g, p, derived_private_terms(paths, p, os.environ))
    for field_name, value in fields:
        unit = f'repository {field_name}'
        hits = gate_unit(unit, value.encode(), rules)
        if hits:
            recovery = None
            if not p.self_publication and any(
                hit.rule_class is RuleClass.HYGIENE and hit.term == 'pubanon' for hit in hits
            ):
                recovery = (
                    'A built-in rule flags the word pubanon. If you are publishing Pubanon '
                    'itself, enable self_publication. During setup, press Ctrl-C and rerun '
                    'your init command with --self-publication '
                    '(pubanon init --self-publication), then choose Resume.'
                )
            label = 'repository name' if field_name == 'repo' else unit
            raise PubanonError(
                f'{label} did not pass content checks',
                fact=f'{field_name}: {escape(value)}',
                recovery=recovery,
            )


def validate_identity(key: str, value: str) -> str:
    """Validate and return one publication identity value Git preserves exactly.

    Args:
        key: Configuration field named in validation failures.
        value: Candidate publication identity value.

    Returns:
        The validated publication identity value.

    Raises:
        PubanonError: The value is empty or unstable in Git identity syntax.
    """
    unstable = (
        not value
        or value != value.strip()
        or '<' in value
        or '>' in value
        or any(unicodedata.category(character) in {'Cc', 'Cs'} for character in value)
    )
    if unstable:
        raise PubanonError(
            f'{key} must be a nonempty value without control characters, '
            'angle brackets, or surrounding whitespace'
        )
    return value


def _require_identity(data: Mapping[str, object], key: str) -> str:
    """Return one publication identity value Git preserves exactly."""
    return validate_identity(key, _require_string(data, key))


def validate_ignored_directory(value: str) -> str:
    """Validate a literal source-relative directory, accepting one trailing slash.

    Returns:
        The directory with one trailing slash removed, if present.

    Raises:
        PubanonError: The directory is empty, absolute, or contains
            noncanonical components.
    """
    directory = value.removesuffix('/')
    if (
        not directory
        or any(part in {'', '.', '..'} for part in directory.split('/'))
        or '\\' in directory
        or ':' in directory
        or any(unicodedata.category(character) in {'Cc', 'Cs'} for character in directory)
    ):
        raise PubanonError('ignored_directories must contain source-relative directory paths')
    return directory


def validate_ignored_file(value: str) -> str:
    """Validate a literal basename or source-relative file path.

    Raises:
        PubanonError: The value is not a canonical relative file path.
    """
    if value.endswith('/'):
        raise PubanonError('ignored_files must not end with a slash')
    try:
        return validate_ignored_directory(value)
    except PubanonError as error:
        raise PubanonError(
            'ignored_files must contain filenames or source-relative file paths'
        ) from error


def validate_settings(value: object) -> Settings | None:
    """Validate optional in-memory repository settings.

    Args:
        value: Candidate settings value.

    Returns:
        The validated settings, or `None` when settings are undeclared.

    Raises:
        PubanonError: The value is not a supported settings declaration.
    """
    if value is None:
        return None
    if not isinstance(value, Settings):
        raise PubanonError('settings must be Settings or None')
    return _settings_from_mapping(
        {
            'description': value.description,
            'default_branch': value.default_branch,
            'issues': value.issues,
            'wiki': value.wiki,
            'projects': value.projects,
            'pull_requests': value.pull_requests,
            'discussions': value.discussions,
        }
    )


def validate_term(value: str) -> str:
    """Validate and return one sensitive term.

    Args:
        value: Candidate sensitive term.

    Returns:
        The validated sensitive term.

    Raises:
        PubanonError: The term is blank after normalization.
    """
    if not unicodedata.normalize('NFC', value).casefold().strip():
        raise PubanonError('sensitive term must not be blank')
    return value


def _require_terms(data: Mapping[str, object], key: str, *, require_one: bool) -> tuple[str, ...]:
    """Return one validated term list with no blank or normalization-empty entry."""
    terms = _require_strings(data, key)
    if require_one and not terms:
        raise PubanonError(f'{key} must contain at least one term')
    for term in terms:
        try:
            validate_term(term)
        except PubanonError:
            raise PubanonError(f'{key} must not contain blank terms') from None
    return terms


def _validate_project_config(cfg: ProjectConfig, expected_name: str) -> None:
    """Validate one in-memory project configuration against the closed schema."""
    data: dict[str, object] = {
        'allow': cfg.allow,
        'deny': cfg.deny,
        'extra_terms': cfg.extra_terms,
        'host': cfg.host,
        'hygiene_terms': cfg.hygiene_terms,
        'name': cfg.name,
        'owner': cfg.owner,
        'publication_route': cfg.publication_route,
        'repo': cfg.repo,
        'required_checks': cfg.required_checks,
        'secret_exceptions': cfg.secret_exceptions,
        'self_publication': cfg.self_publication,
        'source_tags': cfg.source_tags,
        'source_path': cfg.source_path,
        'visibility': cfg.visibility,
    }
    _require_string(data, 'name')
    validate_host_segment('owner', cfg.owner)
    validate_host_segment('repo', cfg.repo)
    _require_choice(data, 'host', _HOSTS)
    _require_choice(data, 'publication_route', PUBLICATION_ROUTES)
    _require_choice(data, 'visibility', VISIBILITIES)
    source_path = data['source_path']
    if not isinstance(source_path, Path):
        raise PubanonError('source_path must be a path')
    if not source_path.is_absolute():
        raise PubanonError('source_path must be absolute')
    if _resolve_path(source_path, 'cannot resolve project source') != source_path:
        raise PubanonError('source_path must be canonical')
    for key in (
        'allow',
        'deny',
        'secret_exceptions',
        'extra_terms',
        'hygiene_terms',
        'required_checks',
    ):
        _require_tuple_strings(data, key)
    for key in ('extra_terms', 'hygiene_terms'):
        for term in cast('tuple[str, ...]', data[key]):
            try:
                validate_term(term)
            except PubanonError:
                raise PubanonError(f'{key} must not contain blank terms') from None
    _require_boolean(data, 'self_publication')
    _require_boolean(data, 'source_tags')
    _validate_stage_repo(cfg._stage_repo, cfg.repo)
    validate_settings(cfg.settings)
    _validate_acknowledged_exposure(cfg.acknowledged_exposure)
    _validate_package(cfg.package)
    _validate_workflows(cfg.workflows)
    if _require_string(data, 'name') != expected_name:
        raise PubanonError('project configuration name does not match requested project')


def global_from_mapping(data: Mapping[str, object]) -> GlobalConfig:
    """Validate and return one complete global configuration mapping.

    Args:
        data: Parsed TOML mapping with every global configuration key.

    Returns:
        Validated global configuration.

    Raises:
        PubanonError: The mapping has missing, unknown, or invalid values.
    """
    _require_exact_keys(
        {
            key: value
            for key, value in data.items()
            if key not in {'ignored_directories', 'ignored_files'}
        },
        _GLOBAL_KEYS,
        'global configuration',
    )
    accounts = _require_table(data, 'accounts')
    _require_exact_keys(accounts, _ADAPTER_KEYS, 'account adapter')
    return GlobalConfig(
        anon_name=_require_identity(data, 'anon_name'),
        anon_email=_require_identity(data, 'anon_email'),
        accounts={'github': _require_string(accounts, 'github')},
        sensitive_terms=_require_terms(data, 'sensitive_terms', require_one=True),
        ignored_files=tuple(
            validate_ignored_file(value) for value in _require_strings(data, 'ignored_files')
        )
        if 'ignored_files' in data
        else (),
        ignored_directories=tuple(
            validate_ignored_directory(value)
            for value in _require_strings(data, 'ignored_directories')
        )
        if 'ignored_directories' in data
        else (),
        default_publication_route=_require_choice(
            data, 'default_publication_route', PUBLICATION_ROUTES
        ),
    )


def load_global(paths: Paths) -> GlobalConfig:
    """Load the global Pubanon configuration.

    Args:
        paths: Paths derived for this command.

    Returns:
        Validated global configuration.

    Raises:
        PubanonError: The global configuration is missing, unreadable,
            malformed, or invalid.
    """
    return global_from_mapping(_load_toml(paths.global_config, 'global'))


def load_project(paths: Paths, name: str) -> ProjectConfig:
    """Load one project's Pubanon configuration.

    Args:
        paths: Paths derived for this command.
        name: Registered project name.

    Returns:
        Validated project configuration.

    Raises:
        PubanonError: The project configuration is missing, unreadable,
            malformed, or invalid.
    """
    project_file = paths.config_dir / 'projects' / f'{name}.toml'
    data = _load_toml(project_file, 'project')
    unknown = sorted(data.keys() - (_PROJECT_KEYS | _PROJECT_OPTIONAL_KEYS))
    if unknown:
        raise PubanonError(f'unknown project configuration key: {", ".join(unknown)}')
    missing = sorted(_PROJECT_KEYS - data.keys())
    if missing:
        raise PubanonError(f'missing project configuration key: {", ".join(missing)}')
    repo = validate_host_segment('repo', data['repo'])
    try:
        settings = _settings_from_mapping(_require_table(data, 'settings'))
    except PubanonError as exc:
        if str(exc) != 'settings.default_branch must be main':
            raise
        raise PubanonError(
            str(exc),
            recovery=(f'set settings.default_branch = "main" in {escape(str(project_file))}'),
        ) from None
    config = ProjectConfig(
        name=_require_string(data, 'name'),
        source_path=_require_absolute_path(data, 'source_path'),
        host=_require_choice(data, 'host', _HOSTS),
        owner=validate_host_segment('owner', data['owner']),
        repo=repo,
        publication_route=_require_choice(data, 'publication_route', PUBLICATION_ROUTES),
        visibility=_require_choice(data, 'visibility', VISIBILITIES),
        allow=_require_strings(data, 'allow'),
        deny=_require_strings(data, 'deny'),
        secret_exceptions=_require_strings(data, 'secret_exceptions'),
        extra_terms=_require_terms(data, 'extra_terms', require_one=False),
        hygiene_terms=_require_terms(data, 'hygiene_terms', require_one=False),
        self_publication=_require_boolean(data, 'self_publication'),
        source_tags=(_require_boolean(data, 'source_tags') if 'source_tags' in data else False),
        required_checks=_require_strings(data, 'required_checks'),
        _stage_repo=_validate_stage_repo(data.get('stage_repo'), repo),
        settings=settings,
        acknowledged_exposure=_validate_acknowledged_exposure(
            data.get('acknowledged_exposure', '')
        ),
        package=(
            _package_from_mapping(_require_table(data, 'package')) if 'package' in data else None
        ),
        workflows=(
            _workflows_from_mapping(_require_table(data, 'workflow')) if 'workflow' in data else {}
        ),
    )
    _validate_project_config(config, name)
    return config


def save_global(paths: Paths, cfg: GlobalConfig, original: bytes | None) -> None:
    """Atomically save a complete global configuration if its snapshot is current.

    The caller must hold the setup lock from reading `original` through the
    complete save operation.

    Args:
        paths: Paths derived for this command.
        cfg: Complete global configuration to validate and write.
        original: Original file bytes, or `None` when the file was missing.

    Raises:
        PubanonError: The configuration is invalid, changed after it was read,
            or cannot be saved. A post-replace failure reports that it may have
            been saved.
    """
    try:
        accounts = dict(cfg.accounts)
    except (TypeError, ValueError) as exc:
        raise PubanonError('accounts must be a table') from exc
    config = global_from_mapping(
        {
            'anon_name': cfg.anon_name,
            'anon_email': cfg.anon_email,
            'accounts': accounts,
            'sensitive_terms': list(cfg.sensitive_terms),
            'ignored_directories': list(cfg.ignored_directories),
            'ignored_files': list(cfg.ignored_files),
            'default_publication_route': cfg.default_publication_route,
        }
    )
    content = (
        f'anon_name = {_toml_string(config.anon_name)}\n'
        f'anon_email = {_toml_string(config.anon_email)}\n'
        f'sensitive_terms = {_toml_array(config.sensitive_terms)}\n'
        f'ignored_files = {_toml_array(config.ignored_files)}\n'
        f'ignored_directories = {_toml_array(config.ignored_directories)}\n'
        f'default_publication_route = {_toml_string(config.default_publication_route)}\n'
        '[accounts]\n'
        f'github = {_toml_string(config.accounts["github"])}\n'
    )
    try:
        payload = content.encode('utf-8')
    except UnicodeError as exc:
        raise PubanonError('invalid global configuration text') from exc

    temporary: Path | None = None
    try:
        try:
            paths.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
            _filesystem.chmod(paths.config_dir, 0o700)
            temporary = _write_global_temporary(paths.config_dir, payload)
            _require_global_snapshot(paths.global_config, original)
        except (OSError, RuntimeError, UnicodeError) as exc:
            raise PubanonError('cannot save global configuration') from exc

        try:
            _filesystem.replace(temporary, paths.global_config)
        except KeyboardInterrupt as exc:
            raise PubanonError(
                'global configuration may have been saved',
                recovery='rerun pubanon setup',
            ) from exc
        except (OSError, RuntimeError) as exc:
            raise PubanonError('cannot save global configuration') from exc
    finally:
        if temporary is not None:
            with suppress(OSError, RuntimeError):
                temporary.unlink(missing_ok=True)
    try:
        _sync_directory(paths.config_dir)
    except (OSError, RuntimeError, KeyboardInterrupt) as exc:
        raise PubanonError(
            'global configuration may have been saved',
            recovery='rerun pubanon setup',
        ) from exc


def save_project(paths: Paths, cfg: ProjectConfig) -> None:
    """Save one project's Pubanon configuration with owner-only POSIX permissions.

    The caller must hold the project lock for the complete save operation.

    Args:
        paths: Paths derived for this command.
        cfg: Project configuration to write.

    Raises:
        PubanonError: The configuration is invalid or cannot be saved.
    """
    _validate_project_config(cfg, cfg.name)
    if cfg.settings is None:
        raise PubanonError('settings must be configured before saving a project')
    content = (
        f'name = {_toml_string(cfg.name)}\n'
        f'source_path = {_toml_string(str(cfg.source_path))}\n'
        f'host = {_toml_string(cfg.host)}\n'
        f'owner = {_toml_string(cfg.owner)}\n'
        f'repo = {_toml_string(cfg.repo)}\n'
        f'visibility = {_toml_string(cfg.visibility)}\n'
        f'allow = {_toml_array(cfg.allow)}\n'
        f'deny = {_toml_array(cfg.deny)}\n'
        f'secret_exceptions = {_toml_array(cfg.secret_exceptions)}\n'
        f'extra_terms = {_toml_array(cfg.extra_terms)}\n'
        f'hygiene_terms = {_toml_array(cfg.hygiene_terms)}\n'
        f'self_publication = {str(cfg.self_publication).lower()}\n'
        f'source_tags = {str(cfg.source_tags).lower()}\n'
        f'required_checks = {_toml_array(cfg.required_checks)}\n'
        f'publication_route = {_toml_string(cfg.publication_route)}\n'
    )
    if cfg._stage_repo is not None:
        content += f'stage_repo = {_toml_string(cfg._stage_repo)}\n'
    if cfg.acknowledged_exposure:
        content += f'acknowledged_exposure = {_toml_string(cfg.acknowledged_exposure)}\n'
    content += (
        '[settings]\n'
        f'description = {_toml_string(cfg.settings.description)}\n'
        f'default_branch = {_toml_string(cfg.settings.default_branch)}\n'
        f'issues = {str(cfg.settings.issues).lower()}\n'
        f'wiki = {str(cfg.settings.wiki).lower()}\n'
        f'projects = {str(cfg.settings.projects).lower()}\n'
        f'pull_requests = {str(cfg.settings.pull_requests).lower()}\n'
        f'discussions = {str(cfg.settings.discussions).lower()}\n'
    )
    if cfg.package is not None:
        content += (
            '[package]\n'
            f'index = {_toml_string(cfg.package.index)}\n'
            f'name = {_toml_string(cfg.package.name)}\n'
            f'release_checks = {_toml_array(cfg.package.release_checks)}\n'
        )
    for workflow_name, stages in cfg.workflows.items():
        content += f'[workflow.{_toml_string(workflow_name)}]\nstages = {_toml_array(stages)}\n'
    payload = content.encode('utf-8')
    projects_dir = paths.config_dir / 'projects'
    project_file = projects_dir / f'{cfg.name}.toml'
    temporary = project_file.with_suffix('.toml.tmp')
    descriptor: int | None = None
    try:
        paths.config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        _filesystem.chmod(paths.config_dir, 0o700)
        projects_dir.mkdir(mode=0o700, exist_ok=True)
        _filesystem.chmod(projects_dir, 0o700)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        _filesystem.fchmod(descriptor, 0o600)
        handle = os.fdopen(descriptor, 'wb')
        descriptor = None
        try:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        except (OSError, RuntimeError, UnicodeError):
            with suppress(OSError, RuntimeError):
                handle.close()
            raise
        handle.close()
        _filesystem.replace(temporary, project_file)
        _sync_directory(projects_dir)
    except (OSError, RuntimeError, UnicodeError) as exc:
        if descriptor is not None:
            with suppress(OSError, RuntimeError):
                os.close(descriptor)
        with suppress(OSError, RuntimeError):
            temporary.unlink(missing_ok=True)
        raise PubanonError('cannot save project configuration') from exc


def install_project(paths: Paths, project: ProjectConfig) -> None:
    """Reserve a source registration before saving its project configuration.

    Args:
        paths: Paths derived for this command.
        project: Project configuration to install while its project lock is
            held.

    Raises:
        PubanonError: The source cannot be reserved or the configuration cannot
            be saved.
    """
    _validate_project_config(project, project.name)
    canonical_source = str(_resolve_path(project.source_path, 'cannot resolve registry source'))
    create_data_dirs(paths, None)
    lock_path = paths.registry.with_name('registry.lock')
    with _exclusive_lock(lock_path, 'project registry is active'):
        _set_registry_entry(paths, canonical_source, project.name)
        save_project(paths, project)
