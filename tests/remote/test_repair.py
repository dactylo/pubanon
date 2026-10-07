from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from pubanon._config import Paths, fingerprint
from pubanon._errors import OperationCanceled, PubanonError
from pubanon._remote import _host, _publish, _repair
from pubanon._run import GitResult
from pubanon._snapshots import (
    _audit,
    _candidate,
    _discard,
    _history,
    _records,
    _source,
    _store,
    _verification,
)
from tests._git import remove_tree
from tests.remote.test_publish import (
    _configs as _publish_configs,
)
from tests.remote.test_publish import (
    _configure_host,
    _context,
    _git_command,
    _literal_history_commit,
    _new_host,
    _remote_ref,
    _second_source_commit,
)
from tests.remote.test_publish import (
    _paths as _publish_paths,
)


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


@pytest.mark.slow
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
    store = _store.store_path(first._paths, first._project.name)
    remove_tree(store)
    destination = _host.verify_destination(first._paths, first._global_config, first._project)

    _repair.rebuild_store(
        first._paths, (first._global_config, first._project), destination, yes=True
    )

    assert _remote_ref(store, 'refs/heads/main') == first._record.commit
    _second_source_commit(source)
    second = _context(tmp_path, source)
    _publish.publish(second)
    assert _remote_ref(host, 'refs/heads/main') == second._record.commit


@pytest.mark.slow
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
    store = _store.store_path(ctx._paths, ctx._project.name)
    destination = _host.verify_destination(ctx._paths, ctx._global_config, ctx._project)

    def _fail_fetch(
        _destination: _host.Destination,
        _store: Path,
        _refspecs: list[str],
    ) -> GitResult:
        raise PubanonError('simulated fetch failure')

    monkeypatch.setattr(_host, 'fetch', _fail_fetch)

    with pytest.raises(PubanonError, match='simulated fetch failure'):
        _repair.rebuild_store(
            ctx._paths, (ctx._global_config, ctx._project), destination, yes=True
        )

    assert _remote_ref(store, 'refs/heads/main') == ctx._record.commit
    _git_command(store, 'cat-file', '-e', f'{ctx._record.commit}^{{commit}}')


@pytest.mark.slow
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
    store = _store.store_path(first._paths, first._project.name)
    _second_source_commit(source)
    second = _context(tmp_path, source)
    _git_command(
        store,
        'push',
        str(host),
        f'{second._record.commit}:refs/heads/main',
        f'{second._record.commit}:refs/tags/v2.0.0',
    )
    _discard.discard(first._paths, first._project.name, force=False)
    _git_command(store, 'update-ref', 'refs/tags/local-only', first._record.commit)
    destination = _host.verify_destination(first._paths, first._global_config, first._project)

    result = _repair.rebuild_store(
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


@pytest.mark.slow
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
    store = _store.store_path(ctx._paths, ctx._project.name)
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

    result = _repair.rebuild_store(
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


@pytest.mark.slow
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
    store = _store.store_path(first._paths, first._project.name)
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
    with pytest.raises(OperationCanceled, match=r'Canceled\. Nothing was restored.'):
        _repair.rebuild_store(
            first._paths, (first._global_config, first._project), destination, yes=False
        )
    assert not answers
    assert _git_command(store, 'for-each-ref') == local_before
    assert _git_command(host, 'for-each-ref') == host_before


@pytest.mark.slow
def test_repair_noop_avoids_consent_and_managed_ref_writes(
    fake_gh: Path, make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx, destination, host = _published_repair_context(fake_gh, make_repo, monkeypatch)
    store = _store.store_path(ctx._paths, ctx._project.name)
    local_before, host_before = (
        _git_command(store, 'for-each-ref'),
        _git_command(host, 'for-each-ref'),
    )

    def _refuse_action(*_args: object) -> None:
        pytest.fail('no-op repair requested consent or wrote managed refs')

    monkeypatch.setattr('builtins.input', _refuse_action)
    monkeypatch.setattr(_repair, '_install_repair_refs', _refuse_action)
    result = _repair.rebuild_store(
        ctx._paths, (ctx._global_config, ctx._project), destination, yes=False
    )
    assert result.refs == _host.RemoteRefs(ctx._record.commit, {})
    assert _git_command(store, 'for-each-ref') == local_before
    assert _git_command(host, 'for-each-ref') == host_before


@pytest.mark.slow
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
    store = _store.store_path(ctx._paths, ctx._project.name)
    tree = _git_command(host, 'rev-parse', 'main^{tree}').decode().strip()
    advanced = _literal_history_commit(host, tree, message=b'advance\n', parent=ctx._record.commit)
    _git_command(host, 'update-ref', 'refs/heads/main', advanced)
    _git_command(host, 'update-ref', 'refs/tags/v2.0.0', advanced)
    _git_command(store, 'update-ref', 'refs/tags/obsolete', ctx._record.commit)
    destination = _host.verify_destination(ctx._paths, ctx._global_config, ctx._project)
    real_install = _repair._install_repair_refs
    expected = b''

    def _install(repo: Path, before: _host.RemoteRefs, fetched: _host.RemoteRefs) -> None:
        nonlocal expected
        if failure == 'locked-tag':
            (store / 'refs/tags/v2.0.0.lock').write_text('locked')
        else:
            _git_command(store, 'update-ref', 'refs/heads/main', advanced)
        expected = _git_command(store, 'for-each-ref', 'refs/heads/', 'refs/tags/')
        real_install(repo, before, fetched)

    monkeypatch.setattr(_repair, '_install_repair_refs', _install)
    with pytest.raises(PubanonError):
        _repair.rebuild_store(
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
) -> None:
    paths = _publish_paths(tmp_path)
    global_config, project = _publish_configs(tmp_path / 'source')
    record = _records.CandidateRecord(
        commit='a' * 40,
        tree='b' * 40,
        snapshot_digest='c' * 64,
        tag_intent=None,
        fingerprint=fingerprint(global_config, project),
        pinned=_source.PinnedSource('d' * 40, 'ref', 'refs/heads/main', 'd' * 40),
        destination=_records.RecordedDestination(
            project.host,
            project.owner,
            project.repo,
            project.visibility,
            global_config.accounts[project.host],
        ),
        prestate_main=None,
        prestate_tags={},
        state=state,
        message='Public snapshot',
    )
    _candidate.save_candidate(paths, project.name, record)
    store = _store.candidate_store_path(paths, project.name)
    destination = _host.Destination(
        project.host,
        project.owner,
        project.repo,
        'https://example.invalid/public-repo.git',
        global_config.accounts[project.host],
        project.visibility,
    )
    assert not store.exists()

    with pytest.raises(PubanonError, match='snapshot'):
        _repair.rebuild_store(paths, (global_config, project), destination, yes=True)

    assert not store.exists()
    assert _records.load_candidate(paths, project.name) == record


@pytest.mark.slow
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
    store = _store.store_path(ctx._paths, ctx._project.name)
    before = _git_command(store, 'for-each-ref', 'refs/heads/', 'refs/tags/')
    tree = _git_command(host, 'rev-parse', 'main^{tree}').decode().strip()
    advanced = _literal_history_commit(host, tree, message=b'advance\n', parent=ctx._record.commit)
    _git_command(host, 'update-ref', 'refs/heads/main', advanced)
    _git_command(host, 'update-ref', 'refs/tags/v2.0.0', advanced)
    destination = _host.verify_destination(ctx._paths, ctx._global_config, ctx._project)
    real_ls_remote = _host.ls_remote
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

    monkeypatch.setattr(_host, 'ls_remote', _observe)
    with pytest.raises(PubanonError) as error:
        _repair.rebuild_store(
            ctx._paths, (ctx._global_config, ctx._project), destination, yes=True
        )

    if boundary.startswith('pre'):
        assert 'managed refs were not replaced' in error.value.render()
        assert _git_command(store, 'for-each-ref', 'refs/heads/', 'refs/tags/') == before
    else:
        assert 'local refs may have changed' in error.value.render()
        assert 'pubanon repo restore --to local' in error.value.render()
        assert _remote_ref(store, 'refs/heads/main') == advanced
        assert _remote_ref(store, 'refs/tags/v2.0.0') == advanced
    assert _git_command(store, 'for-each-ref', 'refs/pubanon/repair/') == b''


@pytest.mark.parametrize(
    'case',
    [
        (
            _candidate.STATE_RETAINED,
            'pubanon discard, then pubanon repo restore --to local',
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
    store = _store.store_path(ctx._paths, ctx._project.name)
    inspection = _verification.materialize_inspect(
        ctx._paths,
        ctx._project.name,
        _verification.prove_objects(ctx._paths, ctx._project.name, record),
    )
    inspected = (inspection / 'README.md').read_bytes()

    with pytest.raises(PubanonError, match='snapshot') as error:
        _repair.rebuild_store(
            ctx._paths, (ctx._global_config, ctx._project), destination, yes=True
        )

    assert error.value.recovery == recovery
    if state == _candidate.STATE_RETAINED:
        assert 'a later pubanon prepare rebuilds the discarded snapshot' in error.value.render()
    assert _records.load_candidate(ctx._paths, ctx._project.name) == record
    assert _remote_ref(store, _candidate.CANDIDATE_REF) == record.commit
    _git_command(store, 'cat-file', '-e', f'{record.commit}^{{commit}}')
    assert (inspection / 'README.md').read_bytes() == inspected


@pytest.mark.slow
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
    store = _store.store_path(ctx._paths, ctx._project.name)
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
        _repair.rebuild_store(
            ctx._paths, (ctx._global_config, ctx._project), destination, yes=True
        )

    assert _git_command(store, 'for-each-ref', 'refs/heads/', 'refs/tags/') == before
    assert _git_command(store, 'for-each-ref', 'refs/pubanon/repair/') == b''


@pytest.mark.slow
@pytest.mark.parametrize('decision', ['host-moved', 'local-moved', 'cancel'])
def test_repair_consent_preserves_ref_leases_and_cancellation(
    fake_gh: Path, make_repo: Callable[..., Path], monkeypatch: pytest.MonkeyPatch, decision: str
) -> None:
    ctx, destination, host = _published_repair_context(fake_gh, make_repo, monkeypatch)
    store = _store.store_path(ctx._paths, ctx._project.name)
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
        _repair.rebuild_store(
            ctx._paths, (ctx._global_config, ctx._project), destination, yes=False
        )
    assert not answers
    if decision == 'host-moved':
        assert 'host refs changed during consent' in error.value.render()
    elif decision == 'cancel':
        assert isinstance(error.value, OperationCanceled)
    if decision != 'local-moved':
        assert _git_command(store, 'for-each-ref') == local_before
    else:
        assert _remote_ref(store, 'refs/tags/local-only') == changed_oid
        assert _remote_ref(store, 'refs/heads/main') == ctx._record.commit
    if decision != 'host-moved':
        assert _git_command(host, 'for-each-ref') == host_before
    assert _git_command(store, 'for-each-ref', 'refs/pubanon/repair/') == b''


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

    monkeypatch.setattr(_repair, 'run_git', _read)
    with pytest.raises(PubanonError):
        _repair._repair_refs(tmp_path)
