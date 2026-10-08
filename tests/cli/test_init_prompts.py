from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from pubanon._cli import _init, _init_draft
from pubanon._snapshots import _source

_Script = Callable[[Sequence[tuple[str, str]]], None]
_CONDITIONAL = """\
on: push
jobs:
  quality:
    if: github.event_name != 'push'
    runs-on: linux
  package:
    runs-on: linux
"""
_DYNAMIC = """\
on: push
jobs:
  lint:
    runs-on: linux
  test:
    strategy:
      matrix:
        os: ${{ fromJSON(vars.OSES) }}
    runs-on: linux
"""
_MENU = 'Require checks: [a]ll listed / [s]elect / [n]one / [m]anual: '
_MENU_ALL = 'Require checks: [a]ll listed / [s]elect / [n]one / [m]anual [all listed]: '
_MENU_KEEP = (
    'Require checks: [a]ll listed / [s]elect / [n]one / [m]anual / [k]eep current [keep current]: '
)


def _workflows(**files: str) -> _source.Snapshot:
    return tuple(
        _source.Entry(f'.github/workflows/{name}.yml', 0o100644, 'blob', data.encode())
        for name, data in files.items()
    )


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


def test_required_checks_default_to_all_listed_for_a_connected_workflow(
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers([(_MENU_ALL, '')])

    assert _init._required_checks_prompt(
        snapshot=_workflows(ci=_CONDITIONAL), connected='ci.yml'
    ) == ('quality', 'package')
    out = capsys.readouterr().out
    assert 'GitHub checks found in ci.yml' in out
    assert 'pubanon accepts a run only if it executed every required job.' in out


def test_required_checks_for_a_connected_workflow_omit_other_workflows(
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    release = 'on: release\njobs:\n  publish:\n    runs-on: linux\n'
    answers([(_MENU_ALL, '')])

    assert _init._required_checks_prompt(
        snapshot=_workflows(ci=_CONDITIONAL, release=release), connected='ci.yml'
    ) == ('quality', 'package')
    assert 'publish' not in capsys.readouterr().out


def test_required_checks_keep_current_requirements_by_default(answers: _Script) -> None:
    answers([(_MENU_KEEP, '')])

    assert _init._required_checks_prompt(
        default=('package',), snapshot=_workflows(ci=_CONDITIONAL), connected='ci.yml'
    ) == ('package',)


@pytest.mark.parametrize(
    ('numbers', 'expected'), [('1', ('quality',)), ('1 2 3', ('quality', 'optional', 'Upload'))]
)
def test_required_checks_needing_review_offer_explicit_selection(
    answers: _Script,
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
    answers(
        [
            (_MENU, 's'),
            ('Check numbers (spaces or commas, for example 1 3; back to return): ', numbers),
        ]
    )

    assert _init._required_checks_prompt(snapshot=snapshot) == expected
    output = capsys.readouterr().out
    assert '.github/workflows/ci.yml' in output
    assert 'condition' in output
    assert '.github/workflows/release.yml' in output
    assert 'manually' in output


def test_required_checks_offer_all_listed_without_a_default_for_flagged_jobs(
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers([(_MENU, 'a')])

    assert _init._required_checks_prompt(snapshot=_workflows(ci=_CONDITIONAL)) == (
        'quality',
        'package',
    )
    out = capsys.readouterr().out
    assert 'A required check that never runs for a push cannot pass.' in out
    assert 'Leave out jobs that run only for tags, releases, or schedules.' in out


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


@pytest.mark.parametrize('connected', [None, 'ci.yml'])
def test_required_checks_have_no_default_when_discovery_is_incomplete(
    answers: _Script,
    connected: str | None,
) -> None:
    answers([(_MENU, 'n')])

    assert (
        _init._required_checks_prompt(snapshot=_workflows(ci=_DYNAMIC), connected=connected) == ()
    )
