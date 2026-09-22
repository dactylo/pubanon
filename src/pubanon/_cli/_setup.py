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
from pubanon._errors import OperationCancelled, PubanonError
from pubanon._gate import RuleClass, gate_unit
from pubanon._remote import _host
from pubanon._terminal import (
    ask,
    confirm,
    diagnostic,
    escape,
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
        global_config.accounts['github'], authority='configuration'
    )


def _check_setup_account(expected: str, actual: str) -> None:
    """Refuse account drift with escaped expected and actual values."""
    if actual != expected:
        raise PubanonError(
            'authenticated account does not match configuration',
            expected=escape(expected),
            actual=escape(actual),
        )


def _read_setup(paths: _config.Paths) -> tuple[_config.GlobalConfig | None, bytes | None]:
    """Read and validate the original configuration without exposing private keys."""
    try:
        original = paths.global_config.read_bytes()
    except FileNotFoundError:
        return None, None
    except (OSError, RuntimeError) as exc:
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
        if message.startswith('unknown '):
            message = message.partition(':')[0]
        raise PubanonError(
            message, recovery='repair the global configuration and rerun pubanon setup'
        ) from None


def _setup_account() -> str:
    """Verify authentication and offer only explicitly selected terminal login."""
    while True:
        try:
            return _host.authenticated_account()
        except OperationCancelled:
            raise
        except PubanonError as error:
            if not sys.stdin.isatty():
                raise
            print(diagnostic(error))
        decision = ask('Authentication:', choices=['retry', 'login', 'cancel'])
        if decision == 'retry':
            continue
        if decision == 'cancel':
            raise OperationCancelled('setup cancelled', recovery='rerun pubanon setup')
        output(
            'Handing this terminal to gh auth login. Completed authentication stays in effect '
            'if setup is cancelled later.',
            kind='warning',
        )
        try:
            _host.login()
        except OperationCancelled:
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
    expected = config.accounts['github']
    if expected and not allow_change:
        _check_setup_account(expected, actual)
        return config
    print(f'{style("Verified GitHub account:")} {escape(actual)}')
    if actual == expected:
        return config
    if not confirm('Use this GitHub account?', default=True):
        raise PubanonError('setup cancelled', recovery='rerun pubanon setup')
    return replace(config, accounts={'github': actual})


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
    print(
        f'{style("Publication identity:")} {escape(config.anon_name)} '
        f'<{escape(config.anon_email)}>'
    )
    print(f'{style("GitHub account:")} {escape(config.accounts["github"])}')
    print(style('Sensitive terms:'))
    for entry_id, term in enumerate(config.sensitive_terms, start=1):
        print(f'  {entry_id}. {escape(term)}')


def _setup_term_id(terms: dict[int, str]) -> int:
    """Read one stable sensitive-term entry number."""
    while True:
        raw = ask('Term number')
        try:
            entry_id = int(raw)
        except ValueError:
            entry_id = 0
        if entry_id in terms:
            return entry_id
        output('Choose one listed term number.', kind='warning')


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
    return _config.global_from_mapping(
        {
            'anon_name': config.anon_name,
            'anon_email': config.anon_email,
            'sensitive_terms': list(config.sensitive_terms),
            'accounts': dict(config.accounts),
        }
    )


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
    action = ask('Edit:', choices=['identity', 'terms', 'account', 'back'])
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
    return config


def _setup_verify_saved(paths: _config.Paths, config: _config.GlobalConfig) -> None:
    """Check saved configuration while preserving completion facts on interruption."""
    try:
        _check_global_identity(paths, config)
        _check_setup_account(config.accounts['github'], _host.authenticated_account())
    except OperationCancelled as error:
        saved = 'configuration was saved; the saved configuration was retained'
        error.fact = f'{error.fact}; {saved}' if error.fact else saved
        raise
    except KeyboardInterrupt:
        raise OperationCancelled(
            'setup cancelled',
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
        if not config.accounts['github']:
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
            raise OperationCancelled(
                'setup cancelled', fact='nothing was saved', recovery='rerun pubanon setup'
            )
        if action == 'edit':
            config = _setup_edit(config)
            continue
        if not account_verified:
            actual = _setup_account()
            try:
                _check_setup_account(config.accounts['github'], actual)
            except PubanonError as error:
                print(diagnostic(error))
                output('Edit the expected account before saving.', kind='warning')
                continue
            account_verified = True
        _config.save_global(paths, config, original)
        _setup_verify_saved(paths, config)
        output('Saved.', kind='success')
        return


def _setup_locked(paths: _config.Paths, *, edit: bool = False) -> None:
    """Complete one setup while holding the configuration-directory lock."""
    config, original = _read_setup(paths)
    if config is not None:
        _filesystem.chmod(paths.global_config, 0o600)
        if not edit:
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
        config = _config.GlobalConfig(name, email, {'github': ''}, terms)
    initial = config
    _setup_save_loop(paths, initial, config, original)


def doctor() -> None:
    """Validate configuration, publication identity, and authenticated account."""
    paths = _config.derive_paths(os.environ)
    _check_global_readiness(paths, _config.load_global(paths))
    output(
        'Pubanon configuration and GitHub authentication are ready.',
        kind='success',
    )


def setup(*, edit: bool = False) -> None:
    """Guide first-run global configuration or verify an existing completed setup."""
    paths = _config.derive_paths(os.environ)
    try:
        _run.check_floors()
        with _config.setup_lock(paths):
            if edit:
                _setup_locked(paths, edit=True)
            else:
                _setup_locked(paths)
    except OperationCancelled as error:
        if error.recovery is None or error.recovery == 'rerun pubanon setup':
            error.recovery = (
                'rerun pubanon setup; any completed GitHub authentication remains in effect'
            )
        raise
    except PubanonError as error:
        if str(error) == 'setup cancelled':
            error.recovery = (
                'rerun pubanon setup; any completed GitHub authentication remains in effect'
            )
        raise
    except KeyboardInterrupt:
        raise OperationCancelled(
            'setup cancelled',
            recovery='rerun pubanon setup; any completed GitHub authentication remains in effect',
        ) from None
    except EOFError:
        raise PubanonError(
            'setup cancelled',
            recovery='rerun pubanon setup; any completed GitHub authentication remains in effect',
        ) from None
    except (OSError, RuntimeError):
        raise PubanonError(
            'cannot complete global setup', recovery='rerun pubanon setup'
        ) from None
    output(
        'Global configuration, runtime requirements, and GitHub account are ready.',
        kind='success',
    )
    output('Run initialization from your source repository.')
    next_step('pubanon init')
