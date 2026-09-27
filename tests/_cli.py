import json
import subprocess
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest

from pubanon import (
    _cli,
    _config,
)
from pubanon._errors import PubanonError
from pubanon._remote import _host, _lifecycle, _watch
from pubanon._snapshots import _candidate, _history, _records, _source
from tests._git import git_env
from tests._setup import configure


def gh_calls(tmp_path: Path) -> list[list[str]]:
    path = tmp_path / 'fake-gh-calls.jsonl'
    if not path.exists():
        return []
    return [json.loads(line)['args'] for line in path.read_text().splitlines()]


def prepare(
    tmp_path: Path,
    source: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str | None = None,
) -> tuple[_config.Paths, _config.ProjectConfig, _candidate.CandidateRecord]:
    paths, _global_config, project_config = configure(tmp_path, monkeypatch, source)
    argv = ['prepare', '--project', 'demo', '--acknowledge-new-paths', '--yes']
    if version is not None:
        argv.extend(['--version', version])
    assert _cli.main(argv) == 0
    assert project_config is not None
    record = _candidate.load_candidate(paths, project_config.name)
    assert record is not None
    return paths, project_config, record


def published_package_reveal_context(
    tmp_path: Path,
    make_repo: Callable[..., Path],
    monkeypatch: pytest.MonkeyPatch,
    *,
    host_visibility: str,
    workflows: dict[str, tuple[str, ...]] | None = None,
) -> tuple[_config.Paths, _config.ProjectConfig]:
    source = make_repo(files={'README.md': 'public\n'})
    paths, project, record = prepare(tmp_path, source, monkeypatch, version='1.0.0')
    store = _candidate.store_path(paths, project.name)
    for ref in ('refs/heads/main', 'refs/tags/v1.0.0'):
        subprocess.run(
            ['git', '-C', store, 'update-ref', ref, record.commit],
            check=True,
            env=git_env(),
        )
    _candidate.discard(paths, project.name, force=True)
    project = replace(
        project,
        package=_config.PackageObservation('pypi', 'demo', ('Release',)),
        workflows=workflows or {},
    )
    _config.save_project(paths, project)
    destination = _host.Destination(
        adapter=project.host,
        owner=project.owner,
        repo=project.repo,
        url='https://github.com/anonymous-account/public-repo.git',
        account='anonymous-account',
        visibility=host_visibility,
    )
    refs = _host.RemoteRefs(record.commit, {'v1.0.0': record.commit})

    def _verified_destination(
        _paths: _config.Paths,
        _global_config: _config.GlobalConfig,
        selected: _config.ProjectConfig,
    ) -> _host.Destination:
        if selected.visibility != host_visibility:
            raise PubanonError(
                'repository visibility does not match configuration',
                fact='repository visibility',
                expected=selected.visibility,
                actual=host_visibility,
            )
        return destination

    def _repository_facts(
        _owner: str,
        _repo: str,
    ) -> tuple[str, str, str]:
        return project.owner, project.repo, host_visibility

    def _remote_refs(*_args: object, **_kwargs: object) -> _host.RemoteRefs:
        return refs

    def _require_account(*_args: object, **_kwargs: object) -> str:
        return project.owner

    def _successful_checks(*_args: object, **_kwargs: object) -> list[_host.CheckVerdict]:
        return [_host.CheckVerdict('Release', 'success', 'success')]

    monkeypatch.setattr(_host, 'verify_destination', _verified_destination)
    monkeypatch.setattr(_host, 'ls_remote', _remote_refs)
    monkeypatch.setattr(_lifecycle, 'require_authenticated_account', _require_account)
    monkeypatch.setattr(_lifecycle, 'repository_facts', _repository_facts)
    monkeypatch.setattr(_lifecycle, 'verify_destination', _verified_destination)
    monkeypatch.setattr(_lifecycle, 'ls_remote', _remote_refs)
    monkeypatch.setattr(_watch, 'ci_verdicts', _successful_checks)
    return paths, project


def record_publication(
    paths: _config.Paths,
    project: _config.ProjectConfig,
    record: _candidate.CandidateRecord,
) -> _records.PublicationRecord:
    bookmark = record.source_bookmark or _source.describe_bookmark(
        project.source_path, project.name, record.commit
    )
    _source.ensure_bookmark(bookmark, record.pinned.commit)
    record = replace(record, source_bookmark=bookmark)
    _candidate.save_candidate(paths, project.name, record)
    return _history.record_publication(paths, project, record)


def record_published_main(
    paths: _config.Paths,
    project: _config.ProjectConfig,
    record: _candidate.CandidateRecord,
) -> None:
    subprocess.run(
        [
            'git',
            '-C',
            _candidate.store_path(paths, project.name),
            'update-ref',
            'refs/heads/main',
            record.commit,
        ],
        check=True,
        env=git_env(),
    )


def save_workflows(
    paths: _config.Paths,
    project: _config.ProjectConfig,
    **workflows: tuple[str, ...],
) -> _config.ProjectConfig:
    updated = replace(project, workflows=workflows)
    _config.save_project(paths, updated)
    return updated


def stub_remote(
    monkeypatch: pytest.MonkeyPatch,
    destination: _host.Destination,
    refs: _host.RemoteRefs,
) -> None:
    def _verified_destination(
        _paths: _config.Paths,
        _global: _config.GlobalConfig,
        _project: _config.ProjectConfig,
        *,
        timeout: float | None = None,
    ) -> _host.Destination:
        del timeout
        return destination

    def _remote_refs(
        _destination: _host.Destination,
        _paths: _config.Paths,
        _project: str,
        *,
        timeout: float | None = None,
    ) -> _host.RemoteRefs:
        del timeout
        return refs

    monkeypatch.setattr(_host, 'verify_destination', _verified_destination)
    monkeypatch.setattr(_host, 'ls_remote', _remote_refs)
