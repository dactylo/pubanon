import json
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from pubanon import (
    _cli,
)
from pubanon._errors import PubanonError
from pubanon._remote import _host
from pubanon._terminal import escape
from tests._setup import configure


def test_doctor_checks_floors_schema_and_expected_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _paths, _global_config, _project = configure(tmp_path, monkeypatch)
    floors: list[None] = []

    def _check_floors() -> None:
        floors.append(None)

    monkeypatch.setattr('pubanon._run.check_floors', _check_floors)

    requirements: list[tuple[str, str]] = []

    def _require_account(expected: str, *, authority: str) -> str:
        requirements.append((expected, authority))
        return expected

    monkeypatch.setattr(_host, 'require_authenticated_account', _require_account)

    assert _cli.main(['doctor']) == 0
    assert floors == [None]
    assert requirements == [('anonymous-account', 'configuration')]
    assert 'Your configuration and GitHub login are ready.' in capsys.readouterr().out


def test_doctor_missing_configuration_reports_setup_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _, _ = configure(tmp_path, monkeypatch)
    paths.global_config.unlink()
    assert _cli.main(['doctor']) == 1
    output = capsys.readouterr()
    assert 'missing' in output.err
    assert escape(str(paths.global_config)) in output.err


def test_doctor_refuses_identity_containing_machine_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _global_config, _project = configure(tmp_path, monkeypatch)
    home = tmp_path / 'home'
    paths.global_config.write_text(
        f'anon_name = {json.dumps(f"Publisher {home}")}\n'
        'anon_email = "anonymous@example.invalid"\n'
        'sensitive_terms = ["Private Person"]\n'
        'default_publication_route = "staged"\n'
        '[accounts]\n'
        'github = "anonymous-account"\n'
    )
    monkeypatch.setattr('pubanon._run.check_floors', lambda: None)

    assert _cli.main(['doctor']) == 1

    err = capsys.readouterr().err
    assert 'rule source: private path' in err
    assert 'does not contain a private path' in err
    assert str(home) not in err


def test_doctor_refuses_identity_matching_sensitive_term(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _global_config, _project = configure(tmp_path, monkeypatch)
    paths.global_config.write_text(
        'anon_name = "Private Person"\n'
        'anon_email = "anonymous@example.invalid"\n'
        'sensitive_terms = ["Private Person"]\n'
        'default_publication_route = "staged"\n'
        '[accounts]\n'
        'github = "anonymous-account"\n'
    )
    monkeypatch.setattr('pubanon._run.check_floors', lambda: None)

    def _unexpected(*_args: object, **_kwargs: object) -> str:
        pytest.fail('doctor contacted the host with a refused identity')

    monkeypatch.setattr(_host, 'require_authenticated_account', _unexpected)

    assert _cli.main(['doctor']) == 1

    err = capsys.readouterr().err
    assert 'publication identity matches an active disclosure rule' in err
    assert 'rule source: sensitive_terms' in err
    assert 'remove the value from sensitive_terms' in err
    assert 'Private Person' not in err


def test_doctor_reports_known_machine_paths_in_host_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _global_config, _project = configure(tmp_path, monkeypatch)
    monkeypatch.setattr('pubanon._run.check_floors', lambda: None)
    canaries = (str(tmp_path / 'home'), str(paths.config_dir), str(paths.data_dir))

    def _fail_account(*, timeout: float | None = None) -> str:
        assert timeout is None
        raise PubanonError(' | '.join(canaries))

    monkeypatch.setattr(_host, '_authenticated_account', _fail_account)

    assert _cli.main(['doctor']) == 1
    error = capsys.readouterr().err
    assert all(canary in error for canary in canaries)
    assert f'{canaries[0]}' in error


def test_doctor_reports_unexpected_authenticated_account(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answers: Callable[[Sequence[tuple[str, str]]], None],
) -> None:
    _paths, _global_config, _project = configure(tmp_path, monkeypatch)

    def _check_floors() -> None:
        pass

    monkeypatch.setattr('pubanon._run.check_floors', _check_floors)

    def _account(*, timeout: float | None = None) -> str:
        assert timeout is None
        return 'real-login-canary'

    monkeypatch.setattr(_host, '_authenticated_account', _account)

    answers([])
    assert _cli.main(['doctor']) == 1

    error = capsys.readouterr().err
    assert 'anonymous-account' in error
    assert 'real-login-canary' in error
