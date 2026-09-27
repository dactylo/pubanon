from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from pubanon._cli import _init, _init_draft
from pubanon._snapshots import _source

_Script = Callable[[Sequence[tuple[str, str]]], None]


def test_draft_description_escapes_saved_default(
    tmp_path: Path,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    description = 'Example\x1b[31m'
    draft = _init_draft.Draft(tmp_path, 'commit', self_publication=False, description=description)
    answers([('Description: [k]eep / [r]eplace / [c]lear [keep]: ', '')])

    assert _init._draft_description(draft) == description
    output = capsys.readouterr().out
    assert r'Current description: Example\x1b[31m' in output
    assert '\x1b' not in output


@pytest.mark.parametrize('saved', [None, '', 'Saved description'])
@pytest.mark.parametrize('action', ['', 'r', 'c'])
def test_draft_description_suggests_metadata_without_overriding_saved(
    tmp_path: Path,
    answers: _Script,
    saved: str | None,
    action: str,
) -> None:
    (tmp_path / 'pyproject.toml').write_text('[project]\ndescription = "Suggested description"\n')
    draft = _init_draft.Draft(tmp_path, 'commit', self_publication=False, description=saved)
    suggested = 'Suggested description' if saved is None else saved
    if suggested:
        replies = [('Description: [k]eep / [r]eplace / [c]lear [keep]: ', action)]
        if action == 'r':
            replies.append(('Repository description (blank for none): ', 'Replacement'))
        expected = {'': suggested, 'r': 'Replacement', 'c': ''}[action]
    else:
        replies = [('Repository description (blank for none): ', '')]
        expected = ''
    answers(replies)
    assert _init._draft_description(draft) == expected


@pytest.mark.parametrize(
    'metadata', [None, '[tool.example]\nx = 1', '[project]\ndescription = 42', 'invalid [']
)
def test_draft_description_without_metadata_allows_blank(
    tmp_path: Path,
    answers: _Script,
    metadata: str | None,
) -> None:
    if metadata is not None:
        (tmp_path / 'pyproject.toml').write_text(metadata)
    answers([('Repository description (blank for none): ', '')])
    assert (
        _init._draft_description(_init_draft.Draft(tmp_path, 'commit', self_publication=False))
        == ''
    )


def test_required_check_default_round_trips_names_containing_commas(answers: _Script) -> None:
    current = ('quality', 'tests (ubuntu-24.04, 3.13)')
    answers(
        [
            (
                (
                    'Required CI checks (comma separated, blank for none) '
                    '[quality,"tests (ubuntu-24.04, 3.13)"]: '
                ),
                '',
            ),
        ]
    )
    assert _init._required_checks_prompt(default=current) == current


@pytest.mark.parametrize(
    ('reply', 'expected'),
    [
        ('quality, "tests (ubuntu-24.04, 3.13)"', ('quality', 'tests (ubuntu-24.04, 3.13)')),
        ('"Check ""quoted"", with comma"', ('Check "quoted", with comma',)),
        (' Test, Lint, none, ', ('Test', 'Lint')),
    ],
)
def test_required_check_names_preserve_quoted_commas(
    answers: _Script,
    reply: str,
    expected: tuple[str, ...],
) -> None:
    answers([('Required CI checks (comma separated, blank for none): ', reply)])
    assert _init._required_checks_prompt() == expected


def test_required_check_prompt_retries_malformed_quoting(answers: _Script) -> None:
    prompt = 'Required CI checks (comma separated, blank for none): '
    answers([(prompt, '"unfinished'), (prompt, '"tests (linux, 3.13)"')])
    assert _init._required_checks_prompt() == ('tests (linux, 3.13)',)


@pytest.mark.parametrize(
    ('numbers', 'expected'), [('1', ('quality',)), ('1 2 3', ('quality', 'optional', 'Upload'))]
)
def test_required_checks_needing_review_require_explicit_selection(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    numbers: str,
    expected: tuple[str, ...],
) -> None:
    snapshot = (
        _source.Entry(
            '.github/workflows/ci.yml',
            0o100644,
            'blob',
            b'on: push\njobs:\n  quality: {}\n  optional:\n    if: inputs.extra\n',
        ),
        _source.Entry(
            '.github/workflows/release.yml',
            0o100644,
            'blob',
            b'on: workflow_dispatch\njobs:\n  upload:\n    name: Upload\n',
        ),
    )
    answers = iter(['a', 's', numbers])

    def _reply(_prompt: str) -> str:
        return next(answers)

    monkeypatch.setattr('builtins.input', _reply)

    assert _init._required_checks_prompt(snapshot=snapshot) == expected
    output = capsys.readouterr().out
    assert '.github/workflows/ci.yml' in output
    assert 'condition' in output
    assert '.github/workflows/release.yml' in output
    assert 'manually' in output


@pytest.mark.parametrize(
    ('responses', 'expected'),
    [
        (['a'], ('quality', 'tests (linux, 3.13)')),
        (['s', '0', '2 1'], ('tests (linux, 3.13)', 'quality')),
        (['s', 'back', 'n'], ()),
        (['s', 'back', 's', '2'], ('tests (linux, 3.13)',)),
        (['n'], ()),
        (['m', '"external, test"'], ('external, test',)),
        (['k'], ('saved, custom',)),
    ],
)
def test_workflow_check_selection_supports_all_subset_none_manual_and_keep(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[str],
    expected: tuple[str, ...],
) -> None:
    snapshot = (
        _source.Entry(
            '.github/workflows/ci.yml',
            0o100644,
            'blob',
            b'on: push\njobs:\n  quality:\n    runs-on: linux\n'
            b'  test:\n    name: tests (linux, 3.13)\n',
        ),
    )
    replies = iter(responses)

    def _answer(_prompt: str) -> str:
        return next(replies)

    monkeypatch.setattr('builtins.input', _answer)
    assert _init._required_checks_prompt(default=('saved, custom',), snapshot=snapshot) == expected
    assert next(replies, None) is None
