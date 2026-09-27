import os
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from pubanon import (
    _cli,
    _config,
)
from pubanon._cli import _arguments, _prepare, _selection, _status
from pubanon._cli._context import ProjectContext, load_context
from pubanon._cli._prepare import PrepareOptions, plan_prepare_locked, prepare_locked
from pubanon._errors import PubanonError
from pubanon._run import run_git
from pubanon._snapshots import _candidate, _records, _source
from pubanon._terminal import escape
from tests._cli import prepare
from tests._git import git_env
from tests._setup import configure


def replacement_context(
    tmp_path: Path,
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    conflict: bool = False,
) -> tuple[_config.Paths, _config.ProjectConfig, _candidate.CandidateRecord]:
    paths, project, original = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    store = _candidate.candidate_store_path(paths, project.name)
    for ref in ('refs/heads/main', 'refs/tags/v1.0.0'):
        subprocess.run(
            ['git', '-C', store, 'update-ref', ref, original.commit], check=True, env=git_env()
        )
    _candidate.discard(paths, project.name, force=False)
    changed = source / ('README.md' if conflict else 'second.txt')
    changed.write_bytes(b'descendant\n')
    subprocess.run(['git', '-C', source, 'add', '.'], check=True, env=git_env())
    subprocess.run(['git', '-C', source, 'commit', '-qm', 'Descendant'], check=True, env=git_env())
    assert (
        _cli.main(
            [
                'prepare',
                '--project',
                project.name,
                '--version',
                '1.0.1',
                '--acknowledge-new-paths',
                '--yes',
            ]
        )
        == 0
    )
    descendant = _candidate.load_candidate(paths, project.name)
    assert descendant is not None
    for ref in ('refs/heads/main', 'refs/tags/v1.0.1'):
        subprocess.run(
            ['git', '-C', store, 'update-ref', ref, descendant.commit], check=True, env=git_env()
        )
    _candidate.discard(paths, project.name, force=False)
    if not conflict:
        changed.unlink()
    (source / 'README.md').write_text('correction\n', newline='\n')
    subprocess.run(['git', '-C', source, 'add', '.'], check=True, env=git_env())
    subprocess.run(['git', '-C', source, 'commit', '-qm', 'Correction'], check=True, env=git_env())
    return paths, project, original


@pytest.mark.parametrize('name', ['my project', '-leading'])
def test_incomplete_prepare_recovery_selects_project(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    name: str,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    _paths, _global_config, project = configure(tmp_path, monkeypatch, source, project=name)
    assert project is not None
    record = SimpleNamespace(replaces='a' * 40, tip=None, tag_intent='v1.0.0')

    def _paused(*_args: object) -> SimpleNamespace:
        return record

    def _empty_status(*_args: object, **_kwargs: object) -> list[str]:
        return []

    monkeypatch.setattr(_prepare, 'prepare_locked', _paused)
    monkeypatch.setattr(_prepare, '_continue_rewrite', _paused)
    monkeypatch.setattr(_status, 'rewrite_status', _empty_status)
    assert _cli.main(['prepare', f'--project={name}']) == 0
    output = capsys.readouterr().out
    command = output.split('Next: ', 1)[1].strip()
    argv = shlex.split(command)
    assert _arguments.build_parser().parse_args(argv[1:]).project == name
    assert argv == ['pubanon', 'prepare', f'--project={name}', '--continue']


def test_prepare_excludes_denied_credential_path_with_metacharacters(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = 'keys/id[old]_rsa'
    source = make_repo(files={path: 'private key fixture\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    _config.save_project(paths, replace(project, allow=('keys/**',)))
    monkeypatch.chdir(source)

    decisions = iter(['e', 'c'])

    def _deny(_prompt: str) -> str:
        return next(decisions)

    monkeypatch.setattr('builtins.input', _deny)

    assert _cli.main(['prepare', '--project', project.name, '--acknowledge-new-paths']) == 0

    saved = _config.load_project(paths, project.name)
    assert saved.deny == ('keys/id[[]old]_rsa',)
    assert saved.secret_exceptions == ()
    record = _candidate.load_candidate(paths, project.name)
    assert record is not None
    snapshot = _source.read_snapshot(_candidate.store_path(paths, project.name), record.commit)
    assert [entry.path for entry in snapshot] == []


@pytest.mark.parametrize('unknown', [False, True])
def test_prepare_existing_candidate_preserves_the_appropriate_choice(
    unknown: bool,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    if unknown:
        _candidate.save_candidate(
            paths, project.name, replace(record, state=_candidate.STATE_UNKNOWN_OUTCOME)
        )
    capsys.readouterr()
    assert _cli.main(['prepare', '--project', project.name]) == 1
    error = capsys.readouterr().err
    if unknown:
        assert 'pubanon publish --project=demo --resolve' in error
        assert 'discard' not in error
    else:
        assert 'v1.0.0' in error
        assert 'pubanon inspect --project=demo' in error
        assert 'pubanon publish --project=demo' in error
        assert 'to replace it:' in error
    assert _candidate.load_candidate(paths, project.name) is not None


def test_prepare_gates_current_publication_coordinates_before_candidate_build(
    tmp_path: Path,
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    capsys: pytest.CaptureFixture[str],
) -> None:
    term = 'private-coordinate'
    paths, _global_config, project = configured_project
    assert project is not None
    project = replace(project, repo=f'public-{term}', extra_terms=(term,))
    _config.save_project(paths, project)

    assert (
        _cli.main(['prepare', '--project', project.name, '--acknowledge-new-paths', '--yes']) == 1
    )

    rendered = capsys.readouterr().err
    assert 'repository name did not pass content checks' in rendered
    assert term in rendered
    assert _candidate.load_candidate(paths, project.name) is None
    assert not (tmp_path / 'fake-gh-calls.jsonl').exists()


def test_prepare_gates_default_staging_coordinates_before_candidate_build(
    tmp_path: Path,
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    capsys: pytest.CaptureFixture[str],
) -> None:
    term = 'default-stage'
    paths, _global_config, project = configured_project
    assert project is not None
    project = replace(
        project,
        repo='public-default',
        hygiene_terms=(term,),
    )
    _config.save_project(paths, project)

    assert (
        _cli.main(['prepare', '--project', project.name, '--acknowledge-new-paths', '--yes']) == 1
    )

    rendered = capsys.readouterr().err
    assert 'repository name did not pass content checks' in rendered
    assert term in rendered
    assert _candidate.load_candidate(paths, project.name) is None
    assert not (tmp_path / 'fake-gh-calls.jsonl').exists()


def test_prepare_gates_explicit_staging_coordinates_before_candidate_build(
    tmp_path: Path,
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    capsys: pytest.CaptureFixture[str],
) -> None:
    term = 'staging-hygiene-canary'
    paths, _global_config, project = configured_project
    assert project is not None
    project = replace(
        project,
        _stage_repo=f'public-{term}',
        hygiene_terms=(term,),
    )
    _config.save_project(paths, project)

    assert (
        _cli.main(['prepare', '--project', project.name, '--acknowledge-new-paths', '--yes']) == 1
    )

    rendered = capsys.readouterr().err
    assert 'repository name did not pass content checks' in rendered
    assert term in rendered
    assert _candidate.load_candidate(paths, project.name) is None
    assert not (tmp_path / 'fake-gh-calls.jsonl').exists()


def test_prepare_in_fresh_process_stays_offline(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    configure(tmp_path, monkeypatch, source)
    env = git_env(
        {
            'PYTHONPATH': str(Path(__file__).parents[2] / 'src'),
        }
    )
    script = """
import os
import socket
import subprocess
from pathlib import Path

original_run = subprocess.run
original_socket = socket.socket


def guarded_run(args, *pargs, **kwargs):
    command = [os.fspath(part) for part in args]
    forbidden_git_operations = {'fetch', 'ls-remote', 'push'}
    if (
        Path(command[0]).name == 'gh'
        or forbidden_git_operations.intersection(command)
        or any('credential' in part or 'github.com' in part for part in command)
    ):
        raise AssertionError(f'offline prepare attempted host command: {command!r}')
    return original_run(args, *pargs, **kwargs)


class OfflineSocket(original_socket):
    def connect(self, address):
        raise AssertionError(f'offline prepare attempted socket connection: {address!r}')

    def connect_ex(self, address):
        raise AssertionError(f'offline prepare attempted socket connection: {address!r}')


subprocess.run = guarded_run
socket.socket = OfflineSocket

from pubanon._cli import main

raise SystemExit(main(['prepare', '--acknowledge-new-paths', '--yes']))
"""

    completed = subprocess.run(
        [sys.executable, '-c', script],
        cwd=source,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert not (tmp_path / 'fake-gh-calls.jsonl').exists()


def test_prepare_keeps_long_source_excerpts_readable_and_escapes_controls(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    terms = ('private-alpha', 'private-beta')
    source = make_repo(
        files={
            'README.md': 'prefix ' * 100 + 'private-alpha\x1b[2J private-beta' + ' suffix' * 100,
        }
    )
    paths, config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    _config.save_global(
        paths, replace(config, sensitive_terms=terms), paths.global_config.read_bytes()
    )
    assert (
        _cli.main(['prepare', '--project', project.name, '--yes', '--acknowledge-new-paths']) == 1
    )
    error = capsys.readouterr().err
    assert error.count('    README.md:1\n') == 1
    excerpt = next(
        line for line in error.splitlines() if 'private-alpha\\x1b[2J private-beta' in line
    )
    assert excerpt.strip().startswith('...')
    assert excerpt.endswith('...')
    assert len(excerpt) < 200
    assert '\x1b' not in error


@pytest.mark.slow
def test_prepare_latest_refuses_bad_untouched_ancestor_before_reporting_ready(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    paths, project, original = replacement_context(tmp_path, source, monkeypatch)
    store = _candidate.candidate_store_path(paths, project.name)
    raw = run_git(['cat-file', 'commit', original.commit], cwd=store).stdout
    raw = raw.replace(b'\nauthor ', b'\ngpgsig invalid-signature\nauthor ')
    bad = (
        run_git(
            ['hash-object', '--literally', '-t', 'commit', '-w', '--stdin'],
            cwd=store,
            input_bytes=raw,
        )
        .stdout.strip()
        .decode()
    )
    raw = run_git(['cat-file', 'commit', 'refs/heads/main'], cwd=store).stdout
    raw = raw.replace(original.commit.encode(), bad.encode())
    selected = (
        run_git(
            ['hash-object', '--literally', '-t', 'commit', '-w', '--stdin'],
            cwd=store,
            input_bytes=raw,
        )
        .stdout.strip()
        .decode()
    )
    for ref, oid in (
        ('refs/heads/main', selected),
        ('refs/tags/v1.0.0', bad),
        ('refs/tags/v1.0.1', selected),
    ):
        run_git(['update-ref', ref, oid], cwd=store)
    capsys.readouterr()
    assert _cli.main(['prepare', '--project', project.name, '--replace', 'latest', '--yes']) == 1
    captured = capsys.readouterr()
    assert 'history has identity problems' in captured.err
    assert 'Rewrite ready' not in captured.out
    assert 'pubanon inspect' not in captured.out
    assert not (tmp_path / 'fake-gh-calls.jsonl').exists()


def test_prepare_refuses_already_published_tag_early(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    store = _candidate.candidate_store_path(paths, project.name)
    for ref in ('refs/tags/v1.0.0', 'refs/heads/main'):
        subprocess.run(
            ['git', '-C', store, 'update-ref', ref, record.commit], check=True, env=git_env()
        )
    _candidate.discard(paths, project.name, force=True)
    ctx = load_context(paths, project.name)

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError('snapshot must not be read before tag refusal')

    monkeypatch.setattr(_source, 'read_snapshot', _forbidden)
    options = PrepareOptions('HEAD', '1.0.0', None, acknowledge_new_paths=False, yes=False)
    with pytest.raises(PubanonError, match='already published') as exc_info:
        plan_prepare_locked(ctx, options)
    assert 'choose a new version' in (exc_info.value.recovery or '')
    assert 'pubanon prepare --project=demo --replace v1.0.0' in (exc_info.value.recovery or '')


def test_prepare_refuses_intact_utf8_term_in_mixed_blob(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answers: Callable[[Sequence[tuple[str, str]]], None],
) -> None:
    term = 'Privat\u00e9Alias'
    source = make_repo(files={'mixed.bin': b'\xffPrivat\xc3\xa9Alias\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    _config.save_project(paths, replace(project, extra_terms=(term,)))

    answers([])
    assert (
        _cli.main(['prepare', '--project', project.name, '--acknowledge-new-paths', '--yes']) == 1
    )

    rendered = capsys.readouterr().err
    assert 'cannot prepare: blocked text in included files' in rendered
    assert term in rendered
    assert _candidate.load_candidate(paths, project.name) is None


@pytest.mark.slow
def test_prepare_refuses_reusing_inferred_package_version(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'pyproject.toml': '[project]\nversion = "0.0.1"\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    store = _candidate.candidate_store_path(paths, project.name)
    for ref in ('refs/tags/v0.0.1', 'refs/heads/main'):
        subprocess.run(
            ['git', '-C', store, 'update-ref', ref, record.commit], check=True, env=git_env()
        )
    _candidate.discard(paths, project.name, force=True)
    assert _cli.main(['prepare', '--project', project.name, '--yes']) != 0
    assert _candidate.load_candidate(paths, project.name) is None
    error = capsys.readouterr().err
    assert 'already published' in error
    assert 'v0.0.1' in error
    assert 'update and commit the package version' in error


def test_prepare_refuses_sensitive_term_in_path(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answers: Callable[[Sequence[tuple[str, str]]], None],
) -> None:
    source = make_repo(files={'docs/Private Person.txt': 'public\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None

    answers([])
    assert (
        _cli.main(['prepare', '--project', project.name, '--acknowledge-new-paths', '--yes']) == 1
    )

    assert 'cannot prepare: blocked text in included files' in capsys.readouterr().err
    assert _candidate.load_candidate(paths, project.name) is None


@pytest.mark.slow
def test_prepare_replacement_refuses_inferred_version_outside_moved_tags(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'pyproject.toml': '[project]\nversion = "1.0.0"\n'})
    paths, project, original = prepare(tmp_path, source, monkeypatch)
    store = _candidate.candidate_store_path(paths, project.name)
    for ref in ('refs/heads/main', 'refs/tags/v1.0.0'):
        subprocess.run(
            ['git', '-C', store, 'update-ref', ref, original.commit], check=True, env=git_env()
        )
    _candidate.discard(paths, project.name, force=False)
    (source / 'pyproject.toml').write_text('[project]\nversion = "2.0.0"\n')
    subprocess.run(['git', '-C', source, 'add', '.'], check=True, env=git_env())
    subprocess.run(
        ['git', '-C', source, 'commit', '-qm', 'New version'], check=True, env=git_env()
    )
    capsys.readouterr()
    assert _cli.main(['prepare', '--project', project.name, '--replace', 'latest', '--yes']) == 1
    error = capsys.readouterr().err
    assert 'replacement version must match a moved tag' in error
    assert 'commit the version correction or publish a new version' in error
    assert _candidate.load_candidate(paths, project.name) is None


@pytest.mark.slow
@pytest.mark.parametrize('selector', ['v1.0.0', 'all'])
def test_prepare_replacement_replays_or_replaces_all_from_current_source(
    selector: str,
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    paths, project, original = replacement_context(tmp_path, source, monkeypatch)
    capsys.readouterr()
    assert (
        _cli.main(
            [
                'prepare',
                '--project',
                project.name,
                '--replace',
                selector,
                '--version',
                '1.0.0',
                '--yes',
            ]
        )
        == 0
    )
    record = _candidate.load_candidate(paths, project.name)
    assert record is not None
    assert record.tip is not None
    store = _candidate.candidate_store_path(paths, project.name)
    snapshot = _source.read_snapshot(store, record.tip)
    files = {entry.path: entry.data for entry in snapshot}
    assert files['README.md'] == b'correction\n'
    assert record.pinned.commit == _source.pin(source, 'HEAD').commit
    if selector == 'all':
        effect = 'Replace all published history and tags with one new initial publication.'
        assert record.replace_scope == 'all'
        assert record.replayed == ()
        assert record.poststate_tags == {'v1.0.0': record.commit}
        assert 'second.txt' not in files
    else:
        effect = (
            'Replace the selected publication and rebuild later publications; '
            'preserve earlier history.'
        )
        assert record.replaces == original.commit
        assert len(record.replayed) == 1
        assert files['second.txt'] == b'descendant\n'
        assert record.poststate_tags == {'v1.0.0': record.commit, 'v1.0.1': record.tip}
    captured = capsys.readouterr()
    assert 'Prepared snapshot' in captured.out
    assert 'Operation:' not in captured.out
    assert effect in captured.out
    assert 'Tags to move:' not in captured.out
    assert captured.out.count('Next:') == 1
    assert not (tmp_path / 'fake-gh-calls.jsonl').exists()
    assert _cli.main(['prepare', '--project', project.name, '--continue']) == 1
    assert 'no rewrite is in progress' in capsys.readouterr().err


@pytest.mark.slow
@pytest.mark.parametrize('selector', [(), ('latest',)])
def test_prepare_replace_latest_preserves_earlier_publications(
    selector: tuple[str, ...],
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    paths, project, original = replacement_context(tmp_path, source, monkeypatch)
    store = _candidate.candidate_store_path(paths, project.name)
    previous_tip = run_git(['rev-parse', 'refs/heads/main'], cwd=store).stdout.decode().strip()
    capsys.readouterr()
    assert _cli.main(['prepare', '--project', project.name, '--yes', '--replace', *selector]) == 0
    record = _candidate.load_candidate(paths, project.name)
    assert record is not None
    assert record.replaces == previous_tip
    assert record.tip == record.commit
    assert record.replayed == ()
    assert record.poststate_tags == {'v1.0.0': original.commit, 'v1.0.1': record.commit}
    parent = run_git(['rev-parse', f'{record.commit}^'], cwd=store).stdout.decode().strip()
    assert parent == original.commit
    files = {entry.path: entry.data for entry in _source.read_snapshot(store, record.commit)}
    assert files['README.md'] == b'correction\n'
    assert 'Replace the latest publication; preserve earlier history.' in capsys.readouterr().out
    assert not (tmp_path / 'fake-gh-calls.jsonl').exists()


@pytest.mark.slow
@pytest.mark.parametrize(
    'case',
    [
        (('latest',), None, None, 0),
        (('v1.2.3',), None, 'v1.2.3', 0),
        (('v1.2.3', 'latest'), None, None, 0),
        (('v1.0.0',), None, 'v1.0.0', 0),
        (('v1.0.0',), '2.0.0', None, 1),
        (('v1.0.0', 'v1.0.1'), '1.0.1', None, 0),
        (('v1.0.0', 'v1.0.1'), '2.0.0', None, 1),
        ((), '2.0.0', 'v2.0.0', 0),
    ],
)
def test_prepare_replacement_uses_selected_publication_tags(
    case: tuple[tuple[str, ...], str | None, str | None, int],
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    tags, requested, expected, status = case
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, original = prepare(tmp_path, source, monkeypatch)
    store = _candidate.candidate_store_path(paths, project.name)
    for ref in ('refs/heads/main', *(f'refs/tags/{tag}' for tag in tags)):
        subprocess.run(
            ['git', '-C', store, 'update-ref', ref, original.commit], check=True, env=git_env()
        )
    _candidate.discard(paths, project.name, force=False)
    capsys.readouterr()
    args = ['prepare', '--project', project.name, '--replace', '--yes']
    if requested:
        args.extend(['--version', requested])
    assert _cli.main(args) == status
    record = _candidate.load_candidate(paths, project.name)
    captured = capsys.readouterr()
    if status:
        assert record is None
        assert 'replacement version must match a moved tag' in captured.err
    else:
        assert record is not None
        assert record.replaces == original.commit
        assert record.tag_intent == expected
        assert record.tip == record.commit
        assert record.message == original.message
        assert record.poststate_tags is not None
        assert all(record.poststate_tags[tag] == record.commit for tag in tags)
        assert 'Prepared snapshot' in captured.out
        assert 'Operation:' not in captured.out
        assert captured.out.count('Next:') == 1
        assert captured.out.rstrip().endswith('Next: pubanon inspect --project=demo')


def test_prepare_reports_gate_term_and_escaped_finding_unit(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    answers: Callable[[Sequence[tuple[str, str]]], None],
) -> None:
    term = 'hygiene-canary'
    separator = '\x1b' if os.name != 'nt' else '\u2028'
    source = make_repo(files={f'docs/{term}{separator}.txt': 'public\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    _config.save_project(paths, replace(project, hygiene_terms=(term,)))

    answers([])
    assert (
        _cli.main(['prepare', '--project', project.name, '--acknowledge-new-paths', '--yes']) == 1
    )

    rendered = capsys.readouterr().err
    assert f'    docs/{term}{escape(separator)}.txt (filename)\n' in rendered
    assert f'      Blocked text: {term}\n' in rendered
    assert 'bytes' not in rendered
    assert 'hygiene_terms' not in rendered
    assert separator not in rendered
    assert term in rendered
    assert _candidate.load_candidate(paths, project.name) is None


def test_prepare_reports_omitted_working_changes(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'committed\n', 'staged.txt': 'before\n'})
    (source / 'README.md').write_text('unstaged\n')
    (source / 'staged.txt').write_text('staged\n')
    subprocess.run(['git', '-C', source, 'add', 'staged.txt'], check=True, env=git_env())
    (source / 'untracked.txt').write_text('untracked\n')

    paths, project, record = prepare(tmp_path, source, monkeypatch)

    snapshot = _source.read_snapshot(
        _candidate.candidate_store_path(paths, project.name), record.commit
    )
    assert {entry.path: entry.data for entry in snapshot} == {
        'README.md': b'committed\n',
        'staged.txt': b'before\n',
    }
    output = capsys.readouterr().out
    assert f'at {record.pinned.commit[:12]}' in output
    assert 'left out: 1 staged change, 1 unstaged change, 1 untracked change)' in output
    assert 'Selected source ref:' not in output
    assert 'untracked.txt' not in output


def test_prepare_reports_operator_outcome_before_candidate_details(
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    capsys: pytest.CaptureFixture[str],
) -> None:
    paths, _global_config, project = configured_project
    assert project is not None

    assert (
        _cli.main(['prepare', '--project', project.name, '--acknowledge-new-paths', '--yes']) == 0
    )

    record = _candidate.load_candidate(paths, project.name)
    assert record is not None
    output = capsys.readouterr().out
    assert 'Prepared snapshot' in output
    assert 'Prepared snapshot untagged with 1 file.' in output
    assert 'Operation:' not in output
    assert 'Next: pubanon inspect --project=demo' in output


def test_prepare_review_excludes_included_file_only_after_acceptance(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n', 'docs/draft.md': 'Private Person\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    prompts = iter(
        [
            ('Use this selection?', 'r'),
            ('Include these files?', 'i'),
            ('Include these files?', 'e'),
            ('Use this selection?', 'c'),
        ]
    )
    previews: list[str] = []

    def _answer(prompt: str) -> str:
        expected, reply = next(prompts)
        assert prompt.startswith(expected)
        assert _config.load_project(paths, project.name) == project
        assert _candidate.load_candidate(paths, project.name) is None
        if prompt.startswith('Use this selection?'):
            previews.append(capsys.readouterr().out)
        return reply

    monkeypatch.setattr('builtins.input', _answer)
    assert _cli.main(['prepare', '--project', project.name, '--acknowledge-new-paths']) == 0
    assert len(previews) == 2
    first_included = previews[0].split('Files to publish', 1)[1].split('Excluded files', 1)[0]
    second_included = previews[1].split('Files to publish', 1)[1].split('Excluded files', 1)[0]
    second_excluded = previews[1].split('Excluded files', 1)[1]
    assert 'README.md' in first_included
    assert 'docs/draft.md' in first_included
    assert 'README.md' in second_included
    assert 'docs/draft.md' not in second_included
    assert 'docs/draft.md' in second_excluded
    saved = _config.load_project(paths, project.name)
    assert saved.allow == ('README.md',)
    assert saved.deny == ('docs/draft.md',)
    record = _candidate.load_candidate(paths, project.name)
    assert record is not None
    snapshot = _source.read_snapshot(_candidate.store_path(paths, project.name), record.commit)
    assert [entry.path for entry in snapshot] == ['README.md']


def test_prepare_rewrite_continue_without_progress_refuses(
    configured_project: tuple[_config.Paths, _config.GlobalConfig, _config.ProjectConfig],
    capsys: pytest.CaptureFixture[str],
) -> None:
    _paths, _global, project = configured_project
    assert _cli.main(['prepare', '--project', project.name, '--continue']) == 1
    assert 'no rewrite is in progress' in capsys.readouterr().err


@pytest.mark.slow
def test_prepare_rewrite_invalidated_continuation_preserves_reprepare_inputs(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    paths, project, original = replacement_context(tmp_path, source, monkeypatch, conflict=True)
    assert _cli.main(['prepare', '--project', project.name, '--replace', 'v1.0.0', '--yes']) == 0
    record = _candidate.load_candidate(paths, project.name)
    assert record is not None
    _config.save_project(paths, replace(project, extra_terms=('new-private-term',)))
    capsys.readouterr()
    assert _cli.main(['prepare', '--project', project.name, '--continue']) == 1
    error = capsys.readouterr().err
    assert 'the configuration changed after the snapshot was prepared' in error
    assert f'pubanon prepare --project=demo --replace {original.commit}' in error
    assert f'--source {record.pinned.commit} --version 1.0.0' in error


@pytest.mark.slow
def test_prepare_rewrite_pause_and_continue_uses_saved_source(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    paths, project, _original = replacement_context(tmp_path, source, monkeypatch, conflict=True)
    capsys.readouterr()
    assert _cli.main(['prepare', '--project', project.name, '--replace', 'v1.0.0', '--yes']) == 0
    record = _candidate.load_candidate(paths, project.name)
    assert record is not None
    assert record.tip is None
    progress = _records.load_rewrite(paths, project.name)
    assert progress is not None
    assert progress.phase == 'awaiting'
    assert progress.pause is not None
    directory = _records.rewrite_directory(paths, project.name)
    output = capsys.readouterr().out
    assert 'Preparation: incomplete\n' in output
    assert 'Rebuilding snapshot: v1.0.1\n' in output
    for conflict in progress.pause.conflicts:
        assert f'{escape(conflict.conflict_type)}: {escape(conflict.message)}' in output
    assert 'Replaces: published snapshot for v1.0.0\n' in output
    assert escape(str(directory / 'resolutions.toml')) in output
    assert output.count('Next:') == 1
    assert output.rstrip().endswith('Next: pubanon prepare --project=demo --continue')
    manifest = directory / 'resolutions.toml'
    manifest.write_text(manifest.read_text().replace('action = "unresolved"', 'action = "keep"'))
    (directory / 'tree' / 'README.md').write_bytes(b'resolved\n')

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail('continuation tried to reselect source')

    monkeypatch.setattr(_source, 'pin', _forbidden)
    assert _cli.main(['prepare', '--project', project.name, '--continue']) == 0
    complete = _candidate.load_candidate(paths, project.name)
    assert complete is not None
    assert complete.tip is not None
    assert complete.pinned == record.pinned
    assert _records.load_rewrite(paths, project.name) is None
    assert {
        entry.path: entry.data
        for entry in _source.read_snapshot(
            _candidate.candidate_store_path(paths, project.name), complete.tip
        )
    } == {'README.md': b'resolved\n'}


def test_prepare_shows_included_files_and_committed_source_lines(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(
        files={
            'Makefile': 'first\nEXCLUDES := private-alpha private-beta\n',
            'README.md': 'private-beta\n',
            'notes/ignored.txt': 'private-alpha\n',
        }
    )
    paths, config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    _config.save_global(
        paths,
        replace(
            config,
            sensitive_terms=('private-alpha', 'private-beta'),
            ignored_directories=('notes',),
        ),
        paths.global_config.read_bytes(),
    )
    (source / 'Makefile').write_text('uncommitted correction\n')
    assert (
        _cli.main(['prepare', '--project', project.name, '--yes', '--acknowledge-new-paths']) == 1
    )
    error = capsys.readouterr().err
    assert 'cannot prepare: blocked text in included files' in error
    assert '2 files selected for publication need changes' in error
    assert error.count('    Makefile:2\n') == 1
    assert error.count('    README.md:1\n') == 1
    assert '      EXCLUDES := private-alpha private-beta\n' in error
    assert '      Blocked text: private-alpha, private-beta\n' in error
    assert '      private-beta\n' in error
    assert 'notes/ignored.txt' not in error
    assert 'uncommitted correction' not in error
    for bookkeeping in ('blob', 'bytes', 'sensitive_terms', 'entry 1', 'content unit'):
        assert bookkeeping not in error
    assert 'commit' in error
    assert 'pubanon prepare' in error
    assert _candidate.load_candidate(paths, project.name) is None


def test_prepare_source_older_revision_uses_selected_bytes(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(
        files={'README.md': 'older\n', 'pyproject.toml': '[project]\nversion = "0.0.1"\n'}
    )
    paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    (source / 'README.md').write_text('newer\n')
    (source / 'pyproject.toml').write_text('[project]\nversion = "0.0.2"\n')
    subprocess.run(['git', '-C', source, 'add', '.'], check=True, env=git_env())
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
            'newer',
        ],
        check=True,
        env=git_env(),
    )

    assert (
        _cli.main(
            [
                'prepare',
                '--project',
                project.name,
                '--source',
                'HEAD~1',
                '--acknowledge-new-paths',
                '--yes',
            ]
        )
        == 0
    )

    record = _candidate.load_candidate(paths, project.name)
    assert record is not None
    snapshot = _source.read_snapshot(
        _candidate.candidate_store_path(paths, project.name), record.commit
    )
    assert snapshot[0].data == b'older\n'
    assert record.tag_intent == 'v0.0.1'
    assert 'Source: HEAD~1 at ' in capsys.readouterr().out


@pytest.mark.parametrize('requested', [None, '0.0.1', '0.0.2'])
def test_prepare_uses_committed_package_version_and_rejects_mismatch(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    requested: str | None,
) -> None:
    source = make_repo(files={'pyproject.toml': '[project]\nname = "demo"\nversion = "0.0.1"\n'})
    paths, _global, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    (source / 'pyproject.toml').write_text('[project]\nname = "demo"\nversion = "0.0.2"\n')
    argv = ['prepare', '--project', project.name, '--yes']
    if requested is not None:
        argv.extend(['--version', requested])
    status = _cli.main(argv)
    captured = capsys.readouterr()
    record = _candidate.load_candidate(paths, project.name)
    if requested == '0.0.2':
        assert status != 0
        assert record is None
        assert 'does not match' in captured.err
        assert '0.0.1' in captured.err
        assert '0.0.2' in captured.err
        assert 'committed pyproject.toml' in captured.err
    else:
        assert status == 0
        assert record is not None
        assert record.tag_intent == 'v0.0.1'
        assert record.message == 'release: v0.0.1'
        assert 'Prepared snapshot v0.0.1' in captured.out


def test_prepare_validates_version_before_prompts(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    ctx = ProjectContext(paths, global_config, project)

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError('no prompt before version validation')

    monkeypatch.setattr(_selection, 'ask', _forbidden)
    monkeypatch.setattr(_prepare, 'confirm', _forbidden)
    options = PrepareOptions('HEAD', 'not-a-version', None, acknowledge_new_paths=False, yes=False)
    with pytest.raises(PubanonError, match=r'X\.Y\.Z'):
        plan_prepare_locked(ctx, options)


@pytest.mark.parametrize(
    'contents',
    ['[project', '[project]\nversion = 1\n', '[project]\nversion = "1.0.0rc1"\n'],
)
def test_prepare_version_reports_unreadable_or_unsupported_metadata(contents: str) -> None:
    snapshot = (_source.Entry('pyproject.toml', 0o100644, 'blob', contents.encode()),)
    with pytest.raises(PubanonError, match=r'pyproject\.toml'):
        _prepare._publication_version(snapshot, None)


@pytest.mark.parametrize(
    ('contents', 'requested', 'expected'),
    [
        ('[tool.ruff]\nline-length = 96\n', None, None),
        ('[project]\nname = "demo"\n', '0.0.2', '0.0.2'),
        ('[project]\ndynamic = ["version"]\n', '0.0.2', '0.0.2'),
        ('[project]\nversion = "0.0.1"\n', None, '0.0.1'),
    ],
)
def test_prepare_version_uses_static_metadata_or_manual_entry(
    contents: str, requested: str | None, expected: str | None
) -> None:
    snapshot = (_source.Entry('pyproject.toml', 0o100644, 'blob', contents.encode()),)
    assert _prepare._publication_version(snapshot, requested) == expected


def test_prepare_yes_allows_first_release_new_paths_without_stdin(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    ctx = ProjectContext(paths, global_config, project)

    def _forbidden(_prompt: str) -> bool:
        raise AssertionError('--yes must not read stdin')

    monkeypatch.setattr(_prepare, 'confirm', _forbidden)
    options = PrepareOptions('HEAD', None, None, acknowledge_new_paths=False, yes=True)
    record = prepare_locked(ctx, options)
    assert record.commit
    output = capsys.readouterr().out
    assert 'Files not in the last release' not in output
    assert 'Saved list:' not in output
    assert (paths.data_dir / 'projects' / project.name / 'new-paths.json').exists()
    store = _candidate.candidate_store_path(paths, project.name)
    run_git(['update-ref', 'refs/heads/main', record.commit], cwd=store)
    _candidate.discard(paths, project.name, force=False)
    (source / 'new.txt').write_text('new file\n')
    subprocess.run(['git', '-C', source, 'add', 'new.txt'], check=True, env=git_env())
    subprocess.run(['git', '-C', source, 'commit', '-qm', 'Add file'], check=True, env=git_env())
    prepare_locked(ctx, replace(options, acknowledge_new_paths=True))
    later = capsys.readouterr().out
    assert 'Files not in the last release' in later
    assert '  new.txt' in later
    assert 'Saved list:' not in later


@pytest.mark.parametrize('directory', ['publications', 'source-worktrees'])
def test_publication_state_is_refused_before_prepare(
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    directory: str,
) -> None:
    selected = f'data/pubanon/projects/demo/{directory}/private.txt'
    source = make_repo(files={selected: 'private state', 'publications/ordinary.txt': 'ordinary'})
    paths, _global, _project = configure(source, monkeypatch, source)
    assert _cli.main(['prepare', '--project', 'demo', '--acknowledge-new-paths', '--yes']) == 1
    error = capsys.readouterr().err
    assert 'publication state' in error
    assert escape(str(source / selected)) in error
    assert _candidate.load_candidate(paths, 'demo') is None


@pytest.mark.parametrize('reference', ['', 'PRIVATE-MARKER', 'docs/private', 'notes/confidential'])
def test_prepare_ignores_private_directories_but_blocks_references_in_makefile(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reference: str,
) -> None:
    source = make_repo(
        files={
            'Makefile': f"EXCLUDES := --exclude '{reference}/**'\n" if reference else 'all:\n',
            'docs/private/notes.md': 'Private Person',
            'notes/confidential/design.md': 'Private Person',
        }
    )
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    _config.save_global(
        paths,
        replace(
            global_config,
            sensitive_terms=(
                *global_config.sensitive_terms,
                'private-marker',
                'docs/private',
                'notes/confidential',
            ),
            ignored_directories=(
                'DOCS/Private',
                'Notes/CONFIDENTIAL',
            ),
        ),
        paths.global_config.read_bytes(),
    )
    result = _cli.main(['prepare', '--project', project.name, '--acknowledge-new-paths', '--yes'])
    record = _candidate.load_candidate(paths, project.name)
    if reference:
        assert result == 1
        error = capsys.readouterr().err
        assert 'cannot prepare: blocked text in included files' in error
        assert 'Makefile' in error
        assert record is None
    else:
        assert result == 0
        assert record is not None
        store = _candidate.candidate_store_path(paths, project.name)
        snapshot = _source.read_snapshot(store, record.commit, git_dir=store)
        assert [entry.path for entry in snapshot] == ['Makefile']


@pytest.mark.parametrize('reference', ['', 'LOCAL.md'])
def test_prepare_ignores_files_but_blocks_references_in_makefile(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    reference: str,
) -> None:
    source = make_repo(
        files={
            'Makefile': f"EXCLUDES := --exclude '{reference}/**'\n" if reference else 'all:\n',
            'LOCAL.md': 'Private Person',
            'nested/LOCAL.md': 'Private Person',
        }
    )
    paths, global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    _config.save_global(
        paths,
        replace(
            global_config,
            sensitive_terms=(
                *global_config.sensitive_terms,
                'LOCAL.md',
            ),
            ignored_files=('local.MD',),
        ),
        paths.global_config.read_bytes(),
    )
    result = _cli.main(['prepare', '--project', project.name, '--acknowledge-new-paths', '--yes'])
    record = _candidate.load_candidate(paths, project.name)
    if reference:
        assert result == 1
        error = capsys.readouterr().err
        assert 'cannot prepare: blocked text in included files' in error
        assert 'Makefile' in error
        assert record is None
    else:
        assert result == 0
        assert record is not None
        store = _candidate.candidate_store_path(paths, project.name)
        snapshot = _source.read_snapshot(store, record.commit, git_dir=store)
        assert [entry.path for entry in snapshot] == ['Makefile']
