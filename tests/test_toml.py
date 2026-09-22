import tomllib

import pytest

from pubanon._toml import toml_array, toml_string

_SURROGATE_MAX = '\udfff'
_SURROGATE_MIN = '\ud800'


def test_toml_array_uses_scalar_safe_encoding_for_every_item() -> None:
    values = ('\x7f', '\U0001f680')

    assert tomllib.loads(f'value = {toml_array(values)}\n')['value'] == list(values)


@pytest.mark.parametrize('value', [_SURROGATE_MIN, _SURROGATE_MAX])
def test_toml_string_refuses_surrogate(value: str) -> None:
    with pytest.raises(ValueError, match='Unicode scalar'):
        toml_string(value)


@pytest.mark.parametrize('value', ['\x7f', '\U0001f680'])
def test_toml_string_round_trips_unicode_scalar(value: str) -> None:
    encoded = toml_string(value)

    assert tomllib.loads(f'value = {encoded}\n')['value'] == value
