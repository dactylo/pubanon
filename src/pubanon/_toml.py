"""Encode the TOML string subset shared by Pubanon records."""

import json
from collections.abc import Iterable

_SURROGATE_MAX = 0xDFFF
_SURROGATE_MIN = 0xD800


def toml_string(value: str) -> str:
    """Encode one Unicode scalar string as TOML basic-string syntax.

    Args:
        value: String to encode.

    Returns:
        TOML basic-string syntax.

    Raises:
        ValueError: The value contains a surrogate code point.
    """
    if any(_SURROGATE_MIN <= ord(character) <= _SURROGATE_MAX for character in value):
        raise ValueError('value is not a Unicode scalar string')
    return json.dumps(value, ensure_ascii=False).replace('\x7f', r'\u007f')


def toml_array(values: Iterable[str]) -> str:
    """Encode strings as one TOML array.

    Args:
        values: Unicode scalar strings to encode.

    Returns:
        TOML array syntax containing the encoded strings.

    Raises:
        ValueError: A value contains a surrogate code point.
    """
    return '[' + ', '.join(toml_string(value) for value in values) + ']'
