import json
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
from pubanon._snapshots import _candidate, _source
from pubanon._terminal import escape
from tests._git import git_env
from tests._setup import configure

_Script = Callable[[Sequence[tuple[str, str]]], None]


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
    assert '  .pubanon.toml - Pubanon configuration (always excluded)\n' in excluded
    assert '  keys/id_rsa\n\n  Exclusions override included directories.\n' in excluded
    assert 'you chose to exclude' not in excluded


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
        assert 'sample:' not in output
        assert 'Exclude rules' not in output
        assert 'precedence' not in output.casefold()


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

    monkeypatch.setattr('builtins.input', _answer)
    assert _cli.main(['init', '--project', 'demo']) == 0
    output = capsys.readouterr().out
    assert output
    assert not output.startswith('\n')
    project = _config.load_project(paths, 'demo')
    assert project.allow == ('README.md', '.env')
    assert project.deny == ()
    assert project.secret_exceptions == ('.env',)


def test_init_collects_complete_repository_settings_for_new_project(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    prompts: list[str] = []
    answers = {
        'Use this selection? [c]ontinue / [r]eview / [b]ack / [q]uit: ': 'c',
        'GitHub owner [anonymous-account]: ': '',
        'GitHub repository [demo]: ': '',
        'Repository visibility (private/public) [private]: ': '',
        'Required CI checks (comma separated, blank for none): ': '',
        'Repository description (blank for none): ': 'Example project',
        'Enable issues? [Y/n] ': '',
        'Enable wiki? [y/N] ': '',
        'Enable projects? [y/N] ': 'yes',
        'Save this configuration? [s]ave / [e]dit / [c]ancel: ': 's',
    }

    def _answer(prompt: str) -> str:
        prompts.append(prompt)
        if prompt in answers:
            return answers[prompt]
        pytest.fail(f'unexpected init prompt: {prompt}')

    monkeypatch.setattr('builtins.input', _answer)

    assert _cli.main(['init', '--project', 'demo']) == 0

    project = _config.load_project(paths, 'demo')
    assert project.settings == _config.Settings(
        description='Example project',
        default_branch='main',
        issues=True,
        wiki=False,
        projects=True,
    )
    text = (paths.config_dir / 'projects' / 'demo.toml').read_text()
    assert '[settings]\ndescription = "Example project"\n' in text
    assert prompts.index('Required CI checks (comma separated, blank for none): ') < prompts.index(
        'Repository description (blank for none): '
    )


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

    monkeypatch.setattr('builtins.input', _answer)

    assert _cli.main(['init', *verbosity_args, '--project', 'demo']) == 0

    output = ''.join((*before_action, capsys.readouterr().out))
    assert escape(str(complete_list)) in before_action[0]
    assert 'Project source' in before_action[0]
    assert escape(str(source.resolve())) in before_action[0]
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
    for removed_heading in (
        'Included paths:',
        'Always excluded:',
        'Flagged for individual review (credential-like):',
        'Proposed files:',
    ):
        assert removed_heading not in output
    _assert_selection_rule_details(output, verbose=bool(verbosity_args))
    assert (
        'Include these files? [i]nclude / [e]xclude / [c]ustomize / [b]ack / [q]uit: '
    ) not in prompts
    assert not any('Pubanon repository' in prompt for prompt in prompts)
    project = _config.load_project(paths, 'demo')
    assert project.allow == ('README.md', 'docs/guide.md', 'src/**', '.env')
    assert project.deny == ('keys/id_rsa',)
    assert project.secret_exceptions == ('.env',)
    assert project.owner == 'anonymous-account'
    assert project.owner != source.name
    assert project.repo == 'demo'
    assert project.visibility == 'private'
    assert project.self_publication is False


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
        '[accounts]\n'
        'github = "anonymous-account"\n'
    )
    answers = iter(['c', '', '', '', '', '', '', '', '', 's'])

    def _answer(_prompt: str) -> str:
        return next(answers)

    monkeypatch.setattr('builtins.input', _answer)
    monkeypatch.chdir(source)

    assert _cli.main(['init', '--project', 'demo']) == 0

    output = capsys.readouterr().out
    assert 'private-name' in output
    assert f'{"private-name"}' in output


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

    monkeypatch.setattr('builtins.input', _answer)
    assert _cli.main(['init', '--project', 'demo']) == 0
    assert _config.load_project(paths, 'demo').required_checks == ()
    assert not any(prompt.startswith('Require checks:') for prompt in prompts)
    assert 'GitHub checks found' not in capsys.readouterr().out


def test_init_final_review_revises_resolved_policy_membership(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(
        files={
            '.env': 'fixture\n',
            'docs/guide.md': 'guide\n',
            'docs/internal/notes.md': 'internal\n',
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

    monkeypatch.setattr('builtins.input', _answer)

    assert _cli.main(['init', '--project', 'demo']) == 0

    project = _config.load_project(paths, 'demo')
    membership = _policy.evaluate_membership(
        ['docs/guide.md', 'docs/internal/notes.md'],
        project.allow,
        project.deny,
    )
    assert membership.allowed == ['docs/guide.md']
    assert membership.denied == ['docs/internal/notes.md']
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

    monkeypatch.setattr('builtins.input', _answer)
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
            pytest.raises(PubanonError, match='active operation'),
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

    monkeypatch.setattr('builtins.input', _reply)
    assert _cli.main(['init', '--project', 'demo']) == 0
    assert _config.load_project(paths, 'demo').required_checks == (
        'quality',
        'tests (ubuntu-24.04, 3.13)',
        'tests (macos-15, 3.13)',
    )
    assert 'Require checks: [a]ll listed / [s]elect / [n]one / [m]anual: ' in prompts
    output = capsys.readouterr().out
    assert '1. quality' in output
    assert '2. tests (ubuntu-24.04, 3.13)' in output
    assert 'not_committed' not in output


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
    error = capsys.readouterr().err
    assert 'fact: project configuration was not saved; setup draft was saved' in error
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
        assert preview.endswith('\n\n')
        return 'q'

    monkeypatch.setattr('builtins.input', _quit_selection)
    assert _cli.main(['init', '--project', 'styled']) == 130
    assert not (paths.config_dir / 'projects' / 'styled.toml').exists()
    retained = paths.data_dir / 'projects' / 'styled' / 'proposed-selection.json'
    assert json.loads(retained.read_text()) == ['README.md', 'src/a.py']
    error = capsys.readouterr().err
    assert '\x1b[33mcancelled: operation cancelled\x1b[0m' in error
    assert 'project configuration was not saved; setup draft was saved' in error


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

    answers = iter(['yes', 'yes', '', '', '', '', 's'])

    def _confirm(_prompt: str) -> str:
        return next(answers)

    monkeypatch.setattr('builtins.input', _confirm)
    assert _cli.main(['init', '--project', 'demo', '--relocate']) == 0
    assert _config.load_project(paths, 'demo').source_path == source.resolve()
    assert _config.registry_lookup(paths, source) == 'demo'


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

    answers = iter([('Move this project to the new source path? [y/n] ', 'yes')])

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
    assert 'Next: pubanon status --project=demo' in output
    assert escape(str(paths.config_dir / 'projects' / 'demo.toml')) in output


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
    )
    _config.save_project(paths, replace(project, settings=settings))
    monkeypatch.chdir(source)

    def _unexpected(prompt: str) -> str:
        pytest.fail(f'init prompted although settings exist: {prompt}')

    monkeypatch.setattr('builtins.input', _unexpected)

    assert _cli.main(['init']) == 0
    assert _config.load_project(paths, 'demo').settings == settings


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

    monkeypatch.setattr('builtins.input', _answer)
    monkeypatch.setattr(_config, 'save_project', _save_after_reservation)
    monkeypatch.chdir(source)

    assert _cli.main(['init', '--project', 'demo']) == 0
    assert reservation_seen == [True]
    output = capsys.readouterr().out
    assert 'Configured and registered project demo.' in output
    assert 'Next: pubanon status --project=demo' in output
    assert escape(str(paths.config_dir / 'projects' / 'demo.toml')) in output


def test_init_resume_can_back_up_a_group_choice(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(
        files={
            'README.md': 'public\n',
            'docs/internal/note.md': 'local\n',
            'docs/superpowers/plan.md': 'plan\n',
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

    monkeypatch.setattr('builtins.input', _interrupt)
    assert _cli.main(['init', '--project', 'demo']) == 130
    resumed = iter(['r', 'b', 'i', 'e', 'c', '', '', '', '', '', '', '', '', 's'])

    def _resume(_prompt: str) -> str:
        return next(resumed)

    monkeypatch.setattr('builtins.input', _resume)
    assert _cli.main(['init', '--project', 'demo']) == 0
    project = _config.load_project(paths, 'demo')
    assert project.allow == ('README.md', 'docs/internal/**')
    assert project.deny == ('docs/superpowers/**',)


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

    monkeypatch.setattr('builtins.input', _reply)
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
    first = iter(['c', '', '', '', 'Lint', 'Description', 'n'])

    def _interrupt(prompt: str) -> str:
        if prompt == 'Enable wiki? [y/N] ':
            raise KeyboardInterrupt
        return next(first)

    monkeypatch.setattr('builtins.input', _interrupt)
    assert _cli.main(['init', '--project', 'demo']) == 130
    replies = iter(['r', 'y', 'n', 's'])
    prompts: list[str] = []

    def _resume(prompt: str) -> str:
        prompts.append(prompt)
        return next(replies)

    monkeypatch.setattr('builtins.input', _resume)
    assert _cli.main(['init', '--project', 'demo']) == 0
    assert prompts[1] == 'Enable wiki? [y/N] '
    project = _config.load_project(paths, 'demo')
    assert project.required_checks == ('Lint',)
    assert project.settings == _config.Settings(
        'Description', 'main', issues=False, wiki=True, projects=False
    )


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
    source = make_repo(files={'README.md': 'public\n', 'docs/internal/note.md': 'local\n'})
    paths, _global, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    first = iter(['r', 'i', 'e', 'c', 'publication-owner'])

    def _interrupt(prompt: str) -> str:
        if prompt == 'GitHub repository [demo]: ':
            raise failure
        return next(first)

    monkeypatch.setattr('builtins.input', _interrupt)
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

    monkeypatch.setattr('builtins.input', _resume)
    resumed_args = ['init', '--project', 'demo']
    if enable_on_resume:
        resumed_args.append('--self-publication')
    assert _cli.main(resumed_args) == 0
    assert prompts[0] == 'Saved setup: [r]esume / [s]tart over / [q]uit: '
    assert prompts[1] == 'GitHub repository [demo]: '
    project = _config.load_project(paths, 'demo')
    assert project.owner == 'publication-owner'
    assert project.repo == 'published-demo'
    assert project.deny == ('docs/internal/**',)
    assert project.self_publication is True
    assert not (paths.data_dir / 'projects' / 'demo' / 'init-draft.json').exists()


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
        ('Required CI checks (comma separated, blank for none): ', ''),
        ('Repository description (blank for none): ', 'Work by Private Person'),
        ('Enable issues? [Y/n] ', 'n'),
        ('Enable wiki? [y/N] ', 'y'),
        ('Enable projects? [y/N] ', 'y'),
        ('Edit: [d]estination / [v]isibility / [p]olicy / [c]hecks / [s]ettings / [b]ack: ', 's'),
        ('Description: [k]eep / [r]eplace / [c]lear [keep]: ', 'replace'),
        ('Repository description (blank for none): ', 'Published demo'),
        ('Enable issues? [y/N] ', ''),
        ('Enable wiki? [Y/n] ', ''),
        ('Enable projects? [Y/n] ', ''),
        ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
    ]

    def _answer(prompt: str) -> str:
        assert answers, f'unexpected prompt: {prompt}'
        expected_prompt, response = answers.pop(0)
        assert prompt == expected_prompt
        return response

    monkeypatch.setattr('builtins.input', _answer)

    assert _cli.main(['init', '--project', 'demo']) == 0
    assert answers == []

    output = capsys.readouterr().out
    diagnostics, current_value = output.split('Current description: ', 1)
    assert 'repository description failed content gates' in diagnostics
    assert 'Private Person' in diagnostics
    assert current_value.startswith('Work by Private Person\n')
    assert _config.load_project(paths, 'demo').settings == _config.Settings(
        description='Published demo',
        default_branch='main',
        issues=False,
        wiki=True,
        projects=True,
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
            'Test, Lint',
            'Published demo',
            'y',
            'n',
            'no',
            's',
        ]
    )

    def _answer(_prompt: str) -> str:
        return next(answers)

    monkeypatch.setattr('builtins.input', _answer)

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
    )
    output = capsys.readouterr().out
    assert 'Expected visibility must be private or public.' in output
    assert 'Publication uses the main branch.' in output


@pytest.mark.parametrize(
    'case',
    [
        ('clear', None, ''),
        ('', None, 'Published demo'),
        ('replace', 'New description', 'New description'),
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
        ('Required CI checks (comma separated, blank for none): ', ''),
        ('Repository description (blank for none): ', 'Published demo'),
        ('Enable issues? [Y/n] ', ''),
        ('Enable wiki? [y/N] ', ''),
        ('Enable projects? [y/N] ', ''),
        ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 'e'),
        ('Edit: [d]estination / [v]isibility / [p]olicy / [c]hecks / [s]ettings / [b]ack: ', 's'),
        ('Description: [k]eep / [r]eplace / [c]lear [keep]: ', action),
        *(
            []
            if replacement is None
            else [('Repository description (blank for none): ', replacement)]
        ),
        ('Enable issues? [Y/n] ', ''),
        ('Enable wiki? [y/N] ', ''),
        ('Enable projects? [y/N] ', ''),
        ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
    ]

    def _answer(prompt: str) -> str:
        assert answers, f'unexpected prompt: {prompt}'
        expected_prompt, response = answers.pop(0)
        assert prompt == expected_prompt
        return response

    monkeypatch.setattr('builtins.input', _answer)

    assert _cli.main(['init', '--project', 'demo']) == 0
    assert answers == []
    project = _config.load_project(paths, 'demo')
    assert project.settings is not None
    assert project.settings.description == expected
    assert f'Description: {expected or "(empty)"}' in capsys.readouterr().out


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

    monkeypatch.setattr('builtins.input', _answer)

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
    assert output.index('Publish list saved to:') < output.index('Files to include or exclude:')
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
            '',
            'Published demo',
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

    monkeypatch.setattr('builtins.input', _answer)

    assert _cli.main(['init', '--project', 'demo']) == 130

    assert (
        'fact: project configuration was not saved; setup draft was saved'
        in capsys.readouterr().err
    )
    review = reviews[0]
    for expected in (
        escape(str(source.resolve())),
        'Destination: anonymous-account/demo',
        'Visibility: private',
        'Selection scope: 1 included; 0 excluded',
        'Required checks: none',
        'Description: Published demo',
        'Publication uses the main branch.',
        'Issues: yes',
        'Wiki: no',
        'Projects: no',
    ):
        assert expected in review
    assert not (paths.config_dir / 'projects' / 'demo.toml').exists()
    assert not paths.registry.exists()


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
            ),
        ),
    )
    monkeypatch.chdir(source)

    def _unexpected_install(_paths: _config.Paths, _project: _config.ProjectConfig) -> None:
        pytest.fail('same-source init used the installation visibility boundary')

    monkeypatch.setattr(_config, 'install_project', _unexpected_install)

    assert _cli.main(['init', '--project', project.name]) == 0
    output = capsys.readouterr().out
    assert 'Project demo is already configured and registered.' in output
    assert 'Next: pubanon status' in output
    assert escape(str(paths.config_dir / 'projects' / 'demo.toml')) in output


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
            ('Required CI checks (comma separated, blank for none): ', ''),
            ('Repository description (blank for none): ', ''),
            ('Enable issues? [Y/n] ', ''),
            ('Enable wiki? [y/N] ', ''),
            ('Enable projects? [y/N] ', ''),
            ('Save this configuration? [s]ave / [e]dit / [c]ancel: ', 's'),
        ]
    )
    assert _cli.main(['init', '--self-publication']) == 0
    assert _config.load_project(paths, 'pubanon').self_publication is True


def test_init_source_change_rechecks_selection_without_losing_exclusions(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n', 'docs/internal/note.md': 'local\n'})
    paths, _global, _project = configure(tmp_path, monkeypatch)
    monkeypatch.chdir(source)
    answers = iter(['r', 'i', 'e', 'c', 'publication-owner'])

    def _reply(_prompt: str) -> str:
        answer = next(answers, None)
        if answer is None:
            raise KeyboardInterrupt
        return answer

    monkeypatch.setattr('builtins.input', _reply)
    assert _cli.main(['init', '--project', 'demo']) == 130
    (source / 'docs/internal/new.md').write_text('also local\n')
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
    assert project.deny == ('docs/internal/**',)
    assert 'The source changed. Review the updated selection' in capsys.readouterr().out
    preview = paths.data_dir / 'projects' / 'demo' / 'proposed-selection.json'
    assert json.loads(preview.read_text()) == ['README.md']


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

    monkeypatch.setattr('builtins.input', _reply)
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
    assert escape(str(paths.config_dir / 'projects' / 'demo.toml')) in error
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
            ),
        ),
    )
    monkeypatch.chdir(source)
    assert _cli.main(['prepare', '--project', 'demo', '--acknowledge-new-paths']) == 0
    assert _candidate.load_candidate(paths, 'demo') is not None

    moved = tmp_path / 'restored-elsewhere'
    monkeypatch.chdir(tmp_path)
    source.rename(moved)
    monkeypatch.chdir(moved)

    def _confirm(_prompt: str) -> str:
        return 'yes'

    monkeypatch.setattr('builtins.input', _confirm)
    assert _cli.main(['init', '--project', 'demo', '--relocate']) == 0
    assert _config.load_project(paths, 'demo').source_path == moved.resolve()

    assert _cli.main(['publish', '--project', 'demo', '--yes']) == 1
    assert 'publication configuration changed after prepare' in capsys.readouterr().err

    assert _cli.main(['discard', '--project', 'demo']) == 0
    assert _cli.main(['prepare', '--project', 'demo', '--acknowledge-new-paths']) == 0
    record = _candidate.load_candidate(paths, 'demo')
    assert record is not None
    assert record.fingerprint == _config.fingerprint(
        _config.load_global(paths), _config.load_project(paths, 'demo')
    )
