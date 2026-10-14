import re
import shlex
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest

from pubanon import _filesystem
from pubanon._cli import _arguments
from pubanon._run import run_git
from pubanon._snapshots import (
    _candidate,
    _records,
    _reports,
    _store,
    _verification,
)
from pubanon._snapshots._source import (
    Entry,
    PinnedSource,
)
from pubanon._terminal import escape, verbose_context
from tests._permissions import assert_private_mode
from tests.snapshots.test_candidate import (
    _build,
    _configs,
    _paths,
    _pinned,
    _snapshot,
)
from tests.snapshots.test_verification import _complete_replacement


def _reprepare_record() -> _records.CandidateRecord:
    return _records.CandidateRecord(
        commit='a' * 40,
        tree='b' * 40,
        snapshot_digest='c' * 64,
        tag_intent=None,
        fingerprint='d' * 64,
        pinned=PinnedSource('e' * 40, 'explicit', None, None),
        destination=_records.RecordedDestination('github', 'owner', 'repo', 'private', 'account'),
        prestate_main=None,
        prestate_tags={},
        state=_candidate.STATE_RETAINED,
        message='release',
    )


def test_inspect_report_bounds_file_list_and_saves_complete_private_report(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    global_config, project = _configs(tmp_path)
    snapshot = tuple(Entry(f'file-{i:03}.txt', 0o100644, 'blob', b'line\n') for i in range(30))
    record = _candidate.build_candidate(
        paths, global_config, project, _candidate.CandidateInputs(snapshot, _pinned(), None, None)
    )

    report = _reports.inspect_report(
        paths, _verification.prove_objects(paths, project.name, record), global_config, project
    )

    assert '30 files changed' in report
    assert 'file-000.txt' in report
    assert 'file-029.txt' not in report
    complete = paths.data_dir / 'projects' / project.name / f'candidate-files-{record.commit}.txt'
    assert escape((complete).as_posix()) in report
    assert_private_mode(complete, 0o600)
    assert 'file-000.txt' in complete.read_text()
    assert 'file-029.txt' in complete.read_text()
    assert 'Snapshot untagged (prepared)' in report
    assert 'Checks:' in report


def test_inspect_report_marks_changed_configuration_without_claiming_current_gates(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    global_config, project_config = _configs(tmp_path)
    record = _candidate.build_candidate(
        paths,
        global_config,
        project_config,
        _candidate.CandidateInputs(_snapshot(), _pinned(), '1.2.3', None),
    )
    changed = replace(project_config, extra_terms=('new-private-term',))

    report = _reports.inspect_report(
        paths, _verification.prove_objects(paths, changed.name, record), global_config, changed
    )

    assert 'no sensitive terms found when prepared' in report
    assert 'snapshot verified' in report
    assert 'Publication configuration: changed since prepare' in report
    assert 'checks have not been rerun with these settings' in report
    assert 'snapshot cannot publish' in report
    assert (
        f'pubanon prepare --project={project_config.name} --source {record.pinned.commit}'
        in _reports.inspect_next_action(record, global_config, changed)
    )
    assert record.tag_intent == 'v1.2.3'
    assert '--version 1.2.3' in _reports.inspect_next_action(record, global_config, changed)


def test_inspect_report_renders_first_release_facts_and_credential_exceptions(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    global_config, project_config = _configs(tmp_path)
    project_config = replace(project_config, secret_exceptions=('canaryx.env',))
    record = _candidate.build_candidate(
        paths,
        global_config,
        project_config,
        _candidate.CandidateInputs(
            _snapshot(), _pinned(), '1.2.3', 'release: v1.2.3\n\nDetails: forged\x1b\rlabel'
        ),
    )

    report = _reports.inspect_report(
        paths,
        _verification.prove_objects(paths, project_config.name, record),
        global_config,
        project_config,
    )

    assert report.startswith('Snapshot ')
    assert 'no sensitive terms found when prepared' in report
    assert 'snapshot verified' in report
    assert 'Publication configuration:' not in report
    assert 'GitHub not rechecked' in report
    assert _reports.inspect_next_action(record, global_config, project_config) == (
        'pubanon publish'
    )
    assert 'Snapshot commit:' not in report
    assert 'Source commit:' not in report
    assert 'Anonymous Publisher <anonymous@example.invalid>' in report
    assert 'Message:' in report
    assert '  release: v1.2.3\n  \n' in report
    assert r'  Details: forged\x1b\x0dlabel' in report
    assert 'Destination: public-owner/public-repo (public)' in report
    assert 'Snapshot v1.2.3 (prepared)' in report
    assert 'first release, 2 files' in report
    assert '2 files changed' in report
    assert 'credential-like' not in report
    assert 'canaryx' not in report

    changed_report = _reports.inspect_report(
        paths,
        _verification.prove_objects(paths, project_config.name, record),
        replace(global_config, anon_name='Replacement Publisher'),
        replace(project_config, owner='replacement-owner', visibility='private'),
    )
    assert 'Anonymous Publisher <anonymous@example.invalid>' in changed_report
    assert re.search(r'Destination:\s+public-owner/public-repo \(public\)', changed_report)
    assert 'checks have not been rerun with these settings' in changed_report
    assert 'Replacement Publisher' not in changed_report


def test_inspect_report_renders_untrusted_fields_without_escaping_trusted_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    global_config, project_config = _configs(tmp_path)
    term = 'private-canary'
    project_config = replace(
        project_config,
        secret_exceptions=(f'fixtures/allowed\x1b{term}.key',),
    )
    record = _candidate.build_candidate(
        paths,
        global_config,
        project_config,
        _candidate.CandidateInputs(_snapshot(), _pinned(), None, None),
    )

    real_git_result = _candidate.git_result

    def _git_result(
        _args: Sequence[str],
        *,
        store: Path,
        failure: str,
        input_bytes: bytes | None = None,
        env_extra: Mapping[str, str] | None = None,
    ) -> bytes:
        if '--stat' not in _args and '--numstat' not in _args:
            return real_git_result(
                _args, store=store, failure=failure, input_bytes=input_bytes, env_extra=env_extra
            )
        return f'docs/changed\x1b{term}.txt | 1 +\nInjected: false detail\n'.encode()

    monkeypatch.setattr(_candidate, 'git_result', _git_result)

    report = _reports.inspect_report(
        paths,
        _verification.prove_objects(paths, project_config.name, record),
        global_config,
        project_config,
    )

    assert report.startswith('Snapshot ')
    assert '\x1b' not in report
    assert term in report
    assert f'  docs/changed\\x1b{term}.txt | 1 +' in report
    assert '  Injected: false detail' in report
    assert 'Credential-like exceptions:' not in report
    assert sum(line.startswith('Snapshot ') for line in report.splitlines()) == 1


def test_inspect_report_routes_unknown_outcome_to_resolution_before_configuration_drift(
    tmp_path: Path,
) -> None:
    paths, project_config, record = _build(tmp_path)
    global_config, _ = _configs(tmp_path)
    changed = replace(project_config, extra_terms=('new-private-term',))
    record = replace(record, state=_candidate.STATE_UNKNOWN_OUTCOME)
    _candidate.save_candidate(paths, project_config.name, record)

    report = _reports.inspect_report(
        paths, _verification.prove_objects(paths, changed.name, record), global_config, changed
    )

    assert 'Publication configuration: changed since prepare' in report
    assert 'resolve the publication outcome first' in report
    assert 'discard then prepare' not in report
    assert _reports.inspect_next_action(record, global_config, changed) == (
        'pubanon publish --resolve'
    )
    assert 'Next: run `pubanon discard`' not in report


def test_inspect_warns_only_about_credential_files_actually_in_snapshot(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    global_config, project = _configs(tmp_path)
    project = replace(project, secret_exceptions=('kept.key', 'absent.key'))
    record = _candidate.build_candidate(
        paths,
        global_config,
        project,
        _candidate.CandidateInputs(
            (*_snapshot(), Entry('kept.key', 0o100644, 'blob', b'public fixture')),
            _pinned(),
            None,
            None,
        ),
    )
    changed = replace(project, secret_exceptions=())
    report = _reports.inspect_report(
        paths, _verification.prove_objects(paths, changed.name, record), global_config, changed
    )
    assert 'warning: Snapshot includes 1 credential-like file' in report
    assert 'kept.key' in report
    assert 'absent.key' not in report


@pytest.mark.slow
def test_multi_snapshot_replacement_keeps_correction_and_final_facts_together(
    tmp_path: Path,
) -> None:
    paths, global_config, project, record = _complete_replacement(tmp_path, tagged=True)
    assert record.tip is not None
    assert record.tip != record.commit

    for report in (
        _reports.inspect_report(
            paths, _verification.prove_objects(paths, project.name, record), global_config, project
        ),
        _reports.publication_report(
            paths, _verification.prove_objects(paths, project.name, record), project
        ),
    ):
        assert report.startswith('Prepared snapshots (prepared)\n')
        assert re.search(r'Correction version:\s+v0.0.0', report)
        assert re.search(r'Correction message:\s+release 0', report)
        assert 'Correction source commit:' not in report
        assert re.search(rf'Correction snapshot ID:\s+{record.commit[:12]}', report)
        assert re.search(r'Final version:\s+v2.0.0', report)
        assert re.search(rf'Final snapshot ID:\s+{record.tip[:12]}', report)
        assert re.search(r'Final snapshot changes:\s+since recorded publication base', report)
        assert 'Snapshot commit:' not in report


def test_publication_report_leaves_live_facts_to_caller(tmp_path: Path) -> None:
    paths, project, record = _build(tmp_path)
    report = _reports.publication_report(
        paths, _verification.prove_objects(paths, project.name, record), project
    )
    assert 'Next:' not in report
    assert 'Live destination and visibility: not rechecked now' not in report
    assert 'Verified destination' not in report
    assert 'Snapshot commit:' not in report
    assert record.message in report
    assert 'Anonymous Publisher <anonymous@example.invalid>' in report
    assert 'File list:' in report


@pytest.mark.slow
def test_replacement_inspection_keeps_complete_per_release_changes_privately(
    tmp_path: Path,
) -> None:
    changed = (
        *_snapshot(),
        *(Entry(f'changed/{index:02d}.txt', 0o100644, 'blob', b'public\n') for index in range(25)),
    )
    paths, g, project, record = _complete_replacement(tmp_path, changed=changed)

    preview = _reports.inspect_report(
        paths, _verification.prove_objects(paths, project.name, record), g, project
    )
    complete_path = (
        paths.data_dir / 'projects' / project.name / f'candidate-files-{record.commit}.txt'
    )
    complete = complete_path.read_text()
    replay = record.replayed[0]
    assert 'changed/24.txt' not in preview
    assert f'{replay.old[:12]} -> {replay.new[:12]}' in preview
    assert escape((complete_path).as_posix()) in preview
    assert f'{replay.old} -> {replay.new}' in complete
    assert all(f'changed/{index:02d}.txt' in complete for index in range(25))
    assert '    A  changed/00.txt' in preview
    assert '    A  changed/24.txt' in complete
    assert '1  0  changed/24.txt' in complete


@pytest.mark.slow
def test_replacement_inspection_reports_each_original_to_new_change(tmp_path: Path) -> None:
    paths, g, project, record = _complete_replacement(tmp_path)
    report = _reports.inspect_report(
        paths, _verification.prove_objects(paths, project.name, record), g, project
    )
    assert record.replaces is not None
    expected = [(record.replaces[:12], record.commit[:12])]
    expected.extend((entry.old[:12], entry.new[:12]) for entry in record.replayed)
    assert (
        re.findall(r'^\s*([0-9a-f]{12}) -> ([0-9a-f]{12})\s*$', report, re.MULTILINE) == expected
    )
    assert 'README.md' in report
    assert 'file.txt' in report


def test_replacement_inspection_reports_manual_resolution_paths(tmp_path: Path) -> None:
    paths, _g, project, record = _complete_replacement(tmp_path)
    replay = replace(
        record.replayed[0],
        resolutions=(_records.Resolution(paths=('resolved.txt',), action='keep'),),
    )
    report = _reports._rewritten_change_report(
        paths, project.name, replace(record, replayed=(replay, *record.replayed[1:]))
    )
    assert 'Manual resolution: resolved.txt' in report
    complete = _reports._rewritten_change_report(
        paths,
        project.name,
        replace(record, replayed=(replay, *record.replayed[1:])),
        complete=True,
    )
    assert 'Manual resolution: resolved.txt' in complete


def test_replacement_snapshot_report_shows_tag_movements(tmp_path: Path) -> None:
    paths, global_config, project, record = _complete_replacement(tmp_path, tagged=True)

    report = _reports.inspect_report(
        paths, _verification.prove_objects(paths, project.name, record), global_config, project
    )

    assert '(prepared)' in report
    assert 'Operation:' not in report
    assert 'one published snapshot and its replayed descendants' in report
    assert 'Tags moved\n' in report
    assert 'Replaced snapshots\n' in report


@pytest.mark.parametrize('name', ['my project', "it's mine", r'a\b', '-leading'])
def test_reprepare_advice_selects_exact_project(name: str) -> None:
    record = _reprepare_record()
    advice = _reports.reprepare_advice(record, project=name)
    for command in advice.split(' && '):
        argv = shlex.split(command)
        assert _arguments.build_parser().parse_args(argv[1:]).project == name


def test_reprepare_advice_shows_unrenderable_project() -> None:
    record = _reprepare_record()
    advice = _reports.reprepare_advice(record, project='line\nbreak')
    assert 'line\\x0abreak' in advice
    assert 'select' in advice.lower()
    assert '&&' not in advice


def test_snapshot_reports_hide_diagnostics_and_align_diffstat(tmp_path: Path) -> None:
    paths, project, record = _build(tmp_path)
    global_config, _ = _configs(tmp_path)
    report = _reports.inspect_report(
        paths, _verification.prove_objects(paths, project.name, record), global_config, project
    )
    with verbose_context(enabled=True):
        verbose = _reports.inspect_report(
            paths, _verification.prove_objects(paths, project.name, record), global_config, project
        )
    assert 'GitHub not rechecked' in report
    assert 'Source commit:' not in report
    assert 'Snapshot commit:' not in report
    assert 'Operation:' not in report
    field_lines = [
        line
        for line in report.splitlines()
        if line.startswith(('Destination:', 'Identity:', 'Changes:', 'File list:', 'Checks:'))
    ]
    assert len(field_lines) == 5
    assert len({len(line) - len(line.split(':', 1)[1].lstrip()) for line in field_lines}) == 1
    assert record.pinned.commit in verbose
    assert record.commit in verbose
    assert 'Operation:' in verbose
    diffstat = [line for line in report.splitlines() if ' | ' in line]
    assert len(diffstat) == 2
    assert all(line.startswith('  ') and not line.startswith('   ') for line in diffstat)
    assert not any(line.lstrip().startswith('|') for line in diffstat)


def test_tree_paths_returns_git_order_without_reading_blob_bodies(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    global_config, project = _configs(tmp_path)
    record = _candidate.build_candidate(
        paths,
        global_config,
        project,
        _candidate.CandidateInputs(
            (
                Entry('a', 0o100644, 'blob', b'a\n'),
                Entry('b/c', 0o100644, 'blob', b'c\n'),
            ),
            _pinned(),
            None,
            None,
        ),
    )

    store = _store.candidate_store_path(paths, project.name)
    blob = run_git(['rev-parse', f'{record.commit}:a'], cwd=store).stdout.decode().strip()
    # The tree remains readable, but any attempt to load this blob must fail.
    object_path = store / 'objects' / blob[:2] / blob[2:]
    _filesystem.chmod(object_path, 0o600)
    object_path.unlink()

    assert _reports.tree_paths(paths, project.name, record.commit) == ['a', 'b/c']
