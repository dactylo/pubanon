"""Guide global setup and check local readiness."""

from __future__ import annotations

import os
import sys
import tomllib
from dataclasses import replace

from pubanon import (
    _config,
    _filesystem,
    _run,
)
from pubanon._errors import OperationCanceled, PubanonError
from pubanon._gate import RuleClass, gate_unit
from pubanon._remote import _host
from pubanon._terminal import (
    ask,
    confirm,
    diagnostic,
    escape,
    fields,
    heading,
    next_step,
    output,
    style,
)


def _check_global_identity(paths: _config.Paths, global_config: _config.GlobalConfig) -> None:
    """Refuse colliding global identity and check its active rules."""
    machine_terms = _config.machine_private_terms(paths, os.environ)
    terms = (*global_config.sensitive_terms, *machine_terms)

    identity = f'{global_config.anon_name} <{global_config.anon_email}>'
    identity_hits = gate_unit(
        'publication identity',
        identity.encode(),
        {RuleClass.IDENTITY_CRITICAL: terms},
    )
    if identity_hits:
        raise _config.identity_refusal(
            _config.identity_rule_sources(identity_hits, global_config, None, machine_terms)
        )


def _check_global_readiness(paths: _config.Paths, global_config: _config.GlobalConfig) -> None:
    """Check the same global identity and account prerequisites for each command."""
    _check_global_identity(paths, global_config)
    _host.require_authenticated_account(
        global_config.accounts[_config.DEFAULT_HOST], authority='configuration'
    )


def _check_setup_account(expected: str, actual: str) -> None:
    """Refuse account drift with escaped expected and actual values."""
    if actual != expected:
        raise PubanonError(
            'authenticated account does not match configuration',
            expected=escape(expected),
            actual=escape(actual),
        )


def _commit_message_summary(settings: _config.CommitMessageSettings | None) -> str:
    """Describe the global commit message defaults in one summary row."""
    entries = (
        ('tagged', settings.tagged if settings is not None else None),
        ('untagged', settings.untagged if settings is not None else None),
    )
    if all(value is None for _key, value in entries):
        return 'built-in (set [commit_message] in config.toml to change)'
    parts: list[str] = []
    for key, value in entries:
        if value is None:
            parts.append(f'{key} built-in')
            continue
        first, _break, rest = value.partition('\n')
        line = escape(first.rstrip('\r'))
        more = '...' if rest.strip('\r\n') else ''
        parts.append(f'{key} "{line}"{more}')
    return ', '.join(parts)


def _read_setup(paths: _config.Paths) -> tuple[_config.GlobalConfig | None, bytes | None]:
    """Read and validate the original configuration without exposing private keys."""
    try:
        original = paths.global_config.read_bytes()
    except FileNotFoundError:
        return None, None
    except OSError as exc:
        raise _config.configuration_error(paths.global_config, 'global', exc) from exc
    try:
        data = tomllib.loads(original.decode('utf-8'))
    except (UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise _config.configuration_error(paths.global_config, 'global', exc) from exc
    try:
        return _config.global_from_mapping(data), original
    except PubanonError as error:
        # Unknown keys can themselves contain unrecognized operator text.
        message = str(error)
        fact = error.fact
        if message.startswith('unknown '):
            message = message.partition(':')[0]
            fact = None
        raise PubanonError(
            message,
            fact=fact,
            recovery='repair the global configuration and rerun pubanon setup',
        ) from None


def _setup_account() -> str:
    """Verify authentication and offer only explicitly selected terminal login."""
    while True:
        try:
            return _host.authenticated_account()
        except OperationCanceled:
            raise
        except PubanonError as error:
            if not sys.stdin.isatty():
                raise
            print(diagnostic(error))
        decision = ask('Authentication:', choices=['retry', 'login', 'cancel'])
        if decision == 'retry':
            continue
        if decision == 'cancel':
            raise OperationCanceled('Canceled.', recovery='rerun pubanon setup')
        output(
            'Handing this terminal to gh auth login. Completed authentication stays in effect '
            'if setup is canceled later.',
            kind='warning',
        )
        try:
            _host.login()
        except OperationCanceled:
            raise
        except PubanonError:
            raise PubanonError(
                'GitHub login did not complete',
                recovery=(
                    'rerun pubanon setup; any completed GitHub authentication remains in effect'
                ),
            ) from None


def _setup_expected_account(
    config: _config.GlobalConfig,
    *,
    allow_change: bool = False,
) -> _config.GlobalConfig:
    """Adopt a reviewed verified account or check an existing account."""
    actual = _setup_account()
    expected = config.accounts[_config.DEFAULT_HOST]
    if expected and not allow_change:
        _check_setup_account(expected, actual)
        return config
    print(f'{style("Verified GitHub account:")} {escape(actual)}')
    if actual == expected:
        return config
    if not confirm('Use this GitHub account?', default=True):
        raise OperationCanceled('Canceled. Nothing was saved.', recovery='rerun pubanon setup')
    return replace(config, accounts={_config.DEFAULT_HOST: actual})


def _setup_field(key: str, label: str) -> str:
    """Read one first-run identity field with local validation retries."""
    while True:
        value = ask(label)
        try:
            return _config.validate_identity(key, value)
        except PubanonError as error:
            print(diagnostic(error))


def _setup_review(
    config: _config.GlobalConfig,
    original: _config.GlobalConfig | None,
) -> None:
    """Show the exact configuration summary needed for a save decision."""
    identity_changed = original is not None and (
        original.anon_name,
        original.anon_email,
    ) != (config.anon_name, config.anon_email)
    if identity_changed and original is not None:
        heading('Publication identity change:')
        print(
            f'  {style("Current:")} {escape(original.anon_name)} <{escape(original.anon_email)}>'
        )
        print(f'  {style("New:")}     {escape(config.anon_name)} <{escape(config.anon_email)}>')
        print()
        output('This affects every project using this configuration.', kind='warning')
        print('Existing publication commits keep the old identity. Their identity audit will')
        print('fail until you restore the matching identity. No history will be rewritten.')
    heading('Configuration to save', separate=identity_changed)
    fields(
        [
            ('Identity', f'{escape(config.anon_name)} <{escape(config.anon_email)}>'),
            ('GitHub account', escape(config.accounts[_config.DEFAULT_HOST])),
            ('Default route', f'{escape(config.default_publication_route)} (new projects only)'),
            ('Commit messages', _commit_message_summary(config.commit_message)),
            (
                'Excluded directories',
                ', '.join(f'{escape(directory)}/' for directory in config.ignored_directories)
                or 'none',
            ),
            ('Excluded files', ', '.join(escape(file) for file in config.ignored_files) or 'none'),
            ('Sensitive terms', str(len(config.sensitive_terms))),
        ]
    )
    for entry_id, term in enumerate(config.sensitive_terms, start=1):
        print(f'  {entry_id}. {escape(term)}')


def _setup_route(default: str) -> str:
    """Read the default route for future projects."""
    print('Staged: try snapshots privately before final publication.')
    print('Direct: upload straight to the final repository.')
    while True:
        route = ask('Default publication route (staged/direct)', default=default).casefold()
        if route in _config.PUBLICATION_ROUTES:
            return route
        output('Choose staged or direct.', kind='warning')


def _setup_term_id(terms: dict[int, str], *, prompt: str = 'Term number') -> int:
    """Read one listed entry number."""
    while True:
        raw = ask(prompt)
        try:
            entry_id = int(raw)
        except ValueError:
            entry_id = 0
        if entry_id in terms:
            return entry_id
        output('Choose one listed number.', kind='warning')


def _setup_edit_directories(config: _config.GlobalConfig) -> _config.GlobalConfig:
    """Edit literal directory exclusions shared by existing and future projects."""
    directories = list(config.ignored_directories)
    print('Enter directories relative to each source root, such as docs/private.')
    print('These exclusions override project includes; text references are still checked.')
    while True:
        heading('Excluded directories (all projects):')
        for entry_id, directory in enumerate(directories, 1):
            print(f'  {entry_id}. {escape(directory)}/')
        if not directories:
            print('  None.')
        action = ask('Directories:', choices=['add', 'remove', 'done'])
        if action == 'done':
            return replace(config, ignored_directories=tuple(directories))
        if action == 'remove':
            if directories:
                entry_id = _setup_term_id(
                    dict(enumerate(directories, 1)), prompt='Directory number'
                )
                directories.pop(entry_id - 1)
            continue
        try:
            directory = _config.validate_ignored_directory(ask('Directory'))
        except PubanonError as error:
            print(diagnostic(error))
            continue
        if directory not in directories:
            directories.append(directory)


def _setup_edit_files(config: _config.GlobalConfig) -> _config.GlobalConfig:
    """Edit literal file exclusions shared by existing and future projects."""
    files = list(config.ignored_files)
    print('Enter files as a basename at any depth, or a path relative to the source root.')
    print('These exclusions override project includes; text references are still checked.')
    while True:
        heading('Excluded files (all projects):')
        for entry_id, file in enumerate(files, 1):
            print(f'  {entry_id}. {escape(file)}')
        if not files:
            print('  None.')
        action = ask('Files:', choices=['add', 'remove', 'done'])
        if action == 'done':
            return replace(config, ignored_files=tuple(files))
        if action == 'remove':
            if files:
                entry_id = _setup_term_id(dict(enumerate(files, 1)), prompt='File number')
                files.pop(entry_id - 1)
            continue
        try:
            file = _config.validate_ignored_file(ask('File'))
        except PubanonError as error:
            print(diagnostic(error))
            continue
        if file not in files:
            files.append(file)


def _setup_terms_from_scratch() -> tuple[str, ...]:
    """Read first-run sensitive terms until blank follows one valid term."""
    terms: list[str] = []
    while True:
        value = ask('Sensitive term', default='')
        if not value and terms:
            return tuple(terms)
        try:
            terms.append(_config.validate_term(value))
        except PubanonError as error:
            print(diagnostic(error))


def _setup_validate(config: _config.GlobalConfig) -> _config.GlobalConfig:
    """Apply the existing global shape rules to one in-memory setup snapshot."""
    return _config.global_from_mapping(_config.global_mapping(config))


def _setup_identity(
    config: _config.GlobalConfig,
    fields: tuple[str, ...],
    *,
    preserve_blank: bool = False,
) -> _config.GlobalConfig:
    """Collect selected visible publication identity fields with validation retries."""
    for field in fields:
        label = 'Publication name' if field == 'anon_name' else 'Publication email'
        while True:
            current = getattr(config, field)
            value = ask(label, default=current if preserve_blank else None)
            candidate = replace(config, **{field: value})
            try:
                config = _setup_validate(candidate)
            except PubanonError as error:
                print(diagnostic(error))
            else:
                break
    return config


def _setup_term(config: _config.GlobalConfig) -> str:
    """Read and validate one sensitive term."""
    while True:
        value = ask('Sensitive term', default='')
        if not value:
            output('Enter a sensitive term.', kind='warning')
            continue
        if not value.strip():
            output('A sensitive term cannot be blank or all whitespace.', kind='warning')
            continue
        if value != value.strip():
            output('This term has surrounding whitespace.', kind='warning')
            choice = ask('Keep it exactly?', choices=['keep', 're-enter'])
            if choice == 're-enter':
                continue
        try:
            _setup_validate(replace(config, sensitive_terms=(value,)))
        except PubanonError as error:
            print(diagnostic(error))
            continue
        return value


def _setup_update_term(
    config: _config.GlobalConfig,
    terms: dict[int, str],
    entry_id: int,
) -> dict[int, str] | None:
    """Return entries with one validated term added or replaced."""
    values = tuple(terms.values())
    value = _setup_term(replace(config, sensitive_terms=values))
    candidate = terms.copy()
    candidate[entry_id] = value
    try:
        _setup_validate(replace(config, sensitive_terms=tuple(candidate.values())))
    except PubanonError as error:
        print(diagnostic(error))
        return None
    return candidate


def _setup_edit_terms(config: _config.GlobalConfig) -> _config.GlobalConfig:
    """Display and edit sensitive terms."""
    terms = dict(enumerate(config.sensitive_terms, start=1))
    next_id = len(terms) + 1
    while True:
        heading(f'Sensitive terms: {len(terms)}')
        for entry_id, term in terms.items():
            print(f'  {entry_id}. {escape(term)}')
        action = ask('Terms:', choices=['add', 'replace', 're[m]ove', 'done'])
        if action == 'done':
            return replace(config, sensitive_terms=tuple(terms.values()))
        if action == 'remove':
            if len(terms) == 1:
                output('At least one sensitive term is required.', kind='warning')
                continue
            entry_id = _setup_term_id(terms)
            del terms[entry_id]
            continue
        entry_id = next_id if action == 'add' else _setup_term_id(terms)
        updated = _setup_update_term(config, terms, entry_id)
        if updated is not None:
            terms = updated
            if action == 'add':
                next_id += 1


def _setup_edit(config: _config.GlobalConfig) -> _config.GlobalConfig:
    """Edit one selected setup section while preserving the other values."""
    action = ask(
        'Edit:', choices=['identity', 'terms', 'account', 'route', 'directories', 'files', 'back']
    )
    if action in {'files', 'directories'}:
        editor = {'files': _setup_edit_files, 'directories': _setup_edit_directories}[action]
        updated = editor(config)
        if updated != config:
            output(f'Changing ignored {action} invalidates retained snapshots.', kind='warning')
        return updated
    if action == 'identity':
        return _setup_identity(config, ('anon_name', 'anon_email'), preserve_blank=True)
    if action == 'terms':
        updated = _setup_edit_terms(config)
        if updated.sensitive_terms != config.sensitive_terms:
            output('Changing sensitive terms invalidates retained snapshots.', kind='warning')
            print('Preparation and disclosure checks will run again.')
        return updated
    if action == 'account':
        updated = _setup_expected_account(config, allow_change=True)
        if updated.accounts != config.accounts:
            output('Changing the expected account invalidates retained snapshots.', kind='warning')
            print('Authentication and destination checks will run again.')
        return updated
    if action == 'route':
        return replace(
            config, default_publication_route=_setup_route(config.default_publication_route)
        )
    return config


def _setup_verify_saved(paths: _config.Paths, config: _config.GlobalConfig) -> None:
    """Check saved configuration while preserving completion facts on interruption."""
    try:
        _check_global_identity(paths, config)
        _check_setup_account(config.accounts[_config.DEFAULT_HOST], _host.authenticated_account())
    except OperationCanceled as error:
        saved = 'configuration was saved; the saved configuration was retained'
        error.fact = f'{error.fact}; {saved}' if error.fact else saved
        raise
    except KeyboardInterrupt:
        raise OperationCanceled(
            'Canceled.',
            fact='configuration was saved; the saved configuration was retained',
            recovery='rerun pubanon setup',
        ) from None
    except (PubanonError, EOFError) as exc:
        raise PubanonError(
            'configuration was saved, but readiness could not be verified',
            fact=exc.fact if isinstance(exc, PubanonError) else None,
            expected=exc.expected if isinstance(exc, PubanonError) else None,
            actual=exc.actual if isinstance(exc, PubanonError) else None,
            recovery='rerun pubanon setup; the saved configuration was retained',
        ) from None


def _setup_save_loop(
    paths: _config.Paths,
    initial: _config.GlobalConfig,
    config: _config.GlobalConfig,
    original: bytes | None,
) -> None:
    """Review, edit, and save one in-memory setup snapshot."""
    account_verified = False
    while True:
        if not config.accounts[_config.DEFAULT_HOST]:
            config = _setup_expected_account(config)
            account_verified = True
        try:
            config = _setup_validate(config)
            _check_global_identity(paths, config)
        except PubanonError as error:
            print(diagnostic(error))
            output(
                'Edit the implicated identity or sensitive terms before saving.', kind='warning'
            )
            config = _setup_edit(config)
            continue
        saved_original = None if original is None else initial
        _setup_review(config, saved_original)
        action = ask('Save this configuration?', choices=['save', 'edit', 'cancel'])
        if action == 'cancel':
            raise OperationCanceled('Canceled. Nothing was saved.', recovery='rerun pubanon setup')
        if action == 'edit':
            config = _setup_edit(config)
            continue
        if not account_verified:
            actual = _setup_account()
            try:
                _check_setup_account(config.accounts[_config.DEFAULT_HOST], actual)
            except PubanonError as error:
                print(diagnostic(error))
                output('Edit the expected account before saving.', kind='warning')
                continue
            account_verified = True
        _config.save_global(paths, config, original)
        _setup_verify_saved(paths, config)
        output('Saved your configuration.', kind='success')
        return


def _setup_locked(paths: _config.Paths, *, edit: bool = False) -> None:
    """Complete one setup while holding the configuration-directory lock."""
    config, original = _read_setup(paths)
    if config is not None:
        _filesystem.chmod(paths.global_config, 0o600)
        if not edit and config.accounts[_config.DEFAULT_HOST]:
            _check_global_identity(paths, config)
            _setup_expected_account(config)
            print(
                f'{style("Publication identity:")} {escape(config.anon_name)} '
                f'<{escape(config.anon_email)}>'
            )
            print('To correct it, run `pubanon setup --edit`.')
            return
    if not sys.stdin.isatty():
        raise PubanonError(
            'setup requires an interactive terminal for missing or edited values',
            recovery='rerun pubanon setup in an interactive terminal',
        )
    if config is None:
        heading('Publication identity')
        print('Choose the author name and email for your publication commits.')
        print('These values will be visible in the published Git history.')
        name = _setup_field('anon_name', 'Publication name')
        email = _setup_field('anon_email', 'Publication email')
        heading('Sensitive terms', separate=True)
        print(
            'Enter at least one sensitive term to block from publication; '
            'then leave blank to finish:'
        )
        terms = _setup_terms_from_scratch()
        route = _setup_route('staged')
        config = _config.GlobalConfig(name, email, {_config.DEFAULT_HOST: ''}, terms, route)
    initial = config
    _setup_save_loop(paths, initial, config, original)


def doctor() -> None:
    """Validate configuration, publication identity, and authenticated account."""
    paths = _config.derive_paths(os.environ)
    _check_global_readiness(paths, _config.load_global(paths))
    output('Your configuration and GitHub login are ready.', kind='success')


def setup(*, edit: bool = False) -> None:
    """Guide first-run global configuration or verify an existing completed setup."""
    paths = _config.derive_paths(os.environ)
    try:
        _run.check_floors()
        with _config.setup_lock(paths):
            _setup_locked(paths, edit=edit)
    except OperationCanceled as error:
        if error.recovery is None or error.recovery == 'rerun pubanon setup':
            error.recovery = (
                'rerun pubanon setup; any completed GitHub authentication remains in effect'
            )
        raise
    except KeyboardInterrupt:
        raise OperationCanceled(
            'Canceled.',
            recovery='rerun pubanon setup; any completed GitHub authentication remains in effect',
        ) from None
    except EOFError:
        raise PubanonError(
            'setup canceled',
            recovery='rerun pubanon setup; any completed GitHub authentication remains in effect',
        ) from None
    # _read_setup can propagate RecursionError from tomllib.loads.
    except (OSError, RuntimeError):
        raise PubanonError(
            'cannot complete global setup', recovery='rerun pubanon setup'
        ) from None
    next_step('run pubanon init in your source repository')
