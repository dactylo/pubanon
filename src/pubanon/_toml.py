"""Encode the TOML string subset shared by pubanon records."""

import json
import re
from collections.abc import Iterable

_DECODE_POSITION = re.compile(r'\(at line (\d+), column (\d+)\)$')
_SURROGATE_MAX = 0xDFFF
_SURROGATE_MIN = 0xD800


def decode_error_position(error: ValueError) -> str | None:
    """Return a TOML decoding error's position as `line L, column C`.

    Python 3.13 exposes the position only in the message's standard suffix.
    The rest of the parser text can quote private content and is never
    returned.
    """
    if position := _DECODE_POSITION.search(str(error)):
        return f'line {position[1]}, column {position[2]}'
    return None


def toml_multiline_string(value: str) -> str:
    """Encode one Unicode scalar string as a TOML multi-line basic string.

    Args:
        value: String to encode.

    Returns:
        TOML syntax whose first line break follows the opening delimiter.

    Raises:
        ValueError: The value contains a surrogate code point.
    """
    if any(_SURROGATE_MIN <= ord(character) <= _SURROGATE_MAX for character in value):
        raise ValueError('value is not a Unicode scalar string')
    parts: list[str] = []
    for character in value:
        if character == '\\':
            parts.append('\\\\')
        elif character == '"':
            parts.append('\\"')
        elif character in '\n\t':
            parts.append(character)
        elif character < ' ' or character == '\x7f':
            parts.append(f'\\u{ord(character):04x}')
        else:
            parts.append(character)
    return '"""\n' + ''.join(parts) + '"""'


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
