import sys
from collections.abc import Callable, Sequence

import pytest

_Script = Callable[[Sequence[tuple[str, str]]], None]


def test_answers_reject_an_unexpected_prompt(answers: _Script) -> None:
    answers([('Expected: ', 'value')])
    with pytest.raises(AssertionError, match='expected prompt'):
        input('Other: ')
    # Consume the scripted reply so teardown's consumption assertion passes.
    assert input('Expected: ') == 'value'


def test_answers_reject_more_prompts_than_replies(answers: _Script) -> None:
    answers([('Only: ', 'value')])
    assert input('Only: ') == 'value'
    with pytest.raises(AssertionError, match='unexpected prompt'):
        input('Only: ')


def test_answers_return_scripted_replies_in_order(answers: _Script) -> None:
    answers([('First: ', 'one'), ('Second: ', 'two')])
    assert input('First: ') == 'one'
    assert input('Second: ') == 'two'


def test_stdin_reports_a_terminal_by_default() -> None:
    assert sys.stdin.isatty() is True


def test_unscripted_input_fails_immediately() -> None:
    with pytest.raises(pytest.fail.Exception, match=r'unscripted prompt: .Question: .'):
        input('Question: ')
