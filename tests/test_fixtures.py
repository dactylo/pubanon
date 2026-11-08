from pathlib import Path

import pytest

from pubanon._errors import PubanonError
from pubanon._remote import _host
from tests._cli import read_gh_calls


def test_fake_gh_autouse_records_and_refuses_unexpected_call(tmp_path: Path) -> None:
    args = ['api', 'unexpected']

    with pytest.raises(PubanonError) as exc_info:
        _host._gh(args)

    assert str(exc_info.value) == 'unexpected fake gh invocation: ["api", "unexpected"]'
    assert read_gh_calls(tmp_path / 'fake-gh-calls.jsonl') == [{'args': args, 'stdin': ''}]
