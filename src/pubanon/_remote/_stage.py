"""Push retained candidates to a disposable staging repository."""

# pyright: reportPrivateUsage=false

import os
import re
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pubanon import _filesystem
from pubanon._config import (
    GlobalConfig,
    Paths,
    ProjectConfig,
    create_data_dirs,
    gate_host_coordinates,
    require_project_name,
    stage_repository,
    validate_host_segment,
)
from pubanon._errors import ObservationTimeoutError, PubanonError
from pubanon._remote._host import (
    URL_TEMPLATE,
    CheckVerdict,
    Destination,
    check_recovery,
    check_result,
    create_repository,
    delete_repository,
    push,
    repository_facts,
    repository_visible,
    require_authenticated_account,
    stage_refs,
)
from pubanon._run import OID_HEX
from pubanon._snapshots._candidate import (
    candidate_store_path,
    load_candidate,
    store_path,
    verify_retained_candidate,
)
from pubanon._snapshots._records import CandidateRecord, candidate_tip
from pubanon._terminal import confirm, escape, field, heading, next_step, output
from pubanon._toml import toml_string

_OID_PATTERN = re.compile(OID_HEX)
_STAGE_RECORD_KEYS = frozenset({'commit', 'owner', 'repo'})
_STAGE_REF = 'refs/heads/stage'
_monotonic = time.monotonic


@dataclass(frozen=True)
class StageRecord:
    """Retain the exact coordinates and commit used for staging.

    Attributes:
        commit: Candidate commit pushed to the staging repository.
    """

    commit: str
    _owner: str
    _repo: str


class _InvalidStageRecordError(PubanonError):
    """Identify malformed persisted staging state for cleanup routing."""


def _record_string(data: dict[str, object], key: str) -> str:
    """Return one required stage-record string."""
    value = data.get(key)
    if not isinstance(value, str):
        raise _InvalidStageRecordError(
            'invalid stage record',
            recovery='run interactively: pubanon stage clean',
        )
    return value


def _remaining_timeout(timeout: float, started: float) -> float:
    """Return the positive remainder of one supplied timeout budget."""
    remaining = timeout - (_monotonic() - started)
    if remaining <= 0:
        raise ObservationTimeoutError
    return remaining


def _stage_destination(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
) -> Destination:
    """Build one locally validated private staging destination."""
    name = stage_repository(p)
    gate_host_coordinates(paths, g, p, coordinates=(p.owner, name))
    if name.casefold() == p.repo.casefold():
        raise PubanonError('staging repository must differ from the publication repository')
    return Destination(
        adapter=p.host,
        owner=p.owner,
        repo=name,
        url=URL_TEMPLATE.format(owner=p.owner, repo=name),
        account=g.accounts[p.host],
        visibility='private',
    )


def _stage_next_action(p: ProjectConfig) -> str:
    """Choose the next configured step after staging."""
    if p.required_checks:
        return 'pubanon watch ci --staged'
    return 'pubanon publish'


def _stage_path(paths: Paths, project: str) -> Path:
    """Return one validated project's stage-record path."""
    require_project_name(project)
    return paths.data_dir / 'projects' / project / 'stage.toml'


def _clear_stage(paths: Paths, project: str) -> None:
    """Remove one advisory stage record if present."""
    try:
        _stage_path(paths, project).unlink(missing_ok=True)
    except (OSError, RuntimeError) as exc:
        raise PubanonError('cannot clear stage record') from exc


def _abandon_stage(paths: Paths, project: str, *, yes: bool) -> None:
    """Require an explicit interactive choice before clearing local evidence."""
    if yes:
        raise PubanonError(
            'local staging evidence abandonment requires interactive confirmation',
            recovery='run interactively: pubanon stage clean',
        )
    if not confirm('Forget the local staging record without deleting the staging repository?'):
        raise PubanonError('staging evidence abandonment declined')
    _clear_stage(paths, project)


def _validate_stage(record: StageRecord) -> None:
    """Validate one in-memory stage record against the frozen schema."""
    try:
        validate_host_segment('owner', record._owner)
        validate_host_segment('repo', record._repo)
    except PubanonError as exc:
        raise _InvalidStageRecordError(
            'invalid stage record',
            recovery='run interactively: pubanon stage clean',
        ) from exc
    if _OID_PATTERN.fullmatch(record.commit) is None:
        raise _InvalidStageRecordError(
            'invalid stage record',
            recovery='run interactively: pubanon stage clean',
        )


def _save_stage(paths: Paths, project: str, record: StageRecord) -> None:
    """Save one strict stage record with owner-only POSIX permissions."""
    _validate_stage(record)
    try:
        content = (
            f'commit = {toml_string(record.commit)}\n'
            f'owner = {toml_string(record._owner)}\n'
            f'repo = {toml_string(record._repo)}\n'
        )
    except ValueError as exc:
        raise _InvalidStageRecordError(
            'invalid stage record',
            recovery='run interactively: pubanon stage clean',
        ) from exc
    path = _stage_path(paths, project)
    create_data_dirs(paths, project)
    try:
        path.write_text(content, encoding='utf-8')
        _filesystem.chmod(path, 0o600)
    except (OSError, RuntimeError, UnicodeError) as exc:
        raise PubanonError('cannot save stage record') from exc


def _verified_stage_tip(
    paths: Paths, g: GlobalConfig, p: ProjectConfig, record: CandidateRecord
) -> str:
    """Verify retained candidate gates and select the resulting commit."""
    verify_retained_candidate(paths, g, p, record, env=os.environ)
    return candidate_tip(record)


def _verify_stage_facts(
    dest: Destination,
    *,
    recovery: str,
    timeout: float | None = None,
) -> None:
    """Require exact private facts for one staging destination."""
    actual = repository_facts(dest.owner, dest.repo, timeout=timeout, missing_recovery=recovery)
    expected = (dest.owner, dest.repo, 'private')
    if actual != expected:
        raise PubanonError(
            'staging repository facts do not match expected private coordinates',
            expected=(
                f'owner={escape(expected[0])}; '
                f'repo={escape(expected[1])}; '
                f'visibility={escape(expected[2])}'
            ),
            actual=(
                f'owner={escape(actual[0])}; '
                f'repo={escape(actual[1])}; '
                f'visibility={escape(actual[2])}'
            ),
            recovery=recovery,
        )


def _verify_stage_refs(
    unexpected: tuple[str, ...],
) -> None:
    """Refuse every staging reference outside the single managed branch."""
    if unexpected:
        raise PubanonError(
            'staging repository contains an unexpected staging reference',
            fact=', '.join(escape(ref) for ref in unexpected),
            recovery='pubanon stage clean',
        )


def load_stage(paths: Paths, project: str) -> StageRecord | None:
    """Load one strict stage record if present.

    Args:
        paths: Derived Pubanon configuration and data paths.
        project: Registered project name.

    Returns:
        The validated stage record, or `None` when absent.

    Raises:
        PubanonError: Persisted staging state is malformed.
    """
    path = _stage_path(paths, project)
    try:
        if not path.exists():
            return None
        data = tomllib.loads(path.read_text(encoding='utf-8'))
    except (OSError, RuntimeError, UnicodeError, ValueError) as exc:
        raise _InvalidStageRecordError(
            'invalid stage record',
            recovery='run interactively: pubanon stage clean',
        ) from exc
    if data.keys() != _STAGE_RECORD_KEYS:
        raise _InvalidStageRecordError(
            'invalid stage record',
            recovery='run interactively: pubanon stage clean',
        )
    record = StageRecord(
        commit=_record_string(data, 'commit'),
        _owner=_record_string(data, 'owner'),
        _repo=_record_string(data, 'repo'),
    )
    _validate_stage(record)
    return record


def stage_clean(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
    *,
    yes: bool,
) -> None:
    """Delete the recorded staging repository or the configured fallback.

    A valid record owns the selected coordinate pair even after staging
    configuration drift. Cleanup refuses a selected pair that matches the
    current publication destination case-insensitively. An absent or
    inaccessible response does not prove host absence; recorded local evidence
    may be abandoned only through its separate interactive confirmation.

    The caller must hold the project lock for the complete operation.

    Args:
        paths: Derived Pubanon configuration and data paths.
        g: Global publication identity configuration.
        p: Project publication configuration.

        yes: Whether to skip the deletion confirmation. This cannot authorize
            local evidence abandonment.

    Raises:
        PubanonError: Local guards, host facts, deletion, or read-back are
            unsafe.
    """
    try:
        stage = load_stage(paths, p.name)
    except _InvalidStageRecordError:
        _abandon_stage(paths, p.name, yes=yes)
        output('Abandoned unreadable local staging evidence.', kind='warning')
        output(
            'No host deletion was attempted because repository coordinates could not be trusted.'
        )
        output('The source repository and publication repository were not changed.')
        output()
        output('Inspect the host account for any private staging repository.')
        return
    configured = (p.owner, stage_repository(p))
    owner, repo = (stage._owner, stage._repo) if stage is not None else configured
    if (owner.casefold(), repo.casefold()) == (p.owner.casefold(), p.repo.casefold()):
        raise PubanonError(
            'staging cleanup target is the configured publication repository',
            recovery=(
                'inspect the recorded staging target and correct the configuration before retrying'
            ),
        )
    if stage is not None and (owner, repo) != configured:
        output(
            'configured staging coordinates changed; cleaning the recorded repository',
            kind='warning',
        )
    if stage is None:
        dest = _stage_destination(paths, g, p)
    else:
        dest = Destination(
            adapter=p.host,
            owner=owner,
            repo=repo,
            url=URL_TEMPLATE.format(owner=owner, repo=repo),
            account=g.accounts[p.host],
            visibility='private',
        )

    require_authenticated_account(dest.account, authority='staging configuration')
    if not repository_visible(dest.owner, dest.repo):
        if stage is not None:
            _abandon_stage(paths, p.name, yes=yes)
            output(
                'Abandoned local staging evidence for an absent or inaccessible repository.',
                kind='warning',
            )
            output('No host repository was deleted.')
            output('The source repository and publication repository were not changed.')
            output()
            output('Inspect the host account to confirm whether the staging repository exists.')
        else:
            output(
                'The staging repository is absent or inaccessible to this request.', kind='warning'
            )
            output('No local staging evidence was found, and no host deletion was attempted.')
            output('The source repository and publication repository were not changed.')
            output()
            output('Inspect the host account to confirm whether the staging repository exists.')
        return
    _verify_stage_facts(dest, recovery='inspect and delete the staging repository manually')

    heading('Staging deletion preview')
    for label, value in {
        'Repository': f'{dest.owner}/{dest.repo}',
        'Visibility': dest.visibility,
        'Selection': 'saved staging record' if stage is not None else 'configured fallback',
        'Current configured staging name': f'{configured[0]}/{configured[1]}',
    }.items():
        output(field(escape(label), escape(value)))
    output(
        'This deletes the selected host repository and clears its local staging record.',
        kind='warning',
    )
    if not yes and not confirm(
        f'Delete the staging repository {escape(dest.owner)}/{escape(dest.repo)}?'
    ):
        raise PubanonError('staging deletion declined')
    delete_repository(dest.owner, dest.repo)
    if repository_visible(dest.owner, dest.repo):
        raise PubanonError(
            'staging deletion did not complete',
            recovery='rerun pubanon stage clean',
        )
    _clear_stage(paths, p.name)
    output(
        'Deletion read-back confirmed the selected staging repository is absent.', kind='success'
    )
    output(
        'No local staging record existed.'
        if stage is None
        else 'Cleared the local staging record.'
    )
    output('The source repository and publication repository were not changed.')


def stage_push(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
    *,
    yes: bool,
) -> None:
    """Push one retained candidate to the private staging branch.

    Create a missing private staging repository after first-use confirmation.
    Replace only the staging branch and record local staging evidence after
    verifying the pushed commit.

    The caller must hold the project lock for the complete operation.

    Args:
        paths: Derived Pubanon configuration and data paths.
        g: Global publication identity configuration.
        p: Project publication configuration.

        yes: Whether to skip the replacement-push confirmation. This does not
            confirm first-use repository creation.

    Raises:
        PubanonError: Local state, host facts, creation, refs, push, read-back,
            or evidence persistence fails. The staging repository may have
            changed before a later failure.
    """
    record = load_candidate(paths, p.name)
    if record is None:
        raise PubanonError('no prepared snapshot is retained', recovery='pubanon prepare')
    tip = _verified_stage_tip(paths, g, p, record)
    dest = _stage_destination(paths, g, p)
    stage = load_stage(paths, p.name)
    if stage is not None and (stage._owner, stage._repo) != (dest.owner, dest.repo):
        raise PubanonError(
            'configured staging coordinates differ from the recorded staging coordinates',
            expected=(f'{escape(stage._owner)}/{escape(stage._repo)}'),
            actual=(f'{escape(dest.owner)}/{escape(dest.repo)}'),
            recovery='pubanon stage clean removes the recorded staging repository first',
        )

    require_authenticated_account(dest.account, authority='staging configuration')
    if creating_repository := not repository_visible(dest.owner, dest.repo):
        heading('Staging repository creation')
        output(field('Private GitHub repository', f'{escape(dest.owner)}/{escape(dest.repo)}'))
        output(field('Prepared snapshot', tip[:7]))
        if not confirm(
            f'Create the private staging repository {escape(dest.owner)}/{escape(dest.repo)}?'
        ):
            raise PubanonError('staging repository creation declined')
        create_repository(dest, None)
    _verify_stage_facts(dest, recovery='pubanon stage clean')

    remote_stage, unexpected = stage_refs(dest, paths, p.name)
    _verify_stage_refs(unexpected)
    expected_old = stage.commit if stage is not None else None
    if remote_stage != expected_old:
        raise PubanonError(
            'surprise staging state',
            expected=escape(expected_old or 'absent'),
            actual=escape(remote_stage or 'absent'),
            recovery='run `pubanon stage clean`, then restage with `pubanon stage push`',
        )

    heading('Stage prepared snapshot', separate=creating_repository)
    output('  ' + field('Destination', f'GitHub {escape(dest.owner)}/{escape(dest.repo)}'))
    output('  Staging uses a private repository and branch stage.')
    output(
        '  ' + field('Current staged commit', escape(expected_old[:7] if expected_old else 'none'))
    )
    output('  ' + field('Prepared version', escape(record.tag_intent or 'none')))
    output('  Git details:')
    output(f'  {escape(tip)}:{_STAGE_REF}')
    output(f'  force-with-lease {_STAGE_REF}:{escape(expected_old or "absent")}')
    if not yes and not confirm(f'Push {tip[:7]} to the staging repository?'):
        raise PubanonError('staging push declined')
    push(
        dest,
        store_path(paths, p.name),
        [
            '--atomic',
            f'--force-with-lease={_STAGE_REF}:{expected_old or ""}',
            '--no-follow-tags',
            f'{tip}:{_STAGE_REF}',
        ],
    )

    try:
        readback, unexpected = stage_refs(dest, paths, p.name)
    except PubanonError as exc:
        raise PubanonError(
            'cannot verify staging push read-back',
            fact=exc.render(),
            recovery='pubanon stage clean',
        ) from exc
    _verify_stage_refs(unexpected)
    if readback != tip:
        raise PubanonError(
            'staging read-back does not match the prepared snapshot commit',
            expected=escape(tip),
            actual=escape(readback or 'absent'),
            recovery='pubanon stage clean',
        )
    try:
        _save_stage(paths, p.name, StageRecord(tip, dest.owner, dest.repo))
    except PubanonError as exc:
        raise PubanonError(
            'cannot record staging push',
            recovery='pubanon stage clean',
        ) from exc
    output('Prepared snapshot staged and verified in the private repository.', kind='success')
    output('The source repository and publication repository were not changed.')
    output()
    output(field('Staged commit', tip))
    next_step(_stage_next_action(p))


def staging_repository_visible(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
) -> bool:
    """Report staging visibility after local gates and the account check.

    Args:
        paths: Derived Pubanon configuration and data paths.
        g: Global publication identity configuration.
        p: Project publication configuration.

    Returns:
        Whether the configured staging repository is visible to the request.
        `False` does not distinguish an absent repository from an inaccessible
        one.

    Raises:
        PubanonError: Staging coordinates fail validation or content gates, the
            authenticated account cannot be read or is invalid, the account
            differs from staging configuration, or the repository visibility
            request fails.
    """
    dest = _stage_destination(paths, g, p)
    require_authenticated_account(dest.account, authority='staging configuration')
    return repository_visible(dest.owner, dest.repo)


def verify_staged_target(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
    *,
    timeout: float | None = None,
) -> tuple[Destination, StageRecord]:
    """Verify and return one exact staged destination and commit record.

    Args:
        paths: Derived Pubanon configuration and data paths.
        g: Global publication identity configuration.
        p: Project publication configuration.

        timeout: Optional shared verification budget in seconds.

    Returns:
        The verified staging destination and exact local stage record.

    Raises:
        PubanonError: Any ordered local, destination, or reference proof fails.
        ObservationTimeoutError: A supplied observation timeout expires, with
            the pending host-read stage retained for the watcher.
    """
    started = 0.0
    if timeout is not None:
        started = _monotonic()
    record = load_stage(paths, p.name)
    if record is None:
        raise PubanonError('no staged commit is recorded', recovery='pubanon stage push')
    configured = (p.owner, stage_repository(p))
    if (record._owner, record._repo) != configured:
        raise PubanonError(
            'configured staging coordinates differ from the recorded staging coordinates',
            expected=(f'{escape(record._owner)}/{escape(record._repo)}'),
            actual=(f'{escape(configured[0])}/{escape(configured[1])}'),
            recovery='pubanon stage clean',
        )
    destination = _stage_destination(paths, g, p)
    if not candidate_store_path(paths, p.name).is_dir():
        raise PubanonError(
            'snapshot store is absent',
            recovery='pubanon repo verify --repair-store',
        )
    try:
        if timeout is None:
            require_authenticated_account(destination.account, authority='staging configuration')
            _verify_stage_facts(destination, recovery='pubanon stage clean')
        else:
            require_authenticated_account(
                destination.account,
                authority='staging configuration',
                timeout=_remaining_timeout(timeout, started),
            )
            _verify_stage_facts(
                destination,
                recovery='pubanon stage clean',
                timeout=_remaining_timeout(timeout, started),
            )
    except ObservationTimeoutError as exc:
        raise ObservationTimeoutError(stage='account/repository verification') from exc
    try:
        if timeout is None:
            remote_stage, unexpected = stage_refs(destination, paths, p.name)
        else:
            remote_stage, unexpected = stage_refs(
                destination,
                paths,
                p.name,
                timeout=_remaining_timeout(timeout, started),
            )
    except ObservationTimeoutError as exc:
        raise ObservationTimeoutError(stage='refs') from exc
    _verify_stage_refs(unexpected)
    if remote_stage != record.commit:
        raise PubanonError(
            'staging tip does not match the recorded staged commit',
            expected=escape(record.commit),
            actual=escape(remote_stage or 'absent'),
            recovery='pubanon stage push',
        )
    return destination, record


def stage_status(
    paths: Paths,
    g: GlobalConfig,
    p: ProjectConfig,
) -> None:
    """Report required checks for the exact staged commit.

    Args:
        paths: Derived Pubanon configuration and data paths.
        g: Global publication identity configuration.
        p: Project publication configuration.

    Raises:
        PubanonError: Local state, host facts, refs, or checks are
            unacceptable.
    """
    dest, stage = verify_staged_target(paths, g, p)

    if not p.required_checks:
        output('No required checks are configured for the staged snapshot.', kind='warning')
        output()
        output(field('Staged commit', escape(stage.commit)))
        next_step(_stage_next_action(p))
        return

    verdicts: list[CheckVerdict] = []
    for name in p.required_checks:
        verdict = check_result((dest.owner, dest.repo), stage.commit, name)
        verdicts.append(verdict)
    if all(verdict.state == 'success' for verdict in verdicts):
        output('All configured required checks succeeded for the staged snapshot.', kind='success')
        heading('Details:', separate=True)
        output('  ' + field('Staged commit', escape(stage.commit)))
        for verdict in verdicts:
            output(
                '  '
                + field(
                    escape(verdict.name),
                    f'{escape(verdict.state)} ({escape(verdict.detail)})',
                    kind='success',
                )
            )
        output()
        next_step('pubanon publish')
        return
    for verdict in verdicts:
        output(
            '  '
            + field(
                escape(verdict.name),
                f'{escape(verdict.state)} ({escape(verdict.detail)})',
                kind='warning',
            )
        )
    raise PubanonError(
        'required checks are not acceptable for the staged commit',
        fact=','.join(f'{escape(verdict.name)}:{escape(verdict.state)}' for verdict in verdicts),
        recovery=check_recovery(verdicts, retry='rerun `pubanon stage status`'),
    )
