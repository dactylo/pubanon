import json
import os
import subprocess
from collections.abc import Callable, Generator
from contextlib import contextmanager, redirect_stdout
from dataclasses import replace
from io import StringIO
from pathlib import Path
from typing import assert_type, cast

import pytest

from pubanon import (
    _cli,
    _config,
    _filesystem,
)
from pubanon._config import GlobalConfig, Paths, ProjectConfig, Settings
from pubanon._errors import OperationCancelled, PubanonError
from pubanon._gate import RuleClass
from pubanon._remote import _host, _publish, _stage
from pubanon._remote._publish import (
    _acknowledge_exposure,
    _verify_for_publish,
    _verify_remote,
    _verify_sync_prefix,
)
from pubanon._remote._stage import _save_stage
from pubanon._run import GitResult
from pubanon._snapshots import _audit, _candidate, _history, _records, _rewrite, _source
from pubanon._snapshots._source import Snapshot, pin, read_snapshot
from pubanon._terminal import escape
from tests._git import git_env, remove_tree
from tests._permissions import assert_private_mode
from tests.snapshots.test_rewrite import _install_faults


def _configs(source: Path) -> tuple[GlobalConfig, ProjectConfig]:
    return (
        GlobalConfig(
            anon_name='Anonymous Publisher',
            anon_email='anonymous@example.invalid',
            accounts={'github': 'release-account'},
            sensitive_terms=('Private Person',),
        ),
        ProjectConfig(
            name='example',
            source_path=source.resolve(),
            host='github',
            owner='public-owner',
            repo='public-repo',
            visibility='public',
            allow=('**',),
            deny=(),
            secret_exceptions=(),
            extra_terms=(),
            hygiene_terms=(),
            self_publication=True,
            required_checks=(),
            settings=Settings('', 'main', issues=True, wiki=False, projects=False),
        ),
    )


@contextmanager
def _failed_readback(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    real_ls_remote = _publish.ls_remote
    calls = 0

    def _fail_after_push(
        dest: _host.Destination,
        paths: Paths,
        project: str,
    ) -> _host.RemoteRefs:
        nonlocal calls
        calls += 1
        if calls > 1:
            raise PubanonError('simulated remote read failure')
        return real_ls_remote(dest, paths, project)

    monkeypatch.setattr(_publish, 'ls_remote', _fail_after_push)
    try:
        yield
    finally:
        monkeypatch.setattr(_publish, 'ls_remote', real_ls_remote)


def _git_command(
    repo: Path,
    *args: str,
    env: dict[str, str] | None = None,
    input_bytes: bytes | None = None,
) -> bytes:
    return subprocess.run(
        ['git', '-C', repo, *args],
        check=True,
        capture_output=True,
        env=git_env(env),
        input=input_bytes,
    ).stdout


def _literal_history_commit(
    store: Path,
    tree: str,
    *,
    author_name: str = 'Anonymous Publisher',
    message: bytes,
    parent: str | None = None,
) -> str:
    headers = [f'tree {tree}']
    if parent is not None:
        headers.append(f'parent {parent}')
    headers.extend(
        (
            f'author {author_name} <anonymous@example.invalid> 1700000000 +0000',
            'committer Anonymous Publisher <anonymous@example.invalid> 1700000000 +0000',
        )
    )
    return (
        _git_command(
            store,
            'hash-object',
            '-t',
            'commit',
            '-w',
            '--stdin',
            input_bytes='\n'.join(headers).encode() + b'\n\n' + message,
        )
        .decode()
        .strip()
    )


def _new_host(tmp_path: Path) -> Path:
    host = tmp_path / 'host.git'
    subprocess.run(['git', 'init', '--bare', '-q', host], check=True, env=git_env())
    return host


def _paths(tmp_path: Path) -> Paths:
    config_dir = tmp_path / 'config' / 'pubanon'
    data_dir = tmp_path / 'data' / 'pubanon'
    return Paths(config_dir, data_dir, config_dir / 'config.toml', data_dir / 'registry.json')


def _context(
    tmp_path: Path,
    source: Path,
    *,
    extra_terms: tuple[str, ...] = (),
    selector: str = 'HEAD',
    version: str | None = None,
) -> _publish.PublishContext:
    paths = _paths(tmp_path)
    global_config, project_config = _configs(source)
    project_config = replace(project_config, extra_terms=extra_terms)

    pinned = pin(source, selector)
    record = _candidate.build_candidate(
        paths,
        global_config,
        project_config,
        _candidate.CandidateInputs(
            read_snapshot(source, pinned.commit),
            pinned,
            version,
            None,
        ),
    )
    return _publish.PublishContext(
        paths, global_config, project_config, record, _yes=True, _unattended=False
    )


def _record_path(ctx: _publish.PublishContext) -> Path:
    return ctx._paths.data_dir / 'projects' / ctx._project.name / 'candidate.toml'


def _remote_ref(host: Path, ref: str) -> str | None:
    output = _git_command(host, 'for-each-ref', '--format=%(objectname)', ref)
    return output.decode().strip() or None


def _assert_offline_replacement_history(
    ctx: _publish.PublishContext, host: Path, originals: list[_records.CandidateRecord]
) -> None:
    paths, project = ctx._paths, ctx._project
    source = project.source_path
    old_b = originals[1]
    b2, c2 = ctx._record.replayed
    expected_tags = {'v1.0.0': ctx._record.commit, 'v1.0.1': b2.new, 'v1.0.2': c2.new}
    assert _candidate.published_refs(paths, project.name) == (c2.new, expected_tags)
    assert _remote_ref(host, 'refs/heads/main') == c2.new
    assert _git_command(host, 'rev-list', '--reverse', 'main').decode().splitlines() == [
        ctx._record.commit,
        b2.new,
        c2.new,
    ]
    assert _git_command(
        host, 'for-each-ref', '--format=%(refname:strip=2)', 'refs/tags'
    ).decode().splitlines() == sorted(expected_tags)
    for tag, commit in expected_tags.items():
        assert _remote_ref(host, f'refs/tags/{tag}') == commit
    assert _git_command(host, 'show', 'v1.0.0:README.md') == b'corrected\n'
    assert _git_command(host, 'show', 'v1.0.1:README.md') == b'corrected and later edit\n'
    assert _git_command(host, 'ls-tree', '--name-only', 'v1.0.1').splitlines() == [b'README.md']
    assert _git_command(
        host, 'ls-tree', '-r', '--format=%(objectmode) %(objecttype) %(path)', 'main'
    ).splitlines() == [b'100644 blob README.md', b'100644 blob third.txt']
    assert _git_command(host, 'show', 'main:README.md') == b'corrected and later edit\n'
    assert _git_command(host, 'show', 'main:third.txt') == b'third release\n'

    entries = {entry.commit: entry for entry in _history.publications(paths, project.name)}
    assert all(entries[old.commit].replaced for old in originals)
    assert all(not entries[commit].replaced for commit in expected_tags.values())
    assert _history.select_publication(paths, project.name, 'v1.0.1').commit == b2.new
    old_entry = _history.select_publication(paths, project.name, old_b.commit)
    old_copy = _history.inspect_publication(paths, project.name, old_entry)
    current_copy = _history.inspect_publication(paths, project.name, entries[b2.new])
    assert (old_copy / 'README.md').read_text() == 'later edit\n'
    assert (current_copy / 'README.md').read_text() == 'corrected and later edit\n'
    derived = entries[b2.new].record
    assert derived is not None
    assert derived.pinned is None
    assert derived.derivation == _records.Derivation(
        old_b.commit, old_b.pinned.commit, ctx._record.commit, b2.resolutions
    )
    assert old_entry.record is not None
    old_bookmark = old_entry.record.source_bookmark
    new_bookmark = derived.source_bookmark
    assert old_bookmark is not None
    assert new_bookmark is not None
    assert old_bookmark.refname != new_bookmark.refname
    _history.delete_publication(paths, project, old_entry)
    assert _records.load_publication(paths, project.name, old_b.commit) is None
    assert _remote_ref(source, old_bookmark.refname) is None
    assert _remote_ref(source, new_bookmark.refname) == old_b.pinned.commit
    assert _records.load_publication(paths, project.name, b2.new) == derived
    assert not old_copy.exists()
    assert current_copy.exists()
    assert _remote_ref(host, 'refs/heads/main') == c2.new


def _assert_provisional_bookmark(ctx: _publish.PublishContext) -> None:
    retained = _candidate.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    bookmark = retained.source_bookmark
    assert bookmark is not None
    assert retained == replace(ctx._record, source_bookmark=bookmark)
    assert _remote_ref(ctx._project.source_path, bookmark.refname) == ctx._record.pinned.commit
    assert _records.load_publication(ctx._paths, ctx._project.name, ctx._record.commit) is None


def _resume_offline_conflict(
    ctx: _publish.PublishContext, old_b: str, monkeypatch: pytest.MonkeyPatch
) -> _records.CandidateRecord:
    paths, g, project = ctx._paths, ctx._global_config, ctx._project
    with monkeypatch.context() as fault:
        _install_faults(fault, 'partial-export')
        with pytest.raises(RuntimeError, match='interrupted'):
            _rewrite.continue_rewrite(paths, g, project, env={})
    progress = _records.load_rewrite(paths, project.name)
    assert progress is not None
    assert progress.phase == 'exporting'
    assert progress.pause is not None
    assert progress.pause.old == old_b
    directory = _records.rewrite_directory(paths, project.name)
    assert (directory / 'partial').exists()

    paused = _rewrite.continue_rewrite(paths, g, project, env={})
    assert paused.tip is None
    progress = _records.load_rewrite(paths, project.name)
    assert progress is not None
    assert progress.phase == 'awaiting'
    assert not (directory / 'partial').exists()
    manifest = directory / 'resolutions.toml'
    assert 'action = "unresolved"' in manifest.read_text()
    with pytest.raises(PubanonError, match='unresolved'):
        _rewrite.continue_rewrite(paths, g, project, env={})
    (directory / 'tree' / 'README.md').write_bytes(b'corrected and later edit\n')
    manifest.write_text(manifest.read_text().replace('action = "unresolved"', 'action = "keep"'))
    completed = _rewrite.continue_rewrite(paths, g, project, env={})
    assert completed.commit == ctx._record.commit
    assert len(completed.replayed) == 2
    b2, c2 = completed.replayed
    assert b2.resolutions == (_records.Resolution(('README.md',), 'keep'),)
    assert c2.resolutions == ()
    assert completed.tip == c2.new
    assert _records.load_rewrite(paths, project.name) is None
    assert not directory.exists()
    return completed


def _second_source_commit(source: Path, content: str = 'second\n') -> None:
    (source / 'README.md').write_text(content, encoding='utf-8', newline='\n')
    _git_command(source, 'add', 'README.md')
    _git_command(
        source,
        '-c',
        'user.name=Test User',
        '-c',
        'user.email=test@example.invalid',
        'commit',
        '-qm',
        'second',
    )


def _sync_context(
    ctx: _publish.PublishContext,
    *,
    yes: bool = True,
    unattended: bool = False,
) -> _publish.SyncContext:
    context = _publish.SyncContext(ctx._paths, ctx._global_config, ctx._project, yes, unattended)
    assert_type(context, _publish.SyncContext)
    return context


def _write_host_responses(path: Path, visibility: str = 'public') -> None:
    path.write_text(
        json.dumps(
            [
                {
                    'args': ['api', 'user', '--jq', '.login'],
                    'stdout': 'release-account\n',
                },
                {
                    'args': [
                        'api',
                        'repos/public-owner/public-repo',
                        '--jq',
                        _host._REPOSITORY_QUERY,
                    ],
                    'stdout': json.dumps(
                        {
                            'owner': 'public-owner',
                            'repo': 'public-repo',
                            'visibility': visibility,
                        }
                    ),
                },
            ]
        )
        + '\n'
    )


def _configure_host(
    host: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_host_responses(fake_gh)
    monkeypatch.setattr(_host, 'URL_TEMPLATE', str(host))


def _context_after_first_publish(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    history: tuple[dict[str, str], tuple[str, ...]],
) -> tuple[Path, _publish.PublishContext]:
    first_files, extra_terms = history
    source = make_repo(files=first_files)
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    _publish.publish(_context(tmp_path, source))
    for path in first_files:
        if path != 'README.md':
            _git_command(source, 'rm', '-q', '--', path)
    _second_source_commit(source, 'public successor\n')
    ctx = _context(
        tmp_path,
        source,
        extra_terms=extra_terms,
    )
    _config.save_project(ctx._paths, ctx._project)
    return host, ctx


def _drifted_context(
    drift: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> _publish.PublishContext:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    if drift in {'rewound', 'diverged'}:
        _second_source_commit(source)
    ctx = _context(tmp_path, source)
    selector = ctx._record.pinned.selector_name
    assert selector is not None
    if drift == 'advanced':
        _second_source_commit(source)
    elif drift == 'rewound':
        _git_command(source, 'update-ref', selector, f'{ctx._record.pinned.commit}^')
    elif drift == 'diverged':
        _git_command(source, 'checkout', '-q', '--detach', f'{ctx._record.pinned.commit}^')
        _second_source_commit(source, 'diverged\n')
        _git_command(source, 'update-ref', selector, 'HEAD')
    else:
        _git_command(source, 'update-ref', '-d', selector)
    return ctx


@pytest.fixture(name='publication_cli')
def _publication_cli(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, _publish.PublishContext]:
    source = make_repo(files={'README.md': 'public\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    visibility, version = cast(
        'tuple[str, str | None]', getattr(request, 'param', ('public', '1.0.0'))
    )
    _write_host_responses(fake_gh, visibility)
    ctx = _context(tmp_path, source, version=version)
    project = replace(ctx._project, visibility=visibility)
    record = replace(
        ctx._record,
        destination=replace(ctx._record.destination, visibility=visibility),
        fingerprint=_config.fingerprint(ctx._global_config, project),
    )
    _candidate.save_candidate(ctx._paths, project.name, record)
    ctx = replace(ctx, _project=project, _record=record)
    _config.save_global(ctx._paths, ctx._global_config, None)
    _config.save_project(ctx._paths, replace(ctx._project, workflows={'ship': ('publish',)}))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(ctx._paths.config_dir.parent))
    monkeypatch.setenv('XDG_DATA_HOME', str(ctx._paths.data_dir.parent))
    return host, ctx


def _published_history(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    first_content: str = 'first\n',
) -> tuple[Path, Path, _publish.PublishContext, _publish.PublishContext]:
    source = make_repo(files={'README.md': first_content})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    first = _context(tmp_path, source, version='1.0.0')
    _publish.publish(first)
    _second_source_commit(source)
    second = _context(tmp_path, source, version='2.0.0')
    _publish.publish(second)
    return source, host, first, second


def _published_repair_context(
    fake_gh: Path, make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> tuple[_publish.PublishContext, _host.Destination, Path]:
    source = make_repo(files={'README.md': 'public\n'})
    host = _new_host(source.parent)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(source.parent, source)
    _publish.publish(ctx)
    destination = _host.verify_destination(ctx._paths, ctx._global_config, ctx._project)
    return ctx, destination, host


def _replacement_fixture(
    tmp_path: Path,
    source: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    version: str | None,
) -> tuple[Path, _candidate.CandidateRecord, _publish.PublishContext]:
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    project = replace(project, visibility='private')
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    _write_host_responses(fake_gh, 'private')
    pinned = pin(source, 'HEAD')
    first = _candidate.build_candidate(
        paths,
        global_config,
        project,
        _candidate.CandidateInputs(read_snapshot(source, pinned.commit), pinned, version, None),
    )
    _publish.publish(
        _publish.PublishContext(
            paths,
            global_config,
            project,
            first,
            _yes=True,
            _unattended=False,
        )
    )
    _second_source_commit(source, 'corrected publication\n')
    pinned = pin(source, 'HEAD')
    record = _candidate.build_candidate(
        paths,
        global_config,
        project,
        _candidate.CandidateInputs(
            read_snapshot(source, pinned.commit),
            pinned,
            version,
            None,
            replaces=first.commit,
            replace_scope='one',
        ),
    )
    return (
        host,
        first,
        _publish.PublishContext(
            paths,
            global_config,
            project,
            record,
            _yes=True,
            _unattended=False,
        ),
    )


def _rewrite_fixture(
    tmp_path: Path,
    source: Path,
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    selection: int | None = 1,
) -> tuple[Path, _publish.PublishContext, tuple[_records.CandidateRecord, ...]]:
    paths = _paths(tmp_path)
    g, project = _configs(source)
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    originals: list[_records.CandidateRecord] = []
    store = _candidate.store_path(paths, project.name)
    for index in range(3):
        if index:
            (source / f'addition-{index}.txt').write_bytes(f'addition {index}\n'.encode())
            _git_command(source, 'add', '.')
            _git_command(source, 'commit', '-qm', f'addition {index}')
        pinned = pin(source, 'HEAD')
        record = _candidate.build_candidate(
            paths,
            g,
            project,
            _candidate.CandidateInputs(
                read_snapshot(source, pinned.commit), pinned, f'1.0.{index}', None
            ),
        )
        _bookmark = _source.describe_bookmark(source, project.name, record.commit)
        record = replace(record, source_bookmark=_bookmark)
        _source.ensure_bookmark(_bookmark, pinned.commit)
        _history.record_publication(paths, project, record)
        store = _candidate.store_path(paths, project.name)
        _git_command(store, 'update-ref', 'refs/heads/main', record.commit)
        _git_command(store, 'update-ref', f'refs/tags/v1.0.{index}', record.commit)
        _records.candidate_path(paths, project.name).unlink()
        originals.append(record)
    _git_command(
        host, 'fetch', str(store), 'refs/heads/main:refs/heads/main', 'refs/tags/*:refs/tags/*'
    )
    all_history = selection is None
    selected = originals[-1] if selection is None else originals[selection]
    _git_command(source, 'checkout', '--detach', selected.pinned.commit)
    _second_source_commit(source, 'corrected\n')
    pinned = pin(source, 'HEAD')
    record = _candidate.build_candidate(
        paths,
        g,
        project,
        _candidate.CandidateInputs(
            read_snapshot(source, pinned.commit),
            pinned,
            '1.0.2' if all_history else None,
            None,
            replaces=selected.commit,
            replace_scope='all' if all_history else 'one',
        ),
    )
    if record.tip is None:
        record = _rewrite.continue_rewrite(paths, g, project, env={})
    return (
        host,
        _publish.PublishContext(paths, g, project, record, _yes=True, _unattended=False),
        tuple(originals),
    )


@pytest.fixture
def advanced_source_context(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> _publish.PublishContext:
    return _drifted_context('advanced', tmp_path, fake_gh, make_repo, monkeypatch)


def test_acknowledged_digest_skips_matching_findings_and_changes_for_new_exposure(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = 'first-private-canary'
    second = 'second-private-canary'
    source = make_repo(files={'README.md': 'public\n'})
    ctx = _context(tmp_path, source, extra_terms=(first, second))
    _config.save_project(ctx._paths, ctx._project)
    findings = (
        _audit.AuditFinding('path:first.txt', RuleClass.IDENTITY_CRITICAL, first),
        _audit.AuditFinding('path:style.txt', RuleClass.HYGIENE, 'style-canary'),
    )

    prompts = ['Continue despite this earlier exposure? [y/n] ']

    def _accept(prompt: str) -> str:
        assert prompts
        assert prompts.pop(0) == prompt
        return 'yes'

    monkeypatch.setattr('builtins.input', _accept)
    acknowledged = _acknowledge_exposure(
        ctx._paths,
        (ctx._global_config, ctx._project),
        _audit.HistoryAudit(
            findings, (), tuple(_audit.HistoryEvidence(item, None, item.unit) for item in findings)
        ),
        unattended=False,
    )

    assert not prompts

    def _unexpected_prompt(_prompt: str) -> str:
        pytest.fail('matching exposure digest prompted again')

    monkeypatch.setattr('builtins.input', _unexpected_prompt)
    assert (
        _acknowledge_exposure(
            ctx._paths,
            (ctx._global_config, acknowledged),
            _audit.HistoryAudit(
                findings,
                (),
                tuple(_audit.HistoryEvidence(item, None, item.unit) for item in findings),
            ),
            unattended=False,
        )
        == acknowledged
    )
    changed = (
        *findings,
        _audit.AuditFinding('path:second.txt', RuleClass.IDENTITY_CRITICAL, second),
    )

    prompts = ['Continue despite this earlier exposure? [y/n] ']

    def _decline(prompt: str) -> str:
        assert prompts
        assert prompts.pop(0) == prompt
        return 'no'

    monkeypatch.setattr('builtins.input', _decline)
    with pytest.raises(PubanonError, match='historical exposure was not acknowledged'):
        _acknowledge_exposure(
            ctx._paths,
            (ctx._global_config, acknowledged),
            _audit.HistoryAudit(
                changed,
                (),
                tuple(_audit.HistoryEvidence(item, None, item.unit) for item in changed),
            ),
            unattended=False,
        )

    assert not prompts


def test_changed_source_tag_is_named_without_claiming_branch_movement(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    _git_command(source, 'tag', 'release')
    ctx = _context(tmp_path, source, selector='refs/tags/release')
    _second_source_commit(source)
    _git_command(source, 'tag', '-f', 'release')

    captured = StringIO()
    with redirect_stdout(captured):
        _verify_for_publish(ctx)
    assert captured.getvalue() == 'Warning: source has changed since snapshot was prepared.\n'
    assert _candidate.load_candidate(ctx._paths, ctx._project.name) == ctx._record


def test_changed_source_uses_one_publication_confirmation(
    advanced_source_context: _publish.PublishContext,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    ctx = replace(advanced_source_context, _yes=False)
    prompts: list[str] = []

    def _decline(prompt: str) -> str:
        prompts.append(prompt)
        return 'n'

    monkeypatch.setattr('builtins.input', _decline)
    with pytest.raises(PubanonError, match='publication declined'):
        _publish.publish(ctx)

    assert len(prompts) == 1
    assert prompts[0].startswith('Publish main at ')
    assert capsys.readouterr().out.startswith(
        'Warning: source has changed since snapshot was prepared.\n'
    )
    destination = _host.verify_destination(ctx._paths, ctx._global_config, ctx._project)
    assert _host.ls_remote(destination, ctx._paths, ctx._project.name) == _host.RemoteRefs(
        None, {}
    )
    _assert_provisional_bookmark(ctx)


def test_clean_push_rejection_restores_retained_state(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)

    def _reject(_dest: _host.Destination, _cwd: Path, _args: list[str]) -> GitResult:
        raise PubanonError('credential-bearing command failed')

    monkeypatch.setattr(_publish, 'push', _reject)

    with pytest.raises(PubanonError, match='credential-bearing command failed'):
        _publish.publish(ctx)

    retained = _candidate.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert retained.state == _candidate.STATE_RETAINED


@pytest.mark.parametrize('accept', [False, True])
def test_exposure_review_shows_rule_and_location_before_acknowledgment(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    accept: bool,
) -> None:
    term = 'private-canary'
    source = make_repo(files={'README.md': 'public\n'})
    ctx = _context(tmp_path, source, extra_terms=(term,))
    _config.save_project(ctx._paths, ctx._project)
    finding = _audit.AuditFinding('path:private-canary.txt', RuleClass.IDENTITY_CRITICAL, term)
    audit = _audit.HistoryAudit(
        (finding,),
        (),
        (_audit.HistoryEvidence(finding, 'a' * 40, 'blob bytes 4:18'),),
    )
    prompts = [('Continue despite this earlier exposure? [y/n] ', 'yes' if accept else 'no')]

    def _answer(prompt: str) -> str:
        assert prompts, f'unexpected prompt: {prompt}'
        expected, response = prompts.pop(0)
        assert expected == prompt
        output = capsys.readouterr().out
        assert 'project extra_terms entry 1' in output
        assert 'blob bytes 4:18' in output
        assert 'a' * 40 in output
        assert term in output
        assert output.count('path:private-canary.txt') == 1
        assert 'does not remove published content' in output
        assert 'new snapshot content' in output
        return response

    monkeypatch.setattr('builtins.input', _answer)
    if accept:
        result = _acknowledge_exposure(
            ctx._paths, (ctx._global_config, ctx._project), audit, unattended=False
        )
        assert result.acknowledged_exposure == _audit.exposure_digest(audit.findings)
    else:
        with pytest.raises(PubanonError, match='historical exposure was not acknowledged'):
            _acknowledge_exposure(
                ctx._paths, (ctx._global_config, ctx._project), audit, unattended=False
            )
    assert not prompts
    saved = _config.load_project(ctx._paths, ctx._project.name)
    assert bool(saved.acknowledged_exposure) is accept


def test_indeterminate_diagnostic_names_both_escapes(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    unknown = replace(ctx._record, state=_candidate.STATE_UNKNOWN_OUTCOME)
    _candidate.save_candidate(ctx._paths, ctx._project.name, unknown)
    _git_command(host, 'fetch', str(source), 'HEAD:refs/heads/main')

    with pytest.raises(PubanonError) as excinfo:
        _publish.resolve(replace(ctx, _record=unknown))

    rendered = excinfo.value.render()
    assert 'publish --resolve' in rendered
    assert 'discard --force' in rendered
    assert 'abandons' in rendered


def test_indeterminate_diagnostic_separates_managed_refs_from_unrelated_drift(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    first = _context(tmp_path, source, version='1.0.0')
    _publish.publish(first)
    _second_source_commit(source)
    ctx = _context(tmp_path, source, version='2.0.0')
    unknown = replace(ctx._record, state=_candidate.STATE_UNKNOWN_OUTCOME)
    _candidate.save_candidate(ctx._paths, ctx._project.name, unknown)
    source_commit = _git_command(source, 'rev-parse', 'HEAD').decode().strip()
    _git_command(
        host,
        'fetch',
        str(source),
        '+HEAD:refs/heads/main',
        'HEAD:refs/heads/private-backup',
    )

    with pytest.raises(PubanonError, match='indeterminate') as excinfo:
        _publish.resolve(replace(ctx, _record=unknown))

    assert excinfo.value.expected == (
        f'prestate: main={first._record.commit}; tags=none; branches=none; '
        f'pushed: main={unknown.commit}; tags=v2.0.0={unknown.commit}; branches=none'
    )
    assert excinfo.value.actual == f'main={source_commit}; tags=none; branches=none'
    assert excinfo.value.fact == (
        f'unrelated remote drift: branches: private-backup={source_commit}'
    )
    assert _candidate.load_candidate(ctx._paths, ctx._project.name) == unknown


@pytest.mark.parametrize(
    'case',
    [
        (command, boundary)
        for command in ('publish', 'run')
        for boundary in ('refs', 'record', 'archive', 'publication-record')
    ],
)
@pytest.mark.parametrize('interruption', ['none', 'raw', 'structured'])
def test_landed_publication_finalization_failure_is_resolvable(
    publication_cli: tuple[Path, _publish.PublishContext],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[str, str],
    interruption: str,
) -> None:
    host, ctx = publication_cli
    command, boundary = case
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    inspection = _candidate.materialize_inspect(ctx._paths, ctx._project.name, ctx._record)
    reviewed = (inspection / 'README.md').read_bytes()
    real_git = _publish.run_git
    real_unlink = Path.unlink

    def _cancel() -> None:
        if interruption == 'raw':
            raise KeyboardInterrupt
        if interruption == 'structured':
            raise OperationCancelled('operation cancelled', fact='local write interrupted')

    def _fail_refs(
        args: list[str],
        *,
        cwd: Path,
        input_bytes: bytes | None = None,
        check: bool = True,
    ) -> GitResult:
        if args[2:] == ['update-ref', '--no-deref', '--stdin']:
            _cancel()
            return GitResult(7, b'', b'diagnostic\x1b\\path')
        return real_git(args, cwd=cwd, input_bytes=input_bytes, check=check)

    def _fail_record(path: Path, missing_ok: bool = False) -> None:
        if path == _record_path(ctx):
            _cancel()
            raise OSError('simulated record removal failure')
        real_unlink(path, missing_ok=missing_ok)

    def _fail_preservation(*_args: object, **_kwargs: object) -> None:
        _cancel()
        raise OSError('preservation write interrupted')

    args = (['publish'] if command == 'publish' else ['run', 'ship', '--version', '1.0.0']) + [
        '--project',
        ctx._project.name,
        '--yes',
    ]
    with monkeypatch.context() as fault:
        target, name, replacement = {
            'refs': (_publish, 'run_git', _fail_refs),
            'record': (Path, 'unlink', _fail_record),
            'archive': (_history, 'run_git', _fail_preservation),
            'publication-record': (_records, 'save_publication', _fail_preservation),
        }[boundary]
        fault.setattr(target, name, replacement)
        assert _cli.main(args) == (1 if interruption == 'none' else 130)

    error = capsys.readouterr().err
    assert f'Published and verified commit {ctx._record.commit}' in error
    assert 'Local finalization is incomplete' in error
    assert (
        boundary != 'refs'
        or interruption != 'none'
        or r'exit status 7: diagnostic\x1b\\path' in error
    )
    assert 'Do not publish a new snapshot' in error
    assert f'pubanon publish --project={ctx._project.name} --resolve' in error
    assert 'Traceback' not in error
    retained = _candidate.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert retained.state == _candidate.STATE_UNKNOWN_OUTCOME
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit
    assert _remote_ref(host, 'refs/tags/v1.0.0') == ctx._record.commit
    assert _remote_ref(store, _candidate.CANDIDATE_REF) == (
        None if boundary == 'record' else ctx._record.commit
    )
    assert _cli.main(['publish', '--project', ctx._project.name, '--resolve']) == 0
    for ref in ('refs/heads/main', 'refs/tags/v1.0.0'):
        assert _remote_ref(store, ref) == _remote_ref(host, ref) == ctx._record.commit
    assert _candidate.load_candidate(ctx._paths, ctx._project.name) is None
    assert _remote_ref(store, _candidate.CANDIDATE_REF) is None
    assert (inspection / 'README.md').read_bytes() == reviewed


def test_landed_push_reports_effective_term_in_unrelated_tag_diagnostic(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    term = 'private-tag-name'
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source, extra_terms=(term,))
    real_push = _publish.push

    def _push_then_drift(
        dest: _host.Destination,
        cwd: Path,
        args: list[str],
    ) -> GitResult:
        result = real_push(dest, cwd, args)
        _git_command(host, 'update-ref', f'refs/tags/{term}', ctx._record.commit)
        return result

    monkeypatch.setattr(_publish, 'push', _push_then_drift)

    with pytest.raises(PubanonError, match='snapshot landed') as error:
        _publish.publish(ctx)

    rendered = error.value.render()
    assert term in rendered


def test_landed_push_with_unrelated_tag_records_published(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    real_push = _publish.push

    def _push_then_drift(
        dest: _host.Destination,
        cwd: Path,
        args: list[str],
    ) -> GitResult:
        result = real_push(dest, cwd, args)
        _git_command(host, 'update-ref', 'refs/tags/unrelated', ctx._record.commit)
        return result

    monkeypatch.setattr(_publish, 'push', _push_then_drift)

    with pytest.raises(PubanonError, match='snapshot landed'):
        _publish.publish(ctx)

    assert _candidate.load_candidate(ctx._paths, ctx._project.name) is None
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    assert _remote_ref(store, 'refs/heads/main') == ctx._record.commit


def test_landed_resolution_failure_reports_current_private_term(
    publication_cli: tuple[Path, _publish.PublishContext],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    host, ctx = publication_cli
    ctx = _publish._preserve_source(ctx)
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    _git_command(
        host,
        'fetch',
        str(store),
        f'{ctx._record.commit}:refs/heads/main',
        f'{ctx._record.commit}:refs/tags/v1.0.0',
    )
    _candidate.save_candidate(
        ctx._paths, ctx._project.name, replace(ctx._record, state=_candidate.STATE_UNKNOWN_OUTCOME)
    )
    term = ctx._record.commit[:12]
    _config.save_global(
        ctx._paths,
        replace(ctx._global_config, sensitive_terms=(*ctx._global_config.sensitive_terms, term)),
        ctx._paths.global_config.read_bytes(),
    )

    def _fail_finalization(*_args: object) -> None:
        raise PubanonError('cannot record published refs locally')

    with monkeypatch.context() as fault:
        fault.setattr(_publish, '_finalize', _fail_finalization)
        assert _cli.main(['publish', '--project', ctx._project.name, '--resolve']) == 1
    error = capsys.readouterr().err
    assert term in error
    assert f'Published and verified commit {term}' in error
    assert f'pubanon publish --project={ctx._project.name} --resolve' in error
    assert _cli.main(['publish', '--project', ctx._project.name, '--resolve']) == 0
    assert _candidate.published_refs(ctx._paths, ctx._project.name) == (
        ctx._record.commit,
        {'v1.0.0': ctx._record.commit},
    )
    assert _candidate.load_candidate(ctx._paths, ctx._project.name) is None


def test_mixed_managed_state_is_indeterminate(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source, version='1.0.0')
    real_push = _publish.push

    def _push_then_delete_tag(
        dest: _host.Destination,
        cwd: Path,
        args: list[str],
    ) -> GitResult:
        result = real_push(dest, cwd, args)
        _git_command(host, 'update-ref', '-d', 'refs/tags/v1.0.0')
        return result

    monkeypatch.setattr(_publish, 'push', _push_then_delete_tag)

    with pytest.raises(PubanonError, match='indeterminate'):
        _publish.publish(ctx)

    record = _candidate.load_candidate(ctx._paths, ctx._project.name)
    assert record is not None
    assert record.state == _candidate.STATE_UNKNOWN_OUTCOME


@pytest.mark.parametrize('selector', ['latest', 'all'])
def test_offline_replacement_latest_and_all_exact_host_state(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    selector: str,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    host, ctx, originals = _rewrite_fixture(
        tmp_path, source, fake_gh, monkeypatch, selection=None if selector == 'all' else 2
    )
    selected, scope = _rewrite.resolve_replacement(ctx._paths, ctx._project.name, selector)
    assert selected == originals[-1].commit
    assert scope == ('all' if selector == 'all' else 'one')
    assert ctx._record.replayed == ()
    assert ctx._record.tip == ctx._record.commit
    _publish.publish(ctx)
    expected_tags = {'v1.0.2': ctx._record.commit}
    expected_chain = [ctx._record.commit]
    if selector == 'latest':
        expected_tags.update({'v1.0.0': originals[0].commit, 'v1.0.1': originals[1].commit})
        expected_chain = [originals[0].commit, originals[1].commit, ctx._record.commit]
    assert (
        _git_command(host, 'rev-list', '--reverse', 'main').decode().splitlines() == expected_chain
    )
    assert _git_command(
        host, 'for-each-ref', '--format=%(refname:strip=2)', 'refs/tags'
    ).decode().splitlines() == sorted(expected_tags)
    for tag, commit in expected_tags.items():
        assert _remote_ref(host, f'refs/tags/{tag}') == commit
    assert _git_command(host, 'show', 'main:README.md') == b'corrected\n'
    assert _git_command(host, 'show', 'main:addition-1.txt') == b'addition 1\n'
    assert _git_command(host, 'show', 'main:addition-2.txt') == b'addition 2\n'
    assert _candidate.published_refs(ctx._paths, ctx._project.name) == (
        ctx._record.commit,
        expected_tags,
    )
    saved = _records.load_publication(ctx._paths, ctx._project.name, ctx._record.commit)
    assert saved is not None
    assert saved.replaces == originals[-1].commit
    assert saved.replace_scope == scope
    assert saved.pinned == ctx._record.pinned


def test_offline_replacement_recovers_conflict_export_and_lost_readback(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    originals: list[_records.CandidateRecord] = []
    ctx = None
    for index in range(3):
        if index == 1:
            _second_source_commit(source, 'later edit\n')
        elif index == 2:
            (source / 'third.txt').write_bytes(b'third release\n')
            _git_command(source, 'add', '.')
            _git_command(source, 'commit', '-qm', 'third')
        ctx = _context(tmp_path, source, version=f'1.0.{index}')
        _publish.publish(ctx)
        originals.append(ctx._record)
        assert _remote_ref(host, f'refs/tags/v1.0.{index}') == ctx._record.commit

    assert ctx is not None
    paths, g, project = ctx._paths, ctx._global_config, ctx._project
    old_a, old_b, old_c = originals
    _git_command(source, 'checkout', '--detach', old_a.pinned.commit)
    _second_source_commit(source, 'corrected\n')
    pinned = pin(source, 'HEAD')
    selected, scope = _rewrite.resolve_replacement(paths, project.name, 'v1.0.0')
    correction = _candidate.build_candidate(
        paths,
        g,
        project,
        _candidate.CandidateInputs(
            read_snapshot(source, pinned.commit),
            pinned,
            '1.0.0',
            None,
            replaces=selected,
            replace_scope=scope,
        ),
    )
    ctx = replace(ctx, _record=correction)
    completed = _resume_offline_conflict(ctx, old_b.commit, monkeypatch)
    assert _candidate.published_refs(paths, project.name)[0] == old_c.commit
    assert _remote_ref(host, 'refs/heads/main') == old_c.commit
    tip_copy = _candidate.materialize_inspect(paths, project.name, completed)
    b2_copy = _candidate.inspect_rewritten(paths, project.name, completed, 'v1.0.1')
    assert tip_copy != b2_copy
    assert (tip_copy / 'third.txt').read_text() == 'third release\n'
    assert not (b2_copy / 'third.txt').exists()
    assert (b2_copy / 'README.md').read_text() == 'corrected and later edit\n'

    calls: list[list[str]] = []
    real_push = _publish.push

    def _push(dest: _host.Destination, store: Path, args: list[str]) -> None:
        calls.append(args)
        real_push(dest, store, args)

    monkeypatch.setattr(_publish, 'push', _push)
    ctx = replace(ctx, _record=completed)
    with _failed_readback(monkeypatch), pytest.raises(PubanonError, match='outcome is unknown'):
        _publish.publish(ctx)
    retained = _records.load_candidate(paths, project.name)
    assert retained is not None
    assert retained.state == _candidate.STATE_UNKNOWN_OUTCOME
    assert _publish.resolve(replace(ctx, _record=retained)) == 'published'
    assert len(calls) == 1
    assert _records.load_candidate(paths, project.name) is None
    _assert_offline_replacement_history(ctx, host, originals)


@pytest.mark.parametrize('boundary', ['intent', 'bookmark', 'unknown', 'unknown-saved'])
def test_publication_bookmark_failure_prevents_push(
    publication_cli: tuple[Path, _publish.PublishContext],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    host, ctx = publication_cli
    real_save = _publish.save_candidate

    def _fail(*_args: object) -> None:
        raise OSError('write denied')

    def _fail_save(paths: Paths, project: str, record: _candidate.CandidateRecord) -> None:
        if boundary.startswith('unknown') and record.state != _candidate.STATE_UNKNOWN_OUTCOME:
            real_save(paths, project, record)
            return
        if boundary == 'unknown-saved':
            real_save(paths, project, record)
        raise OSError('write denied')

    with monkeypatch.context() as fault:
        if boundary != 'bookmark':
            fault.setattr(_publish, 'save_candidate', _fail_save)
        else:
            fault.setattr(_source, 'ensure_bookmark', _fail)
        with pytest.raises(PubanonError) as error:
            _publish.publish(ctx)
    rendered = error.value.render()
    assert rendered.count('Nothing was published') == 1
    assert escape(str(ctx._project.source_path)) in rendered
    if boundary in ('intent', 'bookmark'):
        assert 'No push was attempted' in rendered
        assert 'snapshot evidence and any provisional source bookmark are retained' in rendered
    assert 'pubanon publish' in rendered
    assert _remote_ref(host, 'refs/heads/main') is None
    retained = _candidate.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert (retained.source_bookmark is None) == (boundary == 'intent')
    if boundary == 'unknown-saved':
        assert 'pubanon publish --resolve' in error.value.render()
        assert _publish.resolve(replace(ctx, _record=retained)) == 'not-sent'
        retained = _candidate.load_candidate(ctx._paths, ctx._project.name)
        assert retained is not None
    _publish.publish(replace(ctx, _record=retained))
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit


def test_publication_finalization_checks_old_values_atomically(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    ctx = _context(tmp_path, source, version='1.0.0')
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    real_git = _publish.run_git

    def _git(
        args: list[str], *, cwd: Path, input_bytes: bytes | None = None, check: bool = True
    ) -> GitResult:
        if args[2] == 'update-ref':
            _git_command(store, 'update-ref', 'refs/tags/v1.0.0', ctx._record.commit)
        return real_git(args, cwd=cwd, input_bytes=input_bytes, check=check)

    monkeypatch.setattr(_publish, 'run_git', _git)
    with pytest.raises(PubanonError, match='cannot record published refs locally'):
        _publish._finalize(
            ctx, _host.RemoteRefs(ctx._record.commit, {'v1.0.0': ctx._record.commit})
        )
    assert _remote_ref(store, 'refs/heads/main') is None
    assert _remote_ref(store, _candidate.CANDIDATE_REF) == ctx._record.commit
    assert _records.load_candidate(ctx._paths, ctx._project.name) == ctx._record
    with pytest.raises(PubanonError, match='local published refs conflict'):
        _publish._finalize(
            ctx, _host.RemoteRefs(ctx._record.commit, {'v1.0.0': ctx._record.commit})
        )


@pytest.mark.parametrize('structured', [False, True])
def test_publication_interrupt_after_record_removal_reports_completed_finalization(
    publication_cli: tuple[Path, _publish.PublishContext],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    structured: bool,
) -> None:
    host, ctx = publication_cli
    real_unlink = Path.unlink

    def _interrupt_unlink(path: Path, missing_ok: bool = False) -> None:
        real_unlink(path, missing_ok=missing_ok)
        if path == _record_path(ctx):
            if structured:
                raise OperationCancelled('operation cancelled')
            raise KeyboardInterrupt

    with monkeypatch.context() as fault:
        fault.setattr(Path, 'unlink', _interrupt_unlink)
        assert _cli.main(['publish', '--project', ctx._project.name, '--yes']) == 130
    error = capsys.readouterr().err
    assert f'Published and verified commit {ctx._record.commit}' in error
    assert 'local finalization completed' in error
    assert 'incomplete' not in error
    assert '--resolve' not in error
    assert f'pubanon status --project={ctx._project.name} --remote' in error
    assert 'Traceback' not in error
    assert _candidate.load_candidate(ctx._paths, ctx._project.name) is None
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    for ref in ('refs/heads/main', 'refs/tags/v1.0.0'):
        assert _remote_ref(store, ref) == _remote_ref(host, ref) == ctx._record.commit
    assert _cli.main(['status', '--project', ctx._project.name, '--remote']) == 0


@pytest.mark.parametrize('structured', [False, True])
def test_publication_interrupt_before_push_preserves_retained_candidate(
    publication_cli: tuple[Path, _publish.PublishContext],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    structured: bool,
) -> None:
    host, ctx = publication_cli
    prompts: list[str] = []

    def _cancel_prompt(prompt: str) -> str:
        assert prompt == (
            f'Publish main at {ctx._record.commit[:7]} and tag v1.0.0 to '
            'public-owner/public-repo (public)? [y/n] '
        )
        assert not prompts
        prompts.append(prompt)
        if structured:
            raise OperationCancelled('operation cancelled')
        raise KeyboardInterrupt

    with monkeypatch.context() as fault:
        fault.setattr('builtins.input', _cancel_prompt)
        assert _cli.main(['publish', '--project', ctx._project.name]) == 130
    assert len(prompts) == 1
    error = capsys.readouterr().err
    assert error == (
        'Publication cancelled. Nothing was published.\n'
        'The prepared snapshot is still available.\n\n'
        f'Next: pubanon publish --project={ctx._project.name}\n'
    )
    assert 'Traceback' not in error
    _assert_provisional_bookmark(ctx)
    assert _remote_ref(host, 'refs/heads/main') is None
    assert _cli.main(['publish', '--project', ctx._project.name, '--yes']) == 0


@pytest.mark.parametrize('phase', ['before-push', 'not-pushed'])
@pytest.mark.parametrize('saved', [False, True])
def test_publication_interrupt_during_state_write_has_working_recovery(
    publication_cli: tuple[Path, _publish.PublishContext],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    phase: str,
    saved: bool,
) -> None:
    host, ctx = publication_cli
    real_save = _publish.save_candidate
    target_state = (
        _candidate.STATE_UNKNOWN_OUTCOME if phase == 'before-push' else _candidate.STATE_RETAINED
    )

    def _interrupt_save(paths: Paths, project: str, record: _candidate.CandidateRecord) -> None:
        current = _candidate.load_candidate(paths, project)
        if record.state != target_state or (
            phase == 'not-pushed'
            and current is not None
            and current.state != _candidate.STATE_UNKNOWN_OUTCOME
        ):
            real_save(paths, project, record)
            return
        if saved:
            real_save(paths, project, record)
        raise KeyboardInterrupt

    def _fail_push(*_args: object) -> None:
        raise PubanonError('transport refused before sending')

    with monkeypatch.context() as fault:
        fault.setattr(_publish, 'save_candidate', _interrupt_save)
        fault.setattr(_publish, 'push', _fail_push)
        assert _cli.main(['publish', '--project', ctx._project.name, '--yes']) == 130
    error = capsys.readouterr().err
    assert (
        'No push was attempted'
        if phase == 'before-push'
        else 'still points to the previous published snapshot'
    ) in error
    assert _remote_ref(host, 'refs/heads/main') is None
    record = _candidate.load_candidate(ctx._paths, ctx._project.name)
    assert record is not None
    args = ['publish', '--project', ctx._project.name, '--yes']
    if record.state == _candidate.STATE_UNKNOWN_OUTCOME:
        assert f'pubanon publish --project={ctx._project.name} --resolve' in error
        args.append('--resolve')
    else:
        assert '--resolve' not in error
        assert 'pubanon publish' in error
    assert _cli.main(args) == 0


@pytest.mark.parametrize('phase', ['push-unsent', 'push-sent', 'readback'])
@pytest.mark.parametrize('structured', [False, True])
def test_publication_interrupt_reports_outcome_and_working_recovery(
    publication_cli: tuple[Path, _publish.PublishContext],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    phase: str,
    structured: bool,
) -> None:
    host, ctx = publication_cli
    real_push = _publish.push
    real_read = _publish.ls_remote
    reads = 0

    def _cancel() -> None:
        if structured:
            raise OperationCancelled('operation cancelled', fact='interrupted at boundary')
        raise KeyboardInterrupt

    def _cancel_push(dest: _host.Destination, store: Path, args: list[str]) -> None:
        if phase != 'push-unsent':
            real_push(dest, store, args)
        _cancel()

    def _cancel_read(dest: _host.Destination, paths: Paths, project: str) -> _host.RemoteRefs:
        nonlocal reads
        reads += 1
        if reads == 2:
            _cancel()
        return real_read(dest, paths, project)

    with monkeypatch.context() as fault:
        if phase == 'readback':
            fault.setattr(_publish, 'ls_remote', _cancel_read)
        else:
            fault.setattr(_publish, 'push', _cancel_push)
        args = ['publish', '--project', ctx._project.name, '--yes']
        assert _cli.main(args) == 130

    error = capsys.readouterr().err
    assert 'Traceback' not in error
    record = _candidate.load_candidate(ctx._paths, ctx._project.name)
    assert record is not None
    sent = phase in {'push-sent', 'readback'}
    assert _remote_ref(host, 'refs/heads/main') == (ctx._record.commit if sent else None)
    assert 'publication outcome is unknown' in error
    assert f'pubanon publish --project={ctx._project.name} --resolve' in error
    assert 'Published and verified' not in error
    assert record.state == _candidate.STATE_UNKNOWN_OUTCOME
    assert _cli.main(['publish', '--project', ctx._project.name, '--resolve']) == 0
    if sent:
        assert _candidate.load_candidate(ctx._paths, ctx._project.name) is None
    else:
        restored = _candidate.load_candidate(ctx._paths, ctx._project.name)
        assert restored is not None
        assert restored.state == _candidate.STATE_RETAINED


@pytest.mark.parametrize(
    'publication_cli',
    [('public', '1.0.0'), ('private', '1.0.0'), ('public', None), ('private', None)],
    indirect=True,
)
def test_publication_preserves_source_and_record(
    publication_cli: tuple[Path, _publish.PublishContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    host, ctx = publication_cli
    calls: list[list[str]] = []
    real_push = _publish.push

    def _counted(dest: _host.Destination, store: Path, args: list[str]) -> None:
        calls.append(args)
        real_push(dest, store, args)

    monkeypatch.setattr(_publish, 'push', _counted)
    before = _git_command(ctx._project.source_path, 'show-ref', '--heads', '--tags')
    _publish.publish(ctx)
    record = _records.load_publication(ctx._paths, ctx._project.name, ctx._record.commit)
    assert record is not None
    assert record.source_bookmark is not None
    assert record.pinned == ctx._record.pinned
    assert record.destination.visibility == ctx._project.visibility
    assert record.tag_intent == ctx._record.tag_intent
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit
    assert (
        _remote_ref(ctx._project.source_path, record.source_bookmark.refname)
        == ctx._record.pinned.commit
    )
    assert _git_command(ctx._project.source_path, 'show-ref', '--heads', '--tags') == before
    assert (
        _remote_ref(
            _candidate.store_path(ctx._paths, ctx._project.name),
            _records.publication_ref(record.commit),
        )
        == record.commit
    )
    assert len(calls) == 1
    assert _candidate.load_candidate(ctx._paths, ctx._project.name) is None


@pytest.mark.parametrize('boundary', ['archive', 'publication-record', 'refs', 'unlink'])
@pytest.mark.parametrize('drift', [False, True])
def test_publication_recovery_replays_recording_without_push(
    publication_cli: tuple[Path, _publish.PublishContext],
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    drift: bool,
) -> None:
    host, ctx = publication_cli
    real_git = _history.run_git
    real_unlink = Path.unlink
    calls: list[list[str]] = []
    real_push = _publish.push

    def _counted(dest: _host.Destination, store: Path, args: list[str]) -> None:
        calls.append(args)
        real_push(dest, store, args)

    def _fail_git(
        args: list[str], *, cwd: Path, input_bytes: bytes | None = None, check: bool = True
    ) -> GitResult:
        if args[2] == 'update-ref':
            raise OSError('ref write denied')
        return real_git(args, cwd=cwd, input_bytes=input_bytes, check=check)

    def _fail_record(*_args: object) -> None:
        raise OSError('record write denied')

    def _fail_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == _record_path(ctx):
            raise OSError('unlink denied')
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(_publish, 'push', _counted)
    with monkeypatch.context() as fault:
        target, name, replacement = {
            'archive': (_history, 'run_git', _fail_git),
            'publication-record': (_records, 'save_publication', _fail_record),
            'refs': (_publish, 'run_git', _fail_git),
            'unlink': (Path, 'unlink', _fail_unlink),
        }[boundary]
        fault.setattr(target, name, replacement)
        with pytest.raises(PubanonError, match='Local finalization is incomplete'):
            _publish.publish(ctx)
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit
    retained = _candidate.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert retained.source_bookmark is not None
    assert (
        _remote_ref(ctx._project.source_path, retained.source_bookmark.refname)
        == retained.pinned.commit
    )
    first = _records.load_publication(ctx._paths, ctx._project.name, ctx._record.commit)
    if drift and boundary != 'archive':
        _git_command(host, 'update-ref', '-d', 'refs/heads/main')
        with pytest.raises(PubanonError, match='must not be retried'):
            _publish.resolve(replace(ctx, _record=retained))
    else:
        assert _publish.resolve(replace(ctx, _record=retained)) == 'published'
    saved = _records.load_publication(ctx._paths, ctx._project.name, ctx._record.commit)
    assert saved is not None
    if first is not None:
        assert saved.confirmed_at == first.confirmed_at
    assert _candidate.load_candidate(ctx._paths, ctx._project.name) is None
    assert len(calls) == 1


@pytest.mark.parametrize('structured', [False, True])
def test_publication_reporting_cancellation_preserves_completed_record(
    publication_cli: tuple[Path, _publish.PublishContext],
    monkeypatch: pytest.MonkeyPatch,
    structured: bool,
) -> None:
    host, ctx = publication_cli
    real_output = _publish.output

    def _interrupted(text: str = '', *, kind: str = 'plain') -> None:
        if text.startswith('Publication verified'):
            if structured:
                raise OperationCancelled('operation cancelled')
            raise KeyboardInterrupt
        real_output(text, kind=kind)

    monkeypatch.setattr(_publish, 'output', _interrupted)
    with pytest.raises(OperationCancelled) as error:
        _publish.publish(ctx)
    assert 'local finalization completed' in error.value.render()
    assert _candidate.load_candidate(ctx._paths, ctx._project.name) is None
    assert _records.load_publication(ctx._paths, ctx._project.name, ctx._record.commit) is not None
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit


def test_publication_source_disappears_after_push_preserves_recorded_locator(
    publication_cli: tuple[Path, _publish.PublishContext], monkeypatch: pytest.MonkeyPatch
) -> None:
    _host_path, ctx = publication_cli
    with _failed_readback(monkeypatch), pytest.raises(PubanonError, match='unknown'):
        _publish.publish(ctx)
    retained = _candidate.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert retained.source_bookmark is not None
    remove_tree(ctx._project.source_path)
    assert _publish.resolve(replace(ctx, _record=retained)) == 'published'
    saved = _records.load_publication(ctx._paths, ctx._project.name, ctx._record.commit)
    assert saved is not None
    assert saved.source_bookmark == retained.source_bookmark


def test_publish_acknowledges_visible_exposure_before_confirmation(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exposure = 'historical-private-canary'
    _host_path, ctx = _context_after_first_publish(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
        ({f'{exposure}.txt': f'{exposure}\n'}, (exposure,)),
    )
    prompts: list[str] = []
    answers = [
        ('Continue despite this earlier exposure? [y/n] ', 'yes'),
        (
            (
                f'Publish main at {ctx._record.commit[:7]} to '
                'public-owner/public-repo (public)? [y/n] '
            ),
            'no',
        ),
    ]

    def _answer(prompt: str) -> str:
        if not prompts:
            ordinary = capsys.readouterr().out
            assert exposure in ordinary
            assert 'entry 1' in ordinary
        prompts.append(prompt)
        assert answers, f'unexpected prompt: {prompt}'
        expected, response = answers.pop(0)
        assert expected == prompt
        return response

    monkeypatch.setattr('builtins.input', _answer)
    capsys.readouterr()

    with pytest.raises(PubanonError, match='publication declined'):
        _publish.publish(replace(ctx, _yes=False))

    assert not answers
    output = capsys.readouterr().out
    assert prompts == [
        'Continue despite this earlier exposure? [y/n] ',
        f'Publish main at {ctx._record.commit[:7]} to public-owner/public-repo (public)? [y/n] ',
    ]
    assert exposure in output
    assert 'Publish prepared snapshot' in output
    saved = _config.load_project(ctx._paths, ctx._project.name)
    assert saved.acknowledged_exposure
    _assert_provisional_bookmark(ctx)
    retained = _candidate.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    _verify_for_publish(replace(ctx, _project=saved, _record=retained))


def test_publish_acknowledgment_displays_identity_and_hygiene_in_one_path(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    identity = 'historical-private-segment'
    hygiene = 'historical-hygiene-segment'
    path = f'docs/{identity}-{hygiene}.txt'
    source = make_repo(files={'README.md': 'first\n', path: 'public\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    _publish.publish(_context(tmp_path, source))
    _git_command(source, 'rm', '-q', '--', path)
    _second_source_commit(source)
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    project = replace(
        project,
        extra_terms=(identity,),
        hygiene_terms=(hygiene,),
    )

    pinned = pin(source, 'HEAD')
    record = _candidate.build_candidate(
        paths,
        global_config,
        project,
        _candidate.CandidateInputs(
            read_snapshot(source, pinned.commit),
            pinned,
            None,
            None,
        ),
    )
    ctx = _publish.PublishContext(
        paths, global_config, project, record, _yes=False, _unattended=False
    )
    prompts: list[str] = []
    answers = [
        ('Continue despite this earlier exposure? [y/n] ', 'yes'),
        (
            f'Publish main at {record.commit[:7]} to public-owner/public-repo (public)? [y/n] ',
            'no',
        ),
    ]
    before = _git_command(host, 'for-each-ref')

    def _answer(prompt: str) -> str:
        if not prompts:
            ordinary = capsys.readouterr().out
            assert identity in ordinary
            assert hygiene in ordinary
            assert 'entry 1' in ordinary
        prompts.append(prompt)
        assert answers, f'unexpected prompt: {prompt}'
        expected, response = answers.pop(0)
        assert prompt == expected
        return response

    monkeypatch.setattr('builtins.input', _answer)
    capsys.readouterr()

    with pytest.raises(PubanonError, match='publication declined'):
        _publish.publish(ctx)

    output = capsys.readouterr().out
    assert prompts == [
        'Continue despite this earlier exposure? [y/n] ',
        f'Publish main at {record.commit[:7]} to public-owner/public-repo (public)? [y/n] ',
    ]
    assert identity in output
    assert hygiene in output

    assert answers == []
    assert _git_command(host, 'for-each-ref') == before
    complete = paths.data_dir / 'projects' / project.name / f'candidate-files-{record.commit}.txt'
    assert_private_mode(complete, 0o600)
    assert path in complete.read_text()


def test_publish_declined_exposure_acknowledgment_retains_candidate(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exposure = 'historical-private-canary'
    host, ctx = _context_after_first_publish(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
        ({'README.md': f'{exposure}\n'}, (exposure,)),
    )

    expected = ['Continue despite this earlier exposure? [y/n] ']

    def _decline(prompt: str) -> str:
        assert expected, f'unexpected prompt: {prompt}'
        assert expected.pop(0) == prompt
        return 'no'

    monkeypatch.setattr('builtins.input', _decline)
    capsys.readouterr()

    with pytest.raises(PubanonError, match='historical exposure was not acknowledged'):
        _publish.publish(replace(ctx, _yes=False))

    assert not expected
    assert 'publication mutations:' not in capsys.readouterr().out
    assert _config.load_project(ctx._paths, ctx._project.name).acknowledged_exposure == ''
    _assert_provisional_bookmark(ctx)
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.prestate_main


def test_publish_first_and_second_release_enforce_parent(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    first = _context(tmp_path, source, version='1.0.0')

    _publish.publish(first)

    assert _remote_ref(host, 'refs/heads/main') == first._record.commit
    assert _remote_ref(host, 'refs/tags/v1.0.0') == first._record.commit
    assert _candidate.load_candidate(first._paths, first._project.name) is None
    output = capsys.readouterr().out
    assert '  Destination: GitHub public-owner/public-repo' in output
    assert '  Visibility at publication: PUBLIC' in output
    assert '  Version: v1.0.0' in output
    assert f'  Snapshot commit: {first._record.commit}' in output

    _second_source_commit(source)
    second = _context(tmp_path, source)
    assert second._record.prestate_main == first._record.commit

    _publish.publish(second)

    assert _remote_ref(host, 'refs/heads/main') == second._record.commit
    assert (
        _git_command(
            _candidate.store_path(second._paths, second._project.name),
            'rev-parse',
            f'{second._record.commit}^',
        )
        .decode()
        .strip()
        == first._record.commit
    )


def test_publish_hygiene_audit_result_does_not_block(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hygiene = 'historical-style-canary'
    _host_path, ctx = _context_after_first_publish(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
        ({'README.md': 'public history\n'}, ()),
    )
    real_audit = cast('Callable[..., _audit.HistoryAudit]', _audit.audit_history)

    def _audit_with_hygiene_finding(*args: object, **kwargs: object) -> _audit.HistoryAudit:
        audited = real_audit(*args, **kwargs)
        hygiene_finding = _audit.AuditFinding(
            'path:README.md',
            RuleClass.HYGIENE,
            hygiene,
        )
        return _audit.HistoryAudit(
            (*audited.findings, hygiene_finding),
            (),
            (*audited.evidence, _audit.HistoryEvidence(hygiene_finding, None, 'README.md')),
        )

    prompts: list[str] = []

    def _decline_mutation(prompt: str) -> str:
        prompts.append(prompt)
        return 'no'

    monkeypatch.setattr(_audit, 'audit_history', _audit_with_hygiene_finding)
    monkeypatch.setattr('builtins.input', _decline_mutation)

    with pytest.raises(PubanonError, match='publication declined'):
        _publish.publish(replace(ctx, _yes=False))

    assert prompts == [
        f'Publish main at {ctx._record.commit[:7]} to public-owner/public-repo (public)? [y/n] '
    ]


def test_publish_ignores_host_managed_pull_refs(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    _git_command(host, 'fetch', str(store), f'{ctx._record.commit}:refs/pull/1/head')

    _publish.publish(ctx)

    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit
    assert _remote_ref(host, 'refs/pull/1/head') == ctx._record.commit


def test_publish_prestate_diagnostic_reports_effective_term_in_branch(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    term = 'private-branch-name'
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source, extra_terms=(term,))
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    _git_command(host, 'fetch', str(store), f'{ctx._record.commit}:refs/heads/{term}')

    with pytest.raises(PubanonError, match='remote prestate') as error:
        _publish.publish(ctx)

    rendered = error.value.render()
    assert term in rendered


def test_publish_proceeds_when_staging_evidence_covers_the_candidate(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    _save_stage(
        ctx._paths,
        ctx._project.name,
        _stage.StageRecord(ctx._record.commit, 'stage-owner', 'staging-repo'),
    )

    output = StringIO()
    with redirect_stdout(output):
        _publish.publish(ctx)

    assert 'The staged snapshot matches the prepared snapshot.' in output.getvalue()
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit
    assert _candidate.load_candidate(ctx._paths, ctx._project.name) is None


def test_publish_refuses_existing_intended_tag(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source, version='1.0.0')
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    _git_command(host, 'fetch', str(store), f'{ctx._record.commit}:refs/tags/v1.0.0')

    with pytest.raises(PubanonError, match='intended tag already exists'):
        _publish.publish(ctx)


def test_publish_refuses_remote_prestate_mismatch_before_push(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    _git_command(host, 'fetch', str(store), f'{ctx._record.commit}:refs/heads/main')

    with pytest.raises(PubanonError, match='remote prestate') as error:
        _publish.publish(ctx)

    assert error.value.recovery == (
        'pubanon status --remote, then see "Accepting an outside fix" in the operator guide'
    )
    rendered = error.value.render()
    assert 'expected:' in rendered
    assert 'actual:' in rendered
    assert 'merge' not in rendered
    assert 'pull request' not in rendered
    assert _candidate.load_candidate(ctx._paths, ctx._project.name) == ctx._record


def test_publish_refuses_repaired_structural_history_before_acknowledgment_or_preview(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    exposure = 'historical-private-canary'
    source, host, _first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )
    store = _candidate.store_path(second._paths, second._project.name)
    tree = _git_command(store, 'rev-parse', f'{second._record.commit}^{{tree}}').decode().strip()
    invalid = _literal_history_commit(
        store,
        tree,
        author_name='Foreign Author',
        message=f'{exposure}\n'.encode(),
        parent=second._record.commit,
    )
    _git_command(store, 'update-ref', 'refs/heads/main', invalid)
    _git_command(host, 'fetch', '-q', str(store), f'+{invalid}:refs/heads/main')
    _second_source_commit(source, 'third\n')
    ctx = _context(tmp_path, source, extra_terms=(exposure,))
    remote_before = _remote_ref(host, 'refs/heads/main')

    def _unexpected_prompt(_prompt: str) -> str:
        pytest.fail('structural publication failure requested acknowledgment')

    def _unexpected_push(*_args: object, **_kwargs: object) -> GitResult:
        pytest.fail('structural publication failure attempted a push')

    monkeypatch.setattr('builtins.input', _unexpected_prompt)
    monkeypatch.setattr(_publish, 'push', _unexpected_push)
    capsys.readouterr()

    with pytest.raises(PubanonError, match='published history has identity problems'):
        _publish.publish(ctx)

    assert 'Publish prepared snapshot' not in capsys.readouterr().out
    assert _remote_ref(host, 'refs/heads/main') == remote_before
    _assert_provisional_bookmark(ctx)


def test_publish_refuses_stale_stage_without_preview_or_push(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    candidate_a = ctx._record
    _candidate.discard(ctx._paths, ctx._project.name, force=True)
    (source / 'README.md').write_text('candidate B\n')
    _git_command(source, 'add', 'README.md')
    _git_command(
        source,
        '-c',
        'user.name=Test User',
        '-c',
        'user.email=test@example.invalid',
        'commit',
        '-qm',
        'candidate B',
    )
    ctx = _context(tmp_path, source)
    stale_commit = candidate_a.commit
    assert stale_commit != ctx._record.commit
    ctx = replace(ctx)
    _save_stage(
        ctx._paths,
        ctx._project.name,
        _stage.StageRecord(stale_commit, 'stage-owner', 'staging-repo'),
    )

    output = StringIO()
    with (
        redirect_stdout(output),
        pytest.raises(PubanonError, match='staging evidence does not cover') as exc_info,
    ):
        _publish.publish(ctx)

    assert output.getvalue() == ''
    rendered = exc_info.value.render()
    assert stale_commit in rendered
    assert 'pubanon stage push' in rendered
    assert 'pubanon stage clean' in rendered
    assert _remote_ref(host, 'refs/heads/main') is None
    _assert_provisional_bookmark(ctx)


def test_publish_refuses_unexpected_destination_branch_before_push(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    _git_command(
        host,
        'fetch',
        str(store),
        f'{ctx._record.commit}:refs/heads/private-backup',
    )

    with pytest.raises(PubanonError, match='remote prestate') as error:
        _publish.publish(ctx)

    assert error.value.actual == (
        f'main=absent; tags=none; branches=private-backup={ctx._record.commit}'
    )
    assert _remote_ref(host, 'refs/heads/main') is None
    assert _candidate.load_candidate(ctx._paths, ctx._project.name) == ctx._record


def test_publish_reports_hygiene_only_tag_in_identity_failure(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    term = 'historical-hygiene-tag'
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    first = _context(tmp_path, source, version='1.0.0')
    _publish.publish(first)
    store = _candidate.store_path(first._paths, first._project.name)
    blob = (
        _git_command(store, 'hash-object', '-w', '--stdin', input_bytes=b'not a commit\n')
        .decode()
        .strip()
    )
    _git_command(store, 'update-ref', f'refs/tags/{term}', blob)
    _git_command(host, 'fetch', '-q', str(store), f'refs/tags/{term}:refs/tags/{term}')
    _second_source_commit(source)
    paths = _paths(tmp_path)
    global_config, project = _configs(source)
    project = replace(project, hygiene_terms=(term,))

    pinned = pin(source, 'HEAD')
    record = _candidate.build_candidate(
        paths,
        global_config,
        project,
        _candidate.CandidateInputs(
            read_snapshot(source, pinned.commit),
            pinned,
            None,
            None,
        ),
    )
    ctx = _publish.PublishContext(
        paths, global_config, project, record, _yes=True, _unattended=False
    )
    capsys.readouterr()

    with pytest.raises(PubanonError, match='published history has identity problems') as error:
        _publish.publish(ctx)

    rendered = error.value.render()
    assert f'tag:{term}: tag does not point directly to a commit' in rendered
    assert term in rendered
    assert 'Publish prepared snapshot' not in capsys.readouterr().out


def test_publish_requires_completed_rewrite_cleanup_before_any_external_work(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    paths = _paths(tmp_path)
    g, project = _configs(source)
    unlink = Path.unlink

    def _fail_remove(path: Path, *, missing_ok: bool = False) -> None:
        if path == _records.rewrite_path(paths, project.name):
            raise OSError('interrupted completed rewrite cleanup')
        unlink(path, missing_ok=missing_ok)

    with monkeypatch.context() as fault:
        fault.setattr(Path, 'unlink', _fail_remove)
        with pytest.raises(OSError, match='interrupted completed rewrite cleanup'):
            _rewrite_fixture(tmp_path, source, fake_gh, fault)
    record = _records.load_candidate(paths, project.name)
    assert record is not None
    assert record.tip is not None
    assert _records.load_rewrite(paths, project.name) is not None
    host = tmp_path / 'host.git'
    _configure_host(host, fake_gh, monkeypatch)
    before = _git_command(host, 'show-ref')
    ctx = _publish.PublishContext(paths, g, project, record, _yes=True, _unattended=False)

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        pytest.fail('unfinished rewrite reached host or preservation work')

    with monkeypatch.context() as blocked:
        blocked.setattr(_publish, 'verify_destination', _forbidden)
        blocked.setattr(_publish, '_preserve_source', _forbidden)
        blocked.setattr(_history, 'preserve_previous_tip', _forbidden)
        with pytest.raises(PubanonError, match='rewrite completion is pending') as error:
            _publish.publish(ctx)
    assert 'pubanon prepare --project=example --continue' in error.value.render()
    assert _git_command(host, 'show-ref') == before
    assert _records.load_candidate(paths, project.name) == record
    complete = _rewrite.continue_rewrite(paths, g, project, env={})
    assert _records.load_rewrite(paths, project.name) is None
    _publish.publish(replace(ctx, _record=complete))
    assert _git_command(host, 'rev-parse', 'main').decode().strip() == complete.tip
    assert _records.load_candidate(paths, project.name) is None


def test_publish_requires_confirmation_but_retains_declined_candidate(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = replace(_context(tmp_path, source), _yes=False)

    def _decline(_prompt: str) -> str:
        return 'no'

    monkeypatch.setattr('builtins.input', _decline)

    with pytest.raises(PubanonError, match='publication declined'):
        _publish.publish(ctx)

    _assert_provisional_bookmark(ctx)
    assert _remote_ref(host, 'refs/heads/main') is None


@pytest.mark.skipif(os.name == 'nt', reason='POSIX symlink target bytes')
@pytest.mark.parametrize('edit_target', [False, True])
def test_publish_reuses_raw_symlink_inspection_and_refuses_actual_edit(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    edit_target: bool,
) -> None:
    source = make_repo(
        files={'file': 'root\n', 'relative/file': 'nested\n'},
        symlinks={'dot-link': './file', 'double-link': 'relative//file'},
    )
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    inspection = _candidate.materialize_inspect(ctx._paths, ctx._project.name, ctx._record)
    assert os.fsencode(os.readlink(inspection / 'dot-link', dir_fd=None)) == b'./file'
    assert os.fsencode(os.readlink(inspection / 'double-link', dir_fd=None)) == b'relative//file'
    if edit_target:
        _filesystem.chmod(inspection, 0o700)
        (inspection / 'dot-link').unlink()
        (inspection / 'dot-link').symlink_to('file')
        with pytest.raises(PubanonError, match='review directory does not match'):
            _publish.publish(ctx)
        assert _remote_ref(host, 'refs/heads/main') is None
        return

    _publish.publish(ctx)

    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit
    assert _git_command(host, 'show', 'main:dot-link') == b'./file'
    assert _git_command(host, 'show', 'main:double-link') == b'relative//file'


def test_publish_verification_reads_snapshot_once(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    reads: list[int] = []
    original = _candidate.read_snapshot

    def _counting(root: Path, commit: str, *, git_dir: Path | None = None) -> Snapshot:
        reads.append(1)
        return original(root, commit, git_dir=git_dir)

    monkeypatch.setattr(_candidate, 'read_snapshot', _counting)

    _verify_for_publish(ctx)

    assert len(reads) == 1


def test_publish_yes_refuses_unacknowledged_exposure_without_prompt(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exposure = 'historical-private-canary'
    _host_path, ctx = _context_after_first_publish(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
        ({'README.md': f'{exposure}\n'}, (exposure,)),
    )

    def _unexpected_prompt(_prompt: str) -> str:
        pytest.fail('--yes requested an exposure acknowledgment')

    monkeypatch.setattr('builtins.input', _unexpected_prompt)

    with pytest.raises(
        PubanonError,
        match='--yes cannot acknowledge historical exposure',
    ) as error:
        _publish.publish(replace(ctx, _unattended=True))

    assert error.value.recovery == (
        'run pubanon status --audit to review the findings, then rerun without --yes'
    )
    _assert_provisional_bookmark(ctx)


def test_push_writes_unknown_outcome_before_transport_runs(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source, version='1.0.0')
    real_push = _publish.push

    def _inspect_then_push(
        dest: _host.Destination,
        cwd: Path,
        args: list[str],
    ) -> GitResult:
        current = _candidate.load_candidate(ctx._paths, ctx._project.name)
        assert current is not None
        assert current.state == _candidate.STATE_UNKNOWN_OUTCOME
        assert args == [
            '--atomic',
            '--force-with-lease=refs/heads/main:',
            '--force-with-lease=refs/tags/v1.0.0:',
            '--no-follow-tags',
            f'{ctx._record.commit}:refs/heads/main',
            f'{ctx._record.commit}:refs/tags/v1.0.0',
        ]
        return real_push(dest, cwd, args)

    monkeypatch.setattr(_publish, 'push', _inspect_then_push)

    _publish.publish(ctx)

    assert not _record_path(ctx).exists()


def test_readback_failure_leaves_unknown_outcome_and_publish_refuses_resend(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)

    with (
        _failed_readback(monkeypatch),
        pytest.raises(PubanonError, match='publication outcome is unknown') as raised,
    ):
        _publish.publish(ctx)

    assert raised.value.fact == 'error: simulated remote read failure'
    assert raised.value.recovery == 'pubanon publish --resolve'
    retained = _candidate.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert retained.state == _candidate.STATE_UNKNOWN_OUTCOME
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit
    assert (
        _remote_ref(_candidate.store_path(ctx._paths, ctx._project.name), 'refs/heads/main')
        is None
    )
    retry = replace(ctx, _record=retained)

    def _refuse_resend(*_args: object) -> None:
        pytest.fail('unknown publication outcome attempted another push')

    monkeypatch.setattr(_publish, 'push', _refuse_resend)
    with pytest.raises(PubanonError) as error:
        _publish.publish(retry)
    assert error.value.recovery == 'pubanon publish --resolve'


@pytest.mark.parametrize('action', ['publish', 'discard'])
def test_readonly_inspection_survives_publish_and_discard(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    action: str,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    inspection = _candidate.materialize_inspect(ctx._paths, ctx._project.name, ctx._record)
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    original_commit = _git_command(store, 'cat-file', 'commit', ctx._record.commit)
    if action == 'publish':
        _publish.publish(ctx)
        assert _git_command(host, 'show', 'main:README.md') == b'public\n'
    else:
        assert _candidate.discard(ctx._paths, ctx._project.name, force=False)
        assert not _candidate.discard(ctx._paths, ctx._project.name, force=False)
    assert _git_command(store, 'cat-file', 'commit', ctx._record.commit) == original_commit
    assert _git_command(store, 'show', f'{ctx._record.commit}:README.md') == b'public\n'
    assert (inspection / 'README.md').read_text() == 'public\n'
    assert_private_mode(inspection, 0o500)
    assert_private_mode(inspection / 'README.md', 0o400)


@pytest.mark.parametrize('failure', ['locked-tag', 'changed-main'])
def test_rebuild_store_atomic_failure_preserves_managed_refs(
    failure: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source, version='1.0.0')
    _publish.publish(ctx)
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    tree = _git_command(host, 'rev-parse', 'main^{tree}').decode().strip()
    advanced = _literal_history_commit(host, tree, message=b'advance\n', parent=ctx._record.commit)
    _git_command(host, 'update-ref', 'refs/heads/main', advanced)
    _git_command(host, 'update-ref', 'refs/tags/v2.0.0', advanced)
    _git_command(store, 'update-ref', 'refs/tags/obsolete', ctx._record.commit)
    destination = _host.verify_destination(ctx._paths, ctx._global_config, ctx._project)
    real_install = _publish._install_repair_refs
    expected = b''

    def _install(repo: Path, before: _host.RemoteRefs, fetched: _host.RemoteRefs) -> None:
        nonlocal expected
        if failure == 'locked-tag':
            (store / 'refs/tags/v2.0.0.lock').write_text('locked')
        else:
            _git_command(store, 'update-ref', 'refs/heads/main', advanced)
        expected = _git_command(store, 'for-each-ref', 'refs/heads/', 'refs/tags/')
        real_install(repo, before, fetched)

    monkeypatch.setattr(_publish, '_install_repair_refs', _install)
    with pytest.raises(PubanonError):
        _publish.rebuild_store(
            ctx._paths, (ctx._global_config, ctx._project), destination, yes=True
        )

    assert _git_command(store, 'for-each-ref', 'refs/heads/', 'refs/tags/') == expected
    assert _remote_ref(store, 'refs/tags/obsolete') == ctx._record.commit
    assert _remote_ref(store, 'refs/tags/v2.0.0') is None
    assert _git_command(store, 'for-each-ref', 'refs/pubanon/repair/') == b''


@pytest.mark.parametrize('state', [_candidate.STATE_RETAINED, _candidate.STATE_UNKNOWN_OUTCOME])
def test_rebuild_store_candidate_guard_precedes_missing_store_creation(
    state: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    record = replace(ctx._record, state=state)
    _candidate.save_candidate(ctx._paths, ctx._project.name, record)
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    destination = _host.verify_destination(ctx._paths, ctx._global_config, ctx._project)
    remove_tree(store)

    with pytest.raises(PubanonError, match='snapshot'):
        _publish.rebuild_store(
            ctx._paths, (ctx._global_config, ctx._project), destination, yes=True
        )

    assert not store.exists()
    assert _candidate.load_candidate(ctx._paths, ctx._project.name) == record


def test_rebuild_store_from_host_allows_following_release(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    first = _context(tmp_path, source)
    _publish.publish(first)
    store = _candidate.store_path(first._paths, first._project.name)
    remove_tree(store)
    destination = _host.verify_destination(first._paths, first._global_config, first._project)

    _publish.rebuild_store(
        first._paths, (first._global_config, first._project), destination, yes=True
    )

    assert _remote_ref(store, 'refs/heads/main') == first._record.commit
    _second_source_commit(source)
    second = _context(tmp_path, source)
    _publish.publish(second)
    assert _remote_ref(host, 'refs/heads/main') == second._record.commit


@pytest.mark.parametrize(
    'boundary', ['pre-main', 'pre-branch', 'post-main', 'post-branch', 'post-read']
)
def test_rebuild_store_host_races_report_actual_local_state(
    boundary: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source, version='1.0.0')
    _publish.publish(ctx)
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    before = _git_command(store, 'for-each-ref', 'refs/heads/', 'refs/tags/')
    tree = _git_command(host, 'rev-parse', 'main^{tree}').decode().strip()
    advanced = _literal_history_commit(host, tree, message=b'advance\n', parent=ctx._record.commit)
    _git_command(host, 'update-ref', 'refs/heads/main', advanced)
    _git_command(host, 'update-ref', 'refs/tags/v2.0.0', advanced)
    destination = _host.verify_destination(ctx._paths, ctx._global_config, ctx._project)
    real_ls_remote = _publish.ls_remote
    calls = 0

    def _observe(dest: _host.Destination, paths: Paths, project: str) -> _host.RemoteRefs:
        nonlocal calls
        calls += 1
        if calls == (1 if boundary.startswith('pre') else 3):
            if boundary == 'post-read':
                raise PubanonError('simulated read failure')
            ref = 'refs/heads/extra' if boundary.endswith('branch') else 'refs/heads/main'
            _git_command(host, 'update-ref', ref, ctx._record.commit)
        return real_ls_remote(dest, paths, project)

    monkeypatch.setattr(_publish, 'ls_remote', _observe)
    with pytest.raises(PubanonError) as error:
        _publish.rebuild_store(
            ctx._paths, (ctx._global_config, ctx._project), destination, yes=True
        )

    if boundary.startswith('pre'):
        assert 'managed refs were not replaced' in error.value.render()
        assert _git_command(store, 'for-each-ref', 'refs/heads/', 'refs/tags/') == before
    else:
        assert 'local refs may have changed' in error.value.render()
        assert 'pubanon repo verify --repair-store' in error.value.render()
        assert _remote_ref(store, 'refs/heads/main') == advanced
        assert _remote_ref(store, 'refs/tags/v2.0.0') == advanced
    assert _git_command(store, 'for-each-ref', 'refs/pubanon/repair/') == b''


def test_rebuild_store_preserves_existing_history_when_fetch_fails(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    _publish.publish(ctx)
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    destination = _host.verify_destination(ctx._paths, ctx._global_config, ctx._project)

    def _fail_fetch(
        _destination: _host.Destination,
        _store: Path,
        _refspecs: list[str],
    ) -> GitResult:
        raise PubanonError('simulated fetch failure')

    monkeypatch.setattr(_publish, 'fetch', _fail_fetch)

    with pytest.raises(PubanonError, match='simulated fetch failure'):
        _publish.rebuild_store(
            ctx._paths, (ctx._global_config, ctx._project), destination, yes=True
        )

    assert _remote_ref(store, 'refs/heads/main') == ctx._record.commit
    _git_command(store, 'cat-file', '-e', f'{ctx._record.commit}^{{commit}}')


def test_rebuild_store_refreshes_stale_existing_store(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    first = _context(tmp_path, source)
    _publish.publish(first)
    store = _candidate.store_path(first._paths, first._project.name)
    _second_source_commit(source)
    second = _context(tmp_path, source)
    _git_command(
        store,
        'push',
        str(host),
        f'{second._record.commit}:refs/heads/main',
        f'{second._record.commit}:refs/tags/v2.0.0',
    )
    _candidate.discard(first._paths, first._project.name, force=False)
    _git_command(store, 'update-ref', 'refs/tags/local-only', first._record.commit)
    destination = _host.verify_destination(first._paths, first._global_config, first._project)

    result = _publish.rebuild_store(
        first._paths, (first._global_config, first._project), destination, yes=True
    )

    assert _remote_ref(store, 'refs/heads/main') == second._record.commit
    assert _remote_ref(store, 'refs/tags/v2.0.0') == second._record.commit
    assert _remote_ref(store, 'refs/tags/local-only') is None

    assert result.refs == _host.RemoteRefs(
        second._record.commit,
        {'v2.0.0': second._record.commit},
    )
    assert result.audit == _audit.HistoryAudit((), (), ())


@pytest.mark.parametrize(
    'case',
    [
        (
            _candidate.STATE_RETAINED,
            'pubanon discard, then pubanon repo verify --repair-store',
        ),
        (_candidate.STATE_UNKNOWN_OUTCOME, 'pubanon publish --resolve'),
    ],
)
def test_rebuild_store_refuses_retained_candidate_without_losing_objects(
    case: tuple[str, str],
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, recovery = case
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    record = replace(ctx._record, state=state)
    _candidate.save_candidate(ctx._paths, ctx._project.name, record)
    destination = _host.verify_destination(ctx._paths, ctx._global_config, ctx._project)
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    inspection = _candidate.materialize_inspect(ctx._paths, ctx._project.name, record)
    inspected = (inspection / 'README.md').read_bytes()

    with pytest.raises(PubanonError, match='snapshot') as error:
        _publish.rebuild_store(
            ctx._paths, (ctx._global_config, ctx._project), destination, yes=True
        )

    assert error.value.recovery == recovery
    if state == _candidate.STATE_RETAINED:
        assert 'a later pubanon prepare rebuilds the discarded snapshot' in error.value.render()
    assert _candidate.load_candidate(ctx._paths, ctx._project.name) == record
    assert _remote_ref(store, _candidate.CANDIDATE_REF) == record.commit
    _git_command(store, 'cat-file', '-e', f'{record.commit}^{{commit}}')
    assert (inspection / 'README.md').read_bytes() == inspected


@pytest.mark.parametrize('invalid', ['identity', 'merge', 'outside-tag', 'signed'])
def test_rebuild_store_rejects_structural_import_preserving_managed_refs(
    invalid: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source, version='1.0.0')
    _publish.publish(ctx)
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    before = _git_command(store, 'for-each-ref', 'refs/heads/', 'refs/tags/')
    tree = _git_command(host, 'rev-parse', 'main^{tree}').decode().strip()
    foreign = _literal_history_commit(
        host,
        tree,
        author_name='Private Person' if invalid == 'identity' else 'Anonymous Publisher',
        message=b'public import\n',
    )
    if invalid in {'merge', 'signed'}:
        raw = _git_command(host, 'cat-file', 'commit', foreign)
        extra = (
            f'parent {ctx._record.commit}\nparent {foreign}\n'.encode()
            if invalid == 'merge'
            else b'gpgsig signature\n'
        )
        header, rest = raw.split(b'\n', 1)
        foreign = (
            _git_command(
                host,
                'hash-object',
                '-t',
                'commit',
                '-w',
                '--stdin',
                input_bytes=(
                    header + b'\n' + extra + rest
                    if invalid == 'merge'
                    else raw.replace(b'\n\n', b'\n' + extra + b'\n', 1)
                ),
            )
            .decode()
            .strip()
        )
    _git_command(
        host,
        'update-ref',
        'refs/heads/main',
        ctx._record.commit if invalid == 'outside-tag' else foreign,
    )
    _git_command(host, 'update-ref', 'refs/tags/new-tag', foreign)
    destination = _host.verify_destination(ctx._paths, ctx._global_config, ctx._project)

    with pytest.raises(PubanonError, match='structur'):
        _publish.rebuild_store(
            ctx._paths, (ctx._global_config, ctx._project), destination, yes=True
        )

    assert _git_command(store, 'for-each-ref', 'refs/heads/', 'refs/tags/') == before
    assert _git_command(store, 'for-each-ref', 'refs/pubanon/repair/') == b''


def test_rebuild_store_restores_current_findings_without_acknowledgment_or_ancestry(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    _publish.publish(ctx)
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    tree = _git_command(host, 'rev-parse', 'main^{tree}').decode().strip()
    restored = _literal_history_commit(host, tree, message=b'Private Person\n')
    _git_command(host, 'update-ref', 'refs/heads/main', restored)
    _git_command(store, 'symbolic-ref', 'refs/pubanon/repair/stale', 'refs/heads/main')
    _git_command(store, 'update-ref', 'refs/pubanon/unrelated', ctx._record.commit)
    archive_ref = _records.publication_ref(ctx._record.commit)
    record_path = _records.publication_path(ctx._paths, ctx._project.name, ctx._record.commit)
    original_record = record_path.read_bytes()
    assert _remote_ref(store, archive_ref) == ctx._record.commit
    destination = _host.verify_destination(ctx._paths, ctx._global_config, ctx._project)
    before = ctx._project.acknowledged_exposure

    result = _publish.rebuild_store(
        ctx._paths, (ctx._global_config, ctx._project), destination, yes=True
    )

    assert result.refs == _host.RemoteRefs(restored, {})
    assert _remote_ref(store, 'refs/heads/main') == restored
    assert any(finding.term == 'Private Person' for finding in result.audit.findings)
    assert not result.audit.identity_problems
    assert ctx._project.acknowledged_exposure == before == ''
    assert _git_command(store, 'for-each-ref', 'refs/pubanon/repair/') == b''
    assert _remote_ref(store, 'refs/pubanon/unrelated') == ctx._record.commit
    assert _remote_ref(store, archive_ref) == ctx._record.commit
    assert record_path.read_bytes() == original_record
    assert _history.select_publication(ctx._paths, ctx._project.name, ctx._record.commit).record


def test_rejected_push_with_unrelated_tag_keeps_candidate_reusable(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)

    def _reject_after_drift(
        _dest: _host.Destination,
        _cwd: Path,
        _args: list[str],
    ) -> GitResult:
        store = _candidate.store_path(ctx._paths, ctx._project.name)
        _git_command(host, 'fetch', str(store), f'{ctx._record.commit}:refs/tags/unrelated')
        raise PubanonError('credential-bearing command failed')

    monkeypatch.setattr(_publish, 'push', _reject_after_drift)

    with pytest.raises(PubanonError, match='credential-bearing command failed'):
        _publish.publish(ctx)

    retained = _candidate.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert retained.state == _candidate.STATE_RETAINED


def test_remote_prestate_recovery_names_operator_guide(
    tmp_path: Path,
    make_repo: Callable[..., Path],
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    ctx = _context(tmp_path, source)
    stale = replace(ctx._record, prestate_main='1' * 40)
    remote = _host.RemoteRefs('2' * 40, {})

    with pytest.raises(PubanonError) as excinfo:
        _verify_remote(stale, remote)

    assert excinfo.value.recovery == (
        'pubanon status --remote, then see "Accepting an outside fix" in the operator guide'
    )
    assert 'outside fix' in excinfo.value.render()


def test_remote_prestate_renders_complete_terminal_safe_ref_fields(
    tmp_path: Path,
    make_repo: Callable[..., Path],
) -> None:
    term = 'private-canary'
    source = make_repo(files={'README.md': 'first\n'})
    ctx = _context(tmp_path, source, extra_terms=(term,))
    branch = f'backup\x1b{term}'
    remote = _host.RemoteRefs(None, {}, ((branch, 'a' * 40),))

    with pytest.raises(PubanonError, match='remote prestate') as exc_info:
        _verify_remote(ctx._record, remote)

    assert exc_info.value.expected == 'main=absent; tags=none; branches=none'
    actual = exc_info.value.actual
    assert actual is not None
    assert actual == f'main=absent; tags=none; branches=backup\\x1b{term}={"a" * 40}'
    assert '\x1b' not in actual
    assert term in actual


@pytest.mark.parametrize('decision', ['host-moved', 'local-moved', 'cancel'])
def test_repair_consent_preserves_ref_leases_and_cancellation(
    fake_gh: Path, make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch, decision: str
) -> None:
    ctx, destination, host = _published_repair_context(fake_gh, make_repo, monkeypatch)
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    _git_command(store, 'update-ref', 'refs/tags/local-only', ctx._record.commit)
    local_before, host_before = (
        _git_command(store, 'for-each-ref'),
        _git_command(host, 'for-each-ref'),
    )
    answers = ['y']
    changed_oid = ''

    def _answer(prompt: str) -> str:
        nonlocal changed_oid
        assert prompt == 'Replace local publication refs with verified GitHub refs? [y/n] '
        response = answers.pop(0)
        if decision == 'cancel':
            raise KeyboardInterrupt
        target = host if decision == 'host-moved' else store
        tree = _git_command(target, 'rev-parse', 'main^{tree}').decode().strip()
        changed_oid = _literal_history_commit(
            target, tree, message=b'moved\n', parent=ctx._record.commit
        )
        ref = 'refs/heads/main' if decision == 'host-moved' else 'refs/tags/local-only'
        _git_command(target, 'update-ref', ref, changed_oid)
        return response

    monkeypatch.setattr('builtins.input', _answer)
    with pytest.raises(PubanonError) as error:
        _publish.rebuild_store(
            ctx._paths, (ctx._global_config, ctx._project), destination, yes=False
        )
    assert not answers
    if decision == 'host-moved':
        assert 'host refs changed during consent' in error.value.render()
    elif decision == 'cancel':
        assert isinstance(error.value, OperationCancelled)
    if decision != 'local-moved':
        assert _git_command(store, 'for-each-ref') == local_before
    else:
        assert _remote_ref(store, 'refs/tags/local-only') == changed_oid
        assert _remote_ref(store, 'refs/heads/main') == ctx._record.commit
    if decision != 'host-moved':
        assert _git_command(host, 'for-each-ref') == host_before
    assert _git_command(store, 'for-each-ref', 'refs/pubanon/repair/') == b''


def test_repair_decline_preserves_newer_local_refs(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    first = _context(tmp_path, source, version='1.0.0')
    _publish.publish(first)
    _second_source_commit(source)
    second = _context(tmp_path, source, version='2.0.0')
    _publish.publish(second)
    store = _candidate.store_path(first._paths, first._project.name)
    _git_command(host, 'update-ref', 'refs/heads/main', first._record.commit)
    _git_command(host, 'update-ref', '-d', 'refs/tags/v2.0.0')
    local_before = _git_command(store, 'for-each-ref')
    host_before = _git_command(host, 'for-each-ref')
    destination = _host.verify_destination(first._paths, first._global_config, first._project)
    capsys.readouterr()
    answers = ['n']

    def _decline(prompt: str) -> str:
        assert prompt == 'Replace local publication refs with verified GitHub refs? [y/n] '
        output = capsys.readouterr().out
        assert f'refs/heads/main: {second._record.commit} -> {first._record.commit}' in output
        assert f'refs/tags/v2.0.0: {second._record.commit} -> absent' in output
        return answers.pop(0)

    monkeypatch.setattr('builtins.input', _decline)
    with pytest.raises(PubanonError, match='repair declined'):
        _publish.rebuild_store(
            first._paths, (first._global_config, first._project), destination, yes=False
        )
    assert not answers
    assert _git_command(store, 'for-each-ref') == local_before
    assert _git_command(host, 'for-each-ref') == host_before


def test_repair_noop_avoids_consent_and_managed_ref_writes(
    fake_gh: Path, make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, destination, host = _published_repair_context(fake_gh, make_repo, monkeypatch)
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    local_before, host_before = (
        _git_command(store, 'for-each-ref'),
        _git_command(host, 'for-each-ref'),
    )

    def _refuse_action(*_args: object) -> None:
        pytest.fail('no-op repair requested consent or wrote managed refs')

    monkeypatch.setattr('builtins.input', _refuse_action)
    monkeypatch.setattr(_publish, '_install_repair_refs', _refuse_action)
    result = _publish.rebuild_store(
        ctx._paths, (ctx._global_config, ctx._project), destination, yes=False
    )
    assert result.refs == _host.RemoteRefs(ctx._record.commit, {})
    assert _git_command(store, 'for-each-ref') == local_before
    assert _git_command(host, 'for-each-ref') == host_before


@pytest.mark.parametrize(
    'output',
    [
        b'',
        b'invalid refs/pubanon/repair/main\n',
        b'a' * 40 + b' refs/pubanon/repair/other\n',
        (b'a' * 40 + b' refs/pubanon/repair/main\n') * 2,
        b'a' * 40
        + b' refs/pubanon/repair/main\n'
        + (b'b' * 40 + b' refs/pubanon/repair/tags/v1\n') * 2,
        b'a' * 40
        + b' refs/pubanon/repair/main\n'
        + b'b' * 40
        + b' refs/pubanon/repair/tags/\xff\n',
    ],
)
def test_repair_refs_refuses_missing_or_malformed_result(
    output: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _read(_args: list[str], *, cwd: Path) -> GitResult:
        del cwd
        return GitResult(0, output, b'')

    monkeypatch.setattr(_publish, 'run_git', _read)
    with pytest.raises(PubanonError):
        _publish._repair_refs(tmp_path)


def test_replace_all_confirmation_handles_no_removed_tags(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    _host_path, ctx, originals = _rewrite_fixture(
        tmp_path, source, fake_gh, monkeypatch, selection=None
    )
    record = replace(
        ctx._record,
        prestate_tags={'v1.0.2': originals[-1].commit},
    )
    destination = _host.verify_destination(ctx._paths, ctx._global_config, ctx._project)
    with redirect_stdout(StringIO()):
        question = _publish._replacement_review(
            replace(ctx, _record=record),
            _publish._Verified(
                destination, _host.RemoteRefs(record.prestate_main, record.prestate_tags)
            ),
        )
    assert question == 'Publish this replacement?'


def test_replace_all_preview_shows_removed_tags_and_scope(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    _host_path, ctx, originals = _rewrite_fixture(
        tmp_path, source, fake_gh, monkeypatch, selection=None
    )
    destination = _host.verify_destination(ctx._paths, ctx._global_config, ctx._project)
    questions: list[str] = []

    def _approve(question: str) -> bool:
        questions.append(question)
        return True

    monkeypatch.setattr(_publish, 'confirm', _approve)
    capsys.readouterr()
    _publish._review_publication(
        replace(ctx, _yes=False),
        _publish._Verified(
            destination, _host.RemoteRefs(ctx._record.prestate_main, ctx._record.prestate_tags)
        ),
    )
    preview = capsys.readouterr().out
    assert 'Scope: all published history (3 snapshots) -> one snapshot' in preview
    assert 'Removed tags: v1.0.0, v1.0.1\n' in preview
    assert f'Commit: {originals[-1].commit[:12]} -> {ctx._record.commit[:12]}' in preview
    assert questions == ['Publish this replacement?']


def test_replace_all_publishes_updated_identity_in_exact_new_history(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    host, ctx, originals = _rewrite_fixture(tmp_path, source, fake_gh, monkeypatch, selection=None)
    _candidate.discard(ctx._paths, ctx._project.name, force=False)
    identity = replace(ctx._global_config, anon_name='Corrected Publisher')
    pinned = pin(source, 'HEAD')
    candidate = _candidate.build_candidate(
        ctx._paths,
        identity,
        ctx._project,
        _candidate.CandidateInputs(
            read_snapshot(source, pinned.commit),
            pinned,
            '1.0.2',
            None,
            replaces=originals[-1].commit,
            replace_scope='all',
        ),
    )
    _publish.publish(replace(ctx, _global_config=identity, _record=candidate))
    assert _git_command(host, 'rev-list', 'main').decode().splitlines() == [candidate.commit]
    assert _git_command(host, 'tag', '--list').decode().splitlines() == ['v1.0.2']
    assert _git_command(host, 'rev-parse', 'v1.0.2').decode().strip() == candidate.commit
    headers = _git_command(host, 'show', '-s', '--format=%an%n%ae%n%cn%n%ce', 'main')
    assert headers.decode().splitlines() == [
        identity.anon_name,
        identity.anon_email,
        identity.anon_name,
        identity.anon_email,
    ]


def test_replaced_tag_object_reports_unchanged_selected_commit(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    _git_command(source, 'tag', 'release')
    ctx = _context(tmp_path, source, selector='refs/tags/release')
    _git_command(source, 'tag', '-fa', 'release', '-m', 'annotated release')

    captured = StringIO()
    with redirect_stdout(captured):
        _verify_for_publish(ctx)
    assert (
        captured.getvalue()
        == 'Warning: source tag has changed; it still selects the prepared source commit.\n'
    )
    assert _candidate.load_candidate(ctx._paths, ctx._project.name) == ctx._record


def test_replacement_completion_reports_tip_and_watches_published_main(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    _host_path, ctx, _originals = _rewrite_fixture(tmp_path, source, fake_gh, monkeypatch)
    project = replace(ctx._project, required_checks=('build',))

    def _skip_source_report(_paths: Paths, _project: str, _commit: str) -> None:
        return None

    monkeypatch.setattr(_publish, 'report_source_preservation', _skip_source_report)
    capsys.readouterr()
    _publish._report_publication(replace(ctx, _project=project))
    report = capsys.readouterr().out
    assert f'Snapshot commit: {ctx._record.tip}' in report
    assert 'Corrected snapshot: v1.0.1' in report
    assert 'Replayed snapshots: 1' in report
    assert 'Moved tags: v1.0.1, v1.0.2' in report
    assert report.rstrip().endswith('Next: pubanon watch ci')


@pytest.mark.parametrize(
    ('tags', 'expected'),
    [
        (('latest',), 'pubanon watch package'),
        (
            ('v1.9.0', 'v1.10.0', 'z-release'),
            'pubanon watch package --version 1.10.0',
        ),
    ],
)
def test_replacement_completion_selects_latest_strict_release_version(
    publication_cli: tuple[Path, _publish.PublishContext],
    tags: tuple[str, ...],
    expected: str,
) -> None:
    _host_path, ctx = publication_cli
    tip = ctx._record.commit
    record = replace(
        ctx._record,
        replaces=tip,
        replace_scope='one',
        tip=tip,
        poststate_tags=dict.fromkeys(tags, tip),
    )
    project = replace(
        ctx._project,
        required_checks=(),
        package=_config.PackageObservation('pypi', 'demo', ()),
    )

    assert _publish.publication_next_action(project, record) == expected


@pytest.mark.parametrize('visibility', ['private', 'public'])
def test_replacement_preview_names_ref_changes_warning_and_single_confirmation(
    visibility: str,
    *,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    _host_path, ctx, originals = _rewrite_fixture(tmp_path, source, fake_gh, monkeypatch)
    destination = replace(
        _host.verify_destination(ctx._paths, ctx._global_config, ctx._project),
        visibility=visibility,
    )
    questions: list[str] = []

    def _approve(question: str) -> bool:
        questions.append(question)
        return True

    monkeypatch.setattr(_publish, 'confirm', _approve)
    rendered = StringIO()
    with redirect_stdout(rendered):
        _publish._review_publication(
            replace(ctx, _yes=False),
            _publish._Verified(
                destination, _host.RemoteRefs(ctx._record.prestate_main, ctx._record.prestate_tags)
            ),
        )
    preview = rendered.getvalue()
    assert preview.startswith('Replace published snapshot\n')
    assert f'Commit: {originals[1].commit[:12]} -> {ctx._record.commit[:12]}' in preview
    assert 'Later snapshots: 1 replayed' in preview
    assert f'Visibility: {visibility.upper()}' in preview
    assert 'Existing clones, GitHub Releases, and uploaded packages are unchanged.' in preview
    assert questions == ['Publish this replacement?']

    detailed = StringIO()
    with redirect_stdout(detailed):
        _publish._review_publication(
            replace(ctx, _verbose=True),
            _publish._Verified(
                destination, _host.RemoteRefs(ctx._record.prestate_main, ctx._record.prestate_tags)
            ),
        )
    assert f'Commit: {originals[1].commit} -> {ctx._record.commit}' in detailed.getvalue()
    assert f'Replayed commit: {originals[-1].commit} -> {ctx._record.tip}' in detailed.getvalue()
    assert 'Snapshot directory: ' in detailed.getvalue()

    latest = replace(
        ctx._record,
        replaces=originals[-1].commit,
        replayed=(),
        tip=ctx._record.commit,
        poststate_tags={**ctx._record.prestate_tags, 'v1.0.2': ctx._record.commit},
    )
    compact = StringIO()
    with redirect_stdout(compact):
        _publish._replacement_review(
            replace(ctx, _record=latest),
            _publish._Verified(
                destination, _host.RemoteRefs(latest.prestate_main, latest.prestate_tags)
            ),
        )
    expected = (
        'Replace published snapshot\n'
        'Repository: public-owner/public-repo\n'
        f'Visibility: {visibility.upper()}\n'
        'Version: v1.0.2\n'
        f'Commit: {originals[-1].commit[:12]} -> {ctx._record.commit[:12]}\n'
        'Updates: main, v1.0.2\n\n'
        'Existing clones, GitHub Releases, and uploaded packages are unchanged.\n'
    )
    if visibility == 'public':
        expected += 'Public forks and caches may also retain the old commits.\n'
    assert compact.getvalue() == expected


@pytest.mark.parametrize('version', [None, '1.0.0'])
def test_replacement_push_moves_existing_tag_and_preserves_history(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    version: str | None,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    host, first, ctx = _replacement_fixture(tmp_path, source, fake_gh, monkeypatch, version)
    old_record = _records.load_publication(ctx._paths, ctx._project.name, first.commit)
    assert old_record is not None

    _publish.publish(ctx)

    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit
    assert _git_command(host, 'rev-list', '--parents', 'main').decode().splitlines() == [
        ctx._record.commit
    ]
    if version:
        assert _remote_ref(host, 'refs/tags/v1.0.0') == ctx._record.commit
    assert _git_command(host, 'show', 'main:README.md') == b'corrected publication\n'
    assert _candidate.load_candidate(ctx._paths, ctx._project.name) is None
    saved = _records.load_publication(ctx._paths, ctx._project.name, ctx._record.commit)
    assert saved is not None
    assert saved.replaces == first.commit
    assert _records.load_publication(ctx._paths, ctx._project.name, first.commit) == old_record
    assert (
        _remote_ref(
            _candidate.store_path(ctx._paths, ctx._project.name),
            _records.publication_ref(first.commit),
        )
        == first.commit
    )
    assert old_record.source_bookmark is not None
    assert old_record.pinned is not None
    assert (
        _source.source_recovery(old_record.source_bookmark, old_record.pinned.commit).status
        == 'protected'
    )
    assert _git_command(host, 'for-each-ref', '--format=%(refname)').decode().splitlines() == (
        ['refs/heads/main', 'refs/tags/v1.0.0'] if version else ['refs/heads/main']
    )


@pytest.mark.parametrize('landed', [False, True])
def test_replacement_resolves_lost_readback_without_repeating_push(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    landed: bool,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    host, first, ctx = _replacement_fixture(tmp_path, source, fake_gh, monkeypatch, '1.0.0')
    real_push = _publish.push
    calls: list[list[str]] = []

    def _push(dest: _host.Destination, store: Path, args: list[str]) -> None:
        calls.append(args)
        if landed:
            real_push(dest, store, args)
        else:
            raise PubanonError('simulated transport failure')

    monkeypatch.setattr(_publish, 'push', _push)
    with _failed_readback(monkeypatch), pytest.raises(PubanonError, match='outcome is unknown'):
        _publish.publish(ctx)
    retained = _candidate.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert retained.state == _candidate.STATE_UNKNOWN_OUTCOME
    outcome = _publish.resolve(replace(ctx, _record=retained))
    assert outcome == ('published' if landed else 'not-sent')
    assert len(calls) == 1
    assert f'--force-with-lease=refs/heads/main:{first.commit}' in calls[0]
    assert f'--force-with-lease=refs/tags/v1.0.0:{first.commit}' in calls[0]
    assert '--atomic' in calls[0]
    expected = ctx._record.commit if landed else first.commit
    assert _remote_ref(host, 'refs/heads/main') == expected
    assert _remote_ref(host, 'refs/tags/v1.0.0') == expected


def test_replacement_staging_evidence_must_cover_resulting_tip(
    tmp_path: Path,
    make_repo: Callable[..., Path],
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    ctx = _context(tmp_path, source)
    correction = ctx._record.commit
    tip = 'b' * 40 if correction != 'b' * 40 else 'c' * 40
    replacement = replace(ctx._record, replaces='a' * 40, tip=tip, poststate_tags={})
    ctx = replace(ctx, _record=replacement)
    _save_stage(
        ctx._paths,
        ctx._project.name,
        _stage.StageRecord(correction, 'stage-owner', 'staging-repo'),
    )

    with pytest.raises(PubanonError, match='staging evidence') as error:
        _publish._review_publication(ctx, cast('_publish._Verified', object()))

    assert error.value.expected == tip


@pytest.mark.parametrize('tags', [('latest',), ('latest', 'v1.2.3')])
def test_replay_nonrelease_tags_recover_finalization_without_another_push(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    tags: tuple[str, ...],
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    host, ctx, originals = _rewrite_fixture(tmp_path, source, fake_gh, monkeypatch)
    store = _candidate.candidate_store_path(ctx._paths, ctx._project.name)
    before = dict(ctx._record.prestate_tags)
    after = dict(_records.candidate_poststate(ctx._record)[1])
    del before['v1.0.2']
    del after['v1.0.2']
    for repo in (store, host):
        _git_command(repo, 'update-ref', '-d', 'refs/tags/v1.0.2')
        for tag in tags:
            _git_command(repo, 'update-ref', f'refs/tags/{tag}', originals[-1].commit)
    for tag in tags:
        before[tag] = originals[-1].commit
        after[tag] = _records.candidate_tip(ctx._record)
    ctx = replace(ctx, _record=replace(ctx._record, prestate_tags=before, poststate_tags=after))
    _records.save_candidate(ctx._paths, ctx._project.name, ctx._record)
    real_push = _publish.push
    real_save = _records.save_publication
    pushes: list[list[str]] = []
    failed = False

    def _push(dest: _host.Destination, store: Path, args: list[str]) -> None:
        pushes.append(args)
        real_push(dest, store, args)

    def _save(paths: Paths, project: str, record: _records.PublicationRecord) -> None:
        nonlocal failed
        if record.commit == ctx._record.tip and not failed:
            failed = True
            raise OSError('record write interrupted')
        real_save(paths, project, record)

    monkeypatch.setattr(_publish, 'push', _push)
    monkeypatch.setattr(_records, 'save_publication', _save)
    with pytest.raises(PubanonError, match='Local finalization is incomplete'):
        _publish.publish(ctx)
    assert failed
    retained = _records.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    tip = _records.candidate_tip(retained)
    assert _candidate.confirmed_publication(ctx._paths, ctx._project.name, retained)
    assert _candidate.publication_archive(ctx._paths, ctx._project.name, tip)
    assert _records.load_publication(ctx._paths, ctx._project.name, tip) is None
    with pytest.raises(PubanonError, match='confirmed publication cannot be discarded'):
        _candidate.discard(ctx._paths, ctx._project.name, force=True)

    assert _publish.resolve(replace(ctx, _record=retained)) == 'published'

    assert len(pushes) == 1
    assert _records.load_candidate(ctx._paths, ctx._project.name) is None
    saved = _records.load_publication(ctx._paths, ctx._project.name, tip)
    assert saved is not None
    assert saved.tag_intent == ('v1.2.3' if 'v1.2.3' in tags else None)
    poststate = _records.candidate_poststate(retained)
    assert _candidate.published_refs(ctx._paths, ctx._project.name) == poststate
    assert _remote_ref(host, 'refs/heads/main') == tip
    for tag, commit in poststate[1].items():
        assert _remote_ref(host, f'refs/tags/{tag}') == commit


def test_resolution_requires_saved_source_bookmark(
    publication_cli: tuple[Path, _publish.PublishContext],
) -> None:
    host, ctx = publication_cli
    _git_command(
        host,
        'fetch',
        str(_candidate.store_path(ctx._paths, ctx._project.name)),
        f'{ctx._record.commit}:refs/heads/main',
        f'{ctx._record.commit}:refs/tags/v1.0.0',
    )
    unknown = replace(ctx._record, state=_candidate.STATE_UNKNOWN_OUTCOME, source_bookmark=None)
    _candidate.save_candidate(ctx._paths, ctx._project.name, unknown)
    with pytest.raises(PubanonError, match='source bookmark is missing'):
        _publish.resolve(replace(ctx, _record=unknown))
    assert _candidate.load_candidate(ctx._paths, ctx._project.name) == unknown
    assert _records.load_publication(ctx._paths, ctx._project.name, ctx._record.commit) is None
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.commit


@pytest.mark.parametrize('sent', [False, True])
def test_resolve_classifies_prestate_and_pushed_state(
    sent: bool,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _publish._preserve_source(_context(tmp_path, source))
    unknown = replace(ctx._record, state=_candidate.STATE_UNKNOWN_OUTCOME)
    _candidate.save_candidate(ctx._paths, ctx._project.name, unknown)
    if sent:
        store = _candidate.store_path(ctx._paths, ctx._project.name)
        _git_command(host, 'fetch', str(store), f'{unknown.commit}:refs/heads/main')

    outcome = _publish.resolve(replace(ctx, _record=unknown))

    assert outcome == ('published' if sent else 'not-sent')
    current = _candidate.load_candidate(ctx._paths, ctx._project.name)
    if sent:
        assert current is None
        assert (
            _remote_ref(_candidate.store_path(ctx._paths, ctx._project.name), 'refs/heads/main')
            == unknown.commit
        )
    else:
        assert current is not None
        assert current.state == _candidate.STATE_RETAINED


@pytest.mark.parametrize('structured', [False, True])
def test_resolve_interrupt_before_readback_retains_unknown_outcome(
    publication_cli: tuple[Path, _publish.PublishContext],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    structured: bool,
) -> None:
    _host_path, ctx = publication_cli
    _candidate.save_candidate(
        ctx._paths, ctx._project.name, replace(ctx._record, state=_candidate.STATE_UNKNOWN_OUTCOME)
    )

    def _interrupt_read(*_args: object) -> _host.RemoteRefs:
        if structured:
            raise OperationCancelled('operation cancelled')
        raise KeyboardInterrupt

    with monkeypatch.context() as fault:
        fault.setattr(_publish, 'ls_remote', _interrupt_read)
        assert _cli.main(['publish', '--project', ctx._project.name, '--resolve']) == 130
    error = capsys.readouterr().err
    assert 'publication outcome is unknown' in error
    assert f'pubanon publish --project={ctx._project.name} --resolve' in error
    assert 'Traceback' not in error
    assert _cli.main(['publish', '--project', ctx._project.name, '--resolve']) == 0
    record = _candidate.load_candidate(ctx._paths, ctx._project.name)
    assert record is not None
    assert record.state == _candidate.STATE_RETAINED


def test_resolve_reads_remote_exactly_once(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _publish._preserve_source(_context(tmp_path, source))
    unknown = replace(ctx._record, state=_candidate.STATE_UNKNOWN_OUTCOME)
    _candidate.save_candidate(ctx._paths, ctx._project.name, unknown)
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    _git_command(host, 'fetch', str(store), f'{unknown.commit}:refs/heads/main')
    reads: list[int] = []
    real_ls_remote = _publish.ls_remote

    def _counting(
        dest: _host.Destination,
        paths: Paths,
        project: str,
    ) -> _host.RemoteRefs:
        reads.append(1)
        return real_ls_remote(dest, paths, project)

    monkeypatch.setattr(_publish, 'ls_remote', _counting)

    assert _publish.resolve(replace(ctx, _record=unknown)) == 'published'
    assert len(reads) == 1


def test_resolve_refuses_account_mismatch_before_remote_read(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    unknown = replace(ctx._record, state=_candidate.STATE_UNKNOWN_OUTCOME)
    _candidate.save_candidate(ctx._paths, ctx._project.name, unknown)
    fake_gh.write_text(
        json.dumps(
            [
                {
                    'args': ['api', 'user', '--jq', '.login'],
                    'stdout': 'real-login-canary\n',
                }
            ]
        )
        + '\n',
        encoding='utf-8',
    )

    def _unexpected_remote_read(*_args: object, **_kwargs: object) -> _host.RemoteRefs:
        pytest.fail('resolve read remote refs before destination verification')

    monkeypatch.setattr(_publish, 'ls_remote', _unexpected_remote_read)

    with pytest.raises(PubanonError, match='authenticated account') as exc_info:
        _publish.resolve(replace(ctx, _record=unknown))

    rendered = exc_info.value.render()
    assert 'release-account' in rendered
    assert 'real-login-canary' in rendered
    assert _candidate.load_candidate(ctx._paths, ctx._project.name) == unknown


def test_resolve_refuses_unsupported_recorded_adapter_before_host_calls(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    ctx = _context(tmp_path, source)
    unknown = replace(
        ctx._record,
        destination=replace(
            ctx._record.destination,
            adapter='unsupported-adapter-canary',
        ),
        state=_candidate.STATE_UNKNOWN_OUTCOME,
    )
    _candidate.save_candidate(ctx._paths, ctx._project.name, unknown)
    loaded = _candidate.load_candidate(ctx._paths, ctx._project.name)
    assert loaded is not None

    def _unexpected_verification(*_args: object, **_kwargs: object) -> _host.Destination:
        pytest.fail('resolve verified an unsupported recorded adapter')

    def _unexpected_remote_read(*_args: object, **_kwargs: object) -> _host.RemoteRefs:
        pytest.fail('resolve read remote refs for an unsupported recorded adapter')

    monkeypatch.setattr(_host, '_verify_destination', _unexpected_verification)
    monkeypatch.setattr(_publish, 'ls_remote', _unexpected_remote_read)

    with pytest.raises(PubanonError, match='unsupported recorded destination adapter') as exc_info:
        _publish.resolve(replace(ctx, _record=loaded))

    assert 'unsupported-adapter-canary' not in exc_info.value.render()
    assert _candidate.load_candidate(ctx._paths, ctx._project.name) == unknown


def test_resolve_reports_effective_term_in_unrelated_branch_diagnostic(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    term = 'private-branch-name'
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source, extra_terms=(term,))
    unknown = replace(ctx._record, state=_candidate.STATE_UNKNOWN_OUTCOME)
    _candidate.save_candidate(ctx._paths, ctx._project.name, unknown)
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    _git_command(host, 'fetch', str(store), f'{unknown.commit}:refs/heads/{term}')

    assert _publish.resolve(replace(ctx, _record=unknown)) == 'not-sent'

    rendered = capsys.readouterr().out
    assert term in rendered


def test_resolve_reports_unexpected_destination_branch_and_retains_candidate(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    unknown = replace(ctx._record, state=_candidate.STATE_UNKNOWN_OUTCOME)
    _candidate.save_candidate(ctx._paths, ctx._project.name, unknown)
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    _git_command(host, 'fetch', str(store), f'{unknown.commit}:refs/heads/private-backup')

    assert _publish.resolve(replace(ctx, _record=unknown)) == 'not-sent'

    assert capsys.readouterr().out == (
        f'unrelated remote drift: branches: private-backup={unknown.commit}\n'
    )
    retained = _candidate.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert retained.state == _candidate.STATE_RETAINED


def test_resolve_uses_recorded_destination_when_configuration_drifted(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    unknown = replace(ctx._record, state=_candidate.STATE_UNKNOWN_OUTCOME)
    _candidate.save_candidate(ctx._paths, ctx._project.name, unknown)
    drifted = replace(
        ctx,
        _global_config=replace(ctx._global_config, accounts={'github': 'changed-account'}),
        _project=replace(
            ctx._project,
            owner='changed-owner',
            repo='changed-repo',
            visibility='private',
        ),
        _record=unknown,
    )

    assert _publish.resolve(drifted) == 'not-sent'

    retained = _candidate.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert retained.state == _candidate.STATE_RETAINED


@pytest.mark.parametrize('race', ['leased-tag', 'deletion-refused', 'mixed'])
def test_rewrite_atomic_transport_refuses_races_and_mixed_readback(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    race: str,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    host, ctx, originals = _rewrite_fixture(
        tmp_path,
        source,
        fake_gh,
        monkeypatch,
        selection=None if race == 'deletion-refused' else 1,
    )
    if race == 'deletion-refused':
        hook = host / 'hooks' / 'update'
        hook.parent.mkdir(exist_ok=True)
        hook.write_text(
            '#!/bin/sh\ncase "$3" in 000*) exit 1;; esac\n',
            encoding='utf-8',
            newline='\n',
        )
        hook.chmod(0o755)
    real_push = _publish.push
    calls: list[list[str]] = []

    def _push(dest: _host.Destination, store: Path, args: list[str]) -> None:
        calls.append(args)
        if race == 'leased-tag':
            _git_command(host, 'update-ref', 'refs/tags/v1.0.1', originals[0].commit)
        elif race == 'mixed':
            _git_command(
                host,
                'fetch',
                str(store),
                f'+{_records.candidate_tip(ctx._record)}:refs/heads/main',
            )
            return
        real_push(dest, store, args)

    monkeypatch.setattr(_publish, 'push', _push)
    with pytest.raises(PubanonError):
        _publish.publish(ctx)
    retained = _records.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    if race == 'deletion-refused':
        assert retained.state == _records.STATE_RETAINED
        assert _remote_ref(host, 'refs/tags/v1.0.0') == originals[0].commit
    else:
        assert retained.state == _records.STATE_UNKNOWN_OUTCOME
        with pytest.raises(PubanonError, match='indeterminate'):
            _publish.resolve(replace(ctx, _record=retained))
    if race != 'mixed':
        assert _remote_ref(host, 'refs/heads/main') == originals[-1].commit
    assert _remote_ref(host, 'refs/tags/v1.0.2') == originals[-1].commit
    assert len(calls) == 1
    assert not _candidate.confirmed_publication(ctx._paths, ctx._project.name, retained)


def test_rewrite_known_source_without_locator_remains_input_provenance(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    _, ctx, originals = _rewrite_fixture(tmp_path, source, fake_gh, monkeypatch)
    entry = replace(ctx._record.replayed[-1], original_bookmark=None)
    record = replace(ctx._record, replayed=(entry,))
    _records.save_candidate(ctx._paths, ctx._project.name, record)
    _publish.publish(replace(ctx, _record=record))
    derived = _records.load_publication(ctx._paths, ctx._project.name, entry.new)
    assert derived is not None
    assert derived.pinned is None
    assert derived.source_bookmark is None
    assert derived.derivation is not None
    assert derived.derivation.original_source == originals[-1].pinned.commit
    assert _remote_ref(source, _source.bookmark_ref(ctx._project.name, entry.new)) is None


@pytest.mark.parametrize('boundary', ['locator', 'bookmark', 'old-tip'])
def test_rewrite_preservation_failure_never_pushes_and_retains_locators(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    host, ctx, _ = _rewrite_fixture(tmp_path, source, fake_gh, monkeypatch)
    real_save, real_bookmark = _publish.save_candidate, _source.ensure_bookmark

    def _save(paths: Paths, name: str, record: _records.CandidateRecord) -> None:
        if any(entry.source_bookmark is not None for entry in record.replayed):
            raise OSError('locator persistence refused')
        real_save(paths, name, record)

    def bookmark(locator: _source.SourceBookmark, oid: str) -> None:
        retained = _records.load_candidate(ctx._paths, ctx._project.name)
        assert retained is not None
        if locator.refname.endswith(_records.candidate_tip(ctx._record)):
            assert retained.replayed[-1].source_bookmark == locator
            raise PubanonError('bookmark creation refused')
        real_bookmark(locator, oid)

    def _forbidden(*_args: object) -> None:
        pytest.fail('preservation failure must not send a push')

    def _archive(*_args: object) -> None:
        raise PubanonError('archive creation refused')

    monkeypatch.setattr(_publish, 'push', _forbidden)
    if boundary == 'locator':
        monkeypatch.setattr(_publish, 'save_candidate', _save)
    elif boundary == 'bookmark':
        monkeypatch.setattr(_source, 'ensure_bookmark', bookmark)
    else:
        monkeypatch.setattr(_history, 'preserve_previous_tip', _archive)
    with pytest.raises(PubanonError) as error:
        _publish.publish(ctx)
    assert 'No push was attempted' in error.value.render()
    retained = _records.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert retained.state == _records.STATE_RETAINED
    assert (retained.replayed[-1].source_bookmark is None) == (boundary == 'locator')
    assert _remote_ref(host, 'refs/heads/main') == ctx._record.prestate_main


@pytest.mark.parametrize(
    'boundary', ['tip-record', 'correction-record', 'derived-record', 'refs', 'unlink']
)
def test_rewrite_resolve_retains_tip_proof_after_interruption_and_remote_drift(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    host, ctx, _ = _rewrite_fixture(
        tmp_path,
        source,
        fake_gh,
        monkeypatch,
        selection=0 if boundary == 'derived-record' else 1,
    )
    real_save, real_git, real_unlink = _records.save_publication, _publish.run_git, Path.unlink
    pushes: list[list[str]] = []
    real_push = _publish.push

    def _push(dest: _host.Destination, store: Path, args: list[str]) -> None:
        pushes.append(args)
        real_push(dest, store, args)

    def _save(paths: Paths, project: str, record: _records.PublicationRecord) -> None:
        interrupted = {
            'tip-record': ctx._record.tip,
            'correction-record': ctx._record.commit,
            'derived-record': ctx._record.replayed[0].new,
        }[boundary]
        if record.commit == interrupted:
            raise OSError('record interrupted')
        real_save(paths, project, record)

    def _git(
        args: list[str], *, cwd: Path, input_bytes: bytes | None = None, check: bool = True
    ) -> GitResult:
        if args[2] == 'update-ref':
            raise OSError('refs interrupted')
        return real_git(args, cwd=cwd, input_bytes=input_bytes, check=check)

    def _unlink(path: Path, missing_ok: bool = False) -> None:
        if path == _record_path(ctx):
            raise OSError('candidate removal interrupted')
        real_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(_publish, 'push', _push)
    with monkeypatch.context() as fault:
        if boundary.endswith('record'):
            fault.setattr(_records, 'save_publication', _save)
        elif boundary == 'refs':
            fault.setattr(_publish, 'run_git', _git)
        else:
            fault.setattr(Path, 'unlink', _unlink)
        with pytest.raises(PubanonError, match='Local finalization is incomplete'):
            _publish.publish(ctx)
    retained = _records.load_candidate(ctx._paths, ctx._project.name)
    assert retained is not None
    assert _candidate.confirmed_publication(ctx._paths, ctx._project.name, retained)
    assert _candidate.publication_archive(
        ctx._paths, ctx._project.name, _records.candidate_tip(retained)
    )
    _git_command(host, 'update-ref', '-d', 'refs/heads/main')
    changed_project = replace(ctx._project, source_path=tmp_path / 'missing', repo='changed')
    with pytest.raises(PubanonError, match='must not be retried'):
        _publish.resolve(replace(ctx, _record=retained, _project=changed_project))
    assert len(pushes) == 1
    assert _records.load_candidate(ctx._paths, ctx._project.name) is None
    assert _candidate.published_refs(
        ctx._paths, ctx._project.name
    ) == _records.candidate_poststate(retained)


@pytest.mark.parametrize('all_history', [False, True])
def test_rewrite_transaction_preserves_old_chain_and_moves_only_changed_tags(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    fake_gh: Path,
    monkeypatch: pytest.MonkeyPatch,
    all_history: bool,
) -> None:
    source = make_repo(files={'README.md': 'original\n'})
    host, ctx, originals = _rewrite_fixture(
        tmp_path, source, fake_gh, monkeypatch, selection=None if all_history else 1
    )
    before = (
        _git_command(source, 'status', '--porcelain'),
        _git_command(source, 'rev-parse', 'HEAD'),
        _git_command(source, 'for-each-ref', 'refs/heads', 'refs/tags'),
        _git_command(source, 'ls-files', '--stage'),
        tuple(
            (path.name, path.read_bytes()) for path in sorted(source.iterdir()) if path.is_file()
        ),
    )
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    old_bytes = _git_command(store, 'cat-file', 'commit', originals[0].commit)
    if all_history:
        # A recovered tip has no record or archive; pretransport preservation must retain it.
        _records.publication_path(ctx._paths, ctx._project.name, originals[-1].commit).unlink()
        _git_command(store, 'update-ref', '-d', _records.publication_ref(originals[-1].commit))
    calls: list[list[str]] = []
    real_push = _publish.push

    def _push(dest: _host.Destination, path: Path, args: list[str]) -> None:
        assert _candidate.publication_archive(ctx._paths, ctx._project.name, originals[-1].commit)
        calls.append(args)
        real_push(dest, path, args)

    monkeypatch.setattr(_publish, 'push', _push)
    _publish.publish(ctx)
    tip, tags = _records.candidate_poststate(ctx._record)
    assert _remote_ref(host, 'refs/heads/main') == tip
    assert _candidate.published_refs(ctx._paths, ctx._project.name) == (tip, tags)
    assert _git_command(store, 'cat-file', 'commit', originals[0].commit) == old_bytes
    assert before == (
        _git_command(source, 'status', '--porcelain'),
        _git_command(source, 'rev-parse', 'HEAD'),
        _git_command(source, 'for-each-ref', 'refs/heads', 'refs/tags'),
        _git_command(source, 'ls-files', '--stage'),
        tuple(
            (path.name, path.read_bytes()) for path in sorted(source.iterdir()) if path.is_file()
        ),
    )
    assert len(calls) == 1
    if all_history:
        assert tags == {'v1.0.2': tip}
        assert ':refs/tags/v1.0.0' in calls[0]
        assert ':refs/tags/v1.0.1' in calls[0]
        assert _git_command(host, 'rev-list', '--parents', 'main').decode().strip() == tip
    else:
        assert all('refs/tags/v1.0.0' not in arg for arg in calls[0])
        assert _git_command(host, 'rev-parse', 'main~2').decode().strip() == originals[0].commit
        derived = _records.load_publication(ctx._paths, ctx._project.name, tip)
        assert derived is not None
        assert derived.pinned is None
        assert derived.derivation == _records.Derivation(
            originals[-1].commit,
            originals[-1].pinned.commit,
            ctx._record.commit,
            (),
        )
        assert derived.source_bookmark is not None
        assert originals[-1].source_bookmark is not None
        assert derived.source_bookmark.refname != originals[-1].source_bookmark.refname
        assert (
            _source.source_recovery(derived.source_bookmark, originals[-1].pinned.commit).status
            == 'protected'
        )


@pytest.mark.parametrize('drift', ['advanced', 'rewound', 'diverged', 'deleted'])
def test_source_drift_warns_without_changing_snapshot(
    drift: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = _drifted_context(drift, tmp_path, fake_gh, make_repo, monkeypatch)
    captured = StringIO()
    with redirect_stdout(captured):
        _verify_for_publish(ctx)
    assert captured.getvalue() == 'Warning: source has changed since snapshot was prepared.\n'
    assert _candidate.load_candidate(ctx._paths, ctx._project.name) == ctx._record


def test_sync_acknowledgment_displays_identity_and_hygiene_in_one_path(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    identity = 'historical-private-segment'
    hygiene = 'historical-hygiene-segment'
    path = f'docs/{identity}-{hygiene}.txt'
    source = make_repo(files={'README.md': 'first\n', path: 'public\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    first = _context(tmp_path, source, version='1.0.0')
    _publish.publish(first)
    _git_command(source, 'rm', '-q', '--', path)
    _second_source_commit(source)
    second = _context(tmp_path, source, version='2.0.0')
    _publish.publish(second)
    _git_command(host, 'update-ref', '-d', 'refs/heads/main')
    _git_command(host, 'update-ref', '-d', 'refs/tags/v1.0.0')
    _git_command(host, 'update-ref', '-d', 'refs/tags/v2.0.0')
    project = replace(
        second._project,
        extra_terms=(identity,),
        hygiene_terms=(hygiene,),
    )
    ctx = replace(second, _project=project)
    prompts: list[str] = []
    answers = [
        ('Continue despite this earlier exposure? [y/n] ', 'yes'),
        ('Push these references to public-owner/public-repo? [y/n] ', 'no'),
    ]

    def _answer(prompt: str) -> str:
        if not prompts:
            ordinary = capsys.readouterr().out
            assert identity in ordinary
            assert hygiene in ordinary
            assert 'entry 1' in ordinary
        prompts.append(prompt)
        assert answers, f'unexpected prompt: {prompt}'
        expected, response = answers.pop(0)
        assert expected == prompt
        return response

    monkeypatch.setattr('builtins.input', _answer)
    capsys.readouterr()

    with pytest.raises(PubanonError, match='synchronization declined'):
        _publish.sync(_sync_context(ctx, yes=False))

    assert not answers
    output = capsys.readouterr().out
    assert prompts == [
        'Continue despite this earlier exposure? [y/n] ',
        'Push these references to public-owner/public-repo? [y/n] ',
    ]
    assert 'Synchronization facts' in output


@pytest.mark.parametrize('status', [1, 7])
def test_sync_ancestry_failure_retains_classification_and_diagnostics(
    publication_cli: tuple[Path, _publish.PublishContext],
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    _host_path, ctx = publication_cli

    def _fail(args: list[str], **_kwargs: object) -> GitResult:
        return GitResult(status if args[2] == 'merge-base' else 0, b'', b'diagnostic\x1b\\path')

    monkeypatch.setattr(_publish, 'run_git', _fail)
    remote_commit = 'b' * 40
    with pytest.raises(PubanonError) as error:
        _verify_sync_prefix(
            _sync_context(ctx), ctx._record.commit, {}, _host.RemoteRefs(remote_commit, {})
        )
    assert str(error.value) == (
        'host main diverges from local published history'
        if status == 1
        else 'cannot classify host main ancestry'
    )
    assert error.value.fact == (None if status == 1 else r'exit status 7: diagnostic\x1b\\path')
    assert error.value.expected == ctx._record.commit
    assert error.value.actual == remote_commit
    assert error.value.recovery == 'pubanon repo verify'
    assert _candidate.load_candidate(ctx._paths, ctx._project.name) == ctx._record


def test_sync_declined_confirmation_leaves_remote_unchanged(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _source_path, host, first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )
    _git_command(host, 'update-ref', 'refs/heads/main', first._record.commit)

    capsys.readouterr()

    def _decline(_prompt: str) -> str:
        preview = capsys.readouterr().out
        assert f'{first._record.commit}..{second._record.commit}' in preview
        assert 'Sent commit count: 1' in preview
        assert 'Author: Anonymous Publisher <anonymous@example.invalid>' in preview
        assert second._record.message in preview
        assert f'{second._project.owner}/{second._project.repo}' in preview
        assert _remote_ref(host, 'refs/heads/main') == first._record.commit
        return 'no'

    monkeypatch.setattr('builtins.input', _decline)

    with pytest.raises(PubanonError, match='synchronization declined'):
        _publish.sync(_sync_context(second, yes=False))

    assert _remote_ref(host, 'refs/heads/main') == first._record.commit


def test_sync_empty_host_restores_full_history_and_tags(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _source_path, host, first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )
    _git_command(host, 'update-ref', '-d', 'refs/heads/main')
    _git_command(host, 'update-ref', '-d', 'refs/tags/v1.0.0')
    _git_command(host, 'update-ref', '-d', 'refs/tags/v2.0.0')
    capsys.readouterr()

    _publish.sync(_sync_context(second))

    assert _remote_ref(host, 'refs/heads/main') == second._record.commit
    assert _remote_ref(host, 'refs/tags/v1.0.0') == first._record.commit
    assert _remote_ref(host, 'refs/tags/v2.0.0') == second._record.commit
    assert _candidate.load_candidate(second._paths, second._project.name) is None
    output = capsys.readouterr().out
    assert f'{second._record.commit}:refs/heads/main' in output
    assert f'{first._record.commit}:refs/tags/v1.0.0' in output
    assert f'{second._record.commit}:refs/tags/v2.0.0' in output


def test_sync_equal_host_is_noop(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _source_path, _host_path, _first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )

    def _unexpected_push(*_args: object, **_kwargs: object) -> GitResult:
        pytest.fail('equal sync attempted a push')

    monkeypatch.setattr(_publish, 'push', _unexpected_push)
    capsys.readouterr()

    _publish.sync(_sync_context(second))

    assert capsys.readouterr().out == (
        'Host repository already matches the published history.\n'
        'No source, local, or host state was changed.\n\n'
    )


@pytest.mark.parametrize(
    'history',
    [
        ('empty', 'Anonymous Publisher'),
        ('empty', 'anonymous@example.invalid'),
        ('behind', 'Anonymous Publisher'),
        ('behind', 'anonymous@example.invalid'),
    ],
)
def test_sync_newly_sensitive_anonymous_identity_requires_explicit_acknowledgment(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    history: tuple[str, str],
) -> None:
    host_state, exposure = history
    _source_path, host, first, second = _published_history(
        tmp_path, fake_gh, make_repo, monkeypatch
    )
    if host_state == 'empty':
        for ref in ('refs/heads/main', 'refs/tags/v1.0.0', 'refs/tags/v2.0.0'):
            _git_command(host, 'update-ref', '-d', ref)
    else:
        _git_command(host, 'update-ref', 'refs/heads/main', first._record.commit)
        _git_command(host, 'update-ref', '-d', 'refs/tags/v2.0.0')
    before = _git_command(host, 'for-each-ref', '--format=%(objectname) %(refname)')
    project = replace(second._project, extra_terms=(exposure,))
    _config.save_project(second._paths, project)
    ctx = replace(second, _project=project)

    def _unexpected_prompt(_prompt: str) -> str:
        pytest.fail('unattended synchronization requested acknowledgment')

    monkeypatch.setattr('builtins.input', _unexpected_prompt)
    with pytest.raises(PubanonError, match='--yes cannot acknowledge historical exposure'):
        _publish.sync(_sync_context(ctx, yes=True, unattended=True))

    assert _git_command(host, 'for-each-ref', '--format=%(objectname) %(refname)') == before
    assert _config.load_project(ctx._paths, project.name).acknowledged_exposure == ''
    prompts: list[str] = []
    expected = [
        ('Continue despite this earlier exposure? [y/n] ', 'yes'),
        ('Push these references to public-owner/public-repo? [y/n] ', 'yes'),
    ]
    output_buffer = StringIO()

    def _accept(prompt: str) -> str:
        assert expected, f'unexpected prompt: {prompt}'
        expected_prompt, response = expected.pop(0)
        assert prompt == expected_prompt
        assert _git_command(host, 'for-each-ref', '--format=%(objectname) %(refname)') == before
        assert exposure in output_buffer.getvalue()
        prompts.append(prompt)
        return response

    monkeypatch.setattr('builtins.input', _accept)
    with redirect_stdout(output_buffer):
        _publish.sync(_sync_context(ctx, yes=False))

    assert expected == []
    assert len(prompts) == 2
    assert _remote_ref(host, 'refs/heads/main') == second._record.commit
    assert _remote_ref(host, 'refs/tags/v2.0.0') == second._record.commit
    assert _config.load_project(ctx._paths, project.name).acknowledged_exposure
    output = output_buffer.getvalue()
    assert 'Historical exposure review\n' in output
    for commit in (first._record.commit, second._record.commit):
        for field in ('author', 'committer'):
            assert output.count(f'{commit[:12]}:header:{field}') == 1
    assert exposure in output


def test_sync_preserves_retained_candidate_byte_for_byte_then_publishes_it(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, host, first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )
    _second_source_commit(source, 'third\n')
    retained = _context(tmp_path, source)
    record_path = _record_path(retained)
    before = record_path.read_bytes()
    _git_command(host, 'update-ref', 'refs/heads/main', first._record.commit)
    _git_command(host, 'update-ref', '-d', 'refs/tags/v2.0.0')

    _publish.sync(_sync_context(retained))

    assert record_path.read_bytes() == before
    assert _candidate.load_candidate(retained._paths, retained._project.name) == retained._record
    assert _remote_ref(host, 'refs/heads/main') == second._record.commit

    _publish.publish(retained)

    assert _remote_ref(host, 'refs/heads/main') == retained._record.commit
    assert not record_path.exists()


def test_sync_refuses_diverged_host_showing_both_tips_before_push(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source_path, host, _first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )
    tree = _git_command(host, 'mktree', input_bytes=b'').decode().strip()
    environment = {
        'GIT_AUTHOR_DATE': '1700000000 +0000',
        'GIT_AUTHOR_EMAIL': 'foreign@example.invalid',
        'GIT_AUTHOR_NAME': 'Foreign Author',
        'GIT_COMMITTER_DATE': '1700000000 +0000',
        'GIT_COMMITTER_EMAIL': 'foreign@example.invalid',
        'GIT_COMMITTER_NAME': 'Foreign Author',
    }
    foreign = (
        _git_command(
            host,
            'commit-tree',
            tree,
            env=environment,
            input_bytes=b'foreign\n',
        )
        .decode()
        .strip()
    )
    _git_command(host, 'update-ref', 'refs/heads/main', foreign)

    def _unexpected_push(*_args: object, **_kwargs: object) -> GitResult:
        pytest.fail('diverged sync attempted a push')

    monkeypatch.setattr(_publish, 'push', _unexpected_push)

    with pytest.raises(PubanonError, match='diverg') as error:
        _publish.sync(_sync_context(second))

    rendered = error.value.render()
    assert foreign in rendered
    assert second._record.commit in rendered
    assert error.value.recovery == 'pubanon repo verify'


def test_sync_refuses_mismatched_readback_with_complete_refs(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source_path, host, first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )
    _git_command(host, 'update-ref', 'refs/heads/main', first._record.commit)
    _git_command(host, 'update-ref', '-d', 'refs/tags/v2.0.0')
    local_main, local_tags = _candidate.published_refs(second._paths, second._project.name)
    assert local_main == second._record.commit
    prestate = _host.RemoteRefs(
        first._record.commit,
        {'v1.0.0': first._record.commit},
        (),
    )
    branch = 'private-readback-branch'
    mismatched = _host.RemoteRefs(
        local_main,
        local_tags,
        ((branch, first._record.commit),),
    )
    responses = (prestate, mismatched)
    reads: list[None] = []

    def _remote_refs(
        _destination: _host.Destination,
        _paths: Paths,
        _project: str,
    ) -> _host.RemoteRefs:
        if len(reads) == len(responses):
            pytest.fail('sync read the remote more than twice')
        response = responses[len(reads)]
        reads.append(None)
        return response

    monkeypatch.setattr(_publish, 'ls_remote', _remote_refs)
    masked_ctx = replace(
        second, _project=replace(second._project, extra_terms=(first._record.commit, branch))
    )

    with pytest.raises(PubanonError, match='after synchronization') as error:
        _publish.sync(_sync_context(masked_ctx))

    assert len(reads) == 2
    assert error.value.expected is not None
    assert error.value.actual is not None
    assert first._record.commit in error.value.expected
    assert branch in error.value.actual
    assert first._record.commit in error.value.expected
    assert first._record.commit in error.value.actual
    assert branch in error.value.actual
    assert error.value.recovery == 'rerun pubanon publish --sync'


def test_sync_refuses_missing_local_published_main(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    _candidate.discard(ctx._paths, ctx._project.name, force=False)

    with pytest.raises(PubanonError, match='local published history') as error:
        _publish.sync(_sync_context(ctx))

    assert error.value.recovery == 'pubanon repo verify --repair-store'


def test_sync_refuses_structural_problem_outside_exact_range_before_preview(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _source_path, host, first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )
    store = _candidate.store_path(second._paths, second._project.name)
    tree = _git_command(store, 'rev-parse', f'{second._record.commit}^{{tree}}').decode().strip()
    invalid_root = _literal_history_commit(
        store,
        tree,
        author_name='Foreign Author',
        message=b'invalid root\n',
    )
    valid_tip = _literal_history_commit(
        store,
        tree,
        message=b'valid tip\n',
        parent=invalid_root,
    )
    _git_command(store, 'update-ref', 'refs/heads/main', valid_tip)
    _git_command(store, 'update-ref', 'refs/tags/v1.0.0', invalid_root)
    _git_command(store, 'update-ref', 'refs/tags/v2.0.0', valid_tip)
    _git_command(host, 'fetch', '-q', str(store), f'+{invalid_root}:refs/heads/main')
    _git_command(host, 'fetch', '-q', str(store), f'+{invalid_root}:refs/tags/v1.0.0')
    _git_command(host, 'update-ref', '-d', 'refs/tags/v2.0.0')
    remote_before = _remote_ref(host, 'refs/heads/main')

    def _unexpected_push(*_args: object, **_kwargs: object) -> GitResult:
        pytest.fail('structural sync failure attempted a push')

    monkeypatch.setattr(_publish, 'push', _unexpected_push)
    capsys.readouterr()

    with pytest.raises(PubanonError, match='published history has identity problems'):
        _publish.sync(_sync_context(second))

    assert 'Synchronization preview:' not in capsys.readouterr().out
    assert _remote_ref(host, 'refs/heads/main') == remote_before
    assert remote_before == invalid_root
    assert first._record.commit != invalid_root


def test_sync_refuses_unknown_outcome_candidate_naming_resolve(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, _host_path, _first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )
    _second_source_commit(source, 'third\n')
    retained = _context(tmp_path, source)
    _candidate.save_candidate(
        retained._paths,
        retained._project.name,
        replace(retained._record, state=_candidate.STATE_UNKNOWN_OUTCOME),
    )

    def _refuse_push(*_args: object) -> None:
        pytest.fail('unknown publication outcome attempted a sync push')

    monkeypatch.setattr(_publish, 'push', _refuse_push)
    with pytest.raises(PubanonError) as error:
        _publish.sync(_sync_context(second))

    assert error.value.recovery == 'pubanon publish --resolve'
    saved = _candidate.load_candidate(retained._paths, retained._project.name)
    assert saved is not None
    assert saved.state == _candidate.STATE_UNKNOWN_OUTCOME


def test_sync_refuses_unknown_remote_tag(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _source_path, host, _first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )
    _git_command(host, 'update-ref', 'refs/tags/foreign', second._record.commit)

    with pytest.raises(PubanonError, match='remote tag') as error:
        _publish.sync(_sync_context(second))

    rendered = error.value.render()
    assert 'foreign' in rendered
    assert 'absent' in rendered


def test_sync_reports_hygiene_only_path_finding(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    term = 'new-hygiene-path'
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    first = _context(tmp_path, source, version='1.0.0')
    _publish.publish(first)
    path = f'docs/{term}.txt'
    (source / 'docs').mkdir()
    (source / path).write_text('public\n')
    _git_command(source, 'add', path)
    _git_command(
        source,
        '-c',
        'user.name=Test User',
        '-c',
        'user.email=test@example.invalid',
        'commit',
        '-qm',
        'second',
    )
    second = _context(tmp_path, source, version='2.0.0')
    _publish.publish(second)
    _git_command(host, 'update-ref', 'refs/heads/main', first._record.commit)
    _git_command(host, 'update-ref', '-d', 'refs/tags/v2.0.0')
    project = replace(second._project, hygiene_terms=(term,))
    capsys.readouterr()

    _publish.sync(_sync_context(replace(second, _project=project)))

    output = capsys.readouterr().out
    assert f'policy mismatch: path:docs/{term}.txt' in output
    assert term in output


def test_sync_reports_unexpected_branch_before_noop_or_push(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    term = 'private-branch-canary'
    _source_path, host, _first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )
    _git_command(host, 'update-ref', f'refs/heads/{term}', second._record.commit)
    project = replace(second._project, extra_terms=(term,))
    ctx = replace(second, _project=project)

    def _unexpected_push(*_args: object, **_kwargs: object) -> GitResult:
        pytest.fail('branch-bearing sync attempted a push')

    monkeypatch.setattr(_publish, 'push', _unexpected_push)

    with pytest.raises(PubanonError, match='unexpected branch') as error:
        _publish.sync(_sync_context(ctx))

    rendered = error.value.render()
    assert term in rendered


def test_sync_sent_exposure_requires_interactive_acknowledgment(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exposure = 'historical-private-canary'
    _source_path, host, _first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
        first_content=f'{exposure}\n',
    )
    _git_command(host, 'update-ref', '-d', 'refs/heads/main')
    _git_command(host, 'update-ref', '-d', 'refs/tags/v1.0.0')
    _git_command(host, 'update-ref', '-d', 'refs/tags/v2.0.0')
    project = replace(second._project, extra_terms=(exposure,))
    _config.save_project(second._paths, project)
    ctx = replace(second, _project=project)
    prompts: list[str] = []
    expected = [
        'Continue despite this earlier exposure? [y/n] ',
        'Push these references to public-owner/public-repo? [y/n] ',
    ]

    def _accept(prompt: str) -> str:
        assert expected, f'unexpected prompt: {prompt}'
        assert expected.pop(0) == prompt
        prompts.append(prompt)
        return 'yes'

    monkeypatch.setattr('builtins.input', _accept)

    _publish.sync(_sync_context(ctx, yes=False))

    assert not expected
    assert prompts == [
        'Continue despite this earlier exposure? [y/n] ',
        'Push these references to public-owner/public-repo? [y/n] ',
    ]
    assert _config.load_project(ctx._paths, ctx._project.name).acknowledged_exposure


def test_sync_strict_prefix_audits_and_pushes_exact_missing_range(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _source_path, host, first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
    )
    _git_command(host, 'update-ref', 'refs/heads/main', first._record.commit)
    _git_command(host, 'update-ref', '-d', 'refs/tags/v2.0.0')
    audits: list[
        tuple[
            tuple[str, ...],
            dict[str, str],
            frozenset[str],
            dict[str, str],
        ]
    ] = []
    real_audit = cast(
        'Callable[..., tuple[_audit.HistoryAudit, _audit.HistoryAudit]]',
        _audit.audit_history_pair,
    )
    real_push = _publish.push
    pushed_args: list[str] = []

    def _audit_range(
        *args: object, **kwargs: object
    ) -> tuple[_audit.HistoryAudit, _audit.HistoryAudit]:
        audits.append(
            (
                cast('tuple[str, ...]', args[2]),
                dict(cast('dict[str, str]', args[3])),
                frozenset(cast('frozenset[str]', args[4])),
                dict(cast('dict[str, str]', args[5])),
            )
        )
        return real_audit(*args, **kwargs)

    def _record_push(
        destination: _host.Destination,
        cwd: Path,
        args: list[str],
    ) -> GitResult:
        pushed_args.extend(args)
        return real_push(destination, cwd, args)

    monkeypatch.setattr(_audit, 'audit_history_pair', _audit_range)
    monkeypatch.setattr(_publish, 'push', _record_push)
    capsys.readouterr()

    _publish.sync(_sync_context(second))

    assert audits == [
        (
            (second._record.commit, first._record.commit),
            {
                'v1.0.0': first._record.commit,
                'v2.0.0': second._record.commit,
            },
            frozenset({second._record.commit}),
            {'v2.0.0': second._record.commit},
        ),
    ]
    assert pushed_args == [
        '--atomic',
        f'--force-with-lease=refs/heads/main:{first._record.commit}',
        '--force-with-lease=refs/tags/v2.0.0:',
        '--no-follow-tags',
        f'{second._record.commit}:refs/heads/main',
        f'{second._record.commit}:refs/tags/v2.0.0',
    ]
    output = capsys.readouterr().out
    assert f'--force-with-lease=refs/heads/main:{first._record.commit}' in output
    assert '--force-with-lease=refs/tags/v2.0.0:' in output
    assert f'{second._record.commit}:refs/heads/main' in output
    assert f'{second._record.commit}:refs/tags/v2.0.0' in output


def test_sync_suffix_preserves_full_history_acknowledgment_bytes(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exposure = 'historical-private-canary'
    source = make_repo(files={'README.md': f'{exposure}\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    first = _context(tmp_path, source, version='1.0.0')
    _publish.publish(first)
    project = replace(first._project, extra_terms=(exposure,))
    _config.save_project(first._paths, project)
    _second_source_commit(source)
    second = _context(tmp_path, source, extra_terms=(exposure,), version='2.0.0')

    def _accept_publication(_prompt: str) -> str:
        return 'yes'

    monkeypatch.setattr('builtins.input', _accept_publication)
    _publish.publish(replace(second, _yes=False))
    project_path = second._paths.config_dir / 'projects' / f'{second._project.name}.toml'
    before = project_path.read_bytes()
    _git_command(host, 'update-ref', 'refs/heads/main', first._record.commit)
    _git_command(host, 'update-ref', '-d', 'refs/tags/v2.0.0')
    prompts: list[str] = []

    def _accept_push(prompt: str) -> str:
        prompts.append(prompt)
        return 'yes'

    monkeypatch.setattr('builtins.input', _accept_push)
    saved = _config.load_project(second._paths, second._project.name)

    _publish.sync(_sync_context(replace(second, _project=saved), yes=False))

    assert prompts == ['Push these references to public-owner/public-repo? [y/n] ']
    assert project_path.read_bytes() == before


def test_sync_yes_refuses_unacknowledged_sent_exposure(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    exposure = 'historical-private-canary'
    _source_path, host, _first, second = _published_history(
        tmp_path,
        fake_gh,
        make_repo,
        monkeypatch,
        first_content=f'{exposure}\n',
    )
    _git_command(host, 'update-ref', '-d', 'refs/heads/main')
    project = replace(second._project, extra_terms=(exposure,))
    ctx = replace(second, _project=project)

    with pytest.raises(
        PubanonError,
        match='--yes cannot acknowledge historical exposure',
    ):
        _publish.sync(_sync_context(ctx, unattended=True))

    assert _remote_ref(host, 'refs/heads/main') is None


def test_verify_explicit_revision_skips_ref_drift_acknowledgment(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    commit = _git_command(source, 'rev-parse', 'HEAD').decode().strip()
    ctx = _context(tmp_path, source, selector=commit)
    _second_source_commit(source)

    def _unexpected_prompt(_prompt: str) -> str:
        raise AssertionError('explicit revision must not prompt for ref drift')

    monkeypatch.setattr('builtins.input', _unexpected_prompt)

    verified = _verify_for_publish(ctx)

    assert verified._remote == _host.RemoteRefs(None, {})


def test_verify_refuses_changed_publication_fingerprint(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    changed = replace(ctx._global_config, sensitive_terms=('Different Person',))

    with pytest.raises(PubanonError, match='configuration changed') as error:
        _verify_for_publish(replace(ctx, _global_config=changed))

    assert error.value.recovery == 'pubanon discard, then pubanon prepare'


@pytest.mark.parametrize('drift', ['advanced', 'rewound', 'diverged', 'deleted'])
def test_verify_refuses_ref_drift_under_yes_without_prompt(
    drift: str,
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx = replace(
        _drifted_context(
            drift,
            tmp_path,
            fake_gh,
            make_repo,
            monkeypatch,
        ),
        _unattended=True,
    )

    def _unexpected_prompt(_prompt: str) -> str:
        raise AssertionError('--yes must not prompt for source-ref drift')

    monkeypatch.setattr('builtins.input', _unexpected_prompt)

    with pytest.raises(
        PubanonError, match='--yes cannot approve publishing after the selected source changed'
    ) as error:
        _verify_for_publish(ctx)

    assert error.value.recovery == 'rerun pubanon publish in an interactive terminal without --yes'


def test_verify_refuses_tampered_candidate_commit_author(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'first\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)
    store = _candidate.store_path(ctx._paths, ctx._project.name)
    timestamp = (
        _git_command(store, 'show', '-s', '--format=%at', ctx._record.commit).decode().strip()
    )
    env = {
        'GIT_AUTHOR_DATE': f'{timestamp} +0000',
        'GIT_AUTHOR_EMAIL': 'tampered@example.invalid',
        'GIT_AUTHOR_NAME': 'Tampered Author',
        'GIT_COMMITTER_DATE': f'{timestamp} +0000',
        'GIT_COMMITTER_EMAIL': ctx._global_config.anon_email,
        'GIT_COMMITTER_NAME': ctx._global_config.anon_name,
    }
    tampered = (
        _git_command(
            store,
            'commit-tree',
            ctx._record.tree,
            env=env,
            input_bytes=ctx._record.message.encode() + b'\n',
        )
        .decode()
        .strip()
    )
    record = replace(ctx._record, commit=tampered)
    _git_command(store, 'update-ref', _candidate.CANDIDATE_REF, tampered)
    _candidate.save_candidate(ctx._paths, ctx._project.name, record)

    with pytest.raises(PubanonError, match='snapshot commit metadata'):
        _verify_for_publish(replace(ctx, _record=record))


def test_verify_reruns_content_gates_over_retained_bytes(
    tmp_path: Path,
    fake_gh: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = make_repo(files={'README.md': 'Private Person\n'})
    host = _new_host(tmp_path)
    _configure_host(host, fake_gh, monkeypatch)
    ctx = _context(tmp_path, source)

    with pytest.raises(PubanonError, match='current content gates'):
        _verify_for_publish(ctx)
