import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Never

import pytest

from pubanon import (
    _cli,
    _config,
    _filesystem,
)
from pubanon._cli import _context, _inspect
from pubanon._errors import PubanonError
from pubanon._remote import _host, _releases, _stage
from pubanon._snapshots import (
    _candidate,
    _discard,
    _history,
    _records,
    _reports,
    _source,
    _store,
    _verification,
)
from pubanon._terminal import escape
from tests._cli import gh_calls, prepare, record_published_main
from tests._git import git_env, remove_tree
from tests._permissions import assert_private_mode
from tests._setup import configure


def test_inspect_ambiguous_tags_offer_one_manual_command_per_tag(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_gh: Path,
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, original = prepare(tmp_path, source, monkeypatch)
    store = _store.candidate_store_path(paths, project.name)
    tags = ('v1.2.1', 'v1.2.2')
    for ref in ('refs/heads/main', *(f'refs/tags/{tag}' for tag in tags)):
        subprocess.run(
            ['git', '-C', store, 'update-ref', ref, original.commit], check=True, env=git_env()
        )
    _discard.discard(paths, project.name, force=False)
    assert _cli.main(['prepare', '--project', project.name, '--replace', '--yes']) == 0
    replacement = _records.load_candidate(paths, project.name)
    assert replacement is not None
    assert replacement.replaces == original.commit
    assert replacement.tag_intent is None
    assert replacement.poststate_tags == dict.fromkeys(tags, replacement.commit)
    before = gh_calls(fake_gh.parent)
    capsys.readouterr()

    assert _cli.main(['inspect', '--project', project.name]) == 0

    rendered = capsys.readouterr().out
    assert 'Release page (planned): not created' in rendered
    assert 'v1.2.1 and v1.2.2' in rendered
    assert 'pubanon release create v1.2.1 --project=demo' in rendered
    assert 'pubanon release create v1.2.2 --project=demo' in rendered
    assert 'create "v1.2.1"' not in rendered
    assert gh_calls(fake_gh.parent) == before


@pytest.mark.parametrize(
    'case',
    [
        (
            {'README.md': 'public\n'},
            _config.ReleasePageSettings(enabled=True),
            'create "v1.2.3" with empty notes (no CHANGELOG.md; set release_page.changelog)',
        ),
        (
            {'README.md': 'public\n'},
            None,
            'not created; release pages are off for this project',
        ),
        (
            {'README.md': 'public\n'},
            _config.ReleasePageSettings(),
            'not created; release pages are off for this project',
        ),
        (
            {'README.md': 'public\n'},
            _config.ReleasePageSettings(enabled=False),
            'not created; release pages are off for this project',
        ),
    ],
)
def test_inspect_explains_page_without_changelog_or_when_disabled(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: tuple[dict[str, str], _config.ReleasePageSettings | None, str],
) -> None:
    files, release_page, expected = case
    source = make_repo(files=files)
    paths, project, _record = prepare(tmp_path, source, monkeypatch, version='1.2.3')
    _config.save_project(
        paths,
        replace(
            project,
            release_page=release_page,
        ),
    )
    capsys.readouterr()

    assert _cli.main(['inspect', '--project', project.name]) == 0

    assert f'Release page (planned): {expected}' in capsys.readouterr().out


@pytest.mark.parametrize(
    'case',
    [
        ('direct', 'malformed', 'pubanon publish --project=demo'),
        ('staged', 'absent', 'pubanon stage push --project=demo'),
        ('staged', 'matching', 'pubanon stage watch --project=demo'),
        ('staged', 'malformed', 'pubanon stage repair --project=demo'),
    ],
)
def test_inspect_guides_saved_route_without_host_reads_and_keeps_review_copy(
    case: tuple[str, str, str],
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    route, tracking, expected = case
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    project = replace(project, publication_route=route)
    _config.save_project(paths, project)
    if tracking == 'matching':
        _stage._save_stage(
            paths,
            project.name,
            _stage.StageRecord(record.commit, project.owner, _config.stage_repository(project)),
        )
        project = replace(project, required_checks=('build',))
        _config.save_project(paths, project)
        record = replace(
            record, fingerprint=_candidate.fingerprint(_config.load_global(paths), project)
        )
        _candidate.save_candidate(paths, project.name, record)
    elif tracking == 'malformed':
        stage_path = paths.data_dir / 'projects' / project.name / 'stage.toml'
        stage_path.parent.mkdir(parents=True, exist_ok=True)
        stage_path.write_text('invalid = true\n')

    def _unexpected_host(*_args: object, **_kwargs: object) -> Never:
        pytest.fail('host read')

    monkeypatch.setattr(_host, 'verify_destination', _unexpected_host)
    capsys.readouterr()

    assert _cli.main(['inspect', '--project', project.name]) == 0

    output = capsys.readouterr().out
    assert output.rstrip().endswith(f'Next: {expected}')
    inspection = paths.data_dir / 'projects' / project.name / 'inspections' / record.commit
    assert (inspection / 'README.md').read_text() == 'public\n'


def test_inspect_lists_preserved_copies_without_a_candidate(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, _global_config, project = configure(tmp_path, monkeypatch, source)
    assert project is not None
    root = paths.data_dir / 'projects' / project.name
    copies = [root / 'inspections' / (oid * 40) for oid in ('a', 'b')]
    for copy in copies:
        copy.mkdir(parents=True)
        (copy / 'notes.txt').write_text('Keep my review edits.\n')

    assert _cli.main(['inspect', '--project', project.name]) == 0

    output = capsys.readouterr().out
    assert output.startswith('No prepared snapshot')
    assert 'Preserved snapshot directories: 2' in output.splitlines()
    for copy in copies:
        assert output.count(escape((copy).as_posix()) + '\n') == 1
        assert (copy / 'notes.txt').read_text() == 'Keep my review edits.\n'
    assert 'directories in your editor' in output
    assert 'review their files' in output
    assert output.rstrip().endswith('`pubanon clean --project=demo`.')
    assert 'pubanon prepare' not in output
    assert _records.load_candidate(paths, project.name) is None

    assert _cli.main(['inspect', '--project', project.name, '--refresh']) == 1
    assert 'no prepared snapshot is retained' in capsys.readouterr().err
    for copy in copies:
        assert (copy / 'notes.txt').read_text() == 'Keep my review edits.\n'


def test_inspect_materializes_retained_candidate_after_configuration_drift(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, _record = prepare(tmp_path, source, monkeypatch)
    paths.global_config.write_text(
        'anon_name = "Changed Publisher"\n'
        'anon_email = "changed@example.invalid"\n'
        'sensitive_terms = ["public"]\n'
        'default_publication_route = "staged"\n'
        '[accounts]\n'
        'github = "changed-account"\n',
        encoding='utf-8',
    )
    _config.save_project(paths, replace(project, extra_terms=('new-private-term',)))
    (source / 'README.md').write_text('changed source\n', encoding='utf-8')
    subprocess.run(['git', '-C', source, 'add', 'README.md'], check=True, env=git_env())
    subprocess.run(
        ['git', '-C', source, 'commit', '-qm', 'change source'], check=True, env=git_env()
    )

    def _unexpected_authority_read(*_args: object, **_kwargs: object) -> Never:
        pytest.fail('inspection read current source or host authority')

    monkeypatch.setattr(_source, 'check_source_safety', _unexpected_authority_read)
    monkeypatch.setattr(_source, 'pin', _unexpected_authority_read)
    monkeypatch.setattr(_source, 'read_snapshot', _unexpected_authority_read)
    monkeypatch.setattr(_host, 'verify_destination', _unexpected_authority_read)
    older = paths.data_dir / 'projects' / project.name / 'inspections' / ('a' * 40)
    older.mkdir(parents=True)
    capsys.readouterr()

    assert _cli.main(['inspect', '--project', project.name]) == 0

    output = capsys.readouterr().out
    inspection = paths.data_dir / 'projects' / project.name / 'inspections' / _record.commit
    assert escape((inspection).as_posix()) in output
    assert 'Publication configuration: changed since prepare' in output
    assert 'checks have not been rerun with these settings' in output
    assert output.index('Publication configuration:') < output.index(
        escape((inspection).as_posix())
    )
    assert escape((older).as_posix()) not in output
    assert output.count('Next:') == 1
    assert f'pubanon prepare --project=demo --source {_record.pinned.commit}' in output
    assert (inspection / 'README.md').read_text(encoding='utf-8') == 'public\n'
    assert_private_mode(inspection, 0o500)
    assert_private_mode(inspection / 'README.md', 0o400)

    assert _cli.main(['inspect', '--project', project.name, '--verbose']) == 0
    verbose = capsys.readouterr().out
    assert escape((inspection).as_posix()) in verbose
    assert (inspection / 'README.md').read_text() == 'public\n'
    assert 'Directories from earlier snapshots\n' in verbose
    assert escape((older).as_posix()) in verbose
    assert 'pubanon clean' in verbose
    assert verbose.rstrip().splitlines()[-1].startswith('Next:')


def test_inspect_omits_untagged_page_and_workflow_preview(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    capsys.readouterr()
    assert _cli.main(['inspect', '--project', project.name]) == 0
    assert 'Release page (planned)' not in capsys.readouterr().out

    def _tagged(_record: _candidate.CandidateRecord) -> tuple[str, ...]:
        return ('v1.2.3',)

    monkeypatch.setattr(_releases, 'page_tags', _tagged)
    _inspect.inspection_copy(
        _context.load_context(paths, project.name),
        _verification.prove_objects(paths, project.name, record),
        workflow=True,
    )
    assert 'Release page (planned)' not in capsys.readouterr().out


def test_inspect_previews_tagged_page_without_host_read(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    fake_gh: Path,
) -> None:
    source = make_repo(files={'README.md': 'public\n', 'CHANGELOG.md': 'Changes\n'})
    paths, project, _record = prepare(tmp_path, source, monkeypatch, version='1.2.3')
    project = replace(project, release_page=_config.ReleasePageSettings(enabled=True))
    _config.save_project(paths, project)
    before = gh_calls(fake_gh.parent)
    capsys.readouterr()

    assert _cli.main(['inspect', '--project', project.name]) == 0

    rendered = capsys.readouterr()
    assert 'Release page (planned): create "v1.2.3"' in rendered.out
    assert (
        f'See the [changelog](https://github.com/{project.owner}/{project.repo}/blob/'
        'v1.2.3/CHANGELOG.md).' in rendered.out
    )
    assert rendered.err == ''
    assert gh_calls(fake_gh.parent) == before


def test_inspect_published_materializes_files_without_source_or_candidate(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch)
    record_published_main(paths, project, candidate)
    _records.candidate_path(paths, project.name).unlink()
    monkeypatch.chdir(tmp_path)
    remove_tree(source)
    capsys.readouterr()

    assert (
        _cli.main(['inspect', '--project', project.name, '--published', candidate.commit[:12]])
        == 0
    )
    inspection = paths.data_dir / 'projects' / project.name / 'inspections' / candidate.commit
    assert (inspection / 'README.md').read_text() == 'public\n'
    output = capsys.readouterr().out
    assert escape((inspection).as_posix()) in output
    assert output.rstrip().endswith(f'Next: pubanon show --project=demo {candidate.commit[:12]}')


@pytest.mark.slow
def test_inspect_published_refuses_modified_copy_until_refresh(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, candidate = prepare(tmp_path, source, monkeypatch)
    record_published_main(paths, project, candidate)
    assert candidate.pinned is not None
    bookmark = _source.describe_bookmark(source, project.name, candidate.commit)
    _source.ensure_bookmark(bookmark, candidate.pinned.commit)
    _history.record_publication(paths, project, replace(candidate, source_bookmark=bookmark))
    inspection = _verification.materialize_inspect(
        paths, project.name, _verification.prove_objects(paths, project.name, candidate)
    )
    _filesystem.chmod(inspection / 'README.md', 0o600)
    (inspection / 'README.md').write_text('edited\n')
    older = inspection.parent / ('a' * 40)
    older.mkdir()
    (older / 'notes').write_text('keep\n')
    _records.candidate_path(paths, project.name).unlink()
    monkeypatch.chdir(tmp_path)

    def _unexpected_authority(*_args: object, **_kwargs: object) -> Never:
        pytest.fail('published inspection accessed current source or host')

    monkeypatch.setattr(_host, 'verify_destination', _unexpected_authority)
    monkeypatch.setattr(_source, 'pin', _unexpected_authority)
    capsys.readouterr()

    command = ['inspect', '--project', project.name, '--published']
    assert _cli.main(command) == 1
    error = capsys.readouterr().err
    assert f'--published {candidate.commit} --refresh' in error
    assert (inspection / 'README.md').read_text() == 'edited\n'
    assert _cli.main([*command, '--refresh']) == 0
    output = capsys.readouterr().out
    assert escape((inspection).as_posix()) in output
    assert escape((older).as_posix()) not in output
    assert (inspection / 'README.md').read_text() == 'public\n'
    assert_private_mode(inspection, 0o500)
    assert_private_mode(inspection / 'README.md', 0o400)
    assert (older / 'notes').read_text() == 'keep\n'
    assert output.rstrip().endswith(f'Next: pubanon show --project=demo {candidate.commit[:12]}')


def test_inspect_refresh_reports_exact_location_before_removal(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    inspection = _verification.materialize_inspect(
        paths, project.name, _verification.prove_objects(paths, project.name, record)
    )
    older = inspection.parent / ('a' * 40)
    older.mkdir()
    (older / 'notes').write_text('older notes')
    original_remove_tree = _filesystem.remove_tree
    removals: list[Path] = []
    notices: list[str] = []

    def _remove_after_notice(path: Path) -> bool:
        output = capsys.readouterr().out
        notices.append(output)
        assert escape((path).as_posix()) in ''.join(notices)
        assert 'All current contents of this directory will be replaced.' in ''.join(notices) or (
            'All listed directories and their contents will be removed.' in ''.join(notices)
        )
        removed = original_remove_tree(path)
        removals.append(path)
        return removed

    monkeypatch.setattr(_filesystem, 'remove_tree', _remove_after_notice)
    capsys.readouterr()
    assert _cli.main(['inspect', '--project', project.name, '--refresh']) == 0
    assert removals == [inspection]
    assert (inspection / 'README.md').read_text() == 'public\n'
    assert (older / 'notes').read_text() == 'older notes'
    assert _records.load_candidate(paths, project.name) == record


def test_inspect_reports_rendering_failure_without_failing_command(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, _record = prepare(tmp_path, source, monkeypatch, version='1.2.3')
    _config.save_project(
        paths, replace(project, release_page=_config.ReleasePageSettings(enabled=True))
    )

    def _fail_render(*_args: object, **_kwargs: object) -> Never:
        raise PubanonError('bad\nnotes')

    monkeypatch.setattr(_releases, '_render_page', _fail_render)
    capsys.readouterr()

    assert _cli.main(['inspect', '--project', project.name]) == 0

    rendered = capsys.readouterr()
    assert f'Release page (planned): cannot be planned: {escape("bad\nnotes")}' in rendered.out
    assert rendered.err == ''


def test_inspect_rewrite_delegates_selector_and_refresh(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch)
    selected = paths.data_dir / 'projects' / project.name / 'inspections' / record.commit
    calls: list[tuple[str | None, bool]] = []

    def _inspect_rewritten(
        _paths: _config.Paths,
        _project: str,
        _proof: _verification.ObjectProof,
        selector: str | None,
        *,
        refresh: bool = False,
    ) -> Path:
        calls.append((selector, refresh))
        return selected

    monkeypatch.setattr(_verification, 'inspect_rewritten', _inspect_rewritten)

    def _select_rewritten_commit(_record: _candidate.CandidateRecord, _selector: str) -> str:
        return record.commit

    monkeypatch.setattr(_verification, 'select_rewritten_commit', _select_rewritten_commit)

    def _inspect_report(
        _paths: _config.Paths,
        _proof: _verification.ObjectProof,
        _global_config: _config.GlobalConfig,
        _project: _config.ProjectConfig,
    ) -> str:
        return 'Saved report'

    monkeypatch.setattr(_reports, 'inspect_report', _inspect_report)
    capsys.readouterr()
    assert (
        _cli.main(['inspect', '--project', project.name, '--rewritten', 'v1.0.0', '--refresh'])
        == 0
    )
    assert calls == [('v1.0.0', True)]
    output = capsys.readouterr().out
    assert f'Replacing review copy: {escape((selected).as_posix())}' in output
    assert escape((selected).as_posix()) in output
    assert f'  Snapshot: {record.commit[:12]}\n' in output
    assert output.index('Saved report') < output.index('Review copy')
    assert (
        _cli.main(['inspect', '--project', project.name, '--rewritten', 'v1.0.0', '--published'])
        == 1
    )
    assert calls == [('v1.0.0', True)]
