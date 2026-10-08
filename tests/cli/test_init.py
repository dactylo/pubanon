import json
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest

from pubanon import _cli, _config, _policy
from pubanon._cli import _context, _init, _init_draft
from pubanon._errors import PubanonError
from pubanon._snapshots import _candidate, _records, _source
from pubanon._terminal import escape
from tests._cli import gh_calls
from tests._git import git_env
from tests._setup import configure

_Script = Callable[[Sequence[str | tuple[str, str]]], None]

_CONNECTABLE = """\
on:
  push:
  workflow_dispatch:
    inputs:
      hosted:
        type: boolean
      full_tests:
        type: boolean
jobs:
  quality:
    if: github.event_name != 'push'
    runs-on: linux
  hosted-validation:
    runs-on: linux
"""
_EDIT = (
    'Edit: [v]isibility / [s]ettings / [r]oute / [c]hecks / re[l]ease pages / [t]ags / [b]ack: '
)
_KEEP_CHECKS = (
    'Require checks: [a]ll listed / [s]elect / [n]one / [m]anual / [k]eep current [keep current]: '
)
_PAGES = 'Create a release page for each published version? [y/N] '
_SAVE = 'Save this configuration? [s]ave / [e]dit / [c]ancel: '


def _assert_initial_selection_sections(output: str) -> None:
    decisions = output.split('Files needing your decision\n', maxsplit=1)[1].split(
        'Files to publish\n', maxsplit=1
    )[0]
    assert (
        'These filenames can contain credentials. '
        'A name match is a warning, not proof of a secret.\n' in decisions
    )
    assert '.env' in decisions
    assert 'keys/id_rsa' in decisions
    published = output.split('Files to publish\n', maxsplit=1)[1].split(
        'Excluded files\n', maxsplit=1
    )[0]
    assert '  .env\n' in published
    assert '  README.md\n' in published
    assert '  docs/ - 1 file\n' in published
    assert '    docs/guide.md\n' in published
    assert '  src/ - 2 files\n' in published
    assert '    src/a.py\n' in published
    assert '    src/b.py\n' in published
    excluded = output.split('Excluded files\n', maxsplit=1)[1]
    assert '  .pubanon.toml - pubanon configuration (always excluded)\n' in excluded
    assert '  keys/id_rsa\n\n  Exclusions override included directories.\n' in excluded


def _assert_selection_rule_details(output: str, *, verbose: bool) -> None:
    if verbose:
        assert 'Proposed allow rules' in output
        assert '  README.md: 1 file\n' in output
        assert '  docs/guide.md: 1 file\n' in output
        assert '  src/**: 2 files\n' in output
        assert '  .env: 1 file\n' in output
        assert 'Examples are the first two matching files in path order.\n' in output
        assert '    sample: src/a.py\n' in output
        assert '    sample: src/b.py\n' in output
        assert 'Exclude rules' in output
        assert 'Deny rules take precedence over allow rules.\n' in output
        exclude_rules = output.split('Exclude rules\n', maxsplit=1)[1].split(
            'Publish list saved to:', maxsplit=1
        )[0]
        assert '  keys/id_rsa\n' in exclude_rules
    else:
        assert 'Proposed allow rules' not in output
        assert 'Publish list saved to:' not in output
        assert 'sample:' not in output
        assert 'Exclude rules' not in output
        assert 'precedence' not in output.casefold()


def _default_route(answer: Callable[[str], str]) -> Callable[[str], str]:
    def _reply(prompt: str) -> str:
        if prompt.startswith('Publication route (staged/direct) ['):
            return ''
        if prompt.startswith('Create local tags marking the source of each publication? '):
            return ''
        if prompt in {'Enable pull requests? [y/N] ', 'Enable discussions? [y/N] '}:
            return ''
        if prompt == 'Create a release page for each published version? [y/N] ':
            return ''
        return answer(prompt)

    return _reply


def _edit_checks(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    workflows: dict[str, str],
    **changes: object,
) -> tuple[_context.ProjectContext, _source.Snapshot, _config.ProjectConfig]:
    files = {f'.github/workflows/{name}': data for name, data in workflows.items()}
    source = make_repo(files={**files, 'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    project = replace(project, **changes)
    snapshot = _source.read_snapshot(source, _source.pin(source, 'HEAD').commit)
    return _context.ProjectContext(paths, global_config, project), snapshot, project


def _unselected_project(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_config.Paths, Path]:
    source = make_repo(files={'README.md': 'public\n', 'extra.txt': 'new\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    _config.save_project(paths, replace(project, allow=('README.md',)))
    monkeypatch.chdir(source)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    return paths, paths.config_dir / 'projects' / 'demo.toml'


def test_init_back_corrects_flagged_and_group_choices(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'.env': 'fixture\n', 'README.md': 'public\n'})
    paths, _global, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    replies = iter(['e', 'b', 'i', 'r', 'e', 'b', 'i', 'c', '', '', '', '', '', '', '', '', 's'])

    def _answer(_prompt: str) -> str:
        return next(replies)

    monkeypatch.setattr('builtins.input', _default_route(_answer))
    assert _cli.main(['init', '--project', 'demo']) == 0
    output = capsys.readouterr().out
    assert output
    assert not output.startswith('\n')
    project = _config.load_project(paths, 'demo')
    assert project.allow == ('README.md', '.env')
    assert project.deny == ()
    assert project.secret_exceptions == ('.env',)


@pytest.mark.parametrize(
    'tag_case',
    [
        ('', None, True),
        ('n', None, False),
        (None, '--no-source-tags', False),
        (None, '--source-tags', True),
    ],
)
def test_init_collects_complete_repository_settings_for_new_project(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tag_case: tuple[str | None, str | None, bool],
) -> None:
    tag_answer, flag, source_tags = tag_case
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    prompts: list[str] = []
    answers = {
        'Use this selection? [c]ontinue / [r]eview / [b]ack / [q]uit: ': 'c',
        'GitHub owner [anonymous-account]: ': '',
        'GitHub repository [demo]: ': '',
        'Repository visibility (private/public) [private]: ': '',
        'Publication route (staged/direct) [staged]: ': '',
        'Required CI checks (comma separated, blank for none): ': '',
        'Create a release page for each published version? [y/N] ': 'y',
        'Repository description (blank for none): ': 'Example project',
        'Enable issues? [Y/n] ': '',
        'Enable wiki? [y/N] ': '',
        'Enable projects? [y/N] ': 'y',
        'Enable pull requests? [y/N] ': '',
        'Enable discussions? [y/N] ': 'y',
        'Save this configuration? [s]ave / [e]dit / [c]ancel: ': 's',
    }

    if tag_answer is not None:
        answers['Create local tags marking the source of each publication? [Y/n] '] = tag_answer

    def _answer(prompt: str) -> str:
        prompts.append(prompt)
        if prompt in answers:
            return answers[prompt]
        pytest.fail(f'unexpected init prompt: {prompt}')

    monkeypatch.setattr('builtins.input', _answer)

    assert _cli.main(['init', '--project', 'demo', *([flag] if flag else [])]) == 0

    project = _config.load_project(paths, 'demo')
    assert project.settings == _config.Settings(
        description='Example project',
        default_branch='main',
        issues=True,
        wiki=False,
        projects=True,
        pull_requests=False,
        discussions=True,
    )
    assert project.source_tags is source_tags
    assert project.release_page == _config.ReleasePageSettings(enabled=True)
    assert prompts.count('Create local tags marking the source of each publication? [Y/n] ') == (
        tag_answer is not None
    )
    text = (paths.config_dir / 'projects' / 'demo.toml').read_text()
    assert '[settings]\ndescription = "Example project"\n' in text
    assert '[release_page]\nenabled = true\n' in text
    assert 'projects = true\npull_requests = false\ndiscussions = true\n' in text
    ordered = [
        'Repository visibility (private/public) [private]: ',
        'Repository description (blank for none): ',
        'Enable issues? [Y/n] ',
        'Enable pull requests? [y/N] ',
        'Enable discussions? [y/N] ',
        'Enable projects? [y/N] ',
        'Enable wiki? [y/N] ',
        'Publication route (staged/direct) [staged]: ',
        'Required CI checks (comma separated, blank for none): ',
        'Create a release page for each published version? [y/N] ',
        *(
            ['Create local tags marking the source of each publication? [Y/n] ']
            if tag_answer is not None
            else []
        ),
    ]
    positions = [prompts.index(prompt) for prompt in ordered]
    assert positions == sorted(positions)
    review = capsys.readouterr().out
    assert 'Each page is created right after publishing, before CI runs.' in review
    assert 'Release pages:' in review
    assert 'pubanon publishes from the private source and does not merge on GitHub.' in review
    assert 'Features:' in review
    assert 'issues, discussions, and projects on' in review
    assert 'pull requests and wiki off' in review
    assert 'Staging repository:' in review
    assert 'Publication uses the main branch.' not in review
    assert 'Setup progress saved.' not in review
    assert 'Next: pubanon repo create' in review


def test_init_connects_none_of_several_connectable_workflows(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(
        files={
            '.github/workflows/ci.yml': _CONNECTABLE,
            '.github/workflows/other.yml': _CONNECTABLE,
            'README.md': 'public\n',
        }
    )
    paths, _global, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    replies = iter(['c', '', '', '', '', '', '', '', 'n', 's'])

    def _reply(_prompt: str) -> str:
        return next(replies)

    monkeypatch.setattr('builtins.input', _default_route(_reply))
    assert _cli.main(['init', '--project', 'demo']) == 0

    assert _config.load_project(paths, 'demo').ci_workflow is None
    assert (
        'Several workflows can be connected: ci.yml, other.yml. '
        'Choose one with pubanon init --edit.'
    ) in capsys.readouterr().out


def test_init_connects_nothing_on_the_direct_route(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'.github/workflows/ci.yml': _CONNECTABLE, 'README.md': 'public\n'})
    paths, _global, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    replies = iter(['c', '', '', '', '', '', '', '', 'a', 's'])
    prompts: list[str] = []

    def _reply(prompt: str) -> str:
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr('builtins.input', _default_route(_reply))
    assert _cli.main(['init', '--project', 'demo', '--publication-route', 'direct']) == 0

    project = _config.load_project(paths, 'demo')
    assert project.ci_workflow is None
    assert project.required_checks == ('quality', 'hosted-validation')
    assert 'Require checks: [a]ll listed / [s]elect / [n]one / [m]anual: ' in prompts


def test_init_connects_the_single_connectable_workflow_on_the_staged_route(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'.github/workflows/ci.yml': _CONNECTABLE, 'README.md': 'public\n'})
    paths, _global, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    replies = iter(['c', '', '', '', '', '', '', '', '', 's'])
    prompts: list[str] = []

    def _reply(prompt: str) -> str:
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr('builtins.input', _default_route(_reply))
    assert _cli.main(['init', '--project', 'demo']) == 0

    project = _config.load_project(paths, 'demo')
    assert project.ci_workflow == 'ci.yml'
    assert project.required_checks == ('quality', 'hosted-validation')
    assert 'Require checks: [a]ll listed / [s]elect / [n]one / [m]anual [all listed]: ' in prompts
    output = capsys.readouterr().out
    assert 'pubanon will request runs of ci.yml' in output
    assert re.search(r'CI workflow:\s+ci\.yml', output)


def test_init_displays_policy_offer_paths(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'private-name.txt': 'public\n'})
    paths, _global_config, _project = configure(tmp_path, monkeypatch)
    paths.global_config.write_text(
        'anon_name = "Anonymous Publisher"\n'
        'anon_email = "anonymous@example.invalid"\n'
        'sensitive_terms = ["private-name"]\n'
        'default_publication_route = "staged"\n'
        '[accounts]\n'
        'github = "anonymous-account"\n'
    )
    answers = iter(['c', '', '', '', '', '', '', '', '', 's'])

    def _answer(_prompt: str) -> str:
        return next(answers)

    monkeypatch.setattr('builtins.input', _default_route(_answer))
    monkeypatch.chdir(source)

    assert _cli.main(['init', '--project', 'demo']) == 0

    output = capsys.readouterr().out
    assert 'private-name' in output


def test_init_does_not_offer_checks_from_excluded_workflow(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(
        files={
            '.github/workflows/ci.yml': 'jobs:\n  excluded_check:\n    runs-on: linux\n',
            'README.md': 'public\n',
        }
    )
    paths, _global, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    responses = iter(['r', 'e', 'i', 'c', '', '', '', '', '', '', '', '', 's'])
    prompts: list[str] = []

    def _answer(prompt: str) -> str:
        prompts.append(prompt)
        return next(responses)

    monkeypatch.setattr('builtins.input', _default_route(_answer))
    assert _cli.main(['init', '--project', 'demo']) == 0
    assert _config.load_project(paths, 'demo').required_checks == ()
    assert not any(prompt.startswith('Require checks:') for prompt in prompts)
    assert 'GitHub checks found' not in capsys.readouterr().out


@pytest.mark.parametrize('page', [None, _config.ReleasePageSettings(enabled=None)])
def test_init_edit_accepting_off_release_pages_keeps_unset_configuration(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    answers: _Script,
    page: _config.ReleasePageSettings | None,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    _config.save_project(paths, replace(project, release_page=page))
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    before = project_file.read_bytes()
    monkeypatch.chdir(source)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    answers([(_SAVE, 'e'), 'l', (_PAGES, ''), (_SAVE, 's')])

    assert _cli.main(['init', '--edit']) == 0

    assert project_file.read_bytes() == before
    assert _config.load_project(paths, 'demo').release_page == page


@pytest.mark.parametrize(
    'case',
    [('staged', (), 'ci.yml'), ('direct', (), None), ('staged', ('.github/**',), None)],
)
def test_connect_workflow_follows_the_current_route_and_selection(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    case: tuple[str, tuple[str, ...], str | None],
) -> None:
    route, deny, expected = case
    source = make_repo(files={'.github/workflows/ci.yml': _CONNECTABLE, 'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    project = replace(project, publication_route=route, deny=deny)
    snapshot = _source.read_snapshot(source, _source.pin(source, 'HEAD').commit)

    connected = _init._connect_workflow(
        _context.ProjectContext(paths, global_config, project), snapshot
    )

    assert connected.project.ci_workflow == expected


def test_init_edit_can_correct_description_after_failed_save(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    assert project.settings is not None
    monkeypatch.chdir(source)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    feature_answers = [
        ('Enable issues? [Y/n] ', ''),
        ('Enable pull requests? [y/N] ', ''),
        ('Enable discussions? [y/N] ', ''),
        ('Enable projects? [y/N] ', ''),
        ('Enable wiki? [y/N] ', ''),
    ]
    answers(
        [
            (_SAVE, 'e'),
            's',
            ('Repository description (blank for none): ', 'Private Person'),
            *feature_answers,
            (_SAVE, 's'),
            (_SAVE, 'e'),
            's',
            ('Description: [k]eep / [r]eplace / [c]lear [keep]: ', 'r'),
            ('Repository description (blank for none): ', 'Public tool'),
            *feature_answers,
            (_SAVE, 's'),
        ]
    )

    assert _cli.main(['init', '--edit']) == 0

    assert _config.load_project(paths, 'demo').settings == replace(
        project.settings, description='Public tool'
    )
    out = capsys.readouterr().out
    assert 'repository description did not pass content checks' in out
    assert 'Saved project demo.' in out


def test_init_edit_cancel_after_blocked_checks_saves_nothing(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    answers: _Script,
) -> None:
    _paths, project_file = _unselected_project(tmp_path, make_repo, monkeypatch)
    before = project_file.read_bytes()
    answers(
        [
            (_SAVE, 'e'),
            (_EDIT, 'v'),
            ('Repository visibility (private/public) [private]: ', 'public'),
            (_SAVE, 'e'),
            (_EDIT, 'c'),
            (_SAVE, 'c'),
        ]
    )

    assert _cli.main(['init', '--edit']) == 130

    assert project_file.read_bytes() == before


def test_init_edit_cancel_or_unchanged_save_writes_nothing(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, _project = configure(tmp_path, monkeypatch, source)
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    before = project_file.read_bytes()
    monkeypatch.chdir(source)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)

    answers([(_SAVE, 'c')])
    assert _cli.main(['init', '--edit']) == 130
    answers([(_SAVE, 's')])
    assert _cli.main(['init', '--edit']) == 0

    assert 'No changes to save.' in capsys.readouterr().out
    assert project_file.read_bytes() == before


def test_init_edit_changes_checks_and_repository_settings(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    assert project.settings is not None
    monkeypatch.chdir(source)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    answers(
        [
            (_SAVE, 'e'),
            'c',
            ('Required CI checks (comma separated, blank for none): ', 'test'),
            (_SAVE, 'e'),
            's',
            ('Repository description (blank for none): ', 'Public tool'),
            ('Enable issues? [Y/n] ', 'n'),
            ('Enable pull requests? [y/N] ', ''),
            ('Enable discussions? [y/N] ', 'y'),
            ('Enable projects? [y/N] ', ''),
            ('Enable wiki? [y/N] ', ''),
            (_SAVE, 's'),
        ]
    )

    assert _cli.main(['init', '--edit']) == 0

    saved = _config.load_project(paths, 'demo')
    assert saved.required_checks == ('test',)
    assert saved.settings == replace(
        project.settings, description='Public tool', issues=False, discussions=True
    )
    assert saved.allow == project.allow
    assert (saved.owner, saved.repo) == (project.owner, project.repo)
    assert 'Changing required checks invalidates a prepared snapshot.' in capsys.readouterr().out


def test_init_edit_changes_the_workflow_by_number(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    answers: _Script,
) -> None:
    ctx, snapshot, project = _edit_checks(
        tmp_path,
        make_repo,
        monkeypatch,
        {'ci.yml': _CONNECTABLE, 'other.yml': _CONNECTABLE},
        publication_route='staged',
        ci_workflow='ci.yml',
        required_checks=('quality',),
    )
    answers(
        [
            (_EDIT, 'c'),
            ('CI workflow: [k]eep / [c]onnect / [n]one [keep]: ', 'c'),
            ('Workflow number: ', '3'),
            ('Workflow number: ', '2'),
            (_KEEP_CHECKS, ''),
        ]
    )

    edited = _init._edit_project_section(ctx, snapshot, project)

    assert edited.ci_workflow == 'other.yml'
    assert edited.required_checks == ('quality',)


def test_init_edit_connects_the_offered_workflow(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx, snapshot, project = _edit_checks(
        tmp_path, make_repo, monkeypatch, {'ci.yml': _CONNECTABLE}, publication_route='staged'
    )
    answers(
        [
            (_EDIT, 'c'),
            ('CI workflow: [c]onnect / [n]one [connect]: ', ''),
            ('Require checks: [a]ll listed / [s]elect / [n]one / [m]anual [all listed]: ', ''),
        ]
    )

    edited = _init._edit_project_section(ctx, snapshot, project)

    assert edited.ci_workflow == 'ci.yml'
    assert edited.required_checks == ('quality', 'hosted-validation')
    assert 'Changing the CI workflow invalidates a prepared snapshot.' in capsys.readouterr().out


def test_init_edit_disconnects_the_workflow(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    answers: _Script,
) -> None:
    ctx, snapshot, project = _edit_checks(
        tmp_path,
        make_repo,
        monkeypatch,
        {'ci.yml': _CONNECTABLE},
        publication_route='staged',
        ci_workflow='ci.yml',
    )
    answers(
        [
            (_EDIT, 'c'),
            ('CI workflow: [k]eep / [c]onnect / [n]one [keep]: ', 'n'),
            ('Require checks: [a]ll listed / [s]elect / [n]one / [m]anual: ', 'n'),
        ]
    )

    edited = _init._edit_project_section(ctx, snapshot, project)

    assert edited.ci_workflow is None
    assert edited.required_checks == ()


def test_init_edit_invalid_saved_default_branch_can_cancel_after_failed_save(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    _config.save_global(
        paths,
        replace(global_config, sensitive_terms=(*global_config.sensitive_terms, 'main')),
        paths.global_config.read_bytes(),
    )
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    before = project_file.read_bytes()
    monkeypatch.chdir(source)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    answers(
        [
            (_SAVE, 'e'),
            't',
            ('Create local tags marking the source of each publication? [y/N] ', 'y'),
            (_SAVE, 's'),
            (_SAVE, 'c'),
        ]
    )

    assert _cli.main(['init', '--edit']) == 130

    assert project_file.read_bytes() == before
    out = capsys.readouterr().out
    assert 'repository default branch did not pass content checks' in out
    assert 'cancel and edit the project file' in out
    assert escape(project_file.as_posix()) in out


def test_init_edit_keeps_unsaved_edits_when_new_files_block_checks(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _project_file = _unselected_project(tmp_path, make_repo, monkeypatch)
    answers(
        [
            (_SAVE, 'e'),
            (_EDIT, 'v'),
            ('Repository visibility (private/public) [private]: ', 'public'),
            (_SAVE, 'e'),
            (_EDIT, 'c'),
            (_SAVE, 's'),
        ]
    )

    assert _cli.main(['init', '--edit']) == 0

    project = _config.load_project(paths, 'demo')
    assert project.visibility == 'public'
    assert project.allow == ('README.md',)
    output = capsys.readouterr().out
    assert 'Checks cannot be edited yet' in output
    assert '  extra.txt\n' in output
    assert 'Save or cancel this session first.' in output


def test_init_edit_refuses_without_project_or_terminal(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    unregistered = make_repo(files={'README.md': 'public\n'})
    configure(tmp_path, monkeypatch)
    monkeypatch.chdir(unregistered)
    assert _cli.main(['init', '--edit']) == 1
    error = capsys.readouterr().err
    assert 'no saved project to edit' in error
    assert 'pubanon init' in error

    source = make_repo(name='registered', files={'README.md': 'public\n'})
    configure(tmp_path / 'second', monkeypatch, source)
    monkeypatch.chdir(source)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)
    assert _cli.main(['init', '--edit']) == 1
    assert 'init --edit requires an interactive terminal' in capsys.readouterr().err


def test_init_edit_skips_the_workflow_prompt_without_candidates(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    answers: _Script,
) -> None:
    ctx, snapshot, project = _edit_checks(
        tmp_path, make_repo, monkeypatch, {}, publication_route='staged'
    )
    answers([(_EDIT, 'c'), ('Required CI checks (comma separated, blank for none): ', '')])

    assert _init._edit_project_section(ctx, snapshot, project).ci_workflow is None


def test_init_edit_suggests_no_workflow_on_the_direct_route(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    answers: _Script,
) -> None:
    ctx, snapshot, project = _edit_checks(
        tmp_path, make_repo, monkeypatch, {'ci.yml': _CONNECTABLE}, publication_route='direct'
    )
    answers(
        [
            (_EDIT, 'c'),
            ('CI workflow: [c]onnect / [n]one [none]: ', ''),
            ('Require checks: [a]ll listed / [s]elect / [n]one / [m]anual: ', 'n'),
        ]
    )

    assert _init._edit_project_section(ctx, snapshot, project).ci_workflow is None


def test_init_edit_turns_on_release_pages_and_keeps_other_settings(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    project = replace(
        project,
        allow=('README.md',),
        deny=('private/**',),
        _stage_repo='private-stage',
        workflows={'ship': ('publish',)},
        package=_config.PackageObservation('pypi', 'demo', ('published',)),
        commit_message=_config.CommitMessageSettings(
            tagged='Release {tag}', untagged='Update publication'
        ),
        release_page=_config.ReleasePageSettings(
            enabled=False,
            title='Version {version}',
            notes='Notes for {version}',
            changelog='CHANGELOG.md',
        ),
    )
    _config.save_project(paths, project)
    monkeypatch.chdir(source)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    answers([(_SAVE, 'e'), 'l', (_PAGES, 'y'), (_SAVE, 's')])

    assert _cli.main(['init', '--edit']) == 0

    assert project.release_page is not None
    assert _config.load_project(paths, 'demo') == replace(
        project,
        release_page=replace(project.release_page, enabled=True),
    )
    out = capsys.readouterr().out
    assert 'To change the selected files, run pubanon prepare and choose review.' in out
    assert 'Saved project demo.' in out


def test_init_edit_warns_about_a_workflow_that_cannot_be_connected(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx, snapshot, project = _edit_checks(
        tmp_path,
        make_repo,
        monkeypatch,
        {'ci.yml': _CONNECTABLE},
        publication_route='staged',
        ci_workflow='gone.yml',
    )
    answers(
        [
            (_EDIT, 'c'),
            ('CI workflow: [k]eep / [c]onnect / [n]one [keep]: ', 'k'),
            ('Required CI checks (comma separated, blank for none): ', ''),
        ]
    )

    edited = _init._edit_project_section(ctx, snapshot, project)

    assert edited.ci_workflow == 'gone.yml'
    output = capsys.readouterr().out
    assert 'gone.yml is not in the selected files.' in output
    assert 'Changing the CI workflow' not in output


def test_init_existing_project_with_local_publication_offers_prepare(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    monkeypatch.chdir(source)
    observed: list[tuple[_config.Paths, str]] = []

    def _published_refs(actual_paths: _config.Paths, name: str) -> tuple[str, dict[str, str]]:
        observed.append((actual_paths, name))
        return 'a' * 40, {}

    monkeypatch.setattr(_candidate, 'published_refs', _published_refs)

    assert _cli.main(['init', '--project', project.name]) == 0
    assert observed == [(paths, project.name)]
    assert 'Next: pubanon prepare' in capsys.readouterr().out


def test_init_final_review_can_change_release_pages(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    saves = iter(['e', 's'])
    page_answers = iter(['', 'y'])

    def _answer(prompt: str) -> str:
        if prompt.startswith('Use this selection?'):
            return 'c'
        if prompt == 'Save this configuration? [s]ave / [e]dit / [c]ancel: ':
            return next(saves)
        if prompt.startswith('Edit: '):
            assert 're[l]ease pages' in prompt
            return 'l'
        if prompt == 'Create a release page for each published version? [y/N] ':
            return next(page_answers)
        return ''

    monkeypatch.setattr('builtins.input', _answer)
    assert _cli.main(['init', '--project', 'demo']) == 0
    saved = _config.load_project(paths, 'demo')
    assert saved.release_page == _config.ReleasePageSettings(enabled=True)


def test_init_final_review_revises_resolved_policy_membership(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(
        files={
            '.env': 'fixture\n',
            'docs/guide.md': 'guide\n',
            'docs/private/notes.md': 'internal\n',
            'keys/id_rsa': 'fixture\n',
        }
    )
    paths, _global_config, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    save_actions = iter(['e', 's'])
    selection_actions = iter(['c', 'r', 'c'])
    group_actions = iter(['i', 'e'])
    credential_actions = iter(['i', 'e'])

    def _answer(prompt: str) -> str:
        if prompt == 'Use this selection? [c]ontinue / [r]eview / [b]ack / [q]uit: ':
            return next(selection_actions)
        if prompt == (
            'Include these files? [i]nclude / [e]xclude / [c]ustomize / [b]ack / [q]uit: '
        ):
            return next(group_actions)
        if prompt == 'Include it? [i]nclude / [e]xclude / [b]ack / [q]uit: ':
            return next(credential_actions)
        if prompt == 'Save this configuration? [s]ave / [e]dit / [c]ancel: ':
            return next(save_actions)
        if prompt.startswith('Edit: '):
            return 'p'
        if prompt in {
            'GitHub owner [anonymous-account]: ',
            'GitHub repository [demo]: ',
            'Repository visibility (private/public) [private]: ',
            'Required CI checks (comma separated, blank for none): ',
            'Repository description (blank for none): ',
            'Enable issues? [Y/n] ',
            'Enable wiki? [y/N] ',
            'Enable projects? [y/N] ',
        }:
            return ''
        pytest.fail(f'unexpected init prompt: {prompt}')

    monkeypatch.setattr('builtins.input', _default_route(_answer))

    assert _cli.main(['init', '--project', 'demo']) == 0

    project = _config.load_project(paths, 'demo')
    membership = _policy.evaluate_membership(
        ['docs/guide.md', 'docs/private/notes.md'],
        project.allow,
        project.deny,
    )
    assert membership.allowed == ['docs/guide.md']
    assert membership.denied == ['docs/private/notes.md']
    assert membership.unmatched == []
    credentials = _policy.evaluate_membership(
        ['.env', 'keys/id_rsa'],
        project.allow,
        project.deny,
    )
    assert credentials.allowed == ['.env']
    assert credentials.denied == ['keys/id_rsa']
    assert project.secret_exceptions == ('.env',)


def test_init_from_subdirectory_owns_and_reads_complete_repository_root(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(
        files={
            'README.md': 'root\n',
            'nested/file.txt': 'nested\n',
        }
    )
    paths, _global_config, _project = configure(tmp_path, monkeypatch)
    answers = iter(['c', '', '', '', '', '', '', '', '', 's'])

    def _answer(_prompt: str) -> str:
        return next(answers)

    monkeypatch.setattr('builtins.input', _default_route(_answer))
    monkeypatch.chdir(source / 'nested')

    assert _cli.main(['init', '--project', 'demo']) == 0

    project = _config.load_project(paths, 'demo')
    assert project.source_path == source.resolve()
    assert _config.registry_lookup(paths, source) == 'demo'
    assert _config.registry_lookup(paths, source / 'nested') == 'demo'
    complete_list = paths.data_dir / 'projects' / 'demo' / 'proposed-selection.json'
    assert json.loads(complete_list.read_text(encoding='utf-8')) == [
        'README.md',
        'nested/file.txt',
    ]


def test_init_holds_project_lock_while_reading_the_source(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    real_check = _source.check_source_safety
    checks: list[None] = []

    def _check_while_locked(root: Path) -> None:
        with (
            pytest.raises(PubanonError, match='another pubanon command is running'),
            _config.project_lock(paths, 'demo'),
        ):
            pytest.fail('init did not hold its project lock')
        real_check(root)
        checks.append(None)

    def _cancel(_prompt: str) -> str:
        return 'q'

    monkeypatch.setattr(_source, 'check_source_safety', _check_while_locked)
    monkeypatch.setattr('builtins.input', _cancel)

    assert _cli.main(['init', '--project', 'demo']) == 130
    assert checks == [None]


def test_init_offers_committed_workflow_checks_without_typing_names(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    workflow = (
        'on: push\njobs:\n  quality:\n    runs-on: ubuntu-latest\n'
        '  tests:\n    strategy:\n      matrix:\n        include:\n'
        '          - os: ubuntu-24.04\n            python-version: "3.13"\n'
        '          - os: macos-15\n            python-version: "3.13"\n'
    )
    source = make_repo(files={'.github/workflows/ci.yml': workflow, 'README.md': 'public\n'})
    paths, _global, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    (source / '.github/workflows/ci.yml').write_text(
        'jobs:\n  not_committed:\n    runs-on: linux\n'
    )
    replies = iter(['c', '', '', '', 'a', '', '', '', '', 's'])
    prompts: list[str] = []

    def _reply(prompt: str) -> str:
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr('builtins.input', _default_route(_reply))
    assert _cli.main(['init', '--project', 'demo']) == 0
    assert _config.load_project(paths, 'demo').required_checks == (
        'quality',
        'tests (ubuntu-24.04, 3.13)',
        'tests (macos-15, 3.13)',
    )
    assert 'Require checks: [a]ll listed / [s]elect / [n]one / [m]anual [all listed]: ' in prompts
    output = capsys.readouterr().out
    assert '1. quality' in output
    assert '2. tests (ubuntu-24.04, 3.13)' in output
    assert 'not_committed' not in output


def test_init_other_source_start_over_then_quit_can_resume_new_draft(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(name='current', files={'README.md': 'current\n'})
    other_source = make_repo(name='other', files={'README.md': 'other\n'})
    paths, _global, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    other_commit = _source.pin(other_source, 'HEAD').commit
    _init_draft.save(
        paths, 'demo', _init_draft.Draft(other_source, other_commit, self_publication=False)
    )

    def _answer(prompt: str) -> str:
        if prompt == 'Saved setup: [s]tart over / [q]uit: ':
            return 's'
        assert prompt == 'Use this selection? [c]ontinue / [r]eview / [b]ack / [q]uit: '
        return 'q'

    monkeypatch.setattr('builtins.input', _answer)

    assert _cli.main(['init', '--project', 'demo']) == 130
    rendered = capsys.readouterr()
    assert rendered.out.count('Setup progress saved.') == 1
    assert 'Next: rerun pubanon init --project=demo to resume' in rendered.err
    draft = _init_draft.load(paths, 'demo')
    assert draft is not None
    assert draft.source == source


def test_init_quit_at_flagged_path_does_not_claim_retained_preview(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'.env': 'fixture\n', 'README.md': 'public\n'})
    paths, _global_config, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    prompts: list[str] = []

    def _quit_flagged_path(prompt: str) -> str:
        prompts.append(prompt)
        assert prompt == 'Include it? [i]nclude / [e]xclude / [b]ack / [q]uit: '
        warning = capsys.readouterr().out
        assert 'Files needing your decision\n' in warning
        assert '.env' in warning
        assert 'Files to publish' not in warning
        return 'q'

    monkeypatch.setattr('builtins.input', _quit_flagged_path)

    assert _cli.main(['init', '--project', 'demo']) == 130
    assert prompts == ['Include it? [i]nclude / [e]xclude / [b]ack / [q]uit: ']
    assert not (paths.config_dir / 'projects' / 'demo.toml').exists()
    retained = paths.data_dir / 'projects' / 'demo' / 'proposed-selection.json'
    assert not retained.exists()
    result = capsys.readouterr()
    assert result.out.count('Setup progress saved.') == 1
    error = result.err
    assert '  project configuration was not saved; setup draft was saved' in error
    assert 'proposed selection list was retained' not in error


def test_init_quit_shows_sectioned_preview_and_retained_list(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n', 'src/a.py': 'pass\n'})
    paths, _global_config, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.setattr(sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(sys.stderr, 'isatty', lambda: True)

    def _quit_selection(prompt: str) -> str:
        assert prompt == (
            '\x1b[1mUse this selection?\x1b[0m '
            '\x1b[36m[c]ontinue / [r]eview / [b]ack / [q]uit\x1b[0m: '
        )
        preview = capsys.readouterr().out
        assert '\n\x1b[1;36mFiles needing your decision\x1b[0m\n' in preview
        assert '  None.\n' in preview
        assert '\n\x1b[1;36mFiles to publish\x1b[0m\n' in preview
        assert '  README.md\n' in preview
        assert '  \x1b[1msrc/ - 1 file\x1b[0m\n' in preview
        assert '    src/a.py\n' in preview
        assert '\n\x1b[1;36mExcluded files\x1b[0m\n  None.\n' in preview
        assert 'Exclusions override' not in preview
        assert preview.count('  None.\n') == 2
        assert 'Setup progress saved.' not in preview
        assert preview.endswith('  None.\n')
        return 'q'

    monkeypatch.setattr('builtins.input', _quit_selection)
    assert _cli.main(['init', '--project', 'styled']) == 130
    result = capsys.readouterr()
    assert result.out.count('Setup progress saved.') == 1
    assert not (paths.config_dir / 'projects' / 'styled.toml').exists()
    retained = paths.data_dir / 'projects' / 'styled' / 'proposed-selection.json'
    assert json.loads(retained.read_text()) == ['README.md', 'src/a.py']
    error = result.err
    assert '\x1b[33mCanceled.\x1b[0m' in error
    assert 'project configuration was not saved; setup draft was saved' in error


def test_init_refuses_explicit_project_conflicting_with_registry_before_config_write(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, _project = configure(tmp_path, monkeypatch, source, project='beta')
    monkeypatch.chdir(source)

    def _unexpected_prompt(_prompt: str) -> str:
        pytest.fail('registry collision reached an interactive prompt')

    monkeypatch.setattr('builtins.input', _unexpected_prompt)

    assert _cli.main(['init', '--project', 'alpha']) == 1
    assert not (paths.config_dir / 'projects' / 'alpha.toml').exists()
    assert 'beta' in capsys.readouterr().err


def test_init_refuses_noncanonical_stored_subdirectory_without_relocation(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'nested/file.txt': 'nested\n'})
    nested = source / 'nested'
    paths, _global_config, project = configure(tmp_path, monkeypatch, nested)
    assert project is not None
    original = (paths.config_dir / 'projects' / 'demo.toml').read_bytes()
    monkeypatch.chdir(nested)

    assert _cli.main(['init', '--project', 'demo']) == 1

    assert '--relocate' in capsys.readouterr().err
    assert _config.load_project(paths, 'demo').source_path == nested.resolve()
    assert (paths.config_dir / 'projects' / 'demo.toml').read_bytes() == original

    answers = iter(['y', 'y', '', '', '', '', 's'])

    def _confirm(_prompt: str) -> str:
        return next(answers)

    monkeypatch.setattr('builtins.input', _confirm)
    assert _cli.main(['init', '--project', 'demo', '--relocate']) == 0
    assert _config.load_project(paths, 'demo').source_path == source.resolve()
    assert _config.registry_lookup(paths, source) == 'demo'


def test_init_relocation_enables_self_publication(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    old_source = make_repo('old', files={'README.md': 'old\n'})
    new_source = make_repo('new', files={'README.md': 'new\n'})
    paths, _, project = configure(tmp_path, monkeypatch, old_source)
    assert project is not None
    _config.save_project(paths, replace(project, self_publication=False))
    monkeypatch.chdir(new_source)

    def _confirm(prompt: str) -> str:
        assert prompt == 'Move this project to the new source path? [y/n] '
        return 'y'

    monkeypatch.setattr('builtins.input', _confirm)
    assert _cli.main(['init', '--project', 'demo', '--relocate', '--self-publication']) == 0
    changed = _config.load_project(paths, 'demo')
    assert changed.source_path == new_source.resolve()
    assert changed.self_publication is True
    output = capsys.readouterr().out
    assert 'Relocated project demo to the current source repository.' in output
    assert 'Enabled self_publication.' in output


def test_init_relocation_registry_collision_preserves_existing_config(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo('current', files={'README.md': 'current\n'})
    old_source = make_repo('old', files={'README.md': 'old\n'})
    paths, _global_config, beta = configure(tmp_path, monkeypatch, source, project='beta')
    assert beta is not None
    alpha = replace(beta, name='alpha', source_path=old_source.resolve())
    _config.save_project(paths, alpha)
    alpha_path = paths.config_dir / 'projects' / 'alpha.toml'
    original = alpha_path.read_text()
    monkeypatch.chdir(source)

    def _confirm(prompt: str) -> str:
        pytest.fail(f'registry collision reached an unexpected prompt: {prompt}')

    monkeypatch.setattr('builtins.input', _confirm)

    assert _cli.main(['init', '--project', 'alpha', '--relocate']) == 1
    assert alpha_path.read_text() == original


def test_init_relocation_reserves_new_source_before_saving_configuration(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    old_source = make_repo('old', files={'README.md': 'old\n'})
    new_source = make_repo('new', files={'README.md': 'new\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, old_source, project='demo')
    assert project is not None
    _config.save_project(
        paths,
        replace(
            project,
            settings=_config.Settings(
                description='',
                default_branch='main',
                issues=True,
                wiki=False,
                projects=False,
                pull_requests=False,
                discussions=False,
            ),
        ),
    )
    reservation_seen: list[bool] = []
    real_save_project = _config.save_project

    def _save_after_reservation(
        actual_paths: _config.Paths, actual_project: _config.ProjectConfig
    ) -> None:
        registry = cast('dict[str, str]', json.loads(paths.registry.read_text(encoding='utf-8')))
        reservation_seen.append(registry == {str(new_source.resolve()): actual_project.name})
        real_save_project(actual_paths, actual_project)

    answers = iter([('Move this project to the new source path? [y/n] ', 'y')])

    def _confirm(prompt: str) -> str:
        expected_prompt, answer = next(answers)
        assert prompt == expected_prompt
        return answer

    monkeypatch.setattr('builtins.input', _confirm)
    monkeypatch.setattr(_config, 'save_project', _save_after_reservation)
    monkeypatch.chdir(new_source)

    assert _cli.main(['init', '--project', 'demo', '--relocate']) == 0
    assert next(answers, None) is None
    assert reservation_seen == [True]
    output = capsys.readouterr().out
    assert 'Relocated project demo to the current source repository.' in output
    assert 'Next: pubanon repo create --project=demo' in output
    assert escape((paths.config_dir / 'projects' / 'demo.toml').as_posix()) not in output


def test_init_renders_registered_project_with_its_configured_terms(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    term = 'private-canary'
    suffix = '\x1b' if sys.platform != 'win32' else '\u200b'
    registered = f'registered-{term}{suffix}'
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, project = configure(
        tmp_path,
        monkeypatch,
        source,
        project=registered,
    )
    assert project is not None
    _config.save_project(paths, replace(project, extra_terms=(term,)))
    monkeypatch.chdir(source)
    capsys.readouterr()

    assert _cli.main(['init', '--project', 'other-project']) == 1

    rendered = capsys.readouterr().err
    assert f'registered project: {escape(registered)}' in rendered
    assert suffix not in rendered
    assert term in rendered
    assert _config.registry_lookup(paths, source) == registered


def test_init_rerun_leaves_existing_settings_untouched(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    settings = _config.Settings(
        description='kept',
        default_branch='main',
        issues=True,
        wiki=False,
        projects=False,
        pull_requests=False,
        discussions=False,
    )
    _config.save_project(paths, replace(project, settings=settings))
    monkeypatch.chdir(source)

    def _unexpected(prompt: str) -> str:
        pytest.fail(f'init prompted although settings exist: {prompt}')

    monkeypatch.setattr('builtins.input', _unexpected)

    assert _cli.main(['init']) == 0
    assert _config.load_project(paths, 'demo').settings == settings
    out = capsys.readouterr().out
    assert 'Release pages:' in out
    assert 'To change settings, run `pubanon init --edit`.' in out


def test_init_reserves_new_source_before_saving_configuration(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, _project = configure(tmp_path, monkeypatch)
    answers = iter(['c', '', '', '', '', '', '', '', '', 's'])
    reservation_seen: list[bool] = []
    real_save_project = _config.save_project

    def _answer(_prompt: str) -> str:
        return next(answers)

    def _save_after_reservation(
        actual_paths: _config.Paths, project: _config.ProjectConfig
    ) -> None:
        registry = (
            cast('dict[str, str]', json.loads(paths.registry.read_text(encoding='utf-8')))
            if paths.registry.exists()
            else {}
        )
        reservation_seen.append(registry == {str(source.resolve()): project.name})
        real_save_project(actual_paths, project)

    monkeypatch.setattr('builtins.input', _default_route(_answer))
    monkeypatch.setattr(_config, 'save_project', _save_after_reservation)
    monkeypatch.chdir(source)

    assert _cli.main(['init', '--project', 'demo']) == 0
    assert reservation_seen == [True]
    output = capsys.readouterr().out
    assert 'Saved project demo.' in output
    assert 'Next: pubanon repo create --project=demo' in output
    assert escape((paths.config_dir / 'projects' / 'demo.toml').as_posix()) not in output


def test_init_resume_can_back_up_a_group_choice(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(
        files={
            'README.md': 'public\n',
            'docs/private/note.md': 'local\n',
            'docs/worknotes/plan.md': 'plan\n',
        }
    )
    paths, _global, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    first = iter(['r', 'i', 'e'])

    def _interrupt(_prompt: str) -> str:
        answer = next(first, None)
        if answer is None:
            raise KeyboardInterrupt
        return answer

    monkeypatch.setattr('builtins.input', _default_route(_interrupt))
    assert _cli.main(['init', '--project', 'demo']) == 130
    resumed = iter(['r', 'b', 'i', 'e', 'c', '', '', '', '', '', '', '', '', 's'])

    def _resume(_prompt: str) -> str:
        return next(resumed)

    monkeypatch.setattr('builtins.input', _default_route(_resume))
    assert _cli.main(['init', '--project', 'demo']) == 0
    project = _config.load_project(paths, 'demo')
    assert project.allow == ('README.md', 'docs/private/**')
    assert project.deny == ('docs/worknotes/**',)


def test_init_resumed_draft_still_requires_final_save(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    answers = iter(['c', '', '', '', '', '', '', '', '', 'c'])
    prompts: list[str] = []

    def _reply(prompt: str) -> str:
        prompts.append(prompt)
        return next(answers)

    monkeypatch.setattr('builtins.input', _default_route(_reply))
    assert _cli.main(['init', '--project', 'demo']) == 130
    answers = iter(['r', 's'])
    prompts.clear()
    assert _cli.main(['init', '--project', 'demo']) == 0
    assert prompts == [
        'Saved setup: [r]esume / [s]tart over / [q]uit: ',
        'Save this configuration? [s]ave / [e]dit / [c]ancel: ',
    ]
    assert (paths.config_dir / 'projects' / 'demo.toml').exists()


def test_init_resumes_each_repository_setting_answer(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    first = iter(['c', '', '', '', 'Description', 'n', 'n'])

    def _interrupt(prompt: str) -> str:
        if prompt == 'Enable wiki? [y/N] ':
            raise KeyboardInterrupt
        return next(first)

    monkeypatch.setattr('builtins.input', _default_route(_interrupt))
    assert _cli.main(['init', '--project', 'demo']) == 130
    replies = iter(['r', 'y', 'Lint', 's'])
    prompts: list[str] = []

    def _resume(prompt: str) -> str:
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr('builtins.input', _default_route(_resume))
    assert _cli.main(['init', '--project', 'demo']) == 0
    assert prompts[1] == 'Enable wiki? [y/N] '
    project = _config.load_project(paths, 'demo')
    assert project.required_checks == ('Lint',)
    assert project.settings == _config.Settings(
        'Description',
        'main',
        issues=False,
        wiki=True,
        projects=False,
        pull_requests=False,
        discussions=False,
    )


def test_init_retries_description_matching_sensitive_term(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    answers = [
        ('Use this selection? [c]ontinue / [r]eview / [b]ack / [q]uit: ', 'c'),
        ('GitHub owner [anonymous-account]: ', ''),
        ('GitHub repository [demo]: ', ''),
        ('Repository visibility (private/public) [private]: ', ''),
        ('Repository description (blank for none): ', 'Work by Private Person'),
        ('Enable issues? [Y/n] ', 'n'),
        ('Enable projects? [y/N] ', 'y'),
        ('Enable wiki? [y/N] ', 'y'),
        ('Required CI checks (comma separated, blank for none): ', ''),
        (
            (
                'Edit: [p]olicy / [d]estination / [v]isibility / [s]ettings / '
                '[r]oute / [c]hecks / re[l]ease pages / [t]ags / [b]ack: '
            ),
            's',
        ),
        ('Description: [k]eep / [r]eplace / [c]lear [keep]: ', 'r'),
        ('Repository description (blank for none): ', 'Published demo'),
        ('Enable issues? [y/N] ', ''),
        ('Enable projects? [Y/n] ', ''),
        ('Enable wiki? [Y/n] ', ''),
        ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
    ]

    def _answer(prompt: str) -> str:
        assert answers, f'unexpected prompt: {prompt}'
        expected_prompt, response = answers.pop(0)
        assert prompt == expected_prompt
        return response

    monkeypatch.setattr('builtins.input', _default_route(_answer))

    assert _cli.main(['init', '--project', 'demo']) == 0
    assert answers == []

    output = capsys.readouterr().out
    diagnostics, current_value = output.split('Current description: ', 1)
    assert 'repository description did not pass content checks' in diagnostics
    assert 'Private Person' in diagnostics
    assert current_value.startswith('Work by Private Person\n')
    assert _config.load_project(paths, 'demo').settings == _config.Settings(
        description='Published demo',
        default_branch='main',
        issues=False,
        wiki=True,
        projects=True,
        pull_requests=False,
        discussions=False,
    )


def test_init_retries_late_answer_without_losing_policy(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    answers = iter(
        [
            'c',
            'publication-owner',
            'published-demo',
            'unlisted',
            'public',
            'Published demo',
            'y',
            'n',
            'n',
            'Test, Lint',
            's',
        ]
    )

    def _answer(_prompt: str) -> str:
        return next(answers)

    monkeypatch.setattr('builtins.input', _default_route(_answer))

    assert _cli.main(['init', '--project', 'demo']) == 0

    project = _config.load_project(paths, 'demo')
    assert project.allow == ('README.md',)
    assert project.owner == 'publication-owner'
    assert project.repo == 'published-demo'
    assert project.visibility == 'public'
    assert project.required_checks == ('Test', 'Lint')
    assert project.settings == _config.Settings(
        description='Published demo',
        default_branch='main',
        issues=True,
        wiki=False,
        projects=False,
        pull_requests=False,
        discussions=False,
    )
    output = capsys.readouterr().out
    assert 'Expected visibility must be private or public.' in output
    assert 'Publication uses the main branch.' not in output
    assert 'Setup progress saved.' not in output
    assert 'Next: pubanon repo create' in output


def test_init_review_follows_question_order_and_lists_each_check(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    _paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    reviewed = replace(
        project,
        publication_route='staged',
        ci_workflow='ci.yml',
        required_checks=('quality', 'test (linux, 3.13)'),
    )
    snapshot = (_source.Entry('README.md', 0o100644, 'blob', b'public\n'),)

    _init._project_review(reviewed, snapshot, global_config)

    output = capsys.readouterr().out
    assert re.findall(r'^([A-Z][A-Za-z ]+):', output, flags=re.MULTILINE) == [
        'Source',
        'Files',
        'Final repository',
        'Description',
        'Features',
        'Route',
        'Staging repository',
        'CI workflow',
        'Required checks',
        'Release pages',
        'Source tags',
    ]
    assert 'Required checks:    quality\n' + ' ' * 20 + 'test (linux, 3.13)\n' in output


def test_init_review_shows_overridden_private_staging_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    _paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    reviewed = replace(project, _stage_repo='review-snapshots', publication_route='staged')
    snapshot = (_source.Entry('README.md', 0o100644, 'blob', b'public\n'),)

    _init._project_review(reviewed, snapshot, _global_config)

    output = capsys.readouterr().out
    assert re.search(
        r'Staging repository:\s+anonymous-account/review-snapshots \(private\)', output
    )
    _init._project_review(replace(reviewed, publication_route='direct'), snapshot, _global_config)
    assert 'Staging repository:' not in capsys.readouterr().out


def test_init_review_walks_each_rule_after_the_guided_summary(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(
        files={
            '.env': 'secret fixture\n',
            'README.md': 'public\n',
            'src/a.py': 'a\n',
            'src/b.py': 'b\n',
        }
    )
    paths, _global_config, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    prompts: list[str] = []

    selection_actions = iter(['r', 'c'])

    def _answer(prompt: str) -> str:
        prompts.append(prompt)
        if prompt == 'Use this selection? [c]ontinue / [r]eview / [b]ack / [q]uit: ':
            return next(selection_actions)
        if prompt == (
            'Include these files? [i]nclude / [e]xclude / [c]ustomize / [b]ack / [q]uit: '
        ):
            return 'i'
        if prompt == 'Include it? [i]nclude / [e]xclude / [b]ack / [q]uit: ':
            return 'e'
        if prompt in {
            'GitHub owner [anonymous-account]: ',
            'GitHub repository [demo]: ',
            'Repository visibility (private/public) [private]: ',
            'Required CI checks (comma separated, blank for none): ',
            'Repository description (blank for none): ',
            'Enable issues? [Y/n] ',
            'Enable wiki? [y/N] ',
            'Enable projects? [y/N] ',
            'Save this configuration? [s]ave / [e]dit / [c]ancel: ',
        }:
            return 's' if prompt == 'Save this configuration? [s]ave / [e]dit / [c]ancel: ' else ''
        pytest.fail(f'unexpected init prompt: {prompt}')

    monkeypatch.setattr('builtins.input', _default_route(_answer))

    assert _cli.main(['init', '--project', 'demo']) == 0

    output = capsys.readouterr().out
    assert output.count('Choose the files to publish. Nothing is published now.\n') == 1
    assert (
        prompts.count(
            'Include these files? [i]nclude / [e]xclude / [c]ustomize / [b]ack / [q]uit: '
        )
        == 2
    )
    assert prompts.count('Include it? [i]nclude / [e]xclude / [b]ack / [q]uit: ') == 1
    assert (
        prompts.index('Include it? [i]nclude / [e]xclude / [b]ack / [q]uit: ')
        < prompts.index('Use this selection? [c]ontinue / [r]eview / [b]ack / [q]uit: ')
        < prompts.index(
            'Include these files? [i]nclude / [e]xclude / [c]ustomize / [b]ack / [q]uit: '
        )
    )
    assert 'Publish list saved to:' not in output
    project = _config.load_project(paths, 'demo')
    assert project.allow == ('README.md', 'src/**')
    assert project.deny == ('.env',)


def test_init_reviews_complete_configuration_before_save(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    answers = iter(
        [
            'c',
            '',
            '',
            '',
            'Published demo',
            '',
            '',
            '',
            '',
            'e',
            'v',
            'public',
            'c',
        ]
    )
    reviews: list[str] = []

    def _answer(prompt: str) -> str:
        if prompt.startswith('Save this configuration?'):
            reviews.append(capsys.readouterr().out)
        return next(answers)

    monkeypatch.setattr('builtins.input', _default_route(_answer))

    assert _cli.main(['init', '--project', 'demo']) == 130

    assert (
        '  project configuration was not saved; setup draft was saved' in capsys.readouterr().err
    )
    review = reviews[0]
    for expected in (
        escape((source.resolve()).as_posix()),
        'anonymous-account/demo (private)',
        'anonymous-account/demo-stage (private)',
        'Route:',
        'Source tags:',
        'Files:',
        '1 included, 0 excluded',
        'CI workflow:',
        'Required checks:',
        'Description:',
        'Published demo',
        'issues on; pull requests, discussions, projects, and wiki off',
    ):
        assert expected in review
    assert re.search(
        r'Release pages:\s+no',
        review,
    )
    assert 'Publication uses the main branch.' not in review
    assert not (paths.config_dir / 'projects' / 'demo.toml').exists()
    assert not paths.registry.exists()


def test_init_route_flag_preserves_prepared_candidate_and_other_settings(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_gh: Path,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    monkeypatch.chdir(source)
    assert _cli.main(['prepare', '--project', 'demo', '--acknowledge-new-paths', '--yes']) == 0
    candidate = _records.load_candidate(paths, 'demo')
    assert candidate is not None
    candidate_file = _records.candidate_path(paths, 'demo')
    candidate_bytes = candidate_file.read_bytes()
    store = paths.data_dir / 'projects' / 'demo' / 'store.git'
    commit_bytes = subprocess.run(
        ['git', '--git-dir', str(store), 'cat-file', 'commit', candidate.commit],
        capture_output=True,
        check=True,
        env=git_env(),
    ).stdout
    timestamps = tuple(
        line for line in commit_bytes.splitlines() if line.startswith((b'author ', b'committer '))
    )
    stage_file = paths.data_dir / 'projects' / 'demo' / 'stage.toml'
    stage_file.write_bytes(b'staging-tracking-sentinel\n')
    fake_gh.write_text('[]')
    calls_before = gh_calls(tmp_path)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: False)

    def _unexpected(prompt: str) -> str:
        pytest.fail(f'route flag prompted: {prompt}')

    monkeypatch.setattr('builtins.input', _unexpected)
    assert _cli.main(['init', '--publication-route', 'staged', '--source-tags']) == 0

    changed = _config.load_project(paths, 'demo')
    assert changed == replace(project, publication_route='staged', source_tags=True)
    assert _config.fingerprint(global_config, changed) == candidate.fingerprint
    assert _records.load_candidate(paths, 'demo') == candidate
    assert candidate_file.read_bytes() == candidate_bytes
    retained_commit = subprocess.run(
        ['git', '--git-dir', str(store), 'cat-file', 'commit', candidate.commit],
        capture_output=True,
        check=True,
        env=git_env(),
    ).stdout
    assert retained_commit == commit_bytes
    assert (
        tuple(
            line
            for line in retained_commit.splitlines()
            if line.startswith((b'author ', b'committer '))
        )
        == timestamps
    )
    assert stage_file.read_bytes() == b'staging-tracking-sentinel\n'
    assert gh_calls(tmp_path) == calls_before
    output = capsys.readouterr().out
    assert output.index('Route:') < output.index('Updated project demo')
    assert 'Private staging is required before final publication.' not in output


def test_init_route_flag_refuses_incomplete_saved_project_without_rewriting(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    project_file = paths.config_dir / 'projects' / 'demo.toml'
    incomplete = '\n'.join(
        line
        for line in project_file.read_text().splitlines()
        if not line.startswith('publication_route')
    ).encode()
    project_file.write_bytes(incomplete)
    monkeypatch.chdir(source)

    assert _cli.main(['init', '--publication-route', 'staged']) == 1
    assert project_file.read_bytes() == incomplete


def test_init_same_source_uses_idempotent_registry_update(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    _config.save_project(
        paths,
        replace(
            project,
            settings=_config.Settings(
                description='',
                default_branch='main',
                issues=True,
                wiki=False,
                projects=False,
                pull_requests=False,
                discussions=False,
            ),
        ),
    )
    monkeypatch.chdir(source)

    def _unexpected_install(_paths: _config.Paths, _project: _config.ProjectConfig) -> None:
        pytest.fail('same-source init used the installation visibility boundary')

    monkeypatch.setattr(_config, 'install_project', _unexpected_install)

    assert _cli.main(['init', '--project', project.name]) == 0
    output = capsys.readouterr().out
    assert 'Refreshed source registration for project demo.' in output
    assert 'Next: pubanon repo create' in output
    assert escape((paths.config_dir / 'projects' / 'demo.toml').as_posix()) not in output


def test_init_self_publication_flag_enables_existing_project(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _, _ = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    answers(
        [
            ('Use this selection? [c]ontinue / [r]eview / [b]ack / [q]uit: ', 'c'),
            ('GitHub owner [anonymous-account]: ', ''),
            ('GitHub repository [demo]: ', ''),
            ('Repository visibility (private/public) [private]: ', ''),
            ('Repository description (blank for none): ', ''),
            ('Enable issues? [Y/n] ', ''),
            ('Enable pull requests? [y/N] ', ''),
            ('Enable discussions? [y/N] ', ''),
            ('Enable projects? [y/N] ', ''),
            ('Enable wiki? [y/N] ', ''),
            ('Publication route (staged/direct) [staged]: ', ''),
            ('Required CI checks (comma separated, blank for none): ', ''),
            ('Create a release page for each published version? [y/N] ', ''),
            ('Create local tags marking the source of each publication? [Y/n] ', ''),
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
        ]
    )
    assert _cli.main(['init', '--project', 'demo']) == 0
    assert _config.load_project(paths, 'demo').self_publication is False
    capsys.readouterr()

    assert _cli.main(['init', '--self-publication', '--yes']) == 0
    assert _config.load_project(paths, 'demo').self_publication is True
    assert 'Enabled self_publication.' in capsys.readouterr().out

    assert _cli.main(['init', '--yes']) == 0
    assert _config.load_project(paths, 'demo').self_publication is True
    assert 'Enabled self_publication.' not in capsys.readouterr().out


def test_init_self_publication_flag_sets_project_field(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    answers: _Script,
) -> None:
    source = make_repo(name='pubanon', files={'README.md': 'public\n'})
    paths, _, _ = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    answers(
        [
            ('Use this selection? [c]ontinue / [r]eview / [b]ack / [q]uit: ', 'c'),
            ('GitHub owner [anonymous-account]: ', ''),
            ('GitHub repository [pubanon]: ', ''),
            ('Repository visibility (private/public) [private]: ', ''),
            ('Repository description (blank for none): ', ''),
            ('Enable issues? [Y/n] ', ''),
            ('Enable pull requests? [y/N] ', ''),
            ('Enable discussions? [y/N] ', ''),
            ('Enable projects? [y/N] ', ''),
            ('Enable wiki? [y/N] ', ''),
            ('Publication route (staged/direct) [staged]: ', ''),
            ('Required CI checks (comma separated, blank for none): ', ''),
            ('Create a release page for each published version? [y/N] ', ''),
            ('Create local tags marking the source of each publication? [Y/n] ', ''),
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
        ]
    )
    assert _cli.main(['init', '--self-publication']) == 0
    assert _config.load_project(paths, 'pubanon').self_publication is True


def test_init_skips_globally_ignored_directories(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(
        files={
            'README.md': 'public',
            'docs/private/private.key': 'private',
            'notes/confidential/notes.md': 'private',
        }
    )
    paths, global_config, _ = configure(tmp_path, monkeypatch)
    _config.save_global(
        paths,
        replace(
            global_config,
            ignored_directories=(
                'DOCS/Private',
                'Notes/CONFIDENTIAL',
            ),
        ),
        paths.global_config.read_bytes(),
    )
    monkeypatch.chdir(source)

    def answer(prompt: str) -> str:
        if prompt.startswith('Use this selection?'):
            return 'c'
        if prompt.startswith('Save this configuration?'):
            return 's'
        if prompt.startswith(('Include', 'Selection pattern', 'Replacement pattern')):
            pytest.fail(f'Unexpected selection question: {prompt}')
        if prompt.startswith(
            (
                'GitHub owner',
                'GitHub repository',
                'Repository visibility',
                'Publication route',
                'Required CI checks',
                'Create a release page',
                'Repository description',
                'Enable issues',
                'Enable wiki',
                'Enable projects',
                'Enable pull requests',
                'Enable discussions',
                'Create local tags',
            )
        ):
            return ''
        pytest.fail(f'Unexpected init question: {prompt}')

    monkeypatch.setattr('builtins.input', answer)
    assert _cli.main(['init', '--project', 'demo']) == 0
    output = capsys.readouterr().out
    assert '1 included, 2 excluded' in output
    assert 'globally ignored' in output
    project = _config.load_project(paths, 'demo')
    assert project.allow == ('README.md',)
    assert project.deny == ()


def test_init_skips_globally_ignored_files(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(
        files={
            'README.md': 'public',
            'LOCAL.md': 'private',
            'nested/LOCAL.md': 'private',
        }
    )
    paths, global_config, _ = configure(tmp_path, monkeypatch)
    _config.save_global(
        paths,
        replace(
            global_config,
            ignored_files=('local.MD',),
        ),
        paths.global_config.read_bytes(),
    )
    monkeypatch.chdir(source)

    def answer(prompt: str) -> str:
        if prompt.startswith('Use this selection?'):
            return 'c'
        if prompt.startswith('Save this configuration?'):
            return 's'
        if prompt.startswith(('Include', 'Selection pattern', 'Replacement pattern')):
            pytest.fail(f'Unexpected selection question: {prompt}')
        if prompt.startswith(
            (
                'GitHub owner',
                'GitHub repository',
                'Repository visibility',
                'Publication route',
                'Required CI checks',
                'Create a release page',
                'Repository description',
                'Enable issues',
                'Enable wiki',
                'Enable projects',
                'Enable pull requests',
                'Enable discussions',
                'Create local tags',
            )
        ):
            return ''
        pytest.fail(f'Unexpected init question: {prompt}')

    monkeypatch.setattr('builtins.input', answer)
    assert _cli.main(['init', '--project', 'demo']) == 0
    output = capsys.readouterr().out
    assert '1 included, 2 excluded' in output
    assert 'globally ignored' in output
    project = _config.load_project(paths, 'demo')
    assert project.allow == ('README.md',)
    assert project.deny == ()


def test_init_source_change_rechecks_selection_without_losing_exclusions(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n', 'docs/private/note.md': 'local\n'})
    paths, _global, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    answers = iter(['r', 'i', 'e', 'c', 'publication-owner'])

    def _reply(_prompt: str) -> str:
        answer = next(answers, None)
        if answer is None:
            raise KeyboardInterrupt
        return answer

    monkeypatch.setattr('builtins.input', _default_route(_reply))
    assert _cli.main(['init', '--project', 'demo']) == 130
    (source / 'docs/private/new.md').write_text('also local\n')
    subprocess.run(['git', '-C', source, 'add', '-A'], check=True, env=git_env())
    subprocess.run(
        [
            'git',
            '-C',
            source,
            '-c',
            'user.name=Test',
            '-c',
            'user.email=test@example.invalid',
            'commit',
            '-qm',
            'Add file',
        ],
        check=True,
        env=git_env(),
    )
    answers = iter(['r', 'c', '', '', '', '', '', '', '', 's'])
    assert _cli.main(['init', '--project', 'demo']) == 0
    project = _config.load_project(paths, 'demo')
    assert project.owner == 'publication-owner'
    assert project.deny == ('docs/private/**',)
    assert 'The source changed. Review the updated selection' in capsys.readouterr().out
    preview = paths.data_dir / 'projects' / 'demo' / 'proposed-selection.json'
    assert json.loads(preview.read_text()) == ['README.md']


def test_init_source_tags_flags_update_existing_project(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    monkeypatch.chdir(source)

    def _unexpected(prompt: str) -> str:
        pytest.fail(f'init source tags flag prompted: {prompt}')

    monkeypatch.setattr('builtins.input', _unexpected)
    assert _cli.main(['init', '--source-tags']) == 0
    assert _config.load_project(paths, 'demo') == replace(project, source_tags=True)
    assert _cli.main(['init', '--no-source-tags']) == 0
    assert _config.load_project(paths, 'demo') == project


def test_init_start_over_replaces_draft_only_on_explicit_choice(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'.env': 'fixture\n', 'README.md': 'public\n'})
    paths, _global, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    answers = iter(['e', 'q'])

    def _reply(_prompt: str) -> str:
        return next(answers)

    monkeypatch.setattr('builtins.input', _default_route(_reply))
    assert _cli.main(['init', '--project', 'demo']) == 130
    draft = paths.data_dir / 'projects' / 'demo' / 'init-draft.json'
    saved = draft.read_bytes()
    answers = iter(['q'])
    assert _cli.main(['init', '--project', 'demo']) == 130
    assert draft.read_bytes() == saved
    answers = iter(['s', 'i', 'c', '', '', '', '', '', '', '', '', 's'])
    assert _cli.main(['init', '--project', 'demo']) == 0
    assert _config.load_project(paths, 'demo').secret_exceptions == ('.env',)
    assert not draft.exists()


def test_init_yes_refuses_policy_proposals_without_writing_project(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)

    assert _cli.main(['init', '--project', 'demo', '--yes']) == 1
    error = capsys.readouterr().err
    assert '--yes cannot answer policy proposals' in error
    assert escape((paths.config_dir / 'projects' / 'demo.toml').as_posix()) in error
    assert 'then rerun pubanon init --project=demo' in error
    assert not (paths.config_dir / 'projects' / 'demo.toml').exists()
    assert not paths.registry.exists()
    assert not (paths.data_dir / 'projects' / 'demo').exists()


def test_project_from_draft_omits_reserved_case_variants_from_proposed_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / 'source'
    source.mkdir()
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    snapshot = tuple(
        _source.Entry(path, 0o100644, 'blob', b'fixture')
        for path in ('.PubAnon.toml', 'nested/.PUBANON.TOML', 'README.md')
    )
    draft = _init_draft.Draft(source, 'a' * 40, self_publication=True)

    proposed = _init._project_from_draft(
        _context.ProjectContext(paths, global_config, project), snapshot, draft
    )

    assert proposed.project.allow == ('README.md',)


@pytest.mark.parametrize(
    'case',
    [((), None, True), ((), 'ci.yml', False), (('test',), None, False)],
)
def test_project_review_counts_a_connected_workflow_as_configured_ci(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[tuple[str, ...], str | None, bool],
) -> None:
    checks, workflow, warned = case
    source = make_repo(files={'README.md': 'public\n'})
    _paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    project = replace(
        project, publication_route='staged', required_checks=checks, ci_workflow=workflow
    )
    snapshot = _source.read_snapshot(source, _source.pin(source, 'HEAD').commit)

    _init._project_review(project, snapshot, global_config)

    output = capsys.readouterr().out
    assert re.search(rf'CI workflow:\s+{re.escape(workflow or "none")}', output)
    assert ('No CI checks configured; CI waiting is optional.' in output) is warned


@pytest.mark.parametrize(
    ('route', 'ci_configured', 'timing'),
    [
        ('staged', True, 'Each page is created when you publish, after staging checks pass.'),
        ('staged', False, 'Each page is created right after publishing, before CI runs.'),
        ('direct', True, 'Each page is created right after publishing, before CI runs.'),
    ],
)
def test_release_pages_prompt_explains_when_pages_are_created(
    route: str,
    ci_configured: bool,
    timing: str,
    answers: _Script,
    capsys: pytest.CaptureFixture[str],
) -> None:
    answers([('Create a release page for each published version? [y/N] ', 'y')])
    assert (
        _init._release_pages_prompt(route=route, ci_configured=ci_configured, default=False)
        is True
    )
    out = capsys.readouterr().out
    assert 'Release pages announce published versions.' in out
    assert 'Creating one can start release workflows, such as package uploads.' in out
    assert timing in out
    waits = 'To wait for CI, answer no and run pubanon release create later.'
    assert (waits in out) is (timing.startswith('Each page is created right after'))


@pytest.mark.parametrize('exit_mode', ['quit', 'interrupt'])
def test_init_saved_draft_prompt_retains_progress_on_cancel(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exit_mode: str,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    commit = _source.pin(source, 'HEAD').commit
    _init_draft.save(paths, 'demo', _init_draft.Draft(source, commit, self_publication=False))
    draft_path = paths.data_dir / 'projects' / 'demo' / 'init-draft.json'
    saved = draft_path.read_bytes()

    def _cancel(prompt: str) -> str:
        assert prompt == 'Saved setup: [r]esume / [s]tart over / [q]uit: '
        if exit_mode == 'interrupt':
            raise KeyboardInterrupt
        return 'q'

    monkeypatch.setattr('builtins.input', _cancel)

    assert _cli.main(['init', '--project', 'demo']) == 130
    output = capsys.readouterr()
    assert (
        output.out.count('Setup progress saved. Rerun pubanon init --project=demo to resume.') == 1
    )
    assert 'The setup draft is kept.' in output.err
    assert 'project configuration was not saved' in output.err
    assert 'Next: rerun pubanon init --project=demo to resume' in output.err
    assert draft_path.read_bytes() == saved
    assert not (paths.config_dir / 'projects' / 'demo.toml').exists()


@pytest.mark.parametrize('choice', ['s', 'q', 'interrupt'])
def test_init_unusable_draft_offers_start_over_or_quit(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    choice: str,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    _init_draft.save(paths, 'demo', _init_draft.Draft(source, 'a' * 40, self_publication=False))
    draft_path = paths.data_dir / 'projects' / 'demo' / 'init-draft.json'
    payload = json.loads(draft_path.read_text(encoding='utf-8'))
    del payload['source_tags']
    old_bytes = json.dumps(payload).encode()
    draft_path.write_bytes(old_bytes)
    prompts: list[str] = []
    removed: list[str] = []
    original_remove = _init_draft.remove

    def _remove(paths: _config.Paths, name: str) -> None:
        removed.append(name)
        original_remove(paths, name)

    def _reply(prompt: str) -> str:
        prompts.append(prompt)
        if prompt.startswith('Saved setup:'):
            if choice == 'interrupt':
                raise KeyboardInterrupt
            return choice
        raise KeyboardInterrupt

    monkeypatch.setattr(_init_draft, 'remove', _remove)
    monkeypatch.setattr('builtins.input', _reply)

    assert _cli.main(['init', '--project', 'demo']) == 130
    assert prompts[0].startswith('Saved setup:')
    assert '[s]tart over' in prompts[0]
    assert '[q]uit' in prompts[0]
    assert '[r]esume' not in prompts[0]
    rendered = capsys.readouterr()
    assert 'cannot be resumed' in rendered.out
    if choice in {'q', 'interrupt'}:
        assert 'Setup progress saved.' not in rendered.out
        assert 'Next: rerun pubanon init --project=demo and choose start over' in rendered.err
        assert len(prompts) == 1
        assert removed == []
        assert draft_path.read_bytes() == old_bytes
    else:
        assert rendered.out.count('Setup progress saved.') == 1
        assert 'Next: rerun pubanon init --project=demo to resume' in rendered.err
        assert len(prompts) > 1
        assert removed == ['demo']
        new_draft = _init_draft.load(paths, 'demo')
        assert new_draft is not None
        assert new_draft.source == source
        assert new_draft.pending[0] == 'selection'
        assert new_draft.source_tags is True
        assert draft_path.read_bytes() != old_bytes


@pytest.mark.parametrize('exit_mode', ['quit', 'interrupt'])
def test_init_other_source_draft_prompt_keeps_draft_without_offering_resume_here(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    exit_mode: str,
) -> None:
    source = make_repo(name='current', files={'README.md': 'current\n'})
    other_source = make_repo(name='other', files={'README.md': 'other\n'})
    paths, _global, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    other_commit = _source.pin(other_source, 'HEAD').commit
    _init_draft.save(
        paths, 'demo', _init_draft.Draft(other_source, other_commit, self_publication=False)
    )
    draft_path = paths.data_dir / 'projects' / 'demo' / 'init-draft.json'
    saved = draft_path.read_bytes()

    def _cancel(prompt: str) -> str:
        assert prompt == 'Saved setup: [s]tart over / [q]uit: '
        if exit_mode == 'interrupt':
            raise KeyboardInterrupt
        return 'q'

    monkeypatch.setattr('builtins.input', _cancel)

    assert _cli.main(['init', '--project', 'demo']) == 130
    rendered = capsys.readouterr()
    assert 'This draft belongs to another source repository.' in rendered.out
    assert 'Setup progress saved.' not in rendered.out
    assert 'from the draft source shown above' in rendered.err
    assert 'choose start over' in rendered.err
    assert draft_path.read_bytes() == saved
    assert not (paths.config_dir / 'projects' / 'demo.toml').exists()


@pytest.mark.parametrize(('flag', 'selected'), [(None, 'direct'), ('staged', 'staged')])
def test_new_project_uses_global_route_or_explicit_override(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    flag: str | None,
    selected: str,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, _ = configure(tmp_path, monkeypatch)
    _config.save_global(
        paths,
        replace(global_config, default_publication_route='direct'),
        paths.global_config.read_bytes(),
    )
    monkeypatch.chdir(source)
    prompts: list[str] = []
    responses = {
        'Use this selection? [c]ontinue / [r]eview / [b]ack / [q]uit: ': 'c',
        'GitHub owner [anonymous-account]: ': '',
        'GitHub repository [demo]: ': '',
        'Repository visibility (private/public) [private]: ': '',
        'Required CI checks (comma separated, blank for none): ': '',
        'Create a release page for each published version? [y/N] ': '',
        'Create local tags marking the source of each publication? [Y/n] ': '',
        'Repository description (blank for none): ': '',
        'Enable issues? [Y/n] ': '',
        'Enable wiki? [y/N] ': '',
        'Enable projects? [y/N] ': '',
        'Enable pull requests? [y/N] ': '',
        'Enable discussions? [y/N] ': '',
        'Save this configuration? [s]ave / [e]dit / [c]ancel: ': 's',
    }
    if flag is None:
        responses['Publication route (staged/direct) [direct]: '] = ''

    def _answer(prompt: str) -> str:
        prompts.append(prompt)
        if prompt not in responses:
            pytest.fail(f'unexpected prompt: {prompt}')
        return responses[prompt]

    monkeypatch.setattr('builtins.input', _answer)
    command = ['init', '--project', 'demo']
    if flag is not None:
        command.extend(['--publication-route', flag])
    assert _cli.main(command) == 0
    assert _config.load_project(paths, 'demo').publication_route == selected
    assert any(prompt.startswith('Publication route (') for prompt in prompts) is (flag is None)


@pytest.mark.parametrize(
    'verbosity_args',
    [
        pytest.param([], id='default'),
        pytest.param(['-v'], id='short-verbose'),
        pytest.param(['--verbose'], id='long-verbose'),
    ],
)
def test_init_continue_accepts_complete_guided_policy_proposal(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verbosity_args: list[str],
) -> None:
    source = make_repo(
        files={
            '.env': 'secret fixture\n',
            '.pubanon.toml': 'reserved fixture\n',
            'README.md': 'public\n',
            'docs/guide.md': 'guide\n',
            'keys/id_rsa': 'credential fixture\n',
            'src/a.py': 'a\n',
            'src/b.py': 'b\n',
        }
    )
    paths, _global_config, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    prompts: list[str] = []
    before_action: list[str] = []
    credential_actions = iter(['i', 'e'])
    complete_list = paths.data_dir / 'projects' / 'demo' / 'proposed-selection.json'

    def _answer(prompt: str) -> str:
        prompts.append(prompt)
        if prompt == 'Use this selection? [c]ontinue / [r]eview / [b]ack / [q]uit: ':
            before_action.append(capsys.readouterr().out)
            assert json.loads(complete_list.read_text(encoding='utf-8')) == [
                '.env',
                'README.md',
                'docs/guide.md',
                'src/a.py',
                'src/b.py',
            ]
            return 'c'
        if prompt == 'Include it? [i]nclude / [e]xclude / [b]ack / [q]uit: ':
            return next(credential_actions)
        if prompt in {
            'GitHub owner [anonymous-account]: ',
            'GitHub repository [demo]: ',
            'Repository visibility (private/public) [private]: ',
            'Required CI checks (comma separated, blank for none): ',
            'Repository description (blank for none): ',
            'Enable issues? [Y/n] ',
            'Enable wiki? [y/N] ',
            'Enable projects? [y/N] ',
            'Save this configuration? [s]ave / [e]dit / [c]ancel: ',
        }:
            return 's' if prompt == 'Save this configuration? [s]ave / [e]dit / [c]ancel: ' else ''
        pytest.fail(f'unexpected init prompt: {prompt}')

    monkeypatch.setattr('builtins.input', _default_route(_answer))

    assert _cli.main(['init', *verbosity_args, '--project', 'demo']) == 0

    output = ''.join((*before_action, capsys.readouterr().out))
    if verbosity_args:
        assert before_action[0].count(escape((complete_list).as_posix())) == 1
    else:
        assert escape((complete_list).as_posix()) not in before_action[0]
    assert 'Project source' in before_action[0]
    assert escape((source.resolve()).as_posix()) in before_action[0]
    for path in ('.env', 'README.md', 'docs/guide.md', 'src/a.py', 'src/b.py'):
        assert f'  {path}\n' in before_action[0]
    assert json.loads(complete_list.read_text(encoding='utf-8')) == [
        '.env',
        'README.md',
        'docs/guide.md',
        'src/a.py',
        'src/b.py',
    ]
    assert output.count('Choose the files to publish. Nothing is published now.\n') == 1
    _assert_initial_selection_sections(before_action[0])
    selection_index = prompts.index(
        'Use this selection? [c]ontinue / [r]eview / [b]ack / [q]uit: '
    )
    assert (
        prompts[:selection_index].count('Include it? [i]nclude / [e]xclude / [b]ack / [q]uit: ')
        == 2
    )
    assert prompts.count('Include it? [i]nclude / [e]xclude / [b]ack / [q]uit: ') == 2
    _assert_selection_rule_details(output, verbose=bool(verbosity_args))
    assert (
        'Include these files? [i]nclude / [e]xclude / [c]ustomize / [b]ack / [q]uit: '
    ) not in prompts
    assert not any('pubanon repository' in prompt for prompt in prompts)
    project = _config.load_project(paths, 'demo')
    assert project.allow == ('README.md', 'docs/guide.md', 'src/**', '.env')
    assert project.deny == ('keys/id_rsa',)
    assert project.secret_exceptions == ('.env',)
    assert project.owner == 'anonymous-account'
    assert project.owner != source.name
    assert project.repo == 'demo'
    assert project.visibility == 'private'
    assert project.self_publication is False


@pytest.mark.parametrize('name', ['.', '..'])
def test_init_refuses_dot_project_names_before_prompt_or_state_write(
    name: str,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)

    def _unexpected_prompt(_prompt: str) -> str:
        pytest.fail('invalid project name reached an interactive prompt')

    monkeypatch.setattr('builtins.input', _unexpected_prompt)

    assert _cli.main(['init', '--project', name]) == 1

    assert 'invalid project name' in capsys.readouterr().err
    assert not paths.registry.exists()
    assert not (paths.config_dir / 'projects').exists()
    assert not (paths.data_dir / 'projects' / name / 'project.lock').exists()


@pytest.mark.parametrize(
    'resume_case',
    [(KeyboardInterrupt, False), (EOFError, False), (KeyboardInterrupt, True), (EOFError, True)],
)
def test_init_resumes_saved_answers_without_repeating_selection(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    resume_case: tuple[type[BaseException], bool],
) -> None:
    failure, enable_on_resume = resume_case
    source = make_repo(files={'README.md': 'public\n', 'docs/private/note.md': 'local\n'})
    paths, _global, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    first = iter(['r', 'i', 'e', 'c', 'publication-owner'])

    def _interrupt(prompt: str) -> str:
        if prompt == 'GitHub repository [demo]: ':
            raise failure
        return next(first)

    monkeypatch.setattr('builtins.input', _default_route(_interrupt))
    initial_args = ['init', '--project', 'demo']
    if not enable_on_resume:
        initial_args.append('--self-publication')
    assert _cli.main(initial_args) != 0
    assert not (paths.config_dir / 'projects' / 'demo.toml').exists()
    assert _config.registry_lookup(paths, source) is None
    assert 'setup draft was saved' in capsys.readouterr().err
    resumed = iter(['r', 'published-demo', '', '', '', '', '', '', 's'])
    prompts: list[str] = []

    def _resume(prompt: str) -> str:
        prompts.append(prompt)
        return next(resumed)

    monkeypatch.setattr('builtins.input', _default_route(_resume))
    resumed_args = ['init', '--project', 'demo']
    if enable_on_resume:
        resumed_args.append('--self-publication')
    assert _cli.main(resumed_args) == 0
    assert prompts[0] == 'Saved setup: [r]esume / [s]tart over / [q]uit: '
    assert prompts[1] == 'GitHub repository [demo]: '
    project = _config.load_project(paths, 'demo')
    assert project.owner == 'publication-owner'
    assert project.repo == 'published-demo'
    assert project.deny == ('docs/private/**',)
    assert project.self_publication is True
    assert not (paths.data_dir / 'projects' / 'demo' / 'init-draft.json').exists()


@pytest.mark.parametrize(
    'case',
    [
        ('c', None, ''),
        ('', None, 'Published demo'),
        ('r', 'New description', 'New description'),
    ],
)
def test_init_review_can_clear_keep_or_replace_description(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[str, str | None, str],
) -> None:
    action, replacement, expected = case
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    answers = [
        ('Use this selection? [c]ontinue / [r]eview / [b]ack / [q]uit: ', 'c'),
        ('GitHub owner [anonymous-account]: ', ''),
        ('GitHub repository [demo]: ', ''),
        ('Repository visibility (private/public) [private]: ', ''),
        ('Repository description (blank for none): ', 'Published demo'),
        ('Enable issues? [Y/n] ', ''),
        ('Enable projects? [y/N] ', ''),
        ('Enable wiki? [y/N] ', ''),
        ('Required CI checks (comma separated, blank for none): ', ''),
        ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 'e'),
        (
            (
                'Edit: [p]olicy / [d]estination / [v]isibility / [s]ettings / '
                '[r]oute / [c]hecks / re[l]ease pages / [t]ags / [b]ack: '
            ),
            's',
        ),
        ('Description: [k]eep / [r]eplace / [c]lear [keep]: ', action),
        *(
            []
            if replacement is None
            else [('Repository description (blank for none): ', replacement)]
        ),
        ('Enable issues? [Y/n] ', ''),
        ('Enable projects? [y/N] ', ''),
        ('Enable wiki? [y/N] ', ''),
        ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
    ]

    def _answer(prompt: str) -> str:
        assert answers, f'unexpected prompt: {prompt}'
        expected_prompt, response = answers.pop(0)
        assert prompt == expected_prompt
        return response

    monkeypatch.setattr('builtins.input', _default_route(_answer))

    assert _cli.main(['init', '--project', 'demo']) == 0
    assert answers == []
    project = _config.load_project(paths, 'demo')
    assert project.settings is not None
    assert project.settings.description == expected
    assert re.search(
        r'Description:\s+' + re.escape(expected or '(empty)'), capsys.readouterr().out
    )


def test_relocation_invalidates_candidate_and_discard_prepare_recovers(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    _config.save_project(
        paths,
        replace(
            project,
            settings=_config.Settings(
                description='',
                default_branch='main',
                issues=True,
                wiki=False,
                projects=False,
                pull_requests=False,
                discussions=False,
            ),
        ),
    )
    monkeypatch.chdir(source)
    assert _cli.main(['prepare', '--project', 'demo', '--acknowledge-new-paths', '--yes']) == 0
    assert _records.load_candidate(paths, 'demo') is not None

    moved = tmp_path / 'restored-elsewhere'
    monkeypatch.chdir(tmp_path)
    source.rename(moved)
    monkeypatch.chdir(moved)

    def _confirm(_prompt: str) -> str:
        return 'y'

    monkeypatch.setattr('builtins.input', _confirm)
    assert _cli.main(['init', '--project', 'demo', '--relocate']) == 0
    assert _config.load_project(paths, 'demo').source_path == moved.resolve()

    assert _cli.main(['publish', '--project', 'demo', '--yes']) == 1
    assert 'the configuration changed after the snapshot was prepared' in capsys.readouterr().err

    assert _cli.main(['discard', '--project', 'demo']) == 0
    assert _cli.main(['prepare', '--project', 'demo', '--acknowledge-new-paths', '--yes']) == 0
    record = _records.load_candidate(paths, 'demo')
    assert record is not None
    assert record.fingerprint == _config.fingerprint(
        _config.load_global(paths), _config.load_project(paths, 'demo')
    )


@pytest.mark.parametrize('source_tags', [False, True])
def test_resumed_route_and_source_tags_choices_survive_global_default_change(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    source_tags: bool,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, _ = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    first = {
        'Use this selection? [c]ontinue / [r]eview / [b]ack / [q]uit: ': 'c',
        'GitHub owner [anonymous-account]: ': '',
        'GitHub repository [demo]: ': '',
        'Repository visibility (private/public) [private]: ': '',
        'Repository description (blank for none): ': '',
        'Enable issues? [Y/n] ': '',
        'Enable pull requests? [y/N] ': '',
        'Enable discussions? [y/N] ': '',
        'Enable projects? [y/N] ': '',
        'Enable wiki? [y/N] ': '',
        'Publication route (staged/direct) [staged]: ': '',
        'Required CI checks (comma separated, blank for none): ': '',
        'Create a release page for each published version? [y/N] ': '',
        'Create local tags marking the source of each publication? [Y/n] ': 'y'
        if source_tags
        else 'n',
    }

    def _interrupt(prompt: str) -> str:
        if prompt == 'Save this configuration? [s]ave / [e]dit / [c]ancel: ':
            raise EOFError
        return first[prompt]

    monkeypatch.setattr('builtins.input', _interrupt)
    assert _cli.main(['init', '--project', 'demo']) == 1
    draft = _init_draft.load(paths, 'demo')
    assert draft is not None
    assert draft.publication_route == 'staged'
    assert draft.source_tags is source_tags
    _config.save_global(
        paths,
        replace(global_config, default_publication_route='direct'),
        paths.global_config.read_bytes(),
    )
    remaining = {
        'Saved setup: [r]esume / [s]tart over / [q]uit: ': 'r',
        'Save this configuration? [s]ave / [e]dit / [c]ancel: ': 's',
    }

    def _resume(prompt: str) -> str:
        if prompt not in remaining:
            pytest.fail(f'unexpected resume prompt: {prompt}')
        return remaining[prompt]

    monkeypatch.setattr('builtins.input', _resume)
    assert _cli.main(['init', '--project', 'demo']) == 0
    assert _config.load_project(paths, 'demo').publication_route == 'staged'
    assert _config.load_project(paths, 'demo').source_tags is source_tags
